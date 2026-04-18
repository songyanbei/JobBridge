"""应用配置，集中加载环境变量。

所有配置通过 pydantic-settings 从 .env 或环境变量加载。
其它模块统一 `from app.config import settings` 使用。
"""
from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),  # 优先读项目根目录，fallback 当前目录
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---- 应用 ----
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret_key: str = "change-me"

    # ---- 数据库 MySQL ----
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "jobbridge"
    db_user: str = "jobbridge"
    db_password: str = "jobbridge"

    @property
    def db_url(self) -> str:
        return (
            f"mysql+pymysql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}?charset=utf8mb4"
        )

    # ---- Redis ----
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_max_connections: int = 50  # 连接池上限，按并发量调整

    @property
    def redis_url(self) -> str:
        auth = f":{self.redis_password}@" if self.redis_password else ""
        return f"redis://{auth}{self.redis_host}:{self.redis_port}/{self.redis_db}"

    # ---- 企业微信 ----
    wecom_corp_id: str = ""
    wecom_agent_id: str = ""
    wecom_secret: str = ""
    wecom_token: str = ""
    wecom_aes_key: str = ""

    # ---- LLM（对应方案 §4.3 抽象层）----
    llm_provider: str = "qwen"
    llm_api_key: str = ""
    llm_api_base: str = ""
    llm_intent_model: str = "qwen-turbo"
    llm_reranker_model: str = "qwen-plus"
    llm_timeout_seconds: int = 30

    # ---- 对象存储 ----
    oss_provider: str = "local"
    oss_endpoint: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""
    oss_bucket: str = ""
    oss_local_dir: str = "uploads"           # 本地存储目录（oss_provider=local 时生效）
    oss_local_url_prefix: str = "/files"     # 本地文件 URL 前缀

    # ---- 运营后台 JWT ----
    admin_jwt_secret: str = "change-me"
    admin_jwt_expires_hours: int = 24

    # ---- 事件回传 API ----
    event_api_key: str = ""  # 小程序点击事件回传 API Key（生产环境每季度轮换）

    # ---- CORS ----
    cors_origins: str = ""  # 逗号分隔的允许域名列表，为空时开发环境允许全部，生产环境拒绝全部

    # ---- Phase 7：定时任务与监控 ----
    scheduler_timezone: str = "Asia/Shanghai"
    daily_report_chat_id: str = ""  # 企微群 chatid；为空时日报/告警只打 loguru 不推送
    monitor_queue_incoming_threshold: int = 50
    monitor_send_retry_threshold: int = 20
    monitor_alert_dedupe_seconds: int = 600

    @property
    def is_development(self) -> bool:
        return self.app_env.lower() == "development"

    @property
    def is_production(self) -> bool:
        return self.app_env.lower() == "production"

    @property
    def cors_origin_list(self) -> list[str]:
        """解析 CORS_ORIGINS 环境变量为列表。"""
        if self.cors_origins:
            return [o.strip() for o in self.cors_origins.split(",") if o.strip()]
        return ["*"] if self.is_development else []

    # ------------------------------------------------------------------
    # 启动校验：生产环境禁止 CORS_ORIGINS 为空或包含 "*"
    # ------------------------------------------------------------------

    @model_validator(mode="after")
    def _validate_production_cors(self) -> "Settings":
        """生产环境拒绝以下三种非法配置：
        - CORS_ORIGINS="" （为空）
        - CORS_ORIGINS="*"
        - CORS_ORIGINS="https://a.com, *" （任意一项为 "*"）

        对齐 phase7-main.md §4 实现基线与 §17.3 外部依赖确认单。
        """
        if self.app_env.lower() != "production":
            return self
        origins = [o.strip() for o in (self.cors_origins or "").split(",") if o.strip()]
        if not origins:
            raise ValueError(
                "CORS_ORIGINS must not be empty in production. "
                "Set it to concrete origins, e.g. https://admin.example.com"
            )
        if any(o == "*" for o in origins):
            raise ValueError(
                "CORS_ORIGINS must not contain '*' in production "
                "(even when mixed with concrete origins)."
            )
        return self


settings = Settings()
