
"""BI Agent Gateway 模块：多渠道接入层。"""

from gateway.adapter import GatewayAdapter, WebAdapter, WeLinkAdapter

__all__ = ["GatewayAdapter", "WebAdapter", "WeLinkAdapter"]