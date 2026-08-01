
"""可观测性平台抽象层 — 定义统一的 trace/span/generation 接口。

不同可观测性平台（Langfuse、Langtrace、Jaeger 等）实现此 ABC，
调用方通过 observer_factory.get_trace_observer() 获取实例，
不直接引用具体平台类，实现切换平台时零改动调用方代码。

新增平台步骤：
1. 新建 observability/xxx_observer.py，实现 TraceObserver ABC
2. 在 observer_factory.py 的 _create_observer() 中添加配置分支
3. 调用方代码无需任何改动
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

logger = logging.getLogger(__name__)


class TraceObserver(ABC):
    """可观测性平台抽象基类。

    所有方法均为 context manager 或普通方法，调用方通过 with 语句管理 span 生命周期。
    未启用的实现应返回 yield None 的 no-op context manager。
    """

    @contextmanager
    @abstractmethod
    def start_trace(
        self,
        session_id: str,
        query: str,
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """开始一个顶层 trace span。

        Args:
            session_id: 会话 ID。
            query: 用户查询（作为 trace 名称）。
            user_id: 用户 ID。
            metadata: 附加元数据。

        Yields:
            平台特定的 span 对象，未启用时为 None。
        """
        yield None

    @contextmanager
    @abstractmethod
    def start_span(
        self,
        name: str,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """开始一个子 span。

        Args:
            name: span 名称。
            metadata: 附加元数据。

        Yields:
            平台特定的 span 对象，未启用时为 None。
        """
        yield None

    @contextmanager
    @abstractmethod
    def start_generation(
        self,
        name: str,
        model: str = "",
        input_data: Any = None,
        metadata: dict[str, Any] | None = None,
    ) -> Iterator[Any]:
        """开始一个 LLM generation span。

        Args:
            name: generation 名称。
            model: 模型名称。
            input_data: 输入消息（序列化后的 list[dict]）。
            metadata: 附加元数据。

        Yields:
            平台特定的 generation 对象，未启用时为 None。
        """
        yield None

    @abstractmethod
    def end_generation(
        self,
        gen: Any | None,
        output: Any = None,
        usage: dict[str, int] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """结束 LLM generation，写入输出和 usage。

        Args:
            gen: start_generation 返回的 generation 对象。
            output: LLM 输出（文本或 tool_calls）。
            usage: token 用量 {"input": N, "output": N, "total": N}。
            metadata: 附加元数据（finish_reason 等）。
        """

    @abstractmethod
    def update_span(
        self,
        span: Any | None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新 span 元数据（封装平台特有 API，调用方不直接接触 span 对象）。

        Args:
            span: start_span / start_trace 返回的 span 对象。
            metadata: 要更新的元数据。
        """

    @abstractmethod
    def update_trace(
        self,
        session_id: str = "",
        user_id: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新当前 trace 的元数据。

        Args:
            session_id: 会话 ID。
            user_id: 用户 ID。
            metadata: 附加元数据。
        """

    @abstractmethod
    def flush(self) -> None:
        """刷新待发送的 trace 数据到平台。在应用关停时调用。"""