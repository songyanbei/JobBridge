from types import SimpleNamespace
from unittest.mock import patch

from scripts.conversation_replay_eval import (
    ReplayCase, _redact, _stratified, _unreplayable_reason, run,
)


def test_redact_removes_common_personal_identifiers():
    value = _redact(
        "电话13800138000，身份证110101199001011234，a@example.com https://x.test/a",
    )
    assert "13800138000" not in value
    assert "110101199001011234" not in value
    assert "a@example.com" not in value
    assert "https://" not in value


def test_stratified_sampler_preserves_minor_buckets():
    cases = [
        ReplayCase(str(i), "worker", "x", "search_job") for i in range(10)
    ] + [ReplayCase("factory", "factory", "x", "search_worker")]
    picked = _stratified(cases, 2)
    assert {case.role for case in picked} == {"worker", "factory"}


def test_run_reports_only_case_ids_and_aggregates():
    cases = [ReplayCase("safehash", "worker", "敏感原文", "search_job")]
    route = SimpleNamespace(
        intent_result=SimpleNamespace(intent="search_job"), source="v2_primary",
    )
    with patch("scripts.conversation_replay_eval.load_cases", return_value=cases), \
         patch("scripts.conversation_replay_eval.classify_dialogue", return_value=route):
        report = run(limit=10, repeat=2, source_label="unit")

    assert report["source"] == "unit"
    assert report["legacy_compatibility_agreement"] == 1.0
    assert report["semantic_family_agreement"] == 1.0
    assert report["stable_case_rate"] == 1.0
    assert report["error_count"] == 0
    assert "敏感原文" not in str(report)


def test_unreplayable_stateful_and_role_drift_cases_are_explicit():
    assert _unreplayable_reason(
        ReplayCase("1", "worker", "更多", "show_more"),
    ) == "missing_candidate_snapshot"
    assert _unreplayable_reason(
        ReplayCase("2", "factory", "找工作", "search_job"),
    ) == "role_label_conflict"


def test_curated_matrix_covers_roles_and_core_families():
    from app.evaluation.curated_conversation_cases import CURATED_CASES

    assert len(CURATED_CASES) >= 30
    assert {case["role"] for case in CURATED_CASES} == {"worker", "factory", "broker"}
    assert {case["expected_intent"] for case in CURATED_CASES} >= {
        "search_job", "search_worker", "upload_job", "upload_resume",
    }


def test_synthetic_matrix_has_600_unique_cross_role_cases():
    from app.evaluation.synthetic_intent_matrix import SYNTHETIC_INTENT_MATRIX

    assert len(SYNTHETIC_INTENT_MATRIX) == 600
    assert len({case["case_id"] for case in SYNTHETIC_INTENT_MATRIX}) == 600
    assert {case["role"] for case in SYNTHETIC_INTENT_MATRIX} == {
        "worker", "factory", "broker",
    }
    assert {case["expected_intent"] for case in SYNTHETIC_INTENT_MATRIX} == {
        "search_job", "search_worker", "upload_job", "upload_resume",
    }
