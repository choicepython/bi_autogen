
"""仪表板生成工具：将多个图表组合为交互式 HTML 仪表板。"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path

from autogen_core.tools import FunctionTool

from config.exceptions import VisualizationError
from config.settings import settings
from core.data_context import DataContext

logger = logging.getLogger(__name__)


def _compose_html_page(artifacts: list[object], title: str) -> str:
    """将多个图表 HTML 组合为一个垂直排列的页面。"""
    parts = [
        "<!DOCTYPE html>",
        "<html><head>",
        f"<title>{title}</title>",
        '<meta charset="utf-8">',
        '<style>body{font-family:Arial,sans-serif;margin:20px;background:#fff}'
        ".chart-container{margin:20px 0;padding:10px;border:1px solid #e0e0e0;border-radius:8px;background:#fff}"
        "h1{text-align:center;color:#333}</style>",
        "</head><body>",
        f"<h1>{title}</h1>" if title else "",
    ]

    for art in artifacts:
        html_path = art.html_path  # type: ignore[attr-defined]
        try:
            with open(html_path, encoding="utf-8") as f:
                content = f.read()
            # 提取 body 内容（echarts 渲染的完整 HTML）
            parts.append(f'<div class="chart-container">{content}</div>')
        except Exception as e:
            logger.warning("读取图表HTML失败 %s: %s", html_path, e)

    parts.append("</body></html>")
    return "\n".join(parts)


def _compose_html_tab(artifacts: list[object], title: str) -> str:
    """将多个图表组合为标签页布局。"""
    tab_items = []
    content_divs = []
    for i, art in enumerate(artifacts):
        tab_label = art.title  # type: ignore[attr-defined]
        active = "active" if i == 0 else ""
        tab_items.append(
            f'<button class="tab-btn {active}" onclick="showTab({i})">{tab_label}</button>'
        )
        display = "block" if i == 0 else "none"
        html_path = art.html_path  # type: ignore[attr-defined]
        try:
            with open(html_path, encoding="utf-8") as f:
                content = f.read()
            content_divs.append(f'<div class="tab-content" id="tab-{i}" style="display:{display}">{content}</div>')
        except Exception as e:
            logger.warning("读取图表HTML失败 %s: %s", html_path, e)

    js_code = "function showTab(n){document.querySelectorAll('.tab-content').forEach(function(el){el.style.display='none'});document.querySelectorAll('.tab-btn').forEach(function(el){el.classList.remove('active')});document.getElementById('tab-'+n).style.display='block';document.querySelectorAll('.tab-btn')[n].classList.add('active');}"
    return f"""<!DOCTYPE html>
<html><head>
<title>{title}</title>
<meta charset="utf-8">
<style>
body{{font-family:Arial,sans-serif;margin:20px;background:#fff}}
.tab-bar{{display:flex;gap:0;border-bottom:2px solid #409eff;margin-bottom:20px}}
.tab-btn{{padding:10px 24px;border:none;background:#f5f5f5;cursor:pointer;font-size:14px;border-radius:4px 4px 0 0}}
.tab-btn.active{{background:#409eff;color:#fff}}
.tab-btn:hover{{background:#e0e0e0}}
h1{{text-align:center;color:#333}}
</style>
</head><body>
<h1>{title}</h1>
<div class="tab-bar">{''.join(tab_items)}</div>
{''.join(content_divs)}
<script>{js_code}</script>
</body></html>"""


def _compose_html_grid(artifacts: list[object], title: str, columns: int) -> str:
    """将多个图表组合为网格布局。"""
    cells = []
    for art in artifacts:
        html_path = art.html_path  # type: ignore[attr-defined]
        try:
            with open(html_path, encoding="utf-8") as f:
                content = f.read()
            cells.append(f'<div class="grid-cell">{content}</div>')
        except Exception as e:
            logger.warning("读取图表HTML失败 %s: %s", html_path, e)

    col_pct = 100 // columns
    return f"""<!DOCTYPE html>
<html><head>
<title>{title}</title>
<meta charset="utf-8">
<style>
body{{font-family:Arial,sans-serif;margin:20px;background:#fff}}
.grid-container{{display:flex;flex-wrap:wrap;gap:16px}}
.grid-cell{{width:calc({col_pct}% - 16px);min-width:400px;border:1px solid #e0e0e0;border-radius:8px;padding:10px;background:#fff}}
h1{{text-align:center;color:#333}}
</style>
</head><body>
<h1>{title}</h1>
<div class="grid-container">
{''.join(cells)}
</div>
</body></html>"""


async def dashboard_generate(
    chart_keys: list[str],
    layout: str = "grid",
    title: str = "",
    columns: int = 2,
    data_context: DataContext | None = None,
) -> str:
    """仪表板生成工具。将多个图表组合为交互式 HTML 仪表板。

    Args:
        chart_keys: DataContext 中的图表 key 列表
        layout: 布局模式 grid(网格)/tab(标签页)/page(垂直排列)
        title: 仪表板标题
        columns: 网格列数（grid 模式）
        data_context: 共享数据上下文

    Returns:
        描述仪表板生成结果的中文字符串
    """
    if data_context is None:
        raise VisualizationError("dashboard_generate", "DataContext 未提供")

    if not chart_keys:
        raise VisualizationError("dashboard_generate", "chart_keys 不能为空")

    # 获取所有图表产物
    artifacts = []
    for key in chart_keys:
        art = data_context.get_chart(key)
        if art is None:
            raise VisualizationError("dashboard_generate", f"图表 key '{key}' 不存在，可用: {data_context.list_chart_keys()}")
        artifacts.append(art)

    # 组合 HTML
    dashboard_title = title or "数据仪表板"
    if layout == "tab":
        html = _compose_html_tab(artifacts, dashboard_title)
    elif layout == "grid":
        html = _compose_html_grid(artifacts, dashboard_title, columns)
    else:
        html = _compose_html_page(artifacts, dashboard_title)

    output_dir = settings.chart_output_dir or tempfile.mkdtemp(prefix="bi_dashboard_")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    html_path = str(Path(output_dir) / "dashboard.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)

    logger.info("[dashboard_generate] 仪表板生成完成: layout=%s, charts=%d, path=%s", layout, len(artifacts), html_path)
    return f"仪表板生成完成，布局={layout}，包含 {len(artifacts)} 个图表，HTML文件: {html_path}"


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_dashboard_generate_tool(data_context: DataContext) -> FunctionTool:
    """创建仪表板生成 FunctionTool，闭包捕获 data_context。"""

    async def _dashboard(
        chart_keys: list[str],
        layout: str = "grid",
        title: str = "",
        columns: int = 2,
    ) -> str:
        return await dashboard_generate(chart_keys, layout, title, columns, data_context)

    return FunctionTool(
        func=_dashboard,
        name="dashboard_generate",
        description="仪表板生成工具。将多个图表组合为交互式HTML仪表板。参数：chart_keys(图表key列表), layout(grid/tab/page), title(仪表板标题), columns(网格列数)",
    )