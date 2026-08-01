
"""Redis 版本的 ConversationStore。

当 settings.redis_url 有值时，由 store_factory 创建并注入 DispatchLayer。
所有操作 best-effort：Redis 异常时 log warning 不 raise，不影响主流程。

Key 方案：
- bi:conv:{session_id}:turns   → Redis LIST，每项 TurnSummary.model_dump_json()
- bi:conv:{session_id}:catalog → Redis LIST，每项 DataRef.model_dump_json()
- 两个 key 均设 TTL（session_ttl，默认 7200s）
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from core.conversation_store import ConversationStore
from models.conversation import DataRef, TurnSummary

logger = logging.getLogger(__name__)

_SESSION_TTL = 7200  # 秒，2 小时


class RedisConversationStore(ConversationStore):
    """基于 Redis 的对话存储实现。

    所有操作 best-effort：Redis 异常时 log warning 不 raise。
    """

    def __init__(self, client: aioredis.Redis, session_ttl: int = _SESSION_TTL) -> None:
        self._client = client
        self._ttl = session_ttl

    @staticmethod
    def _turns_key(session_id: str) -> str:
        return f"bi:conv:{session_id}:turns"

    @staticmethod
    def _catalog_key(session_id: str) -> str:
        return f"bi:conv:{session_id}:catalog"

    async def get_history(self, session_id: str) -> list[TurnSummary]:
        """获取指定 session 的历史轮次摘要。"""
        try:
            raw_list = await self._client.lrange(self._turns_key(session_id), 0, -1)
            return [TurnSummary.model_validate_json(item) for item in raw_list]
        except Exception as e:
            logger.warning("[RedisConversationStore] get_history 失败: %s", e)
            return []

    async def append_turn(self, session_id: str, turn: TurnSummary) -> None:
        """追加一轮对话摘要。"""
        try:
            key = self._turns_key(session_id)
            await self._client.rpush(key, turn.model_dump_json())
            await self._client.expire(key, self._ttl)
        except Exception as e:
            logger.warning("[RedisConversationStore] append_turn 失败: %s", e)

    async def get_data_catalog(self, session_id: str) -> list[DataRef]:
        """获取指定 session 的数据目录。"""
        try:
            raw_list = await self._client.lrange(self._catalog_key(session_id), 0, -1)
            return [DataRef.model_validate_json(item) for item in raw_list]
        except Exception as e:
            logger.warning("[RedisConversationStore] get_data_catalog 失败: %s", e)
            return []

    async def update_data_catalog(self, session_id: str, refs: list[DataRef]) -> None:
        """更新数据目录（增量合并：已有 key 更新，新 key 追加）。"""
        try:
            key = self._catalog_key(session_id)
            # 读出已有 catalog
            existing_raw = await self._client.lrange(key, 0, -1)
            existing: list[DataRef] = [DataRef.model_validate_json(item) for item in existing_raw]
            existing_keys = {d.key for d in existing}

            # 内存合并
            for ref in refs:
                if ref.key in existing_keys:
                    for i, d in enumerate(existing):
                        if d.key == ref.key:
                            existing[i] = ref
                            break
                else:
                    existing.append(ref)

            # 写回：先删再批量写
            if existing:
                pipe = self._client.pipeline()
                pipe.delete(key)
                pipe.rpush(key, *[d.model_dump_json() for d in existing])
                pipe.expire(key, self._ttl)
                await pipe.execute()
        except Exception as e:
            logger.warning("[RedisConversationStore] update_data_catalog 失败: %s", e)

    async def remove_session(self, session_id: str) -> None:
        """移除指定 session 的所有数据。"""
        try:
            await self._client.delete(self._turns_key(session_id), self._catalog_key(session_id))
        except Exception as e:
            logger.warning("[RedisConversationStore] remove_session 失败: %s", e)