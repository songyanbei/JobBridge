from app.schemas.recommendation import (
    RecommendationItem,
    RecommendationStrategyParameters,
    StrategyAssignment,
)
from app.schemas.search import SearchResult
from app.services.recommendation_scoring_service import stable_bucket, semantic_scores
from app.services.recommendation_delivery_service import decrypt_body, encrypt_body
from app.services.recommendation_strategy_service import select_assignment
from types import SimpleNamespace


def test_strategy_parameters_have_safe_defaults():
    params = RecommendationStrategyParameters()
    assert sum(getattr(params, k) for k in (
        "match_weight", "quality_weight", "freshness_weight", "exposure_weight"
    )) == 100


def test_stable_bucket_is_deterministic():
    assert stable_bucket("u1", "search_job", "q1", "v1", "2026-01-01") == stable_bucket(
        "u1", "search_job", "q1", "v1", "2026-01-01"
    )


def test_semantic_scores_missing_values_are_zero():
    scores = semantic_scores({"title": "python"}, {"title": "python engineer"})
    assert 0 <= scores["title"] <= 1
    assert scores.get("city", 0) == 0


def test_legacy_search_result_defaults():
    result = SearchResult(reply_text="ok")
    assert result.recommendation_items == []
    assert result.snapshot_id is None


def test_recommendation_body_is_encrypted():
    token = encrypt_body("recommendation secret")
    assert "recommendation secret" not in token
    assert decrypt_body(token) == "recommendation secret"


def test_shadow_never_changes_served_assignment():
    release = SimpleNamespace(
        execution_mode="shadow",
        candidate_version_id=42,
        stable_version_id=None,
        rollout_percentage=100,
    )
    assert select_assignment(
        release=release, userid="u1", direction="search_job",
    ) == ("legacy", None)


def test_search_request_identity_is_preserved_into_delivery_contract():
    from app.services.message_router import _recommendation_reply_fields
    result = SearchResult(
        reply_text="ok",
        recommendation_items=[
            RecommendationItem(
                target_type="job", target_id=7, position=1, final_score=0.9,
            ),
        ],
        snapshot_id="snapshot-1",
        request_id="request-1",
        query_digest="digest-1",
        candidate_ids=["7", "8"],
        precision_pool_ids=["7"],
        strategy_assignment=StrategyAssignment(
            direction="search_job",
            execution_mode="on",
            assignment="candidate",
            strategy_version_id=2,
            algorithm_version="recommendation-v1",
        ),
    )
    fields = _recommendation_reply_fields(result, "u1", "wx-msg-1")
    assert fields["recommendation_context"].request_id == "request-1"
    assert fields["recommendation_context"].query_digest == "digest-1"
    assert fields["recommendation_request"].source_inbound_msg_id == "wx-msg-1"


def test_recommendation_history_is_always_redacted():
    from app.schemas.conversation import ReplyMessage
    from app.services.message_router import _history_reply_content
    reply = ReplyMessage(
        userid="u1",
        content="sensitive recommendation body",
        recommendation_context={
            "delivery_id": "d1",
            "request_id": "r1",
            "viewer_userid": "u1",
            "direction": "search_job",
            "assignment": "candidate",
            "algorithm_version": "recommendation-v1",
            "query_digest": "q1",
            "items": [],
        },
    )
    assert _history_reply_content(reply) == "[recommendation_delivery]"
