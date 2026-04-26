"""Stage C1 兼容式状态机单测。

覆盖 docs/multi-turn-upload-stage-c-implementation.md §3 必测用例：

- SessionState 新字段反序列化兼容（active_flow / last_intent / pending_interruption /
  failed_patch_rounds / last_criteria）
- load_session 缺 active_flow 时按 pending_upload_intent / candidate_snapshot 推导
- load_session 在 active_flow 已存在时不被反复推导覆盖
- self-healing：active_flow=upload_collecting 但 pending_upload_intent 为空 → 修复 idle
- self-healing：active_flow=idle 但 pending_upload 残留 → 清 pending
- self-healing：active_flow=search_active 但 candidate_snapshot=None → 降为 idle
- self-healing：active_flow=upload_conflict 但 pending_interruption=None → 回 collecting/idle
- failed_patch_rounds 连续答非所问 2 次清草稿；chitchat 不递增；补到字段重置
- upload_collecting 中切搜索意图 → upload_conflict + pending_interruption 瘦身
- upload_conflict 选择"继续发布" / "先找工人" / "取消草稿" 三条路径
- pending_interruption 经 model_dump → model_validate 序列化往返不丢字段
- upload_and_search 0 命中：active_flow=idle + last_criteria 写入 + 入库回执仍在
- upload_and_search 有结果：active_flow=search_active + last_criteria 写入
- search_active 中收到新 upload：清快照/shown，保留 search_criteria/last_criteria
- broker /找岗位 in upload_collecting → upload_conflict
- attach_image 优先 active_flow == upload_collecting，回落 current_intent
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import IntentResult
from app.schemas.conversation import CandidateSnapshot, SessionState
from app.services import (
    conversation_service,
    intent_service,
    message_router,
    search_service,
    upload_service,
)
from app.services.message_router import (
    CONFLICT_DEAD_LOOP_REPLY,
    CONFLICT_PROCEED_ACK,
    PENDING_CANCELLED_REPLY,
    PENDING_MAX_ROUNDS_REPLY,
    process,
)
from app.services.user_service import UserContext
from app.wecom.callback import WeComMessage


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _ctx(role="factory"):
    return UserContext(
        external_userid="u1", role=role, status="active",
        display_name="张三",
        company="北京饭店" if role == "factory" else None,
        contact_person="张三" if role == "factory" else None,
        phone=None,
        can_search_jobs=role in ("worker", "broker"),
        can_search_workers=role in ("factory", "broker"),
        is_first_touch=False, should_welcome=False,
    )


def _msg(content: str, msg_id: str = "m1"):
    return WeComMessage(
        msg_id=msg_id, from_user="u1", to_user="bot",
        msg_type="text", content=content, media_id="",
        image_url="", create_time=1700000000,
    )


@pytest.fixture
def stub_user_pipeline():
    with patch(
        "app.services.message_router.user_service.identify_or_register"
    ) as mock_id, patch(
        "app.services.message_router.user_service.check_user_status"
    ) as mock_check, patch(
        "app.services.message_router.user_service.update_last_active"
    ):
        mock_id.return_value = _ctx("factory")
        mock_check.return_value = None
        yield


@pytest.fixture
def stub_broker_pipeline():
    with patch(
        "app.services.message_router.user_service.identify_or_register"
    ) as mock_id, patch(
        "app.services.message_router.user_service.check_user_status"
    ) as mock_check, patch(
        "app.services.message_router.user_service.update_last_active"
    ):
        mock_id.return_value = _ctx("broker")
        mock_check.return_value = None
        yield


def _future_iso(minutes: int = 10) -> str:
    return (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()


# ---------------------------------------------------------------------------
# §2.3 Schema 兼容性
# ---------------------------------------------------------------------------

class TestC1SchemaBackwardCompat:
    def test_legacy_session_without_c1_fields(self):
        """Stage A/B 旧 session（无 C1 字段）反序列化仍走默认值。"""
        legacy = {
            "role": "worker",
            "current_intent": None,
            "search_criteria": {},
            "candidate_snapshot": None,
            "shown_items": [],
            "history": [],
            "follow_up_rounds": 0,
            "pending_upload": {},
        }
        s = SessionState(**legacy)
        assert s.active_flow is None
        assert s.last_intent is None
        assert s.pending_interruption is None
        assert s.failed_patch_rounds == 0
        assert s.last_criteria == {}
        assert s.conflict_followup_rounds == 0


# ---------------------------------------------------------------------------
# §2.4 active_flow 推导 + self-heal
# ---------------------------------------------------------------------------

class TestActiveFlowDerivation:
    def test_derive_idle_when_no_pending_no_snapshot(self):
        s = SessionState(role="worker")
        s.active_flow = None  # 显式重置成旧 session 形态
        conversation_service.ensure_active_flow(s)
        assert s.active_flow == "idle"

    def test_derive_upload_collecting_from_pending_intent(self):
        s = SessionState(
            role="factory",
            pending_upload_intent="upload_job",
            pending_upload={"city": "北京市"},
            pending_expires_at=_future_iso(),
        )
        s.active_flow = None
        conversation_service.ensure_active_flow(s)
        assert s.active_flow == "upload_collecting"

    def test_derive_search_active_from_snapshot(self):
        s = SessionState(
            role="worker",
            candidate_snapshot=CandidateSnapshot(
                candidate_ids=["1"], query_digest="abc",
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at=_future_iso(),
            ),
        )
        s.active_flow = None
        conversation_service.ensure_active_flow(s)
        assert s.active_flow == "search_active"

    def test_existing_active_flow_not_overwritten(self):
        """active_flow 已存在时不再被 derived（避免旧字段反向污染）。"""
        s = SessionState(
            role="worker",
            active_flow="upload_collecting",
            pending_upload_intent="upload_job",
            pending_expires_at=_future_iso(),
        )
        # 改动 candidate_snapshot 不应让 active_flow 飘动
        s.candidate_snapshot = CandidateSnapshot(
            candidate_ids=["1"], query_digest="abc",
            created_at=datetime.now(timezone.utc).isoformat(),
            expires_at=_future_iso(),
        )
        conversation_service.ensure_active_flow(s)
        assert s.active_flow == "upload_collecting"


class TestActiveFlowSelfHeal:
    def test_zombie_upload_collecting_without_intent_drops_to_idle(self):
        """active_flow=upload_collecting 但 pending_upload_intent 为空 → idle。"""
        s = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload_intent=None,
            pending_upload={"city": "北京市"},  # 残留 dict
        )
        conversation_service.ensure_active_flow(s)
        assert s.active_flow == "idle"
        assert s.pending_upload == {}

    def test_idle_with_zombie_pending_clears_pending(self):
        s = SessionState(
            role="factory",
            active_flow="idle",
            pending_upload_intent="upload_job",  # 与 idle 不一致
            pending_upload={"city": "北京市"},
        )
        conversation_service.ensure_active_flow(s)
        assert s.pending_upload_intent is None
        assert s.pending_upload == {}

    def test_search_active_without_snapshot_drops_to_idle_keeps_last_criteria(self):
        s = SessionState(
            role="worker",
            active_flow="search_active",
            candidate_snapshot=None,
            last_criteria={"city": ["北京市"]},
        )
        conversation_service.ensure_active_flow(s)
        assert s.active_flow == "idle"
        assert s.last_criteria == {"city": ["北京市"]}

    def test_upload_conflict_without_interruption_falls_back(self):
        # 还有 pending → 回 upload_collecting
        s = SessionState(
            role="factory",
            active_flow="upload_conflict",
            pending_upload_intent="upload_job",
            pending_interruption=None,
        )
        conversation_service.ensure_active_flow(s)
        assert s.active_flow == "upload_collecting"

        # 无 pending → 回 idle
        s2 = SessionState(
            role="factory",
            active_flow="upload_conflict",
            pending_interruption=None,
        )
        conversation_service.ensure_active_flow(s2)
        assert s2.active_flow == "idle"


# ---------------------------------------------------------------------------
# §2.6 upload_collecting + failed_patch_rounds
# ---------------------------------------------------------------------------

class TestFailedPatchRoundsLifecycle:
    """failed_patch_rounds：失败 +1，补到字段重置，chitchat 不动，连续 2 次清。"""

    @patch("app.services.message_router.upload_service.is_pending_upload_expired",
           return_value=False)
    def test_failed_patch_increments_on_irrelevant_text(self, _):
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
            pending_raw_text_parts=["第一段"],
        )
        intent = IntentResult(
            intent="follow_up", structured_data={}, confidence=0.3,
        )
        message_router._route_upload_collecting(
            intent, _msg("还行吧"), ctx, session, MagicMock(),
        )
        assert session.failed_patch_rounds == 1
        assert session.pending_upload_intent == "upload_job"

    @patch("app.services.message_router.upload_service.is_pending_upload_expired",
           return_value=False)
    def test_failed_patch_clears_at_max(self, _):
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
            pending_raw_text_parts=["第一段"],
            failed_patch_rounds=1,  # 已经失败一次
        )
        intent = IntentResult(intent="follow_up", structured_data={}, confidence=0.3)
        replies = message_router._route_upload_collecting(
            intent, _msg("还行吧"), ctx, session, MagicMock(),
        )
        assert replies[0].content == PENDING_MAX_ROUNDS_REPLY
        assert session.pending_upload_intent is None
        assert session.active_flow == "idle"
        assert session.failed_patch_rounds == 0

    @patch("app.services.message_router.upload_service.is_pending_upload_expired",
           return_value=False)
    def test_chitchat_does_not_increment(self, _):
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
        )
        intent = IntentResult(intent="chitchat", structured_data={}, confidence=0.0)
        message_router._route_upload_collecting(
            intent, _msg("你好"), ctx, session, MagicMock(),
        )
        assert session.failed_patch_rounds == 0
        assert session.pending_upload_intent == "upload_job"

    @patch("app.services.message_router.upload_service.audit_service")
    @patch("app.services.message_router.upload_service._read_ttl_days")
    @patch("app.services.message_router.upload_service._create_job")
    @patch("app.services.message_router.upload_service.is_pending_upload_expired",
           return_value=False)
    def test_field_patch_resets_failed_counter(self, _exp, mock_create, mock_ttl, mock_audit):
        from app.services.audit_service import AuditResult
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload={
                "city": "北京市", "job_category": "餐饮",
                "salary_floor_monthly": 7500, "pay_type": "月薪",
            },
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
            pending_raw_text_parts=["北京饭店招聘厨师"],
            failed_patch_rounds=1,
        )
        intent = IntentResult(
            intent="follow_up", structured_data={},
            criteria_patch=[{"op": "update", "field": "headcount", "value": 2}],
            confidence=0.7,
        )
        mock_audit.audit_content_only.return_value = AuditResult(
            status="passed", reason="", matched_words=[],
        )
        mock_audit.write_audit_log_for_result = MagicMock()
        mock_ttl.return_value = 30
        fake_job = MagicMock()
        fake_job.id = 1
        mock_create.return_value = fake_job

        message_router._route_upload_collecting(
            intent, _msg("2个人"), ctx, session, MagicMock(),
        )
        # 入库成功后 pending 已清；failed_patch_rounds 也复位
        assert session.pending_upload_intent is None
        assert session.failed_patch_rounds == 0
        assert session.active_flow == "idle"


# ---------------------------------------------------------------------------
# §2.7 upload_conflict
# ---------------------------------------------------------------------------

class TestUploadConflictEntry:
    @patch("app.services.message_router.upload_service.is_pending_upload_expired",
           return_value=False)
    def test_search_intent_during_collecting_enters_conflict(self, _):
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload={"city": "北京市", "job_category": "餐饮"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
        )
        intent = IntentResult(
            intent="search_worker",
            structured_data={"city": ["北京市"], "job_category": ["餐饮"]},
            confidence=0.9,
        )
        replies = message_router._route_upload_collecting(
            intent, _msg("先看看厨师简历"), ctx, session, MagicMock(),
        )
        assert session.active_flow == "upload_conflict"
        assert session.pending_interruption is not None
        assert session.pending_interruption["intent"] == "search_worker"
        assert session.pending_interruption["raw_text"] == "先看看厨师简历"
        # pending 草稿仍保留
        assert session.pending_upload_intent == "upload_job"
        assert "继续发布" in replies[0].content
        assert "找工人" in replies[0].content

    @patch("app.services.message_router.upload_service.is_pending_upload_expired",
           return_value=False)
    def test_different_upload_intent_enters_conflict(self, _):
        ctx = _ctx("worker")
        session = SessionState(
            role="worker",
            active_flow="upload_collecting",
            pending_upload={"expected_cities": ["北京市"]},
            pending_upload_intent="upload_resume",
            awaiting_field="expected_job_categories",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
        )
        intent = IntentResult(intent="upload_job", structured_data={}, confidence=0.8)
        message_router._route_upload_collecting(
            intent, _msg("我要发个新岗位"), ctx, session, MagicMock(),
        )
        assert session.active_flow == "upload_conflict"

    @patch("app.services.message_router.upload_service.is_pending_upload_expired",
           return_value=False)
    def test_same_origin_intent_remains_field_patch(self, _):
        """同 origin_intent + 不同字段值 → C1 仍按 field patch 处理（覆盖旧值）。"""
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload={"city": "北京市", "headcount": 2},
            pending_upload_intent="upload_job",
            awaiting_field="job_category",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
        )
        intent = IntentResult(
            intent="upload_job",
            structured_data={"headcount": 5},
            confidence=0.7,
        )
        message_router._route_upload_collecting(
            intent, _msg("改5人"), ctx, session, MagicMock(),
        )
        # 仍在 upload_collecting；headcount 被覆盖
        assert session.active_flow == "upload_collecting"
        assert session.pending_upload.get("headcount") == 5


class TestUploadConflictResolution:
    def test_continue_resumes_collecting(self):
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_conflict",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
            pending_interruption={
                "intent": "search_worker",
                "structured_data": {},
                "criteria_patch": [],
                "raw_text": "先看看厨师简历",
            },
        )
        intent = IntentResult(intent="chitchat", structured_data={}, confidence=0.0)
        replies = message_router._route_upload_conflict(
            intent, _msg("继续发布"), ctx, session, MagicMock(),
        )
        assert session.active_flow == "upload_collecting"
        assert session.pending_interruption is None
        assert "招聘人数" in replies[0].content

    def test_cancel_drops_pending(self):
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_conflict",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
            pending_interruption={
                "intent": "search_worker",
                "structured_data": {},
                "criteria_patch": [],
                "raw_text": "看看简历",
            },
        )
        intent = IntentResult(intent="chitchat", structured_data={}, confidence=0.0)
        replies = message_router._route_upload_conflict(
            intent, _msg("取消草稿"), ctx, session, MagicMock(),
        )
        assert replies[0].content == PENDING_CANCELLED_REPLY
        assert session.active_flow == "idle"
        assert session.pending_upload_intent is None
        assert session.pending_interruption is None

    @patch("app.services.message_router.search_service.search_workers")
    def test_proceed_executes_pending_interruption(self, mock_search_workers):
        """先找工人：清 pending → 用 pending_interruption 重构 IntentResult →
        分发到 _handle_search → 实际调用 search_workers。"""
        mock_search_workers.return_value = search_service.SearchResult(
            reply_text="为您找到 1 位求职者", result_count=1, has_more=False,
        )
        # 给 _handle_search 一个有效 snapshot 让 active_flow 推进到 search_active
        def _populate_snapshot(*_args, **_kw):
            session = _args[2]
            session.candidate_snapshot = CandidateSnapshot(
                candidate_ids=["1"], query_digest="x",
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at=_future_iso(),
            )
            return mock_search_workers.return_value

        mock_search_workers.side_effect = _populate_snapshot

        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_conflict",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
            pending_interruption={
                "intent": "search_worker",
                "structured_data": {"city": ["北京市"], "job_category": ["餐饮"]},
                "criteria_patch": [],
                "raw_text": "先看看厨师简历",
            },
        )
        intent = IntentResult(intent="chitchat", structured_data={}, confidence=0.0)
        replies = message_router._route_upload_conflict(
            intent, _msg("先找工人"), ctx, session, MagicMock(),
        )
        # 先回 ack 再回搜索结果
        assert replies[0].content == CONFLICT_PROCEED_ACK
        assert any("求职者" in r.content for r in replies[1:])
        # pending 已清；新搜索完成后 active_flow=search_active
        assert session.pending_upload_intent is None
        assert session.pending_interruption is None
        assert session.active_flow == "search_active"
        # 调用 search_workers 时使用的是 pending_interruption 的 raw_text 作为 raw_query
        called_args = mock_search_workers.call_args
        assert called_args.args[1] == "先看看厨师简历"

    def test_dead_loop_protection_clears_after_two_rounds(self):
        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="upload_conflict",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
            pending_interruption={
                "intent": "search_worker",
                "structured_data": {},
                "criteria_patch": [],
                "raw_text": "看简历",
            },
            conflict_followup_rounds=1,
        )
        intent = IntentResult(intent="chitchat", structured_data={}, confidence=0.0)
        replies = message_router._route_upload_conflict(
            intent, _msg("我也不知道"), ctx, session, MagicMock(),
        )
        assert replies[0].content == CONFLICT_DEAD_LOOP_REPLY
        assert session.active_flow == "idle"
        assert session.pending_upload_intent is None


class TestPendingInterruptionSerializationRoundTrip:
    """pending_interruption 经 model_dump → model_validate 不掉字段。"""

    def test_round_trip(self):
        s = SessionState(
            role="factory",
            active_flow="upload_conflict",
            pending_interruption={
                "intent": "search_worker",
                "structured_data": {"city": ["北京市"]},
                "criteria_patch": [{"op": "update", "field": "city", "value": ["北京市"]}],
                "raw_text": "先看厨师",
            },
        )
        dumped = s.model_dump(mode="json")
        s2 = SessionState(**dumped)
        assert s2.pending_interruption["intent"] == "search_worker"
        assert s2.pending_interruption["structured_data"]["city"] == ["北京市"]
        assert s2.pending_interruption["criteria_patch"][0]["field"] == "city"
        assert s2.pending_interruption["raw_text"] == "先看厨师"


# ---------------------------------------------------------------------------
# §9.2.1 upload_and_search
# ---------------------------------------------------------------------------

class TestUploadAndSearchPaths:
    @patch("app.services.message_router.search_service.search_workers")
    @patch("app.services.upload_service.audit_service")
    @patch("app.services.upload_service._read_ttl_days")
    @patch("app.services.upload_service._create_job")
    def test_zero_hit_keeps_idle_and_writes_last_criteria(
        self, mock_create, mock_ttl, mock_audit, mock_search,
    ):
        from app.services.audit_service import AuditResult
        mock_audit.audit_content_only.return_value = AuditResult(
            status="passed", reason="", matched_words=[],
        )
        mock_audit.write_audit_log_for_result = MagicMock()
        mock_ttl.return_value = 30
        fake = MagicMock(); fake.id = 99
        mock_create.return_value = fake
        # 0 命中
        mock_search.return_value = search_service.SearchResult(
            reply_text=search_service.NO_WORKER_MATCH_REPLY,
            result_count=0, has_more=False,
        )
        ctx = _ctx("factory")
        session = SessionState(role="factory", active_flow="idle")
        intent = IntentResult(
            intent="upload_and_search",
            structured_data={
                "city": "北京市", "job_category": "餐饮",
                "salary_floor_monthly": 7500, "pay_type": "月薪",
                "headcount": 5,
            },
            confidence=0.9,
        )
        replies = message_router._handle_upload_and_search(
            intent, _msg("招厨师5人月薪7500，顺便找人"),
            ctx, session, MagicMock(),
        )
        # 入库成功 + 暂未找到
        assert any("已入库" in r.content for r in replies)
        assert any(search_service.NO_WORKER_MATCH_REPLY == r.content for r in replies)
        # 0 命中 → idle；last_criteria 写入
        assert session.active_flow == "idle"
        assert session.last_criteria
        assert session.last_criteria.get("city") == ["北京市"]

    @patch("app.services.message_router.search_service.search_workers")
    @patch("app.services.upload_service.audit_service")
    @patch("app.services.upload_service._read_ttl_days")
    @patch("app.services.upload_service._create_job")
    def test_hit_promotes_to_search_active(
        self, mock_create, mock_ttl, mock_audit, mock_search,
    ):
        from app.services.audit_service import AuditResult
        mock_audit.audit_content_only.return_value = AuditResult(
            status="passed", reason="", matched_words=[],
        )
        mock_audit.write_audit_log_for_result = MagicMock()
        mock_ttl.return_value = 30
        fake = MagicMock(); fake.id = 100
        mock_create.return_value = fake

        def _populate(*args, **kw):
            session = args[2]
            session.candidate_snapshot = CandidateSnapshot(
                candidate_ids=["1", "2"], query_digest="x",
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at=_future_iso(),
            )
            return search_service.SearchResult(
                reply_text="为您找到 2 位求职者", result_count=2, has_more=False,
            )
        mock_search.side_effect = _populate

        ctx = _ctx("factory")
        session = SessionState(role="factory", active_flow="idle")
        intent = IntentResult(
            intent="upload_and_search",
            structured_data={
                "city": "北京市", "job_category": "餐饮",
                "salary_floor_monthly": 7500, "pay_type": "月薪",
                "headcount": 5,
            },
            confidence=0.9,
        )
        replies = message_router._handle_upload_and_search(
            intent, _msg("招厨师5人月薪7500，顺便找人"),
            ctx, session, MagicMock(),
        )
        assert any("已入库" in r.content for r in replies)
        assert any("求职者" in r.content for r in replies)
        assert session.active_flow == "search_active"
        assert session.last_criteria.get("city") == ["北京市"]


# ---------------------------------------------------------------------------
# §2.8 search_active 中的新上传
# ---------------------------------------------------------------------------

class TestSearchActiveNewUpload:
    @patch("app.services.message_router.upload_service.process_upload")
    def test_new_upload_clears_snapshot_keeps_criteria(self, mock_process):
        mock_process.return_value = upload_service.UploadResult(
            success=False, reply_text="还需要您补充：招聘人数",
            needs_followup=True,
        )

        def _set_pending(**kwargs):
            session = kwargs["session"]
            session.pending_upload_intent = "upload_job"
            session.pending_upload = {"city": "北京市"}
            session.awaiting_field = "headcount"
            session.active_flow = "upload_collecting"
            return mock_process.return_value
        mock_process.side_effect = _set_pending

        ctx = _ctx("factory")
        session = SessionState(
            role="factory",
            active_flow="search_active",
            search_criteria={"city": ["北京市"]},
            last_criteria={"city": ["北京市"]},
            candidate_snapshot=CandidateSnapshot(
                candidate_ids=["1"], query_digest="x",
                created_at=datetime.now(timezone.utc).isoformat(),
                expires_at=_future_iso(),
            ),
            shown_items=["1"],
        )
        intent = IntentResult(
            intent="upload_job",
            structured_data={"city": "北京市", "job_category": "餐饮"},
            confidence=0.8,
        )
        message_router._route_search_active(
            intent, _msg("北京招厨师"), ctx, session, MagicMock(),
        )
        # 快照/shown 清空，但 search_criteria 与 last_criteria 保留
        assert session.candidate_snapshot is None
        assert session.shown_items == []
        assert session.search_criteria == {"city": ["北京市"]}
        assert session.last_criteria == {"city": ["北京市"]}
        # 新 upload 把 active_flow 拉到 upload_collecting
        assert session.active_flow == "upload_collecting"


# ---------------------------------------------------------------------------
# §2.9 broker direction switch in upload_collecting
# ---------------------------------------------------------------------------

class TestBrokerDirectionDuringCollecting:
    @patch("app.services.message_router.conversation_service.save_session")
    @patch("app.services.message_router.conversation_service.load_session")
    @patch("app.services.message_router.classify_intent")
    def test_broker_switch_to_job_enters_conflict(
        self, mock_classify, mock_load, _save, stub_broker_pipeline,
    ):
        session = SessionState(
            role="broker",
            active_flow="upload_collecting",
            pending_upload={"city": "北京市"},
            pending_upload_intent="upload_job",
            awaiting_field="headcount",
            pending_started_at=datetime.now(timezone.utc).isoformat(),
            pending_expires_at=_future_iso(),
        )
        mock_load.return_value = session
        mock_classify.return_value = IntentResult(
            intent="command",
            structured_data={"command": "switch_to_job", "args": ""},
            confidence=1.0,
        )

        replies = process(_msg("/找岗位"), MagicMock())
        assert session.active_flow == "upload_conflict"
        assert session.pending_interruption is not None
        assert session.pending_interruption["intent"] == "search_job"
        # pending 仍保留
        assert session.pending_upload_intent == "upload_job"
        assert any("继续发布" in r.content for r in replies)


# ---------------------------------------------------------------------------
# §2.10 attach_image 优先 active_flow，回落 current_intent
# ---------------------------------------------------------------------------

class TestAttachImageActiveFlowPriority:
    def test_active_flow_collecting_used_first(self):
        """active_flow=upload_collecting + pending_upload_intent=upload_job
        即使 current_intent='command' 也要挂到 Job（不回落 current_intent）。"""
        from app.models import Job
        session = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload_intent="upload_job",
            current_intent="command",  # 旧逻辑下被命令污染过
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        feedback = upload_service.attach_image(
            external_userid="u1", image_key="key",
            session=session, db=db,
        )
        assert db.query.call_args.args[0] is Job
        assert "正在处理" in feedback or "已收到" in feedback

    def test_fallback_to_current_intent_when_no_active_flow(self):
        """旧 session：无 active_flow，但 current_intent=upload_resume 仍能挂载到 Resume。"""
        from app.models import Resume
        session = SessionState(
            role="worker",
            active_flow=None,
            current_intent="upload_resume",
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.order_by.return_value.first.return_value = None

        upload_service.attach_image(
            external_userid="u1", image_key="key",
            session=session, db=db,
        )
        assert db.query.call_args.args[0] is Resume


# ---------------------------------------------------------------------------
# §2.11 build_session_hint
# ---------------------------------------------------------------------------

class TestSessionHintHelper:
    def test_hint_keys_present(self):
        s = SessionState(
            role="factory",
            active_flow="upload_collecting",
            pending_upload_intent="upload_job",
            pending_upload={"city": "北京市"},
            awaiting_field="headcount",
        )
        hint = intent_service.build_session_hint(s)
        assert hint["active_flow"] == "upload_collecting"
        assert hint["pending_upload_intent"] == "upload_job"
        assert hint["awaiting_field"] == "headcount"
        assert hint["pending_upload"] == {"city": "北京市"}

    def test_hint_with_none_session(self):
        assert intent_service.build_session_hint(None) == {}
