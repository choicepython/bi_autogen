
"""LLM 后台摘要 — 独立异步任务，不阻塞主流程。

从 SessionManager 提取，修复 get_prompt_manager 未导入 bug。
ContextVar 在 finally 中重置（安全冗余，遵循 P7）。
"""

from __future__ import annotations

import asyncio
import logging

from autogen_core.models import ChatCompletionClient, SystemMessage, UserMessage
from config.prompt_manager import get_prompt_manager
from db.writer import db_writer, make_session_data
from observability.logging_client import set_current_agent, set_enable_thinking

logger = logging.getLogger(__name__)


async def summarize_background(
    model_client: ChatCompletionClient,
    query: str,
    data_summary: str,
    session_id: str,
    chat_id: str,
) -> None:
    """后台异步摘要任务：1 分钟超时，成功后更新 DB 会话记录。

    不阻塞主流程，不 yield 事件。所有异常捕获不抛出（后台任务约定）。
    ContextVar (set_current_agent, set_enable_thinking) 在本函数内设置并在 finally
    中重置，调用方无需感知。

    Args:
        model_client: LLM 客户端（调用方应提前判断非 None）。
        query: 用户原始问题。
        data_summary: DataContext 的 all_summaries() 字符串。
        session_id: 会话 ID。
        chat_id: 对话轮次 ID。
    """
    set_current_agent("SummaryAgent")
    set_enable_thinking(False)

    try:
        system_prompt = get_prompt_manager().render(
            "summary_agent", AGENT_NAME="SummaryAgent", AGENT_ROLE="数据分析总结助手",
        )
        user_prompt = (
            f"请根据以下数据，用简洁的自然语言回答用户问题。\n\n"
            f"用户问题：{query}\n\n"
            f"数据：\n{data_summary}\n\n"
            f"请直接回答，不要重复数据原文。"
        )

        full_content = ""
        async with asyncio.timeout(60):
            async for chunk in model_client.create_stream(
                messages=[
                    SystemMessage(content=system_prompt),
                    UserMessage(content=user_prompt, source="user"),
                ],
            ):
                if isinstance(chunk, str):
                    full_content += chunk
                elif hasattr(chunk, "content") and chunk.content:
                    text = chunk.content
                    if hasattr(text, "model_dump_json"):
                        text = text.model_dump_json()
                    full_content += str(text)

        # 成功：更新 DB 会话记录（partial=True 避免覆盖统计字段）
        if full_content:
            await db_writer.enqueue_session(make_session_data(
                session_id=session_id,
                chat_id=chat_id,
                status="completed",
                result=full_content[:10000],
                partial=True,
            ))
            logger.info("[SummaryAgent] 后台摘要完成: %s", full_content[:100])
    except TimeoutError:
        logger.warning("[SummaryAgent] 后台摘要超时(60s)，跳过")
    except Exception as e:
        logger.error("[SummaryAgent] 后台摘要失败: %s", e, exc_info=True)
    finally:
        # 安全冗余：子任务 ContextVar 副本会随任务结束消亡，但遵循 P7 所有路径清理原则
        set_current_agent("")
        set_enable_thinking(None)