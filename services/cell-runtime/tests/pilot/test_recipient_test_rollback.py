"""Exercise a separately authorized change and rollback in generated recipient custody."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import release_gate, repair_workflow, source_intake
from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.managed_run import ManagedSelection, plan_revision
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery, dispatch_local_release
from opentour_cell.pilot.release_authority import (
    HandoffPermission,
    RecordedReleaseAuthority,
    approval_ref,
)
from opentour_cell.pilot.release_outbox import ApprovedRelease, ReleaseOutbox, ReleaseSelection
from opentour_cell.pilot.simple_repair import declared_rule, predict_rule
from test_managed_slack import prepared_workflow
from test_recorded_release_authority import DestinationPermissions
from test_repair_bundle import cli, transfer, write
from test_reviewer_workflow import buttons, screen

Json = dict[str, Any]
TARGET = "test-target-generated-recipient"
OPERATOR = "generated-rollback-operator"
QUERY = {"features": {"event_type": "refund", "description": "generated decision input"}}


@dataclass(frozen=True)
class GeneratedTarget:
    config: Path
    key: bytes
    release: ApprovedRelease
    receipt_ref: str
    receipt_path: Path
    incumbent_version: str
    incumbent_decision: str
    winner_decision: str

    def envelope(self, **changes: object) -> Json:
        claim = {
            "schema_version": "opentour.fixture.recipient-test-change.v1",
            "target_ref": TARGET,
            "tenant": self.release.tenant,
            "engagement": self.release.engagement,
            "release_ref": self.release.reference,
            "receiver_receipt_ref": self.receipt_ref,
            "artifact_sha256": self.release.artifact_sha256,
            "operator": OPERATOR,
            "operation": "activate_test",
            "expected_version": self.incumbent_version,
            "target_version": self.release.artifact_sha256,
            "expires_at": min(int(time.time()) + 90, self.release.expires_at),
            "request_ref": "generated-activation-one",
            **changes,
        }
        return {
            "claim": claim,
            "seal_sha256": hmac.new(self.key, canonical(claim).encode(), "sha256").hexdigest(),
        }


@pytest.fixture
def generated_target(tmp_path: Path) -> GeneratedTarget:
    h, plan, _ = prepared_workflow(tmp_path / "worker", "rule", permit_slack=False, release=True)
    owner = h.path.parent
    organization = h.store.organization
    engagement = h.store.engagement_id
    prior_rule = declared_rule(
        feature="event_type",
        allowed_features=["description", "event_type"],
        mapping={"refund": "sale", "sale": "sale"},
        allowed_labels=["refund", "sale"],
    )
    plan["incumbent_revision"] = prior_rule["revision"]
    plan["handoff"]["fallback"] = {
        "kind": "rule",
        "policy_revision": plan["handoff"]["policy_revision"],
        "rule": prior_rule,
    }
    frozen_manifest = owner / "rollback-plan.json"
    write(frozen_manifest, plan)
    repair_workflow.grant_slack_review(owner, organization, plan)
    selection = ManagedSelection(
        owner, organization, frozen_manifest, owner / "rollback-queue-grant.json"
    )
    revision = plan_revision(plan)
    cutoff = int(time.time()) + 550
    selection.authorize(
        revision, "att-abcddcbaabcddcba", cutoff, None, repair_workflow.inspect_managed
    )
    run = selection.start(revision, "generated-rollback-proof")["run_id"]
    actual = selection.once(repair_workflow.execute_managed)
    assert actual["state"] == "ready" and actual["result"]["outcome"] == "simple_repair"
    assert actual["run_id"] == run

    view = h.launch()
    h.action(view, "ot_evidence", buttons(screen(h, view))["ot_evidence"])
    h.action(view, "ot_release_approve", buttons(screen(h, view))["ot_release_approve"])
    audit = repair_workflow.managed_review_evidence(
        owner, organization, run, plan["audience"], h.config["team"], h.config["channel"]
    )["audit"]
    receipt = release_gate.authorize_release(
        owner, organization, run, audit["run_revision"], audit["candidate_digest"]
    )
    assert receipt["decision"] == "approved" and receipt["provider"] == "slack"
    prepared = repair_workflow.prepare_handoff(owner, organization, plan, run)
    operator = owner / "local-delivery-operator.json"
    write(
        operator,
        {
            **{
                key: prepared[key]
                for key in (
                    *bundle.BOUND,
                    "dataset_id",
                    "store_engagement",
                    "intake_revision",
                    "spec_sha256",
                    "local_assets",
                )
            },
            "role": "operator",
            "actor": plan["handoff"]["exporter"],
            "schema_version": bundle.GRANT,
            "expires_at": cutoff,
            "state": "active",
            "store": str(h.store.path),
        },
    )
    recipient = tmp_path / "recipient"
    recipient.mkdir(mode=0o700)
    grant, offered, _ = transfer(owner, recipient, prepared, operator, plan["audience"])
    receiver = LocalRecipientDelivery(grant)
    intake = source_intake.check_current_intake(
        owner, organization, engagement, plan["intake_revision"]
    )
    permission = HandoffPermission(
        organization,
        engagement,
        intake["workflow"],
        run,
        audit["run_revision"],
        audit["candidate_digest"],
        plan["audience"],
        receiver.destination(),
        "generated-rollback-delivery-permission",
        cutoff,
    )
    authority = RecordedReleaseAuthority(owner, organization, DestinationPermissions(permission))
    release_selection = ReleaseSelection(intake["workflow"], run, approval_ref(receipt))
    outbox = ReleaseOutbox(h.store, authority)
    with h.store._db() as db:
        release = authority.resolve(db, release_selection, "dispatcher", "claim")
    assert release is not None and release.artifact_sha256 != prior_rule["revision"]
    assert receiver.lookup(release, "before-generated-delivery").state == "absent"
    reserved = outbox.reserve(release_selection, "dispatcher", "generated-rollback-delivery")
    assert reserved.stage == "queued"
    pending = dispatch_local_release(outbox, release_selection, "dispatcher", receiver, offered)
    assert pending.stage == "delivery_unconfirmed"
    fresh_receiver = LocalRecipientDelivery(grant)
    observed = fresh_receiver.lookup(release, "independent-generated-readback")
    delivered = outbox.reconcile(release_selection, "dispatcher", fresh_receiver)
    assert delivered.stage == observed.state == "delivered"
    assert delivered.receiver_receipt_ref == observed.receipt_ref
    assert observed.artifact_sha256 == release.artifact_sha256 and observed.receipt_ref
    (installed,) = (recipient / "custody").glob("delivery-*/artifact.zip")
    offered_hash = hashlib.sha256(offered.read_bytes()).hexdigest()
    assert hashlib.sha256(installed.read_bytes()).hexdigest() == offered_hash
    benchmark = cli(grant, "replay", "--archive", installed)
    assert benchmark["fallback_rehearsed"] and benchmark["package_sha256"] == offered_hash

    query_file = recipient / "rollback-query.json"
    write(query_file, QUERY)
    prior = cli(
        grant,
        "predict",
        "--archive",
        installed,
        "--input",
        query_file,
        "--version",
        prior_rule["revision"],
    )
    winner = cli(
        grant,
        "predict",
        "--archive",
        installed,
        "--input",
        query_file,
        "--version",
        release.artifact_sha256,
    )
    incumbent_decision = predict_rule(prior_rule, QUERY["features"])
    assert prior["decision"] == incumbent_decision == {"status": "automatic", "label": "sale"}
    assert winner["decision"] == {"status": "automatic", "label": "refund"}

    target = tmp_path / "test-target"
    target.mkdir(mode=0o700)
    key_path = target / "test-change-key.json"
    key = secrets.token_bytes(32)
    write(key_path, {"key": key.hex()})
    config = target / "test-target.json"
    write(
        config,
        {
            "schema_version": "opentour.fixture.recipient-test-target.v1",
            "target_ref": TARGET,
            "operator": OPERATOR,
            "key_file": key_path.name,
            "recipient_grant": str(grant),
            "release": asdict(release),
            "receiver_receipt_ref": observed.receipt_ref,
            "query": QUERY,
            "baseline": {
                "version": prior_rule["revision"],
                "decision_sha256": digest(incumbent_decision),
            },
        },
    )
    return GeneratedTarget(
        config,
        key,
        release,
        observed.receipt_ref,
        installed.with_name("receipt.json"),
        prior_rule["revision"],
        digest(incumbent_decision),
        digest(winner["decision"]),
    )


def target_cli(config: Path, *args: object, denied: bool = False) -> Json:
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "tools.fixtures.recipient_rollback_target",
            "--configuration",
            str(config),
            *map(str, args),
        ],
        capture_output=True,
        text=True,
        timeout=180,
    )
    if denied:
        assert result.returncode == 2 and not result.stdout, (result.stdout, result.stderr)
        return {}
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def assert_readback(result: Json, version: str, decision_hash: str, effects: int) -> None:
    assert (result["active_version"], result["decision_sha256"], result["effects"]) == (
        version,
        decision_hash,
        effects,
    )


def test_generated_test_target_requires_separate_signed_change_and_rollback(
    generated_target: GeneratedTarget,
) -> None:
    sample = generated_target
    request = sample.config.with_name("test-change-request.json")

    def change(envelope: Json, *, denied: bool = False) -> Json:
        write(request, envelope)
        return target_cli(sample.config, "change", "--request", request, denied=denied)

    def inspect(version: str, decision_hash: str, effects: int) -> None:
        assert_readback(target_cli(sample.config, "inspect"), version, decision_hash, effects)

    inspect(sample.incumbent_version, sample.incumbent_decision, 0)
    first = sample.envelope()
    rollback = sample.envelope(
        operation="rollback_test",
        expected_version=sample.release.artifact_sha256,
        target_version=sample.incumbent_version,
        request_ref="generated-rollback-one",
    )
    for refused in (
        {"claim": first["claim"]},
        first | {"claim": first["claim"] | {"operator": "generated-impostor"}},
        sample.envelope(operator="generated-impostor"),
        sample.envelope(tenant="generated-neighbor"),
        sample.envelope(expires_at=int(time.time()) - 1),
        sample.envelope(receiver_receipt_ref="received-" + "1" * 48),
        rollback,
    ):
        change(refused, denied=True)
    withheld = sample.receipt_path.with_suffix(".withheld")
    sample.receipt_path.rename(withheld)
    try:
        change(first, denied=True)
    finally:
        withheld.rename(sample.receipt_path)
    inspect(sample.incumbent_version, sample.incumbent_decision, 0)

    active = change(first)
    assert active["action_ref"]
    assert_readback(active, sample.release.artifact_sha256, sample.winner_decision, 1)
    inspected = target_cli(sample.config, "inspect")
    assert_readback(inspected, sample.release.artifact_sha256, sample.winner_decision, 1)
    assert change(first)["action_ref"] == active["action_ref"]
    inspect(sample.release.artifact_sha256, sample.winner_decision, 1)

    change({"claim": rollback["claim"], "seal_sha256": first["seal_sha256"]}, denied=True)
    inspect(sample.release.artifact_sha256, sample.winner_decision, 1)
    restored = change(rollback)
    assert restored["action_ref"] and restored["action_ref"] != active["action_ref"]
    assert_readback(restored, sample.incumbent_version, sample.incumbent_decision, 2)
    assert change(rollback)["action_ref"] == restored["action_ref"]
    assert change(first)["action_ref"] == active["action_ref"]
    inspect(sample.incumbent_version, sample.incumbent_decision, 2)


def test_recipient_grant_deadline_after_final_readback_holds_test_target(
    generated_target: GeneratedTarget, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tools.fixtures import recipient_rollback_target

    sample = generated_target
    config = json.loads(sample.config.read_bytes())
    grant = json.loads(Path(config["recipient_grant"]).read_bytes())
    deadline = grant["expires_at"]
    assert deadline < sample.release.expires_at
    request = sample.config.with_name("generated-replace-at-cutoff.json")
    write(request, sample.envelope(expires_at=deadline + 1))
    clock = deadline - 1
    monkeypatch.setattr(time, "time", lambda: clock)
    original = LocalRecipientDelivery._receipt
    reads = 0

    def final_readback(receiver, *args, **kwargs):
        nonlocal clock, reads
        observed = original(receiver, *args, **kwargs)
        reads += 1
        if reads == 2:
            clock = deadline
        return observed

    monkeypatch.setattr(LocalRecipientDelivery, "_receipt", final_readback)
    with pytest.raises(bundle.BundleError, match="test_change_authority_changed"):
        recipient_rollback_target.run(sample.config, request)
    assert reads == 2
    assert not sample.config.with_name(recipient_rollback_target.STATE_FILE).exists()
    assert not list(sample.config.parent.glob(".test-route-*"))
