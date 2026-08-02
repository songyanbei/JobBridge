from app.services.permission_service import filter_job_for_role
from app.services.visibility_policy import (
    default_policy_document, normalize_policy, project_for_reranker,
    project_soft_preferences, snapshot_from_policy,
)


def test_disabled_fields_are_removed_before_model_and_final_reply():
    payload = default_policy_document(3)
    payload["job_search"]["broker"] = ["hiring_company", "job_category", "salary"]
    policy = normalize_policy(payload)
    snapshot = snapshot_from_policy(policy, "job_search", "broker")
    candidate = {
        "id": 7, "hiring_company": "岗位工厂", "job_category": "普工",
        "salary_floor_monthly": 6000, "phone": "13800000000",
        "contact_person": "联系人", "address": "详细地址",
    }
    model_input = project_for_reranker("job_search", "broker", snapshot, candidate)
    final_candidate = filter_job_for_role(candidate, "broker", snapshot)
    assert model_input == {
        "id": 7, "job_category": "普工", "salary_floor_monthly": 6000,
    }
    assert "phone" not in final_candidate
    assert "contact_person" not in final_candidate
    assert "address" not in final_candidate


def test_soft_preferences_follow_the_same_snapshot():
    payload = default_policy_document(3)
    payload["job_search"]["broker"] = ["hiring_company", "job_category", "salary"]
    snapshot = snapshot_from_policy(normalize_policy(payload), "job_search", "broker")
    assert project_soft_preferences(
        snapshot, {"job_category": ["普工"], "city": ["苏州"], "phone": ["138"]},
    ) == {"job_category": ["普工"]}
