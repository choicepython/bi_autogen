
"""用户答复端点：接收用户对 ask_user 的回复。

POST /api/v1/chat/{session_id}/answer
Body: {"question_id": str, "answer": str, "answer_type": "text"|"cancel"}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from models.user_question import AnswerType

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])


@router.post("/api/v1/chat/{session_id}/answer", summary="提交用户答复", tags=["chat"])
async def submit_answer(session_id: str, request: Request) -> JSONResponse:
    """提交用户对 ask_user 提问的答复。

    前端在收到 USER_QUESTION SSE 事件后，通过此端点提交答复。
    PlanAgent 收到答复后继续 DAG 规划。

    Body:
        {
            "question_id": "abc123",
            "answer": "工厂A",
            "answer_type": "text" | "cancel"  # 可选，默认 text
        }
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": "请求体必须是 JSON"})

    question_id = body.get("question_id", "")
    answer = body.get("answer", "")
    answer_type_str = body.get("answer_type", "text")

    if not question_id:
        return JSONResponse(status_code=400, content={"error": "question_id 不能为空"})
    if not answer and answer_type_str != "cancel":
        return JSONResponse(status_code=400, content={"error": "answer 不能为空"})

    try:
        answer_type = AnswerType(answer_type_str)
    except ValueError:
        answer_type = AnswerType.TEXT

    # 通过泛化 AskUserRegistry 找到活跃会话的 handler
    # （PlanAgent pre-plan 澄清阶段注册到此）
    from core.ask_user_registry import ask_user_registry

    handler = ask_user_registry.get(session_id)
    if handler is None:
        logger.warning("[Gateway] 未找到 session=%s 的活跃 ask_user handler", session_id)
        return JSONResponse(
            status_code=404,
            content={
                "error": "会话不存在或已结束",
                "session_id": session_id,
            },
        )

    # 提交答复
    success = handler.submit_answer(question_id, answer, answer_type)
    if not success:
        return JSONResponse(
            status_code=409,
            content={
                "error": "问题已失效或 question_id 不匹配",
                "question_id": question_id,
            },
        )

    logger.info(
        "[Gateway] 收到用户答复: session=%s, question=%s, type=%s",
        session_id, question_id, answer_type.value,
    )

    return JSONResponse(
        status_code=200,
        content={
            "received": True,
            "question_id": question_id,
            "answer_type": answer_type.value,
        },
    )