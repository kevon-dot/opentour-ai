"""Actual dual-signature settings changes and atomic frozen-proposal fences."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from unittest.mock import patch

import pytest
from opentour_cell.pilot import autonomous_cycle, repair_bundle, source_intake
from opentour_cell.pilot import reviewer_assignment as assignments
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.owner_settings import OwnerSettings, SettingsError
from test_autonomous_cycle import read, save
from test_owner_settings import Setup
from test_reviewer_assignment import AssignmentSetup


def policy_for(current, reviewer, mandate):
    p = deepcopy(current["policy"])
    p["agreement"]["release_review"]["reviewer"] = reviewer
    p["mandate"].update(
        reviewer=reviewer, mandate_id=mandate, agreement_revision=digest(p["agreement"])
    )
    return p


def packet_for(s, successor=None, mandate="replacement-one"):
    successor = successor or s.successor
    claim = s.assignments.preview(successor, actor=s.workflow_owner)["claim"]
    assignment = s.assignments.assign(s.owner_packet(claim), actor=s.workflow_owner)
    current = s.service.inspect(actor=s.owner)["current"]
    preview = s.service.preview(
        policy_for(current, successor, mandate),
        actor=s.owner,
        action="replace_reviewer",
        assignment_revision=assignment["assignment_revision"],
    )
    return s.sign(preview["claim"]), assignment


def records(s):
    with s.service.ledger._db(write=False) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_replacements'"
        ).fetchone():
            return [], []
        return (
            [json.loads(r[0]) for r in db.execute("SELECT document FROM reviewer_replacements")],
            [dict(r) for r in db.execute("SELECT * FROM reviewer_reconciliation")],
        )


@pytest.mark.parametrize("dual_role", [False, True])
def test_real_dual_signature_save_records_one_fence_and_keeps_old_execution_denied(
    tmp_path, dual_role
):
    s = AssignmentSetup(tmp_path, dual_role=dual_role)
    packet, assignment = packet_for(s)
    before = s.protocol()
    first = s.service.save(packet, actor=s.owner)
    assert first == s.service.save(packet, actor=s.owner)
    assert first["reviewer"] == s.successor
    assert first["actor"] == s.owner and first["workflow_owner"] == s.workflow_owner
    assert first["reviewer_grant_revision"] != s.current["reviewer_grant_revision"]
    assert first["reviewer_replacement"]["assignment_revision"] == assignment["assignment_revision"]
    fences, outbox = records(s)
    assert len(fences) == len(outbox) == 1
    assert fences[0]["original_reviewer"] == s.reviewer
    assert fences[0]["reviewer"] == s.successor
    assert fences[0]["generation"] == first["revision"]
    assert outbox[0]["state"] == "pending"
    assert s.service.inspect(actor=s.owner)["current"] == first
    assert s.customer.ledger.inspect(s.current["mandate_id"])["withdrawn"] is True
    proposal = assignment["proposals"][0]
    with pytest.raises(ValueError):
        source_intake.check_current_intake(
            s.customer.owner, s.customer.org, s.customer.eng, proposal["intake_revision"]
        )
    # No source content is read by assignment/replacement, and no vote/export is fabricated.
    assert not any(r["method"] == "resources/read" for r in s.protocol()[len(before) :])
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))
    with s.service.store.engagement_guard(engagement_id=s.customer.eng):
        current = assignments.current_review_authority(
            s.customer.owner,
            s.customer.org,
            s.customer.eng,
            proposal["run_id"],
            proposal["run_revision"],
            proposal["artifact_sha"],
        )
    assert current["generation"] == first["revision"]
    assert current["reviewer"] == s.successor
    assert current["release_review"]["reviewer"] == s.successor
    assert current["reviewer_grant_revision"] == first["reviewer_grant_revision"]
    with pytest.raises(SettingsError, match="review_replacement_scope_mismatch"):
        assignments.current_review_authority(
            s.customer.owner,
            s.customer.org,
            s.customer.eng,
            proposal["run_id"],
            proposal["run_revision"],
            "0" * 64,
        )


def test_replacement_of_inherited_proposal_keeps_every_generation(tmp_path):
    s = AssignmentSetup(tmp_path)
    first_packet, _ = packet_for(s)
    first = s.service.save(first_packet, actor=s.owner)
    second_packet, second_assignment = packet_for(s, "UTHIRD", "replacement-two")
    assert len(second_assignment["proposals"]) == 1
    assert second_assignment["proposals"][0]["prior_review_generation"] == first["revision"]
    second = s.service.save(second_packet, actor=s.owner)
    fences, outbox = records(s)
    assert len(fences) == len(outbox) == 2
    assert [r["reviewer"] for r in fences] == [s.successor, "UTHIRD"]
    assert all(r["original_reviewer"] == s.reviewer for r in fences)
    assert all(r["run_id"] == s.outcome["run_id"] for r in fences)
    assert second["reviewer_replacement"]["generation"] != first["revision"]
    proposal = second_assignment["proposals"][0]
    current = assignments.current_review_authority(
        s.customer.owner,
        s.customer.org,
        s.customer.eng,
        proposal["run_id"],
        proposal["run_revision"],
        proposal["artifact_sha"],
    )
    assert current["generation"] == second["revision"] and current["reviewer"] == "UTHIRD"
    # Returning to the original identity still requires inspection of a new generation.
    back_packet, _ = packet_for(s, s.reviewer, "replacement-return")
    back = s.service.save(back_packet, actor=s.owner)
    assert back["reviewer"] == s.reviewer
    assert back["revision"] not in {first["revision"], second["revision"], s.current["revision"]}


def test_failed_fence_transaction_preserves_previous_settings_and_mandate(tmp_path):
    s = AssignmentSetup(tmp_path)
    packet, _ = packet_for(s)
    original = assignments.ReviewerAssignments.record_replacement

    def fail_after_fence(self, db, assignment, receipt):
        original(self, db, assignment, receipt)
        raise RuntimeError("generated crash before commit")

    with patch.object(assignments.ReviewerAssignments, "record_replacement", fail_after_fence):
        with pytest.raises(RuntimeError, match="before commit"):
            s.service.save(packet, actor=s.owner)
    assert s.service.inspect(actor=s.owner)["current"] == s.current
    assert s.customer.ledger.inspect(s.current["mandate_id"])["withdrawn"] is False
    assert records(s) == ([], [])
    assert s.service.save(packet, actor=s.owner)["reviewer"] == s.successor


def test_two_competing_assignments_cannot_create_two_current_replacements(tmp_path):
    s = AssignmentSetup(tmp_path)
    one, _ = packet_for(s)
    two, _ = packet_for(s, "UTHIRD", "replacement-two")

    def apply(packet):
        try:
            return s.service.save(packet, actor=s.owner)
        except SettingsError:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(apply, [one, two]))
    assert sum(r is not None for r in results) == 1
    assert len(records(s)[0]) == len(records(s)[1]) == 1


def test_alias_pem_encoding_cannot_make_same_key_two_roles(tmp_path):
    s = AssignmentSetup(tmp_path, run=False)
    alias = repair_bundle._crypto(
        ["rsa", "-pubin", "-RSAPublicKey_out"], s.public.read_bytes(), "generated_alias_failed"
    )
    assert alias != s.public.read_bytes()
    s.owner_public.write_bytes(alias)
    a = read(s.authority)
    save(s.authority, dict(a, workflow_owner_public_key_sha256=repair_bundle._sha(alias)))
    with pytest.raises(SettingsError, match="distinct_role_signing_keys_required"):
        s.assignments.preview(s.successor, actor=s.workflow_owner)


def test_v1_run_is_explicitly_bound_after_v2_same_reviewer_renewal(tmp_path):
    s = Setup(tmp_path)
    first = s.apply()
    config = s.service.worker_config(first, actor=s.operator)["config"]
    outcome = autonomous_cycle.cycle_once(s.customer.owner, s.customer.org, config)
    assert outcome["state"] == "pending_slack_review"
    a = read(s.authority)
    save(
        s.authority,
        dict(
            a,
            schema_version=assignments.AUTHORITY,
            reviewers=[s.reviewer, "USUCCESSOR"],
            workflow_owner_public_key_file=a["public_key_file"],
            workflow_owner_public_key_sha256=a["public_key_sha256"],
        ),
    )
    service = assignments.ReviewerAssignments(s.service)
    with pytest.raises(SettingsError, match="assignment_authority_epoch_changed"):
        service.preview("USUCCESSOR", actor=s.owner)
    p = deepcopy(s.policy)
    p["mandate"]["mandate_id"] = "explicit-v2-settings"
    current = s.apply(p)
    claim = service.preview("USUCCESSOR", actor=s.owner)["claim"]
    assert len(claim["proposals"]) == 1
    assert claim["proposals"][0]["run_id"] == outcome["run_id"]
    assignment = service.assign(s.sign(claim), actor=s.owner)
    p = policy_for(current, "USUCCESSOR", "explicit-v2-successor")
    preview = s.service.preview(
        p,
        actor=s.owner,
        action="replace_reviewer",
        assignment_revision=assignment["assignment_revision"],
    )
    saved = s.service.save(s.sign(preview["claim"]), actor=s.owner)
    assert len(saved["reviewer_replacement"]["proposals"]) == 1
    assert saved["reviewer"] == "USUCCESSOR"


def test_same_reviewer_renewal_preserves_choice_with_a_fresh_generation(tmp_path):
    from test_reviewer_replacement_callbacks import inspected_case

    s = AssignmentSetup(tmp_path)
    _, request, _ = inspected_case(s, tmp_path)
    packet, assignment = packet_for(s)
    first = s.service.save(packet, actor=s.owner)
    policy = deepcopy(first["policy"])
    policy["mandate"].update(mandate_id="same-reviewer-renewal", max_runs=3)
    renewed = s.apply(policy, action="renew")
    proposal = assignment["proposals"][0]
    grant = assignments.current_review_authority(
        s.customer.owner,
        s.customer.org,
        s.customer.eng,
        proposal["run_id"],
        proposal["run_revision"],
        proposal["artifact_sha"],
    )
    assert grant["reviewer"] == first["reviewer"]
    assert grant["reviewer_grant_revision"] == first["reviewer_grant_revision"]
    assert grant["generation"] == renewed["revision"] != first["revision"]
    assert grant["assignment_revision"] == first["reviewer_replacement"]["assignment_revision"]
    assert len(records(s)[0]) == len(records(s)[1]) == 2
    assert records(s)[0][-1]["expired_request_ids"] == [request["request_id"]]


def test_private_projection_ack_is_idempotent_and_grants_no_remote_proof(tmp_path):
    s = AssignmentSetup(tmp_path)
    packet, _ = packet_for(s)
    s.service.save(packet, actor=s.owner)
    pending = s.assignments.pending_reconciliations(actor=s.operator, limit=1)
    assert pending["pending_count"] == len(pending["records"]) == 1
    row = pending["records"][0]
    with pytest.raises(SettingsError, match="reconciliation_document_changed"):
        s.assignments.acknowledge_reconciliation(row["id"], "0" * 64, actor=s.operator)
    assert s.assignments.pending_reconciliations(actor=s.operator)["pending_count"] == 1
    receipt = s.assignments.acknowledge_reconciliation(
        row["id"], row["document_sha256"], actor=s.operator
    )
    assert receipt == assignments.ReviewerAssignments(s.service).acknowledge_reconciliation(
        row["id"], row["document_sha256"], actor=s.operator
    )
    assert receipt["state"] == "projected"
    assert receipt["provider_outcome"] == receipt["owner_notification"] == "not_qualified"
    assert s.assignments.pending_reconciliations(actor=s.operator)["pending_count"] == 0
    with pytest.raises(SettingsError, match="reconciliation_operator_required"):
        s.assignments.pending_reconciliations(actor=s.successor)
    forged = s.customer.owner / "forged-role-authority.json"
    save(forged, dict(read(s.authority), operator="forged-operator"))
    untrusted = assignments.ReviewerAssignments(
        OwnerSettings(s.customer.owner, s.customer.org, s.customer.eng, authority_file=forged)
    )
    with pytest.raises(SettingsError, match="current_settings_authority_required"):
        untrusted.pending_reconciliations(actor="forged-operator")


def test_v1_downgrade_renewal_serializes_and_keeps_existing_proposal_held(tmp_path):
    from opentour_cell.pilot.owner_settings import AUTHORITY as V1_AUTHORITY
    from test_reviewer_replacement_callbacks import inspected_case

    s = AssignmentSetup(tmp_path)
    _, request, _ = inspected_case(s, tmp_path)
    packet, assignment = packet_for(s)
    first = s.service.save(packet, actor=s.owner)
    authority = read(s.authority)
    authority["schema_version"] = V1_AUTHORITY
    del authority["workflow_owner_public_key_file"]
    del authority["workflow_owner_public_key_sha256"]
    save(s.authority, authority)
    policy = deepcopy(first["policy"])
    policy["mandate"].update(mandate_id="v1-downgrade-renewal", max_runs=3)
    packet = s.sign(s.preview(policy, action="renew")["claim"])
    attempted, entered = Event(), Event()
    original = s.service._head

    def observe(db):
        entered.set()
        return original(db)

    def renew():
        attempted.set()
        return s.service.save(packet, actor=s.owner)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with patch.object(s.service, "_head", observe), s.assignments.serialized():
            saving = pool.submit(renew)
            assert attempted.wait(1)
            # A changing v1 save cannot acquire the engagement writer or inspect
            # the old request inventory while the approval writer is in flight.
            assert not entered.wait(0.1)
        renewed = saving.result()
    document = records(s)[0][-1]
    assert document["generation"] == renewed["revision"]
    assert document["state"] == "review_reauthorization_required"
    assert document["expired_request_ids"] == [request["request_id"]]
    proposal = assignment["proposals"][0]
    with pytest.raises(SettingsError, match="review_reauthorization_required"):
        assignments.current_review_authority(
            s.customer.owner,
            s.customer.org,
            s.customer.eng,
            proposal["run_id"],
            proposal["run_revision"],
            proposal["artifact_sha"],
        )
