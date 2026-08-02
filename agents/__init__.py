
'''
agents 包 — 通过 AgentFactory 惰性加载，不做集中导出（遵循 P2 原则）。

新增 Agent 无需在此处登记，由 AgentFactory._MODULE_MAP + importlib.import_module
按需加载。集中导出会导致：
1. 任何 from agents.xxx import 都触发全量 Agent 加载，破坏 AgentFactory 的惰性设计
2. 循环导入风险（agents 子模块反向依赖 core 时易爆）
3. IDE 自动补全误导（建议 from agents import X 埋雷）

注：循环导入根因在于 core/__init__.py 集中导出 AgentFactory，而后者依赖 agents.base。
主入口 main.py 先加载 core.team 可绕过，但 from agents.base 直接调用会触发。
'''
