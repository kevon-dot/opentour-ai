"""Materialize one admitted historical benchmark on its authorized customer worker."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Never

from opentour_cell.pilot import source_intake
from opentour_cell.pilot.historical_benchmarks import LocalHistoricalBenchmark
from opentour_cell.pilot.intake_admission import DurableIntake, IntakeWork
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

CONFIG = "opentour.pilot.historical-worker.v1"
RESULT = "opentour.pilot.historical-worker-result.v1"
_FILES = {"selection", "traces", "corrections", "agreement"}
_HOLDS = {
    "invalid_worker_arguments",
    "historical_worker_config_changed",
    "recorded_baseline_predictions_required",
    "source_export_digest_mismatch",
    "benchmark_policy_changed",
    "benchmark_cases_required",
    "benchmark_measurement_policy_required",
    "benchmark_materialization_unresolved",
    "benchmark_receipt_binding_unavailable",
    "immutable_benchmark_version",
}


def _require(condition: object, reason: str = "historical_worker_unavailable") -> None:
    if not condition:
        raise IntakeError(reason)


class _ConfiguredProducer(LocalHistoricalBenchmark):
    def __init__(self, config: Path):
        _require(config.is_absolute())
        self.config_path = config
        self.config_bytes = source_intake._read(config, limit=65_536)
        value = json.loads(self.config_bytes)
        _require(
            isinstance(value, dict)
            and set(value) == {"schema_version", *_FILES}
            and value["schema_version"] == CONFIG
        )
        for key in _FILES:
            _require(
                isinstance(value[key], str)
                and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", value[key])
            )
        paths = {key: config.parent / value[key] for key in _FILES}
        _require(len({config, *paths.values()}) == 5)
        super().__init__(
            paths["selection"], paths["traces"], paths["corrections"], paths["agreement"]
        )

    def _authorize(self, actor: IntakePrincipal, work: IntakeWork) -> dict[str, Any]:
        _require(
            source_intake._read(self.config_path, limit=65_536) == self.config_bytes,
            "historical_worker_config_changed",
        )
        policy = super()._authorize(actor, work)
        # Reacquire current evaluation authority after the policy check and keep
        # it held while reading source bytes. Do not nest the parent's writer.
        with self.authority.guard(actor):
            for path, revision in (
                (self.traces, self.authority.selected.source_revision),
                (self.corrections, self.authority.selected.correction_revision),
            ):
                _require(
                    hashlib.sha256(source_intake._read(path)).hexdigest() == revision,
                    "source_export_digest_mismatch",
                )
        return policy


def execute(config: Path, run_ref: str) -> dict[str, Any]:
    """Configuration locates files; current source and evaluation grants authorize work."""
    require_ref(run_ref)
    producer = _ConfiguredProducer(config)
    authority = producer.authority
    actor = authority.principal
    # A worker invocation cannot create an intake register or a new admission.
    reader = DurableIntake(config.parent, authority, existing_only=True)
    with reader.admitted_work(actor, run_ref) as work:
        _require(work.mode == "historical" and work.benchmark_current)
    producer._authorize(actor, work)
    # Only an established current admission enters the ordinary writable worker.
    with authority.guard(actor):
        backend = BenchmarkIntake(DurableIntake(config.parent, authority))
    completed = backend.materialize(actor, run_ref, producer)
    producer._authorize(actor, work)
    with backend.intake.admitted_work(actor, run_ref) as current:
        _require(current == work)
        benchmark = backend.benchmarks.for_run(actor.tenant, current)
        _require(benchmark is not None)
        assert benchmark is not None
        return {
            "schema_version": RESULT,
            "status": "completed" if completed else "held",
            "run_ref": run_ref,
            "benchmark": asdict(benchmark),
            "reason": None if completed else "benchmark_materialization_unresolved",
        }


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise IntakeError("invalid_worker_arguments")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--run-ref", required=True)
    try:
        args = parser.parse_args(argv)
        result = execute(args.config, args.run_ref)
    except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error) as error:
        reason = str(error)
        result = {
            "schema_version": RESULT,
            "status": "held",
            "reason": reason if reason in _HOLDS else "historical_worker_unavailable",
        }
    print(canonical(result))
    return 0 if result["status"] == "completed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
