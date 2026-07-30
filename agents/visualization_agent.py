
"""VisualizationAgent：可视化助手，擅长生成图表和仪表板。

注册2个工具：chart_generate, dashboard_generate。
工具工厂函数定义在 tools/chart_generate.py、tools/dashboard_generate.py 中，Agent 通过组合方式使用。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent, ContextSpec
from core.context import SessionContext
from core.data_context import DataContext
from tools.chart_generate import make_chart_generate_tool
from tools.dashboard_generate import make_dashboard_generate_tool

logger = logging.getLogger(__name__)


class VisualizationAgent(BIBaseAgent):
    """可视化助手，擅长生成图表和仪表板。"""

    context_spec: ClassVar[ContextSpec] = ContextSpec(data_catalog="full", conclusions="all")
    thinking_enabled: ClassVar[bool] = False
    prompt_template: ClassVar[str] = "visualization_agent"
    agent_description: ClassVar[str] = "可视化专家"

    def __init__(
        self,
        model_client: ChatCompletionClient,
        data_context: DataContext,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
    ) -> None:
        # 注册2个工具（工厂函数定义在各 tools/ 模块中）
        tools = [
            make_chart_generate_tool(data_context),
            make_dashboard_generate_tool(data_context),
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

        logger.info("[VisualizationAgent] 已注册2个工具: chart_generate, dashboard_generate")

    def _extra_prompt_vars(self) -> dict[str, str]:
        """注入 DataContext 摘要和图表上下文。"""
        if self.data_context:
            dc_summary = self.data_context.all_summaries()
            chart_summary = self.data_context.chart_summaries()
            return {
                "DATA_CONTEXT": dc_summary or "（DataContext 当前为空）",
                "CHART_CONTEXT": chart_summary or "（当前没有图表）",
            }
        return {"DATA_CONTEXT": "（DataContext 当前为空）", "CHART_CONTEXT": "（当前没有图表）"}