"""Render the aggregate produced by the local pilot evaluator for a workflow owner."""

from __future__ import annotations

from typing import Any

_FINDINGS = {
    "insufficient_evidence": (
        "Insufficient evidence. A missing prediction, an unverified automatic result, "
        "or an empty set of comparable cases prevents an overall comparison."
    ),
    "different_automatic_cases": (
        "The methods automated different cases. Compare paired outcomes only where both "
        "methods automated the same case and its reference was known."
    ),
    "improved": "The proposed repair made fewer observed errors on the same automated cases.",
    "regressed": "The proposed repair made more observed errors on the same automated cases.",
    "equivalent": "Both methods made equal observed error counts on the same automated cases.",
}


def _fraction(numerator: int, denominator: int) -> str:
    if not denominator:
        return f"{numerator}/0 (undefined)"
    return f"{numerator}/{denominator} ({100 * numerator / denominator:.2f}%)"


def _arm_cells(arm: dict[str, Any], size: int) -> tuple[str, ...]:
    coverage = arm["coverage"]
    errors = arm["observed_error_rate"]
    ceiling = {True: "Met", False: "Exceeded", None: "Undetermined"}[arm["observed_ceiling_met"]]
    return (
        _fraction(coverage["numerator"], coverage["denominator"]),
        _fraction(errors["numerator"], errors["denominator"]),
        _fraction(arm["unknown_reference_automatic"], arm["automatic"]),
        _fraction(arm["review"], size),
        _fraction(arm["missing"], size),
        ceiling,
    )


def decision_brief(report: dict[str, Any]) -> str:
    """Format an evaluated aggregate only. Raw case data is never interpolated."""
    size, paired, changes = report["cohort_size"], report["paired"], report["changes"]
    labels = (
        "Automatically handled / supplied cases",
        "Incorrect automatic outputs / automatic cases with a known reference",
        "Automatic cases with unknown reference / all automatic cases",
        "Routed for human review / supplied cases",
        "Missing method output / supplied cases",
        "Observed error ceiling",
    )
    rows = [
        f"| {label} | {current} | {repair} |"
        for label, current, repair in zip(
            labels,
            _arm_cells(report["current"], size),
            _arm_cells(report["repair"], size),
            strict=True,
        )
    ]
    ceiling = report["error_ceiling_bps"]
    lines = [
        "# Offline pilot decision brief",
        "",
        _FINDINGS[report["observed_comparison"]],
        "",
        "| Frozen cohort | Cases |",
        "| --- | --- |",
        f"| Supplied cases | {size} |",
        f"| Known independent reference | {report['known_references']} |",
        f"| Unknown reference | {report['unknown_references']} |",
        "",
        "## Operating point",
        "",
        f"The pre-agreed ceiling for incorrect automatic outputs is {ceiling} basis points "
        f"({ceiling / 100:.2f}%). Percentages are rounded. The fractions give the exact counts.",
        "",
        "| Measure | Incumbent | Proposed repair |",
        "| --- | --- | --- |",
        *rows,
        "",
        f"Human review changed by {changes['review_delta']:+d} cases and missing outputs by "
        f"{changes['missing_delta']:+d} cases (repair minus incumbent). Review counts measure "
        "routing. Time and cost were not observed.",
        "",
        "## Paired outcomes",
        "",
        f"Both methods made automatic decisions with a known reference on "
        f"{paired['comparable']} of {size} cases. Supplied cases outside this paired "
        f"comparison total {paired['excluded']}.",
        "",
        "| Paired outcome | Cases |",
        "| --- | --- |",
        f"| Incumbent wrong, repair correct (fix) | {paired['fixes']} |",
        f"| Incumbent correct, repair wrong (regression) | {paired['regressions']} |",
        f"| Both correct | {paired['ties_correct']} |",
        f"| Both incorrect | {paired['ties_incorrect']} |",
        "",
        f"Cases automated only by the incumbent total {changes['current_only_automatic']}. "
        f"Cases automated only by the repair total {changes['repair_only_automatic']}.",
    ]
    if not changes["same_automatic_cases"]:
        lines.append(
            "The automated sets differ. Whole-arm error rates describe different cases, "
            "including when their coverage percentages match."
        )
    lines.extend(
        [
            "",
            "The manifest begins after intake. Reconcile upstream exclusions, unmatched source "
            "records and group independence against the operator’s private intake record. "
            "The readout measures the supplied cohort only.",
            "",
            "## Workflow owner decision",
            "",
            "After the recipient reruns or witnesses the comparison, record the decision in "
            "the approved location. Options include using the repair, keeping the incumbent, "
            "collecting specified evidence, or stopping.",
            "",
            "| Owner completes | Entry |",
            "| --- | --- |",
            "| Authorized owner and readout revision | |",
            "| Rerun or witness reference | |",
            "| Decision and next action | |",
            "",
        ]
    )
    return "\n".join(lines)
