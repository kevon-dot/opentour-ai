"""OPE-123 acceptance/adverse fixtures. Fake adapters never establish product acceptance."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
from dataclasses import replace

import pytest

from tools import schema_validate
from tools import test_runner as runner


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "checkout with spaces"
    root.mkdir()
    shutil.copytree(runner.ROOT / "packages/contracts", root / "packages/contracts")
    shutil.copyfile(runner.ROOT / "pyproject.toml", root / "pyproject.toml")
    (root / "tools").mkdir()
    for name in (
        "__init__.py",
        "test_runner.py",
        "schema_validate.py",
        "web-unit-runner.mjs",
        "web-unit-loader.mjs",
    ):
        shutil.copyfile(runner.ROOT / "tools" / name, root / "tools" / name)
    for member in ("control-api", "cell-runtime", "audit-journal"):
        directory = root / "services" / member
        directory.mkdir(parents=True)
        shutil.copyfile(
            runner.ROOT / "services" / member / "pyproject.toml", directory / "pyproject.toml"
        )
    write(root, "tests/contract/test_one.py", "def test_one():\n    assert 1 + 1 == 2\n")
    write(
        root, "tests/contract/test_command_contract.py", "def test_registered():\n    assert True\n"
    )
    return root


def write(root, path, content=""):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    return target


def update_manifest(root, edit):
    path = root / runner.COMMON / "command-manifest.json"
    value = json.loads(path.read_text())
    edit(value)
    path.write_text(json.dumps(value))


class FixtureRunner:
    def __init__(self, status="passed", raw_exit=0, mutate=None):
        self.calls = []
        self.status = status
        self.raw_exit = raw_exit
        self.mutate = mutate

    def __call__(self, invocation):
        self.calls.append(invocation)
        if invocation.runner == "setup":
            return runner.ProcessResult(
                self.raw_exit,
                json.dumps(
                    {
                        "issue_key": "OPE-122",
                        "exit_code": self.raw_exit,
                        "status": "PASS" if self.raw_exit == 0 else "FAIL",
                    }
                ),
            )
        if invocation.runner in {"pytest", "playwright", "node_test"}:
            target = invocation.cwd / invocation.target
            file = (
                invocation.target
                if target.is_file()
                else next(target.rglob("*.py")).relative_to(invocation.cwd).as_posix()
            )
            tests = (
                []
                if self.status is None
                else [
                    {
                        "id": "sentinel-one",
                        "file": file,
                        "status": self.status,
                        "mode": None,
                    }
                ]
            )
            report = runner.report_envelope(
                invocation.runner, invocation.target, invocation.environment, tests
            )
            if invocation.consumer == "local_harness":
                tests = (
                    [
                        {"id": "local-" + mode, "file": file, "status": self.status, "mode": mode}
                        for mode in runner.MODES
                    ]
                    if self.status
                    else []
                )
                report = runner.report_envelope(
                    invocation.runner, invocation.target, invocation.environment, tests
                )
                report["kind"] = invocation.expected_kind
                sidecar = invocation.report.with_name(invocation.report.name + ".observations.json")
                if tests:
                    sidecar.write_text(
                        json.dumps(
                            {
                                "issue_key": "OPE-124",
                                "fixture_version": "1",
                                "product_acceptance": "NOT_RUN",
                                "observations": [
                                    {
                                        "kind": "local_integration",
                                        "mode": mode,
                                        "product_acceptance": "NOT_RUN",
                                    }
                                    for mode in runner.MODES
                                ],
                            }
                        )
                    )
            if self.mutate:
                self.mutate(report, invocation)
            invocation.report.write_text(json.dumps(report))
        return runner.ProcessResult(self.raw_exit)


COMMAND_CASES = [
    ("setup", [], 0),
    ("lint", [], 0),
    ("typecheck", [], 0),
    ("test-python", ["tests/contract/test_one.py"], 0),
    ("test-web", ["apps/web/tests/one.test.ts"], 0),
    ("test-browser", ["apps/web/tests/browser-harness.browser.spec.ts"], 0),
    ("test-contract", [], 0),
    ("validate-spec", [], 2),
    ("test-integration", ["bootstrap-contract", "all"], 0),
    ("test-isolation", ["ISO-01", "customer_controlled"], 3),
    ("test-acceptance", ["G1", "opentour_managed"], 3),
]


@pytest.mark.parametrize("command,args,expected", COMMAND_CASES)
def test_ac01_smoke_every_public_command(checkout, command, args, expected):
    write(checkout, "apps/web/tests/one.test.ts")
    write(checkout, "apps/web/tests/browser-harness.browser.spec.ts")
    write(checkout, "tools/browser-test-runner.ts")
    if command in {"lint", "typecheck"}:
        quality_harness(checkout)
    fixture = FixtureRunner()
    result = runner.run_command(command, args, checkout, fixture)
    assert result["exit_code"] == expected
    assert result["status"] == ("PASS" if expected == 0 else "BLOCKED" if expected == 3 else "FAIL")
    assert result["kind"] == "simulated"
    assert result["product_acceptance"] == "NOT_RUN"
    if expected == 0:
        assert fixture.calls
    else:
        assert fixture.calls == []


def test_every_command_has_exact_smoke_case():
    assert set(runner.COMMAND_ARITY) == {case[0] for case in COMMAND_CASES}


def test_actual_setup_recipe_preserves_json_in_merged_output(checkout):
    """Run the actual recipe with a generated bootstrap producer, without reinstalling tools."""
    shutil.copyfile(runner.ROOT / "justfile", checkout / "justfile")
    receipt = {"issue_key": "OPE-122", "status": "PASS", "exit_code": 0}
    expected = json.dumps(receipt, sort_keys=True) + "\n"
    write(checkout, "tools/bootstrap.py", f"print({expected.rstrip()!r})\n")
    invocation = runner.Invocation(
        ("just", "setup"),
        checkout,
        runner.environment(checkout, "a" * 32),
        checkout / "unused-result.json",
        "setup",
        "setup",
        timeout=10,
    )
    observed = runner.execute(invocation)
    assert observed.returncode == 0
    assert observed.cleanup_complete is True
    assert observed.stdout == expected
    assert json.loads(observed.stdout) == receipt


@pytest.mark.parametrize("command,args,_", [case for case in COMMAND_CASES if case[0] != "setup"])
def test_actual_uv_recipes_enable_build_hashes_and_preserve_merged_json(checkout, command, args, _):
    """Actual pinned uv/just transport with a generated producer, not product acceptance."""
    shutil.copyfile(runner.ROOT / "justfile", checkout / "justfile")
    shutil.copyfile(runner.ROOT / "uv.lock", checkout / "uv.lock")
    (checkout / ".venv").symlink_to((runner.ROOT / ".venv").resolve(), target_is_directory=True)
    assert "require-build-hashes = true" in (checkout / "pyproject.toml").read_text()
    write(
        checkout,
        "tools/test_runner.py",
        "import json,sys\n"
        "print(json.dumps({'command':sys.argv[1:],'exit_code':0,'status':'PASS'},sort_keys=True))\n",
    )
    expected = json.dumps(
        {"command": [command, *args], "exit_code": 0, "status": "PASS"}, sort_keys=True
    )
    invocation = runner.Invocation(
        ("just", command, *args),
        checkout,
        runner.environment(checkout, "a" * 32),
        checkout / "unused-result.json",
        command,
        "setup",
        timeout=10,
    )
    observed = runner.execute(invocation)
    assert observed.returncode == 0
    assert observed.cleanup_complete is True
    assert observed.stdout == expected + "\n"
    assert json.loads(observed.stdout)["command"] == [command, *args]


@pytest.mark.parametrize(
    "path",
    [
        "../sentinel.py",
        "tests/../../sentinel.py",
        "/tmp/sentinel.py",
        "tests/./x.py",
        "",
        "tests/x.py;touch sentinel",
        "tests/$(touch sentinel).py",
        "tests/`touch sentinel`.py",
        "tests/x.py|cat",
        "tests/x.py&cat",
        "tests/test_*.py",
        "--collect-only",
        "tests/-x.py",
        "tests/x.py::test_one",
        "tests/x.py\n",
        "tests/x.py\x00",
        "tests/x.py>sentinel",
    ],
)
def test_ac02_badpath_and_shell_syntax_rejected_before_child(checkout, path):
    fixture = FixtureRunner()
    result = runner.run_command("test-python", [path], checkout, fixture)
    assert result["exit_code"] == 2
    assert fixture.calls == []
    assert not (checkout / "sentinel").exists()


def test_ac02_symlink_escape_and_nested_symlink_rejected_before_read(checkout, tmp_path):
    external = tmp_path / "external"
    external.mkdir()
    (external / "test_private.py").write_text("raise RuntimeError('MUST_NOT_BE_READ')")
    (checkout / "tests/link").symlink_to(external, target_is_directory=True)
    fixture = FixtureRunner()
    for selection in ("tests/link/test_private.py", "tests"):
        result = runner.run_command("test-python", [selection], checkout, fixture)
        assert result["exit_code"] == 2
        assert "MUST_NOT_BE_READ" not in json.dumps(result)
    assert fixture.calls == []


@pytest.mark.parametrize(
    "command,args",
    [
        ("unknown", []),
        ("lint", ["extra"]),
        ("test-python", []),
        ("test-integration", ["unregistered", "all"]),
        ("test-isolation", ["ISO-99", "customer_controlled"]),
        ("test-acceptance", ["G99", "opentour_managed"]),
        ("test-acceptance", ["G1", "unknown"]),
        ("test-integration", ["bootstrap-contract", "x;touch sentinel"]),
    ],
)
def test_unknown_suite_profile_mode_and_arity(checkout, command, args):
    fixture = FixtureRunner()
    assert runner.run_command(command, args, checkout, fixture)["exit_code"] == 2
    assert fixture.calls == []


@pytest.mark.parametrize("gate", ["G1", "G6"])
@pytest.mark.parametrize("mode", runner.MODES)
def test_ac03_missing_environment_customer_evidence_never_passes(checkout, gate, mode, monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "sentinel-not-a-real-credential")
    monkeypatch.setenv("CUSTOMER_EVIDENCE", "a-string-is-not-a-receipt")
    fixture = FixtureRunner()
    result = runner.run_command("test-acceptance", [gate, mode], checkout, fixture)
    assert result["exit_code"] == 3
    assert result["status"] == "BLOCKED" and result["product_acceptance"] == "NOT_RUN"
    assert result["mode"] == mode and fixture.calls == []
    assert "sentinel-not" not in json.dumps(result)


def test_missing_provider_credentials_profile_is_blocked_before_dispatch(checkout):
    def add(value):
        value["integration_profiles"]["provider-fixture"] = {
            "kind": "provider_integration",
            "path": None,
            "owner": "fixture-only",
            "prerequisites": ["authorized-environment", "provider-credentials"],
        }

    update_manifest(checkout, add)
    fixture = FixtureRunner()
    result = runner.run_command("test-integration", ["provider-fixture", "all"], checkout, fixture)
    assert result["exit_code"] == 3 and fixture.calls == []


@pytest.mark.parametrize(
    "command,target",
    [
        ("test-web", "apps/web/tests/customer-journeys.spec.ts"),
        ("test-browser", "apps/web/tests/one.test.ts"),
    ],
)
def test_cross_runner_input_fails_with_guidance(checkout, command, target):
    write(checkout, target)
    fixture = FixtureRunner()
    result = runner.run_command(command, [target], checkout, fixture)
    assert result["exit_code"] == 2
    assert "test-browser" in result["message"] and "test-web" in result["message"]
    assert fixture.calls == []


def test_exact_canonical_browser_inventory_preserved(checkout):
    manifest = runner.manifest(checkout)
    source = json.loads((checkout / runner.COMMON / "browser-suite-inventory.json").read_text())
    assert set(manifest["browser_suites"]) == set(source["paths"])
    assert len(manifest["browser_suites"]) == source["distinct_paths"] == 83
    sprint_profiles = {
        "apps/web/components/foundation/data-display/paired-outcomes.visual.spec.ts": "OPE-345",
        "apps/web/components/foundation/forms/nullable-field.visual.spec.ts": "OPE-322",
        "apps/web/features/workspace-lifecycle/project-description.visual.spec.ts": "OPE-323",
        "apps/web/features/account-entry/setup-step-indicator.visual.spec.ts": "OPE-324",
        "apps/web/components/foundation/data-display/category-chart.visual.spec.ts": "OPE-344",
        "apps/web/components/foundation/forms/form-error-summary.visual.spec.ts": "OPE-321",
        "apps/web/components/foundation/forms/derived-value.visual.spec.ts": "OPE-320",
        "apps/web/components/foundation/data-display/relationship-context.visual.spec.ts": (
            "OPE-319"
        ),
        "apps/web/components/foundation/tokens/selection-controls.visual.spec.ts": "OPE-318",
        "apps/web/components/foundation/forms/single-choice-select.visual.spec.ts": "OPE-317",
        "apps/web/components/foundation/forms/numeric-field.visual.spec.ts": "OPE-328",
        "apps/web/components/foundation/forms/unit-field.visual.spec.ts": "OPE-334",
        "apps/web/components/foundation/forms/text-field.visual.spec.ts": "OPE-316",
        "apps/web/components/foundation/tokens/skeleton.visual.spec.ts": "OPE-310",
        "apps/web/components/foundation/tokens/icon-button.visual.spec.ts": "OPE-311",
        "apps/web/components/foundation/tokens/status-icon.visual.spec.ts": "OPE-312",
        "apps/web/components/foundation/tokens/tooltip.visual.spec.ts": "OPE-313",
        "apps/web/components/foundation/forms/difference-review.visual.spec.ts": "OPE-314",
        "apps/web/features/operations/cancellation-impact.visual.spec.ts": "OPE-315",
    }
    for path, owner in sprint_profiles.items():
        assert source["paths"][path] == [owner]
        assert manifest["browser_profiles"][path] == {"kind": "simulated", "owner": owner}
    shell = "apps/web/features/workspace/customer-shell.visual.spec.ts"
    assert source["paths"][shell] == ["OPE-239"]
    assert manifest["browser_profiles"][shell] == {"kind": "simulated", "owner": "OPE-307"}


@pytest.mark.parametrize(
    "target",
    [
        "apps/web/tests/customer-journeys.spec.ts",
        "apps/web/tests/accessibility.spec.ts",
        "apps/web/tests/autonomy-status.spec.ts",
        "apps/web/features/support-case-private/privacy.spec.ts",
        "apps/web/tests/coverage/ot-p22-c01.spec.ts",
    ],
)
def test_plain_declared_browser_names_are_accepted(checkout, target):
    write(checkout, target)
    write(checkout, "tools/browser-test-runner.ts")
    update_manifest(
        checkout,
        lambda value: value["browser_profiles"].update(
            {target: {"kind": "simulated", "owner": "generated-transport-fixture"}}
        ),
    )
    fixture = FixtureRunner()
    result = runner.run_command("test-browser", [target], checkout, fixture)
    assert result["exit_code"] == 0
    assert fixture.calls[0].argv[:3] == ("node", "tools/browser-test-runner.ts", target)


def test_declared_directory_and_filename_spaces_are_single_arguments(checkout):
    target = "apps/web/tests/suite with spaces"
    write(checkout, f"{target}/test_fixture.py")
    write(checkout, "tools/browser-test-runner.ts")
    update_manifest(checkout, lambda value: value["browser_suites"].append(target))
    update_manifest(
        checkout,
        lambda value: value["browser_profiles"].update(
            {target: {"kind": "simulated", "owner": "generated-transport-fixture"}}
        ),
    )
    fixture = FixtureRunner()
    assert runner.run_command("test-browser", [target], checkout, fixture)["exit_code"] == 0
    assert fixture.calls[0].argv[2] == target


def test_undeclared_browser_suite_and_missing_harness_are_truthful(checkout):
    target = "apps/web/tests/browser-harness.browser.spec.ts"
    fixture = FixtureRunner()
    assert runner.run_command("test-browser", [target], checkout, fixture)["exit_code"] == 2
    write(checkout, target)
    result = runner.run_command("test-browser", [target], checkout, fixture)
    assert result["reason_code"] == "MISSING_APPLICATION"
    write(checkout, "apps/web/tests/undeclared.spec.ts")
    assert (
        runner.run_command(
            "test-browser", ["apps/web/tests/undeclared.spec.ts"], checkout, fixture
        )["exit_code"]
        == 2
    )
    assert fixture.calls == []


@pytest.mark.parametrize(
    "status,raw,code", [("passed", 0, 0), ("failed", 1, 1), ("skipped", 0, 1), (None, 5, 2)]
)
def test_child_outcomes_normalize_without_false_pass(checkout, status, raw, code):
    fixture = FixtureRunner(status, raw)
    result = runner.run_command("test-python", ["tests/contract/test_one.py"], checkout, fixture)
    assert result["exit_code"] == code
    assert result["steps"][0]["raw_exit_code"] == raw


@pytest.mark.parametrize("raw", [2, 3, 4])
def test_pytest_internal_and_collection_errors_are_not_external_blocked(checkout, raw):
    result = runner.run_command(
        "test-python", ["tests/contract/test_one.py"], checkout, FixtureRunner("passed", raw)
    )
    assert result["exit_code"] == 2
    assert result["status"] == "FAIL"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r, i: r.update(binding_nonce="0" * 32),
        lambda r, i: r.update(target="tests/contract/other.py"),
        lambda r, i: r.update(runner="playwright"),
        lambda r, i: r.update(kind="provider_integration"),
        lambda r, i: r.update(collected=2),
        lambda r, i: r.update(passed=True),
        lambda r, i: r.update(extra="unknown"),
        lambda r, i: r["tests"].append(r["tests"][0]),
        lambda r, i: r["tests"][0].update(file="tests/contract/test_command_contract.py"),
    ],
)
def test_forged_stale_malformed_out_of_target_reports_fail(checkout, mutation):
    result = runner.run_command(
        "test-python", ["tests/contract/test_one.py"], checkout, FixtureRunner(mutate=mutation)
    )
    assert result["exit_code"] == 2
    assert result["status"] == "FAIL"


def test_cleanup_failure_blocks_success(checkout):
    result = runner.run_command(
        "test-python",
        ["tests/contract/test_one.py"],
        checkout,
        FixtureRunner(mutate=lambda r, i: r.update(cleanup_complete=False)),
    )
    assert result["exit_code"] == 1 and result["reason_code"] == "CLEANUP_FAILED"


def test_missing_report_and_timeout_never_pass(checkout):
    assert (
        runner.run_command(
            "test-python",
            ["tests/contract/test_one.py"],
            checkout,
            lambda i: runner.ProcessResult(0),
        )["exit_code"]
        == 2
    )
    result = runner.run_command(
        "test-python",
        ["tests/contract/test_one.py"],
        checkout,
        lambda i: runner.ProcessResult(-9, timed_out=True),
    )
    assert result["exit_code"] == 1 and result["reason_code"] == "RUNNER_TIMEOUT"


def test_typecheck_failure_stops_dependent_steps(checkout):
    fixture = FixtureRunner(raw_exit=1)
    result = runner.run_command("typecheck", [], checkout, fixture)
    assert result["exit_code"] == 1 and len(fixture.calls) == 1
    assert "mypy" in fixture.calls[0].argv


def test_training_typecheck_uses_the_separate_installed_package_metadata(checkout):
    write(checkout, "training/pyproject.toml")
    write(checkout, "training/.venv/pyvenv.cfg")
    write(checkout, "training/.venv/bin/python")
    fixture = FixtureRunner()

    result = runner.run_command("typecheck", [], checkout, fixture)

    assert result["status"] == "PASS"
    workspace, training, typescript = fixture.calls
    assert "mypy" in workspace.argv and "training/src" not in workspace.argv
    assert "mypy" in training.argv
    assert training.argv[-3:] == (
        "--python-executable",
        "training/.venv/bin/python",
        "training/src",
    )
    assert training.environment["UV_OFFLINE"] == "1"
    assert typescript.argv[:3] == ("pnpm", "exec", "tsc")


@pytest.mark.parametrize("prefix", ["missing", "workspace-symlink"])
def test_training_typecheck_refuses_missing_or_shared_environment(checkout, prefix):
    write(checkout, "training/pyproject.toml")
    if prefix == "workspace-symlink":
        write(checkout, ".venv/pyvenv.cfg")
        write(checkout, ".venv/bin/python")
        (checkout / "training/.venv").symlink_to(checkout / ".venv", target_is_directory=True)
    fixture = FixtureRunner()

    result = runner.run_command("typecheck", [], checkout, fixture)

    assert result["status"] == "BLOCKED"
    assert result["reason_code"] == "MISSING_EXTERNAL_PREREQUISITE"
    assert not fixture.calls


def quality_harness(root):
    for name in (
        "playwright.config.ts",
        "tools/browser-test-runner.ts",
        "apps/web/tests/browser-harness.browser.spec.ts",
        "apps/web/tests/fixtures/harness/tsconfig.json",
        "apps/web/tests/fixtures/harness/.oxlintrc.json",
    ):
        write(root, name)


@pytest.mark.parametrize(
    "command,expected_steps,tool", [("lint", 2, "oxlint"), ("typecheck", 3, "tsc")]
)
def test_browser_quality_runs_actual_owned_config_commands(checkout, command, expected_steps, tool):
    quality_harness(checkout)
    fixture = FixtureRunner()
    result = runner.run_command(command, [], checkout, fixture)
    assert result["exit_code"] == 0 and len(fixture.calls) == expected_steps
    assert fixture.calls[-1].argv[:3] == ("pnpm", "exec", tool)
    if command == "lint":
        assert "--deny-warnings" in fixture.calls[-1].argv
        assert "tools/browser-test-runner.ts" in fixture.calls[-1].argv
    else:
        assert fixture.calls[-1].argv[-2:] == (
            "--project",
            "apps/web/tests/fixtures/harness/tsconfig.json",
        )


@pytest.mark.parametrize("command", ["lint", "typecheck"])
def test_browser_quality_missing_config_is_not_silently_skipped(checkout, command):
    write(checkout, "tools/browser-test-runner.ts")
    fixture = FixtureRunner()
    assert runner.run_command(command, [], checkout, fixture)["exit_code"] == 2
    assert not fixture.calls


@pytest.mark.parametrize("command", ["lint", "typecheck"])
def test_browser_quality_failure_propagates(checkout, command):
    quality_harness(checkout)

    def fail_owned_check(invocation):
        fail = "oxlint" in invocation.argv or "--project" in invocation.argv
        return runner.ProcessResult(1 if fail else 0)

    result = runner.run_command(command, [], checkout, fail_owned_check)
    assert result["exit_code"] == 1 and result["steps"][-1]["exit_code"] == 1


def test_credentials_and_runner_overrides_not_inherited(checkout, monkeypatch):
    for key in ("AWS_SECRET_ACCESS_KEY", "STRIPE_SECRET_KEY", "PYTEST_ADDOPTS", "NODE_OPTIONS"):
        monkeypatch.setenv(key, "sentinel-secret")
    fixture = FixtureRunner()
    runner.run_command("test-python", ["tests/contract/test_one.py"], checkout, fixture)
    assert "sentinel-secret" not in json.dumps(fixture.calls[0].environment)


def local_pytest(invocation):
    """Exercise real pytest in the current pinned interpreter; no uv install or provider calls."""
    index = invocation.argv.index("-m")
    return runner.execute(
        replace(invocation, argv=(sys.executable, *invocation.argv[index:]), timeout=20)
    )


@pytest.mark.parametrize(
    "body,expected",
    [
        ("def test_one():\n    assert 2 + 2 == 4\n", 0),
        ("def test_one():\n    assert False\n", 1),
        ("pass\n", 2),
        (
            "import pytest\n@pytest.mark.skip(reason='required fixture unavailable')\n"
            "def test_one(): pass\n",
            1,
        ),
        (
            "import socket\ndef test_network():\n"
            "    socket.create_connection(('127.0.0.1', 9), timeout=0.1)\n",
            1,
        ),
        (
            "import pytest\n@pytest.mark.xfail(reason='must remain nonpassing')\n"
            "def test_one(): assert False\n",
            1,
        ),
    ],
)
def test_actual_pytest_one_pass_failure_zero_skip_and_forbidden_network(checkout, body, expected):
    target = "tests/contract/test with spaces.py"
    write(checkout, target, body)
    result = runner.run_command("test-python", [target], checkout, local_pytest)
    assert result["exit_code"] == expected, result
    assert result["product_acceptance"] == "NOT_RUN"


def test_package_routing_consumes_workspace_and_keeps_training_separate(checkout):
    assert "opentour-control" in runner.uv_python(
        checkout, "services/control-api/tests/test_one.py"
    )
    assert "opentour-cell" in runner.uv_python(checkout, "services/cell-runtime/tests/test_one.py")
    assert "opentour-audit" in runner.uv_python(
        checkout, "services/audit-journal/tests/test_one.py"
    )
    training = runner.uv_python(checkout, "training/tests/test_one.py")
    assert training[training.index("--project") + 1] == "training"
    assert "--package" not in training


@pytest.mark.parametrize(
    "target",
    [
        "infrastructure/tests/test_ci_oidc_trust.py",
        "infrastructure/tests",
        "infrastructure/tests/nested/test with spaces.py",
    ],
)
def test_infrastructure_test_scope_uses_root_workspace(checkout, target):
    file = target if target.endswith(".py") else target + "/test_ci_oidc_trust.py"
    write(checkout, file, "def test_one(): assert True\n")
    fixture = FixtureRunner()
    result = runner.run_command("test-python", [target], checkout, fixture)
    assert result["exit_code"] == 0
    assert fixture.calls[0].argv[:6] == runner.uv_python(checkout)
    assert fixture.calls[0].argv[-2:] == ("--", target)


@pytest.mark.parametrize(
    "target",
    [
        "infrastructure",
        "infrastructure/test_ci_oidc_trust.py",
        "infrastructure/tests.py",
        "infrastructure/tests-extra/test_ci_oidc_trust.py",
        "infrastructure/tests/../test_ci_oidc_trust.py",
    ],
)
def test_infrastructure_non_test_scope_rejected_before_execution(checkout, target):
    write(checkout, "infrastructure/tests/test_ci_oidc_trust.py")
    if target.endswith(".py") and ".." not in target:
        write(checkout, target)
    fixture = FixtureRunner()
    result = runner.run_command("test-python", [target], checkout, fixture)
    assert result["exit_code"] == 2
    assert fixture.calls == []


def test_infrastructure_test_scope_rejects_symlink_escape(checkout):
    external_scope = write(checkout, "infrastructure/runtime.py")
    link = checkout / "infrastructure/tests/test_ci_oidc_trust.py"
    link.parent.mkdir()
    link.symlink_to(external_scope)
    fixture = FixtureRunner()
    result = runner.run_command(
        "test-python", [link.relative_to(checkout).as_posix()], checkout, fixture
    )
    assert result["exit_code"] == 2
    assert fixture.calls == []


def test_actual_infrastructure_route_uses_root_environment(checkout):
    qualified_environment = runner.ROOT / ".venv"
    shutil.copyfile(runner.ROOT / "uv.lock", checkout / "uv.lock")
    (checkout / ".venv").symlink_to(qualified_environment.resolve(), target_is_directory=True)
    target = "infrastructure/tests/test_ci_oidc_trust.py"
    write(
        checkout,
        target,
        "import sys\nfrom pathlib import Path\n"
        f"EXPECTED_ENVIRONMENT = Path({str(qualified_environment.resolve())!r})\n"
        "def test_root_environment():\n"
        "    assert Path(sys.prefix).resolve() == EXPECTED_ENVIRONMENT\n",
    )
    result = runner.run_command("test-python", [target], checkout)
    assert result["exit_code"] == 0, result
    assert result["steps"][0]["tests"]["collected"] == 1
    assert result["steps"][0]["tests"]["passed"] == 1


@pytest.mark.parametrize(
    "outcome,expected,counts",
    [("pass", 0, (1, 0, 0)), ("fail", 1, (0, 1, 0)), ("skip", 1, (0, 0, 1))],
)
def test_actual_training_route_uses_separate_pytest_only_environment(
    checkout, outcome, expected, counts
):
    qualified_environment = runner.ROOT / "training/.venv"
    assert (qualified_environment / "bin/python").is_file(), "Run qualified separate training setup"
    training = checkout / "training"
    training.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copyfile(runner.ROOT / "training" / name, training / name)
    shutil.copytree(runner.ROOT / "training/src", training / "src")
    (training / ".venv").symlink_to(qualified_environment.resolve(), target_is_directory=True)
    tail = {
        "pass": "assert True",
        "fail": "assert False",
        "skip": "pytest.skip('required sentinel')",
    }[outcome]
    target = "training/tests/test route with spaces.py"
    write(
        checkout,
        target,
        "import importlib.metadata,importlib.util,sys\nfrom pathlib import Path\nimport pytest\n"
        "def test_training_route():\n"
        f"    assert Path(sys.prefix).resolve() == Path({str(qualified_environment.resolve())!r})\n"
        "    assert importlib.metadata.version('jsonschema') == '4.26.0'\n"
        "    assert importlib.util.find_spec('opentour_control') is None\n"
        "    assert importlib.util.find_spec('mypy') is None\n"
        "    assert 'jsonschema' not in sys.modules\n"
        "    assert pytest.__version__ == '9.2.0'\n"
        "    Path('TRAINING_ENV_CHECKED').touch()\n"
        f"    {tail}\n",
    )
    calls = []

    def actual_training(invocation):
        calls.append(invocation)
        return runner.execute(replace(invocation, timeout=20))

    result = runner.run_command("test-python", [target], checkout, actual_training)
    assert result["exit_code"] == expected, result
    assert (checkout / "TRAINING_ENV_CHECKED").exists()
    assert calls[0].argv[calls[0].argv.index("--project") + 1] == "training"
    report = result["steps"][0]["tests"]
    assert report["collected"] == 1
    assert (report["passed"], report["failed"], report["skipped"]) == counts
    assert result["product_acceptance"] == "NOT_RUN"


def test_schema_shapes_semantic_revision_bounds_and_authority_aliases(checkout):
    result = schema_validate.validate_examples(checkout)
    assert result["collected"] >= 29 and result["failed"] == 0
    assert result["product_acceptance"] == "NOT_RUN"


@pytest.mark.parametrize(
    "reference",
    ["https://example.invalid/schema.json", "file:///etc/passwd", "urn:opentour:missing:1"],
)
def test_unknown_schema_never_resolves_network_or_file(checkout, reference, monkeypatch):
    registry = schema_validate.schema_registry(checkout)
    monkeypatch.setattr("socket.socket.connect", lambda *args: pytest.fail("Network forbidden"))
    with pytest.raises(schema_validate.SchemaError):
        schema_validate.valid_instance(reference, {}, registry)


def test_changed_canonical_schema_hash_is_rejected(checkout):
    path = checkout / runner.COMMON / "common-contract.schema.json"
    path.write_text(path.read_text() + " ")
    with pytest.raises(schema_validate.SchemaError, match="hash mismatch"):
        schema_validate.schema_registry(checkout)


def test_duplicate_json_keys_and_nonfinite_numbers_are_rejected(tmp_path):
    for text in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":Infinity}'):
        path = tmp_path / "invalid.json"
        path.write_text(text)
        with pytest.raises(schema_validate.SchemaError):
            schema_validate.read_json(path)


def test_cli_json_nonzero_invalid_command():
    result = subprocess.run(
        [sys.executable, "-m", "tools.test_runner", "unknown"],
        cwd=runner.ROOT,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 2
    receipt = json.loads(result.stdout)
    assert receipt["exit_code"] == 2 and receipt["output_digests"]["summary"]
    assert receipt["git_worktree_dirty"] is True or receipt["commit_sha"]


def test_declared_future_browser_path_has_no_implicit_simulated_profile(checkout):
    target = "apps/web/tests/customer-journeys.spec.ts"
    write(checkout, target)
    write(checkout, "tools/browser-test-runner.ts")
    fixture = FixtureRunner()
    assert runner.run_command("test-browser", [target], checkout, fixture)["exit_code"] == 2
    assert not fixture.calls


@pytest.mark.parametrize("state", ["passed", "failed", "skipped"])
def test_blocked_report_cannot_hide_executed_or_skipped_inventory(checkout, state):
    target = "apps/web/tests/browser-harness.browser.spec.ts"
    write(checkout, target)
    write(checkout, "tools/browser-test-runner.ts")
    fixture = FixtureRunner(
        state,
        3,
        lambda r, i: r.update(status="BLOCKED", reason_code="MISSING_EXTERNAL_PREREQUISITE"),
    )
    result = runner.run_command("test-browser", [target], checkout, fixture)
    assert result["exit_code"] == 2 and result["status"] == "FAIL"
    assert result["steps"][0]["raw_exit_code"] == 3


def test_malformed_child_report_retains_attempted_step_without_raw_data(checkout):
    def malformed(invocation):
        invocation.report.write_text('{"private":"sentinel-secret"')
        return runner.ProcessResult(1)

    result = runner.run_command("test-python", ["tests/contract/test_one.py"], checkout, malformed)
    assert result["exit_code"] == 2
    assert result["steps"][0]["raw_exit_code"] == 1
    assert "sentinel-secret" not in json.dumps(result)


def test_typecheck_retains_completed_step_if_second_executable_is_missing(checkout):
    calls = []

    def missing_second(invocation):
        calls.append(invocation)
        if len(calls) == 2:
            raise runner.CommandError("Missing pinned tool", 3, "MISSING_EXTERNAL_PREREQUISITE")
        return runner.ProcessResult(0)

    result = runner.run_command("typecheck", [], checkout, missing_second)
    assert result["exit_code"] == 3
    assert [step["exit_code"] for step in result["steps"]] == [0, 3]


def test_invalid_arguments_and_parameterized_ids_are_not_copied_to_evidence(checkout):
    secret = "sentinel-private-value"
    invalid = runner.run_command("test-python", ["tests/no.py;api_key=" + secret], checkout)
    assert invalid["exit_code"] == 2 and secret not in json.dumps(invalid)
    fixture = FixtureRunner(mutate=lambda r, i: r["tests"][0].update(id="test[" + secret + "]"))
    result = runner.run_command("test-python", ["tests/contract/test_one.py"], checkout, fixture)
    assert result["exit_code"] == 0 and secret not in json.dumps(result)
    assert result["steps"][0]["tests"]["tests"][0]["id"].startswith("sha256:")


@pytest.mark.parametrize("document", [[], None, "invalid schema"])
def test_nonobject_canonical_schema_is_structured_configuration_failure(checkout, document):
    path = checkout / runner.COMMON / "common-contract.schema.json"
    path.write_text(json.dumps(document))
    registry_path = checkout / runner.COMMON / "schema-registry.json"
    registry = json.loads(registry_path.read_text())
    registry["schemas"][0]["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    registry_path.write_text(json.dumps(registry))
    result = runner.run_command("test-contract", [], checkout, FixtureRunner())
    assert result["exit_code"] == 2


def test_schema_and_test_path_symlink_loops_fail_without_traceback(checkout):
    path = checkout / runner.COMMON / "common-contract.schema.json"
    path.unlink()
    path.symlink_to(path.name)
    assert runner.run_command("test-contract", [], checkout)["exit_code"] == 2
    target = checkout / "tests/loop.py"
    target.symlink_to(target.name)
    assert runner.run_command("test-python", ["tests/loop.py"], checkout)["exit_code"] == 2


def test_source_dirty_before_execution_cannot_be_erased_from_receipt(checkout):
    def git(*args):
        subprocess.run(["git", *args], cwd=checkout, check=True, capture_output=True, timeout=5)

    git("init", "-q")
    git("add", ".")
    git(
        "-c",
        "user.name=Contract Fixture",
        "-c",
        "user.email=fixture@example.invalid",
        "commit",
        "-qm",
        "Generated fixture",
    )
    target = checkout / "tests/contract/test_one.py"
    original = target.read_text()
    target.write_text(original + "# dirty sentinel\n")
    fixture = FixtureRunner()

    def restore_then_execute(invocation):
        target.write_text(original)
        return fixture(invocation)

    result = runner.run_command(
        "test-python", ["tests/contract/test_one.py"], checkout, restore_then_execute
    )
    assert result["exit_code"] == 0
    assert result["git_worktree_dirty"] and result["git_worktree_dirty_before"]
    assert not result["git_worktree_dirty_after"]


def test_runner_kills_descendants_even_when_leader_exits_successfully(checkout):
    pid_file = checkout / "descendant.pid"
    program = (
        "import subprocess,sys,pathlib; "
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid))"
    )
    invocation = runner.Invocation(
        (sys.executable, "-c", program),
        checkout,
        dict(os.environ),
        checkout / "unused-report",
        "probe",
        "probe",
        timeout=5,
    )
    try:
        result = runner.execute(invocation)
        assert result.returncode == 0 and not result.cleanup_complete
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize(
    "limit,size,passes",
    [
        (None, 1_048_576, True),
        (None, 1_048_577, False),
        (8_388_608, 1_048_577, True),
        (8_388_608, 8_388_608, True),
        (8_388_608, 8_388_609, False),
    ],
)
def test_actual_runner_output_limit_counts_both_streams(checkout, limit, size, passes):
    first = size // 2
    invocation = runner.Invocation(
        (
            sys.executable,
            "-c",
            "import sys;"
            f"sys.stdout.buffer.write(b'a'*{first});sys.stdout.flush();"
            f"sys.stderr.buffer.write(b'b'*{size - first});sys.stderr.flush()",
        ),
        checkout,
        runner.environment(checkout, "a" * 32),
        checkout / "unused.json",
        "controlled-output",
        "setup",
        timeout=10,
    )
    assert invocation.output_limit_bytes == 1_048_576
    if limit is not None:
        invocation = replace(invocation, output_limit_bytes=limit)
    if passes:
        result = runner.execute(invocation)
        assert result.returncode == 0 and result.cleanup_complete
        assert len(result.stdout.encode()) == size
        assert result.stdout == "a" * first + "b" * (size - first)
    else:
        with pytest.raises(runner.CommandError) as error:
            runner.execute(invocation)
        assert error.value.code == 1 and error.value.reason == "RUNNER_ERROR"


@pytest.mark.parametrize("limit,passes", [(4, True), (3, False)])
def test_actual_runner_output_limit_counts_utf8_bytes(checkout, limit, passes):
    invocation = runner.Invocation(
        (sys.executable, "-c", "import sys;sys.stdout.buffer.write(bytes([195,169])*2)"),
        checkout,
        runner.environment(checkout, "a" * 32),
        checkout / "unused.json",
        "controlled-output",
        "setup",
        timeout=10,
        output_limit_bytes=limit,
    )
    if passes:
        result = runner.execute(invocation)
        assert result.stdout == "éé" and len(result.stdout.encode()) == 4
    else:
        with pytest.raises(runner.CommandError) as error:
            runner.execute(invocation)
        assert error.value.code == 1


@pytest.mark.parametrize("limit", [0, -1, 8_388_609, True, 1.5, "8388608"])
def test_invalid_runner_output_limit_refuses_before_launch(checkout, monkeypatch, limit):
    monkeypatch.setattr(runner, "_MANAGED_POPEN", lambda *a, **k: pytest.fail("Child was launched"))
    invocation = runner.Invocation(
        (sys.executable, "-c", "pass"),
        checkout,
        {},
        checkout / "unused.json",
        "controlled-output",
        "setup",
        output_limit_bytes=limit,
    )
    with pytest.raises(runner.CommandError) as error:
        runner.execute(invocation)
    assert error.value.code == 2


def test_process_group_permission_uncertainty_is_structured_nonpass(checkout, monkeypatch):
    signals = []

    def denied(pid, sig):
        signals.append((pid, sig))
        raise PermissionError("sentinel-private-os-detail")

    monkeypatch.setattr(runner.os, "killpg", denied)

    def actual(invocation):
        return runner.execute(replace(invocation, argv=(sys.executable, "-c", "pass"), timeout=5))

    result = runner.run_command("lint", [], checkout, actual)
    assert result["exit_code"] == 1 and result["reason_code"] == "CLEANUP_FAILED"
    assert result["steps"][0]["process_cleanup_complete"] is False
    assert [sig for _, sig in signals] == [0]
    assert "sentinel-private-os-detail" not in json.dumps(result)


@pytest.mark.parametrize(
    "operation",
    [
        "socket.getaddrinfo('example.invalid', 80)",
        "socket.socket(socket.AF_INET, socket.SOCK_DGRAM).sendto(b'x', ('127.0.0.1', 9))",
    ],
)
def test_actual_python_dns_and_udp_are_denied(checkout, operation):
    target = "tests/contract/test_network.py"
    write(checkout, target, "import socket\ndef test_network():\n    " + operation + "\n")
    result = runner.run_command("test-python", [target], checkout, local_pytest)
    assert result["exit_code"] == 1


@pytest.mark.parametrize(
    "operation",
    [
        "require('node:net').connect(9,'127.0.0.1')",
        "require('node:dns').lookup('example.invalid',()=>{})",
        "require('node:dgram').createSocket('udp4').send('x',9,'127.0.0.1')",
    ],
)
def test_actual_node_network_preload_denies_tcp_dns_and_udp(checkout, operation):
    node = shutil.which("node")
    assert node, "Pinned Node is required; run qualified setup and use its PATH"
    guard = checkout / runner.COMMON / "unit-network-guard.cjs"
    env = runner.environment(checkout, "0" * 32)
    env["NODE_OPTIONS"] = "--require=" + json.dumps(str(guard))
    result = subprocess.run(
        [node, "-e", operation], cwd=checkout, env=env, capture_output=True, text=True, timeout=10
    )
    assert result.returncode != 0 and "OPENTOUR_NETWORK_DENIED" in result.stderr


def local_node_test(invocation):
    node = shutil.which("node")
    assert node, "Pinned Node is required; run qualified setup and use its PATH"
    assert (runner.ROOT / "node_modules/esbuild").is_dir(), "Frozen esbuild is required"
    (invocation.cwd / "node_modules").symlink_to(
        runner.ROOT / "node_modules", target_is_directory=True
    )
    return runner.execute(replace(invocation, argv=(node, *invocation.argv[1:]), timeout=30))


@pytest.mark.parametrize(
    "body,expected,reason,collected",
    [
        (
            "import {test} from 'node:test'; import assert from 'node:assert/strict'; "
            "test('one',()=>{const value: number = 2+2; assert.equal(value,4)});",
            0,
            None,
            1,
        ),
        (
            "import {test} from 'node:test'; import net from 'node:net'; import fs from 'node:fs'; "
            "test('network',()=>{try{net.connect(9,'127.0.0.1')}catch(error){"
            "if(error.code==='OPENTOUR_NETWORK_DENIED')fs.writeFileSync('NETWORK_DENIED','yes');"
            "throw error}});",
            1,
            "ASSERTION_FAILED",
            1,
        ),
        (
            "import {test} from 'node:test'; test.skip('required',()=>{});",
            1,
            "REQUIRED_TEST_SKIPPED",
            1,
        ),
        (
            "import {test} from 'node:test'; test.todo('required',()=>{});",
            1,
            "REQUIRED_TEST_SKIPPED",
            1,
        ),
        (
            "import {describe} from 'node:test'; describe.skip('suite',()=>{});",
            1,
            "REQUIRED_TEST_SKIPPED",
            0,
        ),
        (
            "import {test} from 'node:test'; test.only('focus',()=>{});",
            2,
            "INVALID_CONFIGURATION",
            1,
        ),
        ("export {};", 2, "NO_TESTS", 0),
        ("throw new Error('load failed');", 2, "INVALID_CONFIGURATION", 0),
    ],
)
def test_actual_node_test_exact_file_network_and_required_inventory(
    checkout, body, expected, reason, collected
):
    target = "apps/web/tests/test with spaces.test.ts"
    write(checkout, target, body)
    write(
        checkout,
        "backup/" + target,
        "import fs from 'node:fs'; fs.writeFileSync('UNREQUESTED_EXECUTED','bad'); "
        "throw new Error('Unrequested test executed');",
    )
    result = runner.run_command("test-web", [target], checkout, local_node_test)
    assert not (checkout / "UNREQUESTED_EXECUTED").exists()
    assert result["exit_code"] == expected and result["reason_code"] == reason, result
    assert result["steps"][0]["tests"]["collected"] == collected
    if "net.connect" in body:
        assert (checkout / "NETWORK_DENIED").read_text() == "yes"


@pytest.mark.parametrize("key", ["browser_profiles", "integration_profiles"])
def test_invalid_unhashable_profile_kind_is_configuration_failure(checkout, key):
    def malformed(value):
        next(iter(value[key].values()))["kind"] = []

    update_manifest(checkout, malformed)
    assert runner.run_command("lint", [], checkout, FixtureRunner())["exit_code"] == 2


@pytest.mark.parametrize("document", [[], None, {"$id": "urn:opentour:test-result:1.0", "type": 7}])
def test_malformed_transport_schema_emits_configuration_error(checkout, document):
    path = checkout / runner.COMMON / "test-result.schema.json"
    path.write_text(json.dumps(document))
    manifest_path = checkout / runner.COMMON / "schema-registry.json"
    registry = json.loads(manifest_path.read_text())
    for entry in registry["schemas"]:
        if entry["file"] == path.name:
            entry["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(registry))
    result = runner.run_command(
        "test-python", ["tests/contract/test_one.py"], checkout, FixtureRunner()
    )
    assert result["exit_code"] == 2 and result["steps"][0]["raw_exit_code"] == 0


def test_actual_network_guard_precedes_initial_conftest_import(checkout):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.1)
        port = receiver.getsockname()[1]
        write(
            checkout,
            "tests/conftest.py",
            "import socket\nsocket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto("
            f"b'sentinel-conftest',('127.0.0.1',{port}))\n",
        )
        result = runner.run_command(
            "test-python", ["tests/contract/test_one.py"], checkout, local_pytest
        )
        assert result["exit_code"] == 2
        with pytest.raises(TimeoutError):
            receiver.recvfrom(1024)


@pytest.mark.parametrize("location", ["conftest", "test_body"])
@pytest.mark.parametrize("option", ["start_new_session=True", "process_group=0"])
def test_actual_python_detached_child_creation_is_denied_before_launch(checkout, location, option):
    pid_file = checkout / "ESCAPED_CHILD.pid"
    program = (
        "import pathlib,subprocess,sys\n"
        f"p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'],{option},"
        "stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)\n"
        "pathlib.Path('ESCAPED_CHILD.pid').write_text(str(p.pid))\n"
    )
    target = "tests/contract/test_one.py"
    if location == "conftest":
        write(checkout, "tests/conftest.py", program)
    else:
        write(
            checkout,
            target,
            "def test_detach():\n"
            + "\n".join("    " + line for line in program.splitlines())
            + "\n",
        )
    try:
        result = runner.run_command("test-python", [target], checkout, local_pytest)
        assert result["exit_code"] == (2 if location == "conftest" else 1)
        assert not pid_file.exists(), "A detached process was created before the guard"
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_actual_node_detached_child_preload_denies_before_config(checkout):
    node = shutil.which("node")
    assert node
    guard = checkout / runner.COMMON / "unit-network-guard.cjs"
    env = runner.environment(checkout, "0" * 32)
    env["NODE_OPTIONS"] = "--require=" + json.dumps(str(guard))
    program = (
        "const cp=require('node:child_process'),fs=require('node:fs');"
        "const p=cp.spawn(process.execPath,['-e','setTimeout(()=>{},10000)'],"
        "{detached:true,stdio:'ignore'});"
        "fs.writeFileSync('ESCAPED_CHILD.pid',String(p.pid));p.unref();"
    )
    pid_file = checkout / "ESCAPED_CHILD.pid"
    try:
        result = subprocess.run(
            [node, "-e", program], cwd=checkout, env=env, capture_output=True, text=True, timeout=5
        )
        assert result.returncode != 0 and "OPENTOUR_DETACHED_CHILD_DENIED" in result.stderr
        assert not pid_file.exists()
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


@pytest.mark.parametrize("location", ["conftest", "test_body"])
def test_ordinary_nested_python_helper_inherits_detachment_guard(checkout, location):
    pid_file = checkout / "ESCAPED_CHILD.pid"
    write(
        checkout,
        "tests/helper.py",
        "import pathlib,subprocess,sys\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(10)'],"
        "start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL)\n"
        "pathlib.Path('ESCAPED_CHILD.pid').write_text(str(p.pid))\n",
    )
    program = (
        "import subprocess,sys\nsubprocess.run([sys.executable,'tests/helper.py'],check=True)\n"
    )
    target = "tests/contract/test_one.py"
    if location == "conftest":
        write(checkout, "tests/conftest.py", program)
    else:
        write(
            checkout,
            target,
            "def test_helper():\n"
            + "\n".join("    " + line for line in program.splitlines())
            + "\n",
        )
    try:
        result = runner.run_command("test-python", [target], checkout, local_pytest)
        assert result["exit_code"] == (2 if location == "conftest" else 1)
        assert not pid_file.exists()
    finally:
        if pid_file.exists():
            try:
                os.kill(int(pid_file.read_text()), signal.SIGKILL)
            except ProcessLookupError:
                pass


def test_ordinary_nested_python_helper_inherits_network_guard(checkout):
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as receiver:
        receiver.bind(("127.0.0.1", 0))
        receiver.settimeout(0.1)
        port = receiver.getsockname()[1]
        write(
            checkout,
            "tests/helper.py",
            "import socket\nsocket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto("
            f"b'sentinel-helper',('127.0.0.1',{port}))\n",
        )
        target = "tests/contract/test_helper.py"
        write(
            checkout,
            target,
            "import subprocess,sys\ndef test_helper():\n"
            "    subprocess.run([sys.executable,'tests/helper.py'],check=True)\n",
        )
        result = runner.run_command("test-python", [target], checkout, local_pytest)
        assert result["exit_code"] == 1
        with pytest.raises(TimeoutError):
            receiver.recvfrom(1024)


def test_startup_guard_installation_failure_stops_before_test_code(checkout):
    guard = checkout / runner.COMMON / "python-unit/sitecustomize.py"
    guard.write_text(
        guard.read_text().replace(
            "def install() -> None:\n",
            "def install() -> None:\n    raise RuntimeError('sentinel-private-startup')\n",
        )
    )
    target = "tests/contract/test_marker.py"
    write(
        checkout,
        target,
        "from pathlib import Path\nPath('UNGUARDED_EXECUTED').touch()\n"
        "def test_one(): assert True\n",
    )
    result = runner.run_command("test-python", [target], checkout, local_pytest)
    assert result["exit_code"] == 2 and result["steps"][0]["raw_exit_code"] == 2
    assert not (checkout / "UNGUARDED_EXECUTED").exists()
    assert "sentinel-private-startup" not in json.dumps(result)


@pytest.fixture
def local_consumer(checkout):
    """Controlled transport only; actual C03 owns/qualifies its full observation schema."""
    write(checkout, "tools/fixtures/local_harness.py")
    write(checkout, "tests/integration/test_failure_harness.py", "def test_owned(): pass\n")
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "urn:opentour:c03-observations:1.0",
        "type": "object",
        "additionalProperties": False,
        "required": ["issue_key", "fixture_version", "product_acceptance", "observations"],
        "properties": {
            "issue_key": {"const": "OPE-124"},
            "fixture_version": {"const": "1"},
            "product_acceptance": {"const": "NOT_RUN"},
            "observations": {
                "type": "array",
                "maxItems": 128,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "mode", "product_acceptance"],
                    "properties": {
                        "kind": {"enum": ["simulated", "local_integration"]},
                        "mode": {"enum": list(runner.MODES)},
                        "product_acceptance": {"const": "NOT_RUN"},
                    },
                },
            },
        },
    }
    path = write(checkout, "tools/fixtures/observation.schema.json", json.dumps(schema))
    binding = checkout / runner.COMMON / "local-observations-binding.json"
    value = json.loads(binding.read_text())
    value["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    binding.write_text(json.dumps(value))
    return checkout


def test_local_profile_dispatch_binds_kind_modes_and_retains_owned_sidecar(local_consumer):
    fixture = FixtureRunner()
    result = runner.run_command(
        "test-integration", ["local", "harness-smoke"], local_consumer, fixture
    )
    assert result["exit_code"] == 0 and result["kind"] == "local_integration"
    invocation = fixture.calls[0]
    assert (
        invocation.consumer == "local_harness" and invocation.expected_kind == "local_integration"
    )
    assert invocation.argv[-4:-2] == ("tools.fixtures.local_harness", "harness-smoke")
    assert invocation.argv[-2:] == ("--result-file", str(invocation.report))
    assert "OPENTOUR_UNIT_GUARD" not in invocation.environment
    artifact = result["steps"][0]["artifacts"][0]
    assert artifact["sha256"] and artifact["content"]["product_acceptance"] == "NOT_RUN"
    assert {t["mode"] for t in result["steps"][0]["tests"]["tests"]} == set(runner.MODES)
    assert not invocation.report.exists(), "Private report cleanup must not lose retained content"


def test_local_missing_postgres_preserves_explicit_blocked_three(local_consumer):
    fixture = FixtureRunner(
        None,
        3,
        lambda r, i: r.update(status="BLOCKED", reason_code="MISSING_EXTERNAL_PREREQUISITE"),
    )
    result = runner.run_command(
        "test-integration", ["local", "harness-smoke"], local_consumer, fixture
    )
    assert result["exit_code"] == 3 and result["status"] == "BLOCKED"
    assert result["steps"][0]["raw_exit_code"] == 3
    assert result["product_acceptance"] == "NOT_RUN"


def test_local_mode_inapplicable_lifecycle_probe_preserves_both_mode_evidence(local_consumer):
    def add_probe(report, invocation):
        report["tests"].append(
            {"id": "cleanup-probe", "file": invocation.target, "status": "passed", "mode": None}
        )
        report["collected"] += 1
        report["passed"] += 1

    result = runner.run_command(
        "test-integration",
        ["local", "harness-smoke"],
        local_consumer,
        FixtureRunner(mutate=add_probe),
    )
    assert result["exit_code"] == 0


@pytest.mark.parametrize("gate", ["OPS-02", "OPS-03"])
@pytest.mark.parametrize("mode", runner.MODES)
def test_c03_external_ops_gates_remain_registered_blocked(checkout, gate, mode):
    fixture = FixtureRunner()
    result = runner.run_command("test-isolation", [gate, mode], checkout, fixture)
    assert result["exit_code"] == 3 and result["status"] == "BLOCKED"
    assert not fixture.calls


@pytest.mark.parametrize("state,raw,expected", [("failed", 1, 1), ("skipped", 0, 1), (None, 2, 2)])
def test_local_assertion_skip_and_zero_never_pass(local_consumer, state, raw, expected):
    result = runner.run_command(
        "test-integration", ["local", "harness-smoke"], local_consumer, FixtureRunner(state, raw)
    )
    assert result["exit_code"] == expected


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r, i: r.update(kind="provider_integration"),
        lambda r, i: r["tests"][1].update(mode="customer_controlled"),
        lambda r, i: i.report.with_name(i.report.name + ".observations.json").unlink(),
        lambda r, i: i.report.with_name(i.report.name + ".observations.json").write_text(
            '{"private":"sentinel"}'
        ),
        lambda r, i: (i.cwd / "tools/fixtures/observation.schema.json").write_text("{}"),
    ],
)
def test_local_wrong_kind_mode_or_unbound_sidecar_fails(local_consumer, mutation):
    result = runner.run_command(
        "test-integration",
        ["local", "harness-smoke"],
        local_consumer,
        FixtureRunner(mutate=mutation),
    )
    assert result["exit_code"] == 2 and result["status"] == "FAIL"


def test_local_profile_rejects_filter_and_absent_adapter_before_execution(checkout):
    fixture = FixtureRunner()
    for option in ("all", "harness-smoke;touch sentinel", "harness-smoke"):
        assert (
            runner.run_command("test-integration", ["local", option], checkout, fixture)[
                "exit_code"
            ]
            == 2
        )
    assert not fixture.calls


@pytest.fixture
def traceability_checkout(checkout, monkeypatch):
    """Controlled C05 transport fixtures; the owning validator proves actual metadata checks."""
    directory = checkout / "docs/specification"
    directory.mkdir(parents=True)
    shutil.copyfile(
        runner.ROOT / "docs/specification/traceability-report.schema.json",
        directory / "traceability-report.schema.json",
    )
    write(checkout, runner.TRACEABILITY_TARGET, "{}\n")
    write(checkout, "tools/validate_spec.py", "# Controlled protocol fixture only.\n")
    monkeypatch.setattr(runner, "source_identity", lambda root: ("a" * 40, False))
    return checkout


class TraceabilityRunner:
    def __init__(self, code=0, mutate=None, artifacts=None):
        self.code = code
        self.mutate = mutate
        self.artifacts = artifacts
        self.calls = []

    def __call__(self, invocation):
        self.calls.append(invocation)
        reason = {
            0: None,
            1: "TRACEABILITY_FAILED",
            2: "INVALID_CONFIGURATION",
            3: "MISSING_EXTERNAL_PREREQUISITE",
        }[self.code]
        report = {
            "schema_version": "opentour.traceability-report.v1",
            "runner": "semantic_validator",
            "target": runner.TRACEABILITY_TARGET,
            "binding_nonce": invocation.environment["OPENTOUR_RESULT_NONCE"],
            "commit_sha": "a" * 40,
            "spec_sha256": hashlib.sha256(
                (invocation.cwd / runner.TRACEABILITY_TARGET).read_bytes()
            ).hexdigest(),
            "kind": "simulated",
            "scope": "bootstrap_metadata_conformance",
            "product_acceptance": "NOT_RUN",
            "checks": [
                {
                    "id": "controlled-check",
                    "owner": "OPE-126",
                    "status": "FAIL" if self.code else "PASS",
                    "code": reason or "COMPLETE",
                    "subject": "generated-sentinel",
                }
            ],
            "collected": 1,
            "passed": int(self.code == 0),
            "failed": int(self.code != 0),
            "status": "PASS" if self.code == 0 else "BLOCKED" if self.code == 3 else "FAIL",
            "exit_code": self.code,
            "reason_code": reason,
            "cleanup_complete": True,
        }
        if self.mutate:
            self.mutate(report, invocation)
        invocation.report.write_text(json.dumps(report))
        findings = invocation.report.with_name("findings.txt")
        findings.write_text("Controlled fixture findings; owner OPE-126.\n")
        if self.artifacts:
            self.artifacts(invocation.report, findings)
        return runner.ProcessResult(self.code)


@pytest.mark.parametrize("code", [0, 1, 2, 3])
def test_traceability_consumer_preserves_semantic_outcomes_and_artifacts(
    traceability_checkout, code
):
    fixture = TraceabilityRunner(code)
    result = runner.run_command("validate-spec", [], traceability_checkout, fixture)
    assert result["exit_code"] == code, result
    invocation = fixture.calls[0]
    assert invocation.runner == "semantic_validator"
    assert invocation.argv[:8] == (
        *runner.uv_python(traceability_checkout),
        "-m",
        "tools.validate_spec",
    )
    assert invocation.environment["OPENTOUR_UNIT_GUARD"] == "1"
    step = result["steps"][0]
    assert step["tests"] is None
    assert step["validation"]["collected"] == 1
    assert "checks" not in step["validation"]
    artifacts = step["artifacts"]
    assert artifacts[0]["content"]["product_acceptance"] == "NOT_RUN"
    assert (
        hashlib.sha256(json.dumps(artifacts[0]["content"]).encode()).hexdigest()
        == artifacts[0]["sha256"]
    )
    assert hashlib.sha256(artifacts[1]["content"].encode()).hexdigest() == artifacts[1]["sha256"]
    assert not invocation.report.parent.exists()
    assert result["product_acceptance"] == "NOT_RUN"


@pytest.mark.parametrize(
    "mutation",
    [
        lambda r, i: r.update(binding_nonce="b" * 32),
        lambda r, i: r.update(commit_sha="b" * 40),
        lambda r, i: r.update(spec_sha256="b" * 64),
        lambda r, i: r.update(target="other.json"),
        lambda r, i: r.update(passed=2),
        lambda r, i: r.update(checks=[], collected=0, passed=0),
        lambda r, i: r.update(checks=r["checks"] * 2, collected=2, passed=2),
        lambda r, i: r.update(exit_code=1),
        lambda r, i: r.update(status="FAIL"),
        lambda r, i: r.update(product_acceptance="PASS"),
        lambda r, i: r.update(kind="provider_integration"),
        lambda r, i: r.update(unregistered="field"),
        lambda r, i: (i.cwd / runner.TRACEABILITY_TARGET).write_text('{"changed":true}'),
    ],
)
def test_traceability_report_identity_counts_and_scope_fail_closed(traceability_checkout, mutation):
    result = runner.run_command(
        "validate-spec", [], traceability_checkout, TraceabilityRunner(mutate=mutation)
    )
    assert result["exit_code"] == 2, result


def test_traceability_blocked_cannot_hide_a_semantic_failure(traceability_checkout):
    def semantic_failure(report, invocation):
        report["checks"][0]["code"] = "MISSING_ASSERTION"

    result = runner.run_command(
        "validate-spec", [], traceability_checkout, TraceabilityRunner(3, semantic_failure)
    )
    assert result["exit_code"] == 2


def test_traceability_incomplete_cleanup_cannot_pass(traceability_checkout):
    result = runner.run_command(
        "validate-spec",
        [],
        traceability_checkout,
        TraceabilityRunner(mutate=lambda r, i: r.update(cleanup_complete=False)),
    )
    assert result["exit_code"] == 1
    assert result["reason_code"] == "CLEANUP_FAILED"


@pytest.mark.parametrize("artifact", ["machine", "readable"])
@pytest.mark.parametrize("defect", ["missing", "empty", "oversize", "symlink", "invalid"])
def test_traceability_artifacts_must_be_fresh_bounded_valid_files(
    traceability_checkout, artifact, defect
):
    def damage(machine, readable):
        target = machine if artifact == "machine" else readable
        if defect == "missing":
            target.unlink()
        elif defect == "empty":
            target.write_bytes(b"")
        elif defect == "oversize":
            target.write_bytes(b" " * (4_194_305 if artifact == "machine" else 1_048_577))
        elif defect == "symlink":
            target.unlink()
            target.symlink_to(traceability_checkout / runner.TRACEABILITY_TARGET)
        else:
            target.write_bytes(b"\xff")

    result = runner.run_command(
        "validate-spec", [], traceability_checkout, TraceabilityRunner(artifacts=damage)
    )
    assert result["exit_code"] == 2


def test_traceability_binding_is_verified_before_child_execution(traceability_checkout):
    write(traceability_checkout, "docs/specification/traceability-report.schema.json", "[]")
    fixture = TraceabilityRunner()
    result = runner.run_command("validate-spec", [], traceability_checkout, fixture)
    assert result["exit_code"] == 2
    assert fixture.calls == []


def test_traceability_interrupted_child_is_execution_failure(traceability_checkout):
    result = runner.run_command(
        "validate-spec", [], traceability_checkout, lambda invocation: runner.ProcessResult(-9)
    )
    assert result["exit_code"] == 1
    assert result["reason_code"] == "RUNNER_INTERRUPTED"


def test_traceability_source_commit_change_is_rejected(traceability_checkout, monkeypatch):
    calls = 0

    def identity(root):
        nonlocal calls
        calls += 1
        return ("a" if calls <= 2 else "b") * 40, False

    monkeypatch.setattr(runner, "source_identity", identity)
    result = runner.run_command("validate-spec", [], traceability_checkout, TraceabilityRunner())
    assert result["exit_code"] == 2


def test_traceability_passing_status_cannot_hide_failed_checks(traceability_checkout):
    def false_pass(report, invocation):
        report["checks"][0]["status"] = "FAIL"
        report.update(passed=0, failed=1)

    result = runner.run_command(
        "validate-spec", [], traceability_checkout, TraceabilityRunner(mutate=false_pass)
    )
    assert result["exit_code"] == 2


@pytest.mark.parametrize("code", [0, 1, 2])
def test_actual_semantic_transport_with_controlled_child(traceability_checkout, code):
    """Actual CLI transport/startup guard only; these generated checks are not C05 acceptance."""
    root = traceability_checkout
    shutil.copyfile(runner.ROOT / "uv.lock", root / "uv.lock")
    (root / ".venv").symlink_to((runner.ROOT / ".venv").resolve(), target_is_directory=True)

    def actual(invocation):
        TraceabilityRunner(code)(invocation)
        template = invocation.report.read_text()
        invocation.report.unlink()
        invocation.report.with_name("findings.txt").unlink()
        write(
            root,
            "tools/validate_spec.py",
            "import argparse,json,os,subprocess\nfrom pathlib import Path\n"
            "assert subprocess._opentour_unit_guard_pid == os.getpid()\n"
            "p=argparse.ArgumentParser()\n"
            "p.add_argument('--result-file',required=True)\n"
            "p.add_argument('--findings-file',required=True)\n"
            "a=p.parse_args()\n"
            "assert a.result_file == os.environ['OPENTOUR_RESULT_FILE']\n"
            "assert Path.cwd() == Path(os.environ['OPENTOUR_REPOSITORY_ROOT'])\n"
            f"report=json.loads({template!r})\n"
            "report['binding_nonce']=os.environ['OPENTOUR_RESULT_NONCE']\n"
            "report['target']=os.environ['OPENTOUR_TEST_TARGET']\n"
            "Path(a.result_file).write_text(json.dumps(report))\n"
            "Path(a.findings_file).write_text('Generated controlled check: OPE-126.\\n')\n"
            f"raise SystemExit({code})\n",
        )
        return runner.execute(replace(invocation, timeout=20))

    result = runner.run_command("validate-spec", [], root, actual)
    assert result["exit_code"] == code, result
    assert result["steps"][0]["process_cleanup_complete"] is True
    assert result["steps"][0]["validation"]["collected"] == 1
