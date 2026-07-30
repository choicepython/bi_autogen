
"""DataAnalysisAgent：数据分析助手，擅长数据导入、清洗、文字分析、时序预测、异常检测和通用预测。

注册6个工具：data_ingest, data_clean, data_summarize, time_series_forecast, anomaly_detect, general_predict。
工具工厂函数定义在各 tools/ 模块中，Agent 通过组合方式使用。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent, ContextSpec
from core.context import SessionContext
from core.data_context import DataContext
from tools.anomaly_detect import make_anomaly_detect_tool
from tools.data_clean import make_data_clean_tool
from tools.data_ingest import make_data_ingest_tool
from tools.data_summarize import make_data_summarize_tool
from tools.general_predict import make_general_predict_tool
from tools.time_series_forecast import make_time_series_forecast_tool

logger = logging.getLogger(__name__)


class DataAnalysisAgent(BIBaseAgent):
    """数据分析助手，擅长数据导入、清洗、文字分析、时序预测、异常检测和通用预测。"""

    context_spec: ClassVar[ContextSpec] = ContextSpec()
    prompt_template: ClassVar[str] = "data_analysis_agent"
    agent_description: ClassVar[str] = "数据分析专家"

    def __init__(
        self,
        model_client: ChatCompletionClient,
        data_context: DataContext,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
    ) -> None:
        # 注册6个工具（工厂函数定义在各 tools/ 模块中）
        tools = [
            make_data_ingest_tool(data_context),
            make_data_clean_tool(data_context),
            make_data_summarize_tool(data_context),
            make_time_series_forecast_tool(data_context),
            make_anomaly_detect_tool(data_context),
            make_general_predict_tool(data_context),
        ]

        super().__init__(
            model_client=model_client,
            data_context=data_context,
            session=session,
            task_id=task_id,
            task_description=task_description,
            tools=tools,
            reflect_on_tool_use=True,
        )

        logger.info("[DataAnalysisAgent] 已注册6个工具: data_ingest, data_clean, data_summarize, time_series_forecast, anomaly_detect, general_predict")

    def _extra_prompt_vars(self) -> dict[str, str]:
        """注入 DataContext 摘要。"""
        if self.data_context:
            dc_summary = self.data_context.all_summaries()
            return {"DATA_CONTEXT": dc_summary or "（DataContext 当前为空）"}
        return {"DATA_CONTEXT": "（DataContext 当前为空）"}