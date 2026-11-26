"""Owner-enrolled prospective permission and atomic local learning budgets."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sqlite3
import stat
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, cast

from opentour_cell.pilot import open_weight
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.simple_repair import RepairError, identifier, validate_features
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]
KIND = "standing_customer_mandate"
MODEL_AUTH = "opentour.pilot.open-weight-permission.v1"
AUTHORITY = "opentour.pilot.standing-learning-mandate.v1"
SOURCE = "opentour.pilot.delegated-source.v1"
CLASS = "opentour.pilot.delegated-class.v1"
TEXT_KEYS = {
    "mandate_id",
    "owner_id",
    "delegate",
    "workflow",
    "trace_source",
    "correction_source",
    "field",
    "trainer",
    "payer",
    "reviewer",
    "audience",
    "destination",
}
CAPS = {
    "max_runs",
    "max_fits",
    "max_total_seconds",
    "max_seconds_per_fit",
    "max_rows_per_run",
    "max_total_rows",
    "max_cost_cents",
}
SPEC_KEYS = (
    TEXT_KEYS
    | CAPS
    | {
        "mode",
        "connectors",
        "fields",
        "labels",
        "profile",
        "uses",
        "permitted_error_pairs",
        "agreement_revision",
        "expires_at",
    }
)
SOURCE_KEYS = {
    "source_revision",
    "trace_sha256",
    "correction_sha256",
    "workflow",
    "connector",
    "trace_source",
    "correction_source",
    "field",
    "fields",
    "rows",
}
RESERVE_KEYS = {"run_id", "plan_revision", "intake_revision", "residual_revision", "max_seconds"}


class MandateError(ValueError):
    """Safe reason why the delegated action is held."""


def require(check: object, code: str) -> None:
    if not check:
        raise MandateError(code)


def _id(value: Any) -> str:
    try:
        return identifier(value)
    except RepairError as exc:
        raise MandateError("invalid_mandate_identifier") from exc


def _hex(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch("[0-9a-f]{64}", value) is not None


def _count(value: Any, low: int, high: int) -> bool:
    return type(value) is int and low <= value <= high


def _list(value: Any, limit: int = 64) -> list[str]:
    require(isinstance(value, list) and 0 < len(value) <= limit, "invalid_mandate_scope")
    require(
        all(
            isinstance(x, str) and x and len(x) <= 128 and x.isprintable() and x.strip() == x
            for x in value
        ),
        "invalid_mandate_scope",
    )
    require(len(set(value)) == len(value), "duplicate_mandate_scope")
    return sorted(value)


def _attest(purpose: str, *args: object) -> str:
    return "att-" + digest([AUTHORITY, purpose, *args])


def _normalized(value: Mapping[str, Any]) -> Json:
    require(
        isinstance(value, Mapping) and SPEC_KEYS <= value.keys() <= SPEC_KEYS | {"model_fields"},
        "invalid_mandate_specification",
    )
    spec = dict(value)
    for key in TEXT_KEYS:
        _id(spec[key])
    require(
        spec["mode"] in ("manual", "autonomous")
        and spec["trace_source"] != spec["correction_source"]
        and spec["owner_id"] != spec["delegate"]
        and spec["reviewer"] not in {spec["delegate"], spec["trainer"]},
        "independent_owner_reviewer_and_source_required",
    )
    for key in ("connectors", "fields", "labels", "uses"):
        spec[key] = _list(spec[key], 8 if key in {"labels", "fields"} else 64)
    if "model_fields" in spec:
        spec["model_fields"] = _list(spec["model_fields"], 8)
        require(
            set(spec["model_fields"]) <= set(spec["fields"]) and "train" in spec["uses"],
            "model_projection_outside_authority",
        )
    for connector in spec["connectors"]:
        _id(connector)
    try:
        validate_features(spec["fields"][0], spec["fields"])
    except RepairError as exc:
        raise MandateError("decision_time_scope_required") from exc
    require(
        2 <= len(spec["labels"]) <= 8 and not any("<|" in label for label in spec["labels"]),
        "invalid_mandate_labels",
    )
    require(
        {"inspect", "evaluate"} <= set(spec["uses"]) <= {"inspect", "evaluate", "train"},
        "invalid_mandate_uses",
    )
    require(_hex(spec["agreement_revision"]), "operating_agreement_revision_required")
    pairs = spec["permitted_error_pairs"]
    require(isinstance(pairs, list) and 0 < len(pairs) <= 56, "permitted_error_pairs_required")
    checked = set()
    for pair in pairs:
        require(
            isinstance(pair, list)
            and len(pair) == 2
            and all(isinstance(x, str) and x in spec["labels"] for x in pair)
            and pair[0] != pair[1],
            "invalid_permitted_error_pair",
        )
        checked.add(tuple(pair))
    require(len(checked) == len(pairs), "duplicate_permitted_error_pair")
    spec["permitted_error_pairs"] = [list(pair) for pair in sorted(checked)]
    for key in CAPS:
        require(_count(spec[key], 0, 10_000_000), "invalid_mandate_budget")
    require(
        spec["max_cost_cents"] == 0
        and spec["max_runs"] >= 1
        and 1 <= spec["max_rows_per_run"] <= min(5000, spec["max_total_rows"])
        and spec["max_fits"] <= spec["max_runs"]
        and spec["max_seconds_per_fit"] <= min(300, spec["max_total_seconds"]),
        "invalid_mandate_budget",
    )
    if "train" in spec["uses"]:
        require(
            spec["profile"] == open_weight.PROFILE
            and spec["max_fits"] > 0
            and spec["max_seconds_per_fit"] > 0,
            "unsupported_standing_model_profile",
        )
    else:
        require(
            spec["profile"] is None
            and not any(spec[k] for k in ("max_fits", "max_total_seconds", "max_seconds_per_fit")),
            "rule_only_compute_must_be_zero",
        )
    require(_count(spec["expires_at"], 1, 2**53 - 1), "mandate_expiry_required")
    return spec


class LearningMandates:
    """A trusted local operator pins the original owner once for each engagement."""

    def __init__(
        self, directory: str | Path, organization: str, engagement: str, *, owner: str | None = None
    ):
        self.directory = Path(directory).absolute()
        self.organization, self.engagement = _id(organization), _id(engagement)
        self.scope = digest([self.organization, self.engagement])
        self.path = self.directory / "learning-mandates.sqlite3"
        if owner is not None:
            _id(owner)
            self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(
            self.directory.is_dir() and not self.directory.is_symlink(),
            "private_mandate_custody_required",
        )
        info = self.directory.stat()
        require(
            info.st_uid == os.getuid() and not info.st_mode & 0o077,
            "private_mandate_custody_required",
        )
        if owner is not None:
            descriptor = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(descriptor)
        require(
            self.path.is_file() and not self.path.is_symlink(), "registered_mandate_owner_required"
        )
        info = self.path.stat()
        require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.getuid()
            and info.st_nlink == 1
            and not info.st_mode & 0o077,
            "private_mandate_custody_required",
        )
        if owner is not None:
            with sqlite3.connect(self.path, timeout=10) as db:
                db.executescript("""
                CREATE TABLE IF NOT EXISTS owners(
                    scope TEXT PRIMARY KEY, actor TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS mandates(
                    scope TEXT, id TEXT, revision TEXT, spec TEXT, withdrawn INTEGER,
                    PRIMARY KEY(scope,id));
                CREATE TABLE IF NOT EXISTS renewals(
                    scope TEXT, prior TEXT, successor TEXT, PRIMARY KEY(scope,prior),
                    UNIQUE(scope,successor));
                CREATE TABLE IF NOT EXISTS runs(
                    scope TEXT, mandate TEXT, id TEXT, source TEXT, objects TEXT, request TEXT,
                    rows INTEGER, PRIMARY KEY(scope,id), UNIQUE(scope,mandate,source),
                    UNIQUE(scope,mandate,objects));
                CREATE TABLE IF NOT EXISTS classes(
                    scope TEXT, run TEXT, document TEXT, PRIMARY KEY(scope,run));
                CREATE TABLE IF NOT EXISTS reservations(
                    scope TEXT, mandate TEXT, id TEXT, run TEXT, request TEXT, seconds INTEGER,
                    state TEXT, bundle TEXT, settlement TEXT,
                    PRIMARY KEY(scope,id), UNIQUE(scope,run));
                """)
            with self._db() as db:
                db.execute("INSERT OR IGNORE INTO owners VALUES (?,?)", (self.scope, owner))
                require(self._owner(db) == owner, "registered_owner_mismatch")
        else:
            with self._db(write=False) as db:
                self._owner(db)

    @contextmanager
    def _db(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _authorized_db(self, *, write: bool = True) -> Iterator[sqlite3.Connection]:
        # Use the same engagement lock as custody before opening a productive mandate
        # transaction. Read-only checks can also run within an existing intake guard.
        try:
            store = LocalTrainingStore(
                self.directory / "training.sqlite3", self.organization, existing_only=True
            )
            with (
                store.engagement_guard(engagement_id=self.engagement, read_only=not write),
                self._db(write=write) as db,
            ):
                yield db
        except (OSError, sqlite3.Error, TrainingError) as exc:
            raise MandateError("standing_engagement_unavailable") from exc

    def _owner(self, db: sqlite3.Connection) -> str:
        row = db.execute("SELECT actor FROM owners WHERE scope=?", (self.scope,)).fetchone()
        require(row is not None, "registered_mandate_owner_required")
        return str(row["actor"])

    def _mandate(self, db: sqlite3.Connection, key: str) -> tuple[sqlite3.Row, Json]:
        row = db.execute(
            "SELECT * FROM mandates WHERE scope=? AND id=?", (self.scope, _id(key))
        ).fetchone()
        require(row is not None, "standing_mandate_unavailable")
        return row, json.loads(row["spec"])

    def _active(self, row: sqlite3.Row, spec: Json, actor: str) -> None:
        require(actor == spec["delegate"], "standing_delegate_mismatch")
        require(spec["mode"] == "autonomous", "manual_learning_only")
        require(row["withdrawn"] is None, "standing_mandate_withdrawn")
        require(time.time() < spec["expires_at"], "standing_mandate_expired")

    def _receipt(self, row: sqlite3.Row, spec: Json) -> Json:
        return {
            "schema_version": AUTHORITY,
            "authority_kind": KIND,
            "organization": self.organization,
            "engagement": self.engagement,
            "mandate_id": row["id"],
            "mandate_revision": row["revision"],
            "owner_id": spec["owner_id"],
            "delegate": spec["delegate"],
            "expires_at": spec["expires_at"],
            "scope": spec,
        }

    def _enroll(self, db: sqlite3.Connection, spec: Mapping[str, Any], actor: str) -> Json:
        data = _normalized(spec)
        require(data["expires_at"] > time.time(), "standing_mandate_expired")
        require(self._owner(db) == actor == data["owner_id"], "registered_owner_approval_required")
        revision = digest([AUTHORITY, self.scope, data])
        db.execute(
            "INSERT OR IGNORE INTO mandates VALUES (?,?,?,?,NULL)",
            (self.scope, data["mandate_id"], revision, canonical(data)),
        )
        row, existing = self._mandate(db, data["mandate_id"])
        require(row["revision"] == revision, "immutable_mandate_conflict")
        require(row["withdrawn"] is None, "standing_mandate_withdrawn")
        return self._receipt(row, existing)

    def enroll(self, spec: Mapping[str, Any], *, actor: str) -> Json:
        with self._authorized_db() as db:
            return self._enroll(db, spec, actor)

    def reauthorize(self, previous: str, spec: Mapping[str, Any], *, actor: str) -> Json:
        """Record fresh owner authority without clearing previous use or resurrecting old runs."""
        with self._authorized_db() as db:
            prior, old = self._mandate(db, previous)
            require(
                actor == self._owner(db) == old["owner_id"], "registered_owner_approval_required"
            )
            require(
                prior["withdrawn"] is not None or time.time() >= old["expires_at"],
                "prior_mandate_still_active",
            )
            successor = _id(spec.get("mandate_id"))
            require(successor != previous, "fresh_mandate_identifier_required")
            related = db.execute(
                "SELECT prior,successor FROM renewals WHERE scope=? AND (prior=? OR successor=?)",
                (self.scope, previous, successor),
            ).fetchone()
            if related:
                require(tuple(related) == (previous, successor), "reauthorization_conflict")
            else:
                require(
                    not db.execute(
                        "SELECT 1 FROM mandates WHERE scope=? AND id=?", (self.scope, successor)
                    ).fetchone(),
                    "fresh_mandate_identifier_required",
                )
                db.execute("INSERT INTO renewals VALUES(?,?,?)", (self.scope, previous, successor))
            receipt = self._enroll(db, spec, actor)
            return dict(receipt, supersedes=previous, prior_revision=prior["revision"])

    def inspect(self, mandate_id: str, *, actor: str | None = None) -> Json:
        with self._db(write=False) as db:
            row, spec = self._mandate(db, mandate_id)
            require(
                actor is None or actor in {spec["owner_id"], spec["delegate"]},
                "mandate_scope_mismatch",
            )
            runs = db.execute(
                "SELECT count(*),coalesce(sum(rows),0) FROM runs WHERE scope=? AND mandate=?",
                (self.scope, mandate_id),
            ).fetchone()
            fits = db.execute(
                "SELECT count(*),coalesce(sum(seconds),0) FROM reservations "
                "WHERE scope=? AND mandate=?",
                (self.scope, mandate_id),
            ).fetchone()
            return {
                **self._receipt(row, spec),
                "withdrawn": row["withdrawn"] is not None,
                "expired": time.time() >= spec["expires_at"],
                "runs": runs[0],
                "rows": runs[1],
                "reserved_fits": fits[0],
                "reserved_seconds": fits[1],
            }

    @contextmanager
    def guard_evaluation_source(
        self, mandate_id: str, run_id: str, *, actor: str
    ) -> Iterator[Json]:
        """Pin an already-budgeted source selection until the scoped operation commits."""
        with self._db() as db:
            row, spec = self._mandate(db, mandate_id)
            self._active(row, spec, actor)
            require("evaluate" in spec["uses"], "evaluation_outside_prospective_scope")
            run = self._run_row(db, run_id)
            require(run["mandate"] == mandate_id, "delegated_run_unavailable")
            yield self._source_receipt(run, row, spec)

    @contextmanager
    def guard_registered_owner(self, owner_id: str) -> Iterator[None]:
        """Pin the existing owner identity without admitting learning or consuming budget."""
        with self._db() as db:
            require(self._owner(db) == owner_id, "registered_owner_approval_required")
            yield

    def _source_receipt(self, run: sqlite3.Row, row: sqlite3.Row, spec: Json) -> Json:
        rid, rev = run["id"], row["revision"]
        return {
            **self._receipt(row, spec),
            "schema_version": SOURCE,
            "run_id": rid,
            "delegation_id": "delegation-" + digest([rev, rid])[:32],
            "source_request": json.loads(run["request"]),
            "source_revision": run["source"],
            "source_attestation": _attest("source", rev, rid),
            "training_attestation": _attest("training-scope", rev, rid)
            if "train" in spec["uses"]
            else None,
            "agreement_attestation": _attest(
                "operating-agreement", rev, rid, spec["agreement_revision"]
            ),
            "agreement_revision": spec["agreement_revision"],
        }

    def authorize_source(self, mandate_id: str, request: Mapping[str, Any], *, actor: str) -> Json:
        require(
            isinstance(request, Mapping)
            and SOURCE_KEYS <= request.keys() <= SOURCE_KEYS | {"organization", "engagement"},
            "invalid_delegated_source_request",
        )
        req = dict(request)
        require(
            req.pop("organization", self.organization) == self.organization
            and req.pop("engagement", self.engagement) == self.engagement,
            "mandate_scope_mismatch",
        )
        req["fields"] = _list(req["fields"], 8)
        with self._authorized_db() as db:
            row, spec = self._mandate(db, mandate_id)
            self._active(row, spec, actor)
            require(
                all(
                    req[key] == spec[key]
                    for key in ("workflow", "trace_source", "correction_source", "field")
                )
                and req["connector"] in spec["connectors"]
                and set(req["fields"]) <= set(spec["fields"]),
                "source_outside_prospective_scope",
            )
            require(
                all(
                    _hex(req[key])
                    for key in ("source_revision", "trace_sha256", "correction_sha256")
                )
                and req["trace_sha256"] != req["correction_sha256"],
                "independent_source_hashes_required",
            )
            require(_count(req["rows"], 1, spec["max_rows_per_run"]), "source_row_budget_exceeded")
            objects = digest([req["trace_sha256"], req["correction_sha256"]])
            existing = db.execute(
                "SELECT * FROM runs WHERE scope=? AND mandate=? AND (source=? OR objects=?)",
                (self.scope, mandate_id, req["source_revision"], objects),
            ).fetchone()
            if existing is not None:
                require(existing["request"] == canonical(req), "source_change_conflict")
                return self._source_receipt(existing, row, spec)
            usage = db.execute(
                "SELECT count(*),coalesce(sum(rows),0) FROM runs WHERE scope=? AND mandate=?",
                (self.scope, mandate_id),
            ).fetchone()
            require(
                usage[0] < spec["max_runs"] and usage[1] + req["rows"] <= spec["max_total_rows"],
                "standing_source_budget_exhausted",
            )
            rid = "learning-" + digest([self.scope, row["revision"], req])[:40]
            db.execute(
                "INSERT INTO runs VALUES(?,?,?,?,?,?,?)",
                (
                    self.scope,
                    mandate_id,
                    rid,
                    req["source_revision"],
                    objects,
                    canonical(req),
                    req["rows"],
                ),
            )
            return self._source_receipt(self._run_row(db, rid), row, spec)

    def _run_row(self, db: sqlite3.Connection, run_id: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM runs WHERE scope=? AND id=?", (self.scope, _id(run_id))
        ).fetchone()
        require(row is not None, "delegated_run_unavailable")
        return cast(sqlite3.Row, row)

    def check_run(self, run_id: str, *, actor: str) -> Json:
        with self._authorized_db(write=False) as db:
            run = self._run_row(db, run_id)
            row, spec = self._mandate(db, run["mandate"])
            self._active(row, spec, actor)
            return self._source_receipt(run, row, spec)

    def confirm_class(
        self,
        run_id: str,
        *,
        actor: str,
        discovery_revision: str,
        candidate: Mapping[str, Any],
        existing_only: bool = False,
    ) -> Json:
        require(
            _hex(discovery_revision) and isinstance(candidate, Mapping), "invalid_delegated_class"
        )
        with self._authorized_db(write=not existing_only) as db:
            run = self._run_row(db, run_id)
            row, spec = self._mandate(db, run["mandate"])
            self._active(row, spec, actor)
            pair = [candidate.get("from_label"), candidate.get("to_label")]
            expected = "class-" + digest(pair)[:24]
            require(
                pair in spec["permitted_error_pairs"]
                and candidate.get("class_id") == expected
                and _count(candidate.get("count"), 2, run["rows"])
                and _count(candidate.get("groups"), 2, candidate["count"]),
                "unapproved_recurring_error_class",
            )
            document = {
                **self._receipt(row, spec),
                "schema_version": CLASS,
                "run_id": run_id,
                "class_id": expected,
                "discovery_revision": discovery_revision,
                "error_pair": pair,
                "candidate_revision": digest(dict(candidate)),
                "attestation": _attest(
                    "error-class", row["revision"], run_id, discovery_revision, dict(candidate)
                ),
            }
            encoded = canonical(document)
            found = db.execute(
                "SELECT document FROM classes WHERE scope=? AND run=?", (self.scope, run_id)
            ).fetchone()
            if found is not None:
                require(found[0] == encoded, "delegated_class_conflict")
            else:
                require(not existing_only, "delegated_class_confirmation_required")
                db.execute("INSERT INTO classes VALUES(?,?,?)", (self.scope, run_id, encoded))
            return document

    def reserve(self, mandate_id: str, request: Mapping[str, Any], *, actor: str) -> Json:
        require(
            isinstance(request, Mapping) and request.keys() == RESERVE_KEYS,
            "invalid_fit_reservation",
        )
        req = dict(request)
        require(
            all(
                _hex(req[key]) for key in ("plan_revision", "intake_revision", "residual_revision")
            ),
            "immutable_fit_revisions_required",
        )
        with self._authorized_db() as db:
            row, spec = self._mandate(db, mandate_id)
            self._active(row, spec, actor)
            run = self._run_row(db, req["run_id"])
            require(
                run["mandate"] == mandate_id and "train" in spec["uses"],
                "model_outside_prospective_scope",
            )
            admitted_fields = json.loads(run["request"])["fields"]
            require(
                set(spec.get("model_fields", admitted_fields)) <= set(admitted_fields),
                "model_projection_outside_admitted_source",
            )
            require(
                db.execute(
                    "SELECT 1 FROM classes WHERE scope=? AND run=?", (self.scope, run["id"])
                ).fetchone()
                is not None,
                "delegated_class_confirmation_required",
            )
            require(
                _count(req["max_seconds"], 1, spec["max_seconds_per_fit"]),
                "per_fit_budget_exceeded",
            )
            existing = db.execute(
                "SELECT * FROM reservations WHERE scope=? AND run=?", (self.scope, run["id"])
            ).fetchone()
            if existing is None:
                usage = db.execute(
                    "SELECT count(*),coalesce(sum(seconds),0) FROM reservations "
                    "WHERE scope=? AND mandate=?",
                    (self.scope, mandate_id),
                ).fetchone()
                require(
                    usage[0] < spec["max_fits"]
                    and usage[1] + req["max_seconds"] <= spec["max_total_seconds"],
                    "standing_fit_budget_exhausted",
                )
                rid = "fit-" + digest([row["revision"], req])[:40]
                db.execute(
                    "INSERT INTO reservations VALUES(?,?,?,?,?,?,'reserved',NULL,NULL)",
                    (self.scope, mandate_id, rid, run["id"], canonical(req), req["max_seconds"]),
                )
                existing = self._reservation(db, rid)
            require(existing["request"] == canonical(req), "fit_reservation_conflict")
            return {
                "reservation_id": existing["id"],
                "run_id": run["id"],
                "status": existing["state"],
                "reserved_seconds": existing["seconds"],
                "authority_kind": KIND,
            }

    def _reservation(self, db: sqlite3.Connection, key: str) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM reservations WHERE scope=? AND id=?", (self.scope, _id(key))
        ).fetchone()
        require(row is not None, "fit_reservation_unavailable")
        return cast(sqlite3.Row, row)

    def _grant(
        self, reservation: sqlite3.Row, row: sqlite3.Row, spec: Json, run: sqlite3.Row
    ) -> Json:
        req = json.loads(reservation["request"])
        return {
            "schema_version": MODEL_AUTH,
            "authority_kind": KIND,
            "organization": self.organization,
            "engagement": self.engagement,
            "profile": open_weight.PROFILE,
            "base_sha256": open_weight.BASE_SHA256,
            "model_revision": open_weight.REVISION,
            "mandate_id": row["id"],
            "mandate_revision": row["revision"],
            "reservation_id": reservation["id"],
            "run_id": run["id"],
            "delegate": spec["delegate"],
            "owner_id": spec["owner_id"],
            "reviewer": spec["reviewer"],
            "audience": spec["audience"],
            "agreement_revision": spec["agreement_revision"],
            "source_revision": run["source"],
            "connector": json.loads(run["request"])["connector"],
            "allowed_uses": spec["uses"],
            "publication": "independent_slack_approval_required",
            "plan_revision": req["plan_revision"],
            "intake_revision": req["intake_revision"],
            "residual_revision": req["residual_revision"],
            "rights_attestation": _attest("model-fit", row["revision"], reservation["id"]),
            "trainer": spec["trainer"],
            "payer": spec["payer"],
            "destination": spec["destination"],
            "max_seconds": reservation["seconds"],
            "max_cost_cents": 0,
            "hourly_cost_cents": 0,
            "expires_at": spec["expires_at"],
            "features": spec.get("model_fields", json.loads(run["request"])["fields"]),
            "labels": spec["labels"],
            **dict.fromkeys(open_weight.GRANTS, True),
        }

    def grant(self, reservation_id: str, *, actor: str) -> Json:
        with self._authorized_db(write=False) as db:
            res = self._reservation(db, reservation_id)
            row, spec = self._mandate(db, res["mandate"])
            self._active(row, spec, actor)
            return self._grant(res, row, spec, self._run_row(db, res["run"]))

    def claim_fit(self, reservation_id: str, *, actor: str, bundle: str | Path) -> Json:
        output = Path(bundle).absolute()
        require(
            output.resolve() == output
            and output.is_relative_to(self.directory)
            and output != self.directory,
            "private_new_fit_destination_required",
        )
        with self._authorized_db() as db:
            res = self._reservation(db, reservation_id)
            row, spec = self._mandate(db, res["mandate"])
            self._active(row, spec, actor)
            require(res["state"] == "reserved", "fit_claim_already_used")
            require(not output.exists(), "private_new_fit_destination_required")
            prior = db.execute(
                "SELECT 1 FROM reservations WHERE scope=? AND bundle=?", (self.scope, str(output))
            ).fetchone()
            require(prior is None, "fit_destination_already_claimed")
            db.execute(
                "UPDATE reservations SET state='running',bundle=? WHERE scope=? AND id=?",
                (str(output), self.scope, reservation_id),
            )
            return self._grant(res, row, spec, self._run_row(db, res["run"]))

    def settle(
        self,
        reservation_id: str,
        *,
        actor: str,
        elapsed_seconds: float,
        artifact_sha256: str | None = None,
    ) -> Json:
        require(
            isinstance(elapsed_seconds, (int, float))
            and not isinstance(elapsed_seconds, bool)
            and math.isfinite(elapsed_seconds)
            and elapsed_seconds >= 0
            and (artifact_sha256 is None or _hex(artifact_sha256)),
            "invalid_fit_settlement",
        )
        # A claimed fit may settle measured usage after a hold. Accepting an artifact
        # still requires live domain authority.
        settlement = self._authorized_db() if artifact_sha256 is not None else self._db()
        with settlement as db:
            res = self._reservation(db, reservation_id)
            row, spec = self._mandate(db, res["mandate"])
            require(actor == spec["delegate"], "standing_delegate_mismatch")
            if artifact_sha256 is not None:
                self._active(row, spec, actor)
                require(res["bundle"] is not None, "fit_has_not_started")
                validated = open_weight.inspect_open_weight(
                    Path(res["bundle"]),
                    grant=self._grant(res, row, spec, self._run_row(db, res["run"])),
                )
                require(validated["artifact_sha256"] == artifact_sha256, "fit_artifact_mismatch")
            require(res["state"] in ("running", "settled"), "fit_has_not_started")
            require(elapsed_seconds <= res["seconds"], "measured_fit_exceeded_reservation")
            payload = {
                "reservation_id": res["id"],
                "elapsed_seconds": elapsed_seconds,
                "artifact_sha256": artifact_sha256,
                "reserved_seconds_charged": res["seconds"],
                "publication": "independent_reviewer_required",
            }
            encoded = canonical(payload)
            require(res["settlement"] in (None, encoded), "immutable_fit_settlement_conflict")
            db.execute(
                "UPDATE reservations SET state='settled',settlement=? WHERE scope=? AND id=?",
                (encoded, self.scope, reservation_id),
            )
            return payload

    def withdraw(self, mandate_id: str, *, actor: str) -> Json:
        cancellations = []
        with self._db() as db:
            row, spec = self._mandate(db, mandate_id)
            require(
                self._owner(db) == actor == spec["owner_id"], "registered_owner_approval_required"
            )
            db.execute(
                "UPDATE mandates SET withdrawn=coalesce(withdrawn,?) WHERE scope=? AND id=?",
                (int(time.time()), self.scope, mandate_id),
            )
            for res in db.execute(
                "SELECT * FROM reservations WHERE scope=? AND mandate=? AND bundle IS NOT NULL",
                (self.scope, mandate_id),
            ):
                cancellations.append(
                    (
                        Path(res["bundle"]),
                        self._grant(res, row, spec, self._run_row(db, res["run"])),
                    )
                )
        # Commit the denial first. A restarted custodian can retry cancellation.
        pending = 0
        for bundle, permission in cancellations:
            try:
                open_weight.withdraw_open_weight(bundle, grant=permission)
            except (OSError, open_weight.OpenWeightError):
                # A damaged destination must not keep the remaining fits from receiving withdrawal.
                pending += 1
        return {
            "mandate_id": mandate_id,
            "withdrawn": True,
            "signaled_fit_destinations": len(cancellations) - pending,
            "pending_fit_destinations": pending,
            "authority_kind": KIND,
        }


def main(argv: list[str] | None = None) -> int:
    """Owner-local setup. The agent's MCP API cannot enroll its own prospective permission."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("enroll", "status", "withdraw", "reauthorize"):
        part = commands.add_parser(command)
        part.add_argument("--directory", type=Path, required=True)
        part.add_argument("--organization", required=True)
        part.add_argument("--engagement", required=True)
        if command in ("enroll", "reauthorize", "withdraw"):
            part.add_argument("--owner", required=True)
        else:
            part.add_argument("--actor", required=True)
        if command in ("enroll", "reauthorize"):
            part.add_argument("--spec", type=Path, required=True)
        if command in ("status", "withdraw"):
            part.add_argument("--mandate", required=True)
        if command == "reauthorize":
            part.add_argument("--supersedes", required=True)
    args = parser.parse_args(argv)
    try:
        registry = LearningMandates(
            args.directory,
            args.organization,
            args.engagement,
            owner=args.owner if args.command == "enroll" else None,
        )
        if args.command in ("enroll", "reauthorize"):
            metadata = args.spec.lstat()
            require(
                stat.S_ISREG(metadata.st_mode)
                and metadata.st_uid == os.getuid()
                and not metadata.st_mode & 0o077
                and metadata.st_size <= 65536,
                "private_mandate_spec_required",
            )
            document = json.loads(args.spec.read_bytes())
            if args.command == "enroll":
                receipt = registry.enroll(document, actor=args.owner)
            else:
                receipt = registry.reauthorize(args.supersedes, document, actor=args.owner)
        elif args.command == "withdraw":
            receipt = registry.withdraw(args.mandate, actor=args.owner)
        else:
            receipt = registry.inspect(args.mandate, actor=args.actor)
        print(canonical(receipt))
    except (ValueError, OSError, sqlite3.Error) as exc:
        reason = (
            str(exc) if isinstance(exc, MandateError) else "invalid_or_unavailable_mandate_setup"
        )
        print(canonical({"state": "held", "reason": reason}))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
