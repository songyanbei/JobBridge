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
    yield
    for k, v in snapshot.items():
        setattr(settings, k, v)


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
    s = Settings(dialogue_v2_mode="primary")  # 阶段四才允许，阶段二应回退 off
    assert s.dialogue_v2_mode == "off"


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
