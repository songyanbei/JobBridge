"""账号管理路由（Phase 5 模块 D）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, Query, UploadFile
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.exceptions import BusinessException
from app.core.responses import ok, paged
from app.models import AdminUser
from app.schemas.account import (
    BlockRequest,
    BrokerCreate,
    BrokerUpdate,
    FactoryCreate,
    FactoryUpdate,
    UnblockRequest,
    UserAdminRead,
)
from app.services import account_service

router = APIRouter(prefix="/admin/accounts", tags=["admin-accounts"])

_MAX_IMPORT_FILE_BYTES = 2 * 1024 * 1024  # 2MB


def _serialize_user(user) -> dict:
    return UserAdminRead.model_validate(user).model_dump(mode="json")


# ---------------------------------------------------------------------------
# 厂家
# ---------------------------------------------------------------------------

@router.get("/factories", summary="厂家列表")
def list_factories(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows, total = account_service.list_users(db, "factory", page=page, size=size, keyword=keyword, status=status)
    return paged([_serialize_user(u) for u in rows], total, page, size)


@router.post("/factories", summary="厂家预注册")
def create_factory(
    req: FactoryCreate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    payload = req.model_dump()
    payload.update({"can_search_jobs": False, "can_search_workers": True})
    user = account_service.pre_register(db, "factory", payload, current.username)
    db.commit()
    db.refresh(user)
    return ok(_serialize_user(user))


@router.get("/factories/{userid}", summary="厂家详情")
def get_factory(
    userid: str,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    user = account_service.get_user(db, userid)
    if user.role != "factory":
        raise BusinessException(40101, "用户不是厂家")
    return ok(_serialize_user(user))


@router.put("/factories/{userid}", summary="厂家编辑")
def update_factory(
    userid: str,
    req: FactoryUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    user = account_service.get_user(db, userid)
    if user.role != "factory":
        raise BusinessException(40101, "用户不是厂家")
    updated = account_service.update_user(db, userid, req.model_dump(exclude_none=True), current.username)
    return ok(_serialize_user(updated))


@router.post("/factories/import", summary="厂家 Excel 批量导入")
async def import_factories(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise BusinessException(40101, "仅支持 .xlsx 文件")
    raw = await file.read()
    if len(raw) > _MAX_IMPORT_FILE_BYTES:
        raise BusinessException(40101, "文件大小超过 2MB 限制")
    result = account_service.import_excel(db, "factory", raw, current.username)
    return ok(result)


# ---------------------------------------------------------------------------
# 中介
# ---------------------------------------------------------------------------

@router.get("/brokers", summary="中介列表")
def list_brokers(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows, total = account_service.list_users(db, "broker", page=page, size=size, keyword=keyword, status=status)
    return paged([_serialize_user(u) for u in rows], total, page, size)


@router.post("/brokers", summary="中介预注册")
def create_broker(
    req: BrokerCreate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    user = account_service.pre_register(db, "broker", req.model_dump(), current.username)
    db.commit()
    db.refresh(user)
    return ok(_serialize_user(user))


@router.get("/brokers/{userid}", summary="中介详情")
def get_broker(
    userid: str,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    user = account_service.get_user(db, userid)
    if user.role != "broker":
        raise BusinessException(40101, "用户不是中介")
    return ok(_serialize_user(user))


@router.put("/brokers/{userid}", summary="中介编辑")
def update_broker(
    userid: str,
    req: BrokerUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    user = account_service.get_user(db, userid)
    if user.role != "broker":
        raise BusinessException(40101, "用户不是中介")
    updated = account_service.update_user(db, userid, req.model_dump(exclude_none=True), current.username)
    return ok(_serialize_user(updated))


@router.post("/brokers/import", summary="中介 Excel 批量导入")
async def import_brokers(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    if not file.filename or not file.filename.lower().endswith(".xlsx"):
        raise BusinessException(40101, "仅支持 .xlsx 文件")
    raw = await file.read()
    if len(raw) > _MAX_IMPORT_FILE_BYTES:
        raise BusinessException(40101, "文件大小超过 2MB 限制")
    result = account_service.import_excel(db, "broker", raw, current.username)
    return ok(result)


# ---------------------------------------------------------------------------
# 工人（只读）
# ---------------------------------------------------------------------------

@router.get("/workers", summary="工人列表（只读）")
def list_workers(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    status: str | None = None,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows, total = account_service.list_users(db, "worker", page=page, size=size, keyword=keyword, status=status)
    return paged([_serialize_user(u) for u in rows], total, page, size)


@router.get("/workers/{userid}", summary="工人详情（只读）")
def get_worker(
    userid: str,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    user = account_service.get_user(db, userid)
    if user.role != "worker":
        raise BusinessException(40101, "用户不是工人")
    return ok(_serialize_user(user))


# ---------------------------------------------------------------------------
# 黑名单
# ---------------------------------------------------------------------------

@router.get("/blacklist", summary="黑名单列表")
def list_blacklist(
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    keyword: str | None = None,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows, total = account_service.list_blacklist(db, page=page, size=size, keyword=keyword)
    return paged([_serialize_user(u) for u in rows], total, page, size)


@router.post("/{userid}/block", summary="封禁用户")
def block(
    userid: str,
    req: BlockRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    account_service.block_user(db, userid, req.reason, current.username)
    return ok()


@router.post("/{userid}/unblock", summary="解封用户")
def unblock(
    userid: str,
    req: UnblockRequest,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    account_service.unblock_user(db, userid, req.reason, current.username)
    return ok()
