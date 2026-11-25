"""Exercise the actual customer worker entry and recheck withdrawal after a Slack decision."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from opentour_cell.pilot import approval_inbox, release_gate
from test_approval_inbox import callback, opt_in, stage
from test_autonomous_cycle import Customer
from test_reviewer_workflow import buttons, screen
from test_slack_adapter import ACTOR


def test_supervised_worker_launch_replays_without_an_extra_provider_read(tmp_path: Path) -> None:
    customer = Customer(tmp_path, real_reader=True)
    args = [
        sys.executable,
        "-B",
        "-m",
        "opentour_cell.pilot.cycle_worker",
        "--once",
        "--directory",
        str(customer.owner),
        "--organization",
        customer.org,
        "--config",
        str(customer.owner / "cycle.json"),
    ]
    first = json.loads(subprocess.check_output(args, text=True))
    second = json.loads(subprocess.check_output(args, text=True))
    assert first == second
    assert first["state"] == "pending_slack_review" and first["review_receipt"]
    assert len([r for r in customer.real_reader.received() if r["method"] == "resources/read"]) == 2
    invalid = subprocess.run([*args, "--interval", "0"], capture_output=True, text=True)
    assert invalid.returncode == 2
    customer.ledger.withdraw(customer.mandate, actor="generated-owner")
    stopped = subprocess.run(args, capture_output=True, text=True)
    assert stopped.returncode == 2
    assert json.loads(stopped.stdout)["state"] == "held"


@pytest.mark.parametrize("change", ["remove_review_role", "alter_sealed_presentation"])
def test_release_rechecks_role_and_evidence_integrity_on_export(
    tmp_path: Path, change: str
) -> None:
    h, plan, run, pointer, audit = stage(tmp_path)
    opt_in(h)
    assert approval_inbox.flush_approvals(h.app) == 1
    h.send("interactive", callback(h, pointer))
    view = list(h.slack.screens)[-1]
    value = buttons(screen(h, view))["ot_release_approve"]
    h.action(view, "ot_release_approve", value)
    args = (
        h.path.parent,
        h.store.organization,
        run,
        audit["run_revision"],
        audit["candidate_digest"],
    )
    assert release_gate.authorize_release(*args)["decision"] == "approved"
    if change == "remove_review_role":
        h.config["users"][ACTOR]["roles"].remove("review")
        h.save()
    else:
        with sqlite3.connect(h.path.parent / "slack-pilot.sqlite3") as db:
            db.execute("UPDATE presentations SET rendered='{}'")
    with pytest.raises(ValueError):
        release_gate.authorize_release(*args)
