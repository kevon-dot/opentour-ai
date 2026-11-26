"""Local generated recovery; no installed Schedule, Slack, or provider qualification."""

from __future__ import annotations

import json
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import patch

import pytest
from opentour_cell.pilot import zapier_approval
from opentour_cell.pilot.custody import Closeout
from test_zapier_approval import HOOK, Case, deliver, make_case, private


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return make_case(tmp_path)


def restart(case: Case) -> None:
    case.bridge = zapier_approval.ApprovalBridge(
        case.directory, case.h.store.organization, case.bridge.config
    )


def rows(case: Case, table: str) -> list[tuple]:
    assert table in {"requests", "outbox"}
    with sqlite3.connect(case.directory / "zapier-reconciliation.sqlite3") as db:
        return db.execute("SELECT * FROM " + table).fetchall()


def test_normal_entry_records_epoch_and_reminder_outbox_survives_claim_restart(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = case.inspect()
    deliver(case, wire)
    before = case.bridge.pending_summary()
    assert before["counts"] == {"pending": 1}
    assert before["items"][0]["request_id"] == wire["request_id"]
    result = case.bridge.reconcile(poll_interval=1, reminder_interval=60)
    assert result["state"] == "reconciled"
    assert case.bridge.reconcile(poll_interval=1)["state"] == "throttled"
    now += 1
    case.bridge.reconcile(poll_interval=1, reminder_interval=60)
    assert len(rows(case, "outbox")) == 1
    claim = case.bridge.claim_notification()
    assert claim and claim["kind"] == "reminder"
    assert set(claim) == {"notification_ref", "request_id", "kind", "tenant_ref"}
    restart(case)
    assert case.bridge.pending_summary()["installation_epoch"] == before["installation_epoch"]
    assert case.bridge.claim_notification() is None  # Lost send response is not a retry.
    confirmed = case.bridge.confirm_notification(claim["notification_ref"])
    assert case.bridge.confirm_notification(claim["notification_ref"]) == confirmed
    now += 58
    case.bridge.reconcile(poll_interval=1, reminder_interval=60)
    assert len(rows(case, "outbox")) == 1
    now += 1
    case.bridge.reconcile(poll_interval=1, reminder_interval=60)
    assert len(rows(case, "outbox")) == 2
    assert case.bridge.claim_notification()["notification_ref"] != claim["notification_ref"]
    assert not list(case.directory.glob("managed-outputs/*/handoff"))


def test_unknown_provider_response_is_held_without_resend_or_cutover(case: Case) -> None:
    wire = case.inspect()
    calls = []

    def lost(_hook, payload):
        calls.append(payload)
        raise TimeoutError("generated lost response")

    assert case.bridge.flush(wire["request_id"], HOOK, lost)["transport"] == "uncertain"
    restart(case)
    case.bridge.reconcile(poll_interval=1)
    state = case.bridge.prepare_cutover()
    assert state["paused"] and state["counts"] == {"held": 1}
    assert state["items"][0]["reason"] == "remote_outcome_unknown"
    with pytest.raises(ValueError, match="approval_dispatch_paused"):
        case.bridge.flush(wire["request_id"], HOOK, lost)
    case.policy["zap_version"] = "version-2"
    private(case.bridge.config, case.policy)
    with pytest.raises(ValueError, match="remote_reconciliation_required"):
        case.bridge.complete_cutover()
    assert len(calls) == 1
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


def test_expiry_at_clock_boundary_invalidates_nonce_and_keeps_owner_recovery(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(case.view, "ot_evidence", case.revision)
    wire = case.bridge.issue(**case.binding, ttl_seconds=2)
    deliver(case, wire)
    old = case.signed(wire)
    now = wire["expires_at"]
    result = case.bridge.reconcile(poll_interval=1)
    assert result["counts"] == {"expired": 1}
    assert (case.directory / f"zapier-approval-{wire['request_id']}-reconciled.json").exists()
    restart(case)
    with pytest.raises(ValueError, match="request_expired"):
        case.bridge.consume(old)
    assert case.bridge.claim_notification()["kind"] == "owner_action"
    assert case.bridge.claim_notification() is None
    with pytest.raises((ValueError, OSError)):
        case.authorize()


def test_cutover_fences_old_request_before_fresh_inspection_and_new_issue(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    delayed = case.signed(wire)
    previous_epoch = case.bridge.pending_summary()["installation_epoch"]
    assert case.bridge.prepare_cutover()["counts"] == {"superseded": 1}
    with pytest.raises(ValueError, match="request_reconciled"):
        case.bridge.consume(delayed)
    with pytest.raises(ValueError, match="approval_dispatch_paused"):
        case.bridge.issue(**case.binding)
    restart(case)
    assert case.bridge.pending_summary()["paused"]
    case.policy["zap_version"] = "version-2"
    private(case.bridge.config, case.policy)
    cutover = case.bridge.complete_cutover()
    assert cutover["installation_epoch"] != previous_epoch
    with pytest.raises(ValueError, match="plugin_policy_changed"):
        case.bridge.consume(delayed)
    case.view = case.h.launch()
    case.h.action(case.view, "ot_evidence", case.revision)
    fresh = case.bridge.issue(**case.binding)
    assert fresh["request_id"] != wire["request_id"] and fresh["nonce"] != wire["nonce"]
    deliver(case, fresh)
    current = case.signed(fresh)
    accepted = case.bridge.consume(current)
    assert case.bridge.consume(current) == accepted
    assert len(list(case.directory.glob("managed-outputs/*/release-decision.json"))) == 1
    assert case.bridge.pending_summary()["counts"] == {"approved": 1, "superseded": 1}


def test_concurrent_decision_conflict_is_quarantined_without_rewriting_first(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    envelopes = [case.signed(wire, status=s) for s in ("Approved", "Declined")]
    barrier = Barrier(2)

    def act(envelope):
        barrier.wait(timeout=5)
        try:
            return case.bridge.consume(envelope)
        except ValueError as error:
            return {"error": str(error)}

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(act, envelopes))
    assert sum("error" in result for result in results) == 1
    assert {result.get("error") for result in results} == {None, "conflicting_provider_event"}
    state = case.bridge.pending_summary()["items"][0]
    assert state["state"] in {"approved", "changes_requested"}
    assert state["reason"] == "conflicting_provider_event"
    assert state["provider_status"] in {"Approved", "Declined"}
    assert len(list(case.directory.glob("zapier-approval-*-conflict-*.json"))) == 1
    assert len(rows(case, "outbox")) == 1
    assert len(list(case.directory.glob("managed-outputs/*/release-decision.json"))) == 1


def test_cutover_and_old_callback_have_one_durable_order(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    event = case.signed(wire)
    barrier = Barrier(2)

    def callback():
        barrier.wait(timeout=5)
        try:
            return case.bridge.consume(event)["state"]
        except ValueError as error:
            return str(error)

    def cutover():
        barrier.wait(timeout=5)
        return case.bridge.prepare_cutover()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(callback), pool.submit(cutover)
        outcome, result = a.result(timeout=10), b.result(timeout=10)
    assert outcome in {"approved", "request_reconciled"}
    assert result["counts"] == ({"approved": 1} if outcome == "approved" else {"superseded": 1})
    before = len(list(case.directory.glob("managed-outputs/*/release-decision.json")))
    if outcome == "request_reconciled":
        with pytest.raises(ValueError, match="request_reconciled"):
            case.bridge.consume(event)
    else:
        assert case.bridge.consume(event)["state"] == "approved"
    assert len(list(case.directory.glob("managed-outputs/*/release-decision.json"))) == before


def test_reviewer_loss_invalidates_and_replacement_waits_for_saved_548_authority(
    case: Case,
) -> None:
    wire = case.inspect()
    deliver(case, wire)
    old = case.signed(wire)
    case.h.config["users"][case.policy["reviewer"]]["roles"].remove("review")
    case.h.save()
    state = case.bridge.reconcile(poll_interval=1)
    assert state["counts"] == {"held": 1}
    assert state["items"][0]["reason"] == "current_authority_unavailable"
    restart(case)
    with pytest.raises(ValueError, match="request_reconciled"):
        case.bridge.consume(old)
    assert case.bridge.claim_notification()["kind"] == "owner_action"
    assert case.bridge.claim_notification() is None
    case.bridge.prepare_cutover()
    case.policy.update(reviewer="USUCCESSOR", zap_version="version-2")
    private(case.bridge.config, case.policy)
    with pytest.raises(ValueError, match="reviewer_replacement_settings_required"):
        case.bridge.complete_cutover()
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


def test_revocation_between_poll_and_notification_claim_has_no_effect(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    case.bridge.reconcile(poll_interval=1)
    case.policy["enabled"] = False
    private(case.bridge.config, case.policy)
    with pytest.raises(ValueError, match="plugin_not_authorized"):
        case.bridge.claim_notification()
    assert rows(case, "outbox")[0][-1] == "pending"


def test_paging_cursor_is_tenant_epoch_bound_and_snapshot_does_not_grow(
    case: Case, tmp_path: Path
) -> None:
    case.inspect()
    policy, key = case.bridge._policy()
    journal = zapier_approval.ReconciliationJournal(case.directory, case.h.store.organization)
    original = json.loads(rows(case, "requests")[0][1])
    for index in (1, 2):
        journal.record(
            original | {"request_id": "za-" + str(index) * 32, "nonce": str(index) * 64}, policy
        )
    first = case.bridge.pending_summary(limit=1)
    cursor = first["next_cursor"]
    assert cursor and len(first["items"]) == 1
    journal.record(original | {"request_id": "za-" + "3" * 32, "nonce": "3" * 64}, policy)
    second = case.bridge.pending_summary(limit=2, cursor=cursor)
    assert len(second["items"]) == 2 and second["next_cursor"] is None
    assert all(item["request_id"] != "za-" + "3" * 32 for item in second["items"])
    foreign = make_case(tmp_path / "foreign", slot="foreign")
    with pytest.raises(ValueError, match="invalid_reconciliation_cursor"):
        foreign.bridge.pending_summary(cursor=cursor)
    with pytest.raises(ValueError, match="invalid_reconciliation_cursor"):
        journal.page(key, limit=1, cursor=cursor[:-4] + "xxxx")


def test_denied_first_caller_cannot_bind_the_installation_to_a_foreign_tenant(case: Case) -> None:
    foreign = zapier_approval.ApprovalBridge(case.directory, "foreign", case.bridge.config)
    with pytest.raises(ValueError, match="plugin_not_authorized"):
        foreign.pending_summary()
    assert not (case.directory / "zapier-reconciliation.sqlite3").exists()
    assert not (case.directory / "zapier-reconciliation.lock").exists()
    case.inspect()
    assert case.bridge.pending_summary()["counts"] == {"pending": 1}


def test_restart_repairs_marker_committed_before_journal_and_owner_outbox(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    wire = case.inspect()
    deliver(case, wire)
    event = case.signed(wire)
    with monkeypatch.context() as local:

        def interrupt(*_args, **_kwargs):
            raise OSError("generated worker stopped after nonce invalidation")

        local.setattr(zapier_approval.ReconciliationJournal, "transition", interrupt)
        with pytest.raises(OSError, match="generated worker"):
            case.bridge.prepare_cutover()
    restart(case)
    recovered = case.bridge.pending_summary()
    assert recovered["counts"] == {"superseded": 1}
    assert len(rows(case, "outbox")) == 1
    with pytest.raises(ValueError, match="request_reconciled"):
        case.bridge.consume(event)
    repeated = case.bridge.pending_summary()
    assert repeated["counts"] == recovered["counts"]
    assert repeated["installation_epoch"] == recovered["installation_epoch"]
    assert len(rows(case, "outbox")) == 1


def test_expired_unknown_attempt_cannot_reissue_after_new_inspection(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(case.view, "ot_evidence", case.revision)
    first = case.bridge.issue(**case.binding, ttl_seconds=2)

    def lost(_hook, _payload):
        raise TimeoutError("generated lost acknowledgement")

    case.bridge.flush(first["request_id"], HOOK, lost)
    now += 3
    case.view = case.h.launch()
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(case.view, "ot_evidence", case.revision)
    with pytest.raises(ValueError, match="remote_reconciliation_required"):
        case.bridge.issue(**case.binding)
    assert len(list(case.directory.glob("zapier-approval-za-*-request.json"))) == 1
    assert case.bridge.reconcile(poll_interval=1)["items"][0]["reason"] == "remote_outcome_unknown"


def test_transient_local_read_failure_holds_without_permanent_nonce_invalidation(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = case.inspect()
    deliver(case, wire)
    with monkeypatch.context() as local:

        def unavailable(*_args, **_kwargs):
            raise OSError("generated temporary disk failure")

        local.setattr(case.bridge, "decision_status", unavailable)
        assert (
            case.bridge.reconcile(poll_interval=1)["items"][0]["reason"]
            == "local_state_unavailable"
        )
    assert not list(case.directory.glob("zapier-approval-*-reconciled.json"))
    now += 1
    assert case.bridge.reconcile(poll_interval=1)["counts"] == {"pending": 1}
    assert case.bridge.consume(case.signed(wire))["state"] == "approved"


def test_superseded_reminder_is_retired_and_does_not_starve_new_request(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = case.inspect()
    deliver(case, wire)
    case.bridge.reconcile(poll_interval=1)
    case.bridge.prepare_cutover()
    case.policy["zap_version"] = "version-2"
    private(case.bridge.config, case.policy)
    case.bridge.complete_cutover()
    case.view = case.h.launch()
    case.h.action(case.view, "ot_evidence", case.revision)
    fresh = case.bridge.issue(**case.binding)
    deliver(case, fresh)
    now += 1
    case.bridge.reconcile(poll_interval=1)
    assert case.bridge.claim_notification()["kind"] == "owner_action"
    current = case.bridge.claim_notification()
    assert current["kind"] == "reminder" and current["request_id"] == fresh["request_id"]
    assert any(row[-1] == "cancelled" for row in rows(case, "outbox"))


def test_signed_pending_readback_resolves_uncertain_transport_before_cutover(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = case.inspect()

    def lost(_hook, _payload):
        raise TimeoutError("generated lost response")

    case.bridge.flush(wire["request_id"], HOOK, lost)
    assert case.bridge.reconcile(poll_interval=1)["counts"] == {"held": 1}
    assert case.bridge.consume(case.signed(wire, status="Pending"))["state"] == "pending"
    now += 1
    observed = case.bridge.reconcile(poll_interval=1)
    assert observed["counts"] == {"pending": 1}
    assert observed["items"][0]["provider_status"] == "Pending"
    restart(case)
    now += 1
    repeated = case.bridge.reconcile(poll_interval=1)
    assert repeated["counts"] == {"pending": 1}
    assert repeated["items"][0]["provider_status"] == "Pending"
    assert case.bridge.prepare_cutover()["counts"] == {"superseded": 1}
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


@pytest.mark.parametrize("change", [{"zap_version": "version-2"}, {"reviewer": "USUCCESSOR"}])
def test_direct_settings_drift_cannot_bypass_cutover_or_successor_authority(
    case: Case, change
) -> None:
    wire = case.inspect()
    deliver(case, wire)
    epoch = case.bridge.pending_summary()["installation_epoch"]
    case.policy.update(change)
    private(case.bridge.config, case.policy)
    with pytest.raises(ValueError, match="installation_cutover_required"):
        case.bridge.issue(**case.binding)
    with pytest.raises(ValueError, match="installation_cutover_required"):
        case.bridge.flush(wire["request_id"], HOOK, lambda *_: pytest.fail("no new dispatch"))
    with pytest.raises(ValueError, match="installation_cutover_required"):
        case.bridge.prepare_cutover()
    assert case.bridge.pending_summary()["installation_epoch"] == epoch
    assert len(list(case.directory.glob("zapier-approval-za-*-request.json"))) == 1
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


def test_custody_accounts_for_reconciliation_after_legacy_request_files_are_removed(
    case: Case,
) -> None:
    case.inspect()
    for path in case.directory.glob("zapier-approval-za-*.json"):
        path.unlink()
    assert not (case.directory / "zapier-decisions").exists()
    assert (case.directory / "zapier-reconciliation.sqlite3").exists()
    closeout = Closeout(case.directory, case.h.store.organization)
    report = closeout.withdraw(
        agreement="generated-terms",
        custodian="generated-custodian",
        support="generated-support",
        due="2040-04-01",
        trigger="withdrawal",
    )
    item = next(item for item in report["items"] if item["item"] == "plugin-decisions")
    assert item["disposition"] == "unresolved" and report["state"] == "open"


def test_reconcile_reference_is_scoped_and_repeat_is_throttled(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    result = case.bridge.reconcile(request_id=wire["request_id"])
    assert result["state"] == "reconciled" and result["counts"] == {"pending": 1}
    assert case.bridge.reconcile(request_id=wire["request_id"])["state"] == "throttled"
    before = rows(case, "outbox")
    with pytest.raises(ValueError, match="request_not_in_reconciliation_inventory"):
        case.bridge.reconcile(request_id="za-" + "f" * 32)
    assert rows(case, "outbox") == before
