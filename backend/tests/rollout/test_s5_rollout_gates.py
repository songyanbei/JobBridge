from datetime import datetime, timedelta

from app.domains.recruitment.matching import MatchingPolicyV1, direction_for
from app.services.resume_rollout_service import rollout_enabled
from app.listing.render import redact_resume_for_viewer


def test_four_direction_matrix_is_explicit():
    assert direction_for("worker", "search_job") == "worker_to_job"
    assert direction_for("factory", "search_worker") == "factory_to_worker"
    assert direction_for("broker", "search_job") == "broker_to_job"
    assert direction_for("broker", "search_worker") == "broker_to_worker"


def test_rollout_kill_switch_and_allowlist_fail_closed():
    assert not rollout_enabled("u", percentage=100, direction="factory_to_worker", kill_switch=True)
    assert rollout_enabled("u", percentage=100, direction="factory_to_worker", allowlist=("factory_to_worker",), kill_switch=False)
    assert not rollout_enabled("u", percentage=100, direction="broker_to_job", allowlist=("factory_to_worker",), kill_switch=False)


def test_matching_policy_hard_filter_and_bounded_rank():
    now = datetime.utcnow()
    candidates = [
        {"id": 1, "audit_status": "passed", "expires_at": now + timedelta(days=1), "expected_cities": ["苏州"]},
        {"id": 2, "audit_status": "pending", "expires_at": now + timedelta(days=1), "expected_cities": ["苏州"]},
    ]
    assert [item["id"] for item in MatchingPolicyV1().rank(candidates, {"city": ["苏州"]})] == [1]


def test_resume_projection_has_no_contact_or_exact_age():
    class Resume:
        id = 3; age = 31; gender = "男"; expected_job_categories = ["普工"]; expected_cities = ["苏州"]
        salary_expect_floor_monthly = 5000; work_experience = "x"; phone = "13800000000"
    result = redact_resume_for_viewer(Resume(), role="factory")
    assert result["age_band"] == "30-34岁"
    assert "phone" not in result

