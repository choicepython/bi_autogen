
from __future__ import annotations

from pydantic import BaseModel, Field


class ReportTable(BaseModel):
    """报告中的数据表格。"""

    headers: list[str] = Field(description="表头列名列表")
    rows: list[list[str]] = Field(default=[], description="表格数据行，每行是字符串列表，与headers等长")
    caption: str = Field(default="", description="表格标题/说明")


class ReportSection(BaseModel):
    """报告中的单个章节。"""

    heading: str = Field(default="", description="章节标题")
    level: int = Field(default=1, ge=1, le=3, description="标题层级: 1=一级, 2=二级, 3=三级")
    paragraphs: list[str] = Field(default=[], description="正文段落列表，每个元素为一段")
    tables: list[ReportTable] = Field(default=[], description="章节内嵌入的数据表格")
    data_key: str = Field(default="", description="DataContext中的数据key，用于自动嵌入DataFrame为表格")
    chart_keys: list[str] = Field(default=[], description="DataContext中的图表key列表，用于嵌入图表")


class ReportContent(BaseModel):
    """报告结构化内容，由ReportAgent LLM输出，传递给渲染工具。"""

    title: str = Field(description="报告标题")
    format_type: str = Field(default="word", description="输出格式: word, ppt, html, pdf")
    template_name: str = Field(default="blue", description="PPT/PDF主题名称: blue, dark")
    editable: bool = Field(default=True, description="PPT是否可编辑。True=原生元素可编辑，False=图片不可编辑")
    sections: list[ReportSection] = Field(default=[], description="报告章节列表")
    conclusion: str = Field(default="", description="总结/结论段落")