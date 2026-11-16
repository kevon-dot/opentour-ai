"""Controlled native organization route tests; no PostgreSQL or WorkOS runs here."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tools import test_runner as runner
from tools.fixtures import python_postgres as adapter

TARGET = "services/control-api/tests/organizations/test_creation_saga.py"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copytree(runner.ROOT / "packages/contracts", root / "packages/contracts")
    for relative in (
        "pyproject.toml",
        "services/control-api/pyproject.toml",
        "tools/fixtures/postgres.py",
        "tools/fixtures/python_postgres.py",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(runner.ROOT / relative, destination)
    path = root / TARGET
    path.parent.mkdir(parents=True)
    path.write_text("# Controlled transport fixture, not actual database evidence.\n")
    return root


def controlled_result(invocation: runner.Invocation, change: str = "") -> runner.ProcessResult:
    tests = [
        {"id": "generated-" + mode, "file": invocation.target, "status": "passed", "mode": mode}
        for mode in runner.MODES
    ]
    if change in {"zero", "missing_pg"}:
        tests = []
    elif change == "one_mode":
        tests = tests[:1]
    elif change in {"failed", "skipped"}:
        tests[0]["status"] = change
    report = runner.report_envelope("pytest", invocation.target, invocation.environment, tests)
    report["kind"] = "provider_integration" if change == "kind" else "local_integration"
    raw = 1 if change == "failed" else 0
    if change == "missing_pg":
        report.update(status="BLOCKED", reason_code="MISSING_EXTERNAL_PREREQUISITE")
        raw = 3
    observations: dict[str, Any] = {
        "schema_version": "opentour.python-postgres-observation.v1",
        "target": invocation.target,
        "owner": "OPE-171",
        "binding_nonce": invocation.environment["OPENTOUR_RESULT_NONCE"],
        "source_sha256": adapter.source_digest(invocation.cwd, invocation.target),
        "report_sha256": hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
        "kind": "local_integration",
        "product_acceptance": "NOT_RUN",
        "postgres_version": "18.4",
        "started_instances": 1,
        "cleanup_complete": True,
        "binary_sha256": {name: "a" * 64 for name in ("postgres", "initdb", "psql")},
    }
    if change in {"nonce", "source", "report"}:
        key = {"nonce": "binding_nonce", "source": "source_sha256", "report": "report_sha256"}[
            change
        ]
        observations[key] = "f" * (32 if change == "nonce" else 64)
    elif change == "target":
        observations["target"] = "services/control-api/tests/auth/test_identity_postgres.py"
    elif change == "owner":
        observations["owner"] = "OPE-142"
    elif change == "cleanup":
        observations["cleanup_complete"] = False
    elif change == "no_instances":
        observations["started_instances"] = 0
    elif change == "no_binaries":
        observations["binary_sha256"] = None
    elif change == "dirty":
        (invocation.cwd / invocation.target).write_text("# Source changed after invocation.\n")
    invocation.report.write_text(json.dumps(report))
    if change != "missing_pg":
        invocation.report.with_name(invocation.report.name + ".postgres.json").write_text(
            json.dumps(observations)
        )
    return runner.ProcessResult(raw)


def test_exact_native_route_binds_owner_modes_and_mixed_fixture_boundary(checkout: Path) -> None:
    calls: list[runner.Invocation] = []

    def run(invocation: runner.Invocation) -> runner.ProcessResult:
        calls.append(invocation)
        return controlled_result(invocation)

    receipt = runner.run_command("test-python", [TARGET], checkout, run)
    assert receipt["exit_code"] == 0 and receipt["kind"] == "local_integration"
    assert len(calls) == 1 and calls[0].consumer == "postgres_python"
    assert calls[0].environment["OPENTOUR_UNIT_GUARD"] == "1"
    assert calls[0].environment["OPENTOUR_TEST_TARGET"] == TARGET
    assert "tools.fixtures.python_postgres" in calls[0].argv
    step = receipt["steps"][0]
    assert step["artifacts"][0]["content"]["owner"] == "OPE-171"
    assert {test["mode"] for test in step["tests"]["tests"]} == set(runner.MODES)
    assert step["fixture_scope"] == {
        "postgresql": "actual_local",
        "workos_responses": "simulated_only",
        "provider_acceptance": "NOT_RUN",
    }
    assert adapter.present_targets(checkout) == (TARGET,)
    assert adapter.target_command(TARGET) == ("just", "test-python", TARGET)


@pytest.mark.parametrize("change", ["nonce", "source", "report", "target", "owner", "dirty"])
def test_stale_or_cross_owner_observations_refuse(checkout: Path, change: str) -> None:
    receipt = runner.run_command(
        "test-python", [TARGET], checkout, lambda invocation: controlled_result(invocation, change)
    )
    assert receipt["exit_code"] == 2


@pytest.mark.parametrize(
    "change",
    ["zero", "one_mode", "failed", "skipped", "kind", "no_instances", "no_binaries", "cleanup"],
)
def test_no_false_success_or_provider_upgrade(checkout: Path, change: str) -> None:
    receipt = runner.run_command(
        "test-python", [TARGET], checkout, lambda invocation: controlled_result(invocation, change)
    )
    assert receipt["exit_code"] != 0 and receipt["status"] != "PASS"
    assert receipt["kind"] != "provider_integration"


def test_missing_postgres_is_blocked_not_empty_pass(checkout: Path) -> None:
    receipt = runner.run_command(
        "test-python",
        [TARGET],
        checkout,
        lambda invocation: controlled_result(invocation, "missing_pg"),
    )
    assert receipt["exit_code"] == 3 and receipt["status"] == "BLOCKED"
    assert receipt["steps"][0]["tests"]["tests"] == []


def test_parent_directory_cannot_collect_local_target_as_unit(checkout: Path) -> None:
    def forbidden(_: runner.Invocation) -> runner.ProcessResult:
        pytest.fail("Mixed fixture directory must be refused before execution")

    receipt = runner.run_command(
        "test-python", ["services/control-api/tests/organizations"], checkout, forbidden
    )
    assert receipt["exit_code"] == 2


def test_neighbor_path_does_not_gain_registered_local_authority(checkout: Path) -> None:
    target = "services/control-api/tests/organizations/test_other_saga.py"
    (checkout / target).write_text("# Unregistered generated neighbor\n")
    calls: list[runner.Invocation] = []

    def run(invocation: runner.Invocation) -> runner.ProcessResult:
        calls.append(invocation)
        return controlled_result(invocation)

    receipt = runner.run_command("test-python", [target], checkout, run)
    assert receipt["exit_code"] == 2
    assert calls[0].consumer is None and calls[0].expected_kind == "simulated"


def test_timeout_retains_failed_cleanup_without_resource_success(checkout: Path) -> None:
    receipt = runner.run_command(
        "test-python",
        [TARGET],
        checkout,
        lambda _: runner.ProcessResult(-9, timed_out=True, cleanup_complete=False),
    )
    assert receipt["exit_code"] == 1 and receipt["status"] == "FAIL"
    assert receipt["steps"][0]["process_cleanup_complete"] is False
    assert receipt["steps"][0]["tests"] is None
