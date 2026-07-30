
#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
知识搜索工具 - 支持华为内部知识库搜索、W3社区搜索、小艺联网搜索
"""
from __future__ import annotations

import asyncio
import logging
import os
import re

import httpx
import pandas as pd
from autogen_core.tools import FunctionTool

from config import SearchError
from core.data_context import DataContext

logger = logging.getLogger(__name__)


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
    """企业知识库搜索工具 - 搜索华为内部文档、Wiki、技术文章等。"""

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
        self.max_result_length = max_result_length if max_result_length is not None else settings.search_max_result_length

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


class W3Search:
    """W3社区综合搜索工具 - 支持8类资源搜索。"""

    def __init__(
        self,
        api_url: str | None = None,
        timeout_seconds: int = 5,
        max_retries: int = 2,
        max_result_length: int | None = None,
    ) -> None:
        from config import settings

        self.api_url = api_url or settings.search_w3_url
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_result_length = max_result_length if max_result_length is not None else settings.search_max_result_length

    async def execute(self, query: str) -> str:
        """执行W3搜索。

        Args:
            query: 搜索关键词

        Returns:
            搜索结果文本，供LLM直接使用
        """
        if not query or not query.strip():
            return "错误：搜索关键词不能为空"

        query = query.strip()
        logger.info("[W3Search] 查询：'%s'", query)

        headers = {"Content-Type": "application/json"}
        body = {
            "user_id": get_uid(),
            "query": query,
        }

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, verify=False) as client:
                    response = await client.post(self.api_url, headers=headers, json=body)
                    if response.status_code == 200:
                        result = response.json()
                        data = result.get("data", {}).get("w3_search_data", [])
                        logger.info("[W3Search] 成功，返回 %d 条结果", len(data))
                        return search_data_to_prompt(data, self.max_result_length)
                    else:
                        raise SearchError("w3", f"HTTP {response.status_code}")

            except SearchError:
                raise
            except httpx.TimeoutException:
                last_error = f"请求超时 ({self.timeout_seconds}秒)"
                logger.warning("[W3Search] 超时 (attempt %d)", attempt + 1)
            except httpx.HTTPError as e:
                last_error = f"网络错误：{e}"
                logger.warning("[W3Search] 网络错误 (attempt %d): %s", attempt + 1, e)
            except Exception as e:
                last_error = f"未知错误：{e}"
                logger.error("[W3Search] 异常 (attempt %d): %s", attempt + 1, e, exc_info=True)

            if attempt < self.max_retries:
                await asyncio.sleep(1 * (attempt + 1))

        raise SearchError("w3", f"所有重试失败：{last_error}")


class XiaoYiSearch:
    """小艺联网搜索工具 - 搜索互联网公开信息。"""

    def __init__(
        self,
        api_url: str | None = None,
        app_id: str = "com.huawei.make.mes.mesai.ikbg",
        timeout_seconds: int = 15,
        max_retries: int = 2,
        max_result_length: int | None = None,
    ) -> None:
        from config import settings

        self.api_url = api_url or settings.search_xiaoyi_url
        self.app_id = app_id
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.max_result_length = max_result_length if max_result_length is not None else settings.search_max_result_length

    async def execute(self, query: str, top_n: int = 10) -> str:
        """执行联网搜索。

        Args:
            query: 搜索关键词
            top_n: 返回结果数量 (1-20)

        Returns:
            搜索结果文本，供LLM直接使用
        """
        if not query or not query.strip():
            return "错误：搜索关键词不能为空"

        query = query.strip()
        top_n = max(1, min(20, top_n))

        logger.info("[XiaoYiSearch] 查询：'%s', top_n=%d", query, top_n)

        headers = {
            "Content-Type": "application/json",
            "app-id": self.app_id,
        }
        body = {
            "query": query,
            "user_id": get_uid(),
            "num": top_n,
        }

        last_error = ""
        for attempt in range(self.max_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout_seconds, verify=False) as client:
                    response = await client.post(self.api_url, headers=headers, json=body)
                    if response.status_code == 200:
                        result = response.json()
                        logger.info("[XiaoYiSearch] 成功，返回 %d 条结果", len(result))
                        return search_data_to_prompt(result, self.max_result_length)
                    else:
                        raise SearchError("xiaoyi", f"HTTP {response.status_code}")

            except SearchError:
                raise
            except httpx.TimeoutException:
                last_error = f"请求超时 ({self.timeout_seconds}秒)"
                logger.warning("[XiaoYiSearch] 超时 (attempt %d)", attempt + 1)
            except httpx.HTTPError as e:
                last_error = f"网络错误：{e}"
                logger.warning("[XiaoYiSearch] 网络错误 (attempt %d): %s", attempt + 1, e)
            except Exception as e:
                last_error = f"未知错误：{e}"
                logger.error("[XiaoYiSearch] 异常 (attempt %d): %s", attempt + 1, e, exc_info=True)

            if attempt < self.max_retries:
                await asyncio.sleep(1 * (attempt + 1))

        raise SearchError("xiaoyi", f"所有重试失败：{last_error}")


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
        description="企业知识库搜索。搜索华为内部文档、Wiki、技术文章等。参数：query(搜索关键词), language(cn/en,默认cn), num(结果数量1-10,默认5)",
    )


def make_w3_search_tool(data_context: DataContext) -> FunctionTool:
    """创建W3社区搜索 FunctionTool。"""

    async def _w3_search(query: str) -> str:
        searcher = W3Search()
        result = await searcher.execute(query)
        await _save_to_context(data_context, "w3_search", query, result)
        return result

    return FunctionTool(
        func=_w3_search,
        name="w3_search",
        description="W3社区综合搜索。支持8类资源：员工、知识、社区、发文、应用、组织、文档、视频。参数：query(搜索关键词，查员工格式：'部门 姓名'或'员工工号')",
    )


def make_xiaoyi_search_tool(data_context: DataContext) -> FunctionTool:
    """创建小艺联网搜索 FunctionTool。"""

    async def _xiaoyi_search(query: str, top_n: int = 10) -> str:
        searcher = XiaoYiSearch()
        result = await searcher.execute(query, top_n)
        await _save_to_context(data_context, "xiaoyi_search", query, result)
        return result

    return FunctionTool(
        func=_xiaoyi_search,
        name="xiaoyi_search",
        description="小艺联网搜索。搜索互联网公开信息，包括新闻、百科、技术文档等。参数：query(搜索关键词), top_n(结果数量1-20,默认10)",
    )