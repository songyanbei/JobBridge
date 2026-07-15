"""Recommendation reason builder tests."""
from app.services.recommendation_reason_service import (
    build_match_reasons,
    project_job_for_explanation,
    project_resume_for_explanation,
)


def test_job_projection_drops_contact_fields():
    projected = project_job_for_explanation({
        "id": 1,
        "company": "XX电子",
        "job_category": "电子厂",
        "city": "苏州市",
        "phone": "13800001111",
        "contact_person": "张经理",
    })

    assert projected.id == "1"
    assert not hasattr(projected, "phone")
    assert not hasattr(projected, "contact_person")


def test_job_hard_reasons_are_limited_and_deterministic():
    item = project_job_for_explanation({
        "id": 1,
        "company": "XX电子",
        "job_category": "电子厂",
        "city": "苏州市",
        "salary_floor_monthly": 6000,
        "salary_ceiling_monthly": 7000,
    })

    reasons = build_match_reasons(
        item=item,
        criteria={
            "city": ["苏州市"],
            "job_category": ["电子厂"],
            "salary_floor_monthly": 6500,
        },
        item_type="job",
    )

    assert len(reasons) == 2
    assert all(r.kind == "hard_match" for r in reasons)


def test_resume_projection_drops_phone_field():
    projected = project_resume_for_explanation({
        "id": 1,
        "display_name": "张三",
        "expected_cities": ["苏州市"],
        "phone": "13800001111",
    })

    assert projected.name == "张三"
    assert not hasattr(projected, "phone")
