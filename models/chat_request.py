
"""BI Agent 系统的统一请求输入模型。

交互层（FastAPI / CLI / SDK）构造 ChatRequest 传入 BITeam.run_stream()。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class UserInfo(BaseModel):
    """用户身份信息。"""

    user_id: str = ""
    user_name: str = ""
    permissions: list[str] = []  # 权限列表，如 ["data_query", "report_generate"]


class BusinessInfo(BaseModel):
    """业务上下文信息。"""

    source_site: str = "les_portal"  # API来源站点过滤
    custom_env: dict[str, Any] = {}  # 自定义参数注入（DynamicAPITool的over_write/no_over_write）
    extra: dict[str, Any] = {}  # 扩展字段，供业务定制


class ChatRequest(BaseModel):
    """BI Agent 系统的统一请求输入模型。

    交互层（FastAPI / CLI / SDK）构造此对象传入 BITeam.run_stream()。

    字段分3类：
    1. 基础字段：query、session_id、chat_id
    2. 用户信息：user（user_id、user_name、permissions）
    3. 业务信息：business（source_site、custom_env、extra）

    数据流::

        ChatRequest → BITeam.run_stream() → SessionContext → Agent/Tool
    """

    # --- 基础字段 ---
    query: str = Field(..., min_length=1, max_length=5000)  # 用户原始问题
    session_id: str = ""  # 外部会话ID（空则自动生成）
    chat_id: str = ""  # 对话轮次ID（多轮对话时使用）

    # --- 用户信息 ---
    user: UserInfo = UserInfo()  # 用户身份

    # --- 业务信息 ---
    business: BusinessInfo = BusinessInfo()  # 业务上下文

    # --- 路由覆盖 ---
    agent_type: str = ""  # 强制路由到指定Agent（Layer 1参数路由）

    # --- 模型参数 ---
    enable_thinking: bool | None = None  # 是否启用模型思考（None=使用配置默认值）