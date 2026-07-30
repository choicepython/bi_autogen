
from __future__ import annotations

import ast
import asyncio
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import pandas as pd

from autogen_core.tools import FunctionTool

from config import PythonExecError, settings
from core.data_context import DataContext

logger = logging.getLogger(__name__)

# 禁止导入的模块（扩充：覆盖导入机制、内省、网络、序列化等逃逸路径）
_BLOCKED_IMPORTS = {
    # 原有
    "os", "sys", "subprocess", "shutil", "signal", "socket", "http", "urllib", "requests", "pathlib",
    # 导入机制（绕过 import 检查）
    "importlib", "pkgutil", "runpy",
    # 内省/调试
    "builtins", "code", "codeop", "pdb", "inspect",
    # FFI/底层
    "ctypes", "cffi",
    # 网络（补充）
    "ssl", "ftplib", "smtplib", "telnetlib", "xmlrpc",
    # 序列化（可执行任意代码）
    "pickle", "shelve", "marshal", "dill",
    # 进程/线程控制
    "multiprocessing", "threading", "asyncio",
}

# 从执行环境 builtins 中移除的危险函数
# 注意: __import__ 不能从 builtins 移除（import 语句依赖），但 AST 层拦截直接调用 __import__("os")
_REMOVED_BUILTINS = {
    "exec", "eval", "compile", "open", "input", "breakpoint",
    "globals", "locals", "vars", "dir",
    "getattr", "setattr", "delattr", "type",
    "memoryview",
}

# AST 层面拦截的危险调用
_BLOCKED_CALLS = _REMOVED_BUILTINS | {"__import__"}

# 默认允许的安全包
_DEFAULT_PACKAGES: list[dict[str, str]] = [
    {"module": "datetime", "alias": "datetime"},
    {"module": "re", "alias": "re"},
    {"module": "math", "alias": "math"},
    {"module": "random", "alias": "random"},
    {"module": "json", "alias": "json"},
    {"module": "collections", "alias": "collections"},
    {"module": "pandas", "alias": "pd"},
    {"module": "numpy", "alias": "np"},
]

# 禁止的代码模式（AST 层面黑名单）
_BLOCKED_PATTERNS = {
    ".to_csv", ".to_excel", ".to_pickle", ".to_parquet", ".to_hdf",
    ".read_csv", ".read_excel", ".read_pickle",
}


def _check_code_safety(code: str) -> list[str]:
    """AST 层安全检查：拦截危险导入、危险内建调用、dfs 覆写。"""
    violations: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        raise PythonExecError(f"代码语法错误: {e}") from e

    for node in ast.walk(tree):
        # 拦截 import
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _BLOCKED_IMPORTS:
                    violations.append(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            if root in _BLOCKED_IMPORTS:
                violations.append(f"from {node.module}")

        # 拦截危险函数调用
        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Name) and func.id in _BLOCKED_CALLS:
                violations.append(func.id)
            # 拦截 obj.__import__() 形式
            elif isinstance(func, ast.Attribute) and func.attr in _BLOCKED_CALLS:
                violations.append(func.attr)
            # 拦截 getattr(obj, "__class__") 等通过字符串访问 dunder 属性
            elif isinstance(func, ast.Name) and func.id == "getattr":
                # getattr 在 _REMOVED_BUILTINS 中已被拦截，此处为双重保障
                violations.append("getattr")

        # 拦截对 __dunder__ 属性的直接访问（如 obj.__class__, obj.__bases__）
        elif isinstance(node, ast.Attribute):
            if node.attr.startswith("__") and node.attr.endswith("__"):
                # 允许 __init__（类定义中常见）和 __name__（无危险）
                _SAFE_DUNDERS = {"__init__", "__name__", "__str__", "__repr__", "__len__", "__iter__", "__next__"}
                if node.attr not in _SAFE_DUNDERS:
                    violations.append(f"访问危险属性: .{node.attr}")

        # 拦截对 dfs 的覆写
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "dfs":
                    violations.append("覆写 dfs 变量")

    return violations


def _is_unsafe_pattern(code: str) -> list[str]:
    """字符串层面检查危险模式（如 .to_csv 等）。"""
    return [p for p in _BLOCKED_PATTERNS if p in code]


def _build_safe_builtins() -> dict[str, Any]:
    """构建安全的 __builtins__，移除危险内建函数。"""
    import builtins

    safe = dict(vars(builtins))
    for name in _REMOVED_BUILTINS:
        safe.pop(name, None)
    return safe


def _sanitize_result(obj: Any) -> Any:
    """递归清洗执行结果中的 numpy 类型，转为 Python 原生类型。

    - np.integer → int
    - np.floating → float（保留3位小数）
    - np.bool_ → bool
    - np.ndarray → list（递归清洗元素）
    - pd.Series → list（递归清洗元素）
    - dict → 递归清洗值
    - list/tuple → 递归清洗元素
    """


    if isinstance(obj, dict):
        return {k: _sanitize_result(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        cleaned = [_sanitize_result(item) for item in obj]
        return type(obj)(cleaned)  # type: ignore[call-arg]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return round(float(obj), 3)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.ndarray):
        return _sanitize_result(obj.tolist())
    if isinstance(obj, pd.Series):
        return _sanitize_result(obj.tolist())
    if isinstance(obj, float):
        return round(obj, 3)
    return obj


def _build_environment(dfs: list[Any] | None = None) -> dict[str, Any]:
    """构建 exec 执行环境：安全内建 + 默认包 + dfs 注入。"""
    import importlib

    env: dict[str, Any] = {"__builtins__": _build_safe_builtins()}

    # 注入默认允许的包
    for pkg in _DEFAULT_PACKAGES:
        try:
            mod = importlib.import_module(pkg["module"])
            env[pkg["alias"]] = mod
        except ImportError:
            logger.warning("默认包 %s 导入失败，跳过", pkg["module"])

    # 注入 dfs
    env["dfs"] = dfs if dfs is not None else []

    return env


def _exec_sync(code: str, dfs: list[Any] | None = None) -> dict[str, Any]:
    """同步执行代码（在线程池中运行），返回 {success, result, error}。"""
    env = _build_environment(dfs)

    # 分离 import 语句和函数体
    import_lines: list[str] = []
    body_lines: list[str] = []
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return {"success": False, "result": None, "error": f"代码语法错误: {e}"}

    for node in tree.body:
        src = ast.get_source_segment(code, node)
        if src is None:
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_lines.append(src)
        else:
            body_lines.append(src)

    import_code = "\n".join(import_lines)
    body_code = "\n".join(body_lines)

    try:
        # 先执行 import（受安全内建约束）
        if import_code:
            exec(import_code, env)

        # 执行函数体（定义 func_call 等）
        exec(body_code, env)

        # 如果定义了 func_call，自动调用
        if "func_call" in env:
            result = env["func_call"](env["dfs"])
            # 只清洗 result["data"] 中的 numpy 类型，其他字段保留原样
            if isinstance(result, dict) and "data" in result:
                result["data"] = _sanitize_result(result["data"])
            env["result"] = result
        elif "result" not in env:
            env["result"] = None

        return {"success": True, "result": env.get("result"), "error": ""}

    except Exception:
        # 提取干净的错误信息，去掉 exec 栈帧
        tb = traceback.format_exc()
        # 去掉 exec 相关的栈帧行
        clean_lines = []
        skip = False
        for line in tb.splitlines():
            if "exec(" in line:
                skip = True
                continue
            if skip and not line.startswith("  "):
                skip = False
            if not skip:
                clean_lines.append(line)
        error = "\n".join(clean_lines) if clean_lines else tb
        return {"success": False, "result": None, "error": error}


# 线程池，用于在线程中运行同步的 exec（同进程，dfs 无需序列化）
_executor = ThreadPoolExecutor(max_workers=2)


async def python_exec(code: str, dfs: list[Any] | None = None, data_context: DataContext | None = None) -> str:
    """执行 LLM 生成的 Python 代码。

    使用 exec() 在独立环境中执行，dfs 直接注入为变量。
    通过 asyncio + 线程池实现超时控制。

    Args:
        code: LLM 生成的 Python 代码，通常包含 func_call(dfs) 函数定义。
        dfs: DataFrame 列表，作为 dfs 变量注入执行环境。
        data_context: 可选的 DataContext，执行成功后保存结果 DataFrame。
    """
    # 1. AST 安全检查
    violations = _check_code_safety(code)
    if violations:
        return f"代码安全检查失败，禁止以下操作: {', '.join(violations)}。请移除后重试。"

    # 2. 字符串层面危险模式检查
    unsafe = _is_unsafe_pattern(code)
    if unsafe:
        return f"代码包含禁止的操作: {', '.join(unsafe)}。请移除后重试。"

    # 3. 在线程池中执行，支持超时
    timeout = settings.python_exec_timeout
    try:
        loop = asyncio.get_running_loop()
        exec_result = await asyncio.wait_for(
            loop.run_in_executor(_executor, _exec_sync, code, dfs),
            timeout=timeout,
        )
    except TimeoutError:
        raise PythonExecError(f"代码执行超时({timeout}秒)") from None
    except Exception as e:
        raise PythonExecError(f"代码执行异常: {e}") from e

    # 4. 处理执行结果
    if not exec_result["success"]:
        error = exec_result["error"]
        logger.warning("[python_exec] 代码执行失败: %s", error[:300])
        return f"代码执行失败:\n{error}"

    result = exec_result["result"]

    # 5. 保存结果到 DataContext
    if data_context is not None and result is not None:
        # 如果 result 是 dict 且包含 "data" 字段，将 data 转为 DataFrame 保存
        if isinstance(result, dict) and "data" in result:
            data = result["data"]
            if isinstance(data, list) and data:
                df = pd.DataFrame(data)
                key = data_context.generate_key("PyFuncAgent")
                await data_context.put(key, df, meta={"tool_name": "python_exec", "code": code[:2000]})
                summary = data_context.summarize(key)
                return f"代码执行成功，结果已存入DataContext(key={key})。\n\n{summary}\n\n执行结果:\n{result}"
        # 如果 result 本身就是 DataFrame，直接保存
        elif isinstance(result, pd.DataFrame):
            key = data_context.generate_key("PyFuncAgent")
            await data_context.put(key, result, meta={"tool_name": "python_exec", "code": code[:2000]})
            summary = data_context.summarize(key)
            return f"代码执行成功，结果已存入DataContext(key={key})。\n\n{summary}\n\n执行结果:\n{result}"

    # 6. 其他类型结果直接返回
    if result is not None:
        return f"代码执行成功。\n\n执行结果:\n{result}"

    return "代码执行成功，无返回值。"


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_python_exec_tool(data_context: DataContext) -> FunctionTool:
    """创建 Python 代码执行 FunctionTool，闭包捕获 data_context。"""

    async def _python_exec(code: str) -> str:
        # 从 DataContext 加载所有 DataFrame 到 dfs
        dfs: list[Any] = []
        for key in data_context.list_keys():
            df = data_context.get(key)
            if df is not None:
                dfs.append(df)
        return await python_exec(code, dfs=dfs, data_context=data_context)

    return FunctionTool(
        func=_python_exec,
        name="python_exec",
        description="执行Python代码进行数据处理和计算。代码必须定义func_call(dfs)函数，dfs为DataFrame列表。可用包：pandas(as pd),numpy(as np),datetime,re,math,random,json,collections。禁止：os/sys/subprocess等导入、exec/eval/open等调用、文件IO、覆写dfs。超时30秒。参数：code(Python代码字符串)",
    )