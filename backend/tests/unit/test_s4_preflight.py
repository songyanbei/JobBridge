from scripts.s4_preflight import Finding, check_migration_artifacts, check_runtime_config


def test_runtime_preflight_fails_closed_by_default(monkeypatch):
    for name in ("ACTION_EXECUTION_MODE", "CONTACT_SERVICE_MODE", "JOB_PUBLISH_FLOW_ENABLED", "JOB_PUBLISH_ROLLOUT_PERCENTAGE"):
        monkeypatch.delenv(name, raising=False)
    findings: list[Finding] = []
    runtime = check_runtime_config(findings)
    assert runtime["publish_rollout_percentage"] == 0
    assert any(item.code == "action_gate_incomplete" for item in findings)
    assert any(item.code == "contact_gate_incomplete" for item in findings)


def test_migration_preflight_checks_up_and_down_files(tmp_path):
    migration_dir = tmp_path / "sql" / "migrations"
    migration_dir.mkdir(parents=True)
    (migration_dir / "phase14_001_domain_outbox_event.sql").write_text("-- up")
    findings: list[Finding] = []
    report = check_migration_artifacts(tmp_path, findings)
    assert report["missing"] == ["phase14_down_001_domain_outbox_event.sql"]
    assert findings and findings[0].level == "error"
