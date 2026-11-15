"""Adverse workflow mutations prove policy, not deployed cloud isolation."""

import copy
import io
import json
import sys
import tarfile
from pathlib import Path

import pytest

from tools.ci.policy import CHECKS, check, workflow_errors
from tools.ci.prepare import extract
from tools.ci.run import run_one, stage_report

ROOT = Path(__file__).resolve().parents[2]


def workflow():
    return json.loads((ROOT / ".github/workflows/ci.yml").read_text())


def test_all_unique_required_jobs_execute_and_cannot_filter():
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
def test_untrusted_or_incomplete_workflow_fails(mutation):
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


def test_archive_traversal_cannot_write_outside_destination(tmp_path):
    archive = tmp_path / "source.tar"
    with tarfile.open(archive, "w") as file:
        item = tarfile.TarInfo("../../escape")
        item.size = 1
        file.addfile(item, io.BytesIO(b"x"))
    with pytest.raises(tarfile.TarError):
        extract(archive, tmp_path / "destination")
    assert not (tmp_path.parent / "escape").exists()


def policy_root(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github/public-content-policy.json").write_bytes(
        (ROOT / ".github/public-content-policy.json").read_bytes()
    )
    return tmp_path


def test_real_nonzero_child_cannot_be_accepted(tmp_path):
    root = policy_root(tmp_path)
    result = run_one([sys.executable, "-c", "print('generated output'); raise SystemExit(7)"], root)
    assert result["status"] == "FAIL" and result["exit_code"] == 7
    assert result["process_cleanup_complete"] is True


def test_real_planted_log_credential_is_withheld(tmp_path):
    root = policy_root(tmp_path)
    script = root / "generated_probe.py"
    script.write_text("print('Bearer ' + 'SENTINEL' * 5)")
    result = run_one([sys.executable, str(script)], root)
    assert result["status"] == "FAIL" and result["disclosure_findings"]
    assert "SENTINEL" not in json.dumps(result)
    assert "command_evidence" not in result


def test_missing_structured_command_result_cannot_pass(monkeypatch, tmp_path):
    from tools import test_runner
    from tools.ci import run as ci_run

    root = policy_root(tmp_path)
    monkeypatch.setattr(ci_run, "execute", lambda _: test_runner.ProcessResult(0, "not a report"))
    assert run_one(["just", "lint"], root)["status"] == "FAIL"


def test_exact_screened_report_and_fresh_publication_only(tmp_path):
    root = policy_root(tmp_path)
    report = {"status": "PASS", "fixture": "generated", "product_acceptance": "NOT_RUN"}
    file = stage_report(root, "lint", report)
    assert json.loads(file.read_bytes()) == report
    with pytest.raises(ValueError):
        stage_report(root, "typecheck", report)


def test_publication_refuses_secret_and_directory_link(tmp_path):
    root = policy_root(tmp_path)
    with pytest.raises(ValueError):
        stage_report(root, "lint", {"text": "Bearer " + "SENTINEL" * 5})
    assert not (root / "evidence").exists()
    (root / "evidence").symlink_to(tmp_path.parent, target_is_directory=True)
    with pytest.raises(ValueError):
        stage_report(root, "lint", {"status": "FAIL"})
