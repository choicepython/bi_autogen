
"""语义缓存：基于 sentence-transformers 嵌入 + numpy 余弦相似度。

嵌入模型懒加载：首次查询时加载 all-MiniLM-L6-v2。
存储结构：numpy 矩阵存储嵌入向量，OrderedDict 存储缓存条目。
淘汰策略：LRU + 过期清理。
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any

import numpy as np

from models.cache import ResultCacheEntry

logger = logging.getLogger(__name__)


class SemanticCache:
    """语义缓存：嵌入向量 + 余弦相似度匹配。"""

    def __init__(
        self,
        maxsize: int = 100,
        ttl: float = 900.0,
        similarity_threshold: float = 0.92,
        model_name: str = "all-MiniLM-L6-v2",
    ) -> None:
        self._maxsize = maxsize
        self._ttl = ttl
        self._similarity_threshold = similarity_threshold
        self._model_name = model_name
        self._lock = asyncio.Lock()

        # 存储：key → (embedding_ndarray, entry, expire_at)
        self._store: OrderedDict[str, tuple[np.ndarray, ResultCacheEntry, float]] = OrderedDict()

        # 嵌入矩阵：用于批量余弦相似度计算
        self._embedding_matrix: np.ndarray | None = None  # shape (N, dim)
        self._key_order: list[str] = []  # 与 embedding_matrix 行对齐

        # 嵌入模型（懒加载）
        self._model: Any = None
        self._model_lock = asyncio.Lock()
        self._model_loaded = False

    async def _ensure_model(self) -> None:
        """懒加载 sentence-transformers 模型。"""
        if self._model_loaded:
            return
        async with self._model_lock:
            if self._model_loaded:
                return
            from sentence_transformers import SentenceTransformer

            loop = asyncio.get_running_loop()
            self._model = await loop.run_in_executor(None, SentenceTransformer, self._model_name)
            self._model_loaded = True
            logger.info("[SemanticCache] 嵌入模型已加载: %s", self._model_name)

    async def _embed(self, text: str) -> np.ndarray:
        """生成文本嵌入向量。"""
        await self._ensure_model()
        loop = asyncio.get_running_loop()
        embedding = await loop.run_in_executor(None, self._model.encode, text)
        return embedding.astype(np.float32)

    @staticmethod
    def _entry_key(query: str, source_site: str) -> str:
        """语义缓存内部键。"""
        raw = f"{source_site}:{query}"
        return hashlib.md5(raw.encode()).hexdigest()

    def _rebuild_matrix(self) -> None:
        """从 _store 重建嵌入矩阵。"""
        if not self._store:
            self._embedding_matrix = None
            self._key_order = []
            return
        self._key_order = list(self._store.keys())
        embeddings = [self._store[k][0] for k in self._key_order]
        self._embedding_matrix = np.stack(embeddings, axis=0)

    async def search(self, query: str, source_site: str = "les_portal") -> tuple[ResultCacheEntry | None, float]:
        """语义搜索：嵌入查询 → 余弦相似度 → 最匹配条目。"""
        if not self._store:
            return None, 0.0

        query_embedding = await self._embed(query)

        async with self._lock:
            if self._embedding_matrix is None or len(self._embedding_matrix) == 0:
                return None, 0.0

            # 批量余弦相似度
            query_norm = query_embedding / (np.linalg.norm(query_embedding) + 1e-8)
            matrix_norms = self._embedding_matrix / (
                np.linalg.norm(self._embedding_matrix, axis=1, keepdims=True) + 1e-8
            )
            similarities = matrix_norms @ query_norm

            best_idx = int(np.argmax(similarities))
            best_similarity = float(similarities[best_idx])

            if best_similarity < self._similarity_threshold:
                return None, best_similarity

            best_key = self._key_order[best_idx]
            _, entry, expire_at = self._store[best_key]

            # 检查过期
            if time.monotonic() > expire_at:
                del self._store[best_key]
                self._rebuild_matrix()
                return None, 0.0

            # LRU: 移到末尾
            self._store.move_to_end(best_key)
            return entry, best_similarity

    async def put(self, query: str, source_site: str, entry: ResultCacheEntry) -> None:
        """存储条目 + 嵌入向量。"""
        embedding = await self._embed(query)
        key = self._entry_key(query, source_site)

        async with self._lock:
            expire_at = time.monotonic() + self._ttl
            if key in self._store:
                self._store.move_to_end(key)
            self._store[key] = (embedding, entry, expire_at)

            # 淘汰超出容量的最早条目
            while len(self._store) > self._maxsize:
                self._store.popitem(last=False)

            self._rebuild_matrix()

    async def remove(self, query: str, source_site: str = "les_portal") -> None:
        """删除指定条目。"""
        key = self._entry_key(query, source_site)
        async with self._lock:
            if key in self._store:
                del self._store[key]
                self._rebuild_matrix()

    async def clear(self) -> None:
        """清空所有条目。"""
        async with self._lock:
            self._store.clear()
            self._embedding_matrix = None
            self._key_order = []

    async def cleanup(self) -> int:
        """清理过期条目。"""
        now = time.monotonic()
        expired = 0
        async with self._lock:
            keys_to_remove = [k for k, (_, _, expire_at) in self._store.items() if now > expire_at]
            for k in keys_to_remove:
                del self._store[k]
                expired += 1
            if expired:
                self._rebuild_matrix()
        if expired:
            logger.debug("[SemanticCache] 清理 %d 条过期缓存", expired)
        return expired

    async def shutdown(self) -> None:
        """释放嵌入模型资源。"""
        async with self._model_lock:
            if self._model is not None:
                del self._model
                self._model = None
                self._model_loaded = False
                logger.info("[SemanticCache] 嵌入模型已释放")

    @property
    def size(self) -> int:
        """当前缓存条目数。"""
        return len(self._store)