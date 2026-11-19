"""Generated contract cases for OPE-346, the standalone temporal resolver."""

from datetime import UTC, datetime, timedelta, timezone
from typing import cast

import pytest
from opentour_cell.reconstruction.temporal_resolution import (
    AvailabilityState,
    ClockInterval,
    EffectivenessState,
    TimingReason,
    resolve_temporal_evidence,
)

CUTOFF = datetime(2026, 11, 12, 9, tzinfo=UTC)
DECISION = ClockInterval(CUTOFF, CUTOFF)
UNKNOWN = ClockInterval(None, None)


def _point(offset: timedelta = timedelta()) -> ClockInterval:
    instant = CUTOFF + offset
    return ClockInterval(instant, instant)


def test_tr01_late_asof_is_effective_before_delivery() -> None:
    result = resolve_temporal_evidence(
        decision_interval=DECISION,
        effective_interval=_point(timedelta(minutes=-1)),
        availability_interval=_point(timedelta(minutes=3)),
        included_in_input=False,
    )

    assert result.effectiveness.state == EffectivenessState.EFFECTIVE
    assert result.availability.state == AvailabilityState.KNOWN_UNAVAILABLE
    assert result.availability.reason == TimingReason.AFTER_CUTOFF
    assert result.included_in_input is False
    assert not result.availability.source_clock_conflict


def test_tr01_late_actual_is_not_yet_effective_and_knowledge_is_unconfirmed() -> None:
    result = resolve_temporal_evidence(
        decision_interval=DECISION,
        effective_interval=_point(timedelta(minutes=2)),
        availability_interval=UNKNOWN,
        included_in_input=None,
    )

    assert result.effectiveness.state == EffectivenessState.NOT_YET_EFFECTIVE
    assert result.availability.state == AvailabilityState.UNKNOWN
    assert result.availability.reason == TimingReason.MISSING_EVENT_INTERVAL
    assert result.included_in_input is None
    assert not result.availability.source_clock_conflict


def test_tr02_unknown_availability_clock_preserves_fact_effectiveness() -> None:
    result = resolve_temporal_evidence(
        decision_interval=DECISION,
        effective_interval=_point(timedelta(minutes=-1)),
        availability_interval=UNKNOWN,
        included_in_input=None,
    )

    assert result.effectiveness.state == EffectivenessState.EFFECTIVE
    assert result.availability.state == AvailabilityState.UNKNOWN
    assert result.availability.reason == TimingReason.MISSING_EVENT_INTERVAL
    assert result.included_in_input is None


def test_tr02_unknown_effective_clock_preserves_availability() -> None:
    result = resolve_temporal_evidence(
        decision_interval=DECISION,
        effective_interval=UNKNOWN,
        availability_interval=DECISION,
        included_in_input=False,
    )

    assert result.effectiveness.state == EffectivenessState.UNKNOWN
    assert result.effectiveness.reason == TimingReason.MISSING_EVENT_INTERVAL
    assert result.availability.state == AvailabilityState.KNOWN_AVAILABLE


def test_tr02_unknown_decision_clock_preserves_unknown() -> None:
    result = resolve_temporal_evidence(
        decision_interval=UNKNOWN,
        effective_interval=DECISION,
        availability_interval=DECISION,
        included_in_input=False,
    )

    assert result.effectiveness.state == EffectivenessState.UNKNOWN
    assert result.effectiveness.reason == TimingReason.MISSING_DECISION_INTERVAL
    assert result.availability.state == AvailabilityState.UNKNOWN
    assert result.availability.reason == TimingReason.MISSING_DECISION_INTERVAL


@pytest.mark.parametrize(
    ("start_us", "end_us", "effective_state", "available_state", "reason"),
    [
        pytest.param(
            -3,
            -2,
            EffectivenessState.EFFECTIVE,
            AvailabilityState.KNOWN_AVAILABLE,
            TimingReason.AT_OR_BEFORE_CUTOFF,
            id="inclusive-earliest-decision",
        ),
        pytest.param(
            3,
            4,
            EffectivenessState.NOT_YET_EFFECTIVE,
            AvailabilityState.KNOWN_UNAVAILABLE,
            TimingReason.AFTER_CUTOFF,
            id="strictly-later-than-latest-decision",
        ),
        pytest.param(
            -3,
            0,
            EffectivenessState.UNKNOWN,
            AvailabilityState.UNKNOWN,
            TimingReason.INTERVALS_OVERLAP,
            id="cutoff_overlap",
        ),
        pytest.param(
            2,
            3,
            EffectivenessState.UNKNOWN,
            AvailabilityState.UNKNOWN,
            TimingReason.INTERVALS_OVERLAP,
            id="touching-latest-decision",
        ),
    ],
)
def test_tr02_interval_boundaries(
    start_us: int,
    end_us: int,
    effective_state: EffectivenessState,
    available_state: AvailabilityState,
    reason: TimingReason,
) -> None:
    decision = ClockInterval(CUTOFF - timedelta(microseconds=2), CUTOFF + timedelta(microseconds=2))
    event = ClockInterval(
        CUTOFF + timedelta(microseconds=start_us), CUTOFF + timedelta(microseconds=end_us)
    )
    result = resolve_temporal_evidence(
        decision_interval=decision,
        effective_interval=event,
        availability_interval=event,
        included_in_input=None,
    )

    assert result.effectiveness.state == effective_state
    assert result.effectiveness.reason == reason
    assert result.availability.state == available_state
    assert result.availability.reason == reason
    assert result.availability.decision_interval == decision
    assert result.availability.availability_interval == event
    assert not result.availability.source_clock_conflict


@pytest.mark.parametrize(
    ("decision", "availability", "clock_conflict"),
    [
        pytest.param(DECISION, UNKNOWN, False, id="absent-availability"),
        pytest.param(UNKNOWN, _point(timedelta(minutes=3)), False, id="absent-decision"),
        pytest.param(DECISION, _point(timedelta(minutes=3)), True, id="contradictory-availability"),
        pytest.param(
            DECISION,
            ClockInterval(CUTOFF - timedelta(seconds=1), CUTOFF + timedelta(seconds=1)),
            False,
            id="ambiguous-availability",
        ),
    ],
)
def test_tr03_verified_inclusion_preserves_factual_judgment(
    decision: ClockInterval, availability: ClockInterval, clock_conflict: bool
) -> None:
    effective = _point(timedelta(minutes=2))
    without_proof = resolve_temporal_evidence(
        decision_interval=decision,
        effective_interval=effective,
        availability_interval=availability,
        included_in_input=None,
    )
    verified = resolve_temporal_evidence(
        decision_interval=decision,
        effective_interval=effective,
        availability_interval=availability,
        included_in_input=True,
    )

    assert verified.effectiveness == without_proof.effectiveness
    assert verified.effectiveness.effective_interval == effective
    assert verified.availability.state == AvailabilityState.KNOWN_AVAILABLE
    assert verified.availability.reason == TimingReason.VERIFIED_INPUT_INCLUSION
    assert verified.availability.decision_interval == decision
    assert verified.availability.availability_interval == availability
    assert verified.availability.source_clock_conflict is clock_conflict
    assert verified.included_in_input is True


@pytest.mark.parametrize("included_in_input", [False, None], ids=["absent", "unknown"])
def test_tr03_available_at_cutoff_preserves_inclusion_flag(
    included_in_input: bool | None,
) -> None:
    result = resolve_temporal_evidence(
        decision_interval=DECISION,
        effective_interval=DECISION,
        availability_interval=DECISION,
        included_in_input=included_in_input,
    )

    assert result.effectiveness.state == EffectivenessState.EFFECTIVE
    assert result.availability.state == AvailabilityState.KNOWN_AVAILABLE
    assert result.availability.reason == TimingReason.AT_OR_BEFORE_CUTOFF
    assert result.included_in_input is included_in_input


@pytest.mark.parametrize(
    ("earliest", "latest", "error", "message"),
    [
        pytest.param(CUTOFF, None, ValueError, "both be present", id="missing-latest"),
        pytest.param(None, CUTOFF, ValueError, "both be present", id="missing-earliest"),
        pytest.param(
            CUTOFF.replace(tzinfo=None), CUTOFF, ValueError, "timezone-aware", id="naive-earliest"
        ),
        pytest.param(
            CUTOFF, CUTOFF.replace(tzinfo=None), ValueError, "timezone-aware", id="naive-latest"
        ),
        pytest.param(
            CUTOFF + timedelta(microseconds=1), CUTOFF, ValueError, "ordered", id="reversed"
        ),
        pytest.param("2026-11-12T09:00:00Z", CUTOFF, TypeError, "datetimes", id="wire-string"),
    ],
)
def test_tr04_malformed_clock_bounds(
    earliest: object, latest: object, error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message):
        ClockInterval(cast(datetime | None, earliest), cast(datetime | None, latest))


@pytest.mark.parametrize("included_in_input", [1, "true", []])
def test_tr04_malformed_inclusion_flag(included_in_input: object) -> None:
    with pytest.raises(TypeError, match="boolean or None"):
        resolve_temporal_evidence(
            decision_interval=DECISION,
            effective_interval=UNKNOWN,
            availability_interval=UNKNOWN,
            included_in_input=cast(bool, included_in_input),
        )


def test_tr04_malformed_clock_container() -> None:
    with pytest.raises(TypeError, match="ClockInterval"):
        resolve_temporal_evidence(
            decision_interval=DECISION,
            effective_interval=UNKNOWN,
            availability_interval=cast(ClockInterval, (None, None)),
            included_in_input=None,
        )


def test_tr04_equivalent_offsets_produce_same_repeatable_result() -> None:
    half_hour_zone = timezone(timedelta(hours=5, minutes=30))
    west_zone = timezone(timedelta(hours=-8))
    earliest = (CUTOFF - timedelta(minutes=1)).astimezone(half_hour_zone)
    latest = CUTOFF.astimezone(west_zone)
    interval = ClockInterval(earliest, latest)
    equivalent_utc = ClockInterval(CUTOFF - timedelta(minutes=1), CUTOFF)

    assert interval == equivalent_utc
    assert interval.earliest is not None and interval.earliest.tzinfo is UTC
    assert interval.latest is not None and interval.latest.tzinfo is UTC
    assert earliest.tzinfo is half_hour_zone
    assert latest.tzinfo is west_zone
    inputs = {
        "decision_interval": DECISION,
        "effective_interval": interval,
        "availability_interval": interval,
    }
    result = resolve_temporal_evidence(**inputs, included_in_input=False)
    assert resolve_temporal_evidence(**inputs, included_in_input=False) == result
    assert (
        resolve_temporal_evidence(
            decision_interval=DECISION,
            effective_interval=equivalent_utc,
            availability_interval=equivalent_utc,
            included_in_input=False,
        )
        == result
    )
    assert interval == equivalent_utc
