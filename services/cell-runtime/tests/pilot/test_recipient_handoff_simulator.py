"""Exercise the versioned handoff entry with signed generated approvals and custody."""

import base64
import hashlib
import json
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import pytest
from opentour_cell.pilot import approval_inbox, release_gate, repair_workflow, source_intake
from opentour_cell.pilot import repair_bundle as bundle
from opentour_cell.pilot.local_handoff_permissions import handoff_outbox
from opentour_cell.pilot.managed_run import ManagedSelection, plan_revision
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.recipient_delivery import LocalRecipientDelivery
from opentour_cell.pilot.recipient_handoff_simulator import LOST_ACK, RECIPE_SHA256, SCHEMA
from opentour_cell.pilot.release_authority import HandoffPermission, approval_ref
from opentour_cell.pilot.release_outbox import ReleaseOutbox, ReleaseOutboxError, ReleaseSelection
from test_approval_inbox import callback, opt_in, stage
from test_local_handoff_permissions import signed_permission
from test_recorded_release_authority import approve_fixture
from test_repair_bundle import cli, read, transfer, write
from test_reviewer_workflow import buttons, screen
from test_slack_adapter import Harness

MODULE = "opentour_cell.pilot.recipient_handoff_simulator"
RECIPE = Path(__file__).resolve().parents[2] / "integrations/zapier/handoff-recipe-v1.json"


@dataclass(frozen=True)
class Enrollment:
    outbox: ReleaseOutbox
    receiver: LocalRecipientDelivery
    offer: Path
    selection: ReleaseSelection
    permit: HandoffPermission
    key: bytes
    configuration: Path
    release_ref: str

    @property
    def slots(self) -> list[Path]:
        return list(self.receiver.grant_file.parent.glob("custody/delivery-*"))


def configure(
    outbox: ReleaseOutbox, selection: ReleaseSelection, recipient: Path, permit: HandoffPermission
) -> Path:
    path = outbox.store.path.parent / "generated-handoff.json"
    write(
        path,
        {
            "schema_version": SCHEMA,
            "tenant": permit.tenant,
            "actor": "dispatcher",
            "recipient_directory": str(recipient),
            "selection": asdict(selection),
        },
    )
    return path


def authorize(outbox: ReleaseOutbox, permit: HandoffPermission, key: bytes, **changes) -> Path:
    worker = outbox.store.path.parent
    write(worker / "handoff-permission-key.json", {"key": key.hex()})
    return signed_permission(
        worker,
        permit,
        key,
        actors={"dispatcher": ["reserve", "inspect", "claim", "reconcile"]},
        **changes,
    )


def record_three_votes(h: Harness, proposal: str, audit: dict, pointer: str) -> dict:
    owner, reviewer = h.path.parent, h.config["reviewer"]
    opt_in(h)
    assert approval_inbox.flush_approvals(h.app) == 1
    h.send("interactive", callback(h, pointer, actor=reviewer))
    view = list(h.slack.screens)[-1]
    action = "ot_release_approve"
    event = h.action(view, action, buttons(screen(h, view))[action], reviewer)
    scope = (
        owner,
        h.store.organization,
        proposal,
        audit["run_revision"],
        audit["candidate_digest"],
    )
    prior = release_gate.authorize_release(*scope)
    for _ in range(2):
        h.send("interactive", event)
        assert release_gate.authorize_release(*scope) == prior
    return prior


def export_authorization(store_path: Path, plan: dict, prepared: dict) -> dict:
    return {
        **{
            field: prepared[field]
            for field in (
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
        "expires_at": int(time.time()) + 600,
        "state": "active",
        "store": str(store_path),
    }


def bind_release(
    h: Harness, receiver: LocalRecipientDelivery, offer: Path, approval: dict, key: bytes
) -> Enrollment:
    owner, org = h.path.parent, h.store.organization
    evidence = repair_workflow.managed_review_evidence(
        owner, org, approval["run_id"], approval["audience"], approval["team"], approval["channel"]
    )
    intake = source_intake.check_current_intake(
        owner, org, h.store.engagement_id, evidence["dataset_revision"]
    )
    permit = HandoffPermission(
        org,
        h.store.engagement_id,
        intake["workflow"],
        approval["run_id"],
        approval["run_revision"],
        approval["artifact_sha"],
        approval["audience"],
        receiver.destination(),
        "generated-owner-release",
        int(time.time()) + 180,
    )
    selection = ReleaseSelection(intake["workflow"], approval["run_id"], approval_ref(approval))
    outbox = handoff_outbox(h.store)
    authorize(outbox, permit, key)
    configuration = configure(outbox, selection, receiver.grant_file.parent, permit)
    reservations = [
        outbox.reserve(selection, "dispatcher", f"{selection.approval_ref}-{i}") for i in range(3)
    ]
    assert reservations == [reservations[0]] * 3
    assert reservations[0].stage == "queued"
    return Enrollment(
        outbox, receiver, offer, selection, permit, key, configuration, reservations[0].reference
    )


def initial_release(path: Path, slot: str) -> tuple[Enrollment, Harness, dict, Path]:
    h, plan, run, pointer, audit = stage(path / "worker", slot=slot)
    owner = h.path.parent
    approval = record_three_votes(h, run, audit, pointer)
    prepared = repair_workflow.prepare_handoff(owner, h.store.organization, plan, run)
    operator = owner / "generated-exporter.json"
    write(operator, export_authorization(h.store.path, plan, prepared))
    recipient = path / "recipient"
    recipient.mkdir(mode=0o700)
    grant, offer, _ = transfer(owner, recipient, prepared, operator, plan["audience"])
    key = hashlib.sha256(f"generated-handoff-custodian-{slot}".encode()).digest()
    return bind_release(h, LocalRecipientDelivery(grant), offer, approval, key), h, plan, operator


def enroll(path: Path, slot: str) -> Enrollment:
    item, _, _, _ = initial_release(path, slot)
    return item


def invoke(
    configuration: Path,
    release_ref: str,
    *flags: str,
    expected: int = 0,
    trigger_binding: tuple[Path, Path] | None = None,
) -> dict:
    selection = (
        ["--recipe", str(trigger_binding[0]), "--trigger", str(trigger_binding[1])]
        if trigger_binding is not None
        else ["--release-ref", release_ref]
    )
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            MODULE,
            "--configuration",
            str(configuration),
            *selection,
            *flags,
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    assert result.returncode == expected, result.stderr
    if expected:
        assert not result.stdout
        if expected == LOST_ACK:
            assert not result.stderr
        else:
            assert str(configuration) not in result.stderr and release_ref not in result.stderr
        return {}
    assert not result.stderr
    value = json.loads(result.stdout)
    assert set(value) == {
        "schema_version",
        "release_ref",
        "state",
        "receiver_receipt_ref",
        "owner_action",
    }
    assert (value["schema_version"], value["release_ref"]) == (SCHEMA, release_ref)
    return value


def independently_check(item: Enrollment, state: dict, slot: Path, offered_sha256: str) -> None:
    installed, receipt_path = slot / "artifact.zip", slot / "receipt.json"
    signed = read(receipt_path)
    package_digest = hashlib.sha256(installed.read_bytes()).hexdigest()
    assert (signed["release_ref"], signed["tenant"], signed["engagement"]) == (
        item.release_ref,
        item.permit.tenant,
        item.permit.engagement,
    )
    assert (signed["destination_ref"], signed["artifact_sha256"], signed["package_sha256"]) == (
        item.permit.destination_ref,
        item.permit.artifact_sha256,
        package_digest,
    )
    assert package_digest == offered_sha256
    signature = base64.b64decode(signed.pop("receipt_signature"), validate=True)
    assert signed["receipt_ref"] == state["receiver_receipt_ref"]
    with tempfile.TemporaryDirectory() as scratch:
        public, seal = Path(scratch) / "public.pem", Path(scratch) / "receipt.sig"
        certificate = read(item.receiver.grant_file)["recipient_certificate"]
        subprocess.run(
            ["openssl", "x509", "-in", certificate, "-pubkey", "-noout", "-out", str(public)],
            check=True,
            capture_output=True,
            timeout=10,
        )
        write(seal, signature)
        subprocess.run(
            ["openssl", "dgst", "-sha256", "-verify", str(public), "-signature", str(seal)],
            input=(canonical(signed) + "\n").encode(),
            check=True,
            capture_output=True,
            timeout=10,
        )
    replay = cli(item.receiver.grant_file, "replay", "--archive", installed)
    assert replay["package_sha256"] == package_digest and replay["fallback_rehearsed"]
    assert (
        replay["arm_readout_digests"]
        == cli(item.receiver.grant_file, "replay", "--archive", installed)["arm_readout_digests"]
    )


def test_two_tenants_read_back_commit_after_real_worker_exit(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    first, second = (enroll(root / name, name) for name in ("alpha", "beta"))
    recipe = root / "installed-handoff-recipe.json"
    write(recipe, RECIPE.read_bytes())
    recipe.chmod(0o644)
    assert first.release_ref != second.release_ref
    assert first.permit.destination_ref != second.permit.destination_ref
    invoke(first.configuration, second.release_ref, expected=2)
    invoke(second.configuration, first.release_ref, expected=2)
    assert first.slots == second.slots == []

    for item in (first, second):
        trigger = item.configuration.with_name("committed-release-event.json")
        write(trigger, {"release_ref": item.release_ref})
        binding = (recipe, trigger)
        offered_sha256 = hashlib.sha256(item.offer.read_bytes()).hexdigest()
        invoke(
            item.configuration,
            item.release_ref,
            "--simulate-lost-ack",
            expected=LOST_ACK,
            trigger_binding=binding,
        )
        assert item.outbox.status(item.selection, "dispatcher").stage == "delivery_unconfirmed"
        (slot,) = item.slots
        receipt = (slot / "receipt.json").read_bytes()
        assert read(slot / "receipt.json")["release_ref"] == item.release_ref
        # No offer can be read or resent during the independent reconciliation.
        item.offer.unlink()
        delivered = invoke(item.configuration, item.release_ref, trigger_binding=binding)
        assert delivered["state"] == "delivered" and delivered["owner_action"] is None
        independently_check(item, delivered, slot, offered_sha256)
        assert (
            invoke(
                item.configuration, item.release_ref, "--simulate-lost-ack", trigger_binding=binding
            )
            == delivered
        )
        assert not item.offer.exists()
        assert item.slots == [slot] and (slot / "receipt.json").read_bytes() == receipt
        with item.outbox.store._db() as db:
            assert db.execute("SELECT ref,stage FROM release_outbox").fetchall()[0][:] == (
                item.release_ref,
                "delivered",
            )
            assert db.execute("SELECT count(*) FROM release_outbox").fetchone()[0] == 1
            assert db.execute("SELECT count(*) FROM release_operation_keys").fetchone()[0] == 3
            assert db.execute("SELECT count(*) FROM release_receiver_seals").fetchone()[0] == 1


def test_crossed_recipient_and_unverifiable_write_hold_without_resend(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    first, second = (enroll(root / name, name) for name in ("alpha", "beta"))
    for item, foreign in ((first, second), (second, first)):
        configure(item.outbox, item.selection, foreign.receiver.grant_file.parent, item.permit)
        held = invoke(item.configuration, item.release_ref, "--simulate-lost-ack")
        assert held["state"] == "delivery_unconfirmed"
        assert held["owner_action"] == "authorized_operator_reconcile"
        assert held["receiver_receipt_ref"] is None
    assert first.slots == second.slots == []
    configure(first.outbox, first.selection, first.receiver.grant_file.parent, first.permit)
    # Known independent absence clears an unclaimed hold. The next trigger may claim it.
    assert invoke(first.configuration, first.release_ref)["state"] == "queued"
    invoke(first.configuration, first.release_ref, "--simulate-lost-ack", expected=LOST_ACK)
    (slot,) = first.slots
    receipt = slot / "receipt.json"
    original = receipt.read_bytes()
    write(receipt, read(receipt) | {"receipt_signature": "GENERATED_UNVERIFIABLE"})
    corrupted = receipt.read_bytes()
    for _ in range(2):
        held = invoke(first.configuration, first.release_ref, "--simulate-lost-ack")
        assert held["state"] == "delivery_unconfirmed" and held["receiver_receipt_ref"] is None
        assert held["owner_action"] == "authorized_operator_reconcile"
        assert first.slots == [slot] and receipt.read_bytes() == corrupted
    write(receipt, original)
    assert invoke(first.configuration, first.release_ref)["state"] == "delivered"


def test_unavailable_custody_stays_pending_before_and_after_commit(tmp_path: Path) -> None:
    item = enroll(tmp_path.resolve(), "unavailable")
    recipient = item.receiver.grant_file.parent
    inaccessible = recipient.with_name("held-custody")
    recipient.rename(inaccessible)
    pending = invoke(item.configuration, item.release_ref, "--simulate-lost-ack")
    assert pending["state"] == "delivery_unconfirmed" and pending["receiver_receipt_ref"] is None
    assert pending["owner_action"] == "authorized_operator_reconcile"
    assert not list(inaccessible.glob("custody/delivery-*"))
    inaccessible.rename(recipient)
    assert invoke(item.configuration, item.release_ref)["state"] == "queued"
    invoke(item.configuration, item.release_ref, "--simulate-lost-ack", expected=LOST_ACK)
    recipient.rename(inaccessible)
    assert invoke(item.configuration, item.release_ref, "--simulate-lost-ack") == pending
    inaccessible.rename(recipient)
    assert invoke(item.configuration, item.release_ref)["state"] == "delivered"
    assert len(item.slots) == 1


def fresh_proposal(h: Harness, plan: dict) -> tuple[dict, str, dict]:
    owner, org = h.path.parent, h.store.organization
    manifest = owner / "successor-plan.json"
    intake = owner / "successor-intake.json"
    grant = owner / "successor-source-grant.json"
    trace, correction = owner / "successor-trace.jsonl", owner / "successor-correction.jsonl"
    for previous, revised in (
        (owner / "trace.jsonl", trace),
        (owner / "corrections.jsonl", correction),
    ):
        updated = []
        for line in previous.read_text().splitlines():
            row = json.loads(line)
            for field in ("event_id", "customer_id", "group_id", "outcome_id"):
                if field in row:
                    row[field] = "successor-" + row[field]
            if row.get("features", {}).get("event_type") == "refund":
                row["features"]["event_type"] = "exchange"
            updated.append(json.dumps(row))
        write(revised, ("\n".join(updated) + "\n").encode())
    source = read(owner / "source-grant.json") | {
        "trace_sha256": hashlib.sha256(trace.read_bytes()).hexdigest(),
        "correction_sha256": hashlib.sha256(correction.read_bytes()).hexdigest(),
    }
    write(grant, source)
    discovery = source_intake.discover_intake(h.store, grant, trace, correction)
    frozen = source_intake.freeze_intake(
        h.store,
        grant,
        trace,
        correction,
        intake,
        confirmation={
            "owner_id": source["owner_id"],
            "attestation": "att-" + "4" * 16,
            "class_id": discovery["suspected_error_classes"][0]["class_id"],
            "discovery_revision": discovery["discovery_revision"],
        },
    )
    successor = plan | {
        "intake_file": intake.name,
        "intake_revision": frozen["intake_revision"],
        "rule": plan["rule"] | {"mapping": {"exchange": "refund", "sale": "sale"}},
    }
    repair_workflow.grant_slack_review(owner, org, successor)
    write(manifest, successor)
    managed = ManagedSelection(owner, org, manifest, owner / "successor-queue-grant.json")
    revision = plan_revision(successor)
    managed.authorize(
        revision,
        "att-abcddcbaabcddcba",
        int(time.time()) + 300,
        None,
        repair_workflow.inspect_managed,
    )
    run = managed.start(revision, "generated-successor-result")["run_id"]
    assert managed.once(repair_workflow.execute_managed)["result"]["outcome"] == "simple_repair"
    team, channel, audience = h.config["team"], h.config["channel"], successor["audience"]
    audit = repair_workflow.managed_review_evidence(owner, org, run, audience, team, channel)[
        "audit"
    ]
    pointer = approval_inbox.enqueue_ready(
        owner,
        org,
        run,
        team=team,
        channel=channel,
        audience=audience,
        reviewer=h.config["reviewer"],
        run_revision=audit["run_revision"],
    )["receipt"]
    return successor, run, record_three_votes(h, run, audit, pointer)


def superseding_offer(
    item: Enrollment, prepared: dict, plan: dict, original_operator: Path
) -> None:
    owner = original_operator.parent
    operator = owner / "successor-exporter.json"
    settings = read(original_operator)
    prior = read(item.receiver.grant_file)
    new_operator = export_authorization(item.outbox.store.path, plan, prepared)
    write(
        operator,
        new_operator
        | {
            key: settings[key]
            for key in ("seal_assets", "recipient_certificate", "recipient_certificate_sha256")
        }
        | {"portable": settings["portable"] | {"supersedes": prior["spec_sha256"]}},
    )
    args = ["--spec", prepared["spec_path"], "--source", prepared["source_directory"]]
    archived, issued = owner / "successor-sealed.zip", owner / "successor-entitlement.json"
    sealed = cli(operator, "seal", *args, "--output", archived)
    assert sealed["spec_sha256"] != prior["spec_sha256"]
    cli(operator, "issue-entitlement", *args, "--archive", archived, "--output", issued)
    entitlement = Path(prior["recipient_custody"]["ledger"]) / "successor-entitlement.json"
    write(entitlement, issued.read_bytes())
    write(item.offer, archived.read_bytes())
    write(
        item.receiver.grant_file,
        prior
        | {
            **{
                key: prepared[key]
                for key in (*bundle.BOUND, "dataset_id", "store_engagement", "intake_revision")
            },
            "spec_sha256": sealed["spec_sha256"],
            "recipient_custody": prior["recipient_custody"] | {"entitlement": str(entitlement)},
        },
    )
    assert cli(item.receiver.grant_file, "enroll-recipient")["spec_sha256"] == sealed["spec_sha256"]


def test_freshly_inspected_successor_has_distinct_delivery_to_same_recipient(
    tmp_path: Path,
) -> None:
    first, h, prior, operator = initial_release(tmp_path.resolve(), "successor")
    invoke(first.configuration, first.release_ref, "--simulate-lost-ack", expected=LOST_ACK)
    first_result = invoke(first.configuration, first.release_ref)
    (baseline,) = first.slots
    preserved = {file: file.read_bytes() for file in baseline.iterdir()}
    plan, run, reviewed = fresh_proposal(h, prior)
    assert reviewed["run_id"] != first.selection.proposal_ref
    assert reviewed["artifact_sha"] != first.permit.artifact_sha256
    prepared = repair_workflow.prepare_handoff(operator.parent, h.store.organization, plan, run)
    superseding_offer(first, prepared, plan, operator)
    successor = bind_release(h, first.receiver, first.offer, reviewed, first.key)
    assert successor.permit.destination_ref == first.permit.destination_ref
    assert successor.permit.workflow_ref == first.permit.workflow_ref
    assert successor.release_ref != first.release_ref
    offered_sha256 = hashlib.sha256(successor.offer.read_bytes()).hexdigest()
    invoke(successor.configuration, first.release_ref, expected=2)
    invoke(successor.configuration, successor.release_ref, "--simulate-lost-ack", expected=LOST_ACK)
    result = invoke(successor.configuration, successor.release_ref)
    assert (
        result["state"] == "delivered"
        and result["receiver_receipt_ref"] != first_result["receiver_receipt_ref"]
    )
    assert len(successor.slots) == 2 and set(successor.slots) - {baseline}
    for file, contents in preserved.items():
        assert file.read_bytes() == contents
    (installed,) = set(successor.slots) - {baseline}
    independently_check(successor, result, installed, offered_sha256)
    assert invoke(successor.configuration, successor.release_ref) == result
    with successor.outbox.store._db() as db:
        assert sorted(map(tuple, db.execute("SELECT ref,stage FROM release_outbox"))) == sorted(
            [(first.release_ref, "delivered"), (successor.release_ref, "delivered")]
        )
        assert db.execute("SELECT count(*) FROM release_receiver_seals").fetchone()[0] == 2


@pytest.mark.parametrize("reason", ["expired", "rotated", "changed_candidate", "mutated_package"])
def test_current_grant_and_exact_package_fence(tmp_path: Path, reason: str) -> None:
    item = enroll(tmp_path.resolve(), "negative")
    if reason == "expired":
        authorize(item.outbox, item.permit, item.key, expires_at=int(time.time()) - 1)
    elif reason == "rotated":
        write(item.configuration.parent / "handoff-permission-key.json", {"key": bytes(32).hex()})
    elif reason == "changed_candidate":
        authorize(item.outbox, item.permit, item.key, artifact_sha256="6" * 64)
    else:
        write(item.offer, b"GENERATED_MUTATED_PACKAGE")
        held = invoke(item.configuration, item.release_ref, "--simulate-lost-ack")
        assert held["state"] == "delivery_unconfirmed" and held["receiver_receipt_ref"] is None
        assert invoke(item.configuration, item.release_ref) == held
        assert item.slots == []
        return
    invoke(item.configuration, item.release_ref, "--simulate-lost-ack", expected=2)
    assert item.slots == []


def test_declined_decision_and_unsigned_activation_have_no_effect(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    donor = enroll(root / "donor", "donor")
    h, _, selected, receipt, permits, _ = approve_fixture(root / "declined", "ot_release_changes")
    assert receipt["decision"] == "changes_requested" and receipt["decision_seal"]
    outbox = handoff_outbox(h.store)
    authorize(outbox, permits.current, donor.key, destination_ref=donor.permit.destination_ref)
    config = configure(outbox, selected, donor.receiver.grant_file.parent, permits.current)
    with pytest.raises(ReleaseOutboxError):
        outbox.reserve(selected, "dispatcher", "declined-decision")
    invoke(config, donor.release_ref, expected=2)
    assert donor.slots == []
    invoke(donor.configuration, donor.release_ref, "--activate", expected=2)
    assert donor.slots == []
    assert donor.outbox.status(donor.selection, "dispatcher").stage == "queued"
    with outbox.store._db() as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='release_outbox'").fetchone()
            is None
        )


def test_disabled_template_is_reference_only() -> None:
    template = read(RECIPE)
    assert template["enabled_by_default"] is template["production_activation"] is False
    assert template["worker_local"]["module"] == MODULE
    assert template["trigger"]["fields"] == ["release_ref"]
    assert template["hosted_mapping"]["provider_history"] == "opaque_metadata_only"
    assert hashlib.sha256(RECIPE.read_bytes()).hexdigest() == RECIPE_SHA256


def test_recipe_and_private_trigger_are_fail_closed(tmp_path: Path) -> None:
    root = tmp_path.resolve()
    item = enroll(root / "alpha", "alpha")
    foreign = enroll(root / "beta", "beta")
    recipe = root / "installed-handoff-recipe.json"
    write(recipe, RECIPE.read_bytes())
    recipe.chmod(0o644)
    trigger = item.configuration.with_name("committed-release-event.json")
    binding = (recipe, trigger)
    bad_events = (
        {"release_ref": foreign.release_ref},
        {"release_ref": "release-" + "0" * 48},
        {"release_ref": item.release_ref, "actor": "dispatcher"},
        {"release_ref": 111},
        {"reference": item.release_ref},
        f'{{"release_ref":"{item.release_ref}","release_ref":"{item.release_ref}"}}'.encode(),
        b'{"release_ref":',
    )
    for event in bad_events:
        write(trigger, event)
        invoke(item.configuration, item.release_ref, expected=2, trigger_binding=binding)
    write(trigger, {"release_ref": item.release_ref})
    trigger.chmod(0o644)
    invoke(item.configuration, item.release_ref, expected=2, trigger_binding=binding)
    trigger.chmod(0o600)
    hostile = read(RECIPE)
    hostile["enabled_by_default"] = True
    hostile["worker_local"]["module"] = "GENERATED_UNAPPROVED_MODULE"
    write(recipe, hostile)
    invoke(item.configuration, item.release_ref, expected=2, trigger_binding=binding)
    write(recipe, RECIPE.read_bytes())
    recipe.chmod(0o666)
    invoke(item.configuration, item.release_ref, expected=2, trigger_binding=binding)
    recipe.chmod(0o644)
    linked = root / "linked-recipe.json"
    linked.symlink_to(recipe)
    invoke(item.configuration, item.release_ref, expected=2, trigger_binding=(linked, trigger))
    for enrolled in (item, foreign):
        assert enrolled.outbox.status(enrolled.selection, "dispatcher").stage == "queued"
        assert enrolled.slots == []


def test_local_connection_requires_private_configuration_and_scoped_principal(
    tmp_path: Path,
) -> None:
    item = enroll(tmp_path.resolve(), "connection")
    cfg = read(item.configuration)
    write(item.configuration, cfg | {"actor": "unknown-principal"})
    invoke(item.configuration, item.release_ref, "--simulate-lost-ack", expected=2)
    write(item.configuration, cfg)
    item.configuration.chmod(0o644)
    invoke(item.configuration, item.release_ref, "--simulate-lost-ack", expected=2)
    item.configuration.chmod(0o600)
    assert item.outbox.status(item.selection, "dispatcher").stage == "queued"
    assert item.slots == []
