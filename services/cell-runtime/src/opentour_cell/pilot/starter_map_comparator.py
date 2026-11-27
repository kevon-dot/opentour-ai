"""Execute the confirmed, authorized typed map over the evaluator's frozen inputs."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.guided_decision_map import Condition
from opentour_cell.pilot.intake_admission import IntakeWork, require_readonly_snapshot
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _has_table, _open_db
from opentour_cell.pilot.starter_cases import StarterCases
from opentour_cell.pilot.starter_scoring import (
    EvaluationPermission,
    PredictionReceipt,
    PredictionRequest,
)
from opentour_cell.pilot.training import _check_directory, _check_file


class TypedBranch(Protocol):
    @property
    def conditions(self) -> tuple[Condition, ...]: ...
    @property
    def action(self) -> str: ...
    @property
    def precedence(self) -> int | None: ...


class TypedContext(Protocol):
    @property
    def comparator_ref(self) -> str: ...
    @property
    def comparator_revision(self) -> str: ...
    @property
    def confirmed_map_ref(self) -> str: ...
    @property
    def source_revision(self) -> str: ...
    @property
    def source_digest(self) -> str: ...
    @property
    def field_kinds(self) -> tuple[tuple[str, str], ...]: ...
    @property
    def branches(self) -> tuple[TypedBranch, ...]: ...
    @property
    def fallback_action(self) -> str | None: ...


class ComparatorAuthority(Protocol):
    def comparator_context(
        self, actor: IntakePrincipal, work: IntakeWork, permission: EvaluationPermission
    ) -> TypedContext:
        """Under the current execution guard, return confirmed, decision-time-eligible rules."""
        ...

    def revalidate(
        self, actor: IntakePrincipal, work: IntakeWork, permission: EvaluationPermission
    ) -> None: ...


def _require(value: object) -> None:
    if not value:
        raise IntakeError("starter_comparator_unavailable")


_FILE = "starter-comparator-receipts.sqlite3"


def initialize_journal(directory: Path) -> None:
    """Provision a new customer-private journal once, outside lookup and execution."""
    path = directory.absolute() / _FILE
    _require(path.parent.resolve(strict=True) == path.parent)
    _check_directory(path)
    _require(not any(os.path.lexists(str(path) + s) for s in ("-wal", "-shm", "-journal")))
    os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
    with closing(sqlite3.connect(path.as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute(
            "CREATE TABLE receipts(operation TEXT PRIMARY KEY, "
            "body TEXT NOT NULL, seal TEXT NOT NULL)"
        )


def _action(context: TypedContext, inputs: Mapping[str, object]) -> dict[str, str]:
    fields: dict[str, str | Decimal | None] = {}
    for name, kind in context.field_kinds:
        value = inputs.get(name)
        if value is None:
            fields[name] = None
        elif kind == "reference" and isinstance(value, str):
            fields[name] = value
        elif kind == "number" and type(value) in (int, float, str):
            try:
                number = Decimal(str(value))
            except InvalidOperation:
                return {"status": "missing"}
            if not number.is_finite():
                return {"status": "missing"}
            fields[name] = number
        else:
            return {"status": "missing"}
    matched = [b for b in context.branches if all(c.matches(fields) for c in b.conditions)]
    label = context.fallback_action
    if matched:
        if all(b.precedence is not None for b in matched):
            first = min(b.precedence for b in matched if b.precedence is not None)
            matched = [b for b in matched if b.precedence == first]
        actions = {b.action for b in matched}
        label = actions.pop() if len(actions) == 1 else None
    if label is None:
        return {"status": "missing"}
    return {"status": "review"} if label == "REVIEW" else {"status": "automatic", "label": label}


class StarterMapComparator:
    def __init__(
        self,
        directory: Path,
        actor: IntakePrincipal,
        work: IntakeWork,
        cases: StarterCases,
        authority: ComparatorAuthority,
        permission: EvaluationPermission,
        comparator_ref: str,
        receipt_key: bytes,
    ):
        _require(type(receipt_key) is bytes and len(receipt_key) >= 32)
        self.path = directory.absolute() / _FILE
        self.actor, self.work, self.cases = actor, work, cases
        self.authority, self.permission = authority, permission
        self.reference, self.revision = comparator_ref, permission.comparator_revision
        self._key = receipt_key

    def _current(
        self, request: PredictionRequest
    ) -> tuple[TypedContext, dict[str, dict[str, Any]]]:
        p = self.permission
        self.authority.revalidate(self.actor, self.work, p)
        context = self.authority.comparator_context(self.actor, self.work, p)
        frozen = self.cases.evaluation_inputs(self.actor, self.work)
        _require(
            (p.tenant, p.workflow_ref, p.epoch, p.run_ref, p.evaluator)
            == (
                self.actor.tenant,
                self.work.workflow_ref,
                self.work.resource.engagement_epoch,
                self.work.run_ref,
                self.actor.actor,
            )
        )
        _require(
            (p.freeze_ref, p.freeze_revision, p.plan_digest, p.evaluator)
            == (
                frozen.freeze_ref,
                frozen.freeze_revision,
                digest(asdict(frozen.plan)),
                frozen.plan.evaluator_ref,
            )
        )
        _require(
            context.confirmed_map_ref
            and context.source_digest == frozen.source_digest
            and context.source_revision == self.work.revisions[0]
            and context.comparator_ref == self.reference == frozen.plan.comparator_ref
            and context.comparator_revision == self.revision
            and 0 < len(frozen.cases) <= p.maximum_cases <= 1000
        )
        inputs = {
            "starter-case-" + digest([p.freeze_revision, c.case_id])[:40]: dict(c.decision_inputs)
            for c in frozen.cases
        }
        kinds = dict(context.field_kinds)
        _require(
            all(
                c.field in p.input_fields and c.field in kinds
                for b in context.branches
                for c in b.conditions
            )
        )
        _require(all(set(fields) <= set(p.input_fields) for fields in inputs.values()))
        input_digest = digest(inputs)
        definition = dict(
            permission=asdict(p), input_digest=input_digest, comparator_revision=self.revision
        )
        _require(
            request
            == PredictionRequest(
                "starter-prediction-" + digest([definition, "comparator"])[:40],
                p.execution_ref,
                "comparator",
                p.freeze_revision,
                input_digest,
                self.reference,
                self.revision,
            )
        )
        self.authority.revalidate(self.actor, self.work, p)
        _require(time.time() < p.expires_at)
        return context, inputs

    def _receipt(
        self, db: sqlite3.Connection, request: PredictionRequest
    ) -> PredictionReceipt | None:
        _require(_has_table(db, "receipts"))
        row = db.execute(
            "SELECT body,seal FROM receipts WHERE operation=?", (request.operation_ref,)
        ).fetchone()
        if row is None:
            return None
        body, seal = row
        _require(hmac.compare_digest(self._seal(body), seal))
        value = json.loads(body)
        _require(value["request"] == asdict(request))
        return PredictionReceipt(request, "map-prediction-" + digest(value)[:40], value["outputs"])

    def _seal(self, body: str) -> str:
        return hmac.new(
            self._key, b"opentour.starter-map-comparator.v1\0" + body.encode(), hashlib.sha256
        ).hexdigest()

    def lookup(self, request: PredictionRequest) -> PredictionReceipt | None:
        self._current(request)
        _require(self.path.parent.resolve(strict=True) == self.path.parent)
        with _open_db(self.path) as db:
            _require(db is not None)
            assert db is not None
            receipt = self._receipt(db, request)
            self._current(request)
            return receipt

    def execute(
        self, request: PredictionRequest, inputs: Mapping[str, Mapping[str, object]]
    ) -> PredictionReceipt:
        context, expected = self._current(request)
        _require(canonical(inputs) == canonical(expected))
        _require(
            os.path.lexists(self.path) and self.path.parent.resolve(strict=True) == self.path.parent
        )
        _check_file(self.path)
        require_readonly_snapshot(self.path)
        with (
            closing(sqlite3.connect(self.path.as_uri() + "?mode=rw", uri=True, timeout=10)) as db,
            db,
        ):
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN IMMEDIATE")
            self._current(request)
            receipt = self._receipt(db, request)
            if receipt is None:
                outputs = {handle: _action(context, fields) for handle, fields in expected.items()}
                self._current(request)
                body = canonical(dict(request=asdict(request), outputs=outputs))
                db.execute(
                    "INSERT INTO receipts VALUES(?,?,?)",
                    (request.operation_ref, body, self._seal(body)),
                )
                receipt = self._receipt(db, request)
                assert receipt is not None
            self._current(request)
        return receipt
