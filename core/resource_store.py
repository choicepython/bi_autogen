
"""资源元数据存储抽象 — API/表资源的搜索与查找。

提供 ResourceStore ABC 和两种实现：
- LocalResourceStore：从 source/ 目录读取 JSONL，jieba 分词 + 加权关键词匹配
- ESResourceStore：封装现有 es_query() 调用，DSL 从 tools/ 迁移至此

通过 core/resource_factory.py 的工厂函数选择实现，调用方不直接引用具体类。
"""

from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from config.settings import settings

logger = logging.getLogger(__name__)

# 项目根目录（core/ 的父目录）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_SOURCE_DIR = _PROJECT_ROOT / "source"

# 搜索结果上限（对齐 ES DSL 的 size: 5）
_MAX_RESULTS = 5

# 匹配 JSON 中尾随逗号（], } 前的逗号），用于清理 JSONC 风格的模板文件
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _strip_trailing_commas(text: str) -> str:
    """去除 JSON 文本中的尾随逗号（JSONC 兼容）。"""
    return _TRAILING_COMMA_RE.sub(r"\1", text)


def _parse_jsonl(raw: str, filename: str) -> list[dict[str, Any]]:
    """解析 JSONL 文本为记录列表。

    支持两种格式：
    - 标准 JSONL：每行一个独立 JSON 对象
    - 格式化 JSON：整个文件为一个 JSON 对象或 JSON 数组（多行缩进）

    自动清理尾随逗号（JSONC 风格），兼容用户手写模板。

    Args:
        raw: 文件文本内容。
        filename: 文件名（用于日志）。

    Returns:
        解析出的记录列表。解析失败时返回空列表。
    """
    # 先尝试逐行解析（标准 JSONL）
    records: list[dict[str, Any]] = []
    has_parse_error = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
            elif isinstance(obj, list):
                records.extend(o for o in obj if isinstance(o, dict))
        except json.JSONDecodeError:
            has_parse_error = True
            break

    if records:
        return records

    # 逐行解析失败或无有效记录 → 尝试整体解析为格式化 JSON
    if has_parse_error:
        cleaned = _strip_trailing_commas(raw)
        try:
            obj = json.loads(cleaned)
            if isinstance(obj, dict):
                return [obj]
            if isinstance(obj, list):
                return [o for o in obj if isinstance(o, dict)]
        except json.JSONDecodeError as e:
            logger.warning("[LocalResourceStore] %s JSON 解析失败: %s", filename, e)

    return []


class ResourceStore(ABC):
    """资源元数据存储抽象接口。"""

    @abstractmethod
    async def search_by_query(self, query: str, source_site: str = "") -> list[dict[str, Any]]:
        """按用户问题模糊搜索匹配的 API/表元数据。

        Args:
            query: 用户原始问题。
            source_site: API来源站点过滤。

        Returns:
            匹配的资源元数据列表（最多 _MAX_RESULTS 条）。
        """

    @abstractmethod
    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        """按名称精确查找资源。

        Args:
            name: 资源名称（如 API 工具名）。

        Returns:
            匹配的资源元数据，未找到时返回 None。
        """


class LocalResourceStore(ResourceStore):
    """本地 JSONL 文件资源存储，使用 jieba 分词 + 加权关键词匹配。

    从 source/ 目录加载 .jsonl 文件（每行一个 JSON 对象），
    懒加载 + 缓存（同 SkillManager 模式）。
    替代 ES 的 multi_match + ik_max_word 查询。
    """

    def __init__(self, source_dir: Path | None = None) -> None:
        self._source_dir = source_dir or _DEFAULT_SOURCE_DIR
        self._records: list[dict[str, Any]] | None = None

    def _load_records(self) -> list[dict[str, Any]]:
        """加载 source/ 目录下所有 .jsonl 文件。

        过滤 name 为空的模板记录，结果缓存。
        """
        if self._records is not None:
            return self._records

        records: list[dict[str, Any]] = []
        if not self._source_dir.is_dir():
            logger.warning("[LocalResourceStore] source 目录不存在: %s", self._source_dir)
            self._records = records
            return records

        for jsonl_file in sorted(self._source_dir.glob("*.jsonl")):
            try:
                raw = jsonl_file.read_text(encoding="utf-8")
            except OSError as e:
                logger.warning("[LocalResourceStore] 读取失败: %s — %s", jsonl_file, e)
                continue

            file_records = _parse_jsonl(raw, jsonl_file.name)
            for record in file_records:
                if not record.get("name"):
                    continue
                records.append(record)

        logger.info("[LocalResourceStore] 加载了 %d 条资源记录（from %s）", len(records), self._source_dir)
        self._records = records
        return records

    async def search_by_query(self, query: str, source_site: str = "") -> list[dict[str, Any]]:
        """jieba 分词 + 加权关键词匹配。

        字段权重对齐 ES DSL: kpi^1.2, description^1.0, keywords^1.0, parameters^0.2
        """
        import jieba

        records = self._load_records()
        if not records:
            return []

        # source_site 过滤：记录的 source_site 为空时跳过过滤（包容所有请求）
        if source_site:
            filtered = [
                r for r in records
                if not r.get("source_site") or r.get("source_site") == source_site
            ]
        else:
            filtered = records

        if not filtered:
            return []

        # jieba 搜索模式分词（更细粒度，对齐 ES ik_max_word）
        query_tokens = [t for t in jieba.cut_for_search(query) if t.strip()]

        scored: list[tuple[float, dict[str, Any]]] = []
        for record in filtered:
            score = self._score_record(query_tokens, record)
            if score > 0:
                scored.append((score, record))

        # 按分数降序，取 top N
        scored.sort(key=lambda x: x[0], reverse=True)
        results = [record for _, record in scored[:_MAX_RESULTS]]

        logger.info(
            "[LocalResourceStore] 查询 '%s' 匹配到 %d/%d 个资源",
            query[:50], len(results), len(filtered),
        )
        return results

    def _score_record(self, query_tokens: list[str], record: dict[str, Any]) -> float:
        """计算查询与记录的加权匹配分数。

        Args:
            query_tokens: jieba 分词后的查询 token 列表。
            record: 资源元数据记录。

        Returns:
            匹配分数（0 表示无匹配）。
        """
        score = 0.0
        kpi_text = " ".join(str(k) for k in record.get("kpi", []))
        keywords_text = " ".join(str(k) for k in record.get("keywords", []))
        desc_text = str(record.get("description", ""))
        params_text = str(record.get("parameters", ""))

        for token in query_tokens:
            if not token.strip():
                continue
            if token in kpi_text:
                score += 1.2
            if token in keywords_text:
                score += 1.0
            if token in desc_text:
                score += 1.0
            if token in params_text:
                score += 0.2
        return score

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        """按名称精确查找资源。"""
        records = self._load_records()
        for record in records:
            if record.get("name") == name:
                return record
        return None

    def reload(self) -> None:
        """清除缓存，下次搜索时重新加载。"""
        self._records = None
        logger.info("[LocalResourceStore] 缓存已清除")


class ESResourceStore(ResourceStore):
    """ES 资源存储实现 — 封装现有 es_query() 调用。

    DSL 从 tools/get_es_data.py 和 tools/api_query.py 迁移至此，
    调用方不再直接引用 es_query()。索引名从 settings.es_resource_index 读取，
    启动时由 startup_check 调用 ensure_index_exists 确保已创建。
    """

    async def search_by_query(self, query: str, source_site: str = "") -> list[dict[str, Any]]:
        """ES multi_match 查询，返回 top 5。"""
        from utils.es_query import es_query

        index = settings.es_resource_index
        dsl: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query,
                                "type": "cross_fields",
                                "operator": "or",
                                "analyzer": "ik_max_word",
                                "fields": ["description", "keywords", "kpi^1.2", "column^0.2", "parameters^0.2"],
                            }
                        }
                    ],
                    "filter": [{"term": {"source_site": source_site}}],
                }
            },
            "size": _MAX_RESULTS,
        }
        df = await es_query(index, dsl)
        df = df.fillna("")
        return df.to_dict(orient="records")

    async def get_by_name(self, name: str) -> dict[str, Any] | None:
        """ES match 精确名查询。"""
        from utils.es_query import es_query

        index = settings.es_resource_index
        dsl: dict[str, Any] = {
            "query": {
                "bool": {
                    "must": [{"match": {"name": name}}],
                }
            },
            "size": 1,
        }
        df = await es_query(index, dsl)
        df = df.fillna("")
        data = df.to_dict(orient="records")
        return data[0] if data else None
