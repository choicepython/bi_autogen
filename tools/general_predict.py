
"""通用预测工具：基于 scikit-learn 的回归/分类模型。

从 DataContext 读取数据，训练模型并生成预测，结果写回 DataContext。
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

_VALID_TASK_TYPES = {"auto", "regression", "classification"}
_VALID_MODEL_TYPES = {"auto", "random_forest", "gradient_boosting", "linear"}


def _validate_input(
    df: pd.DataFrame,
    target_col: str,
    task_type: str,
    model_type: str,
) -> None:
    """校验输入参数。"""
    if target_col not in df.columns:
        raise DataAnalysisError("general_predict", f"目标列 '{target_col}' 不存在，可用列: {list(df.columns)}")
    if task_type not in _VALID_TASK_TYPES:
        raise DataAnalysisError("general_predict", f"不支持的任务类型: {task_type}，可选: {sorted(_VALID_TASK_TYPES)}")
    if model_type not in _VALID_MODEL_TYPES:
        raise DataAnalysisError("general_predict", f"不支持的模型类型: {model_type}，可选: {sorted(_VALID_MODEL_TYPES)}")
    if len(df) < 5:
        raise DataAnalysisError("general_predict", f"数据量不足（{len(df)}行），至少需要5行数据")


def _infer_task_type(y: pd.Series) -> str:
    """自动推断任务类型：数值型 → regression，分类型 → classification。"""
    # 如果是数值且唯一值较多 → regression
    if pd.api.types.is_numeric_dtype(y):
        n_unique = y.nunique()
        if n_unique > 10 or n_unique / len(y) > 0.2:
            return "regression"
    # 唯一值较少 → classification
    return "classification"


def _prepare_features(
    df: pd.DataFrame,
    target_col: str,
    feature_cols: list[str] | None,
) -> tuple[pd.DataFrame, pd.Series, list[str]]:
    """特征工程：选择列、填充缺失值、编码分类列。"""
    from sklearn.preprocessing import LabelEncoder

    # 自动选择特征列
    if feature_cols is None or len(feature_cols) == 0:
        feature_cols = [c for c in df.select_dtypes(include=[np.number]).columns if c != target_col]
        if not feature_cols:
            # 如果没有数值列，尝试用分类列
            feature_cols = [c for c in df.columns if c != target_col]

    if not feature_cols:
        raise DataAnalysisError("general_predict", "没有可用的特征列")

    X = df[feature_cols].copy()
    y = df[target_col].copy()

    # 处理特征列
    encoders: dict[str, LabelEncoder] = {}
    for col in feature_cols:
        if pd.api.types.is_numeric_dtype(X[col]):
            X[col] = X[col].fillna(X[col].mean())
        else:
            le = LabelEncoder()
            X[col] = X[col].astype(str)
            X[col] = le.fit_transform(X[col])
            encoders[col] = le

    # 处理目标列（分类任务）
    if not pd.api.types.is_numeric_dtype(y):
        le_y = LabelEncoder()
        y = le_y.fit_transform(y.astype(str))
        y = pd.Series(y, index=df.index)

    return X, y, feature_cols


def _get_model(task_type: str, model_type: str) -> Any:
    """根据任务类型和模型类型创建模型。"""
    from sklearn.ensemble import (
        GradientBoostingClassifier,
        GradientBoostingRegressor,
        RandomForestClassifier,
        RandomForestRegressor,
    )
    from sklearn.linear_model import LinearRegression, LogisticRegression

    actual_model = model_type
    if model_type == "auto":
        actual_model = "random_forest"

    if task_type == "regression":
        if actual_model == "random_forest":
            return RandomForestRegressor(n_estimators=100, random_state=42, max_depth=10)
        elif actual_model == "gradient_boosting":
            return GradientBoostingRegressor(n_estimators=100, random_state=42, max_depth=5)
        elif actual_model == "linear":
            return LinearRegression()
    else:  # classification
        if actual_model == "random_forest":
            return RandomForestClassifier(n_estimators=100, random_state=42, max_depth=10)
        elif actual_model == "gradient_boosting":
            return GradientBoostingClassifier(n_estimators=100, random_state=42, max_depth=5)
        elif actual_model == "linear":
            return LogisticRegression(max_iter=1000, random_state=42)

    raise DataAnalysisError("general_predict", f"不支持的组合: {task_type} + {actual_model}")


def _run_predict(
    df: pd.DataFrame,
    target_col: str,
    feature_cols: list[str] | None,
    task_type: str,
    model_type: str,
) -> tuple[pd.DataFrame, pd.DataFrame, str, dict[str, Any]]:
    """执行通用预测，返回 (预测结果DF, 特征重要性DF, 实际task_type, 模型指标)。"""
    from sklearn.metrics import accuracy_score, mean_absolute_error, mean_squared_error, r2_score
    from sklearn.model_selection import train_test_split

    _validate_input(df, target_col, task_type, model_type)

    X, y, used_features = _prepare_features(df, target_col, feature_cols)

    # 推断任务类型
    actual_task = task_type
    if task_type == "auto":
        actual_task = _infer_task_type(y if isinstance(y, pd.Series) else pd.Series(y))

    # 划分训练/测试集
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2, random_state=42)

    # 创建并训练模型
    model = _get_model(actual_task, model_type)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model.fit(X_train, y_train)

    # 预测
    y_pred = model.predict(X)

    # 计算指标
    metrics: dict[str, Any] = {}
    y_test_pred = model.predict(X_test)
    if actual_task == "regression":
        metrics["r2"] = round(r2_score(y_test, y_test_pred), 4)
        metrics["rmse"] = round(np.sqrt(mean_squared_error(y_test, y_test_pred)), 4)
        metrics["mae"] = round(mean_absolute_error(y_test, y_test_pred), 4)
    else:
        metrics["accuracy"] = round(accuracy_score(y_test, y_test_pred), 4)

    # 特征重要性
    importance_df = _get_feature_importance(model, used_features, actual_task)

    # 预测结果
    result = df.copy()
    result["predicted"] = np.round(y_pred, 4) if actual_task == "regression" else y_pred

    logger.info("[通用预测] 任务=%s, 模型=%s, 特征数=%d, 指标=%s",
                actual_task, model_type, len(used_features), metrics)

    return result, importance_df, actual_task, metrics


def _get_feature_importance(model: Any, feature_names: list[str], task_type: str) -> pd.DataFrame:
    """提取特征重要性。"""
    importances = None

    if hasattr(model, "feature_importances_"):
        importances = model.feature_importances_
    elif hasattr(model, "coef_"):
        coef = model.coef_
        if coef.ndim > 1:
            importances = np.mean(np.abs(coef), axis=0)
        else:
            importances = np.abs(coef)

    if importances is None:
        return pd.DataFrame({"feature": feature_names, "importance": [0.0] * len(feature_names)})

    # 归一化到 [0, 1]
    total = importances.sum()
    if total > 0:
        importances = importances / total

    df = pd.DataFrame({
        "feature": feature_names,
        "importance": np.round(importances, 4),
    }).sort_values("importance", ascending=False).reset_index(drop=True)

    return df


async def general_predict(
    data_key: str,
    target_col: str,
    feature_cols: list[str] | None = None,
    task_type: str = "auto",
    model_type: str = "auto",
    data_context: DataContext | None = None,
) -> str:
    """通用预测工具。

    从 DataContext 读取数据，训练 scikit-learn 模型并生成预测。

    Args:
        data_key: DataContext 中的数据 key
        target_col: 目标列名
        feature_cols: 特征列名列表，为空则自动选数值列
        task_type: 任务类型 auto/regression/classification
        model_type: 模型类型 auto/random_forest/gradient_boosting/linear
        data_context: 共享数据上下文

    Returns:
        描述预测结果的中文字符串
    """
    if data_context is None:
        raise DataAnalysisError("general_predict", "DataContext 未提供")

    df = data_context.get(data_key)
    if df is None:
        raise DataAnalysisError("general_predict", f"DataContext 中不存在 key: {data_key}，可用: {data_context.list_keys()}")

    try:
        result_df, importance_df, actual_task, metrics = await asyncio.wait_for(
            asyncio.get_event_loop().run_in_executor(
                None, _run_predict, df, target_col, feature_cols, task_type, model_type
            ),
            timeout=settings.data_analysis_timeout,
        )
    except asyncio.TimeoutError as e:
        raise DataAnalysisError("general_predict", f"预测超时（{settings.data_analysis_timeout}s）") from e
    except DataAnalysisError:
        raise
    except Exception as e:
        raise DataAnalysisError("general_predict", f"预测执行失败: {e}") from e

    # 写回 DataContext — 预测结果
    key = data_context.generate_key("DataAnalysisAgent")
    await data_context.put(key, result_df, meta={"tool": "general_predict", "task_type": actual_task, "metrics": metrics})

    # 写回 DataContext — 特征重要性
    imp_key = f"{key}_feature_importance"
    await data_context.put(imp_key, importance_df, meta={"tool": "general_predict", "type": "feature_importance"})

    summary = data_context.summarize(key)

    # 构建指标描述
    if actual_task == "regression":
        metric_str = f"R²={metrics.get('r2', 'N/A')}, RMSE={metrics.get('rmse', 'N/A')}, MAE={metrics.get('mae', 'N/A')}"
    else:
        metric_str = f"Accuracy={metrics.get('accuracy', 'N/A')}"

    # Top 特征
    top_features = importance_df.head(3)["feature"].tolist()
    top_str = "、".join(top_features) if top_features else "无"

    return f"通用预测完成（{actual_task}），{metric_str}，Top特征: {top_str}。{summary}"


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_general_predict_tool(data_context: DataContext) -> FunctionTool:
    """创建通用预测 FunctionTool，闭包捕获 data_context。"""

    async def _predict(
        data_key: str,
        target_col: str,
        feature_cols: list[str] | None = None,
        task_type: str = "auto",
        model_type: str = "auto",
    ) -> str:
        return await general_predict(data_key, target_col, feature_cols, task_type, model_type, data_context)

    return FunctionTool(
        func=_predict,
        name="general_predict",
        description="通用预测工具。基于特征预测目标变量，支持回归和分类。参数：data_key(数据key), target_col(目标列名), feature_cols(特征列名列表,为空则自动选数值列), task_type(auto/regression/classification,默认auto), model_type(auto/random_forest/gradient_boosting/linear,默认auto)",
    )