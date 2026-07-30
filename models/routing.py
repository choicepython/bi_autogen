
"""路由决策模型：3层路由架构的输出结构。"""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel


class ExecutionMode(str, Enum):
    """执行模式。"""

    SINGLE_AGENT = "single_agent"  # 单Agent直接执行，跳过PlanAgent+DAG
    FULL_TEAM = "full_team"  # 完整PlanAgent+DAG流水线


class AgentType(str, Enum):
    """可路由的Agent类型。"""

    SEARCH = "RAGAgent"
    API = "APIAgent"
    SQL = "SQLAgent"
    PYFUNC = "PyFuncAgent"
    DATA_ANALYSIS = "DataAnalysisAgent"
    VISUALIZATION = "VisualizationAgent"
    REPORT = "ReportAgent"


class RoutingResult(BaseModel):
    """路由决策结果。"""

    layer: int  # 命中的路由层 (1/2/3)
    mode: ExecutionMode  # 执行模式
    agent_type: AgentType | None = None  # SINGLE_AGENT模式下的目标Agent
    task_description: str = ""  # 传给Agent的任务描述
    reasoning: str = ""  # 路由决策理由