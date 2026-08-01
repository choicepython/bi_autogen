
"""Redis 版本的 DataContextCache。

当 settings.redis_url 有值时，由 store_factory 创建并注入 DispatchLayer。
所有操作 best-effort：Redis 异常时 log warning 不 raise，不影响主流程。

Key 方案：
- bi:dc:{session_id}:keys     → Redis SET，记录该 session 的所有 DataFrame key
- bi:dc:{session_id}:{key}    → Redis STRING，值为 df.to_json(orient="records") 的 UTF-8 bytes
- 所有 key 均设 TTL

DataFrame 序列化用 to_json/read_json（安全，无代码执行风险，无额外依赖）。
"""

from __future__ import annotations

import logging
from io import StringIO

import pandas as pd
import redis.asyncio as aioredis

from core.data_context import DataContext
from core.data_context_cache import DataContextCache

logger = logging.getLogger(__name__)

_SESSION_TTL = 7200  # 秒，2 小时


class RedisDataContextCache(DataContextCache):
    """基于 Redis 的 DataContext 缓存。

    所有操作 best-effort：Redis 异常时 log warning 不 raise。
    """

    def __init__(self, client: aioredis.Redis, session_ttl: int = _SESSION_TTL) -> None:
        self._client = client
        self._ttl = session_ttl

    @staticmethod
    def _keys_set_key(session_id: str) -> str:
        return f"bi:dc:{session_id}:keys"

    @staticmethod
    def _df_key(session_id: str, key: str) -> str:
        return f"bi:dc:{session_id}:{key}"

    async def save(self, session_id: str, data_context: DataContext) -> int:
        """保存 DataContext 中所有 DataFrame 到 Redis。返回保存的 key 数量。"""
        try:
            keys_set = self._keys_set_key(session_id)
            saved = 0
            pipe = self._client.pipeline()
            for key in data_context.list_keys():
                df = data_context.get(key)
                if df is not None and not df.empty:
                    json_bytes = df.to_json(orient="records").encode("utf-8")
                    pipe.set(self._df_key(session_id, key), json_bytes, ex=self._ttl)
                    pipe.sadd(keys_set, key)
                    saved += 1
            pipe.expire(keys_set, self._ttl)
            await pipe.execute()
            logger.info("[RedisDataContextCache] session=%s 保存 %d 个 DataFrame", session_id, saved)
            return saved
        except Exception as e:
            logger.warning("[RedisDataContextCache] save 失败: %s", e)
            return 0

    async def restore(self, session_id: str, data_context: DataContext) -> int:
        """从 Redis 恢复 DataFrame 到 DataContext（仅恢复不存在的 key）。"""
        try:
            keys_set = self._keys_set_key(session_id)
            cached_keys = await self._client.smembers(keys_set)
            if not cached_keys:
                return 0

            existing_keys = set(data_context.list_keys())
            restored = 0
            for key_bytes in cached_keys:
                key = key_bytes.decode("utf-8") if isinstance(key_bytes, bytes) else key_bytes
                if key in existing_keys:
                    continue
                raw = await self._client.get(self._df_key(session_id, key))
                if raw is None:
                    continue
                json_str = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                df = pd.read_json(StringIO(json_str), orient="records")
                await data_context.put(key, df)
                restored += 1

            logger.info(
                "[RedisDataContextCache] session=%s 恢复 %d 个 DataFrame（跳过 %d 个已存在）",
                session_id, restored, len(cached_keys) - restored,
            )
            return restored
        except Exception as e:
            logger.warning("[RedisDataContextCache] restore 失败: %s", e)
            return 0

    async def clear(self, session_id: str) -> None:
        """清除指定 session 的缓存。"""
        try:
            keys_set = self._keys_set_key(session_id)
            cached_keys = await self._client.smembers(keys_set)
            if not cached_keys:
                return
            pipe = self._client.pipeline()
            for key_bytes in cached_keys:
                key = key_bytes.decode("utf-8") if isinstance(key_bytes, bytes) else key_bytes
                pipe.delete(self._df_key(session_id, key))
            pipe.delete(keys_set)
            await pipe.execute()
            logger.info("[RedisDataContextCache] session=%s 缓存已清除", session_id)
        except Exception as e:
            logger.warning("[RedisDataContextCache] clear 失败: %s", e)

    async def list_cached_keys(self, session_id: str) -> list[str]:
        """列出指定 session 缓存中的所有 key。"""
        try:
            keys_set = self._keys_set_key(session_id)
            raw_keys = await self._client.smembers(keys_set)
            return [
                k.decode("utf-8") if isinstance(k, bytes) else k
                for k in raw_keys
            ]
        except Exception as e:
            logger.warning("[RedisDataContextCache] list_cached_keys 失败: %s", e)
            return []

    async def has_data(self, session_id: str) -> bool:
        """检查指定 session 是否有缓存数据。"""
        try:
            count = await self._client.scard(self._keys_set_key(session_id))
            return count > 0
        except Exception as e:
            logger.warning("[RedisDataContextCache] has_data 失败: %s", e)
            return False

    async def remove_session(self, session_id: str) -> None:
        """移除指定 session 的所有缓存数据。"""
        await self.clear(session_id)