"""Finite native Python targets using observed PostgreSQL or local TCP/TLS fixtures.

This is test discipline, not a hostile-code sandbox or provider qualification.
The adapter observes the accepted fixture implementation; test booleans/markers do
not establish resource execution or cleanup. The outer C02 process group owns crashes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from typing import Any

from tools.fixtures.postgres import VERSION, DisposablePostgres, PrerequisiteMissing

ROOT = Path(__file__).resolve().parents[2]
PILOT_POSTGRES = "services/control-api/tests/auth/test_browser_private_pilot_postgres.py"
TARGETS = {
    "services/control-api/tests/migrations/test_expand_contract.py": "OPE-213",
    "services/control-api/tests/auth/test_identity_postgres.py": "OPE-142",
    "services/control-api/tests/auth/test_grants_postgres.py": "OPE-143",
    "tests/integration/authority_journal_recovery.py": "OPE-217",
    "services/control-api/tests/organizations/test_creation_saga.py": "OPE-171",
    "services/control-api/tests/organizations/test_session_selection.py": "OPE-172",
    "services/control-api/tests/runners/test_enrollment_postgres.py": "OPE-184",
    PILOT_POSTGRES: "OPE-142",
}
TLS_TARGETS = {
    "services/control-api/tests/auth/test_browser_private_listener.py": "OPE-142",
    "services/control-api/tests/auth/test_browser_private_pilot_listener.py": "OPE-142",
    PILOT_POSTGRES: "OPE-142",
}
SCHEMA_ID = "urn:opentour:python-postgres-observation:1.0"


def target_command(target: str) -> tuple[str, ...]:
    """Exact native command for CI; no arbitrary filter or implicit unit exclusion."""
    if target not in TARGETS and target not in TLS_TARGETS:
        raise ValueError("Local Python target is not registered")
    if target == "tests/integration/authority_journal_recovery.py":
        return ("just", "test-integration", "local", "authority_journal_recovery")
    return ("just", "test-python", target)


def present_targets(root: Path) -> tuple[str, ...]:
    """CI inventory: every existing finite target needs the required local job."""
    result = []
    for target in TARGETS.keys() | TLS_TARGETS.keys():
        path = root / target
        if path.exists() or path.is_symlink():
            if (
                not path.is_file()
                or path.is_symlink()
                or not path.resolve().is_relative_to(root.resolve())
            ):
                raise ValueError("Registered local Python target is not a safe repository file")
            result.append(target)
    return tuple(sorted(result))


def source_paths(target: str) -> tuple[str, ...]:
    paths: tuple[str, ...] = (
        target,
        "tools/fixtures/postgres.py",
        "tools/fixtures/python_postgres.py",
    )
    if target in TLS_TARGETS:
        paths += ("packages/contracts/common/python-unit/sitecustomize.py",)
    if target == PILOT_POSTGRES:
        paths += ("services/control-api/tests/auth/private_browser_client_postgres_probe.mjs",)
    return paths


def source_digest(root: Path, target: str) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                (path, hashlib.sha256((root / path).read_bytes()).hexdigest())
                for path in source_paths(target)
            ],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


def case_mode(item: Any) -> str | None:
    """An observed execution variant, never a fixture-kind or authority upgrade."""
    parameters = getattr(getattr(item, "callspec", None), "params", {})
    if "mode" not in parameters:
        return None
    mode = parameters["mode"]
    if mode not in ("customer_controlled", "opentour_managed"):
        raise ValueError("Registered PostgreSQL case has an invalid deployment mode")
    return str(mode)


class Lifecycle:
    """Retain exact live handles and paths observed before test code regains control."""

    def __init__(self) -> None:
        self.instances: list[tuple[DisposablePostgres, subprocess.Popen[bytes] | None, Path]] = []
        self.binaries: dict[str, str] | None = None
        self.enter = DisposablePostgres.__enter__
        self.close = DisposablePostgres.close
        self.cleanup_complete = True
        self.started_instances = 0

    def install(self) -> None:
        observer = self

        def observed_enter(instance: DisposablePostgres) -> DisposablePostgres:
            try:
                result = observer.enter(instance)
            except BaseException:
                # Startup failure may still have a live postmaster or retained recovery
                # directory. Observe it before exception handling returns to test code.
                if instance.root is not None:
                    observer.instances.append((instance, instance.process, instance.root))
                raise
            process, root = instance.process, instance.root
            if process is None or root is None or process.poll() is not None:
                raise RuntimeError("PostgreSQL startup was not observed")
            observer.instances.append((instance, process, root))
            observer.started_instances += 1
            hashes = instance.receipt.get("binary_sha256")
            if not isinstance(hashes, dict) or set(hashes) != {"postgres", "initdb", "psql"}:
                raise RuntimeError("PostgreSQL binary qualification was not observed")
            if observer.binaries is not None and hashes != observer.binaries:
                raise RuntimeError("PostgreSQL fixture changed binaries")
            observer.binaries = hashes.copy()
            return result

        DisposablePostgres.__enter__ = observed_enter  # type: ignore[method-assign]

    def finish(self) -> bool:
        # Inspect the original handles, never test-supplied replacement booleans/PIDs.
        try:
            for instance, process, root in self.instances:
                try:
                    running = process is not None and process.poll() is None
                    if running or (process is None and root.exists()):
                        self.cleanup_complete = False
                        if instance.process is process and instance.root == root:
                            self.close(instance)
                        elif process is not None:
                            # Only the retained exact handle is eligible. Signal refusal
                            # stops further attempts; unknown ownership is never success.
                            process.terminate()
                            try:
                                process.wait(timeout=8)
                            except subprocess.TimeoutExpired:
                                process.kill()
                                process.wait(timeout=3)
                    if (
                        (process is not None and process.poll() != 0)
                        or root.exists()
                        or instance.cleanup_complete is not True
                    ):
                        self.cleanup_complete = False
                except Exception:
                    self.cleanup_complete = False
                    # Keep the exact marked directory for recovery; do not remove a
                    # possibly live cluster or copy private path/PID data to receipts.
        finally:
            DisposablePostgres.__enter__ = self.enter  # type: ignore[method-assign]
        return self.cleanup_complete


def write_json(path: Path, value: dict[str, Any]) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, sort_keys=True)


def main(argv: list[str] | None = None) -> int:
    import pytest

    from tools import test_runner

    parser = argparse.ArgumentParser()
    parser.add_argument("target", choices=sorted(TARGETS.keys() | TLS_TARGETS.keys()))
    parser.add_argument("--result-file", required=True)
    args = parser.parse_args(argv)
    target = args.target
    destination = Path(args.result_file)
    nonce = os.environ.get("OPENTOUR_RESULT_NONCE", "")
    if (
        os.environ.get("OPENTOUR_TEST_TARGET") != target
        or os.environ.get("OPENTOUR_RESULT_FILE") != str(destination)
        or os.environ.get("OPENTOUR_REPOSITORY_ROOT") != str(ROOT)
        or not re.fullmatch(r"[a-f0-9]{32}", nonce)
        or destination.exists()
        or destination.is_symlink()
        or not destination.parent.is_dir()
        or destination.parent.is_symlink()
        or not (ROOT / target).is_file()
        or (ROOT / target).is_symlink()
        or not (ROOT / target).resolve().is_relative_to(ROOT.resolve())
    ):
        return 2
    test_runner.install_unit_guard()
    before = source_digest(ROOT, target)
    report = test_runner.report_envelope("pytest", target, dict(os.environ), [])
    report["kind"] = "local_integration"
    observer = None
    if target in TARGETS:
        try:
            DisposablePostgres().qualify_binaries()
        except PrerequisiteMissing:
            report.update(status="BLOCKED", reason_code="MISSING_EXTERNAL_PREREQUISITE")
            write_json(destination, report)
            return 3
        observer = Lifecycle()
    loopback = None
    inner = destination.with_name(destination.name + ".pytest.json")
    os.environ["OPENTOUR_RESULT_FILE"] = str(inner)
    code = 2
    try:
        with ExitStack() as stack:
            if observer is not None:
                observer.install()
                os.environ["OPENTOUR_POSTGRES_TARGET"] = target
            if target in TLS_TARGETS:
                scope = subprocess._opentour_supervised_loopback  # type: ignore[attr-defined]
                loopback = stack.enter_context(scope())
            code = int(pytest.main(["-p", "tools.test_runner", "-q", "--", target]))
    finally:
        postgres_clean = observer.finish() if observer is not None else True
        os.environ["OPENTOUR_RESULT_FILE"] = str(destination)
    if inner.is_file() and not inner.is_symlink() and inner.stat().st_size <= 8_388_608:
        report = json.loads(inner.read_bytes())
        report["kind"] = "local_integration"
    if code == 0 and (
        (observer is not None and not observer.started_instances)
        or (loopback is not None and not loopback["accepted_connections"])
        or source_digest(ROOT, target) != before
    ):
        code = 1
        report.update(status="FAIL", reason_code="RUNNER_ERROR")
    if not postgres_clean or (loopback is not None and not loopback["cleanup_complete"]):
        code = 1
        report.update(status="FAIL", reason_code="CLEANUP_FAILED", cleanup_complete=False)
    if code not in (0, 1):
        code = 2
        report.update(status="FAIL", reason_code="INVALID_CONFIGURATION")
    if report["skipped"] or report["failed"]:
        code = 1
    if not report["collected"]:
        code = 2
        report.update(status="FAIL", reason_code="NO_TESTS")
    binding = {
        "target": target,
        "binding_nonce": nonce,
        "source_sha256": before,
        "report_sha256": hashlib.sha256(json.dumps(report, sort_keys=True).encode()).hexdigest(),
        "kind": "local_integration",
        "product_acceptance": "NOT_RUN",
    }
    if observer is not None:
        write_json(
            destination.with_name(destination.name + ".postgres.json"),
            {
                **binding,
                "schema_version": "opentour.python-postgres-observation.v1",
                "owner": TARGETS[target],
                "postgres_version": VERSION,
                "started_instances": observer.started_instances,
                "cleanup_complete": postgres_clean,
                "binary_sha256": observer.binaries,
            },
        )
    if loopback is not None:
        write_json(
            destination.with_name(destination.name + ".loopback.json"),
            {
                **binding,
                **loopback,
                "schema_version": "opentour.python-loopback-observation.v1",
                "owner": TLS_TARGETS[target],
            },
        )
    write_json(destination, report)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
