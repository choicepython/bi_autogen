
"""图表生成工具：基于 pyecharts 生成交互式 HTML 图表。"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

import pandas as pd
from autogen_core.tools import FunctionTool

from config.exceptions import VisualizationError
from config.settings import settings
from models.chart_artifact import ChartArtifact
from core.data_context import DataContext

logger = logging.getLogger(__name__)

_VALID_CHART_TYPES = {"bar", "line", "pie", "scatter", "heatmap", "area", "radar", "funnel"}


def _validate_input(df: pd.DataFrame, chart_type: str, x_col: str | None, y_col: str | list[str] | None) -> None:
    """校验图表参数。"""
    if chart_type not in _VALID_CHART_TYPES:
        raise VisualizationError("chart_generate", f"不支持的图表类型: {chart_type}，可选: {', '.join(sorted(_VALID_CHART_TYPES))}")

    if chart_type != "heatmap" and x_col and x_col not in df.columns:
        raise VisualizationError("chart_generate", f"x_col '{x_col}' 不存在，可用列: {list(df.columns)}")

    if y_col:
        cols = [y_col] if isinstance(y_col, str) else y_col
        missing = [c for c in cols if c not in df.columns]
        if missing:
            raise VisualizationError("chart_generate", f"y_col {missing} 不存在，可用列: {list(df.columns)}")


def _aggregate_data(df: pd.DataFrame, x_col: str, y_col: str | list[str], agg_func: str) -> pd.DataFrame:
    """按 x_col 分组聚合。"""
    valid_funcs = {"sum", "mean", "count", "max", "min"}
    if agg_func not in valid_funcs:
        raise VisualizationError("chart_generate", f"不支持的聚合函数: {agg_func}，可选: {valid_funcs}")

    y_cols = [y_col] if isinstance(y_col, str) else y_col
    agg_dict = {c: agg_func for c in y_cols}
    return df.groupby(x_col).agg(agg_dict).reset_index()


def _make_init_opts(theme: str) -> object:
    """构建 InitOpts。pyecharts 2.x 的 theme=None 会触发 js_dependencies bug，light 主题不传 theme 参数。"""
    from pyecharts import options as opts

    kwargs: dict[str, object] = {
        "width": f"{settings.chart_default_width}px",
        "height": f"{settings.chart_default_height}px",
    }
    if theme and theme != "light":
        kwargs["theme"] = theme
    return opts.InitOpts(**kwargs)


def _build_bar(df: pd.DataFrame, x_col: str | None, y_col: str | list[str] | None, title: str, orient: str, theme: str) -> object:
    """构建柱状图。"""
    from pyecharts import options as opts
    from pyecharts.charts import Bar

    x_data = df[x_col].tolist() if x_col else df.index.tolist()
    y_cols = [y_col] if isinstance(y_col, str) else y_col

    chart = Bar(init_opts=_make_init_opts(theme))
    chart.add_xaxis(x_data)

    for col in (y_cols or []):
        chart.add_yaxis(col, df[col].tolist())

    if orient == "horizontal":
        chart.reversal_axis()

    chart.set_global_opts(title_opts=opts.TitleOpts(title=title), toolbox_opts=opts.ToolboxOpts())
    return chart


def _build_line(df: pd.DataFrame, x_col: str | None, y_col: str | list[str] | None, title: str, theme: str, area: bool = False) -> object:
    """构建折线图/面积图。"""
    from pyecharts import options as opts
    from pyecharts.charts import Line

    x_data = df[x_col].tolist() if x_col else df.index.tolist()
    y_cols = [y_col] if isinstance(y_col, str) else y_col

    chart = Line(init_opts=_make_init_opts(theme))
    chart.add_xaxis(x_data)

    for col in (y_cols or []):
        if area:
            chart.add_yaxis(col, df[col].tolist(), areastyle_opts=opts.AreaStyleOpts(opacity=0.5))
        else:
            chart.add_yaxis(col, df[col].tolist())

    chart.set_global_opts(title_opts=opts.TitleOpts(title=title), toolbox_opts=opts.ToolboxOpts())
    return chart


def _build_pie(df: pd.DataFrame, x_col: str | None, y_col: str | list[str] | None, title: str, theme: str) -> object:
    """构建饼图。"""
    from pyecharts import options as opts
    from pyecharts.charts import Pie

    x_data = df[x_col].tolist() if x_col else df.index.tolist()
    y_col_name = y_col if isinstance(y_col, str) else (y_col[0] if y_col else None)
    if not y_col_name:
        raise VisualizationError("chart_generate", "饼图需要指定 y_col")

    data_pair = [[str(x), float(v)] for x, v in zip(x_data, df[y_col_name])]
    chart = Pie(init_opts=_make_init_opts(theme))
    chart.add("", data_pair)
    chart.set_global_opts(title_opts=opts.TitleOpts(title=title), legend_opts=opts.LegendOpts(orient="vertical", pos_top="15%", pos_left="2%"))
    return chart


def _build_scatter(df: pd.DataFrame, x_col: str | None, y_col: str | list[str] | None, title: str, theme: str) -> object:
    """构建散点图。"""
    from pyecharts import options as opts
    from pyecharts.charts import Scatter

    x_data = df[x_col].tolist() if x_col else df.index.tolist()
    y_col_name = y_col if isinstance(y_col, str) else (y_col[0] if y_col else None)
    if not y_col_name:
        raise VisualizationError("chart_generate", "散点图需要指定 y_col")

    chart = Scatter(init_opts=_make_init_opts(theme))
    chart.add_xaxis(x_data)
    chart.add_yaxis(y_col_name, df[y_col_name].tolist())
    chart.set_global_opts(title_opts=opts.TitleOpts(title=title), toolbox_opts=opts.ToolboxOpts())
    return chart


def _build_heatmap(df: pd.DataFrame, x_col: str | None, y_col: str | list[str] | None, title: str, theme: str) -> object:
    """构建热力图。"""
    from pyecharts import options as opts
    from pyecharts.charts import HeatMap

    numeric_cols = df.select_dtypes(include="number").columns.tolist()
    if len(numeric_cols) < 1:
        raise VisualizationError("chart_generate", "热力图需要至少一个数值列")

    if x_col and len(numeric_cols) >= 2:
        x_idx = list(df[x_col].unique())
        y_idx = list(df[numeric_cols[1]].unique()) if len(numeric_cols) >= 2 else [0]
        val_col = numeric_cols[0]
        data = [[str(df.iloc[i][x_col]), str(df.iloc[i][numeric_cols[1]]), float(df.iloc[i][val_col])] for i in range(len(df))]
    else:
        corr = df[numeric_cols].corr()
        x_idx = numeric_cols
        y_idx = numeric_cols
        data = []
        for xi, x_name in enumerate(x_idx):
            for yi, y_name in enumerate(y_idx):
                data.append([xi, yi, round(float(corr.iloc[xi, yi]), 4)])

    chart = HeatMap(init_opts=_make_init_opts(theme))
    chart.add_xaxis(x_idx)
    chart.add_yaxis("", y_idx, data, label_opts=opts.LabelOpts(is_show=True, position="inside"))
    chart.set_global_opts(title_opts=opts.TitleOpts(title=title), visualmap_opts=opts.VisualMapOpts(), toolbox_opts=opts.ToolboxOpts())
    return chart


def _build_radar(df: pd.DataFrame, x_col: str | None, y_col: str | list[str] | None, title: str, theme: str) -> object:
    """构建雷达图。"""
    from pyecharts import options as opts
    from pyecharts.charts import Radar

    y_cols = [y_col] if isinstance(y_col, str) else y_col
    if not y_cols:
        raise VisualizationError("chart_generate", "雷达图需要指定 y_col")

    schema = [opts.RadarIndicatorItem(name=c, max_=float(df[c].max()) * 1.2) for c in y_cols]
    data = [float(df[c].mean()) for c in y_cols]

    chart = Radar(init_opts=_make_init_opts(theme))
    chart.add_schema(schema=schema)
    chart.add("均值", [data])
    chart.set_global_opts(title_opts=opts.TitleOpts(title=title))
    return chart


def _build_funnel(df: pd.DataFrame, x_col: str | None, y_col: str | list[str] | None, title: str, theme: str) -> object:
    """构建漏斗图。"""
    from pyecharts import options as opts
    from pyecharts.charts import Funnel

    x_data = df[x_col].tolist() if x_col else df.index.tolist()
    y_col_name = y_col if isinstance(y_col, str) else (y_col[0] if y_col else None)
    if not y_col_name:
        raise VisualizationError("chart_generate", "漏斗图需要指定 y_col")

    data_pair = [[str(x), float(v)] for x, v in zip(x_data, df[y_col_name])]
    chart = Funnel(init_opts=_make_init_opts(theme))
    chart.add("", data_pair)
    chart.set_global_opts(title_opts=opts.TitleOpts(title=title))
    return chart


def _build_chart(
    df: pd.DataFrame,
    chart_type: str,
    x_col: str | None,
    y_col: str | list[str] | None,
    title: str,
    theme: str,
    orient: str,
) -> object:
    """根据 chart_type 构建对应的 pyecharts 图表对象。"""
    builders = {
        "bar": lambda: _build_bar(df, x_col, y_col, title, orient, theme),
        "line": lambda: _build_line(df, x_col, y_col, title, theme),
        "area": lambda: _build_line(df, x_col, y_col, title, theme, area=True),
        "pie": lambda: _build_pie(df, x_col, y_col, title, theme),
        "scatter": lambda: _build_scatter(df, x_col, y_col, title, theme),
        "heatmap": lambda: _build_heatmap(df, x_col, y_col, title, theme),
        "radar": lambda: _build_radar(df, x_col, y_col, title, theme),
        "funnel": lambda: _build_funnel(df, x_col, y_col, title, theme),
    }
    builder = builders.get(chart_type)
    if builder is None:
        raise VisualizationError("chart_generate", f"不支持的图表类型: {chart_type}")
    return builder()


async def chart_generate(
    data_key: str,
    chart_type: str,
    x_col: str | None = None,
    y_col: str | list[str] | None = None,
    title: str = "",
    width: int = 800,
    height: int = 500,
    theme: str = "light",
    agg_func: str | None = None,
    orient: str = "vertical",
    data_context: DataContext | None = None,
) -> str:
    """图表生成工具。基于 pyecharts 从 DataContext 数据生成交互式 HTML 图表。

    Args:
        data_key: DataContext 中的数据 key
        chart_type: 图表类型 bar/line/pie/scatter/heatmap/area/radar/funnel
        x_col: X 轴/分类列名
        y_col: Y 轴/数值列名，多列用于分组
        title: 图表标题
        width: 图表宽度像素
        height: 图表高度像素
        theme: 图表主题 light/dark
        agg_func: 聚合函数 sum/mean/count/max/min
        orient: 柱状图方向 vertical/horizontal
        data_context: 共享数据上下文

    Returns:
        描述图表生成结果的中文字符串
    """
    if data_context is None:
        raise VisualizationError("chart_generate", "DataContext 未提供")

    df = data_context.get(data_key)
    if df is None:
        raise VisualizationError("chart_generate", f"DataContext 中不存在 key: {data_key}，可用: {data_context.list_keys()}")

    _validate_input(df, chart_type, x_col, y_col)

    # 聚合
    if agg_func and x_col and y_col:
        df = _aggregate_data(df, x_col, y_col, agg_func)

    # 构建图表
    chart = _build_chart(df, chart_type, x_col, y_col, title or f"{chart_type} chart", theme, orient)

    # 渲染 HTML
    output_dir = settings.chart_output_dir or tempfile.mkdtemp(prefix="bi_chart_")
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    chart_key = data_context.generate_key("VisualizationAgent")
    html_path = str(Path(output_dir) / f"{chart_key}.html")
    chart.render(html_path)

    # 提取 echarts option JSON 供仪表板组合用
    echarts_option = chart.dump_options()

    # 存储到 DataContext
    artifact = ChartArtifact(
        key=chart_key,
        chart_type=chart_type,
        title=title or f"{chart_type} chart",
        data_key=data_key,
        html_path=html_path,
        width=width,
        height=height,
        meta={"echarts_option": echarts_option},
    )
    await data_context.put_chart(chart_key, artifact)

    logger.info("[chart_generate] 图表生成完成: type=%s, key=%s, path=%s", chart_type, chart_key, html_path)
    return f"图表生成完成，类型={chart_type}，key='{chart_key}'，HTML文件: {html_path}"


# ---------------------------------------------------------------------------
# FunctionTool 工厂
# ---------------------------------------------------------------------------

def make_chart_generate_tool(data_context: DataContext) -> FunctionTool:
    """创建图表生成 FunctionTool，闭包捕获 data_context。"""

    async def _chart(
        data_key: str,
        chart_type: str,
        x_col: str | None = None,
        y_col: str | list[str] | None = None,
        title: str = "",
        width: int = 800,
        height: int = 500,
        theme: str = "light",
        agg_func: str | None = None,
        orient: str = "vertical",
    ) -> str:
        return await chart_generate(data_key, chart_type, x_col, y_col, title, width, height, theme, agg_func, orient, data_context)

    return FunctionTool(
        func=_chart,
        name="chart_generate",
        description="图表生成工具。基于数据生成交互式HTML图表。参数：data_key(数据key), chart_type(bar/line/pie/scatter/heatmap/area/radar/funnel), x_col(X轴列名), y_col(Y轴列名), title(标题), theme(light/dark), agg_func(sum/mean/count/max/min), orient(vertical/horizontal)",
    )