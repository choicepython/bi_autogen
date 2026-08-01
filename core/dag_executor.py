
"""DAG 执行器：全团队 DAG 流水线执行 + 事件推送 + 收尾。

从 agent_layer.py 提取，职责单一：接收 DAGPlan → 执行 GraphFlow → yield StreamEvent → 收尾清理。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncGenerator
from datetime import datetime

from autogen_agentchat.messages import BaseAgentEvent, BaseChatMessage
from autogen_core.models import ChatCompletionClient

from agents.base import looks_like_failure
from config import settings
from core.context import SessionContext, TaskContext
from core.data_context import DataContext
from core.data_context_cache import DataContextCache
from core.event_translator import EventTranslator, _needs_summarization
from core.table_formatter import emit_table_event_for_key
from models import DAGPlan
from models.routing import RoutingResult
from models.stream_event import StreamEvent, StreamEventType
from models.turn_result import AgentExecutionRecord, TurnResult
from observability.observer_factory import get_trace_observer

logger = logging.getLogger(__name__)


async def run_team(
    query: str,
    routing: RoutingResult,
    session_ctx: SessionContext,
    seq: list[int],
    session_start_time: datetime,
    turn_result: TurnResult,
    model_client: ChatCompletionClient,
    data_context_cache: DataContextCache,
) -> AsyncGenerator[StreamEvent, None]:
    """全团队 DAG 执行入口。"""
    from core import plan_executor

    translator = EventTranslator(session_ctx.session_id, seq)

    # Phase 1: 规划（plan_holder 接收 DAGPlan）
    plan_holder: list[DAGPlan | None] = [None]
    async for ev in plan_executor.run_plan_phase(
        query, session_ctx, translator, model_client, turn_result, plan_holder,
    ):
        yield ev
    plan = plan_holder[0]
    if plan is None or not plan.tasks:
        # PlanAgent 已在规划阶段给出完整回答（PLAN_COMPLETE 已包含 reasoning）
        return

    # Phase 2: DAG 构建
    shared_dc = DataContext()
    await data_context_cache.restore(session_ctx.session_id, shared_dc)
    task_contexts: dict[str, TaskContext] = {"__session__": session_ctx}  # type: ignore[dict-item]
    try:
        team, _agents = plan_executor.build_dag(plan, task_contexts, model_client, shared_dc)
        session_ctx._failed_task_ids = set()
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
    emitted_report_keys: set[str] = set()
    try:
        async with asyncio.timeout(settings.dag_execution_timeout):
            async for event in team.run_stream(task=query):
                if isinstance(event, (BaseAgentEvent, BaseChatMessage)):
                    for stream_ev in translator.translate(event, plan=plan):
                        _append_dag_agent_record(stream_ev, plan, session_ctx, turn_result)
                        yield stream_ev
                        if stream_ev.type == StreamEventType.AGENT_END:
                            final_result = stream_ev.content
                            async for table_ev in _detect_failure_and_emit_tables(
                                stream_ev, task_contexts, emitted_table_keys, translator, session_ctx,
                            ):
                                yield table_ev
                            async for file_ev in _emit_task_reports(
                                stream_ev, task_contexts, emitted_report_keys, translator,
                            ):
                                yield file_ev
                elif hasattr(event, "messages") and hasattr(event, "stop_reason") and event.stop_reason:
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

    bg_summary, _ = await _post_dag_cleanup(
        task_contexts, session_ctx, final_result, close_events, data_context_cache,
    )
    if bg_summary:
        turn_result.bg_summary = bg_summary


def _append_dag_agent_record(
    stream_ev: StreamEvent,
    plan: DAGPlan,
    session_ctx: SessionContext,
    turn_result: TurnResult,
) -> None:
    """为 DAG 路径的 AGENT_END 事件追加 Agent 执行记录到 TurnResult + Langfuse span。"""
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
    # Langfuse DAG agent span（retrospective — agent 已执行完毕，记录元数据）
    with get_trace_observer().start_span(
        f"agent:{stream_ev.agent_name}",
        metadata={
            "agent_type": agent_type_str,
            "task_id": stream_ev.task_id,
            "task_description": task_desc[:500],
            "status": stream_ev.data.get("status", "success"),
            "duration_ms": stream_ev.data.get("duration_ms"),
        },
    ):
        pass


async def _detect_failure_and_emit_tables(
    stream_ev: StreamEvent,
    task_contexts: dict[str, TaskContext],
    emitted_table_keys: set[str],
    translator: EventTranslator,
    session_ctx: SessionContext,
) -> AsyncGenerator[StreamEvent, None]:
    """AGENT_END 后：失败检测 + TABLE 事件即时推送。"""
    if looks_like_failure(stream_ev.content):
        session_ctx._failed_task_ids.add(stream_ev.task_id)
        logger.warning("[DAG] 任务失败，标记以跳过下游: %s", stream_ev.task_id)

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


async def _emit_task_reports(
    stream_ev: StreamEvent,
    task_contexts: dict[str, TaskContext],
    emitted_report_keys: set[str],
    translator: EventTranslator,
) -> AsyncGenerator[StreamEvent, None]:
    """AGENT_END 后：推送该任务 DataContext 中注册的报告文件（FILE 事件）。"""
    task_id = stream_ev.task_id
    if not task_id or task_id not in task_contexts:
        return
    tc = task_contexts[task_id]
    if not isinstance(tc, TaskContext) or tc.data_context is None:
        return
    for report in tc.data_context.get_reports():
        filename = report.get("filename", "")
        if not filename or filename in emitted_report_keys:
            continue
        emitted_report_keys.add(filename)
        yield translator.make_file_event(
            filename=filename,
            format=report.get("format", ""),
            url=f"/api/v1/reports/{filename}",
            title=report.get("title", ""),
        )


async def _post_dag_cleanup(
    task_contexts: dict[str, TaskContext],
    session_ctx: SessionContext,
    final_result: str,
    close_events: list[StreamEvent],
    data_context_cache: DataContextCache,
) -> tuple[str | None, DataContext | None]:
    """DAG 执行后收尾：摘要提取 + 结果合并 + 缓存保存 + TaskContext 销毁。"""
    shared_dc = None
    for key, ctx in task_contexts.items():
        if key == "__session__":
            continue
        if isinstance(ctx, TaskContext) and ctx.data_context is not None:
            shared_dc = ctx.data_context
            break

    bg_summary: str | None = None
    if shared_dc is not None and shared_dc.list_keys() and _needs_summarization(final_result):
        bg_summary = shared_dc.all_summaries()

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
        await data_context_cache.save(session_ctx.session_id, shared_dc)

    # 销毁 TaskContext（会清空 DataContext）
    for key, ctx in task_contexts.items():
        if key == "__session__":
            continue
        if isinstance(ctx, TaskContext):
            await ctx.destroy()

    if bg_summary and bg_summary == "DataContext 当前为空，没有可用数据。":
        bg_summary = None

    return bg_summary, shared_dc