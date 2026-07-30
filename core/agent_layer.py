
"""智能体层：规划、DAG构建、执行、事件翻译。

职责：
- 规划（PlanAgent → DAGPlan）
- DAG 构建（GraphFlow）
- 单Agent执行 / 全团队执行
- 结果汇总（DataContext → TABLE事件 + 自然语言总结）

不负责：
- 会话生命周期管理（DispatchLayer / SessionManager）
- 路由决策（RoutingLayer）
- Agent 创建（AgentFactory，独立模块）
- 事件翻译（EventTranslator，独立模块）
- DataFrame→TABLE 转换（table_formatter，独立模块）
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from autogen_agentchat.base import ChatAgent, Response
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    StructuredMessage,
    TextMessage,
)
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow
from autogen_core.models import ChatCompletionClient

from agents.base import looks_like_failure
from agents.plan_agent import PlanAgent
from config import settings
from core.agent_factory import AgentFactory
from core.context import SessionContext, TaskContext
from core.data_context import DataContext
from core.data_context_cache import DataContextCache, InMemoryDataContextCache
from core.event_translator import EventTranslator, _needs_summarization
from core.table_formatter import emit_table_event_for_key, emit_table_events
from models import DAGPlan, TaskNode
from models.plan_output import PlanStep
from models.routing import AgentType, RoutingResult
from models.stream_event import StreamEvent, StreamEventType
from models.turn_result import AgentExecutionRecord, PlanRecord, TurnResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# AgentLayer — 智能体harness
# ---------------------------------------------------------------------------

class AgentLayer:
    """智能体层：规划、DAG构建、执行、生命周期。

    Responsibilities:
    - 规划（_plan, _parse_plan_data）
    - DAG 构建（_build_dag）
    - 单Agent执行（run_single_agent）
    - 全团队执行（run_team）

    Does NOT:
    - 管理会话（DispatchLayer）
    - 路由查询（RoutingLayer）
    - 创建Agent（AgentFactory）
    """

    def __init__(
        self,
        model_client: ChatCompletionClient,
        selector_client: ChatCompletionClient,
        data_context_cache: DataContextCache | None = None,
    ) -> None:
        self._model_client = model_client
        self._selector_client = selector_client
        self._data_context_cache = data_context_cache or InMemoryDataContextCache()
        # PlanAgent 产出的 DAGPlan（_plan async generator 写入，run_team 读取）
        self._last_plan: DAGPlan | None = None

    # ------------------------------------------------------------------
    # 单Agent快速路径
    # ------------------------------------------------------------------

    async def run_single_agent(
        self,
        routing: RoutingResult,
        session_ctx: SessionContext,
        seq: list[int],
        session_start_time: datetime,
        turn_result: TurnResult,
    ) -> AsyncGenerator[StreamEvent, None]:
        """单Agent快速路径：创建Agent → run → yield事件。

        Args:
            turn_result: 显式结果载体，执行记录和摘要写入此对象，供 SessionManager 消费。
        """
        translator = EventTranslator(session_ctx.session_id, seq)

        dc = DataContext()
        # 从缓存恢复历史数据，使当前 Agent 能访问前序 turn 产生的 DataFrame
        await self._data_context_cache.restore(session_ctx.session_id, dc)
        if routing.agent_type is not None:
            agent = AgentFactory.create(
                routing.agent_type, self._model_client, dc,
                session=session_ctx, task_id="direct", task_description=routing.task_description,
            )
        else:
            agent = AgentFactory.create_default(
                self._model_client, dc,
                session=session_ctx, task_id="direct", task_description=routing.task_description,
            )

        # PLAN_COMPLETE（无需规划）
        yield translator.make_plan_complete(
            content=routing.reasoning,
            agent_name="Router",
            reasoning=routing.reasoning,
        )

        # AGENT_START
        agent_type_str = routing.agent_type.value if routing.agent_type else "RAGAgent"
        yield translator.make_agent_start(
            agent_name=agent.name,
            agent_type=agent_type_str,
            task_id="direct",
            task_description=routing.task_description,
        )

        final_result = "未能生成结果。"

        try:
            agent_start = datetime.now()
            from autogen_core import CancellationToken
            cancellation_token = CancellationToken()

            user_msg = TextMessage(content=routing.task_description, source="user")
            timeout_sec = settings.agent_execution_timeout
            try:
                async with asyncio.timeout(timeout_sec):
                    async for event in agent.on_messages_stream([user_msg], cancellation_token):
                        for stream_ev in translator.translate(event):
                            yield stream_ev
            finally:
                cancellation_token.cancel()

            duration_ms = int((datetime.now() - agent_start).total_seconds() * 1000)
            content = translator.last_content or final_result
            agent_end_ev = translator.make_agent_end(
                agent_name=agent.name,
                task_id="direct",
                content=content,
                duration_ms=duration_ms,
            )
            final_result = content

            # 发送 TABLE 事件（DataContext 中的结构化数据）
            for table_ev in emit_table_events(translator, dc):
                yield table_ev

            # 后台摘要数据（由 SessionManager.finish() 触发）
            bg_summary: str | None = None
            if dc.list_keys() and _needs_summarization(content):
                data_summary = dc.all_summaries()
                if data_summary and data_summary != "DataContext 当前为空，没有可用数据。":
                    bg_summary = data_summary

            # Agent 执行记录 → 写入 TurnResult（替代 StreamEvent.data sideband）
            turn_result.agent_records.append(AgentExecutionRecord(
                session_id=session_ctx.session_id,
                chat_id=session_ctx.chat_id,
                task_id="direct",
                agent_type=agent_type_str,
                agent_name=agent.name,
                task_description=routing.task_description,
                status="success",
                result_preview=content,
                duration_ms=duration_ms,
                finished_at=datetime.now().isoformat(),
            ))
            if bg_summary:
                turn_result.bg_summary = bg_summary
            yield agent_end_ev

            # 保存 DataContext 到缓存，供后续 turn 恢复
            if dc.list_keys():
                await self._data_context_cache.save(session_ctx.session_id, dc)
                # 合并结果到 SessionContext（供 _assemble_turn_summary 使用）
                task_ctx = TaskContext("direct", session_ctx, data_context=dc)
                task_ctx.set_final_answer(final_result)
                await task_ctx.merge_to_session()
        except Exception as e:
            logger.error("单Agent执行失败: %s", e, exc_info=True)
            final_result = f"抱歉，执行失败。({e})"
            error_ev = translator.make_error(
                content=final_result,
                error_type=type(e).__name__,
                message=str(e),
            )
            # Agent 执行记录（失败）→ 写入 TurnResult
            turn_result.agent_records.append(AgentExecutionRecord(
                session_id=session_ctx.session_id,
                chat_id=session_ctx.chat_id,
                task_id="direct",
                agent_type=agent_type_str,
                agent_name=agent.name,
                task_description=routing.task_description,
                status="error",
                error_type=type(e).__name__,
                error_message=str(e),
                duration_ms=int((datetime.now() - agent_start).total_seconds() * 1000),
                finished_at=datetime.now().isoformat(),
            ))
            yield error_ev

    # ------------------------------------------------------------------
    # 全团队路径
    # ------------------------------------------------------------------

    async def run_team(
        self,
        query: str,
        routing: RoutingResult,
        session_ctx: SessionContext,
        seq: list[int],
        session_start_time: datetime,
        turn_result: TurnResult,
    ) -> AsyncGenerator[StreamEvent, None]:
        """全团队路径：PlanAgent + DAG 流水线。

        Args:
            turn_result: 显式结果载体，执行记录和摘要写入此对象，供 SessionManager 消费。
        """
        translator = EventTranslator(session_ctx.session_id, seq)

        # Phase 1: 规划
        async for ev in self._plan_phase(query, session_ctx, translator, turn_result):
            yield ev
        plan = self._last_plan
        if plan is None:
            return

        # Phase 2: DAG 构建
        shared_dc = DataContext()
        await self._data_context_cache.restore(session_ctx.session_id, shared_dc)
        try:
            team, task_contexts = self._build_dag_phase(plan, session_ctx, shared_dc)
        except Exception as e:
            logger.error("DAG构建失败: %s", e, exc_info=True)
            yield translator.make_error(
                content=f"DAG构建失败: {e}",
                error_type="DAGBuildError",
                message=str(e),
            )
            return

        # Phase 3: DAG 执行
        final_result = "未能生成结果。"
        emitted_table_keys: set[str] = set()
        try:
            async with asyncio.timeout(180):
                async for event in team.run_stream(task=query):
                    if isinstance(event, (BaseAgentEvent, BaseChatMessage)):
                        for stream_ev in translator.translate(event, plan=plan):
                            self._append_dag_agent_record(stream_ev, plan, session_ctx, turn_result)
                            yield stream_ev
                            if stream_ev.type == StreamEventType.AGENT_END:
                                final_result = stream_ev.content
                                async for table_ev in self._detect_failure_and_emit_tables(
                                    stream_ev, task_contexts, emitted_table_keys, translator, session_ctx,
                                ):
                                    yield table_ev
                    elif hasattr(event, "messages"):
                        if hasattr(event, "stop_reason") and event.stop_reason:
                            pass  # stop_reason 由 DispatchLayer 的 TraceRecorder 处理
        except Exception as e:
            logger.error("DAG执行失败: %s", e, exc_info=True)
            for task_node in plan.tasks:
                session_ctx._failed_task_ids.add(task_node.task_id)
            yield translator.make_error(
                content=f"执行失败: {e}",
                error_type=type(e).__name__,
                message=str(e),
            )
            return

        # Phase 4: 收尾
        close_events = list(translator.close_all_agents())
        for close_ev in close_events:
            yield close_ev
            final_result = close_ev.content

        bg_summary, _ = await self._post_dag_cleanup(
            task_contexts, session_ctx, final_result, close_events,
        )
        if bg_summary:
            turn_result.bg_summary = bg_summary

    # ------------------------------------------------------------------
    # run_team 子方法
    # ------------------------------------------------------------------

    async def _plan_phase(
        self,
        query: str,
        session_ctx: SessionContext,
        translator: EventTranslator,
        turn_result: TurnResult,
    ) -> AsyncGenerator[StreamEvent, None]:
        """规划阶段：PLAN_START → _plan → PLAN_COMPLETE。"""
        yield translator.make_plan_start()

        plan_start = datetime.now()
        self._last_plan = None
        async for ev in self._plan(query, session_ctx=session_ctx, translator=translator):
            yield ev
        plan = self._last_plan
        plan_duration = int((datetime.now() - plan_start).total_seconds() * 1000)
        logger.info("[AgentLayer] DAG规划完成: %d个任务", len(plan.tasks))

        # Plan 记录 → 写入 TurnResult（替代 StreamEvent.data sideband）
        turn_result.plan_record = PlanRecord(
            session_id=session_ctx.session_id,
            chat_id=session_ctx.chat_id,
            task_count=len(plan.tasks),
            tasks_json=[t.model_dump() for t in plan.tasks],
            reasoning=plan.reasoning,
            is_complete=plan.is_complete,
            duration_ms=plan_duration,
        )

        if plan.is_complete or not plan.tasks:
            plan_ev = translator.make_plan_complete(
                content=plan.reasoning,
                agent_name="PlanAgent",
                reasoning=plan.reasoning,
            )
            yield plan_ev
            return

        tasks_data = [t.model_dump() for t in plan.tasks]
        edges = [{"from": dep, "to": t.task_id} for t in plan.tasks for dep in t.depends_on]
        plan_ev = translator.make_plan_complete(
            content=f"规划完成，共 {len(plan.tasks)} 个任务",
            agent_name="PlanAgent",
            tasks=tasks_data,
            edges=edges,
            reasoning=plan.reasoning,
        )
        yield plan_ev

    def _build_dag_phase(
        self,
        plan: DAGPlan,
        session_ctx: SessionContext,
        shared_dc: DataContext,
    ) -> tuple[GraphFlow, dict[str, TaskContext]]:
        """DAG构建阶段：构建 task_contexts + DAG + 清空失败追踪集。"""
        task_contexts: dict[str, TaskContext] = {"__session__": session_ctx}  # type: ignore[dict-item]
        team, _agents = self._build_dag(plan, task_contexts, shared_dc)
        session_ctx._failed_task_ids = set()
        return team, task_contexts

    def _append_dag_agent_record(
        self,
        stream_ev: StreamEvent,
        plan: DAGPlan,
        session_ctx: SessionContext,
        turn_result: TurnResult,
    ) -> None:
        """为 DAG 路径的 AGENT_END 事件追加 Agent 执行记录到 TurnResult。"""
        if stream_ev.type != StreamEventType.AGENT_END:
            return
        agent_type_str = EventTranslator._extract_agent_type(stream_ev.agent_name)
        task_desc = ""
        for t in plan.tasks:
            if t.task_id == stream_ev.task_id:
                task_desc = t.description
                break
        turn_result.agent_records.append(AgentExecutionRecord(
            session_id=session_ctx.session_id,
            chat_id=session_ctx.chat_id,
            task_id=stream_ev.task_id,
            agent_type=agent_type_str,
            agent_name=stream_ev.agent_name,
            task_description=task_desc,
            status=stream_ev.data.get("status", "success"),
            result_preview=stream_ev.content,
            duration_ms=stream_ev.data.get("duration_ms"),
            finished_at=datetime.now().isoformat(),
        ))

    async def _detect_failure_and_emit_tables(
        self,
        stream_ev: StreamEvent,
        task_contexts: dict[str, TaskContext],
        emitted_table_keys: set[str],
        translator: EventTranslator,
        session_ctx: SessionContext,
    ) -> AsyncGenerator[StreamEvent, None]:
        """AGENT_END 后：失败检测 + TABLE 事件即时推送。"""
        # 集中式失败检测
        if looks_like_failure(stream_ev.content):
            session_ctx._failed_task_ids.add(stream_ev.task_id)
            logger.warning("[DAG] 任务失败，标记以跳过下游: %s", stream_ev.task_id)

        # 每个 Agent 完成后，立即推送其写入 DataContext 的 TABLE
        task_id = stream_ev.task_id
        if task_id and task_id in task_contexts:
            tc = task_contexts[task_id]
            if isinstance(tc, TaskContext) and tc.data_context is not None:
                for key in tc.data_context.list_keys():
                    if key not in emitted_table_keys:
                        emitted_table_keys.add(key)
                        df = tc.data_context.get(key)
                        if df is not None and not df.empty:
                            table_ev = emit_table_event_for_key(translator, key, df)
                            if table_ev is not None:
                                yield table_ev

    async def _post_dag_cleanup(
        self,
        task_contexts: dict[str, TaskContext],
        session_ctx: SessionContext,
        final_result: str,
        close_events: list[StreamEvent],
    ) -> tuple[str | None, DataContext | None]:
        """DAG 执行后收尾：摘要提取 + 结果合并 + 缓存保存 + TaskContext 销毁。

        Returns:
            (bg_summary, shared_dc) — 后台摘要数据和共享 DataContext。
        """
        # 提取 shared_dc 用于 summarize（必须在 destroy 前提取）
        shared_dc = None
        for key, ctx in task_contexts.items():
            if key == "__session__":
                continue
            if isinstance(ctx, TaskContext) and ctx.data_context is not None:
                shared_dc = ctx.data_context
                break

        # 如果 DataContext 有数据且最终结果不是自然语言回答，后台异步生成总结
        bg_summary: str | None = None
        if shared_dc is not None and shared_dc.list_keys() and _needs_summarization(final_result):
            bg_summary = shared_dc.all_summaries()

        # 合并结果到 SessionContext
        shared_dc = None
        for key, ctx in task_contexts.items():
            if key == "__session__":
                continue
            if isinstance(ctx, TaskContext):
                ctx.set_final_answer(final_result)
                await ctx.merge_to_session()
                if ctx.data_context is not None:
                    shared_dc = ctx.data_context

        # 保存 DataContext 到缓存（必须在 destroy 之前！）
        if shared_dc is not None and shared_dc.list_keys():
            await self._data_context_cache.save(session_ctx.session_id, shared_dc)

        # 销毁 TaskContext（会清空 DataContext）
        for key, ctx in task_contexts.items():
            if key == "__session__":
                continue
            if isinstance(ctx, TaskContext):
                await ctx.destroy()

        # 后台摘要数据过滤
        if bg_summary and bg_summary == "DataContext 当前为空，没有可用数据。":
            bg_summary = None

        return bg_summary, shared_dc

    # ------------------------------------------------------------------
    # 规划
    # ------------------------------------------------------------------

    async def _plan(
        self,
        task: str,
        session_ctx: SessionContext | None = None,
        translator: Any | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """通过 PlanAgent 实例获取DAG规划（async generator，yield StreamEvent）。

        PlanAgent 内置 ask_user + dag 两个 FunctionTool：
        - ask_user: on_messages_stream 拦截 tool call，yield USER_QUESTION/USER_ANSWER
        - dag: LLM 调用 dag 工具提交任务列表，工具构建 DAGPlan 并存入 _dag_plan

        优先使用 _dag_plan（dag 工具结果），fallback 到解析 _final_content 文本。
        解析后的 DAGPlan 存入 self._last_plan。
        """
        from autogen_agentchat.messages import TextMessage as _TextMessage

        try:
            plan_agent = PlanAgent(
                model_client=self._model_client,
                session=session_ctx,
                translator=translator,
            )

            # 当前用户消息（历史消息由 PlanAgent.on_before_turn 注入）
            plan_messages: list[_TextMessage] = [_TextMessage(content=task, source="user")]

            async for event in plan_agent.on_messages_stream(
                plan_messages,
                cancellation_token=None,
            ):
                if isinstance(event, StreamEvent):
                    yield event

            # dag 工具结果优先
            dag_plan = plan_agent._dag_plan
            if dag_plan is not None:
                dag_plan.validate_dag()
                self._last_plan = dag_plan
                return

            # Fallback: 解析 _final_content 文本为 DAGPlan
            content = plan_agent._final_content
            if not content:
                logger.warning("PlanAgent未输出内容，降级为单任务")
                self._last_plan = DAGPlan(
                    reasoning="PlanAgent未输出内容，降级为单任务",
                    tasks=[TaskNode(task_id="1", agent="RAGAgent", description=task)],
                )
                return

            try:
                plan_data = json.loads(content)
                self._last_plan = self._parse_plan_data(plan_data, task)
                return
            except (json.JSONDecodeError, TypeError):
                json_match = re.search(r"```json\s*(\{.*?\}|\[.*?\])\s*```", content, re.DOTALL)
                if json_match:
                    try:
                        plan_data = json.loads(json_match.group(1))
                        self._last_plan = self._parse_plan_data(plan_data, task)
                        return
                    except (json.JSONDecodeError, TypeError):
                        pass

                logger.warning("PlanAgent输出无法解析为DAGPlan，内容: %s", content[:200])
                self._last_plan = DAGPlan(
                    reasoning="PlanAgent输出解析失败，降级为单任务",
                    tasks=[TaskNode(task_id="1", agent="RAGAgent", description=task)],
                )
        except Exception as e:
            logger.error("PlanAgent执行失败: %s", e, exc_info=True)
            self._last_plan = DAGPlan(
                reasoning=f"PlanAgent执行失败: {e}",
                tasks=[TaskNode(task_id="1", agent="RAGAgent", description=task)],
            )

    @staticmethod
    def _parse_plan_data(plan_data: Any, task: str) -> DAGPlan:
        """解析LLM输出的规划数据，兼容数组和对象两种格式。"""
        if isinstance(plan_data, list):
            plan = DAGPlan.from_task_list(plan_data)
        elif isinstance(plan_data, dict):
            if "tasks" in plan_data:
                plan = DAGPlan.model_validate(plan_data)
            else:
                plan = DAGPlan.from_task_list([plan_data])
        else:
            logger.warning("PlanAgent输出格式不支持: %s", type(plan_data))
            plan = DAGPlan(
                reasoning="PlanAgent输出格式不支持，降级为单任务",
                tasks=[TaskNode(task_id="task_1", agent="DataAnalysisAgent", description=task)],
            )

        plan.validate_dag()
        return plan

    # ------------------------------------------------------------------
    # DAG 构建
    # ------------------------------------------------------------------

    def _build_dag(
        self,
        plan: DAGPlan,
        task_contexts: dict[str, TaskContext],
        shared_data_context: DataContext | None = None,
    ) -> tuple[GraphFlow, dict[str, ChatAgent]]:
        """从DAGPlan构建GraphFlow团队。使用 AgentFactory.create_from_task_node。"""
        builder = DiGraphBuilder()
        agents: dict[str, ChatAgent] = {}

        shared_data_context = shared_data_context or DataContext()

        for task_node in plan.tasks:
            task_ctx = TaskContext(task_node.task_id, task_contexts.get("__session__"), data_context=shared_data_context)  # type: ignore[arg-type]
            agent = AgentFactory.create_from_task_node(task_node, self._model_client, task_ctx)
            builder.add_node(agent)
            agents[task_node.task_id] = agent
            task_contexts[task_node.task_id] = task_ctx

        for task_node in plan.tasks:
            for dep_id in task_node.depends_on:
                if dep_id not in agents:
                    raise ValueError(f"任务 {task_node.task_id} 依赖的 {dep_id} 不存在")
                builder.add_edge(agents[dep_id], agents[task_node.task_id])

        graph = builder.build()

        termination = TextMentionTermination("TERMINATE") | MaxMessageTermination(settings.max_team_turns)
        team = GraphFlow(
            participants=builder.get_participants(),
            graph=graph,
            termination_condition=termination,
            max_turns=settings.max_team_turns,
            custom_message_types=[StructuredMessage[PlanStep]],
        )

        return team, agents