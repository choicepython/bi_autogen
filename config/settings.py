
from pydantic import SecretStr
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # ---- LLM 模型 ----
    primary_model: str = ""
    primary_base_url: str = ""
    primary_api_key: SecretStr = SecretStr("")

    selector_model: str = ""
    selector_base_url: str = ""
    selector_api_key: SecretStr = SecretStr("")

    # ---- API 网关 ----
    api_gateway_url: str = "http://localhost:9000"

    # ---- Elasticsearch ----
    es_hosts: str = ""
    es_username: str = ""
    es_password: SecretStr = SecretStr("")
    es_timeout: int = 30

    # ---- 执行参数 ----
    python_exec_timeout: int = 30
    max_team_turns: int = 20
    data_context_max_summary_rows: int = 5
    data_context_max_summary_cols: int = 20

    # ---- LLM 超时 ----
    llm_request_timeout: int = 120  # 单次 LLM 请求超时（秒）
    agent_execution_timeout: int = 180  # 单 Agent 执行超时（秒）
    routing_llm_timeout: int = 30  # 路由层 LLM 意图分类超时（秒）

    # ---- PPT 渲染 ----
    ppt_slide_width: int = 1280
    ppt_slide_height: int = 720
    ppt_theme_dir: str = ""

    # ---- PostgreSQL ----
    db_host: str = ""
    db_port: int = 5432
    db_name: str = ""
    db_user: str = ""
    db_password: SecretStr = SecretStr("")
    _db_dsn: str = ""

    # ---- SQL 执行限制 ----
    sql_max_rows: int = 5000
    sql_query_timeout: int = 5
    sql_pool_min_size: int = 2
    sql_pool_max_size: int = 10

    # ---- 数据分析工具 ----
    data_analysis_timeout: int = 60

    # ---- 图表与可视化 ----
    chart_output_dir: str = ""  # 空=tempfile
    chart_default_width: int = 800
    chart_default_height: int = 500
    chart_render_timeout: int = 30  # Playwright 截图超时

    # ---- 数据清洗/导入 ----
    data_clean_max_operations: int = 20
    data_ingest_max_rows: int = 100000

    # ---- 搜索工具 ----
    search_knowledge_url: str = ""
    search_w3_url: str = ""
    search_xiaoyi_url: str = ""
    search_timeout: int = 15
    search_max_retries: int = 2
    search_max_result_length: int = 8000  # 搜索结果最大字符数，防止token爆炸

    # ---- Server ----
    server_host: str = "0.0.0.0"
    server_port: int = 8000
    cors_origins: list[str] = []  # 空=不允许跨域，生产环境必须配置具体域名
    auth_enabled: bool = False

    # ---- LLM 思考/推理参数 ----
    enable_thinking: bool = True  # 是否启用 Qwen3 思考模式
    enable_thinking_output: bool = True  # 是否在流式输出中展示思考内容

    # ---- WeLink ----
    welink_app_id: str = ""
    welink_app_secret: SecretStr = SecretStr("")
    welink_callback_url: str = ""

    # ---- SSRF 防护 ----
    api_allowed_domains: list[str] = []  # API 调用允许的域名白名单，空=不限制
    welink_allowed_domains: list[str] = []  # WeLink 回调允许的域名白名单，空=不限制

    # ---- 结果缓存 ----
    result_cache_enabled: bool = False  # 是否启用结果级缓存
    result_cache_maxsize: int = 200  # 精确缓存最大条目数
    result_cache_ttl: float = 600.0  # 精确缓存过期时间（秒），默认10分钟

    # ---- 语义缓存 ----
    semantic_cache_enabled: bool = False  # 是否启用语义缓存
    semantic_cache_maxsize: int = 100  # 语义缓存最大条目数
    semantic_cache_ttl: float = 900.0  # 语义缓存过期时间（秒），默认15分钟
    semantic_cache_threshold: float = 0.92  # 语义相似度阈值（0-1）
    semantic_cache_model: str = "all-MiniLM-L6-v2"  # 嵌入模型名称

    model_config = {"env_prefix": "BI_", "env_file": ".env", "extra": "ignore"}

    @property
    def db_dsn(self) -> str:
        """构建 PostgreSQL DSN。如果 _db_dsn 有值直接用，否则从各字段拼接。"""
        if self._db_dsn:
            return self._db_dsn
        return f"postgresql://{self.db_user}:{self.db_password.get_secret_value()}@{self.db_host}:{self.db_port}/{self.db_name}"


settings = Settings()