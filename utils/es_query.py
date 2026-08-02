
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd
from elasticsearch7 import Elasticsearch
from elasticsearch7.exceptions import ElasticsearchException, NotFoundError

from config.exceptions import BIAgentError
from config.settings import settings
from core.data_context import DataContext

logger = logging.getLogger(__name__)

_client: Elasticsearch | None = None

# ES schema 文件默认目录（db/es_schema/）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SCHEMA_DIR = _PROJECT_ROOT / "db" / "es_schema"


class ESQueryError(BIAgentError):
    """Raised when ES query fails."""

    def __init__(self, index: str, detail: str = "") -> None:
        self.index = index
        super().__init__(f"ES query failed on index '{index}': {detail}")


def get_es_client() -> Elasticsearch:
    """获取ES客户端单例（懒初始化，复用连接）。"""
    global _client
    if _client is not None:
        return _client

    hosts = [h.strip() for h in settings.es_hosts.split(",") if h.strip()]
    kwargs: dict[str, Any] = {
        "hosts": hosts,
        "timeout": settings.es_timeout,
    }
    if settings.es_username and settings.es_password.get_secret_value():
        kwargs["http_auth"] = (settings.es_username, settings.es_password.get_secret_value())

    _client = Elasticsearch(**kwargs)
    return _client


def ensure_index_exists(index: str, schema_path: Path | None = None) -> bool:
    """确保 ES 索引存在，不存在则按 schema 创建（幂等）。

    索引已存在时直接返回 True；创建失败时记录 ERROR 日志并返回 False，
    不抛异常（遵循"警告不阻塞"策略，避免 ES 故障阻断应用启动）。

    Args:
        index: ES 索引名。
        schema_path: mapping schema 文件路径。None 时用 db/es_schema/{index}.json。

    Returns:
        True 表示索引已就绪（存在或创建成功），False 表示创建失败或 schema 缺失。
    """
    client = get_es_client()
    path = schema_path or (_DEFAULT_SCHEMA_DIR / f"{index}.json")

    # 索引已存在，直接返回
    if client.indices.exists(index=index):
        logger.info("[ES] 索引 '%s' 已存在", index)
        return True

    # schema 文件缺失，无法创建
    if not path.is_file():
        logger.error("[ES] 索引 '%s' 不存在且 schema 文件缺失: %s", index, path)
        return False

    # 读取 schema 并创建索引
    try:
        mapping = json.loads(path.read_text(encoding="utf-8"))
        client.indices.create(index=index, body=mapping)
        logger.info("[ES] 索引 '%s' 创建成功（schema: %s）", index, path.name)
        return True
    except ElasticsearchException as e:
        logger.error("[ES] 索引 '%s' 创建失败: %s", index, e)
        return False


async def es_query(
        index: str,
        dsl: dict[str, Any],
        data_context: DataContext | None = None,
) -> pd.DataFrame | str:
    """执行ES DSL查询，返回DataFrame或DataContext摘要。

    Args:
        index: ES索引名称，支持通配符如 "log-*"。
        dsl: 完整的ES查询DSL，如 {"query": {"match_all": {}}, "size": 10}。
        data_context: 如果提供，查询结果自动存入DataContext并返回摘要字符串。

    Returns:
        传入data_context时返回摘要字符串，否则返回DataFrame。
    """
    client = get_es_client()

    try:
        resp = client.search(index=index, body=dsl)
    except NotFoundError:
        logger.warning("ES index '%s' not found, returning empty DataFrame", index)
        return pd.DataFrame()
    except ElasticsearchException as e:
        raise ESQueryError(index, detail=str(e)) from e

    hits = resp.get("hits", {}).get("hits", [])
    if not hits:
        logger.info("ES query returned 0 hits on index '%s'", index)
        return pd.DataFrame()

    rows = [hit["_source"] for hit in hits]
    for row, hit in zip(rows, hits, strict=True):
        row["_id"] = hit["_id"]
        row["_index"] = hit.get("_index", "")
        if "_score" in hit:
            row["_score"] = hit["_score"]

    df = pd.DataFrame(rows)
    df = df[df["_score"]>1.8]
    total = resp.get("hits", {}).get("total", {})
    total_value = total.get("value", len(rows)) if isinstance(total, dict) else total
    logger.info("ES query on '%s' returned %d/%d hits", index, len(rows), total_value)

    if data_context is not None and not df.empty:
        key = data_context.generate_key("ESQuery")
        await data_context.put(key, df, meta={"index": index, "dsl": dsl})
        summary = data_context.summarize(key)
        return f"ES查询成功，索引 '{index}' 返回 {len(df)} 条数据（共{total_value}条），已存入DataContext(key={key})。\n\n{summary}"

    return df
