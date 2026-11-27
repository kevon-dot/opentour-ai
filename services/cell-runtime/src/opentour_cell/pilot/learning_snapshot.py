"""Read a local, version-bound live evidence receipt without granting training rights."""

from __future__ import annotations

import argparse
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Never

from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import LocalHistoricalAuthority, _selected_json
from opentour_cell.pilot.intake_mcp import IntakeError, require_ref
from opentour_cell.pilot.live_authority import (
    SELECTION_SCHEMA,
    LocalLiveIntake,
    PrivateLiveAuthority,
)
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope, WorkflowBenchmarks

SCHEMA = "opentour.pilot.live-learning-snapshot.v1"


def _require(condition: object, reason: str = "live_snapshot_unavailable") -> None:
    if not condition:
        raise IntakeError(reason)


def _capture(
    live_selection: Path, live_run: str, historical_selection: Path, benchmark_run: str
) -> dict[str, Any]:
    selected = _selected_json(live_selection)
    _require(
        set(selected) == {"schema_version", "tenant", "workflow_ref", "actor"}
        and selected["schema_version"] == SELECTION_SCHEMA
        and historical_selection.parent == live_selection.parent
    )
    directory = live_selection.parent
    authority = PrivateLiveAuthority(directory, selected["tenant"], selected["workflow_ref"])
    live = LocalLiveIntake(authority, selected["actor"])
    historical = LocalHistoricalAuthority(historical_selection, read_only=True)
    actor = live.principal
    _require(actor == historical.principal)
    intake = DurableIntake(directory, live, existing_only=True)
    source = DurableIntake(directory, historical, existing_only=True)
    ledger = WorkflowBenchmarks(directory)

    _require(
        intake.get_run(actor, live_run).state == "materialized"
        and source.get_run(actor, benchmark_run).state == "materialized"
    )
    # The historical grant guard reserves its mandate transaction even for a read.
    # Do not nest get_run (which would try to acquire the same guard again).
    with (
        intake.admitted_work(actor, live_run) as incoming,
        source.admitted_work(actor, benchmark_run) as benchmark,
        authority.guard(actor, incoming.workflow_ref) as scope,
    ):
        _require(
            incoming.mode == "live"
            and benchmark.mode == "historical"
            and benchmark.benchmark_current
            and incoming.workflow_ref == benchmark.workflow_ref == scope.workflow_ref
            and incoming.resource.engagement_epoch
            == benchmark.resource.engagement_epoch
            == scope.engagement_epoch
            and scope.can_inspect
        )
        registered = ledger.for_run(actor.tenant, benchmark)
        _require(registered is not None and registered.benchmark_state == "scored")
        assert registered is not None
        definition = ledger.inspect(
            BenchmarkScope(actor.tenant, scope.engagement_epoch, scope.workflow_ref),
            registered.benchmark_ref,
            current_run=benchmark_run,
        )
        _require(
            definition is not None
            and definition["version"] == registered.version
            and definition["benchmark_state"] == "scored"
            and definition["materialized"] is not None
        )
        snapshot = live.live.snapshot(actor, scope.workflow_ref)
        _require(snapshot.sequence > 0)
        archived = live.live.snapshot(actor, scope.workflow_ref, as_of_revision=snapshot.sequence)
        _require(archived == snapshot, "live_snapshot_changed")
        return {
            "schema_version": SCHEMA,
            "origin": "observed_live",
            "scope": [actor.tenant, scope.workflow_ref, scope.engagement_epoch],
            "live_authority_revision": scope.authority_revision,
            "live_admission": asdict(incoming),
            "benchmark_admission": asdict(benchmark),
            "benchmark_definition": definition,
            "policy_revision": benchmark.revisions[2],
            "dataset_scope": [snapshot.dataset_ref, snapshot.dataset_revision],
            "evidence_revision": snapshot.evidence_revision,
            # The reducer's counters are decisions and quarantined events, not
            # eligible training examples. Keep their units explicit.
            "verified_decisions": snapshot.verified,
            "pending_decisions": snapshot.pending,
            "disputed_decisions": snapshot.disputed,
            "quarantined_events": snapshot.quarantined,
        }


def read_live_receipt(
    live_selection: Path,
    live_run: str,
    historical_selection: Path,
    benchmark_run: str,
    *,
    expected_digest: str | None = None,
) -> dict[str, Any]:
    """Return an opaque receipt only while its authenticated input bindings are current."""
    require_ref(live_run)
    require_ref(benchmark_run)
    _require(expected_digest is None or re.fullmatch(r"[a-f0-9]{64}", expected_digest))
    args = live_selection, live_run, historical_selection, benchmark_run
    body = _capture(*args)
    _require(_capture(*args) == body, "live_snapshot_changed")
    revision = digest(body)
    _require(expected_digest is None or expected_digest == revision, "live_snapshot_changed")
    return {
        "schema_version": SCHEMA,
        "state": "observed",
        "origin": "observed_live",
        "snapshot_digest": revision,
        **{
            key: body[key]
            for key in (
                "policy_revision",
                "verified_decisions",
                "pending_decisions",
                "disputed_decisions",
                "quarantined_events",
            )
        },
    }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise IntakeError("live_snapshot_unavailable")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--live-selection", type=Path, required=True)
    parser.add_argument("--live-run", required=True)
    parser.add_argument("--historical-selection", type=Path, required=True)
    parser.add_argument("--benchmark-run", required=True)
    parser.add_argument("--expected-digest")
    try:
        args = parser.parse_args(argv)
        result = read_live_receipt(
            args.live_selection,
            args.live_run,
            args.historical_selection,
            args.benchmark_run,
            expected_digest=args.expected_digest,
        )
    except (OSError, ValueError, TypeError, KeyError, RuntimeError, sqlite3.Error) as error:
        reason = (
            "live_snapshot_changed"
            if str(error) == "live_snapshot_changed"
            else "live_snapshot_unavailable"
        )
        print(canonical({"schema_version": SCHEMA, "state": "held", "reason": reason}))
        return 2
    print(canonical(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
