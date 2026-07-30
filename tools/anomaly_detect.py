
"""异常检测工具：基于 scikit-learn 的 Isolation Forest / Z-Score / IQR 检测。

从 DataContext 读取数据，检测异常点，结果写回 DataContext。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
import pandas as pd

from autogen_core.tools import FunctionTool

from config.exceptions import DataAnalysisError
from config.settings import settings
from core.data_context import DataContext

logger = logging.getLogger(__name__)

_VALID_METHODS = {"isolation_forest", "zscore", "iqr"}


def _validate_input(
    df: pd.DataFrame,
    detect_cols: list[str] | None,
    method: str,
) -> list[str]:
    """校验输入并返回实际检测列。"""
    if method not in _VALID_METHODS:
        raise DataAnalysisError("anomaly_detect", f"不支持的方法: {method}，可选: {', '.join(sorted(_VALID_METHODS))}")

    # 自动选择数值列
    if detect_cols is None or len(detect_cols) == 0:
        numeric_cols = df.select_dtypes(include=[np.number]).columns.tolist()
        if not numeric_cols:
            raise DataAnalysisError("anomaly_detect", "数据中没有数值列，无法进行异常检测")
        return numeric_cols

    # 校验指定列存在
    missing = [c for c in detect_cols if c not in df.columns]
    if missing:
        raise DataAnalysisError("anomaly_detect", f"列不存在: {missing}，可用列: {list(df.columns)}")
    return detect_cols


def _detect_isolation_forest(
    df: pd.DataFrame,
    cols: list[str],
    contamination: float,
) -> tuple[pd.Series, pd.Series]:
    """Isolation Forest 异常检测。返回 (is_anomaly, anomaly_score)。"""
    from sklearn.ensemble import IsolationForest

    X = df[cols].copy()
    # 填充缺失值
    for col in cols:
        X[col] = X[col].fillna(X[col].median())

    clf = IsolationForest(contamination=contamination, random_state=42, n_estimators=100)
    predictions = clf.fit_predict(X)
    scores = clf.decision_function(X)

    # IsolationForest: -1 = 异常, 1 = 正常
    is_anomaly = pd.Series(predictions == -1, index=df.index)
    # 分数越低越异常，归一化到 [0, 1]，0=最异常
    score_min, score_max = scores.min(), scores.max()
    if score_max > score_min:
        normalized = 1 - (scores - score_min) / (score_max - score_min)
    else:
        normalized = np.zeros(len(scores))

    anomaly_score = pd.Series(normalized, index=df.index)
    return is_anomaly, anomaly_score


def _detect_zscore(
    df: pd.DataFrame,
    cols: list[str],
    threshold: float = 3.0,
) -> tuple[pd.Series, pd.Series]:
    """Z-Score 异常检测。|Z| > threshold 标记为异常。"""
    from scipy import stats

    X = df[cols].copy()
    for col in cols:
        X[col] = X[col].fillna(X[col].mean())

    # 计算每行的最大 Z-Score
    z_scores = np.abs(stats.zscore(X, nan_policy="omit"))
    if z_scores.ndim == 1:
        max_z = np.abs(z_scores)
    else:
        max_z = np.max(z_scores, axis=1)

    is_anomaly = pd.Series(max_z > threshold, index=df.index)
    # 分数：Z-Score 归一化到 [0, 1]
    score_min, score_max = max_z.min(), max_z.max()
    if score_max > score_min:
        normalized = (max_z - score_min) / (score_max - score_min)
    else:
        normalized = np.zeros(len(max_z))

    anomaly_score = pd.Series(normalized, index=df.index)
    return is_anomaly, anomaly_score


def _detect_iqr(
    df: pd.DataFrame,
    cols: list[str],
) -> tuple[pd.Series, pd.Series]:
    """IQR 异常检测。Q1 - 1.5*IQR ~ Q3 + 1.5*IQR 为正常范围。"""
    X = df[cols].copy()
    for col in cols:
        X[col] = X[col].fillna(X[col].median())

    is_anomaly = pd.Series(False, index=df.index)
    max_deviation = pd.Series(0.0, index=df.index)

    for col in cols:
        Q1 = X[col].quantile(0.25)
        Q3 = X[col].quantile(0.75)
        IQR = Q3 - Q1
        if IQR == 0:
            continue
        lower = Q1 - 1.5 * IQR
        upper = Q3 + 1.5 * IQR

        col_anomaly = (X[col] < lower) | (X[col] > upper)
        is_anomaly = is_anomaly | col_anomaly

        # 偏离程度：超出范围的距离 / IQR
        deviation = np.maximum(X[col] - upper, lower - X[col]) / IQR
        deviation = deviation.clip(lower=0)
        max_deviation = np.maximum(max_deviation, deviation)

    # 归一化分数
    if max_deviation.max() > 0:
        anomaly_score = max_deviation / max_deviation.max()
    else:
        anomaly_score = max_deviation

    return is_anomaly, anomaly_score


def _run_detect(
    df: pd.DataFrame,
    detect_cols: list[str] | None,
    method: str,
    contamination: float,
) -> pd.DataFrame:
    """执行异常检测，返回带 is_anomaly 和 anomaly_score 列的 DataFrame。"""
    cols = _validate_input(df, detect_cols, method)

    if method == "isolation_forest":
        is_anomaly, anomaly_score = _detect_isolation_forest(df, cols, contamination)
    elif method == "zscore":
        is_anomaly, anomaly_score = _detect_zscore(df, cols)
    elif method == "iqr":
        is_anomaly, anomaly_score = _detect_iqr(df, cols)
    else:
        raise DataAnalysisError("anomaly_detect", f"不支持的方法: {method}")

    result = df.copy()
    result["is_anomaly"] = is_anomaly
    result["anomaly_score"] = anomaly_score.round(4)

    n_anomaly = int(is_anomaly.sum())
    logger.info("[异常检测] 方法=%s, 检测列=%s, 异常数=%d/%d(%.1f%%)",
                method, cols, n_anomaly, len(df), 100 * n_anomaly / len(df) if len(df) > 0 else 0)

    return result


async def anomaly_detect(
    data_key: str,
    detect_cols: list[str] | None = None,
    method: str = "isolation_forest",
    contamination: float = 0.05,
    data_context: DataContext | None = None,
) -> str:
    """异常检测工具。

    从 DataContext 读取数据，检测异常点，结果写回 DataContext。

    Args:
        data_key: DataContext 中的数据 key
        detect_cols: 检测列名列表，为空则自动选所有数值列
        method: 检测方法 isolation_forest/zscore/iqr
        contamination: 异常比例（仅 isolation_forest 使用），默认 0.05
        data_context: 共享数据上下文

    Returns:
        描述检测结果的中文字符串
    """
    if data_context is None:
        raise DataAnalysisError("anomaly_detect", "DataContext 未提供")

    df = data_context.get(data_key)
    if df is None:
        raise DataAnalysisError("anomaly_detect", f"DataContext 中不存在 key: {data_key}，可用: {data_context.list_keys()}")

    try:
        result_df = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None, _run_detect, df, detect_cols, method, contamination
            ),
            timeout=settings.data_analysis_timeout,
        )
    except asyncio.TimeoutError as e:
        raise DataAnalysisError("anomaly_detect", f"检测超时（{settings.data_analysis_timeout}s）") from e
    except DataAnalysisError:
        raise
    except Exception as e:
        raise DataAnalysisError("anomaly_detect", f"检测执行失败: {e}") from e

    # 写回 DataContext
    key = data_context.generate_key("DataAnalysisAgent")
    await data_context.put(key, result_df, meta={"tool": "anomaly_detect", "method": method})

    n_anomaly = int(result_df["is_anomaly"].sum())
    n_total = len(result_df)
    summary = data_context.summarize(key)

    return f"异常检测完成，共{n_total}条数据中检测到{n_anomaly}个异常点（{100 * n_anomaly / n_total:.1f}%）。{summary}"


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_anomaly_detect_tool(data_context: DataContext) -> FunctionTool:
    """创建异常检测 FunctionTool，闭包捕获 data_context。"""

    async def _anomaly(
        data_key: str,
        detect_cols: list[str] | None = None,
        method: str = "isolation_forest",
        contamination: float = 0.05,
    ) -> str:
        return await anomaly_detect(data_key, detect_cols, method, contamination, data_context)

    return FunctionTool(
        func=_anomaly,
        name="anomaly_detect",
        description="异常检测工具。识别数据中的异常点。支持 Isolation Forest、Z-Score、IQR 方法。参数：data_key(数据key), detect_cols(检测列名列表,为空则自动选数值列), method(isolation_forest/zscore/iqr,默认isolation_forest), contamination(异常比例,默认0.05)",
    )