from types import SimpleNamespace
from app.services.job_replacement_lock_service import business_digest

def test_business_digest_changes_for_business_field_not_lifecycle_field():
    job = SimpleNamespace(owner_userid='u', city='A', district=None, address=None, job_category='x', job_sub_category=None, salary_floor_monthly=1, salary_ceiling_monthly=None, pay_type='月薪', headcount=1, gender_required='不限', age_min=None, age_max=None, is_long_term=True, images=[], extra={}, raw_text='x', description=None, expires_at='old')
    before = business_digest(job)
    job.expires_at = 'new'
    assert business_digest(job) == before
    job.city = 'B'
    assert business_digest(job) != before
