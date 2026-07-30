
"""单次请求的显式结果契约。

替代此前通过 StreamEvent.data["_db_record"] / ["_db_plan_record"] / ["_bg_summary"]
在 AgentLayer 和 SessionManager 之间传递数据的隐式 sideband 模式。

AgentLayer 产出 TurnResult，SessionManager 消费 — 类型安全，schema 集中。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from models.stream_event import StreamEvent


@dataclass
class AgentExecutionRecord:
    """单个 Agent 的执行记录，供 SessionManager 写入 agent_execution 表。

    字段与 db.writer.make_agent_execution_data 的关键字参数对齐。
    """

    session_id: str
    chat_id: str
    task_id: str
    agent_type: str
    agent_name: str
    task_description: str
    status: str  # "success" / "error"
    result_preview: str = ""
    error_type: str = ""
    error_message: str = ""
    duration_ms: int | None = None
    finished_at: str = ""

    def to_db_kwargs(self) -> dict[str, object]:
        """转为 make_agent_execution_data 的关键字参数。"""
        kwargs: dict[str, object] = {
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "task_id": self.task_id,
            "agent_type": self.agent_type,
            "agent_name": self.agent_name,
            "task_description": self.task_description,
            "status": self.status,
            "result_preview": self.result_preview,
            "duration_ms": self.duration_ms or 0,
            "finished_at": self.finished_at or None,
        }
        if self.status == "error":
            kwargs["error_type"] = self.error_type
            kwargs["error_message"] = self.error_message
        return kwargs


@dataclass
class PlanRecord:
    """DAG 规划记录，供 SessionManager 写入 dag_plan 表。

    字段与 db.writer.make_plan_data 的关键字参数对齐。
    """

    session_id: str
    chat_id: str
    task_count: int
    tasks_json: list[dict[str, object]]
    reasoning: str
    is_complete: bool
    duration_ms: int

    def to_db_kwargs(self) -> dict[str, object]:
        """转为 make_plan_data 的关键字参数。"""
        return {
            "session_id": self.session_id,
            "chat_id": self.chat_id,
            "task_count": self.task_count,
            "tasks_json": self.tasks_json,
            "reasoning": self.reasoning,
            "is_complete": self.is_complete,
            "duration_ms": self.duration_ms,
        }


@dataclass
class TurnResult:
    """单次请求的完整结果，AgentLayer 产出，SessionManager 消费。

    取代此前通过 StreamEvent.data 传递的三个 sideband key：
    - _db_record     → agent_records: list[AgentExecutionRecord]
    - _db_plan_record → plan_record: PlanRecord | None
    - _bg_summary    → bg_summary: str | None
    """

    events: list[StreamEvent] = field(default_factory=list)
    agent_records: list[AgentExecutionRecord] = field(default_factory=list)
    plan_record: PlanRecord | None = None
    bg_summary: str | None = None