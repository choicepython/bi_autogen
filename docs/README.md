
# BI AutoGen 文档索引

> BI 数据分析智能体系统 — 基于 AutoGen SDK 构建的多智能体协作平台

本文档为 BI AutoGen 项目的统一文档入口，涵盖架构设计、详细设计、专题设计与运维部署共 11 份文档。

## 快速导航

### 新开发者入门（按顺序阅读）

1. [系统架构视图](系统架构视图.md) — 理解三层架构和核心设计模式
2. [业务架构视图](业务架构视图.md) — 理解业务目标和核心工作流
3. [技术架构视图](技术架构视图.md) — 理解技术栈和设计决策
4. [数据架构视图](数据架构视图.md) — 理解数据模型和数据流
5. [Agent设计](Agent设计.md) — 理解 8 种 Agent 的设计
6. [工具设计](工具设计.md) — 理解工具体系与安全机制
7. [数据库设计](数据库设计.md) — 理解表 schema 和异步写入
8. [部署指南](部署指南.md) — 搭建开发环境和运行系统
9. [API.md](API.md) — 理解 REST API 和 SSE 协议

### 架构师

- [系统架构视图](系统架构视图.md) — 架构总览 + 核心设计模式 + 扩展指南
- [多轮对话上下文方案设计](多轮对话上下文方案设计.md) — ContextSpec + TurnSummary + 4 层防御
- [数据架构视图](数据架构视图.md) — 数据模型 + 存储方案 + 数据流
- [技术架构视图](技术架构视图.md) — 技术栈选型 + 设计决策 + 风险应对

### 运维部署

- [部署指南](部署指南.md) — 环境配置 + 启动方式 + 日志体系
- [数据库设计](数据库设计.md) — 数据库初始化 + 异步写入 + 监控
- [API.md](API.md) — API 端点 + 健康检查 + 限流

### 功能开发

- [Agent设计](Agent设计.md) — Agent 基类 + ContextSpec + 8 种 Agent + 扩展指南
- [工具设计](工具设计.md) — 工具分类 + 安全机制 + DataContext 集成
- [记忆设计](记忆设计.md) — 三层记忆体系设计
- [系统架构视图](系统架构视图.md) — 新增 Agent/工具/路由中间件扩展点

## 文档全览（11 份）

### 架构设计（4 份）

| 文档 | 说明 |
|------|------|
| [系统架构视图](系统架构视图.md) | 三层架构、核心组件、请求生命周期、设计模式、扩展指南 |
| [业务架构视图](业务架构视图.md) | 业务目标、用户角色、核心工作流、查询类型、多轮对话场景 |
| [技术架构视图](技术架构视图.md) | 技术栈、设计决策、目录结构、依赖清单、风险应对 |
| [数据架构视图](数据架构视图.md) | 数据模型、存储方案、数据流、数据隔离、数据安全 |

### 详细设计（4 份）

| 文档 | 说明 |
|------|------|
| [Agent设计](Agent设计.md) | BIBaseAgent 基类、ContextSpec 自声明、8 种 Agent 详细设计、AgentFactory |
| [工具设计](工具设计.md) | 工具分类、签名、安全机制（exec 沙箱/SSRF/SQL 注入）、DataContext 集成 |
| [数据库设计](数据库设计.md) | 表 schema、表关系、异步写入、连接池、监控查询 |
| [API.md](API.md) | HTTP/SSE API 接口文档：REST 端点、SSE 协议、认证、限流、错误码 |

### 专题设计（2 份）

| 文档 | 说明 |
|------|------|
| [多轮对话上下文方案设计](多轮对话上下文方案设计.md) | ContextSpec、ConversationContext、TurnSummary、DataRef、4 层防御 |
| [记忆设计](记忆设计.md) | UserMemory + SlotMemory + KnowledgeMemory 三层记忆体系设计 |

### 运维部署（1 份）

| 文档 | 说明 |
|------|------|
| [部署指南](部署指南.md) | 环境要求、配置说明、启动方式、日志体系、可观测性 |

## 近期更新

以下为近期完成的重要架构与功能更新，文档已同步：

- **优雅降级机制** — ES 不可用时降级为本地 jieba 分词；Redis 不可用时降级为 InMemory 缓存；DB 写入失败时采用指数退避重试；Langfuse 不可用时降级为 no-op，单点故障不影响主流程
- **可观测性平台抽象** — 引入 `TraceObserver` 抽象层，Langfuse 作为其实现之一，便于后续接入其他可观测性平台
- **资源存储抽象** — `ResourceStore` 抽象层支持 ES 与 Local 双模式部署，按配置自动切换
- **AgentLayer 职责拆分** — 将原 AgentLayer 拆分为 `dag_executor` 与 `plan_executor`，职责单一化，便于测试与维护
- **搜索工具重构** — `WebSearch` 接入百度千帆平台，替代原 `XiaoYiSearch` / `W3Search`
- **报告下载功能** — 新增 `FILE` 事件类型与 `/api/v1/reports` 端点，支持生成报告的下载与查询
- **启动配置校验** — 新增 `startup_check.py`，在服务启动时校验关键配置项（LLM、DB、ES、Redis），缺失或异常时给出明确告警

## 项目概览

- **语言**：Python 3.12+
- **框架**：AutoGen SDK（`autogen-agentchat`、`autogen-ext`）
- **数据库**：PostgreSQL 14+（可选，自动降级）
- **检索**：Elasticsearch 7.10.2（可选，自动降级为本地 jieba）
- **缓存**：Redis（可选，自动降级为 InMemory）
- **LLM**：Qwen3（OpenAI 兼容 API）
- **包管理**：uv
- **测试**：pytest

## 核心架构

```
BITeam (薄门面)
  └── DispatchLayer (调度层)
        ├── SessionManager (会话生命周期)
        ├── RoutingLayer (3 层路由)
        └── AgentLayer (执行层)
              ├── AgentFactory (惰性注册表)
              ├── EventTranslator (事件翻译)
              ├── dag_executor / plan_executor (DAG 与计划执行)
              └── GraphFlow (DAG 并行执行)
```

**8 种 Agent**：PlanAgent / APIAgent / PyFuncAgent / SQLAgent / DataAnalysisAgent / RAGAgent / VisualizationAgent / ReportAgent

**SSE 事件协议**：13+ 种 StreamEvent 类型（SESSION_START/END、PLAN_START/COMPLETE、AGENT_START/END、TOOL_CALL/RESULT、LLM_CHUNK、THINK_CHUNK、DATA_STORED、TABLE、ERROR，以及新增的 FILE 事件）
