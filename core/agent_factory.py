
"""统一 Agent 工厂。

替代 agent_layer.py 中的内联 if/elif 链，使用注册表模式创建 Agent。
惰性导入避免顶层加载全部 Agent 类，支持 P2 开闭原则：
新增 Agent 只需调用 AgentFactory.register() 注册，无需修改工厂代码。
"""

from __future__ import annotations

import importlib
import logging
from typing import ClassVar

from autogen_core.models import ChatCompletionClient

from agents.base import BIBaseAgent
from core.context import SessionContext, TaskContext
from core.data_context import DataContext
from models import TaskNode
from models.routing import AgentType

logger = logging.getLogger(__name__)


class AgentFactory:
    """统一Agent工厂，惰性注册表 + importlib 延迟加载。

    使用方式：
        agent = AgentFactory.create(AgentType.API, model_client, dc, session=s)
        AgentFactory.register(AgentType.CUSTOM, "agents.custom_agent.CustomAgent")
    """

    # AgentType → 模块路径映射（惰性加载，不在顶层 import 具体类）
    _MODULE_MAP: ClassVar[dict[AgentType, str]] = {
        AgentType.API: "agents.api_agent.APIAgent",
        AgentType.SQL: "agents.sql_agent.SQLAgent",
        AgentType.PYFUNC: "agents.pyfunc_agent.PyFuncAgent",
        AgentType.DATA_ANALYSIS: "agents.data_analysis_agent.DataAnalysisAgent",
        AgentType.VISUALIZATION: "agents.visualization_agent.VisualizationAgent",
        AgentType.REPORT: "agents.report_agent.ReportAgent",
        AgentType.SEARCH: "agents.rag_agent.RAGAgent",
    }

    # 已解析的 AgentType → 类缓存
    _resolved: ClassVar[dict[AgentType, type[BIBaseAgent]]] = {}

    @classmethod
    def register(cls, agent_type: AgentType, module_path: str) -> None:
        """注册新的 Agent 类型（P2 开闭扩展点）。

        Args:
            agent_type: Agent 类型枚举值。
            module_path: 完整模块路径，格式 "agents.xxx.XxxAgent"。
        """
        cls._MODULE_MAP[agent_type] = module_path
        # 清除可能已缓存的旧解析结果
        cls._resolved.pop(agent_type, None)
        logger.info("[AgentFactory] 注册: %s → %s", agent_type.value, module_path)

    @classmethod
    def _resolve(cls, agent_type: AgentType) -> type[BIBaseAgent]:
        """惰性解析 AgentType → Agent 类，解析后缓存。"""
        if agent_type in cls._resolved:
            return cls._resolved[agent_type]

        module_path = cls._MODULE_MAP.get(agent_type)
        if module_path is None:
            raise ValueError(f"未注册的Agent类型: {agent_type.value}")

        # "agents.api_agent.APIAgent" → module="agents.api_agent", attr="APIAgent"
        parts = module_path.rsplit(".", 1)
        if len(parts) != 2:
            raise ValueError(f"模块路径格式错误: {module_path}，应为 'module.ClassName'")

        module_name, class_name = parts
        module = importlib.import_module(module_name)
        agent_cls = getattr(module, class_name, None)
        if agent_cls is None:
            raise ValueError(f"模块 {module_name} 中未找到类 {class_name}")

        if not issubclass(agent_cls, BIBaseAgent):
            raise TypeError(f"{module_path} 不是 BIBaseAgent 的子类")

        cls._resolved[agent_type] = agent_cls
        return agent_cls

    @classmethod
    def create(
        cls,
        agent_type: AgentType,
        model_client: ChatCompletionClient,
        data_context: DataContext,
        *,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
    ) -> BIBaseAgent:
        """从 AgentType 枚举创建Agent。SINGLE_AGENT 和 DAG 通用。"""
        agent_cls = cls._resolve(agent_type)
        return agent_cls(
            model_client, data_context,
            session=session, task_id=task_id, task_description=task_description,
        )

    @classmethod
    def create_from_task_node(
        cls,
        task_node: TaskNode,
        model_client: ChatCompletionClient,
        task_context: TaskContext,
    ) -> BIBaseAgent:
        """从 TaskNode 创建Agent（str → AgentType 转换后委托 create()）。"""
        agent_type = AgentType(task_node.agent)
        agent = cls.create(
            agent_type, model_client, task_context.data_context,
            session=task_context.session,
            task_id=task_node.task_id,
            task_description=task_node.description,
        )
        # 设置 DAG 依赖信息，供 agent 入口跳过检查使用
        agent._depends_on = task_node.depends_on
        return agent

    @classmethod
    def create_default(
        cls,
        model_client: ChatCompletionClient,
        data_context: DataContext,
        *,
        session: SessionContext | None = None,
        task_id: str | None = None,
        task_description: str | None = None,
    ) -> BIBaseAgent:
        """创建默认Agent（RAGAgent），用于路由结果无明确agent_type时。"""
        return cls.create(
            AgentType.SEARCH, model_client, data_context,
            session=session, task_id=task_id, task_description=task_description,
        )