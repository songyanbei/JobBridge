"""B1 hard gate: search, cards, prompts and logs never carry contact PII."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.llm import prompts
from app.llm import base as llm_base
from app.services import permission_service, search_service


_SENSITIVE_VALUES = ("13800138000", "wxid_secret", "张经理")
_SENSITIVE_KEYS = ("phone", "wechat", "contact_person")


def _job(**overrides):
    values = {
        "id": 1,
        "owner_userid": "owner-1",
        "city": "苏州市",
        "job_category": "普工",
        "salary_floor_monthly": 5000,
        "salary_ceiling_monthly": 6000,
        "pay_type": "月薪",
        "headcount": 1,
        "gender_required": "不限",
        "is_long_term": True,
        "district": "吴中区",
        "provide_meal": True,
        "provide_housing": True,
        "shift_pattern": "白班",
        "work_hours": "8小时",
        "employment_type": None,
        "accept_couple": None,
        "accept_student": None,
        "accept_minority": None,
        "description": "岗位描述 联系人张经理 电话13800138000 微信wxid_secret",
        "created_at": None,
        "hiring_company": "华星电子",
        "address": "木渎镇",
        "phone": _SENSITIVE_VALUES[0],
        "contact_person": _SENSITIVE_VALUES[2],
        "wechat": _SENSITIVE_VALUES[1],
        "phone_ciphertext": None,
        "contact_person_ciphertext": None,
        "wechat_ciphertext": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _resume(**overrides):
    values = {
        "id": 1,
        "owner_userid": "owner-1",
        "expected_cities": ["苏州市"],
        "expected_job_categories": ["普工"],
        "salary_expect_floor_monthly": 5000,
        "gender": "男",
        "age": 30,
        "education": "高中",
        "work_experience": "联系人张经理 电话13800138000 微信wxid_secret",
        "description": "可联系张经理，电话13800138000，微信wxid_secret",
        "created_at": None,
        "expected_districts": [],
        "available_from": None,
        "accept_night_shift": None,
        "accept_overtime": None,
        "accept_long_term": None,
        "accept_short_term": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_search_projection_is_pii_free_even_when_legacy_values_exist(monkeypatch):
    monkeypatch.setattr(
        search_service,
        "_build_users_map",
        lambda _ids, _db: {"owner-1": {"contact_available": False}},
    )
    card = search_service._jobs_to_dicts([_job()], MagicMock())[0]
    assert card["contact_available"] is False
    assert card["contact_placeholder"]
    assert not any(key in card for key in _SENSITIVE_KEYS)
    assert not any(value in str(card) for value in _SENSITIVE_VALUES)
    assert "[联系方式已隐藏]" in card["description"]
    rendered = search_service._format_job_results([card], 0)
    assert not any(value in rendered for value in _SENSITIVE_VALUES)
    assert "联系方式需通过联系请求获取" in rendered


def test_search_projection_uses_ciphertext_presence_only(monkeypatch):
    monkeypatch.setattr(
        search_service,
        "_build_users_map",
        lambda _ids, _db: {"owner-1": {"contact_available": False}},
    )
    card = search_service._jobs_to_dicts(
        [_job(phone_ciphertext=b"sealed")], MagicMock(),
    )[0]
    assert card["contact_available"] is True
    assert not any(key in card for key in _SENSITIVE_KEYS)


def test_resume_free_text_is_redacted_in_projection_and_card():
    projected = search_service._resumes_to_dicts([_resume()])[0]
    assert not any(value in str(projected) for value in _SENSITIVE_VALUES)
    assert "[联系方式已隐藏]" in projected["work_experience"]
    assert "[联系方式已隐藏]" in projected["description"]

    rendered = search_service._format_resume_results([{
        "id": 1,
        "expected_job_categories": ["普工"],
        "work_experience": "联系人张经理 电话13800138000 微信wxid_secret",
    }], 0)
    assert not any(value in rendered for value in _SENSITIVE_VALUES)
    assert "[联系方式已隐藏]" in rendered


def test_contact_verb_without_person_label_is_redacted():
    redacted = search_service._redact_contact_text("请联系张经理，电话13800138000")
    assert "张经理" not in redacted
    assert "13800138000" not in redacted


def test_permission_projection_maps_contact_policy_to_safe_fields():
    candidate = {
        "id": 1,
        "contact_available": True,
        "contact_placeholder": "联系方式需通过联系请求获取",
        "phone": _SENSITIVE_VALUES[0],
        "contact_person": _SENSITIVE_VALUES[2],
    }
    from app.services.visibility_policy import (
        default_policy_document,
        normalize_policy,
        snapshot_from_policy,
    )

    snapshot = snapshot_from_policy(
        normalize_policy(default_policy_document()), "job_search", "broker",
    )
    projected = permission_service.filter_job_for_role(candidate, "broker", snapshot)
    assert not any(key in projected for key in _SENSITIVE_KEYS)
    assert not any(value in str(projected) for value in _SENSITIVE_VALUES)


def test_prompt_and_dialogue_allowlist_have_no_plaintext_contact_examples():
    assert "phone" not in llm_base._DIALOGUE_V1_SLOTS
    assert {"phone", "contact_person"}.issubset(llm_base._DIALOGUE_V1_UPLOAD_SLOTS)
    prompt_text = prompts.INTENT_SYSTEM_PROMPT + prompts.DIALOGUE_PARSE_PROMPT_V2
    assert "13800138000" not in prompt_text
    assert "wxid_" not in prompt_text
    assert "<受控提取>" in prompt_text
    assert "contact_available" in prompt_text
