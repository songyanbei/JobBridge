"""Phase 1/2 golden dialogue case runner。

设计目标（详见 docs/dialogue-intent-extraction-phased-plan.md §1.4.bis / §2.5）：

1. 用 mock LLM 驱动整条 message_router → intent_service → upload_service /
   search_service 链路，避免在 case 里手写一长串 monkeypatch；
2. 每条 case 是 ``CASE`` 字典，包含 ``initial_session`` / ``turns``，每 turn 给
   ``mock_llm``（让 provider 返回的 IntentResult 替身）+ ``expect``（按字段断言）；
3. 阶段一只断言可观测、稳定的字段——`intent` / `search_criteria` / handler 入口 /
   是否触发 SQL 检索 / awaiting 队列状态。文案不进主断言。

阶段二扩展（dialogue-intent-extraction-phased-plan §2）：
- turn 可选 ``mock_v2``：注入 DialogueParseResult；存在则走 v2_dual_read 路径，
  否则走 legacy（mock_llm 注入 IntentResult）。
- turn 可选 ``v2_mode``：覆盖默认 mode（默认 case 顶层 ``mode``，case 默认 ``off``）。
- expect 新增键见 ``_KNOWN_EXPECT_KEYS``。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import DialogueParseResult, IntentResult
from app.schemas.conversation import SessionState
from app.services import intent_service, message_router
from app.services.intent_service import DialogueRouteResult
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage


# ---------------------------------------------------------------------------
# 测试桩
# ---------------------------------------------------------------------------


def _build_user_ctx(role: str, userid: str = "u-golden") -> UserContext:
    return UserContext(
        external_userid=userid,
        role=role,
        status="active",
        display_name="测试用户",
        company="X厂" if role == "factory" else None,
        contact_person="测试" if role == "factory" else None,
        phone=None,
        can_search_jobs=role in ("worker", "broker"),
        can_search_workers=role in ("factory", "broker"),
        is_first_touch=False,
        should_welcome=False,
    )


def _build_msg(content: str, idx: int, userid: str = "u-golden") -> WeComMessage:
    return WeComMessage(
        msg_id=f"m-{idx}",
        from_user=userid,
        to_user="bot",
        msg_type="text",
        content=content,
        media_id="",
        image_url="",
        create_time=1700000000 + idx,
    )


@dataclass
class _SearchCallSpy:
    """记录 search_service 是否真的被调用 + 用什么 criteria。"""

    jobs_calls: list[dict] = field(default_factory=list)
    workers_calls: list[dict] = field(default_factory=list)

    def fake_search_jobs(self, criteria, raw_query, session, user_ctx, db, **_kw):
        from types import SimpleNamespace
        self.jobs_calls.append(dict(criteria))
        return SimpleNamespace(reply_text="[mock-jobs-result]")

    def fake_search_workers(self, criteria, raw_query, session, user_ctx, db, **_kw):
        from types import SimpleNamespace
        self.workers_calls.append(dict(criteria))
        return SimpleNamespace(reply_text="[mock-workers-result]")


# ---------------------------------------------------------------------------
# 主 runner
# ---------------------------------------------------------------------------


def run_dialogue_case(case: dict) -> dict:
    """执行一条 golden case，返回每轮收集到的 trace。

    阶段二增加：
    - turn 可选 ``mock_v2``：DialogueParseResult 替身；命中后走 v2_dual_read。
    - case 顶层可选 ``v2_mode``：覆盖默认 ``off``；turn 也可单独覆盖。
    - case 顶层可选 ``ambiguous_city_query_policy``：覆盖默认配置。

    trace 形态：
        {
            "turns": [
                {
                    "intent": str,
                    "search_criteria": dict,
                    "awaiting_fields": list[str],
                    "awaiting_frame": str | None,
                    "ran_search": bool,
                    "handler": str,
                    "reply": str,
                    "needs_clarification": bool,
                    "legacy_missing": list[str],
                    # 阶段二新增：
                    "dialogue_act": str | None,
                    "resolved_frame": str | None,
                    "resolved_merge_policy": dict,
                    "clarification_kind": str | None,
                    "clarification_options": list[str] | None,
                    "source": str,            # legacy / v2_shadow / v2_dual_read / v2_fallback_legacy
                    "state_transition": str,
                    "final_search_criteria": dict,
                },
                ...
            ],
            "session": SessionState,
        }
    """
    role = case["role"]
    user_ctx = _build_user_ctx(role)

    # 用 initial_session 构造 session（带默认值兼容旧字段）
    init = dict(case.get("initial_session") or {})
    init.setdefault("role", role)
    session = SessionState(**init)

    spy = _SearchCallSpy()
    trace_turns: list[dict] = []

    mock_intent_result_holder: dict[str, IntentResult] = {}
    mock_v2_holder: dict[str, DialogueParseResult | None] = {"value": None}
    last_decision_holder: dict[str, Any] = {"value": None}
    last_source_holder: dict[str, str] = {"value": "legacy"}

    case_mode = (case.get("v2_mode") or "off").strip()
    case_policy = case.get("ambiguous_city_query_policy")
    case_low_conf_threshold = case.get("low_confidence_threshold")

    with _golden_mocks(
        user_ctx, session, spy,
        mock_intent_result_holder, mock_v2_holder,
        last_decision_holder, last_source_holder,
    ) as mocks:
        prev_search_calls = 0
        from app.config import settings as _settings
        original_mode = getattr(_settings, "dialogue_v2_mode", "off")
        original_policy = getattr(_settings, "ambiguous_city_query_policy", "clarify")
        original_threshold = getattr(_settings, "low_confidence_threshold", 0.6)
        try:
            for idx, turn in enumerate(case["turns"]):
                mocks["set_mock_llm"](turn["mock_llm"])
                mocks["set_mock_v2"](turn.get("mock_v2"))
                # turn 级别覆盖（不写则继承 case，case 不写则保持原 settings）
                turn_mode = (turn.get("v2_mode") or case_mode or "off").strip()
                _settings.dialogue_v2_mode = turn_mode
                if case_policy is not None:
                    _settings.ambiguous_city_query_policy = case_policy
                if case_low_conf_threshold is not None:
                    _settings.low_confidence_threshold = float(case_low_conf_threshold)

                handler_marker = {"name": ""}
                mocks["mark_handler"](handler_marker)
                last_decision_holder["value"] = None
                last_source_holder["value"] = "legacy"

                msg = _build_msg(turn["user"], idx)
                replies = message_router.process(msg, db=mocks["db"])

                cur_search_calls = len(spy.jobs_calls) + len(spy.workers_calls)
                reply_text = replies[0].content if replies else ""
                replied_intent = replies[0].intent if replies and replies[0].intent else None
                # 派生 needs_clarification：v2 路径下 decision.clarification 非空 = True
                decision = last_decision_holder["value"]
                needs_clarification = bool(getattr(decision, "clarification", None))
                clar = getattr(decision, "clarification", None) or {}
                # 派生 expected_legacy_missing
                frame = _frame_for_intent(replied_intent)
                if frame:
                    legacy_missing = intent_service._legacy_compute_missing(
                        frame, dict(session.search_criteria or {}),
                    )
                else:
                    legacy_missing = []
                trace_turns.append({
                    "intent": replied_intent,
                    "search_criteria": dict(session.search_criteria or {}),
                    "awaiting_fields": list(session.awaiting_fields or []),
                    "awaiting_frame": session.awaiting_frame,
                    "ran_search": cur_search_calls > prev_search_calls,
                    "handler": handler_marker["name"],
                    "reply": reply_text,
                    "needs_clarification": needs_clarification,
                    "legacy_missing": legacy_missing,
                    "dialogue_act": getattr(decision, "dialogue_act", None),
                    "resolved_frame": getattr(decision, "resolved_frame", None),
                    "resolved_merge_policy": dict(getattr(decision, "resolved_merge_policy", {}) or {}),
                    "clarification_kind": clar.get("kind") if clar else None,
                    "clarification_options": list(clar.get("options") or []) if clar else None,
                    "source": last_source_holder["value"],
                    "state_transition": getattr(decision, "state_transition", "none"),
                    "final_search_criteria": dict(getattr(decision, "final_search_criteria", {}) or {}),
                })
                prev_search_calls = cur_search_calls
        finally:
            _settings.dialogue_v2_mode = original_mode
            _settings.ambiguous_city_query_policy = original_policy
            _settings.low_confidence_threshold = original_threshold

    return {"turns": trace_turns, "session": session, "spy": spy}


def _frame_for_intent(intent: str | None) -> str | None:
    """与 message_router._search_frame_for_intent 同义；测试侧不依赖私有函数导入路径。"""
    if intent in ("search_job", "follow_up"):
        return "job_search"
    if intent == "search_worker":
        return "candidate_search"
    return None


@contextmanager
def _golden_mocks(
    user_ctx, session, spy, mock_intent_result_holder,
    mock_v2_holder=None, last_decision_holder=None, last_source_holder=None,
):
    """统一打桩：避免每条 case 重复 patch。

    阶段二（dialogue-intent-extraction-phased-plan §2.5）：
    - 当 turn 提供 mock_v2 + dialogue_v2_mode != off 时，走 v2_dual_read 路径，
      让 reducer / compat / applier 真实运行；
    - 否则走 legacy（_sanitize_intent_result 仍然跑，与生产路径对齐）；
    - last_decision_holder / last_source_holder 用于 trace 回读。
    """
    from unittest.mock import MagicMock
    from app.services.dialogue_compat import decision_to_intent_result
    from app.services.dialogue_reducer import reduce as _reduce

    if mock_v2_holder is None:
        mock_v2_holder = {"value": None}
    if last_decision_holder is None:
        last_decision_holder = {"value": None}
    if last_source_holder is None:
        last_source_holder = {"value": "legacy"}

    db = MagicMock()

    def set_mock_llm(payload: dict) -> None:
        mock_intent_result_holder["value"] = IntentResult(**payload)

    def set_mock_v2(payload):
        if payload is None:
            mock_v2_holder["value"] = None
        elif isinstance(payload, dict) and payload.get("_raise"):
            # 用 dict {"_raise": True} 显式模拟 v2 解析失败 → fallback 测试
            mock_v2_holder["value"] = "_raise"
        else:
            mock_v2_holder["value"] = DialogueParseResult(**payload)

    def _legacy_intent_result(text, role):
        # Phase 1 worker 搜索护栏要在这里也跑 sanitize（与生产路径对齐）
        from app.services.intent_service import _sanitize_intent_result
        result = mock_intent_result_holder["value"]
        result_copy = result.model_copy(deep=True)
        return _sanitize_intent_result(result_copy, role, raw_text=text.strip())

    def fake_classify_dialogue(text, role, history=None, *, session=None,
                               user_msg_id=None, userid=None):
        from app.config import settings as _settings
        mode = getattr(_settings, "dialogue_v2_mode", "off")
        v2_payload = mock_v2_holder.get("value")

        # 优先用 mock_v2 + mode=dual_read 走 v2 路径
        if mode == "dual_read" and v2_payload is not None and session is not None:
            # 显式模拟 v2 解析失败 → fallback
            if v2_payload == "_raise":
                last_source_holder["value"] = "v2_fallback_legacy"
                return DialogueRouteResult(
                    intent_result=_legacy_intent_result(text, role),
                    decision=None, source="v2_fallback_legacy",
                )
            try:
                decision = _reduce(
                    v2_payload, session, role, raw_text=text.strip(),
                )
                ir = decision_to_intent_result(decision, session)
                last_decision_holder["value"] = decision
                last_source_holder["value"] = "v2_dual_read"
                return DialogueRouteResult(
                    intent_result=ir, decision=decision, source="v2_dual_read",
                )
            except Exception:
                # fallback 到 legacy
                last_source_holder["value"] = "v2_fallback_legacy"
                return DialogueRouteResult(
                    intent_result=_legacy_intent_result(text, role),
                    decision=None, source="v2_fallback_legacy",
                )

        # legacy 路径（mode=off / shadow / dual_read 未提供 mock_v2）
        last_source_holder["value"] = "legacy"
        return DialogueRouteResult(
            intent_result=_legacy_intent_result(text, role),
            decision=None, source="legacy",
        )

    def fake_classify(text, role, history=None, current_criteria=None,
                      user_msg_id=None, session_hint=None):
        return _legacy_intent_result(text, role)

    handler_holder: dict[str, dict] = {}

    def mark_handler(holder: dict) -> None:
        handler_holder["current"] = holder

    real_handle_follow_up = message_router._handle_follow_up
    real_handle_search = message_router._handle_search
    real_handle_upload = message_router._handle_upload

    def wrap_handler(name: str, real):
        def _wrapped(*a, **kw):
            holder = handler_holder.get("current") or {}
            holder["name"] = name
            return real(*a, **kw)
        return _wrapped

    saved: list[tuple[Any, str, Any]] = []

    def _swap(target, attr, new_value):
        saved.append((target, attr, getattr(target, attr)))
        setattr(target, attr, new_value)

    _swap(message_router.user_service, "identify_or_register",
          lambda *a, **k: user_ctx)
    _swap(message_router.user_service, "check_user_status",
          lambda *a, **k: None)
    _swap(message_router.user_service, "update_last_active",
          lambda *a, **k: None)
    _swap(message_router.conversation_service, "load_session",
          lambda *a, **k: session)
    _swap(message_router.conversation_service, "save_session",
          lambda *a, **k: None)
    _swap(message_router, "classify_intent", fake_classify)
    _swap(message_router, "classify_dialogue", fake_classify_dialogue)
    _swap(message_router.search_service, "search_jobs", spy.fake_search_jobs)
    _swap(message_router.search_service, "search_workers", spy.fake_search_workers)
    _swap(message_router.search_service, "has_effective_search_criteria",
          lambda criteria: True)
    _swap(message_router, "_handle_follow_up",
          wrap_handler("_handle_follow_up", real_handle_follow_up))
    _swap(message_router, "_handle_search",
          wrap_handler("_handle_search", real_handle_search))
    _swap(message_router, "_handle_upload",
          wrap_handler("_handle_upload", real_handle_upload))

    try:
        yield {
            "db": db,
            "set_mock_llm": set_mock_llm,
            "set_mock_v2": set_mock_v2,
            "mark_handler": mark_handler,
        }
    finally:
        for target, attr, original in reversed(saved):
            setattr(target, attr, original)


# ---------------------------------------------------------------------------
# 断言 helpers
# ---------------------------------------------------------------------------


# 阶段一 fixture 允许的 expect 键。每个键都必须有真实断言；不允许只为
# 「文档化意图」保留 no-op 键（reviewer P2 第二轮）。要扩展时同步在
# ``assert_turn`` 内实现断言后再加入此集合。
#
# 设计说明：
# - LLM 派生的 ``missing_fields`` 经 sanitize 后语义不稳定（搜索护栏会清空它），
#   `should_ask_missing` 已覆盖追问 UI 行为，`expected_legacy_missing` 已覆盖
#   后端 schema 计算结果，因此不再保留 ``missing_fields`` 键。
_KNOWN_EXPECT_KEYS = frozenset({
    "intent",
    "intent_not",
    "search_criteria",
    "expected_legacy_missing",
    "awaiting_fields",
    "should_run_search",
    "should_ask_missing",
    "handler",
    "needs_clarification",
    # 阶段二（dialogue-intent-extraction-phased-plan §2）新增
    "dialogue_act",
    "resolved_frame",
    "resolved_merge_policy",
    "clarification_kind",
    "clarification_options",
    "source",
    "state_transition",
    "final_search_criteria",
})


def assert_turn(trace_turn: dict, expect: dict, label: str = "") -> None:
    """对单轮 trace 应用 expect 字段断言。

    expect 支持的键见 ``_KNOWN_EXPECT_KEYS``。出现未知键时直接 fail，避免 fixture
    引入拼写错误后被静默忽略（reviewer P2）。
    """
    prefix = f"[{label}] " if label else ""

    # 严格模式：fixture 写错的键不能被静默吃掉。
    unknown = set(expect.keys()) - _KNOWN_EXPECT_KEYS
    assert not unknown, (
        f"{prefix}unknown expect keys: {sorted(unknown)}; "
        f"either implement the assertion or remove from fixture"
    )

    if "intent" in expect:
        assert trace_turn["intent"] == expect["intent"], (
            f"{prefix}intent={trace_turn['intent']} != expect={expect['intent']}"
        )
    if "intent_not" in expect:
        assert trace_turn["intent"] != expect["intent_not"], (
            f"{prefix}intent={trace_turn['intent']} should NOT be {expect['intent_not']}"
        )
    if "search_criteria" in expect:
        actual = _normalize_criteria_for_compare(trace_turn["search_criteria"])
        want = _normalize_criteria_for_compare(expect["search_criteria"])
        assert actual == want, (
            f"{prefix}search_criteria={actual} != expect={want}"
        )
    if "awaiting_fields" in expect:
        assert list(trace_turn["awaiting_fields"]) == list(expect["awaiting_fields"]), (
            f"{prefix}awaiting_fields={trace_turn['awaiting_fields']} "
            f"!= expect={expect['awaiting_fields']}"
        )
    if "handler" in expect:
        assert trace_turn["handler"] == expect["handler"], (
            f"{prefix}handler={trace_turn['handler']} != expect={expect['handler']}"
        )
    if "should_run_search" in expect:
        ran = trace_turn["ran_search"]
        if expect["should_run_search"]:
            assert ran, f"{prefix}expected SQL search to run but it did not"
        else:
            assert not ran, f"{prefix}expected NO SQL search but one ran"
    if "should_ask_missing" in expect and expect["should_ask_missing"]:
        assert "信息还不够完整" in trace_turn["reply"], (
            f"{prefix}expected missing follow-up text in reply"
        )
    if "should_ask_missing" in expect and not expect["should_ask_missing"]:
        assert "信息还不够完整" not in trace_turn["reply"], (
            f"{prefix}did not expect missing follow-up text but reply={trace_turn['reply']!r}"
        )
    # needs_clarification：阶段一固定 False；阶段二 reducer 接入后由 fixture 显式声明 True / False。
    if "needs_clarification" in expect:
        assert trace_turn["needs_clarification"] == expect["needs_clarification"], (
            f"{prefix}needs_clarification={trace_turn['needs_clarification']} "
            f"!= expect={expect['needs_clarification']}"
        )
    # 后端 legacy schema 重算的 missing：顺序无关比较，因为 _legacy_compute_missing
    # 内部按 sorted(required_all) 排序，而 fixture 写法不强制顺序。
    if "expected_legacy_missing" in expect:
        actual_missing = sorted(trace_turn["legacy_missing"])
        want_missing = sorted(expect["expected_legacy_missing"])
        assert actual_missing == want_missing, (
            f"{prefix}legacy_missing={actual_missing} "
            f"!= expect={want_missing}"
        )

    # 阶段二字段断言
    if "dialogue_act" in expect:
        assert trace_turn["dialogue_act"] == expect["dialogue_act"], (
            f"{prefix}dialogue_act={trace_turn['dialogue_act']} "
            f"!= expect={expect['dialogue_act']}"
        )
    if "resolved_frame" in expect:
        assert trace_turn["resolved_frame"] == expect["resolved_frame"], (
            f"{prefix}resolved_frame={trace_turn['resolved_frame']} "
            f"!= expect={expect['resolved_frame']}"
        )
    if "resolved_merge_policy" in expect:
        assert trace_turn["resolved_merge_policy"] == expect["resolved_merge_policy"], (
            f"{prefix}resolved_merge_policy={trace_turn['resolved_merge_policy']} "
            f"!= expect={expect['resolved_merge_policy']}"
        )
    if "clarification_kind" in expect:
        assert trace_turn["clarification_kind"] == expect["clarification_kind"], (
            f"{prefix}clarification_kind={trace_turn['clarification_kind']} "
            f"!= expect={expect['clarification_kind']}"
        )
    if "clarification_options" in expect:
        actual_opts = sorted(trace_turn["clarification_options"] or [])
        want_opts = sorted(expect["clarification_options"] or [])
        assert actual_opts == want_opts, (
            f"{prefix}clarification_options={actual_opts} != expect={want_opts}"
        )
    if "source" in expect:
        assert trace_turn["source"] == expect["source"], (
            f"{prefix}source={trace_turn['source']} != expect={expect['source']}"
        )
    if "state_transition" in expect:
        assert trace_turn["state_transition"] == expect["state_transition"], (
            f"{prefix}state_transition={trace_turn['state_transition']} "
            f"!= expect={expect['state_transition']}"
        )
    if "final_search_criteria" in expect:
        actual_fc = _normalize_criteria_for_compare(trace_turn["final_search_criteria"])
        want_fc = _normalize_criteria_for_compare(expect["final_search_criteria"])
        assert actual_fc == want_fc, (
            f"{prefix}final_search_criteria={actual_fc} != expect={want_fc}"
        )


def _normalize_criteria_for_compare(criteria: dict) -> dict:
    out: dict = {}
    for k, v in (criteria or {}).items():
        if isinstance(v, list):
            out[k] = sorted(v)
        else:
            out[k] = v
    return out
