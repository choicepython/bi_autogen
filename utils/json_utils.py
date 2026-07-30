
"""JSON 序列化辅助工具。"""

from __future__ import annotations

from typing import Any


def sanitize_for_json(obj: Any) -> Any:
    """递归清理不可 JSON 序列化的对象，替换为字符串表示。"""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)