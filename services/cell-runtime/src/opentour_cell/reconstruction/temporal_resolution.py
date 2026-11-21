"""Determine factual effectiveness and knowability from qualified clock bounds."""

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


@dataclass(frozen=True, slots=True)
class ClockInterval:
    """Closed clock uncertainty bounds, or two absent endpoints."""

    earliest: datetime | None
    latest: datetime | None

    def __post_init__(self) -> None:
        if self.earliest is None and self.latest is None:
            return
        if self.earliest is None or self.latest is None:
            raise ValueError("Clock bounds must both be present or both be absent.")

        earliest = _as_utc(self.earliest)
        latest = _as_utc(self.latest)
        if earliest > latest:
            raise ValueError("Clock bounds must be ordered.")
        object.__setattr__(self, "earliest", earliest)
        object.__setattr__(self, "latest", latest)


def _as_utc(instant: datetime) -> datetime:
    if not isinstance(instant, datetime):
        raise TypeError("Clock bounds must be datetimes.")
    if instant.utcoffset() is None:
        raise ValueError("Clock bounds must be timezone-aware.")
    try:
        return instant.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("Clock bounds must be representable in UTC.") from exc


class EffectivenessState(StrEnum):
    EFFECTIVE = "effective"
    NOT_YET_EFFECTIVE = "not_yet_effective"
    UNKNOWN = "unknown"


class AvailabilityState(StrEnum):
    KNOWN_AVAILABLE = "known_available"
    KNOWN_UNAVAILABLE = "known_unavailable"
    UNKNOWN = "unknown"


class TimingReason(StrEnum):
    AT_OR_BEFORE_CUTOFF = "at_or_before_cutoff"
    AFTER_CUTOFF = "after_cutoff"
    MISSING_DECISION_INTERVAL = "missing_decision_interval"
    MISSING_EVENT_INTERVAL = "missing_event_interval"
    INTERVALS_OVERLAP = "intervals_overlap"
    VERIFIED_INPUT_INCLUSION = "verified_input_inclusion"


@dataclass(frozen=True, slots=True)
class EffectivenessJudgment:
    state: EffectivenessState
    reason: TimingReason
    effective_interval: ClockInterval


@dataclass(frozen=True, slots=True)
class AvailabilityJudgment:
    state: AvailabilityState
    reason: TimingReason
    decision_interval: ClockInterval
    availability_interval: ClockInterval
    source_clock_conflict: bool


@dataclass(frozen=True, slots=True)
class TemporalResolution:
    effectiveness: EffectivenessJudgment
    availability: AvailabilityJudgment
    included_in_input: bool | None


_EFFECTIVENESS_STATES = {
    TimingReason.AT_OR_BEFORE_CUTOFF: EffectivenessState.EFFECTIVE,
    TimingReason.AFTER_CUTOFF: EffectivenessState.NOT_YET_EFFECTIVE,
}
_AVAILABILITY_STATES = {
    TimingReason.AT_OR_BEFORE_CUTOFF: AvailabilityState.KNOWN_AVAILABLE,
    TimingReason.AFTER_CUTOFF: AvailabilityState.KNOWN_UNAVAILABLE,
    TimingReason.VERIFIED_INPUT_INCLUSION: AvailabilityState.KNOWN_AVAILABLE,
}


def _compare_to_cutoff(event: ClockInterval, decision: ClockInterval) -> TimingReason:
    if decision.earliest is None or decision.latest is None:
        return TimingReason.MISSING_DECISION_INTERVAL
    if event.earliest is None or event.latest is None:
        return TimingReason.MISSING_EVENT_INTERVAL
    if event.latest <= decision.earliest:
        return TimingReason.AT_OR_BEFORE_CUTOFF
    if event.earliest > decision.latest:
        return TimingReason.AFTER_CUTOFF
    return TimingReason.INTERVALS_OVERLAP


def resolve_temporal_evidence(
    *,
    decision_interval: ClockInterval,
    effective_interval: ClockInterval,
    availability_interval: ClockInterval,
    included_in_input: bool | None,
) -> TemporalResolution:
    """Assess one fact's effective instant and corroborated actor availability.

    The caller qualifies the clock bounds from source and delivery evidence.
    A true inclusion requires caller verification of the exact as-served bytes.
    False denotes verified absence from that input; None denotes unknown inclusion.
    """
    if not all(
        isinstance(interval, ClockInterval)
        for interval in (decision_interval, effective_interval, availability_interval)
    ):
        raise TypeError("Expected ClockInterval values.")
    if included_in_input is not None and type(included_in_input) is not bool:
        raise TypeError("Input inclusion must be a boolean or None.")

    effective_reason = _compare_to_cutoff(effective_interval, decision_interval)
    availability_reason = _compare_to_cutoff(availability_interval, decision_interval)
    clock_conflict = included_in_input is True and availability_reason == TimingReason.AFTER_CUTOFF
    if included_in_input is True:
        availability_reason = TimingReason.VERIFIED_INPUT_INCLUSION

    return TemporalResolution(
        effectiveness=EffectivenessJudgment(
            state=_EFFECTIVENESS_STATES.get(effective_reason, EffectivenessState.UNKNOWN),
            reason=effective_reason,
            effective_interval=effective_interval,
        ),
        availability=AvailabilityJudgment(
            state=_AVAILABILITY_STATES.get(availability_reason, AvailabilityState.UNKNOWN),
            reason=availability_reason,
            decision_interval=decision_interval,
            availability_interval=availability_interval,
            source_clock_conflict=clock_conflict,
        ),
        included_in_input=included_in_input,
    )
