"""Worker-held OPE-526 audit store: chained event log, benchmark state and store identity.

The release check opens this store itself; a caller never hands it a ledger, an event list or a
set of "current" bindings. One directory holds one SQLite file (``audit-ledger.sqlite3``) and one
head file (``audit-ledger.head.json``).

What the store establishes, and how:

* **Identity.** The store id is the digest of the realpath, device and inode of the directory and
  of the SQLite file. It is pinned in the store at creation and recorded in every issuance event
  (the receipt body does not carry it). A naively copied directory, a store swapped in under the
  same path while the original still exists, a symlinked directory or a hardlinked SQLite file in
  another directory no longer matches. The id is key-free and the store vouches for itself.
* **Forward-only event log.** Every event is linked into a SHA-256 hash chain that starts from a
  genesis bound to the store id. The worker writes the chain head and event count to the head
  file on every append. Reading the store recomputes the chain and compares it with the head, so
  a dropped, reordered, edited or rolled-back event is detected. Appends refuse to write unless
  the log on disk still matches the stored head. SQLite triggers refuse UPDATE and DELETE on the
  log (defence in depth only: anyone with write access to the file can drop them).
* **State.** Receipt status, successors, fences and reconciliation are replayed from the verified
  log with ``AuditLedger.replay``; nothing derived is stored.
* **Benchmark state.** ``benchmark_set`` events hold the benchmark's current family records and,
  per opaque binding, either the material the digest is computed from or a supplied digest.

What it does NOT establish. The log and the head are unsigned and live in the same directory.
Someone with write access to both can rewrite them consistently in place (same inodes), or build a
fresh, self-consistent store with this class and issue any receipt into it, including one that was
superseded elsewhere. Closing that needs all four of: an expected store id from worker
configuration, a signed head (OPE-566, OPE-582), a monotonic anchor outside the store, and the
issuing store id inside the signed receipt. Device and inode numbers can also be reused: if the
live store is deleted and an older copy (log and head together) is restored in its place, the
filesystem may hand the copy the same inodes, and that rollback is then not detected either.
Binding material is whatever was ingested into the store: recomputing a digest from it shows the
receipt matches the worker's copy, not that the copy matches upstream.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import tempfile
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Literal

from opentour_training.evaluation.benchmark_audit.audit import (
    label_manifest_digest,
    split_manifest_digest,
)
from opentour_training.evaluation.benchmark_audit.ledger import (
    AuditLedger,
    DependentEffect,
    Event,
    LedgerError,
)
from opentour_training.evaluation.benchmark_audit.model import (
    RECEIPT_SCHEMA,
    BindingDigests,
    FamilyRecord,
    canonical_digest,
)

STORE_SCHEMA = "opentour.benchmark-audit-worker-store.v1"
LEDGER_FILE = "audit-ledger.sqlite3"
HEAD_FILE = "audit-ledger.head.json"
RECORD_BINDINGS = ("split_manifest", "label_manifest")
MATERIAL_BINDINGS = ("canonical_inputs", "source_revisions", "scoring_policy", "answer_key")
BASIS_RECORDS = "recomputed_from_worker_store_records"
BASIS_MATERIAL = "recomputed_from_worker_store_material"
BASIS_ASSERTED = "caller_asserted_digest_not_recomputed"
BASIS_ABSENT = "absent_from_worker_store"
RECOMPUTED_BASES = frozenset({BASIS_RECORDS, BASIS_MATERIAL})
EvidenceKind = Literal["generated_sentinel", "observed"]
EVIDENCE_KINDS = frozenset({"generated_sentinel", "observed"})
STORE_FAILURES = frozenset(
    {
        "audit_store_unavailable",
        "audit_store_identity_mismatch",
        "audit_ledger_chain_broken",
        "audit_ledger_head_mismatch",
    }
)
_AUDIT_KINDS = frozenset({"issued", "effect_registered", "disclosure", "superseded"})
_SELF_FIELDS = ("receipt_digest", "receipt_id")
_SCHEMA_SQL = """
CREATE TABLE meta (
    slot INTEGER PRIMARY KEY CHECK (slot = 1), schema TEXT NOT NULL, store_id TEXT NOT NULL
);
CREATE TABLE events (
    seq INTEGER PRIMARY KEY, event TEXT NOT NULL, prev TEXT NOT NULL, head TEXT NOT NULL
);
CREATE TRIGGER events_append_only_update BEFORE UPDATE ON events
    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER events_append_only_delete BEFORE DELETE ON events
    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER meta_fixed_update BEFORE UPDATE ON meta
    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER meta_fixed_delete BEFORE DELETE ON meta
    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
"""


class StoreError(ValueError):
    """A closed-vocabulary store failure; ``code`` is one of ``STORE_FAILURES``."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def receipt_body_digest(receipt: Mapping[str, Any]) -> str:
    """Recompute the digest ``audit()`` seals: the document without its self-referencing ids."""
    return canonical_digest({k: v for k, v in receipt.items() if k not in _SELF_FIELDS})


def receipt_self_consistent(receipt: Mapping[str, Any], receipt_id: str) -> bool:
    """Schema, digest and id recompute from the body. Not an issuer check: nothing is signed."""
    try:
        recomputed = receipt_body_digest(receipt)
    except (TypeError, ValueError):
        return False
    supplied = receipt.get("receipt_digest")
    return (
        receipt.get("schema_version") == RECEIPT_SCHEMA
        and isinstance(supplied, str)
        and supplied == recomputed
        and receipt.get("receipt_id") == f"audit-{recomputed[:16]}" == receipt_id
    )


def _canonical_text(event: Event) -> str:
    return json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )


def _genesis(store_id: str) -> str:
    return canonical_digest([STORE_SCHEMA, "genesis", store_id])


def _link(prev: str, seq: int, event: Event) -> str:
    return canonical_digest([STORE_SCHEMA, prev, seq, event])


def store_identity(directory: Path | str) -> str:
    """Realpath, device and inode of the directory and the SQLite file, as one digest."""
    root = Path(directory)
    parts: list[object] = [STORE_SCHEMA]
    try:
        for item, is_kind in ((root, stat.S_ISDIR), (root / LEDGER_FILE, stat.S_ISREG)):
            info = os.lstat(item)
            if not is_kind(info.st_mode):
                raise StoreError("audit_store_unavailable")
            parts += [os.path.realpath(item), info.st_dev, info.st_ino]
    except OSError as exc:
        raise StoreError("audit_store_unavailable") from exc
    return canonical_digest(parts)


def _read_head(root: Path) -> dict[str, Any]:
    path = root / HEAD_FILE
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise StoreError("audit_ledger_head_mismatch")
        head = json.loads(path.read_text(encoding="ascii"))
    except (OSError, ValueError, UnicodeDecodeError) as exc:
        raise StoreError("audit_ledger_head_mismatch") from exc
    if (
        not isinstance(head, dict)
        or head.get("schema") != STORE_SCHEMA
        or type(head.get("seq")) is not int
        or not isinstance(head.get("head"), str)
        or not isinstance(head.get("store_id"), str)
    ):
        raise StoreError("audit_ledger_head_mismatch")
    return head


def _write_head(root: Path, store_id: str, seq: int, head: str) -> None:
    text = _canonical_text({"schema": STORE_SCHEMA, "store_id": store_id, "seq": seq, "head": head})
    fd, tmp = tempfile.mkstemp(dir=root, prefix=".head-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(text + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, root / HEAD_FILE)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _verified_events(root: Path, db: sqlite3.Connection) -> tuple[str, list[Event], str]:
    """Return (store_id, events, head) after the identity, chain and head checks pass."""
    identity = store_identity(root)
    try:
        meta = db.execute("SELECT schema, store_id FROM meta WHERE slot = 1").fetchone()
        rows = db.execute("SELECT seq, event, prev, head FROM events ORDER BY seq").fetchall()
    except sqlite3.DatabaseError as exc:
        raise StoreError("audit_store_unavailable") from exc
    if meta is None or meta[0] != STORE_SCHEMA:
        raise StoreError("audit_store_unavailable")
    head_doc = _read_head(root)
    if meta[1] != identity or head_doc["store_id"] != identity:
        raise StoreError("audit_store_identity_mismatch")
    prev = _genesis(identity)
    events: list[Event] = []
    for expected, (seq, text, row_prev, row_head) in enumerate(rows, start=1):
        try:
            event = json.loads(text)
        except (TypeError, ValueError) as exc:
            raise StoreError("audit_ledger_chain_broken") from exc
        if (
            seq != expected
            or row_prev != prev
            or not isinstance(event, dict)
            or _canonical_text(event) != text
            or row_head != _link(prev, seq, event)
        ):
            raise StoreError("audit_ledger_chain_broken")
        prev = row_head
        events.append(event)
    if head_doc["seq"] != len(rows) or head_doc["head"] != prev:
        raise StoreError("audit_ledger_head_mismatch")
    return identity, events, prev


def _record_from_json(value: object) -> FamilyRecord:
    if not isinstance(value, dict) or set(value) != {f.name for f in fields(FamilyRecord)}:
        raise ValueError("record")
    data = dict(value)
    data["post_decision_fields_in_input"] = tuple(data["post_decision_fields_in_input"])
    return FamilyRecord(**data)


@dataclass(frozen=True)
class BenchmarkState:
    """The worker's current copy of one benchmark, from its latest ``benchmark_set`` event."""

    benchmark_id: str
    records: tuple[Mapping[str, Any], ...]
    materials: Mapping[str, str]
    asserted_digests: Mapping[str, str]
    evidence_kind: str

    def bindings(self) -> tuple[dict[str, str | None], dict[str, str]]:
        """Each binding digest with its basis. A caller-asserted digest is never "recomputed"."""
        values: dict[str, str | None] = {}
        basis: dict[str, str] = {}
        try:
            records = tuple(_record_from_json(r) for r in self.records)
        except (TypeError, ValueError, KeyError):
            records = None
        for name in RECORD_BINDINGS:
            if records is None:
                values[name], basis[name] = None, BASIS_ABSENT
                continue
            digest = split_manifest_digest if name == "split_manifest" else label_manifest_digest
            values[name], basis[name] = digest(records), BASIS_RECORDS
        for name in MATERIAL_BINDINGS:
            if name in self.materials:
                values[name] = canonical_digest(self.materials[name])
                basis[name] = BASIS_MATERIAL
            elif name in self.asserted_digests:
                values[name], basis[name] = self.asserted_digests[name], BASIS_ASSERTED
            else:
                values[name], basis[name] = None, BASIS_ABSENT
        order = [f.name for f in fields(BindingDigests)]
        return {k: values[k] for k in order}, {k: basis[k] for k in order}


@dataclass(frozen=True)
class WorkerState:
    """Verified view of the store at one read."""

    store_id: str
    seq: int
    head: str
    ledger: AuditLedger
    issuance: Mapping[str, Mapping[str, str]]
    benchmarks: Mapping[str, BenchmarkState]


def _state(store_id: str, events: Sequence[Event], head: str) -> WorkerState:
    ledger = AuditLedger.replay(e for e in events if e.get("kind") in _AUDIT_KINDS)
    issuance: dict[str, dict[str, str]] = {}
    benchmarks: dict[str, BenchmarkState] = {}
    for event in events:
        kind = event.get("kind")
        if kind == "issued":
            receipt = event.get("receipt") or {}
            issuance.setdefault(
                str(receipt.get("receipt_id")),
                {
                    "benchmark_id": str(event.get("benchmark_id")),
                    "store_id": str(event.get("store_id")),
                },
            )
        elif kind == "benchmark_set":
            bid = str(event["benchmark_id"])
            benchmarks[bid] = BenchmarkState(
                bid,
                tuple(event["records"]),
                dict(event["materials"]),
                dict(event["asserted_digests"]),
                str(event["evidence_kind"]),
            )
        elif kind not in _AUDIT_KINDS:
            raise StoreError("audit_ledger_chain_broken")
    return WorkerState(store_id, len(events), head, ledger, issuance, benchmarks)


def _connect(root: Path, *, read_only: bool) -> sqlite3.Connection:
    path = root / LEDGER_FILE
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            raise StoreError("audit_store_unavailable")
        if read_only:
            uri = path.resolve().as_uri() + "?mode=ro"
            return sqlite3.connect(uri, uri=True, isolation_level=None, timeout=30)
        return sqlite3.connect(path, isolation_level=None, timeout=30)
    except (OSError, sqlite3.Error) as exc:
        raise StoreError("audit_store_unavailable") from exc


def read_worker_state(directory: Path | str) -> WorkerState:
    """Open the store read-only and return its verified state, or raise ``StoreError``."""
    root = Path(directory)
    db = _connect(root, read_only=True)
    try:
        try:
            store_id, events, head = _verified_events(root, db)
            return _state(store_id, events, head)
        except (KeyError, TypeError, ValueError, LedgerError) as exc:
            if isinstance(exc, StoreError):
                raise
            raise StoreError("audit_ledger_chain_broken") from exc
    finally:
        db.close()


class WorkerAuditStore:
    """Writer for one worker store directory. Every write re-verifies the log first."""

    def __init__(self, directory: Path | str) -> None:
        self.directory = Path(directory)
        read_worker_state(self.directory)

    @classmethod
    def create(cls, directory: Path | str) -> WorkerAuditStore:
        """Create a new store in a directory that must not exist yet."""
        root = Path(directory)
        root.mkdir(mode=0o700)
        db = sqlite3.connect(root / LEDGER_FILE, isolation_level=None)
        try:
            db.executescript(_SCHEMA_SQL)
            store_id = store_identity(root)
            db.execute("INSERT INTO meta (slot, schema, store_id) VALUES (1, ?, ?)",
                       (STORE_SCHEMA, store_id))  # fmt: skip
        finally:
            db.close()
        _write_head(root, store_id, 0, _genesis(store_id))
        return cls(root)

    @property
    def store_id(self) -> str:
        return read_worker_state(self.directory).store_id

    def state(self) -> WorkerState:
        return read_worker_state(self.directory)

    def events(self) -> tuple[Event, ...]:
        """The verified event log, oldest first."""
        db = _connect(self.directory, read_only=True)
        try:
            return tuple(_verified_events(self.directory, db)[1])
        finally:
            db.close()

    @contextmanager
    def _transaction(self) -> Iterator[tuple[sqlite3.Connection, str, list[Event], str]]:
        db = _connect(self.directory, read_only=False)
        try:
            db.execute("BEGIN IMMEDIATE")
            try:
                store_id, events, head = _verified_events(self.directory, db)
                yield db, store_id, events, head
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        finally:
            db.close()

    def _append(self, new: Iterable[Event]) -> bool:
        """Append events whose key is new. The head file is written before the commit.

        A crash between the head write and the commit leaves the head ahead of the log; every
        later read then fails closed with ``audit_ledger_head_mismatch`` until an operator
        reconciles it. A reader racing an uncommitted append fails closed the same way.
        """
        with self._transaction() as (db, store_id, events, head):
            keys = {str(e["key"]) for e in events}
            staged: list[Event] = []
            for event in new:
                if str(event["key"]) not in keys:
                    keys.add(str(event["key"]))
                    copy = json.loads(_canonical_text(event))
                    if copy.get("kind") == "issued":
                        copy["store_id"] = store_id  # sealed inside the write transaction
                    staged.append(copy)
            if not staged:
                return False
            _state(store_id, [*events, *staged], head)  # refuses unknown kinds or bad events
            seq = len(events)
            for event in staged:
                seq += 1
                link = _link(head, seq, event)
                db.execute(
                    "INSERT INTO events (seq, event, prev, head) VALUES (?, ?, ?, ?)",
                    (seq, _canonical_text(event), head, link),
                )
                head = link
            _write_head(self.directory, store_id, seq, head)
            return True

    def _issued_event(self, key: str, receipt: Mapping[str, Any], benchmark_id: str) -> Event:
        if not receipt_self_consistent(receipt, str(receipt.get("receipt_id"))):
            raise LedgerError("receipt_not_self_consistent")
        return {
            "key": key, "kind": "issued", "receipt": dict(receipt), "benchmark_id": benchmark_id,
        }  # fmt: skip

    def issue(self, key: str, receipt: Mapping[str, Any], *, benchmark_id: str) -> bool:
        """Issue a self-consistent receipt for a benchmark; the store id is sealed in the event."""
        if benchmark_id not in self.state().benchmarks:
            raise LedgerError("benchmark_unknown_to_worker")
        return self._append([self._issued_event(key, receipt, benchmark_id)])

    def supersede(
        self, key: str, old_id: str, successor: Mapping[str, Any], *, benchmark_id: str
    ) -> bool:
        if old_id not in self.state().ledger.receipts:
            raise LedgerError("unknown receipt")
        return self._append(
            [
                self._issued_event(f"{key}:issue", successor, benchmark_id),
                {"key": key, "kind": "superseded", "old": old_id,
                 "new": successor["receipt_id"]},
            ]
        )  # fmt: skip

    def register_effect(self, key: str, effect: DependentEffect) -> bool:
        return self._append(
            [{"key": key, "kind": "effect_registered", "effect_id": effect.effect_id,
              "receipt_id": effect.receipt_id, "consumed": effect.consumed}]
        )  # fmt: skip

    def log_disclosure(self, key: str, receipt_id: str, families: Iterable[str]) -> bool:
        return self._append(
            [{"key": key, "kind": "disclosure", "receipt_id": receipt_id,
              "families": sorted(families)}]
        )  # fmt: skip

    def set_benchmark(
        self,
        key: str,
        benchmark_id: str,
        records: Sequence[FamilyRecord],
        *,
        evidence_kind: EvidenceKind,
        materials: Mapping[str, str] | None = None,
        asserted_digests: Mapping[str, str] | None = None,
    ) -> bool:
        """Record the benchmark's current records and, per opaque binding, material or a digest.

        ``asserted_digests`` is for a binding whose material the worker does not hold; it is
        labelled caller-asserted and is never treated as recomputed.
        """
        materials, asserted = dict(materials or {}), dict(asserted_digests or {})
        if (
            not set(materials) | set(asserted) <= set(MATERIAL_BINDINGS)
            or set(materials) & set(asserted)
            or not all(type(v) is str for v in [*materials.values(), *asserted.values()])
            or evidence_kind not in EVIDENCE_KINDS
            or not all(type(r) is FamilyRecord for r in records)
        ):
            raise LedgerError("benchmark_state_malformed")
        return self._append(
            [
                {
                    "key": key, "kind": "benchmark_set", "benchmark_id": benchmark_id,
                    "records": [asdict(r) for r in records], "materials": materials,
                    "asserted_digests": asserted, "evidence_kind": evidence_kind,
                }
            ]
        )  # fmt: skip
