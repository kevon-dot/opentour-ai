"""Screen prequalified dataset identifiers for exact cross-partition overlap."""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


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
