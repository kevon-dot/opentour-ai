"""Watch the approved workplace connection from the customer's supervised local process."""

from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

from opentour_cell.pilot.repair_workflow import _private_json
from opentour_cell.pilot.training_data import identifier


def learning_tick(directory: Path, organization: str) -> dict[str, Any]:
    """One schedule tick for learning runs under the current standing mandate.

    The owner opts in by authorizing the scheduler selection. The tick rereads the mandate's
    pause state, mode, fit count and caps every time; it never stores its own copy.
    """
    from opentour_cell.pilot.learning_runs import LearningRunError, run_schedule_tick

    try:
        tick = run_schedule_tick(directory, organization)
    except LearningRunError as error:
        return {"state": "held", "reason": str(error)}
    return {"state": tick["tick"], "reason": tick["reason_code"], "run_id": tick["run_ref"]}


def run(
    directory: Path,
    organization: str,
    config: Path,
    interval: int,
    *,
    once: bool,
    stopped: threading.Event,
) -> int:
    from opentour_cell.pilot import eve_scan
    from opentour_cell.pilot.autonomous_cycle import cycle_once
    from opentour_cell.pilot.eve_authority import SELECTION_FILE, renew_host_permit
    from opentour_cell.pilot.learning_runs import SCHEDULE_SELECTION

    identifier(organization)
    if (
        not directory.is_absolute()
        or directory.resolve() != directory
        or config.parent != directory
        or not 5 <= interval <= 86400
    ):
        raise ValueError("invalid_worker_configuration")

    def eve_installed() -> bool:
        return (
            eve_scan.installed(directory)
            or os.path.lexists(directory / SELECTION_FILE)
            or os.path.lexists(directory / "eve-exchange" / "controls.sqlite3")
        )

    def scan_once() -> int:
        try:
            outcome: dict[str, Any]
            if eve_scan.installed(directory):
                outcome = eve_scan.scan_tick(directory, organization, config, interval)
            elif eve_installed():
                # On the EVE route every fit consumes its own Slack decision. The separate
                # standing classifier schedule may admit work automatically and cannot run here.
                outcome = {"state": "waiting_for_scan"}
            elif os.path.lexists(directory / SCHEDULE_SELECTION):
                # Scheduled learning goes only through the shared learning-run admission, so a
                # Slack or Zapier trigger for the same intent converges on the same run.
                outcome = learning_tick(directory, organization)
            else:
                outcome = cycle_once(directory, organization, _private_json(config))
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
            if outcome.get("state") == "held":
                return 2
        except (OSError, ValueError):
            print(
                '{"state":"held","reason":"check_private_configuration_and_source"}',
                file=sys.stderr,
                flush=True,
            )
            return 2
        return 0

    # The EVE lease is shorter than a discovery interval. Renew independently of a slow scan.
    own_stop = threading.Event()

    def permits() -> None:
        while not own_stop.wait(5):
            if stopped.is_set():
                return
            renew_host_permit(directory, organization)

    keeper = None
    next_scan = 0.0
    try:
        while not stopped.is_set():
            if keeper is None and eve_installed():
                renew_host_permit(directory, organization)
                if not once:
                    keeper = threading.Thread(target=permits, name="eve-permit", daemon=True)
                    keeper.start()
            delay = next_scan - time.monotonic()
            if delay > 0:
                stopped.wait(min(5.0, delay) if keeper is None else delay)
                continue
            result = scan_once()
            if once:
                return result
            next_scan = time.monotonic() + (5 if eve_scan.installed(directory) else interval)
    finally:
        own_stop.set()
        if keeper is not None:
            keeper.join()
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
