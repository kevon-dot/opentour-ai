"""Freeze a private operating agreement and qualify the two arms on exact v1 counts."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

AGREEMENT_VERSION = "opentour.pilot.operating-agreement.v1"
POINT_VERSION = "opentour.pilot.operating-point.v1"
_FIELDS = {
    "schema_version",
    "organization",
    "engagement",
    "source_digest",
    "current_method",
    "repair_method",
    "owner_attestation",
    "agreed_at",
    "expires_at",
    "state",
    "basis",
    "minimum_coverage_bps",
    "error_ceiling_bps",
}


class AgreementError(ValueError):
    """A safe diagnostic with no owner identifiers or case content."""


def canonical(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def source_digest(value: dict[str, Any]) -> str:
    """Fingerprint the entire selected input before attaching its own agreement."""
    return digest({key: item for key, item in value.items() if key != "operating_agreement"})


def enrollment_revision(source: str, agreement_revision: str) -> str:
    return digest(["opentour.pilot.agreed-enrollment.v1", source, agreement_revision])


def validate_agreement(
    value: Any, *, organization: str, source: str, ceiling: int, engagement: str | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict) or not _FIELDS <= value.keys() <= _FIELDS | {"supersedes"}:
        raise AgreementError("invalid_operating_agreement")
    if "supersedes" in value and (
        not isinstance(value["supersedes"], str)
        or not re.fullmatch(r"[0-9a-f]{64}", value["supersedes"])
    ):
        raise AgreementError("invalid_superseded_agreement")
    if value["schema_version"] != AGREEMENT_VERSION:
        raise AgreementError("unsupported_operating_agreement")
    for key in ("organization", "engagement", "current_method", "repair_method"):
        if not isinstance(value[key], str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", value[key]
        ):
            raise AgreementError("invalid_agreement_reference")
    if not isinstance(value["owner_attestation"], str) or not re.fullmatch(
        r"att-[0-9a-f]{16,64}", value["owner_attestation"]
    ):
        raise AgreementError("owner_attestation_required")
    if (
        value["organization"] != organization
        or (engagement is not None and value["engagement"] != engagement)
        or type(value["source_digest"]) is not str
        or not re.fullmatch(r"[0-9a-f]{64}", value["source_digest"])
        or value["source_digest"] != source
    ):
        raise AgreementError("operating_agreement_scope_mismatch")
    if value["state"] != "active" or value["basis"] != "predeclared":
        raise AgreementError("active_predeclared_agreement_required")
    for key in ("minimum_coverage_bps", "error_ceiling_bps"):
        if type(value[key]) is not int or not 0 <= value[key] <= 10_000:
            raise AgreementError("invalid_operating_point_bounds")
    if value["error_ceiling_bps"] != ceiling:
        raise AgreementError("operating_error_ceiling_mismatch")
    start, end = value["agreed_at"], value["expires_at"]
    if (
        type(start) is not int
        or type(end) is not int
        or not 0 <= start <= time.time() < end < 2**63
    ):
        raise AgreementError("operating_agreement_outside_validity")
    return value


def _arm(arm: dict[str, Any], floor: int) -> dict[str, str]:
    coverage = arm["coverage"]
    coverage_verdict = "undetermined"
    if coverage["denominator"]:
        coverage_verdict = (
            "met" if coverage["numerator"] * 10_000 >= floor * coverage["denominator"] else "missed"
        )
    error = {True: "met", False: "missed", None: "undetermined"}[arm["observed_ceiling_met"]]
    if "missed" in (coverage_verdict, error):
        combined = "missed"
    elif coverage_verdict == error == "met":
        combined = "met"
    else:
        combined = "undetermined"
    return {"coverage": coverage_verdict, "error": error, "combined": combined}


def operating_point(agreement: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """The disclosure-safe projection; authority, engagement and method references stay private."""
    floor = agreement["minimum_coverage_bps"]
    return {
        "schema_version": POINT_VERSION,
        "agreement_revision": digest(agreement),
        "minimum_coverage_bps": floor,
        "error_ceiling_bps": agreement["error_ceiling_bps"],
        "current": _arm(report["current"], floor),
        "repair": _arm(report["repair"], floor),
    }


def decision_quality(
    report: dict[str, Any],
    point: dict[str, Any] | None,
    *,
    source: str,
    aggregate: str,
    revision: str,
    cohort: str,
    artifact: str | None,
) -> dict[str, Any]:
    return {
        "schema_version": "opentour.pilot.decision-quality.v1",
        "basis": "predeclared" if point else "descriptive",
        "source_digest": source,
        "source_revision": revision,
        "cohort_digest": cohort,
        "artifact_digest": artifact,
        "aggregate_digest": aggregate,
        "operating_point": point,
        "observed_comparison": report["observed_comparison"],
        "paired": report["paired"],
        "changes": report["changes"],
        **{
            name: {
                key: report[name][key]
                for key in (
                    "coverage",
                    "observed_error_rate",
                    "review",
                    "missing",
                    "unknown_reference_automatic",
                )
            }
            for name in ("current", "repair")
        },
    }


def _display_rate(rate: dict[str, Any]) -> str:
    value = rate["value"]
    percentage = "undefined" if value is None else f"{100 * value:.2f}%"
    return f"{rate['numerator']}/{rate['denominator']} ({percentage})"


def quality_brief(result: dict[str, Any]) -> str:
    point = result["operating_point"]
    lines = [
        "# Frozen operating-point comparison",
        "",
        "| Evidence | SHA-256 |",
        "| --- | --- |",
        f"| Input | `{result['source_digest']}` |",
        f"| Revision | `{result['source_revision']}` |",
        f"| Cohort | `{result['cohort_digest']}` |",
        f"| Aggregate | `{result['aggregate_digest']}` |",
        "",
    ]
    if result["artifact_digest"] is not None:
        lines.insert(-1, f"| Frozen weights | `{result['artifact_digest']}` |")
    if point is None:
        lines.append("Descriptive only. The owner has not bound a prospective operating point.")
    else:
        lines.extend(
            [
                f"Agreement revision `{point['agreement_revision']}`.",
                "",
                f"The minimum automated coverage is {point['minimum_coverage_bps']} basis points "
                f"({point['minimum_coverage_bps'] / 100:.2f}%).",
                f"The maximum wrong-automatic rate is {point['error_ceiling_bps']} basis points "
                f"({point['error_ceiling_bps'] / 100:.2f}%).",
            ]
        )
    lines.extend(
        [
            "",
            "| Method | Automatic / eligible | Coverage "
            "| Errors / verified automatic | Error limit | Combined |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
    )
    for key, label in (("current", "Incumbent"), ("repair", "Repair")):
        arm = result[key]
        coverage = _display_rate(arm["coverage"])
        errors = _display_rate(arm["observed_error_rate"])
        if point:
            verdict = point[key]
            coverage_verdict = verdict["coverage"].title()
            error_verdict = verdict["error"].title()
            combined = verdict["combined"].title()
        else:
            coverage_verdict = error_verdict = combined = "Unrecorded"
        lines.append(
            f"| {label} | {coverage} | {coverage_verdict} | "
            f"{errors} | {error_verdict} | {combined} |"
        )
    lines.extend(
        [
            "",
            "| Method | Routed to review | Missing output | Unknown automatic reference |",
            "| --- | --- | --- | --- |",
        ]
    )
    for key, label in (("current", "Incumbent"), ("repair", "Repair")):
        arm = result[key]
        lines.append(
            f"| {label} | {arm['review']} | {arm['missing']} "
            f"| {arm['unknown_reference_automatic']} |"
        )
    paired = result["paired"]
    lines.extend(
        [
            "",
            f"The observed comparison is {result['observed_comparison'].replace('_', ' ')}.",
            f"There were {paired['comparable']} comparable pairs, including "
            f"{paired['fixes']} fixes and {paired['regressions']} regressions.",
        ]
    )
    if not result["changes"]["same_automatic_cases"]:
        lines.append(
            "The methods automated different cases. Their whole-arm error rates use "
            "different populations."
        )
    lines.extend(
        ["", "The workflow owner records the separate rollout or stop decision privately.", ""]
    )
    return "\n".join(lines)
