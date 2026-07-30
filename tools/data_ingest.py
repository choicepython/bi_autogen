
"""数据导入工具：读取 Excel/CSV/JSON 文件到 DataContext。"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from autogen_core.tools import FunctionTool

from config.exceptions import DataAnalysisError
from config.settings import settings
from core.data_context import DataContext

logger = logging.getLogger(__name__)

_EXT_MAP: dict[str, str] = {
    ".xlsx": "excel",
    ".xls": "excel",
    ".csv": "csv",
    ".json": "json",
    ".jsonl": "json",
}

# 允许读取的根目录白名单（空=不限制，生产环境应配置）
_ALLOWED_DATA_DIRS: list[str] = getattr(settings, "data_ingest_allowed_dirs", [])


def _validate_path(file_path: str) -> Path:
    """校验文件路径安全性：防止路径穿越和任意文件读取。"""
    path = Path(file_path).resolve()

    # 防止路径穿越（.. 组分）
    if ".." in Path(file_path).parts:
        raise DataAnalysisError("data_ingest", f"路径不允许包含 '..': {file_path}")

    # 白名单校验（仅当配置了允许目录时启用）
    if _ALLOWED_DATA_DIRS:
        allowed_resolved = [Path(d).resolve() for d in _ALLOWED_DATA_DIRS]
        if not any(str(path).startswith(str(d)) for d in allowed_resolved):
            raise DataAnalysisError(
                "data_ingest",
                f"路径不在允许目录内: {file_path}，允许目录: {_ALLOWED_DATA_DIRS}",
            )

    # 阻止敏感系统路径
    _BLOCKED_PREFIXES = [
        "/etc/", "/proc/", "/sys/", "/dev/",
        "C:\\Windows\\", "C:\\Program Files\\",
    ]
    path_str = str(path)
    for prefix in _BLOCKED_PREFIXES:
        if path_str.startswith(prefix):
            raise DataAnalysisError("data_ingest", f"不允许访问系统目录: {file_path}")

    return path


def _infer_file_type(file_path: str) -> str:
    """从扩展名推断文件类型。"""
    ext = Path(file_path).suffix.lower()
    ft = _EXT_MAP.get(ext)
    if ft is None:
        raise DataAnalysisError("data_ingest", f"不支持的文件类型: {ext}，支持: {list(_EXT_MAP.keys())}")
    return ft


def _read_file(file_path: str, file_type: str, sheet_name: str | int | None, encoding: str) -> pd.DataFrame:
    """读取文件并返回 DataFrame。"""
    path = _validate_path(file_path)
    if not path.exists():
        raise DataAnalysisError("data_ingest", f"文件不存在: {file_path}")

    try:
        if file_type == "excel":
            kwargs: dict[str, object] = {}
            if sheet_name is not None:
                kwargs["sheet_name"] = sheet_name
            df = pd.read_excel(file_path, **kwargs)
        elif file_type == "csv":
            df = pd.read_csv(file_path, encoding=encoding)
        elif file_type == "json":
            df = pd.read_json(file_path, encoding=encoding if encoding != "utf-8" else None)
        else:
            raise DataAnalysisError("data_ingest", f"不支持的文件类型: {file_type}")
    except DataAnalysisError:
        raise
    except Exception as e:
        raise DataAnalysisError("data_ingest", f"文件读取失败: {e}") from e

    if not isinstance(df, pd.DataFrame):
        raise DataAnalysisError("data_ingest", f"文件内容无法解析为 DataFrame: {file_path}")
    if df.empty:
        raise DataAnalysisError("data_ingest", f"文件为空: {file_path}")
    if len(df) > settings.data_ingest_max_rows:
        logger.warning("[data_ingest] 文件行数 %d 超过限制 %d，截断", len(df), settings.data_ingest_max_rows)
        df = df.head(settings.data_ingest_max_rows)

    return df


async def data_ingest(
    file_path: str,
    file_type: str = "auto",
    sheet_name: str | int | None = None,
    encoding: str = "utf-8",
    key: str | None = None,
    data_context: DataContext | None = None,
) -> str:
    """数据导入工具。读取本地文件（Excel/CSV/JSON）并写入 DataContext。

    Args:
        file_path: 文件路径（支持 .xlsx/.xls/.csv/.json）
        file_type: 文件类型 auto/csv/excel/json，auto 自动从扩展名推断
        sheet_name: Excel 工作表名或索引（仅 Excel 有效）
        encoding: CSV/JSON 文件编码，默认 utf-8
        key: 自定义 DataContext key，为空则自动生成
        data_context: 共享数据上下文

    Returns:
        描述导入结果的中文字符串
    """
    if data_context is None:
        raise DataAnalysisError("data_ingest", "DataContext 未提供")

    # 推断文件类型
    if file_type == "auto":
        file_type = _infer_file_type(file_path)

    # 读取文件
    df = _read_file(file_path, file_type, sheet_name, encoding)

    # 存入 DataContext
    store_key = key or data_context.generate_key("DataAnalysisAgent")
    await data_context.put(store_key, df, meta={"tool": "data_ingest", "source": file_path, "file_type": file_type})

    logger.info("[data_ingest] 导入成功: %s -> %s, shape=%s", file_path, store_key, df.shape)
    return f"数据导入完成，文件 {file_path} 已导入为 '{store_key}'，共 {len(df)} 行 x {len(df.columns)} 列。列名: {list(df.columns)}"


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_data_ingest_tool(data_context: DataContext) -> FunctionTool:
    """创建数据导入 FunctionTool，闭包捕获 data_context。"""

    async def _ingest(
        file_path: str,
        file_type: str = "auto",
        sheet_name: str | int | None = None,
        encoding: str = "utf-8",
        key: str | None = None,
    ) -> str:
        return await data_ingest(file_path, file_type, sheet_name, encoding, key, data_context)

    return FunctionTool(
        func=_ingest,
        name="data_ingest",
        description="数据导入工具。读取Excel/CSV/JSON文件到DataContext。参数：file_path(文件路径), file_type(auto/csv/excel/json), sheet_name(Excel工作表名), encoding(编码), key(自定义key)",
    )