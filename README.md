# BI AutoGen

> 基于 AutoGen SDK 构建的 BI 智能数据分析多智能体系统

BI AutoGen 是一套面向企业 BI 场景的智能数据分析平台，通过多 Agent 协作完成自然语言到数据分析、可视化、报告生成的端到端流程。系统采用三层架构设计，支持 DAG 并行执行与 SSE 流式输出，并具备完善的优雅降级机制。

## 特性

- **8 种 Agent 协作**：PlanAgent、APIAgent、PyFuncAgent、SQLAgent、DataAnalysisAgent、RAGAgent、VisualizationAgent、ReportAgent，覆盖规划、查询、分析、可视化、报告全流程
- **三层智能路由**：确定性规则 → ES 召回 → LLM 分类，兼顾速度与准确率
- **DAG 并行执行**：基于 GraphFlow 的任务图调度，自动识别可并行任务节点
- **SSE 流式输出**：13 种 StreamEvent 类型，前端实时获取规划、执行、数据、表格、错误等事件
- **优雅降级**：ES→本地 jieba 分词、Redis→InMemory 缓存、DB→指数退避重试、Langfuse→no-op，单点故障不影响主流程
- **共享数据上下文**：DataContext 在 Agent 间安全传递 DataFrame，带锁、淘汰、摘要
- **可观测性**：TraceObserver 抽象 + Langfuse 集成，请求全链路追踪
- **安全沙箱**：exec 沙箱阻止危险 import、SSRF DNS 校验、SQL 注入防御（sqlparse + 参数化查询）

## 快速开始

### 环境要求

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) 包管理工具
- PostgreSQL 14+（可选，未配置时自动降级）
- Elasticsearch 7.10.2（可选，未配置时使用本地 jieba）

### 安装

```bash
# 克隆仓库
git clone <repo-url>
cd bi_autogen

# 同步依赖（创建虚拟环境并安装全部依赖）
uv sync
```

### 配置

复制环境变量模板并填写：

```bash
cp .env.example .env
```

关键配置项（均带 `BI_` 前缀）：

| 配置项 | 说明 |
|--------|------|
| `BI_LLM_API_KEY` | LLM 服务 API Key（OpenAI 兼容） |
| `BI_LLM_BASE_URL` | LLM 服务地址 |
| `BI_LLM_MODEL` | 模型名称（默认 Qwen3） |
| `BI_PG_DSN` | PostgreSQL 连接串 |
| `BI_ES_HOSTS` | Elasticsearch 地址列表 |
| `BI_REDIS_URL` | Redis 连接（可选） |
| `BI_LANGFUSE_*` | Langfuse 可观测性配置（可选） |

### 运行

```bash
# 服务模式（启动 FastAPI + SSE 网关）
uv run python main.py --serve

# 单任务模式（命令行直接传入查询）
uv run python main.py 查询本月生产产出数据

# 交互模式（CLI 交互式问答）
uv run python main.py
```

## 架构概览

### 三层架构

```
BITeam (core/team.py) — 薄门面
  └── DispatchLayer (core/dispatch.py) — 调度层：会话生命周期、模型客户端
        ├── RoutingLayer (core/routing_layer.py) — 路由层：3层路由 + 中间件
        │     └── BIRouter (core/router.py) — 确定性规则 → ES召回 → LLM分类
        └── AgentLayer (core/agent_layer.py) — Agent层：工厂、DAG构建、执行
              ├── AgentFactory — 统一创建 8 种 Agent
              ├── EventTranslator — AutoGen 事件 → StreamEvent
              └── GraphFlow — DAG 并行执行
```

### Agent 协作流程

```
用户查询 → BIRouter → RoutingResult
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

### SSE 事件协议

13 种 StreamEvent 类型：`SESSION_START/END`、`PLAN_START/COMPLETE`、`AGENT_START/END`、`TOOL_CALL/RESULT`、`LLM_CHUNK`、`THINK_CHUNK`、`DATA_STORED`、`TABLE`、`ERROR`。

## 关键链接

- **API 文档**：服务启动后访问 [http://localhost:8000/docs](http://localhost:8000/docs)
- **项目文档**：见 [`docs/`](docs/) 目录，包含系统架构、Agent 设计、工具设计、数据库设计、部署指南等 11 份文档
- **文档索引**：[docs/README.md](docs/README.md)

## 技术栈

| 类别 | 技术 | 说明 |
|------|------|------|
| 语言 | Python 3.12+ | 使用 `|` 联合类型语法 |
| Agent 框架 | AutoGen SDK | `autogen-agentchat` + `autogen-ext` |
| Web 框架 | FastAPI | HTTP/SSE 网关 |
| 数据库 | PostgreSQL 14+ | 异步驱动 asyncpg |
| 检索引擎 | Elasticsearch 7.10.2 | 知识召回 + 路由召回 |
| 缓存 | Redis（可选） | 未配置时降级为 InMemory |
| 可观测性 | Langfuse（可选） | 未配置时降级为 no-op |
| 数据处理 | pandas | DataFrame 共享存储 |
| 包管理 | uv | 虚拟环境与依赖管理 |
| 代码检查 | ruff + mypy | Lint + 类型检查 |

## License

本项目 License 待补充（placeholder）。
