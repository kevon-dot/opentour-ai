"""Shared usage and fence value types: estimated usage is never settleable (generated values)."""

from __future__ import annotations

import pytest
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
