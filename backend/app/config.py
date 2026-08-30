"""应用配置，集中加载环境变量。

所有配置通过 pydantic-settings 从 .env 或环境变量加载。
其它模块统一 `from app.config import settings` 使用。
"""
import os
from typing import Literal
from urllib.parse import quote, quote_plus

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

    post_search_policy_mode: Literal["off", "shadow", "on"] = "off"
    """Phase 5 §5.0：结果感知二阶段裁决（post_search_reduce）的灰度模式。
    off=不调 reducer，行为完全等价 5.0 前；
    shadow=旁路调 reducer 写日志，不影响 reply；
    on=按 PostSearchDecision.action 改写 reply / 触发 applier。
    5.0 子阶段默认 off，message_router 暂不消费；5.1 起接通。"""

    soft_preference_ranking_enabled: bool = False
    """Phase 5 §5.3：是否启用 reranker 软偏好排序。
    False（默认）→ search_service 调 reranker 时不传 soft_preferences/ranking_weights，
    严格走 v2.0 等价路径；
    True → 抽取 criteria 中软偏好字段 + 权重表（slot_schema.extract_soft_preferences）
    传给 reranker，走 v2.1 prompt。**5.3 默认关闭**：业务直觉权重未经真实日志验证；
    生产灰度由独立运营开关推全（phased-plan §5.4）。"""

    phase5_rollout_percentage: int = 0
    """Phase 5 §5.4：post_search_policy_mode + soft_preference_ranking_enabled
    联合灰度的 hash 桶比例，0..100。0=不启用 Phase 5 整体灰度。**5.4 默认 0**：
    代码就位但不真灰度（用户决策"开发期只做代码不推灰度"）；上线时按 phased-plan
    §5.4.1 第 4 项的阶梯（5%/25%/50%/100%）推全，任一阶段关键指标回退 ≥ 5%
    立即回滚到上一比例。"""

    recommendation_experience_enabled: bool = False
    """Phase 5 recommendation experience master switch.

    False is the highest-priority kill switch: recommendation reasons, shadow
    reason building, soft-preference ranking, soft-preference reasons, and the
    soft-preference notice must all resolve to disabled.
    """

    recommendation_reason_rollout_percentage: int = 0
    """User rollout percentage for writing match reasons into replies."""

    recommendation_reason_shadow_enabled: bool = False
    """Build recommendation reasons for structured logs only; never changes reply text."""

    soft_preference_ranking_rollout_percentage: int = 0
    """User rollout percentage for soft-preference ranking; the global bool remains the kill switch."""

    soft_preference_reason_rollout_percentage: int = 0
    """User rollout percentage for per-result soft-preference match reasons."""

    soft_preference_notice_rollout_percentage: int = 0
    """User rollout percentage for the overall soft-preference visibility notice."""

    @field_validator("v2_mode", mode="before")
    @classmethod
    def _coerce_v2_mode(cls, v):
        v = (str(v) if v is not None else "").strip()
        return v if v in {"off", "shadow", "dual_read", "primary"} else "off"

    @field_validator(
        "hash_buckets", "primary_rollout_percentage",
        "phase5_rollout_percentage",
        "recommendation_reason_rollout_percentage",
        "soft_preference_ranking_rollout_percentage",
        "soft_preference_reason_rollout_percentage",
        "soft_preference_notice_rollout_percentage", mode="before",
    )
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

    @field_validator("post_search_policy_mode", mode="before")
    @classmethod
    def _coerce_psm(cls, v):
        v = (str(v) if v is not None else "").strip()
        return v if v in {"off", "shadow", "on"} else "off"


# 旧顶层 env 名 → DialoguePolicy 字段名
_LEGACY_DIALOGUE_FIELD_MAP = {
    "dialogue_v2_mode": "v2_mode",
    "dialogue_v2_shadow_sample_rate": "shadow_sample_rate",
    "dialogue_v2_userid_whitelist": "userid_whitelist",
    "dialogue_v2_hash_buckets": "hash_buckets",
    "dialogue_v2_primary_rollout_percentage": "primary_rollout_percentage",
    "ambiguous_city_query_policy": "ambiguous_city_query_policy",
    "low_confidence_threshold": "low_confidence_threshold",
    "search_awaiting_ttl_seconds": "search_awaiting_ttl_seconds",
    "post_search_policy_mode": "post_search_policy_mode",
    "soft_preference_ranking_enabled": "soft_preference_ranking_enabled",
    "phase5_rollout_percentage": "phase5_rollout_percentage",
    "recommendation_experience_enabled": "recommendation_experience_enabled",
    "recommendation_reason_rollout_percentage": "recommendation_reason_rollout_percentage",
    "recommendation_reason_shadow_enabled": "recommendation_reason_shadow_enabled",
    "soft_preference_ranking_rollout_percentage": "soft_preference_ranking_rollout_percentage",
    "soft_preference_reason_rollout_percentage": "soft_preference_reason_rollout_percentage",
    "soft_preference_notice_rollout_percentage": "soft_preference_notice_rollout_percentage",
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

    # Phase 11 rollout gates.  All remain fail-closed until a later cutover
    # stage explicitly enables them; keeping them in the contract now lets
    # mixed-version app/worker fleets parse the same environment.
    resume_lifecycle_v2_enabled: bool = False
    resume_replacement_enabled: bool = False
    resume_expiry_cleanup_enabled: bool = False
    resume_candidate_cleanup_enabled: bool = False
    resume_hard_delete_enabled: bool = False
    ttl_resume_days: int = 30
    ttl_resume_candidate_days: int = 7
    phase11_build_number: int = 0
    phase11_build_sha: str = "0000000000000000000000000000000000000000"
    phase11_resume_writes_paused: bool = False

    @field_validator("ttl_resume_days", mode="after")
    @classmethod
    def _valid_resume_ttl(cls, value: int) -> int:
        value = int(value)
        if not 1 <= value <= 3650:
            raise ValueError("ttl_resume_days must be between 1 and 3650")
        return value

    @field_validator("ttl_resume_candidate_days", mode="after")
    @classmethod
    def _valid_resume_candidate_ttl(cls, value: int) -> int:
        value = int(value)
        if not 1 <= value <= 365:
            raise ValueError("ttl_resume_candidate_days must be between 1 and 365")
        return value

    @field_validator("phase11_build_number", mode="after")
    @classmethod
    def _valid_phase11_build_number(cls, value: int) -> int:
        value = int(value)
        if value < 0:
            raise ValueError("phase11_build_number must be non-negative")
        return value

    @field_validator("phase11_build_sha", mode="after")
    @classmethod
    def _valid_phase11_build_sha(cls, value: str) -> str:
        value = str(value).strip().lower()
        if len(value) != 40 or any(ch not in "0123456789abcdef" for ch in value):
            raise ValueError("phase11_build_sha must be a 40-character lowercase git SHA")
        return value

    # ---- 应用 ----
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_secret_key: str = "change-me"

    # Recruitment job-search facade.  The compatibility path remains the
    # default until an operator explicitly enables and rolls it out.
    job_search_facade_enabled: bool = False
    job_search_facade_rollout_percentage: int = 0
    job_search_facade_timeout_ms: int = 5000

    # Workstream A Action execution remains fail-closed until explicitly rolled out.
    action_execution_mode: Literal["off", "shadow", "on"] = "off"
    action_execution_rollout_percentage: int = 0
    action_execution_lease_seconds: int = 180
    action_replay_max_attempts: int = 5
    action_replay_stale_seconds: int = 3600
    action_parse_cache_ttl_seconds: int = 60
    action_parse_artifact_retention_seconds: int = 86400
    action_execution_search_enabled: bool = False
    action_show_more_enabled: bool = False
    action_relax_enabled: bool = False
    contact_service_mode: Literal["off", "shadow", "on"] = "off"
    action_execution_auto_kill_switch: bool = True
    monitor_action_stale_lease_max_age_seconds: int = 300
    monitor_action_replay_backlog_max_age_seconds: int = 600
    monitor_action_replay_backlog_threshold: int = 0
    monitor_action_missing_reference_threshold: int = 0

    @field_validator("job_search_facade_rollout_percentage", mode="after")
    @classmethod
    def _valid_job_search_facade_rollout(cls, value: int) -> int:
        value = int(value)
        if not 0 <= value <= 100:
            raise ValueError("job_search_facade_rollout_percentage must be between 0 and 100")
        return value

    @field_validator("job_search_facade_timeout_ms", mode="after")
    @classmethod
    def _valid_job_search_facade_timeout(cls, value: int) -> int:
        value = int(value)
        if value <= 0:
            raise ValueError("job_search_facade_timeout_ms must be positive")
        return value

    @field_validator("action_execution_rollout_percentage", mode="after")
    @classmethod
    def _valid_action_rollout(cls, value: int) -> int:
        value = int(value)
        if not 0 <= value <= 100:
            raise ValueError("action_execution_rollout_percentage must be between 0 and 100")
        return value

    @field_validator(
        "action_execution_lease_seconds",
        "action_replay_max_attempts",
        "action_replay_stale_seconds",
        "action_parse_cache_ttl_seconds",
        "action_parse_artifact_retention_seconds",
        "monitor_action_stale_lease_max_age_seconds",
        "monitor_action_replay_backlog_max_age_seconds",
        mode="after",
    )
    @classmethod
    def _valid_action_positive_limits(cls, value: int) -> int:
        value = int(value)
        if value <= 0:
            raise ValueError("action observation limits must be positive")
        return value

    @field_validator(
        "monitor_action_replay_backlog_threshold",
        "monitor_action_missing_reference_threshold",
        mode="after",
    )
    @classmethod
    def _valid_action_thresholds(cls, value: int) -> int:
        value = int(value)
        if value < 0:
            raise ValueError("action observation thresholds must be non-negative")
        return value

    # ---- 数据库 MySQL ----
    db_host: str = "localhost"
    db_port: int = 3306
    db_name: str = "jobbridge"
    db_user: str = "jobbridge"
    db_password: str = "jobbridge"
    db_connect_timeout_seconds: int = 2
    db_read_timeout_seconds: int = 5
    db_write_timeout_seconds: int = 5
    db_pool_timeout_seconds: int = 3

    @property
    def db_url(self) -> str:
        return (
            f"mysql+pymysql://{quote_plus(self.db_user)}:{quote_plus(self.db_password)}"
            f"@{self.db_host}:{self.db_port}/{quote_plus(self.db_name)}?charset=utf8mb4"
        )

    # ---- Redis ----
    redis_host: str = "localhost"
    redis_port: int = 6379
    redis_db: int = 0
    redis_password: str = ""
    redis_max_connections: int = 50  # 连接池上限，按并发量调整
    redis_connect_timeout_seconds: float = 1.0
    redis_socket_timeout_seconds: float = 2.0

    @property
    def redis_url(self) -> str:
        auth = f":{quote(self.redis_password, safe='')}@" if self.redis_password else ""
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
    llm_circuit_failure_threshold: int = 5
    llm_circuit_recovery_seconds: int = 30
    reranker_queue_degrade_threshold: int = 10

    # ---- 推荐策略 v1：shadow 双算 + LLM 调用预算（方案 §7.5 / §11.5）----
    # 这些都属于**运维配置**，不在推荐策略后台开放给运营编辑。

    recommendation_shadow_timeout_seconds: float = 3.0
    """shadow 双算的绝对超时预算（秒）。

    在**提交 runner 之前**换算成 ``deadline_monotonic``，因此这几秒里包含 runner
    排队、prompt 构造、建连、写入、服务端响应和读取；runner 真正开始执行时只剩
    remaining，不会重新拿到完整预算（§7.5）。"""

    recommendation_shadow_max_concurrency: int = 4
    """同一 provider 在**整个部署环境的全局并发上限**，不是每进程上限。

    进程内 semaphore 只是本地防护；真正的闸门是 Redis Lua 对
    ``recommendation:shadow:permits:{provider}`` 带过期 token ZSET 的原子
    prune/acquire（lease 5 秒，进程崩溃自动过期）。Redis 不可用时 shadow
    fail-closed 直接跳过，**绝不允许退化成"每进程各自并发 4"**——否则
    ``worker=N`` 横向扩容会线性放大供应商并发（§7.5）。"""

    recommendation_shadow_queue_capacity: int = 100
    """每进程 shadow runner 待执行队列容量。

    队列满时立即记 ``shadow_skipped_capacity`` 并放弃该候选，不得挤占前台 legacy
    的线程池或连接池。"""

    recommendation_shadow_persistence_threads: int = 2
    """每进程 shadow persistence executor 的线程数（§7.5）。

    event loop 回调只做 O(1) 非阻塞投递；真正写库在这些线程里，每个任务自建并
    关闭独立 ``SessionLocal``。禁止在 event loop 或 activate 调用线程里使用同步
    SQLAlchemy。"""

    recommendation_shadow_persistence_queue_capacity: int = 100
    """shadow persistence executor 队列容量；满时记 ``shadow_persistence_dropped``，
    只影响观测，不影响实际回复。"""

    recommendation_shadow_daily_token_budget_search_job: int = 200_000
    """search_job 方向的 shadow 日 token 预算（Asia/Shanghai 业务日）。

    按 §7.5 用 Redis Lua 原子预占
    ``recommendation:shadow:token_budget:{direction}:{business_date}``：
    reserve = 悲观 input tokens + 配置的 max output tokens，超预算直接跳过 shadow；
    拿到真实 usage 后可原子退回差额，**timeout/unknown 不退款**（provider 断连后
    仍可能完成推理并计费）。预算 Redis 不可用时同样跳过 shadow。

    这里的默认值只是"未经审批前的保守占位"，扩量前必须替换为审批后的方向级预算。"""

    recommendation_shadow_daily_token_budget_search_worker: int = 200_000
    """search_worker 方向的 shadow 日 token 预算，语义同上。"""

    recommendation_shadow_max_output_tokens: int = 1_024
    """shadow 单次调用预占预算时使用的悲观最大输出 token 数。

    provider 返回真实 usage 后会退回 ``reserve - actual``；timeout/unknown 不退款，
    因为客户端断开后供应商仍可能完成推理并计费。"""

    recommendation_strategy_kill_switch: bool = False
    """环境变量 ``RECOMMENDATION_STRATEGY_KILL_SWITCH``：进程启动时的更强 override。

    语义严格按 §7.5：

    - 日常事故处置的**真源是动态控制面** ``recommendation_runtime_control``
      （DB revision + Redis write-through + Pub/Sub，最大生效时间 5 秒）；
    - 本变量为 ``true`` 时在本进程内强制 off/legacy，压过 DB 里的任何取值；
    - 本变量为 ``false`` **永远不能覆盖 DB 里的 true**——它只是"不额外强制关闭"，
      不是"强制打开"；
    - 改 ``.env`` 需要滚动重启全部 App/Worker 才生效，**不是在线秒级开关**，
      不要在事故中把它当 kill switch 用。"""

    @field_validator(
        "recommendation_shadow_max_concurrency",
        "recommendation_shadow_queue_capacity",
        "recommendation_shadow_persistence_threads",
        "recommendation_shadow_persistence_queue_capacity",
        "recommendation_shadow_max_output_tokens",
        mode="after",
    )
    @classmethod
    def _at_least_one(cls, v: int) -> int:
        """0 / 负数会让 semaphore、队列和密钥版本静默失效，一律夹到 1。"""
        return max(1, int(v))

    @field_validator("recommendation_content_key_active_version", mode="after")
    @classmethod
    def _valid_recommendation_content_key_version(cls, v: int) -> int:
        """Keep key versions representable by the envelope and MySQL SMALLINT."""
        version = max(1, int(v))
        if version > 65_535:
            raise ValueError(
                "recommendation_content_key_active_version must be between 1 and 65535"
            )
        return version

    @field_validator("recommendation_shadow_timeout_seconds", mode="after")
    @classmethod
    def _positive_shadow_timeout(cls, v: float) -> float:
        """非正超时等于"每次都 deadline 已耗尽"，夹到一个仍会真实发起请求的下限。"""
        return max(0.1, float(v))

    @field_validator(
        "recommendation_shadow_daily_token_budget_search_job",
        "recommendation_shadow_daily_token_budget_search_worker",
        mode="after",
    )
    @classmethod
    def _non_negative_budget(cls, v: int) -> int:
        """0 是合法配置，含义是"该方向不跑 shadow"。"""
        return max(0, int(v))

    @property
    def recommendation_shadow_daily_token_budgets(self) -> dict[str, int]:
        """方向 → 日 token 预算。未知方向不在表里 = 没有预算 = 跳过 shadow。"""
        return {
            "search_job": max(0, int(self.recommendation_shadow_daily_token_budget_search_job)),
            "search_worker": max(
                0, int(self.recommendation_shadow_daily_token_budget_search_worker),
            ),
        }

    def recommendation_shadow_daily_token_budget(self, direction: str) -> int:
        """取某个推荐方向的日 token 预算；未知方向返回 0（fail-closed，不跑 shadow）。"""
        return self.recommendation_shadow_daily_token_budgets.get(direction, 0)

    # ---- 推荐正文加密密钥环（方案 §9.11）----
    # 密钥只来自 secrets/KMS；日志、审计和数据库不得保存明文密钥。
    # 轮换流程：先加新 key（写进 key ring）→ 切 active version → 等旧密文过期
    # → 再从 ring 里退役旧 key。旧 key 必须至少保留到对应 ciphertext 全部过期。

    recommendation_content_key: str = ""
    """当前 active version 对应的密钥材料（``RECOMMENDATION_CONTENT_KEY``）。

    生产/预发必须配置：缺失时投递侧无法加密正文。单 key 场景只配这一项即可，
    它会被登记为 ``recommendation_content_key_active_version`` 那一版。"""

    recommendation_content_key_ring: str = ""
    """只读 key ring，格式 ``version:material`` 的逗号分隔列表。

    例：``RECOMMENDATION_CONTENT_KEY_RING=1:old-secret,2:new-secret``。
    轮换期间必须同时含新旧两版，否则旧 ciphertext 解不开。密钥材料本身不能含
    英文逗号和冒号以外的分隔歧义（冒号只按**第一个**切分，材料里可以有冒号）。"""

    recommendation_content_key_active_version: int = 1
    """``RECOMMENDATION_CONTENT_KEY_ACTIVE_VERSION``：新写入 ciphertext 使用的版本号，
    会落到 ``content_key_version`` 列。解密一律按行上记录的版本回查 key ring。"""

    @property
    def recommendation_content_keys(self) -> dict[int, str]:
        """只读 key ring：``{version: 密钥材料}``。

        返回的是每次重新构建的副本，调用方改它不会影响进程配置。
        ``recommendation_content_key`` 作为 active version 的条目补进来，且
        **不覆盖** key ring 里已显式声明的同版本值（ring 是更明确的声明）。
        """
        ring: dict[int, str] = {}
        for entry in (self.recommendation_content_key_ring or "").split(","):
            entry = entry.strip()
            if not entry or ":" not in entry:
                continue
            raw_version, material = entry.split(":", 1)
            try:
                version = int(raw_version.strip())
            except (TypeError, ValueError):
                continue
            material = material.strip()
            if material:
                ring[version] = material
        active = int(self.recommendation_content_key_active_version)
        if self.recommendation_content_key and active not in ring:
            ring[active] = self.recommendation_content_key
        return ring

    @property
    def recommendation_content_key_configured(self) -> bool:
        """active version 是否有可用密钥材料。

        投递侧应在生产/预发环境用它做启动自检，把"首次投递才 RuntimeError"
        提前成部署期可见的失败。
        """
        return bool(
            self.recommendation_content_keys.get(
                int(self.recommendation_content_key_active_version),
            )
        )

    def recommendation_content_key_material(self, version: int | None = None) -> str:
        """取指定版本（默认 active）的密钥材料。

        Raises:
            RuntimeError: 该版本不在 key ring 中。加密侧说明未配置密钥；
                解密侧说明旧 key 被过早退役，此时**必须**报错而不是回退到任何
                硬编码常量——回退等于用固定密钥保护用户正文。
        """
        target = (
            int(self.recommendation_content_key_active_version)
            if version is None else int(version)
        )
        material = self.recommendation_content_keys.get(target)
        if not material:
            raise RuntimeError(
                f"recommendation content key version {target} is not configured; "
                "set RECOMMENDATION_CONTENT_KEY / RECOMMENDATION_CONTENT_KEY_RING"
            )
        return material

    # ---- 对象存储 ----
    oss_provider: str = "local"
    oss_endpoint: str = ""
    oss_access_key: str = ""
    oss_secret_key: str = ""
    oss_bucket: str = ""
    oss_local_dir: str = "uploads"           # 本地存储目录（oss_provider=local 时生效）
    oss_local_url_prefix: str = "/files"     # 本地文件 URL 前缀
    oss_trusted_origins: str = ""             # 允许反解为 object key 的历史访问域名，逗号分隔

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
    def dialogue_v2_primary_rollout_percentage(self) -> int:
        return self.dialogue_policy.primary_rollout_percentage

    @dialogue_v2_primary_rollout_percentage.setter
    def dialogue_v2_primary_rollout_percentage(self, value: int) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"primary_rollout_percentage": DialoguePolicy._clamp_pct(value)},
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
    def post_search_policy_mode(self) -> str:
        """Phase 5 §5.0：post_search_reduce 灰度模式（off/shadow/on）。"""
        return self.dialogue_policy.post_search_policy_mode

    @post_search_policy_mode.setter
    def post_search_policy_mode(self, value: str) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={
                "post_search_policy_mode": DialoguePolicy._coerce_psm(value),
            },
        )

    @property
    def soft_preference_ranking_enabled(self) -> bool:
        """Phase 5 §5.3：reranker 软偏好排序开关。"""
        return self.dialogue_policy.soft_preference_ranking_enabled

    @soft_preference_ranking_enabled.setter
    def soft_preference_ranking_enabled(self, value: bool) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"soft_preference_ranking_enabled": bool(value)},
        )

    @property
    def phase5_rollout_percentage(self) -> int:
        return self.dialogue_policy.phase5_rollout_percentage

    @phase5_rollout_percentage.setter
    def phase5_rollout_percentage(self, value: int) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"phase5_rollout_percentage": DialoguePolicy._clamp_pct(value)},
        )

    @property
    def recommendation_experience_enabled(self) -> bool:
        return self.dialogue_policy.recommendation_experience_enabled

    @recommendation_experience_enabled.setter
    def recommendation_experience_enabled(self, value: bool) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"recommendation_experience_enabled": bool(value)},
        )

    @property
    def recommendation_reason_rollout_percentage(self) -> int:
        return self.dialogue_policy.recommendation_reason_rollout_percentage

    @recommendation_reason_rollout_percentage.setter
    def recommendation_reason_rollout_percentage(self, value: int) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={
                "recommendation_reason_rollout_percentage": DialoguePolicy._clamp_pct(value),
            },
        )

    @property
    def recommendation_reason_shadow_enabled(self) -> bool:
        return self.dialogue_policy.recommendation_reason_shadow_enabled

    @recommendation_reason_shadow_enabled.setter
    def recommendation_reason_shadow_enabled(self, value: bool) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={"recommendation_reason_shadow_enabled": bool(value)},
        )

    @property
    def soft_preference_ranking_rollout_percentage(self) -> int:
        return self.dialogue_policy.soft_preference_ranking_rollout_percentage

    @soft_preference_ranking_rollout_percentage.setter
    def soft_preference_ranking_rollout_percentage(self, value: int) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={
                "soft_preference_ranking_rollout_percentage": DialoguePolicy._clamp_pct(value),
            },
        )

    @property
    def soft_preference_reason_rollout_percentage(self) -> int:
        return self.dialogue_policy.soft_preference_reason_rollout_percentage

    @soft_preference_reason_rollout_percentage.setter
    def soft_preference_reason_rollout_percentage(self, value: int) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={
                "soft_preference_reason_rollout_percentage": DialoguePolicy._clamp_pct(value),
            },
        )

    @property
    def soft_preference_notice_rollout_percentage(self) -> int:
        return self.dialogue_policy.soft_preference_notice_rollout_percentage

    @soft_preference_notice_rollout_percentage.setter
    def soft_preference_notice_rollout_percentage(self, value: int) -> None:
        self.dialogue_policy = self.dialogue_policy.model_copy(
            update={
                "soft_preference_notice_rollout_percentage": DialoguePolicy._clamp_pct(value),
            },
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
    job_replacement_enabled: bool = False
    job_expiry_cleanup_enabled: bool = False
    job_candidate_cleanup_enabled: bool = False
    job_hard_delete_enabled: bool = False
    daily_report_chat_id: str = ""  # 企微群 chatid；为空时日报/告警只打 loguru 不推送
    monitor_queue_incoming_threshold: int = 50
    monitor_queue_incoming_max_age_seconds: int = 120
    monitor_outbox_pending_max_age_seconds: int = 300
    monitor_session_commit_pending_max_age_seconds: int = 300
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
