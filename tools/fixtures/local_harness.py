"""C03 local/harness-smoke adapter and actual pytest inventory transport.

OPE-123 owns command dispatch/result schema. This adapter supplies its registered
local consumer, never a real-provider or customer acceptance implementation.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

from tools.fixtures.evidence import validate_observations
from tools.fixtures.postgres import (
    DisposablePostgres,
    PostgresFixtureFailure,
    PrerequisiteMissing,
)

ROOT = Path(__file__).resolve().parents[2]
TARGET = "tests/integration/test_failure_harness.py"
MODES = ("customer_controlled", "opentour_managed")
_items: dict[str, dict[str, Any]] = {}


def envelope(tests: list[dict[str, Any]], *, cleanup: bool = True) -> dict[str, Any]:
    counts = {
        status: sum(t["status"] == status for t in tests)
        for status in ("passed", "failed", "skipped")
    }
    passed = bool(tests) and not counts["failed"] and not counts["skipped"] and cleanup
    reason = (
        None
        if passed
        else "CLEANUP_FAILED"
        if not cleanup
        else "NO_TESTS"
        if not tests
        else "ASSERTION_FAILED"
        if counts["failed"]
        else "REQUIRED_TEST_SKIPPED"
    )
    return {
        "schema_version": "opentour.test-result.v1",
        "runner": "pytest",
        "target": TARGET,
        "binding_nonce": os.environ["OPENTOUR_RESULT_NONCE"],
        "kind": "local_integration",
        "mode": None,
        "collected": len(tests),
        **counts,
        "tests": tests,
        "status": "PASS" if passed else "FAIL",
        "reason_code": reason,
        "cleanup_complete": cleanup,
    }


def write_report(path: Path, report: dict[str, Any]) -> None:
    # Atomic replace of a private sibling; caller and adapter both require absent REPORT.
    temporary = path.with_name(path.name + ".writing")
    with temporary.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, sort_keys=True)
    temporary.replace(path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Registered C03 local harness only")
    parser.add_argument("filter", choices=["harness-smoke"])
    parser.add_argument("--result-file", required=True)
    args = parser.parse_args(argv)
    destination = Path(args.result_file)
    nonce = os.environ.get("OPENTOUR_RESULT_NONCE", "")
    if (
        os.environ.get("OPENTOUR_TEST_TARGET") != TARGET
        or os.environ.get("OPENTOUR_RESULT_FILE") != str(destination)
        or os.environ.get("OPENTOUR_REPOSITORY_ROOT") != str(ROOT)
        or not re.fullmatch(r"[a-f0-9]{32}", nonce)
        or destination.exists()
        or destination.is_symlink()
        or not destination.parent.is_dir()
        or destination.parent.is_symlink()
    ):
        return 2
    try:
        DisposablePostgres().qualify_binaries()
    except PrerequisiteMissing:
        report = envelope([])
        report.update(status="BLOCKED", reason_code="MISSING_EXTERNAL_PREREQUISITE")
        write_report(destination, report)
        return 3
    except PostgresFixtureFailure:
        report = envelope([])
        report.update(status="FAIL", reason_code="RUNNER_ERROR")
        write_report(destination, report)
        return 1
    guard = ROOT / "packages/contracts/common/python-unit/sitecustomize.py"
    if not guard.is_file():
        # Corrected C02 foundation is a real integration prerequisite, never optional.
        report = envelope([])
        report.update(status="FAIL", reason_code="INVALID_CONFIGURATION")
        write_report(destination, report)
        return 2
    environment = {
        key: os.environ[key] for key in ("PATH", "TMPDIR", "SYSTEMROOT") if key in os.environ
    }
    environment.update(
        {
            "PYTHONPATH": os.pathsep.join((str(guard.parent), str(ROOT))),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "OPENTOUR_UNIT_GUARD": "1",
            "CI": "true",
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "OPENTOUR_RESULT_NONCE": nonce,
            "OPENTOUR_RESULT_FILE": str(destination),
            "OPENTOUR_TEST_TARGET": TARGET,
            "OPENTOUR_REPOSITORY_ROOT": str(ROOT),
        }
    )
    try:
        process = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "tools.fixtures.local_harness",
                "-q",
                "--",
                TARGET,
            ],
            cwd=ROOT,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        report = envelope([], cleanup=False)
        report.update(reason_code="RUNNER_TIMEOUT")
        write_report(destination, report)
        return 1
    if not destination.is_file():
        report = envelope([], cleanup=False)
        report.update(reason_code="RUNNER_ERROR")
        write_report(destination, report)
        return 1
    report = json.loads(destination.read_text())
    if process.returncode in {2, 3, 4, 5}:
        return 2
    return 0 if process.returncode == 0 and report["status"] == "PASS" else 1


def pytest_sessionstart(session: Any) -> None:
    _items.clear()
    session.config._opentour_cleanup_complete = False
    session.config._opentour_observations = []


def pytest_collection_finish(session: Any) -> None:
    for item in session.items:
        mode = getattr(item, "callspec", None)
        mode = mode.params.get("mode") if mode is not None else None
        file = Path(item.path).resolve().relative_to(ROOT).as_posix()
        _items[item.nodeid] = {"id": item.nodeid, "file": file, "status": "skipped", "mode": mode}


def pytest_runtest_logreport(report: Any) -> None:
    item = _items.get(report.nodeid)
    if item is None:
        return
    if report.failed or getattr(report, "wasxfail", None):
        item["status"] = "failed"
    elif report.skipped and item["status"] != "failed":
        item["status"] = "skipped"
    elif report.when == "call" and report.passed and item["status"] != "failed":
        item["status"] = "passed"


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    destination = Path(os.environ["OPENTOUR_RESULT_FILE"])
    result = envelope(list(_items.values()), cleanup=session.config._opentour_cleanup_complete)
    if exitstatus not in (0, 1, 5):
        result.update(status="FAIL", reason_code="INVALID_CONFIGURATION")
    observations = destination.with_name(destination.name + ".observations.json")
    observation_document = {
        "issue_key": "OPE-124",
        "fixture_version": "1",
        "observations": session.config._opentour_observations,
        "product_acceptance": "NOT_RUN",
    }
    validate_observations(observation_document)
    with observations.open("x", encoding="utf-8") as stream:
        json.dump(observation_document, stream, sort_keys=True)
    write_report(destination, result)


if __name__ == "__main__":
    raise SystemExit(main())
