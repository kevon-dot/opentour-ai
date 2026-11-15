"""OPE-122: validate exact prerequisites and sync locked local dependencies only."""

from __future__ import annotations

import sys

# This launcher can be invoked before the project interpreter has been selected.
if sys.version_info < (3, 12):  # noqa: UP036
    import json

    print(
        json.dumps(
            {
                "issue_key": "OPE-122",
                "status": "BLOCKED",
                "exit_code": 3,
                "prerequisite": "python",
                "message": "Python 3.12 or newer is required to launch setup; "
                "install the pinned Python from tools/toolchain.json and put it on PATH.",
                "product_acceptance": "NOT_RUN",
            }
        ),
        file=sys.stderr,
    )
    raise SystemExit(3)

import argparse
import hashlib
import json
import os
import platform
import re
import subprocess
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ("uv.lock", "pnpm-lock.yaml", "training/uv.lock")
MANIFESTS = (
    "pyproject.toml",
    "package.json",
    "pnpm-workspace.yaml",
    ".python-version",
    ".node-version",
    "services/control-api/pyproject.toml",
    "services/cell-runtime/pyproject.toml",
    "services/audit-journal/pyproject.toml",
    "training/pyproject.toml",
    "apps/web/package.json",
    "packages/client-ts/package.json",
    "tools/toolchain.json",
)
CPU_IMPORTS = ("opentour_control", "opentour_cell", "opentour_audit")
LEARNING_IMPORTS = ("opentour_training.training", "opentour_training.evaluation")
Runner = Callable[[Sequence[str], Path], str]


class BootstrapError(Exception):
    def __init__(self, message: str, exit_code: int = 2, prerequisite: str | None = None):
        super().__init__(message)
        self.exit_code = exit_code
        self.prerequisite = prerequisite


def repository_file(root: Path, relative: str) -> Path:
    """Resolve only a declared repository file; refuse links escaping the checkout."""
    root = root.resolve()
    candidate = root / relative
    if Path(relative).is_absolute() or ".." in Path(relative).parts:
        raise BootstrapError(f"Invalid repository-relative path: {relative}")
    if not candidate.resolve().is_relative_to(root):
        raise BootstrapError(f"Repository path escapes the checkout: {relative}")
    if not candidate.is_file():
        raise BootstrapError(f"Required committed file is missing: {relative}")
    return candidate


def fingerprints(root: Path, paths: Sequence[str]) -> dict[str, str]:
    return {p: hashlib.sha256(repository_file(root, p).read_bytes()).hexdigest() for p in paths}


def safe_error(text: str) -> str:
    text = re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", text)
    text = re.sub(r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", r"\1 [redacted]", text)
    text = re.sub(
        r'''(?ix)(["']?(?:[\w-]*(?:token|password|secret)|api[_-]?key|access[_-]?key'''
        r'''|authorization)["']?\s*[=:]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s&,}]+)''',
        r"\1[redacted]",
        text,
    )
    text = re.sub(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[redacted]", text)
    text = re.sub(
        r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b", "[redacted]", text
    )
    return text[-1800:]


def execute(args: Sequence[str], cwd: Path) -> str:
    # No shell and no caller-controlled command concatenation.
    env = os.environ.copy()
    env.update(
        {
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "npm_config_registry": "https://registry.npmjs.org/",
            "NEXT_TELEMETRY_DISABLED": "1",
            "CI": "true",
        }
    )
    for key in (
        "UV_INDEX",
        "UV_INDEX_URL",
        "UV_EXTRA_INDEX_URL",
        "PIP_INDEX_URL",
        "PIP_EXTRA_INDEX_URL",
    ):
        env.pop(key, None)
    try:
        result = subprocess.run(
            list(args), cwd=cwd, env=env, capture_output=True, text=True, timeout=600, check=False
        )
    except FileNotFoundError as exc:
        raise BootstrapError(f"Required executable is unavailable: {args[0]}", 3, args[0]) from exc
    except subprocess.TimeoutExpired as exc:
        raise BootstrapError(
            f"Local command timed out after 600 seconds: {args[0]}", 3, args[0]
        ) from exc
    if result.returncode:
        detail = safe_error(result.stderr or result.stdout)
        raise BootstrapError(
            f"{args[0]} failed (exit {result.returncode}); "
            f"check the exact tool/package prerequisite. {detail}",
            3,
            args[0],
        )
    return result.stdout.strip()


def toolchain(root: Path) -> dict[str, Any]:
    try:
        value = json.loads(repository_file(root, "tools/toolchain.json").read_text())
    except (ValueError, UnicodeError) as exc:
        raise BootstrapError("tools/toolchain.json is not valid UTF-8 JSON") from exc
    if not isinstance(value, dict) or value.get("schema_version") != "opentour.toolchain.v1":
        raise BootstrapError("Unsupported toolchain manifest schema")
    if not isinstance(value.get("tools"), dict) or set(value["tools"]) != {
        "python",
        "node",
        "uv",
        "pnpm",
        "just",
    }:
        raise BootstrapError("Toolchain must name exactly python, node, uv, pnpm and just")
    for name, item in value["tools"].items():
        if not isinstance(item, dict) or not re.fullmatch(
            r"\d+\.\d+\.\d+", str(item.get("version", ""))
        ):
            raise BootstrapError(f"{name} must have an exact version pin")
    platforms = value.get("supported_platforms")
    if (
        not isinstance(platforms, list)
        or not platforms
        or not all(isinstance(item, str) for item in platforms)
    ):
        raise BootstrapError("Toolchain supported_platforms must be a nonempty string list")
    for name, item in value["tools"].items():
        artifacts = item.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != set(platforms):
            raise BootstrapError(f"{name} must pin one artifact for every supported platform")
        for target, artifact in artifacts.items():
            if (
                not isinstance(artifact, dict)
                or not str(artifact.get("url", "")).startswith("https://")
                or not re.fullmatch(r"[a-f0-9]{64}", str(artifact.get("sha256", "")))
            ):
                raise BootstrapError(f"Invalid pinned {name} artifact for {target}")
    images = value.get("bootstrap_images")
    if not isinstance(images, dict) or set(images) != {"python", "node", "uv"}:
        raise BootstrapError("Toolchain must pin Python, Node and uv bootstrap images")
    for name, image in images.items():
        if not re.fullmatch(r"[^\s@]+@sha256:[a-f0-9]{64}", str(image)):
            raise BootstrapError(f"{name} bootstrap image must have a SHA256 digest")
    source = value.get("spec_source")
    if not isinstance(source, dict) or not re.fullmatch(
        r"[a-f0-9]{64}", str(source.get("sha256", ""))
    ):
        raise BootstrapError("Toolchain spec_source must bind a SHA256 source digest")
    return cast(dict[str, Any], value)


def preflight(
    root: Path,
    run: Runner = execute,
    system: str | None = None,
    machine: str | None = None,
    libc: tuple[str, str] | None = None,
    macos_version: str | None = None,
    kernel_release: str | None = None,
) -> tuple[dict[str, Any], str, dict[str, str]]:
    manifest = toolchain(root)
    target = f"{system or platform.system()}-{machine or platform.machine()}"
    if target not in manifest["supported_platforms"]:
        raise BootstrapError(
            f"Unsupported platform {target}; "
            f"supported profiles: {', '.join(manifest['supported_platforms'])}",
            3,
            "supported-platform",
        )
    if target.startswith("Linux-"):
        library, version = libc if libc is not None else platform.libc_ver()
        parts = tuple(int(part) for part in version.split(".") if part.isdigit())
        if library != "glibc" or parts < (2, 28):
            raise BootstrapError(
                "Linux glibc 2.28+ is required; "
                f"found {library or 'unknown'} {version or 'unknown'}",
                3,
                "supported-platform",
            )
        kernel = kernel_release if kernel_release is not None else platform.release()
        match = re.match(r"^(\d+)\.(\d+)", kernel)
        kernel_parts = tuple(map(int, match.groups())) if match else ()
        if kernel_parts < (4, 18):
            raise BootstrapError(
                f"Linux kernel 4.18+ is required; found {kernel or 'unknown'}",
                3,
                "supported-platform",
            )
    if target.startswith("Darwin-"):
        version = macos_version if macos_version is not None else platform.mac_ver()[0]
        parts = tuple(int(part) for part in version.split(".") if part.isdigit())
        if parts < (13, 5):
            raise BootstrapError(
                f"macOS 13.5+ is required; found {version or 'unknown'}", 3, "supported-platform"
            )
    actual: dict[str, str] = {}
    for name in ("uv", "node", "pnpm", "just"):
        output = run([name, "--version"], root)
        match = re.search(r"(?<!\d)(\d+\.\d+\.\d+)(?!\d)", output)
        observed = match.group(1) if match else "unrecognized"
        expected = manifest["tools"][name]["version"]
        if observed != expected:
            raise BootstrapError(
                f"{name} {expected} is required; found {observed}. "
                "Install the exact pinned artifact from tools/toolchain.json.",
                3,
                name,
            )
        actual[name] = observed
    expected_python = manifest["tools"]["python"]["version"]
    python = run(["uv", "python", "find", expected_python, "--no-python-downloads"], root)
    if not python or "\n" in python or not Path(python).is_absolute():
        raise BootstrapError(
            "uv did not return one absolute pinned Python interpreter path", 3, "python"
        )
    version = run([python, "-c", "import platform; print(platform.python_version())"], root)
    if version != expected_python:
        raise BootstrapError(
            f"Python {expected_python} is required; found {version}. "
            f"Run uv python install {expected_python}.",
            3,
            "python",
        )
    actual["python"] = version
    for filename, expected in (
        (".python-version", expected_python),
        (".node-version", actual["node"]),
    ):
        if repository_file(root, filename).read_text().strip() != expected:
            raise BootstrapError(f"{filename} disagrees with tools/toolchain.json")
    return manifest, python, actual


def setup(root: Path = ROOT, run: Runner = execute, offline: bool = False) -> dict[str, Any]:
    root = root.resolve()
    started = time.time()
    started_at = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    manifest, python, versions = preflight(root, run)
    commit = run(["git", "rev-parse", "HEAD"], root)
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise BootstrapError("The checkout must have a recorded 40-character base commit")
    dirty_before = bool(run(["git", "status", "--porcelain", "--untracked-files=normal"], root))
    before = fingerprints(root, (*LOCKS, *MANIFESTS))
    flag = ["--offline"] if offline else []
    # `--frozen` alone does not detect manifest drift. Check before every frozen sync.
    run(["uv", "lock", "--check", "--python", python, "--no-python-downloads", *flag], root)
    run(
        [
            "uv",
            "sync",
            "--frozen",
            "--all-packages",
            "--python",
            python,
            "--no-python-downloads",
            *flag,
        ],
        root,
    )
    run(
        ["uv", "lock", "--check", "--python", python, "--no-python-downloads", *flag],
        root / "training",
    )
    run(
        ["uv", "sync", "--frozen", "--python", python, "--no-python-downloads", *flag],
        root / "training",
    )
    run(["pnpm", "install", "--frozen-lockfile", "--ignore-scripts", *flag], root)
    for base, modules in ((root, CPU_IMPORTS), (root / "training", LEARNING_IMPORTS)):
        script = "import importlib; " + "; ".join(
            f"importlib.import_module({module!r})" for module in modules
        )
        run(
            [
                "uv",
                "run",
                "--no-sync",
                "--python",
                python,
                "--no-python-downloads",
                "python",
                "-c",
                script,
            ],
            base,
        )
    run(["pnpm", "--filter", "@opentour/web", "run", "bootstrap:smoke"], root)
    after = fingerprints(root, (*LOCKS, *MANIFESTS))
    if before != after:
        changed = [p for p in before if before[p] != after[p]]
        raise BootstrapError(f"Frozen setup changed tracked inputs: {', '.join(changed)}", 1)
    dirty_after = bool(run(["git", "status", "--porcelain", "--untracked-files=normal"], root))
    return {
        "schema_version": "opentour.bootstrap-receipt.v1",
        "issue_key": "OPE-122",
        "kind": "local_integration" if run is execute else "simulated",
        "fixture_id": "bootstrap-package-boundaries",
        "fixture_version": "1",
        "status": "PASS",
        "exit_code": 0,
        "product_acceptance": "NOT_RUN",
        "commit_sha": commit,
        "git_worktree_dirty": dirty_before or dirty_after,
        "spec_sha256": manifest["spec_source"]["sha256"],
        "platform": f"{platform.system()}-{platform.machine()}",
        "versions": versions,
        "lock_and_manifest_sha256": after,
        "toolchain_sha256": before["tools/toolchain.json"],
        "offline": offline,
        "started_at": started_at,
        "finished_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "deployment_modes_applicable": ["customer_controlled", "opentour_managed"],
        "elapsed_seconds": round(time.time() - started, 3),
        "limitations": [
            "Dependency/import smoke only",
            "No cloud provisioning or credentials",
            "No GPU, protected evaluation, serving or customer-value qualification",
        ],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Require cached packages; never fetch missing dependencies",
    )
    args = parser.parse_args(argv)
    try:
        result = setup(offline=args.offline)
        print(json.dumps(result, sort_keys=True))
        return 0
    except BootstrapError as exc:
        print(
            json.dumps(
                {
                    "issue_key": "OPE-122",
                    "status": "BLOCKED" if exc.exit_code == 3 else "FAIL",
                    "exit_code": exc.exit_code,
                    "prerequisite": exc.prerequisite,
                    "message": str(exc),
                    "product_acceptance": "NOT_RUN",
                },
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return exc.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
