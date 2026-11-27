"""Custody withdrawal retires the engagement epoch before freeing its live slot (OPE-543).

Generated sentinel fixtures only. No provider, network, customer record or cross-CRM claim.
Crash cases run the real custody entry point in a child process that is killed with
``os._exit`` at a named point, so nothing after that point (no ``finally``, no commit)
runs; the parent then restarts from whatever the files durably hold.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot.custody import Closeout, CustodyError
from opentour_cell.pilot.engagement_epochs import EngagementHold, EngagementLedger
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
from test_engagement_epochs import LiveCase, decision, outcome, totals
from test_live_authority import SignedCase
from test_live_evidence import DECISION, OUTCOME

ORG = "generated-org-1"
NEIGHBOR = "generated-org-2"
RES = ("decisions", "truth")
SUCCESSOR = "successor-epoch"
START = {
    "agreement": "terms-a",
    "custodian": "custodian-a",
    "support": "support-a",
    "due": "2040-04-01",
    "trigger": "withdrawal",
}
CRASH = 86
CHILD = r"""
import os
import sys
from pathlib import Path

from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.engagement_epochs import EngagementLedger

directory, organization, point = Path(sys.argv[1]), sys.argv[2], sys.argv[3]


def crash(*_args, **_kwargs):
    os._exit(86)


if point == "before_fence":
    EngagementLedger._append_fence = crash
elif point == "after_fence_journal":
    append = EngagementLedger._append_fence

    def appended_then_crash(self, *args):
        append(self, *args)
        os._exit(86)

    EngagementLedger._append_fence = appended_then_crash
elif point == "after_terminal_commit":
    EngagementLedger.release_slot = crash
else:
    raise SystemExit(3)
Closeout(directory, organization).withdraw(
    agreement="terms-a",
    custodian="custodian-a",
    support="support-a",
    due="2040-04-01",
    trigger="withdrawal",
)
raise SystemExit(0)
"""


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    return path


def enroll(ledger, org, epoch, lease, checkpoint, **overrides):
    values = dict(
        lease_ref=lease,
        authority_digest=sha(f"authority-{org}-{epoch}-{lease}"),
        resources=RES,
        lineage_ref="generated-lineage",
        checkpoint=checkpoint,
        budget_limit=100,
    )
    return ledger.record_engagement(org, epoch, **{**values, **overrides})


def engaged(parent: Path) -> tuple[Path, str]:
    """A live epoch bound to the directory's training engagement, as the signed grant needs."""
    directory = private(parent / ORG)
    epoch = LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_id
    ledger = EngagementLedger(directory)
    enroll(ledger, ORG, epoch, "lease-a", 10)
    ledger.activate(ORG, epoch)
    ledger.observe_positions(ORG, epoch, 31)
    return directory, epoch


def successor(ledger: EngagementLedger):
    return enroll(ledger, ORG, SUCCESSOR, "lease-b", 40, budget_limit=50)


def raw_row(directory: Path, org: str, epoch: str) -> tuple[str, int | None, int]:
    """Read what SQLite durably holds (its own hot-journal recovery only, no ledger code)."""
    with closing(sqlite3.connect(directory / "engagement-epochs.sqlite3")) as db:
        row = db.execute(
            "SELECT state, revocation_epoch, slot_released FROM engagement WHERE org=? AND epoch=?",
            (org, epoch),
        ).fetchone()
    assert row is not None
    return row


def fences(directory: Path) -> list[dict]:
    journal = directory / "engagement-fences.jsonl"
    if not journal.exists():
        return []
    return [json.loads(line) for line in journal.read_text().splitlines()]


def ledger_bytes(directory: Path) -> tuple[bytes, bytes]:
    return (
        (directory / "engagement-epochs.sqlite3").read_bytes(),
        (directory / "engagement-fences.jsonl").read_bytes(),
    )


def crash_withdrawal(directory: Path, point: str) -> None:
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(directory), ORG, point],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert child.returncode == CRASH, child.stderr


def assert_retired_once(directory: Path, epoch: str) -> None:
    assert raw_row(directory, ORG, epoch) == ("terminal", 1, 1)
    assert [(f["org"], f["epoch"], f["revocation_epoch"]) for f in fences(directory)] == [
        (ORG, epoch, 1)
    ]


def test_public_custody_command_retires_epoch_and_frees_slot_for_same_directory_reentry(
    tmp_path: Path,
) -> None:
    case = SignedCase(tmp_path / "worker")
    case.body["engagement_ledger_required"] = True
    case.write()
    epoch = case.body["engagement_epoch"]
    ledger = EngagementLedger(case.directory)
    enroll(
        ledger,
        "org-a",
        epoch,
        DECISION.lease_ref,
        DECISION.checkpoint,
        resources=(DECISION.resource, OUTCOME.resource),
    )
    ledger.activate("org-a", epoch)
    assert case.connect()

    command = [
        sys.executable,
        "-m",
        "opentour_cell.pilot.custody",
        "--directory",
        str(case.directory),
        "--org",
        "org-a",
        "withdraw",
        *(f"--{name}={value}" for name, value in START.items()),
    ]
    stopped = subprocess.run(command, capture_output=True, text=True, check=False, timeout=120)
    assert stopped.returncode == 0, stopped.stderr
    assert json.loads(stopped.stdout)["engagement"] == epoch

    retired = EngagementLedger(case.directory).get("org-a", epoch)
    assert retired is not None
    assert (retired.state, retired.revocation_epoch, retired.slot_released) == ("terminal", 1, True)
    with pytest.raises((IntakeError, TrainingError)):
        case.connect()  # the withdrawn epoch's live binding stays held

    # Same directory, fresh authority, checkpoint after the prior high-water: re-entry opens.
    fresh = EngagementLedger(case.directory)
    enroll(fresh, "org-a", SUCCESSOR, "lease-fresh", DECISION.checkpoint + 1)
    assert fresh.activate("org-a", SUCCESSOR).state == "active"
    with pytest.raises(EngagementHold, match="epoch_terminal"):
        fresh.live_gate("org-a", epoch, DECISION.resource, DECISION.lease_ref)
    assert [e.epoch for e in fresh.engagements("org-a")] == [epoch, SUCCESSOR]


def test_new_epoch_binding_excludes_prior_rows_after_custody_withdrawal(tmp_path: Path) -> None:
    case = LiveCase(tmp_path / "owner")
    first = LocalTrainingStore(case.root / "training.sqlite3", ORG).engagement_id
    case.enter(first, "lease-a", 10)
    case.ingest(decision(), position=31)
    case.ledger.observe_positions(ORG, first, 31)
    case.ledger.bind(ORG, first, "queue", "queue-1")
    case.ledger.grant_approval(ORG, first, "approval-1")
    assert totals(case.snapshot()) == (1, 0, 1, 0, 1)

    Closeout(case.root, ORG).withdraw(**START)  # the custody path, not a direct ledger call

    case.authority.scope = replace(case.authority.scope, engagement_epoch=SUCCESSOR)
    with pytest.raises(IntakeError, match="engagement_epoch_hold"):
        case.bind(SUCCESSOR, "lease-b", 40)  # held until the new checkpoint is recorded
    case.enter(SUCCESSOR, "lease-b", 40)
    assert case.ledger.get(ORG, SUCCESSOR).checkpoint == 40

    # Nothing bound to the prior epoch reads as current.
    assert totals(case.snapshot()) == (0, 0, 0, 0, 0)
    assert not case.ledger.authorize(ORG, SUCCESSOR, "queue", "queue-1")
    assert not case.ledger.has_approval(ORG, SUCCESSOR, "approval-1")
    assert case.ledger.counts(ORG, SUCCESSOR) == {"decision": 0, "read": 0, "fit": 0, "export": 0}
    case.ingest(decision(), position=35)  # old row redelivered at/before the new checkpoint
    assert totals(case.snapshot()) == (0, 0, 0, 0, 0)

    before = (case.snapshot(), case.reads(), case.page_rows(), case.ledger.counts(ORG, SUCCESSOR))
    with pytest.raises(IntakeError, match="engagement_epoch_fenced"):
        case.ingest(outcome(), position=90, epoch=first, outcomes=True)
    with pytest.raises(IntakeError, match="engagement_epoch_fenced"):
        case.ingest(decision("D9"), position=91, epoch=first)
    assert (
        case.snapshot(),
        case.reads(),
        case.page_rows(),
        case.ledger.counts(ORG, SUCCESSOR),
    ) == before

    case.ingest(decision("D2", case_id="invoice-2"), position=92)
    assert case.snapshot().pending == 1
    (history,) = case.ledger.historical(ORG, before=SUCCESSOR)
    assert history["epoch"] == first and history["historical"] is True
    assert history["revocation_epoch"] == 1


@pytest.mark.parametrize("point", ["before_fence", "after_fence_journal"])
def test_crash_before_terminal_state_commits_never_releases_the_slot(
    tmp_path: Path, point: str
) -> None:
    directory, epoch = engaged(tmp_path)
    crash_withdrawal(directory, point)

    # The custody admission hold committed before the bridge; the ledger row did not change.
    assert not LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_active()
    assert Closeout(directory, ORG).status()["reconciled"] is False
    assert raw_row(directory, ORG, epoch) == ("active", None, 0)
    assert len(fences(directory)) == (1 if point == "after_fence_journal" else 0)

    ledger = EngagementLedger(directory)  # restart: re-applies any journaled fence
    if point == "before_fence":
        assert raw_row(directory, ORG, epoch) == ("active", None, 0)
        with pytest.raises(sqlite3.IntegrityError, match="terminal_state_not_persisted"):
            with closing(sqlite3.connect(ledger.path)) as db, db:
                db.execute("UPDATE engagement SET slot_released=1 WHERE epoch=?", (epoch,))
        with pytest.raises(EngagementHold, match="terminal_state_not_persisted"):
            ledger.release_slot(ORG, epoch)
        with pytest.raises(EngagementHold, match="prior_engagement_not_terminal"):
            successor(ledger)
    else:
        # The fsynced journal entry is the durable termination; restart re-applies it.
        assert raw_row(directory, ORG, epoch) == ("terminal", 1, 0)
        with pytest.raises(EngagementHold, match="live_slot_not_released"):
            successor(ledger)
    assert raw_row(directory, ORG, epoch)[2] == 0

    restarted = Closeout(directory, ORG).reconcile()
    assert restarted["reconciled"] is True
    assert_retired_once(directory, epoch)
    assert successor(EngagementLedger(directory)).state == "recorded"


def test_crash_after_terminal_commit_restart_completes_release_exactly_once(
    tmp_path: Path,
) -> None:
    directory, epoch = engaged(tmp_path)
    crash_withdrawal(directory, "after_terminal_commit")

    assert raw_row(directory, ORG, epoch) == ("terminal", 1, 0)  # durable, slot still held
    with pytest.raises(EngagementHold, match="live_slot_not_released"):
        successor(EngagementLedger(directory))

    Closeout(directory, ORG).reconcile()
    assert_retired_once(directory, epoch)
    settled = ledger_bytes(directory)
    Closeout(directory, ORG).withdraw(**START)
    Closeout(directory, ORG).reconcile()
    assert ledger_bytes(directory) == settled  # no second termination or release write
    assert successor(EngagementLedger(directory)).state == "recorded"


def test_repeated_custody_withdrawal_is_one_termination_and_one_release(tmp_path: Path) -> None:
    directory, epoch = engaged(tmp_path)
    first = Closeout(directory, ORG).withdraw(**START)
    assert first["engagement"] == epoch
    assert_retired_once(directory, epoch)
    settled = ledger_bytes(directory)

    assert Closeout(directory, ORG).withdraw(**START)["engagement"] == epoch
    Closeout(directory, ORG).reconcile()
    cli = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.custody",
            "--directory",
            str(directory),
            "--org",
            ORG,
            "reconcile",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert cli.returncode == 0, cli.stderr
    assert ledger_bytes(directory) == settled
    assert_retired_once(directory, epoch)


def test_successor_is_still_refused_while_prior_engagement_is_genuinely_active(
    tmp_path: Path,
) -> None:
    directory, epoch = engaged(tmp_path)
    ledger = EngagementLedger(directory)
    with pytest.raises(EngagementHold, match="prior_engagement_not_terminal"):
        successor(ledger)  # no withdrawal at all

    # Withdrawing another organization's closeout in the same ledger touches nothing here.
    neighbor_dir = private(tmp_path / NEIGHBOR)
    neighbor_epoch = LocalTrainingStore(neighbor_dir / "training.sqlite3", NEIGHBOR).engagement_id
    neighbor = EngagementLedger(neighbor_dir)
    enroll(neighbor, NEIGHBOR, neighbor_epoch, "lease-n", 10)
    neighbor.activate(NEIGHBOR, neighbor_epoch)
    enroll(neighbor, ORG, "unbound-epoch", "lease-u", 10)
    neighbor.activate(ORG, "unbound-epoch")
    Closeout(neighbor_dir, NEIGHBOR).withdraw(**START)
    assert raw_row(neighbor_dir, NEIGHBOR, neighbor_epoch) == ("terminal", 1, 1)
    # An epoch this closeout is not bound to stays live, and its successor stays refused.
    assert raw_row(neighbor_dir, ORG, "unbound-epoch") == ("active", None, 0)
    with pytest.raises(EngagementHold, match="prior_engagement_not_terminal"):
        successor(neighbor)
    assert raw_row(directory, ORG, epoch) == ("active", None, 0)
    with pytest.raises(EngagementHold, match="prior_engagement_not_terminal"):
        successor(ledger)


def test_revocation_epoch_is_assigned_by_the_ledger_not_supplied(tmp_path: Path) -> None:
    directory = private(tmp_path / ORG)
    epoch = LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_id
    ledger = EngagementLedger(directory)
    enroll(ledger, ORG, "earlier-epoch", "lease-0", 5)
    ledger.activate(ORG, "earlier-epoch")
    ledger.withdraw(ORG, "earlier-epoch", revocation_epoch=5)
    ledger.release_slot(ORG, "earlier-epoch")
    enroll(ledger, ORG, epoch, "lease-a", 10)
    ledger.activate(ORG, epoch)

    Closeout(directory, ORG).withdraw(**START)
    assert raw_row(directory, ORG, epoch) == ("terminal", 6, 1)
    assert [f["revocation_epoch"] for f in fences(directory)] == [5, 6]


def test_released_slot_without_persisted_terminal_epoch_is_impossible(tmp_path: Path) -> None:
    directory, epoch = engaged(tmp_path)
    ledger = EngagementLedger(directory)
    with closing(sqlite3.connect(ledger.path)) as db:
        for statement in (
            "UPDATE engagement SET slot_released=1",
            "UPDATE engagement SET state='terminal', slot_released=1",
            "UPDATE engagement SET state='terminal', revocation_epoch=1, slot_released=1",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="terminal_state_not_persisted"):
                with db:
                    db.execute(statement)
    assert raw_row(directory, ORG, epoch) == ("active", None, 0)

    # A database fence without its fsynced journal entry is not a persisted termination.
    ledger.withdraw(ORG, epoch, revocation_epoch=1)
    (directory / "engagement-fences.jsonl").write_text("")
    with pytest.raises(EngagementHold, match="terminal_state_not_persisted"):
        ledger.release_slot(ORG, epoch)
    assert raw_row(directory, ORG, epoch) == ("terminal", 1, 0)
    with closing(sqlite3.connect(ledger.path)) as db:
        with pytest.raises(sqlite3.IntegrityError, match="fence_is_permanent"):
            with db:
                db.execute("DELETE FROM fence")


def test_unverifiable_ledger_keeps_the_closeout_hold(tmp_path: Path) -> None:
    directory, epoch = engaged(tmp_path)
    (directory / "engagement-fences.jsonl").write_text("not a fence\n")
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
    held = Closeout(directory, ORG)
    assert held.status()["reconciled"] is False
    assert not LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_active()
    with pytest.raises(CustodyError, match="reconcile_before_purge"):
        held.purge_local(
            engagement=epoch, agreement="terms-a", custodian="custodian-a", evidence="generated"
        )
    assert raw_row(directory, ORG, epoch) == ("active", None, 0)
