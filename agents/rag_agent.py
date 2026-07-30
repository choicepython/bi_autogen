
"""RAGAgent：检索增强生成助手，擅长搜索企业内部知识库、W3社区和互联网信息，并对搜索结果进行归纳总结。

注册3个工具：knowledge_search, w3_search, xiaoyi_search。
搜索结果自动存入DataContext，供下游Agent引用。
与纯搜索不同，RAGAgent 会在检索后对结果进行综合分析和总结回答。
工具工厂函数定义在 tools/search_tools.py 中，Agent 通过组合方式使用。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from autogen_agentchat.messages import (
    BaseChatMessage,
)
from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent, ContextSpec
from core.context import SessionContext
from core.data_context import DataContext
from tools.search_tools import (
    make_knowledge_search_tool,
    make_w3_search_tool,
    make_xiaoyi_search_tool,
)

logger = logging.getLogger(__name__)


class RAGAgent(BIBaseAgent):
    """检索增强生成助手，擅长搜索并总结企业内部知识库、W3社区和互联网信息。"""

    context_spec: ClassVar[ContextSpec] = ContextSpec(query_history="full", data_catalog="none")
    thinking_enabled: ClassVar[bool] = False
    prompt_template: ClassVar[str] = "rag_agent"
    agent_description: ClassVar[str] = "知识检索与总结专家"

    def __init__(
            self,
            model_client: ChatCompletionClient,
            data_context: DataContext,
            session: SessionContext | None = None,
            task_id: str | None = None,
            task_description: str | None = None,
    ) -> None:
        # 注册3个工具（工厂函数定义在 tools/search_tools.py 中）
        tools = [
            make_knowledge_search_tool(data_context),
            make_w3_search_tool(data_context),
            make_xiaoyi_search_tool(data_context),
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

        logger.info("[RAGAgent] 已注册3个工具: knowledge_search, w3_search, xiaoyi_search")