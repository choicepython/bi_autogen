
"""SQLAgent：专职处理 SQL 查询任务的 Agent。

注册 sql_query 工具，LLM 生成 SQL 后通过工具安全执行。
工具工厂函数定义在 tools/sql_query.py 中，Agent 通过组合方式使用。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent, ContextSpec
from core.context import SessionContext
from core.data_context import DataContext
from tools.sql_query import make_sql_query_tool

logger = logging.getLogger(__name__)


class SQLAgent(BIBaseAgent):
    """SQL查询助手，擅长生成和执行安全的SQL查询语句。"""

    context_spec: ClassVar[ContextSpec] = ContextSpec()
    thinking_enabled: ClassVar[bool] = False
    prompt_template: ClassVar[str] = "sql_agent"
    agent_description: ClassVar[str] = "SQL查询专家"

    def __init__(
        self,
        model_client: ChatCompletionClient,
        data_context: DataContext,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
    ) -> None:
        # 注册 sql_query 工具（工厂函数定义在 tools/sql_query.py 中）
        sql_tool = make_sql_query_tool(data_context)

        super().__init__(
            model_client=model_client,
            data_context=data_context,
            session=session,
            task_id=task_id,
            task_description=task_description,
            tools=[sql_tool],
            reflect_on_tool_use=True,
        )

        logger.info("[SQLAgent] 已注册 sql_query 工具")

    def _extra_prompt_vars(self) -> dict[str, str]:
        """注入表信息占位。"""
        return {"TABLE_INFO": "（暂无表信息，请根据用户描述推断表名和字段）"}