"""Adverse workflow mutations prove policy, not deployed cloud isolation."""

import copy
import io
import json
import sys
import tarfile
from pathlib import Path
from typing import Any

import pytest

from tools.ci.browser_evidence import retain_browser_evidence
from tools.ci.policy import CHECKS, check, workflow_errors
from tools.ci.prepare import extract
from tools.ci.run import python_unit_selection, run_browser, run_one, stage_report
from tools.public_content_check import digest

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
    assert len(absent) == 4
    (tmp_path / "services/control-api/tests").mkdir(parents=True)
    targets, absent = python_unit_selection(tmp_path)
    # Even an empty present suite is dispatched: the owning wrapper must reject zero tests.
    assert "services/control-api/tests" in targets
    assert "services/control-api/tests" not in absent and len(absent) == 3


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
