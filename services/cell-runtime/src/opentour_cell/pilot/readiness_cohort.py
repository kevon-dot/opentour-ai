"""Project already authorized readiness and verified expert-effort receipts."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from fractions import Fraction
from statistics import median
from typing import Literal

from opentour_cell.pilot.intake_mcp import require_ref
from opentour_cell.pilot.operating_point import digest

Disposition = Literal["held", "fit_ready", "simple_resolved", "withdrawn", "disputed"]
_DISPOSITIONS = {"held", "fit_ready", "simple_resolved", "withdrawn", "disputed"}
SCHEMA = "opentour.pilot.readiness-cohort.v1"


def _require(condition: object) -> None:
    if not condition:
        raise ValueError("readiness_cohort_evidence_unavailable")


def _time(value: int) -> bool:
    return type(value) is int and 0 <= value < 2**63


@dataclass(frozen=True)
class CohortWorkflow:
    """One workflow selected by the current reporting authority."""

    tenant: str
    workflow: str
    engagement: str
    first_seed_at: int | None
    seed_receipt: str | None
    policy_only: bool = False

    @property
    def scope(self) -> tuple[str, str, str]:
        return self.tenant, self.workflow, self.engagement

    def __post_init__(self) -> None:
        for ref in self.scope:
            require_ref(ref)
        _require(type(self.policy_only) is bool)
        _require((self.first_seed_at is None) == (self.seed_receipt is None))
        if self.first_seed_at is not None:
            _require(not self.policy_only and _time(self.first_seed_at))
            require_ref(self.seed_receipt)


@dataclass(frozen=True)
class ReadinessTransition:
    scope: tuple[str, str, str]
    receipt_ref: str
    revision: int
    effective_at: int
    recorded_at: int
    disposition: Disposition

    def __post_init__(self) -> None:
        _require(len(self.scope) == 3)
        for ref in (*self.scope, self.receipt_ref):
            require_ref(ref)
        _require(type(self.revision) is int and self.revision > 0)
        _require(
            _time(self.effective_at)
            and _time(self.recorded_at)
            and self.effective_at <= self.recorded_at
            and self.disposition in _DISPOSITIONS
        )


@dataclass(frozen=True)
class CohortReadout:
    schema_version: str
    window_start: int
    window_end: int
    cutoff: int
    source_revision: str
    workflows: int
    ever_fit_ready: int
    current: tuple[tuple[str, int], ...]
    policy_only: int
    unknown_seed: int
    median_seconds_to_ready: int | float | None
    pending_ages_seconds: tuple[int, ...]

    @property
    def revision(self) -> str:
        return digest(asdict(self))


def project_cohort(
    workflows: Sequence[CohortWorkflow],
    transitions: Sequence[ReadinessTransition],
    *,
    window_start: int,
    window_end: int,
    cutoff: int,
    may_disclose: bool,
) -> CohortReadout | None:
    """Project one consented cohort. Callers authenticate and scope inputs before use."""
    if may_disclose is not True:
        return None
    _require(all(_time(value) for value in (window_start, window_end, cutoff)))
    _require(window_start < window_end <= cutoff)
    unique: dict[tuple[str, str], CohortWorkflow] = {}
    for workflow in workflows:
        key = (workflow.tenant, workflow.workflow)
        _require(key not in unique or unique[key] == workflow)
        unique[key] = workflow

    allowed_scopes = {workflow.scope: workflow for workflow in unique.values()}
    evidence: dict[tuple[tuple[str, str, str], int], ReadinessTransition] = {}
    receipts: dict[tuple[tuple[str, str, str], str], ReadinessTransition] = {}
    for transition in transitions:
        _require(transition.scope in allowed_scopes)
        if transition.recorded_at > cutoff:
            continue
        version = (transition.scope, transition.revision)
        receipt = (transition.scope, transition.receipt_ref)
        _require(
            evidence.get(version, transition) == receipts.get(receipt, transition) == transition
        )
        evidence[version] = receipts[receipt] = transition
    by_workflow: dict[tuple[str, str, str], list[ReadinessTransition]] = defaultdict(list)
    for transition in evidence.values():
        by_workflow[transition.scope].append(transition)

    current: Counter[str] = Counter()
    ready_seconds: list[int] = []
    pending: list[int] = []
    policy_only = unknown_seed = 0
    for workflow in unique.values():
        seed = workflow.first_seed_at
        if seed is None:
            policy_only += workflow.policy_only
            unknown_seed += not workflow.policy_only
            continue
        if not window_start <= seed < window_end:
            continue
        history = by_workflow[workflow.scope]
        _require(all(item.effective_at >= seed or item.disposition == "held" for item in history))
        events = sorted(
            (item for item in history if item.effective_at >= seed),
            key=lambda item: (item.effective_at, item.revision),
        )
        first_ready = next((item for item in events if item.disposition == "fit_ready"), None)
        if first_ready:
            ready_seconds.append(first_ready.effective_at - seed)
        latest = events[-1].disposition if events else "pending"
        current[latest] += 1
        if latest in {"held", "pending", "disputed"}:
            pending.append(cutoff - seed)
    return CohortReadout(
        SCHEMA,
        window_start,
        window_end,
        cutoff,
        digest(
            [
                [asdict(unique[key]) for key in sorted(unique)],
                [asdict(evidence[key]) for key in sorted(evidence)],
            ]
        ),
        current.total(),
        len(ready_seconds),
        tuple(sorted(current.items())),
        policy_only,
        unknown_seed,
        median(ready_seconds) if ready_seconds else None,
        tuple(sorted(pending)),
    )


@dataclass(frozen=True)
class ExpertInterval:
    reviewer_ref: str
    receipt_ref: str
    task_ref: str
    started_at: int
    finished_at: int
    arm: str | None

    def __post_init__(self) -> None:
        for ref in (self.reviewer_ref, self.receipt_ref, self.task_ref):
            require_ref(ref)
        if self.arm is not None:
            require_ref(self.arm)
        _require(_time(self.started_at) and _time(self.finished_at))
        _require(self.started_at < self.finished_at)


def verified_person_seconds(
    intervals: Iterable[ExpertInterval], *, arm: str | None = None
) -> int | None:
    """Count verified time once per person. Shared intervals belong to each standalone arm."""
    if arm is not None:
        require_ref(arm)
    unique: dict[tuple[str, str], ExpertInterval] = {}
    for interval in intervals:
        key = (interval.reviewer_ref, interval.receipt_ref)
        _require(unique.get(key, interval) == interval)
        unique[key] = interval
    by_reviewer: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for interval in unique.values():
        if arm is None or interval.arm in {arm, None}:
            by_reviewer[interval.reviewer_ref].append((interval.started_at, interval.finished_at))
    if not by_reviewer:
        return None
    total = 0
    for observations in by_reviewer.values():
        finish = 0
        for start, end in sorted(observations):
            total += max(0, end - max(start, finish))
            finish = max(end, finish)
    return total


@dataclass(frozen=True)
class LabelCheckpoint:
    target_ref: str
    comparison_scope: str
    target_confirmed: bool
    verified_labels: int
    expert_person_seconds: int | None

    def __post_init__(self) -> None:
        require_ref(self.target_ref)
        require_ref(self.comparison_scope)
        _require(type(self.target_confirmed) is bool)
        _require(type(self.verified_labels) is int and self.verified_labels >= 0)
        if self.expert_person_seconds is not None:
            _require(type(self.expert_person_seconds) is int and self.expert_person_seconds >= 0)


@dataclass(frozen=True)
class QualifiedSavings:
    label_reduction: Fraction
    expert_person_seconds_saved: int | None


def compare_checkpoints(
    real_only: LabelCheckpoint, augmented: LabelCheckpoint
) -> QualifiedSavings | None:
    if (
        not real_only.target_confirmed
        or not augmented.target_confirmed
        or (real_only.target_ref, real_only.comparison_scope)
        != (augmented.target_ref, augmented.comparison_scope)
        or real_only.verified_labels == 0
    ):
        return None
    a, b = real_only.expert_person_seconds, augmented.expert_person_seconds
    return QualifiedSavings(
        Fraction(real_only.verified_labels - augmented.verified_labels, real_only.verified_labels),
        a - b if a is not None and b is not None else None,
    )
