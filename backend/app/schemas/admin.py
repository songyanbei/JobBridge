"""运营后台相关 DTO。"""
from datetime import datetime

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 管理员认证
# ---------------------------------------------------------------------------

class AdminLogin(BaseModel):
    """管理员登录请求。"""
    username: str = Field(..., max_length=32)
    password: str = Field(..., min_length=6)


class AdminToken(BaseModel):
    """登录成功后返回的 Token。"""
    access_token: str
    token_type: str = "bearer"
    expires_at: datetime | None = None
    password_changed: bool = False


class ChangePasswordRequest(BaseModel):
    """修改管理员密码。"""
    old_password: str = Field(..., min_length=1, max_length=64)
    new_password: str = Field(..., min_length=8, max_length=64)


class AdminUserRead(BaseModel):
    """管理员用户输出 DTO（不含密码哈希）。"""
    id: int
    username: str
    display_name: str | None = None
    password_changed: bool = False
    enabled: bool = True
    last_login_at: datetime | None = None
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 系统配置
# ---------------------------------------------------------------------------

class SystemConfigRead(BaseModel):
    """系统配置输出 DTO。"""
    config_key: str
    config_value: str
    value_type: str = "string"
    description: str | None = None
    updated_at: datetime
    updated_by: str | None = None

    model_config = {"from_attributes": True}


class SystemConfigUpdate(BaseModel):
    """更新系统配置。"""
    config_value: str
    value_type: str | None = None


# ---------------------------------------------------------------------------
# 审核日志
# ---------------------------------------------------------------------------

class AuditLogRead(BaseModel):
    """审核日志输出 DTO。"""
    id: int
    target_type: str
    target_id: str
    action: str
    reason: str | None = None
    operator: str | None = None
    snapshot: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
