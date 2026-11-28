"""Shared usage and fence value types: estimated usage is never settleable (generated values)."""

from __future__ import annotations

from dataclasses import replace

import pytest
from opentour_cell.pilot import connector_ext as ext
from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.connector_ext import FenceReceipt, Usage, UsageSource


def make(source: object = UsageSource.MEASURED, **changes: object) -> Usage:
    fields: dict[str, object] = {
        "job_ref": "simulated:job",
        "currency": "USD",
        "incurred_minor_units": 3,
        "seconds": 2,
        "rows": 4,
        "steps": 5,
        "source": source,
    }
    fields.update(changes)
    return Usage(**fields)  # type: ignore[arg-type]


def test_estimated_usage_is_never_settleable_even_when_given_as_a_plain_string() -> None:
    assert not make(UsageSource.ESTIMATED).settleable
    assert not make("estimated").settleable
    assert make("estimated").source is UsageSource.ESTIMATED
    assert make(UsageSource.MEASURED).settleable
    assert make("provider_reported").settleable


@pytest.mark.parametrize(
    "changes",
    [
        {"source": "guessed"},
        {"source": None},
        {"job_ref": ""},
        {"currency": ""},
        {"incurred_minor_units": -1},
        {"seconds": -1},
        {"rows": True},
        {"steps": 1.5},
        {"irreversible": "yes"},
    ],
)
def test_malformed_usage_is_refused_instead_of_becoming_settleable(
    changes: dict[str, object],
) -> None:
    with pytest.raises(ValueError, match="INVALID_USAGE"):
        make(**changes)


def test_fence_receipt_has_exactly_the_documented_fields() -> None:
    receipt = FenceReceipt("generated-scope", ("simulated:job",), "simulated:fence")
    assert set(vars(receipt)) == {"scope", "canceled_job_refs", "evidence_ref"}


# ---- OPE-508 follow-up: the usage-vs-cap hold shared by the managed, open-weight and trainer
# routes
# X1 an intent bound's four maxima must each be exactly ``int``
# X2 usage at a journaled maximum is within the cap; one above it is over
# X3 the three fixed hold reasons, and their precedence
# X4 ``CappedUsage.settleable`` is True only within the cap
BOUND = mc.FitBound("USD", 500, 60, 20, 100)
MAXIMA = ("maximum_minor_units", "maximum_seconds", "maximum_rows", "maximum_steps")
CAP_DOCUMENT: dict[str, object] = {
    "currency": "USD",
    "maximum_minor_units": 500,
    "maximum_seconds": 60,
    "maximum_rows": 20,
    "maximum_steps": 100,
}
WITHIN: dict[str, object] = {
    "currency": "USD",
    "minor_units": 80,
    "seconds": 25,
    "rows": 4,
    "steps": 12,
}
DIMENSION = {
    "maximum_minor_units": "minor_units",
    "maximum_seconds": "seconds",
    "maximum_rows": "rows",
    "maximum_steps": "steps",
}


class SneakyInt(int):
    """An ``int`` subclass that serializes like the plain int."""


def figures(**changes: object) -> ext.UsageFigures | None:
    return ext.UsageFigures.from_document({**WITHIN, **changes})


def cap(**changes: object) -> ext.JournaledCap:
    return ext.JournaledCap.from_document({**CAP_DOCUMENT, **changes})


def test_x1_plain_int_bounds_are_journaled_with_the_currency() -> None:
    ext.require_exact_bound(BOUND)
    assert ext.journal_bound(BOUND) == CAP_DOCUMENT
    assert ext.JournaledCap.from_document(ext.journal_bound(BOUND)) == cap()


@pytest.mark.parametrize("limit", MAXIMA)
@pytest.mark.parametrize("value", [SneakyInt(5), True, False, 5.0, None, "5"])
def test_x1_a_maximum_that_is_not_exactly_int_is_refused(limit: str, value: object) -> None:
    bound = replace(BOUND, **{limit: value})
    with pytest.raises(ValueError, match="^fit_bound_not_exact_int$"):
        ext.require_exact_bound(bound)
    with pytest.raises(ValueError, match="^fit_bound_not_exact_int$"):
        ext.journal_bound(bound)


# Review of #241, F4 (owner-delegate decision: refuse before spend).
class SneakyStr(str):
    """A ``str`` subclass that serializes like the plain string."""


@pytest.mark.parametrize("limit", MAXIMA)
@pytest.mark.parametrize("value", [-1, -(10**9)])
def test_x1_a_negative_maximum_is_refused(limit: str, value: int) -> None:
    bound = replace(BOUND, **{limit: value})
    with pytest.raises(ValueError, match="^fit_bound_negative$"):
        ext.require_exact_bound(bound)
    with pytest.raises(ValueError, match="^fit_bound_negative$"):
        ext.journal_bound(bound)


@pytest.mark.parametrize("limit", MAXIMA)
def test_x1_a_zero_maximum_is_accepted(limit: str) -> None:
    ext.require_exact_bound(replace(BOUND, **{limit: 0}))


@pytest.mark.parametrize(
    "currency",
    ["", "usd", "Usd", "US", "USDX", " USD", "USD ", "U5D", "ÜSD", None, 840, SneakyStr("USD")],
)
def test_x1_a_currency_that_is_not_an_iso_4217_shaped_code_is_refused(currency: object) -> None:
    bound = replace(BOUND, currency=currency)
    with pytest.raises(ValueError, match="^fit_bound_currency_invalid$"):
        ext.require_exact_bound(bound)
    with pytest.raises(ValueError, match="^fit_bound_currency_invalid$"):
        ext.journal_bound(bound)


@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY", "XXX"])
def test_x1_an_uppercase_three_letter_currency_is_accepted(currency: str) -> None:
    ext.require_exact_bound(replace(BOUND, currency=currency))


@pytest.mark.parametrize("limit", MAXIMA)
def test_x2_usage_at_a_maximum_is_within_and_one_above_is_over(limit: str) -> None:
    used = DIMENSION[limit]
    maximum = CAP_DOCUMENT[limit]
    assert isinstance(maximum, int)
    at = figures(**{used: maximum})
    above = figures(**{used: maximum + 1})
    assert ext.cap_status(at, cap()) == ext.CAP_WITHIN and ext.usage_hold(at, cap()) is None
    assert ext.cap_status(above, cap()) == ext.CAP_OVER
    assert ext.usage_hold(above, cap()) == "over_cap"


def test_x3_the_three_reasons_are_fixed_and_distinct() -> None:
    assert (
        ext.HOLD_OVER_CAP,
        ext.HOLD_USAGE_UNVERIFIABLE,
        ext.HOLD_CAP_UNVERIFIABLE,
    ) == ("over_cap", "usage_unverifiable", "cap_unverifiable")


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {**WITHIN, "minor_units": -1},
        {**WITHIN, "seconds": True},
        {**WITHIN, "rows": 1.5},
        {**WITHIN, "steps": "1"},
        {**WITHIN, "currency": ""},
        {**WITHIN, "currency": 7},
        {k: v for k, v in WITHIN.items() if k != "steps"},
        {**WITHIN, "unexpected": 1},
        [WITHIN],
    ],
)
def test_x3_missing_or_malformed_usage_is_usage_unverifiable(usage: object) -> None:
    parsed = ext.UsageFigures.from_document(usage)
    assert not ext.usage_valid(parsed)
    assert ext.cap_status(parsed, cap()) is None
    # Reported before an unusable cap: there is no verified figure to compare.
    assert ext.usage_hold(parsed, cap()) == "usage_unverifiable"
    assert ext.usage_hold(parsed, ext.JournaledCap()) == "usage_unverifiable"


@pytest.mark.parametrize(
    "journaled",
    [
        {"currency": None},
        {"currency": ""},
        {"maximum_minor_units": None},
        {"maximum_seconds": True},
        {"maximum_rows": 20.0},
        {"maximum_steps": -1},
        {"maximum_steps": "100"},
    ],
)
def test_x3_an_unusable_cap_is_cap_unverifiable(journaled: dict[str, object]) -> None:
    assert ext.cap_status(figures(), cap(**journaled)) == ext.CAP_UNKNOWN
    assert ext.usage_hold(figures(), cap(**journaled)) == "cap_unverifiable"


@pytest.mark.parametrize("currency", ["EUR", "usd"])
def test_x3_another_currency_is_cap_unverifiable_but_a_verified_excess_wins(currency: str) -> None:
    assert ext.usage_hold(figures(currency=currency), cap()) == "cap_unverifiable"
    assert ext.usage_hold(figures(currency=currency, steps=101), cap()) == "over_cap"
    assert ext.usage_hold(figures(steps=101), cap(maximum_seconds=None)) == "over_cap"


@pytest.mark.parametrize("value", [None, "generated", [CAP_DOCUMENT], 5])
def test_x3_a_cap_document_that_is_not_a_mapping_is_empty(value: object) -> None:
    assert ext.JournaledCap.from_document(value) == ext.JournaledCap()
    assert ext.usage_hold(figures(), ext.JournaledCap.from_document(value)) == "cap_unverifiable"


@pytest.mark.parametrize(
    "status, settleable",
    [("within_cap", True), ("over_cap", False), ("cap_unknown", False)],
)
def test_x4_capped_usage_is_settleable_only_within_the_cap(status: str, settleable: bool) -> None:
    usage = ext.CappedUsage(
        "simulated:job", "USD", 3, 2, 4, 5, UsageSource.MEASURED, cap_status=status
    )
    assert usage.settleable is settleable
    estimated = ext.CappedUsage(
        "simulated:job", "USD", 3, 2, 4, 5, UsageSource.ESTIMATED, cap_status="within_cap"
    )
    assert estimated.settleable is False
