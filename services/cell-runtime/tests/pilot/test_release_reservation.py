"""Exercise the private operator CLI on receipts from the ordinary review flow."""

import json
import re
import subprocess
import sys
import time
from dataclasses import replace

import pytest
from opentour_cell.pilot import release_gate, release_reservation
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.release_outbox import ReleaseOutboxError
from test_local_handoff_permissions import KEY, signed_permission
from test_recipient_handoff_simulator import fresh_proposal
from test_recorded_release_authority import approve_fixture
from test_repair_bundle import write


def configure(tmp_path, decision="ot_release_approve"):
    h, _, _, approval, permission, _ = approve_fixture(tmp_path, decision)
    assert approval["decision_seal"].startswith("release_")
    permit = permission.current
    worker = h.store.path.parent
    write(worker / "handoff-permission-key.json", {"key": KEY.hex()})
    grant = signed_permission(worker, permit)
    configuration = worker / "release-reservation.json"
    scope = {
        "schema_version": release_reservation.SCHEMA,
        "tenant": h.store.organization,
        "actor": "dispatcher",
        "workflow_ref": permit.workflow_ref,
    }
    write(configuration, scope)
    return h, approval, permit, grant, configuration, scope


def invoke(configuration, operation_key, *, discard=False):
    return subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "opentour_cell.pilot.release_reservation",
            "--configuration",
            str(configuration),
            "--operation-key",
            operation_key,
        ],
        stdout=subprocess.DEVNULL if discard else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=30,
        check=False,
    )


def received_reference(result):
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    ref = json.loads(result.stdout)["release_ref"]
    assert re.fullmatch(r"release-[a-f0-9]{48}", ref)
    assert result.stdout == f'{{"release_ref":"{ref}"}}\n'
    return ref


def assert_refused(result):
    assert result.returncode == 2, result.stderr
    assert result.stdout == ""
    assert result.stderr == "release_reservation_unavailable\n"


def reservation_state(store):
    with store._db() as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='release_outbox'").fetchone() is None:
            return [], []
        rows = db.execute(
            "SELECT ref,stage,claimed_at,last_checked_at,receiver_receipt_ref,received_at "
            "FROM release_outbox ORDER BY ref"
        )
        keys = db.execute("SELECT operation_key,ref FROM release_operation_keys ORDER BY 1")
        assert not db.execute(
            "SELECT 1 FROM sqlite_master WHERE name='release_receiver_seals'"
        ).fetchone()
        return list(map(tuple, rows)), list(map(tuple, keys))


def queued(ref):
    return (ref, "queued", None, None, None, None)


def test_lost_output_and_three_keys_reuse_one_committed_reference(tmp_path):
    h, _, _, _, configuration, _ = configure(tmp_path)
    assert reservation_state(h.store) == ([], [])
    lost = invoke(configuration, "operation-a", discard=True)
    assert lost.returncode == 0 and not lost.stderr
    (committed,), aliases = reservation_state(h.store)
    ref = committed[0]
    assert committed == queued(ref)
    assert aliases == [("operation-a", ref)]

    assert received_reference(invoke(configuration, "operation-a")) == ref
    assert received_reference(invoke(configuration, "operation-b")) == ref
    assert received_reference(invoke(configuration, "operation-c")) == ref
    assert reservation_state(h.store) == (
        [queued(ref)],
        [("operation-a", ref), ("operation-b", ref), ("operation-c", ref)],
    )


@pytest.mark.parametrize(
    "condition",
    ["inspect_only", "approving_actor", "no_grant", "revoked", "expired", "foreign", "declined"],
)
def test_no_reservation_without_current_exact_operator_and_approval(tmp_path, condition):
    choice = "ot_release_changes" if condition == "declined" else "ot_release_approve"
    h, approval, permit, grant, configuration, scope = configure(tmp_path, choice)
    if condition in {"inspect_only", "approving_actor"}:
        actor = "inspector" if condition == "inspect_only" else approval["reviewer"]
        write(configuration, scope | {"actor": actor})
    elif condition == "no_grant":
        grant.unlink()
    elif condition == "revoked":
        revoked = digest(
            [permit.tenant, permit.workflow_ref, permit.engagement, "generated-release-grant"]
        )
        write(grant.parent / f"handoff-permission-revoked-{revoked}.json", {})
    elif condition == "expired":
        signed_permission(grant.parent, permit, expires_at=int(time.time()) - 1)
    elif condition == "foreign":
        signed_permission(grant.parent, permit, tenant="generated-foreign")
    else:
        assert approval["decision"] == "changes_requested"

    assert_refused(invoke(configuration, "refused-operation"))
    assert reservation_state(h.store) == ([], [])


def test_cyclic_configuration_path_refuses_without_disclosing_path(tmp_path):
    configuration = tmp_path / "loop"
    configuration.symlink_to(configuration)
    assert_refused(invoke(configuration, "unavailable-operation"))


def test_changed_destination_during_approval_read_leaves_no_reservation(tmp_path, monkeypatch):
    h, _, permit, grant, configuration, _ = configure(tmp_path)
    original = release_gate.inspect_recorded_release

    def rotate_after_read(*args):
        approval = original(*args)
        signed_permission(grant.parent, permit, destination_ref="generated-rotated-destination")
        return approval

    monkeypatch.setattr(release_gate, "inspect_recorded_release", rotate_after_read)
    with pytest.raises(ReleaseOutboxError):
        release_reservation.reserve(configuration, "operation-after-rotation")
    assert reservation_state(h.store) == ([], [])


def test_separately_inspected_and_approved_successor_reserves_second_intent(tmp_path):
    h, _, permit, grant, configuration, _ = configure(tmp_path)
    first_ref = received_reference(invoke(configuration, "first-approval"))
    prior_plan = json.loads((h.store.path.parent / "managed-plan.json").read_text())
    _, run, approval = fresh_proposal(h, prior_plan)
    assert run != permit.proposal_ref
    assert approval["run_revision"] != permit.proposal_revision
    assert approval["artifact_sha"] != permit.artifact_sha256
    successor = replace(
        permit,
        proposal_ref=run,
        proposal_revision=approval["run_revision"],
        artifact_sha256=approval["artifact_sha"],
        expires_at=int(time.time()) + 300,
    )
    signed_permission(grant.parent, successor)
    assert_refused(invoke(configuration, "first-approval"))
    second_ref = received_reference(invoke(configuration, "second-approval"))
    assert second_ref != first_ref
    assert reservation_state(h.store) == (
        sorted([queued(first_ref), queued(second_ref)]),
        [("first-approval", first_ref), ("second-approval", second_ref)],
    )
