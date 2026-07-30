
"""ask_user 工具：让 PlanAgent 在需求不明确时主动向用户提问。

核心机制：
- AskUserHandler 管理 pending questions（asyncio.Future）
- ask_user 作为 FunctionTool 注册给 LLM（仅用于 schema 生成）
- PlanAgent 拦截 ask_user tool_call，不执行 func，而是：
  1. yield USER_QUESTION 事件
  2. await asyncio.Future() 等待用户答复
  3. yield USER_ANSWER 事件
  4. 将答复作为 tool_result 注入 messages，LLM 继续

Gateway 通过 AskUserRegistry 找到活跃的 AskUserHandler，
调用 submit_answer() 提交用户答复。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime
from typing import Any

from autogen_core.tools import FunctionTool

from models.user_question import AnswerType, QuestionType, UserQuestion

logger = logging.getLogger(__name__)

# 默认超时：5 分钟
DEFAULT_TIMEOUT_SECONDS = 300
# 单会话最大提问次数
DEFAULT_MAX_ASKS = 3


class AskUserHandler:
    """管理 ask_user 的提问/答复状态。

    线程安全：基于 asyncio.Future，同一 event loop 内并发安全。
    跨 session 隔离：每个 PlanAgent 持有独立实例。
    """

    def __init__(
        self,
        *,
        max_asks: int = DEFAULT_MAX_ASKS,
        default_timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._max_asks = max_asks
        self._default_timeout = default_timeout
        self._questions: dict[str, UserQuestion] = {}
        self._answers: dict[str, str] = {}  # 已提交但 future 未创建时的暂存
        self._futures: dict[str, asyncio.Future[str] | None] = {}
        self._ask_count = 0

    @property
    def ask_count(self) -> int:
        return self._ask_count

    @property
    def max_asks(self) -> int:
        return self._max_asks

    @property
    def has_pending(self) -> bool:
        """是否有未答复的问题。"""
        return bool(self._questions)

    def can_ask(self) -> bool:
        """是否还能提问（未超上限）。"""
        return self._ask_count < self._max_asks

    def create_question(
        self,
        question: str,
        *,
        question_type: QuestionType = QuestionType.OPEN,
        options: list[str] | None = None,
        context: str = "",
        default: str | None = None,
        timeout_seconds: int | None = None,
    ) -> UserQuestion:
        """创建一个问题，返回 UserQuestion 对象（不等待答复）。

        调用方负责 yield USER_QUESTION 事件，然后调用 wait_for_answer()。
        Future 延迟到 wait_for_answer() 时创建（避免无 event loop 时报错）。
        """
        self._ask_count += 1
        question_id = uuid.uuid4().hex
        uq = UserQuestion(
            question_id=question_id,
            question=question,
            question_type=question_type,
            options=options,
            context=context,
            default=default,
            timeout_seconds=timeout_seconds or self._default_timeout,
            ask_count=self._ask_count,
            max_asks=self._max_asks,
            created_at=datetime.now().isoformat(),
        )
        self._questions[question_id] = uq
        self._futures[question_id] = None  # 延迟创建
        self._answers[question_id] = ""  # 占位，submit_answer 时写入
        logger.info(
            "[AskUserHandler] 创建问题 #%d: %s (id=%s)",
            self._ask_count, question[:80], question_id,
        )
        return uq

    async def wait_for_answer(self, question_id: str, timeout: int | None = None) -> str:
        """等待用户答复。超时返回 default 或提示文本。

        调用方应先调用 create_question() 并 yield USER_QUESTION 事件。
        """
        uq = self._questions.get(question_id)
        if uq is None:
            logger.warning("[AskUserHandler] 未知 question_id: %s", question_id)
            return "问题已失效"

        # 检查是否已提交答复（submit_answer 在 wait_for_answer 之前调用）
        existing_answer = self._answers.get(question_id, "")
        if existing_answer:
            self._cleanup(question_id)
            return existing_answer

        if timeout is not None:
            effective_timeout = timeout
        else:
            effective_timeout = uq.timeout_seconds if uq else self._default_timeout

        # 创建 Future（此时一定在 async 上下文中，有 running loop）
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        self._futures[question_id] = future

        try:
            answer = await asyncio.wait_for(future, timeout=effective_timeout)
            return answer
        except asyncio.TimeoutError:
            logger.warning("[AskUserHandler] 问题超时: %s", question_id)
            default = uq.default if uq else None
            return default or "用户未在规定时间内回复，已跳过该问题"
        finally:
            self._cleanup(question_id)

    def _cleanup(self, question_id: str) -> None:
        """清理已答复/超时的问题。"""
        self._questions.pop(question_id, None)
        self._futures.pop(question_id, None)
        self._answers.pop(question_id, None)

    def submit_answer(self, question_id: str, answer: str, answer_type: AnswerType = AnswerType.TEXT) -> bool:
        """提交用户答复。返回 True 表示成功匹配，False 表示 question_id 未知或已过期。

        由 Gateway 调用（通过 SessionRegistry 找到 handler）。
        """
        if question_id not in self._questions:
            logger.warning(
                "[AskUserHandler] 收到未知答复: question_id=%s, answer=%s",
                question_id, answer[:100],
            )
            return False

        # 已提交过答复（未消费）→ 拒绝重复提交
        existing = self._answers.get(question_id, "")
        if existing:
            logger.warning(
                "[AskUserHandler] 重复提交: question_id=%s, 已有=%s, 新=%s",
                question_id, existing[:50], answer[:50],
            )
            return False

        # 暂存答复
        self._answers[question_id] = answer

        # 如果 Future 已创建（wait_for_answer 正在等待），解析它
        future = self._futures.get(question_id)
        if future is not None and not future.done():
            future.set_result(answer)
            logger.info(
                "[AskUserHandler] 收到答复（解析 Future）: question_id=%s, answer=%s",
                question_id, answer[:100],
            )
            return True

        # Future 未创建（submit_answer 在 wait_for_answer 之前调用）
        # 答复已暂存，wait_for_answer 会直接读取
        logger.info(
            "[AskUserHandler] 收到答复（暂存）: question_id=%s, type=%s, answer=%s",
            question_id, answer_type.value, answer[:100],
        )
        return True

    def cancel_pending(self) -> None:
        """取消所有 pending questions（会话结束时调用）。"""
        for qid, future in list(self._futures.items()):
            if future is not None and not future.done():
                future.set_result("会话已结束，问题被取消")
        self._questions.clear()
        self._futures.clear()
        self._answers.clear()


# ---------------------------------------------------------------------------
# Queue-based ask_user 工具（PlanAgent 使用）
# ---------------------------------------------------------------------------

_ASK_USER_DESCRIPTION = (
    "向用户提问以澄清需求。仅在API必填参数缺失时调用。"
    "参数：question(提问内容，必须具体明确)，question_type(open/choice/multi_choice/confirm)，"
    "options(选项列表，choice/multi_choice时必填)，context(上下文说明)，default(默认值)。"
    "禁止场景：API不需要的参数、用户已提供的信息、闲聊。"
    "单会话最多调用3次。"
)


def make_ask_user_tool_with_queue(answer_queue: asyncio.Queue[str]) -> FunctionTool:
    """创建 ask_user FunctionTool，从 answer_queue 读取用户答复。

    PlanAgent.on_messages_stream 拦截 ToolCallRequestEvent 后，
    将用户答复 put 到 answer_queue，本函数 get 取值返回给 AutoGen。

    Args:
        answer_queue: 用户答复队列，由 PlanAgent.on_messages_stream 写入。

    Returns:
        注册给 LLM 的 ask_user FunctionTool。
    """

    async def _ask_user_impl(
        question: str,
        question_type: str = "open",
        options: list[str] | None = None,
        context: str = "",
        default: str | None = None,
    ) -> str:
        return await answer_queue.get()

    return FunctionTool(
        func=_ask_user_impl,
        name="ask_user",
        description=_ASK_USER_DESCRIPTION,
    )