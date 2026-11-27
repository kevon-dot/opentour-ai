"""Exercise an authenticated request's deadline at the actual release writer."""

from __future__ import annotations

import os
import time
from unittest.mock import patch

import pytest
from opentour_cell.pilot import release_gate, zapier_approval
from test_approval_reconciliation import signed_at_clock
from test_zapier_approval import deliver, make_case, private


@pytest.fixture
def admitted_request(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(case.view, "ot_evidence", case.revision)
    clock = [int(time.time())]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    wire = case.bridge.issue(**case.binding, ttl_seconds=5)
    deliver(case, wire)
    event = signed_at_clock(case, wire)
    return case, wire, event, clock


@pytest.mark.parametrize("remaining", [1, 0])
def test_authenticated_callback_rechecks_deadline_at_real_writer(
    admitted_request, monkeypatch, remaining
):
    case, wire, event, clock = admitted_request
    original = release_gate.record_release

    def before_writer(*args, **kwargs):
        # The event already passed authentication. Expire the short request
        # while its separately valid presentation is still fresh.
        assert kwargs["provider_request_expires_at"] == wire["expires_at"]
        assert clock[0] < wire["expires_at"]
        clock[0] = wire["expires_at"] - remaining
        return original(*args, **kwargs)

    monkeypatch.setattr(release_gate, "record_release", before_writer)
    if remaining:
        first = case.bridge.consume(event)
        assert first["state"] == "approved"
        monkeypatch.setattr(release_gate, "record_release", original)
        assert case.bridge.consume(event) == first
        assert len(list(case.directory.glob("managed-outputs/*/release-decision.json"))) == 1
        return

    with pytest.raises(ValueError, match="request_expired"):
        case.bridge.consume(event)
    assert case.bridge._path(wire["request_id"], "event").exists()
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))
    history = case.bridge.observe_fenced_status(event)
    assert history["provider_status"] == "Approved"
    assert history["release_decision"] == "not_recorded"
    assert case.bridge.pending_summary()["counts"] == {"expired": 1}
    assert not list(case.directory.glob("managed-outputs/*/handoff*"))


def test_completed_release_survives_expiry_before_journal_ack(admitted_request, monkeypatch):
    case, wire, event, clock = admitted_request
    original = release_gate._record

    def advance_after_effect(path, value, **kwargs):
        assert clock[0] < wire["expires_at"]
        original(path, value, **kwargs)
        clock[0] = wire["expires_at"]

    monkeypatch.setattr(release_gate, "_record", advance_after_effect)
    receipt = case.bridge.consume(event)
    assert receipt["state"] == "approved"
    assert len(list(case.directory.glob("managed-outputs/*/release-decision.json"))) == 1
    assert case.bridge.pending_summary()["counts"] == {"approved": 1}


def test_zapier_effect_writer_requires_the_bridge_request_deadline(admitted_request):
    case, _, _, _ = admitted_request
    with pytest.raises(ValueError, match="sealed_provider_deadline_required"):
        release_gate.record_release(
            case.directory,
            case.h.store.organization,
            **case.binding,
            reviewer=case.policy["reviewer"],
            team=case.policy["team"],
            channel=case.policy["channel"],
            audience=case.policy["audience"],
            decision="approved",
            provider="zapier_slack",
        )
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


@pytest.mark.parametrize("remaining", [1, 0])
def test_publication_rechecks_deadline_after_real_fsync(admitted_request, monkeypatch, remaining):
    case, wire, event, clock = admitted_request
    work = case.directory / "managed-outputs" / case.binding["run_id"]
    before = set(work.iterdir())
    publish = release_gate._record
    fsync = os.fsync
    flush_clocks = []

    def slow_staging(path, value, **kwargs):
        def finish_flush(fd):
            fsync(fd)
            assert not path.exists()
            flush_clocks.append(clock[0])
            clock[0] = wire["expires_at"] - remaining

        with monkeypatch.context() as storm:
            storm.setattr(os, "fsync", finish_flush)
            return publish(path, value, **kwargs)

    monkeypatch.setattr(release_gate, "_record", slow_staging)
    recorded = work / "release-decision.json"
    if remaining:
        assert case.bridge.consume(event)["state"] == "approved"
        assert set(work.iterdir()) == before | {recorded}
    else:
        with pytest.raises(ValueError, match="request_expired"):
            case.bridge.consume(event)
        assert set(work.iterdir()) == before
        assert case.bridge._path(wire["request_id"], "event").exists()
        assert case.bridge.observe_fenced_status(event)["release_decision"] == "not_recorded"
    assert len(flush_clocks) == 1 and flush_clocks[0] < wire["expires_at"]


def test_pending_cannot_advance_status_after_owner_disables_policy(admitted_request, monkeypatch):
    case, wire, _, _ = admitted_request
    event = signed_at_clock(case, wire, status="Pending")
    inspect = case.bridge._inspect
    journal = zapier_approval.ReconciliationJournal(case.directory, case.h.store.organization)

    def stored():
        with journal.db() as db:
            return tuple(
                db.execute(
                    "SELECT state, provider_status FROM requests WHERE id=?", (wire["request_id"],)
                ).fetchone()
            )

    def disable_after_inspection(*args, **kwargs):
        seen = inspect(*args, **kwargs)
        private(case.bridge.config, {**case.policy, "enabled": False})
        return seen

    before = stored()
    monkeypatch.setattr(case.bridge, "_inspect", disable_after_inspection)
    with pytest.raises(ValueError, match="plugin_not_authorized"):
        case.bridge.consume(event)
    assert stored() == before
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))
