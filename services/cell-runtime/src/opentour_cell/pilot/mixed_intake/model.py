"""Shared vocabulary for mixed-source intake: states, roles, limits, spans and records."""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

SCHEMA_VERSION = "opentour.mixed-intake.v1"
MANIFEST_VERSION = "opentour.mixed-intake.manifest.v1"
INVENTORY_VERSION = "opentour.mixed-intake.inventory.v1"

Json = dict[str, Any]


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class ExtractionState(StrEnum):
    """Separate, recoverable per-object outcomes. Only ELIGIBLE may feed evidence."""

    ELIGIBLE = "eligible"
    NEEDS_RENDITION = "needs_rendition"
    NEEDS_REVIEW = "needs_review"
    LOCKED = "locked"
    CORRUPT = "corrupt"
    OVERSIZED = "oversized"
    OVER_DEPTH = "over_depth"
    TIMED_OUT = "timed_out"
    UNSUPPORTED = "unsupported"
    DENIED = "denied"
    DESELECTED = "deselected"
    WITHDRAWN = "withdrawn"
    DELETED = "deleted"
    ABSENCE_UNKNOWN = "absence_unknown"


class Role(StrEnum):
    """Owner-declared lanes. Nothing but an owner declaration may leave UNKNOWN."""

    POLICY = "policy"
    EXAMPLE = "example"
    OBSERVED_DECISION = "observed_decision"
    INDEPENDENT_FINAL = "independent_final"
    UNKNOWN = "unknown"


class NextActor(StrEnum):
    NONE = "none"
    OWNER = "owner"
    CUSTOMER_ADMIN = "customer_admin"
    PROVIDER = "provider"
    SYSTEM = "system"


@dataclass(frozen=True)
class Limits:
    """Configured, visible ceilings. Every one is enforced by a reader or the scanner."""

    max_object_bytes: int = 2_000_000
    max_expanded_bytes: int = 8_000_000
    max_archive_entries: int = 200
    max_archive_depth: int = 1
    max_folder_depth: int = 8
    max_pages: int = 100
    max_rows: int = 10_000
    max_json_depth: int = 32
    max_xml_depth: int = 64
    max_seconds: float = 20.0
    max_run_seconds: float = 600.0

    def visible(self) -> Json:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class ReaderFailure(Exception):
    """A classified, recoverable inability to read one object."""

    def __init__(
        self,
        state: ExtractionState,
        reason: str,
        next_actor: NextActor,
        *,
        limit: str | None = None,
    ):
        super().__init__(reason)
        self.state = state
        self.reason = reason
        self.next_actor = next_actor
        self.limit = limit


class Deadline:
    """Cooperative run-time ceiling with an injectable clock for deterministic tests."""

    def __init__(self, seconds: float, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._end = clock() + seconds

    def check(self) -> None:
        if self._clock() > self._end:
            raise ReaderFailure(
                ExtractionState.TIMED_OUT, "run_time_limit", NextActor.OWNER, limit="max_seconds"
            )


@dataclass(frozen=True)
class Span:
    """One cited unit. The locator is format specific and pinned to the object revision."""

    locator: Json
    text: str
    data: Json | None = None
    flags: tuple[str, ...] = ()
    available: bool = True

    def as_json(self) -> Json:
        return {
            "locator": self.locator,
            "text": self.text,
            "data": self.data,
            "flags": list(self.flags),
            "available": self.available,
        }

    @classmethod
    def from_json(cls, value: Json) -> Span:
        return cls(
            value["locator"],
            value["text"],
            value["data"],
            tuple(value["flags"]),
            value["available"],
        )


# Flags that route an otherwise readable object to explicit review.
REVIEW_FLAGS = frozenset(
    {
        "ambiguous_date",
        "timezone_unspecified",
        "ragged_row",
        "duplicate_header",
        "column_layout_ambiguous",
        "font_encoding_unverified",
        "merged_cell",
        "tracked_changes",
        "ocr_text_requires_review",
        "pages_need_rendition",
    }
)


@dataclass(frozen=True)
class ReadResult:
    state: ExtractionState
    spans: tuple[Span, ...] = ()
    reason: str = ""
    next_actor: NextActor = NextActor.NONE
    notes: tuple[str, ...] = ()
    limit: str | None = None

    @property
    def unavailable_spans(self) -> int:
        return sum(1 for span in self.spans if not span.available)


def failure_result(error: ReaderFailure) -> ReadResult:
    return ReadResult(error.state, (), error.reason, error.next_actor, limit=error.limit)


def finish(spans: list[Span], notes: list[str]) -> ReadResult:
    """Route readable output to ELIGIBLE, or to review when any span is ambiguous."""
    flagged = sorted({flag for span in spans for flag in span.flags if flag in REVIEW_FLAGS})
    if flagged:
        return ReadResult(
            ExtractionState.NEEDS_REVIEW,
            tuple(spans),
            "ambiguous_extraction:" + ",".join(flagged),
            NextActor.OWNER,
            tuple(notes),
        )
    return ReadResult(ExtractionState.ELIGIBLE, tuple(spans), "", NextActor.NONE, tuple(notes))


@dataclass
class Occurrence:
    """One permitted source occurrence with full custody metadata."""

    occurrence_id: str
    family_id: str
    content_identity: str | None
    tenant: str
    connection: str
    scoped_root: str
    native_id: str
    native_revision: str
    content_digest: str | None
    parser_revision: str
    access_epoch: int
    as_of: str
    source_role: str
    extraction_state: str
    reason: str
    next_actor: str
    display_name: str | None
    path: str | None
    format: str
    original_revision: str | None = None
    original_digest: str | None = None
    export_revision: str | None = None
    export_digest: str | None = None
    unavailable_spans: int = 0
    notes: list[str] = field(default_factory=list)
    spans: list[Json] = field(default_factory=list)
    limit: str | None = None
    ancestors: list[str] = field(default_factory=list)
    eval_key: str = ""

    def as_json(self) -> Json:
        return dict(self.__dict__)

    def semantic(self) -> Json:
        """Rename- and order-independent projection used for replay equality and fingerprints."""
        return {
            "family_id": self.family_id,
            "native_id": self.native_id,
            "native_revision": self.native_revision,
            "content_digest": self.content_digest,
            "content_identity": self.content_identity,
            "source_role": self.source_role,
            "extraction_state": self.extraction_state,
            "parser_revision": self.parser_revision,
            "format": self.format,
            "unavailable_spans": self.unavailable_spans,
            "lineage": digest([span["locator"] for span in self.spans]),
        }
