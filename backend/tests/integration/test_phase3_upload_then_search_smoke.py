"""Phase 3 Smoke 测试：upload + search 服务级串联。

验证：厂家上传岗位 → 审核通过 → 工人搜索能命中。
运行方式：RUN_INTEGRATION=1 pytest tests/integration/test_phase3_upload_then_search_smoke.py -v
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.db import SessionLocal
from app.llm.base import IntentResult, RerankResult
from app.models import SystemConfig, User
from app.schemas.conversation import SessionState
from app.services import search_service, upload_service
from app.services.user_service import UserContext

pytestmark = pytest.mark.integration


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def setup(db):
    """创建基础用户和配置。"""
    factory = User(
        external_userid="smoke_factory",
        role="factory", status="active",
        company="Smoke电子厂", phone="13811112222",
        can_search_jobs=False, can_search_workers=True,
    )
    worker = User(
        external_userid="smoke_worker",
        role="worker", status="active",
        can_search_jobs=True, can_search_workers=False,
    )
    db.add(factory)
    db.add(worker)
    db.flush()  # 用户先落库，后续 upload_service 创建子记录时外键可用

    for key, val in [
        ("match.top_n", "3"),
        ("match.max_candidates", "50"),
        ("ttl.job.days", "30"),
    ]:
        db.merge(SystemConfig(config_key=key, config_value=val, value_type="int"))

    db.flush()
    return factory, worker


class TestUploadThenSearchSmoke:
    @patch("app.services.search_service.get_reranker")
    def test_full_flow(self, mock_reranker_factory, db, setup):
        factory, worker = setup

        # Step 1: 厂家上传岗位
        factory_ctx = UserContext(
            external_userid="smoke_factory",
            role="factory", status="active",
            display_name=None, company="Smoke电子厂",
            contact_person=None, phone="13811112222",
            can_search_jobs=False, can_search_workers=True,
            is_first_touch=False, should_welcome=False,
        )
        intent = IntentResult(
            intent="upload_job",
            structured_data={
                "city": "苏州市", "job_category": "电子厂",
                "salary_floor_monthly": 5500, "pay_type": "月薪",
                "headcount": 20, "provide_meal": True, "provide_housing": True,
            },
            confidence=0.95,
        )
        factory_session = SessionState(role="factory")
        upload_result = upload_service.process_upload(
            factory_ctx, intent, "苏州电子厂招普工20人", [], factory_session, db,
        )
        assert upload_result.success is True
        assert upload_result.entity_id is not None
        job_id = upload_result.entity_id

        # Step 2: 工人搜索
        mock_reranker = MagicMock()
        mock_reranker.rerank.return_value = RerankResult(
            ranked_items=[{"id": job_id, "score": 0.95}],
            reply_text="推荐",
        )
        mock_reranker_factory.return_value = mock_reranker

        worker_ctx = UserContext(
            external_userid="smoke_worker",
            role="worker", status="active",
            display_name=None, company=None,
            contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=False,
            is_first_touch=False, should_welcome=False,
        )
        worker_session = SessionState(role="worker")
        search_result = search_service.search_jobs(
            {"city": ["苏州市"], "job_category": ["电子厂"]},
            "苏州找电子厂",
            worker_session,
            worker_ctx,
            db,
        )
        assert search_result.result_count > 0
        # 工人不应看到电话
        assert "13811112222" not in search_result.reply_text
        # 应该有结果
        assert "苏州" in search_result.reply_text or "电子" in search_result.reply_text
