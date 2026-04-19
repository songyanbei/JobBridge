"""``require_admin_password_changed`` 依赖单元测试。

Phase 7 上线 checklist 要求强制首登改密。本依赖在 ``require_admin`` 基础上
叠加门禁，让默认 admin/admin123 即便登录成功也不能调业务接口。

覆盖：
- 已改密 → 放行
- 未改密 + ADMIN_FORCE_PASSWORD_CHANGE=true → 40301
- 未改密 + ADMIN_FORCE_PASSWORD_CHANGE=false → 放行（开发模式逃生口）
- password_changed 字段为 0 / None / "" / 1 的边界
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from app.api import deps
from app.core.exceptions import BusinessException


def _admin(password_changed):
    """生成一个 enabled=1 的假 admin。"""
    a = MagicMock()
    a.password_changed = password_changed
    a.enabled = 1
    return a


class TestRequireAdminPasswordChanged:
    def test_changed_passes(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "admin_force_password_change", True)
        admin = _admin(password_changed=1)
        result = deps.require_admin_password_changed(current=admin)
        assert result is admin

    def test_unchanged_with_force_on_rejects(self, monkeypatch):
        monkeypatch.setattr(deps.settings, "admin_force_password_change", True)
        admin = _admin(password_changed=0)
        with pytest.raises(BusinessException) as exc_info:
            deps.require_admin_password_changed(current=admin)
        assert exc_info.value.code == 40301
        assert "默认密码" in exc_info.value.message

    def test_unchanged_with_force_off_passes(self, monkeypatch):
        """开发场景手动关闭 ADMIN_FORCE_PASSWORD_CHANGE 时不应被阻断。"""
        monkeypatch.setattr(deps.settings, "admin_force_password_change", False)
        admin = _admin(password_changed=0)
        result = deps.require_admin_password_changed(current=admin)
        assert result is admin

    @pytest.mark.parametrize("falsy_value", [0, None, ""])
    def test_falsy_password_changed_treated_as_unchanged(self, monkeypatch, falsy_value):
        monkeypatch.setattr(deps.settings, "admin_force_password_change", True)
        admin = _admin(password_changed=falsy_value)
        with pytest.raises(BusinessException) as exc_info:
            deps.require_admin_password_changed(current=admin)
        assert exc_info.value.code == 40301

    @pytest.mark.parametrize("truthy_value", [1, True, "1"])
    def test_truthy_password_changed_passes(self, monkeypatch, truthy_value):
        monkeypatch.setattr(deps.settings, "admin_force_password_change", True)
        admin = _admin(password_changed=truthy_value)
        # 不应抛异常
        deps.require_admin_password_changed(current=admin)
