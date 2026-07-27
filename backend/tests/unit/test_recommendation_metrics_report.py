"""§11.9 报表指标 + §9.8 日曝光聚合的固定向量测试。

用内存 SQLite 建真表跑真 SQL。模型用了一批 MySQL 专有类型（TINYINT /
MEDIUMBLOB / BIGINT UNSIGNED 自增），这里为 sqlite 方言注册等价渲染；
索引不建（SQLite 的索引名是库级唯一，几张表撞名），指标查询不依赖索引存在。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateTable

from app.api.admin import recommendation_metrics as metrics_api
from app.core.exceptions import BusinessException
from app.db import Base
from app.models import (
    ConversationLog,
    EventLog,
    RecommendationDelivery,
    RecommendationExposureDaily,
    RecommendationImpression,
    RecommendationRequest,
    RecommendationSearchAttempt,
    User,
)
from app.services import recommendation_metrics_service as metrics_service
from app.services import report_service
from app.tasks import recommendation_exposure_reconcile as reconcile

_SQLITE_TYPE_MAP = (
    (mysql.TINYINT, "SMALLINT"),
    (mysql.SMALLINT, "SMALLINT"),
    (mysql.INTEGER, "INTEGER"),
    # BIGINT UNSIGNED 主键在 SQLite 上不会自增，必须落到 INTEGER 才拿得到 rowid。
    (mysql.BIGINT, "INTEGER"),
    (mysql.MEDIUMBLOB, "BLOB"),
    (mysql.MEDIUMTEXT, "TEXT"),
    (mysql.LONGTEXT, "TEXT"),
    (mysql.DOUBLE, "FLOAT"),
)

for _type, _rendered in _SQLITE_TYPE_MAP:
    compiles(_type, "sqlite")(lambda type_, compiler, _r=_rendered, **kw: _r)

_TABLES = (
    "user",
    "conversation_log",
    "event_log",
    "recommendation_request",
    "recommendation_search_attempt",
    "recommendation_delivery",
    "recommendation_impression",
    "recommendation_exposure_daily",
)

NOW = datetime(2026, 3, 10, 12, 0, tzinfo=timezone.utc)
EXPOSED_AT = datetime(2026, 3, 9, 5, 0)
UTC = timezone.utc


@pytest.fixture()
def db():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as conn:
        for name in _TABLES:
            conn.execute(CreateTable(Base.metadata.tables[name]))
    session = sessionmaker(bind=engine)()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# 固定向量数据集
# ---------------------------------------------------------------------------

def _request(**kwargs) -> RecommendationRequest:
    defaults = dict(
        source_inbound_msg_id=kwargs["request_id"],
        request_index=0,
        request_kind="initial_search",
        direction="search_job",
        query_digest="q1",
        execution_mode="on",
        served_assignment="candidate",
        algorithm_version="recommendation-v1",
        final_candidate_count=0,
        result_count=0,
        is_zero_result=0,
        show_more_exhausted=0,
        total_latency_ms=100,
        served_top_ids=[],
        served_owner_count=0,
        served_max_owner_items=0,
        served_exploration_count=0,
    )
    defaults.update(kwargs)
    return RecommendationRequest(**defaults)


def _attempt(**kwargs) -> RecommendationSearchAttempt:
    defaults = dict(
        attempt_no=0,
        attempt_kind="initial_search",
        criteria_digest="c",
        scoring_time_utc=datetime(2026, 3, 9, 1, 0),
        candidate_count=0,
        precision_pool_ids=[],
        result_count=0,
        is_zero_result=0,
        algorithm_version="recommendation-v1",
        llm_status="ok",
        ranking_latency_ms=10,
        total_latency_ms=20,
        created_at=datetime(2026, 3, 9, 1, 0),
    )
    defaults.update(kwargs)
    return RecommendationSearchAttempt(**defaults)


def _impression(delivery_id: str, target_id: int, **kwargs) -> RecommendationImpression:
    defaults = dict(
        request_id="r1",
        snapshot_id="s1",
        viewer_userid="u1",
        direction="search_job",
        target_type="job",
        position=1,
        strategy_version_id=7,
        algorithm_version="recommendation-v1",
        assignment="candidate",
        is_exploration=0,
        query_digest="q1",
        exposed_at=EXPOSED_AT,
    )
    defaults.update(kwargs)
    return RecommendationImpression(delivery_id=delivery_id, target_id=target_id, **defaults)


_DELIVERY_ORDER = iter(range(1, 10_000))


def _delivery(delivery_id: str, **kwargs) -> RecommendationDelivery:
    defaults = dict(
        # MySQL 用 AUTO_INCREMENT 生成，SQLite 只给 INTEGER PRIMARY KEY 自增，
        # 所以测试里显式给序号。
        delivery_order=next(_DELIVERY_ORDER),
        source_inbound_msg_id=delivery_id,
        reply_index=0,
        request_id="r1",
        userid="u1",
        recommendation_context={},
        session_commit_token=delivery_id,
        status="sent",
        impression_state="completed",
        next_attempt_at=EXPOSED_AT,
        impression_next_attempt_at=EXPOSED_AT,
        created_at=datetime(2026, 3, 9, 4, 0),
        updated_at=datetime(2026, 3, 9, 4, 0),
    )
    defaults.update(kwargs)
    return RecommendationDelivery(delivery_id=delivery_id, **defaults)


def _click(delivery_id: str, target_id: int, status: str, **kwargs) -> EventLog:
    defaults = dict(
        event_type="miniprogram_click",
        userid="u1",
        target_type="job",
        occurred_at=datetime(2026, 3, 9, 6, 0),
        attribution_status=status,
    )
    defaults.update(kwargs)
    return EventLog(delivery_id=delivery_id, target_id=target_id, **defaults)


@pytest.fixture()
def seeded(db):
    """一份可手算的固定数据集，所有断言都对着注释里的算式。"""
    db.add_all([
        # 同一 viewer + 同 query_digest 的三次请求：r1→r2 Top3 完全重复，r2→r3 不重复
        _request(request_id="r1", served_attempt_id="a1", created_at=datetime(2026, 3, 9, 1, 0),
                 viewer_userid="u1", served_top_ids=["1", "2", "3"], result_count=3,
                 served_max_owner_items=3, total_latency_ms=100),
        _request(request_id="r2", served_attempt_id="a2", created_at=datetime(2026, 3, 9, 2, 0),
                 viewer_userid="u1", served_top_ids=["1", "2", "3"], result_count=3,
                 served_max_owner_items=1, total_latency_ms=200),
        _request(request_id="r3", served_attempt_id="a3", created_at=datetime(2026, 3, 9, 3, 0),
                 viewer_userid="u1", served_top_ids=["1", "2", "4"], result_count=3,
                 served_max_owner_items=1, total_latency_ms=300,
                 shadow_status="skipped_capacity", shadow_fallback="global_capacity",
                 shadow_queue_wait_ms=15, shadow_latency_ms=0),
        # 业务零结果（legacy 流量）
        _request(request_id="r4", served_attempt_id="a4", created_at=datetime(2026, 3, 9, 3, 30),
                 viewer_userid="u2", query_digest="q2", is_zero_result=1,
                 served_assignment="legacy", execution_mode="on",
                 shadow_status="timeout", shadow_fallback="deadline",
                 shadow_queue_wait_ms=5, shadow_latency_ms=3000),
        # show_more 耗尽：复用 r1 的 served attempt，且不计入业务零结果率
        _request(request_id="r5", served_attempt_id="a1", created_at=datetime(2026, 3, 9, 3, 40),
                 viewer_userid="u1", request_kind="show_more", is_zero_result=1,
                 show_more_exhausted=1, shadow_status="skipped_capacity",
                 shadow_fallback="local_capacity", shadow_queue_wait_ms=20,
                 shadow_latency_ms=0),
        # 上游若把 shadow attempt 写成 served_attempt_id，召回池必须仍然排除它
        _request(request_id="r6", served_attempt_id="a6", created_at=datetime(2026, 3, 9, 3, 50),
                 viewer_userid="u3", query_digest="q3", is_zero_result=1,
                 served_top_ids=["777", "778", "779"],
                 shadow_top_ids=["777", "780", "778"], shadow_overlap_count=2,
                 shadow_rank_delta={"777": 0, "778": 1}, shadow_status="completed",
                 shadow_queue_wait_ms=10, shadow_latency_ms=30,
                 shadow_input_tokens=30, shadow_output_tokens=6),
    ])
    db.add_all([
        _attempt(attempt_id="a1", request_id="r1", candidate_ids=["1", "2", "3", "4", "5"],
                 candidate_count=5, result_count=3),
        _attempt(attempt_id="a2", request_id="r2", candidate_ids=["1", "2", "3"],
                 candidate_count=3, result_count=3, ranking_fallback="llm_timeout"),
        _attempt(attempt_id="a3", request_id="r3", candidate_ids=["1", "2", "4"],
                 candidate_count=3, result_count=3, llm_input_tokens=100, llm_output_tokens=20),
        _attempt(attempt_id="a4", request_id="r4", candidate_ids=[], is_zero_result=1),
        _attempt(attempt_id="a5", request_id="r1", attempt_no=1, attempt_kind="relax_probe",
                 candidate_ids=["900"]),
        _attempt(attempt_id="a6", request_id="r6", attempt_kind="shadow_candidate",
                 candidate_ids=["777"], llm_input_tokens=30, llm_output_tokens=6),
    ])
    db.add_all([
        _delivery("d1", sent_at=datetime(2026, 3, 9, 5, 0),
                  impression_derived_at=datetime(2026, 3, 9, 5, 0, 0, 400_000)),
        _delivery("d2", impression_state="pending"),
        _delivery("d3", sent_at=datetime(2026, 3, 9, 5, 0),
                  impression_derived_at=datetime(2026, 3, 9, 5, 0, 0, 600_000)),
        _delivery("d4", status="unknown", impression_state="pending"),
    ])
    db.add_all([
        _impression("d1", 1), _impression("d1", 2, position=2), _impression("d1", 3, position=3),
        _impression("d2", 1, viewer_userid="u2"), _impression("d2", 2, viewer_userid="u2", position=2),
        _impression("d3", 1, is_exploration=1, strategy_version_id=8, position=3),
    ])
    db.add_all([
        _click("d1", 1, "attributed"),
        # 同一 (delivery, target) 的重复点击必须只算一次
        _click("d1", 1, "attributed", client_event_id="dup"),
        _click("d2", 2, "legacy_unattributed"),
        _click("d3", 1, "attributed"),
    ])
    db.commit()
    return db


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------

class TestPureHelpers:
    def test_gini_uniform_is_zero(self):
        assert metrics_service.gini([2, 2, 2, 2]) == 0.0

    def test_gini_counts_zero_exposure_candidates(self):
        # 补 0 与不补 0 是两个不同指标，数值必须不同
        assert metrics_service.gini([1, 2, 3]) == pytest.approx(0.222222, abs=1e-6)
        assert metrics_service.gini([0, 0, 1, 2, 3]) == pytest.approx(0.533333, abs=1e-6)

    def test_gini_empty_or_all_zero(self):
        assert metrics_service.gini([]) == 0.0
        assert metrics_service.gini([0, 0]) == 0.0

    def test_percentile_none_for_empty_sample(self):
        assert metrics_service.percentile([], 0.95) is None
        assert metrics_service.percentile([400.0, 600.0], 0.5) == pytest.approx(500.0)

    def test_target_type_mapping_matches_writer(self):
        assert metrics_service.target_type_for_direction("search_job") == "job"
        assert metrics_service.target_type_for_direction("search_worker") == "resume"

    def test_window_is_rolling_utc_hours_not_calendar_day(self):
        start, end = metrics_service.resolve_window(7, NOW)
        assert end == datetime(2026, 3, 10, 12, 0)
        assert start == datetime(2026, 3, 3, 12, 0)
        assert start.tzinfo is None and end.tzinfo is None


# ---------------------------------------------------------------------------
# 请求级指标
# ---------------------------------------------------------------------------

class TestRequestMetrics:
    def test_zero_result_rate_excludes_show_more_exhausted(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).requests
        assert result.total == 6
        assert result.show_more_exhausted == 1
        assert result.zero_result == 3          # r4 / r5 / r6
        assert result.business_zero_result == 2  # r5 是 show_more 耗尽，不算业务零结果
        assert result.zero_result_rate == pytest.approx(2 / 5)

    def test_assignment_and_mode_share(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).requests
        assert result.assignment_counts == {"candidate": 5, "legacy": 1, "stable": 0}
        assert result.assignment_share["legacy"] == pytest.approx(1 / 6, abs=1e-6)
        assert result.execution_mode_share["on"] == pytest.approx(1.0)

    def test_top3_repeat_rate_pairs_consecutive_same_condition_requests(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).requests
        assert result.top3_repeat_pairs == 2   # r1→r2、r2→r3
        assert result.top3_repeat_rate == pytest.approx(0.5)

    def test_top3_single_owner_rate(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).requests
        assert result.top3_single_owner_requests == 1
        assert result.top3_single_owner_rate == pytest.approx(1 / 3)

    def test_reranker_fallback_rate(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).attempts
        assert result.total == 5
        assert result.ranking_attempts == 4
        assert result.fallback_by_reason == {"llm_timeout": 1}
        assert result.reranker_fallback_rate == pytest.approx(1 / 4, abs=1e-6)

    def test_requests_outside_window_are_excluded(self, seeded):
        seeded.add(_request(request_id="old", created_at=datetime(2026, 2, 1, 0, 0),
                            viewer_userid="u9"))
        seeded.commit()
        assert metrics_service.collect_metrics(seeded, days=7, now=NOW).requests.total == 6


# ---------------------------------------------------------------------------
# 曝光与召回池集中度
# ---------------------------------------------------------------------------

class TestExposureMetrics:
    def test_exposure_totals(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).exposure
        assert result.impressions == 6
        assert result.exposed_users == 2
        assert result.exposed_candidates == 3
        assert result.exploration_impressions == 1
        assert result.exploration_share == pytest.approx(1 / 6, abs=1e-6)

    def test_exposed_candidate_gini_is_a_different_metric(self, seeded):
        report = metrics_service.collect_metrics(seeded, days=7, now=NOW)
        # 已曝光候选口径：[3, 2, 1]
        assert report.exposure.exposed_candidate_gini == pytest.approx(0.222222, abs=1e-6)
        # 全召回池口径：[3, 2, 1, 0, 0]
        assert report.recall_pool.gini == pytest.approx(0.533333, abs=1e-6)
        assert report.exposure.exposed_candidate_gini != report.recall_pool.gini

    def test_recall_pool_includes_zero_exposure_candidates(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).recall_pool
        # a1(1..5) ∪ a2(1..3) ∪ a3(1,2,4) = {1,2,3,4,5}
        assert result.pool_candidates == 5
        assert result.exposed_candidates == 3
        assert result.coverage == pytest.approx(0.6)

    def test_recall_pool_dedupes_show_more_and_drops_probe_and_shadow(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).recall_pool
        # r1 与 r5 共用 a1，DISTINCT 后只有 a1/a2/a3/a4/a6 五条
        assert result.attempts == 5
        # a5 是 relax_probe（没被 served_attempt_id 引用），a6 是 shadow_candidate
        assert result.pool_candidates == 5
        assert 900 not in {cid for _, cid in _pool_ids(seeded)}
        assert 777 not in {cid for _, cid in _pool_ids(seeded)}


def _pool_ids(db):
    """把召回池成员抽出来，方便断言 probe/shadow 候选没混进分母。"""
    start, end = metrics_service.resolve_window(7, NOW)
    counts = metrics_service._impression_counts_by_target(db, None, start, end)
    section = metrics_service._recall_pool_section(db, None, start, end, counts)
    assert section.pool_candidates == 5
    rows = db.query(RecommendationSearchAttempt.candidate_ids).filter(
        RecommendationSearchAttempt.attempt_id.in_(["a1", "a2", "a3", "a4"]),
    ).all()
    return {("job", int(cid)) for (ids,) in rows for cid in (ids or [])}


# ---------------------------------------------------------------------------
# CTR / 归因
# ---------------------------------------------------------------------------

class TestClickMetrics:
    def test_ctr_uses_attributed_clicks_deduped_per_delivery_target(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).clicks
        # d1/job/1 的两条 attributed 点击去重成 1，d2/job/2 的 legacy 点击不计入
        assert result.attributed_impression_clicks == 2
        assert result.ctr == pytest.approx(2 / 6)

    def test_exploration_ctr_split(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).clicks
        assert result.exploration_clicks == 1
        assert result.exploration_ctr == pytest.approx(1.0)
        assert result.non_exploration_ctr == pytest.approx(1 / 5)

    def test_ctr_by_strategy_version(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).clicks
        by_version = {point.strategy_version_id: point for point in result.by_strategy_version}
        assert by_version[7].impressions == 5 and by_version[7].clicks == 1
        assert by_version[7].ctr == pytest.approx(0.2)
        assert by_version[8].impressions == 1 and by_version[8].ctr == pytest.approx(1.0)

    def test_click_without_impression_fact_is_not_counted(self, seeded):
        # delivery 尚未派生曝光时点击不能先于分母被计入（§9.9）
        seeded.add(_click("d4", 42, "attributed"))
        seeded.commit()
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).clicks
        assert result.attributed_impression_clicks == 2
        assert result.attribution_counts["attributed"] == 4

    def test_attribution_share(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).clicks
        assert result.attribution_counts == {
            "attributed": 3, "legacy_unattributed": 1, "rejected": 0,
        }
        assert result.attributed_click_rate == pytest.approx(0.75)
        assert result.attribution_direction_scoped is False


# ---------------------------------------------------------------------------
# 投递可靠性
# ---------------------------------------------------------------------------

class TestDeliveryMetrics:
    def test_unknown_rate_and_status_share(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).delivery
        assert result.total == 4
        assert result.status_counts["sent"] == 3
        assert result.status_counts["unknown"] == 1
        assert result.unknown_rate == pytest.approx(0.25)

    def test_impression_backlog_rate_uses_impression_state(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).delivery
        assert result.impression_state_counts["completed"] == 2
        assert result.impression_state_counts["pending"] == 1
        assert result.impression_backlog_rate == pytest.approx(1 / 3)

    def test_sent_to_impression_latency(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).delivery
        assert result.sent_to_impression_samples == 2
        assert result.sent_to_impression_p99_ms == pytest.approx(598.0, abs=5.0)

    def test_missing_data_sources_report_none_not_zero(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).delivery
        assert result.prepared_session_conflicts is None
        assert result.dispatcher_claim_latency_p95_ms is None


class TestShadowMetrics:
    def test_shadow_section_aggregates_persisted_facts(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).shadow
        assert result.available is True
        assert result.requests == 4
        assert result.top_n_overlap_rate == pytest.approx(2 / 3)
        assert result.average_position_delta == pytest.approx(0.5)
        assert result.timeout_count == 1
        assert result.local_capacity_skip_count == 1
        assert result.global_capacity_skip_count == 1
        assert result.persistence_drop_count is None
        assert result.queue_wait_p95_ms == pytest.approx(19.25)
        assert result.duration_p95_ms == pytest.approx(2560.25)
        assert any("shadow_persistence_dropped" in source for source in result.missing_sources)

    def test_llm_cost_separates_serving_and_shadow_tokens(self, seeded):
        result = metrics_service.collect_metrics(seeded, days=7, now=NOW).llm
        assert result.legacy_input_tokens == 100
        assert result.legacy_output_tokens == 20
        assert result.shadow_input_tokens == 30
        assert result.shadow_output_tokens == 6
        assert result.by_direction["search_job"]["legacy_attempts"] == 5
        assert result.by_direction["search_job"]["shadow_attempts"] == 1


class TestDirectionFilter:
    def test_direction_filter_isolates_other_direction(self, seeded):
        worker = metrics_service.collect_metrics(seeded, direction="search_worker", days=7, now=NOW)
        assert worker.requests.total == 0
        assert worker.exposure.impressions == 0
        assert worker.recall_pool.pool_candidates == 0
        job = metrics_service.collect_metrics(seeded, direction="search_job", days=7, now=NOW)
        assert job.requests.total == 6
        assert job.exposure.impressions == 6

    def test_response_never_exposes_viewer_userid(self, seeded):
        payload = metrics_service.collect_metrics(seeded, days=7, now=NOW).model_dump_json()
        assert "u1" not in payload and "viewer_userid" not in payload


# ---------------------------------------------------------------------------
# §9.8 日曝光聚合
# ---------------------------------------------------------------------------

class TestExposureDailyReconcile:
    def test_stat_date_uses_asia_shanghai_business_day(self, db):
        # 15:59:59Z = 北京 23:59:59 当天；16:00:00Z = 北京次日 00:00
        db.add(_impression("d1", 1, exposed_at=datetime(2026, 1, 1, 15, 59, 59)))
        db.add(_impression("d1", 2, exposed_at=datetime(2026, 1, 1, 16, 0, 0)))
        db.commit()

        first = reconcile.reconcile_day(db, date(2026, 1, 1), now=NOW)
        second = reconcile.reconcile_day(db, date(2026, 1, 2), now=NOW)
        assert first["rows"] == 1 and first["impressions"] == 1
        assert second["rows"] == 1 and second["impressions"] == 1

        rows = {
            (row.stat_date, row.target_id): row.impression_count
            for row in db.query(RecommendationExposureDaily).all()
        }
        assert rows == {(date(2026, 1, 1), 1): 1, (date(2026, 1, 2), 2): 1}

    def test_aggregation_counts_and_is_idempotent(self, db):
        # 同一候选被三次不同投递曝光（唯一键是 delivery+target，不能复用 delivery）
        for index in range(3):
            db.add(_impression(f"d1{index}", 1, exposed_at=datetime(2026, 1, 1, 2, index)))
        db.add(_impression("d2", 2, exposed_at=datetime(2026, 1, 1, 3, 0)))
        db.commit()

        reconcile.reconcile_day(db, date(2026, 1, 1), now=NOW)
        reconcile.reconcile_day(db, date(2026, 1, 1), now=NOW + timedelta(minutes=1))

        rows = {row.target_id: row.impression_count for row in db.query(RecommendationExposureDaily).all()}
        assert rows == {1: 3, 2: 1}

    def test_recompute_drops_rows_whose_facts_disappeared(self, db):
        db.add(_impression("d1", 1, exposed_at=datetime(2026, 1, 1, 2, 0)))
        db.add(_impression("d2", 2, exposed_at=datetime(2026, 1, 1, 3, 0)))
        db.commit()
        reconcile.reconcile_day(db, date(2026, 1, 1), now=NOW)
        assert db.query(RecommendationExposureDaily).count() == 2

        # 事实表是唯一真源：明细被 TTL 清掉后聚合行必须一起消失
        db.query(RecommendationImpression).filter(
            RecommendationImpression.target_id == 2,
        ).delete(synchronize_session=False)
        db.commit()
        result = reconcile.reconcile_day(db, date(2026, 1, 1), now=NOW + timedelta(minutes=1))

        assert result["purged"] == 1
        rows = {row.target_id: row.impression_count for row in db.query(RecommendationExposureDaily).all()}
        assert rows == {1: 1}

    def test_batching_covers_more_rows_than_one_batch(self, db, monkeypatch):
        monkeypatch.setattr(reconcile, "BATCH_SIZE", 2)
        for target_id in range(1, 6):
            db.add(_impression("d1", target_id, position=target_id,
                               exposed_at=datetime(2026, 1, 1, 2, 0)))
        db.commit()
        result = reconcile.reconcile_day(db, date(2026, 1, 1), now=NOW)
        assert result["rows"] == 5
        assert result["batches"] == 3
        assert db.query(RecommendationExposureDaily).count() == 5

    def test_recent_business_days_is_ascending_and_capped(self):
        days = reconcile.recent_business_days(2, now=datetime(2026, 1, 1, 16, 0, tzinfo=UTC))
        # 16:00Z = 北京 1/2 00:00，所以"今天"是 1/2，回补到 1/1
        assert days == [date(2026, 1, 1), date(2026, 1, 2)]
        assert len(reconcile.recent_business_days(999, now=NOW)) == reconcile.MAX_LOOKBACK_DAYS

    def test_rebuild_range_rejects_inverted_and_oversized_range(self):
        with pytest.raises(ValueError):
            reconcile.rebuild_range(date(2026, 1, 2), date(2026, 1, 1))
        with pytest.raises(ValueError):
            reconcile.rebuild_range(date(2026, 1, 1), date(2026, 12, 31))

    def test_exposure_daily_report_reads_the_aggregate_table(self, db):
        db.add(_impression("d1", 1, exposed_at=datetime(2026, 1, 1, 2, 0)))
        db.add(_impression("d2", 1, exposed_at=datetime(2026, 1, 1, 3, 0)))
        db.add(_impression("d3", 2, exposed_at=datetime(2026, 1, 1, 4, 0)))
        db.commit()
        reconcile.reconcile_day(db, date(2026, 1, 1), now=NOW)

        report = metrics_service.get_exposure_daily(
            db, start=date(2026, 1, 1), end=date(2026, 1, 2),
        )
        assert len(report.points) == 1
        point = report.points[0]
        assert point.stat_date == "2026-01-01"
        assert point.target_type == "job"
        assert point.candidates == 2
        assert point.impressions == 3
        assert point.max_candidate_impressions == 2


# ---------------------------------------------------------------------------
# §11.9 漏斗改用曝光事实
# ---------------------------------------------------------------------------

class TestFunnel:
    def test_received_recommendation_stage_uses_impression_facts(self, db):
        recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
        db.add(User(external_userid="u1", role="worker", registered_at=recent))
        db.add(ConversationLog(userid="u1", direction="in", msg_type="text", content="找工作",
                               intent="search_job", created_at=recent,
                               expires_at=recent + timedelta(days=30)))
        # 系统生成了两条回复日志，但只有一条真正发出去并派生了曝光
        for index in range(2):
            db.add(ConversationLog(
                userid=f"u{index + 1}", direction="out", msg_type="text", content="推荐",
                intent="search_job", criteria_snapshot={"recommend_count": 3},
                created_at=recent, expires_at=recent + timedelta(days=30),
            ))
        db.add(_impression("d1", 1, viewer_userid="u1", exposed_at=recent))
        db.commit()

        stages = {row["stage"]: row["count"] for row in report_service.get_funnel(db)}
        assert stages["收到推荐"] == 1

    def test_failed_delivery_does_not_count_as_received(self, db):
        recent = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=1)
        db.add(ConversationLog(userid="u1", direction="out", msg_type="text", content="推荐",
                               intent="search_job", criteria_snapshot={"recommend_count": 3},
                               created_at=recent, expires_at=recent + timedelta(days=30)))
        db.commit()
        stages = {row["stage"]: row["count"] for row in report_service.get_funnel(db)}
        assert stages["收到推荐"] == 0


class TestDayRange:
    def test_day_range_is_asia_shanghai_midnight_in_utc(self):
        start, end = report_service._day_range(date(2026, 3, 10))
        assert start == datetime(2026, 3, 9, 16, 0)
        assert end == datetime(2026, 3, 10, 16, 0)


# ---------------------------------------------------------------------------
# API 层：只做参数校验 + 序列化
# ---------------------------------------------------------------------------

class TestMetricsApi:
    def test_legacy_flat_keys_are_preserved_alongside_new_sections(self, seeded, monkeypatch):
        # 路由自己不带 now 参数（生产就该用真实时钟），测试里把窗口钉到数据集上
        original = metrics_service.collect_metrics
        monkeypatch.setattr(
            metrics_service, "collect_metrics",
            lambda db, **kwargs: original(db, **{**kwargs, "now": NOW}),
        )
        payload = metrics_api.metrics(direction=None, days=7, db=seeded, _=None)["data"]
        # 后台卡片读的老契约
        assert payload["requests"] == 6
        assert payload["impressions"] == 6
        assert payload["clicks"] == 2
        assert payload["ctr"] == pytest.approx(2 / 6 * 100, abs=1e-2)
        assert payload["unique_candidates"] == 3
        # 新分组
        assert payload["request_metrics"]["zero_result_rate"] == pytest.approx(0.4)
        assert payload["recall_pool"]["pool_candidates"] == 5
        assert payload["delivery"]["unknown_rate"] == pytest.approx(0.25)

    def test_invalid_direction_is_rejected(self, seeded):
        with pytest.raises(BusinessException):
            metrics_api.metrics(direction="search_alien", days=7, db=seeded, _=None)

    def test_exposure_daily_rejects_bad_params(self, db):
        with pytest.raises(BusinessException):
            metrics_api.exposure_daily(
                from_=None, to=None, target_type="worker", db=db, _=None,
            )
        with pytest.raises(BusinessException):
            metrics_api.exposure_daily(
                from_=date(2026, 3, 10), to=date(2026, 3, 1), target_type=None, db=db, _=None,
            )
        with pytest.raises(BusinessException):
            metrics_api.exposure_daily(
                from_=date(2025, 1, 1), to=date(2026, 3, 1), target_type=None, db=db, _=None,
            )
