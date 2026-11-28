"""Anti-rollback marker and revocation records for the engagement ledger (OPE-543).

What these records protect against, and what they do not: they defend against a snapshot
restore or an accidental rollback of the worker volume. They are worker-attested at most.
Both worker units run as one uid, which can rewrite the volume and wipe the anti-rollback
directory, so nothing here is tamper-proof against a hostile same-uid process; that needs a
separate uid or an external anchor (OPE-566/OPE-583).

Generated sentinel fixtures only. Every location is a pytest temporary directory; the real
``/var/lib/opentour-antirollback`` is never touched (see ``conftest.py``). The module's new
names are read through the module object so this file still collects on a tree without
them, where the residual repros below reconcile instead of failing closed.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path

import opentour_cell.pilot.engagement_epochs as epochs
import pytest
from opentour_cell.pilot.custody import Closeout, CustodyError
from opentour_cell.pilot.engagement_epochs import (
    EngagementError,
    EngagementHold,
    EngagementLedger,
)
from opentour_cell.pilot.training import LocalTrainingStore

ORG = "generated-org-1"
OTHER_ORG = "generated-org-2"
RES = ("decisions", "truth")
START = {
    "agreement": "terms-a",
    "custodian": "custodian-a",
    "support": "support-a",
    "due": "2040-04-01",
    "trigger": "withdrawal",
}
CRASH = 86
DATABASE = "engagement-epochs.sqlite3"
JOURNAL = "engagement-fences.jsonl"
LOCK = "engagement-epochs.lock"
HOLD = "engagement_ledger_unavailable"


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    return path


def enroll(ledger, epoch, lease="lease-a", checkpoint=10, *, org=ORG, **overrides):
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
    """An active epoch bound to the directory's training engagement; no grant, lock or
    live-evidence file is created, which is exactly the residual's directory."""
    directory = private(parent / ORG)
    epoch = LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_id
    ledger = EngagementLedger(directory)
    enroll(ledger, epoch)
    ledger.activate(ORG, epoch)
    return directory, epoch


def marker_file(location: Path, directory: Path) -> Path:
    return epochs.record_path(location, epochs.marker_key(directory))


def revocation_file(location: Path, directory: Path, org: str = ORG) -> Path:
    store = read_record(marker_file(location, directory))["value"]["store_id"]
    return epochs.record_path(location, epochs.revocation_key(store, org))


def read_record(path: Path) -> dict:
    return json.loads(path.read_bytes())


def snapshot(directory: Path) -> dict[str, bytes | None]:
    """What a volume snapshot of the ledger holds: bytes, or None when absent."""
    return {
        name: (directory / name).read_bytes() if (directory / name).exists() else None
        for name in (DATABASE, JOURNAL)
    }


def restore(directory: Path, taken: dict[str, bytes | None]) -> None:
    """Restore a snapshot in place, out of band, as a volume restore would."""
    for name, data in taken.items():
        path = directory / name
        if data is None:
            path.unlink(missing_ok=True)
        else:
            path.write_bytes(data)
            path.chmod(0o600)


def rows(directory: Path) -> list[tuple]:
    with closing(sqlite3.connect(directory / DATABASE)) as db:
        return db.execute("SELECT org, epoch, state FROM engagement ORDER BY ordinal").fetchall()


# -- the residual: ledger deleted before the closeout opens --------------------------------


def test_ledger_deleted_before_closeout_opens_without_traces_fails_closed(tmp_path: Path) -> None:
    directory, epoch = engaged(tmp_path)
    for name in (LOCK, "live-evidence.sqlite3"):
        assert not (directory / name).exists()
    assert not list(directory.glob("live-authority-*.json"))
    (directory / DATABASE).unlink()  # nothing in the volume shows a ledger was ever used

    with pytest.raises(CustodyError, match=HOLD):
        Closeout(directory, ORG).withdraw(**START)
    with closing(sqlite3.connect(directory / "custody.sqlite3")) as db:
        # The marker is recorded at open as a ledger that must stay present.
        assert db.execute("SELECT engagement_ledger FROM closeout").fetchone()[0] == "present"
    held = Closeout(directory, ORG)
    assert held.status()["reconciled"] is False
    with pytest.raises(CustodyError, match="reconcile_before_purge"):
        held.purge_local(
            engagement=epoch, agreement="terms-a", custodian="custodian-a", evidence="generated"
        )
    with pytest.raises(CustodyError, match=HOLD):
        held.reconcile()
    assert not (directory / DATABASE).exists()  # the hold recreated nothing


@pytest.mark.parametrize("recorded", [None, "absent"])
def test_closeout_opened_by_earlier_code_in_the_residual_state_holds_on_reconcile(
    tmp_path: Path, recorded: str | None
) -> None:
    # A closeout opened before this slice recorded the deleted ledger as absent (or, before
    # PR #208, recorded nothing). The marker still requires the ledger at reconcile.
    directory, _ = engaged(tmp_path)
    (directory / DATABASE).unlink()
    with pytest.raises(CustodyError, match=HOLD):
        Closeout(directory, ORG).withdraw(**START)
    with closing(sqlite3.connect(directory / "custody.sqlite3")) as db, db:
        db.execute("UPDATE closeout SET engagement_ledger=?, reconciled=1", (recorded,))
    with pytest.raises(CustodyError, match=HOLD):
        Closeout(directory, ORG).reconcile()
    assert Closeout(directory, ORG).status()["reconciled"] is False


def test_whole_ledger_and_its_traces_deleted_after_a_withdrawal_fails_closed(
    tmp_path: Path,
) -> None:
    directory = private(tmp_path / ORG)
    epoch = LocalTrainingStore(directory / "training.sqlite3", ORG).engagement_id
    ledger = EngagementLedger(directory)
    enroll(ledger, "earlier-epoch", "lease-0", 5)
    ledger.activate(ORG, "earlier-epoch")
    ledger.retire(ORG, "earlier-epoch")
    enroll(ledger, epoch, "lease-a", 40)
    ledger.activate(ORG, epoch)
    for name in (DATABASE, JOURNAL, LOCK):
        (directory / name).unlink()
    with pytest.raises(CustodyError, match=HOLD):
        Closeout(directory, ORG).withdraw(**START)
    assert Closeout(directory, ORG).status()["reconciled"] is False


def test_marker_present_while_the_ledger_is_missing_holds_and_creates_nothing(
    tmp_path: Path,
) -> None:
    directory, _ = engaged(tmp_path)
    (directory / DATABASE).unlink()
    with pytest.raises(EngagementHold, match="anti_rollback_ledger_missing"):
        EngagementLedger(directory)
    assert not (directory / DATABASE).exists()
    with pytest.raises(EngagementError):
        EngagementLedger(directory, existing_only=True)
    assert not (directory / DATABASE).exists()


# -- revocation records: a restored pre-withdrawal snapshot cannot un-withdraw ---------------


def test_restored_pre_withdrawal_snapshot_is_refused(tmp_path: Path) -> None:
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    enroll(ledger, "epoch-1")
    ledger.activate(ORG, "epoch-1")
    before = snapshot(directory)
    ledger.retire(ORG, "epoch-1")
    after = snapshot(directory)

    restore(directory, before)  # the volume now says epoch-1 is active again
    assert rows(directory) == [(ORG, "epoch-1", "active")]
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        EngagementLedger(directory)  # startup
    reopened = EngagementLedger(directory, existing_only=True)
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        reopened.live_gate(ORG, "epoch-1", "decisions", "lease-a")  # before any live write
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        reopened.record_effect(ORG, "epoch-1", "decision", "late-1")
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        reopened.bind(ORG, "epoch-1", "queue", "queue-1")
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        enroll(reopened, "epoch-2", "lease-b", 40)
    assert rows(directory) == [(ORG, "epoch-1", "active")]  # every refusal changed nothing

    restore(directory, after)
    current = EngagementLedger(directory)
    assert current.get(ORG, "epoch-1").state == "terminal"
    with pytest.raises(EngagementHold, match="epoch_terminal"):
        current.live_gate(ORG, "epoch-1", "decisions", "lease-a")


def test_pre_withdrawal_snapshot_holds_the_custody_closeout(tmp_path: Path) -> None:
    directory, epoch = engaged(tmp_path)
    before = snapshot(directory)
    assert Closeout(directory, ORG).withdraw(**START)["reconciled"] is True
    restore(directory, before)
    with pytest.raises(CustodyError, match=HOLD):
        Closeout(directory, ORG).reconcile()
    assert Closeout(directory, ORG).status()["reconciled"] is False


def test_emptied_journal_with_an_older_database_is_refused(tmp_path: Path) -> None:
    # The reviewer's probe on PR #168: deleting the journal and restoring an older database
    # brought an active epoch back. The revocation record outside the volume now refuses it.
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    enroll(ledger, "epoch-1")
    ledger.activate(ORG, "epoch-1")
    older = tmp_path / "older.sqlite3"
    ledger.backup(older)
    ledger.retire(ORG, "epoch-1")
    (directory / JOURNAL).unlink()
    shutil.copyfile(older, directory / DATABASE)
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        EngagementLedger(directory)
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        EngagementLedger(directory, existing_only=True).get(ORG, "epoch-1")


def test_revocation_record_is_durable_before_the_slot_is_released(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    enroll(ledger, "epoch-1")
    ledger.activate(ORG, "epoch-1")
    ledger.withdraw(ORG, "epoch-1", revocation_epoch=1)
    record = revocation_file(anti_rollback_directory, directory)
    stored = read_record(record)
    assert stored["key"] == epochs.revocation_key(stored["value"]["store_id"], ORG)
    assert stored["sequence"] == "1"
    assert {k: stored["value"][k] for k in ("org", "epoch", "revocation_epoch")} == {
        "org": ORG,
        "epoch": "epoch-1",
        "revocation_epoch": "1",
    }
    assert stored["value"]["assurance"] == "worker_attested"
    assert stored["value"]["protects_against"] == "snapshot_restore_only"

    saved = record.read_bytes()
    record.unlink()  # the record was lost before the release
    with pytest.raises(EngagementHold, match="anti_rollback_revocation_not_durable"):
        ledger.release_slot(ORG, "epoch-1")
    assert ledger.get(ORG, "epoch-1").slot_released is False
    # Without the record, an emptied journal is still caught by the ledger's own check.
    journal = (directory / JOURNAL).read_bytes()
    (directory / JOURNAL).write_text("")
    with pytest.raises(EngagementHold, match="terminal_state_not_persisted"):
        ledger.release_slot(ORG, "epoch-1")
    (directory / JOURNAL).write_bytes(journal)
    record.write_bytes(saved)
    assert ledger.release_slot(ORG, "epoch-1").slot_released is True


def test_withdrawal_refuses_before_any_write_when_the_record_cannot_be_kept(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    enroll(ledger, "epoch-1")
    ledger.activate(ORG, "epoch-1")
    # The marker stays readable, but no record can be written: the writer lock is unusable.
    lock = anti_rollback_directory / "pilot-trust-state.lock"
    lock.unlink()  # left by the first-grant marker write
    private(lock)
    database = (directory / DATABASE).read_bytes()
    for withdraw in (
        lambda: ledger.retire(ORG, "epoch-1"),
        lambda: ledger.withdraw(ORG, "epoch-1", revocation_epoch=1),
    ):
        with pytest.raises(EngagementHold, match="anti_rollback_unavailable"):
            withdraw()
        assert not (directory / JOURNAL).exists()  # no fence journaled
        assert (directory / DATABASE).read_bytes() == database
    assert not list((anti_rollback_directory / "records").glob("*.tmp"))
    lock.rmdir()
    assert ledger.retire(ORG, "epoch-1").slot_released is True


def test_revocation_record_only_moves_forward_and_detects_equivocation(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    enroll(ledger, "epoch-1")
    ledger.activate(ORG, "epoch-1")
    ledger.retire(ORG, "epoch-1")
    enroll(ledger, "epoch-2", "lease-b", 40)
    ledger.activate(ORG, "epoch-2")
    ledger.retire(ORG, "epoch-2")
    record = revocation_file(anti_rollback_directory, directory)
    assert read_record(record)["sequence"] == "2"
    ledger.retire(ORG, "epoch-1")  # repeating an older retirement never lowers the record
    assert read_record(record)["sequence"] == "2"
    assert read_record(record)["value"]["epoch"] == "epoch-2"

    # A record that names a different epoch at the same revocation epoch is a conflict.
    stored = read_record(record)
    stored["value"]["epoch"] = "epoch-1"
    record.write_bytes(epochs._canonical(stored))
    with pytest.raises(EngagementHold, match="anti_rollback_revocation_conflict"):
        EngagementLedger(directory)


def test_revocation_records_cover_an_organization_missing_from_the_volume(
    tmp_path: Path,
) -> None:
    directory = private(tmp_path / "owner")
    ledger = EngagementLedger(directory)
    enroll(ledger, "epoch-1", "lease-o", org=OTHER_ORG)
    empty = snapshot(directory)  # before this organization existed
    enroll(ledger, "epoch-1")
    ledger.activate(ORG, "epoch-1")
    ledger.retire(ORG, "epoch-1")
    restore(directory, empty)
    with pytest.raises(EngagementHold, match="anti_rollback_volume_behind"):
        enroll(EngagementLedger(directory), "epoch-9", "lease-z", 90)


# -- the marker ------------------------------------------------------------------------------


def test_marker_is_written_at_first_grant_ingest_and_binds_the_store(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    assert not marker_file(anti_rollback_directory, directory).exists()  # no grant yet
    before = time.time()
    enroll(ledger, "epoch-1")
    after = time.time()
    stored = read_record(marker_file(anti_rollback_directory, directory))
    status = os.stat(directory)
    value = stored["value"]
    assert stored["format"] == "opentour.pilot.trust-state.v1"
    assert stored["key"] == epochs.marker_key(directory)
    assert stored["sequence"] == "2"  # 1 while the first ingest is in flight, 2 once committed
    assert value["directory"] == os.path.realpath(directory)
    assert (value["device"], value["inode"]) == (str(status.st_dev), str(status.st_ino))
    with closing(sqlite3.connect(directory / DATABASE)) as db:
        assert value["store_id"] == db.execute("SELECT store_id FROM ledger_store").fetchone()[0]
    assert value["assurance"] == "worker_attested"
    assert value["protects_against"] == "snapshot_restore_only"
    written = datetime.fromisoformat(value["written_at"].replace("Z", "+00:00")).timestamp()
    assert before - 1 <= written <= after + 1  # the worker's own clock at ingest
    marker = marker_file(anti_rollback_directory, directory).read_bytes()
    enroll(ledger, "epoch-1")  # idempotent re-ingest rewrites nothing
    enroll(ledger, "epoch-1", "lease-o", org=OTHER_ORG)
    assert marker_file(anti_rollback_directory, directory).read_bytes() == marker


def test_ingest_signature_takes_no_caller_clock() -> None:
    for method in (
        EngagementLedger.record_engagement,
        EngagementLedger.retire,
        EngagementLedger.withdraw,
    ):
        names = set(inspect.signature(method).parameters)
        assert not names & {"now", "clock", "time", "timestamp", "written_at", "recorded_at"}


def test_records_use_the_trust_state_format_without_a_format_change(
    tmp_path: Path, anti_rollback_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from opentour_cell.pilot import signed_records
    from opentour_journal.crypto import canonical

    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    enroll(ledger, "epoch-1")
    ledger.activate(ORG, "epoch-1")
    ledger.retire(ORG, "epoch-1")
    monkeypatch.setattr(signed_records, "ANTI_ROLLBACK_DIRECTORY", anti_rollback_directory)
    store = signed_records.TrustStateStore()
    for path, key in (
        (marker_file(anti_rollback_directory, directory), epochs.marker_key(directory)),
        (revocation_file(anti_rollback_directory, directory), None),
    ):
        data = path.read_bytes()
        stored = json.loads(data)
        assert canonical(stored) == data
        record = store.read(key or stored["key"])
        assert record is not None
        assert (record.key, str(record.sequence), dict(record.value)) == (
            stored["key"],
            stored["sequence"],
            stored["value"],
        )


@pytest.mark.parametrize(
    "location",
    [
        "missing",
        "regular_file",
        "group_writable",
        "symlink",
        "records_is_file",
        "lock_is_directory",
        "unset",
        "inside",
    ],
)
def test_unusable_location_refuses_first_grant_ingest_without_advancing_state(
    tmp_path: Path,
    anti_rollback_directory: Path,
    monkeypatch: pytest.MonkeyPatch,
    location: str,
) -> None:
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    target = anti_rollback_directory
    if location == "missing":
        target = tmp_path / "absent"
    elif location == "regular_file":
        target = tmp_path / "plain-file"
        target.write_bytes(b"")
    elif location == "group_writable":
        target.chmod(0o770)
    elif location == "symlink":
        target = tmp_path / "linked"
        target.symlink_to(anti_rollback_directory)
    elif location == "records_is_file":
        (target / "records").write_bytes(b"")
    elif location == "lock_is_directory":
        private(target / "pilot-trust-state.lock")
    elif location == "inside":
        target = private(directory / "antirollback")
    if location == "unset":
        monkeypatch.setattr(epochs, "ANTI_ROLLBACK_DIRECTORY", None)
        monkeypatch.delenv(epochs.ANTI_ROLLBACK_ENV)
    else:
        monkeypatch.setenv(epochs.ANTI_ROLLBACK_ENV, str(target))
    database = (directory / DATABASE).read_bytes()
    with pytest.raises(EngagementHold, match="anti_rollback_"):
        enroll(ledger, "epoch-1")
    assert (directory / DATABASE).read_bytes() == database
    assert rows(directory) == []
    assert not list(anti_rollback_directory.rglob("*.json"))
    assert not list(anti_rollback_directory.rglob("*.tmp"))
    # Once the location is usable the same ingest succeeds.
    anti_rollback_directory.chmod(0o700)
    if (anti_rollback_directory / "records").is_file():
        (anti_rollback_directory / "records").unlink()
    if (anti_rollback_directory / "pilot-trust-state.lock").is_dir():
        (anti_rollback_directory / "pilot-trust-state.lock").rmdir()
    monkeypatch.setenv(epochs.ANTI_ROLLBACK_ENV, str(anti_rollback_directory))
    assert enroll(ledger, "epoch-1").state == "recorded"


def test_default_location_is_the_worker_host_state_directory_and_tests_never_use_it() -> None:
    assert epochs.ANTI_ROLLBACK_DIRECTORY == Path("/var/lib/opentour-antirollback")
    configured = Path(os.environ[epochs.ANTI_ROLLBACK_ENV])
    assert configured.resolve() != Path("/var/lib/opentour-antirollback")


def test_atomic_marker_write_leaves_no_partial_record(
    tmp_path: Path, anti_rollback_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)

    def interrupted(*_args):
        raise OSError("generated interruption before rename")

    replace = os.replace
    monkeypatch.setattr(epochs.os, "replace", interrupted)
    with pytest.raises(EngagementHold, match="anti_rollback_unavailable"):
        enroll(ledger, "epoch-1")
    monkeypatch.setattr(epochs.os, "replace", replace)
    assert not marker_file(anti_rollback_directory, directory).exists()
    assert not list(anti_rollback_directory.rglob("*.tmp"))
    assert rows(directory) == []
    assert enroll(ledger, "epoch-1").state == "recorded"


CHILD = r"""
import os
import sys
from pathlib import Path

import opentour_cell.pilot.engagement_epochs as epochs

directory = Path(sys.argv[1])
write = epochs._write_record


def written_then_crash(*args, **kwargs):
    write(*args, **kwargs)
    os._exit(86)


epochs._write_record = written_then_crash
ledger = epochs.EngagementLedger(directory)
ledger.record_engagement(
    "generated-org-1",
    "epoch-1",
    lease_ref="lease-a",
    authority_digest="a" * 64,
    resources=("decisions",),
    lineage_ref="generated-lineage",
    checkpoint=10,
    budget_limit=100,
)
raise SystemExit(0)
"""


def test_crash_after_marker_before_ledger_commit_recovers_on_retry(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    directory = private(tmp_path / ORG)
    EngagementLedger(directory)
    child = subprocess.run(
        [sys.executable, "-c", CHILD, str(directory)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert child.returncode == CRASH, child.stderr
    assert read_record(marker_file(anti_rollback_directory, directory))["sequence"] == "1"
    assert rows(directory) == []
    ledger = EngagementLedger(directory)
    engagement = ledger.record_engagement(
        ORG,
        "epoch-1",
        lease_ref="lease-a",
        authority_digest="a" * 64,
        resources=("decisions",),
        lineage_ref="generated-lineage",
        checkpoint=10,
        budget_limit=100,
    )
    assert engagement.state == "recorded"
    assert read_record(marker_file(anti_rollback_directory, directory))["sequence"] == "2"


@pytest.mark.parametrize(
    "damage",
    [
        "garbage",
        "not_canonical",
        "wrong_key",
        "wrong_label",
        "extra_field",
        "bad_sequence",
        "sequence_three",
    ],
)
def test_corrupt_marker_fails_closed(
    tmp_path: Path, anti_rollback_directory: Path, damage: str
) -> None:
    directory, epoch = engaged(tmp_path)
    path = marker_file(anti_rollback_directory, directory)
    stored = read_record(path)
    if damage == "garbage":
        data = b"\x00not a record"
    elif damage == "not_canonical":
        data = json.dumps(stored, indent=1).encode()
    else:
        if damage == "wrong_key":
            stored["key"] = epochs.marker_key(tmp_path)
        elif damage == "wrong_label":
            stored["value"]["assurance"] = "tamper_proof"
        elif damage == "extra_field":
            stored["value"]["note"] = "generated"
        elif damage == "bad_sequence":
            stored["sequence"] = "0"
        else:
            stored["sequence"] = "3"
        data = epochs._canonical(stored)
    path.write_bytes(data)
    with pytest.raises(EngagementHold, match="anti_rollback_marker_corrupt"):
        EngagementLedger(directory)
    with pytest.raises(EngagementHold, match="anti_rollback_marker_corrupt"):
        EngagementLedger(directory, existing_only=True).live_gate(
            ORG, epoch, "decisions", "lease-a"
        )
    assert epochs.ledger_marker_present(directory) is True
    with pytest.raises(CustodyError, match=HOLD):
        Closeout(directory, ORG).withdraw(**START)


def test_marker_missing_while_the_ledger_has_history_holds_and_is_not_rewritten(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    directory, epoch = engaged(tmp_path)
    path = marker_file(anti_rollback_directory, directory)
    path.unlink()
    with pytest.raises(EngagementHold, match="anti_rollback_marker_missing"):
        EngagementLedger(directory)
    with pytest.raises(EngagementHold, match="anti_rollback_marker_missing"):
        EngagementLedger(directory, existing_only=True).live_gate(
            ORG, epoch, "decisions", "lease-a"
        )
    with pytest.raises(EngagementHold, match="anti_rollback_marker_missing"):
        enroll(EngagementLedger(directory, existing_only=True), "epoch-2", "lease-b", 40)
    assert not path.exists()  # never re-derived from the volume it protects
    with pytest.raises(CustodyError, match=HOLD):
        Closeout(directory, ORG).withdraw(**START)


def test_marker_missing_on_a_fresh_ledger_is_the_ordinary_first_grant(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    assert EngagementLedger(directory).organizations() == ()
    assert epochs.ledger_marker_present(directory) is False
    enroll(ledger, "epoch-1")
    assert epochs.ledger_marker_present(directory) is True


def test_a_marker_from_a_different_store_does_not_match(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    first, _ = engaged(tmp_path / "a")
    second, _ = engaged(tmp_path / "b")
    assert (
        read_record(marker_file(anti_rollback_directory, first))["value"]["store_id"]
        != (read_record(marker_file(anti_rollback_directory, second))["value"]["store_id"])
    )
    # The other store's ledger copied over this one: same path, different store id.
    original = (first / DATABASE).read_bytes()
    shutil.copyfile(second / DATABASE, first / DATABASE)
    with pytest.raises(EngagementHold, match="anti_rollback_store_mismatch"):
        EngagementLedger(first)
    (first / DATABASE).write_bytes(original)
    assert EngagementLedger(first).organizations() == (ORG,)

    # The same path recreated as a new directory: same store id, different inode.
    moved = tmp_path / "moved"
    first.rename(moved)
    private(first)
    for name in os.listdir(moved):
        shutil.copy2(moved / name, first / name)
    with pytest.raises(EngagementHold, match="anti_rollback_store_mismatch"):
        EngagementLedger(first)
    with pytest.raises(CustodyError, match=HOLD):
        Closeout(first, ORG).withdraw(**START)

    # A marker for a different path does not bind an unrelated fresh directory.
    third = private(tmp_path / "c" / ORG)
    assert epochs.ledger_marker_present(third) is False
    assert enroll(EngagementLedger(third), "epoch-1").state == "recorded"


def test_restore_backup_checks_the_backup_before_replacing_anything(tmp_path: Path) -> None:
    first, _ = engaged(tmp_path / "a")
    second, _ = engaged(tmp_path / "b")
    foreign = tmp_path / "foreign.sqlite3"
    EngagementLedger(second).backup(foreign)
    ledger = EngagementLedger(first)
    before = (first / DATABASE).read_bytes()
    with pytest.raises(EngagementHold, match="anti_rollback_store_mismatch"):
        ledger.restore_backup(foreign)
    assert (first / DATABASE).read_bytes() == before
    assert not (first / (DATABASE + ".restore")).exists()
    own = tmp_path / "own.sqlite3"
    ledger.backup(own)
    ledger.restore_backup(own)  # this store's own backup is accepted
    assert ledger.organizations() == (ORG,)


def test_existing_custody_behaviour_without_this_engagements_row_is_kept(
    tmp_path: Path,
) -> None:
    # The marker means "a ledger must be here", not "this engagement's row must be here".
    directory = private(tmp_path / ORG)
    LocalTrainingStore(directory / "training.sqlite3", ORG)
    ledger = EngagementLedger(directory)
    enroll(ledger, "unrelated-epoch", "lease-u", 10)
    ledger.activate(ORG, "unrelated-epoch")
    assert epochs.ledger_marker_present(directory) is True
    assert Closeout(directory, ORG).withdraw(**START)["reconciled"] is True


def test_ledger_module_import_closure_stays_inside_the_packaged_worker() -> None:
    tree = ast.parse(Path(epochs.__file__).read_text())
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    project = {name for name in imported if name.startswith("opentour")}
    assert project == {"opentour_cell.pilot.text_classifier", "opentour_cell.pilot.training"}


# -- review findings on f11c2cb (F1, F2, F3) ----------------------------------------------------


def test_location_on_the_same_volume_as_the_ledger_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F1, the reviewer's repro: a sibling directory on the ledger's own filesystem would be
    # snapshotted and restored together with the ledger, so it protects nothing.
    directory = private(tmp_path / "vol" / "ledger")
    sibling = private(tmp_path / "vol" / "ar")
    assert os.stat(sibling).st_dev == os.stat(directory).st_dev
    ledger = EngagementLedger(directory)
    monkeypatch.setenv(epochs.ANTI_ROLLBACK_ENV, str(sibling))
    database = (directory / DATABASE).read_bytes()
    with pytest.raises(EngagementHold, match="anti_rollback_same_volume"):
        enroll(ledger, "epoch-1")
    assert (directory / DATABASE).read_bytes() == database
    assert not list(sibling.rglob("*.json"))
    with pytest.raises(EngagementHold, match="anti_rollback_same_volume"):
        EngagementLedger(private(tmp_path / "vol" / "fresh"))  # every open holds
    assert epochs.ledger_marker_present(directory) is True  # custody fails closed too


def test_same_volume_check_is_applied_at_every_open_after_ingest(
    tmp_path: Path, anti_rollback_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    directory, epoch = engaged(tmp_path)
    # The same records, moved onto the ledger's own filesystem after the ingest.
    moved = tmp_path / "ar-on-ledger-volume"
    shutil.copytree(anti_rollback_directory, moved, symlinks=True)
    moved.chmod(0o700)
    assert os.stat(moved).st_dev == os.stat(directory).st_dev
    monkeypatch.setenv(epochs.ANTI_ROLLBACK_ENV, str(moved))
    with pytest.raises(EngagementHold, match="anti_rollback_same_volume"):
        EngagementLedger(directory)
    with pytest.raises(EngagementHold, match="anti_rollback_same_volume"):
        EngagementLedger(directory, existing_only=True).live_gate(
            ORG, epoch, "decisions", "lease-a"
        )
    (directory / DATABASE).unlink()
    with pytest.raises(CustodyError, match=HOLD):
        Closeout(directory, ORG).withdraw(**START)


def test_no_flag_can_relax_the_same_volume_check(
    tmp_path: Path, anti_rollback_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = Path(epochs.__file__).read_text()
    environ_reads = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Attribute) and node.attr == "environ"
    ]
    assert len(environ_reads) == 1  # OPENTOUR_ANTI_ROLLBACK_DIRECTORY, the location itself
    assert "os.environ.get(ANTI_ROLLBACK_ENV)" in source
    # The test location really is another filesystem: no seam stands in for production.
    assert os.stat(anti_rollback_directory).st_dev != os.stat(tmp_path).st_dev

    def unreadable(_path: str) -> int:
        raise OSError("generated device lookup failure")

    monkeypatch.setattr(epochs, "_device_of", unreadable)
    with pytest.raises(EngagementHold, match="anti_rollback_unavailable"):
        EngagementLedger(private(tmp_path / ORG))


def test_record_lock_and_layout_match_the_trust_state_store() -> None:
    from opentour_cell.pilot import signed_records

    # The default location, record directory and writer lock are the TrustStateStore's own,
    # so on the worker host both writers serialize on one lock file.
    assert epochs.ANTI_ROLLBACK_DIRECTORY == signed_records.ANTI_ROLLBACK_DIRECTORY
    assert epochs._RECORDS == signed_records.RECORDS_NAME
    assert epochs._RECORD_LOCK == signed_records.LOCK_NAME
    assert epochs.TRUST_STATE_FORMAT == signed_records.TRUST_STATE_FORMAT


def test_restored_pre_first_grant_snapshot_is_refused(tmp_path: Path) -> None:
    # F2, the reviewer's repro: an empty ledger of the same store, restored after a grant,
    # a budget reservation and an effect, would let the budget be spent twice.
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    empty = snapshot(directory)
    enroll(ledger, "epoch-1")
    ledger.activate(ORG, "epoch-1")
    assert ledger.reserve_budget(ORG, "epoch-1", "spend-1", 60) is True
    assert ledger.record_effect(ORG, "epoch-1", "decision", "d-1") is True
    restore(directory, empty)
    assert rows(directory) == []
    with pytest.raises(EngagementHold, match="anti_rollback_ledger_rewound"):
        EngagementLedger(directory)
    reopened = EngagementLedger(directory, existing_only=True)
    with pytest.raises(EngagementHold, match="anti_rollback_ledger_rewound"):
        enroll(reopened, "epoch-1")  # the same grant cannot be re-ingested over it
    with pytest.raises(EngagementHold, match="anti_rollback_ledger_rewound"):
        reopened.reserve_budget(ORG, "epoch-1", "spend-1", 60)
    assert rows(directory) == []


def test_first_grant_marker_reaches_sequence_two_after_a_lost_promotion(
    tmp_path: Path, anti_rollback_directory: Path
) -> None:
    directory, _ = engaged(tmp_path)
    path = marker_file(anti_rollback_directory, directory)
    stored = read_record(path)
    stored["sequence"] = "1"  # as if the post-commit promotion had been lost
    path.write_bytes(epochs._canonical(stored))
    lock = anti_rollback_directory / "pilot-trust-state.lock"
    lock.unlink()
    private(lock)  # the promotion cannot be written now
    with pytest.raises(EngagementHold, match="anti_rollback_unavailable"):
        EngagementLedger(directory, existing_only=True).organizations()
    assert read_record(path)["sequence"] == "1"
    lock.rmdir()
    assert EngagementLedger(directory, existing_only=True).organizations() == (ORG,)
    assert read_record(path)["sequence"] == "2"  # promoted by the next ledger operation


def test_location_owned_by_another_uid_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F3: a location (and its records) owned by any other uid is not this worker's.
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    database = (directory / DATABASE).read_bytes()
    real = os.geteuid()
    monkeypatch.setattr(epochs.os, "geteuid", lambda: real + 1)
    with pytest.raises(EngagementHold, match="anti_rollback_unavailable"):
        enroll(ledger, "epoch-1")
    with pytest.raises(EngagementHold, match="anti_rollback_unavailable"):
        EngagementLedger(private(tmp_path / "other"))
    monkeypatch.setattr(epochs.os, "geteuid", lambda: real)
    assert (directory / DATABASE).read_bytes() == database
    assert enroll(ledger, "epoch-1").state == "recorded"


def test_location_inside_the_ledger_directory_is_refused_even_on_its_own_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A separate filesystem mounted inside the ledger directory still travels with it.
    directory = private(tmp_path / ORG)
    ledger = EngagementLedger(directory)
    inside = private(directory / "antirollback-mount")
    monkeypatch.setattr(epochs, "_device_of", lambda path: -1 if path == str(inside) else 1)
    monkeypatch.setenv(epochs.ANTI_ROLLBACK_ENV, str(inside))
    with pytest.raises(EngagementHold, match="anti_rollback_inside_snapshot"):
        enroll(ledger, "epoch-1")
    assert rows(directory) == []
