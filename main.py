
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

from core.team import BITeam
from models.chat_request import ChatRequest
from models.stream_event import StreamEvent, StreamEventType

_LOG_DIR = Path(__file__).parent / "log"


class _UnicodeLogFilter(logging.Filter):
    """Decode \\uXXXX escapes in log messages so Chinese characters display properly."""

    _UNICODE_ESCAPE_RE = re.compile(r"\\u([0-9a-fA-F]{4})")

    def filter(self, record: logging.LogRecord) -> bool:
        # autogen_core Event 对象：先转字符串再解码
        if not isinstance(record.msg, str) and hasattr(record.msg, "__str__"):
            record.msg = self._decode_unicode(str(record.msg))
        elif isinstance(record.msg, str):
            record.msg = self._decode_unicode(record.msg)
        if record.args:
            record.args = tuple(
                self._decode_unicode(a) if isinstance(a, str) else a for a in record.args
            )
        return True

    @classmethod
    def _decode_unicode(cls, text: str) -> str:
        try:
            data = json.loads(text)
            return json.dumps(data, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return cls._UNICODE_ESCAPE_RE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _make_file_handler(filename: str) -> TimedRotatingFileHandler:
    """创建按日轮转的文件handler。"""
    handler = TimedRotatingFileHandler(
        filename=_LOG_DIR / filename,
        when="midnight",
        interval=1,
        backupCount=30,
        encoding="utf-8",
    )
    handler.suffix = "%Y-%m-%d.log"
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s"))
    handler.addFilter(_UnicodeLogFilter())
    return handler


def _setup_logging() -> None:
    _LOG_DIR.mkdir(exist_ok=True)

    unicode_filter = _UnicodeLogFilter()
    log_format = "%(asctime)s [%(levelname)s] %(name)s:%(lineno)d: %(message)s"

    # Console handler — 所有日志
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(logging.Formatter(log_format))
    console_handler.addFilter(unicode_filter)

    # 通用日志文件 — 应用运行日志
    app_file = _make_file_handler("app.log")

    # LLM交互摘要日志文件 — 每次调用的简要信息（详细请求/响应在 log/llm/ JSONL文件中）
    llm_file = _make_file_handler("llm.log")

    # Agent trace日志文件 — agent动作轨迹
    trace_file = _make_file_handler("trace.log")

    # 根logger — console + app.log
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(console_handler)
    root.addHandler(app_file)

    # bi_autogen.llm_trace → llm.log（摘要，不传播到root避免重复）
    llm_logger = logging.getLogger("bi_autogen.llm_trace")
    llm_logger.propagate = False
    llm_logger.addHandler(llm_file)
    llm_logger.addHandler(console_handler)

    # bi_autogen.trace → trace.log
    trace_logger = logging.getLogger("bi_autogen.trace")
    trace_logger.propagate = False
    trace_logger.addHandler(trace_file)
    trace_logger.addHandler(console_handler)

    # 抑制第三方库日志
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("autogen_core").setLevel(logging.WARNING)
    logging.getLogger("autogen_agentchat").setLevel(logging.WARNING)
    logging.getLogger("autogen_ext").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)


_setup_logging()
logger = logging.getLogger(__name__)


def format_text(text: str) -> str:
    try:
        data = json.loads(text)
        return json.dumps(data, ensure_ascii=False, indent=2)
    except (json.JSONDecodeError, TypeError):
        pass
    json_match = re.search(r"```json\s*(\{.*?\}|\[.*?\])\s*```", text, re.DOTALL)
    if json_match:
        try:
            data = json.loads(json_match.group(1))
            formatted = json.dumps(data, ensure_ascii=False, indent=2)
            return text[: json_match.start()] + formatted + text[json_match.end() :]
        except (json.JSONDecodeError, TypeError):
            pass
    return text


_in_thinking = False  # 跟踪是否处于思考输出状态


def _safe_print(text: str, **kwargs: Any) -> None:
    """安全打印，处理 Windows 控制台 GBK 编码不支持某些 Unicode 字符的问题。"""
    try:
        print(text, **kwargs)
    except UnicodeEncodeError:
        # 替换无法编码的字符
        encoded = text.encode(sys.stdout.encoding or "utf-8", errors="replace").decode(
            sys.stdout.encoding or "utf-8", errors="replace"
        )
        print(encoded, **kwargs)


def _print_stream_event(ev: StreamEvent) -> None:
    """CLI 模式：人类可读格式打印流式事件。"""
    global _in_thinking
    if ev.type == StreamEventType.SESSION_START:
        _in_thinking = False
        _safe_print(f"[会话开始] {ev.data.get('query', '')}")
    elif ev.type == StreamEventType.SESSION_END:
        _safe_print(f"\n[会话结束] 耗时 {ev.data.get('duration_ms', 0)}ms")
    elif ev.type == StreamEventType.PLAN_START:
        _safe_print("[规划中...]")
    elif ev.type == StreamEventType.PLAN_COMPLETE:
        tasks = ev.data.get("tasks", [])
        _safe_print(f"[规划完成] 共 {len(tasks)} 个任务")
    elif ev.type == StreamEventType.AGENT_START:
        _safe_print(f"\n[{ev.agent_name}] 开始执行...")
    elif ev.type == StreamEventType.AGENT_END:
        duration = ev.data.get("duration_ms")
        duration_info = f" ({duration}ms)" if duration else ""
        _safe_print(f"[{ev.agent_name}] 执行完成{duration_info}")
    elif ev.type == StreamEventType.TOOL_CALL:
        tool_name = ev.data.get("tool_name", "")
        _safe_print(f"  调用工具: {tool_name}")
    elif ev.type == StreamEventType.TOOL_RESULT:
        is_error = ev.data.get("is_error", False)
        label = "工具错误" if is_error else "工具结果"
        _safe_print(f"  {label}: {ev.content[:200]}")
    elif ev.type == StreamEventType.LLM_CHUNK:
        # 回答内容；如果刚从思考切换过来，加 [回答] 标记
        if _in_thinking:
            _safe_print("\n[回答] ", end="", flush=True)
            _in_thinking = False
        _safe_print(ev.data.get("chunk", ""), end="", flush=True)
    elif ev.type == StreamEventType.THINK_CHUNK:
        # 思考内容；首次进入思考加 [思考] 标记
        if not _in_thinking:
            _safe_print("[思考] ", end="", flush=True)
            _in_thinking = True
        _safe_print(ev.data.get("chunk", ""), end="", flush=True)
    elif ev.type == StreamEventType.DATA_STORED:
        key = ev.data.get("key", "")
        _safe_print(f"  数据已存储: {key}")
    elif ev.type == StreamEventType.TABLE:
        key = ev.data.get("key", "")
        columns = ev.data.get("columns", [])
        rows = ev.data.get("rows", [])
        row_count = ev.data.get("row_count", 0)
        _safe_print(f"  [表格] {key}: {row_count}行 x {len(columns)}列")
        if rows:
            # 打印简易表格
            col_widths = [min(max(len(str(c)), max((len(str(r[i])) for r in rows), default=0)), 20) for i, c in enumerate(columns)]
            header = " | ".join(str(c).ljust(w) for c, w in zip(columns, col_widths, strict=False))
            _safe_print(f"  {header}")
            _safe_print(f"  {'-' * len(header)}")
            for row in rows[:5]:
                line = " | ".join(str(v).ljust(w) for v, w in zip(row, col_widths, strict=False))
                _safe_print(f"  {line}")
            if row_count > 5:
                _safe_print(f"  ... (共{row_count}行，仅显示前5行)")
    elif ev.type == StreamEventType.ERROR:
        _safe_print(f"[错误] {ev.content[:500]}")


async def run_interactive() -> None:
    team = BITeam()
    # 保持 session_id 不变，使多轮对话上下文生效
    session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    print("BI AutoGen 智能分析助手已启动（输入 'quit' 退出）\n")

    while True:
        try:
            user_input = input("你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见！")
            break

        if not user_input:
            continue
        if user_input.lower() in ("quit", "exit", "q"):
            print("再见！")
            break

        print()
        try:
            async for ev in team.run_stream(ChatRequest(query=user_input, session_id=session_id)):
                _print_stream_event(ev)
            print()
            if team._last_recorder:
                print(team._last_recorder.to_text_summary())
        except Exception as e:
            logger.error("执行失败: %s", e, exc_info=True)
            print(f"\n执行出错: {e}\n")
        finally:
            await team.reset()


async def run_single(task: str, sse: bool = False) -> None:
    """运行单个任务。

    Args:
        task: 用户查询
        sse: True 时输出标准 SSE 格式，False 时输出人类可读格式
    """
    req = ChatRequest(query=task)
    team = BITeam()
    try:
        if sse:
            async for sse_text in team.run_stream_sse(req):
                sys.stdout.write(sse_text)
                sys.stdout.flush()
        else:
            async for ev in team.run_stream(req):
                _print_stream_event(ev)
            print()
            if team._last_recorder:
                print(team._last_recorder.to_text_summary())
    except Exception as e:
        logger.error("执行失败: %s", e, exc_info=True)
        if sse:
            err_ev = StreamEvent(
                type=StreamEventType.ERROR,
                content=f"执行出错: {e}",
                data={"error_type": type(e).__name__, "message": str(e)},
            )
            sys.stdout.write(err_ev.to_sse())
            sys.stdout.flush()
        else:
            print(f"执行出错: {e}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    # 启动配置校验
    from config.startup_check import check_startup_config
    if not check_startup_config():
        sys.exit(1)

    if "--serve" in sys.argv:
        # FastAPI 服务模式
        import uvicorn

        from config import settings
        from gateway.app import create_app

        uvicorn.run(
            create_app(),
            host=settings.server_host,
            port=settings.server_port,
            timeout_graceful_shutdown=settings.shutdown_grace_period,
        )
        return

    sse = "--sse" in sys.argv
    args = [a for a in sys.argv[1:] if a not in ("--sse", "--serve")]
    if args:
        task = " ".join(args)
        asyncio.run(run_single(task, sse=sse))
    else:
        asyncio.run(run_interactive())

if __name__ == "__main__":
    main()