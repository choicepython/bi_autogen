
"""可观测性平台工厂 — 按配置选择 TraceObserver 实现。

新增平台步骤：
1. 新建 observability/xxx_observer.py，实现 TraceObserver ABC
2. 在本文件的 _create_observer() 中添加配置分支
3. 调用方代码无需任何改动
"""

from __future__ import annotations

import logging

from observability.trace_observer import TraceObserver

logger = logging.getLogger(__name__)

_observer: TraceObserver | None = None


def get_trace_observer() -> TraceObserver:
    """获取全局 TraceObserver 单例。

    首次调用时根据配置创建实例，后续调用返回同一实例。
    未配置任何可观测性平台时，返回 LangfuseObserver（内置 no-op 降级）。
    """
    global _observer
    if _observer is None:
        _observer = _create_observer()
    return _observer


def _create_observer() -> TraceObserver:
    """根据配置创建 TraceObserver 实例。"""
    from config import settings

    # Langfuse
    if settings.langfuse_host:
        from observability.langfuse_observer import LangfuseObserver
        logger.info("[ObserverFactory] 使用 Langfuse 可观测性平台")
        return LangfuseObserver()

    # 未来扩展：
    # if settings.langtrace_host:
    #     from observability.langtrace_observer import LangtraceObserver
    #     logger.info("[ObserverFactory] 使用 Langtrace 可观测性平台")
    #     return LangtraceObserver()

    # 默认：LangfuseObserver（未配置时内置 no-op 降级）
    from observability.langfuse_observer import LangfuseObserver
    return LangfuseObserver()