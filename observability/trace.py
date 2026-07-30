
from __future__ import annotations

import json
import logging
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    SelectSpeakerEvent,
    StructuredMessage,
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
)

logger = logging.getLogger("bi_autogen.trace")

# ContextVar: 让 Agent 的 on_after_turn 钩子可以访问当前的 TraceRecorder
_trace_recorder_var: ContextVar[TraceRecorder | None] = ContextVar("trace_recorder", default=None)


def set_trace_recorder(recorder: TraceRecorder | None) -> None:
    """设置当前任务运行的 TraceRecorder 到 ContextVar。"""
    _trace_recorder_var.set(recorder)


def get_trace_recorder() -> TraceRecorder | None:
    """获取当前任务运行的 TraceRecorder，供 Agent 钩子使用。"""
    return _trace_recorder_var.get()


def _serialize_message(msg: BaseAgentEvent | BaseChatMessage) -> dict[str, Any]:
    """将AutoGen消息转为可序列化的dict。"""
    record: dict[str, Any] = {
        "id": msg.id,
        "source": msg.source,
        "type": type(msg).__name__,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
    }

    if msg.models_usage:
        record["usage"] = {
            "prompt_tokens": msg.models_usage.prompt_tokens,
            "completion_tokens": msg.models_usage.completion_tokens,
        }

    if isinstance(msg, TextMessage):
        record["content"] = msg.content

    elif isinstance(msg, StructuredMessage):
        content = msg.content
        if hasattr(content, "model_dump"):
            record["content"] = content.model_dump()
        else:
            record["content"] = str(content)

    elif isinstance(msg, ToolCallRequestEvent):
        record["tool_calls"] = [
            {"id": tc.id, "name": tc.name, "arguments": tc.arguments}
            for tc in msg.content
        ]

    elif isinstance(msg, ToolCallExecutionEvent):
        record["tool_results"] = [
            {
                "call_id": r.call_id,
                "name": getattr(r, "name", ""),
                "content": r.content[:2000] if len(r.content) > 2000 else r.content,
                "is_error": r.is_error if hasattr(r, "is_error") else False,
            }
            for r in msg.content
        ]

    elif isinstance(msg, SelectSpeakerEvent):
        record["selected"] = msg.content

    elif hasattr(msg, "content"):
        content = msg.content
        if isinstance(content, str):
            record["content"] = content[:2000]
        else:
            record["content"] = str(content)[:2000]

    return record


class TraceRecorder:
    """记录一次任务执行中所有agent的交互trace，每步实时写入文件。

    不记录流式 token chunk，只记录路由、规划、工具调用等关键事件。
    """

    def __init__(
        self,
        task: str,
        trace_dir: str | Path | None = None,
        *,
        session_id: str = "",
        chat_id: str = "",
    ) -> None:
        self.task = task
        self.session_id = session_id
        self.chat_id = chat_id
        self.trace_dir = Path(trace_dir) if trace_dir else Path(__file__).parent.parent / "log" / "trace"
        self.trace_dir.mkdir(parents=True, exist_ok=True)

        self.started_at = datetime.now()
        self.finished_at: datetime | None = None
        self.step_count: int = 0
        self.stop_reason: str | None = None

        # 打开文件，准备实时追加写入
        # 使用 session_id 前缀避免同一天不同 session 互相覆盖
        sid = self.session_id[:8] if self.session_id else "ns"
        self._filename = f"{sid}_{self.started_at.strftime('%Y%m%d_%H%M%S')}.jsonl"
        self._filepath = self.trace_dir / self._filename
        self._file = open(self._filepath, "a", encoding="utf-8")  # noqa: SIM115

        # 写入头部
        header: dict[str, Any] = {
            "type": "header",
            "task": self.task,
            "started_at": self.started_at.isoformat(),
        }
        if self.session_id:
            header["session_id"] = self.session_id
        if self.chat_id:
            header["chat_id"] = self.chat_id
        self._write_line(header)
        logger.info("[Trace] 任务开始: '%s', trace文件: %s", task[:50], self._filepath)

    def _write_line(self, data: dict[str, Any]) -> None:
        """实时写入一行JSON到trace文件。"""
        # 每条记录都带 session_id 和 chat_id
        if self.session_id and "session_id" not in data:
            data["session_id"] = self.session_id
        if self.chat_id and "chat_id" not in data:
            data["chat_id"] = self.chat_id
        self._file.write(json.dumps(data, ensure_ascii=False) + "\n")
        self._file.flush()

    def record_routing(
        self,
        layer: int,
        mode: str,
        agent_type: str,
        reasoning: str,
        *,
        api_meta_count: int = 0,
        skills_count: int = 0,
        duration_ms: int = 0,
    ) -> None:
        """记录路由决策。"""
        self.step_count += 1
        record: dict[str, Any] = {
            "type": "routing",
            "step": self.step_count,
            "source": "Router",
            "layer": layer,
            "mode": mode,
            "agent_type": agent_type,
            "reasoning": reasoning[:500],
            "api_meta_count": api_meta_count,
            "skills_count": skills_count,
            "duration_ms": duration_ms,
            "created_at": datetime.now().isoformat(),
        }
        self._write_line(record)
        logger.info(
            "[Trace] #%d Router → Layer%d %s %s: %s",
            self.step_count, layer, mode, agent_type, reasoning[:100],
        )

    def record_plan_output(self, plan: Any) -> None:
        """记录 PlanAgent 的 DAG 规划输出。"""
        if hasattr(plan, "model_dump"):
            plan_data = plan.model_dump()
        elif isinstance(plan, dict):
            plan_data = plan
        else:
            plan_data = str(plan)
        self.step_count += 1
        record = {
            "type": "plan_output",
            "step": self.step_count,
            "source": "PlanAgent",
            "plan": plan_data,
            "created_at": datetime.now().isoformat(),
        }
        self._write_line(record)
        logger.info("[Trace] #%d PlanAgent → DAG规划: %d个任务", self.step_count, len(plan_data.get("tasks", [])) if isinstance(plan_data, dict) else 0)

    def record_tool_call(
        self,
        agent_name: str,
        tool_name: str,
        arguments: dict[str, Any] | str,
        result: str,
    ) -> None:
        """记录工具调用的入参和执行结果。"""
        self.step_count += 1
        record = {
            "type": "tool_call",
            "step": self.step_count,
            "source": agent_name,
            "tool_name": tool_name,
            "arguments": arguments if isinstance(arguments, dict) else {"raw": arguments},
            "result": result[:3000] if len(result) > 3000 else result,
            "created_at": datetime.now().isoformat(),
        }
        self._write_line(record)
        args_preview = str(arguments)[:200]
        logger.info("[Trace] #%d %s → 工具调用 %s(%s) 结果: %s", self.step_count, agent_name, tool_name, args_preview, result[:200])

    def close(self) -> None:
        """关闭文件句柄（不写 footer），用于异常路径的安全清理。"""
        if self._file and not self._file.closed:
            self._file.close()

    def finish(self) -> Path:
        """结束记录，写入尾部并关闭文件。"""
        self.finished_at = datetime.now()
        duration = (self.finished_at - self.started_at).total_seconds()

        footer: dict[str, Any] = {
            "type": "footer",
            "finished_at": self.finished_at.isoformat(),
            "duration_seconds": duration,
            "total_steps": self.step_count,
            "stop_reason": self.stop_reason,
        }
        self._write_line(footer)
        self._file.close()

        logger.info("[Trace] 任务完成: %d步, %.1fs, trace文件: %s", self.step_count, duration, self._filepath)
        return self._filepath

    def to_text_summary(self) -> str:
        """生成人类可读的trace摘要。"""
        lines = [f"任务: {self.task}", f"开始: {self.started_at.strftime('%Y-%m-%d %H:%M:%S')}"]
        if self.session_id:
            lines.append(f"会话: {self.session_id}")
        if self.finished_at:
            lines.append(f"结束: {self.finished_at.strftime('%Y-%m-%d %H:%M:%S')}")
            lines.append(f"耗时: {(self.finished_at - self.started_at).total_seconds():.1f}s")

        lines.append(f"总步数: {self.step_count}")
        lines.append(f"trace文件: {self._filepath}")
        return "\n".join(lines)