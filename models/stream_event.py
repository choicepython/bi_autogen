
"""流式输出事件模型：BI Agent 系统的标准 SSE 事件协议。

事件生命周期::

    SESSION_START
      PLAN_START → PLAN_COMPLETE
      AGENT_START
        TOOL_CALL → TOOL_RESULT  (可多次)
        LLM_CHUNK              (可多次)
        DATA_STORED            (可多次)
      AGENT_END
      ... (下一个 Agent)
    SESSION_END

所有流式输出必须遵循此事件协议，确保前端 / API 网关 / CLI 消费端统一解析。

前端集成指南
===========

按渲染区域和用途，事件分为 4 类：

1. 过程展示 → 进度面板 / 步骤条
   用户看到"系统正在做什么"，渲染到侧边栏或时间线组件：

   - PLAN_COMPLETE  显示 DAG 任务步骤条和依赖关系
   - AGENT_START    高亮当前执行步骤，显示 "XXAgent 正在执行..."
   - TOOL_CALL      折叠详情：调用工具名 + 参数摘要
   - TOOL_RESULT    折叠摘要：返回结果预览（is_error=true 时标红）
   - AGENT_END      标记步骤完成：打勾 + 耗时

2. 回答展示 → 主聊天区
   用户最终要看的文本内容，逐字/逐段渲染到对话气泡：

   - LLM_CHUNK      流式追加文本，逐字渲染（打字机效果）
   - SESSION_END    最终结果兜底（当没有 LLM_CHUNK 流式输出时使用 data.result）

3. UI 状态控制 → 不直接展示，控制界面状态
   - SESSION_START  开启加载动画，禁用输入框
   - PLAN_START     显示"规划中"骨架屏

4. 不做展示 → 纯内部事件
   - DATA_STORED    DataContext 内部写入，用户无需感知

5. 错误 → 特殊处理
   - ERROR          toast 提示 / 回答区红色错误卡片，
                    data.error_type 可做国际化映射

前端集成代码示例::

    const eventSource = new EventSource('/api/stream?task=...');

    // --- 过程展示 ---
    eventSource.addEventListener('plan_complete', (e) => {
        const { data } = JSON.parse(e.data);
        renderStepTimeline(data.data.tasks);
    });
    eventSource.addEventListener('agent_start', (e) => {
        const { data } = JSON.parse(e.data);
        highlightStep(data.data.agent_type);
    });
    eventSource.addEventListener('tool_call', (e) => {
        const { data } = JSON.parse(e.data);
        appendToolLog(data.data.tool_name, data.data.arguments);
    });
    eventSource.addEventListener('tool_result', (e) => {
        const { data } = JSON.parse(e.data);
        appendToolResult(data.data.is_error);
    });
    eventSource.addEventListener('agent_end', (e) => {
        const { data } = JSON.parse(e.data);
        markStepDone(data.data.status, data.data.duration_ms);
    });

    // --- 回答展示 ---
    eventSource.addEventListener('llm_chunk', (e) => {
        const { data } = JSON.parse(e.data);
        appendAnswerText(data.data.chunk);
    });
    eventSource.addEventListener('session_end', (e) => {
        const { data } = JSON.parse(e.data);
        if (!hasStreamedText) renderFinalAnswer(data.data.result);
    });

    // --- UI 状态控制 ---
    eventSource.addEventListener('session_start', () => setLoading(true));
    eventSource.addEventListener('plan_start', () => showSkeleton());
    eventSource.addEventListener('session_end', () => setLoading(false));

    // --- 错误 ---
    eventSource.addEventListener('error', (e) => {
        const { data } = JSON.parse(e.data);
        showErrorToast(data.data.error_type, data.data.message);
    });
"""

from __future__ import annotations

import json
from enum import Enum
from typing import Any

from pydantic import BaseModel


class StreamEventType(str, Enum):
    """SSE 事件类型枚举。

    ┌──────────────┬────────────────────────────────────────────────────┬──────────────┐
    │ 事件类型      │ 说明                                               │ 前端展示分类  │
    ├──────────────┼────────────────────────────────────────────────────┼──────────────┤
    │ SESSION_START│ 会话开始，携带用户查询                              │ UI 状态控制  │
    │ SESSION_END  │ 会话结束，携带最终结果和耗时                        │ 回答展示(兜底)│
    │ PLAN_START   │ 开始规划                                           │ UI 状态控制  │
    │ PLAN_COMPLETE│ 规划完成，携带 DAG 任务列表                         │ 过程展示      │
    │ AGENT_START  │ Agent 开始执行，携带类型和任务描述                   │ 过程展示      │
    │ AGENT_END    │ Agent 执行完成，携带状态和耗时                      │ 过程展示      │
    │ TOOL_CALL    │ 调用工具，携带工具名和参数                           │ 过程展示      │
    │ TOOL_RESULT  │ 工具返回结果，携带是否出错                           │ 过程展示      │
    │ LLM_CHUNK   │ LLM 流式输出片段                                    │ 回答展示      │
    │ THINK_CHUNK │ LLM 思考过程片段（reasoning_content）               │ 过程展示      │
    │ DATA_STORED  │ 数据写入 DataContext，携带 key 和数据摘要            │ 不展示        │
    │ TABLE        │ 结构化表格数据，携带列名和行数据                     │ 表格展示      │
    │ USER_QUESTION│ PlanAgent 向用户提问，携带问题和选项                 │ 用户交互      │
    │ USER_ANSWER  │ 用户答复已收到，携带 question_id 和答复              │ 用户交互      │
    │ ERROR        │ 错误事件，携带错误类型和消息                         │ 错误提示      │
    └──────────────┴────────────────────────────────────────────────────┴──────────────┘
    """

    # 会话生命周期
    SESSION_START = "session_start"
    SESSION_END = "session_end"

    # 规划阶段
    PLAN_START = "plan_start"
    PLAN_COMPLETE = "plan_complete"

    # Agent 生命周期
    AGENT_START = "agent_start"
    AGENT_END = "agent_end"

    # 工具生命周期
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"

    # LLM 流式输出
    LLM_CHUNK = "llm_chunk"
    THINK_CHUNK = "think_chunk"

    # 数据上下文
    DATA_STORED = "data_stored"

    # 结构化表格数据
    TABLE = "table"

    # 用户交互（PlanAgent 主动提问）
    USER_QUESTION = "user_question"
    USER_ANSWER = "user_answer"

    # 错误
    ERROR = "error"


# ---------------------------------------------------------------------------
# 各事件类型的 data 字段 schema 文档
# ---------------------------------------------------------------------------

# SESSION_START:  {"query": str}
# SESSION_END:    {"result": str, "duration_ms": int}
# PLAN_START:     {}
# PLAN_COMPLETE:  {"tasks": list[dict], "reasoning": str}
# AGENT_START:    {"agent_type": str, "description": str}
# AGENT_END:      {"status": "success" | "error", "duration_ms": int}
# TOOL_CALL:      {"tool_name": str, "arguments": dict | str}
# TOOL_RESULT:    {"tool_name": str, "is_error": bool, "duration_ms": int | None}
# LLM_CHUNK:      {"chunk": str}
# THINK_CHUNK:    {"chunk": str}
# DATA_STORED:    {"key": str, "shape": list[int] | None, "columns": list[str] | None}
# TABLE:          {"key": str, "title": str, "columns": list[str], "rows": list[list], "row_count": int}
# USER_QUESTION:  {"question_id": str, "question_type": str, "options": list[str] | None,
#                  "context": str, "default": str | None, "timeout_seconds": int,
#                  "ask_count": int, "max_asks": int}
# USER_ANSWER:    {"question_id": str, "answer": str, "answer_type": str}
# ERROR:          {"error_type": str, "message": str}


class StreamEvent(BaseModel):
    """流式输出事件，由 BITeam.run_stream() yield。

    字段说明::

        seq        全局递增序号，用于 SSE id 和断线重连
        type       事件类型
        timestamp  ISO 8601 时间戳
        session_id 会话 ID（格式 YYYYMMDD_HHMMSS）
        task_id    DAG 任务节点 ID（如 "task_1"），非任务事件为空
        agent_name Agent 实例名（如 "APIAgent_task_1"），非 Agent 事件为空
        content    人类可读摘要文本，适合 CLI / 日志展示
        data       事件类型专有的结构化数据，见上方 schema 文档

    SSE 输出示例::

        event: tool_call
        data: {"seq":3,"type":"tool_call","timestamp":"2026-07-23T10:00:02","session_id":"20260723_100001","task_id":"task_1","agent_name":"APIAgent_task_1","content":"调用 api_query","data":{"tool_name":"api_query","arguments":{"query":"产量"}}}
        id: 3

    """

    type: StreamEventType
    seq: int = 0
    timestamp: str = ""
    session_id: str = ""
    task_id: str = ""
    agent_name: str = ""
    content: str = ""
    data: dict[str, Any] = {}

    def to_sse(self) -> str:
        """转换为标准 SSE 文本格式。

        SSE 规范 (https://html.spec.whatwg.org/multipage/server-sent-events.html):
        - event: 事件类型
        - data:  负载数据（JSON）
        - id:    事件序号，用于断线重连
        - 每条消息以空行 \\n 结尾
        """
        payload = json.dumps(
            {
                "seq": self.seq,
                "type": self.type.value,
                "timestamp": self.timestamp,
                "session_id": self.session_id,
                "task_id": self.task_id,
                "agent_name": self.agent_name,
                "content": self.content,
                "data": self.data,
            },
            ensure_ascii=False,
        )
        lines = [
            f"event: {self.type.value}",
            f"data: {payload}",
        ]
        if self.seq > 0:
            lines.append(f"id: {self.seq}")
        return "\n".join(lines) + "\n\n"