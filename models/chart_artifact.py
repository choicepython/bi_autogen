
"""图表产物元数据模型。"""

from __future__ import annotations

from pydantic import BaseModel


class ChartArtifact(BaseModel):
    """存储在 DataContext._charts 中的图表产物元数据。"""

    key: str  # DataContext 中的唯一 key
    chart_type: str  # bar / line / pie / scatter / heatmap / area / radar / funnel
    title: str  # 图表标题
    data_key: str  # 来源 DataFrame 的 DataContext key
    html_path: str  # pyecharts 生成的 HTML 文件路径
    png_path: str | None = None  # Playwright 截图的 PNG 路径（按需生成）
    width: int = 800  # 图表宽度（像素）
    height: int = 500  # 图表高度（像素）
    meta: dict[str, object] = {}  # 扩展元数据（含 echarts_option JSON，供仪表板组合用）