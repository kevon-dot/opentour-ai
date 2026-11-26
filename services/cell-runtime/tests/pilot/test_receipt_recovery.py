"""Operator discovery is a private read, including after a stopped or withdrawn pilot."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

import pytest
from opentour_cell.pilot import receipt_recovery, slack_adapter, slack_views
from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_data import generated_evaluation_dataset
from test_slack_adapter import ACTOR, HOOK, Harness

TERMS = {
    "agreement": "generated-terms",
    "custodian": "generated-custodian",
    "support": "generated-support",
    "due": "2040-04-01",
    "trigger": "withdrawal",
}


def settings(h: Harness) -> slack_adapter.Settings:
    return slack_adapter.Settings.load(h.path)


def cli(h: Harness, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", "-B", "-m", "opentour_cell.pilot.slack_adapter", str(h.path), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )


def approve_evaluation(h: Harness, name: str) -> str:
    dataset = generated_evaluation_dataset(h.config["organization"])
    dataset["dataset_id"] = name
    h.store.register(dataset)
    h.config["users"][ACTOR]["datasets"].append(name)
    h.save()
    view = h.launch(prompt="data")
    h.action(view, "ot_dataset_open", name)
    revision = next(
        b["value"]
        for row in h.slack.screens[view]["blocks"]
        for b in row.get("elements", [])
        if b.get("action_id") == "ot_evidence"
    )
    h.action(view, "ot_evidence", revision)
    h.action(view, "ot_home", "home")
    h.action(view, "ot_approve", revision)
    return str(h.app.journal.decision(revision)["receipt"])


def hashes(directory: Path) -> dict[str, str]:
    return {
        f.name: hashlib.sha256(f.read_bytes()).hexdigest()
        for f in directory.iterdir()
        if f.is_file()
    }


def test_uncertain_fresh_operator_after_withdrawal_and_verified_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    calls = []

    def timeout(*args: object, **kwargs: object) -> None:
        calls.append(1)
        raise TimeoutError

    monkeypatch.setattr(http.client, "HTTPSConnection", timeout)
    assert h.flush() == h.flush() == 0 and len(calls) == 1
    closeout = Closeout(h.path.parent, "pilot")
    closeout.withdraw(**TERMS)
    before = hashes(h.path.parent)
    found = cli(h, "--unresolved-receipts")
    assert found.returncode == 0, found.stderr
    page = json.loads(found.stdout)
    assert len(page["receipts"]) == 1 and page["next_cursor"] is None
    item = page["receipts"][0]
    assert item == {
        "receipt": receipt,
        "state": "uncertain",
        "attempted_at": item["attempted_at"],
        "destination": slack_adapter.hook_fingerprint(HOOK),
    }
    assert isinstance(item["attempted_at"], int)
    assert json.loads(cli(h, "--receipt", receipt).stdout) == item
    assert hashes(h.path.parent) == before
    assert cli(h, "--confirm-zapier", receipt, "--downstream-ref", "generated-zap").returncode == 0
    assert json.loads(cli(h, "--unresolved-receipts").stdout)["receipts"] == []
    assert json.loads(cli(h, "--receipt", receipt).stdout)["state"] == "downstream_confirmed"
    assert len(calls) == 1


def test_stale_approval_after_custody_restart_is_fenced_for_only_its_tenant(tmp_path: Path) -> None:
    first = Harness(tmp_path / "first")
    other = Harness(tmp_path / "second", organization="second")
    view, job, revision = first.completed()
    first.action(view, "ot_evidence", revision)
    first.action(view, "ot_home", "home")
    old_click = first.payload(view)
    old_click.update(type="block_actions", actions=[{"action_id": "ot_approve", "value": revision}])
    # This worker and the second organization use the same generated workspace and dataset IDs.
    assert settings(first).team == settings(other).team
    assert first.config["users"] == other.config["users"]

    root = first.path.parent
    before = first.store.custody_inventory()
    state = Closeout(root, "pilot").withdraw(**TERMS)
    assert state["delivery_inventory"]["decisions"] == 0
    first.store = LocalTrainingStore(root / "training.sqlite3", "pilot")
    first.app = slack_adapter.Workbench(
        lambda: settings(first), first.slack, first.store, slack_views, submit=lambda call: call()
    )
    first.send("interactive", old_click)
    assert first.app.journal.decision(revision) == {}
    assert first.store.custody_inventory() == before
    with pytest.raises(TrainingError):
        first.store.status(job)
    with patch("http.client.HTTPSConnection") as transport:
        assert first.flush() == 0
        transport.assert_not_called()
    assert Closeout(root, "pilot").reconcile()["delivery_inventory"]["decisions"] == 0

    _, other_receipt = other.approve()
    assert other.store.engagement_active()
    assert other.app.journal.receipt(other_receipt)["decision"]["delivery"] == "pending"


def test_active_pending_and_inflight_reads_never_change_the_journal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    journal = h.app.journal
    before = hashes(h.path.parent)
    item = receipt_recovery.unresolved_receipts(settings(h))["receipts"][0]
    assert item["attempted_at"] is None and item["state"] == "pending"
    assert receipt_recovery.inspect_receipt(settings(h), receipt) == journal.receipt(receipt)
    assert hashes(h.path.parent) == before

    with journal.db() as db:
        db.execute("UPDATE decisions SET delivery='sending',attempts=1 WHERE receipt=?", (receipt,))
        db.execute("UPDATE authorizations SET transmitted=17 WHERE receipt=?", (receipt,))
    before = hashes(h.path.parent)
    assert json.loads(cli(h, "--unresolved-receipts").stdout)["receipts"][0]["state"] == "sending"
    assert json.loads(cli(h, "--receipt", receipt).stdout)["decision"]["delivery"] == "sending"
    assert hashes(h.path.parent) == before


def test_pagination_stable_when_new_arrivals_or_an_item_is_confirmed(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    decisions = [approve_evaluation(h, f"sample-{idx}") for idx in range(4)]
    with h.app.journal.db() as db:
        db.execute("UPDATE decisions SET delivery='accepted',attempts=1")
        db.execute("UPDATE authorizations SET transmitted=31")
    cfg = settings(h)
    initial = receipt_recovery.unresolved_receipts(cfg, limit=1)
    assert initial["receipts"][0]["receipt"] == decisions[0]
    cursor = initial["next_cursor"]
    assert cursor and receipt_recovery.unresolved_receipts(cfg, limit=1) == initial
    h.app.journal.confirm_delivery(decisions[1], "seen-by-owner")
    newest = approve_evaluation(h, "sample-new")
    next_page = receipt_recovery.unresolved_receipts(cfg, limit=2, cursor=cursor)
    assert [row["receipt"] for row in next_page["receipts"]] == decisions[2:]
    assert next_page["next_cursor"] is None
    assert newest in [
        row["receipt"] for row in receipt_recovery.unresolved_receipts(cfg)["receipts"]
    ]
    for bad in ("junk", cursor + "0", cursor.replace("v1.", "v2.")):
        with pytest.raises(slack_adapter.Refused):
            receipt_recovery.unresolved_receipts(cfg, cursor=bad)
    for bad_limit in (0, -1, 101, True):
        with pytest.raises(slack_adapter.Refused):
            receipt_recovery.unresolved_receipts(cfg, limit=bad_limit)
    assert cli(h, "--check-config", "--limit", "2").returncode != 0


def test_foreign_scope_destination_cursor_and_colliding_dataset_keys(tmp_path: Path) -> None:
    one = Harness(tmp_path / "one")
    two = Harness(tmp_path / "two", organization="second")
    first = approve_evaluation(one, "same-dataset")
    second = approve_evaluation(two, "same-dataset")
    extra = approve_evaluation(one, "another-dataset")
    result = receipt_recovery.unresolved_receipts(settings(one), limit=1)
    cursor = result["next_cursor"]
    assert result["receipts"][0]["receipt"] == first and cursor
    assert [
        entry["receipt"]
        for entry in receipt_recovery.unresolved_receipts(settings(two))["receipts"]
    ] == [second]
    with pytest.raises(slack_adapter.Refused):
        receipt_recovery.unresolved_receipts(settings(two), cursor=cursor)
    with pytest.raises(slack_adapter.Refused):
        receipt_recovery.inspect_receipt(settings(two), first)
    one.config["zapier_destination_sha256"] = "a" * 64
    one.save()
    assert receipt_recovery.unresolved_receipts(settings(one))["receipts"] == []
    with pytest.raises(slack_adapter.Refused):
        receipt_recovery.unresolved_receipts(settings(one), cursor=cursor)
    with pytest.raises(slack_adapter.Refused):
        receipt_recovery.inspect_receipt(settings(one), extra)
    one.config["organization"] = "other"
    one.save()
    with pytest.raises(slack_adapter.Refused):
        receipt_recovery.unresolved_receipts(settings(one))


def test_active_journal_restored_next_to_custody_respects_hold_and_minimization(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    base = h.path.parent
    journal = base / "slack-pilot.sqlite3"
    training = base / "training.sqlite3"
    shutil.copyfile(journal, tmp_path / "saved-journal")
    shutil.copyfile(training, tmp_path / "saved-domain")
    closeout = Closeout(base, "pilot")
    state = closeout.withdraw(**TERMS)
    shutil.copyfile(tmp_path / "saved-journal", journal)
    shutil.copyfile(tmp_path / "saved-domain", training)
    # The old pending row was never submitted and must not become actionable after withdrawal.
    before = hashes(base)
    assert receipt_recovery.unresolved_receipts(settings(h))["receipts"] == []
    assert hashes(base) == before
    closeout.reconcile()
    closeout.purge_local(
        engagement=state["engagement"],
        agreement=TERMS["agreement"],
        custodian=TERMS["custodian"],
        evidence="generated-purge",
    )
    for item in closeout.status()["items"]:
        if item["disposition"] == "live_rows_removed":
            continue
        kind, key = item["kind"], item["item"]
        disposition = {"inventory": "inventory_verified", "support": "handoff_confirmed"}.get(
            kind, "custody_transferred"
        )
        closeout.record(
            item=key,
            kind=kind,
            owner="new-owner",
            due=TERMS["due"],
            disposition=disposition,
            evidence="verified-private-transfer",
        )
    final = closeout.finish(
        custodian=TERMS["custodian"], evidence="closure-receipt", receipt_only=True
    )
    shutil.copyfile(tmp_path / "saved-journal", journal)
    before = hashes(base)
    assert receipt_recovery.unresolved_receipts(settings(h)) == final
    assert receipt_recovery.inspect_receipt(settings(h), receipt) == final
    assert cli(h, "--confirm-zapier", receipt, "--downstream-ref", "too-late").returncode != 0
    assert hashes(base) == before and final["retained"] == "receipt_only"


def test_unbound_legacy_history_remains_unknown_and_unchanged(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    journal = h.app.journal
    with journal.db() as db:
        db.execute(
            "INSERT INTO decisions(revision,receipt,actor,disposition,decided,exportable,"
            "audience,attempts,delivery) VALUES('old','legacy-opaque','U','approved',"
            "3,1,'U',1,'accepted')"
        )
        db.execute("DROP TABLE authorizations")
    before = hashes(h.path.parent)
    assert receipt_recovery.unresolved_receipts(settings(h)) == {
        "receipts": [],
        "next_cursor": None,
        "legacy_scope": "unknown",
    }
    assert cli(h, "--receipt", "legacy-opaque").returncode != 0
    assert hashes(h.path.parent) == before


def test_empty_absent_and_malformed_stores_fail_safely(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    original = h.path.parent / "slack-pilot.sqlite3"
    original.unlink()
    before = hashes(h.path.parent)
    assert receipt_recovery.unresolved_receipts(settings(h))["receipts"] == []
    assert hashes(h.path.parent) == before
    os.symlink(tmp_path / "missing", original)
    assert cli(h, "--unresolved-receipts").returncode != 0
    original.unlink()
    (h.path.parent / "custody.sqlite3-wal").write_bytes(b"partial")
    assert cli(h, "--unresolved-receipts").returncode != 0


def test_damaged_scope_seal_returns_no_opaque_record(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    with closing(sqlite3.connect(h.app.journal.path)) as db, db:
        db.execute("UPDATE integrity SET seal='invalid' WHERE kind='decision' AND id=?", (receipt,))
    proc = cli(h, "--unresolved-receipts")
    assert proc.returncode != 0 and receipt not in proc.stdout and not proc.stdout


def test_closed_wal_reads_cannot_create_bookkeeping_files(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    journal = h.app.journal.path
    with closing(sqlite3.connect(journal)) as db:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    before = hashes(h.path.parent)
    assert cli(h, "--unresolved-receipts").returncode != 0
    assert hashes(h.path.parent) == before
    with closing(sqlite3.connect(journal)) as worker:
        worker.execute("SELECT count(*) FROM decisions").fetchone()
        assert (
            receipt_recovery.unresolved_receipts(settings(h))["receipts"][0]["receipt"] == receipt
        )


def test_confirm_rejects_changed_destination_and_callback_never_implies_completion(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    with h.app.journal.db() as db:
        db.execute("UPDATE decisions SET delivery='failed',attempts=1")
        db.execute("UPDATE authorizations SET transmitted=43")
    current = receipt_recovery.unresolved_receipts(settings(h))["receipts"][0]
    assert current["state"] == "failed" and current["attempted_at"] == 43
    h.config["zapier_destination_sha256"] = "b" * 64
    h.save()
    before = hashes(h.path.parent)
    assert (
        cli(h, "--confirm-zapier", receipt, "--downstream-ref", "wrong-destination").returncode != 0
    )
    assert hashes(h.path.parent) == before
    h.config["zapier_destination_sha256"] = slack_adapter.hook_fingerprint(HOOK)
    h.save()
    assert (
        cli(h, "--confirm-zapier", receipt, "--downstream-ref", "verified-externally").returncode
        == 0
    )
    assert receipt_recovery.unresolved_receipts(settings(h))["receipts"] == []


def test_concurrent_first_withdrawal_discards_inflight_owner_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    h.approve()
    custody = Closeout(h.path.parent, "pilot")
    selected = threading.Event()
    proceed = threading.Event()
    original = receipt_recovery._JournalSnapshot._seal

    def pause(
        self: receipt_recovery._JournalSnapshot,
        db: sqlite3.Connection,
        kind: str,
        key: str,
        *,
        create: bool = False,
    ) -> None:
        selected.set()
        assert proceed.wait(4)
        original(self, db, kind, key, create=create)

    monkeypatch.setattr(receipt_recovery._JournalSnapshot, "_seal", pause)
    with ThreadPoolExecutor(max_workers=2) as workers:
        reader = workers.submit(receipt_recovery.unresolved_receipts, settings(h))
        assert selected.wait(4)
        writer = workers.submit(custody.withdraw, **TERMS)
        until = time.monotonic() + 3
        while not custody.path.exists() and time.monotonic() < until:
            time.sleep(0.005)
        assert custody.path.exists()
        proceed.set()
        with pytest.raises(slack_adapter.Refused):
            reader.result(timeout=4)
        assert writer.result(timeout=4)["state"] == "open"
    assert receipt_recovery.unresolved_receipts(settings(h))["receipts"] == []
