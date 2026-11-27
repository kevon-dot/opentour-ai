"""Reduce metadata-only app capture without persisting request or response content."""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.slack_adapter import Refused
from opentour_cell.pilot.training import LocalTrainingStore

Boundary = Literal["application_request", "provider_response", "application_commit"]
Action = Literal["record", "inspect"]
MAX_CHUNKS = 1024
MAX_CHUNK_BYTES = 64 * 1024
MAX_EVENTS = 2 * MAX_CHUNKS + 32
_BOUNDARY: dict[str, Boundary] = {
    "start": "application_request",
    "chunk": "provider_response",
    "terminal": "provider_response",
    "commit": "application_commit",
}


class CaptureError(ValueError):
    """A scoped metadata diagnostic."""


def _require(condition: object, reason: str = "decision_capture_unavailable") -> None:
    if not condition:
        raise CaptureError(reason)


@dataclass(frozen=True)
class CaptureGrant:
    actor: str
    tenant: str
    epoch: str
    workflow: str
    revision: str
    policy_revision: str
    expires_at: int
    mode: Literal["off", "metadata_only"]
    sources: tuple[tuple[Boundary, str], ...]
    opaque_key: bytes = field(repr=False)

    def __post_init__(self) -> None:
        for value in (
            self.actor,
            self.tenant,
            self.epoch,
            self.workflow,
            self.revision,
            self.policy_revision,
        ):
            require_ref(value)
        for boundary, source in self.sources:
            _require(boundary in _BOUNDARY.values())
            require_ref(source)
        _require(
            type(self.expires_at) is int
            and 0 < self.expires_at < 2**63
            and self.mode in {"off", "metadata_only"}
            and len(dict(self.sources)) == len(self.sources)
            and type(self.opaque_key) is bytes
            and len(self.opaque_key) == 32
        )


class CaptureAuthority(Protocol):
    def resolve(
        self, actor: str, workflow: str, action: Action, boundary: Boundary | None
    ) -> CaptureGrant | None: ...


@dataclass(frozen=True)
class CapturedAttempt:
    operation_ref: str
    attempt_ref: str
    case_ref: str
    family_ref: str
    decision_ref: str
    decision_revision: int

    def __post_init__(self) -> None:
        for value in (
            self.operation_ref,
            self.attempt_ref,
            self.case_ref,
            self.family_ref,
            self.decision_ref,
        ):
            require_ref(value)
        _require(type(self.decision_revision) is int and 0 < self.decision_revision < 2**31)


@dataclass(frozen=True)
class EventRef:
    source_ref: str
    event_ref: str
    event_revision: int
    observed_at: int

    def __post_init__(self) -> None:
        require_ref(self.source_ref)
        require_ref(self.event_ref)
        _require(
            type(self.event_revision) is int
            and 0 < self.event_revision < 2**31
            and type(self.observed_at) is int
            and 0 <= self.observed_at < 2**63
        )


@dataclass(frozen=True)
class RequestMetadata:
    """References observed at the application invocation."""

    requested_model_ref: str
    projection_revision: str
    template_revision: str | None = None

    def __post_init__(self) -> None:
        require_ref(self.requested_model_ref)
        require_ref(self.projection_revision)
        if self.template_revision is not None:
            require_ref(self.template_revision)


@dataclass(frozen=True)
class ResponseMetadata:
    """Provider and parser references from the original attempt."""

    provider_receipt_ref: str
    served_model_ref: str | None = None
    parsed_output_ref: str | None = None
    parser_revision: str | None = None

    def __post_init__(self) -> None:
        require_ref(self.provider_receipt_ref)
        for ref in (self.served_model_ref, self.parsed_output_ref, self.parser_revision):
            if ref is not None:
                require_ref(ref)
        _require((self.parsed_output_ref is None) == (self.parser_revision is None))


def _observation_seal(
    grant: CaptureGrant, attempt: str, observed_at: int, field: str, payload: object
) -> str:
    boundary: Boundary = "application_request" if field == "request" else "provider_response"
    subject = [
        "opentour.pilot.attempt-metadata.v1",
        grant.tenant,
        grant.epoch,
        grant.workflow,
        grant.revision,
        grant.policy_revision,
        dict(grant.sources)[boundary],
        attempt,
        observed_at,
        field,
        payload,
    ]
    return hmac.digest(grant.opaque_key, canonical(subject).encode(), "sha256").hex()


def _seal_observation(
    grant: CaptureGrant,
    attempt: CapturedAttempt,
    event: EventRef,
    field: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    if field not in payload:
        return payload
    seal = _observation_seal(grant, canonical(asdict(attempt)), event.observed_at, field, payload)
    return payload | {"metadata_seal": seal}


class CaptureStreams:
    """Journal observations from the existing application invocation and commit boundaries."""

    def __init__(
        self,
        store: LocalTrainingStore,
        authority: CaptureAuthority,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.authority = authority
        self.clock = clock

    def _current(
        self,
        db: sqlite3.Connection,
        actor: str,
        workflow: str,
        action: Action,
        boundary: Boundary | None,
    ) -> CaptureGrant:
        scope = db.execute("SELECT id,withdrawn_at FROM engagement WHERE singleton=1").fetchone()
        _require(scope is not None and scope[1] is None)
        grant = self.authority.resolve(require_ref(actor), require_ref(workflow), action, boundary)
        _require(
            isinstance(grant, CaptureGrant)
            and (grant.actor, grant.tenant, grant.epoch, grant.workflow)
            == (actor, self.store.organization, scope[0], workflow)
            and (boundary is None or boundary in dict(grant.sources))
            and self.clock() < grant.expires_at
        )
        assert grant is not None
        _require(grant.mode == "metadata_only", "decision_capture_disabled")
        return grant

    def _persist(
        self,
        actor: str,
        workflow: str,
        attempt: CapturedAttempt,
        event: EventRef,
        boundary: Boundary,
        kind: str,
        project: Callable[[CaptureGrant], dict[str, Any]],
    ) -> None:
        _require(event.observed_at <= self.clock())
        with self.store._db(write=True) as db:
            grant = self._current(db, actor, workflow, "record", boundary)
            _require(dict(grant.sources)[boundary] == event.source_ref)
            payload = canonical(project(grant))
            identity = canonical(asdict(attempt))
            db.execute(
                "CREATE TABLE IF NOT EXISTS decision_capture_streams("
                "tenant TEXT NOT NULL,epoch TEXT NOT NULL,workflow TEXT NOT NULL,"
                "grant_revision TEXT NOT NULL,policy_revision TEXT NOT NULL,"
                "operation_ref TEXT NOT NULL,attempt TEXT NOT NULL,"
                "decision_ref TEXT NOT NULL,decision_revision INTEGER NOT NULL,"
                "source_ref TEXT NOT NULL,event_ref TEXT NOT NULL,event_revision INTEGER NOT NULL,"
                "observed_at INTEGER NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,"
                "PRIMARY KEY(tenant,epoch,workflow,grant_revision,policy_revision,"
                "operation_ref,attempt,"
                "source_ref,event_ref,event_revision,observed_at,kind,payload))"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS capture_business_commit "
                "ON decision_capture_streams("
                "tenant,epoch,workflow,grant_revision,policy_revision,source_ref,"
                "decision_ref,decision_revision,operation_ref) WHERE kind='commit'"
            )
            scope = (
                grant.tenant,
                grant.epoch,
                grant.workflow,
                grant.revision,
                grant.policy_revision,
            )
            other = db.execute(
                "SELECT 1 FROM decision_capture_streams WHERE "
                "(tenant,epoch,workflow,grant_revision,policy_revision,source_ref,event_ref,"
                "event_revision)=(?,?,?,?,?,?,?,?) AND operation_ref<>? LIMIT 1",
                (
                    *scope,
                    event.source_ref,
                    event.event_ref,
                    event.event_revision,
                    attempt.operation_ref,
                ),
            ).fetchone()
            _require(other is None, "capture_event_scope_changed")
            db.execute(
                "INSERT OR IGNORE INTO decision_capture_streams "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    *scope,
                    attempt.operation_ref,
                    identity,
                    attempt.decision_ref,
                    attempt.decision_revision,
                    event.source_ref,
                    event.event_ref,
                    event.event_revision,
                    event.observed_at,
                    kind,
                    payload,
                ),
            )
            count = db.execute(
                "SELECT count(*) FROM decision_capture_streams WHERE "
                "(tenant,epoch,workflow,grant_revision,policy_revision,operation_ref)=(?,?,?,?,?,?)",
                (*scope, attempt.operation_ref),
            ).fetchone()[0]
            _require(count <= MAX_EVENTS, "capture_operation_limit")
            _require(self._current(db, actor, workflow, "record", boundary) == grant)
            _require(not os.path.lexists(self.store.path.with_name("custody.sqlite3")))

    def started(
        self,
        actor: str,
        workflow: str,
        attempt: CapturedAttempt,
        event: EventRef,
        *,
        parent_attempt: str | None = None,
        metadata: RequestMetadata | None = None,
    ) -> None:
        if parent_attempt is not None:
            require_ref(parent_attempt)
            _require(parent_attempt != attempt.attempt_ref)
        payload: dict[str, Any] = {"parent_attempt": parent_attempt}
        if metadata is not None:
            _require(type(metadata) is RequestMetadata)
            payload["request"] = asdict(metadata)
        self._persist(
            actor,
            workflow,
            attempt,
            event,
            "application_request",
            "start",
            lambda grant: _seal_observation(grant, attempt, event, "request", payload),
        )

    def observed_chunk(
        self,
        actor: str,
        workflow: str,
        attempt: CapturedAttempt,
        event: EventRef,
        index: int,
        observed_bytes: bytes,
    ) -> None:
        _require(
            type(index) is int
            and 0 <= index < MAX_CHUNKS
            and type(observed_bytes) is bytes
            and len(observed_bytes) <= MAX_CHUNK_BYTES
        )

        def project(grant: CaptureGrant) -> dict[str, Any]:
            binding = canonical(
                [
                    grant.tenant,
                    grant.epoch,
                    grant.workflow,
                    grant.revision,
                    grant.policy_revision,
                    asdict(attempt),
                    index,
                ]
            ).encode()
            opaque_ref = hmac.digest(grant.opaque_key, binding + b"\x00" + observed_bytes, "sha256")
            return {"index": index, "opaque_chunk_ref": opaque_ref.hex()}

        self._persist(actor, workflow, attempt, event, "provider_response", "chunk", project)

    def terminal(
        self,
        actor: str,
        workflow: str,
        attempt: CapturedAttempt,
        event: EventRef,
        state: Literal["finished", "timeout", "failed"],
        expected_chunks: int | None,
        *,
        metadata: ResponseMetadata | None = None,
    ) -> None:
        _require(
            (
                state == "finished"
                and type(expected_chunks) is int
                and 0 <= expected_chunks <= MAX_CHUNKS
            )
            or (state in {"timeout", "failed"} and expected_chunks is None)
        )
        payload: dict[str, Any] = {"state": state, "expected_chunks": expected_chunks}
        if metadata is not None:
            _require(type(metadata) is ResponseMetadata)
            _require(state == "finished" or metadata.parsed_output_ref is None)
            payload["response"] = asdict(metadata)
        self._persist(
            actor,
            workflow,
            attempt,
            event,
            "provider_response",
            "terminal",
            lambda grant: _seal_observation(grant, attempt, event, "response", payload),
        )

    def committed(
        self,
        actor: str,
        workflow: str,
        attempt: CapturedAttempt,
        event: EventRef,
        commit_ref: str,
        *,
        decision_payload_ref: str,
    ) -> None:
        """Record app receipt provenance and its opaque, canonical payload identity."""
        require_ref(commit_ref)
        require_ref(decision_payload_ref)
        self._persist(
            actor,
            workflow,
            attempt,
            event,
            "application_commit",
            "commit",
            lambda _: {"commit_ref": commit_ref, "decision_payload_ref": decision_payload_ref},
        )

    def inspect(self, actor: str, workflow: str, operation_ref: str) -> dict[str, Any]:
        require_ref(operation_ref)
        custody = self.store.path.with_name("custody.sqlite3")
        _require(not os.path.lexists(custody))
        try:
            with _open_db(self.store.path) as db:
                _require(db is not None)
                assert db is not None
                self.store._check_owner(db)
                grant = self._current(db, actor, workflow, "inspect", None)
                sources = dict(grant.sources)
                permitted = [
                    (kind, sources[boundary])
                    for kind, boundary in _BOUNDARY.items()
                    if boundary in sources
                ]
                competing: set[tuple[str, int]] = set()
                if (
                    permitted
                    and db.execute(
                        "SELECT 1 FROM sqlite_master WHERE type='table' "
                        "AND name='decision_capture_streams'"
                    ).fetchone()
                ):
                    filters = ",".join("(?,?)" for _ in permitted)
                    rows = db.execute(
                        "SELECT attempt,source_ref,event_ref,event_revision,"
                        "observed_at,kind,payload,decision_ref,decision_revision "
                        "FROM decision_capture_streams WHERE "
                        "(tenant,epoch,workflow,grant_revision,policy_revision,operation_ref)"
                        f"=(?,?,?,?,?,?) AND (kind,source_ref) IN (VALUES {filters}) LIMIT ?",
                        (
                            grant.tenant,
                            grant.epoch,
                            grant.workflow,
                            grant.revision,
                            grant.policy_revision,
                            operation_ref,
                            *(value for pair in permitted for value in pair),
                            MAX_EVENTS + 1,
                        ),
                    ).fetchall()
                    _require(len(rows) <= MAX_EVENTS, "capture_operation_limit")
                    if "application_commit" in sources:
                        competing = _competing_revisions(
                            db, grant, sources["application_commit"], operation_ref
                        )
                else:
                    rows = []
                view = _view(rows, competing, grant)
                if "application_commit" not in sources:
                    view.update(committed_decisions=None, disputed_decisions=None)
                _require(self._current(db, actor, workflow, "inspect", None) == grant)
            _require(not os.path.lexists(custody))
            return view
        except CaptureError:
            raise
        except (OSError, sqlite3.Error, Refused, ValueError, KeyError, TypeError, OverflowError):
            raise CaptureError("decision_capture_unavailable") from None


def _competing_revisions(
    db: sqlite3.Connection, grant: CaptureGrant, source: str, operation_ref: str
) -> set[tuple[str, int]]:
    # The caller has permission to inspect only the requested operation. Return
    # a dispute flag for its revisions without returning competing references.
    rows = db.execute(
        "SELECT DISTINCT current.decision_ref,current.decision_revision "
        "FROM decision_capture_streams AS current "
        "WHERE (current.tenant,current.epoch,current.workflow,current.grant_revision,"
        "current.policy_revision,current.operation_ref,current.source_ref)=(?,?,?,?,?,?,?) "
        "AND current.kind='commit' AND EXISTS ("
        "SELECT 1 FROM decision_capture_streams AS competing "
        "WHERE (competing.tenant,competing.epoch,competing.workflow,competing.grant_revision,"
        "competing.policy_revision,competing.source_ref,competing.decision_ref,"
        "competing.decision_revision)=(current.tenant,current.epoch,current.workflow,"
        "current.grant_revision,current.policy_revision,current.source_ref,current.decision_ref,"
        "current.decision_revision) "
        "AND competing.kind='commit' AND competing.operation_ref<>current.operation_ref "
        "AND json_extract(competing.payload,'$.decision_payload_ref') "
        "IS NOT json_extract(current.payload,'$.decision_payload_ref'))",
        (
            grant.tenant,
            grant.epoch,
            grant.workflow,
            grant.revision,
            grant.policy_revision,
            operation_ref,
            source,
        ),
    )
    return {(decision, revision) for decision, revision in rows}


def _details(
    evidence: list[tuple[str, str, int]], grant: CaptureGrant, attempt: str
) -> dict[str, Any]:
    details: dict[str, Any] = {}
    for kind, boundary_field, schema in (
        ("start", "request", RequestMetadata),
        ("terminal", "response", ResponseMetadata),
    ):
        observations = {(body, when) for name, body, when in evidence if name == kind}
        if len(observations) != 1:
            continue
        body, observed_at = next(iter(observations))
        payload = json.loads(body)
        _require(isinstance(payload, dict))
        metadata = payload.get(boundary_field)
        if metadata is not None:
            _require(isinstance(metadata, dict))
            _require(type(observed_at) is int and 0 <= observed_at < 2**63)
            signature = payload.pop("metadata_seal", None)
            _require(
                isinstance(signature, str)
                and hmac.compare_digest(
                    signature,
                    _observation_seal(grant, attempt, observed_at, boundary_field, payload),
                )
            )
            details[boundary_field] = {"observed_at": observed_at, **asdict(schema(**metadata))}
    return details


def _view(
    rows: list[sqlite3.Row], competing: set[tuple[str, int]], grant: CaptureGrant
) -> dict[str, Any]:
    attempts: dict[str, list[tuple[str, str, int]]] = {}
    identities: dict[str, set[str]] = {}
    deliveries: dict[tuple[str, str, int], set[tuple[str, str, str, int]]] = {}
    commits: dict[tuple[str, int], set[tuple[str, str]]] = {}
    for attempt, source, event, revision, when, kind, body, decision_ref, decision_revision in rows:
        deliveries.setdefault((source, event, revision), set()).add((attempt, kind, body, when))
        attempts.setdefault(attempt, []).append((kind, body, when))
        identity = json.loads(attempt)
        _require(
            (identity["decision_ref"], identity["decision_revision"])
            == (decision_ref, decision_revision)
        )
        identities.setdefault(identity["attempt_ref"], set()).add(attempt)
        if kind == "commit":
            committed = json.loads(body)
            require_ref(committed["commit_ref"])
            commits.setdefault((decision_ref, decision_revision), set()).add(
                (attempt, require_ref(committed["decision_payload_ref"]))
            )
    conflicting = {
        attempt
        for alternatives in deliveries.values()
        if len(alternatives) > 1
        for attempt, _, _, _ in alternatives
    }
    conflicting_commits = {
        attempt
        for alternatives in deliveries.values()
        if len(alternatives) > 1
        for attempt, kind, _, _ in alternatives
        if kind == "commit"
    }
    result: list[dict[str, Any]] = []
    for reference, candidate_identities in sorted(identities.items()):
        if len(candidate_identities) != 1:
            conflicting_commits.update(candidate_identities)
            result.append(
                {
                    "attempt_ref": reference,
                    "state": "disputed",
                    "terminal_state": None,
                    "observed_chunks": None,
                    "missing_indices": None,
                    "exact_replay": "unavailable",
                }
            )
            continue
        attempt = next(iter(candidate_identities))
        evidence = attempts[attempt]
        chunks: dict[int, set[str]] = {}
        for kind, serialized, _ in evidence:
            if kind == "chunk":
                projected = json.loads(serialized)
                chunks.setdefault(projected["index"], set()).add(projected["opaque_chunk_ref"])
        starts = {encoded for kind, encoded, _ in evidence if kind == "start"}
        terminals = {encoded for kind, encoded, _ in evidence if kind == "terminal"}
        reported = json.loads(next(iter(terminals))) if len(terminals) == 1 else None
        needed = reported["expected_chunks"] if reported else None
        invalid = (
            attempt in conflicting
            or len(starts) > 1
            or len(terminals) > 1
            or any(len(versions) > 1 for versions in chunks.values())
            or (needed is not None and any(index >= needed for index in chunks))
        )
        gap = sorted(set(range(needed)) - chunks.keys()) if needed is not None else []
        state = (
            "disputed"
            if invalid
            else "incomplete"
            if not starts or gap or not reported or reported["state"] != "finished"
            else "finished"
        )
        result.append(
            {
                "attempt_ref": reference,
                "state": state,
                "terminal_state": reported["state"] if reported else None,
                "observed_chunks": len(chunks),
                "missing_indices": gap,
                "exact_replay": "unavailable",
                **({} if invalid else _details(evidence, grant, attempt)),
            }
        )
    current: dict[str, int] = {}
    for decision, revision in commits:
        current[decision] = max(revision, current.get(decision, revision))
    disputed = 0
    for identity in current.items():
        events = commits[identity]
        disputed += (
            identity in competing
            or len({payload_ref for _, payload_ref in events}) > 1
            or any(attempt in conflicting_commits for attempt, _ in events)
        )
    return {
        "attempts": result,
        "committed_decisions": len(current) - disputed,
        "disputed_decisions": disputed,
    }
