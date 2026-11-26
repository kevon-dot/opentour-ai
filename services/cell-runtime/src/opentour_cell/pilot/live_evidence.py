"""Customer-local live bindings and immutable committed-decision revisions.

The authority port is worker-owned. Neither the event body nor a Slack payload
can supply grants, roles, an engagement epoch, or the deciding namespace.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal, Protocol

from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, LiveBinding, require_ref
from opentour_cell.pilot.training import _check_file


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _require(condition: object, reason: str = "live_evidence_unavailable") -> None:
    if not condition:
        raise IntakeError(reason)


@dataclass(frozen=True)
class FeedGrant:
    """Verified current resource grant, obtained inside the authority guard."""

    resource: str
    kind: Literal["decision", "independent_outcome"]
    revision: str
    source_revision: str
    deciding_namespace: str
    checkpoint: int
    lease_ref: str
    verifier: str | None = None


@dataclass(frozen=True)
class LiveScope:
    tenant: str
    workflow_ref: str
    engagement_epoch: str
    dataset_ref: str
    dataset_revision: str
    grants: tuple[FeedGrant, ...]
    can_bind: bool
    can_ingest: bool
    can_inspect: bool
    custodian: str
    expires_at: int
    authority_revision: str = ""
    slack_verifiers: tuple[tuple[str, str], ...] = ()
    can_verify: bool = False
    can_nominate: bool = False


class LiveAuthority(Protocol):
    def guard(self, actor: IntakePrincipal, workflow_ref: str) -> AbstractContextManager[LiveScope]:
        """Pin current custody, resource grants and actor rights until operation returns."""


def grant_digest(grant: FeedGrant) -> str:
    return _digest(asdict(grant))


def binding_candidate(actor: IntakePrincipal, scope: LiveScope, grant: FeedGrant) -> str:
    """A displayed selection is bound to the complete current authority context."""
    return _digest([asdict(actor), asdict(scope), asdict(grant)])


@dataclass(frozen=True)
class VerifiedRoot:
    namespace: str
    decision_id: str
    decision_revision: int
    case_id: str
    family_id: str
    input_ref: str | None
    input_fidelity: str
    action_ref: str
    policy_ref: str
    verified_action_ref: str
    outcome_ref: str


@dataclass(frozen=True)
class LiveSnapshot:
    tenant: str
    workflow_ref: str
    engagement_epoch: str
    dataset_ref: str
    dataset_revision: str
    evidence_revision: str
    sequence: int
    decisions: int
    verified: int
    pending: int
    disputed: int
    cases: int
    families: int
    verified_roots: tuple[VerifiedRoot, ...]
    verifier_bound: bool
    next_actor: str
    can_bind_verifier: bool
    quarantined: int
    unmatched_outcomes: int = 0
    last_ingested_at: int | None = None


_COMMON = {"kind", "event_id", "event_revision", "decision_id", "decision_revision"}
_DECISION = {
    "case_id",
    "family_id",
    "decision_at",
    "action_ref",
    "policy_ref",
    "input_ref",
    "input_fidelity",
    "source_provenance",
    "model_ref",
    "actor_ref",
}
_OUTCOME = {
    "outcome_id",
    "outcome_revision",
    "effective_at",
    "recorded_at",
    "finality",
    "verified_action_ref",
    "verifier",
}


def _event(value: Mapping[str, Any], grant: FeedGrant) -> dict[str, Any]:
    """Normalize a trusted adapter envelope without accepting inline source content."""
    body = dict(value)
    expected = "DecisionRecorded" if grant.kind == "decision" else "OutcomeVerified"
    fields = _DECISION if grant.kind == "decision" else _OUTCOME
    _require(set(body) == _COMMON | fields and body["kind"] == expected, "invalid_live_event")
    for key in ("event_id", "decision_id"):
        require_ref(body[key])
    for key in ("event_revision", "decision_revision"):
        _require(type(body[key]) is int and 0 < body[key] < 2**31, "invalid_live_event")
    clocks: tuple[str, ...]
    if grant.kind == "decision":
        for key in ("case_id", "family_id", "action_ref", "policy_ref", "source_provenance"):
            require_ref(body[key])
        for key in ("input_ref", "model_ref", "actor_ref"):
            if body[key] is not None:
                require_ref(body[key])
        _require(body["input_fidelity"] in {"observed", "reconstructed", "unavailable"})
        _require((body["input_ref"] is None) == (body["input_fidelity"] == "unavailable"))
        clocks = ("decision_at",)
    else:
        require_ref(body["outcome_id"])
        _require(type(body["outcome_revision"]) is int and 0 < body["outcome_revision"] < 2**31)
        _require(body["verifier"] == grant.verifier and grant.verifier is not None)
        _require(body["finality"] in {"final", "reopened", "withdrawn"})
        if body["finality"] == "final":
            require_ref(body["verified_action_ref"])
        else:
            _require(body["verified_action_ref"] is None)
        clocks = ("effective_at", "recorded_at")
    for key in clocks:
        _require(type(body[key]) is int and 0 <= body[key] < 2**63, "invalid_live_event")
    return body


class LiveEvidence:
    """Explicit initialization writes; resolution and snapshots never create or migrate state."""

    def __init__(self, directory: Path, authority: LiveAuthority):
        self.path = directory / "live-evidence.sqlite3"
        self.authority = authority

    def initialize(self) -> None:
        parent = self.path.parent
        _require(parent.is_absolute() and parent.resolve(strict=True) == parent)
        info = parent.stat()
        _require(info.st_uid == os.getuid() and not info.st_mode & 0o077)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        with self._db(write=True) as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS live_identity(id INTEGER PRIMARY KEY, key BLOB NOT NULL);
                CREATE TABLE IF NOT EXISTS live_bindings(
                    ref TEXT PRIMARY KEY, tenant TEXT NOT NULL, workflow TEXT NOT NULL,
                    epoch TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    seal TEXT NOT NULL, successor TEXT);
                CREATE TABLE IF NOT EXISTS live_events(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT, tenant TEXT NOT NULL,
                    workflow TEXT NOT NULL, epoch TEXT NOT NULL, resource TEXT NOT NULL,
                    binding_ref TEXT NOT NULL,
                    dataset_ref TEXT NOT NULL, dataset_revision TEXT NOT NULL,
                    namespace TEXT NOT NULL, event_id TEXT NOT NULL,
                    event_revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, ingested_at INTEGER NOT NULL,
                    UNIQUE(tenant,workflow,epoch,dataset_ref,dataset_revision,
                           resource,event_id,event_revision,payload));
                CREATE TABLE IF NOT EXISTS live_quarantine(
                    tenant TEXT NOT NULL, workflow TEXT NOT NULL, epoch TEXT NOT NULL,
                    dataset_ref TEXT NOT NULL, dataset_revision TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(tenant,workflow,epoch,dataset_ref,dataset_revision,fingerprint));
                CREATE TABLE IF NOT EXISTS live_pages(
                    binding_ref TEXT NOT NULL, page_ref TEXT NOT NULL, digest TEXT NOT NULL,
                    checkpoint INTEGER NOT NULL, receipt_ref TEXT NOT NULL,
                    PRIMARY KEY(binding_ref,page_ref));
                CREATE TABLE IF NOT EXISTS live_snapshots(
                    tenant TEXT NOT NULL, workflow TEXT NOT NULL, epoch TEXT NOT NULL,
                    sequence INTEGER NOT NULL, body TEXT NOT NULL,
                    PRIMARY KEY(tenant,workflow,epoch,sequence));
            """)
            db.execute(
                "INSERT OR IGNORE INTO live_identity VALUES (1,?)", (secrets.token_bytes(32),)
            )

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        _check_file(self.path)
        if not write:
            from opentour_cell.pilot.receipt_recovery import _open_db

            with _open_db(self.path) as existing:
                _require(existing is not None)
                assert existing is not None
                yield existing
            return
        db = sqlite3.connect(self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"), uri=True)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def _scope(self, actor: IntakePrincipal, workflow: str) -> Iterator[LiveScope]:
        with self.authority.guard(actor, workflow) as scope:
            _require(scope.tenant == actor.tenant and scope.workflow_ref == workflow)
            _require(scope.expires_at > int(time.time()))
            yield scope
            _require(scope.expires_at > int(time.time()))

    @staticmethod
    def _key(db: sqlite3.Connection) -> bytes:
        row = db.execute("SELECT key FROM live_identity WHERE id=1").fetchone()
        _require(row is not None and isinstance(row[0], bytes) and len(row[0]) == 32)
        key: bytes = row[0]
        return key

    def _binding(self, db: sqlite3.Connection, reference: str) -> tuple[dict[str, Any], str | None]:
        row = db.execute(
            "SELECT payload,seal,successor FROM live_bindings WHERE ref=?", (reference,)
        ).fetchone()
        _require(row is not None)
        expected = hmac.new(self._key(db), row[0].encode(), hashlib.sha256).hexdigest()
        _require(hmac.compare_digest(row[1], expected) and reference == "feed-" + expected[:32])
        return json.loads(row[0]), row[2]

    @staticmethod
    def _same_scope(body: Mapping[str, Any], scope: LiveScope) -> bool:
        if "grant" in body:
            assigned = dict(scope.slack_verifiers).get(body["grant"]["resource"])
            if body.get("slack_verifier") != assigned:
                return False
        return all(
            body.get(key) == getattr(scope, key)
            for key in (
                "tenant",
                "workflow_ref",
                "engagement_epoch",
                "dataset_ref",
                "dataset_revision",
            )
        )

    def save_binding(
        self,
        actor: IntakePrincipal,
        workflow: str,
        resource: str,
        *,
        expected_revision: str | None = None,
    ) -> str:
        with self._scope(actor, workflow) as scope, self._db(write=True) as db:
            _require(scope.expires_at > int(time.time()))
            _require(scope.can_bind, "data_custodian_required")
            grants = [g for g in scope.grants if g.resource == resource]
            _require(len(grants) == 1)
            grant = grants[0]
            _require(
                expected_revision is None
                or expected_revision == binding_candidate(actor, scope, grant)
            )
            _require(type(grant.checkpoint) is int and grant.checkpoint >= 0)
            require_ref(grant.lease_ref)
            body = {
                "tenant": scope.tenant,
                "workflow_ref": workflow,
                "engagement_epoch": scope.engagement_epoch,
                "dataset_ref": scope.dataset_ref,
                "dataset_revision": scope.dataset_revision,
                "grant": asdict(grant),
            }
            subject = dict(scope.slack_verifiers).get(resource)
            if subject is not None:
                body["slack_verifier"] = subject
            existing = db.execute(
                "SELECT ref FROM live_bindings WHERE tenant=? AND workflow=? "
                "AND epoch=? AND kind=? AND successor IS NULL",
                (scope.tenant, workflow, scope.engagement_epoch, grant.kind),
            ).fetchall()
            for active in existing:
                previous, _ = self._binding(db, active[0])
                if {k: v for k, v in previous.items() if k != "generation"} == body:
                    return str(active[0])
            body["generation"] = db.execute(
                "SELECT count(*) + 1 FROM live_bindings WHERE tenant=? AND workflow=? "
                "AND epoch=? AND kind=?",
                (scope.tenant, workflow, scope.engagement_epoch, grant.kind),
            ).fetchone()[0]
            payload = _canonical(body)
            signature = hmac.new(self._key(db), payload.encode(), hashlib.sha256).hexdigest()
            reference = "feed-" + signature[:32]
            db.execute(
                "UPDATE live_bindings SET successor=? WHERE tenant=? AND workflow=? "
                "AND epoch=? AND kind=? AND successor IS NULL",
                (reference, scope.tenant, workflow, scope.engagement_epoch, grant.kind),
            )
            db.execute(
                "INSERT INTO live_bindings VALUES (?,?,?,?,?,?,?,NULL)",
                (
                    reference,
                    scope.tenant,
                    workflow,
                    scope.engagement_epoch,
                    grant.kind,
                    payload,
                    signature,
                ),
            )
            _require(scope.expires_at > int(time.time()))
            return reference

    def current_bindings(
        self, actor: IntakePrincipal, workflow_ref: str
    ) -> tuple[tuple[str, LiveBinding], ...]:
        with self._scope(actor, workflow_ref) as scope:
            _require(scope.can_bind or scope.can_inspect)
            try:
                with self._db() as db:
                    rows = db.execute(
                        "SELECT ref FROM live_bindings WHERE tenant=? AND workflow=? "
                        "AND epoch=? AND successor IS NULL ORDER BY ref",
                        (scope.tenant, workflow_ref, scope.engagement_epoch),
                    ).fetchall()
                    current = []
                    for row in rows:
                        binding = self.resolve(row[0], actor)
                        if binding is not None and binding.state == "current":
                            current.append((str(row[0]), binding))
                    return tuple(current)
            except FileNotFoundError:
                _require(
                    not any(
                        os.path.lexists(str(self.path) + s) for s in ("-wal", "-shm", "-journal")
                    )
                )
                return ()

    def resolve(
        self, feed_binding_ref: str, authenticated_actor: IntakePrincipal
    ) -> LiveBinding | None:
        try:
            with self._db() as db:
                body, _ = self._binding(db, feed_binding_ref)
            if body["tenant"] != authenticated_actor.tenant:
                return None
            with self._scope(authenticated_actor, body["workflow_ref"]) as scope, self._db() as db:
                body, successor = self._binding(db, feed_binding_ref)
                if not (scope.can_ingest or scope.can_inspect or scope.can_bind):
                    return None
                if not self._same_scope(body, scope):
                    return None
                if body.get("slack_verifier") and authenticated_actor.role == "intake":
                    return None
                original = body["grant"]
                exact = next((g for g in scope.grants if asdict(g) == original), None)
                state: Literal["superseded", "current"] = "superseded" if successor else "current"
                if not successor and exact is None:
                    return None
                visible_successor = None
                if successor:
                    newer, _ = self._binding(db, successor)
                    if self._same_scope(newer, scope) and any(
                        asdict(g) == newer["grant"] for g in scope.grants
                    ):
                        _require(newer["grant"]["kind"] == original["kind"])
                        _require(newer["generation"] > body["generation"])
                        visible_successor = successor
                        exact = FeedGrant(**newer["grant"])
                if exact is None:
                    return None
                return LiveBinding(
                    scope.tenant,
                    scope.workflow_ref,
                    scope.engagement_epoch,
                    original["kind"],
                    feed_binding_ref,
                    original["kind"],
                    original["resource"],
                    exact.revision,
                    state,
                    original["source_revision"],
                    visible_successor,
                )
        except (OSError, sqlite3.Error, IntakeError):
            return None

    def ingest_page(
        self,
        actor: IntakePrincipal,
        reference: str,
        *,
        page_ref: str,
        source_revision: str,
        events: tuple[Mapping[str, Any], ...],
        source_positions: tuple[int, ...],
        event_epochs: tuple[str, ...],
        checkpoint: int,
        read_metadata: Mapping[str, Any] | None = None,
    ) -> str:
        """Called by the authorized reader with authenticated source lineage, never raw MCP args."""
        require_ref(page_ref)
        _require(len(events) == len(source_positions) == len(event_epochs) and len(events) <= 1000)
        if read_metadata is not None:
            _require(
                set(read_metadata)
                == {"interval_start", "interval_end", "coverage_through", "complete", "next_cursor"}
            )
            _require(type(read_metadata["complete"]) is bool)
            for name in ("interval_start", "interval_end", "coverage_through"):
                value = read_metadata[name]
                _require(value is None or (type(value) is int and 0 <= value < 2**63))
            start, end = read_metadata["interval_start"], read_metadata["interval_end"]
            _require(
                (start is None and end is None)
                or (start is not None and end is not None and start <= end)
            )
            coverage = read_metadata["coverage_through"]
            _require(coverage is None or (start is not None and start <= coverage <= end))
            if read_metadata["next_cursor"] is not None:
                require_ref(read_metadata["next_cursor"])
        with self._db() as db:
            body, _ = self._binding(db, reference)
        with self._scope(actor, body["workflow_ref"]) as scope, self._db(write=True) as db:
            _require(scope.expires_at > int(time.time()))
            _require(scope.can_ingest and scope.engagement_epoch == body["engagement_epoch"])
            fresh, successor = self._binding(db, reference)
            _require(self._same_scope(fresh, scope) and fresh.get("slack_verifier") is None)
            grant = FeedGrant(**fresh["grant"])
            _require(successor is None and grant in scope.grants)
            _require(source_revision == grant.source_revision)
            _require(type(checkpoint) is int and checkpoint >= grant.checkpoint)
            _require(all(type(p) is int and p <= checkpoint for p in source_positions))
            page_identity: list[Any] = [
                source_revision,
                events,
                source_positions,
                event_epochs,
                checkpoint,
            ]
            if read_metadata is not None:
                page_identity.append(read_metadata)
            fingerprint = _digest(page_identity)
            old = db.execute(
                "SELECT digest,receipt_ref FROM live_pages WHERE binding_ref=? AND page_ref=?",
                (reference, page_ref),
            ).fetchone()
            if old:
                _require(old[0] == fingerprint, "source_page_conflict")
                return str(old[1])
            for event, position, epoch in zip(events, source_positions, event_epochs, strict=True):
                try:
                    _require(
                        epoch == scope.engagement_epoch and position > grant.checkpoint,
                        "source_epoch_mismatch",
                    )
                    parsed = _event(event, grant)
                except (IntakeError, TypeError, ValueError, KeyError):
                    db.execute(
                        "INSERT OR IGNORE INTO live_quarantine VALUES (?,?,?,?,?,?,?)",
                        (
                            scope.tenant,
                            scope.workflow_ref,
                            scope.engagement_epoch,
                            scope.dataset_ref,
                            scope.dataset_revision,
                            _digest([grant.resource, event, position, epoch]),
                            "invalid_or_unattributable_event",
                        ),
                    )
                    continue
                self._record_event(db, scope, grant, reference, parsed)
            receipt = "live-" + _digest([reference, page_ref, fingerprint])[:32]
            db.execute(
                "INSERT INTO live_pages VALUES (?,?,?,?,?)",
                (reference, page_ref, fingerprint, checkpoint, receipt),
            )
            if read_metadata is not None:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS live_reads (binding_ref TEXT NOT NULL, "
                    "page_ref TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(binding_ref,page_ref))"
                )
                db.execute(
                    "INSERT INTO live_reads VALUES (?,?,?)",
                    (
                        reference,
                        page_ref,
                        _canonical(
                            {
                                **read_metadata,
                                "receipt_ref": receipt,
                                "source_revision": source_revision,
                                "checkpoint": checkpoint,
                                "row_count": len(events),
                                "polled_at": int(time.time()),
                            }
                        ),
                    ),
                )
            self._archive(db, scope)
            _require(scope.expires_at > int(time.time()))
            return receipt

    def _record_event(
        self,
        db: sqlite3.Connection,
        scope: LiveScope,
        grant: FeedGrant,
        reference: str,
        parsed: dict[str, Any],
    ) -> None:
        encoded = _canonical(parsed)
        db.execute(
            "INSERT OR IGNORE INTO live_events(tenant,workflow,epoch,resource,binding_ref,"
            "dataset_ref,dataset_revision,namespace,event_id,event_revision,payload,"
            "ingested_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
                grant.resource,
                reference,
                scope.dataset_ref,
                scope.dataset_revision,
                grant.deciding_namespace,
                parsed["event_id"],
                parsed["event_revision"],
                encoded,
                int(time.time()),
            ),
        )
        row = db.execute(
            "SELECT seq FROM live_events WHERE tenant=? AND workflow=? AND epoch=? "
            "AND dataset_ref=? AND dataset_revision=? AND resource=? AND event_id=? "
            "AND event_revision=? AND payload=?",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
                scope.dataset_ref,
                scope.dataset_revision,
                grant.resource,
                parsed["event_id"],
                parsed["event_revision"],
                encoded,
            ),
        ).fetchone()
        _require(row is not None)
        db.execute(
            "CREATE TABLE IF NOT EXISTS live_event_origins ("
            "seq INTEGER NOT NULL, binding_ref TEXT NOT NULL, "
            "PRIMARY KEY(seq,binding_ref))"
        )
        db.execute("INSERT OR IGNORE INTO live_event_origins VALUES (?,?)", (row[0], reference))

    def _archive(self, db: sqlite3.Connection, scope: LiveScope) -> None:
        snapshot = self._snapshot(db, scope)
        db.execute(
            "INSERT OR IGNORE INTO live_snapshots VALUES (?,?,?,?,?)",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
                snapshot.sequence,
                _canonical(
                    {
                        "snapshot": asdict(snapshot),
                        "grants": [asdict(g) for g in scope.grants],
                        "slack_verifiers": dict(scope.slack_verifiers),
                    }
                ),
            ),
        )

    def read_receipts(self, actor: IntakePrincipal, workflow: str) -> tuple[dict[str, Any], ...]:
        """Read actual acknowledged source pages; no poll or state initialization."""
        with self._scope(actor, workflow) as scope:
            _require(scope.can_inspect)
            with self._db() as db:
                if (
                    db.execute("SELECT 1 FROM sqlite_master WHERE name='live_reads'").fetchone()
                    is None
                ):
                    return ()
                rows = db.execute(
                    "SELECT r.binding_ref,r.page_ref,r.body FROM live_reads r "
                    "JOIN live_bindings b ON b.ref=r.binding_ref "
                    "WHERE b.tenant=? AND b.workflow=? AND b.epoch=? ORDER BY r.rowid",
                    (scope.tenant, workflow, scope.engagement_epoch),
                ).fetchall()
                receipts = []
                for row in rows:
                    body, successor = self._binding(db, row[0])
                    if (
                        successor is None
                        and self._same_scope(body, scope)
                        and FeedGrant(**body["grant"]) in scope.grants
                    ):
                        receipts.append(
                            {"binding_ref": row[0], "page_ref": row[1], **json.loads(row[2])}
                        )
                return tuple(receipts)

    def _snapshot(
        self,
        db: sqlite3.Connection,
        scope: LiveScope,
        *,
        form_targets: dict[tuple[str, str, int], str] | None = None,
        verifying_actor: str | None = None,
    ) -> LiveSnapshot:
        all_rows = db.execute(
            "SELECT * FROM live_events WHERE tenant=? AND workflow=? AND epoch=? "
            "AND dataset_ref=? AND dataset_revision=? ORDER BY seq",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
                scope.dataset_ref,
                scope.dataset_revision,
            ),
        ).fetchall()
        has_origins = (
            db.execute("SELECT 1 FROM sqlite_master WHERE name='live_event_origins'").fetchone()
            is not None
        )
        rows = []
        for row in all_rows:
            origins = {row["binding_ref"]}
            if has_origins:
                origins.update(
                    r[0]
                    for r in db.execute(
                        "SELECT binding_ref FROM live_event_origins WHERE seq=?", (row["seq"],)
                    )
                )
            namespaces = set()
            for reference in sorted(origins):
                body, _ = self._binding(db, reference)
                grant = FeedGrant(**body["grant"])
                if self._same_scope(body, scope) and grant in scope.grants:
                    namespaces.add(grant.deciding_namespace)
            for namespace in sorted(namespaces):
                rows.append({**dict(row), "namespace": namespace})
        deliveries: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            deliveries.setdefault((row["resource"], row["namespace"], row["event_id"]), []).append(
                row
            )
        decisions: dict[tuple[str, str], list[tuple[dict[str, Any], bool]]] = {}
        outcomes: dict[tuple[str, str, int], list[tuple[dict[str, Any], bool]]] = {}
        for delivery_versions in deliveries.values():
            latest = max(row["event_revision"] for row in delivery_versions)
            current_deliveries = [
                row for row in delivery_versions if row["event_revision"] == latest
            ]
            conflict = len(current_deliveries) > 1
            for row in current_deliveries:
                event = json.loads(row["payload"])
                identity = (row["namespace"], event["decision_id"])
                if event["kind"] == "DecisionRecorded":
                    decisions.setdefault(identity, []).append((event, conflict))
                else:
                    outcomes.setdefault((*identity, event["decision_revision"]), []).append(
                        (event, conflict)
                    )
        verified: list[VerifiedRoot] = []
        pending = disputed = 0
        cases, families = set(), set()
        known_decisions = {
            (*identity, event["decision_revision"])
            for identity, versions in decisions.items()
            for event, _ in versions
        }
        unmatched_outcomes = len(
            {
                (*target, event["verifier"], event["outcome_id"])
                for target, versions in outcomes.items()
                if target not in known_decisions
                for event, _ in versions
            }
        )
        for identity, versions in decisions.items():
            revision = max(event["decision_revision"] for event, _ in versions)
            current = [
                (event, conflict)
                for event, conflict in versions
                if event["decision_revision"] == revision
            ]
            canonical = [
                {
                    k: v
                    for k, v in event.items()
                    if k not in {"event_id", "event_revision", "source_provenance"}
                }
                for event, _ in current
            ]
            event = current[0][0]
            attributed_cases = {e["case_id"] for e, _ in current}
            attributed_families = {e["family_id"] for e, _ in current}
            if len(attributed_cases) == 1:
                cases.update(attributed_cases)
            if len(attributed_families) == 1:
                families.update(attributed_families)
            if len({_canonical(v) for v in canonical}) != 1 or any(c for _, c in current):
                disputed += 1
                continue
            if form_targets is not None and event["actor_ref"] != verifying_actor:
                form_targets[(*identity, revision)] = _digest(canonical[0])
            truth: dict[tuple[str, str], list[tuple[dict[str, Any], bool]]] = {}
            for outcome, conflict in outcomes.get((*identity, revision), []):
                truth.setdefault((outcome["verifier"], outcome["outcome_id"]), []).append(
                    (outcome, conflict)
                )
            effective: list[tuple[dict[str, Any], bool]] = []
            for revisions in truth.values():
                latest = max(o["outcome_revision"] for o, _ in revisions)
                effective.extend((o, c) for o, c in revisions if o["outcome_revision"] == latest)
            finals = [o for o, _ in effective if o["finality"] == "final"]
            if (
                any(c for _, c in effective)
                or len({(o["finality"], o["verified_action_ref"]) for o, _ in effective}) > 1
            ):
                disputed += 1
            elif finals:
                outcome = finals[0]
                verified.append(
                    VerifiedRoot(
                        *identity,
                        revision,
                        event["case_id"],
                        event["family_id"],
                        event["input_ref"],
                        event["input_fidelity"],
                        event["action_ref"],
                        event["policy_ref"],
                        outcome["verified_action_ref"],
                        outcome["outcome_id"],
                    )
                )
            else:
                pending += 1
        bindings = db.execute(
            "SELECT ref FROM live_bindings WHERE tenant=? AND workflow=? AND epoch=? "
            "AND successor IS NULL",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
            ),
        ).fetchall()
        bound_grants = []
        active_references = []
        matching_scope = False
        for binding in bindings:
            body, _ = self._binding(db, binding[0])
            if not self._same_scope(body, scope):
                continue
            matching_scope = True
            if any(asdict(g) == body["grant"] for g in scope.grants):
                bound_grants.append(FeedGrant(**body["grant"]))
                active_references.append(binding[0])
        _require(not bindings or matching_scope)
        bound = any(g.kind == "independent_outcome" for g in bound_grants)
        actors = sorted({g.verifier for g in bound_grants if g.verifier})
        page_rows = db.execute(
            "SELECT p.rowid,p.binding_ref FROM live_pages p "
            "JOIN live_bindings b ON b.ref=p.binding_ref "
            "WHERE b.tenant=? AND b.workflow=? AND b.epoch=?",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
            ),
        ).fetchall()
        sequence = max(
            (row[0] for row in page_rows if self._same_scope(self._binding(db, row[1])[0], scope)),
            default=0,
        )
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='live_form_receipts'").fetchone():
            sequence += db.execute(
                "SELECT count(*) FROM live_form_receipts WHERE tenant=? AND workflow=? "
                "AND epoch=? AND dataset=? AND dataset_revision=?",
                (
                    scope.tenant,
                    scope.workflow_ref,
                    scope.engagement_epoch,
                    scope.dataset_ref,
                    scope.dataset_revision,
                ),
            ).fetchone()[0]
        quarantined = db.execute(
            "SELECT count(*) FROM live_quarantine WHERE tenant=? AND workflow=? AND epoch=? "
            "AND dataset_ref=? AND dataset_revision=?",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
                scope.dataset_ref,
                scope.dataset_revision,
            ),
        ).fetchone()[0]
        return LiveSnapshot(
            scope.tenant,
            scope.workflow_ref,
            scope.engagement_epoch,
            scope.dataset_ref,
            scope.dataset_revision,
            _digest(
                [
                    scope.tenant,
                    scope.workflow_ref,
                    scope.engagement_epoch,
                    scope.dataset_ref,
                    scope.dataset_revision,
                    sorted((asdict(g) for g in scope.grants), key=_canonical),
                    sorted(active_references),
                    [r["payload"] for r in rows],
                    quarantined,
                ]
            ),
            sequence,
            len(decisions),
            len(verified),
            pending,
            disputed,
            len(cases),
            len(families),
            tuple(sorted(verified, key=lambda r: (r.namespace, r.decision_id))),
            bound,
            actors[0] if actors else scope.custodian,
            scope.can_bind and any(g.kind == "independent_outcome" for g in scope.grants),
            quarantined,
            unmatched_outcomes,
            max((row["ingested_at"] for row in rows), default=None),
        )

    def snapshot(
        self, actor: IntakePrincipal, workflow_ref: str, *, as_of_revision: int | None = None
    ) -> LiveSnapshot:
        with self._scope(actor, workflow_ref) as scope, self._db() as db:
            _require(scope.can_inspect)
            if as_of_revision is None:
                return self._snapshot(db, scope)
            row = db.execute(
                "SELECT body FROM live_snapshots WHERE tenant=? AND workflow=? "
                "AND epoch=? AND sequence=?",
                (
                    scope.tenant,
                    workflow_ref,
                    scope.engagement_epoch,
                    as_of_revision,
                ),
            ).fetchone()
            _require(row is not None)
            saved = json.loads(row[0])
            _require(saved.get("slack_verifiers", {}) == dict(scope.slack_verifiers))
            value = saved["snapshot"]
            _require(self._same_scope(value, scope))
            # Retention is not continuing disclosure authority. Historical receipts
            # stay immutable, but removed/replaced grants hold the entire old view.
            _require(all(grant in [asdict(g) for g in scope.grants] for grant in saved["grants"]))
            value["verified_roots"] = tuple(VerifiedRoot(**v) for v in value["verified_roots"])
            # Historical evidence survives; historical role claims do not grant current actions.
            value["can_bind_verifier"] = scope.can_bind and any(
                g.kind == "independent_outcome" for g in scope.grants
            )
            return LiveSnapshot(**value)
