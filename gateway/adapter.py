
"""渠道适配器：不同消息来源的请求解析、认证和响应格式化。

GatewayAdapter 是核心抽象，每个渠道（Web/WeLink/CLI）实现自己的适配器。
适配器负责：
1. parse_request: 将渠道特有的请求格式转为统一的 ChatRequest
2. authenticate: 按渠道方式验证用户身份
3. format_response: 执行任务并返回渠道适配的响应格式
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from core.team import BITeam
from models.chat_request import BusinessInfo, ChatRequest, UserInfo

logger = logging.getLogger(__name__)


class GatewayAdapter(ABC):
    """渠道适配器基类。"""

    channel: str = ""

    @abstractmethod
    async def parse_request(self, request: Request) -> ChatRequest:
        """从原始HTTP请求构造 ChatRequest。"""

    @abstractmethod
    async def format_response(self, chat_request: ChatRequest, team: BITeam) -> StreamingResponse | JSONResponse:
        """执行任务并返回FastAPI响应。"""

    @abstractmethod
    async def authenticate(self, request: Request) -> UserInfo:
        """验证请求身份，返回UserInfo。失败抛HTTPException。"""


class WebAdapter(GatewayAdapter):
    """Web渠道适配器：标准ChatRequest JSON + IAM Token认证 + SSE流式响应。"""

    channel = "web"

    async def parse_request(self, request: Request) -> ChatRequest:
        """从JSON body解析ChatRequest，注入认证用户信息。"""
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"无效的请求体: {e}") from e

        if not body.get("query"):
            raise HTTPException(status_code=400, detail="query 字段不能为空")

        # 认证
        user = await self.authenticate(request)

        # 构造 ChatRequest
        user_info = body.get("user", {})
        business_info = body.get("business", {})

        return ChatRequest(
            query=body["query"],
            session_id=body.get("session_id", ""),
            chat_id=body.get("chat_id", ""),
            user=UserInfo(
                user_id=user.user_id or user_info.get("user_id", ""),
                user_name=user.user_name or user_info.get("user_name", ""),
                permissions=user_info.get("permissions", []),
            ),
            business=BusinessInfo(
                source_site=business_info.get("source_site", "les_portal"),
                custom_env=business_info.get("custom_env", {}),
                extra=business_info.get("extra", {}),
            ),
            agent_type=body.get("agent_type", ""),
            enable_thinking=body.get("enable_thinking") if "enable_thinking" in body else None,
        )

    async def format_response(self, chat_request: ChatRequest, team: BITeam) -> StreamingResponse:
        """SSE 流式响应。"""
        await team.reset()

        async def event_generator():
            try:
                async for sse_text in team.run_stream_sse(chat_request):
                    yield sse_text
            except Exception as e:
                logger.error("[WebAdapter] SSE流异常: %s", e, exc_info=True)
                # 使用 json.dumps 安全构造错误事件（避免引号/特殊字符破坏 JSON）
                error_payload = json.dumps({
                    "type": "error",
                    "data": {
                        "error_type": type(e).__name__,
                        "message": str(e),
                    },
                }, ensure_ascii=False)
                yield f"event: error\ndata: {error_payload}\n\n"
            finally:
                await team.reset()

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def authenticate(self, request: Request) -> UserInfo:
        """验证 IAM Token。

        开发模式（无 BI_AUTH_ENABLED 环境变量）跳过验证。
        生产模式从 Authorization header 提取用户信息。
        """
        from config import settings

        if not getattr(settings, "auth_enabled", False):
            return UserInfo()

        auth_header = request.headers.get("Authorization", "")
        if not auth_header:
            raise HTTPException(status_code=401, detail="缺少 Authorization 头")

        # TODO: 对接华为 IAM Token 验证，从 token 解析 user_id/user_name
        # 当前仅做格式校验
        token = auth_header.removeprefix("Bearer ").strip()
        if not token:
            raise HTTPException(status_code=401, detail="无效的 Token")

        return UserInfo(user_id=token[:32], user_name="")


class WeLinkAdapter(GatewayAdapter):
    """WeLink渠道适配器：事件回调格式 + 签名验证 + 回调API推送。"""

    channel = "welink"

    async def parse_request(self, request: Request) -> ChatRequest:
        """解析 WeLink 事件回调格式。"""
        try:
            body = await request.json()
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"无效的请求体: {e}") from e

        # WeLink 事件回调格式
        event = body.get("event", body)
        content = event.get("content", body.get("content", ""))
        from_user = event.get("from", body.get("from", {}))

        if isinstance(content, dict):
            query = content.get("text", content.get("message", ""))
        else:
            query = str(content)

        if not query:
            raise HTTPException(status_code=400, detail="无法从回调中提取用户问题")

        # 认证
        user = await self.authenticate(request)

        # WeLink conversation_id 作为 session_id
        conversation_id = event.get("conversationId", body.get("conversationId", ""))

        return ChatRequest(
            query=query,
            session_id=conversation_id,
            chat_id=body.get("msgId", ""),
            user=UserInfo(
                user_id=user.user_id or from_user.get("userId", ""),
                user_name=user.user_name or from_user.get("userName", ""),
            ),
            business=BusinessInfo(
                source_site="welink",
                extra={"welink_raw": body},
            ),
        )

    async def format_response(self, chat_request: ChatRequest, team: BITeam) -> JSONResponse:
        """非流式：收集完整结果后通过回调API推送。"""
        await team.reset()

        try:
            result, _recorder = await team.run(chat_request)
        except Exception as e:
            logger.error("[WeLinkAdapter] 执行失败: %s", e, exc_info=True)
            result = f"抱歉，执行失败。({e})"
        finally:
            await team.reset()

        # 异步推送结果到 WeLink（不阻塞响应）
        self._push_to_welink(chat_request, result)

        # WeLink 回调需要快速返回 200
        return JSONResponse(content={"code": 0, "message": "ok"})

    async def authenticate(self, request: Request) -> UserInfo:
        """验证 WeLink 回调签名。"""
        from config import settings

        app_secret = getattr(settings, "welink_app_secret", "")
        if not app_secret:
            # 未配置密钥，跳过验证
            return UserInfo()

        timestamp = request.headers.get("X-Welink-Timestamp", "")
        nonce = request.headers.get("X-Welink-Nonce", "")
        signature = request.headers.get("X-Welink-Signature", "")

        if not all([timestamp, nonce, signature]):
            raise HTTPException(status_code=401, detail="缺少 WeLink 签名头")

        # 验签：HMAC-SHA256(timestamp + nonce + body)
        body = await request.body()
        message = f"{timestamp}{nonce}".encode() + body
        expected = hmac.new(app_secret.encode(), message, hashlib.sha256).hexdigest()

        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="WeLink 签名验证失败")

        return UserInfo()

    @staticmethod
    def _push_to_welink(chat_request: ChatRequest, result: str) -> None:
        """异步推送结果到 WeLink 消息回调 API。"""
        import asyncio
        from urllib.parse import urlparse

        from config import settings

        welink_url = chat_request.business.extra.get("welink_raw", {}).get("callbackUrl", "")
        if not welink_url:
            logger.debug("[WeLinkAdapter] 无回调URL，跳过推送")
            return

        # SSRF 防护：校验回调 URL（DNS 解析后二次校验）
        try:
            import ipaddress
            import socket

            parsed = urlparse(welink_url)
            if parsed.scheme not in ("http", "https"):
                logger.warning("[WeLinkAdapter] 回调URL协议不合法: %s", welink_url)
                return
            hostname = parsed.hostname or ""

            # DNS 解析后校验 IP
            try:
                addr_infos = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
                for family, _type, _proto, _canonname, sockaddr in addr_infos:
                    ip_str = sockaddr[0]
                    try:
                        ip = ipaddress.ip_address(ip_str)
                        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                            logger.warning("[WeLinkAdapter] 回调URL指向内网/保留地址: %s -> %s", hostname, ip_str)
                            return
                    except ValueError:
                        continue
            except socket.gaierror:
                pass

            # 域名白名单
            if settings.welink_allowed_domains and hostname not in settings.welink_allowed_domains:
                logger.warning("[WeLinkAdapter] 回调域名不在白名单: %s", hostname)
                return
        except Exception as e:
            logger.warning("[WeLinkAdapter] 回调URL校验失败: %s", e)
            return

        async def _do_push() -> None:
            try:
                import httpx
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(welink_url, json={"msgType": "TEXT", "text": result[:4000]})
                    logger.info("[WeLinkAdapter] 结果已推送到WeLink")
            except Exception as e:
                logger.error("[WeLinkAdapter] 推送WeLink失败: %s", e)

        asyncio.create_task(_do_push())