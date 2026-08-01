
"""FastAPI 应用工厂：路由注册、中间件、生命周期管理。"""

from __future__ import annotations

import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from config import settings
from core.team import BITeam
from db.writer import db_writer
from gateway.adapter import WebAdapter, WeLinkAdapter
from gateway.middleware import RateLimitMiddleware, RequestLogMiddleware

logger = logging.getLogger(__name__)

# 全局 BITeam 实例（应用级单例）
_team: BITeam | None = None


def _get_team() -> BITeam:
    """获取全局 BITeam 实例（懒初始化）。"""
    global _team
    if _team is None:
        _team = BITeam()
    return _team


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动和关闭。"""
    # --- 启动 ---
    logger.info("[Gateway] 服务启动中...")

    # 启动配置校验
    from config.startup_check import check_startup_config
    check_startup_config()

    try:
        from db import init_db
        await init_db()
        logger.info("[Gateway] 数据库初始化完成")
    except Exception as e:
        logger.warning("[Gateway] 数据库初始化失败（将跳过DB写入）: %s", e)

    try:
        await db_writer.start()
        logger.info("[Gateway] DB Writer 已启动")
    except Exception as e:
        logger.warning("[Gateway] DB Writer 启动失败: %s", e)

    # 创建存储实例（Redis 或 InMemory，根据配置优雅降级）
    from core.store_factory import create_conversation_store, create_data_context_cache

    conversation_store = await create_conversation_store()
    data_context_cache = await create_data_context_cache()
    global _team
    _team = BITeam(conversation_store=conversation_store, data_context_cache=data_context_cache)
    logger.info(
        "[Gateway] BITeam 已初始化（store=%s, cache=%s）",
        type(conversation_store).__name__, type(data_context_cache).__name__,
    )

    logger.info("[Gateway] 服务已启动: http://%s:%d", settings.server_host, settings.server_port)
    yield

    # --- 关闭 ---
    logger.info("[Gateway] 服务关闭中（等待在飞请求完成，最多 %ds）...", settings.shutdown_grace_period)
    try:
        await db_writer.stop()
    except Exception as e:
        logger.warning("[Gateway] DB Writer 停止失败: %s", e)

    # 优雅关闭 BITeam（清理 ContextVar、释放资源）
    if _team is not None:
        try:
            await _team.shutdown()
        except Exception as e:
            logger.warning("[Gateway] BITeam 关闭失败: %s", e)

    try:
        from db import close_pool
        await close_pool()
    except Exception as e:
        logger.warning("[Gateway] 关闭连接池失败: %s", e)

    # 刷新可观测性平台待发送数据
    try:
        from observability.observer_factory import get_trace_observer
        get_trace_observer().flush()
    except Exception as e:
        logger.warning("[Gateway] 可观测性 flush 失败: %s", e)

    logger.info("[Gateway] 服务已关闭")


def create_app() -> FastAPI:
    """创建 FastAPI 应用实例。"""
    app = FastAPI(
        title="BI Agent",
        version="0.1.0",
        description="BI 智能分析助手 API",
        lifespan=lifespan,
    )

    # 中间件（顺序：后添加的先执行）
    cors_origins = settings.cors_origins
    if cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.add_middleware(RateLimitMiddleware, max_concurrent=20)
    app.add_middleware(RequestLogMiddleware)

    # 注册路由
    _register_routes(app)

    # 注册用户答复端点（PlanAgent ask_user）
    from gateway.answer_route import router as answer_router
    app.include_router(answer_router)

    # 静态文件（前端页面）
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists():
        app.mount("/", StaticFiles(directory=str(static_dir), html=True), name="static")

    return app


def _register_routes(app: FastAPI) -> None:
    """注册所有API路由。"""

    # --- Web 渠道 ---
    @app.post("/api/v1/chat", summary="Web SSE 聊天", tags=["chat"])
    async def web_chat(request: Request):
        """Web渠道：接收ChatRequest JSON，返回SSE流式响应。"""
        adapter = WebAdapter()
        chat_req = await adapter.parse_request(request)
        team = _get_team()
        return await adapter.format_response(chat_req, team)

    # --- WeLink 渠道 ---
    @app.post("/api/v1/welink/callback", summary="WeLink 事件回调", tags=["welink"])
    async def welink_callback(request: Request):
        """WeLink渠道：接收事件回调，异步处理后推送结果。"""
        adapter = WeLinkAdapter()
        chat_req = await adapter.parse_request(request)
        team = _get_team()
        return await adapter.format_response(chat_req, team)

    # --- 健康检查 ---
    @app.get("/api/v1/health", summary="健康检查", tags=["system"])
    async def health():
        """服务健康检查端点，探测 DB/ES 依赖可达性。"""
        checks: dict[str, str] = {}

        # DB 可达性
        try:
            from db import get_pool
            pool = await get_pool()
            async with pool.acquire() as conn:
                await conn.fetchval("SELECT 1")
            checks["db"] = "ok"
        except Exception as e:
            checks["db"] = f"error: {str(e)[:100]}"

        # ES 可达性
        try:
            from utils.es_query import get_es_client
            client = get_es_client()
            if client.ping():
                checks["es"] = "ok"
            else:
                checks["es"] = "error: ping failed"
        except Exception as e:
            checks["es"] = f"error: {str(e)[:100]}"

        overall = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
        return {"status": overall, "version": "0.1.0", "checks": checks}

    # --- 指标查询 ---
    @app.get("/api/v1/metrics/agents", summary="Agent指标", tags=["metrics"])
    async def agent_metrics(days: int = 7):
        """各Agent成功率、平均耗时、Token消耗。"""
        try:
            from db.metrics import get_agent_metrics
            data = await get_agent_metrics(days=days)
            return {"data": data}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    @app.get("/api/v1/metrics/tools", summary="工具指标", tags=["metrics"])
    async def tool_metrics(days: int = 7):
        """工具调用频次、错误率、平均耗时。"""
        try:
            from db.metrics import get_tool_metrics
            data = await get_tool_metrics(days=days)
            return {"data": data}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    @app.get("/api/v1/metrics/sessions", summary="会话统计", tags=["metrics"])
    async def session_stats(hours: int = 24):
        """按小时统计会话量、成功率、平均耗时。"""
        try:
            from db.metrics import get_session_stats
            data = await get_session_stats(hours=hours)
            return {"data": data}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    @app.get("/api/v1/metrics/routing", summary="路由分布", tags=["metrics"])
    async def routing_distribution(days: int = 7):
        """路由层命中分布和Agent分布。"""
        try:
            from db.metrics import get_routing_agent_distribution, get_routing_distribution
            layers = await get_routing_distribution(days=days)
            agents = await get_routing_agent_distribution(days=days)
            return {"data": {"layers": layers, "agents": agents}}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    @app.get("/api/v1/sessions/{session_id}", summary="会话追溯", tags=["trace"])
    async def session_trace(session_id: str, chat_id: str = ""):
        """追溯某次会话的完整执行链路。"""
        try:
            from db.metrics import get_session_trace
            data = await get_session_trace(session_id, chat_id)
            if not data:
                return JSONResponse(status_code=404, content={"error": "会话不存在"})
            return {"data": data}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    # --- 反馈 ---
    @app.post("/api/v1/feedback", summary="用户反馈", tags=["feedback"])
    async def submit_feedback(request: Request):
        """提交用户对会话的反馈。"""
        try:
            body = await request.json()
            session_id = body.get("session_id", "")
            if not session_id:
                return JSONResponse(status_code=400, content={"error": "session_id 不能为空"})

            from db.writer import db_writer

            await db_writer.enqueue_feedback({
                "session_id": session_id,
                "chat_id": body.get("chat_id", ""),
                "rating": body.get("rating"),
                "is_positive": body.get("is_positive"),
                "feedback_text": body.get("feedback_text", ""),
                "tags": body.get("tags", []),
                "source": body.get("source", "web"),
            })
            return {"status": "ok"}
        except Exception as e:
            return JSONResponse(status_code=500, content={"error": str(e)})

    # --- 报告下载 ---
    @app.get("/api/v1/reports/{filename}", summary="下载报告文件", tags=["report"])
    async def download_report(filename: str):
        """下载已生成的报告文件（word/ppt/html/pdf）。

        路径遍历防御：取 Path.name 后与原值比对，拒绝含目录分隔符或 .. 的输入。
        """
        # 路径遍历防御：禁止 .. 和绝对路径，仅允许纯文件名
        safe = Path(filename).name
        if safe != filename or not safe:
            raise HTTPException(status_code=400, detail="非法文件名")
        path = Path(tempfile.gettempdir()) / "bi_reports" / safe
        if not path.is_file():
            raise HTTPException(status_code=404, detail="报告不存在或已过期")
        # RFC 5987: 非 ASCII 文件名需用 filename*=UTF-8'' 编码，避免 latin-1 header 编码失败
        quoted = quote(safe)
        return FileResponse(
            str(path),
            headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"},
        )