"""Connect an actual private Slack decision to the customer-local release outbox."""

import json
import sqlite3
import time
from contextlib import closing
from dataclasses import replace

import pytest
from opentour_cell.pilot import approval_inbox, release_gate, source_intake
from opentour_cell.pilot.release_authority import (
    HandoffPermission,
    RecordedReleaseAuthority,
    approval_ref,
)
from opentour_cell.pilot.release_outbox import ReleaseOutbox, ReleaseOutboxError, ReleaseSelection
from opentour_cell.pilot.training import LocalTrainingStore
from test_approval_inbox import callback, opt_in, stage
from test_release_outbox import Recipient
from test_reviewer_workflow import buttons, screen
from test_slack_adapter import ACTOR


class DestinationPermissions:
    def __init__(self, permit):
        self.current = permit

    def resolve(self, workflow_ref, actor, action):
        if self.current is None or self.current.workflow_ref != workflow_ref:
            return None
        if actor == "dispatcher" and action in {"reserve", "claim", "reconcile"}:
            return self.current
        if actor == "inspector" and action == "inspect":
            return self.current
        return None


def approve_fixture(tmp_path, action):
    h, plan, run, pointer, audit = stage(tmp_path)
    opt_in(h)
    assert approval_inbox.flush_approvals(h.app) == 1
    h.send("interactive", callback(h, pointer))
    view = list(h.slack.screens)[-1]
    h.action(view, action, buttons(screen(h, view))[action], ACTOR)
    args = (
        h.path.parent,
        h.store.organization,
        run,
        audit["run_revision"],
        audit["candidate_digest"],
    )
    receipt = release_gate.inspect_recorded_release(*args)
    assert receipt
    intake = source_intake.check_current_intake(
        h.path.parent, h.store.organization, h.store.engagement_id, plan["intake_revision"]
    )
    permit = HandoffPermission(
        h.store.organization,
        h.store.engagement_id,
        intake["workflow"],
        run,
        audit["run_revision"],
        audit["candidate_digest"],
        plan["audience"],
        "generated-recipient-target",
        "generated-custodian-grant",
        int(time.time()) + 300,
    )
    permissions = DestinationPermissions(permit)
    authority = RecordedReleaseAuthority(h.path.parent, h.store.organization, permissions)
    selected = ReleaseSelection(intake["workflow"], run, approval_ref(receipt))
    return h, args, selected, receipt, permissions, ReleaseOutbox(h.store, authority)


def test_actual_accepted_decision_retains_one_release_and_retry_time(tmp_path):
    h, args, selected, original, permissions, outbox = approve_fixture(
        tmp_path, "ot_release_approve"
    )
    assert type(original["recorded_at"]) is int and original["recorded_at"] <= time.time()
    assert original["decision_seal"].startswith("release_")
    same = release_gate.record_release(
        *args[:2],
        **{
            key: original[key]
            for key in (
                "run_id",
                "run_revision",
                "artifact_sha",
                "reviewer",
                "team",
                "channel",
                "audience",
                "decision",
                "inspection_ref",
                "provider",
            )
        },
    )
    assert same == original
    assert outbox.status(selected, "inspector") is None
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(selected, "inspector", "not-a-dispatcher")
    receipts = [
        outbox.reserve(selected, "dispatcher", f"approved-{attempt}") for attempt in range(3)
    ]
    assert receipts == [receipts[0]] * 3
    assert receipts[0].stage == "queued" and receipts[0].receiver_receipt_ref is None
    assert receipts[0].destination_ref == permissions.current.destination_ref
    clock = int(time.time())
    outbox.clock = lambda: clock
    receiver = Recipient(tmp_path / "receiver.sqlite3", observed_at=clock, received_at=clock)
    claim = outbox.claim(selected, "dispatcher", receiver)
    assert claim.dispatch and claim.status.stage == "delivery_unconfirmed"
    with h.store._db() as db:
        grant = outbox.authority.resolve(db, selected, "dispatcher", "claim")
    assert grant is not None
    with pytest.raises(TimeoutError):
        receiver.send_then_lose_reply(grant)
    restarted = ReleaseOutbox(
        LocalTrainingStore(h.store.path, h.store.organization, existing_only=True),
        outbox.authority,
        lambda: clock,
    )
    assert restarted.status(selected, "inspector") == claim.status
    assert restarted.status(selected, "inspector") == claim.status
    assert receiver.calls == [grant.reference]
    assert not restarted.claim(selected, "dispatcher", receiver).dispatch
    delivered = restarted.reconcile(selected, "dispatcher", receiver)
    assert delivered.stage == "delivered" and delivered.receiver_receipt_ref == "receiver-delivery"
    assert receiver.calls == [grant.reference] * 2
    with closing(sqlite3.connect(receiver.path)) as db:
        assert db.execute("SELECT tenant,destination,artifact FROM deliveries").fetchall() == [
            (grant.tenant, grant.destination_ref, grant.artifact_sha256)
        ]
    with h.store._db() as db:
        assert db.execute("SELECT count(*) FROM release_outbox").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM release_operation_keys").fetchone()[0] == 3
    permissions.current = replace(permissions.current, recipient="UFOREIGN")
    with pytest.raises(ReleaseOutboxError):
        outbox.status(selected, "inspector")
    permissions.current = None
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(selected, "dispatcher", "withdrawn")


@pytest.mark.parametrize("during_source_read", [False, True])
def test_destination_permission_lost_at_commit_leaves_no_reservation(
    tmp_path, monkeypatch, during_source_read
):
    h, _, approval, _, permissions, outbox = approve_fixture(tmp_path, "ot_release_approve")
    resolve = permissions.resolve

    def withdraw_after_resolution(*args):
        grant = resolve(*args)
        permissions.current = None
        return grant

    source_read = source_intake.check_current_intake

    def read_then_withdraw(*args, **kwargs):
        evidence = source_read(*args, **kwargs)
        permissions.current = None
        return evidence

    if during_source_read:
        monkeypatch.setattr(source_intake, "check_current_intake", read_then_withdraw)
    else:
        monkeypatch.setattr(permissions, "resolve", withdraw_after_resolution)
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(approval, "dispatcher", "withdrawn-before-commit")
    with h.store._db() as db:
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='release_outbox'").fetchone() is None
        )


def test_declined_and_unstamped_legacy_receipts_never_reserve_dispatch(tmp_path):
    _, _, declined, receipt, _, negative = approve_fixture(
        tmp_path / "decline", "ot_release_changes"
    )
    assert receipt["decision"] == "changes_requested"
    with pytest.raises(ReleaseOutboxError):
        negative.reserve(declined, "dispatcher", "no-delivery")

    h, args, approved, original, _, outbox = approve_fixture(
        tmp_path / "legacy", "ot_release_approve"
    )
    legacy = {
        key: value for key, value in original.items() if key not in {"recorded_at", "decision_seal"}
    }
    path = h.path.parent / "managed-outputs" / approved.proposal_ref / "release-decision.json"
    path.write_text(json.dumps(original | {"recorded_at": 1}))
    with pytest.raises(ValueError, match="release_record_time_invalid"):
        release_gate.authorize_release(*args)
    path.write_text(json.dumps(legacy))
    assert release_gate.authorize_release(*args) == legacy
    selected = replace(approved, approval_ref=approval_ref(legacy))
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(selected, "dispatcher", "missing-time")
    with h.store._db() as db:
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='release_outbox'").fetchone() is None
        )


@pytest.mark.parametrize("strip_evidence", [False, True])
def test_changed_decline_cannot_stage_even_with_recomputed_reference(tmp_path, strip_evidence):
    h, _, selected, recorded, _, outbox = approve_fixture(tmp_path, "ot_release_changes")
    rebound = recorded | {"decision": "approved"}
    if strip_evidence:
        del rebound["recorded_at"], rebound["decision_seal"]
    path = h.path.parent / "managed-outputs" / selected.proposal_ref / "release-decision.json"
    path.write_text(json.dumps(rebound))
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(
            replace(selected, approval_ref=approval_ref(rebound)), "dispatcher", "forged"
        )
    with h.store._db() as db:
        assert (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='release_outbox'").fetchone() is None
        )
