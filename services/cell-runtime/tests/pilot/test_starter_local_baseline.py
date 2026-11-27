"""Generated pre-existing CPU parameters; no fit or provider is claimed."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest
from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot import text_classifier as cpu
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_local_baseline import LocalStarterBaseline
from opentour_cell.pilot.starter_scoring import PredictionRequest
from test_model_registry import write
from test_starter_scoring import EVALUATOR, WORK, run, scored_setup  # noqa: F401


@pytest.fixture
def local_setup(scored_setup):  # noqa: F811
    scorer, authority, _, document, path = scored_setup
    labels = ["SOFTWARE", "SUPPLIES", "REVIEW"]
    weights = [[0] * cpu.DIMENSIONS for _ in labels]
    for label, row in zip(labels, weights, strict=True):
        for feature, _ in cpu.feature_signature(label):
            if feature:
                row[feature] = 2
    artifact = path.parent / "selected-cpu-model.json"
    write(
        artifact,
        {
            "schema_version": "opentour.pilot.cpu-text-model.v1",
            "profile": cpu.PROFILE,
            "labels": labels,
            "dimensions": cpu.DIMENSIONS,
            "weights": weights,
        },
    )
    key = path.parent / "starter-prediction-key"
    key.write_bytes(hashlib.sha256(b"generated-fixture-receipt-key").digest())
    key.chmod(0o600)
    revision = "sha256-" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    document["models"][0].update(
        immutable_revision=revision,
        input_schema="starter-text-category-v1",
        preprocessing_revision=cpu.PROFILE,
    )
    document["grants"][0]["immutable_revision"] = revision
    write(path, document)
    authority.permission = replace(authority.permission, model_revision=revision)
    baseline = LocalStarterBaseline(
        path.parent,
        actor=EVALUATOR,
        work=WORK,
        permission=authority.permission,
        authority=authority,
        registry=scorer.registry,
        cases=scorer.cases,
        artifact=artifact,
        receipt_key=key,
        input_field="category",
    )
    scorer.baseline = baseline
    return scored_setup, baseline


def request_inputs(setup):
    scorer, authority, *_ = setup
    frozen = scorer.cases.evaluation_inputs(EVALUATOR, WORK)
    inputs = {
        "starter-case-" + digest([frozen.freeze_revision, c.case_id])[:40]: dict(c.decision_inputs)
        for c in frozen.cases
    }
    p = authority.permission
    definition = {
        "permission": asdict(p),
        "input_digest": digest(inputs),
        "comparator_revision": p.comparator_revision,
    }
    return PredictionRequest(
        "starter-prediction-" + digest([definition, "model"])[:40],
        p.execution_ref,
        "model",
        frozen.freeze_revision,
        digest(inputs),
        p.model_ref,
        p.model_revision,
    ), inputs


def test_existing_cpu_model_scores_and_restart_reconciles_without_prediction(
    local_setup, monkeypatch
):
    setup, baseline = local_setup
    scorer, authority, *_ = setup
    initial = {p.name: p.read_bytes() for p in baseline.path.parent.iterdir()}
    binding = scorer.registry.binding(baseline.reference)
    observed = baseline.observe(binding, model.Purpose.BASELINE)
    assert observed.resolved_revision == baseline.revision
    assert not observed.training_supported
    assert {p.name: p.read_bytes() for p in baseline.path.parent.iterdir()} == initial
    result = run(setup)
    assert result["state"] == "scored"
    assert result["metrics"]["model"]["exact_action"]["numerator"] == 3
    assert result["metrics"]["model"]["coverage"]["numerator"] == 3
    assert result["metrics"]["paired"]["fixed"] == 1
    assert all(row["origin"] == "generated_starter" for row in result["rows"])

    def forbidden(*args):
        raise AssertionError("replay must only reconcile the durable receipt")

    monkeypatch.setattr(cpu, "predict", forbidden)
    assert run(setup) == result
    request, inputs = request_inputs(setup)
    assert baseline.lookup(request) == baseline.execute(request, inputs)
    with sqlite3.connect(baseline.path) as db:
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    raw = baseline.path.read_bytes()
    assert b"owner_adjudicated_target" not in raw and b"decision_inputs" not in raw
    assert authority.allowed


def test_two_claimants_execute_one_local_prediction_batch(local_setup, monkeypatch):
    setup, baseline = local_setup
    request, inputs = request_inputs(setup)
    calls = []
    original = cpu.predict

    def observe(fitted, text):
        calls.append(text)
        return original(fitted, text)

    monkeypatch.setattr(cpu, "predict", observe)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: baseline.execute(request, inputs), range(2)))
    assert receipts[0] == receipts[1] == baseline.lookup(request)
    assert len(calls) == 4


@pytest.mark.parametrize("change", ["input", "request", "model", "key", "grant", "actor"])
def test_invalid_scope_has_zero_prediction_or_receipt_effect(local_setup, monkeypatch, change):
    setup, baseline = local_setup
    _, authority, _, document, registry_path = setup
    request, inputs = request_inputs(setup)
    if change == "input":
        inputs[next(iter(inputs))]["category"] = "unauthorized changed input"
    elif change == "request":
        request = replace(request, execution_ref="unadmitted-rerun")
    elif change == "model":
        baseline.artifact.write_bytes(b"{}")
    elif change == "key":
        baseline.receipt_key.chmod(0o644)
    elif change == "grant":
        document["grants"] = []
        write(registry_path, document)
    else:
        authority.allowed = False
    calls = []
    monkeypatch.setattr(cpu, "predict", lambda *args: calls.append(args))
    with pytest.raises((IntakeError, ValueError)):
        baseline.execute(request, inputs)
    assert calls == [] and not baseline.path.exists()


def test_receipt_corruption_denied_and_lookup_has_zero_disk_changes(local_setup):
    setup, baseline = local_setup
    request, inputs = request_inputs(setup)
    expected = baseline.execute(request, inputs)
    before = {p.name: p.read_bytes() for p in baseline.path.parent.iterdir()}
    assert baseline.lookup(request) == expected
    assert {p.name: p.read_bytes() for p in baseline.path.parent.iterdir()} == before
    with sqlite3.connect(baseline.path) as db:
        row = db.execute("SELECT body FROM receipts").fetchone()
        body = json.loads(row[0])
        body["outputs"][next(iter(inputs))] = {"status": "automatic", "label": "ALTERED"}
        db.execute("UPDATE receipts SET body=?", (canonical(body),))
    with pytest.raises(IntakeError, match="local_baseline_unavailable"):
        baseline.lookup(request)


def test_withdrawal_during_prediction_rolls_back_receipt_and_stops_batch(local_setup, monkeypatch):
    setup, baseline = local_setup
    request, inputs = request_inputs(setup)
    authority = setup[1]
    calls = []

    def withdraw(*args):
        calls.append(args)
        authority.allowed = False
        return "SOFTWARE"

    monkeypatch.setattr(cpu, "predict", withdraw)
    with pytest.raises(IntakeError):
        baseline.execute(request, inputs)
    assert len(calls) == 1
    with sqlite3.connect(baseline.path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='receipts'").fetchall()


def test_lost_ack_recovers_actual_durable_receipt(local_setup, monkeypatch):
    setup, baseline = local_setup
    original = baseline.execute
    calls = []

    def lose_ack(request, inputs):
        calls.append(request)
        original(request, inputs)
        raise OSError("generated acknowledgment loss")

    monkeypatch.setattr(baseline, "execute", lose_ack)
    result = run(setup)
    assert result["state"] == "scored" and len(calls) == 1
    assert run(setup) == result and len(calls) == 1
