
"""3层路由器：参数路由 → 规则路由 → 意图识别路由。
管全部路由逻辑，包括 ES 上下文获取
优化点：
- ES上下文只查一次，在Layer2查询后缓存到RoutingResult，Layer3和AgentLayer直接复用
- Layer2有明确匹配时跳过Layer3 LLM调用
- 同步ES调用通过asyncio.to_thread包装，不阻塞事件循环
- 路由结果TTL缓存，相似查询复用

"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from typing import Any

from autogen_core.models import ChatCompletionClient, SystemMessage, UserMessage

from config.prompt_manager import get_prompt_manager
from models.routing import AgentType, ExecutionMode, RoutingResult
from core.context import SessionContext
from utils.cache import TTLCache

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Layer 1 规则：闲聊关键词 + 数据关键词
# ---------------------------------------------------------------------------

_CHAT_PATTERNS = re.compile(
    r"^(你好|hi|hello|嗨|hey|在吗|在不在|谢谢|感谢|再见|拜拜|好的|ok|嗯|是|否|对|不对|"
    r"你是谁|你叫什么|你能做什么|你能帮我什么|介绍.*自己|自我介绍|"
    r"怎么样|如何|什么是|什么是.*|.*是什么|.*怎么用|怎么操作|"
    r"测试|test|ping)$",
    re.IGNORECASE,
)

_DATA_KEYWORDS = re.compile(
    r"查询|数据|统计|分析|报表|报告|图表|可视化|预测|检测|排名|对比|趋势|导出|产量|库存|订单|良率|齐套"
)

_ANALYSIS_KEYWORDS = re.compile(
    r"排名|对比|统计|最多|最少|趋势|占比|汇总|计算|分析|筛选|排序|分组|求和|平均|分布"
)

# 路由缓存：最多100条，5分钟过期
_route_cache = TTLCache(maxsize=100, ttl=300.0)


def _cache_key(query: str, source_site: str, has_history: bool = False) -> str:
    """生成路由缓存key。有历史时追加维度，避免多轮对话命中首轮缓存。"""
    raw = f"{source_site}:{query}:history={has_history}"
    return hashlib.md5(raw.encode()).hexdigest()


class BIRouter:
    """3层路由器：参数路由 → 规则路由 → 意图识别路由。"""

    def __init__(self, model_client: ChatCompletionClient) -> None:

        self._model_client = model_client

    async def route(
            self,
            query: str,
            *,
            source_site: str = "les_portal",
            session_ctx: SessionContext | None = None,
            **kwargs: Any,
    ) -> RoutingResult:
        """执行3层路由决策，返回 RoutingResult。

        优化策略：
        1. Layer 1 命中直接返回（零网络开销）
        2. ES上下文只在Layer 2查询一次，Layer 3复用
        3. Layer 2有明确匹配时跳过Layer 3 LLM调用
        4. 路由结果TTL缓存（有历史时不缓存）
        """
        if session_ctx is not None:
            conversation_context = session_ctx.conversation_context
            has_history = conversation_context is not None and bool(conversation_context.turns)
        else:
            has_history = False
        # 生成缓存Key
        cache_k = _cache_key(query, source_site, has_history=has_history)
        # 路由缓存检查（有历史时不走缓存结果，避免多轮对话命中首轮结果）
        if not has_history:
            cached = _route_cache.get(cache_k)
            if cached is not None:
                logger.info("[Router] 缓存命中: %s → %s", query[:50], cached.reasoning)
                return cached

        # Layer 1: 参数路由（零网络/LLM调用）
        result = self._route_layer1(query, **kwargs)
        if result is not None:
            logger.info("[Router] Layer 1 命中: %s → %s", query[:50], result.reasoning)
            if not has_history:
                _route_cache.put(cache_k, result)
            return result

        # Layer 2: 规则路由 + ES工具召回（ES只查这一次）

        result = self._route_layer2(query, session_ctx)
        if result is not None:
            logger.info("[Router] Layer 2 命中: %s → %s", query[:50], result.reasoning)
            if not has_history:
                _route_cache.put(cache_k, result)
            return result

        # Layer 3: 意图识别（复用Layer 2的ES结果，不再重复查询）
        result = await self._route_layer3(query, session_ctx)
        logger.info("[Router] Layer 3 命中: %s → %s", query[:50], result.reasoning)
        if not has_history:
            _route_cache.put(cache_k, result)
        return result

    # ------------------------------------------------------------------
    # Layer 1: 确定性参数路由
    # ------------------------------------------------------------------
    def _route_layer1(self, query: str, **kwargs: Any) -> RoutingResult | None:
        """根据显式参数或简单规则直接路由，零网络/LLM调用。"""
        # 1. agent_type 参数：调用方明确指定Agent
        agent_type = kwargs.get("agent_type")
        if agent_type:
            try:
                at = AgentType(agent_type)
            except ValueError:
                logger.warning("[Router] 无效的 agent_type 参数: %s", agent_type)
                return None
            return RoutingResult(
                layer=1,
                mode=ExecutionMode.SINGLE_AGENT,
                agent_type=at,
                task_description=query,
                reasoning=f"参数指定 agent_type={agent_type}",
            )

        stripped = query.strip()

        # 2. 闲聊精确匹配
        if _CHAT_PATTERNS.match(stripped):
            return RoutingResult(
                layer=1,
                mode=ExecutionMode.SINGLE_AGENT,
                agent_type=AgentType.SEARCH,
                task_description=query,
                reasoning="闲聊关键词精确匹配",
            )

        # 3. 短文本且无数据关键词 → RAGAgent
        # 已禁用：未考虑多轮对话上下文，短文本可能引用前轮数据，直接判定闲聊会误路由

        return None

    # ------------------------------------------------------------------
    # Layer 2: 规则路由 + ES工具召回
    # ------------------------------------------------------------------

    def _route_layer2(self, query: str, session_ctx: SessionContext | None = None) -> RoutingResult | None:
        """关键字匹配 + ES工具召回，无工具时路由知识问答智能体。

        注意：ES上下文由调用方（route()）查询后传入，本方法不再查ES。
        上下文感知：已有可用数据且查询为分析类时，直接走 FULL_TEAM。
        """
        # 没有准备 session_ctx
        if session_ctx is None:
            return RoutingResult(
                    layer=2,
                    mode=ExecutionMode.FULL_TEAM,
                    task_description=query,
                    reasoning=f"没有准备session 上下文，直接走PlanAgent规划分析任务",
                )
        # 上下文感知：已有数据 + 分析类查询 → 直接走 FULL_TEAM
        if session_ctx.conversation_context is not None:
            available = session_ctx.conversation_context.available_data
            if available and (_DATA_KEYWORDS.search(query) or _ANALYSIS_KEYWORDS.search(query)):
                return RoutingResult(
                    layer=2,
                    mode=ExecutionMode.FULL_TEAM,
                    task_description=query,
                    reasoning=f"已有{len(available)}条可用数据，直接走PlanAgent规划分析任务",
                )

        # 无工具召回 → 路由到 RAGAgent（知识问答）
        if not session_ctx.api_meta:
            return RoutingResult(
                layer=2,
                mode=ExecutionMode.SINGLE_AGENT,
                agent_type=AgentType.SEARCH,
                task_description=query,
                reasoning="ES未召回任何工具，路由到知识问答",
            )

        # 分析工具类型
        tool_types = {item.get("tool_type", "api") for item in session_ctx.api_meta}

        # 单一API工具 → APIAgent
        if len(tool_types) == 1 and len(session_ctx.api_meta) == 1:
            tool_type = tool_types.pop()
            if tool_type == "api":
                return RoutingResult(
                    layer=2,
                    mode=ExecutionMode.SINGLE_AGENT,
                    agent_type=AgentType.API,
                    task_description=query,
                    reasoning=f"ES召回单一API工具: {session_ctx.api_meta[0].get('name', '')}",
                )
            if tool_type == "table":
                return RoutingResult(
                    layer=2,
                    mode=ExecutionMode.SINGLE_AGENT,
                    agent_type=AgentType.SQL,
                    task_description=query,
                    reasoning="ES召回数据表工具，路由到SQLAgent",
                )

        # 多工具或多种类型 → 需要PlanAgent规划
        return RoutingResult(
            layer=2,
            mode=ExecutionMode.FULL_TEAM,
            task_description=query,
            reasoning=f"ES召回{len(session_ctx.api_meta)}个工具({len(tool_types)}种类型)，需要PlanAgent规划",
        )

    # ------------------------------------------------------------------
    # Layer 3: 意图识别
    # ------------------------------------------------------------------

    async def _route_layer3(
            self,
            query: str,
            session_ctx: SessionContext | None = None,
    ) -> RoutingResult:
        """LLM意图识别，判断问题复杂度。

        注意：ES上下文由调用方（route()）查询后传入，本方法不再查ES。
        如有对话上下文，将数据目录摘要注入 LLM prompt。
        """
        # 构建 prompt，如有上下文则注入数据目录
        data_catalog_hint = ""
        conversation_context = session_ctx.conversation_context if session_ctx else None
        if conversation_context is not None and conversation_context.available_data:
            data_catalog_hint = conversation_context._render_data_catalog(include_failed=False)

        prompt = get_prompt_manager().render_routing(
            "intent_classification",
            QUERY=query,
            DATA_CATALOG=data_catalog_hint,
        )
        logger.info("router[_route_layer3]:prompt=%s...", prompt[200:500])
        try:
            from config.settings import settings
            response = await asyncio.wait_for(
                self._model_client.create(
                    messages=[
                        SystemMessage(content="你是查询意图分类器，只输出JSON。"),
                        UserMessage(content=prompt, source="user")],
                    json_output=True,
                ),
                timeout=settings.routing_llm_timeout,
            )
            content = response.content
            if hasattr(content, "model_dump_json"):
                content = content.model_dump_json()

            # 解析JSON
            data = json.loads(content)
            complexity = data.get("complexity", "complex")
            agent_type_str = data.get("agent_type", "RAGAgent")
            reasoning = data.get("reasoning", "LLM意图识别")

            if complexity == "simple":
                try:
                    at = AgentType(agent_type_str)
                except ValueError:
                    at = AgentType.SEARCH
                    reasoning += f"（无效agent_type={agent_type_str}，降级为RAGAgent）"
                return RoutingResult(
                    layer=3,
                    mode=ExecutionMode.SINGLE_AGENT,
                    agent_type=at,
                    task_description=query,
                    reasoning=reasoning,
                )

        except (json.JSONDecodeError, TypeError, ValueError) as e:
            logger.warning("[Router] Layer 3 LLM输出解析失败: %s", e)
        except Exception as e:
            logger.error("[Router] Layer 3 意图识别异常: %s", e, exc_info=True)

        # 兜底：FULL_TEAM
        return RoutingResult(
            layer=3,
            mode=ExecutionMode.FULL_TEAM,
            task_description=query,
            reasoning="Layer 3 兜底：LLM意图识别失败或判断为complex，走完整流水线",
        )