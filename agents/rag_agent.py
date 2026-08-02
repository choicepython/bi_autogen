
"""RAGAgent：检索增强生成助手，擅长搜索企业内部知识库、互联网信息，并对搜索结果进行归纳总结。

搜索工具按 URL 配置条件注册：仅已配置 URL 的工具才会注册到 Agent 并出现在提示词中。
未配置任何 URL 时，降级为纯知识回答模式（无工具，基于 LLM 自身知识回答）。
搜索结果自动存入DataContext，供下游Agent引用。
"""

from __future__ import annotations

import logging
from typing import ClassVar

from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent, ContextSpec
from config import settings
from core.context import SessionContext
from core.data_context import DataContext
from tools.search_tools import SEARCH_TOOL_META, SearchToolMeta

logger = logging.getLogger(__name__)

# 无工具时的行为规范
_NO_TOOL_BEHAVIOR = (
    "1. 基于自身知识直接回答用户问题\n"
    "2. 简单闲聊可以直接回答\n"
    "3. 必须如实回答，不要凭空编造\n"
    "4. 如果自身知识不足以回答用户问题，如实说明\n"
    "5. 回答要完整、准确、有条理"
)

# 有工具时的行为规范
_WITH_TOOL_BEHAVIOR = (
    "1. 知识性问题必须调用搜索工具获取信息后再回答\n"
    "2. 简单闲聊或已有充足知识可以直接回答的问题，可以不调用工具\n"
    "3. 搜索至多三轮：调用一次搜索工具后，不要再次调用搜索工具\n"
    "4. 搜索完成后，必须直接用自然语言总结回答，不要再调用任何工具\n"
    "5. 必须基于搜索结果来回答，不要凭空编造\n"
    "6. 不要直接复制粘贴搜索原文，要用自己的语言总结归纳\n"
    "7. 回答要完整、准确、有条理\n"
    "8. 如果多个搜索结果有冲突或互补，要综合分析\n"
    "9. 如果搜索结果不足以回答用户问题，如实说明，不要编造"
)


def _get_available_tools() -> list[SearchToolMeta]:
    """返回已配置的搜索工具元数据列表（URL 和 API Key 均需配置）。"""

    def _is_set(attr: str) -> bool:
        val = getattr(settings, attr, "")
        if hasattr(val, "get_secret_value"):
            return bool(val.get_secret_value())
        return bool(val)

    return [m for m in SEARCH_TOOL_META if _is_set(m.url_attr) and (not m.api_key_attr or _is_set(m.api_key_attr))]


class RAGAgent(BIBaseAgent):
    """检索增强生成助手，擅长搜索并总结企业内部知识库、互联网信息。"""

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
        # 按 URL 配置条件注册工具
        tools = self._build_tools(data_context)

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

        if tools:
            logger.info("[RAGAgent] 已注册 %d 个工具: %s", len(tools), ", ".join(t.name for t in tools))
        else:
            logger.warning("[RAGAgent] 未配置任何搜索 URL，无工具注册（降级为纯知识回答模式）")

    @staticmethod
    def _build_tools(data_context: DataContext) -> list:
        """根据 URL 配置构建搜索工具列表。"""
        # 延迟导入避免循环依赖
        from tools.search_tools import (
            make_knowledge_search_tool,
            make_web_search_tool,
        )

        factories = {
            "make_knowledge_search_tool": make_knowledge_search_tool,
            "make_web_search_tool": make_web_search_tool,
        }
        tools = []
        for meta in _get_available_tools():
            factory = factories[meta.factory]
            tools.append(factory(data_context))
        return tools

    def _extra_prompt_vars(self) -> dict[str, str]:
        """根据搜索 URL 配置动态生成工具描述占位符内容。

        未配置 URL 的工具不出现在提示词中，避免 LLM 调用不存在的工具。
        """
        tools = _get_available_tools()
        has_tools = bool(tools)

        # 工具使用策略
        if has_tools:
            guide = "\n".join(m.guide for m in tools)
            guide += "\n\n### 常见陷阱\n"
            guide += "- 搜索不超过3轮！调用搜索后直接总结回答，不要再调用任何搜索工具\n"
            guide += "- 不要被搜索结果中的无关信息带偏，聚焦用户问题\n"
            guide += "- 搜索结果可能包含广告或低质量内容，需自行判断可信度"
        else:
            guide = "（当前未配置搜索工具，基于自身知识回答用户问题）"

        # 决策原则
        if has_tools:
            rules = "，".join(f"{m.scene}用{m.name}" for m in tools)
            decision = (
                f"- {rules}\n- 可以同时调用多个搜索工具获取更全面的信息\n- 搜索至多三轮即止，基于已有结果组织回答"
            )
        else:
            decision = "- 未配置搜索工具，基于自身知识直接回答\n- 信息不足时如实说明，绝不编造"

        # 搜索流程中的工具选择步骤
        if has_tools:
            choices = "，".join(f"{m.scene}→{m.name}" for m in tools)
            workflow = f"2. 选择搜索工具：{choices}"
        else:
            workflow = "2. 无搜索工具可用，直接基于自身知识回答"

        # 搜索策略表
        if has_tools:
            rows = ["| 问题类型 | 首选工具 | 备选 |", "|----------|----------|------|"]
            for m in tools:
                rows.append(f"| {m.scene} | {m.name} | - |")
            strategy = "\n".join(rows)
        else:
            strategy = ""

        # 技能目录表
        if has_tools:
            rows = ["| 技能 | 说明 | 何时使用 |", "|------|------|----------|"]
            for m in tools:
                rows.append(f"| {m.name} | {m.label} | {m.scene} |")
            skills_table = "\n".join(rows)
        else:
            skills_table = "（当前无可用搜索技能）"

        return {
            "TOOLS_GUIDE": guide,
            "SEARCH_DECISION": decision,
            "SEARCH_WORKFLOW": workflow,
            "SEARCH_STRATEGY": strategy,
            "SKILLS_TABLE": skills_table,
            "SEARCH_BEHAVIOR": _WITH_TOOL_BEHAVIOR if has_tools else _NO_TOOL_BEHAVIOR,
        }