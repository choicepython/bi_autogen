
"""DataContext 跨轮缓存。

解决多轮对话中 DataContext 被销毁导致下游 Agent 无法访问历史数据的问题。

提供 InMemoryDataContextCache 实现：内存引用，零序列化开销，适合单进程场景。

使用方式：
  DispatchLayer 在 turn 执行后调用 save() 保存 DataFrame，
  在新 turn 开始时调用 restore() 恢复到新 DataContext。
  Agent 执行时 _load_dfs() 能找到历史数据。
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod

import pandas as pd

from core.data_context import DataContext

logger = logging.getLogger(__name__)

# 内存缓存默认限制
_MAX_SESSIONS = 500
_SESSION_TTL = 7200.0  # 秒，2小时


class DataContextCache(ABC):
    """DataContext 跨轮缓存接口。"""

    @abstractmethod
    async def save(self, session_id: str, data_context: DataContext) -> int:
        """保存 DataContext 中所有 DataFrame 到缓存。返回保存的 key 数量。"""

    @abstractmethod
    async def restore(self, session_id: str, data_context: DataContext) -> int:
        """从缓存恢复 DataFrame 到 DataContext。返回恢复的 key 数量。"""

    @abstractmethod
    async def clear(self, session_id: str) -> None:
        """清除指定 session 的缓存。"""

    @abstractmethod
    async def list_cached_keys(self, session_id: str) -> list[str]:
        """列出指定 session 缓存中的所有 key。"""

    @abstractmethod
    async def has_data(self, session_id: str) -> bool:
        """检查指定 session 是否有缓存数据。"""

    @abstractmethod
    async def remove_session(self, session_id: str) -> None:
        """移除指定 session 的所有缓存数据。"""


class InMemoryDataContextCache(DataContextCache):
    """基于内存的 DataContext 缓存。

    直接保留 DataFrame 对象的深拷贝，防止 aliasing。
    支持 TTL 过期淘汰和最大会话数限制，防止内存无限增长。
    适合单进程场景，进程重启后数据丢失。
    """

    def __init__(
        self,
        max_keys_per_session: int = 50,
        max_sessions: int = _MAX_SESSIONS,
        session_ttl: float = _SESSION_TTL,
    ) -> None:
        self._cache: dict[str, dict[str, pd.DataFrame]] = {}
        self._access_time: dict[str, float] = {}
        self._max_keys = max_keys_per_session
        self._max_sessions = max_sessions
        self._session_ttl = session_ttl
        self._lock = asyncio.Lock()

    def _evict_expired(self) -> None:
        """淘汰过期会话。"""
        now = time.monotonic()
        expired = [sid for sid, t in self._access_time.items() if now - t > self._session_ttl]
        for sid in expired:
            del self._cache[sid]
            del self._access_time[sid]
        if expired:
            logger.debug("[DataContextCache] 淘汰 %d 个过期会话", len(expired))

    def _evict_oldest(self) -> None:
        """淘汰最久未访问的会话，直到不超过 max_sessions。"""
        while len(self._cache) > self._max_sessions:
            oldest = min(self._access_time, key=self._access_time.get)
            del self._cache[oldest]
            del self._access_time[oldest]
            logger.debug("[DataContextCache] 淘汰最旧会话: %s", oldest)

    def _get_or_create(self, session_id: str) -> dict[str, pd.DataFrame]:
        if session_id not in self._cache:
            self._cache[session_id] = {}
        self._access_time[session_id] = time.monotonic()
        return self._cache[session_id]

    async def save(self, session_id: str, data_context: DataContext) -> int:
        """保存 DataContext 中所有 DataFrame 到缓存（深拷贝）。

        已有 key 会更新（DataFrame 可能被下游 Agent 修改过），
        新 key 会追加。超过 max_keys 时淘汰最早写入的 key。
        """
        async with self._lock:
            self._evict_expired()
            store = self._get_or_create(session_id)
            saved = 0
            for key in data_context.list_keys():
                df = data_context.get(key)
                if df is not None and not df.empty:
                    store[key] = df.copy()  # 深拷贝，防止 aliasing
                    saved += 1

            # 淘汰超出限制的旧 key
            if len(store) > self._max_keys:
                excess = len(store) - self._max_keys
                oldest_keys = list(store.keys())[:excess]
                for k in oldest_keys:
                    del store[k]
                logger.debug("[DataContextCache] session=%s 淘汰 %d 个旧 key", session_id, excess)

            self._evict_oldest()
            logger.info("[DataContextCache] session=%s 保存 %d 个 DataFrame，缓存共 %d 个",
                         session_id, saved, len(store))
            return saved

    async def restore(self, session_id: str, data_context: DataContext) -> int:
        """从缓存恢复 DataFrame 到 DataContext（深拷贝）。

        只恢复 DataContext 中不存在的 key（不覆盖当前 turn 已有的数据）。
        """
        async with self._lock:
            self._evict_expired()
            store = self._cache.get(session_id)
            if not store:
                return 0
            self._access_time[session_id] = time.monotonic()

        restored = 0
        existing_keys = set(data_context.list_keys())
        for key, df in store.items():
            if key not in existing_keys:
                await data_context.put(key, df.copy())  # 深拷贝，防止 aliasing
                restored += 1

        logger.info("[DataContextCache] session=%s 恢复 %d 个 DataFrame（跳过 %d 个已存在）",
                     session_id, restored, len(store) - restored)
        return restored

    async def clear(self, session_id: str) -> None:
        async with self._lock:
            self._cache.pop(session_id, None)
            self._access_time.pop(session_id, None)
            logger.info("[DataContextCache] session=%s 缓存已清除", session_id)

    async def list_cached_keys(self, session_id: str) -> list[str]:
        async with self._lock:
            store = self._cache.get(session_id)
            return list(store.keys()) if store else []

    async def has_data(self, session_id: str) -> bool:
        async with self._lock:
            store = self._cache.get(session_id)
            return bool(store)

    async def remove_session(self, session_id: str) -> None:
        async with self._lock:
            self._cache.pop(session_id, None)
            self._access_time.pop(session_id, None)