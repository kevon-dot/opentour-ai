"""Watch the approved workplace connection from the customer's supervised local process."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path
from typing import Any

from opentour_cell.pilot.repair_workflow import _private_json
from opentour_cell.pilot.training_data import identifier


def run(
    directory: Path,
    organization: str,
    config: Path,
    interval: int,
    *,
    once: bool,
    stopped: threading.Event,
) -> int:
    from opentour_cell.pilot.autonomous_cycle import cycle_once

    identifier(organization)
    if (
        not directory.is_absolute()
        or directory.resolve() != directory
        or config.parent != directory
        or not 5 <= interval <= 86400
    ):
        raise ValueError("invalid_worker_configuration")
    while not stopped.is_set():
        try:
            settings = _private_json(config)
            outcome: dict[str, Any] = cycle_once(directory, organization, settings)
            report = {
                key: outcome[key]
                for key in (
                    "state",
                    "reason",
                    "source_revision",
                    "run_id",
                    "result_revision",
                    "review_state",
                    "review_receipt",
                )
                if key in outcome
            }
            print(json.dumps(report), flush=True)
            if once and outcome.get("state") == "held":
                return 2
        except (OSError, ValueError):
            print(
                '{"state":"held","reason":"check_private_configuration_and_source"}',
                file=sys.stderr,
                flush=True,
            )
            if once:
                return 2
        if once or stopped.wait(interval):
            break
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--interval", type=int, default=300)
    parser.add_argument("--once", action="store_true")
    a = parser.parse_args()
    stopped = threading.Event()

    def stop(signum: int, frame: object) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        return run(a.directory, a.organization, a.config, a.interval, once=a.once, stopped=stopped)
    except (OSError, ValueError):
        parser.exit(2, "invalid_worker_configuration\n")


if __name__ == "__main__":
    raise SystemExit(main())
