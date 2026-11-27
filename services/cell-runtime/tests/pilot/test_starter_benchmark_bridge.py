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
