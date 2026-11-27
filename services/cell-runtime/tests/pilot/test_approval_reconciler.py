"""Generated real local bridge and scheduled-entry lifecycle."""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from opentour_cell.pilot import approval_reconciler, zapier_approval
from opentour_cell.pilot.approval_reconciler import ApprovalReconciler
from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.zapier_approval import Json, canonical
from test_approval_reconciliation import rows, signed_at_clock
from test_zapier_approval import HOOK, Case, deliver, make_case, private


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return make_case(tmp_path)


def epoch(case: Case) -> str:
    return case.bridge.pending_summary()["installation_epoch"]


def scheduler(case: Case, installation_epoch: str, **options) -> ApprovalReconciler:
    bridge = zapier_approval.ApprovalBridge(
        case.directory, case.h.store.organization, case.bridge.config
    )
    settings = {"poll_interval": 4, "reminder_interval": 60, **options}
    return ApprovalReconciler(
        bridge, installation_epoch=installation_epoch, enabled=True, **settings
    )


def progress(case: Case) -> Json:
    with sqlite3.connect(case.directory / "zapier-reconciliation.sqlite3") as db:
        (document,) = db.execute("SELECT document FROM approval_schedule_progress").fetchone()
    return json.loads(document)


def first_pending(case: Case, *, ttl: int = 900) -> Json:
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(case.view, "ot_evidence", case.revision)
    wire = case.bridge.issue(**case.binding, ttl_seconds=ttl)
    deliver(case, wire)
    case.bridge.consume(signed_at_clock(case, wire, status="Pending"))
    return wire


def fresh_request(case: Case, *, ttl: int = 900) -> Json:
    view = case.h.launch()
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(view, "ot_evidence", case.revision)
    return case.bridge.issue(**case.binding, ttl_seconds=ttl)


class Interrupted(BaseException):
    """Represent interruption before the worker can write its next receipt."""


def test_normal_local_entry_and_disablement(
    case: Case, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "approval-reconciler",
        "--directory",
        str(case.directory),
        "--organization",
        case.h.store.organization,
        "--config",
        str(case.bridge.config),
        "--installation-epoch",
        "0" * 32,
    ]
    monkeypatch.setattr(sys, "argv", args)
    assert approval_reconciler.main() == 0
    assert json.loads(capsys.readouterr().out)["state"] == "disabled"
    assert not (case.directory / "zapier-reconciliation.sqlite3").exists()
    wire = first_pending(case)
    args[-1] = epoch(case)
    args.extend(["--enabled", "--poll-interval", "4"])
    assert approval_reconciler.main() == 0
    output = capsys.readouterr().out
    receipt = json.loads(output)
    assert receipt["state"] == "sweep_complete"
    assert receipt["coverage"]["acknowledged_requests"] == 1
    assert receipt["coverage"]["completed_this_tick"]
    assert all(value not in output for value in (case.key, case.policy["reviewer"], wire["nonce"]))
    assert len(rows(case, "outbox")) == 1


def test_real_issued_pages_preserve_snapshot_across_restart(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    first = first_pending(case, ttl=2)
    now += 3
    second = fresh_request(case, ttl=2)
    installation_epoch = epoch(case)
    assert first["request_id"] != second["request_id"]
    settings = {"page_size": 1, "max_pages": 1}
    one = scheduler(case, installation_epoch, **settings).tick()
    assert one["state"] == "page_limit"
    assert one["coverage"]["observed_requests"] == 1 and one["acknowledged"] == 1
    now += 3
    third = fresh_request(case)
    assert third["request_id"] not in {first["request_id"], second["request_id"]}
    two = scheduler(case, installation_epoch, **settings).tick()
    assert two["state"] == "sweep_complete"
    assert two["coverage"]["sweep_ref"] == one["coverage"]["sweep_ref"]
    assert two["coverage"]["observed_requests"] == two["coverage"]["acknowledged_requests"] == 2
    assert scheduler(case, installation_epoch, **settings).tick()["state"] == "cadence_held"
    now += 4
    for _ in range(2):
        assert scheduler(case, installation_epoch, **settings).tick()["state"] == "page_limit"
    next_sweep = scheduler(case, installation_epoch, **settings).tick()
    assert next_sweep["state"] == "sweep_complete"
    assert next_sweep["coverage"]["observed_requests"] == 3


def test_restart_after_page_staged_and_before_final_receipt(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = first_pending(case)
    installation_epoch = epoch(case)
    original = ApprovalReconciler._save

    def after_stage(self, journal, scope, prior, changes):
        saved = original(self, journal, scope, prior, changes)
        if changes.get("page_loaded") is True:
            raise Interrupted()
        return saved

    with monkeypatch.context() as local:
        local.setattr(ApprovalReconciler, "_save", after_stage)
        with pytest.raises(Interrupted):
            scheduler(case, installation_epoch).tick()
    before = progress(case)
    assert [entry["request_id"] for entry in before["pending"]] == [wire["request_id"]]
    assert rows(case, "outbox") == []

    def after_finish(self, journal, scope, prior, changes):
        saved = original(self, journal, scope, prior, changes)
        if changes.get("completed_at") is not None:
            raise Interrupted()
        return saved

    with monkeypatch.context() as local:
        local.setattr(ApprovalReconciler, "_save", after_finish)
        with pytest.raises(Interrupted):
            scheduler(case, installation_epoch).tick()
    stored = progress(case)
    assert stored["sweep_ref"] == before["sweep_ref"]
    assert stored["completed_at"] is not None
    assert stored["next_sweep_at"] == stored["completed_at"] + 4
    assert not stored["pending"] and not stored["page_loaded"] and stored["cursor"] is None
    assert scheduler(case, installation_epoch).tick()["state"] == "cadence_held"
    assert len(rows(case, "outbox")) == 1


@pytest.mark.parametrize("lost_response", [False, True])
@pytest.mark.parametrize("recovery_delay", [0, 61])
def test_completed_local_transition_retains_original_reference_on_missing_ack(
    case: Case, monkeypatch: pytest.MonkeyPatch, lost_response: bool, recovery_delay: int
) -> None:
    now = int(time.time())
    started = now
    monkeypatch.setattr(time, "time", lambda: now)
    wire = first_pending(case)
    installation_epoch = epoch(case)
    original = zapier_approval.ApprovalBridge._reconcile

    def after_core(self, journal, **arguments):
        result = original(self, journal, **arguments)
        assert result["state"] == "reconciled"
        if lost_response:
            raise TimeoutError("generated local response lost")
        raise Interrupted()

    with monkeypatch.context() as local:
        local.setattr(zapier_approval.ApprovalBridge, "_reconcile", after_core)
        if lost_response:
            assert scheduler(case, installation_epoch).tick()["state"] == "deferred"
        else:
            with pytest.raises(Interrupted):
                scheduler(case, installation_epoch).tick()
    stored = progress(case)
    assert stored["pending"][0]["request_id"] == wire["request_id"]
    if lost_response:
        assert stored["active"] is None
    else:
        assert stored["active"]["request_id"] == wire["request_id"]
        assert stored["active"]["action"] == stored["pending"][0]["action"]
    before = rows(case, "outbox")
    assert len(before) == 1
    now += recovery_delay
    resumed = scheduler(case, installation_epoch).tick()
    if lost_response and not recovery_delay:
        assert resumed["state"] == "deferred" and resumed["attempts"] == 0
        now += 4
        resumed = scheduler(case, installation_epoch).tick()
    assert resumed["state"] == "sweep_complete" and resumed["acknowledged"] == 1
    assert resumed["coverage"]["sweep_ref"] == stored["sweep_ref"]
    assert rows(case, "outbox") == before
    if not recovery_delay:
        now = started + 56
        assert scheduler(case, installation_epoch).tick()["state"] == "sweep_complete"
        assert rows(case, "outbox") == before
        now = started + 60
    else:
        now += 4
    next_sweep = scheduler(case, installation_epoch).tick()
    assert next_sweep["state"] == "sweep_complete"
    assert next_sweep["coverage"]["sweep_ref"] != stored["sweep_ref"]
    assert len(rows(case, "outbox")) == 2
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


def test_interruption_after_poll_but_before_disposition_is_not_a_completed_action(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = first_pending(case)
    installation_epoch = epoch(case)

    def interrupt_disposition(self, request_id, state, *args, **kwargs):
        assert request_id == wire["request_id"] and state == "pending"
        assert kwargs.get("schedule_action")
        raise Interrupted()

    with monkeypatch.context() as local:
        local.setattr(zapier_approval.ReconciliationJournal, "transition", interrupt_disposition)
        with pytest.raises(Interrupted):
            scheduler(case, installation_epoch).tick()
    staged = progress(case)
    assert staged["active"]["request_id"] == wire["request_id"]
    assert rows(case, "outbox") == []
    result = scheduler(case, installation_epoch).tick()
    assert result["state"] == "deferred" and result["attempts"] == 1
    assert result["acknowledged"] == 0
    assert progress(case)["pending"][0]["action"] == staged["active"]["action"]
    now += 4
    result = scheduler(case, installation_epoch).tick()
    assert result["state"] == "sweep_complete" and result["acknowledged"] == 1
    assert len(rows(case, "outbox")) == 1


def test_expiry_after_real_inspection_is_fenced_without_a_reminder(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = first_pending(case, ttl=2)
    installed = epoch(case)
    original = zapier_approval.ApprovalBridge._inspect
    now = wire["expires_at"] - 1

    def inspect_to_expiry(self, request, **kwargs):
        nonlocal now
        result = original(self, request, **kwargs)
        now = wire["expires_at"]
        return result

    with monkeypatch.context() as local:
        local.setattr(zapier_approval.ApprovalBridge, "_inspect", inspect_to_expiry)
        result = scheduler(case, installed).tick()
    assert result["state"] == "sweep_complete" and result["acknowledged"] == 1
    assert case.bridge.pending_summary()["counts"] == {"expired": 1}
    assert [row[2] for row in rows(case, "outbox")] == ["owner_action"]


def test_overlapping_ticks_run_one_reconciliation_and_reminder(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = first_pending(case)
    installation_epoch = epoch(case)
    start = Barrier(2)
    original = zapier_approval.ApprovalBridge._reconcile
    calls = []

    def traced(self, journal, **arguments):
        calls.append(arguments["request_id"])
        return original(self, journal, **arguments)

    monkeypatch.setattr(zapier_approval.ApprovalBridge, "_reconcile", traced)

    def tick():
        start.wait(timeout=10)
        return scheduler(case, installation_epoch).tick()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = pool.submit(tick), pool.submit(tick)
        outcomes = [first.result(timeout=20), second.result(timeout=20)]
    assert {result["state"] for result in outcomes} == {"sweep_complete", "cadence_held"}
    assert calls == [wire["request_id"]]
    assert len(rows(case, "outbox")) == 1


def test_deadline_under_existing_writer_precedes_inventory(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_pending(case)
    installation_epoch = epoch(case)
    now = time.monotonic()
    monkeypatch.setattr(time, "monotonic", lambda: now)
    original = zapier_approval.fcntl.flock

    def slow_flock(descriptor, op):
        nonlocal now
        original(descriptor, op)
        now += 2

    with monkeypatch.context() as local:
        local.setattr(zapier_approval.fcntl, "flock", slow_flock)

        def forbidden_inventory(*_args):
            pytest.fail("an expired lock waiter began inventory work")

        local.setattr(zapier_approval.ApprovalBridge, "_inventory", forbidden_inventory)
        result = scheduler(case, installation_epoch, tick_seconds=1).tick()
    assert result["state"] == "timeout" and result["attempts"] == 0
    assert rows(case, "outbox") == []


def test_invalid_cursor_and_changed_epoch_stop_before_per_request_action(case: Case) -> None:
    first_pending(case)
    installation_epoch = epoch(case)
    assert scheduler(case, installation_epoch).tick()["state"] == "sweep_complete"
    prior = progress(case)
    tampered = {**prior, "cursor": "tampered"}
    with sqlite3.connect(case.directory / "zapier-reconciliation.sqlite3") as db:
        db.execute("UPDATE approval_schedule_progress SET document=?", (canonical(tampered),))
    result = scheduler(case, installation_epoch).tick()
    assert result["state"] == "unavailable" and result["attempts"] == 0
    assert progress(case) == tampered
    assert case.bridge.prepare_cutover()["paused"]
    case.policy["zap_version"] = "version-2"
    private(case.bridge.config, case.policy)
    assert case.bridge.complete_cutover()["installation_epoch"] != installation_epoch
    result = scheduler(case, installation_epoch).tick()
    assert result["state"] == "binding_held" and result["attempts"] == 0
    assert progress(case) == tampered


def test_unknown_remote_outcome_keeps_hold_across_scheduled_restart(case: Case) -> None:
    wire = case.inspect()
    sends = []

    def lost(_hook, payload):
        sends.append(payload)
        raise TimeoutError("generated remote response lost")

    assert case.bridge.flush(wire["request_id"], HOOK, lost)["transport"] == "uncertain"
    installation_epoch = epoch(case)
    assert scheduler(case, installation_epoch).tick()["state"] == "sweep_complete"
    assert scheduler(case, installation_epoch).tick()["state"] == "cadence_held"
    observed = case.bridge.pending_summary()["items"][0]
    assert observed["state"] == "held" and observed["reason"] == "remote_outcome_unknown"
    assert observed["retries"] == 1
    assert len(sends) == 1
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


def test_budget_retains_page_and_policy_revocation_cannot_begin_another_action(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    first = first_pending(case, ttl=2)
    now += 3
    second = fresh_request(case)
    installation_epoch = epoch(case)
    limited = scheduler(case, installation_epoch, max_requests=1).tick()
    assert limited["state"] == "request_limit"
    assert limited["attempts"] == limited["acknowledged"] == 1
    stored = progress(case)
    assert [item["request_id"] for item in stored["pending"]] == [second["request_id"]]
    outbox = rows(case, "outbox")
    original = dict(case.policy)
    case.policy["enabled"] = False
    private(case.bridge.config, case.policy)
    revoked = scheduler(case, installation_epoch, max_requests=1).tick()
    assert revoked["state"] == "unavailable" and revoked["attempts"] == 0
    assert rows(case, "outbox") == outbox and progress(case) == stored
    with sqlite3.connect(case.directory / "zapier-reconciliation.sqlite3") as db:
        polls = db.execute("SELECT key FROM installation WHERE key LIKE 'last_poll:%'").fetchall()
    assert polls == [("last_poll:" + first["request_id"],)]
    private(case.bridge.config, original)
    assert scheduler(case, installation_epoch, max_requests=1).tick()["state"] == "sweep_complete"


def test_withdrawal_accounts_for_the_existing_ledger_with_active_schedule(case: Case) -> None:
    first_pending(case)
    installation_epoch = epoch(case)
    assert scheduler(case, installation_epoch).tick()["state"] == "sweep_complete"
    for path in case.directory.glob("zapier-approval-za-*.json"):
        path.unlink()
    assert not (case.directory / "zapier-decisions").exists()
    report = Closeout(case.directory, case.h.store.organization).withdraw(
        agreement="generated-terms",
        custodian="generated-custodian",
        support="generated-support",
        due="2040-04-01",
        trigger="withdrawal",
    )
    item = next(item for item in report["items"] if item["item"] == "plugin-decisions")
    assert item["disposition"] == "unresolved" and report["state"] == "open"
