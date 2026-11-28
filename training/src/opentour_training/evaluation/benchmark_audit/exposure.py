"""Protected-answer exposure controls for charts, text, exports, tooltips and filters.

Every release path (private Slack and worker UI alike) goes through one guard. Any request whose
roles include selector or evaluator must not receive a cell, or a complementary/differencing
combination of cells, that isolates one protected family, unless the release is a logged,
authorized disclosure that retires the affected families. Whether a release is
answer-revealing depends on the cell released, not on whether a family is newly isolated: a
repeat release of an already-isolated cell is suppressed or logged like the first one.
Suppression and authorized disclosure are both logged; earlier as-of manifests are never
mutated.

Adversary model: released cell counts are pooled knowledge shared by every viewer, and answers
combine linearly. Per-viewer knowledge and bounds on 0/1 answers are not modelled.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from fractions import Fraction

from opentour_training.evaluation.benchmark_audit.model import (
    ExposureRecord,
    ReleaseEntry,
    canonical_digest,
)

PROTECTED_ROLES = frozenset({"evaluator", "selector"})


class Surface(StrEnum):
    CHART = "chart"
    ACCESSIBLE_TEXT = "accessible_text"
    EXPORT = "export"
    TOOLTIP = "tooltip"
    NL_FILTER = "nl_filter"


class Channel(StrEnum):
    PRIVATE_SLACK = "private_slack"
    WORKER_UI = "worker_ui"


@dataclass(frozen=True)
class QueryRequest:
    query_id: str
    surface: Surface
    channel: Channel
    cell: frozenset[str]  # protected families the worker resolved from the request
    roles: frozenset[str]


@dataclass(frozen=True)
class QueryDecision:
    allowed: bool
    reason: str
    isolated_digest: str | None
    log_index: int


@dataclass(frozen=True)
class LogEntry:
    index: int
    query_id: str
    surface: str
    channel: str
    cell_digest: str
    decision: str
    reason: str
    authorized_by: str | None


@dataclass(frozen=True)
class ProtectedManifest:
    version: int
    family_ids: frozenset[str]
    predecessor_digest: str | None = None

    def digest(self) -> str:
        return canonical_digest(
            {"v": self.version, "f": sorted(self.family_ids), "p": self.predecessor_digest}
        )


def isolated_families(released: list[frozenset[str]], universe: frozenset[str]) -> frozenset[str]:
    """Families a viewer could pin down from released cell counts, however they are combined.

    Each released cell (and the universe total) is an indicator row over the universe. A family
    is isolated exactly when its unit vector lies in the row space, which covers complements,
    nested differencing and non-nested combinations such as ``a+b``, ``b+c`` and ``a+c``.
    Exact rational elimination keeps the decision deterministic.
    """
    order = sorted(universe)
    rows = [[Fraction(1)] * len(order)]
    for cell in released:
        rows.append([Fraction(int(fid in cell)) for fid in order])
    pivots: list[int] = []
    rank = 0
    for col in range(len(order)):
        pick = next((r for r in range(rank, len(rows)) if rows[r][col] != 0), None)
        if pick is None:
            continue
        rows[rank], rows[pick] = rows[pick], rows[rank]
        lead = rows[rank][col]
        rows[rank] = [v / lead for v in rows[rank]]
        for r in range(len(rows)):
            if r != rank and rows[r][col] != 0:
                factor = rows[r][col]
                rows[r] = [a - factor * b for a, b in zip(rows[r], rows[rank], strict=True)]
        pivots.append(col)
        rank += 1
    found = {
        order[col]
        for row, col in zip(rows, pivots, strict=False)
        if sum(1 for v in row if v != 0) == 1
    }
    return frozenset(found)


def revealed_families(
    prior: Sequence[frozenset[str]], cell: frozenset[str], universe: frozenset[str]
) -> frozenset[str]:
    """Families that releasing ``cell`` lets a viewer pin down, judged by the cell itself.

    Earlier copies of ``cell`` are set aside first, so a repeat release is not masked by its own
    earlier release. The result is every family whose isolation needs this cell's count
    (complementary, nested and non-nested differencing) plus every family the cell isolates on
    its own with the universe total. A malformed cell is treated as revealing everything.
    """
    if not cell or not cell <= universe:
        return universe
    others = [c for c in prior if c != cell]
    with_cell = isolated_families([*others, cell], universe)
    alone = isolated_families([cell], universe)
    return (with_cell - isolated_families(others, universe)) | alone


def replay_exposure(record: ExposureRecord) -> tuple[frozenset[str], int]:
    """Re-derive exposed families and unlogged disclosures from a guard's release history.

    An authorized release exposes what it revealed. A release to a selector or evaluator that
    revealed a family without authorization is an unlogged case-level disclosure, and its
    families are exposed too.
    """
    released: list[frozenset[str]] = []
    exposed: set[str] = set()
    unlogged = 0
    for entry in record.releases:
        revealed = revealed_families(released, entry.cell, record.universe)
        if entry.authorized_by:
            exposed.update(revealed)
        elif entry.protected_role and revealed:
            unlogged += 1
            exposed.update(revealed)
        released.append(entry.cell)
    return frozenset(exposed), unlogged


class ExposureGuard:
    """Single release gate shared by every surface and channel."""

    def __init__(self, universe: frozenset[str], arm_disagreements: frozenset[str]) -> None:
        self._universe = universe
        self._arm_disagreements = arm_disagreements
        self._released: list[frozenset[str]] = []
        self._history: list[ReleaseEntry] = []
        self._log: list[LogEntry] = []
        self._exposed: set[str] = set()

    @property
    def log(self) -> tuple[LogEntry, ...]:
        return tuple(self._log)

    @property
    def exposed_families(self) -> frozenset[str]:
        return frozenset(self._exposed)

    def record(self) -> ExposureRecord:
        """The release history the audit replays to derive exposure itself."""
        return ExposureRecord(self._universe, tuple(self._history))

    def _record(self, request: QueryRequest, decision: str, reason: str, by: str | None) -> int:
        index = len(self._log)
        self._log.append(
            LogEntry(
                index,
                request.query_id,
                request.surface.value,
                request.channel.value,
                canonical_digest(sorted(request.cell)),
                decision,
                reason,
                by,
            )
        )
        return index

    def _revealed(self, request: QueryRequest) -> frozenset[str]:
        return revealed_families(self._released, request.cell, self._universe)

    def _append(self, request: QueryRequest, by: str | None) -> None:
        self._released.append(request.cell)
        protected = bool(request.roles & PROTECTED_ROLES)
        self._history.append(ReleaseEntry(request.cell, protected, by))

    def evaluate(self, request: QueryRequest) -> QueryDecision:
        protected = bool(request.roles & PROTECTED_ROLES)
        revealed = self._revealed(request)
        malformed = not request.cell or not request.cell <= self._universe
        if malformed or (protected and revealed):
            if malformed:
                reason = "suppressed_malformed_cell"
            elif revealed & self._arm_disagreements:
                reason = "suppressed_answer_revealing_arm_disagreement"
            else:
                reason = "suppressed_single_family_isolation"
            digest = canonical_digest(sorted(revealed))
            index = self._record(request, "suppressed", reason, None)
            return QueryDecision(False, reason, digest, index)
        self._append(request, None)
        if revealed:
            # Neither selector nor evaluator: released, and the isolating release is logged.
            reason = "released_isolating_cell_to_unprotected_role"
            index = self._record(request, "released", reason, None)
            return QueryDecision(True, reason, canonical_digest(sorted(revealed)), index)
        return QueryDecision(True, "released", None, self._record(request, "released", "ok", None))

    def authorize_disclosure(self, request: QueryRequest, authorized_by: str) -> QueryDecision:
        """Release an isolating cell with a logged authorization; affected families retire."""
        if not authorized_by.strip():
            raise ValueError("authorization requires a named authorizer")
        revealed = self._revealed(request)
        self._append(request, authorized_by)
        self._exposed.update(revealed)
        index = self._record(request, "authorized_disclosure", "authorized", authorized_by)
        return QueryDecision(
            True, "authorized_disclosure", canonical_digest(sorted(revealed)), index
        )

    def release(
        self, request: QueryRequest, compute: Callable[[frozenset[str]], object]
    ) -> tuple[QueryDecision, object | None]:
        """Run ``compute`` only for an allowed request, so suppression never touches answers."""
        decision = self.evaluate(request)
        return decision, (compute(request.cell) if decision.allowed else None)


def successor_protected_manifest(
    manifest: ProtectedManifest, exposed: frozenset[str]
) -> ProtectedManifest:
    """New manifest without exposed families; the predecessor object stays untouched."""
    return ProtectedManifest(
        version=manifest.version + 1,
        family_ids=manifest.family_ids - exposed,
        predecessor_digest=manifest.digest(),
    )
