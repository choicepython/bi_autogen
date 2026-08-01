
"""存储工厂：根据配置创建 ConversationStore / DataContextCache。

优雅降级策略：
1. settings.redis_url 为空 → 直接返回 InMemory 实现
2. settings.redis_url 有值但 Redis 连接失败 → log warning + 返回 InMemory
3. Redis 连接成功 → 返回 Redis 实现

使用方式（在 gateway/app.py lifespan 中调用）：
    store = await create_conversation_store()
    cache = await create_data_context_cache()
    team = BITeam(conversation_store=store, data_context_cache=cache)
"""

from __future__ import annotations

import logging

import redis.asyncio as aioredis

from config import settings
from core.conversation_store import ConversationStore, InMemoryConversationStore
from core.data_context_cache import DataContextCache, InMemoryDataContextCache

logger = logging.getLogger(__name__)


async def create_conversation_store() -> ConversationStore:
    """根据 settings.redis_url 创建对话存储实例。

    未配置或连接失败时降级到 InMemoryConversationStore。
    """
    if not settings.redis_url:
        logger.info("[StoreFactory] redis_url 未配置，使用 InMemoryConversationStore")
        return InMemoryConversationStore()

    try:
        client = aioredis.from_url(settings.redis_url, decode_responses=False)
        await client.ping()
        logger.info("[StoreFactory] Redis 已连接，使用 RedisConversationStore")
        # 延迟导入避免未安装 redis 时模块加载失败
        from core.redis_conversation_store import RedisConversationStore

        return RedisConversationStore(client)
    except Exception as e:
        logger.warning("[StoreFactory] Redis 连接失败，降级到 InMemoryConversationStore: %s", e)
        return InMemoryConversationStore()


async def create_data_context_cache() -> DataContextCache:
    """根据 settings.redis_url 创建 DataContext 缓存实例。

    未配置或连接失败时降级到 InMemoryDataContextCache。
    """
    if not settings.redis_url:
        logger.info("[StoreFactory] redis_url 未配置，使用 InMemoryDataContextCache")
        return InMemoryDataContextCache()

    try:
        client = aioredis.from_url(settings.redis_url, decode_responses=False)
        await client.ping()
        logger.info("[StoreFactory] Redis 已连接，使用 RedisDataContextCache")
        from core.redis_data_context_cache import RedisDataContextCache

        return RedisDataContextCache(client)
    except Exception as e:
        logger.warning("[StoreFactory] Redis 连接失败，降级到 InMemoryDataContextCache: %s", e)
        return InMemoryDataContextCache()