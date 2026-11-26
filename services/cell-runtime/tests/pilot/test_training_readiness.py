"""Generated source-port conformance; joined reducer tests have separate evidence."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.training import LocalTrainingStore, TrainingError
from opentour_cell.pilot.training_readiness import (
    ReadinessAssessment,
    ReadinessSnapshot,
    TrainingReadiness,
    VerifiedRoot,
)

ACTOR = IntakePrincipal("fixture-org", "fixture-custodian", "operator")


class Source:
    def __init__(self, state: ReadinessSnapshot):
        self.state: ReadinessSnapshot | None = state
        self.calls = 0
        self.revoke_at: int | None = None

    def snapshot(self, actor: IntakePrincipal, workflow_ref: str) -> ReadinessSnapshot | None:
        self.calls += 1
        if self.calls == self.revoke_at:
            self.state = None
        if self.state is None or workflow_ref != self.state.workflow_ref:
            return None
        if actor.actor not in {"fixture-custodian", "fixture-reader"}:
            return None
        return replace(self.state, can_bind_verifier=actor.actor == "fixture-custodian")


class Assessment:
    def __init__(self, state: ReadinessAssessment):
        self.state = state

    def assessment(self, snapshot: ReadinessSnapshot) -> ReadinessAssessment:
        return self.state


@pytest.fixture
def fixture(tmp_path: Path) -> tuple[TrainingReadiness, Source, LocalTrainingStore]:
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    store = LocalTrainingStore(directory / "training.sqlite3", ACTOR.tenant)
    source = Source(
        ReadinessSnapshot(
            ACTOR.tenant,
            "workflow-1",
            store.engagement_id,
            "evidence-0",
            0,
            0,
            0,
            0,
            (),
            False,
            None,
            True,
        )
    )
    registry = TrainingReadiness(directory, ACTOR.tenant, store.engagement_id, source=source)
    return registry, source, store


def observed(source: Source, count: int, *, pending: int = 0) -> ReadinessSnapshot:
    assert source.state is not None
    return replace(
        source.state,
        evidence_revision=f"evidence-{count}-{pending}",
        decisions=count + pending,
        verified=count,
        pending=pending,
        roots=tuple(VerifiedRoot(f"decision-{i}", f"family-{i}") for i in range(count)),
    )


def assessment(
    snapshot: ReadinessSnapshot, *, residuals: int = 0, missing: tuple[str, ...] = ()
) -> ReadinessAssessment:
    return ReadinessAssessment(
        snapshot.organization,
        snapshot.workflow_ref,
        snapshot.engagement,
        snapshot.evidence_revision,
        "assessment-1",
        "model-1",
        "mandate-1",
        "mandate-revision-1",
        "policy-1",
        3,
        tuple(root.reference for root in snapshot.roots[:residuals]),
        missing,
        "simple" if residuals == 0 else "model",
    )


def test_missing_source_is_not_a_zero_and_read_does_not_initialize(fixture) -> None:
    registry, source, _ = fixture
    assert registry.read("workflow-1", actor=ACTOR) is None
    assert not registry.path.exists()
    source.state = None
    assert registry.refresh("workflow-1", actor=ACTOR) is None
    assert not registry.path.exists()


def test_no_decisions_pending_verifier_and_role_bound_next_action(fixture) -> None:
    registry, source, _ = fixture
    empty = registry.refresh("workflow-1", actor=ACTOR)
    assert empty["reason_code"] == "no_live_decisions"
    assert empty["decision_count"] == 0 and "error_rate" not in empty
    source.state = observed(source, 0, pending=1)
    pending = registry.refresh("workflow-1", actor=ACTOR)
    assert pending["reason_code"] == "no_verifier_bound"
    assert pending["next_action"] == "bind_verifier"
    reader = registry.read("workflow-1", actor=replace(ACTOR, actor="fixture-reader"))
    assert reader["revision"] == pending["revision"]
    assert reader["next_action"] == "ask_data_custodian_to_bind_verifier"
    source.state = replace(source.state, verifier_bound=True, next_actor="fixture-verifier")
    assert registry.read("workflow-1", actor=ACTOR) is None
    bound = registry.refresh("workflow-1", actor=ACTOR)
    assert bound["reason_code"] == "awaiting_outcome"
    assert bound["next_owner"] == "fixture-verifier"
    assert bound["verdict"] == "held"


def test_one_correction_simple_repair_and_unavailable_assessment(fixture) -> None:
    registry, source, _ = fixture
    source.state = observed(source, 1)
    absent = registry.refresh("workflow-1", actor=ACTOR)
    assert absent["reason_code"] == "repair_assessment_required"
    assert absent["residual_group_count"] is None
    assert absent["minimum_residual_groups"] is None
    registry.assessments = Assessment(assessment(source.state))
    result = registry.refresh("workflow-1", actor=ACTOR)
    assert result["reason_code"] == "too_early_to_train"
    assert (
        result["verified_root_count"],
        result["residual_group_count"],
        result["minimum_residual_groups"],
    ) == (1, 0, 3)
    assert result["augmentation_state"] == "not_enabled"


@pytest.mark.parametrize(
    "gate",
    [
        "training_mandate_required",
        "training_rights_required",
        "training_budget_required",
        "untouched_holdout_required",
        "qualified_model_required",
        "decision_time_input_required",
    ],
)
def test_threshold_crossing_cannot_clear_other_gates(fixture, gate: str) -> None:
    registry, source, _ = fixture
    source.state = observed(source, 1)
    registry.assessments = Assessment(assessment(source.state, residuals=1, missing=(gate,)))
    first = registry.refresh("workflow-1", actor=ACTOR)
    assert first["reason_code"] == (
        gate if gate == "decision_time_input_required" else "too_early_to_train"
    )
    source.state = observed(source, 3)
    registry.assessments.state = assessment(source.state, residuals=3, missing=(gate,))
    result = registry.refresh("workflow-1", actor=ACTOR)
    assert result["reason_code"] == gate
    assert result["verdict"] == "held"


def test_all_gates_eligible_is_receipt_only_and_restart_is_idempotent(fixture) -> None:
    registry, source, store = fixture
    source.state = observed(source, 3)
    registry.assessments = Assessment(assessment(source.state, residuals=3))
    first = registry.refresh("workflow-1", actor=ACTOR)
    reopened = TrainingReadiness(
        registry.directory,
        registry.organization,
        registry.engagement,
        source=source,
        assessments=registry.assessments,
    )
    assert first == reopened.refresh("workflow-1", actor=ACTOR)
    assert first == reopened.read("workflow-1", actor=ACTOR, expected_revision=first["revision"])
    assert first["verdict"] == "fit_eligible"
    assert len(reopened.history("workflow-1", actor=ACTOR)) == 1
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 0
    assert not (registry.directory / "learning-mandates.sqlite3").exists()
    assert not (registry.directory / "approval-inbox.sqlite3").exists()


def test_reopen_fences_old_receipt_and_preserves_transition_history(fixture) -> None:
    registry, source, _ = fixture
    source.state = observed(source, 3)
    registry.assessments = Assessment(assessment(source.state, residuals=3))
    prior = registry.refresh("workflow-1", actor=ACTOR)
    source.state = observed(source, 0, pending=3)
    registry.assessments.state = assessment(source.state)
    assert registry.read("workflow-1", actor=ACTOR, expected_revision=prior["revision"]) is None
    current = registry.refresh("workflow-1", actor=ACTOR)
    assert current["verified_root_count"] == 0
    assert current["verdict"] == "held"
    history = registry.history("workflow-1", actor=ACTOR)
    assert [x["version"] for x in history] == [1, 2]
    assert all(set(x) == {"readiness_ref", "revision", "version", "recorded_at"} for x in history)
    with sqlite3.connect(registry.path) as db:
        # Full old facts are operator-local audit records, not current-viewer disclosures.
        import json

        assert [
            json.loads(r[0])["verdict"]
            for r in db.execute("SELECT content FROM readiness ORDER BY version")
        ] == ["fit_eligible", "held"]


def test_stale_assessment_is_rejected_and_same_family_cannot_supply_recurrence(fixture) -> None:
    registry, source, _ = fixture
    source.state = observed(source, 3)
    source.state = replace(
        source.state,
        roots=tuple(VerifiedRoot(root.reference, "same-family") for root in source.state.roots),
    )
    adapter = Assessment(assessment(source.state, residuals=3))
    registry.assessments = adapter
    result = registry.refresh("workflow-1", actor=ACTOR)
    assert result["residual_group_count"] == 1
    assert result["verdict"] == "held"
    adapter.state = replace(adapter.state, evidence_revision="stale")
    with pytest.raises(TrainingError, match="readiness_evidence_unavailable"):
        registry.read("workflow-1", actor=ACTOR)


def test_unauthorized_actor_revocation_and_foreign_workflow_reveal_nothing(fixture) -> None:
    registry, source, store = fixture
    registry.refresh("workflow-1", actor=ACTOR)
    assert registry.read("workflow-1", actor=replace(ACTOR, actor="stranger")) is None
    assert registry.read("foreign", actor=ACTOR) is None
    with pytest.raises(TrainingError):
        registry.read("workflow-1", actor=replace(ACTOR, tenant="foreign"))
    source.revoke_at = source.calls + 2
    assert registry.read("workflow-1", actor=ACTOR) is None
    store.withdraw()
    with pytest.raises(TrainingError):
        registry.read("workflow-1", actor=ACTOR)


def test_read_works_under_existing_readonly_engagement_guard(fixture) -> None:
    registry, _, store = fixture
    receipt = registry.refresh("workflow-1", actor=ACTOR)
    with store.engagement_guard(read_only=True):
        assert registry.read("workflow-1", actor=ACTOR) == receipt


def test_source_revoked_before_commit_does_not_write_a_receipt(fixture) -> None:
    registry, source, _ = fixture
    source.revoke_at = 2
    with pytest.raises(TrainingError):
        registry.refresh("workflow-1", actor=ACTOR)
    with sqlite3.connect(registry.path) as db:
        assert not db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()


def test_concurrent_refreshes_persist_one_transition(fixture) -> None:
    registry, _, _ = fixture
    with ThreadPoolExecutor(max_workers=4) as pool:
        receipts = list(pool.map(lambda _: registry.refresh("workflow-1", actor=ACTOR), range(4)))
    assert all(value == receipts[0] for value in receipts)
    assert len(registry.history("workflow-1", actor=ACTOR)) == 1


def test_evidence_changed_while_waiting_for_writer_cannot_publish_old_verdict(fixture) -> None:
    registry, source, _ = fixture
    first = registry.refresh("workflow-1", actor=ACTOR)
    entered = Event()
    original = source.snapshot

    def observed_snapshot(actor, workflow):
        result = original(actor, workflow)
        entered.set()
        return result

    source.snapshot = observed_snapshot
    with ThreadPoolExecutor(max_workers=1) as pool:
        with sqlite3.connect(registry.path) as writer:
            writer.execute("BEGIN IMMEDIATE")
            request = pool.submit(registry.refresh, "workflow-1", actor=ACTOR)
            assert entered.wait(2)
            assert not request.done()
            source.state = observed(source, 0, pending=1)
            writer.commit()
        with pytest.raises(TrainingError, match="readiness_evidence_unavailable"):
            request.result(timeout=5)
    assert registry.read("workflow-1", actor=ACTOR, expected_revision=first["revision"]) is None
    assert len(registry.history("workflow-1", actor=ACTOR)) == 1
