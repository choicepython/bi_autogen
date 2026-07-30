
"""数据清洗工具：对 DataContext 中的 DataFrame 执行清洗/转换操作。"""

from __future__ import annotations

import json
import logging

import numpy as np
import pandas as pd

from autogen_core.tools import FunctionTool

from config.exceptions import DataAnalysisError
from config.settings import settings
from core.data_context import DataContext

logger = logging.getLogger(__name__)


def _apply_fill_na(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    columns = params.get("columns")
    value = params.get("value")
    method = params.get("method")

    target_cols = columns if columns else df.columns.tolist()

    if value is not None:
        for col in target_cols:
            if col in df.columns:
                df[col] = df[col].fillna(value)
    elif method in ("mean", "median", "mode", "ffill", "bfill"):
        for col in target_cols:
            if col not in df.columns:
                continue
            if method == "mean":
                df[col] = df[col].fillna(df[col].mean())
            elif method == "median":
                df[col] = df[col].fillna(df[col].median())
            elif method == "mode":
                mode_val = df[col].mode()
                if len(mode_val) > 0:
                    df[col] = df[col].fillna(mode_val.iloc[0])
            elif method == "ffill":
                df[col] = df[col].ffill()
            elif method == "bfill":
                df[col] = df[col].bfill()
    else:
        raise DataAnalysisError("data_clean", f"fill_na 需要 value 或 method(mean/median/mode/ffill/bfill)")

    return df


def _apply_drop_na(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    subset = params.get("subset")
    how = params.get("how", "any")
    kwargs: dict[str, object] = {"how": how}
    if subset:
        kwargs["subset"] = subset
    return df.dropna(**kwargs)  # type: ignore[arg-type]


def _apply_drop_duplicates(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    subset = params.get("subset")
    kwargs: dict[str, object] = {}
    if subset:
        kwargs["subset"] = subset
    return df.drop_duplicates(**kwargs)  # type: ignore[arg-type]


def _apply_rename_columns(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    mapping = params.get("mapping")
    if not mapping or not isinstance(mapping, dict):
        raise DataAnalysisError("data_clean", "rename_columns 需要 mapping 参数，格式: {旧列名: 新列名}")
    return df.rename(columns=mapping)


def _apply_filter(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    condition = params.get("condition")
    if not condition or not isinstance(condition, str):
        raise DataAnalysisError("data_clean", "filter 需要 condition 参数，如 'col1 > 100'")
    try:
        mask = df.eval(condition)
        return df[mask]
    except Exception as e:
        raise DataAnalysisError("data_clean", f"过滤条件执行失败: {condition}, 错误: {e}") from e


def _apply_select_columns(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    columns = params.get("columns")
    if not columns or not isinstance(columns, list):
        raise DataAnalysisError("data_clean", "select_columns 需要 columns 参数")
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise DataAnalysisError("data_clean", f"列不存在: {missing}")
    return df[columns]


def _apply_normalize(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    columns = params.get("columns")
    method = params.get("method", "min_max")
    if not columns:
        raise DataAnalysisError("data_clean", "normalize 需要 columns 参数")

    for col in columns:
        if col not in df.columns:
            continue
        if method == "min_max":
            col_min, col_max = df[col].min(), df[col].max()
            if col_max > col_min:
                df[col] = (df[col] - col_min) / (col_max - col_min)
        elif method == "z_score":
            mean, std = df[col].mean(), df[col].std()
            if std > 0:
                df[col] = (df[col] - mean) / std
        else:
            raise DataAnalysisError("data_clean", f"不支持的归一化方法: {method}")

    return df


def _apply_convert_type(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    column = params.get("column")
    dtype = params.get("dtype")
    if not column or not dtype:
        raise DataAnalysisError("data_clean", "convert_type 需要 column 和 dtype 参数")
    if column not in df.columns:
        raise DataAnalysisError("data_clean", f"列不存在: {column}")
    try:
        df[column] = df[column].astype(dtype)
    except Exception as e:
        raise DataAnalysisError("data_clean", f"类型转换失败: {column} -> {dtype}: {e}") from e
    return df


def _apply_strip_whitespace(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    columns = params.get("columns")
    target_cols = columns if columns else df.select_dtypes(include=["object", "string"]).columns.tolist()
    for col in target_cols:
        if col in df.columns and hasattr(df[col], "str"):
            df[col] = df[col].str.strip()
    return df


def _apply_replace(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    column = params.get("column")
    old_val = params.get("old")
    new_val = params.get("new")
    if not column or old_val is None or new_val is None:
        raise DataAnalysisError("data_clean", "replace 需要 column, old, new 参数")
    if column not in df.columns:
        raise DataAnalysisError("data_clean", f"列不存在: {column}")
    df[column] = df[column].replace(old_val, new_val)
    return df


def _apply_sort(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    columns = params.get("columns")
    ascending = params.get("ascending", True)
    if not columns:
        raise DataAnalysisError("data_clean", "sort 需要 columns 参数")
    return df.sort_values(by=columns, ascending=ascending)


def _apply_group_aggregate(df: pd.DataFrame, params: dict[str, object]) -> pd.DataFrame:
    group_by = params.get("group_by")
    agg_dict = params.get("agg_dict")
    # 兜底：LLM可能将聚合字段直接放在op dict外层而非嵌套在agg_dict里
    # 如 {"op": "group_aggregate", "group_by": "city", "sales": "sum"}
    # 而非 {"op": "group_aggregate", "group_by": "city", "agg_dict": {"sales": "sum"}}
    if not agg_dict and group_by:
        known_keys = {"op", "group_by", "agg_dict"}
        agg_dict = {k: v for k, v in params.items() if k not in known_keys}
    if not group_by or not agg_dict:
        raise DataAnalysisError("data_clean", "group_aggregate 需要 group_by 和 agg_dict 参数，如: {'op':'group_aggregate','group_by':'city','agg_dict':{'sales':'sum'}}")
    try:
        return df.groupby(group_by).agg(agg_dict).reset_index()
    except Exception as e:
        raise DataAnalysisError("data_clean", f"分组聚合失败: {e}") from e


# 操作分发表
_OP_HANDLERS: dict[str, object] = {
    "fill_na": _apply_fill_na,
    "drop_na": _apply_drop_na,
    "drop_duplicates": _apply_drop_duplicates,
    "rename_columns": _apply_rename_columns,
    "filter": _apply_filter,
    "select_columns": _apply_select_columns,
    "normalize": _apply_normalize,
    "convert_type": _apply_convert_type,
    "strip_whitespace": _apply_strip_whitespace,
    "replace": _apply_replace,
    "sort": _apply_sort,
    "group_aggregate": _apply_group_aggregate,
}


def _run_clean(df: pd.DataFrame, operations: list[dict[str, object]]) -> tuple[pd.DataFrame, list[str]]:
    """按顺序执行清洗操作，返回 (结果DataFrame, 操作描述列表)。"""
    applied: list[str] = []
    for i, op_dict in enumerate(operations):
        op_name = op_dict.get("op")
        if not op_name or op_name not in _OP_HANDLERS:
            raise DataAnalysisError("data_clean", f"不支持的操作: {op_name}，可用: {list(_OP_HANDLERS.keys())}")

        handler = _OP_HANDLERS[op_name]
        df = handler(df, op_dict)  # type: ignore[operator]
        applied.append(str(op_name))
        logger.info("[data_clean] 操作 %d/%d: %s 完成, shape=%s", i + 1, len(operations), op_name, df.shape)

    return df, applied


async def data_clean(
    data_key: str,
    operations: list[dict[str, object]],
    output_key: str | None = None,
    data_context: DataContext | None = None,
) -> str:
    """数据清洗工具。对 DataContext 中的 DataFrame 执行清洗/转换操作。

    Args:
        data_key: DataContext 中的数据 key
        operations: 清洗操作列表，每个操作为 dict，如 [{"op": "fill_na", "value": 0}]
        output_key: 输出 key，为空则覆盖原数据
        data_context: 共享数据上下文

    Returns:
        描述清洗结果的中文字符串
    """
    if data_context is None:
        raise DataAnalysisError("data_clean", "DataContext 未提供")

    # LLM 可能将 operations 序列化为 JSON 字符串
    if isinstance(operations, str):
        try:
            operations = json.loads(operations)
        except (json.JSONDecodeError, TypeError) as e:
            raise DataAnalysisError("data_clean", f"operations 参数解析失败: {e}") from e

    if not operations or not isinstance(operations, list):
        raise DataAnalysisError("data_clean", "operations 必须为非空列表")

    if len(operations) > settings.data_clean_max_operations:
        raise DataAnalysisError("data_clean", f"操作数 {len(operations)} 超过限制 {settings.data_clean_max_operations}")

    df = data_context.get(data_key)
    if df is None:
        raise DataAnalysisError("data_clean", f"DataContext 中不存在 key: {data_key}，可用: {data_context.list_keys()}")

    rows_before = len(df)
    result_df, applied = _run_clean(df.copy(), operations)
    rows_after = len(result_df)

    store_key = output_key or data_key
    await data_context.put(store_key, result_df, meta={"tool": "data_clean", "source_key": data_key, "operations": applied})

    logger.info("[data_clean] 清洗完成: %s -> %s, %d->%d 行, 操作: %s", data_key, store_key, rows_before, rows_after, applied)
    return f"数据清洗完成，共执行 {len(applied)} 个操作({', '.join(applied)})。数据行数: {rows_before} -> {rows_after}，结果已存为 '{store_key}'。"


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_data_clean_tool(data_context: DataContext) -> FunctionTool:
    """创建数据清洗 FunctionTool，闭包捕获 data_context。"""

    async def _clean(
        data_key: str,
        operations: list[dict[str, object]],
        output_key: str | None = None,
    ) -> str:
        return await data_clean(data_key, operations, output_key, data_context)

    return FunctionTool(
        func=_clean,
        name="data_clean",
        description="数据清洗工具。对DataContext中的DataFrame执行清洗/转换操作。参数：data_key(数据key), operations(操作列表，如[{'op':'fill_na','value':0}]), output_key(输出key)",
    )