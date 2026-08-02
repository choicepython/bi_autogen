
# BI Agent API 接口文档

> 版本: 0.1.0 | 基础路径: `/api/v1` | 协议: HTTP/1.1 + SSE

---

## 目录

- [1. 概述](#1-概述)
- [2. 通用约定](#2-通用约定)
- [3. 聊天接口](#3-聊天接口)
  - [3.1 Web SSE 聊天](#31-web-sse-聊天)
- [4. 系统接口](#4-系统接口)
  - [4.1 健康检查](#41-健康检查)
- [5. 指标查询接口](#5-指标查询接口)
  - [5.1 Agent 指标](#51-agent-指标)
  - [5.2 工具指标](#52-工具指标)
  - [5.3 会话统计](#53-会话统计)
  - [5.4 路由分布](#54-路由分布)
- [6. 追溯接口](#6-追溯接口)
  - [6.1 会话追溯](#61-会话追溯)
- [7. 反馈接口](#7-反馈接口)
  - [7.1 提交反馈](#71-提交反馈)
- [8. 报告接口](#8-报告接口)
  - [8.1 下载报告](#81-下载报告)
- [9. SSE 事件协议](#9-sse-事件协议)
- [10. 错误码](#10-错误码)
- [11. 认证机制](#11-认证机制)
- [12. 限流策略](#12-限流策略)

---

## 1. 概述

BI Agent 是基于 AutoGen SDK 构建的多智能体数据分析系统，对外通过 FastAPI 提供 HTTP/SSE 接口。

| 渠道 | 入口路径 | 请求格式 | 认证方式 | 响应格式 |
|------|----------|----------|----------|----------|
| Web | `/api/v1/chat` | ChatRequest JSON | IAM Token (可选) | SSE 流式 |

---

## 2. 通用约定

### 请求头

| Header | 必填 | 说明 |
|--------|------|------|
| `Content-Type` | 是 | `application/json` |
| `Authorization` | 否 | IAM Token，格式 `Bearer <token>`。`auth_enabled=True` 时必填 |

### 响应格式

非流式接口统一返回 JSON：

```json
{
  "data": { ... },
  "error": "错误信息"  // 仅失败时存在
}
```

### 时间格式

所有时间字段均为 ISO 8601 格式：`2026-07-24T10:30:00+08:00`

---

## 3. 聊天接口

### 3.1 Web SSE 聊天

Web 渠道的核心对话接口，返回 SSE 流式响应，前端可逐事件渲染。

```
POST /api/v1/chat
```

**请求体** (ChatRequest):

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `query` | string | **是** | - | 用户原始问题 |
| `session_id` | string | 否 | `""` | 外部会话 ID，空则自动生成 |
| `chat_id` | string | 否 | `""` | 对话轮次 ID，多轮对话时使用 |
| `agent_type` | string | 否 | `""` | 强制路由到指定 Agent（参数路由覆盖） |
| `user` | object | 否 | - | 用户身份信息 |
| `user.user_id` | string | 否 | `""` | 用户 ID |
| `user.user_name` | string | 否 | `""` | 用户姓名 |
| `user.permissions` | string[] | 否 | `[]` | 权限列表，如 `["data_query", "report_generate"]` |
| `business` | object | 否 | - | 业务上下文信息 |
| `business.source_site` | string | 否 | `"les_portal"` | API 来源站点过滤 |
| `business.custom_env` | object | 否 | `{}` | 自定义参数注入 |
| `business.extra` | object | 否 | `{}` | 扩展字段 |

**请求示例**:

```json
{
  "query": "查询本月R13立体库转出的编码箱数",
  "session_id": "conv_20260724_001",
  "chat_id": "chat_001",
  "user": {
    "user_id": "u001",
    "user_name": "张三"
  },
  "business": {
    "source_site": "les_portal"
  }
}
```

**响应**: SSE 流 (`Content-Type: text/event-stream`)

```
event: session_start
data: {"seq":1,"type":"session_start","timestamp":"2026-07-24T10:00:01","session_id":"20260724_100001","content":"查询本月R13立体库转出的编码箱数","data":{"query":"查询本月R13立体库转出的编码箱数"}}
id: 1

event: plan_start
data: {"seq":2,"type":"plan_start","timestamp":"2026-07-24T10:00:01","session_id":"20260724_100001","data":{}}
id: 2

event: plan_complete
data: {"seq":3,"type":"plan_complete","timestamp":"2026-07-24T10:00:03","session_id":"20260724_100001","data":{"tasks":[{"task_id":"task_1","agent":"APIAgent","description":"查询R13立体库转出编码箱数","depends_on":[]}],"reasoning":"需要调用API查询数据"}}
id: 3

event: agent_start
data: {"seq":4,"type":"agent_start","timestamp":"2026-07-24T10:00:03","session_id":"20260724_100001","task_id":"task_1","agent_name":"APIAgent_task_1","data":{"agent_type":"APIAgent","description":"查询R13立体库转出编码箱数"}}
id: 4

event: tool_call
data: {"seq":5,"type":"tool_call","timestamp":"2026-07-24T10:00:04","session_id":"20260724_100001","task_id":"task_1","agent_name":"APIAgent_task_1","content":"调用 api_query","data":{"tool_name":"api_query","arguments":{"query":"R13立体库转出编码箱数"}}}
id: 5

event: tool_result
data: {"seq":6,"type":"tool_result","timestamp":"2026-07-24T10:00:06","session_id":"20260724_100001","task_id":"task_1","agent_name":"APIAgent_task_1","content":"查询到 1234 条","data":{"tool_name":"api_query","is_error":false,"duration_ms":1820}}
id: 6

event: llm_chunk
data: {"seq":7,"type":"llm_chunk","timestamp":"2026-07-24T10:00:07","session_id":"20260724_100001","agent_name":"APIAgent_task_1","data":{"chunk":"根据"}}
id: 7

event: llm_chunk
data: {"seq":8,"type":"llm_chunk","timestamp":"2026-07-24T10:00:07","session_id":"20260724_100001","agent_name":"APIAgent_task_1","data":{"chunk":"查询结果"}}
id: 8

event: agent_end
data: {"seq":9,"type":"agent_end","timestamp":"2026-07-24T10:00:08","session_id":"20260724_100001","task_id":"task_1","agent_name":"APIAgent_task_1","data":{"status":"success","duration_ms":4500}}
id: 9

event: session_end
data: {"seq":10,"type":"session_end","timestamp":"2026-07-24T10:00:08","session_id":"20260724_100001","data":{"result":"根据查询结果，R13立体库转出的编码箱数为1234箱","duration_ms":7200}}
id: 10
```

**错误响应** (SSE 流内):

```
event: error
data: {"seq":5,"type":"error","timestamp":"2026-07-24T10:00:05","session_id":"20260724_100001","content":"API调用超时","data":{"error_type":"TimeoutError","message":"API调用超时(30s)"}}
id: 5
```

**HTTP 错误**:

| 状态码 | 说明 |
|--------|------|
| 400 | 请求体无效或 `query` 为空 |
| 401 | 未认证（`auth_enabled=True` 时缺少 Token） |
| 429 | 并发请求超限 |

---

## 4. 系统接口

### 4.1 健康检查

```
GET /api/v1/health
```

**响应**:

```json
{
  "status": "ok",
  "version": "0.1.0",
  "checks": {
    "db": "ok",
    "es": "ok",
    "es_index": "ok"
  }
}
```

> `es_index` 检查项：启动时 `ensure_index_exists()` 自动检查 ES 资源索引（`BI_ES_RESOURCE_INDEX` 配置）是否存在，不存在则根据 `db/es_schema/` 中的 schema 自动创建。

> 健康检查端点不受限流影响。

---

## 5. 指标查询接口

所有指标接口在数据库不可用时返回 500 错误。

### 5.1 Agent 指标

查询各 Agent 的成功率、平均耗时和 Token 消耗。

```
GET /api/v1/metrics/agents
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `days` | int | 7 | 统计最近 N 天的数据 |

**响应**:

```json
{
  "data": [
    {
      "agent_type": "APIAgent",
      "total_executions": 150,
      "success_rate": 92.0,
      "avg_duration_ms": 3500,
      "avg_tokens": 1200,
      "total_tokens": 180000,
      "total_retries": 12
    },
    {
      "agent_type": "PyFuncAgent",
      "total_executions": 80,
      "success_rate": 85.5,
      "avg_duration_ms": 5200,
      "avg_tokens": 2100,
      "total_tokens": 168000,
      "total_retries": 24
    }
  ]
}
```

**字段说明**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `agent_type` | string | Agent 类型名称 |
| `total_executions` | int | 总执行次数 |
| `success_rate` | float | 成功率（百分比，保留1位小数） |
| `avg_duration_ms` | int | 平均执行耗时（毫秒） |
| `avg_tokens` | int | 平均每次消耗 Token 数 |
| `total_tokens` | int | 总 Token 消耗 |
| `total_retries` | int | 总重试次数 |

---

### 5.2 工具指标

查询工具调用的频次、错误率和平均耗时。

```
GET /api/v1/metrics/tools
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `days` | int | 7 | 统计最近 N 天的数据 |

**响应**:

```json
{
  "data": [
    {
      "tool_name": "api_query",
      "total_calls": 320,
      "error_count": 15,
      "error_rate": 4.7,
      "avg_duration_ms": 1800
    },
    {
      "tool_name": "python_exec",
      "total_calls": 85,
      "error_count": 8,
      "error_rate": 9.4,
      "avg_duration_ms": 4200
    }
  ]
}
```

**字段说明**:

| 字段 | 类型 | 说明 |
|------|------|------|
| `tool_name` | string | 工具名称 |
| `total_calls` | int | 总调用次数（仅统计调用 >5 次的工具） |
| `error_count` | int | 错误次数 |
| `error_rate` | float | 错误率（百分比） |
| `avg_duration_ms` | int | 平均调用耗时（毫秒） |

---

### 5.3 会话统计

按小时统计会话量、成功率、平均耗时和 Token 消耗。

```
GET /api/v1/metrics/sessions
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `hours` | int | 24 | 统计最近 N 小时的数据 |

**响应**:

```json
{
  "data": [
    {
      "hour": "2026-07-24T09:00:00+08:00",
      "total_sessions": 45,
      "success_rate": 88.9,
      "avg_duration_ms": 6200,
      "total_tokens": 540000
    },
    {
      "hour": "2026-07-24T10:00:00+08:00",
      "total_sessions": 32,
      "success_rate": 93.8,
      "avg_duration_ms": 5800,
      "total_tokens": 380000
    }
  ]
}
```

---

### 5.4 路由分布

查询路由层命中分布和目标 Agent 分布。

```
GET /api/v1/metrics/routing
```

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `days` | int | 7 | 统计最近 N 天的数据 |

**响应**:

```json
{
  "data": {
    "layers": [
      {
        "route_layer": 1,
        "hit_count": 120,
        "avg_duration_ms": 5
      },
      {
        "route_layer": 2,
        "hit_count": 80,
        "avg_duration_ms": 15
      },
      {
        "route_layer": 3,
        "hit_count": 50,
        "avg_duration_ms": 2500
      }
    ],
    "agents": [
      {
        "agent_type": "APIAgent",
        "hit_count": 95
      },
      {
        "agent_type": "PyFuncAgent",
        "hit_count": 60
      },
      {
        "agent_type": "FewDataAnalysisAgent",
        "hit_count": 25
      }
    ]
  }
}
```

**路由层说明**:

| 层级 | 说明 |
|------|------|
| 1 | 参数路由 — 根据 `agent_type` 参数直接路由 |
| 2 | 规则路由 — 根据关键词规则匹配 |
| 3 | 意图识别路由 — LLM 语义理解后路由 |

---

## 6. 追溯接口

### 6.1 会话追溯

追溯某次会话的完整执行链路，包括路由决策、DAG 规划、Agent 执行、工具调用和 LLM 调用。

```
GET /api/v1/sessions/{session_id}
```

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | 会话 ID |

**查询参数**:

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `chat_id` | string | `""` | 对话轮次 ID（多轮对话时使用） |

**响应**:

```json
{
  "data": {
    "session": {
      "id": "a1b2c3d4-...",
      "session_id": "20260724_100001",
      "chat_id": "",
      "query": "查询本月产量",
      "status": "completed",
      "execution_mode": "full_team",
      "result": "根据查询结果...",
      "duration_ms": 7200,
      "total_tokens": 5400,
      "agent_count": 2,
      "tool_call_count": 3,
      "llm_call_count": 5,
      "user_id": "u001",
      "created_at": "2026-07-24T10:00:01+08:00"
    },
    "routing": [
      {
        "route_layer": 3,
        "execution_mode": "full_team",
        "agent_type": "",
        "reasoning": "需要查询数据并分析",
        "duration_ms": 2500
      }
    ],
    "plan": [
      {
        "task_count": 2,
        "tasks_json": [
          {"task_id": "task_1", "agent": "APIAgent", "description": "查询产量数据", "depends_on": []},
          {"task_id": "task_2", "agent": "ReportAgent", "description": "生成报告", "depends_on": ["task_1"]}
        ],
        "reasoning": "先查数据再出报告",
        "duration_ms": 1800
      }
    ],
    "agents": [
      {
        "agent_type": "APIAgent",
        "agent_name": "APIAgent_task_1",
        "task_id": "task_1",
        "status": "success",
        "duration_ms": 3500,
        "total_tokens": 1200,
        "tool_call_count": 1
      },
      {
        "agent_type": "ReportAgent",
        "agent_name": "ReportAgent_task_2",
        "task_id": "task_2",
        "status": "success",
        "duration_ms": 2800,
        "total_tokens": 2100,
        "tool_call_count": 1
      }
    ],
    "tools": [
      {
        "tool_name": "api_query",
        "agent_name": "APIAgent_task_1",
        "is_error": false,
        "duration_ms": 1800,
        "arguments": {"query": "本月产量"}
      },
      {
        "tool_name": "report_generate",
        "agent_name": "ReportAgent_task_2",
        "is_error": false,
        "duration_ms": 1200,
        "arguments": {"format": "docx"}
      }
    ],
    "llm_calls": [
      {
        "agent_name": "APIAgent_task_1",
        "call_index": 1,
        "direction": "response",
        "model_name": "Qwen3.6-35B-A3B-FP8",
        "prompt_tokens": 800,
        "completion_tokens": 400,
        "total_tokens": 1200,
        "duration_ms": 3200,
        "finish_reason": "tool_calls"
      }
    ]
  }
}
```

**HTTP 错误**:

| 状态码 | 说明 |
|--------|------|
| 404 | 会话不存在 |
| 500 | 数据库查询失败 |

---

## 7. 反馈接口

### 7.1 提交反馈

用户对某次会话的回答质量进行评价。

```
POST /api/v1/feedback
```

**请求体**:

| 字段 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `session_id` | string | **是** | - | 关联的会话 ID |
| `chat_id` | string | 否 | `""` | 对话轮次 ID |
| `rating` | int | 否 | `null` | 评分，1-5 分 |
| `is_positive` | boolean | 否 | `null` | 是否正面评价 |
| `feedback_text` | string | 否 | `""` | 用户文字反馈 |
| `tags` | string[] | 否 | `[]` | 反馈标签，如 `["回答不准确", "响应太慢"]` |
| `source` | string | 否 | `"web"` | 反馈来源：`web` / `api` / `cli` |

**请求示例**:

```json
{
  "session_id": "20260724_100001",
  "chat_id": "",
  "rating": 4,
  "is_positive": true,
  "feedback_text": "回答准确，但响应稍慢",
  "tags": ["响应太慢"],
  "source": "web"
}
```

**响应**:

```json
{
  "status": "ok"
}
```

**HTTP 错误**:

| 状态码 | 说明 |
|--------|------|
| 400 | `session_id` 为空 |
| 500 | 写入失败 |

---

## 8. 报告接口

### 8.1 下载报告

下载已生成的报告文件（word/ppt/html/pdf）。

```
GET /api/v1/reports/{filename}
```

**路径参数**:

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `filename` | string | 是 | 报告文件名，仅允许纯文件名（不含目录路径） |

**安全防御**:

- **路径遍历防御**: 使用 `Path.name` 比对原始输入，拒绝含目录分隔符（`/`、`\`）或 `..` 的输入
- **文件位置**: 报告文件存放于 `tempfile.gettempdir()/bi_reports/` 目录
- **Content-Disposition**: 按 RFC 5987 规范使用 `filename*=UTF-8''` 编码文件名，支持非 ASCII 文件名

**响应**: 文件二进制流（`Content-Type` 按文件扩展名映射，如 `application/vnd.openxmlformats-officedocument.wordprocessingml.document`）

**HTTP 错误**:

| 状态码 | 说明 |
|--------|------|
| 400 | 非法文件名（含目录分隔符或 `..`） |
| 404 | 报告不存在或已过期 |

---

## 9. SSE 事件协议

Web 聊天接口 (`/api/v1/chat`) 返回 SSE (Server-Sent Events) 流式响应。每个事件遵循标准 SSE 格式：

```
event: <事件类型>
data: <JSON 负载>
id: <事件序号>

```

### 事件生命周期

```
SESSION_START
  PLAN_START → PLAN_COMPLETE
  AGENT_START
    TOOL_CALL → TOOL_RESULT  (可多次)
    LLM_CHUNK              (可多次)
    THINK_CHUNK            (可多次)
    DATA_STORED            (可多次)
    TABLE                  (可多次)
    FILE                   (可多次)
    USER_QUESTION → USER_ANSWER  (可多次)
  AGENT_END
  ... (下一个 Agent)
SESSION_END
```

### 事件类型一览

| 事件类型 | SSE event | 前端展示分类 | 说明 |
|----------|-----------|-------------|------|
| SESSION_START | `session_start` | UI 状态控制 | 会话开始，开启加载动画 |
| SESSION_END | `session_end` | 回答展示(兜底) | 会话结束，携带最终结果 |
| PLAN_START | `plan_start` | UI 状态控制 | 开始规划，显示骨架屏 |
| PLAN_COMPLETE | `plan_complete` | 过程展示 | 规划完成，渲染步骤条 |
| AGENT_START | `agent_start` | 过程展示 | Agent 开始执行，高亮步骤 |
| AGENT_END | `agent_end` | 过程展示 | Agent 完成，标记打勾+耗时 |
| TOOL_CALL | `tool_call` | 过程展示 | 调用工具，折叠显示 |
| TOOL_RESULT | `tool_result` | 过程展示 | 工具返回，错误时标红 |
| LLM_CHUNK | `llm_chunk` | 回答展示 | 流式文本片段，打字机效果 |
| THINK_CHUNK | `think_chunk` | 过程展示 | LLM 思考过程片段 (reasoning_content) |
| DATA_STORED | `data_stored` | 不展示 | DataContext 内部写入 |
| TABLE | `table` | 表格展示 | 结构化表格数据，携带列名和行数据 |
| FILE | `file` | 文件下载 | 文件产物（报告等），携带文件名和下载链接 |
| USER_QUESTION | `user_question` | 用户交互 | PlanAgent 向用户提问，携带问题和选项 |
| USER_ANSWER | `user_answer` | 用户交互 | 用户答复已收到，携带 question_id 和答复 |
| ERROR | `error` | 错误提示 | 错误事件，toast/红色卡片 |

### 各事件 data 字段 Schema

#### SESSION_START

```json
{
  "query": "用户原始问题"
}
```

#### SESSION_END

```json
{
  "result": "最终回答文本",
  "duration_ms": 7200
}
```

#### PLAN_START

```json
{}
```

#### PLAN_COMPLETE

```json
{
  "tasks": [
    {
      "task_id": "task_1",
      "agent": "APIAgent",
      "description": "查询产量数据",
      "depends_on": []
    }
  ],
  "reasoning": "规划思路简述"
}
```

#### AGENT_START

```json
{
  "agent_type": "APIAgent",
  "description": "查询产量数据"
}
```

#### AGENT_END

```json
{
  "status": "success",
  "duration_ms": 3500
}
```

> `status` 取值: `success` | `error` | `timeout`

#### TOOL_CALL

```json
{
  "tool_name": "api_query",
  "arguments": {"query": "产量"}
}
```

#### TOOL_RESULT

```json
{
  "tool_name": "api_query",
  "is_error": false,
  "duration_ms": 1800
}
```

#### LLM_CHUNK

```json
{
  "chunk": "根据查询"
}
```

> 前端应将连续的 `LLM_CHUNK` 事件拼接为完整回答文本。

#### THINK_CHUNK

```json
{
  "chunk": "我需要先查询..."
}
```

#### DATA_STORED

```json
{
  "key": "APIAgent_task_1_20260724_100003",
  "shape": [100, 5],
  "columns": ["日期", "产线", "产量", "单位", "备注"]
}
```

#### TABLE

```json
{
  "key": "SQLAgent_task_1_20260724_100003",
  "title": "查询结果",
  "columns": ["日期", "产线", "产量"],
  "rows": [["2026-07-01", "A线", 1200]],
  "row_count": 1
}
```

#### FILE

```json
{
  "filename": "report_20260724.docx",
  "format": "docx",
  "url": "/api/v1/reports/report_20260724.docx",
  "title": "本月生产数据分析报告"
}
```

#### USER_QUESTION

```json
{
  "question_id": "q_001",
  "question_type": "choice",
  "options": ["选项A", "选项B"],
  "context": "需要确认查询范围",
  "default": null,
  "timeout_seconds": 300,
  "ask_count": 1,
  "max_asks": 3
}
```

#### USER_ANSWER

```json
{
  "question_id": "q_001",
  "answer": "选项A",
  "answer_type": "choice"
}
```

#### ERROR

```json
{
  "error_type": "TimeoutError",
  "message": "API调用超时(30s)"
}
```

### SSE 负载公共字段

每个 SSE 事件的 `data` 字段是 JSON，包含以下公共字段：

| 字段 | 类型 | 说明 |
|------|------|------|
| `seq` | int | 全局递增序号，用于断线重连 |
| `type` | string | 事件类型（同 SSE event 字段） |
| `timestamp` | string | ISO 8601 时间戳 |
| `session_id` | string | 会话 ID |
| `task_id` | string | DAG 任务节点 ID（非任务事件为空） |
| `agent_name` | string | Agent 实例名（非 Agent 事件为空） |
| `content` | string | 人类可读摘要文本 |
| `data` | object | 事件类型专有的结构化数据 |

---

## 10. 错误码

### HTTP 状态码

| 状态码 | 说明 |
|--------|------|
| 200 | 成功 |
| 400 | 请求参数错误（缺少必填字段、格式错误） |
| 401 | 认证失败（Token 无效或缺失、签名验证失败） |
| 404 | 资源不存在（会话追溯时 session_id 不存在） |
| 429 | 请求被限流（并发超限） |
| 500 | 服务器内部错误（数据库不可用等） |

### SSE 流内错误

SSE 流中通过 `error` 事件类型传递错误，不中断流连接：

| error_type | 说明 |
|------------|------|
| `TimeoutError` | 执行超时 |
| `ValueError` | 参数值错误 |
| `RuntimeError` | 运行时异常 |
| `ToolExecutionError` | 工具执行失败 |

---

## 11. 认证机制

### Web 渠道 — IAM Token

- **开发模式** (`auth_enabled=False`): 跳过认证，所有请求放行
- **生产模式** (`auth_enabled=True`): 从 `Authorization: Bearer <token>` 提取 Token
  - Token 为空或格式错误 → 401
  - TODO: 对接华为 IAM Token 验证，从 Token 解析 `user_id` / `user_name`

配置项:

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `BI_AUTH_ENABLED` | `False` | 是否启用认证 |
| `BI_SERVER_HOST` | `0.0.0.0` | 服务监听地址 |
| `BI_SERVER_PORT` | `8000` | 服务监听端口 |
| `BI_CORS_ORIGINS` | `["*"]` | CORS 允许的源 |

---

## 12. 限流策略

| 策略 | 配置 | 说明 |
|------|------|------|
| 并发限流 | `max_concurrent=20` | 基于 `asyncio.Semaphore`，同时处理的最大请求数 |
| 健康检查豁免 | - | `/api/v1/health` 不受限流影响 |
| 限流响应 | HTTP 429 | 返回文本 `服务繁忙，请稍后重试` |

> 当前限流为进程内单机限流，不跨实例共享状态。

---

## 附录 A: 完整 cURL 示例

### 健康检查

```bash
curl http://localhost:8000/api/v1/health
```

### Web SSE 聊天

```bash
curl -N -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -d '{"query": "查询本月产量"}'
```

### Web SSE 聊天（带认证）

```bash
curl -N -X POST http://localhost:8000/api/v1/chat \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your_iam_token_here" \
  -d '{"query": "查询本月产量", "session_id": "conv_001"}'
```

### Agent 指标

```bash
curl http://localhost:8000/api/v1/metrics/agents?days=7
```

### 会话追溯

```bash
curl http://localhost:8000/api/v1/sessions/20260724_100001
```

### 提交反馈

```bash
curl -X POST http://localhost:8000/api/v1/feedback \
  -H "Content-Type: application/json" \
  -d '{"session_id": "20260724_100001", "rating": 5, "is_positive": true}'
```

### 下载报告

```bash
curl http://localhost:8000/api/v1/reports/report_20260724.docx -o report.docx
```