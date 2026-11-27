"""Generated role keys, actual saved settings and actual frozen worker evidence."""

from __future__ import annotations

import base64
import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path

import pytest
from opentour_cell.pilot import autonomous_cycle, repair_bundle
from opentour_cell.pilot import reviewer_assignment as assignments
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.owner_settings import SettingsError
from test_autonomous_cycle import read, save
from test_owner_settings import Setup


class AssignmentSetup(Setup):
    def __init__(self, path: Path, *, dual_role: bool = False, run: bool = True):
        super().__init__(path)
        self.workflow_owner = self.owner if dual_role else "generated-workflow-owner"
        self.successor = "USUCCESSOR"
        self.owner_private = path / "workflow-owner-private.pem"
        self.owner_public = self.customer.owner / "workflow-owner-public.pem"
        repair_bundle._crypto(
            [
                "genpkey",
                "-algorithm",
                "RSA",
                "-pkeyopt",
                "rsa_keygen_bits:2048",
                "-out",
                str(self.owner_private),
            ],
            b"",
            "generated_key_failed",
        )
        self.owner_private.chmod(0o600)
        repair_bundle._crypto(
            ["pkey", "-in", str(self.owner_private), "-pubout", "-out", str(self.owner_public)],
            b"",
            "generated_key_failed",
        )
        self.owner_public.chmod(0o600)
        save(
            self.authority,
            dict(
                read(self.authority),
                schema_version=assignments.AUTHORITY,
                workflow_owner=self.workflow_owner,
                reviewers=[self.reviewer, self.successor, "UTHIRD"],
                workflow_owner_public_key_file=self.owner_public.name,
                workflow_owner_public_key_sha256=repair_bundle._sha(self.owner_public.read_bytes()),
            ),
        )
        self.current = self.apply()
        self.assignments = assignments.ReviewerAssignments(self.service)
        self.outcome = None
        if run:
            config = self.service.worker_config(self.current, actor=self.operator)["config"]
            self.outcome = autonomous_cycle.cycle_once(
                self.customer.owner, self.customer.org, config
            )
            assert self.outcome["state"] == "pending_slack_review", self.outcome

    def owner_packet(self, claim=None, *, key=None):
        if claim is None:
            claim = self.assignments.preview(self.successor, actor=self.workflow_owner)["claim"]
        signature = repair_bundle._crypto(
            ["dgst", "-sha256", "-sign", str(key or self.owner_private)],
            canonical(claim).encode(),
            "generated_sign_failed",
        )
        return {"claim": claim, "signature": base64.b64encode(signature).decode()}

    def count(self):
        with self.service.ledger._db(write=False) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_assignments'"
            ).fetchone():
                return 0
            return db.execute("SELECT count(*) FROM reviewer_assignments").fetchone()[0]


@pytest.mark.parametrize("dual_role", [False, True])
def test_actual_owner_assignment_preserves_settings_and_frozen_proof(tmp_path, dual_role):
    s = AssignmentSetup(tmp_path, dual_role=dual_role)
    packet = s.owner_packet()
    before_reads = s.protocol()
    first = s.assignments.assign(packet, actor=s.workflow_owner)
    restarted = assignments.ReviewerAssignments(s.service)
    assert restarted.assign(packet, actor=s.workflow_owner) == first
    assert first["state"] == "awaiting_custodian_save"
    assert first["workflow_owner"] == s.workflow_owner
    assert first["prior_settings_revision"] == s.current["revision"]
    assert len(first["proposals"]) == 1
    proposal = first["proposals"][0]
    assert proposal["run_id"] == s.outcome["run_id"]
    assert proposal["run_revision"] == s.outcome["result_revision"]
    assert proposal["original_reviewer"] == s.reviewer
    assert len(proposal["artifact_sha"]) == len(proposal["review_digest"]) == 64
    assert s.count() == 1
    assert s.service.inspect(actor=s.owner)["current"] == s.current
    assert s.protocol() == before_reads
    assert not list(s.customer.owner.glob("managed-outputs/*/release-decision.json"))
    assert not list(s.customer.owner.glob("managed-outputs/*/handoff*"))


def test_owner_and_custodian_keys_do_not_substitute_for_roles(tmp_path):
    s = AssignmentSetup(tmp_path, run=False)
    packet = s.owner_packet()
    with pytest.raises(SettingsError, match="workflow_owner_assignment_required"):
        s.assignments.preview(s.successor, actor=s.owner)
    with pytest.raises(SettingsError, match="workflow_owner_assignment_required"):
        s.assignments.assign(packet, actor=s.owner)
    with pytest.raises(SettingsError, match="custodian_required"):
        s.service.preview(s.policy, actor=s.workflow_owner)
    with pytest.raises(SettingsError, match="invalid_assignment_signature"):
        s.assignments.assign(s.owner_packet(key=s.private), actor=s.workflow_owner)
    with pytest.raises(SettingsError, match="signature_scope_mismatch"):
        s.service.save(packet, actor=s.owner)
    assert s.count() == 0
    assert s.service.inspect(actor=s.owner)["current"] == s.current


@pytest.mark.parametrize(
    "case", ["tenant", "engagement", "actor", "epoch", "expiry", "forged", "absent"]
)
def test_invalid_assignments_do_not_mutate_or_read_provider(tmp_path, case):
    s = AssignmentSetup(tmp_path, run=False)
    packet = s.owner_packet()
    claim = deepcopy(packet["claim"])
    if case == "tenant":
        claim["organization"] = "neighbor-organization"
    elif case == "engagement":
        claim["engagement"] = "neighbor-engagement"
    elif case == "actor":
        claim["actor"] = s.owner
    elif case == "epoch":
        claim["workflow_owner_role_revision"] = "0" * 64
    elif case == "expiry":
        claim["expires_at"] = int(time.time()) - 1
    elif case == "forged":
        packet["claim"]["successor"] = s.reviewer
    elif case == "absent":
        del packet["signature"]
    if case not in {"forged", "absent"}:
        packet = s.owner_packet(claim)
    before = s.protocol()
    with pytest.raises(SettingsError):
        s.assignments.assign(packet, actor=s.workflow_owner)
    assert s.count() == 0
    assert s.protocol() == before


@pytest.mark.parametrize(
    "case", ["owner_role", "reviewer_grant", "key", "source_grant", "settings"]
)
def test_assignment_rechecks_actual_current_authority(tmp_path, case):
    s = AssignmentSetup(tmp_path, run=False)
    packet = s.owner_packet()
    authority = read(s.authority)
    if case == "owner_role":
        save(s.authority, dict(authority, workflow_owner=s.owner))
    elif case == "reviewer_grant":
        save(s.authority, dict(authority, reviewers=[s.reviewer]))
    elif case == "key":
        s.owner_public.write_bytes(b"changed-key")
    elif case == "source_grant":
        cfg = s.customer.real_reader.cfg
        save(
            s.customer.owner / ("source-withdrawn-" + cfg["scope"]["owner_attestation"] + ".json"),
            {"withdrawn": True},
        )
    else:
        policy = deepcopy(s.policy)
        policy["mandate"]["mandate_id"] = "newer-settings"
        s.apply(policy)
    before = s.protocol()
    with pytest.raises((SettingsError, ValueError)):
        s.assignments.assign(packet, actor=s.workflow_owner)
    assert s.count() == 0
    assert s.protocol() == before


def test_concurrent_duplicate_assignment_is_one_durable_record(tmp_path):
    s = AssignmentSetup(tmp_path, run=False)
    packet = s.owner_packet()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda _: s.assignments.assign(packet, actor=s.workflow_owner), range(2))
        )
    assert results[0] == results[1]
    assert s.count() == 1


def test_new_frozen_proposal_requires_fresh_assignment(tmp_path):
    s = AssignmentSetup(tmp_path, run=False)
    packet = s.owner_packet()
    config = s.service.worker_config(s.current, actor=s.operator)["config"]
    assert (
        autonomous_cycle.cycle_once(s.customer.owner, s.customer.org, config)["state"]
        == "pending_slack_review"
    )
    with pytest.raises(SettingsError, match="assignment_revision_conflict"):
        s.assignments.assign(packet, actor=s.workflow_owner)
    assert s.count() == 0
    receipt = s.assignments.assign(s.owner_packet(), actor=s.workflow_owner)
    assert len(receipt["proposals"]) == 1


def test_actual_assignment_command_uses_the_role_signature(tmp_path):
    s = AssignmentSetup(tmp_path, run=False)
    base = [sys.executable, "-B", "-m", "opentour_cell.pilot.reviewer_assignment"]
    scope = [
        "--directory",
        str(s.customer.owner),
        "--organization",
        s.customer.org,
        "--engagement",
        s.customer.eng,
        "--authority",
        str(s.authority),
        "--actor",
        s.workflow_owner,
    ]
    preview = subprocess.run(
        base + ["preview", *scope, "--successor", s.successor],
        capture_output=True,
        text=True,
        check=True,
    )
    packet = s.owner_packet(json.loads(preview.stdout)["claim"])
    source = s.customer.owner / "owner-assignment-packet.json"
    save(source, packet)
    first = subprocess.run(
        base + ["assign", *scope, "--input", str(source)],
        capture_output=True,
        text=True,
        check=True,
    )
    retry = subprocess.run(
        base + ["assign", *scope, "--input", str(source)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(first.stdout) == json.loads(retry.stdout)
    assert s.count() == 1
    assert s.service.inspect(actor=s.owner)["current"] == s.current


def test_legacy_custodian_key_does_not_imply_owner_assignment_authority(tmp_path):
    s = Setup(tmp_path)
    s.apply()
    service = assignments.ReviewerAssignments(s.service)
    with pytest.raises(SettingsError, match="workflow_owner_signing_authority_required"):
        service.preview(s.reviewer, actor=s.owner)


def test_distinct_roles_cannot_share_the_custodian_key(tmp_path):
    s = AssignmentSetup(tmp_path, run=False)
    a = read(s.authority)
    save(
        s.authority,
        dict(
            a,
            workflow_owner_public_key_file=a["public_key_file"],
            workflow_owner_public_key_sha256=a["public_key_sha256"],
        ),
    )
    with pytest.raises(SettingsError, match="distinct_role_signing_keys_required"):
        s.assignments.preview(s.successor, actor=s.workflow_owner)
    assert s.count() == 0


def test_changed_frozen_proof_cannot_be_assigned(tmp_path):
    s = AssignmentSetup(tmp_path)
    proof = s.customer.owner / "managed-outputs" / s.outcome["run_id"] / "review.json"
    value = read(proof)
    value["audit"]["candidate_digest"] = "0" * 64
    save(proof, value)
    with pytest.raises(SettingsError, match="assignment_frozen_proposal_changed"):
        s.assignments.preview(s.successor, actor=s.workflow_owner)
    assert s.count() == 0
