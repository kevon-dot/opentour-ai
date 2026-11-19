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
    ManyJoinResult,
    ResolverContract,
    join_one_to_many,
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


RESOLVER = ResolverContract(
    SCOPE, "resolver-artifact", "revision-1", "retain-all", "descending-reference", "receipt-only"
)


class RecordingResolver:
    def __init__(
        self,
        contract: ResolverContract,
        output: tuple[str, ...] | None = None,
    ) -> None:
        self.contract = contract
        self.output = output
        self.calls: list[tuple[str, ...]] = []

    def order_references(self, references: tuple[str, ...]) -> tuple[str, ...]:
        self.calls.append(references)
        return references[::-1] if self.output is None else self.output


def test_one_to_many_uses_resolver_order_and_keeps_all_exact_references() -> None:
    records = (
        JoinRecord(SCOPE, "ref-z", {**QUERY, "detail": "simulated-local-row-detail"}),
        JoinRecord(replace(SCOPE, snapshot_id="other-snapshot"), "foreign-ref", QUERY),
        JoinRecord(SCOPE, "ref-a", QUERY),
        JoinRecord(SCOPE, "lookalike-ref", {"entity": "entity-1", "operation": "42"}),
    )
    resolver = RecordingResolver(RESOLVER)
    for order in (records, records[::-1]):
        assert join_one_to_many(
            QUERY, order, scope=SCOPE, fields=FIELDS, required_resolver=RESOLVER, resolver=resolver
        ) == ManyJoinResult(JoinStatus.MATCHED, ("ref-z", "ref-a"))
    assert resolver.calls == [("ref-a", "ref-z"), ("ref-a", "ref-z")]


@pytest.mark.parametrize(
    "wrong_binding",
    [
        replace(RESOLVER, reference="other-resolver"),
        replace(RESOLVER, revision="revision-2"),
        replace(RESOLVER, aggregation_rule="one-record"),
        replace(RESOLVER, order_rule="ascending-reference"),
        replace(RESOLVER, authority_rule="other-authority"),
        replace(RESOLVER, scope=replace(SCOPE, connection_id="other-source")),
    ],
)
def test_one_to_many_refuses_different_pinned_bindings(wrong_binding: ResolverContract) -> None:
    resolver = RecordingResolver(wrong_binding)
    with pytest.raises(JoinInputError, match="exact qualified resolver binding"):
        join_one_to_many(
            QUERY,
            (JoinRecord(SCOPE, "ref-a", QUERY),),
            scope=SCOPE,
            fields=FIELDS,
            required_resolver=RESOLVER,
            resolver=resolver,
        )
    assert resolver.calls == []


def test_one_to_many_has_no_resolver_default_even_for_zero_or_one_matches() -> None:
    with pytest.raises(JoinInputError, match="exact qualified resolver binding"):
        join_one_to_many(
            QUERY, (), scope=SCOPE, fields=FIELDS, required_resolver=RESOLVER, resolver=None
        )
    resolver = RecordingResolver(RESOLVER)
    with pytest.raises(JoinInputError, match="exact qualified resolver binding"):
        join_one_to_many(
            QUERY,
            (JoinRecord(SCOPE, "ref-a", QUERY),),
            scope=SCOPE,
            fields=FIELDS,
            required_resolver=None,
            resolver=resolver,
        )
    assert resolver.calls == []


@pytest.mark.parametrize("output", [("ref-a",), ("ref-a", "ref-a"), ("ref-a", "foreign-ref")])
def test_one_to_many_refuses_omitted_duplicate_or_foreign_references(
    output: tuple[str, ...],
) -> None:
    resolver = RecordingResolver(RESOLVER, output)
    records = (JoinRecord(SCOPE, "ref-a", QUERY), JoinRecord(SCOPE, "ref-b", QUERY))

    with pytest.raises(JoinInputError, match="preserve all exact references") as error:
        join_one_to_many(
            QUERY,
            records,
            scope=SCOPE,
            fields=FIELDS,
            required_resolver=RESOLVER,
            resolver=resolver,
        )
    assert "foreign-ref" not in str(error.value)


def test_one_to_many_missing_keys_and_no_matches_do_not_call_resolver() -> None:
    resolver = RecordingResolver(RESOLVER)
    missing_key: dict[str, JoinScalar] = {"entity": "entity-1"}
    for query, reason in (
        (missing_key, JoinReason.MISSING_KEY),
        (QUERY, JoinReason.NO_EXACT_MATCH),
    ):
        assert join_one_to_many(
            query, (), scope=SCOPE, fields=FIELDS, required_resolver=RESOLVER, resolver=resolver
        ) == ManyJoinResult(JoinStatus.MISSING, reason=reason)
    assert resolver.calls == []


def test_resolver_binding_read_failure_is_sanitized_before_dispatch() -> None:
    class UnavailableResolver:
        called = False

        @property
        def contract(self) -> ResolverContract:
            raise ValueError("simulated-resolver-binding-detail")

        def order_references(self, references: tuple[str, ...]) -> tuple[str, ...]:
            self.called = True
            return references

    resolver = UnavailableResolver()
    with pytest.raises(
        JoinInputError, match="qualified resolver binding could not be read"
    ) as error:
        join_one_to_many(
            QUERY,
            (JoinRecord(SCOPE, "ref-a", QUERY),),
            scope=SCOPE,
            fields=FIELDS,
            required_resolver=RESOLVER,
            resolver=resolver,
        )
    assert not resolver.called
    assert error.value.__suppress_context__
    assert "simulated-resolver-binding-detail" not in str(error.value)
