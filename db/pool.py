
"""asyncpg 连接池管理 + DDL 建表。"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

from config.settings import settings

logger = logging.getLogger(__name__)

_pool: asyncpg.Pool | None = None
_pool_lock = asyncio.Lock()

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


async def get_pool() -> asyncpg.Pool:
    """获取全局连接池（懒初始化，带锁防并发创建）。"""
    global _pool
    if _pool is not None:
        return _pool
    async with _pool_lock:
        # Double-check after acquiring lock
        if _pool is not None:
            return _pool
        _pool = await asyncpg.create_pool(
            dsn=settings.db_dsn,
            min_size=2,
            max_size=10,
            command_timeout=30,
        )
        logger.info("[DB] 连接池已创建: %s", settings.db_dsn.split("@")[-1])
    return _pool


async def close_pool() -> None:
    """关闭连接池。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None
        logger.info("[DB] 连接池已关闭")


async def init_db() -> None:
    """执行 schema.sql 建表。幂等操作（IF NOT EXISTS）。"""
    pool = await get_pool()
    sql = _SCHEMA_PATH.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
    logger.info("[DB] schema.sql 执行完成")


async def fetch(query: str, *args: object) -> list[asyncpg.Record]:
    """快捷查询多行。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        return await conn.fetch(query, *args)