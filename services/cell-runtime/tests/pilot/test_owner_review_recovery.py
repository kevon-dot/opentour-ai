"""Owner recovery through signed settings and actual approval projection."""

from __future__ import annotations

import secrets
import sqlite3
import time
from copy import deepcopy

import pytest
from opentour_cell.pilot import release_gate, zapier_approval
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.owner_settings import AUTHORITY as LEGACY_AUTHORITY
from opentour_cell.pilot.owner_settings import SettingsError
from opentour_cell.pilot.reviewer_assignment import RECOVERY, ReviewerAssignments
from opentour_cell.pilot.slack_adapter import hook_fingerprint
from test_autonomous_cycle import read, save
from test_reviewer_assignment import AssignmentSetup
from test_reviewer_replacement import packet_for, records
from test_reviewer_replacement_callbacks import inspected_case, prepare_next_zap_revision
from test_zapier_approval import HOOK, resign


def build_bridge(s: AssignmentSetup) -> zapier_approval.ApprovalBridge:
    """Bind a generated installation without making or sending a request."""
    directory = s.customer.owner
    config = directory / "zapier-approval.json"
    save(
        config,
        {
            "schema_version": zapier_approval.POLICY,
            "enabled": True,
            "organization": s.customer.org,
            "engagement": s.customer.eng,
            **s.current["policy"]["agreement"]["release_review"],
            "zap_id": "generated-zap",
            "zap_version": "generated-version-one",
            "approver_mode": "verified_actor",
            "setup_attestation": "att-abcdef0123456789",
            "key_file": "zapier-signer.json",
            "expires_at": int(time.time()) + 600,
            "hook_fingerprint": hook_fingerprint(HOOK),
        },
    )
    save(directory / "zapier-signer.json", {"secret_hex": secrets.token_hex(32)})
    return zapier_approval.ApprovalBridge(directory, s.customer.org, config)


def binding(proposal):
    return {key: proposal[key] for key in ("run_id", "run_revision", "artifact_sha")}


@pytest.mark.parametrize("nonce_count", [0, 2])
def test_one_private_proposal_item_survives_projection_and_notice_outcome(tmp_path, nonce_count):
    s = AssignmentSetup(tmp_path)
    if nonce_count:
        case, first, envelope = inspected_case(s, tmp_path)
        prepare_next_zap_revision(case, envelope)
        second = case.bridge.issue(**case.binding)

        def lost(_hook, _payload):
            raise TimeoutError("generated unknown provider response")

        assert case.bridge.flush(second["request_id"], HOOK, lost)["transport"] == "uncertain"
        nonce_refs = [first["request_id"], second["request_id"]]
        bridge = case.bridge
    else:
        nonce_refs = []
        bridge = build_bridge(s)
    packet, assignment = packet_for(s)
    proposal = binding(assignment["proposals"][0])
    assert s.assignments.owner_recovery(**proposal, actor=s.workflow_owner) is None
    receipt = s.service.save(packet, actor=s.owner)
    assert records(s)[0][0]["expired_request_ids"] == sorted(nonce_refs)
    item = s.assignments.owner_recovery(**proposal, actor=s.workflow_owner)
    assert item == {
        "schema_version": RECOVERY,
        "recovery_ref": receipt["reviewer_replacement"]["proposals"][0]["reconciliation_ref"],
        "proposal_ref": proposal["run_id"],
        "settings_generation": receipt["revision"],
        "state": "review_on_hold",
        "reason": "fresh_successor_inspection_required",
        "next_action": "check_reviewer_replacement_reconciliation",
    }
    masked = canonical(item)
    for private_value in [s.reviewer, s.successor, s.workflow_owner, *nonce_refs]:
        assert private_value not in masked
    assert s.assignments.owner_recovery(**proposal, actor=s.owner) == item
    assert s.assignments.owner_recovery(**proposal, actor=s.operator) == item

    projected = bridge.reconcile_reviewers(authority=s.authority, actor=s.operator)
    assert [record["id"] for record in projected["records"]] == [item["recovery_ref"]]
    assert projected["remaining"] == 0
    assert s.assignments.pending_reconciliations(actor=s.operator)["pending_count"] == 0
    if nonce_count:
        claim = bridge.claim_notification()
        assert claim is not None and claim["kind"] == "owner_action"
        # Stop before any consumer transmission or confirmation.
    else:
        assert bridge.claim_notification() is None

    restarted = ReviewerAssignments(s.service)
    ledger = s.customer.owner / "learning-mandates.sqlite3"
    before = ledger.read_bytes()
    assert restarted.owner_recovery(**proposal, actor=s.workflow_owner) == item
    assert restarted.owner_recovery(**proposal, actor=s.owner) == item
    assert ledger.read_bytes() == before
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))
    assert not list(s.customer.owner.glob("managed-outputs/*/handoff"))


def test_only_current_scoped_assignment_is_visible_to_authorized_actor(tmp_path):
    s = AssignmentSetup(tmp_path)
    one, assignment = packet_for(s)
    proposal = binding(assignment["proposals"][0])
    first = s.service.save(one, actor=s.owner)
    earlier = s.assignments.owner_recovery(**proposal, actor=s.workflow_owner)
    two, _ = packet_for(s, "UTHIRD", "replacement-two")
    second = s.service.save(two, actor=s.owner)
    current = s.assignments.owner_recovery(**proposal, actor=s.workflow_owner)
    assert earlier["settings_generation"] == first["revision"]
    assert current["settings_generation"] == second["revision"]
    assert current["recovery_ref"] != earlier["recovery_ref"]
    assert (
        s.assignments.owner_recovery(
            **dict(proposal, run_id="run-" + "f" * 32), actor=s.workflow_owner
        )
        is None
    )
    for key in ("run_revision", "artifact_sha"):
        with pytest.raises(SettingsError, match="review_recovery_record_unavailable"):
            s.assignments.owner_recovery(
                **dict(proposal, **{key: "f" * 64}), actor=s.workflow_owner
            )
    for actor in (s.reviewer, s.successor, "UTHIRD", "UFOREIGN"):
        with pytest.raises(SettingsError):
            s.assignments.owner_recovery(**proposal, actor=actor)
    authority = read(s.authority)
    save(s.authority, dict(authority, workflow_owner="new-generated-owner"))
    with pytest.raises(SettingsError):
        s.assignments.owner_recovery(**proposal, actor=s.workflow_owner)
    with pytest.raises(SettingsError, match="current_settings_authority_required"):
        s.assignments.owner_recovery(**proposal, actor=s.operator)


def test_real_signed_renewal_preserves_current_owner_visible_reauthorization(tmp_path):
    s = AssignmentSetup(tmp_path)
    packet, assignment = packet_for(s)
    first = s.service.save(packet, actor=s.owner)
    proposal = binding(assignment["proposals"][0])
    old = s.assignments.owner_recovery(**proposal, actor=s.workflow_owner)
    authority = read(s.authority)
    authority["schema_version"] = LEGACY_AUTHORITY
    del authority["workflow_owner_public_key_file"]
    del authority["workflow_owner_public_key_sha256"]
    save(s.authority, authority)
    policy = deepcopy(first["policy"])
    policy["mandate"].update(mandate_id="current-recovery-renewal", max_runs=3)
    renewed = s.apply(policy, action="renew")
    current = s.assignments.owner_recovery(**proposal, actor=s.workflow_owner)
    assert current["settings_generation"] == renewed["revision"]
    assert current["recovery_ref"] != old["recovery_ref"]
    assert current["state"] == "review_on_hold"
    assert current["reason"] == "review_reauthorization_required"
    assert current["next_action"] == "reauthorize_named_reviewer"
    assert s.assignments.owner_recovery(**proposal, actor=s.owner) == current


@pytest.mark.parametrize("interrupted", [False, True])
def test_matched_durable_terminal_readback_suppresses_item_but_wrong_proof_does_not(
    tmp_path, monkeypatch, interrupted
):
    s = AssignmentSetup(tmp_path)
    case, wire, envelope = inspected_case(s, tmp_path)
    if interrupted:

        def stop_after_receipt(_journal, request_id, state, *_args):
            assert request_id == wire["request_id"] and state == "approved"
            raise OSError("generated stop before provider journal transition")

        with monkeypatch.context() as local:
            local.setattr(zapier_approval.ReconciliationJournal, "transition", stop_after_receipt)
            with pytest.raises(OSError, match="provider journal transition"):
                case.bridge.consume(envelope)
    else:
        assert case.bridge.consume(envelope)["state"] == "approved"
    proposal = binding(case.binding)
    path = s.customer.owner / "managed-outputs" / proposal["run_id"] / "release-decision.json"
    durable = path.read_bytes()
    # Hold aside the exact generated receipt while saving the current generation,
    # then simulate owner-local recovery of those original terminal bytes.
    saved = path.with_name("generated-terminal-backup.json")
    path.rename(saved)
    packet, _ = packet_for(s)
    s.service.save(packet, actor=s.owner)
    item = s.assignments.owner_recovery(**proposal, actor=s.workflow_owner)
    assert item is not None
    original = read(saved)
    for changes in (
        {"run_revision": "f" * 64},
        {"presentation_id": "generated-missing-exposure"},
        {"decision": "transport_accepted"},
    ):
        save(path, original | changes)
        assert s.assignments.owner_recovery(**proposal, actor=s.workflow_owner) == item
    path.unlink()
    saved.rename(path)
    assert s.assignments.owner_recovery(**proposal, actor=s.workflow_owner) is None
    assert path.read_bytes() == durable and read(path)["reviewer"] == s.reviewer
    with sqlite3.connect(s.customer.owner / "slack-pilot.sqlite3") as db:
        db.execute(
            "UPDATE integrity SET seal=? WHERE kind='presentation' AND id=?",
            ("f" * 64, original["presentation_id"]),
        )
    assert s.assignments.owner_recovery(**proposal, actor=s.workflow_owner) == item


@pytest.mark.parametrize("opposite_reservation", [False, True])
def test_exact_shaped_approval_without_matching_authenticated_terminal_keeps_item(
    tmp_path, monkeypatch, opposite_reservation
):
    s = AssignmentSetup(tmp_path)
    case, wire, envelope = inspected_case(s, tmp_path)
    request_id = wire["request_id"]
    archived = read(case.bridge._path(request_id, "request"))["request"]
    if opposite_reservation:
        declined = resign(
            case, envelope, {"status": "Declined", "event_id": request_id + ".declined"}
        )

        def decline_did_not_commit(*_args, **_kwargs):
            raise OSError("generated stop after denied terminal reservation")

        with monkeypatch.context() as local:
            local.setattr(release_gate, "record_release", decline_did_not_commit)
            with pytest.raises(OSError, match="terminal reservation"):
                case.bridge.consume(declined)
    assert case.bridge._path(request_id, "event").exists() is opposite_reservation
    packet, assignment = packet_for(s)
    s.service.save(packet, actor=s.owner)
    proposal = binding(assignment["proposals"][0])
    item = s.assignments.owner_recovery(**proposal, actor=s.workflow_owner)
    assert item is not None
    presentation = archived["inspection_ref"]
    unconfirmed = {
        "organization": s.customer.org,
        **proposal,
        **s.current["policy"]["agreement"]["release_review"],
        "decision": "approved",
        "provider": "zapier_slack",
        "inspection_ref": presentation,
        "presentation_id": presentation,
    }
    path = s.customer.owner / "managed-outputs" / proposal["run_id"] / "release-decision.json"
    assert not path.exists()
    save(path, unconfirmed)
    assert s.assignments.owner_recovery(**proposal, actor=s.workflow_owner) == item
