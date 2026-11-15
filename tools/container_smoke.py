"""Build and run the six CPU bootstrap package targets; never deploy or push images."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.bootstrap import BootstrapError, safe_error, toolchain  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    "control-api": ("python", "opentour-control", "opentour_control"),
    "cell-runtime": ("python", "opentour-cell", "opentour_cell"),
    "audit-journal": ("python", "opentour-audit", "opentour_audit"),
    "trainer": ("learning", "trainer", "opentour_training.training"),
    "protected-evaluator": ("learning", "protected_evaluator", "opentour_training.evaluation"),
    "web": ("web", "", ""),
}


class SmokeError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def command(args: list[str], prerequisite: bool = False) -> str:
    result = subprocess.run(
        args, cwd=ROOT, text=True, capture_output=True, timeout=900, check=False
    )
    if result.returncode:
        raise SmokeError(
            f"{args[0]} exited {result.returncode}: {safe_error(result.stderr or result.stdout)}",
            3 if prerequisite else 1,
        )
    return result.stdout.strip()


def build_arguments(target: str, platform: str, manifest: dict[str, Any]) -> list[str]:
    kind, package, module = TARGETS[target]
    images = manifest["bootstrap_images"]
    for required in ("python", "node", "uv"):
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", str(images[required])):
            raise ValueError(f"{required} bootstrap image is not pinned to a digest")
    options = {
        "PYTHON_IMAGE": images["python"],
        "UV_IMAGE": images["uv"],
        "NODE_IMAGE": images["node"],
        "PACKAGE": package,
        "MODULE": module,
        "ROLE": package,
    }
    key = "Linux-aarch64" if platform == "linux/arm64" else "Linux-x86_64"
    artifact = manifest["tools"]["pnpm"]["artifacts"][key]
    options.update({"PNPM_URL": artifact["url"], "PNPM_SHA256": artifact["sha256"]})
    args = [
        "docker",
        "build",
        "--platform",
        platform,
        "--file",
        f"containers/{kind}.bootstrap.Dockerfile",
        "--tag",
        f"opentour-bootstrap/{target}:local",
    ]
    for key, value in options.items():
        args.extend(["--build-arg", f"{key}={value}"])
    return args + ["."]


def validate_result(target: str, result: Any, manifest: dict[str, Any]) -> None:
    if not isinstance(result, dict):
        raise SmokeError(f"Smoke result for {target} is not a JSON object")
    expected = {"status": "PASS", "kind": "local_integration", "product_acceptance": "NOT_RUN"}
    kind, package, module = TARGETS[target]
    if kind == "web":
        expected.update(
            {"check": "web-package-import", "node": manifest["tools"]["node"]["version"]}
        )
    else:
        expected["python"] = manifest["tools"]["python"]["version"]
        if kind == "learning":
            expected.update(
                {
                    "check": "learning-container-package-import",
                    "role": package,
                    "gpu_qualification": "NOT_RUN",
                }
            )
        else:
            expected.update({"check": "container-package-import", "module": module})
    for key, value in expected.items():
        if result.get(key) != value:
            raise SmokeError(
                f"Invalid {key} in bootstrap smoke result for {target}: expected {value}"
            )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=["linux/arm64", "linux/amd64"], required=True)
    parser.add_argument("--target", choices=list(TARGETS), action="append")
    options = parser.parse_args()
    started = time.time()
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    results = []
    try:
        manifest_bytes = (ROOT / "tools/toolchain.json").read_bytes()
        manifest = toolchain(ROOT)
        daemon = command(
            ["docker", "info", "--format", "{{.OSType}}/{{.Architecture}}"], prerequisite=True
        )
        commit = command(["git", "rev-parse", "HEAD"])
        dirty_before = bool(command(["git", "status", "--porcelain", "--untracked-files=normal"]))
        for target in options.target or TARGETS:
            print(
                f"Building bootstrap target {target} for {options.platform}",
                file=sys.stderr,
                flush=True,
            )
            command(build_arguments(target, options.platform, manifest))
            tag = f"opentour-bootstrap/{target}:local"
            image_id = command(["docker", "image", "inspect", "--format", "{{.Id}}", tag])
            output = command(
                [
                    "docker",
                    "run",
                    "--rm",
                    "--platform",
                    options.platform,
                    "--network",
                    "none",
                    "--read-only",
                    "--cap-drop",
                    "ALL",
                    "--security-opt",
                    "no-new-privileges",
                    "--tmpfs",
                    "/tmp:rw,noexec,nosuid,size=16m",
                    tag,
                ]
            )
            result = json.loads(output)
            validate_result(target, result, manifest)
            results.append({"target": target, "image_id": image_id, "result": result})
        dirty_after = bool(command(["git", "status", "--porcelain", "--untracked-files=normal"]))
        print(
            json.dumps(
                {
                    "issue_key": "OPE-122",
                    "kind": "local_integration",
                    "check": "container-build-and-import",
                    "status": "PASS",
                    "started_at": started_at,
                    "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
                    "fixture_id": "bootstrap-container-boundaries",
                    "fixture_version": "1",
                    "commit_sha": commit,
                    "git_worktree_dirty": dirty_before or dirty_after,
                    "spec_source_sha256": manifest["spec_source"]["sha256"],
                    "platform": options.platform,
                    "daemon_platform": daemon,
                    "native_execution": daemon
                    in (
                        options.platform,
                        options.platform.replace("arm64", "aarch64"),
                        options.platform.replace("amd64", "x86_64"),
                    ),
                    "toolchain_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
                    "targets": results,
                    "elapsed_seconds": round(time.time() - started, 3),
                    "product_acceptance": "NOT_RUN",
                    "gpu_qualification": "NOT_RUN",
                },
                sort_keys=True,
            )
        )
        return 0
    except (
        OSError, ValueError, KeyError, BootstrapError, SmokeError, subprocess.TimeoutExpired
    ) as exc:
        code = (
            exc.exit_code
            if isinstance(exc, (SmokeError, BootstrapError))
            else 3
            if isinstance(exc, (OSError, subprocess.TimeoutExpired))
            else 2
        )
        print(
            json.dumps(
                {
                    "issue_key": "OPE-122",
                    "status": "BLOCKED" if code == 3 else "FAIL",
                    "message": safe_error(str(exc)),
                    "completed_targets": results,
                    "product_acceptance": "NOT_RUN",
                }
            ),
            file=sys.stderr,
        )
        return code


if __name__ == "__main__":
    raise SystemExit(main())
