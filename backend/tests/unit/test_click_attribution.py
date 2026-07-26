"""点击归因单测（v0.7 §9.9 / §9.12）。

覆盖 code review P1-29 / P2-27 的归因部分：
- 归因必须同时核对持久投递和**曝光事实**，delivery 未发出时不许出现 CTR 分子；
- 上下文不匹配写 `rejected` 行 + 安全日志，且不允许污染真实用户的幂等键；
- `occurred_at` 按 UTC 落库，不受宿主机时区影响；
- `attribution_dedupe_key` 拼接与方案逐字一致，唯一键竞态可幂等兜底。

模型用的是 MySQL 方言类型，这里给 sqlite 补上编译规则，好在内存库里真正跑一遍
唯一键约束（只对 sqlite 生效，不影响生产 DDL）。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker

from app.db import Base
from app.models import (
    EventLog,
    RecommendationDelivery,
    RecommendationImpression,
)
from app.schemas.event import MiniProgramClickRequest
from app.services import event_service


@compiles(mysql.TINYINT, "sqlite")
@compiles(mysql.SMALLINT, "sqlite")
@compiles(mysql.INTEGER, "sqlite")
@compiles(mysql.BIGINT, "sqlite")
def _sqlite_int(type_, compiler, **kw):  # pragma: no cover - DDL 编译钩子
    return "INTEGER"


@compiles(mysql.MEDIUMBLOB, "sqlite")
def _sqlite_blob(type_, compiler, **kw):  # pragma: no cover - DDL 编译钩子
    return "BLOB"


@compiles(mysql.DATETIME, "sqlite")
def _sqlite_datetime(type_, compiler, **kw):  # pragma: no cover - DDL 编译钩子
    return "DATETIME"


_TABLES = [
    EventLog.__table__,
    RecommendationDelivery.__table__,
    RecommendationImpression.__table__,
]

DELIVERY_ID = "11111111-1111-1111-1111-111111111111"
REQUEST_ID = "22222222-2222-2222-2222-222222222222"
SNAPSHOT_ID = "33333333-3333-3333-3333-333333333333"


@pytest.fixture()
def db():
    engine = sa.create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=_TABLES)
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """老客户端链路会打 Redis，单测里换成内存实现。"""
    marked: set[tuple] = set()

    def _mark(userid, target_type, target_id, ttl=600):
        key = (userid, target_type, str(target_id))
        if key in marked:
            return False
        marked.add(key)
        return True

    def _clear(userid, target_type, target_id):
        marked.discard((userid, target_type, str(target_id)))

    monkeypatch.setattr(event_service, "mark_event_idem", _mark)
    monkeypatch.setattr(event_service, "clear_event_idem", _clear)
    monkeypatch.setattr(event_service, "_get_dedupe_ttl", lambda _db: 600)
    return marked


@pytest.fixture()
def security_logs(monkeypatch):
    captured: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        event_service, "log_event",
        lambda event, **fields: captured.append((event, fields)),
    )
    return captured


def _add_delivery(db, *, userid="worker-1", status="sent", snapshot_id=SNAPSHOT_ID):
    db.add(RecommendationDelivery(
        delivery_id=DELIVERY_ID,
        delivery_order=1,
        source_inbound_msg_id="msg-1",
        reply_index=0,
        request_id=REQUEST_ID,
        snapshot_id=snapshot_id,
        userid=userid,
        recommendation_context={"items": [{"target_type": "job", "target_id": 42, "position": 1}]},
        status=status,
        session_commit_token="token-1",
    ))
    db.commit()


def _add_impression(db, *, userid="worker-1", target_id=42, position=1, is_exploration=True):
    db.add(RecommendationImpression(
        delivery_id=DELIVERY_ID,
        request_id=REQUEST_ID,
        snapshot_id=SNAPSHOT_ID,
        viewer_userid=userid,
        direction="search_job",
        target_type="job",
        target_id=target_id,
        position=position,
        strategy_version_id=7,
        algorithm_version="v1.2.0",
        assignment="v1",
        is_exploration=is_exploration,
        query_digest="abcdef0123456789",
        exposed_at=datetime(2026, 7, 26, 1, 0, 0),
    ))
    db.commit()


def _events(db) -> list[EventLog]:
    return db.query(EventLog).order_by(EventLog.id).all()


# ---------------------------------------------------------------------------
# §9.9 幂等合同
# ---------------------------------------------------------------------------

def test_dedupe_key_matches_plan_formula_byte_for_byte():
    expected = hashlib.sha256(
        f"miniprogram_click|{DELIVERY_ID}|job|42".encode("utf-8")
    ).hexdigest()

    assert event_service.build_attribution_dedupe_key(
        "miniprogram_click", DELIVERY_ID, "job", 42,
    ) == expected


# ---------------------------------------------------------------------------
# §9.9 归因成功路径
# ---------------------------------------------------------------------------

def test_attributed_click_copies_fields_from_impression_fact(db):
    _add_delivery(db)
    _add_impression(db)

    result = event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42,
        delivery_id=DELIVERY_ID, client_event_id="c-1",
    )

    assert result == event_service.ClickResult(deduped=False, attribution_status="attributed")
    (row,) = _events(db)
    assert row.attribution_status == "attributed"
    assert row.delivery_id == DELIVERY_ID
    assert row.request_id == REQUEST_ID
    assert row.snapshot_id == SNAPSHOT_ID
    assert row.position == 1
    assert row.attributed_strategy_version_id == 7
    assert row.attributed_algorithm_version == "v1.2.0"
    assert bool(row.attributed_is_exploration) is True
    assert row.client_event_id == "c-1"
    assert row.attribution_dedupe_key == event_service.build_attribution_dedupe_key(
        "miniprogram_click", DELIVERY_ID, "job", 42,
    )


def test_client_supplied_position_never_overrides_the_exposure_fact(db):
    _add_delivery(db)
    _add_impression(db, position=3)

    event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42,
        delivery_id=DELIVERY_ID, position=1,
    )

    (row,) = _events(db)
    assert row.position == 3


def test_repeat_attributed_click_is_idempotent(db):
    _add_delivery(db)
    _add_impression(db)

    first = event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42, delivery_id=DELIVERY_ID,
    )
    second = event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42, delivery_id=DELIVERY_ID,
    )

    assert first.deduped is False
    assert second.deduped is True
    assert len(_events(db)) == 1


def test_unique_key_absorbs_the_check_then_insert_race(db, monkeypatch):
    """先查后插的竞态：快路径查空也必须靠唯一键幂等收敛（P2-27）。"""
    _add_delivery(db)
    _add_impression(db)
    event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42, delivery_id=DELIVERY_ID,
    )
    monkeypatch.setattr(event_service, "_attribution_already_recorded", lambda *_: False)

    result = event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42, delivery_id=DELIVERY_ID,
    )

    assert result.deduped is True
    assert len(_events(db)) == 1


# ---------------------------------------------------------------------------
# §9.9 拒绝路径
# ---------------------------------------------------------------------------

def test_click_without_impression_fact_is_rejected_not_attributed(db, security_logs):
    """P1-29：delivery 还没 sent/派生时归因会让 CTR 分子先于分母出现。"""
    _add_delivery(db, status="prepared")

    with pytest.raises(event_service.ClickAttributionRejected) as excinfo:
        event_service.record_click(
            db, userid="worker-1", target_type="job", target_id=42, delivery_id=DELIVERY_ID,
        )

    assert excinfo.value.reason == "impression_not_found"
    (row,) = _events(db)
    assert row.attribution_status == "rejected"
    assert row.attributed_strategy_version_id is None
    assert row.attribution_dedupe_key is None
    assert row.extra == {"reject_reason": "impression_not_found"}
    assert security_logs[0][0] == "recommendation_click_attribution_rejected"
    assert security_logs[0][1]["reason"] == "impression_not_found"


def test_security_log_hashes_the_userid(db, security_logs):
    with pytest.raises(event_service.ClickAttributionRejected):
        event_service.record_click(
            db, userid="worker-1", target_type="job", target_id=42, delivery_id=DELIVERY_ID,
        )

    fields = security_logs[0][1]
    assert fields["reason"] == "delivery_not_found"
    assert "worker-1" not in str(fields)
    assert fields["user_hash"] and fields["user_hash"] != "worker-1"


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"userid": "attacker"}, "delivery_userid_mismatch"),
        ({"request_id": "44444444-4444-4444-4444-444444444444"}, "request_id_mismatch"),
        ({"snapshot_id": "55555555-5555-5555-5555-555555555555"}, "snapshot_id_mismatch"),
    ],
)
def test_context_mismatch_is_rejected(db, security_logs, kwargs, reason):
    _add_delivery(db)
    _add_impression(db)
    call = {
        "userid": "worker-1", "target_type": "job", "target_id": 42,
        "delivery_id": DELIVERY_ID,
    }
    call.update(kwargs)

    with pytest.raises(event_service.ClickAttributionRejected) as excinfo:
        event_service.record_click(db, **call)

    assert excinfo.value.reason == reason
    assert [row.attribution_status for row in _events(db)] == ["rejected"]


def test_rejected_click_does_not_poison_the_real_owner_dedupe_key(db, security_logs):
    _add_delivery(db)
    _add_impression(db)

    with pytest.raises(event_service.ClickAttributionRejected):
        event_service.record_click(
            db, userid="attacker", target_type="job", target_id=42, delivery_id=DELIVERY_ID,
        )
    result = event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42, delivery_id=DELIVERY_ID,
    )

    assert result.deduped is False
    assert [row.attribution_status for row in _events(db)] == ["rejected", "attributed"]


def test_target_outside_the_delivery_is_rejected(db, security_logs):
    _add_delivery(db)
    _add_impression(db, target_id=42)

    with pytest.raises(event_service.ClickAttributionRejected) as excinfo:
        event_service.record_click(
            db, userid="worker-1", target_type="job", target_id=999, delivery_id=DELIVERY_ID,
        )

    assert excinfo.value.reason == "impression_not_found"


# ---------------------------------------------------------------------------
# §9.9 老客户端
# ---------------------------------------------------------------------------

def test_legacy_click_is_saved_but_never_attributed(db):
    result = event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42,
    )

    assert result.attribution_status == "legacy_unattributed"
    (row,) = _events(db)
    assert row.delivery_id is None
    assert row.attribution_dedupe_key is None
    assert row.attributed_strategy_version_id is None


def test_legacy_click_still_uses_the_redis_window(db):
    first = event_service.record_click(db, userid="worker-1", target_type="job", target_id=42)
    second = event_service.record_click(db, userid="worker-1", target_type="job", target_id=42)

    assert (first.deduped, second.deduped) == (False, True)
    assert len(_events(db)) == 1


# ---------------------------------------------------------------------------
# §9.12 时间口径
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("timestamp", [1700000000, 1700000000_000])
def test_occurred_at_is_naive_utc_regardless_of_host_timezone(db, timestamp):
    event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42, timestamp=timestamp,
    )

    (row,) = _events(db)
    assert row.occurred_at.tzinfo is None
    assert row.occurred_at == datetime.fromtimestamp(
        1700000000, tz=timezone.utc,
    ).replace(tzinfo=None)


def test_missing_timestamp_falls_back_to_utc_now(db):
    before = datetime.now(timezone.utc).replace(tzinfo=None)

    event_service.record_click(db, userid="worker-1", target_type="job", target_id=42)

    after = datetime.now(timezone.utc).replace(tzinfo=None)
    (row,) = _events(db)
    assert before <= row.occurred_at <= after


def test_out_of_range_timestamp_degrades_to_utc_now(db):
    # 999999999999 < 10^12，会被当成秒；转出来是公元 33658 年，datetime 直接抛错
    before = datetime.now(timezone.utc).replace(tzinfo=None)

    event_service.record_click(
        db, userid="worker-1", target_type="job", target_id=42, timestamp=999999999999,
    )

    after = datetime.now(timezone.utc).replace(tzinfo=None)
    (row,) = _events(db)
    assert before <= row.occurred_at <= after


# ---------------------------------------------------------------------------
# DTO 合同
# ---------------------------------------------------------------------------

def test_click_dto_attribution_fields_are_all_optional():
    payload = MiniProgramClickRequest(userid="worker-1", target_type="job", target_id=42)

    assert payload.delivery_id is None
    assert payload.request_id is None
    assert payload.snapshot_id is None
    assert payload.position is None
    assert payload.client_event_id is None


def test_click_dto_accepts_the_full_new_client_payload():
    payload = MiniProgramClickRequest(
        userid="worker-1", target_type="job", target_id=42,
        timestamp=1700000000,
        delivery_id=DELIVERY_ID, request_id=REQUEST_ID, snapshot_id=SNAPSHOT_ID,
        position=2, client_event_id="c-1",
    )

    assert payload.delivery_id == DELIVERY_ID
    assert payload.position == 2
