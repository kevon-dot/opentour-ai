"""Screen prequalified dataset identifiers for cross-partition overlap."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol


class AuditInputError(ValueError):
    """An invalid normalized audit population."""


class Partition(StrEnum):
    TRAIN = "train"
    DEVELOPMENT = "development"
    PROTECTED = "protected"
    DISCLOSED_RETENTION = "disclosed_retention"


class OverlapReason(StrEnum):
    EXACT_CONTENT = "exact_content"
    SHARED_FAMILY = "shared_family"


@dataclass(frozen=True, slots=True)
class AuditRow:
    scope_id: str
    row_ref: str
    partition: Partition
    exact_content_sha256: str = field(repr=False)
    family_id: str = field(repr=False)


@dataclass(frozen=True, order=True, slots=True)
class PartitionMembers:
    partition: Partition
    row_refs: tuple[str, ...]


@dataclass(frozen=True, order=True, slots=True)
class BlockingOverlap:
    reason: OverlapReason
    members: tuple[PartitionMembers, ...]


@dataclass(frozen=True, slots=True)
class ExactOverlapReport:
    """Hard findings from the exact detectors, with no similarity assessment."""

    blocking_findings: tuple[BlockingOverlap, ...]

    @property
    def has_blocking_overlap(self) -> bool:
        return bool(self.blocking_findings)


class ApproximateStatus(StrEnum):
    CHECKED = "checked"
    SKIPPED_BY_POLICY = "skipped_by_policy"
    REQUIRED_NOT_RUN = "required_not_run"
    INCOMPLETE = "incomplete"
    LIMIT_EXCEEDED = "limit_exceeded"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class ApproximateProfile:
    """Exact caller-qualified scope, embedding route, thresholds, and budget."""

    scope_id: str
    embedding_route_ref: str
    thresholds_ref: str
    max_candidates: int

    def __post_init__(self) -> None:
        identifiers = (self.scope_id, self.embedding_route_ref, self.thresholds_ref)
        if any(type(value) is not str or not value for value in identifiers):
            raise AuditInputError("A complete qualified approximate detector profile is required")
        if type(self.max_candidates) is not int or self.max_candidates < 1:
            raise AuditInputError("The approximate candidate limit must be a positive integer")


@dataclass(frozen=True, order=True, slots=True)
class AuditMember:
    partition: Partition
    row_ref: str


@dataclass(frozen=True, order=True, slots=True)
class ApproximateCandidate:
    left: AuditMember
    right: AuditMember


@dataclass(frozen=True, slots=True)
class DetectorScan:
    candidates: tuple[ApproximateCandidate, ...]
    complete: bool


class QualifiedApproximateDetector(Protocol):
    @property
    def profile(self) -> ApproximateProfile: ...

    def find_candidates(self, members: tuple[AuditMember, ...]) -> DetectorScan:
        """Screen only the supplied members using the bound tenant-local profile."""
        ...


@dataclass(frozen=True, slots=True)
class OverlapReport:
    exact: ExactOverlapReport
    approximate_status: ApproximateStatus
    approximate_profile: ApproximateProfile | None = None
    unresolved_candidates: tuple[ApproximateCandidate, ...] = ()

    @property
    def limitations(self) -> tuple[str, ...]:
        base = ("similarity_screening_cannot_prove_independence",)
        if self.approximate_status is ApproximateStatus.CHECKED:
            return base
        return (*base, self.approximate_status.value)


_SHA256 = re.compile(r"[0-9a-f]{64}")


def _overlap_groups(
    rows_by_identifier: dict[str, list[AuditRow]], reason: OverlapReason
) -> list[BlockingOverlap]:
    findings = []
    for rows in rows_by_identifier.values():
        if len(rows) < 2:
            continue
        by_partition: dict[Partition, list[str]] = defaultdict(list)
        for row in rows:
            by_partition[row.partition].append(row.row_ref)
        if len(by_partition) > 1:
            members = tuple(
                PartitionMembers(partition, tuple(sorted(row_refs)))
                for partition, row_refs in sorted(by_partition.items())
            )
            findings.append(BlockingOverlap(reason, members))
    return findings


def audit_exact_overlap(rows: Sequence[AuditRow], *, scope_id: str) -> ExactOverlapReport:
    """Find exact overlaps across distinct partitions of one approved scope.

    The caller verifies ownership and comparability of the fingerprint recipe
    and family policy before supplying rows. Each blocking group keeps its
    members by partition without expanding all cross-partition pairs. An empty
    result has no implication for near-duplicate detection or release gates.
    """
    if type(scope_id) is not str or not scope_id:
        raise AuditInputError("An approved audit scope is required")

    memberships: set[tuple[Partition, str]] = set()
    row_identities: dict[str, tuple[str, str]] = {}
    by_digest: dict[str, list[AuditRow]] = defaultdict(list)
    by_family: dict[str, list[AuditRow]] = defaultdict(list)
    for row in rows:
        if type(row.scope_id) is not str or row.scope_id != scope_id:
            raise AuditInputError("The audit population contains a foreign scope")
        if type(row.row_ref) is not str or not row.row_ref:
            raise AuditInputError("Opaque row references must be nonempty")
        if not isinstance(row.partition, Partition):
            raise AuditInputError("The audit partition is invalid")
        if type(row.exact_content_sha256) is not str or not _SHA256.fullmatch(
            row.exact_content_sha256
        ):
            raise AuditInputError("A qualified lowercase SHA-256 fingerprint is required")
        if type(row.family_id) is not str or not row.family_id:
            raise AuditInputError("A qualified family identifier is required")
        membership = (row.partition, row.row_ref)
        if membership in memberships:
            raise AuditInputError("Opaque row references must be distinct within each partition")
        identity = (row.exact_content_sha256, row.family_id)
        if row_identities.setdefault(row.row_ref, identity) != identity:
            raise AuditInputError("An immutable row reference has conflicting audit identifiers")
        memberships.add(membership)
        by_digest[row.exact_content_sha256].append(row)
        by_family[row.family_id].append(row)

    findings = _overlap_groups(by_digest, OverlapReason.EXACT_CONTENT)
    findings.extend(_overlap_groups(by_family, OverlapReason.SHARED_FAMILY))
    return ExactOverlapReport(tuple(sorted(findings)))


def _verified_candidate_pairs(
    candidates: tuple[ApproximateCandidate, ...], members: set[AuditMember]
) -> tuple[ApproximateCandidate, ...]:
    pairs: set[ApproximateCandidate] = set()
    for candidate in candidates:
        if type(candidate) is not ApproximateCandidate:
            raise AuditInputError("The detector returned an invalid candidate")
        left, right = candidate.left, candidate.right
        if (
            type(left) is not AuditMember
            or type(right) is not AuditMember
            or not isinstance(left.partition, Partition)
            or not isinstance(right.partition, Partition)
            or type(left.row_ref) is not str
            or type(right.row_ref) is not str
            or left.partition is right.partition
            or left not in members
            or right not in members
        ):
            raise AuditInputError(
                "The detector returned a candidate outside the admitted comparison"
            )
        if right < left:
            left, right = right, left
        pairs.add(ApproximateCandidate(left, right))
    return tuple(sorted(pairs))


def audit_overlap(
    rows: Sequence[AuditRow],
    *,
    scope_id: str,
    policy_permits_approximate_skip: bool,
    expected_profile: ApproximateProfile | None,
    detector: QualifiedApproximateDetector | None,
) -> OverlapReport:
    """Combine hard matches and unresolved signals from an approved local detector.

    The caller verifies the actual detector, model configuration, thresholds,
    isolation, and policy. This layer binds the declared profiles exactly and
    refuses foreign inputs before calling the detector. It never grants approval.
    """
    if type(policy_permits_approximate_skip) is not bool:
        raise AuditInputError("The approximate-skip policy must be explicit")
    exact = audit_exact_overlap(rows, scope_id=scope_id)
    if expected_profile is not None and expected_profile.scope_id != scope_id:
        raise AuditInputError("The approximate detector must match the exact qualified profile")
    if detector is None:
        status = (
            ApproximateStatus.SKIPPED_BY_POLICY
            if expected_profile is None and policy_permits_approximate_skip
            else ApproximateStatus.REQUIRED_NOT_RUN
        )
        return OverlapReport(exact, status, expected_profile)

    if expected_profile is None:
        raise AuditInputError("The approximate detector must match the exact qualified profile")
    try:
        actual_profile = detector.profile
    except Exception:
        return OverlapReport(exact, ApproximateStatus.UNAVAILABLE, expected_profile)
    if type(actual_profile) is not ApproximateProfile or actual_profile != expected_profile:
        raise AuditInputError("The approximate detector must match the exact qualified profile")

    members = tuple(sorted(AuditMember(row.partition, row.row_ref) for row in rows))
    try:
        scan = detector.find_candidates(members)
    except Exception:
        return OverlapReport(exact, ApproximateStatus.UNAVAILABLE, expected_profile)
    if (
        type(scan) is not DetectorScan
        or type(scan.complete) is not bool
        or type(scan.candidates) is not tuple
    ):
        raise AuditInputError("The detector returned an invalid scan receipt")
    if len(scan.candidates) > expected_profile.max_candidates:
        return OverlapReport(exact, ApproximateStatus.LIMIT_EXCEEDED, expected_profile)

    candidates = _verified_candidate_pairs(scan.candidates, set(members))
    status = ApproximateStatus.CHECKED if scan.complete else ApproximateStatus.INCOMPLETE
    return OverlapReport(exact, status, expected_profile, candidates)
