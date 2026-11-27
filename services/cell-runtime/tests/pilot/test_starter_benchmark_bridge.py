"""Actual owner-adjudicated pack observed by a separate materialization worker."""

import sqlite3

import pytest
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.starter_benchmark_bridge import publish_starter_state
from opentour_cell.pilot.starter_map_confirmation import StarterCaseSession
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake, WorkflowBenchmarks
from test_starter_map_confirmation import FiveFamilies, adjudicate

pytest_plugins = ("test_starter_map_confirmation",)


def test_normal_starter_states_survive_restart_and_keep_admitted_benchmark(joined):
    h, owner, cap, _, setup = joined
    worker = StarterCaseSession(owner.settings, setup.principal)
    run = cap["run_ref"]

    def current():
        api = BenchmarkIntake(DurableIntake(h.path.parent, setup, existing_only=True))
        return api.get_run(setup.principal, run).benchmark

    draft = current()
    owner.propose(FiveFamilies())
    with pytest.raises(IntakeError):
        publish_starter_state(owner, run)
    pending = publish_starter_state(worker, run)
    assert pending.state == current().benchmark_state == "needs_owner_review"
    assert publish_starter_state(worker, run) == pending
    adjudicate(owner)
    frozen = owner.freeze(splits={f"family-{i}": "reserved" for i in range(5)}, expected_revision=5)
    observed = publish_starter_state(worker, run)
    assert observed.state == current().benchmark_state == "frozen_unscored"
    assert observed.definition_ref == frozen.reference and observed.result_ref is None
    assert (
        publish_starter_state(StarterCaseSession(owner.settings, setup.principal), run) == observed
    )
    assert (current().benchmark_ref, current().version) == (draft.benchmark_ref, draft.version)


def test_worker_withdrawal_before_receipt_commit_rolls_back(joined, monkeypatch):
    h, owner, cap, _, setup = joined
    worker = StarterCaseSession(owner.settings, setup.principal)
    owner.propose(FiveFamilies())
    original = WorkflowBenchmarks._receipt_revision

    def withdraw(row, encoded):
        result = original(row, encoded)
        (h.path.parent / "starter-case-grant.json").unlink(missing_ok=True)
        return result

    monkeypatch.setattr(WorkflowBenchmarks, "_receipt_revision", staticmethod(withdraw))
    with pytest.raises((ValueError, OSError)):
        publish_starter_state(worker, cap["run_ref"])
    with sqlite3.connect(h.path.parent / "workflow-benchmarks.sqlite3") as db:
        assert db.execute("SELECT state FROM benchmarks").fetchone() == ("draft",)


def test_worker_catches_up_on_both_observed_transitions(joined):
    h, owner, cap, _, setup = joined
    owner.propose(FiveFamilies())
    adjudicate(owner)
    owner.freeze(splits={f"family-{i}": "reserved" for i in range(5)}, expected_revision=5)
    worker = StarterCaseSession(owner.settings, setup.principal)
    assert publish_starter_state(worker, cap["run_ref"]).state == "frozen_unscored"
    with sqlite3.connect(h.path.parent / "workflow-benchmarks.sqlite3") as db:
        assert db.execute(
            "SELECT target,completed FROM benchmark_operations ORDER BY rowid"
        ).fetchall() == [("needs_owner_review", 1), ("frozen_unscored", 1)]


@pytest.mark.parametrize("damage", [None, "owner", "intent", "order", "missing_operation"])
def test_owner_append_keeps_original_proposal_and_requires_current_provenance(joined, damage):
    import json

    from opentour_cell.pilot.operating_point import canonical, digest
    from opentour_cell.pilot.starter_cases import ProposedCase

    h, owner, cap, _, setup = joined
    owner.propose(FiveFamilies())
    worker = StarterCaseSession(owner.settings, setup.principal)
    original = publish_starter_state(worker, cap["run_ref"])
    first = owner.review()["cases"][0]
    example = ProposedCase(
        "owner-extra",
        "family-0",
        "template-0",
        first["branch_id"],
        (("po", None), ("total", 12)),
        "REVIEW",
        "Boundary at a different amount",
        first["citation"],
    )
    owner.add_counterexample(case=example, expected_revision=0, operation_ref="append-one")
    if damage:
        with sqlite3.connect(owner.core.path) as db:
            body = json.loads(db.execute("SELECT body FROM packs").fetchone()[0])
            row = body["cases"][-1]
            if damage == "owner":
                row["counterexample_owner"] = "another-owner"
            elif damage == "intent":
                body["operations"]["append-one"]["intent"] = "0" * 64
            elif damage == "order":
                body["cases"].insert(0, body["cases"].pop())
            else:
                del row["counterexample_operation"]
            db.execute("UPDATE packs SET body=?,seal=?", (canonical(body), digest(body)))
        with pytest.raises(IntakeError):
            publish_starter_state(worker, cap["run_ref"])
        return
    assert publish_starter_state(worker, cap["run_ref"]) == original
    rows = owner.review()["cases"]
    for revision, row in enumerate(rows, 1):
        owner.adjudicate(
            case_id=row["case_id"],
            target=row["suggestion"],
            reason="Owner reviewed",
            expected_revision=revision,
            operation_ref="review-" + row["case_id"],
        )
    freeze = owner.freeze(splits={r["family_id"]: "reserved" for r in rows}, expected_revision=7)
    assert freeze.proposed == 6
    receipt = publish_starter_state(worker, cap["run_ref"])
    assert (receipt.definition_ref, receipt.definition_digest) == (
        freeze.reference,
        freeze.revision,
    )
    assert (
        publish_starter_state(StarterCaseSession(owner.settings, setup.principal), cap["run_ref"])
        == receipt
    )
