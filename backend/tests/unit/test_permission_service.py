"""permission_service 单元测试。"""
import pytest

from app.services.permission_service import (
    filter_job_for_role,
    filter_jobs_batch,
    filter_resume_for_role,
    filter_resumes_batch,
)
from app.services.visibility_policy import default_policy_document, normalize_policy, snapshot_from_policy


def _snapshot(scene, role):
    return snapshot_from_policy(normalize_policy(default_policy_document()), scene, role)


def _sample_job():
    return {
        "id": 1,
        "city": "苏州市",
        "job_category": "电子厂",
        "salary_floor_monthly": 5500,
        "salary_ceiling_monthly": 6500,
        "pay_type": "月薪",
        "headcount": 30,
        "gender_required": "不限",
        "age_min": 18,
        "age_max": 45,
        "accept_minority": True,
        "is_long_term": True,
        "district": "吴中区",
        "provide_meal": True,
        "provide_housing": True,
        "company": "XX电子厂",
        "hiring_company": "XX电子厂",
        "hiring_company_source": "job.hiring_company",
        "contact_person": "张经理",
        "phone": "13812345678",
        "description": "招普工",
    }


def _sample_resume():
    return {
        "id": 1,
        "expected_cities": ["苏州市"],
        "expected_job_categories": ["电子厂"],
        "salary_expect_floor_monthly": 5000,
        "gender": "男",
        "age": 35,
        "education": "高中",
        "work_experience": "3年电子厂经验",
        "owner_userid": "u_worker_1",
    }


class TestFilterJobForRole:
    def test_worker_no_phone(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "worker", _snapshot("job_search", "worker"))
        assert "phone" not in filtered
        assert "contact_person" not in filtered

    def test_worker_no_discriminatory_fields(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "worker", _snapshot("job_search", "worker"))
        assert "gender_required" not in filtered
        assert "age_min" not in filtered
        assert "age_max" not in filtered
        assert "accept_minority" not in filtered

    def test_worker_keeps_business_fields(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "worker", _snapshot("job_search", "worker"))
        assert "city" not in filtered
        assert "salary_floor_monthly" in filtered
        assert "provide_meal" not in filtered
        assert "hiring_company" in filtered

    def test_factory_job_search_is_fail_closed(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "factory", _snapshot("job_search", "factory"))
        assert filtered == {"id": 1}

    def test_broker_sees_all(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "broker", _snapshot("job_search", "broker"))
        assert "phone" in filtered


class TestFilterResumeForRole:
    def test_factory_sees_phone(self):
        resume = _sample_resume()
        user = {"display_name": "张三", "phone": "13800001111"}
        filtered = filter_resume_for_role(resume, user, "factory", _snapshot("candidate_search", "factory"))
        assert filtered["phone"] == "13800001111"
        assert filtered["display_name"] == "张三"

    def test_phone_missing_placeholder(self):
        resume = _sample_resume()
        user = {"display_name": "张三", "phone": None}
        filtered = filter_resume_for_role(resume, user, "factory", _snapshot("candidate_search", "factory"))
        assert filtered["phone"] is None
        assert filtered["phone_placeholder"] == "联系方式待补充"

    def test_no_user_data(self):
        resume = _sample_resume()
        filtered = filter_resume_for_role(resume, None, "factory", _snapshot("candidate_search", "factory"))
        assert filtered["phone_placeholder"] == "联系方式待补充"


class TestBatchFiltering:
    def test_jobs_batch(self):
        jobs = [_sample_job(), _sample_job()]
        filtered = filter_jobs_batch(jobs, "worker", _snapshot("job_search", "worker"))
        for j in filtered:
            assert "phone" not in j

    def test_resumes_batch(self):
        resumes = [_sample_resume()]
        users_map = {"u_worker_1": {"display_name": "张三", "phone": "138"}}
        filtered = filter_resumes_batch(resumes, users_map, "factory", _snapshot("candidate_search", "factory"))
        assert filtered[0]["display_name"] == "张三"


def test_explicit_missing_snapshot_returns_id_only():
    assert filter_job_for_role(_sample_job(), "broker", None) == {"id": 1}
    assert filter_resume_for_role(_sample_resume(), {}, "factory", None) == {"id": 1}


def test_omitted_snapshot_is_a_programming_error():
    with pytest.raises(TypeError):
        filter_job_for_role(_sample_job(), "broker")
    with pytest.raises(TypeError):
        filter_resume_for_role(_sample_resume(), {}, "factory")
