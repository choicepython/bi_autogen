
"""上下文规格解析器：从 Agent 类上读取 context_spec。

从 models/conversation.py 迁移而来，打破 models → agents/core 的循环依赖。
此模块位于 core/，可以安全导入 agents/ 和 core/。
"""

from __future__ import annotations

from models.conversation import ContextSpec
from models.routing import AgentType


def resolve_context_spec(agent_type: AgentType | str) -> ContextSpec:
    """从 Agent 类上读取 context_spec，新增 Agent 无需改这里。

    Args:
        agent_type: Agent 类型枚举值或字符串名称。

    Returns:
        对应 Agent 的 ContextSpec，未找到时返回默认值。
    """
    # lazy import: 避免 core → agents 循环依赖（模块加载顺序问题）
    from agents.base import BIBaseAgent, ContextSpec as _CS

    agent_cls: type[BIBaseAgent] | None = None

    # 统一为字符串名称
    agent_name = agent_type.value if isinstance(agent_type, AgentType) else str(agent_type)

    # PlanAgent 不在 AgentFactory 注册表中
    if agent_name == "PlanAgent" or agent_name == "PLAN":
        try:
            from agents.plan_agent import PlanAgent
            return getattr(PlanAgent, "context_spec", _CS())
        except ImportError:
            return _CS()

    # 从 AgentFactory 注册表查找
    try:
        from core.agent_factory import AgentFactory

        at = agent_type if isinstance(agent_type, AgentType) else None
        if at is not None:
            if at in AgentFactory._MODULE_MAP:
                agent_cls = AgentFactory._resolve(at)
        else:
            for enum_val in AgentFactory._MODULE_MAP:
                if enum_val.value == agent_name or enum_val.value + "Agent" == agent_name:
                    agent_cls = AgentFactory._resolve(enum_val)
                    break
    except ImportError:
        pass

    if agent_cls is not None:
        return getattr(agent_cls, "context_spec", _CS())
    return _CS()