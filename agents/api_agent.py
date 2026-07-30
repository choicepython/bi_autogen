
from __future__ import annotations

import logging
from typing import ClassVar

from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent, ContextSpec
from core.context import SessionContext
from core.skill_manager import get_skill_manager
from core.data_context import DataContext
from tools.api_query import DynamicAPITool

logger = logging.getLogger(__name__)


class APIAgent(BIBaseAgent):
    """API调用助手，擅长通过调用API获取业务数据。

    每个 API 动态注册为独立的 DynamicAPITool，LLM 通过 function calling
    直接调用带完整参数 schema 的工具，而非通过泛化工具传递 API 名和参数。
    """

    context_spec: ClassVar[ContextSpec] = ContextSpec()
    thinking_enabled: ClassVar[bool] = False
    prompt_template: ClassVar[str] = "api_agent"
    agent_description: ClassVar[str] = "API调用专家"

    def __init__(
        self,
        model_client: ChatCompletionClient,
        data_context: DataContext,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
    ) -> None:
        # 从 SessionContext 获取 api_meta，动态创建 per-API tools
        api_meta = session.api_meta if session and session.api_meta else []
        custom_env = session.business.custom_env if session else None
        user_id = session.user.user_id if session else ""
        tools: list[DynamicAPITool] = [
            DynamicAPITool(meta, data_context, custom_env=custom_env, user_id=user_id) for meta in api_meta
        ]

        super().__init__(
            model_client=model_client,
            data_context=data_context,
            session=session,
            task_id=task_id,
            task_description=task_description,
            tools=tools,
            reflect_on_tool_use=True,
            max_tool_iterations=5,
        )

        logger.info("[APIAgent] 注册了 %d 个API工具", len(tools))

    def _extra_prompt_vars(self) -> dict[str, str]:
        """注入业务技能到 prompt。"""
        if self.session and self.session.skills:
            skills_text = get_skill_manager().format_skills_for_prompt(self.session.skills)
            return {"SKILLS": skills_text}
        return {"SKILLS": ""}