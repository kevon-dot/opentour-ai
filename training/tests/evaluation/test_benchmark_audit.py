"""OPE-526 audit receipt: statistics, fixture mutations A-D and acceptance packs.

Fixture kind: generated. None of this is provider or customer evidence, and the upstream
OPE-416 snapshot receipt is unlanded, so the end-to-end gate is not exercised here.
"""

from __future__ import annotations

import json
import random
from dataclasses import replace
from typing import Any

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
from opentour_training.evaluation.benchmark_audit.inference import (
    design_weighted_error,
    families_needed_for_ceiling,
    upper_error,
    zero_error_upper_bound,
)
from opentour_training.evaluation.benchmark_audit.model import (
    PopulationKind,
    PreprocessingRecord,
    SamplingDesign,
    SelectionUse,
    Verdict,
)

V = Verdict


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
    assert receipt["claims"]["overall_accuracy_claim"] == "eligible_pending_upstream_receipt"
    assert receipt["claims"]["customer_accuracy_alert_permitted"] is False
    assert receipt["label_audit"]["design_weighted_error"] == 0.0
    # Consumer contract only: a future OPE-416 consumer would set this after verifying it.
    assert real(upstream_snapshot_verified=True)["claims"]["customer_accuracy_alert_permitted"]


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


def real_changed(records: Any, tolerance: float) -> dict[str, Any]:
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
