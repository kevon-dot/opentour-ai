"""Cross the managed entry points, actual private records and the customer handoff."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest
from opentour_cell.pilot import repair_workflow as repair
from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.training import LocalTrainingStore

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "prepare-repair-example.py"
ORG = "generated-co"
RUN = "run-" + "3" * 32


def setup(tmp_path, route="rule"):
    directory = tmp_path / "owner"
    subprocess.run(
        [sys.executable, str(EXAMPLE), str(directory), "--route", route],
        capture_output=True,
        check=True,
    )
    return directory, json.loads((directory / "managed-plan.json").read_bytes())


def say(directory, manifest):
    manifest["slack_review"] = {
        "team": "TGERNERATED",
        "channel": "CGGENERATED",
        "audience": manifest["audience"],
        "owner_attestation": "att-" + "4" * 16,
        "aggregate": True,
        "expires_at": int(time.time()) + 600,
    }
    return repair.grant_slack_review(directory, ORG, manifest)


def test_rule_selects_no_model_replays_and_stages_witness(tmp_path):
    p, manifest = setup(tmp_path)
    assert repair.inspect_managed(p, ORG, manifest)["protected_count"] == 4
    result = repair.execute_managed(p, ORG, manifest, RUN)
    assert result["outcome"] == "simple_repair" and result["case_count"] == 4
    assert repair.execute_managed(p, ORG, manifest, RUN) == result
    work = p / "managed-outputs" / RUN
    assert not (work / "model").exists()
    comparison = json.loads((work / "comparison.json").read_text())
    assert result["result_revision"] == digest(comparison)
    assert comparison["arms"]["incumbent"]["metrics"]["incorrect_automatic"] == 2
    assert comparison["arms"]["simple"]["metrics"]["incorrect_automatic"] == 0
    assert not (work / "review.json").exists()
    prepared = repair.prepare_handoff(p, ORG, manifest, RUN)
    assert prepared["disposition"] == "rule"
    assert Path(prepared["spec_path"]).is_file()


def test_missing_grant_holds_without_opening_protected_answers(tmp_path, monkeypatch):
    p, manifest = setup(tmp_path, "model")

    def no_answers(*args, **kwargs):
        raise AssertionError("protected answers opened during a held decision")

    monkeypatch.setattr(repair.source_intake, "load_intake", no_answers)
    assert repair.inspect_managed(p, ORG, manifest)["state"] == "ready"
    result = repair.execute_managed(p, ORG, manifest, RUN)
    assert result["outcome"] == "hold" and result["case_count"] == 0
    assert not (p / "managed-outputs" / RUN / "model").exists()


def test_source_revocation_and_changed_organization_do_not_run(tmp_path):
    p, manifest = setup(tmp_path)
    with pytest.raises(ValueError):
        repair.inspect_managed(p, "other", manifest)
    (p / "source-grant.json").unlink()
    with pytest.raises((OSError, ValueError)):
        repair.execute_managed(p, ORG, manifest, RUN)
    assert not (p / "managed-outputs").exists()


def test_managed_review_is_minimal_and_withdrawal_stops_disclosure(tmp_path):
    p, manifest = setup(tmp_path)
    say(p, manifest)
    receipt = repair.execute_managed(p, ORG, manifest, RUN)
    audience, team, channel = manifest["audience"], "TGERNERATED", "CGGENERATED"
    selected = repair.managed_review_sources(p, ORG, ["generated-repair"], audience, team, channel)
    assert selected == [("generated-repair", "managed:" + RUN)]
    assert not repair.managed_review_sources(p, ORG, ["foreign"], audience, team, channel)
    assert not repair.managed_review_sources(
        p, ORG, ["generated-repair"], audience, "TOTHER", channel
    )
    view = repair.managed_review_evidence(p, ORG, RUN, audience, team, channel)
    assert view["audit"]["run_revision"] == receipt["result_revision"]
    assert view["readout"]["cohort_size"] == 4
    assert not any(x in json.dumps(view) for x in ["paired_cases", "returned coat", "refund-6"])
    (p / ("slack-review-" + digest(manifest) + ".json")).unlink()
    with pytest.raises((ValueError, OSError)):
        repair.check_managed_review(
            p, ORG, RUN, audience, team, channel, receipt["result_revision"]
        )


def test_custodian_accounts_for_managed_data_and_preserves_denial(tmp_path):
    p, manifest = setup(tmp_path)
    repair.execute_managed(p, ORG, manifest, RUN)
    (p / "workplace-snapshots").mkdir(mode=0o700)
    (p / "workplace-source-generated.json").touch(mode=0o600)
    keeper = Closeout(p, ORG)
    record = keeper.withdraw(
        agreement="generated-terms",
        custodian="generated-custodian",
        support="generated-support",
        due="2030-01-01",
        trigger="withdrawal",
    )
    names = {i["item"] for i in record["items"]}
    assert {
        "source-intake-copies",
        "managed-repair-copies",
        "workplace-snapshots",
        "workplace-permissions",
    } <= names
    with pytest.raises(ValueError):
        repair.execute_managed(p, ORG, manifest, RUN)
    with pytest.raises(ValueError):
        LocalTrainingStore(p / "training.sqlite3", ORG, existing_only=True)


def test_release_depends_on_inspection_then_named_human_decision(tmp_path):
    import sqlite3

    from opentour_cell.pilot.release_gate import authorize_release, record_release
    from opentour_cell.pilot.slack_adapter import Journal

    p, manifest = setup(tmp_path)
    team, channel = "TGERNERATED", "CGGENERATED"
    reviewer, audience = "UREVIEW", manifest["audience"]
    manifest["release_review"] = {
        "reviewer": reviewer,
        "team": team,
        "channel": channel,
        "audience": audience,
    }
    say(p, manifest)
    receipt = repair.execute_managed(p, ORG, manifest, RUN)
    evidence = repair.managed_review_evidence(p, ORG, RUN, audience, team, channel)
    artifact = evidence["audit"]["candidate_digest"]
    with pytest.raises((OSError, ValueError)):
        repair.prepare_handoff(p, ORG, manifest, RUN)
    ledger = p / "slack-pilot.sqlite3"
    Journal(ledger, "generated")
    now = int(time.time())
    choice = dict(
        run_id=RUN,
        run_revision=receipt["result_revision"],
        artifact_sha=artifact,
        reviewer=reviewer,
        team=team,
        channel=channel,
        audience=audience,
        decision="approved",
        inspection_ref="presented",
        provider="slack",
    )
    with pytest.raises(ValueError):
        record_release(p, ORG, **choice)
    seen = {
        "engagement": manifest["engagement"],
        "run_revision": receipt["result_revision"],
        "model_revision": artifact,
        "job_id": RUN,
        "viewer": reviewer,
        "team": team,
        "channel": channel,
        "recipient": audience,
    }
    with sqlite3.connect(ledger) as db:
        db.execute(
            "INSERT INTO exposures VALUES(?,?,?,?,?)",
            ("presented", "generated", "presented", now, now),
        )
        db.execute(
            "INSERT INTO presentations VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "presented",
                reviewer,
                "readout-version",
                "policy",
                "scope",
                now,
                now + 120,
                json.dumps(seen),
                "{}",
            ),
        )
    with pytest.raises((OSError, ValueError)):
        authorize_release(p, ORG, RUN, receipt["result_revision"], artifact)
    with pytest.raises(ValueError):
        record_release(p, ORG, **{**choice, "reviewer": "OTHER"})
    with pytest.raises(ValueError):
        record_release(p, ORG, **{**choice, "provider": "zapier_slack"})
    # A planted SQL row never substitutes for the authenticated Slack presentation.
    with pytest.raises(ValueError):
        record_release(p, ORG, **choice)
