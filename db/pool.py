
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
# 无 DB 配置时设为 True，避免反复尝试连接
_pool_unavailable: bool = False

_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _is_db_configured() -> bool:
    """检查 DB 配置是否完整（host + db_name + user 均非空）。"""
    return bool(settings.db_host and settings.db_name and settings.db_user)


async def get_pool() -> asyncpg.Pool:
    """获取全局连接池（懒初始化，带锁防并发创建）。

    无 DB 配置时直接抛 RuntimeError，不尝试连接。
    """
    global _pool, _pool_unavailable
    if _pool is not None:
        return _pool
    if _pool_unavailable:
        raise RuntimeError("DB 不可用：未配置或连接失败")

    if not _is_db_configured():
        _pool_unavailable = True
        logger.info("[DB] db_host/db_name/db_user 未配置，跳过数据库连接")
        raise RuntimeError("DB 未配置：db_host/db_name/db_user 不能为空")

    async with _pool_lock:
        # Double-check after acquiring lock
        if _pool is not None:
            return _pool
        try:
            _pool = await asyncpg.create_pool(
                dsn=settings.db_dsn,
                min_size=2,
                max_size=10,
                command_timeout=30,
            )
            logger.info("[DB] 连接池已创建: %s", settings.db_dsn.split("@")[-1])
        except Exception as e:
            _pool_unavailable = True
            logger.warning("[DB] 连接池创建失败，标记为不可用: %s", e)
            raise
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