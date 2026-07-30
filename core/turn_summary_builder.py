
"""TurnSummary 组装 — 从 StreamEvent 列表和 SessionContext 提取结构化对话摘要。

纯数据变换函数，无 I/O、无 ContextVar、无 agents 依赖。
agent_types 由调用方注入，消除 core→agents 反向依赖。
"""

from __future__ import annotations

from core.context import SessionContext
from models.conversation import (
    AgentConclusion,
    DataRef,
    RoutingSnapshot,
    TaskStatus,
    TurnSummary,
)
from models.routing import RoutingResult
from models.stream_event import StreamEvent, StreamEventType


def extract_final_result(collected_events: list[StreamEvent]) -> str:
    """从收集的事件中提取最终结果文本。

    优先返回最后一个成功的 AGENT_END 内容，否则最后一个 ERROR 内容。
    """
    for ev in reversed(collected_events):
        if ev.type == StreamEventType.AGENT_END and ev.data.get("status") == "success":
            return ev.content
    for ev in reversed(collected_events):
        if ev.type == StreamEventType.ERROR:
            return ev.content
    return "未能生成结果。"


def extract_agent_conclusions(collected_events: list[StreamEvent]) -> list[AgentConclusion]:
    """从 AGENT_END / ERROR 事件提取结论列表。"""
    conclusions: list[AgentConclusion] = []
    for ev in collected_events:
        if ev.type == StreamEventType.AGENT_END:
            status = TaskStatus.SUCCESS if ev.data.get("status") == "success" else TaskStatus.FAILED
            conclusions.append(AgentConclusion(
                agent_name=ev.agent_name or "",
                status=status,
                conclusion=ev.content[:500] if ev.content else "",
            ))
        elif ev.type == StreamEventType.ERROR:
            conclusions.append(AgentConclusion(
                agent_name=ev.data.get("error_type", "Unknown"),
                status=TaskStatus.FAILED,
                conclusion=ev.content[:500] if ev.content else "",
                failure_reason=ev.data.get("message", ""),
            ))
    return conclusions


def extract_dag_tasks(collected_events: list[StreamEvent]) -> list[str] | None:
    """从 PLAN_COMPLETE 事件提取 DAG 任务 ID 列表。"""
    for ev in collected_events:
        if ev.type == StreamEventType.PLAN_COMPLETE and ev.data.get("tasks"):
            return [t.get("task_id", "") for t in ev.data["tasks"] if isinstance(t, dict)]
    return None


def extract_data_refs(
    session_ctx: SessionContext,
    agent_types: frozenset[str],
) -> list[DataRef]:
    """从 SessionContext 的历史结果中提取 DataRef 列表。

    优先使用 TaskResult.data_snapshot（destroy 前保存的结构化快照），
    回退到 data_keys（仅 key 列表，无 schema/rows 信息）。

    Args:
        session_ctx: 会话上下文。
        agent_types: 合法 Agent 类型名集合，用于从 data_key 反查 agent。
    """
    refs: list[DataRef] = []
    for task_id, result in session_ctx._results.items():
        if not result.data_keys:
            continue
        for key in result.data_keys:
            agent_name = ""
            for at in agent_types:
                if key.startswith(at):
                    agent_name = at
                    break
            if not agent_name:
                agent_name = task_id

            snap = result.data_snapshot.get(key)
            if snap is not None:
                refs.append(DataRef(
                    key=key, agent=agent_name, status=TaskStatus.SUCCESS,
                    schema=snap.get("schema", {}), rows=snap.get("rows", 0),
                    summary=snap.get("summary", ""), source=snap.get("meta", {}),
                ))
            else:
                refs.append(DataRef(
                    key=key, agent=agent_name, status=TaskStatus.SUCCESS,
                    summary=result.answer[:200] if result.answer else "",
                ))
    return refs


def assemble_turn_summary(
    *,
    session_id: str,
    query: str,
    routing: RoutingResult,
    collected_events: list[StreamEvent],
    session_ctx: SessionContext,
    turn_id: int,
    agent_types: frozenset[str],
) -> TurnSummary:
    """从收集的事件和 SessionContext 组装 TurnSummary。

    Args:
        session_id: 会话 ID。
        query: 用户原始问题。
        routing: 路由结果。
        collected_events: 本轮收集的 StreamEvent 列表。
        session_ctx: 会话上下文。
        turn_id: 本轮序号（由调用方计算）。
        agent_types: 合法 Agent 类型名集合。
    """
    agent_conclusions = extract_agent_conclusions(collected_events)
    dag_tasks = extract_dag_tasks(collected_events)
    data_refs = extract_data_refs(session_ctx, agent_types)

    return TurnSummary(
        turn_id=turn_id,
        query=query,
        routing=RoutingSnapshot(
            mode=routing.mode,
            agent_type=routing.agent_type,
            task_description=routing.task_description,
        ),
        agent_conclusions=agent_conclusions,
        data_produced=data_refs,
        dag_tasks=dag_tasks,
    )