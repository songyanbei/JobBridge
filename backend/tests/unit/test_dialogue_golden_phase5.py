"""Phase 5 dialogue 回归 golden cases。

phased-plan §5.1.4 验收 #3：on 模式下 paginate_no_more 路径全绿。

5.1 子阶段引入 2 条 case：
- worker 翻完 → 建议放宽薪资 / 换附近城市 / 切换工种大类
- broker 翻完 → candidate_search frame 用 salary_ceiling 文案

case 在后续子阶段不得删除（§跨阶段共同约束 5），只能升级断言。
"""
from __future__ import annotations

import pytest

from tests.fixtures.dialogue_golden import (
    broker_show_more_exhausted_paginate,
    worker_relaxation_offer_accept,
    worker_relaxation_offer_reject,
    worker_show_more_exhausted_paginate,
    worker_zero_result_auto_relax,
    worker_zero_result_no_relax_steps,
)
from tests.fixtures.dialogue_golden.runner import (
    assert_turn,
    run_dialogue_case,
)


@pytest.mark.parametrize(
    "case",
    [
        # 5.1 paginate_no_more
        worker_show_more_exhausted_paginate.CASE,
        broker_show_more_exhausted_paginate.CASE,
        # 5.2 0/低召回策略
        worker_zero_result_auto_relax.CASE,
        worker_zero_result_no_relax_steps.CASE,
        worker_relaxation_offer_accept.CASE,
        worker_relaxation_offer_reject.CASE,
    ],
    ids=lambda c: c["id"],
)
def test_dialogue_golden_phase5(case):
    """逐 turn 比对 Phase 5.1 paginate_no_more 行为。"""
    result = run_dialogue_case(case)
    assert len(result["turns"]) == len(case["turns"])
    for idx, (trace_turn, turn_def) in enumerate(zip(result["turns"], case["turns"])):
        assert_turn(trace_turn, turn_def["expect"], label=f"{case['id']}#{idx}")


# ---------------------------------------------------------------------------
# Phase 5 §5.1.4 验收 #1：off 模式逐字节等价 5.0 前路径
# ---------------------------------------------------------------------------


def test_phase5_off_mode_byte_equivalent_to_pre_5_1():
    """phased-plan §5.1.4 验收 #1：post_search_policy_mode=off 时 reply
    与 5.0 前完全相同（含 show_more 翻完仍是旧字符串"已经是所有匹配结果了..."）。
    """
    case = {
        **worker_show_more_exhausted_paginate.CASE,
        "id": "worker_show_more_exhausted_off_mode",
        "post_search_policy_mode": "off",  # 强制 off
        "turns": [
            {
                **worker_show_more_exhausted_paginate.CASE["turns"][0],
                "expect": {
                    "intent": "show_more",
                    # off 模式：reply 应是 fake_show_more 给的 mock 文案
                    # （不是 paginate header），且不出现新 directions 文案
                    "reply_includes_paginate_header": False,
                    "reply_contains": ["[mock-show-more-exhausted]"],
                    "reply_not_contains": [
                        "下调月薪下限 10%",
                        "本轮结果已经看完了。可以试试这些方向",
                    ],
                },
            },
        ],
    }
    result = run_dialogue_case(case)
    assert_turn(result["turns"][0], case["turns"][0]["expect"], label=case["id"])


def test_phase5_shadow_mode_does_not_affect_reply():
    """phased-plan §5.1.4 验收 #2：shadow 模式 reply 与 off 相同；reducer
    被调用并写日志，但不改回复（caplog 验证日志非空在 test_post_search_reducer
    覆盖；这里只断言 reply 等价 off）。
    """
    case = {
        **worker_show_more_exhausted_paginate.CASE,
        "id": "worker_show_more_exhausted_shadow_mode",
        "post_search_policy_mode": "shadow",
        "turns": [
            {
                **worker_show_more_exhausted_paginate.CASE["turns"][0],
                "expect": {
                    "intent": "show_more",
                    "reply_includes_paginate_header": False,
                    "reply_contains": ["[mock-show-more-exhausted]"],
                    "reply_not_contains": [
                        "下调月薪下限 10%",
                        "本轮结果已经看完了。可以试试这些方向",
                    ],
                },
            },
        ],
    }
    result = run_dialogue_case(case)
    assert_turn(result["turns"][0], case["turns"][0]["expect"], label=case["id"])
