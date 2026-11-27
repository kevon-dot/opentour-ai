"""Assemble typed evaluation metrics from qualified inputs and the registered kernel."""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction
from typing import Literal

from opentour_training.evaluation.inference_plan import (
    InferencePlan,
    MetricRole,
    numeric_alpha,
    qualify_numeric_profile,
    validate_denominators,
    validate_pair_cells,
)
from opentour_training.evaluation.statistics_kernel import (
    UndefinedReason,
    binomial_upper_bound,
    compute_interval,
)


@dataclass(frozen=True, slots=True)
class MetricDescriptor:
    """Caller-validated ArtifactRef bytes bound to the selected metric and method."""

    metric_ref: bytes
    unit: str
    method_ref: bytes


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    lower: float | None
    upper: float | None
    confidence_level: float
    sidedness: Literal["one", "two"]
    method_ref: bytes
    claim_alpha: Fraction


@dataclass(frozen=True, slots=True)
class MetricResult:
    metric_ref: bytes
    numerator: int | None
    denominator: int | None
    value: float | None
    unit: str
    interval: ConfidenceInterval | None
    independent_units: int
    cluster_count: int | None
    undefined_reason: UndefinedReason | None


def assemble_paired_metric(
    descriptor: MetricDescriptor,
    plan: InferencePlan,
    denominators: object,
    selected_look_id: str,
    selected_gate_id: str,
    *,
    metric_role: MetricRole,
    pair_cells: object = None,
    family_effects: object = None,
) -> MetricResult:
    """Use the frozen look, claim alpha, and explicit effect or retention role."""
    estimate = compute_interval(
        plan,
        denominators,
        selected_look_id,
        selected_gate_id,
        metric_role=metric_role,
        pair_cells=pair_cells,
        family_effects=family_effects,
    )
    counts = validate_denominators(denominators)
    units = counts["independent_units"]
    clusters = units if plan.independent_unit == "family" else None
    if estimate.undefined_reason is not None:
        return MetricResult(
            descriptor.metric_ref,
            None,
            None,
            None,
            descriptor.unit,
            None,
            units,
            clusters,
            estimate.undefined_reason,
        )

    selected = qualify_numeric_profile(
        plan, selected_look_id, selected_gate_id, metric_role=metric_role
    )
    numerator = None
    if pair_cells is not None:
        fixed, regressed, _, _ = validate_pair_cells(pair_cells)
        numerator = fixed - regressed
    return MetricResult(
        descriptor.metric_ref,
        numerator,
        units,
        estimate.value,
        descriptor.unit,
        ConfidenceInterval(
            estimate.lower,
            estimate.upper,
            float(1 - selected.alpha),
            "two" if selected.sides == 2 else "one",
            descriptor.method_ref,
            selected.alpha,
        ),
        units,
        clusters,
        None,
    )


def assemble_critical_risk_metric(
    descriptor: MetricDescriptor, *, errors: int, independent_exposures: int, alpha: float
) -> MetricResult:
    """Assemble the one-sided upper risk bound using an already allocated claim alpha."""
    upper = binomial_upper_bound(errors, independent_exposures, alpha)
    if upper is None:
        return MetricResult(
            descriptor.metric_ref,
            None,
            None,
            None,
            descriptor.unit,
            None,
            independent_exposures,
            None,
            "no_gradeable_units",
        )
    claim_alpha = numeric_alpha(alpha)
    return MetricResult(
        descriptor.metric_ref,
        errors,
        independent_exposures,
        errors / independent_exposures,
        descriptor.unit,
        ConfidenceInterval(
            None, upper, float(1 - claim_alpha), "one", descriptor.method_ref, claim_alpha
        ),
        independent_exposures,
        None,
        None,
    )


def assemble_planned_critical_risk_metric(
    descriptor: MetricDescriptor,
    plan: InferencePlan,
    selected_look_id: str,
    selected_gate_id: str,
    *,
    errors: int,
    independent_exposures: int,
) -> MetricResult:
    """Use the claim's prespecified alpha and the qualified risk population's denominator.

    Risk exposures are counted independently of paired effect rows by the
    caller. The look selection supplies only the frozen risk-claim alpha.
    """
    selected = qualify_numeric_profile(
        plan, selected_look_id, selected_gate_id, metric_role="effect"
    )
    return assemble_critical_risk_metric(
        descriptor,
        errors=errors,
        independent_exposures=independent_exposures,
        alpha=float(selected.alpha),
    )
