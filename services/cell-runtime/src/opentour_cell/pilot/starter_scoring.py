"""Evaluate a frozen starter without giving the model its owner-reviewed answers."""

from __future__ import annotations

import json
import os
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot.intake_admission import IntakeWork, require_readonly_snapshot
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.model_registry import ModelRegistry
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.simple_repair import decision
from opentour_cell.pilot.starter_cases import FrozenEvaluationInputs, StarterCases
from opentour_cell.pilot.training import _check_file

Json = dict[str, Any]


def _require(value: object, reason: str = "starter_evaluation_unavailable") -> None:
    if not value:
        raise IntakeError(reason)


@dataclass(frozen=True)
class EvaluationPermission:
    """Resolved by the owner's authority, never deserialized from an action payload."""

    tenant: str
    workflow_ref: str
    epoch: str
    run_ref: str
    evaluator: str
    execution_ref: str
    revision: str
    freeze_ref: str
    freeze_revision: str
    plan_digest: str
    comparator_revision: str
    model_ref: str
    model_revision: str
    baseline_grant_revision: str
    input_fields: tuple[str, ...]
    maximum_cases: int
    expires_at: int


class EvaluationAuthority(Protocol):
    def revalidate(
        self, actor: IntakePrincipal, work: IntakeWork, permission: EvaluationPermission
    ) -> None:
        """Read current capability/epoch under an already-held guard, without a new writer.

        Deny a revoked, expired, rebound or superseded capability immediately.
        This is not satisfied by returning the original captured permission.
        """
        ...

    def guard(
        self, actor: IntakePrincipal, work: IntakeWork, execution_ref: str
    ) -> AbstractContextManager[EvaluationPermission]:
        """Hold the current explicit evaluation/rerun capability through each boundary.

        Same logical execution resolves to the same reference even if transport
        keys differ. A new execution requires explicit current rerun authority.
        Owner review, materialization and model discovery do not grant this role.
        """
        ...


@dataclass(frozen=True)
class PredictionRequest:
    operation_ref: str
    execution_ref: str
    arm: str
    freeze_revision: str
    input_digest: str
    implementation_ref: str
    implementation_revision: str


@dataclass(frozen=True)
class PredictionReceipt:
    request: PredictionRequest
    receipt_ref: str
    outputs: Mapping[str, Mapping[str, object]]


class PredictionPort(Protocol):
    reference: str
    revision: str

    def lookup(self, request: PredictionRequest) -> PredictionReceipt | None:
        """Independently reconcile this exact operation; None means still unknown."""
        ...

    def execute(
        self, request: PredictionRequest, inputs: Mapping[str, Mapping[str, object]]
    ) -> PredictionReceipt:
        """Use the stable operation key and return only a durably queryable receipt.

        Inputs contain no answer key. The caller holds the current evaluation
        authority and checks the current baseline grant at this boundary.
        """
        ...


class BaselinePort(PredictionPort, Protocol):
    def observe(self, binding: model.ModelBinding, purpose: model.Purpose) -> model.Observation: ...


class StarterScoring:
    """Private evaluator journal. There is no fit, publication or delivery operation."""

    def __init__(
        self,
        directory: Path,
        cases: StarterCases,
        authority: EvaluationAuthority,
        registry: ModelRegistry,
        baseline: BaselinePort | None,
        comparator: PredictionPort,
    ):
        self.path = directory.absolute() / "starter-evaluations.sqlite3"
        self.cases, self.authority, self.registry = cases, authority, registry
        self.baseline, self.comparator = baseline, comparator

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            parent = self.path.parent
            _require(parent.resolve(strict=True) == parent and parent.is_dir())
            _require(parent.stat().st_uid == os.getuid() and not parent.stat().st_mode & 0o077)
            try:
                os.close(
                    os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                )
            except FileExistsError:
                pass
        _check_file(self.path)
        if not write:
            require_readonly_snapshot(self.path)
        db = sqlite3.connect(
            self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            isolation_level=None,
            timeout=10,
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS evaluations("
                    "ref TEXT PRIMARY KEY, definition TEXT, result TEXT)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS predictions("
                    "ref TEXT PRIMARY KEY, request TEXT, receipt TEXT)"
                )
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _permission(
        self,
        actor: IntakePrincipal,
        work: IntakeWork,
        permission: EvaluationPermission,
        frozen: FrozenEvaluationInputs,
    ) -> None:
        _require(
            (permission.tenant, permission.workflow_ref, permission.epoch, permission.run_ref)
            == (actor.tenant, work.workflow_ref, work.resource.engagement_epoch, work.run_ref)
            and permission.evaluator == actor.actor == frozen.plan.evaluator_ref
            and permission.freeze_ref == frozen.freeze_ref
            and permission.freeze_revision == frozen.freeze_revision
            and permission.plan_digest == digest(asdict(frozen.plan))
            and permission.expires_at > time.time()
            and type(permission.maximum_cases) is int
            and 0 <= len(frozen.cases) <= permission.maximum_cases <= 1000
            and frozen.plan.scorer_ref == "exact-action-v1"
            and frozen.plan.abstention_ref == "review-v1"
            and frozen.plan.comparator_ref == self.comparator.reference
            and permission.comparator_revision == self.comparator.revision
            and frozen.plan.baseline_ref in {"baseline-pending", permission.model_ref}
            and frozen.plan.evaluated_model_ref in {None, permission.model_ref}
        )
        for ref in (permission.execution_ref, permission.revision, self.comparator.revision):
            require_ref(ref)
        fields = {key for case in frozen.cases for key, _ in case.decision_inputs}
        _require(fields <= set(permission.input_fields), "starter_input_scope_unavailable")

    def _model(self, permission: EvaluationPermission) -> model.ModelBinding:
        _require(self.baseline is not None, "baseline_execution_unavailable")
        assert self.baseline is not None
        binding = self.registry.binding(permission.model_ref)
        _require(
            binding.tenant == permission.tenant
            and binding.immutable_revision == permission.model_revision
            and (self.baseline.reference, self.baseline.revision)
            == (binding.model_ref, binding.immutable_revision),
            "baseline_identity_unavailable",
        )
        grant = self.registry.current_grant(permission.evaluator, binding, model.Purpose.BASELINE)
        _require(
            grant is not None
            and grant.revision == permission.baseline_grant_revision
            and grant.valid_until > time.time()
            and set(permission.input_fields) <= grant.permitted_fields,
            "baseline_inference_permission_required",
        )
        # Only the metadata operation of the common preflight is used. This port
        # has no training methods and cannot fabricate a fitted Candidate.
        preflight = model.Preflight(
            cast(model.Connector, self.baseline), self.registry.current_grant
        )
        capability = preflight.discover(permission.evaluator, binding, model.Purpose.BASELINE)
        _require(
            capability.state is model.State.BENCHMARK_ONLY and capability.exact_model_attribution,
            "baseline_identity_unavailable",
        )
        # Remote execution needs its own bounded processor/disclosure adapter.
        _require(binding.route is model.Route.LOCAL, "bounded_baseline_adapter_required")
        _require(
            self.registry.current_grant(permission.evaluator, binding, model.Purpose.BASELINE)
            == grant,
            "baseline_inference_permission_required",
        )
        return binding

    def _current(
        self,
        actor: IntakePrincipal,
        work: IntakeWork,
        permission: EvaluationPermission,
        frozen: FrozenEvaluationInputs,
        *,
        inference: bool = True,
    ) -> None:
        self.authority.revalidate(actor, work, permission)
        _require(self.cases.evaluation_inputs(actor, work) == frozen, "starter_freeze_changed")
        self._permission(actor, work, permission, frozen)
        if inference:
            self._model(permission)

    @staticmethod
    def _held(permission: EvaluationPermission, reason: str) -> Json:
        return {
            "state": "frozen_unscored",
            "execution_ref": permission.execution_ref,
            "freeze_ref": permission.freeze_ref,
            "freeze_revision": permission.freeze_revision,
            "reason_code": reason,
            "next_action": "reconnect_baseline_or_resolve_execution",
            "result_ref": None,
            "result_digest": None,
        }

    def evaluate(self, actor: IntakePrincipal, work: IntakeWork, execution_ref: str) -> Json:
        require_ref(execution_ref)
        with self.authority.guard(actor, work, execution_ref) as permission:
            frozen = self.cases.evaluation_inputs(actor, work)
            self._permission(actor, work, permission, frozen)
            _require(permission.execution_ref == execution_ref)
            handles = {
                case.case_id: "starter-case-" + digest([frozen.freeze_revision, case.case_id])[:40]
                for case in frozen.cases
            }
            inputs = {handles[case.case_id]: dict(case.decision_inputs) for case in frozen.cases}
            definition = {
                "permission": asdict(permission),
                "input_digest": digest(inputs),
                "comparator_revision": self.comparator.revision,
            }
            with self._db(write=True) as db:
                self._current(actor, work, permission, frozen, inference=False)
                old = db.execute(
                    "SELECT definition FROM evaluations WHERE ref=?", (execution_ref,)
                ).fetchone()
                _require(
                    old is None or old[0] == canonical(definition), "evaluation_intent_changed"
                )
                db.execute(
                    "INSERT OR IGNORE INTO evaluations VALUES(?,?,NULL)",
                    (execution_ref, canonical(definition)),
                )
            try:
                self._model(permission)
            except (IntakeError, model.CapabilityError):
                self._current(actor, work, permission, frozen, inference=False)
                return self._held(permission, "baseline_inference_unavailable")
            if not inputs:
                return self._held(permission, "reserved_denominator_unavailable")
        assert self.baseline is not None
        outputs: dict[str, Json] = {}
        for arm, port in (("model", self.baseline), ("comparator", self.comparator)):
            request = PredictionRequest(
                "starter-prediction-" + digest([definition, arm])[:40],
                execution_ref,
                arm,
                frozen.freeze_revision,
                digest(inputs),
                port.reference,
                port.revision,
            )
            with self.authority.guard(actor, work, execution_ref) as now:
                _require(now == permission, "evaluation_permission_changed")
                self._permission(actor, work, now, self.cases.evaluation_inputs(actor, work))
                self._model(now)
                with self._db(write=True) as db:
                    self._current(actor, work, now, frozen)
                    old = db.execute(
                        "SELECT request,receipt FROM predictions WHERE ref=?",
                        (request.operation_ref,),
                    ).fetchone()
                    _require(
                        old is None or old["request"] == canonical(asdict(request)),
                        "prediction_intent_changed",
                    )
                    fresh = old is None
                    db.execute(
                        "INSERT OR IGNORE INTO predictions VALUES(?,?,NULL)",
                        (request.operation_ref, canonical(asdict(request))),
                    )
                receipt = port.lookup(request)
                if receipt is None and fresh:
                    # Lookup is an independent adapter boundary. It may reveal
                    # withdrawn or changed capability before execution starts.
                    self._current(actor, work, now, frozen)
                    try:
                        receipt = port.execute(request, json.loads(canonical(inputs)))
                    except OSError:
                        receipt = port.lookup(request)
                if receipt is None:
                    self._current(actor, work, now, frozen, inference=False)
                    return self._held(permission, "prediction_outcome_unknown")
                durable = port.lookup(request)
                if durable is None:
                    self._current(actor, work, now, frozen, inference=False)
                    return self._held(permission, "prediction_outcome_unknown")
                _require(durable == receipt, "prediction_receipt_changed")
                _require(receipt.request == request, "prediction_receipt_mismatch")
                require_ref(receipt.receipt_ref)
                body: Json = {
                    "request": asdict(request),
                    "receipt_ref": receipt.receipt_ref,
                    "outputs": {
                        key: decision(dict(value)) for key, value in receipt.outputs.items()
                    },
                }
                _require(set(body["outputs"]) <= set(inputs), "prediction_population_mismatch")
                _require(
                    all(
                        not (value["status"] == "automatic" and value["label"] == "REVIEW")
                        for value in body["outputs"].values()
                    ),
                    "prediction_abstention_contract_mismatch",
                )
                self._model(now)
                _require(
                    self.cases.evaluation_inputs(actor, work) == frozen,
                    "starter_freeze_changed",
                )
                if old is not None and old["receipt"] is not None:
                    _require(old["receipt"] == canonical(body), "prediction_receipt_changed")
                else:
                    with self._db(write=True) as db:
                        self._current(actor, work, now, frozen)
                        db.execute(
                            "UPDATE predictions SET receipt=? WHERE ref=? AND receipt IS NULL",
                            (canonical(body), request.operation_ref),
                        )
                        self._current(actor, work, now, frozen)
                _require(body["request"] == asdict(request), "prediction_receipt_mismatch")
                outputs[arm] = body
        with self.authority.guard(actor, work, execution_ref) as now:
            _require(now == permission and self.cases.evaluation_inputs(actor, work) == frozen)
            self._model(now)
            targets = self.cases.evaluation_targets(
                actor, work, freeze_revision=frozen.freeze_revision
            )
            _require(set(targets) == set(handles), "starter_target_population_changed")
            rows = []
            for case in frozen.cases:
                rows.append(
                    {
                        "case_id": case.case_id,
                        "family_id": case.family_id,
                        "origin": case.origin,
                        "expected": targets[case.case_id],
                        **{
                            arm: body["outputs"].get(handles[case.case_id], {"status": "missing"})
                            for arm, body in outputs.items()
                        },
                    }
                )
            result: Json = {
                "state": "scored",
                "execution_ref": execution_ref,
                "freeze_ref": frozen.freeze_ref,
                "freeze_revision": frozen.freeze_revision,
                "evidence_origin": "expert_reviewed_generated_starter",
                "rows": rows,
                "predictions": {
                    arm: {key: value for key, value in body.items() if key != "outputs"}
                    for arm, body in outputs.items()
                },
                "metrics": _metrics(rows),
                "unresolved": frozen.unresolved,
                "excluded": frozen.excluded,
            }
            result["result_digest"] = digest(result)
            result["result_ref"] = "starter-result-" + result["result_digest"][:40]
            with self._db(write=True) as db:
                self._current(actor, work, now, frozen)
                old = db.execute(
                    "SELECT result FROM evaluations WHERE ref=?", (execution_ref,)
                ).fetchone()
                _require(
                    old is not None and (old[0] is None or old[0] == canonical(result)),
                    "evaluation_result_changed",
                )
                db.execute(
                    "UPDATE evaluations SET result=? WHERE ref=?",
                    (canonical(result), execution_ref),
                )
                self._current(actor, work, now, frozen)
            return result


def _metrics(rows: list[Json]) -> Json:
    def correct(row: Json, arm: str) -> bool:
        value = row[arm]
        return bool(
            value["status"] != "missing"
            and ("REVIEW" if value["status"] == "review" else value["label"])
            == row["expected"]["target"]
        )

    def rate(numerator: int, denominator: int) -> Json:
        return {
            "numerator": numerator,
            "denominator": denominator,
            "value": numerator / denominator if denominator else None,
        }

    metrics = {}
    for arm in ("model", "comparator"):
        automatic = [row for row in rows if row[arm]["status"] == "automatic"]
        metrics[arm] = {
            "exact_action": rate(sum(correct(row, arm) for row in rows), len(rows)),
            "coverage": rate(len(automatic), len(rows)),
            "wrong_automatic": rate(
                sum(not correct(row, arm) for row in automatic), len(automatic)
            ),
            "missing": sum(row[arm]["status"] == "missing" for row in rows),
        }
    paired = [
        row
        for row in rows
        if all(row[arm]["status"] != "missing" for arm in ("model", "comparator"))
    ]
    metrics["paired"] = {
        "fixed": sum(not correct(row, "model") and correct(row, "comparator") for row in paired),
        "regressed": sum(
            correct(row, "model") and not correct(row, "comparator") for row in paired
        ),
        "denominator": len(paired),
        "excluded_missing": len(rows) - len(paired),
    }
    return metrics
