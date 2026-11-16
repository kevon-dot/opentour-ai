"""Controlled journal transport tests; these do not qualify PostgreSQL or AWS."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tools import test_runner as runner
from tools.fixtures import python_postgres as adapter

TARGET = "tests/integration/authority_journal_recovery.py"


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    shutil.copytree(runner.ROOT / "packages/contracts", root / "packages/contracts")
    for relative in (
        "pyproject.toml",
        "tools/fixtures/postgres.py",
        "tools/fixtures/python_postgres.py",
    ):
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(runner.ROOT / relative, destination)
    target = root / TARGET
    target.parent.mkdir(parents=True)
    target.write_text("# Controlled transport fixture; no resource runs here.\n")
    return root


def result(invocation: runner.Invocation, change: str = "") -> runner.ProcessResult:
    tests = [
        {"id": "generated-" + mode, "file": TARGET, "status": "passed", "mode": mode}
        for mode in runner.MODES
    ]
    if change in {"zero", "missing_pg"}:
        tests = []
    elif change == "one_mode":
        tests = tests[:1]
    elif change in {"failed", "skipped"}:
        tests[0]["status"] = change
    report = runner.report_envelope("pytest", TARGET, invocation.environment, tests)
    report["kind"] = "local_integration"
    raw = 1 if change == "failed" else 0
    if change == "missing_pg":
        report.update(status="BLOCKED", reason_code="MISSING_EXTERNAL_PREREQUISITE")
        raw = 3
    if change == "kind":
        report["kind"] = "provider_integration"
    observation: dict[str, Any] = {
        "schema_version": "opentour.python-postgres-observation.v1",
        "target": TARGET,
        "owner": "OPE-217",
        "binding_nonce": invocation.environment["OPENTOUR_RESULT_NONCE"],
        "source_sha256": adapter.source_digest(invocation.cwd, TARGET),
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
        observation[key] = "f" * (32 if change == "nonce" else 64)
    elif change == "target":
        observation["target"] = "services/control-api/tests/auth/test_identity_postgres.py"
    elif change == "owner":
        observation["owner"] = "OPE-142"
    elif change == "cleanup":
        observation["cleanup_complete"] = False
    elif change == "no_instances":
        observation["started_instances"] = 0
    elif change == "dirty":
        (invocation.cwd / TARGET).write_text("# changed after execution\n")
    invocation.report.write_text(json.dumps(report))
    if change != "missing_pg":
        invocation.report.with_name(invocation.report.name + ".postgres.json").write_text(
            json.dumps(observation)
        )
    return runner.ProcessResult(raw)


def test_exact_journal_filter_uses_observed_local_profile_and_preserves_boundary(
    checkout: Path,
) -> None:
    calls: list[runner.Invocation] = []

    def run(invocation: runner.Invocation) -> runner.ProcessResult:
        calls.append(invocation)
        return result(invocation)

    receipt = runner.run_command(
        "test-integration", ["local", "authority_journal_recovery"], checkout, run
    )
    assert receipt["exit_code"] == 0 and receipt["kind"] == "local_integration"
    assert receipt["product_acceptance"] == "NOT_RUN"
    invocation = calls[0]
    assert invocation.consumer == "postgres_python" and invocation.target == TARGET
    assert invocation.environment["OPENTOUR_UNIT_GUARD"] == "1"
    assert invocation.argv[-5:-2] == ("-m", "tools.fixtures.python_postgres", TARGET)
    scope = receipt["steps"][0]["fixture_scope"]
    assert scope == {
        "postgresql": "actual_local",
        "aws_responses": "simulated_only",
        "sql_consumer": "test_fixture",
        "provider_acceptance": "NOT_RUN",
        "production_grant_consumer": "NOT_RUN",
    }
    assert receipt["steps"][0]["artifacts"][0]["content"]["owner"] == "OPE-217"
    assert {test["mode"] for test in receipt["steps"][0]["tests"]["tests"]} == set(runner.MODES)
    assert not invocation.report.exists()


@pytest.mark.parametrize(
    "change,code",
    [
        ("missing_pg", 3),
        ("zero", 2),
        ("failed", 1),
        ("skipped", 1),
        ("one_mode", 2),
        ("kind", 2),
        ("nonce", 2),
        ("source", 2),
        ("report", 2),
        ("target", 2),
        ("owner", 2),
        ("dirty", 2),
        ("cleanup", 1),
        ("no_instances", 1),
    ],
)
def test_journal_result_cannot_upgrade_missing_or_invalid_evidence(
    checkout: Path, change: str, code: int
) -> None:
    receipt = runner.run_command(
        "test-integration",
        ["local", "authority_journal_recovery"],
        checkout,
        lambda invocation: result(invocation, change),
    )
    assert receipt["exit_code"] == code
    assert receipt["status"] == ("BLOCKED" if code == 3 else "FAIL")


@pytest.mark.parametrize(
    "option",
    [
        "all",
        "authority_journal",
        "authority_journal_recovery.py",
        "-authority_journal_recovery",
        "authority_journal_recovery;echo x",
    ],
)
def test_journal_filter_is_exact_and_never_passed_as_arbitrary_pytest_filter(
    checkout: Path, option: str
) -> None:
    def forbidden(_: runner.Invocation) -> runner.ProcessResult:
        pytest.fail("Invalid filter must be rejected before execution")

    assert (
        runner.run_command("test-integration", ["local", option], checkout, forbidden)["exit_code"]
        == 2
    )


@pytest.mark.parametrize(
    "change",
    ["owner", "path", "kind", "unknown", "filters_list", "legacy_shape", "missing", "symlink"],
)
def test_wrong_registration_or_unsafe_target_is_refused_before_execution(
    checkout: Path, change: str, tmp_path: Path
) -> None:
    path = checkout / runner.COMMON / "command-manifest.json"
    registry = json.loads(path.read_bytes())
    local = registry["integration_profiles"]["local"]
    entry = local["filters"]["authority_journal_recovery"]
    if change in {"owner", "path", "kind"}:
        entry[change] = {
            "owner": "OPE-124",
            "path": "tests/other.py",
            "kind": "provider_integration",
        }[change]
    elif change == "unknown":
        entry["unexpected"] = True
    elif change == "filters_list":
        local["filters"] = []
    elif change == "legacy_shape":
        registry["integration_profiles"]["local"] = entry
    elif change in {"missing", "symlink"}:
        (checkout / TARGET).unlink()
        if change == "symlink":
            outside = tmp_path / "outside.py"
            outside.write_text("# must not execute\n")
            (checkout / TARGET).symlink_to(outside)
    path.write_text(json.dumps(registry))

    def forbidden(_: runner.Invocation) -> runner.ProcessResult:
        pytest.fail("Invalid registration must be rejected before execution")

    assert (
        runner.run_command(
            "test-integration", ["local", "authority_journal_recovery"], checkout, forbidden
        )["exit_code"]
        == 2
    )


def test_ci_inventory_and_native_commands_cover_each_present_fixed_target(checkout: Path) -> None:
    for target in adapter.TARGETS:
        path = checkout / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    assert set(adapter.present_targets(checkout)) == set(adapter.TARGETS)
    for target in adapter.TARGETS:
        command = adapter.target_command(target)
        assert command == (
            ("just", "test-integration", "local", "authority_journal_recovery")
            if target == TARGET
            else ("just", "test-python", target)
        )
    with pytest.raises(ValueError):
        adapter.target_command("tests/integration/unregistered.py")
    receipt = runner.run_command(
        "test-python",
        ["tests/integration"],
        checkout,
        lambda _: pytest.fail("No directory upgrade"),
    )
    assert receipt["exit_code"] == 2
