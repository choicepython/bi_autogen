
"""循环检测器：检测 LLM 反复调用同一工具（同名同参）。

当 LLM 陷入循环（如连续3次调用 api_query 相同参数），强制终止 ReAct 循环。
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections import deque
from typing import Any

logger = logging.getLogger(__name__)


class LoopDetector:
    """检测连续相同工具调用。

    维护最近 N 次调用的 (tool_name, args_hash) 历史，
    连续 max_consecutive 次完全相同则判定为循环。
    """

    def __init__(self, max_consecutive: int = 3, history_size: int = 10) -> None:
        self._max_consecutive = max_consecutive
        self._history: deque[tuple[str, str]] = deque(maxlen=history_size)
        self._total_calls = 0

    @property
    def total_calls(self) -> int:
        return self._total_calls

    @staticmethod
    def _hash_args(args: Any) -> str:
        """对工具参数做稳定 hash（排序后 JSON 序列化）。"""
        try:
            if isinstance(args, str):
                # LLM 可能传 JSON string，尝试解析后排序
                try:
                    parsed = json.loads(args)
                    args = parsed
                except json.JSONDecodeError:
                    return hashlib.md5(args.encode()).hexdigest()
            return hashlib.md5(
                json.dumps(args, sort_keys=True, ensure_ascii=False, default=str).encode()
            ).hexdigest()
        except (TypeError, ValueError) as e:
            logger.debug("[LoopDetector] 参数 hash 失败: %s", e)
            return "unhashable"

    def record(self, tool_name: str, args: Any) -> bool:
        """记录一次工具调用，返回 True 表示检测到循环。

        Args:
            tool_name: 工具名
            args: 工具参数（dict 或 JSON string）

        Returns:
            True = 检测到循环，应终止；False = 正常
        """
        self._total_calls += 1
        args_hash = self._hash_args(args)
        entry = (tool_name, args_hash)
        self._history.append(entry)

        if len(self._history) < self._max_consecutive:
            return False

        # 检查最近 max_consecutive 次是否完全相同
        recent = list(self._history)[-self._max_consecutive:]
        first = recent[0]
        if all(entry == first for entry in recent[1:]):
            logger.warning(
                "[LoopDetector] 检测到循环：连续 %d 次调用 %s(相同参数)",
                self._max_consecutive, tool_name,
            )
            return True

        return False

    def reset(self) -> None:
        """重置历史（新会话时调用）。"""
        self._history.clear()
        self._total_calls = 0