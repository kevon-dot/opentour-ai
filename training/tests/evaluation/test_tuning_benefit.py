"""OPE-517 software checks over GENERATED sentinel data only.

Fixture kind: generated. These tests prove mechanics and refusals; they are not provider,
customer or real-workplace evidence, and no real model is fitted.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest
from opentour_training.evaluation import tuning_benefit as tb
from opentour_training.evaluation.tuning_benefit import (
    AUGMENTED,
    REAL_ONLY,
    ArmRecord,
    CaseRef,
    Checkpoint,
    Preregistration,
    ProtectedKey,
    SelectionUse,
    SpendLedger,
    TuningBenefitError,
)

GROUPS = 100
PER_GROUP = 2


def prereg(**over: Any) -> Preregistration:
    base = dict(
        protocol_id="generated-protocol-1",
        model_ref="simulated:model-1",
        base_revision="generated-rev-1",
        task_ref="simulated:task-1",
        scorer_version="generated-scorer-1",
        operating_point_ref="simulated:op-1",
        recipe_id="generated-recipe-1",
        seed=7,
        fit_steps=100,
        critical_slices=("crit",),
        practical_threshold=0.3,
        critical_slice_tolerance=0.0,
        min_slice_cases=4,
        min_independent_families=20,
        alpha=0.05,
        fits_authorized=2,
        spend_cap=1000,
    )
    base.update(over)
    return Preregistration(**base)  # type: ignore[arg-type]


def case(i: int, j: int, *, crit: bool = False, prefix: str = "u") -> CaseRef:
    return CaseRef(
        case_id=f"generated-{prefix}-{i}-{j}",
        family_id=f"generated-fam-{prefix}-{i}",
        text=f"generated sentinel {prefix} text number {i} variant {j} zq{i}x{j}",
        slices=("crit",) if crit else (),
    )


def candidates(crit_groups: int = 10) -> list[CaseRef]:
    return [case(i, j, crit=i < crit_groups) for i in range(GROUPS) for j in range(PER_GROUP)]


def key_for(cases: list[CaseRef]) -> ProtectedKey:
    return ProtectedKey({c.case_id: "act-a" for c in cases})


def child(
    cid: str, root: str, fam: str, disposition: str = "admitted", text: str = "t"
) -> dict[str, Any]:
    return {
        "child_id": cid,
        "origin": "synthetic",
        "mode": "training",
        "disposition": disposition,
        "root_ref": root,
        "parent_case_ref": root,
        "family_id": fam,
        "rendered_text": f"generated child {text} {cid}",
    }


def frozen(
    cases: list[CaseRef] | None = None,
    *,
    p: Preregistration | None = None,
    training: list[CaseRef] | None = None,
    dev: list[CaseRef] | None = None,
    children: list[dict[str, Any]] | None = None,
    uses: list[SelectionUse] | None = None,
    declared: int | None = None,
) -> tuple[tb.FrozenProtocol, ProtectedKey, list[CaseRef]]:
    cs = cases if cases is not None else candidates()
    key = key_for(cs)
    proto = tb.freeze_protocol(
        p or prereg(),
        candidates=cs,
        real_training_roots=training or [case(i, 0, prefix="t") for i in range(5)],
        development=dev or [case(i, 0, prefix="d") for i in range(3)],
        variant_children=children
        if children is not None
        else [child("var-1", "generated-t-0-0", "generated-fam-t-0")],
        selection_uses=uses or [],
        answer_key_digest=tb.key_digest(key),
        declared_independent_family_count=declared,
    )
    return proto, key, cs


def arm(proto: tb.FrozenProtocol, arm_id: str, correct: set[str], **over: Any) -> ArmRecord:
    eligible = proto.eligible
    base = dict(
        arm_id=arm_id,
        status="completed",
        model_ref="simulated:model-1",
        base_revision="generated-rev-1",
        recipe_id="generated-recipe-1",
        seed=7,
        steps=100,
        real_row_ids=("generated-t-0-0", "generated-t-1-0"),
        synthetic_child_ids=("var-1",) if arm_id == AUGMENTED else (),
        fit_cost=10,
        generation_cost=3 if arm_id == AUGMENTED else 0,
        predictions={c: ("act-a" if c in correct else "act-wrong") for c in eligible},
        declared_case_ids=tuple(eligible),
    )
    base.update(over)
    return ArmRecord(**base)  # type: ignore[arg-type]


def run(
    proto: tb.FrozenProtocol,
    key: ProtectedKey,
    a: ArmRecord | None,
    b: ArmRecord | None,
    **kw: Any,
) -> dict[str, Any]:
    arms = [x for x in (a, b) if x is not None]
    return tb.measure(proto, tb.seal_arms(proto, arms), arms, key, **kw)


def test_measured_benefit_on_generated_fixture_is_scoped_implementation_only() -> None:
    proto, key, _ = frozen()
    el = list(proto.eligible)
    a = arm(proto, REAL_ONLY, set(el[: len(el) // 2]))
    b = arm(proto, AUGMENTED, set(el))
    res = run(proto, key, a, b)
    body = res["body"]
    assert body["verdict"] == "measured_benefit" and body["promotion_allowed"]
    assert body["claim_scope"] == "implementation_only" and body["significance_claim"] is False
    assert body["paired"]["fixes"] == len(el) // 2 and body["paired"]["new_errors"] == 0
    assert body["cost"]["incremental"] == 3 and body["cost"]["augmented_generation_separate"] == 3
    assert (
        "authorized_real_heldout_workplace_cases_unavailable" in body["unsatisfied_external_gates"]
    )


def test_remeasuring_identical_inputs_reproduces_verdict_and_digest() -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    a, b = arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el)
    assert run(proto, key, a, b) == run(proto, key, a, b)
    proto2, key2, _ = frozen()
    assert proto2.protocol_digest == proto.protocol_digest


def test_no_material_benefit_when_identical_arms() -> None:
    proto, key, _ = frozen()
    ok = set(proto.eligible)
    res = run(proto, key, arm(proto, REAL_ONLY, ok), arm(proto, AUGMENTED, ok))
    assert res["body"]["verdict"] == "no_material_benefit"
    assert res["body"]["preferred_arm"] == REAL_ONLY  # tie goes to the cheaper option


def test_aggregate_regression_verdict() -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    res = run(proto, key, arm(proto, REAL_ONLY, el), arm(proto, AUGMENTED, set()))
    assert res["body"]["verdict"] == "regression" and not res["body"]["promotion_allowed"]
    assert res["body"]["paired"]["new_errors"] == len(el)


def test_small_gain_with_wide_interval_is_inconclusive_not_a_win() -> None:
    proto, key, _ = frozen()
    el = sorted(proto.eligible)
    fixed = set(el[:40])  # mean effect 0.2: straddles zero and threshold
    res = run(proto, key, arm(proto, REAL_ONLY, set(el) - fixed), arm(proto, AUGMENTED, set(el)))
    assert res["body"]["verdict"] == "inconclusive"
    assert res["body"]["claim"] == tb.UNPROVEN and res["body"]["improvement_claim_blocked"]


def test_critical_slice_regression_blocks_promotion_despite_aggregate_gain() -> None:
    proto, key, _ = frozen()
    el = sorted(proto.eligible)
    crit = [c for c in el if "crit" in proto.slices_of[c]]
    rest = [c for c in el if c not in crit]
    a = arm(proto, REAL_ONLY, set(crit))  # A right only on the critical slice
    b = arm(proto, AUGMENTED, set(rest))  # B fixes everything else, breaks the slice
    res = run(proto, key, a, b)
    assert res["body"]["verdict"] == "regression"
    assert res["body"]["promotion_allowed"] is False
    assert res["body"]["paired"]["mean_group_effect"] > 0  # aggregate gain, still blocked
    assert any(r.startswith("critical_slice_regression") for r in res["body"]["reasons"])


def test_critical_slice_too_small_is_unknown_decisive_metric() -> None:
    cs = candidates(crit_groups=1)  # 2 critical cases < min_slice_cases 4
    proto, key, _ = frozen(cs)
    el = set(proto.eligible)
    res = run(proto, key, arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el))
    assert res["body"]["verdict"] == "inconclusive"
    assert "critical_slice_unknown:crit" in res["body"]["reasons"]


@pytest.mark.parametrize("which", ["training", "dev", "child_any_disposition", "selection"])
def test_touched_cases_are_excluded_and_stay_visible(which: str) -> None:
    cs = candidates()
    victim = cs[0]
    kw: dict[str, Any] = {}
    if which == "training":
        kw["training"] = [victim]
    elif which == "dev":
        kw["dev"] = [victim]
    elif which == "child_any_disposition":
        kw["children"] = [child("var-9", victim.case_id, "other", disposition="rejected")]
    else:
        kw["uses"] = [SelectionUse("threshold_selection", case_ids=(victim.case_id,))]
    proto, _, _ = frozen(cs, **kw)
    assert victim.case_id not in proto.eligible
    ex = dict(map(tuple, proto.body["untouched"]["excluded"]))
    assert ex[victim.case_id].startswith("touched_by_")
    assert proto.body["untouched"]["denominator"] == len(proto.eligible)


def test_touching_one_member_excludes_the_whole_family() -> None:
    cs = candidates()
    proto, _, _ = frozen(cs, uses=[SelectionUse("model_choice", family_ids=(cs[0].family_id,))])
    assert cs[0].case_id not in proto.eligible and cs[1].case_id not in proto.eligible


def test_near_duplicate_of_training_text_is_excluded() -> None:
    cs = candidates()
    dup = dataclasses.replace(cs[10], text="Generated  SENTINEL, u... text number 10 ")
    root = dataclasses.replace(dup, case_id="generated-t-x", family_id="generated-fam-t-x")
    root = dataclasses.replace(root, text="sentinel generated text u number 10")
    cs[10] = dup
    proto, _, _ = frozen(cs, training=[root])
    assert cs[10].case_id not in proto.eligible


def test_near_duplicate_families_do_not_inflate_independent_count() -> None:
    cs = candidates()
    # families 0 and 1 become the same content under different family ids
    cs[2] = dataclasses.replace(cs[2], text=cs[0].text.upper() + "!!")
    cs[3] = dataclasses.replace(cs[3], text=cs[1].text)
    proto, _, _ = frozen(cs)
    assert proto.body["untouched"]["independent_group_count"] == GROUPS - 1
    assert proto.group_of[cs[0].case_id] == proto.group_of[cs[2].case_id]


def test_declared_family_count_is_not_trusted_and_disagreement_is_recorded() -> None:
    proto, _, _ = frozen(declared=GROUPS + 40)
    rec = proto.body["family_count_reconciliation"]
    assert rec["derived_here"] == GROUPS and rec["agrees"] is False
    assert proto.body["untouched"]["independent_group_count"] == GROUPS


def test_unresolved_stays_visible_outside_the_denominator() -> None:
    cs = candidates()
    cs[0] = dataclasses.replace(cs[0], resolved=False)
    proto, key, _ = frozen(cs)
    assert cs[0].case_id in proto.body["untouched"]["unresolved"]
    assert cs[0].case_id not in proto.eligible
    assert proto.body["untouched"]["denominator"] == len(cs) - 1


def test_key_none_cases_are_unscored_not_wrong() -> None:
    proto, key, _ = frozen()
    el = sorted(proto.eligible)
    answers = dict(key.answers)
    answers[el[0]] = None
    key2 = ProtectedKey(answers)
    proto2 = tb.freeze_protocol(
        prereg(),
        candidates=candidates(),
        real_training_roots=[case(0, 0, prefix="t")],
        development=[],
        variant_children=[child("var-1", "generated-t-0-0", "f")],
        selection_uses=[],
        answer_key_digest=tb.key_digest(key2),
    )
    ok = set(proto2.eligible)
    res = run(
        proto2,
        key2,
        arm(proto2, REAL_ONLY, ok),
        arm(proto2, AUGMENTED, ok, real_row_ids=("generated-t-0-0", "generated-t-1-0")),
    )
    assert res["body"]["paired"]["scorable_cases"] == len(el) - 1


@pytest.mark.parametrize(
    "use",
    [
        SelectionUse("variant_generation", case_ids=("generated-u-3-0",)),
        SelectionUse("threshold_selection", case_ids=("generated-u-3-1",)),
        SelectionUse("model_choice", used_protected_results=True),
    ],
)
def test_post_freeze_leak_invalidates_and_reports_no_numbers(use: SelectionUse) -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    res = run(
        proto, key, arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el), post_freeze_uses=[use]
    )
    body = res["body"]
    assert body["status"] == "invalidated" and body["paired"] is None
    assert body["verdict"] == "inconclusive" and not body["promotion_allowed"]


def test_untouched_case_in_arm_fit_rows_invalidates() -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    leaky = arm(proto, AUGMENTED, el, real_row_ids=("generated-t-0-0", "generated-u-5-0"))
    res = run(proto, key, arm(proto, REAL_ONLY, set(), real_row_ids=leaky.real_row_ids), leaky)
    assert res["body"]["status"] == "invalidated"
    assert any("untouched_case_in_fit_rows" in r for r in res["body"]["reasons"])


def test_protected_answer_key_case_never_reaches_fit_path() -> None:
    proto, key, _ = frozen()
    ids = ("generated-t-0-0", "generated-d-0-0")
    odd = ProtectedKey({**key.answers, "generated-d-0-0": "act-a"})
    proto2 = tb.freeze_protocol(
        prereg(),
        candidates=candidates(),
        real_training_roots=[case(0, 0, prefix="t")],
        development=[],
        variant_children=[child("var-1", "generated-t-0-0", "x")],
        selection_uses=[],
        answer_key_digest=tb.key_digest(odd),
    )
    el = set(proto2.eligible)
    a = arm(proto2, REAL_ONLY, set(), real_row_ids=ids)
    b = arm(proto2, AUGMENTED, el, real_row_ids=ids)
    res = run(proto2, odd, a, b)
    assert res["body"]["status"] == "invalidated"
    assert any("protected_case_in_fit_rows" in r for r in res["body"]["reasons"])


def test_case_records_carry_no_label_field() -> None:
    assert not {f.name for f in dataclasses.fields(CaseRef)} & {"label", "answer", "expected"}
    assert not {f.name for f in dataclasses.fields(ArmRecord)} & {"key", "answers"}


@pytest.mark.parametrize("missing", [REAL_ONLY, AUGMENTED])
def test_missing_arm_holds_and_retains_real_only_route(missing: str) -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    arms = {REAL_ONLY: arm(proto, REAL_ONLY, set()), AUGMENTED: arm(proto, AUGMENTED, el)}
    arms.pop(missing)
    res = run(proto, key, arms.get(REAL_ONLY), arms.get(AUGMENTED))
    body = res["body"]
    assert body["verdict"] == "unrun" and body["status"] == "held"
    assert body["claim"] == tb.UNPROVEN and not body["promotion_allowed"]
    assert body["retained_route"] == (REAL_ONLY if missing == AUGMENTED else None)


@pytest.mark.parametrize("status", ["interrupted", "revoked"])
def test_interrupted_or_revoked_arm_is_unrun_not_a_win(status: str) -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    res = run(proto, key, arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el, status=status))
    assert res["body"]["verdict"] == "unrun" and res["body"]["retained_route"] == REAL_ONLY


def test_declared_reference_arm_missing_holds() -> None:
    p = prereg(reference_arms=("incumbent",))
    proto, key, _ = frozen(p=p)
    el = set(proto.eligible)
    res = run(proto, key, arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el))
    assert res["body"]["verdict"] == "unrun"
    assert "arm_missing:incumbent" in res["body"]["reasons"]


def test_changed_denominator_between_arms_blocks_any_claim() -> None:
    proto, key, _ = frozen()
    el = sorted(proto.eligible)
    b = arm(
        proto,
        AUGMENTED,
        set(el),
        declared_case_ids=tuple(el[:-10]),
        predictions={c: "act-a" for c in el[:-10]},
    )
    res = run(proto, key, arm(proto, REAL_ONLY, set()), b)
    assert res["body"]["verdict"] == "inconclusive" and res["body"]["improvement_claim_blocked"]
    assert "changed_denominator:real_plus_synthetic" in res["body"]["reasons"]


def test_prediction_outside_frozen_set_changes_denominator() -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    b = arm(proto, AUGMENTED, el)
    b = dataclasses.replace(b, predictions={**b.predictions, "generated-extra": "act-a"})
    res = run(proto, key, arm(proto, REAL_ONLY, set()), b)
    assert res["body"]["verdict"] == "inconclusive"


def test_synthetic_or_starter_origin_scores_are_refused() -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    b = arm(proto, AUGMENTED, el, scored_origin="synthetic_stress")
    res = run(proto, key, arm(proto, REAL_ONLY, set()), b)
    assert "non_real_origin_scores:real_plus_synthetic" in res["body"]["reasons"]
    assert res["body"]["verdict"] != "measured_benefit"


@pytest.mark.parametrize(
    "field,value",
    [
        ("seed", 8),
        ("steps", 50),
        ("model_ref", "simulated:other"),
        ("recipe_id", "other"),
        ("base_revision", "generated-rev-2"),
    ],
)
def test_noncomparable_fit_conditions_are_unproven(field: str, value: Any) -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    res = run(proto, key, arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el, **{field: value}))
    assert res["body"]["verdict"] == "inconclusive" and res["body"]["claim"] == tb.UNPROVEN
    assert "noncomparable_arms" in res["body"]["reasons"]


def test_different_real_rows_or_unadmitted_child_is_noncomparable() -> None:
    proto, key, _ = frozen(
        children=[
            child("var-1", "generated-t-0-0", "f"),
            child("var-2", "generated-t-1-0", "f", "quarantined"),
        ]
    )
    el = set(proto.eligible)
    a = arm(proto, REAL_ONLY, set())
    rows = arm(proto, AUGMENTED, el, real_row_ids=("generated-t-0-0",))
    assert "real_rows_not_identical" in run(proto, key, a, rows)["body"]["reasons"]
    bad = arm(proto, AUGMENTED, el, synthetic_child_ids=("var-1", "var-2"))
    assert "synthetic_child_not_admitted" in run(proto, key, a, bad)["body"]["reasons"]
    assert proto.body["variants"]["child_counts_by_disposition"] == {
        "admitted": 1,
        "quarantined": 1,
    }


def test_zero_untouched_cases_is_insufficient_evidence_not_zero_benefit() -> None:
    cs = candidates()
    proto, key, _ = frozen(cs, training=cs[:], dev=[])
    assert proto.eligible == ()
    a = arm(proto, REAL_ONLY, set())
    b = arm(proto, AUGMENTED, set())
    res = run(proto, key, a, b)
    body = res["body"]
    assert body["verdict"] == "inconclusive" and body["claim"] == tb.UNPROVEN
    assert "insufficient_independent_real_cases" in body["reasons"]


def test_too_few_independent_groups_even_with_many_cases() -> None:
    cs = [case(i % 3, j, prefix="u") for i in range(30) for j in range(1)]
    cs = [
        dataclasses.replace(c, case_id=f"generated-u-{n}", text=f"generated n{n} zz{n}")
        for n, c in enumerate(cs)
    ]
    proto, key, _ = frozen(cs)
    assert proto.body["untouched"]["independent_group_count"] == 3
    el = set(proto.eligible)
    res = run(proto, key, arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el))
    assert "insufficient_independent_real_cases" in res["body"]["reasons"]


def test_arm_edited_after_seal_is_refused() -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    a, b = arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el)
    seal = tb.seal_arms(proto, [a, b])
    tampered = dataclasses.replace(b, predictions={c: "act-a" for c in proto.eligible} | {})
    tampered = dataclasses.replace(tampered, fit_cost=0)
    with pytest.raises(TuningBenefitError) as e:
        tb.measure(proto, seal, [a, tampered], key)
    assert e.value.code == "arm_changed_after_seal"


def test_wrong_protected_key_is_refused() -> None:
    proto, key, _ = frozen()
    el = set(proto.eligible)
    a, b = arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el)
    other = ProtectedKey({**key.answers, sorted(el)[0]: "act-other"})
    with pytest.raises(TuningBenefitError) as e:
        tb.measure(proto, tb.seal_arms(proto, [a, b]), [a, b], other)
    assert e.value.code == "answer_key_not_the_frozen_key"


def test_seal_for_another_protocol_is_refused() -> None:
    proto, key, _ = frozen()
    other, _, _ = frozen(p=prereg(seed=9))
    a, b = arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, set(proto.eligible))
    with pytest.raises(TuningBenefitError) as e:
        tb.measure(proto, tb.seal_arms(other, [a, b]), [a, b], key)
    assert e.value.code == "seal_not_for_protocol"


@pytest.mark.parametrize(
    "over",
    [
        dict(uncertainty_rule="bootstrap"),
        dict(alpha=0.0),
        dict(practical_threshold=0.0),
        dict(min_independent_families=1),
        dict(protocol_id=""),
        dict(fit_steps=0),
    ],
)
def test_incomplete_preregistration_is_refused(over: dict[str, Any]) -> None:
    with pytest.raises(TuningBenefitError):
        frozen(p=prereg(**over))


def test_non_synthetic_child_record_is_refused() -> None:
    bad = child("var-1", "generated-t-0-0", "f")
    bad["origin"] = "real"
    with pytest.raises(TuningBenefitError) as e:
        frozen(children=[bad])
    assert e.value.code == "child_origin_not_synthetic"


def test_unknown_selection_stage_refused() -> None:
    with pytest.raises(TuningBenefitError):
        frozen(uses=[SelectionUse("peeking")])


# ------------------------------------------------ plan, spend, retry/replay


def test_one_fit_grant_authorizes_one_fit_and_leaves_comparison_unrun() -> None:
    plan = tb.plan_arms(prereg(fits_authorized=1), simple_fix_succeeded=False)
    assert plan == {"fits": [REAL_ONLY], "comparison": "unrun", "reason": "one_fit_grant"}


def test_successful_simple_fix_spends_zero_fits() -> None:
    plan = tb.plan_arms(prereg(), simple_fix_succeeded=True)
    assert plan["fits"] == [] and plan["comparison"] == "unrun"


def test_two_fit_grant_and_prior_arm_reuse() -> None:
    assert tb.plan_arms(prereg(), simple_fix_succeeded=False)["fits"] == [REAL_ONLY, AUGMENTED]
    reuse = tb.plan_arms(
        prereg(fits_authorized=1), simple_fix_succeeded=False, matching_prior_arm=True
    )
    assert reuse["fits"] == [AUGMENTED]


def test_one_fit_ledger_cannot_silently_become_two() -> None:
    led = SpendLedger(spend_cap=100, fits_authorized=1)
    assert led.reserve("d", REAL_ONLY, "fit", 10) == "reserved"
    with pytest.raises(TuningBenefitError) as e:
        led.reserve("d", AUGMENTED, "fit", 10)
    assert e.value.code == "fit_grant_exceeded"
    led.settle("d", REAL_ONLY, "fit", 10)
    assert led.charged == 10  # one reservation, one actual charge


def test_duplicate_and_restarted_two_arm_run_never_double_charges() -> None:
    led = SpendLedger(spend_cap=100, fits_authorized=2)
    for _ in range(2):  # duplicate delivery
        led.reserve("d", REAL_ONLY, "fit", 10)
        led.reserve("d", AUGMENTED, "fit", 10)
        led.reserve("d", AUGMENTED, "generation", 3)  # separate cost, own key
    led.settle("d", REAL_ONLY, "fit", 9)
    restarted = SpendLedger.from_json(led.to_json())  # restart/replay from durable state
    assert restarted.reserve("d", REAL_ONLY, "fit", 10) == "replayed"
    assert restarted.settle("d", REAL_ONLY, "fit", 9) == "replayed"
    restarted.settle("d", AUGMENTED, "fit", 10)
    restarted.settle("d", AUGMENTED, "generation", 3)
    assert restarted.charged == 22
    with pytest.raises(TuningBenefitError) as e:
        restarted.reserve("d", REAL_ONLY, "fit", 11)
    assert e.value.code == "reservation_conflict"
    with pytest.raises(TuningBenefitError) as e2:
        restarted.settle("d", REAL_ONLY, "fit", 8)
    assert e2.value.code == "settlement_conflict"


def test_spend_cap_and_unreserved_settlement_refused() -> None:
    led = SpendLedger(spend_cap=12, fits_authorized=2)
    led.reserve("d", REAL_ONLY, "fit", 10)
    with pytest.raises(TuningBenefitError) as e:
        led.reserve("d", AUGMENTED, "fit", 10)
    assert e.value.code == "spend_cap_exceeded"
    with pytest.raises(TuningBenefitError) as e2:
        led.settle("d", "ghost", "fit", 1)
    assert e2.value.code == "settle_without_reservation"


# ------------------------------------------------ label efficiency


def cp(
    arm_id: str,
    labels: int,
    met: bool | None,
    *,
    confirmed: bool = True,
    expert: int | None = 100,
    review: int | None = 0,
) -> Checkpoint:
    return Checkpoint(arm_id, labels, labels // 2, met, confirmed, expert, review)


def test_label_efficiency_reports_percent_only_at_tested_budgets() -> None:
    res = tb.label_efficiency(
        [
            cp(REAL_ONLY, 40, False),
            cp(AUGMENTED, 40, True, expert=40, review=20),
            cp(REAL_ONLY, 100, True, expert=100),
            cp(AUGMENTED, 100, True),
        ]
    )
    assert res["reduction_bps"] == 6000 and "60% fewer" in res["claim"]
    assert res["labor_delta_minutes"] == -40
    assert res["launches_job"] is False


def test_label_efficiency_accepts_negative_labor_savings() -> None:
    res = tb.label_efficiency(
        [
            cp(REAL_ONLY, 40, False),
            cp(AUGMENTED, 40, True, expert=40, review=200),
            cp(REAL_ONLY, 100, True, expert=100),
            cp(AUGMENTED, 100, True),
        ]
    )
    assert res["reduction_bps"] == 6000 and res["labor_delta_minutes"] == 140


def test_single_or_unconfirmed_checkpoint_is_label_savings_not_measured() -> None:
    one = tb.label_efficiency([cp(REAL_ONLY, 100, True), cp(AUGMENTED, 100, True)])
    assert one["claim"] == tb.NOT_MEASURED
    pending = tb.label_efficiency(
        [
            cp(REAL_ONLY, 40, False, confirmed=False),
            cp(AUGMENTED, 40, True, confirmed=False),
            cp(REAL_ONLY, 100, True),
            cp(AUGMENTED, 100, True),
        ]
    )
    assert pending["claim"] == tb.NOT_MEASURED
    assert tb.label_efficiency([])["claim"] == tb.NOT_MEASURED


def test_real_only_never_reaching_target_reports_tested_bound_not_a_percent() -> None:
    res = tb.label_efficiency(
        [
            cp(REAL_ONLY, 40, False),
            cp(AUGMENTED, 40, True),
            cp(REAL_ONLY, 100, False),
            cp(AUGMENTED, 100, True),
        ]
    )
    assert res["reduction_bps"] is None and res["tested_bound"] == 100


def test_label_efficiency_rejects_unknown_arm() -> None:
    with pytest.raises(TuningBenefitError):
        tb.label_efficiency([cp("mystery", 10, True)])


def test_fingerprint_is_accent_case_punctuation_order_insensitive() -> None:
    assert tb.fingerprint("Café, au LAIT!") == tb.fingerprint("lait au cafe")
    assert tb.fingerprint("a b") != tb.fingerprint("a b c")


def test_one_real_root_under_two_families_is_refused() -> None:
    r1 = case(0, 0, prefix="t")
    r2 = dataclasses.replace(r1, family_id="generated-fam-other")
    with pytest.raises(TuningBenefitError) as e:
        frozen(training=[r1, r2])
    assert e.value.code == "root_identity_conflict"


# ---- closer additions: propagation of case-level touches, family leaks, ledger forgery ----


def _small_freeze(
    cands: list[CaseRef],
    *,
    uses: list[SelectionUse] | None = None,
    children: list[dict[str, Any]] | None = None,
    training: list[CaseRef] | None = None,
) -> tb.FrozenProtocol:
    return tb.freeze_protocol(
        prereg(),
        candidates=cands,
        real_training_roots=training or [],
        development=[],
        variant_children=children or [],
        selection_uses=uses or [],
        answer_key_digest="sha256:generated-key",
    )


_X = CaseRef("generated-x", "generated-fam-f", "generated alpha beta")
_SIB = CaseRef("generated-sib", "generated-fam-f", "generated gamma delta")
_DUP = CaseRef("generated-dup", "generated-fam-g", "Generated  ALPHA, beta!")
_OTHER = CaseRef("generated-other", "generated-fam-h", "generated unrelated text")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"uses": [SelectionUse("threshold_selection", case_ids=("generated-x",))]},
        {"uses": [SelectionUse("model_choice", family_ids=("generated-fam-f",))]},
        {
            "children": [
                {
                    "child_id": "generated-c1",
                    "origin": "synthetic",
                    "mode": "training",
                    "disposition": "rejected",
                    "root_ref": "generated-x",
                    "rendered_text": "generated unrelated child",
                }
            ]
        },
        {"training": [CaseRef("generated-t", "generated-fam-f", "generated tt")]},
    ],
)
def test_case_level_touch_also_excludes_family_siblings_and_duplicate_content(
    kwargs: dict[str, Any],
) -> None:
    proto = _small_freeze([_X, _SIB, _DUP, _OTHER], **kwargs)
    assert proto.eligible == ("generated-other",)
    excluded = {k for k, _ in proto.body["untouched"]["excluded"]}
    assert "generated-sib" in excluded and "generated-dup" in excluded


def test_post_freeze_family_only_use_invalidates_and_reports_no_numbers() -> None:
    proto, key, _ = frozen()
    fam = proto.body["untouched"]["family_of"][proto.eligible[0]]
    el = set(proto.eligible)
    res = run(
        proto,
        key,
        arm(proto, REAL_ONLY, set()),
        arm(proto, AUGMENTED, el),
        post_freeze_uses=[SelectionUse("threshold_selection", family_ids=(fam,))],
    )
    assert res["body"]["status"] == "invalidated" and res["body"]["paired"] is None
    assert any("untouched_family_used" in r for r in res["body"]["reasons"])


def test_key_unresolved_eligible_cases_stay_visible_outside_the_denominator() -> None:
    cs = candidates()
    answers: dict[str, str | None] = {c.case_id: "act-a" for c in cs}
    answers[cs[0].case_id] = None
    key = ProtectedKey(answers)
    proto = tb.freeze_protocol(
        prereg(),
        candidates=cs,
        real_training_roots=[case(i, 0, prefix="t") for i in range(5)],
        development=[case(i, 0, prefix="d") for i in range(3)],
        variant_children=[child("var-1", "generated-t-0-0", "generated-fam-t-0")],
        selection_uses=[],
        answer_key_digest=tb.key_digest(key),
    )
    el = set(proto.eligible)
    res = run(proto, key, arm(proto, REAL_ONLY, set()), arm(proto, AUGMENTED, el))
    paired = res["body"]["paired"]
    assert paired["unresolved_by_key_outside_denominator"] == [cs[0].case_id]
    assert paired["scorable_cases"] == len(el) - 1


def test_ledger_refuses_negative_settlement_and_forged_json() -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    ledger.reserve("d", "a", "fit", 10)
    with pytest.raises(TuningBenefitError) as neg:
        ledger.settle("d", "a", "fit", -50)
    assert neg.value.code == "settlement_amount_invalid" and ledger.charged == 0
    for mutate in (
        lambda r: r["reserved"].__setitem__("d|b|fit", ["fit", -200]),
        lambda r: r["reserved"].__setitem__("d|b|fit", ["fit", 1]),  # second fit, one granted
        lambda r: r["reserved"].__setitem__("d|b|generation", ["generation", 999]),
        lambda r: r["settled"].__setitem__("d|a|fit", 11),
        lambda r: r["settled"].__setitem__("d|ghost|fit", 1),
    ):
        raw = ledger.to_json()
        mutate(raw)
        with pytest.raises(TuningBenefitError) as err:
            SpendLedger.from_json(raw)
        assert err.value.code.startswith("ledger_forged")
    assert SpendLedger.from_json(ledger.to_json()).to_json() == ledger.to_json()


def _forged_refusal(raw: dict[str, Any]) -> str:
    with pytest.raises(TuningBenefitError) as err:
        SpendLedger.from_json(raw)
    return err.value.code


def test_ledger_refuses_fit_keys_that_store_a_different_kind() -> None:
    # Reviewer reproduction: two fit-keyed reservations stored as generation would
    # slip past a fit count taken from stored kinds, bypassing a one-fit grant.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    raw = ledger.to_json()
    raw["reserved"]["d|a|fit"] = ["generation", 10]
    raw["reserved"]["d|b|fit"] = ["generation", 10]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


def test_ledger_refuses_non_fit_key_that_stores_fit() -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    raw = ledger.to_json()
    raw["reserved"]["d|a|generation"] = ["fit", 10]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


@pytest.mark.parametrize(
    "key", ["garbage", "d|fit", "d|a|b|fit", "|fit", "d||fit", "d|a|", "", "||"]
)
def test_ledger_refuses_malformed_reservation_keys(key: str) -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    raw = ledger.to_json()
    raw["reserved"][key] = ["generation", 1]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


def test_ledger_round_trip_accepts_honest_state_at_grant_and_cap() -> None:
    ledger = SpendLedger(spend_cap=30, fits_authorized=2)
    assert ledger.reserve("d", "a", "fit", 10) == "reserved"
    assert ledger.reserve("d", "b", "fit", 10) == "reserved"
    assert ledger.reserve("d", "a", "generation", 10) == "reserved"
    assert ledger.settle("d", "a", "fit", 10) == "settled"
    raw = ledger.to_json()
    restored = SpendLedger.from_json(raw)
    assert restored.to_json() == raw and restored.charged == 10
    # Exactly at the fit grant and exactly at the cap: nothing further is admitted.
    with pytest.raises(TuningBenefitError) as grant:
        restored.reserve("d", "c", "fit", 1)
    assert grant.value.code == "fit_grant_exceeded"
    with pytest.raises(TuningBenefitError) as cap:
        restored.reserve("d", "c", "generation", 1)
    assert cap.value.code == "spend_cap_exceeded"


# ---- post-merge audit follow-up (2026-11-28): ledger key structure ----


@pytest.mark.parametrize(
    ("key", "kind"),
    [("||generation", "generation"), ("d||generation", "generation"), ("|a|fit", "fit")],
)
def test_ledger_refuses_keys_with_empty_digest_or_arm(key: str, kind: str) -> None:
    # The third part matches the stored kind, so only the empty-part check can refuse these.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    raw = ledger.to_json()
    raw["reserved"][key] = [kind, 1]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


@pytest.mark.parametrize(
    ("key", "kind"),
    [
        ("d|a|fit|x", "fit"),
        ("d|a|generation|generation", "generation"),
        ("d|a|generation|", "generation"),
        ("d|a|generation|b|z", "generation"),
    ],
)
def test_ledger_refuses_keys_with_extra_parts_even_when_the_third_is_the_kind(
    key: str, kind: str
) -> None:
    # Pins the ``len(parts) == 3`` check: every key here has a well-formed third part.
    assert key.split("|")[2] == kind
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    raw = ledger.to_json()
    raw["reserved"][key] = [kind, 1]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


@pytest.mark.parametrize(
    ("digest", "arm"),
    [
        ("d", "a|b"),
        ("d|a", "b"),
        ("d", "a|"),
        ("|d", "a"),
        ("", "a"),
        ("d", ""),
        (None, "a"),
        ("d", 7),
    ],
)
def test_reserve_and_settle_refuse_separator_empty_or_non_str_parts(
    digest: object, arm: object
) -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=2)
    for call in (ledger.reserve, ledger.settle):
        with pytest.raises(TuningBenefitError) as err:
            call(digest, arm, "generation", 1)  # type: ignore[arg-type]
        assert err.value.code == "reservation_key_invalid"
    assert ledger.to_json()["reserved"] == {} and ledger.charged == 0


def test_separator_in_an_identifier_cannot_collide_two_pairs() -> None:
    # ("d|a", "b") and ("d", "a|b") would both key as "d|a|b|..." and read as "replayed".
    ledger = SpendLedger(spend_cap=100, fits_authorized=2)
    assert ledger.reserve("d", "a", "generation", 5) == "reserved"
    with pytest.raises(TuningBenefitError) as first:
        ledger.reserve("d|a", "b", "generation", 5)
    with pytest.raises(TuningBenefitError) as second:
        ledger.reserve("d", "a|b", "generation", 5)
    assert first.value.code == second.value.code == "reservation_key_invalid"
    restored = SpendLedger.from_json(ledger.to_json())  # what reserve admits always round-trips
    assert restored.to_json() == ledger.to_json()


# ---- cross-session review follow-up (2026-11-28): exact-str kind and key parts ----


class _FormatLiesKind(str):
    """Equal to ``"generation"`` but formats as ``"generation|zz"``."""

    def __format__(self, spec: str) -> str:
        return "generation|zz"


class _FirstCompareFit:
    """Not a str: equal to ``"fit"`` only on its first comparison, then never again."""

    def __init__(self) -> None:
        self.compared = 0

    def __hash__(self) -> int:
        return hash("fit")

    def __eq__(self, other: object) -> bool:
        self.compared += 1
        return self.compared == 1 and other == "fit"

    def __str__(self) -> str:
        return "fit"

    def __format__(self, spec: str) -> str:
        return "fit"


class _ContainsLies(str):
    """A str subclass that hides the separator from ``in``."""

    def __contains__(self, item: object) -> bool:
        return False


class _SplitLies(str):
    """A str subclass whose ``split`` reports a well-formed three-part key."""

    def split(self, sep: Any = None, maxsplit: int = -1) -> list[str]:  # type: ignore[override]
        return ["d", "a", "fit"]

    def __hash__(self) -> int:
        return str.__hash__(self)


@pytest.mark.parametrize(
    "kind",
    [_FormatLiesKind("generation"), _FormatLiesKind("fit"), "Fit", "fit ", None, 1, b"fit"],
    ids=["format-lies-generation", "subclass-fit", "case", "padded", "none", "int", "bytes"],
)
def test_reserve_and_settle_refuse_a_kind_that_is_not_an_exact_known_str(kind: object) -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    assert ledger.reserve("d", "a", "generation", 1) == "reserved"
    before = ledger.to_json()
    for call in (ledger.reserve, ledger.settle):
        with pytest.raises(TuningBenefitError) as err:
            call("d", "a", kind, 1)  # type: ignore[arg-type]
        assert err.value.code == "reservation_kind_unknown"
    assert ledger.to_json() == before and ledger.charged == 0
    assert SpendLedger.from_json(ledger.to_json()).to_json() == before


def test_format_lying_kind_cannot_reserve_an_unloadable_key() -> None:
    # Reviewer vector: the subclass reserved "d|a|generation|zz", which reload then refused.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    with pytest.raises(TuningBenefitError) as err:
        ledger.reserve("d", "a", _FormatLiesKind("generation"), 1)
    assert err.value.code == "reservation_kind_unknown"
    assert ledger.to_json()["reserved"] == {}


def test_stateful_kind_cannot_take_fits_past_the_grant() -> None:
    # Reviewer vector: a kind equal to "fit" on its first comparison only took three fits past
    # fits_authorized=1 in memory, and reload then failed with ledger_forged_fit_count.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    for arm in ("a", "b", "c"):
        with pytest.raises(TuningBenefitError) as err:
            ledger.reserve("d", arm, _FirstCompareFit(), 1)  # type: ignore[arg-type]
        assert err.value.code == "reservation_kind_unknown"
    assert ledger.to_json()["reserved"] == {}
    assert ledger.reserve("d", "a", "fit", 1) == "reserved"
    with pytest.raises(TuningBenefitError) as grant:
        ledger.reserve("d", "b", "fit", 1)
    assert grant.value.code == "fit_grant_exceeded"


@pytest.mark.parametrize(
    "stored_kind",
    [_FormatLiesKind("fit"), _FirstCompareFit(), "fit "],
    ids=["str-subclass", "stateful", "padded"],
)
def test_ledger_reload_refuses_a_stored_kind_that_is_not_an_exact_str(stored_kind: object) -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    raw = ledger.to_json()
    raw["reserved"]["d|a|fit"] = [stored_kind, 1]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


def test_separator_hidden_by_a_str_subclass_cannot_collide_two_pairs() -> None:
    # Pins ``type(part) is str`` in the key builder (mutant K1: isinstance). Under isinstance,
    # these two pairs both key as "d|a|b|generation": reserved, then "replayed".
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    with pytest.raises(TuningBenefitError) as first:
        ledger.reserve(_ContainsLies("d|a"), "b", "generation", 5)
    with pytest.raises(TuningBenefitError) as second:
        ledger.reserve("d", _ContainsLies("a|b"), "generation", 5)
    with pytest.raises(TuningBenefitError) as third:
        ledger.settle(_ContainsLies("d|a"), "b", "generation", 5)
    assert first.value.code == second.value.code == third.value.code == "reservation_key_invalid"
    assert ledger.to_json()["reserved"] == {}


def test_ledger_reload_refuses_a_str_subclass_key() -> None:
    # Pins ``type(k) is str`` in from_json (mutant K2: isinstance). The subclass claims a
    # well-formed split, but its real value has four parts.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    raw = ledger.to_json()
    raw["reserved"][_SplitLies("d|a|b|fit")] = ["fit", 1]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


def test_ledger_reload_refuses_a_str_subclass_settlement_key() -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    ledger.reserve("d", "a", "fit", 5)
    raw = ledger.to_json()
    raw["settled"][_SplitLies("d|a|fit")] = 1
    assert _forged_refusal(raw) == "ledger_forged_settlement"


_BAD_PARTS = [
    "d ",
    " d",
    "   ",
    "\t",
    "d\x00",
    "\x00",
    "d\u200b",
    "\u200b",
    "\ufeffd",
    "d\u2028a",
    "d\ud800",  # lone surrogate (Cs): not UTF-8 encodable
    "\udfff",
    "e\u0301",  # NFD of U+00E9
    "x" * 257,
]
_BAD_PART_IDS = [
    "trailing-space",
    "leading-space",
    "whitespace-only",
    "tab-only",
    "nul",
    "nul-only",
    "zwsp",
    "zwsp-only",
    "bom",
    "line-separator",
    "lone-surrogate",
    "lone-surrogate-only",
    "nfd",
    "too-long",
]


@pytest.mark.parametrize("bad", _BAD_PARTS, ids=_BAD_PART_IDS)
def test_reserve_and_settle_refuse_ill_formed_digest_or_arm(bad: str) -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    for digest_, arm in ((bad, "a"), ("d", bad)):
        for call in (ledger.reserve, ledger.settle):
            with pytest.raises(TuningBenefitError) as err:
                call(digest_, arm, "generation", 1)
            assert err.value.code == "reservation_key_invalid"
    assert ledger.to_json()["reserved"] == {} and ledger.charged == 0


@pytest.mark.parametrize("bad", _BAD_PARTS, ids=_BAD_PART_IDS)
def test_ledger_reload_refuses_ill_formed_digest_or_arm(bad: str) -> None:
    for key in (f"{bad}|a|generation", f"d|{bad}|generation"):
        raw = SpendLedger(spend_cap=100, fits_authorized=1).to_json()
        raw["reserved"][key] = ["generation", 1]
        assert _forged_refusal(raw) == "ledger_forged_reservation"


def test_visually_identical_parts_are_not_charged_twice() -> None:
    # Reviewer vector: "d" vs "d " and NFC vs NFD were two keys and two charges.
    ledger = SpendLedger(spend_cap=10, fits_authorized=1)
    assert ledger.reserve("d", "\u00e9", "generation", 6) == "reserved"
    for digest_, arm in (("d ", "\u00e9"), ("d", "e\u0301"), ("d\u200b", "\u00e9")):
        with pytest.raises(TuningBenefitError) as err:
            ledger.reserve(digest_, arm, "generation", 6)
        assert err.value.code == "reservation_key_invalid"
    assert sum(v for _, v in ledger.to_json()["reserved"].values()) == 6


def test_well_formed_edge_parts_are_admitted_and_round_trip() -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    longest = "x" * 256
    assert ledger.reserve(longest, longest, "fit", 1) == "reserved"
    assert ledger.reserve("sha256:" + "0" * 64, "real only", "generation", 1) == "reserved"
    assert ledger.reserve("d", "\u00e9", "generation", 1) == "reserved"  # NFC is admitted
    assert ledger.settle(longest, longest, "fit", 1) == "settled"
    raw = ledger.to_json()
    restored = SpendLedger.from_json(raw)
    assert restored.to_json() == raw
    assert all(type(k) is str for k in raw["reserved"])
    assert all(type(v[0]) is str for v in raw["reserved"].values())


def test_ledger_reload_refuses_a_list_subclass_reservation_value() -> None:
    class _Row(list):  # type: ignore[type-arg]
        pass

    raw = SpendLedger(spend_cap=100, fits_authorized=1).to_json()
    raw["reserved"]["d|a|fit"] = _Row(["fit", 1])
    assert _forged_refusal(raw) == "ledger_forged_reservation"


@pytest.mark.parametrize("surrogate", ["d\ud800", "\udfff", "\ud800d"])
def test_lone_surrogate_part_is_refused_before_any_state_change(surrogate: str) -> None:
    # Re-review vector (Cs): without the category rule, reserve("d\ud800", ...) was admitted and
    # the ledger then could not be written as UTF-8 (UnicodeEncodeError).
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    assert ledger.reserve("d", "a", "generation", 1) == "reserved"
    before = ledger.to_json()
    for digest_, arm in ((surrogate, "a"), ("d", surrogate)):
        for call in (ledger.reserve, ledger.settle):
            with pytest.raises(TuningBenefitError) as err:
                call(digest_, arm, "generation", 1)
            assert err.value.code == "reservation_key_invalid"
    assert ledger.to_json() == before and ledger.charged == 0
    json.dumps(ledger.to_json(), ensure_ascii=False).encode("utf-8")  # still writable


@pytest.mark.parametrize("entry", ["reserve", "settle"])
def test_kind_is_checked_before_the_amount(entry: str) -> None:
    # Pins the order: a bad kind with a bad amount reports the kind, never the amount.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    with pytest.raises(TuningBenefitError) as kind_first:
        getattr(ledger, entry)("d", "a", "bogus", -1)
    assert kind_first.value.code == "reservation_kind_unknown"
    with pytest.raises(TuningBenefitError) as key_first:
        getattr(ledger, entry)("d|x", "a", "generation", -1)
    assert key_first.value.code == "reservation_key_invalid"
    assert ledger.to_json()["reserved"] == {}


@pytest.mark.parametrize("field", ["spend_cap", "fits_authorized", "reserved", "settled"])
def test_ledger_reload_refuses_a_missing_top_level_field(field: str) -> None:
    raw = SpendLedger(spend_cap=100, fits_authorized=1).to_json()
    del raw[field]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


# from_json on a value of the wrong JSON shape must refuse with a typed code, never a bare
# TypeError/AttributeError/KeyError. The check is ``type(x) is dict``, so a dict subclass that
# answers ``items`` differently from what it holds never reaches the per-entry checks.


class _LyingItems(dict):  # type: ignore[type-arg]
    """Empty (or anything) underneath; ``items`` yields an honest-looking entry."""

    def __init__(self, *args: Any, fake: tuple[str, Any] = ("d|a|fit", ["fit", 1])) -> None:
        super().__init__(*args)
        self._fake = fake

    def items(self) -> Any:
        return iter([self._fake])


@pytest.mark.parametrize(
    "raw",
    [
        None,
        3,
        1.5,
        True,
        "spend_cap fits_authorized reserved settled",
        b"{}",
        [],
        ["spend_cap", "fits_authorized", "reserved", "settled"],
        ("spend_cap", "fits_authorized", "reserved", "settled"),
        {"spend_cap", "fits_authorized", "reserved", "settled"},
    ],
    ids=[
        "null",
        "int",
        "float",
        "bool",
        "str",
        "bytes",
        "list",
        "list-of-fields",
        "tuple-of-fields",
        "set-of-fields",
    ],
)
def test_ledger_reload_refuses_a_top_level_value_that_is_not_an_object(raw: object) -> None:
    with pytest.raises(TuningBenefitError) as err:
        SpendLedger.from_json(raw)  # type: ignore[arg-type]
    assert err.value.code == "ledger_forged_reservation"


def test_ledger_reload_refuses_a_top_level_dict_subclass() -> None:
    # Pins the exact type on the top level: the content is honest, only the type is wrong.
    raw = _LyingItems(SpendLedger(spend_cap=100, fits_authorized=1).to_json())
    assert _forged_refusal(raw) == "ledger_forged_reservation"


@pytest.mark.parametrize(
    "field, value, code",
    [
        ("reserved", [], "ledger_forged_reservation"),
        ("reserved", [["d|a|fit", ["fit", 1]]], "ledger_forged_reservation"),
        ("reserved", None, "ledger_forged_reservation"),
        ("reserved", "d|a|fit", "ledger_forged_reservation"),
        ("reserved", 0, "ledger_forged_reservation"),
        ("settled", [], "ledger_forged_settlement"),
        ("settled", [["d|a|fit", 1]], "ledger_forged_settlement"),
        ("settled", None, "ledger_forged_settlement"),
        ("settled", "d|a|fit", "ledger_forged_settlement"),
        ("settled", 0, "ledger_forged_settlement"),
    ],
    ids=[
        "reserved-empty-list",
        "reserved-pairs-list",
        "reserved-null",
        "reserved-str",
        "reserved-int",
        "settled-empty-list",
        "settled-pairs-list",
        "settled-null",
        "settled-str",
        "settled-int",
    ],
)
def test_ledger_reload_refuses_reserved_or_settled_that_is_not_an_object(
    field: str, value: object, code: str
) -> None:
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    ledger.reserve("d", "a", "fit", 5)
    raw = ledger.to_json()
    raw[field] = value
    assert _forged_refusal(raw) == code


def test_ledger_reload_refuses_reserved_or_settled_dict_subclasses() -> None:
    # Pins the exact type on each table. Under isinstance, each would load the honest-looking
    # entry its ``items`` yields rather than what it holds.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    ledger.reserve("d", "a", "fit", 5)
    raw = ledger.to_json()
    raw["reserved"] = _LyingItems()
    assert _forged_refusal(raw) == "ledger_forged_reservation"
    raw = ledger.to_json()
    raw["settled"] = _LyingItems(fake=("d|a|fit", 1))
    assert _forged_refusal(raw) == "ledger_forged_settlement"


@pytest.mark.parametrize(
    "stored_kind", [["fit"], {"fit": 1}, [], {}], ids=["list", "dict", "empty-list", "empty-dict"]
)
def test_ledger_reload_refuses_a_list_or_dict_stored_kind_with_a_code(stored_kind: object) -> None:
    # Audit vector: a list or dict kind gives a coded refusal, not an unhashable TypeError.
    raw = SpendLedger(spend_cap=100, fits_authorized=1).to_json()
    raw["reserved"]["d|a|fit"] = [stored_kind, 1]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


@pytest.mark.parametrize(
    "row",
    [{"fit": 1}, {"kind": "fit", "amount": 1}, "fi", None, 1],
    ids=["dict", "dict-named", "two-char-str", "null", "int"],
)
def test_ledger_reload_refuses_a_reservation_row_of_the_wrong_shape(row: object) -> None:
    raw = SpendLedger(spend_cap=100, fits_authorized=1).to_json()
    raw["reserved"]["d|a|fit"] = row
    assert _forged_refusal(raw) == "ledger_forged_reservation"


@pytest.mark.parametrize(
    "extra",
    [("note", "x"), ("fits_used", 0), ("", None), ("reserved ", {})],
    ids=["note", "fits-used", "empty-name", "padded-name"],
)
def test_ledger_reload_refuses_an_extra_top_level_field(extra: tuple[str, object]) -> None:
    # The persisted format is closed: an unknown field is refused, not silently dropped.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    ledger.reserve("d", "a", "fit", 5)
    raw: dict[str, Any] = ledger.to_json()
    raw[extra[0]] = extra[1]
    assert _forged_refusal(raw) == "ledger_forged_reservation"


def test_ledger_reload_after_a_json_round_trip_still_loads_honest_state() -> None:
    # The exact-dict checks must not refuse what json.loads produces from to_json.
    ledger = SpendLedger(spend_cap=100, fits_authorized=1)
    ledger.reserve("d", "a", "fit", 5)
    ledger.settle("d", "a", "fit", 4)
    raw = json.loads(json.dumps(ledger.to_json()))
    assert SpendLedger.from_json(raw).to_json() == ledger.to_json()
