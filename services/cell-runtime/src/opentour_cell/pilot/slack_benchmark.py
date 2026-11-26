"""Allowlisted decision quality and work measurements for an already-authorized private review."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from typing import Any

from opentour_cell.pilot.pilot_metrics import scaled

Json = dict[str, Any]
SCHEMA = "opentour.pilot.workflow-benchmark.v1"
HEX = re.compile("[0-9a-f]{64}")
MEASURES = (
    ("review_seconds", "review_minutes_per_100"),
    ("latency_ms", "mean_latency_ms"),
    ("usage", "cost_per_1000"),
)


def _require(value: object) -> None:
    if not value:
        raise ValueError("invalid_workflow_benchmark")


def _count(value: Any) -> int:
    _require(type(value) is int and 0 <= value <= 2**31 - 1)
    return int(value)


def _amount(value: Any) -> str | None:
    if value is None:
        return None
    _require(isinstance(value, str) and re.fullmatch(r"[0-9]{1,32}(?:\.[0-9]{1,6})?", value))
    return str(value)


def _interval(data: Mapping[str, Any], verified: int) -> Json:
    _require(_count(data.get("verified")) == verified)
    if data.get("method") == "unavailable":
        _require(data.get("reason") == "zero_verified_or_correlated_cases")
        return {"method": "unavailable", "verified": verified}
    _require(data.get("method") == "wilson_95_independent_verified_cases" and verified > 0)
    lower, upper = data["lower"], data["upper"]
    _require(all(type(i) in (float, int) and math.isfinite(i) for i in (lower, upper)))
    _require(0 <= lower <= upper <= 1 and data.get("small_sample") is (verified < 30))
    return {
        "method": "wilson_95",
        "verified": verified,
        "lower": lower,
        "upper": upper,
        "small_sample": verified < 30,
    }


def _meter(data: Mapping[str, Any], population: int) -> Json:
    basis = data.get("basis")
    _require(basis in ("unavailable", "observed", "modeled", "generated"))
    _require(_count(data.get("eligible_cases")) == population)
    if basis == "unavailable":
        return {"basis": basis}
    currency, unit = data.get("currency"), data.get("unit")
    _require(isinstance(currency, str) and re.fullmatch("[A-Z]{3}", currency))
    _require(isinstance(unit, str) and re.fullmatch("[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", unit))
    start, end = data.get("period_start"), data.get("period_end")
    _require(type(start) is int and type(end) is int and 0 <= start < end < 2**63)
    counts = data["observed_case_counts"]
    readings = {}
    for field, metric in MEASURES:
        n = _count(counts[field])
        amount = _amount(data[metric])
        _require(n <= population and (amount is not None) == (n == population and n > 0))
        readings[metric] = {"count": n, "value": amount}
    return {
        "basis": basis,
        "currency": currency,
        "unit": unit,
        "period_start": start,
        "period_end": end,
        "readings": readings,
    }


def _arm(data: Mapping[str, Any], population: int) -> Json:
    automatic, review, missing, unknown, wrong = (
        _count(data[k])
        for k in (
            "automatic",
            "review",
            "missing",
            "unknown_reference_automatic",
            "incorrect_automatic",
        )
    )
    verified = automatic - unknown
    _require(automatic + review + missing == population and 0 <= wrong <= verified)
    _require(data["review_per_100"] == scaled(review, population, 100))
    return {
        "automatic": automatic,
        "review": review,
        "missing": missing,
        "verified": verified,
        "wrong": wrong,
        "uncertainty": _interval(data["uncertainty"], verified),
        "meter": _meter(data["economics"], population),
    }


def select_benchmark(result: Mapping[str, Any]) -> Json:
    """Select the same incumbent and candidate as the existing managed evidence projection."""
    _require(result.get("schema_version") == "opentour.pilot.protected-comparison.v1")
    winner = result["decision"]["winner"]
    _require(winner in (None, "incumbent", "simple", "model"))
    arms = result["arms"]
    if arms == {}:
        _require(
            result.get("outcome") == "hold"
            and result["decision"].get("disposition") == "hold"
            and winner is None
            and result.get("comparisons") == {}
            and result.get("paired_cases") == []
            and _count(result.get("protected_rows_evaluated")) == 0
        )
        return {}
    selected = "model" if winner == "model" or (winner is None and "model" in arms) else "simple"
    population = _count(result["protected_rows_evaluated"])
    cohort = result["protected_membership_digest"]
    revision = arms[selected]["revision"]
    _require(isinstance(cohort, str) and HEX.fullmatch(cohort))
    _require(isinstance(revision, str) and HEX.fullmatch(revision))
    first = _arm(arms["incumbent"]["metrics"], population)
    second = _arm(arms[selected]["metrics"], population)
    return {
        "schema_version": SCHEMA,
        "cohort_revision": cohort,
        "population": population,
        "candidate_revision": revision,
        "candidate_arm": selected,
        "incumbent": first,
        "candidate": second,
    }


def _validated_arm(value: Mapping[str, Any], population: int) -> Json:
    numbers = {k: _count(value[k]) for k in ("automatic", "review", "missing", "verified", "wrong")}
    _require(numbers["automatic"] + numbers["review"] + numbers["missing"] == population)
    _require(numbers["wrong"] <= numbers["verified"] <= numbers["automatic"])
    raw = value["uncertainty"]
    if raw["method"] == "unavailable":
        source = {
            "method": "unavailable",
            "verified": raw["verified"],
            "reason": "zero_verified_or_correlated_cases",
        }
    else:
        _require(raw["method"] == "wilson_95")
        source = {**raw, "method": "wilson_95_independent_verified_cases"}
    uncertainty = _interval(source, numbers["verified"])
    meter = value["meter"]
    if meter["basis"] == "unavailable":
        selected = {"basis": "unavailable"}
    else:
        readings = meter["readings"]
        selected = _meter(
            {
                **{
                    k: meter[k] for k in ("basis", "currency", "unit", "period_start", "period_end")
                },
                "eligible_cases": population,
                "observed_case_counts": {
                    field: readings[name]["count"] for field, name in MEASURES
                },
                **{name: readings[name]["value"] for _, name in MEASURES},
            },
            population,
        )
    return {**numbers, "uncertainty": uncertainty, "meter": selected}


def validated_benchmark(
    value: Mapping[str, Any],
    *,
    cohort_revision: str,
    population: int,
    candidate_arm: str,
    candidate_revision: str | None = None,
) -> Json:
    try:
        _require(isinstance(value, Mapping) and value["schema_version"] == SCHEMA)
        _require(
            isinstance(cohort_revision, str)
            and HEX.fullmatch(cohort_revision)
            and value["cohort_revision"] == cohort_revision
            and _count(value["population"]) == _count(population)
            and value["candidate_arm"] == candidate_arm
            and candidate_arm in ("simple", "model")
        )
        version = value["candidate_revision"]
        _require(
            isinstance(version, str)
            and HEX.fullmatch(version)
            and (candidate_revision is None or candidate_revision == version)
        )
        return {
            "schema_version": SCHEMA,
            "cohort_revision": cohort_revision,
            "population": population,
            "candidate_arm": candidate_arm,
            "candidate_revision": version,
            "incumbent": _validated_arm(value["incumbent"], population),
            "candidate": _validated_arm(value["candidate"], population),
        }
    except (TypeError, KeyError, AttributeError):
        raise ValueError("invalid_workflow_benchmark") from None


def _number(n: Decimal | str | None) -> str:
    if n is None:
        return "unavailable"
    # Keep small positive costs distinct from a measured zero.
    value = format(Decimal(n), "f")
    return value.rstrip("0").rstrip(".") if "." in value else value


def _workload(a: int, b: int, total: int) -> str:
    if total == 0:
        return "Review routing unavailable (0 eligible decisions)."
    current, candidate = (Decimal(scaled(c, total, 100)["value"]) for c in (a, b))
    delta = Decimal(scaled(abs(a - b), total, 100)["value"])
    change = (
        "unchanged" if delta == 0 else f"{_number(delta)} {'more' if b > a else 'fewer'} per 100"
    )
    return (
        f"Review routed {a}/{total} → {b}/{total} "
        f"({_number(current)} → {_number(candidate)} per 100, {change})."
    )


def _timestamp(seconds: int) -> str:
    try:
        return datetime.fromtimestamp(seconds, UTC).strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, OSError, OverflowError):
        return str(seconds)


def _timing(a: Json, b: Json, total: int) -> list[str]:
    if "unavailable" in (a["basis"], b["basis"]):
        return ["Review time, workflow latency and cost unavailable: no comparable measurements."]
    if any(a[k] != b[k] for k in ("basis", "currency", "unit", "period_start", "period_end")):
        return [
            "Review time, workflow latency and cost unavailable: "
            "measurement bases, periods or units differ."
        ]
    basis = {
        "observed": "Observed in the benchmark",
        "modeled": "Modeled estimate",
        "generated": "Generated example",
    }[a["basis"]]
    lines = [
        f"{basis} · {total} eligible decisions · meter unit {a['unit']}. "
        f"UTC window {_timestamp(a['period_start'])} to {_timestamp(a['period_end'])}."
    ]
    for key, label in (
        ("review_minutes_per_100", "Review minutes / 100"),
        ("mean_latency_ms", "Mean workflow latency (ms)"),
        ("cost_per_1000", f"Cost ({a['currency']}) / 1,000"),
    ):
        old, new = a["readings"][key], b["readings"][key]
        line = f"{label}  {_number(old['value'])} → {_number(new['value'])}."
        if old["value"] is None or new["value"] is None:
            line += f" Recorded {old['count']}/{total} → {new['count']}/{total}."
        else:
            with localcontext() as context:
                context.prec = 64
                delta = Decimal(new["value"]) - Decimal(old["value"])
            line += (
                " Unchanged."
                if delta == 0
                else f" {_number(abs(delta))} {'higher' if delta > 0 else 'lower'}."
            )
        lines.append(line)
    return lines


def _confidence(arm: Json) -> str:
    ci = arm["uncertainty"]
    if ci["method"] == "unavailable":
        return "unavailable (zero verified or correlated cases)"
    note = ", small sample" if ci["small_sample"] else ""
    return f"{100 * ci['lower']:.1f}–{100 * ci['upper']:.1f}% (n={ci['verified']}{note})"


def benchmark_summary(value: Mapping[str, Any]) -> str:
    checked = validated_benchmark(
        value,
        cohort_revision=value["cohort_revision"],
        population=value["population"],
        candidate_arm=value["candidate_arm"],
        candidate_revision=value["candidate_revision"],
    )
    a, b, n = checked["incumbent"], checked["candidate"], checked["population"]
    lines = [
        _workload(a["review"], b["review"], n),
        *_timing(a["meter"], b["meter"], n),
        f"95% verified-error interval · current {_confidence(a)}, candidate {_confidence(b)}.",
        "Realized post-adoption impact is unmeasured in this benchmark.",
    ]
    return "\n".join(lines)
