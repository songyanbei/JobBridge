"""投递持久化：信封加密、正文 TTL、事实写入顺序与 context 白名单。

对照方案 §9.4 / §9.5 / §9.6 / §9.11 / §10.1.1 / §10.6。
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models import WecomInboundEvent
from app.schemas.recommendation import (
    DELIVERY_CONTEXT_ITEM_KEYS,
    DELIVERY_CONTEXT_KEYS,
    RecommendationDeliveryContext,
    RecommendationItem,
    RecommendationScoreDetail,
    assert_delivery_context_whitelisted,
    project_delivery_context,
)
from app.services import recommendation_delivery_service as svc


@pytest.fixture
def content_keys(monkeypatch):
    monkeypatch.setattr(
        settings, "recommendation_content_key_ring", "1:key-one,2:key-two",
    )
    monkeypatch.setattr(settings, "recommendation_content_key", "")
    monkeypatch.setattr(settings, "recommendation_content_key_active_version", 2)
    return settings


# ---------------------------------------------------------------------------
# P1-9 信封加密
# ---------------------------------------------------------------------------

def test_envelope_binds_aad_and_active_key_version(content_keys):
    envelope = svc.encrypt_body("正文", delivery_id="d-1", userid="u1")
    assert svc.active_content_key_version() == 2
    assert svc.decrypt_body(envelope, delivery_id="d-1", userid="u1") == "正文"


@pytest.mark.parametrize("kwargs", [
    {"delivery_id": "d-2", "userid": "u1"},
    {"delivery_id": "d-1", "userid": "u2"},
])
def test_envelope_rejects_cross_row_substitution(content_keys, kwargs):
    envelope = svc.encrypt_body("正文", delivery_id="d-1", userid="u1")
    with pytest.raises(svc.ContentEnvelopeError):
        svc.decrypt_body(envelope, **kwargs)


def test_envelope_rejects_cross_purpose_substitution(content_keys):
    envelope = svc.encrypt_session_patch("patch", delivery_id="d-1", userid="u1")
    with pytest.raises(svc.ContentEnvelopeError):
        svc.decrypt_body(envelope, delivery_id="d-1", userid="u1")
    assert svc.decrypt_session_patch(
        envelope, delivery_id="d-1", userid="u1",
    ) == "patch"


def test_envelope_decrypts_with_the_version_recorded_in_the_row(content_keys, monkeypatch):
    old = svc.encrypt_body("旧正文", delivery_id="d-1", userid="u1", key_version=1)
    # 轮换：active 已经切到 2，旧密文仍然必须能按自己那一版解开。
    assert svc.decrypt_body(old, delivery_id="d-1", userid="u1") == "旧正文"
    # 旧 key 被过早退役时 fail-closed，不允许任何硬编码兜底。
    monkeypatch.setattr(settings, "recommendation_content_key_ring", "2:key-two")
    with pytest.raises(RuntimeError):
        svc.decrypt_body(old, delivery_id="d-1", userid="u1")


def test_missing_key_is_fail_closed(monkeypatch):
    monkeypatch.setattr(settings, "recommendation_content_key_ring", "")
    monkeypatch.setattr(settings, "recommendation_content_key", "")
    monkeypatch.setattr(settings, "app_env", "development")
    with pytest.raises(RuntimeError):
        svc.encrypt_body("正文", delivery_id="d-1", userid="u1")


def test_legacy_headerless_envelope_is_rejected(content_keys):
    import base64

    with pytest.raises(svc.ContentEnvelopeError):
        svc.decrypt_body(
            base64.urlsafe_b64encode(b"x" * 40), delivery_id="d-1", userid="u1",
        )


def test_content_digest_is_salted_per_delivery():
    first = svc.content_digest("同一段正文", delivery_id="d-1")
    second = svc.content_digest("同一段正文", delivery_id="d-2")
    assert len(first) == 64 and first != second
    assert first == svc.content_digest("同一段正文", delivery_id="d-1")


# ---------------------------------------------------------------------------
# P1-13 正文 TTL
# ---------------------------------------------------------------------------

def test_content_ttl_follows_status():
    created = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)
    terminal = created + timedelta(hours=5)
    assert svc.content_expires_at_for_status(
        "sent", created_at=created, terminal_at=terminal,
    ) == terminal + timedelta(hours=24)
    assert svc.content_expires_at_for_status(
        "permanent_failed", created_at=created, terminal_at=terminal,
    ) == terminal + timedelta(hours=24)
    assert svc.content_expires_at_for_status(
        "unknown", created_at=created,
    ) == created + timedelta(days=7)
    assert svc.content_expires_at_for_status(
        "prepared", created_at=created,
    ) == created + timedelta(hours=24)


class _Delivery:
    def __init__(self, **kwargs):
        self.delivery_id = "d-1"
        self.userid = "u1"
        self.status = "prepared"
        self.created_at = datetime(2026, 7, 26, 0, 0, tzinfo=timezone.utc)
        self.content_ciphertext = b"cipher"
        self.session_patch_ciphertext = b"patch"
        self.content_expires_at = None
        self.last_error = None
        self.last_error_code = None
        self.__dict__.update(kwargs)


def test_prepared_expires_into_permanent_failed_and_clears_body():
    delivery = _Delivery()
    assert not svc.expire_prepared_delivery(
        delivery, now=delivery.created_at + timedelta(hours=23),
    )
    assert svc.expire_prepared_delivery(
        delivery, now=delivery.created_at + timedelta(hours=25),
    )
    assert delivery.status == "permanent_failed"
    assert delivery.content_ciphertext is None
    assert delivery.session_patch_ciphertext is None


def test_session_patch_is_cleared_without_touching_the_body():
    delivery = _Delivery()
    svc.clear_session_patch(delivery)
    assert delivery.session_patch_ciphertext is None
    assert delivery.content_ciphertext == b"cipher"


# ---------------------------------------------------------------------------
# P2-10 / P2-11 recommendation_context 白名单
# ---------------------------------------------------------------------------

def _context() -> RecommendationDeliveryContext:
    return RecommendationDeliveryContext(
        delivery_id="d-1",
        request_id="r-1",
        snapshot_id="s-1",
        viewer_userid="viewer-1",
        direction="search_job",
        assignment="candidate",
        strategy_version_id=7,
        algorithm_version="recommendation-v1",
        query_digest="digest-1",
        items=[
            RecommendationItem(
                target_type="job", target_id=11, position=1,
                owner_userid="owner-1", final_score=0.8,
                is_exploration=True, reason_codes=["salary_fit"],
                score_detail=RecommendationScoreDetail(
                    match_score=0.9, quality_score=0.5, freshness_score=0.4,
                    exposure_opportunity=0.5, base_score=0.7,
                    repeat_factor=1.0, repeat_adjusted_score=0.7,
                ),
            ),
        ],
    )


def test_projection_drops_identity_fields():
    projected = project_delivery_context(_context())
    assert set(projected) == set(DELIVERY_CONTEXT_KEYS)
    assert "viewer_userid" not in projected
    assert "direction" not in projected
    item = projected["items"][0]
    assert set(item) <= DELIVERY_CONTEXT_ITEM_KEYS
    assert "owner_userid" not in item
    assert item["target_id"] == 11 and item["position"] == 1
    assert item["score_detail"]["match_score"] == 0.9


def test_projection_ignores_unknown_keys_from_raw_dicts():
    raw = {
        "assignment": "candidate",
        "algorithm_version": "recommendation-v1",
        "query_digest": "d",
        "strategy_version_id": 1,
        "viewer_userid": "leaked",
        "raw_query": "张三 13800000000",
        "items": [{
            "target_type": "job", "target_id": 1, "position": 1,
            "owner_userid": "leaked", "title": "泥瓦工", "phone": "13800000000",
            "score_detail": {"match_score": 0.5, "work_experience": "十年"},
        }],
    }
    projected = project_delivery_context(raw)
    assert "raw_query" not in projected and "viewer_userid" not in projected
    assert "title" not in projected["items"][0]
    assert "phone" not in projected["items"][0]
    assert "work_experience" not in projected["items"][0]["score_detail"]


def test_whitelist_assertion_rejects_extra_keys():
    with pytest.raises(ValueError):
        assert_delivery_context_whitelisted({"assignment": "legacy", "viewer_userid": "x"})
    with pytest.raises(ValueError):
        assert_delivery_context_whitelisted(
            {"items": [{"target_type": "job", "owner_userid": "x"}]},
        )


# ---------------------------------------------------------------------------
# P1-24 / P2-14 / P2-26 / P0-5 事实持久化
# ---------------------------------------------------------------------------

class _FakeSession:
    """只记录写入顺序的假 Session。

    真实 DB 里 ``request.served_attempt_id`` 和 ``attempt.request_id`` 互为外键，
    这里断言的正是"先插 request(NULL) → 插 attempt → 回填"这个顺序。
    """

    def __init__(self):
        self.rows: dict[tuple, object] = {}
        self.added: list[object] = []
        self.add_snapshots: list[tuple[str, object]] = []
        self.flushes = 0

    def get(self, model, pk):
        return self.rows.get((model.__name__, pk))

    def add(self, obj):
        self.added.append(obj)
        self.add_snapshots.append(
            (type(obj).__name__, getattr(obj, "served_attempt_id", None)),
        )

    def flush(self):
        self.flushes += 1


def _fact(**overrides) -> dict:
    fact = {
        "request_id": "r-1",
        "source_inbound_msg_id": "wx-msg-1",
        "request_kind": "initial_search",
        "viewer_userid": "u1",
        "direction": "search_job",
        "query_digest": "digest-1",
        "algorithm_version": "recommendation-v1",
        "candidate_count": 2,
        "candidate_ids": ["11", "12"],
        "precision_pool_ids": ["11"],
        "served_top_ids": ["11"],
        "execution_mode": "on",
        "served_assignment": "candidate",
        "served_strategy_version_id": 7,
    }
    fact.update(overrides)
    return fact


def _prepare(db, content_keys, *, ctx=None, fact=None, **kwargs):
    context = _context() if ctx is None else ctx
    payload = context.model_dump(mode="json") if hasattr(context, "model_dump") else context
    return svc.prepare_delivery(
        db,
        inbound_event_id=1,
        reply_index=kwargs.pop("reply_index", 0),
        userid="u1",
        body="推荐正文，含电话 13800000000",
        request_id="r-1",
        delivery_id="d-1",
        recommendation_context=payload,
        source_inbound_msg_id="wx-msg-1",
        request_fact=_fact() if fact is None else fact,
        **kwargs,
    )


def test_served_attempt_id_is_backfilled_after_the_attempt_insert(content_keys):
    db = _FakeSession()
    _prepare(db, content_keys)
    assert [name for name, _ in db.add_snapshots] == [
        "RecommendationRequest",
        "RecommendationSearchAttempt",
        "RecommendationDelivery",
        "WecomOutboundOutbox",
    ]
    # request 落库那一刻 served_attempt_id 必须还是 NULL（循环外键）。
    assert db.add_snapshots[0][1] is None
    request, attempt = db.added[0], db.added[1]
    assert request.served_attempt_id == attempt.attempt_id
    assert attempt.request_id == request.request_id


def test_aibot_recommendation_outbox_inherits_inbound_conversation_contract(content_keys):
    db = _FakeSession()
    created = datetime(2026, 9, 3, 1, 2, 3)
    db.rows[(WecomInboundEvent.__name__, 1)] = SimpleNamespace(
        source_channel="wecom_aibot",
        conversation_type="single",
        conversation_id="u1",
        chat_id=None,
        ordering_key="wecom:wecom_aibot:single:u1",
        provider_req_id="req-aibot-1",
        created_at=created,
    )

    _prepare(db, content_keys)

    outbox = db.added[-1]
    assert isinstance(outbox, svc.WecomOutboundOutbox)
    assert outbox.channel == "wecom_aibot"
    assert outbox.conversation_type == "single"
    assert outbox.conversation_id == "u1"
    assert outbox.chat_id is None
    assert outbox.ordering_key == "wecom:wecom_aibot:single:u1"
    assert outbox.provider_req_id == "req-aibot-1"
    assert outbox.reply_command == "aibot_respond_msg"
    assert outbox.stream_id
    assert outbox.finish is True
    assert outbox.reply_expires_at == created + timedelta(hours=24)


def test_attempt_uses_legal_enums_and_a_real_64_char_digest(content_keys):
    db = _FakeSession()
    scoring_time = datetime(2026, 7, 26, 1, 2, 3, tzinfo=timezone.utc)
    _prepare(db, content_keys, fact=_fact(
        scoring_time_utc=scoring_time.isoformat(),
        llm_status="ok",
        llm_retry_count=1,
        attempt_latency_ms=42,
    ))
    attempt = db.added[1]
    assert attempt.attempt_kind == "initial"
    assert attempt.llm_status == "ok"
    assert len(attempt.criteria_digest) == 64
    assert attempt.criteria_digest != "digest-1"
    assert attempt.scoring_time_utc == scoring_time.replace(tzinfo=None)
    assert attempt.llm_retry_count == 1
    assert attempt.total_latency_ms == 42
    assert attempt.precision_pool_ids == ["11"]


def test_every_real_relax_query_is_persisted_as_an_attempt(content_keys):
    db = _FakeSession()
    _prepare(db, content_keys, fact=_fact(
        request_kind="auto_relaxed",
        attempt_kind="auto_relaxed",
        attempt_no=2,
        additional_attempts=[
            {
                "attempt_no": 0,
                "attempt_kind": "initial",
                "candidate_ids": [],
                "candidate_count": 0,
                "result_count": 0,
                "is_zero_result": True,
            },
            {
                "attempt_no": 1,
                "attempt_kind": "relax_probe",
                "candidate_ids": ["99"],
                "candidate_count": 1,
                "result_count": 1,
                "is_zero_result": False,
            },
        ],
    ))

    attempts = [
        row for row in db.added if type(row).__name__ == "RecommendationSearchAttempt"
    ]
    assert [(row.attempt_no, row.attempt_kind) for row in attempts] == [
        (2, "auto_relaxed"),
        (0, "initial"),
        (1, "relax_probe"),
    ]
    request = db.added[0]
    assert request.served_attempt_id == attempts[0].attempt_id


def test_illegal_enum_values_fall_back_instead_of_being_written(content_keys):
    db = _FakeSession()
    _prepare(db, content_keys, fact=_fact(llm_status="completed", attempt_kind="show_more"))
    attempt = db.added[1]
    assert attempt.llm_status == "skipped"
    assert attempt.attempt_kind == "initial"


def test_show_more_reuses_the_parent_served_attempt(content_keys):
    from app.models import RecommendationRequest

    db = _FakeSession()
    parent = RecommendationRequest(request_id="r-0", served_attempt_id="a-0")
    db.rows[("RecommendationRequest", "r-0")] = parent
    _prepare(db, content_keys, fact=_fact(
        request_kind="show_more", parent_request_id="r-0", show_more_exhausted=True,
    ))
    request = db.added[0]
    assert not any(
        name == "RecommendationSearchAttempt" for name, _ in db.add_snapshots
    )
    assert request.served_attempt_id == "a-0"
    assert request.parent_request_id == "r-0"
    # §9.4：show_more 分页耗尽不是业务零结果。
    assert request.is_zero_result is False
    assert request.show_more_exhausted is True


def test_dangling_parent_request_id_is_not_persisted(content_keys):
    db = _FakeSession()
    _prepare(db, content_keys, fact=_fact(
        request_kind="confirmed_relaxed", parent_request_id="r-missing",
    ))
    assert db.added[0].parent_request_id is None
    assert db.added[1].attempt_kind == "confirmed_relaxed"


def test_zero_result_request_is_still_persisted(content_keys):
    db = _FakeSession()
    empty = RecommendationDeliveryContext(
        delivery_id="d-1", request_id="r-1", viewer_userid="u1",
        direction="search_job", assignment="legacy",
        algorithm_version="legacy", query_digest="digest-1", items=[],
    )
    _prepare(db, content_keys, ctx=empty, fact=_fact(
        served_assignment="legacy", execution_mode="off", algorithm_version="legacy",
        candidate_count=0, candidate_ids=[], precision_pool_ids=[], served_top_ids=[],
        served_strategy_version_id=None, is_zero_result=True,
    ))
    request = db.added[0]
    assert request.is_zero_result is True
    assert request.served_top_ids == []
    assert request.execution_mode == "off"
    assert request.served_assignment == "legacy"
    assert db.added[-2].impression_expected_count == 0


def test_request_index_falls_back_to_reply_index(content_keys):
    db = _FakeSession()
    _prepare(db, content_keys, reply_index=2)
    assert db.added[0].request_index == 2
    db = _FakeSession()
    _prepare(db, content_keys, reply_index=2, fact=_fact(request_index=5))
    assert db.added[0].request_index == 5


def test_delivery_row_carries_hash_key_version_and_expected_count(content_keys):
    db = _FakeSession()
    delivery = _prepare(db, content_keys)
    assert delivery.content_key_version == svc.active_content_key_version()
    assert delivery.content_hash == svc.content_digest(
        "推荐正文，含电话 13800000000", delivery_id="d-1",
    )
    assert delivery.impression_expected_count == 1
    assert delivery.status == "prepared"
    assert delivery.session_commit_token == "d-1"
    assert svc.decrypt_delivery_body(delivery) == "推荐正文，含电话 13800000000"
    assert set(delivery.recommendation_context) == set(DELIVERY_CONTEXT_KEYS)
    assert "viewer_userid" not in delivery.recommendation_context


def test_empty_snapshot_id_is_stored_as_null(content_keys):
    db = _FakeSession()
    no_snapshot = RecommendationDeliveryContext(
        delivery_id="d-1", request_id="r-1", snapshot_id="", viewer_userid="u1",
        direction="search_job", assignment="candidate",
        algorithm_version="recommendation-v1", query_digest="digest-1", items=[],
    )
    delivery = _prepare(
        db, content_keys, ctx=no_snapshot, fact=_fact(snapshot_id=""), snapshot_id="",
    )
    assert delivery.snapshot_id is None
    assert db.added[0].snapshot_id is None
