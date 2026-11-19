"""Exact one-to-one joins over authorized, normalized source snapshots."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

type JoinScalar = str | int | bool


class JoinInputError(ValueError):
    """An invalid normalized key or join plan."""


class KeyKind(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    BOOLEAN = "boolean"


class JoinStatus(StrEnum):
    MATCHED = "matched"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


class JoinReason(StrEnum):
    MISSING_KEY = "missing_key"
    NULL_KEY = "null_key"
    NO_EXACT_MATCH = "no_exact_match"
    MULTIPLE_EXACT_MATCHES = "multiple_exact_matches"


@dataclass(frozen=True, slots=True)
class JoinScope:
    organization_id: str
    environment_id: str
    connection_id: str
    source_role: str
    snapshot_id: str

    def __post_init__(self) -> None:
        identifiers = (
            self.organization_id,
            self.environment_id,
            self.connection_id,
            self.source_role,
            self.snapshot_id,
        )
        if any(type(value) is not str or not value for value in identifiers):
            raise JoinInputError("The normalized source scope must be complete")


@dataclass(frozen=True, slots=True)
class JoinField:
    """An approved field whose path has already been resolved by the caller."""

    name: str
    kind: KeyKind
    allow_empty: bool = False

    def __post_init__(self) -> None:
        if (
            type(self.name) is not str
            or not self.name
            or not isinstance(self.kind, KeyKind)
            or type(self.allow_empty) is not bool
            or (self.allow_empty and self.kind is not KeyKind.STRING)
        ):
            raise JoinInputError("The join field declaration is invalid")


@dataclass(frozen=True, slots=True)
class JoinRecord:
    scope: JoinScope
    reference: str
    keys: Mapping[str, JoinScalar | None] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.reference) is not str or not self.reference:
            raise JoinInputError("A normalized source row must carry an opaque reference")


@dataclass(frozen=True, slots=True)
class JoinResult:
    status: JoinStatus
    reference: str | None = None
    reason: JoinReason | None = None


_SCALAR_TYPES = {KeyKind.STRING: str, KeyKind.INTEGER: int, KeyKind.BOOLEAN: bool}


def _valid_scalar(value: object, join_field: JoinField) -> bool:
    return type(value) is _SCALAR_TYPES[join_field.kind] and (value != "" or join_field.allow_empty)


def join_one_to_one(
    query_keys: Mapping[str, JoinScalar | None],
    records: Sequence[JoinRecord],
    *,
    scope: JoinScope,
    fields: tuple[JoinField, ...],
) -> JoinResult:
    """Match within one caller-approved scope and pinned snapshot.

    Source access and mapping validation happen before the call. Source rows
    with missing or ineligible keys cannot satisfy a match. This component
    never selects among duplicate rows or executes a one-to-many resolver.
    """
    if not fields or len({join_field.name for join_field in fields}) != len(fields):
        raise JoinInputError("Join fields must be nonempty and distinct")

    selected_values: list[JoinScalar] = []
    for join_field in fields:
        if join_field.name not in query_keys:
            return JoinResult(JoinStatus.MISSING, reason=JoinReason.MISSING_KEY)
        value = query_keys[join_field.name]
        if value is None:
            return JoinResult(JoinStatus.MISSING, reason=JoinReason.NULL_KEY)
        if not _valid_scalar(value, join_field):
            raise JoinInputError("A query key does not satisfy its declared scalar type")
        selected_values.append(value)

    match: str | None = None
    for record in records:
        if record.scope != scope:
            continue
        for join_field, expected in zip(fields, selected_values, strict=True):
            actual = record.keys.get(join_field.name)
            if not _valid_scalar(actual, join_field) or actual != expected:
                break
        else:
            if match is not None:
                return JoinResult(JoinStatus.AMBIGUOUS, reason=JoinReason.MULTIPLE_EXACT_MATCHES)
            match = record.reference

    if match is None:
        return JoinResult(JoinStatus.MISSING, reason=JoinReason.NO_EXACT_MATCH)
    return JoinResult(JoinStatus.MATCHED, reference=match)
