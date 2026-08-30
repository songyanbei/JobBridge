from app.config import Settings


def test_contact_defaults_are_fail_closed_and_bounded():
    settings = Settings(_env_file=None)
    assert settings.contact_service_mode == "off"
    assert settings.contact_grant_ttl_seconds == 60
    assert settings.contact_rate_per_listing_limit == 3
    assert settings.contact_daily_limit == 30
    assert settings.pii_active_key_version == 1


def test_contact_limits_reject_non_positive_values():
    for field in (
        "contact_grant_ttl_seconds", "contact_rate_per_listing_limit",
        "contact_daily_limit", "contact_delivery_ttl_seconds",
        "pii_migration_batch_size",
    ):
        try:
            Settings(_env_file=None, **{field: 0})
        except ValueError:
            continue
        raise AssertionError(f"{field} must reject zero")
