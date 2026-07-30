
-- BI Agent 系统 DDL
-- 幂等建表：所有 CREATE 均使用 IF NOT EXISTS

-- 启用 UUID 扩展
CREATE EXTENSION IF NOT EXISTS "pgcrypto";

-- ============================================================================
-- 1. session — 会话主表
-- ============================================================================
CREATE TABLE IF NOT EXISTS session (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              VARCHAR(64) NOT NULL,
    chat_id                 VARCHAR(64) DEFAULT '',
    query                   TEXT NOT NULL,
    status                  VARCHAR(20) NOT NULL DEFAULT 'running',
    execution_mode          VARCHAR(20) DEFAULT '',
    result                  TEXT DEFAULT '',
    error_message           TEXT DEFAULT '',

    -- 用户与业务上下文
    user_id                 VARCHAR(64) DEFAULT '',
    user_name               VARCHAR(128) DEFAULT '',
    source_site             VARCHAR(64) DEFAULT 'les_portal',

    -- 性能指标（会话级汇总）
    duration_ms             INTEGER DEFAULT 0,
    total_prompt_tokens     INTEGER DEFAULT 0,
    total_completion_tokens INTEGER DEFAULT 0,
    total_tokens            INTEGER DEFAULT 0,
    agent_count             SMALLINT DEFAULT 0,
    tool_call_count         SMALLINT DEFAULT 0,
    llm_call_count          SMALLINT DEFAULT 0,

    -- 元数据
    model_name              VARCHAR(64) DEFAULT '',
    selector_model          VARCHAR(64) DEFAULT '',
    app_version             VARCHAR(32) DEFAULT '',
    extra                   JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    UNIQUE(session_id, chat_id)
);

CREATE INDEX IF NOT EXISTS idx_session_user_id ON session(user_id);
CREATE INDEX IF NOT EXISTS idx_session_status ON session(status);
CREATE INDEX IF NOT EXISTS idx_session_created_at ON session(created_at);
CREATE INDEX IF NOT EXISTS idx_session_source_site ON session(source_site);

-- ============================================================================
-- 2. routing_decision — 路由决策记录
-- ============================================================================
CREATE TABLE IF NOT EXISTS routing_decision (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              VARCHAR(64) NOT NULL,
    chat_id                 VARCHAR(64) DEFAULT '',

    route_layer             SMALLINT NOT NULL,
    execution_mode          VARCHAR(20) NOT NULL,
    agent_type              VARCHAR(32) DEFAULT '',
    task_description        TEXT DEFAULT '',
    reasoning               TEXT DEFAULT '',

    api_meta_count          SMALLINT DEFAULT 0,
    skills_count            SMALLINT DEFAULT 0,
    api_meta_names          TEXT[] DEFAULT '{}',

    duration_ms             INTEGER DEFAULT 0,

    extra                   JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    FOREIGN KEY (session_id, chat_id) REFERENCES session(session_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_routing_session ON routing_decision(session_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_routing_agent_type ON routing_decision(agent_type);
CREATE INDEX IF NOT EXISTS idx_routing_layer ON routing_decision(route_layer);

-- ============================================================================
-- 3. dag_plan — DAG规划记录
-- ============================================================================
CREATE TABLE IF NOT EXISTS dag_plan (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              VARCHAR(64) NOT NULL,
    chat_id                 VARCHAR(64) DEFAULT '',

    task_count              SMALLINT NOT NULL DEFAULT 0,
    tasks_json              JSONB NOT NULL DEFAULT '[]',
    reasoning               TEXT DEFAULT '',
    is_complete             BOOLEAN DEFAULT FALSE,

    duration_ms             INTEGER DEFAULT 0,

    parse_success           BOOLEAN DEFAULT TRUE,
    fallback_used           BOOLEAN DEFAULT FALSE,
    retry_count             SMALLINT DEFAULT 0,

    extra                   JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    FOREIGN KEY (session_id, chat_id) REFERENCES session(session_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_plan_session ON dag_plan(session_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_plan_task_count ON dag_plan(task_count);

-- ============================================================================
-- 4. agent_execution — Agent执行记录
-- ============================================================================
CREATE TABLE IF NOT EXISTS agent_execution (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              VARCHAR(64) NOT NULL,
    chat_id                 VARCHAR(64) DEFAULT '',
    task_id                 VARCHAR(32) DEFAULT '',

    agent_type              VARCHAR(32) NOT NULL,
    agent_name              VARCHAR(64) NOT NULL,
    task_description        TEXT DEFAULT '',

    status                  VARCHAR(20) NOT NULL DEFAULT 'running',
    error_type              VARCHAR(64) DEFAULT '',
    error_message           TEXT DEFAULT '',

    result_preview          TEXT DEFAULT '',
    data_keys               TEXT[] DEFAULT '{}',
    data_row_count          INTEGER DEFAULT 0,

    duration_ms             INTEGER DEFAULT 0,
    prompt_tokens           INTEGER DEFAULT 0,
    completion_tokens       INTEGER DEFAULT 0,
    total_tokens            INTEGER DEFAULT 0,
    llm_call_count          SMALLINT DEFAULT 0,
    tool_call_count         SMALLINT DEFAULT 0,

    retry_count             SMALLINT DEFAULT 0,
    retry_reason            TEXT DEFAULT '',

    extra                   JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at             TIMESTAMPTZ,

    FOREIGN KEY (session_id, chat_id) REFERENCES session(session_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_agent_exec_session ON agent_execution(session_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_agent_exec_type ON agent_execution(agent_type);
CREATE INDEX IF NOT EXISTS idx_agent_exec_status ON agent_execution(status);
CREATE INDEX IF NOT EXISTS idx_agent_exec_created_at ON agent_execution(created_at);
CREATE INDEX IF NOT EXISTS idx_agent_exec_type_status ON agent_execution(agent_type, status);

-- ============================================================================
-- 5. tool_call — 工具调用记录
-- ============================================================================
CREATE TABLE IF NOT EXISTS tool_call (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              VARCHAR(64) NOT NULL,
    chat_id                 VARCHAR(64) DEFAULT '',
    task_id                 VARCHAR(32) DEFAULT '',
    agent_name              VARCHAR(64) NOT NULL,

    tool_name               VARCHAR(64) NOT NULL,
    call_id                 VARCHAR(64) DEFAULT '',

    arguments               JSONB DEFAULT '{}',

    is_error                BOOLEAN DEFAULT FALSE,
    error_message           TEXT DEFAULT '',
    result_preview          TEXT DEFAULT '',

    duration_ms             INTEGER DEFAULT 0,

    extra                   JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    FOREIGN KEY (session_id, chat_id) REFERENCES session(session_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_tool_call_session ON tool_call(session_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_tool_call_tool_name ON tool_call(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_call_agent ON tool_call(agent_name);
CREATE INDEX IF NOT EXISTS idx_tool_call_error ON tool_call(is_error) WHERE is_error = TRUE;

-- ============================================================================
-- 6. llm_call — LLM调用记录
-- ============================================================================
CREATE TABLE IF NOT EXISTS llm_call (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              VARCHAR(64) NOT NULL,
    chat_id                 VARCHAR(64) DEFAULT '',
    task_id                 VARCHAR(32) DEFAULT '',
    agent_name              VARCHAR(64) NOT NULL,

    call_index              INTEGER NOT NULL,
    direction               VARCHAR(10) NOT NULL,
    is_stream               BOOLEAN DEFAULT FALSE,

    model_name              VARCHAR(64) DEFAULT '',
    message_count           SMALLINT DEFAULT 0,
    tool_count              SMALLINT DEFAULT 0,
    tool_names              TEXT[] DEFAULT '{}',

    finish_reason           VARCHAR(32) DEFAULT '',
    prompt_tokens           INTEGER DEFAULT 0,
    completion_tokens       INTEGER DEFAULT 0,
    total_tokens            INTEGER DEFAULT 0,
    cached                  BOOLEAN DEFAULT FALSE,
    chunk_count             INTEGER DEFAULT 0,

    request_summary         TEXT DEFAULT '',
    response_preview        TEXT DEFAULT '',
    thought                 TEXT DEFAULT '',

    duration_ms             INTEGER DEFAULT 0,

    extra                   JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    FOREIGN KEY (session_id, chat_id) REFERENCES session(session_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_llm_call_session ON llm_call(session_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_llm_call_agent ON llm_call(agent_name);
CREATE INDEX IF NOT EXISTS idx_llm_call_model ON llm_call(model_name);
CREATE INDEX IF NOT EXISTS idx_llm_call_created_at ON llm_call(created_at);

-- ============================================================================
-- 7. session_feedback — 用户反馈
-- ============================================================================
CREATE TABLE IF NOT EXISTS session_feedback (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              VARCHAR(64) NOT NULL,
    chat_id                 VARCHAR(64) DEFAULT '',

    rating                  SMALLINT DEFAULT NULL,
    is_positive             BOOLEAN DEFAULT NULL,
    feedback_text           TEXT DEFAULT '',
    tags                    TEXT[] DEFAULT '{}',

    source                  VARCHAR(32) DEFAULT 'web',

    extra                   JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    FOREIGN KEY (session_id, chat_id) REFERENCES session(session_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_feedback_session ON session_feedback(session_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_feedback_rating ON session_feedback(rating);
CREATE INDEX IF NOT EXISTS idx_feedback_created_at ON session_feedback(created_at);

-- ============================================================================
-- 8. data_context_snapshot — DataContext数据快照
-- ============================================================================
CREATE TABLE IF NOT EXISTS data_context_snapshot (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id              VARCHAR(64) NOT NULL,
    chat_id                 VARCHAR(64) DEFAULT '',
    task_id                 VARCHAR(32) DEFAULT '',
    agent_name              VARCHAR(64) DEFAULT '',

    data_key                VARCHAR(128) NOT NULL,
    row_count               INTEGER DEFAULT 0,
    column_count            INTEGER DEFAULT 0,
    columns_json            JSONB DEFAULT '[]',
    data_schema             JSONB DEFAULT '{}',

    data_csv                TEXT DEFAULT '',
    storage_path            VARCHAR(256) DEFAULT '',

    extra                   JSONB DEFAULT '{}',

    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),

    FOREIGN KEY (session_id, chat_id) REFERENCES session(session_id, chat_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_snapshot_session ON data_context_snapshot(session_id, chat_id);
CREATE INDEX IF NOT EXISTS idx_snapshot_agent ON data_context_snapshot(agent_name);

-- ============================================================================
-- 字段中文注释
-- ============================================================================

-- 1. session 表注释
COMMENT ON TABLE session IS '会话主表，记录每次用户交互的完整生命周期';
COMMENT ON COLUMN session.id IS '主键UUID';
COMMENT ON COLUMN session.session_id IS '业务会话ID，格式YYYYMMDD_HHMMSS';
COMMENT ON COLUMN session.chat_id IS '对话轮次ID，多轮对话时使用';
COMMENT ON COLUMN session.query IS '用户原始问题';
COMMENT ON COLUMN session.status IS '会话状态：running-运行中/completed-已完成/failed-失败/cancelled-已取消/cached-缓存命中';
COMMENT ON COLUMN session.execution_mode IS '执行模式：single_agent-单Agent直接执行/full_team-完整PlanAgent+DAG流水线';
COMMENT ON COLUMN session.result IS '最终回答内容（截断到10000字符）';
COMMENT ON COLUMN session.error_message IS '错误信息（失败时记录）';
COMMENT ON COLUMN session.user_id IS '用户ID';
COMMENT ON COLUMN session.user_name IS '用户姓名';
COMMENT ON COLUMN session.source_site IS 'API来源站点，如les_portal';
COMMENT ON COLUMN session.duration_ms IS '会话总耗时（毫秒）';
COMMENT ON COLUMN session.total_prompt_tokens IS '汇总输入Token数';
COMMENT ON COLUMN session.total_completion_tokens IS '汇总输出Token数';
COMMENT ON COLUMN session.total_tokens IS '汇总总Token数';
COMMENT ON COLUMN session.agent_count IS '执行的Agent数量';
COMMENT ON COLUMN session.tool_call_count IS '工具调用总次数';
COMMENT ON COLUMN session.llm_call_count IS 'LLM调用总次数';
COMMENT ON COLUMN session.model_name IS '主模型名称';
COMMENT ON COLUMN session.selector_model IS '选择器模型名称';
COMMENT ON COLUMN session.app_version IS '应用版本号';
COMMENT ON COLUMN session.extra IS '扩展字段（JSONB），预留后续迭代';
COMMENT ON COLUMN session.created_at IS '创建时间';
COMMENT ON COLUMN session.updated_at IS '最后更新时间';

-- 2. routing_decision 表注释
COMMENT ON TABLE routing_decision IS '路由决策记录，记录每次请求的路由判断结果';
COMMENT ON COLUMN routing_decision.id IS '主键UUID';
COMMENT ON COLUMN routing_decision.session_id IS '关联会话ID';
COMMENT ON COLUMN routing_decision.chat_id IS '关联对话轮次ID';
COMMENT ON COLUMN routing_decision.route_layer IS '命中的路由层：1-参数路由/2-规则路由/3-意图识别路由';
COMMENT ON COLUMN routing_decision.execution_mode IS '执行模式：single_agent/full_team';
COMMENT ON COLUMN routing_decision.agent_type IS '路由目标Agent类型，如APIAgent/PyFuncAgent等';
COMMENT ON COLUMN routing_decision.task_description IS '传给Agent的任务描述';
COMMENT ON COLUMN routing_decision.reasoning IS '路由决策理由';
COMMENT ON COLUMN routing_decision.api_meta_count IS 'ES召回的API工具数量';
COMMENT ON COLUMN routing_decision.skills_count IS 'ES召回的业务技能数量';
COMMENT ON COLUMN routing_decision.api_meta_names IS '召回的API名称列表';
COMMENT ON COLUMN routing_decision.duration_ms IS '路由决策耗时（毫秒）';
COMMENT ON COLUMN routing_decision.extra IS '扩展字段（JSONB）';
COMMENT ON COLUMN routing_decision.created_at IS '创建时间';

-- 3. dag_plan 表注释
COMMENT ON TABLE dag_plan IS 'DAG规划记录，记录PlanAgent输出的任务编排方案';
COMMENT ON COLUMN dag_plan.id IS '主键UUID';
COMMENT ON COLUMN dag_plan.session_id IS '关联会话ID';
COMMENT ON COLUMN dag_plan.chat_id IS '关联对话轮次ID';
COMMENT ON COLUMN dag_plan.task_count IS '任务节点数量';
COMMENT ON COLUMN dag_plan.tasks_json IS '完整的TaskNode列表（JSONB），包含task_id/agent/description/depends_on';
COMMENT ON COLUMN dag_plan.reasoning IS '规划思路简述';
COMMENT ON COLUMN dag_plan.is_complete IS '是否无需执行任何任务（PlanAgent判断可直接回答）';
COMMENT ON COLUMN dag_plan.duration_ms IS '规划耗时（毫秒）';
COMMENT ON COLUMN dag_plan.parse_success IS 'LLM输出是否解析成功';
COMMENT ON COLUMN dag_plan.fallback_used IS '是否降级为单任务（解析失败时的兜底策略）';
COMMENT ON COLUMN dag_plan.retry_count IS '规划重试次数';
COMMENT ON COLUMN dag_plan.extra IS '扩展字段（JSONB）';
COMMENT ON COLUMN dag_plan.created_at IS '创建时间';

-- 4. agent_execution 表注释
COMMENT ON TABLE agent_execution IS 'Agent执行记录，记录每个智能体的运行状态和性能指标';
COMMENT ON COLUMN agent_execution.id IS '主键UUID';
COMMENT ON COLUMN agent_execution.session_id IS '关联会话ID';
COMMENT ON COLUMN agent_execution.chat_id IS '关联对话轮次ID';
COMMENT ON COLUMN agent_execution.task_id IS 'DAG中的任务ID，如task_1/direct';
COMMENT ON COLUMN agent_execution.agent_type IS 'Agent类型，如APIAgent/PyFuncAgent/DataAnalysisAgent等';
COMMENT ON COLUMN agent_execution.agent_name IS 'Agent实例名，如APIAgent_task_1';
COMMENT ON COLUMN agent_execution.task_description IS '传给Agent的任务描述';
COMMENT ON COLUMN agent_execution.status IS '执行状态：running-运行中/success-成功/error-失败/timeout-超时';
COMMENT ON COLUMN agent_execution.error_type IS '错误类型，如ValueError/TimeoutError等';
COMMENT ON COLUMN agent_execution.error_message IS '错误详情';
COMMENT ON COLUMN agent_execution.result_preview IS '执行结果预览（截断到2000字符）';
COMMENT ON COLUMN agent_execution.data_keys IS '写入DataContext的数据key列表';
COMMENT ON COLUMN agent_execution.data_row_count IS '输出DataFrame行数';
COMMENT ON COLUMN agent_execution.duration_ms IS '执行耗时（毫秒）';
COMMENT ON COLUMN agent_execution.prompt_tokens IS '消耗的输入Token数';
COMMENT ON COLUMN agent_execution.completion_tokens IS '消耗的输出Token数';
COMMENT ON COLUMN agent_execution.total_tokens IS '消耗的总Token数';
COMMENT ON COLUMN agent_execution.llm_call_count IS 'LLM调用次数';
COMMENT ON COLUMN agent_execution.tool_call_count IS '工具调用次数';
COMMENT ON COLUMN agent_execution.retry_count IS '重试次数（PyFuncAgent最多3次）';
COMMENT ON COLUMN agent_execution.retry_reason IS '重试原因';
COMMENT ON COLUMN agent_execution.extra IS '扩展字段（JSONB）';
COMMENT ON COLUMN agent_execution.created_at IS '开始执行时间';
COMMENT ON COLUMN agent_execution.finished_at IS '执行完成时间';

-- 5. tool_call 表注释
COMMENT ON TABLE tool_call IS '工具调用记录，记录Agent调用的每个工具的入参和结果';
COMMENT ON COLUMN tool_call.id IS '主键UUID';
COMMENT ON COLUMN tool_call.session_id IS '关联会话ID';
COMMENT ON COLUMN tool_call.chat_id IS '关联对话轮次ID';
COMMENT ON COLUMN tool_call.task_id IS 'DAG中的任务ID';
COMMENT ON COLUMN tool_call.agent_name IS '调用者Agent实例名';
COMMENT ON COLUMN tool_call.tool_name IS '工具名称，如api_query/python_exec/report_generate等';
COMMENT ON COLUMN tool_call.call_id IS 'AutoGen工具调用ID';
COMMENT ON COLUMN tool_call.arguments IS '工具调用参数（JSONB）';
COMMENT ON COLUMN tool_call.is_error IS '是否调用出错';
COMMENT ON COLUMN tool_call.error_message IS '错误信息';
COMMENT ON COLUMN tool_call.result_preview IS '结果预览（截断到2000字符）';
COMMENT ON COLUMN tool_call.duration_ms IS '调用耗时（毫秒）';
COMMENT ON COLUMN tool_call.extra IS '扩展字段（JSONB）';
COMMENT ON COLUMN tool_call.created_at IS '调用时间';

-- 6. llm_call 表注释
COMMENT ON TABLE llm_call IS 'LLM调用记录，记录每次大模型请求和响应的详细信息';
COMMENT ON COLUMN llm_call.id IS '主键UUID';
COMMENT ON COLUMN llm_call.session_id IS '关联会话ID';
COMMENT ON COLUMN llm_call.chat_id IS '关联对话轮次ID';
COMMENT ON COLUMN llm_call.task_id IS 'DAG中的任务ID';
COMMENT ON COLUMN llm_call.agent_name IS '调用者Agent名称';
COMMENT ON COLUMN llm_call.call_index IS '会话内第几次LLM调用（从1递增）';
COMMENT ON COLUMN llm_call.direction IS '调用方向：request-请求/response-响应';
COMMENT ON COLUMN llm_call.is_stream IS '是否流式调用';
COMMENT ON COLUMN llm_call.model_name IS '模型名称';
COMMENT ON COLUMN llm_call.message_count IS '输入消息条数';
COMMENT ON COLUMN llm_call.tool_count IS '可用工具数量';
COMMENT ON COLUMN llm_call.tool_names IS '可用工具名称列表';
COMMENT ON COLUMN llm_call.finish_reason IS '完成原因，如stop/tool_calls/length等';
COMMENT ON COLUMN llm_call.prompt_tokens IS '输入Token数';
COMMENT ON COLUMN llm_call.completion_tokens IS '输出Token数';
COMMENT ON COLUMN llm_call.total_tokens IS '总Token数';
COMMENT ON COLUMN llm_call.cached IS '是否命中缓存';
COMMENT ON COLUMN llm_call.chunk_count IS '流式chunk数量';
COMMENT ON COLUMN llm_call.request_summary IS '请求摘要（system+user前200字符）';
COMMENT ON COLUMN llm_call.response_preview IS '响应预览（前500字符）';
COMMENT ON COLUMN llm_call.thought IS '思考链内容（如有）';
COMMENT ON COLUMN llm_call.duration_ms IS '请求到响应的耗时（毫秒）';
COMMENT ON COLUMN llm_call.extra IS '扩展字段（JSONB）';
COMMENT ON COLUMN llm_call.created_at IS '调用时间';

-- 7. session_feedback 表注释
COMMENT ON TABLE session_feedback IS '用户反馈记录，收集用户对回答质量的评价';
COMMENT ON COLUMN session_feedback.id IS '主键UUID';
COMMENT ON COLUMN session_feedback.session_id IS '关联会话ID';
COMMENT ON COLUMN session_feedback.chat_id IS '关联对话轮次ID';
COMMENT ON COLUMN session_feedback.rating IS '评分，1-5分';
COMMENT ON COLUMN session_feedback.is_positive IS '是否正面评价';
COMMENT ON COLUMN session_feedback.feedback_text IS '用户文字反馈';
COMMENT ON COLUMN session_feedback.tags IS '反馈标签，如"回答不准确"/"响应太慢"等';
COMMENT ON COLUMN session_feedback.source IS '反馈来源：web/api/cli';
COMMENT ON COLUMN session_feedback.extra IS '扩展字段（JSONB）';
COMMENT ON COLUMN session_feedback.created_at IS '反馈时间';

-- 8. data_context_snapshot 表注释
COMMENT ON TABLE data_context_snapshot IS 'DataContext数据快照，异步归档Agent间传递的DataFrame结构信息';
COMMENT ON COLUMN data_context_snapshot.id IS '主键UUID';
COMMENT ON COLUMN data_context_snapshot.session_id IS '关联会话ID';
COMMENT ON COLUMN data_context_snapshot.chat_id IS '关联对话轮次ID';
COMMENT ON COLUMN data_context_snapshot.task_id IS 'DAG中的任务ID';
COMMENT ON COLUMN data_context_snapshot.agent_name IS '产生数据的Agent名称';
COMMENT ON COLUMN data_context_snapshot.data_key IS 'DataContext中的数据key';
COMMENT ON COLUMN data_context_snapshot.row_count IS '数据行数';
COMMENT ON COLUMN data_context_snapshot.column_count IS '数据列数';
COMMENT ON COLUMN data_context_snapshot.columns_json IS '列名和类型列表（JSONB）';
COMMENT ON COLUMN data_context_snapshot.data_schema IS '列统计摘要（JSONB）';
COMMENT ON COLUMN data_context_snapshot.data_csv IS 'CSV格式数据（小数据集内联存储）';
COMMENT ON COLUMN data_context_snapshot.storage_path IS '外部存储路径（大数据集存储在对象存储）';
COMMENT ON COLUMN data_context_snapshot.extra IS '扩展字段（JSONB）';
COMMENT ON COLUMN data_context_snapshot.created_at IS '快照时间';

-- ============================================================================
-- 迁移：knowledge_meta_count → skills_count（幂等，已重命名时跳过）
-- ============================================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'routing_decision' AND column_name = 'knowledge_meta_count'
    ) THEN
        ALTER TABLE routing_decision RENAME COLUMN knowledge_meta_count TO skills_count;
        COMMENT ON COLUMN routing_decision.skills_count IS 'ES召回的业务技能数量';
    END IF;
END $$;