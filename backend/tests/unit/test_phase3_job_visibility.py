"""P3 acceptance tests for job fields and source-aware candidate data."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.llm.base import IntentResult
from app.services import account_service, audit_workbench_service, search_service, upload_service
from app.services.permission_service import filter_job_for_role
from app.services.user_service import UserContext
from app.services.visibility_policy import normalize_policy, snapshot_from_policy, default_policy_document
from app.schemas.conversation import SessionState


def _job(**overrides):
    values = {
        "id": 1, "owner_userid": "publisher-1", "city": "苏州市",
        "job_category": "普工", "salary_floor_monthly": 6000,
        "salary_ceiling_monthly": 7000, "pay_type": "月薪", "headcount": 2,
        "gender_required": "不限", "is_long_term": True, "district": "吴中区",
        "provide_meal": True, "provide_housing": True, "shift_pattern": "白班",
        "work_hours": "8小时", "description": "岗位描述", "created_at": None,
        "employment_type": None, "accept_couple": None, "accept_student": None,
        "accept_minority": None, "hiring_company": None, "address": None,
        "contact_person": None, "phone": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _user_map(data):
    return {"publisher-1": data}


def test_job_level_values_win_and_source_metadata_is_preserved(monkeypatch) -> None:
    monkeypatch.setattr(
        search_service, "_build_users_map",
        lambda _ids, _db: _user_map({
            "company": "中介甲", "address": "中介经营地址",
            "contact_person": "账号联系人", "phone": "账号电话",
        }),
    )
    candidate = search_service._jobs_to_dicts([
        _job(
            hiring_company="真实工厂",
            address="工厂工作地址",
            contact_person="岗位联系人",
            phone="岗位电话",
        ),
    ], MagicMock())[0]
    assert candidate["hiring_company"] == "真实工厂"
    assert candidate["hiring_company_source"] == "job.hiring_company"
    assert candidate["address"] == "工厂工作地址"
    assert candidate["address_source"] == "job.address"
    # Search projections must never carry plaintext contact PII; Contact is
    # obtained through the opaque grant flow instead.
    assert "contact_person" not in candidate
    assert "phone" not in candidate
    assert candidate["contact_available"] is False


def test_old_job_uses_explicit_publisher_fallbacks_and_blank_normalization(monkeypatch) -> None:
    monkeypatch.setattr(
        search_service, "_build_users_map",
        lambda _ids, _db: _user_map({
            "company": "  历史发布主体  ", "address": "  发布方经营地址 ",
            "contact_person": "  账号联系人 ", "phone": "  账号电话 ",
        }),
    )
    candidate = search_service._jobs_to_dicts([_job()], MagicMock())[0]
    assert candidate["hiring_company"] == "历史发布主体"
    assert candidate["hiring_company_source"] == "publisher_company_fallback"
    assert candidate["address"] == "发布方经营地址"
    assert candidate["address_source"] == "publisher_address_fallback"
    assert "contact_person" not in candidate
    assert "phone" not in candidate
    assert candidate["contact_available"] is False

    monkeypatch.setattr(search_service, "_build_users_map", lambda _ids, _db: _user_map({
        "company": " ", "address": "", "contact_person": None, "phone": "  ",
    }))
    empty = search_service._jobs_to_dicts([_job(hiring_company=" ")], MagicMock())[0]
    assert empty["hiring_company"] is None
    assert empty["hiring_company_source"] == "none"
    assert empty["address_source"] == "none"
    assert "contact_person" not in empty
    assert "phone" not in empty
    assert empty["contact_available"] is False


def test_source_aware_job_rendering_never_mislabels_fallbacks() -> None:
    text = search_service._format_job_results([
        {
            "id": 1, "hiring_company": "历史中介", "hiring_company_source": "publisher_company_fallback",
            "job_category": "普工", "salary_floor_monthly": 6000, "pay_type": "月薪",
            "city": "苏州市", "address": "经营地址", "address_source": "publisher_address_fallback",
            "contact_person": None, "phone": None, "contact_placeholder": "联系方式需通过联系请求获取",
        },
    ], 0)
    assert "发布主体：历史中介（历史回退）" in text
    assert "招聘工厂：历史中介" not in text
    assert "发布方经营地址：经营地址（岗位地址缺失）" in text
    assert "工作地址：经营地址" not in text
    assert "联系方式需通过联系请求获取" in text


def test_worker_legacy_filter_does_not_expose_new_sensitive_job_fields() -> None:
    candidate = {
        "id": 1, "hiring_company": "工厂", "job_category": "普工",
        "salary_floor_monthly": 6000, "address": "门牌", "address_source": "job.address",
        "phone": "电话", "contact_person": "联系人",
    }
    policy = normalize_policy(default_policy_document())
    snapshot = snapshot_from_policy(policy, "job_search", "worker")
    filtered = filter_job_for_role(candidate, "worker", snapshot)
    assert "address" not in filtered
    assert "phone" not in filtered
    assert "contact_person" not in filtered


def test_upload_writes_job_level_fields() -> None:
    user = UserContext(
        external_userid="publisher-1", role="broker", status="active", display_name=None,
        company="中介", contact_person="账号联系人", phone="账号电话",
        can_search_jobs=True, can_search_workers=True, is_first_touch=False, should_welcome=False,
    )
    audit = SimpleNamespace(status="passed", reason=None)
    db = MagicMock()
    entity = upload_service._create_job({
        "city": "苏州市", "job_category": "普工", "salary_floor_monthly": 6000,
        "pay_type": "月薪", "headcount": 1, "hiring_company": "真实工厂",
        "address": "岗位地址", "contact_person": "岗位联系人", "phone": "岗位电话",
    }, user, audit, 30, "原文", [], db)
    assert entity.hiring_company == "真实工厂"
    assert entity.address == "岗位地址"
    assert entity.contact_person == "岗位联系人"
    assert entity.phone == "岗位电话"


def test_account_address_is_persisted_and_audited() -> None:
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    user = account_service.pre_register(
        db, "broker", {
            "display_name": "中介", "company": "中介公司", "address": "经营地址",
            "contact_person": "联系人", "phone": "电话",
        }, "admin",
    )
    assert user.address == "经营地址"
    calls = [call for call in db.add.call_args_list if call.args]
    audit_rows = [call.args[0] for call in calls if hasattr(call.args[0], "action")]
    assert audit_rows
    assert "经营地址" in str(audit_rows[-1].snapshot)


def test_audit_workbench_job_edit_whitelist_contains_new_fields() -> None:
    assert {"hiring_company", "contact_person", "phone", "address"}.issubset(
        audit_workbench_service._JOB_EDIT_FIELDS,
    )


def test_schema_and_migration_declare_new_columns() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    schema = (root / "sql" / "schema.sql").read_text(encoding="utf-8")
    migration = (root / "sql" / "migrations" / "phase10_001_job_visibility_fields.sql").read_text(encoding="utf-8")
    for field in ("hiring_company", "contact_person", "phone"):
        assert f"`{field}`" in schema
        assert f"COLUMN_NAME = '{field}'" in migration
    assert "IF(@col_exists = 0" in migration
