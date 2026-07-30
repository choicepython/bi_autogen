
"""多轮对话上下文存储。

提供 ConversationStore 抽象接口和 InMemoryConversationStore 内存实现。
DispatchLayer 在每轮对话开始时加载历史，结束时存储 TurnSummary。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

from models.conversation import DataRef, TurnSummary

logger = logging.getLogger(__name__)

# 内存存储默认限制
_MAX_SESSIONS = 1000
_SESSION_TTL = 7200.0  # 秒，2小时


class ConversationStore(ABC):
    """多轮对话上下文存储接口。"""

    @abstractmethod
    async def get_history(self, session_id: str) -> list[TurnSummary]:
        """获取指定 session 的历史轮次摘要。"""

    @abstractmethod
    async def append_turn(self, session_id: str, turn: TurnSummary) -> None:
        """追加一轮对话摘要。"""

    @abstractmethod
    async def get_data_catalog(self, session_id: str) -> list[DataRef]:
        """获取指定 session 的数据目录。"""

    @abstractmethod
    async def update_data_catalog(self, session_id: str, refs: list[DataRef]) -> None:
        """更新数据目录（增量合并：已有 key 更新，新 key 追加）。"""

    @abstractmethod
    async def remove_session(self, session_id: str) -> None:
        """移除指定 session 的所有数据。"""


class InMemoryConversationStore(ConversationStore):
    """基于内存的对话存储实现，适用于单进程场景。

    支持 TTL 过期淘汰和最大会话数限制，防止内存无限增长。
    """

    def __init__(self, max_sessions: int = _MAX_SESSIONS, session_ttl: float = _SESSION_TTL) -> None:
        self._store: dict[str, _ConversationState] = {}
        self._access_time: dict[str, float] = {}
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl
        self._lock = asyncio.Lock()

    def _evict_expired(self) -> None:
        """淘汰过期会话。"""
        now = time.monotonic()
        expired = [sid for sid, t in self._access_time.items() if now - t > self._session_ttl]
        for sid in expired:
            del self._store[sid]
            del self._access_time[sid]
        if expired:
            logger.debug("[ConversationStore] 淘汰 %d 个过期会话", len(expired))

    def _evict_oldest(self) -> None:
        """淘汰最久未访问的会话，直到不超过 max_sessions。"""
        while len(self._store) > self._max_sessions:
            oldest = min(self._access_time, key=self._access_time.get)
            del self._store[oldest]
            del self._access_time[oldest]
            logger.debug("[ConversationStore] 淘汰最旧会话: %s", oldest)

    def _get_or_create(self, session_id: str) -> _ConversationState:
        if session_id not in self._store:
            self._store[session_id] = _ConversationState()
        self._access_time[session_id] = time.monotonic()
        return self._store[session_id]

    async def get_history(self, session_id: str) -> list[TurnSummary]:
        async with self._lock:
            self._evict_expired()
            state = self._store.get(session_id)
            if state:
                self._access_time[session_id] = time.monotonic()
            return list(state.turns) if state else []

    async def append_turn(self, session_id: str, turn: TurnSummary) -> None:
        async with self._lock:
            self._evict_expired()
            state = self._get_or_create(session_id)
            state.turns.append(turn)
            self._evict_oldest()
            logger.debug("[ConversationStore] session=%s 追加第%d轮", session_id, turn.turn_id)

    async def get_data_catalog(self, session_id: str) -> list[DataRef]:
        async with self._lock:
            state = self._store.get(session_id)
            return list(state.data_catalog) if state else []

    async def update_data_catalog(self, session_id: str, refs: list[DataRef]) -> None:
        async with self._lock:
            state = self._get_or_create(session_id)
            # 增量合并：已有 key 更新，新 key 追加
            existing_keys = {d.key for d in state.data_catalog}
            for ref in refs:
                if ref.key in existing_keys:
                    for i, d in enumerate(state.data_catalog):
                        if d.key == ref.key:
                            state.data_catalog[i] = ref
                            break
                else:
                    state.data_catalog.append(ref)
            logger.debug(
                "[ConversationStore] session=%s 更新数据目录: %d条",
                session_id, len(refs),
            )

    async def remove_session(self, session_id: str) -> None:
        async with self._lock:
            self._store.pop(session_id, None)
            self._access_time.pop(session_id, None)


class _ConversationState:
    """单个 session 的对话状态。"""

    __slots__ = ("turns", "data_catalog")

    def __init__(self) -> None:
        self.turns: list[TurnSummary] = []
        self.data_catalog: list[DataRef] = []