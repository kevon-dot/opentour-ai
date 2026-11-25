"""Owner-local dataset registry, explicit training approval and durable CPU job results."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Collection, Iterator
from contextlib import closing, contextmanager, nullcontext
from pathlib import Path
from typing import Any

from opentour_cell.pilot.brief import decision_brief
from opentour_cell.pilot.readout import (
    INPUT_VERSION,
    MAX_MANIFEST_BYTES,
    ManifestError,
    evaluate_manifest,
    parse_manifest,
)
from opentour_cell.pilot.text_classifier import (
    CPU_LIMIT_SECONDS,
    EPOCH_CHOICES,
    PROFILE,
    TrainingError,
    check_budget,
    fit,
    predict,
)
from opentour_cell.pilot.training_data import (
    Dataset,
    canonical,
    digest,
    generated_dataset,
    generated_evaluation_dataset,
    identifier,
    permitted_actions,
    prepare,
)

PURPOSE = "local_text_classification"
LEASE_SECONDS = 30
_SCHEMA = """
CREATE TABLE IF NOT EXISTS owner (singleton INTEGER PRIMARY KEY CHECK (singleton=1),
 organization TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS datasets (
 id TEXT PRIMARY KEY, revision TEXT NOT NULL, reviewers TEXT NOT NULL, fields TEXT NOT NULL,
 preview_fields TEXT NOT NULL, previews TEXT NOT NULL, train_count INTEGER NOT NULL,
 heldout_count INTEGER NOT NULL, ceiling INTEGER NOT NULL, expires_at INTEGER,
 revoked INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS train_data (dataset TEXT PRIMARY KEY, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS heldout_data (dataset TEXT PRIMARY KEY, content TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS split_keys (fingerprint TEXT PRIMARY KEY,
 split TEXT NOT NULL, used_by TEXT);
CREATE TABLE IF NOT EXISTS evaluation_keys (dataset TEXT NOT NULL, fingerprint TEXT NOT NULL,
 PRIMARY KEY(dataset,fingerprint));
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, request TEXT UNIQUE, dataset TEXT NOT NULL, revision TEXT NOT NULL,
 epochs INTEGER NOT NULL, status TEXT NOT NULL, reviewer TEXT, lease_token TEXT, lease_until REAL,
 artifact TEXT, result TEXT, error TEXT);
CREATE TABLE IF NOT EXISTS artifacts (id TEXT PRIMARY KEY, model TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fit_checkpoints (job TEXT PRIMARY KEY, binding TEXT NOT NULL,
 digest TEXT NOT NULL, model TEXT NOT NULL, updates INTEGER NOT NULL, fit_cpu REAL NOT NULL);
CREATE TABLE IF NOT EXISTS engagement (singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 id TEXT NOT NULL UNIQUE, withdrawn_at INTEGER);
CREATE TABLE IF NOT EXISTS dataset_scope (dataset TEXT PRIMARY KEY, mode TEXT NOT NULL,
 destination TEXT NOT NULL, retention TEXT NOT NULL, train_digest TEXT, cohort_digest TEXT NOT NULL,
 baseline_result TEXT);
"""


_DATASET_QUERY = (
    "SELECT d.*,s.mode,s.destination,s.retention,s.train_digest,s.cohort_digest,"
    "s.baseline_result FROM datasets d LEFT JOIN dataset_scope s ON s.dataset=d.id "
)


def _cohort_digest(
    organization: str, name: str, revision: str, ceiling: int, rows: list[Any]
) -> str:
    return digest(["opentour.pilot.frozen-cohort.v1", organization, name, revision, ceiling, rows])


def _report_bytes(readout: dict[str, Any]) -> tuple[bytes, bytes]:
    # Byte-for-byte identical to the existing local scoring CLI's output contract.
    aggregate = (json.dumps(readout, sort_keys=True, allow_nan=False) + "\n").encode()
    return aggregate, decision_brief(readout).encode()


def _evidence(
    name: str,
    revision: str,
    mode: str,
    cohort: str,
    model: str | None,
    readout: dict[str, Any],
) -> dict[str, Any]:
    aggregate, brief = _report_bytes(readout)
    return {
        "dataset_id": name,
        "dataset_revision": revision,
        "mode": mode,
        "cohort_digest": cohort,
        "artifact_digest": model,
        "aggregate_digest": hashlib.sha256(aggregate).hexdigest(),
        "brief_digest": hashlib.sha256(brief).hexdigest(),
        "readout": readout,
    }


def _score(
    rows: list[Any], ceiling: int, model: dict[str, Any] | None, started: float | None = None
) -> dict[str, Any]:
    cases = rows
    if model is not None:
        cases = []
        for row in rows:
            if started is not None:
                check_budget(started)
            cases.append(
                {
                    "case_id": row["case_id"],
                    "reference": row["reference"],
                    "current": row["current"],
                    "repair": {"status": "automatic", "label": predict(model, row["text"])},
                }
            )
    return evaluate_manifest(
        {"schema_version": INPUT_VERSION, "error_ceiling_bps": ceiling, "cases": cases}
    )


def _check_directory(path: Path) -> None:
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.getuid() or parent.st_mode & 0o077:
        raise TrainingError("private_store_required")


def _check_file(path: Path) -> None:
    _check_directory(path)
    record = path.lstat()
    if (
        not stat.S_ISREG(record.st_mode)
        or record.st_uid != os.getuid()
        or record.st_mode & 0o077
        or record.st_nlink != 1
    ):
        raise TrainingError("private_store_required")


def _custody_hold(path: Path, organization: str) -> str | None:
    """Read only the closeout identity, outside any training transaction.

    A committed closeout covers this private directory even if just its training file is
    restored. Custody retains the row when it minimizes a completed agreement.
    """
    ledger = path.with_name("custody.sqlite3")
    if path == ledger:
        raise TrainingError("private_store_required")
    try:
        _check_file(ledger)
    except FileNotFoundError:
        if any(os.path.lexists(str(ledger) + suffix) for suffix in ("-journal", "-wal", "-shm")):
            raise TrainingError("custody_unavailable") from None
        return None
    except (OSError, TrainingError):
        raise TrainingError("custody_unavailable") from None
    try:
        with closing(
            sqlite3.connect(ledger.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA trusted_schema=OFF")
            db.execute("BEGIN")
            table = db.execute("SELECT type FROM sqlite_master WHERE name='closeout'").fetchone()
            if table != ("table",):
                raise TrainingError("custody_unavailable")
            rows = db.execute(
                "SELECT singleton,organization,engagement,withdrawn_at FROM closeout LIMIT 2"
            ).fetchall()
    except (OSError, sqlite3.Error):
        raise TrainingError("custody_unavailable") from None
    if not rows:
        # Initial closeout has no committed historical hold. Its own first operation
        # serializes withdrawal against admissions in the primary training database.
        return None
    singleton, owner, engagement, recorded = rows[0]
    if (
        len(rows) != 1
        or singleton != 1
        or not isinstance(engagement, str)
        or not re.fullmatch(r"[a-f0-9]{32}", engagement)
        or type(recorded) is not int
        or recorded < 0
    ):
        raise TrainingError("custody_unavailable")
    if owner != organization:
        raise TrainingError("custody_scope_mismatch")
    return engagement


@contextmanager
def _initialization_guard(path: Path, *, creator: bool) -> Iterator[None]:
    """Protect the first file creation and owner/schema commit as one observable operation."""
    fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY | os.O_NOFOLLOW)
    try:
        limit = time.monotonic() + 2
        mode = (fcntl.LOCK_EX if creator else fcntl.LOCK_SH) | fcntl.LOCK_NB
        while True:
            try:
                fcntl.flock(fd, mode)
                break
            except BlockingIOError:
                if time.monotonic() >= limit:
                    raise TrainingError("private_store_busy") from None
                time.sleep(0.005)
        yield
    finally:
        os.close(fd)


def _registration_probe(
    path: Path | str, organization: str, selection: tuple[str, str] | None
) -> str:
    path = Path(path)
    try:
        identifier(organization)
        if selection is not None:
            identifier(selection[0])
        _check_directory(path)
        if _custody_hold(path, organization) is not None:
            return "unavailable"
        with _initialization_guard(path, creator=False):
            try:
                _check_file(path)
            except FileNotFoundError:
                return "absent"
            return _enrollment_snapshot(path, organization, selection)
    except (OSError, sqlite3.Error, TrainingError):
        return "unavailable"


def _enrollment_snapshot(path: Path, organization: str, selection: tuple[str, str] | None) -> str:
    with closing(sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        owner = db.execute("SELECT organization FROM owner WHERE singleton=1").fetchone()
        if owner is None or owner[0] != organization:
            return "unavailable"
        tables = {
            row[0]
            for row in db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name IN ('engagement','dataset_scope','fit_checkpoints')"
            )
        }
        if tables:
            if "engagement" not in tables:
                return "unavailable"
            binding = db.execute(
                "SELECT id,withdrawn_at FROM engagement WHERE singleton=1"
            ).fetchone()
            if binding is None or not binding[0] or binding[1] is not None:
                return "unavailable"
        if selection is None:
            db.execute("SELECT id,revision,revoked,expires_at FROM datasets LIMIT 0")
            return "available"
        dataset_id, expected_revision = selection
        row = db.execute(
            "SELECT revision,revoked,expires_at FROM datasets WHERE id=?", (dataset_id,)
        ).fetchone()
        if row is None:
            return "absent"
        if row[1] or (row[2] is not None and row[2] <= time.time()):
            return "unavailable"
        return "registered" if row[0] == expected_revision else "stale"


def onboarding_store_available(path: Path | str, organization: str) -> bool:
    """Test private onboarding admission before reading the selected manifest."""
    return _registration_probe(path, organization, None) in {"absent", "available"}


def registration_state(
    path: Path | str, organization: str, dataset_id: str, expected_revision: str
) -> str:
    """Read enrollment status without initializing a database or retrieving training records."""
    return _registration_probe(path, organization, (dataset_id, expected_revision))


class LocalTrainingStore:
    """The operator fixes one private file/organization. Slack supplies neither path nor scope."""

    def __init__(
        self,
        path: Path | str,
        organization: str,
        *,
        _check_closeout: bool = True,
        _expected_engagement: str | None = None,
    ):
        self.path = Path(path)
        self.organization = identifier(organization)
        _check_directory(self.path)
        expected = (
            _custody_hold(self.path, self.organization) if _check_closeout else _expected_engagement
        )
        try:
            starting = self.path.lstat().st_size == 0
        except FileNotFoundError:
            starting = True
        # Existing initialized stores keep normal SQLite transaction order. Only a potential
        # creator takes the directory writer lock, and never the custody ledger within it.
        guard = _initialization_guard(self.path, creator=True) if starting else nullcontext()
        with guard:
            self._initialize(expected)

    def _initialize(self, expected: str | None) -> None:
        created = False
        if expected is not None:
            # A new store, an unbound backup or a foreign engagement cannot stand in for
            # the one covered by the ledger. Refuse before creating or migrating anything.
            try:
                _check_file(self.path)
            except FileNotFoundError:
                raise TrainingError("engagement_unavailable") from None
        else:
            try:
                fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                _check_file(self.path)
            else:
                os.close(fd)
                created = True
        # Create or migrate under one writer lock and verify the owner before altering legacy data.
        with self._db(write=True, check_owner=False) as db:
            existing = {
                row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
            }
            if not existing and not created:
                raise TrainingError("uninitialized_private_store")
            if expected is not None and "engagement" not in existing:
                raise TrainingError("engagement_unavailable")
            if existing:
                if "owner" not in existing:
                    raise TrainingError("scope_mismatch")
                self._check_owner(db)
                if "engagement" in existing:
                    scope = db.execute("SELECT id FROM engagement WHERE singleton=1").fetchone()
                    if scope is None or not scope[0]:
                        raise TrainingError("engagement_unavailable")
                    if expected is not None and expected != scope[0]:
                        raise TrainingError("engagement_mismatch")
                elif existing.intersection({"dataset_scope", "fit_checkpoints"}):
                    raise TrainingError("engagement_unavailable")
            for statement in _SCHEMA.split(";"):
                if statement.strip():
                    db.execute(statement)
            db.execute("INSERT OR IGNORE INTO owner VALUES (1,?)", (self.organization,))
            if "engagement" not in existing:
                db.execute("INSERT INTO engagement(singleton,id) VALUES(1,?)", (uuid.uuid4().hex,))

    @classmethod
    def for_custody(
        cls, path: Path | str, organization: str, *, expected_engagement: str | None = None
    ) -> LocalTrainingStore:
        """The closeout writer already holds its ledger lock and has checked its identity.

        This avoids reversing lock order during withdrawal. Ordinary actions on the
        resulting handle still check both the live training store and the closeout ledger.
        """
        return cls(
            path, organization, _check_closeout=False, _expected_engagement=expected_engagement
        )

    def _check_owner(self, db: sqlite3.Connection) -> None:
        owner = db.execute("SELECT organization FROM owner WHERE singleton=1").fetchone()
        if owner is None or owner[0] != self.organization:
            raise TrainingError("scope_mismatch")

    @contextmanager
    def _db(
        self, *, write: bool = False, check_owner: bool = True, allow_withdrawn: bool = False
    ) -> Iterator[sqlite3.Connection]:
        if (
            check_owner
            and not allow_withdrawn
            and _custody_hold(self.path, self.organization) is not None
        ):
            raise TrainingError("engagement_withdrawn")
        _check_file(self.path)
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
                if check_owner:
                    self._check_owner(db)
                    scope = db.execute(
                        "SELECT withdrawn_at FROM engagement WHERE singleton=1"
                    ).fetchone()
                    if scope is None or (scope[0] is not None and not allow_withdrawn):
                        raise TrainingError("engagement_withdrawn")
                yield db
        finally:
            db.close()

    @property
    def engagement_id(self) -> str:
        with self._db(allow_withdrawn=True) as db:
            return str(db.execute("SELECT id FROM engagement WHERE singleton=1").fetchone()[0])

    def engagement_active(self) -> bool:
        try:
            with self._db():
                return True
        except (OSError, sqlite3.Error, TrainingError):
            return False

    @contextmanager
    def engagement_guard(
        self,
        dataset_id: str | None = None,
        dataset_revision: str | None = None,
        *,
        engagement_id: str | None = None,
    ) -> Iterator[None]:
        """Serialize a live grant check and claim with engagement withdrawal."""
        with self._db(write=True) as db:
            if engagement_id is not None:
                actual = db.execute("SELECT id FROM engagement WHERE singleton=1").fetchone()[0]
                if actual != engagement_id:
                    raise TrainingError("engagement_mismatch")
            if (dataset_id is None) != (dataset_revision is None):
                raise TrainingError("stale_revision")
            if (
                dataset_id is not None
                and self._dataset(db, dataset_id)["revision"] != dataset_revision
            ):
                raise TrainingError("stale_revision")
            yield

    def withdraw(self) -> dict[str, Any]:
        with self._db(write=True, allow_withdrawn=True) as db:
            db.execute(
                "UPDATE engagement SET withdrawn_at=COALESCE(withdrawn_at,?) WHERE singleton=1",
                (int(time.time()),),
            )
            row = db.execute("SELECT id,withdrawn_at FROM engagement WHERE singleton=1").fetchone()
            return {"engagement_id": row[0], "withdrawn_at": row[1], "status": "withdrawn"}

    def custody_inventory(self) -> dict[str, int]:
        with self._db(allow_withdrawn=True) as db:
            sizes = db.execute(
                "SELECT COALESCE(SUM(train_count),0), COALESCE(SUM(heldout_count),0) FROM datasets"
            ).fetchone()
            models = db.execute(
                "SELECT COUNT(*) FROM (SELECT id FROM artifacts UNION "
                "SELECT digest FROM fit_checkpoints)"
            ).fetchone()[0]
            outputs = db.execute(
                "SELECT (SELECT COUNT(*) FROM jobs WHERE result IS NOT NULL) + "
                "(SELECT COUNT(*) FROM dataset_scope WHERE baseline_result IS NOT NULL)"
            ).fetchone()[0]
            return {"inputs": sizes[0], "heldout": sizes[1], "models": models, "outputs": outputs}

    def purge_withdrawn(self) -> dict[str, int]:
        """Remove only this store's live records after a separate custodian instruction."""
        with self._db(write=True, allow_withdrawn=True) as db:
            if (
                db.execute("SELECT withdrawn_at FROM engagement WHERE singleton=1").fetchone()[0]
                is None
            ):
                raise TrainingError("withdrawal_required")
            db.execute("PRAGMA secure_delete=ON")
            for table in (
                "artifacts",
                "fit_checkpoints",
                "jobs",
                "train_data",
                "heldout_data",
                "evaluation_keys",
                "split_keys",
                "dataset_scope",
                "datasets",
            ):
                db.execute("DELETE FROM " + table)
        return self.custody_inventory()

    @staticmethod
    def _dataset(db: sqlite3.Connection, dataset_id: str) -> sqlite3.Row:
        row: sqlite3.Row | None = db.execute(
            _DATASET_QUERY
            + "WHERE d.id=? AND d.revoked=0 AND (d.expires_at IS NULL OR d.expires_at>?)",
            (identifier(dataset_id), time.time()),
        ).fetchone()
        if row is None:
            raise TrainingError("dataset_unavailable")
        return row

    @staticmethod
    def _summary(row: sqlite3.Row) -> dict[str, Any]:
        mode = row["mode"] or "training"
        actions = permitted_actions(mode, bool(json.loads(row["previews"])))
        if row["mode"] is None:
            actions = [
                action for action in actions if action not in {"score_frozen", "train_cpu_text"}
            ]
        return {
            "dataset_id": row["id"],
            "dataset_revision": row["revision"],
            "mode": mode,
            "purpose": PURPOSE if mode == "training" else "evaluation_only",
            "can_train": mode == "training" and bool(row["train_digest"]),
            "fields": json.loads(row["fields"]),
            "visible_fields": json.loads(row["fields"]),
            "train_count": row["train_count"],
            "heldout_count": row["heldout_count"],
            "cohort_digest": row["cohort_digest"],
            "error_ceiling_bps": row["ceiling"],
            "permitted_actions": actions,
            "destination": row["destination"] or "owner_local_store",
            "retention": json.loads(row["retention"])
            if row["retention"]
            else {"disposition": "unrecorded"},
        }

    def _bind(self, db: sqlite3.Connection, data: Dataset) -> None:
        cohort = _cohort_digest(
            self.organization, data.name, data.revision, data.ceiling_bps, data.heldout
        )
        initial = None
        if data.mode == "evaluation_only":
            report = _score(data.heldout, data.ceiling_bps, None)
            initial = canonical(
                _evidence(data.name, data.revision, data.mode, cohort, None, report)
            )
        db.execute(
            "INSERT INTO dataset_scope VALUES(?,?,?,?,?,?,?)",
            (
                data.name,
                data.mode,
                data.destination,
                canonical(data.retention),
                digest(data.train) if data.mode == "training" else None,
                cohort,
                initial,
            ),
        )

    def dataset_summary(self, dataset_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._summary(self._dataset(db, dataset_id))

    def register(
        self, manifest: dict[str, Any], *, check_authorization: Callable[[], None] | None = None
    ) -> dict[str, Any]:
        """Operator enrollment only. Database, training split and incumbent become immutable."""
        data = prepare(manifest, self.organization)
        if data.expires_at is not None and data.expires_at <= time.time():
            raise TrainingError("grant_expired")
        with self._db(write=True) as db:
            if check_authorization is not None:
                check_authorization()
            prior = db.execute("SELECT revision FROM datasets WHERE id=?", (data.name,)).fetchone()
            if prior:
                if prior[0] != data.revision:
                    raise TrainingError("dataset_revision_conflict")
                prior_row = self._dataset(db, data.name)
                if prior_row["mode"] is None:
                    # Legacy rows lacked independent cohort digests. Only an exact original
                    # enrollment can establish the missing binding, never a cached report alone.
                    old_train = db.execute(
                        "SELECT content FROM train_data WHERE dataset=?", (data.name,)
                    ).fetchone()
                    old_test = db.execute(
                        "SELECT content FROM heldout_data WHERE dataset=?", (data.name,)
                    ).fetchone()
                    if (
                        old_train is None
                        or old_test is None
                        or old_train[0] != canonical(data.train)
                        or old_test[0] != canonical(data.heldout)
                    ):
                        raise TrainingError("legacy_binding_unverified")
                    self._bind(db, data)
                if check_authorization is not None:
                    check_authorization()
                return self._summary(self._dataset(db, data.name))
            for key, split in data.keys.items():
                db.execute(
                    "INSERT OR IGNORE INTO split_keys(fingerprint,split) VALUES(?,?)", (key, split)
                )
                old_split = db.execute(
                    "SELECT split FROM split_keys WHERE fingerprint=?", (key,)
                ).fetchone()[0]
                if old_split != split:
                    raise TrainingError("split_overlap")
                if split == "heldout":
                    db.execute("INSERT INTO evaluation_keys VALUES(?,?)", (data.name, key))
            db.execute(
                "INSERT INTO"
                " datasets(id,revision,reviewers,fields,preview_fields,previews,train_count,"
                "heldout_count,ceiling,expires_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    data.name,
                    data.revision,
                    canonical(data.reviewers),
                    canonical(data.fields),
                    canonical(data.preview_fields),
                    canonical(data.previews),
                    len(data.train),
                    len(data.heldout),
                    data.ceiling_bps,
                    data.expires_at,
                ),
            )
            self._bind(db, data)
            if data.mode == "training":
                db.execute("INSERT INTO train_data VALUES(?,?)", (data.name, canonical(data.train)))
            db.execute("INSERT INTO heldout_data VALUES(?,?)", (data.name, canonical(data.heldout)))
            if check_authorization is not None:
                check_authorization()
            return self._summary(self._dataset(db, data.name))

    def revoke(self, dataset_id: str) -> None:
        with self._db(write=True) as db:
            self._dataset(db, dataset_id)
            db.execute("UPDATE datasets SET revoked=1 WHERE id=?", (dataset_id,))

    def catalog(
        self, query: str = "", *, allowed_ids: Collection[str] | None = None
    ) -> list[dict[str, Any]]:
        if not isinstance(query, str) or len(query) > 64 or (query and not query.isprintable()):
            raise TrainingError("invalid_query")
        if isinstance(allowed_ids, str):
            raise TrainingError("invalid_grant")
        allowed = None if allowed_ids is None else {identifier(value) for value in allowed_ids}
        term = query.strip().casefold()
        with self._db() as db:
            rows = db.execute(
                _DATASET_QUERY
                + "WHERE d.revoked=0 AND (d.expires_at IS NULL OR d.expires_at>?) ORDER BY d.id",
                (time.time(),),
            )
            matches = []
            for row in rows:
                if allowed is not None and row["id"] not in allowed:
                    continue
                summary = self._summary(row)
                if any(term in s.casefold() for s in [summary["dataset_id"], *summary["fields"]]):
                    matches.append(summary)
                if len(matches) == 25:
                    break
            return matches

    def preview(self, dataset_id: str) -> dict[str, Any]:
        with self._db() as db:
            row = self._dataset(db, dataset_id)
            examples = json.loads(row["previews"])
            return {
                **self._summary(row),
                "fields": json.loads(row["preview_fields"]),
                "rows": examples,
                "scope": "metadata_only" if row["mode"] == "evaluation_only" else "training_only",
                "example_limit": len(examples),
            }

    def _job(self, db: sqlite3.Connection, job_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
        job = db.execute("SELECT * FROM jobs WHERE id=?", (identifier(job_id),)).fetchone()
        if job is None:
            raise TrainingError("job_unavailable")
        return job, self._dataset(db, job["dataset"])

    @staticmethod
    def _envelope(job: sqlite3.Row, data: sqlite3.Row) -> dict[str, Any]:
        return {
            "job_id": job["id"],
            "dataset_id": data["id"],
            "dataset_revision": data["revision"],
            "revision": job["revision"],
            "profile": PROFILE,
            "epochs": job["epochs"],
            "status": job["status"],
            "train_count": data["train_count"],
            "heldout_count": data["heldout_count"],
            "cpu_limit_seconds": CPU_LIMIT_SECONDS,
            "max_train_updates": job["epochs"] * data["train_count"],
            "error": job["error"],
            "recoverable": job["status"] == "running" and job["lease_until"] <= time.time(),
            "result": json.loads(job["result"]) if job["result"] else None,
        }

    @staticmethod
    def _heldout_used(db: sqlite3.Connection, dataset_id: str, job_id: str) -> bool:
        return (
            db.execute(
                "SELECT 1 FROM evaluation_keys e JOIN split_keys s USING(fingerprint) "
                "WHERE e.dataset=? AND s.used_by IS NOT NULL AND s.used_by!=? LIMIT 1",
                (dataset_id, job_id),
            ).fetchone()
            is not None
        )

    def propose(
        self,
        dataset_id: str,
        *,
        epochs: int = 3,
        profile: str = PROFILE,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        if profile != PROFILE:
            raise TrainingError("qualified_trainer_required")
        if type(epochs) is not int or epochs not in EPOCH_CHOICES:
            raise TrainingError("input_outside_profile")
        if request_id is not None and (
            not isinstance(request_id, str)
            or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id) is None
        ):
            raise TrainingError("invalid_request_id")
        with self._db(write=True) as db:
            data = self._dataset(db, dataset_id)
            if data["mode"] != "training" or not data["train_digest"]:
                raise TrainingError("training_not_authorized")
            revision = digest(
                [
                    "opentour.pilot.training-proposal.v1",
                    self.organization,
                    dataset_id,
                    data["revision"],
                    profile,
                    epochs,
                    CPU_LIMIT_SECONDS,
                ]
            )
            old = db.execute("SELECT * FROM jobs WHERE request=?", (request_id,)).fetchone()
            if old:
                if old["revision"] != revision:
                    raise TrainingError("request_conflict")
                return self._envelope(old, data)
            if self._heldout_used(db, dataset_id, ""):
                raise TrainingError("fresh_heldout_required")
            job_id = f"job-{uuid.uuid4().hex}"
            db.execute(
                "INSERT INTO jobs(id,request,dataset,revision,epochs,status) VALUES(?,?,?,?,?,?)",
                (job_id, request_id, dataset_id, revision, epochs, "awaiting_approval"),
            )
            return self._envelope(*self._job(db, job_id))

    def status(self, job_id: str) -> dict[str, Any]:
        with self._db() as db:
            return self._envelope(*self._job(db, job_id))

    @staticmethod
    def _model(db: sqlite3.Connection, job: sqlite3.Row) -> dict[str, Any]:
        row = db.execute("SELECT model FROM artifacts WHERE id=?", (job["artifact"],)).fetchone()
        if job["status"] != "completed" or row is None or not job["result"]:
            raise TrainingError("artifact_unavailable")
        try:
            model: dict[str, Any] = json.loads(row[0])
            accepted = json.loads(job["result"])
            if digest(model) != job["artifact"] or accepted["artifact_digest"] != job["artifact"]:
                raise TrainingError("artifact_digest_mismatch")
        except (ValueError, TypeError, KeyError) as error:
            if isinstance(error, TrainingError):
                raise
            raise TrainingError("artifact_digest_mismatch") from None
        return model

    def artifact(self, job_id: str) -> dict[str, Any]:
        """Local owner export of verified weights. Slack receives approved aggregates only."""
        with self._db() as db:
            job, _ = self._job(db, job_id)
            return self._model(db, job)

    def _cohort(self, db: sqlite3.Connection, data: sqlite3.Row) -> list[Any]:
        row = db.execute(
            "SELECT content FROM heldout_data WHERE dataset=?", (data["id"],)
        ).fetchone()
        if row is None or data["cohort_digest"] is None:
            raise TrainingError("cohort_binding_required")
        try:
            rows: list[Any] = json.loads(row[0])
            actual = _cohort_digest(
                self.organization, data["id"], data["revision"], data["ceiling"], rows
            )
        except (ValueError, TypeError, RecursionError):
            raise TrainingError("cohort_digest_mismatch") from None
        if actual != data["cohort_digest"]:
            raise TrainingError("cohort_digest_mismatch")
        return rows

    def frozen_evaluation(self, dataset_id: str) -> dict[str, Any]:
        with self._db() as db:
            data = self._dataset(db, dataset_id)
            if data["mode"] != "evaluation_only" or not data["baseline_result"]:
                raise TrainingError("frozen_evaluation_unavailable")
            return self._recompute(db, data, None)

    def frozen_training(self, job_id: str) -> dict[str, Any]:
        """Recompute the existing readout from this specific completed job's fitted parameters."""
        with self._db() as db:
            job, data = self._job(db, job_id)
            return self._recompute(db, data, job)

    def _recompute(
        self, db: sqlite3.Connection, data: sqlite3.Row, job: sqlite3.Row | None
    ) -> dict[str, Any]:
        if job is None:
            if data["mode"] != "evaluation_only" or not data["baseline_result"]:
                raise TrainingError("supplied_decisions_required")
            model = None
            artifact = None
            recorded = data["baseline_result"]
        else:
            if data["mode"] != "training":
                raise TrainingError("training_not_authorized")
            model = self._model(db, job)
            artifact = job["artifact"]
            recorded = job["result"]
        rows = self._cohort(db, data)
        try:
            report = _score(rows, data["ceiling"], model)
            expected: dict[str, Any] = json.loads(recorded)
            repeat = _evidence(
                data["id"],
                data["revision"],
                data["mode"],
                data["cohort_digest"],
                artifact,
                report,
            )
            if expected.get("readout") != report:
                raise TrainingError("readout_repeat_mismatch")
            # Pre-binding software jobs require independently re-enrolled original inputs.
            # Their legacy result can validate aggregates, but it never attested a brief.
            for key in (
                "dataset_id",
                "dataset_revision",
                "mode",
                "cohort_digest",
                "aggregate_digest",
                "brief_digest",
                "artifact_digest",
            ):
                if key not in expected or expected[key] != repeat[key]:
                    raise TrainingError(
                        "readout_binding_required"
                        if key not in expected
                        else "readout_repeat_mismatch"
                    )
            return repeat
        except TrainingError:
            raise
        except (TypeError, KeyError, ValueError, IndexError, RecursionError):
            raise TrainingError("readout_repeat_mismatch") from None

    def score_only(
        self, *, output_dir: Path, dataset_id: str | None = None, job_id: str | None = None
    ) -> dict[str, Any]:
        if (dataset_id is None) == (job_id is None):
            raise TrainingError("choose_one_frozen_source")
        with self._db() as db:
            if job_id is not None:
                job, data = self._job(db, job_id)
            else:
                assert dataset_id is not None
                job = None
                data = self._dataset(db, dataset_id)
            result = self._recompute(db, data, job)
        target = Path(output_dir)
        _check_directory(target)
        aggregate, brief = _report_bytes(result["readout"])
        receipt = {k: v for k, v in result.items() if k != "readout"}
        with tempfile.TemporaryDirectory(dir=target.parent) as temp:
            staged = Path(temp) / "repeat"
            staged.mkdir(mode=0o700)
            for name, content in (
                ("aggregate.json", aggregate),
                ("brief.md", brief),
                ("repeat.json", (canonical(receipt) + "\n").encode()),
            ):
                (staged / name).write_bytes(content)
            with self.engagement_guard(data["id"], data["revision"]):
                try:
                    target.mkdir(mode=0o700)
                except FileExistsError:
                    raise TrainingError("fresh_output_directory_required") from None
                reservation = target.stat(follow_symlinks=False)
                try:
                    staged.replace(target)
                except OSError:
                    if os.path.samestat(reservation, target.stat(follow_symlinks=False)):
                        target.rmdir()
                    raise
        return receipt

    def recover(self, job_id: str, *, reviewer: str, expected_revision: str) -> dict[str, Any]:
        return self._execute(job_id, reviewer, expected_revision, recover_only=True)

    def run(self, job_id: str, *, reviewer: str, expected_revision: str) -> dict[str, Any]:
        return self._execute(job_id, reviewer, expected_revision, recover_only=False)

    @staticmethod
    def _restore_fit(
        db: sqlite3.Connection, job: sqlite3.Row, data: sqlite3.Row
    ) -> tuple[dict[str, Any], int, float]:
        row = db.execute("SELECT * FROM fit_checkpoints WHERE job=?", (job["id"],)).fetchone()
        if row is None:
            raise TrainingError("interrupted_fit_unverified")
        try:
            model: dict[str, Any] = json.loads(row["model"])
            updates, fit_cpu = row["updates"], row["fit_cpu"]
            identity = [job["id"], job["revision"], row["digest"], updates, fit_cpu]
            if (
                not isinstance(model, dict)
                or model.get("profile") != PROFILE
                or digest(model) != row["digest"]
                or digest(identity) != row["binding"]
                or type(updates) is not int
                or not 0 <= updates <= data["train_count"] * job["epochs"]
                or not 0 <= fit_cpu <= CPU_LIMIT_SECONDS
            ):
                raise ValueError
            return model, updates, fit_cpu
        except (ValueError, TypeError, RecursionError):
            raise TrainingError("checkpoint_unverified") from None

    def _execute(
        self, job_id: str, reviewer: str, expected_revision: str, *, recover_only: bool
    ) -> dict[str, Any]:
        """Consume one previously confirmed, actor-bound proposal. Retries reuse its result."""
        training: list[tuple[str, str]] = []
        checkpoint = None
        with self._db(write=True) as db:
            job, data = self._job(db, job_id)
            if data["mode"] != "training" or not data["train_digest"]:
                raise TrainingError("training_not_authorized")
            if recover_only and (
                job["status"] != "running"
                or job["lease_until"] > time.time()
                or not job["reviewer"]
            ):
                raise TrainingError("approved_interrupted_job_required")
            if reviewer not in json.loads(data["reviewers"]):
                raise TrainingError("reviewer_denied")
            if expected_revision != job["revision"]:
                raise TrainingError("stale_revision")
            if job["reviewer"] is not None and job["reviewer"] != reviewer:
                raise TrainingError("reviewer_denied")
            if job["status"] in ("completed", "failed"):
                return self._envelope(job, data)
            if job["status"] == "running" and not recover_only:
                return self._envelope(job, data)
            if self._heldout_used(db, data["id"], job_id):
                db.execute(
                    "UPDATE jobs SET status='failed',error='fresh_heldout_required' WHERE id=?",
                    (job_id,),
                )
                return self._envelope(*self._job(db, job_id))
            if recover_only:
                try:
                    checkpoint = self._restore_fit(db, job, data)
                except TrainingError as exc:
                    db.execute(
                        "UPDATE jobs SET status='failed',error=?,lease_token=NULL,lease_until=NULL "
                        "WHERE id=?",
                        (str(exc), job_id),
                    )
                    return self._envelope(*self._job(db, job_id))
            else:
                training = json.loads(
                    db.execute(
                        "SELECT content FROM train_data WHERE dataset=?", (data["id"],)
                    ).fetchone()[0]
                )
                if digest(training) != data["train_digest"]:
                    raise TrainingError("training_digest_mismatch")
            lease = uuid.uuid4().hex
            db.execute(
                "UPDATE split_keys SET used_by=? WHERE fingerprint IN (SELECT fingerprint "
                "FROM evaluation_keys WHERE dataset=?)",
                (job_id, data["id"]),
            )
            db.execute(
                "UPDATE jobs SET status='running',reviewer=?,lease_token=?,lease_until=?"
                " WHERE id=?",
                (reviewer, lease, time.time() + LEASE_SECONDS, job_id),
            )
        start = time.thread_time()
        try:
            if checkpoint is None:
                model, updates = fit(training, job["epochs"], start)
            else:
                model, updates, fit_cpu = checkpoint
                start -= fit_cpu
            model_bytes = canonical(model)
            artifact_id = digest(model)
            if checkpoint is None:
                # Persist the fitted parameters before any held-out content is fetched. An
                # interruption before this commit has an unknown fit outcome and cannot refit.
                with self._db(write=True) as db:
                    current, _ = self._job(db, job_id)
                    if current["lease_token"] != lease:
                        raise TrainingError("run_superseded")
                    fit_cpu = time.thread_time() - start
                    if fit_cpu > CPU_LIMIT_SECONDS:
                        raise TrainingError("compute_limit_reached")
                    binding = digest([job_id, job["revision"], artifact_id, updates, fit_cpu])
                    db.execute(
                        "INSERT INTO fit_checkpoints VALUES(?,?,?,?,?,?)",
                        (job_id, binding, artifact_id, model_bytes, updates, fit_cpu),
                    )
            with self._db() as db:
                current_data = self._dataset(db, data["id"])
                heldout = self._cohort(db, current_data)
            readout = _score(heldout, data["ceiling"], model, start)
            check_budget(start)
            result = {
                **_evidence(
                    data["id"],
                    data["revision"],
                    data["mode"],
                    data["cohort_digest"],
                    artifact_id,
                    readout,
                ),
                "changed_weights": sum(
                    bool(weight) for weights in model["weights"] for weight in weights
                ),
                "update_count": updates,
                "train_count": data["train_count"],
                "heldout_count": len(heldout),
            }
            with self._db(write=True) as db:
                current, _ = self._job(db, job_id)
                if current["lease_token"] != lease:
                    raise TrainingError("run_superseded")
                db.execute(
                    "INSERT OR IGNORE INTO artifacts VALUES(?,?)", (artifact_id, model_bytes)
                )
                db.execute(
                    "UPDATE jobs SET status='completed',result=?,artifact=?,"
                    "lease_token=NULL,lease_until=NULL WHERE id=?",
                    (canonical(result), artifact_id, job_id),
                )
                db.execute("DELETE FROM fit_checkpoints WHERE job=?", (job_id,))
        except TrainingError as exc:
            with self._db(write=True, allow_withdrawn=True) as db:
                db.execute(
                    "UPDATE jobs SET status='failed',error=?,lease_token=NULL,lease_until=NULL"
                    " WHERE id=? AND lease_token=?",
                    (str(exc), job_id, lease),
                )
        return self.status(job_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db", required=True, type=Path, help="SQLite file in an owner-private directory"
    )
    parser.add_argument("--org", required=True, help="operator-selected organization ID")
    actions = parser.add_subparsers(dest="command", required=True)
    actions.add_parser("register", help="enroll an owner-approved JSON file").add_argument(
        "manifest", type=Path
    )
    actions.add_parser("seed-generated", help="enroll synthetic practice data only").add_argument(
        "--reviewer", required=True
    )
    actions.add_parser(
        "generated-fixture", help="print the complete synthetic registration format"
    ).add_argument("--reviewer", required=True)
    actions.add_parser(
        "generated-evaluation-fixture", help="print generated supplied-output enrollment"
    )
    score = actions.add_parser(
        "score-only", help="independently rescore frozen inputs in a fresh directory"
    )
    selection = score.add_mutually_exclusive_group(required=True)
    selection.add_argument("--dataset", dest="dataset_id")
    selection.add_argument("--job", dest="job_id")
    score.add_argument("--output-dir", type=Path, required=True)
    recovery = actions.add_parser("recover", help="resume an already-confirmed interrupted job")
    recovery.add_argument("job_id")
    recovery.add_argument("--reviewer", required=True)
    recovery.add_argument("--expected-revision", required=True)
    actions.add_parser("catalog").add_argument("query", nargs="?", default="")
    actions.add_parser("preview").add_argument("dataset_id")
    actions.add_parser("status").add_argument("job_id")
    actions.add_parser(
        "artifact", help="print an approved, completed local fitted model"
    ).add_argument("job_id")
    actions.add_parser(
        "revoke", help="immediately revoke access to a registered dataset"
    ).add_argument("dataset_id")
    actions.add_parser(
        "rehearse", help="fit and evaluate only the built-in generated fixture for generated-demo"
    )
    args = parser.parse_args()
    try:
        value: Any
        if args.command == "generated-fixture":
            value = generated_dataset(identifier(args.org), identifier(args.reviewer))
        elif args.command == "generated-evaluation-fixture":
            value = generated_evaluation_dataset(identifier(args.org))
        else:
            store = LocalTrainingStore(args.db, args.org)
            if args.command == "register":
                with args.manifest.open("rb") as source:
                    value = store.register(parse_manifest(source.read(MAX_MANIFEST_BYTES + 1)))
            elif args.command == "seed-generated":
                value = store.register(generated_dataset(args.org, args.reviewer))
            elif args.command == "rehearse":
                if args.org != "generated-demo":
                    raise TrainingError("generated_rehearsal_only")
                row = store.register(generated_dataset(args.org, "generated-reviewer"))
                job = store.propose(row["dataset_id"], request_id="generated-rehearsal-v1")
                value = store.run(
                    job["job_id"], reviewer="generated-reviewer", expected_revision=job["revision"]
                )
            elif args.command == "score-only":
                value = store.score_only(
                    dataset_id=args.dataset_id, job_id=args.job_id, output_dir=args.output_dir
                )
            elif args.command == "recover":
                value = store.recover(
                    args.job_id, reviewer=args.reviewer, expected_revision=args.expected_revision
                )
            elif args.command == "catalog":
                value = store.catalog(args.query)
            elif args.command == "preview":
                value = store.preview(args.dataset_id)
            elif args.command == "status":
                value = store.status(args.job_id)
            elif args.command == "artifact":
                value = store.artifact(args.job_id)
            else:
                store.revoke(args.dataset_id)
                value = {"status": "revoked"}
        print(canonical(value))
    except (TrainingError, ManifestError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (OSError, sqlite3.Error):
        print("private_store_unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
