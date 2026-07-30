
"""SQL 查询工具：两阶段管线 — 安全校验 + 安全执行。

阶段一：SQL 安全校验（sqlparse）
  - 仅允许 SELECT 语句
  - 拦截恒真查询（WHERE 1=1, WHERE 'a'='a', WHERE TRUE 等）
  - 参数化处理防止 SQL 注入
  - 清除注释防止注释注入

阶段二：安全执行（asyncpg）
  - 自动追加 LIMIT 限制最大返回行数
  - 查询超时控制
  - 结果写入 DataContext
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import sqlparse
from sqlparse.sql import Comment, Identifier, Parenthesis
from sqlparse.tokens import DML, Keyword, Literal

from autogen_core.tools import FunctionTool

from config.exceptions import SQLQueryError
from config.settings import settings
from core.data_context import DataContext

logger = logging.getLogger(__name__)

# 禁止的 SQL 语句类型
_BLOCKED_DML = {"INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE", "TRUNCATE", "REPLACE", "MERGE", "GRANT", "REVOKE"}

# 恒真条件模式
_TAUTOLOGY_PATTERNS = [
    re.compile(r"""\b1\s*=\s*1\b"""),  # 1=1
    re.compile(r"""\bTRUE\b""", re.IGNORECASE),  # TRUE
    re.compile(r"""'([^']*)'\s*=\s*'\1'"""),  # 'a'='a' 字符串自等
    re.compile(r""""([^"]*)"\s*=\s*"\1"""),  # "a"="a" 字符串自等
    re.compile(r"""\b(\w+)\s*=\s*\1\b(?!\s*\.)""", re.IGNORECASE),  # col = col 标识符自等
]


# ---------------------------------------------------------------------------
# 阶段一：SQL 安全校验
# ---------------------------------------------------------------------------


def _strip_comments(sql: str) -> str:
    """移除 SQL 中的注释，防止注释注入。"""
    parsed = sqlparse.parse(sql)
    tokens_to_keep: list[str] = []
    for statement in parsed:
        for token in statement.flatten():
            if isinstance(token, Comment):
                continue
            tokens_to_keep.append(token.ttype)
    # 用 sqlparse 的 format 功能移除注释
    return sqlparse.format(sql, strip_comments=True)


def _check_statement_type(sql: str) -> None:
    """检查 SQL 语句类型，仅允许 SELECT。

    CTE (WITH ... AS (...)) 需要递归检查内部语句是否为 SELECT。
    """
    parsed = sqlparse.parse(sql)
    if not parsed:
        raise SQLQueryError("无法解析SQL语句", stage="validation")

    for statement in parsed:
        _check_statement_tokens(statement)


def _check_statement_tokens(statement: Any) -> None:
    """递归检查语句 token，包括 CTE 内部语句。"""
    first_token = statement.token_first(skip_ws=True, skip_cm=True)
    if first_token is None:
        return

    token_ttype = first_token.ttype
    token_value = first_token.normalized.upper()

    # 检查是否是 DML SELECT
    if token_ttype is DML and token_value == "SELECT":
        return

    # 检查是否是 WITH (CTE) — 需要递归检查内部语句
    # 注意: sqlparse 将 WITH 标记为 Token.Keyword.CTE（不是 Token.Keyword）
    if token_value == "WITH" and (token_ttype is Keyword or (token_ttype is not None and token_ttype in Keyword)):
        _check_cte_inner(statement)
        return

    # 检查是否为禁止的类型
    if token_value in _BLOCKED_DML:
        raise SQLQueryError(f"禁止执行{token_value}语句，仅允许SELECT查询", stage="validation")

    # 如果不是 SELECT 也不是 WITH，也拒绝
    if token_ttype is DML or (token_ttype is not None and token_ttype in Keyword):
        raise SQLQueryError(f"仅允许SELECT查询语句，检测到: {token_value}", stage="validation")


def _check_cte_inner(statement: Any) -> None:
    """递归检查 CTE 内部语句，确保所有子查询都是 SELECT。

    sqlparse 将 CTE 解析为: WITH → Identifier(name AS (subquery)) → ...
    Parenthesis 嵌套在 Identifier 内部，需递归遍历。
    """
    from sqlparse.sql import Identifier, Parenthesis

    def _walk_tokens(token_list: Any) -> None:
        """递归遍历 token 树，找到所有 Parenthesis 并检查。"""
        for token in token_list.tokens if hasattr(token_list, "tokens") else []:
            if isinstance(token, Parenthesis):
                _check_parenthesis(token)
            elif isinstance(token, Identifier):
                _walk_tokens(token)
            # Function 或其他复合 token 也可能包含 Parenthesis
            elif hasattr(token, "tokens"):
                _walk_tokens(token)

    def _check_parenthesis(paren: Parenthesis) -> None:
        """检查 Parenthesis 内的 SQL 语句。"""
        inner_sql = paren.value[1:-1].strip()  # 去掉外层括号
        if not inner_sql:
            return
        inner_parsed = sqlparse.parse(inner_sql)
        for inner_stmt in inner_parsed:
            inner_first = inner_stmt.token_first(skip_ws=True, skip_cm=True)
            if inner_first is None:
                continue
            inner_value = inner_first.normalized.upper()
            inner_ttype = inner_first.ttype

            # 内部必须是 SELECT
            if inner_ttype is DML and inner_value == "SELECT":
                continue
            # 嵌套 WITH
            if inner_ttype is Keyword and inner_value == "WITH":
                _check_cte_inner(inner_stmt)
                continue

            if inner_value in _BLOCKED_DML:
                raise SQLQueryError(
                    f"CTE内部禁止执行{inner_value}语句，仅允许SELECT查询",
                    stage="validation",
                )
            if inner_ttype is DML or inner_ttype is Keyword:
                raise SQLQueryError(
                    f"CTE内部仅允许SELECT查询语句，检测到: {inner_value}",
                    stage="validation",
                )

    _walk_tokens(statement)


def _check_tautology(sql: str) -> None:
    """检测恒真查询条件。"""
    # 提取 WHERE 子句
    where_match = re.search(r"\bWHERE\b(.+?)(?:\bGROUP\b|\bORDER\b|\bLIMIT\b|\bHAVING\b|\bUNION\b|$)", sql, re.IGNORECASE | re.DOTALL)
    if not where_match:
        return

    where_clause = where_match.group(1)

    for pattern in _TAUTOLOGY_PATTERNS:
        match = pattern.search(where_clause)
        if match:
            matched_text = match.group(0).strip()
            raise SQLQueryError(f"检测到恒真条件: {matched_text}，查询条件必须基于实际业务需求", stage="validation")


def _parameterize(sql: str) -> tuple[str, list[Any]]:
    """参数化 SQL，将字面值替换为 $1, $2, ... 占位符。

    返回 (参数化SQL, 参数列表)。
    """
    params: list[Any] = []
    param_idx = 1

    parsed = sqlparse.parse(sql)
    if not parsed:
        return sql, params

    statement = parsed[0]
    new_tokens: list[str] = []

    for token in statement.flatten():
        ttype = token.ttype
        value = token.value

        # 字符串字面值：'xxx' 或 "xxx"
        if ttype in (Literal.String.Single, Literal.String.Symbol):
            # 去掉引号，存入参数
            inner = value[1:-1]
            # 检查是否是常量字符串自等（恒真的另一种形式）
            params.append(inner)
            new_tokens.append(f"${param_idx}")
            param_idx += 1

        # 数字字面值
        elif ttype in (Literal.Number.Integer, Literal.Number.Float):
            if "." in value:
                params.append(float(value))
            else:
                params.append(int(value))
            new_tokens.append(f"${param_idx}")
            param_idx += 1

        # 其他 token 保留原样
        else:
            new_tokens.append(value)

    parameterized = "".join(new_tokens)
    return parameterized, params


def _validate_sql(sql: str) -> tuple[str, list[Any]]:
    """校验并参数化 SQL。

    返回 (参数化SQL, 参数列表)。
    不合法时抛出 SQLQueryError。
    """
    # 1. 清除注释
    sql = _strip_comments(sql)

    # 2. 检查语句类型
    _check_statement_type(sql)

    # 3. 检测恒真条件
    _check_tautology(sql)

    # 4. 参数化
    parameterized, params = _parameterize(sql)

    logger.info("[SQL] 校验通过，参数化SQL: %s, 参数数: %d", parameterized[:200], len(params))
    return parameterized, params


# ---------------------------------------------------------------------------
# 阶段二：安全执行
# ---------------------------------------------------------------------------

# 全局连接池
_pool: Any = None


async def _get_pool() -> Any:
    """懒初始化 asyncpg 连接池。"""
    global _pool
    if _pool is None:
        try:
            import asyncpg
        except ImportError as e:
            raise SQLQueryError("asyncpg not installed, run: uv add asyncpg", stage="execution") from e

        dsn = settings.db_dsn
        logger.info("[SQL] 创建连接池: %s", dsn.split("@")[-1] if "@" in dsn else dsn)
        try:
            _pool = await asyncpg.create_pool(
                dsn=dsn,
                min_size=settings.sql_pool_min_size,
                max_size=settings.sql_pool_max_size,
                command_timeout=settings.sql_query_timeout,
            )
        except Exception as e:
            raise SQLQueryError(f"数据库连接失败: {e}", stage="execution") from e

    return _pool


def _append_limit(sql: str, max_rows: int) -> str:
    """如果 SQL 没有 LIMIT 或 LIMIT 超过阈值，追加或替换 LIMIT。"""
    # 检查是否已有 LIMIT
    limit_match = re.search(r"\bLIMIT\s+(\d+)\s*$", sql, re.IGNORECASE)
    if limit_match:
        current_limit = int(limit_match.group(1))
        if current_limit <= max_rows:
            return sql
        # 替换为最大行数
        return sql[: limit_match.start()] + f"LIMIT {max_rows}"

    # 追加 LIMIT
    return f"{sql} LIMIT {max_rows}"


async def _execute_sql(parameterized_sql: str, params: list[Any]) -> list[dict[str, Any]]:
    """执行参数化 SQL，返回结果行列表。"""
    import pandas as pd

    pool = await _get_pool()

    # 追加行数限制
    limited_sql = _append_limit(parameterized_sql, settings.sql_max_rows)

    try:
        async with pool.acquire() as conn:
            rows = await asyncio.wait_for(
                conn.fetch(limited_sql, *params),
                timeout=settings.sql_query_timeout,
            )
    except asyncio.TimeoutError as e:
        raise SQLQueryError(f"查询超时（{settings.sql_query_timeout}s），请优化查询或缩小查询范围", stage="execution") from e
    except Exception as e:
        error_msg = str(e)
        # 脱敏：不暴露完整的参数化 SQL
        raise SQLQueryError(f"查询执行失败: {error_msg[:200]}", stage="execution") from e

    # 转换为 dict 列表
    if not rows:
        return []

    columns = [col for col in rows[0].keys()]
    result = [{col: row[col] for col in columns} for row in rows]
    logger.info("[SQL] 查询返回 %d 行, %d 列", len(result), len(columns))
    return result


# ---------------------------------------------------------------------------
# 公开入口
# ---------------------------------------------------------------------------


async def sql_query(sql: str, data_context: DataContext | None = None) -> str:
    """安全的 SQL 查询工具。

    两阶段管线：
    1. SQL 安全校验：仅允许 SELECT，拦截恒真查询，参数化防注入
    2. 安全执行：行数限制 + 超时控制，结果写入 DataContext

    Args:
        sql: SQL 查询语句
        data_context: 共享数据上下文，查询结果自动写入

    Returns:
        描述查询结果的中文字符串
    """
    import pandas as pd

    if not sql or not sql.strip():
        raise SQLQueryError("SQL语句不能为空", stage="validation")

    # 阶段一：校验 + 参数化
    parameterized_sql, params = _validate_sql(sql)

    # 阶段二：执行
    rows = await _execute_sql(parameterized_sql, params)

    if not rows:
        return "查询完成，未返回数据。"

    # 转换为 DataFrame
    df = pd.DataFrame(rows)

    # 写入 DataContext
    if data_context is not None:
        key = data_context.generate_key("SQLAgent")
        await data_context.put(key, df, meta={"sql": sql[:500], "rows": len(df), "cols": len(df.columns)})
        summary = data_context.summarize(key)
        return f"SQL查询完成，返回{len(df)}行数据。{summary}"

    return f"SQL查询完成，返回{len(df)}行{len(df.columns)}列数据。"


async def close_sql_pool() -> None:
    """关闭连接池，用于程序退出时清理。"""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_sql_query_tool(data_context: DataContext) -> FunctionTool:
    """创建 SQL 查询 FunctionTool，闭包捕获 data_context。"""

    async def _sql_query(sql: str) -> str:
        return await sql_query(sql, data_context)

    return FunctionTool(
        func=_sql_query,
        name="sql_query",
        description="执行安全的SQL查询语句，仅支持SELECT查询。工具会自动进行SQL安全校验、参数化防注入、行数限制和超时控制。参数：sql(SQL查询语句)",
    )