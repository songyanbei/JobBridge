"""Phase 3 集成测试：中介双向检索流程。

验证：broker 方向切换、session 状态、双向检索。
运行方式：RUN_INTEGRATION=1 pytest tests/integration/test_phase3_broker_flow.py -v
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.db import SessionLocal
from app.llm.base import RerankResult
from app.models import Job, Resume, SystemConfig, User
from app.schemas.conversation import SessionState
from app.services import conversation_service, search_service
from app.services.user_service import UserContext

pytestmark = pytest.mark.integration


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def broker_user(db):
    user = User(
        external_userid="test_broker_001",
        role="broker",
        status="active",
        company="中介公司",
        can_search_jobs=True,
        can_search_workers=True,
    )
    db.add(user)
    db.flush()
    return user


@pytest.fixture
def seed_data(db, broker_user):
    now = datetime.now(timezone.utc)

    # 配置
    for key, val in [("match.top_n", "3"), ("match.max_candidates", "50")]:
        db.merge(SystemConfig(config_key=key, config_value=val, value_type="int"))

    # 工厂用户（岗位所有者）— 先 flush 父记录
    factory = User(
        external_userid="test_factory_for_broker",
        role="factory", status="active",
        company="电子厂A", phone="13800001111",
        can_search_jobs=False, can_search_workers=True,
    )
    db.add(factory)

    # 工人用户（简历所有者）
    worker = User(
        external_userid="test_worker_for_broker",
        role="worker", status="active",
        display_name="张三", phone="13900002222",
        can_search_jobs=True, can_search_workers=False,
    )
    db.add(worker)
    db.flush()  # 父记录先落库，确保子记录外键可用

    # 岗位
    job = Job(
        owner_userid="test_factory_for_broker",
        city="苏州市", job_category="电子厂",
        salary_floor_monthly=5500, pay_type="月薪", headcount=10,
        gender_required="不限", is_long_term=True,
        raw_text="招普工", audit_status="passed",
        audited_by="system", audited_at=now,
        expires_at=now + timedelta(days=30),
    )
    db.add(job)

    # 简历
    resume = Resume(
        owner_userid="test_worker_for_broker",
        expected_cities=["苏州市"],
        expected_job_categories=["电子厂"],
        salary_expect_floor_monthly=5000,
        gender="男", age=30,
        accept_long_term=True, accept_short_term=False,
        raw_text="求职简历", audit_status="passed",
        audited_by="system", audited_at=now,
        expires_at=now + timedelta(days=30),
    )
    db.add(resume)
    db.flush()
    return job, resume


class TestBrokerDirectionSwitch:
    def test_switch_to_job(self, db, broker_user):
        session = SessionState(role="broker")
        err = conversation_service.set_broker_direction(session, "search_job")
        assert err is None
        assert session.broker_direction == "search_job"

    def test_switch_to_worker(self, db, broker_user):
        session = SessionState(role="broker")
        err = conversation_service.set_broker_direction(session, "search_worker")
        assert err is None
        assert session.broker_direction == "search_worker"

    def test_non_broker_fails(self, db):
        session = SessionState(role="worker")
        err = conversation_service.set_broker_direction(session, "search_job")
        assert err is not None


class TestBrokerDualSearch:
    @patch("app.services.search_service.get_reranker")
    def test_broker_search_jobs(self, mock_factory, db, broker_user, seed_data):
        job, _ = seed_data
        mock_reranker = MagicMock()
        mock_reranker.rerank.return_value = RerankResult(
            ranked_items=[{"id": job.id, "score": 0.9}],
            reply_text="推荐",
        )
        mock_factory.return_value = mock_reranker

        user_ctx = UserContext(
            external_userid=broker_user.external_userid,
            role="broker", status="active",
            display_name=None, company="中介公司",
            contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(role="broker", broker_direction="search_job")

        result = search_service.search_jobs(
            {"city": ["苏州市"]}, "苏州找岗位", session, user_ctx, db,
        )
        assert result.result_count > 0

    @patch("app.services.search_service.get_reranker")
    def test_broker_search_workers(self, mock_factory, db, broker_user, seed_data):
        _, resume = seed_data
        mock_reranker = MagicMock()
        mock_reranker.rerank.return_value = RerankResult(
            ranked_items=[{"id": resume.id, "score": 0.85}],
            reply_text="推荐",
        )
        mock_factory.return_value = mock_reranker

        user_ctx = UserContext(
            external_userid=broker_user.external_userid,
            role="broker", status="active",
            display_name=None, company="中介公司",
            contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        session = SessionState(role="broker", broker_direction="search_worker")

        result = search_service.search_workers(
            {"city": ["苏州市"]}, "苏州找工人", session, user_ctx, db,
        )
        assert result.result_count > 0
