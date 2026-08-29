from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.services.job_business_digest_service import (
    DIGEST_FIELDS_V2,
    DIGEST_FIELDS_V1,
    DIGEST_VERSION,
    business_digest,
    canonical_business_bytes,
)


def _job(**overrides):
    values = {field: None for field in DIGEST_FIELDS_V2}
    values.update({
        "owner_userid": "owner",
        "city": "上海",
        "job_category": "普工",
        "salary_floor_monthly": 6000,
        "pay_type": "月薪",
        "headcount": 2,
        "gender_required": "不限",
        "is_long_term": True,
        "raw_text": "第一行\n第二行",
        "images": ["images/owner/a.jpg"],
        "extra": {"b": 2, "a": 1},
    })
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize("field", DIGEST_FIELDS_V2)
def test_each_business_field_changes_digest(field):
    original = _job()
    changed = deepcopy(original)
    current = getattr(changed, field)
    if field == "images":
        value = ["images/owner/b.jpg"]
    elif field == "extra":
        value = {"a": 1, "b": 3}
    elif isinstance(current, bool):
        value = not current
    elif isinstance(current, int):
        value = current + 1
    else:
        value = "changed" if current is None else f"{current}-changed"
    setattr(changed, field, value)
    assert business_digest(changed) != business_digest(original)


def test_lifecycle_fields_do_not_change_digest():
    original = _job()
    changed = deepcopy(original)
    changed.expires_at = "later"
    changed.deleted_at = "now"
    changed.version = 99
    assert business_digest(changed) == business_digest(original)


def test_canonicalization_normalizes_text_and_extra_order():
    left = _job(raw_text="Cafe\u0301\r\nline", extra={"z": [2, 1], "a": False})
    right = _job(raw_text="Café\nline", extra={"a": False, "z": [2, 1]})
    assert canonical_business_bytes(left) == canonical_business_bytes(right)


def test_images_are_normalized_to_object_keys():
    assert business_digest(_job(images=["/files/images/owner/a.jpg"])) == business_digest(
        _job(images=["images/owner/a.jpg"])
    )


def test_float_and_unsupported_digest_version_fail_closed():
    with pytest.raises(ValueError):
        business_digest(_job(extra={"score": 1.5}))
    with pytest.raises(ValueError):
        business_digest(_job(), digest_version=3)


def test_digest_v1_golden_vector():
    assert business_digest(
        _job(), digest_version=1
    ) == "1507f659e97c1c68ef2013596c560f883075da8617118d41d482116e2a908d74"


def test_digest_v2_tracks_visibility_fields_without_reinterpreting_v1():
    original = _job(
        hiring_company="华星电子",
        contact_person="张经理",
        phone="13800138000",
    )
    changed = deepcopy(original)
    changed.phone = "13900139000"

    assert DIGEST_VERSION == 2
    assert business_digest(changed) != business_digest(original)
    assert business_digest(changed, digest_version=1) == business_digest(
        original, digest_version=1
    )


def test_digest_v2_golden_vector():
    assert business_digest(_job()) == (
        "9234daf9c1cba56480847176f4616d9ac93aa9a7d47ca591afa08683ee90eeef"
    )
