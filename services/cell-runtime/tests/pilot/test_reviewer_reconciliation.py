"""Actual saved successor fences projected into the real approval journal."""

import json
import os
import secrets
import subprocess
import sys
import time
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from opentour_cell.pilot import reviewer_assignment, zapier_approval
from opentour_cell.pilot.training import LocalTrainingStore
from test_autonomous_cycle import read, save
from test_reviewer_assignment import AssignmentSetup
from test_reviewer_replacement import packet_for
from test_reviewer_replacement_callbacks import inspected_case
from test_zapier_approval import HOOK, deliver, make_case, resign


def pending(tmp_path, *, uncertain=False):
    setup = AssignmentSetup(tmp_path)
    case, wire, envelope = inspected_case(setup, tmp_path)
    if uncertain:
        transport = case.bridge._path(wire["request_id"], "transport")
        save(transport, {"transport": "uncertain"})
    else:
        case.bridge.consume(resign(case, envelope, {"status": "Pending", "actor_id": None}))
    packet, _ = packet_for(setup)
    receipt = setup.service.save(packet, actor=setup.owner)
    return setup, case, wire, envelope, receipt


def project(setup, case):
    return case.bridge.reconcile_reviewers(authority=setup.authority, actor=setup.operator)


def test_signed_replacement_projects_once_without_vote_or_notification(tmp_path):
    s, case, wire, envelope, receipt = pending(tmp_path)
    result = project(s, case)
    assert result["remaining"] == 0 and len(result["records"]) == 1
    assert result["provider_outcome"] == "not_qualified"
    assert project(s, case)["records"] == []
    state = case.bridge.pending_summary()
    assert state["paused"] and state["counts"] == {"superseded": 1}
    assert state["items"][0]["request_id"] == wire["request_id"]
    assert s.service.inspect(actor=s.owner)["current"] == receipt
    with pytest.raises(ValueError):
        case.bridge.consume(envelope)
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))


def test_unknown_outcome_remains_held_after_projection_ack_and_restart(tmp_path):
    s, case, wire, envelope, _ = pending(tmp_path, uncertain=True)
    project(s, case)
    case.bridge = zapier_approval.ApprovalBridge(case.directory, s.customer.org, case.bridge.config)
    state = case.bridge.pending_summary()
    assert state["counts"] == {"held": 1}
    assert state["items"][0]["reason"] == "remote_outcome_unknown"
    with pytest.raises(ValueError):
        case.bridge.consume(envelope)
    with pytest.raises(ValueError, match="remote_reconciliation_required"):
        case.bridge._assert_no_unknown_proposal(case.binding)
    assert s.assignments.pending_reconciliations(actor=s.operator)["pending_count"] == 0


def test_hook_acceptance_alone_is_unknown_until_signed_old_observation(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, wire, envelope = inspected_case(s, tmp_path)
    packet, _ = packet_for(s)
    s.service.save(packet, actor=s.owner)
    project(s, case)
    assert case.bridge.pending_summary()["counts"] == {"held": 1}
    observed = case.bridge.observe_replaced_status(envelope)
    assert observed["provider_status"] == "Approved"
    assert observed["release_decision"] == "not_recorded"
    assert case.bridge.observe_replaced_status(envelope) == observed
    assert case.bridge.pending_summary()["counts"] == {"superseded": 1}
    with pytest.raises(ValueError):
        case.bridge.consume(envelope)
    with pytest.raises(ValueError, match="conflicting_provider_event"):
        case.bridge.observe_replaced_status(resign(case, envelope, {"status": "Declined"}))
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))


def test_current_saved_reviewer_refresh_is_idempotent_but_does_not_invent_inspection(tmp_path):
    s, case, _, _, receipt = pending(tmp_path)
    before = case.bridge.pending_summary()["installation_epoch"]
    project(s, case)
    case.policy["reviewer"] = s.successor
    save(case.bridge.config, case.policy)
    result = case.bridge.complete_reviewer_replacement(authority=s.authority, actor=s.operator)
    assert result["generation"] == receipt["revision"]
    assert result["installation_epoch"] != before
    assert (
        case.bridge.complete_reviewer_replacement(authority=s.authority, actor=s.operator) == result
    )
    assert not case.bridge.pending_summary()["paused"]
    # Real successor evidence consumers are not yet integrated in this source stack.
    with pytest.raises(ValueError):
        case.bridge.issue(**case.binding)
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))


def test_membership_loss_marker_survives_saved_replacement_projection(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, wire, envelope = inspected_case(s, tmp_path)
    case.bridge.consume(resign(case, envelope, {"status": "Pending", "actor_id": None}))
    case.h.config["users"][s.reviewer]["roles"].remove("review")
    case.h.save()
    assert case.bridge.reconcile(poll_interval=1)["counts"] == {"held": 1}
    marker = case.bridge._path(wire["request_id"], "reconciled")
    original = read(marker)
    assert original["reason"] == "current_authority_unavailable"
    packet, _ = packet_for(s)
    s.service.save(packet, actor=s.owner)
    project(s, case)
    assert read(marker) == original
    assert case.bridge.pending_summary()["counts"] == {"superseded": 1}
    case.bridge.observe_replaced_status(envelope)
    case.policy["reviewer"] = s.successor
    save(case.bridge.config, case.policy)
    assert (
        case.bridge.complete_reviewer_replacement(authority=s.authority, actor=s.operator)["state"]
        == "fresh_successor_inspection_required"
    )
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))


def test_late_authenticated_old_observation_clears_unknown_without_a_vote(tmp_path, monkeypatch):
    s = AssignmentSetup(tmp_path)
    issue = zapier_approval.ApprovalBridge._issue

    def shorter(self, **arguments):
        return issue(self, **(arguments | {"ttl_seconds": 2}))

    with patch.object(zapier_approval.ApprovalBridge, "_issue", shorter):
        case, wire, envelope = inspected_case(s, tmp_path)
    packet, _ = packet_for(s)
    s.service.save(packet, actor=s.owner)
    project(s, case)
    now = wire["expires_at"] + 1
    monkeypatch.setattr(time, "time", lambda: now)
    late = resign(case, envelope, {"decided_at": now})
    assert case.bridge.observe_replaced_status(late)["release_decision"] == "not_recorded"
    assert case.bridge.pending_summary()["counts"] == {"superseded": 1}
    with pytest.raises(ValueError):
        case.bridge.consume(late)
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))


def test_retired_signer_preserves_closed_lane_during_next_reviewer_projection(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, old, old_event = inspected_case(s, tmp_path)
    case.bridge.consume(resign(case, old_event, {"status": "Pending"}))
    assert case.bridge.prepare_cutover()["counts"] == {"superseded": 1}
    old_key_path = case.directory / case.policy["key_file"]
    case.key = secrets.token_hex(32)
    case.policy.update(zap_version="generated-v2", key_file="next-signer.json")
    save(case.directory / case.policy["key_file"], {"secret_hex": case.key})
    save(case.bridge.config, case.policy)
    old_key_path.unlink()
    case.bridge.complete_cutover()
    case.view = case.h.launch()
    case.h.action(case.view, "ot_evidence", case.revision)
    fresh = case.bridge.issue(**case.binding)
    deliver(case, fresh)
    case.bridge.consume(case.signed(fresh, status="Pending"))
    packet, _ = packet_for(s)
    s.service.save(packet, actor=s.owner)
    project(s, case)
    assert case.bridge.pending_summary()["counts"] == {"superseded": 2}
    with pytest.raises(ValueError, match="original_provider_signer_required"):
        case.bridge.observe_replaced_status(old_event)
    case.policy["reviewer"] = s.successor
    save(case.bridge.config, case.policy)
    assert (
        case.bridge.complete_reviewer_replacement(authority=s.authority, actor=s.operator)["state"]
        == "fresh_successor_inspection_required"
    )
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))


def test_crash_after_projection_before_ack_repeats_without_new_outbox(tmp_path):
    s, case, _, _, _ = pending(tmp_path)
    original = reviewer_assignment.ReviewerAssignments.acknowledge_reconciliation
    with patch.object(
        reviewer_assignment.ReviewerAssignments,
        "acknowledge_reconciliation",
        side_effect=OSError("generated stop before ACK"),
    ):
        with pytest.raises(OSError):
            project(s, case)
    assert s.assignments.pending_reconciliations(actor=s.operator)["pending_count"] == 1
    assert case.bridge.pending_summary()["counts"] == {"superseded": 1}
    result = project(s, case)
    assert result["remaining"] == 0
    assert reviewer_assignment.ReviewerAssignments.acknowledge_reconciliation is original
    journal = zapier_approval.ReconciliationJournal(case.directory, s.customer.org)
    with journal.db() as db:
        assert db.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM reviewer_projections").fetchone()[0] == 1


@pytest.mark.parametrize("fault", ["seal", "actor"])
def test_projection_requires_actual_seal_and_current_operator(tmp_path, fault):
    s, case, wire, _, _ = pending(tmp_path)
    if fault == "seal":
        path = case.bridge._path(wire["request_id"], "request")
        save(path, dict(read(path), seal="0" * 64))
        actor = s.operator
    else:
        actor = s.reviewer
    with pytest.raises(ValueError):
        case.bridge.reconcile_reviewers(authority=s.authority, actor=actor)
    assert s.assignments.pending_reconciliations(actor=s.operator)["pending_count"] == 1
    assert not case.bridge._path(wire["request_id"], "reconciled").exists()


@pytest.mark.parametrize("reason", ["version_superseded", "request_expired"])
def test_legacy_marker_cannot_clear_projected_unknown_outcome(tmp_path, reason):
    s = AssignmentSetup(tmp_path)
    case, wire, envelope = inspected_case(s, tmp_path)
    # A prior bridge could mark an HTTP-accepted request closed without a signed
    # approval-action observation. Preserve that immutable historical marker.
    case.bridge._invalidate(wire["request_id"], reason)
    marker = case.bridge._path(wire["request_id"], "reconciled")
    original = read(marker)
    packet, _ = packet_for(s)
    s.service.save(packet, actor=s.owner)
    project(s, case)
    restarted = zapier_approval.ApprovalBridge(case.directory, s.customer.org, case.bridge.config)
    assert restarted.pending_summary()["counts"] == {"held": 1}
    assert read(marker) == original
    assert restarted.observe_replaced_status(envelope)["release_decision"] == "not_recorded"
    assert restarted.pending_summary()["counts"] == {"superseded": 1}
    assert read(marker) == original
    with pytest.raises(ValueError):
        restarted.consume(envelope)
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))


def test_summary_does_not_count_retries_or_reobserve_projected_unknown(tmp_path, monkeypatch):
    s, case, wire, _, _ = pending(tmp_path, uncertain=True)
    project(s, case)
    journal = zapier_approval.ReconciliationJournal(case.directory, s.customer.org)
    with journal.db() as db:
        before = tuple(
            db.execute("SELECT * FROM requests WHERE id=?", (wire["request_id"],)).fetchone()
        )
    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now + 2)
    for _ in range(3):
        assert case.bridge.pending_summary()["counts"] == {"held": 1}
    with journal.db() as db:
        after = tuple(
            db.execute("SELECT * FROM requests WHERE id=?", (wire["request_id"],)).fetchone()
        )
    assert after == before


def test_dispatch_rechecks_request_expiry_after_waiting_for_writer(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    issue = zapier_approval.ApprovalBridge._issue
    with patch.object(
        zapier_approval.ApprovalBridge,
        "_issue",
        lambda self, **arguments: issue(self, **(arguments | {"ttl_seconds": 2})),
    ):
        wire = case.inspect()
    original = LocalTrainingStore.engagement_guard
    clock = int(time.time())
    monkeypatch.setattr(time, "time", lambda: clock)

    @contextmanager
    def after_wait(store, **arguments):
        nonlocal clock
        with original(store, **arguments):
            clock = wire["expires_at"]
            yield

    calls = []
    with patch.object(LocalTrainingStore, "engagement_guard", after_wait):
        with pytest.raises(ValueError, match="request_expired"):
            case.bridge.flush(wire["request_id"], HOOK, lambda *args: calls.append(args) or 202)
    assert calls == []
    assert not case.bridge._path(wire["request_id"], "admitted").exists()


def test_expiry_during_admission_sync_records_known_zero_submission(tmp_path, monkeypatch):
    case = make_case(tmp_path)
    issue = zapier_approval.ApprovalBridge._issue
    with patch.object(
        zapier_approval.ApprovalBridge,
        "_issue",
        lambda self, **arguments: issue(self, **(arguments | {"ttl_seconds": 2})),
    ):
        wire = case.inspect()
    original = os.fsync
    clock = int(time.time())
    monkeypatch.setattr(time, "time", lambda: clock)

    def synced(fd):
        nonlocal clock
        original(fd)
        if case.bridge._path(wire["request_id"], "admitted").exists():
            clock = wire["expires_at"]

    calls = []
    monkeypatch.setattr(os, "fsync", synced)
    with pytest.raises(ValueError, match="request_expired"):
        case.bridge.flush(wire["request_id"], HOOK, lambda *args: calls.append(args) or 202)
    assert calls == []
    assert case.bridge._path(wire["request_id"], "admitted").exists()
    assert not case.bridge._unknown_outcome(wire["request_id"], "unknown")
    assert case.bridge.reconcile(poll_interval=1)["counts"] == {"expired": 1}


def test_same_filename_signer_rotation_preserves_verified_closure_and_key_recovery(tmp_path):
    case = make_case(tmp_path)
    old = case.inspect()
    deliver(case, old)
    event = case.signed(old, status="Pending")
    case.bridge.consume(event)
    original_policy = case.directory / "original-policy.json"
    save(original_policy, case.policy)
    original_key = case.directory / "archived-signer.json"
    save(original_key, {"secret_hex": case.key})
    case.bridge.prepare_cutover()
    case.key = secrets.token_hex(32)
    case.policy["zap_version"] = "generated-v2"
    save(case.directory / case.policy["key_file"], {"secret_hex": case.key})
    save(case.bridge.config, case.policy)
    assert case.bridge.complete_cutover()["state"] == "ready"
    assert case.bridge.pending_summary()["counts"] == {"superseded": 1}
    journal = zapier_approval.ReconciliationJournal(case.directory, case.h.store.organization)
    with journal.db() as db:
        db.execute("DELETE FROM policies")
        db.execute("DELETE FROM verified_requests")
    assert case.bridge.pending_summary()["counts"] == {"held": 1}
    restored = case.bridge.recover_request_authority(
        old["request_id"], original_policy=original_policy, original_key=original_key
    )
    assert restored["state"] == "original_authority_restored"
    assert case.bridge.pending_summary()["counts"] == {"superseded": 1}
    assert read(case.directory / case.policy["key_file"])["secret_hex"] == case.key
    request, _, _, signer = case.bridge._archived_request(
        journal, old["request_id"], bytes.fromhex(case.key)
    )
    assert request["request_id"] == old["request_id"]
    assert signer == bytes.fromhex(read(original_key)["secret_hex"])


def test_replaced_pending_before_projection_cannot_reopen_active_request(tmp_path):
    s, case, wire, envelope, _ = pending(tmp_path)
    before = case.bridge.pending_summary()
    with pytest.raises(ValueError):
        case.bridge.consume(resign(case, envelope, {"status": "Pending"}))
    assert case.bridge.pending_summary() == before
    assert s.assignments.pending_reconciliations(actor=s.operator)["pending_count"] == 1
    assert not case.bridge._path(wire["request_id"], "event").exists()


def test_completed_receipt_survives_replacement_without_provider_status_inference(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, wire, envelope = inspected_case(s, tmp_path)
    decision = case.bridge.consume(envelope)
    assert decision["state"] == "approved"
    journal = zapier_approval.ReconciliationJournal(case.directory, s.customer.org)
    # A prior worker may stop after the durable release before its provider observation commits.
    with journal.db() as db:
        db.execute(
            "UPDATE requests SET provider_status='unknown' WHERE id=?", (wire["request_id"],)
        )
    packet, _ = packet_for(s)
    saved = s.service.save(packet, actor=s.owner)
    assert project(s, case)["records"] == []
    case.bridge.prepare_cutover()
    case.policy["reviewer"] = s.successor
    save(case.bridge.config, case.policy)
    adopted = case.bridge.complete_reviewer_replacement(authority=s.authority, actor=s.operator)
    assert adopted["generation"] == saved["revision"]
    state = case.bridge.pending_summary()["items"][0]
    assert state["state"] == "approved" and state["provider_status"] == "unknown"
    assert len(list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))) == 1


def test_adoption_preserves_completed_reissue_after_original_mandate_withdrawal(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, old, envelope = inspected_case(s, tmp_path)
    case.bridge.consume(resign(case, envelope, {"status": "Pending"}))
    case.bridge.prepare_cutover()
    case.policy["zap_version"] = "generated-v2"
    save(case.bridge.config, case.policy)
    case.bridge.complete_cutover()
    case.view = case.h.launch()
    case.h.action(case.view, "ot_evidence", case.revision)
    fresh = case.bridge.issue(**case.binding)
    assert fresh["request_id"] != old["request_id"]
    deliver(case, fresh)
    case.bridge.consume(case.signed(fresh))
    journal = zapier_approval.ReconciliationJournal(case.directory, s.customer.org)
    with journal.db() as db:
        db.execute(
            "UPDATE requests SET provider_status='unknown' WHERE id=?", (fresh["request_id"],)
        )
    packet, _ = packet_for(s)
    receipt = s.service.save(packet, actor=s.owner)
    assert project(s, case)["records"] == []
    case.bridge.prepare_cutover()
    case.policy["reviewer"] = s.successor
    save(case.bridge.config, case.policy)
    adopted = case.bridge.complete_reviewer_replacement(authority=s.authority, actor=s.operator)
    assert adopted["generation"] == receipt["revision"]
    assert case.bridge.pending_summary()["counts"] == {"approved": 1, "superseded": 1}
    # The actual final receipt belongs only to the newly inspected request.
    policy, key = case.bridge._policy()
    old_request, _, _, _ = case.bridge._archived_request(journal, old["request_id"], key)
    assert case.bridge._completed_decision(old_request) is None
    assert len(list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))) == 1


def test_new_installation_epoch_requires_a_distinct_actual_presentation(tmp_path):
    case = make_case(tmp_path)
    old = case.inspect()
    deliver(case, old)
    case.bridge.consume(case.signed(old, status="Pending"))
    case.bridge.prepare_cutover()
    case.policy["zap_version"] = "generated-v2"
    save(case.bridge.config, case.policy)
    case.bridge.complete_cutover()
    with pytest.raises(ValueError, match="fresh_epoch_presentation_required"):
        case.bridge.issue(**case.binding)
    assert len(list(case.directory.glob("zapier-approval-*-request.json"))) == 1
    case.view = case.h.launch()
    case.h.action(case.view, "ot_evidence", case.revision)
    fresh = case.bridge.issue(**case.binding)
    assert fresh["request_id"] != old["request_id"]
    assert case.bridge.issue(**case.binding) == fresh


def test_legacy_ambiguous_presentation_cannot_attribute_a_completed_receipt(tmp_path):
    case = make_case(tmp_path)
    wire = case.inspect()
    deliver(case, wire)
    case.bridge.consume(case.signed(wire))
    policy, key = case.bridge._policy()
    req = case.bridge._request(wire["request_id"], policy, key)
    assert case.bridge._completed_decision(req) == "approved"
    # Generated legacy ambiguity: two valid sealed nonces once shared a presentation.
    other = req | {"request_id": "za-" + secrets.token_hex(16), "nonce": secrets.token_hex(32)}
    save(
        case.bridge._path(other["request_id"], "request"),
        {"request": other, "seal": zapier_approval._sig(key, zapier_approval.REQUEST, other)},
    )
    journal = zapier_approval.ReconciliationJournal(case.directory, case.h.store.organization)
    journal.record(other, policy)
    assert case.bridge._completed_decision(req) is None
    assert case.bridge._completed_decision(other) is None
    # Neither absence nor presence of a callback reservation proves final ownership.
    save(case.bridge._path(other["request_id"], "event"), {"event_identity": "generated-reserved"})
    assert case.bridge._completed_decision(other) is None
    assert len(list(case.directory.glob("managed-outputs/*/release-decision.json"))) == 1


@pytest.mark.parametrize("retired_key", [False, True])
def test_legacy_archive_gap_holds_only_old_lane_until_original_material_is_restored(
    tmp_path, retired_key
):
    case = make_case(tmp_path)
    old = case.inspect()
    deliver(case, old)
    case.bridge.consume(case.signed(old, status="Pending"))
    original_policy = case.directory / "original-policy.json"
    save(original_policy, case.policy)
    key_path = case.directory / case.policy["key_file"]
    original_secret = read(key_path)
    case.bridge.prepare_cutover()
    case.key = secrets.token_hex(32)
    case.policy.update(zap_version="generated-v2", key_file="next-signer.json")
    save(case.directory / case.policy["key_file"], {"secret_hex": case.key})
    save(case.bridge.config, case.policy)
    case.bridge.complete_cutover()
    case.view = case.h.launch()
    case.h.action(case.view, "ot_evidence", case.revision)
    current = case.bridge.issue(**case.binding)
    deliver(case, current)
    journal = zapier_approval.ReconciliationJournal(case.directory, case.h.store.organization)
    # Model the shipped v1 schema: it had no policy or authenticated-wrapper archive.
    with journal.db() as db:
        db.execute("DELETE FROM policies")
        db.execute("DELETE FROM verified_requests")
    if retired_key:
        key_path.unlink()
    summary = case.bridge.pending_summary()
    item = next(row for row in summary["items"] if row["request_id"] == old["request_id"])
    assert item["state"] == "held" and item["reason"] == "original_request_policy_required"
    assert case.bridge.consume(case.signed(current, status="Pending"))["state"] == "pending"
    case.bridge.reconcile(request_id=current["request_id"], poll_interval=1)
    if retired_key:
        with pytest.raises(ValueError, match="original_request_signer_required"):
            case.bridge.recover_request_authority(
                old["request_id"], original_policy=original_policy
            )
        save(key_path, original_secret)
    result = case.bridge.recover_request_authority(
        old["request_id"], original_policy=original_policy
    )
    assert result["state"] == "original_authority_restored"
    assert case.bridge.pending_summary()["counts"] == {"pending": 1, "superseded": 1}
    assert not list(case.directory.glob("managed-outputs/*/release-decision.json"))


def test_reconcile_reviewers_cli_projects_actual_saved_intent(tmp_path):
    s, case, _, _, _ = pending(tmp_path)
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "opentour_cell.pilot.zapier_approval",
            "--directory",
            str(case.directory),
            "--organization",
            s.customer.org,
            "--config",
            str(case.bridge.config),
            "reconcile-reviewers",
            "--authority",
            str(s.authority),
            "--actor",
            s.operator,
        ],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["state"] == "projected" and value["remaining"] == 0
    assert case.bridge.pending_summary()["counts"] == {"superseded": 1}
