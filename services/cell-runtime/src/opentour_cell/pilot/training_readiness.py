"""Durable, read-only-at-disclosure readiness receipts for current live evidence.

Only the worker supplies the evidence and assessment ports. Neither is a wire
input, a training grant, or a dispatch operation. Absent producers remain unknown.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.simple_repair import identifier
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore, _check_file

Json = dict[str, Any]
SCHEMA = "opentour.pilot.training-readiness.v1"


@dataclass(frozen=True)
class VerifiedRoot:
    reference: str
    family: str


@dataclass(frozen=True)
class ReadinessSnapshot:
    organization: str
    workflow_ref: str
    engagement: str
    evidence_revision: str
    decisions: int
    verified: int
    pending: int
    disputed: int
    roots: tuple[VerifiedRoot, ...]
    verifier_bound: bool
    next_actor: str | None
    can_bind_verifier: bool
    dataset_ref: str | None = None
    dataset_revision: str | None = None


class ReadinessSource(Protocol):
    def snapshot(self, actor: IntakePrincipal, workflow_ref: str) -> ReadinessSnapshot | None:
        """Resolve current authority and the actual durable reducer; never take row counts."""
        ...


@dataclass(frozen=True)
class ReadinessAssessment:
    organization: str
    workflow_ref: str
    engagement: str
    evidence_revision: str
    assessment_ref: str
    target_ref: str | None
    mandate_ref: str | None
    mandate_revision: str | None
    policy_revision: str
    minimum_residual_groups: int
    residual_roots: tuple[str, ...]
    missing_gates: tuple[str, ...]
    route: str


class AssessmentSource(Protocol):
    def assessment(self, snapshot: ReadinessSnapshot) -> ReadinessAssessment | None:
        """Read an actual worker assessment bound to this evidence and current policy."""
        ...


_GATE_ACTIONS = {
    "decision_time_input_required": ("provide_decision_time_input", "data_custodian"),
    "training_mandate_required": ("authorize_training_scope", "data_custodian"),
    "training_rights_required": ("authorize_training_rights", "data_custodian"),
    "training_budget_required": ("review_training_budget", "payer"),
    "untouched_holdout_required": ("supply_independent_holdout", "verifier"),
    "qualified_model_required": ("qualify_selected_model", "model_owner"),
    "development_evidence_required": ("supply_independent_evidence", "verifier"),
    "repair_assessment_required": ("assess_simple_repair", "worker_operator"),
}


def _require(condition: object) -> None:
    if not condition:
        raise TrainingError("readiness_evidence_unavailable")


def _verdict(snapshot: ReadinessSnapshot, assessment: ReadinessAssessment | None) -> Json:
    counts = (snapshot.decisions, snapshot.verified, snapshot.pending, snapshot.disputed)
    _require(all(type(n) is int and n >= 0 for n in counts))
    _require(snapshot.decisions == sum(counts[1:]))
    roots = {root.reference: root.family for root in snapshot.roots}
    _require(len(roots) == len(snapshot.roots) <= snapshot.verified)
    body: Json = {
        "schema_version": SCHEMA,
        "organization": snapshot.organization,
        "workflow_ref": snapshot.workflow_ref,
        "engagement": snapshot.engagement,
        "evidence_revision": snapshot.evidence_revision,
        "dataset_ref": snapshot.dataset_ref,
        "dataset_revision": snapshot.dataset_revision,
        "decision_count": snapshot.decisions,
        "verified_decision_count": snapshot.verified,
        "pending_count": snapshot.pending,
        "disputed_count": snapshot.disputed,
        "verified_root_count": len(roots),
        "independent_family_count": len(set(roots.values())),
        "root_manifest_digest": digest(sorted(roots.items())),
        "residual_group_count": None,
        "minimum_residual_groups": None,
        "target_ref": None,
        "mandate_ref": None,
        "mandate_revision": None,
        "assessment_ref": None,
        "policy_revision": None,
        "verifier_state": "bound" if snapshot.verifier_bound else "not_bound",
        "augmentation_state": "not_enabled",
        "verdict": "held",
        "missing_gates": [],
    }
    reason = "repair_assessment_required"
    if assessment is not None:
        _require(
            (
                assessment.organization,
                assessment.workflow_ref,
                assessment.engagement,
                assessment.evidence_revision,
            )
            == (
                snapshot.organization,
                snapshot.workflow_ref,
                snapshot.engagement,
                snapshot.evidence_revision,
            )
        )
        _require(
            type(assessment.minimum_residual_groups) is int
            and assessment.minimum_residual_groups > 0
        )
        _require(set(assessment.residual_roots) <= roots.keys())
        _require(len(set(assessment.residual_roots)) == len(assessment.residual_roots))
        _require(set(assessment.missing_gates) <= _GATE_ACTIONS.keys())
        _require(assessment.route in {"simple", "no_change", "hold", "model"})
        residuals = len({roots[key] for key in assessment.residual_roots})
        body.update(
            residual_group_count=residuals,
            minimum_residual_groups=assessment.minimum_residual_groups,
            residual_manifest_digest=digest(sorted(assessment.residual_roots)),
            target_ref=assessment.target_ref,
            mandate_ref=assessment.mandate_ref,
            mandate_revision=assessment.mandate_revision,
            assessment_ref=assessment.assessment_ref,
            policy_revision=assessment.policy_revision,
            repair_route=assessment.route,
            missing_gates=list(assessment.missing_gates),
        )
        unmeasured = next(
            (
                gate
                for gate in assessment.missing_gates
                if gate
                in {
                    "decision_time_input_required",
                    "development_evidence_required",
                    "repair_assessment_required",
                }
            ),
            None,
        )
        if unmeasured:
            body["residual_group_count"] = None
            reason = unmeasured
        elif residuals < assessment.minimum_residual_groups:
            reason = "too_early_to_train"
        elif assessment.route in {"simple", "no_change"}:
            reason = "simpler_route_sufficient"
        elif assessment.missing_gates:
            reason = assessment.missing_gates[0]
        elif assessment.route != "model":
            reason = "development_evidence_required"
        elif not all((assessment.target_ref, assessment.mandate_ref, assessment.mandate_revision)):
            reason = "training_mandate_required"
        else:
            reason = "fit_eligible"
            body["verdict"] = "fit_eligible"
    if snapshot.decisions == 0:
        reason = "no_live_decisions"
    elif snapshot.verified == 0:
        reason = (
            ("no_verifier_bound" if not snapshot.verifier_bound else "awaiting_outcome")
            if snapshot.pending
            else "disputed_outcomes"
        )
    body["reason_code"] = reason
    body["verdict"] = "fit_eligible" if reason == "fit_eligible" else "held"
    return body


def _project(body: Json, snapshot: ReadinessSnapshot) -> Json:
    reason = body["reason_code"]
    if reason == "no_verifier_bound":
        action, owner = (
            ("bind_verifier", "data_custodian")
            if snapshot.can_bind_verifier
            else ("ask_data_custodian_to_bind_verifier", "data_custodian")
        )
    elif reason in {"awaiting_outcome", "disputed_outcomes"}:
        action, owner = "verify_outcome", snapshot.next_actor or "verifier"
    elif reason == "no_live_decisions":
        action, owner = "observe_committed_decisions", "workflow_owner"
    elif reason == "fit_eligible":
        action, owner = "request_authorized_fit_admission", "worker_operator"
    elif reason == "simpler_route_sufficient":
        action, owner = "inspect_simple_repair", "workflow_owner"
    elif reason == "too_early_to_train":
        action, owner = "supply_independent_evidence", "verifier"
    else:
        action, owner = _GATE_ACTIONS[reason]
    return {**body, "next_action": action, "next_owner": owner}


class TrainingReadiness:
    def __init__(
        self,
        directory: Path,
        organization: str,
        engagement: str,
        *,
        source: ReadinessSource,
        assessments: AssessmentSource | None = None,
    ):
        self.directory = Path(directory).absolute()
        self.organization = identifier(organization)
        self.engagement = identifier(engagement)
        self.path = self.directory / "training-readiness.sqlite3"
        self.source, self.assessments = source, assessments

    @contextmanager
    def _current(
        self, workflow_ref: str, actor: IntakePrincipal
    ) -> Iterator[ReadinessSnapshot | None]:
        identifier(workflow_ref)
        _require(actor.tenant == self.organization)
        store = LocalTrainingStore(
            self.directory / "training.sqlite3", self.organization, existing_only=True
        )
        with store.engagement_guard(engagement_id=self.engagement, read_only=True):
            snapshot = self.source.snapshot(actor, workflow_ref)
            if snapshot is not None:
                _require(
                    (snapshot.organization, snapshot.workflow_ref, snapshot.engagement)
                    == (self.organization, workflow_ref, self.engagement)
                )
            yield snapshot

    def _body(self, snapshot: ReadinessSnapshot) -> Json:
        assessment = self.assessments.assessment(snapshot) if self.assessments else None
        return _verdict(snapshot, assessment)

    @contextmanager
    def _db(self, *, write: bool) -> Iterator[sqlite3.Connection]:
        if write:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
        _check_file(self.path)
        db = sqlite3.connect(
            self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            timeout=10,
            isolation_level=None,
        )
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS readiness (scope TEXT, revision TEXT, "
                    "version INTEGER, content TEXT, PRIMARY KEY(scope,revision), "
                    "UNIQUE(scope,version))"
                )
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _scope(self, workflow_ref: str) -> str:
        return digest([self.organization, self.engagement, workflow_ref])

    def refresh(self, workflow_ref: str, *, actor: IntakePrincipal) -> Json | None:
        with self._current(workflow_ref, actor) as snapshot:
            if snapshot is None:
                return None
            body = self._body(snapshot)
            revision = digest(body)
            scope = self._scope(workflow_ref)
            with self._db(write=True) as db:
                prior = db.execute(
                    "SELECT content FROM readiness WHERE scope=? AND revision=?", (scope, revision)
                ).fetchone()
                if prior is not None:
                    _require(
                        self.source.snapshot(actor, workflow_ref) == snapshot
                        and self._body(snapshot) == body
                    )
                    return _project(json.loads(prior[0]), snapshot)
                version = db.execute(
                    "SELECT COALESCE(MAX(version),0)+1 FROM readiness WHERE scope=?", (scope,)
                ).fetchone()[0]
                receipt = {
                    **body,
                    "revision": revision,
                    "readiness_ref": "ready-" + revision[:40],
                    "version": version,
                    "recorded_at": int(time.time()),
                }
                # The source owns its authority and revision checks. Re-read before commit.
                _require(
                    self.source.snapshot(actor, workflow_ref) == snapshot
                    and self._body(snapshot) == body
                )
                db.execute(
                    "INSERT INTO readiness VALUES(?,?,?,?)",
                    (scope, revision, version, canonical(receipt)),
                )
                return _project(receipt, snapshot)

    def read(
        self, workflow_ref: str, *, actor: IntakePrincipal, expected_revision: str | None = None
    ) -> Json | None:
        with self._current(workflow_ref, actor) as snapshot:
            if snapshot is None or not self.path.exists():
                return None
            body = self._body(snapshot)
            revision = digest(body)
            if expected_revision is not None and expected_revision != revision:
                return None
            with self._db(write=False) as db:
                row = db.execute(
                    "SELECT content FROM readiness WHERE scope=? AND revision=?",
                    (self._scope(workflow_ref), revision),
                ).fetchone()
                if (
                    row is None
                    or self.source.snapshot(actor, workflow_ref) != snapshot
                    or self._body(snapshot) != body
                ):
                    return None
                receipt = json.loads(row[0])
                _require(
                    all(receipt.get(k) == v for k, v in body.items())
                    and receipt.get("revision") == revision
                )
                return _project(receipt, snapshot)

    def history(self, workflow_ref: str, *, actor: IntakePrincipal) -> list[Mapping[str, Any]]:
        with self._current(workflow_ref, actor) as snapshot:
            if snapshot is None or not self.path.exists():
                return []
            with self._db(write=False) as db:
                rows = db.execute(
                    "SELECT content FROM readiness WHERE scope=? ORDER BY version",
                    (self._scope(workflow_ref),),
                ).fetchall()
                if self.source.snapshot(actor, workflow_ref) != snapshot:
                    return []
                return [json.loads(row[0]) for row in rows]
