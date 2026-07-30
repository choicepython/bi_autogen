
# BI AutoGen 文档索引

> BI 数据分析智能体系统 — 基于 AutoGen SDK 构建的多智能体协作平台

## 快速导航

### 新开发者入门（按顺序阅读）

1. [系统架构视图](系统架构视图.md) — 理解三层架构和核心设计模式
2. [业务架构视图](业务架构视图.md) — 理解业务目标和核心工作流
3. [技术架构视图](技术架构视图.md) — 理解技术栈和设计决策
4. [数据架构视图](数据架构视图.md) — 理解数据模型和数据流
5. [Agent设计](Agent设计.md) — 理解8种Agent的设计
6. [工具设计](工具设计.md) — 理解19个工具的设计和安全机制
7. [数据库设计](数据库设计.md) — 理解8表schema和异步写入
8. [部署指南](部署指南.md) — 搭建开发环境和运行系统
9. [API.md](API.md) — 理解REST API和SSE协议

### 架构师

- [系统架构视图](系统架构视图.md) — 架构总览 + 核心设计模式 + 扩展指南
- [多轮对话上下文方案设计](多轮对话上下文方案设计.md) — ContextSpec + TurnSummary + 4层防御
- [数据架构视图](数据架构视图.md) — 数据模型 + 存储方案 + 数据流

### 运维部署

- [部署指南](部署指南.md) — 环境配置 + 启动方式 + 日志体系
- [数据库设计](数据库设计.md) — 数据库初始化 + 异步写入 + 监控
- [API.md](API.md) — API端点 + 健康检查 + 限流

### 功能开发

- [Agent设计](Agent设计.md) — Agent基类 + ContextSpec + 8种Agent + 扩展指南
- [工具设计](工具设计.md) — 工具分类 + 安全机制 + DataContext集成
- [系统架构视图](系统架构视图.md#7-扩展指南) — 新增Agent/工具/路由中间件

## 文档分类

### 架构设计（4份）

| 文档 | 说明 |
|------|------|
| [系统架构视图](系统架构视图.md) | 三层架构、核心组件、请求生命周期、设计模式、扩展指南 |
| [业务架构视图](业务架构视图.md) | 业务目标、用户角色、核心工作流、查询类型、多轮对话场景 |
| [技术架构视图](技术架构视图.md) | 技术栈、设计决策、目录结构、依赖清单、风险应对 |
| [数据架构视图](数据架构视图.md) | 数据模型、存储方案、数据流、数据隔离、数据安全 |

### 详细设计（4份）

| 文档 | 说明 |
|------|------|
| [Agent设计](Agent设计.md) | BIBaseAgent基类、ContextSpec自声明、8种Agent详细设计、AgentFactory |
| [工具设计](工具设计.md) | 19个工具分类、签名、安全机制（exec沙箱/SSRF/SQL注入）、DataContext集成 |
| [数据库设计](数据库设计.md) | 8表schema、表关系、异步写入、连接池、监控查询 |
| [API.md](API.md) | 9个REST端点、SSE协议、认证、限流、错误码 |

### 专题设计（2份）

| 文档 | 说明 |
|------|------|
| [多轮对话上下文方案设计](多轮对话上下文方案设计.md) | ContextSpec、ConversationContext、TurnSummary、DataRef、4层防御 |
| [记忆设计](记忆设计.md) | UserMemory + SlotMemory + KnowledgeMemory 三层记忆（设计文档，未实现） |

### 运维部署（1份）

| 文档 | 说明 |
|------|------|
| [部署指南](部署指南.md) | 环境要求、配置说明、启动方式、日志体系、可观测性 |

### 历史归档（7份）

历史设计文档和审计报告，保留供参考：

| 文档 | 说明 |
|------|------|
| [archive/MVP_PLAN.md](archive/MVP_PLAN.md) | 初始MVP计划（已过时，当前为8Agent+GraphFlow） |
| [archive/orchestrator_agent_design.md](archive/orchestrator_agent_design.md) | OrchestratorAgent替代方案（已拒绝） |
| [archive/a.md](archive/a.md) | Plan&Execute vs OrchestratorAgent 决策记录 |
| [archive/生产系统缺陷评估报告.md](archive/生产系统缺陷评估报告.md) | 21个缺陷审计报告 |
| [archive/agent_感受野_audit.md](archive/agent_感受野_audit.md) | Agent上下文感受野审计 |
| [archive/设计草稿.md](archive/设计草稿.md) | 初始设计草稿 |
| [archive/里程碑总结.md](archive/里程碑总结.md) | 里程碑总结（2026-07-25） |

## 项目概览

- **语言**: Python 3.12+
- **框架**: AutoGen SDK (autogen-agentchat, autogen-ext)
- **数据库**: PostgreSQL 14+
- **检索**: Elasticsearch 7.10.2
- **LLM**: Qwen3 (OpenAI兼容API)
- **包管理**: uv
- **测试**: pytest (700+ 测试用例)

## 核心架构

```
BITeam (薄门面)
  └── DispatchLayer (调度层)
        ├── SessionManager (会话生命周期)
        ├── RoutingLayer (3层路由)
        └── AgentLayer (执行层)
              ├── AgentFactory (惰性注册表)
              ├── EventTranslator (事件翻译)
              └── GraphFlow (DAG并行执行)
```

8种Agent: PlanAgent / APIAgent / PyFuncAgent / SQLAgent / DataAnalysisAgent / RAGAgent / VisualizationAgent / ReportAgent

19个工具: api_query / python_exec / sql_query / chart_generate / dashboard_generate / report_generate / search_tools (3种) / data_ingest / data_clean / data_summarize / time_series_forecast / anomaly_detect / general_predict / ask_user / dag / ppt_renderer / get_es_data / global_tools