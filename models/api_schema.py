
from pydantic import BaseModel, Field


class APIQueryParams(BaseModel):
    api_name: str = Field(description="API工具名称")
    params: dict[str, str | int | float | None] = Field(default_factory=dict, description="API查询参数")


class APIQueryResult(BaseModel):
    success: bool = Field(description="查询是否成功")
    data_key: str = Field(default="", description="DataContext中存储的数据key")
    row_count: int = Field(default=0, description="返回数据行数")
    error_message: str = Field(default="", description="错误信息")
    summary: str = Field(default="", description="数据摘要")