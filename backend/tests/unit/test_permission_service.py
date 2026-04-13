"""permission_service 单元测试。"""
import pytest

from app.services.permission_service import (
    filter_job_for_role,
    filter_jobs_batch,
    filter_resume_for_role,
    filter_resumes_batch,
)


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
        filtered = filter_job_for_role(job, "worker")
        assert "phone" not in filtered
        assert "contact_person" not in filtered

    def test_worker_no_discriminatory_fields(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "worker")
        assert "gender_required" not in filtered
        assert "age_min" not in filtered
        assert "age_max" not in filtered
        assert "accept_minority" not in filtered

    def test_worker_keeps_business_fields(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "worker")
        assert "city" in filtered
        assert "salary_floor_monthly" in filtered
        assert "provide_meal" in filtered
        assert "company" in filtered

    def test_factory_sees_all(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "factory")
        assert "phone" in filtered
        assert "gender_required" in filtered

    def test_broker_sees_all(self):
        job = _sample_job()
        filtered = filter_job_for_role(job, "broker")
        assert "phone" in filtered


class TestFilterResumeForRole:
    def test_factory_sees_phone(self):
        resume = _sample_resume()
        user = {"display_name": "张三", "phone": "13800001111"}
        filtered = filter_resume_for_role(resume, user, "factory")
        assert filtered["phone"] == "13800001111"
        assert filtered["display_name"] == "张三"

    def test_phone_missing_placeholder(self):
        resume = _sample_resume()
        user = {"display_name": "张三", "phone": None}
        filtered = filter_resume_for_role(resume, user, "factory")
        assert filtered["phone"] is None
        assert filtered["phone_placeholder"] == "联系方式待补充"

    def test_no_user_data(self):
        resume = _sample_resume()
        filtered = filter_resume_for_role(resume, None, "factory")
        assert filtered["phone_placeholder"] == "联系方式待补充"


class TestBatchFiltering:
    def test_jobs_batch(self):
        jobs = [_sample_job(), _sample_job()]
        filtered = filter_jobs_batch(jobs, "worker")
        for j in filtered:
            assert "phone" not in j

    def test_resumes_batch(self):
        resumes = [_sample_resume()]
        users_map = {"u_worker_1": {"display_name": "张三", "phone": "138"}}
        filtered = filter_resumes_batch(resumes, users_map, "factory")
        assert filtered[0]["display_name"] == "张三"
