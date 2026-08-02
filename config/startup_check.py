
"""启动配置校验 — 检查必需/可选依赖配置，打印降级状态摘要。

在 main.py 和 gateway/app.py 启动时调用，让用户一目了然知道哪些依赖已配置、哪些降级。

注：ensure_index_exists 采用 lazy import（函数内导入），避免 startup_check 早期执行时
触发 utils.es_query → core.data_context 的依赖链加载。
"""

from __future__ import annotations

import logging
import sys

from config import settings

logger = logging.getLogger(__name__)


def check_startup_config() -> bool:
    """校验启动配置，打印降级状态摘要。

    Returns:
        True 表示必需配置完整可以启动，False 表示缺少必需配置。
    """
    lines: list[str] = ["", "=" * 60, "BI Agent 启动配置检查", "=" * 60]

    # ---- 必需配置：LLM API ----
    all_ok = True
    llm_ok = bool(settings.primary_model and settings.primary_base_url and settings.primary_api_key.get_secret_value())
    if llm_ok:
        lines.append(f"  [OK]   LLM 主模型: {settings.primary_model}")
    else:
        lines.append("  [FAIL] LLM 主模型: 未配置（BI_PRIMARY_MODEL / BI_PRIMARY_BASE_URL / BI_PRIMARY_API_KEY）")
        all_ok = False

    # 路由层 LLM（selector）— 可复用主模型
    selector_ok = bool(settings.selector_model and settings.selector_base_url and settings.selector_api_key.get_secret_value())
    if selector_ok:
        lines.append(f"  [OK]   LLM 路由模型: {settings.selector_model}")
    else:
        lines.append("  [WARN] LLM 路由模型: 未配置，将复用主模型（路由层 LLM 分类不可用，降级为规则路由）")

    # ---- 可选依赖：逐个检查降级状态 ----
    lines.append("-" * 60)

    # PostgreSQL
    if settings.db_host and settings.db_name and settings.db_user:
        lines.append(f"  [OK]   PostgreSQL: {settings.db_host}:{settings.db_port}/{settings.db_name}")
    else:
        lines.append("  [SKIP] PostgreSQL: 未配置 -> 降级为跳过写入（执行记录不持久化，溢出到 logs/db_spill/）")

    # Elasticsearch
    if settings.es_hosts:
        lines.append(f"  [OK]   Elasticsearch: {settings.es_hosts}")
        # 启动时确保资源索引存在，不存在则按 schema 创建（幂等，失败不阻塞）
        from utils.es_query import ensure_index_exists

        index = settings.es_resource_index
        if ensure_index_exists(index):
            lines.append(f"  [OK]   ES 资源索引: {index}（已就绪）")
        else:
            lines.append(f"  [WARN] ES 资源索引: {index} 创建失败，资源召回将返回空（详见日志）")
    else:
        lines.append("  [SKIP] Elasticsearch: 未配置 -> 降级为本地资源搜索（读取 source/*.jsonl）")

    # Redis
    if settings.redis_url:
        lines.append(f"  [OK]   Redis: {settings.redis_url}")
    else:
        lines.append("  [SKIP] Redis: 未配置 -> 降级为内存存储（会话历史不跨进程）")

    # Langfuse
    if settings.langfuse_host:
        lines.append(f"  [OK]   Langfuse: {settings.langfuse_host}")
    else:
        lines.append("  [SKIP] Langfuse: 未配置 -> 降级为 no-op（trace 仅写本地 JSONL）")

    # 搜索工具
    search_urls = [u for u in (settings.search_knowledge_url, settings.search_w3_url, settings.web_search_url) if u]
    if search_urls:
        lines.append(f"  [OK]   搜索工具: {len(search_urls)} 个已配置")
    else:
        lines.append("  [SKIP] 搜索工具: 未配置 -> RAGAgent 搜索功能不可用")

    # Elasticsearch
    if settings.es_hosts:
        lines.append(f"  [OK]   Elasticsearch: {settings.es_hosts}")
        # 启动时确保资源索引存在，不存在则按 schema 创建（幂等，失败不阻塞）
        from utils.es_query import ensure_index_exists

        index = settings.es_resource_index
        if ensure_index_exists(index):
            lines.append(f"  [OK]   ES 资源索引: {index}（已就绪）")
        else:
            lines.append(f"  [WARN] ES 资源索引: {index} 创建失败，资源召回将返回空（详见日志）")
    else:
        lines.append("  [SKIP] Elasticsearch: 未配置 -> 降级为本地资源搜索（读取 source/*.jsonl）")

    lines.append("=" * 60)

    if all_ok:
        lines.append("  必需配置完整，系统就绪。")
    else:
        lines.append("  必需配置缺失！请配置 LLM API 后重试。")
        lines.append("  最小配置示例 (.env):")
        lines.append('    BI_PRIMARY_MODEL=gpt-4o')
        lines.append('    BI_PRIMARY_BASE_URL=https://api.openai.com/v1')
        lines.append('    BI_PRIMARY_API_KEY=sk-xxx')
        lines.append('    BI_SELECTOR_MODEL=gpt-4o-mini')
        lines.append('    BI_SELECTOR_BASE_URL=https://api.openai.com/v1')
        lines.append('    BI_SELECTOR_API_KEY=sk-xxx')
    lines.append("=" * 60)

    # 输出到日志（INFO 级别，确保用户可见）
    summary = "\n".join(lines)
    if all_ok:
        logger.info(summary)
    else:
        logger.error(summary)
        print(summary, file=sys.stderr)

    return all_ok
