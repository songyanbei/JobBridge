"""字典管理路由（Phase 5 模块 F）。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.api.deps import get_db, require_admin
from app.core.responses import ok, paged
from app.models import AdminUser
from app.schemas.dict import (
    CityAliasesUpdate,
    CityRead,
    JobCategoryCreate,
    JobCategoryRead,
    JobCategoryUpdate,
    SensitiveWordBatchCreate,
    SensitiveWordCreate,
    SensitiveWordRead,
)
from app.services import dict_service

router = APIRouter(prefix="/admin/dicts", tags=["admin-dicts"])


# ---------------------------------------------------------------------------
# 城市
# ---------------------------------------------------------------------------

@router.get("/cities", summary="城市字典（按省份分组）")
def list_cities(
    keyword: str | None = None,
    include_disabled: bool = True,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    return ok(dict_service.list_cities_grouped(db, keyword, include_disabled))


@router.put("/cities/{city_id}", summary="编辑城市别名")
def update_city(
    city_id: int,
    req: CityAliasesUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    city = dict_service.update_city_aliases(db, city_id, req.aliases, current.username)
    return ok(CityRead.model_validate(city).model_dump(mode="json"))


# ---------------------------------------------------------------------------
# 工种
# ---------------------------------------------------------------------------

@router.get("/job-categories", summary="工种列表")
def list_job_categories(
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows = dict_service.list_job_categories(db)
    return ok([JobCategoryRead.model_validate(r).model_dump(mode="json") for r in rows])


@router.post("/job-categories", summary="新增工种")
def create_job_category(
    req: JobCategoryCreate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    cat = dict_service.create_job_category(db, req.model_dump(), current.username)
    return ok(JobCategoryRead.model_validate(cat).model_dump(mode="json"))


@router.put("/job-categories/{cat_id}", summary="编辑工种")
def update_job_category(
    cat_id: int,
    req: JobCategoryUpdate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    cat = dict_service.update_job_category(db, cat_id, req.model_dump(exclude_none=True), current.username)
    return ok(JobCategoryRead.model_validate(cat).model_dump(mode="json"))


@router.delete("/job-categories/{cat_id}", summary="删除工种")
def delete_job_category(
    cat_id: int,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    dict_service.delete_job_category(db, cat_id, current.username)
    return ok()


# ---------------------------------------------------------------------------
# 敏感词
# ---------------------------------------------------------------------------

@router.get("/sensitive-words", summary="敏感词列表")
def list_sensitive_words(
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    level: str | None = None,
    keyword: str | None = None,
    db: Session = Depends(get_db),
    _: AdminUser = Depends(require_admin),
):
    rows, total = dict_service.list_sensitive_words(db, page=page, size=size, level=level, keyword=keyword)
    return paged(
        [SensitiveWordRead.model_validate(r).model_dump(mode="json") for r in rows],
        total, page, size,
    )


@router.post("/sensitive-words", summary="新增敏感词")
def add_sensitive_word(
    req: SensitiveWordCreate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    entry = dict_service.add_sensitive_word(db, req.word, req.level, req.category, current.username)
    return ok(SensitiveWordRead.model_validate(entry).model_dump(mode="json"))


@router.delete("/sensitive-words/{word_id}", summary="删除敏感词")
def delete_sensitive_word(
    word_id: int,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    dict_service.delete_sensitive_word(db, word_id, current.username)
    return ok()


@router.post("/sensitive-words/batch", summary="批量新增敏感词")
def batch_add_sensitive_words(
    req: SensitiveWordBatchCreate,
    db: Session = Depends(get_db),
    current: AdminUser = Depends(require_admin),
):
    result = dict_service.batch_add_sensitive_words(
        db, req.words, req.level, req.category, current.username,
    )
    return ok(result)
