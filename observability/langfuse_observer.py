
"""Langfuse 可观测性实现 — 封装 Langfuse SDK v3。

使用低级 API（start_as_current_span / start_as_current_generation）精确控制 span 生命周期，
不用 @observe() 装饰器，匹配现有事件驱动架构。

优雅降级：未配置 langfuse_host 或 langfuse 包未安装时，所有方法为 no-op。
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from config import settings
from observability.trace_observer import TraceObserver

logger = logging.getLogger(__name__)


class LangfuseObserver(TraceObserver):
    """Langfuse 可观测性实现，封装 Langfuse SDK v3。

    优雅降级：未配置 langfuse_host 时，所有方法为 no-op。
    """

    _client: Any | None = None  # langfuse.Langfuse 实例，懒加载
    _enabled: bool = False
    _init_attempted: bool = False

    def __init__(self) -> None:
        self._enabled = bool(settings.langfuse_host)

    def _ensure_client(self) -> Any | None:
        """懒加载 Langfuse 客户端。仅在首次调用时初始化。"""
        if not self._enabled:
            return None
        if self._init_attempted:
            return self._client
        self._init_attempted = True
        try:
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=settings.langfuse_public_key,
                secret_key=settings.langfuse_secret_key.get_secret_value(),
                base_url=settings.langfuse_host,
            )
            logger.info("[LangfuseObserver] Langfuse 已连接: %s", settings.langfuse_host)
        except ImportError:
            logger.warning("[LangfuseObserver] langfuse 包未安装，降级为 no-op")
            self._enabled = False
        except Exception as e:
            logger.warning("[LangfuseObserver] Langfuse 初始化失败，降级为 no-op: %s", e)
            self._enabled = False
        return self._client

    @contextmanager
    def start_trace(
        self,
        session_id: str,
        query: str,
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """开始一个 Langfuse trace（顶层 span）。"""
        client = self._ensure_client()
        if client is None:
            yield None
            return
        update_trace_params: dict[str, Any] = {"session_id": session_id}
        if user_id:
            update_trace_params["user_id"] = user_id
        if metadata:
            update_trace_params["metadata"] = metadata
        with client.start_as_current_span(
            name=query[:100],
            update_trace_params=update_trace_params,
        ) as span:
            yield span

    @contextmanager
    def start_span(
        self,
        name: str,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """开始一个子 span。"""
        client = self._ensure_client()
        if client is None:
            yield None
            return
        with client.start_as_current_span(name=name) as span:
            if metadata:
                span.update(metadata=metadata)
            yield span

    @contextmanager
    def start_generation(
        self,
        name: str,
        model: str = "",
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """开始一个 LLM generation span。"""
        client = self._ensure_client()
        if client is None:
            yield None
            return
        kwargs: dict[str, Any] = {"name": name}
        if model:
            kwargs["model"] = model
        if input_data is not None:
            kwargs["input"] = input_data
        with client.start_as_current_generation(**kwargs) as gen:
            if metadata:
                gen.update(metadata=metadata)
            yield gen

    def end_generation(
        self,
        gen: Any | None,
        output: Any = None,
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """结束 LLM generation，写入输出和 usage。"""
        if gen is None:
            return
        try:
            update_kwargs: dict[str, Any] = {}
            if output is not None:
                update_kwargs["output"] = output
            if usage:
                update_kwargs["usage"] = usage
            if metadata:
                update_kwargs["metadata"] = metadata
            if update_kwargs:
                gen.update(**update_kwargs)
        except Exception as e:
            logger.debug("[LangfuseObserver] end_generation 失败: %s", e)

    def update_span(
        self,
        span: Any | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新 span 元数据（封装 Langfuse 的 span.update API）。"""
        if span is None or metadata is None:
            return
        try:
            span.update(metadata=metadata)
        except Exception as e:
            logger.debug("[LangfuseObserver] update_span 失败: %s", e)

    def update_trace(
        self,
        session_id: str = "",
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新当前 trace 的元数据。"""
        client = self._ensure_client()
        if client is None:
            return
        try:
            kwargs: dict[str, Any] = {}
            if session_id:
                kwargs["session_id"] = session_id
            if user_id:
                kwargs["user_id"] = user_id
            if metadata:
                kwargs["metadata"] = metadata
            if kwargs:
                client.update_current_trace(**kwargs)
        except Exception as e:
            logger.debug("[LangfuseObserver] update_trace 失败: %s", e)

    def flush(self) -> None:
        """刷新待发送的 trace 数据到 Langfuse。"""
        if self._client is not None:
            try:
                self._client.flush()
            except Exception as e:
                logger.debug("[LangfuseObserver] flush 失败: %s", e)