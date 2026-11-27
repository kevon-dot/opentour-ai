"""Read a retained starter evaluation under separate, current inspection authority."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import AbstractContextManager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _has_table, _open_db
from opentour_cell.pilot.simple_repair import decision
from opentour_cell.pilot.starter_scoring import EvaluationPermission, PredictionRequest, _metrics

Json = dict[str, Any]
_ORIGIN = "expert_reviewed_generated_starter"


def _require(condition: object) -> None:
    if not condition:
        raise IntakeError("starter_score_inspection_unavailable")


@dataclass(frozen=True)
class InspectionScope:
    viewer: IntakePrincipal
    inspection_revision: str
    inspection_expires_at: int
    permission: EvaluationPermission
    input_digest: str
    comparator_ref: str
    population_digest: str
    reserved_count: int
    unresolved: int
    excluded: int


class InspectionAuthority(Protocol):
    def guard(
        self, actor: IntakePrincipal, workflow_ref: str, execution_ref: str
    ) -> AbstractContextManager[InspectionScope]:
        """Resolve current inspection rights and independent frozen population metadata."""
        ...

    def revalidate(self, actor: IntakePrincipal, scope: InspectionScope) -> None:
        """Fence current role, source, epoch, retention and scope through disclosure."""
        ...


def _check_scope(scope: InspectionScope, actor: IntakePrincipal, workflow: str, run: str) -> None:
    p = scope.permission
    _require(
        scope.viewer == actor
        and (actor.tenant, workflow, run) == (p.tenant, p.workflow_ref, p.execution_ref)
    )
    require_ref(scope.inspection_revision)
    require_ref(scope.comparator_ref)
    _require(type(scope.inspection_expires_at) is int and time.time() < scope.inspection_expires_at)
    _require(
        all(
            type(n) is int and n >= 0
            for n in (scope.reserved_count, scope.unresolved, scope.excluded, p.maximum_cases)
        )
    )
    _require(scope.reserved_count <= p.maximum_cases <= 1000)
    for value in (scope.input_digest, scope.population_digest):
        _require(re.fullmatch("[a-f0-9]{64}", value))


def _read(db: sqlite3.Connection | None, scope: InspectionScope) -> Json:
    p = scope.permission
    summary: Json = dict(
        state="frozen_unscored",
        evidence_origin=_ORIGIN,
        freeze_ref=p.freeze_ref,
        freeze_revision=p.freeze_revision,
        reserved=scope.reserved_count,
        unresolved=scope.unresolved,
        excluded=scope.excluded,
    )
    if db is None:
        return summary
    _require(_has_table(db, "evaluations") and _has_table(db, "predictions"))
    record = db.execute(
        "SELECT definition,result FROM evaluations WHERE ref=?", (p.execution_ref,)
    ).fetchone()
    definition = dict(
        permission=asdict(p),
        input_digest=scope.input_digest,
        comparator_revision=p.comparator_revision,
    )
    receipts = {}
    for arm, implementation, revision in (
        ("model", p.model_ref, p.model_revision),
        ("comparator", scope.comparator_ref, p.comparator_revision),
    ):
        key = "starter-prediction-" + digest([definition, arm])[:40]
        request = asdict(
            PredictionRequest(
                key,
                p.execution_ref,
                arm,
                p.freeze_revision,
                scope.input_digest,
                implementation,
                revision,
            )
        )
        row = db.execute("SELECT request,receipt FROM predictions WHERE ref=?", (key,)).fetchone()
        if row is None:
            continue
        _require(record is not None and row["request"] == canonical(request))
        receipt = None
        if row["receipt"] is not None:
            receipt = json.loads(row["receipt"])
            _require(
                set(receipt) == {"request", "outputs", "receipt_ref"}
                and receipt["request"] == request
                and isinstance(receipt["outputs"], dict)
            )
            require_ref(receipt["receipt_ref"])
        receipts[arm] = receipt
    if record is None:
        return summary
    _require(record["definition"] == canonical(definition))
    if record["result"] is None:
        if receipts:
            summary["state"] = "held"
            summary["reason_code"] = "evaluation_outcome_unknown"
        return summary
    result = json.loads(record["result"])
    _require(
        set(result)
        == {
            "state",
            "execution_ref",
            "freeze_ref",
            "freeze_revision",
            "evidence_origin",
            "rows",
            "predictions",
            "metrics",
            "unresolved",
            "excluded",
            "result_digest",
            "result_ref",
        }
    )
    result_ref, result_digest = result.pop("result_ref"), result.pop("result_digest")
    _require(
        result_digest == digest(result) and result_ref == "starter-result-" + result_digest[:40]
    )
    _require(result["state"] == "scored" and result["execution_ref"] == p.execution_ref)
    for key in ("freeze_ref", "freeze_revision", "evidence_origin", "unresolved", "excluded"):
        _require(canonical(result[key]) == canonical(summary[key]))
    rows = result["rows"]
    _require(isinstance(rows, list) and 0 < len(rows) == scope.reserved_count)
    population = []
    handles = set()
    for item in rows:
        _require(set(item) == {"case_id", "family_id", "origin", "expected", "model", "comparator"})
        population.append({k: item[k] for k in ("case_id", "family_id", "origin", "expected")})
        handles.add("starter-case-" + digest([p.freeze_revision, item["case_id"]])[:40])
    _require(len(handles) == len(rows) and digest(population) == scope.population_digest)
    _require(set(result["predictions"]) == {"model", "comparator"})
    for arm in ("model", "comparator"):
        receipt = receipts.get(arm)
        _require(receipt is not None)
        assert receipt is not None
        _require(result["predictions"][arm] == {k: receipt[k] for k in ("request", "receipt_ref")})
        outputs = receipt["outputs"]
        _require(set(outputs) <= handles)
        for item in rows:
            handle = "starter-case-" + digest([p.freeze_revision, item["case_id"]])[:40]
            output = decision(outputs.get(handle, {"status": "missing"}))
            _require(
                item[arm] == output
                and not (output["status"] == "automatic" and output["label"] == "REVIEW")
            )
    metrics = _metrics(rows)
    _require(canonical(result["metrics"]) == canonical(metrics))
    return {**summary, "state": "scored", "metrics": metrics}


class StarterScoreInspection:
    def __init__(self, directory: Path, authority: InspectionAuthority):
        self.path = directory.absolute() / "starter-evaluations.sqlite3"
        self.authority = authority

    def summary(self, actor: IntakePrincipal, workflow_ref: str, execution_ref: str) -> Json:
        require_ref(workflow_ref)
        require_ref(execution_ref)
        with self.authority.guard(actor, workflow_ref, execution_ref) as scope:
            self.authority.revalidate(actor, scope)
            _check_scope(scope, actor, workflow_ref, execution_ref)
            try:
                with _open_db(self.path) as db:
                    result = _read(db, scope)
            except (ValueError, TypeError, KeyError, OSError, sqlite3.Error, RecursionError):
                raise IntakeError("starter_score_inspection_unavailable") from None
            self.authority.revalidate(actor, scope)
            _check_scope(scope, actor, workflow_ref, execution_ref)
            return result
