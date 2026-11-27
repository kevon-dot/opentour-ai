"""Generated callbacks through real inspection/release and shared durable ordering."""

from __future__ import annotations

import json
import secrets
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event
from unittest.mock import patch

import pytest
from opentour_cell.pilot import autonomous_cycle, release_gate, repair_workflow, zapier_approval
from opentour_cell.pilot import reviewer_assignment as assignments
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.owner_settings import SettingsError
from opentour_cell.pilot.slack_adapter import hook_fingerprint
from test_autonomous_cycle import read, save
from test_reviewer_assignment import AssignmentSetup
from test_reviewer_replacement import packet_for, records
from test_reviewer_workflow import buttons, restart, screen
from test_slack_adapter import ACTOR, Harness
from test_zapier_approval import HOOK, Case, deliver, resign


def inspected_case(s, tmp_path):
    h = Harness(tmp_path / "separate-generated-ui", organization=s.customer.org)
    h.path = s.customer.owner / "slack-config.json"
    policy = s.current["policy"]["agreement"]["release_review"]
    evidence = repair_workflow.managed_review_evidence(
        s.customer.owner,
        s.customer.org,
        s.outcome["run_id"],
        policy["audience"],
        policy["team"],
        policy["channel"],
    )
    role = deepcopy(h.config["users"][ACTOR])
    role["datasets"] = [evidence["dataset_id"]]
    h.config.update(
        directory=str(s.customer.owner),
        reviewer=s.reviewer,
        recipient=policy["audience"],
        team=policy["team"],
        channel=policy["channel"],
        users={s.reviewer: role},
        zapier=False,
        approval_provider="zapier_slack",
    )
    h.store = s.service.store
    h.save()
    restart(h)
    key = secrets.token_hex(32)
    plugin = {
        "schema_version": zapier_approval.POLICY,
        "enabled": True,
        "organization": s.customer.org,
        "engagement": s.customer.eng,
        **policy,
        "zap_id": "generated-zap",
        "zap_version": "generated-version-one",
        "approver_mode": "verified_actor",
        "setup_attestation": "att-abcdef0123456789",
        "key_file": "zapier-signer.json",
        "expires_at": int(time.time()) + 600,
        "hook_fingerprint": hook_fingerprint(HOOK),
    }
    config = s.customer.owner / "zapier-approval.json"
    save(config, plugin)
    save(s.customer.owner / plugin["key_file"], {"secret_hex": key})
    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    case = Case(
        h,
        read(s.customer.owner / ("cycle-plan-" + s.outcome["run_id"] + ".json")),
        {
            "run_id": s.outcome["run_id"],
            "run_revision": s.outcome["result_revision"],
            "artifact_sha": evidence["audit"]["candidate_digest"],
            "inspection_ref": revision,
        },
        zapier_approval.ApprovalBridge(s.customer.owner, s.customer.org, config),
        plugin,
        key,
        view,
        revision,
    )
    request = case.inspect()
    deliver(case, request)
    event = {
        "schema_version": zapier_approval.EVENT,
        "request_id": request["request_id"],
        "nonce": request["nonce"],
        "zap_id": plugin["zap_id"],
        "zap_version": plugin["zap_version"],
        "event_id": request["request_id"] + ".approved",
        "decided_at": int(time.time()),
        "status": "Approved",
        "actor_id": s.reviewer,
    }
    # Generated authenticated return. Actual Node action has its separate OPE-414 evidence.
    return case, request, resign(case, {"event": event}, {})


def test_real_old_callback_after_saved_replacement_is_denied(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, request, envelope = inspected_case(s, tmp_path)
    packet, _ = packet_for(s)
    receipt = s.service.save(packet, actor=s.owner)
    assert receipt["reviewer"] == s.successor
    with pytest.raises(ValueError):
        case.bridge.consume(envelope)
    with pytest.raises(ValueError):
        case.authorize()
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))
    assert len(records(s)[0]) == 1
    assert request["request_id"].startswith("za-")
    assert records(s)[0][0]["expired_request_ids"] == [request["request_id"]]
    assert records(s)[0][0]["remote_reconciliation_required"] is False


def test_replacement_commit_wins_before_old_confirm(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, _, envelope = inspected_case(s, tmp_path)
    packet, _ = packet_for(s)
    entered, release, attempted = Event(), Event(), Event()
    original = assignments.ReviewerAssignments.record_replacement

    def fence(self, db, assignment, receipt):
        value = original(self, db, assignment, receipt)
        entered.set()
        assert release.wait(5)
        return value

    def old_confirm():
        attempted.set()
        with pytest.raises(ValueError):
            case.bridge.consume(envelope)

    with (
        patch.object(assignments.ReviewerAssignments, "record_replacement", fence),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        saving = pool.submit(s.service.save, packet, actor=s.owner)
        assert entered.wait(10)
        confirming = pool.submit(old_confirm)
        assert attempted.wait(1)
        release.set()
        assert saving.result()["reviewer"] == s.successor
        confirming.result()
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))


def test_old_confirm_commits_before_replacement_preserves_original_actor(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, _, envelope = inspected_case(s, tmp_path)
    packet, _ = packet_for(s)
    entered, release, attempted = Event(), Event(), Event()
    original = release_gate._record

    def record(path, value):
        if path.name == "release-decision.json":
            entered.set()
            assert release.wait(5)
        return original(path, value)

    def replacing():
        attempted.set()
        with pytest.raises(SettingsError, match="assignment_revision_conflict"):
            s.service.save(packet, actor=s.owner)

    with patch.object(release_gate, "_record", record), ThreadPoolExecutor(max_workers=2) as pool:
        confirming = pool.submit(case.bridge.consume, envelope)
        assert entered.wait(10)
        saving = pool.submit(replacing)
        assert attempted.wait(1)
        release.set()
        assert confirming.result()["state"] == "approved"
        saving.result()
    path = s.customer.owner / "managed-outputs" / s.outcome["run_id"] / "release-decision.json"
    assert read(path)["reviewer"] == s.reviewer
    assert s.service.inspect(actor=s.owner)["current"] == s.current
    assert records(s) == ([], [])


def test_two_actual_sealed_requests_preserve_unknown_remote_hold(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, first, envelope = inspected_case(s, tmp_path)
    prepare_next_zap_revision(case, envelope)
    second = case.bridge.issue(**case.binding)
    assert first["request_id"] != second["request_id"]
    deliveries = []

    def lost_ack(url, payload):
        deliveries.append(payload["request_id"])
        raise OSError("generated lost acknowledgment")

    result = case.bridge.flush(second["request_id"], HOOK, lost_ack)
    assert result["transport"] == "uncertain"
    packet, _ = packet_for(s)
    receipt = s.service.save(packet, actor=s.owner)
    row = records(s)[0][0]
    assert row["expired_request_ids"] == sorted([first["request_id"], second["request_id"]])
    for request_id in row["expired_request_ids"]:
        path = case.bridge._path(request_id, "request")
        assert row["request_fingerprints"][request_id] == digest(read(path))
    assert row["remote_reconciliation_required"] is True
    assert s.service.save(packet, actor=s.owner) == receipt
    assert len(records(s)[1]) == 1
    assert deliveries == [second["request_id"]]
    with pytest.raises(ValueError):
        case.bridge.flush(second["request_id"], HOOK, lost_ack)
    assert deliveries == [second["request_id"]]


def prepare_next_zap_revision(case, envelope, *, inspect=True):
    # A signed pending readback settles the first dispatch without approving it.
    # The normal cutover fences that request before a fresh sealed inspection.
    assert (
        case.bridge.consume(resign(case, envelope, {"status": "Pending", "actor_id": None}))[
            "state"
        ]
        == "pending"
    )
    assert case.bridge.prepare_cutover()["counts"] == {"superseded": 1}
    case.policy["zap_version"] = "generated-version-two"
    save(case.bridge.config, case.policy)
    assert case.bridge.complete_cutover()["state"] == "ready"
    case.view = case.h.launch()
    case.revision = buttons(screen(case.h, case.view))["ot_evidence"]
    case.binding["inspection_ref"] = case.revision
    if inspect:
        case.h.action(case.view, "ot_evidence", case.revision)


def test_completed_history_under_changed_source_does_not_block_current_assignment(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, _, envelope = inspected_case(s, tmp_path)
    assert case.bridge.consume(envelope)["state"] == "approved"
    old_run = s.outcome["run_id"]
    decision_path = s.customer.owner / "managed-outputs" / old_run / "release-decision.json"
    historical = decision_path.read_bytes()
    reader = s.customer.real_reader
    reader.cfg["scope"]["as_of"] += 1
    # The next real worker must use a fresh protected cohort, not reuse the
    # historical decision's held-out keys or examples.
    for row in reader.rows["trace"]:
        if row["decision_at"] >= 200:
            key = row["event_id"]
            row.update(event_id="next-" + key, group_id="next-family-" + key)
            row["features"]["description"] = "Fresh generated decision for " + key
    for row in reader.rows["correction"]:
        if row["outcome_at"] >= 200:
            key = row["event_id"]
            row.update(event_id="next-" + key, outcome_id="next-adjudication-" + key)
    reader.versions = {"trace": "source-v2", "correction": "source-v2"}
    reader.rebuild()
    policy = deepcopy(s.current["policy"])
    policy["agreement"]["connection_revision"] = digest(reader.cfg)
    policy["mandate"].update(
        mandate_id="changed-source-current",
        agreement_revision=digest(policy["agreement"]),
        max_runs=3,
        max_total_rows=100,
    )
    current = s.apply(policy)
    assert current["binding"] != s.current["binding"]
    config = s.service.worker_config(current, actor=s.operator)["config"]
    outcome = autonomous_cycle.cycle_once(s.customer.owner, s.customer.org, config)
    assert outcome["state"] == "pending_slack_review", json.dumps(outcome)
    claim = s.assignments.preview(s.successor, actor=s.workflow_owner)["claim"]
    assert [p["run_id"] for p in claim["proposals"]] == [outcome["run_id"]]
    assignment = s.assignments.assign(s.owner_packet(claim), actor=s.workflow_owner)
    assert assignment["proposals"] == claim["proposals"]
    assert decision_path.read_bytes() == historical


def test_v2_ordinary_renew_waits_for_serialized_actual_issue(tmp_path):
    s = AssignmentSetup(tmp_path)
    case, first, envelope = inspected_case(s, tmp_path)
    prepare_next_zap_revision(case, envelope, inspect=False)
    policy = deepcopy(s.current["policy"])
    policy["mandate"].update(mandate_id="ordinary-renew-after-issue", max_runs=3)
    packet = s.sign(s.preview(policy, action="renew")["claim"])
    entered, release, attempted = Event(), Event(), Event()
    original = zapier_approval._record
    sealed = []

    def record(path, value):
        if path.name.endswith("-request.json"):
            sealed.append(value["request"])
            entered.set()
            assert release.wait(10)
        return original(path, value)

    def renew():
        attempted.set()
        return s.service.save(packet, actor=s.owner)

    with (
        patch.object(zapier_approval, "_record", record),
        ThreadPoolExecutor(max_workers=2) as pool,
    ):
        # Fresh evidence UI invokes the real issue entry, which already holds
        # the shared approval lock. Do not issue once before installing the
        # barrier or acquire a second, non-reentrant outer flock.
        issuing = pool.submit(case.h.action, case.view, "ot_evidence", case.revision)
        if not entered.wait(10):
            issuing.result(timeout=1)
            pytest.fail("issue did not reach the durable request boundary")
        saving = pool.submit(renew)
        assert attempted.wait(1)
        assert not saving.done()
        release.set()
        issuing.result()
        renewed = saving.result()
    assert len(sealed) == 1
    second = sealed[0]
    assert first["request_id"] != second["request_id"]
    assert renewed["action"] == "renew"
    assert len(list(s.customer.owner.glob("zapier-approval-za-*-request.json"))) == 2
    assert read(case.bridge._path(second["request_id"], "request"))["request"] == second
