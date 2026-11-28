"""Read customer capture sources into the metadata-only decision capture ledger.

Three input methods feed `CaptureStreams`: the application's own instrumentation calls the
ledger directly; `CaptureSourceAdapter` reads the customer's authorized gateway/provider
request log and its application decision journal; `HistoricalImport` reads a pinned
historical export. Each adapter applies a field allowlist before anything is persisted,
lets the ledger reduce content to keyed references, dead-letters an unusable record by
position and reason code only, and never calls a provider or starts a fit. A dry run
validates the same bytes with no grant, store or key and writes nothing.

Scoring helpers keep the provider boundary and the application boundary in separate runs.
Independent truth comes from the OPE-514 live reducer's verified roots.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, cast

from opentour_cell.pilot.capture_streams import (
    CONTENT_COMPONENTS,
    INTEGRITY,
    MAX_CHUNK_BYTES,
    MAX_CHUNKS,
    MAX_PAGE_BYTES,
    MAX_PROJECTION_BYTES,
    SCALAR_COMPONENTS,
    UNAVAILABLE_REASONS,
    Boundary,
    CapturedAttempt,
    CaptureError,
    CaptureStreams,
    ContentEvidence,
    EventRef,
    FieldEvidence,
    HistoricalRecord,
    PageReceipt,
    RequestMetadata,
    ResponseMetadata,
)
from opentour_cell.pilot.intake_mcp import REF_PATTERN
from opentour_cell.pilot.live_evidence import VerifiedRoot

_LOG = logging.getLogger(__name__)

GATEWAY_SCHEMA = "opentour.capture.gateway-log.v1"
JOURNAL_SCHEMA = "opentour.capture.decision-journal.v1"
HISTORICAL_SCHEMA = "opentour.capture.historical-export.v1"
ARCHIVE_SCHEMA = "opentour.capture.as-of-archive.v1"
MANIFEST_SCHEMA = "opentour.capture.sampling-manifest.v1"
GATEWAY_PROJECTION = "opentour.capture.gateway-projection.v1"
JOURNAL_PROJECTION = "opentour.capture.journal-projection.v1"
HISTORICAL_PARSER = "opentour.capture.historical-parser.v1"
MAX_RECORD_BYTES = 8 * 1024 * 1024
MAX_RECORDS = 4096
MAX_MANIFEST_OPERATIONS = 100_000
# Ledger refusals that concern one record. Every other refusal stops the page unacknowledged.
_RECORD_REFUSALS = frozenset({"capture_event_scope_changed", "capture_operation_limit"})
_PAGE_REFUSALS = frozenset(
    {
        "decision_capture_disabled",
        "decision_capture_unavailable",
        "capture_cursor_conflict",
        "capture_operation_limit",
        "export_pin_mismatch",
        "archive_invalid",
    }
)
_IDENTITY = ("operation_id", "attempt_id", "case_id", "family_id", "decision_id")
_ENVELOPE = frozenset(
    {"schema", "record", "event_id", "event_revision", "observed_at", *_IDENTITY}
    | {"decision_revision"}
)
_KIND_FIELDS: dict[str, frozenset[str]] = {
    "request": frozenset(
        {
            "parent_attempt_id",
            "requested_model",
            "projection_revision",
            "template_revision",
            "request_projection_b64",
        }
    ),
    "chunk": frozenset({"index", "data_b64"}),
    "terminal": frozenset(
        {
            "state",
            "expected_chunks",
            "provider_receipt",
            "served_model",
            "parsed_output",
            "parser_revision",
        }
    ),
    "final_body": frozenset({"body_b64", "chunk_lengths"}),
    "commit": frozenset({"commit_ref", "decision_payload_ref"}),
}
_KIND_BOUNDARY: dict[str, Boundary] = {
    "request": "application_request",
    "chunk": "provider_response",
    "terminal": "provider_response",
    "final_body": "provider_response",
    "commit": "application_commit",
}
_SCHEMA_KINDS: dict[str, frozenset[str]] = {
    GATEWAY_SCHEMA: frozenset({"request", "chunk", "terminal", "final_body"}),
    JOURNAL_SCHEMA: frozenset({"commit"}),
}
_HISTORICAL_FIELDS = frozenset(
    {
        "schema",
        "record",
        "record_id",
        "recorded_at",
        "operation_id",
        "case_id",
        "family_id",
        "decision_id",
        "decision_revision",
        "decided_at",
        "retention",
        "components",
    }
)
# Revision references an as-of archive may supply. Content, labels and model identity never.
_RECONSTRUCTABLE = ("policy", "template", "attachment")
_RECONSTRUCT_FROM = frozenset({"not_in_export", "retention"})
_REF = re.compile(REF_PATTERN)
_HEX64 = re.compile(r"[0-9a-f]{64}")


def _require(condition: object, reason: str = "decision_capture_unavailable") -> None:
    if not condition:
        raise CaptureError(reason)


def _is_ref(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 128 and _REF.fullmatch(value) is not None


def _is_int(value: object, low: int = 0, high: int = 2**31) -> bool:
    return type(value) is int and low <= value < high


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _DuplicateKey(Exception):
    pass


class _Constant(Exception):
    pass


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    keys = [key for key, _ in pairs]
    if len(keys) != len(set(keys)):
        raise _DuplicateKey
    return dict(pairs)


def _constant(_: str) -> None:
    raise _Constant


def _parse(line: bytes) -> dict[str, Any] | str:
    """One strict JSON object, or a reason code. No source text can reach an exception."""
    if len(line) > MAX_RECORD_BYTES:
        return "oversized_record"
    outcome: dict[str, Any] | str = "malformed_record"
    try:
        value = json.loads(line.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
    except _DuplicateKey:
        outcome = "duplicate_key"
    except (_Constant, ValueError, RecursionError):
        outcome = "malformed_record"
    else:
        if isinstance(value, dict):
            outcome = value
    return outcome


def _lines(data: bytes) -> Iterator[tuple[int, bytes]]:
    for position, line in enumerate(data.split(b"\n")):
        if line.strip():
            yield position, line


def _b64(value: object, limit: int) -> bytes | None:
    if not isinstance(value, str) or len(value) > 4 * ((limit + 2) // 3):
        return None
    decoded: bytes | None = None
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (ValueError, binascii.Error):
        decoded = None
    return decoded if decoded is not None and len(decoded) <= limit else None


@dataclass(frozen=True)
class SourcePage:
    """One page an authenticated collector read. The worker, not the page, names the source."""

    source_ref: str
    collector_ref: str
    cursor: str
    next_cursor: str | None
    window_start: int | None
    window_end: int | None
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        for ref in (self.source_ref, self.collector_ref, self.cursor):
            _require(_is_ref(ref))
        _require(self.next_cursor is None or _is_ref(self.next_cursor))
        _require(type(self.data) is bytes and len(self.data) <= MAX_PAGE_BYTES)


@dataclass(frozen=True)
class _Observation:
    kind: str
    boundary: Boundary
    attempt: CapturedAttempt
    event: EventRef
    values: Mapping[str, Any] = field(repr=False)


def _request_values(record: Mapping[str, Any]) -> dict[str, Any] | str:
    parent = record.get("parent_attempt_id")
    if parent is not None and (not _is_ref(parent) or parent == record["attempt_id"]):
        return "invalid_field"
    model, projection = record.get("requested_model"), record.get("projection_revision")
    template = record.get("template_revision")
    metadata: RequestMetadata | None = None
    if model is not None or projection is not None or template is not None:
        if not (_is_ref(model) and _is_ref(projection)):
            return "invalid_field"
        if template is not None and not _is_ref(template):
            return "invalid_field"
        metadata = RequestMetadata(cast(str, model), cast(str, projection), template)
    content: bytes | None = None
    if "request_projection_b64" in record:
        content = _b64(record["request_projection_b64"], MAX_PROJECTION_BYTES)
        if content is None:
            return "invalid_field"
    return {"parent_attempt": parent, "metadata": metadata, "request_projection": content}


def _terminal_values(record: Mapping[str, Any]) -> dict[str, Any] | str:
    state, expected = record.get("state"), record.get("expected_chunks")
    if not (
        (state == "finished" and _is_int(expected, 0, MAX_CHUNKS + 1))
        or (state in {"timeout", "failed"} and expected is None)
    ):
        return "invalid_field"
    receipt = record.get("provider_receipt")
    served, parsed = record.get("served_model"), record.get("parsed_output")
    parser = record.get("parser_revision")
    for ref in (receipt, served, parsed, parser):
        if ref is not None and not _is_ref(ref):
            return "invalid_field"
    if (parsed is None) != (parser is None) or (parsed is not None and state != "finished"):
        return "invalid_field"
    metadata: ResponseMetadata | None = None
    if receipt is not None:
        metadata = ResponseMetadata(receipt, served, parsed, parser)
    elif served is not None or parsed is not None:
        return "invalid_field"
    return {"state": state, "expected_chunks": expected, "metadata": metadata}


def _final_body_values(record: Mapping[str, Any]) -> dict[str, Any] | str:
    lengths = record.get("chunk_lengths")
    if not (
        isinstance(lengths, list)
        and len(lengths) <= MAX_CHUNKS
        and all(_is_int(n, 0, MAX_CHUNK_BYTES + 1) for n in lengths)
    ):
        return "invalid_field"
    body = _b64(record.get("body_b64"), MAX_RECORD_BYTES)
    if body is None or len(body) != sum(lengths):
        return "invalid_field"
    return {"body": body, "chunk_lengths": tuple(lengths)}


def _values(kind: str, record: Mapping[str, Any]) -> dict[str, Any] | str:
    if kind == "request":
        return _request_values(record)
    if kind == "chunk":
        index, data = record.get("index"), _b64(record.get("data_b64"), MAX_CHUNK_BYTES)
        if not _is_int(index, 0, MAX_CHUNKS) or data is None:
            return "invalid_field"
        return {"index": index, "data": data}
    if kind == "terminal":
        return _terminal_values(record)
    if kind == "final_body":
        return _final_body_values(record)
    commit, payload = record.get("commit_ref"), record.get("decision_payload_ref")
    if not (_is_ref(commit) and _is_ref(payload)):
        return "invalid_field"
    return {"commit_ref": commit, "decision_payload_ref": payload}


def _observe(
    record: Mapping[str, Any],
    schema: str,
    boundaries: tuple[Boundary, ...],
    source_ref: str,
    now: float,
) -> tuple[_Observation | str, Boundary | None, int]:
    """Project one record through the allowlist; never read a field outside it."""
    kind = record.get("record")
    if record.get("schema") != schema or kind not in _SCHEMA_KINDS[schema]:
        return "unsupported_record", None, 0
    assert isinstance(kind, str)
    allowed = _ENVELOPE | _KIND_FIELDS[kind]
    dropped = sum(1 for key in record if key not in allowed)
    projected = {key: record[key] for key in allowed if key in record}
    boundary = _KIND_BOUNDARY[kind]
    if boundary not in boundaries:
        return "boundary_mismatch", None, dropped
    if not (
        all(_is_ref(projected.get(name)) for name in (*_IDENTITY, "event_id"))
        and _is_int(projected.get("decision_revision"), 1)
        and _is_int(projected.get("event_revision"), 1)
    ):
        return "unattributable", boundary, dropped
    observed_at = projected.get("observed_at")
    if not _is_int(observed_at, 0, 2**63):
        return "invalid_field", boundary, dropped
    assert isinstance(observed_at, int)
    if observed_at > now:
        return "future_observation", boundary, dropped
    values = _values(kind, projected)
    if isinstance(values, str):
        return values, boundary, dropped
    attempt = CapturedAttempt(
        projected["operation_id"],
        projected["attempt_id"],
        projected["case_id"],
        projected["family_id"],
        projected["decision_id"],
        projected["decision_revision"],
    )
    event = EventRef(source_ref, projected["event_id"], projected["event_revision"], observed_at)
    return _Observation(kind, boundary, attempt, event, values), boundary, dropped


def _records(
    data: bytes, schema: str, boundaries: tuple[Boundary, ...], source_ref: str, now: float
) -> list[tuple[int, _Observation | str, Boundary | None, int]]:
    parsed: list[tuple[int, _Observation | str, Boundary | None, int]] = []
    for position, line in _lines(data):
        _require(len(parsed) < MAX_RECORDS, "capture_operation_limit")
        record = _parse(line)
        if isinstance(record, str):
            parsed.append((position, record, None, 0))
            continue
        parsed.append((position, *_observe(record, schema, boundaries, source_ref, now)))
    return parsed


def _summary(parsed: list[tuple[int, _Observation | str, Boundary | None, int]]) -> dict[str, Any]:
    """Counts and stream state from in-memory records. Digests never leave this function."""
    kinds: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    dropped = 0
    streams: dict[tuple[str, str], dict[str, Any]] = {}
    for _, item, _, extra in parsed:
        dropped += extra
        if isinstance(item, str):
            reasons[item] += 1
            continue
        kinds[item.kind] += 1
        key = (item.attempt.operation_ref, item.attempt.attempt_ref)
        stream = streams.setdefault(key, {"chunks": {}, "terminals": set(), "body": set()})
        if item.kind == "chunk":
            digest = _sha256(item.values["data"])
            stream["chunks"].setdefault(item.values["index"], set()).add(digest)
        elif item.kind == "terminal":
            stream["terminals"].add((item.values["state"], item.values["expected_chunks"]))
        elif item.kind == "final_body":
            stream["body"].add(item.values["chunk_lengths"])
    attempts = []
    for (operation, attempt), stream in sorted(streams.items()):
        terminals = stream["terminals"]
        state, expected = next(iter(terminals)) if len(terminals) == 1 else (None, None)
        received = sorted(stream["chunks"])
        attempts.append(
            {
                "operation_ref": operation,
                "attempt_ref": attempt,
                "received_indices": received,
                "terminal_state": state,
                "missing_indices": sorted(set(range(expected)) - set(received))
                if expected is not None
                else [],
                "conflicting": len(terminals) > 1
                or any(len(found) > 1 for found in stream["chunks"].values()),
                "final_body": bool(stream["body"]),
            }
        )
    return {
        "records": len(parsed),
        "admitted": sum(kinds.values()),
        "by_kind": dict(sorted(kinds.items())),
        "quarantined": dict(sorted(reasons.items())),
        "dropped_fields": dropped,
        "operations": len({operation for operation, _ in streams}),
        "attempts": attempts,
    }


def dry_run_capture(
    data: bytes,
    *,
    schema: str,
    boundaries: tuple[Boundary, ...],
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Validate a gateway log or decision journal page locally. Nothing is written."""
    _require(schema in _SCHEMA_KINDS and type(data) is bytes and len(data) <= MAX_PAGE_BYTES)
    _require(_valid_boundaries(schema, boundaries))
    report = _summary(_records(data, schema, boundaries, "dry-run", clock()))
    return {"mode": "dry_run", "schema": schema, "writes": 0, **report}


def _valid_boundaries(schema: str, boundaries: tuple[Boundary, ...]) -> bool:
    allowed = {_KIND_BOUNDARY[kind] for kind in _SCHEMA_KINDS[schema]}
    return (
        type(boundaries) is tuple
        and 0 < len(boundaries) == len(set(boundaries))
        and set(boundaries) <= allowed
    )


class CaptureSourceAdapter:
    """Admit pages from the customer's gateway/provider log and application decision journal."""

    def __init__(self, ledger: CaptureStreams, clock: Callable[[], float] = time.time) -> None:
        self.ledger = ledger
        self.clock = clock

    def ingest_gateway(
        self,
        actor: str,
        workflow: str,
        page: SourcePage,
        *,
        boundaries: tuple[Boundary, ...] = ("application_request", "provider_response"),
    ) -> dict[str, Any]:
        return self._ingest(actor, workflow, page, GATEWAY_SCHEMA, GATEWAY_PROJECTION, boundaries)

    def ingest_journal(self, actor: str, workflow: str, page: SourcePage) -> dict[str, Any]:
        return self._ingest(
            actor, workflow, page, JOURNAL_SCHEMA, JOURNAL_PROJECTION, ("application_commit",)
        )

    def _ingest(
        self,
        actor: str,
        workflow: str,
        page: SourcePage,
        schema: str,
        projection: str,
        boundaries: tuple[Boundary, ...],
    ) -> dict[str, Any]:
        _require(type(page) is SourcePage and _valid_boundaries(schema, boundaries))
        failure: str | None = None
        result: dict[str, Any] = {}
        try:
            result = self._admit(actor, workflow, page, schema, projection, boundaries)
        except CaptureError as error:
            code = error.args[0] if error.args else None
            failure = code if code in _PAGE_REFUSALS else "decision_capture_unavailable"
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            failure = "decision_capture_unavailable"
        if failure is not None:
            _LOG.warning(
                "capture page refused",
                extra={
                    "capture": {"source": page.source_ref, "cursor": page.cursor, "reason": failure}
                },
            )
            raise CaptureError(failure)
        return result

    def _admit(
        self,
        actor: str,
        workflow: str,
        page: SourcePage,
        schema: str,
        projection: str,
        boundaries: tuple[Boundary, ...],
    ) -> dict[str, Any]:
        statuses = [
            self.ledger.page_status(actor, workflow, b, page.source_ref, page.cursor, page.data)
            for b in boundaries
        ]
        if all(status == "acknowledged" for status in statuses):
            _LOG.info(
                "capture page already acknowledged",
                extra={"capture": {"source": page.source_ref, "cursor": page.cursor}},
            )
            return {"status": "acknowledged", "cursor": page.cursor, "receipts": None}
        parsed = _records(page.data, schema, boundaries, page.source_ref, self.clock())
        counts = {b: Counter[str]() for b in boundaries}
        for position, item, attributed, dropped in parsed:
            boundary = attributed or boundaries[0]
            counts[boundary]["dropped_fields"] += dropped
            reason = item if isinstance(item, str) else self._apply(actor, workflow, item)
            if reason is None:
                counts[boundary]["admitted"] += 1
                continue
            self.ledger.quarantined(
                actor, workflow, boundary, page.source_ref, page.cursor, position, reason
            )
            counts[boundary]["quarantined"] += 1
            _LOG.info(
                "capture record quarantined",
                extra={"capture": {"cursor": page.cursor, "position": position, "reason": reason}},
            )
        receipts: dict[str, dict[str, Any]] = {}
        for boundary in boundaries:
            receipt = PageReceipt(
                page.source_ref,
                page.collector_ref,
                projection,
                page.cursor,
                page.next_cursor,
                counts[boundary]["admitted"],
                counts[boundary]["quarantined"],
                counts[boundary]["dropped_fields"],
                page.window_start,
                page.window_end,
            )
            stored = self.ledger.acknowledge(actor, workflow, boundary, receipt, page.data)
            stored.pop("page_ref", None)
            receipts[boundary] = stored
            _LOG.info(
                "capture page acknowledged",
                extra={"capture": {key: stored[key] for key in _LOGGED_RECEIPT}},
            )
        return {"status": "admitted", "cursor": page.cursor, "receipts": receipts}

    def _apply(self, actor: str, workflow: str, item: _Observation) -> str | None:
        values, attempt, event = item.values, item.attempt, item.event
        try:
            if item.kind == "request":
                self.ledger.started(
                    actor,
                    workflow,
                    attempt,
                    event,
                    parent_attempt=values["parent_attempt"],
                    metadata=values["metadata"],
                    request_projection=values["request_projection"],
                )
            elif item.kind == "chunk":
                self.ledger.observed_chunk(
                    actor, workflow, attempt, event, values["index"], values["data"]
                )
            elif item.kind == "terminal":
                self.ledger.terminal(
                    actor,
                    workflow,
                    attempt,
                    event,
                    values["state"],
                    values["expected_chunks"],
                    metadata=values["metadata"],
                )
            elif item.kind == "final_body":
                self.ledger.final_body(
                    actor, workflow, attempt, event, values["body"], values["chunk_lengths"]
                )
            else:
                self.ledger.committed(
                    actor,
                    workflow,
                    attempt,
                    event,
                    values["commit_ref"],
                    decision_payload_ref=values["decision_payload_ref"],
                )
        except CaptureError as error:
            code = error.args[0] if error.args else None
            if code in _RECORD_REFUSALS:
                return str(code)
            raise
        return None


_LOGGED_RECEIPT = (
    "boundary",
    "source_ref",
    "collector_ref",
    "projection_revision",
    "cursor",
    "admitted",
    "quarantined",
    "dropped_fields",
)


@dataclass(frozen=True)
class PinnedExport:
    """A historical export whose SHA-256 the custodian declared independently of its bytes."""

    source_ref: str
    collector_ref: str
    sha256: str
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require(_is_ref(self.source_ref) and _is_ref(self.collector_ref))
        _require(isinstance(self.sha256, str) and _HEX64.fullmatch(self.sha256) is not None)
        _require(type(self.data) is bytes and len(self.data) <= MAX_PAGE_BYTES)


@dataclass(frozen=True)
class AsOfArchive:
    """Pinned revision history: which policy, template or attachment was effective when."""

    sha256: str
    data: bytes = field(repr=False)

    def __post_init__(self) -> None:
        _require(isinstance(self.sha256, str) and _HEX64.fullmatch(self.sha256) is not None)
        _require(type(self.data) is bytes and len(self.data) <= MAX_PAGE_BYTES)


def _archive_entries(archive: AsOfArchive) -> tuple[tuple[str, str, int, int | None], ...]:
    _require(hmac.compare_digest(_sha256(archive.data), archive.sha256), "archive_invalid")
    entries: list[tuple[str, str, int, int | None]] = []
    for _, line in _lines(archive.data):
        record = _parse(line)
        _require(isinstance(record, dict), "archive_invalid")
        assert isinstance(record, dict)
        start, end = record.get("effective_from"), record.get("effective_until")
        _require(
            set(record) == {"schema", "component", "ref", "effective_from", "effective_until"}
            and record["schema"] == ARCHIVE_SCHEMA
            and record["component"] in _RECONSTRUCTABLE
            and _is_ref(record["ref"])
            and _is_int(start, 0, 2**63)
            and (end is None or (_is_int(end, 0, 2**63) and start < end)),
            "archive_invalid",
        )
        entries.append((record["component"], record["ref"], cast(int, start), end))
        _require(len(entries) <= MAX_RECORDS, "archive_invalid")
    return tuple(entries)


def _scalar(value: object) -> FieldEvidence | None:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((key, item),) = value.items()
    if key == "value" and _is_ref(item):
        return FieldEvidence("observed", ref=item)
    if key == "unavailable" and item in UNAVAILABLE_REASONS:
        return FieldEvidence("unavailable", reason=item)
    return None


def _content(value: object, retention: str) -> ContentEvidence | None:
    if not isinstance(value, dict) or len(value) != 1:
        return None
    ((key, item),) = value.items()
    if key == "content_b64" and retention == "content":
        data = _b64(item, MAX_PROJECTION_BYTES)
        return None if data is None else ContentEvidence("content", content=data)
    if key == "pointer_sha256" and retention in {"content", "customer_local_pointer"}:
        if isinstance(item, str) and _HEX64.fullmatch(item):
            return ContentEvidence("customer_local_pointer", pointer_sha256=item)
        return None
    if key == "unavailable" and item in UNAVAILABLE_REASONS:
        return ContentEvidence("unavailable", reason=item)
    return None


def _reconstruct(
    name: str,
    evidence: FieldEvidence,
    decided_at: int,
    entries: tuple[tuple[str, str, int, int | None], ...],
    archive_ref: str | None,
) -> FieldEvidence:
    """Fill an omitted revision reference only from the pinned as-of archive, never `now`."""
    if (
        archive_ref is None
        or name not in _RECONSTRUCTABLE
        or evidence.fidelity != "unavailable"
        or evidence.reason not in _RECONSTRUCT_FROM
    ):
        return evidence
    matches = {
        ref
        for component, ref, start, end in entries
        if component == name and start <= decided_at and (end is None or decided_at < end)
    }
    if len(matches) > 1:
        return FieldEvidence("unavailable", reason="ambiguous_as_of_source")
    if matches:
        return FieldEvidence("reconstructed", ref=next(iter(matches)), source_ref=archive_ref)
    return evidence


def _historical(
    record: Mapping[str, Any],
    export_ref: str,
    entries: tuple[tuple[str, str, int, int | None], ...],
    archive_ref: str | None,
    now: float,
) -> tuple[HistoricalRecord | str, int]:
    if record.get("schema") != HISTORICAL_SCHEMA or record.get("record") != "decision":
        return "unsupported_record", 0
    dropped = sum(1 for key in record if key not in _HISTORICAL_FIELDS)
    ids = ("record_id", "operation_id", "case_id", "family_id", "decision_id")
    if not (
        all(_is_ref(record.get(name)) for name in ids)
        and _is_int(record.get("decision_revision"), 1)
    ):
        return "unattributable", dropped
    recorded_at, decided_at = record.get("recorded_at"), record.get("decided_at")
    retention = record.get("retention")
    components = record.get("components", {})
    if not (
        _is_int(recorded_at, 0, 2**63)
        and _is_int(decided_at, 0, 2**63)
        and retention in {"content", "customer_local_pointer", "metadata_only"}
        and isinstance(components, dict)
    ):
        return "invalid_field", dropped
    assert isinstance(recorded_at, int) and isinstance(decided_at, int)
    assert isinstance(retention, str) and isinstance(components, dict)
    if recorded_at > now:
        return "future_observation", dropped
    if decided_at > recorded_at:
        return "invalid_field", dropped
    known = set(SCALAR_COMPONENTS) | set(CONTENT_COMPONENTS)
    dropped += sum(1 for key in components if key not in known)
    fields: list[tuple[str, FieldEvidence]] = []
    for name in SCALAR_COMPONENTS:
        scalar = (
            _scalar(components[name])
            if name in components
            else FieldEvidence("unavailable", reason="not_in_export")
        )
        if scalar is None:
            return "invalid_field", dropped
        fields.append((name, _reconstruct(name, scalar, decided_at, entries, archive_ref)))
    contents: list[tuple[str, ContentEvidence]] = []
    for name in CONTENT_COMPONENTS:
        item = (
            _content(components[name], retention)
            if name in components
            else ContentEvidence("unavailable", reason="not_in_export")
        )
        if item is None:
            return "invalid_field", dropped
        contents.append((name, item))
    return (
        HistoricalRecord(
            export_ref,
            HISTORICAL_PARSER,
            record["record_id"],
            recorded_at,
            record["operation_id"],
            record["case_id"],
            record["family_id"],
            record["decision_id"],
            record["decision_revision"],
            decided_at,
            cast(Literal["content", "customer_local_pointer", "metadata_only"], retention),
            tuple(fields),
            tuple(contents),
        ),
        dropped,
    )


def _historical_records(
    data: bytes,
    export_ref: str,
    archive: AsOfArchive | None,
    now: float,
) -> list[tuple[int, HistoricalRecord | str, int]]:
    entries = _archive_entries(archive) if archive is not None else ()
    archive_ref = None if archive is None else "sha256:" + archive.sha256
    parsed: list[tuple[int, HistoricalRecord | str, int]] = []
    for position, line in _lines(data):
        _require(len(parsed) < MAX_RECORDS, "capture_operation_limit")
        record = _parse(line)
        if isinstance(record, str):
            parsed.append((position, record, 0))
            continue
        parsed.append((position, *_historical(record, export_ref, entries, archive_ref, now)))
    return parsed


def _historical_summary(parsed: list[tuple[int, HistoricalRecord | str, int]]) -> dict[str, Any]:
    reasons: Counter[str] = Counter()
    fidelity: dict[str, Counter[str]] = {}
    decided: list[int] = []
    for _, item, _ in parsed:
        if isinstance(item, str):
            reasons[item] += 1
            continue
        decided.append(item.decided_at)
        for name, value in item.fields:
            fidelity.setdefault(name, Counter())[value.fidelity] += 1
        for name, content in item.contents:
            fidelity.setdefault(name, Counter())[content.form] += 1
    return {
        "records": len(parsed),
        "admitted": len(decided),
        "quarantined": dict(sorted(reasons.items())),
        "dropped_fields": sum(extra for _, _, extra in parsed),
        "fidelity": {
            name: dict(sorted(counts.items())) for name, counts in sorted(fidelity.items())
        },
        "window": [min(decided), max(decided)] if decided else None,
    }


def dry_run_historical(
    data: bytes,
    sha256: str,
    *,
    archive: AsOfArchive | None = None,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """Check the pin and per-field fidelity of a historical export locally. Nothing is written."""
    _require(type(data) is bytes and len(data) <= MAX_PAGE_BYTES)
    _require(isinstance(sha256, str) and _HEX64.fullmatch(sha256) is not None)
    _require(hmac.compare_digest(_sha256(data), sha256), "export_pin_mismatch")
    parsed = _historical_records(data, "sha256:" + sha256, archive, clock())
    return {
        "mode": "dry_run",
        "schema": HISTORICAL_SCHEMA,
        "writes": 0,
        **_historical_summary(parsed),
    }


class HistoricalImport:
    """Import a pinned historical export without filling gaps from current folder state."""

    def __init__(self, ledger: CaptureStreams, clock: Callable[[], float] = time.time) -> None:
        self.ledger = ledger
        self.clock = clock

    def ingest(
        self,
        actor: str,
        workflow: str,
        export: PinnedExport,
        *,
        archive: AsOfArchive | None = None,
    ) -> dict[str, Any]:
        _require(type(export) is PinnedExport)
        _require(archive is None or type(archive) is AsOfArchive)
        failure: str | None = None
        result: dict[str, Any] = {}
        try:
            result = self._admit(actor, workflow, export, archive)
        except CaptureError as error:
            code = error.args[0] if error.args else None
            failure = code if code in _PAGE_REFUSALS else "decision_capture_unavailable"
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            failure = "decision_capture_unavailable"
        if failure is not None:
            _LOG.warning(
                "historical export refused",
                extra={"capture": {"source": export.source_ref, "reason": failure}},
            )
            raise CaptureError(failure)
        return result

    def _admit(
        self, actor: str, workflow: str, export: PinnedExport, archive: AsOfArchive | None
    ) -> dict[str, Any]:
        _require(hmac.compare_digest(_sha256(export.data), export.sha256), "export_pin_mismatch")
        export_ref = "sha256:" + export.sha256
        boundary: Boundary = "historical_export"
        status = self.ledger.page_status(
            actor, workflow, boundary, export.source_ref, export_ref, export.data
        )
        if status == "acknowledged":
            return {"status": "acknowledged", "export_ref": export_ref, "receipt": None}
        parsed = _historical_records(export.data, export_ref, archive, self.clock())
        admitted = quarantined = 0
        for position, item, _ in parsed:
            if isinstance(item, str):
                self.ledger.quarantined(
                    actor, workflow, boundary, export.source_ref, export_ref, position, item
                )
                quarantined += 1
                continue
            self.ledger.imported(actor, workflow, export.source_ref, item)
            admitted += 1
        summary = _historical_summary(parsed)
        window = summary["window"] or [None, None]
        receipt = PageReceipt(
            export.source_ref,
            export.collector_ref,
            HISTORICAL_PARSER,
            export_ref,
            None,
            admitted,
            quarantined,
            summary["dropped_fields"],
            window[0],
            window[1],
        )
        stored = self.ledger.acknowledge(actor, workflow, boundary, receipt, export.data)
        stored.pop("page_ref", None)
        _LOG.info(
            "historical export acknowledged",
            extra={"capture": {key: stored[key] for key in _LOGGED_RECEIPT}},
        )
        return {"status": "admitted", "export_ref": export_ref, "receipt": stored}


@dataclass(frozen=True)
class SamplingManifest:
    """An upstream count of eligible operations from a source independent of the traces."""

    source_ref: str
    manifest_ref: str
    window_start: int
    window_end: int
    eligible_operations: frozenset[str]


def sampling_manifest(data: bytes, sha256: str) -> SamplingManifest:
    """Parse a pinned sampling manifest. Any irregularity refuses it rather than guessing."""
    _require(type(data) is bytes and len(data) <= MAX_PAGE_BYTES)
    _require(isinstance(sha256, str) and _HEX64.fullmatch(sha256) is not None)
    _require(hmac.compare_digest(_sha256(data), sha256), "manifest_pin_mismatch")
    record = _parse(data.strip())
    _require(isinstance(record, dict), "manifest_invalid")
    assert isinstance(record, dict)
    operations = record.get("eligible_operations")
    start, end = record.get("window_start"), record.get("window_end")
    _require(
        set(record) == {"schema", "source_ref", "window_start", "window_end", "eligible_operations"}
        and record["schema"] == MANIFEST_SCHEMA
        and _is_ref(record["source_ref"])
        and _is_int(start, 0, 2**63)
        and _is_int(end, 0, 2**63)
        and isinstance(operations, list)
        and len(operations) <= MAX_MANIFEST_OPERATIONS
        and all(_is_ref(op) for op in operations)
        and len(set(operations)) == len(operations),
        "manifest_invalid",
    )
    assert isinstance(start, int) and isinstance(end, int) and isinstance(operations, list)
    _require(start <= end, "manifest_invalid")
    return SamplingManifest(
        record["source_ref"], "sha256:" + sha256, start, end, frozenset(operations)
    )


def capture_coverage(
    traced_operations: Iterable[str],
    manifest: SamplingManifest | None,
    *,
    trace_source_ref: str,
) -> dict[str, Any]:
    """Traced operations over an independent source-wide denominator, or unavailable."""
    traced = set(traced_operations)
    _require(all(_is_ref(op) for op in traced) and _is_ref(trace_source_ref))
    reason: str | None = None
    if manifest is None:
        reason = "no_independent_denominator"
    elif type(manifest) is not SamplingManifest:
        reason = "no_independent_denominator"
    elif manifest.source_ref == trace_source_ref:
        reason = "denominator_not_independent"
    elif not traced <= manifest.eligible_operations:
        reason = "traced_outside_denominator"
    if reason is not None or manifest is None:
        return {
            "status": "unavailable",
            "reason": reason,
            "capture_coverage": None,
            "agreement_scope": "captured_subset",
            "manifest_ref": None,
        }
    total = len(manifest.eligible_operations)
    return {
        "status": "known",
        "reason": None,
        "capture_coverage": [len(traced), total],
        "agreement_scope": "declared_population" if len(traced) == total else "captured_subset",
        "manifest_ref": manifest.manifest_ref,
    }


ScoringBoundary = Literal["provider_response", "application_commit"]


@dataclass(frozen=True)
class BoundaryOutcome:
    """One decision revision as seen at exactly one scoring boundary."""

    boundary: ScoringBoundary
    decision_ref: str
    decision_revision: int
    predicted_ref: str | None
    truth_ref: str | None
    status: Literal["scorable", "pending", "unavailable"]
    reason: str | None = None


def _roots(roots: Iterable[VerifiedRoot], namespace: str) -> dict[tuple[str, int], VerifiedRoot]:
    """OPE-514 verified roots for the deciding namespace; the reducer stays authoritative."""
    found: dict[tuple[str, int], VerifiedRoot] = {}
    for root in roots:
        _require(type(root) is VerifiedRoot)
        if root.namespace != namespace:
            continue
        key = (root.decision_id, root.decision_revision)
        _require(key not in found)
        found[key] = root
    return found


def _captured(views: Iterable[Mapping[str, Any]]) -> dict[tuple[str, int], list[dict[str, Any]]]:
    decisions: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for view in views:
        for attempt in view["attempts"]:
            if attempt["decision_ref"] is None:
                continue
            key = (attempt["decision_ref"], attempt["decision_revision"])
            decisions.setdefault(key, []).append(attempt)
    return decisions


def provider_outcomes(
    views: Iterable[Mapping[str, Any]], roots: Iterable[VerifiedRoot], namespace: str
) -> tuple[BoundaryOutcome, ...]:
    """Raw parsed output of the attempt the application committed, else its only output."""
    truth = _roots(roots, namespace)
    outcomes: list[BoundaryOutcome] = []
    for key, attempts in sorted(_captured(views).items()):
        committed = [a for a in attempts if a["committed"] is True]
        candidates = committed or [a for a in attempts if a["state"] == "finished"]
        outputs = {
            a.get("response", {}).get("parsed_output_ref")
            for a in candidates
            if a["state"] == "finished"
        }
        reason: str | None = None
        if any(a["state"] == "disputed" for a in candidates):
            reason = "disputed_attempt"
        elif len(committed) > 1:
            reason = "ambiguous_committed_attempt"
        elif not outputs or None in outputs:
            reason = "raw_output_unavailable"
        elif len(outputs) > 1:
            reason = "ambiguous_raw_output"
        outcomes.append(_outcome("provider_response", key, outputs, truth.get(key), reason))
    return tuple(outcomes)


def application_outcomes(
    views: Iterable[Mapping[str, Any]], roots: Iterable[VerifiedRoot], namespace: str
) -> tuple[BoundaryOutcome, ...]:
    """The committed action as OPE-514 recorded it; no commit means unavailable, not zero."""
    truth = _roots(roots, namespace)
    outcomes: list[BoundaryOutcome] = []
    for key, attempts in sorted(_captured(views).items()):
        root = truth.get(key)
        reason = None
        if all(a["committed"] is None for a in attempts):
            reason = "commit_not_permitted"
        elif not any(a["committed"] is True for a in attempts):
            reason = "no_application_commit"
        actions = {root.action_ref} if root is not None else set()
        outcomes.append(_outcome("application_commit", key, actions, root, reason))
    return tuple(outcomes)


def historical_outcomes(
    import_view: Mapping[str, Any], roots: Iterable[VerifiedRoot], namespace: str
) -> tuple[BoundaryOutcome, ...]:
    """Observed historical committed labels joined with independently verified truth."""
    truth = _roots(roots, namespace)
    outcomes: list[BoundaryOutcome] = []
    for decision in import_view["decisions"] or ():
        if not decision["current"]:
            continue
        key = (decision["decision_ref"], decision["decision_revision"])
        root = truth.get(key)
        reason: str | None = None
        labels: set[str] = set()
        if decision["state"] != "imported":
            reason = "disputed_import"
        elif decision["fields"]["committed_action"]["fidelity"] != "observed":
            reason = "committed_label_unavailable"
        else:
            labels = {decision["fields"]["committed_action"]["ref"]}
            if root is not None and labels != {root.action_ref}:
                reason = "committed_label_conflict"
        outcomes.append(_outcome("application_commit", key, labels, root, reason))
    return tuple(outcomes)


def _outcome(
    boundary: ScoringBoundary,
    key: tuple[str, int],
    predicted: Iterable[str | None],
    root: VerifiedRoot | None,
    reason: str | None,
) -> BoundaryOutcome:
    decision, revision = key
    if reason is not None:
        return BoundaryOutcome(boundary, decision, revision, None, None, "unavailable", reason)
    values = tuple(predicted)
    value = values[0] if len(values) == 1 else None
    if root is None:
        return BoundaryOutcome(boundary, decision, revision, value, None, "pending")
    return BoundaryOutcome(
        boundary, decision, revision, value, root.verified_action_ref, "scorable"
    )


def score_boundary(
    boundary: ScoringBoundary,
    outcomes: Iterable[BoundaryOutcome],
    *,
    coverage: Mapping[str, Any],
) -> dict[str, Any]:
    """Exact agreement at one declared boundary. Each decision revision counts once."""
    _require(boundary in {"provider_response", "application_commit"})
    items = tuple(outcomes)
    _require(all(type(item) is BoundaryOutcome and item.boundary == boundary for item in items))
    keys = [(item.decision_ref, item.decision_revision) for item in items]
    _require(len(keys) == len(set(keys)))
    scored = [item for item in items if item.status == "scorable"]
    matches = sum(1 for item in scored if item.predicted_ref == item.truth_ref)
    unavailable = Counter(str(item.reason) for item in items if item.status == "unavailable")
    return {
        "boundary": boundary,
        "status": "measured" if scored else "unavailable",
        "agreement": [matches, len(scored)] if scored else None,
        "pending": sum(1 for item in items if item.status == "pending"),
        "unavailable": dict(sorted(unavailable.items())),
        "decisions": len(items),
        "agreement_scope": coverage.get("agreement_scope", "captured_subset"),
        "capture_coverage": coverage.get("capture_coverage"),
        "integrity": INTEGRITY,
        "issuer_authenticated": False,
    }
