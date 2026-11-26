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


class LiveAuthority(Protocol):
    def guard(self, actor: IntakePrincipal, workflow_ref: str) -> AbstractContextManager[LiveScope]:
        """Pin current custody, resource grants and actor rights until operation returns."""


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
                    namespace TEXT NOT NULL, event_id TEXT NOT NULL,
                    event_revision INTEGER NOT NULL,
                    payload TEXT NOT NULL, ingested_at INTEGER NOT NULL,
                    UNIQUE(tenant,workflow,epoch,resource,event_id,event_revision,payload));
                CREATE TABLE IF NOT EXISTS live_quarantine(
                    tenant TEXT NOT NULL, workflow TEXT NOT NULL, epoch TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, reason TEXT NOT NULL,
                    PRIMARY KEY(tenant,workflow,epoch,fingerprint));
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
        return row[0]

    def _binding(self, db: sqlite3.Connection, reference: str) -> tuple[dict[str, Any], str | None]:
        row = db.execute(
            "SELECT payload,seal,successor FROM live_bindings WHERE ref=?", (reference,)
        ).fetchone()
        _require(row is not None)
        expected = hmac.new(self._key(db), row[0].encode(), hashlib.sha256).hexdigest()
        _require(hmac.compare_digest(row[1], expected))
        return json.loads(row[0]), row[2]

    def save_binding(self, actor: IntakePrincipal, workflow: str, resource: str) -> str:
        with self._scope(actor, workflow) as scope, self._db(write=True) as db:
            _require(scope.expires_at > int(time.time()))
            _require(scope.can_bind, "data_custodian_required")
            grants = [g for g in scope.grants if g.resource == resource]
            _require(len(grants) == 1)
            grant = grants[0]
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
            payload = _canonical(body)
            signature = hmac.new(self._key(db), payload.encode(), hashlib.sha256).hexdigest()
            reference = "feed-" + signature[:32]
            prior = db.execute("SELECT ref FROM live_bindings WHERE ref=?", (reference,)).fetchone()
            if prior:
                return reference
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

    def resolve(
        self, feed_binding_ref: str, authenticated_actor: IntakePrincipal
    ) -> LiveBinding | None:
        try:
            with self._db() as db:
                body, successor = self._binding(db, feed_binding_ref)
            if body["tenant"] != authenticated_actor.tenant:
                return None
            with self._scope(authenticated_actor, body["workflow_ref"]) as scope:
                if not (scope.can_ingest or scope.can_inspect or scope.can_bind):
                    return None
                if scope.engagement_epoch != body["engagement_epoch"]:
                    return None
                original = body["grant"]
                current = [g for g in scope.grants if g.kind == original["kind"]]
                if not current:
                    return None
                state = "superseded" if successor else "current"
                if not successor and asdict(current[0]) != original:
                    return None
                visible_successor = None
                if successor:
                    with self._db() as db:
                        newer, _ = self._binding(db, successor)
                    if any(asdict(g) == newer["grant"] for g in current):
                        visible_successor = successor
                return LiveBinding(
                    scope.tenant,
                    scope.workflow_ref,
                    scope.engagement_epoch,
                    original["kind"],
                    feed_binding_ref,
                    original["kind"],
                    original["resource"],
                    current[0].revision,
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
    ) -> str:
        """Called by the authorized reader with authenticated source lineage, never raw MCP args."""
        require_ref(page_ref)
        _require(len(events) == len(source_positions) == len(event_epochs) and len(events) <= 1000)
        with self._db() as db:
            body, _ = self._binding(db, reference)
        with self._scope(actor, body["workflow_ref"]) as scope, self._db(write=True) as db:
            _require(scope.expires_at > int(time.time()))
            _require(scope.can_ingest and scope.engagement_epoch == body["engagement_epoch"])
            fresh, successor = self._binding(db, reference)
            grant = FeedGrant(**fresh["grant"])
            _require(successor is None and grant in scope.grants)
            _require(source_revision == grant.source_revision)
            _require(type(checkpoint) is int and checkpoint >= grant.checkpoint)
            _require(all(type(p) is int and p <= checkpoint for p in source_positions))
            fingerprint = _digest(
                [source_revision, events, source_positions, event_epochs, checkpoint]
            )
            old = db.execute(
                "SELECT digest,receipt_ref FROM live_pages WHERE binding_ref=? AND page_ref=?",
                (reference, page_ref),
            ).fetchone()
            if old:
                _require(old[0] == fingerprint, "source_page_conflict")
                return old[1]
            for event, position, epoch in zip(events, source_positions, event_epochs, strict=True):
                try:
                    _require(
                        epoch == scope.engagement_epoch and position > grant.checkpoint,
                        "source_epoch_mismatch",
                    )
                    parsed = _event(event, grant)
                except (IntakeError, TypeError, ValueError, KeyError):
                    db.execute(
                        "INSERT OR IGNORE INTO live_quarantine VALUES (?,?,?,?,?)",
                        (
                            scope.tenant,
                            scope.workflow_ref,
                            scope.engagement_epoch,
                            _digest([grant.resource, event, position, epoch]),
                            "invalid_or_unattributable_event",
                        ),
                    )
                    continue
                db.execute(
                    "INSERT OR IGNORE INTO live_events(tenant,workflow,epoch,resource,namespace,"
                    "event_id,event_revision,payload,ingested_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        scope.tenant,
                        scope.workflow_ref,
                        scope.engagement_epoch,
                        grant.resource,
                        grant.deciding_namespace,
                        parsed["event_id"],
                        parsed["event_revision"],
                        _canonical(parsed),
                        int(time.time()),
                    ),
                )
            receipt = "live-" + _digest([reference, page_ref, fingerprint])[:32]
            db.execute(
                "INSERT INTO live_pages VALUES (?,?,?,?,?)",
                (reference, page_ref, fingerprint, checkpoint, receipt),
            )
            snapshot = self._snapshot(db, scope)
            db.execute(
                "INSERT OR IGNORE INTO live_snapshots VALUES (?,?,?,?,?)",
                (
                    scope.tenant,
                    scope.workflow_ref,
                    scope.engagement_epoch,
                    snapshot.sequence,
                    _canonical(asdict(snapshot)),
                ),
            )
            _require(scope.expires_at > int(time.time()))
            return receipt

    def _snapshot(self, db: sqlite3.Connection, scope: LiveScope) -> LiveSnapshot:
        all_rows = db.execute(
            "SELECT * FROM live_events WHERE tenant=? AND workflow=? AND epoch=? ORDER BY seq",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
            ),
        ).fetchall()
        permitted = {(g.resource, g.deciding_namespace) for g in scope.grants}
        rows = [r for r in all_rows if (r["resource"], r["namespace"]) in permitted]
        deliveries: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            deliveries.setdefault((row["resource"], row["event_id"]), []).append(row)
        decisions: dict[tuple[str, str], list[tuple[dict[str, Any], bool]]] = {}
        outcomes: dict[tuple[str, str, int], list[tuple[dict[str, Any], bool]]] = {}
        for versions in deliveries.values():
            latest = max(row["event_revision"] for row in versions)
            current = [row for row in versions if row["event_revision"] == latest]
            conflict = len(current) > 1
            for row in current:
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
            cases.add(event["case_id"])
            families.add(event["family_id"])
            if len({_canonical(v) for v in canonical}) != 1 or any(c for _, c in current):
                disputed += 1
                continue
            truth: dict[tuple[str, str], list[tuple[dict[str, Any], bool]]] = {}
            for outcome, conflict in outcomes.get((*identity, revision), []):
                truth.setdefault((outcome["verifier"], outcome["outcome_id"]), []).append(
                    (outcome, conflict)
                )
            effective = []
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
        for binding in bindings:
            body, _ = self._binding(db, binding[0])
            if any(asdict(g) == body["grant"] for g in scope.grants):
                bound_grants.append(FeedGrant(**body["grant"]))
        bound = any(g.kind == "independent_outcome" for g in bound_grants)
        actors = sorted({g.verifier for g in bound_grants if g.verifier})
        sequence = db.execute(
            "SELECT coalesce(max(p.rowid),0) FROM live_pages p "
            "JOIN live_bindings b ON b.ref=p.binding_ref "
            "WHERE b.tenant=? AND b.workflow=? AND b.epoch=?",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
            ),
        ).fetchone()[0]
        quarantined = db.execute(
            "SELECT count(*) FROM live_quarantine WHERE tenant=? AND workflow=? AND epoch=?",
            (
                scope.tenant,
                scope.workflow_ref,
                scope.engagement_epoch,
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
                    scope.dataset_revision,
                    [asdict(g) for g in scope.grants],
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
            scope.can_bind,
            quarantined,
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
            value = json.loads(row[0])
            value["verified_roots"] = tuple(VerifiedRoot(**v) for v in value["verified_roots"])
            # Historical evidence survives; historical role claims do not grant current actions.
            value["can_bind_verifier"] = scope.can_bind
            return LiveSnapshot(**value)
