
# Agent 设计文档

> 本文档描述 `bi_autogen` 项目中 8 种 Agent 的设计，从 `BIBaseAgent` 基类、`ContextSpec` 自声明模式，到 `AgentFactory` 惰性注册表和 PlanAgent 的 `ask_user` 拦截机制。
>
> 阅读本文后，一位资深开发者应能够理解并复现每个 Agent 的设计。
>

---

## 目录

1. [BIBaseAgent 基类设计](#1-bibaseagent-基类设计)
2. [ContextSpec 自声明模式](#2-contextspec-自声明模式)
3. [8 个 Agent 详细设计](#3-8-个-agent-详细设计)
4. [AgentFactory 惰性注册表](#4-agentfactory-惰性注册表)
5. [Agent 生命周期](#5-agent-生命周期)
6. [PlanAgent ask_user 拦截机制](#6-planagent-ask_user-拦截机制)

---

## 1. BIBaseAgent 基类设计

> 源码：`agents/base.py`

### 1.1 类继承关系

```
AssistantAgent (autogen_agentchat.agents)
    └── BIBaseAgent (agents/base.py)
            ├── APIAgent / SQLAgent / PyFuncAgent / DataAnalysisAgent
            ├── VisualizationAgent / ReportAgent / RAGAgent
            └── PlanAgent       # 进一步覆写 on_messages_stream
```

`BIBaseAgent` 继承自 AutoGen 的 `AssistantAgent`，**通过组合 + 生命周期钩子**扩展而非重新实现 agent 运行循环。AutoGen 的 `AssistantAgent` 内部方法（如 `_assistant_agent_base_model_process_stream`）是 `@classmethod`，无法被子类覆写，因此只能从两个入口介入：

- `on_messages`：完整调用入口
- `on_messages_stream`：流式生成器入口（事件流）

### 1.2 类变量

类变量由子类覆写，基类提供默认值：

| 类变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `context_spec` | `ClassVar[ContextSpec]` | `ContextSpec()` | 多轮上下文需求声明（默认保守策略 last + schema + own） |
| `thinking_enabled` | `ClassVar[bool]` | `True` | 思考分层标志。`True`=深度思考（规划/代码/报告），`False`=浅层执行（选 API/写 SQL/搜索/选图表）。在 `on_messages`/`on_messages_stream` 入口写入 `ContextVar`，`LoggingChatCompletionClient` 据此覆盖 `extra_body.chat_template_kwargs.enable_thinking` |
| `prompt_template` | `ClassVar[str]` | `""` | prompt 模板名（如 `"api_agent"`）。基类自动渲染 `system_message`。为空时跳过自动渲染（如 `PlanAgent` 自行管理 prompt） |
| `agent_description` | `ClassVar[str]` | `""` | Agent 描述，用于 `description` 参数和 prompt 的 `AGENT_ROLE` 占位 |

类变量定义见 `agents/base.py:84-96`。

### 1.3 构造函数 `__init__`

签名（`agents/base.py:98-114`）：

```python
def __init__(
    self,
    model_client: ChatCompletionClient,
    data_context: DataContext | None = None,
    session: SessionContext | None = None,
    task_id: str | None = None,
    task_description: str | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    system_message: str | None = None,
    tools: list[Any] | None = None,
    output_content_type: type | None = None,
    reflect_on_tool_use: bool = False,
    max_tool_iterations: int = 1,
    global_tools: list[Any] | None = None,
) -> None:
```

构造逻辑：

1. **保存运行时上下文**（行 115-122）：
   - `data_context` / `session` / `task_id` / `task_description`
   - `_depends_on: list[str]`：DAG 上游依赖（由 `AgentFactory.create_from_task_node` 设置）
   - `_failed: bool`：本任务是否失败（供 `run_team` 集中检测）

2. **自动生成 name 与 description**（行 125-127）：
   - `name` 默认通过 `make_agent_name(agent_type, task_id)` 生成——有 `task_id` 时格式为 `"APIAgent_task_1"`，否则为 `"APIAgent"`
   - `description` 默认回退到 `agent_description`，再回退到 `"{AgentType}助手"`

3. **自动渲染 system_message**（行 130-133）：
   - 若子类声明了 `prompt_template` 且未显式传入 `system_message`，调用 `_render_prompt(name)` 渲染
   - 否则 `system_message = ""`

4. **合并工具**（行 136-141）：全局工具在前，agent 专属工具在后；同名专属工具覆盖全局（去重由 AutoGen 处理）

5. **委托 `super().__init__()`**（行 141-151）：将参数透传给 `AssistantAgent`，`model_client_stream=True` 强制流式

### 1.4 prompt 自动渲染

```python
def _render_prompt(self, agent_name: str) -> str:
    """渲染 prompt 模板，注入公共变量 + 子类额外变量。"""
    from config.prompt_manager import get_prompt_manager
    today = datetime.now().strftime("%Y年%m月%d日")
    vars_dict: dict[str, str] = {
        "AGENT_NAME": agent_name,
        "AGENT_ROLE": self.agent_description or type(self).__name__,
        "DATE": today,
    }
    vars_dict.update(self._extra_prompt_vars())
    return get_prompt_manager().render(self.prompt_template, **vars_dict)
```

- **公共变量**：`AGENT_NAME`、`AGENT_ROLE`、`DATE`
- **扩展点**：子类覆写 `_extra_prompt_vars()`（默认返回空 dict）注入业务变量，如 `SKILLS`、`DATA_CONTEXT`、`TABLE_INFO`、`CHART_CONTEXT`
- **惰性 import**：`get_prompt_manager` 在函数内导入，避免顶层循环依赖（`config/prompt_manager.py` 会反向导入 `agents/`）

### 1.5 on_messages 入口

```python
async def on_messages(
    self,
    messages: Sequence[BaseChatMessage],
    cancellation_token: CancellationToken,
) -> Response:
    from observability.logging_client import set_current_agent, set_enable_thinking
    set_current_agent(self.name)
    set_enable_thinking(self.thinking_enabled)
    return await super().on_messages(messages, cancellation_token)
```

（`agents/base.py:170-185`）

**作用**：在执行前将 agent 名和思考模式写入 `ContextVar`。

- `set_current_agent(self.name)` 解决首轮 LLM 调用时无 `AssistantMessage` 导致日志中 `agent="?"` 的问题
- `set_enable_thinking(self.thinking_enabled)` 让 `LoggingChatCompletionClient` 根据 `thinking_enabled` 覆盖 `extra_body.chat_template_kwargs.enable_thinking`（Qwen3 思考模式开关）

任何子类（包括覆写 `on_messages_stream` 的 `PlanAgent`）只要经由 `on_messages` 入口，都会先经过此方法。

### 1.6 on_messages_stream 入口

签名（`agents/base.py:187-191`）：

```python
async def on_messages_stream(
    self,
    messages: Sequence[BaseChatMessage],
    cancellation_token: CancellationToken,
) -> AsyncGenerator[BaseAgentEvent | BaseChatMessage | Response, None]:
```

执行步骤（行 192-275）：

#### 步骤 1：跳过失败依赖任务

```python
session = getattr(self, "session", None)
if session and self._depends_on:
    failed = getattr(session, "_failed_task_ids", set())
    if any(dep in failed for dep in self._depends_on):
        skip_content = f"任务跳过：上游依赖任务 {self._depends_on} 失败"
        skip_msg = TextMessage(content=skip_content, source=self.name)
        yield skip_msg
        yield Response(chat_message=skip_msg)
        # 标记自身为失败（传播跳过到更下游）
        session._failed_task_ids.add(self.task_id or "")
        self._failed = True
        return
```

如果上游任务已失败（在 `session._failed_task_ids` 中），本任务直接生成"任务跳过"消息并标记自身失败，**让跳过沿着 DAG 传播到更下游**。这是 DAG 失败传播的核心机制，由 `_FAILURE_KEYWORDS` 中的 `"任务跳过"` 关键字确保实现（见 1.8）。

#### 步骤 2：注入任务描述

如果设置了 `task_description`，将 `messages` 中最后一条 `source="user"` 的消息替换为 `task_description`：

```python
if self.task_description:
    last_user_idx = -1
    for i in range(len(messages) - 1, -1, -1):
        if getattr(messages[i], "source", None) == "user":
            last_user_idx = i
            break
    if last_user_idx >= 0:
        modified = list(messages)
        modified[last_user_idx] = TextMessage(content=self.task_description, source="user")
        messages = modified
```

这样 DAG 场景下每个 agent 收到的都是 PlanAgent 给它的专属任务描述，而非原始用户查询。

#### 步骤 3：调用前置钩子 `on_before_turn`

```python
await self.on_before_turn(messages)
```

子类可覆写以注入多轮对话历史（见 1.7）。

#### 步骤 4：再次设置 `ContextVar`

```python
set_current_agent(self.name)
set_enable_thinking(self.thinking_enabled)
```

防止子类直接调用 `on_messages_stream`（绕过 `on_messages`）时 `ContextVar` 没有被正确设置。

#### 步骤 5：拦截事件流并缓冲工具调用

```python
pending_tool_calls: dict[str, dict[str, Any]] = {}  # call_id -> {name, arguments}
recorder = get_trace_recorder()
final_response: Response | None = None
async for event in super().on_messages_stream(messages, cancellation_token):
    if isinstance(event, Response):
        final_response = event
        break

    # 拦截工具调用请求
    if isinstance(event, ToolCallRequestEvent) and recorder is not None:
        for tc in event.content:
            try:
                args = json.loads(tc.arguments) if isinstance(tc.arguments, str) else tc.arguments
            except (json.JSONDecodeError, TypeError):
                args = tc.arguments
            pending_tool_calls[tc.id] = {"name": tc.name, "arguments": args}

    # 拦截工具调用结果，与请求配对后写入 trace + DB
    if isinstance(event, ToolCallExecutionEvent):
        for r in event.content:
            call_info = pending_tool_calls.pop(r.call_id, None)
            if call_info is not None:
                tool_name = call_info["name"]
                display_name = tool_name.lstrip("_")  # 去掉前缀下划线
                result_text = r.content[:3000] if len(r.content) > 3000 else r.content
                is_error = getattr(r, "is_error", False)
                await BIBaseAgent._record_tool_call(
                    agent_name=self.name,
                    task_id=self.task_id or "",
                    tool_name=display_name,
                    call_id=r.call_id,
                    arguments=call_info["arguments"],
                    result=result_text,
                    is_error=is_error,
                    recorder=recorder,
                )

    yield event
```

**关键设计**：

- 用一个 `dict[call_id, {name, arguments}]` 缓冲 `ToolCallRequestEvent`，等 `ToolCallExecutionEvent` 配对后再写入 trace + DB——这样 trace 记录既有调用参数又有返回结果
- `display_name = tool_name.lstrip("_")` 把内部方法名前缀的下划线去掉，记录用户可见的工具名
- `result_text[:3000]` 截断过长结果，防止单条记录过大
- 静态方法 `_record_tool_call` 同时被 `PyFuncAgent` 直接复用（行 277-317）

#### 步骤 6：调用后置钩子 `on_after_turn`

```python
if final_response is not None:
    processed = await self.on_after_turn(final_response, messages)
    yield processed or final_response
```

子类返回 `None` 则保持原 `Response`，返回 `Response` 则替换输出。`PlanAgent` 用此钩子将 `DAGPlan` 写入 `TraceRecorder`（见 6.4）。

### 1.7 多轮历史注入 on_before_turn

```python
async def on_before_turn(self, messages: Sequence[BaseChatMessage]) -> None:
    ...
    # 提取 agent 类型名（在 AGENT_TYPES 中匹配）
    agent_type = ""
    for at in AGENT_TYPES:
        if self.name.startswith(at):
            agent_type = at
            break
    if not agent_type:
        return

    # 按消息规范构建历史轮次
    history_messages = self._build_history_messages(ctx, agent_type)
    if not history_messages:
        return

    # 将历史消息插入到 messages 列表前面（在最后一条 user 之前）
    if isinstance(messages, list):
        last_user_idx = ...  # 找最后一条 user
        if last_user_idx >= 0:
            for j, hist_msg in enumerate(history_messages):
                messages.insert(last_user_idx + j, hist_msg)
```

（`agents/base.py:319-364`）

**LLM messages 规范**：历史轮次以 `user/assistant` 消息对插入 messages 列表，而非把历史拼接到当前 user 消息文本里。格式：

```
[system, ...历史轮次(user→assistant), 当前user消息]
```

每个历史轮次生成一对消息：

- `user`：历史查询 `turn.query`
- `assistant`：该 Agent 在该轮可见的上下文摘要（结论 + 数据 + 规划提示）

注意：`PlanAgent` 覆写了此方法，把历史消息插入到 messages 列表开头（`messages.insert(0, msg)`），而不是当前 user 之前——因为 PlanAgent 的 `on_messages_stream` 不复用 `BIBaseAgent.on_messages_stream`，需要自行控制注入位置。

### 1.8 `_build_history_messages` 解析 ContextSpec

```python
@staticmethod
def _build_history_messages(ctx: ConversationContext, agent_type: str) -> list[BaseChatMessage]:
    from core.spec_resolver import resolve_context_spec
    spec = resolve_context_spec(agent_type)
    history: list[BaseChatMessage] = []
    for turn in ctx.turns:
        history.append(TextMessage(content=turn.query, source="user"))
        parts: list[str] = []
        # 结论摘要
        if spec.conclusions in ("all", "own"):
            conclusions = []
            for c in turn.agent_conclusions:
                if spec.conclusions == "own" and agent_type not in c.agent_name:
                    continue
                ...
            if conclusions:
                parts.append("## 执行结论\n" + "\n".join(conclusions))
        # 数据目录
        if spec.data_catalog in ("full", "schema"):
            ...
        # DAG 历史
        if spec.dag_history and turn.dag_tasks:
            ...
        # 规划提示
        if spec.planning_hints:
            parts.append("规划原则：已有数据/结论直接引用，不重复获取；仅规划新增任务；依赖标注数据来源key")
        assistant_content = "\n\n".join(parts) if parts else "（无输出）"
        history.append(TextMessage(content=assistant_content, source="assistant"))
    return history
```

（`agents/base.py:366-447`）

每个 `ContextSpec` 字段决定该轮 `assistant` 摘要里会拼接哪些内容——详见 §2。

### 1.9 `looks_like_failure` 失败检测

```python
_FAILURE_KEYWORDS = ("执行失败", "调用失败", "查询失败", "生成失败", "DAG生成失败", "报告生成失败", "任务跳过")
_SUCCESS_KEYWORDS = ("成功", "完成", "已存入")

def looks_like_failure(content: str) -> bool:
    if not content or len(content) < 5:
        return True
    has_failure = any(kw in content for kw in _FAILURE_KEYWORDS)
    has_success = any(kw in content for kw in _SUCCESS_KEYWORDS)
    return has_failure and not has_success
```

（`agents/base.py:54-67`）

**判定逻辑**：

- 内容少于 5 个字符 → 失败
- 含失败关键字 **且** 不含成功关键字 → 失败
- `"任务跳过"` 在失败关键字中，确保 DAG 中跳过的任务能继续传播到下游（步骤 1 的 `session._failed_task_ids.add(self.task_id)`）

调用方：`core/agent_layer.py:409` `_detect_failure_and_emit_tables` 中对每个 `AGENT_END` 调用，标记 `session_ctx._failed_task_ids.add(stream_ev.task_id)`。

### 1.10 `AGENT_TYPES` frozenset

```python
AGENT_TYPES = {"APIAgent", "PyFuncAgent", "ReportAgent", "SQLAgent",
               "DataAnalysisAgent", "VisualizationAgent", "RAGAgent"}
```

（`agents/base.py:29`）

**注意：不包含 `"PlanAgent"`**——`PlanAgent` 是规划 Agent，不走 DAG 执行，由 `AgentLayer.run_team` 直接创建。`on_before_turn` 中提取 agent_type 时若匹配不到则直接返回（注入历史跳过）。同样，`AgentFactory._MODULE_MAP` 中也没有 PlanAgent，它由 `AgentLayer._plan` 直接 `PlanAgent(...)` 构造。

---

## 2. ContextSpec 自声明模式

### 2.1 ContextSpec 模型

> 源码：`agents/base.py:32-43`

```python
class ContextSpec(BaseModel):
    """Agent 的上下文需求声明——Agent 自己定义，ConversationContext 消费。

    不声明则走默认值（保守策略：last + schema + own），不会泄露不该看的信息。
    """

    query_history: str = "last"      # none / last / full
    data_catalog: str = "schema"     # none / schema / full
    conclusions: str = "own"         # none / own / all
    dag_history: bool = False
    tool_summary: bool = False
    planning_hints: bool = False
```

字段语义：

| 字段 | 取值 | 含义 |
|---|---|---|
| `query_history` | `none` / `last` / `full` | 是否注入历史用户查询。`last`=只看上一轮，`full`=看全部历史 |
| `data_catalog` | `none` / `schema` / `full` | 历史数据目录可见度。`schema` 只展示可用数据的 schema，`full` 还展示不可用数据 |
| `conclusions` | `none` / `own` / `all` | 历史结论可见度。`own`=只看自己产出的结论，`all`=看所有 agent 的结论 |
| `dag_history` | `bool` | 是否注入历史 DAG 执行信息 |
| `tool_summary` | `bool` | 是否注入历史工具调用摘要 |
| `planning_hints` | `bool` | 是否注入"规划原则"提示（已有数据直接引用，不重复获取） |

### 2.2 默认保守策略

`BIBaseAgent.context_spec = ContextSpec()`，即默认 `last + schema + own`——只看上一轮、只看可用数据 schema、只看自己产出的结论。这是**安全默认**：

- 默认不泄露其他 agent 的执行细节
- 保守地提供最小化上下文，避免 token 浪费和噪声

子类按职责**显式放宽**——例如 `PlanAgent` 拉最全上下文（要全局视角），`RAGAgent` 完全关闭数据目录（搜索不需要看结构化数据）。

### 2.3 resolve_context_spec 解析

> 源码：`core/spec_resolver.py`

```python
def resolve_context_spec(agent_type: AgentType | str) -> ContextSpec:
    """从 Agent 类上读取 context_spec，新增 Agent 无需改这里。"""
    from agents.base import BIBaseAgent, ContextSpec as _CS

    agent_name = agent_type.value if isinstance(agent_type, AgentType) else str(agent_type)

    # PlanAgent 不在 AgentFactory 注册表
    if agent_name in ("PlanAgent", "PLAN"):
        from agents.plan_agent import PlanAgent
        return getattr(PlanAgent, "context_spec", _CS())

    # 从 AgentFactory 注册表查找
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

    if agent_cls is not None:
        return getattr(agent_cls, "context_spec", _CS())
    return _CS()
```

**关键设计**：

1. **自声明而非集中表**：直接从 Agent 类的 `context_spec` 类属性读取，新增 Agent **无需修改 `spec_resolver.py`**
2. **惰性 import**：`from agents.base import ...` 在函数体内，避免 `core → agents → core` 循环依赖
3. **PlanAgent 特例处理**：PlanAgent 不在 `AgentFactory._MODULE_MAP`，直接 import 解析
4. **支持两种入参**：`AgentType` 枚举 或 字符串名（兼容 `"API"` 和 `"APIAgent"`）

### 2.4 自声明工作流

```
Agent 子类定义时 → 设置 context_spec 类属性
        ↓
BIBaseAgent.on_before_turn 调用 _build_history_messages
        ↓
_build_history_messages 内部 resolve_context_spec(agent_type) 读取 spec
        ↓
按 spec 字段过滤 ctx.turns，构建历史消息对
        ↓
插入到 messages 列表传给 LLM
```

**好处**：

- Agent 自己声明"我需要看什么"，框架消费——职责清晰
- 新增 Agent 时只改自己的 `context_spec`，不会污染其他 Agent 的上下文（开闭原则 P2）
- 默认保守——忘记声明也不会泄露敏感信息

---

## 3. 8 个 Agent 详细设计

### 3.1 总览对比表

| Agent | 文件 | thinking_enabled | prompt_template | reflect_on_tool_use | max_tool_iterations | context_spec（差异） |
|---|---|---|---|---|---|---|
| **PlanAgent** | `agents/plan_agent.py` | `True`（默认） | （自行渲染） | - | - | full + full + all + dag + tool + hints |
| **APIAgent** | `agents/api_agent.py` | `False` | `api_agent` | `True` | `5` | 默认（last + schema + own） |
| **PyFuncAgent** | `agents/pyfunc_agent.py` | `True` | `pyfunc_agent` | `True` | `3` | `data_catalog="full"` |
| **SQLAgent** | `agents/sql_agent.py` | `False` | `sql_agent` | `True` | `1`（默认） | 默认 |
| **DataAnalysisAgent** | `agents/data_analysis_agent.py` | `True` | `data_analysis_agent` | `True` | `1`（默认） | 默认 |
| **RAGAgent** | `agents/rag_agent.py` | `False` | `rag_agent` | `True` | `5` | `query_history="full"`, `data_catalog="none"` |
| **VisualizationAgent** | `agents/visualization_agent.py` | `False` | `visualization_agent` | `True` | `1`（默认） | `data_catalog="full"`, `conclusions="all"` |
| **ReportAgent** | `agents/report_agent.py` | `True` | `report_agent` | `False`（默认） | `1`（默认） | `query_history="full"`, `conclusions="all"` |

> 未显式声明的字段全部走 `ContextSpec` 默认值（`last + schema + own`，`dag_history=tool_summary=planning_hints=False`）。

---

### 3.2 PlanAgent（任务规划专家）

> 源码：`agents/plan_agent.py`

```python
class PlanAgent(BIBaseAgent):
    """任务规划专家，一次性输出完整DAG任务列表和依赖关系。

    内置 ask_user FunctionTool：当 API 必填参数缺失时，LLM 调用 ask_user
    向用户提问。on_messages_stream 拦截 ToolCallRequestEvent，创建
    USER_QUESTION 事件，等待用户答复后通过 asyncio.Queue 将答复传给
    tool function，AutoGen 将结果喂回 LLM，LLM 最终输出 DAG 计划。
    """
```

#### 类配置

```python
context_spec: ClassVar[ContextSpec] = ContextSpec(
    query_history="full",
    data_catalog="full",
    conclusions="all",
    dag_history=True,
    tool_summary=True,
    planning_hints=True,
)
```

**全部拉满**——PlanAgent 需要全局视角才能避免重复规划（已有数据直接引用、不重新获取）。

#### 构造函数

```python
def __init__(
    self,
    model_client: ChatCompletionClient,
    session: SessionContext | None = None,
    *,
    translator: Any | None = None,
) -> None:
    self._session = session
    self._translator = translator
    self._final_content = ""
    self._dag_plan: DAGPlan | None = None

    # ask_user 基础设施
    self._ask_user_handler = AskUserHandler()
    self._answer_queue: asyncio.Queue[str] = asyncio.Queue()
    if session:
        ask_user_registry.register(session.session_id, self._ask_user_handler)

    # ask_user / dag 工具
    ask_user_tool = make_ask_user_tool_with_queue(self._answer_queue)
    dag_tool = make_dag_tool(lambda plan: setattr(self, "_dag_plan", plan))

    # 渲染 system_message（注入 API_LIST 和 SKILLS）
    today = datetime.now().strftime("%Y年%m月%d日")
    api_list_text = format_api_list_detailed(session.api_meta if session else [])
    skills_text = get_skill_manager().format_skills_for_prompt(session.skills if session else [])
    system_message = get_prompt_manager().render(
        "plan_agent",
        AGENT_NAME="PlanAgent",
        AGENT_ROLE="任务规划专家",
        DATE=today,
        API_LIST=api_list_text,
        SKILLS=skills_text,
    )
    super().__init__(
        name="PlanAgent",
        description="任务规划专家，负责分析用户需求并输出完整的DAG任务执行计划。",
        model_client=model_client,
        system_message=system_message,
        tools=[ask_user_tool, dag_tool],
    )
```

**关键点**：

- **自行管理 prompt**：不依赖基类 `prompt_template` 自动渲染，因为需要注入 `API_LIST` 和 `SKILLS` 两个动态变量
- **两个 FunctionTool**：
  - `ask_user_tool`：让 LLM 在 API 必填参数缺失时主动提问
  - `dag_tool`：让 LLM 提交结构化 DAG 任务列表，工具内构建并校验 `DAGPlan`
- **回调注入 plan**：`dag_tool` 通过 `lambda plan: setattr(self, "_dag_plan", plan)` 回调把 plan 存到实例属性，供 `on_after_turn` 和 `AgentLayer._plan` 读取
- **AskUserRegistry 注册**：通过 `session_id` 注册 handler，Gateway 收到用户答复时按 `session_id` 找到 handler 调用 `submit_answer`

#### 工具

| 工具名 | 实现位置 | 作用 |
|---|---|---|
| `ask_user` | `tools/ask_user.py` `make_ask_user_tool_with_queue` | 向用户提问，从 `answer_queue` 取答复 |
| `dag` | `tools/dag_tool.py` `make_dag_tool` | 提交任务列表，构造并校验 DAGPlan |

#### 特殊行为

**完全覆写 `on_messages_stream`**（不调用 `super().on_messages_stream`，而是调用 `AssistantAgent.on_messages_stream(self, ...)` 跳过 `BIBaseAgent` 的实现）：

- 拦截 `ask_user` tool call，将其翻译为 `USER_QUESTION` / `USER_ANSWER` 事件
- 详见 §6

**覆写 `on_before_turn`**：

```python
async def on_before_turn(self, messages: Sequence[BaseChatMessage]) -> None:
    conv_ctx = self._session.conversation_context if self._session else None
    if conv_ctx is not None and isinstance(messages, list):
        history = BIBaseAgent._build_history_messages(conv_ctx, "PlanAgent")
        for msg in history:
            messages.insert(0, msg)  # 注意：插入到开头
```

**覆写 `on_after_turn`**：记录 `DAGPlan` 到 `TraceRecorder`：

```python
async def on_after_turn(self, response: Response, messages: Sequence[BaseChatMessage]) -> Response | None:
    recorder = get_trace_recorder()
    if recorder is None:
        return None
    if self._dag_plan is not None:
        recorder.record_plan_output(self._dag_plan)
        return None
    # Fallback: 解析 response 文本为 JSON
    if response.chat_message and isinstance(response.chat_message, TextMessage):
        content = response.chat_message.content
        try:
            plan_data = json.loads(content)
            plan = DAGPlan.from_task_list(plan_data) if isinstance(plan_data, list) else None
            if plan is not None:
                recorder.record_plan_output(plan)
        except (json.JSONDecodeError, TypeError):
            ...
    return None
```

#### 设计依据

- **PlanAgent 是规划者，需要全局视角**：`context_spec` 全开
- **ask_user 必须发生在 LLM 流程中间**：所以覆写 `on_messages_stream`（详见 §6）
- **`dag` 工具优先于文本解析**：让 LLM 通过 function calling 输出结构化数据，比 JSON 文本解析更可靠；保留 `_final_content` 文本解析作为 fallback

---

### 3.3 APIAgent（API 调用专家）

> 源码：`agents/api_agent.py`

#### 类配置

```python
context_spec: ClassVar[ContextSpec] = ContextSpec()          # 默认 last + schema + own
thinking_enabled: ClassVar[bool] = False                     # 浅层执行
prompt_template: ClassVar[str] = "api_agent"
agent_description: ClassVar[str] = "API调用专家"
```

#### 构造函数

```python
def __init__(
    self,
    model_client: ChatCompletionClient,
    data_context: DataContext,
    session: SessionContext | None = None,
    task_id: str | None = None,
    task_description: str | None = None,
) -> None:
    api_meta = session.api_meta if session and session.api_meta else []
    custom_env = session.business.custom_env if session else None
    user_id = session.user.user_id if session else ""
    tools: list[DynamicAPITool] = [
        DynamicAPITool(meta, data_context, custom_env=custom_env, user_id=user_id)
        for meta in api_meta
    ]
    super().__init__(
        model_client=model_client,
        data_context=data_context,
        session=session,
        task_id=task_id,
        task_description=task_description,
        tools=tools,
        reflect_on_tool_use=True,
        max_tool_iterations=5,
    )
```

#### 工具注册

**每个 API 动态注册为独立的 `DynamicAPITool`**——LLM 通过 function calling 直接调用带完整参数 schema 的工具，而非通过泛化的 `call_api(name, params)` 工具。

- 输入：`session.api_meta`（从 ES 获取的 API 元数据列表）
- 每个 `meta` 生成一个 `DynamicAPITool`，自带参数 schema
- 注入 `custom_env` 和 `user_id` 用于鉴权和上下文

#### Prompt 扩展变量

```python
def _extra_prompt_vars(self) -> dict[str, str]:
    if self.session and self.session.skills:
        skills_text = get_skill_manager().format_skills_for_prompt(self.session.skills)
        return {"SKILLS": skills_text}
    return {"SKILLS": ""}
```

注入业务技能（如"仓库日均拆包指标计算规则"）到 prompt，告诉 LLM 该用户场景下可用的领域规则。

#### 设计依据

- **per-API 独立工具**：每个 API 有自己的 schema，LLM 看到完整的参数定义，避免"把 API 名和参数当字符串传"的幻觉
- **`thinking_enabled=False`**：选 API + 填参数是浅层判断，不需要深度思考
- **`reflect_on_tool_use=True, max_tool_iterations=5`**：API 失败时允许 LLM 自重试（如换参数、重试其他 API），最多 5 轮
- **`context_spec` 默认**：API Agent 不需要全局视角，看上一轮足够

---

### 3.4 PyFuncAgent（Python 计算专家）

> 源码：`agents/pyfunc_agent.py`

#### 类配置

```python
context_spec: ClassVar[ContextSpec] = ContextSpec(data_catalog="full")   # 看完整数据目录
prompt_template: ClassVar[str] = "pyfunc_agent"
agent_description: ClassVar[str] = "Python计算专家"
```

`thinking_enabled` 未显式声明，走默认 `True`（代码生成需要深度思考）。

#### 构造函数

```python
def __init__(self, model_client, data_context, session=None, task_id=None, task_description=None):
    python_tool = make_python_exec_tool(data_context)
    super().__init__(
        model_client=model_client,
        data_context=data_context,
        session=session,
        task_id=task_id,
        task_description=task_description,
        tools=[python_tool],
        reflect_on_tool_use=True,
        max_tool_iterations=3,
    )
```

#### 工具

| 工具名 | 实现位置 | 说明 |
|---|---|---|
| `python_exec` | `tools/python_exec.py` `make_python_exec_tool` | 在 exec 沙箱中执行 LLM 生成的 Python 代码 |

沙箱特性（见 `feedback_exec_vs_subprocess.md` 记忆）：

- `exec()` 而非 subprocess + pickle
- 31 个被阻止的 import + 15 个移除的 builtin
- AST 级 `__import__` 阻止（保留 `builtins.__import__`）
- AST 级 getattr/type/`__dunder__` 阻止
- numpy 输出净化（避免 NaN/Inf 序列化问题）

#### Prompt 扩展变量

```python
def _extra_prompt_vars(self) -> dict[str, str]:
    if self.session and self.session.skills:
        skills_text = get_skill_manager().format_skills_for_prompt(self.session.skills)
        return {"SKILLS": skills_text}
    return {"SKILLS": ""}
```

#### 设计依据

- **默认 Agent**：根据 `feedback_dataanalysis_routing.md` 记忆，PyFuncAgent 是所有非特定 ML 工具场景的默认分析 Agent
- **`max_tool_iterations=3`**：代码执行失败 → AutoGen 把错误反馈给 LLM 重试，3 次足够
- **`data_catalog="full"`**：代码需要知道所有可用 DataFrame 的 key 和 schema 才能正确加载
- **`thinking_enabled=True`**：代码生成需要 Chain-of-Thought

---

### 3.5 SQLAgent（SQL 查询专家）

> 源码：`agents/sql_agent.py`

#### 类配置

```python
context_spec: ClassVar[ContextSpec] = ContextSpec()
thinking_enabled: ClassVar[bool] = False                  # 浅层执行
prompt_template: ClassVar[str] = "sql_agent"
agent_description: ClassVar[str] = "SQL查询专家"
```

#### 构造函数

```python
def __init__(self, model_client, data_context, session=None, task_id=None, task_description=None):
    sql_tool = make_sql_query_tool(data_context)
    super().__init__(
        model_client=model_client,
        data_context=data_context,
        session=session,
        task_id=task_id,
        task_description=task_description,
        tools=[sql_tool],
        reflect_on_tool_use=True,
        # max_tool_iterations 走默认 1
    )
```

#### 工具

| 工具名 | 实现位置 | 说明 |
|---|---|---|
| `sql_query` | `tools/sql_query.py` `make_sql_query_tool` | 两阶段 SQL 执行：`sqlparse` 验证 + `asyncpg` 执行 |

安全特性：

- `sqlparse` Token 级校验（注意 CTE 关键字 gotcha：`Token.Keyword.CTE` 不在 `Token.Keyword` 中，必须用 `ttype in Keyword`）
- 参数化查询防 SQL 注入
- 行数限制 + 执行超时

#### Prompt 扩展变量

```python
def _extra_prompt_vars(self) -> dict[str, str]:
    """注入表信息占位。"""
    return {"TABLE_INFO": "（暂无表信息，请根据用户描述推断表名和字段）"}
```

**注意**：当前是占位字符串，未来需从 DB metadata 动态获取。

#### 设计依据

- **`thinking_enabled=False`**：写 SQL 是浅层任务
- **`max_tool_iterations=1`（默认）**：SQL 一次性执行，失败由 LLM 修正后再次执行（实际上 AutoGen 的 `reflect_on_tool_use=True` 默认会重试到 `max_tool_iterations` 次；显式不传表示走基类默认 1，需要 LLM 一轮内完成）
- **`context_spec` 默认**：SQL Agent 主要靠用户查询和表信息，不需要历史

---

### 3.6 DataAnalysisAgent（数据分析专家）

> 源码：`agents/data_analysis_agent.py`

#### 类配置

```python
context_spec: ClassVar[ContextSpec] = ContextSpec()
prompt_template: ClassVar[str] = "data_analysis_agent"
agent_description: ClassVar[str] = "数据分析专家"
```

`thinking_enabled` 走默认 `True`。

#### 构造函数

```python
def __init__(self, model_client, data_context, session=None, task_id=None, task_description=None):
    tools = [
        make_data_ingest_tool(data_context),
        make_data_clean_tool(data_context),
        make_data_summarize_tool(data_context),
        make_time_series_forecast_tool(data_context),
        make_anomaly_detect_tool(data_context),
        make_general_predict_tool(data_context),
    ]
    super().__init__(
        model_client=model_client,
        data_context=data_context,
        session=session,
        task_id=task_id,
        task_description=task_description,
        tools=tools,
        reflect_on_tool_use=True,
        # max_tool_iterations 走默认 1
    )
```

#### 工具（6 个 ML 工具）

| 工具名 | 模块 | 用途 |
|---|---|---|
| `data_ingest` | `tools/data_ingest.py` | 数据导入到 DataContext |
| `data_clean` | `tools/data_clean.py` | 数据清洗（缺失值、异常值、去重） |
| `data_summarize` | `tools/data_summarize.py` | 数据描述性统计摘要 |
| `time_series_forecast` | `tools/time_series_forecast.py` | 时序预测（ARIMA / Holt-Winters） |
| `anomaly_detect` | `tools/anomaly_detect.py` | 异常检测（IsolationForest / Z-Score / IQR） |
| `general_predict` | `tools/general_predict.py` | 通用预测（RF / GBM / Linear） |

所有 ML 工具用 `run_in_executor` 包裹同步模型训练。

#### Prompt 扩展变量

```python
def _extra_prompt_vars(self) -> dict[str, str]:
    """注入 DataContext 摘要。"""
    if self.data_context:
        dc_summary = self.data_context.all_summaries()
        return {"DATA_CONTEXT": dc_summary or "（DataContext 当前为空）"}
    return {"DATA_CONTEXT": "（DataContext 当前为空）"}
```

#### 设计依据

- **专用 Agent，仅 6 种 ML 场景触发**：根据 `feedback_dataanalysis_routing.md` 记忆，DataAnalysisAgent 只在 ML 工具场景下使用，其他分析任务走 PyFuncAgent
- **`DATA_CONTEXT` 注入**：ML 工具操作的是 DataContext 中的 DataFrame，必须把数据摘要告诉 LLM
- **`reflect_on_tool_use=True`**：工具失败时让 LLM 修正参数或换算法

---

### 3.7 RAGAgent（知识检索与总结专家）

> 源码：`agents/rag_agent.py`

#### 类配置

```python
context_spec: ClassVar[ContextSpec] = ContextSpec(
    query_history="full",       # 看完整查询历史
    data_catalog="none",        # 不看数据目录（搜索不需要）
)
thinking_enabled: ClassVar[bool] = False
prompt_template: ClassVar[str] = "rag_agent"
agent_description: ClassVar[str] = "知识检索与总结专家"
```

#### 构造函数

```python
def __init__(self, model_client, data_context, session=None, task_id=None, task_description=None):
    tools = [
        make_knowledge_search_tool(data_context),
        make_w3_search_tool(data_context),
        make_xiaoyi_search_tool(data_context),
    ]
    super().__init__(
        model_client=model_client,
        data_context=data_context,
        session=session,
        task_id=task_id,
        task_description=task_description,
        tools=tools,
        reflect_on_tool_use=True,
        max_tool_iterations=5,
    )
```

#### 工具（3 个搜索工具）

| 工具名 | 模块 | 用途 |
|---|---|---|
| `knowledge_search` | `tools/search_tools.py` | 企业内部知识库检索 |
| `w3_search` | `tools/search_tools.py` | W3 社区搜索 |
| `xiaoyi_search` | `tools/search_tools.py` | 小艺搜索（互联网搜索） |

搜索结果自动存入 DataContext，供下游 Agent 引用。

#### 设计依据

- **`query_history="full"`**：用户多轮提问经常基于前一轮的搜索结果展开追问，必须看完整历史
- **`data_catalog="none"`**：搜索工具不操作 DataFrame，看数据目录无意义
- **`max_tool_iterations=5`**：单个搜索源结果不全时，LLM 可尝试其他源或换关键词
- **`thinking_enabled=False`**：搜索 + 总结是浅层任务
- **`reflect_on_tool_use=True`**：让 LLM 在搜索后自行总结归纳（不再依赖单独的 `_summarize` 安全网，但保留为兜底——见 `feedback_react_prompt_design.md` 记忆）

---

### 3.8 VisualizationAgent（可视化专家）

> 源码：`agents/visualization_agent.py`

#### 类配置

```python
context_spec: ClassVar[ContextSpec] = ContextSpec(
    data_catalog="full",      # 看完整数据目录（要选数据画图）
    conclusions="all",        # 看所有 agent 结论（决定画什么图）
)
thinking_enabled: ClassVar[bool] = False
prompt_template: ClassVar[str] = "visualization_agent"
agent_description: ClassVar[str] = "可视化专家"
```

#### 构造函数

```python
def __init__(self, model_client, data_context, session=None, task_id=None, task_description=None):
    tools = [
        make_chart_generate_tool(data_context),
        make_dashboard_generate_tool(data_context),
    ]
    super().__init__(
        model_client=model_client,
        data_context=data_context,
        session=session,
        task_id=task_id,
        task_description=task_description,
        tools=tools,
        reflect_on_tool_use=True,
    )
```

#### 工具

| 工具名 | 模块 | 用途 |
|---|---|---|
| `chart_generate` | `tools/chart_generate.py` | 用 pyecharts 生成单张图表 |
| `dashboard_generate` | `tools/dashboard_generate.py` | 生成多图仪表盘 |

#### Prompt 扩展变量

```python
def _extra_prompt_vars(self) -> dict[str, str]:
    if self.data_context:
        dc_summary = self.data_context.all_summaries()
        chart_summary = self.data_context.chart_summaries()
        return {
            "DATA_CONTEXT": dc_summary or "（DataContext 当前为空）",
            "CHART_CONTEXT": chart_summary or "（当前没有图表）",
        }
    return {"DATA_CONTEXT": "（DataContext 当前为空）", "CHART_CONTEXT": "（当前没有图表）"}
```

注入两个上下文：

- `DATA_CONTEXT`：可用 DataFrame 列表和 schema
- `CHART_CONTEXT`：已生成的图表列表（避免重复生成）

#### 设计依据

- **`data_catalog="full"` + `conclusions="all"`**：画图前要决定"画哪种数据、配什么标题"——这需要看所有结论和所有数据
- **`thinking_enabled=False`**：选图表类型和参数是浅层判断
- **`reflect_on_tool_use=True`**：图表生成失败（如列名错误）时让 LLM 修正

---

### 3.9 ReportAgent（报告生成专家）

> 源码：`agents/report_agent.py`

#### 类配置

```python
context_spec: ClassVar[ContextSpec] = ContextSpec(
    query_history="full",      # 看完整用户问题（理解意图）
    conclusions="all",         # 看所有 agent 结论（汇编报告）
)
prompt_template: ClassVar[str] = "report_agent"
agent_description: ClassVar[str] = "报告生成专家"
```

`thinking_enabled` 未声明，走默认 `True`（报告生成需要深度思考组织结构）。

#### 构造函数

```python
def __init__(self, model_client, data_context, session=None, task_id=None, task_description=None):
    super().__init__(
        model_client=model_client,
        data_context=data_context,
        session=session,
        task_id=task_id,
        task_description=task_description,
        tools=[self._call_report_generate],
    )
```

**注意**：直接传**实例方法** `self._call_report_generate` 作为工具，而非工厂函数。`reflect_on_tool_use` 和 `max_tool_iterations` 都走默认（`False` 和 `1`）。

#### 工具

| 工具名 | 实现 | 用途 |
|---|---|---|
| `_call_report_generate` | ReportAgent 实例方法 | 调用 `tools/report_generate.py:report_generate` 渲染报告 |

#### 实例方法工具

```python
async def _call_report_generate(self, report_json: str) -> str:
    """生成格式化的报告文件。

    Args:
        report_json: 报告内容的JSON字符串，格式需符合ReportContent模型。
    """
    try:
        return await report_generate(report_json, self.data_context)
    except Exception as e:
        logger.error("report_generate tool error: %s", e)
        return f"报告生成失败: {e}"
```

#### Prompt 扩展变量

```python
def _extra_prompt_vars(self) -> dict[str, str]:
    if self.data_context:
        return {"DATA_CONTEXT": self.data_context.all_summaries()}
    return {"DATA_CONTEXT": ""}
```

#### 设计依据

- **纯渲染工具（不调 LLM）**：根据 `project_report_tool_architecture.md` 记忆，`report_generate` 工具 **必须不调用 LLM**。LLM 在 Agent 这一层输出结构化 JSON（符合 `ReportContent` 模型），工具只做渲染（docx/pptx/html/pdf）。这保证 LLM 和渲染逻辑解耦
- **`reflect_on_tool_use=False`**：报告生成是终端步骤，不需要重试
- **`query_history="full" + conclusions="all"`**：报告要汇编整个会话的所有结论，必须全视角
- **`thinking_enabled=True`**：报告结构组织、章节编排需要深度思考

---

## 4. AgentFactory 惰性注册表

> 源码：`core/agent_factory.py`

### 4.1 设计动机

替代 `agent_layer.py` 历史中的内联 `if/elif` 链，使用注册表模式集中创建 Agent。**惰性导入避免顶层加载全部 Agent 类**——只有真正用到时才 import，加快启动速度、降低循环依赖风险。

### 4.2 `_MODULE_MAP` 映射表

```python
_MODULE_MAP: ClassVar[dict[AgentType, str]] = {
    AgentType.API: "agents.api_agent.APIAgent",
    AgentType.SQL: "agents.sql_agent.SQLAgent",
    AgentType.PYFUNC: "agents.pyfunc_agent.PyFuncAgent",
    AgentType.DATA_ANALYSIS: "agents.data_analysis_agent.DataAnalysisAgent",
    AgentType.VISUALIZATION: "agents.visualization_agent.VisualizationAgent",
    AgentType.REPORT: "agents.report_agent.ReportAgent",
    AgentType.SEARCH: "agents.rag_agent.RAGAgent",
}
```

（`core/agent_factory.py:34-42`）

**注意**：

- 只映射 7 个执行类 Agent——`PlanAgent` 不在内（由 `AgentLayer._plan` 直接创建）
- 值是字符串模块路径，而非类引用——惰性加载的基础

`AgentType` 枚举（`models/routing.py:18-27`）：

```python
class AgentType(str, Enum):
    SEARCH = "RAGAgent"
    API = "APIAgent"
    SQL = "SQLAgent"
    PYFUNC = "PyFuncAgent"
    DATA_ANALYSIS = "DataAnalysisAgent"
    VISUALIZATION = "VisualizationAgent"
    REPORT = "ReportAgent"
```

### 4.3 `_resolve` 惰性解析 + 缓存

```python
@classmethod
def _resolve(cls, agent_type: AgentType) -> type[BIBaseAgent]:
    """惰性解析 AgentType → Agent 类，解析后缓存。"""
    if agent_type in cls._resolved:
        return cls._resolved[agent_type]

    module_path = cls._MODULE_MAP.get(agent_type)
    if module_path is None:
        raise ValueError(f"未注册的Agent类型: {agent_type.value}")

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
```

**关键校验**：

- `rsplit(".", 1)` 切分模块路径——从右往左切一次，确保类名正确分离
- `issubclass(agent_cls, BIBaseAgent)` 保证所有注册的 Agent 都是合法子类
- 解析结果缓存到 `_resolved: ClassVar[dict[AgentType, type[BIBaseAgent]]]`，下次直接命中

### 4.4 `register` 扩展点（P2 开闭原则）

```python
@classmethod
def register(cls, agent_type: AgentType, module_path: str) -> None:
    """注册新的 Agent 类型（P2 开闭扩展点）。

    Args:
        agent_type: Agent 类型枚举值。
        module_path: 完整模块路径，格式 "agents.xxx.XxxAgent"。
    """
    cls._MODULE_MAP[agent_type] = module_path
    cls._resolved.pop(agent_type, None)  # 清除旧缓存
    logger.info("[AgentFactory] 注册: %s → %s", agent_type.value, module_path)
```

**新增 Agent 的流程**：

1. 编写 `agents/my_agent.py`，定义 `class MyAgent(BIBaseAgent)`
2. 在 `models/routing.py` 的 `AgentType` 枚举添加 `MY_AGENT = "MyAgent"`
3. 在程序初始化时调用 `AgentFactory.register(AgentType.MY_AGENT, "agents.my_agent.MyAgent")`

**无需修改** `agent_factory.py` 的核心代码、`agent_layer.py`、`BIBaseAgent`——这是 P2 开闭原则的实践。

### 4.5 `create` / `create_default` / `create_from_task_node`

#### `create`：从 AgentType 创建（通用入口）

```python
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
```

签名约定：所有 Agent 子类的 `__init__` 都接收 `(model_client, data_context, session=, task_id=, task_description=)`，工厂可以统一调用。

#### `create_default`：兜底创建

```python
@classmethod
def create_default(cls, model_client, data_context, *, session=None, task_id=None, task_description=None) -> BIBaseAgent:
    """创建默认Agent（RAGAgent），用于路由结果无明确agent_type时。"""
    return cls.create(AgentType.SEARCH, model_client, data_context, ...)
```

路由层未给出明确 `agent_type` 时，默认走 `RAGAgent`（搜索是最通用的兜底）。

#### `create_from_task_node`：DAG 任务节点工厂

```python
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
```

**关键步骤**：

1. `AgentType(task_node.agent)` 把字符串 `"APIAgent"` 转成枚举（task_node.agent 是 LLM 输出的字符串）
2. 调用 `create` 实例化
3. **设置 `agent._depends_on = task_node.depends_on`**——这样 `BIBaseAgent.on_messages_stream` 步骤 1 才能做跳过检查

---

## 5. Agent 生命周期

### 5.1 创建时机

两种路径（`core/agent_layer.py`）：

#### 单 Agent 路径（`run_single_agent`）

```
RoutingResult.agent_type ≠ None
    ↓
AgentFactory.create(routing.agent_type, model_client, dc,
                    session=session_ctx, task_id="direct",
                    task_description=routing.task_description)
```

详见 `core/agent_layer.py:91-118`。

#### 全团队 DAG 路径（`run_team`）

```
1. PlanAgent(model_client, session=session_ctx, translator=translator) 直接创建
   ↓
2. PlanAgent.on_messages_stream → 输出 DAGPlan
   ↓
3. _build_dag(plan, task_contexts, shared_dc):
       for task_node in plan.tasks:
           agent = AgentFactory.create_from_task_node(task_node, model_client, task_ctx)
           builder.add_node(agent)
           agents[task_node.task_id] = agent
       for task_node in plan.tasks:
           for dep_id in task_node.depends_on:
               builder.add_edge(agents[dep_id], agents[task_node.task_id])
       team = GraphFlow(participants=..., graph=builder.build(), ...)
   ↓
4. team.run_stream(query) 启动 DAG 并行执行
```

详见 `core/agent_layer.py:485-622`。

### 5.2 GraphFlow DAG 执行

`GraphFlow` 是 AutoGen 提供的 DAG 编排器。执行特点：

- **依赖感知**：上游 agent 完成后下游才启动
- **并行执行**：无依赖关系的 agent 并行
- **事件流**：通过 `team.run_stream()` 异步生成事件

`AgentLayer` 拦截事件并翻译为 `StreamEvent`：

```python
async with asyncio.timeout(180):
    async for event in team.run_stream(task=query):
        if isinstance(event, (BaseAgentEvent, BaseChatMessage)):
            for stream_ev in translator.translate(event, plan=plan):
                self._append_dag_agent_record(stream_ev, plan, session_ctx, turn_result)
                yield stream_ev
                if stream_ev.type == StreamEventType.AGENT_END:
                    final_result = stream_ev.content
                    async for table_ev in self._detect_failure_and_emit_tables(
                        stream_ev, task_contexts, emitted_table_keys, translator, session_ctx,
                    ):
                        yield table_ev
```

每个 `AGENT_END` 后立即：

1. 集中失败检测：`looks_like_failure(stream_ev.content)` → 标记 `session_ctx._failed_task_ids`
2. 推送该 agent 写入 DataContext 的 TABLE 事件（去重，避免重复推送）

### 5.3 钩子调用顺序

```
GraphFlow 调用 agent.on_messages([user_msg])  或 agent.on_messages_stream([user_msg])
    ↓
BIBaseAgent.on_messages: set_current_agent(name), set_enable_thinking(thinking_enabled)
    ↓
BIBaseAgent.on_messages_stream:
    ├── 步骤 1: 跳过失败依赖检查
    ├── 步骤 2: task_description 注入（替换最后一条 user 消息）
    ├── 步骤 3: await on_before_turn(messages)   ← 子类可覆写
    ├── 步骤 4: set_current_agent / set_enable_thinking（防 ContextVar 未设置）
    ├── 步骤 5: async for event in super().on_messages_stream(...):
    │         ├── 拦截 ToolCallRequestEvent → 缓冲 call_id
    │         ├── 拦截 ToolCallExecutionEvent → 配对 + 写入 trace + DB
    │         └── yield event
    └── 步骤 6: await on_after_turn(final_response, messages) → yield processed or response
```

### 5.4 工具调用记录

`BIBaseAgent.on_messages_stream` 拦截 `ToolCallRequestEvent` 和 `ToolCallExecutionEvent`，配对后调用静态方法 `_record_tool_call`（`agents/base.py:277-317`）：

```python
@staticmethod
async def _record_tool_call(
    agent_name, task_id, tool_name, call_id, arguments, result, is_error=False, recorder=None,
) -> None:
    if recorder is not None:
        recorder.record_tool_call(
            agent_name=agent_name, tool_name=tool_name,
            arguments=arguments, result=result,
        )
    # DB: 异步写入工具调用记录
    try:
        from db.writer import db_writer, make_tool_call_data
        from observability.logging_client import _chat_id_var, _session_id_var
        await db_writer.enqueue_tool_call(make_tool_call_data(
            session_id=_session_id_var.get(),
            chat_id=_chat_id_var.get(),
            task_id=task_id,
            agent_name=agent_name,
            tool_name=tool_name,
            call_id=call_id,
            arguments=arguments,
            is_error=is_error,
            error_message=result[:2000] if is_error else "",
            result_preview=result[:2000] if not is_error else "",
        ))
    except Exception as db_err:
        logger.debug("[BIBaseAgent] DB写入工具调用失败: %s", db_err)
```

**两路写入**：

1. **TraceRecorder**（同步）：写入内存 trace，供实时查看
2. **DBWriter**（异步 enqueue）：批量写入 PostgreSQL `tool_calls` 表

**注意**：`PyFuncAgent` 历史上曾绕过 `BIBaseAgent.on_messages_stream` 自行调用 `python_exec`，所以静态方法被设计为可独立调用——`PyFuncAgent` 通过显式调用 `_record_tool_call` 补全记录（见 docstring `agents/base.py:288-292`）。

### 5.5 失败检测与传播

```
Agent 输出 content
    ↓
AGENT_END 事件
    ↓
AgentLayer._detect_failure_and_emit_tables 调用 looks_like_failure(content)
    ↓
若失败 → session_ctx._failed_task_ids.add(stream_ev.task_id)
    ↓
下游 Agent.on_messages_stream 步骤 1 检测到 _depends_on 命中 _failed_task_ids
    ↓
yield "任务跳过：上游依赖任务 [...] 失败" + 标记自身失败 + return
    ↓
更下游继续传播
```

"任务跳过" 文本本身在 `_FAILURE_KEYWORDS` 中，所以即使没有 `AGENT_END` 显式失败，跳过消息也会被 `looks_like_failure` 识别为失败——**保证跳过链路不会断**。

### 5.6 单 Agent 路径超时

```python
timeout_sec = settings.agent_execution_timeout
try:
    async with asyncio.timeout(timeout_sec):
        async for event in agent.on_messages_stream([user_msg], cancellation_token):
            for stream_ev in translator.translate(event):
                yield stream_ev
finally:
    cancellation_token.cancel()
```

使用 `asyncio.timeout()` 上下文管理器（不是 `asyncio.wait_for`，因为 `wait_for` 不能包裹 async generator）。`finally` 中取消 token 防止挂起。

---

## 6. PlanAgent ask_user 拦截机制

### 6.1 为什么覆写 on_messages_stream

`PlanAgent` 是唯一**完全覆写** `on_messages_stream` 的 Agent。原因：

> **ask_user 必须发生在 LLM 流程中间**——LLM 调用 `ask_user` tool 后，需要先向用户提问、等待答复，再把答复作为 tool 结果喂回 LLM，让 LLM 继续输出 DAG plan。

如果走基类 `on_messages_stream`，AutoGen 会立即执行 `ask_user` 的 func——但 func 是 `await answer_queue.get()`，会阻塞整个事件流且无法先 yield `USER_QUESTION` 事件。

所以必须：

1. 拦截 `ToolCallRequestEvent(name="ask_user")`，**不让 AutoGen 立即执行 func**
2. 转而 yield `USER_QUESTION` 事件给前端
3. 等待用户答复（通过 `AskUserHandler.wait_for_answer`）
4. yield `USER_ANSWER` 事件
5. 将答复 `put` 到 `answer_queue`——此时 AutoGen 执行 `_ask_user_impl` 的 `await answer_queue.get()` 立即返回，结果喂给 LLM
6. LLM 收到答复后继续输出 DAG plan

### 6.2 流程总览

```
AgentLayer._plan:
    plan_agent = PlanAgent(model_client, session=session_ctx, translator=translator)
    plan_messages = [TextMessage(content=task, source="user")]
    async for event in plan_agent.on_messages_stream(plan_messages, cancellation_token=None):
        if isinstance(event, StreamEvent):
            yield event
    ↓
PlanAgent.on_messages_stream:

[1] 注入多轮历史（on_before_turn）
        ↓
[2] AGENT_START 事件
        ↓
[3] for event in AssistantAgent.on_messages_stream(self, messages, cancellation_token):
        ├── ToolCallRequestEvent:
        │     ├── translate → TOOL_CALL
        │     └── 如果 tc.name == "ask_user":
        │           ├── 解析参数 (question, question_type, options, context, default)
        │           ├── handler.can_ask() 检查（不能提问则 put "无法提问" 文本跳过）
        │           ├── 创建 UserQuestion 对象
        │           ├── yield USER_QUESTION 事件
        │           ├── answer = await handler.wait_for_answer(question_id)  ← 阻塞
        │           ├── yield USER_ANSWER 事件
        │           └── await answer_queue.put(answer)   ← 喂给 AutoGen 的 _ask_user_impl
        ├── ToolCallExecutionEvent:
        │     └── translate → TOOL_RESULT
        ├── Response:
        │     ├── 提取 content 存入 self._final_content
        │     └── translate → AGENT_END 准备
        └── 其他事件（streaming chunks 等）: translate → THINK_CHUNK / LLM_CHUNK
[4] finally:
        ├── handler.cancel_pending()
        ├── ask_user_registry.unregister(session_id)
        └── yield AGENT_END (with status)
```

### 6.3 USER_QUESTION / USER_ANSWER 事件

```python
# USER_QUESTION
yield translator._make_event(
    StreamEventType.USER_QUESTION,
    question_text,
    uq.to_sse_data(),  # {question_id, question_type, options, context, default, ...}
)

# 等待用户答复（异步阻塞）
answer = await handler.wait_for_answer(uq.question_id)

# USER_ANSWER
yield translator._make_event(
    StreamEventType.USER_ANSWER,
    f"用户答复: {answer[:200]}",
    {
        "question_id": uq.question_id,
        "answer": answer,
        "answer_type": AnswerType.TEXT.value,
    },
)

# 将答复喂给 tool function
await self._answer_queue.put(answer)
```

**关键时序**：

- `wait_for_answer` 阻塞当前协程，等待 Gateway 通过 HTTP 提交答复
- 答复提交后，`AskUserHandler.submit_answer` 解析 `asyncio.Future`，`wait_for_answer` 解阻塞返回
- 然后才把答复 put 到 queue——此时 AutoGen 的 `_ask_user_impl` 协程已经在 `answer_queue.get()` 上等待，put 后立即返回

### 6.4 dag 工具与 DAGPlan 构造

LLM 规划完成后调用 `dag(tasks, reasoning)`：

```python
async def _dag_impl(tasks: list[TaskNode], reasoning: str = "") -> str:
    try:
        plan = DAGPlan(
            reasoning=reasoning,
            tasks=tasks,
            is_complete=not tasks,
        )
        plan.validate_dag()
        on_plan_created(plan)   # 回调：setattr(self, "_dag_plan", plan)
        return f"DAG生成成功，共{len(plan.tasks)}个任务"
    except ValueError as e:
        return f"DAG生成失败：{e}"
    except Exception as e:
        return f"DAG生成失败: {e}"
```

（`tools/dag_tool.py:27-41`）

**校验逻辑**（`models/dag_plan.py:87-124` `validate_dag`）：

1. 移除指向不存在 `task_id` 的依赖
2. 环检测（Kahn 拓扑排序，`has_cycle`）——有环抛 `ValueError`
3. 如果所有任务都有依赖（无根节点），清除所有依赖使其全部成为根节点

**plan 传递路径**：

```
LLM 调用 dag tool
    ↓
_dag_impl 构造 DAGPlan + validate_dag
    ↓
on_plan_created(plan) = setattr(self, "_dag_plan", plan)
    ↓
LLM 返回 "DAG生成成功，共N个任务" 给 AutoGen
    ↓
LLM 继续生成 Response（最终内容）
    ↓
on_after_turn:
    if self._dag_plan is not None:
        recorder.record_plan_output(self._dag_plan)
    ↓
AgentLayer._plan:
    dag_plan = plan_agent._dag_plan
    if dag_plan is not None:
        dag_plan.validate_dag()
        self._last_plan = dag_plan
        return
```

### 6.5 Fallback 解析

`AgentLayer._plan`（`core/agent_layer.py:485-560`）的 fallback 顺序：

```
1. 优先：plan_agent._dag_plan（dag 工具提交的 plan）
2. 降级：解析 plan_agent._final_content（LLM 文本输出）
    2a. 直接 json.loads(content)
        ├── list → DAGPlan.from_task_list(list)
        └── dict {"tasks": [...]} → DAGPlan.model_validate(dict)
3. 降级：从 content 提取 ```json ... ``` markdown 代码块，再 json.loads
4. 最终兜底：构造单任务 DAGPlan（"PlanAgent输出解析失败，降级为单任务"），
            用 RAGAgent 执行原任务
```

`_parse_plan_data`（`core/agent_layer.py:562-580`）兼容 3 种格式：

- `list` → `DAGPlan.from_task_list(plan_data)`
- `dict` 含 `tasks` → `DAGPlan.model_validate(plan_data)`
- `dict` 不含 `tasks` → 当作单任务包装为列表

### 6.6 AskUserHandler 状态机

`AskUserHandler`（`tools/ask_user.py:36-202`）的提问/答复配对机制：

**核心字段**：

```python
self._questions: dict[str, UserQuestion]                  # 已创建的问题
self._answers: dict[str, str]                              # 已提交但 future 未创建时的暂存
self._futures: dict[str, asyncio.Future[str] | None]      # Future（延迟创建）
self._ask_count: int                                        # 当前会话已提问次数
self._max_asks: int = DEFAULT_MAX_ASKS                      # 3，单会话最大提问次数
```

**关键方法**：

- `create_question()`：创建 `UserQuestion`，`_futures[qid] = None`（延迟创建避免无 event loop 时报错），返回 `uq`
- `wait_for_answer(qid)`：
  - 先检查 `_answers[qid]` 是否已有暂存答复（Gateway 早于 wait 提交）
  - 否则创建 Future，`await asyncio.wait_for(future, timeout)`
  - 超时返回 `default` 或 "用户未在规定时间内回复，已跳过该问题"
  - finally: `_cleanup(qid)`
- `submit_answer(qid, answer)`：由 Gateway 调用
  - 暂存到 `_answers[qid]`
  - 如果 Future 已创建（wait 在等）→ `future.set_result(answer)` 立即唤醒
  - 否则不创建 Future，等 `wait_for_answer` 被调用时直接读暂存

**单会话配额**：`max_asks = 3`，超过后 `can_ask()` 返回 False，PlanAgent 直接 put "无法提问，请直接输出DAG计划" 到 queue 跳过。

**会话结束清理**：`cancel_pending()` 把所有未完成 Future 设为 "会话已结束，问题被取消"。

### 6.7 AskUserRegistry 跨会话路由

```python
# PlanAgent.__init__
if session:
    ask_user_registry.register(session.session_id, self._ask_user_handler)
```

Gateway 收到用户答复 HTTP 请求时：

1. 从请求中拿到 `session_id` 和 `question_id`、`answer`
2. 通过 `ask_user_registry` 找到该会话的 `AskUserHandler`
3. 调用 `handler.submit_answer(question_id, answer)`
4. handler 内部唤醒等待中的 `wait_for_answer`

PlanAgent 执行结束时在 finally 中 `ask_user_registry.unregister(session_id)`。

---

## 附录：Agent 与 AgentType 对照

| AgentType 枚举 | 字符串值 | Agent 类 | 文件 |
|---|---|---|---|
| `AgentType.API` | `"APIAgent"` | `APIAgent` | `agents/api_agent.py` |
| `AgentType.SQL` | `"SQLAgent"` | `SQLAgent` | `agents/sql_agent.py` |
| `AgentType.PYFUNC` | `"PyFuncAgent"` | `PyFuncAgent` | `agents/pyfunc_agent.py` |
| `AgentType.DATA_ANALYSIS` | `"DataAnalysisAgent"` | `DataAnalysisAgent` | `agents/data_analysis_agent.py` |
| `AgentType.VISUALIZATION` | `"VisualizationAgent"` | `VisualizationAgent` | `agents/visualization_agent.py` |
| `AgentType.REPORT` | `"ReportAgent"` | `ReportAgent` | `agents/report_agent.py` |
| `AgentType.SEARCH` | `"RAGAgent"` | `RAGAgent` | `agents/rag_agent.py` |
| —（不在枚举） | `"PlanAgent"` | `PlanAgent` | `agents/plan_agent.py` |

## 附录：关键扩展点

| 扩展点 | 位置 | 用途 |
|---|---|---|
| `BIBaseAgent.context_spec` | 类属性 | 声明多轮上下文需求 |
| `BIBaseAgent.thinking_enabled` | 类属性 | 切换深度/浅层思考 |
| `BIBaseAgent.prompt_template` | 类属性 | 指定 prompt 模板名 |
| `BIBaseAgent._extra_prompt_vars()` | 实例方法 | 注入业务 prompt 变量 |
| `BIBaseAgent.on_before_turn()` | async 钩子 | 在 LLM 调用前注入历史/上下文 |
| `BIBaseAgent.on_after_turn()` | async 钩子 | 在 LLM 调用后处理 Response |
| `BIBaseAgent.on_messages_stream()` | async generator | 完全覆写事件流（PlanAgent 用） |
| `AgentFactory.register()` | classmethod | 注册新 Agent 类型 |

---