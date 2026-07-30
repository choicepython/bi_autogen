
from pydantic import BaseModel, Field


class PlanStep(BaseModel):
    next_agent: str = Field(
        description="下一步执行的Agent名称，可选：APIAgent, SQLAgent, PyFuncAgent, DataAnalysisAgent, VisualizationAgent, ReportAgent, RAGAgent"
    )
    task_description: str = Field(description="传递给执行Agent的任务描述")
    is_complete: bool = Field(default=False, description="所有任务是否已完成")