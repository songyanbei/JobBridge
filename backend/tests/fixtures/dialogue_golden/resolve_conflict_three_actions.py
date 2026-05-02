"""阶段二反例：upload_conflict 中三种 conflict_action 都要走对路径（codex review P1）。

phased-plan §2.1.8 三种 resolve_conflict 行为：
- cancel_draft → state_transition=clear_pending_upload → 草稿清空 + PENDING_CANCELLED_REPLY
- resume_pending_upload → state_transition=resume_upload_collecting → 回 upload_collecting + CONFLICT_RESUME_FMT
- proceed_with_new → state_transition=apply_pending_interruption → 派发 pending_interruption + CONFLICT_PROCEED_ACK

每个 action 单独一条 case（同样的初始 session，三个并列轮次），断言 reducer
和 message_router._route_v2_resolve_conflict 都正确。

主断言：
- dialogue_act == resolve_conflict
- state_transition 三选一
- intent == "command"（compat 派生），但**不**应触发 UNKNOWN_COMMAND
  （回复文案不是「暂不支持该指令」）

注意：每个 turn 都用独立 case，因为同一 session 走完一个 conflict_action 后
session 状态会变（active_flow / pending_upload 都会变），无法继续测试另一个 action。
"""

_INITIAL_CONFLICT_SESSION = {
    "active_flow": "upload_conflict",
    "broker_direction": None,
    "search_criteria": {},
    "awaiting_fields": [],
    "awaiting_frame": None,
    "pending_upload": {
        "job_category": "餐饮",
        "city": "北京市",
        "salary_floor_monthly": 4000,
        "pay_type": "月薪",
    },
    "pending_upload_intent": "upload_job",
    "awaiting_field": "headcount",
    "pending_started_at": "2099-01-01T00:00:00+00:00",
    "pending_updated_at": "2099-01-01T00:00:00+00:00",
    "pending_expires_at": "2099-01-01T00:10:00+00:00",
    "pending_interruption": {
        "intent": "search_worker",
        "structured_data": {"job_category": "普工"},
        "criteria_patch": [],
        "raw_text": "先帮我找个普工",
    },
    "conflict_followup_rounds": 0,
}


def _mock_legacy(intent: str = "chitchat") -> dict:
    """legacy IntentResult 占位（不会被 v2 路径使用，但 runner 要求存在）。"""
    return {"intent": intent, "structured_data": {}, "confidence": 0.5}


def _mock_v2_resolve(action: str) -> dict:
    return {
        "dialogue_act": "resolve_conflict",
        "frame_hint": "none",
        "slots_delta": {},
        "merge_hint": {},
        "needs_clarification": False,
        "confidence": 0.95,
        "conflict_action": action,
    }


CASE_CANCEL = {
    "id": "resolve_conflict_cancel_draft",
    "role": "factory",
    "v2_mode": "dual_read",
    "initial_session": dict(_INITIAL_CONFLICT_SESSION),
    "turns": [
        {
            "user": "取消草稿吧",
            "mock_llm": _mock_legacy("chitchat"),
            "mock_v2": _mock_v2_resolve("cancel_draft"),
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "resolve_conflict",
                "state_transition": "clear_pending_upload",
                "should_run_search": False,
            },
        },
    ],
}


CASE_RESUME = {
    "id": "resolve_conflict_resume_pending_upload",
    "role": "factory",
    "v2_mode": "dual_read",
    "initial_session": dict(_INITIAL_CONFLICT_SESSION),
    "turns": [
        {
            "user": "继续发布岗位",
            "mock_llm": _mock_legacy("chitchat"),
            "mock_v2": _mock_v2_resolve("resume_pending_upload"),
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "resolve_conflict",
                "state_transition": "resume_upload_collecting",
                "should_run_search": False,
            },
        },
    ],
}


CASE_PROCEED = {
    "id": "resolve_conflict_proceed_with_new",
    "role": "factory",
    "v2_mode": "dual_read",
    "initial_session": dict(_INITIAL_CONFLICT_SESSION),
    "turns": [
        {
            "user": "先帮我找个普工",
            "mock_llm": _mock_legacy("search_worker"),
            "mock_v2": _mock_v2_resolve("proceed_with_new"),
            "expect": {
                "source": "v2_dual_read",
                "dialogue_act": "resolve_conflict",
                "state_transition": "apply_pending_interruption",
                # _route_v2_resolve_conflict 会走 _route_idle 派发 search_worker，
                # 最终 handler 是 _handle_search
                "handler": "_handle_search",
                "should_run_search": True,
            },
        },
    ],
}
