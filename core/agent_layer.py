
"""智能体层：单Agent执行 + DAG团队执行入口。

职责：
- 单Agent快速路径（run_single_agent）
- 全团队 DAG 执行委托（run_team → dag_executor）
- 结果汇总（DataContext → TABLE事件 + FILE事件）

不负责：
- 会话生命周期管理（DispatchLayer / SessionManager）
- 路由决策（RoutingLayer）
- Agent 创建（AgentFactory，独立模块）
- 事件翻译（EventTranslator，独立模块）
- 规划逻辑（plan_executor，独立模块）
- DAG 执行细节（dag_executor，独立模块）
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import datetime

from autogen_agentchat.messages import TextMessage
from autogen_core import CancellationToken
from autogen_core.models import ChatCompletionClient

from config import settings
from core.agent_factory import AgentFactory
from core.context import SessionContext, TaskContext
from core.data_context import DataContext
from core.data_context_cache import DataContextCache, InMemoryDataContextCache
from core.event_translator import EventTranslator, _needs_summarization
from core.table_formatter import emit_table_event_for_key
from models.routing import RoutingResult
from models.stream_event import StreamEvent
from models.turn_result import AgentExecutionRecord, TurnResult
from observability.observer_factory import get_trace_observer

logger = logging.getLogger(__name__)


class AgentLayer:
    """智能体层：单Agent执行 + DAG团队执行入口。

    规划委托 plan_executor，DAG 执行委托 dag_executor。
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
        # 历史恢复的 key 不重复下发 TABLE，避免上一轮表格在本轮被重放
        restored_keys = set(dc.list_keys())

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

        # Langfuse agent span
        observer = get_trace_observer()
        agent_span_cm = observer.start_span(
            f"agent:{agent.name}",
            metadata={"agent_type": agent_type_str, "task_description": routing.task_description[:500]},
        )
        agent_span = agent_span_cm.__enter__()

        try:
            agent_start = datetime.now()
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

            # 收尾：duration + content + TABLE + 记录 + 摘要
            duration_ms = int((datetime.now() - agent_start).total_seconds() * 1000)
            content = translator.last_content or final_result
            final_result = content

            # Langfuse agent span 更新成功状态
            if agent_span is not None:
                observer.update_span(agent_span, metadata={"status": "success", "duration_ms": duration_ms})

            # TABLE 事件（仅推送本轮新增的 key，不重放历史恢复的数据）
            for key in dc.list_keys():
                if key not in restored_keys:
                    df = dc.get(key)
                    if df is not None and not df.empty:
                        table_ev = emit_table_event_for_key(translator, key, df)
                        if table_ev is not None:
                            yield table_ev

            # 后台摘要数据（由 SessionManager.finish() 触发）
            bg_summary: str | None = None
            if dc.list_keys() and _needs_summarization(content):
                data_summary = dc.all_summaries()
                if data_summary and data_summary != "DataContext 当前为空，没有可用数据。":
                    bg_summary = data_summary

            # Agent 执行记录 → 写入 TurnResult
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

            yield translator.make_agent_end(
                agent_name=agent.name,
                task_id="direct",
                content=content,
                duration_ms=duration_ms,
            )

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
            # Langfuse agent span 更新失败状态
            observer.update_span(agent_span, metadata={
                "status": "error",
                "error_type": type(e).__name__,
                "error_message": str(e)[:500],
            })
            yield error_ev
        finally:
            agent_span_cm.__exit__(None, None, None)

    # ------------------------------------------------------------------
    # 全团队 DAG 执行（委托 dag_executor）
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
        """全团队路径：委托 dag_executor 执行 DAG 流水线。"""
        from core import dag_executor

        async for ev in dag_executor.run_team(
            query, routing, session_ctx, seq, session_start_time, turn_result,
            self._model_client, self._data_context_cache,
        ):
            yield ev