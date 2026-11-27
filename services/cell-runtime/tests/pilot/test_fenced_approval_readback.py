"""Generated ordinary cutover readback through the actual signer and local bridge."""

from __future__ import annotations

import copy
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from opentour_cell.pilot import zapier_approval
from test_approval_reconciliation import restart, rows, signed_at_clock
from test_zapier_approval import HOOK, Case, deliver, make_case, private


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return make_case(tmp_path)


def short_request(case: Case):
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(case.view, "ot_evidence", case.revision)
    return case.bridge.issue(**case.binding, ttl_seconds=2)


def lose_reply(case: Case, wire):
    def lost(_hook, _payload):
        raise TimeoutError("generated lost response")

    assert case.bridge.flush(wire["request_id"], HOOK, lost)["transport"] == "uncertain"


def no_effects(case: Case):
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))
    assert not list(case.directory.glob("managed-outputs/*/handoff"))
    assert not list(case.directory.glob("zapier-approval-*-event.json"))


@pytest.mark.parametrize("status", ["Pending", "Approved", "Declined"])
def test_expired_unknown_readback_is_history_only_and_survives_restart(
    case: Case, monkeypatch: pytest.MonkeyPatch, status: str
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    event = signed_at_clock(case, wire, status)
    lose_reply(case, wire)
    now += 3
    assert case.bridge.reconcile(poll_interval=1)["counts"] == {"held": 1}
    assert case.bridge.prepare_cutover()["counts"] == {"held": 1}
    restart(case)
    observed = case.bridge.observe_fenced_status(event)
    assert observed["provider_status"] == status
    assert observed["release_decision"] == "not_recorded"
    before = rows(case, "requests")
    restart(case)
    assert case.bridge.observe_fenced_status(event) == observed
    assert rows(case, "requests") == before
    summary = case.bridge.pending_summary()
    assert summary["counts"] == {"expired": 1}
    assert summary["items"][0]["provider_status"] == status
    with pytest.raises(ValueError, match="request_reconciled|request_expired"):
        case.bridge.consume(event)
    no_effects(case)
    case.policy["zap_version"] = "version-2"
    private(case.bridge.config, case.policy)
    assert case.bridge.complete_cutover()["state"] == "ready"
    no_effects(case)
    case.view = case.h.launch()
    case.h.action(case.view, "ot_evidence", case.revision)
    fresh = case.bridge.issue(**case.binding)
    assert fresh["request_id"] != wire["request_id"] and fresh["nonce"] != wire["nonce"]
    deliver(case, fresh)
    new_event = signed_at_clock(case, fresh)
    accepted = case.bridge.consume(new_event)
    assert case.bridge.consume(new_event) == accepted
    assert len(list(case.directory.glob("managed-outputs/*/release-decision.json"))) == 1
    assert not list(case.directory.glob("managed-outputs/*/handoff"))


def test_pause_before_expiry_can_fence_authenticated_readback(case: Case):
    wire = case.inspect()
    event = case.signed(wire, status="Pending")
    lose_reply(case, wire)
    assert case.bridge.prepare_cutover()["counts"] == {"held": 1}
    result = case.bridge.observe_fenced_status(event)
    assert result["release_decision"] == "not_recorded"
    assert case.bridge.pending_summary()["counts"] == {"superseded": 1}
    with pytest.raises(ValueError, match="request_reconciled"):
        case.bridge.consume(event)
    no_effects(case)


def test_active_and_completed_requests_keep_existing_vote_path(case: Case):
    wire = case.inspect()
    event = case.signed(wire)
    deliver(case, wire)
    with pytest.raises(ValueError, match="request_not_fenced"):
        case.bridge.observe_fenced_status(event)
    assert not list(case.directory.glob("zapier-approval-*-reconciled.json"))
    accepted = case.bridge.consume(event)
    assert accepted["state"] == "approved"
    before = rows(case, "requests")
    with pytest.raises(ValueError, match="completed_request_observation_not_required"):
        case.bridge.observe_fenced_status(event)
    assert rows(case, "requests") == before
    assert case.bridge.consume(event) == accepted
    assert len(list(case.directory.glob("managed-outputs/*/release-decision.json"))) == 1


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("nonce", "0" * 64, "provider_scope_mismatch"),
        ("zap_id", "foreign-zap", "provider_scope_mismatch"),
        ("zap_version", "foreign-version", "provider_scope_mismatch"),
        ("actor_id", "UFOREIGN", "named_approver_unverified"),
        ("decided_at", 2**62, "invalid_provider_event"),
        ("status", "Unknown", "invalid_provider_event"),
        ("signature", "0" * 64, "provider_signature_invalid"),
    ],
)
def test_invalid_readback_cannot_fence_or_resolve_unknown(
    case: Case, monkeypatch: pytest.MonkeyPatch, field: str, value, error: str
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    envelope = signed_at_clock(case, wire)
    lose_reply(case, wire)
    now += 3
    if field == "signature":
        envelope[field] = value
    else:
        envelope["event"][field] = value
        envelope["signature"] = zapier_approval._sig(
            bytes.fromhex(case.key), zapier_approval.EVENT, envelope["event"]
        )
    with pytest.raises(ValueError, match=error):
        case.bridge.observe_fenced_status(envelope)
    assert not list(case.directory.glob("zapier-approval-*-reconciled.json"))
    assert not list(case.directory.glob("zapier-approval-*-fenced-observation-*.json"))
    assert case.bridge.prepare_cutover()["counts"] == {"held": 1}
    no_effects(case)


def test_no_readback_and_crash_after_fence_preserve_unknown_hold(
    case: Case, monkeypatch: pytest.MonkeyPatch
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    event = signed_at_clock(case, wire, "Pending")
    lose_reply(case, wire)
    now += 3
    case.bridge.prepare_cutover()
    with monkeypatch.context() as local:
        original = case.bridge._record_observation

        def stop(path, value):
            if "fenced-observation-" in path.name:
                raise OSError("generated stop after fence")
            return original(path, value)

        local.setattr(case.bridge, "_record_observation", stop)
        with pytest.raises(OSError, match="generated stop"):
            case.bridge.observe_fenced_status(event)
    assert list(case.directory.glob("zapier-approval-*-reconciled.json"))
    restart(case)
    state = case.bridge.pending_summary()
    assert state["counts"] == {"held": 1}
    assert state["items"][0]["reason"] == "remote_outcome_unknown"
    old_policy = copy.deepcopy(case.policy)
    case.policy["zap_version"] = "version-2"
    private(case.bridge.config, case.policy)
    with pytest.raises(ValueError, match="remote_reconciliation_required"):
        case.bridge.complete_cutover()
    private(case.bridge.config, old_policy)
    case.policy = old_policy
    case.bridge.observe_fenced_status(event)
    assert case.bridge.pending_summary()["counts"] == {"expired": 1}
    no_effects(case)


@pytest.mark.parametrize("conflict", ["Pending", "Declined"])
def test_conflicting_history_cannot_replace_authenticated_terminal_status(
    case: Case, monkeypatch: pytest.MonkeyPatch, conflict: str
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    accepted = signed_at_clock(case, wire)
    contradicted = signed_at_clock(case, wire, conflict)
    lose_reply(case, wire)
    now += 3
    observed = case.bridge.observe_fenced_status(accepted)
    with pytest.raises(ValueError, match="conflicting_provider_event"):
        case.bridge.observe_fenced_status(contradicted)
    assert observed["provider_status"] == "Approved"
    before = rows(case, "outbox")
    now += 3
    restart(case)
    with pytest.raises(ValueError, match="conflicting_provider_event"):
        case.bridge.observe_fenced_status(contradicted)
    with pytest.raises(ValueError, match="conflicting_provider_event"):
        case.bridge.observe_fenced_status(accepted)
    state = case.bridge.pending_summary()
    assert state["counts"] == {"held": 1}
    assert state["items"][0]["provider_status"] == "Approved"
    assert state["items"][0]["reason"] == "conflicting_provider_event"
    assert rows(case, "outbox") == before
    assert len(list(case.directory.glob("zapier-approval-*-fenced-observation-*.json"))) == 1
    assert len(list(case.directory.glob("zapier-approval-*-status-conflict-*.json"))) == 1
    case.view = case.h.launch()
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(case.view, "ot_evidence", case.revision)
    with pytest.raises(ValueError, match="conflicting_provider_event"):
        case.bridge.issue(**case.binding)
    assert case.bridge.prepare_cutover()["counts"] == {"held": 1}
    case.policy["zap_version"] = "version-2"
    private(case.bridge.config, case.policy)
    with pytest.raises(ValueError, match="remote_reconciliation_required"):
        case.bridge.complete_cutover()
    no_effects(case)


def test_source_cli_observes_only_original_private_request(
    case: Case, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    wire = case.inspect()
    event = case.signed(wire, status="Pending")
    lose_reply(case, wire)
    case.bridge.prepare_cutover()
    incoming = case.directory / "generated-provider-status.json"
    private(incoming, event)
    monkeypatch.setattr(
        "sys.argv",
        [
            "zapier-approval",
            "--directory",
            str(case.directory),
            "--organization",
            case.h.store.organization,
            "--config",
            str(case.bridge.config),
            "observe-fenced-status",
            "--input",
            str(incoming),
        ],
    )
    assert zapier_approval.main() == 0
    result = json.loads(capsys.readouterr().out)
    assert result["request_id"] == wire["request_id"]
    assert result["release_decision"] == "not_recorded"
    no_effects(case)


@pytest.mark.parametrize("never_sent", [False, True])
def test_unadmitted_or_known_never_sent_request_has_no_provider_history(
    case: Case, monkeypatch: pytest.MonkeyPatch, never_sent: bool
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    event = signed_at_clock(case, wire)
    calls = []
    if never_sent:
        original = os.fsync

        def synced(fd):
            nonlocal now
            original(fd)
            if case.bridge._path(wire["request_id"], "admitted").exists():
                now = wire["expires_at"]

        with monkeypatch.context() as local:
            local.setattr(os, "fsync", synced)
            with pytest.raises(ValueError, match="request_expired"):
                case.bridge.flush(wire["request_id"], HOOK, lambda *a: calls.append(a) or 202)
    now = wire["expires_at"]
    with pytest.raises((ValueError, OSError)):
        case.bridge.observe_fenced_status(event)
    assert calls == []
    assert not list(case.directory.glob("zapier-approval-*-fenced-observation-*.json"))
    assert case.bridge.pending_summary()["items"][0]["provider_status"] == "unknown"
    no_effects(case)


def test_foreign_tenant_and_missing_original_signer_cannot_observe(
    case: Case, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    event = signed_at_clock(case, wire)
    lose_reply(case, wire)
    foreign = make_case(tmp_path / "foreign", slot="foreign")
    foreign.bridge.prepare_cutover()
    with pytest.raises((ValueError, OSError)):
        foreign.bridge.observe_fenced_status(event)
    assert foreign.bridge.pending_summary()["counts"] == {}
    now += 3
    original = case.directory / case.policy["key_file"]
    original.rename(case.directory / "retired-test-key.json")
    with pytest.raises((ValueError, OSError)):
        case.bridge.observe_fenced_status(event)
    assert not list(case.directory.glob("zapier-approval-*-fenced-observation-*.json"))
    no_effects(case)
    no_effects(foreign)


def test_conflict_quarantine_survives_stop_before_journal_transition(
    case: Case, monkeypatch: pytest.MonkeyPatch
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    positive = signed_at_clock(case, wire)
    negative = signed_at_clock(case, wire, "Declined")
    lose_reply(case, wire)
    now += 3
    case.bridge.observe_fenced_status(positive)
    with monkeypatch.context() as local:
        transition = zapier_approval.ReconciliationJournal.transition

        def stop(self, request_id, state, reason, provider, **kwargs):
            if reason == "conflicting_provider_event":
                raise OSError("generated stopped after conflict receipt")
            return transition(self, request_id, state, reason, provider, **kwargs)

        local.setattr(zapier_approval.ReconciliationJournal, "transition", stop)
        with pytest.raises(OSError, match="stopped after conflict"):
            case.bridge.observe_fenced_status(negative)
    restart(case)
    recovered = case.bridge.pending_summary()
    assert recovered["counts"] == {"held": 1}
    assert recovered["items"][0]["reason"] == "conflicting_provider_event"
    assert recovered["items"][0]["provider_status"] == "Approved"
    assert len(list(case.directory.glob("zapier-approval-*-status-conflict-*.json"))) == 1
    no_effects(case)


@pytest.mark.parametrize(
    "status,state", [("Approved", "approved"), ("Declined", "changes_requested")]
)
def test_actual_completed_decision_before_journal_commit_is_never_fenced_as_history(
    case: Case, monkeypatch: pytest.MonkeyPatch, status: str, state: str
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    event = signed_at_clock(case, wire, status)
    deliver(case, wire)
    with monkeypatch.context() as local:
        transition = zapier_approval.ReconciliationJournal.transition

        def stop(self, request_id, decision, reason, provider, **kwargs):
            if decision == state:
                raise OSError("generated stopped after actual decision")
            return transition(self, request_id, decision, reason, provider, **kwargs)

        local.setattr(zapier_approval.ReconciliationJournal, "transition", stop)
        with pytest.raises(OSError, match="stopped after actual decision"):
            case.bridge.consume(event)
    receipts = list(case.directory.glob("managed-outputs/*/release-decision.json"))
    assert len(receipts) == 1
    original = receipts[0].read_bytes()
    now += 3
    restart(case)
    with pytest.raises(ValueError, match="completed_request_observation_not_required"):
        case.bridge.observe_fenced_status(event)
    assert not case.bridge._path(wire["request_id"], "reconciled").exists()
    assert not list(case.directory.glob("zapier-approval-*-fenced-observation-*.json"))
    assert case.bridge.prepare_cutover()["counts"] == {state: 1}
    assert receipts[0].read_bytes() == original
    assert not list(case.directory.glob("managed-outputs/*/handoff"))


@pytest.mark.parametrize("original_status", ["Approved", "Declined"])
@pytest.mark.parametrize("matches", [False, True])
def test_reserved_terminal_event_requires_matching_history_or_quarantine(
    case: Case, monkeypatch: pytest.MonkeyPatch, original_status: str, matches: bool
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    original = signed_at_clock(case, wire, original_status)
    other = signed_at_clock(case, wire, "Declined" if original_status == "Approved" else "Approved")
    deliver(case, wire)
    case.bridge.consume(signed_at_clock(case, wire, "Pending"))
    with monkeypatch.context() as local:

        def stop(*args, **kwargs):
            raise OSError("generated stopped after verified event reservation")

        local.setattr(zapier_approval.release_gate, "record_release", stop)
        with pytest.raises(OSError, match="after verified event"):
            case.bridge.consume(original)
    assert case.bridge._path(wire["request_id"], "event").exists()
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))
    now += 3
    restart(case)
    assert case.bridge.prepare_cutover()["counts"] == {"held": 1}
    if matches:
        result = case.bridge.observe_fenced_status(original)
        assert result["provider_status"] == original_status
        assert result["release_decision"] == "not_recorded"
        assert case.bridge.pending_summary()["counts"] == {"expired": 1}
    else:
        with pytest.raises(ValueError, match="conflicting_provider_event"):
            case.bridge.observe_fenced_status(other)
        restart(case)
        state = case.bridge.pending_summary()
        assert state["counts"] == {"held": 1}
        assert state["items"][0]["reason"] == "conflicting_provider_event"
        assert state["items"][0]["provider_status"] == "Pending"
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))
    assert not list(case.directory.glob("managed-outputs/*/handoff"))


@pytest.mark.parametrize("kind", ["reconciled", "status-conflict-"])
def test_stop_after_actual_rename_keeps_inventory_single_linked(
    case: Case, monkeypatch: pytest.MonkeyPatch, kind: str
):
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    wire = short_request(case)
    positive = signed_at_clock(case, wire)
    negative = signed_at_clock(case, wire, "Declined")
    lose_reply(case, wire)
    now += 3
    if kind == "status-conflict-":
        case.bridge.observe_fenced_status(positive)
    with monkeypatch.context() as local:
        rename = os.replace

        def stop(source, target):
            rename(source, target)
            if kind in target.name:
                raise OSError("generated stopped immediately after rename")

        local.setattr(os, "replace", stop)
        with pytest.raises(OSError, match="immediately after rename"):
            case.bridge.observe_fenced_status(negative if kind == "status-conflict-" else positive)
    restart(case)
    state = case.bridge.pending_summary()
    assert state["counts"] == {"held": 1}
    assert state["items"][0]["reason"] == (
        "conflicting_provider_event" if kind == "status-conflict-" else "remote_outcome_unknown"
    )
    assert all(path.stat().st_nlink == 1 for path in case.directory.glob("zapier-approval-*.json"))
    no_effects(case)
