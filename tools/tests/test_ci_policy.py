"""Adverse workflow mutations prove policy, not deployed cloud isolation."""

import copy
import io
import json
import subprocess
import sys
import tarfile
import urllib.error
import urllib.request
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from typing import Any

import pytest

from tools.ci import prepare
from tools.ci.browser_evidence import retain_browser_evidence
from tools.ci.policy import CHECKS, check, workflow_errors
from tools.ci.prepare import download, extract
from tools.ci.run import (
    python_unit_selection,
    run_browser,
    run_one,
    stage_report,
    web_unit_selection,
)
from tools.public_content_check import digest
from tools.test_runner import CommandError

ROOT = Path(__file__).resolve().parents[2]


def workflow() -> dict[str, Any]:
    value: dict[str, Any] = json.loads((ROOT / ".github/workflows/ci.yml").read_text())
    return value


def test_all_unique_required_jobs_execute_and_cannot_filter() -> None:
    data = workflow()
    assert set(data["jobs"]) == set(CHECKS)
    assert check(ROOT)["status"] == "PASS"


@pytest.mark.parametrize(
    "mutation",
    [
        "write",
        "oidc",
        "target",
        "filter",
        "skip",
        "ignore",
        "floating",
        "interpolation",
        "secret",
        "upload",
    ],
)
def test_untrusted_or_incomplete_workflow_fails(mutation: str) -> None:
    data = copy.deepcopy(workflow())
    job = data["jobs"]["lint"]
    if mutation == "write":
        job["permissions"] = {"contents": "write"}
    elif mutation == "oidc":
        job["permissions"]["id-token"] = "write"
    elif mutation == "target":
        data["on"]["pull_request_target"] = {}
    elif mutation == "filter":
        data["on"]["pull_request"] = {"paths": ["apps/**"]}
    elif mutation == "skip":
        job["if"] = "false"
    elif mutation == "ignore":
        job["steps"][2]["continue-on-error"] = True
    elif mutation == "floating":
        job["steps"][0]["uses"] = "actions/checkout@main"
    elif mutation == "interpolation":
        job["steps"][1]["run"] = "echo ${{ github.event.pull_request.title }}"
    elif mutation == "secret":
        job["env"] = {"DEPLOYMENT": "${{ secrets.DEPLOYMENT }}"}
    elif mutation == "upload":
        job["steps"][-1]["with"]["path"] = "**/*"
    assert workflow_errors(data)


ARTIFACT_URL = "https://artifacts.example.invalid/pinned-tool"
ARTIFACT = b"generated pinned prerequisite bytes"


class ArtifactResponse(io.BytesIO):
    def __init__(
        self, data: bytes, url: str = ARTIFACT_URL, *, ending: Exception | None = None
    ) -> None:
        super().__init__(data)
        self.url = url
        self.ending = ending

    def geturl(self) -> str:
        return self.url

    def read(self, size: int | None = -1, /) -> bytes:
        result = super().read(size)
        if not result and self.ending is not None:
            raise self.ending
        return result


def script_artifact_requests(
    monkeypatch: pytest.MonkeyPatch,
    *responses: ArtifactResponse | Exception,
) -> list[tuple[str, int]]:
    pending = iter(responses)
    calls: list[tuple[str, int]] = []

    def urlopen(url: str, *, timeout: int) -> ArtifactResponse:
        calls.append((url, timeout))
        item = next(pending)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)
    return calls


@pytest.mark.parametrize("failure", ["partial_read", "wrapped_timeout", "incomplete", "http_503"])
def test_pinned_download_recovers_from_a_transient_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    response: ArtifactResponse | Exception
    if failure == "partial_read":
        response = ArtifactResponse(b"partial attempt", ending=TimeoutError())
    elif failure == "wrapped_timeout":
        response = urllib.error.URLError(TimeoutError())
    elif failure == "incomplete":
        response = IncompleteRead(b"partial attempt")
    else:
        response = urllib.error.HTTPError(ARTIFACT_URL, 503, "Unavailable", Message(), None)
    dst = tmp_path / "tool"
    calls = script_artifact_requests(monkeypatch, response, ArtifactResponse(ARTIFACT))
    download(ARTIFACT_URL, digest(ARTIFACT), dst)
    assert calls == [(ARTIFACT_URL, 60)] * 2
    assert dst.read_bytes() == ARTIFACT
    assert list(tmp_path.iterdir()) == [dst]


def test_pinned_download_exhausts_attempts_without_publishing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = script_artifact_requests(
        monkeypatch,
        *(ArtifactResponse(b"partial attempt", ending=TimeoutError()) for _ in range(3)),
    )
    with pytest.raises(TimeoutError):
        download(ARTIFACT_URL, digest(ARTIFACT), tmp_path / "tool")
    assert calls == [(ARTIFACT_URL, 60)] * 3
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("failure", ["digest", "redirect", "size", "http_404"])
def test_pinned_download_refuses_terminal_errors_without_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    response: ArtifactResponse | Exception
    if failure == "digest":
        response = ArtifactResponse(b"unexpected artifact bytes")
    elif failure == "redirect":
        response = ArtifactResponse(ARTIFACT, "http://artifacts.example.invalid/pinned-tool")
    elif failure == "size":
        monkeypatch.setattr(prepare, "MAX_DOWNLOAD", len(ARTIFACT) - 1)
        response = ArtifactResponse(ARTIFACT)
    else:
        response = urllib.error.HTTPError(ARTIFACT_URL, 404, "Missing", Message(), None)
    calls = script_artifact_requests(monkeypatch, response)
    error = urllib.error.HTTPError if failure == "http_404" else ValueError
    with pytest.raises(error):
        download(ARTIFACT_URL, digest(ARTIFACT), tmp_path / "tool")
    assert calls == [(ARTIFACT_URL, 60)]
    assert not list(tmp_path.iterdir())


def test_pinned_download_cannot_overwrite_existing_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dst = tmp_path / "tool"
    dst.write_bytes(b"existing destination")
    calls = script_artifact_requests(monkeypatch)
    with pytest.raises(FileExistsError):
        download(ARTIFACT_URL, digest(ARTIFACT), dst)
    assert not calls and dst.read_bytes() == b"existing destination"


def test_pinned_download_never_exposes_partial_or_replaces_concurrent_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dst = tmp_path / "tool"

    class ConcurrentResponse(ArtifactResponse):
        def read(self, size: int | None = -1, /) -> bytes:
            data = super().read(size)
            if data:
                assert not dst.exists()
            else:
                dst.write_bytes(b"concurrently installed")
            return data

    calls = script_artifact_requests(monkeypatch, ConcurrentResponse(ARTIFACT))
    with pytest.raises(FileExistsError):
        download(ARTIFACT_URL, digest(ARTIFACT), dst)
    assert calls == [(ARTIFACT_URL, 60)]
    assert dst.read_bytes() == b"concurrently installed"
    assert list(tmp_path.iterdir()) == [dst]


def test_archive_traversal_cannot_write_outside_destination(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as file:
        item = tarfile.TarInfo("../../escape")
        item.size = 1
        file.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(tarfile.TarError):
        extract(archive, tmp_path / "destination")
    assert not (tmp_path.parent / "escape").exists()


@pytest.mark.parametrize("step", [0, 1, 2])
def test_required_steps_cannot_be_conditionally_skipped(step: int) -> None:
    value = workflow()
    value["jobs"]["lint"]["steps"][step]["if"] = "${{ false }}"
    assert workflow_errors(value)


@pytest.mark.parametrize("expression", ["${{ secrets['DEPLOYMENT'] }}", "${{ toJSON(secrets) }}"])
def test_every_secrets_context_form_is_refused(expression: str) -> None:
    value = workflow()
    value["jobs"]["lint"]["env"] = {"DEPLOYMENT": expression}
    assert workflow_errors(value)


def test_unscreened_output_cannot_trigger_artifact_upload() -> None:
    value = workflow()
    value["jobs"]["lint"]["steps"][-1]["if"] = "${{ always() }}"
    assert workflow_errors(value)


def policy_root(tmp_path: Path) -> Path:
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/public-content-policy.json").write_bytes(
        (ROOT / ".github/public-content-policy.json").read_bytes()
    )
    return tmp_path


def test_real_nonzero_child_cannot_be_accepted(tmp_path: Path) -> None:
    root = policy_root(tmp_path)
    result = run_one([sys.executable, "-c", "print('generated output'); raise SystemExit(7)"], root)
    assert result["status"] == "FAIL" and result["exit_code"] == 7
    assert result["process_cleanup_complete"] is True


def test_actual_quiet_recipe_preserves_structured_receipt(tmp_path: Path) -> None:
    root = policy_root(tmp_path)
    recipe = root / "justfile"
    proof = root / "proof.py"
    proof.write_text("import json\nprint(json.dumps(dict(status='PASS', exit_code=0)))\n")
    recipe.write_text("proof:\n    '" + sys.executable + "' '" + str(proof) + "'\n")
    noisy = run_one(["just", "--justfile", str(recipe), "proof"], root)
    recipe.write_text("proof:\n    @'" + sys.executable + "' '" + str(proof) + "'\n")
    quiet = run_one(["just", "--justfile", str(recipe), "proof"], root)
    assert noisy["status"] == "FAIL"
    assert quiet["status"] == "PASS" and quiet["command_evidence"]["exit_code"] == 0


def test_actual_just_failure_retains_structured_evidence_without_passing(tmp_path: Path) -> None:
    root = policy_root(tmp_path)
    proof = root / "proof.py"
    proof.write_text("import json\nprint(json.dumps(dict(status='FAIL', exit_code=1)))\nexit(1)\n")
    recipe = root / "justfile"
    recipe.write_text("proof:\n    @'" + sys.executable + "' '" + str(proof) + "'\n")
    result = run_one(["just", "--justfile", str(recipe), "proof"], root)
    assert result["status"] == "FAIL" and result["exit_code"] != 0
    assert result["command_evidence"] == {"status": "FAIL", "exit_code": 1}
    assert result["failure_diagnostic_sha256"] != digest(b"")
    proof.write_text(
        "import json\nprint(json.dumps(dict(status='PASS', exit_code=0)))\nprint('noise')"
    )
    assert run_one(["just", "--justfile", str(recipe), "proof"], root)["status"] == "FAIL"


def test_python_units_run_bootstrap_and_every_present_product_suite(tmp_path: Path) -> None:
    targets, absent = python_unit_selection(tmp_path)
    assert "tests/contract/test_bootstrap.py" in targets and "tools/tests" in targets
    assert len(absent) == 5
    (tmp_path / "services/control-api/tests").mkdir(parents=True)
    targets, absent = python_unit_selection(tmp_path)
    # Even an empty present suite is dispatched: the owning wrapper must reject zero tests.
    assert "services/control-api/tests" in targets
    assert "services/control-api/tests" not in absent and len(absent) == 4


def test_numerical_kernel_routes_both_required_test_files_once(tmp_path: Path) -> None:
    kernel = "training/src/opentour_training/evaluation/statistics_kernel.py"
    plan_tests = "training/tests/evaluation/test_inference_plan.py"
    kernel_tests = "training/tests/evaluation/test_statistics_kernel.py"
    other_tests = "training/tests/evaluation/test_other.py"
    for relative in (kernel, plan_tests, kernel_tests, other_tests):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Generated routing fixture\n")

    selected, absent = python_unit_selection(tmp_path)

    assert [path for path in selected if path.startswith("training/")] == [
        plan_tests,
        kernel_tests,
        other_tests,
    ]
    assert "training/tests" not in absent

    (tmp_path / kernel_tests).unlink()
    with pytest.raises(CommandError, match="does not exist"):
        python_unit_selection(tmp_path)


def test_only_registered_database_files_leave_the_unit_job(tmp_path: Path) -> None:
    from tools.fixtures.python_postgres import present_targets

    files = {
        "services/control-api/tests/auth/test_identity_postgres.py",
        "services/control-api/tests/migrations/test_expand_contract.py",
        "services/control-api/tests/auth/test_session_context.py",
        "services/control-api/tests/auth/refresh_test.py",
        "services/control-api/tests/auth/test_unregistered_database.py",
        "services/control-api/tests/auth/helpers.py",
        "infrastructure/tests/test_environment_boundaries.py",
    }
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Generated selection fixture\n")
    units, absent = python_unit_selection(tmp_path)
    local = set(present_targets(tmp_path))
    assert local == {
        "services/control-api/tests/auth/test_identity_postgres.py",
        "services/control-api/tests/migrations/test_expand_contract.py",
    }
    assert not (set(units) & local)
    assert {
        "services/control-api/tests/auth/test_session_context.py",
        "services/control-api/tests/auth/refresh_test.py",
        "services/control-api/tests/auth/test_unregistered_database.py",
        "infrastructure/tests",
    } <= set(units)
    assert "services/control-api/tests/auth/helpers.py" not in units
    assert "services/control-api/tests" not in units + absent


@pytest.mark.parametrize("broken", [False, True])
def test_partition_cannot_hide_a_symlink(tmp_path: Path, broken: bool) -> None:
    root = tmp_path / "services/control-api/tests/auth"
    root.mkdir(parents=True)
    (root / "test_identity_postgres.py").write_text("# Generated local target\n")
    link = root / "test_aliased.py"
    link.symlink_to(root / ("absent.py" if broken else "test_identity_postgres.py"))
    with pytest.raises(CommandError, match="symlink"):
        python_unit_selection(tmp_path)


def test_local_only_product_root_is_not_claimed_as_unit_execution(tmp_path: Path) -> None:
    from tools.fixtures.python_postgres import present_targets

    relative = "services/control-api/tests/auth/test_identity_postgres.py"
    path = tmp_path / relative
    path.parent.mkdir(parents=True)
    path.write_text("# Generated local target\n")
    units, absent = python_unit_selection(tmp_path)
    assert present_targets(tmp_path) == (relative,)
    assert not any(p.startswith("services/control-api/tests") for p in units + absent)


def test_web_job_discovers_colocated_tracked_tests_and_excludes_browser_specs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tools.ci import run as ci_run

    files = [
        "packages/contracts/examples/web-command-smoke.test.ts",
        "apps/web/lib/server/authkit-adapter.test.ts",
        "apps/web/components/context.test.tsx",
        "apps/web/tests/browser-harness.browser.spec.ts",
    ]
    for relative in files:
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("// Generated selection fixture\n")
    monkeypatch.setattr(ci_run, "tracked_paths", lambda _: files)
    assert web_unit_selection(tmp_path) == sorted(files[:3])
    monkeypatch.setattr(ci_run, "tracked_paths", lambda _: [])
    with pytest.raises(CommandError, match="No tracked"):
        web_unit_selection(tmp_path)


def test_web_job_refuses_missing_or_linked_tracked_tests(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tools.ci import run as ci_run

    relative = "missing.test.ts"
    monkeypatch.setattr(ci_run, "tracked_paths", lambda _: [relative])
    with pytest.raises(CommandError):
        web_unit_selection(tmp_path)
    (tmp_path / "target.test.ts").write_text("// Generated fixture\n")
    (tmp_path / relative).symlink_to(tmp_path / "target.test.ts")
    with pytest.raises(CommandError, match="symlink"):
        web_unit_selection(tmp_path)


@pytest.mark.parametrize("job", ["python-unit", "local-integration", "web-unit"])
@pytest.mark.parametrize(
    "local",
    [
        "services/control-api/tests/auth/test_identity_postgres.py",
        "tests/integration/authority_journal_recovery.py",
    ],
)
def test_required_jobs_dispatch_every_partition_in_its_own_evidence_class(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, job: str, local: str
) -> None:
    from tools.ci import run as ci_run

    local_command = (
        ["just", "test-integration", "local", "authority_journal_recovery"]
        if local == "tests/integration/authority_journal_recovery.py"
        else ["just", "test-python", local]
    )
    unit = "services/control-api/tests/auth/test_session_context.py"
    web = "apps/web/lib/server/authkit-adapter.test.ts"
    for target in (local, unit, web):
        path = tmp_path / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# Generated dispatch fixture\n")
    monkeypatch.setattr(ci_run, "tracked_paths", lambda _: [local, unit, web])
    monkeypatch.setattr(ci_run, "load_policy", lambda _: {})
    monkeypatch.setattr(
        ci_run, "scan_paths", lambda *_: {"status": "PASS", "covered_source_sha256": "a" * 64}
    )
    monkeypatch.setattr(
        subprocess,
        "check_output",
        lambda argv, **_: "b" * 40 if "rev-parse" in argv else b"",
    )
    monkeypatch.setattr(ci_run, "postgres", lambda _: "/generated/postgres")
    calls: list[list[str]] = []

    def simulated_command(argv: list[str], *_: Any, **__: Any) -> dict[str, Any]:
        calls.append(argv)
        return {
            "command": argv,
            "status": "PASS",
            "exit_code": 0,
            "command_evidence": {
                "kind": "local_integration" if argv == local_command else "simulated"
            },
        }

    monkeypatch.setattr(ci_run, "run_one", simulated_command)
    result = ci_run.run_check(job, tmp_path)
    assert result["status"] == "PASS"
    if job == "python-unit":
        unit_target = (
            "services/control-api/tests"
            if local == "tests/integration/authority_journal_recovery.py"
            else unit
        )
        assert ["just", "test-python", unit_target] in calls
        assert local_command not in calls
        assert result["registered_local_integration_targets"] == [local]
    elif job == "local-integration":
        assert local_command in calls
        assert calls.count(local_command) == 1
        assert ["just", "test-python", unit] not in calls
        assert result["registered_local_integration_targets"] == [local]

        def wrong_kind(argv: list[str], *args: Any, **kwargs: Any) -> dict[str, Any]:
            record = simulated_command(argv, *args, **kwargs)
            record["command_evidence"]["kind"] = "simulated"
            return record

        monkeypatch.setattr(ci_run, "run_one", wrong_kind)
        refused = ci_run.run_check(job, tmp_path)
        assert refused["status"] == "FAIL"
        assert refused["steps"][-1]["status"] == "FAIL"
    else:
        assert ["just", "test-web", web] in calls
        assert ["just", "test-python", local] not in calls


def test_real_planted_log_credential_is_withheld(tmp_path: Path) -> None:
    root = policy_root(tmp_path)
    script = root / "generated_probe.py"
    script.write_text("print('Bearer ' + 'SENTINEL' * 5)")
    result = run_one([sys.executable, str(script)], root)
    assert result["status"] == "FAIL" and result["disclosure_findings"]
    assert "SENTINEL" not in json.dumps(result)
    assert "command_evidence" not in result


def test_missing_structured_command_result_cannot_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tools import test_runner
    from tools.ci import run as ci_run

    root = policy_root(tmp_path)
    monkeypatch.setattr(ci_run, "execute", lambda _: test_runner.ProcessResult(0, "not a report"))
    assert run_one(["just", "lint"], root)["status"] == "FAIL"


def test_only_exact_traceability_command_uses_measured_larger_capture(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tools import test_runner
    from tools.ci import run as ci_run

    seen: list[int] = []

    def capture(invocation: test_runner.Invocation) -> test_runner.ProcessResult:
        seen.append(invocation.output_limit_bytes)
        return test_runner.ProcessResult(0, '{"status":"PASS","exit_code":0}')

    root = policy_root(tmp_path)
    monkeypatch.setattr(ci_run, "execute", capture)
    for command in (
        ["just", "validate-spec"],
        ["just", "lint"],
        ["just", "validate-spec", "extra"],
    ):
        run_one(command, root)
    assert seen == [2_097_152, 1_048_576, 1_048_576]


def test_exact_screened_report_and_fresh_publication_only(tmp_path: Path) -> None:
    root = policy_root(tmp_path)
    report = {"status": "PASS", "fixture": "generated", "product_acceptance": "NOT_RUN"}
    file = stage_report(root, "lint", report)
    assert json.loads(file.read_bytes()) == report
    with pytest.raises(ValueError):
        stage_report(root, "typecheck", report)


def test_publication_refuses_secret_and_directory_link(tmp_path: Path) -> None:
    root = policy_root(tmp_path)
    with pytest.raises(ValueError):
        stage_report(root, "lint", {"text": "Bearer " + "SENTINEL" * 5})
    assert not (root / "evidence").exists()
    (root / "evidence").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        stage_report(root, "lint", {"status": "FAIL"})


def browser_fixture(root: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    nonce = "a" * 32
    relative = "evidence/OT-BOOT-C06/runs/" + nonce
    directory = root / relative
    (directory / "artifacts").mkdir(parents=True)
    target = "apps/web/tests/browser-harness.browser.spec.ts"
    (root / target).parent.mkdir(parents=True)
    (root / target).write_text("// Generated source")
    output = directory / "artifacts/generated.json"
    output.write_text('{"fixture":"generated"}')
    report: dict[str, Any] = {
        "binding_nonce": nonce,
        "status": "PASS",
        "collected": 1,
        "tests": [{"id": "generated-case", "file": target, "status": "passed", "mode": None}],
    }
    manifest = {
        "schema_version": "opentour.browser-artifacts.v1",
        "issue_key": "OPE-127",
        "kind": "simulated",
        "product_acceptance": "NOT_RUN",
        "git_worktree_dirty": False,
        "commit_sha": "b" * 40,
        "report": report,
        "target": target,
        "test_source_sha256": digest((root / target).read_bytes()),
        "baseline_review_request": None,
        "outputs": [
            {"path": relative + "/artifacts/generated.json", "sha256": digest(output.read_bytes())}
        ],
    }
    path = directory / "evidence.json"
    path.write_text(json.dumps(manifest))
    transport = copy.deepcopy(report)
    transport["tests"][0]["id"] = "sha256:" + digest(b"generated-case")
    return (
        path,
        manifest,
        {"commit_sha": "b" * 40, "steps": [{"runner": "playwright", "tests": transport}]},
    )


def test_nonce_bound_browser_inventory_is_retained(tmp_path: Path) -> None:
    root = policy_root(tmp_path)
    path, manifest, command = browser_fixture(root)
    result = retain_browser_evidence(root, command)
    assert result["manifest_sha256"] == digest(path.read_bytes())
    assert result["manifest"] == manifest and result["verified_output_files"] == 1
    assert result["raw_artifacts_uploaded"] is False


def test_actual_runner_log_is_bound_without_allowing_arbitrary_run_files(tmp_path: Path) -> None:
    root = policy_root(tmp_path)
    path, manifest, command = browser_fixture(root)
    log = path.parent / "runner.log"
    log.write_text("Generated runner result\n")
    manifest["outputs"].append(
        {"path": str(log.relative_to(root)), "sha256": digest(log.read_bytes())}
    )
    path.write_text(json.dumps(manifest))
    assert retain_browser_evidence(root, command)["verified_output_files"] == 2
    other = path.parent / "unreviewed.json"
    other.write_text("{}")
    manifest["outputs"].append(
        {"path": str(other.relative_to(root)), "sha256": digest(other.read_bytes())}
    )
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="scope"):
        retain_browser_evidence(root, command)


@pytest.mark.parametrize("mutation", ["nonce", "commit", "digest", "escape", "report", "missing"])
def test_browser_inventory_refuses_stale_missing_or_altered_outputs(
    tmp_path: Path, mutation: str
) -> None:
    root = policy_root(tmp_path)
    path, manifest, command = browser_fixture(root)
    if mutation == "nonce":
        command["steps"][0]["tests"]["binding_nonce"] = "c" * 32
    elif mutation == "commit":
        manifest["commit_sha"] = "c" * 40
    elif mutation == "digest":
        manifest["outputs"][0]["sha256"] = "c" * 64
    elif mutation == "escape":
        manifest["outputs"][0]["path"] = "../outside"
    elif mutation == "report":
        manifest["report"] = {"status": "FAIL"}
    elif mutation == "missing":
        (root / manifest["outputs"][0]["path"]).unlink()
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        retain_browser_evidence(root, command)


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_browser_result_preserves_bound_inventory_even_when_tests_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from tools.ci import run as ci_run

    root = policy_root(tmp_path)
    path, manifest, command = browser_fixture(root)
    manifest["report"]["status"] = status
    command["steps"][0]["tests"]["status"] = status
    path.write_text(json.dumps(manifest))
    monkeypatch.setattr(
        ci_run, "run_one", lambda *a, **kw: {"status": status, "command_evidence": command}
    )
    result = run_browser(root, {})
    assert result["status"] == status
    assert result["browser_artifacts"]["verified_output_files"] == 1
    assert result["browser_artifacts"]["manifest"]["report"]["status"] == status


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
def test_missing_browser_manifest_is_explicit_and_cannot_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    from tools.ci import run as ci_run

    root = policy_root(tmp_path)
    monkeypatch.setattr(ci_run, "run_one", lambda *a, **kw: {"status": status})
    result = run_browser(root, {})
    assert result["status"] == "FAIL"
    assert result["browser_artifacts"]["available"] is False
    assert result["browser_artifacts"]["reason"] == "MISSING_OR_INVALID_BROWSER_EVIDENCE"
