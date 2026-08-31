"""ORM 模型定义（对应 schema.sql 11 张表）。

真值来源：backend/sql/schema.sql
所有默认值、可空性、索引、唯一约束、字段类型与 DDL 保持严格一致。
目标数据库：MySQL 8.0+，因此直接使用 sqlalchemy.dialects.mysql 类型
以确保 UNSIGNED / TINYINT / MEDIUMTEXT 等与 DDL 完全对齐。
"""
from uuid import uuid4

import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.ext.mutable import MutableDict
from sqlalchemy.orm import deferred

from app.db import Base


class _CurrentTimestamp6(sa.sql.expression.FunctionElement):
    """Portable ORM default; MySQL keeps the frozen microsecond contract."""
    type = sa.DateTime()
    inherit_cache = True


@compiles(_CurrentTimestamp6)
def _compile_current_timestamp(element, compiler, **kw):  # noqa: ARG001
    return "CURRENT_TIMESTAMP"


@compiles(_CurrentTimestamp6, "mysql")
def _compile_mysql_current_timestamp(element, compiler, **kw):  # noqa: ARG001
    return "CURRENT_TIMESTAMP(6)"


class _CurrentTimestamp6OnUpdate(sa.sql.expression.FunctionElement):
    """Portable server default with MySQL's ON UPDATE clause."""
    type = sa.DateTime()
    inherit_cache = True


@compiles(_CurrentTimestamp6OnUpdate)
def _compile_current_timestamp_on_update(element, compiler, **kw):  # noqa: ARG001
    return "CURRENT_TIMESTAMP"


@compiles(_CurrentTimestamp6OnUpdate, "mysql")
def _compile_mysql_current_timestamp_on_update(element, compiler, **kw):  # noqa: ARG001
    return "CURRENT_TIMESTAMP(6) ON UPDATE CURRENT_TIMESTAMP(6)"


# ============================================================================
# 1. User 用户表
# ============================================================================

class User(Base):
    __tablename__ = "user"

    external_userid = sa.Column(sa.String(64), primary_key=True, comment="企微外部联系人 ID")
    role = sa.Column(
        sa.Enum("worker", "factory", "broker", name="user_role"),
        nullable=False, comment="角色：工人/厂家/中介",
    )
    display_name = sa.Column(sa.String(64), nullable=True, comment="展示昵称")
    company = sa.Column(sa.String(128), nullable=True, comment="公司名")
    address = sa.Column(sa.String(255), nullable=True, comment="公司/经营地址")
    contact_person = sa.Column(sa.String(64), nullable=True, comment="联系人姓名")
    phone = sa.Column(sa.String(32), nullable=True, comment="联系电话")
    phone_ciphertext = sa.Column(sa.LargeBinary, nullable=True, comment="加密联系电话（Contact B1）")
    phone_key_version = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True)
    phone_digest = sa.Column(mysql.CHAR(64), nullable=True)
    contact_person_ciphertext = sa.Column(sa.LargeBinary, nullable=True, comment="加密联系人（Contact B1）")
    contact_person_key_version = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True)
    contact_person_digest = sa.Column(mysql.CHAR(64), nullable=True)
    wechat_ciphertext = sa.Column(sa.LargeBinary, nullable=True, comment="加密微信号（Contact B1）")
    wechat_key_version = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True)
    wechat_digest = sa.Column(mysql.CHAR(64), nullable=True)
    can_search_jobs = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("0"), comment="能否检索岗位")
    can_search_workers = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("0"), comment="能否检索工人")
    status = sa.Column(
        sa.Enum("active", "blocked", "deleted", name="user_status"),
        nullable=False, server_default="active", comment="状态",
    )
    blocked_reason = sa.Column(sa.String(255), nullable=True, comment="封禁原因")
    registered_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), comment="注册时间")
    last_active_at = sa.Column(sa.DateTime, nullable=True, comment="最近活跃时间")
    extra = sa.Column(MutableDict.as_mutable(sa.JSON), nullable=True, comment="扩展字段")

    __table_args__ = (
        sa.Index("idx_role_status", "role", "status"),
        sa.Index("idx_last_active", "last_active_at"),
    )


# ============================================================================
# 2. Job 岗位信息表
# ============================================================================

class Job(Base):
    __tablename__ = "job"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    owner_userid = sa.Column(sa.String(64), sa.ForeignKey("user.external_userid", ondelete="RESTRICT"), nullable=False, comment="发布者")
    hiring_company = sa.Column(sa.String(128), nullable=True, comment="实际招聘工厂名（岗位级）")

    # ---- 硬过滤字段（§7.1）----
    city = sa.Column(sa.String(32), nullable=False, comment="城市")
    job_category = sa.Column(sa.String(32), nullable=False, comment="工种大类")
    salary_floor_monthly = sa.Column(sa.Integer, nullable=False, comment="月综合收入下限（元）")
    pay_type = sa.Column(
        sa.Enum("月薪", "时薪", "计件", name="job_pay_type"),
        nullable=False, comment="计薪方式",
    )
    headcount = sa.Column(sa.Integer, nullable=False, comment="还缺多少人")
    gender_required = sa.Column(
        sa.Enum("男", "女", "不限", name="job_gender_required"),
        nullable=False, server_default="不限", comment="性别要求",
    )
    age_min = sa.Column(mysql.TINYINT(unsigned=True), nullable=True, comment="年龄下限")
    age_max = sa.Column(mysql.TINYINT(unsigned=True), nullable=True, comment="年龄上限")
    is_long_term = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("1"), comment="1=长期工，0=短期工")

    # ---- 软匹配字段（§7.1）----
    district = sa.Column(sa.String(32), nullable=True, comment="区县（细粒度）")
    address = sa.Column(sa.String(255), nullable=True, comment="岗位详细工作地址（街道+门牌）")
    contact_person = sa.Column(sa.String(64), nullable=True, comment="岗位级联系人（覆盖发布账号）")
    phone = sa.Column(sa.String(32), nullable=True, comment="岗位级联系电话（覆盖发布账号）")
    phone_ciphertext = sa.Column(sa.LargeBinary, nullable=True, comment="加密岗位联系电话（Contact B1）")
    phone_key_version = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True)
    phone_digest = sa.Column(mysql.CHAR(64), nullable=True)
    contact_person_ciphertext = sa.Column(sa.LargeBinary, nullable=True, comment="加密岗位联系人（Contact B1）")
    contact_person_key_version = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True)
    contact_person_digest = sa.Column(mysql.CHAR(64), nullable=True)
    wechat_ciphertext = sa.Column(sa.LargeBinary, nullable=True, comment="加密岗位微信号（Contact B1）")
    wechat_key_version = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True)
    wechat_digest = sa.Column(mysql.CHAR(64), nullable=True)
    salary_ceiling_monthly = sa.Column(sa.Integer, nullable=True, comment="月综合收入上限")
    provide_meal = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="包吃")
    provide_housing = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="包住")
    dorm_condition = sa.Column(sa.String(255), nullable=True, comment="宿舍条件自由描述")
    shift_pattern = sa.Column(sa.String(128), nullable=True, comment="班次模式")
    work_hours = sa.Column(sa.String(128), nullable=True, comment="工时描述")
    accept_couple = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="接受夫妻工")
    accept_student = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="接受学生工")
    accept_minority = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="接受少数民族")
    height_required = sa.Column(sa.String(32), nullable=True, comment="身高要求")
    experience_required = sa.Column(sa.String(255), nullable=True, comment="经验要求自由文本")
    education_required = sa.Column(
        sa.Enum("不限", "初中", "高中", "中专", "大专及以上", name="education_level"),
        nullable=True, server_default="不限",
    )
    rebate = sa.Column(sa.String(255), nullable=True, comment="返费承诺")
    employment_type = sa.Column(
        sa.Enum("厂家直招", "劳务派遣", "中介代招", name="employment_type"),
        nullable=True,
    )
    contract_type = sa.Column(
        sa.Enum("长期合同", "短期合同", "劳务关系", name="contract_type"),
        nullable=True,
    )
    min_duration = sa.Column(sa.String(64), nullable=True, comment="最短做满多少天")
    job_sub_category = sa.Column(sa.String(64), nullable=True, comment="工种子类")

    # ---- 原始描述 ----
    raw_text = sa.Column(sa.Text, nullable=False, comment="用户原始提交")
    description = sa.Column(sa.Text, nullable=True, comment="IntentExtractor 清洗后的规范化描述")

    # ---- 媒体 ----
    images = sa.Column(sa.JSON, nullable=True, comment="图片对象存储 key 数组（最多 5 张）")
    miniprogram_url = sa.Column(sa.String(512), nullable=True, comment="小程序详情页链接")

    # ---- 审核 ----
    audit_status = sa.Column(
        sa.Enum("pending", "passed", "rejected", name="audit_status"),
        nullable=False, server_default="pending",
    )
    audit_reason = sa.Column(sa.String(255), nullable=True, comment="审核理由（驳回时必填）")
    audited_by = sa.Column(sa.String(64), nullable=True, comment="审核人（system / admin 用户名）")
    audited_at = sa.Column(sa.DateTime, nullable=True)

    # ---- 生命周期 ----
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())
    updated_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())
    expires_at = sa.Column(sa.DateTime, nullable=True, comment="激活后的业务过期时间")
    activated_at = sa.Column(sa.DateTime, nullable=True, comment="业务激活时间")
    candidate_expires_at = sa.Column(sa.DateTime, nullable=True, comment="候选版本回收时间")
    delist_reason = sa.Column(
        sa.Enum("filled", "manual_delist", "expired", "replaced", name="delist_reason"),
        nullable=True, comment="下架原因",
    )
    deleted_at = sa.Column(sa.DateTime, nullable=True, comment="软删除时间")

    version = sa.Column(
        mysql.INTEGER(unsigned=True), nullable=False,
        server_default=sa.text("1"), comment="乐观锁版本号",
    )
    # Phase 14 monotonic fact-source version. Kept alongside legacy version
    # during the additive migration window.
    aggregate_version = sa.Column(
        mysql.BIGINT(unsigned=True), nullable=False,
        server_default=sa.text("1"), comment="领域聚合版本号",
    )
    extra = sa.Column(MutableDict.as_mutable(sa.JSON), nullable=True, comment="扩展字段（§7.6）")

    __table_args__ = (
        sa.Index("idx_owner", "owner_userid"),
        sa.Index("idx_audit_time", "audit_status", "created_at"),
        sa.Index("idx_expires", "expires_at"),
        sa.Index("idx_job_candidate_expiry", "audit_status", "candidate_expires_at"),
        sa.Index("idx_filter_hot", "city", "job_category", "is_long_term", "audit_status", "deleted_at", "expires_at"),
        sa.Index("idx_salary", "salary_floor_monthly"),
    )


# Phase 14: versioned domain events emitted with fact-source writes.
class DomainOutboxEvent(Base):
    """Versioned, privacy-safe domain event for fact-source changes."""
    __tablename__ = "domain_outbox_event"

    id = sa.Column(sa.Integer, primary_key=True, autoincrement=True)
    aggregate_type = sa.Column(sa.String(32), nullable=False)
    aggregate_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    aggregate_version = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    event_type = sa.Column(sa.String(64), nullable=False)
    payload = sa.Column(sa.JSON, nullable=False)
    payload_digest = sa.Column(mysql.CHAR(64), nullable=False)
    trace_id = sa.Column(sa.String(64), nullable=True)
    occurred_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())
    tombstone = sa.Column(sa.Boolean, nullable=False, server_default=sa.text("0"))
    status = sa.Column(sa.Enum("pending", "processing", "published", "dead_letter", name="domain_outbox_status"), nullable=False, server_default="pending")
    attempt_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    next_attempt_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    lease_owner = sa.Column(sa.String(64), nullable=True)
    lease_until = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    fencing_token = sa.Column(mysql.BIGINT(unsigned=True), nullable=False, server_default=sa.text("0"))
    last_error = sa.Column(sa.String(255), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())

    __table_args__ = (
        sa.UniqueConstraint("aggregate_type", "aggregate_id", "aggregate_version", "event_type", name="uq_domain_outbox_versioned_event"),
        sa.Index("idx_domain_outbox_pending", "status", "occurred_at", "id"),
        sa.Index("idx_domain_outbox_aggregate", "aggregate_type", "aggregate_id", "aggregate_version"),
    )


# ============================================================================
# Contact B0: opaque requests, one-time grants and privacy-safe audit
# ============================================================================

class ContactRequest(Base):
    """Server-owned contact entry point. No contact value is stored here."""

    __tablename__ = "contact_request"

    request_id = sa.Column(sa.String(64), primary_key=True)
    actor_id = sa.Column(sa.String(64), nullable=False)
    listing_ref = sa.Column(sa.String(200), nullable=False)
    direction = sa.Column(sa.String(32), nullable=True, comment="绑定的招聘搜索方向")
    action = sa.Column(sa.String(32), nullable=False, server_default="request_contact")
    request_digest = sa.Column(mysql.CHAR(64), nullable=False)
    nonce_digest = sa.Column(mysql.CHAR(64), nullable=False)
    listing_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    policy_version = sa.Column(sa.String(64), nullable=True)
    status = sa.Column(sa.Enum("pending", "authorized", "revoked", "expired", name="contact_request_status"), nullable=False, server_default="pending")
    expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False)
    revoked_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    revoke_reason = sa.Column(sa.String(64), nullable=True)
    trace_id = sa.Column(sa.String(64), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6OnUpdate(), server_onupdate=_CurrentTimestamp6())

    __table_args__ = (
        sa.Index("idx_contact_request_actor", "actor_id", "created_at", "request_id"),
        sa.Index("idx_contact_request_listing", "listing_ref", "status", "expires_at"),
    )


class ContactGrant(Base):
    """Hashed, short-lived, single-use credential; token is never persisted."""

    __tablename__ = "contact_grant"

    grant_id = sa.Column(sa.String(64), primary_key=True)
    request_id = sa.Column(sa.String(64), sa.ForeignKey("contact_request.request_id", ondelete="RESTRICT"), nullable=False)
    actor_id = sa.Column(sa.String(64), nullable=False)
    listing_ref = sa.Column(sa.String(200), nullable=False)
    direction = sa.Column(sa.String(32), nullable=True)
    action = sa.Column(sa.String(32), nullable=False)
    token_hash = sa.Column(mysql.CHAR(64), nullable=False, unique=True)
    nonce_digest = sa.Column(mysql.CHAR(64), nullable=False)
    listing_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    policy_version = sa.Column(sa.String(64), nullable=True)
    status = sa.Column(sa.Enum("issued", "used", "revoked", "expired", name="contact_grant_status"), nullable=False, server_default="issued")
    expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False)
    used_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    revoked_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    revoke_reason = sa.Column(sa.String(64), nullable=True)
    trace_id = sa.Column(sa.String(64), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())

    __table_args__ = (
        sa.Index("idx_contact_grant_actor", "actor_id", "created_at", "grant_id"),
        sa.Index("idx_contact_grant_due", "status", "expires_at", "grant_id"),
        sa.Index("idx_contact_grant_request", "request_id", "status"),
    )


class ContactAccessAudit(Base):
    """Append-only contact decision log. Values are hashes/reason codes only."""

    __tablename__ = "contact_access_audit"

    # SQLite unit tests need an INTEGER-affinity primary key for rowid
    # autoincrement; retain MySQL BIGINT UNSIGNED in the production dialect.
    id = sa.Column(
        sa.Integer().with_variant(mysql.BIGINT(unsigned=True), "mysql"),
        primary_key=True,
        autoincrement=True,
    )
    event_id = sa.Column(sa.String(36), nullable=False, unique=True)
    event_type = sa.Column(sa.String(32), nullable=False)
    outcome = sa.Column(sa.String(32), nullable=False)
    reason_code = sa.Column(sa.String(64), nullable=False)
    actor_hash = sa.Column(mysql.CHAR(64), nullable=True)
    listing_hash = sa.Column(mysql.CHAR(64), nullable=True)
    request_id = sa.Column(sa.String(64), nullable=True)
    grant_id = sa.Column(sa.String(64), nullable=True)
    trace_id = sa.Column(sa.String(64), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())

    __table_args__ = (
        sa.Index("idx_contact_audit_trace", "trace_id", "created_at"),
        sa.Index("idx_contact_audit_actor", "actor_hash", "created_at"),
        sa.Index("idx_contact_audit_request", "request_id", "created_at"),
    )


class ContactDelivery(Base):
    """Stable encrypted delivery created when a grant is consumed (B2)."""

    __tablename__ = "contact_delivery"

    delivery_id = sa.Column(sa.String(64), primary_key=True)
    grant_id = sa.Column(sa.String(64), sa.ForeignKey("contact_grant.grant_id", ondelete="RESTRICT"), nullable=False, unique=True)
    actor_id = sa.Column(sa.String(64), nullable=False)
    listing_ref = sa.Column(sa.String(200), nullable=False)
    channel = sa.Column(sa.String(32), nullable=False, server_default="platform_request")
    content_ciphertext = sa.Column(sa.LargeBinary, nullable=True)
    key_version = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True)
    content_hash = sa.Column(mysql.CHAR(64), nullable=True)
    status = sa.Column(sa.Enum("prepared", "sending", "sent", "retry_wait", "revoked", "expired", name="contact_delivery_status"), nullable=False, server_default="prepared")
    expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False)
    revoked_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    revoke_reason = sa.Column(sa.String(64), nullable=True)
    sent_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())

    __table_args__ = (
        sa.Index("idx_contact_delivery_due", "status", "expires_at", "delivery_id"),
        sa.Index("idx_contact_delivery_actor", "actor_id", "created_at", "delivery_id"),
    )


class JobReplacement(Base):
    __tablename__ = "job_replacement"
    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    operation_id = sa.Column(sa.String(36), nullable=False, unique=True)
    source_msg_id = sa.Column(sa.String(128), nullable=False, unique=True)
    owner_userid = sa.Column(sa.String(64), nullable=False)
    old_job_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    new_job_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False, unique=True)
    old_job_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False)
    old_expires_at = sa.Column(sa.DateTime, nullable=True)
    old_business_digest = sa.Column(sa.String(64), nullable=False)
    old_business_digest_version = sa.Column(mysql.TINYINT(unsigned=True), nullable=False, server_default="2")
    review_outcome = sa.Column(sa.Enum("pending", "passed", "rejected", name="replacement_review_outcome"), nullable=False)
    reviewed_at = sa.Column(sa.DateTime, nullable=True)
    reviewed_by = sa.Column(sa.String(64), nullable=True)
    lifecycle_status = sa.Column(sa.Enum("awaiting_review", "activated", "closed", "conflict", name="replacement_lifecycle_status"), nullable=False)
    active_old_job_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True, unique=True)
    closed_reason = sa.Column(sa.String(64), nullable=True)
    conflict_reason = sa.Column(sa.String(255), nullable=True)
    activated_at = sa.Column(sa.DateTime, nullable=True)
    candidate_cleaned_at = sa.Column(sa.DateTime, nullable=True)
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())
    updated_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())
    __table_args__ = (
        sa.Index("idx_replacement_old_status", "old_job_id", "lifecycle_status"),
        sa.Index("idx_replacement_owner_created", "owner_userid", "created_at"),
        sa.Index("idx_replacement_lifecycle_created", "lifecycle_status", "created_at"),
        sa.Index("idx_replacement_review_created", "review_outcome", "created_at"),
    )


class MediaAssetLifecycle(Base):
    __tablename__ = "media_asset_lifecycle"
    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    object_key = sa.Column(sa.String(512), nullable=False, unique=True)
    operation_id = sa.Column(sa.String(36), nullable=True, index=True)
    owner_userid = sa.Column(sa.String(64), nullable=False)
    entity_type = sa.Column(sa.Enum("job", "resume", name="media_entity_type"), nullable=True)
    entity_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    # Version of the owning listing at attachment time.  Nullable keeps old
    # lifecycle rows readable during the additive migration/backfill window.
    entity_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    state = sa.Column(
        sa.Enum(
            "pending",
            "attached",
            "delete_pending",
            "deleted",
            "dead_letter",
            name="media_asset_state",
        ),
        nullable=False, server_default="pending",
    )
    draft_expires_at = sa.Column(sa.DateTime, nullable=True)
    attempt_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default="0")
    next_attempt_at = sa.Column(sa.DateTime, nullable=True)
    last_error = sa.Column(sa.String(255), nullable=True)
    lease_owner = sa.Column(sa.String(64), nullable=True)
    lease_expires_at = sa.Column(sa.DateTime, nullable=True)
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())
    updated_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())
    deleted_at = sa.Column(sa.DateTime, nullable=True)

    __table_args__ = (
        sa.Index("idx_media_entity", "entity_type", "entity_id", "state"),
        sa.Index("idx_media_entity_version", "entity_type", "entity_id", "entity_version", "state"),
        sa.Index("idx_media_cleanup", "state", "next_attempt_at"),
        sa.Index("idx_media_draft_expiry", "state", "draft_expires_at"),
    )


class TargetCleanupTask(Base):
    __tablename__ = "target_cleanup_task"
    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    operation_id = sa.Column(sa.String(36), nullable=False, unique=True)
    target_type = sa.Column(sa.String(32), nullable=False)
    target_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    reason = sa.Column(sa.String(32), nullable=False)
    reason_history = sa.Column(sa.JSON, nullable=True)
    status = sa.Column(
        sa.Enum("pending", "processing", "retry_wait", "succeeded", "dead_letter", name="target_cleanup_status"),
        nullable=False, server_default="pending",
    )
    delivery_ids = sa.Column(sa.JSON, nullable=True)
    db_redacted_at = sa.Column(sa.DateTime, nullable=True)
    conversation_redacted_at = sa.Column(sa.DateTime, nullable=True)
    session_invalidated_at = sa.Column(sa.DateTime, nullable=True)
    attempt_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default="0")
    next_attempt_at = sa.Column(sa.DateTime, nullable=True)
    last_error = sa.Column(sa.String(255), nullable=True)
    lease_owner = sa.Column(sa.String(64), nullable=True)
    lease_expires_at = sa.Column(sa.DateTime, nullable=True)
    completed_at = sa.Column(sa.DateTime, nullable=True)
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())
    updated_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())

    __table_args__ = (
        sa.UniqueConstraint("target_type", "target_id", name="uq_cleanup_target"),
        sa.Index("idx_target_cleanup_ready", "status", "next_attempt_at"),
    )


# ============================================================================
# 3. Resume 简历信息表
# ============================================================================

class Resume(Base):
    __tablename__ = "resume"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    owner_userid = sa.Column(sa.String(64), sa.ForeignKey("user.external_userid", ondelete="RESTRICT"), nullable=False, comment="工人 external_userid")

    # ---- 硬过滤字段（§7.2）----
    expected_cities = sa.Column(sa.JSON, nullable=False, comment="期望城市列表（至少一个）")
    expected_job_categories = sa.Column(sa.JSON, nullable=False, comment="期望工种大类列表")
    salary_expect_floor_monthly = sa.Column(sa.Integer, nullable=False, comment="期望月综合收入下限")
    gender = sa.Column(
        sa.Enum("男", "女", name="resume_gender"),
        nullable=False, comment="性别",
    )
    age = sa.Column(mysql.TINYINT(unsigned=True), nullable=False, comment="年龄")
    accept_long_term = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("1"), comment="接受长期工")
    accept_short_term = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("0"), comment="接受短期工")

    # ---- 软匹配字段（§7.2）----
    expected_districts = sa.Column(sa.JSON, nullable=True, comment="期望区县")
    height = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True, comment="身高 cm")
    weight = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True, comment="体重 kg")
    education = sa.Column(
        sa.Enum("不限", "初中", "高中", "中专", "大专及以上", name="resume_education"),
        nullable=True, server_default="不限",
    )
    work_experience = sa.Column(sa.Text, nullable=True, comment="工作经历自由文本")
    accept_night_shift = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="接受倒班/夜班")
    accept_standing_work = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="接受长时间站立")
    accept_overtime = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="接受加班")
    accept_outside_province = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="接受出省")
    couple_seeking_together = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="夫妻同求")
    has_health_certificate = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="持有健康证")
    ethnicity = sa.Column(sa.String(32), nullable=True, comment="民族")
    available_from = sa.Column(sa.Date, nullable=True, comment="可到岗日期")
    has_tattoo = sa.Column(mysql.TINYINT(display_width=1), nullable=True, comment="有纹身")
    taboo = sa.Column(sa.String(255), nullable=True, comment="禁忌自由文本")

    # ---- 原始描述 ----
    raw_text = sa.Column(sa.Text, nullable=False, comment="用户原始提交")
    description = sa.Column(sa.Text, nullable=True, comment="IntentExtractor 清洗后的规范化描述")

    # ---- 媒体 ----
    images = sa.Column(sa.JSON, nullable=True, comment="图片对象存储 key 数组（最多 5 张）")
    miniprogram_url = sa.Column(sa.String(512), nullable=True, comment="小程序个人介绍链接")

    # ---- 审核 ----
    audit_status = sa.Column(
        sa.Enum("pending", "passed", "rejected", name="resume_audit_status"),
        nullable=False, server_default="pending",
    )
    audit_reason = sa.Column(sa.String(255), nullable=True)
    audited_by = sa.Column(sa.String(64), nullable=True)
    audited_at = sa.Column(sa.DateTime, nullable=True)

    # ---- 生命周期 ----
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())
    updated_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=False,
        server_default=_CurrentTimestamp6OnUpdate(),
        server_onupdate=_CurrentTimestamp6(),
    )
    activated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True, comment="业务激活时间")
    candidate_expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True, comment="候选版本回收时间")
    expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True, comment="激活后的业务过期时间")
    delist_reason = sa.Column(sa.String(32), nullable=True, comment="下架原因")
    deleted_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)

    # ---- 乐观锁 ----
    version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1"), comment="乐观锁版本号")
    # Phase 15 additive domain version; legacy ``version`` remains mirrored.
    aggregate_version = sa.Column(mysql.BIGINT(unsigned=True), nullable=False, server_default=sa.text("1"), comment="领域聚合版本号")

    # ---- 扩展 ----
    extra = sa.Column(MutableDict.as_mutable(sa.JSON), nullable=True, comment="扩展字段")

    __table_args__ = (
        sa.Index("idx_owner", "owner_userid"),
        sa.Index("idx_audit_time", "audit_status", "created_at"),
        sa.Index("idx_expires", "expires_at"),
        sa.Index("idx_resume_candidate_expiry", "audit_status", "candidate_expires_at"),
        sa.Index("idx_resume_hard_delete", "deleted_at", "id"),
        sa.Index("idx_filter_hot", "gender", "age", "audit_status", "deleted_at", "expires_at"),
        sa.Index("idx_salary_exp", "salary_expect_floor_monthly"),
    )


class ResumeReplacement(Base):
    """Durable relation between an old active resume and its replacement candidate."""
    __tablename__ = "resume_replacement"
    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    operation_id = sa.Column(mysql.CHAR(36), nullable=False)
    source_msg_id = sa.Column(sa.String(128), nullable=False)
    owner_userid = sa.Column(sa.String(64), nullable=False)
    old_resume_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    new_resume_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    old_resume_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False)
    old_expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    old_business_digest = sa.Column(mysql.CHAR(64), nullable=False)
    old_business_digest_version = sa.Column(mysql.TINYINT(unsigned=True), nullable=False, server_default=sa.text("2"))
    review_outcome = sa.Column(sa.Enum("pending", "passed", "rejected", name="resume_replacement_review_outcome"), nullable=False, server_default="pending")
    reviewed_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    reviewed_by = sa.Column(sa.String(64), nullable=True)
    lifecycle_status = sa.Column(sa.Enum("awaiting_review", "activated", "closed", "conflict", name="resume_replacement_lifecycle_status"), nullable=False, server_default="awaiting_review")
    active_old_resume_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    closed_reason = sa.Column(sa.String(64), nullable=True)
    conflict_reason = sa.Column(sa.String(255), nullable=True)
    activated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    candidate_cleaned_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())
    updated_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=False,
        server_default=_CurrentTimestamp6OnUpdate(),
        server_onupdate=_CurrentTimestamp6(),
    )
    __table_args__ = (
        sa.UniqueConstraint("operation_id", name="uq_resume_replacement_operation"),
        sa.UniqueConstraint("source_msg_id", name="uq_resume_replacement_message"),
        sa.UniqueConstraint("new_resume_id", name="uq_resume_replacement_new"),
        sa.UniqueConstraint("active_old_resume_id", name="uq_resume_replacement_active_old"),
        sa.Index("idx_resume_replacement_old_status", "old_resume_id", "lifecycle_status"),
        sa.Index("idx_resume_replacement_owner_created", "owner_userid", "created_at"),
        sa.Index("idx_resume_replacement_lifecycle_created", "lifecycle_status", "created_at"),
    )


class ResumeReplacementRolloutAssignment(Base):
    __tablename__ = "resume_replacement_rollout_assignment"
    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    operation_id = sa.Column(mysql.CHAR(36), nullable=False)
    owner_userid = sa.Column(sa.String(64), nullable=False)
    cohort = sa.Column(sa.Enum("enabled", "control", name="resume_rollout_cohort"), nullable=False)
    allowlist_revision = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    source_msg_id = sa.Column(sa.String(128), nullable=False)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())
    __table_args__ = (
        sa.UniqueConstraint("operation_id", name="uq_resume_rollout_operation"),
        sa.UniqueConstraint("source_msg_id", name="uq_resume_rollout_message"),
    )


class Phase11MigrationLedger(Base):
    __tablename__ = "phase11_migration_ledger"
    migration_key = sa.Column(sa.String(128), primary_key=True)
    script_sha256 = sa.Column(mysql.CHAR(64), nullable=False)
    stage = sa.Column(sa.Enum("pre_cutover", "post_cutover", "verify", "down", name="phase11_migration_stage"), nullable=False)
    kind = sa.Column(sa.Enum("sql", "python", "verify_sql", name="phase11_migration_kind"), nullable=False)
    status = sa.Column(sa.Enum("running", "succeeded", "failed", "verified", name="phase11_migration_status"), nullable=False)
    attempt = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default="0")
    last_statement_ordinal = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default="0")
    resume_cursor_json = sa.Column(sa.JSON, nullable=True)
    started_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    completed_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    cutover_resume_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    build_probe_digest = sa.Column(mysql.CHAR(64), nullable=True)
    executed_by = sa.Column(sa.String(128), nullable=False)
    error_code = sa.Column(sa.String(64), nullable=True)
    verification_digest = sa.Column(mysql.CHAR(64), nullable=True)
    updated_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=False,
        server_default=_CurrentTimestamp6OnUpdate(),
        server_onupdate=_CurrentTimestamp6(),
    )


class ResumeMediaIsolationIssue(Base):
    __tablename__ = "resume_media_isolation_issue"
    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    resume_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    key_hash = sa.Column(mysql.CHAR(64), nullable=False)
    issue_type = sa.Column(sa.String(64), nullable=False)
    status = sa.Column(sa.Enum("open", "approved", "resolved", "blocked", name="resume_media_issue_status"), nullable=False, server_default="open")
    disposition = sa.Column(sa.Enum("assign_owner", "detach_reference", "delete_object", name="resume_media_disposition"), nullable=True)
    approval_reason = sa.Column(sa.String(255), nullable=True)
    approved_by = sa.Column(sa.String(64), nullable=True)
    approved_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    executed_by = sa.Column(sa.String(64), nullable=True)
    executed_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    resolved_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())
    updated_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=False,
        server_default=_CurrentTimestamp6OnUpdate(),
        server_onupdate=_CurrentTimestamp6(),
    )
    __table_args__ = (sa.UniqueConstraint("resume_id", "key_hash", "issue_type", name="uq_resume_media_isolation_issue"),)


class Phase11ResumeMediaKeyScan(Base):
    """Hash-only migration registry used to detect shared media on resume."""
    __tablename__ = "phase11_resume_media_key_scan"
    resume_id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=False)
    key_hash = sa.Column(mysql.CHAR(64), primary_key=True)
    reference_kind = sa.Column(
        sa.Enum("valid", "invalid", name="phase11_resume_media_reference_kind"),
        primary_key=True,
    )
    reference_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False)
    first_seen_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())
    __table_args__ = (
        sa.Index(
            "idx_phase11_media_scan_key",
            "key_hash",
            "reference_kind",
            "resume_id",
        ),
    )


class Phase11ResumeLifecycleBackup(Base):
    __tablename__ = "phase11_resume_lifecycle_backup"
    resume_id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=False)
    expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    activated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    candidate_expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    deleted_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False)
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False)
    captured_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=_CurrentTimestamp6())


# ============================================================================
# 4. ConversationLog 对话历史日志
# ============================================================================

class ConversationLog(Base):
    __tablename__ = "conversation_log"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    userid = sa.Column(sa.String(64), nullable=False, comment="external_userid")
    direction = sa.Column(
        sa.Enum("in", "out", name="msg_direction"),
        nullable=False, comment="in=用户发 out=系统回",
    )
    msg_type = sa.Column(
        sa.Enum("text", "image", "voice", "system", name="conv_msg_type"),
        nullable=False,
    )
    content = sa.Column(mysql.MEDIUMTEXT, nullable=False, comment="文本内容 or 媒体 key")
    wecom_msg_id = sa.Column(sa.String(64), nullable=True, unique=True, comment="企微消息 ID（幂等 L3 防线）")
    intent = sa.Column(sa.String(32), nullable=True, comment="识别意图")
    criteria_snapshot = sa.Column(sa.JSON, nullable=True, comment="本轮 criteria 快照")
    recommendation_delivery_id = sa.Column(sa.String(36), nullable=True)
    redaction_state = sa.Column(sa.String(24), nullable=True)
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())
    expires_at = sa.Column(sa.DateTime, nullable=False, comment="默认 created_at + 30 天")

    __table_args__ = (
        sa.Index("idx_user_time", "userid", "created_at"),
        sa.Index("idx_expires", "expires_at"),
        # §10.1.1: deletion walks back from a delivery to every other user's
        # recommendation log, so this lookup must not be a full table scan.
        sa.Index("idx_conversation_recommendation_delivery", "recommendation_delivery_id"),
    )


# ============================================================================
# 5. AuditLog 审核动作日志
# ============================================================================

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    target_type = sa.Column(
        sa.Enum("job", "resume", "user", "system", "recommendation_strategy", name="audit_target_type"),
        nullable=False, comment="审核对象类型（system=系统配置）",
    )
    target_id = sa.Column(sa.String(64), nullable=False, comment="目标 ID")
    action = sa.Column(
        sa.Enum(
            "auto_pass", "auto_reject",
            "manual_pass", "manual_reject",
            "manual_edit", "undo",
            "appeal", "reinstate",
            "strategy_publish", "strategy_rollout", "strategy_promote",
            "strategy_rollback", "strategy_kill_switch",
            name="audit_action",
        ),
        nullable=False,
    )
    reason = sa.Column(sa.String(255), nullable=True, comment="动作原因")
    operator = sa.Column(sa.String(64), nullable=True, comment="操作人")
    snapshot = sa.Column(sa.JSON, nullable=True, comment="动作发生时的对象快照")
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())

    __table_args__ = (
        sa.Index("idx_target", "target_type", "target_id"),
        sa.Index("idx_time", "created_at"),
    )


# ============================================================================
# 6. DictCity 城市字典
# ============================================================================

class DictCity(Base):
    __tablename__ = "dict_city"

    id = sa.Column(mysql.INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    code = sa.Column(sa.String(16), nullable=False, unique=True, comment="国家统计局行政区划代码（6 位）")
    name = sa.Column(sa.String(32), nullable=False, comment="地级市规范名")
    short_name = sa.Column(sa.String(32), nullable=True, comment="简称")
    province = sa.Column(sa.String(32), nullable=False, comment="所属省份")
    aliases = sa.Column(sa.JSON, nullable=True, comment="别名数组")
    enabled = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("1"))
    updated_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())

    __table_args__ = (
        sa.Index("idx_name", "name"),
        sa.Index("idx_province", "province"),
    )


# ============================================================================
# 7. DictJobCategory 工种大类字典
# ============================================================================

class DictJobCategory(Base):
    __tablename__ = "dict_job_category"

    id = sa.Column(mysql.INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    code = sa.Column(sa.String(32), nullable=False, unique=True, comment="内部代码")
    name = sa.Column(sa.String(32), nullable=False, unique=True, comment="显示名")
    aliases = sa.Column(sa.JSON, nullable=True, comment="别名数组")
    sort_order = sa.Column(sa.Integer, nullable=False, server_default=sa.text("0"), comment="排序权重")
    enabled = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("1"))
    updated_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())


# ============================================================================
# 8. DictSensitiveWord 敏感词字典
# ============================================================================

class DictSensitiveWord(Base):
    __tablename__ = "dict_sensitive_word"

    id = sa.Column(mysql.INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    word = sa.Column(sa.String(64), nullable=False, unique=True, comment="敏感词")
    level = sa.Column(
        sa.Enum("high", "mid", "low", name="sensitive_level"),
        nullable=False, server_default="mid", comment="high=直接拒 mid=灰度 low=仅打标",
    )
    category = sa.Column(sa.String(32), nullable=True, comment="分类")
    enabled = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("1"))
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())

    __table_args__ = (
        sa.Index("idx_level_enabled", "level", "enabled"),
    )


# ============================================================================
# 9. SystemConfig 系统配置
# ============================================================================

class SystemConfig(Base):
    __tablename__ = "system_config"

    config_key = sa.Column(sa.String(64), primary_key=True, comment="配置键")
    config_value = sa.Column(sa.Text, nullable=False, comment="配置值（字符串 / JSON 字符串）")
    value_type = sa.Column(
        sa.Enum("string", "int", "bool", "json", name="config_value_type"),
        nullable=False, server_default="string",
    )
    description = sa.Column(sa.String(255), nullable=True, comment="配置说明")
    updated_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())
    updated_by = sa.Column(sa.String(64), nullable=True, comment="最近修改人")


# ============================================================================
# 10. AdminUser 运营管理员账号
# ============================================================================

class AdminUser(Base):
    __tablename__ = "admin_user"

    id = sa.Column(mysql.INTEGER(unsigned=True), primary_key=True, autoincrement=True)
    username = sa.Column(sa.String(32), nullable=False, unique=True, comment="登录用户名")
    password_hash = sa.Column(sa.String(128), nullable=False, comment="bcrypt 哈希")
    display_name = sa.Column(sa.String(64), nullable=True, comment="显示名")
    # §9.10 grandfathers *existing* accounts into super_admin via phase9_004; the
    # column default stays least-privileged so new accounts must pick a role.
    role = sa.Column(
        sa.Enum("viewer", "operator", "super_admin", name="admin_role"),
        nullable=False,
        server_default="viewer",
    )
    password_changed = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("0"), comment="是否已修改初始密码")
    enabled = sa.Column(mysql.TINYINT(display_width=1), nullable=False, server_default=sa.text("1"))
    last_login_at = sa.Column(sa.DateTime, nullable=True)
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())


# ============================================================================
# 11. EventLog 小程序点击等外部事件回传日志（Phase 5 新增）
# ============================================================================

class EventLog(Base):
    __tablename__ = "event_log"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    event_type = sa.Column(
        sa.Enum("miniprogram_click", name="event_type"),
        nullable=False, comment="事件类型",
    )
    userid = sa.Column(sa.String(64), nullable=False, comment="external_userid")
    target_type = sa.Column(
        sa.Enum("job", "resume", name="event_target_type"),
        nullable=False, comment="点击目标类型",
    )
    target_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False, comment="目标主键")
    occurred_at = sa.Column(sa.DateTime, nullable=False, comment="客户端上报的发生时间")
    extra = sa.Column(sa.JSON, nullable=True, comment="扩展字段（版本号 / 来源页面等）")
    delivery_id = sa.Column(sa.String(36), nullable=True)
    request_id = sa.Column(sa.String(36), nullable=True)
    snapshot_id = sa.Column(sa.String(36), nullable=True)
    position = sa.Column(mysql.SMALLINT(unsigned=True), nullable=True)
    attribution_status = sa.Column(
        sa.Enum("attributed", "legacy_unattributed", "rejected", name="attribution_status"),
        nullable=False,
        server_default="legacy_unattributed",
    )
    attributed_strategy_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    attributed_algorithm_version = sa.Column(sa.String(32), nullable=True)
    attributed_is_exploration = sa.Column(mysql.TINYINT(1), nullable=True)
    client_event_id = sa.Column(sa.String(64), nullable=True)
    attribution_dedupe_key = sa.Column(sa.String(64), nullable=True, unique=True)
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())

    __table_args__ = (
        sa.Index("idx_target", "target_type", "target_id", "occurred_at"),
        sa.Index("idx_user_time", "userid", "occurred_at"),
        sa.Index("idx_event_delivery_target", "delivery_id", "target_type", "target_id"),
        sa.Index("idx_event_attributed_version", "attributed_strategy_version_id", "event_type", "occurred_at"),
        sa.Index("idx_event_attribution_status", "attribution_status", "occurred_at"),
        sa.UniqueConstraint(
            "userid", "event_type", "client_event_id",
            name="uk_event_client_idempotency",
        ),
    )


# ============================================================================
# 12. WecomInboundEvent 企微入站事件表
# ============================================================================

class WecomInboundEvent(Base):
    __tablename__ = "wecom_inbound_event"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    msg_id = sa.Column(sa.String(64), nullable=False, unique=True, comment="企微消息 ID，幂等键")
    turn_id = sa.Column(
        sa.String(36), nullable=False, unique=True,
        default=lambda: str(uuid4()),
        comment="不可变入站轮次 ID；重试复用，人工重放新建",
    )
    source_channel = sa.Column(sa.String(24), nullable=False, server_default="wecom_app", comment="入站渠道")
    provider_msg_id = sa.Column(sa.String(128), nullable=True, comment="供应商原始消息 ID，不截断")
    dedupe_key = sa.Column(sa.String(64), nullable=True, comment="跨渠道规范幂等键 SHA-256")
    from_userid = sa.Column(sa.String(64), nullable=True, comment="发送者 external_userid 或 opaque actor")
    conversation_type = sa.Column(sa.String(16), nullable=False, server_default="single")
    conversation_id = sa.Column(sa.String(128), nullable=True, comment="单聊 userid 或群聊 chat_id")
    chat_id = sa.Column(sa.String(128), nullable=True, comment="群聊目标 ID")
    ordering_key = sa.Column(sa.String(192), nullable=True, comment="锁、顺序门禁和出站统一键")
    provider_req_id = sa.Column(sa.String(128), nullable=True, comment="AIBot callback headers.req_id")
    aibot_id = sa.Column(sa.String(128), nullable=True)
    actor_id_kind = sa.Column(sa.String(16), nullable=False, server_default="plain")
    from_userid = sa.Column(sa.String(64), nullable=True, comment="发送者 external_userid 或 opaque actor")
    msg_type = sa.Column(
        sa.Enum(
            "text", "image", "voice",
            "video", "file", "link", "location",
            "event", "other",
            name="wecom_msg_type",
        ),
        nullable=False,
        comment="原始企微 MsgType；一期仅 text/image/voice/event 走业务路径，其余走不支持分支",
    )
    media_id = sa.Column(
        sa.String(128), nullable=True,
        comment="媒体消息的 media_id（image/voice/video/file 有效），用于 Worker crash 后补下载",
    )
    media_url_ciphertext = sa.Column(mysql.MEDIUMBLOB, nullable=True, comment="加密的短时媒体 URL")
    media_aes_key_ciphertext = sa.Column(mysql.VARBINARY(512), nullable=True, comment="加密的媒体 aeskey")
    media_expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    media_storage_ref = sa.Column(sa.String(512), nullable=True, comment="下载后的安全对象引用")
    media_download_status = sa.Column(sa.String(24), nullable=False, server_default="pending")
    media_download_attempts = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    content_brief = sa.Column(sa.String(500), nullable=True, comment="消息摘要（文本取前 500 字）")
    status = sa.Column(
        sa.Enum(
            "received", "processing", "session_pending",
            "done", "failed", "dead_letter",
            name="wecom_event_status",
        ),
        nullable=False, server_default="received", comment="处理状态",
    )
    rate_limit_decision = sa.Column(
        sa.Enum("accepted", "rate_limited", name="rate_limit_decision"),
        nullable=False, server_default="accepted", comment="限流审计决策",
    )
    rate_limit_rule = sa.Column(
        sa.String(128), nullable=True, comment="限流规则/版本（仅审计）",
    )
    rate_limited_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=True, comment="限流决策时间",
    )
    dispatcher_lease_owner = sa.Column(
        sa.String(64), nullable=True, comment="入站 dispatcher 当前 owner",
    )
    dispatcher_lease_expires_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=True, comment="入站 dispatcher lease 到期时间",
    )
    retry_count = sa.Column(mysql.TINYINT(unsigned=True), nullable=False, server_default=sa.text("0"), comment="已重试次数")
    session_operation = sa.Column(sa.String(8), nullable=True)
    session_expected_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    session_payload = sa.Column(sa.JSON, nullable=True)
    session_apply_attempts = sa.Column(
        mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"),
    )
    session_apply_locked_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    session_apply_lease_owner = sa.Column(sa.String(64), nullable=True)
    session_next_attempt_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    session_commit_deadline_epoch = sa.Column(sa.Numeric(20, 6), nullable=True)
    session_applied_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    worker_started_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=True, comment="Worker 开始处理时间",
    )
    worker_finished_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=True, comment="Worker 处理完成时间",
    )
    error_message = sa.Column(sa.Text, nullable=True, comment="失败原因")
    created_at = sa.Column(
        mysql.DATETIME(fsp=6),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP(6)"),
        comment="回调到达时间",
    )

    __table_args__ = (
        sa.Index("idx_status_time", "status", "created_at"),
        sa.Index(
            "idx_inbound_dispatch", "status", "rate_limit_decision", "created_at", "id",
        ),
        sa.Index(
            "idx_inbound_dispatch_lease",
            "status", "rate_limit_decision", "dispatcher_lease_expires_at", "id",
        ),
        sa.Index("idx_status_worker_started", "status", "worker_started_at"),
        sa.Index("idx_status_worker_finished", "status", "worker_finished_at"),
        sa.Index("idx_from_user", "from_userid", "created_at"),
        sa.Index("idx_user_status_id", "from_userid", "status", "id"),
        sa.Index("idx_inbound_ordering_status", "ordering_key", "status", "id"),
        sa.UniqueConstraint("source_channel", "provider_msg_id", name="uk_inbound_channel_provider"),
        sa.UniqueConstraint("dedupe_key", name="uk_inbound_dedupe_key"),
        sa.Index(
            "idx_session_commit_due",
            "status", "session_next_attempt_at", "session_apply_locked_at", "id",
        ),
    )


# ============================================================================
# 13. ActionExecution Action 幂等执行凭据（Job Search v1）
# ============================================================================

class ActionExecution(Base):
    """Durable idempotency and lease/fencing record for one turn action.

    ``turn_id`` + ``action_name`` is the stable idempotency key.  A worker may
    only finalize a started row while it still owns both the lease and its
    fencing token; retries reuse the same row and saved result digests.
    """

    __tablename__ = "action_execution"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    turn_id = sa.Column(sa.String(36), nullable=False, comment="不可变入站轮次 ID")
    actor_userid = deferred(sa.Column(sa.String(64), nullable=True, comment="Action actor 绑定"))
    action_name = sa.Column(sa.String(64), nullable=False, comment="稳定 Action 名称")
    status = sa.Column(
        sa.Enum(
            "started", "succeeded", "failed_retryable", "failed_terminal",
            name="action_execution_status",
        ),
        nullable=False,
        server_default="started",
        comment="Action 执行状态",
    )
    request_digest = sa.Column(mysql.CHAR(64), nullable=True, comment="规范化请求 SHA-256")
    result_digest = sa.Column(mysql.CHAR(64), nullable=True, comment="结果/快照 SHA-256")
    action_version = deferred(sa.Column(sa.String(32), nullable=False, server_default="v1", comment="Action 契约版本"))
    result_ref_type = deferred(sa.Column(sa.String(32), nullable=True, comment="结果引用类型"))
    request_id = deferred(sa.Column(sa.String(36), nullable=True, comment="推荐 request 引用"))
    snapshot_id = deferred(sa.Column(sa.String(36), nullable=True, comment="最终 snapshot 引用"))
    delivery_ids = deferred(sa.Column(sa.JSON, nullable=True, comment="RecommendationDelivery 主键集合"))
    outbox_ids = deferred(sa.Column(sa.JSON, nullable=True, comment="Outbox 主键集合"))
    session_commit_id = deferred(sa.Column(sa.String(36), nullable=True, comment="durable Session commit 引用"))
    result_schema_version = deferred(sa.Column(sa.String(32), nullable=True, comment="结果引用 schema 版本"))
    failure_code = deferred(sa.Column(sa.String(64), nullable=True, comment="稳定失败原因码"))
    replay_count = deferred(sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0")))
    last_replayed_at = deferred(sa.Column(mysql.DATETIME(fsp=6), nullable=True))
    parse_ref = deferred(sa.Column(sa.String(36), nullable=True, comment="ActionParseArtifact 引用"))
    parse_digest = deferred(sa.Column(mysql.CHAR(64), nullable=True, comment="解析产物 SHA-256"))
    parse_version = deferred(sa.Column(sa.String(32), nullable=True, comment="解析 schema 版本"))
    parse_expires_at = deferred(sa.Column(mysql.DATETIME(fsp=6), nullable=True, comment="解析产物过期时间"))
    lease_owner = sa.Column(sa.String(64), nullable=True, comment="当前 Worker owner")
    lease_until = sa.Column(
        mysql.DATETIME(fsp=6), nullable=True, comment="当前 lease 到期时间；过期后才可抢占",
    )
    fencing_token = sa.Column(
        mysql.BIGINT(unsigned=True), nullable=False, server_default=sa.text("1"),
        comment="每次过期抢占递增的 fencing token",
    )
    created_at = sa.Column(
        mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"),
    )
    finished_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("turn_id", "action_name", name="uk_action_execution_turn_action"),
        sa.Index("idx_action_execution_claim", "status", "lease_until", "id"),
        sa.Index("idx_action_execution_turn", "turn_id", "id"),
        sa.Index("idx_action_execution_request_snapshot", "request_id", "snapshot_id"),
        sa.Index("idx_action_execution_replay", "status", "last_replayed_at", "id"),
    )


class ActionParseArtifact(Base):
    """PII-free, short-lived parse artifact shared by Gateway and Router."""

    __tablename__ = "action_parse_artifact"

    parse_ref = sa.Column(sa.String(36), primary_key=True)
    turn_id = sa.Column(sa.String(36), nullable=False)
    actor_userid = sa.Column(sa.String(64), nullable=False)
    parse_digest = sa.Column(mysql.CHAR(64), nullable=False)
    schema_version = sa.Column(sa.String(32), nullable=False)
    classifier_version = sa.Column(sa.String(64), nullable=False)
    session_version = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    payload = sa.Column(sa.JSON, nullable=False)
    expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())

    __table_args__ = (
        sa.UniqueConstraint("turn_id", "parse_digest", name="uk_action_parse_turn_digest"),
        sa.Index("idx_action_parse_expires", "expires_at", "parse_ref"),
        sa.Index("idx_action_parse_turn", "turn_id", "created_at"),
    )


# ============================================================================
# 14. WecomOutboundOutbox 企微出站事务箱
# ============================================================================

class WecomOutboundOutbox(Base):
    """与入站业务事务一起提交、由 Worker 异步投递的回复。

    企微 message/send 不支持客户端幂等键，因此该表保证“回复意图不丢、业务路由
    不重跑”；若 HTTP 响应丢失而企微实际已接收，重试仍可能产生重复消息。
    """

    __tablename__ = "wecom_outbound_outbox"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    inbound_event_id = sa.Column(
        mysql.BIGINT(unsigned=True), nullable=False,
        comment="来源 wecom_inbound_event.id",
    )
    reply_index = sa.Column(
        mysql.SMALLINT(unsigned=True), nullable=False,
        comment="同一入站事件内的回复顺序，从 0 开始",
    )
    userid = sa.Column(sa.String(64), nullable=True, comment="接收者 external_userid；群聊可空")
    channel = sa.Column(sa.String(24), nullable=False, server_default="wecom_app")
    conversation_type = sa.Column(sa.String(16), nullable=False, server_default="single")
    conversation_id = sa.Column(sa.String(128), nullable=True)
    chat_id = sa.Column(sa.String(128), nullable=True)
    ordering_key = sa.Column(sa.String(192), nullable=True)
    provider_req_id = sa.Column(sa.String(128), nullable=True)
    reply_command = sa.Column(sa.String(40), nullable=True)
    stream_id = sa.Column(sa.String(128), nullable=True)
    finish = sa.Column(mysql.TINYINT(1), nullable=True)
    msg_type = sa.Column(sa.String(16), nullable=False, server_default="text")
    content = sa.Column(mysql.MEDIUMTEXT, nullable=True)
    recommendation_delivery_id = sa.Column(sa.String(36), nullable=True, unique=True)
    contact_delivery_id = sa.Column(sa.String(64), nullable=True, unique=True, comment="ContactDelivery 引用，不存联系方式")
    intent = sa.Column(sa.String(32), nullable=True)
    criteria_snapshot = sa.Column(sa.JSON, nullable=True)
    status = sa.Column(
        sa.Enum(
            "pending", "sending", "sent", "uncertain", "dead_letter",
            name="wecom_outbox_status",
        ),
        nullable=False,
        server_default="pending",
    )
    attempt_count = sa.Column(
        mysql.TINYINT(unsigned=True), nullable=False, server_default=sa.text("0"),
    )
    next_attempt_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    locked_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    provider_msg_id = sa.Column(sa.String(128), nullable=True)
    provider_response = sa.Column(sa.JSON, nullable=True)
    reply_expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    stream_deadline_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    ack_req_id = sa.Column(sa.String(128), nullable=True)
    ack_received_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    first_sent_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    uncertain_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    provider_close_code = sa.Column(sa.String(32), nullable=True)
    lease_owner = sa.Column(sa.String(64), nullable=True)
    fencing_token = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    last_error = sa.Column(sa.Text, nullable=True)
    created_at = sa.Column(
        mysql.DATETIME(fsp=6),
        nullable=False,
        server_default=sa.text("CURRENT_TIMESTAMP(6)"),
    )
    sent_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint(
            "inbound_event_id", "reply_index",
            name="uk_outbox_event_reply",
        ),
        sa.Index("idx_outbox_status_due", "status", "next_attempt_at", "id"),
        sa.Index("idx_outbox_status_locked", "status", "locked_at"),
        sa.Index("idx_outbox_event", "inbound_event_id", "id"),
        sa.Index("idx_outbox_user_status_id", "userid", "status", "id"),
        sa.Index("idx_outbox_contact_delivery", "status", "contact_delivery_id", "id"),
        sa.CheckConstraint(
            "NOT (recommendation_delivery_id IS NOT NULL AND contact_delivery_id IS NOT NULL)",
            name="ck_outbox_single_delivery_kind",
        ),
        sa.Index("idx_outbox_channel_status_due", "channel", "status", "next_attempt_at", "id"),
        sa.Index("idx_outbox_ordering_status", "ordering_key", "status", "id"),
    )


class WecomAibotIdentity(Base):
    """Opaque AIBot actor IDs and their explicitly verified mappings."""

    __tablename__ = "wecom_aibot_identity"

    opaque_actor_id = sa.Column(sa.String(128), primary_key=True)
    bot_id = sa.Column(sa.String(128), nullable=False, server_default="")
    actor_id_kind = sa.Column(sa.Enum("plain", "open_userid", name="aibot_actor_id_kind"), nullable=False, server_default="open_userid")
    opaque_actor_digest = sa.Column(mysql.CHAR(64), nullable=True)
    mapped_external_userid = sa.Column(sa.String(64), nullable=True)
    canonical_userid = sa.Column(sa.String(64), nullable=True)
    identity_status = sa.Column(
        sa.Enum("unverified", "conversion_pending", "verified", "rejected", "revoked", name="wecom_aibot_identity_status"),
        nullable=False, server_default="unverified",
    )
    resolution_attempts = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    next_resolution_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    last_error_code = sa.Column(sa.String(64), nullable=True)
    last_error_digest = sa.Column(mysql.CHAR(64), nullable=True)
    source_msg_id = sa.Column(sa.String(128), nullable=True)
    first_seen_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    last_seen_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    verified_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    revoked_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"))
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"), onupdate=sa.func.now())

    __table_args__ = (
        sa.Index("idx_aibot_identity_mapped", "mapped_external_userid"),
        sa.Index("idx_aibot_identity_status_due", "identity_status", "next_resolution_at"),
        sa.Index("idx_aibot_identity_canonical", "canonical_userid"),
        sa.UniqueConstraint("bot_id", "opaque_actor_digest", name="uk_aibot_identity_bot_digest"),
        sa.UniqueConstraint("bot_id", "canonical_userid", name="uk_aibot_identity_bot_canonical"),
    )


class AibotIdentityBinding(Base):
    """Explicit channel-to-business identity binding; never a role grant."""

    __tablename__ = "aibot_identity_binding"

    binding_id = sa.Column(sa.String(36), primary_key=True)
    bot_id = sa.Column(sa.String(128), nullable=False)
    opaque_actor_digest = sa.Column(mysql.CHAR(64), nullable=False)
    canonical_userid = sa.Column(sa.String(64), sa.ForeignKey("user.external_userid"), nullable=False)
    binding_status = sa.Column(sa.Enum("pending", "active", "rejected", "revoked", name="aibot_binding_status"), nullable=False, server_default="pending")
    binding_source = sa.Column(sa.Enum("auto_verified", "invite", "pre_registered", "admin", name="aibot_binding_source"), nullable=False, server_default="auto_verified")
    invite_id = sa.Column(sa.String(36), nullable=True)
    approved_by = sa.Column(sa.String(64), nullable=True)
    approved_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    revoked_by = sa.Column(sa.String(64), nullable=True)
    revoked_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1"))
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"))
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"), onupdate=sa.func.now())

    __table_args__ = (
        sa.UniqueConstraint("bot_id", "opaque_actor_digest", "binding_status", name="uk_aibot_binding_identity_status"),
        sa.Index("idx_aibot_binding_canonical", "bot_id", "canonical_userid", "binding_status"),
    )


class AibotRegistration(Base):
    __tablename__ = "aibot_registration"

    registration_id = sa.Column(sa.String(36), primary_key=True)
    canonical_userid = sa.Column(sa.String(64), sa.ForeignKey("user.external_userid"), nullable=True)
    identity_binding_id = sa.Column(sa.String(36), nullable=False)
    registration_status = sa.Column(sa.Enum("discovered", "pending_role", "active", "rejected", "revoked", name="aibot_registration_status"), nullable=False, server_default="discovered")
    registration_source = sa.Column(sa.Enum("auto_worker", "pre_registered", "invite", "admin", name="aibot_registration_source"), nullable=False, server_default="auto_worker")
    requested_role = sa.Column(sa.Enum("worker", "factory", "broker", name="aibot_requested_role"), nullable=True)
    granted_role = sa.Column(sa.Enum("worker", "factory", "broker", name="aibot_granted_role"), nullable=True)
    capability_snapshot = sa.Column(sa.JSON, nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"))
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"), onupdate=sa.func.now())

    __table_args__ = (sa.Index("idx_aibot_registration_user_status", "canonical_userid", "registration_status"),)


class AibotRoleInvite(Base):
    __tablename__ = "aibot_role_invite"

    invite_id = sa.Column(sa.String(36), primary_key=True)
    token_digest = sa.Column(mysql.CHAR(64), nullable=False, unique=True)
    target_role = sa.Column(sa.Enum("factory", "broker", name="aibot_invite_role"), nullable=False)
    expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False)
    max_uses = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1"))
    used_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    created_by = sa.Column(sa.String(64), nullable=False)
    revoked_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"))

    __table_args__ = (sa.Index("idx_aibot_invite_active", "target_role", "expires_at", "revoked_at"),)


class AibotIdentityAudit(Base):
    __tablename__ = "aibot_identity_audit"

    audit_id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    bot_id = sa.Column(sa.String(128), nullable=False)
    opaque_actor_digest = sa.Column(mysql.CHAR(64), nullable=False)
    canonical_userid = sa.Column(sa.String(64), nullable=True)
    action = sa.Column(sa.String(48), nullable=False)
    result = sa.Column(sa.String(32), nullable=False)
    reason_code = sa.Column(sa.String(64), nullable=True)
    actor = sa.Column(sa.String(64), nullable=True)
    metadata = sa.Column(sa.JSON, nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"))

    __table_args__ = (sa.Index("idx_aibot_identity_audit_lookup", "bot_id", "opaque_actor_digest", "created_at"),)


# ============================================================================
# Recommendation v1 facts and governance
# ============================================================================

class RecommendationStrategyVersion(Base):
    __tablename__ = "recommendation_strategy_version"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    direction = sa.Column(sa.String(32), nullable=False)
    version_no = sa.Column(mysql.INTEGER(unsigned=True), nullable=False)
    template_key = sa.Column(sa.String(32), nullable=False)
    status = sa.Column(sa.Enum("draft", "published", "archived", name="recommendation_version_status"), nullable=False, server_default="draft")
    parameters = sa.Column(sa.JSON, nullable=False)
    parameters_digest = sa.Column(sa.String(64), nullable=False)
    last_simulated_digest = sa.Column(sa.String(64), nullable=True)
    last_simulated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    algorithm_version = sa.Column(sa.String(32), nullable=False, server_default="recommendation-v1")
    base_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    lock_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1"))
    change_reason = sa.Column(sa.String(255), nullable=False)
    created_by = sa.Column(sa.String(64), nullable=False)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())
    published_by = sa.Column(sa.String(64), nullable=True)
    published_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)

    __table_args__ = (
        sa.UniqueConstraint("direction", "version_no", name="uk_recommendation_version_direction_no"),
        sa.Index("idx_recommendation_version_status", "direction", "status"),
    )


class RecommendationStrategyRelease(Base):
    __tablename__ = "recommendation_strategy_release"

    direction = sa.Column(sa.String(32), primary_key=True)
    execution_mode = sa.Column(sa.Enum("off", "shadow", "on", name="recommendation_execution_mode"), nullable=False, server_default="off")
    stable_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    candidate_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    rollout_percentage = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    revision = sa.Column(mysql.BIGINT(unsigned=True), nullable=False, server_default=sa.text("1"))
    lock_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1"))
    updated_by = sa.Column(sa.String(64), nullable=False, server_default="system")
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())


class RecommendationReleaseHistory(Base):
    __tablename__ = "recommendation_release_history"

    direction = sa.Column(sa.String(32), primary_key=True)
    revision = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True)
    operation = sa.Column(sa.String(32), nullable=False)
    execution_mode = sa.Column(sa.String(16), nullable=False)
    stable_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    candidate_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    rollout_percentage = sa.Column(mysql.INTEGER(unsigned=True), nullable=False)
    target_revision = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    change_reason = sa.Column(sa.String(255), nullable=False)
    created_by = sa.Column(sa.String(64), nullable=False)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())


class RecommendationRuntimeControl(Base):
    __tablename__ = "recommendation_runtime_control"

    scope = sa.Column(sa.String(16), primary_key=True)
    kill_switch = sa.Column(mysql.TINYINT(1), nullable=False, server_default=sa.text("0"))
    revision = sa.Column(mysql.BIGINT(unsigned=True), nullable=False, server_default=sa.text("1"))
    lock_version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1"))
    change_reason = sa.Column(sa.String(255), nullable=False, server_default="initial")
    updated_by = sa.Column(sa.String(64), nullable=False, server_default="system")
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())


class RecommendationRequest(Base):
    __tablename__ = "recommendation_request"

    request_id = sa.Column(sa.String(36), primary_key=True)
    source_inbound_msg_id = sa.Column(sa.String(64), nullable=False)
    request_index = sa.Column(mysql.SMALLINT(unsigned=True), nullable=False, server_default=sa.text("0"))
    request_kind = sa.Column(sa.String(32), nullable=False)
    parent_request_id = sa.Column(sa.String(36), nullable=True)
    served_attempt_id = sa.Column(sa.String(36), nullable=True)
    snapshot_id = sa.Column(sa.String(36), nullable=True)
    viewer_userid = sa.Column(sa.String(64), nullable=False)
    direction = sa.Column(sa.String(32), nullable=False)
    query_digest = sa.Column(sa.String(16), nullable=False)
    execution_mode = sa.Column(sa.String(16), nullable=False)
    served_assignment = sa.Column(sa.String(16), nullable=False)
    served_strategy_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    candidate_strategy_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    algorithm_version = sa.Column(sa.String(32), nullable=False)
    final_candidate_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    result_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    is_zero_result = sa.Column(mysql.TINYINT(1), nullable=False, server_default=sa.text("0"))
    show_more_exhausted = sa.Column(mysql.TINYINT(1), nullable=False, server_default=sa.text("0"))
    total_latency_ms = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    served_top_ids = sa.Column(sa.JSON, nullable=False)
    served_owner_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    served_max_owner_items = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    served_exploration_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    shadow_top_ids = sa.Column(sa.JSON, nullable=True)
    shadow_overlap_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    shadow_rank_delta = sa.Column(sa.JSON, nullable=True)
    shadow_status = sa.Column(sa.String(32), nullable=True)
    shadow_queue_wait_ms = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    shadow_latency_ms = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    shadow_input_tokens = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    shadow_output_tokens = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    shadow_fallback = sa.Column(sa.String(32), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())

    __table_args__ = (
        sa.UniqueConstraint("source_inbound_msg_id", "request_index", name="uk_recommendation_request_inbound_index"),
        sa.Index("idx_recommendation_request_viewer_time", "viewer_userid", "direction", "created_at"),
        sa.Index("idx_recommendation_request_attempt", "served_attempt_id"),
        sa.Index("idx_recommendation_request_parent", "parent_request_id"),
        sa.Index("idx_recommendation_request_mode_time", "created_at", "direction", "execution_mode"),
        sa.Index("idx_recommendation_request_kind_zero", "request_kind", "is_zero_result", "created_at"),
        sa.Index("idx_recommendation_request_version_time", "served_strategy_version_id", "created_at"),
    )


class RecommendationSearchAttempt(Base):
    __tablename__ = "recommendation_search_attempt"

    attempt_id = sa.Column(sa.String(36), primary_key=True)
    request_id = sa.Column(sa.String(36), nullable=False)
    attempt_no = sa.Column(mysql.SMALLINT(unsigned=True), nullable=False)
    attempt_kind = sa.Column(sa.String(32), nullable=False)
    criteria_digest = sa.Column(sa.String(64), nullable=False)
    scoring_time_utc = sa.Column(mysql.DATETIME(fsp=6), nullable=False)
    candidate_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    candidate_ids = sa.Column(sa.JSON, nullable=False)
    precision_pool_ids = sa.Column(sa.JSON, nullable=False)
    result_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    is_zero_result = sa.Column(mysql.TINYINT(1), nullable=False, server_default=sa.text("0"))
    strategy_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    algorithm_version = sa.Column(sa.String(32), nullable=False)
    llm_status = sa.Column(sa.String(32), nullable=False, server_default="skipped")
    llm_input_tokens = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    llm_output_tokens = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    llm_timeout_budget_ms = sa.Column(mysql.INTEGER(unsigned=True), nullable=True)
    llm_retry_count = sa.Column(mysql.SMALLINT(unsigned=True), nullable=False, server_default=sa.text("0"))
    ranking_fallback = sa.Column(sa.String(32), nullable=True)
    ranking_latency_ms = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    total_latency_ms = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())

    __table_args__ = (
        sa.UniqueConstraint("request_id", "attempt_no", name="uk_recommendation_attempt_request_no"),
        sa.Index("idx_recommendation_attempt_kind_time", "created_at", "attempt_kind"),
        sa.Index("idx_recommendation_attempt_version_time", "strategy_version_id", "created_at"),
        sa.Index("idx_recommendation_attempt_llm_status", "llm_status", "created_at"),
    )


class RecommendationDelivery(Base):
    __tablename__ = "recommendation_delivery"

    delivery_id = sa.Column(sa.String(36), primary_key=True)
    delivery_order = sa.Column(
        mysql.BIGINT(unsigned=True), nullable=False, unique=True, autoincrement=True,
    )
    source_inbound_msg_id = sa.Column(sa.String(64), nullable=False)
    reply_index = sa.Column(mysql.SMALLINT(unsigned=True), nullable=False)
    request_id = sa.Column(sa.String(36), nullable=False)
    snapshot_id = sa.Column(sa.String(36), nullable=True)
    userid = sa.Column(sa.String(64), nullable=False)
    content_ciphertext = sa.Column(mysql.MEDIUMBLOB, nullable=True)
    content_key_version = sa.Column(mysql.SMALLINT(unsigned=True), nullable=False, server_default=sa.text("1"))
    content_hash = sa.Column(sa.String(64), nullable=True)
    content_expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    recommendation_context = sa.Column(sa.JSON, nullable=False)
    status = sa.Column(sa.String(24), nullable=False, server_default="prepared")
    session_expected_version = sa.Column(mysql.BIGINT(unsigned=True), nullable=False, server_default=sa.text("0"))
    session_commit_token = sa.Column(sa.String(36), nullable=False)
    session_patch_ciphertext = sa.Column(mysql.MEDIUMBLOB, nullable=True)
    session_commit_state = sa.Column(sa.String(16), nullable=False, server_default="not_applied")
    session_committed_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    attempt_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    next_attempt_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())
    lease_owner = sa.Column(sa.String(64), nullable=True)
    lease_expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    wecom_msgid = sa.Column(sa.String(128), nullable=True)
    wecom_response = sa.Column(sa.JSON, nullable=True)
    invalid_recipients = sa.Column(sa.JSON, nullable=True)
    last_error_code = sa.Column(sa.String(32), nullable=True)
    last_error = sa.Column(sa.String(500), nullable=True)
    sent_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    impression_state = sa.Column(sa.String(24), nullable=False, server_default="pending")
    impression_expected_count = sa.Column(mysql.SMALLINT(unsigned=True), nullable=False, server_default=sa.text("0"))
    impression_actual_count = sa.Column(mysql.SMALLINT(unsigned=True), nullable=False, server_default=sa.text("0"))
    impression_attempt_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    impression_next_attempt_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())
    # §9.6: the derivation lease is deliberately separate from the send lease so
    # the deriver and the dispatcher cannot steal each other's claim.
    impression_lease_owner = sa.Column(sa.String(64), nullable=True)
    impression_lease_expires_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    impression_derived_at = sa.Column(mysql.DATETIME(fsp=6), nullable=True)
    impression_last_error = sa.Column(sa.String(500), nullable=True)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())

    __table_args__ = (
        sa.UniqueConstraint("source_inbound_msg_id", "reply_index", name="uk_recommendation_delivery_inbound_index"),
        sa.Index("idx_recommendation_delivery_user_order", "userid", "delivery_order"),
        sa.Index("idx_recommendation_delivery_status_due", "status", "next_attempt_at"),
        sa.Index("idx_recommendation_delivery_session_recovery", "status", "session_commit_state", "updated_at"),
        sa.Index("idx_recommendation_delivery_user_status_order", "userid", "status", "delivery_order"),
        sa.Index("idx_recommendation_delivery_lease", "lease_expires_at", "status"),
        sa.Index("idx_recommendation_delivery_impression_due", "status", "impression_state", "impression_next_attempt_at"),
        sa.Index("idx_recommendation_delivery_impression_lease", "impression_lease_expires_at", "impression_state"),
        sa.Index("idx_recommendation_delivery_request", "request_id"),
        sa.Index("idx_recommendation_delivery_msgid", "wecom_msgid"),
    )


class RecommendationImpression(Base):
    __tablename__ = "recommendation_impression"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    delivery_id = sa.Column(sa.String(36), nullable=False)
    request_id = sa.Column(sa.String(36), nullable=False)
    snapshot_id = sa.Column(sa.String(36), nullable=False)
    viewer_userid = sa.Column(sa.String(64), nullable=False)
    direction = sa.Column(sa.String(32), nullable=False)
    target_type = sa.Column(sa.String(16), nullable=False)
    target_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=False)
    position = sa.Column(mysql.SMALLINT(unsigned=True), nullable=False)
    strategy_version_id = sa.Column(mysql.BIGINT(unsigned=True), nullable=True)
    algorithm_version = sa.Column(sa.String(32), nullable=False)
    assignment = sa.Column(sa.String(16), nullable=False)
    is_exploration = sa.Column(mysql.TINYINT(1), nullable=False, server_default=sa.text("0"))
    query_digest = sa.Column(sa.String(16), nullable=False)
    score_detail = sa.Column(sa.JSON, nullable=True)
    exposed_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False)
    created_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now())

    __table_args__ = (
        sa.UniqueConstraint("delivery_id", "target_type", "target_id", name="uk_recommendation_impression_delivery_target"),
        sa.Index("idx_recommendation_impression_viewer_time", "viewer_userid", "target_type", "exposed_at"),
        sa.Index("idx_recommendation_impression_target_time", "target_type", "target_id", "exposed_at"),
        sa.Index("idx_recommendation_impression_version_time", "strategy_version_id", "exposed_at"),
        sa.Index("idx_recommendation_impression_snapshot_position", "snapshot_id", "position"),
    )


class RecommendationExposureDaily(Base):
    __tablename__ = "recommendation_exposure_daily"

    stat_date = sa.Column(sa.Date, primary_key=True)
    target_type = sa.Column(sa.String(16), primary_key=True)
    target_id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True)
    impression_count = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("0"))
    updated_at = sa.Column(mysql.DATETIME(fsp=6), nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())
