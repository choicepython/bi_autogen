
"""会话生命周期管理：ContextVar + TraceRecorder + DB + 缓存 + 对话历史。

由 DispatchLayer 在每次请求时创建，管理会话从 start 到 finish 的完整生命周期。
DispatchLayer 只做编排（路由 → 执行 → 后处理），会话细节全部委托本类。
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import datetime

from autogen_core.models import ChatCompletionClient

from config import settings
from core import turn_summary_builder
from core.context import SessionContext
from core.conversation_store import ConversationStore, InMemoryConversationStore
from core.result_cache import result_cache
from core.summarizer import summarize_background
from db.writer import db_writer, make_agent_execution_data, make_plan_data, make_routing_data, make_session_data
from models.cache import CacheLookupResult, ResultCacheEntry
from models.chat_request import ChatRequest
from models.conversation import ConversationContext
from models.routing import RoutingResult
from models.stream_event import StreamEvent, StreamEventType
from models.turn_result import TurnResult
from observability.logging_client import (
    reset_call_index,
    set_chat_id,
    set_current_agent,
    set_enable_thinking,
    set_session_id,
)
from observability.observer_factory import get_trace_observer
from observability.trace import TraceRecorder, set_trace_recorder
from tools.get_es_data import fetch_es_context

logger = logging.getLogger(__name__)


class SessionStartResult:
    """SessionManager.start() 的返回值，封装会话启动后的所有状态。

    避免方法返回超过 5 个值，统一用数据类承载。
    """

    def __init__(
        self,
        session_ctx: SessionContext,
        recorder: TraceRecorder,
        cache_hit: CacheLookupResult,
        seq: list[int],
        session_start_time: datetime,
    ) -> None:
        self.session_ctx = session_ctx
        self.recorder = recorder
        self.cache_hit = cache_hit
        self.seq = seq
        self.session_start_time = session_start_time


class SessionManager:
    """会话生命周期管理器。

    拥有会话从 start → routing → execution → finish 的全部状态管理。
    DispatchLayer 通过本类完成会话编排，自身只保留路由和执行的委托逻辑。

    用法:
        mgr = SessionManager(req, conversation_store)
        start = await mgr.start()
        if start.cache_hit.hit:
            async for ev in mgr.replay_cache(start): yield ev
            return
        # ... 路由 + 执行 (产出 turn_result) ...
        async for ev in mgr.finish(routing, collected_events, start, turn_result): yield ev
    """

    def __init__(
        self,
        req: ChatRequest,
        conversation_store: ConversationStore | None = None,
        model_client: ChatCompletionClient | None = None,
        agent_types: frozenset[str] | None = None,
    ) -> None:
        self._req = req
        self._conversation_store = conversation_store or InMemoryConversationStore()
        self._model_client = model_client
        # agent_types: 注入合法 Agent 类型名集合，None 时 lazy import（消除 core→agents 反向依赖）
        if agent_types is None:
            from agents.base import AGENT_TYPES
            agent_types = AGENT_TYPES
        self._agent_types = agent_types
        # session_id：外部传入优先，否则自动生成（时间戳+UUID 防碰撞）
        self._session_id = req.session_id or f"{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"

    # ------------------------------------------------------------------
    # 辅助：事件序号和时间戳
    # ------------------------------------------------------------------

    @staticmethod
    def _now() -> str:
        """当前时间 ISO 格式。"""
        return datetime.now().isoformat()

    @staticmethod
    def _next_seq(seq: list[int]) -> int:
        """递增并返回事件序号。"""
        seq[0] += 1
        return seq[0]

    # ------------------------------------------------------------------
    # start: 会话启动（ContextVar + DB + 历史 + ES + 缓存）
    # ------------------------------------------------------------------

    async def start(self) -> SessionStartResult:
        """启动会话：设置 ContextVar、写入 DB、加载历史、获取 ES 上下文、检查缓存。

        Returns:
            SessionStartResult 包含 session_ctx、recorder、cache_hit、seq、session_start_time。
        """
        self._init_context_vars()
        recorder = TraceRecorder(task=self._req.query, session_id=self._session_id, chat_id=self._req.chat_id)
        set_trace_recorder(recorder)

        await self._write_session_running(recorder)

        session_ctx = SessionContext(
            self._session_id,
            chat_id=self._req.chat_id, user=self._req.user, business=self._req.business,
        )
        await self._load_history(session_ctx)
        await self._fetch_es_context(session_ctx)

        seq = [0]
        session_start_time = datetime.now()
        cache_hit = await self._check_cache(session_ctx)

        # Langfuse trace 开始
        self._langfuse_trace_cm = get_trace_observer().start_trace(
            session_id=self._session_id,
            query=self._req.query,
            user_id=self._req.user.user_id,
            metadata={"source_site": self._req.business.source_site, "chat_id": self._req.chat_id},
        )
        self._langfuse_trace_cm.__enter__()

        return SessionStartResult(
            session_ctx=session_ctx,
            recorder=recorder,
            cache_hit=cache_hit,
            seq=seq,
            session_start_time=session_start_time,
        )

    def _init_context_vars(self) -> None:
        """设置会话级 ContextVar。"""
        set_session_id(self._session_id)
        set_chat_id(self._req.chat_id)
        set_enable_thinking(self._req.enable_thinking)

    async def _write_session_running(self, recorder: TraceRecorder) -> None:
        """DB: 写入会话 running 状态。同时保存 recorder 供后续使用。"""
        # recorder 保存到实例，finish 时需要
        self._recorder = recorder
        await db_writer.enqueue_session(make_session_data(
            session_id=self._session_id,
            chat_id=self._req.chat_id,
            query=self._req.query,
            status="running",
            user_id=self._req.user.user_id,
            user_name=self._req.user.user_name,
            source_site=self._req.business.source_site,
            model_name=settings.primary_model,
            selector_model=settings.selector_model,
        ))

    async def _load_history(self, session_ctx: SessionContext) -> None:
        """从 ConversationStore 加载多轮对话历史。"""
        try:
            history = await self._conversation_store.get_history(self._session_id)
            data_catalog = await self._conversation_store.get_data_catalog(self._session_id)
            if history or data_catalog:
                session_ctx.conversation_context = ConversationContext(
                    session_id=self._session_id,
                    turns=history,
                    data_catalog=data_catalog,
                    current_query=self._req.query,
                )
                logger.info(
                    "[SessionManager] 加载多轮对话历史: %d轮, %d条数据目录",
                    len(history), len(data_catalog),
                )
        except Exception as e:
            logger.warning("[SessionManager] 加载多轮对话历史失败: %s", e)

    async def _fetch_es_context(self, session_ctx: SessionContext) -> None:
        """资源召回（整个会话只查这一次）。失败时降级为空列表。"""
        try:
            es_api_meta, es_skills = await fetch_es_context(
                self._req.query, source_site=self._req.business.source_site,
            )
            session_ctx.api_meta = es_api_meta
            session_ctx.skills = es_skills
        except Exception as e:
            logger.warning("[SessionManager] 资源召回失败，降级为空列表: %s", e)
            session_ctx.api_meta = []
            session_ctx.skills = []

    async def _check_cache(self, session_ctx: SessionContext) -> CacheLookupResult:
        """缓存检查（有历史时跳过缓存，多轮对话不复用首轮缓存）。"""
        has_history = session_ctx.conversation_context is not None
        if self._req.agent_type or has_history:
            return CacheLookupResult()
        return await result_cache.lookup(self._req.query, self._req.business.source_site)

    # ------------------------------------------------------------------
    # SESSION_START 事件
    # ------------------------------------------------------------------

    def make_session_start(self, seq: list[int]) -> StreamEvent:
        """生成 SESSION_START 事件。"""
        return StreamEvent(
            type=StreamEventType.SESSION_START,
            seq=self._next_seq(seq),
            timestamp=self._now(),
            session_id=self._session_id,
            content=self._req.query,
            data={"query": self._req.query},
        )

    # ------------------------------------------------------------------
    # replay_cache: 缓存命中时重放事件
    # ------------------------------------------------------------------

    async def replay_cache(self, start: SessionStartResult) -> AsyncGenerator[StreamEvent, None]:
        """缓存命中：重放事件 + DB 写入 + SESSION_END。"""
        cached = start.cache_hit.entry
        if cached is None:
            return

        try:
            # DB: 写入路由决策（使用缓存的路由结果）
            await self._write_cached_routing(cached, start)

            # 重放缓存的 StreamEvent（替换 session_id 和 seq）
            for cached_ev in cached.events:
                yield StreamEvent(
                    type=cached_ev.type,
                    seq=self._next_seq(start.seq),
                    timestamp=self._now(),
                    session_id=self._session_id,
                    task_id=cached_ev.task_id,
                    agent_name=cached_ev.agent_name,
                    content=cached_ev.content,
                    data={**cached_ev.data, "cache_source": start.cache_hit.source},
                )

            # DB: 更新会话（cached状态）
            duration_ms = int((datetime.now() - start.session_start_time).total_seconds() * 1000)
            await self._write_session_cached(cached, duration_ms)

            # SESSION_END
            yield StreamEvent(
                type=StreamEventType.SESSION_END,
                seq=self._next_seq(start.seq),
                timestamp=self._now(),
                session_id=self._session_id,
                content=cached.final_result,
                data={"result": cached.final_result, "duration_ms": duration_ms, "cache_source": start.cache_hit.source},
            )
        finally:
            self.cleanup()

    async def _write_cached_routing(self, cached: ResultCacheEntry, start: SessionStartResult) -> None:
        """DB: 写入缓存命中的路由决策。"""
        if cached.routing_result is None:
            return
        routing = cached.routing_result
        api_meta_names = [
            item.get("name", "") for item in start.session_ctx.api_meta if isinstance(item, dict)
        ]
        await db_writer.enqueue_routing(make_routing_data(
            session_id=self._session_id,
            chat_id=self._req.chat_id,
            route_layer=routing.layer,
            execution_mode=routing.mode.value,
            agent_type=routing.agent_type.value if routing.agent_type else "",
            task_description=routing.task_description,
            reasoning=f"[缓存命中-{start.cache_hit.source}] {routing.reasoning}",
            api_meta_count=len(start.session_ctx.api_meta),
            skills_count=len(start.session_ctx.skills),
            api_meta_names=api_meta_names,
            duration_ms=0,
        ))

    async def _write_session_cached(self, cached: ResultCacheEntry, duration_ms: int) -> None:
        """DB: 更新会话为 cached 状态。"""
        await db_writer.enqueue_session(make_session_data(
            session_id=self._session_id,
            chat_id=self._req.chat_id,
            query=self._req.query,
            status="cached",
            execution_mode=cached.routing_result.mode.value if cached.routing_result else "",
            result=cached.final_result[:10000],
            user_id=self._req.user.user_id,
            user_name=self._req.user.user_name,
            source_site=self._req.business.source_site,
            duration_ms=duration_ms,
            model_name=settings.primary_model,
            selector_model=settings.selector_model,
        ))

    # ------------------------------------------------------------------
    # record_routing: 路由后记录 Trace + DB
    # ------------------------------------------------------------------

    async def record_routing(self, routing: RoutingResult, duration_ms: int, session_ctx: SessionContext) -> None:
        """记录路由决策到 Trace + DB + Langfuse。"""
        self._recorder.record_routing(
            layer=routing.layer,
            mode=routing.mode.value,
            agent_type=routing.agent_type.value if routing.agent_type else "",
            reasoning=routing.reasoning,
            api_meta_count=len(session_ctx.api_meta),
            skills_count=len(session_ctx.skills),
            duration_ms=duration_ms,
        )
        # Langfuse routing span（retrospective — 记录路由结果）
        with get_trace_observer().start_span("routing", metadata={
            "layer": routing.layer,
            "mode": routing.mode.value,
            "agent_type": routing.agent_type.value if routing.agent_type else "",
            "reasoning": routing.reasoning[:500],
            "duration_ms": duration_ms,
        }):
            pass
        api_meta_names = [item.get("name", "") for item in session_ctx.api_meta if isinstance(item, dict)]
        await db_writer.enqueue_routing(make_routing_data(
            session_id=self._session_id,
            chat_id=self._req.chat_id,
            route_layer=routing.layer,
            execution_mode=routing.mode.value,
            agent_type=routing.agent_type.value if routing.agent_type else "",
            task_description=routing.task_description,
            reasoning=routing.reasoning,
            api_meta_count=len(session_ctx.api_meta),
            skills_count=len(session_ctx.skills),
            api_meta_names=api_meta_names,
            duration_ms=duration_ms,
        ))

    # ------------------------------------------------------------------
    # finish: 会话结束（TurnSummary + DB + 缓存 + SESSION_END）
    # ------------------------------------------------------------------

    async def finish(
        self,
        routing: RoutingResult,
        collected_events: list[StreamEvent],
        start: SessionStartResult,
        turn_result: TurnResult,
    ) -> AsyncGenerator[StreamEvent, None]:
        """会话结束：存储 TurnSummary、写入 DB 最终状态、存入缓存、生成 SESSION_END。

        Args:
            turn_result: AgentLayer 产出的显式结果载体，包含 agent_records/plan_record/bg_summary。
        """
        final_result = turn_summary_builder.extract_final_result(collected_events)

        # DB: Agent 执行记录 + DAG 规划记录（从 TurnResult 读取，替代事件 sideband）
        await self._record_agent_executions(turn_result)
        await self._record_plan(turn_result)

        # 存储 TurnSummary
        await self._store_turn_summary(routing, collected_events, start.session_ctx)

        # DB: 更新会话最终状态
        duration_ms = int((datetime.now() - start.session_start_time).total_seconds() * 1000)
        has_error = any(ev.type == StreamEventType.ERROR for ev in collected_events)
        db_status = "error" if has_error else "completed"
        await self._write_session_final(db_status, duration_ms, routing, final_result)

        # 后台异步摘要（从 TurnResult 读取 bg_summary）
        bg_summary = turn_result.bg_summary
        if bg_summary and bg_summary != "DataContext 当前为空，没有可用数据。":
            if self._model_client is not None:
                asyncio.create_task(
                    summarize_background(
                        model_client=self._model_client,
                        query=self._req.query,
                        data_summary=bg_summary,
                        session_id=self._session_id,
                        chat_id=self._req.chat_id,
                    )
                )

        # SESSION_END
        yield StreamEvent(
            type=StreamEventType.SESSION_END,
            seq=self._next_seq(start.seq),
            timestamp=self._now(),
            session_id=self._session_id,
            content=final_result,
            data={"result": final_result, "duration_ms": duration_ms},
        )

        # 存入缓存（仅缓存成功结果）
        await self._store_result_cache(routing, collected_events, final_result)

    async def _store_turn_summary(
        self, routing: RoutingResult, collected_events: list[StreamEvent], session_ctx: SessionContext,
    ) -> None:
        """存储多轮对话 TurnSummary。"""
        try:
            turn_id = (
                len(session_ctx.conversation_context.turns) + 1
                if session_ctx.conversation_context else 1
            )
            turn_summary = turn_summary_builder.assemble_turn_summary(
                session_id=self._session_id,
                query=self._req.query,
                routing=routing,
                collected_events=collected_events,
                session_ctx=session_ctx,
                turn_id=turn_id,
                agent_types=self._agent_types,
            )
            await self._conversation_store.append_turn(self._session_id, turn_summary)

            data_refs = turn_summary_builder.extract_data_refs(session_ctx, self._agent_types)
            if data_refs:
                await self._conversation_store.update_data_catalog(self._session_id, data_refs)

            logger.info("[SessionManager] 存储TurnSummary: turn_id=%d", turn_summary.turn_id)
        except Exception as e:
            logger.warning("[SessionManager] 存储TurnSummary失败: %s", e)

    async def _write_session_final(
        self, db_status: str, duration_ms: int, routing: RoutingResult, final_result: str,
    ) -> None:
        """DB: 更新会话最终状态。"""
        await db_writer.enqueue_session(make_session_data(
            session_id=self._session_id,
            chat_id=self._req.chat_id,
            query=self._req.query,
            status=db_status,
            execution_mode=routing.mode.value,
            result=final_result[:10000],
            user_id=self._req.user.user_id,
            user_name=self._req.user.user_name,
            source_site=self._req.business.source_site,
            duration_ms=duration_ms,
            model_name=settings.primary_model,
            selector_model=settings.selector_model,
        ))

    async def _store_result_cache(
        self, routing: RoutingResult, collected_events: list[StreamEvent], final_result: str,
    ) -> None:
        """存入结果缓存（仅缓存成功结果）。"""
        has_error = any(ev.type == StreamEventType.ERROR for ev in collected_events)
        if has_error or final_result == "未能生成结果。" or not settings.result_cache_enabled:
            return
        cacheable_events = [
            ev for ev in collected_events
            if ev.type not in (StreamEventType.SESSION_START, StreamEventType.SESSION_END)
        ]
        cache_entry = ResultCacheEntry(
            query=self._req.query,
            source_site=self._req.business.source_site,
            events=cacheable_events,
            final_result=final_result,
            routing_result=routing,
            created_at=datetime.now().isoformat(),
            original_session_id=self._session_id,
            event_count=len(cacheable_events),
            has_table_data=any(ev.type == StreamEventType.TABLE for ev in cacheable_events),
        )
        await result_cache.store(self._req.query, self._req.business.source_site, cache_entry)

    # ------------------------------------------------------------------
    # cleanup: ContextVar 清理
    # ------------------------------------------------------------------

    def cleanup(self) -> None:
        """清理 ContextVar（在 finally 块中调用，确保所有路径都清理）。"""
        # Langfuse trace 结束
        if hasattr(self, "_langfuse_trace_cm"):
            self._langfuse_trace_cm.__exit__(None, None, None)
            del self._langfuse_trace_cm
        get_trace_observer().flush()

        if hasattr(self, "_recorder") and self._recorder is not None:
            try:
                self._recorder.finish()
            except Exception:
                self._recorder.close()
        set_trace_recorder(None)
        set_session_id("")
        set_chat_id("")
        set_current_agent("")
        set_enable_thinking(None)
        reset_call_index()

    # ------------------------------------------------------------------
    # Agent 执行记录 & DAG 规划记录（从事件中提取，写入 DB）
    # ------------------------------------------------------------------

    async def _record_agent_executions(self, turn_result: TurnResult) -> None:
        """从 TurnResult 中读取 Agent 执行记录并写入 DB。"""
        for record in turn_result.agent_records:
            await db_writer.enqueue_agent_execution(make_agent_execution_data(**record.to_db_kwargs()))

    async def _record_plan(self, turn_result: TurnResult) -> None:
        """从 TurnResult 中读取 DAG 规划记录并写入 DB。"""
        if turn_result.plan_record is None:
            return
        await db_writer.enqueue_plan(make_plan_data(**turn_result.plan_record.to_db_kwargs()))