"""OPE-122 acceptance and adverse fixtures; injected runner evidence stays simulated."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib

import pytest

from tools import bootstrap


class RecordedRunner:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.versions = {
            "uv": "uv 0.13.8",
            "node": "v24.22.3",
            "pnpm": "12.18.0",
            "just": "just 1.59.0",
        }

    def __call__(self, args, cwd):
        self.calls.append((tuple(args), cwd))
        if self.failure and self.failure(args, cwd):
            raise bootstrap.BootstrapError("Required package unavailable in fixture", 3, args[0])
        if args == ["git", "rev-parse", "HEAD"]:
            return "a" * 40
        if len(args) == 2 and args[1] == "--version":
            return self.versions[args[0]]
        if list(args[:3]) == ["uv", "python", "find"]:
            return "/qualified/python3.12"
        if args[0] == "/qualified/python3.12":
            return "3.12.16"
        return ""


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    for relative in (*bootstrap.LOCKS, *bootstrap.MANIFESTS):
        source = bootstrap.ROOT / relative
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    (root / "README.md").write_text("Existing user README\n")
    (root / "LICENSE").write_text("Existing license terms\n")
    (root / "unrelated.txt").write_text("Do not modify\n")
    return root


def test_ac01_repeatable_setup_preserves_all_inputs_and_imports(checkout):
    runner = RecordedRunner()
    before = bootstrap.fingerprints(
        checkout, (*bootstrap.LOCKS, *bootstrap.MANIFESTS, "README.md", "LICENSE", "unrelated.txt")
    )
    first = bootstrap.setup(checkout, runner)
    second = bootstrap.setup(checkout, runner)
    assert first["status"] == second["status"] == "PASS"
    assert first["kind"] == second["kind"] == "simulated"
    assert first["product_acceptance"] == "NOT_RUN"
    assert before == bootstrap.fingerprints(checkout, tuple(before))
    scripts = [args[-1] for args, _ in runner.calls if args[0] == "uv" and "-c" in args]
    assert all(
        any(module in script for script in scripts)
        for module in (*bootstrap.CPU_IMPORTS, *bootstrap.LEARNING_IMPORTS)
    )
    assert first["lock_and_manifest_sha256"] == second["lock_and_manifest_sha256"]


def test_ac02_setup_preserves_readme_license_and_unrelated_file(checkout):
    files = {
        name: (checkout / name).read_bytes() for name in ("README.md", "LICENSE", "unrelated.txt")
    }
    bootstrap.setup(checkout, RecordedRunner())
    assert files == {name: (checkout / name).read_bytes() for name in files}


def test_ac03_unsupported_platform_refused_before_commands(checkout):
    runner = RecordedRunner()
    with pytest.raises(bootstrap.BootstrapError, match="Unsupported platform") as error:
        bootstrap.preflight(checkout, runner, "Windows", "AMD64")
    assert error.value.exit_code == 3
    assert runner.calls == []


def test_unqualified_macos_intel_profile_is_refused(checkout):
    runner = RecordedRunner()
    with pytest.raises(bootstrap.BootstrapError, match="Unsupported platform Darwin-x86_64"):
        bootstrap.preflight(checkout, runner, "Darwin", "x86_64", macos_version="15.0")
    assert runner.calls == []


@pytest.mark.parametrize("libc", [("musl", "1.2.5"), ("glibc", "2.27"), ("", "")])
def test_unsupported_linux_library_is_refused(checkout, libc):
    runner = RecordedRunner()
    with pytest.raises(bootstrap.BootstrapError, match="glibc 2.28"):
        bootstrap.preflight(checkout, runner, "Linux", "x86_64", libc=libc)
    assert runner.calls == []


@pytest.mark.parametrize("version", ["11.7.10", "12.7", "13.0", "13.4.9", ""])
def test_unsupported_macos_version_is_refused(checkout, version):
    runner = RecordedRunner()
    with pytest.raises(bootstrap.BootstrapError, match="macOS 13.5"):
        bootstrap.preflight(checkout, runner, "Darwin", "arm64", macos_version=version)
    assert runner.calls == []


@pytest.mark.parametrize("version", ["13.5", "13.5.1", "14.0"])
def test_supported_macos_version_boundary(checkout, version):
    bootstrap.preflight(checkout, RecordedRunner(), "Darwin", "arm64", macos_version=version)


@pytest.mark.parametrize("kernel", ["3.10.0", "4.17.99", "unknown", ""])
def test_unsupported_linux_kernel_is_refused(checkout, kernel):
    runner = RecordedRunner()
    with pytest.raises(bootstrap.BootstrapError, match="Linux kernel 4.18"):
        bootstrap.preflight(
            checkout, runner, "Linux", "x86_64", libc=("glibc", "2.28"), kernel_release=kernel
        )
    assert runner.calls == []


@pytest.mark.parametrize("kernel", ["4.18.0", "5.15.0-1092-azure", "6.12.0-linuxkit"])
def test_supported_linux_kernel_boundary(checkout, kernel):
    bootstrap.preflight(
        checkout, RecordedRunner(), "Linux", "x86_64",
        libc=("glibc", "2.28"), kernel_release=kernel,
    )


@pytest.mark.parametrize("tool", ["uv", "node", "pnpm", "just"])
def test_ac03_wrong_exact_tool_version_refused(checkout, tool):
    runner = RecordedRunner()
    runner.versions[tool] = "0.0.1"
    with pytest.raises(bootstrap.BootstrapError, match=tool) as error:
        bootstrap.setup(checkout, runner)
    assert error.value.exit_code == 3
    assert not any("sync" in args or "install" in args for args, _ in runner.calls)


def test_ac03_unavailable_package_is_nonzero_and_no_followup(checkout):
    runner = RecordedRunner()
    runner.failure = lambda args, cwd: args[:2] == ["uv", "sync"]
    with pytest.raises(bootstrap.BootstrapError, match="package unavailable") as error:
        bootstrap.setup(checkout, runner)
    assert error.value.exit_code == 3
    assert not any(args[0] == "pnpm" and "install" in args for args, _ in runner.calls)


def test_wrong_python_patch_cannot_silently_fallback(checkout):
    original = RecordedRunner()

    def wrong_python(args, cwd):
        if args[0] == "/qualified/python3.12":
            return "3.12.4"
        return original(args, cwd)

    with pytest.raises(bootstrap.BootstrapError, match="Python 3.12.16 is required"):
        bootstrap.setup(checkout, wrong_python)
    assert not any("sync" in args or "install" in args for args, _ in original.calls)


@pytest.mark.parametrize("content", ["[]", "{}", "not-json", '{"schema_version":"wrong"}'])
def test_corrupt_manifest_is_an_actionable_config_failure(checkout, content):
    (checkout / "tools/toolchain.json").write_text(content)
    with pytest.raises(bootstrap.BootstrapError) as error:
        bootstrap.setup(checkout, RecordedRunner())
    assert error.value.exit_code == 2


@pytest.mark.parametrize("field", ["artifacts", "sha256", "url", "image"])
def test_missing_or_mutable_artifact_pins_are_rejected(checkout, field):
    path = checkout / "tools/toolchain.json"
    manifest = json.loads(path.read_text())
    if field == "artifacts":
        manifest["tools"]["python"]["artifacts"] = {}
    elif field == "image":
        manifest["bootstrap_images"]["node"] = "node:latest"
    else:
        manifest["tools"]["python"]["artifacts"]["Darwin-arm64"][field] = "unverified"
    path.write_text(json.dumps(manifest))
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.toolchain(checkout)


def test_manifest_drift_check_precedes_frozen_sync(checkout):
    runner = RecordedRunner()
    runner.failure = lambda args, cwd: args[:3] == ["uv", "lock", "--check"]
    with pytest.raises(bootstrap.BootstrapError):
        bootstrap.setup(checkout, runner)
    assert not any("sync" in args or "install" in args for args, _ in runner.calls)


def test_cpu_lock_excludes_gpu_libraries():
    root = tomllib.loads((bootstrap.ROOT / "uv.lock").read_text())
    names = {package["name"] for package in root["package"]}
    assert {"opentour-control", "opentour-cell", "opentour-audit"} <= names
    assert not any(name.startswith(("torch", "nvidia-", "cuda", "triton")) for name in names)
    assert "opentour-training" not in names
    learning = tomllib.loads((bootstrap.ROOT / "training/uv.lock").read_text())
    assert "opentour-training" in {package["name"] for package in learning["package"]}


def test_trainer_evaluator_profiles_have_distinct_roles_and_hosts():
    profiles = json.loads((bootstrap.ROOT / "training/execution-profiles.json").read_text())
    trainer = profiles["profiles"]["trainer"]
    evaluator = profiles["profiles"]["protected_evaluator"]
    assert trainer["principal_class"] != evaluator["principal_class"]
    assert trainer["host_class"] != evaluator["host_class"]
    assert trainer["protected_answers_allowed"] is False
    assert evaluator["training_dispatch_allowed"] is False
    assert profiles["qualification_status"] == "NOT_RUN"


def test_no_cloud_commands_or_shell_interpolation(checkout):
    runner = RecordedRunner()
    bootstrap.setup(checkout, runner, offline=True)
    assert {args[0] for args, _ in runner.calls} <= {
        "uv",
        "node",
        "pnpm",
        "just",
        "git",
        "/qualified/python3.12",
    }
    for args, _ in runner.calls:
        if "sync" in args or "lock" in args or "install" in args:
            assert "--offline" in args
    assert all(isinstance(args, tuple) for args, _ in runner.calls)


@pytest.mark.parametrize("path", ["../private.txt", "/etc/passwd", "tools/../../outside"])
def test_repository_path_escape_refused(checkout, path):
    with pytest.raises(bootstrap.BootstrapError, match="relative path"):
        bootstrap.repository_file(checkout, path)


def test_external_symlink_refused_before_read(checkout, tmp_path):
    outside = tmp_path / "private.json"
    outside.write_text('{"private":true}')
    target = checkout / "tools/toolchain.json"
    target.unlink()
    target.symlink_to(outside)
    with pytest.raises(bootstrap.BootstrapError, match="escapes"):
        bootstrap.toolchain(checkout)


def test_frozen_lock_mutation_detected(checkout):
    original = RecordedRunner()

    def mutate(args, cwd):
        output = original(args, cwd)
        if args[:3] == ["pnpm", "--filter", "@opentour/web"]:
            (checkout / "uv.lock").write_text("tampered")
        return output

    with pytest.raises(bootstrap.BootstrapError, match="changed tracked inputs") as error:
        bootstrap.setup(checkout, mutate)
    assert error.value.exit_code == 1


def test_dependency_error_redacts_credentials():
    message = bootstrap.safe_error(
        "https://user:password@example.test token=abcdef api_key=123 secret: hidden"
    )
    assert "password@" not in message
    assert "abcdef" not in message and "123" not in message and "hidden" not in message


@pytest.mark.parametrize(
    "value",
    [
        "Authorization: Bearer fixture-token-123",
        '"api_key": "fixture-secret-value"',
        "'password': 'a secret with spaces'",
        "AWS_ACCESS_KEY_ID=AKIAABCDEFGHIJKLMNOP",
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJmaXh0dXJlIn0.Zml4dHVyZQ",
        "Authorization: Basic Zml4dHVyZTpwYXNzd29yZA==",
    ],
)
def test_dependency_error_redacts_common_credential_formats(value):
    sanitized = bootstrap.safe_error(value)
    assert "[redacted]" in sanitized
    assert "fixture-token-123" not in sanitized
    assert "fixture-secret-value" not in sanitized
    assert "a secret with spaces" not in sanitized
    assert "AKIAABCDEFGHIJKLMNOP" not in sanitized
    assert "eyJ" not in sanitized
    assert "Zml4dHVyZTpwYXNzd29yZA==" not in sanitized


def test_missing_executable_is_actionable(monkeypatch, tmp_path):
    def missing(*args, **kwargs):
        raise FileNotFoundError("fixture")

    monkeypatch.setattr(subprocess, "run", missing)
    with pytest.raises(
        bootstrap.BootstrapError, match="Required executable is unavailable"
    ) as error:
        bootstrap.execute(["missing-tool", "--version"], tmp_path)
    assert error.value.prerequisite == "missing-tool"
    assert error.value.exit_code == 3


def test_execution_is_bounded_and_never_shell(monkeypatch, tmp_path):
    observed = {}

    def record(args, **kwargs):
        observed.update(kwargs)
        return subprocess.CompletedProcess(args, 0, "ok", "")

    monkeypatch.setattr(subprocess, "run", record)
    assert bootstrap.execute(["tool", "a path with spaces"], tmp_path) == "ok"
    assert "shell" not in observed
    assert observed["timeout"] == 600


def test_manifest_pins_are_exact():
    manifest = bootstrap.toolchain(bootstrap.ROOT)
    assert manifest["tools"]["python"]["version"] == "3.12.16"
    assert manifest["tools"]["node"]["version"] == "24.22.3"
    assert all("@sha256:" in image for image in manifest["bootstrap_images"].values())
    for action in manifest["ci_actions"].values():
        assert len(action["reference"].split("@")[1]) == 40
    assert manifest["gpu_qualification"]["status"] == "NOT_RUN"


def test_receipt_identifies_uncommitted_source(checkout):
    original = RecordedRunner()

    def dirty(args, cwd):
        if args[:2] == ["git", "status"]:
            return " M tools/bootstrap.py"
        return original(args, cwd)

    receipt = bootstrap.setup(checkout, dirty)
    assert receipt["git_worktree_dirty"] is True
    assert receipt["kind"] == "simulated"


def test_unsupported_launcher_returns_actionable_json():
    script = (
        "import runpy,sys; sys.version_info=(3,9,6); "
        f"runpy.run_path({str(bootstrap.ROOT / 'tools/bootstrap.py')!r}, run_name='__main__')"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=20, check=False
    )
    assert result.returncode == 3
    error = json.loads(result.stderr)
    assert error["prerequisite"] == "python"
    assert "Python 3.12" in error["message"]
    assert error["product_acceptance"] == "NOT_RUN"


def test_build_dependencies_are_pinned_and_hashed_in_both_locks():
    expected = {
        "editables", "hatchling", "packaging", "pathspec", "pluggy", "tomlkit", "trove-classifiers"
    }
    for relative in (".", "training"):
        directory = bootstrap.ROOT / relative
        config = tomllib.loads((directory / "pyproject.toml").read_text())["tool"]["uv"]
        assert config["require-build-hashes"] is True
        constraints = config["build-constraint-dependencies"]
        assert {item["requirement"].split("==")[0] for item in constraints} == expected
        for item in constraints:
            assert "==" in item["requirement"] and item["hashes"]
            assert all(value.startswith("sha256:") and len(value) == 71 for value in item["hashes"])
        lock = tomllib.loads((directory / "uv.lock").read_text())
        assert len(lock["manifest"]["build-constraints"]) == len(expected)


def test_container_context_only_includes_explicit_technical_files():
    lines = (bootstrap.ROOT / ".dockerignore").read_text().splitlines()
    assert lines[0] == "**"
    for line in lines:
        if line.startswith("!"):
            path = line[1:]
            assert not any(char in path for char in "*?[]")
            assert (bootstrap.ROOT / path).is_file()


def offline_command(args, cwd, extra_env=None):
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.update({"UV_OFFLINE": "1", "CI": "true"})
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        args, cwd=cwd, env=env, capture_output=True, text=True, timeout=60, check=False
    )


@pytest.mark.local_integration
@pytest.mark.parametrize("relative", [".", "training"])
def test_real_python_manifest_drift_is_refused_offline(checkout, relative):
    directory = checkout / relative
    path = directory / "pyproject.toml"
    original = path.read_text()
    changed = original.replace("dependencies = [", 'dependencies = ["pytest==0.0.0", ', 1)
    dependencies = tomllib.loads(original)["project"]["dependencies"]
    assert tomllib.loads(changed)["project"]["dependencies"] == ["pytest==0.0.0", *dependencies]
    path.write_text(changed)
    before = bootstrap.fingerprints(directory, ("uv.lock",))
    result = offline_command(
        ["uv", "lock", "--check", "--offline", "--no-python-downloads"], directory
    )
    assert result.returncode != 0, result.stdout
    assert "pytest" in result.stderr or "lockfile" in result.stderr
    assert bootstrap.fingerprints(directory, ("uv.lock",)) == before


@pytest.mark.local_integration
def test_real_typescript_manifest_drift_is_refused_offline(checkout):
    path = checkout / "package.json"
    data = json.loads(path.read_text())
    data["devDependencies"]["typescript"] = "0.0.0"
    path.write_text(json.dumps(data))
    before = bootstrap.fingerprints(checkout, ("pnpm-lock.yaml",))
    result = offline_command(
        ["pnpm", "install", "--frozen-lockfile", "--ignore-scripts", "--offline"], checkout
    )
    assert result.returncode != 0, result.stdout
    assert "ERR_PNPM_OUTDATED_LOCKFILE" in result.stdout + result.stderr
    assert bootstrap.fingerprints(checkout, ("pnpm-lock.yaml",)) == before


@pytest.mark.local_integration
def test_real_uncached_package_fails_without_network(checkout, tmp_path):
    before = bootstrap.fingerprints(checkout, bootstrap.LOCKS)
    result = offline_command(
        ["uv", "sync", "--frozen", "--offline", "--no-install-workspace", "--no-python-downloads"],
        checkout,
        {"UV_CACHE_DIR": str(tmp_path / "empty-cache")},
    )
    assert result.returncode != 0, result.stdout
    assert "wasn't found in the cache" in result.stderr
    assert "Network connectivity is disabled" in result.stderr
    assert bootstrap.fingerprints(checkout, bootstrap.LOCKS) == before


@pytest.mark.local_integration
def test_cpu_packages_import_without_cuda():
    import importlib
    from importlib.metadata import distributions

    for module in bootstrap.CPU_IMPORTS:
        importlib.import_module(module)
    names = {distribution.metadata["Name"].lower() for distribution in distributions()}
    assert not any(name.startswith(("torch", "nvidia-", "cuda", "triton")) for name in names)
