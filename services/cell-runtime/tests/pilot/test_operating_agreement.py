"""Prospective owner limits, private scoped enrollment and byte-compatible frozen replays."""

import hashlib
import json
import sqlite3
import subprocess
import sys
import time

import pytest
from opentour_cell.pilot import readout
from opentour_cell.pilot.onboarding_mcp import CONFIRM, PREVIEW, Selection
from opentour_cell.pilot.operating_point import (
    AGREEMENT_VERSION,
    AgreementError,
    digest,
    operating_point,
    source_digest,
    validate_agreement,
)
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore, registration_state
from opentour_cell.pilot.training_data import (
    generated_dataset,
    generated_evaluation_dataset,
    prepare,
)

ORG = "generated-scope"
ENG = "generated-engagement"
ACTOR = "generated-trainer"
EVAL = "opentour_cell.pilot.readout"
DATA = "opentour_cell.pilot.training"


def agreement(value, **changes):
    now = int(time.time())
    return {
        "schema_version": AGREEMENT_VERSION,
        "organization": ORG,
        "engagement": ENG,
        "source_digest": source_digest(value),
        "current_method": "generated-incumbent",
        "repair_method": "generated-candidate",
        "owner_attestation": "att-1111111122222222",
        "agreed_at": now - 10,
        "expires_at": now + 3600,
        "state": "active",
        "basis": "predeclared",
        "minimum_coverage_bps": 8000,
        "error_ceiling_bps": value.get("manifest", value)["error_ceiling_bps"],
        **changes,
    }


def private(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")
    path.chmod(0o600)
    return path


def cli(module, *args, success=True):
    result = subprocess.run(
        [sys.executable, "-B", "-m", module, *map(str, args)],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == (0 if success else 2), result.stderr
    return result.stdout if success else result.stderr


def manifest(size=10, automatic=2):
    cases = []
    for i in range(size):
        known = {"status": "automatic", "label": "a"}
        wrong = {"status": "automatic", "label": "b"}
        review = {"status": "review"}
        cases.append(
            {
                "case_id": f"generated-{i}",
                "reference": "a",
                "current": (wrong if i == 0 else known) if i < automatic else review,
                "repair": known if i < automatic else review,
            }
        )
    return {"schema_version": readout.INPUT_VERSION, "error_ceiling_bps": 500, "cases": cases}


def test_portable_operating_agreement_preserves_legacy_bytes_and_shows_both_limits(tmp_path):
    raw = manifest()
    path = private(tmp_path / "cases.json", raw)
    selected = agreement(raw)
    terms = private(tmp_path / "terms.json", selected)
    assert cli(EVAL, "--source-digest", path).strip() == source_digest(raw)
    opts = ("--organization", ORG, "--engagement", ENG, "--operating-agreement", terms)
    cli(EVAL, path, "--report-dir", tmp_path / "legacy")
    assert {p.name for p in (tmp_path / "legacy").iterdir()} == {"aggregate.json", "brief.md"}
    for name in ("first", "second"):
        cli(EVAL, path, *opts, "--report-dir", tmp_path / name)
    for name in ("aggregate.json", "brief.md"):
        assert (tmp_path / "legacy" / name).read_bytes() == (tmp_path / "first" / name).read_bytes()
    for file in (tmp_path / "first").iterdir():
        assert file.read_bytes() == (tmp_path / "second" / file.name).read_bytes()
    quality = json.loads((tmp_path / "first/decision-quality.json").read_text())
    assert quality == json.loads(cli(EVAL, path, *opts))
    assert quality["operating_point"] == {
        "schema_version": "opentour.pilot.operating-point.v1",
        "agreement_revision": digest(selected),
        "minimum_coverage_bps": 8000,
        "error_ceiling_bps": 500,
        "current": {"coverage": "missed", "error": "missed", "combined": "missed"},
        "repair": {"coverage": "missed", "error": "met", "combined": "missed"},
    }
    assert quality["observed_comparison"] == "improved"
    assert quality["current"]["review"] == 8
    text = (tmp_path / "first/decision-quality.md").read_text()
    assert "2/10 (20.00%)" in text and "0/2 (0.00%)" in text and "8000 basis points" in text
    assert (
        quality["aggregate_digest"]
        == hashlib.sha256((tmp_path / "first/aggregate.json").read_bytes()).hexdigest()
    )
    assert selected["owner_attestation"] not in text and ORG not in json.dumps(quality)
    descriptive = json.loads(cli(EVAL, path, "--decision-quality"))
    assert descriptive["basis"] == "descriptive" and descriptive["operating_point"] is None
    cli(EVAL, path, "--decision-quality", "--report-dir", tmp_path / "descriptive")
    neutral = (tmp_path / "descriptive/decision-quality.md").read_text()
    assert "| Repair | 2/10 (20.00%) | Unrecorded | 0/2 (0.00%) |" in neutral
    assert json.loads(cli(EVAL, path)) == json.loads(
        (tmp_path / "legacy/aggregate.json").read_text()
    )
    private(terms, {**selected, "minimum_coverage_bps": 2000})
    revised = json.loads(cli(EVAL, path, *opts))
    assert revised["operating_point"]["agreement_revision"] != digest(selected)
    assert revised["operating_point"]["repair"]["combined"] == "met"


@pytest.mark.parametrize(
    "field,value",
    [
        ("minimum_coverage_bps", True),
        ("minimum_coverage_bps", 10001),
        ("error_ceiling_bps", -1),
        ("error_ceiling_bps", 501),
        ("schema_version", "future"),
        ("basis", "retrospective"),
        ("state", "withdrawn"),
        ("owner_attestation", "unnamed"),
        ("agreed_at", True),
        ("agreed_at", 2**61),
        ("expires_at", 1),
        ("organization", "other"),
        ("engagement", "other"),
        ("source_digest", "b" * 64),
        ("supersedes", "not-a-revision"),
    ],
)
def test_invalid_or_stale_terms_fail_closed(field, value):
    raw = manifest()
    with pytest.raises(AgreementError):
        validate_agreement(
            agreement(raw, **{field: value}),
            organization=ORG,
            engagement=ENG,
            source=source_digest(raw),
            ceiling=500,
        )


def test_agreement_file_scope_and_late_withdrawal_cancel_publication(tmp_path, monkeypatch):
    raw = manifest()
    path = private(tmp_path / "cases.json", raw)
    original = agreement(raw)
    selected = private(tmp_path / "agreement.json", original)
    opts = ("--operating-agreement", selected, "--organization", ORG, "--engagement", ENG)
    selected.chmod(0o644)
    assert "private_operating_agreement_required" in cli(EVAL, path, *opts, success=False)
    selected.chmod(0o600)
    out = tmp_path / "should-not-exist"
    evaluate = readout.evaluate_manifest

    def withdraw_after_evaluation(data):
        private(selected, {**original, "state": "withdrawn"})
        return evaluate(data)

    monkeypatch.setattr(readout, "evaluate_manifest", withdraw_after_evaluation)
    monkeypatch.setattr(sys, "argv", list(map(str, ("evaluate", path, *opts, "--report-dir", out))))
    assert readout.main() == 2 and not out.exists()


def test_exact_integer_boundaries_empty_unknown_missing_and_nonmatching_automatic_sets():
    for size, automated, floor, verdict in (
        (3, 1, 3333, "met"),
        (3, 1, 3334, "missed"),
        (10, 8, 8000, "met"),
        (10, 10, 10000, "met"),
        (10, 0, 0, "met"),
        (0, 0, 0, "undetermined"),
    ):
        raw = manifest(size, automated)
        p = operating_point(
            agreement(raw, minimum_coverage_bps=floor), readout.evaluate_manifest(raw)
        )
        assert p["repair"]["coverage"] == verdict
        expected = verdict if automated else "undetermined"
        assert p["repair"]["combined"] == expected
    raw = manifest(20, 20)
    point = operating_point(agreement(raw), readout.evaluate_manifest(raw))
    assert point["current"]["error"] == "met"  # 1/20 is exactly the 500 bps maximum.
    raw["cases"][0]["reference"] = None
    raw["cases"][1]["repair"] = {"status": "missing"}
    report = readout.evaluate_manifest(raw)
    point = operating_point(agreement(raw, minimum_coverage_bps=10000), report)
    assert point["repair"] == {"coverage": "missed", "error": "undetermined", "combined": "missed"}
    assert report["observed_comparison"] == "insufficient_evidence"
    switched = manifest(2, 1)
    switched["cases"][0]["repair"], switched["cases"][1]["repair"] = (
        switched["cases"][1]["repair"],
        switched["cases"][0]["repair"],
    )
    # One shared case makes the changed-set warning observable alongside the relative result.
    switched["cases"].append(manifest(3, 3)["cases"][2])
    assert readout.evaluate_manifest(switched)["observed_comparison"] == "different_automatic_cases"


@pytest.mark.parametrize("training", [False, True])
def test_separate_mcp_grant_and_fresh_process_score_only_verify_the_same_target(tmp_path, training):
    raw = generated_dataset(ORG, ACTOR) if training else generated_evaluation_dataset(ORG)
    terms = agreement(raw)
    value = {**raw, "operating_agreement": terms}
    path = private(tmp_path / "dataset.json", value)
    db, grant = tmp_path / "training.sqlite3", tmp_path / "grant.json"
    selection = Selection(ORG, db, path, grant, "att-0000111122223333")
    preview = selection.call(PREVIEW, {})["structuredContent"]
    assert preview["operating_agreement"] == {
        "minimum_coverage_bps": 8000,
        "error_ceiling_bps": 1000,
        "revision": digest(terms),
    }
    assert terms["owner_attestation"] not in json.dumps(preview)
    assert not db.exists()
    args = {"dataset_revision": selection.revision}
    assert selection.call(CONFIRM, args).get("isError")
    private(grant, selection.binding() | {"expires_at": int(time.time()) + 1000})
    assert selection.call(CONFIRM, args)["structuredContent"]["registration"] == "registered"
    assert cli(EVAL, "--source-digest", path).strip() == source_digest(raw)
    store = LocalTrainingStore(db, ORG)
    name = raw["dataset_id"]
    if training:
        assert store.can_propose(name, ACTOR)
        assert not store.can_propose(name, "somebody-else")
        with pytest.raises(TrainingError, match="training_not_authorized"):
            store.propose(name, authorized_trainer="somebody-else")
        job = store.propose(name, authorized_trainer=ACTOR, request_id="generated-one")
        completed = store.run(job["job_id"], reviewer=ACTOR, expected_revision=job["revision"])
        result = store.frozen_training(job["job_id"])
        assert completed["result"]["operating_point"] == result["operating_point"]
        flag, key = "--job", job["job_id"]
    else:
        assert not store.can_propose(name, ACTOR)
        result = store.frozen_evaluation(name)
        flag, key = "--dataset", name
    counts = store.custody_inventory()
    assert counts["models"] == int(training)
    for directory in ("first", "second"):
        cli(
            DATA,
            "--db",
            db,
            "--org",
            ORG,
            "score-only",
            flag,
            key,
            "--output-dir",
            tmp_path / directory,
        )
    for output in (tmp_path / "first").iterdir():
        assert output.read_bytes() == (tmp_path / "second" / output.name).read_bytes()
    quality = json.loads((tmp_path / "first/decision-quality.json").read_text())
    assert quality["operating_point"] == result["operating_point"]
    assert quality["cohort_digest"] == result["cohort_digest"]
    assert quality["source_revision"] == selection.revision
    assert quality["source_digest"] == source_digest(raw)
    assert quality["artifact_digest"] == result["artifact_digest"]
    assert (
        result["decision_quality_digest"]
        == hashlib.sha256((tmp_path / "first/decision-quality.json").read_bytes()).hexdigest()
    )
    assert store.custody_inventory() == counts
    store.withdraw()
    assert selection.call(PREVIEW, {})["structuredContent"] == {"registration": "unavailable"}
    assert cli(
        DATA,
        "--db",
        db,
        "--org",
        ORG,
        "score-only",
        flag,
        key,
        "--output-dir",
        tmp_path / "blocked",
        success=False,
    )
    assert not (tmp_path / "blocked").exists()


def test_new_scope_rejects_retrofit_and_atomically_supersedes_a_fresh_comparison(tmp_path):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", ORG)
    first = generated_evaluation_dataset(ORG)
    original = agreement(first)
    store.register({**first, "operating_agreement": original})
    prior = store.frozen_evaluation(first["dataset_id"])
    with pytest.raises(TrainingError, match="dataset_revision_conflict"):
        store.register({**first, "operating_agreement": {**original, "minimum_coverage_bps": 2000}})
    alias = {**first, "dataset_id": "generated-fresh"}
    with pytest.raises(TrainingError, match="prospective_agreement_requires_fresh_heldout"):
        store.register(
            {**alias, "operating_agreement": agreement(alias, supersedes=digest(original))}
        )
    fresh = generated_evaluation_dataset(ORG)
    fresh["dataset_id"] = "generated-fresh"
    for row in fresh["manifest"]["cases"]:
        row["case_id"] += "-new"
    for changes, error in (
        ({"engagement": "other"}, "operating_engagement_mismatch"),
        ({"repair_method": "some-other-method"}, "superseded_agreement_mismatch"),
        ({"supersedes": "2" * 64}, "superseded_agreement_unavailable"),
    ):
        terms = {**agreement(fresh, supersedes=digest(original)), **changes}
        with pytest.raises(TrainingError, match=error):
            store.register({**fresh, "operating_agreement": terms})
    assert store.frozen_evaluation(first["dataset_id"]) == prior
    updated = agreement(fresh, minimum_coverage_bps=2000, supersedes=digest(original))
    saved = store.register({**fresh, "operating_agreement": updated})
    assert saved["agreement_revision"] == digest(updated) != digest(original)
    with pytest.raises(TrainingError, match="dataset_unavailable"):
        store.frozen_evaluation(first["dataset_id"])
    assert (
        store.frozen_evaluation(fresh["dataset_id"])["operating_point"]["minimum_coverage_bps"]
        == 2000
    )


def test_stored_agreement_tamper_expiry_and_missing_rows_never_downgrade(tmp_path, monkeypatch):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", ORG)
    item = generated_evaluation_dataset(ORG)
    terms = agreement(item)
    summary = store.register({**item, "operating_agreement": terms})
    name = item["dataset_id"]
    selected = (store.path, ORG, name, summary["dataset_revision"])
    assert registration_state(*selected) == "registered"
    with monkeypatch.context() as clock:
        clock.setattr(time, "time", lambda: terms["expires_at"])
        with pytest.raises(TrainingError, match="dataset_unavailable"):
            store.frozen_evaluation(name)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE operating_agreements SET content=?",
            (json.dumps({**terms, "minimum_coverage_bps": 5}),),
        )
    assert registration_state(*selected) == "unavailable"
    with pytest.raises(TrainingError, match="operating_agreement_unverified"):
        store.frozen_evaluation(name)
    with sqlite3.connect(store.path) as db:
        db.execute("DELETE FROM operating_agreements")
    assert store.catalog() == []
    assert registration_state(*selected) == "unavailable"
    with pytest.raises(TrainingError, match="operating_agreement_unverified"):
        store.frozen_evaluation(name)


def test_old_database_migrates_without_qualifying_legacy_or_mutating_its_report(tmp_path):
    db = tmp_path / "training.sqlite3"
    store = LocalTrainingStore(db, ORG)
    raw = generated_evaluation_dataset(ORG)
    saved = store.register(raw)
    result = store.frozen_evaluation(raw["dataset_id"])
    with sqlite3.connect(db) as old:
        old.execute("DROP TABLE operating_agreements")
        old.execute("ALTER TABLE datasets DROP COLUMN agreement_revision")
    reopened = LocalTrainingStore(db, ORG)
    assert reopened.dataset_summary(raw["dataset_id"]) == saved
    assert reopened.frozen_evaluation(raw["dataset_id"]) == result
    assert prepare(raw, ORG).revision == digest(raw)
    out = tmp_path / "descriptive"
    reopened.score_only(dataset_id=raw["dataset_id"], output_dir=out, include_decision_quality=True)
    assert json.loads((out / "decision-quality.json").read_text())["basis"] == "descriptive"
