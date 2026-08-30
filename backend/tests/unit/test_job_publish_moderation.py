import pytest

from app.services.job_publish_moderation import moderate_job_fields


BASE = {"city": "苏州", "job_category": "普工", "salary_floor_monthly": 5000, "pay_type": "月薪", "headcount": 2}


def test_moderation_is_deterministic_and_records_rule_version():
    first = moderate_job_fields(BASE)
    second = moderate_job_fields(BASE)
    assert first == second
    assert first.status == "passed"
    assert first.rule_version == "job_moderation_v1"


@pytest.mark.parametrize("field,value,reason", [
    ("salary_floor_monthly", 0, "invalid_salary"),
    ("headcount", 0, "invalid_headcount"),
    ("pay_type", "未知", "invalid_pay_type"),
])
def test_invalid_business_ranges_are_rejected(field, value, reason):
    values = {**BASE, field: value}
    result = moderate_job_fields(values)
    assert result.status == "rejected"
    assert reason in result.reason


def test_missing_fields_are_pending_and_not_auto_activated():
    result = moderate_job_fields({"city": "苏州"})
    assert result.status == "pending"
    assert result.matched_rules == ("required_fields",)
