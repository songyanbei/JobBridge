from datetime import datetime

from app.schemas.job import JobCreate, JobRead


def test_job_read_accepts_candidate_without_business_expiry():
    job = JobRead(
        id=1, owner_userid="owner", city="上海", job_category="普工",
        salary_floor_monthly=6000, pay_type="月薪", headcount=1,
        gender_required="不限", is_long_term=True, raw_text="岗位",
        audit_status="pending", created_at=datetime.now(), updated_at=datetime.now(),
        expires_at=None, version=1,
    )
    assert job.model_dump(mode="json")["expires_at"] is None


def test_job_create_does_not_require_callers_to_choose_business_expiry():
    job = JobCreate(
        owner_userid="owner", city="上海", job_category="普工",
        salary_floor_monthly=6000, pay_type="月薪", headcount=1,
        gender_required="不限", is_long_term=True, raw_text="岗位",
    )

    assert job.expires_at is None
