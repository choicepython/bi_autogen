
"""规划执行器：PlanAgent 调用 + DAGPlan 解析 + GraphFlow 构建。

从 agent_layer.py 提取，职责单一：接收用户 query → 产出 DAGPlan + StreamEvent。
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from autogen_agentchat.base import ChatAgent
from autogen_agentchat.conditions import MaxMessageTermination, TextMentionTermination
from autogen_agentchat.messages import StructuredMessage, TextMessage
from autogen_agentchat.teams import DiGraphBuilder, GraphFlow
from autogen_core.models import ChatCompletionClient

from agents.plan_agent import PlanAgent
from config import settings
from core.context import SessionContext, TaskContext
from core.data_context import DataContext
from core.event_translator import EventTranslator
from models import DAGPlan, TaskNode
from models.plan_output import PlanStep
from models.stream_event import StreamEvent
from models.turn_result import PlanRecord, TurnResult
from observability.observer_factory import get_trace_observer

logger = logging.getLogger(__name__)


async def run_plan_phase(
    query: str,
    session_ctx: SessionContext,
    translator: EventTranslator,
    model_client: ChatCompletionClient,
    turn_result: TurnResult,
    plan_holder: list[DAGPlan | None],
) -> AsyncGenerator[StreamEvent, None]:
    """规划阶段：PLAN_START → PlanAgent → PLAN_COMPLETE。

    产出 DAGPlan 并写入 turn_result.plan_record + plan_holder[0]。

    Args:
        plan_holder: 可变容器，规划完成后 plan_holder[0] = DAGPlan。
    """
    yield translator.make_plan_start()

    plan_start = datetime.now()

    # Langfuse planning span
    observer = get_trace_observer()
    plan_span_cm = observer.start_span("planning")
    plan_span = plan_span_cm.__enter__()

    # 运行 PlanAgent，转发事件流
    try:
        plan_agent = PlanAgent(
            model_client=model_client,
            session=session_ctx,
            translator=translator,
        )
        plan_messages: list[TextMessage] = [TextMessage(content=query, source="user")]
        async for event in plan_agent.on_messages_stream(plan_messages, cancellation_token=None):
            if isinstance(event, StreamEvent):
                yield event

        plan = _extract_plan(plan_agent, query)
    except Exception as e:
        logger.error("PlanAgent执行失败: %s", e, exc_info=True)
        plan = DAGPlan(
            reasoning=f"PlanAgent执行失败: {e}",
            tasks=[TaskNode(task_id="1", agent="RAGAgent", description=query)],
        )

    plan_duration = int((datetime.now() - plan_start).total_seconds() * 1000)
    logger.info("[PlanExecutor] DAG规划完成: %d个任务", len(plan.tasks))

    # Langfuse planning span 结束
    if plan_span is not None:
        observer.update_span(plan_span, metadata={
            "task_count": len(plan.tasks),
            "is_complete": plan.is_complete,
            "reasoning": plan.reasoning[:500],
            "duration_ms": plan_duration,
        })
    plan_span_cm.__exit__(None, None, None)

    # Plan 记录 → 写入 TurnResult
    turn_result.plan_record = PlanRecord(
        session_id=session_ctx.session_id,
        chat_id=session_ctx.chat_id,
        task_count=len(plan.tasks),
        tasks_json=[t.model_dump() for t in plan.tasks],
        reasoning=plan.reasoning,
        is_complete=plan.is_complete,
        duration_ms=plan_duration,
    )

    plan_holder[0] = plan

    if plan.is_complete or not plan.tasks:
        yield translator.make_plan_complete(
            content=plan.reasoning,
            agent_name="PlanAgent",
            reasoning=plan.reasoning,
        )
        return

    tasks_data = [t.model_dump() for t in plan.tasks]
    edges = [{"from": dep, "to": t.task_id} for t in plan.tasks for dep in t.depends_on]
    yield translator.make_plan_complete(
        content=f"规划完成，共 {len(plan.tasks)} 个任务",
        agent_name="PlanAgent",
        tasks=tasks_data,
        edges=edges,
        reasoning=plan.reasoning,
    )


def _extract_plan(plan_agent: PlanAgent, task: str) -> DAGPlan:
    """从 PlanAgent 提取 DAGPlan，优先 dag 工具结果，fallback 解析文本。"""
    # dag 工具结果优先
    dag_plan = plan_agent._dag_plan
    if dag_plan is not None:
        dag_plan.validate_dag()
        return dag_plan

    # Fallback: 解析 _final_content 文本
    content = plan_agent._final_content
    if not content:
        logger.warning("PlanAgent未输出内容，降级为单任务")
        return DAGPlan(
            reasoning="PlanAgent未输出内容，降级为单任务",
            tasks=[TaskNode(task_id="1", agent="RAGAgent", description=task)],
        )

    try:
        plan_data = json.loads(content)
        return parse_plan_data(plan_data, task)
    except (json.JSONDecodeError, TypeError):
        json_match = re.search(r"```json\s*(\{.*?\}|\[.*?\])\s*```", content, re.DOTALL)
        if json_match:
            try:
                plan_data = json.loads(json_match.group(1))
                return parse_plan_data(plan_data, task)
            except (json.JSONDecodeError, TypeError):
                pass

        logger.warning("PlanAgent输出无法解析为DAGPlan，内容: %s", content[:200])
        return DAGPlan(
            reasoning="PlanAgent输出解析失败，降级为单任务",
            tasks=[TaskNode(task_id="1", agent="RAGAgent", description=task)],
        )


def parse_plan_data(plan_data: Any, task: str) -> DAGPlan:
    """解析LLM输出的规划数据，兼容数组和对象两种格式。"""
    if isinstance(plan_data, list):
        plan = DAGPlan.from_task_list(plan_data)
    elif isinstance(plan_data, dict):
        plan = DAGPlan.model_validate(plan_data) if "tasks" in plan_data else DAGPlan.from_task_list([plan_data])
    else:
        logger.warning("PlanAgent输出格式不支持: %s", type(plan_data))
        plan = DAGPlan(
            reasoning="PlanAgent输出格式不支持，降级为单任务",
            tasks=[TaskNode(task_id="task_1", agent="DataAnalysisAgent", description=task)],
        )

    plan.validate_dag()
    return plan


def build_dag(
    plan: DAGPlan,
    task_contexts: dict[str, TaskContext],
    model_client: ChatCompletionClient,
    shared_data_context: DataContext | None = None,
) -> tuple[GraphFlow, dict[str, ChatAgent]]:
    """从DAGPlan构建GraphFlow团队。使用 AgentFactory.create_from_task_node。"""
    from core.agent_factory import AgentFactory

    builder = DiGraphBuilder()
    agents: dict[str, ChatAgent] = {}

    shared_data_context = shared_data_context or DataContext()

    for task_node in plan.tasks:
        task_ctx = TaskContext(task_node.task_id, task_contexts.get("__session__"), data_context=shared_data_context)  # type: ignore[arg-type]
        agent = AgentFactory.create_from_task_node(task_node, model_client, task_ctx)
        builder.add_node(agent)
        agents[task_node.task_id] = agent
        task_contexts[task_node.task_id] = task_ctx

    for task_node in plan.tasks:
        for dep_id in task_node.depends_on:
            if dep_id not in agents:
                raise ValueError(f"任务 {task_node.task_id} 依赖的 {dep_id} 不存在")
            builder.add_edge(agents[dep_id], agents[task_node.task_id])

    graph = builder.build()

    termination = TextMentionTermination("TERMINATE") | MaxMessageTermination(settings.max_team_turns)
    team = GraphFlow(
        participants=builder.get_participants(),
        graph=graph,
        termination_condition=termination,
        max_turns=settings.max_team_turns,
        custom_message_types=[StructuredMessage[PlanStep]],
    )

    return team, agents