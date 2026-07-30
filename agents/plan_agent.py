
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Sequence
from datetime import datetime
from typing import Any, ClassVar

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

from agents.base import BIBaseAgent, ContextSpec
from config.prompt_manager import get_prompt_manager
from core.ask_user_registry import ask_user_registry
from core.context import SessionContext
from core.skill_manager import get_skill_manager
from models.dag_plan import DAGPlan
from models.stream_event import StreamEvent, StreamEventType
from models.user_question import AnswerType, QuestionType
from observability.logging_client import set_current_agent, set_enable_thinking
from observability.trace import get_trace_recorder
from tools.ask_user import AskUserHandler, make_ask_user_tool_with_queue
from tools.dag_tool import make_dag_tool
from tools.get_es_data import format_api_list_detailed

logger = logging.getLogger(__name__)


class PlanAgent(BIBaseAgent):
    """任务规划专家，一次性输出完整DAG任务列表和依赖关系。

    内置 ask_user FunctionTool：当 API 必填参数缺失时，LLM 调用 ask_user
    向用户提问。on_messages_stream 拦截 ToolCallRequestEvent，创建
    USER_QUESTION 事件，等待用户答复后通过 asyncio.Queue 将答复传给
    tool function，AutoGen 将结果喂回 LLM，LLM 最终输出 DAG 计划。
    """

    context_spec: ClassVar[ContextSpec] = ContextSpec(
        query_history="full",
        data_catalog="full",
        conclusions="all",
        dag_history=True,
        tool_summary=True,
        planning_hints=True,
    )

    def __init__(
            self,
            model_client: ChatCompletionClient,
            session: SessionContext | None = None,
            *,
            translator: Any | None = None,
    ) -> None:
        self._session = session
        self._translator = translator
        self._final_content = ""
        self._dag_plan: DAGPlan | None = None

        # ask_user 基础设施
        self._ask_user_handler = AskUserHandler()
        self._answer_queue: asyncio.Queue[str] = asyncio.Queue()
        if session:
            ask_user_registry.register(session.session_id, self._ask_user_handler)

        # ask_user / dag 工具（工厂函数，详见 tools/ask_user.py、tools/dag_tool.py）
        ask_user_tool = make_ask_user_tool_with_queue(self._answer_queue)
        dag_tool = make_dag_tool(lambda plan: setattr(self, "_dag_plan", plan))

        today = datetime.now().strftime("%Y年%m月%d日")
        api_list_text = format_api_list_detailed(session.api_meta if session else [])
        skills_text = get_skill_manager().format_skills_for_prompt(session.skills if session else [])
        system_message = get_prompt_manager().render(
            "plan_agent",
            AGENT_NAME="PlanAgent",
            AGENT_ROLE="任务规划专家",
            DATE=today,
            API_LIST=api_list_text,
            SKILLS=skills_text,
        )
        super().__init__(
            name="PlanAgent",
            description="任务规划专家，负责分析用户需求并输出完整的DAG任务执行计划。",
            model_client=model_client,
            system_message=system_message,
            tools=[ask_user_tool, dag_tool],
        )

    async def on_before_turn(self, messages: Sequence[BaseChatMessage]) -> None:
        """注入多轮对话历史消息（复用 BIBaseAgent._build_history_messages）。"""
        conv_ctx = self._session.conversation_context if self._session else None
        if conv_ctx is not None and isinstance(messages, list):
            history = BIBaseAgent._build_history_messages(conv_ctx, "PlanAgent")
            for msg in history:
                messages.insert(0, msg)  # type: ignore[arg-type]

    async def on_after_turn(self, response: Response, messages: Sequence[BaseChatMessage]) -> Response | None:
        """记录 DAGPlan 结构化输出到 TraceRecorder。"""
        recorder = get_trace_recorder()
        if recorder is None:
            return None

        # dag 工具已存储 plan
        if self._dag_plan is not None:
            recorder.record_plan_output(self._dag_plan)
            logger.info("[PlanAgent] 已记录DAG规划: %d个任务", len(self._dag_plan.tasks))
            return None

        # Fallback: 解析 response 文本为 JSON
        if response.chat_message and isinstance(response.chat_message, TextMessage):
            content = response.chat_message.content
            try:
                plan_data = json.loads(content)
                plan = DAGPlan.from_task_list(plan_data) if isinstance(plan_data, list) else None
                if plan is not None:
                    recorder.record_plan_output(plan)
                    logger.info("[PlanAgent] 已记录DAG规划: %d个任务", len(plan.tasks))
            except (json.JSONDecodeError, TypeError):
                logger.debug("[PlanAgent] 输出非JSON格式，跳过trace记录")

        return None

    async def on_messages_stream(
            self,
            messages: Sequence[BaseChatMessage],
            cancellation_token: CancellationToken,
    ) -> AsyncGenerator[BaseAgentEvent | BaseChatMessage | Response | StreamEvent, None]:
        """重写消息流：拦截 ask_user tool call，yield USER_QUESTION/USER_ANSWER。

        流程：
        1. 注入多轮对话历史（on_before_turn）
        2. AGENT_START 事件
        3. 调用 AssistantAgent.on_messages_stream（AutoGen 处理 LLM + tool calling）
        4. 拦截 ToolCallRequestEvent(name=ask_user)：
           - 翻译为 TOOL_CALL
           - 创建 UserQuestion → yield USER_QUESTION
           - await handler.wait_for_answer() 阻塞等待用户答复
           - yield USER_ANSWER → 将答复放入 asyncio.Queue
        5. AutoGen 执行 tool function（从 queue 取值）→ 喂回 LLM → LLM 输出 DAG plan
        6. 翻译 Response → AGENT_END
        7. finally: 清理 handler
        """
        set_current_agent("PlanAgent")
        set_enable_thinking(self.thinking_enabled)
        translator = self._translator
        handler = self._ask_user_handler
        session_id = self._session.session_id if self._session else ""

        # 1. 注入多轮对话历史
        if isinstance(messages, list):
            await self.on_before_turn(messages)

        # 2. AGENT_START
        if translator is not None:
            yield translator.make_agent_start("PlanAgent", "PlanAgent", "plan", "任务规划+需求澄清")
            translator._current_agent = "PlanAgent"
            translator._agent_start_time = datetime.now()

        try:
            content = ""
            had_error = False
            async for event in AssistantAgent.on_messages_stream(self, messages, cancellation_token):
                if isinstance(event, ToolCallRequestEvent):
                    # 翻译为 TOOL_CALL
                    if translator is not None:
                        for sev in translator.translate(event):
                            yield sev

                    # 拦截 ask_user tool call
                    for tc in event.content:
                        if tc.name == "ask_user":
                            try:
                                args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
                            except (json.JSONDecodeError, TypeError):
                                args = {"question": str(tc.arguments)}

                            question_text = str(args.get("question", "")).strip()
                            if not question_text or not handler.can_ask():
                                # 无法提问，跳过
                                await self._answer_queue.put("[无法提问，请直接输出DAG计划]")
                                continue

                            qtype_str = args.get("question_type", "open")
                            try:
                                question_type = QuestionType(qtype_str)
                            except ValueError:
                                question_type = QuestionType.OPEN
                            options = args.get("options")
                            context_str = args.get("context", "")
                            default = args.get("default")

                            uq = handler.create_question(
                                question=question_text,
                                question_type=question_type,
                                options=options if isinstance(options, list) else None,
                                context=context_str,
                                default=default,
                            )

                            # USER_QUESTION
                            if translator is not None:
                                yield translator._make_event(
                                    StreamEventType.USER_QUESTION,
                                    question_text,
                                    uq.to_sse_data(),
                                )

                            # 等待用户答复
                            answer = await handler.wait_for_answer(uq.question_id)

                            # USER_ANSWER
                            if translator is not None:
                                yield translator._make_event(
                                    StreamEventType.USER_ANSWER,
                                    f"用户答复: {answer[:200]}",
                                    {
                                        "question_id": uq.question_id,
                                        "answer": answer,
                                        "answer_type": AnswerType.TEXT.value,
                                    },
                                )

                            # 将答复喂给 tool function（AutoGen 即将执行）
                            await self._answer_queue.put(answer)

                elif isinstance(event, ToolCallExecutionEvent):
                    # tool 结果翻译为 TOOL_RESULT
                    if translator is not None:
                        for sev in translator.translate(event):
                            yield sev

                elif isinstance(event, Response):
                    if event.chat_message:
                        msg = event.chat_message
                        if isinstance(msg.content, str):
                            content = msg.content
                        elif hasattr(msg.content, "model_dump_json"):
                            content = msg.content.model_dump_json()
                    if translator is not None:
                        for sev in translator.translate(event):
                            yield sev

                else:
                    # 其他事件（streaming chunks 等）
                    if translator is not None:
                        for sev in translator.translate(event):
                            yield sev

            self._final_content = content

        except Exception as e:
            had_error = True
            logger.error("[PlanAgent] 执行失败: %s", e, exc_info=True)
            if translator is not None:
                yield translator.make_error(f"PlanAgent执行失败: {e}", error_type="PlanAgentError", message=str(e))
        finally:
            handler.cancel_pending()
            if session_id:
                ask_user_registry.unregister(session_id)
            if translator is not None:
                end_duration_ms = None
                if translator._agent_start_time:
                    end_duration_ms = int((datetime.now() - translator._agent_start_time).total_seconds() * 1000)
                yield translator.make_agent_end(
                    "PlanAgent", "plan", "PlanAgent 执行完成",
                    status="failed" if had_error else "success", duration_ms=end_duration_ms,
                )
                translator._current_agent = ""
                translator._agent_start_time = None