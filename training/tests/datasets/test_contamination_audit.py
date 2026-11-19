from __future__ import annotations

import hashlib
from dataclasses import replace
from itertools import permutations
from typing import cast

import pytest
from opentour_training.datasets.contamination_audit import (
    AuditInputError,
    AuditRow,
    BlockingOverlap,
    OverlapReason,
    Partition,
    PartitionMembers,
    audit_exact_overlap,
)

SCOPE = "audit-scope-1"


def generated(row_ref: str, partition: Partition, *, content: str, family: str) -> AuditRow:
    return AuditRow(SCOPE, row_ref, partition, hashlib.sha256(content.encode()).hexdigest(), family)


def test_renamed_content_copy_blocks_cross_partition_use() -> None:
    train = generated("train-ref", Partition.TRAIN, content="generated-copy", family="family-a")
    protected = generated(
        "protected-ref", Partition.PROTECTED, content="generated-copy", family="family-b"
    )
    report = audit_exact_overlap((train, protected), scope_id=SCOPE)

    assert report.has_blocking_overlap
    assert report.blocking_findings == (
        BlockingOverlap(
            OverlapReason.EXACT_CONTENT,
            (
                PartitionMembers(Partition.PROTECTED, ("protected-ref",)),
                PartitionMembers(Partition.TRAIN, ("train-ref",)),
            ),
        ),
    )
    assert train.exact_content_sha256 not in repr(report)
    assert train.family_id not in repr(report)


def test_shared_family_blocks_even_when_exact_content_differs() -> None:
    root = generated(
        "root-ref", Partition.TRAIN, content="generated-original", family="same-family"
    )
    child = generated(
        "child-ref", Partition.PROTECTED, content="generated-paraphrase", family="same-family"
    )
    report = audit_exact_overlap((root, child), scope_id=SCOPE)

    assert report.has_blocking_overlap
    assert tuple(finding.reason for finding in report.blocking_findings) == (
        OverlapReason.SHARED_FAMILY,
    )


def test_same_partition_duplicates_and_distinct_identifiers_do_not_create_hard_findings() -> None:
    rows = (
        generated("train-ref-1", Partition.TRAIN, content="generated-copy", family="family-a"),
        generated("train-ref-2", Partition.TRAIN, content="generated-copy", family="family-a"),
        generated(
            "protected-ref", Partition.PROTECTED, content="generated-close-copy", family="family-b"
        ),
    )
    report = audit_exact_overlap(rows, scope_id=SCOPE)

    assert report.blocking_findings == ()
    assert not report.has_blocking_overlap


def test_findings_keep_both_reasons_and_are_deterministic() -> None:
    rows = tuple(
        generated(f"row-{index}", partition, content="generated", family="one-family")
        for index, partition in enumerate(Partition)
    )
    expected = audit_exact_overlap(rows, scope_id=SCOPE)

    assert tuple(finding.reason for finding in expected.blocking_findings) == (
        OverlapReason.EXACT_CONTENT,
        OverlapReason.SHARED_FAMILY,
    )
    assert all(len(finding.members) == 4 for finding in expected.blocking_findings)
    for order in permutations(rows):
        assert audit_exact_overlap(order, scope_id=SCOPE) == expected


def test_large_overlap_is_one_group_with_linear_membership() -> None:
    rows = tuple(
        generated(f"row-{index:04}", partition, content="generated-copy", family=f"family-{index}")
        for index, partition in enumerate((Partition.TRAIN, Partition.PROTECTED) * 250)
    )
    report = audit_exact_overlap(rows, scope_id=SCOPE)

    assert len(report.blocking_findings) == 1
    assert tuple(len(group.row_refs) for group in report.blocking_findings[0].members) == (250, 250)


def test_foreign_scope_refused_without_disclosing_record() -> None:
    row = generated("train-ref", Partition.TRAIN, content="generated", family="family")
    foreign = replace(
        row, scope_id="other-scope", row_ref="foreign-ref", partition=Partition.PROTECTED
    )

    with pytest.raises(AuditInputError, match="foreign scope") as error:
        audit_exact_overlap((row, foreign), scope_id=SCOPE)
    assert "other-scope" not in str(error.value)
    assert "foreign-ref" not in str(error.value)


@pytest.mark.parametrize("identifier", ["", "not-a-sha256", "A" * 64])
def test_invalid_fingerprint_refused(identifier: str) -> None:
    row = generated("train-ref", Partition.TRAIN, content="generated", family="family")

    with pytest.raises(AuditInputError, match="lowercase SHA-256"):
        audit_exact_overlap((replace(row, exact_content_sha256=identifier),), scope_id=SCOPE)


def test_unknown_family_and_partition_cannot_be_treated_as_disjoint() -> None:
    row = generated("train-ref", Partition.TRAIN, content="generated", family="family")

    with pytest.raises(AuditInputError, match="family identifier"):
        audit_exact_overlap((replace(row, family_id=""),), scope_id=SCOPE)
    with pytest.raises(AuditInputError, match="partition is invalid"):
        audit_exact_overlap((replace(row, partition=cast(Partition, "train")),), scope_id=SCOPE)


def test_duplicate_opaque_row_reference_is_refused() -> None:
    row = generated("row-ref", Partition.TRAIN, content="generated", family="family")

    with pytest.raises(AuditInputError, match="distinct"):
        audit_exact_overlap((row, row), scope_id=SCOPE)


def test_same_immutable_row_in_two_partitions_is_blocking() -> None:
    row = generated("row-ref", Partition.TRAIN, content="generated", family="family")
    duplicate = replace(row, partition=Partition.PROTECTED)

    report = audit_exact_overlap((row, duplicate), scope_id=SCOPE)

    assert tuple(finding.reason for finding in report.blocking_findings) == (
        OverlapReason.EXACT_CONTENT,
        OverlapReason.SHARED_FAMILY,
    )
    with pytest.raises(AuditInputError, match="conflicting audit identifiers"):
        audit_exact_overlap((row, replace(duplicate, family_id="conflict")), scope_id=SCOPE)
