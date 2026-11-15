"""Finite PG transport adversaries. Controlled reports do not qualify PostgreSQL."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tools import test_runner as runner
from tools.fixtures import python_postgres as adapter
from tools.fixtures.postgres import DisposablePostgres, PrerequisiteMissing

TARGET = "services/control-api/tests/migrations/test_expand_contract.py"


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
    target = root / TARGET
    target.parent.mkdir(parents=True)
    target.write_text("def test_generated():\n    assert True\n")
    return root


def controlled_result(
    invocation: runner.Invocation, change: str | None = None
) -> runner.ProcessResult:
    report = runner.report_envelope(
        "pytest",
        invocation.target,
        invocation.environment,
        [{"id": "generated", "file": invocation.target, "status": "passed", "mode": None}],
    )
    report["kind"] = "local_integration"
    observations: dict[str, Any] = {
        "schema_version": "opentour.python-postgres-observation.v1",
        "target": invocation.target,
        "owner": adapter.TARGETS[invocation.target],
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
    if change == "nonce":
        observations["binding_nonce"] = "f" * 32
    elif change == "target":
        observations["target"] = "services/control-api/tests/auth/test_identity_postgres.py"
    elif change == "owner":
        observations["owner"] = "OPE-142"
    elif change == "source":
        observations["source_sha256"] = "f" * 64
    elif change == "report":
        observations["report_sha256"] = "f" * 64
    elif change == "shape":
        observations["untrusted"] = True
    elif change == "zero":
        observations["started_instances"] = 0
    elif change == "binaries":
        observations["binary_sha256"] = None
    elif change == "cleanup":
        observations["cleanup_complete"] = False
    elif change == "dirty":
        (invocation.cwd / invocation.target).write_text("# changed\n")
    invocation.report.write_text(json.dumps(report))
    invocation.report.with_name(invocation.report.name + ".postgres.json").write_text(
        json.dumps(observations)
    )
    return runner.ProcessResult(0)


def test_exact_target_uses_fixed_local_profile(checkout: Path) -> None:
    calls: list[runner.Invocation] = []

    def run(invocation: runner.Invocation) -> runner.ProcessResult:
        calls.append(invocation)
        return controlled_result(invocation)

    result = runner.run_command("test-python", [TARGET], checkout, run)
    assert result["exit_code"] == 0 and result["kind"] == "local_integration"
    assert calls[0].consumer == "postgres_python"
    assert calls[0].environment["OPENTOUR_UNIT_GUARD"] == "1"
    assert "tools.fixtures.python_postgres" in calls[0].argv
    assert result["steps"][0]["artifacts"][0]["content"]["product_acceptance"] == "NOT_RUN"


@pytest.mark.parametrize(
    "change", ["nonce", "target", "owner", "source", "report", "shape", "dirty"]
)
def test_stale_or_tampered_lifecycle_sidecar_is_refused(checkout: Path, change: str) -> None:
    result = runner.run_command(
        "test-python", [TARGET], checkout, lambda invocation: controlled_result(invocation, change)
    )
    assert result["exit_code"] == 2


@pytest.mark.parametrize("change", ["zero", "binaries", "cleanup"])
def test_false_success_cannot_upgrade_missing_resource_or_cleanup(
    checkout: Path, change: str
) -> None:
    result = runner.run_command(
        "test-python", [TARGET], checkout, lambda invocation: controlled_result(invocation, change)
    )
    assert result["exit_code"] == 1


def test_timeout_is_failure_without_lifecycle_claim(checkout: Path) -> None:
    result = runner.run_command(
        "test-python",
        [TARGET],
        checkout,
        lambda _: runner.ProcessResult(-9, timed_out=True, cleanup_complete=False),
    )
    assert result["exit_code"] == 1
    assert result["steps"][0]["tests"] is None
    assert result["steps"][0]["process_cleanup_complete"] is False


def test_generic_test_path_does_not_self_upgrade(checkout: Path) -> None:
    path = "services/control-api/tests/test_arbitrary.py"
    (checkout / path).write_text("import pytest\npytestmark=pytest.mark.local_integration\n")
    calls: list[runner.Invocation] = []

    def run(invocation: runner.Invocation) -> runner.ProcessResult:
        calls.append(invocation)
        report = runner.report_envelope(
            "pytest",
            path,
            invocation.environment,
            [{"id": "one", "file": path, "status": "passed", "mode": None}],
        )
        invocation.report.write_text(json.dumps(report))
        return runner.ProcessResult(0)

    assert runner.run_command("test-python", [path], checkout, run)["kind"] == "simulated"
    assert calls[0].consumer is None and calls[0].expected_kind == "simulated"


def test_adapter_absent_postgres_returns_empty_blocked_report(
    checkout: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "result.json"
    monkeypatch.setattr(adapter, "ROOT", checkout)
    monkeypatch.setattr(runner, "install_unit_guard", lambda: None)
    for key, value in {
        "OPENTOUR_TEST_TARGET": TARGET,
        "OPENTOUR_RESULT_FILE": str(output),
        "OPENTOUR_REPOSITORY_ROOT": str(checkout),
        "OPENTOUR_RESULT_NONCE": "a" * 32,
    }.items():
        monkeypatch.setenv(key, value)

    def missing(_: Any) -> None:
        raise PrerequisiteMissing("generated missing prerequisite")

    monkeypatch.setattr(DisposablePostgres, "qualify_binaries", missing)
    assert adapter.main([TARGET, "--result-file", str(output)]) == 3
    report = json.loads(output.read_bytes())
    assert report["status"] == "BLOCKED" and report["tests"] == []
    assert not output.with_name(output.name + ".postgres.json").exists()


def test_directory_with_registered_postgres_is_not_mislabeled_as_unit(checkout: Path) -> None:
    def forbidden(_: runner.Invocation) -> runner.ProcessResult:
        pytest.fail("Directory must be refused before execution")

    result = runner.run_command("test-python", ["services/control-api/tests"], checkout, forbidden)
    assert result["exit_code"] == 2
    assert adapter.present_targets(checkout) == (TARGET,)


def test_actual_helper_is_included_in_public_typecheck(checkout: Path) -> None:
    path = checkout / "infrastructure/environment_contract.py"
    path.parent.mkdir()
    path.write_text("def required_check() -> int:\n    return 1\n")
    calls: list[runner.Invocation] = []

    def run(invocation: runner.Invocation) -> runner.ProcessResult:
        calls.append(invocation)
        return runner.ProcessResult(0)

    result = runner.run_command("typecheck", [], checkout, run)
    assert result["exit_code"] == 0
    assert "infrastructure/environment_contract.py" in calls[0].argv
