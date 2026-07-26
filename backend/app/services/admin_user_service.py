"""运营管理员账号 service（Phase 5）。

只做用户查询 / 密码字段更新 / 角色治理；不直接颁发 token、不直接接触 Redis。
登录编排 + 失败计数在 `api/admin/auth.py` 路由层完成，保持 service 纯粹。

角色矩阵见方案 §9.10 与 §14.8：viewer 只能查看和模拟，operator 额外可以创建和
编辑草稿，只有 super_admin 可以发布、调灰度、全量、回滚和切总开关。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Iterable

from sqlalchemy.orm import Session

from app.core.security import hash_password
from app.models import AdminUser

# 顺序即权限强弱，供 `role_at_least` 使用。
ADMIN_ROLES: tuple[str, ...] = ("viewer", "operator", "super_admin")

#: 缺省角色。§9.10 的存量账号由迁移显式回填 super_admin，因此代码侧一律按最小
#: 权限兜底——角色缺失/损坏时绝不能 fail-open 成 super_admin。
DEFAULT_ADMIN_ROLE = "viewer"

#: §9.10 + §2.8 权限矩阵。key 为能力，value 为允许的角色集合。
ROLE_PERMISSIONS: dict[str, tuple[str, ...]] = {
    "strategy_view": ("viewer", "operator", "super_admin"),
    "strategy_simulate": ("viewer", "operator", "super_admin"),
    "strategy_draft_edit": ("operator", "super_admin"),
    "strategy_publish": ("super_admin",),
    "strategy_rollout": ("super_admin",),
    "strategy_promote": ("super_admin",),
    "strategy_rollback": ("super_admin",),
    "strategy_kill_switch": ("super_admin",),
    "admin_role_manage": ("super_admin",),
}


def normalize_role(raw: Any) -> str:
    """把任意输入收敛到合法角色；不认识的一律降到最小权限。"""
    if isinstance(raw, str):
        value = raw.strip().lower()
        if value in ADMIN_ROLES:
            return value
    return DEFAULT_ADMIN_ROLE


def resolve_role(admin: Any) -> str:
    """读取管理员实际角色。

    ``getattr(admin, "role", "super_admin")`` 这类写法是 fail-open：字段缺失、
    值损坏或对象是 mock 时都会静默提权成超级管理员，让所有
    ``require_admin_role`` 声明恒真。本函数改为 fail-closed。
    """
    return normalize_role(getattr(admin, "role", None))


def role_at_least(admin: Any, minimum: str) -> bool:
    """按 ADMIN_ROLES 顺序比较权限强弱。"""
    try:
        return ADMIN_ROLES.index(resolve_role(admin)) >= ADMIN_ROLES.index(normalize_role(minimum))
    except ValueError:  # pragma: no cover - normalize_role 已经保证合法
        return False


def has_permission(admin: Any, permission: str) -> bool:
    allowed = ROLE_PERMISSIONS.get(permission)
    if not allowed:
        return False
    return resolve_role(admin) in allowed


def permissions_for(role: Any) -> list[str]:
    """当前角色可用的能力清单，供后台按钮显隐使用（不替代服务端校验）。"""
    normalized = normalize_role(role)
    return sorted(name for name, roles in ROLE_PERMISSIONS.items() if normalized in roles)


def get_by_username(db: Session, username: str) -> AdminUser | None:
    return db.query(AdminUser).filter(AdminUser.username == username).first()


def get_by_id(db: Session, admin_id: int) -> AdminUser | None:
    return db.query(AdminUser).filter(AdminUser.id == admin_id).first()


def list_admins(db: Session) -> list[AdminUser]:
    return db.query(AdminUser).order_by(AdminUser.id.asc()).all()


def touch_login(db: Session, admin: AdminUser) -> None:
    """更新 last_login_at。调用方负责 commit。"""
    admin.last_login_at = datetime.now()


def change_password(db: Session, admin: AdminUser, new_plain: str) -> None:
    """更新密码并置 password_changed=1。调用方负责 commit。"""
    admin.password_hash = hash_password(new_plain)
    admin.password_changed = 1


def create_admin(
    db: Session,
    *,
    username: str,
    password: str,
    role: str,
    display_name: str | None = None,
    enabled: bool = True,
) -> AdminUser:
    """创建管理员，角色必须显式指定（§14.8）。调用方负责 commit。"""
    if role not in ADMIN_ROLES:
        raise ValueError("invalid admin role")
    if get_by_username(db, username):
        raise ValueError("admin username already exists")
    row = AdminUser(
        username=username,
        password_hash=hash_password(password),
        display_name=display_name,
        role=role,
        # 新账号一律按"未改初始密码"处理，沿用 require_admin_password_changed 门禁。
        password_changed=0,
        enabled=1 if enabled else 0,
    )
    db.add(row)
    db.flush()
    return row


def set_role(db: Session, admin: AdminUser, role: str) -> str:
    """修改管理员角色，返回旧角色。调用方负责 commit 与审计。"""
    if role not in ADMIN_ROLES:
        raise ValueError("invalid admin role")
    previous = resolve_role(admin)
    admin.role = role
    db.flush()
    return previous


def count_enabled_super_admins(db: Session, *, exclude_id: int | None = None) -> int:
    """统计仍然可用的 super_admin，用于避免把最后一个超级管理员降权锁死系统。"""
    query = db.query(AdminUser).filter(
        AdminUser.role == "super_admin",
        AdminUser.enabled == 1,
    )
    if exclude_id is not None:
        query = query.filter(AdminUser.id != exclude_id)
    return query.count()


def admin_summary(admin: AdminUser) -> dict[str, Any]:
    """后台可见的管理员摘要（不含密码哈希）。"""
    role = resolve_role(admin)
    return {
        "id": admin.id,
        "username": admin.username,
        "display_name": admin.display_name,
        "role": role,
        "permissions": permissions_for(role),
        "enabled": bool(admin.enabled),
        "password_changed": bool(admin.password_changed),
        "last_login_at": admin.last_login_at,
        "created_at": admin.created_at,
    }


def summarize_all(admins: Iterable[AdminUser]) -> list[dict[str, Any]]:
    return [admin_summary(item) for item in admins]
