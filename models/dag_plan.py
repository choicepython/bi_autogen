
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class TaskNode(BaseModel):
    """DAG中的一个任务节点。"""

    task_id: str = Field(description="任务唯一标识，如 1 或 task_1")
    agent: str = Field(
        description="执行Agent名称，可选：APIAgent, SQLAgent, DataAnalysisAgent, VisualizationAgent, RAGAgent, PyFuncAgent, ReportAgent"
    )
    description: str = Field(description="传递给Agent的任务描述")
    depends_on: list[str] = Field(default=[], description="依赖的task_id列表，空列表表示无依赖")


class DAGPlan(BaseModel):
    """PlanAgent输出的DAG规划，包含任务列表和依赖关系。

    LLM输出格式为纯数组 [{task_id, agent, description, depends_on}, ...]，
    解析时自动包装为 DAGPlan。
    """

    reasoning: str = Field(default="", description="规划思路简述（内部生成，非LLM输出）")
    tasks: list[TaskNode] = Field(description="任务节点列表，按依赖关系构成DAG")
    is_complete: bool = Field(default=False, description="是否无需执行任何任务")

    @classmethod
    def from_task_list(cls, task_list: list[dict[str, Any]]) -> DAGPlan:
        """从LLM输出的纯数组格式构建DAGPlan。

        兼容处理：
        - task_id 可能是 int，统一转为 str
        - depends_on 中的 id 可能是 int，统一转为 str
        - 空数组视为 is_complete=True
        """
        if not task_list:
            return cls(reasoning="LLM返回空任务列表", tasks=[], is_complete=True)

        normalized_tasks = []
        for item in task_list:
            task_id = str(item.get("task_id", ""))
            depends_on = [str(d) for d in item.get("depends_on", [])]
            # 防御：移除自依赖（LLM 可能输出 depends_on 包含自身 task_id）
            depends_on = [d for d in depends_on if d != task_id]
            normalized_tasks.append(
                TaskNode(
                    task_id=task_id,
                    agent=item.get("agent", ""),
                    description=item.get("description", ""),
                    depends_on=depends_on,
                )
            )
        return cls(reasoning="", tasks=normalized_tasks)

    def has_cycle(self) -> bool:
        """检测DAG中是否存在循环依赖（非破坏性检查，Kahn拓扑排序）。

        存在未访问节点（入度始终>0）说明有环。
        """
        if not self.tasks:
            return False

        task_ids = {t.task_id for t in self.tasks}
        in_degree: dict[str, int] = {t.task_id: 0 for t in self.tasks}
        adj: dict[str, list[str]] = {t.task_id: [] for t in self.tasks}
        for t in self.tasks:
            for dep in t.depends_on:
                if dep in task_ids and dep != t.task_id:
                    adj[dep].append(t.task_id)
                    in_degree[t.task_id] += 1

        queue = [tid for tid, deg in in_degree.items() if deg == 0]
        visited = 0
        while queue:
            node = queue.pop(0)
            visited += 1
            for neighbor in adj[node]:
                in_degree[neighbor] -= 1
                if in_degree[neighbor] == 0:
                    queue.append(neighbor)

        return visited < len(task_ids)

    def validate_dag(self) -> None:
        """验证 DAG 完整性，修复常见 LLM 输出问题。

        1. 移除指向不存在 task_id 的依赖
        2. 环检测（has_cycle），有环则抛出 ValueError
        3. 如果所有任务都有依赖（无根节点），清除所有依赖使其全部成为根节点
        """
        if not self.tasks:
            return

        task_ids = {t.task_id for t in self.tasks}

        # 移除指向不存在 task_id 的依赖
        for t in self.tasks:
            t.depends_on = [d for d in t.depends_on if d in task_ids]

        # 环检测
        if self.has_cycle():
            cycle_nodes = [
                t.task_id for t in self.tasks
                if any(d in task_ids and d != t.task_id for d in t.depends_on)
            ]
            raise ValueError(
                f"DAG存在环! 环中节点: {cycle_nodes}"
            )

        # 检查是否有根节点（无依赖的任务）
        root_tasks = [t for t in self.tasks if not t.depends_on]
        if not root_tasks:
            # 所有任务都有依赖 → DAG 无法执行，清除所有依赖
            import logging
            logging.getLogger(__name__).warning(
                "DAG无根节点（所有任务都有依赖），清除所有依赖: %s",
                [(t.task_id, t.depends_on) for t in self.tasks],
            )
            for t in self.tasks:
                t.depends_on = []