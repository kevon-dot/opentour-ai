"""Controlled OPE-253 transport/quality checks; no rendered UI acceptance is claimed."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tools import test_runner as runner

TARGET = "apps/web/components/foundation/tokens/visual.spec.ts"
FILES = (
    TARGET,
    "apps/web/components/foundation/tokens/index.tsx",
    "apps/web/components/foundation/tokens/testing/server.ts",
    "apps/web/components/foundation/tokens/testing/index.html",
    "apps/web/components/foundation/tokens/testing/gallery.tsx",
    "apps/web/components/foundation/tokens/testing/baseline-review.json",
    "apps/web/styles/product-tokens.css",
    "apps/web/styles/product-tokens.json",
    "apps/web/styles/fonts/Geist.woff2",
    "apps/web/styles/fonts/GeistMono.woff2",
    "apps/web/styles/fonts/source.json",
)


def write(root: Path, name: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("Generated controlled transport fixture; not a font or rendered source.\n")
    return path


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "controlled checkout with spaces"
    root.mkdir()
    shutil.copytree(runner.ROOT / "packages/contracts", root / "packages/contracts")
    shutil.copyfile(runner.ROOT / "pyproject.toml", root / "pyproject.toml")
    for path in (*FILES, "tools/browser-test-runner.ts"):
        write(root, path)
    return root


def controlled_result(invocation: runner.Invocation, change: str = "") -> runner.ProcessResult:
    tests: list[dict[str, Any]] = [
        {"id": "generated-" + mode, "file": invocation.target, "status": "passed", "mode": mode}
        for mode in runner.MODES
    ]
    if change == "zero":
        tests = []
    elif change == "one_mode":
        tests = tests[:1]
    elif change in {"null_mode", "invalid_mode"}:
        tests[0]["mode"] = None if change == "null_mode" else "generated-invalid"
    elif change in {"failed", "skipped"}:
        tests[0]["status"] = change
    report = runner.report_envelope("playwright", invocation.target, invocation.environment, tests)
    if change == "nonce":
        report["binding_nonce"] = "f" * 32
    elif change == "target":
        report["target"] = "apps/web/tests/browser-harness.browser.spec.ts"
    elif change == "file":
        report["tests"][0]["file"] = "apps/web/tests/browser-harness.browser.spec.ts"
    elif change == "kind":
        report["kind"] = "provider_integration"
    elif change == "counts":
        report["passed"] += 1
    elif change == "cleanup":
        report["cleanup_complete"] = False
    invocation.report.write_text(json.dumps(report))
    return runner.ProcessResult(
        1 if change == "failed" else 0,
        cleanup_complete=change != "outer_cleanup",
        timed_out=change == "timeout",
        interrupted=change == "interrupt",
    )


def test_exact_foundation_target_preserves_own_modes_and_transport_identity(checkout: Path) -> None:
    calls: list[runner.Invocation] = []

    def run(invocation: runner.Invocation) -> runner.ProcessResult:
        calls.append(invocation)
        return controlled_result(invocation)

    receipt = runner.run_command("test-browser", [TARGET], checkout, run)
    assert receipt["status"] == "PASS" and receipt["exit_code"] == 0
    assert receipt["kind"] == "simulated" and receipt["product_acceptance"] == "NOT_RUN"
    assert receipt["fixture_id"] == "bootstrap-command-contract"
    assert len(calls) == 1
    assert calls[0].argv[:3] == ("node", "tools/browser-test-runner.ts", TARGET)
    assert calls[0].environment["OPENTOUR_TEST_TARGET"] == TARGET
    result = receipt["steps"][0]["tests"]
    assert result["target"] == TARGET and result["collected"] == result["passed"] == 2
    assert {test["mode"] for test in result["tests"]} == set(runner.MODES)
    assert not calls[0].report.exists()


@pytest.mark.parametrize(
    "change",
    [
        "zero",
        "one_mode",
        "null_mode",
        "invalid_mode",
        "failed",
        "skipped",
        "nonce",
        "target",
        "file",
        "kind",
        "counts",
        "cleanup",
        "outer_cleanup",
        "timeout",
        "interrupt",
    ],
)
def test_incomplete_or_mismatched_browser_evidence_never_passes(
    checkout: Path, change: str
) -> None:
    receipt = runner.run_command(
        "test-browser", [TARGET], checkout, lambda invocation: controlled_result(invocation, change)
    )
    assert receipt["exit_code"] != 0 and receipt["status"] != "PASS"
    assert receipt["kind"] != "provider_integration"


@pytest.mark.parametrize("path", FILES)
def test_missing_owned_host_or_asset_refuses_before_launch(checkout: Path, path: str) -> None:
    (checkout / path).unlink()

    def forbidden(_: runner.Invocation) -> runner.ProcessResult:
        pytest.fail("Missing foundation source must not launch a browser")

    assert runner.run_command("test-browser", [TARGET], checkout, forbidden)["exit_code"] == 2


@pytest.mark.parametrize("change", ["owner", "kind", "undeclared"])
def test_profile_registration_cannot_relabel_foundation_authority(
    checkout: Path, change: str
) -> None:
    path = checkout / runner.COMMON / "command-manifest.json"
    document = json.loads(path.read_text())
    if change == "undeclared":
        del document["browser_profiles"][TARGET]
    else:
        document["browser_profiles"][TARGET][change] = (
            "OPE-127" if change == "owner" else "provider_integration"
        )
    path.write_text(json.dumps(document))

    def forbidden(_: runner.Invocation) -> runner.ProcessResult:
        pytest.fail("Unadmitted profile must not launch")

    assert runner.run_command("test-browser", [TARGET], checkout, forbidden)["exit_code"] == 2


@pytest.mark.parametrize("command", ["lint", "typecheck"])
def test_quality_dispatch_includes_owned_tsx_sources_and_propagates_failure(
    checkout: Path, command: str
) -> None:
    write(checkout, "apps/web/components/foundation/tokens/tsconfig.json")
    # Browser setup is independent of quality dispatch and its C06 files are absent here.
    (checkout / "tools/browser-test-runner.ts").unlink()
    calls: list[runner.Invocation] = []

    def run(invocation: runner.Invocation) -> runner.ProcessResult:
        calls.append(invocation)
        return runner.ProcessResult(1 if "foundation/tokens" in " ".join(invocation.argv) else 0)

    receipt = runner.run_command(command, [], checkout, run)
    assert receipt["status"] == "FAIL" and receipt["exit_code"] == 1
    expected = (
        ("pnpm", "exec", "oxlint", "--deny-warnings", "apps/web/components/foundation/tokens")
        if command == "lint"
        else (
            "pnpm",
            "exec",
            "tsc",
            "--project",
            "apps/web/components/foundation/tokens/tsconfig.json",
        )
    )
    assert calls[-1].argv == expected


def test_partial_or_symlinked_quality_configuration_never_silently_skips(tmp_path: Path) -> None:
    assert runner.foundation_quality_commands(tmp_path, "typecheck") == []
    config = write(tmp_path, "apps/web/components/foundation/tokens/tsconfig.json")
    with pytest.raises(runner.CommandError):
        runner.foundation_quality_commands(tmp_path, "typecheck")
    for path in FILES:
        write(tmp_path, path)
    config.unlink()
    config.symlink_to(tmp_path / "generated-missing-config")
    with pytest.raises(runner.CommandError):
        runner.foundation_quality_commands(tmp_path, "typecheck")
