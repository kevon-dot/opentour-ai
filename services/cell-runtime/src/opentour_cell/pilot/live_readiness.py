"""Readiness from the durable live reducer and an owner-pinned simple repair.

The input port resolves observed decision-time references within the current
grant. No event, prompt, generated child, or readiness receipt supplies a grant.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import simple_repair
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError
from opentour_cell.pilot.live_evidence import LiveEvidence, LiveSnapshot, VerifiedRoot
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import _check_file
from opentour_cell.pilot.training_readiness import (
    AutomaticCounts,
    ReadinessAssessment,
    ReadinessSnapshot,
)
from opentour_cell.pilot.training_readiness import (
    VerifiedRoot as ReadinessRoot,
)

Json = dict[str, Any]
POLICY = "opentour.pilot.live-repair-policy.v1"
_POLICY_KEYS = {
    "schema_version",
    "organization",
    "workflow_ref",
    "engagement",
    "owner_ref",
    "dataset_ref",
    "dataset_revision",
    "feature",
    "allowed_features",
    "labels",
    "rule_mapping",
    "fallback",
    "terms",
    "development_families",
    "protected_families",
    "target_ref",
    "mandate_ref",
    "mandate_revision",
    "agreement_revision",
}


def root_ref(root: VerifiedRoot) -> str:
    return "root-" + digest([root.namespace, root.decision_id, root.decision_revision])[:40]


def normalized(value: LiveSnapshot) -> ReadinessSnapshot:
    return ReadinessSnapshot(
        value.tenant,
        value.workflow_ref,
        value.engagement_epoch,
        value.evidence_revision,
        value.decisions,
        value.verified,
        value.pending,
        value.disputed,
        tuple(ReadinessRoot(root_ref(root), root.family_id) for root in value.verified_roots),
        value.verifier_bound,
        value.next_actor,
        value.can_bind_verifier,
        value.dataset_ref,
        value.dataset_revision,
    )


class LiveReadinessSource:
    def __init__(self, live: LiveEvidence):
        self.live = live

    def snapshot(self, actor: IntakePrincipal, workflow_ref: str) -> ReadinessSnapshot | None:
        try:
            return normalized(self.live.snapshot(actor, workflow_ref))
        except (IntakeError, OSError, sqlite3.Error):
            return None


@dataclass(frozen=True)
class ObservedDecision:
    input_ref: str
    action_ref: str
    receipt_ref: str
    features: Mapping[str, Any]
    incumbent: Mapping[str, Any] | None


class DecisionContext(Protocol):
    def read(
        self, snapshot: LiveSnapshot, root: VerifiedRoot, allowed_fields: tuple[str, ...]
    ) -> ObservedDecision | None:
        """Resolve observed input AND the committed action's evidenced classification.

        An opaque action_ref cannot establish automatic vs defer/ask. If the
        actual application boundary did not capture that classification, return
        incumbent=None; never infer it from a label or owner policy default.
        """
        ...


def _require(condition: object) -> None:
    if not condition:
        raise TrainingError("repair_assessment_unavailable")


class LocalRepairAssessments:
    """Explicit worker refresh writes; disclosure reads stored assessments only.

    Capability and untouched-live-holdout qualification have no implemented producer
    on this route yet. They remain explicit holds, even if recurrence reaches policy.
    """

    def __init__(
        self,
        directory: Path,
        live: LiveEvidence,
        worker: IntakePrincipal,
        policy_path: Path,
        *,
        contexts: DecisionContext | None = None,
        context_revision: Callable[[LiveSnapshot], str] | None = None,
        seal_aggregate: Callable[[LiveSnapshot, Json], str] | None = None,
    ):
        self.directory = directory.absolute()
        self.path = self.directory / "live-repair-assessments.sqlite3"
        self.live, self.worker, self.policy_path, self.contexts = (
            live,
            worker,
            policy_path,
            contexts,
        )
        self.context_revision = context_revision
        self.seal_aggregate = seal_aggregate

    @staticmethod
    def _aggregate_key(assessment_key: str) -> str:
        return digest(["opentour.live-incumbent-aggregate.v1", assessment_key])

    def _key(
        self, snapshot: ReadinessSnapshot, policy: Json, authority: Json, context: str | None
    ) -> str:
        parts: list[Any] = [
            snapshot.organization,
            snapshot.engagement,
            snapshot.workflow_ref,
            snapshot.evidence_revision,
            digest(policy),
            authority,
        ]
        if self.context_revision is not None:
            parts.append(context)
        return digest(parts)

    def _policy(self, snapshot: ReadinessSnapshot) -> Json:
        value = _read_private(self.policy_path, 262144)
        _require(isinstance(value, dict) and value.keys() == _POLICY_KEYS)
        _require(value["schema_version"] == POLICY)
        _require(
            (
                value["organization"],
                value["workflow_ref"],
                value["engagement"],
                value["dataset_ref"],
                value["dataset_revision"],
            )
            == (
                snapshot.organization,
                snapshot.workflow_ref,
                snapshot.engagement,
                snapshot.dataset_ref,
                snapshot.dataset_revision,
            )
        )
        simple_repair.identifier(value["owner_ref"])
        simple_repair.validate_features(value["feature"], value["allowed_features"])
        for key in ("development_families", "protected_families", "labels"):
            _require(isinstance(value[key], list) and len(value[key]) <= 5000)
            _require(len(set(value[key])) == len(value[key]))
            for ref in value[key]:
                simple_repair.identifier(ref)
        _require(not set(value["development_families"]) & set(value["protected_families"]))
        _require(
            isinstance(value["terms"], dict)
            and set(value["terms"])
            == {
                "minimum_residual_errors",
                "minimum_development_cases",
                "minimum_coverage_bps",
                "error_ceiling_bps",
            }
        )
        simple_repair.row_bound(value["terms"]["minimum_residual_errors"], minimum=2)
        simple_repair.row_bound(value["terms"]["minimum_development_cases"])
        simple_repair.fraction_bps(value["terms"], "minimum_coverage_bps")
        simple_repair.fraction_bps(value["terms"], "error_ceiling_bps")
        return dict(value)

    def _authority(self, snapshot: ReadinessSnapshot, policy: Json) -> Json:
        # Future capability/holdout producers must supply independently verified
        # receipts. A target name or a declared protected family is insufficient.
        missing = ["untouched_holdout_required", "qualified_model_required"]
        mandate = policy["mandate_ref"]
        result: Json = {
            "missing": missing,
            "mandate_ref": None,
            "mandate_revision": None,
            "reserved_fits": None,
            "reserved_seconds": None,
        }
        if mandate is None:
            missing.insert(0, "training_mandate_required")
            return result
        try:
            current = LearningMandates(
                self.directory, snapshot.organization, snapshot.engagement
            ).inspect(mandate, actor=self.worker.actor)
        except (MandateError, OSError, sqlite3.Error):
            missing.insert(0, "training_mandate_required")
            return result
        scope = current["scope"]
        if (
            current["withdrawn"]
            or current["expired"]
            or current["mandate_revision"] != policy["mandate_revision"]
            or scope["workflow"] != snapshot.workflow_ref
            or scope["owner_id"] != policy["owner_ref"]
            or scope["agreement_revision"] != policy["agreement_revision"]
        ):
            missing.insert(0, "training_mandate_required")
            return result
        result.update(
            mandate_ref=mandate,
            mandate_revision=current["mandate_revision"],
            reserved_fits=current["reserved_fits"],
            reserved_seconds=current["reserved_seconds"],
        )
        if "train" not in scope["uses"]:
            missing.insert(0, "training_rights_required")
        elif (
            current["reserved_fits"] >= scope["max_fits"]
            or current["reserved_seconds"] + scope["max_seconds_per_fit"]
            > scope["max_total_seconds"]
        ):
            missing.insert(0, "training_budget_required")
        return result

    def _selection(self, snapshot: ReadinessSnapshot) -> LiveSnapshot:
        actual = self.live.snapshot(self.worker, snapshot.workflow_ref)
        candidate = normalized(actual)
        # Permission to bind belongs to the viewing actor; evidence identity does not.
        _require(
            asdict(candidate) | {"can_bind_verifier": snapshot.can_bind_verifier}
            == asdict(snapshot)
        )
        return actual

    def refresh(self, snapshot: ReadinessSnapshot) -> ReadinessAssessment:
        with self.live.authority.guard(self.worker, snapshot.workflow_ref):
            actual = self._selection(snapshot)
            context = self.context_revision(actual) if self.context_revision else None
            policy = self._policy(snapshot)
            authority = self._authority(snapshot, policy)
            rule = simple_repair.declared_rule(
                feature=policy["feature"],
                allowed_features=policy["allowed_features"],
                mapping=policy["rule_mapping"],
                fallback=policy["fallback"],
                allowed_labels=policy["labels"],
            )
            rows: list[Json] = []
            missing_input = False
            missing_classification = False
            deferred_roots = []
            context_receipts = []
            for root in actual.verified_roots:
                if root.family_id not in policy["development_families"]:
                    continue
                observed = None
                if root.input_fidelity == "observed" and root.input_ref and self.contexts:
                    observed = self.contexts.read(actual, root, tuple(policy["allowed_features"]))
                if observed is None:
                    missing_input = True
                    continue
                _require(
                    observed.input_ref == root.input_ref and observed.action_ref == root.action_ref
                )
                simple_repair.identifier(observed.receipt_ref)
                context_receipts.append(observed.receipt_ref)
                features = observed.features
                _require(set(features) <= set(policy["allowed_features"]))
                if observed.incumbent is None:
                    missing_classification = True
                    continue
                incumbent = simple_repair.decision(dict(observed.incumbent))
                if incumbent["status"] != "automatic":
                    deferred_roots.append(root_ref(root))
                    continue
                _require(
                    root.action_ref in policy["labels"]
                    and root.verified_action_ref in policy["labels"]
                    and incumbent["label"] == root.action_ref
                )
                rows.append(
                    {
                        "case_id": root_ref(root),
                        "group_id": root.family_id,
                        "features": dict(features),
                        "reference": root.verified_action_ref,
                        "incumbent": incumbent,
                    }
                )
            decision = simple_repair.assess_development(
                rows, rule, terms=policy["terms"], model_authorized=False, compute_authorized=False
            )
            residuals = []
            for row in rows:
                output = simple_repair.apply_frozen_rule(rule, row["features"], row["incumbent"])
                label = row["reference"]
                if [row["incumbent"]["label"], label] == decision["residual_class"] and (
                    (output["status"] == "automatic" and output["label"] != label)
                    or (output["status"] != "automatic" and row["incumbent"]["label"] != label)
                ):
                    residuals.append(row["case_id"])
            _require(digest(sorted(residuals)) == decision["residual_membership_digest"])
            missing = list(authority["missing"])
            if missing_input:
                missing.insert(0, "decision_time_input_required")
            if missing_classification:
                missing.insert(0, "committed_action_classification_required")
            if not rows:
                if missing_input or missing_classification:
                    missing.append("development_evidence_required")
                else:
                    missing.insert(0, "development_evidence_required")
            receipt = ReadinessAssessment(
                snapshot.organization,
                snapshot.workflow_ref,
                snapshot.engagement,
                snapshot.evidence_revision,
                "assessment-"
                + (digest([decision["revision"], context]) if context else decision["revision"])[
                    :40
                ],
                policy["target_ref"],
                authority["mandate_ref"],
                authority["mandate_revision"],
                digest(policy),
                policy["terms"]["minimum_residual_errors"],
                tuple(sorted(residuals)),
                tuple(dict.fromkeys(missing)),
                decision["route"],
            )
            body: Json = {
                "assessment": asdict(receipt),
                "authority": authority,
                "decision": decision,
                "roots": sorted(root_ref(r) for r in actual.verified_roots),
                "families": sorted({r.family_id for r in actual.verified_roots}),
                "policy_digest": digest(policy),
                "context_receipts": sorted(context_receipts),
                "nonautomatic_roots": sorted(deferred_roots),
            }
            # Preserve legacy bodies. An aggregate requires a complete, signed
            # classification of each verified decision, including deferred ones.
            del body["assessment"]["incumbent_automatic"]
            key = self._key(snapshot, policy, authority, context)
            if (
                self.seal_aggregate is not None
                and context is not None
                and len(rows) + len(deferred_roots) == snapshot.verified
            ):
                counts = AutomaticCounts(
                    sum(row["incumbent"]["label"] != row["reference"] for row in rows), len(rows)
                )
                summary = {
                    "assessment_key": key,
                    "assessment": body["assessment"],
                    "counts": asdict(counts),
                }
                body["incumbent_automatic"] = {
                    "counts": summary["counts"],
                    "seal": self.seal_aggregate(actual, summary),
                }
                key = self._aggregate_key(key)
                receipt = replace(receipt, incumbent_automatic=counts)
            if self.context_revision:
                body["context_permission_revision"] = context
            self._selection(snapshot)
            _require(
                self._policy(snapshot) == policy and self._authority(snapshot, policy) == authority
            )
            if self.context_revision:
                _require(self.context_revision(actual) == context)
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                os.close(fd)
            _check_file(self.path)
            with sqlite3.connect(self.path) as db:
                db.execute("BEGIN IMMEDIATE")
                if self.context_revision:
                    _require(self.context_revision(actual) == context)
                db.execute(
                    "CREATE TABLE IF NOT EXISTS assessments (key TEXT PRIMARY KEY, body TEXT)"
                )
                old = db.execute("SELECT body FROM assessments WHERE key=?", (key,)).fetchone()
                _require(old is None or old[0] == canonical(body))
                db.execute("INSERT OR IGNORE INTO assessments VALUES(?,?)", (key, canonical(body)))
                self._selection(snapshot)
                _require(
                    self._policy(snapshot) == policy
                    and self._authority(snapshot, policy) == authority
                )
                if self.context_revision:
                    _require(self.context_revision(actual) == context)
            return receipt

    def assessment(self, snapshot: ReadinessSnapshot) -> ReadinessAssessment | None:
        try:
            actual = self._selection(snapshot)
            context = self.context_revision(actual) if self.context_revision else None
            policy = self._policy(snapshot)
            authority = self._authority(snapshot, policy)
            _check_file(self.path)
            key = self._key(snapshot, policy, authority, context)
            from opentour_cell.pilot.receipt_recovery import _open_db

            with _open_db(self.path) as db:
                if db is None:
                    return None
                aggregate = None
                if self.seal_aggregate is not None and context is not None:
                    aggregate = db.execute(
                        "SELECT body FROM assessments WHERE key=?", (self._aggregate_key(key),)
                    ).fetchone()
                row = (
                    aggregate
                    or db.execute("SELECT body FROM assessments WHERE key=?", (key,)).fetchone()
                )
            if row is None:
                return None
            body = json.loads(row[0])
            if self.context_revision:
                _require(body.get("context_permission_revision") == context)
            value = body["assessment"]
            receipt_fields = dict(value)
            value["residual_roots"] = tuple(value["residual_roots"])
            value["missing_gates"] = tuple(value["missing_gates"])
            receipt = ReadinessAssessment(**value)
            _require(receipt.incumbent_automatic is None)
            if aggregate is not None:
                assert self.seal_aggregate is not None
                packet = body["incumbent_automatic"]
                counts = AutomaticCounts(**packet["counts"])
                summary = {
                    "assessment_key": key,
                    "assessment": receipt_fields,
                    "counts": asdict(counts),
                }
                _require(hmac.compare_digest(packet["seal"], self.seal_aggregate(actual, summary)))
                return replace(receipt, incumbent_automatic=counts)
            return receipt
        except (
            IntakeError,
            TrainingError,
            OSError,
            sqlite3.Error,
            ValueError,
            TypeError,
            KeyError,
        ):
            return None
