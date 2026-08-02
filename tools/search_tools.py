#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
知识搜索工具
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass

import httpx
import pandas as pd
from autogen_core.tools import FunctionTool

from config import SearchError
from core.data_context import DataContext

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具元数据 — 供 RAGAgent 条件注册 + prompt 渲染使用
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchToolMeta:
    """搜索工具元数据：工具自描述，Agent 消费。

    Attributes:
        name: 工具名（与 FunctionTool.name 一致）。
        url_attr: settings 中的 URL 属性名，用于判断是否已配置。
        label: 工具简短标签（技能目录用）。
        scene: 适用场景描述（策略表用）。
        guide: 使用策略文本（工具指南用）。
        factory: FunctionTool 工厂函数名。
        api_key_attr: settings 中的 API Key 属性名（可选，用于需要鉴权的搜索服务）。
    """

    name: str
    url_attr: str
    label: str
    scene: str
    guide: str
    factory: str
    api_key_attr: str = ""


SEARCH_TOOL_META: list[SearchToolMeta] = [
    SearchToolMeta(
        name="knowledge_search",
        url_attr="search_knowledge_url",
        label="内部知识库搜索",
        scene="内部文档、Wiki、技术文章",
        guide=(
            "### knowledge_search 使用策略\n"
            "- query: 搜索关键词，中文即可\n"
            "- language: cn(默认)/en\n"
            "- num: 结果数量1-10，默认5\n"
            "- 适用：企业内部文档、Wiki、技术文章"
        ),
        factory="make_knowledge_search_tool",
    ),
    SearchToolMeta(
        name="web_search",
        url_attr="web_search_url",
        label="联网搜索",
        scene="互联网公开信息",
        guide=(
            "### web_search 使用策略\n"
            "- query: 搜索关键词\n"
            "- top_n: 结果数量1-20，默认10\n"
            "- 适用：互联网公开信息（新闻、百科、技术文档）\n"
            "- 注意：领域相关性较弱，优先用knowledge_search"
        ),
        factory="make_web_search_tool",
        api_key_attr="web_search_api_key",
    ),
]


def get_uid() -> str:
    """获取当前用户ID。"""
    return os.environ.get("USERNAME", "") or os.environ.get("USER", "") or ""


def rm_html_tag(src_str: str) -> str:
    """移除HTML标签，将<>替换为---防止注入。"""
    clean = re.compile(r"<.*?>")
    inject_1 = re.compile(r"<")
    inject_2 = re.compile(r">")
    dst_str1 = re.sub(clean, "", src_str)
    dst_str2 = re.sub(inject_1, "---", dst_str1)
    return re.sub(inject_2, "---", dst_str2)


def search_data_to_prompt(search_data: list[dict], max_result_length: int = 8000) -> str:
    """将搜索结果转换为LLM可读的文本格式。

    top5 全文（chunk>10000用摘要），top5以外使用摘要。
    最终结果截断到 max_result_length 字符，防止 token 爆炸。
    """
    ret: list[str] = []
    for index, obj in enumerate(search_data):
        title_show = obj.get("subtitle", "") if "subtitle" in obj else obj.get("title", "")
        content = obj.get("content", "")
        if index < 5 and len(content) > 10000:
            abstract = obj.get("abstract", "") or obj.get("abstract_md", "")
            content = f"\n{abstract}"
        if index >= 5:
            content = obj.get("abstract", "") or obj.get("abstract_md", "")
        ret.append(f"[{index + 1}]")
        if title_show:
            ret.append(rm_html_tag(title_show))
        if content:
            content = content.replace("\n", " ")
            content = content.replace("^", "")
            content = re.sub("image_[0-9]{1,3}", "", content)
            content = re.sub("table_[0-9]{1,3}", "", content)
            content = rm_html_tag(content)
            ret.append(content)

    result = "\n".join(ret)

    # 截断到最大长度，防止 token 爆炸
    if len(result) > max_result_length:
        result = result[:max_result_length] + "\n\n[搜索结果已截断，完整内容过长]"

    return result


class KnowledgeSearch:
    """企业知识库搜索工具 - 搜索内部文档、Wiki、技术文章等。"""

    def __init__(
        self,
        api_url: str | None = None,
        app_id: str = "ikbg",
        timeout_seconds: int = 15,
        max_retries: int = 2,
        max_result_length: int | None = None,
    ) -> None:
        from config import settings

        self.api_url = api_url or settings.search_knowledge_url
        self.app_id = app_id
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_result_length = (
            max_result_length if max_result_length is not None else settings.search_max_result_length
        )

    async def execute(self, query: str, language: str = "cn", num: int = 5) -> str:
        """执行知识搜索。

        Args:
            query: 搜索关键词
            language: 结果语言 ('cn' 或 'en')
            num: 返回结果数量 (1-10)

        Returns:
            搜索结果文本，供LLM直接使用
        """
        if not query or not query.strip():
            return "错误：搜索关键词不能为空"

        query = query.strip()
        num = max(1, min(10, num))

        logger.info("[KnowledgeSearch] 查询：'%s', language=%s, num=%d", query, language, num)

        headers = {
            "Content-Type": "application/json",
            "app-id": self.app_id,
        }
        body = {
            "user_id": get_uid(),
            "language": language,
            "num": num,
            "query": query,
            "out_fields": ["content", "scenario"],
            "must_count": 0,
            "core_count": 7,
            "fragment_size": 500,
            "number_of_fragments": 3,
            "method": "base",
            "source_web": "ikbg-claw",
        }

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, verify=False) as client:
                    response = await client.post(self.api_url, headers=headers, json=body)
                    if response.status_code == 200:
                        result = response.json()
                        data = result.get("data", [])
                        logger.info("[KnowledgeSearch] 成功，返回 %d 条结果", len(data))
                        return search_data_to_prompt(data, self.max_result_length)
                    else:
                        error_msg = "unknown error"
                        try:
                            error_msg = response.json().get("return_msg", error_msg)
                        except Exception:
                            error_msg = response.text[:200]
                        raise SearchError("knowledge", f"HTTP {response.status_code}: {error_msg}")

            except SearchError:
                raise
            except httpx.TimeoutException:
                last_error = f"请求超时 ({self.timeout_seconds}秒)"
                logger.warning("[KnowledgeSearch] 超时 (attempt %d)", attempt + 1)
            except httpx.HTTPError as e:
                last_error = f"网络错误：{e}"
                logger.warning("[KnowledgeSearch] 网络错误 (attempt %d): %s", attempt + 1, e)
            except Exception as e:
                last_error = f"未知错误：{e}"
                logger.error("[KnowledgeSearch] 异常 (attempt %d): %s", attempt + 1, e, exc_info=True)

            if attempt < self.max_retries:
                await asyncio.sleep(1 * (attempt + 1))

        raise SearchError("knowledge", f"所有重试失败：{last_error}")


class WebSearch:
    """百度千帆联网搜索工具 - 通过百度搜索 API 搜索互联网公开信息。

    使用百度千帆 AI Search 接口，支持网页搜索，返回结构化搜索结果。
    需要 API URL 和 Bearer token（API Key）。
    """

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout_seconds: int = 15,
        max_retries: int = 2,
        max_result_length: int | None = None,
    ) -> None:
        from config import settings

        self.api_url = api_url or settings.web_search_url
        self.api_key = api_key or settings.web_search_api_key.get_secret_value()
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_result_length = (
            max_result_length if max_result_length is not None else settings.search_max_result_length
        )

    async def execute(self, query: str, top_n: int = 10) -> str:
        """执行联网搜索。

        Args:
            query: 搜索关键词
            top_n: 返回结果数量 (1-20)

        Returns:
            搜索结果文本，供LLM直接使用

        Raises:
            SearchError: 搜索请求失败时抛出。
        """
        if not query or not query.strip():
            return "错误：搜索关键词不能为空"

        query = query.strip()
        top_n = max(1, min(20, top_n))

        logger.info("[WebSearch] 查询：'%s', top_n=%d", query, top_n)

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }
        body = {
            "messages": [{"role": "user", "content": query}],
            "edition": "standard",
            "search_source": "baidu_search_v2",
            "resource_type_filter": [
                {"type": "web", "top_k": top_n},
                {"type": "video", "top_k": 0},
                {"type": "image", "top_k": 0},
                {"type": "aladdin", "top_k": 0},
            ],
        }

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, verify=False) as client:
                    response = await client.post(self.api_url, headers=headers, json=body)
                    if response.status_code == 200:
                        result = response.json()
                        references = result.get("references", [])
                        logger.info("[WebSearch] 成功，返回 %d 条结果", len(references))
                        normalized = self._normalize_references(references)
                        return search_data_to_prompt(normalized, self.max_result_length)
                    else:
                        error_msg = "unknown error"
                        try:
                            error_msg = response.json().get("error_msg", error_msg)
                        except Exception:
                            error_msg = response.text[:200]
                        raise SearchError("web", f"HTTP {response.status_code}: {error_msg}")

            except SearchError:
                raise
            except httpx.TimeoutException:
                last_error = f"请求超时 ({self.timeout_seconds}秒)"
                logger.warning("[WebSearch] 超时 (attempt %d)", attempt + 1)
            except httpx.HTTPError as e:
                last_error = f"网络错误：{e}"
                logger.warning("[WebSearch] 网络错误 (attempt %d): %s", attempt + 1, e)
            except Exception as e:
                last_error = f"未知错误：{e}"
                logger.error("[WebSearch] 异常 (attempt %d): %s", attempt + 1, e, exc_info=True)

            if attempt < self.max_retries:
                await asyncio.sleep(1 * (attempt + 1))

        raise SearchError("web", f"所有重试失败：{last_error}")

    @staticmethod
    def _normalize_references(references: list[dict]) -> list[dict]:
        """将百度千帆搜索结果归一化为 search_data_to_prompt 所需格式。

        Args:
            references: 百度千帆 API 返回的 references 列表。

        Returns:
            归一化后的搜索结果列表，每项含 title/content/abstract 字段。
        """
        normalized: list[dict] = []
        for ref in references:
            normalized.append({
                "title": ref.get("title", ""),
                "subtitle": ref.get("website", ""),
                "content": ref.get("content", ""),
                "abstract": ref.get("snippet", ""),
                "url": ref.get("url", ""),
                "date": ref.get("date", ""),
            })
        return normalized


# ---------------------------------------------------------------------------
# FunctionTool 工厂（供 RAGAgent 使用）
# ---------------------------------------------------------------------------

async def _save_to_context(data_context: DataContext, search_type: str, query: str, result: str) -> None:
    """将搜索结果存入DataContext，供下游Agent引用。"""
    try:
        key = data_context.generate_key("RAGAgent")
        df = pd.DataFrame({"search_type": [search_type], "query": [query], "search_result": [result]})
        await data_context.put(key, df)
        logger.info("[search_tools] 搜索结果已存入DataContext: %s", key)
    except Exception as e:
        logger.warning("[search_tools] 搜索结果存入DataContext失败: %s", e)


def make_knowledge_search_tool(data_context: DataContext) -> FunctionTool:
    """创建企业知识库搜索 FunctionTool。"""

    async def _knowledge_search(query: str, language: str = "cn", num: int = 5) -> str:
        searcher = KnowledgeSearch()
        result = await searcher.execute(query, language, num)
        await _save_to_context(data_context, "knowledge_search", query, result)
        return result

    return FunctionTool(
        func=_knowledge_search,
        name="knowledge_search",
        description="企业知识库搜索。搜索企业内部文档、Wiki、技术文章等。参数：query(搜索关键词), language(cn/en,默认cn), num(结果数量1-10,默认5)",
    )


def make_web_search_tool(data_context: DataContext) -> FunctionTool:
    """创建百度千帆联网搜索 FunctionTool。"""

    async def _web_search(query: str, top_n: int = 10) -> str:
        searcher = WebSearch()
        result = await searcher.execute(query, top_n)
        await _save_to_context(data_context, "web_search", query, result)
        return result

    return FunctionTool(
        func=_web_search,
        name="web_search",
        description="联网搜索。通过百度搜索互联网公开信息，包括新闻、百科、技术文档等。参数：query(搜索关键词), top_n(结果数量1-20,默认10)",
    )