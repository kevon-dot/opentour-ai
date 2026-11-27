from __future__ import annotations

import hashlib
from dataclasses import replace
from itertools import permutations
from typing import cast

import pytest
from opentour_training.datasets.contamination_audit import (
    ApproximateCandidate,
    ApproximateProfile,
    ApproximateStatus,
    AuditInputError,
    AuditMember,
    AuditRow,
    BlockingOverlap,
    DetectorScan,
    OverlapReason,
    Partition,
    PartitionMembers,
    audit_exact_overlap,
    audit_overlap,
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


PROFILE = ApproximateProfile(SCOPE, "embedding-route-revision-1", "thresholds-revision-1", 4)
TRAIN_MEMBER = AuditMember(Partition.TRAIN, "train-ref")
PROTECTED_MEMBER = AuditMember(Partition.PROTECTED, "protected-ref")
SIMILAR_CANDIDATE = ApproximateCandidate(PROTECTED_MEMBER, TRAIN_MEMBER)
SCREEN_ROWS = (
    generated("train-ref", Partition.TRAIN, content="generated-a", family="family-a"),
    generated("protected-ref", Partition.PROTECTED, content="generated-similar", family="family-b"),
)


class RecordingDetector:
    def __init__(self, profile: ApproximateProfile, scan: DetectorScan | None) -> None:
        self.profile = profile
        self.scan = scan
        self.calls: list[tuple[AuditMember, ...]] = []

    def find_candidates(self, members: tuple[AuditMember, ...]) -> DetectorScan:
        self.calls.append(members)
        if self.scan is None:
            raise ValueError("simulated-local-detail")
        return self.scan


def test_approved_detector_keeps_candidates_unresolved_and_binds_profile() -> None:
    reverse = ApproximateCandidate(TRAIN_MEMBER, PROTECTED_MEMBER)
    detector = RecordingDetector(PROFILE, DetectorScan((SIMILAR_CANDIDATE, reverse), complete=True))
    report = audit_overlap(
        SCREEN_ROWS,
        scope_id=SCOPE,
        policy_permits_approximate_skip=False,
        expected_profile=PROFILE,
        detector=detector,
    )
    assert report.approximate_status is ApproximateStatus.CHECKED
    assert report.approximate_profile == PROFILE
    assert report.unresolved_candidates == (SIMILAR_CANDIDATE,)
    assert report.limitations == ("similarity_screening_cannot_prove_independence",)
    assert detector.calls == [(PROTECTED_MEMBER, TRAIN_MEMBER)]
    assert not report.exact.has_blocking_overlap


@pytest.mark.parametrize(
    "detector_profile",
    [
        replace(PROFILE, scope_id="other-scope"),
        replace(PROFILE, embedding_route_ref="different-route"),
        replace(PROFILE, thresholds_ref="different-thresholds"),
        replace(PROFILE, max_candidates=8),
    ],
)
def test_detector_must_match_exact_scope_route_thresholds_and_cap(
    detector_profile: ApproximateProfile,
) -> None:
    detector = RecordingDetector(detector_profile, DetectorScan((), complete=True))

    with pytest.raises(AuditInputError, match="exact qualified profile"):
        audit_overlap(
            SCREEN_ROWS,
            scope_id=SCOPE,
            policy_permits_approximate_skip=False,
            expected_profile=PROFILE,
            detector=detector,
        )
    assert detector.calls == []


def test_foreign_row_refused_before_scanning_even_with_approved_detector() -> None:
    detector = RecordingDetector(PROFILE, DetectorScan((), complete=True))
    foreign = replace(SCREEN_ROWS[0], scope_id="other-scope", row_ref="foreign-ref")

    with pytest.raises(AuditInputError, match="foreign scope"):
        audit_overlap(
            (foreign, SCREEN_ROWS[1]),
            scope_id=SCOPE,
            policy_permits_approximate_skip=False,
            expected_profile=PROFILE,
            detector=detector,
        )
    assert detector.calls == []


@pytest.mark.parametrize(
    "other",
    [
        AuditMember(Partition.TRAIN, "foreign-ref"),
        AuditMember(Partition.PROTECTED, "train-ref"),
        PROTECTED_MEMBER,
    ],
)
def test_detector_cannot_inject_foreign_wrong_partition_or_same_partition_candidates(
    other: AuditMember,
) -> None:
    scan = DetectorScan((ApproximateCandidate(PROTECTED_MEMBER, other),), complete=True)
    detector = RecordingDetector(PROFILE, scan)

    with pytest.raises(AuditInputError, match="outside the admitted comparison") as error:
        audit_overlap(
            SCREEN_ROWS,
            scope_id=SCOPE,
            policy_permits_approximate_skip=False,
            expected_profile=PROFILE,
            detector=detector,
        )
    assert "foreign-ref" not in str(error.value)


@pytest.mark.parametrize(
    "permit_skip,status",
    [(False, ApproximateStatus.REQUIRED_NOT_RUN), (True, ApproximateStatus.SKIPPED_BY_POLICY)],
)
def test_absent_detector_preserves_explicit_policy_outcome(
    permit_skip: bool, status: ApproximateStatus
) -> None:
    report = audit_overlap(
        SCREEN_ROWS,
        scope_id=SCOPE,
        policy_permits_approximate_skip=permit_skip,
        expected_profile=None,
        detector=None,
    )
    assert report.approximate_status is status
    assert status.value in report.limitations
    assert report.unresolved_candidates == ()


def test_missing_selected_detector_is_not_a_policy_skip() -> None:
    report = audit_overlap(
        SCREEN_ROWS,
        scope_id=SCOPE,
        policy_permits_approximate_skip=True,
        expected_profile=PROFILE,
        detector=None,
    )
    assert report.approximate_status is ApproximateStatus.REQUIRED_NOT_RUN
    assert report.approximate_profile == PROFILE


@pytest.mark.parametrize(
    "scan,status,unresolved",
    [
        (
            DetectorScan((SIMILAR_CANDIDATE,), False),
            ApproximateStatus.INCOMPLETE,
            (SIMILAR_CANDIDATE,),
        ),
        (DetectorScan((SIMILAR_CANDIDATE,) * 5, True), ApproximateStatus.LIMIT_EXCEEDED, ()),
        (None, ApproximateStatus.UNAVAILABLE, ()),
    ],
)
def test_detector_partial_over_budget_or_unavailable_retains_exact_blocks(
    scan: DetectorScan | None,
    status: ApproximateStatus,
    unresolved: tuple[ApproximateCandidate, ...],
) -> None:
    shared_content = replace(
        SCREEN_ROWS[0], exact_content_sha256=SCREEN_ROWS[1].exact_content_sha256
    )
    report = audit_overlap(
        (shared_content, SCREEN_ROWS[1]),
        scope_id=SCOPE,
        policy_permits_approximate_skip=False,
        expected_profile=PROFILE,
        detector=RecordingDetector(PROFILE, scan),
    )
    assert report.exact.has_blocking_overlap
    assert report.approximate_status is status
    assert report.unresolved_candidates == unresolved
    assert status.value in report.limitations
    assert "simulated-local-detail" not in repr(report)


def test_detector_binding_read_failure_preserves_hard_blocks_without_dispatch() -> None:
    class UnavailableDetector:
        called = False

        @property
        def profile(self) -> ApproximateProfile:
            raise ValueError("simulated-detector-binding-detail")

        def find_candidates(self, members: tuple[AuditMember, ...]) -> DetectorScan:
            self.called = True
            return DetectorScan((), complete=True)

    detector = UnavailableDetector()
    shared_content = replace(
        SCREEN_ROWS[0], exact_content_sha256=SCREEN_ROWS[1].exact_content_sha256
    )
    report = audit_overlap(
        (shared_content, SCREEN_ROWS[1]),
        scope_id=SCOPE,
        policy_permits_approximate_skip=False,
        expected_profile=PROFILE,
        detector=detector,
    )
    assert not detector.called
    assert report.exact.has_blocking_overlap
    assert report.approximate_status is ApproximateStatus.UNAVAILABLE
    assert report.approximate_profile == PROFILE
    assert "simulated-detector-binding-detail" not in repr(report)
