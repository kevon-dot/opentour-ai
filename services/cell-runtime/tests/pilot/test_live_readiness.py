"""Generated actual reducer/repair/readiness receipts, without provider or UI claims."""

from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot import open_weight
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.live_evidence import FeedGrant, LiveEvidence, LiveScope
from opentour_cell.pilot.live_readiness import (
    POLICY,
    LiveReadinessSource,
    LocalRepairAssessments,
    ObservedDecision,
)
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_readiness import TrainingReadiness

ACTOR = IntakePrincipal("generated-org", "generated-custodian", "intake")
DECISION = FeedGrant("decisions", "decision", "grant-v1", "source-v1", "invoice-app", 10, "lease")
TRUTH = FeedGrant(
    "truth",
    "independent_outcome",
    "truth-grant",
    "truth-v1",
    "invoice-app",
    20,
    "lease",
    "generated-verifier",
)


class Authority:
    def __init__(self, store: LocalTrainingStore):
        self.store = store
        self.scope = LiveScope(
            ACTOR.tenant,
            "workflow",
            store.engagement_id,
            "dataset",
            "dataset-revision",
            (DECISION, TRUTH),
            True,
            True,
            True,
            ACTOR.actor,
            2**40,
        )

    @contextmanager
    def guard(self, actor, workflow):
        if actor.tenant != self.scope.tenant or workflow != self.scope.workflow_ref:
            raise IntakeError("live_evidence_unavailable")
        if actor.actor not in {ACTOR.actor, "generated-reader"}:
            raise IntakeError("live_evidence_unavailable")
        with self.store.engagement_guard(read_only=True):
            yield replace(self.scope, can_bind=actor.actor == ACTOR.actor)


class Contexts:
    def __init__(self):
        self.reads = []
        self.classification = "automatic"
        self.inputs = {"input-1": {"description": "carrier"}}
        # Optional training children are not live roots and never enter the reducer.
        self.inputs.update({f"synthetic-{i}": {"description": "carrier"} for i in range(100)})

    def read(self, snapshot, root, allowed_fields):
        self.reads.append(root.input_ref)
        features = self.inputs.get(root.input_ref)
        if features is None:
            return None
        incumbent = (
            {"status": "automatic", "label": root.action_ref}
            if self.classification == "automatic"
            else {"status": "review"}
            if self.classification == "review"
            else None
        )
        return ObservedDecision(
            root.input_ref,
            root.action_ref,
            "observed-action-" + root.decision_id,
            features,
            incumbent,
        )


class Case:
    def __init__(self, directory: Path):
        directory.mkdir(mode=0o700)
        self.directory = directory
        self.training = LocalTrainingStore(directory / "training.sqlite3", ACTOR.tenant)
        self.authority = Authority(self.training)
        self.live = LiveEvidence(directory, self.authority)
        self.live.initialize()
        self.decision_ref = self.live.save_binding(ACTOR, "workflow", "decisions")
        self.truth_ref = None
        self.source = LiveReadinessSource(self.live)
        self.contexts = Contexts()
        self.policy_path = directory / "repair-policy.json"
        self.policy = {
            "schema_version": POLICY,
            "organization": ACTOR.tenant,
            "workflow_ref": "workflow",
            "engagement": self.training.engagement_id,
            "owner_ref": ACTOR.actor,
            "dataset_ref": "dataset",
            "dataset_revision": "dataset-revision",
            "feature": "description",
            "allowed_features": ["description"],
            "labels": ["supplies", "freight"],
            "rule_mapping": {"carrier": "freight"},
            "fallback": "incumbent",
            "terms": {
                "minimum_residual_errors": 3,
                "minimum_development_cases": 1,
                "minimum_coverage_bps": 10000,
                "error_ceiling_bps": 0,
            },
            "development_families": ["supplier-1", "supplier-2", "supplier-3"],
            "protected_families": ["protected-1"],
            "target_ref": None,
            "mandate_ref": None,
            "mandate_revision": None,
            "agreement_revision": None,
        }
        self.save_policy()
        self.assessments = LocalRepairAssessments(
            directory, self.live, ACTOR, self.policy_path, contexts=self.contexts
        )
        self.ready = TrainingReadiness(
            directory,
            ACTOR.tenant,
            self.training.engagement_id,
            source=self.source,
            assessments=self.assessments,
        )
        self.page = 0

    def save_policy(self):
        self.policy_path.touch(mode=0o600)
        self.policy_path.write_text(json.dumps(self.policy))

    def bind_verifier(self):
        self.truth_ref = self.live.save_binding(ACTOR, "workflow", "truth")

    def ingest(self, *events, truth=False):
        self.page += 1
        self.live.ingest_page(
            ACTOR,
            self.truth_ref if truth else self.decision_ref,
            page_ref=f"page-{self.page}",
            source_revision="truth-v1" if truth else "source-v1",
            events=events,
            source_positions=tuple(30 + self.page for _ in events),
            event_epochs=tuple(self.training.engagement_id for _ in events),
            checkpoint=30 + self.page,
        )

    def refresh(self):
        snapshot = self.source.snapshot(ACTOR, "workflow")
        self.assessments.refresh(snapshot)
        return self.ready.refresh("workflow", actor=ACTOR)


def decision(identity="D1", family="supplier-1", **changes):
    return {
        "kind": "DecisionRecorded",
        "event_id": "delivery-" + identity,
        "event_revision": 1,
        "decision_id": identity,
        "decision_revision": 1,
        "case_id": "invoice-" + identity,
        "family_id": family,
        "decision_at": 100,
        "action_ref": "supplies",
        "policy_ref": "policy-v1",
        "input_ref": "input-1",
        "input_fidelity": "observed",
        "source_provenance": "application-commit",
        "model_ref": None,
        "actor_ref": None,
        **changes,
    }


def outcome(identity="D1", **changes):
    return {
        "kind": "OutcomeVerified",
        "event_id": "truth-" + identity,
        "event_revision": 1,
        "decision_id": identity,
        "decision_revision": 1,
        "outcome_id": "outcome-" + identity,
        "outcome_revision": 1,
        "effective_at": 200,
        "recorded_at": 201,
        "finality": "final",
        "verified_action_ref": "freight",
        "verifier": "generated-verifier",
        **changes,
    }


def test_actual_reducer_pending_bind_correction_and_zero_effects(tmp_path: Path):
    case = Case(tmp_path / "private")
    empty = case.ready.refresh("workflow", actor=ACTOR)
    assert empty["reason_code"] == "no_live_decisions"
    case.ingest(decision(), decision(), decision())
    pending = case.ready.refresh("workflow", actor=ACTOR)
    assert pending["pending_count"] == 1
    assert pending["reason_code"] == "no_verifier_bound"
    assert (
        case.ready.read("workflow", actor=replace(ACTOR, actor="generated-reader"))["next_action"]
        == "ask_data_custodian_to_bind_verifier"
    )
    case.bind_verifier()
    assert case.ready.read("workflow", actor=ACTOR) is None
    assert case.ready.refresh("workflow", actor=ACTOR)["reason_code"] == "awaiting_outcome"
    case.ingest(outcome(), outcome(), truth=True)
    result = case.refresh()
    assert (
        result["verified_root_count"],
        result["residual_group_count"],
        result["minimum_residual_groups"],
    ) == (1, 0, 3)
    assert result["reason_code"] == "too_early_to_train"
    assert result["repair_route"] == "simple"
    assert not any(ref.startswith("synthetic-") for ref in case.contexts.reads)
    prior_reads = list(case.contexts.reads)
    assert case.ready.read("workflow", actor=ACTOR) == result
    assert case.contexts.reads == prior_reads  # UI disclosure never re-runs the repair.
    assert case.refresh() == result
    with sqlite3.connect(case.training.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
    assert not (case.directory / "learning-mandates.sqlite3").exists()
    assert not (case.directory / "approval-inbox.sqlite3").exists()


def test_reopen_correction_denies_previous_eligibility_after_restart(tmp_path: Path):
    case = Case(tmp_path / "private")
    case.bind_verifier()
    case.ingest(decision())
    case.ingest(outcome(), truth=True)
    first = case.refresh()
    case.ingest(
        outcome(
            event_revision=2, outcome_revision=2, finality="reopened", verified_action_ref=None
        ),
        truth=True,
    )
    assert case.ready.read("workflow", actor=ACTOR, expected_revision=first["revision"]) is None
    # Fresh handles read the actual persisted live database and readiness history.
    live = LiveEvidence(case.directory, case.authority)
    reopened = TrainingReadiness(
        case.directory, ACTOR.tenant, case.training.engagement_id, source=LiveReadinessSource(live)
    )
    current = reopened.refresh("workflow", actor=ACTOR)
    assert (current["verified_root_count"], current["pending_count"]) == (0, 1)
    assert current["verdict"] == "held"
    assert current["reason_code"] == "awaiting_outcome"
    assert len(reopened.history("workflow", actor=ACTOR)) == 2


@pytest.mark.parametrize("limited_train", [False, True], ids=["no-mandate", "limited-train"])
def test_three_independent_residuals_do_not_admit_training(tmp_path: Path, limited_train: bool):
    case = Case(tmp_path / "private")
    case.policy["rule_mapping"] = {"carrier": "supplies"}
    mandate = None
    if limited_train:
        owner = "generated-rights-owner"
        mandate = LearningMandates(
            case.directory, ACTOR.tenant, case.training.engagement_id, owner=owner
        )
        agreement = digest("generated-limited-repair-agreement")
        enrolled = mandate.enroll(
            dict(
                mandate_id="generated-limited-repair",
                owner_id=owner,
                delegate=ACTOR.actor,
                mode="manual",
                workflow="workflow",
                connectors=["generated-reader"],
                trace_source="decisions",
                correction_source="truth",
                field="description",
                fields=["description"],
                labels=["supplies", "freight"],
                profile=open_weight.PROFILE,
                uses=["inspect", "evaluate", "train"],
                permitted_error_pairs=[["supplies", "freight"]],
                agreement_revision=agreement,
                trainer=ACTOR.actor,
                payer="generated-zero-cost-cpu",
                reviewer="generated-independent-reviewer",
                audience="generated-private-review",
                destination="generated-local",
                expires_at=int(time.time()) + 600,
                max_runs=2,
                max_fits=1,
                max_total_seconds=5,
                max_seconds_per_fit=5,
                max_rows_per_run=5,
                max_total_rows=5,
                max_cost_cents=0,
            ),
            actor=owner,
        )
        case.policy.update(
            mandate_ref="generated-limited-repair",
            mandate_revision=enrolled["mandate_revision"],
            agreement_revision=agreement,
            owner_ref=owner,
        )
    case.save_policy()
    case.bind_verifier()
    for index in range(1, 4):
        case.ingest(decision(f"D{index}", f"supplier-{index}"))
        case.ingest(outcome(f"D{index}"), truth=True)
        result = case.refresh()
        assert result["residual_group_count"] == index
        assert result["verdict"] == "held"
        assert result["reason_code"] == (
            "too_early_to_train"
            if index < 3
            else "untouched_holdout_required"
            if limited_train
            else "training_mandate_required"
        )
        if index == 1:
            case.ingest(decision("same-supplier", "supplier-1"))
            case.ingest(outcome("same-supplier"), truth=True)
            repeated = case.refresh()
            assert repeated["verified_root_count"] == 2
            assert repeated["residual_group_count"] == 1
            assert repeated["reason_code"] == "too_early_to_train"
    with sqlite3.connect(case.training.path) as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    if mandate is None:
        assert not (case.directory / "learning-mandates.sqlite3").exists()
    else:
        current = mandate.inspect("generated-limited-repair", actor=ACTOR.actor)
        assert current["reserved_fits"] == current["reserved_seconds"] == 0
        assert "qualified_model_required" in result["missing_gates"]


def test_missing_inputs_are_unknown_and_protected_context_is_never_read(tmp_path: Path):
    case = Case(tmp_path / "private")
    case.bind_verifier()
    case.ingest(
        decision(input_ref=None, input_fidelity="unavailable"),
        decision("protected", "protected-1", input_ref="protected-answers"),
    )
    case.ingest(outcome(), outcome("protected"), truth=True)
    result = case.refresh()
    assert result["residual_group_count"] is None
    assert result["verdict"] == "held"
    assert case.contexts.reads == []
    assert "development_evidence_required" in result["missing_gates"]


def test_policy_change_fences_stored_assessment_and_foreign_actor_is_unavailable(tmp_path: Path):
    case = Case(tmp_path / "private")
    case.bind_verifier()
    case.ingest(decision())
    case.ingest(outcome(), truth=True)
    first = case.refresh()
    case.policy["terms"]["minimum_residual_errors"] = 4
    case.save_policy()
    assert case.ready.read("workflow", actor=ACTOR, expected_revision=first["revision"]) is None
    assert case.ready.read("workflow", actor=replace(ACTOR, actor="stranger")) is None
    with pytest.raises(TrainingError):
        case.ready.read("workflow", actor=replace(ACTOR, tenant="foreign"))


@pytest.mark.parametrize("action", ["defer", "ask"])
def test_observed_defer_or_ask_does_not_become_an_automatic_error(tmp_path: Path, action: str):
    case = Case(tmp_path / "private")
    case.contexts.classification = "review"
    case.bind_verifier()
    for index in range(1, 4):
        case.ingest(decision(f"D{index}", f"supplier-{index}", action_ref=action))
        case.ingest(outcome(f"D{index}"), truth=True)
    result = case.refresh()
    assert result["verdict"] == "held"
    with sqlite3.connect(case.assessments.path) as db:
        record = json.loads(db.execute("SELECT body FROM assessments").fetchone()[0])
    assert record["assessment"]["residual_roots"] == []
    assert record["decision"]["residual_groups"] == 0
    assert len(record["nonautomatic_roots"]) == 3
    assert not (case.directory / "learning-mandates.sqlite3").exists()


def test_opaque_action_with_no_observed_classification_requests_that_evidence(tmp_path: Path):
    case = Case(tmp_path / "private")
    case.contexts.classification = "unobserved"
    case.bind_verifier()
    case.ingest(decision())
    case.ingest(outcome(), truth=True)
    result = case.refresh()
    assert result["reason_code"] == "committed_action_classification_required"
    assert result["residual_group_count"] is None
    assert result["verdict"] == "held"


def test_revoked_truth_history_contains_only_opaque_version_metadata(tmp_path: Path):
    case = Case(tmp_path / "private")
    case.bind_verifier()
    case.ingest(decision())
    case.ingest(outcome(), truth=True)
    case.refresh()
    case.authority.scope = replace(case.authority.scope, grants=(DECISION,))
    history = case.ready.history("workflow", actor=ACTOR)
    assert len(history) == 1
    assert set(history[0]) == {"readiness_ref", "revision", "version", "recorded_at"}
    assert not any(
        key in json.dumps(history)
        for key in ["verified_root_count", "mandate_ref", "target_ref", "residual_group_count"]
    )
    assert case.ready.read("workflow", actor=ACTOR) is None


def test_private_policy_symlink_is_denied_before_context_read(tmp_path: Path):
    case = Case(tmp_path / "private")
    case.bind_verifier()
    case.ingest(decision())
    case.ingest(outcome(), truth=True)
    original = case.policy_path.with_name("other-policy.json")
    case.policy_path.rename(original)
    case.policy_path.symlink_to(original)
    with pytest.raises(OSError):
        case.refresh()
    assert case.contexts.reads == []
    assert not case.assessments.path.exists()
