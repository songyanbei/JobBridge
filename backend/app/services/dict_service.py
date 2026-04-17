"""字典管理 service（Phase 5 模块 F）。"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.exceptions import BusinessException
from app.models import DictCity, DictJobCategory, DictSensitiveWord, Job
from app.services.admin_log_service import _json_safe, write_admin_log


# ---------------------------------------------------------------------------
# 城市
# ---------------------------------------------------------------------------

def list_cities_grouped(
    db: Session,
    keyword: str | None = None,
    include_disabled: bool = True,
) -> list[dict]:
    """按省份分组返回城市字典。

    默认包含禁用城市（admin 场景需看到以便重新启用），enabled 字段透出由前端筛选。
    如需仅返回启用项，传 include_disabled=False。
    """
    query = db.query(DictCity)
    if not include_disabled:
        query = query.filter(DictCity.enabled == 1)
    if keyword:
        k = f"%{keyword}%"
        query = query.filter((DictCity.name.ilike(k)) | (DictCity.short_name.ilike(k)))
    rows = query.order_by(DictCity.province, DictCity.name).all()
    grouped: dict[str, list] = {}
    for c in rows:
        grouped.setdefault(c.province, []).append({
            "id": c.id, "code": c.code, "name": c.name,
            "short_name": c.short_name, "province": c.province,
            "aliases": c.aliases or [], "enabled": bool(c.enabled),
        })
    return [{"province": p, "items": items} for p, items in grouped.items()]


def update_city_aliases(db: Session, city_id: int, aliases: list[str], operator: str) -> DictCity:
    city = db.query(DictCity).filter(DictCity.id == city_id).first()
    if not city:
        raise BusinessException(40401, "城市不存在")
    before = {"aliases": city.aliases or []}
    city.aliases = [str(a).strip() for a in aliases if str(a).strip()]
    write_admin_log(
        db,
        target_type="user", target_id=f"dict_city:{city.code}",
        action="manual_edit", operator=operator,
        before=before, after={"aliases": city.aliases},
        reason="dict_city.update_aliases",
    )
    db.commit()
    db.refresh(city)
    return city


# ---------------------------------------------------------------------------
# 工种
# ---------------------------------------------------------------------------

def list_job_categories(db: Session) -> list[DictJobCategory]:
    return db.query(DictJobCategory).order_by(DictJobCategory.sort_order, DictJobCategory.id).all()


def create_job_category(db: Session, payload: dict, operator: str) -> DictJobCategory:
    code = payload.get("code")
    name = payload.get("name")
    if not code or not name:
        raise BusinessException(40101, "code / name 必填")
    if db.query(DictJobCategory).filter(DictJobCategory.code == code).first():
        raise BusinessException(40904, "工种 code 已存在")
    if db.query(DictJobCategory).filter(DictJobCategory.name == name).first():
        raise BusinessException(40904, "工种 name 已存在")

    cat = DictJobCategory(
        code=code, name=name,
        aliases=payload.get("aliases") or [],
        sort_order=payload.get("sort_order") or 0,
        enabled=1,
    )
    db.add(cat)
    db.flush()
    write_admin_log(
        db,
        target_type="user", target_id=f"dict_job_category:{cat.id}",
        action="reinstate", operator=operator,
        before=None, after=_snap_cat(cat), reason="dict_job_category.create",
    )
    db.commit()
    db.refresh(cat)
    return cat


def _snap_cat(cat: DictJobCategory) -> dict:
    return {
        "code": cat.code, "name": cat.name,
        "aliases": cat.aliases, "sort_order": cat.sort_order,
        "enabled": bool(cat.enabled),
    }


def update_job_category(db: Session, cat_id: int, payload: dict, operator: str) -> DictJobCategory:
    cat = db.query(DictJobCategory).filter(DictJobCategory.id == cat_id).first()
    if not cat:
        raise BusinessException(40401, "工种不存在")

    new_name = payload.get("name")
    if new_name and new_name != cat.name:
        dup = db.query(DictJobCategory).filter(
            DictJobCategory.name == new_name, DictJobCategory.id != cat_id,
        ).first()
        if dup:
            raise BusinessException(40904, "工种 name 已存在")

    before = _snap_cat(cat)
    if "name" in payload and payload["name"] is not None:
        cat.name = payload["name"]
    if "aliases" in payload and payload["aliases"] is not None:
        cat.aliases = payload["aliases"]
    if "sort_order" in payload and payload["sort_order"] is not None:
        cat.sort_order = payload["sort_order"]
    if "enabled" in payload and payload["enabled"] is not None:
        cat.enabled = 1 if payload["enabled"] else 0

    write_admin_log(
        db,
        target_type="user", target_id=f"dict_job_category:{cat.id}",
        action="manual_edit", operator=operator,
        before=before, after=_snap_cat(cat), reason="dict_job_category.update",
    )
    db.commit()
    db.refresh(cat)
    return cat


def delete_job_category(db: Session, cat_id: int, operator: str) -> None:
    cat = db.query(DictJobCategory).filter(DictJobCategory.id == cat_id).first()
    if not cat:
        raise BusinessException(40401, "工种不存在")
    # 引用检查
    referenced = db.query(Job).filter(Job.job_category == cat.name).first()
    if referenced:
        raise BusinessException(40904, "工种正在被岗位引用，无法删除")

    before = _snap_cat(cat)
    db.delete(cat)
    write_admin_log(
        db,
        target_type="user", target_id=f"dict_job_category:{cat_id}",
        action="manual_reject", operator=operator,
        before=before, after=None, reason="dict_job_category.delete",
    )
    db.commit()


# ---------------------------------------------------------------------------
# 敏感词
# ---------------------------------------------------------------------------

def list_sensitive_words(
    db: Session,
    page: int = 1,
    size: int = 50,
    level: str | None = None,
    keyword: str | None = None,
) -> tuple[list[DictSensitiveWord], int]:
    page = max(1, page)
    size = max(1, min(size, 200))
    query = db.query(DictSensitiveWord)
    if level:
        query = query.filter(DictSensitiveWord.level == level)
    if keyword:
        query = query.filter(DictSensitiveWord.word.ilike(f"%{keyword}%"))
    total = query.count()
    rows = query.order_by(DictSensitiveWord.id.desc()).offset((page - 1) * size).limit(size).all()
    return rows, total


def add_sensitive_word(db: Session, word: str, level: str, category: str | None, operator: str) -> DictSensitiveWord:
    if not word or not word.strip():
        raise BusinessException(40101, "word 不能为空")
    word = word.strip()
    if db.query(DictSensitiveWord).filter(DictSensitiveWord.word == word).first():
        raise BusinessException(40904, "敏感词已存在")
    if level not in ("high", "mid", "low"):
        raise BusinessException(40101, "无效的 level")

    entry = DictSensitiveWord(word=word, level=level, category=category, enabled=1)
    db.add(entry)
    db.flush()
    write_admin_log(
        db,
        target_type="user", target_id=f"dict_sensitive_word:{entry.id}",
        action="reinstate", operator=operator,
        before=None, after={"word": word, "level": level, "category": category},
        reason="dict_sensitive_word.create",
    )
    db.commit()
    db.refresh(entry)
    return entry


def delete_sensitive_word(db: Session, word_id: int, operator: str) -> None:
    entry = db.query(DictSensitiveWord).filter(DictSensitiveWord.id == word_id).first()
    if not entry:
        raise BusinessException(40401, "敏感词不存在")
    before = {"word": entry.word, "level": entry.level, "category": entry.category}
    db.delete(entry)
    write_admin_log(
        db,
        target_type="user", target_id=f"dict_sensitive_word:{word_id}",
        action="manual_reject", operator=operator,
        before=before, after=None, reason="dict_sensitive_word.delete",
    )
    db.commit()


def batch_add_sensitive_words(
    db: Session, words: list[str], level: str, category: str | None, operator: str,
) -> dict:
    if level not in ("high", "mid", "low"):
        raise BusinessException(40101, "无效的 level")

    added: list[str] = []
    duplicated: list[str] = []
    seen: set[str] = set()

    for raw in words:
        w = (raw or "").strip()
        if not w or w in seen:
            continue
        seen.add(w)
        if db.query(DictSensitiveWord).filter(DictSensitiveWord.word == w).first():
            duplicated.append(w)
            continue
        entry = DictSensitiveWord(word=w, level=level, category=category, enabled=1)
        db.add(entry)
        try:
            db.flush()
            added.append(w)
        except Exception:
            # 并发下可能被其它管理员先插入：回滚本次 flush 并计入 duplicated
            db.rollback()
            duplicated.append(w)

    if added:
        write_admin_log(
            db,
            target_type="user", target_id="dict_sensitive_word:batch",
            action="reinstate", operator=operator,
            before=None, after={"added": added, "level": level, "category": category},
            reason="dict_sensitive_word.batch_create",
        )
    db.commit()
    return {"added": len(added), "duplicated": len(duplicated)}
