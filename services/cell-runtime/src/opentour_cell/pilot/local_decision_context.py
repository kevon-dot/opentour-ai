"""Read explicitly granted, pinned application observations on the private worker."""

from __future__ import annotations

import hmac
import json
import os
import re
import time
from dataclasses import asdict
from typing import Any

from opentour_cell.pilot import simple_repair
from opentour_cell.pilot.intake_mcp import IntakePrincipal, require_ref
from opentour_cell.pilot.live_authority import PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence, LiveScope, LiveSnapshot, VerifiedRoot
from opentour_cell.pilot.live_readiness import ObservedDecision
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _has_table, _open_db
from opentour_cell.pilot.text_classifier import TrainingError

GRANT = "opentour.pilot.decision-context-grant.v1"
BODY = "opentour.pilot.observed-decision-context.v1"
_BASENAME = r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json"
_KEYS = {
    "schema_version",
    "grant_ref",
    "authority_revision",
    "tenant",
    "workflow_ref",
    "engagement_epoch",
    "worker_ref",
    "resource",
    "grant_revision",
    "source_revision",
    "deciding_namespace",
    "allowed_fields",
    "expires_at",
    "body",
}
_ROW = {
    "decision_id",
    "decision_revision",
    "case_id",
    "family_id",
    "input_ref",
    "action_ref",
    "policy_ref",
    "input_fidelity",
    "receipt_ref",
    "features",
    "incumbent",
}


def _require(value: object) -> None:
    if not value:
        raise TrainingError("decision_context_unavailable")


class PrivateDecisionContext:
    """Configuration selects a grant. Only its current custodian signature permits reads.

    Caller holds the existing live/engagement guard. Metadata checks do not open
    the raw context body, including when a previous assessment is disclosed.
    """

    def __init__(
        self,
        authority: PrivateLiveAuthority,
        worker: IntakePrincipal,
        scope: LiveScope,
        grant_file: str,
    ):
        _require(re.fullmatch(_BASENAME, grant_file))
        self.authority, self.worker, self.scope = authority, worker, scope
        self.path = authority.directory / grant_file

    def _grant(self, snapshot: LiveSnapshot) -> dict[str, Any]:
        scope = self.scope
        _require(scope.can_ingest and scope.can_inspect)
        _require(
            (
                snapshot.tenant,
                snapshot.workflow_ref,
                snapshot.engagement_epoch,
                snapshot.dataset_ref,
                snapshot.dataset_revision,
            )
            == (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
                scope.dataset_ref,
                scope.dataset_revision,
            )
        )
        # Current signed metadata is re-read; acquiring a second training writer is unnecessary.
        current = self.authority._document()
        _require(digest(current) == scope.authority_revision)
        _require(self.worker.tenant == scope.tenant)
        _require({"intake", "inspect"} <= set(current["actors"].get(self.worker.actor, [])))
        envelope = _read_private(self.path, 32768)
        _require(set(envelope) == {"grant", "seal_sha256"})
        grant, signature = envelope["grant"], envelope["seal_sha256"]
        _require(isinstance(grant, dict) and set(grant) == _KEYS)
        _require(isinstance(signature, str) and re.fullmatch(r"[0-9a-f]{64}", signature))
        key = _read_private(self.authority.directory / "live-authority-key.json", 4096)["key"]
        _require(
            hmac.compare_digest(
                signature,
                hmac.digest(bytes.fromhex(key), canonical(grant).encode(), "sha256").hex(),
            )
        )
        _require(grant["schema_version"] == GRANT)
        _require(
            (
                grant["tenant"],
                grant["workflow_ref"],
                grant["engagement_epoch"],
                grant["worker_ref"],
                grant["authority_revision"],
            )
            == (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
                self.worker.actor,
                scope.authority_revision,
            )
        )
        require_ref(grant["grant_ref"])
        _require(
            not os.path.lexists(
                self.authority.directory / f"decision-context-withdrawn-{grant['grant_ref']}.json"
            )
        )
        _require(type(grant["expires_at"]) is int and int(time.time()) < grant["expires_at"])
        _require(
            any(
                (g.resource, g.revision, g.source_revision, g.deciding_namespace)
                == (
                    grant["resource"],
                    grant["grant_revision"],
                    grant["source_revision"],
                    grant["deciding_namespace"],
                )
                and g.kind == "decision"
                for g in scope.grants
            )
        )
        fields = grant["allowed_fields"]
        _require(isinstance(fields, list) and 0 < len(fields) <= 64)
        _require(all(isinstance(f, str) for f in fields) and len(set(fields)) == len(fields))
        for field in fields:
            simple_repair.identifier(field)
        pin = grant["body"]
        _require(isinstance(pin, dict) and set(pin) == {"file", "sha256"})
        _require(isinstance(pin["file"], str) and re.fullmatch(_BASENAME, pin["file"]))
        _require(isinstance(pin["sha256"], str) and re.fullmatch(r"[0-9a-f]{64}", pin["sha256"]))
        _require(self.authority.directory / pin["file"] != self.path)
        return dict(grant)

    def revision(self, snapshot: LiveSnapshot) -> str:
        return digest(self._grant(snapshot))

    def _has_origin(self, root: VerifiedRoot, grant: dict[str, Any]) -> bool:
        """Only normalized commit metadata is read here, before any raw input."""
        live = LiveEvidence(self.authority.directory, self.authority)
        scope = self.scope
        expected = next(g for g in scope.grants if g.resource == grant["resource"])
        with _open_db(live.path) as db:
            _require(db is not None)
            assert db is not None
            rows = db.execute(
                "SELECT seq,binding_ref,payload FROM live_events WHERE tenant=? AND workflow=? "
                "AND epoch=? AND dataset_ref=? AND dataset_revision=? AND resource=?",
                (
                    scope.tenant,
                    scope.workflow_ref,
                    scope.engagement_epoch,
                    scope.dataset_ref,
                    scope.dataset_revision,
                    grant["resource"],
                ),
            ).fetchall()
            for row in rows:
                event = json.loads(row["payload"])
                if event.get("kind") != "DecisionRecorded" or any(
                    event.get(key) != getattr(root, key)
                    for key in (
                        "decision_id",
                        "decision_revision",
                        "case_id",
                        "family_id",
                        "input_ref",
                        "action_ref",
                        "policy_ref",
                        "input_fidelity",
                    )
                ):
                    continue
                references = {row["binding_ref"]}
                if _has_table(db, "live_event_origins"):
                    references.update(
                        r[0]
                        for r in db.execute(
                            "SELECT binding_ref FROM live_event_origins WHERE seq=?", (row["seq"],)
                        )
                    )
                for reference in references:
                    body, _ = live._binding(db, reference)
                    if live._same_scope(body, scope) and body["grant"] == asdict(expected):
                        return True
        return False

    def read(
        self, snapshot: LiveSnapshot, root: VerifiedRoot, allowed_fields: tuple[str, ...]
    ) -> ObservedDecision | None:
        grant = self._grant(snapshot)
        _require(root in snapshot.verified_roots and root.input_fidelity == "observed")
        _require(root.namespace == grant["deciding_namespace"])
        _require(set(allowed_fields) <= set(grant["allowed_fields"]))
        _require(self._has_origin(root, grant))
        body = _read_private(self.authority.directory / grant["body"]["file"], 2_097_152)
        _require(digest(body) == grant["body"]["sha256"])
        _require(set(body) == {"schema_version", "records"} and body["schema_version"] == BODY)
        rows = body["records"]
        _require(isinstance(rows, list) and len(rows) <= 5000)
        chosen = None
        identities = set()
        for row in rows:
            _require(isinstance(row, dict) and set(row) == _ROW)
            for field in (
                "decision_id",
                "case_id",
                "family_id",
                "input_ref",
                "action_ref",
                "policy_ref",
                "receipt_ref",
            ):
                require_ref(row[field])
            _require(type(row["decision_revision"]) is int and row["decision_revision"] > 0)
            identity = (row["decision_id"], row["decision_revision"])
            _require(identity not in identities)
            identities.add(identity)
            if identity == (root.decision_id, root.decision_revision):
                chosen = row
        if chosen is None:
            return None
        _require(
            (
                chosen["case_id"],
                chosen["family_id"],
                chosen["input_ref"],
                chosen["action_ref"],
                chosen["policy_ref"],
                chosen["input_fidelity"],
            )
            == (
                root.case_id,
                root.family_id,
                root.input_ref,
                root.action_ref,
                root.policy_ref,
                "observed",
            )
        )
        features = chosen["features"]
        _require(isinstance(features, dict) and set(features) <= set(grant["allowed_fields"]))
        incumbent = chosen["incumbent"]
        if incumbent is not None:
            _require(isinstance(incumbent, dict))
            incumbent = simple_repair.decision(incumbent)
            if incumbent["status"] == "automatic":
                _require(incumbent["label"] == root.action_ref)
        # Grant changes while the body was read cannot publish an assessment.
        _require(self._grant(snapshot) == grant)
        return ObservedDecision(
            root.input_ref or "",
            root.action_ref,
            chosen["receipt_ref"],
            {key: features[key] for key in allowed_fields if key in features},
            incumbent,
        )
