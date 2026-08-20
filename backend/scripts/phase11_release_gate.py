"""Run the bounded Phase 11 release acceptance gate.

This is intentionally an orchestrator, not another end-to-end test suite.  The
individual stage tests remain the smallest executable units and own their data.
The release gate adds one important CI invariant: an integration run with any
skipped test is a failure, because a missing MySQL/Redis variable must not look
green.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET


BACKEND = Path(__file__).resolve().parents[1]
RELEASE_TESTS = (
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


def _require_isolated_services() -> None:
    missing = [
        name for name in (
            "RUN_INTEGRATION", "PHASE11_TEST_MYSQL_DSN", "PHASE11_TEST_REDIS_DSN",
        )
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit("missing Phase 11 integration settings: " + ", ".join(missing))
    if os.environ["RUN_INTEGRATION"] != "1":
        raise SystemExit("RUN_INTEGRATION must be exactly 1")
    if not os.environ.get("REDIS_HOST") or not os.environ.get("REDIS_PORT"):
        raise SystemExit("REDIS_HOST and REDIS_PORT are required")


def _run_release() -> int:
    _require_isolated_services()
    with tempfile.TemporaryDirectory(prefix="phase11-release-") as temp_dir:
        report = Path(temp_dir) / "pytest.xml"
        command = [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q",
            f"--junitxml={report}", *RELEASE_TESTS,
        ]
        completed = subprocess.run(command, cwd=BACKEND, check=False)
        if completed.returncode:
            return completed.returncode
        root = ET.parse(report).getroot()
        skipped = sum(int(node.attrib.get("skipped", "0")) for node in root.iter("testsuite"))
        tests = sum(int(node.attrib.get("tests", "0")) for node in root.iter("testsuite"))
        if tests == 0 or skipped:
            print(f"Phase 11 gate rejected tests={tests}, skipped={skipped}", file=sys.stderr)
            return 2
        print(f"Phase 11 release units passed: tests={tests}, skipped=0")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("release",))
    args = parser.parse_args()
    return _run_release() if args.command == "release" else 2


if __name__ == "__main__":
    raise SystemExit(main())
