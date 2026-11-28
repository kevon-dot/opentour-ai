"""Generated sentinel fixtures exercise the synthetic-example allocator and its budget ledger.

Acceptance IDs below (A*, E*, P*, B*, C*) are this suite's own: OPE-520 has no entry in the
approved spec projection, so they are not registered test bindings. Software behavior only; no
provider, customer or ROI evidence.
"""

from __future__ import annotations

import dataclasses
import inspect
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot import synthetic_allocation as sa
from opentour_cell.pilot import training_variants as tv
from opentour_cell.pilot.synthetic_allocation import (
    AllocationError,
    AllocationLedger,
    AllocationPolicy,
    FamilyInput,
    RootInput,
    allocate,
    covered_from_children,
    family_from_record,
)

CELL_A = ("generated-class-a", "generated-branch-1")
CELL_B = ("generated-class-b", "generated-branch-1")
R1, R2, R3 = "simulated:recipe-1", "simulated:recipe-2", "simulated:recipe-3"


def family(
    fid: str,
    cell: tuple[str, str] = CELL_A,
    recipes: tuple[str, ...] = (R1,),
    **kw: object,
) -> FamilyInput:
    base: dict[str, object] = {
        "split": "training",
        "verification": "verified",
        "verifier_independent": True,
        "roots": (
            RootInput(
                f"generated-root-{fid}",
                f"generated-digest-{fid}",
                recipes,
                0,
                f"generated-case-{fid}",
            ),
        ),
    }
    base.update(kw)
    return FamilyInput(family_id=fid, cell=cell, **base)  # type: ignore[arg-type]


def policy(**kw: object) -> AllocationPolicy:
    base: dict[str, object] = {
        "profile_revision": "generated-profile-1",
        "seed": "generated-seed",
        "total_units": 100,
        "spend_ceiling": 100,
    }
    base.update(kw)
    return AllocationPolicy(**base)  # type: ignore[arg-type]


def fleet() -> list[FamilyInput]:
    a = [family(f"a{i:02d}", CELL_A, (R1, R2)) for i in range(12)]
    b = [family(f"b{i:02d}", CELL_B, (R1,)) for i in range(8)]
    return a + b


def zero_by_reason(plan: sa.AllocationPlan) -> dict[str, tuple[str, int]]:
    return {d.family_id: (d.reason, d.units) for d in plan.decisions}


# A1: worked test. A = 12 x 2 = 24, B = 8 x 1 = 8; ranking prefers A.
def test_worked_example_scores_and_ranking() -> None:
    plan = allocate(fleet(), policy(per_family_cap=2, per_root_cap=2))
    assert dict(plan.cell_scores) == {CELL_A: "24/1", CELL_B: "8/1"}
    assert plan.ranking_basis == "count_based" and plan.roi_claim is False
    assert plan.status == "allocated" and plan.units == 32
    tight = allocate(
        fleet(),
        policy(total_units=5, spend_ceiling=5, per_family_cap=2, per_root_cap=2, batch_size=5),
    )
    assert tight.units == 5 and {x.cell for x in tight.lines} == {CELL_A}
    assert tight.stop_reasons == ("budget_exhausted",)


# A1b: one error with 100 duplicate copies is one independent family.
def test_hundred_duplicates_count_as_one_family() -> None:
    copies = [
        family(
            f"dup{i:03d}",
            CELL_A,
            (R1,),
            roots=(
                RootInput(
                    f"generated-root-dup{i}",
                    "generated-digest-same",
                    (R1,),
                    0,
                    f"generated-case-dup{i}",
                ),
            ),
        )
        for i in range(100)
    ]
    plan = allocate(copies, policy())
    assert plan.units == 1
    reasons = [d.reason for d in plan.decisions]
    assert reasons.count("eligible") == 1 and reasons.count("duplicate_family") == 99
    assert dict(plan.cell_scores) == {CELL_A: "1/1"}


# A2: identical inputs reproduce the identical allocation, regardless of input order.
def test_identical_inputs_reproduce_identical_allocation() -> None:
    p = policy(
        total_units=9, spend_ceiling=9, per_family_cap=2, per_root_cap=2, exploration_quota=2
    )
    first = allocate(fleet(), p)
    second = allocate(list(reversed(fleet())), p)
    assert first.as_record() == second.as_record()
    assert first.allocation_id == second.allocation_id
    other = allocate(fleet(), replace(p, seed="generated-seed-2"))
    assert other.allocation_id != first.allocation_id


# A3: seeded uniform control uses the same eligible roots, cap and budget.
def test_uniform_control_matches_roots_and_budget() -> None:
    p = policy(total_units=10, spend_ceiling=10, per_family_cap=2, per_root_cap=2)
    targeted = allocate(fleet(), p)
    control = allocate(fleet(), p, control=True, match_units=targeted.units)
    assert control.control and control.units == targeted.units == 10
    assert {x.phase for x in control.lines} == {"uniform_control"}
    assert {d.family_id for d in control.decisions if d.eligible} == {
        d.family_id for d in targeted.decisions if d.eligible
    }
    assert control.lines == allocate(fleet(), p, control=True, match_units=10).lines
    assert (
        len({x.family_id for x in control.lines})
        > len({x.family_id for x in targeted.lines if x.cell == CELL_A}) - 100
    )
    with pytest.raises(AllocationError, match="control_not_reservable"):
        led = AllocationLedger()
        led.open_budget("generated-budget", cap_units=10, cap_spend=10, unit_cost=1)
        led.reserve("generated-budget", control)


# E1: ineligible families receive zero.
@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"verification": "unverified"}, "unverified"),
        ({"verification": "disputed"}, "disputed"),
        ({"verification": "retracted"}, "retracted"),
        ({"verifier_independent": False}, "verifier_not_independent"),
        ({"protected_derived": True}, "protected_derived"),
        ({"split": "protected"}, "protected_split"),
        ({"split": "development"}, "not_training_split"),
        (
            {
                "roots": (
                    RootInput(
                        "var-generated-child", "generated-digest-x", (R1,), 0, "generated-case-v"
                    ),
                )
            },
            "child_derived",
        ),
        ({"roots": ()}, "no_roots"),
    ],
)
def test_ineligible_family_gets_zero(kw: dict[str, object], reason: str) -> None:
    plan = allocate([family("good"), family("bad", **kw)], policy())
    got = zero_by_reason(plan)
    assert got["bad"] == (reason, 0)
    assert got["good"] == ("eligible", 1)
    assert all(x.family_id != "bad" for x in plan.lines)


# E2: policy conflict is excluded regardless of score and queued for an owner answer.
def test_policy_conflict_excluded_and_queued() -> None:
    big = family("conflict", CELL_A, (R1, R2, R3), policy_conflict=True)
    plan = allocate([big, family("ok", CELL_B)], policy(per_family_cap=3, per_root_cap=3))
    assert zero_by_reason(plan)["conflict"] == ("policy_conflict", 0)
    assert {"kind": "owner_decision", "family_id": "conflict"} in plan.requests
    assert all(x.cell == CELL_B for x in plan.lines)


# E3: a root claimed by two families makes both untrustworthy.
def test_shared_root_across_families_excludes_both() -> None:
    shared = RootInput("generated-root-shared", "generated-digest-s1", (R1,), 0, "generated-case-s")
    plan = allocate(
        [family("x1", roots=(shared,)), family("x2", roots=(shared,)), family("ok")], policy()
    )
    got = zero_by_reason(plan)
    assert got["x1"][0] == got["x2"][0] == "family_identity_conflict"
    assert got["ok"] == ("eligible", 1)
    with pytest.raises(AllocationError, match="duplicate_family_id"):
        allocate([family("same"), family("same")], policy())


# E4: explicit holds, never a silent empty success.
@pytest.mark.parametrize(
    ("families", "kw", "reason"),
    [
        ([family("u", verification="unverified")], {}, "no_eligible_families"),
        ([], {}, "no_eligible_families"),
        ([family("a")], {"total_units": 0}, "zero_budget"),
        ([family("a")], {"spend_ceiling": 0}, "zero_budget"),
        ([family("a")], {"unit_cost": 5, "spend_ceiling": 4}, "zero_budget"),
        (
            [family("a")],
            {"min_yield_milli": 500, "observed_yield_milli": 499},
            "poor_validation_yield",
        ),
        ([family("a", recipes=())], {}, "no_uncovered_transformations"),
        ([family("a")], {"min_support": 2}, "insufficient_support"),
    ],
)
def test_hold_is_explicit(families: list[FamilyInput], kw: dict[str, object], reason: str) -> None:
    plan = allocate(families, policy(**kw))
    assert plan.status == "hold" and plan.hold_reason == reason and plan.units == 0
    with pytest.raises(AllocationError, match="allocation_held"):
        led = AllocationLedger()
        led.open_budget("generated-budget", cap_units=5, cap_spend=5, unit_cost=1)
        led.reserve("generated-budget", plan)


def test_default_policy_spends_nothing() -> None:
    plan = allocate([family("a")], AllocationPolicy(profile_revision="r", seed="s"))
    assert plan.status == "hold" and plan.hold_reason == "zero_budget"


def test_low_support_cell_requests_real_labels_not_paraphrases() -> None:
    plan = allocate(
        [family("a", CELL_A), family("b", CELL_B), family("c", CELL_B)], policy(min_support=2)
    )
    assert zero_by_reason(plan)["a"] == ("below_min_support", 0)
    assert {"kind": "real_label_request", "cell": list(CELL_A), "support": 1} in plan.requests
    assert all(x.cell == CELL_B for x in plan.lines)


def test_mandatory_slice_and_reserved_quota_conflicts_hold() -> None:
    fams = [family("a", CELL_A), family("b", CELL_B)]
    unfit = allocate(fams, policy(critical_cells=((CELL_A, 2),)))
    assert unfit.hold_reason == "mandatory_slice_unfit"
    missing = allocate(fams, policy(critical_cells=((("generated-none", "x"), 1),)))
    assert missing.hold_reason == "mandatory_slice_unfit"
    conflict = allocate(
        fams,
        policy(total_units=2, spend_ceiling=2, critical_cells=((CELL_A, 1),), exploration_quota=2),
    )
    assert conflict.hold_reason == "constraints_conflict"


def test_minimum_and_exploration_reserved_before_ranked() -> None:
    fams = fleet()
    p = policy(
        total_units=6,
        spend_ceiling=6,
        per_family_cap=2,
        per_root_cap=2,
        critical_cells=((CELL_B, 2),),
        exploration_quota=1,
    )
    plan = allocate(fams, p)
    phases = [x.phase for x in plan.lines]
    assert phases[:2] == ["minimum", "minimum"] and phases[2] == "exploration"
    assert sum(1 for x in plan.lines if x.cell == CELL_B and x.phase == "minimum") == 2
    assert plan.units == 6


def test_caps_bound_family_and_root() -> None:
    f = family("a", recipes=(R1, R2, R3))
    assert allocate([f], policy(per_family_cap=2, per_root_cap=3)).units == 2
    assert allocate([f], policy(per_family_cap=3, per_root_cap=1)).units == 1
    spent = family(
        "a",
        roots=(
            RootInput(
                "generated-root-a", "generated-digest-a", (R1, R2, R3), 2, "generated-case-a"
            ),
        ),
    )
    assert allocate([spent], policy(per_family_cap=3, per_root_cap=3)).units == 1
    full = family(
        "a",
        roots=(RootInput("generated-root-a", "generated-digest-a", (R1,), 1, "generated-case-a"),),
    )
    assert allocate([full], policy(per_family_cap=1)).hold_reason == "no_uncovered_transformations"


# P1: protected answers and scores have no entry point (structural proof).
def test_no_protected_input_exists_structurally() -> None:
    names = {
        f.name
        for cls in (FamilyInput, RootInput, AllocationPolicy)
        for f in dataclasses.fields(cls)
    }
    assert not names & sa.PROTECTED_KEYS
    assert not set(inspect.signature(allocate).parameters) & sa.PROTECTED_KEYS
    assert "protected" not in "".join(sorted(names - {"protected_derived"}))
    for key in sorted(sa.PROTECTED_KEYS):
        record = {
            "family_id": "f",
            "cell": list(CELL_A),
            "split": "training",
            "verification": "verified",
            "verifier_independent": True,
            "roots": [{"root_ref": "r", "content_digest": "d"}],
        }
        with pytest.raises(AllocationError, match="protected_input_rejected"):
            family_from_record({**record, key: "generated-answer"})
        with pytest.raises(AllocationError, match="protected_input_rejected"):
            family_from_record(
                {**record, "roots": [{"root_ref": "r", "content_digest": "d", key: 1}]}
            )
    with pytest.raises(AllocationError, match="unknown_field"):
        family_from_record({**record, "surprise": 1})


def test_plan_record_carries_no_protected_key_and_ignores_protected_flag_only_to_exclude() -> None:
    plan = allocate(fleet(), policy(per_family_cap=2, per_root_cap=2))
    assert not _keys(plan.as_record()) & sa.PROTECTED_KEYS
    record = {
        "family_id": "f",
        "cell": list(CELL_A),
        "split": "training",
        "verification": "verified",
        "verifier_independent": True,
        "protected_derived": True,
        "roots": [{"root_ref": "r", "content_digest": "d", "uncovered_recipes": [R1]}],
    }
    assert allocate([family_from_record(record)], policy()).hold_reason == "no_eligible_families"


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


# C1: only admitted OPE-516 children cover a recipe; nothing else is consumed from them.
def test_covered_from_children_uses_admitted_only() -> None:
    kids = [
        {
            "disposition": d,
            "root_ref": "generated-root-a",
            "recipe": {"recipe_id": R1},
            "independent_family_count": 99,
        }
        for d in ("admitted", "held", "validated_preview", "quarantined", "rejected", "invalidated")
    ]
    assert covered_from_children(kids) == {"generated-root-a": {R1: 1}}
    root = RootInput("generated-root-a", "generated-digest-a", (R1, R2), 1, "generated-case-a")
    plan = allocate([family("a", roots=(root,))], policy(per_root_cap=2, per_family_cap=2))
    assert plan.units == 1


# B*: ledger.
def ledger(
    tmp: Path | None = None, units: int = 3, spend: int = 3, unit_cost: int = 1
) -> AllocationLedger:
    led = AllocationLedger(tmp / "alloc.db" if tmp else ":memory:")
    led.open_budget("generated-budget", cap_units=units, cap_spend=spend, unit_cost=unit_cost)
    return led


def plan_of(prefix: str, n: int, **kw: object) -> sa.AllocationPlan:
    fams = [family(f"{prefix}{i}") for i in range(n)]
    return allocate(fams, policy(total_units=n, spend_ceiling=n, **kw))


def test_budget_exactly_exhausted_then_one_more_refused_not_rounded() -> None:
    led = ledger(units=3, spend=3)
    first = plan_of("p", 3)
    assert led.reserve("generated-budget", first)["remaining_units"] == 0
    led.commit("generated-budget", first.allocation_id)
    with pytest.raises(AllocationError, match="budget_exceeded"):
        led.reserve("generated-budget", plan_of("q", 1))
    status = led.status("generated-budget")
    assert status["used_units"] == 3 and status["used_spend"] == 3


def test_partial_fit_is_refused_whole() -> None:
    led = ledger(units=3, spend=3)
    led.reserve("generated-budget", plan_of("p", 2))
    with pytest.raises(AllocationError, match="budget_exceeded"):
        led.reserve("generated-budget", plan_of("q", 2))
    assert led.status("generated-budget")["used_units"] == 2
    assert not [r for r in led.live_lines("generated-budget") if r[0].startswith("q")]


def test_spend_ceiling_binds_independently_of_units() -> None:
    led = ledger(units=10, spend=3, unit_cost=2)
    pricey = allocate(
        [family("a"), family("b")], policy(total_units=2, spend_ceiling=8, unit_cost=2)
    )
    assert pricey.spend == 4
    with pytest.raises(AllocationError, match="budget_exceeded"):
        led.reserve("generated-budget", pricey)


def test_replay_adds_nothing_and_spends_nothing(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    plan = plan_of("p", 2)
    first = led.reserve("generated-budget", plan)
    again = led.reserve("generated-budget", plan)
    assert first["replayed"] is False and again["replayed"] is True
    assert again["used_units"] == 2
    led.commit("generated-budget", plan.allocation_id)
    after = led.reserve("generated-budget", plan)
    assert after["replayed"] is True and after["state"] == "committed" and after["used_units"] == 2
    assert led.commit("generated-budget", plan.allocation_id)["used_units"] == 2
    restarted = AllocationLedger(tmp_path / "alloc.db")
    assert restarted.reserve("generated-budget", plan)["replayed"] is True
    assert restarted.status("generated-budget")["used_units"] == 2


def test_crash_mid_allocation_leaves_nothing(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    plan = plan_of("p", 3)
    with pytest.raises(RuntimeError, match="simulated crash"):
        led.reserve("generated-budget", plan, _fail_after=2)
    assert led.status("generated-budget")["used_units"] == 0
    assert led.live_lines("generated-budget") == []
    restarted = AllocationLedger(tmp_path / "alloc.db")
    assert restarted.reserve("generated-budget", plan)["used_units"] == 3
    assert len(restarted.live_lines("generated-budget")) == 3


def test_unfinished_reservation_is_released_then_replayed(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    plan = plan_of("p", 3)
    led.reserve("generated-budget", plan)
    assert led.status("generated-budget")["remaining_units"] == 0  # held until reconciled
    assert led.release("generated-budget", plan.allocation_id)["used_units"] == 0
    assert led.reserve("generated-budget", plan)["replayed"] is False
    led.commit("generated-budget", plan.allocation_id)
    with pytest.raises(AllocationError, match="allocation_committed"):
        led.release("generated-budget", plan.allocation_id)
    with pytest.raises(AllocationError, match="allocation_unknown"):
        led.commit("generated-budget", "generated-missing")


def test_concurrent_allocation_cannot_exceed_cap(tmp_path: Path) -> None:
    led = ledger(tmp_path, units=5, spend=5)
    plans = [plan_of(f"w{i}-", 3) for i in range(8)]

    def attempt(plan: sa.AllocationPlan) -> str:
        try:
            AllocationLedger(tmp_path / "alloc.db").reserve("generated-budget", plan)
            return "ok"
        except AllocationError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, plans))
    assert results.count("ok") == 1
    assert results.count("budget_exceeded") == 7
    assert led.status("generated-budget")["used_units"] == 3
    assert len(led.live_lines("generated-budget")) == 3


def test_concurrent_replay_of_same_plan_counts_once(tmp_path: Path) -> None:
    led = ledger(tmp_path, units=5, spend=5)
    plan = plan_of("p", 3)

    def attempt(_: int) -> bool:
        return bool(
            AllocationLedger(tmp_path / "alloc.db").reserve("generated-budget", plan)["replayed"]
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        replayed = list(pool.map(attempt, range(8)))
    assert replayed.count(False) == 1
    assert led.status("generated-budget")["used_units"] == 3


def test_retracted_family_allocation_is_invalidated() -> None:
    led = ledger(units=6, spend=6)
    plan = plan_of("p", 3)
    led.reserve("generated-budget", plan)
    led.commit("generated-budget", plan.allocation_id)
    before = led.status("generated-budget")
    led.retract_family("generated-budget", "p0", "generated-retraction")
    assert ("p0", "generated-root-p0", R1) not in led.live_lines("generated-budget")
    assert len(led.live_lines("generated-budget")) == 2
    # committed spend is never refunded
    assert led.status("generated-budget")["used_spend"] == before["used_spend"] == 3
    # replay does not resurrect it and a new plan cannot re-admit the family
    assert led.reserve("generated-budget", plan)["replayed"] is True
    assert len(led.live_lines("generated-budget")) == 2
    with pytest.raises(AllocationError, match="family_retracted"):
        led.reserve(
            "generated-budget", allocate([family("p0")], policy(total_units=1, spend_ceiling=1))
        )


def test_retracting_unspent_reservation_frees_budget() -> None:
    led = ledger(units=3, spend=3)
    led.reserve("generated-budget", plan_of("p", 3))
    led.retract_family("generated-budget", "p1", "generated-retraction")
    assert led.status("generated-budget")["used_units"] == 2


def test_overlapping_line_across_allocations_refused_atomically() -> None:
    led = ledger(units=10, spend=10)
    led.reserve(
        "generated-budget",
        plan_of(
            "p",
            2,
        ),
    )
    overlap = allocate(
        [family("p0"), family("n1")],
        policy(total_units=2, spend_ceiling=2, seed="generated-seed-2"),
    )
    with pytest.raises(AllocationError, match="line_already_allocated"):
        led.reserve("generated-budget", overlap)
    assert led.status("generated-budget")["used_units"] == 2


def test_budget_definition_is_immutable_and_required() -> None:
    led = ledger(units=3, spend=3)
    led.open_budget("generated-budget", cap_units=3, cap_spend=3, unit_cost=1)  # idempotent
    with pytest.raises(AllocationError, match="budget_conflict"):
        led.open_budget("generated-budget", cap_units=9, cap_spend=9, unit_cost=1)
    with pytest.raises(AllocationError, match="budget_unknown"):
        led.reserve("generated-other", plan_of("p", 1))
    with pytest.raises(AllocationError, match="policy_invalid"):
        allocate([family("a")], policy(unit_cost=0))
    with pytest.raises(AllocationError, match="policy_invalid"):
        allocate([family("a")], policy(total_units=-1))


# N1/N2 (OPE-516 review): one real case behind several roots or families is not several families.
def test_one_case_under_several_families_is_not_several_families() -> None:
    def clone(fid: str) -> FamilyInput:
        root = RootInput(
            f"generated-root-{fid}", f"generated-digest-{fid}", (R1,), 0, "generated-case-one"
        )
        return family(fid, roots=(root,))

    plan = allocate([clone("c1"), clone("c2"), clone("c3"), family("ok")], policy(per_family_cap=1))
    got = zero_by_reason(plan)
    assert [got[f][0] for f in ("c1", "c2", "c3")] == ["family_identity_conflict"] * 3
    assert got["ok"] == ("eligible", 1) and plan.units == 1
    assert dict(plan.cell_scores) == {CELL_A: "1/1"}


def test_five_roots_in_one_family_stay_one_family() -> None:
    roots = tuple(
        RootInput(f"generated-root-m{i}", f"generated-digest-m{i}", (R1,), 0, "generated-case-m")
        for i in range(5)
    )
    plan = allocate([family("m", roots=roots)], policy(per_family_cap=1, per_root_cap=1))
    assert plan.units == 1 and dict(plan.cell_scores) == {CELL_A: "1/1"}


def test_missing_case_ref_is_not_trusted() -> None:
    root = RootInput("generated-root-n", "generated-digest-n", (R1,))
    plan = allocate([family("n", roots=(root,)), family("ok")], policy())
    assert zero_by_reason(plan)["n"] == ("case_ref_missing", 0)


def test_replay_under_another_grant_cannot_double_spend() -> None:
    led = ledger(units=3, spend=3)
    led.open_budget("generated-budget-b", cap_units=3, cap_spend=3, unit_cost=1)
    plan = plan_of("p", 3)
    led.reserve("generated-budget", plan)
    with pytest.raises(AllocationError, match="line_already_allocated"):
        led.reserve("generated-budget-b", plan)
    assert led.status("generated-budget-b")["used_units"] == 0
    assert led.status("generated-budget")["used_units"] == 3


def test_retraction_is_global_across_budgets() -> None:
    led = ledger(units=3, spend=3)
    led.open_budget("generated-budget-b", cap_units=3, cap_spend=3, unit_cost=1)
    led.retract_family("generated-budget", "p0", "generated-retraction")
    with pytest.raises(AllocationError, match="family_retracted"):
        led.reserve("generated-budget-b", plan_of("p", 1))


# R1-R6: closer review regressions (independent reviewer). Each failed before the fix.
def _one(fid: str, root: str, digest: str, case: str) -> FamilyInput:
    return family(fid, roots=(RootInput(root, digest, (R1,), 0, case),))


@pytest.mark.parametrize(
    "other",
    ["Generated-Case-x", " generated-case-x ", "generated-case-x​", "generated-case－x"],
)
def test_case_ref_spelling_variants_are_one_case(other: str) -> None:
    fams = [_one("f1", "generated-root-1", "generated-d1", "generated-case-x")]
    fams.append(_one("f2", "generated-root-2", "generated-d2", other))
    plan = allocate(fams, policy())
    assert {d.reason for d in plan.decisions} == {"family_identity_conflict"}
    assert plan.status == "hold" and plan.units == 0


def test_root_and_digest_spelling_variants_do_not_count_twice() -> None:
    shared_root = [
        _one("f1", "generated-root-A", "generated-d1", "generated-c1"),
        _one("f2", " GENERATED-ROOT-a", "generated-d2", "generated-c2"),
    ]
    assert {d.reason for d in allocate(shared_root, policy()).decisions} == {
        "family_identity_conflict"
    }
    dup_digest = [
        _one("f1", "generated-root-1", "generated-DIGEST", "generated-c1"),
        _one("f2", "generated-root-2", "generated-digest ", "generated-c2"),
    ]
    reasons = {d.family_id: d.reason for d in allocate(dup_digest, policy()).decisions}
    assert reasons == {"f1": "eligible", "f2": "duplicate_family"}
    child = [_one("f1", "VAR-generated", "generated-d1", "generated-c1")]
    assert allocate(child, policy()).decisions[0].reason == "child_derived"


def test_repeated_root_within_family_is_excluded_not_double_allocated() -> None:
    fam = family(
        "f1",
        roots=(
            RootInput("generated-root", "generated-d1", (R1,), 0, "generated-c1"),
            RootInput("generated-root", "generated-d2", (R1,), 0, "generated-c1"),
        ),
    )
    plan = allocate([fam], policy(per_family_cap=5, per_root_cap=5))
    assert plan.decisions[0].reason == "duplicate_root" and plan.units == 0


@pytest.mark.parametrize("bad", [-1, True, 1.5])
def test_admitted_children_cannot_be_negative_to_widen_caps(bad: object) -> None:
    root = RootInput("generated-root", "generated-d1", (R1, R2, R3), bad, "generated-c1")  # type: ignore[arg-type]
    with pytest.raises(AllocationError) as err:
        allocate([family("f1", roots=(root,))], policy(per_family_cap=1, per_root_cap=1))
    assert err.value.code == "input_invalid"


def test_ledger_refuses_forged_cost_or_repeated_lines() -> None:
    led = ledger(units=5, spend=5)
    good = plan_of("generated-p", 2)
    forged = replace(good, lines=tuple(replace(x, cost=-5) for x in good.lines))
    repeated = replace(good, lines=(good.lines[0], good.lines[0]))
    for bad in (forged, repeated):
        with pytest.raises(AllocationError) as err:
            led.reserve("generated-budget", bad)
        assert err.value.code == "plan_invalid"
    assert led.status("generated-budget")["used_spend"] == 0


def test_record_shapes_are_strict() -> None:
    base = {"family_id": "f", "split": "training", "verification": "verified", "roots": []}
    for cell in ({"protected_answer": 1, "x": 2}, ["a"], [1, 2]):
        with pytest.raises(AllocationError) as err:
            family_from_record({**base, "cell": cell})
        assert err.value.code == "cell_invalid"
    with pytest.raises(AllocationError) as err2:
        family_from_record({**base, "cell": ["a", "b"], "roots": {"x": 1}})
    assert err2.value.code == "roots_invalid"


# L1-L4 (audit batch 2, LOW): the ledger binds a plan to the budget's unit cost and to the
# allocation id its own content implies. Every mismatch is one fixed code.
def priced_plan(unit_cost: int = 5, n: int = 2) -> sa.AllocationPlan:
    fams = [family(f"u{i}") for i in range(n)]
    return allocate(fams, policy(total_units=n, spend_ceiling=n * unit_cost, unit_cost=unit_cost))


def test_reserve_refuses_under_charged_plan() -> None:
    honest = priced_plan(5)
    assert honest.spend == 10
    forged = replace(honest, lines=tuple(replace(x, cost=1) for x in honest.lines))
    led = ledger(units=10, spend=10, unit_cost=5)
    with pytest.raises(AllocationError) as err:
        led.reserve("generated-budget", forged)
    assert err.value.code == sa.BINDING_MISMATCH
    # An honest plan built under a cheaper profile is an under-charge against this budget too.
    cheap = priced_plan(1)
    with pytest.raises(AllocationError) as err2:
        led.reserve("generated-budget", cheap)
    assert err2.value.code == sa.BINDING_MISMATCH
    assert led.status("generated-budget")["used_spend"] == 0
    ok = led.reserve("generated-budget", honest)
    assert ok["used_units"] == 2 and ok["used_spend"] == 10 and ok["unit_cost"] == 5


def test_reserve_refuses_wrong_allocation_id() -> None:
    plan = priced_plan(1)
    led = ledger(units=10, spend=10)
    renamed = replace(plan, allocation_id="generated-other-id")
    swapped = replace(plan, lines=(replace(plan.lines[0], recipe_id=R2), *plan.lines[1:]))
    dropped = replace(plan, lines=plan.lines[:1])
    for bad in (renamed, swapped, dropped):
        with pytest.raises(AllocationError) as err:
            led.reserve("generated-budget", bad)
        assert err.value.code == sa.BINDING_MISMATCH
    assert led.status("generated-budget")["used_units"] == 0
    assert sa.plan_allocation_id(plan) == plan.allocation_id


def test_replayed_plan_must_match_what_was_reserved(tmp_path: Path) -> None:
    plan = priced_plan(1, n=3)
    led = ledger(tmp_path, units=10, spend=10)
    assert led.reserve("generated-budget", plan)["replayed"] is False
    # Same id and lines, altered recorded content: a replay must match what was stored.
    altered = replace(plan, decisions=(), requests=({"kind": "generated-forged"},))
    with pytest.raises(AllocationError) as err:
        led.reserve("generated-budget", altered)
    assert err.value.code == sa.BINDING_MISMATCH
    restarted = AllocationLedger(tmp_path / "alloc.db")
    assert restarted.reserve("generated-budget", plan)["replayed"] is True
    assert restarted.status("generated-budget")["used_units"] == 3
    led.release("generated-budget", plan.allocation_id)
    # After a release the id is free again, but only for the content it was derived from.
    with pytest.raises(AllocationError) as err2:
        led.reserve("generated-budget", replace(plan, lines=plan.lines[:2]))
    assert err2.value.code == sa.BINDING_MISMATCH
    assert led.status("generated-budget")["used_units"] == 0


def test_budget_unit_cost_is_bound_and_immutable(tmp_path: Path) -> None:
    led = ledger(units=3, spend=3, unit_cost=1)
    with pytest.raises(AllocationError, match="budget_conflict"):
        led.open_budget("generated-budget", cap_units=3, cap_spend=3, unit_cost=2)
    for bad in (0, True, "1", 1.0, -1):
        with pytest.raises(AllocationError, match="budget_invalid"):
            led.open_budget("generated-other", cap_units=1, cap_spend=1, unit_cost=bad)  # type: ignore[arg-type]
    old = tmp_path / "old.db"
    db = sqlite3.connect(old)
    db.execute(
        "CREATE TABLE budget (budget_ref TEXT PRIMARY KEY, cap_units INTEGER NOT NULL,"
        " cap_spend INTEGER NOT NULL)"
    )
    db.commit()
    db.close()
    with pytest.raises(AllocationError, match="ledger_schema_outdated"):
        AllocationLedger(old)


# V1-V5 (audit batch 2, MEDIUM): verification comes from OPE-516's worker record where one
# exists; caller-asserted flags are labelled and can only narrow, never lift a hold.
TV_POLICY = tv.Policy("generated-policy-r1", ("vendor_note",), ("vendor_note",), {})
TV_GRANT = tv.Grant("generated-grant-1", True, True, True, budget=10, expires_at=1000)
TV_RECIPE = tv.Recipe("generated-paraphrase", "r1", "paraphrase", frozenset({"vendor_note"}))
TV_NOW = 100


def tv_seal(*fids: str) -> tv.SplitSeal:
    return tv.SplitSeal.from_rows([(f"generated-case-{f}", f, "training") for f in fids])


def tv_register(
    variants: tv.VariantLedger, fid: str, seal: tv.SplitSeal, cost: int | None = None
) -> None:
    seed = tv.SeedRoot(
        root_ref=f"generated-root-{fid}",
        case_ref=f"generated-case-{fid}",
        label_ref=f"generated-label-{fid}",
        family_id=fid,
        split="training",
        policy_revision=TV_POLICY.revision,
        source_revision="generated-src-r1",
        fields={"vendor_note": "generated carrier note"},
        prediction="SUPPLIES",
        target="FREIGHT",
        target_verified=True,
        target_verifier_ref="generated-verifier",
        verifier_independent=True,
    )
    proposals: tuple[tv.ProposedChild, ...] = ()
    if cost is not None:
        proposals = (
            tv.ProposedChild(
                TV_RECIPE.recipe_id, {"vendor_note": "generated haulage wording"}, "FREIGHT", cost
            ),
        )
    variants.submit(
        tv.GenerationPlan(
            plan_ref=f"generated-plan-{fid}",
            mode="training",
            root=seed,
            policy=TV_POLICY,
            grant=TV_GRANT,
            seal=seal,
            recipes={TV_RECIPE.recipe_id: TV_RECIPE},
            generator=tv.Generator("generated-generator", "r1"),
            proposals=proposals,
            fit_gate=tv.FitGate(True, "generated-gate"),
            approved_validators=frozenset({"generated-validator"}),
        ),
        now=TV_NOW,
    )


def scope(variants: tv.VariantLedger, seal: tv.SplitSeal) -> sa.VerificationScope:
    return sa.VerificationScope(variants, seal, TV_POLICY.revision, TV_GRANT.grant_ref)


def test_verification_is_read_from_the_ope516_worker_record() -> None:
    seal = tv_seal("a0", "b0")
    variants = tv.VariantLedger()
    tv_register(variants, "a0", seal)  # b0 is sealed but OPE-516 never registered a root
    fams = [family("a0"), family("b0")]
    plan = allocate(fams, policy(), verified_by=scope(variants, seal))
    assert zero_by_reason(plan) == {
        "a0": ("eligible", 1),
        "b0": ("verification_uncorroborated", 0),
    }
    assert plan.verification_basis == sa.WORKER_RECORD
    assert plan.as_record()["verification_grant_ref"] == TV_GRANT.grant_ref
    assert len(plan.verification_ref) == 64
    bare = allocate(fams, policy())
    assert bare.verification_basis == sa.CALLER_ASSERTED
    assert bare.as_record()["verification_basis"] == "caller_asserted"
    assert bare.allocation_id != plan.allocation_id


def test_caller_flags_cannot_lift_a_hold() -> None:
    seal = tv_seal("a0", "b0")
    variants = tv.VariantLedger()
    tv_register(variants, "a0", seal)
    sc = scope(variants, seal)
    # An uncorroborated family stays held however loudly the caller asserts verification.
    loud = family("b0", verification="verified", verifier_independent=True)
    held = allocate([loud], policy(), verified_by=sc)
    assert held.status == "hold" and held.hold_reason == "no_eligible_families"
    # Neither flag alone lifts a hold, with or without the worker record.
    for kw in (
        {"verification": "verified", "verifier_independent": False},
        {"verification": "unverified", "verifier_independent": True},
    ):
        for verified_by in (None, sc):
            alone = allocate([family("a0", **kw)], policy(), verified_by=verified_by)
            assert alone.status == "hold" and alone.units == 0
    # The caller cannot widen a worker record: disputed still excludes a corroborated family.
    narrowed = allocate([family("a0", verification="disputed")], policy(), verified_by=sc)
    assert zero_by_reason(narrowed)["a0"] == ("disputed", 0)
    # A root whose case is not sealed under this family is not corroborated by the family id.
    moved_root = RootInput(
        "generated-root-a0", "generated-digest-a0", (R1,), 0, "generated-case-b0"
    )
    moved = allocate([family("a0", roots=(moved_root,))], policy(), verified_by=sc)
    assert zero_by_reason(moved)["a0"][0] == "verification_uncorroborated"
    # OPE-516 retracts the root: the family is no longer corroborated.
    variants.retract_root("generated-root-a0", "a0", "generated-reopen")
    gone = allocate([family("a0")], policy(), verified_by=sc)
    assert gone.status == "hold"
    assert zero_by_reason(gone)["a0"][0] == "verification_uncorroborated"


def test_grant_budget_refuses_caller_asserted_plans() -> None:
    seal = tv_seal("a0")
    variants = tv.VariantLedger()
    tv_register(variants, "a0", seal)
    led = AllocationLedger()
    led.open_budget_for_grant(
        "generated-budget", grant=TV_GRANT, variants=variants, now=TV_NOW, cap_units=5, unit_cost=1
    )
    asserted = allocate([family("a0")], policy())
    with pytest.raises(AllocationError) as err:
        led.reserve("generated-budget", asserted)
    assert err.value.code == "verification_uncorroborated"
    other_scope = sa.VerificationScope(variants, seal, TV_POLICY.revision, "generated-grant-2")
    elsewhere = allocate([family("a0")], policy(), verified_by=other_scope)
    assert elsewhere.status == "hold"  # nothing registered under that grant
    verified = allocate([family("a0")], policy(), verified_by=scope(variants, seal))
    assert led.reserve("generated-budget", verified)["used_units"] == 1
    fake = replace(scope(variants, seal), variants=object())
    with pytest.raises(AllocationError) as err2:
        allocate([family("a0")], policy(), verified_by=fake)
    assert err2.value.code == "verification_source_invalid"


def _t_root(**kw: object) -> RootInput:
    base: dict[str, object] = {
        "root_ref": "generated-root-t",
        "content_digest": "generated-digest-t",
        "uncovered_recipes": (R1,),
        "admitted_children": 0,
        "case_ref": "generated-case-t",
    }
    return RootInput(**{**base, **kw})  # type: ignore[arg-type]


class _Str(str):
    pass


@pytest.mark.parametrize(
    "kw",
    [
        {"family_id": 7},
        {"verification": _Str("verified")},
        {"verifier_independent": 1},
        {"policy_conflict": 0},
        {"split": None},
        {"cell": ["generated-class-a", "generated-branch-1"]},
        {"model_revision": None},
        {"roots": [_t_root()]},
        {"roots": (_t_root(case_ref=None),)},
        {"roots": (_t_root(root_ref=None),)},
        {"roots": (_t_root(content_digest=7),)},
        {"roots": (_t_root(uncovered_recipes=[R1]),)},
        {"roots": (_t_root(admitted_children=False),)},
    ],
)
def test_identity_and_count_fields_need_exact_types(kw: dict[str, object]) -> None:
    base: dict[str, object] = {
        "family_id": "t",
        "cell": CELL_A,
        "split": "training",
        "verification": "verified",
        "verifier_independent": True,
        "roots": (_t_root(),),
    }
    fam = FamilyInput(**{**base, **kw})  # type: ignore[arg-type]
    with pytest.raises(AllocationError) as err:
        allocate([fam, family("ok")], policy())
    assert err.value.code == "input_invalid"


@pytest.mark.parametrize(
    ("change", "root_change"),
    [
        ({"family_id": 7}, {}),
        ({"verification": True}, {}),
        ({"verifier_independent": "true"}, {}),
        ({"verifier_independent": 1}, {}),
        ({"model_revision": 3}, {}),
        ({}, {"root_ref": None}),
        ({}, {"content_digest": 7}),
        ({}, {"case_ref": None}),
        ({}, {"admitted_children": float("nan")}),
        ({}, {"admitted_children": "1"}),
        ({}, {"uncovered_recipes": [R1, 2]}),
    ],
)
def test_record_identity_and_count_fields_are_not_coerced(
    change: dict[str, object], root_change: dict[str, object]
) -> None:
    root = {
        "root_ref": "generated-root",
        "content_digest": "generated-d",
        "case_ref": "generated-c",
    }
    record = {
        "family_id": "f",
        "cell": list(CELL_A),
        "split": "training",
        "verification": "verified",
        "verifier_independent": True,
        "roots": [{**root, **root_change}],
        **change,
    }
    with pytest.raises(AllocationError) as err:
        family_from_record(record)
    assert err.value.code == "input_invalid"
    missing = {k: v for k, v in record.items() if k != "split"}
    with pytest.raises(AllocationError) as err2:
        family_from_record({**missing, "roots": [root]})
    assert err2.value.code == "input_invalid"


# G1 (audit batch 2, MEDIUM): the allocation spend cap is narrowed to OPE-516's grant headroom.
def open_for_grant(
    led: AllocationLedger, ref: str, variants: tv.VariantLedger, **kw: object
) -> dict:
    args: dict[str, object] = {
        "grant": TV_GRANT,
        "variants": variants,
        "now": TV_NOW,
        "cap_units": 50,
        "unit_cost": 1,
    }
    return led.open_budget_for_grant(ref, **{**args, **kw})  # type: ignore[arg-type]


def test_grant_link_narrows_spend_cap_to_ope516_headroom() -> None:
    seal = tv_seal("a0", "b0")
    variants = tv.VariantLedger()
    tv_register(variants, "a0", seal, cost=4)
    assert variants.spent(TV_GRANT.grant_ref) == 4
    assert sa.grant_headroom(TV_GRANT, variants, now=TV_NOW) == 6
    led = AllocationLedger()
    opened = open_for_grant(led, "generated-budget", variants)
    assert opened["cap_spend"] == 6 and opened["grant_ref"] == TV_GRANT.grant_ref
    assert open_for_grant(led, "generated-budget-2", variants, cap_spend=3)["cap_spend"] == 3
    # Never wider than the grant.
    assert open_for_grant(led, "generated-budget-3", variants, cap_spend=999)["cap_spend"] == 6
    tv_register(variants, "b0", seal, cost=2)
    # Idempotent reopen keeps the stored cap.
    assert open_for_grant(led, "generated-budget", variants)["cap_spend"] == 6
    with pytest.raises(AllocationError, match="budget_conflict"):
        open_for_grant(led, "generated-budget", variants, unit_cost=2)
    for grant, now in (
        (TV_GRANT, 1000),
        (replace(TV_GRANT, generation_authorized=False), TV_NOW),
        (replace(TV_GRANT, execution_location_approved=False), TV_NOW),
        (replace(TV_GRANT, budget=6), TV_NOW),  # 6 already spent
    ):
        assert sa.grant_headroom(grant, variants, now=now) == 0
        with pytest.raises(AllocationError, match="grant_unavailable"):
            open_for_grant(led, "generated-budget-x", variants, grant=grant, now=now)
    with pytest.raises(AllocationError, match="grant_link_invalid"):
        sa.grant_headroom(replace(TV_GRANT, budget=True), variants, now=TV_NOW)


# S1 (acceptance): subdivide by model revision only when the owner's minimum is met.
def revision_fleet(r1: int, r2: int) -> list[FamilyInput]:
    one = "generated-model-r1"
    two = "generated-model-r2"
    fams = [family(f"x{i}", CELL_A, (R1,), model_revision=one) for i in range(r1)]
    fams += [family(f"y{i}", CELL_A, (R1, R2), model_revision=two) for i in range(r2)]
    return fams


def test_model_revision_subdivides_only_when_each_revision_meets_minimum() -> None:
    split = allocate(
        revision_fleet(3, 3), policy(revision_min_support=3, per_family_cap=2, per_root_cap=2)
    )
    s1 = (*CELL_A, "generated-model-r1")
    s2 = (*CELL_A, "generated-model-r2")
    assert dict(split.cell_scores) == {s1: "3/1", s2: "6/1"}
    assert {x.stratum for x in split.lines} == {s1, s2}
    assert all(x.cell == CELL_A for x in split.lines)
    assert split.detail["revision_split"] == [[list(CELL_A), "subdivided"]]
    for fams, minimum, why in (
        (revision_fleet(3, 2), 3, "support_below_minimum"),
        (revision_fleet(3, 3), 0, "disabled"),
        (revision_fleet(4, 0), 2, "single_revision"),
        ([*revision_fleet(3, 3), family("z0", CELL_A)], 3, "revision_missing"),
    ):
        plan = allocate(fams, policy(revision_min_support=minimum))
        assert plan.detail["revision_split"] == [[list(CELL_A), why]]
        assert set(dict(plan.cell_scores)) == {CELL_A}
    # Critical-slice minima still apply to the parent cell across its revision strata.
    mins = allocate(
        revision_fleet(3, 3),
        policy(
            revision_min_support=3, critical_cells=((CELL_A, 2),), total_units=2, spend_ceiling=2
        ),
    )
    assert [x.phase for x in mins.lines] == ["minimum", "minimum"]
    with pytest.raises(AllocationError, match="policy_invalid"):
        allocate(revision_fleet(3, 3), policy(revision_min_support=-1))


# H1-H4 (acceptance): later-time confirmation cohort with an outcome-maturity window, a
# separate repeat-entity partition, leakage exclusion and holdout from allocation.
def case(ref: str, entity: str, at: int, dig: str = "", **kw: object) -> sa.CohortCase:
    return sa.CohortCase(ref, entity, at, dig or f"generated-content-{ref}", **kw)  # type: ignore[arg-type]


def frame() -> list[sa.CohortCase]:
    return [
        case(
            "generated-case-a0",
            "generated-entity-1",
            10,
            template_ref="generated-template-1",
            attachment_digests=("generated-attach-1",),
        ),
        case("generated-case-b0", "generated-entity-2", 20),
    ]


def cohort_candidates() -> list[sa.CohortCase]:
    return [
        case("generated-late-early", "generated-entity-9", 40),  # not later-time
        case("generated-late-ind", "generated-entity-9", 60),  # matured, new entity
        case("generated-late-rep", "generated-entity-1", 61),  # matured, known entity
        case("generated-late-young", "generated-entity-8", 95),  # not matured yet
        case("generated-late-dup", "generated-entity-7", 62, "generated-content-generated-case-b0"),
        case("generated-late-tpl", "generated-entity-6", 63, template_ref="GENERATED-TEMPLATE-1"),
        case(
            "generated-late-att",
            "generated-entity-5",
            64,
            attachment_digests=("generated-attach-1",),
        ),
        case("generated-late-noent", "", 65),
        case(
            "generated-late-twin", "generated-entity-4", 66, "generated-content-generated-late-ind"
        ),
        case("generated-case-b0", "generated-entity-2", 67, "generated-content-fresh"),
    ]


def cohort(now: int = 100, **kw: object) -> sa.ConfirmationCohort:
    args: dict[str, object] = {"cutoff": 50, "maturity_window": 10, "now": now, **kw}
    return sa.confirmation_cohort(frame(), cohort_candidates(), **args)  # type: ignore[arg-type]


def test_confirmation_cohort_partitions_later_time_cases() -> None:
    got = cohort()
    assert got.independent == ("generated-late-ind",)
    assert got.repeat_entity == ("generated-late-rep",)
    assert got.immature == (("generated-late-young", 105),)
    assert dict(got.excluded) == {
        "generated-case-b0": "leakage_case",
        "generated-late-att": "leakage_attachment",
        "generated-late-dup": "leakage_duplicate",
        "generated-late-early": "not_later_time",
        "generated-late-noent": "entity_ref_missing",
        "generated-late-tpl": "leakage_template",
        "generated-late-twin": "duplicate_in_cohort",
    }
    assert "generated-late-early" not in got.held_out and len(got.held_out) == 9
    flipped = sa.confirmation_cohort(
        list(reversed(frame())),
        list(reversed(cohort_candidates())),
        cutoff=50,
        maturity_window=10,
        now=100,
    )
    assert flipped.as_record() == got.as_record()
    matured = cohort(now=105)
    assert "generated-late-young" in matured.independent and not matured.immature
    with pytest.raises(AllocationError, match="cutoff_overlaps_frame"):
        sa.confirmation_cohort(frame(), [], cutoff=15, maturity_window=10, now=100)
    twins = [case("generated-x", "e", 60), case("Generated-X ", "e", 61)]
    with pytest.raises(AllocationError, match="duplicate_case_ref"):
        sa.confirmation_cohort(frame(), twins, cutoff=50, maturity_window=0, now=100)
    for bad in ({"cutoff": True}, {"maturity_window": -1}, {"now": 1.0}):
        with pytest.raises(AllocationError, match="cohort_invalid"):
            cohort(**bad)
    with pytest.raises(AllocationError, match="cohort_invalid"):
        sa.confirmation_cohort(
            frame(), [case("generated-x", "e", True)], cutoff=50, maturity_window=0, now=100
        )


def test_allocation_holds_out_confirmation_cases_and_unframed_roots() -> None:
    held = cohort()
    late_root = RootInput(
        "generated-root-late", "generated-d-late", (R1,), 0, "generated-late-young"
    )
    late = family("late", roots=(late_root,))
    plan = allocate([family("a0"), family("b0"), late, family("c0")], policy(), holdout=held)
    got = zero_by_reason(plan)
    assert got["late"] == ("confirmation_holdout", 0)
    assert got["c0"] == ("outside_confirmation_frame", 0)
    assert got["a0"] == ("eligible", 1)
    # b0's case reappears later-time (excluded there as leakage_case): it is used nowhere.
    assert got["b0"] == ("confirmation_holdout", 0)
    assert plan.units == 1 and {x.family_id for x in plan.lines} == {"a0"}
    assert plan.holdout_ref == held.cohort_digest
    without = allocate([family("a0")], policy())
    assert without.allocation_id != allocate([family("a0")], policy(), holdout=held).allocation_id


def test_module_docstring_states_the_ope516_link_accurately() -> None:
    doc = sa.__doc__ or ""
    assert "correction lands" not in doc
    for phrase in ("open_budget_for_grant", "VariantLedger.manifest", "caller_asserted", "OPE-517"):
        assert phrase in doc
