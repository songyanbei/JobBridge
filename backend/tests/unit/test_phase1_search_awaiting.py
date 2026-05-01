"""Phase 1 单元测试：worker 搜索护栏 + 搜索 awaiting 物化。

对应 docs/dialogue-intent-extraction-phased-plan.md §1.5 验收条件 2。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services import (
    conversation_service,
    intent_service,
    message_router,
)
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage


def _ctx(role: str = "worker") -> UserContext:
    return UserContext(
        external_userid="u-phase1", role=role, status="active",
        display_name="Tester",
        company="X厂" if role == "factory" else None,
        contact_person="Tester" if role == "factory" else None,
        phone=None,
        can_search_jobs=role in ("worker", "broker"),
        can_search_workers=role in ("factory", "broker"),
        is_first_touch=False, should_welcome=False,
    )


def _msg(content: str, idx: int = 1, userid: str = "u-phase1") -> WeComMessage:
    return WeComMessage(
        msg_id=f"m-{idx}", from_user=userid, to_user="bot",
        msg_type="text", content=content, media_id="", image_url="",
        create_time=1700000000 + idx,
    )


# ---------------------------------------------------------------------------
# Worker 搜索护栏（§1.1）
# ---------------------------------------------------------------------------


class TestWorkerSearchGuardrail:
    def test_should_force_worker_search_basic(self):
        # 命中搜索信号、不命中发布信号、role=worker、intent=upload_job
        assert intent_service._should_force_worker_search(
            role="worker", text="想找个饭店服务员的工作", intent="upload_job",
        ) is True

    def test_not_force_when_role_not_worker(self):
        assert intent_service._should_force_worker_search(
            role="factory", text="想找个工作", intent="upload_job",
        ) is False
        assert intent_service._should_force_worker_search(
            role="broker", text="想找个工作", intent="upload_job",
        ) is False

    def test_not_force_when_intent_not_upload_job(self):
        assert intent_service._should_force_worker_search(
            role="worker", text="想找个工作", intent="search_job",
        ) is False
        assert intent_service._should_force_worker_search(
            role="worker", text="想找个工作", intent="chitchat",
        ) is False

    def test_not_force_when_no_search_signal(self):
        assert intent_service._should_force_worker_search(
            role="worker", text="苏州", intent="upload_job",
        ) is False

    def test_not_force_when_job_posting_signal_present(self):
        # 文本里出现 "招聘 / 招人" 等显式发布词时不能纠正
        assert intent_service._should_force_worker_search(
            role="worker", text="想找人帮我招聘服务员", intent="upload_job",
        ) is False

    def test_sanitize_corrects_intent_and_clears_missing(self):
        result = IntentResult(
            intent="upload_job",
            structured_data={"city": "西安市", "job_category": "餐饮"},
            missing_fields=["pay_type", "headcount"],
            confidence=0.8,
        )
        sanitized = intent_service._sanitize_intent_result(
            result, role="worker", raw_text="西安，想找个饭店服务员的工作",
        )
        assert sanitized.intent == "search_job"
        assert sanitized.missing_fields == []
        # 城市 / 工种被搜索分支强制为 list
        assert sanitized.structured_data["city"] == ["西安市"]
        assert sanitized.structured_data["job_category"] == ["餐饮"]

    def test_sanitize_does_not_touch_factory_upload(self):
        result = IntentResult(
            intent="upload_job",
            structured_data={"city": "苏州市"},
            missing_fields=["pay_type"],
            confidence=0.9,
        )
        sanitized = intent_service._sanitize_intent_result(
            result, role="factory", raw_text="苏州找人发岗位",
        )
        assert sanitized.intent == "upload_job"


# ---------------------------------------------------------------------------
# Legacy schema helpers（§1.3.bis）
# ---------------------------------------------------------------------------


class TestLegacyComputeMissing:
    def test_job_search_required_all(self):
        # job_search required_all = {city, job_category}
        assert intent_service._legacy_compute_missing(
            "job_search", {},
        ) == ["city", "job_category"]
        assert intent_service._legacy_compute_missing(
            "job_search", {"city": ["西安市"]},
        ) == ["job_category"]
        assert intent_service._legacy_compute_missing(
            "job_search", {"city": ["西安市"], "job_category": ["餐饮"]},
        ) == []

    def test_candidate_search_required_any_with_placeholder(self):
        # required_any = {city, job_category}, both empty → 组合占位
        out = intent_service._legacy_compute_missing("candidate_search", {})
        assert len(out) == 1
        assert "city" in out[0] and "job_category" in out[0]

        # 任一填上即满足
        assert intent_service._legacy_compute_missing(
            "candidate_search", {"city": ["北京市"]},
        ) == []
        assert intent_service._legacy_compute_missing(
            "candidate_search", {"job_category": ["普工"]},
        ) == []

    def test_empty_list_treated_as_unfilled(self):
        assert intent_service._legacy_compute_missing(
            "job_search", {"city": [], "job_category": ["餐饮"]},
        ) == ["city"]


# ---------------------------------------------------------------------------
# search awaiting helpers（§1.1.2 / §1.4）
# ---------------------------------------------------------------------------


class TestSearchAwaitingHelpers:
    def test_set_and_clear(self):
        s = SessionState(role="worker")
        conversation_service.set_search_awaiting(
            s, ["salary_floor_monthly"], frame="job_search", ttl_seconds=600,
        )
        assert s.awaiting_fields == ["salary_floor_monthly"]
        assert s.awaiting_frame == "job_search"
        assert s.awaiting_expires_at is not None

        conversation_service.clear_search_awaiting(s)
        assert s.awaiting_fields == []
        assert s.awaiting_frame is None
        assert s.awaiting_expires_at is None

    def test_consume_removes_only_accepted_fields(self):
        s = SessionState(
            role="worker",
            awaiting_fields=["salary_floor_monthly", "city"],
            awaiting_frame="job_search",
            awaiting_expires_at=(
                datetime.now(timezone.utc) + timedelta(seconds=300)
            ).isoformat(),
        )
        conversation_service.consume_search_awaiting(s, ["salary_floor_monthly"])
        assert s.awaiting_fields == ["city"]
        assert s.awaiting_frame == "job_search"

        conversation_service.consume_search_awaiting(s, ["city"])
        assert s.awaiting_fields == []
        assert s.awaiting_frame is None
        assert s.awaiting_expires_at is None

    def test_is_expired_when_past_ttl(self):
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        s = SessionState(
            role="worker",
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
            awaiting_expires_at=past.isoformat(),
        )
        assert conversation_service.is_search_awaiting_expired(s) is True

    def test_is_expired_when_no_queue(self):
        s = SessionState(role="worker")
        assert conversation_service.is_search_awaiting_expired(s) is True


# ---------------------------------------------------------------------------
# search_awaiting 路径（§1.4 + §1.5 验收 2）
# ---------------------------------------------------------------------------


class TestSearchAwaitingFlow:
    """通过 message_router 驱动 _handle_search / _handle_follow_up。"""

    def _setup_mocks(self, monkeypatch, session, search_workers_mock=None):
        from types import SimpleNamespace

        monkeypatch.setattr(
            message_router.user_service, "identify_or_register",
            lambda *a, **k: _ctx(),
        )
        monkeypatch.setattr(
            message_router.user_service, "check_user_status",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            message_router.user_service, "update_last_active",
            lambda *a, **k: None,
        )
        monkeypatch.setattr(
            message_router.conversation_service, "load_session",
            lambda *a, **k: session,
        )
        monkeypatch.setattr(
            message_router.conversation_service, "save_session",
            lambda *a, **k: None,
        )
        # 阻断真正 SQL 检索
        sj = MagicMock(return_value=SimpleNamespace(reply_text="[mock]"))
        monkeypatch.setattr(
            message_router.search_service, "search_jobs", sj,
        )
        sw = MagicMock(return_value=SimpleNamespace(reply_text="[mock]"))
        monkeypatch.setattr(
            message_router.search_service, "search_workers", sw,
        )
        monkeypatch.setattr(
            message_router.search_service,
            "has_effective_search_criteria",
            lambda criteria: True,
        )
        return sj, sw

    def test_handle_search_writes_awaiting_when_missing(self, monkeypatch):
        session = SessionState(role="worker", active_flow="idle")
        sj, sw = self._setup_mocks(monkeypatch, session)

        # LLM 给出 city 但没给 job_category；legacy schema 算出 missing=["job_category"]
        monkeypatch.setattr(
            message_router, "classify_intent",
            lambda **kw: IntentResult(
                intent="search_job",
                structured_data={"city": ["西安市"]},
                missing_fields=[],
                confidence=0.85,
            ),
        )

        replies = message_router.process(_msg("西安"), MagicMock())

        assert "信息还不够完整" in replies[0].content
        assert session.awaiting_fields == ["job_category"]
        assert session.awaiting_frame == "job_search"
        assert session.awaiting_expires_at is not None
        # 不应触发 SQL
        sj.assert_not_called()

    def test_followup_consumes_bare_value_for_salary(self, monkeypatch):
        # 上一轮已有 city + job_category，本轮追问 salary_floor_monthly
        # 用户裸值 "2500"，LLM 漂移返回 follow_up + 空 structured_data
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        session = SessionState(
            role="worker",
            active_flow="search_active",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
            awaiting_expires_at=future.isoformat(),
        )
        sj, sw = self._setup_mocks(monkeypatch, session)

        monkeypatch.setattr(
            message_router, "classify_intent",
            lambda **kw: IntentResult(
                intent="follow_up",
                structured_data={},  # LLM 漂移：没抽出任何字段
                missing_fields=[],
                confidence=0.5,
            ),
        )

        message_router.process(_msg("2500"), MagicMock())

        # 裸值落到了 salary_floor_monthly，跑了 SQL 检索
        assert session.search_criteria.get("salary_floor_monthly") == 2500
        # 关键回归断言：裸值兜底**不能**擦掉旧 city/job_category（reviewer P1）
        assert session.search_criteria.get("city") == ["西安市"]
        assert session.search_criteria.get("job_category") == ["餐饮"]
        sj.assert_called_once()
        # 实际传给 search_jobs 的 criteria 也必须保留旧条件
        called_criteria = sj.call_args[0][0] if sj.call_args[0] else sj.call_args.kwargs.get("criteria")
        assert called_criteria.get("city") == ["西安市"]
        assert called_criteria.get("job_category") == ["餐饮"]
        assert called_criteria.get("salary_floor_monthly") == 2500
        # awaiting 队列被消费（_run_search 末尾会清空）
        assert session.awaiting_fields == []

    def test_followup_does_not_consume_when_llm_has_slots(self, monkeypatch):
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        session = SessionState(
            role="worker",
            active_flow="search_active",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
            awaiting_expires_at=future.isoformat(),
        )
        sj, sw = self._setup_mocks(monkeypatch, session)

        # LLM 已抽出全量快照，包括新字段；不应进入裸值兜底路径
        monkeypatch.setattr(
            message_router, "classify_intent",
            lambda **kw: IntentResult(
                intent="follow_up",
                structured_data={
                    "city": ["西安市"],
                    "job_category": ["餐饮"],
                    "salary_floor_monthly": 2500,
                },
                confidence=0.9,
            ),
        )
        message_router.process(_msg("月薪 2500 起"), MagicMock())

        assert session.search_criteria["salary_floor_monthly"] == 2500
        # awaiting 队列仍被消费（因为 LLM slots_delta 中包含 awaiting 字段）
        assert session.awaiting_fields == []

    def test_followup_bare_value_rejected_when_out_of_range(self, monkeypatch):
        # awaiting_fields=[salary_floor_monthly]，但用户发裸值 "2"——不在 [500, 200000] 范围内
        # 不应被错误地当作 salary_floor_monthly 消费（关键属性）
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        session = SessionState(
            role="worker",
            active_flow="search_active",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
            awaiting_expires_at=future.isoformat(),
        )
        sj, sw = self._setup_mocks(monkeypatch, session)

        monkeypatch.setattr(
            message_router, "classify_intent",
            lambda **kw: IntentResult(
                intent="follow_up", structured_data={}, confidence=0.4,
            ),
        )
        message_router.process(_msg("2"), MagicMock())

        # 关键断言：裸值没有命中 awaiting 字段（"2" 不是合法薪资）；
        # session.search_criteria 不被错值污染
        assert "salary_floor_monthly" not in session.search_criteria
        # _run_search 末尾会清搜索 awaiting，所以队列已清空——
        # 但 awaiting 字段值没有被吃进 search_criteria 是核心保证
        assert session.awaiting_fields == []

    def test_awaiting_expired_is_not_consumed(self, monkeypatch):
        past = datetime.now(timezone.utc) - timedelta(seconds=10)
        session = SessionState(
            role="worker",
            active_flow="search_active",
            search_criteria={"city": ["西安市"], "job_category": ["餐饮"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
            awaiting_expires_at=past.isoformat(),
        )
        sj, sw = self._setup_mocks(monkeypatch, session)

        monkeypatch.setattr(
            message_router, "classify_intent",
            lambda **kw: IntentResult(
                intent="follow_up", structured_data={}, confidence=0.4,
            ),
        )
        message_router.process(_msg("2500"), MagicMock())

        # 过期 awaiting 不应消费裸值；search_criteria 不被污染
        assert "salary_floor_monthly" not in session.search_criteria
        # 过期队列被清空
        assert session.awaiting_fields == []
        assert session.awaiting_frame is None


# ---------------------------------------------------------------------------
# Phase 1 边界文档化：探询型 follow_up 的 LLM drift 是已知边界，prompt 是唯一防线
# ---------------------------------------------------------------------------


class TestPromptIsTheOnlyDefenseAgainstDrift:
    """阶段一对 "X 有吗 / 北京呢 / 看看苏州 / 北京" 这类高歧义探询的 replace
    语义保证 **完全依赖 prompt + few-shot**。phased-plan §9 与 §1.2 明令禁止
    在后端用关键词表来弥补 LLM 输出，因为枚举永远不全（用户可以说"试试北京"
    "改去北京""北京呢""单独发个北京"等任意表达）。

    这里只断言：
      1. prompt 显式约束了 "X 有吗" 的 replace 语义（防止未来 prompt 编辑误删）
      2. message_router 内部不再存在任何关键词驱动的"漂移防护"分支

    残留 LLM 漂移在阶段一作为已知边界存在；Phase 2 reducer + clarification
    接入后由结构化 DTO + confidence 兜底处理。
    """

    def test_prompt_explicitly_constrains_x_youma(self):
        from app.llm.prompts import INTENT_SYSTEM_PROMPT
        assert "X 有吗" in INTENT_SYSTEM_PROMPT
        assert "按替换处理" in INTENT_SYSTEM_PROMPT

    def test_prompt_contains_few_shot_12(self):
        from app.llm.prompts import INTENT_SYSTEM_PROMPT
        assert "示例12" in INTENT_SYSTEM_PROMPT
        assert "北京有吗" in INTENT_SYSTEM_PROMPT

    def test_no_keyword_drift_branch_in_message_router(self):
        # 防御未来又有人加回关键词分支：扫描 message_router 模块属性，
        # 确保没有以"replace_query"/"drift"/"_youma"等命名的关键词常量或函数。
        # 这是反向守卫——不是断言行为正确，是断言**没有**走错路的实现。
        for name in dir(message_router):
            assert "REPLACE_QUERY" not in name.upper(), (
                f"reverted keyword-based drift defense — {name!r} reintroduced; "
                "phased-plan §9 forbids enumerated keyword tables"
            )
            assert "DRIFT" not in name.upper(), (
                f"reverted keyword-based drift defense — {name!r} reintroduced"
            )


# ---------------------------------------------------------------------------
# 跨 frame 隔离（§1.4）：上传草稿 awaiting 不被搜索流程消费
# ---------------------------------------------------------------------------


class TestCrossFrameIsolation:
    """upload_collecting 中的 awaiting_field=headcount 不能被搜索 awaiting 路径吃掉。

    关键属性：搜索 awaiting 字段集合中**不**包含 headcount。
    """

    def test_search_awaiting_int_ranges_excludes_headcount(self):
        # headcount 是上传字段，不应出现在搜索 awaiting 的可消费字段里
        assert "headcount" not in message_router._SEARCH_AWAITING_INT_RANGES
        # salary 字段是允许的
        assert "salary_floor_monthly" in message_router._SEARCH_AWAITING_INT_RANGES

    def test_upload_awaiting_field_independent_of_search_awaiting(self):
        # 验证 SessionState 的两个 awaiting 字段独立存储
        s = SessionState(
            role="worker",
            awaiting_field="headcount",  # 上传草稿
            pending_upload_intent="upload_job",
        )
        # 搜索 awaiting 默认是空，不会被上传字段污染
        assert s.awaiting_fields == []
        assert s.awaiting_frame is None


# ---------------------------------------------------------------------------
# awaiting 清理时机（§1.1.2）：搜索成功 / reset / 上传切流
# ---------------------------------------------------------------------------


class TestAwaitingClearing:
    def test_clear_on_reset_search(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        s = SessionState(
            role="worker",
            search_criteria={"city": ["西安市"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
            awaiting_expires_at=future.isoformat(),
        )
        conversation_service.reset_search(s)
        assert s.awaiting_fields == []
        assert s.awaiting_frame is None
        assert s.awaiting_expires_at is None

    def test_clear_on_broker_direction_switch(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=300)
        s = SessionState(
            role="broker",
            broker_direction="search_worker",
            search_criteria={"job_category": ["普工"]},
            awaiting_fields=["city|job_category"],
            awaiting_frame="candidate_search",
            awaiting_expires_at=future.isoformat(),
        )
        err = conversation_service.set_broker_direction(s, "search_job")
        assert err is None
        assert s.awaiting_fields == []
        assert s.awaiting_frame is None


# ---------------------------------------------------------------------------
# build_session_hint：旧 Redis session 反序列化兼容
# ---------------------------------------------------------------------------


class TestSessionHintBackcompat:
    def test_build_hint_handles_missing_new_fields(self):
        # 模拟旧 Redis 反序列化：构造一个不含新字段的 SessionState
        # （Pydantic 会用默认值填充，不报错）
        s = SessionState(role="worker")
        hint = intent_service.build_session_hint(s)
        assert hint["awaiting_fields"] == []
        assert hint["awaiting_frame"] is None
        assert hint["search_criteria"] == {}

    def test_build_hint_includes_search_criteria(self):
        s = SessionState(
            role="worker",
            active_flow="search_active",
            search_criteria={"city": ["西安市"]},
            awaiting_fields=["salary_floor_monthly"],
            awaiting_frame="job_search",
        )
        hint = intent_service.build_session_hint(s)
        assert hint["search_criteria"] == {"city": ["西安市"]}
        assert hint["awaiting_fields"] == ["salary_floor_monthly"]
        assert hint["awaiting_frame"] == "job_search"
        assert hint["active_flow"] == "search_active"
