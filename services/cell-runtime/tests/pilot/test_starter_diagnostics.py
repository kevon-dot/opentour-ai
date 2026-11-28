"""Generated post-miss lineage: actual frozen cases, actual scoring journal, no provider.

A deterministic local model misses the owner-edited FREIGHT case. The exposed case
becomes diagnostic, and the revised model's claim-bearing run must be pre-registered
on independently authored, unexposed successor families. Generated fixtures prove
software behavior only, not customer accuracy or recurrence.
"""

import json
import sqlite3
import time
from contextlib import closing, contextmanager
from dataclasses import asdict, replace

import pytest
from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.model_registry import ModelRegistry
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_cases import ProposedCase, StarterCases
from opentour_cell.pilot.starter_coverage_receipt import owner_coverage_receipt
from opentour_cell.pilot.starter_diagnostics import RevisedChoice, StarterDiagnostics
from opentour_cell.pilot.starter_scoring import (
    EvaluationPermission,
    PredictionReceipt,
    StarterScoring,
    _metrics,
)
from test_model_registry import write
from test_starter_cases import OWNER, PLAN, WORK, Confirmation, adjudicate_all, seven
from test_starter_scoring import Port

EVALUATOR = IntakePrincipal(OWNER.tenant, "generated-evaluator", "untrusted")
WORK_V2 = replace(WORK, run_ref="run-2", revisions=("policy-v2", "settings-v1"))
PLAN_E = replace(
    PLAN,
    baseline_ref="selected-model",
    evaluator_ref=EVALUATOR.actor,
    evaluated_model_ref="selected-model",
)
FIELDS = ("category", "variant")
SPLITS_P = {
    "software": "reserved",
    "freight": "reserved",
    "supplies": "reserved",
    "review": "reserved",
    "utilities": "development",
    "tax": "development",
    "mixed": "excluded",
}
MISSES_FREIGHT = {"FREIGHT": "SOFTWARE"}
REVISED = RevisedChoice("model", "selected-model", "immutable-model-v2")


class Lineage:
    """Independent generated owner/evaluator grants for a predecessor and a successor."""

    def __init__(self):
        base = replace(Confirmation().scope, frozen_plan=PLAN_E)
        self.allowed = True
        self.scopes = {
            WORK: base,
            WORK_V2: replace(
                base,
                work=WORK_V2,
                map_ref="confirmed-map-v2",
                confirmation_ref="owner-confirmation-2",
                policy_revision="policy-v2",
            ),
        }

    @contextmanager
    def guard(self, actor, work, *, purpose):
        scope = self.scopes.get(work)
        expected = EVALUATOR if purpose == "evaluate" else OWNER
        if not self.allowed or scope is None or actor != expected:
            raise IntakeError("confirmation_denied")
        yield scope


class Authority:
    def __init__(self):
        self.permissions = {}

    @contextmanager
    def guard(self, actor, work, execution_ref):
        permission = self.permissions.get(execution_ref)
        if actor != EVALUATOR or permission is None or permission.run_ref != work.run_ref:
            raise IntakeError("rerun_permission_required")
        yield permission

    def revalidate(self, actor, work, permission):
        if actor != EVALUATOR or self.permissions.get(permission.execution_ref) != permission:
            raise IntakeError("evaluation_permission_changed")


class Arm(Port):
    """Deterministic local arm: each category maps to one action; REVIEW abstains."""

    def __init__(self, reference, revision, binding=None, labels=None, omitted=()):
        super().__init__(reference, revision, binding)
        self.labels = labels or {}
        self.omitted = set(omitted)

    def execute(self, request, inputs):
        self.calls.append((request, inputs))
        values = {}
        for key, fields in inputs.items():
            if fields["category"] in self.omitted:
                continue
            label = self.labels.get(fields["category"], fields["category"])
            values[key] = (
                {"status": "review"}
                if label == "REVIEW"
                else {"status": "automatic", "label": label}
            )
        receipt = PredictionReceipt(request, "prediction-" + digest(asdict(request))[:32], values)
        self.receipts[request.operation_ref] = receipt
        return receipt


def binding(revision):
    return model.ModelBinding(
        OWNER.tenant,
        "selected-model",
        revision,
        "classification",
        "category-input-v1",
        "action-output-v1",
        "identity-preprocessing-v1",
        model.Route.LOCAL,
        "generated-host",
        "local",
        "local-credential-ref",
    )


class Harness:
    def __init__(self, path):
        self.path = path
        self.lineage = Lineage()
        self.cases = StarterCases(path, self.lineage)
        self.authority = Authority()
        self.registry = path / "model-registration.json"
        self.diagnostics = StarterDiagnostics(path, self.cases)

    def register(self, revision):
        selected = binding(revision)
        registered = asdict(selected)
        registered.pop("tenant")
        write(
            self.registry,
            {
                "schema_version": "opentour.pilot.model-registration.v1",
                "tenant": OWNER.tenant,
                "models": [registered],
                "grants": [
                    {
                        "actor": EVALUATOR.actor,
                        "model_ref": selected.model_ref,
                        "immutable_revision": revision,
                        "purpose": model.Purpose.BASELINE,
                        "revision": "baseline-grant-" + revision,
                        "expires_at": int(time.time()) + 300,
                        "retention_until": int(time.time()) + 600,
                        "permitted_fields": list(FIELDS),
                        "fit_bound": None,
                        "benchmark_revisions": [],
                    }
                ],
            },
        )
        return selected

    def score(self, work, execution_ref, revision, labels, omitted=(), comparator=None):
        frozen = self.cases.evaluation_inputs(EVALUATOR, work)
        selected = self.register(revision)
        self.authority.permissions[execution_ref] = EvaluationPermission(
            OWNER.tenant,
            work.workflow_ref,
            work.resource.engagement_epoch,
            work.run_ref,
            EVALUATOR.actor,
            execution_ref,
            "evaluation-grant-" + execution_ref,
            frozen.freeze_ref,
            frozen.freeze_revision,
            digest(asdict(frozen.plan)),
            "declared-rule-v1",
            selected.model_ref,
            selected.immutable_revision,
            "baseline-grant-" + revision,
            FIELDS,
            16,
            int(time.time()) + 300,
        )
        scorer = StarterScoring(
            self.path,
            self.cases,
            self.authority,
            ModelRegistry(self.registry, OWNER.tenant),
            Arm(selected.model_ref, revision, selected, labels, omitted),
            Arm(PLAN_E.comparator_ref, "declared-rule-v1", labels=comparator),
        )
        return scorer.evaluate(EVALUATOR, work, execution_ref)

    def freeze_predecessor(self):
        cases = seven()
        self.cases.propose(OWNER, WORK, cases)
        adjudicate_all(self.cases, cases)
        return self.cases.freeze(OWNER, WORK, splits=SPLITS_P, plan=PLAN_E, expected_revision=7)

    def freeze_successor(self, generated, *, counterexamples=(), splits=None, work=WORK_V2):
        self.cases.propose(OWNER, work, generated)
        revision = 0
        for item in counterexamples:
            revision = self.cases.add_counterexample(
                OWNER,
                work,
                case=item,
                expected_revision=revision,
                operation_ref="owner-" + item.case_id,
            )
        for row in self.cases.review(OWNER, work)["cases"]:
            revision = self.cases.adjudicate(
                OWNER,
                work,
                case_id=row["case_id"],
                target=row["suggestion"],
                reason="Owner checked the cited policy",
                expected_revision=revision,
                operation_ref="successor-label-" + row["case_id"],
            )
        families = {c.family_id for c in (*generated, *counterexamples)}
        return self.cases.freeze(
            OWNER,
            work,
            splits=splits or dict.fromkeys(families, "reserved"),
            plan=PLAN_E,
            expected_revision=revision,
        )


def fresh(name, category, *, origin="generated_starter"):
    return ProposedCase(
        name,
        "fresh-" + name,
        "fresh-" + name,
        "route",
        (("category", category), ("variant", name)),
        category,
        "Independently authored successor case",
        "policy-section-1",
        origin,
    )


SUCCESSOR = (
    fresh("carrier-invoice", "FREIGHT"),
    fresh("license-renewal", "SOFTWARE"),
    fresh("paper-order", "SUPPLIES"),
    fresh("unknown-vendor", "REVIEW"),
)
OWNER_VARIANT = replace(
    fresh("owner-freight", "FREIGHT"), rationale="Owner counterexample written after the miss"
)


@pytest.fixture
def harness(tmp_path):
    tmp_path.chmod(0o700)
    return Harness(tmp_path)


@pytest.fixture
def exposed(harness):
    harness.freeze_predecessor()
    result = harness.score(WORK, "exec-p-1", "immutable-model-v1", MISSES_FREIGHT)
    assert result["metrics"]["model"]["exact_action"]["numerator"] == 3
    return harness, harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1")


def journal(harness):
    with closing(sqlite3.connect(harness.path / "starter-evaluations.sqlite3")) as db:
        return dict(db.execute("SELECT ref,result FROM evaluations").fetchall())


def lineage_files(harness):
    return sorted(
        p.name
        for p in harness.path.iterdir()
        if p.name.startswith(("starter-exposure-", "starter-reassessment-"))
    )


def test_post_miss_lineage_witness_keeps_old_manifest_and_needs_fresh_families(harness):
    frozen_p = harness.freeze_predecessor()
    receipt_before = owner_coverage_receipt(harness.cases, OWNER, WORK)
    original = harness.score(WORK, "exec-p-1", "immutable-model-v1", MISSES_FREIGHT)
    pack_before = harness.cases.path.read_bytes()
    record = harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1")
    assert record["diagnostic_set"] == [
        {
            "case_id": "freight",
            "family_id": "freight",
            "origin": "generated_starter",
            "arm": "model",
            "outcome": "miss",
            "observed": {"status": "automatic", "label": "SOFTWARE"},
        }
    ]
    assert record["exposed_families"] == ["freight", "review", "software", "supplies"]
    assert record["result_ref"] == original["result_ref"]
    assert record["exposed_choice"]["model"] == {
        "reference": "selected-model",
        "revision": "immutable-model-v1",
    }
    assert (record["evidence_origin"], record["training_credit"]) == (
        "expert_reviewed_generated_starter",
        0,
    )
    assert "target" not in json.dumps(record["diagnostic_set"])
    assert harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1") == record
    assert harness.cases.path.read_bytes() == pack_before

    # A rerun of the revised model on the exposed freeze is only a diagnosis.
    rerun = harness.score(WORK, "exec-p-2", "immutable-model-v2", {})
    assert rerun["metrics"]["model"]["exact_action"]["numerator"] == 4
    diagnosis = harness.diagnostics.assessment(EVALUATOR, WORK, execution_ref="exec-p-2")
    assert diagnosis["role"] == "exposed_diagnostic" and diagnosis["claim"] is None
    assert diagnosis["diagnosis"] == [
        {"case_id": "freight", "arm": "model", "original": "miss", "now": "fixed"}
    ]
    assert "metrics" not in diagnosis and diagnosis["training_credit"] == 0
    first = harness.diagnostics.assessment(EVALUATOR, WORK, execution_ref="exec-p-1")
    assert first["role"] == "original_exposure"
    assert first["claim"] == "original_starter_result_unchanged"

    # Fresh, independently authored successor families; one owner example after the miss.
    harness.freeze_successor(SUCCESSOR, counterexamples=(OWNER_VARIANT,))
    registration = harness.diagnostics.place_reassessment(
        EVALUATOR,
        WORK,
        WORK_V2,
        exposure_ref=record["exposure_ref"],
        execution_ref="exec-s-1",
        revised=REVISED,
    )
    assert registration["protected_families"] == [
        "fresh-carrier-invoice",
        "fresh-license-renewal",
        "fresh-paper-order",
        "fresh-unknown-vendor",
    ]
    assert registration["exposure_informed_families"] == ["fresh-owner-freight"]
    assert registration["revised"] is True
    assert (registration["evidence_origin"], registration["training_credit"]) == (
        "expert_reviewed_generated_starter",
        0,
    )
    assert registration["predecessor"]["freeze_revision"] == frozen_p.revision
    assert (
        harness.diagnostics.place_reassessment(
            EVALUATOR,
            WORK,
            WORK_V2,
            exposure_ref=record["exposure_ref"],
            execution_ref="exec-s-1",
            revised=REVISED,
        )
        == registration
    )
    successor = harness.score(WORK_V2, "exec-s-1", "immutable-model-v2", {})
    assert successor["metrics"]["model"]["exact_action"]["denominator"] == 5
    claim = harness.diagnostics.assessment(EVALUATOR, WORK_V2, execution_ref="exec-s-1")
    assert claim["role"] == "protected_reassessment"
    assert claim["choice"]["model"] == {
        "reference": "selected-model",
        "revision": "immutable-model-v2",
    }
    assert claim["claim"]["protected_cases"] == 4
    assert claim["claim"]["metrics"]["model"]["exact_action"] == {
        "numerator": 4,
        "denominator": 4,
        "value": 1.0,
    }
    assert claim["claim"]["exposure_informed_families"] == ["fresh-owner-freight"]
    assert claim["evidence_origin"] == "expert_reviewed_generated_starter"
    assert claim["training_credit"] == 0

    # The old manifest, the old result and the starter label never changed.
    assert owner_coverage_receipt(harness.cases, OWNER, WORK) == receipt_before
    assert harness.cases.freeze(OWNER, WORK, splits=SPLITS_P, plan=PLAN_E, expected_revision=7) == (
        frozen_p
    )
    stored = json.loads(journal(harness)["exec-p-1"])
    assert canonical(stored) == canonical(original)
    assert len(lineage_files(harness)) == 2


def test_exposure_requires_the_evaluator_a_scored_result_and_a_miss(harness):
    harness.freeze_predecessor()
    with pytest.raises(IntakeError, match="starter_scored_result_required"):
        harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1")
    harness.score(WORK, "exec-p-1", "immutable-model-v2", {})
    with pytest.raises(IntakeError, match="starter_miss_not_observed"):
        harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1")
    harness.score(WORK, "exec-p-2", "immutable-model-v1", MISSES_FREIGHT)
    for actor in (OWNER, replace(EVALUATOR, tenant="foreign"), replace(EVALUATOR, actor="other")):
        with pytest.raises(IntakeError):
            harness.diagnostics.place_exposure(actor, WORK, execution_ref="exec-p-2")
    assert lineage_files(harness) == []
    harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-2")
    assert len(lineage_files(harness)) == 1


@pytest.mark.parametrize(
    "tamper",
    ["output", "answer_key", "receipt_ref", "digest_field", "metrics", "family", "dropped_row"],
)
def test_tampered_scored_result_cannot_be_exposed(harness, tamper):
    harness.freeze_predecessor()
    harness.score(WORK, "exec-p-1", "immutable-model-v1", MISSES_FREIGHT)
    path = harness.path / "starter-evaluations.sqlite3"
    with closing(sqlite3.connect(path)) as db, db:
        result = json.loads(db.execute("SELECT result FROM evaluations").fetchone()[0])
        freight = next(r for r in result["rows"] if r["case_id"] == "freight")
        resealed = tamper not in {"output", "receipt_ref", "digest_field"}
        if tamper == "digest_field":
            result["result_digest"] = "0" * 64
        elif tamper == "output":
            freight["model"] = {"status": "automatic", "label": "FREIGHT"}
        elif tamper == "receipt_ref":
            result["predictions"]["model"]["receipt_ref"] = "prediction-forged"
        elif tamper == "answer_key":
            freight["expected"]["target"] = "SOFTWARE"
        elif tamper == "metrics":
            result["metrics"]["model"]["exact_action"]["numerator"] = 4
        elif tamper == "family":
            freight["family_id"] = "renamed-family"
        else:
            result["rows"].remove(freight)
            result["metrics"] = _metrics(result["rows"])
        if resealed:
            # A re-sealed result still has to match the owner's freeze and its own rows.
            body = {k: v for k, v in result.items() if k not in {"result_ref", "result_digest"}}
            result["result_digest"] = digest(body)
            result["result_ref"] = "starter-result-" + result["result_digest"][:40]
        db.execute("UPDATE evaluations SET result=?", (canonical(result),))
    with pytest.raises(IntakeError, match="starter_scored_result_invalid"):
        harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1")
    assert lineage_files(harness) == []


def test_revoked_evaluator_during_publication_leaves_no_exposure(harness, monkeypatch):
    harness.freeze_predecessor()
    harness.score(WORK, "exec-p-1", "immutable-model-v1", MISSES_FREIGHT)
    original = harness.cases.evaluation_inputs
    calls = []

    def revoke_before_publication(actor, work):
        calls.append(work)
        if len(calls) == 2:
            harness.lineage.allowed = False
        return original(actor, work)

    monkeypatch.setattr(harness.cases, "evaluation_inputs", revoke_before_publication)
    with pytest.raises(IntakeError):
        harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1")
    assert len(calls) == 2 and lineage_files(harness) == []


@pytest.mark.parametrize("change", ["seal", "content"])
def test_edited_exposure_record_is_rejected(exposed, change):
    harness, record = exposed
    path = harness.path / (record["exposure_ref"] + ".json")
    edited = json.loads(path.read_bytes())
    edited["exposed_families"] = ["freight"]
    if change == "content":
        body = {k: v for k, v in edited.items() if k != "seal"}
        edited["seal"] = digest(body)
    path.write_text(canonical(edited))
    harness.freeze_successor(SUCCESSOR)
    reason = "starter_lineage_record_invalid" if change == "seal" else "starter_exposure_invalid"
    with pytest.raises(IntakeError, match=reason):
        harness.diagnostics.place_reassessment(
            EVALUATOR,
            WORK,
            WORK_V2,
            exposure_ref=record["exposure_ref"],
            execution_ref="exec-s-1",
            revised=REVISED,
        )


def test_reassessment_must_be_registered_before_any_run_on_the_successor(exposed):
    harness, record = exposed
    harness.freeze_successor(SUCCESSOR)
    arguments = dict(exposure_ref=record["exposure_ref"], revised=REVISED)
    with pytest.raises(IntakeError, match="reassessment_must_precede_execution"):
        harness.diagnostics.place_reassessment(
            EVALUATOR, WORK, WORK_V2, execution_ref="exec-p-1", **arguments
        )
    harness.score(WORK_V2, "exec-s-early", "immutable-model-v2", {})
    with pytest.raises(IntakeError, match="successor_already_exposed"):
        harness.diagnostics.place_reassessment(
            EVALUATOR, WORK, WORK_V2, execution_ref="exec-s-1", **arguments
        )
    early = harness.diagnostics.assessment(EVALUATOR, WORK_V2, execution_ref="exec-s-early")
    assert early["role"] == "unplaced" and early["claim"] is None
    assert lineage_files(harness) == [record["exposure_ref"] + ".json"]


@pytest.mark.parametrize(
    ("revised", "reason"),
    [
        (RevisedChoice("model", "other-model", "immutable-model-v2"), "revised_choice_not_frozen"),
        (RevisedChoice("comparator", "other-rule", "v2"), "revised_choice_not_frozen"),
        (RevisedChoice("trainer", "selected-model", "v2"), "invalid_revised_choice"),
    ],
)
def test_revised_choice_must_be_admitted_by_the_successor_plan(exposed, revised, reason):
    harness, record = exposed
    harness.freeze_successor(SUCCESSOR)
    with pytest.raises(IntakeError, match=reason):
        harness.diagnostics.place_reassessment(
            EVALUATOR,
            WORK,
            WORK_V2,
            exposure_ref=record["exposure_ref"],
            execution_ref="exec-s-1",
            revised=revised,
        )


def test_successor_must_be_a_different_freeze_in_the_same_workflow(exposed):
    harness, record = exposed
    other_workflow = replace(WORK_V2, workflow_ref="workflow-2")
    for successor in (WORK, other_workflow):
        with pytest.raises(IntakeError, match="starter_successor_required"):
            harness.diagnostics.place_reassessment(
                EVALUATOR,
                WORK,
                successor,
                exposure_ref=record["exposure_ref"],
                execution_ref="exec-s-1",
                revised=REVISED,
            )


def test_exposed_input_under_a_new_family_name_is_refused_at_successor_freeze(exposed):
    harness, _ = exposed
    renamed = ProposedCase(
        "freight-again",
        "renamed-family",
        "renamed-template",
        "route",
        (("category", "FREIGHT"),),
        "FREIGHT",
        "The exposed input under a new family name",
        "policy-section-1",
    )
    with pytest.raises(IntakeError, match="starter_family_already_assigned"):
        harness.freeze_successor((renamed, *SUCCESSOR))


@pytest.mark.parametrize("field", ["exposed_families", "exposed_inputs"])
def test_exposure_itself_refuses_a_listed_family_or_input(exposed, field):
    """Defense in depth beyond the core reservation table, checked on exposure content."""
    harness, record = exposed
    harness.freeze_successor(SUCCESSOR)
    successor = harness.cases.evaluation_inputs(EVALUATOR, WORK_V2)
    paper = next(c for c in successor.cases if c.family_id == "fresh-paper-order")
    listed = (
        "fresh-paper-order" if field == "exposed_families" else digest(dict(paper.decision_inputs))
    )
    widened = record | {field: [*record[field], listed]}
    with pytest.raises(IntakeError, match="exposed_family_reused"):
        harness.diagnostics._reassessment_record(EVALUATOR, widened, WORK_V2, "exec-s-1", REVISED)
    assert harness.diagnostics._reassessment_record(EVALUATOR, record, WORK_V2, "exec-s-1", REVISED)


def test_owner_counterexamples_alone_cannot_support_a_protected_reassessment(exposed):
    harness, record = exposed
    harness.freeze_successor(
        (fresh("generated-dev", "SUPPLIES"),),
        counterexamples=(OWNER_VARIANT,),
        splits={"fresh-generated-dev": "development", "fresh-owner-freight": "reserved"},
    )
    with pytest.raises(IntakeError, match="independent_reserved_families_required"):
        harness.diagnostics.place_reassessment(
            EVALUATOR,
            WORK,
            WORK_V2,
            exposure_ref=record["exposure_ref"],
            execution_ref="exec-s-1",
            revised=REVISED,
        )


def test_only_the_registered_execution_and_exact_choice_carry_a_claim(exposed):
    harness, record = exposed
    harness.freeze_successor(SUCCESSOR)
    pinned = RevisedChoice("model", "selected-model", "immutable-model-v3")
    registration = harness.diagnostics.place_reassessment(
        EVALUATOR,
        WORK,
        WORK_V2,
        exposure_ref=record["exposure_ref"],
        execution_ref="exec-s-1",
        revised=pinned,
    )
    with pytest.raises(IntakeError, match="starter_reassessment_conflict"):
        harness.diagnostics.place_reassessment(
            EVALUATOR,
            WORK,
            WORK_V2,
            exposure_ref=record["exposure_ref"],
            execution_ref="exec-s-1",
            revised=REVISED,
        )
    # The run used a different model revision than the one fixed before it started.
    harness.score(WORK_V2, "exec-s-1", "immutable-model-v2", {})
    mismatched = harness.diagnostics.assessment(EVALUATOR, WORK_V2, execution_ref="exec-s-1")
    assert mismatched["role"] == "unregistered_choice" and mismatched["claim"] is None
    harness.score(WORK_V2, "exec-s-2", "immutable-model-v2", {})
    later = harness.diagnostics.assessment(EVALUATOR, WORK_V2, execution_ref="exec-s-2")
    assert later["role"] == "unregistered_choice" and later["claim"] is None
    assert registration["revised_choice"] == asdict(pinned)


def test_lineage_reads_are_evaluator_only(exposed):
    harness, record = exposed
    for actor in (OWNER, replace(EVALUATOR, actor="other")):
        with pytest.raises(IntakeError):
            harness.diagnostics.exposure(actor, WORK, record["exposure_ref"])
        with pytest.raises(IntakeError):
            harness.diagnostics.assessment(actor, WORK, execution_ref="exec-p-1")
    with pytest.raises(IntakeError, match="starter_lineage_record_invalid"):
        harness.diagnostics.exposure(EVALUATOR, WORK, "starter-exposure-not-a-reference")


def test_missing_and_still_missed_outputs_stay_diagnostic(harness):
    harness.freeze_predecessor()
    harness.score(WORK, "exec-p-1", "immutable-model-v1", MISSES_FREIGHT, omitted={"SUPPLIES"})
    record = harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1")
    assert [(d["case_id"], d["arm"], d["outcome"]) for d in record["diagnostic_set"]] == [
        ("freight", "model", "miss"),
        ("supplies", "model", "missing"),
    ]
    assert record["diagnostic_set"][1]["observed"] == {"status": "missing"}
    harness.score(WORK, "exec-p-2", "immutable-model-v2", MISSES_FREIGHT)
    harness.score(WORK, "exec-p-3", "immutable-model-v3", {}, omitted={"SUPPLIES"})
    nows = {}
    for execution in ("exec-p-2", "exec-p-3"):
        value = harness.diagnostics.assessment(EVALUATOR, WORK, execution_ref=execution)
        assert value["role"] == "exposed_diagnostic" and value["claim"] is None
        nows[execution] = [(d["case_id"], d["now"]) for d in value["diagnosis"]]
    assert nows == {
        "exec-p-2": [("freight", "still_miss"), ("supplies", "fixed")],
        "exec-p-3": [("freight", "fixed"), ("supplies", "still_missing")],
    }


def test_exposure_record_is_bound_to_its_own_name(exposed):
    harness, record = exposed
    source = harness.path / (record["exposure_ref"] + ".json")
    copied = "starter-exposure-" + "0" * 40
    (harness.path / (copied + ".json")).write_bytes(source.read_bytes())
    (harness.path / (copied + ".json")).chmod(0o600)
    with pytest.raises(IntakeError, match="starter_lineage_record_invalid"):
        harness.diagnostics.exposure(EVALUATOR, WORK, copied)
    with pytest.raises(IntakeError, match="starter_lineage_record_invalid"):
        harness.diagnostics.assessment(EVALUATOR, WORK, execution_ref="exec-p-1")


def test_protected_claim_holds_when_its_sealed_exposure_is_edited(exposed):
    harness, record = exposed
    harness.freeze_successor(SUCCESSOR)
    harness.diagnostics.place_reassessment(
        EVALUATOR,
        WORK,
        WORK_V2,
        exposure_ref=record["exposure_ref"],
        execution_ref="exec-s-1",
        revised=REVISED,
    )
    harness.score(WORK_V2, "exec-s-1", "immutable-model-v2", {})
    claim = harness.diagnostics.assessment(EVALUATOR, WORK_V2, execution_ref="exec-s-1")
    assert claim["role"] == "protected_reassessment"
    path = harness.path / (record["exposure_ref"] + ".json")
    edited = json.loads(path.read_bytes())
    edited["diagnostic_set"] = []
    path.write_text(canonical(edited))
    with pytest.raises(IntakeError, match="starter_lineage_record_invalid"):
        harness.diagnostics.assessment(EVALUATOR, WORK_V2, execution_ref="exec-s-1")


def test_a_comparator_miss_is_diagnostic_too(harness):
    harness.freeze_predecessor()
    harness.score(WORK, "exec-p-1", "immutable-model-v2", {}, comparator={"SUPPLIES": "TAX"})
    record = harness.diagnostics.place_exposure(EVALUATOR, WORK, execution_ref="exec-p-1")
    assert [(d["case_id"], d["arm"], d["outcome"]) for d in record["diagnostic_set"]] == [
        ("supplies", "comparator", "miss")
    ]
    assert record["exposed_choice"]["comparator"] == {
        "reference": PLAN_E.comparator_ref,
        "revision": "declared-rule-v1",
    }
