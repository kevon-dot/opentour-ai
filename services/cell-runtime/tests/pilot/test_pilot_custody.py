"""Generated customer withdrawals through the private operator closeout entry points."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from opentour_cell.pilot.custody import Closeout, CustodyError
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_data import generated_dataset

START = {
    "agreement": "terms-a",
    "custodian": "custodian-a",
    "support": "support-a",
    "due": "2040-04-01",
    "trigger": "withdrawal",
}
ORG = "generated-custody-a"


def workspace(parent: Path, organization: str) -> Path:
    directory = parent / organization
    directory.mkdir(mode=0o700)
    return directory


def train(directory: Path, organization: str) -> tuple[LocalTrainingStore, str]:
    store = LocalTrainingStore(directory / "training.sqlite3", organization)
    data = generated_dataset(organization, "trainer")
    data["train"][2]["text"] += " " + organization
    store.register(data)
    proposal = store.propose(data["dataset_id"], request_id="generated-request")
    result = store.run(
        proposal["job_id"], reviewer="trainer", expected_revision=proposal["revision"]
    )
    assert result["status"] == "completed"
    return store, str(result["job_id"])


def items(report: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {v["item"]: v for v in report["items"]}


def record(c: Closeout, item: str, kind: str, disposition: str, **changes: str) -> dict[str, Any]:
    fields = {"owner": "audit-owner", "due": "2040-04-01", "evidence": "private-receipt", **changes}
    return c.record(item=item, kind=kind, disposition=disposition, **fields)


def legacy_journal(directory: Path, organization: str) -> None:
    """Prior SQLite contract with an accepted send, a pending send and an interrupted send."""
    path = directory / "slack-pilot.sqlite3"
    path.touch(mode=0o600)
    with closing(sqlite3.connect(path)) as db, db:
        db.executescript("""
            CREATE TABLE identity (id INTEGER PRIMARY KEY, binding TEXT, key BLOB);
            CREATE TABLE decisions (
                revision TEXT PRIMARY KEY,receipt TEXT UNIQUE,actor TEXT,disposition TEXT,
                decided INTEGER,exportable INTEGER,audience TEXT,attempts INTEGER DEFAULT 0,
                delivery TEXT DEFAULT 'pending');
        """)
        binding = json.dumps(
            [organization, "TDEMO", "ADEMO", "CDEMO", str(directory)], separators=(",", ":")
        )
        db.execute("INSERT INTO identity VALUES(1,?,?)", (binding, bytes(32)))
        for index, delivery in enumerate(("pending", "sending", "accepted")):
            db.execute(
                "INSERT INTO decisions VALUES(?,?,?,'approved',1,1,?,1,?)",
                (f"r{index}", f"receipt{index}", "UREVIEW", "URECIP", delivery),
            )


def test_early_withdrawal_is_durable_and_safe_on_replay(tmp_path: Path) -> None:
    directory = workspace(tmp_path, ORG)
    c = Closeout(directory, ORG)
    with pytest.raises(CustodyError, match="closeout_not_open"):
        c.status()
    original = c.withdraw(**START)
    assert original["reconciled"] and original["state"] == "open"
    assert "export_disposition" not in original
    assert set(items(original)) == {"inventory-review", "support-handoff"}
    assert c.withdraw(**START) == original
    after_restart = Closeout(directory, ORG)
    assert after_restart.status() == original
    domain = LocalTrainingStore(directory / "training.sqlite3", ORG)
    assert not domain.engagement_active()
    with pytest.raises(TrainingError):
        domain.register(generated_dataset(ORG, "trainer"))
    with pytest.raises(CustodyError, match="scope_conflict"):
        after_restart.withdraw(**(START | {"agreement": "different-terms"}))
    assert after_restart.status()["agreement"] == "terms-a"
    assert (directory / "custody.sqlite3").stat().st_mode & 0o077 == 0
    assert not (directory / "slack-pilot.sqlite3").exists()


def test_org_bound_purge_keeps_other_customer_with_same_case_ids_intact(tmp_path: Path) -> None:
    a_dir = workspace(tmp_path, ORG)
    b_name = "generated-custody-b"
    b_dir = workspace(tmp_path, b_name)
    a, a_job = train(a_dir, ORG)
    b, b_job = train(b_dir, b_name)
    b_artifact = b.artifact(b_job)
    c = Closeout(a_dir, ORG)
    result = c.withdraw(**START)
    observed = items(result)
    assert [
        observed["local-" + k]["observed"] for k in ("inputs", "heldout", "models", "outputs")
    ] == [6, 4, 1, 1]
    assert "generated-custody-b" not in json.dumps(result)
    with pytest.raises(TrainingError):
        a.status(a_job)
    with pytest.raises(CustodyError, match="scope|organization"):
        Closeout(a_dir, b_name).status()
    with pytest.raises(CustodyError, match="live_data_still_present"):
        record(c, "local-inputs", "local", "confirmed_deleted")
    auth = {
        "engagement": result["engagement"],
        "agreement": "terms-a",
        "custodian": "custodian-a",
        "evidence": "purge-authorization",
    }
    for key in ("engagement", "agreement", "custodian"):
        with pytest.raises(CustodyError, match="authority_mismatch"):
            c.purge_local(**(auth | {key: "other"}))
    assert a.custody_inventory()["inputs"] == 6
    purged = c.purge_local(**auth)
    assert a.custody_inventory() == {"inputs": 0, "heldout": 0, "models": 0, "outputs": 0}
    assert items(purged)["local-inputs"]["disposition"] == "live_rows_removed"
    assert c.purge_local(**auth) == purged
    assert not a.engagement_active()
    assert b.engagement_active() and b.custody_inventory()["inputs"] == 6
    assert b.artifact(b_job) == b_artifact
    with pytest.raises(TrainingError):
        a.register(generated_dataset(ORG, "trainer"))


def test_failed_reconciliation_keeps_hold_and_blocks_purge_until_retry(tmp_path: Path) -> None:
    directory = workspace(tmp_path, ORG)
    store, _ = train(directory, ORG)
    c = Closeout(directory, ORG)
    with patch(
        "opentour_cell.pilot.custody.withdraw_for_custody", side_effect=sqlite3.OperationalError
    ):
        with pytest.raises(sqlite3.OperationalError):
            c.withdraw(**START)
    assert not store.engagement_active()
    before = c.status()
    assert before["state"] == "open" and not before["reconciled"]
    with pytest.raises(CustodyError, match="reconcile_before_purge"):
        c.purge_local(
            engagement=before["engagement"],
            agreement="terms-a",
            custodian="custodian-a",
            evidence="authorized",
        )
    assert store.custody_inventory()["models"] == 1
    resume = Closeout(directory, ORG).reconcile()
    assert resume["engagement"] == before["engagement"] and resume["reconciled"]
    assert items(resume)["local-models"]["observed"] == 1


def test_legacy_inflight_delivered_and_pending_copies_remain_accounted_for(tmp_path: Path) -> None:
    directory = workspace(tmp_path, ORG)
    legacy_journal(directory, ORG)
    c = Closeout(directory, ORG)
    rep = c.withdraw(**START)
    delivery = rep["delivery_inventory"]
    assert delivery["zapier_accepted"] == 1 and delivery["zapier_uncertain"] == 1
    assert delivery["zapier_suppressed"] == 1 and delivery["decisions"] == 3
    assert items(rep)["zapier-copies"]["observed"] == 2
    assert items(rep)["historical-destinations"]["disposition"] == "unresolved"
    assert items(rep)["local-handoff-journal"]["disposition"] == "unresolved"
    assert c.reconcile() == rep
    with pytest.raises(CustodyError, match="journal_still_present"):
        record(c, "local-handoff-journal", "receipt", "confirmed_deleted")
    follow = record(
        c, "zapier-copies", "destination", "recipient_followup", owner="recipient-owner"
    )
    assert items(follow)["zapier-copies"]["owner"] == "recipient-owner"
    with pytest.raises(CustodyError, match="obligations_open"):
        c.finish(custodian="custodian-a", evidence="not-yet")


def test_each_case_requires_support_actual_destination_evidence_and_schedule(
    tmp_path: Path,
) -> None:
    c = Closeout(workspace(tmp_path, ORG), ORG)
    c.withdraw(**START)
    with pytest.raises(CustodyError, match="custodian_evidence_required"):
        c.record(
            item="old-backup",
            kind="backup",
            disposition="confirmed_deleted",
            owner="custodian",
            due="2040-04-01",
        )
    with pytest.raises(CustodyError, match="expiry_not_due"):
        record(c, "old-backup", "backup", "confirmed_expiry")
    record(c, "old-backup", "backup", "agreed_retention", owner="backup-custodian")
    record(c, "invitation-queue", "grant", "unresolved", owner="workspace-admin")
    record(c, "inventory-review", "inventory", "inventory_verified")
    with pytest.raises(CustodyError, match="obligations_open"):
        c.finish(custodian="custodian-a", evidence="not-yet")
    record(c, "invitation-queue", "grant", "confirmed_deleted", evidence="invites-cancelled")
    record(
        c, "old-backup", "backup", "confirmed_expiry", due="2020-04-01", evidence="verified-expiry"
    )
    with pytest.raises(CustodyError, match="obligations_open"):
        c.finish(custodian="custodian-a", evidence="not-yet")
    record(c, "support-handoff", "support", "handoff_confirmed")
    done = c.finish(custodian="custodian-a", evidence="closeout-confirmation")
    assert done["state"] == "closed" and done["outstanding"] == 0
    assert done["export_disposition"] == "zero_exports"
    assert "accepted" not in done
    assert c.finish(custodian="custodian-a", evidence="closeout-confirmation") == done
    reopened = record(
        c, "customer-drive", "destination", "recipient_followup", owner="customer-custodian"
    )
    assert reopened["state"] == "open" and reopened["outstanding"] == 1
    assert not reopened["closure_evidence"]
    assert "export_disposition" not in reopened


def test_concurrent_withdrawal_and_cli_read_use_same_engagement(tmp_path: Path) -> None:
    directory = workspace(tmp_path, ORG)
    c = Closeout(directory, ORG)
    with ThreadPoolExecutor(2) as pool:
        reported = list(pool.map(lambda _: Closeout(directory, ORG).withdraw(**START), range(2)))
    assert {r["engagement"] for r in reported} == {c.status()["engagement"]}
    args = [
        sys.executable,
        "-m",
        "opentour_cell.pilot.custody",
        "--directory",
        str(directory),
        "--org",
        ORG,
    ]
    ok = subprocess.run([*args, "status"], capture_output=True, text=True, check=True)
    assert json.loads(ok.stdout)["engagement"] == reported[0]["engagement"]
    fail = subprocess.run(
        [*args, "finish", "--custodian", "custodian-a", "--evidence", "premature"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert fail.returncode == 2 and fail.stdout == "" and "closeout_obligations_open" in fail.stderr
    assert os.stat(directory / "custody.sqlite3").st_mode & 0o077 == 0


def test_awaiting_job_blocked_and_other_org_identical_receipts_preserved(tmp_path: Path) -> None:
    a_dir = workspace(tmp_path, ORG)
    b_name = "generated-custody-b"
    b_dir = workspace(tmp_path, b_name)
    legacy_journal(a_dir, ORG)
    legacy_journal(b_dir, b_name)
    a = LocalTrainingStore(a_dir / "training.sqlite3", ORG)
    a.register(generated_dataset(ORG, "trainer"))
    pending = a.propose("generated-intake", request_id="awaiting-confirmation")
    b = LocalTrainingStore(b_dir / "training.sqlite3", b_name)
    b.register(generated_dataset(b_name, "trainer"))
    other = b.propose("generated-intake", request_id="awaiting-confirmation")
    before = (b_dir / "slack-pilot.sqlite3").read_bytes()
    closeout = Closeout(a_dir, ORG).withdraw(**START)
    assert "local-outputs" not in items(closeout)
    with pytest.raises(TrainingError):
        a.run(pending["job_id"], reviewer="trainer", expected_revision=pending["revision"])
    assert a.custody_inventory()["outputs"] == 0
    assert (b_dir / "slack-pilot.sqlite3").read_bytes() == before
    assert (
        b.run(other["job_id"], reviewer="trainer", expected_revision=other["revision"])["status"]
        == "completed"
    )


def test_receipt_only_finish_requires_settled_obligations_and_discards_working_refs(
    tmp_path: Path,
) -> None:
    root = workspace(tmp_path, ORG)
    c = Closeout(root, ORG)
    before = c.withdraw(**START)
    with pytest.raises(CustodyError, match="obligations_open"):
        c.finish(custodian="custodian-a", evidence="closure-receipt", receipt_only=True)
    record(c, "inventory-review", "inventory", "inventory_verified", evidence="working-inventory")
    record(c, "support-handoff", "support", "handoff_confirmed", evidence="working-handoff")
    result = c.finish(custodian="custodian-a", evidence="closure-receipt", receipt_only=True)
    assert result == {
        "organization": ORG,
        "engagement": before["engagement"],
        "state": "closed",
        "retained": "receipt_only",
        "closure_evidence": "closure-receipt",
        "export_disposition": "zero_exports",
    }
    assert c.status() == c.reconcile() == c.withdraw(**START) == result
    assert (
        c.finish(custodian="custodian-a", evidence="closure-receipt", receipt_only=True) == result
    )
    with pytest.raises(CustodyError, match="closure_receipt_conflict"):
        c.finish(custodian="custodian-a", evidence="different")
    with closing(sqlite3.connect(c.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
        fields = db.execute(
            "SELECT organization,agreement,custodian,support,due,revision,trigger,"
            "transport FROM closeout"
        ).fetchone()
        assert fields[:-1] == (ORG, "", "", "", "", None, "")
        assert json.loads(fields[-1]) == {"export_disposition": "zero_exports"}
    assert not LocalTrainingStore(root / "training.sqlite3", ORG).engagement_active()


def test_late_acknowledgement_requires_fresh_destination_reconciliation(tmp_path: Path) -> None:
    directory = workspace(tmp_path, ORG)
    legacy_journal(directory, ORG)
    closeout = Closeout(directory, ORG)
    closeout.withdraw(**START)
    record(closeout, "zapier-copies", "destination", "custody_transferred")
    with closing(sqlite3.connect(directory / "slack-pilot.sqlite3")) as journal, journal:
        # A provider receipt arrives for the earlier in-flight claim after withdrawal.
        journal.execute(
            "UPDATE decisions SET delivery='accepted' WHERE receipt='receipt1'"
            " AND delivery='uncertain'"
        )
    state = closeout.reconcile()
    assert state["delivery_inventory"]["zapier_accepted"] == 2
    assert state["delivery_inventory"]["zapier_uncertain"] == 0
    assert items(state)["zapier-copies"]["disposition"] == "unresolved"
    assert items(state)["zapier-copies"]["evidence"] is None


def test_known_destination_history_cannot_be_projected_as_zero_exports(tmp_path: Path) -> None:
    directory = workspace(tmp_path, ORG)
    legacy_journal(directory, ORG)
    closeout = Closeout(directory, ORG)
    closeout.withdraw(**START)
    with closing(sqlite3.connect(directory / "slack-pilot.sqlite3")) as journal:
        assert journal.execute(
            "SELECT delivery,COUNT(*) FROM decisions GROUP BY delivery ORDER BY delivery"
        ).fetchall() == [("accepted", 1), ("legacy_hold", 1), ("uncertain", 1)]
    record(closeout, "inventory-review", "inventory", "inventory_verified")
    record(closeout, "support-handoff", "support", "handoff_confirmed")
    record(closeout, "zapier-copies", "destination", "agreed_retention", owner="archive-owner")
    held = closeout.status()
    assert held["delivery_inventory"]["zapier_uncertain"] == 1
    assert items(held)["zapier-copies"]["disposition"] == "agreed_retention"
    with pytest.raises(CustodyError, match="obligations_open"):
        closeout.finish(custodian="custodian-a", evidence="premature")
    record(
        closeout,
        "zapier-copies",
        "destination",
        "custody_transferred",
        evidence="independent-destination-custody",
    )
    record(closeout, "historical-destinations", "destination", "custody_transferred")
    record(closeout, "local-handoff-journal", "receipt", "custody_transferred")
    done = closeout.finish(custodian="custodian-a", evidence="destination-closeout")
    assert done["state"] == "closed"
    assert done["delivery_inventory"]["zapier_uncertain"] == 1
    assert "export_disposition" not in done


def test_restore_of_same_training_store_reasserts_hold_and_reopens_removed_rows(
    tmp_path: Path,
) -> None:
    directory = workspace(tmp_path, ORG)
    domain, _ = train(directory, ORG)
    backup = tmp_path / "operator-owned-backup.sqlite3"
    with closing(sqlite3.connect(domain.path)) as live, closing(sqlite3.connect(backup)) as copy:
        live.backup(copy)
    c = Closeout(directory, ORG)
    begin = c.withdraw(**START)
    c.purge_local(
        engagement=begin["engagement"],
        agreement="terms-a",
        custodian="custodian-a",
        evidence="approved-training-row-disposal",
    )
    record(c, "inventory-review", "inventory", "inventory_verified")
    record(c, "support-handoff", "support", "handoff_confirmed")
    assert c.finish(custodian="custodian-a", evidence="closure")["state"] == "closed"
    with closing(sqlite3.connect(backup)) as copy, closing(sqlite3.connect(domain.path)) as live:
        copy.backup(live)
    # Normal data entry points must fail closed before a custodian runs reconciliation.
    assert not domain.engagement_active()
    with pytest.raises(TrainingError):
        domain.catalog()
    ordinary = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.training",
            "--db",
            str(domain.path),
            "--org",
            ORG,
            "catalog",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert ordinary.returncode == 2 and not ordinary.stdout
    reopened = c.reconcile()
    assert not domain.engagement_active()
    assert reopened["state"] == "open" and reopened["outstanding"] == 4
    assert items(reopened)["local-models"]["disposition"] == "unresolved"
