"""DTO 序列化 / 反序列化 / 校验测试。"""
from datetime import datetime, date

import pytest
from pydantic import ValidationError

from app.schemas.user import UserCreate, UserRead, UserUpdate
from app.schemas.job import JobCreate, JobRead, JobBrief
from app.schemas.resume import ResumeCreate, ResumeRead, ResumeBrief
from app.schemas.conversation import (
    CandidateSnapshot,
    SessionState,
    CriteriaPatch,
    ConversationLogCreate,
    ConversationLogRead,
)
from app.schemas.llm import IntentResult, RerankResult
from app.schemas.admin import (
    AdminLogin,
    AdminToken,
    AdminUserRead,
    SystemConfigRead,
    SystemConfigUpdate,
    AuditLogRead,
)


# ---------------------------------------------------------------------------
# User
# ---------------------------------------------------------------------------

class TestUserSchemas:
    def test_user_create(self):
        u = UserCreate(external_userid="ext_001", role="worker")
        assert u.external_userid == "ext_001"
        assert u.role == "worker"

    def test_user_read_from_dict(self):
        data = {
            "external_userid": "ext_001",
            "role": "factory",
            "registered_at": datetime(2026, 1, 1),
        }
        u = UserRead(**data)
        assert u.status == "active"

    def test_user_update_partial(self):
        u = UserUpdate(display_name="张三")
        assert u.phone is None


# ---------------------------------------------------------------------------
# Job
# ---------------------------------------------------------------------------

class TestJobSchemas:
    def _job_create_data(self) -> dict:
        return {
            "owner_userid": "ext_f01",
            "city": "苏州市",
            "job_category": "电子厂",
            "salary_floor_monthly": 5000,
            "pay_type": "月薪",
            "headcount": 10,
            "raw_text": "招工人 10 名",
            "expires_at": datetime(2026, 5, 1),
        }

    def test_job_create(self):
        j = JobCreate(**self._job_create_data())
        assert j.gender_required == "不限"
        assert j.is_long_term is True

    def test_job_create_missing_required_field(self):
        data = self._job_create_data()
        del data["city"]
        with pytest.raises(ValidationError):
            JobCreate(**data)

    def test_job_read(self):
        data = {
            **self._job_create_data(),
            "id": 1,
            "gender_required": "不限",
            "is_long_term": True,
            "audit_status": "pending",
            "created_at": datetime(2026, 4, 1),
            "updated_at": datetime(2026, 4, 1),
            "version": 1,
        }
        j = JobRead(**data)
        assert j.id == 1

    def test_job_brief(self):
        b = JobBrief(
            id=1, city="苏州市", job_category="电子厂",
            salary_floor_monthly=5000, pay_type="月薪",
            headcount=10, gender_required="不限", is_long_term=True,
            created_at=datetime(2026, 4, 1),
        )
        assert b.district is None


# ---------------------------------------------------------------------------
# Resume
# ---------------------------------------------------------------------------

class TestResumeSchemas:
    def _resume_create_data(self) -> dict:
        return {
            "owner_userid": "ext_w01",
            "expected_cities": ["苏州市"],
            "expected_job_categories": ["电子厂"],
            "salary_expect_floor_monthly": 5000,
            "gender": "男",
            "age": 25,
            "raw_text": "找苏州电子厂",
            "expires_at": datetime(2026, 5, 1),
        }

    def test_resume_create(self):
        r = ResumeCreate(**self._resume_create_data())
        assert r.accept_long_term is True

    def test_resume_create_empty_cities_fails(self):
        data = self._resume_create_data()
        data["expected_cities"] = []
        with pytest.raises(ValidationError):
            ResumeCreate(**data)

    def test_resume_read(self):
        data = {
            **self._resume_create_data(),
            "id": 1,
            "audit_status": "pending",
            "created_at": datetime(2026, 4, 1),
            "updated_at": datetime(2026, 4, 1),
            "version": 1,
            "accept_long_term": True,
            "accept_short_term": False,
        }
        r = ResumeRead(**data)
        assert r.id == 1


# ---------------------------------------------------------------------------
# Conversation
# ---------------------------------------------------------------------------

class TestConversationSchemas:
    def test_candidate_snapshot(self):
        s = CandidateSnapshot(
            candidate_ids=["1", "2", "3"],
            ranking_version=1,
            query_digest="abc123def456",
            created_at="2026-04-01T00:00:00",
            expires_at="2026-04-01T00:30:00",
        )
        assert len(s.candidate_ids) == 3

    def test_session_state(self):
        ss = SessionState(
            role="worker",
            updated_at="2026-04-01T00:00:00",
        )
        assert ss.search_criteria == {}
        assert ss.candidate_snapshot is None
        assert ss.shown_items == []
        assert ss.history == []

    def test_session_state_with_snapshot(self):
        snap = CandidateSnapshot(
            candidate_ids=["10", "20"],
            ranking_version=2,
            query_digest="xyz",
            created_at="2026-04-01T00:00:00",
            expires_at="2026-04-01T00:30:00",
        )
        ss = SessionState(
            role="factory",
            candidate_snapshot=snap,
            shown_items=["10"],
            updated_at="2026-04-01T00:00:00",
        )
        assert ss.candidate_snapshot.ranking_version == 2

    def test_criteria_patch(self):
        cp = CriteriaPatch(op="add", field="city", value="苏州市")
        assert cp.op == "add"

    def test_conversation_log_create(self):
        c = ConversationLogCreate(
            userid="ext_001",
            direction="in",
            msg_type="text",
            content="你好",
            expires_at=datetime(2026, 5, 1),
        )
        assert c.wecom_msg_id is None


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

class TestLLMSchemas:
    def test_intent_result(self):
        r = IntentResult(intent="search_job")
        assert r.confidence == 0.0
        assert r.structured_data == {}

    def test_rerank_result(self):
        r = RerankResult()
        assert r.ranked_items == []
        assert r.reply_text == ""


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------

class TestAdminSchemas:
    def test_admin_login(self):
        a = AdminLogin(username="admin", password="123456")
        assert a.username == "admin"

    def test_admin_login_short_password_fails(self):
        with pytest.raises(ValidationError):
            AdminLogin(username="admin", password="123")

    def test_admin_token(self):
        t = AdminToken(access_token="abc.def.ghi")
        assert t.token_type == "bearer"

    def test_admin_user_read(self):
        a = AdminUserRead(
            id=1, username="admin",
            created_at=datetime(2026, 1, 1),
        )
        assert a.enabled is True

    def test_system_config_read(self):
        c = SystemConfigRead(
            config_key="max_retry",
            config_value="3",
            updated_at=datetime(2026, 1, 1),
        )
        assert c.value_type == "string"

    def test_audit_log_read(self):
        a = AuditLogRead(
            id=1,
            target_type="job",
            target_id="100",
            action="auto_pass",
            created_at=datetime(2026, 4, 1),
        )
        assert a.operator is None
