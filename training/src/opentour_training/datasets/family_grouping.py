"""Group caller-verified, immutable dataset metadata before assigning any split."""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum


class GroupingInputError(ValueError):
    """The normalized grouping input violates a component invariant."""


class GroupingKeyKind(StrEnum):
    ROOT_CONVERSATION = "root_conversation"
    OPERATION = "operation"
    ENTITY = "entity"
    DOCUMENT = "document"
    TEMPLATE = "template"


class ExclusionReason(StrEnum):
    UNRESOLVED_ANCESTRY = "unresolved_ancestry"
    CYCLIC_ANCESTRY = "cyclic_ancestry"


@dataclass(frozen=True, slots=True)
class GroupingScope:
    organization_id: str
    workspace_id: str
    project_id: str
    environment_id: str
    cell_id: str


@dataclass(frozen=True, slots=True)
class GroupingKey:
    kind: GroupingKeyKind
    namespace: str
    value: str


@dataclass(frozen=True, slots=True)
class GroupingRow:
    """Opaque row ID, exact policy-selected keys and caller-verified ancestry.

    A root reference contains canonical UTF-8 ArtifactRef bytes. The caller
    validates the reference and verifies its association with the row. Parent
    IDs identify exact rows in the same snapshot. A derivative with missing
    ancestry must not be represented by a self-assigned root reference.
    """

    row_id: str
    scope: GroupingScope
    root_reference: bytes | None = field(default=None, repr=False)
    parent_row_ids: tuple[str, ...] = ()
    keys: tuple[GroupingKey, ...] = ()


@dataclass(frozen=True, slots=True)
class Family:
    family_id: str
    row_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FamilyExclusion:
    row_ids: tuple[str, ...]
    reasons: tuple[ExclusionReason, ...]


@dataclass(frozen=True, slots=True)
class GroupingResult:
    families: tuple[Family, ...]
    exclusions: tuple[FamilyExclusion, ...]
    minimum_independent_families: int

    @property
    def independent_family_count(self) -> int:
        return len(self.families)

    @property
    def insufficient_independent_families(self) -> bool:
        return self.independent_family_count < self.minimum_independent_families


class _Components:
    def __init__(self, count: int) -> None:
        self.parents = list(range(count))
        self.sizes = [1] * count

    def find(self, index: int) -> int:
        while self.parents[index] != index:
            self.parents[index] = self.parents[self.parents[index]]
            index = self.parents[index]
        return index

    def union(self, left: int, right: int) -> None:
        left, right = self.find(left), self.find(right)
        if left == right:
            return
        if self.sizes[left] < self.sizes[right]:
            left, right = right, left
        self.parents[right] = left
        self.sizes[left] += self.sizes[right]


def _family_id(scope: GroupingScope, policy_revision: str, roots: tuple[bytes, ...]) -> str:
    # SHA-256 of canonical JSON [scope, policy revision, sorted ArtifactRefs].
    # This prefix uses only arrays of strings. The caller canonicalizes refs.
    scope_parts = (
        scope.organization_id,
        scope.workspace_id,
        scope.project_id,
        scope.environment_id,
        scope.cell_id,
    )
    prefix = json.dumps((scope_parts, policy_revision), ensure_ascii=False, separators=(",", ":"))
    digest = hashlib.sha256(prefix[:-1].encode("utf-8"))
    digest.update(b",[")
    for index, root in enumerate(roots):
        if index:
            digest.update(b",")
        digest.update(root)
    digest.update(b"]]")
    return digest.hexdigest()


def _remove_acyclic_rows(degrees: list[int], children: dict[int, list[int]]) -> None:
    """Peel the ancestry DAG. Residual rows belong to or depend on a cycle."""
    ready = deque(index for index, degree in enumerate(degrees) if degree == 0)
    while ready:
        for child in children.get(ready.popleft(), ()):
            degrees[child] -= 1
            if degrees[child] == 0:
                ready.append(child)


def group_families(
    rows: Sequence[GroupingRow],
    *,
    scope: GroupingScope,
    policy_revision: str,
    minimum_independent_families: int,
) -> GroupingResult:
    """Compute complete families over one validated snapshot, across all windows.

    The exclude policy quarantines an entire connected component when any
    ancestry is unproved. The count is scoped to the supplied grouping policy
    and says nothing about whether a particular split is feasible.
    """
    if type(policy_revision) is not str or not policy_revision:
        raise GroupingInputError("A frozen grouping policy revision is required")
    if (
        type(minimum_independent_families) is not int
        or not 1 <= minimum_independent_families <= 9_007_199_254_740_991
    ):
        raise GroupingInputError("The minimum independent family count must be a positive integer")

    row_indexes: dict[str, int] = {}
    for index, row in enumerate(rows):
        if row.scope != scope:
            raise GroupingInputError("Grouping input contains a foreign scope")
        if type(row.row_id) is not str or not row.row_id or row.row_id in row_indexes:
            raise GroupingInputError("Grouping row identifiers must be nonempty and unique")
        row_indexes[row.row_id] = index

    components = _Components(len(rows))
    degrees = [0] * len(rows)
    children: dict[int, list[int]] = defaultdict(list)
    root_owners: dict[bytes, int] = {}
    key_owners: dict[GroupingKey, int] = {}
    missing_parent_owners: dict[str, int] = {}
    unresolved: set[int] = set()
    for index, row in enumerate(rows):
        if row.root_reference is not None:
            if type(row.root_reference) is not bytes or not row.root_reference:
                raise GroupingInputError("A supplied root reference must contain canonical bytes")
            components.union(index, root_owners.setdefault(row.root_reference, index))
        elif not row.parent_row_ids:
            unresolved.add(index)

        for key in row.keys:
            if (
                not isinstance(key.kind, GroupingKeyKind)
                or type(key.namespace) is not str
                or not key.namespace
                or type(key.value) is not str
                or not key.value
            ):
                raise GroupingInputError(
                    "Grouping keys require an approved kind and exact identity"
                )
            components.union(index, key_owners.setdefault(key, index))

        for parent_id in row.parent_row_ids:
            parent = row_indexes.get(parent_id)
            if parent is None:
                unresolved.add(index)
                components.union(index, missing_parent_owners.setdefault(parent_id, index))
                continue
            components.union(index, parent)
            children[parent].append(index)
            degrees[index] += 1

    _remove_acyclic_rows(degrees, children)
    members: dict[int, list[int]] = defaultdict(list)
    for index in range(len(rows)):
        members[components.find(index)].append(index)

    families: list[Family] = []
    exclusions: list[FamilyExclusion] = []
    for indexes in members.values():
        row_ids = tuple(sorted(rows[index].row_id for index in indexes))
        reasons = []
        if any(index in unresolved for index in indexes):
            reasons.append(ExclusionReason.UNRESOLVED_ANCESTRY)
        if any(degrees[index] for index in indexes):
            reasons.append(ExclusionReason.CYCLIC_ANCESTRY)
        if reasons:
            exclusions.append(FamilyExclusion(row_ids, tuple(reasons)))
            continue

        roots = tuple(sorted({root for index in indexes if (root := rows[index].root_reference)}))
        families.append(Family(_family_id(scope, policy_revision, roots), row_ids))

    return GroupingResult(
        families=tuple(sorted(families, key=lambda family: family.family_id)),
        exclusions=tuple(sorted(exclusions, key=lambda exclusion: exclusion.row_ids)),
        minimum_independent_families=minimum_independent_families,
    )
