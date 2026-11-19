"""Exact joins over authorized, normalized source snapshots."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

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


@dataclass(frozen=True, slots=True)
class ResolverContract:
    """Caller-qualified, exact binding to the versioned resolver artifact and rules."""

    scope: JoinScope
    reference: str
    revision: str
    aggregation_rule: str
    order_rule: str
    authority_rule: str

    def __post_init__(self) -> None:
        declaration = (
            self.reference,
            self.revision,
            self.aggregation_rule,
            self.order_rule,
            self.authority_rule,
        )
        if any(type(value) is not str or not value for value in declaration):
            raise JoinInputError("A complete versioned resolver declaration is required")


class QualifiedResolver(Protocol):
    @property
    def contract(self) -> ResolverContract: ...

    def order_references(self, references: tuple[str, ...]) -> tuple[str, ...]:
        """Apply the pinned order to opaque references supplied in canonical order."""
        ...


@dataclass(frozen=True, slots=True)
class ManyJoinResult:
    status: JoinStatus
    references: tuple[str, ...] = ()
    reason: JoinReason | None = None


_SCALAR_TYPES = {KeyKind.STRING: str, KeyKind.INTEGER: int, KeyKind.BOOLEAN: bool}


def _valid_scalar(value: object, join_field: JoinField) -> bool:
    return type(value) is _SCALAR_TYPES[join_field.kind] and (value != "" or join_field.allow_empty)


def _query_values(
    query_keys: Mapping[str, JoinScalar | None],
    fields: tuple[JoinField, ...],
) -> tuple[JoinScalar, ...] | JoinReason:
    if not fields or len({join_field.name for join_field in fields}) != len(fields):
        raise JoinInputError("Join fields must be nonempty and distinct")

    selected_values: list[JoinScalar] = []
    for join_field in fields:
        if join_field.name not in query_keys:
            return JoinReason.MISSING_KEY
        value = query_keys[join_field.name]
        if value is None:
            return JoinReason.NULL_KEY
        if not _valid_scalar(value, join_field):
            raise JoinInputError("A query key does not satisfy its declared scalar type")
        selected_values.append(value)
    return tuple(selected_values)


def _exact_matches(
    records: Sequence[JoinRecord],
    scope: JoinScope,
    fields: tuple[JoinField, ...],
    selected_values: tuple[JoinScalar, ...],
) -> Iterator[JoinRecord]:
    for record in records:
        if record.scope != scope:
            continue
        for join_field, expected in zip(fields, selected_values, strict=True):
            actual = record.keys.get(join_field.name)
            if not _valid_scalar(actual, join_field) or actual != expected:
                break
        else:
            yield record


def join_one_to_one(
    query_keys: Mapping[str, JoinScalar | None],
    records: Sequence[JoinRecord],
    *,
    scope: JoinScope,
    fields: tuple[JoinField, ...],
) -> JoinResult:
    """Match within one caller-approved scope and pinned snapshot."""
    selected_values = _query_values(query_keys, fields)
    if isinstance(selected_values, JoinReason):
        return JoinResult(JoinStatus.MISSING, reason=selected_values)
    matches = _exact_matches(records, scope, fields, selected_values)
    first_match = next(matches, None)
    if first_match is None:
        return JoinResult(JoinStatus.MISSING, reason=JoinReason.NO_EXACT_MATCH)
    if next(matches, None) is not None:
        return JoinResult(JoinStatus.AMBIGUOUS, reason=JoinReason.MULTIPLE_EXACT_MATCHES)
    return JoinResult(JoinStatus.MATCHED, reference=first_match.reference)


def join_one_to_many(
    query_keys: Mapping[str, JoinScalar | None],
    records: Sequence[JoinRecord],
    *,
    scope: JoinScope,
    fields: tuple[JoinField, ...],
    required_resolver: ResolverContract | None,
    resolver: QualifiedResolver | None,
) -> ManyJoinResult:
    """Preserve every exact reference under the caller's qualified resolver binding.

    The caller resolves the artifact and qualifies its aggregation, order and
    authority rules before entering this component. The adapter receives only
    opaque references and must preserve them all. It looks up any further
    ordering metadata through its own qualified catalog.
    """
    if required_resolver is None or required_resolver.scope != scope or resolver is None:
        raise JoinInputError("One-to-many joins require the exact qualified resolver binding")
    try:
        actual_contract = resolver.contract
    except Exception:
        raise JoinInputError("The qualified resolver binding could not be read") from None
    if type(actual_contract) is not ResolverContract or actual_contract != required_resolver:
        raise JoinInputError("One-to-many joins require the exact qualified resolver binding")

    selected_values = _query_values(query_keys, fields)
    if isinstance(selected_values, JoinReason):
        return ManyJoinResult(JoinStatus.MISSING, reason=selected_values)
    references = tuple(
        sorted(row.reference for row in _exact_matches(records, scope, fields, selected_values))
    )
    if not references:
        return ManyJoinResult(JoinStatus.MISSING, reason=JoinReason.NO_EXACT_MATCH)
    expected = set(references)
    if len(expected) != len(references):
        raise JoinInputError("Exact one-to-many matches must have distinct immutable references")
    try:
        ordered = resolver.order_references(references)
    except Exception:
        raise JoinInputError(
            "The qualified resolver could not order the exact references"
        ) from None
    if (
        type(ordered) is not tuple
        or len(ordered) != len(references)
        or any(type(reference) is not str for reference in ordered)
        or set(ordered) != expected
    ):
        raise JoinInputError("The qualified resolver must preserve all exact references")
    return ManyJoinResult(JoinStatus.MATCHED, references=ordered)
