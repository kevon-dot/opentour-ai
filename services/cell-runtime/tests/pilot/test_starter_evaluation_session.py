"""Actual local worker composition with generated, pre-existing CPU parameters."""

import hashlib
import json
import sqlite3
from dataclasses import asdict, replace

import pytest
from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot import model_registry
from opentour_cell.pilot import text_classifier as cpu
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.model_registry import ModelRegistry
from opentour_cell.pilot.starter_evaluation_session import RUNTIME, StarterEvaluationSession
from opentour_cell.pilot.starter_local_baseline import LocalStarterBaseline
from opentour_cell.pilot.starter_map_comparator import StarterMapComparator, initialize_journal
from test_intake_mcp import _write_private
from test_starter_evaluation_authority import files, seal

pytest_plugins = ("test_starter_evaluation_authority",)


@pytest.fixture
def installed(authorized):
    h, owner, cap, maps, authority, permission, execution, inspection, path, original = authorized
    directory = h.path.parent
    artifact = directory / "existing-cpu.json"
    # A deliberately weak, generated existing model. The factory does not fit it.
    _write_private(
        artifact,
        dict(
            schema_version="opentour.pilot.cpu-text-model.v1",
            profile=cpu.PROFILE,
            labels=["APPROVE", "MANUAL", "REVIEW"],
            dimensions=cpu.DIMENSIONS,
            weights=[[1] + [0] * (cpu.DIMENSIONS - 1), [0] * cpu.DIMENSIONS, [0] * cpu.DIMENSIONS],
        ),
    )
    revision = "sha256-" + hashlib.sha256(artifact.read_bytes()).hexdigest()
    permission = replace(permission, model_revision=revision)
    for filename, grant in (("execution-a.json", execution), ("inspection-a.json", inspection)):
        grant["permission"] = asdict(permission)
        seal(directory, filename, grant)
    registry = dict(
        schema_version=model_registry.VERSION,
        tenant=permission.tenant,
        models=[
            dict(
                model_ref=permission.model_ref,
                immutable_revision=revision,
                task="classification",
                input_schema="starter-structured-fields-v1",
                output_schema="action-output-v1",
                preprocessing_revision=cpu.PROFILE,
                route=model.Route.LOCAL.value,
                account_or_host="generated-local-host",
                region="local",
                credential_ref="generated-private-reference",
            )
        ],
        grants=[
            dict(
                actor=permission.evaluator,
                model_ref=permission.model_ref,
                immutable_revision=revision,
                purpose=model.Purpose.BASELINE.value,
                revision=permission.baseline_grant_revision,
                expires_at=permission.expires_at,
                permitted_fields=list(permission.input_fields),
                retention_until=permission.expires_at,
                fit_bound=None,
                benchmark_revisions=[],
            )
        ],
    )
    _write_private(directory / "model-registration.json", registry)
    key = directory / "existing-receipt-key"
    key.write_bytes(hashlib.sha256(b"generated-normal-worker-receipt-key").digest())
    key.chmod(0o600)
    runtime = dict(
        schema_version=RUNTIME,
        models={permission.model_ref: dict(artifact_file=artifact.name, receipt_key_file=key.name)},
    )
    _write_private(directory / "starter-evaluation-runtime.json", runtime)
    # Explicit fixture owner provisioning, never performed by the factory.
    initialize_journal(directory)
    worker = StarterEvaluationSession(owner.settings, authority.actor)
    reader = StarterEvaluationSession(owner.settings, owner.actor)
    return directory, worker, reader, permission, registry, runtime, authorized


def test_actual_factory_scores_same_frozen_inputs_and_retries_without_dispatch(
    installed, monkeypatch
):
    directory, worker, reader, p, _, _, authorized = installed
    calls = []
    comparator_inputs = []
    original_predict = cpu.predict
    original_comparator = StarterMapComparator.execute

    def predict(fitted, text):
        calls.append(text)
        return original_predict(fitted, text)

    monkeypatch.setattr(cpu, "predict", predict)
    monkeypatch.setattr(cpu, "fit", lambda *a, **k: pytest.fail("Baseline trained a model"))

    def compare(port, request, inputs):
        comparator_inputs.extend(inputs.values())
        return original_comparator(port, request, inputs)

    monkeypatch.setattr(StarterMapComparator, "execute", compare)
    result = worker.run(p.execution_ref)
    assert result["state"] == "scored", result
    assert len(calls) == 5
    assert all(set(json.loads(text)) == {"po", "total"} for text in calls)
    assert sorted(map(json.dumps, comparator_inputs)) == sorted(
        json.dumps(json.loads(text), sort_keys=True) for text in calls
    )
    assert result["metrics"]["model"]["exact_action"] == dict(numerator=1, denominator=5, value=0.2)
    assert result["metrics"]["comparator"]["exact_action"]["numerator"] == 5
    assert result["metrics"]["comparator"]["coverage"]["numerator"] == 3
    assert all(row["origin"] == "generated_starter" for row in result["rows"])
    assert authorized[-2].read_bytes() == authorized[-1]
    restarted = StarterEvaluationSession(worker.settings, worker.actor)
    assert restarted.run(p.execution_ref) == result
    assert len(calls) == 5
    for filename in ("starter-local-predictions.sqlite3", "starter-comparator-receipts.sqlite3"):
        with sqlite3.connect(directory / filename) as db:
            assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] == 1
    summary = reader.summary(p.execution_ref)
    assert summary["state"] == "scored" and summary["reserved"] == 5
    assert summary["metrics"] == result["metrics"]
    assert summary["model_revision"] == p.model_revision
    assert summary["comparator_revision"] == p.comparator_revision


def test_home_entries_only_disclose_current_owner_navigation_without_effects(
    installed, monkeypatch
):
    from opentour_cell.pilot import starter_evaluation_authority as authority_module
    from opentour_cell.pilot.starter_cases import StarterCases

    directory, worker, reader, p, *_ = installed
    before = files(directory)
    for target, name in (
        (authority_module, "_read_policy"),
        (StarterCases, "_db"),
        (ModelRegistry, "binding"),
        (LocalStarterBaseline, "observe"),
        (cpu, "predict"),
    ):
        monkeypatch.setattr(target, name, lambda *a, **k: pytest.fail("Home performed work"))
    entry = reader.entry(p.execution_ref)
    assert set(entry) == {"workflow_ref", "execution_ref", "revision"}
    assert reader.entries() == (entry,)
    assert worker.entries() == () and worker.entry(p.execution_ref) is None
    assert reader.entry("unknown-execution") is None
    assert files(directory) == before
    _write_private(directory / "starter-evaluation-withdrawn-inspection-a.json", {})
    assert reader.entries() == ()


def test_retained_owner_summary_never_requires_inference_or_execution(installed, monkeypatch):
    directory, worker, reader, p, registry, *_ = installed
    scored = worker.run(p.execution_ref)
    assert scored["state"] == "scored"
    registry["grants"] = []
    _write_private(directory / "model-registration.json", registry)
    _write_private(directory / "starter-evaluation-withdrawn-evaluation-a.json", {})
    before = files(directory)
    for target, name in (
        (ModelRegistry, "binding"),
        (LocalStarterBaseline, "lookup"),
        (LocalStarterBaseline, "execute"),
        (cpu, "predict"),
    ):
        monkeypatch.setattr(
            target, name, lambda *a, **k: pytest.fail("Inspection executed model work")
        )
    assert reader.summary(p.execution_ref)["metrics"] == scored["metrics"]
    assert files(directory) == before
    with pytest.raises(IntakeError):
        worker.run(p.execution_ref)
    with pytest.raises(IntakeError):
        worker.summary(p.execution_ref)


def test_selected_entry_revision_is_fenced_before_summary_body(installed, monkeypatch):
    from opentour_cell.pilot import starter_evaluation_authority as authority_module

    directory, _, reader, p, _, _, authorized = installed
    old = reader.entry(p.execution_ref)
    inspection = authorized[7].copy()
    inspection["grant_ref"] = "inspection-b"
    seal(directory, "inspection-a.json", inspection)
    before = files(directory)
    monkeypatch.setattr(authority_module, "_read_policy", lambda *a: pytest.fail("Stale body read"))
    with pytest.raises(IntakeError):
        reader.summary(p.execution_ref, expected_revision=old["revision"])
    assert files(directory) == before


def test_actual_all_review_model_preserves_undefined_wrong_automatic(installed):
    directory, worker, reader, p, registry, _, authorized = installed
    path = directory / "existing-cpu.json"
    artifact = json.loads(path.read_text())
    artifact["weights"] = [
        [0] * cpu.DIMENSIONS,
        [0] * cpu.DIMENSIONS,
        [1] + [0] * (cpu.DIMENSIONS - 1),
    ]
    _write_private(path, artifact)
    revision = "sha256-" + hashlib.sha256(path.read_bytes()).hexdigest()
    p = replace(p, model_revision=revision)
    registry["models"][0]["immutable_revision"] = revision
    registry["grants"][0]["immutable_revision"] = revision
    _write_private(directory / "model-registration.json", registry)
    for filename, grant in (
        ("execution-a.json", authorized[6]),
        ("inspection-a.json", authorized[7]),
    ):
        grant["permission"] = asdict(p)
        seal(directory, filename, grant)
    result = worker.run(p.execution_ref)
    assert result["state"] == "scored"
    assert result["metrics"]["model"]["exact_action"]["numerator"] == 2
    assert result["metrics"]["model"]["coverage"] == dict(numerator=0, denominator=5, value=0)
    assert result["metrics"]["model"]["wrong_automatic"] == dict(
        numerator=0, denominator=0, value=None
    )
    assert reader.summary(p.execution_ref)["metrics"] == result["metrics"]


@pytest.mark.parametrize("missing", ["runtime", "key", "artifact", "model", "grant", "journal"])
def test_missing_runtime_holds_existing_freeze_without_provisioning(
    installed, monkeypatch, missing
):
    directory, worker, _, p, registry, *_ = installed
    paths = {
        "runtime": "starter-evaluation-runtime.json",
        "key": "existing-receipt-key",
        "artifact": "existing-cpu.json",
        "model": "model-registration.json",
        "journal": "starter-comparator-receipts.sqlite3",
    }
    if missing == "grant":
        registry["grants"] = []
        _write_private(directory / "model-registration.json", registry)
    else:
        (directory / paths[missing]).unlink()
    before = files(directory)
    monkeypatch.setattr(cpu, "predict", lambda *a: pytest.fail("Missing runtime dispatched"))
    result = worker.run(p.execution_ref)
    assert result["state"] == "frozen_unscored"
    assert result["freeze_revision"] == p.freeze_revision
    assert result["result_ref"] is result["result_digest"] is None and "metrics" not in result
    assert files(directory) == before


@pytest.mark.parametrize(
    "bad", ["traversal", "symlink", "extra", "schema", "foreign_registry", "fields"]
)
def test_invalid_runtime_or_model_scope_has_zero_effect(installed, monkeypatch, bad):
    directory, worker, _, p, registry, runtime, _ = installed
    if bad == "traversal":
        runtime["models"][p.model_ref]["artifact_file"] = "../existing-cpu.json"
    elif bad == "symlink":
        (directory / "alias.json").symlink_to(directory / "existing-cpu.json")
        runtime["models"][p.model_ref]["artifact_file"] = "alias.json"
    elif bad == "extra":
        runtime["models"][p.model_ref]["actor"] = p.evaluator
    elif bad == "schema":
        registry["models"][0]["input_schema"] = "unregistered-input-v1"
    elif bad == "foreign_registry":
        registry["tenant"] = "foreign-tenant"
    else:
        registry["grants"][0]["permitted_fields"] = ["po"]
    _write_private(directory / "starter-evaluation-runtime.json", runtime)
    _write_private(directory / "model-registration.json", registry)
    before = files(directory)
    monkeypatch.setattr(cpu, "predict", lambda *a: pytest.fail("Invalid runtime dispatched"))
    assert worker.run(p.execution_ref)["state"] == "frozen_unscored"
    assert files(directory) == before


def test_owner_and_unselected_execution_do_not_inherit_worker_authority(installed):
    directory, worker, reader, p, *_ = installed
    before = files(directory)
    with pytest.raises(IntakeError):
        reader.run(p.execution_ref)
    with pytest.raises(IntakeError):
        worker.run("unadmitted-rerun")
    with pytest.raises(IntakeError):
        StarterEvaluationSession(
            worker.settings, IntakePrincipal("foreign", p.evaluator, "evaluator")
        )
    assert files(directory) == before


def test_started_unknown_execution_cannot_be_replaced_with_fresh_unscored(installed, monkeypatch):
    directory, worker, _, p, *_ = installed
    monkeypatch.setattr(LocalStarterBaseline, "execute", lambda *a: None)
    first = worker.run(p.execution_ref)
    assert (
        first["state"] == "held"
        and first["reason_code"] == "existing_execution_requires_reconciliation"
    )
    (directory / "starter-evaluation-runtime.json").unlink()
    before = files(directory)
    assert worker.run(p.execution_ref) == first
    assert files(directory) == before


def test_completed_execution_missing_runtime_stays_held_until_retained_inspection(installed):
    directory, worker, reader, p, *_ = installed
    result = worker.run(p.execution_ref)
    assert result["state"] == "scored"
    (directory / "existing-receipt-key").unlink()
    before = files(directory)
    assert worker.run(p.execution_ref)["state"] == "held"
    assert reader.summary(p.execution_ref)["state"] == "scored"
    assert files(directory) == before


def test_closed_wal_comparator_holds_without_creating_sidecars(installed):
    directory, worker, _, p, *_ = installed
    path = directory / "starter-comparator-receipts.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA journal_mode=WAL")
    db.close()
    assert not (directory / (path.name + "-wal")).exists()
    before = files(directory)
    assert worker.run(p.execution_ref)["state"] == "frozen_unscored"
    assert files(directory) == before


@pytest.mark.parametrize("empty", [False, True])
def test_corrupt_existing_execution_state_is_held_not_fresh(installed, empty):
    directory, worker, _, p, *_ = installed
    (directory / "starter-evaluation-runtime.json").unlink()
    journal = directory / "starter-evaluations.sqlite3"
    if empty:
        sqlite3.connect(journal).close()
    else:
        journal.write_bytes(b"generated invalid existing journal")
    journal.chmod(0o600)
    before = files(directory)
    result = worker.run(p.execution_ref)
    assert result["state"] == "held" and result["reason_code"] == "execution_state_unavailable"
    assert files(directory) == before


@pytest.mark.parametrize("grant", ["evaluation-a", "inspection-a"])
def test_withdrawal_at_inspection_boundary_cannot_disclose_old_result(
    installed, monkeypatch, grant
):
    from opentour_cell.pilot.starter_score_inspection import StarterScoreInspection

    directory, _, reader, p, *_ = installed
    original = StarterScoreInspection.summary

    def withdraw(*args):
        result = original(*args)
        _write_private(directory / f"starter-evaluation-withdrawn-{grant}.json", {})
        return result

    monkeypatch.setattr(StarterScoreInspection, "summary", withdraw)
    if grant == "inspection-a":
        with pytest.raises(IntakeError):
            reader.summary(p.execution_ref)
    else:
        # Independent retained owner permission survives execution withdrawal.
        assert reader.summary(p.execution_ref)["state"] == "frozen_unscored"


@pytest.mark.parametrize("grant", ["execution", "source", "case"])
def test_current_withdrawal_stops_actual_prediction_batch(installed, monkeypatch, grant):
    directory, worker, _, p, *_ = installed
    files_by_grant = {
        "execution": "starter-evaluation-withdrawn-evaluation-a.json",
        "source": "starter-intake-withdrawn-policy-scope.json",
        "case": "starter-case-withdrawn-case-permission.json",
    }
    calls = []

    def withdraw(*args):
        calls.append(args)
        _write_private(directory / files_by_grant[grant], {})
        return "APPROVE"

    monkeypatch.setattr(cpu, "predict", withdraw)
    with pytest.raises(IntakeError):
        worker.run(p.execution_ref)
    assert len(calls) == 1
    with sqlite3.connect(directory / "starter-local-predictions.sqlite3") as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='receipts'").fetchall()
    with sqlite3.connect(directory / "starter-evaluations.sqlite3") as db:
        assert db.execute("SELECT result FROM evaluations").fetchone()[0] is None


def test_explicit_new_execution_reuses_original_freeze(installed, monkeypatch):
    directory, worker, _, p, _, _, authorized = installed
    calls = []
    original_predict = cpu.predict
    monkeypatch.setattr(
        cpu, "predict", lambda fitted, text: (calls.append(text), original_predict(fitted, text))[1]
    )
    first = worker.run(p.execution_ref)
    execution = authorized[6].copy()
    second = replace(p, execution_ref="execution-b", revision="evaluation-b")
    execution.update(grant_ref=second.revision, permission=asdict(second))
    seal(directory, "execution-b.json", execution)
    selection_path = directory / "starter-evaluation-selection.json"
    selection = json.loads(selection_path.read_text())
    selection["executions"][second.execution_ref] = "execution-b.json"
    _write_private(selection_path, selection)
    result = worker.run(second.execution_ref)
    assert first["state"] == result["state"] == "scored"
    assert first["freeze_revision"] == result["freeze_revision"] == p.freeze_revision
    assert first["result_ref"] != result["result_ref"]
    assert len(calls) == 10 and authorized[-2].read_bytes() == authorized[-1]
