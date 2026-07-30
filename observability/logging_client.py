
from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

from autogen_core._cancellation_token import CancellationToken
from autogen_core.models import (
    ChatCompletionClient,
    CreateResult,
    LLMMessage,
    RequestUsage,
)
from autogen_core.models._types import (
    AssistantMessage,
    FunctionExecutionResultMessage,
    SystemMessage,
    UserMessage,
)
from autogen_core.tools import Tool, ToolSchema

from utils.json_utils import sanitize_for_json

logger = logging.getLogger("bi_autogen.llm_trace")

# ---------------------------------------------------------------------------
# Qwen3 兼容：将 API 响应中的 `reasoning` 字段映射到 `reasoning_content`
# ---------------------------------------------------------------------------


def _patch_qwen3_reasoning() -> None:
    """Patch OpenAI SDK 使其将 Qwen3 的 `reasoning` 字段映射到 `reasoning_content`。

    Qwen3 通过 vLLM 部署时，思考内容放在 `reasoning` 字段中，
    而 AutoGen 只检查 `reasoning_content`。此 patch 在 OpenAI SDK
    构建响应对象后，将 `reasoning` 复制到 `model_extra["reasoning_content"]`。

    Patch 点：openai._models.ConstructType.__init__，这是所有 Pydantic v1
    兼容模型的构造入口，OpenAI SDK 内部统一走此路径。
    """
    try:
        from openai._models import construct_type

        _orig_construct = construct_type

        def _patched_construct(*args: Any, **kwargs: Any) -> Any:
            result = _orig_construct(*args, **kwargs)
            _inject_reasoning_to_extra(result)
            return result

        import openai._models
        openai._models.construct_type = _patched_construct

        logger.debug("[LoggingClient] 已 patch openai._models.construct_type 支持 Qwen3 reasoning")
    except Exception as e:
        logger.debug("[LoggingClient] Qwen3 reasoning patch 失败: %s", e)


def _inject_reasoning_to_extra(obj: Any) -> None:
    """递归检查对象，将 reasoning 属性复制到 model_extra["reasoning_content"]。"""
    if obj is None:
        return
    # ChatCompletionMessage 或 ChatCompletionChunkDelta
    if hasattr(obj, "reasoning") and hasattr(obj, "model_extra"):
        reasoning = getattr(obj, "reasoning", None)
        if reasoning and obj.model_extra is not None:
            if "reasoning_content" not in obj.model_extra:
                obj.model_extra["reasoning_content"] = reasoning
    # 递归处理 choices
    if hasattr(obj, "choices") and obj.choices is not None:
        for choice in obj.choices:
            if hasattr(choice, "message") and choice.message is not None:
                _inject_reasoning_to_extra(choice.message)
            if hasattr(choice, "delta") and choice.delta is not None:
                _inject_reasoning_to_extra(choice.delta)


_patch_qwen3_reasoning()

_LLM_LOG_DIR = Path(__file__).parent.parent / "log" / "llm"

# 使用 ContextVar 替代模块全局变量，支持并发隔离
_session_id_var: ContextVar[str] = ContextVar("session_id", default="")
_chat_id_var: ContextVar[str] = ContextVar("chat_id", default="")
_call_index_var: ContextVar[int] = ContextVar("call_index", default=0)
_agent_name_var: ContextVar[str] = ContextVar("agent_name", default="")
_enable_thinking_var: ContextVar[bool | None] = ContextVar("enable_thinking", default=None)


def set_session_id(session_id: str) -> None:
    """设置当前任务的session_id，并重置调用计数。由DispatchLayer.run()调用。"""
    _session_id_var.set(session_id)
    _call_index_var.set(0)
    logger.info("[Session] 开始新会话: %s", session_id)


def set_chat_id(chat_id: str) -> None:
    """设置当前对话轮次的chat_id。"""
    _chat_id_var.set(chat_id)


def set_current_agent(agent_name: str) -> None:
    """设置当前正在执行的Agent名称，供LLM日志记录使用。

    由 BIBaseAgent.on_messages_stream 在每轮开始时调用，
    解决首轮LLM调用时 messages 中无 AssistantMessage 导致 agent="?" 的问题。
    """
    _agent_name_var.set(agent_name)


def set_enable_thinking(enable: bool | None) -> None:
    """设置当前请求的思考模式偏好。

    None=使用配置默认值, True=强制开启, False=强制关闭。
    由 DispatchLayer.run_stream 在每个请求开始时调用。
    """
    _enable_thinking_var.set(enable)


def _next_call_index() -> int:
    idx = _call_index_var.get() + 1
    _call_index_var.set(idx)
    return idx


def reset_call_index() -> None:
    """重置调用计数。由 SessionManager.cleanup() 调用。"""
    _call_index_var.set(0)


def _resolve_agent_name(messages: Sequence[LLMMessage]) -> str:
    """解析当前LLM调用所属的Agent名称。

    优先级：
    1. ContextVar 中显式设置的 agent_name（由 BIBaseAgent 设置）
    2. messages 中最后一条 AssistantMessage 的 source
    3. 兜底返回 "?"
    """
    # 优先使用 ContextVar
    ctx_name = _agent_name_var.get()
    if ctx_name:
        return ctx_name

    # 回退：从消息历史中推断
    for msg in reversed(messages):
        if isinstance(msg, AssistantMessage):
            return msg.source

    return "?"


def _serialize_messages(messages: Sequence[LLMMessage]) -> list[dict[str, Any]]:
    """将LLM消息列表转为可读的dict列表。"""
    result: list[dict[str, Any]] = []
    for msg in messages:
        if isinstance(msg, SystemMessage):
            result.append({"role": "system", "content": msg.content})
        elif isinstance(msg, UserMessage):
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            result.append({"role": "user", "source": msg.source, "content": content})
        elif isinstance(msg, AssistantMessage):
            entry: dict[str, Any] = {"role": "assistant", "source": msg.source}
            if isinstance(msg.content, str):
                entry["content"] = msg.content
            elif isinstance(msg.content, list):
                entry["tool_calls"] = [
                    {"name": tc.name, "arguments": tc.arguments} for tc in msg.content
                ]
            if msg.thought:
                entry["thought"] = msg.thought
            result.append(entry)
        elif isinstance(msg, FunctionExecutionResultMessage):
            for r in msg.content:
                result.append({
                    "role": "tool_result",
                    "call_id": r.call_id,
                    "name": getattr(r, "name", ""),
                    "content": r.content[:3000] if len(r.content) > 3000 else r.content,
                    "is_error": getattr(r, "is_error", False),
                })
    return result


def _serialize_tools(tools: Sequence[Tool | ToolSchema]) -> list[dict[str, Any]]:
    """将工具定义转为简要dict列表。"""
    result: list[dict[str, Any]] = []
    for t in tools:
        if hasattr(t, "schema"):
            s = t.schema  # type: ignore[union-attr]
            result.append({
                "name": s.get("name", getattr(t, "name", "?")),
                "description": s.get("description", "")[:100],
            })
        elif isinstance(t, dict):
            if "function" in t:
                func = t.get("function", {})
                result.append({"name": func.get("name", "?"), "description": func.get("description", "")[:100]})
            else:
                result.append({"name": t.get("name", "?"), "description": t.get("description", "")[:100]})
        elif hasattr(t, "name"):
            result.append({"name": getattr(t, "name", "?"), "description": ""})
        else:
            result.append({"raw": str(t)[:200]})
    return result


def _build_request_body_snapshot(inner: ChatCompletionClient) -> dict[str, Any] | None:
    """从内部 OpenAIChatCompletionClient 的 _create_args 构造请求体快照。

    由于 OpenAI SDK 可能不使用我们传入的 httpx client，
    httpx hook 无法可靠捕获请求体，因此直接从 _create_args 重建。
    """
    try:
        create_args = getattr(inner, "_create_args", None)
        if create_args is None:
            return None
        # 只保留关键参数，排除大对象
        snapshot: dict[str, Any] = {}
        for key in ("model", "temperature", "top_p", "max_tokens", "stream",
                     "tool_choice", "response_format", "extra_body", "seed",
                     "presence_penalty", "frequency_penalty", "stop"):
            if key in create_args:
                snapshot[key] = create_args[key]
        return snapshot
    except Exception:
        return None


def _write_llm_log(data: dict[str, Any]) -> None:
    """实时追加写入LLM日志JSONL文件。

    使用同步 I/O 但在独立线程中执行，避免阻塞事件循环。
    对于高频 LLM 调用场景，同步追加写入比 aiofiles 更高效
    （避免每次 await 的开销），且 JSONL 行级写入是原子操作。
    """
    try:
        _LLM_LOG_DIR.mkdir(parents=True, exist_ok=True)
        today = datetime.now().strftime("%Y-%m-%d")
        filepath = _LLM_LOG_DIR / f"llm_{today}.jsonl"
        with open(filepath, "a", encoding="utf-8") as f:
            f.write(json.dumps(sanitize_for_json(data), ensure_ascii=False) + "\n")
    except Exception as e:
        logger.debug("[LoggingClient] LLM日志写入失败: %s", e)


class LoggingChatCompletionClient(ChatCompletionClient):
    """包装ChatCompletionClient，每次LLM调用实时写入统一的JSONL日志文件。"""

    def __init__(self, inner: ChatCompletionClient) -> None:
        self._inner = inner

    def _build_extra_create_args(
        self, extra_create_args: Mapping[str, Any],
    ) -> dict[str, Any]:
        """根据请求级 enable_thinking 偏好，构造覆盖 extra_body 的参数。

        如果 _enable_thinking_var 为 None，不做覆盖（使用模型默认配置）。
        否则，强制设置 extra_body.chat_template_kwargs.enable_thinking。
        """
        thinking_override = _enable_thinking_var.get()
        if thinking_override is None:
            return dict(extra_create_args)

        # 合并 extra_body：保留已有字段，覆盖/添加 enable_thinking
        merged = dict(extra_create_args)
        existing_extra_body = dict(merged.get("extra_body", {}) or {})
        chat_kwargs = dict(existing_extra_body.get("chat_template_kwargs", {}) or {})
        chat_kwargs["enable_thinking"] = thinking_override
        existing_extra_body["chat_template_kwargs"] = chat_kwargs
        merged["extra_body"] = existing_extra_body
        return merged

    async def create(
            self,
            messages: Sequence[LLMMessage],
            *,
            tools: Sequence[Tool | ToolSchema] = [],
            tool_choice: Tool | str = "auto",
            json_output: bool | type | None = None,
            extra_create_args: Mapping[str, Any] = {},
            cancellation_token: CancellationToken | None = None,
    ) -> CreateResult:
        call_index = _next_call_index()
        agent_name = _resolve_agent_name(messages)

        # 序列化请求
        req_data: dict[str, Any] = {
            "session_id": _session_id_var.get(),
            "chat_id": _chat_id_var.get(),
            "call_index": call_index,
            "agent": agent_name,
            "timestamp": datetime.now().isoformat(),
            "direction": "request",
            "message_count": len(messages),
            "tool_count": len(tools),
            "messages": _serialize_messages(messages),
            "tools": _serialize_tools(tools) if tools else [],
        }

        # 从内部 client 构造请求体快照（含 enable_thinking 等参数）
        req_body_snapshot = _build_request_body_snapshot(self._inner)
        if req_body_snapshot is not None:
            req_data["request_body"] = req_body_snapshot

        logger.info(
            "[%s] #%d LLM请求 → %d条消息, %d个工具",
            agent_name, call_index, len(messages), len(tools),
        )

        # 请求日志在调用前写入，确保异常时请求不丢失
        _write_llm_log(req_data)

        # DB: 异步写入LLM请求记录
        _tool_names = [s.get("name", "") for s in (_serialize_tools(tools) if tools else []) if s.get("name")]
        try:
            from db.writer import db_writer, make_llm_call_data
            await db_writer.enqueue_llm_call(make_llm_call_data(
                session_id=_session_id_var.get(),
                chat_id=_chat_id_var.get(),
                agent_name=agent_name,
                call_index=call_index,
                direction="request",
                message_count=len(messages),
                tool_count=len(tools),
                tool_names=_tool_names,
                model_name=getattr(self._inner, "model_info", {}).get("model", ""),
            ))
        except Exception as db_err:
            logger.debug("[LoggingClient] DB写入LLM请求失败: %s", db_err)

        request_time = datetime.now()
        try:
            from config.settings import settings
            result = await asyncio.wait_for(
                self._inner.create(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    json_output=json_output,
                    extra_create_args=self._build_extra_create_args(extra_create_args),
                    cancellation_token=cancellation_token,
                ),
                timeout=settings.llm_request_timeout,
            )
        except Exception as e:
            # 记录调用异常（含超时）
            error_type = "TimeoutError" if isinstance(e, asyncio.TimeoutError) else type(e).__name__
            _write_llm_log({
                "session_id": _session_id_var.get(),
                "chat_id": _chat_id_var.get(),
                "call_index": call_index,
                "agent": agent_name,
                "timestamp": datetime.now().isoformat(),
                "direction": "error",
                "error_type": error_type,
                "error_message": str(e)[:2000],
            })
            raise

        # 序列化响应
        resp_data: dict[str, Any] = {
            "session_id": _session_id_var.get(),
            "chat_id": _chat_id_var.get(),
            "call_index": call_index,
            "agent": agent_name,
            "timestamp": datetime.now().isoformat(),
            "direction": "response",
            "finish_reason": result.finish_reason,
            "usage": {
                "prompt_tokens": result.usage.prompt_tokens,
                "completion_tokens": result.usage.completion_tokens,
            },
            "cached": result.cached,
        }
        if isinstance(result.content, str):
            resp_data["content"] = result.content
        elif isinstance(result.content, list):
            resp_data["tool_calls"] = [
                {"name": tc.name, "arguments": tc.arguments} for tc in result.content
            ]
        if result.thought:
            resp_data["thought"] = result.thought

        logger.info(
            "[%s] #%d LLM响应 ← finish_reason=%s, tokens=%d+%d",
            agent_name, call_index,
            result.finish_reason,
            result.usage.prompt_tokens,
            result.usage.completion_tokens,
        )

        _write_llm_log(resp_data)

        # DB: 异步写入LLM调用记录（response）
        try:
            from db.writer import db_writer, make_llm_call_data
            duration = int((datetime.now() - request_time).total_seconds() * 1000)
            await db_writer.enqueue_llm_call(make_llm_call_data(
                session_id=_session_id_var.get(),
                chat_id=_chat_id_var.get(),
                agent_name=agent_name,
                call_index=call_index,
                direction="response",
                message_count=len(messages),
                tool_count=len(tools),
                tool_names=_tool_names,
                model_name=getattr(self._inner, "model_info", {}).get("model", ""),
                finish_reason=result.finish_reason,
                prompt_tokens=result.usage.prompt_tokens,
                completion_tokens=result.usage.completion_tokens,
                total_tokens=result.usage.prompt_tokens + result.usage.completion_tokens,
                cached=result.cached,
                thought=result.thought[:2000] if result.thought else "",
                duration_ms=duration,
            ))
        except Exception as db_err:
            logger.debug("[LoggingClient] DB写入LLM调用失败: %s", db_err)

        return result

    async def create_stream(
            self,
            messages: Sequence[LLMMessage],
            *,
            tools: Sequence[Tool | ToolSchema] = [],
            tool_choice: Tool | str = "auto",
            json_output: bool | type | None = None,
            extra_create_args: Mapping[str, Any] = {},
            cancellation_token: CancellationToken | None = None,
    ):
        call_index = _next_call_index()
        agent_name = _resolve_agent_name(messages)

        req_data: dict[str, Any] = {
            "session_id": _session_id_var.get(),
            "chat_id": _chat_id_var.get(),
            "call_index": call_index,
            "agent": agent_name,
            "timestamp": datetime.now().isoformat(),
            "direction": "request",
            "stream": True,
            "message_count": len(messages),
            "tool_count": len(tools),
            "messages": _serialize_messages(messages),
            "tools": _serialize_tools(tools) if tools else [],
            "tool_choice": str(tool_choice),
            "json_output": str(json_output),
        }

        # 从内部 client 构造请求体快照（含 enable_thinking 等参数）
        req_body_snapshot = _build_request_body_snapshot(self._inner)
        if req_body_snapshot is not None:
            req_data["request_body"] = req_body_snapshot

        logger.info(
            "[%s] #%d LLM Stream请求 → %d条消息, %d个工具",
            agent_name, call_index, len(messages), len(tools),
        )
        _write_llm_log(req_data)

        # DB: 异步写入LLM Stream请求记录
        _stream_tool_names = [s.get("name", "") for s in (_serialize_tools(tools) if tools else []) if s.get("name")]
        try:
            from db.writer import db_writer, make_llm_call_data
            await db_writer.enqueue_llm_call(make_llm_call_data(
                session_id=_session_id_var.get(),
                chat_id=_chat_id_var.get(),
                agent_name=agent_name,
                call_index=call_index,
                direction="request",
                is_stream=True,
                message_count=len(messages),
                tool_count=len(tools),
                tool_names=_stream_tool_names,
                model_name=getattr(self._inner, "model_info", {}).get("model", ""),
            ))
        except Exception as db_err:
            logger.debug("[LoggingClient] DB写入LLM Stream请求失败: %s", db_err)

        request_time = datetime.now()
        chunk_count = 0
        try:
            inner_stream = self._inner.create_stream(
                    messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    json_output=json_output,
                    extra_create_args=self._build_extra_create_args(extra_create_args),
                    cancellation_token=cancellation_token,
            )
        except Exception as e:
            error_type = "TimeoutError" if isinstance(e, asyncio.TimeoutError) else type(e).__name__
            _write_llm_log({
                "session_id": _session_id_var.get(),
                "chat_id": _chat_id_var.get(),
                "call_index": call_index,
                "agent": agent_name,
                "timestamp": datetime.now().isoformat(),
                "direction": "error",
                "stream": True,
                "error_type": error_type,
                "error_message": str(e)[:2000],
            })
            raise

        async for chunk in inner_stream:
            if isinstance(chunk, str):
                chunk_count += 1
            elif isinstance(chunk, CreateResult):
                resp_data: dict[str, Any] = {
                    "session_id": _session_id_var.get(),
                    "chat_id": _chat_id_var.get(),
                    "call_index": call_index,
                    "agent": agent_name,
                    "timestamp": datetime.now().isoformat(),
                    "direction": "response",
                    "stream": True,
                    "chunk_count": chunk_count,
                    "finish_reason": chunk.finish_reason,
                    "usage": {
                        "prompt_tokens": chunk.usage.prompt_tokens,
                        "completion_tokens": chunk.usage.completion_tokens,
                    },
                }
                if isinstance(chunk.content, str):
                    resp_data["content"] = chunk.content
                elif isinstance(chunk.content, list):
                    resp_data["tool_calls"] = [
                        {"name": tc.name, "arguments": tc.arguments} for tc in chunk.content
                    ]
                if chunk.thought:
                    resp_data["thought"] = chunk.thought
                logger.info(
                    "[%s] #%d LLM Stream响应 ← %d chunks, finish_reason=%s",
                    agent_name, call_index, chunk_count, chunk.finish_reason,
                )
                _write_llm_log(resp_data)

                # DB: 异步写入LLM Stream调用记录（response）
                try:
                    from db.writer import db_writer, make_llm_call_data
                    duration = int((datetime.now() - request_time).total_seconds() * 1000)
                    await db_writer.enqueue_llm_call(make_llm_call_data(
                        session_id=_session_id_var.get(),
                        chat_id=_chat_id_var.get(),
                        agent_name=agent_name,
                        call_index=call_index,
                        direction="response",
                        is_stream=True,
                        message_count=len(messages),
                        tool_count=len(tools),
                        tool_names=_stream_tool_names,
                        model_name=getattr(self._inner, "model_info", {}).get("model", ""),
                        finish_reason=chunk.finish_reason,
                        prompt_tokens=chunk.usage.prompt_tokens,
                        completion_tokens=chunk.usage.completion_tokens,
                        total_tokens=chunk.usage.prompt_tokens + chunk.usage.completion_tokens,
                        cached=chunk.cached,
                        chunk_count=chunk_count,
                        thought=chunk.thought[:2000] if chunk.thought else "",
                        duration_ms=duration,
                    ))
                except Exception as db_err:
                    logger.debug("[LoggingClient] DB写入LLM Stream调用失败: %s", db_err)

            yield chunk

    @property
    def model_info(self) -> dict[str, Any]:
        return self._inner.model_info

    @property
    def capabilities(self) -> dict[str, Any]:
        return self._inner.capabilities

    @property
    def actual_usage(self) -> RequestUsage:
        return self._inner.actual_usage

    @property
    def total_usage(self) -> RequestUsage:
        return self._inner.total_usage

    @property
    def remaining_tokens(self) -> int:
        return self._inner.remaining_tokens

    async def count_tokens(self, messages: Sequence[LLMMessage], *, tools: Sequence[Tool | ToolSchema] = []) -> int:
        return await self._inner.count_tokens(messages, tools=tools)

    async def close(self) -> None:
        await self._inner.close()

    def __repr__(self) -> str:
        return f"LoggingChatCompletionClient({self._inner!r})"