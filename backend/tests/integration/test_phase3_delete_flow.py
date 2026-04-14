"""Phase 3 集成测试：/删除我的信息 流程。

验证：session 清空、简历软删除、对话日志软删除、user.status=deleted、写 log。
运行方式：RUN_INTEGRATION=1 pytest tests/integration/test_phase3_delete_flow.py -v
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

from app.db import SessionLocal
from app.models import AuditLog, ConversationLog, Resume, User
from app.services import user_service

pytestmark = pytest.mark.integration


@pytest.fixture
def db():
    session = SessionLocal()
    yield session
    session.rollback()
    session.close()


@pytest.fixture
def worker_with_data(db):
    now = datetime.now(timezone.utc)
    user = User(
        external_userid="test_delete_worker",
        role="worker",
        status="active",
        can_search_jobs=True,
        can_search_workers=False,
    )
    db.add(user)
    db.flush()  # 父记录先落库，确保外键可用

    resume = Resume(
        owner_userid="test_delete_worker",
        expected_cities=["苏州市"],
        expected_job_categories=["电子厂"],
        salary_expect_floor_monthly=5000,
        gender="男",
        age=30,
        accept_long_term=True,
        accept_short_term=False,
        raw_text="求职简历",
        audit_status="passed",
        expires_at=now + timedelta(days=30),
    )
    db.add(resume)

    log = ConversationLog(
        userid="test_delete_worker",
        direction="in",
        msg_type="text",
        content="苏州找电子厂",
        expires_at=now + timedelta(days=30),
    )
    db.add(log)
    db.flush()
    return user, resume, log


class TestDeleteFlow:
    @patch("app.services.user_service.conversation_service.clear_session")
    def test_delete_clears_all(self, mock_clear, db, worker_with_data):
        user, resume, log = worker_with_data

        reply = user_service.delete_user_data("test_delete_worker", db)
        db.flush()

        # 1. session 被清空
        mock_clear.assert_called_once_with("test_delete_worker")

        # 2. 简历被软删除
        updated_resume = db.query(Resume).filter(Resume.id == resume.id).first()
        assert updated_resume.deleted_at is not None

        # 3. user.status = deleted
        updated_user = db.query(User).filter(
            User.external_userid == "test_delete_worker",
        ).first()
        assert updated_user.status == "deleted"

        # 4. 写了 conversation_log
        delete_logs = db.query(ConversationLog).filter(
            ConversationLog.userid == "test_delete_worker",
            ConversationLog.msg_type == "system",
        ).all()
        assert len(delete_logs) >= 1

        # 5. 写了 audit_log
        audit_logs = db.query(AuditLog).filter(
            AuditLog.target_type == "user",
            AuditLog.target_id == "test_delete_worker",
        ).all()
        assert len(audit_logs) >= 1

        # 6. 回复文案
        assert "删除" in reply
