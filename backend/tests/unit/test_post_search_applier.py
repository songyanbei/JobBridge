"""post_search_applier 单元测试（Phase 5 §5.1）。

5.1 验收覆盖：
- no_action / paginate_no_more / ask_clarification 三个分支正确渲染
- 未实现 action（show_results_with_soft_pref_notice 等）fallback 到 no_action +
  打 post_search_unsupported_action 日志
- ReplyMessage userid 字段透传正确
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.schemas.search import SearchOutcome, SearchResult
from app.services.dialogue_reducer import DialogueDecision
from app.services.post_search_applier import apply_post_search_decision
from app.services.post_search_reducer import (
    PostSearchContext,
    PostSearchDecision,
)


# ---------------------------------------------------------------------------
# helper
# ---------------------------------------------------------------------------

def _make_msg(userid: str = "u-1", content: str = "更多") -> MagicMock:
    msg = MagicMock()
    msg.from_user = userid
    msg.content = content
    msg.msg_id = "m-1"
    return msg


def _make_user_ctx(role: str = "worker") -> MagicMock:
    uc = MagicMock()
    uc.role = role
    return uc


def _make_parse() -> DialogueParseResult:
    return DialogueParseResult(
        dialogue_act="chitchat",
        frame_hint="none",
        slots_delta={},
        merge_hint={},
        needs_clarification=False,
        confidence=0.0,
    )


def _make_decision_dto() -> DialogueDecision:
    return DialogueDecision(
        dialogue_act="chitchat",
        resolved_frame="none",
        route_intent="show_more",
    )


def _make_ctx(
    *,
    decision: PostSearchDecision,
    reply_text: str = "原始 reply",
    role: str = "worker",
    snapshot_exhausted: bool = False,
    direction: str = "search_job",
) -> PostSearchContext:
    return PostSearchContext(
        decision=decision,
        search_result=SearchResult(reply_text=reply_text, has_more=False, result_count=0),
        search_outcome=SearchOutcome(
            direction=direction,
            criteria_used={"city": ["北京市"]},
            initial_count=0,
            final_count=0,
            desired_count=3,
            low_recall_threshold=3,
            snapshot_exhausted=snapshot_exhausted,
        ),
        parse_result=_make_parse(),
        dialogue_decision=_make_decision_dto(),
        session=SessionState(role=role, search_criteria={"city": ["北京市"]}),
        msg=_make_msg(),
        user_ctx=_make_user_ctx(role=role),
        db=MagicMock(),
        raw_query="更多",
        role=role,
        recursion_depth=0,
    )


# ---------------------------------------------------------------------------
# 分支：no_action
# ---------------------------------------------------------------------------


class TestNoAction:
    def test_passthrough_search_result_reply_text(self):
        ctx = _make_ctx(
            decision=PostSearchDecision(action="no_action"),
            reply_text="原始搜索回复",
        )
        replies = apply_post_search_decision(ctx)
        assert len(replies) == 1
        assert replies[0].content == "原始搜索回复"
        assert replies[0].userid == "u-1"


# ---------------------------------------------------------------------------
# 分支：paginate_no_more
# ---------------------------------------------------------------------------


class TestPaginateNoMore:
    def test_renders_directions_as_bullets(self):
        decision = PostSearchDecision(
            action="paginate_no_more",
            suggested_directions=[
                {"dimension": "city", "hint_text": "换附近城市", "target_field": "city"},
                {"dimension": "job_category", "hint_text": "切换工种大类",
                 "target_field": "job_category"},
            ],
        )
        ctx = _make_ctx(decision=decision)
        replies = apply_post_search_decision(ctx)
        assert len(replies) == 1
        text = replies[0].content
        assert "已经是所有匹配结果了" in text
        assert "换附近城市" in text
        assert "切换工种大类" in text
        # 应该是项目符号格式
        assert "- 换附近城市" in text

    def test_empty_directions_falls_back_to_static(self):
        decision = PostSearchDecision(
            action="paginate_no_more",
            suggested_directions=[],
        )
        ctx = _make_ctx(decision=decision)
        replies = apply_post_search_decision(ctx)
        # criteria 极简 fallback 文案
        assert "换城市或工种重新搜索" in replies[0].content


# ---------------------------------------------------------------------------
# 分支：ask_clarification（5.1 桩，5.2 才会被 reducer 真正触发）
# ---------------------------------------------------------------------------


class TestAskClarificationStub:
    def test_renders_question_directly(self):
        # Phase 5.2：renderer 直接用 clarification.question（不再用模板包装）
        # 因为 reducer 已经通过 slot_schema.relax_step_human_label 拼好了业务文案
        decision = PostSearchDecision(
            action="ask_clarification",
            clarification={"question": "要把薪资放宽 10% 重新搜索吗？"},
        )
        ctx = _make_ctx(decision=decision)
        replies = apply_post_search_decision(ctx)
        assert "要把薪资放宽 10% 重新搜索吗？" in replies[0].content

    def test_missing_question_falls_back_to_search_result(self):
        decision = PostSearchDecision(
            action="ask_clarification",
            clarification={},
        )
        ctx = _make_ctx(decision=decision, reply_text="兜底文案")
        replies = apply_post_search_decision(ctx)
        assert replies[0].content == "兜底文案"


# ---------------------------------------------------------------------------
# 防御：未实现的 action（5.1 不输出，但 reducer 误输出时必须 fallback）
# ---------------------------------------------------------------------------


class TestUnsupportedActionFallback:
    """phased-plan §5.1.4 验收 #6：reducer 输出 show_results_with_soft_pref_notice
    等本子阶段未实现的 action 时 applier fallback 到 no_action + 日志告警。"""

    @pytest.mark.parametrize("unsupported", [
        # Phase 5.2 起 auto_relax_and_retry / suggest_relaxation 已真接通；
        # 仅剩 show_results_with_soft_pref_notice / show_results 留给 5.4。
        "show_results_with_soft_pref_notice",
        "show_results",
    ])
    def test_unsupported_action_falls_back_to_no_action(self, unsupported, caplog):
        decision = PostSearchDecision(action=unsupported)
        ctx = _make_ctx(decision=decision, reply_text="原始回复")
        with caplog.at_level(logging.WARNING):
            replies = apply_post_search_decision(ctx)
        # 应直出 search_result.reply_text
        assert replies[0].content == "原始回复"
        # 必须打 warning 日志
        assert any(
            "post_search_unsupported_action" in record.message
            for record in caplog.records
        ), f"expected post_search_unsupported_action warning, got: {[r.message for r in caplog.records]}"
