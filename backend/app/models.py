"""ORM 模型定义（对应 schema.sql 11 张表）。

真值来源：backend/sql/schema.sql
所有默认值、可空性、索引、唯一约束、字段类型与 DDL 保持严格一致。
目标数据库：MySQL 8.0+，因此直接使用 sqlalchemy.dialects.mysql 类型
以确保 UNSIGNED / TINYINT / MEDIUMTEXT 等与 DDL 完全对齐。
"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.mutable import MutableDict

from app.db import Base


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
    contact_person = sa.Column(sa.String(64), nullable=True, comment="联系人姓名")
    phone = sa.Column(sa.String(32), nullable=True, comment="联系电话")
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
    expires_at = sa.Column(sa.DateTime, nullable=False, comment="过期时间（默认 created_at + 30 天）")
    delist_reason = sa.Column(
        sa.Enum("filled", "manual_delist", "expired", name="delist_reason"),
        nullable=True, comment="下架原因",
    )
    deleted_at = sa.Column(sa.DateTime, nullable=True, comment="软删除时间")

    # ---- 乐观锁 ----
    version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1"), comment="乐观锁版本号")

    # ---- 扩展 ----
    extra = sa.Column(MutableDict.as_mutable(sa.JSON), nullable=True, comment="扩展字段（§7.6）")

    __table_args__ = (
        sa.Index("idx_owner", "owner_userid"),
        sa.Index("idx_audit_time", "audit_status", "created_at"),
        sa.Index("idx_expires", "expires_at"),
        sa.Index("idx_filter_hot", "city", "job_category", "is_long_term", "audit_status", "deleted_at", "expires_at"),
        sa.Index("idx_salary", "salary_floor_monthly"),
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
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())
    updated_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), onupdate=sa.func.now())
    expires_at = sa.Column(sa.DateTime, nullable=False, comment="过期时间")
    deleted_at = sa.Column(sa.DateTime, nullable=True)

    # ---- 乐观锁 ----
    version = sa.Column(mysql.INTEGER(unsigned=True), nullable=False, server_default=sa.text("1"), comment="乐观锁版本号")

    # ---- 扩展 ----
    extra = sa.Column(MutableDict.as_mutable(sa.JSON), nullable=True, comment="扩展字段")

    __table_args__ = (
        sa.Index("idx_owner", "owner_userid"),
        sa.Index("idx_audit_time", "audit_status", "created_at"),
        sa.Index("idx_expires", "expires_at"),
        sa.Index("idx_filter_hot", "gender", "age", "audit_status", "deleted_at", "expires_at"),
        sa.Index("idx_salary_exp", "salary_expect_floor_monthly"),
    )


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
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())
    expires_at = sa.Column(sa.DateTime, nullable=False, comment="默认 created_at + 30 天")

    __table_args__ = (
        sa.Index("idx_user_time", "userid", "created_at"),
        sa.Index("idx_expires", "expires_at"),
    )


# ============================================================================
# 5. AuditLog 审核动作日志
# ============================================================================

class AuditLog(Base):
    __tablename__ = "audit_log"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    target_type = sa.Column(
        sa.Enum("job", "resume", "user", name="audit_target_type"),
        nullable=False, comment="审核对象类型",
    )
    target_id = sa.Column(sa.String(64), nullable=False, comment="目标 ID")
    action = sa.Column(
        sa.Enum(
            "auto_pass", "auto_reject",
            "manual_pass", "manual_reject",
            "manual_edit", "undo",
            "appeal", "reinstate",
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
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now())

    __table_args__ = (
        sa.Index("idx_target", "target_type", "target_id", "occurred_at"),
        sa.Index("idx_user_time", "userid", "occurred_at"),
    )


# ============================================================================
# 12. WecomInboundEvent 企微入站事件表
# ============================================================================

class WecomInboundEvent(Base):
    __tablename__ = "wecom_inbound_event"

    id = sa.Column(mysql.BIGINT(unsigned=True), primary_key=True, autoincrement=True)
    msg_id = sa.Column(sa.String(64), nullable=False, unique=True, comment="企微消息 ID，幂等键")
    from_userid = sa.Column(sa.String(64), nullable=False, comment="发送者 external_userid")
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
    content_brief = sa.Column(sa.String(500), nullable=True, comment="消息摘要（文本取前 500 字）")
    status = sa.Column(
        sa.Enum("received", "processing", "done", "failed", "dead_letter", name="wecom_event_status"),
        nullable=False, server_default="received", comment="处理状态",
    )
    retry_count = sa.Column(mysql.TINYINT(unsigned=True), nullable=False, server_default=sa.text("0"), comment="已重试次数")
    worker_started_at = sa.Column(sa.DateTime, nullable=True, comment="Worker 开始处理时间")
    worker_finished_at = sa.Column(sa.DateTime, nullable=True, comment="Worker 处理完成时间")
    error_message = sa.Column(sa.Text, nullable=True, comment="失败原因")
    created_at = sa.Column(sa.DateTime, nullable=False, server_default=sa.func.now(), comment="回调到达时间")

    __table_args__ = (
        sa.Index("idx_status_time", "status", "created_at"),
        sa.Index("idx_from_user", "from_userid", "created_at"),
    )
