
# AGENTS.md

This file provides guidance to AI coding agents when working with code in this repository.

## Project Overview

BI data analysis agent built on the AutoGen SDK (`autogen-agentchat`, `autogen-ext`).
Uses `uv` for dependency management and virtual environment. Python 3.12+.

## Build & Run Commands

```bash
# Install dependencies (sync venv)
uv sync

# Add a runtime dependency
uv add <package>

# Add a dev dependency
uv add --dev <package>

# Run the application (interactive mode)
uv run python main.py

# Run in single-task mode (pass query as CLI args)
uv run python main.py 查询本月生产产出数据

# Run as FastAPI server
uv run python main.py --serve
```

## Lint / Format / Typecheck

```bash
uv run ruff check . tests           # Lint
uv run ruff check --fix . tests     # Lint and auto-fix
uv run ruff format --check . tests  # Format check
uv run ruff format . tests          # Format apply
uv run mypy .                       # Type check
```

## Test Commands

```bash
uv run pytest                         # Run all tests
uv run pytest -v                      # Verbose
uv run pytest tests/test_team.py      # Single file
uv run pytest tests/test_team.py::test_agent_creation  # Single function
uv run pytest -k "test_agent"         # Match keyword
uv run pytest --cov                   # With coverage
```

## Architecture

### Three-Layer Architecture

```
BITeam (core/team.py) — 薄门面
  └── DispatchLayer (core/dispatch.py) — 调度层：会话生命周期、模型客户端
        ├── RoutingLayer (core/routing_layer.py) — 路由层：3层路由 + 中间件
        │     └── BIRouter (core/router.py) — 确定性规则 → ES召回 → LLM分类
        └── AgentLayer (core/agent_layer.py) — Agent层：工厂、DAG构建、执行
              ├── AgentFactory — 统一创建8种Agent
              ├── EventTranslator — AutoGen事件 → StreamEvent
              └── GraphFlow — DAG并行执行
```

### Directory Structure

```
core/                # 三层核心编排
  team.py            # BITeam facade
  dispatch.py        # DispatchLayer + create_model_client
  agent_layer.py     # AgentLayer — 规划、DAG构建、执行
  agent_factory.py   # AgentFactory — 惰性注册表 + importlib 延迟加载
  event_translator.py # EventTranslator — AutoGen事件 → StreamEvent
  table_formatter.py # DataFrame → TABLE 事件转换（numpy净化+列截断）
  db_helpers.py      # DB记录字典构建（agent/plan记录 + attach辅助）
  routing_layer.py   # RoutingLayer + RoutingMiddleware
  router.py          # BIRouter (3层路由)
  context.py         # SessionContext + TaskContext
  data_context.py    # DataContext (共享DataFrame存储，带锁+淘汰+摘要)

observability/       # 可观测性
  logging_client.py  # LoggingChatCompletionClient + ContextVar
  trace.py           # TraceRecorder

agents/              # Agent 定义 (BIBaseAgent 子类)
  base.py            # BIBaseAgent 基类 + 生命周期钩子
  plan_agent.py      # PlanAgent — DAG规划
  api_agent.py       # APIAgent — 外部API调用
  pyfunc_agent.py    # PyFuncAgent — Python代码执行
  data_analysis_agent.py  # DataAnalysisAgent — 6种ML工具
  sql_agent.py       # SQLAgent — SQL查询
  rag_agent.py       # RAGAgent — 知识检索
  visualization_agent.py  # VisualizationAgent — 图表/仪表盘
  report_agent.py    # ReportAgent — 报告生成

models/              # Pydantic 数据模型
  chat_request.py    # ChatRequest + UserInfo + BusinessInfo
  stream_event.py    # StreamEvent + StreamEventType (13种SSE事件)
  dag_plan.py        # DAGPlan + TaskNode
  routing.py         # RoutingResult + AgentType + ExecutionMode
  plan_output.py     # PlanStep
  report_content.py  # ReportContent + ReportSection
  chart_artifact.py  # ChartArtifact
  api_schema.py      # APIQueryParams + APIQueryResult

tools/               # Agent 工具 (async 函数)
  api_query.py       # DynamicAPITool + api_query
  python_exec.py     # python_exec (exec沙箱)
  sql_query.py       # sql_query (sqlparse验证 + asyncpg)
  report_generate.py # report_generate (word/ppt/html/pdf)
  ppt_renderer.py    # PPT渲染管线 (Jinja2 + python-pptx)
  chart_generate.py  # chart_generate (pyecharts)
  dashboard_generate.py # dashboard_generate
  search_tools.py    # KnowledgeSearch + W3Search + XiaoYiSearch
  data_ingest.py     # data_ingest
  data_clean.py      # data_clean
  data_summarize.py  # data_summarize
  time_series_forecast.py  # time_series_forecast (ARIMA/Holt-Winters)
  anomaly_detect.py  # anomaly_detect (IsolationForest/Z-Score/IQR)
  general_predict.py # general_predict (RF/GBM/Linear)
  get_es_data.py     # ES上下文发现 (API + 知识)

config/              # 配置
  settings.py        # Settings (pydantic-settings, BI_ 前缀)
  prompts.py         # 所有Agent系统提示词
  exceptions.py      # 异常层级 (BIAgentError 基类)

gateway/             # HTTP 网关
  app.py             # FastAPI应用工厂 + 路由
  adapter.py         # GatewayAdapter + WebAdapter + WeLinkAdapter
  middleware.py      # RequestLogMiddleware + RateLimitMiddleware

db/                  # 数据库
  pool.py            # asyncpg连接池
  writer.py          # 异步DB写入器 (Queue + batch INSERT)
  metrics.py         # 监控查询函数
  schema.sql         # DDL (8表)

utils/               # 工具函数
  es_query.py        # Elasticsearch 7.10.2 查询
  common_auth.py     # 华为IAM认证

templates/           # 模板文件
  ppt/               # PPT Jinja2模板 + CSS主题
  ppt_node/          # PPT原生节点模板

tests/               # 测试
docs/                # 文档
```

### Agent Flow

```
User Query → BIRouter → RoutingResult
  ├── single_agent → AgentLayer.run_single_agent()
  └── full_team → PlanAgent → DAGPlan → GraphFlow
        ├── APIAgent → DataContext
        ├── PyFuncAgent → DataContext
        ├── SQLAgent → DataContext
        ├── DataAnalysisAgent → DataContext
        ├── RAGAgent → DataContext
        ├── VisualizationAgent → ChartArtifact
        └── ReportAgent → word/ppt/html/pdf
```

### DataContext — Shared State

`DataContext` (`core/data_context.py`) is the shared data store between agents.
It holds `dict[str, pd.DataFrame]` and is injected into tools at runtime.
Keys are auto-generated via `generate_key(agent_name, task_id)`.

### StreamEvent — SSE Protocol

13-type event protocol defined in `models/stream_event.py`:
SESSION_START/END, PLAN_START/COMPLETE, AGENT_START/END, TOOL_CALL/RESULT,
LLM_CHUNK, THINK_CHUNK, DATA_STORED, TABLE, ERROR

### Configuration

`Settings` (`config/settings.py`) uses `pydantic-settings` with `BI_` env prefix.
All config is overridable via environment variables or `.env` file.

## Code Style Guidelines

### Imports
- Import 顺序必须严格遵守：**标准库 → 第三方包 → 项目自定义包**，三组之间空一行分隔
- Use absolute imports: `from core.team import BITeam`, `from observability.trace import TraceRecorder`
- Avoid wildcard imports
- **禁止在函数内部导包** — 所有 import 必须在模块顶层声明。唯一例外：避免循环导入的 lazy import，但必须在文件头部用注释说明原因

```python
# 标准库
import asyncio
import logging
from typing import Any

# 第三方包
import httpx
from pydantic import BaseModel

# 项目自定义包
from core.context import SessionContext
from observability.trace import TraceRecorder
```

### Function Design

- **原子化函数逻辑（区分通用方法与功能方法）** — 通用工具方法（utils、helpers）必须原子化，一个函数只做一件事；功能方法以业务目的为导向，允许编排多个步骤（如"获取数据 + 转换 + 存储"），但每个步骤必须委托给独立的子函数调用，编排函数本身只做流程串联
- **单个函数最大 60 行** — 超过 60 行的函数必须拆分。计数不含空行和注释，但含 docstring
- **禁止嵌套函数** — 非必要不在函数内部定义函数。如果需要复用逻辑，提取为模块级私有函数（`_` 前缀）。唯一例外：闭包捕获特定上下文的工厂函数，但必须在 docstring 中说明为何不能提取
- **函数参数不超过 5 个** — 超过时用 Pydantic model 或 dataclass 封装

### Comments & Docstring

- **所有公开函数和类必须有 docstring** — 格式遵循 Google Style：
  ```python
  def run_agent(self, agent_type: AgentType, task: str) -> StreamEvent:
      """运行指定类型的 Agent 并流式返回事件。

      Args:
          agent_type: Agent 类型枚举值。
          task: 用户任务描述。

      Returns:
          异步生成器，yield StreamEvent 事件。

      Raises:
          BIAgentError: Agent 执行失败时抛出。
      """
  ```
- **私有函数（`_` 前缀）至少有单行 docstring** — 说明做什么，不需要详细参数说明
- **关键业务逻辑必须加行内注释** — 解释"为什么"而非"做什么"（代码本身已说明做什么）
- **禁止无意义注释** — 如 `# 初始化变量`、`# 返回结果`，删除它们
- **TODO 注释必须带日期和负责人** — `# TODO(x00922253, 2026-07-28): 优化查询性能`

### Module Responsibility

- **模块承重检测** — 当一个模块出现以下信号时，必须考虑职责分离：
  - 单文件超过 400 行（不含空行和注释）
  - 一个类有超过 10 个公开方法
  - 模块内存在多个 `# --- xxx ---` 风格的分区注释（说明已承载多个职责）
  - import 了不属于同一职责域的依赖
- **职责分离方式** — 优先水平拆分（同层拆为多个模块），而非垂直拆分（抽基类/混入）
- **拆分后模块命名** — 从原模块名派生，保持可追溯：`dispatch.py` → `dispatch.py`（核心）+ `dispatch_helpers.py`（辅助）
- **拆分前先讨论** — 模块拆分影响面大，必须与用户确认拆分方案后再动手

### Formatting
- Line length: 120 characters
- Quote style: double quotes (`"`)
- Indent: 4 spaces
- Trailing commas in multi-line collections
- Use `ruff format` — do not format manually

### Type Annotations
- All function signatures must have full type annotations (strict mypy)
- Use `Pydantic` models for structured data, not raw dicts
- Use `async def` for agent/tool functions that call LLM or I/O
- Return types must be explicit; avoid `Any` unless unavoidable
- Use `|` union syntax (Python 3.12+): `str | None` not `Optional[str]`

### Naming Conventions
- Modules: `snake_case` (`data_query.py`)
- Classes: `PascalCase` (`DataAnalystAgent`, `QueryResult`)
- Functions/methods: `snake_case` (`analyze_data`, `build_chart`)
- Constants: `UPPER_SNAKE_CASE` (`MAX_RETRIES`, `DEFAULT_MODEL`)
- Private members: prefix with `_` (`_internal_state`)
- Pydantic models: suffix with context (`QueryRequest`, `AnalysisResult`)

### Error Handling
- Use custom exception classes in `config.exceptions` (hierarchy: `BIAgentError` base)
- Raise specific exceptions, never bare `raise Exception`
- Agent tools should return structured error results, not raise to the caller
- Use `logging` module, never `print()` for production code

### AutoGen-Specific Conventions
- Agents are `BIBaseAgent` subclasses with `on_before_turn`/`on_after_turn` hooks
- Keep agent system prompts in `config/prompts.py`, not inline strings
- Use `autogen-ext` `OpenAIChatCompletionClient` for LLM provider integration
- All agent interactions are async — use `await` properly
- PlanAgent uses `output_content_type=DAGPlan` for structured output

### Testing Conventions
- Test files: `test_<module>.py` in `tests/`
- Test classes: `Test<Feature>` with methods `test_<behavior>`
- Use `pytest-asyncio` for async tests (auto mode enabled in pyproject.toml)
- Use `pytest.fixture` for shared setup (mock clients, sample data)
- Mock LLM calls in tests — never hit real API endpoints
- One assertion per test when possible; descriptive test names

### Configuration
- Environment variables loaded via `pydantic-settings` with `BI_` prefix
- Config classes go in `config/`
- All configurable values should have sensible defaults

## Architecture Design Principles

> **核心规则：当任何实现决定与以下原则冲突时，必须立刻暂停并与用户讨论方案细节，说明改动后的风险。不得自行绕过原则打补丁。**

### P1. 分层职责不可越界

| 层 | 职责 | 禁止 |
|---|---|---|
| **core/** | 编排、路由、调度、生命周期、DataContext 共享状态 | 禁止包含业务逻辑；禁止直接操作 DataContext 的 DataFrame 内容做数据转换 |
| **agents/** | Agent 行为定义、工具编排、prompt 管理 | 禁止跨 Agent 直接调用；禁止修改 core/ 框架代码 |
| **tools/** | 纯 I/O 执行（SQL/API/图表/报告） | 禁止调用 LLM；禁止包含业务判断 |
| **models/** | 纯数据结构定义 | 禁止包含行为逻辑；禁止 import agents/ 或 tools/ |
| **gateway/** | HTTP 接入、协议转换 | 禁止包含业务逻辑；禁止直接操作 Agent |

**原则：每层只做自己该做的事。** 如果一个改动需要跨层越界，说明设计有问题，需要先讨论再动手。

### P2. 新增功能必须符合开闭原则

- **应该：** 新 Agent 通过 `BIBaseAgent` 子类 + `ContextSpec` 自声明接入，框架无需改动
- **应该：** 新工具作为独立 `async` 函数，Agent 通过 `FunctionTool` 注册
- **禁止：** 新增 Agent 时修改 `core/` 下的任何框架代码（AgentFactory、AgentLayer、DispatchLayer）
- **禁止：** 为了支持新功能在基类中加 `if agent_type == "xxx"` 分支
- **禁止：** 在 `agents/__init__.py` 中做集中导出（会导致循环导入）

> 如果新功能必须改框架，先讨论：是功能设计不合理，还是框架扩展性不足？

### P3. 补丁思维零容忍

| 场景 | 正确做法 | 禁止做法 |
|---|---|---|
| 修复 bug | 找到根因，修正源头逻辑 | `try/except pass` 吞掉异常 |
| 新增字段 | 在对应 Pydantic model 中声明 | 在函数间传 raw dict |
| 跨 Agent 传数据 | 通过 DataContext 共享 | 全局变量 / ContextVar 传业务数据 |
| 绕过已有逻辑 | 理解为什么有这层逻辑，必要时重构 | 直接跳过（如 PyFuncAgent 绕过 BIBaseAgent） |
| 兼容旧接口 | 修改调用方适配新接口 | 加 `**kwargs` 吞掉未知参数 |
| 处理边界情况 | 在正确的层级做校验 | 在每层都加重复防御代码 |

**原则：每个补丁都是未来的债。** 如果修一个问题需要"绕过"现有机制，说明现有机制可能需要调整，而不是绕过它。

### P4. 改动范围最小化

- **应该：** 每次改动聚焦一个关注点，影响 3-5 个文件以内
- **应该：** 按逻辑变更点拆分 commit（参考：一个 commit = 一个可独立 review 的变更）
- **应该：** 大改动先写 Plan，经审核后再实施
- **禁止：** 一次对话改 10+ 文件（除非是全局重命名等机械变更）
- **禁止：** 顺手"改进"与本次任务无关的代码
- **禁止：** 在修复 A 的同时重构 B（混在一起无法回滚）

### P5. 数据流必须可追踪

- **应该：** 数据流路径：User → Router → Agent → Tool → DataContext → 下游 Agent
- **应该：** 工具通过闭包捕获 DataContext，执行结果直接写入 DataContext 并返回摘要字符串供 LLM 消费
- **应该：** 通过 `data_context.generate_key()` 生成唯一 key，不硬编码
- **禁止：** 在 EventTranslator 中做数据内容过滤或业务判断
- **禁止：** 跨 Session 共享 DataContext（隔离原则）

### P6. 提示词与代码分离

- **应该：** Prompt 存放在 `config/prompts/` 下的 Jinja2 模板中
- **应该：** 使用 `__PLACEHOLDER__` + `str.replace()` 处理模板变量（避免与 JSON `{}` 冲突）
- **禁止：** 在 Python 代码中内联长字符串 prompt
- **禁止：** 使用 `str.format()` 渲染含 JSON 示例的模板
- **禁止：** 在 prompt 中硬编码 API schema（应从 ES 动态获取）

### P7. 异步与并发安全

- **应该：** 所有 I/O 操作使用 `async def`
- **应该：** ContextVar 在 `finally` 块中清理，确保所有代码路径（含 early return / cache hit）都能清理
- **应该：** 共享资源用 `asyncio.Lock` 保护
- **禁止：** 在 async 函数中调用阻塞 I/O 而不使用 `run_in_executor`
- **禁止：** 用 `asyncio.wait_for()` 包裹 async generator（用 `asyncio.timeout()` 上下文管理器）

### P8. 测试必须验证架构不变量

- **应该：** 为架构约束写断言测试（如：无 God Method、无循环导入）
- **应该：** Mock LLM 调用，不命中真实 API
- **应该：** 测试 `on_messages_stream` 时 patch 方法本身，不 mock `model_client`
- **禁止：** 为了让测试通过而修改生产代码逻辑
- **禁止：** 用 `try/except` 吞掉测试失败

### P9. 安全底线

- **应该：** SSRF 防御必须 DNS 解析 + `ipaddress.ip_address()` 校验
- **应该：** SQL 注入防御使用 `sqlparse` 验证 + 参数化查询
- **应该：** 代码沙箱阻止危险 import 和 builtins
- **禁止：** 字符串前缀匹配做 IP 白名单（IPv6/十六进制/八进制可绕过）
- **禁止：** 将用户输入直接拼入 SQL / shell 命令 / exec()

### 冲突处理流程

当实现决定与以上原则冲突时：

1. **暂停实现** — 不要继续写代码
2. **向用户说明** — 哪条原则被违反、为什么需要违反、有哪些替代方案
3. **评估风险** — 明确说明违反后的影响范围和潜在后果
4. **等待决策** — 用户确认后才可继续

**绝不自行决定绕过原则。** 历史经验表明，每个"临时补丁"最终都会变成技术债。

## Key Dependencies

| Package | Purpose |
|---|---|
| `autogen-agentchat` | Multi-agent conversation framework (GraphFlow, BIBaseAgent) |
| `autogen-ext` | OpenAIChatCompletionClient model client |
| `pandas` | DataFrame-based data storage in DataContext |
| `httpx` | Async HTTP client for API tool |
| `openpyxl` | Excel file read/write |
| `pydantic-settings` | Configuration with env variable support |
| `tiktoken` | Token counting |
| `fastapi` | HTTP/SSE gateway |
| `asyncpg` | PostgreSQL async driver |