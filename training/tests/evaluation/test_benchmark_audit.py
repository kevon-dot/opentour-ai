"""OPE-526 audit receipt: statistics, fixture mutations A-D and acceptance packs.

Fixture kind: generated. None of this is provider or customer evidence, and the upstream
OPE-416 snapshot receipt is unlanded, so the end-to-end gate is not exercised here.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from typing import Any

import opentour_training.evaluation.benchmark_audit.audit as audit_module
import pytest
from audit_fixtures import (
    CONSUMED,
    INDEPENDENT,
    OBSERVED_SNAPSHOT,
    baseline_records,
    commit,
    family,
    inputs_for,
    real_sample_records,
)
from opentour_training.evaluation.benchmark_audit.audit import (
    audit,
    draw_audit_sample,
    mapper_view,
)
from opentour_training.evaluation.benchmark_audit.exposure import (
    Channel,
    ExposureGuard,
    QueryRequest,
    Surface,
    replay_exposure,
)
from opentour_training.evaluation.benchmark_audit.inference import (
    design_weighted_error,
    families_needed_for_ceiling,
    upper_error,
    zero_error_upper_bound,
)
from opentour_training.evaluation.benchmark_audit.model import (
    ExposureRecord,
    PopulationKind,
    PreprocessingRecord,
    SamplingDesign,
    SelectionUse,
    Verdict,
)

V = Verdict
SELECTOR = frozenset({"selector"})


def codes(receipt: dict[str, Any]) -> set[str]:
    return {r["code"] for r in receipt["reasons"]}


def run(records: Any = None, **kw: Any) -> dict[str, Any]:
    return audit(inputs_for(records if records is not None else baseline_records(), **kw))


# ---- Statistics: computed from 1 - 0.05**(1/n), asserted against the specified numbers ----


def test_zero_error_exact_bounds_match_specified_values() -> None:
    assert round(zero_error_upper_bound(12) * 100, 2) == 22.09
    assert round(zero_error_upper_bound(4) * 100, 2) == 52.71
    assert zero_error_upper_bound(12) == pytest.approx(1 - 0.05 ** (1 / 12), abs=0)
    # The registered kernel agrees with the closed form (it rounds outward by <= 1 ulp-scale).
    for n in (4, 12, 59):
        kernel = upper_error(0, n, 0.05)
        assert kernel is not None
        assert kernel == pytest.approx(zero_error_upper_bound(n), abs=1e-12)


def test_five_percent_ceiling_is_inconclusive_for_12_and_4() -> None:
    assert zero_error_upper_bound(12) > 0.05 and zero_error_upper_bound(4) > 0.05
    receipt = run()
    inference = receipt["inference_to_future_cases"]
    assert round(inference["upper_error"] * 100, 2) == 22.09
    assert inference["conclusion"] == "inconclusive"
    assert inference["applicable"] is False
    critical = receipt["coverage"]["critical"]
    assert round(critical["upper_error"] * 100, 2) == 52.71
    assert critical["label"] == "4/4"


def test_families_needed_for_five_percent_ceiling_is_computed() -> None:
    needed = families_needed_for_ceiling(0.05, 0.05)
    assert needed == 59
    assert zero_error_upper_bound(needed) <= 0.05 < zero_error_upper_bound(needed - 1)


def test_design_weighting_requires_every_probability() -> None:
    assert design_weighted_error([True, False], [0.5, 0.5]) == pytest.approx(0.5)
    assert design_weighted_error([True, False], [0.5, None]) is None
    assert design_weighted_error([], []) is None


# ---- Baseline ----


def test_baseline_enumerated_fixture_allows_evaluation_without_customer_alert() -> None:
    receipt = run()
    assert receipt["verdict"] == V.EVALUATION_ALLOWED.value
    assert receipt["fixture_agreement"] == {"matches": 12, "verified": 12}
    flow = receipt["denominator_flow"]
    assert (flow["eligible"], flow["mature"], flow["verified"], flow["scoreable"]) == (
        12,
        12,
        12,
        12,
    )
    assert flow["disputed"] == flow["excluded"] == 0 and flow["reconciles"] == 1
    assert (flow["correction"], flow["correct"], flow["deferral"]) == (4, 6, 2)
    claims = receipt["claims"]
    assert claims["customer_accuracy_alert_permitted"] is False
    assert claims["overall_accuracy_claim"] == "not_established"
    assert claims["overall_accuracy_alerts_emitted"] == 0
    assert receipt["coverage"]["critical"]["label"] == "4/4"
    selection = receipt["audit_selection"]
    assert selection["seed"] == 7 and selection["scoring_version"] == "generated-scorer-v1"
    assert selection["adjudicator_independence"] == "blinded_to_first_label_and_predictions"
    assert receipt["upstream"]["verification"] == "unverified_dependency_ope_416"


def test_receipt_reproduces_and_holds_no_labels_or_family_ids() -> None:
    first, second = run(), run()
    assert first == second and first["receipt_digest"] == second["receipt_digest"]
    shuffled = list(baseline_records())
    random.Random(3).shuffle(shuffled)
    assert run(tuple(shuffled))["receipt_digest"] == first["receipt_digest"]
    wire = json.dumps(first)
    assert "generated-action" not in wire and "generated-r0" not in wire
    assert commit("generated-r01", "generated-action-generated-r01") not in wire


def test_mapper_receives_no_protected_labels() -> None:
    for view in mapper_view(baseline_records()):
        if view["split"] == "protected":
            assert not {"final_label_digest", "independent_label_digest"} & set(view)
            assert "adjudicator_label_digest" not in view and "independent_source" not in view


# ---- Mutations A-D ----


def test_mutation_a_corrections_only_permits_error_family_claim_only() -> None:
    kept = tuple(
        r for r in baseline_records() if r.split == "development" or r.outcome_kind == "correction"
    )
    receipt = run(kept, prereg_overrides={"sampling_design": SamplingDesign.CORRECTION_ONLY})
    assert receipt["verdict"] == V.CLAIM_LIMITED.value
    assert "correction_only_sample" in codes(receipt)
    claims = receipt["claims"]
    assert claims["error_family_claim"] == "permitted"
    assert claims["overall_accuracy_claim"] == "not_established"
    assert claims["overall_accuracy_alerts_emitted"] == 0
    assert claims["customer_accuracy_alert_permitted"] is False


def test_mutation_a_selective_corrections_are_detected_even_under_a_full_design() -> None:
    kept = tuple(
        r for r in baseline_records() if r.split == "development" or r.outcome_kind == "correction"
    )
    assert run(kept)["verdict"] == V.CLAIM_LIMITED.value


def test_mutation_b_no_critical_verification_holds_critical_inclusive_claim() -> None:
    records = tuple(
        replace(
            r,
            adjudicator_id=None,
            adjudicator_label_digest=None,
            independent_source=None,
            independent_label_digest=None,
        )
        if r.stratum == "critical"
        else r
        for r in baseline_records()
    )
    receipt = run(records)
    critical = receipt["coverage"]["critical"]
    assert critical["label"] == "0/4" and critical["status"] == "unavailable"
    assert critical["error_rate"] is None and critical["upper_error"] is None
    assert receipt["claims"]["critical_inclusive_claim"] == "held"
    assert receipt["verdict"] == V.CLAIM_LIMITED.value
    assert "critical_coverage_gap" in codes(receipt)


def test_mutation_c_cross_split_sibling_invalidates_until_resealed() -> None:
    sibling = family(
        "generated-d09",
        "routine",
        "correct",
        split="development",
        root="generated-root-generated-r05",
    )
    leaked = (*baseline_records(), sibling)
    receipt = run(leaked)
    assert receipt["verdict"] == V.INVALID.value and "family_crosses_split" in codes(receipt)
    assert receipt["claims"]["broad_improvement_alert_permitted"] is False
    # Reseal: the sibling leaves the development cohort and the split is frozen afresh.
    assert run(baseline_records())["verdict"] == V.EVALUATION_ALLOWED.value


@pytest.mark.parametrize(
    "key", ["exact_hash", "template_group", "attachment_hash", "ancestry_root"]
)
def test_transformed_duplicate_links_across_cohorts_invalidate(key: str) -> None:
    base = baseline_records()
    target = next(r for r in base if r.family_id == "generated-r02")
    sibling = replace(
        family("generated-d08", "routine", "correct", split="development"),
        **{key: getattr(target, key)},
    )
    receipt = run((*base, sibling))
    assert receipt["verdict"] == V.INVALID.value
    assert receipt["contamination"]["cross_split_overlaps"][key] == 1


def test_mutation_d_label_changed_after_freeze_is_stale_and_names_the_label_manifest() -> None:
    frozen = baseline_records()
    relabeled = tuple(
        replace(r, final_label_digest=commit(r.family_id, "generated-changed-action"))
        if r.family_id == "generated-r03"
        else r
        for r in frozen
    )
    receipt = run(relabeled, frozen_records=frozen)
    assert receipt["verdict"] == V.STALE.value
    assert any("label_manifest" in r["detail"] for r in receipt["reasons"])
    assert receipt["claims"]["broad_improvement_alert_permitted"] is False


# ---- Acceptance packs ----


def test_pack_policy_only_starter_is_preserved() -> None:
    receipt = run((), prereg_overrides={"sampling_design": SamplingDesign.POLICY_ONLY})
    assert receipt["verdict"] == V.STARTER_ONLY.value
    assert "policy_only_starter" in codes(receipt)
    assert receipt["claims"]["overall_accuracy_claim"] == "not_established"


def test_pack_two_reviewers_agree_on_wrong_label_overturned_by_independent_record() -> None:
    wrong = commit("generated-r04", "generated-wrong-action")
    right = commit("generated-r04", "generated-action-generated-r04")
    records = tuple(
        replace(
            r,
            final_label_digest=wrong,
            adjudicator_label_digest=wrong,
            independent_label_digest=right,
        )
        if r.family_id == "generated-r04"
        else r
        for r in baseline_records()
    )
    receipt = run(records)
    assert receipt["verdict"] == V.INVALID.value
    assert "label_overturned_by_independent_record" in codes(receipt)
    assert (
        receipt["label_audit"]["disagreement_matrix"]["overturned_after_two_reviewers_agreed"] == 1
    )
    assert receipt["next_evidence_request"]


def test_pack_unresolved_ambiguity_holds() -> None:
    records = tuple(
        replace(
            r,
            adjudicator_id=None,
            adjudicator_label_digest=None,
            independent_source=None,
            independent_label_digest=None,
            unresolved=True,
        )
        if r.family_id in {"generated-r07", "generated-r08"}
        else r
        for r in baseline_records()
    )
    receipt = run(records)
    assert receipt["verdict"] == V.INSUFFICIENT_EVIDENCE.value
    assert "unresolved_ambiguity" in codes(receipt)
    assert receipt["label_audit"]["disagreement_matrix"]["unresolved"] == 2


def test_pack_future_only_field_is_invalid() -> None:
    receipt = run(consumed_features=(*CONSUMED, "refund_settled_at"))
    assert receipt["verdict"] == V.INVALID.value and "future_only_field" in codes(receipt)
    inline = tuple(
        replace(r, post_decision_fields_in_input=("refund_settled_at",))
        if r.family_id == "generated-r01"
        else r
        for r in baseline_records()
    )
    assert "future_only_field" in codes(run(inline))
    assert "future_only_field" in codes(run(consumed_features=("undeclared_feature",)))


def test_pack_leaked_family_is_the_cross_split_pack() -> None:
    assert test_mutation_c_cross_split_sibling_invalidates_until_resealed() is None


ADVERSE_POPULATIONS = {
    "policy_only": lambda: run(
        (), prereg_overrides={"sampling_design": SamplingDesign.POLICY_ONLY}
    ),
    "empty_audit": lambda: run(
        tuple(
            replace(
                r,
                adjudicator_id=None,
                adjudicator_label_digest=None,
                independent_source=None,
                independent_label_digest=None,
            )
            for r in baseline_records()
        )
    ),
    "future_only": lambda: run(consumed_features=("refund_settled_at",)),
    "protected_selection": lambda: run(selection_log=(SelectionUse("threshold", "protected"),)),
    "prep_on_protected": lambda: run(
        preprocessing=PreprocessingRecord("generated-prep-r1", "protected", "generated-prep-r1")
    ),
    "prep_unverified": lambda: run(
        preprocessing=PreprocessingRecord("generated-prep-r1", "development", None)
    ),
    "unlogged_disclosure": lambda: run(unlogged_disclosures=1),
    "prereg_after_reveal": lambda: run(candidate_outputs_revealed_day=100),
    "superseded": lambda: run(superseded_by="generated-successor"),
}


@pytest.mark.parametrize("name", sorted(ADVERSE_POPULATIONS))
def test_every_invalid_population_yields_hold_and_zero_broad_improvement_alerts(name: str) -> None:
    receipt = ADVERSE_POPULATIONS[name]()
    assert receipt["verdict"] != V.EVALUATION_ALLOWED.value
    assert receipt["claims"]["broad_improvement_alert_permitted"] is False
    assert receipt["claims"]["broad_improvement_alerts_emitted"] == 0
    assert receipt["reasons"] and receipt["next_evidence_request"]


def test_missing_verifier_is_never_passing_and_unblinded_review_does_not_count() -> None:
    unblinded = tuple(
        replace(
            r,
            independent_source=None,
            independent_label_digest=None,
            adjudicator_saw_first_label=True,
        )
        for r in baseline_records()
    )
    receipt = run(unblinded)
    assert receipt["verdict"] == V.INSUFFICIENT_EVIDENCE.value
    assert "no_verified_families" in codes(receipt)
    same_person = tuple(replace(r, adjudicator_id=r.first_labeler_id) for r in unblinded)
    assert run(same_person)["verdict"] == V.INSUFFICIENT_EVIDENCE.value


def test_preregistration_must_precede_reveal_and_be_complete() -> None:
    assert "preregistration_after_reveal" in codes(run(candidate_outputs_revealed_day=100))
    assert "preregistration_incomplete" in codes(run(prereg_overrides={"intended_decision": " "}))


def test_protected_comparison_selection_and_preprocessing_are_denied() -> None:
    assert "protected_comparison_used_for_selection" in codes(
        run(selection_log=(SelectionUse("pick-candidate", "protected"),))
    )
    development_only = run(selection_log=(SelectionUse("pick-candidate", "development"),))
    assert development_only["verdict"] == V.EVALUATION_ALLOWED.value
    assert run(preprocessing=None)["verdict"] == V.INSUFFICIENT_EVIDENCE.value


def test_exposed_families_are_retired_and_the_prior_result_is_unchanged() -> None:
    before = run()
    after = run(exposed_families=frozenset({"generated-c02"}))
    assert after["denominator_flow"]["excluded"] == 1
    assert after["denominator_flow"]["verified"] == 11
    assert after["excluded_reasons"] == ["exposed_answer_key_retired"]
    assert before["fixture_agreement"] == {"matches": 12, "verified": 12}


def test_late_and_immature_rows_leave_the_denominator_explicitly() -> None:
    records = tuple(
        replace(r, decided_day=290) if r.family_id == "generated-r01" else r
        for r in baseline_records()
    )
    flow = run(records, prereg_overrides={"temporal_cutoff_day": 400})["denominator_flow"]
    assert flow["immature"] == 1 and flow["mature"] == 11 and flow["reconciles"] == 1
    late = tuple(
        replace(r, decided_day=250) if r.family_id == "generated-r02" else r
        for r in baseline_records()
    )
    assert run(late)["excluded_reasons"] == ["after_temporal_cutoff"]


def test_duplicate_family_ids_are_invalid() -> None:
    twice = (*baseline_records(), baseline_records()[0])
    assert "duplicate_family_id" in codes(run(twice))


def test_group_collapse_counts_one_independent_unit_per_family_root() -> None:
    base = baseline_records()
    twin = replace(
        family("generated-r09", "routine", "correct", root="generated-root-generated-r01")
    )
    receipt = run((*base, twin))
    assert receipt["label_audit"]["raw_error"]["families"] == 12
    assert receipt["fixture_agreement"]["verified"] == 13


# ---- Real-sample branch (generated stand-in; observed origin needs the OPE-416 binding) ----


def real(**kw: Any) -> dict[str, Any]:
    records, sizes = real_sample_records()
    return run(
        records,
        prereg_overrides={
            "population_kind": PopulationKind.REAL_SAMPLE,
            "evidence_origin": "observed",
            "sampling_design": SamplingDesign.STRATIFIED_RANDOM,
            "sample_sizes": sizes,
            "tolerable_label_error": 0.02,
        },
        **{"candidate_snapshot": OBSERVED_SNAPSHOT, **kw},
    )


def test_real_sample_with_precision_is_eligible_but_alerts_wait_for_the_upstream_receipt() -> None:
    receipt = real()
    assert receipt["verdict"] == V.EVALUATION_ALLOWED.value
    assert receipt["inference_to_future_cases"]["conclusion"] == "ruled_out_at_ceiling"
    # No guard history: "no exposure" is caller-asserted, so the claim is limited (I2 ruling).
    assert receipt["claims"]["overall_accuracy_claim"] == "claim_limited_caller_asserted"
    assert receipt["claims"]["customer_accuracy_alert_permitted"] is False
    assert receipt["label_audit"]["design_weighted_error"] == 0.0
    # A caller's verification flag is caller-asserted and cannot by itself enable the alert;
    # it waits for an upstream receipt the worker verifies itself (OPE-416, unlanded).
    asserted = real(upstream_snapshot_verified=True)
    assert asserted["claims"]["customer_accuracy_alert_permitted"] is False
    assert (
        "upstream_receipt_not_worker_verified"
        in (asserted["claims"]["customer_accuracy_alert_blocked_by"])
    )
    assert any("OPE-416" in text for text in asserted["next_evidence_request"])


def test_real_sample_without_snapshot_binding_is_insufficient() -> None:
    assert "candidate_snapshot_receipt_missing" in codes(real(candidate_snapshot=None))
    bad = replace(OBSERVED_SNAPSHOT, snapshot_digest="not-a-digest")
    assert real(candidate_snapshot=bad)["verdict"] == V.INSUFFICIENT_EVIDENCE.value


def test_small_real_sample_cannot_rule_out_the_ceiling() -> None:
    records, sizes = real_sample_records(20, 20)
    receipt = run(
        records,
        prereg_overrides={
            "population_kind": PopulationKind.REAL_SAMPLE,
            "evidence_origin": "observed",
            "sampling_design": SamplingDesign.STRATIFIED_RANDOM,
            "sample_sizes": sizes,
        },
        candidate_snapshot=OBSERVED_SNAPSHOT,
    )
    assert receipt["verdict"] == V.INSUFFICIENT_EVIDENCE.value
    assert "precision_cannot_rule_out_ceiling" in codes(receipt)
    assert any("59" in text for text in receipt["next_evidence_request"])


def test_audit_sample_must_be_the_reproducible_draw() -> None:
    records, sizes = real_sample_records()
    frame = [(r.family_id, r.stratum) for r in records if r.split == "protected"]
    assert draw_audit_sample(frame, sizes, 7) == draw_audit_sample(frame, sizes, 7)
    assert draw_audit_sample(frame, sizes, 7) != draw_audit_sample(frame, sizes, 8)
    swapped = tuple(
        replace(
            r,
            adjudicator_id=None,
            adjudicator_label_digest=None,
            independent_source=None,
            independent_label_digest=None,
        )
        if r.adjudicator_id and r.family_id.startswith("generated-sr")
        else r
        for r in records
    )
    receipt = run(
        swapped,
        prereg_overrides={
            "population_kind": PopulationKind.REAL_SAMPLE,
            "evidence_origin": "observed",
            "sampling_design": SamplingDesign.STRATIFIED_RANDOM,
            "sample_sizes": sizes,
        },
        candidate_snapshot=OBSERVED_SNAPSHOT,
    )
    assert "audit_sample_not_reproducible" in codes(receipt)


def test_label_error_beyond_tolerance_is_invalid_and_within_tolerance_is_recorded() -> None:
    records, _ = real_sample_records()
    audited = [r for r in records if r.independent_source and r.stratum == "routine"]
    wrong = commit(audited[0].family_id, "generated-other")
    changed = tuple(
        replace(r, independent_label_digest=wrong, adjudicator_label_digest=r.final_label_digest)
        if r.family_id == audited[0].family_id
        else r
        for r in records
    )
    assert INDEPENDENT  # the independent record is the governing authority
    receipt = real_changed(changed, tolerance=0.0)
    assert receipt["verdict"] == V.INVALID.value
    within = real_changed(changed, tolerance=0.02)
    assert within["verdict"] == V.INVALID.value  # overturned by the independent record
    assert "label_overturned_by_independent_record" in codes(within)


def real_changed(records: Any, tolerance: float, **kw: Any) -> dict[str, Any]:
    _, sizes = real_sample_records()
    return run(
        records,
        prereg_overrides={
            "population_kind": PopulationKind.REAL_SAMPLE,
            "evidence_origin": "observed",
            "sampling_design": SamplingDesign.STRATIFIED_RANDOM,
            "sample_sizes": sizes,
            "tolerable_label_error": tolerance,
        },
        candidate_snapshot=OBSERVED_SNAPSHOT,
        **kw,
    )


def test_single_label_error_within_registered_tolerance_is_recorded_not_invalid() -> None:
    records, sizes = real_sample_records()
    target = next(r for r in records if r.independent_source and r.stratum == "routine")
    wrong = commit(target.family_id, "generated-other")
    changed = tuple(
        replace(
            r,
            independent_source=None,
            independent_label_digest=None,
            adjudicator_label_digest=wrong,
        )
        if r.family_id == target.family_id
        else r
        for r in records
    )
    within = real_changed(changed, tolerance=0.02)
    assert within["verdict"] == V.EVALUATION_ALLOWED.value
    assert within["label_audit"]["disagreement_matrix"]["disagree"] == 1
    assert within["denominator_flow"]["disputed"] == 1
    assert within["denominator_flow"]["scoreable"] == within["denominator_flow"]["verified"] - 1
    beyond = real_changed(changed, tolerance=0.0)
    assert beyond["verdict"] == V.INVALID.value
    assert "label_error_exceeds_tolerance" in codes(beyond)
    assert sizes


# ---- Adjudicator independence: each recorded condition is load-bearing ----


def adjudicator_only(**changes: Any) -> tuple[Any, ...]:
    """Every protected family verified by the blinded adjudicator alone; r01 takes ``changes``."""
    return tuple(
        replace(
            r,
            independent_source=None,
            independent_label_digest=None,
            **(changes if r.family_id == "generated-r01" else {}),
        )
        for r in baseline_records()
    )


def test_blinded_distinct_adjudicator_alone_verifies_every_family() -> None:
    receipt = run(adjudicator_only())
    assert receipt["verdict"] == V.EVALUATION_ALLOWED.value
    assert receipt["denominator_flow"]["verified"] == 12


def test_adjudicator_who_saw_predictions_does_not_verify_the_family() -> None:
    receipt = run(adjudicator_only(adjudicator_saw_predictions=True))
    assert receipt["denominator_flow"]["verified"] == 11
    assert receipt["denominator_flow"]["unverified"] == 1
    every = tuple(replace(r, adjudicator_saw_predictions=True) for r in adjudicator_only())
    assert run(every)["verdict"] == V.INSUFFICIENT_EVIDENCE.value
    assert "no_verified_families" in codes(run(every))


def test_adjudicator_who_is_the_first_labeler_does_not_verify_the_family() -> None:
    receipt = run(adjudicator_only(adjudicator_id="generated-labeler-a"))
    assert receipt["denominator_flow"]["verified"] == 11
    assert receipt["denominator_flow"]["unverified"] == 1
    every = tuple(replace(r, adjudicator_id=r.first_labeler_id) for r in adjudicator_only())
    assert run(every)["verdict"] == V.INSUFFICIENT_EVIDENCE.value


@pytest.mark.parametrize(
    ("changes", "counter"),
    [
        ({"adjudicator_saw_predictions": True}, "recorded_blind_to_predictions"),
        ({"adjudicator_saw_first_label": True}, "recorded_blind_to_first_label"),
        ({"adjudicator_id": "generated-labeler-a"}, "distinct_from_first_labeler"),
    ],
)
def test_receipt_states_blinding_only_when_recorded_conditions_show_it(
    changes: dict[str, Any], counter: str
) -> None:
    baseline = run()["audit_selection"]
    assert baseline["adjudicator_independence"] == "blinded_to_first_label_and_predictions"
    assert baseline["adjudicator_independence_recorded"][counter] == 12
    # r01 is still verified by the independent record, so the verdict need not move; the
    # receipt must nonetheless stop claiming that every adjudication was blinded.
    records = tuple(
        replace(r, **changes) if r.family_id == "generated-r01" else r for r in baseline_records()
    )
    selection = run(records)["audit_selection"]
    assert selection["adjudicator_independence"] == "independence_not_shown_for_every_adjudication"
    assert selection["adjudicator_independence_recorded"][counter] == 11
    assert selection["adjudicator_independence_recorded"]["adjudicated"] == 12


def test_receipt_states_no_adjudication_when_only_independent_records_verify() -> None:
    records = tuple(
        replace(r, adjudicator_id=None, adjudicator_label_digest=None) for r in baseline_records()
    )
    receipt = run(records)
    assert receipt["verdict"] == V.EVALUATION_ALLOWED.value
    assert receipt["audit_selection"]["adjudicator_independence"] == "no_adjudication_recorded"


# ---- Caller-asserted fields cannot lift a hold ----


def test_caller_asserted_exposure_cannot_launder_a_disputed_or_unresolved_family() -> None:
    wrong = commit("generated-r04", "generated-wrong-action")
    overturned = tuple(
        replace(r, final_label_digest=wrong, adjudicator_label_digest=wrong)
        if r.family_id == "generated-r04"
        else r
        for r in baseline_records()
    )
    retired = run(overturned, exposed_families=frozenset({"generated-r04"}))
    assert retired["verdict"] == V.INVALID.value
    assert "label_overturned_by_independent_record" in codes(retired)
    assert retired["label_audit"]["label_quality_gate"]["overturned"] == 1
    unresolved = tuple(
        replace(
            r,
            adjudicator_id=None,
            adjudicator_label_digest=None,
            independent_source=None,
            independent_label_digest=None,
            unresolved=True,
        )
        if r.family_id in {"generated-r07", "generated-r08"}
        else r
        for r in baseline_records()
    )
    hidden = run(unresolved, exposed_families=frozenset({"generated-r07", "generated-r08"}))
    assert hidden["verdict"] == V.INSUFFICIENT_EVIDENCE.value
    assert "unresolved_ambiguity" in codes(hidden)


def test_caller_asserted_upstream_verification_cannot_lift_a_held_verdict() -> None:
    small, sizes = real_sample_records(20, 20)
    overrides = {
        "population_kind": PopulationKind.REAL_SAMPLE,
        "evidence_origin": "observed",
        "sampling_design": SamplingDesign.STRATIFIED_RANDOM,
        "sample_sizes": sizes,
    }
    held = {
        "imprecise": run(
            small,
            prereg_overrides=overrides,
            candidate_snapshot=OBSERVED_SNAPSHOT,
            upstream_snapshot_verified=True,
        ),
        "no_snapshot": real(candidate_snapshot=None, upstream_snapshot_verified=True),
        "unlogged": real(unlogged_disclosures=1, upstream_snapshot_verified=True),
    }
    for name, receipt in held.items():
        assert receipt["verdict"] != V.EVALUATION_ALLOWED.value, name
        assert receipt["claims"]["customer_accuracy_alert_permitted"] is False, name
        assert receipt["claims"]["overall_accuracy_claim"] == "not_established", name
        assert receipt["upstream"]["verification"] == "caller_asserted_by_consumer", name


def test_receipt_labels_caller_asserted_fields_and_the_claims_resting_on_them() -> None:
    provenance = run()["provenance"]
    assert provenance == {
        "exposed_families": "caller_asserted",
        "unlogged_disclosures": "caller_asserted",
        "exposure_log_covers_protected_families": True,
        "exposure_history_nonempty": False,
        "exposure_history_signed": False,
        "excluded_reason": "caller_asserted",
        "decided_day": "caller_asserted",
        "upstream_snapshot_verified": "caller_asserted",
        "evidence_origin": "caller_asserted",
        "adjudicator_blinding_flags": "caller_asserted",
        "adjudicator_distinct_from_first_labeler": "worker_compared_supplied_ids",
    }
    assert run()["claims"]["caller_asserted_dependencies"] == []
    limited = real()["claims"]
    assert limited["overall_accuracy_claim"] == "claim_limited_caller_asserted"
    assert limited["caller_asserted_dependencies"] == ["exposed_families", "unlogged_disclosures"]
    alert = real(upstream_snapshot_verified=True)["claims"]
    assert alert["customer_accuracy_alert_permitted"] is False
    assert alert["caller_asserted_dependencies"] == ["exposed_families", "unlogged_disclosures"]


# ---- Exposure histories are supplied and unsigned until OPE-566 ----

REAL_UNIVERSE = frozenset(r.family_id for r in real_sample_records()[0] if r.split == "protected")


def selector_query(query_id: str, cell: set[str], roles: frozenset[str] = SELECTOR) -> Any:
    return QueryRequest(query_id, Surface.CHART, Channel.WORKER_UI, frozenset(cell), roles)


def harmless_history() -> Any:
    """A fresh guard over the real universe with one harmless selector release.

    Anyone can build this. It shows nothing about the guard that served real traffic.
    """
    guard = ExposureGuard(REAL_UNIVERSE, frozenset())
    assert guard.evaluate(selector_query("q", {"generated-sr000", "generated-sr001"})).allowed
    return guard.record()


def test_repro_fresh_guard_history_cannot_launder_a_guard_that_leaked() -> None:
    served = ExposureGuard(REAL_UNIVERSE, frozenset())
    assert served.evaluate(selector_query("s", {"generated-sr000", "generated-sr001"})).allowed
    reader = frozenset({"reader"})
    assert served.evaluate(selector_query("r", {"generated-sr000"}, reader)).allowed
    assert real(exposure_record=served.record())["verdict"] == V.INVALID.value
    fresh = real(exposure_record=harmless_history())
    assert fresh["verdict"] == V.EVALUATION_ALLOWED.value  # the replayed history is harmless
    assert fresh["provenance"]["exposed_families"] == "replayed_from_supplied_unsigned_history"
    assert fresh["claims"]["overall_accuracy_claim"] == "claim_limited_caller_asserted"
    assert fresh["claims"]["caller_asserted_dependencies"] == [
        "exposed_families",
        "unlogged_disclosures",
    ]
    assert fresh["claims"]["customer_accuracy_alert_permitted"] is False
    assert (
        "exposure_not_derived_from_covering_history"
        in (fresh["claims"]["customer_accuracy_alert_blocked_by"])
    )


def test_repro_releases_split_across_two_guards_are_never_worker_held() -> None:
    p0, p1, p2 = "generated-sr000", "generated-sr001", "generated-sr002"
    first = ExposureGuard(REAL_UNIVERSE, frozenset())
    second = ExposureGuard(REAL_UNIVERSE, frozenset())
    assert first.evaluate(selector_query("wide", {p0, p1, p2})).allowed
    assert second.evaluate(selector_query("narrow", {p0, p1})).allowed
    combined = ExposureRecord(REAL_UNIVERSE, first.record().releases + second.record().releases)
    assert p2 in replay_exposure(combined)[0]  # together they isolate p2
    for history in (first.record(), second.record()):
        receipt = real(exposure_record=history)
        assert receipt["contamination"]["exposed_families_retired"] == 0
        assert receipt["claims"]["overall_accuracy_claim"] == "claim_limited_caller_asserted"
        assert "exposed_families" in receipt["claims"]["caller_asserted_dependencies"]
        assert receipt["claims"]["customer_accuracy_alert_permitted"] is False


def test_customer_accuracy_alert_requires_worker_held_evidence_not_caller_flags() -> None:
    blocked = "customer_accuracy_alert_blocked_by"
    every_blocker = [
        "upstream_receipt_not_worker_verified",
        "exposure_not_derived_from_covering_history",
        "claim_rests_on_caller_assertions",
    ]
    no_history = real(upstream_snapshot_verified=True)["claims"]
    assert no_history["customer_accuracy_alert_permitted"] is False
    assert no_history[blocked] == every_blocker
    supplied = real(upstream_snapshot_verified=True, exposure_record=harmless_history())
    assert supplied["verdict"] == V.EVALUATION_ALLOWED.value
    assert supplied["claims"]["customer_accuracy_alert_permitted"] is False
    assert supplied["claims"][blocked] == every_blocker
    assert supplied["claims"]["broad_improvement_alert_permitted"] is False
    assert any("OPE-416" in text for text in supplied["next_evidence_request"])
    generated = run()["claims"][blocked]
    assert generated[:2] == ["overall_accuracy_not_eligible", "evidence_origin_not_observed"]


def test_sealed_history_tampered_after_issue_is_invalid() -> None:
    served = ExposureGuard(REAL_UNIVERSE, frozenset())
    served.evaluate(selector_query("s", {"generated-sr000", "generated-sr001"}))
    served.evaluate(selector_query("r", {"generated-sr000"}, frozenset({"reader"})))
    history = served.record()
    assert real(exposure_record=history)["verdict"] == V.INVALID.value
    object.__setattr__(history, "releases", ())  # erase the leak from the frozen record
    tampered = real(exposure_record=history)
    assert tampered["verdict"] == V.INVALID.value
    assert "exposure_history_tampered" in codes(tampered)
    assert tampered["next_evidence_request"]
    object.__setattr__(history, "content_digest", None)  # an unsealed record is not intact
    assert "exposure_history_tampered" in codes(real(exposure_record=history))
    clean = real(exposure_record=harmless_history())
    assert "exposure_history_tampered" not in codes(clean)
    assert served.record().intact() and not history.intact()


def test_history_coverage_and_emptiness_are_recorded_and_hold() -> None:
    narrow = ExposureGuard(frozenset({"generated-sr000", "generated-sr001"}), frozenset())
    narrow.evaluate(selector_query("q", {"generated-sr000", "generated-sr001"}))
    narrowed = real(exposure_record=narrow.record())
    assert narrowed["verdict"] == V.INSUFFICIENT_EVIDENCE.value
    assert narrowed["provenance"]["exposure_log_covers_protected_families"] is False
    empty = real(exposure_record=ExposureGuard(REAL_UNIVERSE, frozenset()).record())
    assert empty["provenance"]["exposure_history_nonempty"] is False
    assert empty["claims"]["overall_accuracy_claim"] == "claim_limited_caller_asserted"


def stand_in_history_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test-local stand-in for the OPE-566 verifier; production inputs cannot reach it."""
    monkeypatch.setattr(audit_module, "_exposure_history_verified_by_worker", lambda _r: True)


def test_overall_claim_is_eligible_only_when_every_dependency_is_worker_derived(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stand_in_history_verifier(monkeypatch)
    held = real(exposure_record=harmless_history())["claims"]
    assert held["overall_accuracy_claim"] == "eligible_pending_upstream_receipt"
    assert held["caller_asserted_dependencies"] == []
    # Even a verified history must cover every family and be non-empty.
    empty = real(exposure_record=ExposureGuard(REAL_UNIVERSE, frozenset()).record())["claims"]
    assert empty["overall_accuracy_claim"] == "claim_limited_caller_asserted"
    narrow = ExposureGuard(frozenset({"generated-sr000", "generated-sr001"}), frozenset())
    narrow.evaluate(selector_query("q", {"generated-sr000", "generated-sr001"}))
    narrowed = real(exposure_record=narrow.record())["claims"]
    assert (
        "exposure_not_derived_from_covering_history"
        in (narrowed["customer_accuracy_alert_blocked_by"])
    )
    records, _ = real_sample_records()
    target = next(r for r in records if r.independent_source and r.stratum == "routine")
    adjudicated = tuple(
        replace(r, independent_source=None, independent_label_digest=None)
        if r.family_id == target.family_id
        else r
        for r in records
    )
    blinded = real_changed(adjudicated, 0.02, exposure_record=harmless_history())["claims"]
    assert blinded["overall_accuracy_claim"] == "claim_limited_caller_asserted"
    assert blinded["caller_asserted_dependencies"] == ["adjudicator_blinding_flags"]
    unsampled = next(r for r in records if r.split == "protected" and not r.independent_source)
    excluded = tuple(
        replace(r, excluded_reason="generated-caller-exclusion")
        if r.family_id == unsampled.family_id
        else r
        for r in records
    )
    caller_excluded = real_changed(excluded, 0.02, exposure_record=harmless_history())["claims"]
    assert caller_excluded["caller_asserted_dependencies"] == ["excluded_reason"]
    late = tuple(
        replace(r, decided_day=250) if r.family_id == unsampled.family_id else r for r in records
    )
    caller_timed = real_changed(late, 0.02, exposure_record=harmless_history())["claims"]
    assert caller_timed["overall_accuracy_claim"] == "claim_limited_caller_asserted"
    assert caller_timed["caller_asserted_dependencies"] == ["decided_day"]


def test_alert_gate_opens_only_with_stand_in_verifiers_and_a_covering_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gate composition only. Neither a verifiable OPE-416 receipt nor a signed OPE-566 history
    exists (held gaps); both stand-ins are test-local and unreachable from production inputs."""
    stand_in_history_verifier(monkeypatch)
    assert (
        real(exposure_record=harmless_history())["claims"]["customer_accuracy_alert_permitted"]
        is False
    )  # the upstream receipt is still unverified
    monkeypatch.setattr(audit_module, "_upstream_receipt_verified_by_worker", lambda _s: True)
    allowed = real(exposure_record=harmless_history())["claims"]
    assert allowed["customer_accuracy_alert_permitted"] is True
    assert allowed["customer_accuracy_alert_blocked_by"] == []
    assert allowed["caller_asserted_dependencies"] == ["evidence_origin"]
    assert real()["claims"]["customer_accuracy_alert_permitted"] is False  # no history


def test_caller_set_timing_cannot_launder_a_disputed_or_unresolved_family() -> None:
    wrong = commit("generated-r04", "generated-wrong-action")
    for day in (201, 290):  # after the cutoff; inside the cutoff but immature at as-of
        overturned = tuple(
            replace(r, final_label_digest=wrong, adjudicator_label_digest=wrong, decided_day=day)
            if r.family_id == "generated-r04"
            else r
            for r in baseline_records()
        )
        cutoff = 200 if day == 201 else 400
        receipt = run(overturned, prereg_overrides={"temporal_cutoff_day": cutoff})
        assert receipt["verdict"] == V.INVALID.value, day
        assert "label_overturned_by_independent_record" in codes(receipt)
    unresolved = tuple(
        replace(
            r,
            adjudicator_id=None,
            adjudicator_label_digest=None,
            independent_source=None,
            independent_label_digest=None,
            unresolved=True,
            decided_day=201,
        )
        if r.family_id in {"generated-r07", "generated-r08"}
        else r
        for r in baseline_records()
    )
    held = run(unresolved)
    assert "unresolved_ambiguity" in codes(held)
    assert held["provenance"]["decided_day"] == "caller_asserted"


# ---- Caller-set exclusions, identity types and malformed caller values ----


def test_caller_set_exclusion_cannot_launder_a_disputed_or_unresolved_family() -> None:
    wrong = commit("generated-r04", "generated-wrong-action")
    overturned = tuple(
        replace(
            r,
            final_label_digest=wrong,
            adjudicator_label_digest=wrong,
            excluded_reason="generated-caller-exclusion",
        )
        if r.family_id == "generated-r04"
        else r
        for r in baseline_records()
    )
    receipt = run(overturned)
    assert receipt["verdict"] == V.INVALID.value
    assert "label_overturned_by_independent_record" in codes(receipt)
    unresolved = tuple(
        replace(
            r,
            adjudicator_id=None,
            adjudicator_label_digest=None,
            independent_source=None,
            independent_label_digest=None,
            unresolved=True,
            excluded_reason="generated-caller-exclusion",
        )
        if r.family_id in {"generated-r07", "generated-r08"}
        else r
        for r in baseline_records()
    )
    assert "unresolved_ambiguity" in codes(run(unresolved))
    assert run(unresolved)["provenance"]["excluded_reason"] == "caller_asserted"


class _LyingId(str):
    """A str subclass that claims to differ from every other value."""

    def __eq__(self, other: object) -> bool:
        return False

    def __ne__(self, other: object) -> bool:
        return True

    __hash__ = str.__hash__


def test_identity_fields_must_be_exact_str_so_a_subclass_cannot_fake_independence() -> None:
    liar = run(adjudicator_only(adjudicator_id=_LyingId("generated-labeler-a")))
    assert liar["denominator_flow"]["verified"] == 11
    counts = liar["audit_selection"]["adjudicator_independence_recorded"]
    assert counts["distinct_from_first_labeler"] == 11
    # r01's only verifier would be an independent source given as a str subclass: not accepted.
    records = tuple(
        replace(
            r,
            independent_source=_LyingId("generated-source"),
            adjudicator_saw_predictions=True,
        )
        if r.family_id == "generated-r01"
        else r
        for r in baseline_records()
    )
    assert run(records)["denominator_flow"]["verified"] == 11


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unlogged_disclosures", -5),
        ("unlogged_disclosures", True),
        ("unlogged_disclosures", 1.0),
        ("exposed_families", {"generated-r01"}),
        ("exposed_families", frozenset({1})),
    ],
)
def test_malformed_caller_exposure_values_are_invalid(field: str, value: object) -> None:
    receipt = run(**{field: value})
    assert receipt["verdict"] == V.INVALID.value
    assert {"code": "caller_assertion_malformed", "detail": field} in receipt["reasons"]
    assert receipt["next_evidence_request"]


def test_max_unresolved_boundary_is_inclusive() -> None:
    def unresolved(count: int) -> tuple[Any, ...]:
        chosen = {f"generated-r0{i}" for i in range(1, count + 1)}
        return tuple(
            replace(
                r,
                adjudicator_id=None,
                adjudicator_label_digest=None,
                independent_source=None,
                independent_label_digest=None,
                unresolved=True,
            )
            if r.family_id in chosen
            else r
            for r in baseline_records()
        )

    at = run(unresolved(2), prereg_overrides={"max_unresolved": 2})
    assert "unresolved_ambiguity" not in codes(at)
    assert at["verdict"] == V.EVALUATION_ALLOWED.value
    over = run(unresolved(3), prereg_overrides={"max_unresolved": 2})
    assert "unresolved_ambiguity" in codes(over)
