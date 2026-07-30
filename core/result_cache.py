
"""结果级缓存：精确匹配 + 语义相似度两层缓存。

集成点：DispatchLayer.run_stream() 在路由前调用 lookup()，
在执行完成后调用 store()。
"""
from __future__ import annotations

import hashlib
import logging

from config.settings import settings
from core.semantic_cache import SemanticCache
from models.cache import CacheLookupResult, ResultCacheEntry
from utils.cache import AsyncTTLCache

logger = logging.getLogger(__name__)


class ResultCache:
    """结果级缓存门面：精确缓存 → 语义缓存两层查找。"""

    def __init__(self) -> None:
        self._exact_cache = AsyncTTLCache(
            maxsize=settings.result_cache_maxsize,
            ttl=settings.result_cache_ttl,
        )
        self._semantic_cache = SemanticCache(
            maxsize=settings.semantic_cache_maxsize,
            ttl=settings.semantic_cache_ttl,
            similarity_threshold=settings.semantic_cache_threshold,
            model_name=settings.semantic_cache_model,
        )
        self._enabled = settings.result_cache_enabled
        self._semantic_enabled = settings.semantic_cache_enabled

    @staticmethod
    def _cache_key(query: str, source_site: str) -> str:
        """生成缓存键：hash(source_site:query)。"""
        raw = f"{source_site}:{query}"
        return hashlib.md5(raw.encode()).hexdigest()

    async def lookup(self, query: str, source_site: str = "les_portal") -> CacheLookupResult:
        """两层缓存查找：精确 → 语义。"""
        if not self._enabled:
            return CacheLookupResult()

        cache_key = self._cache_key(query, source_site)

        # Layer 1: 精确缓存
        entry = await self._exact_cache.get(cache_key)
        if entry is not None:
            entry.hit_count += 1
            logger.info("[ResultCache] 精确缓存命中: query=%s, hits=%d", query[:50], entry.hit_count)
            return CacheLookupResult(hit=True, entry=entry, source="exact")

        # Layer 2: 语义缓存
        if self._semantic_enabled:
            entry, similarity = await self._semantic_cache.search(query, source_site)
            if entry is not None:
                entry.hit_count += 1
                logger.info("[ResultCache] 语义缓存命中: query=%s, similarity=%.3f", query[:50], similarity)
                return CacheLookupResult(hit=True, entry=entry, source="semantic", similarity=similarity)

        logger.debug("[ResultCache] 缓存未命中: query=%s", query[:50])
        return CacheLookupResult()

    async def store(self, query: str, source_site: str, entry: ResultCacheEntry) -> None:
        """存储到精确缓存 + 语义缓存。"""
        if not self._enabled:
            return

        cache_key = self._cache_key(query, source_site)

        # 写入精确缓存
        await self._exact_cache.put(cache_key, entry)

        # 写入语义缓存
        if self._semantic_enabled:
            await self._semantic_cache.put(query, source_site, entry)

        logger.info("[ResultCache] 缓存已存储: query=%s, events=%d", query[:50], len(entry.events))

    async def invalidate(self, query: str, source_site: str = "les_portal") -> None:
        """删除指定查询的缓存。"""
        cache_key = self._cache_key(query, source_site)
        await self._exact_cache.invalidate(cache_key)
        await self._semantic_cache.remove(query, source_site)

    async def clear(self) -> None:
        """清空所有缓存。"""
        await self._exact_cache.clear()
        await self._semantic_cache.clear()

    async def cleanup(self) -> tuple[int, int]:
        """清理过期条目，返回 (exact_removed, semantic_removed)。"""
        exact_removed = await self._exact_cache.cleanup()
        semantic_removed = await self._semantic_cache.cleanup()
        return exact_removed, semantic_removed

    async def shutdown(self) -> None:
        """释放资源（嵌入模型）。"""
        await self._semantic_cache.shutdown()


# 全局单例
result_cache = ResultCache()