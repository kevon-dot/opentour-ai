"""OPE-123 command dispatch. Test receipts describe execution, never product acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import re
import selectors
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
_MANAGED_POPEN = getattr(subprocess, "_opentour_managed_popen", subprocess.Popen)
COMMON = "packages/contracts/common"
TRACEABILITY_TARGET = "docs/specification/manifest.json"
MODES = ("customer_controlled", "opentour_managed")
COMMAND_ARITY = {
    "setup": 0,
    "lint": 0,
    "typecheck": 0,
    "test-contract": 0,
    "validate-spec": 0,
    "test-python": 1,
    "test-web": 1,
    "test-browser": 1,
    "test-integration": 2,
    "test-isolation": 2,
    "test-acceptance": 2,
}
EXIT_REASONS = {
    "ASSERTION_FAILED": 1,
    "REQUIRED_TEST_SKIPPED": 1,
    "RUNNER_TIMEOUT": 1,
    "RUNNER_INTERRUPTED": 1,
    "CLEANUP_FAILED": 1,
    "RUNNER_ERROR": 1,
    "NO_TESTS": 2,
    "INVALID_CONFIGURATION": 2,
    "MISSING_BROWSER": 2,
    "MISSING_APPLICATION": 2,
    "MISSING_EXTERNAL_PREREQUISITE": 3,
}


class CommandError(ValueError):
    def __init__(self, message: str, code: int = 2, reason: str = "INVALID_CONFIGURATION"):
        super().__init__(message)
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Invocation:
    argv: tuple[str, ...]
    cwd: Path
    environment: dict[str, str]
    report: Path
    target: str
    runner: str
    timeout: int = 300
    expected_kind: str = "simulated"
    consumer: str | None = None


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str = ""
    timed_out: bool = False
    interrupted: bool = False
    cleanup_complete: bool = True


Executor = Callable[[Invocation], ProcessResult]


def execute(invocation: Invocation) -> ProcessResult:
    """Bound child lifetime and output without shell interpolation or unbounded log files."""
    try:
        process = _MANAGED_POPEN(
            invocation.argv,
            cwd=invocation.cwd,
            env=invocation.environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except FileNotFoundError as exc:
        raise CommandError(
            "Required pinned executable is unavailable; run qualified setup.",
            3,
            "MISSING_EXTERNAL_PREREQUISITE",
        ) from exc
    except OSError as exc:
        raise CommandError("Required runner could not be started.") from exc
    assert process.stdout is not None
    output = bytearray()
    byte_count = 0
    timed_out = False
    interrupted = False
    cleanup_complete = True
    group_permission_denied = False
    deadline = time.monotonic() + invocation.timeout

    def signal_owned_group(sig: int) -> bool:
        nonlocal cleanup_complete, group_permission_denied
        if group_permission_denied:
            return False
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            return False
        except OSError:
            # EPERM/unknown ownership is not evidence that the group is gone. Stop
            # signalling it and preserve a nonpassing, bounded cleanup outcome.
            cleanup_complete = False
            group_permission_denied = True
            return False
        return True

    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        try:
            while selector.get_map():
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                for key, _ in selector.select(
                    timeout=min(0.1, max(0, deadline - time.monotonic()))
                ):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    byte_count += len(chunk)
                    if byte_count > 1_048_576:
                        break
                    if invocation.runner == "setup":
                        output.extend(chunk)
                if byte_count > 1_048_576:
                    break
        except KeyboardInterrupt:
            interrupted = True
        finally:
            if process.poll() is None and (
                timed_out or interrupted or byte_count > 1_048_576 or selector.get_map()
            ):
                signal_owned_group(signal.SIGKILL)
            try:
                process.wait(
                    timeout=0.1
                    if group_permission_denied
                    else max(0.1, deadline - time.monotonic())
                )
            except subprocess.TimeoutExpired:
                timed_out = True
                signal_owned_group(signal.SIGKILL)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    cleanup_complete = False
            process.stdout.close()
            # A leader can exit while descendants keep running with redirected stdio. The
            # private session is ours: terminate leftovers and refuse successful cleanup.
            if signal_owned_group(0):
                cleanup_complete = False
                signal_owned_group(signal.SIGKILL)
    if process.returncode is None:
        raise CommandError("Owned runner cleanup could not be confirmed.", 1, "CLEANUP_FAILED")
    if byte_count > 1_048_576:
        raise CommandError("Runner output exceeded the one MiB limit.", 1, "RUNNER_ERROR")
    return ProcessResult(
        process.returncode,
        output.decode("utf-8", errors="replace"),
        timed_out,
        interrupted,
        cleanup_complete,
    )


def path_target(root: Path, supplied: str, *, require_exists: bool = True) -> str:
    """Validate lexical and resolved boundaries before opening content or launching a child."""
    if (
        not supplied
        or not re.fullmatch(r"[A-Za-z0-9_ ./+-]+", supplied)
        or Path(supplied).is_absolute()
        or any(p in {"", ".", ".."} or p.startswith("-") for p in supplied.split("/"))
    ):
        raise CommandError(
            "Use an explicit repository-relative path without traversal or shell syntax."
        )
    root = root.resolve()
    target = root / supplied
    try:
        resolved = target.resolve()
    except (OSError, RuntimeError) as exc:
        raise CommandError("Requested path cannot be resolved safely.") from exc
    if not resolved.is_relative_to(root):
        raise CommandError("Requested path escapes the repository.")
    if require_exists and not target.exists():
        raise CommandError("Requested test or suite does not exist.")
    if target.exists() and not (target.is_file() or target.is_dir()):
        raise CommandError("Requested path must be a regular file or directory.")
    # Avoid ambiguity and a runner recursively following links that validation did not follow.
    for parent in (target, *target.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise CommandError("Test selections may not contain symlinks.")
    if target.is_dir():
        for current, directories, files in os.walk(target, followlinks=False):
            if any((Path(current) / name).is_symlink() for name in (*directories, *files)):
                raise CommandError("Suite directory contains a symlink; select regular files.")
    return resolved.relative_to(root).as_posix()


def manifest(root: Path) -> dict[str, Any]:
    from tools import schema_validate

    try:
        value = schema_validate.read_json(
            schema_validate.local_file(root, f"{COMMON}/command-manifest.json")
        )
    except schema_validate.SchemaError as exc:
        raise CommandError(str(exc)) from exc
    if (
        not isinstance(value, dict)
        or set(value)
        != {
            "schema_version",
            "issue_key",
            "spec_sha256",
            "native_issue_updated_at",
            "fixture_version",
            "browser_suites",
            "browser_profiles",
            "integration_profiles",
            "isolation_suites",
            "acceptance_gates",
        }
        or value["schema_version"] != "opentour.command-manifest.v1"
        or value["issue_key"] != "OPE-123"
        or not isinstance(value["spec_sha256"], str)
        or not re.fullmatch(r"[a-f0-9]{64}", value["spec_sha256"])
        or not isinstance(value["native_issue_updated_at"], str)
        or not isinstance(value["fixture_version"], str)
        or not isinstance(value["browser_suites"], list)
        or any(not isinstance(p, str) for p in value["browser_suites"])
        or len(set(value["browser_suites"])) != len(value["browser_suites"])
        or any(
            not isinstance(value[key], dict)
            for key in (
                "browser_profiles",
                "integration_profiles",
                "isolation_suites",
                "acceptance_gates",
            )
        )
    ):
        raise CommandError("Invalid command registry.")
    for entry in value["browser_suites"]:
        path_target(root, entry, require_exists=False)
    for target, profile in value["browser_profiles"].items():
        if (
            target not in value["browser_suites"]
            or not isinstance(profile, dict)
            or set(profile) != {"kind", "owner"}
            or not isinstance(profile["kind"], str)
            or profile["kind"]
            not in {"simulated", "local_integration", "provider_integration", "customer_evidence"}
            or not isinstance(profile["owner"], str)
        ):
            raise CommandError("Invalid registered browser execution profile.")
    for key in ("integration_profiles", "isolation_suites", "acceptance_gates"):
        for name, profile in value[key].items():
            if (
                not isinstance(name, str)
                or not isinstance(profile, dict)
                or set(profile) != {"kind", "path", "prerequisites", "owner"}
                or not isinstance(profile["kind"], str)
                or profile["kind"]
                not in {
                    "simulated",
                    "local_integration",
                    "provider_integration",
                    "customer_evidence",
                }
                or (profile["path"] is not None and not isinstance(profile["path"], str))
                or not isinstance(profile["prerequisites"], list)
                or any(not isinstance(item, str) for item in profile["prerequisites"])
                or not isinstance(profile["owner"], str)
            ):
                raise CommandError("Invalid registered profile contract.")
    return value


def environment(root: Path, nonce: str) -> dict[str, str]:
    # Do not inherit provider/customer credentials, pytest flags, NODE_OPTIONS or plugin injection.
    safe = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT")
        if key in os.environ
    }
    safe.update(
        {
            "CI": "true",
            "TZ": "UTC",
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "PYTHONPATH": str(root),
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "UV_OFFLINE": "1",
            "UV_PYTHON_DOWNLOADS": "never",
            "NEXT_TELEMETRY_DISABLED": "1",
            "OPENTOUR_RESULT_NONCE": nonce,
            "OPENTOUR_REPOSITORY_ROOT": str(root),
        }
    )
    return safe


def uv_python(root: Path, target: str | None = None) -> tuple[str, ...]:
    from tools import schema_validate

    argv = ("uv", "run", "--no-sync", "--offline", "--no-python-downloads")
    if target:
        try:
            project = tomllib.loads((root / "pyproject.toml").read_text())
            members = project["tool"]["uv"]["workspace"]["members"]
            for member in members:
                if target == member or target.startswith(member + "/"):
                    package_file = schema_validate.local_file(root, f"{member}/pyproject.toml")
                    package = tomllib.loads(package_file.read_text())["project"]["name"]
                    return (*argv, "--package", package, "python")
            if target.startswith("training/"):
                return (*argv, "--project", "training", "python")
            if not target.startswith(("tests/", "tools/", "packages/contracts/examples/")):
                raise CommandError("Test path has no declared Python workspace/package mapping.")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise CommandError("Invalid Python workspace/package mapping.") from exc
    return (*argv, "python")


def validate_report(
    report: Any,
    invocation: Invocation,
    process: ProcessResult,
    root: Path,
) -> tuple[int, str | None]:
    from tools import schema_validate

    registry = schema_validate.schema_registry(root)
    if not schema_validate.valid_instance("urn:opentour:test-result:1.0", report, registry):
        raise CommandError("Runner report is missing required fields or has an invalid shape.")
    if any(
        report[key] != expected
        for key, expected in (
            ("runner", invocation.runner),
            ("target", invocation.target),
            ("binding_nonce", invocation.environment["OPENTOUR_RESULT_NONCE"]),
            ("kind", invocation.expected_kind),
        )
    ):
        raise CommandError("Runner report identity does not match this invocation.")
    counts = {"passed": 0, "failed": 0, "skipped": 0}
    ids: set[str] = set()
    target = root / invocation.target
    for test in report["tests"]:
        if test["id"] in ids:
            raise CommandError("Runner report contains duplicate test identities.")
        ids.add(test["id"])
        file = path_target(root, test["file"])
        if file != invocation.target and not (
            target.is_dir() and file.startswith(invocation.target + "/")
        ):
            raise CommandError("Runner collected a file outside the exact requested selection.")
        if report["mode"] is not None and test["mode"] != report["mode"]:
            raise CommandError("Runner mode does not match test evidence.")
        counts[test["status"]] += 1
    if report["collected"] != len(ids) or any(report[k] != v for k, v in counts.items()):
        raise CommandError("Runner report counts do not match its test inventory.")
    if not report["cleanup_complete"]:
        return 1, "CLEANUP_FAILED"
    if process.timed_out:
        return 1, "RUNNER_TIMEOUT"
    if report["status"] == "BLOCKED":
        if (
            process.returncode != 3
            or report["reason_code"] != "MISSING_EXTERNAL_PREREQUISITE"
            or ids
        ):
            raise CommandError("Invalid prerequisite-blocked report.")
        return 3, "MISSING_EXTERNAL_PREREQUISITE"
    if counts["skipped"]:
        return 1, "REQUIRED_TEST_SKIPPED"
    if not ids:
        reason = report["reason_code"] or "NO_TESTS"
        if reason == "MISSING_EXTERNAL_PREREQUISITE":
            raise CommandError("External prerequisites require an explicit blocked report.")
        return EXIT_REASONS[reason], reason
    if counts["failed"] or process.returncode or report["status"] != "PASS":
        return 1, report["reason_code"] or "RUNNER_ERROR"
    if report["reason_code"] is not None:
        raise CommandError("Passing report has a contradictory failure reason.")
    return 0, None


def vitest_report(raw: Any, invocation: Invocation) -> dict[str, Any]:
    """Normalize the pinned JSON reporter, retaining every selected file and pending case."""
    if not isinstance(raw, dict) or not isinstance(raw.get("testResults"), list):
        raise CommandError("Missing or malformed Vitest JSON report.")
    tests = []
    for suite in raw["testResults"]:
        try:
            file = Path(suite["name"]).resolve().relative_to(invocation.cwd.resolve()).as_posix()
            for index, case in enumerate(suite["assertionResults"]):
                state = case["status"]
                status = (
                    "passed" if state == "passed" else "failed" if state == "failed" else "skipped"
                )
                tests.append(
                    {"id": f"{file}::{index}", "file": file, "status": status, "mode": None}
                )
        except (KeyError, ValueError, TypeError) as exc:
            raise CommandError("Invalid Vitest test inventory.") from exc
    result = report_envelope(invocation.runner, invocation.target, invocation.environment, tests)
    expected = {
        "numTotalTests": "collected",
        "numPassedTests": "passed",
        "numFailedTests": "failed",
    }
    if any(type(raw.get(k)) is not int or raw[k] != result[v] for k, v in expected.items()):
        raise CommandError("Vitest summary and actual inventory disagree.")
    if (
        type(raw.get("numPendingTests")) is not int
        or type(raw.get("numTodoTests")) is not int
        or raw["numPendingTests"] < 0
        or raw["numTodoTests"] < 0
        or raw["numPendingTests"] + raw["numTodoTests"] != result["skipped"]
    ):
        raise CommandError("Vitest pending/todo counts do not match the inventory.")
    if raw.get("success") is not True:
        result.update(status="FAIL", reason_code="RUNNER_ERROR")
    return result


def report_envelope(
    runner: str,
    target: str,
    env: dict[str, str],
    tests: list[dict[str, Any]],
) -> dict[str, Any]:
    counts = {
        status: sum(t["status"] == status for t in tests)
        for status in ("passed", "failed", "skipped")
    }
    passed = bool(tests) and not counts["failed"] and not counts["skipped"]
    reason = (
        None
        if passed
        else "NO_TESTS"
        if not tests
        else ("REQUIRED_TEST_SKIPPED" if counts["skipped"] else "ASSERTION_FAILED")
    )
    return {
        "schema_version": "opentour.test-result.v1",
        "runner": runner,
        "target": target,
        "binding_nonce": env["OPENTOUR_RESULT_NONCE"],
        "kind": "simulated",
        "mode": None,
        "collected": len(tests),
        **counts,
        "tests": tests,
        "status": "PASS" if passed else "FAIL",
        "reason_code": reason,
        "cleanup_complete": True,
    }


def invoke(
    argv: Sequence[str],
    root: Path,
    target: str,
    runner: str,
    run: Executor,
    expected_kind: str = "simulated",
    consumer: str | None = None,
) -> dict[str, Any]:
    """Retain an attempted step even when its report/configuration is invalid."""
    from tools import schema_validate

    process: ProcessResult | None = None

    def recorded(invocation: Invocation) -> ProcessResult:
        nonlocal process
        process = run(invocation)
        return process

    try:
        return _invoke(argv, root, target, runner, recorded, expected_kind, consumer)
    except (CommandError, schema_validate.SchemaError) as exc:
        code, reason = (
            (exc.code, exc.reason)
            if isinstance(exc, CommandError)
            else (2, "INVALID_CONFIGURATION")
        )
        return {
            "runner": runner,
            "target": target,
            "raw_exit_code": process.returncode if process else None,
            "exit_code": code,
            "reason_code": reason,
            "message": str(exc),
            "tests": None,
        }


def traceability_result(
    invocation: Invocation,
    process: ProcessResult,
    expected_commit: str,
    expected_spec: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate the owning C05 semantic report without relabeling it as test execution."""
    from tools import schema_validate

    def read_artifact(path: Path, limit: int) -> bytes:
        if (
            not path.is_file()
            or path.is_symlink()
            or path.parent.is_symlink()
            or path.resolve().parent != invocation.report.parent
            or path.stat().st_size > limit
        ):
            raise CommandError("Traceability output is missing, oversized or unsafe.")
        return path.read_bytes()

    try:
        machine = read_artifact(invocation.report, 4_194_304)
        report = schema_validate.read_json(invocation.report)
        if not schema_validate.valid_bound_document(
            invocation.cwd, f"{COMMON}/traceability-report-binding.json", report
        ):
            raise CommandError("Traceability report violates its owning schema.")
        readable = read_artifact(invocation.report.with_name("findings.txt"), 1_048_576)
        findings = readable.decode("utf-8")
        if not findings.strip():
            raise CommandError("Traceability validator produced no readable findings.")
        if any(
            report[key] != expected
            for key, expected in (
                ("target", invocation.target),
                ("binding_nonce", invocation.environment["OPENTOUR_RESULT_NONCE"]),
                ("commit_sha", expected_commit),
                ("spec_sha256", expected_spec),
            )
        ):
            raise CommandError("Traceability report identity does not match this invocation.")
        if (
            source_identity(invocation.cwd)[0] != expected_commit
            or hashlib.sha256((invocation.cwd / invocation.target).read_bytes()).hexdigest()
            != expected_spec
        ):
            raise CommandError("Traceability source changed while validation was running.")
        checks = report["checks"]
        failed = [check for check in checks if check["status"] == "FAIL"]
        if (
            len({check["id"] for check in checks}) != len(checks)
            or report["collected"] != len(checks)
            or report["failed"] != len(failed)
            or report["passed"] != len(checks) - len(failed)
        ):
            raise CommandError("Traceability check identity or counts are inconsistent.")
        code = process.returncode
        expected_status = "PASS" if code == 0 else "BLOCKED" if code == 3 else "FAIL"
        expected_reason = {
            0: None,
            1: "TRACEABILITY_FAILED",
            2: "INVALID_CONFIGURATION",
            3: "MISSING_EXTERNAL_PREREQUISITE",
        }.get(code)
        if (
            code not in (0, 1, 2, 3)
            or report["exit_code"] != code
            or report["status"] != expected_status
            or report["reason_code"] != expected_reason
            or (code == 0 and failed)
            or (code != 0 and not failed)
            or (
                code == 3
                and any(check["code"] != "MISSING_EXTERNAL_PREREQUISITE" for check in failed)
            )
        ):
            raise CommandError("Traceability status does not match the observed check outcome.")
        if not report["cleanup_complete"]:
            raise CommandError("Traceability cleanup was not complete.", 1, "CLEANUP_FAILED")
        return report, [
            {
                "type": "c05-traceability-report",
                "sha256": hashlib.sha256(machine).hexdigest(),
                "content": report,
            },
            {
                "type": "c05-readable-findings",
                "sha256": hashlib.sha256(readable).hexdigest(),
                "content": findings,
            },
        ]
    except (OSError, UnicodeError, RuntimeError) as exc:
        raise CommandError("Traceability output could not be read safely.") from exc


def _invoke(
    argv: Sequence[str],
    root: Path,
    target: str,
    runner: str,
    run: Executor,
    expected_kind: str,
    consumer: str | None,
) -> dict[str, Any]:
    from tools import schema_validate

    with tempfile.TemporaryDirectory(prefix="opentour-command-") as temporary:
        private_dir = Path(temporary).resolve()
        report_path = private_dir / "result.json"
        env = environment(root, uuid.uuid4().hex)
        env.update({"OPENTOUR_RESULT_FILE": str(report_path), "OPENTOUR_TEST_TARGET": target})
        args = list(argv)
        expected_commit: str | None = None
        expected_spec: str | None = None
        if consumer == "local_harness":
            args.extend(["--result-file", str(report_path)])
        elif runner in {"pytest", "semantic_validator"}:
            if runner == "semantic_validator":
                expected_commit = source_identity(root)[0]
                if expected_commit is None:
                    raise CommandError("Traceability requires a readable source commit.")
                expected_spec = hashlib.sha256(
                    schema_validate.local_file(root, target).read_bytes()
                ).hexdigest()
                # Validate the owned binding before executing the consumer. The empty value
                # is intentionally not a report; binding/schema errors still fail here.
                schema_validate.valid_bound_document(
                    root, f"{COMMON}/traceability-report-binding.json", {}
                )
                args.extend(
                    [
                        "--result-file",
                        str(report_path),
                        "--findings-file",
                        str(private_dir / "findings.txt"),
                    ]
                )
            guard = schema_validate.local_file(root, f"{COMMON}/python-unit/sitecustomize.py")
            try:
                compile(guard.read_text(encoding="utf-8"), str(guard), "exec")
            except (OSError, UnicodeError, SyntaxError) as exc:
                raise CommandError("Invalid inherited unit startup guard.") from exc
            env["PYTHONPATH"] = os.pathsep.join((str(guard.parent), str(root)))
            env["OPENTOUR_UNIT_GUARD"] = "1"
        elif runner == "playwright":
            args.extend(["--result-file", str(report_path)])
        elif runner == "vitest":
            args[args.index("REPORT")] = str(report_path)
            # Positional Vitest filters are substrings. An exact include is applied before
            # collection, so colliding filenames cannot execute and merely be rejected later.
            configuration = private_dir / "vitest.config.mjs"
            configuration.write_text(
                "export default "
                + json.dumps(
                    {
                        "root": str(root),
                        "test": {
                            "include": [target],
                            "exclude": [],
                            "passWithNoTests": False,
                            "allowOnly": False,
                            "retry": 0,
                            "watch": False,
                            "fileParallelism": False,
                            "maxWorkers": 1,
                        },
                    }
                )
                + ";\n",
                encoding="utf-8",
            )
            args.extend(["--config", str(configuration)])
            guard = schema_validate.local_file(root, f"{COMMON}/unit-network-guard.cjs")
            env["NODE_OPTIONS"] = f"--require={json.dumps(str(guard))}"
        elif runner == "setup":
            env.pop("UV_OFFLINE", None)
        invocation = Invocation(
            tuple(args),
            root,
            env,
            report_path,
            target,
            runner,
            expected_kind=expected_kind,
            consumer=consumer,
        )
        process = run(invocation)
        report: dict[str, Any] | None = None
        validation: dict[str, Any] | None = None
        validation_artifacts: list[dict[str, Any]] = []
        reason: str | None = None
        if process.timed_out:
            code, reason = 1, "RUNNER_TIMEOUT"
        elif process.interrupted:
            code, reason = 1, "RUNNER_INTERRUPTED"
        elif not process.cleanup_complete:
            code, reason = 1, "CLEANUP_FAILED"
        elif runner == "semantic_validator":
            if process.returncode < 0:
                raise CommandError("Traceability runner was interrupted.", 1, "RUNNER_INTERRUPTED")
            assert expected_commit is not None and expected_spec is not None
            validation, validation_artifacts = traceability_result(
                invocation, process, expected_commit, expected_spec
            )
            code, reason = validation["exit_code"], validation["reason_code"]
        elif runner in {"pytest", "vitest", "playwright"}:
            if process.returncode < 0 and not report_path.is_file():
                return {
                    "runner": runner,
                    "target": target,
                    "raw_exit_code": process.returncode,
                    "exit_code": 1,
                    "reason_code": "RUNNER_INTERRUPTED",
                    "tests": None,
                }
            if (
                not report_path.is_file()
                or report_path.is_symlink()
                or report_path.parent.is_symlink()
                or report_path.resolve().parent != private_dir
                or report_path.stat().st_size > 8_388_608
            ):
                raise CommandError("Runner did not produce a fresh result report.")
            try:
                report = schema_validate.read_json(report_path)
            except schema_validate.SchemaError as exc:
                raise CommandError("Runner produced invalid JSON.") from exc
            if runner == "vitest":
                report = vitest_report(report, invocation)
            code, reason = validate_report(report, invocation, process, root)
            # Pytest codes are not the wrapper's public code vocabulary.
            if runner == "pytest" and consumer is None and process.returncode in {2, 3, 4}:
                code, reason = 2, "INVALID_CONFIGURATION"
            elif runner == "pytest" and consumer is None and process.returncode == 5:
                code, reason = 2, "NO_TESTS"
        elif runner == "setup":
            try:
                setup = json.loads(process.stdout.strip())
            except (ValueError, TypeError) as exc:
                raise CommandError("Setup did not produce its required receipt.") from exc
            code = process.returncode
            if (
                not isinstance(setup, dict)
                or code not in (0, 1, 2, 3)
                or setup.get("exit_code") != code
            ):
                raise CommandError("Setup exit and receipt disagree.")
            if code == 0 and (setup.get("status") != "PASS" or setup.get("issue_key") != "OPE-122"):
                raise CommandError("Setup receipt does not establish successful setup.")
            reason = None if code == 0 else "RUNNER_ERROR"
        else:
            code = 0 if process.returncode == 0 else 1
            reason = None if code == 0 else "RUNNER_ERROR"
        summary = {
            "runner": runner,
            "target": target,
            "raw_exit_code": process.returncode,
            "exit_code": code,
            "reason_code": reason,
            "process_cleanup_complete": process.cleanup_complete,
            "tests": report,
        }
        if validation:
            summary["validation"] = {
                key: value for key, value in validation.items() if key != "checks"
            }
            summary["artifacts"] = validation_artifacts
        if report:
            # Parameterized test names can contain submitted values. Preserve stable identity
            # for correlation without copying those values into the public command receipt.
            summary["tests"] = {
                **report,
                "tests": [
                    {**test, "id": "sha256:" + hashlib.sha256(test["id"].encode()).hexdigest()}
                    for test in report["tests"]
                ],
            }
        if consumer == "local_harness" and report:
            if code == 0 and not set(MODES).issubset({test["mode"] for test in report["tests"]}):
                raise CommandError(
                    "Local harness must report actual tests in both deployment modes."
                )
            sidecar = report_path.with_name(report_path.name + ".observations.json")
            if sidecar.exists() or code == 0:
                if (
                    not sidecar.is_file()
                    or sidecar.is_symlink()
                    or sidecar.resolve().parent != private_dir
                    or sidecar.stat().st_size > 1_048_576
                ):
                    raise CommandError("Local harness observation sidecar is missing or unsafe.")
                observations = schema_validate.read_json(sidecar)
                if not schema_validate.valid_bound_document(
                    root, f"{COMMON}/local-observations-binding.json", observations
                ):
                    raise CommandError("Local harness observations violate their owning schema.")
                if code == 0 and not observations["observations"]:
                    raise CommandError("Passing local harness produced no observations.")
                summary["artifacts"] = [
                    {
                        "type": "c03-observations",
                        "sha256": hashlib.sha256(sidecar.read_bytes()).hexdigest(),
                        "content": observations,
                    }
                ]
        summary["output_sha256"] = hashlib.sha256(
            json.dumps(summary, sort_keys=True).encode()
        ).hexdigest()
        return summary


def python_test(root: Path, target: str, run: Executor) -> dict[str, Any]:
    if (root / target).is_file() and not target.endswith(".py"):
        raise CommandError("test-python expects a Python file or declared package test directory.")
    return invoke(
        (*uv_python(root, target), "-m", "pytest", "-p", "tools.test_runner", "-q", "--", target),
        root,
        target,
        "pytest",
        run,
    )


def browser_quality_commands(root: Path, command: str) -> list[tuple[str, ...]]:
    """Run the owning C06 configurations when harness source is present; no partial skip."""
    harness = "apps/web/tests/fixtures/harness"
    required = (
        "playwright.config.ts",
        "tools/browser-test-runner.ts",
        "apps/web/tests/browser-harness.browser.spec.ts",
        f"{harness}/tsconfig.json",
        f"{harness}/.oxlintrc.json",
    )
    if not any((root / name).exists() or (root / name).is_symlink() for name in required):
        return []
    for name in (*required, harness):
        path_target(root, name)
    if command == "lint":
        return [
            (
                "pnpm",
                "exec",
                "oxlint",
                "--config",
                f"{harness}/.oxlintrc.json",
                "--deny-warnings",
                *required[:3],
                harness,
                "packages/contracts/examples/web-command-smoke.test.ts",
            )
        ]
    return [("pnpm", "exec", "tsc", "--project", f"{harness}/tsconfig.json")]


def dispatch(
    command: str, args: list[str], root: Path, registry: dict[str, Any], run: Executor
) -> list[dict[str, Any]]:
    from tools import schema_validate

    if command not in COMMAND_ARITY or len(args) != COMMAND_ARITY[command]:
        raise CommandError("Unknown command or incorrect number of arguments.")
    if command == "setup":
        return [invoke((sys.executable, "tools/bootstrap.py"), root, "setup", "setup", run)]
    if command in {"test-python", "test-web", "test-browser"}:
        target = path_target(root, args[0])
        if command == "test-python":
            return [python_test(root, target, run)]
        if command == "test-web":
            if not target.endswith((".test.ts", ".test.tsx")) or not (root / target).is_file():
                raise CommandError(
                    "test-web accepts .test.ts/.test.tsx files; use test-browser for .spec.ts."
                )
            return [
                invoke(
                    (
                        "pnpm",
                        "exec",
                        "vitest",
                        "run",
                        "--reporter=json",
                        "--outputFile",
                        "REPORT",
                        "--no-file-parallelism",
                        "--maxWorkers=1",
                    ),
                    root,
                    target,
                    "vitest",
                    run,
                )
            ]
        if (root / target).is_file() and not target.endswith(".spec.ts"):
            raise CommandError(
                "test-browser accepts declared .spec.ts suites; use test-web for Vitest tests."
            )
        if target not in registry["browser_suites"]:
            raise CommandError(
                "Browser suite is not registered in the canonical command inventory."
            )
        profile = registry["browser_profiles"].get(target)
        if profile is None:
            raise CommandError("Declared browser suite has no admitted execution profile yet.")
        if profile["kind"] != "simulated":
            raise CommandError(
                "Browser integration profile requires its owning authorized harness.",
                3,
                "MISSING_EXTERNAL_PREREQUISITE",
            )
        if not (root / "tools/browser-test-runner.ts").is_file():
            raise CommandError(
                "OPE-127 browser adapter is not available.", 2, "MISSING_APPLICATION"
            )
        path_target(root, "tools/browser-test-runner.ts")
        return [
            invoke(
                ("node", "tools/browser-test-runner.ts", target),
                root,
                target,
                "playwright",
                run,
                expected_kind=profile["kind"],
            )
        ]
    if command in {"test-integration", "test-isolation", "test-acceptance"}:
        selector, option = args
        key = {
            "test-integration": "integration_profiles",
            "test-isolation": "isolation_suites",
            "test-acceptance": "acceptance_gates",
        }[command]
        if selector not in registry[key]:
            raise CommandError("Requested profile, isolation suite or gate is not registered.")
        if command != "test-integration" and option not in MODES:
            raise CommandError("Unknown deployment mode.")
        if command == "test-integration":
            if not re.fullmatch(r"[A-Za-z0-9_ .-]+", option) or option.startswith("-"):
                raise CommandError("Invalid integration filter.")
        entry = registry[key][selector]
        if command == "test-integration" and selector == "local":
            if option != "harness-smoke":
                raise CommandError(
                    "Local profile accepts only the registered harness-smoke filter."
                )
            target = "tests/integration/test_failure_harness.py"
            if (
                entry["kind"] != "local_integration"
                or entry["path"] != target
                or entry["owner"] != "OPE-124"
            ):
                raise CommandError("Local profile does not match the admitted OPE-124 consumer.")
            path_target(root, "tools/fixtures/local_harness.py")
            path_target(root, target)
            return [
                invoke(
                    (*uv_python(root), "-m", "tools.fixtures.local_harness", "harness-smoke"),
                    root,
                    target,
                    "pytest",
                    run,
                    expected_kind="local_integration",
                    consumer="local_harness",
                )
            ]
        # Providers and customer gates have no admitted adapters at bootstrap. Credentials alone
        # would not prove environment authority or required evidence, so never infer readiness.
        if entry.get("kind") != "simulated" or not entry.get("path"):
            raise CommandError(
                "Registered suite requires its owning harness, authorized resources "
                "and actual evidence.",
                3,
                "MISSING_EXTERNAL_PREREQUISITE",
            )
        if command != "test-integration":
            raise CommandError("Simulated evidence cannot run a real isolation or acceptance gate.")
        if option != "all":
            raise CommandError("Bootstrap contract profile has only the explicit all filter.")
        return [python_test(root, path_target(root, entry["path"]), run)]
    if command == "validate-spec":
        path_target(root, "tools/validate_spec.py")
        target = path_target(root, TRACEABILITY_TARGET)
        return [
            invoke(
                (*uv_python(root), "-m", "tools.validate_spec"),
                root,
                target,
                "semantic_validator",
                run,
            )
        ]
    if command == "test-contract":
        try:
            shape = schema_validate.validate_examples(root)
        except schema_validate.SchemaError as exc:
            raise CommandError(str(exc)) from exc
        if shape["exit_code"]:
            return [{"runner": "schema", "exit_code": 1, "raw_exit_code": 1, "tests": shape}]
        return [
            {"runner": "schema", "exit_code": 0, "raw_exit_code": 0, "tests": shape},
            python_test(root, path_target(root, "tests/contract"), run),
        ]
    if command == "lint":
        commands = [(*uv_python(root)[:-1], "ruff", "check", ".")]
    else:
        commands = [
            (
                *uv_python(root)[:-1],
                "mypy",
                "tools",
                "services/control-api/src",
                "services/cell-runtime/src",
                "services/audit-journal/src",
                "training/src",
            ),
            (
                "pnpm",
                "exec",
                "tsc",
                "--noEmit",
                "--strict",
                "--skipLibCheck",
                "--module",
                "NodeNext",
                "--moduleResolution",
                "NodeNext",
                "--types",
                "node",
                "packages/client-ts/src/index.d.ts",
                "packages/contracts/examples/web-command-smoke.test.ts",
            ),
        ]
    commands.extend(browser_quality_commands(root, command))
    steps = []
    for argv in commands:
        step = invoke(argv, root, command, command, run)
        steps.append(step)
        if step["exit_code"]:
            break
    return steps


def source_identity(root: Path) -> tuple[str | None, bool]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=normal"],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        ).stdout
        return commit, bool(dirty)
    except (OSError, subprocess.SubprocessError):
        return None, True


def run_command(
    command: str, args: Sequence[str], root: Path = ROOT, run: Executor = execute
) -> dict[str, Any]:
    from tools import schema_validate

    started = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    root = root.resolve()
    commit_before, dirty_before = source_identity(root)
    code, reason, message = 0, None, None
    steps: list[dict[str, Any]] = []
    registry: dict[str, Any] = {}
    try:
        registry = manifest(root)
        steps = dispatch(command, list(args), root, registry, run)
        if not steps:
            raise CommandError("No requested checks executed.")
        code = next((step["exit_code"] for step in steps if step["exit_code"]), 0)
        reason = next((step.get("reason_code") for step in steps if step["exit_code"]), None)
    except (CommandError, schema_validate.SchemaError) as exc:
        code, reason = (
            (exc.code, exc.reason)
            if isinstance(exc, CommandError)
            else (2, "INVALID_CONFIGURATION")
        )
        message = str(exc)
    commit_after, dirty_after = source_identity(root)
    return {
        "schema_version": "opentour.command-evidence.v1",
        "issue_key": "OPE-123",
        "requirement_ids": ["OT-BOOT-C02-AC01"],
        "contract_requirement_ids": ["OT-BOOT-C02-AC01", "OT-BOOT-C02-AC02", "OT-BOOT-C02-AC03"],
        "command": [
            command if command in COMMAND_ARITY else "<invalid-command>",
            *(args if steps or code == 3 else ["<rejected-argument>"] * len(args)),
        ],
        "fixture_id": "bootstrap-command-contract",
        "fixture_version": registry.get("fixture_version", "1"),
        "kind": next(
            (
                step["tests"]["kind"]
                for step in reversed(steps)
                if isinstance(step.get("tests"), dict) and "kind" in step["tests"]
            ),
            "simulated",
        ),
        "mode": (
            args[-1]
            if command in {"test-isolation", "test-acceptance"} and args and args[-1] in MODES
            else None
        ),
        "commit_sha": commit_before,
        "commit_sha_after": commit_after,
        "git_head_changed": commit_before != commit_after,
        "git_worktree_dirty": dirty_before or dirty_after,
        "git_worktree_dirty_before": dirty_before,
        "git_worktree_dirty_after": dirty_after,
        "spec_sha256": registry.get("spec_sha256"),
        "source_revision": registry.get("native_issue_updated_at"),
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "status": "PASS" if code == 0 else "BLOCKED" if code == 3 else "FAIL",
        "exit_code": code,
        "reason_code": reason,
        "message": message,
        "steps": steps,
        "product_acceptance": "NOT_RUN",
        "limitations": [
            "Command/fixture evidence only; no provider or customer gate acceptance",
            "Dirty sources do not establish reviewed commit qualification",
            "Unit process/network guards are test discipline, not operating-system isolation",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    result = run_command(args[0] if args else "", args[1:])
    # Structured, bounded evidence only. OPE-126 owns durable aggregation and source traceability.
    result["output_digests"] = {
        "summary": hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest()
    }
    print(json.dumps(result, sort_keys=True))
    return int(result["exit_code"])


# Pytest transport plugin: every collected case appears in the final report, including skipped
# and unexecuted cases. This is not a provider/isolation harness and never opens network authority.
_items: dict[str, dict[str, Any]] = {}


def install_unit_guard() -> None:
    """Verify the inherited startup preload before pytest loads any configuration."""
    if not os.environ.get("OPENTOUR_RESULT_FILE"):
        return
    if getattr(subprocess, "_opentour_unit_guard_pid", None) != os.getpid():
        raise RuntimeError("Required inherited unit startup guard did not load")


def pytest_sessionstart(session: Any) -> None:
    _items.clear()
    install_unit_guard()


def pytest_collection_finish(session: Any) -> None:
    root = Path(os.environ.get("OPENTOUR_REPOSITORY_ROOT", str(ROOT))).resolve()
    for item in session.items:
        try:
            file = Path(item.path).resolve().relative_to(root).as_posix()
        except ValueError:
            file = "OUTSIDE_REPOSITORY"
        _items[item.nodeid] = {"id": item.nodeid, "file": file, "status": "skipped", "mode": None}


def pytest_runtest_logreport(report: Any) -> None:
    if report.nodeid not in _items:
        return
    item = _items[report.nodeid]
    if report.failed or getattr(report, "wasxfail", None):
        item["status"] = "failed"
    elif report.skipped and item["status"] != "failed":
        item["status"] = "skipped"
    elif report.when == "call" and report.passed and item["status"] != "failed":
        item["status"] = "passed"


def pytest_sessionfinish(session: Any, exitstatus: int) -> None:
    destination = os.environ.get("OPENTOUR_RESULT_FILE")
    if not destination:
        return
    result = report_envelope(
        "pytest", os.environ["OPENTOUR_TEST_TARGET"], dict(os.environ), list(_items.values())
    )
    if exitstatus not in (0, 1, 5):
        result.update(status="FAIL", reason_code="INVALID_CONFIGURATION")
    with Path(destination).open("x", encoding="utf-8") as stream:
        json.dump(result, stream, sort_keys=True)


# Explicit -p loading happens before initial conftest import. Do not wait for
# pytest_sessionstart: conftest/module imports are part of the protected unit run.
install_unit_guard()

if __name__ == "__main__":
    raise SystemExit(main())
