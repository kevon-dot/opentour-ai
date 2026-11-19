from __future__ import annotations

from dataclasses import replace

import pytest
from opentour_cell.reconstruction.exact_join_engine import (
    JoinField,
    JoinInputError,
    JoinReason,
    JoinRecord,
    JoinResult,
    JoinScalar,
    JoinScope,
    JoinStatus,
    KeyKind,
    join_one_to_one,
)

SCOPE = JoinScope("org-a", "production", "source-1", "receipt", "snapshot-1")
FIELDS = (JoinField("entity", KeyKind.STRING), JoinField("operation", KeyKind.INTEGER))
QUERY: dict[str, JoinScalar] = {"entity": "entity-1", "operation": 42}


@pytest.mark.parametrize(
    "foreign_scope",
    [
        replace(SCOPE, organization_id="org-b"),
        replace(SCOPE, environment_id="test"),
        replace(SCOPE, connection_id="source-2"),
        replace(SCOPE, source_role="other-role"),
        replace(SCOPE, snapshot_id="snapshot-2"),
    ],
)
def test_exact_identity_is_fenced_by_every_source_dimension(foreign_scope: JoinScope) -> None:
    records = (
        JoinRecord(foreign_scope, "foreign-ref", QUERY),
        JoinRecord(SCOPE, "local-ref", QUERY),
    )

    assert join_one_to_one(QUERY, records, scope=SCOPE, fields=FIELDS) == JoinResult(
        JoinStatus.MATCHED, reference="local-ref"
    )
    assert join_one_to_one(QUERY, records[:1], scope=SCOPE, fields=FIELDS) == JoinResult(
        JoinStatus.MISSING, reason=JoinReason.NO_EXACT_MATCH
    )


def test_exact_composite_key_does_not_use_nearby_references() -> None:
    records = (
        JoinRecord(SCOPE, "other-op-ref", {"entity": "entity-1", "operation": 43}),
        JoinRecord(SCOPE, "other-entity-ref", {"entity": "entity-01", "operation": 42}),
        JoinRecord(SCOPE, "other-case-ref", {"entity": "Entity-1", "operation": 42}),
    )

    assert join_one_to_one(QUERY, records, scope=SCOPE, fields=FIELDS) == JoinResult(
        JoinStatus.MISSING, reason=JoinReason.NO_EXACT_MATCH
    )


def test_multiple_exact_rows_are_ambiguous_regardless_of_order() -> None:
    records = (JoinRecord(SCOPE, "ref-1", QUERY), JoinRecord(SCOPE, "ref-2", QUERY))
    expected = JoinResult(JoinStatus.AMBIGUOUS, reason=JoinReason.MULTIPLE_EXACT_MATCHES)

    assert join_one_to_one(QUERY, records, scope=SCOPE, fields=FIELDS) == expected
    assert join_one_to_one(QUERY, records[::-1], scope=SCOPE, fields=FIELDS) == expected


def test_missing_and_null_keys_are_distinct_and_never_match() -> None:
    records = (JoinRecord(SCOPE, "not-a-match", {"entity": "entity-1", "operation": None}),)

    assert join_one_to_one(
        {"entity": "entity-1"}, records, scope=SCOPE, fields=FIELDS
    ) == JoinResult(JoinStatus.MISSING, reason=JoinReason.MISSING_KEY)
    assert join_one_to_one(records[0].keys, records, scope=SCOPE, fields=FIELDS) == JoinResult(
        JoinStatus.MISSING, reason=JoinReason.NULL_KEY
    )
    assert join_one_to_one(QUERY, records, scope=SCOPE, fields=FIELDS) == JoinResult(
        JoinStatus.MISSING, reason=JoinReason.NO_EXACT_MATCH
    )


@pytest.mark.parametrize("encoded_number", [True, "1"])
def test_a_lookalike_scalar_kind_cannot_match_integer(encoded_number: bool | str) -> None:
    fields = (JoinField("operation", KeyKind.INTEGER),)
    records = (JoinRecord(SCOPE, "lookalike-ref", {"operation": encoded_number}),)

    assert join_one_to_one({"operation": 1}, records, scope=SCOPE, fields=fields) == JoinResult(
        JoinStatus.MISSING, reason=JoinReason.NO_EXACT_MATCH
    )
    with pytest.raises(JoinInputError, match="declared scalar type"):
        join_one_to_one({"operation": encoded_number}, records, scope=SCOPE, fields=fields)


def test_boolean_and_zero_integer_each_match_only_their_own_kind() -> None:
    records = (
        JoinRecord(SCOPE, "integer-ref", {"key": 0}),
        JoinRecord(SCOPE, "boolean-ref", {"key": False}),
    )

    assert (
        join_one_to_one(
            {"key": False}, records, scope=SCOPE, fields=(JoinField("key", KeyKind.BOOLEAN),)
        ).reference
        == "boolean-ref"
    )
    assert (
        join_one_to_one(
            {"key": 0}, records, scope=SCOPE, fields=(JoinField("key", KeyKind.INTEGER),)
        ).reference
        == "integer-ref"
    )


def test_empty_strings_match_only_if_explicitly_allowed() -> None:
    fields = (JoinField("entity", KeyKind.STRING, allow_empty=True),)
    records = (JoinRecord(SCOPE, "empty-ref", {"entity": ""}),)

    assert (
        join_one_to_one({"entity": ""}, records, scope=SCOPE, fields=fields).reference
        == "empty-ref"
    )
    with pytest.raises(JoinInputError, match="declared scalar type"):
        join_one_to_one({"entity": "", "operation": 42}, records, scope=SCOPE, fields=FIELDS)


@pytest.mark.parametrize("fields", [(), (FIELDS[0], FIELDS[0])])
def test_invalid_join_plan_refused(fields: tuple[JoinField, ...]) -> None:
    with pytest.raises(JoinInputError, match="nonempty and distinct"):
        join_one_to_one(QUERY, (), scope=SCOPE, fields=fields)
