
"""PyFuncAgent：Python计算助手，擅长使用Python代码进行自定义数据处理和复杂计算。

注册 python_exec 工具，LLM 通过 function calling 调用工具执行代码。
执行失败时 AutoGen 自动将错误反馈给 LLM 重试（reflect_on_tool_use=True, max_tool_iterations=3）。
工具工厂函数定义在 tools/python_exec.py 中，Agent 通过组合方式使用。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent, ContextSpec
from core.context import SessionContext
from core.skill_manager import get_skill_manager
from core.data_context import DataContext
from tools.python_exec import make_python_exec_tool

logger = logging.getLogger(__name__)


class PyFuncAgent(BIBaseAgent):
    """Python计算助手，擅长使用Python代码进行自定义数据处理和复杂计算。"""

    context_spec: ClassVar[ContextSpec] = ContextSpec(data_catalog="full")
    prompt_template: ClassVar[str] = "pyfunc_agent"
    agent_description: ClassVar[str] = "Python计算专家"

    def __init__(
        self,
        model_client: ChatCompletionClient,
        data_context: DataContext,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
    ) -> None:
        # 注册 python_exec 工具（工厂函数定义在 tools/python_exec.py 中）
        python_tool = make_python_exec_tool(data_context)

        super().__init__(
            model_client=model_client,
            data_context=data_context,
            session=session,
            task_id=task_id,
            task_description=task_description,
            tools=[python_tool],
            reflect_on_tool_use=True,
            max_tool_iterations=3,
        )

        logger.info("[PyFuncAgent] 已注册 python_exec 工具")

    def _extra_prompt_vars(self) -> dict[str, str]:
        """注入业务技能到 prompt。"""
        if self.session and self.session.skills:
            skills_text = get_skill_manager().format_skills_for_prompt(self.session.skills)
            return {"SKILLS": skills_text}
        return {"SKILLS": ""}