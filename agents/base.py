from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar

from autogen_agentchat.agents import AssistantAgent
from autogen_agentchat.base import Response
from autogen_agentchat.messages import (
    BaseAgentEvent,
    BaseChatMessage,
    TextMessage,
    ToolCallExecutionEvent,
    ToolCallRequestEvent,
)
from autogen_core import CancellationToken
from autogen_core.models import ChatCompletionClient
from pydantic import BaseModel

from core.context import SessionContext
from core.data_context import DataContext
from observability.observer_factory import get_trace_observer
from observability.trace import get_trace_recorder

if TYPE_CHECKING:
    from models.conversation import ConversationContext

logger = logging.getLogger(__name__)

# 合法的Agent类型名
AGENT_TYPES = {"APIAgent", "PyFuncAgent", "ReportAgent", "SQLAgent", "DataAnalysisAgent", "VisualizationAgent", "RAGAgent"}


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


def make_agent_name(agent_type: str, task_id: str | None = None) -> str:
    """生成agent名称。有task_id时格式为 'AgentType_task_id'，否则为 'AgentType'。"""
    if task_id:
        return f"{agent_type}_{task_id}"
    return agent_type


# 失败/成功关键词，用于检测 agent 输出是否表示任务失败
_FAILURE_KEYWORDS = ("执行失败", "调用失败", "查询失败", "生成失败", "DAG生成失败", "报告生成失败", "任务跳过")
_SUCCESS_KEYWORDS = ("成功", "完成", "已存入")


def looks_like_failure(content: str) -> bool:
    """检测 agent 输出内容是否表示失败。

    "任务跳过" 也在失败关键词中，确保跳过能传播到更下游的依赖任务。
    """
    if not content or len(content) < 5:
        return True
    has_failure = any(kw in content for kw in _FAILURE_KEYWORDS)
    has_success = any(kw in content for kw in _SUCCESS_KEYWORDS)
    return has_failure and not has_success


class BIBaseAgent(AssistantAgent):
    """BI Agent 基类，基于组合模式包装 AssistantAgent，提供生命周期钩子。

    子类可覆写以下钩子方法介入 agent 运行流程：
    - on_before_turn(messages): agent 开始处理前调用
    - on_after_turn(response, messages): agent 完成一轮后调用，可观察/修改最终输出

    子类可通过类属性声明配置：
    - context_spec: 多轮对话上下文需求声明
    - prompt_template: prompt 模板名（如 "api_agent"），基类自动渲染
    - _extra_prompt_vars(): 注入额外 prompt 变量（如 SKILLS、DATA_CONTEXT）
    """

    # 默认保守策略：last + schema + own
    context_spec: ClassVar[ContextSpec] = ContextSpec()

    # 思考分层：True=深度思考（规划/代码/报告），False=浅层执行（选API/写SQL/搜索/选图表）
    # 子类按职责覆写。在 on_messages/on_messages_stream 入口设置到 ContextVar，
    # LoggingChatCompletionClient 据此覆盖 extra_body.chat_template_kwargs.enable_thinking。
    thinking_enabled: ClassVar[bool] = True

    # prompt 模板名，子类覆写。基类自动渲染 system_message。
    # 为空字符串时跳过自动渲染（如 PlanAgent 自行管理 prompt）。
    prompt_template: ClassVar[str] = ""

    # Agent 描述，子类覆写。用于 description 参数和 prompt 的 AGENT_ROLE。
    agent_description: ClassVar[str] = ""

    def __init__(
        self,
        model_client: ChatCompletionClient,
        data_context: DataContext | None = None,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
        *,
        name: str | None = None,
        description: str | None = None,
        system_message: str | None = None,
        tools: list[Any] | None = None,
        output_content_type: type | None = None,
        reflect_on_tool_use: bool = False,
        max_tool_iterations: int = 1,
        global_tools: list[Any] | None = None,
    ) -> None:
        self.data_context = data_context
        self.session = session
        self.task_id = task_id
        self.task_description = task_description
        # DAG 依赖：本任务依赖的上游 task_id 列表（由 AgentFactory.create_from_task_node 设置）
        self._depends_on: list[str] = []
        # 本任务是否失败（供 run_team 集中检测）
        self._failed: bool = False

        # 自动生成 name 和 description
        agent_type = type(self).__name__
        _name = name or make_agent_name(agent_type, task_id)
        _description = description or self.agent_description or f"{agent_type}助手"

        # 自动渲染 system_message（如果子类声明了 prompt_template 且未显式传入）
        if system_message is None and self.prompt_template:
            system_message = self._render_prompt(_name)
        elif system_message is None:
            system_message = ""

        # 全局工具在前，agent 专属工具在后；同名时专属工具覆盖全局（dict 去重由 AutoGen 处理）
        merged_tools: list[Any] = []
        if global_tools:
            merged_tools.extend(global_tools)
        if tools:
            merged_tools.extend(tools)
        super().__init__(
            name=_name,
            description=_description,
            model_client=model_client,
            system_message=system_message,
            tools=merged_tools,
            output_content_type=output_content_type,
            reflect_on_tool_use=reflect_on_tool_use,
            max_tool_iterations=max_tool_iterations,
            model_client_stream=True,
        )

    def _render_prompt(self, agent_name: str) -> str:
        """渲染 prompt 模板，注入公共变量 + 子类额外变量。"""
        from config.prompt_manager import get_prompt_manager

        today = datetime.now().strftime("%Y年%m月%d日")
        vars_dict: dict[str, str] = {
            "AGENT_NAME": agent_name,
            "AGENT_ROLE": self.agent_description or type(self).__name__,
            "DATE": today,
        }
        vars_dict.update(self._extra_prompt_vars())
        return get_prompt_manager().render(self.prompt_template, **vars_dict)

    def _extra_prompt_vars(self) -> dict[str, str]:
        """子类覆写，注入额外 prompt 变量（如 SKILLS、DATA_CONTEXT）。"""
        return {}

    async def on_messages(
        self,
        messages: Sequence[BaseChatMessage],
        cancellation_token: CancellationToken,
    ) -> Response:
        """重写 on_messages 入口，在执行前设置 agent 名称到 ContextVar。

        这是 AutoGen GraphFlow 调用 agent 的标准入口。
        任何子类（包括重写 on_messages_stream 的 PyFuncAgent）
        都会经过此方法，确保 LLM 日志中的 agent 名称正确。
        同时按 thinking_enabled 配置切换深度/浅层思考模式。
        """
        from observability.logging_client import set_current_agent, set_enable_thinking
        set_current_agent(self.name)
        set_enable_thinking(self.thinking_enabled)
        return await super().on_messages(messages, cancellation_token)

    async def on_messages_stream(
        self,
        messages: Sequence[BaseChatMessage],
        cancellation_token: CancellationToken,
    ) -> AsyncGenerator[BaseAgentEvent | BaseChatMessage | Response, None]:
        """拦截 AssistantAgent 的消息流，在关键节点调用钩子并记录结构化输出。"""
        # 跳过检查：上游依赖任务已失败
        session = getattr(self, "session", None)
        if session and self._depends_on:
            failed = getattr(session, "_failed_task_ids", set())
            if any(dep in failed for dep in self._depends_on):
                skip_content = f"任务跳过：上游依赖任务 {self._depends_on} 失败"
                skip_msg = TextMessage(content=skip_content, source=self.name)
                yield skip_msg
                yield Response(chat_message=skip_msg)
                # 标记自身为失败（传播跳过到更下游）
                session._failed_task_ids.add(self.task_id or "")
                self._failed = True
                return

        # 如果设置了 task_description，替换最后一条用户消息为任务描述
        if self.task_description:
            # 找到最后一条 user 消息并替换
            last_user_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                if getattr(messages[i], "source", None) == "user":
                    last_user_idx = i
                    break
            if last_user_idx >= 0:
                modified = list(messages)
                modified[last_user_idx] = TextMessage(content=self.task_description, source="user")
                messages = modified

        # 前置钩子
        await self.on_before_turn(messages)

        # 设置当前 Agent 名称到 ContextVar（直接调用 on_messages_stream 时需要，
        # 通过 on_messages 入口时已在调用前设置），并按 thinking_enabled 配置思考模式
        from observability.logging_client import set_current_agent, set_enable_thinking
        set_current_agent(self.name)
        set_enable_thinking(self.thinking_enabled)

        # 缓冲工具调用事件，用于结构化记录
        pending_tool_calls: dict[str, dict[str, Any]] = {}  # call_id -> {name, arguments}
        recorder = get_trace_recorder()

        # 透传内部事件流，拦截最终 Response
        final_response: Response | None = None
        async for event in super().on_messages_stream(messages, cancellation_token):
            if isinstance(event, Response):
                final_response = event
                break

            # 拦截工具调用请求，缓冲 tool_name 和 arguments
            if isinstance(event, ToolCallRequestEvent) and recorder is not None:
                for tc in event.content:
                    try:
                        args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                    except (json.JSONDecodeError, TypeError):
                        args = tc.arguments
                    pending_tool_calls[tc.id] = {"name": tc.name, "arguments": args}

            # 拦截工具调用结果，与请求配对后写入 trace + DB
            if isinstance(event, ToolCallExecutionEvent):
                for r in event.content:
                    call_info = pending_tool_calls.pop(r.call_id, None)
                    if call_info is not None:
                        tool_name = call_info["name"]
                        # 去掉内部方法名前缀的下划线，记录用户可见的工具名
                        display_name = tool_name.lstrip("_")
                        result_text = r.content[:3000] if len(r.content) > 3000 else r.content
                        is_error = getattr(r, "is_error", False)
                        await BIBaseAgent._record_tool_call(
                            agent_name=self.name,
                            task_id=self.task_id or "",
                            tool_name=display_name,
                            call_id=r.call_id,
                            arguments=call_info["arguments"],
                            result=result_text,
                            is_error=is_error,
                            recorder=recorder,
                        )

            yield event

        # 后置钩子：可观察或替换 Response
        if final_response is not None:
            processed = await self.on_after_turn(final_response, messages)
            yield processed or final_response

    @staticmethod
    async def _record_tool_call(
        agent_name: str,
        task_id: str,
        tool_name: str,
        call_id: str,
        arguments: Any,
        result: str,
        is_error: bool = False,
        recorder: Any = None,
    ) -> None:
        """记录工具调用到 trace + DB（供 on_messages_stream 与 PyFuncAgent 共用）。

        PyFuncAgent 绕过 BIBaseAgent.on_messages_stream 自行调用 python_exec，
        故需显式调用本助手补全工具调用记录。
        """
        if recorder is not None:
            recorder.record_tool_call(
                agent_name=agent_name,
                tool_name=tool_name,
                arguments=arguments,
                result=result,
            )
        # Langfuse tool span（retrospective — 记录工具调用元数据）
        with get_trace_observer().start_span(
            f"tool:{tool_name}",
            metadata={
                "agent_name": agent_name,
                "task_id": task_id,
                "arguments": arguments if isinstance(arguments, dict) else {"raw": str(arguments)[:1000]},
                "result_preview": result[:1000],
                "is_error": is_error,
            },
        ):
            pass
        # DB: 异步写入工具调用记录
        try:
            from db.writer import db_writer, make_tool_call_data
            from observability.logging_client import _chat_id_var, _session_id_var
            await db_writer.enqueue_tool_call(make_tool_call_data(
                session_id=_session_id_var.get(),
                chat_id=_chat_id_var.get(),
                task_id=task_id,
                agent_name=agent_name,
                tool_name=tool_name,
                call_id=call_id,
                arguments=arguments,
                is_error=is_error,
                error_message=result[:2000] if is_error else "",
                result_preview=result[:2000] if not is_error else "",
            ))
        except Exception as db_err:
            logger.debug("[BIBaseAgent] DB写入工具调用失败: %s", db_err)

    async def on_before_turn(self, messages: Sequence[BaseChatMessage]) -> None:
        """钩子：agent 开始处理前调用。注入多轮对话历史为 LLM messages 规范格式。

        按 LLM chat completion 协议，将历史轮次以 user/assistant 消息对的形式
        插入到 messages 列表前面，让 LLM 自然理解多轮对话上下文。

        格式：
          [system, ...历史轮次(user→assistant), 当前user消息]

        历史轮次内容：
        - user 消息：历史查询
        - assistant 消息：该 Agent 在该轮的结论 + 数据目录 + 规划提示
        """
        session = getattr(self, "session", None)
        if session is None:
            return
        ctx = getattr(session, "conversation_context", None)
        if ctx is None:
            return
        # 提取 agent 类型名
        agent_type = ""
        for at in AGENT_TYPES:
            if self.name.startswith(at):
                agent_type = at
                break
        if not agent_type:
            return

        # 按消息规范构建历史轮次
        history_messages = self._build_history_messages(ctx, agent_type)
        if not history_messages:
            return

        # 将历史消息插入到 messages 列表前面（在 system 之后、当前 user 之前）
        # messages 是 Sequence，需要转为 list 才能修改
        # 注意：PyFuncAgent 传入的是 list，其他 Agent 通过 on_messages_stream 也传入 list
        if isinstance(messages, list):
            # 找到最后一条 user message 的位置
            last_user_idx = -1
            for i in range(len(messages) - 1, -1, -1):
                if getattr(messages[i], "source", None) == "user":
                    last_user_idx = i
                    break
            if last_user_idx >= 0:
                for j, hist_msg in enumerate(history_messages):
                    messages.insert(last_user_idx + j, hist_msg)

    @staticmethod
    def _build_history_messages(ctx: ConversationContext, agent_type: str) -> list[BaseChatMessage]:
        """按 LLM messages 规范构建历史对话消息列表。

        每个历史轮次生成一对 user/assistant 消息：
        - user: 历史查询
        - assistant: 该 Agent 可见的上下文摘要（结论 + 数据 + 规划提示）

        这样 LLM 能自然理解"之前我说过/做过什么"，无需在 user 消息中拼接大段上下文。
        """
        from core.spec_resolver import resolve_context_spec

        spec = resolve_context_spec(agent_type)
        history: list[BaseChatMessage] = []

        for turn in ctx.turns:
            # user 消息：历史查询
            history.append(TextMessage(content=turn.query, source="user"))

            # assistant 消息：组装该 Agent 可见的上下文摘要
            parts: list[str] = []

            # 结论摘要
            if spec.conclusions in ("all", "own"):
                conclusions = []
                for c in turn.agent_conclusions:
                    if spec.conclusions == "own" and agent_type not in c.agent_name:
                        continue
                    icon = "✓" if c.status.value == "success" else "✗"
                    text = f"- {icon} {c.agent_name}: {c.conclusion}"
                    if c.failure_reason:
                        text += f" (原因: {c.failure_reason})"
                    conclusions.append(text)
                if conclusions:
                    parts.append("## 执行结论\n" + "\n".join(conclusions))

            # 数据目录
            if spec.data_catalog in ("full", "schema"):
                data_parts = []
                for d in turn.data_produced:
                    if spec.data_catalog == "schema" and not d.usable:
                        continue
                    status_icon = "✓" if d.usable else "✗"
                    schema_info = f" (列: {list(d.schema.keys())})" if d.schema else ""
                    sample_info = ""
                    if d.usable and d.schema:
                        # 从 schema 中提取前3列名作为样本提示
                        cols = list(d.schema.keys())[:3]
                        sample_info = f"，含列如: {cols}"
                    # 数据来源溯源（source 含 api_name 或 tool_name 时展示）
                    source_info = ""
                    src = d.source or {}
                    api_name = src.get("api_name")
                    if api_name:
                        src_params = src.get("params")
                        if src_params:
                            source_info = f"，来源: API {api_name}(参数: {src_params})"
                        else:
                            source_info = f"，来源: API {api_name}"
                    elif src.get("tool_name"):
                        source_info = f"，来源: {src['tool_name']}"
                    data_parts.append(f"- {status_icon} {d.key}: {d.summary}{schema_info}{sample_info}{source_info}")
                if data_parts:
                    parts.append("## 数据产出\n" + "\n".join(data_parts))

            # DAG 历史
            if spec.dag_history and turn.dag_tasks:
                mode = turn.routing.mode.value
                agents = " → ".join(c.agent_name for c in turn.agent_conclusions)
                tasks = f" [{', '.join(turn.dag_tasks)}]"
                parts.append(f"## 规划\n{mode} → {agents}{tasks}")

            # 规划提示
            if spec.planning_hints:
                parts.append(
                    "规划原则：已有数据/结论直接引用，不重复获取；仅规划新增任务；依赖标注数据来源key"
                )

            assistant_content = "\n\n".join(parts) if parts else "（无输出）"
            history.append(TextMessage(content=assistant_content, source="assistant"))

        return history

    async def on_after_turn(
        self, response: Response, messages: Sequence[BaseChatMessage]
    ) -> Response | None:
        """钩子：agent 完成一轮后调用。返回 None 保持原 response，返回 Response 替换。"""
        return None