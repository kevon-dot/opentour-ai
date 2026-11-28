"""Reduce metadata-only decision capture without persisting request or response content.

The ledger records what each authorized boundary observed: the application's request,
the provider's streamed chunks, terminal marker and authenticated final body, the
application's committed decision, and records read from a pinned historical export.
Request projections, chunks, final bodies and historical content are reduced to keyed
references under the customer-held grant key before anything is written. Every row the
inspection recomputes from is sealed under the same key, so a copied or edited row
cannot fill a gap, invent a commit or change an observed reference. Integrity is
``locally_keyed``: the verifying worker holds the key; no issuer is authenticated.
"""

from __future__ import annotations

import hmac
import json
import os
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.slack_adapter import Refused
from opentour_cell.pilot.training import LocalTrainingStore

Boundary = Literal[
    "application_request", "provider_response", "application_commit", "historical_export"
]
Action = Literal["record", "inspect"]
Fidelity = Literal["observed", "reconstructed", "unavailable"]
ContentForm = Literal["content", "customer_local_pointer", "unavailable"]
BOUNDARIES: frozenset[str] = frozenset(
    {"application_request", "provider_response", "application_commit", "historical_export"}
)
MAX_CHUNKS = 1024
MAX_CHUNK_BYTES = 64 * 1024
MAX_PROJECTION_BYTES = 256 * 1024
MAX_PAGE_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 2 * MAX_CHUNKS + 32
MAX_QUARANTINE = 4096
MAX_RECEIPTS = 4096
MAX_IMPORTS = 4096
INTEGRITY = "locally_keyed"
_BOUNDARY: dict[str, Boundary] = {
    "start": "application_request",
    "chunk": "provider_response",
    "terminal": "provider_response",
    "final_body": "provider_response",
    "commit": "application_commit",
}
_SEALED_KINDS = frozenset({"chunk", "final_body", "commit"})
_SEALED_FIELDS = ("request", "response", "opaque_projection_ref")
# Closed vocabularies. A reason is a code, never a copied source value.
QUARANTINE_REASONS: frozenset[str] = frozenset(
    {
        "malformed_record",
        "duplicate_key",
        "oversized_record",
        "unsupported_record",
        "unattributable",
        "invalid_field",
        "future_observation",
        "boundary_mismatch",
        "capture_event_scope_changed",
        "capture_operation_limit",
    }
)
UNAVAILABLE_REASONS: frozenset[str] = frozenset(
    {
        "retention",
        "opt_out",
        "redaction",
        "opaque_vendor",
        "not_in_export",
        "unresolved_alias",
        "no_as_of_source",
        "ambiguous_as_of_source",
    }
)
SCALAR_COMPONENTS = (
    "committed_action",
    "policy",
    "template",
    "attachment",
    "requested_model",
    "served_model",
    "parsed_output",
)
CONTENT_COMPONENTS = ("request_projection", "response_output")
# An exact replay of the original call needs every component below as originally observed.
_REPLAY_SCALARS = ("policy", "template", "served_model")


class CaptureError(ValueError):
    """A scoped metadata diagnostic."""


def _require(condition: object, reason: str = "decision_capture_unavailable") -> None:
    if not condition:
        raise CaptureError(reason)


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value < 2**31


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
            _require(boundary in BOUNDARIES)
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


@dataclass(frozen=True)
class PageReceipt:
    """What an adapter admitted from one source page. Counts only; never source values."""

    source_ref: str
    collector_ref: str
    projection_revision: str
    cursor: str
    next_cursor: str | None
    admitted: int
    quarantined: int
    dropped_fields: int
    window_start: int | None
    window_end: int | None

    def __post_init__(self) -> None:
        for ref in (self.source_ref, self.collector_ref, self.projection_revision, self.cursor):
            require_ref(ref)
        if self.next_cursor is not None:
            require_ref(self.next_cursor)
        _require(all(_count(n) for n in (self.admitted, self.quarantined, self.dropped_fields)))
        window = (self.window_start, self.window_end)
        _require(
            window == (None, None)
            or (
                all(type(t) is int and 0 <= t < 2**63 for t in window)
                and self.window_start is not None
                and self.window_end is not None
                and self.window_start <= self.window_end
            )
        )


@dataclass(frozen=True)
class FieldEvidence:
    """One scalar component of a historical decision and how its value is known."""

    fidelity: Fidelity
    ref: str | None = None
    reason: str | None = None
    source_ref: str | None = None

    def __post_init__(self) -> None:
        if self.fidelity == "observed":
            require_ref(self.ref)
            _require(self.reason is None and self.source_ref is None)
        elif self.fidelity == "reconstructed":
            require_ref(self.ref)
            require_ref(self.source_ref)
            _require(self.reason is None)
        else:
            _require(self.fidelity == "unavailable")
            _require(self.ref is None and self.source_ref is None)
            _require(self.reason in UNAVAILABLE_REASONS)


@dataclass(frozen=True)
class ContentEvidence:
    """Historical content held as bytes, as a customer-local pointer digest, or not at all.

    Bytes and pointer digests exist only in memory; the ledger stores a keyed reference.
    """

    form: ContentForm
    content: bytes | None = field(default=None, repr=False)
    pointer_sha256: str | None = field(default=None, repr=False)
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.form == "content":
            _require(type(self.content) is bytes and len(self.content) <= MAX_PROJECTION_BYTES)
            _require(self.pointer_sha256 is None and self.reason is None)
        elif self.form == "customer_local_pointer":
            pointer = self.pointer_sha256
            _require(
                isinstance(pointer, str)
                and len(pointer) == 64
                and all(c in "0123456789abcdef" for c in pointer)
            )
            _require(self.content is None and self.reason is None)
        else:
            _require(self.form == "unavailable")
            _require(self.content is None and self.pointer_sha256 is None)
            _require(self.reason in UNAVAILABLE_REASONS)


@dataclass(frozen=True)
class HistoricalRecord:
    """One decision read from a pinned historical export, with per-field fidelity."""

    export_ref: str
    parser_revision: str
    record_ref: str
    recorded_at: int
    operation_ref: str
    case_ref: str
    family_ref: str
    decision_ref: str
    decision_revision: int
    decided_at: int
    retention: Literal["content", "customer_local_pointer", "metadata_only"]
    fields: tuple[tuple[str, FieldEvidence], ...]
    contents: tuple[tuple[str, ContentEvidence], ...]

    def __post_init__(self) -> None:
        for ref in (
            self.export_ref,
            self.parser_revision,
            self.record_ref,
            self.operation_ref,
            self.case_ref,
            self.family_ref,
            self.decision_ref,
        ):
            require_ref(ref)
        _require(type(self.decision_revision) is int and 0 < self.decision_revision < 2**31)
        _require(
            all(type(t) is int and 0 <= t < 2**63 for t in (self.recorded_at, self.decided_at))
        )
        _require(self.retention in {"content", "customer_local_pointer", "metadata_only"})
        _require(tuple(name for name, _ in self.fields) == SCALAR_COMPONENTS)
        _require(all(type(value) is FieldEvidence for _, value in self.fields))
        _require(tuple(name for name, _ in self.contents) == CONTENT_COMPONENTS)
        _require(all(type(value) is ContentEvidence for _, value in self.contents))
        # Metadata-only retention keeps no content, so no content can be presented as observed.
        _require(
            self.retention != "metadata_only"
            or all(value.form == "unavailable" for _, value in self.contents)
        )


@dataclass(frozen=True)
class ProjectionSpec:
    """How customer-held projection bytes were serialized at one boundary."""

    boundary: Literal["application_request", "provider_response"]
    serializer: Literal["canonical-json-v1", "raw-chunks-v1"]
    projection_revision: str
    components: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        require_ref(self.projection_revision)
        _require(
            (self.boundary, self.serializer)
            in {
                ("application_request", "canonical-json-v1"),
                ("provider_response", "raw-chunks-v1"),
            }
        )
        _require(
            type(self.components) is tuple
            and len(set(self.components)) == len(self.components) <= 64
            and all(type(name) is str and 0 < len(name) <= 64 for name in self.components)
            and (self.serializer == "canonical-json-v1") == bool(self.components)
        )


def _scope(grant: CaptureGrant) -> tuple[str, str, str, str, str]:
    return grant.tenant, grant.epoch, grant.workflow, grant.revision, grant.policy_revision


def _keyed(grant: CaptureGrant, identity: object, label: object, content: bytes) -> str:
    """Keyed reference to content under the current grant; never offline-checkable."""
    binding = canonical([*_scope(grant), identity, label]).encode()
    return hmac.digest(grant.opaque_key, binding + b"\x00" + content, "sha256").hex()


def _chunk_ref(grant: CaptureGrant, attempt: CapturedAttempt, index: int, data: bytes) -> str:
    return _keyed(grant, asdict(attempt), index, data)


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
    if not any(name in payload for name in _SEALED_FIELDS):
        return payload
    seal = _observation_seal(grant, canonical(asdict(attempt)), event.observed_at, field, payload)
    return payload | {"metadata_seal": seal}


def _row_seal(
    grant: CaptureGrant,
    boundary: Boundary,
    identity: str,
    event: tuple[str, str, int, int],
    kind: str,
    payload: object,
) -> str:
    """Bind a row to its boundary, source, event, identity and observation time."""
    source, event_ref, revision, observed_at = event
    subject = [
        "opentour.pilot.capture-observation.v1",
        *_scope(grant),
        boundary,
        source,
        identity,
        event_ref,
        revision,
        observed_at,
        kind,
        payload,
    ]
    return hmac.digest(grant.opaque_key, canonical(subject).encode(), "sha256").hex()


def _unsealed(
    grant: CaptureGrant,
    boundary: Boundary,
    identity: str,
    event: tuple[str, str, int, int],
    kind: str,
    body: str,
) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads(body)
    _require(isinstance(payload, dict))
    seal = payload.pop("seal", None)
    _require(
        isinstance(seal, str)
        and hmac.compare_digest(seal, _row_seal(grant, boundary, identity, event, kind, payload))
    )
    return payload


def _create_tables(db: sqlite3.Connection) -> None:
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
    db.execute(
        "CREATE TABLE IF NOT EXISTS decision_capture_quarantine("
        "tenant TEXT NOT NULL,epoch TEXT NOT NULL,workflow TEXT NOT NULL,"
        "grant_revision TEXT NOT NULL,policy_revision TEXT NOT NULL,"
        "boundary TEXT NOT NULL,source_ref TEXT NOT NULL,cursor TEXT NOT NULL,"
        "position INTEGER NOT NULL,reason TEXT NOT NULL,"
        "PRIMARY KEY(tenant,epoch,workflow,grant_revision,policy_revision,"
        "boundary,source_ref,cursor,position,reason))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS decision_capture_receipts("
        "tenant TEXT NOT NULL,epoch TEXT NOT NULL,workflow TEXT NOT NULL,"
        "grant_revision TEXT NOT NULL,policy_revision TEXT NOT NULL,"
        "boundary TEXT NOT NULL,source_ref TEXT NOT NULL,cursor TEXT NOT NULL,"
        "body TEXT NOT NULL,"
        "PRIMARY KEY(tenant,epoch,workflow,grant_revision,policy_revision,"
        "boundary,source_ref,cursor))"
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS decision_capture_imports("
        "tenant TEXT NOT NULL,epoch TEXT NOT NULL,workflow TEXT NOT NULL,"
        "grant_revision TEXT NOT NULL,policy_revision TEXT NOT NULL,"
        "source_ref TEXT NOT NULL,export_ref TEXT NOT NULL,record_ref TEXT NOT NULL,"
        "parser_revision TEXT NOT NULL,recorded_at INTEGER NOT NULL,"
        "identity TEXT NOT NULL,decision_ref TEXT NOT NULL,decision_revision INTEGER NOT NULL,"
        "evidence TEXT NOT NULL,seal TEXT NOT NULL,"
        "PRIMARY KEY(tenant,epoch,workflow,grant_revision,policy_revision,"
        "source_ref,export_ref,record_ref,parser_revision,identity,evidence))"
    )


def _has_table(db: sqlite3.Connection, name: str) -> bool:
    row = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


class CaptureStreams:
    """Journal observations from authorized application, provider and historical boundaries."""

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

    @contextmanager
    def _writing(
        self, actor: str, workflow: str, boundary: Boundary, source_ref: str
    ) -> Iterator[tuple[sqlite3.Connection, CaptureGrant]]:
        """One write transaction under the current grant, rechecked before it commits."""
        with self.store._db(write=True) as db:
            grant = self._current(db, actor, workflow, "record", boundary)
            _require(dict(grant.sources)[boundary] == source_ref)
            _create_tables(db)
            yield db, grant
            _require(self._current(db, actor, workflow, "record", boundary) == grant)
            _require(not os.path.lexists(self.store.path.with_name("custody.sqlite3")))

    @contextmanager
    def _reading(
        self, actor: str, workflow: str, action: Action, boundary: Boundary | None
    ) -> Iterator[tuple[sqlite3.Connection, CaptureGrant]]:
        """A read-only snapshot under the current grant, rechecked before results leave."""
        custody = self.store.path.with_name("custody.sqlite3")
        _require(not os.path.lexists(custody))
        try:
            with _open_db(self.store.path) as db:
                _require(db is not None)
                assert db is not None
                self.store._check_owner(db)
                grant = self._current(db, actor, workflow, action, boundary)
                yield db, grant
                _require(self._current(db, actor, workflow, action, boundary) == grant)
            _require(not os.path.lexists(custody))
        except CaptureError:
            raise
        except (OSError, sqlite3.Error, Refused, ValueError, KeyError, TypeError, OverflowError):
            raise CaptureError("decision_capture_unavailable") from None

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
        with self._writing(actor, workflow, boundary, event.source_ref) as (db, grant):
            identity = canonical(asdict(attempt))
            body = project(grant)
            if kind in _SEALED_KINDS:
                where = (event.source_ref, event.event_ref, event.event_revision, event.observed_at)
                seal = _row_seal(grant, boundary, identity, where, kind, body)
                body = body | {"seal": seal}
            payload = canonical(body)
            scope = _scope(grant)
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

    def started(
        self,
        actor: str,
        workflow: str,
        attempt: CapturedAttempt,
        event: EventRef,
        *,
        parent_attempt: str | None = None,
        metadata: RequestMetadata | None = None,
        request_projection: bytes | None = None,
    ) -> None:
        """Record the invocation. A supplied projection is reduced to a keyed reference."""
        if parent_attempt is not None:
            require_ref(parent_attempt)
            _require(parent_attempt != attempt.attempt_ref)
        payload: dict[str, Any] = {"parent_attempt": parent_attempt}
        if metadata is not None:
            _require(type(metadata) is RequestMetadata)
            payload["request"] = asdict(metadata)
        if request_projection is not None:
            _require(
                type(request_projection) is bytes
                and len(request_projection) <= MAX_PROJECTION_BYTES
            )

        def project(grant: CaptureGrant) -> dict[str, Any]:
            body = dict(payload)
            if request_projection is not None:
                body["opaque_projection_ref"] = _keyed(
                    grant, asdict(attempt), "request-projection", request_projection
                )
            return _seal_observation(grant, attempt, event, "request", body)

        self._persist(actor, workflow, attempt, event, "application_request", "start", project)

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
            opaque_ref = _chunk_ref(grant, attempt, index, observed_bytes)
            return {"index": index, "opaque_chunk_ref": opaque_ref}

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

    def final_body(
        self,
        actor: str,
        workflow: str,
        attempt: CapturedAttempt,
        event: EventRef,
        body: bytes,
        chunk_lengths: tuple[int, ...],
    ) -> None:
        """Record a complete final body delivered by the authorized response source.

        Only keyed references to each declared chunk slice are kept. Inspection accepts the
        body as completing the stream only when every chunk that was observed separately has
        the identical reference and the finished terminal declares the same count.
        """
        _require(
            type(body) is bytes
            and len(body) <= MAX_CHUNKS * MAX_CHUNK_BYTES
            and type(chunk_lengths) is tuple
            and len(chunk_lengths) <= MAX_CHUNKS
            and all(type(n) is int and 0 <= n <= MAX_CHUNK_BYTES for n in chunk_lengths)
            and sum(chunk_lengths) == len(body)
        )

        def project(grant: CaptureGrant) -> dict[str, Any]:
            refs: list[str] = []
            offset = 0
            for index, length in enumerate(chunk_lengths):
                refs.append(_chunk_ref(grant, attempt, index, body[offset : offset + length]))
                offset += length
            return {"chunk_refs": refs}

        self._persist(actor, workflow, attempt, event, "provider_response", "final_body", project)

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

    def quarantined(
        self,
        actor: str,
        workflow: str,
        boundary: Boundary,
        source_ref: str,
        cursor: str,
        position: int,
        reason: str,
    ) -> None:
        """Dead-letter one source record by position and reason code only."""
        require_ref(source_ref)
        require_ref(cursor)
        _require(_count(position) and reason in QUARANTINE_REASONS)
        with self._writing(actor, workflow, boundary, source_ref) as (db, grant):
            key = (*_scope(grant), boundary, source_ref, cursor)
            db.execute(
                "INSERT OR IGNORE INTO decision_capture_quarantine VALUES(?,?,?,?,?,?,?,?,?,?)",
                (*key, position, reason),
            )
            count = db.execute(
                "SELECT count(*) FROM decision_capture_quarantine WHERE "
                "(tenant,epoch,workflow,grant_revision,policy_revision,boundary,source_ref,"
                "cursor)=(?,?,?,?,?,?,?,?)",
                key,
            ).fetchone()[0]
            _require(count <= MAX_QUARANTINE, "capture_operation_limit")

    def page_status(
        self,
        actor: str,
        workflow: str,
        boundary: Boundary,
        source_ref: str,
        cursor: str,
        page: bytes,
    ) -> Literal["new", "acknowledged"]:
        """Whether this exact page was already acknowledged. A changed page is refused."""
        require_ref(source_ref)
        require_ref(cursor)
        _require(type(page) is bytes and len(page) <= MAX_PAGE_BYTES)
        with self._reading(actor, workflow, "record", boundary) as (db, grant):
            _require(dict(grant.sources)[boundary] == source_ref)
            if not _has_table(db, "decision_capture_receipts"):
                return "new"
            row = db.execute(
                "SELECT body FROM decision_capture_receipts WHERE "
                "(tenant,epoch,workflow,grant_revision,policy_revision,boundary,source_ref,"
                "cursor)=(?,?,?,?,?,?,?,?)",
                (*_scope(grant), boundary, source_ref, cursor),
            ).fetchone()
            if row is None:
                return "new"
            stored = json.loads(row[0])
            page_ref = _keyed(grant, [source_ref, cursor], "capture-page", page)
            _require(hmac.compare_digest(stored["page_ref"], page_ref), "capture_cursor_conflict")
            return "acknowledged"

    def acknowledge(
        self, actor: str, workflow: str, boundary: Boundary, receipt: PageReceipt, page: bytes
    ) -> dict[str, Any]:
        """Acknowledge a processed page with a keyed page reference and its counts."""
        _require(type(receipt) is PageReceipt)
        _require(type(page) is bytes and len(page) <= MAX_PAGE_BYTES)
        with self._writing(actor, workflow, boundary, receipt.source_ref) as (db, grant):
            page_ref = _keyed(grant, [receipt.source_ref, receipt.cursor], "capture-page", page)
            body = asdict(receipt) | {"boundary": boundary, "page_ref": page_ref}
            key = (*_scope(grant), boundary, receipt.source_ref, receipt.cursor)
            row = db.execute(
                "SELECT body FROM decision_capture_receipts WHERE "
                "(tenant,epoch,workflow,grant_revision,policy_revision,boundary,source_ref,"
                "cursor)=(?,?,?,?,?,?,?,?)",
                key,
            ).fetchone()
            if row is None:
                count = db.execute(
                    "SELECT count(*) FROM decision_capture_receipts WHERE "
                    "(tenant,epoch,workflow,grant_revision,policy_revision)=(?,?,?,?,?)",
                    _scope(grant),
                ).fetchone()[0]
                _require(count < MAX_RECEIPTS, "capture_operation_limit")
                db.execute(
                    "INSERT INTO decision_capture_receipts VALUES(?,?,?,?,?,?,?,?,?)",
                    (*key, canonical(body)),
                )
            else:
                _require(row[0] == canonical(body), "capture_cursor_conflict")
        return body

    def last_receipt(
        self, actor: str, workflow: str, boundary: Boundary, source_ref: str
    ) -> dict[str, Any] | None:
        """The most recently acknowledged page for a source, for a restarted collector."""
        require_ref(source_ref)
        with self._reading(actor, workflow, "record", boundary) as (db, grant):
            _require(dict(grant.sources)[boundary] == source_ref)
            if not _has_table(db, "decision_capture_receipts"):
                return None
            row = db.execute(
                "SELECT body FROM decision_capture_receipts WHERE "
                "(tenant,epoch,workflow,grant_revision,policy_revision,boundary,source_ref)"
                "=(?,?,?,?,?,?,?) ORDER BY rowid DESC LIMIT 1",
                (*_scope(grant), boundary, source_ref),
            ).fetchone()
            return None if row is None else dict(json.loads(row[0]))

    def receipts(self, actor: str, workflow: str) -> dict[str, Any]:
        """Per-boundary page receipts and dead-letter reason counts for a scoped inspector."""
        with self._reading(actor, workflow, "inspect", None) as (db, grant):
            view: dict[str, Any] = {}
            for boundary, source in sorted(dict(grant.sources).items()):
                pages: list[dict[str, Any]] = []
                reasons: dict[str, int] = {}
                if _has_table(db, "decision_capture_receipts"):
                    rows = db.execute(
                        "SELECT body FROM decision_capture_receipts WHERE "
                        "(tenant,epoch,workflow,grant_revision,policy_revision,boundary,"
                        "source_ref)=(?,?,?,?,?,?,?) ORDER BY rowid LIMIT ?",
                        (*_scope(grant), boundary, source, MAX_RECEIPTS + 1),
                    ).fetchall()
                    _require(len(rows) <= MAX_RECEIPTS, "capture_operation_limit")
                    for (body,) in rows:
                        page = json.loads(body)
                        page.pop("page_ref", None)
                        pages.append(page)
                    for reason, count in db.execute(
                        "SELECT reason,count(*) FROM decision_capture_quarantine WHERE "
                        "(tenant,epoch,workflow,grant_revision,policy_revision,boundary,"
                        "source_ref)=(?,?,?,?,?,?,?) GROUP BY reason",
                        (*_scope(grant), boundary, source),
                    ):
                        reasons[str(reason)] = int(count)
                view[boundary] = {"pages": pages, "quarantined": dict(sorted(reasons.items()))}
            return view

    def imported(
        self, actor: str, workflow: str, source_ref: str, record: HistoricalRecord
    ) -> None:
        """Record one historical decision. Its as-of evidence comes only from the export."""
        _require(type(record) is HistoricalRecord and record.recorded_at <= self.clock())
        with self._writing(actor, workflow, "historical_export", source_ref) as (db, grant):
            identity = canonical(
                {
                    "operation_ref": record.operation_ref,
                    "case_ref": record.case_ref,
                    "family_ref": record.family_ref,
                    "decision_ref": record.decision_ref,
                    "decision_revision": record.decision_revision,
                }
            )
            contents: dict[str, dict[str, Any]] = {}
            for name, item in record.contents:
                opaque: str | None = None
                if item.form == "content":
                    assert item.content is not None
                    opaque = _keyed(grant, identity, ["historical", name, "content"], item.content)
                elif item.form == "customer_local_pointer":
                    assert item.pointer_sha256 is not None
                    pointer = item.pointer_sha256.encode()
                    opaque = _keyed(grant, identity, ["historical", name, "pointer"], pointer)
                contents[name] = {"form": item.form, "opaque_ref": opaque, "reason": item.reason}
            evidence = canonical(
                {
                    "decided_at": record.decided_at,
                    "retention": record.retention,
                    "fields": {name: asdict(value) for name, value in record.fields},
                    "contents": contents,
                }
            )
            row = (
                source_ref,
                record.export_ref,
                record.record_ref,
                record.parser_revision,
                record.recorded_at,
                identity,
                record.decision_ref,
                record.decision_revision,
                evidence,
            )
            seal = _import_seal(grant, row)
            db.execute(
                "INSERT OR IGNORE INTO decision_capture_imports "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*_scope(grant), *row, seal),
            )
            count = db.execute(
                "SELECT count(*) FROM decision_capture_imports WHERE "
                "(tenant,epoch,workflow,grant_revision,policy_revision,source_ref)"
                "=(?,?,?,?,?,?)",
                (*_scope(grant), source_ref),
            ).fetchone()[0]
            _require(count <= MAX_IMPORTS, "capture_operation_limit")

    def inspect_imports(self, actor: str, workflow: str) -> dict[str, Any]:
        """Historical decisions with field fidelity and the claims each one can support."""
        with self._reading(actor, workflow, "inspect", None) as (db, grant):
            sources = dict(grant.sources)
            if "historical_export" not in sources:
                return {
                    "decisions": None,
                    "observed_decisions": None,
                    "disputed_decisions": None,
                    "eligible_for_replay": None,
                    "exact_replay": "unavailable",
                }
            rows: list[Any] = []
            if _has_table(db, "decision_capture_imports"):
                rows = db.execute(
                    "SELECT source_ref,export_ref,record_ref,parser_revision,recorded_at,"
                    "identity,decision_ref,decision_revision,evidence,seal "
                    "FROM decision_capture_imports WHERE "
                    "(tenant,epoch,workflow,grant_revision,policy_revision,source_ref)"
                    "=(?,?,?,?,?,?) LIMIT ?",
                    (*_scope(grant), sources["historical_export"], MAX_IMPORTS + 1),
                ).fetchall()
                _require(len(rows) <= MAX_IMPORTS, "capture_operation_limit")
            return _import_view(rows, grant)

    def inspect(self, actor: str, workflow: str, operation_ref: str) -> dict[str, Any]:
        require_ref(operation_ref)
        with self._reading(actor, workflow, "inspect", None) as (db, grant):
            sources = dict(grant.sources)
            rows, competing = self._operation_rows(db, grant, operation_ref)
            view = _view(rows, competing, grant, set(sources))
            if "application_commit" not in sources:
                view.update(committed_decisions=None, disputed_decisions=None)
            return view

    def _operation_rows(
        self, db: sqlite3.Connection, grant: CaptureGrant, operation_ref: str
    ) -> tuple[list[Any], set[tuple[str, int]]]:
        sources = dict(grant.sources)
        permitted = [
            (kind, sources[boundary]) for kind, boundary in _BOUNDARY.items() if boundary in sources
        ]
        if not permitted or not _has_table(db, "decision_capture_streams"):
            return [], set()
        filters = ",".join("(?,?)" for _ in permitted)
        rows = db.execute(
            "SELECT attempt,source_ref,event_ref,event_revision,"
            "observed_at,kind,payload,decision_ref,decision_revision "
            "FROM decision_capture_streams WHERE "
            "(tenant,epoch,workflow,grant_revision,policy_revision,operation_ref)"
            f"=(?,?,?,?,?,?) AND (kind,source_ref) IN (VALUES {filters}) LIMIT ?",
            (
                *_scope(grant),
                operation_ref,
                *(value for pair in permitted for value in pair),
                MAX_EVENTS + 1,
            ),
        ).fetchall()
        _require(len(rows) <= MAX_EVENTS, "capture_operation_limit")
        competing: set[tuple[str, int]] = set()
        if "application_commit" in sources:
            competing = _competing_revisions(
                db, grant, sources["application_commit"], operation_ref
            )
        return rows, competing

    def verify_projection(
        self,
        actor: str,
        workflow: str,
        operation_ref: str,
        attempt_ref: str,
        spec: ProjectionSpec,
        saved: bytes | tuple[bytes, ...],
    ) -> dict[str, Any]:
        """Compare customer-held projection bytes with what the boundary observed.

        Exact replay is verified only when the bytes are the unique serialization of a
        projection with exactly the declared components and their keyed references equal
        the references recorded at observation time. Bytes cannot fill an unobserved chunk.
        """
        require_ref(operation_ref)
        require_ref(attempt_ref)
        _require(type(spec) is ProjectionSpec)
        with self._reading(actor, workflow, "inspect", None) as (db, grant):
            _require(spec.boundary in dict(grant.sources))
            rows, competing = self._operation_rows(db, grant, operation_ref)
            view = _view(rows, competing, grant, set(dict(grant.sources)))
            entries = [item for item in view["attempts"] if item["attempt_ref"] == attempt_ref]
            _require(len(entries) == 1)
            identities = {
                row[0] for row in rows if json.loads(row[0])["attempt_ref"] == attempt_ref
            }
            if entries[0]["state"] == "disputed" or len(identities) != 1:
                return _replay_verdict(spec, "disputed_attempt")
            identity = next(iter(identities))
            evidence = [(row[5], row[6]) for row in rows if row[0] == identity]
            if spec.boundary == "application_request":
                reason = _request_mismatch(grant, identity, evidence, spec, saved)
            else:
                reason = _response_mismatch(grant, identity, evidence, entries[0], saved)
            return _replay_verdict(spec, reason)


def _replay_verdict(spec: ProjectionSpec, reason: str | None) -> dict[str, Any]:
    return {
        "boundary": spec.boundary,
        "serializer": spec.serializer,
        "projection_revision": spec.projection_revision,
        "exact_replay": "verified" if reason is None else "unavailable",
        "reason": reason,
        "integrity": INTEGRITY,
        "issuer_authenticated": False,
    }


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise CaptureError("normalization_collision")
    return dict(pairs)


def _reject_constant(_: str) -> None:
    raise CaptureError("normalization_collision")


def _request_mismatch(
    grant: CaptureGrant,
    identity: str,
    evidence: list[tuple[str, str]],
    spec: ProjectionSpec,
    saved: bytes | tuple[bytes, ...],
) -> str | None:
    starts = {body for kind, body in evidence if kind == "start"}
    if len(starts) != 1:
        return "request_not_observed"
    payload = json.loads(next(iter(starts)))
    recorded = payload.get("opaque_projection_ref")
    metadata = payload.get("request")
    if not isinstance(recorded, str):
        return "request_not_fingerprinted"
    if not isinstance(metadata, dict) or metadata.get("projection_revision") != (
        spec.projection_revision
    ):
        return "projection_revision_mismatch"
    if type(saved) is not bytes or len(saved) > MAX_PROJECTION_BYTES:
        return "missing_component"
    value: object = None
    collision = False
    try:
        value = json.loads(
            saved.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_constant,
        )
    except CaptureError:
        collision = True
    except ValueError:
        return "unparseable_projection"
    if collision:
        return "normalization_collision"
    if not isinstance(value, dict):
        return "unparseable_projection"
    if not set(spec.components) <= set(value):
        return "missing_component"
    if set(value) != set(spec.components):
        return "undeclared_component"
    serialized = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    if serialized != saved:
        return "normalization_collision"
    attempt = json.loads(identity)
    expected = _keyed(grant, attempt, "request-projection", saved)
    return None if hmac.compare_digest(expected, recorded) else "fingerprint_mismatch"


def _response_mismatch(
    grant: CaptureGrant,
    identity: str,
    evidence: list[tuple[str, str]],
    entry: dict[str, Any],
    saved: bytes | tuple[bytes, ...],
) -> str | None:
    if not entry["exact_stream"]:
        return "incomplete_stream"
    observed: dict[int, str] = {}
    derived: list[str] = []
    for kind, body in evidence:
        if kind == "chunk":
            chunk = json.loads(body)
            observed[chunk["index"]] = chunk["opaque_chunk_ref"]
        elif kind == "final_body":
            derived = list(json.loads(body)["chunk_refs"])
    expected_count = entry["observed_chunks"] + len(entry["missing_indices"])
    if type(saved) is not tuple or len(saved) != expected_count:
        return "missing_component"
    attempt = CapturedAttempt(**json.loads(identity))
    for index, data in enumerate(saved):
        if type(data) is not bytes or len(data) > MAX_CHUNK_BYTES:
            return "missing_component"
        recorded = observed.get(index) or (derived[index] if index < len(derived) else None)
        if recorded is None:
            return "missing_component"
        if not hmac.compare_digest(recorded, _chunk_ref(grant, attempt, index, data)):
            return "fingerprint_mismatch"
    return None


def _import_seal(grant: CaptureGrant, row: tuple[Any, ...]) -> str:
    subject = ["opentour.pilot.capture-import.v1", *_scope(grant), *row]
    return hmac.digest(grant.opaque_key, canonical(subject).encode(), "sha256").hex()


def _import_view(rows: list[Any], grant: CaptureGrant) -> dict[str, Any]:
    by_decision: dict[tuple[str, int], set[tuple[str, str]]] = {}
    exports: dict[tuple[str, int], set[str]] = {}
    for row in rows:
        source, export, record, parser, recorded, identity, decision, revision, evidence, seal = row
        expected = _import_seal(
            grant,
            (source, export, record, parser, recorded, identity, decision, revision, evidence),
        )
        _require(isinstance(seal, str) and hmac.compare_digest(seal, expected))
        by_decision.setdefault((decision, revision), set()).add((identity, evidence))
        exports.setdefault((decision, revision), set()).add(export)
    current: dict[str, int] = {}
    for decision, revision in by_decision:
        current[decision] = max(revision, current.get(decision, revision))
    decisions: list[dict[str, Any]] = []
    observed = disputed = eligible = 0
    for (decision, revision), versions in sorted(by_decision.items()):
        entry: dict[str, Any] = {
            "decision_ref": decision,
            "decision_revision": revision,
            "current": current[decision] == revision,
            "exports": len(exports[(decision, revision)]),
        }
        if len(versions) != 1:
            entry.update(state="disputed", fields=None, contents=None, eligibility=None)
            disputed += entry["current"]
            decisions.append(entry)
            continue
        identity, body = next(iter(versions))
        known = json.loads(body)
        fields = known["fields"]
        contents = {
            name: {
                "form": item["form"],
                "fingerprint": "recorded" if item["opaque_ref"] else "absent",
                "reason": item["reason"],
            }
            for name, item in known["contents"].items()
        }
        missing = sorted(
            [name for name in _REPLAY_SCALARS if fields[name]["fidelity"] != "observed"]
            + [name for name, item in contents.items() if item["form"] == "unavailable"]
        )
        agreement_ready = fields["committed_action"]["fidelity"] == "observed"
        eligibility = {
            "observed_agreement": "eligible" if agreement_ready else "unavailable",
            "exact_model_replay": "requires_projection_verification"
            if not missing
            else ("unavailable"),
            "exact_model_replay_missing": missing,
            # Capture never carries a training purpose; training needs its own grant.
            "exact_target_training": "unavailable",
        }
        entry.update(
            json.loads(identity),
            state="imported",
            retention=known["retention"],
            decided_at=known["decided_at"],
            fields=fields,
            contents=contents,
            eligibility=eligibility,
        )
        if entry["current"]:
            observed += agreement_ready
            eligible += not missing
        decisions.append(entry)
    return {
        "decisions": decisions,
        "observed_decisions": observed,
        "disputed_decisions": disputed,
        "eligible_for_replay": eligible,
        "exact_replay": "unavailable",
    }


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
        (*_scope(grant), operation_ref, source),
    )
    return {(decision, revision) for decision, revision in rows}


def _details(
    evidence: list[tuple[str, str, int]], grant: CaptureGrant, attempt: str
) -> tuple[dict[str, Any], str | None]:
    details: dict[str, Any] = {}
    projection: str | None = None
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
        _require(type(observed_at) is int and 0 <= observed_at < 2**63)
        if "metadata_seal" in payload or any(name in payload for name in _SEALED_FIELDS):
            signature = payload.pop("metadata_seal", None)
            _require(
                isinstance(signature, str)
                and hmac.compare_digest(
                    signature,
                    _observation_seal(grant, attempt, observed_at, boundary_field, payload),
                )
            )
        if kind == "start":
            projection = "fingerprinted" if "opaque_projection_ref" in payload else "not_recorded"
        metadata = payload.get(boundary_field)
        if metadata is not None:
            _require(isinstance(metadata, dict))
            details[boundary_field] = {"observed_at": observed_at, **asdict(schema(**metadata))}
    return details, projection


def _view(
    rows: list[sqlite3.Row],
    competing: set[tuple[str, int]],
    grant: CaptureGrant,
    permitted: set[str],
) -> dict[str, Any]:
    attempts: dict[str, list[tuple[str, str, int]]] = {}
    identities: dict[str, set[str]] = {}
    deliveries: dict[tuple[str, str, int], set[tuple[str, str, str, int]]] = {}
    commits: dict[tuple[str, int], set[tuple[str, str]]] = {}
    for attempt, source, event, revision, when, kind, body, decision_ref, decision_revision in rows:
        _require(kind in _BOUNDARY)
        if kind in _SEALED_KINDS:
            where = (source, event, revision, when)
            _unsealed(grant, _BOUNDARY[kind], attempt, where, kind, body)
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
                    "decision_ref": None,
                    "decision_revision": None,
                    "state": "disputed",
                    "terminal_state": None,
                    "observed_chunks": None,
                    "received_indices": None,
                    "missing_indices": None,
                    "terminal_seen": None,
                    "exact_stream": False,
                    "completed_by": None,
                    "request_projection": None,
                    "committed": None,
                    "exact_replay": "unavailable",
                }
            )
            continue
        attempt = next(iter(candidate_identities))
        identity = json.loads(attempt)
        evidence = attempts[attempt]
        chunks: dict[int, set[str]] = {}
        bodies: set[tuple[str, ...]] = set()
        for kind, serialized, _ in evidence:
            if kind == "chunk":
                projected = json.loads(serialized)
                chunks.setdefault(projected["index"], set()).add(projected["opaque_chunk_ref"])
            elif kind == "final_body":
                bodies.add(tuple(json.loads(serialized)["chunk_refs"]))
        starts = {encoded for kind, encoded, _ in evidence if kind == "start"}
        terminals = {encoded for kind, encoded, _ in evidence if kind == "terminal"}
        reported = json.loads(next(iter(terminals))) if len(terminals) == 1 else None
        needed = reported["expected_chunks"] if reported else None
        body = next(iter(bodies)) if len(bodies) == 1 else None
        body_conflict = len(bodies) > 1 or (
            body is not None
            and (
                (reported is not None and (reported["state"], needed) != ("finished", len(body)))
                or any(
                    index >= len(body) or versions != {body[index]}
                    for index, versions in chunks.items()
                )
            )
        )
        invalid = (
            attempt in conflicting
            or len(starts) > 1
            or len(terminals) > 1
            or any(len(versions) > 1 for versions in chunks.values())
            or (needed is not None and any(index >= needed for index in chunks))
            or body_conflict
        )
        gap = sorted(set(range(needed)) - chunks.keys()) if needed is not None else []
        exact_stream = (
            not invalid
            and reported is not None
            and reported["state"] == "finished"
            and (not gap or body is not None)
        )
        state = "disputed" if invalid else "finished" if starts and exact_stream else "incomplete"
        details, projection = ({}, None) if invalid else _details(evidence, grant, attempt)
        result.append(
            {
                "attempt_ref": reference,
                "decision_ref": identity["decision_ref"],
                "decision_revision": identity["decision_revision"],
                "state": state,
                "terminal_state": reported["state"] if reported else None,
                "observed_chunks": len(chunks),
                "received_indices": sorted(chunks),
                "missing_indices": gap,
                "terminal_seen": bool(terminals),
                "exact_stream": exact_stream,
                "completed_by": None if not exact_stream else "final_body" if gap else "chunks",
                "request_projection": projection if "application_request" in permitted else None,
                "committed": (
                    any(kind == "commit" for kind, _, _ in evidence)
                    if "application_commit" in permitted
                    else None
                ),
                "exact_replay": "unavailable",
                **details,
            }
        )
    current: dict[str, int] = {}
    for decision, revision in commits:
        current[decision] = max(revision, current.get(decision, revision))
    disputed = 0
    for key in current.items():
        events = commits[key]
        disputed += (
            key in competing
            or len({payload_ref for _, payload_ref in events}) > 1
            or any(attempt in conflicting_commits for attempt, _ in events)
        )
    return {
        "attempts": result,
        "committed_decisions": len(current) - disputed,
        "disputed_decisions": disputed,
    }
