
"""Gateway 中间件：请求日志、限流、CORS。"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

logger = logging.getLogger(__name__)


class RequestLogMiddleware(BaseHTTPMiddleware):
    """请求日志中间件：记录请求路径、耗时、状态码。"""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        start = time.monotonic()
        method = request.method
        path = request.url.path
        client = request.client.host if request.client else "?"

        try:
            response = await call_next(request)
        except Exception as e:
            duration_ms = int((time.monotonic() - start) * 1000)
            logger.error("[HTTP] %s %s from %s → 500 (%dms) 异常: %s", method, path, client, duration_ms, e)
            raise

        duration_ms = int((time.monotonic() - start) * 1000)
        logger.info("[HTTP] %s %s from %s → %d (%dms)", method, path, client, response.status_code, duration_ms)
        return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    """限流中间件：全局并发控制 + 每 IP 请求频率限制。

    - 全局并发：asyncio.Semaphore 限制同时处理的请求数
    - 每 IP 限流：滑动窗口计数，限制每 IP 每分钟请求数
    """

    def __init__(
        self,
        app: ASGIApp,
        max_concurrent: int = 20,
        per_ip_rpm: int = 60,
        per_ip_window: float = 60.0,
        max_tracked_ips: int = 10000,
    ) -> None:
        super().__init__(app)
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._max_concurrent = max_concurrent
        self._per_ip_rpm = per_ip_rpm
        self._per_ip_window = per_ip_window
        self._max_tracked_ips = max_tracked_ips
        # {ip: [(timestamp, ...)]}
        self._ip_requests: dict[str, list[float]] = defaultdict(list)
        self._last_cleanup = time.monotonic()

    def _cleanup_stale_ips(self) -> None:
        """定期清理过期 IP 记录，防止内存泄漏。"""
        now = time.monotonic()
        # 每 60 秒清理一次
        if now - self._last_cleanup < 60.0:
            return
        self._last_cleanup = now
        cutoff = now - self._per_ip_window
        stale_ips = [ip for ip, times in self._ip_requests.items() if not times or times[-1] < cutoff]
        for ip in stale_ips:
            del self._ip_requests[ip]
        if stale_ips:
            logger.debug("[RateLimit] 清理 %d 个过期 IP 记录", len(stale_ips))

    def _check_per_ip(self, ip: str) -> bool:
        """检查 IP 是否超过每分钟请求限制。返回 True=允许，False=拒绝。"""
        now = time.monotonic()
        cutoff = now - self._per_ip_window
        # 清理过期记录
        self._ip_requests[ip] = [t for t in self._ip_requests[ip] if t > cutoff]
        if len(self._ip_requests[ip]) >= self._per_ip_rpm:
            return False
        self._ip_requests[ip].append(now)

        # 定期清理 + 超 IP 上限淘汰
        self._cleanup_stale_ips()
        if len(self._ip_requests) > self._max_tracked_ips:
            # 淘汰最久未活跃的 IP
            oldest_ips = sorted(self._ip_requests, key=lambda ip: self._ip_requests[ip][-1] if self._ip_requests[ip] else 0)
            for ip in oldest_ips[: len(self._ip_requests) - self._max_tracked_ips]:
                del self._ip_requests[ip]

        return True

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # 健康检查不限流
        if request.url.path == "/api/v1/health":
            return await call_next(request)

        # 每 IP 限流
        client_ip = request.client.host if request.client else "unknown"
        if not self._check_per_ip(client_ip):
            logger.warning("[RateLimit] IP %s 超过每分钟 %d 次限制", client_ip, self._per_ip_rpm)
            return Response(content="请求过于频繁，请稍后重试", status_code=429)

        # 全局并发限流
        if self._semaphore.locked():
            logger.warning("[RateLimit] 并发上限 %d 已满，拒绝: %s %s", self._max_concurrent, request.method, request.url.path)
            return Response(content="服务繁忙，请稍后重试", status_code=429)

        async with self._semaphore:
            return await call_next(request)