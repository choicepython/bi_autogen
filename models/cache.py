
"""结果缓存数据模型：缓存条目的结构和缓存查询结果。"""
from __future__ import annotations

from pydantic import BaseModel

from models.routing import RoutingResult
from models.stream_event import StreamEvent


class ResultCacheEntry(BaseModel):
    """结果级缓存条目：存储完整的 StreamEvent 序列和路由结果。

    缓存的事件不包含 SESSION_START/SESSION_END，
    这两个事件由 DispatchLayer 在每次请求时重新生成。
    """

    # 缓存键组成
    query: str
    source_site: str = "les_portal"

    # 缓存内容
    events: list[StreamEvent] = []  # 可重放的 StreamEvent 列表（不含 SESSION_START/END）
    final_result: str = ""  # 最终结果文本
    routing_result: RoutingResult | None = None  # 路由结果（用于 DB 写入）

    # 元数据
    created_at: str = ""  # 缓存时间 ISO 8601
    hit_count: int = 0  # 缓存命中次数
    original_session_id: str = ""  # 首次缓存时的 session_id

    # 事件统计（用于缓存淘汰权重计算）
    event_count: int = 0
    has_table_data: bool = False  # 是否包含 TABLE 事件


class CacheLookupResult(BaseModel):
    """缓存查询结果：标记命中/未命中和来源。"""

    hit: bool = False
    entry: ResultCacheEntry | None = None
    source: str = ""  # "exact" | "semantic" | ""
    similarity: float = 0.0  # 语义命中时的相似度