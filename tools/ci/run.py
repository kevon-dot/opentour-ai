"""Run actual finite checks, retain provenance, and publish only screened reports."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools.ci.browser_evidence import retain_browser_evidence
from tools.ci.dependency_evidence import (
    SCANNER_VERSION,
    DependencyEvidenceError,
    project_osv_report,
)
from tools.ci.policy import CHECKS, check
from tools.ci.prepare import download, postgres
from tools.fixtures.python_postgres import present_targets, target_command
from tools.public_content_check import digest, inspect_bytes, load_policy, scan_paths, tracked_paths
from tools.test_runner import CommandError, Invocation, execute, path_target

ROOT = Path(__file__).resolve().parents[2]


def safe_environment() -> dict[str, str]:
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "SYSTEMROOT") if k in os.environ}
    env.update(
        CI="true",
        TZ="UTC",
        LANG="C.UTF-8",
        NEXT_TELEMETRY_DISABLED="1",
        PYTHONPATH=str(ROOT),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
    )
    return env


def dependency_scan_command(root: Path) -> list[str]:
    return [
        str(root / ".toolchain/ci/osv-scanner"),
        "scan",
        "source",
        "--format=json",
        "--all-packages",
        "--verbosity=error",
        "--lockfile=uv.lock",
        "--lockfile=training/uv.lock",
        "--lockfile=pnpm-lock.yaml",
    ]


def run_one(argv: list[str], root: Path, *, env: dict[str, str] | None = None) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="opentour-ci-command-") as temporary:
        result = execute(
            Invocation(
                tuple(argv),
                root,
                env or safe_environment(),
                Path(temporary) / "report.json",
                "ci-check",
                "setup",
                660,
                output_limit_bytes=2_097_152 if argv == ["just", "validate-spec"] else 1_048_576,
            )
        )
    raw = result.stdout.encode()
    projected_dependency: dict[str, Any] | None = None
    projection_failed = False
    published = raw
    if argv == dependency_scan_command(root):
        try:
            projected_dependency = project_osv_report(raw, root=root)
            published = json.dumps(projected_dependency, sort_keys=True).encode()
        except DependencyEvidenceError:
            # The raw scanner output can contain third-party advisory contacts.
            # Invalid reports never publish raw text or become an empty success.
            published = b""
            projection_failed = True
    findings = inspect_bytes("command-output.txt", published, load_policy(root))
    report: dict[str, Any] = {
        "command": argv,
        "exit_code": result.returncode,
        "output_sha256": digest(raw),
        "process_cleanup_complete": result.cleanup_complete,
        "timed_out": result.timed_out,
        "interrupted": result.interrupted,
        "disclosure_findings": findings,
        "status": "PASS"
        if result.returncode == 0
        and result.cleanup_complete
        and not result.timed_out
        and not result.interrupted
        and not findings
        and not projection_failed
        else "FAIL",
    }
    if argv == dependency_scan_command(root):
        report["raw_scanner_output_published"] = False
        report["published_projection_sha256"] = digest(published)
        if projection_failed:
            report["reason"] = "INVALID_DEPENDENCY_REPORT"
        elif not findings and projected_dependency is not None:
            report["command_evidence"] = projected_dependency
            if projected_dependency.get("status") != "PASS":
                report["status"] = "FAIL"
        return report
    if not findings:
        parsed = None
        try:
            if result.returncode != 0 and argv[0] == "just":
                # Just appends its own failure diagnostic to the merged stream.
                # Recover the leading structured failure only; it can never pass.
                text = result.stdout.lstrip()
                parsed, end = json.JSONDecoder().raw_decode(text)
                report["failure_diagnostic_sha256"] = digest(text[end:].encode())
            else:
                parsed = json.loads(result.stdout)
            if isinstance(parsed, dict):
                report["command_evidence"] = parsed
                if parsed.get("status") not in (None, "PASS"):
                    report["status"] = "FAIL"
        except ValueError:
            # Non-JSON compiler/install output is represented by its digest, never echoed.
            pass
        if argv[0] == "just" and (
            not isinstance(parsed, dict)
            or parsed.get("status") != "PASS"
            or parsed.get("exit_code") != 0
        ):
            report["status"] = "FAIL"
    return report


def stage_report(root: Path, name: str, report: dict[str, Any]) -> Path:
    """Screen the exact bytes written; never hand arbitrary input globs to an uploader."""
    data = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    policy = load_policy(root)
    if inspect_bytes(name + ".json", data, policy):
        raise ValueError("Evidence disclosure check failed")
    destination = root / "evidence/ci/publish"
    # Refuse symlink roots/parents, including an attacker-controlled preexisting evidence tree.
    for directory in (root / "evidence", root / "evidence/ci", destination):
        if directory.is_symlink():
            raise ValueError("Unsafe publication directory")
        directory.mkdir(exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Publication directory must be fresh")
    file = destination / (name + ".json")
    with file.open("xb") as stream:
        stream.write(data)
    return file


def run_browser(root: Path, env: dict[str, str]) -> dict[str, Any]:
    result = run_one(
        ["just", "test-browser", "apps/web/tests/browser-harness.browser.spec.ts"], root, env=env
    )
    # A failed actual test may still have valuable, nonce-bound failure evidence.
    # Retain it before propagating failure; an absent/invalid manifest cannot pass.
    try:
        result["browser_artifacts"] = retain_browser_evidence(
            root, result.get("command_evidence", {})
        )
    except (KeyError, TypeError, ValueError, OSError):
        result["browser_artifacts"] = {
            "available": False,
            "reason": "MISSING_OR_INVALID_BROWSER_EVIDENCE",
            "raw_artifacts_uploaded": False,
        }
        result["status"] = "FAIL"
    return result


def python_unit_selection(root: Path) -> tuple[list[str], list[str]]:
    local_targets = set(present_targets(root))
    targets = [
        "tests/contract/test_bootstrap.py",
        "tests/contract/test_harness_modes.py",
        "tests/contract/test_local_harness_transport.py",
        "tools/tests",
    ]
    absent = []
    for target in (
        "services/control-api/tests",
        "services/cell-runtime/tests",
        "services/audit-journal/tests",
        "training/tests",
        "infrastructure/tests",
    ):
        if (root / target).exists() or (root / target).is_symlink():
            # Validate the whole tree before partitioning: a symlink must fail, not
            # disappear from a glob or move into a different evidence class.
            path_target(root, target)
            routed_local = {p for p in local_targets if p.startswith(target + "/")}
            if not routed_local:
                # Keep empty present directories: the command must refuse zero tests.
                targets.append(target)
            else:
                # Pytest's pinned default discovery names. Each remaining file keeps
                # the unit guard; only the finite approved paths go to PostgreSQL CI.
                targets.extend(
                    sorted(
                        str(p.relative_to(root))
                        for p in (root / target).rglob("*.py")
                        if (p.name.startswith("test_") or p.name.endswith("_test.py"))
                        and str(p.relative_to(root)) not in routed_local
                    )
                )
        else:
            absent.append(target)
    return targets, absent


def web_unit_selection(root: Path) -> list[str]:
    # Tracked source inventory excludes installed node_modules/build output while
    # including colocated tests outside apps/web/tests. Browser specs use their own job.
    targets = sorted(p for p in tracked_paths(root) if p.endswith((".test.ts", ".test.tsx")))
    if not targets:
        raise CommandError("No tracked web unit tests exist.", 1)
    for target in targets:
        path_target(root, target)
        if not (root / target).is_file():
            raise CommandError("Web unit target must be a regular file.", 1)
    return targets


def run_check(name: str, root: Path) -> dict[str, Any]:
    if name not in CHECKS:
        raise ValueError("Unknown required check")
    started = datetime.now(UTC).isoformat()
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    source = scan_paths(root, tracked_paths(root), load_policy(root))
    if source["status"] != "PASS":
        return {
            "status": "FAIL",
            "reason": "PUBLIC_SOURCE_REJECTED",
            "check": name,
            "commit_sha": commit,
            "content_result": source,
        }
    steps: list[dict[str, Any]] = []
    not_implemented_test_roots: list[str] = []
    routed_local_targets: list[str] = []
    env = safe_environment()

    def run(argv: list[str]) -> None:
        result = run_one(argv, root, env=env)
        steps.append(result)
        if result["status"] != "PASS":
            raise CommandError("CI command failed", 1)

    try:
        # Frozen CPU/TypeScript setup is a real check prerequisite, never a fake success.
        run(["just", "setup"])
        run(["uv", "sync", "--project", "training", "--frozen", "--python", "3.12.16"])
        if name in {"lint", "typecheck"}:
            run(["just", name])
        elif name == "python-unit":
            routed_local_targets = list(present_targets(root))
            targets, not_implemented_test_roots = python_unit_selection(root)
            for target in targets:
                run(["just", "test-python", target])
        elif name == "web-unit":
            for target in web_unit_selection(root):
                run(["just", "test-web", target])
        elif name == "browser-fixtures":
            env["PLAYWRIGHT_BROWSERS_PATH"] = str(root / ".toolchain/playwright")
            run(["pnpm", "exec", "playwright", "install", "--only-shell", "chromium"])
            browser = run_browser(root, env)
            steps.append(browser)
            if browser["status"] != "PASS":
                raise CommandError("Browser check failed", 1)
        elif name == "contract":
            run(["just", "test-contract"])
        elif name == "local-integration":
            env["PATH"] = postgres(root) + os.pathsep + env.get("PATH", "")
            run(["just", "test-integration", "local", "harness-smoke"])
            routed_local_targets = list(present_targets(root))
            for target in routed_local_targets:
                run(list(target_command(target)))
                if steps[-1].get("command_evidence", {}).get("kind") != "local_integration":
                    steps[-1]["status"] = "FAIL"
                    raise CommandError("PostgreSQL target lacked local integration evidence.", 1)
        elif name == "spec-traceability":
            run(["just", "validate-spec"])
        elif name == "secret-scan":
            run(["just", "test-python", "tests/contract/test_public_content_policy.py"])
        elif name == "dependency-scan":
            item = json.loads((root / ".github/ci-policy.json").read_text())["osv_scanner"]
            if item["version"] != SCANNER_VERSION:
                raise CommandError("Dependency evidence adapter does not match the scanner.", 1)
            binary = root / ".toolchain/ci/osv-scanner"
            download(item["url"], item["sha256"], binary)
            binary.chmod(0o700)
            run(dependency_scan_command(root))
        elif name == "iac-policy":
            policy_result = check(root)
            steps.append(policy_result)
            if policy_result["status"] != "PASS":
                raise CommandError("Workflow policy failed", 1)
            run(["just", "test-python", "tools/tests/test_ci_policy.py"])
            run(["just", "test-python", "infrastructure/tests/test_ci_oidc_trust.py"])
    except (CommandError, ValueError, OSError, subprocess.SubprocessError):
        status = "FAIL"
    else:
        status = "PASS" if steps else "FAIL"
    after = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=root)
    if after != commit or dirty:
        status = "FAIL"
    return {
        "schema_version": "opentour.ci-evidence.v1",
        "issue_key": "OPE-125",
        "check": name,
        "status": status,
        "commit_sha": commit,
        "commit_sha_after": after,
        "source_clean_after": not bool(dirty),
        "covered_source_sha256": source["covered_source_sha256"],
        "started_at": started,
        "finished_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "runner_image": os.environ.get("ImageVersion"),
        "fixture_kind": "local_integration" if name == "local-integration" else "simulated",
        "product_acceptance": "NOT_RUN",
        "not_implemented_product_test_roots": not_implemented_test_roots,
        "registered_local_integration_targets": routed_local_targets,
        "steps": steps,
        "limits": [
            "Repository CI only; provider, GPU and customer gates are not qualified",
            "Artifact upload contains screened structured reports, not arbitrary raw logs",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("check", choices=CHECKS)
    args = parser.parse_args()
    try:
        report = run_check(args.check, ROOT)
        stage_report(ROOT, args.check, report)
        output = os.environ.get("GITHUB_OUTPUT")
        if output:
            with Path(output).open("a") as stream:
                stream.write("publish_ready=true\n")
        print(
            json.dumps(
                {
                    "check": args.check,
                    "status": report["status"],
                    "commit_sha": report.get("commit_sha"),
                }
            )
        )
        return 0 if report["status"] == "PASS" else 1
    except (OSError, ValueError, CommandError, subprocess.SubprocessError):
        print(json.dumps({"check": args.check, "status": "FAIL", "reason": "CI_CHECK_UNAVAILABLE"}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
