
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any

from models.chat_request import BusinessInfo, UserInfo
from models.conversation import ConversationContext
from core.data_context import DataContext

logger = logging.getLogger(__name__)

# 单会话最大任务结果数，防止内存无限增长
_MAX_RESULTS = 200


class TaskResult:
    """任务执行结果，由TaskContext完成后生成。"""

    def __init__(
        self,
        task_id: str,
        answer: str,
        data_keys: list[str],
        data_snapshot: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.task_id = task_id
        self.answer = answer
        self.data_keys = data_keys
        # 数据快照：{key: {schema: {col: dtype}, rows: int, summary: str}}
        # 在 DataContext.destroy() 之前保存，供多轮对话上下文使用
        self.data_snapshot = data_snapshot or {}
        self.finished_at = datetime.now()

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "answer": self.answer[:500],
            "data_keys": self.data_keys,
            "finished_at": self.finished_at.isoformat(),
        }


class SessionContext:
    """会话级上下文，跨任务持久，管理共享只读资源和历史结果。

    线程安全：写入用asyncio.Lock保护，读操作无需加锁。
    """

    def __init__(
        self,
        session_id: str,
        *,
        chat_id: str = "",
        user: UserInfo | None = None,
        business: BusinessInfo | None = None,
    ) -> None:
        self.session_id = session_id
        self.chat_id = chat_id
        # 用户信息
        self.user = user or UserInfo()
        # 业务信息
        self.business = business or BusinessInfo()
        # ES查询结果：规划前获取，供所有Agent使用
        self.api_meta: list[dict[str, Any]] = []
        self.skills: list[Any] = []  # list[Skill]，按 API 名匹配的本地业务技能
        # 任务结果
        self._results: dict[str, TaskResult] = {}
        self._lock = asyncio.Lock()
        # 多轮对话上下文
        self.conversation_context: ConversationContext | None = None
        # DAG 执行中已失败/跳过的 task_id 集合（跨任务共享，供下游 agent 入口检查）
        self._failed_task_ids: set[str] = set()

    async def add_result(self, result: TaskResult) -> None:
        """追加任务结果（写锁保护）。超过 _MAX_RESULTS 时淘汰最早的结果。"""
        async with self._lock:
            self._results[result.task_id] = result
            # FIFO 淘汰
            if len(self._results) > _MAX_RESULTS:
                oldest_key = next(iter(self._results))
                del self._results[oldest_key]
                logger.warning("[Session] 结果数超限，淘汰最早: %s", oldest_key)
        logger.info("[Session] 任务结果已合并: %s", result.task_id)

    def get_result(self, task_id: str) -> TaskResult | None:
        """获取指定任务结果（读无需锁）。"""
        return self._results.get(task_id)

    def all_result_summaries(self) -> str:
        """返回所有历史结果的摘要，供新任务参考。"""
        if not self._results:
            return "暂无历史任务结果。"
        lines = []
        for result in self._results.values():
            lines.append(f"- {result.task_id}: {result.answer[:200]}")
        return "\n".join(lines)

    @property
    def result_count(self) -> int:
        return len(self._results)


class TaskContext:
    """任务级上下文，agent运行时创建，结束后销毁。

    同一DAG中的所有任务共享同一个DataContext实例，确保上游Agent写入的数据
    下游Agent可以读取。
    """

    def __init__(self, task_id: str, session: SessionContext, data_context: DataContext | None = None) -> None:
        self.task_id = task_id
        self.session = session
        self.data_context = data_context or DataContext()
        self._final_answer: str = ""
        self._destroyed = False

    def set_final_answer(self, answer: str) -> None:
        """设置任务的最终输出。"""
        self._final_answer = answer

    async def merge_to_session(self) -> None:
        """将结果合并到SessionContext，保存DataContext快照。"""
        # 在 destroy 之前保存数据快照
        snapshot: dict[str, dict[str, Any]] = {}
        for key in self.data_context.list_keys():
            df = self.data_context.get(key)
            if df is not None and not df.empty:
                schema = {col: str(dtype) for col, dtype in df.dtypes.items()}
                snapshot[key] = {
                    "schema": schema,
                    "rows": len(df),
                    "columns": df.columns.tolist(),
                    "summary": f"{len(df)}行 x {len(df.columns)}列",
                    "meta": self.data_context.get_meta(key),
                }

        result = TaskResult(
            task_id=self.task_id,
            answer=self._final_answer,
            data_keys=self.data_context.list_keys(),
            data_snapshot=snapshot,
        )
        await self.session.add_result(result)

    async def destroy(self) -> None:
        """释放资源。"""
        if self._destroyed:
            return
        await self.data_context.clear()
        self._destroyed = True
        logger.info("[TaskContext] 已销毁: %s", self.task_id)