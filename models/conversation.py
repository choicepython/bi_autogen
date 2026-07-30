
"""多轮对话上下文数据模型。

实现设计文档中的三层上下文（意图层/数据层/决策层），
通过 Agent 自声明的 ContextSpec 按需组装，新增 Agent 零侵入框架。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel

from models.routing import AgentType, ExecutionMode


# ---------------------------------------------------------------------------
# 枚举与基础模型
# ---------------------------------------------------------------------------


class TaskStatus(str, Enum):
    """任务执行状态。"""

    SUCCESS = "success"
    EMPTY = "empty"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"


class AgentConclusion(BaseModel):
    """Agent 执行结论——带状态。"""

    agent_name: str
    status: TaskStatus = TaskStatus.SUCCESS
    conclusion: str = ""
    failure_reason: str = ""
    fallback_hint: str = ""


class DataRef(BaseModel):
    """数据引用——Agent 不拿原始 DataFrame，拿引用。"""

    key: str
    agent: str = ""
    status: TaskStatus = TaskStatus.SUCCESS
    schema: dict[str, str] = {}  # {列名: dtype}
    rows: int = 0
    summary: str = ""
    source: dict[str, Any] = {}  # 数据来源元数据（api_name/params 或 tool_name/code）

    @property
    def usable(self) -> bool:
        """数据是否可用于下游消费。"""
        return self.status == TaskStatus.SUCCESS and self.rows > 0


class RoutingSnapshot(BaseModel):
    """路由快照：不暴露路由内部逻辑，只暴露结论。"""

    mode: ExecutionMode = ExecutionMode.SINGLE_AGENT
    agent_type: AgentType | None = None
    task_description: str = ""


# ---------------------------------------------------------------------------
# TurnSummary — 单轮对话的结构化摘要
# ---------------------------------------------------------------------------


class TurnSummary(BaseModel):
    """单轮对话的结构化摘要——Agent 看到的是这个，不是原始消息流。"""

    turn_id: int
    query: str
    routing: RoutingSnapshot = RoutingSnapshot()
    agent_conclusions: list[AgentConclusion] = []
    data_produced: list[DataRef] = []
    dag_tasks: list[str] | None = None
    errors: list[str] = []


# ---------------------------------------------------------------------------
# ContextSpec — Agent 自声明上下文需求
# ---------------------------------------------------------------------------


class ContextSpec(BaseModel):
    """Agent 的上下文需求声明——Agent 自己定义，ConversationContext 消费。

    不声明则走默认值（保守策略：last + schema + own），不会泄露不该看的信息。
    """

    query_history: str = "last"  # none / last / full
    data_catalog: str = "schema"  # none / schema / full
    conclusions: str = "own"  # none / own / all
    dag_history: bool = False
    tool_summary: bool = False
    planning_hints: bool = False


# ---------------------------------------------------------------------------
# ConversationContext — 完整多轮对话上下文
# ---------------------------------------------------------------------------


class ConversationContext(BaseModel):
    """完整的多轮对话上下文——DispatchLayer 组装，AgentLayer 消费。"""

    session_id: str
    turns: list[TurnSummary] = []
    data_catalog: list[DataRef] = []
    current_query: str = ""

    @property
    def available_data(self) -> list[DataRef]:
        """真正可用的数据。"""
        return [d for d in self.data_catalog if d.usable]

    @property
    def failed_data(self) -> list[DataRef]:
        """失败/不可用的数据。"""
        return [d for d in self.data_catalog if not d.usable]

    def for_agent(self, agent_type: AgentType | str) -> str:
        """通用上下文组装：读 Agent 的 context_spec，按需过滤。"""
        spec = self._get_spec(agent_type)
        parts: list[str] = []

        # 查询历史
        if spec.query_history == "full":
            parts.append(self._render_query_history(full=True))
        elif spec.query_history == "last":
            parts.append(self._render_query_history(full=False))

        # 数据目录
        if spec.data_catalog == "full":
            parts.append(self._render_data_catalog(include_failed=True))
        elif spec.data_catalog == "schema":
            parts.append(self._render_data_catalog(include_failed=False))

        # 结论摘要
        if spec.conclusions == "all":
            parts.append(self._render_conclusions(filter_agent=None))
        elif spec.conclusions == "own":
            agent_name = self._normalize_agent_name(agent_type)
            parts.append(self._render_conclusions(filter_agent=agent_name))

        # 历史 DAG
        if spec.dag_history:
            parts.append(self._render_dag_history())

        # 增量规划提示
        if spec.planning_hints:
            parts.append(self._render_planning_hints())

        # 过滤空部分
        return "\n\n".join(p for p in parts if p)

    # --- 内部方法 ---

    @staticmethod
    def _get_spec(agent_type: AgentType | str) -> ContextSpec:
        """从 Agent 类上读取 context_spec，委托 core.spec_resolver。"""
        # lazy import: 避免 models → core 循环依赖（运行时所有模块已加载）
        from core.spec_resolver import resolve_context_spec
        return resolve_context_spec(agent_type)

    @staticmethod
    def _normalize_agent_name(agent_type: AgentType | str) -> str:
        """将 AgentType/str 统一为 Agent 类名。"""
        name = agent_type.value if isinstance(agent_type, AgentType) else str(agent_type)
        # AgentType 枚举值如 "API" → "APIAgent"
        if not name.endswith("Agent"):
            name = name + "Agent"
        return name

    def _render_query_history(self, full: bool = False) -> str:
        """渲染查询历史。"""
        if not self.turns:
            return ""
        turns_to_show = self.turns if full else self.turns[-1:]
        lines = [f"第{t.turn_id}轮: {t.query}" for t in turns_to_show]
        return "## 历史查询\n" + "\n".join(lines)

    def _render_data_catalog(self, include_failed: bool = False) -> str:
        """渲染数据目录。"""
        if include_failed:
            refs = self.data_catalog
        else:
            refs = self.available_data
        if not refs and not self.failed_data:
            return ""
        if not refs:
            return ""
        lines = [f"- {d.key}: {d.summary} ({d.rows}行, 列: {list(d.schema.keys())})" for d in refs]
        header = "## 数据目录"
        if include_failed:
            header = "## 数据目录（含不可用）"
            # 分离可用和不可用
            avail_lines = [f"- {d.key}: {d.summary} ({d.rows}行)" for d in self.available_data]
            failed_lines = [f"- {d.key}: ❌ {d.status.value} — {d.summary}" for d in self.failed_data]
            parts = []
            if avail_lines:
                parts.append("### 可用\n" + "\n".join(avail_lines))
            if failed_lines:
                parts.append("### 不可用（请勿依赖）\n" + "\n".join(failed_lines))
            return header + "\n" + "\n\n".join(parts)
        return header + "\n" + "\n".join(lines)

    def _render_conclusions(self, filter_agent: str | None = None) -> str:
        """渲染 Agent 结论摘要。"""
        conclusions: list[str] = []
        for t in self.turns:
            for c in t.agent_conclusions:
                if filter_agent and filter_agent not in c.agent_name:
                    continue
                icon = "✓" if c.status == TaskStatus.SUCCESS else "✗"
                text = f"- {icon} {c.agent_name}: {c.conclusion}"
                if c.failure_reason:
                    text += f" (原因: {c.failure_reason})"
                if c.fallback_hint:
                    text += f" [替代: {c.fallback_hint}]"
                conclusions.append(text)
        if not conclusions:
            return ""
        return "## 已有结论\n" + "\n".join(conclusions)

    def _render_dag_history(self) -> str:
        """渲染历史 DAG 结构。"""
        if not self.turns:
            return ""
        lines = []
        for t in self.turns:
            mode = t.routing.mode.value
            agents = " → ".join(c.agent_name for c in t.agent_conclusions)
            tasks = f" [{', '.join(t.dag_tasks)}]" if t.dag_tasks else ""
            lines.append(f"第{t.turn_id}轮: {mode} → {agents}{tasks}")
        return "## 历史规划\n" + "\n".join(lines)

    @staticmethod
    def _render_planning_hints() -> str:
        """渲染增量规划提示。"""
        return (
            "## 规划原则\n"
            "- 可用数据目录中的数据已自动加载到 DataContext，下游 Agent 可直接使用\n"
            "- 已有结论不需要重新计算，直接引用\n"
            "- 仅依赖「可用数据」规划下游任务\n"
            "- 如果所需数据不在可用数据目录中，必须先规划数据获取任务（APIAgent/SQLAgent）\n"
            "- 优先尝试替代方案（如 APIAgent 失败可换 SQLAgent）\n"
            "- 不可假设数据存在，必须从可用数据目录确认\n"
            "- 只规划本轮新增的任务\n"
            "- 新任务的依赖要明确标注数据来源 key"
        )