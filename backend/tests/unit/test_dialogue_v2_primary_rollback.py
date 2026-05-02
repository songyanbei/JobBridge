"""阶段四 PR3：v2 primary → dual_read → off 回滚演练单测。

dialogue-intent-extraction-phased-plan §4.4「回滚演练」验收要求：
> 从 primary 切回 dual_read 一次，5 分钟内灰度比例归零，session 无残留状态错乱
> （用 worker / broker 两种角色各演练 1 次）。

本单测在进程内模拟该切换，验证：
1. 同一个 SessionState 在 mode 切换间被复用，不出现状态污染
2. 每次切换后，下一轮 classify_dialogue 按当时 mode 走对应路径
3. fallback 路径不递归调本入口（避免 primary 路径里的递归）
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.llm.base import DialogueParseResult, IntentResult
from app.schemas.conversation import SessionState
from app.services import intent_service
from app.services.intent_service import classify_dialogue


def _v2_payload() -> DialogueParseResult:
    return DialogueParseResult(
        dialogue_act="start_search",
        frame_hint="job_search",
        slots_delta={"city": ["北京市"], "job_category": ["餐饮"]},
        merge_hint={},
        needs_clarification=False,
        confidence=0.9,
        conflict_action=None,
    )


def _legacy_result() -> IntentResult:
    return IntentResult(
        intent="search_job",
        structured_data={"city": "北京市", "job_category": "餐饮"},
        confidence=0.9,
    )


class _FakeExtractor:
    def extract(self, text, role, history=None, current_criteria=None, session_hint=None):
        return _legacy_result()

    def extract_dialogue(self, text, role, history=None, current_criteria=None, session_hint=None):
        return _v2_payload()


@pytest.fixture
def restore_dialogue_settings():
    """快照所有对话相关 settings + dialogue_policy.primary_rollout_percentage。"""
    snap = {
        "dialogue_v2_mode": settings.dialogue_v2_mode,
        "dialogue_v2_userid_whitelist": settings.dialogue_v2_userid_whitelist,
        "dialogue_v2_hash_buckets": settings.dialogue_v2_hash_buckets,
    }
    pct = settings.dialogue_policy.primary_rollout_percentage
    yield
    for k, v in snap.items():
        setattr(settings, k, v)
    settings.dialogue_policy = settings.dialogue_policy.model_copy(
        update={"primary_rollout_percentage": pct},
    )


def _set_primary(percentage: int) -> None:
    settings.dialogue_policy = settings.dialogue_policy.model_copy(
        update={"primary_rollout_percentage": percentage},
    )


@pytest.mark.parametrize("role", ["worker", "broker"])
def test_primary_to_dual_read_to_off_no_session_residue(restore_dialogue_settings, role):
    """同一 SessionState 经历 primary → dual_read → off 三档切换，每档行为独立、
    session 字段无残留（不被前一档的 v2 决策物化污染）。
    """
    sess = SessionState(role=role, search_criteria={})
    extractor = _FakeExtractor()
    common_kwargs = dict(history=[], session=sess, userid="u-rollback-test")

    # === 第 1 档：primary 100% → 走 v2 primary ===
    settings.dialogue_v2_mode = "primary"
    _set_primary(100)
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 0
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        r1 = classify_dialogue("北京找服务员", role, **common_kwargs)
    assert r1.source == "v2_primary"
    assert r1.decision is not None

    # === 第 2 档：切 dual_read（primary_rollout_percentage 归零模拟回滚）===
    # 与 plan §4.1.6「灰度比例归零」对齐：primary_rollout_percentage = 0 即回退
    _set_primary(0)
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-rollback-test"
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        r2 = classify_dialogue("苏州找服务员", role, **common_kwargs)
    # primary 已切回 dual_read，本用户在白名单 → 走 v2_dual_read（不是 v2_primary）
    assert r2.source == "v2_dual_read"

    # === 第 3 档：切 off → 回 legacy ===
    settings.dialogue_v2_mode = "off"
    settings.dialogue_v2_userid_whitelist = ""
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        r3 = classify_dialogue("上海找服务员", role, **common_kwargs)
    assert r3.source == "legacy"
    assert r3.decision is None

    # session 字段无残留状态错乱：active_flow 应仍是合法值（None 默认 / idle /
    # search_active / upload_collecting / upload_conflict 之一）。
    # classify_dialogue 不直接物化 active_flow（由 message_router 经 applier 物化），
    # 在纯 classify_dialogue 测试流程下 active_flow 保持 None 是预期。
    assert sess.active_flow in {
        None, "idle", "search_active", "upload_collecting", "upload_conflict",
    }
    # search_criteria 同样由 applier 物化，本测不经 router → 应保持 fixture 初值
    assert sess.search_criteria == {}


def test_primary_disable_via_percentage_zero_immediate_takes_effect(restore_dialogue_settings):
    """plan §4.4「5 分钟内灰度比例归零」核心契约：
    把 primary_rollout_percentage 从 100 调回 0 后，下一轮请求立即不再命中 primary。
    """
    sess = SessionState(role="worker", search_criteria={})
    extractor = _FakeExtractor()
    settings.dialogue_v2_mode = "primary"
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 0

    # primary 100% → 命中
    _set_primary(100)
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        r1 = classify_dialogue(
            "北京找服务员", "worker", history=[], session=sess, userid="u-x",
        )
    assert r1.source == "v2_primary"

    # primary 0% → 立即不命中，且没有 dual_read 桶 → 回 legacy
    _set_primary(0)
    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        r2 = classify_dialogue(
            "北京找服务员", "worker", history=[], session=sess, userid="u-x",
        )
    assert r2.source == "legacy"


def test_primary_fallback_legacy_does_not_recurse(restore_dialogue_settings):
    """plan §4.1.2 关键约束：primary 路径下 v2 异常 → fallback 直接调
    _classify_intent_legacy 内核，不调 classify_intent 顶层入口（避免递归）。

    本测试用 monkeypatch 的方式：让 classify_intent 顶层入口 raise（如果被调用,
    立即失败），从而验证 fallback 路径只走内核。
    """
    sess = SessionState(role="worker", search_criteria={})

    class _RaiseV2Extractor(_FakeExtractor):
        def extract_dialogue(self, *args, **kwargs):
            from app.core.exceptions import LLMParseError
            raise LLMParseError("primary v2 fail")

    extractor = _RaiseV2Extractor()

    settings.dialogue_v2_mode = "primary"
    _set_primary(100)
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 0

    # 让 classify_intent 顶层入口失败（如果被调），用以证明 fallback 不递归
    def _classify_intent_should_not_be_called(*args, **kwargs):
        raise AssertionError(
            "classify_intent 顶层入口被调用！fallback 必须直接调 _classify_intent_legacy "
            "内核，不可递归回到顶层入口（plan §4.1.2 约束）",
        )

    with patch.object(intent_service, "get_intent_extractor", return_value=extractor), \
         patch.object(intent_service, "classify_intent",
                      side_effect=_classify_intent_should_not_be_called):
        result = classify_dialogue(
            "北京找服务员", "worker", history=[], session=sess, userid="u-x",
        )
    # 走到 fallback：source=v2_primary_fallback_legacy，且没有触发 classify_intent
    assert result.source == "v2_primary_fallback_legacy"
    assert result.intent_result.intent == "search_job"  # 来自 _classify_intent_legacy
