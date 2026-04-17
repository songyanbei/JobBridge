"""运营后台相关 DTO。"""
from datetime import datetime

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# 管理员认证
# ---------------------------------------------------------------------------

class AdminLogin(BaseModel):
    """管理员登录请求。"""
    username: str = Field(..., max_length=32, description="登录用户名", examples=["admin"])
    password: str = Field(..., min_length=6, description="明文密码，服务端 bcrypt 校验", examples=["admin123"])


class AdminToken(BaseModel):
    """登录成功后返回的 Token。"""
    access_token: str = Field(..., description="JWT Access Token，有效期见 expires_at")
    token_type: str = Field(default="bearer", description="固定 'bearer'")
    expires_at: datetime | None = Field(default=None, description="Token 过期时间 ISO 8601")
    password_changed: bool = Field(default=False, description="是否已修改初始密码，前端据此决定是否强制改密")


class ChangePasswordRequest(BaseModel):
    """修改管理员密码。"""
    old_password: str = Field(..., min_length=1, max_length=64, description="旧密码明文")
    new_password: str = Field(
        ..., min_length=8, max_length=64,
        description="新密码明文，长度 ≥ 8，必须与旧密码不同",
    )


class AdminUserRead(BaseModel):
    """管理员用户输出 DTO（不含密码哈希）。"""
    id: int = Field(..., description="管理员数值 ID")
    username: str = Field(..., description="登录用户名")
    display_name: str | None = Field(default=None, description="显示名")
    password_changed: bool = Field(default=False, description="是否已修改初始密码")
    enabled: bool = Field(default=True, description="账号是否启用")
    last_login_at: datetime | None = Field(default=None, description="最近登录时间")
    created_at: datetime = Field(..., description="账号创建时间")

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# 系统配置
# ---------------------------------------------------------------------------

class SystemConfigRead(BaseModel):
    """系统配置输出 DTO。"""
    config_key: str = Field(..., description="配置键，例如 `rate_limit.window_seconds`")
    config_value: str = Field(..., description="配置值（以字符串保存，按 value_type 解析）")
    value_type: str = Field(default="string", description="值类型：string / int / bool / json")
    description: str | None = Field(default=None, description="配置说明")
    updated_at: datetime = Field(..., description="最近更新时间")
    updated_by: str | None = Field(default=None, description="最近更新人 username")

    model_config = {"from_attributes": True}


class SystemConfigUpdate(BaseModel):
    """更新系统配置。"""
    config_value: str = Field(..., description="新值，按 value_type 校验；bool 接受 'true'/'false'/'1'/'0'")
    value_type: str | None = Field(
        default=None,
        description="可选：显式指定 value_type 以变更类型（string/int/bool/json）",
    )


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
