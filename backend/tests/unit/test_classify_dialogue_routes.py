"""阶段二 classify_dialogue 三种模式 + fallback 路径单元测试。

覆盖 source 标签：legacy / v2_dual_read / v2_fallback_legacy；
mode=off / shadow / dual_read 各一条；hash 桶 / 白名单各一条。
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.core.exceptions import LLMParseError
from app.llm.base import DialogueParseResult, IntentResult
from app.schemas.conversation import SessionState
from app.services import intent_service
from app.services.intent_service import (
    DialogueRouteResult,
    classify_dialogue,
)


def _session(**kwargs) -> SessionState:
    base = dict(role="worker", search_criteria={})
    base.update(kwargs)
    return SessionState(**base)


def _v2_payload(**kwargs) -> DialogueParseResult:
    base = dict(
        dialogue_act="start_search",
        frame_hint="job_search",
        slots_delta={"city": ["北京市"], "job_category": ["餐饮"]},
        merge_hint={},
        needs_clarification=False,
        confidence=0.9,
        conflict_action=None,
    )
    base.update(kwargs)
    return DialogueParseResult(**base)


class _FakeExtractor:
    def __init__(self, *, raise_v2: Exception | None = None,
                 v2_result: DialogueParseResult | None = None,
                 legacy_result: IntentResult | None = None):
        self.raise_v2 = raise_v2
        self.v2_result = v2_result or _v2_payload()
        self.legacy_result = legacy_result or IntentResult(
            intent="search_job",
            structured_data={"city": "北京市", "job_category": "餐饮"},
            confidence=0.9,
        )

    def extract(self, text, role, history=None, current_criteria=None,
                session_hint=None):
        return self.legacy_result.model_copy(deep=True)

    def extract_dialogue(self, text, role, history=None, current_criteria=None,
                         session_hint=None):
        if self.raise_v2:
            raise self.raise_v2
        return self.v2_result.model_copy(deep=True)


@pytest.fixture
def restore_settings():
    """每个 test 跑完恢复关键 settings。"""
    snapshot = {
        "dialogue_v2_mode": settings.dialogue_v2_mode,
        "dialogue_v2_shadow_sample_rate": settings.dialogue_v2_shadow_sample_rate,
        "dialogue_v2_userid_whitelist": settings.dialogue_v2_userid_whitelist,
        "dialogue_v2_hash_buckets": settings.dialogue_v2_hash_buckets,
    }
    primary_pct = settings.dialogue_policy.primary_rollout_percentage
    yield
    for k, v in snapshot.items():
        setattr(settings, k, v)
    settings.dialogue_policy = settings.dialogue_policy.model_copy(
        update={"primary_rollout_percentage": primary_pct},
    )


def test_mode_off_returns_legacy(restore_settings):
    settings.dialogue_v2_mode = "off"
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-1",
        )
    assert result.source == "legacy"
    assert result.decision is None
    assert result.intent_result.intent == "search_job"


def test_mode_shadow_main_path_is_legacy_v2_runs_for_logging(restore_settings):
    settings.dialogue_v2_mode = "shadow"
    settings.dialogue_v2_shadow_sample_rate = 1.0  # 100% 采样
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-1",
        )
    # shadow 模式主路由仍走 legacy
    assert result.source == "legacy"
    assert result.decision is None


def test_mode_dual_read_whitelist_hits_v2(restore_settings):
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-test-1"
    settings.dialogue_v2_hash_buckets = 0
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    assert result.source == "v2_dual_read"
    assert result.decision is not None
    assert result.decision.dialogue_act == "start_search"


def test_mode_dual_read_not_in_whitelist_falls_back_to_legacy(restore_settings):
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-other"
    settings.dialogue_v2_hash_buckets = 0
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    assert result.source == "legacy"


def test_mode_dual_read_hash_bucket_can_hit(restore_settings):
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 100  # 全员命中
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-anybody",
        )
    assert result.source == "v2_dual_read"


def test_mode_dual_read_v2_parse_failure_falls_back(restore_settings):
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-test-1"
    settings.dialogue_v2_hash_buckets = 0
    extractor = _FakeExtractor(raise_v2=LLMParseError("boom"))
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    # 关键：不递归调 classify_intent，直接回 _classify_intent_legacy 内核
    assert result.source == "v2_fallback_legacy"
    assert result.decision is None
    assert result.intent_result.intent == "search_job"


def test_mode_dual_read_v2_not_implemented_falls_back(restore_settings):
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-test-1"
    settings.dialogue_v2_hash_buckets = 0
    extractor = _FakeExtractor(raise_v2=NotImplementedError("legacy provider"))
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    assert result.source == "v2_fallback_legacy"


def test_dialogue_v2_hash_buckets_clamped_to_100(restore_settings):
    """adversarial review I14：buckets > 100 不能静默全量灰度。"""
    from app.config import Settings
    s = Settings(dialogue_v2_hash_buckets=200)
    assert s.dialogue_v2_hash_buckets == 100
    s2 = Settings(dialogue_v2_hash_buckets=-5)
    assert s2.dialogue_v2_hash_buckets == 0


def test_dialogue_v2_mode_validator_rejects_invalid(restore_settings):
    from app.config import Settings
    # 阶段四 PR2：primary 已添加为合法占位（PR3 接通灰度桶后才生效）。
    # 其它非法值仍回退 off。
    s = Settings(dialogue_v2_mode="primary")
    assert s.dialogue_v2_mode == "primary"
    s2 = Settings(dialogue_v2_mode="bogus")
    assert s2.dialogue_v2_mode == "off"
    s3 = Settings(dialogue_v2_mode="")
    assert s3.dialogue_v2_mode == "off"


def test_session_none_always_legacy(restore_settings):
    """session=None 不应触发 v2，避免破坏没有 session 的旧调用点。"""
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-test-1"
    extractor = _FakeExtractor()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=None, userid="u-test-1",
        )
    assert result.source == "legacy"


# ---------------------------------------------------------------------------
# codex review P4：phased-plan §2.3 三类事件齐全（dialogue_v2_parse 独立）
# ---------------------------------------------------------------------------

def _captured_log_events(monkeypatch):
    """patch app.tasks.common.log_event 捕获事件列表。intent_service 通过
    `from app.tasks.common import log_event` 在模块级别绑定，所以也得 patch
    intent_service.log_event。"""
    captured: list[tuple[str, dict]] = []

    def _fake(event_type: str, **kwargs):
        captured.append((event_type, kwargs))

    import app.tasks.common as _common
    monkeypatch.setattr(_common, "log_event", _fake)
    monkeypatch.setattr(intent_service, "log_event", _fake)
    return captured


def test_dual_read_emits_dialogue_v2_parse_then_decision(restore_settings, monkeypatch):
    """dual_read 路径：先发 dialogue_v2_parse，再发 dialogue_v2_decision。"""
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-test-1"
    settings.dialogue_v2_hash_buckets = 0

    captured = _captured_log_events(monkeypatch)
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    assert result.source == "v2_dual_read"
    types = [t for t, _ in captured]
    assert "dialogue_v2_parse" in types
    assert "dialogue_v2_decision" in types
    # 顺序：parse 必须在 decision 之前
    assert types.index("dialogue_v2_parse") < types.index("dialogue_v2_decision")
    parse_kwargs = next(kw for t, kw in captured if t == "dialogue_v2_parse")
    # 字段：dialogue_act / frame_hint / slots_delta_keys / merge_hint_keys / mode / prompt_version
    assert parse_kwargs["dialogue_act"] == "start_search"
    assert parse_kwargs["frame_hint"] == "job_search"
    assert parse_kwargs["mode"] == "dual_read"
    assert parse_kwargs["prompt_version"]  # 非空字符串
    assert isinstance(parse_kwargs["slots_delta_keys"], list)


def test_shadow_emits_dialogue_v2_parse_then_legacy_diff(restore_settings, monkeypatch):
    """shadow 路径：旁路调 v2 时也要先发 dialogue_v2_parse，再发 dialogue_v2_legacy_diff。"""
    settings.dialogue_v2_mode = "shadow"
    settings.dialogue_v2_shadow_sample_rate = 1.0  # 100% 采样

    captured = _captured_log_events(monkeypatch)
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    types = [t for t, _ in captured]
    assert "dialogue_v2_parse" in types
    assert "dialogue_v2_legacy_diff" in types
    assert types.index("dialogue_v2_parse") < types.index("dialogue_v2_legacy_diff")
    parse_kwargs = next(kw for t, kw in captured if t == "dialogue_v2_parse")
    assert parse_kwargs["mode"] == "shadow"


def test_v2_parse_failure_does_not_emit_parse_event(restore_settings, monkeypatch):
    """parse 失败应只发 dialogue_v2_fallback_to_legacy（dual_read）/
    dialogue_v2_parse_error（shadow），不应发 dialogue_v2_parse。"""
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-test-1"
    settings.dialogue_v2_hash_buckets = 0

    captured = _captured_log_events(monkeypatch)
    extractor = _FakeExtractor(raise_v2=LLMParseError("boom"))
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    types = [t for t, _ in captured]
    assert "dialogue_v2_parse" not in types
    assert "dialogue_v2_fallback_to_legacy" in types


# ---------------------------------------------------------------------------
# 阶段四 PR3：primary 分支灰度入口测试
#
# 关键不变量：
# 1. mode=primary + 命中 primary_rollout_percentage 桶 → source=v2_primary
# 2. mode=primary + 未命中 primary 桶 + 在 dual_read 白名单 → source=v2_dual_read
#    （fallthrough 到 dual_read 既有逻辑，保留 95% 用户的 v2 观察窗口）
# 3. mode=primary + 未命中任何桶 / 白名单 → source=legacy
# 4. mode=primary + 命中 primary 桶 + v2 抛异常 → source=v2_primary_fallback_legacy
# ---------------------------------------------------------------------------


def _set_primary_rollout(percentage: int) -> None:
    """设置 primary_rollout_percentage（PR2 嵌套字段，无顶层 setter）。"""
    settings.dialogue_policy = settings.dialogue_policy.model_copy(
        update={"primary_rollout_percentage": percentage},
    )


def test_mode_primary_hit_rollout_routes_to_v2_primary(restore_settings):
    """primary 桶命中 → source=v2_primary（与 dual_read 共用 v2 派生路径）。"""
    settings.dialogue_v2_mode = "primary"
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 0
    _set_primary_rollout(100)  # 100% → 任何 userid 都命中
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-anybody",
        )
    assert result.source == "v2_primary"
    assert result.decision is not None
    assert result.decision.dialogue_act == "start_search"


def test_mode_primary_miss_rollout_falls_to_legacy_not_dual_read(restore_settings):
    """codex review 修订（PR4 P2-1）：primary 未命中桶 → **直接 legacy**，
    不 fallthrough 到 dual_read（即便用户在 dual_read 白名单内）。

    与 phased-plan §4.1.1「primary mode：新 DTO 是 source of truth，不再
    dual-read 切换」+ §4.1.6「立即回滚到 dual-read」（mode 切换语义）一致。
    回滚通过 dialogue_v2_mode 切换驱动，不通过 primary 内部 fallthrough。
    """
    settings.dialogue_v2_mode = "primary"
    _set_primary_rollout(0)  # 0% → 永不命中 primary
    settings.dialogue_v2_userid_whitelist = "u-test-1"  # 即便在 dual_read 白名单
    settings.dialogue_v2_hash_buckets = 0
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    # 不再 fallthrough 到 v2_dual_read
    assert result.source == "legacy"
    assert result.decision is None


def test_mode_dual_read_not_invoked_in_primary_mode(restore_settings):
    """primary 模式下 dual_read 桶 / 白名单都不生效（必须显式切 mode=dual_read）。"""
    settings.dialogue_v2_mode = "primary"
    _set_primary_rollout(0)
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 100  # dual_read 桶设满
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-anybody",
        )
    # primary 模式不评估 dual_read 桶，未命中 primary → legacy
    assert result.source == "legacy"


def test_mode_primary_miss_all_buckets_falls_to_legacy(restore_settings):
    """primary 未命中 + 不在 dual_read 白名单/桶 → source=legacy（最终兜底）。"""
    settings.dialogue_v2_mode = "primary"
    _set_primary_rollout(0)
    settings.dialogue_v2_userid_whitelist = "u-other"  # 不含本测用户
    settings.dialogue_v2_hash_buckets = 0
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    assert result.source == "legacy"
    assert result.decision is None


def test_mode_primary_v2_exception_falls_back_to_legacy(restore_settings):
    """primary 命中但 v2 抛异常 → source=v2_primary_fallback_legacy + 不抛 500。

    严格约束：fallback 只调 _classify_intent_legacy 内核（已是非递归内核），
    不调 classify_intent 顶层入口；避免 primary 路径产生递归。
    """
    settings.dialogue_v2_mode = "primary"
    _set_primary_rollout(100)
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 0
    extractor = _FakeExtractor(raise_v2=LLMParseError("boom"))
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    assert result.source == "v2_primary_fallback_legacy"
    assert result.decision is None
    # legacy 内核派生的 IntentResult 仍然有效
    assert result.intent_result.intent == "search_job"


def test_mode_primary_emits_dialogue_v2_primary_route_event(restore_settings, monkeypatch):
    """primary 命中桶时必须 emit dialogue_v2_primary_route 埋点供大盘观察。"""
    settings.dialogue_v2_mode = "primary"
    _set_primary_rollout(100)
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 0

    captured = _captured_log_events(monkeypatch)
    extractor = _FakeExtractor()
    s = _session()
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        classify_dialogue(
            "西安找服务员", "worker", history=[], session=s, userid="u-test-1",
        )
    types = [t for t, _ in captured]
    assert "dialogue_v2_primary_route" in types
    primary_kwargs = next(kw for t, kw in captured if t == "dialogue_v2_primary_route")
    assert primary_kwargs["percentage"] == 100
