"""检索服务（Phase 3）。

三步漏斗：硬过滤 → Reranker 重排 → 权限过滤 → 文本格式化。
show_more 复用快照，不重新执行全量检索。

Phase 7：在 LLM 调用处补 loguru 结构化打点（llm_call 事件）。
"""
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.config import settings
from app.core.exceptions import LLMError, LLMParseError, LLMTimeout
from app.llm import get_reranker
from app.llm.base import RerankResult
from app.models import Job, Resume, SystemConfig, User
from app.schemas.conversation import SessionState
from app.services import conversation_service, permission_service
from app.services.user_service import UserContext
from app.tasks.common import log_event

logger = logging.getLogger(__name__)

RERANK_PROMPT_VERSION = "v1"


def _rerank_with_logging(
    query: str,
    candidates: list[dict],
    role: str,
    top_n: int,
    call_site: str,
    user_msg_id: str | None = None,
) -> RerankResult:
    """统一封装 reranker.rerank，附带 loguru 结构化打点。

    Phase 7：``llm_call`` 日志含 input_tokens / output_tokens / user_msg_id，
    便于成本分析、定位单条消息对应的检索链路；``parse_failed`` 回落为空结果以
    保持搜索调用不中断，日志仍反映真实失败类型。
    """
    reranker = get_reranker()
    start = time.perf_counter()
    status = "ok"
    result: RerankResult | None = None
    parse_failed = False
    try:
        result = reranker.rerank(
            query=query,
            candidates=candidates,
            role=role,
            top_n=top_n,
        )
    except LLMTimeout:
        status = "timeout"
        raise
    except LLMParseError as exc:
        # 空结果回落，后续业务按 0 召回处理；不再 raise 以对齐 intent 侧策略。
        # provider 在 raise 前已把 token 挂到 exc.input_tokens / exc.output_tokens，
        # 这里回读到 fallback RerankResult，保证 log_event 记录真实 token 用量。
        status = "parse_failed"
        parse_failed = True
        result = RerankResult(
            ranked_items=[],
            reply_text="",
            raw_response="",
            input_tokens=getattr(exc, "input_tokens", None),
            output_tokens=getattr(exc, "output_tokens", None),
        )
    except LLMError:
        status = "http_error"
        raise
    except Exception:
        # 非 LLMError 家族的意外异常（如 provider 实现 bug、类型错误等）。
        # 单独打 unknown_error 便于日志归因与告警分级。
        status = "unknown_error"
        raise
    finally:
        log_event(
            "llm_call",
            call_site=call_site,
            provider=settings.llm_provider,
            model=settings.llm_reranker_model,
            prompt_version=RERANK_PROMPT_VERSION,
            duration_ms=int((time.perf_counter() - start) * 1000),
            candidate_count=len(candidates),
            top_n=top_n,
            ranked_count=len(result.ranked_items) if result else 0,
            input_tokens=getattr(result, "input_tokens", None),
            output_tokens=getattr(result, "output_tokens", None),
            user_msg_id=user_msg_id,
            status=status,
        )
    return result


@dataclass
class SearchResult:
    reply_text: str
    has_more: bool = False
    result_count: int = 0


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------

def search_jobs(
    criteria: dict,
    raw_query: str,
    session: SessionState,
    user_ctx: UserContext,
    db: Session,
    user_msg_id: str | None = None,
) -> SearchResult:
    """工人/中介找岗位。"""
    top_n = _get_config_int("match.top_n", db, 3)
    max_candidates = _get_config_int("match.max_candidates", db, 50)

    # 硬过滤
    candidates = _query_jobs(criteria, max_candidates, db)

    # 0 召回 → 尝试宽松匹配（一次）
    if 0 < len(candidates) < top_n:
        relaxed = _try_relaxed_job_search(criteria, max_candidates, db)
        if len(relaxed) > len(candidates):
            candidates = relaxed

    if not candidates:
        return SearchResult(
            reply_text="暂未找到完全匹配的岗位，建议调整条件重新搜索。比如放宽城市或薪资要求。",
            result_count=0,
        )

    # 转为 dict 列表用于 rerank
    candidate_dicts = _jobs_to_dicts(candidates, db)

    # Reranker（含结构化打点）
    rerank_result = _rerank_with_logging(
        query=raw_query,
        candidates=candidate_dicts,
        role=user_ctx.role,
        top_n=top_n,
        call_site="search_jobs",
        user_msg_id=user_msg_id,
    )

    # 从 rerank 结果提取排序后的 ID 列表（全量快照）
    ranked_ids = [str(item["id"]) for item in rerank_result.ranked_items]
    # 如果 rerank 只返回了 top_n，把剩余候选补到后面
    ranked_id_set = set(ranked_ids)
    for c in candidate_dicts:
        cid = str(c["id"])
        if cid not in ranked_id_set:
            ranked_ids.append(cid)

    # 保存快照
    digest = conversation_service.compute_query_digest(criteria)
    conversation_service.save_snapshot(session, ranked_ids, digest)

    # 取首批
    first_batch_ids = conversation_service.get_next_candidate_ids(session, top_n)
    if not first_batch_ids:
        return SearchResult(reply_text="暂无匹配结果。", result_count=0)

    # 从候选中找到对应记录
    id_to_dict = {str(c["id"]): c for c in candidate_dicts}
    batch = [id_to_dict[cid] for cid in first_batch_ids if cid in id_to_dict]

    # 权限过滤
    filtered = permission_service.filter_jobs_batch(batch, user_ctx.role)

    # 记录已展示
    shown_ids = [str(j["id"]) for j in batch]
    conversation_service.record_shown(session, shown_ids)

    # 格式化
    remaining = conversation_service.get_remaining_count(session)
    reply = _format_job_results(filtered, remaining)

    return SearchResult(
        reply_text=reply,
        has_more=remaining > 0,
        result_count=len(filtered),
    )


def search_workers(
    criteria: dict,
    raw_query: str,
    session: SessionState,
    user_ctx: UserContext,
    db: Session,
    user_msg_id: str | None = None,
) -> SearchResult:
    """厂家/中介找工人。"""
    top_n = _get_config_int("match.top_n", db, 3)
    max_candidates = _get_config_int("match.max_candidates", db, 50)

    candidates = _query_resumes(criteria, max_candidates, db)

    if 0 < len(candidates) < top_n:
        relaxed = _try_relaxed_resume_search(criteria, max_candidates, db)
        if len(relaxed) > len(candidates):
            candidates = relaxed

    if not candidates:
        return SearchResult(
            reply_text="暂未找到完全匹配的求职者，建议调整条件重新搜索。",
            result_count=0,
        )

    candidate_dicts = _resumes_to_dicts(candidates)

    rerank_result = _rerank_with_logging(
        query=raw_query,
        candidates=candidate_dicts,
        role=user_ctx.role,
        top_n=top_n,
        call_site="search_workers",
        user_msg_id=user_msg_id,
    )

    ranked_ids = [str(item["id"]) for item in rerank_result.ranked_items]
    ranked_id_set = set(ranked_ids)
    for c in candidate_dicts:
        cid = str(c["id"])
        if cid not in ranked_id_set:
            ranked_ids.append(cid)

    digest = conversation_service.compute_query_digest(criteria)
    conversation_service.save_snapshot(session, ranked_ids, digest)

    first_batch_ids = conversation_service.get_next_candidate_ids(session, top_n)
    if not first_batch_ids:
        return SearchResult(reply_text="暂无匹配结果。", result_count=0)

    id_to_dict = {str(c["id"]): c for c in candidate_dicts}
    batch = [id_to_dict[cid] for cid in first_batch_ids if cid in id_to_dict]

    # 构建 users_map 用于权限过滤
    owner_ids = list({r.get("owner_userid", "") for r in batch})
    users_map = _build_users_map(owner_ids, db)
    filtered = permission_service.filter_resumes_batch(batch, users_map, user_ctx.role)

    shown_ids = [str(r["id"]) for r in batch]
    conversation_service.record_shown(session, shown_ids)

    remaining = conversation_service.get_remaining_count(session)
    reply = _format_resume_results(filtered, remaining)

    return SearchResult(
        reply_text=reply,
        has_more=remaining > 0,
        result_count=len(filtered),
    )


def show_more(
    session: SessionState,
    user_ctx: UserContext,
    db: Session,
) -> SearchResult:
    """show_more：从快照取下一批，跳过失效条目。"""
    if session.candidate_snapshot is None:
        return SearchResult(reply_text="当前没有可以继续查看的结果，请先搜索。")

    # 快照过期检查
    if conversation_service.invalidate_snapshot_if_expired(session):
        return SearchResult(reply_text="搜索结果已过期，请重新搜索。")

    top_n = _get_config_int("match.top_n", db, 3)
    # 确定搜索方向
    is_job_search = _is_job_search(session, user_ctx)

    collected = []
    attempts = 0
    max_attempts = top_n * 3  # 防止无限循环

    while len(collected) < top_n and attempts < max_attempts:
        attempts += 1
        batch_ids = conversation_service.get_next_candidate_ids(
            session, top_n - len(collected),
        )
        if not batch_ids:
            break

        # 标记为已展示（即使失效也要标记，避免重复取）
        conversation_service.record_shown(session, batch_ids)

        if is_job_search:
            # 重新查询验证有效性
            valid = _validate_job_ids(batch_ids, db)
            valid_dicts = _jobs_to_dicts(valid, db)
            filtered = permission_service.filter_jobs_batch(valid_dicts, user_ctx.role)
        else:
            valid = _validate_resume_ids(batch_ids, db)
            valid_dicts = _resumes_to_dicts(valid)
            owner_ids = list({r.get("owner_userid", "") for r in valid_dicts})
            users_map = _build_users_map(owner_ids, db)
            filtered = permission_service.filter_resumes_batch(
                valid_dicts, users_map, user_ctx.role,
            )

        collected.extend(filtered)

    if not collected:
        return SearchResult(
            reply_text="已经是所有匹配结果了。要不要调整条件重新搜索？",
            result_count=0,
        )

    # 截断到 top_n
    collected = collected[:top_n]
    remaining = conversation_service.get_remaining_count(session)
    has_more = remaining > 0

    if is_job_search:
        reply = _format_job_results(collected, remaining)
    else:
        reply = _format_resume_results(collected, remaining)

    return SearchResult(
        reply_text=reply,
        has_more=has_more,
        result_count=len(collected),
    )


# ---------------------------------------------------------------------------
# 硬过滤查询
# ---------------------------------------------------------------------------

def has_effective_search_criteria(criteria: dict) -> bool:
    """Stage A 搜索安全护栏：city / job_category 至少一个非空才允许查询。

    任何无 city/job_category 的 criteria（例如只含 headcount）都视为无效，
    上层应跳过 SQL 查询直接返回空结果，避免全表召回。
    """
    if not criteria:
        return False
    return bool(criteria.get("city") or criteria.get("job_category"))


def _query_jobs(criteria: dict, limit: int, db: Session) -> list:
    """构建岗位硬过滤查询。"""
    if not has_effective_search_criteria(criteria):
        return []
    now = datetime.now(timezone.utc)
    q = db.query(Job).join(User, Job.owner_userid == User.external_userid).filter(
        Job.audit_status == "passed",
        Job.deleted_at.is_(None),
        Job.expires_at > now,
        Job.delist_reason.is_(None),
        User.status == "active",
    )

    # 业务条件
    cities = criteria.get("city", [])
    if cities:
        if isinstance(cities, list):
            q = q.filter(Job.city.in_(cities))
        else:
            q = q.filter(Job.city == cities)

    categories = criteria.get("job_category", [])
    if categories:
        if isinstance(categories, list):
            q = q.filter(Job.job_category.in_(categories))
        else:
            q = q.filter(Job.job_category == categories)

    salary_floor = criteria.get("salary_floor_monthly")
    if salary_floor is not None:
        q = q.filter(Job.salary_floor_monthly >= salary_floor)

    is_long_term = criteria.get("is_long_term")
    if is_long_term is not None:
        q = q.filter(Job.is_long_term == is_long_term)

    # 可选过滤开关（从 system_config 读取）
    gender = criteria.get("gender_required")
    if gender and _get_config_bool("filter.enable_gender", db, True):
        q = q.filter(Job.gender_required.in_([gender, "不限"]))

    age = criteria.get("age")
    if age is not None and _get_config_bool("filter.enable_age", db, True):
        q = q.filter(sa.or_(Job.age_min.is_(None), Job.age_min <= age))
        q = q.filter(sa.or_(Job.age_max.is_(None), Job.age_max >= age))

    # 排序 + 截断
    q = q.order_by(Job.created_at.desc(), Job.id.desc())
    return q.limit(limit).all()


def _query_resumes(criteria: dict, limit: int, db: Session) -> list:
    """构建简历硬过滤查询。"""
    if not has_effective_search_criteria(criteria):
        return []
    now = datetime.now(timezone.utc)
    q = db.query(Resume).join(
        User, Resume.owner_userid == User.external_userid,
    ).filter(
        Resume.audit_status == "passed",
        Resume.deleted_at.is_(None),
        Resume.expires_at > now,
        User.status == "active",
    )

    # 城市：检索条件的 city 需要与简历的 expected_cities JSON 数组匹配
    # 使用 JSON_CONTAINS + OR 逻辑：简历期望城市包含搜索条件中的任一城市即命中
    cities = criteria.get("city", [])
    if cities:
        if isinstance(cities, str):
            cities = [cities]
        city_filters = [
            sa.func.json_contains(
                Resume.expected_cities,
                sa.func.cast(city, sa.JSON),
            )
            for city in cities
        ]
        if city_filters:
            q = q.filter(sa.or_(*city_filters))

    categories = criteria.get("job_category", [])
    if categories:
        if isinstance(categories, str):
            categories = [categories]
        cat_filters = [
            sa.func.json_contains(
                Resume.expected_job_categories,
                sa.func.cast(cat, sa.JSON),
            )
            for cat in categories
        ]
        if cat_filters:
            q = q.filter(sa.or_(*cat_filters))

    salary_ceiling = criteria.get("salary_ceiling_monthly")
    if salary_ceiling is not None:
        q = q.filter(Resume.salary_expect_floor_monthly <= salary_ceiling)

    # 可选过滤开关
    gender = criteria.get("gender")
    if gender and _get_config_bool("filter.enable_gender", db, True):
        q = q.filter(Resume.gender == gender)

    age = criteria.get("age")
    if age is not None and _get_config_bool("filter.enable_age", db, True):
        q = q.filter(Resume.age == age)

    q = q.order_by(Resume.created_at.desc(), Resume.id.desc())
    return q.limit(limit).all()


# ---------------------------------------------------------------------------
# 宽松匹配
# ---------------------------------------------------------------------------

def _try_relaxed_job_search(criteria: dict, limit: int, db: Session) -> list:
    """薪资下限放宽 10%（单次）。"""
    relaxed = dict(criteria)
    salary = relaxed.get("salary_floor_monthly")
    if salary is not None:
        relaxed["salary_floor_monthly"] = math.floor(salary * 0.9)
    return _query_jobs(relaxed, limit, db)


def _try_relaxed_resume_search(criteria: dict, limit: int, db: Session) -> list:
    """薪资上限放宽 10%（单次）。"""
    relaxed = dict(criteria)
    salary = relaxed.get("salary_ceiling_monthly")
    if salary is not None:
        relaxed["salary_ceiling_monthly"] = math.ceil(salary * 1.1)
    return _query_resumes(relaxed, limit, db)


# ---------------------------------------------------------------------------
# ORM → dict 转换
# ---------------------------------------------------------------------------

def _jobs_to_dicts(jobs: list, db: Session) -> list[dict]:
    """将 Job ORM 对象转为字典列表，补充关联用户信息。"""
    if not jobs:
        return []
    owner_ids = list({j.owner_userid for j in jobs})
    users_map = _build_users_map(owner_ids, db)

    result = []
    for j in jobs:
        d = {
            "id": j.id,
            "city": j.city,
            "job_category": j.job_category,
            "salary_floor_monthly": j.salary_floor_monthly,
            "salary_ceiling_monthly": j.salary_ceiling_monthly,
            "pay_type": j.pay_type,
            "headcount": j.headcount,
            "gender_required": j.gender_required,
            "is_long_term": j.is_long_term,
            "district": j.district,
            "provide_meal": j.provide_meal,
            "provide_housing": j.provide_housing,
            "shift_pattern": j.shift_pattern,
            "work_hours": j.work_hours,
            "description": j.description,
            "created_at": str(j.created_at) if j.created_at else "",
            "owner_userid": j.owner_userid,
        }
        user_data = users_map.get(j.owner_userid, {})
        d["company"] = user_data.get("company", "")
        d["contact_person"] = user_data.get("contact_person", "")
        d["phone"] = user_data.get("phone", "")
        result.append(d)
    return result


def _resumes_to_dicts(resumes: list) -> list[dict]:
    """将 Resume ORM 对象转为字典列表。"""
    result = []
    for r in resumes:
        d = {
            "id": r.id,
            "expected_cities": r.expected_cities or [],
            "expected_job_categories": r.expected_job_categories or [],
            "salary_expect_floor_monthly": r.salary_expect_floor_monthly,
            "gender": r.gender,
            "age": r.age,
            "education": r.education,
            "work_experience": r.work_experience,
            "description": r.description,
            "created_at": str(r.created_at) if r.created_at else "",
            "owner_userid": r.owner_userid,
        }
        result.append(d)
    return result


def _build_users_map(user_ids: list[str], db: Session) -> dict[str, dict]:
    """构建 {userid: user_data} 映射。"""
    if not user_ids:
        return {}
    users = db.query(User).filter(User.external_userid.in_(user_ids)).all()
    return {
        u.external_userid: {
            "display_name": u.display_name,
            "company": u.company,
            "contact_person": u.contact_person,
            "phone": u.phone,
        }
        for u in users
    }


# ---------------------------------------------------------------------------
# 有效性验证
# ---------------------------------------------------------------------------

def _validate_job_ids(job_ids: list[str], db: Session) -> list:
    """重新查询 ID 列表，过滤已失效的。"""
    now = datetime.now(timezone.utc)
    int_ids = [int(i) for i in job_ids if i.isdigit()]
    if not int_ids:
        return []
    return db.query(Job).join(User, Job.owner_userid == User.external_userid).filter(
        Job.id.in_(int_ids),
        Job.audit_status == "passed",
        Job.deleted_at.is_(None),
        Job.expires_at > now,
        Job.delist_reason.is_(None),
        User.status == "active",
    ).all()


def _validate_resume_ids(resume_ids: list[str], db: Session) -> list:
    """重新查询 ID 列表，过滤已失效的。"""
    now = datetime.now(timezone.utc)
    int_ids = [int(i) for i in resume_ids if i.isdigit()]
    if not int_ids:
        return []
    return db.query(Resume).join(
        User, Resume.owner_userid == User.external_userid,
    ).filter(
        Resume.id.in_(int_ids),
        Resume.audit_status == "passed",
        Resume.deleted_at.is_(None),
        Resume.expires_at > now,
        User.status == "active",
    ).all()


# ---------------------------------------------------------------------------
# 格式化
# ---------------------------------------------------------------------------

def _format_job_results(jobs: list[dict], remaining: int) -> str:
    """按 §10.5 格式化岗位结果（工人视角）。"""
    if not jobs:
        return "暂无匹配结果。"

    lines = [f"为您找到 {len(jobs)} 个匹配岗位：\n"]
    markers = ["①", "②", "③", "④", "⑤"]

    for i, j in enumerate(jobs):
        marker = markers[i] if i < len(markers) else f"({i+1})"
        company = j.get("company", "")
        category = j.get("job_category", "")
        title = f"{company} | {category}" if company else category

        salary_floor = j.get("salary_floor_monthly", 0)
        salary_ceil = j.get("salary_ceiling_monthly")
        pay_type = j.get("pay_type", "")
        if salary_ceil and salary_ceil > salary_floor:
            salary_str = f"{salary_floor}-{salary_ceil}元/月"
        else:
            salary_str = f"{salary_floor}元/月"

        benefits = []
        if j.get("provide_meal"):
            benefits.append("包吃")
        if j.get("provide_housing"):
            benefits.append("包住")
        benefit_str = f"（{pay_type}，{''.join(benefits)}）" if benefits else f"（{pay_type}）"

        city = j.get("city", "")
        district = j.get("district", "")
        location = f"{city}{district}" if district else city

        lines.append(f"{marker} {title}")
        lines.append(f"   💰 {salary_str}{benefit_str}")
        lines.append(f"   📍 {location}")

        shift = j.get("shift_pattern", "")
        hours = j.get("work_hours", "")
        if shift or hours:
            lines.append(f"   🔧 {shift}{'，' + hours if hours else ''}")
        lines.append("")

    if remaining > 0:
        lines.append(f'还有 {remaining} 个相关岗位，回复"更多"继续查看')
    lines.append('不满意？直接告诉我调整方向，比如"薪资再高点""要包住的"')

    return "\n".join(lines)


def _format_resume_results(resumes: list[dict], remaining: int) -> str:
    """按 §10.5 格式化简历结果（厂家/中介视角）。"""
    if not resumes:
        return "暂无匹配结果。"

    lines = [f"为您找到 {len(resumes)} 位匹配的求职者：\n"]
    markers = ["①", "②", "③", "④", "⑤"]

    for i, r in enumerate(resumes):
        marker = markers[i] if i < len(markers) else f"({i+1})"
        name = r.get("display_name", "求职者")
        gender = r.get("gender", "")
        age = r.get("age", "")
        title = f"{name} | {gender} {age}岁" if gender and age else name

        categories = r.get("expected_job_categories", [])
        cat_str = "/".join(categories) if categories else ""
        salary = r.get("salary_expect_floor_monthly", 0)

        cities = r.get("expected_cities", [])
        city_str = "、".join(cities) if cities else ""

        lines.append(f"{marker} {title}")
        if cat_str or salary:
            lines.append(f"   🔧 期望：{cat_str}，{salary}+/月")
        if city_str:
            lines.append(f"   📍 期望城市：{city_str}")

        phone = r.get("phone")
        placeholder = r.get("phone_placeholder")
        if phone:
            lines.append(f"   📞 联系电话：{phone}")
        elif placeholder:
            lines.append(f"   📞 {placeholder}")

        exp = r.get("work_experience", "")
        if exp:
            lines.append(f"   💼 经验：{exp[:50]}")
        lines.append("")

    if remaining > 0:
        lines.append(f'还有 {remaining} 位相关求职者，回复"更多"继续查看')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------

def _is_job_search(session: SessionState, user_ctx: UserContext) -> bool:
    """判断当前搜索方向。"""
    if user_ctx.role == "worker":
        return True
    if user_ctx.role == "broker" and session.broker_direction:
        return session.broker_direction == "search_job"
    # factory 默认找工人
    return False


def _get_config_int(key: str, db: Session, default: int) -> int:
    """从 system_config 读取整数配置。"""
    config = db.query(SystemConfig).filter(
        SystemConfig.config_key == key,
    ).first()
    if config:
        try:
            return int(config.config_value)
        except (ValueError, TypeError):
            pass
    return default


def _get_config_bool(key: str, db: Session, default: bool) -> bool:
    """从 system_config 读取布尔配置。"""
    config = db.query(SystemConfig).filter(
        SystemConfig.config_key == key,
    ).first()
    if config:
        val = config.config_value.strip().lower()
        return val in ("true", "1", "yes")
    return default
