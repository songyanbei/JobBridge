"""§9.4 请求事实覆盖面 + §10.1 虚假曝光回归（评审 P0-5 / P1-10 / P1-25 / P1-26）。

只覆盖 message_router 的纯函数部分，不需要 DB / Redis。
"""
from datetime import datetime, timezone

from app.schemas.conversation import ReplyMessage
from app.schemas.recommendation import RecommendationItem, StrategyAssignment
from app.schemas.search import SearchOutcome, SearchResult
from app.services.message_router import (
    _attach_recommendation_fields,
    _recommendation_reply_fields,
)
from app.services.search_service import _legacy_fallback_assignment


def _outcome(**overrides) -> SearchOutcome:
    base = dict(
        direction="search_job",
        criteria_used={"city": ["北京市"]},
        initial_count=0,
        final_count=0,
        desired_count=3,
        low_recall_threshold=3,
    )
    base.update(overrides)
    return SearchOutcome(**base)


def _v1_result(reply_text: str = "为您找到 3 个匹配岗位") -> SearchResult:
    return SearchResult(
        reply_text=reply_text,
        result_count=3,
        recommendation_items=[
            RecommendationItem(
                target_type="job", target_id=1, position=1,
                final_score=0.9, owner_userid="owner-a",
            ),
            RecommendationItem(
                target_type="job", target_id=2, position=2,
                final_score=0.8, owner_userid="owner-a",
            ),
            RecommendationItem(
                target_type="job", target_id=3, position=3,
                final_score=0.5, owner_userid="owner-b", is_exploration=True,
            ),
        ],
        snapshot_id="snap-1",
        request_id="req-1",
        query_digest="digest-1",
        candidate_ids=["1", "2", "3"],
        precision_pool_ids=["1", "2"],
        strategy_assignment=StrategyAssignment(
            direction="search_job",
            execution_mode="on",
            assignment="candidate",
            strategy_version_id=7,
            candidate_version_id=7,
            algorithm_version="recommendation-v1",
        ),
    )


# ---------------------------------------------------------------------------
# P0-5：请求事实不能只覆盖 "v1 on 且有结果"
# ---------------------------------------------------------------------------

def test_zero_result_search_still_writes_request_fact_without_delivery():
    result = SearchResult(reply_text="暂时没有匹配的岗位。", result_count=0)
    fields = _recommendation_reply_fields(
        result, "u1", "msg-1", search_outcome=_outcome(),
    )
    fact = fields["recommendation_request"]
    assert fact.is_zero_result is True
    assert fact.result_count == 0
    # 零结果没有候选，绝不能建 delivery（否则会派生出虚假曝光）。
    assert "delivery_id" not in fields
    assert "recommendation_context" not in fields


def test_legacy_search_writes_fact_with_legacy_assignment():
    """§7.5：off 只关闭新排序，不关闭可观测性。"""
    result = SearchResult(reply_text="为您找到 2 个匹配岗位", result_count=2)
    fact = _recommendation_reply_fields(
        result, "u1", "msg-1", search_outcome=_outcome(final_count=2),
    )["recommendation_request"]
    assert fact.served_assignment == "legacy"
    assert fact.algorithm_version == "legacy"
    assert fact.served_strategy_version_id is None
    assert fact.execution_mode == "off"
    # legacy 不产生 RecommendationItem，但它不是业务零结果。
    assert fact.is_zero_result is False
    assert fact.result_count == 2


def test_shadow_serves_legacy_but_keeps_candidate_version():
    result = SearchResult(
        reply_text="为您找到 2 个匹配岗位",
        result_count=2,
        strategy_assignment=StrategyAssignment(
            direction="search_job",
            execution_mode="shadow",
            assignment="legacy",
            candidate_version_id=9,
            algorithm_version="recommendation-v1",
        ),
    )
    fact = _recommendation_reply_fields(
        result, "u1", "msg-1", search_outcome=_outcome(final_count=2),
    )["recommendation_request"]
    assert fact.execution_mode == "shadow"
    assert fact.served_assignment == "legacy"
    assert fact.algorithm_version == "legacy"
    assert fact.candidate_strategy_version_id == 9


def test_v1_failure_keeps_real_execution_mode_in_legacy_fact():
    """v1 回退不是运维关闭，事实表必须保留当时的 on/shadow 模式。"""
    decision = type("Decision", (), {
        "assignment": StrategyAssignment(
            direction="search_job",
            execution_mode="on",
            assignment="candidate",
            strategy_version_id=9,
            candidate_version_id=9,
            algorithm_version="recommendation-v1",
        ),
    })()

    fallback = _legacy_fallback_assignment(decision)

    assert fallback is not None
    assert fallback.execution_mode == "on"
    assert fallback.assignment == "legacy"
    assert fallback.strategy_version_id is None
    assert fallback.candidate_version_id == 9
    assert fallback.algorithm_version == "legacy"


def test_reply_without_real_query_does_not_fabricate_a_request():
    """快照过期这类回复根本没查过库，不能污染零结果率。"""
    result = SearchResult(reply_text="搜索结果已过期，请重新搜索。")
    fields = _recommendation_reply_fields(
        result, "u1", "msg-1", request_kind="show_more",
        search_outcome=_outcome(criteria_used={}),
    )
    assert fields == {}


# ---------------------------------------------------------------------------
# P1-26：请求级聚合字段
# ---------------------------------------------------------------------------

def test_served_aggregates_come_from_items():
    fact = _recommendation_reply_fields(
        _v1_result(), "u1", "msg-1", search_outcome=_outcome(final_count=3),
    )["recommendation_request"]
    assert fact.served_owner_count == 2
    assert fact.served_max_owner_items == 2
    assert fact.served_exploration_count == 1
    assert fact.served_top_ids == ["1", "2", "3"]
    assert fact.snapshot_id == "snap-1"
    assert fact.result_count == 3


def test_show_more_exhausted_is_not_a_business_zero_result():
    result = SearchResult(reply_text="已经是所有匹配结果了。", result_count=0)
    fact = _recommendation_reply_fields(
        result, "u1", "msg-1", request_kind="show_more",
        search_outcome=_outcome(snapshot_exhausted=True),
    )["recommendation_request"]
    assert fact.show_more_exhausted is True
    assert fact.is_zero_result is False


def test_total_latency_is_recorded():
    fact = _recommendation_reply_fields(
        _v1_result(), "u1", "msg-1", search_outcome=_outcome(final_count=3),
        total_latency_ms=137,
    )["recommendation_request"]
    assert fact.total_latency_ms == 137


# ---------------------------------------------------------------------------
# P1-25：attempt kind 必须区分自动放宽和确认放宽
# ---------------------------------------------------------------------------

def test_auto_relaxed_and_confirmed_relaxed_are_distinct():
    result = SearchResult(reply_text="放宽后结果", result_count=1, request_id="req-1")
    auto = _recommendation_reply_fields(
        result, "u1", "msg-1", request_kind="auto_relaxed",
        parent_request_id="req-1", search_outcome=_outcome(final_count=1),
    )["recommendation_request"]
    confirmed = _recommendation_reply_fields(
        result, "u1", "msg-2", request_kind="confirmed_relaxed",
        parent_request_id="req-1", search_outcome=_outcome(final_count=1),
    )["recommendation_request"]

    assert auto.attempt_kind == "auto_relaxed"
    # §9.4 行 1839-1840：自动放宽仍然只有一条 request，沿用原 request_id、没有 parent。
    assert auto.request_id == "req-1"
    assert auto.parent_request_id is None

    assert confirmed.attempt_kind == "confirmed_relaxed"
    assert confirmed.request_id != "req-1"
    assert confirmed.parent_request_id == "req-1"


def test_relax_probe_steps_are_carried():
    result = SearchResult(reply_text="暂时没有匹配的岗位。", result_count=0)
    fact = _recommendation_reply_fields(
        result, "u1", "msg-1",
        search_outcome=_outcome(
            relax_probe_results=[{
                "step": "relax_salary_10pct",
                "count": 4,
                "candidate_count": 4,
                "candidate_ids": ["1", "2", "3", "4"],
            }],
        ),
    )["recommendation_request"]
    assert fact.relax_probe_steps == ["relax_salary_10pct"]
    assert len(fact.additional_attempts) == 1
    assert fact.additional_attempts[0]["attempt_kind"] == "relax_probe"
    assert fact.additional_attempts[0]["attempt_no"] == 1


def test_auto_relaxed_keeps_initial_and_probe_attempts_on_final_request():
    prior = SearchResult(
        reply_text="暂无结果",
        result_count=0,
        request_id="req-preliminary",
        query_digest="strict-digest",
        candidate_ids=[],
        llm_status="ok",
        ranking_latency_ms=12,
    )
    final = SearchResult(
        reply_text="放宽后结果",
        result_count=1,
        request_id="req-final",
        query_digest="relaxed-digest",
        candidate_ids=["7"],
    )
    fact = _recommendation_reply_fields(
        final,
        "u1",
        "msg-1",
        request_kind="auto_relaxed",
        parent_request_id="req-preliminary",
        search_outcome=_outcome(final_count=1),
        prior_search_result=prior,
        prior_search_outcome=_outcome(
            relax_probe_results=[{
                "step": "relax_salary_10pct",
                "candidate_count": 1,
                "candidate_ids": ["7"],
            }],
        ),
    )["recommendation_request"]

    assert fact.request_id == "req-final"
    assert fact.parent_request_id is None
    assert fact.attempt_kind == "auto_relaxed"
    assert fact.attempt_no == 2
    assert [
        (item["attempt_no"], item["attempt_kind"])
        for item in fact.additional_attempts
    ] == [(0, "initial"), (1, "relax_probe")]


# ---------------------------------------------------------------------------
# P1-10：不含候选的回复不得携带 delivery
# ---------------------------------------------------------------------------

def test_clarification_reply_gets_fact_but_never_a_delivery():
    result = _v1_result()
    fields = _recommendation_reply_fields(
        result, "u1", "msg-1", search_outcome=_outcome(final_count=3),
    )
    replies = _attach_recommendation_fields(
        [ReplyMessage(userid="u1", content="需要确认一下：要放宽城市吗？")],
        fields,
        result,
    )
    assert replies[0].recommendation_request is not None
    assert replies[0].recommendation_context is None
    assert replies[0].delivery_id is None


def test_only_the_reply_that_renders_candidates_gets_the_delivery():
    result = _v1_result()
    fields = _recommendation_reply_fields(
        result, "u1", "msg-1", search_outcome=_outcome(final_count=3),
    )
    replies = _attach_recommendation_fields(
        [
            ReplyMessage(userid="u1", content="已按您的偏好优先展示。"),
            ReplyMessage(userid="u1", content=result.reply_text),
        ],
        fields,
        result,
    )
    assert replies[0].recommendation_context is None
    assert replies[0].recommendation_request is None
    assert replies[1].recommendation_context is not None
    assert replies[1].delivery_id == replies[1].recommendation_context.delivery_id
    assert len(replies[1].recommendation_context.items) == 3


def test_legacy_results_create_delivery_items_for_actual_visible_candidates():
    from app.services.search_service import _served_recommendation_items

    visible = [
        {"id": 8, "owner_userid": "owner-8"},
        {"id": 3, "owner_userid": "owner-3"},
    ]
    items = _served_recommendation_items(visible, "search_job")
    result = SearchResult(
        reply_text="岗位 8\n岗位 3",
        result_count=2,
        recommendation_items=items,
    )
    fields = _recommendation_reply_fields(
        result,
        "u1",
        "msg-legacy",
        search_outcome=_outcome(final_count=2),
    )

    assert fields["recommendation_request"].served_assignment == "legacy"
    assert fields["recommendation_request"].served_top_ids == ["8", "3"]
    assert [item.target_id for item in fields["recommendation_context"].items] == [8, 3]
    assert all(item.reason_codes == ["legacy_baseline"] for item in items)


def test_served_items_drop_v1_candidates_removed_by_permission_filter():
    from app.services.search_service import _served_recommendation_items

    ranked = _v1_result().recommendation_items
    items = _served_recommendation_items(
        [{"id": 2, "owner_userid": "visible-owner"}],
        "search_job",
        ranked,
    )

    assert [item.target_id for item in items] == [2]
    assert items[0].position == 1
    assert items[0].owner_userid == "visible-owner"


def test_served_attempt_carries_real_llm_telemetry():
    scoring_time = datetime(2026, 7, 27, 2, 3, 4, tzinfo=timezone.utc)
    result = _v1_result()
    result.scoring_time_utc = scoring_time
    result.llm_status = "timeout"
    result.llm_input_tokens = 31
    result.llm_output_tokens = 7
    result.llm_retry_count = 1
    result.ranking_fallback = "provider_timeout"
    result.ranking_latency_ms = 432

    fact = _recommendation_reply_fields(
        result, "u1", "msg-telemetry", search_outcome=_outcome(final_count=3),
    )["recommendation_request"]

    assert fact.scoring_time_utc == scoring_time
    assert fact.llm_status == "timeout"
    assert fact.llm_input_tokens == 31
    assert fact.llm_output_tokens == 7
    assert fact.llm_retry_count == 1
    assert fact.ranking_fallback == "provider_timeout"
    assert fact.ranking_latency_ms == 432
    assert fact.attempt_latency_ms == 432


def test_applier_attached_fields_are_not_overwritten():
    """自动放宽二次检索已经挂过事实时，外层不得再覆盖成初次搜索的事实。"""
    result = _v1_result()
    relaxed_fields = _recommendation_reply_fields(
        result, "u1", "msg-1", request_kind="auto_relaxed",
        search_outcome=_outcome(final_count=3),
    )
    already = ReplyMessage(userid="u1", content=result.reply_text).model_copy(
        update=relaxed_fields,
    )
    initial_fields = _recommendation_reply_fields(
        SearchResult(reply_text="暂时没有匹配的岗位。", result_count=0),
        "u1", "msg-1", search_outcome=_outcome(),
    )
    replies = _attach_recommendation_fields([already], initial_fields, result)
    assert replies[0].recommendation_request.request_kind == "auto_relaxed"
