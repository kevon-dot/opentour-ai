"""Private custodian-signed settings, atomic mandate succession and worker handoff.

The hosting private page supplies an authenticated actor; a pinned public key also
verifies every save. Neither an actor string nor a model/tool call grants authority.
The key/role file is installed by the local administrator, never accepted in a packet.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import sqlite3
import tempfile
import time
from contextlib import ExitStack, nullcontext
from pathlib import Path
from typing import Any

from opentour_cell.pilot import autonomous_cycle, repair_bundle, repair_workflow
from opentour_cell.pilot import workplace_context as workplace
from opentour_cell.pilot.learning_mandate import LearningMandates, _normalized
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import parse_manifest
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]
AUTHORITY = "opentour.pilot.owner-settings-authority.v1"
REPLACEMENT_AUTHORITY = "opentour.pilot.owner-settings-authority.v2"
CLAIM = "opentour.pilot.owner-settings-save.v1"
RECEIPT = "opentour.pilot.owner-settings-receipt.v1"
EXCLUSIONS = [
    "changed_policy_or_schema",
    "fields_outside_grant",
    "future_decision_features",
    "missing_outcome",
    "contradictory_outcomes",
    "ambiguous_outcomes",
    "reused_outcome",
    "unapproved_adjudicator",
    "outcome_unavailable",
    "facts_unavailable_at_decision",
    "inconsistent_outcome_time",
    "verification_after_cutoff",
    "group_crosses_split",
    "development_label_after_split",
]


class SettingsError(ValueError):
    """A public-safe refusal; never includes customer input or signature bytes."""


def _need(check: object, reason: str) -> None:
    if not check:
        raise SettingsError(reason)


def _read(path: Path) -> Json:
    return parse_manifest(repair_bundle._read(path, 64 * 1024))


def _publish_worker(path: Path, value: Json) -> None:
    """Publish complete bytes under the mandate writer; a crash leaves no linked temp."""
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".settings-stage-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((canonical(value) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        if os.path.lexists(path):
            _need(_read(path) == value, "settings_worker_artifact_changed")
        else:
            # All settings exporters hold the same SQLite writer throughout this operation.
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)


class OwnerSettings:
    def __init__(
        self, directory: Path, organization: str, engagement: str, *, authority_file: Path
    ):
        self.directory = directory
        self.organization, self.engagement = organization, engagement
        self.authority_file = authority_file
        repair_bundle._private_directory(directory)
        self.store = LocalTrainingStore(
            directory / "training.sqlite3", organization, existing_only=True
        )
        self.ledger = LearningMandates(directory, organization, engagement)
        # This adds metadata to the existing ledger; it never creates another budget store.
        with self.ledger._db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS owner_settings_heads "
                "(scope TEXT PRIMARY KEY, revision TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS owner_settings_revisions "
                "(scope TEXT, revision TEXT, packet TEXT, receipt TEXT, "
                "PRIMARY KEY(scope,revision))"
            )

    def _authority(self, actor: str, *, save: bool = False) -> Json:
        a = _read(self.authority_file)
        replacement_keys = {"workflow_owner_public_key_file", "workflow_owner_public_key_sha256"}
        replacement = a.get("schema_version") == REPLACEMENT_AUTHORITY
        _need(
            set(a) - (replacement_keys if replacement else set())
            == {
                "schema_version",
                "organization",
                "engagement",
                "custodian",
                "operator",
                "reviewers",
                "public_key_file",
                "public_key_sha256",
                "expires_at",
                "revoked",
                "initial_mandate",
                "workflow_owner",
            }
            and a["schema_version"] in {AUTHORITY, REPLACEMENT_AUTHORITY}
            and (not replacement or replacement_keys <= set(a))
            and (a["organization"], a["engagement"]) == (self.organization, self.engagement),
            "settings_authority_scope_mismatch",
        )
        _need(
            isinstance(a["reviewers"], list)
            and bool(a["reviewers"])
            and all(
                isinstance(v, str) and v
                for v in [a["custodian"], a["operator"], a["workflow_owner"], *a["reviewers"]]
            )
            and a["custodian"] != a["operator"]
            and not {a["custodian"], a["operator"]}.intersection(a["reviewers"]),
            "settings_distinct_roles_required",
        )
        _need(
            a["revoked"] is False
            and type(a["expires_at"]) is int
            and time.time() < a["expires_at"],
            "settings_authority_unavailable",
        )
        _need(
            actor == a["custodian"]
            if save
            else actor in {a["custodian"], a["operator"], a["workflow_owner"], *a["reviewers"]},
            "settings_custodian_required" if save else "settings_actor_not_authorized",
        )
        return a

    def _head(self, db: sqlite3.Connection) -> Json | None:
        row = db.execute(
            "SELECT r.receipt FROM owner_settings_heads h JOIN owner_settings_revisions r "
            "ON r.scope=h.scope AND r.revision=h.revision WHERE h.scope=?",
            (self.ledger.scope,),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def inspect(self, *, actor: str) -> Json:
        self._authority(actor)
        with self.ledger._db(write=False) as db:
            head = self._head(db)
            return {
                "current": head,
                "usage": self.ledger._usage(db, head["mandate_id"]) if head else None,
            }

    def _policy(self, p: Json, a: Json) -> Json:
        _need(
            isinstance(p, dict)
            and set(p) == {"mandate", "agreement", "connection_file", "currency"},
            "invalid_settings_policy",
        )
        _need(p["currency"] == "USD", "unsupported_settings_currency")
        spec = _normalized(p["mandate"])
        agreement = p["agreement"]
        _need(
            isinstance(agreement, dict)
            and set(agreement)
            == {
                "incumbent_revision",
                "terms",
                "rule",
                "handoff",
                "release_review",
                "slack_review",
                "connection_revision",
            },
            "invalid_standing_agreement",
        )
        _need(
            spec["owner_id"] == a["custodian"]
            and spec["delegate"] == a["operator"]
            and spec["reviewer"] in a["reviewers"]
            and spec["reviewer"] != spec["owner_id"]
            and time.time() < spec["expires_at"] <= a["expires_at"]
            and spec["agreement_revision"] == digest(agreement),
            "settings_roles_or_agreement_mismatch",
        )
        release, slack = agreement["release_review"], agreement["slack_review"]
        from opentour_cell.pilot.slack_adapter import _slack_id

        _need(
            isinstance(release, dict)
            and isinstance(slack, dict)
            and set(release) - {"provider"} == {"reviewer", "team", "channel", "audience"}
            and release.get("provider", "zapier_slack") in {"slack", "zapier_slack"}
            and _slack_id(release.get("reviewer"), "UW")
            and _slack_id(release.get("team"), "T")
            and _slack_id(release.get("channel"), "CG")
            and (
                _slack_id(release.get("audience"), "UWG")
                or release.get("audience") == release.get("channel")
            )
            and release.get("reviewer") == spec["reviewer"]
            and release.get("audience") == spec["audience"] == spec["destination"]
            and all(slack.get(k) == release.get(k) for k in ("team", "channel", "audience"))
            and type(slack.get("expires_at")) is int
            and time.time() < slack["expires_at"] <= spec["expires_at"],
            "settings_private_destination_mismatch",
        )
        repair_workflow._publication_permission(
            {"slack_review": slack, "audience": spec["audience"]}
        )
        name = repair_workflow._name(p["connection_file"])
        return {
            "mandate": spec,
            "agreement": agreement,
            "connection_file": name,
            "currency": p["currency"],
        }

    def _live(self, p: Json, *, discover: bool) -> Json:
        spec, agreement = p["mandate"], p["agreement"]
        path = self.directory / p["connection_file"]
        cfg = workplace._live(self.store, path, agreement["connection_revision"])
        scope = cfg["scope"]
        _need(
            scope["owner_id"] == spec["owner_id"]
            and scope["workflow"] == spec["workflow"]
            and scope["field"] == spec["field"]
            and sorted(scope["permitted_features"]) == spec["fields"]
            and spec["connectors"] == [cfg["reader"]["provider"]]
            and spec["expires_at"] <= scope["expires_at"],
            "settings_outside_live_source_grant",
        )
        result = {
            "connection_revision": digest(cfg),
            "grant_revision": digest(scope),
            "data_policy": {
                k: scope[k] for k in ("as_of", "split", "retention_terms", "adjudicators")
            },
            "correction_authority": cfg["roles"]["correction"]["authority"],
        }
        result["data_policy"]["exclusions"] = EXCLUSIONS
        if discover:
            found = workplace.discover_workplace(
                self.store,
                path,
                expected_connection_revision=digest(cfg),
                max_total_rows=spec["max_rows_per_run"],
            )
            _need(
                all(
                    found["sources"][role]["source_id"] == spec[role + "_source"]
                    for role in workplace.ROLES
                ),
                "settings_selected_pair_changed",
            )
            result["pair_revision"] = found["pair_revision"]
        return result

    def preview(
        self,
        policy: Json | None,
        *,
        actor: str,
        action: str = "save",
        assignment_revision: str | None = None,
    ) -> Json:
        if action == "replace_reviewer":
            from opentour_cell.pilot.reviewer_assignment import ReviewerAssignments

            _need(
                isinstance(policy, dict) and isinstance(assignment_revision, str),
                "saved_owner_assignment_required",
            )
            assert isinstance(policy, dict) and isinstance(assignment_revision, str)
            return ReviewerAssignments(self).preview_save(policy, assignment_revision, actor=actor)
        _need(assignment_revision is None, "unexpected_owner_assignment")
        a = self._authority(actor, save=True)
        _need(action in {"save", "renew", "pause", "revoke"}, "invalid_settings_action")
        with self.ledger._authorized_db(write=False) as db:
            _need(self.ledger._owner(db) == a["custodian"], "settings_owner_mismatch")
            head = self._head(db)
            prior_mandate = head["mandate_id"] if head else a["initial_mandate"]
            if head is None and prior_mandate is None:
                _need(
                    not db.execute(
                        "SELECT 1 FROM mandates WHERE scope=?", (self.ledger.scope,)
                    ).fetchone(),
                    "settings_existing_mandate_binding_required",
                )
            if prior_mandate is not None:
                _, prior_spec = self.ledger._mandate(db, prior_mandate)
            else:
                prior_spec = None
        if action in {"pause", "revoke"}:
            _need(head is not None and policy is None, "saved_settings_required")
            assert head is not None
            p, binding = head["policy"], head["binding"]
        else:
            _need(action != "renew" or head is not None, "saved_settings_required")
            p = self._policy(policy, a)  # type: ignore[arg-type]
            _need(
                not prior_spec or prior_spec["reviewer"] == p["mandate"]["reviewer"],
                "reviewer_replacement_settings_required",
            )
            binding = self._live(p, discover=True)
        claim = {
            "schema_version": CLAIM,
            "organization": self.organization,
            "engagement": self.engagement,
            "actor": a["custodian"],
            "authority_revision": digest(a),
            "prior_revision": head["revision"] if head else None,
            "prior_mandate": prior_mandate,
            "action": action,
            "policy": p,
            "binding": binding,
            "expires_at": min(int(time.time()) + 300, a["expires_at"]),
        }
        return {
            "claim": claim,
            "prior": head,
            "changed_fields": sorted(
                k
                for k in p["mandate"]
                if not head or p["mandate"][k] != head["policy"]["mandate"].get(k)
            ),
        }

    def _verify(self, packet: Json, actor: str) -> tuple[Json, Json]:
        _need(
            isinstance(packet, dict) and set(packet) == {"claim", "signature"},
            "signed_settings_save_required",
        )
        claim = packet["claim"]
        a = self._authority(actor, save=True)
        _need(
            isinstance(claim, dict)
            and set(claim)
            - ({"assignment_revision"} if claim.get("action") == "replace_reviewer" else set())
            == {
                "schema_version",
                "organization",
                "engagement",
                "actor",
                "authority_revision",
                "prior_revision",
                "prior_mandate",
                "action",
                "policy",
                "binding",
                "expires_at",
            }
            and claim["schema_version"] == CLAIM
            and (
                claim.get("action") != "replace_reviewer"
                or isinstance(claim.get("assignment_revision"), str)
            )
            and (claim["organization"], claim["engagement"], claim["actor"])
            == (self.organization, self.engagement, a["custodian"])
            and claim["authority_revision"] == digest(a),
            "settings_signature_scope_mismatch",
        )
        _need(
            type(claim["expires_at"]) is int
            and time.time() < claim["expires_at"] <= min(a["expires_at"], int(time.time()) + 300),
            "settings_preview_expired",
        )
        try:
            key = repair_bundle._read(
                self.authority_file.parent / repair_workflow._name(a["public_key_file"]), 16384
            )
            _need(
                repair_bundle._sha(key) == a["public_key_sha256"], "settings_trust_anchor_changed"
            )
            signature = base64.b64decode(packet["signature"], validate=True)
            _need(128 <= len(signature) <= 1024, "invalid_settings_signature")
            with tempfile.TemporaryDirectory(dir=self.directory, prefix=".settings-verify-") as tmp:
                pub, sig = Path(tmp) / "public.pem", Path(tmp) / "signature"
                repair_bundle._write_private(pub, key)
                repair_bundle._write_private(sig, signature)
                repair_bundle._crypto(
                    ["dgst", "-sha256", "-verify", str(pub), "-signature", str(sig)],
                    canonical(claim).encode(),
                    "invalid_settings_signature",
                )
        except (binascii.Error, TypeError, repair_bundle.BundleError) as exc:
            raise SettingsError("invalid_settings_signature") from exc
        return claim, a

    def save(self, packet: Json, *, actor: str) -> Json:
        encoded = canonical(packet).encode()
        _need(len(encoded) <= 64 * 1024, "settings_packet_too_large")
        packet = parse_manifest(encoded)  # Freeze caller-owned objects before signature validation.
        claim, authority = self._verify(packet, actor)
        action, p = claim["action"], claim["policy"]
        _need(
            action in {"save", "renew", "pause", "revoke", "replace_reviewer"},
            "invalid_settings_action",
        )
        changing = action in {"save", "renew", "replace_reviewer"}
        replacement = None
        serialization = None
        if changing and authority["schema_version"] == REPLACEMENT_AUTHORITY:
            from opentour_cell.pilot.reviewer_assignment import ReviewerAssignments

            serialization = ReviewerAssignments(self)
        if action == "replace_reviewer":
            from opentour_cell.pilot.reviewer_assignment import ReviewerAssignments

            replacement = ReviewerAssignments(self)
        if changing:
            p = self._policy(p, authority)
            _need(p == claim["policy"], "noncanonical_settings_policy")
            _need(self._live(p, discover=True) == claim["binding"], "settings_live_binding_changed")
        revision = digest(claim)
        # All v2 changing saves take approval -> training -> mandate -> lease,
        # even before replacement history exists; never decide from a stale ledger read.
        # Commit the settings transaction
        # before releasing the lease. The empty stack changes no acquisition order.
        with (
            ExitStack() as guards,
            serialization.serialized() if serialization else nullcontext(),
            self.ledger._authorized_db() as db,
        ):
            # Repeat authentication and revocation after acquiring the admission writer lock.
            self._verify(packet, actor)
            _need(self.ledger._owner(db) == actor, "settings_owner_mismatch")
            found = db.execute(
                "SELECT packet,receipt FROM owner_settings_revisions WHERE scope=? AND revision=?",
                (self.ledger.scope, revision),
            ).fetchone()
            if found:
                _need(json.loads(found["packet"])["claim"] == claim, "settings_receipt_conflict")
                return parse_manifest(found["receipt"].encode())
            head = self._head(db)
            _need(
                claim["prior_revision"] == (head["revision"] if head else None)
                and claim["prior_mandate"]
                == (head["mandate_id"] if head else authority["initial_mandate"]),
                "settings_revision_conflict",
            )
            prior = claim["prior_mandate"]
            prior_spec = self.ledger._mandate(db, prior)[1] if prior else None
            if changing:
                live = self._live(p, discover=False)
                _need(
                    live == {k: v for k, v in claim["binding"].items() if k != "pair_revision"},
                    "settings_live_binding_changed",
                )
                cfg = workplace._live(
                    self.store,
                    self.directory / p["connection_file"],
                    p["agreement"]["connection_revision"],
                )
                guards.enter_context(workplace._claim_lease(self.store, cfg, False)())
                if replacement:
                    assignment = replacement.check_save(
                        db, claim["assignment_revision"], p, actor=actor
                    )
                else:
                    _need(
                        not prior_spec or prior_spec["reviewer"] == p["mandate"]["reviewer"],
                        "reviewer_replacement_settings_required",
                    )
                _need(prior != p["mandate"]["mandate_id"], "fresh_mandate_identifier_required")
                _need(
                    not db.execute(
                        "SELECT 1 FROM mandates WHERE scope=? AND id=?",
                        (self.ledger.scope, p["mandate"]["mandate_id"]),
                    ).fetchone(),
                    "fresh_mandate_identifier_required",
                )
                if prior is None:
                    _need(
                        not db.execute(
                            "SELECT 1 FROM mandates WHERE scope=?", (self.ledger.scope,)
                        ).fetchone(),
                        "settings_existing_mandate_binding_required",
                    )
            else:
                _need(
                    head is not None
                    and p == head["policy"]
                    and claim["binding"] == head["binding"],
                    "settings_revision_conflict",
                )
            if prior is not None:
                self.ledger._mandate(db, prior)
                db.execute(
                    "UPDATE mandates SET withdrawn=coalesce(withdrawn,?) WHERE scope=? AND id=?",
                    (int(time.time()), self.ledger.scope, prior),
                )
            if changing:
                mandate = (
                    self.ledger._reauthorize(db, prior, p["mandate"], actor)
                    if prior
                    else self.ledger._enroll(db, p["mandate"], actor)
                )
                mid, mrev = mandate["mandate_id"], mandate["mandate_revision"]
            else:
                assert head is not None  # Validated against the signed prior revision above.
                mid, mrev = head["mandate_id"], head["mandate_revision"]
            receipt = {
                "schema_version": RECEIPT,
                "organization": self.organization,
                "engagement": self.engagement,
                "revision": revision,
                "prior_revision": claim["prior_revision"],
                "prior_mandate": prior,
                "actor": actor,
                "workflow_owner": authority["workflow_owner"],
                "workflow_owner_role_revision": digest(
                    {
                        "authority_revision": claim["authority_revision"],
                        "organization": self.organization,
                        "engagement": self.engagement,
                        "workflow_owner": authority["workflow_owner"],
                    }
                ),
                "reviewer": p["mandate"]["reviewer"],
                "reviewer_grant_revision": digest(
                    {
                        "authority_revision": claim["authority_revision"],
                        "organization": self.organization,
                        "engagement": self.engagement,
                        "workflow": p["mandate"]["workflow"],
                        "reviewer": p["mandate"]["reviewer"],
                        "scope": p["agreement"]["release_review"],
                    }
                ),
                "action": action,
                "state": "active" if changing else action,
                "mandate_id": mid,
                "mandate_revision": mrev,
                "policy": p,
                "binding": claim["binding"],
                "authority_revision": claim["authority_revision"],
                "usage_at_save": self.ledger._usage(db, mid),
                "prior_treatment": {
                    "admission": "denied",
                    "release": "denied",
                    "fits": "withdrawal_reconciliation_required",
                    "artifacts": "retained_private_under_existing_terms",
                }
                if prior
                else None,
            }
            if replacement:
                receipt["reviewer_replacement"] = replacement.record_replacement(
                    db, assignment, receipt
                )
            elif changing and head is not None:
                # The import is needed only when the private ledger has replacement history.
                if db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' "
                    "AND name='reviewer_replacements'"
                ).fetchone():
                    from opentour_cell.pilot.reviewer_assignment import ReviewerAssignments

                    continued = ReviewerAssignments(self).carry_forward(db, head, receipt)
                    if continued is not None:
                        receipt["reviewer_replacement"] = continued
            db.execute(
                "INSERT INTO owner_settings_revisions VALUES(?,?,?,?)",
                (self.ledger.scope, revision, canonical(packet), canonical(receipt)),
            )
            db.execute(
                "INSERT INTO owner_settings_heads VALUES(?,?) ON CONFLICT(scope) "
                "DO UPDATE SET revision=excluded.revision",
                (self.ledger.scope, revision),
            )
            return receipt

    def reconcile(self, receipt: Json, *, actor: str) -> Json:
        """Retry local running-fit cancellation after durable denial; never refunds usage."""
        self._authority(actor, save=True)
        with self.ledger._db(write=False) as db:
            saved = db.execute(
                "SELECT receipt FROM owner_settings_revisions WHERE scope=? AND revision=?",
                (self.ledger.scope, receipt.get("revision")),
            ).fetchone()
            _need(saved and json.loads(saved[0]) == receipt, "settings_receipt_mismatch")
        if receipt["prior_mandate"] is None:
            return {"state": "not_required"}
        return self.ledger.withdraw(receipt["prior_mandate"], actor=actor)

    def worker_config(self, receipt: Json, *, actor: str) -> Json:
        a = self._authority(actor)
        _need(actor in {a["custodian"], a["operator"]}, "settings_operator_required")
        with self.ledger._authorized_db() as db:
            a = self._authority(actor)
            _need(actor in {a["custodian"], a["operator"]}, "settings_operator_required")
            head = self._head(db)
            _need(head == receipt and head is not None, "stale_settings_receipt")
            assert head is not None
            _need(head["authority_revision"] == digest(a), "settings_authority_changed")
            _need(head["state"] == "active", "settings_not_active")
            spec = head["policy"]["mandate"]
            row, stored = self.ledger._mandate(db, head["mandate_id"])
            self.ledger._active(row, stored, spec["delegate"])
            _need(row["revision"] == head["mandate_revision"], "settings_mandate_changed")
            self._live(head["policy"], discover=False)
            # Immutable filenames make a crash/retry safe; no mutable worker pointer is written.
            name = "settings-agreement-" + spec["agreement_revision"] + ".json"
            agreement = head["policy"]["agreement"]
            path = self.directory / name
            if path.exists():
                _need(_read(path) == agreement, "settings_worker_artifact_changed")
            else:
                _publish_worker(path, agreement)
            cfg = {
                "schema_version": autonomous_cycle.SCHEMA,
                "organization": self.organization,
                "engagement": self.engagement,
                "mandate_id": spec["mandate_id"],
                "delegate": spec["delegate"],
                "connector": spec["connectors"][0],
                "connection_file": head["policy"]["connection_file"],
                "agreement_file": name,
            }
            target = self.directory / ("settings-cycle-" + head["revision"] + ".json")
            if target.exists():
                _need(_read(target) == cfg, "settings_worker_artifact_changed")
            else:
                _publish_worker(target, cfg)
            return {"revision": head["revision"], "config": cfg, "config_file": str(target)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preview", "save", "status", "worker", "reconcile"))
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--engagement", required=True)
    parser.add_argument("--authority", type=Path, required=True)
    parser.add_argument("--actor", required=True)
    parser.add_argument("--input", type=Path)
    parser.add_argument(
        "--action", choices=("save", "renew", "pause", "revoke", "replace_reviewer"), default="save"
    )
    parser.add_argument("--assignment-revision")
    args = parser.parse_args(argv)
    try:
        service = OwnerSettings(
            args.directory, args.organization, args.engagement, authority_file=args.authority
        )
        value = _read(args.input) if args.input else None
        if args.command == "preview":
            result = service.preview(
                value,
                actor=args.actor,
                action=args.action,
                assignment_revision=args.assignment_revision,
            )
        elif args.command == "status":
            result = service.inspect(actor=args.actor)
        else:
            _need(isinstance(value, dict), "settings_input_required")
            assert isinstance(value, dict)
            method = {
                "save": service.save,
                "worker": service.worker_config,
                "reconcile": service.reconcile,
            }[args.command]
            result = method(value, actor=args.actor)
        print(canonical(result))
        return 0
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        parser.exit(2, "owner_settings_request_held\n")


if __name__ == "__main__":
    raise SystemExit(main())
