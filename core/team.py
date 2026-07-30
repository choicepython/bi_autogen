
"""BI Agent 系统顶层入口。BITeam 作为薄门面，委托给 DispatchLayer。

公共 API 保持不变，main.py 和 FastAPI 无需修改。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncGenerator
from typing import Any

from autogen_core.models import ChatCompletionClient

from core.conversation_store import ConversationStore
from core.data_context_cache import DataContextCache
from core.dispatch import DispatchLayer
from models.chat_request import ChatRequest
from models.stream_event import StreamEvent
from observability.trace import TraceRecorder

logger = logging.getLogger(__name__)


class BITeam:
    """BI Agent 系统薄门面。委托所有工作给 DispatchLayer。

    保持公共 API 不变：
    - run(task) → (result, TraceRecorder)
    - run_stream(task) → AsyncGenerator[StreamEvent]
    - run_stream_sse(task) → AsyncGenerator[str]
    - reset()
    - shutdown() — 优雅关闭
    """

    def __init__(
        self,
        model_client: ChatCompletionClient | None = None,
        selector_client: ChatCompletionClient | None = None,
        conversation_store: ConversationStore | None = None,
        data_context_cache: DataContextCache | None = None,
    ) -> None:
        self._dispatch = DispatchLayer(model_client, selector_client, conversation_store, data_context_cache)
        self._shutdown = False

    @property
    def routing_layer(self):
        """暴露路由层，供外部注册中间件。"""
        return self._dispatch.routing_layer

    @property
    def agent_layer(self):
        """暴露智能体层，供外部扩展。"""
        return self._dispatch.agent_layer

    @property
    def _last_recorder(self) -> TraceRecorder | None:
        """透传 DispatchLayer 的 trace 记录器。"""
        return self._dispatch._last_recorder

    async def run(self, task: str | ChatRequest) -> tuple[str, TraceRecorder]:
        """运行任务，返回(结果文本, trace记录器)。"""
        if self._shutdown:
            raise RuntimeError("BITeam 已关闭，不再接受新请求")
        return await self._dispatch.run(task)

    async def run_stream(self, task: str | ChatRequest, **kwargs: Any) -> AsyncGenerator[StreamEvent, None]:  # type: ignore[misc]
        """流式运行任务，yield StreamEvent 事件。"""
        if self._shutdown:
            raise RuntimeError("BITeam 已关闭，不再接受新请求")
        async for ev in self._dispatch.run_stream(task, **kwargs):
            yield ev

    async def run_stream_sse(self, task: str | ChatRequest, **kwargs: Any) -> AsyncGenerator[str, None]:
        """流式运行任务，yield SSE 格式文本。"""
        if self._shutdown:
            raise RuntimeError("BITeam 已关闭，不再接受新请求")
        async for sse in self._dispatch.run_stream_sse(task, **kwargs):
            yield sse

    async def reset(self) -> None:
        pass

    async def shutdown(self) -> None:
        """优雅关闭：清理 ContextVar、停止 DB writer、释放资源。"""
        if self._shutdown:
            return
        self._shutdown = True
        logger.info("[BITeam] 优雅关闭中...")

        # 清理 ContextVar
        from observability.logging_client import set_chat_id, set_current_agent, set_enable_thinking, set_session_id
        from observability.trace import set_trace_recorder
        set_trace_recorder(None)
        set_session_id("")
        set_chat_id("")
        set_current_agent("")
        set_enable_thinking(False)

        # 停止 DB writer
        try:
            from db.writer import db_writer
            await db_writer.stop()
        except Exception as e:
            logger.warning("[BITeam] DB writer 停止失败: %s", e)

        # 关闭数据库连接池
        try:
            from db import close_pool
            await close_pool()
        except Exception as e:
            logger.warning("[BITeam] 关闭连接池失败: %s", e)

        # 关闭 SQL 连接池
        try:
            from tools.sql_query import close_sql_pool
            await close_sql_pool()
        except Exception as e:
            logger.warning("[BITeam] 关闭SQL连接池失败: %s", e)

        logger.info("[BITeam] 优雅关闭完成")