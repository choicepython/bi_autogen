
"""调度层：只管编排（路由 → 执行 → 后处理），不管持久化/缓存/对话。

职责：
- Model client 创建（create_model_client, _create_http_client）
- 编排路由层 → 智能体层
- 输出格式化、SSE包装

不负责：
- 会话生命周期管理（SessionManager）
- Agent 创建/执行（AgentLayer）
- 路由决策（RoutingLayer）
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

import httpx
from autogen_core.models import ChatCompletionClient
from autogen_ext.models.openai import OpenAIChatCompletionClient

from config import settings
from core.agent_layer import AgentLayer
from core.conversation_store import ConversationStore, InMemoryConversationStore
from core.data_context_cache import DataContextCache, InMemoryDataContextCache
from core.router import BIRouter
from core.routing_layer import RoutingContext, RoutingLayer
from core.session_message import SessionManager
from models.chat_request import ChatRequest
from models.routing import ExecutionMode
from models.stream_event import StreamEvent, StreamEventType
from models.turn_result import TurnResult
from observability.logging_client import LoggingChatCompletionClient, set_current_agent
from observability.trace import TraceRecorder

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model Client 工厂
# ---------------------------------------------------------------------------

async def _log_request(request: httpx.Request) -> None:
    logger.info("LLM Request: %s %s", request.method, request.url)


async def _log_response(response: httpx.Response) -> None:
    logger.info("LLM Response: %s -> %d", response.request.url, response.status_code)


def _create_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        event_hooks={"request": [_log_request], "response": [_log_response]},
        timeout=httpx.Timeout(60.0, connect=10.0),
    )


def create_model_client(
        model: str | None = None, base_url: str | None = None, api_key: str | None = None
) -> OpenAIChatCompletionClient:
    """创建 OpenAI 兼容的 ChatCompletionClient。"""
    model = model or settings.primary_model
    base_url = base_url or settings.primary_base_url
    api_key = api_key or settings.primary_api_key.get_secret_value()

    create_args: dict[str, Any] = {
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
        "http_client": _create_http_client(),
        "model_info": {
            "vision": False,
            "function_calling": True,
            "json_output": True,
            "structured_output": True,
            "family": "unknown",
        },
    }

    # 通过 extra_body 传入 Qwen3/vLLM 思考模式参数
    # Qwen3 使用 chat_template_kwargs.enable_thinking 控制思考输出
    # 必须显式设置，否则 Qwen3 默认启用思考模式
    create_args["extra_body"] = {"chat_template_kwargs": {"enable_thinking": settings.enable_thinking}}

    return OpenAIChatCompletionClient(**create_args)


# ---------------------------------------------------------------------------
# DispatchLayer
# ---------------------------------------------------------------------------

class DispatchLayer:
    """调度层：只管编排（路由 → 执行 → 后处理）。

    会话生命周期委托 SessionManager，路由委托 RoutingLayer，执行委托 AgentLayer。
    """

    def __init__(
            self,
            model_client: ChatCompletionClient | None = None,
            selector_client: ChatCompletionClient | None = None,
            conversation_store: ConversationStore | None = None,
            data_context_cache: DataContextCache | None = None,
    ) -> None:
        self._model_client = model_client or LoggingChatCompletionClient(create_model_client())
        self._selector_client = selector_client or LoggingChatCompletionClient(
            create_model_client(
                model=settings.selector_model,
                base_url=settings.selector_base_url,
                api_key=settings.selector_api_key.get_secret_value(),
            )
        )
        self._conversation_store = conversation_store or InMemoryConversationStore()
        self._data_context_cache = data_context_cache or InMemoryDataContextCache()
        self._last_recorder: TraceRecorder | None = None
        self._routing = RoutingLayer(BIRouter(self._selector_client))
        self._agent_layer = AgentLayer(self._model_client, self._selector_client, self._data_context_cache)

    @property
    def routing_layer(self) -> RoutingLayer:
        """暴露路由层，供外部注册中间件。"""
        return self._routing

    @property
    def agent_layer(self) -> AgentLayer:
        """暴露智能体层，供外部扩展。"""
        return self._agent_layer

    def _format_output(self, text: str) -> str:
        """JSON 美化输出。"""
        try:
            data = json.loads(text)
            return json.dumps(data, ensure_ascii=False, indent=2)
        except (json.JSONDecodeError, TypeError):
            pass
        json_match = re.search(r"```json\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
        if json_match:
            try:
                data = json.loads(json_match.group(1))
                formatted = json.dumps(data, ensure_ascii=False, indent=2)
                return text[: json_match.start()] + formatted + text[json_match.end():]
            except (json.JSONDecodeError, TypeError):
                pass
        return text

    async def run(self, task: str | ChatRequest) -> tuple[str, TraceRecorder]:
        """运行任务，返回(结果文本, trace记录器)。内部消费 run_stream。"""
        req = task if isinstance(task, ChatRequest) else ChatRequest(query=task)
        final_result = "未能生成结果。"
        async for ev in self.run_stream(req):
            if ev.type == StreamEventType.SESSION_END:
                final_result = self._format_output(ev.content)
        return final_result, self._last_recorder or TraceRecorder(task=req.query)

    async def run_stream(self, task: str | ChatRequest, **kwargs: Any) -> AsyncGenerator[
        StreamEvent, None]:  # type: ignore[misc]
        """流式运行任务，yield StreamEvent 事件。编排三层。"""
        req = task if isinstance(task, ChatRequest) else ChatRequest(query=task, **kwargs)

        # 会话启动
        mgr = SessionManager(req, self._conversation_store, model_client=self._model_client)
        start = await mgr.start()

        # SESSION_START
        yield mgr.make_session_start(start.seq)

        # 缓存命中：重放事件后直接返回
        if start.cache_hit.hit and start.cache_hit.entry is not None:
            async for ev in mgr.replay_cache(start):
                yield ev
            return

        # 路由
        set_current_agent("Router")
        routing, routing_duration = await self._route(req, start.session_ctx)
        await mgr.record_routing(routing, routing_duration, start.session_ctx)
        logger.info(
            "[DispatchLayer] 路由结果: Layer%d, mode=%s, agent=%s",
            routing.layer, routing.mode.value,
            routing.agent_type.value if routing.agent_type else "N/A",
        )

        # 执行 + 收集事件
        collected_events: list[StreamEvent] = []
        turn_result = TurnResult()
        try:
            async for ev in self._execute(routing, req, start, turn_result):
                collected_events.append(ev)
                yield ev
        except Exception as e:
            logger.error("任务执行异常: %s", e, exc_info=True)
        finally:
            mgr.cleanup()

        # 会话结束
        self._last_recorder = start.recorder
        async for ev in mgr.finish(routing, collected_events, start, turn_result):
            yield ev

    async def _route(self, req: ChatRequest, session_ctx: Any) -> tuple[Any, int]:
        """执行路由决策，返回 (RoutingResult, duration_ms)。"""
        routing_start = datetime.now()
        routing = await self._routing.route(
            req.query,
            source_site=req.business.source_site,
            agent_type=req.agent_type or None,
            context=RoutingContext(
                source_site=req.business.source_site,
                user=req.user,
                business=req.business,
                session_ctx=session_ctx,
            ),
        )
        routing_duration = int((datetime.now() - routing_start).total_seconds() * 1000)
        return routing, routing_duration

    async def _execute(
        self, routing: Any, req: ChatRequest, start: Any, turn_result: TurnResult,
    ) -> AsyncGenerator[StreamEvent, None]:
        """执行路由结果：单 Agent 或全团队。

        Args:
            turn_result: 显式结果载体，由 AgentLayer 产出，供 SessionManager 消费。
        """
        if routing.mode == ExecutionMode.SINGLE_AGENT:
            async for ev in self._agent_layer.run_single_agent(
                    routing, start.session_ctx, start.seq, start.session_start_time, turn_result,
            ):
                yield ev
        else:
            async for ev in self._agent_layer.run_team(
                    req.query, routing, start.session_ctx, start.seq, start.session_start_time, turn_result,
            ):
                yield ev

    async def run_stream_sse(self, task: str | ChatRequest, **kwargs: Any) -> AsyncGenerator[str, None]:
        """流式运行任务，yield SSE 格式文本。"""
        async for ev in self.run_stream(task, **kwargs):
            yield ev.to_sse()

    async def reset(self) -> None:
        """重置状态，确保DB writer后台协程运行。"""
        try:
            from db.writer import db_writer
            if not db_writer._running:
                await db_writer.start()
        except Exception as e:
            logger.warning("[DispatchLayer] DB writer启动失败（将跳过DB写入）: %s", e)