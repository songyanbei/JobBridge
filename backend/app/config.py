"""应用配置，集中加载环境变量。

所有配置通过 pydantic-settings 从 .env 或环境变量加载。
其它模块统一 `from app.config import settings` 使用。
"""
import os
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---------------------------------------------------------------------------
# 阶段四 PR2（dialogue-intent-extraction-phased-plan §4.1.5）：
# 把对话策略类配置收敛到嵌套 DialoguePolicy 子结构。
# 旧顶层字段（dialogue_v2_mode 等）通过 @property + setter 向后转发，0 调用方改动。
# 旧 env 变量（DIALOGUE_V2_MODE 等）通过 _legacy_dialogue_env_to_policy
# model_validator 在构造前 hook 进 dialogue_policy，**旧名优先级 > 新名**
# （plan §4.1.5「旧名作为唯一权威源不变，新名只是补充」）。阶段五移除旧名。
# ---------------------------------------------------------------------------


class DialoguePolicy(BaseModel):
    """对话策略子配置（阶段四 PR2 引入）。

    PR2 阶段：旧顶层字段名仍是权威环境变量来源；本类提供结构化命名空间，
    供新代码读取（settings.dialogue_policy.v2_mode）。
    PR3 阶段：新增 primary_rollout_percentage 接通 primary 灰度桶。
    阶段五：旧顶层字段统一移除，本类成为唯一来源。
    """

    model_config = ConfigDict(extra="ignore")

    v2_mode: Literal["off", "shadow", "dual_read", "primary"] = "off"
    """v2 灰度模式。off=纯 legacy；shadow=旁路写日志；dual_read=白名单/桶命中走 v2；
    primary=阶段四 PR3 接通的主路径模式（命中 primary_rollout_percentage 桶走 v2）。

    **PR2 阶段注意**：``primary`` 已加入合法值集，但 classify_dialogue 还没有
    primary 分支（PR3 接通）。当前若设 v2_mode=primary，classify_dialogue 会
    落入「未匹配模式 → legacy」兜底分支，行为等价 off。设此值不会破坏任何路径，
    只是不会启用 primary 灰度行为。"""

    shadow_sample_rate: float = 0.05
    """shadow 模式旁路调 v2 的采样率，0..1。"""

    userid_whitelist: str = ""
    """dual_read 命中白名单（CSV）。"""

    hash_buckets: int = 0
    """dual_read 灰度 hash 桶数，0..100；0 = 不启用。"""

    primary_rollout_percentage: int = 0
    """阶段四 PR3 占位：primary 模式 hash 桶比例 0..100；0 = 不启用 primary。"""

    ambiguous_city_query_policy: Literal["clarify", "replace"] = "clarify"
    """「北京有吗」歧义策略：clarify 反问 / replace 直接换城市。"""

    low_confidence_threshold: float = 0.6
    """关键字段（city/job_category/salary_*）低置信度时强制反问的阈值。"""

    search_awaiting_ttl_seconds: int = 600
    """搜索追问字段 FIFO 队列过期时间；与上传草稿 TTL 独立可调。"""

    @field_validator("v2_mode", mode="before")
    @classmethod
    def _coerce_v2_mode(cls, v):
        v = (str(v) if v is not None else "").strip()
        return v if v in {"off", "shadow", "dual_read", "primary"} else "off"

    @field_validator("hash_buckets", "primary_rollout_percentage", mode="before")
    @classmethod
    def _clamp_pct(cls, v):
        try:
            v = int(v)
        except (TypeError, ValueError):
            return 0
        return max(0, min(100, v))

    @field_validator("ambiguous_city_query_policy", mode="before")
    @classmethod
    def _coerce_acqp(cls, v):
        v = (str(v) if v is not None else "").strip()
        return v if v in {"clarify", "replace"} else "clarify"


# 旧顶层 env 名 → DialoguePolicy 字段名
_LEGACY_DIALOGUE_FIELD_MAP = {
    "dialogue_v2_mode": "v2_mode",
    "dialogue_v2_shadow_sample_rate": "shadow_sample_rate",
    "dialogue_v2_userid_whitelist": "userid_whitelist",
    "dialogue_v2_hash_buckets": "hash_buckets",
    "ambiguous_city_query_policy": "ambiguous_city_query_policy",
    "low_confidence_threshold": "low_confidence_threshold",
    "search_awaiting_ttl_seconds": "search_awaiting_ttl_seconds",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=("../.env", ".env"),  # 优先读项目根目录，fallback 当前目录
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        # 阶段四 PR2：支持 DIALOGUE_POLICY__V2_MODE 这类嵌套环境变量名。
        env_nested_delimiter="__",
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

    # 强制首登改密。开发环境可关，生产环境必须保持 True，防止 admin/admin123
    # 默认账号未改密就能直接调业务接口（详见 phase7-release-report 上线 checklist）。
    admin_force_password_change: bool = True

    # 默认/弱口令黑名单（逗号分隔）。命中场景：
    #   1) 登录时 supplied password 命中 → 即便 password_changed=1 也强制重置为 0，
    #      下一步业务接口被 require_admin_password_changed 拦截
    #   2) 改密时 new_password 命中 → 直接 40101 拒绝
    # 默认仅 "admin123"（seed.sql 的初始口令）；运营可在 .env 加企业自有的弱口令。
    admin_default_passwords: str = "admin123"

    @property
    def admin_default_password_set(self) -> set[str]:
        """解析 ``admin_default_passwords`` 为 set；空字符串视为不启用。"""
        return {p.strip() for p in (self.admin_default_passwords or "").split(",") if p.strip()}

    # ---- 事件回传 API ----
    event_api_key: str = ""  # 小程序点击事件回传 API Key（生产环境每季度轮换）

    # ---- CORS ----
    cors_origins: str = ""  # 逗号分隔的允许域名列表，为空时开发环境允许全部，生产环境拒绝全部

    # ---- 阶段四 PR2（dialogue-intent-extraction-phased-plan §4.1.5）：对话策略子结构 ----
    # 默认全部走 DialoguePolicy 默认值：代码 / 配置 / 单测就位但不影响生产路由;
    # 上线后由 .env 切换。详见 DialoguePolicy 类与 _legacy_dialogue_env_to_policy
    # 文件顶部说明。**旧顶层字段名（dialogue_v2_mode 等）通过 @property + setter
    # 转发**，保持 0 调用方改动；旧 env 名（DIALOGUE_V2_MODE 等）由
    # _legacy_dialogue_env_to_policy 在构造前 hook 进 dialogue_policy。
    dialogue_policy: DialoguePolicy = Field(default_factory=DialoguePolicy)

    # 旧顶层字段 → dialogue_policy 转发（向后兼容；阶段五移除）
    @property
    def dialogue_v2_mode(self) -> str:
        return self.dialogue_policy.v2_mode

    @dialogue_v2_mode.setter
    def dialogue_v2_mode(self, value) -> None:
        # 经 DialoguePolicy.v2_mode 的 mode="before" validator 校验非法值会回落 off
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"v2_mode": DialoguePolicy._coerce_v2_mode(value)},
        )

    @property
    def dialogue_v2_shadow_sample_rate(self) -> float:
        return self.dialogue_policy.shadow_sample_rate

    @dialogue_v2_shadow_sample_rate.setter
    def dialogue_v2_shadow_sample_rate(self, value: float) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"shadow_sample_rate": float(value)},
        )

    @property
    def dialogue_v2_userid_whitelist(self) -> str:
        return self.dialogue_policy.userid_whitelist

    @dialogue_v2_userid_whitelist.setter
    def dialogue_v2_userid_whitelist(self, value: str) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"userid_whitelist": str(value or "")},
        )

    @property
    def dialogue_v2_hash_buckets(self) -> int:
        return self.dialogue_policy.hash_buckets

    @dialogue_v2_hash_buckets.setter
    def dialogue_v2_hash_buckets(self, value: int) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"hash_buckets": DialoguePolicy._clamp_pct(value)},
        )

    @property
    def ambiguous_city_query_policy(self) -> str:
        return self.dialogue_policy.ambiguous_city_query_policy

    @ambiguous_city_query_policy.setter
    def ambiguous_city_query_policy(self, value: str) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={
                "ambiguous_city_query_policy": DialoguePolicy._coerce_acqp(value),
            },
        )

    @property
    def low_confidence_threshold(self) -> float:
        return self.dialogue_policy.low_confidence_threshold

    @low_confidence_threshold.setter
    def low_confidence_threshold(self, value: float) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"low_confidence_threshold": float(value)},
        )

    @property
    def search_awaiting_ttl_seconds(self) -> int:
        return self.dialogue_policy.search_awaiting_ttl_seconds

    @search_awaiting_ttl_seconds.setter
    def search_awaiting_ttl_seconds(self, value: int) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"search_awaiting_ttl_seconds": int(value)},
        )

    @property
    def dialogue_v2_userid_whitelist_set(self) -> set[str]:
        """解析 dialogue_policy.userid_whitelist 为 set；空字符串视为不启用。"""
        return {
            u.strip()
            for u in (self.dialogue_policy.userid_whitelist or "").split(",")
            if u.strip()
        }

    @model_validator(mode="before")
    @classmethod
    def _legacy_dialogue_env_to_policy(cls, data):
        """阶段四 PR2 兼容层：把旧顶层字段名 / 旧 env 名映射到 dialogue_policy。

        优先级（plan §4.1.5「旧名作为唯一权威源不变，新名只是补充」）：
          旧 env > 旧 kwarg > 新 env / 新 kwarg

        旧 env 名（如 DIALOGUE_V2_MODE）在 PR2 仍是权威来源；新 env 名
        （DIALOGUE_POLICY__V2_MODE）由 pydantic-settings env_nested_delimiter
        原生支持。两者同时设置时旧名生效；阶段五移除旧名。
        """
        if not isinstance(data, dict):
            return data

        # 解析当前 dialogue_policy（可能来自 nested env、构造 kwarg、或缺省）
        policy_data = data.pop("dialogue_policy", None)
        if hasattr(policy_data, "model_dump"):
            policy_data = policy_data.model_dump()
        if not isinstance(policy_data, dict):
            policy_data = {}

        # 1. 旧 kwarg 名：构造 Settings(dialogue_v2_mode="x") 这类用法
        for old, new in _LEGACY_DIALOGUE_FIELD_MAP.items():
            if old in data:
                policy_data[new] = data.pop(old)

        # 2. 旧 env 名：pydantic-settings 因为 dialogue_v2_mode 不再是字段，
        # 不会自动加载 DIALOGUE_V2_MODE；这里直接读 os.environ 兜底。
        # 与 model_config.case_sensitive=False 契约对齐：upper / lower 都尝试
        # （Linux/Mac 上 os.environ 是 case-sensitive，pre-PR2 时 pydantic-settings
        # 会自动 case-insensitive 匹配字段，PR2 后我们必须手动覆盖两种大小写）。
        for old, new in _LEGACY_DIALOGUE_FIELD_MAP.items():
            for candidate in (old.upper(), old.lower()):
                env_value = os.environ.get(candidate)
                if env_value is not None:
                    # 旧 env > 新 env / 旧 kwarg：env 总是覆盖
                    policy_data[new] = env_value
                    break

        if policy_data:
            data["dialogue_policy"] = policy_data
        return data

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
