
"""泛化的 ask_user 会话注册表。

session_id → AskUserHandler 映射，供 Gateway 提交用户答复。

PlanAgent 的 ask_user 澄清阶段将 AskUserHandler 注册到此处，
Gateway /answer 端点统一通过本注册表查找。
"""

from __future__ import annotations

import logging
from typing import Any

from tools.ask_user import AskUserHandler

logger = logging.getLogger(__name__)


class AskUserRegistry:
    """session_id → AskUserHandler 映射。"""

    def __init__(self) -> None:
        self._handlers: dict[str, AskUserHandler] = {}

    def register(self, session_id: str, handler: AskUserHandler) -> None:
        self._handlers[session_id] = handler
        logger.debug("[AskUserRegistry] 注册 session=%s", session_id)

    def unregister(self, session_id: str) -> None:
        self._handlers.pop(session_id, None)
        logger.debug("[AskUserRegistry] 注销 session=%s", session_id)

    def get(self, session_id: str) -> AskUserHandler | None:
        return self._handlers.get(session_id)

    def submit_answer(self, session_id: str, question_id: str, answer: str, answer_type: Any = None) -> bool:
        """提交用户答复。返回 True 表示成功匹配。

        answer_type 默认由调用方处理；此处保持与 AskUserHandler.submit_answer 一致。
        """
        from models.user_question import AnswerType

        handler = self._handlers.get(session_id)
        if handler is None:
            return False
        at = answer_type if answer_type is not None else AnswerType.TEXT
        return handler.submit_answer(question_id, answer, at)


# 全局单例
ask_user_registry = AskUserRegistry()