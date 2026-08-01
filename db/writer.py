
"""异步DB写入器：后台协程消费队列，批量写入PostgreSQL。

使用方式：
    from db.writer import db_writer

    # 主流程中 enqueue（不阻塞）
    await db_writer.enqueue_session({...})
    await db_writer.enqueue_routing({...})

    # 启动/停止后台写入协程
    await db_writer.start()
    await db_writer.stop()
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from db.pool import get_pool
from utils.json_utils import sanitize_for_json

logger = logging.getLogger(__name__)

# 批量写入参数
_FLUSH_INTERVAL = 0.5  # 秒
_BATCH_SIZE = 50
# 退避参数：DB 连接失败时指数退避，避免每 0.5s 重试刷屏
_BACKOFF_INITIAL = 1.0  # 初始退避秒数
_BACKOFF_MAX = 60.0  # 最大退避秒数
_BACKOFF_FACTOR = 2.0  # 退避乘数


class _WriteItem:
    """待写入的数据库记录。"""

    __slots__ = ("table", "data")

    def __init__(self, table: str, data: dict[str, Any]) -> None:
        self.table = table
        self.data = data


class DBWriter:
    """异步DB写入器。

    主流程通过 enqueue_* 方法将记录推入队列，
    后台协程消费队列并批量 INSERT。
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[_WriteItem | None] = asyncio.Queue(maxsize=10000)
        self._task: asyncio.Task[None] | None = None
        self._running = False
        self._dropped_count = 0
        self._total_enqueued = 0
        # 缓存各表的实际列名，INSERT 前过滤掉不存在的列
        self._table_columns: dict[str, set[str]] = {}
        # DB 健康标志 + 退避状态
        self._db_healthy = True
        self._backoff = _BACKOFF_INITIAL

    async def start(self) -> None:
        """启动后台写入协程。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._consume_loop())
        logger.info("[DBWriter] 后台写入协程已启动")

    async def stop(self) -> None:
        """停止后台写入协程，刷写剩余数据。"""
        if not self._running:
            return
        self._running = False
        await self._queue.put(None)  # 哨兵
        if self._task:
            await self._task
            self._task = None
        logger.info("[DBWriter] 后台写入协程已停止")

    # ------------------------------------------------------------------
    # 列过滤 — 防止 schema 未迁移时写入不存在的列
    # ------------------------------------------------------------------

    async def _ensure_columns_loaded(self, conn: Any, table: str) -> None:
        """查询并缓存表的实际列名。"""
        if table in self._table_columns:
            return
        rows = await conn.fetch(
            "SELECT column_name FROM information_schema.columns WHERE table_name = $1",
            table,
        )
        self._table_columns[table] = {r["column_name"] for r in rows}
        logger.debug("[DBWriter] 缓存表 %s 列: %s", table, self._table_columns[table])

    def _filter_row(self, table: str, row: dict[str, Any]) -> dict[str, Any]:
        """过滤掉表中不存在的列。"""
        columns = self._table_columns.get(table)
        if not columns:
            # 未缓存时不过滤（降级为全量写入，由 DB 报错）
            return row
        filtered = {k: v for k, v in row.items() if k in columns}
        dropped = set(row.keys()) - set(filtered.keys())
        if dropped:
            logger.warning("[DBWriter] 表 %s 不存在列 %s，已跳过", table, dropped)
        return filtered

    # ------------------------------------------------------------------
    # Enqueue 方法 — 各业务模块调用
    # ------------------------------------------------------------------

    async def enqueue_session(self, data: dict[str, Any]) -> None:
        """写入/更新会话记录。"""
        await self._enqueue("session", data)

    async def enqueue_routing(self, data: dict[str, Any]) -> None:
        """写入路由决策记录。"""
        await self._enqueue("routing_decision", data)

    async def enqueue_plan(self, data: dict[str, Any]) -> None:
        """写入DAG规划记录。"""
        await self._enqueue("dag_plan", data)

    async def enqueue_agent_execution(self, data: dict[str, Any]) -> None:
        """写入Agent执行记录。"""
        await self._enqueue("agent_execution", data)

    async def enqueue_tool_call(self, data: dict[str, Any]) -> None:
        """写入工具调用记录。"""
        await self._enqueue("tool_call", data)

    async def enqueue_llm_call(self, data: dict[str, Any]) -> None:
        """写入LLM调用记录。"""
        await self._enqueue("llm_call", data)

    async def enqueue_feedback(self, data: dict[str, Any]) -> None:
        """写入用户反馈记录。"""
        await self._enqueue("session_feedback", data)

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------

    async def _enqueue(self, table: str, data: dict[str, Any]) -> None:
        """推入队列。满时丢弃并记录警告，连续丢弃超过阈值时抛出异常回压。"""
        item = _WriteItem(table, data)
        try:
            self._queue.put_nowait(item)
            self._total_enqueued += 1
        except asyncio.QueueFull:
            self._dropped_count += 1
            logger.warning(
                "[DBWriter] 队列已满，丢弃 %s 记录! dropped=%d/%d",
                table, self._dropped_count, self._total_enqueued + self._dropped_count,
            )
            # 回压：连续丢弃超过阈值时告警
            if self._dropped_count > 100:
                logger.error("[DBWriter] 累计丢弃 %d 条记录，系统可能过载!", self._dropped_count)

    async def _consume_loop(self) -> None:
        """后台消费循环：定时或批量刷写。DB 不可用时指数退避。"""
        batch: list[_WriteItem] = []

        while True:
            try:
                # DB 不可用时退避等待，不反复尝试连接
                if not self._db_healthy:
                    logger.debug("[DBWriter] DB 不可用，退避 %ds", int(self._backoff))
                    await asyncio.sleep(self._backoff)
                    # 退避期间积攒的数据尝试刷写
                    if batch:
                        await self._flush_batch(batch)
                        batch = []
                    continue

                # 等待新数据或超时
                try:
                    item = await asyncio.wait_for(self._queue.get(), timeout=_FLUSH_INTERVAL)
                except asyncio.TimeoutError:
                    # 超时，刷写当前批次
                    if batch:
                        await self._flush_batch(batch)
                        batch = []
                    continue

                # 哨兵：退出
                if item is None:
                    if batch:
                        await self._flush_batch(batch)
                    break

                batch.append(item)

                # 批量满，立即刷写
                if len(batch) >= _BATCH_SIZE:
                    await self._flush_batch(batch)
                    batch = []

            except Exception as e:
                # DB 连接/写入失败 → 标记不健康，启动指数退避
                self._db_healthy = False
                self._backoff = min(self._backoff * _BACKOFF_FACTOR, _BACKOFF_MAX)
                logger.warning(
                    "[DBWriter] DB 写入失败，退避 %ds（下次重试）: %s, batch_size=%d",
                    int(self._backoff), e, len(batch),
                )
                # 失败 batch 写入磁盘，避免数据丢失
                if batch:
                    self._spill_to_disk(batch, e)
                    batch = []

    def _spill_to_disk(self, batch: list[_WriteItem], error: Exception) -> None:
        """将失败的 batch 写入磁盘 JSONL，避免数据丢失。"""
        try:
            spill_dir = Path("logs/db_spill")
            spill_dir.mkdir(parents=True, exist_ok=True)
            spill_path = spill_dir / f"spill_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"
            with open(spill_path, "w", encoding="utf-8") as f:
                for item in batch:
                    f.write(json.dumps({"table": item.table, "data": sanitize_for_json(item.data)}, ensure_ascii=False, default=str) + "\n")
            logger.warning("[DBWriter] %d 条记录已写入磁盘: %s", len(batch), spill_path)
        except Exception as write_err:
            logger.error("[DBWriter] 磁盘溢出写入也失败: %s", write_err)

    async def _flush_batch(self, batch: list[_WriteItem]) -> None:
        """批量写入一批记录。按表分组，逐表批量INSERT。成功时重置退避。"""
        if not batch:
            return

        # 按表分组
        by_table: dict[str, list[dict[str, Any]]] = {}
        for item in batch:
            by_table.setdefault(item.table, []).append(item.data)

        # 保证写入顺序：session 表必须先于子表（外键依赖）
        _TABLE_ORDER = {"session": 0, "routing_decision": 1, "dag_plan": 2, "agent_execution": 3,
                        "tool_call": 4, "llm_call": 5, "session_feedback": 6}
        sorted_tables = sorted(by_table.keys(), key=lambda t: _TABLE_ORDER.get(t, 99))

        pool = await get_pool()  # DB 不可用时抛 RuntimeError，由 _consume_loop 捕获
        async with pool.acquire() as conn:
            # 预加载所有涉及的表列信息
            for table in sorted_tables:
                await self._ensure_columns_loaded(conn, table)

            for table in sorted_tables:
                raw_rows = by_table[table]
                # 过滤掉表中不存在的列
                rows = [self._filter_row(table, r) for r in raw_rows]
                rows = [r for r in rows if r]  # 跳过过滤后为空的行
                if not rows:
                    continue
                try:
                    await self._insert_batch(conn, table, rows)
                except Exception as e:
                    logger.error("[DBWriter] 批量INSERT失败 %s (%d条): %s", table, len(rows), e)
                    # 降级为逐条插入
                    for row in rows:
                        try:
                            await self._insert_one(conn, table, row)
                        except Exception as e2:
                            logger.error("[DBWriter] 单条INSERT也失败 %s: %s", table, e2)

        # 写入成功 → 重置退避
        if not self._db_healthy or self._backoff != _BACKOFF_INITIAL:
            logger.info("[DBWriter] DB 恢复健康，重置退避")
        self._db_healthy = True
        self._backoff = _BACKOFF_INITIAL

    @staticmethod
    async def _insert_batch(conn: Any, table: str, rows: list[dict[str, Any]]) -> None:
        """批量INSERT（使用 asyncpg.copy_records_to_table 或拼接SQL）。"""
        if not rows:
            return

        # 统一列顺序
        columns = list(rows[0].keys())
        values_list = []
        for row in rows:
            values = tuple(row.get(col) for col in columns)
            values_list.append(values)

        # 构建批量INSERT SQL
        cols_str = ", ".join(columns)
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        sql = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})"

        # session 表使用 UPSERT（ON CONFLICT UPDATE）
        # 仅更新数据中实际传入的列，避免用默认值覆盖已写入的统计字段
        if table == "session":
            update_cols = [c for c in columns if c not in ("session_id", "chat_id")]
            if update_cols:
                update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                sql += f" ON CONFLICT (session_id, chat_id) DO UPDATE SET {update_set}"
            else:
                sql += " ON CONFLICT (session_id, chat_id) DO NOTHING"

        await conn.executemany(sql, values_list)
        logger.debug("[DBWriter] 批量INSERT %s: %d条", table, len(rows))

    @staticmethod
    async def _insert_one(conn: Any, table: str, row: dict[str, Any]) -> None:
        """单条INSERT（降级路径）。"""
        columns = list(row.keys())
        values = tuple(row.get(col) for col in columns)
        cols_str = ", ".join(columns)
        placeholders = ", ".join(f"${i}" for i in range(1, len(columns) + 1))
        sql = f"INSERT INTO {table} ({cols_str}) VALUES ({placeholders})"

        if table == "session":
            update_cols = [c for c in columns if c not in ("session_id", "chat_id")]
            if update_cols:
                update_set = ", ".join(f"{c} = EXCLUDED.{c}" for c in update_cols)
                sql += f" ON CONFLICT (session_id, chat_id) DO UPDATE SET {update_set}"
            else:
                sql += " ON CONFLICT (session_id, chat_id) DO NOTHING"

        await conn.execute(sql, *values)


# 全局单例
db_writer = DBWriter()


# ------------------------------------------------------------------
# 便捷构造函数 — 从业务对象构造写入数据
# ------------------------------------------------------------------

def make_session_data(
    *,
    session_id: str,
    chat_id: str = "",
    query: str = "",
    status: str = "running",
    execution_mode: str = "",
    result: str = "",
    error_message: str = "",
    user_id: str = "",
    user_name: str = "",
    source_site: str = "les_portal",
    duration_ms: int = 0,
    total_prompt_tokens: int = 0,
    total_completion_tokens: int = 0,
    total_tokens: int = 0,
    agent_count: int = 0,
    tool_call_count: int = 0,
    llm_call_count: int = 0,
    model_name: str = "",
    selector_model: str = "",
    extra: dict[str, Any] | None = None,
    partial: bool = False,
) -> dict[str, Any]:
    """构造 session 表写入数据。

    Args:
        partial: 为 True 时仅包含非默认值的字段，用于 UPSERT 部分更新，
                 避免用默认值覆盖已写入的统计字段。
    """
    full = {
        "session_id": session_id,
        "chat_id": chat_id,
        "query": query[:5000],
        "status": status,
        "execution_mode": execution_mode,
        "result": result[:10000],
        "error_message": error_message[:5000],
        "user_id": user_id,
        "user_name": user_name,
        "source_site": source_site,
        "duration_ms": duration_ms,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_tokens,
        "agent_count": agent_count,
        "tool_call_count": tool_call_count,
        "llm_call_count": llm_call_count,
        "model_name": model_name,
        "selector_model": selector_model,
        "extra": json.dumps(extra or {}, ensure_ascii=False),
    }
    if not partial:
        return full

    # partial 模式：仅保留主键 + 非默认值字段
    _DEFAULTS = {
        "chat_id": "", "query": "", "execution_mode": "", "result": "",
        "error_message": "", "user_id": "", "user_name": "", "source_site": "les_portal",
        "duration_ms": 0, "total_prompt_tokens": 0, "total_completion_tokens": 0,
        "total_tokens": 0, "agent_count": 0, "tool_call_count": 0, "llm_call_count": 0,
        "model_name": "", "selector_model": "", "extra": json.dumps({}, ensure_ascii=False),
    }
    return {k: v for k, v in full.items() if k in ("session_id", "chat_id") or v != _DEFAULTS.get(k)}


def make_routing_data(
    *,
    session_id: str,
    chat_id: str = "",
    route_layer: int,
    execution_mode: str,
    agent_type: str = "",
    task_description: str = "",
    reasoning: str = "",
    api_meta_count: int = 0,
    skills_count: int = 0,
    api_meta_names: list[str] | None = None,
    duration_ms: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 routing_decision 表写入数据。"""
    return {
        "session_id": session_id,
        "chat_id": chat_id,
        "route_layer": route_layer,
        "execution_mode": execution_mode,
        "agent_type": agent_type,
        "task_description": task_description[:2000],
        "reasoning": reasoning[:2000],
        "api_meta_count": api_meta_count,
        "skills_count": skills_count,
        "api_meta_names": api_meta_names or [],
        "duration_ms": duration_ms,
        "extra": json.dumps(extra or {}, ensure_ascii=False),
    }


def make_plan_data(
    *,
    session_id: str,
    chat_id: str = "",
    task_count: int,
    tasks_json: list[dict[str, Any]] | str,
    reasoning: str = "",
    is_complete: bool = False,
    duration_ms: int = 0,
    parse_success: bool = True,
    fallback_used: bool = False,
    retry_count: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 dag_plan 表写入数据。"""
    tasks = tasks_json if isinstance(tasks_json, str) else json.dumps(tasks_json, ensure_ascii=False)
    return {
        "session_id": session_id,
        "chat_id": chat_id,
        "task_count": task_count,
        "tasks_json": tasks,
        "reasoning": reasoning[:2000],
        "is_complete": is_complete,
        "duration_ms": duration_ms,
        "parse_success": parse_success,
        "fallback_used": fallback_used,
        "retry_count": retry_count,
        "extra": json.dumps(extra or {}, ensure_ascii=False),
    }


def make_agent_execution_data(
    *,
    session_id: str,
    chat_id: str = "",
    task_id: str = "",
    agent_type: str,
    agent_name: str,
    task_description: str = "",
    status: str = "running",
    error_type: str = "",
    error_message: str = "",
    result_preview: str = "",
    data_keys: list[str] | None = None,
    data_row_count: int = 0,
    duration_ms: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    llm_call_count: int = 0,
    tool_call_count: int = 0,
    retry_count: int = 0,
    retry_reason: str = "",
    finished_at: datetime | str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 agent_execution 表写入数据。"""
    # asyncpg 要求 TIMESTAMPTZ 字段传入 datetime 对象，不能传字符串
    _finished_at: datetime | None = None
    if isinstance(finished_at, str):
        _finished_at = datetime.fromisoformat(finished_at)
    elif isinstance(finished_at, datetime):
        _finished_at = finished_at
    return {
        "session_id": session_id,
        "chat_id": chat_id,
        "task_id": task_id,
        "agent_type": agent_type,
        "agent_name": agent_name,
        "task_description": task_description[:2000],
        "status": status,
        "error_type": error_type,
        "error_message": error_message[:5000],
        "result_preview": result_preview[:2000],
        "data_keys": data_keys or [],
        "data_row_count": data_row_count,
        "duration_ms": duration_ms,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "llm_call_count": llm_call_count,
        "tool_call_count": tool_call_count,
        "retry_count": retry_count,
        "retry_reason": retry_reason[:1000],
        "finished_at": _finished_at,
        "extra": json.dumps(extra or {}, ensure_ascii=False),
    }


def make_tool_call_data(
    *,
    session_id: str,
    chat_id: str = "",
    task_id: str = "",
    agent_name: str,
    tool_name: str,
    call_id: str = "",
    arguments: dict[str, Any] | str = "",
    is_error: bool = False,
    error_message: str = "",
    result_preview: str = "",
    duration_ms: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 tool_call 表写入数据。"""
    args = arguments if isinstance(arguments, str) else json.dumps(arguments, ensure_ascii=False)
    return {
        "session_id": session_id,
        "chat_id": chat_id,
        "task_id": task_id,
        "agent_name": agent_name,
        "tool_name": tool_name,
        "call_id": call_id,
        "arguments": args,
        "is_error": is_error,
        "error_message": error_message[:2000],
        "result_preview": result_preview[:2000],
        "duration_ms": duration_ms,
        "extra": json.dumps(extra or {}, ensure_ascii=False),
    }


def make_llm_call_data(
    *,
    session_id: str,
    chat_id: str = "",
    task_id: str = "",
    agent_name: str,
    call_index: int,
    direction: str,
    is_stream: bool = False,
    model_name: str = "",
    message_count: int = 0,
    tool_count: int = 0,
    tool_names: list[str] | None = None,
    finish_reason: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    total_tokens: int = 0,
    cached: bool = False,
    chunk_count: int = 0,
    request_summary: str = "",
    response_preview: str = "",
    thought: str = "",
    duration_ms: int = 0,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """构造 llm_call 表写入数据。"""
    return {
        "session_id": session_id,
        "chat_id": chat_id,
        "task_id": task_id,
        "agent_name": agent_name,
        "call_index": call_index,
        "direction": direction,
        "is_stream": is_stream,
        "model_name": model_name,
        "message_count": message_count,
        "tool_count": tool_count,
        "tool_names": tool_names or [],
        "finish_reason": finish_reason,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": total_tokens,
        "cached": cached,
        "chunk_count": chunk_count,
        "request_summary": request_summary[:500],
        "response_preview": response_preview[:500],
        "thought": thought[:2000],
        "duration_ms": duration_ms,
        "extra": json.dumps(extra or {}, ensure_ascii=False),
    }