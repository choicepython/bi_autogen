
"""TTL 缓存工具：支持过期时间的内存缓存。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict
from threading import Lock
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


class TTLCache:
    """线程安全的 TTL 内存缓存。

    用法::

        cache = TTLCache(maxsize=100, ttl=300)  # 最多100条，5分钟过期

        # 写入
        cache.put("key", value)

        # 读取
        value = cache.get("key")  # 过期返回 None

        # 带默认值的获取
        value = cache.get("key", default=[])
    """

    def __init__(self, maxsize: int = 100, ttl: float = 300.0) -> None:
        """初始化 TTL 缓存。

        Args:
            maxsize: 最大缓存条目数，超过时淘汰最早写入的。
            ttl: 缓存过期时间（秒），默认5分钟。
        """
        self._maxsize = maxsize
        self._ttl = ttl
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = Lock()

    def get(self, key: str, default: Any = None) -> Any:
        """获取缓存值，过期或不存在则返回 default。"""
        with self._lock:
            if key not in self._cache:
                return default
            value, expire_at = self._cache[key]
            if time.monotonic() > expire_at:
                del self._cache[key]
                return default
            # 命中时移到末尾（LRU）
            self._cache.move_to_end(key)
            return value

    def put(self, key: str, value: Any) -> None:
        """写入缓存，如果 key 已存在则更新。"""
        with self._lock:
            expire_at = time.monotonic() + self._ttl
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (value, expire_at)
            # 淘汰超出容量的最早条目
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    def invalidate(self, key: str) -> None:
        """删除指定 key 的缓存。"""
        with self._lock:
            self._cache.pop(key, None)

    def clear(self) -> None:
        """清空所有缓存。"""
        with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        """当前缓存条目数。"""
        with self._lock:
            return len(self._cache)

    def cleanup(self) -> int:
        """清理所有过期条目，返回清理数量。"""
        now = time.monotonic()
        expired = 0
        with self._lock:
            keys_to_remove = [k for k, (_, expire_at) in self._cache.items() if now > expire_at]
            for k in keys_to_remove:
                del self._cache[k]
                expired += 1
        if expired:
            logger.debug("[TTLCache] 清理 %d 条过期缓存", expired)
        return expired


class AsyncTTLCache:
    """异步安全的 TTL 内存缓存。使用 asyncio.Lock 替代 threading.Lock。

    用法::

        cache = AsyncTTLCache(maxsize=100, ttl=300)

        # 写入
        await cache.put("key", value)

        # 读取
        value = await cache.get("key")  # 过期返回 None

        # 清理过期
        removed = await cache.cleanup()
    """

    def __init__(self, maxsize: int = 100, ttl: float = 300.0) -> None:
        """初始化异步 TTL 缓存。

        Args:
            maxsize: 最大缓存条目数，超过时淘汰最早写入的。
            ttl: 缓存过期时间（秒），默认5分钟。
        """
        self._maxsize = maxsize
        self._ttl = ttl
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._lock = asyncio.Lock()

    async def get(self, key: str, default: Any = None) -> Any:
        """获取缓存值，过期或不存在则返回 default。"""
        async with self._lock:
            if key not in self._cache:
                return default
            value, expire_at = self._cache[key]
            if time.monotonic() > expire_at:
                del self._cache[key]
                return default
            # 命中时移到末尾（LRU）
            self._cache.move_to_end(key)
            return value

    async def put(self, key: str, value: Any) -> None:
        """写入缓存，如果 key 已存在则更新。"""
        async with self._lock:
            expire_at = time.monotonic() + self._ttl
            if key in self._cache:
                self._cache.move_to_end(key)
            self._cache[key] = (value, expire_at)
            # 淘汰超出容量的最早条目
            while len(self._cache) > self._maxsize:
                self._cache.popitem(last=False)

    async def invalidate(self, key: str) -> None:
        """删除指定 key 的缓存。"""
        async with self._lock:
            self._cache.pop(key, None)

    async def clear(self) -> None:
        """清空所有缓存。"""
        async with self._lock:
            self._cache.clear()

    @property
    def size(self) -> int:
        """当前缓存条目数。"""
        return len(self._cache)

    async def cleanup(self) -> int:
        """清理所有过期条目，返回清理数量。"""
        now = time.monotonic()
        expired = 0
        async with self._lock:
            keys_to_remove = [k for k, (_, expire_at) in self._cache.items() if now > expire_at]
            for k in keys_to_remove:
                del self._cache[k]
                expired += 1
        if expired:
            logger.debug("[AsyncTTLCache] 清理 %d 条过期缓存", expired)
        return expired