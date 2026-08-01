
"""资源存储工厂：根据配置创建 ResourceStore。

优雅降级策略（同 store_factory.py 模式）：
1. settings.es_hosts 为空 → 使用 LocalResourceStore（读 source/ 目录）
2. settings.es_hosts 有值 → 使用 ESResourceStore

使用方式：
    from core.resource_factory import get_resource_store
    store = get_resource_store()
    results = await store.search_by_query(query, source_site)
"""

from __future__ import annotations

import logging

from config import settings
from core.resource_store import ResourceStore

logger = logging.getLogger(__name__)

_resource_store: ResourceStore | None = None


def get_resource_store() -> ResourceStore:
    """获取全局 ResourceStore 单例。

    有 es_hosts → ESResourceStore
    无 es_hosts → LocalResourceStore（读 source/ 目录）
    """
    global _resource_store
    if _resource_store is None:
        _resource_store = _create_resource_store()
    return _resource_store


def _create_resource_store() -> ResourceStore:
    """根据配置创建 ResourceStore 实例。"""
    if settings.es_hosts:
        logger.info("[ResourceFactory] es_hosts 已配置，使用 ESResourceStore")
        from core.resource_store import ESResourceStore
        return ESResourceStore()
    logger.info("[ResourceFactory] es_hosts 未配置，使用 LocalResourceStore（source/ 目录）")
    from core.resource_store import LocalResourceStore
    return LocalResourceStore()