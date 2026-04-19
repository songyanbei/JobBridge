"""Phase 7 codex rev2 P1：admin/auth.py 默认/弱口令拦截单元测试。

覆盖：
- 登录时 supplied password 命中 ``ADMIN_DEFAULT_PASSWORDS`` → 即便
  ``password_changed=1`` 也强制重置为 0 并 commit；token 仍发放，
  返回体 ``password_changed=False``
- 登录时未命中 → 不修改 ``password_changed`` 字段
- 登录时 ``password_changed=0`` 命中默认口令 → 字段维持 0，不抛异常
- 改密时 new_password 命中黑名单 → 40101，不写库
- 改密时 new_password 不在黑名单 → 走 admin_user_service.change_password
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from app.api.admin import auth as admin_auth
from app.core.exceptions import BusinessException
from app.schemas.admin import AdminLogin, ChangePasswordRequest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _make_admin(password_changed: int, hash_value: str = "stub-hash"):
    a = MagicMock()
    a.id = 1
    a.username = "admin"
    a.password_hash = hash_value
    a.password_changed = password_changed
    a.enabled = 1
    return a


@pytest.fixture
def force_default_set(monkeypatch):
    """统一注入 admin_default_passwords，避免依赖默认值。"""
    monkeypatch.setattr(
        admin_auth.settings, "admin_default_passwords", "admin123,Pa$$w0rd"
    )
    return None


# ---------------------------------------------------------------------------
# login(): 默认口令命中重置 password_changed
# ---------------------------------------------------------------------------

class TestLoginDefaultPasswordDetection:
    def test_password_changed_true_but_default_password_force_reset(
        self, force_default_set, monkeypatch,
    ):
        """字段已置 1 但口令仍是 admin123 → 强制重置 0，下一步业务接口被门禁拦截。"""
        admin = _make_admin(password_changed=1)
        db = MagicMock()
        monkeypatch.setattr(admin_auth, "get_admin_login_fail", lambda *_: 0)
        monkeypatch.setattr(admin_auth, "incr_admin_login_fail", lambda *_: None)
        monkeypatch.setattr(admin_auth, "clear_admin_login_fail", lambda *_: None)
        monkeypatch.setattr(admin_auth, "verify_password", lambda p, h: p == "admin123")
        monkeypatch.setattr(
            admin_auth.admin_user_service, "get_by_username", lambda _db, _u: admin
        )
        monkeypatch.setattr(admin_auth.admin_user_service, "touch_login", lambda *_: None)
        monkeypatch.setattr(admin_auth, "create_admin_token",
                            lambda *_: ("tok", MagicMock(isoformat=lambda: "2026-01-01T00:00:00")))

        result = admin_auth.login(req=AdminLogin(username="admin", password="admin123"), db=db)

        # 关键断言：内存中的字段被改写
        assert admin.password_changed == 0
        # 必须 commit，否则重启就丢
        db.commit.assert_called()
        # 返回体里 password_changed 必须为 False，让前端跳改密页
        body = result["data"] if "data" in result else result
        assert body["password_changed"] is False

    def test_normal_password_does_not_reset_field(
        self, force_default_set, monkeypatch,
    ):
        """非默认口令登录 → 字段保持原状，不动数据。"""
        admin = _make_admin(password_changed=1)
        db = MagicMock()
        monkeypatch.setattr(admin_auth, "get_admin_login_fail", lambda *_: 0)
        monkeypatch.setattr(admin_auth, "incr_admin_login_fail", lambda *_: None)
        monkeypatch.setattr(admin_auth, "clear_admin_login_fail", lambda *_: None)
        monkeypatch.setattr(admin_auth, "verify_password", lambda p, h: p == "MyStrongPass!42")
        monkeypatch.setattr(
            admin_auth.admin_user_service, "get_by_username", lambda _db, _u: admin
        )
        monkeypatch.setattr(admin_auth.admin_user_service, "touch_login", lambda *_: None)
        monkeypatch.setattr(admin_auth, "create_admin_token",
                            lambda *_: ("tok", MagicMock(isoformat=lambda: "2026-01-01T00:00:00")))

        admin_auth.login(req=AdminLogin(username="admin", password="MyStrongPass!42"), db=db)

        assert admin.password_changed == 1  # 不动

    def test_password_changed_false_with_default_password_stays_false(
        self, force_default_set, monkeypatch,
    ):
        """字段已经是 0 + 命中默认口令：登录正常发 token，字段保持 0；后续业务接口被门禁拦截。"""
        admin = _make_admin(password_changed=0)
        db = MagicMock()
        monkeypatch.setattr(admin_auth, "get_admin_login_fail", lambda *_: 0)
        monkeypatch.setattr(admin_auth, "incr_admin_login_fail", lambda *_: None)
        monkeypatch.setattr(admin_auth, "clear_admin_login_fail", lambda *_: None)
        monkeypatch.setattr(admin_auth, "verify_password", lambda p, h: p == "admin123")
        monkeypatch.setattr(
            admin_auth.admin_user_service, "get_by_username", lambda _db, _u: admin
        )
        monkeypatch.setattr(admin_auth.admin_user_service, "touch_login", lambda *_: None)
        monkeypatch.setattr(admin_auth, "create_admin_token",
                            lambda *_: ("tok", MagicMock(isoformat=lambda: "2026-01-01T00:00:00")))

        result = admin_auth.login(req=AdminLogin(username="admin", password="admin123"), db=db)
        assert admin.password_changed == 0
        body = result["data"] if "data" in result else result
        assert body["password_changed"] is False

    def test_empty_default_set_disables_check(self, monkeypatch):
        """ADMIN_DEFAULT_PASSWORDS=""时不做任何检测，保留原 password_changed。"""
        monkeypatch.setattr(admin_auth.settings, "admin_default_passwords", "")
        admin = _make_admin(password_changed=1)
        db = MagicMock()
        monkeypatch.setattr(admin_auth, "get_admin_login_fail", lambda *_: 0)
        monkeypatch.setattr(admin_auth, "incr_admin_login_fail", lambda *_: None)
        monkeypatch.setattr(admin_auth, "clear_admin_login_fail", lambda *_: None)
        monkeypatch.setattr(admin_auth, "verify_password", lambda p, h: p == "admin123")
        monkeypatch.setattr(
            admin_auth.admin_user_service, "get_by_username", lambda _db, _u: admin
        )
        monkeypatch.setattr(admin_auth.admin_user_service, "touch_login", lambda *_: None)
        monkeypatch.setattr(admin_auth, "create_admin_token",
                            lambda *_: ("tok", MagicMock(isoformat=lambda: "2026-01-01T00:00:00")))

        admin_auth.login(req=AdminLogin(username="admin", password="admin123"), db=db)

        # 空集合时即便口令是 admin123 也不重置（运营显式关闭了该机制）
        assert admin.password_changed == 1


# ---------------------------------------------------------------------------
# change_password(): 拒绝把新密码改成黑名单中的任何一个
# ---------------------------------------------------------------------------

class TestChangePasswordBlocksDefault:
    def test_new_password_in_default_set_rejected(self, force_default_set):
        """new_password 命中黑名单 → 40101，且不写库。"""
        admin = _make_admin(password_changed=1)
        db = MagicMock()
        with patch.object(admin_auth, "verify_password", return_value=True), \
             patch.object(admin_auth.admin_user_service, "change_password") as mock_change:
            with pytest.raises(BusinessException) as exc_info:
                admin_auth.change_password(
                    req=ChangePasswordRequest(
                        old_password="OldStrong!42",
                        new_password="admin123",
                    ),
                    current=admin,
                    db=db,
                )
            assert exc_info.value.code == 40101
            assert "默认" in exc_info.value.message or "弱口令" in exc_info.value.message
            mock_change.assert_not_called()
            db.commit.assert_not_called()

    def test_other_blacklisted_password_rejected(self, force_default_set):
        """黑名单第二项也要被拦截，不只是 admin123。"""
        admin = _make_admin(password_changed=1)
        db = MagicMock()
        with patch.object(admin_auth, "verify_password", return_value=True), \
             patch.object(admin_auth.admin_user_service, "change_password") as mock_change:
            with pytest.raises(BusinessException) as exc_info:
                admin_auth.change_password(
                    req=ChangePasswordRequest(
                        old_password="OldStrong!42",
                        new_password="Pa$$w0rd",  # force_default_set 把它放进了黑名单
                    ),
                    current=admin,
                    db=db,
                )
            assert exc_info.value.code == 40101
            mock_change.assert_not_called()

    def test_strong_password_passes(self, force_default_set):
        """非黑名单 + 长度足够 + 与旧密码不同 → 走 service。"""
        admin = _make_admin(password_changed=1)
        db = MagicMock()
        with patch.object(admin_auth, "verify_password", return_value=True), \
             patch.object(admin_auth.admin_user_service, "change_password") as mock_change:
            admin_auth.change_password(
                req=ChangePasswordRequest(
                    old_password="OldStrong!42",
                    new_password="NewStrong!89",
                ),
                current=admin,
                db=db,
            )
            mock_change.assert_called_once_with(db, admin, "NewStrong!89")
            db.commit.assert_called_once()
