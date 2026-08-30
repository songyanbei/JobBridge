"""LLM 能力抽象层（对应方案 §4.3）。

业务代码只依赖本文件中的 ABC 和数据结构，不依赖具体 provider。
切换供应商只需在 llm/__init__.py 的工厂函数里改注册。
"""
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal, Any

from pydantic import BaseModel, Field, model_validator


@dataclass(frozen=True)
class LLMCallPolicy:
    """调用级 LLM 策略（方案 §11.5）。

    ``deadline_monotonic`` 是 ``time.monotonic()`` 坐标系下的**绝对时刻**，不是
    相对时长：§7.5 要求 shadow 在**提交 runner 之前**就把预算固定下来，排队等待
    必须一起消耗，用相对时长会让 worker 启动时重新拿到完整的 3 秒。
    一律用 monotonic 而不是 UTC wall clock，避免对时 / 闰秒把 deadline 抖成负数。

    ``deadline_monotonic=None``（也就是 ``call_policy=None`` 的默认值）表示
    **legacy 语义**：单次 HTTP timeout 用 ``settings.llm_timeout_seconds``（30 秒），
    网络错误最多重试一次（``max_retries=1``）。本次推荐改造不得改变这条基线。
    shadow 必须显式传 ``max_retries=0``。
    """

    deadline_monotonic: float | None = None
    max_retries: int = 1

    def remaining_seconds(self) -> float | None:
        """距离 deadline 还剩多少秒；无 deadline 时返回 None（不设总时限）。"""
        if self.deadline_monotonic is None:
            return None
        return self.deadline_monotonic - time.monotonic()

    @classmethod
    def with_deadline_in(
        cls, seconds: float, *, max_retries: int = 0,
    ) -> "LLMCallPolicy":
        """以"从现在起 N 秒"构造绝对 deadline。

        提供这个入口是为了让调用方（shadow runner）不必自己碰时钟，杜绝
        ``datetime.now()`` / wall clock 混入 deadline 计算。
        """
        return cls(
            deadline_monotonic=time.monotonic() + float(seconds),
            max_retries=max_retries,
        )


class LLMDeadlineExceeded(TimeoutError):
    """调用超出 ``LLMCallPolicy.deadline_monotonic`` 约定的绝对时限。

    继承 ``TimeoutError`` 而不是 ``LLMError``：它描述的是调用方给出的预算耗尽，
    不代表上游一定不可用（provider 很可能仍在推理并计费，见 §7.5 的预算口径）。
    """


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

class IntentResult(BaseModel):
    """IntentExtractor 的返回值。"""
    intent: str = Field(
        ...,
        description="意图类型: upload_job / upload_resume / search_job / search_worker / "
                    "upload_and_search / follow_up / show_more / command / chitchat",
    )
    structured_data: dict = Field(
        default_factory=dict,
        description="从用户文本中抽取出的结构化字段（对齐 §7 字段清单）",
    )
    criteria_patch: list[dict] = Field(
        default_factory=list,
        description="多轮对话的 criteria 增量更新指令列表，每项格式: "
                    '{"op": "add|update|remove", "field": "字段名", "value": "新值"}',
    )
    missing_fields: list[str] = Field(
        default_factory=list,
        description="缺失的必填字段列表（用于触发追问）",
    )
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="整体置信度 0-1",
    )
    raw_response: str = Field(
        default="",
        description="LLM 原始输出（调试 & 日志用）",
    )
    # Phase 7：token 用量（OpenAI 兼容响应的 usage.prompt_tokens / completion_tokens），
    # 由 provider 从响应体提取并回填；解析失败 / 无 usage 时保持 None。
    input_tokens: int | None = Field(default=None, description="prompt_tokens")
    output_tokens: int | None = Field(default=None, description="completion_tokens")


class DialogueParseResult(BaseModel):
    """阶段二 LLM 解析层 DTO（对应 docs/dialogue-intent-extraction-phased-plan §2.1.1）。

    LLM 只输出语言理解结果，不输出最终 merge_policy / 不写 session。
    后端裁决通过 dialogue_reducer.reduce 把本结构映射成 DialogueDecision。
    """

    dialogue_act: Literal[
        "start_search",
        "modify_search",
        "answer_missing_slot",
        "show_more",
        "start_upload",
        "cancel",
        "reset",
        "resolve_conflict",
        # Phase 5 §5.2：放宽确认（独立于 resolve_conflict，后者仅用于
        # upload_conflict 上下文）。系统上一轮反问"要把 X 放宽吗"且
        # session.pending_relaxation 非空时，本轮用户回应解析为该 act。
        "respond_relaxation_offer",
        "chitchat",
    ] = Field(..., description="本轮对话行为")
    frame_hint: Literal[
        "job_search",
        "candidate_search",
        "job_upload",
        "resume_upload",
        "none",
    ] = Field(default="none", description="本轮候选业务对象，仅作信号，不写 session")
    slots_delta: dict = Field(default_factory=dict, description="本轮抽到的字段变化")
    merge_hint: dict[str, Literal["replace", "add", "remove", "unknown"]] = Field(
        default_factory=dict,
        description=(
            "对 slots_delta 中 list 字段的合并意图："
            "明确表达替换 / 追加 / 删除 时给 replace/add/remove，"
            "裸值 / 模糊表达统一 unknown，由后端 reducer 决策"
        ),
    )
    needs_clarification: bool = Field(default=False, description="是否需要反问澄清")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    conflict_action: Literal[
        "cancel_draft",
        "resume_pending_upload",
        "proceed_with_new",
    ] | None = Field(
        default=None,
        description="仅 dialogue_act=resolve_conflict 时出现",
    )
    # Phase 5 §5.2：放宽确认场景的用户响应（accept/reject）。与 conflict_action
    # **独立**——前者属于 upload_conflict 流程，后者属于搜索放宽流程。
    relaxation_response: Literal["accept", "reject"] | None = Field(
        default=None,
        description="仅 dialogue_act=respond_relaxation_offer 时出现",
    )
    raw_response: str = Field(default="", description="LLM 原始输出（调试 & 日志用）")
    input_tokens: int | None = Field(default=None, description="prompt_tokens")
    output_tokens: int | None = Field(default=None, description="completion_tokens")

    @model_validator(mode="after")
    def _validate_action_fields_exclusive(self):
        """Phase 5 §第 8 轮 review fix 4：跨字段守护，避免 LLM 把
        conflict_action / relaxation_response 与错误的 dialogue_act 组合
        （reducer 主路径不会进入错误分支，但 prompt drift 时早 fail 比静默通过好）。
        """
        if self.conflict_action is not None and self.dialogue_act != "resolve_conflict":
            raise ValueError(
                f"conflict_action={self.conflict_action!r} requires "
                f"dialogue_act='resolve_conflict', got {self.dialogue_act!r}"
            )
        if (
            self.relaxation_response is not None
            and self.dialogue_act != "respond_relaxation_offer"
        ):
            raise ValueError(
                f"relaxation_response={self.relaxation_response!r} requires "
                f"dialogue_act='respond_relaxation_offer', got {self.dialogue_act!r}"
            )
        return self


class VersionedDialogueParse(BaseModel):
    """Internal, versioned envelope for Dialogue v1.

    Providers may still return the historical ``DialogueParseResult`` without a
    schema marker.  The adapter below adds the marker only inside the runtime;
    provider DTOs and their wire contracts remain unchanged.
    """

    schema_version: Literal["dialogue.v1"] = "dialogue.v1"
    result: DialogueParseResult
    profile: str = "recruitment.job"
    unknown_slots: list[str] = Field(default_factory=list)

    @property
    def dialogue_act(self) -> str:
        return self.result.dialogue_act

    def model_dump_result(self) -> dict[str, Any]:
        return self.result.model_dump(mode="json")


# Public alias used by adapters/tests while keeping the descriptive name above.
DialogueV1Parse = VersionedDialogueParse


_DIALOGUE_V1_ACTS = {
    "start_search", "modify_search", "answer_missing_slot", "show_more",
    "start_upload", "cancel", "reset", "resolve_conflict",
    "respond_relaxation_offer", "chitchat",
}
_DIALOGUE_V1_FRAMES = {
    "job_search", "candidate_search", "job_upload", "resume_upload", "none",
}
_DIALOGUE_V1_SLOTS = {
    "city", "job_category", "salary_floor_monthly", "salary_ceiling_monthly",
    "shift_pattern", "employment_type", "experience_years", "education",
    "gender", "age", "name", "description", "job_title",
    "years_experience", "expected_salary", "work_years", "skill_tags",
}
_DIALOGUE_V1_UPLOAD_SLOTS = _DIALOGUE_V1_SLOTS | {
    # Controlled upload extraction only.  These keys are never valid for
    # search/follow-up frames and are encrypted before persistence.
    "contact_person", "phone",
}


def _profile_slots(profile: str, frame: str) -> set[str]:
    """Resolve slot keys for the active profile and frame.

    Frame-flat schemas are intentional: a slot valid for resume upload must not
    become valid merely because it happens to share a name with another frame.
    """
    if profile != "recruitment.job":
        return set()
    try:
        from app.dialogue import slot_schema
        return set(slot_schema.fields_for(frame))
    except Exception:
        if frame == "job_upload":
            return set(_DIALOGUE_V1_UPLOAD_SLOTS)
        return set(_DIALOGUE_V1_SLOTS)


def adapt_dialogue_parse(
    value: DialogueParseResult | VersionedDialogueParse | dict[str, Any],
    *,
    profile: str = "recruitment.job",
) -> VersionedDialogueParse:
    """Validate a provider result at the v1 boundary.

    Missing ``schema_version`` is the sole compatibility case.  Unknown top
    level protocol values and unknown slots are rejected so callers can record a
    fallback and invoke the legacy parser.  No input object is mutated.
    """
    if isinstance(value, VersionedDialogueParse):
        if value.schema_version != "dialogue.v1":
            raise ValueError(f"unsupported dialogue schema: {value.schema_version}")
        data = value.result.model_dump(mode="python")
        effective_profile = value.profile or profile
    elif isinstance(value, DialogueParseResult):
        data = value.model_dump(mode="python")
        effective_profile = profile
    elif isinstance(value, dict):
        raw = dict(value)
        schema = raw.pop("schema_version", "dialogue.v1")
        if schema != "dialogue.v1":
            raise ValueError(f"unsupported dialogue schema: {schema}")
        data = raw
        effective_profile = profile
    else:
        raise TypeError(f"unsupported dialogue parse type: {type(value).__name__}")

    if effective_profile != "recruitment.job":
        raise ValueError(f"unsupported dialogue profile: {effective_profile!r}")

    act = data.get("dialogue_act")
    if act not in _DIALOGUE_V1_ACTS:
        raise ValueError(f"unknown dialogue_act: {act!r}")
    frame = data.get("frame_hint", "none")
    if frame not in _DIALOGUE_V1_FRAMES:
        raise ValueError(f"unknown frame_hint: {frame!r}")
    slots = data.get("slots_delta") or {}
    if not isinstance(slots, dict):
        raise ValueError("slots_delta must be an object")
    known_slots = _profile_slots(effective_profile, frame)
    unknown = sorted(str(k) for k in slots if k not in known_slots)
    if unknown:
        raise ValueError(f"unknown dialogue slots: {', '.join(unknown)}")
    parse = DialogueParseResult.model_validate(data)
    return VersionedDialogueParse(
        schema_version="dialogue.v1", result=parse, profile=effective_profile,
    )


class RerankResult(BaseModel):
    """Reranker 的返回值。"""
    ranked_items: list[dict] = Field(
        default_factory=list,
        description="排序后的候选集，每项含 id + score + 原始字段",
    )
    reply_text: str = Field(
        default="",
        description="LLM 生成的自然语言推荐回复（已按 §10.5 格式化）",
    )
    raw_response: str = Field(
        default="",
        description="LLM 原始输出",
    )
    # Phase 7：同 IntentResult。
    input_tokens: int | None = Field(default=None, description="prompt_tokens")
    output_tokens: int | None = Field(default=None, description="completion_tokens")
    # Serving-attempt telemetry. Providers may populate retry_count; the
    # search wrapper always normalizes status/latency/fallback before the result
    # is attached to recommendation_request/recommendation_search_attempt.
    llm_status: str = Field(default="ok")
    retry_count: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    ranking_fallback: str | None = Field(default=None)


# ---------------------------------------------------------------------------
# 抽象接口
# ---------------------------------------------------------------------------

class IntentExtractor(ABC):
    """意图抽取档：把用户自由文本解析为结构化 JSON + 意图分类。

    职责：
    - 判断 intent 类型（上传 / 检索 / 追问 / 闲聊 / 命令）
    - 从文本中抽取结构化字段
    - 检查必填字段缺失
    - 生成 criteria patch（多轮对话场景）
    """

    @abstractmethod
    def extract(
        self,
        text: str,
        role: str,
        history: list[dict] | None = None,
        current_criteria: dict | None = None,
        session_hint: dict | None = None,
    ) -> IntentResult:
        """解析一条用户消息。

        Args:
            text: 用户原始文本
            role: 用户角色 (worker / factory / broker)
            history: 最近 N 轮对话历史 [{"role":"user","content":"..."}, ...]
            current_criteria: 当前会话的累积检索条件（多轮 merge 用）
            session_hint: 当前会话状态摘要（active_flow / awaiting_fields / search_criteria 等）；
                Phase 1 起 provider 应把它结构化拼入 system prompt，未实现的旧
                provider 可忽略不报错。

        Returns:
            IntentResult
        """
        ...

    def extract_dialogue(
        self,
        text: str,
        role: str,
        history: list[dict] | None = None,
        current_criteria: dict | None = None,
        session_hint: dict | None = None,
    ) -> "DialogueParseResult":
        """阶段二新增：解析为 DialogueParseResult。

        默认实现 raise NotImplementedError；旧 provider 不强制实现。
        生产路径在 dialogue_v2_mode != off 时才会调用本方法；
        遇到 NotImplementedError / LLMParseError 时由 classify_dialogue 回退到
        _classify_intent_legacy 内核（避免递归），见 phased-plan §2.3。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement extract_dialogue; "
            "set dialogue_v2_mode=off or upgrade the provider."
        )


class Reranker(ABC):
    """重排生成档：对候选集语义排序 + 生成自然语言推荐回复。"""

    @abstractmethod
    def rerank(
        self,
        query: str,
        candidates: list[dict],
        role: str,
        top_n: int = 3,
        *,
        soft_preferences: dict | None = None,
        ranking_weights: dict[str, float] | None = None,
        call_policy: LLMCallPolicy | None = None,
    ) -> RerankResult:
        """对候选集重排并生成回复。

        Args:
            query: 用户原始检索文本
            candidates: SQL 硬过滤后的候选集（字典列表，含全部字段）
            role: 用户角色（决定回复视角和可见字段）
            top_n: 返回的 Top N 条数
            soft_preferences: Phase 5 §5.0 预声明位（5.3 才会消费）。例如
                ``{"provide_meal": True, "shift_pattern": "日班"}``。5.0/5.1/5.2
                所有 provider 实现接收形参但忽略，确保后续接通时不破坏现有调用。
            ranking_weights: Phase 5 §5.0 预声明位（5.3 才会消费）。例如
                ``{"provide_meal": 0.3, "shift_pattern": 0.2}``。
            call_policy: 调用级 timeout / 重试配置，``None`` = legacy
                （30 秒单次 timeout + 一次网络重试）。**同步路径只用它统一接口和
                调用级重试次数**：httpx 的同步 timeout 是分阶段的（connect/read/
                write/pool 各自计时），不能兑现严格的总 deadline。需要硬 deadline
                的 shadow 必须调 :meth:`arerank`（方案 §11.5）。

        Returns:
            RerankResult
        """
        ...

    async def arerank(
        self,
        query: str,
        candidates: list[dict],
        role: str,
        top_n: int = 3,
        *,
        soft_preferences: dict | None = None,
        ranking_weights: dict[str, float] | None = None,
        call_policy: LLMCallPolicy,
    ) -> RerankResult:
        """真异步重排：唯一能兑现 ``call_policy`` 绝对 deadline 的入口。

        默认实现直接 raise：方案 §7.5 明文**禁止**用 ThreadPoolExecutor /
        ``asyncio.to_thread`` 包同步 :meth:`rerank` 来实现 shadow 硬超时——
        deadline 到期后那条同步 socket 仍在后台线程上跑满 30 秒 × 2，既不释放
        并发槽也继续消耗供应商配额。provider 必须基于共享 ``httpx.AsyncClient``
        自己实现真异步路径。
        """
        raise NotImplementedError(
            f"{self.__class__.__name__} does not implement arerank; a shadow-capable "
            "provider must issue a real async HTTP call (wrapping the synchronous "
            "rerank in a thread is forbidden by the hard-deadline contract)."
        )
