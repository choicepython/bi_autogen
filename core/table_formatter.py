
"""DataFrame → TABLE 事件转换工具。

将 DataContext 中的 DataFrame 转换为前端可渲染的 TABLE StreamEvent。
提取自 AgentLayer，消除 core/ 对 DataFrame 内容的直接操作，
同时消除 run_team 和 _emit_table_events 中的重复 numpy 类型净化逻辑。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from core.data_context import DataContext
from core.event_translator import EventTranslator
from models.stream_event import StreamEvent


def sanitize_row(values: list[Any]) -> list[Any]:
    """将 DataFrame 行值中的 numpy 类型转换为原生 Python 类型。

    Args:
        values: DataFrame 单行的 .tolist() 结果。

    Returns:
        转换后的值列表（numpy int→int, float→float, bool→bool, NaN→""）。
    """
    sanitized: list[Any] = []
    for v in values:
        # NaN 检查必须在 numpy 类型检查之前，因为 np.nan 是 np.floating 实例
        if pd.isna(v):
            sanitized.append("")
        elif isinstance(v, np.integer):
            sanitized.append(int(v))
        elif isinstance(v, np.floating):
            sanitized.append(float(v))
        elif isinstance(v, np.bool_):
            sanitized.append(bool(v))
        else:
            sanitized.append(str(v))
    return sanitized


def dataframe_to_table_rows(
    df: pd.DataFrame,
    max_rows: int = 100,
    max_columns: int = 50,
) -> tuple[list[str], list[list[Any]], int, bool]:
    """将 DataFrame 转换为 TABLE 事件所需的列名和行数据。

    Args:
        df: 待转换的 DataFrame。
        max_rows: 最大显示行数。
        max_columns: 最大显示列数。

    Returns:
        (columns, rows, total_row_count, truncated_cols) 四元组。
    """
    truncated_cols = len(df.columns) > max_columns
    columns = df.columns[:max_columns].tolist()
    if truncated_cols:
        columns.append(f"...({len(df.columns) - max_columns}列已截断)")

    display_df = df.head(max_rows).iloc[:, :max_columns]
    rows: list[list[Any]] = []
    for _, row in display_df.iterrows():
        sanitized = sanitize_row(row.tolist())
        if truncated_cols:
            sanitized.append("...")
        rows.append(sanitized)

    return columns, rows, len(df), truncated_cols


def emit_table_events(
    translator: EventTranslator,
    data_context: DataContext,
    max_rows: int = 100,
    max_columns: int = 50,
) -> list[StreamEvent]:
    """从 DataContext 中提取所有 DataFrame，生成 TABLE 事件列表。

    Args:
        translator: 事件翻译器，用于生成 StreamEvent。
        data_context: 共享数据上下文。
        max_rows: 每个表格最大显示行数。
        max_columns: 每个表格最大显示列数。

    Returns:
        TABLE 类型的 StreamEvent 列表。
    """
    events: list[StreamEvent] = []
    for key in data_context.list_keys():
        df = data_context.get(key)
        if df is None or df.empty:
            continue
        columns, rows, row_count, _ = dataframe_to_table_rows(df, max_rows, max_columns)
        events.append(translator.make_table_event(
            key=key,
            columns=columns,
            rows=rows,
            row_count=row_count,
        ))
    return events


def emit_table_event_for_key(
    translator: EventTranslator,
    key: str,
    df: pd.DataFrame,
    max_rows: int = 100,
) -> StreamEvent | None:
    """为单个 DataFrame 生成一个 TABLE 事件。

    Args:
        translator: 事件翻译器。
        key: DataContext 中的数据集 key。
        df: 待转换的 DataFrame。
        max_rows: 最大显示行数。

    Returns:
        TABLE StreamEvent，或 None（DataFrame 为空时）。
    """
    if df.empty:
        return None
    columns, rows, row_count, _ = dataframe_to_table_rows(df, max_rows)
    return translator.make_table_event(
        key=key,
        columns=columns,
        rows=rows,
        row_count=row_count,
    )