"""Generated deterministic inference executes over actual frozen owner-reviewed cases."""

import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace

import pytest
from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.model_registry import ModelRegistry
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.starter_cases import StarterCases
from opentour_cell.pilot.starter_scoring import (
    EvaluationPermission,
    PredictionReceipt,
    StarterScoring,
)
from test_model_registry import write
from test_starter_cases import OWNER, PLAN, WORK, WORKER, Confirmation, adjudicate_all, seven

EVALUATOR = IntakePrincipal(OWNER.tenant, "generated-evaluator", "untrusted")


class Confirm(Confirmation):
    @contextmanager
    def guard(self, actor, work, *, purpose):
        if purpose == "evaluate":
            if not self.allowed or actor != EVALUATOR or work != self.scope.work:
                raise IntakeError("evaluation_scope_denied")
            yield self.scope
        else:
            with super().guard(actor, work, purpose=purpose) as scope:
                yield scope


class Authority:
    def __init__(self, permission):
        self.permission = permission
        self.allowed = True

    @contextmanager
    def guard(self, actor, work, execution_ref):
        if not self.allowed or actor != EVALUATOR or work != WORK:
            raise IntakeError("evaluation_permission_denied")
        if execution_ref != self.permission.execution_ref:
            raise IntakeError("rerun_permission_required")
        yield self.permission

    def revalidate(self, actor, work, permission):
        if not self.allowed or actor != EVALUATOR or work != WORK or permission != self.permission:
            raise IntakeError("evaluation_permission_changed")


class Port:
    def __init__(self, reference, revision, binding=None):
        self.reference, self.revision, self.binding = reference, revision, binding
        self.calls = []
        self.receipts = {}
        self.lost_ack = self.unknown = self.all_review = self.missing = False
        self.before = None

    def observe(self, binding, purpose):
        assert purpose is model.Purpose.BASELINE and binding == self.binding
        return model.Observation(
            binding,
            "generated-local-observation",
            int(time.time()) - 1,
            int(time.time()) + 300,
            binding.immutable_revision,
            True,
            False,
            False,
            False,
            False,
            False,
            {},
        )

    def lookup(self, request):
        return self.receipts.get(request.operation_ref)

    def execute(self, request, inputs):
        assert all(key.startswith("starter-case-") for key in inputs)
        assert all(set(fields) == {"category"} for fields in inputs.values())
        assert all(isinstance(fields["category"], str) for fields in inputs.values())
        self.calls.append((request, inputs))
        if self.before:
            self.before()
        values = {}
        for key, fields in inputs.items():
            category = fields["category"]
            if self.missing and category == "FREIGHT":
                continue
            if self.all_review or category == "REVIEW":
                values[key] = {"status": "review"}
            else:
                label = (
                    "SOFTWARE"
                    if self.reference == "selected-model" and category == "FREIGHT"
                    else category
                )
                values[key] = {"status": "automatic", "label": label}
        receipt = PredictionReceipt(request, "prediction-" + digest(asdict(request))[:32], values)
        if self.unknown:
            raise OSError("generated lost request outcome")
        self.receipts[request.operation_ref] = receipt
        if self.lost_ack:
            raise OSError("generated lost acknowledgment")
        return receipt


@pytest.fixture
def scored_setup(tmp_path):
    tmp_path.chmod(0o700)
    auth = Confirm()
    plan = replace(
        PLAN,
        baseline_ref="selected-model",
        evaluator_ref=EVALUATOR.actor,
        evaluated_model_ref="selected-model",
    )
    auth.scope = replace(auth.scope, frozen_plan=plan)
    cases = StarterCases(tmp_path, auth)
    proposed = seven()
    cases.propose(OWNER, WORK, proposed)
    adjudicate_all(cases, proposed)
    splits = {
        c.family_id: "excluded"
        if c.case_id == "mixed"
        else "development"
        if c.case_id in {"utilities", "tax"}
        else "reserved"
        for c in proposed
    }
    frozen = cases.freeze(OWNER, WORK, splits=splits, plan=plan, expected_revision=7)
    path = tmp_path / "model-registration.json"
    binding = model.ModelBinding(
        OWNER.tenant,
        "selected-model",
        "immutable-model-v1",
        "classification",
        "category-input-v1",
        "action-output-v1",
        "identity-preprocessing-v1",
        model.Route.LOCAL,
        "generated-host",
        "local",
        "local-credential-ref",
    )
    registered = asdict(binding)
    registered.pop("tenant")
    document = {
        "schema_version": "opentour.pilot.model-registration.v1",
        "tenant": OWNER.tenant,
        "models": [registered],
        "grants": [
            {
                "actor": EVALUATOR.actor,
                "model_ref": binding.model_ref,
                "immutable_revision": binding.immutable_revision,
                "purpose": model.Purpose.BASELINE,
                "revision": "baseline-grant-v1",
                "expires_at": int(time.time()) + 300,
                "retention_until": int(time.time()) + 600,
                "permitted_fields": ["category"],
                "fit_bound": None,
                "benchmark_revisions": [],
            }
        ],
    }
    write(path, document)
    permission = EvaluationPermission(
        OWNER.tenant,
        WORK.workflow_ref,
        WORK.resource.engagement_epoch,
        WORK.run_ref,
        EVALUATOR.actor,
        "explicit-evaluation-1",
        "evaluation-grant-v1",
        frozen.reference,
        frozen.revision,
        digest(asdict(plan)),
        "declared-rule-v1",
        binding.model_ref,
        binding.immutable_revision,
        "baseline-grant-v1",
        ("category",),
        4,
        int(time.time()) + 300,
    )
    authority = Authority(permission)
    baseline = Port(binding.model_ref, binding.immutable_revision, binding)
    comparator = Port(plan.comparator_ref, "declared-rule-v1")
    scorer = StarterScoring(
        tmp_path, cases, authority, ModelRegistry(path, OWNER.tenant), baseline, comparator
    )
    return scorer, authority, auth, document, path


def run(setup):
    scorer, authority, *_ = setup
    return scorer.evaluate(EVALUATOR, WORK, authority.permission.execution_ref)


def test_four_reserved_cases_actual_predictions_metrics_and_restart(scored_setup):
    scorer, authority, _, _, _ = scored_setup
    before = scorer.cases.path.read_bytes()
    result = run(scored_setup)
    assert result["state"] == "scored"
    metrics = result["metrics"]
    for arm, exact, wrong in (("model", 3, 1), ("comparator", 4, 0)):
        assert metrics[arm]["exact_action"] == {
            "numerator": exact,
            "denominator": 4,
            "value": exact / 4,
        }
        assert metrics[arm]["coverage"] == {"numerator": 3, "denominator": 4, "value": 0.75}
        assert metrics[arm]["wrong_automatic"]["numerator"] == wrong
        assert metrics[arm]["wrong_automatic"]["denominator"] == 3
    assert metrics["paired"] == {
        "fixed": 1,
        "regressed": 0,
        "denominator": 4,
        "excluded_missing": 0,
    }
    assert result["unresolved"] == 1 and result["excluded"] == 1
    assert scorer.baseline.calls[0][1] == scorer.comparator.calls[0][1]
    assert "owner_adjudicated_target" not in json.dumps(scorer.baseline.calls[0][1])
    assert all(row["origin"] == "generated_starter" for row in result["rows"])
    reopened = StarterScoring(
        scorer.path.parent,
        scorer.cases,
        authority,
        scorer.registry,
        scorer.baseline,
        scorer.comparator,
    )
    assert reopened.evaluate(EVALUATOR, WORK, authority.permission.execution_ref) == result
    assert len(scorer.baseline.calls) == len(scorer.comparator.calls) == 1
    assert scorer.cases.path.read_bytes() == before


def test_separate_rerun_authority_and_unknown_outcome_never_resends(scored_setup):
    scorer, authority, *_ = scored_setup
    scorer.baseline.unknown = True
    assert run(scored_setup)["reason_code"] == "prediction_outcome_unknown"
    assert run(scored_setup)["result_digest"] is None
    assert len(scorer.baseline.calls) == 1 and not scorer.comparator.calls
    with pytest.raises(IntakeError, match="rerun_permission_required"):
        scorer.evaluate(EVALUATOR, WORK, "unapproved-new-key")
    authority.permission = replace(authority.permission, execution_ref="explicit-rerun-2")
    scorer.baseline.unknown = False
    assert run(scored_setup)["state"] == "scored"
    assert len(scorer.baseline.calls) == 2


def test_lost_ack_is_independently_reconciled_and_all_review_denominator(scored_setup):
    scorer, *_ = scored_setup
    scorer.baseline.lost_ack = True
    scorer.baseline.all_review = True
    result = run(scored_setup)
    assert result["metrics"]["model"]["coverage"]["numerator"] == 0
    assert result["metrics"]["model"]["wrong_automatic"] == {
        "numerator": 0,
        "denominator": 0,
        "value": None,
    }
    assert result["metrics"]["model"]["exact_action"]["numerator"] == 1
    assert run(scored_setup) == result and len(scorer.baseline.calls) == 1


@pytest.mark.parametrize(
    "fault", ["missing_model", "revoked", "expired", "wrong_revision", "authoring_only"]
)
def test_no_inference_permission_stays_unscored(scored_setup, fault):
    scorer, _, _, document, path = scored_setup
    if fault == "missing_model":
        scorer.baseline = None
    elif fault == "revoked":
        document["grants"] = []
    elif fault == "expired":
        document["grants"][0]["expires_at"] = 1
    elif fault == "wrong_revision":
        document["grants"][0]["immutable_revision"] = "other-model"
    else:
        document["grants"][0]["purpose"] = model.Purpose.AUTHORING
    write(path, document)
    result = run(scored_setup)
    assert result["state"] == "frozen_unscored" and result["result_digest"] is None
    assert scorer.baseline is None or not scorer.baseline.calls
    assert not scorer.comparator.calls


def test_missing_prediction_is_distinct_from_review(scored_setup):
    scorer, *_ = scored_setup
    scorer.baseline.missing = True
    result = run(scored_setup)
    assert result["metrics"]["model"]["missing"] == 1
    assert result["metrics"]["model"]["exact_action"]["numerator"] == 3
    assert result["metrics"]["model"]["coverage"]["numerator"] == 2
    assert result["metrics"]["paired"]["denominator"] == 3


def test_materializer_owner_and_forged_role_cannot_read_evaluator_projection(scored_setup):
    scorer, *_ = scored_setup
    for actor in (OWNER, WORKER, replace(EVALUATOR, tenant="foreign")):
        with pytest.raises(IntakeError):
            scorer.cases.evaluation_inputs(actor, WORK)
    value = scorer.cases.evaluation_inputs(EVALUATOR, WORK)
    assert len(value.cases) == 4 and "target" not in json.dumps(asdict(value))
    with pytest.raises(IntakeError):
        scorer.cases.evaluation_targets(EVALUATOR, WORK, freeze_revision="wrong-freeze")


def test_revoked_model_after_execution_cannot_persist_prediction_or_score(scored_setup):
    scorer, _, _, document, path = scored_setup

    def revoke():
        document["grants"] = []
        write(path, document)

    scorer.baseline.before = revoke
    with pytest.raises(IntakeError):
        run(scored_setup)
    with scorer._db() as db:
        assert db.execute("SELECT receipt FROM predictions").fetchall()[0][0] is None
        assert db.execute("SELECT result FROM evaluations").fetchall()[0][0] is None
    assert not scorer.comparator.calls


def test_concurrent_same_intent_has_one_prediction_execution_per_arm(scored_setup):
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(scored_setup), range(2)))
    scorer, *_ = scored_setup
    assert all(r["state"] in {"scored", "frozen_unscored"} for r in results)
    assert run(scored_setup)["state"] == "scored"
    assert len(scorer.baseline.calls) == len(scorer.comparator.calls) == 1


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("link", [False, True])
def test_interrupted_journal_holds_before_writer_or_dispatch(scored_setup, suffix, existing, link):
    scorer, *_ = scored_setup
    if existing:
        with scorer._db(write=True):
            pass
    sidecar = scorer.path.with_name(scorer.path.name + suffix)
    if link:
        sidecar.symlink_to(scorer.path.with_name("missing-restore-file"))
    else:
        sidecar.write_bytes(b"generated incomplete restore")
        sidecar.chmod(0o600)
    original = scorer.path.read_bytes() if existing else None
    with pytest.raises(IntakeError, match="starter_execution_recovery_required"):
        run(scored_setup)
    assert scorer.path.exists() == existing and os.path.lexists(sidecar)
    if existing:
        assert scorer.path.read_bytes() == original
    assert not scorer.baseline.calls and not scorer.comparator.calls


@pytest.mark.parametrize("state", ["empty", "partial_schema", "malformed", "dangling"])
def test_restored_main_journal_is_never_silently_initialized(scored_setup, state):
    scorer, *_ = scored_setup
    if state == "dangling":
        scorer.path.symlink_to(scorer.path.with_name("missing-backup"))
    elif state == "malformed":
        scorer.path.write_bytes(b"generated damaged backup")
    else:
        with sqlite3.connect(scorer.path) as db:
            if state == "partial_schema":
                db.execute("CREATE TABLE evaluations(ref TEXT,definition TEXT,result TEXT)")
        db.close()
    if state != "dangling":
        scorer.path.chmod(0o600)
    before = scorer.path.read_bytes() if state != "dangling" else None
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        run(scored_setup)
    assert os.path.lexists(scorer.path)
    if before is not None:
        assert scorer.path.read_bytes() == before
    assert not scorer.baseline.calls and not scorer.comparator.calls


def test_cached_prediction_must_match_independent_adapter_receipt(scored_setup):
    scorer, *_ = scored_setup
    run(scored_setup)
    with sqlite3.connect(scorer.path) as db:
        reference, raw = db.execute("SELECT ref,receipt FROM predictions LIMIT 1").fetchone()
        body = json.loads(raw)
        body["outputs"][next(iter(body["outputs"]))] = {"status": "automatic", "label": "WRONG"}
        db.execute("UPDATE predictions SET receipt=? WHERE ref=?", (json.dumps(body), reference))
    with pytest.raises(IntakeError, match="prediction_receipt_changed"):
        run(scored_setup)
    assert len(scorer.baseline.calls) == len(scorer.comparator.calls) == 1


def test_stable_case_handles_hide_author_supplied_identifiers(scored_setup):
    scorer, *_ = scored_setup
    result = run(scored_setup)
    request, inputs = scorer.baseline.calls[0]
    assert request.input_digest == digest(inputs)
    assert not set(inputs) & {row["case_id"] for row in result["rows"]}
    assert all(len(handle) == len("starter-case-") + 40 for handle in inputs)


@pytest.mark.parametrize(
    "value", [{"owner_answer": "FREIGHT"}, ["FREIGHT"], float("nan"), float("inf")]
)
def test_declared_scalar_fields_cannot_conceal_target_objects(tmp_path, value):
    from test_starter_cases import case

    tmp_path.chmod(0o700)
    cases = StarterCases(tmp_path, Confirm())
    proposal = replace(case("SOFTWARE"), decision_inputs=(("category", value),))
    with pytest.raises(IntakeError, match="invalid_starter_inputs"):
        cases.propose(OWNER, WORK, (proposal,))


def test_stored_scalar_violation_is_rejected_even_with_updated_local_seal(scored_setup):
    from opentour_cell.pilot.operating_point import canonical

    scorer, *_ = scored_setup
    with sqlite3.connect(scorer.cases.path) as db:
        ref, raw = db.execute("SELECT ref,body FROM packs").fetchone()
        body = json.loads(raw)
        body["cases"][0]["decision_inputs"] = [["category", {"target": "FREIGHT"}]]
        db.execute(
            "UPDATE packs SET body=?,seal=? WHERE ref=?", (canonical(body), digest(body), ref)
        )
        db.execute("UPDATE freezes SET seal=? WHERE ref=?", (digest(body), ref))
    with pytest.raises(IntakeError, match="invalid_starter_inputs"):
        scorer.cases.evaluation_inputs(EVALUATOR, WORK)


def test_lookup_revocation_is_checked_before_executing_model(scored_setup, monkeypatch):
    scorer, _, _, document, path = scored_setup
    lookup = scorer.baseline.lookup

    def withdraw(request):
        document["grants"] = []
        write(path, document)
        return lookup(request)

    monkeypatch.setattr(scorer.baseline, "lookup", withdraw)
    with pytest.raises(IntakeError):
        run(scored_setup)
    assert not scorer.baseline.calls and not scorer.comparator.calls


def test_automatic_review_response_cannot_count_as_automation(scored_setup, monkeypatch):
    scorer, *_ = scored_setup
    execute = scorer.baseline.execute

    def malformed(request, inputs):
        receipt = execute(request, inputs)
        receipt = replace(
            receipt, outputs={key: {"status": "automatic", "label": "REVIEW"} for key in inputs}
        )
        scorer.baseline.receipts[request.operation_ref] = receipt
        return receipt

    monkeypatch.setattr(scorer.baseline, "execute", malformed)
    with pytest.raises(IntakeError, match="prediction_abstention_contract_mismatch"):
        run(scored_setup)
    assert not scorer.comparator.calls


def test_receipt_must_be_durably_queryable_before_first_score(scored_setup, monkeypatch):
    scorer, *_ = scored_setup
    execute = scorer.baseline.execute

    def unrecorded(request, inputs):
        receipt = execute(request, inputs)
        scorer.baseline.receipts.clear()
        return receipt

    monkeypatch.setattr(scorer.baseline, "execute", unrecorded)
    first = run(scored_setup)
    assert first["state"] == "frozen_unscored" and first["result_digest"] is None
    assert run(scored_setup) == first
    assert len(scorer.baseline.calls) == 1 and not scorer.comparator.calls


def test_lookup_withdraws_evaluator_capability_before_any_inference(scored_setup, monkeypatch):
    scorer, authority, *_ = scored_setup

    def withdraw(_):
        authority.allowed = False
        return None

    monkeypatch.setattr(scorer.baseline, "lookup", withdraw)
    with pytest.raises(IntakeError, match="evaluation_permission_changed"):
        run(scored_setup)
    assert not scorer.baseline.calls and not scorer.comparator.calls


def test_observation_callback_cannot_bypass_current_evaluator_permission(scored_setup, monkeypatch):
    scorer, authority, *_ = scored_setup
    lookup, observe = scorer.baseline.lookup, scorer.baseline.observe
    ready = False

    def found(request):
        nonlocal ready
        ready = True
        return lookup(request)

    def withdrawn(*args):
        observation = observe(*args)
        if ready:
            authority.allowed = False
        return observation

    monkeypatch.setattr(scorer.baseline, "lookup", found)
    monkeypatch.setattr(scorer.baseline, "observe", withdrawn)
    with pytest.raises(IntakeError, match="evaluation_permission_changed"):
        run(scored_setup)
    assert not scorer.baseline.calls and not scorer.comparator.calls
