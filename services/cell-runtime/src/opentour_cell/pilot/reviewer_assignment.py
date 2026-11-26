"""Signed workflow-owner assignments against actual saved owner settings.

An assignment records the owner's choice only. It cannot change a settings head,
release a proposal, or substitute for the custodian's separately signed save.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentour_cell.pilot import repair_bundle, repair_workflow
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.owner_settings import OwnerSettings, SettingsError, _need
from opentour_cell.pilot.readout import parse_manifest
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]
AUTHORITY = "opentour.pilot.owner-settings-authority.v2"
CLAIM = "opentour.pilot.reviewer-assignment.v1"
RECEIPT = "opentour.pilot.reviewer-assignment-receipt.v1"
HEX = re.compile(r"[0-9a-f]{64}")


def role_revision(authority: Json, organization: str, engagement: str) -> str:
    return digest(
        {
            "authority_revision": digest(authority),
            "organization": organization,
            "engagement": engagement,
            "workflow_owner": authority["workflow_owner"],
        }
    )


class ReviewerAssignments:
    def __init__(self, settings: OwnerSettings):
        self.settings = settings

    def _owner(self, actor: str) -> Json:
        a = self.settings._authority(actor)
        _need(actor == a["workflow_owner"], "workflow_owner_assignment_required")
        _need(
            a["schema_version"] == AUTHORITY
            and isinstance(a.get("workflow_owner_public_key_file"), str)
            and isinstance(a.get("workflow_owner_public_key_sha256"), str)
            and HEX.fullmatch(a["workflow_owner_public_key_sha256"]),
            "workflow_owner_signing_authority_required",
        )
        keys = []
        for prefix in ("", "workflow_owner_"):
            key = repair_bundle._read(
                self.settings.authority_file.parent
                / repair_workflow._name(a[prefix + "public_key_file"]),
                16384,
            )
            _need(
                repair_bundle._sha(key) == a[prefix + "public_key_sha256"],
                "assignment_trust_anchor_changed",
            )
            keys.append(
                repair_bundle._crypto(
                    ["pkey", "-pubin", "-outform", "DER"], key, "assignment_public_key_invalid"
                )
            )
        _need(
            a["workflow_owner"] == a["custodian"] or keys[0] != keys[1],
            "distinct_role_signing_keys_required",
        )
        return a

    def _current(self, db: sqlite3.Connection, authority: Json) -> Json:
        s = self.settings
        head = s._head(db)
        _need(head is not None and head["state"] == "active", "active_saved_settings_required")
        assert head is not None
        _need(
            head["authority_revision"] == digest(authority)
            and head["workflow_owner_role_revision"]
            == role_revision(authority, s.organization, s.engagement),
            "assignment_authority_epoch_changed",
        )
        row, spec = s.ledger._mandate(db, head["mandate_id"])
        s.ledger._active(row, spec, spec["delegate"])
        _need(row["revision"] == head["mandate_revision"], "settings_mandate_changed")
        live = s._live(head["policy"], discover=False)
        _need(
            live == {k: v for k, v in head["binding"].items() if k != "pair_revision"},
            "settings_live_binding_changed",
        )
        return head

    def _proposals(self, db: sqlite3.Connection, head: Json) -> list[Json]:
        """Resolve frozen objects from actual admitted runs, never caller-supplied paths."""
        s = self.settings
        result = []
        rows = db.execute(
            "WITH RECURSIVE lineage(id) AS (VALUES (?) UNION SELECT r.prior "
            "FROM renewals r JOIN lineage l ON r.successor=l.id WHERE r.scope=?) "
            "SELECT id,mandate FROM runs WHERE scope=? AND mandate IN "
            "(SELECT id FROM lineage) ORDER BY id",
            (head["mandate_id"], s.ledger.scope, s.ledger.scope),
        ).fetchall()
        saved = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT receipt FROM owner_settings_revisions WHERE scope=?", (s.ledger.scope,)
            )
        ]
        for row in rows:
            run_id = "run-" + digest([s.organization, row["mandate"], row["id"]])[:32]
            work = s.directory / "managed-outputs" / run_id
            if not (work / "run.json").exists():
                continue
            _, execution = s.ledger._mandate(db, row["mandate"])
            plan = repair_workflow._private_json(s.directory / ("cycle-plan-" + run_id + ".json"))
            run = repair_workflow._private_json(work / "run.json")
            _need(
                plan["organization"] == s.organization
                and plan["engagement"] == s.engagement
                and plan["standing_delegation"]
                == {
                    "mandate_id": row["mandate"],
                    "run_id": row["id"],
                    "delegate": execution["delegate"],
                }
                and run["run_id"] == run_id
                and run["plan_revision"] == digest(plan),
                "assignment_frozen_proposal_changed",
            )
            if run["outcome"] not in {"simple_repair", "model_candidate"}:
                continue
            review = repair_workflow._private_json(work / "review.json")
            audit = review["audit"]
            _need(
                run["review_digest"] == digest(review)
                and review["organization"] == s.organization
                and review["engagement"] == s.engagement
                and review["plan_revision"] == run["plan_revision"]
                and audit["run_revision"] == run["result_revision"]
                and isinstance(audit["candidate_digest"], str)
                and HEX.fullmatch(audit["candidate_digest"])
                and run["release_review"]["reviewer"] == execution["reviewer"],
                "assignment_frozen_proposal_changed",
            )
            prior = self._replacement(db, run_id)
            current_reviewer = prior["reviewer"] if prior else execution["reviewer"]
            completed = work / "release-decision.json"
            if completed.exists():
                decision = repair_workflow._private_json(completed)
                _need(
                    decision["organization"] == s.organization
                    and decision["run_id"] == run_id
                    and decision["run_revision"] == run["result_revision"]
                    and decision["artifact_sha"] == audit["candidate_digest"]
                    and decision["reviewer"] == current_reviewer
                    and decision["decision"] in {"approved", "changes_requested"},
                    "assignment_completed_receipt_changed",
                )
                continue
            previous = [
                r for r in saved if r["mandate_id"] == row["mandate"] and r["state"] == "active"
            ]
            _need(len(previous) == 1, "assignment_proposal_adoption_required")
            origin = previous[0]
            _need(origin["binding"] == head["binding"], "assignment_proposal_scope_changed")
            _need(current_reviewer == head["reviewer"], "assignment_proposal_adoption_required")
            result.append(
                {
                    "run_id": run_id,
                    "run_revision": run["result_revision"],
                    "artifact_sha": audit["candidate_digest"],
                    "plan_revision": run["plan_revision"],
                    "review_digest": run["review_digest"],
                    "intake_revision": plan["intake_revision"],
                    "dataset_revision": review["dataset_revision"],
                    "original_reviewer": origin["reviewer"],
                    "current_reviewer": current_reviewer,
                    "prior_review_generation": prior["generation"] if prior else origin["revision"],
                    "prior_review_state": prior["state"] if prior else "original",
                    "source_mandate": row["mandate"],
                }
            )
        return result

    @staticmethod
    def _choice_required(head: Json, proposals: list[Json], successor: str) -> bool:
        return successor != head["reviewer"] or any(
            p["prior_review_generation"] != head["revision"]
            or p["prior_review_state"] == "review_reauthorization_required"
            for p in proposals
        )

    def _replacement(self, db: sqlite3.Connection, run_id: str) -> Json | None:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_replacements'"
        ).fetchone():
            return None
        row = db.execute(
            "SELECT document FROM reviewer_replacements WHERE scope=? AND run=? "
            "ORDER BY rowid DESC LIMIT 1",
            (self.settings.ledger.scope, run_id),
        ).fetchone()
        return parse_manifest(str(row[0]).encode()) if row else None

    def _request_inventory(self, proposal: Json) -> Json:
        """Record private references for reconciliation; these rows grant no authority."""
        fingerprints = {}
        uncertain = False
        directory = self.settings.directory
        for path in sorted(directory.glob("zapier-approval-za-*-request.json")):
            wrapped = repair_workflow._private_json(path)
            request = wrapped.get("request")
            _need(isinstance(request, dict), "approval_request_inventory_unavailable")
            assert isinstance(request, dict)
            if request.get("run_id") != proposal["run_id"]:
                continue
            rid = request.get("request_id")
            _need(
                isinstance(rid, str)
                and re.fullmatch(r"za-[a-f0-9]{32}", rid)
                and path.name == "zapier-approval-" + rid + "-request.json"
                and request.get("engagement") == self.settings.engagement
                and request.get("run_revision") == proposal["run_revision"]
                and request.get("artifact_sha") == proposal["artifact_sha"]
                and isinstance(wrapped.get("seal"), str)
                and HEX.fullmatch(wrapped["seal"]),
                "approval_request_inventory_unavailable",
            )
            assert isinstance(rid, str)
            fingerprints[rid] = digest(wrapped)
            prefix = directory / ("zapier-approval-" + rid)
            if Path(str(prefix) + "-admitted.json").exists():
                transport = Path(str(prefix) + "-transport.json")
                uncertain |= (
                    not transport.exists()
                    or repair_workflow._private_json(transport).get("transport") != "accepted"
                )
        return {
            "expired_request_ids": sorted(fingerprints),
            "request_fingerprints": fingerprints,
            "remote_reconciliation_required": uncertain,
        }

    @contextmanager
    def serialized(self) -> Iterator[None]:
        """Use the existing approval lock before the engagement/mandate writers."""
        with self.settings._approval_serialized():
            yield

    def check_save(
        self, db: sqlite3.Connection, reference: str, policy: Json, *, actor: str
    ) -> Json:
        s = self.settings
        authority = s._authority(actor, save=True)
        _need(
            isinstance(reference, str) and HEX.fullmatch(reference),
            "saved_owner_assignment_required",
        )
        _need(
            db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_assignments'"
            ).fetchone(),
            "saved_owner_assignment_required",
        )
        row = db.execute(
            "SELECT packet FROM reviewer_assignments WHERE scope=? AND revision=?",
            (s.ledger.scope, reference),
        ).fetchone()
        _need(row is not None, "saved_owner_assignment_required")
        assert row is not None
        claim, a = self._verify(parse_manifest(str(row[0]).encode()), authority["workflow_owner"])
        head = self._current(db, a)
        _need(
            claim["prior_settings_revision"] == head["revision"]
            and claim["prior_reviewer_grant_revision"] == head["reviewer_grant_revision"]
            and claim["proposals"] == self._proposals(db, head),
            "assignment_revision_conflict",
        )
        expected = parse_manifest(canonical(head["policy"]).encode())
        expected["agreement"]["release_review"]["reviewer"] = claim["successor"]
        expected["mandate"].update(
            reviewer=claim["successor"],
            mandate_id=policy["mandate"]["mandate_id"],
            agreement_revision=digest(expected["agreement"]),
        )
        _need(expected == policy, "assignment_policy_scope_changed")
        return claim

    def preview_save(self, policy: Json, reference: str, *, actor: str) -> Json:
        s = self.settings
        a = s._authority(actor, save=True)
        p = s._policy(policy, a)
        with s.ledger._authorized_db(write=False) as db:
            self.check_save(db, reference, p, actor=actor)
            head = s._head(db)
        assert head is not None
        binding = s._live(p, discover=True)
        claim = {
            "schema_version": "opentour.pilot.owner-settings-save.v1",
            "organization": s.organization,
            "engagement": s.engagement,
            "actor": actor,
            "authority_revision": digest(a),
            "prior_revision": head["revision"],
            "prior_mandate": head["mandate_id"],
            "action": "replace_reviewer",
            "policy": p,
            "binding": binding,
            "assignment_revision": reference,
            "expires_at": min(int(time.time()) + 300, a["expires_at"]),
        }
        return {"claim": claim, "prior": head, "changed_fields": ["reviewer"]}

    def record_replacement(self, db: sqlite3.Connection, assignment: Json, receipt: Json) -> Json:
        """Called in the same mandate transaction as the actual signed settings head."""
        s = self.settings
        _need(s.authority_file.parent == s.directory, "replacement_authority_must_be_owner_local")
        db.execute(
            "CREATE TABLE IF NOT EXISTS reviewer_replacements "
            "(scope TEXT,generation TEXT,run TEXT,document TEXT,"
            "PRIMARY KEY(scope,generation,run))"
        )
        db.execute(
            "CREATE TABLE IF NOT EXISTS reviewer_reconciliation "
            "(scope TEXT,id TEXT,document TEXT,state TEXT,PRIMARY KEY(scope,id))"
        )
        records = []
        for proposal in assignment["proposals"]:
            document = {
                **proposal,
                **self._request_inventory(proposal),
                "organization": s.organization,
                "engagement": s.engagement,
                "generation": receipt["revision"],
                "assignment_revision": digest(assignment),
                "reviewer": receipt["reviewer"],
                "reviewer_grant_revision": receipt["reviewer_grant_revision"],
                "prior_reviewer_grant_revision": assignment["prior_reviewer_grant_revision"],
                "workflow_owner": assignment["actor"],
                "custodian": receipt["actor"],
                "workflow_owner_role_revision": receipt["workflow_owner_role_revision"],
                "authority_file": s.authority_file.name,
                "release_review": {
                    "provider": "zapier_slack",
                    **receipt["policy"]["agreement"]["release_review"],
                },
                "state": "fresh_successor_inspection_required",
            }
            intent = "review-reconcile-" + digest(document)
            db.execute(
                "INSERT INTO reviewer_replacements VALUES(?,?,?,?)",
                (s.ledger.scope, receipt["revision"], proposal["run_id"], canonical(document)),
            )
            db.execute(
                "INSERT INTO reviewer_reconciliation VALUES(?,?,?,'pending')",
                (s.ledger.scope, intent, canonical(document)),
            )
            records.append({"run_id": proposal["run_id"], "reconciliation_ref": intent})
        return {
            "assignment_revision": digest(assignment),
            "generation": receipt["revision"],
            "proposals": records,
            "state": "fresh_successor_inspection_required",
        }

    def carry_forward(self, db: sqlite3.Connection, previous: Json, receipt: Json) -> Json | None:
        """A signed same-role renewal retains the choice, but never the old inspection."""
        s = self.settings
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_replacements'"
        ).fetchone():
            return None
        rows = db.execute(
            "SELECT document FROM reviewer_replacements WHERE scope=? AND rowid IN "
            "(SELECT max(rowid) FROM reviewer_replacements WHERE scope=? GROUP BY run)",
            (s.ledger.scope, s.ledger.scope),
        ).fetchall()
        same_scope = all(
            previous[k] == receipt[k]
            for k in ("authority_revision", "reviewer", "reviewer_grant_revision", "binding")
        )
        eligible = {p["run_id"] for p in self._proposals(db, previous)} if same_scope else set()
        records = []
        for row in rows:
            old = parse_manifest(str(row[0]).encode())
            completed = s.directory / "managed-outputs" / old["run_id"] / "release-decision.json"
            if completed.exists():
                decision = repair_workflow._private_json(completed)
                _need(
                    all(
                        decision.get(k) == old[k]
                        for k in (
                            "organization",
                            "run_id",
                            "run_revision",
                            "artifact_sha",
                            "reviewer",
                        )
                    )
                    and decision.get("decision") in {"approved", "changes_requested"},
                    "assignment_completed_receipt_changed",
                )
                continue
            current = (
                same_scope
                and old["run_id"] in eligible
                and old["generation"] == previous["revision"]
                and old["state"] == "fresh_successor_inspection_required"
            )
            document = {
                **old,
                **self._request_inventory(old),
                "generation": receipt["revision"],
                "prior_review_generation": old["generation"],
                "continued_from_settings_revision": previous["revision"],
                "state": "fresh_successor_inspection_required"
                if current
                else "review_reauthorization_required",
            }
            intent = "review-reconcile-" + digest(document)
            db.execute(
                "INSERT INTO reviewer_replacements VALUES(?,?,?,?)",
                (s.ledger.scope, receipt["revision"], old["run_id"], canonical(document)),
            )
            db.execute(
                "INSERT INTO reviewer_reconciliation VALUES(?,?,?,'pending')",
                (s.ledger.scope, intent, canonical(document)),
            )
            records.append(
                {"run_id": old["run_id"], "reconciliation_ref": intent, "state": document["state"]}
            )
        if not records:
            return None
        return {
            "generation": receipt["revision"],
            "continued_from": previous["revision"],
            "proposals": records,
            "state": "current_grant_inspection_required",
        }

    def _operator(self, actor: str, db: sqlite3.Connection | None = None) -> Json:
        authority = self.settings._authority(actor)
        _need(
            actor in {authority["custodian"], authority["operator"]},
            "reconciliation_operator_required",
        )
        if db is None:
            with self.settings.ledger._db(write=False) as lookup:
                head = self.settings._head(lookup)
        else:
            head = self.settings._head(db)
        _need(
            head is not None and head["authority_revision"] == digest(authority),
            "current_settings_authority_required",
        )
        return authority

    def pending_reconciliations(self, *, actor: str, limit: int = 50) -> Json:
        """Read a bounded private queue; acknowledgments advance it without offset races."""
        self._operator(actor)
        _need(type(limit) is int and 1 <= limit <= 50, "invalid_reconciliation_limit")
        with self.settings.ledger._authorized_db(write=False) as db:
            self._operator(actor, db)
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_reconciliation'"
            ).fetchone():
                return {"pending_count": 0, "records": []}
            scope = self.settings.ledger.scope
            count = db.execute(
                "SELECT count(*) FROM reviewer_reconciliation WHERE scope=? AND state='pending'",
                (scope,),
            ).fetchone()[0]
            rows = db.execute(
                "SELECT id,document FROM reviewer_reconciliation "
                "WHERE scope=? AND state='pending' ORDER BY rowid LIMIT ?",
                (scope, limit),
            ).fetchall()
            return {
                "pending_count": count,
                "records": [
                    {
                        "id": row["id"],
                        "document": parse_manifest(str(row["document"]).encode()),
                        "document_sha256": digest(parse_manifest(str(row["document"]).encode())),
                    }
                    for row in rows
                ],
            }

    def acknowledge_reconciliation(
        self, reference: str, document_sha256: str, *, actor: str
    ) -> Json:
        """Confirm local projection only, never a remote send, vote or customer notice."""
        self._operator(actor)
        _need(
            isinstance(reference, str)
            and re.fullmatch(r"review-reconcile-[0-9a-f]{64}", reference)
            and isinstance(document_sha256, str)
            and HEX.fullmatch(document_sha256),
            "invalid_reconciliation_reference",
        )
        with self.settings.ledger._authorized_db() as db:
            self._operator(actor, db)
            _need(
                db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='reviewer_reconciliation'"
                ).fetchone(),
                "reconciliation_reference_unavailable",
            )
            scope = self.settings.ledger.scope
            row = db.execute(
                "SELECT document FROM reviewer_reconciliation WHERE scope=? AND id=?",
                (scope, reference),
            ).fetchone()
            _need(row is not None, "reconciliation_reference_unavailable")
            assert row is not None
            document = parse_manifest(str(row[0]).encode())
            _need(digest(document) == document_sha256, "reconciliation_document_changed")
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviewer_projection_receipts "
                "(scope TEXT,id TEXT,receipt TEXT,PRIMARY KEY(scope,id))"
            )
            prior = db.execute(
                "SELECT receipt FROM reviewer_projection_receipts WHERE scope=? AND id=?",
                (scope, reference),
            ).fetchone()
            if prior:
                return parse_manifest(str(prior[0]).encode())
            receipt = {
                "schema_version": "opentour.pilot.review-projection-receipt.v1",
                "organization": self.settings.organization,
                "engagement": self.settings.engagement,
                "reference": reference,
                "document_sha256": document_sha256,
                "actor": actor,
                "state": "projected",
                "provider_outcome": "not_qualified",
                "owner_notification": "not_qualified",
            }
            db.execute(
                "INSERT INTO reviewer_projection_receipts VALUES(?,?,?)",
                (scope, reference, canonical(receipt)),
            )
            db.execute(
                "UPDATE reviewer_reconciliation SET state='projected' WHERE scope=? AND id=?",
                (scope, reference),
            )
            return receipt

    def preview(self, successor: str, *, actor: str) -> Json:
        a = self._owner(actor)
        _need(successor in a["reviewers"], "eligible_successor_required")
        s = self.settings
        with s.ledger._authorized_db(write=False) as db:
            head = self._current(db, a)
            proposals = self._proposals(db, head)
            _need(self._choice_required(head, proposals, successor), "reviewer_already_current")
        claim = {
            "schema_version": CLAIM,
            "organization": s.organization,
            "engagement": s.engagement,
            "actor": actor,
            "authority_revision": digest(a),
            "workflow_owner_role_revision": head["workflow_owner_role_revision"],
            "prior_settings_revision": head["revision"],
            "prior_reviewer_grant_revision": head["reviewer_grant_revision"],
            "successor": successor,
            "proposals": proposals,
            "expires_at": min(int(time.time()) + 300, a["expires_at"]),
        }
        _need(len(canonical(claim).encode()) <= 64 * 1024, "assignment_inventory_too_large")
        return {"claim": claim, "state": "awaiting_owner_signature"}

    def _verify(self, packet: Json, actor: str) -> tuple[Json, Json]:
        _need(
            isinstance(packet, dict) and set(packet) == {"claim", "signature"},
            "signed_owner_assignment_required",
        )
        a, claim = self._owner(actor), packet["claim"]
        s = self.settings
        _need(
            isinstance(claim, dict)
            and set(claim)
            == {
                "schema_version",
                "organization",
                "engagement",
                "actor",
                "authority_revision",
                "workflow_owner_role_revision",
                "prior_settings_revision",
                "prior_reviewer_grant_revision",
                "successor",
                "proposals",
                "expires_at",
            }
            and claim["schema_version"] == CLAIM
            and (claim["organization"], claim["engagement"], claim["actor"])
            == (s.organization, s.engagement, actor)
            and claim["authority_revision"] == digest(a)
            and claim["workflow_owner_role_revision"]
            == role_revision(a, s.organization, s.engagement)
            and claim["successor"] in a["reviewers"],
            "assignment_signature_scope_mismatch",
        )
        _need(
            type(claim["expires_at"]) is int
            and time.time() < claim["expires_at"] <= min(a["expires_at"], int(time.time()) + 300),
            "assignment_expired",
        )
        try:
            key = repair_bundle._read(
                s.authority_file.parent
                / repair_workflow._name(a["workflow_owner_public_key_file"]),
                16384,
            )
            _need(
                repair_bundle._sha(key) == a["workflow_owner_public_key_sha256"],
                "assignment_trust_anchor_changed",
            )
            signature = base64.b64decode(packet["signature"], validate=True)
            _need(128 <= len(signature) <= 1024, "invalid_assignment_signature")
            with tempfile.TemporaryDirectory(dir=s.directory, prefix=".assignment-verify-") as tmp:
                pub, sig = Path(tmp) / "public.pem", Path(tmp) / "signature"
                repair_bundle._write_private(pub, key)
                repair_bundle._write_private(sig, signature)
                repair_bundle._crypto(
                    ["dgst", "-sha256", "-verify", str(pub), "-signature", str(sig)],
                    canonical(claim).encode(),
                    "invalid_assignment_signature",
                )
        except (binascii.Error, TypeError, repair_bundle.BundleError) as exc:
            raise SettingsError("invalid_assignment_signature") from exc
        return claim, a

    def assign(self, packet: Json, *, actor: str) -> Json:
        encoded = canonical(packet).encode()
        _need(len(encoded) <= 64 * 1024, "assignment_packet_too_large")
        packet = parse_manifest(encoded)
        claim, _ = self._verify(packet, actor)
        s = self.settings
        with s.ledger._authorized_db() as db:
            claim, a = self._verify(packet, actor)
            revision = digest(claim)
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_assignments'"
            ).fetchone():
                prior = db.execute(
                    "SELECT packet,receipt FROM reviewer_assignments WHERE scope=? AND revision=?",
                    (s.ledger.scope, revision),
                ).fetchone()
                if prior:
                    _need(
                        json.loads(prior["packet"])["claim"] == claim, "assignment_receipt_conflict"
                    )
                    return parse_manifest(str(prior["receipt"]).encode())
            head = self._current(db, a)
            _need(
                claim["prior_settings_revision"] == head["revision"]
                and claim["prior_reviewer_grant_revision"] == head["reviewer_grant_revision"]
                and self._choice_required(head, claim["proposals"], claim["successor"])
                and claim["proposals"] == self._proposals(db, head),
                "assignment_revision_conflict",
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS reviewer_assignments "
                "(scope TEXT, revision TEXT, packet TEXT, receipt TEXT, "
                "PRIMARY KEY(scope,revision))"
            )
            receipt = {
                "schema_version": RECEIPT,
                "organization": s.organization,
                "engagement": s.engagement,
                "assignment_revision": revision,
                "prior_settings_revision": head["revision"],
                "workflow_owner": actor,
                "workflow_owner_role_revision": claim["workflow_owner_role_revision"],
                "prior_reviewer_grant_revision": head["reviewer_grant_revision"],
                "successor": claim["successor"],
                "proposals": claim["proposals"],
                "state": "awaiting_custodian_save",
            }
            db.execute(
                "INSERT INTO reviewer_assignments VALUES(?,?,?,?)",
                (s.ledger.scope, revision, canonical(packet), canonical(receipt)),
            )
            return receipt


def current_review_authority(
    directory: Path,
    organization: str,
    engagement: str,
    run_id: str,
    run_revision: str,
    artifact_sha: str,
) -> Json | None:
    """Resolve the actual current saved grant; callers separately authenticate the viewer.

    Effect callers must hold their engagement writer through this check and the
    durable effect. A missing replacement returns None for the original route; a
    stale or invalid existing replacement raises and must never fall back.
    """
    store = LocalTrainingStore(directory / "training.sqlite3", organization, existing_only=True)
    with store.engagement_guard(engagement_id=engagement, read_only=True):
        if not os.path.lexists(directory / "learning-mandates.sqlite3"):
            return None
    ledger = LearningMandates(directory, organization, engagement)
    with ledger._db(write=False) as db:
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='reviewer_replacements'"
        ).fetchone():
            return None
        row = db.execute(
            "SELECT document FROM reviewer_replacements WHERE scope=? AND run=? "
            "ORDER BY rowid DESC LIMIT 1",
            (ledger.scope, run_id),
        ).fetchone()
    if row is None:
        return None
    document = parse_manifest(str(row[0]).encode())
    _need(
        (
            document["organization"],
            document["engagement"],
            document["run_id"],
            document["run_revision"],
            document["artifact_sha"],
        )
        == (organization, engagement, run_id, run_revision, artifact_sha),
        "review_replacement_scope_mismatch",
    )
    _need(
        document["state"] == "fresh_successor_inspection_required",
        "review_reauthorization_required",
    )
    settings = OwnerSettings(
        directory,
        organization,
        engagement,
        authority_file=directory / repair_workflow._name(document["authority_file"]),
    )
    service = ReviewerAssignments(settings)
    authority = service._owner(document["workflow_owner"])
    with settings.ledger._authorized_db(write=False) as db:
        head = service._current(db, authority)
        _need(
            service._replacement(db, run_id) == document
            and head["revision"] == document["generation"]
            and head["reviewer"] == document["reviewer"]
            and head["reviewer_grant_revision"] == document["reviewer_grant_revision"],
            "review_replacement_no_longer_current",
        )
        work = directory / "managed-outputs" / run_id
        run = repair_workflow._private_json(work / "run.json")
        review = repair_workflow._private_json(work / "review.json")
        _need(
            run["result_revision"] == run_revision
            and run["review_digest"] == document["review_digest"] == digest(review)
            and run["plan_revision"] == document["plan_revision"]
            and review["organization"] == organization
            and review["engagement"] == engagement
            and review["dataset_revision"] == document["dataset_revision"]
            and review["audit"]["candidate_digest"] == artifact_sha,
            "review_replacement_evidence_changed",
        )
        return document


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "assign"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--engagement", required=True)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--successor")
    parser.add_argument("--input", type=Path)
    args = parser.parse_args(argv)
    try:
        service = ReviewerAssignments(
            OwnerSettings(
                args.directory, args.organization, args.engagement, authority_file=args.authority
            )
        )
        if args.command == "preview":
            _need(args.input is None and isinstance(args.successor, str), "successor_required")
            result = service.preview(args.successor, actor=args.actor)
        else:
            _need(args.successor is None and args.input is not None, "assignment_input_required")
            result = service.assign(repair_workflow._private_json(args.input), actor=args.actor)
        print(canonical(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        parser.exit(2, "reviewer_assignment_request_held\n")


if __name__ == "__main__":
    raise SystemExit(main())
