"""Custody withdrawal retires the engagement epoch before freeing its live slot (OPE-543).

Generated sentinel fixtures only. No provider, network, customer record or cross-CRM claim.
Crash cases run the real custody entry point in a child process that is killed with
``os._exit`` at a named point, so nothing after that point (no ``finally``, no commit)
runs; the parent then restarts from whatever the files durably hold.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot.custody import Closeout, CustodyError
from opentour_cell.pilot.engagement_epochs import (
    EngagementError,
    EngagementHold,
    EngagementLedger,
)
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
    if ledger.get("org-a", epoch) is None:  # the fixture may already enroll its epoch
        enroll(
            ledger,
            "org-a",
            epoch,
            DECISION.lease_ref,
            DECISION.checkpoint,
            resources=(DECISION.resource, OUTCOME.resource),
        )
        ledger.activate("org-a", epoch)
    assert ledger.get("org-a", epoch).state == "active"
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
    assert case.ledger.reserve_budget(ORG, first, "fit-1", 30)
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
    assert case.snapshot().quarantined == 1

    # The withdrawn epoch grants, spends and counts nothing, and its rows are refused later.
    assert not case.ledger.authorize(ORG, first, "queue", "queue-1")
    assert not case.ledger.has_approval(ORG, first, "approval-1")
    for kind in ("fit", "export", "read"):
        assert case.ledger.record_effect(ORG, first, kind, "late-" + kind) is False
    with pytest.raises(EngagementError, match="epoch_not_live"):
        case.ledger.reserve_budget(ORG, first, "fit-2", 1)
    assert case.ledger.get(ORG, SUCCESSOR).budget_spent == 0
    late = case.ledger.admit_row(
        ORG, SUCCESSOR, resource="decisions", lease_ref="lease-b", row_epoch=first, position=500
    )
    assert (late.verdict, late.reason) == ("deny", "prior_epoch")

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
    case.ingest(decision("D2", case_id="invoice-2"), position=93)  # redelivery counts once
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
    assert ledger.retire(ORG, "earlier-epoch").revocation_epoch == 1
    enroll(ledger, ORG, epoch, "lease-a", 10)
    ledger.activate(ORG, epoch)

    # The public withdraw only confirms the ledger's own epoch; any other value is refused.
    with pytest.raises(EngagementError, match="revocation_epoch_not_assigned"):
        ledger.withdraw(ORG, epoch, revocation_epoch=5)
    with pytest.raises(EngagementError, match="revocation_epoch_not_monotonic"):
        ledger.withdraw(ORG, epoch, revocation_epoch=1)
    with pytest.raises(EngagementError, match="revocation_epoch_conflict"):
        ledger.withdraw(ORG, "earlier-epoch", revocation_epoch=2)
    assert raw_row(directory, ORG, epoch) == ("active", None, 0)
    assert len(fences(directory)) == 1

    Closeout(directory, ORG).withdraw(**START)
    assert raw_row(directory, ORG, epoch) == ("terminal", 2, 1)
    assert [f["revocation_epoch"] for f in fences(directory)] == [1, 2]


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
    # The revocation record kept outside the snapshot now catches the emptied journal first
    # (OPE-543 anti-rollback); without that record the same release still holds with
    # terminal_state_not_persisted (test_engagement_anti_rollback.py).
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        ledger.release_slot(ORG, epoch)
    assert raw_row(directory, ORG, epoch) == ("terminal", 1, 0)
    with closing(sqlite3.connect(ledger.path)) as db:
        with pytest.raises(sqlite3.IntegrityError, match="fence_is_permanent"):
            with db:
                db.execute("DELETE FROM fence")

    # Plain SQL that inserts the fence row first does satisfy the triggers (they guard code
    # paths, not the owning uid), but without the fsynced journal entry no successor enters.
    forged_dir, forged = engaged(tmp_path / "forged")
    with closing(sqlite3.connect(forged_dir / "engagement-epochs.sqlite3")) as db, db:
        db.execute("INSERT INTO fence VALUES (?,?,1)", (ORG, forged))
        db.execute(
            "UPDATE engagement SET state='terminal', revocation_epoch=1, slot_released=1 "
            "WHERE epoch=?",
            (forged,),
        )
    assert raw_row(forged_dir, ORG, forged) == ("terminal", 1, 1)
    assert fences(forged_dir) == []
    with pytest.raises(EngagementHold, match="prior_termination_not_journaled"):
        successor(EngagementLedger(forged_dir))
    assert [e.epoch for e in EngagementLedger(forged_dir).engagements(ORG)] == [forged]


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


def test_fence_rows_cannot_be_replaced(tmp_path: Path) -> None:
    directory, epoch = engaged(tmp_path)
    Closeout(directory, ORG).withdraw(**START)
    with closing(sqlite3.connect(directory / "engagement-epochs.sqlite3")) as db:
        for statement in (
            "INSERT OR REPLACE INTO fence VALUES (?,?,9)",
            "REPLACE INTO fence VALUES (?,?,10)",
            "INSERT OR IGNORE INTO fence VALUES (?,?,11)",
        ):
            with pytest.raises(sqlite3.IntegrityError, match="fence_is_permanent"):
                with db:
                    db.execute(statement, (ORG, epoch))
        with db:
            db.execute("INSERT OR IGNORE INTO fence VALUES (?,?,1)", (ORG, epoch))  # same row
        assert db.execute("SELECT org, epoch, revocation_epoch FROM fence").fetchall() == [
            (ORG, epoch, 1)
        ]
    assert_retired_once(directory, epoch)


def test_ledger_the_closeout_has_seen_must_stay_present(tmp_path: Path) -> None:
    # Closeout opened while the ledger was present, interrupted before retiring the epoch.
    directory, epoch = engaged(tmp_path)
    crash_withdrawal(directory, "before_fence")
    database = directory / "engagement-epochs.sqlite3"
    moved = tmp_path / "moved-away.bin"
    database.rename(moved)
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).reconcile()
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
    held = Closeout(directory, ORG)
    assert held.status()["reconciled"] is False
    with pytest.raises(CustodyError, match="reconcile_before_purge"):
        held.purge_local(
            engagement=epoch, agreement="terms-a", custodian="custodian-a", evidence="generated"
        )
    moved.rename(database)
    assert raw_row(directory, ORG, epoch) == ("active", None, 0)
    assert Closeout(directory, ORG).reconcile()["reconciled"] is True
    assert_retired_once(directory, epoch)

    # Once retired, deleting the whole ledger cannot quietly lift the requirement.
    database.unlink()
    (directory / "engagement-fences.jsonl").unlink()
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).reconcile()
    assert Closeout(directory, ORG).status()["reconciled"] is False


def test_live_grant_requires_the_ledger_and_this_engagements_row(tmp_path: Path) -> None:
    case = SignedCase(tmp_path / "worker")
    case.body["engagement_ledger_required"] = True
    case.write()
    epoch = case.body["engagement_epoch"]
    ledger = EngagementLedger(case.directory)
    if ledger.get("org-a", epoch) is None:  # the fixture may already enroll its epoch
        enroll(
            ledger,
            "org-a",
            epoch,
            DECISION.lease_ref,
            DECISION.checkpoint,
            resources=(DECISION.resource, OUTCOME.resource),
        )
        ledger.activate("org-a", epoch)
    database = case.directory / "engagement-epochs.sqlite3"
    elsewhere = tmp_path / "elsewhere.sqlite3"
    database.rename(elsewhere)  # absent before the closeout ever opens
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(case.directory, "org-a").withdraw(**START)
    assert Closeout(case.directory, "org-a").status()["reconciled"] is False
    elsewhere.rename(database)
    assert Closeout(case.directory, "org-a").reconcile()["reconciled"] is True
    retired = EngagementLedger(case.directory).get("org-a", epoch)
    assert (retired.state, retired.revocation_epoch, retired.slot_released) == ("terminal", 1, True)

    # A live grant with a ledger that lacks this engagement's row is a hold, not a no-op.
    directory = private(tmp_path / ORG)
    LocalTrainingStore(directory / "training.sqlite3", ORG)
    other = EngagementLedger(directory)
    enroll(other, ORG, "unrelated-epoch", "lease-u", 10)
    other.activate(ORG, "unrelated-epoch")
    grant = directory / "live-authority-generated.json"
    grant.write_text("{}")
    grant.chmod(0o600)
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
    assert Closeout(directory, ORG).status()["reconciled"] is False
    assert raw_row(directory, ORG, "unrelated-epoch") == ("active", None, 0)


def closeout_ledger_state(directory: Path) -> str | None:
    with closing(sqlite3.connect(directory / "custody.sqlite3")) as db:
        return db.execute("SELECT engagement_ledger FROM closeout").fetchone()[0]


def test_older_ledger_backup_without_the_seen_row_holds_the_closeout(tmp_path: Path) -> None:
    directory = private(tmp_path / ORG)
    epoch = LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_id
    ledger = EngagementLedger(directory)
    older = tmp_path / "older.sqlite3"
    ledger.backup(older)  # taken before this epoch existed
    enroll(ledger, ORG, epoch, "lease-a", 10)
    ledger.activate(ORG, epoch)
    ledger.observe_positions(ORG, epoch, 31)
    crash_withdrawal(directory, "before_fence")  # the closeout opened and saw the row
    assert closeout_ledger_state(directory) == "row_seen"

    # The ledger refuses to restore a backup older than its first grant (OPE-543 review F2);
    # a volume restore puts the same bytes back out of band, and custody still holds.
    with pytest.raises(EngagementHold, match="anti_rollback_ledger_rewound"):
        EngagementLedger(directory).restore_backup(older)
    shutil.copyfile(older, directory / "engagement-epochs.sqlite3")
    assert fences(directory) == []
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).reconcile()
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
    held = Closeout(directory, ORG)
    assert held.status()["reconciled"] is False
    with pytest.raises(CustodyError, match="reconcile_before_purge"):
        held.purge_local(
            engagement=epoch, agreement="terms-a", custodian="custodian-a", evidence="generated"
        )
    assert closeout_ledger_state(directory) == "row_seen"  # never weakened


def test_row_absent_when_the_closeout_opened_keeps_existing_behaviour(tmp_path: Path) -> None:
    directory = private(tmp_path / ORG)
    epoch = LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_id
    ledger = EngagementLedger(directory)
    enroll(ledger, ORG, "unrelated-epoch", "lease-u", 10)
    ledger.activate(ORG, "unrelated-epoch")
    before = (directory / "engagement-epochs.sqlite3").read_bytes()

    report = Closeout(directory, ORG).withdraw(**START)
    assert report["engagement"] == epoch and report["reconciled"] is True
    assert closeout_ledger_state(directory) == "present"
    assert ledger.get(ORG, epoch) is None  # the open-time lookup created nothing
    assert raw_row(directory, ORG, "unrelated-epoch") == ("active", None, 0)
    assert fences(directory) == []
    assert (directory / "engagement-epochs.sqlite3").read_bytes() == before
    assert Closeout(directory, ORG).reconcile()["reconciled"] is True


def settle_receipt_only(directory: Path) -> dict:
    closeout = Closeout(directory, ORG)
    for item, kind, disposition in (
        ("inventory-review", "inventory", "inventory_verified"),
        ("support-handoff", "support", "handoff_confirmed"),
    ):
        closeout.record(
            item=item,
            kind=kind,
            disposition=disposition,
            owner="audit-owner",
            due="2040-04-01",
            evidence="generated-receipt",
        )
    return closeout.finish(custodian="custodian-a", evidence="generated-closure", receipt_only=True)


def test_journal_without_its_database_holds_and_is_not_recreated(tmp_path: Path) -> None:
    # S1b: an earlier epoch's fence is journaled; the database is gone before the closeout opens.
    directory = private(tmp_path / ORG)
    epoch = LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_id
    ledger = EngagementLedger(directory)
    enroll(ledger, ORG, "earlier-epoch", "lease-0", 5)
    ledger.activate(ORG, "earlier-epoch")
    ledger.retire(ORG, "earlier-epoch")
    enroll(ledger, ORG, epoch, "lease-a", 40)
    ledger.activate(ORG, epoch)
    (directory / "engagement-epochs.lock").unlink()  # isolate the journal-only path
    database = directory / "engagement-epochs.sqlite3"
    database.unlink()
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
    assert not database.exists()  # reconcile did not recreate an empty ledger
    assert Closeout(directory, ORG).status()["reconciled"] is False
    assert len(fences(directory)) == 1


def test_zero_byte_ledger_database_holds(tmp_path: Path) -> None:
    # S2: a truncated ledger is not an empty ledger.
    directory, epoch = engaged(tmp_path)
    database = directory / "engagement-epochs.sqlite3"
    database.write_bytes(b"")
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
    with closing(sqlite3.connect(database)) as db:  # no schema was created over it
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone()[0] == 0
    assert Closeout(directory, ORG).status()["reconciled"] is False


@pytest.mark.parametrize("trace", ["engagement-epochs.lock", "live-evidence.sqlite3"])
def test_ledger_deleted_while_its_traces_remain_holds(tmp_path: Path, trace: str) -> None:
    # S11: the ledger is gone before the closeout opens, but the directory shows it was used.
    directory, epoch = engaged(tmp_path)
    if trace == "engagement-epochs.lock":
        with EngagementLedger(directory).pin():  # what every pinned live write leaves behind
            pass
    else:
        (directory / trace).write_bytes(b"")
        (directory / trace).chmod(0o600)
    assert (directory / trace).exists()
    (directory / "engagement-epochs.sqlite3").unlink()
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
    assert closeout_ledger_state(directory) == "present"
    assert Closeout(directory, ORG).status()["reconciled"] is False
    assert not (directory / "engagement-epochs.sqlite3").exists()


def test_older_backup_restored_before_the_closeout_opens_holds(tmp_path: Path) -> None:
    # S6: the restore itself takes the epoch lock, so the missing row is a hold, not a skip.
    directory = private(tmp_path / ORG)
    epoch = LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_id
    ledger = EngagementLedger(directory)
    older = tmp_path / "older.sqlite3"
    ledger.backup(older)
    enroll(ledger, ORG, epoch, "lease-a", 10)
    ledger.activate(ORG, epoch)
    # Refused by the ledger (OPE-543 review F2); the same bytes restored out of band still hold.
    with pytest.raises(EngagementHold, match="anti_rollback_ledger_rewound"):
        ledger.restore_backup(older)
    shutil.copyfile(older, directory / "engagement-epochs.sqlite3")
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
    assert Closeout(directory, ORG).status()["reconciled"] is False


def test_busy_ledger_at_open_refuses_instead_of_recording_a_guess(tmp_path: Path) -> None:
    directory, epoch = engaged(tmp_path)
    holder = sqlite3.connect(directory / "engagement-epochs.sqlite3", isolation_level=None)
    try:
        holder.execute("BEGIN EXCLUSIVE")
        with pytest.raises(CustodyError, match="engagement_ledger_busy"):
            Closeout(directory, ORG).withdraw(**START)
    finally:
        holder.execute("ROLLBACK")
        holder.close()
    # Admission stopped first; the closeout did not open on a downgraded record.
    assert not LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_active()
    with pytest.raises(CustodyError, match="closeout_not_open"):
        Closeout(directory, ORG).status()
    assert raw_row(directory, ORG, epoch) == ("active", None, 0)
    # The caller retries once the ledger is free.
    assert Closeout(directory, ORG).withdraw(**START)["reconciled"] is True
    assert closeout_ledger_state(directory) == "bound"
    assert_retired_once(directory, epoch)


def test_ledger_present_at_open_without_the_row_must_stay_present(tmp_path: Path) -> None:
    directory = private(tmp_path / ORG)
    LocalTrainingStore(directory / "training.sqlite3", ORG)
    ledger = EngagementLedger(directory)
    enroll(ledger, ORG, "unrelated-epoch", "lease-u", 10)
    ledger.activate(ORG, "unrelated-epoch")
    assert Closeout(directory, ORG).withdraw(**START)["reconciled"] is True
    assert closeout_ledger_state(directory) == "present"
    assert not (directory / "engagement-epochs.lock").exists()
    (directory / "engagement-epochs.sqlite3").unlink()
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).reconcile()
    assert Closeout(directory, ORG).status()["reconciled"] is False


def test_minimized_closeout_still_retires_and_requires_the_ledger(tmp_path: Path) -> None:
    directory, epoch = engaged(tmp_path)
    crash_withdrawal(directory, "before_fence")
    before_retire = tmp_path / "before-retire.sqlite3"
    EngagementLedger(directory).backup(before_retire)  # row active, slot held
    assert Closeout(directory, ORG).reconcile()["reconciled"] is True
    closed = settle_receipt_only(directory)
    assert closed["state"] == "closed" and closed["retained"] == "receipt_only"
    assert_retired_once(directory, epoch)

    # Restoring the pre-retirement ledger re-applies the journaled fence but holds the slot;
    # both minimized entry points must complete the release again.
    for entry in ("reconcile", "withdraw"):
        EngagementLedger(directory).restore_backup(before_retire)
        assert raw_row(directory, ORG, epoch) == ("terminal", 1, 0)
        closeout = Closeout(directory, ORG)
        result = closeout.reconcile() if entry == "reconcile" else closeout.withdraw(**START)
        assert result == closed
        assert_retired_once(directory, epoch)

    # And a minimized closeout still requires the ledger it retired.
    (directory / "engagement-epochs.sqlite3").unlink()
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).reconcile()
    with pytest.raises(CustodyError, match="engagement_ledger_unavailable"):
        Closeout(directory, ORG).withdraw(**START)
