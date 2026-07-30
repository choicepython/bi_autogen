
"""路由层：智能路由 + 中间件扩展。

职责：
- 委托 BIRouter 执行3层路由
- 执行中间件链（pre-route + post-route）
- 扩展窗口：权限检查、限流、审计、A/B测试等

不负责：
- 创建Agent
- 处理事件
- 管理会话
"""

from __future__ import annotations

import logging
from typing import Any

from core.context import SessionContext
from core.router import BIRouter
from models.chat_request import BusinessInfo, UserInfo
from models.routing import RoutingResult

logger = logging.getLogger(__name__)


class RoutingContext:
    """请求级上下文，在中间件链中传递。"""

    def __init__(
        self,
        *,
        source_site: str = "les_portal",
        user: UserInfo | None = None,
        business: BusinessInfo | None = None,
        session_ctx: SessionContext | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        self.source_site = source_site
        self.user = user
        self.business = business
        self.session_ctx = session_ctx
        self.extra = extra or {}


class RoutingMiddleware:
    """路由中间件基类。子类覆盖 on_route/on_routed 实现扩展逻辑。

    扩展场景举例：
    - PermissionMiddleware：检查 user.permissions，将 FULL_TEAM 降级为 SINGLE_AGENT
    - RateLimitMiddleware：并发会话数检查
    - AuditMiddleware：记录路由决策到审计日志
    - ABTestMiddleware：根据实验分组覆盖 agent_type
    """

    async def on_route(
        self,
        query: str,
        context: RoutingContext,
    ) -> RoutingResult | None:
        """路由前调用。返回 RoutingResult 可短路路由（跳过 BIRouter）。"""
        return None

    async def on_routed(
        self,
        query: str,
        result: RoutingResult,
        context: RoutingContext,
    ) -> RoutingResult:
        """路由后调用。可观察或修改路由结果（如权限降级）。"""
        return result


class RoutingLayer:
    """路由层：智能路由 + 中间件扩展。

    Flow:
    1. 构建 RoutingContext
    2. on_route() 链 — 首个非None结果短路
    3. 无短路 → BIRouter.route()
    4. on_routed() 链 — 可修改结果
    5. 返回最终 RoutingResult
    """

    def __init__(
        self,
        router: BIRouter,
        middlewares: list[RoutingMiddleware] | None = None,
    ) -> None:
        self._router = router
        self._middlewares = middlewares or []

    async def route(
        self,
        query: str,
        *,
        source_site: str = "les_portal",
        agent_type: str | None = None,
        context: RoutingContext | None = None,
    ) -> RoutingResult:
        """执行路由 + 中间件。"""
        # 构建 RoutingContext
        if context is None:
            context = RoutingContext(source_site=source_site)

        # Pre-route 中间件链
        for middleware in self._middlewares:
            result = await middleware.on_route(query, context)
            if result is not None:
                logger.info("[RoutingLayer] 中间件 %s 短路路由: %s", type(middleware).__name__, result.reasoning[:80])
                return result

        # 委托 BIRouter
        result = await self._router.route(
            query,
            source_site=source_site,
            agent_type=agent_type,
            session_ctx=context.session_ctx,
        )

        # Post-route 中间件链
        for middleware in self._middlewares:
            result = await middleware.on_routed(query, result, context)

        return result

    def add_middleware(self, middleware: RoutingMiddleware) -> None:
        """运行时注册中间件。"""
        self._middlewares.append(middleware)