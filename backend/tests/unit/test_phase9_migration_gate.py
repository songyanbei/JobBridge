"""Phase 9 manifest/preflight gates for incremental recommendation releases."""
from __future__ import annotations

from scripts import apply_phase9_migrations as migrations
from scripts import recommendation_preflight as preflight


class _Rows(list):
    pass


class _LedgerConnection:
    def __init__(self, rows):
        self.rows = rows

    def execute(self, statement):
        sql = str(statement)
        if "FROM schema_migration_history" in sql:
            return _Rows(self.rows)
        raise AssertionError(f"unexpected query: {sql}")


def test_manifest_contains_ttl_backfill_and_all_checksums_match():
    root = preflight.PHASE9_MANIFEST.parent
    entries = migrations.check_files(root, preflight.PHASE9_MANIFEST)

    assert len(entries) == 9
    assert entries[-1][0] == (
        "phase9_009_recommendation_delivery_ttl_backfill.sql"
    )


def test_post_migration_preflight_rejects_partial_phase9_ledger():
    expected, mismatches = preflight._phase9_manifest_entries()
    assert mismatches == []
    partial_names = list(expected)[:4]
    conn = _LedgerConnection([
        (name, expected[name], 1) for name in partial_names
    ])
    findings = []
    tables = set(preflight.PHASE9_TABLES) | {"schema_migration_history"}

    preflight.check_phase9_applied(
        conn,
        tables,
        "post-migration",
        findings,
    )

    finding = next(
        item for item in findings
        if item.code == "migration_ledger_incomplete"
    )
    assert "phase9_005_recommendation_delivery_ttl.sql" in finding.detail["missing"]
    assert "phase9_009_recommendation_delivery_ttl_backfill.sql" in finding.detail["missing"]


def test_ttl_backfill_preserves_existing_deadlines_and_covers_all_states():
    sql = (
        preflight.PHASE9_MANIFEST.parent
        / "phase9_009_recommendation_delivery_ttl_backfill.sql"
    ).read_text(encoding="utf-8")

    assert "WHERE `content_expires_at` IS NULL" in sql
    for status in (
        "prepared",
        "pending",
        "sending",
        "retry_wait",
        "sent",
        "permanent_failed",
        "unknown",
    ):
        assert f"'{status}'" in sql
