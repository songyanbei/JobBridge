"""Recommendation visibility domain contract.

This module is deliberately free of database and request-layer dependencies.  It
is the single code-level baseline for the role/scene matrix, registered business
fields, safe defaults and request snapshot semantics.  Runtime policy loading is
implemented separately in :mod:`app.services.visibility_policy`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping


class VisibilityScene(StrEnum):
    JOB_SEARCH = "job_search"
    CANDIDATE_SEARCH = "candidate_search"


class ViewerRole(StrEnum):
    WORKER = "worker"
    FACTORY = "factory"
    BROKER = "broker"


class HiringCompanySource(StrEnum):
    JOB = "job.hiring_company"
    PUBLISHER_FALLBACK = "publisher_company_fallback"
    NONE = "none"


class AddressSource(StrEnum):
    JOB = "job.address"
    PUBLISHER_FALLBACK = "publisher_address_fallback"
    NONE = "none"


class ContactSource(StrEnum):
    JOB_OVERRIDE = "job_override"
    PUBLISHER_FALLBACK = "publisher_fallback"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class VisibilityField:
    """Metadata for one operator-configurable business display field."""

    key: str
    label: str
    source_fields: tuple[str, ...]
    data_subject: str
    missing_value_rule: str
    display_transform: str
    ranking_projection: tuple[str, ...] = ()
    soft_preference_mapping: tuple[str, ...] = ()
    sensitive: bool = False
    default_visible_roles: tuple[ViewerRole, ...] = ()

    @property
    def reranker_allowed(self) -> bool:
        return bool(self.ranking_projection)


@dataclass(frozen=True, slots=True)
class SnapshotBehavior:
    """Product-level rules for recommendation request and candidate snapshots."""

    load_policy_once_per_request: bool
    candidate_snapshot_payload: str
    show_more_policy_revision: str
    policy_change_effect: str


JOB_FIELDS: tuple[VisibilityField, ...] = (
    VisibilityField(
        "hiring_company", "招聘工厂", ("job.hiring_company", "publisher.company"),
        "招聘岗位/发布主体", "均为空时不渲染主体标签；发布方回退必须标记为历史回退",
        "按 hiring_company_source 渲染招聘工厂或发布主体（历史回退）",
        default_visible_roles=(ViewerRole.WORKER, ViewerRole.BROKER),
    ),
    VisibilityField(
        "job_category", "岗位", ("job.job_category",), "招聘岗位",
        "缺失时不渲染", "原值展示", ("job_category",), ("job_category",),
        default_visible_roles=(ViewerRole.WORKER, ViewerRole.BROKER),
    ),
    VisibilityField(
        "salary", "薪资",
        ("job.salary_floor_monthly", "job.salary_ceiling_monthly", "job.pay_type"),
        "招聘岗位", "按现有薪资规则使用存在的组成字段降级",
        "组合为薪资区间和计薪方式",
        ("salary_floor_monthly", "salary_ceiling_monthly", "pay_type"),
        ("salary_floor_monthly", "salary_ceiling_monthly"),
        default_visible_roles=(ViewerRole.WORKER, ViewerRole.BROKER),
    ),
    VisibilityField(
        "city", "城市", ("job.city",), "招聘岗位", "缺失时不渲染", "原值展示",
        ("city",), ("city",),
        default_visible_roles=(ViewerRole.BROKER,),
    ),
    VisibilityField(
        "district", "区县", ("job.district",), "招聘岗位", "缺失时不渲染", "原值展示",
        ("district",), ("district",),
        default_visible_roles=(ViewerRole.BROKER,),
    ),
    VisibilityField(
        "address", "具体地址", ("job.address", "publisher.address"),
        "招聘岗位/发布账号", "岗位地址优先；仅可回退发布方经营地址并明确标注岗位地址缺失",
        "按 address_source 渲染工作地址或发布方经营地址（岗位地址缺失）",
        sensitive=True, default_visible_roles=(ViewerRole.BROKER,),
    ),
    VisibilityField(
        "benefits", "吃住福利", ("job.provide_meal", "job.provide_housing"),
        "招聘岗位", "组成项均缺失时不渲染", "合成为包吃/包住标签",
        ("provide_meal", "provide_housing"), ("provide_meal", "provide_housing"),
        default_visible_roles=(ViewerRole.BROKER,),
    ),
    VisibilityField(
        "shift", "班次和工时", ("job.shift_pattern", "job.work_hours"),
        "招聘岗位", "缺失部分不渲染", "合成为班次和工时文本",
        ("shift_pattern", "work_hours"), ("shift_pattern",),
        default_visible_roles=(ViewerRole.BROKER,),
    ),
    VisibilityField(
        "contact_person", "联系人", ("job.contact_person", "publisher.contact_person"),
        "招聘岗位/发布账号", "岗位级优先；均无值时不渲染",
        "按 contact_source 使用岗位级覆盖或发布账号回退", sensitive=True,
        default_visible_roles=(ViewerRole.BROKER,),
    ),
    VisibilityField(
        "phone", "联系电话", ("job.phone", "publisher.phone"),
        "招聘岗位/发布账号", "岗位级优先；均无值且字段可见时显示联系方式待补充",
        "按 phone_source 使用岗位级覆盖或发布账号回退", sensitive=True,
        default_visible_roles=(ViewerRole.BROKER,),
    ),
    VisibilityField(
        "publisher_company", "发布主体", ("publisher.company",), "发布账号",
        "缺失时不渲染", "原值展示",
        default_visible_roles=(ViewerRole.BROKER,),
    ),
)


CANDIDATE_FIELDS: tuple[VisibilityField, ...] = (
    VisibilityField(
        "display_name", "姓名", ("user.display_name",), "求职者账号",
        "无值显示求职者", "原值展示或安全占位",
        default_visible_roles=(ViewerRole.FACTORY, ViewerRole.BROKER),
    ),
    VisibilityField(
        "gender_age", "性别年龄", ("resume.gender", "resume.age"), "求职者简历",
        "仅展示存在的组成部分", "组合为性别和年龄",
        ("gender", "age"), ("gender", "age"),
        default_visible_roles=(ViewerRole.FACTORY, ViewerRole.BROKER),
    ),
    VisibilityField(
        "expected_job_categories", "期望工种", ("resume.expected_job_categories",),
        "求职者简历", "空数组不渲染", "列表展示",
        ("expected_job_categories",), ("expected_job_categories",),
        default_visible_roles=(ViewerRole.FACTORY, ViewerRole.BROKER),
    ),
    VisibilityField(
        "salary_expectation", "期望薪资", ("resume.salary_expect_floor_monthly",),
        "求职者简历", "缺失时不渲染", "渲染为 X+/月",
        ("salary_expect_floor_monthly",), ("salary_expect_floor_monthly",),
        default_visible_roles=(ViewerRole.FACTORY, ViewerRole.BROKER),
    ),
    VisibilityField(
        "expected_cities", "期望城市", ("resume.expected_cities",), "求职者简历",
        "空数组不渲染", "列表展示", ("expected_cities",), ("expected_cities",),
        default_visible_roles=(ViewerRole.FACTORY, ViewerRole.BROKER),
    ),
    VisibilityField(
        "phone", "联系电话", ("user.phone",), "求职者账号",
        "无值且字段可见时显示联系方式待补充", "原值展示或安全占位",
        sensitive=True,
        default_visible_roles=(ViewerRole.FACTORY, ViewerRole.BROKER),
    ),
)


FIELD_REGISTRIES: Mapping[VisibilityScene, tuple[VisibilityField, ...]] = MappingProxyType({
    VisibilityScene.JOB_SEARCH: JOB_FIELDS,
    VisibilityScene.CANDIDATE_SEARCH: CANDIDATE_FIELDS,
})

ROLE_SCENE_ACCESS: Mapping[VisibilityScene, frozenset[ViewerRole]] = MappingProxyType({
    VisibilityScene.JOB_SEARCH: frozenset({ViewerRole.WORKER, ViewerRole.BROKER}),
    VisibilityScene.CANDIDATE_SEARCH: frozenset({ViewerRole.FACTORY, ViewerRole.BROKER}),
})

WORKER_JOB_FIELDS = ("hiring_company", "job_category", "salary")
SENSITIVE_EXPANSION_FIELDS = frozenset({"phone", "contact_person", "address"})

BUSINESS_DEFAULT_POLICY: Mapping[VisibilityScene, Mapping[ViewerRole, tuple[str, ...]]] = MappingProxyType({
    VisibilityScene.JOB_SEARCH: MappingProxyType({
        ViewerRole.WORKER: WORKER_JOB_FIELDS,
        ViewerRole.FACTORY: (),
        ViewerRole.BROKER: tuple(field.key for field in JOB_FIELDS),
    }),
    VisibilityScene.CANDIDATE_SEARCH: MappingProxyType({
        ViewerRole.WORKER: (),
        ViewerRole.FACTORY: tuple(field.key for field in CANDIDATE_FIELDS),
        ViewerRole.BROKER: tuple(field.key for field in CANDIDATE_FIELDS),
    }),
})

# A corrupt or unreadable database policy must never widen high-sensitivity
# visibility.  Worker still receives the product-mandated exact three fields.
BUILTIN_SAFE_POLICY_ID = "builtin-safe-v1"
BUILTIN_SAFE_POLICY: Mapping[VisibilityScene, Mapping[ViewerRole, tuple[str, ...]]] = MappingProxyType({
    VisibilityScene.JOB_SEARCH: MappingProxyType({
        ViewerRole.WORKER: WORKER_JOB_FIELDS,
        ViewerRole.FACTORY: (),
        ViewerRole.BROKER: tuple(
            field.key for field in JOB_FIELDS if field.key not in SENSITIVE_EXPANSION_FIELDS
        ),
    }),
    VisibilityScene.CANDIDATE_SEARCH: MappingProxyType({
        ViewerRole.WORKER: (),
        ViewerRole.FACTORY: tuple(
            field.key for field in CANDIDATE_FIELDS if field.key not in SENSITIVE_EXPANSION_FIELDS
        ),
        ViewerRole.BROKER: tuple(
            field.key for field in CANDIDATE_FIELDS if field.key not in SENSITIVE_EXPANSION_FIELDS
        ),
    }),
})

SNAPSHOT_BEHAVIOR = SnapshotBehavior(
    load_policy_once_per_request=True,
    candidate_snapshot_payload="ids_only",
    show_more_policy_revision="current_request_revision",
    policy_change_effect="next_generated_reply",
)


def registry_for(scene: VisibilityScene) -> Mapping[str, VisibilityField]:
    """Return a read-only, ordered field lookup for ``scene``."""

    return MappingProxyType({field.key: field for field in FIELD_REGISTRIES[scene]})


def hard_visibility_limit(scene: VisibilityScene, role: ViewerRole) -> tuple[str, ...]:
    """Return the maximum configurable fields allowed by the backend."""

    if role not in ROLE_SCENE_ACCESS[scene]:
        return ()
    if scene is VisibilityScene.JOB_SEARCH and role is ViewerRole.WORKER:
        return WORKER_JOB_FIELDS
    return tuple(field.key for field in FIELD_REGISTRIES[scene])
