
"""dag 工具：让 LLM 通过 function calling 提交 DAG 任务列表。

PlanAgent 注册此工具，LLM 规划完成后调用 dag(tasks, reasoning) 提交结构化任务。
工具内部构建 DAGPlan、校验无环，通过回调通知 PlanAgent。
"""

from __future__ import annotations

from collections.abc import Callable

from autogen_core.tools import FunctionTool

from models.dag_plan import DAGPlan, TaskNode


def make_dag_tool(on_plan_created: Callable[[DAGPlan], None]) -> FunctionTool:
    """创建 dag FunctionTool，plan 创建后通过回调通知调用方。

    Args:
        on_plan_created: DAGPlan 构建成功后的回调，调用方在此存储 plan。

    Returns:
        注册给 LLM 的 dag FunctionTool。
    """

    async def _dag_impl(tasks: list[TaskNode], reasoning: str = "") -> str:
        """根据任务列表生成DAG有向无环图。"""
        try:
            plan = DAGPlan(
                reasoning=reasoning,
                tasks=tasks,
                is_complete=not tasks,
            )
            plan.validate_dag()
            on_plan_created(plan)
            return f"DAG生成成功，共{len(plan.tasks)}个任务"
        except ValueError as e:
            return f"DAG生成失败：{e}"
        except Exception as e:
            return f"DAG生成失败: {e}"

    return FunctionTool(
        func=_dag_impl,
        name="dag",
        description=(
            "根据任务列表生成DAG有向无环图。规划完成后调用此工具提交任务列表。"
            "参数：tasks(任务节点列表，每个含task_id/agent/description/depends_on)，"
            "reasoning(规划思路)。调用此工具后无需再输出JSON文本。"
        ),
    )