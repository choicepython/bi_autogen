
"""渠道适配器：不同消息来源的请求解析、认证和响应格式化。

GatewayAdapter 是核心抽象，每个渠道（Web/CLI）实现自己的适配器。
适配器负责：
1. parse_request: 将渠道特有的请求格式转为统一的 ChatRequest
2. authenticate: 按渠道方式验证用户身份
3. format_response: 执行任务并返回渠道适配的响应格式
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod

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
