
"""统一事件翻译器：AutoGen 事件 → StreamEvent。

独立于 AgentLayer，供 DispatchLayer 和 AgentLayer 共用。
将 AutoGen 的内部事件类型翻译为前端可消费的 13 种 StreamEvent。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from autogen_agentchat.base import Response
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    ModelClientStreamingChunkEvent,
    StructuredMessage,
    TextMessage,
    ThoughtEvent,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
)
from config import settings
from models import DAGPlan
from models.stream_event import StreamEvent, StreamEventType

logger = logging.getLogger(__name__)

# think 标签的 Unicode 转义常量（避免在源码中直接写入 HTML 标签）
THINK_OPEN = "\u003cthink\u003e"
THINK_CLOSE = "\u003c/think\u003e"


class EventTranslator:
    """统一 AutoGen 事件 → StreamEvent 翻译。

    替代 _run_single_agent 和 run_stream 中的重复翻译逻辑。
    单一 source of truth，两种执行路径共用。
    """

    def __init__(self, session_id: str, seq: list[int]) -> None:
        self._session_id = session_id
        self._seq = seq
        self._current_agent = ""
        self._agent_start_time: datetime | None = None
        self.last_content: str = ""
        self._is_reasoning = False
        # 多 Agent 并行跟踪（替代单值 _current_agent/_is_reasoning）
        self._active_agents: set[str] = set()
        self._agent_start_times: dict[str, datetime] = {}
        self._reasoning_state: dict[str, bool] = {}

    def _next_seq(self) -> int:
        self._seq[0] += 1
        return self._seq[0]

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _extract_task_id(agent_name: str) -> str:
        """从 'AgentType_task_1' 格式提取 task_id。"""
        # lazy import: 避免 core → agents 循环依赖
        from agents.base import AGENT_TYPES
        for at in AGENT_TYPES:
            if agent_name.startswith(at + "_"):
                return agent_name[len(at) + 1:]
        return ""

    @staticmethod
    def _extract_agent_type(agent_name: str) -> str:
        """从 'AgentType_task_1' 格式提取 agent 类型。"""
        # lazy import: 避免 core → agents 循环依赖
        from agents.base import AGENT_TYPES
        for at in AGENT_TYPES:
            if agent_name.startswith(at + "_") or agent_name == at:
                return at
        return agent_name

    def _make_event(
        self,
        event_type: StreamEventType,
        content: str,
        data: dict[str, Any],
        source: str | None = None,
    ) -> StreamEvent:
        """构造 StreamEvent 快捷方法，自动填充公共字段。

        source 参数用于并行 Agent 场景，将事件归属到正确的 Agent。
        """
        agent = source or self._current_agent
        return StreamEvent(
            type=event_type,
            seq=self._next_seq(),
            timestamp=self._now(),
            session_id=self._session_id,
            task_id=self._extract_task_id(agent),
            agent_name=agent,
            content=content,
            data=data,
        )

    def _handle_agent_switch(self, source: str, plan: DAGPlan | None, results: list[StreamEvent]) -> None:
        """处理 Agent 切换：开启新 Agent（不关闭前一个，支持并行）。

        并行 Agent 事件交替到达时，多个 Agent 可同时活跃。
        前一个 Agent 的 AGENT_END 由 _translate_text_message/structured_message 关闭。
        """
        self._current_agent = source
        if source not in self._active_agents:
            self._active_agents.add(source)
            self._agent_start_times[source] = datetime.now()
            agent_type = self._extract_agent_type(source)
            task_id = self._extract_task_id(source)
            task_desc = ""
            if plan:
                for t in plan.tasks:
                    if t.task_id == task_id:
                        task_desc = t.description
                        break
            results.append(self._make_event(
                StreamEventType.AGENT_START,
                f"{agent_type} 开始执行",
                {"agent_type": agent_type, "description": task_desc},
                source=source,
            ))

    def _translate_streaming_chunk(self, event: ModelClientStreamingChunkEvent) -> list[StreamEvent]:
        """翻译 LLM 流式 chunk 事件。per-agent 思考状态，支持并行 Agent。"""
        source = getattr(event, "source", "") or self._current_agent
        chunk = event.content
        if THINK_OPEN in chunk:
            self._reasoning_state[source] = True
            chunk = chunk.replace(THINK_OPEN, "")
        if THINK_CLOSE in chunk:
            self._reasoning_state[source] = False
            chunk = chunk.replace(THINK_CLOSE, "")
        is_reasoning = self._reasoning_state.get(source, False)
        event_type = StreamEventType.THINK_CHUNK if is_reasoning else StreamEventType.LLM_CHUNK
        if not settings.enable_thinking_output and is_reasoning:
            chunk = ""
        if chunk:
            return [self._make_event(event_type, chunk, {"chunk": chunk}, source=source)]
        return []

    def _translate_tool_call(self, event: ToolCallRequestEvent) -> list[StreamEvent]:
        """翻译工具调用事件。用 event.source 归属，支持并行 Agent。"""
        source = getattr(event, "source", "") or self._current_agent
        results: list[StreamEvent] = []
        for tc in event.content:
            try:
                args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
            except (json.JSONDecodeError, TypeError):
                args = tc.arguments
            results.append(self._make_event(
                StreamEventType.TOOL_CALL, f"调用 {tc.name}", {"tool_name": tc.name, "arguments": args}, source=source,
            ))
        return results

    def _translate_tool_result(self, event: ToolCallExecutionEvent) -> list[StreamEvent]:
        """翻译工具返回事件。用 event.source 归属，支持并行 Agent。"""
        source = getattr(event, "source", "") or self._current_agent
        results: list[StreamEvent] = []
        for r in event.content:
            is_error = getattr(r, "is_error", False)
            results.append(self._make_event(
                StreamEventType.TOOL_RESULT, r.content[:2000], {"tool_name": "", "is_error": is_error, "duration_ms": None}, source=source,
            ))
        return results

    def _translate_text_message(self, event: TextMessage) -> list[StreamEvent]:
        """翻译文本消息事件。关闭特定 Agent（用 event.source），支持并行。"""
        if getattr(event, "source", "") == "user":
            return []
        source = event.source
        self.last_content = event.content
        duration_ms = None
        if source in self._agent_start_times:
            duration_ms = int((datetime.now() - self._agent_start_times[source]).total_seconds() * 1000)
        results = [self._make_event(
            StreamEventType.AGENT_END, event.content[:5000],
            {"status": "success", "duration_ms": duration_ms}, source=source,
        )]
        self._active_agents.discard(source)
        self._agent_start_times.pop(source, None)
        self._reasoning_state.pop(source, None)
        if self._current_agent == source:
            self._current_agent = ""
            self._agent_start_time = None
        return results

    def _translate_structured_message(self, event: StructuredMessage) -> list[StreamEvent]:
        """翻译结构化消息事件。关闭特定 Agent（用 event.source），支持并行。"""
        content = event.content
        text = content.model_dump_json() if hasattr(content, "model_dump_json") else str(content)
        self.last_content = text
        source = getattr(event, "source", "") or self._current_agent
        duration_ms = None
        if source in self._agent_start_times:
            duration_ms = int((datetime.now() - self._agent_start_times[source]).total_seconds() * 1000)
        results = [self._make_event(
            StreamEventType.AGENT_END, text[:5000],
            {"status": "success", "duration_ms": duration_ms}, source=source,
        )]
        self._active_agents.discard(source)
        self._agent_start_times.pop(source, None)
        self._reasoning_state.pop(source, None)
        if self._current_agent == source:
            self._current_agent = ""
            self._agent_start_time = None
        return results

    def translate(
        self,
        event: BaseAgentEvent | BaseChatMessage | Response,
        plan: DAGPlan | None = None,
    ) -> list[StreamEvent]:
        """翻译单个 AutoGen 事件为零或多个 StreamEvent。"""
        results: list[StreamEvent] = []

        # Agent source 变化检测
        if isinstance(event, (BaseAgentEvent, BaseChatMessage)):
            source = getattr(event, "source", "")
            if source and source != "user" and source != self._current_agent:
                self._handle_agent_switch(source, plan, results)

        # 按事件类型分派翻译
        if isinstance(event, ModelClientStreamingChunkEvent):
            results.extend(self._translate_streaming_chunk(event))
        elif isinstance(event, ThoughtEvent):
            logger.debug("[EventTranslator] ThoughtEvent skipped (already streamed)")
        elif isinstance(event, ToolCallRequestEvent):
            results.extend(self._translate_tool_call(event))
        elif isinstance(event, ToolCallExecutionEvent):
            results.extend(self._translate_tool_result(event))
        elif isinstance(event, TextMessage):
            results.extend(self._translate_text_message(event))
        elif isinstance(event, StructuredMessage):
            results.extend(self._translate_structured_message(event))
        elif isinstance(event, Response):
            if event.chat_message:
                self.last_content = (
                    event.chat_message.to_text()
                    if hasattr(event.chat_message, "to_text")
                    else str(event.chat_message)
                )

        return results

    def make_plan_complete(
        self,
        content: str,
        agent_name: str,
        tasks: list[dict[str, Any]] | None = None,
        edges: list[dict[str, str]] | None = None,
        reasoning: str = "",
    ) -> StreamEvent:
        """生成 PLAN_COMPLETE 事件。"""
        data: dict[str, Any] = {"tasks": tasks or [], "reasoning": reasoning}
        if edges:
            data["edges"] = edges
        return StreamEvent(
            type=StreamEventType.PLAN_COMPLETE,
            seq=self._next_seq(),
            timestamp=self._now(),
            session_id=self._session_id,
            agent_name=agent_name,
            content=content,
            data=data,
        )

    def make_plan_start(self) -> StreamEvent:
        """生成 PLAN_START 事件。"""
        return StreamEvent(
            type=StreamEventType.PLAN_START,
            seq=self._next_seq(),
            timestamp=self._now(),
            session_id=self._session_id,
            content="开始规划任务",
            data={},
        )

    def make_agent_start(
        self,
        agent_name: str,
        agent_type: str,
        task_id: str,
        task_description: str,
    ) -> StreamEvent:
        """生成 AGENT_START 事件。"""
        self._current_agent = agent_name
        self._agent_start_time = datetime.now()
        return StreamEvent(
            type=StreamEventType.AGENT_START,
            seq=self._next_seq(),
            timestamp=self._now(),
            session_id=self._session_id,
            task_id=task_id,
            agent_name=agent_name,
            content=f"{agent_type} 开始执行",
            data={"agent_type": agent_type, "description": task_description},
        )

    def make_agent_end(
        self,
        agent_name: str,
        task_id: str,
        content: str,
        status: str = "success",
        duration_ms: int | None = None,
    ) -> StreamEvent:
        """生成 AGENT_END 事件。"""
        self.last_content = content
        return StreamEvent(
            type=StreamEventType.AGENT_END,
            seq=self._next_seq(),
            timestamp=self._now(),
            session_id=self._session_id,
            task_id=task_id,
            agent_name=agent_name,
            content=content,
            data={"status": status, "duration_ms": duration_ms},
        )

    def make_error(
        self,
        content: str,
        error_type: str = "",
        message: str = "",
    ) -> StreamEvent:
        """生成 ERROR 事件。"""
        return StreamEvent(
            type=StreamEventType.ERROR,
            seq=self._next_seq(),
            timestamp=self._now(),
            session_id=self._session_id,
            content=content,
            data={"error_type": error_type, "message": message},
        )

    def close_current_agent(self) -> StreamEvent | None:
        """如果当前有活跃 agent，生成其 AGENT_END 事件。用于 DAG 流程结束。"""
        if not self._current_agent:
            return None
        end_duration_ms = None
        if self._agent_start_time:
            end_duration_ms = int((datetime.now() - self._agent_start_time).total_seconds() * 1000)
        ev = StreamEvent(
            type=StreamEventType.AGENT_END,
            seq=self._next_seq(),
            timestamp=self._now(),
            session_id=self._session_id,
            task_id=self._extract_task_id(self._current_agent),
            agent_name=self._current_agent,
            content=f"{self._extract_agent_type(self._current_agent)} 执行完成",
            data={"status": "success", "duration_ms": end_duration_ms},
        )
        self._current_agent = ""
        self._agent_start_time = None
        return ev

    def close_all_agents(self) -> list[StreamEvent]:
        """关闭所有活跃 Agent，返回 AGENT_END 事件列表。用于 DAG 流程结束。"""
        events: list[StreamEvent] = []
        for agent in list(self._active_agents):
            duration_ms = None
            start = self._agent_start_times.get(agent)
            if start:
                duration_ms = int((datetime.now() - start).total_seconds() * 1000)
            events.append(StreamEvent(
                type=StreamEventType.AGENT_END,
                seq=self._next_seq(),
                timestamp=self._now(),
                session_id=self._session_id,
                task_id=self._extract_task_id(agent),
                agent_name=agent,
                content=f"{self._extract_agent_type(agent)} 执行完成",
                data={"status": "success", "duration_ms": duration_ms},
            ))
        self._active_agents.clear()
        self._agent_start_times.clear()
        self._reasoning_state.clear()
        self._current_agent = ""
        self._agent_start_time = None
        self._is_reasoning = False
        return events

    def make_table_event(
        self,
        key: str,
        columns: list[str],
        rows: list[list[Any]],
        row_count: int,
    ) -> StreamEvent:
        """生成 TABLE 事件，携带结构化表格数据供前端渲染。"""
        return StreamEvent(
            type=StreamEventType.TABLE,
            seq=self._next_seq(),
            timestamp=self._now(),
            session_id=self._session_id,
            content=f"表格数据: {key} ({row_count}行)",
            data={
                "key": key,
                "title": key,
                "columns": columns,
                "rows": rows,
                "row_count": row_count,
            },
        )


# ---------------------------------------------------------------------------
# Helper: 判断文本是否为自然语言回答（而非工具输出摘要）
# ---------------------------------------------------------------------------

_TOOL_OUTPUT_PREFIXES = (
    "API '",
    "数据已存入DataContext",
    "数据集 '",
    "列名: [",
    "数据类型: {",
    "前 ",
    "数值列统计:",
)

_DEFAULT_RESULTS = {"未能生成结果。", ""}
_AGENT_END_PATTERNS = ("执行完成", "执行失败")


def _needs_summarization(text: str) -> bool:
    """判断是否需要调用 LLM 生成自然语言总结。

    不需要总结的情况（直接返回 False）：
    - 文本较长（>200字符）且不含工具输出特征前缀，说明已是自然语言回答

    需要总结的情况：
    1. 结果为默认值（"未能生成结果。"或空字符串）
    2. 结果是工具输出摘要（以特征前缀开头或包含"调用成功"）
    3. 结果是 AGENT_END 的默认内容（"XXX 执行完成"）
    """
    if not text or text in _DEFAULT_RESULTS:
        return True
    for prefix in _TOOL_OUTPUT_PREFIXES:
        if text.strip().startswith(prefix):
            return True
    if len(text) < 200 and "调用成功" in text:
        return True
    for pattern in _AGENT_END_PATTERNS:
        if pattern in text and len(text) < 50:
            return True
    if len(text) > 200:
        return False
    return False