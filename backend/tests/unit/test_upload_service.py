"""upload_service 单元测试。"""
from unittest.mock import MagicMock, patch

import pytest

from app.llm.base import IntentResult
from app.schemas.conversation import SessionState
from app.services.upload_service import (
    UploadResult,
    _check_required_fields,
    _generate_followup_text,
    process_upload,
)
from app.services.user_service import UserContext
from app.llm.prompts import JOB_REQUIRED_FIELDS, RESUME_REQUIRED_FIELDS


def _make_user_ctx(**kwargs):
    return UserContext(
        external_userid=kwargs.get("external_userid", "u1"),
        role=kwargs.get("role", "factory"),
        status="active",
        display_name=None, company="XX厂", contact_person=None, phone=None,
        can_search_jobs=False, can_search_workers=True,
        is_first_touch=False, should_welcome=False,
    )


def _make_session(**kwargs):
    return SessionState(role=kwargs.get("role", "factory"), **{
        k: v for k, v in kwargs.items() if k != "role"
    })


class TestCheckRequiredFields:
    def test_all_present(self):
        data = {
            "city": "苏州市", "job_category": "电子厂",
            "salary_floor_monthly": 5000, "pay_type": "月薪", "headcount": 10,
        }
        missing = _check_required_fields(data, JOB_REQUIRED_FIELDS)
        assert missing == []

    def test_missing_some(self):
        data = {"city": "苏州市", "job_category": "电子厂"}
        missing = _check_required_fields(data, JOB_REQUIRED_FIELDS)
        assert "salary_floor_monthly" in missing
        assert "pay_type" in missing
        assert "headcount" in missing

    def test_empty_list_counts_as_missing(self):
        data = {"expected_cities": [], "expected_job_categories": ["电子厂"],
                "salary_expect_floor_monthly": 5000, "gender": "男", "age": 30}
        missing = _check_required_fields(data, RESUME_REQUIRED_FIELDS)
        assert "expected_cities" in missing


class TestGenerateFollowupText:
    def test_one_field_merged(self):
        text = _generate_followup_text(["city"])
        assert "工作城市" in text
        assert "\n" not in text  # 单句

    def test_two_fields_merged(self):
        text = _generate_followup_text(["city", "job_category"])
        assert "和" in text

    def test_three_fields_list(self):
        text = _generate_followup_text(["city", "job_category", "salary_floor_monthly"])
        assert "- " in text  # 列表式


class TestProcessUpload:
    @patch("app.services.upload_service.audit_service")
    @patch("app.services.upload_service.conversation_service")
    def test_successful_upload(self, mock_conv, mock_audit):
        from app.services.audit_service import AuditResult
        mock_audit.audit_content_only.return_value = AuditResult(
            status="passed", reason="", matched_words=[],
        )
        mock_audit.write_audit_log_for_result = MagicMock()

        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = MagicMock(
            config_value="30",
        )
        db.add = MagicMock()
        db.flush = MagicMock()

        user_ctx = _make_user_ctx()
        intent = IntentResult(
            intent="upload_job",
            structured_data={
                "city": "苏州市", "job_category": "电子厂",
                "salary_floor_monthly": 5500, "pay_type": "月薪", "headcount": 30,
            },
            confidence=0.95,
        )
        session = _make_session()

        result = process_upload(user_ctx, intent, "招工文本", [], session, db)
        assert result.success is True
        assert "已入库" in result.reply_text

    @patch("app.services.upload_service.conversation_service")
    def test_missing_fields_followup(self, mock_conv):
        user_ctx = _make_user_ctx()
        intent = IntentResult(
            intent="upload_job",
            structured_data={"city": "苏州市"},
            confidence=0.7,
        )
        session = _make_session(follow_up_rounds=0)
        db = MagicMock()

        result = process_upload(user_ctx, intent, "苏州招工", [], session, db)
        assert result.success is False
        assert result.needs_followup is True

    @patch("app.services.upload_service.conversation_service")
    def test_high_follow_up_rounds_does_not_short_circuit(self, mock_conv):
        """Stage C1（spec §2.6）：max rounds 退出由 message_router._handle_field_patch
        的 failed_patch_rounds 全权管控；process_upload 不再用 follow_up_rounds 早退。

        旧 Stage A 行为（follow_up_rounds >= MAX 时由 process_upload 返回降级文案 +
        needs_followup=False）已主动放弃，否则用户分多轮成功补不同有效字段时会被
        误清草稿（spec §9.5"补了其它有效字段不算 failed"）。
        """
        user_ctx = _make_user_ctx()
        intent = IntentResult(
            intent="upload_job",
            structured_data={"city": "苏州市"},
            confidence=0.7,
        )
        # follow_up_rounds 高也不应让 process_upload 早退；它只是兼容计数器
        session = _make_session(follow_up_rounds=5)
        db = MagicMock()

        result = process_upload(user_ctx, intent, "苏州招工", [], session, db)
        assert result.success is False
        # C1 契约：missing 时一律返回追问（needs_followup=True），由 _handle_field_patch
        # 在外层用 failed_patch_rounds 决定是否清草稿。
        assert result.needs_followup is True
        assert session.pending_upload_intent == "upload_job"
