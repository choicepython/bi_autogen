
from __future__ import annotations

import logging
from typing import ClassVar

from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent, ContextSpec
from core.context import SessionContext
from core.data_context import DataContext
from tools.report_generate import report_generate

logger = logging.getLogger(__name__)


class ReportAgent(BIBaseAgent):
    """报告生成助手，擅长将分析结果和数据整理为结构化报告。"""

    context_spec: ClassVar[ContextSpec] = ContextSpec(query_history="full", conclusions="all")
    prompt_template: ClassVar[str] = "report_agent"
    agent_description: ClassVar[str] = "报告生成专家"

    def __init__(
        self,
        model_client: ChatCompletionClient,
        data_context: DataContext,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
    ) -> None:
        super().__init__(
            model_client=model_client,
            data_context=data_context,
            session=session,
            task_id=task_id,
            task_description=task_description,
            tools=[self._call_report_generate],
            reflect_on_tool_use=True,
        )

    def _extra_prompt_vars(self) -> dict[str, str]:
        """注入 DataContext 摘要。"""
        if self.data_context:
            return {"DATA_CONTEXT": self.data_context.all_summaries()}
        return {"DATA_CONTEXT": ""}

    async def _call_report_generate(self, report_json: str) -> str:
        """生成格式化的报告文件。

        Args:
            report_json: 报告内容的JSON字符串，格式需符合ReportContent模型。
        """
        try:
            return await report_generate(report_json, self.data_context)
        except Exception as e:
            logger.error("report_generate tool error: %s", e)
            return f"报告生成失败: {e}"