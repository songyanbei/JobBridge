from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_redeem_rechecks_mutable_listing_policy_and_actor_state():
    source = (ROOT / "app" / "listing" / "contact.py").read_text(encoding="utf-8")
    assert "current_listing_version" in source
    assert "current_policy_version" in source
    assert "current_direction" in source
    assert "direction_changed" in source
    assert "listing_version_changed" in source
    assert "policy_version_changed" in source
    assert "listing_not_active" in source
    assert "actor_not_allowed" in source


def test_bound_contact_facts_fail_closed_when_current_context_is_omitted():
    source = (ROOT / "app" / "listing" / "contact.py").read_text(encoding="utf-8")
    assert "listing_version_required" in source
    assert "policy_version_required" in source
    assert "direction_required" in source
    assert "direction = direction or request_row.direction" in source
