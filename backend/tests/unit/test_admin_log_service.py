from unittest.mock import MagicMock

from app.services.admin_log_service import write_admin_log


def test_system_audit_target_is_preserved_for_visibility_readiness():
    db = MagicMock()

    entry = write_admin_log(
        db,
        target_type="system",
        target_id="visibility.recommendation_fields",
        action="manual_edit",
        operator="admin",
        after={"config_value": {"schema_version": 1, "revision": 1}},
    )

    assert entry.target_type == "system"
    assert entry.target_id == "visibility.recommendation_fields"
    assert entry.reason is None
    db.add.assert_called_once_with(entry)
