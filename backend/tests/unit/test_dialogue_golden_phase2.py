"""Phase 2 dialogue 回归 golden cases（dialogue-intent-extraction-phased-plan §2.5）。

阶段一两条 happy path 在 mode=off 下由 test_dialogue_golden_phase1 覆盖；
本文件覆盖 Phase 2 v2 路径下的 7 条 case：
- happy 2 条（worker_xian_to_beijing_replace_v2 / broker_machinery_to_suzhou_replace_v2）
- 反例 5 条（clarify / role / conflict / parse_fail / low_conf / awaiting_expired）

阶段三 / 四 不能删 case，只能升级断言（跨阶段共同约束 5）。
"""
from __future__ import annotations

import pytest

from tests.fixtures.dialogue_golden import (
    active_flow_conflict_upload_to_search,
    awaiting_expired_no_pollution,
    broker_machinery_to_suzhou_replace_v2,
    cancel_during_upload_v2,
    llm_json_parse_failure_fallback,
    low_confidence_clarify,
    resolve_conflict_three_actions,
    role_permission_worker_upload,
    worker_xian_to_beijing_clarify,
    worker_xian_to_beijing_replace_v2,
)
from tests.fixtures.dialogue_golden.runner import (
    assert_turn,
    run_dialogue_case,
)


@pytest.mark.parametrize(
    "case",
    [
        worker_xian_to_beijing_replace_v2.CASE,
        broker_machinery_to_suzhou_replace_v2.CASE,
        worker_xian_to_beijing_clarify.CASE,
        role_permission_worker_upload.CASE,
        active_flow_conflict_upload_to_search.CASE,
        llm_json_parse_failure_fallback.CASE,
        low_confidence_clarify.CASE,
        awaiting_expired_no_pollution.CASE,
        # codex review P3 防回归：phased-plan §2.5.5 必测项
        cancel_during_upload_v2.CASE,
        resolve_conflict_three_actions.CASE_CANCEL,
        resolve_conflict_three_actions.CASE_RESUME,
        resolve_conflict_three_actions.CASE_PROCEED,
    ],
    ids=lambda c: c["id"],
)
def test_dialogue_golden_phase2(case):
    result = run_dialogue_case(case)
    assert len(result["turns"]) == len(case["turns"])
    for idx, (trace_turn, turn_def) in enumerate(zip(result["turns"], case["turns"])):
        assert_turn(trace_turn, turn_def["expect"], label=f"{case['id']}#{idx}")
