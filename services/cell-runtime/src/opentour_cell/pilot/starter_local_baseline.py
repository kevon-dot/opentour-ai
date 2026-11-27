"""Run an already registered CPU text model over the exact frozen starter inputs.

The scoring worker holds the evaluation guard. This adapter revalidates its
current authority at each boundary and keeps a separate, authenticated receipt
journal. It has no training, network, target-key or delivery operation.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import sqlite3
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot import text_classifier as cpu
from opentour_cell.pilot.intake_admission import IntakeWork, require_readonly_snapshot
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.model_registry import ModelRegistry
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_cases import StarterCases, _input_fields
from opentour_cell.pilot.starter_scoring import (
    EvaluationAuthority,
    EvaluationPermission,
    PredictionReceipt,
    PredictionRequest,
)
from opentour_cell.pilot.training import _check_file

Json = dict[str, Any]


def _require(condition: object) -> None:
    if not condition:
        raise IntakeError("local_baseline_unavailable")


def _private_bytes(path: Path, maximum: int) -> bytes:
    _require(path.is_absolute() and path.resolve(strict=True) == path)
    _check_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and not info.st_mode & 0o077
            and info.st_nlink == 1
            and info.st_size <= maximum
        )
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(maximum + 1)
        current = path.lstat()
        _require(
            len(raw) <= maximum
            and (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            == (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        )
        return raw
    finally:
        os.close(fd)


class LocalStarterBaseline:
    """One explicit evaluation capability and a pre-existing private model.

    Construct a new instance for an explicitly authorized rerun. The caller must
    hold EvaluationAuthority.guard while executing or looking up predictions.
    The receipt key is provisioned privately by the custodian, never by a Slack
    action. Neither this constructor nor metadata observation creates files.
    """

    def __init__(
        self,
        directory: Path,
        *,
        actor: IntakePrincipal,
        work: IntakeWork,
        permission: EvaluationPermission,
        authority: EvaluationAuthority,
        registry: ModelRegistry,
        cases: StarterCases,
        artifact: Path,
        receipt_key: Path,
        input_field: str | None,
    ):
        self.path = directory.absolute() / "starter-local-predictions.sqlite3"
        self.actor, self.work, self.permission = actor, work, permission
        self.authority, self.registry, self.cases = authority, registry, cases
        self.artifact, self.receipt_key, self.input_field = artifact, receipt_key, input_field
        self.reference, self.revision = permission.model_ref, permission.model_revision

    def _text(self, fields: Mapping[str, object]) -> str:
        """Apply only the registered input encoding, before CPU feature extraction."""
        _require(set(fields) == set(self.permission.input_fields))
        if self.input_field is not None:
            _require(set(fields) == {self.input_field})
            value = fields[self.input_field]
            _require(isinstance(value, str))
            assert isinstance(value, str)
            text = value
        else:
            text = canonical(_input_fields(tuple(fields.items())))
        cpu.words(text)
        return text

    def _identity(self) -> tuple[model.ModelBinding, Json, bytes]:
        p = self.permission
        self.authority.revalidate(self.actor, self.work, p)
        _require(
            (p.tenant, p.evaluator, p.workflow_ref, p.epoch, p.run_ref)
            == (
                self.actor.tenant,
                self.actor.actor,
                self.work.workflow_ref,
                self.work.resource.engagement_epoch,
                self.work.run_ref,
            )
            and p.expires_at > time.time()
            and 0 < len(p.input_fields) <= 32
            and len(set(p.input_fields)) == len(p.input_fields)
            and (self.input_field is None or p.input_fields == (self.input_field,))
            and type(p.maximum_cases) is int
            and 0 < p.maximum_cases <= cpu.MAX_HELDOUT_ROWS
        )
        binding = self.registry.binding(self.reference)
        _require(
            binding.tenant == p.tenant
            and binding.immutable_revision == self.revision
            and binding.route is model.Route.LOCAL
            and binding.task == "classification"
            and binding.input_schema
            == (
                "starter-structured-fields-v1"
                if self.input_field is None
                else "starter-text-" + self.input_field + "-v1"
            )
            and binding.output_schema == "action-output-v1"
            and binding.preprocessing_revision == cpu.PROFILE
        )
        grant = self.registry.current_grant(self.actor.actor, binding, model.Purpose.BASELINE)
        _require(
            grant is not None
            and grant.revision == p.baseline_grant_revision
            and grant.valid_until > time.time()
            and set(p.input_fields) <= set(grant.permitted_fields)
        )
        raw = _private_bytes(self.artifact, 262144)
        _require(self.revision == "sha256-" + hashlib.sha256(raw).hexdigest())
        value = json.loads(raw)
        _require(isinstance(value, dict))
        _require(
            value.keys() == {"schema_version", "profile", "labels", "dimensions", "weights"}
            and value["schema_version"] == "opentour.pilot.cpu-text-model.v1"
            and value["profile"] == cpu.PROFILE
            and type(value["dimensions"]) is int
            and value["dimensions"] == cpu.DIMENSIONS
        )
        labels, weights = value["labels"], value["weights"]
        _require(
            isinstance(labels, list)
            and 2 <= len(labels) <= 8
            and all(isinstance(v, str) and 0 < len(v) <= 128 and v.isprintable() for v in labels)
            and len(set(labels)) == len(labels)
            and isinstance(weights, list)
            and len(weights) == len(labels)
            and all(
                isinstance(row, list)
                and len(row) == cpu.DIMENSIONS
                and all(type(v) is int and abs(v) <= 1000000 for v in row)
                for row in weights
            )
        )
        key = _private_bytes(self.receipt_key, 32)
        _require(len(key) == 32)
        self.authority.revalidate(self.actor, self.work, p)
        _require(
            p.expires_at > time.time()
            and self.registry.binding(self.reference) == binding
            and self.registry.current_grant(self.actor.actor, binding, model.Purpose.BASELINE)
            == grant
            and grant is not None
            and grant.valid_until > time.time()
        )
        return binding, value, key

    def observe(self, binding: model.ModelBinding, purpose: model.Purpose) -> model.Observation:
        _require(purpose is model.Purpose.BASELINE)
        current, _, _ = self._identity()
        _require(binding == current)
        now = int(time.time())
        return model.Observation(
            binding,
            "local-starter-artifact-" + digest(asdict(binding))[:32],
            now,
            min(now + 30, self.permission.expires_at),
            self.revision,
            True,
            False,
            False,
            False,
            False,
            False,
            {},
        )

    def _current(self, request: PredictionRequest) -> tuple[Json, bytes, Json]:
        binding, fitted, key = self._identity()
        p = self.permission
        frozen = self.cases.evaluation_inputs(self.actor, self.work)
        _require(
            (frozen.freeze_ref, frozen.freeze_revision, frozen.plan.evaluator_ref)
            == (p.freeze_ref, p.freeze_revision, self.actor.actor)
            and digest(asdict(frozen.plan)) == p.plan_digest
            and frozen.plan.baseline_ref in {"baseline-pending", self.reference}
            and frozen.plan.evaluated_model_ref in {None, self.reference}
            and 0 < len(frozen.cases) <= p.maximum_cases
        )
        inputs = {
            "starter-case-" + digest([frozen.freeze_revision, c.case_id])[:40]: dict(
                c.decision_inputs
            )
            for c in frozen.cases
        }
        definition = {
            "permission": asdict(p),
            "input_digest": digest(inputs),
            "comparator_revision": p.comparator_revision,
        }
        _require(
            request
            == PredictionRequest(
                "starter-prediction-" + digest([definition, "model"])[:40],
                p.execution_ref,
                "model",
                frozen.freeze_revision,
                digest(inputs),
                self.reference,
                self.revision,
            )
        )
        for fields in inputs.values():
            self._text(fields)
        self.authority.revalidate(self.actor, self.work, p)
        _require(p.expires_at > time.time())
        _require(self._identity() == (binding, fitted, key))
        return fitted, key, inputs

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        _require(self.path.parent.resolve(strict=True) == self.path.parent)
        if write:
            parent = self.path.parent.stat()
            _require(parent.st_uid == os.getuid() and not parent.st_mode & 0o077)
            if not os.path.lexists(self.path):
                self._absent()
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
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS receipts("
                    "operation TEXT PRIMARY KEY, body TEXT, seal TEXT)"
                )
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _seal(key: bytes, body: str) -> str:
        return hmac.new(
            key, b"opentour.starter-local-receipt.v1\0" + body.encode(), "sha256"
        ).hexdigest()

    def _absent(self) -> None:
        # A database lost during restore is not a fresh operation if any of its
        # rollback/WAL state remains. Do not open SQLite or replace those files.
        _require(
            not any(
                os.path.lexists(str(self.path) + suffix)
                for suffix in ("", "-wal", "-shm", "-journal")
            )
        )

    def _receipt(
        self, db: sqlite3.Connection, request: PredictionRequest, key: bytes
    ) -> PredictionReceipt | None:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not tables:
            return None
        _require("receipts" in tables)
        row = db.execute(
            "SELECT body,seal FROM receipts WHERE operation=?", (request.operation_ref,)
        ).fetchone()
        if row is None:
            return None
        _require(hmac.compare_digest(row[1], self._seal(key, row[0])))
        body = json.loads(row[0])
        _require(body["request"] == asdict(request))
        return PredictionReceipt(request, "local-prediction-" + digest(body)[:40], body["outputs"])

    def lookup(self, request: PredictionRequest) -> PredictionReceipt | None:
        _, key, _ = self._current(request)
        if not os.path.lexists(self.path):
            self._absent()
            return None
        with self._db() as db:
            receipt = self._receipt(db, request, key)
            _require(self._current(request)[1] == key)
            return receipt

    def execute(
        self, request: PredictionRequest, inputs: Mapping[str, Mapping[str, object]]
    ) -> PredictionReceipt:
        fitted, key, expected = self._current(request)
        _require(canonical(inputs) == canonical(expected))
        with self._db(write=True) as db:
            _require(self._current(request) == (fitted, key, expected))
            existing = self._receipt(db, request, key)
            if existing is not None:
                _require(self._current(request) == (fitted, key, expected))
                return existing
            outputs = {}
            for handle, fields in expected.items():
                _require(self._current(request) == (fitted, key, expected))
                label = cpu.predict(fitted, self._text(fields))
                outputs[handle] = (
                    {"status": "review"}
                    if label == "REVIEW"
                    else {"status": "automatic", "label": label}
                )
            body = canonical({"request": asdict(request), "outputs": outputs})
            _require(self._current(request) == (fitted, key, expected))
            db.execute(
                "INSERT INTO receipts VALUES(?,?,?)",
                (request.operation_ref, body, self._seal(key, body)),
            )
            receipt = self._receipt(db, request, key)
            assert receipt is not None
            _require(self._current(request) == (fitted, key, expected))
        return receipt
