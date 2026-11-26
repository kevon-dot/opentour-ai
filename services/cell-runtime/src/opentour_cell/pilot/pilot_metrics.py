"""Measure protected decision quality and owner-authorized operational observations."""

from __future__ import annotations

import math
import re
import time
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction
from typing import Any

from opentour_cell.pilot.operating_point import _arm as frozen_arm_verdict
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.simple_repair import RepairError, identifier

Json = dict[str, Any]
SCALE = Decimal("0.000001")


def amount(value: Any) -> Fraction:
    if type(value) is int:
        value = str(value)
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,9})?", value):
        raise RepairError("invalid_decimal_measurement")
    try:
        return Fraction(Decimal(value))
    except (ValueError, InvalidOperation):
        raise RepairError("invalid_decimal_measurement") from None


def rendered(value: Fraction | None) -> str | None:
    if value is None:
        return None
    with localcontext() as context:
        context.prec = 50
        part = (Decimal(value.numerator) / Decimal(value.denominator)).quantize(SCALE)
    return format(part, "f")


def scaled(n: int, denominator: int, multiplier: int) -> Json:
    return {
        "numerator": n,
        "denominator": denominator,
        "per": multiplier,
        "value": rendered(Fraction(n * multiplier, denominator)) if denominator else None,
    }


def _interval(wrong: int, known: int, groups: int) -> Json:
    # Fixed 95% Wilson score interval for independent, verified automatic observations.
    # Multiple observations per family would break the binomial independence assumption.
    if known == 0 or known != groups:
        return {
            "method": "unavailable",
            "reason": "zero_verified_or_correlated_cases",
            "verified": known,
        }
    z = 1.959963984540054
    p = wrong / known
    scale = 1 + z * z / known
    centre = (p + z * z / (2 * known)) / scale
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * known)) / known) / scale
    return {
        "method": "wilson_95_independent_verified_cases",
        "verified": known,
        "lower": round(max(0.0, centre - margin), 8),
        "upper": round(min(1.0, centre + margin), 8),
        "small_sample": known < 30,
    }


def operating_verdict(summary: Json, size: int, floor: int) -> Json:
    if summary["coverage"]["denominator"] != size:
        raise RepairError("coverage_population_mismatch")
    return frozen_arm_verdict(summary, floor)


def arm_metrics(summary: Json, rows: list[Json], arm: str, terms: Json) -> Json:
    groups = set()
    material_wrong = material_unknown = 0
    critical = set(terms.get("material_slices", []))
    for row in rows:
        outcome = row[arm]
        if outcome["status"] != "automatic":
            continue
        marked = not critical or not critical.isdisjoint(row["slices"])
        if row["reference"] is None:
            material_unknown += int(marked)
        else:
            groups.add(row["group_id"])
            material_wrong += int(marked and row["reference"] != outcome["label"])
    total = len(rows)
    known = summary["observed_error_rate"]["denominator"]
    return {
        **summary,
        "automatic_per_100": scaled(summary["automatic"], total, 100),
        "review_per_100": scaled(summary["review"], total, 100),
        "material_errors_per_1000": scaled(material_wrong, total, 1000),
        "unknown_material_automatic": material_unknown,
        "uncertainty": _interval(summary["incorrect_automatic"], known, len(groups)),
        "operating_point": operating_verdict(summary, total, terms["minimum_coverage_bps"]),
    }


def _unavailable() -> Json:
    return {
        "basis": "unavailable",
        "reason": "measurements_not_supplied",
        "eligible_cases": 0,
        "review_minutes_per_100": None,
        "review_minutes_per_1000": None,
        "mean_latency_ms": None,
        "total_latency_ms_per_1000": None,
        "cost_per_1000": None,
        "latency_cap": "unavailable",
        "cost_cap": "unavailable",
        "complete_fields": [],
    }


def economics(rows: list[Json], measurements: Json | None, terms: Json) -> Json:
    total = len(rows)
    if measurements is None:
        return {**_unavailable(), "eligible_cases": total}
    required = {
        "basis",
        "permission_ref",
        "provenance_ref",
        "hardware",
        "period_start",
        "period_end",
        "currency",
        "unit",
        "rate_per_unit",
        "cases",
    }
    if not isinstance(measurements, dict) or set(measurements) != required:
        raise RepairError("invalid_measurement_contract")
    basis = measurements["basis"]
    if basis not in ("observed", "modeled", "generated"):
        raise RepairError("invalid_measurement_basis")
    for key in ("permission_ref", "provenance_ref", "hardware", "unit"):
        identifier(measurements[key])
    currency = measurements["currency"]
    if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
        raise RepairError("invalid_measurement_currency")
    start, end = measurements["period_start"], measurements["period_end"]
    if type(start) is not int or type(end) is not int or not 0 <= start < end < 2**63:
        raise RepairError("invalid_measurement_period")
    if basis == "observed" and end > time.time():
        raise RepairError("observed_measurement_window_in_future")
    rate = amount(measurements["rate_per_unit"])
    cases = measurements["cases"]
    ids = {r["case_id"] for r in rows}
    if not isinstance(cases, dict) or not cases.keys() <= ids:
        raise RepairError("measurement_case_outside_cohort")
    names = ("review_seconds", "latency_ms", "usage")
    values: dict[str, list[Fraction]] = {name: [] for name in names}
    for item in cases.values():
        if not isinstance(item, dict) or not item.keys() <= set(names):
            raise RepairError("invalid_measurement_fields")
        for name in names:
            if item.get(name) is not None:
                values[name].append(amount(item[name]))
    # Explicit zero is required on an automated/abstained case with no observed usage.
    # Missing measurement on any eligible case prevents normalization on that metric.
    complete = [k for k, vs in values.items() if total and len(vs) == total]
    mean = {k: sum(values[k], Fraction(0)) / total for k in complete}
    latency = mean.get("latency_ms")
    cost = mean["usage"] * rate * 1000 if "usage" in mean else None
    review = mean.get("review_seconds")
    latency_cap = cost_cap = "unavailable"
    if latency is not None and "max_latency_ms" in terms and basis == "observed":
        latency_cap = "met" if latency <= amount(terms["max_latency_ms"]) else "missed"
    cap = terms.get("max_cost_per_1000")
    if cap and cost is not None and cap["currency"] == currency and basis == "observed":
        cost_cap = "met" if cost <= amount(cap["amount"]) else "missed"
    return {
        "basis": basis,
        "currency": currency,
        "unit": measurements["unit"],
        "hardware": measurements["hardware"],
        "permission_ref": measurements["permission_ref"],
        "provenance_ref": measurements["provenance_ref"],
        "period_start": start,
        "period_end": end,
        "rate_per_unit": str(measurements["rate_per_unit"]),
        "measurement_revision": digest(measurements),
        "eligible_cases": total,
        "complete_fields": complete,
        "observed_case_counts": {k: len(v) for k, v in values.items()},
        "review_minutes_per_100": rendered(review * 100 / 60 if review is not None else None),
        "review_minutes_per_1000": rendered(review * 1000 / 60 if review is not None else None),
        "mean_latency_ms": rendered(latency),
        "total_latency_ms_per_1000": rendered(latency * 1000 if latency is not None else None),
        "cost_per_1000": rendered(cost),
        "cost_per_1000_exact": [cost.numerator, cost.denominator] if cost is not None else None,
        "latency_cap": latency_cap,
        "cost_cap": cost_cap,
    }


def economic_comparison(incumbent: Json, candidate: Json) -> Json:
    context = ("currency", "unit", "period_start", "period_end")
    if "unavailable" in (incumbent["basis"], candidate["basis"]):
        return {"basis": "unavailable", "reason": "measurements_not_supplied", "savings": None}
    if any(incumbent[k] != candidate[k] for k in context):
        return {
            "basis": "unavailable",
            "reason": "currency_unit_or_period_mismatch",
            "savings": None,
        }
    if incumbent["basis"] != candidate["basis"]:
        return {"basis": "unavailable", "reason": "different_measurement_bases", "savings": None}
    basis = incumbent["basis"]
    diffs = {}
    for name in (
        "cost_per_1000",
        "review_minutes_per_100",
        "review_minutes_per_1000",
        "mean_latency_ms",
    ):
        first, second = incumbent[name], candidate[name]
        diffs[name] = (
            None
            if first is None or second is None
            else rendered(Fraction(first) - Fraction(second))
        )
    return {
        "basis": basis,
        "currency": candidate["currency"],
        "unit": candidate["unit"],
        "savings": diffs if basis == "observed" else None,
        "observed_cost_increased": (
            Fraction(*candidate["cost_per_1000_exact"])
            > Fraction(*incumbent["cost_per_1000_exact"])
            if basis == "observed"
            and incumbent["cost_per_1000_exact"] is not None
            and candidate["cost_per_1000_exact"] is not None
            else None
        ),
        "modeled_differences": diffs if basis == "modeled" else None,
        "generated_differences": diffs if basis == "generated" else None,
    }
