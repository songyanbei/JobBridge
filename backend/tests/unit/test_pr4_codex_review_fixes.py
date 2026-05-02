"""阶段四 PR4：codex review 第二轮 P1+P2 修复验证测试。

复现 + 验收 4 个 finding 的修复：
- P1-1：v2 cancel/reset 不再走通用 command handler，走专用 _route_v2_cancel_reset
- P1-2：reducer 透传 LLM needs_clarification=True 到 decision.clarification
- P1-3：dropped slots + 业务 act → 强制 clarify（不再继续旧搜索条件）
- P2-1：primary 未命中桶 → legacy（不 fallthrough dual_read）已在 test_classify_dialogue_routes.py 覆盖
"""
from __future__ import annotations

import dataclasses
from unittest.mock import patch

import pytest

from app.config import settings
from app.llm.base import DialogueParseResult
from app.schemas.conversation import SessionState
from app.services import intent_service
from app.services.dialogue_reducer import reduce


# ---------------------------------------------------------------------------
# P1-2 / P1-3：reducer 单元层验证（不经 message_router）
# ---------------------------------------------------------------------------


def _parse(**kwargs) -> DialogueParseResult:
    base = dict(
        dialogue_act="modify_search",
        frame_hint="job_search",
        slots_delta={},
        merge_hint={},
        needs_clarification=False,
        confidence=0.9,
        conflict_action=None,
    )
    base.update(kwargs)
    return DialogueParseResult(**base)


class TestP1_2_NeedsClarificationPassthrough:
    """P1-2 修复：LLM needs_clarification=True 必须落到 decision.clarification。"""

    def test_llm_needs_clarification_creates_decision_clarification(self):
        """复现 review 报告：输入 needs_clarification=True，过去 decision.clarification=None,
        现在应有 clarification.kind='llm_requested'。"""
        s = SessionState(
            role="worker",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
        )
        d = reduce(
            _parse(
                dialogue_act="modify_search",
                slots_delta={"city": ["北京市"]},
                merge_hint={"city": "replace"},  # 明确 replace 避免触发 city_replace_or_add
                needs_clarification=True,  # ← 关键信号
                confidence=0.9,
            ),
            s, "worker", raw_text="北京",
        )
        # 修复后：decision.clarification 必须非空
        assert d.clarification is not None
        assert d.clarification["kind"] == "llm_requested"

    def test_reducer_specific_clarification_takes_priority_over_llm_signal(self):
        """如果 reducer 自己已经决定了具体 clarify kind（如 city_replace_or_add）,
        LLM needs_clarification=True 不应覆盖 — reducer 决策更具体优先。"""
        original = settings.ambiguous_city_query_policy
        settings.ambiguous_city_query_policy = "clarify"
        try:
            s = SessionState(
                role="worker", search_criteria={"city": ["西安市"]},
            )
            d = reduce(
                _parse(
                    dialogue_act="modify_search",
                    slots_delta={"city": ["北京市"]},
                    merge_hint={"city": "unknown"},  # 触发 city_replace_or_add
                    needs_clarification=True,  # 即便 LLM 也请求 clarify
                ),
                s, "worker", raw_text="北京",
            )
            assert d.clarification is not None
            assert d.clarification["kind"] == "city_replace_or_add"  # reducer 优先
        finally:
            settings.ambiguous_city_query_policy = original

    def test_no_llm_signal_no_reducer_decision_no_clarification(self):
        """LLM needs_clarification=False + reducer 也无歧义 → decision.clarification=None。"""
        s = SessionState(role="worker", search_criteria={})
        d = reduce(
            _parse(
                dialogue_act="start_search",
                slots_delta={"city": ["北京市"], "job_category": ["餐饮"]},
                needs_clarification=False,
                confidence=0.9,
            ),
            s, "worker", raw_text="北京餐饮",
        )
        assert d.clarification is None


class TestP1_3_DroppedSlotsClarify:
    """P1-3 修复：业务 act + slots 全被 schema drop → 强制 clarify。"""

    def test_unknown_field_dropped_with_business_act_triggers_clarify(self):
        """复现 review 报告：slots_delta={'unknown_field':'x'} + modify_search,
        过去仍按 follow_up 走旧 search_criteria，现在应走 clarify。"""
        s = SessionState(
            role="worker",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
        )
        d = reduce(
            _parse(
                dialogue_act="modify_search",
                slots_delta={"unknown_field": "x"},  # 全是非法字段
            ),
            s, "worker", raw_text="x",
        )
        # 修复后：accepted 空 + dropped 非空 + act 是业务动作 → clarification 必非空
        assert d.accepted_slots_delta == {}
        assert d.clarification is not None
        assert d.clarification["kind"] == "dropped_slots_no_valid"
        # state_transition 不应继续 enter_search_active
        assert d.state_transition == "none"

    def test_unknown_field_dropped_with_chitchat_act_no_clarify(self):
        """非业务 act（如 chitchat）即便全 drop 也不强制 clarify（与 plan §5 对齐）。"""
        s = SessionState(role="worker", search_criteria={})
        d = reduce(
            _parse(
                dialogue_act="chitchat",
                frame_hint="none",
                slots_delta={"unknown_field": "x"},
            ),
            s, "worker", raw_text="hi",
        )
        # chitchat 不是业务 act，drop 后不触发 dropped_slots_no_valid
        # （decision.clarification 可能为 None 或其它非 dropped_slots_no_valid kind）
        if d.clarification is not None:
            assert d.clarification["kind"] != "dropped_slots_no_valid"

    def test_partial_drop_with_some_valid_no_clarify(self):
        """部分 drop + 有有效字段保留 → 不触发 dropped_slots_no_valid 强制 clarify。"""
        s = SessionState(role="worker", search_criteria={})
        d = reduce(
            _parse(
                dialogue_act="start_search",
                slots_delta={
                    "city": ["北京市"],          # 合法保留
                    "unknown_field": "x",        # 被 drop
                },
            ),
            s, "worker", raw_text="北京",
        )
        # accepted 非空（city 保留）→ 不强制 clarify
        assert "city" in d.accepted_slots_delta
        if d.clarification is not None:
            assert d.clarification["kind"] != "dropped_slots_no_valid"


# ---------------------------------------------------------------------------
# P1-1：message_router 端到端验证 v2 cancel/reset 文案
# ---------------------------------------------------------------------------


def _make_msg(content: str, userid: str = "u-test") -> object:
    """构造最小 WeComMessage 用于 message_router 测试。"""
    from app.wecom.callback import WeComMessage
    return WeComMessage(
        msg_id=f"msg-{userid}",
        from_user=userid,
        msg_type="text",
        content=content,
        media_id=None,
        create_time=1700000000,
    )


class TestP1_1_V2CancelResetReply:
    """P1-1 修复：v2 cancel/reset 走专用 handler，文案基于 pre-apply session state 准确生成。"""

    def test_v2_cancel_in_upload_collecting_returns_ok_not_no_draft(self):
        """复现 review 报告：cancel 在 upload_collecting 下不再返回反向「当前没有可取消的草稿」。"""
        from app.services.dialogue_applier import apply_decision
        from app.services.message_router import _route_v2_cancel_reset
        from app.services.command_service import CANCEL_PENDING_OK, CANCEL_PENDING_NO_DRAFT
        from app.services.dialogue_reducer import DialogueDecision

        s = SessionState(
            role="worker",
            active_flow="upload_collecting",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="salary_floor_monthly",
        )
        decision = DialogueDecision(
            dialogue_act="cancel",
            resolved_frame="none",
            accepted_slots_delta={},
            resolved_merge_policy={},
            final_search_criteria={},
            missing_slots=[],
            route_intent="command",
            clarification=None,
            state_transition="clear_pending_upload",
            awaiting_ops=[],
        )
        # 模拟 message_router 的执行序：先 snapshot pre_state，再 apply，再调专用 handler
        pre_state = {
            "had_pending_upload": bool(s.pending_upload_intent),
            "had_search_state": bool(
                s.search_criteria or s.candidate_snapshot is not None or s.shown_items
            ),
            "active_flow": s.active_flow,
        }
        assert pre_state["had_pending_upload"] is True  # 前置确认
        apply_decision(decision, s)  # 清 session
        assert s.pending_upload_intent is None  # 确认清干净

        msg = _make_msg("取消")
        replies = _route_v2_cancel_reset(decision, pre_state, msg, s)
        assert len(replies) == 1
        # 修复后：基于 pre-state 渲染「已取消」，不是反向 NO_DRAFT
        assert replies[0].content == CANCEL_PENDING_OK
        assert replies[0].content != CANCEL_PENDING_NO_DRAFT

    def test_v2_reset_in_search_active_returns_success_not_empty(self):
        """复现 review 报告：reset 在 search_active 下不再返回反向「当前没有可清空的搜索条件」。"""
        from app.services.dialogue_applier import apply_decision
        from app.services.message_router import _route_v2_cancel_reset
        from app.services.command_service import RESET_SEARCH_SUCCESS, RESET_SEARCH_EMPTY
        from app.services.dialogue_reducer import DialogueDecision

        s = SessionState(
            role="worker",
            active_flow="search_active",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
        )
        decision = DialogueDecision(
            dialogue_act="reset",
            resolved_frame="none",
            accepted_slots_delta={},
            resolved_merge_policy={},
            final_search_criteria={},
            missing_slots=[],
            route_intent="command",
            clarification=None,
            state_transition="reset_search",
            awaiting_ops=[],
        )
        pre_state = {
            "had_pending_upload": bool(s.pending_upload_intent),
            "had_search_state": bool(
                s.search_criteria or s.candidate_snapshot is not None or s.shown_items
            ),
            "active_flow": s.active_flow,
        }
        assert pre_state["had_search_state"] is True  # 前置确认
        apply_decision(decision, s)  # 清 search_criteria
        assert s.search_criteria == {}

        msg = _make_msg("重新找")
        replies = _route_v2_cancel_reset(decision, pre_state, msg, s)
        assert len(replies) == 1
        # 修复后：基于 pre-state 渲染「已清空」，不是反向 EMPTY
        assert replies[0].content == RESET_SEARCH_SUCCESS
        assert replies[0].content != RESET_SEARCH_EMPTY

    def test_v2_reset_apply_decision_sets_active_flow_idle_in_same_turn(self):
        """codex review 第三轮：reset_search 在本轮 apply_decision 后 active_flow 必须
        立即落 idle，不依赖下一轮 load 时的 _self_heal_active_flow 修复。

        与「写入即一致」状态机口径对齐：apply_decision 是状态写入入口，应该一次性
        把所有相关字段都写到目标态。
        """
        from app.services.dialogue_applier import apply_decision
        from app.services.dialogue_reducer import DialogueDecision

        s = SessionState(
            role="worker",
            active_flow="search_active",
            search_criteria={"city": ["西安市"]},
            shown_items=["job-1"],
        )
        decision = DialogueDecision(
            dialogue_act="reset",
            resolved_frame="none",
            accepted_slots_delta={},
            resolved_merge_policy={},
            final_search_criteria={},
            missing_slots=[],
            route_intent="command",
            clarification=None,
            state_transition="reset_search",
            awaiting_ops=[],
        )
        apply_decision(decision, s)
        # 本轮写入即一致：所有 reset 相关字段都落到目标态
        assert s.search_criteria == {}
        assert s.candidate_snapshot is None
        assert s.shown_items == []
        # 关键：active_flow 必须立即落 idle，不留给下一轮 self-heal
        assert s.active_flow == "idle"

    def test_v2_cancel_in_idle_no_pending_returns_no_draft(self):
        """边界：v2 cancel 在 idle 且无 pending → 仍按 pre-state 给「无草稿可取消」（与 legacy 对齐）。"""
        from app.services.message_router import _route_v2_cancel_reset
        from app.services.command_service import CANCEL_PENDING_NO_DRAFT
        from app.services.dialogue_reducer import DialogueDecision

        s = SessionState(role="worker", active_flow="idle")
        decision = DialogueDecision(
            dialogue_act="cancel",
            resolved_frame="none",
            accepted_slots_delta={},
            resolved_merge_policy={},
            final_search_criteria={},
            missing_slots=[],
            route_intent="command",
            clarification=None,
            state_transition="clear_pending_upload",
            awaiting_ops=[],
        )
        pre_state = {
            "had_pending_upload": False,
            "had_search_state": False,
            "active_flow": "idle",
        }
        msg = _make_msg("取消")
        replies = _route_v2_cancel_reset(decision, pre_state, msg, s)
        assert replies[0].content == CANCEL_PENDING_NO_DRAFT
