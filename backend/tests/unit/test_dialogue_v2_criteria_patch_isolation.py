"""阶段四 PR3：v2 派生路径 criteria_patch 隔离单测。

dialogue-intent-extraction-phased-plan §4.1.4 + §PR3 收口要求：
- IntentResult.criteria_patch 字段保留 schema（旧 provider / legacy 序列化兼容）
- v2 派生路径（dual_read / primary）不消费 criteria_patch 的 op 语义
- 新 prompt（v2.7）一律输出 criteria_patch=[]，让 LLM 不再生成 op

本单测从两个角度锁住「v2 派生路径下 criteria_patch op 不会污染 session」：

A. dialogue_compat.decision_to_intent_result 输出的 IntentResult 必定 criteria_patch=[]
   （结构性保证，DialogueDecision 没有 patch 字段）

B. 即便 LLM 在 DialogueParseResult 里塞了「不该有」的 op-style 数据，
   compat 派生 + reducer 链路也不会把它放到最终 IntentResult.criteria_patch
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.services import intent_service
from app.services.intent_service import classify_dialogue


@pytest.fixture
def restore_dialogue_settings():
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


def _v2_payload_with_replace_intent() -> DialogueParseResult:
    """LLM 输出「替换 city」的 v2 payload。注意：DialogueParseResult schema 没有
    criteria_patch 字段 — 这是结构性的隔离。即便 LLM 在 raw response 里塞了 op,
    pydantic 校验会 drop 掉。"""
    return DialogueParseResult(
        dialogue_act="modify_search",
        frame_hint="job_search",
        slots_delta={"city": ["北京市"]},
        merge_hint={"city": "replace"},
        needs_clarification=False,
        confidence=0.9,
        conflict_action=None,
    )


class _FakeExtractor:
    def __init__(self, v2_payload):
        self.v2_payload = v2_payload

    def extract(self, *args, **kwargs):
        # 不会被调（v2 primary 命中路径直接走 extract_dialogue）
        raise AssertionError("legacy extract should not be called in v2 primary path")

    def extract_dialogue(self, *args, **kwargs):
        return self.v2_payload.model_copy(deep=True)


def test_v2_primary_path_emits_empty_criteria_patch(restore_dialogue_settings):
    """v2 primary 路径下，最终 IntentResult.criteria_patch 必定为 []，
    无论 LLM 输出什么样的语义信号。

    这是 dialogue_compat.decision_to_intent_result 的结构性约束：DialogueDecision
    的 final_search_criteria 走 structured_data 全量快照，criteria_patch 一律 [].
    """
    settings.dialogue_v2_mode = "primary"
    settings.dialogue_policy = settings.dialogue_policy.model_copy(
        update={"primary_rollout_percentage": 100},
    )
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 0

    sess = SessionState(
        role="worker",
        search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
    )
    extractor = _FakeExtractor(v2_payload=_v2_payload_with_replace_intent())

    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "北京有吗", "worker", history=[], session=sess, userid="u-isolation",
        )

    assert result.source == "v2_primary"
    # 关键不变量：v2 派生 IntentResult.criteria_patch 必定 []
    assert result.intent_result.criteria_patch == [], (
        f"v2 primary path leaked criteria_patch op semantics: "
        f"{result.intent_result.criteria_patch}"
    )


def test_v2_primary_path_uses_reducer_decision_not_patch_ops(restore_dialogue_settings):
    """即便 session 已有 city=['西安市']，v2 reducer 应按 merge_hint=replace
    把 final_search_criteria.city 替换为 ['北京市']，且这个决策走的是 reducer
    的 final_search_criteria，不是任何 criteria_patch op。
    """
    settings.dialogue_v2_mode = "primary"
    settings.dialogue_policy = settings.dialogue_policy.model_copy(
        update={"primary_rollout_percentage": 100},
    )
    settings.dialogue_v2_userid_whitelist = ""
    settings.dialogue_v2_hash_buckets = 0
    # 让 acqp=replace（避免 reducer 强制 clarify）
    settings.ambiguous_city_query_policy = "replace"

    sess = SessionState(
        role="worker",
        search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
    )
    extractor = _FakeExtractor(v2_payload=_v2_payload_with_replace_intent())

    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "北京有吗", "worker", history=[], session=sess, userid="u-isolation",
        )

    assert result.source == "v2_primary"
    # decision.final_search_criteria 由 reducer 的 schema 裁决产生（非 patch op）
    decision = result.decision
    assert decision is not None
    assert decision.resolved_merge_policy.get("city") == "replace"
    # final_search_criteria.city 替换为 ["北京市"]（不是 ["西安市", "北京市"] 这种叠加）
    assert decision.final_search_criteria["city"] == ["北京市"]


def test_v2_dual_read_path_also_isolates_criteria_patch(restore_dialogue_settings):
    """同样的隔离应在 dual_read 路径生效（结构性约束，与 mode 无关）。"""
    settings.dialogue_v2_mode = "dual_read"
    settings.dialogue_v2_userid_whitelist = "u-dual-iso"
    settings.dialogue_v2_hash_buckets = 0

    sess = SessionState(role="worker", search_criteria={})
    extractor = _FakeExtractor(v2_payload=_v2_payload_with_replace_intent())

    with patch.object(intent_service, "get_intent_extractor", return_value=extractor):
        result = classify_dialogue(
            "北京找服务员", "worker", history=[], session=sess, userid="u-dual-iso",
        )

    assert result.source == "v2_dual_read"
    assert result.intent_result.criteria_patch == []
