"""Adopt existing starter-case evidence on the independently authorized worker path."""

from dataclasses import asdict

from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_mcp import IntakeError, require_ref
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.starter_cases import ProposedCase, _scope_record
from opentour_cell.pilot.starter_map_confirmation import StarterCaseSession
from opentour_cell.pilot.workflow_benchmarks import (
    BenchmarkIntake,
    MaterializedBenchmark,
    WorkflowBenchmarks,
)


def _require(value: object) -> None:
    if not value:
        raise IntakeError("starter_benchmark_unavailable")


def publish_starter_state(session: StarterCaseSession, run_ref: str) -> MaterializedBenchmark:
    """Inspect a real proposed/frozen pack and publish metadata for its admitted run."""
    require_ref(run_ref)
    with session._pin("materialize") as preflight:
        _require(preflight.scope.work.run_ref == run_ref)
    ledger = WorkflowBenchmarks(session.directory)
    with ledger.worker_guard(), session._pin("materialize") as pinned:
        work = pinned.scope.work
        _require(work.run_ref == run_ref and work.benchmark_current and work.mode == "starter")
        core = session.core
        with core._db() as db:
            body = core._load(db, pinned.scope)
            core._validate_prior_freezes(db, pinned.scope)
            pack = core._ref(pinned.scope)
            row = db.execute("SELECT proposal FROM packs WHERE ref=?", (pack,)).fetchone()
            proposed = tuple(
                ProposedCase(**{key: item[key] for key in ProposedCase.__dataclass_fields__})
                for item in body["cases"]
            )
            original = core._cases(pinned.scope, proposed)
            _require(original and row is not None and digest(original) == row[0])
            proposal = MaterializedBenchmark("needs_owner_review", pack, row[0], work.revisions[0])
            receipt = None
            if body["frozen"] is not None:
                frozen = core._frozen(body["frozen"])
                splits = {item["family_id"]: item["split_ref"] for item in body["cases"]}
                revision = digest(
                    [
                        _scope_record(pinned.scope),
                        body["cases"],
                        asdict(pinned.scope.frozen_plan),
                        splits,
                    ]
                )
                _require(
                    frozen.reference == "starter-freeze-" + revision[:40]
                    and frozen.revision == revision
                )
                receipt = MaterializedBenchmark(
                    "frozen_unscored", frozen.reference, revision, work.revisions[0]
                )
            backend = BenchmarkIntake(
                DurableIntake(session.directory, pinned.setup, existing_only=True)
            )
            backend.adopt_starter_state(
                session.actor,
                work,
                proposal,
                receipt,
                check_current=lambda: session._current(pinned),
            )
            return receipt or proposal
