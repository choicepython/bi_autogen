
#!/usr/bin/env python
# -*- coding: UTF-8 -*-
"""
@Project ：bi_autogen
@File ：get_es_data.py
@IDE ：PyCharm
@Author ：熊家阳
@Date ：2026/7/22
@Description ：从ES获取API工具元数据，并按API名匹配本地业务技能（Skills）。

knowledge（ES chat_bi_knowledge_sit）已被本地 skills/ 目录替换，
匹配逻辑移至 core/skill_manager.py 的 SkillManager.match_skills_by_api_names。
"""
from __future__ import annotations

import logging
from typing import Any

from core.skill_manager import Skill, get_skill_manager
from utils.es_query import ESQueryError, es_query

logger = logging.getLogger(__name__)


async def get_api_for_query(query: str, source_site: str = "les_portal") -> list[dict[str, Any]]:
    """根据用户问题从ES搜索匹配的API工具。（异步版本）"""
    dsl = {
        "query": {
            "bool": {
                "must": [
                    {
                        "multi_match": {
                            "query": query,
                            "type": "cross_fields",
                            "operator": "or",
                            "analyzer": "ik_max_word",
                            "fields": ["description", "keywords", "kpi^1.2", "column^0.2", "parameters^0.2"]
                        }
                    }
                ],
                "filter": [
                    {
                        "term": {
                            "source_site": source_site
                        }
                    }
                ]
            }
        },
        "size": 5
    }
    try:
        df = await es_query("chat_bi_doc_sit", dsl)
    except ESQueryError as e:
        logger.warning("[ES] 查询失败，返回空列表: %s", e)
        return []
    df = df.fillna("")
    data = df.to_dict(orient="records")
    return data


async def fetch_es_context(query: str, source_site: str = "les_portal") -> tuple[list[dict[str, Any]], list[Skill]]:
    """根据用户问题获取匹配的API列表和关联业务技能。

    ES 查询为异步，Skills 从本地文件加载（SkillManager 缓存）。
    在 PlanAgent 规划前调用，结果存入 SessionContext 供所有 Agent 使用。

    Args:
        query: 用户原始问题
        source_site: API来源站点过滤

    Returns:
        (api_meta, skills) — API元数据列表和匹配的业务技能列表
    """
    try:
        api_meta = await get_api_for_query(query, source_site)
    except Exception as e:
        logger.warning("[ES] fetch_es_context 异常，返回空结果: %s", e)
        return [], []
    tool_names = [api["name"] for api in api_meta if api.get("name")]
    skills = get_skill_manager().match_skills_by_api_names(tool_names) if tool_names else []

    logger.info(
        "[ES] 查询 '%s' 匹配到 %d 个API, %d 个业务技能",
        query[:50], len(api_meta), len(skills),
    )
    return api_meta, skills


def format_api_list_detailed(api_meta: list[dict[str, Any]]) -> str:
    """将API元数据格式化为含必填/可选参数详情的文本，供 PlanAgent 判断是否需要澄清。

    解析 ES parameters dict 的结构化信息：
    {"param_name": {"type": "string", "description": "...", "required": True, "default": "..."}}

    输出格式：
        - lps_transBox [API]: 查询转出箱数
          必填参数: warehouse_id (string): 仓库ID
          可选参数: date (string): 日期, 默认: 本月
    """
    if not api_meta:
        return "用户问题暂无匹配的业务资源依赖。"
    lines = ["可用工具列表：\n"]
    for item in api_meta:
        name = item.get("name", "")
        description = item.get("description", "")
        tool_type = item.get("tool_type", "api")
        parameters = item.get("parameters", {})
        type_label = "API" if tool_type == "api" else "数据表"
        lines.append(f"- {name} [{type_label}]: {description}")

        if isinstance(parameters, dict) and parameters:
            required: list[str] = []
            optional: list[str] = []
            for pname, pschema in parameters.items():
                if not isinstance(pschema, dict):
                    continue
                ptype = pschema.get("type", "string")
                pdesc = pschema.get("description", pname)
                is_required = pschema.get("required", False)
                default = pschema.get("default")
                if is_required:
                    required.append(f"{pname} ({ptype}): {pdesc}")
                else:
                    suffix = f", 默认: {default}" if default else ""
                    optional.append(f"{pname} ({ptype}): {pdesc}{suffix}")
            if required:
                lines.append(f"  必填参数: {'; '.join(required)}")
            if optional:
                lines.append(f"  可选参数: {'; '.join(optional)}")
        elif parameters:
            lines.append(f"  参数: {parameters}")
    return "\n".join(lines)