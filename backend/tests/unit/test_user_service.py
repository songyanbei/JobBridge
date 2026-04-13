"""user_service 单元测试。"""
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest

from app.services.user_service import (
    UserContext,
    check_user_status,
    delete_user_data,
    identify_or_register,
)


def _make_user_mock(**kwargs):
    user = MagicMock()
    user.external_userid = kwargs.get("external_userid", "u1")
    user.role = kwargs.get("role", "worker")
    user.status = kwargs.get("status", "active")
    user.display_name = kwargs.get("display_name", "张三")
    user.company = kwargs.get("company", None)
    user.contact_person = kwargs.get("contact_person", None)
    user.phone = kwargs.get("phone", None)
    user.can_search_jobs = kwargs.get("can_search_jobs", True)
    user.can_search_workers = kwargs.get("can_search_workers", False)
    user.last_active_at = kwargs.get("last_active_at", None)
    return user


class TestIdentifyOrRegister:
    def test_new_user_auto_registers_as_worker(self):
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None
        ctx = identify_or_register("new_user", db)
        assert ctx.role == "worker"
        assert ctx.is_first_touch is True
        assert ctx.should_welcome is True
        db.add.assert_called_once()

    def test_existing_worker_no_welcome(self):
        user = _make_user_mock(last_active_at=datetime(2026, 1, 1))
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user
        ctx = identify_or_register("u1", db)
        assert ctx.role == "worker"
        assert ctx.should_welcome is False
        assert ctx.is_first_touch is False

    def test_factory_first_touch_welcome(self):
        user = _make_user_mock(role="factory", last_active_at=None, company="XX电子厂")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user
        ctx = identify_or_register("u1", db)
        assert ctx.role == "factory"
        assert ctx.is_first_touch is True
        assert ctx.should_welcome is True

    def test_factory_returning_no_welcome(self):
        user = _make_user_mock(role="factory", last_active_at=datetime(2026, 1, 1))
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user
        ctx = identify_or_register("u1", db)
        assert ctx.should_welcome is False

    def test_blocked_user_returns_blocked_status(self):
        user = _make_user_mock(status="blocked")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user
        ctx = identify_or_register("u1", db)
        assert ctx.status == "blocked"

    def test_deleted_user_returns_deleted_status(self):
        user = _make_user_mock(status="deleted")
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = user
        ctx = identify_or_register("u1", db)
        assert ctx.status == "deleted"


class TestCheckUserStatus:
    def test_active_allowed(self):
        ctx = UserContext(
            external_userid="u1", role="worker", status="active",
            display_name=None, company=None, contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=False,
            is_first_touch=False, should_welcome=False,
        )
        assert check_user_status(ctx) is None

    def test_blocked(self):
        ctx = UserContext(
            external_userid="u1", role="worker", status="blocked",
            display_name=None, company=None, contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=False,
            is_first_touch=False, should_welcome=False,
        )
        msg = check_user_status(ctx)
        assert "限制使用" in msg

    def test_deleted(self):
        ctx = UserContext(
            external_userid="u1", role="worker", status="deleted",
            display_name=None, company=None, contact_person=None, phone=None,
            can_search_jobs=True, can_search_workers=False,
            is_first_touch=False, should_welcome=False,
        )
        msg = check_user_status(ctx)
        assert "删除状态" in msg


class TestDeleteUserData:
    @patch("app.services.user_service.conversation_service")
    def test_delete_flow(self, mock_conv):
        db = MagicMock()
        # Mock query chains
        db.query.return_value.filter.return_value.update.return_value = 1

        reply = delete_user_data("u1", db)
        assert "删除" in reply
        mock_conv.clear_session.assert_called_once_with("u1")
        # Verify db.add was called for conversation_log and audit_log
        assert db.add.call_count >= 2
