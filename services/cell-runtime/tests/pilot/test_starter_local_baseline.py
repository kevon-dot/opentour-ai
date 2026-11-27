"""Generated pre-existing CPU parameters; no fit or provider is claimed."""

import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from pathlib import Path

import pytest
import test_starter_scoring as scoring_tests
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


@pytest.mark.parametrize("pending", [True, False])
def test_current_model_can_reconnect_pending_freeze_but_not_replace_pinned_model(
    tmp_path, monkeypatch, pending
):
    original = scoring_tests.replace

    def frozen_plan(obj, **changes):
        if obj is scoring_tests.PLAN:
            changes.update(
                baseline_ref="baseline-pending" if pending else "another-model",
                evaluated_model_ref=None if pending else "another-model",
            )
        return original(obj, **changes)

    monkeypatch.setattr(scoring_tests, "replace", frozen_plan)
    setup = scoring_tests.scored_setup.__wrapped__(tmp_path)
    _, baseline = local_setup.__wrapped__(setup)
    request, inputs = request_inputs(setup)
    if pending:
        before = setup[0].cases.path.read_bytes()
        receipt = baseline.execute(request, inputs)
        assert baseline.lookup(request) == receipt
        assert setup[0].cases.path.read_bytes() == before
        assert run(setup)["state"] == "scored"
    else:
        with pytest.raises(IntakeError):
            baseline.execute(request, inputs)
        assert not baseline.path.exists()


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
def test_ambiguous_missing_journal_is_held_without_new_prediction(local_setup, monkeypatch, suffix):
    setup, baseline = local_setup
    request, inputs = request_inputs(setup)
    baseline.execute(request, inputs)
    baseline.path.rename(baseline.path.with_suffix(".backup"))
    sidecar = Path(str(baseline.path) + suffix)
    sidecar.write_bytes(b"generated-pre-existing-restore-state")
    sidecar.chmod(0o600)
    before = {p.name: p.read_bytes() for p in baseline.path.parent.iterdir()}
    calls = []
    monkeypatch.setattr(cpu, "predict", lambda *args: calls.append(args))
    with pytest.raises(IntakeError):
        baseline.lookup(request)
    with pytest.raises(IntakeError):
        baseline.execute(request, inputs)
    assert calls == []
    assert {p.name: p.read_bytes() for p in baseline.path.parent.iterdir()} == before


@pytest.fixture
def structured_setup(tmp_path, monkeypatch, request):
    original = scoring_tests.seven
    facts = {
        "software": "annual_subscription",
        "freight": "carrier_freight",
        "supplies": "office_supply",
        "utilities": "utility_service",
        "tax": "tax_remittance",
        "review": "annual_subscription",
        "mixed": "mixed_invoice",
    }

    def proposed():
        return tuple(
            replace(
                item,
                decision_inputs=(
                    ("category", facts[item.case_id]),
                    (
                        "po",
                        None
                        if item.case_id == "review"
                        else request.param
                        if hasattr(request, "param")
                        else "generated-reference",
                    ),
                ),
            )
            for item in original()
        )

    monkeypatch.setattr(scoring_tests, "seven", proposed)
    setup = scoring_tests.scored_setup.__wrapped__(tmp_path)
    _, baseline = local_setup.__wrapped__(setup)
    scorer, authority, _, document, path = setup
    authority.permission = replace(authority.permission, input_fields=("category", "po"))
    baseline.permission = authority.permission
    baseline.input_field = None
    document["models"][0]["input_schema"] = "starter-structured-fields-v1"
    document["grants"][0]["permitted_fields"] = ["category", "po"]
    write(path, document)
    return setup, baseline


def test_structured_profile_receives_exact_frozen_facts_and_replays(structured_setup, monkeypatch):
    setup, baseline = structured_setup
    request, inputs = request_inputs(setup)
    calls = []
    original = cpu.predict

    def observe(fitted, text):
        calls.append(text)
        return original(fitted, text)

    monkeypatch.setattr(cpu, "predict", observe)
    receipt = baseline.execute(request, inputs)
    assert sorted(calls) == sorted(canonical(fields) for fields in inputs.values())
    assert {row["category"] for row in inputs.values()} == {
        "annual_subscription",
        "carrier_freight",
        "office_supply",
    }
    assert sum(row["po"] is None for row in inputs.values()) == 1
    assert not any(token in " ".join(calls) for token in ("target", "suggestion", "REVIEW"))
    assert len(calls) == 4 and set(receipt.outputs) == set(inputs)
    assert baseline.lookup(request) == baseline.execute(request, inputs) == receipt
    assert len(calls) == 4


@pytest.mark.parametrize("structured_setup", [2**63 - 1, -(2**63) + 1], indirect=True)
def test_structured_profile_preserves_frozen_exact_integer_domain(structured_setup, monkeypatch):
    setup, baseline = structured_setup
    request, inputs = request_inputs(setup)
    seen = []
    original = cpu.predict

    def observe(fitted, text):
        seen.append(json.loads(text))
        return original(fitted, text)

    monkeypatch.setattr(cpu, "predict", observe)
    assert set(baseline.execute(request, inputs).outputs) == set(inputs)
    assert seen == list(inputs.values())
    assert sum(row["po"] is None for row in seen) == 1
    assert all(
        row["po"] is None or (type(row["po"]) is int and abs(row["po"]) == 2**63 - 1)
        for row in seen
    )


@pytest.mark.parametrize("denial", ["field_grant", "schema", "substitution", "scope"])
def test_structured_profile_requires_full_current_schema_and_field_grants(
    structured_setup, monkeypatch, denial
):
    setup, baseline = structured_setup
    _, _, _, document, path = setup
    request, inputs = request_inputs(setup)
    if denial == "field_grant":
        document["grants"][0]["permitted_fields"] = ["category"]
        write(path, document)
    elif denial == "schema":
        document["models"][0]["input_schema"] = "starter-text-category-v1"
        write(path, document)
    elif denial == "substitution":
        inputs[next(iter(inputs))]["po"] = "substituted-reference"
    else:
        baseline.permission = replace(baseline.permission, input_fields=("category",))
    calls = []
    monkeypatch.setattr(cpu, "predict", lambda *args: calls.append(args))
    with pytest.raises(IntakeError):
        baseline.execute(request, inputs)
    assert calls == [] and not baseline.path.exists()
