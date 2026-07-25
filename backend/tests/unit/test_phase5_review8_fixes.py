"""Phase 5 第 8 轮 review 修复验证。

四项修复：
1. pending_relaxation.expires_at 真接 TTL 检查（previously 写了不读）
2. _v2_turn_context module-level dict 换 contextvars.ContextVar（previously 多线程 footgun）
3. assert 换 RuntimeError（previously -O 模式下守护失效）
4. DialogueParseResult 跨字段 validator（previously LLM 输出错误组合不报错）
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from app.config import settings


class TestRelaxationClosedSetFallback:
    """v2 关闭/失败时，系统二选一后的精确短回答仍能完成确认。"""

    @pytest.mark.parametrize("text", ["好", "好的。", "可以！", "同意", "确认"])
    def test_exact_accept_phrases(self, text):
        from app.schemas.conversation import SessionState
        from app.services.message_router import _match_pending_relaxation_response

        session = SessionState(role="worker", pending_relaxation={"step": "x"})
        assert _match_pending_relaxation_response(text, session) == "apply_relaxation"

    @pytest.mark.parametrize("text", ["不要", "算了。", "取消", "保持原条件", "否"])
    def test_exact_reject_phrases(self, text):
        from app.schemas.conversation import SessionState
        from app.services.message_router import _match_pending_relaxation_response

        session = SessionState(role="worker", pending_relaxation={"step": "x"})
        assert _match_pending_relaxation_response(text, session) == "cancel_relaxation"

    @pytest.mark.parametrize(
        "text",
        ["不要取消搜索", "可以先看看别的吗", "好的但是先别放宽", "取消草稿"],
    )
    def test_complex_sentences_do_not_trigger_exact_fallback(self, text):
        from app.schemas.conversation import SessionState
        from app.services.message_router import _match_pending_relaxation_response

        session = SessionState(role="worker", pending_relaxation={"step": "x"})
        assert _match_pending_relaxation_response(text, session) is None

    def test_no_pending_never_matches(self):
        from app.schemas.conversation import SessionState
        from app.services.message_router import _match_pending_relaxation_response

        assert _match_pending_relaxation_response(
            "好的", SessionState(role="worker"),
        ) is None

    def test_handle_text_short_circuits_classifier_when_exact_reply_matches(self):
        from app.schemas.conversation import ReplyMessage, SessionState
        from app.services import message_router

        session = SessionState(
            role="worker",
            pending_relaxation={
                "frame": "job_search",
                "direction": "search_job",
                "step": "relax_salary_10pct",
            },
        )
        msg = MagicMock(
            from_user="u1", content="好的", msg_id="m1", msg_type="text",
        )
        user_ctx = MagicMock(role="worker", should_welcome=False)

        with patch.object(
            message_router.conversation_service, "load_session", return_value=session,
        ), patch.object(
            message_router.conversation_service, "save_session",
        ), patch.object(
            message_router, "classify_intent",
        ) as mock_classify, patch.object(
            message_router,
            "_route_v2_relaxation_response",
            return_value=[ReplyMessage(userid="u1", content="已放宽")],
        ) as mock_route:
            replies = message_router._handle_text(msg, user_ctx, MagicMock())

        mock_classify.assert_not_called()
        assert mock_route.call_args.args[0].state_transition == "apply_relaxation"
        assert replies[0].content == "已放宽"


class TestComplexActionGuard:
    @pytest.mark.parametrize(
        "text",
        [
            "先帮我找苏州普工，不行再看无锡",
            "找岗位，顺便把我的简历也发了",
            "先发布这个岗位，然后找两个焊工",
        ],
    )
    def test_two_action_plan_is_bounded_not_rejected(self, text):
        from app.services.message_router import (
            _extract_bounded_action_plan,
            _requires_action_plan_clarification,
        )

        assert _extract_bounded_action_plan(text) is not None
        assert _requires_action_plan_clarification(text) is False

    def test_three_action_plan_requires_clarification(self):
        from app.services.message_router import _requires_action_plan_clarification

        assert _requires_action_plan_clarification(
            "先找北京普工，不行再看无锡，顺便把我的简历发了",
        ) is True

    @pytest.mark.parametrize("text", ["先找工人", "再看看", "苏州也可以"])
    def test_single_action_phrases_do_not_trigger_plan_guard(self, text):
        from app.services.message_router import _requires_action_plan_clarification

        assert _requires_action_plan_clarification(text) is False

    def test_handle_text_executes_first_and_persists_second(self):
        from app.llm.base import IntentResult
        from app.schemas.conversation import ReplyMessage, SessionState
        from app.services import message_router
        from app.wecom.callback import WeComMessage

        session = SessionState(role="worker", active_flow="idle")
        msg = WeComMessage(
            msg_id="m-plan", from_user="u1", msg_type="text",
            content="先找苏州普工，然后找杭州焊工岗位",
        )
        user_ctx = MagicMock(role="worker", should_welcome=False)
        with patch.object(
            message_router.conversation_service, "load_session", return_value=session,
        ), patch.object(
            message_router.conversation_service, "save_session",
        ), patch.object(
            message_router._settings_module.dialogue_policy, "v2_mode", "off",
        ), patch.object(
            message_router, "classify_intent",
            return_value=IntentResult(intent="search_job", confidence=0.9),
        ) as classify, patch.object(
            message_router, "_route_idle",
            return_value=[ReplyMessage(userid="u1", content="第一项已处理")],
        ) as route:
            replies = message_router._handle_text(msg, user_ctx, MagicMock())

        assert classify.call_args.kwargs["text"] == "找苏州普工"
        assert route.call_args.args[1].content == "找苏州普工"
        assert session.pending_action["raw_text"] == "找杭州焊工岗位"
        assert "已记住下一步" in replies[0].content

    def test_v2_clarification_short_return_still_announces_saved_second_action(self):
        from types import SimpleNamespace

        from app.llm.base import IntentResult
        from app.schemas.conversation import SessionState
        from app.services import message_router
        from app.services.dialogue_reducer import DialogueDecision
        from app.wecom.callback import WeComMessage

        session = SessionState(
            role="worker",
            active_flow="search_active",
            search_criteria={"city": ["北京市"], "job_category": ["普工"]},
        )
        msg = WeComMessage(
            msg_id="m-plan-clarify",
            from_user="u1",
            msg_type="text",
            content="先找苏州普工，然后找杭州焊工岗位",
        )
        decision = DialogueDecision(
            dialogue_act="modify_search",
            resolved_frame="job_search",
            route_intent="follow_up",
            clarification={
                "kind": "city_replace_or_add",
                "old_value": ["北京市"],
                "new_value": ["苏州市"],
            },
            state_transition="none",
        )
        route = SimpleNamespace(
            intent_result=IntentResult(intent="follow_up", confidence=0.9),
            decision=decision,
            source="v2_primary",
            parse_result=None,
        )
        user_ctx = MagicMock(role="worker", should_welcome=False)

        with patch.object(
            message_router.conversation_service, "load_session", return_value=session,
        ), patch.object(
            message_router.conversation_service, "save_session",
        ), patch.object(
            message_router._settings_module.dialogue_policy, "v2_mode", "primary",
        ), patch.object(
            message_router, "classify_dialogue", return_value=route,
        ):
            replies = message_router._handle_text(msg, user_ctx, MagicMock())
        message_router._clear_v2_turn_context()

        assert session.pending_action["raw_text"] == "找杭州焊工岗位"
        assert "已记住下一步" in replies[0].content
        assert "苏州市" in replies[0].content

    def test_consumed_pending_action_is_cleared_on_v2_clarification_short_return(self):
        from types import SimpleNamespace

        from app.llm.base import IntentResult
        from app.schemas.conversation import SessionState
        from app.services import message_router
        from app.services.dialogue_reducer import DialogueDecision
        from app.wecom.callback import WeComMessage

        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        session = SessionState(
            role="worker",
            active_flow="idle",
            pending_action={
                "raw_text": "找杭州焊工岗位",
                "expires_at": future,
            },
        )
        msg = WeComMessage(
            msg_id="m-pending-clarify",
            from_user="u1",
            msg_type="text",
            content="找杭州焊工岗位",
        )
        decision = DialogueDecision(
            dialogue_act="start_search",
            resolved_frame="job_search",
            route_intent="search_job",
            clarification={"kind": "llm_requested"},
            state_transition="none",
        )
        route = SimpleNamespace(
            intent_result=IntentResult(intent="search_job", confidence=0.7),
            decision=decision,
            source="v2_primary",
            parse_result=None,
        )
        user_ctx = MagicMock(role="worker", should_welcome=False)

        with patch.object(
            message_router.conversation_service, "load_session", return_value=session,
        ), patch.object(
            message_router.conversation_service, "save_session",
        ), patch.object(
            message_router._settings_module.dialogue_policy, "v2_mode", "primary",
        ), patch.object(
            message_router, "classify_dialogue", return_value=route,
        ):
            message_router._handle_text(msg, user_ctx, MagicMock())
        message_router._clear_v2_turn_context()

        assert session.pending_action is None

    def test_pending_action_round_trip_and_expiry(self):
        from app.schemas.conversation import SessionState
        from app.services.message_router import _is_pending_action_expired

        future = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
        session = SessionState(
            role="worker",
            pending_action={"raw_text": "找杭州焊工岗位", "expires_at": future},
        )
        restored = SessionState.model_validate(session.model_dump(mode="json"))
        assert restored.pending_action["raw_text"] == "找杭州焊工岗位"
        assert _is_pending_action_expired(restored.pending_action) is False


# ---------------------------------------------------------------------------
# Fix 1：pending_relaxation TTL 检查
# ---------------------------------------------------------------------------


class TestRelaxationTtl:
    """phased-plan §5.2.4 验收 #8：TTL 与清理路径必须覆盖"TTL 过期"场景。"""

    def test_is_relaxation_expired_past_returns_true(self):
        from app.services.message_router import _is_relaxation_expired
        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        assert _is_relaxation_expired(past) is True

    def test_is_relaxation_expired_future_returns_false(self):
        from app.services.message_router import _is_relaxation_expired
        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        assert _is_relaxation_expired(future) is False

    def test_is_relaxation_expired_naive_datetime_treated_as_utc(self):
        from app.services.message_router import _is_relaxation_expired
        past_naive = (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None).isoformat()
        assert _is_relaxation_expired(past_naive) is True

    def test_is_relaxation_expired_invalid_string_treated_as_expired(self):
        from app.services.message_router import _is_relaxation_expired
        assert _is_relaxation_expired("not-a-datetime") is True

    def test_is_relaxation_expired_empty_treated_as_not_expired(self):
        """没有 expires_at 字段（旧 session 兼容）按未过期处理。"""
        from app.services.message_router import _is_relaxation_expired
        assert _is_relaxation_expired("") is False

    def test_route_v2_relaxation_response_expired_clears_pending_and_returns_timeout_msg(self):
        """端到端：pending_relaxation 过期时 _route_v2_relaxation_response
        清状态 + 返回过期文案，不调 execute_relaxed_search。"""
        from app.services import message_router
        from app.schemas.conversation import SessionState
        from app.services.dialogue_reducer import DialogueDecision

        past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        session = SessionState(
            role="worker",
            pending_relaxation={
                "frame": "job_search",
                "direction": "search_job",
                "step": "relax_salary_10pct",
                "original_criteria": {"city": ["北京市"]},
                "relaxed_criteria": {"city": ["北京市"], "salary_floor_monthly": 4500},
                "raw_query": "北京餐饮 5000",
                "user_msg_id": "m-old",
                "expires_at": past,
            },
        )
        decision = DialogueDecision(
            dialogue_act="respond_relaxation_offer",
            resolved_frame="none",
            route_intent="follow_up",
            state_transition="apply_relaxation",
        )
        msg = MagicMock()
        msg.from_user = "u-1"
        msg.content = "好的"
        msg.msg_id = "m-new"
        user_ctx = MagicMock()
        user_ctx.role = "worker"

        with patch("app.services.search_service.execute_relaxed_search") as mock_exec:
            replies = message_router._route_v2_relaxation_response(
                decision, msg, user_ctx, session, MagicMock(),
            )
            # 关键：execute_relaxed_search 不应被调用
            mock_exec.assert_not_called()
        # session.pending_relaxation 已清
        assert session.pending_relaxation is None
        # reply 是过期文案
        assert "过期" in replies[0].content or "重新开始" in replies[0].content

    def test_route_v2_relaxation_response_not_expired_runs_search(self):
        """端到端：未过期时正常调 execute_relaxed_search。"""
        from app.services import message_router
        from app.schemas.conversation import SessionState
        from app.schemas.search import SearchResult, SearchOutcome
        from app.services.dialogue_reducer import DialogueDecision

        future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
        session = SessionState(
            role="worker",
            pending_relaxation={
                "frame": "job_search",
                "direction": "search_job",
                "step": "relax_salary_10pct",
                "original_criteria": {"city": ["北京市"], "salary_floor_monthly": 5000},
                "relaxed_criteria": {"city": ["北京市"], "salary_floor_monthly": 4500},
                "raw_query": "北京餐饮 5000",
                "user_msg_id": "m-orig",
                "expires_at": future,
            },
        )
        decision = DialogueDecision(
            dialogue_act="respond_relaxation_offer",
            resolved_frame="none",
            route_intent="follow_up",
            state_transition="apply_relaxation",
        )
        msg = MagicMock()
        msg.from_user = "u-1"
        msg.content = "好的"
        msg.msg_id = "m-new"
        user_ctx = MagicMock()
        user_ctx.role = "worker"

        with patch("app.services.search_service.execute_relaxed_search") as mock_exec:
            mock_exec.return_value = (
                SearchResult(reply_text="放宽后结果", result_count=2),
                SearchOutcome(
                    direction="search_job",
                    criteria_used={"city": ["北京市"], "salary_floor_monthly": 4500},
                    initial_count=2, final_count=2, desired_count=3,
                    low_recall_threshold=3, applied_relax_step="relax_salary_10pct",
                ),
            )
            replies = message_router._route_v2_relaxation_response(
                decision, msg, user_ctx, session, MagicMock(),
            )
            # 未过期 → execute_relaxed_search 必须被调用
            mock_exec.assert_called_once()
        # session.pending_relaxation 已清（应用放宽后）
        assert session.pending_relaxation is None


# ---------------------------------------------------------------------------
# Fix 2：contextvars 替换 module-level dict
# ---------------------------------------------------------------------------


class TestV2TurnContextContextVar:
    def test_default_values_none(self):
        from app.services.message_router import _v2_parse_result, _v2_decision
        # 单独的 context — 默认应为 None
        assert _v2_parse_result.get() is None
        assert _v2_decision.get() is None

    def test_set_and_clear(self):
        from app.services.message_router import (
            _set_v2_turn_context,
            _clear_v2_turn_context,
            _v2_parse_result,
            _v2_decision,
        )
        _set_v2_turn_context("parse", "decision")
        assert _v2_parse_result.get() == "parse"
        assert _v2_decision.get() == "decision"
        _clear_v2_turn_context()
        assert _v2_parse_result.get() is None
        assert _v2_decision.get() is None

    def test_no_module_level_dict_named_v2_turn_context(self):
        """grep 守护：旧的 _v2_turn_context module-level dict 已被替换。"""
        from app.services import message_router
        assert not hasattr(message_router, "_v2_turn_context"), (
            "_v2_turn_context module-level dict should be replaced by ContextVar"
        )

    def test_contextvars_thread_isolation(self):
        """contextvars 默认跨线程隔离：每个线程拿到自己的值。"""
        import threading
        from app.services.message_router import _set_v2_turn_context, _v2_parse_result

        results: dict[str, object] = {}
        barrier = threading.Barrier(2)

        def worker(name: str, value: str) -> None:
            _set_v2_turn_context(value, value)
            barrier.wait()  # 两个线程都 set 后再读
            results[name] = _v2_parse_result.get()

        t1 = threading.Thread(target=worker, args=("t1", "parse-1"))
        t2 = threading.Thread(target=worker, args=("t2", "parse-2"))
        t1.start(); t2.start()
        t1.join(); t2.join()
        # 两个线程各自独立的 ContextVar 值
        assert results["t1"] == "parse-1"
        assert results["t2"] == "parse-2"


# ---------------------------------------------------------------------------
# Fix 3：assert 换 RuntimeError
# ---------------------------------------------------------------------------


class TestRecursionGuardRaisesRuntimeError:
    """守护代码必须用 raise 而不是 assert（assert 在 -O 模式下被剥）。"""

    def test_apply_post_search_decision_raises_on_depth_2(self):
        from unittest.mock import MagicMock
        from app.services.post_search_applier import apply_post_search_decision
        from app.services.post_search_reducer import (
            PostSearchContext,
            PostSearchDecision,
        )
        from app.schemas.search import SearchOutcome, SearchResult
        from app.schemas.conversation import SessionState
        from app.llm.base import DialogueParseResult
        from app.services.dialogue_reducer import DialogueDecision

        ctx = PostSearchContext(
            decision=PostSearchDecision(action="no_action"),
            search_result=SearchResult(reply_text="x"),
            search_outcome=SearchOutcome(
                direction="search_job", criteria_used={},
                initial_count=0, final_count=0,
                desired_count=3, low_recall_threshold=3,
            ),
            parse_result=DialogueParseResult(
                dialogue_act="chitchat", frame_hint="none",
                slots_delta={}, merge_hint={}, needs_clarification=False,
                confidence=1.0,
            ),
            dialogue_decision=DialogueDecision(
                dialogue_act="chitchat", resolved_frame="none",
                route_intent="chitchat",
            ),
            session=SessionState(role="worker"),
            msg=MagicMock(), user_ctx=MagicMock(), db=MagicMock(),
            raw_query="x", role="worker",
            recursion_depth=2,  # 超过硬限制
        )
        with pytest.raises(RuntimeError, match="recursion_depth"):
            apply_post_search_decision(ctx)

    def test_no_assert_in_production_code_paths(self):
        """grep 守护：服务代码不应用 assert 守护生产 invariant。"""
        import re
        files = [
            "backend/app/services/post_search_applier.py",
            "backend/app/services/message_router.py",
        ]
        repo_root = Path(__file__).resolve().parents[3]
        # 只检查"assert"独立单词作为语句开头（不是 docstring / comment）
        for path in files:
            with (repo_root / path).open("r", encoding="utf-8") as f:
                content = f.read()
            # 排除注释和 docstring 内的 assert
            for i, line in enumerate(content.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                # 简单匹配 `assert <expr>` 在语句位置
                if re.match(r"^\s*assert\s+\w", line) and "test_" not in path:
                    pytest.fail(
                        f"{path}:{i} uses bare `assert` for production invariant — "
                        f"this is stripped under python -O. Use raise instead. "
                        f"Line: {line!r}"
                    )


# ---------------------------------------------------------------------------
# Fix 4：DialogueParseResult 跨字段 validator
# ---------------------------------------------------------------------------


class TestDialogueParseResultActionFieldsValidator:
    """conflict_action / relaxation_response 必须与对应 dialogue_act 一致。"""

    def test_valid_resolve_conflict_with_action(self):
        from app.llm.base import DialogueParseResult
        p = DialogueParseResult(
            dialogue_act="resolve_conflict",
            frame_hint="none",
            slots_delta={}, merge_hint={},
            needs_clarification=False, confidence=0.9,
            conflict_action="cancel_draft",
        )
        assert p.conflict_action == "cancel_draft"

    def test_valid_relaxation_offer_with_response(self):
        from app.llm.base import DialogueParseResult
        p = DialogueParseResult(
            dialogue_act="respond_relaxation_offer",
            frame_hint="none",
            slots_delta={}, merge_hint={},
            needs_clarification=False, confidence=0.9,
            relaxation_response="accept",
        )
        assert p.relaxation_response == "accept"

    def test_invalid_chitchat_with_conflict_action(self):
        from app.llm.base import DialogueParseResult
        with pytest.raises(ValidationError, match="conflict_action"):
            DialogueParseResult(
                dialogue_act="chitchat",  # 错误组合
                frame_hint="none",
                slots_delta={}, merge_hint={},
                needs_clarification=False, confidence=0.9,
                conflict_action="cancel_draft",
            )

    def test_invalid_chitchat_with_relaxation_response(self):
        from app.llm.base import DialogueParseResult
        with pytest.raises(ValidationError, match="relaxation_response"):
            DialogueParseResult(
                dialogue_act="chitchat",  # 错误组合
                frame_hint="none",
                slots_delta={}, merge_hint={},
                needs_clarification=False, confidence=0.9,
                relaxation_response="accept",
            )

    def test_invalid_cross_field_resolve_with_relaxation(self):
        """dialogue_act=resolve_conflict 但给了 relaxation_response 也应拒绝。"""
        from app.llm.base import DialogueParseResult
        with pytest.raises(ValidationError, match="relaxation_response"):
            DialogueParseResult(
                dialogue_act="resolve_conflict",
                frame_hint="none",
                slots_delta={}, merge_hint={},
                needs_clarification=False, confidence=0.9,
                relaxation_response="accept",
            )

    def test_invalid_cross_field_relaxation_with_conflict_action(self):
        """dialogue_act=respond_relaxation_offer 但给了 conflict_action 也应拒绝。"""
        from app.llm.base import DialogueParseResult
        with pytest.raises(ValidationError, match="conflict_action"):
            DialogueParseResult(
                dialogue_act="respond_relaxation_offer",
                frame_hint="none",
                slots_delta={}, merge_hint={},
                needs_clarification=False, confidence=0.9,
                conflict_action="cancel_draft",
            )

    def test_both_fields_none_is_valid(self):
        from app.llm.base import DialogueParseResult
        p = DialogueParseResult(
            dialogue_act="chitchat",
            frame_hint="none",
            slots_delta={}, merge_hint={},
            needs_clarification=False, confidence=0.9,
        )
        assert p.conflict_action is None
        assert p.relaxation_response is None
