"""Phase 1 golden dialogue case runner。

设计目标（详见 docs/dialogue-intent-extraction-phased-plan.md §1.4.bis）：

1. 用 mock LLM 驱动整条 message_router → intent_service → upload_service /
   search_service 链路，避免在 case 里手写一长串 monkeypatch；
2. 每条 case 是 ``CASE`` 字典，包含 ``initial_session`` / ``turns``，每 turn 给
   ``mock_llm``（让 provider 返回的 IntentResult 替身）+ ``expect``（按字段断言）；
3. 阶段一只断言可观测、稳定的字段——`intent` / `search_criteria` / handler 入口 /
   是否触发 SQL 检索 / awaiting 队列状态。文案不进主断言。

阶段二再扩 `dialogue_act` / `resolved_frame` / `clarification.kind` 等字段。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import intent_service, message_router
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

    # 把 mock LLM 容器从 _golden_mocks 提到外层，便于 trace 里读取本轮 mock_llm.intent
    mock_intent_result_holder: dict[str, IntentResult] = {}

    # Mock：identify_or_register / load_session / save_session / search_service
    with _golden_mocks(user_ctx, session, spy, mock_intent_result_holder) as mocks:
        prev_search_calls = 0
        for idx, turn in enumerate(case["turns"]):
            mocks["set_mock_llm"](turn["mock_llm"])
            handler_marker = {"name": ""}
            mocks["mark_handler"](handler_marker)

            msg = _build_msg(turn["user"], idx)
            replies = message_router.process(msg, db=mocks["db"])

            cur_search_calls = len(spy.jobs_calls) + len(spy.workers_calls)
            reply_text = replies[0].content if replies else ""
            replied_intent = replies[0].intent if replies and replies[0].intent else None
            # 派生 needs_clarification：阶段一没有真正的 clarification 通道，
            # "信息还不够完整" 是 missing 追问而非 clarification（后者要等阶段二）。
            # 阶段一固定 False；fixture 显式断言这一点，避免 Phase 2 落 clarification
            # 后忘记升级 fixture。
            needs_clarification = False
            # 派生 expected_legacy_missing：用 turn 末态的 search_criteria 跑 legacy schema
            # 重算，验证后端按 schema 重算 missing 与 fixture 期望一致。
            # 使用 reply 的 intent（即 sanitize 后的最终 intent），而不是 mock 原始 intent，
            # 这样 worker 护栏纠正后 (upload_job → search_job) 也能取到正确 frame。
            frame = _frame_for_intent(replied_intent)
            if frame:
                legacy_missing = intent_service._legacy_compute_missing(
                    frame, dict(session.search_criteria or {}),
                )
            else:
                legacy_missing = []
            trace_turns.append({
                "intent": (
                    replies[0].intent if replies and replies[0].intent
                    else None
                ),
                "search_criteria": dict(session.search_criteria or {}),
                "awaiting_fields": list(session.awaiting_fields or []),
                "awaiting_frame": session.awaiting_frame,
                "ran_search": cur_search_calls > prev_search_calls,
                "handler": handler_marker["name"],
                "reply": reply_text,
                "needs_clarification": needs_clarification,
                "legacy_missing": legacy_missing,
            })
            prev_search_calls = cur_search_calls

    return {"turns": trace_turns, "session": session, "spy": spy}


def _frame_for_intent(intent: str | None) -> str | None:
    """与 message_router._search_frame_for_intent 同义；测试侧不依赖私有函数导入路径。"""
    if intent in ("search_job", "follow_up"):
        return "job_search"
    if intent == "search_worker":
        return "candidate_search"
    return None


@contextmanager
def _golden_mocks(user_ctx, session, spy, mock_intent_result_holder):
    """统一打桩：避免每条 case 重复 patch。"""
    from unittest.mock import MagicMock

    db = MagicMock()

    def set_mock_llm(payload: dict) -> None:
        mock_intent_result_holder["value"] = IntentResult(**payload)

    def fake_classify(text, role, history=None, current_criteria=None,
                      user_msg_id=None, session_hint=None):
        # Phase 1 worker 搜索护栏要在这里也跑 sanitize（与生产路径对齐）
        from app.services.intent_service import _sanitize_intent_result
        result = mock_intent_result_holder["value"]
        result_copy = result.model_copy(deep=True)
        return _sanitize_intent_result(result_copy, role, raw_text=text.strip())

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


def _normalize_criteria_for_compare(criteria: dict) -> dict:
    out: dict = {}
    for k, v in (criteria or {}).items():
        if isinstance(v, list):
            out[k] = sorted(v)
        else:
            out[k] = v
    return out
