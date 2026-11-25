"""Generated owner-local training data, durable approvals and genuine fitted outputs."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest
from opentour_cell.pilot import training
from opentour_cell.pilot.text_classifier import TrainingError, predict
from opentour_cell.pilot.training_data import digest, generated_dataset

ORG = "generated-org"
REVIEWER = "U123"
DATASET = "generated-intake"


def enrolled(root):
    store = training.LocalTrainingStore(root / "private.db", ORG)
    store.register(generated_dataset(ORG, REVIEWER))
    return store


def execute(store, job):
    return store.run(job["job_id"], reviewer=REVIEWER, expected_revision=job["revision"])


def test_grounded_examples_search_and_private_model_status(tmp_path):
    store = enrolled(tmp_path)
    assert store.catalog("shipping") == []  # Searching content or labels cannot reveal enrollment.
    assert store.catalog("absent") == []
    assert store.catalog("text")[0]["dataset_id"] == DATASET
    sample = store.preview(DATASET)
    assert sample["fields"] == ["case_id", "label", "text"]
    assert sample["example_limit"] == 2
    assert all(row["case_id"].startswith("train-") for row in sample["rows"])
    assert "test-" not in json.dumps(sample)
    job = store.propose(DATASET, request_id="before-readout")
    assert job == store.propose(DATASET, request_id="before-readout")
    assert job["status"] == "awaiting_approval" and job["result"] is None
    assert (job["train_count"], job["heldout_count"], job["max_train_updates"]) == (6, 4, 18)
    with pytest.raises(TrainingError, match="artifact_unavailable"):
        store.artifact(job["job_id"])
    with pytest.raises(TrainingError, match="request_conflict"):
        store.propose(DATASET, request_id="before-readout", epochs=1)
    with pytest.raises(TrainingError, match="reviewer_denied"):
        store.run(job["job_id"], reviewer="U999", expected_revision=job["revision"])
    with pytest.raises(TrainingError, match="stale_revision"):
        store.run(job["job_id"], reviewer=REVIEWER, expected_revision="wrong")
    assert store.status(job["job_id"])["status"] == "awaiting_approval"
    finished = execute(store, job)
    assert finished["status"] == "completed"
    result = finished["result"]
    verified = store.frozen_training(job["job_id"])
    assert all(value == result[key] for key, value in verified.items())
    assert (result["update_count"], result["changed_weights"]) == (2, 8)
    report = result["readout"]
    assert (report["current"]["incorrect_automatic"], report["repair"]["incorrect_automatic"]) == (
        2,
        1,
    )
    assert report["paired"] == {
        "comparable": 4,
        "excluded": 0,
        "fixes": 1,
        "regressions": 0,
        "ties_correct": 2,
        "ties_incorrect": 1,
    }
    assert (
        report["observed_comparison"] == "improved" and not report["repair"]["observed_ceiling_met"]
    )
    assert "test-" not in json.dumps(result) and "invoice" not in json.dumps(result)
    model = store.artifact(job["job_id"])
    assert digest(model) == result["artifact_digest"]
    assert any(weight for weights in model["weights"] for weight in weights)
    assert predict(model, "parcel delivery delayed") == "shipping"
    reopened = training.LocalTrainingStore(tmp_path / "private.db", ORG)
    assert reopened.status(job["job_id"]) == finished == execute(reopened, job)
    assert reopened.artifact(job["job_id"]) == model
    with pytest.raises(TrainingError, match="fresh_heldout_required"):
        reopened.propose(DATASET, epochs=5)


def test_heldout_relabeling_changes_audit_but_cannot_change_fitted_weights(tmp_path):
    artifacts = []
    counts = []
    for index in range(2):
        store = training.LocalTrainingStore(tmp_path / f"{index}.db", ORG)
        dataset = generated_dataset(ORG, REVIEWER)
        if index:
            for row in dataset["heldout"]:
                row["label"] = None
        store.register(dataset)
        result = execute(store, store.propose(DATASET))["result"]
        artifacts.append(result["artifact_digest"])
        counts.append(result["readout"]["unknown_references"])
        if index:
            assert result["readout"]["observed_comparison"] == "insufficient_evidence"
    assert artifacts[0] == artifacts[1] and counts == [0, 4]


@pytest.mark.parametrize(
    "change,code",
    [
        (lambda d: d.update(train_approved=False), "permission_required"),
        (lambda d: d.update(evaluation_approved=False), "permission_required"),
        (lambda d: d.update(expires_at=1), "grant_expired"),
        (lambda d: d["train"][0].update(organization="other"), "scope_mismatch"),
        (lambda d: d["heldout"][0].update(family_id=d["train"][0]["family_id"]), "split_overlap"),
        (lambda d: d["heldout"][0].update(text="STAtement, invoice billing"), "split_overlap"),
        (lambda d: d["heldout"][0].update(case_id=d["train"][0]["case_id"]), "duplicate_case"),
        (lambda d: d["heldout"][0].update(preview={"text": "forbidden"}), "invalid_dataset"),
        (
            lambda d: d["train"][0]["preview"].update(text="fabricated-example"),
            "invalid_visibility",
        ),
        (
            lambda d: d["train"][0]["preview"].update(text="invoice [redacted] fabricated"),
            "invalid_visibility",
        ),
        (lambda d: d["train"][0]["preview"].update(label="other"), "invalid_visibility"),
        (lambda d: d.update(preview_fields=["secret"]), "invalid_visibility"),
        (
            lambda d: d["heldout"][0].update(incumbent={"status": "unknown"}),
            "invalid_heldout_incumbent",
        ),
        (lambda d: d.update(train=d["train"] * 43), "training_input_required"),
        (lambda d: d.update(heldout=[]), "heldout_input_required"),
    ],
)
def test_invalid_enrollment_has_no_visible_side_effect_or_content_error(tmp_path, change, code):
    store = training.LocalTrainingStore(tmp_path / "private.db", ORG)
    data = generated_dataset(ORG, REVIEWER)
    change(data)
    with pytest.raises(TrainingError) as error:
        store.register(data)
    assert str(error.value) == code and store.catalog() == []


def test_redacted_and_disabled_preview_do_not_change_training(tmp_path):
    data = generated_dataset(ORG, REVIEWER)
    store = training.LocalTrainingStore(tmp_path / "private.db", ORG)
    data["train"][0]["preview"]["text"] = "invoice [redacted] statement"
    store.register(data)
    assert store.preview(DATASET)["rows"][0]["text"] == "invoice [redacted] statement"
    data["dataset_id"] = "private-only"
    data["visible_fields"] = ["case_id"]
    del data["preview_fields"]
    for row in data["train"]:
        row.pop("preview", None)
    store.register(data)
    assert store.preview("private-only")["rows"] == []
    assert [d["dataset_id"] for d in store.catalog("label")] == [DATASET]


def test_store_permissions_and_organization_binding(tmp_path):
    db = tmp_path / "private.db"
    reopened = enrolled(tmp_path)
    assert db.stat().st_mode & 0o077 == 0
    with pytest.raises(TrainingError, match="scope_mismatch"):
        training.LocalTrainingStore(db, "other")
    db.chmod(0o640)
    with pytest.raises(TrainingError, match="private_store_required"):
        reopened.catalog()
    with pytest.raises(TrainingError, match="private_store_required"):
        training.LocalTrainingStore(db, ORG)
    db.chmod(0o600)
    bad = tmp_path / "public-dir"
    bad.mkdir(mode=0o755)
    bad.chmod(0o755)
    with pytest.raises(TrainingError, match="private_store_required"):
        training.LocalTrainingStore(bad / "fresh.db", ORG)
    assert not (bad / "fresh.db").exists()
    link = tmp_path / "link.db"
    link.symlink_to(db)
    with pytest.raises(TrainingError, match="private_store_required"):
        training.LocalTrainingStore(link, ORG)


def test_immutable_registration_cross_dataset_overlap_and_revoked_grant(tmp_path):
    store = enrolled(tmp_path)
    original = generated_dataset(ORG, REVIEWER)
    assert store.register(original) == store.catalog()[0]
    original["error_ceiling_bps"] = 5
    with pytest.raises(TrainingError, match="dataset_revision_conflict"):
        store.register(original)
    clone = generated_dataset(ORG, REVIEWER)
    clone["dataset_id"] = "split-swap"
    # Held-out family from one enrollment cannot become training in another.
    clone["train"][0]["family_id"] = "test-family-0"
    clone["heldout"][0]["family_id"] = "other-family"
    with pytest.raises(TrainingError, match="split_overlap"):
        store.register(clone)
    job = store.propose(DATASET)
    store.revoke(DATASET)
    assert store.catalog() == []
    for operation in (
        lambda: store.preview(DATASET),
        lambda: execute(store, job),
        lambda: store.status(job["job_id"]),
    ):
        with pytest.raises(TrainingError, match="dataset_unavailable"):
            operation()


@pytest.mark.parametrize("epochs", [False, 0, 2, 4, 10000])
def test_compute_profile_enforced_before_proposal(tmp_path, epochs):
    store = enrolled(tmp_path)
    with pytest.raises(TrainingError, match="input_outside_profile"):
        store.propose(DATASET, epochs=epochs)
    with pytest.raises(TrainingError, match="qualified_trainer_required"):
        store.propose(DATASET, profile="external-llm-gpu")


def test_concurrent_approval_starts_one_fit_and_reuses_heldout(tmp_path, monkeypatch):
    store = enrolled(tmp_path)
    job = store.propose(DATASET)
    other = store.propose(DATASET, epochs=5)
    started, resume = threading.Event(), threading.Event()
    actual_fit = training.fit
    submissions = []

    def delayed_fit(rows, epochs, begin):
        submissions.append(len(rows))
        started.set()
        assert resume.wait(3)
        return actual_fit(rows, epochs, begin)

    monkeypatch.setattr(training, "fit", delayed_fit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute, store, job)
        assert started.wait(3)
        assert execute(store, job)["status"] == "running"
        rejected = execute(store, other)
        assert rejected["status"] == "failed" and rejected["error"] == "fresh_heldout_required"
        resume.set()
        assert first.result()["status"] == "completed"
    assert submissions == [6]


def test_interrupted_fit_without_checkpoint_is_held_without_a_second_fit(tmp_path, monkeypatch):
    store = enrolled(tmp_path)
    proposal = store.propose(DATASET)

    def crash(*args):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(training, "fit", crash)
        with pytest.raises(KeyboardInterrupt):
            execute(store, proposal)
    assert store.status(proposal["job_id"])["status"] == "running"
    with sqlite3.connect(tmp_path / "private.db") as db:
        db.execute(
            "UPDATE jobs SET lease_until=? WHERE id=?", (time.time() - 1, proposal["job_id"])
        )
    assert store.status(proposal["job_id"])["recoverable"] is True
    monkeypatch.setattr(training, "fit", lambda *_: pytest.fail("started a second fit"))
    monkeypatch.setattr(training, "_score", lambda *_: pytest.fail("read an unverified cohort"))
    assert execute(store, proposal)["status"] == "running"
    held = store.recover(
        proposal["job_id"], reviewer=REVIEWER, expected_revision=proposal["revision"]
    )
    assert (held["status"], held["error"], held["result"]) == (
        "failed",
        "interrupted_fit_unverified",
        None,
    )
    assert store.custody_inventory()["models"] == 0
    assert not store.status(proposal["job_id"])["recoverable"]
    with pytest.raises(TrainingError, match="fresh_heldout_required"):
        store.propose(DATASET)


def test_failed_compute_leaves_no_artifact_and_no_new_evaluation_grant(tmp_path, monkeypatch):
    store = enrolled(tmp_path)
    job = store.propose(DATASET)

    def over_budget(*args):
        raise TrainingError("compute_limit_reached")

    monkeypatch.setattr(training, "fit", over_budget)
    result = execute(store, job)
    assert result["status"] == "failed" and result["result"] is None
    assert result["error"] == "compute_limit_reached" and execute(store, job) == result
    with pytest.raises(TrainingError, match="artifact_unavailable"):
        store.artifact(job["job_id"])


def test_cli_generated_rehearsal_uses_one_persistent_result(tmp_path):
    env = {**os.environ, "PYTHONPATH": str(Path(training.__file__).resolve().parents[2])}
    args = [
        sys.executable,
        "-m",
        "opentour_cell.pilot.training",
        "--db",
        str(tmp_path / "demo.db"),
        "--org",
        "generated-demo",
        "rehearse",
    ]
    a, b = [
        subprocess.run(args, capture_output=True, text=True, check=True, env=env) for _ in range(2)
    ]
    assert a.stdout == b.stdout and json.loads(a.stdout)["result"]["changed_weights"] > 0


def test_registration_permission_is_rechecked_under_the_writer_lock(tmp_path):
    store = training.LocalTrainingStore(tmp_path / "private.db", ORG)

    def forbid():
        with sqlite3.connect(tmp_path / "private.db", timeout=0) as contender:
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                contender.execute("BEGIN IMMEDIATE")
        raise TrainingError("expired_enrollment")

    with pytest.raises(TrainingError, match="expired_enrollment"):
        store.register(generated_dataset(ORG, REVIEWER), check_authorization=forbid)
    assert store.catalog() == []
    enrolled(tmp_path)
    summary = store.dataset_summary(DATASET)
    assert summary == store.catalog()[0] and "rows" not in summary and "reviewers" not in summary


def test_registration_revoked_before_commit_rolls_back_complete_enrollment(tmp_path):
    store = training.LocalTrainingStore(tmp_path / "private.db", ORG)
    checks = []

    def recheck():
        checks.append(True)
        if len(checks) == 2:
            raise TrainingError("authorization_changed")

    with pytest.raises(TrainingError, match="authorization_changed"):
        store.register(generated_dataset(ORG, REVIEWER), check_authorization=recheck)
    assert len(checks) == 2 and store.catalog() == []
    assert not any(store.custody_inventory().values())


def test_two_dataset_aliases_cannot_expose_same_heldout_to_a_new_tuning_job(tmp_path):
    store = enrolled(tmp_path)
    alternate = generated_dataset(ORG, REVIEWER)
    alternate["dataset_id"] = "renamed-sample"
    store.register(alternate)
    assert execute(store, store.propose(DATASET))["status"] == "completed"
    with pytest.raises(TrainingError, match="fresh_heldout_required"):
        store.propose("renamed-sample", epochs=5)


def test_catalog_grants_apply_before_cap_and_readonly_probe_has_no_side_effect(tmp_path):
    path = tmp_path / "private.db"
    data = generated_dataset(ORG, REVIEWER)
    assert training.registration_state(path, ORG, DATASET, digest(data)) == "absent"
    assert not path.exists()
    store = enrolled(tmp_path)
    assert training.registration_state(path, ORG, DATASET, digest(data)) == "registered"
    assert training.registration_state(path, "foreign", DATASET, digest(data)) == "unavailable"
    assert training.registration_state(path, ORG, DATASET, "stale") == "stale"
    for i in range(27):
        data["dataset_id"] = f"before-{i:02d}"
        store.register(data)
    assert DATASET not in [row["dataset_id"] for row in store.catalog()]
    assert [row["dataset_id"] for row in store.catalog(allowed_ids={DATASET})] == [DATASET]
    assert store.dataset_summary(DATASET)["dataset_id"] == DATASET
    store.revoke(DATASET)
    assert training.registration_state(path, ORG, DATASET, "stale") == "unavailable"


def test_evaluation_only_registers_original_v1_and_cannot_create_training_jobs(tmp_path):
    store = training.LocalTrainingStore(tmp_path / "private.db", ORG)
    manifest = training.generated_evaluation_dataset(ORG)
    item = store.register(manifest)
    assert item["mode"] == item["purpose"] == "evaluation_only"
    assert item["can_train"] is False and item["train_count"] == 0 and item["heldout_count"] == 3
    assert item["permitted_actions"] == ["inspect_metadata", "score_frozen"]
    assert item["destination"] == "owner_local_store" and item["retention"] == manifest["retention"]
    assert store.catalog("reference") == [item]
    detail = store.preview(item["dataset_id"])
    assert detail["rows"] == [] and detail["scope"] == "metadata_only"
    with pytest.raises(TrainingError, match="training_not_authorized"):
        store.propose(item["dataset_id"])
    evidence = store.frozen_evaluation(item["dataset_id"])
    assert evidence["readout"] == training.evaluate_manifest(manifest["manifest"])
    assert evidence["artifact_digest"] is None
    assert "supplied-1" not in json.dumps(evidence)
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM train_data").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT reviewers FROM datasets").fetchone()[0] == "[]"


@pytest.mark.parametrize(
    "added",
    [
        {"train_approved": True},
        {"train": []},
        {"reviewers": [REVIEWER]},
        {"preview_fields": []},
        {"destination": "https://example.invalid/private"},
        {"retention": {"terms_ref": "private@example.invalid"}},
        {"retention": {"disposition": "delete", "date": "2027-02-30"}},
        {"evaluation_approved": False},
    ],
)
def test_supplied_only_rejects_undeclared_rights_bad_scope_and_dates(tmp_path, added):
    store = training.LocalTrainingStore(tmp_path / "private.db", ORG)
    data = training.generated_evaluation_dataset(ORG)
    data.update(added)
    with pytest.raises(TrainingError):
        store.register(data)
    assert store.catalog() == []


def test_fresh_process_score_only_rebuilds_v1_and_fitted_reports_without_new_work(tmp_path):
    store = enrolled(tmp_path)
    supplied = training.generated_evaluation_dataset(ORG)
    # Exercise review, unknown and equal coverage on different automatic subsets.
    supplied["manifest"]["cases"].extend(
        [
            {
                "case_id": "s-left",
                "reference": "route-a",
                "current": {"status": "review"},
                "repair": {"status": "automatic", "label": "route-a"},
            },
            {
                "case_id": "s-right",
                "reference": None,
                "current": {"status": "automatic", "label": "route-b"},
                "repair": {"status": "review"},
            },
        ]
    )
    store.register(supplied)
    job = store.propose(DATASET)
    original = execute(store, job)
    before_counts = store.custody_inventory()
    env = {**os.environ, "PYTHONPATH": str(Path(training.__file__).resolve().parents[2])}
    source = tmp_path / "owner-supplied.json"
    source.write_text(json.dumps(supplied["manifest"]))
    raw = tmp_path / "canonical-v1"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.readout",
            str(source),
            "--report-dir",
            str(raw),
        ],
        cwd=tmp_path,
        env=env,
        check=True,
    )
    for flag, key, ref in (
        ("--dataset", supplied["dataset_id"], store.frozen_evaluation(supplied["dataset_id"])),
        ("--job", job["job_id"], original["result"]),
    ):
        target = tmp_path / ("fresh" + flag)
        invocation = [
            sys.executable,
            "-m",
            "opentour_cell.pilot.training",
            "--db",
            str(store.path),
            "--org",
            ORG,
            "score-only",
            flag,
            key,
            "--output-dir",
            str(target),
        ]
        receipt = json.loads(subprocess.check_output(invocation, cwd=tmp_path, env=env))
        assert receipt == json.loads((target / "repeat.json").read_text())
        assert (
            receipt["aggregate_digest"],
            receipt["brief_digest"],
            receipt["artifact_digest"],
        ) == (ref["aggregate_digest"], ref["brief_digest"], ref["artifact_digest"])
        assert json.loads((target / "aggregate.json").read_text()) == ref["readout"]
        if flag == "--dataset":
            assert all(
                (raw / f).read_bytes() == (target / f).read_bytes()
                for f in ("aggregate.json", "brief.md")
            )
        repeat = subprocess.run(invocation, cwd=tmp_path, env=env, capture_output=True, check=False)
        assert repeat.returncode == 2 and b"fresh_output_directory_required" in repeat.stderr
    assert store.custody_inventory() == before_counts
    assert store.status(job["job_id"]) == original


@pytest.mark.parametrize(
    "modified,expected",
    [
        ("model", "artifact_digest_mismatch"),
        ("missing_model", "artifact_unavailable"),
        ("cohort", "cohort_digest_mismatch"),
        ("report", "readout_repeat_mismatch"),
        ("consistent_wrong_report", "readout_repeat_mismatch"),
        ("brief", "readout_repeat_mismatch"),
    ],
)
def test_heldout_repeat_holds_on_changed_binding_without_writing_files(
    tmp_path, modified, expected
):
    store = enrolled(tmp_path)
    job = execute(store, store.propose(DATASET))
    with sqlite3.connect(store.path) as db:
        if modified == "model":
            db.execute("UPDATE artifacts SET model='{}'")
        elif modified == "missing_model":
            db.execute("DELETE FROM artifacts")
        elif modified == "cohort":
            db.execute("UPDATE heldout_data SET content='[]'")
        else:
            original = job["result"]
            if modified in {"report", "consistent_wrong_report"}:
                original["readout"]["cohort_size"] += 1
                if modified == "consistent_wrong_report":
                    cached = training._evidence(
                        DATASET,
                        original["dataset_revision"],
                        "training",
                        original["cohort_digest"],
                        original["artifact_digest"],
                        original["readout"],
                    )
                    original.update(cached)
            else:
                original["brief_digest"] = "0" * 64
            db.execute("UPDATE jobs SET result=?", (json.dumps(original),))
    target = tmp_path / "untrusted-repeat"
    with pytest.raises(TrainingError, match=expected):
        store.score_only(job_id=job["job_id"], output_dir=target)
    with pytest.raises(TrainingError, match=expected):
        store.frozen_training(job["job_id"])
    assert not target.exists()


def test_withdraw_prevents_later_access_but_inventory_and_copy_disposition_stay_separate(tmp_path):
    first = enrolled(tmp_path)
    supplied = training.generated_evaluation_dataset(ORG)
    first.register(supplied)
    job = execute(first, first.propose(DATASET))
    outsider = training.LocalTrainingStore(tmp_path / "other.db", "other")
    other = outsider.register(training.generated_evaluation_dataset("other"))
    assert outsider.engagement_id != first.engagement_id
    with pytest.raises(TrainingError, match="withdrawal_required"):
        first.purge_withdrawn()
    with (
        pytest.raises(TrainingError, match="engagement_mismatch"),
        first.engagement_guard(engagement_id=outsider.engagement_id),
    ):
        pytest.fail("unexpected grant")
    with (
        pytest.raises(TrainingError, match="stale_revision"),
        first.engagement_guard(DATASET, "stale", engagement_id=first.engagement_id),
    ):
        pytest.fail("unexpected grant")
    hold = first.withdraw()
    reopened = training.LocalTrainingStore(first.path, ORG)
    assert hold == reopened.withdraw() and not reopened.engagement_active()
    assert reopened.custody_inventory() == {"inputs": 6, "heldout": 7, "models": 1, "outputs": 2}
    denied = (
        lambda: reopened.catalog(),
        lambda: reopened.register(supplied),
        lambda: reopened.propose(DATASET),
        lambda: reopened.status(job["job_id"]),
        lambda: reopened.score_only(job_id=job["job_id"], output_dir=tmp_path / "forbidden"),
        lambda: reopened.frozen_evaluation(supplied["dataset_id"]),
        lambda: reopened.frozen_training(job["job_id"]),
        lambda: reopened.preview(DATASET),
    )
    for op in denied:
        with pytest.raises(TrainingError, match="engagement_withdrawn"):
            op()
    assert (
        training.registration_state(first.path, ORG, supplied["dataset_id"], digest(supplied))
        == "unavailable"
    )
    with (
        pytest.raises(TrainingError, match="engagement_withdrawn"),
        reopened.engagement_guard(engagement_id=hold["engagement_id"]),
    ):
        pytest.fail("unexpected grant")
    assert (
        outsider.engagement_active()
        and outsider.frozen_evaluation(other["dataset_id"])["readout"]["cohort_size"] == 3
    )
    assert (
        reopened.purge_withdrawn()
        == reopened.purge_withdrawn()
        == {"inputs": 0, "heldout": 0, "models": 0, "outputs": 0}
    )
    assert reopened.withdraw() == hold and not reopened.engagement_active()


def test_withdrawal_linearizes_against_an_existing_export_claim(tmp_path):
    store = enrolled(tmp_path)
    data = store.dataset_summary(DATASET)
    engaged = store.engagement_id
    started, finish = threading.Event(), threading.Event()

    def existing_claim():
        with store.engagement_guard(DATASET, data["dataset_revision"], engagement_id=engaged):
            started.set()
            assert finish.wait(3)

    with ThreadPoolExecutor(max_workers=2) as pool:
        active = pool.submit(existing_claim)
        assert started.wait(3)
        withdrawal = pool.submit(store.withdraw)
        assert not withdrawal.done()
        finish.set()
        active.result()
        withdrawal.result()
    with (
        pytest.raises(TrainingError, match="engagement_withdrawn"),
        store.engagement_guard(DATASET, data["dataset_revision"], engagement_id=engaged),
    ):
        pytest.fail("a queued claim was admitted after withdrawal")


def test_recovery_is_limited_to_original_confirmed_expired_job(tmp_path, monkeypatch):
    store = enrolled(tmp_path)
    job = store.propose(DATASET)

    def recovery(reviewer=REVIEWER, rev=None):
        return store.recover(
            job["job_id"], reviewer=reviewer, expected_revision=rev or job["revision"]
        )

    with pytest.raises(TrainingError, match="approved_interrupted_job_required"):
        recovery()
    original_fit = training.fit
    fits = []

    def counted(*args):
        fits.append(True)
        return original_fit(*args)

    monkeypatch.setattr(training, "fit", counted)
    with monkeypatch.context() as patch:

        def crash(*args):
            assert store.custody_inventory()["models"] == 1
            raise KeyboardInterrupt

        patch.setattr(training, "_score", crash)
        with pytest.raises(KeyboardInterrupt):
            execute(store, job)
    with pytest.raises(TrainingError, match="approved_interrupted_job_required"):
        recovery()
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE jobs SET lease_until=0")
    with pytest.raises(TrainingError, match="reviewer_denied"):
        recovery("U999")
    with pytest.raises(TrainingError, match="stale_revision"):
        recovery(rev="stale")
    completed = recovery()
    assert completed["status"] == "completed"
    assert len(fits) == 1 and completed["result"]["update_count"] == 2
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM fit_checkpoints").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 1
    with pytest.raises(TrainingError, match="approved_interrupted_job_required"):
        recovery()


def test_recovery_rejects_changed_checkpoint_before_loading_heldout(tmp_path, monkeypatch):
    store = enrolled(tmp_path)
    job = store.propose(DATASET)

    def interrupt(*_):
        raise KeyboardInterrupt

    with monkeypatch.context() as patch:
        patch.setattr(training, "_score", interrupt)
        with pytest.raises(KeyboardInterrupt):
            execute(store, job)
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE jobs SET lease_until=0")
        db.execute("UPDATE fit_checkpoints SET updates=updates+1")
    monkeypatch.setattr(training, "fit", lambda *_: pytest.fail("refit unverified checkpoint"))
    monkeypatch.setattr(store, "_cohort", lambda *_: pytest.fail("exposed heldout after mismatch"))
    result = store.recover(job["job_id"], reviewer=REVIEWER, expected_revision=job["revision"])
    assert (result["status"], result["error"], result["result"]) == (
        "failed",
        "checkpoint_unverified",
        None,
    )
    assert store.custody_inventory()["models"] == 1
    store.withdraw()
    assert not any(store.purge_withdrawn().values())


def test_preexisting_tenant_db_migration_does_not_claim_authority_or_proof_from_old_state(tmp_path):
    store = enrolled(tmp_path)
    job = execute(store, store.propose(DATASET))
    current = store.engagement_id
    # Earlier candidate's schema lacked dataset_scope and engagement, and lacked evidence bindings.
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE dataset_scope")
        db.execute("DROP TABLE engagement")
        db.execute("DROP TABLE fit_checkpoints")
        old = {
            k: v
            for k, v in job["result"].items()
            if k
            in (
                "readout",
                "artifact_digest",
                "update_count",
                "changed_weights",
                "heldout_count",
                "train_count",
            )
        }
        db.execute("UPDATE jobs SET result=?", (json.dumps(old),))
    with pytest.raises(TrainingError, match="scope_mismatch"):
        training.LocalTrainingStore(store.path, "foreign")
    with sqlite3.connect(store.path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='engagement'").fetchone()
    migrated = training.LocalTrainingStore(store.path, ORG)
    assert migrated.engagement_id != current
    assert not migrated.dataset_summary(DATASET)["can_train"]
    assert "train_cpu_text" not in migrated.dataset_summary(DATASET)["permitted_actions"]
    assert "score_frozen" not in migrated.dataset_summary(DATASET)["permitted_actions"]
    with pytest.raises(TrainingError):
        migrated.score_only(job_id=job["job_id"], output_dir=tmp_path / "inferred")
    migrated.register(generated_dataset(ORG, REVIEWER))
    with pytest.raises(TrainingError, match="readout_binding_required"):
        migrated.score_only(job_id=job["job_id"], output_dir=tmp_path / "assumed")
    hold = migrated.withdraw()
    assert training.LocalTrainingStore(store.path, ORG).withdraw() == hold


@pytest.mark.parametrize("erase_table", [False, True])
def test_partial_engagement_schema_cannot_reactivate_after_withdrawal(tmp_path, erase_table):
    store = enrolled(tmp_path)
    enrolled_revision = store.dataset_summary(DATASET)["dataset_revision"]
    store.withdraw()
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE engagement" if erase_table else "DELETE FROM engagement")
    assert training.registration_state(store.path, ORG, DATASET, enrolled_revision) == "unavailable"
    with pytest.raises(TrainingError, match="engagement_unavailable"):
        training.LocalTrainingStore(store.path, ORG)


def old_closeout(directory, organization, engagement, recorded):
    # A historical ledger predating later workflow columns still carries the durable identity.
    path = directory / "custody.sqlite3"
    path.touch(mode=0o600)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE closeout(singleton,organization,engagement,withdrawn_at)")
        if organization is not None:
            db.execute("INSERT INTO closeout VALUES(1,?,?,?)", (organization, engagement, recorded))
    return path


@pytest.mark.parametrize("recorded", [0, 12345])
def test_restored_training_file_cannot_restart_under_live_or_minimized_custody(tmp_path, recorded):
    private, other = tmp_path / "owner", tmp_path / "second"
    private.mkdir(mode=0o700)
    other.mkdir(mode=0o700)
    store = training.LocalTrainingStore(private / "training.sqlite3", ORG)
    tenant_b = training.LocalTrainingStore(other / "training.sqlite3", "org-b")
    second = tenant_b.register(generated_dataset("org-b", REVIEWER))
    dataset = store.register(generated_dataset(ORG, REVIEWER))
    evaluation = store.register(training.generated_evaluation_dataset(ORG))
    pending = store.propose(DATASET)
    backup = tmp_path / "backup.sqlite3"
    with sqlite3.connect(store.path) as live, sqlite3.connect(backup) as copy:
        live.backup(copy)
    engagement = store.engagement_id
    store.withdraw()
    old_closeout(private, ORG, engagement, recorded)
    with sqlite3.connect(backup) as copy, sqlite3.connect(store.path) as live:
        copy.backup(live)
        assert live.execute("SELECT withdrawn_at FROM engagement").fetchone() == (None,)

    restarted = training.LocalTrainingStore(store.path, ORG)
    assert not store.engagement_active() and not restarted.engagement_active()
    assert (
        training.registration_state(store.path, ORG, DATASET, dataset["dataset_revision"])
        == "unavailable"
    )
    for operation in (
        restarted.catalog,
        lambda: store.preview(DATASET),
        lambda: store.register(generated_dataset(ORG, REVIEWER)),
        lambda: execute(restarted, pending),
        lambda: restarted.frozen_evaluation(evaluation["dataset_id"]),
        lambda: restarted.score_only(
            dataset_id=evaluation["dataset_id"], output_dir=private / "repeat"
        ),
    ):
        with pytest.raises(TrainingError, match="engagement_withdrawn"):
            operation()
    with (
        pytest.raises(TrainingError, match="engagement_withdrawn"),
        restarted.engagement_guard(DATASET, dataset["dataset_revision"], engagement_id=engagement),
    ):
        pytest.fail("restored training store admitted an outbox claim")
    new_path = private / "replacement.sqlite3"
    with pytest.raises(TrainingError, match="engagement_unavailable"):
        training.LocalTrainingStore(new_path, ORG)
    assert not new_path.exists() and not (private / "repeat").exists()
    assert tenant_b.dataset_summary(DATASET) == second
    assert execute(tenant_b, tenant_b.propose(DATASET))["status"] == "completed"

    custodian = training.LocalTrainingStore.for_custody(
        store.path, ORG, expected_engagement=engagement
    )
    assert custodian.custody_inventory()["inputs"] == 6
    assert not custodian.engagement_active()
    assert custodian.withdraw()["engagement_id"] == engagement


@pytest.mark.parametrize("defect", ["empty_file", "opaque_bytes", "foreign", "wrong_id", "orphan"])
def test_unverifiable_closeout_denies_projection_and_active_handles(tmp_path, defect):
    store = enrolled(tmp_path)
    dataset_revision = store.dataset_summary(DATASET)["dataset_revision"]
    ledger = tmp_path / "custody.sqlite3"
    if defect in {"empty_file", "opaque_bytes"}:
        ledger.touch(mode=0o600)
        if defect == "opaque_bytes":
            ledger.write_bytes(b"invalid private ledger")
    elif defect == "orphan":
        ledger.with_name("custody.sqlite3-wal").touch(mode=0o600)
    else:
        old_closeout(
            tmp_path,
            "other" if defect == "foreign" else ORG,
            "invalid" if defect == "wrong_id" else store.engagement_id,
            1,
        )
    assert training.registration_state(store.path, ORG, DATASET, dataset_revision) == "unavailable"
    assert not store.engagement_active()
    with pytest.raises(TrainingError):
        training.LocalTrainingStore(store.path, ORG)
    with pytest.raises(TrainingError):
        store.catalog()


def test_empty_initialized_closeout_and_first_withdrawal_serialize_without_inverting_locks(
    tmp_path,
):
    store = enrolled(tmp_path)
    ledger = old_closeout(tmp_path, None, None, 0)
    identity = store.engagement_id
    with sqlite3.connect(ledger) as writer:
        writer.execute("BEGIN IMMEDIATE")
        assert store.engagement_active()
        manager = training.LocalTrainingStore.for_custody(store.path, ORG)
        manager.withdraw()
        writer.execute("INSERT INTO closeout VALUES(1,?,?,1)", (ORG, identity))
        assert not store.engagement_active()
    assert not training.LocalTrainingStore(store.path, ORG).engagement_active()


def test_readonly_onboarding_waits_for_the_actual_first_owner_schema_commit(tmp_path, monkeypatch):
    path = tmp_path / "training.sqlite3"
    assert training.onboarding_store_available(path, ORG)
    assert list(tmp_path.iterdir()) == []
    created, reading, allow_commit = threading.Event(), threading.Event(), threading.Event()
    original = training.LocalTrainingStore._db

    @contextmanager
    def pause_new_store(self, **kwargs):
        if kwargs.get("check_owner") is False:
            assert path.stat().st_size == 0
            created.set()
            assert allow_commit.wait(3)
        with original(self, **kwargs) as db:
            yield db

    def lookup():
        reading.set()
        return training.registration_state(path, ORG, DATASET, "unregistered")

    monkeypatch.setattr(training.LocalTrainingStore, "_db", pause_new_store)
    with ThreadPoolExecutor(max_workers=3) as pool:
        writer = pool.submit(training.LocalTrainingStore, path, ORG)
        assert created.wait(2)
        observer = pool.submit(lookup)
        assert reading.wait(2)
        preflight = pool.submit(training.onboarding_store_available, path, ORG)
        assert not observer.done()
        allow_commit.set()
        writer.result(timeout=2)
        assert observer.result(timeout=2) == "absent" and preflight.result(timeout=2)
    assert list(tmp_path.iterdir()) == [path]
    assert not training.onboarding_store_available(path, "foreign")


def test_initializer_lock_timeout_and_interrupted_empty_file_are_unavailable(tmp_path, monkeypatch):
    path = tmp_path / "training.sqlite3"
    with training._initialization_guard(path, creator=True):
        with monkeypatch.context() as patch:
            ticks = iter([0, 3])
            patch.setattr(time, "monotonic", lambda: next(ticks))
            assert not training.onboarding_store_available(path, ORG)
    assert list(tmp_path.iterdir()) == []
    path.touch(mode=0o600)
    assert not training.onboarding_store_available(path, ORG)
    assert training.registration_state(path, ORG, DATASET, "none") == "unavailable"
    with pytest.raises(TrainingError, match="uninitialized_private_store"):
        training.LocalTrainingStore(path, ORG)
    assert path.read_bytes() == b""
