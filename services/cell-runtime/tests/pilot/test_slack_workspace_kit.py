"""Prove clean source builds and owner-local onboarding from the published archive shape."""

from __future__ import annotations

import hashlib
import json
import runpy
import shutil
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZipFile


def verify_operating_agreement(executable, target):
    work = target / "private-agreement"
    work.mkdir(mode=0o700)
    sample = target / "generated/coverage-floor.json"
    score = [*executable, "evaluate"]
    source = subprocess.check_output([*score, "--source-digest", sample], text=True).strip()
    agreement = json.loads((target / "generated/operating-agreement.template.json").read_text())
    now = int(time.time())
    agreement.update(source_digest=source, agreed_at=now, expires_at=now + 1800)
    path = work / "agreement.json"
    path.write_text(json.dumps(agreement))
    path.chmod(0o600)
    bound = [
        *score,
        "--operating-agreement",
        path,
        "--organization",
        agreement["organization"],
        "--engagement",
        agreement["engagement"],
    ]
    for directory, command in (("legacy", score), ("first", bound), ("repeat", bound)):
        subprocess.run([*command, "--report-dir", work / directory, sample], check=True, env={})
    for name in ("brief.md", "aggregate.json", "decision-quality.md", "decision-quality.json"):
        assert (work / "first" / name).read_bytes() == (work / "repeat" / name).read_bytes()
        if name in ("brief.md", "aggregate.json"):
            assert (work / "first" / name).read_bytes() == (work / "legacy" / name).read_bytes()
    quality = json.loads((work / "first" / "decision-quality.json").read_text())
    assert quality["operating_point"]["repair"] == {
        "coverage": "missed",
        "error": "met",
        "combined": "missed",
    }
    private_receipt = json.loads((target / "templates/pilot-receipt.blank.json").read_text())
    assert private_receipt["record_class"] == "unclassified"
    assert not (work / "legacy" / "decision-quality.json").exists()


def test_reproducible_kit_runs_isolated_data_mcp_and_slack_entrypoints(tmp_path):
    root = Path(__file__).resolve().parents[4]
    builder = "tools/build_slack_pilot_kit.py"
    settings = runpy.run_path(str(root / builder))
    paths = {
        builder,
        settings["ARCHIVE_HELPER"],
        *settings["ASSETS"].values(),
        *(f"{settings['CELL']}/src/{name}" for name in settings["MODULES"]),
        *(f"{settings['JOURNAL']}/src/{name}" for name in settings["JOURNAL_MODULES"]),
    }
    repo = tmp_path / "snapshot"
    for path in paths:
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / path, repo / path)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--no-gpg-sign",
            "-qm",
            "Generated packaging input",
        ],
        cwd=repo,
        check=True,
    )
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=repo, text=True).strip()
    python = [sys.executable, "-I", "-S", "-B"]
    first, second = tmp_path / "one.zip", tmp_path / "two.zip"
    for dest in [first, second]:
        subprocess.run([*python, builder, str(dest)], cwd=repo, check=True, capture_output=True)
    assert first.read_bytes() == second.read_bytes()
    target = tmp_path / "extracted"
    with ZipFile(first) as archive:
        archive.extractall(target)
        assert json.loads(archive.read("PROVENANCE.json"))["source_commit"] == revision
        sums = archive.read("SHA256SUMS").decode().splitlines()
        assert len(sums) == len(archive.namelist()) - 1
        for line in sums:
            sha, name = line.split("  ")
            assert sha == hashlib.sha256(archive.read(name)).hexdigest()
    with ZipFile(target / "opentour-slack.pyz") as archive:
        assert "opentour_cell/pilot/recipient_lifecycle.py" in archive.namelist()
        for module in settings["JOURNAL_MODULES"]:
            source = root / settings["JOURNAL"] / "src" / module
            assert archive.read(module) == source.read_bytes()
    executable = [*python, str(target / "opentour-slack.pyz")]

    def run(*args, stdin=b""):
        return subprocess.check_output([*executable, *args], cwd=target, env={}, input=stdin)

    assert revision.encode() in run("--version")
    assert b"usage" in run("slack", "--help").lower()
    assert b"--unresolved-receipts" in run("slack", "--help")
    assert b"withdraw" in run("custody", "--help").lower()
    private_register_options = (b"--register", b"--authority", b"--submission")
    for command in ("qualification", "cohort-lease"):
        help_output = run(command, "--help")
        assert all(option in help_output for option in private_register_options)
    assert (target / "pilot-receipt-recovery.md").is_file()
    verify_operating_agreement(executable, target)
    supplied = "generated/readout-input.json"
    for directory in ("first-readout", "repeat-readout"):
        run("evaluate", "--report-dir", directory, supplied)
    for name in ("aggregate.json", "brief.md"):
        expected_file = (target / "generated/expected-supplied" / name).read_bytes()
        for directory in ("first-readout", "repeat-readout"):
            assert (target / directory / name).read_bytes() == expected_file
    expected = json.loads((target / "generated/expected-training.json").read_text())
    private = tmp_path / "enrollment"
    private.mkdir(mode=0o700)
    args = ["data", "--db", str(private / "demo.sqlite"), "--org", "generated-demo", "rehearse"]
    actual = json.loads(run(*args))
    assert all(actual[key] == value for key, value in expected.items())
    assert actual == json.loads(run(*args))
    assert actual["result"]["changed_weights"] > 0
    data_command = args[:-1]
    trained_repeat = private / "trained-repeat"
    run(*data_command, "score-only", "--job", actual["job_id"], "--output-dir", str(trained_repeat))
    assert (
        json.loads((trained_repeat / "aggregate.json").read_text()) == actual["result"]["readout"]
    )
    assert (
        json.loads((trained_repeat / "repeat.json").read_text())["artifact_digest"]
        == (actual["result"]["artifact_digest"])
    )
    hello = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "generated-kit-client", "version": "1"},
        },
    }
    initialized = {"jsonrpc": "2.0", "method": "notifications/initialized"}

    def frame(*requests):
        return b"".join(
            (json.dumps(req) + "\n").encode() for req in (hello, initialized, *requests)
        )

    def call(number, name, arguments):
        return {
            "jsonrpc": "2.0",
            "id": number,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }

    def body(output):
        response = json.loads(output.splitlines()[-1])["result"]
        assert not response.get("isError")
        return json.loads(response["content"][0]["text"])

    aggregate = body(
        run(
            "mcp-readout",
            "generated/readout-input.json",
            stdin=frame(call(2, "inspect_offline_evaluation", {})),
        )
    )
    assert aggregate["cohort_size"] > 0
    manifest = private / "dataset.json"
    shutil.copyfile(target / "generated/dataset.json", manifest)
    manifest.chmod(0o600)
    database = private / "training.sqlite3"
    auth = [
        "--organization",
        "generated-demo",
        "--store",
        str(database),
        "--manifest",
        str(manifest),
        "--authorization",
        str(private / "authorization.json"),
        "--custodian-attestation",
        "att-0123456789abcdef",
    ]
    preview = call(2, "preview_dataset_onboarding", {})
    first_preview = body(run("mcp-onboard", *auth, stdin=frame(preview)))
    assert first_preview["registration"] == "absent" and not database.exists()
    run(
        "onboarding-authorize",
        *auth,
        "--dataset-revision",
        first_preview["dataset_revision"],
        "--expires-at",
        str(int(time.time()) + 600),
    )
    confirmed = body(
        run(
            "mcp-onboard",
            *auth,
            stdin=frame(
                preview,
                call(
                    3,
                    "confirm_dataset_onboarding",
                    {"dataset_revision": expected["dataset_revision"]},
                ),
            ),
        )
    )
    assert confirmed["registration"] == "registered" and database.is_file()
    supplied_manifest = private / "supplied.json"
    shutil.copyfile(target / "generated/evaluation-only.json", supplied_manifest)
    supplied_manifest.chmod(0o600)
    supplied_args = auth.copy()
    supplied_args[supplied_args.index(str(manifest))] = str(supplied_manifest)
    supplied_args[supplied_args.index(str(private / "authorization.json"))] = str(
        private / "evaluation-grant.json"
    )
    supplied_preview = body(run("mcp-onboard", *supplied_args, stdin=frame(preview)))
    assert supplied_preview["training_rows"] == 0
    supplied_revision = supplied_preview["dataset_revision"]
    run(
        "onboarding-authorize",
        *supplied_args,
        "--dataset-revision",
        supplied_revision,
        "--expires-at",
        str(int(time.time()) + 600),
    )
    assert (
        body(
            run(
                "mcp-onboard",
                *supplied_args,
                stdin=frame(
                    preview,
                    call(3, "confirm_dataset_onboarding", {"dataset_revision": supplied_revision}),
                ),
            )
        )["dataset_revision"]
        == supplied_revision
    )
    supplied_store = ["data", "--db", str(database), "--org", "generated-demo"]
    supplied_repeat = private / "supplied-repeat"
    run(
        *supplied_store,
        "score-only",
        "--dataset",
        "generated-supplied",
        "--output-dir",
        str(supplied_repeat),
    )
    assert json.loads((supplied_repeat / "repeat.json").read_text())["artifact_digest"] is None
    config = private / "worker.json"
    example = json.loads((target / "integrations/slack/worker.example.json").read_text())
    assert not example["zapier"] and all(
        user["expires_at"] == 0 for user in example["users"].values()
    )
    assert set(example["users"]["UREVIEWER"]["datasets"]) == {
        "generated-intake",
        "generated-supplied",
    }
    example["directory"] = str(private)
    config.write_text(json.dumps(example))
    config.chmod(0o600)
    assert b"validated" in run("slack", str(config), "--check-config").lower()
