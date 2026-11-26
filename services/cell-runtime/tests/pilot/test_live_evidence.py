"""Generated committed business events; no provider or customer evidence is claimed."""

from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.live_evidence import FeedGrant, LiveEvidence, LiveScope

ACTOR = IntakePrincipal("org-a", "custodian", "intake")
DECISION = FeedGrant("decisions", "decision", "grant-v1", "source-v1", "invoice-app", 10, "lease-a")
OUTCOME = FeedGrant(
    "truth",
    "independent_outcome",
    "truth-grant-v1",
    "truth-v1",
    "invoice-app",
    20,
    "lease-a",
    "independent-verifier",
)


class Authority:
    def __init__(self):
        self.scope = LiveScope(
            "org-a",
            "workflow-a",
            "epoch-a",
            "dataset-a",
            "dataset-v1",
            (DECISION, OUTCOME),
            True,
            True,
            True,
            "custodian",
            2**40,
        )

    @contextmanager
    def guard(self, actor, workflow):
        if actor.tenant != self.scope.tenant or workflow != self.scope.workflow_ref:
            raise IntakeError("live_evidence_unavailable")
        yield self.scope


class Case:
    def __init__(self, path):
        path.mkdir(mode=0o700, parents=True)
        self.authority = Authority()
        self.store = LiveEvidence(path, self.authority)
        self.store.initialize()
        self.decisions = self.store.save_binding(ACTOR, "workflow-a", "decisions")
        self.outcomes = self.store.save_binding(ACTOR, "workflow-a", "truth")
        self.page = 0

    def ingest(self, *events, outcome=False, epochs=None, positions=None):
        self.page += 1
        return self.store.ingest_page(
            ACTOR,
            self.outcomes if outcome else self.decisions,
            page_ref=f"page-{self.page}",
            source_revision="truth-v1" if outcome else "source-v1",
            events=events,
            source_positions=positions or tuple(30 + self.page for _ in events),
            event_epochs=epochs or tuple("epoch-a" for _ in events),
            checkpoint=30 + self.page,
        )

    def snapshot(self, **kwargs):
        return self.store.snapshot(ACTOR, "workflow-a", **kwargs)


def decision(identity="D1", **changes):
    return {
        "kind": "DecisionRecorded",
        "event_id": "delivery-" + identity,
        "event_revision": 1,
        "decision_id": identity,
        "decision_revision": 1,
        "case_id": "invoice-1",
        "family_id": "supplier-1",
        "decision_at": 100,
        "action_ref": "defer",
        "policy_ref": "policy-v1",
        "input_ref": None,
        "input_fidelity": "unavailable",
        "source_provenance": "application-commit",
        "actor_ref": None,
        "model_ref": None,
        **changes,
    }


def outcome(**changes):
    return {
        "kind": "OutcomeVerified",
        "event_id": "truth-event",
        "event_revision": 1,
        "decision_id": "D1",
        "decision_revision": 1,
        "outcome_id": "O1",
        "outcome_revision": 1,
        "effective_at": 110,
        "recorded_at": 111,
        "finality": "final",
        "verified_action_ref": "approve",
        "verifier": "independent-verifier",
        **changes,
    }


def totals(snapshot):
    return (
        snapshot.decisions,
        snapshot.verified,
        snapshot.pending,
        snapshot.disputed,
        snapshot.families,
    )


def test_redelivered_decision_counts_once_and_distinct_decisions_count_twice(tmp_path: Path):
    case = Case(tmp_path / "owner")
    for _ in range(3):
        case.ingest(decision())
    case.ingest(decision("D2"))
    case.ingest(outcome(), outcome=True)
    case.store = LiveEvidence(case.store.path.parent, case.authority)
    assert totals(case.snapshot()) == (2, 1, 1, 0, 1)
    case.ingest(decision(event_id="second-delivery"))
    assert totals(case.snapshot()) == (2, 1, 1, 0, 1)
    assert case.snapshot().cases == 1


def test_same_decision_same_revision_two_event_ids_disputed(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.ingest(decision(), decision("D2"))
    case.ingest(outcome(), outcome=True)
    before = case.snapshot()
    case.ingest(decision(event_id="conflict", action_ref="pay"))
    assert totals(case.snapshot()) == (2, 0, 1, 1, 1)
    assert totals(case.snapshot(as_of_revision=before.sequence)) == (2, 1, 1, 0, 1)


def test_new_decision_revision_reopens_verification(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.ingest(decision(), decision("D2"))
    case.ingest(outcome(), outcome=True)
    before = case.snapshot()
    case.ingest(decision(event_id="revised", decision_revision=2, action_ref="ask"))
    assert totals(case.snapshot()) == (2, 0, 2, 0, 1)
    assert totals(case.snapshot(as_of_revision=before.sequence)) == (2, 1, 1, 0, 1)
    case.ingest(
        outcome(event_id="new-verification", outcome_id="O2", decision_revision=2), outcome=True
    )
    assert totals(case.snapshot()) == (2, 1, 1, 0, 1)


def test_final_reopen_stale_final_reverify_preserves_original_input(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.ingest(decision(input_ref="original-as-of", input_fidelity="observed"))
    final = outcome()
    case.ingest(final, outcome=True)
    as_of = case.snapshot()
    case.ingest(
        outcome(
            event_id="reopen", outcome_revision=2, finality="reopened", verified_action_ref=None
        ),
        outcome=True,
    )
    assert totals(case.snapshot()) == (1, 0, 1, 0, 1)
    case.ingest(final, outcome=True)
    assert totals(case.snapshot()) == (1, 0, 1, 0, 1)
    case.ingest(outcome(event_id="reverified", outcome_revision=3), outcome=True)
    assert totals(case.snapshot()) == (1, 1, 0, 0, 1)
    assert case.snapshot().verified_roots[0].input_ref == "original-as-of"
    assert totals(case.snapshot(as_of_revision=as_of.sequence)) == (1, 1, 0, 0, 1)


def test_outcome_before_decision_is_buffered_and_wrong_verifier_quarantined(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.ingest(outcome(), outcome=True)
    assert totals(case.snapshot()) == (0, 0, 0, 0, 0)
    case.ingest(decision())
    assert totals(case.snapshot()) == (1, 1, 0, 0, 1)
    case.ingest(outcome(event_id="unattested", decision_id="D2", verifier="model"), outcome=True)
    case.ingest(decision("D2"))
    assert totals(case.snapshot()) == (2, 1, 1, 0, 1)
    assert case.snapshot().quarantined == 1


def test_conflicting_delivery_revision_is_disputed(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.ingest(decision(), decision(action_ref="pay"))
    assert totals(case.snapshot()) == (1, 0, 0, 1, 1)


def test_import_without_input_is_metric_only_and_content_off(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.ingest(decision())
    case.ingest(outcome(), outcome=True)
    root = case.snapshot().verified_roots[0]
    assert root.input_ref is None and root.input_fidelity == "unavailable"
    case.ingest(decision("D2", raw_reply="GENERATED_PRIVATE_SENTINEL"))
    assert totals(case.snapshot()) == (1, 1, 0, 0, 1)
    assert b"GENERATED_PRIVATE_SENTINEL" not in case.store.path.read_bytes()


def test_binding_saved_once_successor_revocation_and_old_epoch(tmp_path: Path):
    case = Case(tmp_path / "owner")
    assert case.store.save_binding(ACTOR, "workflow-a", "decisions") == case.decisions
    assert case.store.resolve(case.decisions, ACTOR).state == "current"
    case.authority.scope = replace(
        case.authority.scope, grants=(replace(DECISION, revision="grant-v2"), OUTCOME)
    )
    next_ref = case.store.save_binding(ACTOR, "workflow-a", "decisions")
    old = case.store.resolve(case.decisions, ACTOR)
    assert old.state == "superseded" and old.successor_ref == next_ref
    case.authority.scope = replace(case.authority.scope, grants=())
    assert case.store.resolve(case.decisions, ACTOR) is None
    assert case.store.resolve(next_ref, ACTOR) is None
    case.authority.scope = replace(
        case.authority.scope, grants=(DECISION,), engagement_epoch="new-epoch"
    )
    assert case.store.resolve(case.decisions, ACTOR) is None
    foreign = replace(ACTOR, tenant="org-b")
    assert case.store.resolve(next_ref, foreign) is None


def test_new_epoch_binding_excludes_prior_rows_and_late_old_epoch_outcome(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.ingest(decision(), positions=(10,))
    assert totals(case.snapshot()) == (0, 0, 0, 0, 0)
    case.ingest(decision(), epochs=("old-epoch",))
    assert totals(case.snapshot()) == (0, 0, 0, 0, 0)
    case.ingest(decision())
    case.ingest(outcome(), outcome=True, epochs=("old-epoch",))
    assert totals(case.snapshot()) == (1, 0, 1, 0, 1)


def test_binding_requires_custodian_and_read_has_zero_writes(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.authority.scope = replace(case.authority.scope, can_bind=False)
    with pytest.raises(IntakeError, match="data_custodian_required"):
        case.store.save_binding(ACTOR, "workflow-a", "decisions")
    contents = case.store.path.read_bytes()
    assert case.snapshot().can_bind_verifier is False
    assert case.store.path.read_bytes() == contents
    assert LiveEvidence(tmp_path / "missing", case.authority).resolve("unknown", ACTOR) is None
    assert not (tmp_path / "missing").exists()


def test_page_retry_same_receipt_and_conflicting_page_holds(tmp_path: Path):
    case = Case(tmp_path / "owner")
    params = dict(
        page_ref="one-page",
        source_revision="source-v1",
        events=(decision(),),
        source_positions=(11,),
        event_epochs=("epoch-a",),
        checkpoint=11,
    )
    receipt = case.store.ingest_page(ACTOR, case.decisions, **params)
    assert case.store.ingest_page(ACTOR, case.decisions, **params) == receipt
    with pytest.raises(IntakeError, match="source_page_conflict"):
        case.store.ingest_page(ACTOR, case.decisions, **{**params, "events": (decision("D2"),)})
    assert totals(case.snapshot()) == (1, 0, 1, 0, 1)


def test_verifier_grant_is_not_a_saved_binding(tmp_path: Path):
    path = tmp_path / "owner"
    path.mkdir(mode=0o700)
    authority = Authority()
    store = LiveEvidence(path, authority)
    store.initialize()
    store.save_binding(ACTOR, "workflow-a", "decisions")
    assert store.snapshot(ACTOR, "workflow-a").verifier_bound is False
    store.save_binding(ACTOR, "workflow-a", "truth")
    assert store.snapshot(ACTOR, "workflow-a").verifier_bound is True


def test_revoked_truth_cannot_continue_to_credit_current_verification(tmp_path: Path):
    case = Case(tmp_path / "owner")
    case.ingest(decision())
    case.ingest(outcome(), outcome=True)
    before = case.snapshot()
    case.authority.scope = replace(case.authority.scope, grants=(DECISION,))
    after = case.snapshot()
    assert totals(after) == (1, 0, 1, 0, 1)
    assert after.evidence_revision != before.evidence_revision
    assert after.verifier_bound is False
