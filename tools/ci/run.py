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
from tools.ci.policy import CHECKS, check
from tools.ci.prepare import download, postgres
from tools.public_content_check import digest, inspect_bytes, load_policy, scan_paths, tracked_paths
from tools.test_runner import CommandError, Invocation, execute

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
            )
        )
    raw = result.stdout.encode()
    findings = inspect_bytes("command-output.txt", raw, load_policy(root))
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
        else "FAIL",
    }
    if not findings:
        parsed = None
        try:
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
            for target in (
                "services/control-api/tests",
                "services/cell-runtime/tests",
                "services/audit-journal/tests",
                "training/tests",
            ):
                run(["just", "test-python", target])
        elif name == "web-unit":
            run(["just", "test-web", "packages/contracts/examples/web-command-smoke.test.ts"])
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
        elif name == "spec-traceability":
            run(["just", "validate-spec"])
        elif name == "secret-scan":
            run(["just", "test-python", "tests/contract/test_public_content_policy.py"])
        elif name == "dependency-scan":
            item = json.loads((root / ".github/ci-policy.json").read_text())["osv_scanner"]
            binary = root / ".toolchain/ci/osv-scanner"
            download(item["url"], item["sha256"], binary)
            binary.chmod(0o700)
            run(
                [
                    str(binary),
                    "scan",
                    "source",
                    "--format=json",
                    "--all-packages",
                    "--verbosity=error",
                    "--lockfile=uv.lock",
                    "--lockfile=training/uv.lock",
                    "--lockfile=pnpm-lock.yaml",
                ]
            )
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
        "steps": steps,
        "limits": [
            "Bootstrap CI only; provider, GPU and customer gates are not qualified",
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
