"""Phase 3 集成测试：上传→审核→检索→权限过滤→格式化。

需要 MySQL + Redis。运行方式：RUN_INTEGRATION=1 pytest tests/integration/test_phase3_upload_and_search.py -v
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.db import SessionLocal
from app.llm.base import IntentResult, RerankResult
from app.models import DictSensitiveWord, Job, Resume, SystemConfig, User
from app.schemas.conversation import SessionState
from app.services import (
    audit_service,
    conversation_service,
    permission_service,
    search_service,
    upload_service,
)
from app.services.user_service import UserContext

pytestmark = pytest.mark.integration


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def factory_user(db):
    user = User(
        external_userid="test_factory_001",
        role="factory",
        status="active",
        company="测试电子厂",
        contact_person="张经理",
        phone="13800001111",
        can_search_jobs=False,
        can_search_workers=True,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def worker_user(db):
    user = User(
        external_userid="test_worker_001",
        role="worker",
        status="active",
        can_search_jobs=True,
        can_search_workers=False,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def seed_config(db):
    configs = [
        SystemConfig(config_key="match.top_n", config_value="3", value_type="int"),
        SystemConfig(config_key="match.max_candidates", config_value="50", value_type="int"),
        SystemConfig(config_key="ttl.job.days", config_value="30", value_type="int"),
        SystemConfig(config_key="ttl.resume.days", config_value="30", value_type="int"),
    ]
    for c in configs:
        db.merge(c)
    db.flush()


@pytest.fixture
def seed_jobs(db, factory_user, seed_config):
    now = datetime.now(timezone.utc)
    jobs = []
    for i in range(5):
        job = Job(
            owner_userid=factory_user.external_userid,
            city="苏州市",
            job_category="电子厂",
            salary_floor_monthly=5000 + i * 500,
            pay_type="月薪",
            headcount=10,
            gender_required="不限",
            is_long_term=True,
            raw_text=f"苏州电子厂招普工{i}",
            audit_status="passed",
            audited_by="system",
            audited_at=now,
            expires_at=now + timedelta(days=30),
        )
        db.add(job)
        jobs.append(job)
    db.flush()
    return jobs


class TestJobUploadAndSearch:
    def test_upload_job_passes_audit(self, db, factory_user, seed_config):
        """岗位上传→审核通过→入库。"""
        user_ctx = UserContext(
            external_userid=factory_user.external_userid,
            role="factory", status="active",
            display_name=None, company="测试电子厂",
            contact_person="张经理", phone="13800001111",
            can_search_jobs=False, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        intent = IntentResult(
            intent="upload_job",
            structured_data={
                "city": "苏州市", "job_category": "电子厂",
                "salary_floor_monthly": 5500, "pay_type": "月薪", "headcount": 30,
            },
            confidence=0.95,
        )
        session = SessionState(role="factory")
        result = upload_service.process_upload(
            user_ctx, intent, "苏州电子厂招普工30人", [], session, db,
        )
        assert result.success is True
        assert result.entity_type == "job"
        assert result.entity_id is not None

    @patch("app.services.search_service.get_reranker")
    def test_worker_search_jobs_no_leak(
        self, mock_reranker_factory, db, worker_user, seed_jobs, seed_config,
    ):
        """工人找岗位→结果不泄漏电话/歧视性字段。"""
        mock_reranker = MagicMock()
        mock_reranker.rerank.return_value = RerankResult(
            ranked_items=[
                {"id": seed_jobs[0].id, "score": 0.9},
                {"id": seed_jobs[1].id, "score": 0.8},
                {"id": seed_jobs[2].id, "score": 0.7},
            ],
            reply_text="推荐岗位",
        )
        mock_reranker_factory.return_value = mock_reranker

        user_ctx = UserContext(
            external_userid=worker_user.external_userid,
            role="worker", status="active",
            display_name=None, company=None, contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=False,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(role="worker")
        criteria = {"city": ["苏州市"], "job_category": ["电子厂"]}

        result = search_service.search_jobs(
            criteria, "苏州找电子厂", session, user_ctx, db,
        )
        assert result.result_count > 0
        # 工人侧回复不应包含电话
        assert "13800001111" not in result.reply_text


class TestPendingJobNotInRecall:
    def test_pending_job_excluded(self, db, factory_user, seed_config):
        """待审岗位不进入召回池。"""
        now = datetime.now(timezone.utc)
        pending_job = Job(
            owner_userid=factory_user.external_userid,
            city="苏州市", job_category="电子厂",
            salary_floor_monthly=6000, pay_type="月薪", headcount=5,
            gender_required="不限", is_long_term=True,
            raw_text="pending job", audit_status="pending",
            expires_at=now + timedelta(days=30),
        )
        db.add(pending_job)
        db.flush()

        from app.services.search_service import _query_jobs
        results = _query_jobs({"city": ["苏州市"]}, 50, db)
        ids = [j.id for j in results]
        assert pending_job.id not in ids
