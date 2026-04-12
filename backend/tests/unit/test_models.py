"""ORM 模型单元测试。

验证：
- 所有模型可导入
- __tablename__ 正确
- 关键字段存在
- 元数据中包含全部 11 张表
- 列类型与 DDL 严格一致（UNSIGNED / TINYINT / MEDIUMTEXT 等）
"""
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from app.db import Base
from app.models import (
    User,
    Job,
    Resume,
    ConversationLog,
    AuditLog,
    DictCity,
    DictJobCategory,
    DictSensitiveWord,
    SystemConfig,
    AdminUser,
    WecomInboundEvent,
)

EXPECTED_TABLES = {
    "user",
    "job",
    "resume",
    "conversation_log",
    "audit_log",
    "dict_city",
    "dict_job_category",
    "dict_sensitive_word",
    "system_config",
    "admin_user",
    "wecom_inbound_event",
}

ALL_MODELS = [
    User, Job, Resume, ConversationLog, AuditLog,
    DictCity, DictJobCategory, DictSensitiveWord,
    SystemConfig, AdminUser, WecomInboundEvent,
]


class TestModelImport:
    """模型导入与表名验证。"""

    def test_all_models_importable(self):
        """所有 11 个模型类可正常导入。"""
        assert len(ALL_MODELS) == 11

    def test_tablenames(self):
        """每个模型的 __tablename__ 与 DDL 表名一致。"""
        actual = {m.__tablename__ for m in ALL_MODELS}
        assert actual == EXPECTED_TABLES

    def test_metadata_contains_all_tables(self):
        """Base.metadata 中注册了全部 11 张表。"""
        registered = set(Base.metadata.tables.keys())
        assert EXPECTED_TABLES.issubset(registered)


class TestKeyColumns:
    """关键字段 / 约束验证。"""

    @staticmethod
    def _col(model, name: str) -> sa.Column:
        return model.__table__.columns[name]

    def test_user_pk(self):
        col = self._col(User, "external_userid")
        assert col.primary_key

    def test_user_status_includes_deleted(self):
        col = self._col(User, "status")
        assert "deleted" in col.type.enums

    def test_job_version(self):
        col = self._col(Job, "version")
        assert not col.nullable

    def test_resume_version(self):
        col = self._col(Resume, "version")
        assert not col.nullable

    def test_conversation_log_wecom_msg_id_unique(self):
        col = self._col(ConversationLog, "wecom_msg_id")
        assert col.unique

    def test_wecom_inbound_event_status_enum(self):
        col = self._col(WecomInboundEvent, "status")
        expected = {"received", "processing", "done", "failed", "dead_letter"}
        assert set(col.type.enums) == expected

    def test_wecom_inbound_event_msg_id_unique(self):
        col = self._col(WecomInboundEvent, "msg_id")
        assert col.unique

    def test_job_delist_reason_enum(self):
        col = self._col(Job, "delist_reason")
        expected = {"filled", "manual_delist", "expired"}
        assert set(col.type.enums) == expected

    def test_system_config_pk(self):
        col = self._col(SystemConfig, "config_key")
        assert col.primary_key

    def test_job_foreign_key(self):
        col = self._col(Job, "owner_userid")
        assert len(col.foreign_keys) == 1

    def test_resume_foreign_key(self):
        col = self._col(Resume, "owner_userid")
        assert len(col.foreign_keys) == 1

    def test_job_hard_filter_fields_exist(self):
        """岗位硬过滤字段均存在。"""
        table = Job.__table__
        for name in ["city", "job_category", "salary_floor_monthly", "pay_type",
                      "headcount", "gender_required", "age_min", "age_max", "is_long_term"]:
            assert name in table.columns, f"Missing column: {name}"

    def test_resume_hard_filter_fields_exist(self):
        """简历硬过滤字段均存在。"""
        table = Resume.__table__
        for name in ["expected_cities", "expected_job_categories", "salary_expect_floor_monthly",
                      "gender", "age", "accept_long_term", "accept_short_term"]:
            assert name in table.columns, f"Missing column: {name}"

    def test_job_soft_match_fields_exist(self):
        """岗位关键软匹配字段均存在。"""
        table = Job.__table__
        for name in ["district", "salary_ceiling_monthly", "provide_meal", "provide_housing",
                      "shift_pattern", "education_required", "rebate", "employment_type"]:
            assert name in table.columns, f"Missing column: {name}"

    def test_resume_soft_match_fields_exist(self):
        """简历关键软匹配字段均存在。"""
        table = Resume.__table__
        for name in ["expected_districts", "height", "weight", "education",
                      "work_experience", "ethnicity", "available_from", "has_tattoo", "taboo"]:
            assert name in table.columns, f"Missing column: {name}"


# ========================================================================
# DDL 严格类型校验：验证 ORM 列类型与 schema.sql 一致
# ========================================================================

def _is_unsigned(col_type) -> bool:
    """检查列类型是否为 UNSIGNED。"""
    return getattr(col_type, "unsigned", False)


def _is_tinyint(col_type) -> bool:
    """检查列类型是否为 MySQL TINYINT。"""
    return isinstance(col_type, mysql.TINYINT)


def _is_smallint_unsigned(col_type) -> bool:
    """检查列类型是否为 MySQL SMALLINT UNSIGNED。"""
    return isinstance(col_type, mysql.SMALLINT) and getattr(col_type, "unsigned", False)


def _is_bigint_unsigned(col_type) -> bool:
    """检查列类型是否为 MySQL BIGINT UNSIGNED。"""
    return isinstance(col_type, mysql.BIGINT) and getattr(col_type, "unsigned", False)


def _is_int_unsigned(col_type) -> bool:
    """检查列类型是否为 MySQL INTEGER UNSIGNED。"""
    return isinstance(col_type, mysql.INTEGER) and getattr(col_type, "unsigned", False)


def _is_mediumtext(col_type) -> bool:
    """检查列类型是否为 MySQL MEDIUMTEXT。"""
    return isinstance(col_type, mysql.MEDIUMTEXT)


class TestDDLTypeAlignment:
    """ORM 列类型与 schema.sql DDL 严格一致性校验。

    确保 UNSIGNED / TINYINT / MEDIUMTEXT 等 MySQL 特有类型被正确映射，
    防止 Base.metadata.create_all() 生成与真实 DDL 不一致的表结构。
    """

    @staticmethod
    def _col(model, name: str):
        return model.__table__.columns[name]

    # ---- BIGINT UNSIGNED PK ----

    def test_job_id_bigint_unsigned(self):
        assert _is_bigint_unsigned(self._col(Job, "id").type)

    def test_resume_id_bigint_unsigned(self):
        assert _is_bigint_unsigned(self._col(Resume, "id").type)

    def test_conversation_log_id_bigint_unsigned(self):
        assert _is_bigint_unsigned(self._col(ConversationLog, "id").type)

    def test_audit_log_id_bigint_unsigned(self):
        assert _is_bigint_unsigned(self._col(AuditLog, "id").type)

    def test_wecom_inbound_event_id_bigint_unsigned(self):
        assert _is_bigint_unsigned(self._col(WecomInboundEvent, "id").type)

    # ---- INT UNSIGNED PK ----

    def test_dict_city_id_int_unsigned(self):
        assert _is_int_unsigned(self._col(DictCity, "id").type)

    def test_dict_job_category_id_int_unsigned(self):
        assert _is_int_unsigned(self._col(DictJobCategory, "id").type)

    def test_dict_sensitive_word_id_int_unsigned(self):
        assert _is_int_unsigned(self._col(DictSensitiveWord, "id").type)

    def test_admin_user_id_int_unsigned(self):
        assert _is_int_unsigned(self._col(AdminUser, "id").type)

    # ---- INT UNSIGNED 非PK ----

    def test_job_version_int_unsigned(self):
        assert _is_int_unsigned(self._col(Job, "version").type)

    def test_resume_version_int_unsigned(self):
        assert _is_int_unsigned(self._col(Resume, "version").type)

    # ---- TINYINT UNSIGNED ----

    def test_job_age_min_tinyint_unsigned(self):
        col_type = self._col(Job, "age_min").type
        assert _is_tinyint(col_type) and _is_unsigned(col_type)

    def test_job_age_max_tinyint_unsigned(self):
        col_type = self._col(Job, "age_max").type
        assert _is_tinyint(col_type) and _is_unsigned(col_type)

    def test_resume_age_tinyint_unsigned(self):
        col_type = self._col(Resume, "age").type
        assert _is_tinyint(col_type) and _is_unsigned(col_type)

    def test_wecom_retry_count_tinyint_unsigned(self):
        col_type = self._col(WecomInboundEvent, "retry_count").type
        assert _is_tinyint(col_type) and _is_unsigned(col_type)

    # ---- TINYINT(1) 布尔型 ----

    def test_job_boolean_fields_tinyint(self):
        """DDL 中 TINYINT(1) 的布尔字段用 mysql.TINYINT 映射。"""
        for name in ["is_long_term", "provide_meal", "provide_housing",
                      "accept_couple", "accept_student", "accept_minority"]:
            col_type = self._col(Job, name).type
            assert _is_tinyint(col_type), f"Job.{name} should be TINYINT, got {type(col_type)}"

    def test_resume_boolean_fields_tinyint(self):
        """DDL 中 TINYINT(1) 的布尔字段用 mysql.TINYINT 映射。"""
        for name in ["accept_long_term", "accept_short_term", "accept_night_shift",
                      "accept_standing_work", "accept_overtime", "accept_outside_province",
                      "couple_seeking_together", "has_health_certificate", "has_tattoo"]:
            col_type = self._col(Resume, name).type
            assert _is_tinyint(col_type), f"Resume.{name} should be TINYINT, got {type(col_type)}"

    def test_user_boolean_fields_tinyint(self):
        for name in ["can_search_jobs", "can_search_workers"]:
            col_type = self._col(User, name).type
            assert _is_tinyint(col_type), f"User.{name} should be TINYINT, got {type(col_type)}"

    def test_admin_user_boolean_fields_tinyint(self):
        for name in ["password_changed", "enabled"]:
            col_type = self._col(AdminUser, name).type
            assert _is_tinyint(col_type), f"AdminUser.{name} should be TINYINT, got {type(col_type)}"

    def test_dict_enabled_tinyint(self):
        for model in [DictCity, DictJobCategory, DictSensitiveWord]:
            col_type = self._col(model, "enabled").type
            assert _is_tinyint(col_type), f"{model.__tablename__}.enabled should be TINYINT"

    # ---- SMALLINT UNSIGNED ----

    def test_resume_height_smallint_unsigned(self):
        assert _is_smallint_unsigned(self._col(Resume, "height").type)

    def test_resume_weight_smallint_unsigned(self):
        assert _is_smallint_unsigned(self._col(Resume, "weight").type)

    # ---- MEDIUMTEXT ----

    def test_conversation_log_content_mediumtext(self):
        assert _is_mediumtext(self._col(ConversationLog, "content").type)
