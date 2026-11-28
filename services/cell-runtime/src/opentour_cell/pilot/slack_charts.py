"""Create a named reviewer's chart message from the selected frozen aggregate."""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from opentour_cell.pilot.slack_views import managed_report

Json = dict[str, Any]
NAMES = {"current": "Current workflow", "rule": "Simple repair", "model": "Tuned model"}
PAIRED = {
    "Fixed": "fixes",
    "Newly wrong": "regressions",
    "Stayed correct": "ties_correct",
    "Stayed wrong": "ties_incorrect",
}


def _bars(title: str, labels: list[str], series: dict[str, list[int]]) -> Json:
    return {
        "type": "data_visualization",
        "title": title,
        "chart": {
            "type": "bar",
            "series": [
                {
                    "name": name,
                    "data": [
                        {"label": label, "value": value}
                        for label, value in zip(labels, values, strict=True)
                    ],
                }
                for name, values in series.items()
            ],
            "axis_config": {"categories": labels, "y_label": "Cases"},
        },
    }


def decision_charts(report: Mapping[str, Any], audit: Mapping[str, Any], revision: str) -> Json:
    """Render allowlisted numeric aggregates. Delivery and authorization belong to the caller."""
    pairs, cohorts = audit.get("comparisons"), audit.get("comparison_cohorts")
    if (
        not re.fullmatch(r"rev_[a-f0-9]{26}", revision)
        or not re.fullmatch(r"[a-f0-9]{64}", str(audit.get("cohort_revision")))
        or not re.fullmatch(r"[a-f0-9]{64}", str(audit.get("run_revision")))
        or not isinstance(pairs, dict)
        or set(pairs) not in ({"rule"}, {"rule", "model"})
        or not isinstance(cohorts, dict)
        or set(cohorts) != set(pairs)
        or any(v != audit["cohort_revision"] for v in cohorts.values())
    ):
        raise ValueError("bound_chart_evidence_required")
    selected = managed_report(report)
    options = {arm: managed_report(pairs[arm]) for arm in ("rule", "model") if arm in pairs}
    for option in options.values():
        if any(
            option[k] != selected[k]
            for k in (
                "cohort_size",
                "current",
                "known_references",
                "unknown_references",
                "error_ceiling_bps",
            )
        ):
            raise ValueError("inconsistent_chart_cohort")
    route = audit.get("handoff_disposition")
    winner = {"simple_repair": "rule", "model_candidate": "model"}.get(str(route))
    if route not in {"simple_repair", "model_candidate", "hold", "no_change"} or (
        winner and (winner not in options or options[winner] != selected)
    ):
        raise ValueError("selected_chart_result_mismatch")

    arms = {"current": selected["current"], **{k: v["repair"] for k, v in options.items()}}
    size = selected["cohort_size"]
    version = audit["run_revision"][:12]
    sentences = [f"Protected comparison · version {version}"]
    charts = []
    error_counts = []
    for key, arm in arms.items():
        verified = arm["observed_error_rate"]["denominator"]
        wrong = arm["incorrect_automatic"]
        error = (
            f"{wrong}/{verified} verified automated decisions wrong"
            if verified
            else "verified error rate unavailable"
        )
        coverage = (
            f"{arm['automatic']}/{size} eligible cases automated"
            if size
            else "coverage unavailable"
        )
        sentences.append(
            f"{NAMES[key]} · {coverage}. {error}. "
            f"Automated cases with an unknown answer: {arm['unknown_reference_automatic']}."
        )
        error_counts.append(wrong)
    if size:
        series = {"Automated": [a["automatic"] for a in arms.values()]}
        if all(a["observed_error_rate"]["denominator"] for a in arms.values()):
            series["Verified errors"] = error_counts
        charts.append(_bars("How many decisions and errors?", [NAMES[k] for k in arms], series))
    else:
        sentences.append("No eligible protected cases.")
    different = [
        NAMES[k]
        for k, option in options.items()
        if option["changes"]["current_only_automatic"] or option["changes"]["repair_only_automatic"]
    ]
    if different:
        sentences.append(
            "Different cases were automated by the current workflow and "
            + ", ".join(different)
            + "."
        )
    for key, option in options.items():
        pair = option["paired"]
        changes = option["changes"]
        prefix = f"Current vs. {NAMES[key].lower()}"
        sentences.append(
            f"{prefix}: {pair['comparable']} comparable cases, {pair['excluded']} excluded. "
            f"Only current automated: {changes['current_only_automatic']}. "
            f"Only {NAMES[key].lower()} automated: {changes['repair_only_automatic']}."
        )
        if pair["comparable"]:
            counts = [pair[v] for v in PAIRED.values()]
            sentences.append(
                ", ".join(f"{c} {label.lower()}" for label, c in zip(PAIRED, counts, strict=True))
                + "."
            )
            if key == winner:
                charts.append(_bars(prefix + ": paired outcomes", list(PAIRED), {"Cases": counts}))
        else:
            sentences.append("No comparable paired cases.")
    if not winner:
        sentences.append("No repair was selected for handoff.")
    text = "\n".join(sentences)
    return {
        "text": text,
        "blocks": [{"type": "section", "text": {"type": "plain_text", "text": text}}, *charts],
    }
