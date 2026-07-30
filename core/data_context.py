
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

import pandas as pd

from config import DataContextError, settings
from models.chart_artifact import ChartArtifact

logger = logging.getLogger(__name__)


class DataContext:
    _MAX_KEYS = 50  # 单个 DataContext 最大 key 数

    def __init__(self) -> None:
        self._store: dict[str, pd.DataFrame] = {}
        self._metadata: dict[str, dict[str, object]] = {}
        self._charts: dict[str, ChartArtifact] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, df: pd.DataFrame, meta: dict[str, object] | None = None) -> None:
        if not isinstance(df, pd.DataFrame):
            raise DataContextError(key, "put", "value must be a pandas DataFrame")
        async with self._lock:
            # 超出上限时淘汰最旧条目
            if key not in self._store and len(self._store) >= self._MAX_KEYS:
                oldest = next(iter(self._store))
                del self._store[oldest]
                del self._metadata[oldest]
                logger.warning("[DataContext] 超出 %d key 上限，淘汰: %s", self._MAX_KEYS, oldest)
            self._store[key] = df
            self._metadata[key] = meta or {}
        logger.info("DataContext put: key=%s, shape=%s", key, df.shape)

    def get(self, key: str) -> pd.DataFrame | None:
        return self._store.get(key)

    def get_meta(self, key: str) -> dict[str, object]:
        """获取指定 key 的元数据（如来源 API 名/参数、python_exec 代码等）。"""
        return self._metadata.get(key, {})

    def list_keys(self) -> list[str]:
        return list(self._store.keys())

    async def remove(self, key: str) -> None:
        async with self._lock:
            self._store.pop(key, None)
            self._metadata.pop(key, None)

    async def clear(self) -> None:
        async with self._lock:
            self._store.clear()
            self._metadata.clear()
            self._charts.clear()

    def summarize(self, key: str, max_rows: int | None = None, max_cols: int | None = None) -> str:
        df = self._store.get(key)
        if df is None:
            return f"数据集 '{key}' 不存在。可用数据集: {self.list_keys()}"

        max_rows = max_rows or settings.data_context_max_summary_rows
        max_cols = max_cols or settings.data_context_max_summary_cols

        cols = df.columns[:max_cols].tolist()
        df_display = df[cols].head(max_rows)

        parts = [
            f"数据集 '{key}': {len(df)} 行 x {len(df.columns)} 列",
            f"列名: {list(df.columns)}",
            f"数据类型: {dict(df.dtypes)}",
            f"前 {max_rows} 行:\n{df_display.to_string()}",
        ]

        numeric_cols = df.select_dtypes(include="number").columns.tolist()
        if numeric_cols:
            parts.append(f"数值列统计:\n{df[numeric_cols].describe().to_string()}")

        # 数据来源溯源（meta 含 api_name 或 tool_name 时追加）
        meta = self._metadata.get(key, {})
        source_line = self._render_source(meta)
        if source_line:
            parts.append(source_line)

        return "\n\n".join(parts)

    @staticmethod
    def _render_source(meta: dict[str, object]) -> str:
        """根据 meta 渲染数据来源行，无来源信息时返回空串。"""
        if not meta:
            return ""
        api_name = meta.get("api_name")
        if api_name:
            params = meta.get("params")
            if params:
                return f"数据来源: API {api_name}(参数: {params})"
            return f"数据来源: API {api_name}"
        tool_name = meta.get("tool_name")
        if tool_name:
            return f"数据来源: {tool_name}"
        return ""

    def all_summaries(self) -> str:
        parts: list[str] = []
        if self._store:
            parts.append("\n\n---\n\n".join(self.summarize(k) for k in self._store))
        if self._charts:
            parts.append(self.chart_summaries())
        if not parts:
            return "DataContext 当前为空，没有可用数据。"
        return "\n\n---\n\n".join(parts)

    _key_counter: int = 0

    def generate_key(self, agent_name: str, task_id: int = 0) -> str:
        DataContext._key_counter += 1
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return f"{agent_name}_{task_id}_{ts}_{DataContext._key_counter}"

    # ---- Chart artifact methods ----

    async def put_chart(self, key: str, artifact: ChartArtifact) -> None:
        async with self._lock:
            self._charts[key] = artifact
        logger.info("DataContext put_chart: key=%s, type=%s, title=%s", key, artifact.chart_type, artifact.title)

    def get_chart(self, key: str) -> ChartArtifact | None:
        return self._charts.get(key)

    def list_chart_keys(self) -> list[str]:
        return list(self._charts.keys())

    async def remove_chart(self, key: str) -> None:
        async with self._lock:
            self._charts.pop(key, None)

    def chart_summaries(self) -> str:
        if not self._charts:
            return "当前没有可用图表。"
        lines: list[str] = []
        for key, art in self._charts.items():
            lines.append(f"图表 '{key}': 类型={art.chart_type}, 标题={art.title}, 数据源={art.data_key}, 尺寸={art.width}x{art.height}")
        return "\n".join(lines)