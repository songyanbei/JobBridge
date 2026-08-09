"""「删除我的信息」推荐域闭环单测（方案 §9.11.1 / §10.1.1 / §14.12）。

模型是 MySQL 方言，这里给 sqlite 补编译规则，好在内存库里真跑一遍外键顺序、
分批删除和 JSON 擦除，而不是只验证 Python 分支（只对 sqlite 生效，不动生产 DDL）。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    AuditLog,
    ConversationLog,
    EventLog,
    Job,
    MediaAssetLifecycle,
    RecommendationDelivery,
    RecommendationExposureDaily,
    RecommendationImpression,
    RecommendationRequest,
    RecommendationSearchAttempt,
    Resume,
    TargetCleanupTask,
    User,
    WecomOutboundOutbox,
)
from app.services import recommendation_privacy_service as privacy


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _sqlite_int(type_, compiler, **kw):  # pragma: no cover - DDL 编译钩子
    return "INTEGER"


@compiles(mysql.MEDIUMBLOB, "sqlite")
def _sqlite_blob(type_, compiler, **kw):  # pragma: no cover - DDL 编译钩子
    return "BLOB"


@compiles(mysql.MEDIUMTEXT, "sqlite")
@compiles(mysql.LONGTEXT, "sqlite")
def _sqlite_text(type_, compiler, **kw):  # pragma: no cover - DDL 编译钩子
    return "TEXT"


@compiles(mysql.DATETIME, "sqlite")
def _sqlite_datetime(type_, compiler, **kw):  # pragma: no cover - DDL 编译钩子
    return "DATETIME"


_TABLES = [
    User.__table__,
    Job.__table__,
    Resume.__table__,
    MediaAssetLifecycle.__table__,
    TargetCleanupTask.__table__,
    ConversationLog.__table__,
    AuditLog.__table__,
    EventLog.__table__,
    WecomOutboundOutbox.__table__,
    RecommendationRequest.__table__,
    RecommendationSearchAttempt.__table__,
    RecommendationDelivery.__table__,
    RecommendationImpression.__table__,
    RecommendationExposureDaily.__table__,
]

OWNER = "worker-owner"
VIEWER = "viewer-other"
# 明文电话必须只出现在测试夹具里，不能出现在任何日志/持久化字段中。
PHONE_IN_BODY = "推荐工人 张三 13800001111"


# 屏蔽副作用的 autouse fixture 会替换掉这两个函数本体，需要真跑的用例用这里的原件。
_REAL_SCRUB = privacy.scrub_recommendation_sessions
_REAL_ENQUEUE = privacy.enqueue_privacy_retry
_REAL_DEPTH = privacy.privacy_retry_depth
_REAL_POP = privacy.pop_privacy_retry


@contextmanager
def _sqlite_server_defaults():
    """``CURRENT_TIMESTAMP(6)`` 是 MySQL 语法，建 sqlite 表时临时降级再还原。"""
    saved = []
    for table in _TABLES:
        for column in table.columns:
            default = column.server_default
            arg = str(getattr(default, "arg", "")) if default is not None else ""
            if "CURRENT_TIMESTAMP(" in arg:
                saved.append((column, default))
                column.server_default = sa.DefaultClause(sa.text("CURRENT_TIMESTAMP"))
    try:
        yield
    finally:
        for column, default in saved:
            column.server_default = default


@pytest.fixture()
def db():
    engine = sa.create_engine("sqlite://")
    # 不能用 create_all：sqlite 的索引名是库级唯一的，而 event_log/audit_log 都叫
    # idx_target、event_log/conversation_log 都叫 idx_user_time。这里只建表结构，
    # 索引对断言没有影响。
    with _sqlite_server_defaults(), engine.begin() as conn:
        for table in _TABLES:
            conn.execute(sa.schema.CreateTable(table))
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _stub_side_effects(monkeypatch):
    """默认屏蔽 Redis 与对象存储；需要断言的用例自己再 monkeypatch。"""
    monkeypatch.setattr(privacy, "scrub_recommendation_sessions", lambda *a, **k: 0)
    monkeypatch.setattr(privacy, "enqueue_privacy_retry", lambda *a, **k: True)
    import app.storage as storage_module

    monkeypatch.setattr(storage_module, "get_storage", lambda *a, **k: _FakeStorage())


class _FakeStorage:
    deleted: list[str] = []

    def delete(self, key):
        _FakeStorage.deleted.append(key)


def _naive(moment: datetime) -> datetime:
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


_ORDER = iter(range(1, 10_000))


def _add_user(db, userid: str) -> User:
    user = User(external_userid=userid, role="worker", status="active")
    db.add(user)
    db.flush()
    return user


def _add_resume(db, owner: str, resume_id: int, images=None) -> Resume:
    resume = Resume(
        id=resume_id,
        owner_userid=owner,
        expected_cities=["苏州市"],
        expected_job_categories=["电子厂"],
        salary_expect_floor_monthly=5000,
        gender="男",
        age=30,
        accept_long_term=1,
        accept_short_term=0,
        raw_text="求职",
        images=images,
        audit_status="passed",
        expires_at=datetime(2030, 1, 1),
        version=1,
    )
    db.add(resume)
    db.flush()
    return resume


def _add_job(db, owner: str, job_id: int, images=None) -> Job:
    job = Job(
        id=job_id,
        owner_userid=owner,
        city="苏州市",
        job_category="普工",
        salary_floor_monthly=5000,
        pay_type="月薪",
        headcount=10,
        gender_required="不限",
        is_long_term=1,
        raw_text="招聘",
        images=images,
        audit_status="passed",
        activated_at=datetime(2026, 1, 1),
        expires_at=datetime(2030, 1, 1),
        version=1,
    )
    db.add(job)
    db.flush()
    return job


def _add_attached_media(db, owner: str, entity_type: str, entity_id: int, key: str):
    media = MediaAssetLifecycle(
        object_key=key,
        owner_userid=owner,
        entity_type=entity_type,
        entity_id=entity_id,
        state="attached",
    )
    db.add(media)
    db.flush()
    return media


def _add_request(db, request_id: str, viewer: str, served_top_ids) -> RecommendationRequest:
    request = RecommendationRequest(
        request_id=request_id,
        source_inbound_msg_id=f"in-{request_id}",
        request_index=0,
        request_kind="initial_search",
        viewer_userid=viewer,
        direction="search_worker",
        query_digest="digest",
        execution_mode="on",
        served_assignment="candidate",
        algorithm_version="recommendation-v1",
        served_top_ids=served_top_ids,
    )
    db.add(request)
    db.flush()
    return request


def _add_attempt(db, attempt_id: str, request_id: str, candidate_ids) -> RecommendationSearchAttempt:
    attempt = RecommendationSearchAttempt(
        attempt_id=attempt_id,
        request_id=request_id,
        attempt_no=0,
        attempt_kind="initial_search",
        criteria_digest="digest",
        scoring_time_utc=_naive(datetime.now(timezone.utc)),
        candidate_ids=list(candidate_ids),
        precision_pool_ids=list(candidate_ids),
        algorithm_version="recommendation-v1",
    )
    db.add(attempt)
    db.flush()
    return attempt


def _add_delivery(
    db,
    delivery_id: str,
    *,
    userid: str,
    request_id: str,
    target_ids,
    status: str = "sent",
    body: str = PHONE_IN_BODY,
    session_patch: bytes | None = b"session-patch",
) -> RecommendationDelivery:
    delivery = RecommendationDelivery(
        delivery_id=delivery_id,
        delivery_order=next(_ORDER),
        source_inbound_msg_id=f"in-{delivery_id}",
        reply_index=0,
        request_id=request_id,
        snapshot_id=f"snap-{delivery_id}",
        userid=userid,
        content_ciphertext=body.encode("utf-8"),
        content_expires_at=_naive(datetime.now(timezone.utc) + timedelta(hours=24)),
        session_patch_ciphertext=session_patch,
        recommendation_context={
            "direction": "search_worker",
            "algorithm_version": "recommendation-v1",
            "served_top_ids": [str(t) for t in target_ids],
            "items": [
                {"target_type": "resume", "target_id": t, "position": i}
                for i, t in enumerate(target_ids, start=1)
            ],
        },
        status=status,
        session_commit_token=delivery_id,
        next_attempt_at=_naive(datetime.now(timezone.utc)),
        impression_next_attempt_at=_naive(datetime.now(timezone.utc)),
        impression_state="completed" if status == "sent" else "pending",
        impression_expected_count=len(target_ids),
        impression_actual_count=len(target_ids) if status == "sent" else 0,
        sent_at=_naive(datetime.now(timezone.utc)) if status == "sent" else None,
    )
    db.add(delivery)
    db.flush()
    return delivery


def _add_impression(db, delivery_id: str, request_id: str, viewer: str, target_id: int) -> None:
    db.add(RecommendationImpression(
        delivery_id=delivery_id,
        request_id=request_id,
        snapshot_id=f"snap-{delivery_id}",
        viewer_userid=viewer,
        direction="search_worker",
        target_type="resume",
        target_id=target_id,
        position=1,
        algorithm_version="recommendation-v1",
        assignment="candidate",
        query_digest="digest",
        exposed_at=_naive(datetime.now(timezone.utc)),
    ))
    db.flush()


# ---------------------------------------------------------------------------
# §9.11.1 行 2147：命令执行当下立即清正文
# ---------------------------------------------------------------------------

class TestImmediateRedaction:
    def test_clears_body_patch_and_shortens_ttl(self, db):
        _add_user(db, OWNER)
        _add_request(db, "req-own", OWNER, ["1"])
        sent = _add_delivery(db, "d-sent", userid=OWNER, request_id="req-own", target_ids=[1])
        prepared = _add_delivery(
            db, "d-prepared", userid=OWNER, request_id="req-own",
            target_ids=[1], status="prepared",
        )
        db.add(WecomOutboundOutbox(
            inbound_event_id=1, reply_index=0, userid=OWNER,
            msg_type="text", recommendation_delivery_id="d-prepared", status="pending",
        ))
        db.flush()

        moment = datetime.now(timezone.utc)
        changed = privacy.redact_user_recommendation_content(db, OWNER, now=moment)

        assert changed == 2
        assert sent.content_ciphertext is None
        assert sent.session_patch_ciphertext is None
        # §9.6 的状态枚举里没有 `redacted`：`sent` 是终态，投递状态与正文是否
        # 还在是两件事，清正文不改状态。
        assert sent.status == "sent"
        assert sent.content_expires_at <= _naive(moment)
        # 还没确认发出的 delivery 不能停在「可重试但正文没了」的僵尸态，
        # 只能落 §9.6 的合法终态 permanent_failed。
        assert prepared.status == "permanent_failed"
        outbox = db.query(WecomOutboundOutbox).one()
        assert outbox.status == "dead_letter"

    def test_is_idempotent(self, db):
        _add_user(db, OWNER)
        _add_request(db, "req-own", OWNER, ["1"])
        _add_delivery(db, "d-sent", userid=OWNER, request_id="req-own", target_ids=[1])

        assert privacy.redact_user_recommendation_content(db, OWNER) == 1
        assert privacy.redact_user_recommendation_content(db, OWNER) == 0


# ---------------------------------------------------------------------------
# §9.11.1 步骤 1~7 闭环
# ---------------------------------------------------------------------------

def _build_full_fixture(db):
    """被删用户 OWNER 拥有简历 7；VIEWER 收到过一条包含简历 7 和 9 的推荐。"""
    _add_user(db, OWNER)
    _add_user(db, VIEWER)
    _add_resume(db, OWNER, 7, images=["oss/resume-7.jpg"])
    _add_attached_media(db, OWNER, "resume", 7, "oss/resume-7.jpg")
    _add_resume(db, VIEWER, 9)

    # OWNER 自己作为 viewer 的推荐事实
    own_request = _add_request(db, "req-own", OWNER, ["9"])
    _add_attempt(db, "att-own", "req-own", ["9"])
    own_request.served_attempt_id = "att-own"
    _add_delivery(db, "d-own", userid=OWNER, request_id="req-own", target_ids=[9])
    _add_impression(db, "d-own", "req-own", OWNER, 9)
    db.add(EventLog(
        event_type="miniprogram_click", userid=OWNER, target_type="resume",
        target_id=9, occurred_at=_naive(datetime.now(timezone.utc)),
        delivery_id="d-own", request_id="req-own",
    ))

    # VIEWER 收到的、引用了 OWNER 简历 7 的推荐
    other_request = _add_request(db, "req-other", VIEWER, ["7", "9"])
    _add_attempt(db, "att-other", "req-other", ["7", "9"])
    other_request.served_attempt_id = "att-other"
    other = _add_delivery(
        db, "d-other", userid=VIEWER, request_id="req-other", target_ids=[7, 9],
    )
    _add_impression(db, "d-other", "req-other", VIEWER, 7)
    _add_impression(db, "d-other", "req-other", VIEWER, 9)
    db.add(EventLog(
        event_type="miniprogram_click", userid=VIEWER, target_type="resume",
        target_id=7, occurred_at=_naive(datetime.now(timezone.utc)),
        delivery_id="d-other", request_id="req-other",
    ))
    db.add(ConversationLog(
        userid=VIEWER, direction="out", msg_type="text",
        content=PHONE_IN_BODY, recommendation_delivery_id="d-other",
        criteria_snapshot={"items": [7, 9]},
        expires_at=datetime(2030, 1, 1),
    ))
    db.add(ConversationLog(
        userid=OWNER, direction="in", msg_type="text", content="苏州找电子厂",
        expires_at=datetime(2030, 1, 1),
    ))
    for target_id in (7, 9):
        db.add(RecommendationExposureDaily(
            stat_date=date(2026, 7, 25), target_type="resume",
            target_id=target_id, impression_count=3,
        ))
    db.flush()
    return other


class TestDeleteRecommendationUserData:
    def test_removes_every_viewer_side_fact(self, db):
        _build_full_fixture(db)

        report = privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        assert report.ok, report.failed_steps
        assert db.query(RecommendationRequest).filter_by(viewer_userid=OWNER).count() == 0
        assert db.get(RecommendationSearchAttempt, "att-own") is None
        assert db.get(RecommendationDelivery, "d-own") is None
        assert db.query(RecommendationImpression).filter_by(viewer_userid=OWNER).count() == 0
        assert db.query(EventLog).filter_by(userid=OWNER).count() == 0
        # 别人的 request/attempt 不能被误删
        assert db.get(RecommendationRequest, "req-other") is not None
        assert db.get(RecommendationSearchAttempt, "att-other") is not None

    def test_scrubs_other_users_delivery_and_recounts_impressions(self, db):
        other = _build_full_fixture(db)

        privacy.delete_recommendation_user_data(db, OWNER, commit=False)
        db.refresh(other)

        assert other.content_ciphertext is None
        assert other.content_expires_at <= _naive(datetime.now(timezone.utc))
        remaining = [i["target_id"] for i in other.recommendation_context["items"]]
        assert remaining == [9]
        assert other.recommendation_context["served_top_ids"] == ["9"]
        # 步骤 4 的「重算 count」必须在步骤 3 删掉 impression 之后才是终值。
        assert other.impression_expected_count == 1
        assert other.impression_actual_count == 1
        assert other.impression_state == "completed"

        request = db.get(RecommendationRequest, "req-other")
        attempt = db.get(RecommendationSearchAttempt, "att-other")
        assert request.served_top_ids == ["9"]
        assert attempt.candidate_ids == ["9"]
        assert attempt.precision_pool_ids == ["9"]

    def test_removes_target_side_facts_and_daily_rows(self, db):
        _build_full_fixture(db)

        privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        assert db.query(RecommendationImpression).filter_by(target_id=7).count() == 0
        assert db.query(RecommendationImpression).filter_by(target_id=9).count() == 1
        assert db.query(EventLog).filter_by(target_id=7).count() == 0
        # 步骤 7：带 target_id 的日聚合是可回溯的，必须整行删；别人的行保留计数。
        assert db.query(RecommendationExposureDaily).filter_by(target_id=7).count() == 0
        kept = db.query(RecommendationExposureDaily).filter_by(target_id=9).one()
        assert kept.impression_count == 3

    def test_overwrites_other_users_conversation_log_with_placeholder(self, db):
        _build_full_fixture(db)

        privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        log = db.query(ConversationLog).filter_by(userid=VIEWER).one()
        assert log.content == privacy.REDACTED_PLACEHOLDER
        assert log.content != PHONE_IN_BODY
        assert log.criteria_snapshot is None
        assert log.redaction_state == "redacted"

    def test_hands_owned_content_to_durable_media_cleanup(self, db):
        _build_full_fixture(db)
        _FakeStorage.deleted = []

        privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        assert db.query(ConversationLog).filter_by(userid=OWNER).count() == 0
        owned = db.query(Resume).filter_by(owner_userid=OWNER).one()
        assert owned.deleted_at is not None
        assert db.query(Resume).filter_by(owner_userid=VIEWER).count() == 1
        media = db.query(MediaAssetLifecycle).filter_by(
            object_key="oss/resume-7.jpg"
        ).one()
        assert media.state == "delete_pending"
        assert media.next_attempt_at is not None
        assert _FakeStorage.deleted == []

    def test_hands_soft_deleted_jobs_to_target_and_media_cleanup(self, db):
        _add_user(db, OWNER)
        job = _add_job(db, OWNER, 11, images=["oss/job-11.jpg"])
        job.deleted_at = datetime(2026, 1, 2)
        job.delist_reason = "manual_delist"
        media = _add_attached_media(db, OWNER, "job", 11, "oss/job-11.jpg")
        db.flush()

        report = privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        assert report.ok, report.failed_steps
        assert job.deleted_at is not None
        assert job.delist_reason == "manual_delist"
        assert job.version == 1
        db.refresh(media)
        assert media.state == "delete_pending"
        task = db.query(TargetCleanupTask).filter_by(
            target_type="job", target_id=job.id
        ).one()
        assert task.reason == "manual_delete"
        assert task.status == "pending"

    def test_active_job_anomaly_fails_closed_without_deleting_media(self, db):
        _add_user(db, OWNER)
        job = _add_job(db, OWNER, 12, images=["oss/job-12.jpg"])
        media = _add_attached_media(db, OWNER, "job", 12, "oss/job-12.jpg")

        report = privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        assert not report.ok
        assert "delete_owned_content" in report.failed_steps
        assert job.deleted_at is None
        assert job.delist_reason is None
        assert job.version == 1
        db.refresh(media)
        assert media.state == "attached"
        assert db.query(TargetCleanupTask).count() == 0

    def test_owned_content_handoff_uses_collected_target_snapshot(self, db, monkeypatch):
        _add_user(db, OWNER)
        first = _add_resume(db, OWNER, 21, images=["oss/resume-21.jpg"])
        first_media = _add_attached_media(
            db, OWNER, "resume", 21, "oss/resume-21.jpg"
        )
        later = _add_resume(db, OWNER, 22, images=["oss/resume-22.jpg"])
        later_media = _add_attached_media(
            db, OWNER, "resume", 22, "oss/resume-22.jpg"
        )
        monkeypatch.setattr(
            privacy,
            "owned_target_refs",
            lambda *_args: [privacy.TargetRef("resume", first.id)],
        )

        report = privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        assert report.ok, report.failed_steps
        assert first.deleted_at is not None
        assert later.deleted_at is None
        db.refresh(first_media)
        db.refresh(later_media)
        assert first_media.state == "delete_pending"
        assert later_media.state == "attached"

    def test_is_idempotent(self, db):
        _build_full_fixture(db)

        first = privacy.delete_recommendation_user_data(db, OWNER, commit=False)
        second = privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        assert first.ok and second.ok
        assert second.rows.get("viewer_request", 0) == 0
        assert second.rows.get("target_impression", 0) == 0
        assert second.rows.get("resume", 0) == 0
        assert db.query(Resume).filter_by(owner_userid=OWNER).one().deleted_at is not None
        log = db.query(ConversationLog).filter_by(userid=VIEWER).one()
        assert log.content == privacy.REDACTED_PLACEHOLDER

    def test_session_scrub_covers_own_and_other_deliveries(self, db, monkeypatch):
        _build_full_fixture(db)
        seen: dict = {}

        def _scrub(delivery_ids, targets, **kwargs):
            seen["ids"] = set(delivery_ids)
            seen["owner"] = kwargs.get("owner_userid")
            return 0

        monkeypatch.setattr(privacy, "scrub_recommendation_sessions", _scrub)

        privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        # 自己的 delivery 索引键也得删，否则 Redis 里还留着可反查的 delivery ID。
        assert seen["ids"] == {"d-own", "d-other"}
        assert seen["owner"] == OWNER

    def test_batched_commit_mode_completes(self, db):
        """延迟硬删任务用 commit=True 逐批提交，避免长事务。"""
        _build_full_fixture(db)

        report = privacy.delete_recommendation_user_data(db, OWNER, commit=True)

        assert report.ok, report.failed_steps
        assert db.query(RecommendationRequest).filter_by(viewer_userid=OWNER).count() == 0
        assert db.query(Resume).filter_by(owner_userid=OWNER).one().deleted_at is not None
        assert db.query(RecommendationExposureDaily).filter_by(target_id=7).count() == 0

    def test_logs_never_contain_userid_target_or_body(self, db, caplog):
        _build_full_fixture(db)

        with caplog.at_level(logging.INFO, logger=privacy.logger.name):
            privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        blob = "\n".join(record.getMessage() for record in caplog.records)
        assert blob, "闭环必须留下可观测的批次日志"
        assert OWNER not in blob
        assert VIEWER not in blob
        assert "13800001111" not in blob
        assert "张三" not in blob
        assert "resume-7" not in blob

    def test_failed_step_is_reported_not_swallowed(self, db, monkeypatch, caplog):
        _build_full_fixture(db)
        # 真实场景里失败异常是 SQLAlchemy 错误，消息里带着 SQL 和绑定参数；
        # 这里用同样带敏感串的异常验证日志不会把它们抄出去。
        boom = RuntimeError(f"SQL failed for {OWNER} target 7 body {PHONE_IN_BODY}")
        monkeypatch.setattr(
            privacy, "redact_conversation_logs",
            lambda *a, **k: (_ for _ in ()).throw(boom),
        )

        with caplog.at_level(logging.DEBUG, logger=privacy.logger.name):
            report = privacy.delete_recommendation_user_data(db, OWNER, commit=False)

        assert not report.ok
        assert "redact_conversation_logs" in report.failed_steps
        owned = db.query(Resume).filter_by(owner_userid=OWNER).one()
        media = db.query(MediaAssetLifecycle).filter_by(entity_id=owned.id).one()
        assert owned.deleted_at is None
        assert media.state == "attached"
        # 正文清理排在失败步骤之前：§10.1.1 行 2240 不允许留下可解密正文。
        assert db.get(RecommendationDelivery, "d-other").content_ciphertext is None
        blob = "\n".join(
            record.getMessage() + str(record.exc_info or "") for record in caplog.records
        )
        assert OWNER not in blob
        assert "13800001111" not in blob
        assert "RuntimeError" in blob


# ---------------------------------------------------------------------------
# §10.1.1 行 2228-2229：Redis session 索引反查，禁止 KEYS 扫描
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self, index: dict[str, set[str]]):
        self._index = index
        self.deleted: list[str] = []

    def smembers(self, key):
        return set(self._index.get(key, set()))

    def delete(self, key):
        self.deleted.append(key)
        self._index.pop(key, None)

    def keys(self, *args, **kwargs):  # pragma: no cover - 必须永远不被调用
        raise AssertionError("§10.1.1 禁止全库 KEYS 扫描")


class TestSessionScrub:
    def test_rewrites_history_shown_items_and_snapshot(self, monkeypatch):
        from app.core import redis_client

        fake = _FakeRedis({
            f"{privacy.SESSION_DELIVERY_INDEX_PREFIX}d-other": {VIEWER},
            f"{privacy.SESSION_TARGET_INDEX_PREFIX}resume:7": {VIEWER},
        })
        stored = {
            VIEWER: {
                "session_version": 4,
                "history": [
                    {"role": "user", "content": "找工人"},
                    {"role": "assistant", "content": PHONE_IN_BODY, "delivery_id": "d-other"},
                ],
                "shown_items": ["7", "9"],
                "candidate_snapshot": {
                    "direction": "search_worker",
                    "candidate_ids": ["7", "9"],
                    "ranking_metadata": {
                        "candidate_scores": {
                            "7": {"final_score": 0.8},
                            "9": {"final_score": 0.7},
                        },
                        "precision_pool_ids": ["7", "9"],
                    },
                },
            },
        }
        saved: dict = {}

        def _save(userid, session, expected_version, *a, **k):
            assert expected_version == 4
            saved[userid] = session
            return True

        monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
        monkeypatch.setattr(redis_client, "get_session", lambda uid: stored.get(uid))
        monkeypatch.setattr(redis_client, "save_session_if_version", _save)

        rewritten = _REAL_SCRUB(
            ["d-other"], [privacy.TargetRef("resume", 7)], owner_userid=OWNER,
        )

        assert rewritten == 1
        session = saved[VIEWER]
        assert session["history"][1] == {
            "role": "assistant", "content": privacy.REDACTED_PLACEHOLDER,
        }
        assert PHONE_IN_BODY not in str(session)
        assert session["shown_items"] == ["9"]
        assert session["candidate_snapshot"]["candidate_ids"] == ["9"]
        assert session["candidate_snapshot"]["ranking_metadata"] == {
            "candidate_scores": {"9": {"final_score": 0.7}},
            "precision_pool_ids": ["9"],
        }
        # 版本 +1，让按旧版本算好的 staged mutation CAS 失败，不会把正文写回来。
        assert session["session_version"] == 5
        # 索引用完即删，且全程没有 KEYS 扫描。
        assert set(fake.deleted) == {
            f"{privacy.SESSION_DELIVERY_INDEX_PREFIX}d-other",
            f"{privacy.SESSION_TARGET_INDEX_PREFIX}resume:7",
        }

    def test_skips_owner_session(self, monkeypatch):
        from app.core import redis_client

        fake = _FakeRedis({f"{privacy.SESSION_DELIVERY_INDEX_PREFIX}d-own": {OWNER}})
        monkeypatch.setattr(redis_client, "get_redis", lambda: fake)
        monkeypatch.setattr(
            redis_client, "get_session",
            lambda uid: pytest.fail("被删用户自己的 session 由 clear_session 处理"),
        )

        assert _REAL_SCRUB(["d-own"], [], owner_userid=OWNER) == 0


# ---------------------------------------------------------------------------
# §10.1.1 行 2240：失败进入可观测重试队列
# ---------------------------------------------------------------------------

class TestRetryQueue:
    def test_enqueue_and_pop_roundtrip(self, monkeypatch):
        from app.core import redis_client

        queue: dict[str, list[str]] = {}

        class _Queue:
            def rpush(self, key, value):
                queue.setdefault(key, []).append(value)

            def lpop(self, key):
                items = queue.get(key) or []
                return items.pop(0) if items else None

            def llen(self, key):
                return len(queue.get(key) or [])

        monkeypatch.setattr(redis_client, "get_redis", lambda: _Queue())

        assert _REAL_ENQUEUE(
            OWNER, batch_id="b1", failed_steps=["delete_viewer_facts"],
        )
        assert _REAL_DEPTH()["pending"] == 1
        job = _REAL_POP()
        assert job["userid"] == OWNER
        assert job["attempt"] == 1
        assert job["failed_steps"] == ["delete_viewer_facts"]
        assert _REAL_POP() is None

    def test_exhausted_attempts_go_to_dead_letter(self, monkeypatch):
        from app.core import redis_client

        queue: dict[str, list[str]] = {}

        class _Queue:
            def rpush(self, key, value):
                queue.setdefault(key, []).append(value)

        monkeypatch.setattr(redis_client, "get_redis", lambda: _Queue())
        _REAL_ENQUEUE(
            OWNER, batch_id="b1", failed_steps=["x"],
            attempt=privacy.PRIVACY_RETRY_MAX_ATTEMPTS,
        )
        assert privacy.PRIVACY_RETRY_DEAD_QUEUE in queue
        assert privacy.PRIVACY_RETRY_QUEUE not in queue


# ---------------------------------------------------------------------------
# 命令路径：§14.12 行 3392「/删除我的信息 执行时正文和 patch 立即清空」
# ---------------------------------------------------------------------------

class TestDeleteCommandPath:
    def test_command_redacts_bodies_without_deleting_facts(self, db, monkeypatch):
        from app.services import user_service

        monkeypatch.setattr(
            user_service.conversation_service, "clear_session", lambda uid: None,
        )
        _build_full_fixture(db)

        reply = user_service.delete_user_data(OWNER, db)

        assert "删除" in reply
        own = db.get(RecommendationDelivery, "d-own")
        assert own.content_ciphertext is None
        assert own.session_patch_ciphertext is None
        # 清正文不改 sent 终态（§9.6 的状态枚举里没有 `redacted`）。
        assert own.status == "sent"
        # 命令阶段只脱敏；事实行留给延迟硬删，保留可撤回窗口。
        assert db.query(RecommendationRequest).filter_by(viewer_userid=OWNER).count() == 1
        assert db.query(RecommendationImpression).filter_by(viewer_userid=OWNER).count() == 1
        # 不做假名化：viewer_userid 必须保持原值，否则延迟硬删再也反查不到。
        assert db.get(RecommendationRequest, "req-own").viewer_userid == OWNER
        assert db.get(User, OWNER).status == "deleted"


# ---------------------------------------------------------------------------
# ID 擦除的类型隔离
# ---------------------------------------------------------------------------

class TestIdStripping:
    def test_does_not_confuse_job_and_resume_ids(self, db):
        """岗位 7 和简历 7 是两个不同实体，方向决定裸 ID 列表该删哪一个。"""
        _add_user(db, OWNER)
        _add_user(db, VIEWER)
        _add_resume(db, OWNER, 7)
        request = _add_request(db, "req-job", VIEWER, ["7"])
        request.direction = "search_job"
        attempt = _add_attempt(db, "att-job", "req-job", ["7"])
        delivery = _add_delivery(
            db, "d-job", userid=VIEWER, request_id="req-job", target_ids=[7],
        )
        context = dict(delivery.recommendation_context)
        context["direction"] = "search_job"
        context["items"] = [{"target_type": "job", "target_id": 7, "position": 1}]
        delivery.recommendation_context = context
        db.flush()

        privacy.delete_recommendation_user_data(db, OWNER, commit=False)
        db.refresh(delivery)
        db.refresh(attempt)

        # 简历 7 被删，但这条推荐推的是岗位 7，不能被牵连清空。
        assert delivery.recommendation_context["items"] == [
            {"target_type": "job", "target_id": 7, "position": 1}
        ]
        assert attempt.candidate_ids == ["7"]
        assert db.get(RecommendationRequest, "req-job").served_top_ids == ["7"]
