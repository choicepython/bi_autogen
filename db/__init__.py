

"""BI Agent 数据库模块。

提供 asyncpg 连接池、DDL 建表、异步写入器、指标查询。
"""

from db.pool import close_pool, get_pool, init_db

__all__ = ["get_pool", "init_db", "close_pool"]