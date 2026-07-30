
"""ask_user 工具的数据模型。

定义 UserQuestion（向用户提问）和 UserAnswer（用户答复）的 Pydantic 模型。
用于 PlanAgent 在需求不明确时主动向用户澄清。
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class QuestionType(str, Enum):
    """提问类型。"""

    OPEN = "open"                # 开放式问答
    CHOICE = "choice"            # 单选
    MULTI_CHOICE = "multi_choice"  # 多选
    CONFIRM = "confirm"           # 是/否确认


class AnswerType(str, Enum):
    """答复类型。"""

    TEXT = "text"        # 正常文本回复
    CANCEL = "cancel"    # 用户主动取消
    TIMEOUT = "timeout"  # 超时未回复
    DEFAULT = "default"  # 使用默认值


class UserQuestion(BaseModel):
    """向用户提出的问题。"""

    question_id: str = Field(description="问题唯一ID，用于匹配答复")
    question: str = Field(description="提问内容，必须具体明确")
    question_type: QuestionType = Field(default=QuestionType.OPEN, description="提问类型")
    options: list[str] | None = Field(default=None, description="选项列表（choice/multi_choice 时必填）")
    context: str = Field(default="", description="上下文说明：为什么问这个问题")
    default: str | None = Field(default=None, description="默认值（用户拒绝/超时时使用）")
    timeout_seconds: int = Field(default=300, description="超时秒数，默认5分钟")
    ask_count: int = Field(default=1, description="当前会话第几次提问")
    max_asks: int = Field(default=3, description="单会话最大提问次数")
    created_at: str = Field(default="", description="创建时间 ISO 格式")

    def to_sse_data(self) -> dict[str, Any]:
        """转为 SSE 事件的 data 字段。"""
        return {
            "question_id": self.question_id,
            "question_type": self.question_type.value,
            "options": self.options,
            "context": self.context,
            "default": self.default,
            "timeout_seconds": self.timeout_seconds,
            "ask_count": self.ask_count,
            "max_asks": self.max_asks,
        }


class UserAnswer(BaseModel):
    """用户对问题的答复。"""

    question_id: str = Field(description="对应 UserQuestion.question_id")
    answer: str = Field(description="用户答复内容")
    answer_type: AnswerType = Field(default=AnswerType.TEXT, description="答复类型")
    received_at: str = Field(default="", description="收到时间 ISO 格式")