
"""data_summarize: 对DataContext中的DataFrame生成多维度文字分析摘要。"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

from autogen_core.tools import FunctionTool

from config import DataAnalysisError
from core.data_context import DataContext

logger = logging.getLogger(__name__)


async def data_summarize(
    data_key: str,
    aspects: list[str] | None = None,
    data_context: DataContext | None = None,
) -> str:
    """对DataContext中的DataFrame生成多维度文字分析摘要。

    Args:
        data_key: DataContext中的数据key
        aspects: 指定分析维度列表，可选值：overview, key_metrics, distribution,
                 missing_values, correlation, top_values。为空则全部分析。
        data_context: DataContext实例
    """
    if data_context is None:
        raise DataAnalysisError("data_summarize", "DataContext未提供")

    df = data_context.get(data_key)
    if df is None:
        raise DataAnalysisError("data_summarize", f"数据key '{data_key}' 不存在于DataContext中")

    if df.empty:
        return f"数据 '{data_key}' 为空（0行），无法生成摘要。"

    valid_aspects = {"overview", "key_metrics", "distribution", "missing_values", "correlation", "top_values"}
    if aspects:
        aspects = [a for a in aspects if a in valid_aspects]
    if not aspects:
        aspects = list(valid_aspects)

    parts: list[str] = []

    for aspect in aspects:
        try:
            if aspect == "overview":
                parts.append(_overview(df, data_key))
            elif aspect == "key_metrics":
                parts.append(_key_metrics(df))
            elif aspect == "distribution":
                parts.append(_distribution(df))
            elif aspect == "missing_values":
                parts.append(_missing_values(df))
            elif aspect == "correlation":
                parts.append(_correlation(df))
            elif aspect == "top_values":
                parts.append(_top_values(df))
        except Exception as e:
            logger.warning("分析维度 '%s' 失败: %s", aspect, e)
            parts.append(f"## {aspect}\n分析失败: {e}")

    return "\n\n".join(parts)


def _overview(df: pd.DataFrame, data_key: str) -> str:
    """数据概况。"""
    lines = [
        f"## 数据概况",
        f"- 数据集: {data_key}",
        f"- 行数: {len(df)}",
        f"- 列数: {len(df.columns)}",
        f"- 列名: {', '.join(str(c) for c in df.columns)}",
    ]
    # 列类型统计
    dtype_counts = df.dtypes.value_counts()
    type_str = ", ".join(f"{dtype}: {count}" for dtype, count in dtype_counts.items())
    lines.append(f"- 列类型分布: {type_str}")
    # 内存占用
    mem = df.memory_usage(deep=True).sum()
    if mem > 1024 * 1024:
        lines.append(f"- 内存占用: {mem / 1024 / 1024:.1f} MB")
    else:
        lines.append(f"- 内存占用: {mem / 1024:.1f} KB")
    return "\n".join(lines)


def _key_metrics(df: pd.DataFrame) -> str:
    """关键指标：数值列的统计摘要。"""
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if not numeric_cols:
        return "## 关键指标\n无数值列，跳过统计。"

    lines = ["## 关键指标"]
    for col in numeric_cols[:10]:  # 最多10列
        series = df[col].dropna()
        if series.empty:
            lines.append(f"- **{col}**: 全部缺失")
            continue
        mean_val = series.mean()
        median_val = series.median()
        std_val = series.std()
        min_val = series.min()
        max_val = series.max()
        # 格式化数值
        def _fmt(v: object) -> str:
            if isinstance(v, float):
                return f"{v:.2f}"
            return str(v)

        lines.append(
            f"- **{col}**: 均值={_fmt(mean_val)}, 中位数={_fmt(median_val)}, "
            f"标准差={_fmt(std_val)}, 最小={_fmt(min_val)}, 最大={_fmt(max_val)}"
        )
    if len(numeric_cols) > 10:
        lines.append(f"- ... 共{len(numeric_cols)}个数值列，仅展示前10个")
    return "\n".join(lines)


def _distribution(df: pd.DataFrame) -> str:
    """分布特征：分位数和偏度。"""
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if not numeric_cols:
        return "## 分布特征\n无数值列，跳过。"

    lines = ["## 分布特征"]
    for col in numeric_cols[:8]:
        series = df[col].dropna()
        if len(series) < 3:
            continue
        q1 = series.quantile(0.25)
        q3 = series.quantile(0.75)
        iqr = q3 - q1
        skew = series.skew()
        # 简单判断分布形态
        if abs(skew) < 0.5:
            shape = "近似对称"
        elif skew > 0:
            shape = "右偏"
        else:
            shape = "左偏"

        def _fmt(v: object) -> str:
            return f"{v:.2f}" if isinstance(v, float) else str(v)

        lines.append(
            f"- **{col}**: Q1={_fmt(q1)}, Q3={_fmt(q3)}, IQR={_fmt(iqr)}, "
            f"偏度={_fmt(skew)} ({shape})"
        )
    return "\n".join(lines)


def _missing_values(df: pd.DataFrame) -> str:
    """缺失值分析。"""
    missing = df.isnull().sum()
    missing = missing[missing > 0]
    if missing.empty:
        return "## 缺失值\n数据完整，无缺失值。"

    lines = ["## 缺失值"]
    total_cells = df.shape[0] * df.shape[1]
    total_missing = missing.sum()
    lines.append(f"- 总缺失: {total_missing}/{total_cells} ({total_missing / total_cells * 100:.1f}%)")
    for col, count in missing.items():
        pct = count / len(df) * 100
        lines.append(f"- **{col}**: {count}条缺失 ({pct:.1f}%)")
    return "\n".join(lines)


def _correlation(df: pd.DataFrame) -> str:
    """相关性分析：数值列之间的Pearson相关系数。"""
    numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
    if len(numeric_cols) < 2:
        return "## 相关性\n数值列不足2个，跳过。"

    corr = df[numeric_cols].corr()
    # 找出强相关对（|r| > 0.7，排除自身）
    pairs: list[tuple[float, str, str]] = []
    for i, c1 in enumerate(numeric_cols):
        for c2 in numeric_cols[i + 1 :]:
            r = corr.loc[c1, c2]
            if not np.isnan(r):
                pairs.append((abs(r), c1, c2))

    pairs.sort(reverse=True)
    strong = [(r, c1, c2) for r, c1, c2 in pairs if r > 0.7]
    moderate = [(r, c1, c2) for r, c1, c2 in pairs if 0.4 < r <= 0.7]

    lines = ["## 相关性"]
    if strong:
        lines.append("### 强相关 (|r| > 0.7)")
        for r, c1, c2 in strong[:5]:
            actual_r = corr.loc[c1, c2]
            lines.append(f"- **{c1}** ↔ **{c2}**: r={actual_r:.2f}")
    if moderate:
        lines.append("### 中等相关 (0.4 < |r| ≤ 0.7)")
        for r, c1, c2 in moderate[:5]:
            actual_r = corr.loc[c1, c2]
            lines.append(f"- **{c1}** ↔ **{c2}**: r={actual_r:.2f}")
    if not strong and not moderate:
        lines.append("无明显相关关系。")
    return "\n".join(lines)


def _top_values(df: pd.DataFrame) -> str:
    """高频值：每列的Top值统计。"""
    lines = ["## 高频值统计"]
    for col in df.columns[:8]:
        series = df[col].dropna()
        if series.empty:
            continue
        unique_count = series.nunique()
        if unique_count <= 5:
            # 类别型：直接列出所有值
            vc = series.value_counts().head(5)
            items = ", ".join(f"{v}({c})" for v, c in vc.items())
            lines.append(f"- **{col}** (唯一值{unique_count}): {items}")
        else:
            # 高基数：只列Top5
            vc = series.value_counts().head(5)
            items = ", ".join(f"{v}({c})" for v, c in vc.items())
            lines.append(f"- **{col}** (唯一值{unique_count}): Top5: {items}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_data_summarize_tool(data_context: DataContext) -> FunctionTool:
    """创建数据摘要 FunctionTool，闭包捕获 data_context。"""

    async def _summarize(
        data_key: str,
        aspects: list[str] | None = None,
    ) -> str:
        return await data_summarize(data_key, aspects, data_context)

    return FunctionTool(
        func=_summarize,
        name="data_summarize",
        description="数据摘要工具。对DataContext中的DataFrame生成多维度文字分析摘要。参数：data_key(数据key), aspects(分析维度列表，可选：overview/key_metrics/distribution/missing_values/correlation/top_values，为空则全部分析)",
    )