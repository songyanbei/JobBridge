"""Phase 1 dialogue 回归 golden cases。

按 docs/dialogue-intent-extraction-phased-plan.md §1.5 验收条件 1，
worker / broker 两条主路径必须长期防回退。Phase 2 接入新 DTO 时 fixture 不删除，
只能升级断言（详见 §跨阶段共同约束 5）。
"""
from __future__ import annotations

import pytest

from tests.fixtures.dialogue_golden import (
    broker_machinery_to_suzhou_replace,
    worker_xian_to_beijing_replace,
)
from tests.fixtures.dialogue_golden.runner import (
    assert_turn,
    run_dialogue_case,
)


@pytest.mark.parametrize(
    "case",
    [
        worker_xian_to_beijing_replace.CASE,
        broker_machinery_to_suzhou_replace.CASE,
    ],
    ids=lambda c: c["id"],
)
def test_dialogue_golden_phase1(case):
    """逐 turn 比对 Phase 1 阶段一可观测字段。"""
    result = run_dialogue_case(case)
    assert len(result["turns"]) == len(case["turns"])
    for idx, (trace_turn, turn_def) in enumerate(zip(result["turns"], case["turns"])):
        assert_turn(trace_turn, turn_def["expect"], label=f"{case['id']}#{idx}")


def test_assert_turn_rejects_unknown_keys():
    """Reviewer P2 回归：fixture 写错的 key 不能被静默忽略。"""
    fake_trace = {
        "intent": "search_job",
        "search_criteria": {},
        "awaiting_fields": [],
        "awaiting_frame": None,
        "ran_search": True,
        "handler": "_handle_search",
        "reply": "ok",
        "needs_clarification": False,
        "legacy_missing": [],
    }
    # 拼写错误的 key（typo: clarifcation vs clarification）必须 fail
    with pytest.raises(AssertionError, match="unknown expect keys"):
        assert_turn(fake_trace, {"intent": "search_job", "needs_clarifcation": False})

    # Reviewer P2 第二轮：missing_fields 已经从 _KNOWN_EXPECT_KEYS 移除，
    # 旧 fixture 误写 missing_fields 必须 fail，避免再次出现"看似覆盖、实际 no-op"。
    with pytest.raises(AssertionError, match="unknown expect keys.*missing_fields"):
        assert_turn(fake_trace, {"intent": "search_job", "missing_fields": []})
