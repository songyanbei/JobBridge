from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import phase11_release_gate as gate


ROOT = Path(__file__).resolve().parents[3]


def test_release_gate_is_the_bounded_stage_chain():
    assert gate.RELEASE_TESTS == (
        "tests/unit/test_phase11_stage2_visibility.py",
        "tests/unit/test_resume_replacement_stage3_units.py",
        "tests/unit/test_resume_replacement_stage4_units.py",
        "tests/unit/test_resume_phase11_cleanup_units.py",
        "tests/integration/test_phase11_stage1_migration_mysql.py",
        "tests/integration/test_phase11_stage2_activation_mysql.py",
        "tests/integration/test_resume_replacement_stage3_mysql.py",
        "tests/integration/test_resume_replacement_stage4_mysql.py",
        "tests/integration/test_resume_phase11_stage5_fences.py",
        "tests/integration/test_resume_admin_stage6_mysql.py",
    )
    assert all((ROOT / "backend" / path).is_file() for path in gate.RELEASE_TESTS)


def test_release_gate_fails_closed_before_pytest_when_services_are_implicit(monkeypatch):
    for name in (
        "RUN_INTEGRATION", "PHASE11_TEST_MYSQL_DSN", "PHASE11_TEST_REDIS_DSN",
        "REDIS_HOST", "REDIS_PORT",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(SystemExit, match="missing Phase 11 integration settings"):
        gate._require_isolated_services()


@pytest.mark.parametrize(("tests", "skipped"), ((0, 0), (1, 1)))
def test_release_gate_rejects_empty_or_skipped_junit(monkeypatch, tests, skipped):
    def fake_run(command, **_kwargs):
        report = Path(next(value.split("=", 1)[1] for value in command if value.startswith("--junitxml=")))
        report.write_text(
            f'<testsuites><testsuite tests="{tests}" skipped="{skipped}" failures="0"/></testsuites>',
            encoding="utf-8",
        )
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(gate, "_require_isolated_services", lambda: None)
    monkeypatch.setattr(gate.subprocess, "run", fake_run)
    assert gate._run_release() == 2


def test_release_gate_propagates_pytest_failure_without_accepting_junit(monkeypatch):
    monkeypatch.setattr(gate, "_require_isolated_services", lambda: None)
    monkeypatch.setattr(
        gate.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=1),
    )
    assert gate._run_release() == 1


def test_ci_has_explicit_phase11_and_frontend_quality_gates():
    workflow = (ROOT / ".github/workflows/backend-ci.yml").read_text(encoding="utf-8")
    phase11 = workflow.split("  backend-phase11-resume-mysql-redis:", 1)[1]
    assert "RUN_INTEGRATION: \"1\"" in phase11
    assert "PHASE11_TEST_MYSQL_DSN:" in phase11
    assert "PHASE11_TEST_REDIS_DSN:" in phase11
    assert "python scripts/phase11_release_gate.py release" in phase11
    frontend = workflow.split("  frontend-quality:", 1)[1]
    for command in ("npm ci", "npm run lint", "npm test", "npm run build"):
        assert command in frontend


def test_release_documents_link_only_existing_acceptance_files():
    matrix = (ROOT / "docs/简历Phase11需求测试追踪矩阵.md").read_text(encoding="utf-8")
    runbook = (ROOT / "docs/简历Phase11发布回退与清理Runbook.md").read_text(encoding="utf-8")
    for required in (
        "test_phase11_stage1_migration_mysql.py",
        "test_resume_replacement_stage4_mysql.py",
        "test_resume_phase11_stage5_fences.py",
        "test_resume_admin_stage6_mysql.py",
        "idx_resume_hard_delete",
    ):
        assert required in matrix
    for required in (
        "RESUME_LIFECYCLE_V2_ENABLED",
        "RESUME_REPLACEMENT_ENABLED",
        "RESUME_CANDIDATE_CLEANUP_ENABLED",
        "RESUME_EXPIRY_CLEANUP_ENABLED",
        "RESUME_HARD_DELETE_ENABLED",
        "每批最多 50 条",
        "两个不同管理员",
        "不授权连接预发布或生产",
        "manifest-check --manifest",
        "check --manifest",
        "apply --manifest",
        "resume --manifest",
        "verify --manifest",
        "--stage post_cutover",
        "--build-probe-url",
        "--cutover-resume-id",
        "--confirm-down",
    ):
        assert required in runbook
