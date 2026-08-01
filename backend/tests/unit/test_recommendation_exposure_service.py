"""曝光读取与 impression 派生的单测（方案 §6.6 / §9.7 / §10.5）。

单测环境没有 MySQL，这里用 SQLite 内存库承载 ``recommendation_delivery`` /
``recommendation_impression`` 两张表，从而真正验证过滤条件（租约、窗口、状态），
而不是只验证 Python 侧的合并逻辑。几个 MySQL 专有类型/函数只在 SQLite 方言下改写，
不影响生产 DDL。
"""
from __future__ import annotations

import itertools
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects.mysql import BIGINT, MEDIUMBLOB, TINYINT
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.sql.functions import now as sql_now

from app.models import Base, RecommendationDelivery, RecommendationImpression
from app.services.recommendation_exposure_service import (
    batch_candidate_exposures,
    claim_impression_deliveries,
    derive_impressions,
    exposure_opportunities,
    mark_impression_retry,
    recent_user_exposures,
)


@compiles(MEDIUMBLOB, "sqlite")
def _compile_mediumblob_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL helper
    return "BLOB"


@compiles(TINYINT, "sqlite")
def _compile_tinyint_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL helper
    return "INTEGER"


@compiles(BIGINT, "sqlite")
def _compile_bigint_sqlite(type_, compiler, **kw):  # pragma: no cover - DDL helper
    # SQLite 只把 "INTEGER PRIMARY KEY" 当 rowid 别名，BIGINT 主键无法自增。
    return "INTEGER"


@compiles(sql_now, "sqlite")
def _compile_now_sqlite(element, compiler, **kw):  # pragma: no cover - DDL helper
    return "CURRENT_TIMESTAMP"


_TABLES = [RecommendationDelivery.__table__, RecommendationImpression.__table__]
_ORDER = itertools.count(1)


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=_TABLES)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def _items(*targets, target_type: str = "job") -> list[dict]:
    return [
        {"target_type": target_type, "target_id": target_id, "position": index}
        for index, target_id in enumerate(targets, start=1)
    ]


def _add_delivery(
    session,
    delivery_id: str,
    *,
    userid: str = "viewer-1",
    status: str = "sent",
    items: list[dict] | None = None,
    impression_state: str = "pending",
    sent_at: datetime | None = None,
    **overrides,
) -> RecommendationDelivery:
    context = {"direction": "search_job", "algorithm_version": "recommendation-v1"}
    context["items"] = items if items is not None else []
    delivery = RecommendationDelivery(
        delivery_id=delivery_id,
        delivery_order=next(_ORDER),
        source_inbound_msg_id=f"msg-{delivery_id}",
        reply_index=0,
        request_id=f"req-{delivery_id}",
        snapshot_id=f"snap-{delivery_id}",
        userid=userid,
        recommendation_context=context,
        status=status,
        session_commit_token=delivery_id,
        next_attempt_at=_naive(datetime.now(timezone.utc)),
        impression_next_attempt_at=_naive(datetime.now(timezone.utc) - timedelta(seconds=1)),
        impression_state=impression_state,
        sent_at=_naive(sent_at) if sent_at else None,
    )
    for key, value in overrides.items():
        setattr(delivery, key, value)
    session.add(delivery)
    session.flush()
    return delivery


def _add_impression(
    session,
    *,
    delivery_id: str,
    target_id: int,
    exposed_at: datetime,
    viewer_userid: str = "viewer-1",
    target_type: str = "job",
) -> RecommendationImpression:
    row = RecommendationImpression(
        delivery_id=delivery_id,
        request_id=f"req-{delivery_id}",
        snapshot_id=f"snap-{delivery_id}",
        viewer_userid=viewer_userid,
        direction="search_job",
        target_type=target_type,
        target_id=target_id,
        position=1,
        algorithm_version="recommendation-v1",
        assignment="stable",
        is_exploration=False,
        query_digest="",
        exposed_at=_naive(exposed_at),
    )
    session.add(row)
    session.flush()
    return row


# ---------------------------------------------------------------------------
# §6.6 曝光机会分
# ---------------------------------------------------------------------------

class TestExposureOpportunities:
    def test_single_candidate_is_neutral(self):
        assert exposure_opportunities({"1": 9}, ["1"]) == {"1": 0.5}

    def test_all_equal_counts_are_exactly_half(self):
        result = exposure_opportunities({"1": 3, "2": 3, "3": 3}, ["1", "2", "3"])
        assert result == {"1": 0.5, "2": 0.5, "3": 0.5}

    def test_lowest_exposure_wins(self):
        result = exposure_opportunities({"1": 0, "2": 5}, ["1", "2"])
        assert result == {"1": 1.0, "2": 0.0}

    def test_missing_counts_are_zero_not_full_marks(self):
        result = exposure_opportunities({"2": 4, "3": 8}, ["1", "2", "3"])
        assert result == {"1": 1.0, "2": 0.5, "3": 0.0}

    def test_duplicate_candidate_ids_do_not_skew_n(self):
        counts = {"1": 0, "2": 5}
        assert exposure_opportunities(counts, ["1", "2", "1"]) == exposure_opportunities(counts, ["1", "2"])

    def test_empty_candidates(self):
        assert exposure_opportunities({}, []) == {}


# ---------------------------------------------------------------------------
# §6.6 168 小时滚动窗口
# ---------------------------------------------------------------------------

class TestBatchCandidateExposures:
    def test_counts_only_inside_the_rolling_window(self, db):
        now = datetime.now(timezone.utc)
        _add_impression(db, delivery_id="d1", target_id=7, exposed_at=now - timedelta(hours=1))
        _add_impression(db, delivery_id="d2", target_id=7, exposed_at=now - timedelta(hours=167))
        _add_impression(db, delivery_id="d3", target_id=7, exposed_at=now - timedelta(hours=169))
        _add_impression(db, delivery_id="d4", target_id=8, exposed_at=now + timedelta(minutes=1))
        counts = batch_candidate_exposures(
            db, target_type="job", candidate_ids=["7", "8"], request_now_utc=now,
        )
        assert counts == {"7": 2}

    def test_other_target_type_is_ignored(self, db):
        now = datetime.now(timezone.utc)
        _add_impression(db, delivery_id="d1", target_id=7, exposed_at=now - timedelta(hours=1), target_type="resume")
        assert batch_candidate_exposures(db, target_type="job", candidate_ids=["7"], request_now_utc=now) == {}

    def test_empty_candidates_skips_query(self, db):
        assert batch_candidate_exposures(db, target_type="job", candidate_ids=[], request_now_utc=datetime.now(timezone.utc)) == {}


# ---------------------------------------------------------------------------
# P1-5 §10.5 背靠背搜索：impression + 未派生完的 sent delivery
# ---------------------------------------------------------------------------

class TestRecentUserExposures:
    def test_reads_impression_facts(self, db):
        now = datetime.now(timezone.utc)
        exposed = now - timedelta(hours=2)
        _add_impression(db, delivery_id="d1", target_id=7, exposed_at=exposed)
        result = recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7"],
            request_now_utc=now, cooldown_hours=24,
        )
        assert set(result) == {"7"}
        assert result["7"] == exposed

    def test_sent_delivery_without_impression_still_counts(self, db):
        """P1-5：第二条背靠背搜索必须看到第一条刚 sent、还没派生的推荐。"""
        now = datetime.now(timezone.utc)
        sent_at = now - timedelta(seconds=3)
        _add_delivery(db, "d1", items=_items(7, 8), sent_at=sent_at, impression_state="pending")
        result = recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7", "8", "9"],
            request_now_utc=now, cooldown_hours=24,
        )
        assert set(result) == {"7", "8"}
        assert result["7"] == sent_at

    def test_processing_and_retry_states_also_count(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(db, "d1", items=_items(7), sent_at=now - timedelta(minutes=1), impression_state="processing")
        _add_delivery(db, "d2", items=_items(8), sent_at=now - timedelta(minutes=2), impression_state="retry")
        result = recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7", "8"],
            request_now_utc=now, cooldown_hours=24,
        )
        assert set(result) == {"7", "8"}

    def test_completed_delivery_context_is_not_double_counted(self, db):
        """completed 的 delivery 只由 impression 事实表代表。"""
        now = datetime.now(timezone.utc)
        _add_delivery(db, "d1", items=_items(7), sent_at=now - timedelta(hours=1), impression_state="completed")
        assert recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7"],
            request_now_utc=now, cooldown_hours=24,
        ) == {}

    def test_impression_wins_over_context_for_the_same_delivery(self, db):
        """按 (delivery_id, target_type, target_id) 去重，同一投递只算一次。"""
        now = datetime.now(timezone.utc)
        exposed = now - timedelta(hours=3)
        _add_delivery(db, "d1", items=_items(7), sent_at=now - timedelta(minutes=1), impression_state="retry")
        _add_impression(db, delivery_id="d1", target_id=7, exposed_at=exposed)
        result = recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7"],
            request_now_utc=now, cooldown_hours=24,
        )
        assert result["7"] == exposed

    def test_most_recent_wins_across_deliveries(self, db):
        now = datetime.now(timezone.utc)
        older = now - timedelta(hours=5)
        newer = now - timedelta(minutes=10)
        _add_impression(db, delivery_id="d1", target_id=7, exposed_at=older)
        _add_delivery(db, "d2", items=_items(7), sent_at=newer, impression_state="pending")
        result = recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7"],
            request_now_utc=now, cooldown_hours=24,
        )
        assert result["7"] == newer

    def test_cooldown_window_and_viewer_isolation(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(db, "d1", items=_items(7), sent_at=now - timedelta(hours=25), impression_state="pending")
        _add_delivery(db, "d2", userid="viewer-2", items=_items(8), sent_at=now - timedelta(minutes=1), impression_state="pending")
        _add_impression(db, delivery_id="d3", target_id=9, exposed_at=now - timedelta(hours=25))
        assert recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7", "8", "9"],
            request_now_utc=now, cooldown_hours=24,
        ) == {}

    def test_context_target_type_and_unknown_candidates_filtered(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(
            db, "d1",
            items=_items(7, target_type="resume") + _items(8) + [{"target_type": "job"}, {"foo": "bar"}],
            sent_at=now - timedelta(seconds=5),
        )
        result = recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7", "8"],
            request_now_utc=now, cooldown_hours=24,
        )
        assert set(result) == {"8"}

    def test_zero_cooldown_disables_the_lookup(self, db):
        now = datetime.now(timezone.utc)
        _add_impression(db, delivery_id="d1", target_id=7, exposed_at=now - timedelta(minutes=1))
        assert recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7"],
            request_now_utc=now, cooldown_hours=0,
        ) == {}

    def test_legacy_redacted_delivery_still_counts(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(db, "d1", items=_items(7), sent_at=now - timedelta(minutes=1), status="redacted")
        assert set(recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7"],
            request_now_utc=now, cooldown_hours=24,
        )) == {"7"}

    def test_unsent_delivery_is_ignored(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(db, "d1", items=_items(7), status="pending", sent_at=None)
        assert recent_user_exposures(
            db, viewer_userid="viewer-1", target_type="job", candidate_ids=["7"],
            request_now_utc=now, cooldown_hours=24,
        ) == {}


# ---------------------------------------------------------------------------
# §10.5 派生与数量核对
# ---------------------------------------------------------------------------

class TestDeriveImpressions:
    def test_derives_all_items_and_completes(self, db):
        sent_at = datetime.now(timezone.utc) - timedelta(seconds=2)
        delivery = _add_delivery(db, "d1", items=_items(7, 8), sent_at=sent_at)
        assert derive_impressions(db, delivery) == 2
        assert delivery.impression_state == "completed"
        assert delivery.impression_expected_count == 2
        assert delivery.impression_actual_count == 2
        assert delivery.impression_derived_at == _naive(sent_at)
        rows = db.query(RecommendationImpression).all()
        assert {row.target_id for row in rows} == {7, 8}
        # 曝光时间以真实发送时间为准，而不是派生时刻。
        assert {row.exposed_at for row in rows} == {_naive(sent_at)}

    def test_is_idempotent(self, db):
        delivery = _add_delivery(db, "d1", items=_items(7, 8), sent_at=datetime.now(timezone.utc))
        derive_impressions(db, delivery)
        assert derive_impressions(db, delivery) == 0
        assert db.query(RecommendationImpression).count() == 2
        assert delivery.impression_state == "completed"

    def test_duplicate_context_items_count_once(self, db):
        delivery = _add_delivery(db, "d1", items=_items(7) + _items(7), sent_at=datetime.now(timezone.utc))
        assert derive_impressions(db, delivery) == 1
        assert delivery.impression_expected_count == 1
        assert delivery.impression_state == "completed"

    def test_unusable_items_do_not_stall_the_delivery(self, db):
        delivery = _add_delivery(
            db, "d1",
            items=_items(7) + [{"target_type": "job"}, {"target_type": "job", "target_id": "abc"}],
            sent_at=datetime.now(timezone.utc),
        )
        assert derive_impressions(db, delivery) == 1
        assert delivery.impression_state == "completed"
        assert delivery.impression_expected_count == 1

    def test_empty_context_completes_immediately(self, db):
        delivery = _add_delivery(db, "d1", items=[], sent_at=datetime.now(timezone.utc))
        assert derive_impressions(db, delivery) == 0
        assert delivery.impression_state == "completed"
        assert delivery.impression_expected_count == 0

    def test_count_mismatch_writes_retry_with_backoff(self, db):
        now = datetime.now(timezone.utc)
        delivery = _add_delivery(db, "d1", items=_items(7), sent_at=now)
        # 多出一条不在 context 里的 impression：实际数量与预期数不符。
        _add_impression(db, delivery_id="d1", target_id=99, exposed_at=now)
        derive_impressions(db, delivery)
        assert delivery.impression_state == "retry"
        assert delivery.impression_derived_at is None
        assert delivery.impression_attempt_count == 1
        assert delivery.impression_next_attempt_at > _naive(now)
        assert "mismatch" in delivery.impression_last_error

    def test_only_sent_like_statuses_derive(self, db):
        delivery = _add_delivery(db, "d1", items=_items(7), status="sending", sent_at=None)
        assert derive_impressions(db, delivery) == 0
        assert db.query(RecommendationImpression).count() == 0
        assert delivery.impression_state == "pending"

    def test_legacy_redacted_delivery_still_derives(self, db):
        """``redacted`` 已从写入侧移除，但历史行不能永久停摆（迁移期兼容）。"""
        delivery = _add_delivery(db, "d1", items=_items(7), status="redacted", sent_at=datetime.now(timezone.utc))
        assert derive_impressions(db, delivery) == 1
        assert delivery.impression_state == "completed"

    def test_never_touches_the_send_lease(self, db):
        """P1-27 / 重复发送：派生成功或失败都不得清空发送租约。"""
        now = datetime.now(timezone.utc)
        lease_until = _naive(now + timedelta(seconds=30))
        ok = _add_delivery(
            db, "d1", items=_items(7), sent_at=now,
            lease_owner="wecom-outbox", lease_expires_at=lease_until,
            impression_lease_owner="impression-worker", impression_lease_expires_at=lease_until,
        )
        derive_impressions(db, ok)
        assert ok.lease_owner == "wecom-outbox"
        assert ok.lease_expires_at == lease_until
        assert ok.impression_lease_owner is None
        assert ok.impression_lease_expires_at is None

        bad = _add_delivery(
            db, "d2", items=_items(8), sent_at=now,
            lease_owner="wecom-outbox", lease_expires_at=lease_until,
        )
        _add_impression(db, delivery_id="d2", target_id=99, exposed_at=now)
        derive_impressions(db, bad)
        assert bad.impression_state == "retry"
        assert bad.lease_owner == "wecom-outbox"
        assert bad.lease_expires_at == lease_until


# ---------------------------------------------------------------------------
# P1-27 §10.5 claim / 租约
# ---------------------------------------------------------------------------

class TestClaimImpressionDeliveries:
    def test_claims_pending_and_marks_processing(self, db):
        _add_delivery(db, "d1", items=_items(7), sent_at=datetime.now(timezone.utc))
        assert claim_impression_deliveries(db) == ["d1"]
        delivery = db.get(RecommendationDelivery, "d1")
        assert delivery.impression_state == "processing"
        assert delivery.impression_lease_owner == "impression-worker"
        assert delivery.impression_lease_expires_at > _naive(datetime.now(timezone.utc))
        # claim 本身不算一次失败尝试。
        assert delivery.impression_attempt_count == 0

    def test_claim_ignores_the_send_lease(self, db):
        """P1-27：发送租约未过期不得永久阻塞派生，也不得被派生任务改写。"""
        now = datetime.now(timezone.utc)
        send_lease = _naive(now + timedelta(minutes=5))
        _add_delivery(
            db, "d1", items=_items(7), sent_at=now,
            lease_owner="wecom-outbox", lease_expires_at=send_lease,
        )
        assert claim_impression_deliveries(db) == ["d1"]
        delivery = db.get(RecommendationDelivery, "d1")
        assert delivery.lease_owner == "wecom-outbox"
        assert delivery.lease_expires_at == send_lease
        assert delivery.impression_lease_owner == "impression-worker"

    def test_processing_row_with_live_lease_is_not_reclaimed(self, db):
        """行锁在 claim 事务 commit 后就没了，正在派生的行只能靠租约挡住。"""
        now = datetime.now(timezone.utc)
        _add_delivery(
            db, "d1", items=_items(7), sent_at=now, impression_state="processing",
            impression_lease_owner="worker-a",
            impression_lease_expires_at=_naive(now + timedelta(seconds=30)),
        )
        assert claim_impression_deliveries(db) == []
        assert db.get(RecommendationDelivery, "d1").impression_lease_owner == "worker-a"

    def test_expired_processing_lease_is_reclaimed_and_counts_as_failure(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(
            db, "d1", items=_items(7), sent_at=now, impression_state="processing",
            impression_lease_owner="worker-a",
            impression_lease_expires_at=_naive(now - timedelta(seconds=1)),
        )
        assert claim_impression_deliveries(db, owner="worker-b") == ["d1"]
        delivery = db.get(RecommendationDelivery, "d1")
        assert delivery.impression_state == "processing"
        assert delivery.impression_lease_owner == "worker-b"
        assert delivery.impression_attempt_count == 1

    def test_legacy_deriving_state_is_recovered(self, db):
        """迁移期兼容：历史行可能写着自造的 ``deriving``。"""
        now = datetime.now(timezone.utc)
        _add_delivery(
            db, "d1", items=_items(7), sent_at=now, impression_state="deriving",
            impression_lease_owner="worker-a",
            impression_lease_expires_at=_naive(now - timedelta(seconds=1)),
        )
        assert claim_impression_deliveries(db) == ["d1"]
        assert db.get(RecommendationDelivery, "d1").impression_state == "processing"

    def test_completed_and_future_attempts_are_skipped(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(db, "d1", items=_items(7), sent_at=now, impression_state="completed")
        _add_delivery(
            db, "d2", items=_items(8), sent_at=now,
            impression_next_attempt_at=_naive(now + timedelta(minutes=5)),
        )
        _add_delivery(db, "d3", items=_items(9), status="sending")
        assert claim_impression_deliveries(db) == []

    def test_respects_limit_and_due_order(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(db, "d1", items=_items(7), sent_at=now,
                      impression_next_attempt_at=_naive(now - timedelta(seconds=5)))
        _add_delivery(db, "d2", items=_items(8), sent_at=now,
                      impression_next_attempt_at=_naive(now - timedelta(seconds=60)))
        assert claim_impression_deliveries(db, limit=1) == ["d2"]


class TestMarkImpressionRetry:
    def test_sets_backoff_and_releases_only_the_impression_lease(self, db):
        now = datetime.now(timezone.utc)
        send_lease = _naive(now + timedelta(minutes=5))
        _add_delivery(
            db, "d1", items=_items(7), sent_at=now, impression_state="processing",
            impression_attempt_count=2,
            lease_owner="wecom-outbox", lease_expires_at=send_lease,
            impression_lease_owner="impression-worker",
            impression_lease_expires_at=_naive(now + timedelta(seconds=30)),
        )
        mark_impression_retry(db, "d1", RuntimeError("boom"))
        delivery = db.get(RecommendationDelivery, "d1")
        assert delivery.impression_state == "retry"
        assert delivery.impression_attempt_count == 3
        assert delivery.impression_last_error == "RuntimeError: boom"
        assert delivery.impression_next_attempt_at > _naive(now)
        assert delivery.impression_lease_owner is None
        assert delivery.impression_lease_expires_at is None
        # 关键：派生失败绝不能把发送租约释放掉，否则会重复发企微消息。
        assert delivery.lease_owner == "wecom-outbox"
        assert delivery.lease_expires_at == send_lease

    def test_missing_delivery_is_a_noop(self, db):
        mark_impression_retry(db, "missing", RuntimeError("boom"))

    def test_backoff_grows_and_is_capped(self, db):
        now = datetime.now(timezone.utc)
        _add_delivery(db, "d1", items=_items(7), sent_at=now, impression_attempt_count=0)
        _add_delivery(db, "d2", items=_items(8), sent_at=now, impression_attempt_count=99)
        mark_impression_retry(db, "d1", RuntimeError("boom"))
        mark_impression_retry(db, "d2", RuntimeError("boom"))
        first = db.get(RecommendationDelivery, "d1").impression_next_attempt_at
        capped = db.get(RecommendationDelivery, "d2").impression_next_attempt_at
        assert first <= _naive(now + timedelta(seconds=3))
        assert capped <= _naive(now + timedelta(seconds=301))
        assert capped > _naive(now + timedelta(seconds=250))
