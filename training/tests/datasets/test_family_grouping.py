from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from itertools import pairwise, permutations
from typing import cast

import pytest
from opentour_training.datasets.family_grouping import (
    ExclusionReason,
    Family,
    FamilyExclusion,
    GroupingInputError,
    GroupingKey,
    GroupingKeyKind,
    GroupingRow,
    GroupingScope,
    group_families,
)

SCOPE = GroupingScope("org-é", "workspace", "project", "environment", "cell")
POLICY_REVISION = "policy-1"


def root_reference(row_id: str) -> bytes:
    # Generated full ArtifactRef, canonical bytes as supplied by the caller.
    return json.dumps(
        {
            "artifact_id": row_id,
            "kind": "case",
            "revision": "revision-1",
            "sha256": "0" * 64,
            "manifest_id": "manifest",
            "environment_id": SCOPE.environment_id,
            "classification": "customer_confidential",
            "purpose_grant_id": "purpose-grant",
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def real(row_id: str, *keys: GroupingKey) -> GroupingRow:
    return GroupingRow(row_id, SCOPE, root_reference(row_id), keys=keys)


def key(kind: GroupingKeyKind, value: str, namespace: str = "source") -> GroupingKey:
    return GroupingKey(kind, namespace, value)


def test_fg01_descendants_share_one_verified_family() -> None:
    rows = (
        real("real"),
        GroupingRow("paraphrase-a", SCOPE, parent_row_ids=("real",)),
        GroupingRow("paraphrase-b", SCOPE, parent_row_ids=("paraphrase-a",)),
        GroupingRow("paraphrase-c", SCOPE, parent_row_ids=("real",)),
        GroupingRow("synthetic-a", SCOPE, parent_row_ids=("paraphrase-b",)),
        GroupingRow("synthetic-b", SCOPE, parent_row_ids=("synthetic-a", "paraphrase-c")),
    )
    result = group_families(
        rows, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=1
    )

    assert result.independent_family_count == 1
    assert result.families[0].row_ids == tuple(sorted(row.row_id for row in rows))
    assert result.exclusions == ()
    assert not result.insufficient_independent_families
    assert "purpose-grant" not in repr(result)


def test_fg01_transitive_exact_links_across_all_supported_kinds() -> None:
    grouping_keys = tuple(key(kind, "identity") for kind in GroupingKeyKind)
    rows = (
        real("first", grouping_keys[0]),
        *(
            real(f"link-{index}", left, right)
            for index, (left, right) in enumerate(pairwise(grouping_keys))
        ),
        real("last", grouping_keys[-1]),
    )
    result = group_families(
        rows, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=1
    )

    assert result.independent_family_count == 1
    assert result.families[0].row_ids == tuple(sorted(row.row_id for row in rows))


def test_fg01_same_verified_root_joins_distinct_row_ids() -> None:
    rows = (real("source"), GroupingRow("copy", SCOPE, root_reference("source")))
    result = group_families(
        rows, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=1
    )

    assert result.independent_family_count == 1
    assert result.families[0].row_ids == ("copy", "source")


def test_fg02_large_component_does_not_satisfy_minimum() -> None:
    operation = key(GroupingKeyKind.OPERATION, "same-operation")
    rows = tuple(real(f"row-{index:03}", operation) for index in range(120))
    result = group_families(
        rows, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=2
    )

    assert result.independent_family_count == 1
    assert result.insufficient_independent_families
    assert result.families[0].row_ids == tuple(row.row_id for row in rows)


def test_fg03_cross_window_family_is_permutation_stable_and_hash_is_canonical() -> None:
    document = key(GroupingKeyKind.DOCUMENT, "repeat-document")
    earlier_rows = (real("earlier", document), real("unrelated"))
    later_rows = (
        real("later", document),
        GroupingRow("later-copy", SCOPE, parent_row_ids=("earlier",)),
    )
    rows = earlier_rows + later_rows
    result = group_families(
        rows, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=2
    )

    expected_payload = json.dumps(
        [
            ["org-é", "workspace", "project", "environment", "cell"],
            POLICY_REVISION,
            [json.loads(root_reference("earlier")), json.loads(root_reference("later"))],
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    expected_family = Family(
        hashlib.sha256(expected_payload).hexdigest(), ("earlier", "later", "later-copy")
    )
    assert expected_family in result.families
    assert not result.insufficient_independent_families
    for order in permutations(rows):
        assert (
            group_families(
                order, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=2
            )
            == result
        )


def test_fg03_root_equivalence_stable_under_descendant_addition_but_bound_to_scope_and_policy() -> (
    None
):
    root = real("root")
    descendant = GroupingRow("new-copy", SCOPE, parent_row_ids=("root",))
    first = group_families(
        (root,), scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=1
    )
    second = group_families(
        (descendant, root),
        scope=SCOPE,
        policy_revision=POLICY_REVISION,
        minimum_independent_families=1,
    )
    new_policy = group_families(
        (root,), scope=SCOPE, policy_revision="policy-2", minimum_independent_families=1
    )
    other_scope = replace(SCOPE, organization_id="other-org")
    new_scope = group_families(
        (replace(root, scope=other_scope),),
        scope=other_scope,
        policy_revision=POLICY_REVISION,
        minimum_independent_families=1,
    )

    assert first.families[0].family_id == second.families[0].family_id
    assert (
        len(
            {
                first.families[0].family_id,
                new_policy.families[0].family_id,
                new_scope.families[0].family_id,
            }
        )
        == 3
    )


def test_fg04_excludes_rootless_rows_and_unresolved_component() -> None:
    operation = key(GroupingKeyKind.OPERATION, "operation")
    rows = (
        real("known-but-connected", operation),
        GroupingRow("missing-parent", SCOPE, parent_row_ids=("unavailable",), keys=(operation,)),
        GroupingRow("same-missing-parent", SCOPE, parent_row_ids=("unavailable",)),
        GroupingRow("missing-parent-child", SCOPE, parent_row_ids=("missing-parent",)),
        GroupingRow("rootless", SCOPE),
        real("unaffected"),
    )
    result = group_families(
        rows, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=2
    )

    assert result.families[0].row_ids == ("unaffected",)
    assert result.exclusions == (
        FamilyExclusion(
            (
                "known-but-connected",
                "missing-parent",
                "missing-parent-child",
                "same-missing-parent",
            ),
            (ExclusionReason.UNRESOLVED_ANCESTRY,),
        ),
        FamilyExclusion(("rootless",), (ExclusionReason.UNRESOLVED_ANCESTRY,)),
    )
    assert result.independent_family_count == 1
    assert result.insufficient_independent_families


def test_fg04_cycle_excludes_whole_component_even_with_a_verified_root() -> None:
    rows = (
        real("root"),
        GroupingRow("cycle-a", SCOPE, parent_row_ids=("root", "cycle-b")),
        GroupingRow("cycle-b", SCOPE, parent_row_ids=("cycle-a",)),
        GroupingRow("dependent", SCOPE, parent_row_ids=("cycle-a",)),
        GroupingRow("self-cycle", SCOPE, parent_row_ids=("self-cycle",)),
    )
    result = group_families(
        rows, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=1
    )

    assert result.families == ()
    assert result.exclusions == (
        FamilyExclusion(
            ("cycle-a", "cycle-b", "dependent", "root"), (ExclusionReason.CYCLIC_ANCESTRY,)
        ),
        FamilyExclusion(("self-cycle",), (ExclusionReason.CYCLIC_ANCESTRY,)),
    )
    assert result.insufficient_independent_families


def test_fg04_deep_ancestry_grouping_is_iterative() -> None:
    rows = (real("row-0000"),) + tuple(
        GroupingRow(f"row-{index:04}", SCOPE, parent_row_ids=(f"row-{index - 1:04}",))
        for index in range(1, 2_000)
    )
    result = group_families(
        rows[::-1], scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=1
    )

    assert result.families[0].row_ids == tuple(row.row_id for row in rows)
    assert result.exclusions == ()


def test_fg04_exact_keys_are_typed_and_namespaced() -> None:
    rows = (
        real("document-source-a", key(GroupingKeyKind.DOCUMENT, "same", "source-a")),
        real("operation-source-a", key(GroupingKeyKind.OPERATION, "same", "source-a")),
        real("document-source-b", key(GroupingKeyKind.DOCUMENT, "same", "source-b")),
        real("case-distinct", key(GroupingKeyKind.DOCUMENT, "Same", "source-a")),
    )
    result = group_families(
        rows, scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=4
    )

    assert result.independent_family_count == 4
    assert not result.insufficient_independent_families


@pytest.mark.parametrize(
    "namespace,value", [(1, "valid"), ("valid", True), ("", "valid"), ("valid", "")]
)
def test_fg04_invalid_key_identity_refused(namespace: object, value: object) -> None:
    invalid_key = GroupingKey(GroupingKeyKind.OPERATION, cast(str, namespace), cast(str, value))

    with pytest.raises(GroupingInputError, match="exact identity"):
        group_families(
            (real("known", invalid_key),),
            scope=SCOPE,
            policy_revision=POLICY_REVISION,
            minimum_independent_families=1,
        )


def test_fg04_mixed_scope_is_refused_without_disclosing_row() -> None:
    row = real("scoped", key(GroupingKeyKind.ENTITY, "shared"))
    foreign = replace(
        row, row_id="foreign-row", scope=replace(SCOPE, organization_id="foreign-org")
    )

    with pytest.raises(GroupingInputError, match="foreign scope") as error:
        group_families(
            (row, foreign),
            scope=SCOPE,
            policy_revision=POLICY_REVISION,
            minimum_independent_families=1,
        )
    assert "foreign-row" not in str(error.value)
    assert "foreign-org" not in str(error.value)


def test_fg04_duplicate_row_id_is_refused() -> None:
    row = real("same-id")
    duplicate = replace(row, root_reference=root_reference("different-root"))

    with pytest.raises(GroupingInputError, match="unique"):
        group_families(
            (row, duplicate),
            scope=SCOPE,
            policy_revision=POLICY_REVISION,
            minimum_independent_families=1,
        )


def test_fg04_empty_input_reports_insufficiency() -> None:
    result = group_families(
        (), scope=SCOPE, policy_revision=POLICY_REVISION, minimum_independent_families=1
    )

    assert result.independent_family_count == 0
    assert result.exclusions == ()
    assert result.insufficient_independent_families


@pytest.mark.parametrize("minimum", [False, 0, -1, 9_007_199_254_740_992])
def test_fg04_invalid_minimum_is_refused(minimum: int) -> None:
    with pytest.raises(GroupingInputError, match="positive integer"):
        group_families(
            (real("known"),),
            scope=SCOPE,
            policy_revision=POLICY_REVISION,
            minimum_independent_families=minimum,
        )
