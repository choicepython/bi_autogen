
"""指标查询函数：供监控面板和智能体演进分析使用。"""

from __future__ import annotations

import logging
from typing import Any

from db.pool import fetch

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 会话级指标
# ---------------------------------------------------------------------------

async def get_session_stats(hours: int = 24) -> list[dict[str, Any]]:
    """按小时统计会话量、成功率、平均耗时、Token消耗。"""
    rows = await fetch(
        """
        SELECT
            date_trunc('hour', created_at) AS hour,
            COUNT(*) AS total_sessions,
            ROUND(AVG(CASE WHEN status='completed' THEN 1 ELSE 0 END) * 100, 1) AS success_rate,
            ROUND(AVG(duration_ms)) AS avg_duration_ms,
            SUM(total_tokens) AS total_tokens
        FROM session
        WHERE created_at >= NOW() - ($1 || ' hours')::INTERVAL
        GROUP BY hour
        ORDER BY hour
        """,
        str(hours),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Agent 指标
# ---------------------------------------------------------------------------

async def get_agent_metrics(days: int = 7) -> list[dict[str, Any]]:
    """各Agent成功率、平均耗时、Token消耗。"""
    rows = await fetch(
        """
        SELECT
            agent_type,
            COUNT(*) AS total_executions,
            ROUND(AVG(CASE WHEN status='success' THEN 1 ELSE 0 END) * 100, 1) AS success_rate,
            ROUND(AVG(duration_ms)) AS avg_duration_ms,
            ROUND(AVG(total_tokens)) AS avg_tokens,
            SUM(total_tokens) AS total_tokens,
            SUM(retry_count) AS total_retries
        FROM agent_execution
        WHERE created_at >= NOW() - ($1 || ' days')::INTERVAL
        GROUP BY agent_type
        ORDER BY total_executions DESC
        """,
        str(days),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 工具指标
# ---------------------------------------------------------------------------

async def get_tool_metrics(days: int = 7) -> list[dict[str, Any]]:
    """工具调用频次、错误率、平均耗时。"""
    rows = await fetch(
        """
        SELECT
            tool_name,
            COUNT(*) AS total_calls,
            SUM(CASE WHEN is_error THEN 1 ELSE 0 END) AS error_count,
            ROUND(AVG(CASE WHEN is_error THEN 1 ELSE 0 END) * 100, 1) AS error_rate,
            ROUND(AVG(duration_ms)) AS avg_duration_ms
        FROM tool_call
        WHERE created_at >= NOW() - ($1 || ' days')::INTERVAL
        GROUP BY tool_name
        HAVING COUNT(*) > 5
        ORDER BY total_calls DESC
        """,
        str(days),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 路由指标
# ---------------------------------------------------------------------------

async def get_routing_distribution(days: int = 7) -> list[dict[str, Any]]:
    """路由层命中分布。"""
    rows = await fetch(
        """
        SELECT
            route_layer,
            COUNT(*) AS hit_count,
            ROUND(AVG(duration_ms)) AS avg_duration_ms
        FROM routing_decision
        WHERE created_at >= NOW() - ($1 || ' days')::INTERVAL
        GROUP BY route_layer
        ORDER BY route_layer
        """,
        str(days),
    )
    return [dict(r) for r in rows]


async def get_routing_agent_distribution(days: int = 7) -> list[dict[str, Any]]:
    """路由目标Agent分布。"""
    rows = await fetch(
        """
        SELECT
            agent_type,
            COUNT(*) AS hit_count
        FROM routing_decision
        WHERE created_at >= NOW() - ($1 || ' days')::INTERVAL
        GROUP BY agent_type
        ORDER BY hit_count DESC
        """,
        str(days),
    )
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# 完整链路追溯
# ---------------------------------------------------------------------------

async def get_session_trace(session_id: str, chat_id: str = "") -> dict[str, Any]:
    """追溯某次会话的完整执行链路。"""
    # 会话基本信息
    session_rows = await fetch(
        "SELECT * FROM session WHERE session_id = $1 AND chat_id = $2",
        session_id, chat_id,
    )
    if not session_rows:
        return {}

    result: dict[str, Any] = {"session": dict(session_rows[0])}

    # 路由决策
    routing_rows = await fetch(
        "SELECT * FROM routing_decision WHERE session_id = $1 AND chat_id = $2",
        session_id, chat_id,
    )
    result["routing"] = [dict(r) for r in routing_rows]

    # DAG规划
    plan_rows = await fetch(
        "SELECT * FROM dag_plan WHERE session_id = $1 AND chat_id = $2",
        session_id, chat_id,
    )
    result["plan"] = [dict(r) for r in plan_rows]

    # Agent执行
    agent_rows = await fetch(
        "SELECT * FROM agent_execution WHERE session_id = $1 AND chat_id = $2 ORDER BY created_at",
        session_id, chat_id,
    )
    result["agents"] = [dict(r) for r in agent_rows]

    # 工具调用
    tool_rows = await fetch(
        "SELECT * FROM tool_call WHERE session_id = $1 AND chat_id = $2 ORDER BY created_at",
        session_id, chat_id,
    )
    result["tools"] = [dict(r) for r in tool_rows]

    # LLM调用
    llm_rows = await fetch(
        "SELECT * FROM llm_call WHERE session_id = $1 AND chat_id = $2 ORDER BY call_index",
        session_id, chat_id,
    )
    result["llm_calls"] = [dict(r) for r in llm_rows]

    return result