"""Exercise an authenticated request's deadline at the actual release writer."""

from __future__ import annotations

import time
from unittest.mock import patch

import pytest
from opentour_cell.pilot import release_gate, zapier_approval
from test_approval_reconciliation import signed_at_clock
from test_zapier_approval import deliver, make_case


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

    def advance_after_effect(path, value):
        assert clock[0] < wire["expires_at"]
        original(path, value)
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
