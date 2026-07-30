
"""时序预测工具：基于 statsmodels 的 ARIMA / Holt-Winters 预测。

从 DataContext 读取时序数据，生成未来 periods 期的预测值 + 置信区间，
结果写回 DataContext 供后续 Agent 使用。
"""

from __future__ import annotations

import asyncio
import logging
import warnings
from typing import Any

import numpy as np
import pandas as pd

from autogen_core.tools import FunctionTool

from config.exceptions import DataAnalysisError
from config.settings import settings
from core.data_context import DataContext

logger = logging.getLogger(__name__)

_VALID_METHODS = {"auto", "arima", "holt_winters"}


def _validate_input(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    method: str,
) -> None:
    """校验输入参数。"""
    if method not in _VALID_METHODS:
        raise DataAnalysisError("time_series_forecast", f"不支持的方法: {method}，可选: {', '.join(sorted(_VALID_METHODS))}")
    if date_col not in df.columns:
        raise DataAnalysisError("time_series_forecast", f"日期列 '{date_col}' 不存在，可用列: {list(df.columns)}")
    if value_col not in df.columns:
        raise DataAnalysisError("time_series_forecast", f"数值列 '{value_col}' 不存在，可用列: {list(df.columns)}")
    if len(df) < 3:
        raise DataAnalysisError("time_series_forecast", f"数据量不足（{len(df)}行），至少需要3行数据")


def _preprocess(df: pd.DataFrame, date_col: str, value_col: str) -> pd.Series:
    """预处理时序数据：解析日期、排序、填充缺失值。"""
    # 解析日期列
    dates = pd.to_datetime(df[date_col], errors="coerce")
    if dates.isna().all():
        raise DataAnalysisError("time_series_forecast", f"日期列 '{date_col}' 无法解析为日期格式")

    # 按日期排序
    sorted_idx = dates.argsort()
    values = pd.to_numeric(df.loc[sorted_idx, value_col], errors="coerce")

    # 创建带日期索引的 Series
    series = pd.Series(values.values, index=dates.iloc[sorted_idx].values, name=value_col)
    series = series.sort_index()

    # 填充缺失值：线性插值
    if series.isna().any():
        series = series.interpolate(method="linear")
    # 如果首尾有 NaN，前向/后向填充
    series = series.ffill().bfill()

    if series.isna().all():
        raise DataAnalysisError("time_series_forecast", f"数值列 '{value_col}' 全为无效值")

    return series


def _forecast_arima(series: pd.Series, periods: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ARIMA 预测，返回 (预测值, 置信下界, 置信上界)。"""
    from statsmodels.tsa.arima.model import ARIMA

    # 启发式定阶
    n = len(series)
    p = min(3, n // 3)
    d = 1 if not _is_stationary(series) else 0
    q = min(2, n // 4)

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = ARIMA(series, order=(p, d, q))
        result = model.fit()

    forecast = result.get_forecast(steps=periods)
    pred = forecast.predicted_mean.values
    ci = forecast.conf_int(alpha=0.05)
    lower = ci.iloc[:, 0].values
    upper = ci.iloc[:, 1].values

    return pred, lower, upper


def _forecast_holt_winters(series: pd.Series, periods: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Holt-Winters 预测，返回 (预测值, 置信下界, 置信上界)。"""
    from statsmodels.tsa.holtwinters import ExponentialSmoothing

    n = len(series)
    # 检测季节性：至少2个完整周期
    seasonal = None
    seasonal_periods = None
    if n >= 14:
        seasonal = "add"
        seasonal_periods = 7  # 默认周周期

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = ExponentialSmoothing(
            series,
            trend="add",
            seasonal=seasonal,
            seasonal_periods=seasonal_periods,
        )
        result = model.fit()

    pred = result.forecast(periods).values

    # Holt-Winters 没有内置置信区间，用残差标准差估算
    residuals = result.resid
    std = residuals.std()
    lower = pred - 1.96 * std
    upper = pred + 1.96 * std

    return pred, lower, upper


def _is_stationary(series: pd.Series) -> bool:
    """简单的平稳性检测（ADF 近似）。"""
    try:
        from statsmodels.tsa.stattools import adfuller

        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            result = adfuller(series.dropna(), autolag="AIC")
        return result[1] < 0.05
    except Exception:
        # 如果 ADF 失败，假设非平稳
        return False


def _run_forecast(
    df: pd.DataFrame,
    date_col: str,
    value_col: str,
    periods: int,
    method: str,
) -> pd.DataFrame:
    """执行时序预测，返回结果 DataFrame。"""
    series = _preprocess(df, date_col, value_col)

    if method == "auto":
        # 先试 Holt-Winters，失败则回退 ARIMA
        try:
            pred, lower, upper = _forecast_holt_winters(series, periods)
            used_method = "holt_winters"
        except Exception:
            pred, lower, upper = _forecast_arima(series, periods)
            used_method = "arima"
    elif method == "arima":
        pred, lower, upper = _forecast_arima(series, periods)
        used_method = "arima"
    elif method == "holt_winters":
        pred, lower, upper = _forecast_holt_winters(series, periods)
        used_method = "holt_winters"
    else:
        raise DataAnalysisError("time_series_forecast", f"不支持的方法: {method}")

    # 构建结果 DataFrame
    # 历史数据
    actual_df = pd.DataFrame({
        "date": series.index,
        "value": series.values,
        "type": "actual",
        "lower": np.nan,
        "upper": np.nan,
    })

    # 预测数据
    freq = pd.infer_freq(series.index)
    if freq is None:
        freq = "D"  # 默认按天
    future_dates = pd.date_range(start=series.index[-1], periods=periods + 1, freq=freq)[1:]

    forecast_df = pd.DataFrame({
        "date": future_dates,
        "value": pred,
        "type": "forecast",
        "lower": lower,
        "upper": upper,
    })

    result = pd.concat([actual_df, forecast_df], ignore_index=True)
    result["date"] = result["date"].astype(str)

    logger.info("[时序预测] 方法=%s, 历史数据=%d行, 预测%d期", used_method, len(series), periods)
    return result


async def time_series_forecast(
    data_key: str,
    date_col: str,
    value_col: str,
    periods: int = 7,
    method: str = "auto",
    data_context: DataContext | None = None,
) -> str:
    """时序预测工具。

    从 DataContext 读取时序数据，基于 statsmodels 生成未来 periods 期的预测。

    Args:
        data_key: DataContext 中的数据 key
        date_col: 日期列名
        value_col: 数值列名
        periods: 预测期数，默认7
        method: 预测方法 auto/arima/holt_winters，默认 auto
        data_context: 共享数据上下文

    Returns:
        描述预测结果的中文字符串
    """
    if data_context is None:
        raise DataAnalysisError("time_series_forecast", "DataContext 未提供")

    df = data_context.get(data_key)
    if df is None:
        raise DataAnalysisError("time_series_forecast", f"DataContext 中不存在 key: {data_key}，可用: {data_context.list_keys()}")

    _validate_input(df, date_col, value_col, method)

    try:
        result_df = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None, _run_forecast, df, date_col, value_col, periods, method
            ),
            timeout=settings.data_analysis_timeout,
        )
    except asyncio.TimeoutError as e:
        raise DataAnalysisError("time_series_forecast", f"预测超时（{settings.data_analysis_timeout}s）") from e
    except DataAnalysisError:
        raise
    except Exception as e:
        raise DataAnalysisError("time_series_forecast", f"预测执行失败: {e}") from e

    # 写回 DataContext
    key = data_context.generate_key("DataAnalysisAgent")
    await data_context.put(key, result_df, meta={"tool": "time_series_forecast", "method": method, "periods": periods})
    summary = data_context.summarize(key)

    # 生成摘要
    forecast_rows = result_df[result_df["type"] == "forecast"]
    if len(forecast_rows) > 0:
        first_val = forecast_rows.iloc[0]["value"]
        last_val = forecast_rows.iloc[-1]["value"]
        trend = "上升" if last_val > first_val else "下降" if last_val < first_val else "持平"
        trend_info = f"预测趋势: {trend}（{first_val:.2f} → {last_val:.2f}）"
    else:
        trend_info = ""

    return f"时序预测完成，预测{periods}期数据。{trend_info}{summary}"


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_time_series_forecast_tool(data_context: DataContext) -> FunctionTool:
    """创建时序预测 FunctionTool，闭包捕获 data_context。"""

    async def _forecast(
        data_key: str,
        date_col: str,
        value_col: str,
        periods: int = 7,
        method: str = "auto",
    ) -> str:
        return await time_series_forecast(data_key, date_col, value_col, periods, method, data_context)

    return FunctionTool(
        func=_forecast,
        name="time_series_forecast",
        description="时序预测工具。基于历史时序数据预测未来趋势。支持 ARIMA 和 Holt-Winters 方法。参数：data_key(数据key), date_col(日期列名), value_col(数值列名), periods(预测期数,默认7), method(auto/arima/holt_winters,默认auto)",
    )