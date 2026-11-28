"""Generated sentinel fixtures exercise the synthetic-example allocator and its budget ledger.

Acceptance IDs below (A*, E*, P*, B*, C*) are this suite's own: OPE-520 has no entry in the
approved spec projection, so they are not registered test bindings. Software behavior only; no
provider, customer or ROI evidence.
"""

from __future__ import annotations

import dataclasses
import inspect
import os
import shutil
import sqlite3
import stat
import time
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

WORKER_NOW = {"t": 100}  # TV_NOW: the worker clock agrees with the callers by default
REAL_WORKER_NOW = sa._worker_now if hasattr(sa, "_worker_now") else None


@pytest.fixture(autouse=True)
def _pin_worker_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the worker clock (test-only seam, as in OPE-544); tests move it via WORKER_NOW."""
    WORKER_NOW["t"] = 100
    monkeypatch.setattr(sa, "_worker_now", lambda: WORKER_NOW["t"], raising=False)


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
    assert ("p0", "generated-root-p0", R1) in [x[:3] for x in led.live_lines("generated-budget")]
    before = led.status("generated-budget")
    led.retract_family("generated-budget", "p0", "generated-retraction")
    assert ("p0", "generated-root-p0", R1) not in [
        x[:3] for x in led.live_lines("generated-budget")
    ]
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
#
# Fixture kind: generated. The OPE-516 ledger is a real file-backed ``VariantLedger``. Roots are
# registered with a training-mode plan that has no proposals and no fit gate, and spend is made
# with a preview-mode child, so the fixture works unchanged on OPE-516's current API and on
# PR #225's ``FitGate(reservation_id, actor)`` API (neither path consults a fit gate).
TV_POLICY = tv.Policy("generated-policy-r1", ("vendor_note",), ("vendor_note",), {})
TV_GRANT = tv.Grant("generated-grant-1", True, True, True, budget=10, expires_at=1000)
TV_GRANT_2 = replace(TV_GRANT, grant_ref="generated-grant-2")
TV_RECIPE = tv.Recipe("generated-paraphrase", "r1", "paraphrase", frozenset({"vendor_note"}))
TV_NOW = 100


def tv_seal(*fids: str, extra: tuple[tuple[str, str, str], ...] = ()) -> tv.SplitSeal:
    return tv.SplitSeal.from_rows([*((f"generated-case-{f}", f, "training") for f in fids), *extra])


def tv_ledger(tmp: Path, name: str = "variants.db") -> Path:
    path = tmp / name
    tv.VariantLedger(path)
    return path


def tv_plan(
    fid: str,
    seal: tv.SplitSeal,
    *,
    mode: str = "training",
    grant: tv.Grant = TV_GRANT,
    proposals: tuple[tv.ProposedChild, ...] = (),
    plan_ref: str = "",
) -> tv.GenerationPlan:
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
    return tv.GenerationPlan(
        plan_ref=plan_ref or f"generated-plan-{fid}-{mode}",
        mode=mode,  # type: ignore[arg-type]
        root=seed,
        policy=TV_POLICY,
        grant=grant,
        seal=seal,
        recipes={TV_RECIPE.recipe_id: TV_RECIPE},
        generator=tv.Generator("generated-generator", "r1"),
        proposals=proposals,
        fit_gate=None,
        approved_validators=frozenset({"generated-validator"}),
    )


def tv_register(
    path: Path,
    fid: str,
    seal: tv.SplitSeal,
    *,
    mode: str = "training",
    grant: tv.Grant = TV_GRANT,
) -> None:
    tv.VariantLedger(path).submit(tv_plan(fid, seal, mode=mode, grant=grant), now=TV_NOW)


def tv_spend(path: Path, fid: str, seal: tv.SplitSeal, cost: int, tag: str) -> None:
    """Charge ``cost`` to the grant in OPE-516 through one preview child of ``fid``'s root."""
    child = tv.ProposedChild(
        TV_RECIPE.recipe_id, {"vendor_note": f"generated haulage wording {tag}"}, "FREIGHT", cost
    )
    plan = tv_plan(fid, seal, mode="preview", proposals=(child,), plan_ref=f"generated-spend-{tag}")
    tv.VariantLedger(path).submit(plan, now=TV_NOW)


def scope(
    path: Path, seal: tv.SplitSeal, grant: tv.Grant = TV_GRANT, mode: str = "training"
) -> sa.VerificationScope:
    return sa.VerificationScope(path, seal, TV_POLICY.revision, grant.grant_ref, mode)


def grant_budget(
    led: AllocationLedger, path: Path, ref: str = "generated-budget", **kw: object
) -> dict:
    args: dict[str, object] = {
        "grant": TV_GRANT,
        "variants": path,
        "now": TV_NOW,
        "cap_units": 50,
        "unit_cost": 1,
    }
    return led.open_budget_for_grant(ref, **{**args, **kw})  # type: ignore[arg-type]


def code_of(fn: object, *args: object, **kw: object) -> str:
    with pytest.raises(AllocationError) as err:
        fn(*args, **kw)  # type: ignore[operator]
    return err.value.code


def test_verification_is_read_from_the_ope516_worker_record(tmp_path: Path) -> None:
    seal = tv_seal("a0", "b0")
    path = tv_ledger(tmp_path)
    tv_register(path, "a0", seal)  # b0 is sealed but OPE-516 never registered a root
    fams = [family("a0"), family("b0")]
    plan = allocate(fams, policy(), verified_by=scope(path, seal))
    assert zero_by_reason(plan) == {
        "a0": ("eligible", 1),
        "b0": ("verification_uncorroborated", 0),
    }
    assert plan.verification_basis == sa.WORKER_RECORD
    record = plan.as_record()
    assert record["verification_grant_ref"] == TV_GRANT.grant_ref
    assert record["verification_mode"] == "training"
    assert len(plan.verification_ref) == 64 and len(plan.verification_scope) == 64
    bare = allocate(fams, policy())
    assert bare.verification_basis == sa.CALLER_ASSERTED
    assert bare.as_record()["verification_basis"] == "caller_asserted"
    # Same lines, different verification: the id must still differ (tuple is in the id).
    one = allocate([family("a0")], policy(), verified_by=scope(path, seal))
    asserted = allocate([family("a0")], policy())
    assert one.lines == asserted.lines and one.allocation_id != asserted.allocation_id
    for change in (
        {"verification_basis": sa.CALLER_ASSERTED},
        {"verification_grant_ref": "generated-grant-2"},
        {"verification_ref": "0" * 64},
        {"verification_mode": "preview"},
        {"verification_scope": "0" * 64},
    ):
        assert sa.plan_allocation_id(replace(one, **change)) != one.allocation_id  # type: ignore[arg-type]


def test_caller_flags_cannot_lift_a_hold(tmp_path: Path) -> None:
    seal = tv_seal("a0", "b0")
    path = tv_ledger(tmp_path)
    tv_register(path, "a0", seal)
    sc = scope(path, seal)
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
    # A root whose case is not the one OPE-516 stored is not corroborated by the family id.
    moved_root = RootInput(
        "generated-root-a0", "generated-digest-a0", (R1,), 0, "generated-case-b0"
    )
    moved = allocate([family("a0", roots=(moved_root,))], policy(), verified_by=sc)
    assert zero_by_reason(moved)["a0"][0] == "verification_uncorroborated"
    # OPE-516 retracts the root: the family is no longer corroborated.
    tv.VariantLedger(path).retract_root("generated-root-a0", "a0", "generated-reopen")
    gone = allocate([family("a0")], policy(), verified_by=sc)
    assert gone.status == "hold"
    assert zero_by_reason(gone)["a0"][0] == "verification_uncorroborated"


def test_grant_budget_refuses_caller_asserted_plans(tmp_path: Path) -> None:
    seal = tv_seal("a0")
    path = tv_ledger(tmp_path)
    tv_register(path, "a0", seal)
    led = AllocationLedger()
    grant_budget(led, path, cap_units=5)
    asserted = allocate([family("a0")], policy())
    assert code_of(led.reserve, "generated-budget", asserted) == "verification_uncorroborated"
    other_scope = scope(path, seal, TV_GRANT_2)
    elsewhere = allocate([family("a0")], policy(), verified_by=other_scope)
    assert elsewhere.status == "hold"  # nothing registered under that grant
    sc = scope(path, seal)
    verified = allocate([family("a0")], policy(), verified_by=sc)
    # The worker-record label alone is not enough: the reserve must re-read OPE-516 itself.
    assert code_of(led.reserve, "generated-budget", verified) == "verification_uncorroborated"
    assert (
        code_of(led.reserve, "generated-budget", verified, verified_by=sc) == "grant_link_invalid"
    )
    assert led.reserve("generated-budget", verified, verified_by=sc, now=TV_NOW)["used_units"] == 1
    for fake in (object(), tv.VariantLedger(), ":memory:", tmp_path / "missing.db"):
        with pytest.raises(AllocationError) as err2:
            allocate([family("a0")], policy(), verified_by=replace(sc, variants=fake))  # type: ignore[arg-type]
        assert err2.value.code == "ledger_identity_invalid"


# G1 (audit batch 2, MEDIUM): the allocation spend cap is narrowed to OPE-516's grant headroom.
def test_grant_link_narrows_spend_cap_to_ope516_headroom(tmp_path: Path) -> None:
    seal = tv_seal("a0", "b0", "c0")
    path = tv_ledger(tmp_path)
    for fid in ("a0", "b0", "c0"):
        tv_register(path, fid, seal)
    tv_spend(path, "a0", seal, 4, "g1")
    variants = tv.VariantLedger(path)
    assert variants.spent(TV_GRANT.grant_ref) == 4
    assert sa.grant_headroom(TV_GRANT, variants, now=TV_NOW) == 6
    led = AllocationLedger()
    opened = grant_budget(led, path)
    assert opened["cap_spend"] == 6 and opened["grant_ref"] == TV_GRANT.grant_ref
    assert opened["authority"] == "ope516_grant_unverified"
    assert opened["grant_basis"] == "first_presentation_unverified"
    assert opened["authorized_spend"] is False
    assert grant_budget(led, path, "generated-budget-2", cap_spend=3)["cap_spend"] == 3
    # Never wider than the grant.
    assert grant_budget(led, path, "generated-budget-3", cap_spend=999)["cap_spend"] == 6
    # Caps are per budget; the grant-wide headroom binds across all of them at reserve.
    sc = scope(path, seal)
    six = allocate(
        [family("a0", recipes=(R1, R2, R3)), family("b0", recipes=(R1, R2, R3))],
        policy(per_family_cap=3, per_root_cap=3, total_units=6, spend_ceiling=6),
        verified_by=sc,
    )
    assert six.units == 6
    assert led.reserve("generated-budget", six, verified_by=sc, now=TV_NOW)["used_spend"] == 6
    one = allocate([family("c0")], policy(), verified_by=sc)
    assert (
        code_of(led.reserve, "generated-budget-2", one, verified_by=sc, now=TV_NOW)
        == "grant_headroom_exceeded"
    )
    assert led.status("generated-budget-2")["used_units"] == 0
    # A budget opened now sees the live reservations: nothing is left to open.
    assert code_of(grant_budget, led, path, "generated-budget-4") == "grant_unavailable"
    # Idempotent reopen keeps the stored cap.
    assert grant_budget(led, path)["cap_spend"] == 6
    assert code_of(grant_budget, led, path, unit_cost=2) == "budget_conflict"
    for grant, now in (
        (TV_GRANT, 1000),
        (replace(TV_GRANT, generation_authorized=False), TV_NOW),
        (replace(TV_GRANT, execution_location_approved=False), TV_NOW),
        (replace(TV_GRANT, budget=4), TV_NOW),  # 4 already spent
    ):
        WORKER_NOW["t"] = now  # expiry is the worker clock's call
        assert sa.grant_headroom(grant, variants, now=now) == 0
        fresh = AllocationLedger()
        assert code_of(grant_budget, fresh, path, grant=grant, now=now) == "grant_unavailable"
    WORKER_NOW["t"] = TV_NOW
    with pytest.raises(AllocationError, match="grant_link_invalid"):
        sa.grant_headroom(replace(TV_GRANT, budget=True), variants, now=TV_NOW)


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


# X* (cross-session review of a1bd58e, session_01CRHWYhLdLYHfPGfPzgxrPV): the reviewer's probes
# P1-P9 re-created as tests. Each failed against a1bd58e. Generated fixtures; file-backed
# OPE-516 and allocation ledgers in pytest's private tmp directory.
def world(
    tmp: Path, *fids: str, registered: tuple[str, ...] | None = None
) -> tuple[Path, tv.SplitSeal]:
    seal = tv_seal(*fids)
    path = tv_ledger(tmp)
    for fid in fids if registered is None else registered:
        tv_register(path, fid, seal)
    return path, seal


# X1 / P1: relabelling a caller-asserted plan as worker-verified does not spend on a grant.
def test_p1_relabelled_caller_plan_is_re_derived_and_refused(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "zz", registered=("a0",))
    led = AllocationLedger(tmp_path / "alloc.db")
    grant_budget(led, path)
    sc = scope(path, seal)
    p = allocate([family("zz")], policy())  # OPE-516 never registered zz
    probe = replace(
        p,
        verification_basis=sa.WORKER_RECORD,
        verification_grant_ref=TV_GRANT.grant_ref,
        verification_ref="0" * 64,
    )
    probe = replace(probe, allocation_id=sa.plan_allocation_id(probe))
    assert code_of(led.reserve, "generated-budget", probe, verified_by=sc, now=TV_NOW) == (
        "verification_uncorroborated"
    )
    # Copy every verification label from an honest plan: the lines are still re-derived.
    honest = allocate([family("a0")], policy(), verified_by=sc)
    forged = replace(
        p,
        verification_basis=honest.verification_basis,
        verification_grant_ref=honest.verification_grant_ref,
        verification_ref=honest.verification_ref,
        verification_mode=honest.verification_mode,
        verification_scope=honest.verification_scope,
    )
    forged = replace(forged, allocation_id=sa.plan_allocation_id(forged))
    with pytest.raises(AllocationError) as err:
        led.reserve("generated-budget", forged, verified_by=sc, now=TV_NOW)
    assert err.value.code == "verification_uncorroborated" and "root_not_live" in str(err.value)
    stale = replace(honest, verification_ref="0" * 64)
    stale = replace(stale, allocation_id=sa.plan_allocation_id(stale))
    assert code_of(led.reserve, "generated-budget", stale, verified_by=sc, now=TV_NOW) == (
        "verification_stale"
    )
    assert (
        led.status("generated-budget")["used_units"] == 0
        and led.live_lines("generated-budget") == []
    )
    assert led.reserve("generated-budget", honest, verified_by=sc, now=TV_NOW)["used_units"] == 1


# X2 / P2: a separate ledger with an invented seed and seal cannot spend the real grant.
def test_p2_other_ope516_ledger_cannot_spend_the_bound_grant(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0")
    led = AllocationLedger()
    grant_budget(led, path)
    other_dir = tmp_path / "other"
    other_dir.mkdir(mode=0o700)
    invented = tv_seal("zz")
    other = tv_ledger(other_dir)
    tv_register(other, "zz", invented)
    elsewhere = scope(other, invented)
    plan = allocate([family("zz")], policy(), verified_by=elsewhere)
    assert plan.verification_basis == sa.WORKER_RECORD and plan.units == 1  # planning only
    assert code_of(led.reserve, "generated-budget", plan, verified_by=elsewhere, now=TV_NOW) == (
        "ledger_identity_mismatch"
    )
    assert code_of(
        led.reserve, "generated-budget", plan, verified_by=scope(path, invented), now=TV_NOW
    ) == ("verification_scope_mismatch")
    assert code_of(grant_budget, led, other, "generated-budget-b") == "ledger_identity_mismatch"
    assert led.status("generated-budget")["used_units"] == 0


# X3 / P2b: an inflated grant or an unidentifiable ledger never sets the cap.
def test_p2b_inflated_grant_or_fresh_ledger_cannot_open_a_cap(tmp_path: Path) -> None:
    path, _ = world(tmp_path, "a0")
    led = AllocationLedger()
    big = replace(TV_GRANT, budget=10**9)
    assert code_of(grant_budget, led, path, grant=big, variants=tv.VariantLedger()) == (
        "ledger_identity_invalid"
    )
    assert grant_budget(led, path)["cap_spend"] == 10
    assert code_of(grant_budget, led, path, "generated-budget-2", grant=big) == "grant_conflict"
    assert code_of(grant_budget, led, path, grant=big) == "grant_conflict"
    assert led.status("generated-budget")["cap_spend"] == 10


# X4 / P3: a copied ledger is not the bound ledger; retraction in the original is what counts.
def test_p3_copied_ledger_is_refused_and_original_retraction_binds(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    led = AllocationLedger()
    grant_budget(led, path, unit_cost=1)
    copy = tmp_path / "copy.db"
    shutil.copyfile(path, copy)
    tv.VariantLedger(path).retract_root("generated-root-a0", "a0", "generated-reopen")
    copied = allocate([family("a0")], policy(), verified_by=scope(copy, seal))
    assert zero_by_reason(copied)["a0"] == ("eligible", 1)  # eligible against the copy only
    assert code_of(
        led.reserve, "generated-budget", copied, verified_by=scope(copy, seal), now=TV_NOW
    ) == ("ledger_identity_mismatch")
    assert code_of(
        led.reserve, "generated-budget", copied, verified_by=scope(path, seal), now=TV_NOW
    ) == ("verification_scope_mismatch")
    original = allocate([family("a0")], policy(), verified_by=scope(path, seal))
    assert zero_by_reason(original)["a0"] == ("verification_uncorroborated", 0)
    # Reserved before a retraction: commit re-reads OPE-516 and refuses.
    sc = scope(path, seal)
    b0 = allocate([family("b0")], policy(), verified_by=sc)
    led.reserve("generated-budget", b0, verified_by=sc, now=TV_NOW)
    tv.VariantLedger(path).retract_root("generated-root-b0", "b0", "generated-reopen")
    assert (
        code_of(led.commit, "generated-budget", b0.allocation_id, now=TV_NOW)
        == "verification_uncorroborated"
    )
    assert led.release("generated-budget", b0.allocation_id)["used_units"] == 0


# X5 / P4 + P4c: a forged seal is refused; every root must be a live OPE-516 root of its family.
def test_p4_forged_seal_and_unregistered_root_are_refused(tmp_path: Path) -> None:
    extra = (("generated-case-a0x", "a0", "training"),)
    seal = tv_seal("a0", extra=extra)
    path = tv_ledger(tmp_path)
    tv_register(path, "a0", seal)
    forged = tv.SplitSeal(
        seal.assignments, {**seal.cases, "generated-case-x": ("a0",)}, seal.digest
    )
    with pytest.raises(AllocationError) as err:
        allocate([family("a0")], policy(), verified_by=scope(path, forged))
    assert err.value.code == "verification_source_invalid" and "seal_digest" in str(err.value)
    resealed = tv_seal("a0", extra=(*extra, ("generated-case-x", "a0", "training")))
    other_seal = allocate([family("a0")], policy(), verified_by=scope(path, resealed))
    assert zero_by_reason(other_seal)["a0"] == ("verification_uncorroborated", 0)
    roots = (
        RootInput("generated-root-a0", "generated-digest-a0", (R1, R2), 0, "generated-case-a0"),
        RootInput("generated-root-a0x", "generated-digest-a0x", (R1, R2), 0, "generated-case-a0x"),
    )
    p = policy(per_family_cap=3, per_root_cap=2)
    caller = allocate([family("a0", roots=roots)], p)
    assert caller.units == 3  # the reviewer's P4c: three lines without the worker record
    worker = allocate([family("a0", roots=roots)], p, verified_by=scope(path, seal))
    assert zero_by_reason(worker)["a0"] == ("verification_uncorroborated", 0)
    assert worker.units == 0


# X6 (mutant: a sealed case listing other families): the case must be the one OPE-516 stored.
def test_root_case_sealed_under_several_families_is_not_corroborated(tmp_path: Path) -> None:
    shared = (
        ("generated-case-shared", "a0", "training"),
        ("generated-case-shared", "b0", "training"),
    )
    seal = tv_seal("a0", "b0", extra=shared)
    assert seal.cases["generated-case-shared"] == ("a0", "b0")
    path = tv_ledger(tmp_path)
    tv_register(path, "a0", seal)
    root = RootInput("generated-root-a0", "generated-digest-a0", (R1,), 0, "generated-case-shared")
    plan = allocate([family("a0", roots=(root,))], policy(), verified_by=scope(path, seal))
    assert zero_by_reason(plan)["a0"] == ("verification_uncorroborated", 0)
    assert allocate([family("a0")], policy(), verified_by=scope(path, seal)).units == 1


# X7 / P6: the grant headroom binds across budgets and re-reads OPE-516 spend at reserve.
def test_p6_reservations_across_budgets_cannot_exceed_grant_headroom(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0", "c0")
    led = AllocationLedger()
    for ref in ("generated-budget", "generated-budget-2", "generated-budget-3"):
        assert grant_budget(led, path, ref, unit_cost=5, cap_spend=10)["cap_spend"] == 10
    sc = scope(path, seal)
    pricey = policy(total_units=2, spend_ceiling=10, unit_cost=5)
    two = allocate([family("a0"), family("b0")], pricey, verified_by=sc)
    assert led.reserve("generated-budget", two, verified_by=sc, now=TV_NOW)["used_spend"] == 10
    one = allocate([family("c0")], pricey, verified_by=sc)
    for ref in ("generated-budget-2", "generated-budget-3"):
        assert code_of(led.reserve, ref, one, verified_by=sc, now=TV_NOW) == (
            "grant_headroom_exceeded"
        )
    # Recorded limit: OPE-516 cannot see these reservations and still accepts a cost-9 child.
    tv_spend(path, "a0", seal, 9, "p6")
    assert tv.VariantLedger(path).spent(TV_GRANT.grant_ref) == 9
    # This ledger re-reads OPE-516 spend: freeing the reservation leaves 10 - 9 = 1 < 5.
    led.release("generated-budget", two.allocation_id)
    fresh = allocate([family("c0")], pricey, verified_by=sc)
    assert code_of(led.reserve, "generated-budget-2", fresh, verified_by=sc, now=TV_NOW) == (
        "grant_headroom_exceeded"
    )
    WORKER_NOW["t"] = 1000
    assert code_of(led.reserve, "generated-budget-2", fresh, verified_by=sc, now=TV_NOW) == (
        "grant_expired"
    )


# X8 / P8: the ledger file's grant binding and caps cannot be changed by editing a column.
def test_p8_edited_ledger_file_is_refused_or_still_bounded(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    db_path = tmp_path / "alloc.db"
    grant_budget(AllocationLedger(db_path), path)

    def edit(sql: str, *args: object) -> None:
        db = sqlite3.connect(db_path)
        db.execute(sql, args)
        db.commit()
        db.close()

    def row() -> list[object]:
        db = sqlite3.connect(db_path)
        got = list(
            db.execute(
                "SELECT budget_ref, cap_units, cap_spend, unit_cost, cost_basis, grant_ref,"
                " ledger_id, grant_digest FROM budget"
            ).fetchone()
        )
        db.close()
        return got

    original = row()
    asserted = allocate([family("a0")], policy())
    edit("UPDATE budget SET grant_ref=''")
    assert code_of(AllocationLedger(db_path).reserve, "generated-budget", asserted) == (
        "ledger_integrity_failed"
    )
    edit("UPDATE budget SET grant_ref=?", original[5])
    edit("UPDATE budget SET cap_spend=?", 10**9)
    assert (
        code_of(AllocationLedger(db_path).status, "generated-budget") == "ledger_integrity_failed"
    )
    # A deliberate same-uid edit that recomputes the row digest: downgrade is still detected.
    plain = [*original[:5], "", "", ""]
    edit(
        "UPDATE budget SET grant_ref='', ledger_id='', grant_digest='', row_digest=?",
        sa._budget_digest(*plain),
    )
    assert code_of(AllocationLedger(db_path).reserve, "generated-budget", asserted) == (
        "ledger_integrity_failed"
    )
    # ...and a recomputed cap raise is still bounded by the pinned grant and OPE-516 spend.
    raised = [*original[:2], 10**9, *original[3:]]
    edit(
        "UPDATE budget SET grant_ref=?, ledger_id=?, grant_digest=?, cap_spend=?, row_digest=?",
        original[5],
        original[6],
        original[7],
        10**9,
        sa._budget_digest(*raised),
    )
    led = AllocationLedger(db_path)
    assert led.status("generated-budget")["cap_spend"] == 10**9
    sc = scope(path, seal)
    eleven = allocate(
        [
            family("a0", recipes=tuple(f"simulated:r{i}" for i in range(6))),
            family("b0", recipes=tuple(f"simulated:r{i}" for i in range(5))),
        ],
        policy(per_family_cap=6, per_root_cap=6, total_units=11, spend_ceiling=11),
        verified_by=sc,
    )
    assert eleven.units == 11
    assert code_of(led.reserve, "generated-budget", eleven, verified_by=sc, now=TV_NOW) == (
        "grant_headroom_exceeded"
    )
    # Schema drift: an extra or missing column is not silently accepted.
    edit("ALTER TABLE line ADD COLUMN generated_extra TEXT")
    assert code_of(AllocationLedger, db_path) == "ledger_schema_outdated"


# X9 / P9: every stored or read plan field is covered by the id; ROI is never claimed.
@pytest.mark.parametrize(
    "change",
    [
        {"roi_claim": True},
        {"ranking_basis": "cost_based"},
        {"requests": ({"kind": "generated-forged"},)},
        {"cell_scores": ((CELL_A, "999/1"),)},
        {"stop_reasons": ("generated-forged",)},
        {"decisions": ()},
        {"detail": {"budget_units": 999}},
        {"hold_reason": "generated-forged"},
        {"policy_digest": "0" * 64},
        {"verification_mode": "training"},
    ],
)
def test_p9_first_reserve_cannot_store_fabricated_fields(change: dict[str, object]) -> None:
    led = ledger(units=5, spend=5)
    plan = plan_of("p", 2)
    fabricated = replace(plan, **change)  # type: ignore[arg-type]
    assert code_of(led.reserve, "generated-budget", fabricated) == sa.BINDING_MISMATCH
    assert led.status("generated-budget")["used_units"] == 0


def test_roi_claim_is_refused_even_with_a_recomputed_id() -> None:
    led = ledger(units=5, spend=5)
    claim = replace(plan_of("p", 2), roi_claim=True)
    claim = replace(claim, allocation_id=sa.plan_allocation_id(claim))
    assert code_of(led.reserve, "generated-budget", claim) == "plan_invalid"


# X10 (finding 5): a preview-scope plan records its mode and never reserves on a grant budget.
def test_preview_scope_is_recorded_and_cannot_reserve_on_a_grant(tmp_path: Path) -> None:
    seal = tv_seal("a0")
    path = tv_ledger(tmp_path)
    tv_register(path, "a0", seal, mode="preview")
    tv_register(path, "a0", seal)
    led = AllocationLedger()
    grant_budget(led, path)
    pv = scope(path, seal, mode="preview")
    preview = allocate([family("a0")], policy(), verified_by=pv)
    assert preview.verification_mode == "preview" and preview.units == 1
    assert preview.as_record()["verification_mode"] == "preview"
    assert code_of(led.reserve, "generated-budget", preview, verified_by=pv, now=TV_NOW) == (
        "verification_preview_only"
    )
    training = allocate([family("a0")], policy(), verified_by=scope(path, seal))
    assert code_of(led.reserve, "generated-budget", training, verified_by=pv, now=TV_NOW) == (
        "verification_preview_only"
    )
    assert led.status("generated-budget")["used_units"] == 0


# X11 (finding 8): values beyond sqlite int64 are refused cleanly, never a raw OverflowError.
def test_values_beyond_int64_are_refused_cleanly(tmp_path: Path) -> None:
    huge = 2**63
    for kw in ({"unit_cost": huge}, {"spend_ceiling": huge}, {"total_units": huge}):
        assert code_of(allocate, [family("a")], policy(**kw)) == "policy_invalid"
    assert code_of(allocate, [family("a")], policy(min_yield_milli=huge)) == "policy_invalid"
    assert code_of(allocate, [family("a")], policy(critical_cells=((CELL_A, huge),))) == (
        "policy_invalid"
    )
    led = AllocationLedger()
    for kw in ({"cap_spend": huge}, {"cap_units": huge}, {"unit_cost": huge}):
        args = {"cap_units": 1, "cap_spend": 1, "unit_cost": 1, **kw}
        assert code_of(led.open_budget, "generated-budget", **args) == "budget_invalid"
    path, _ = world(tmp_path, "a0")
    assert code_of(grant_budget, led, path, grant=replace(TV_GRANT, budget=huge)) == (
        "grant_link_invalid"
    )
    assert code_of(grant_budget, led, path, cap_spend=huge) == "budget_invalid"
    top = sa.INT64_MAX
    led.open_budget("generated-top", cap_units=1, cap_spend=top, unit_cost=top)
    plan = allocate([family("a")], policy(total_units=1, spend_ceiling=top, unit_cost=top))
    assert led.reserve("generated-top", plan)["used_spend"] == top


# X12 (finding 9): a hand-built or homoglyph cohort is refused, not trusted.
def test_hand_built_or_homoglyph_cohort_is_refused() -> None:
    fake = sa.ConfirmationCohort(
        cutoff=50,
        maturity_window=10,
        now=100,
        independent=(),
        repeat_entity=(),
        immature=(),
        excluded=(),
        held_out=frozenset(),
        frame_cases=frozenset({"generated-case-a0"}),
        cohort_digest="0" * 64,
    )
    assert code_of(allocate, [family("a0")], policy(), holdout=fake) == "holdout_invalid"
    real = cohort()
    assert allocate([family("a0")], policy(), holdout=real).units == 1
    for change in (
        {"cohort_digest": "0" * 64},
        {"held_out": real.held_out - {"generated-late-young"}},
        {"held_out": frozenset()},
        {"independent": ("generated-unheld",)},
    ):
        bad = replace(real, **change)  # type: ignore[arg-type]
        assert code_of(allocate, [family("a0")], policy(), holdout=bad) == "holdout_invalid"
    emptied = replace(real, held_out=frozenset(), independent=(), repeat_entity=(), immature=())
    emptied = replace(emptied, cohort_digest=sa._cohort_digest(emptied))
    assert code_of(allocate, [family("a0")], policy(), holdout=emptied) == "holdout_invalid"
    cyrillic = case("generated-сase-a0", "generated-entity-9", 60)  # Cyrillic es in "case"
    with pytest.raises(AllocationError) as err:
        sa.confirmation_cohort(frame(), [cyrillic], cutoff=50, maturity_window=10, now=100)
    assert err.value.code == "cohort_invalid" and "non_ascii_identity" in str(err.value)
    # NFKC-folded (fullwidth) spellings are one identity, so leakage is still caught.
    wide = case(
        "generated-late-w", "generated-entity-9", 60, "ｇenerated-content-generated-case-b0"
    )
    folded = sa.confirmation_cohort(frame(), [wide], cutoff=50, maturity_window=10, now=100)
    assert dict(folded.excluded) == {"generated-late-w": "leakage_duplicate"}


# X13 (mutant: unnormalised held-out check): a respelled held-out case is still held out.
def test_held_out_check_normalises_case_refs() -> None:
    held = cohort()
    respelled = RootInput("generated-root-r", "generated-d-r", (R1,), 0, " GENERATED-LATE-YOUNG")
    plan = allocate([family("r", roots=(respelled,)), family("a0")], policy(), holdout=held)
    assert zero_by_reason(plan)["r"] == ("confirmation_holdout", 0)


# X14 (mutant: grant budget ignoring a verification_grant_ref mismatch).
def test_grant_budget_refuses_a_plan_verified_under_another_grant(tmp_path: Path) -> None:
    seal = tv_seal("a0")
    path = tv_ledger(tmp_path)
    tv_register(path, "a0", seal)
    tv_register(path, "a0", seal, grant=TV_GRANT_2)
    led = AllocationLedger()
    grant_budget(led, path)
    other = scope(path, seal, TV_GRANT_2)
    plan = allocate([family("a0")], policy(), verified_by=other)
    assert plan.units == 1 and plan.verification_grant_ref == TV_GRANT_2.grant_ref
    for verified_by in (other, scope(path, seal)):
        assert code_of(
            led.reserve, "generated-budget", plan, verified_by=verified_by, now=TV_NOW
        ) == ("verification_grant_mismatch")
    relabelled = replace(plan, verification_grant_ref=TV_GRANT.grant_ref)
    relabelled = replace(relabelled, allocation_id=sa.plan_allocation_id(relabelled))
    assert code_of(led.reserve, "generated-budget", relabelled, verified_by=other, now=TV_NOW) == (
        "verification_grant_mismatch"
    )


# X15 (mutant: reopening a grant budget ignoring grant_ref).
def test_reopening_a_grant_budget_under_another_grant_conflicts(tmp_path: Path) -> None:
    seal = tv_seal("a0")
    path = tv_ledger(tmp_path)
    tv_register(path, "a0", seal)
    led = AllocationLedger()
    grant_budget(led, path)
    assert code_of(grant_budget, led, path, grant=TV_GRANT_2) == "budget_conflict"
    assert led.status("generated-budget")["grant_ref"] == TV_GRANT.grant_ref
    led.open_budget("generated-plain", cap_units=1, cap_spend=1, unit_cost=1)
    assert code_of(grant_budget, led, path, "generated-plain") == "budget_conflict"
    assert code_of(
        led.open_budget, "generated-budget", cap_units=50, cap_spend=10, unit_cost=1
    ) == ("budget_conflict")


# X16 (reviewer ruling): planning budgets expose that their lines are planning records only.
def test_status_and_live_lines_expose_authority_and_verification_basis(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    led = AllocationLedger()
    led.open_budget("generated-plain", cap_units=5, cap_spend=5, unit_cost=1)
    led.reserve("generated-plain", allocate([family("p0")], policy()))
    plain = led.status("generated-plain")
    assert plain["authority"] == "planning_record_only" and plain["grant_ref"] == ""
    assert plain["verification_required"] == "none"
    assert plain["lines_by_verification_basis"] == {sa.CALLER_ASSERTED: 1}
    assert led.live_lines("generated-plain") == [
        sa.LiveLine(
            "p0", "generated-root-p0", R1, "", sa.CALLER_ASSERTED, sa.PLANNING_RECORD, "none"
        )
    ]
    grant_budget(led, path)
    sc = scope(path, seal)
    led.reserve(
        "generated-budget",
        allocate([family("a0")], policy(), verified_by=sc),
        verified_by=sc,
        now=TV_NOW,
    )
    bound = led.status("generated-budget")
    assert bound["authority"] == sa.GRANT_AUTHORITY and bound["grant_ref"] == TV_GRANT.grant_ref
    assert bound["verification_required"] == sa.WORKER_RECORD and len(bound["ledger_id"]) == 64
    assert led.live_lines("generated-budget") == [
        sa.LiveLine(
            "a0",
            "generated-root-a0",
            R1,
            TV_GRANT.grant_ref,
            sa.WORKER_RECORD,
            sa.GRANT_AUTHORITY,
            sa.GRANT_BASIS_UNVERIFIED,
        )
    ]
    # A worker-record plan on a planning budget is still only a planning record.
    led.open_budget("generated-plain-2", cap_units=5, cap_spend=5, unit_cost=1)
    led.reserve("generated-plain-2", allocate([family("b0")], policy(), verified_by=sc))
    assert led.live_lines("generated-plain-2")[0].authority == sa.PLANNING_RECORD


# X17: the OPE-516 ledger must be a privately held, single-link regular file.
def test_ope516_ledger_custody_is_checked(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0")
    link = tmp_path / "link.db"
    link.symlink_to(path)
    hard = tmp_path / "hard.db"
    for bad in (link,):
        assert code_of(allocate, [family("a0")], policy(), verified_by=scope(bad, seal)) == (
            "ledger_identity_invalid"
        )
    os.link(path, hard)
    assert code_of(allocate, [family("a0")], policy(), verified_by=scope(path, seal)) == (
        "ledger_identity_invalid"
    )
    hard.unlink()
    path.chmod(0o664)
    assert code_of(allocate, [family("a0")], policy(), verified_by=scope(path, seal)) == (
        "ledger_identity_invalid"
    )
    path.chmod(0o600)
    assert allocate([family("a0")], policy(), verified_by=scope(path, seal)).units == 1


# X18: a grant-bound reserve needs the scope and the time; commit re-reads OPE-516.
def test_grant_reserve_needs_scope_and_now_and_commit_rechecks(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    db_path = tmp_path / "alloc.db"
    led = AllocationLedger(db_path)
    grant_budget(led, path)
    sc = scope(path, seal)
    plan = allocate([family("a0")], policy(), verified_by=sc)
    assert (
        code_of(led.reserve, "generated-budget", plan, now=TV_NOW) == "verification_uncorroborated"
    )
    assert code_of(led.reserve, "generated-budget", plan, verified_by=sc, now=True) == (
        "grant_link_invalid"
    )
    assert led.reserve("generated-budget", plan, verified_by=sc, now=TV_NOW)["replayed"] is False
    # Replays are no-ops even after OPE-516 has moved on.
    tv_spend(path, "b0", seal, 1, "x18")
    assert led.reserve("generated-budget", plan, verified_by=sc, now=TV_NOW)["replayed"] is True
    restarted = AllocationLedger(db_path)
    assert code_of(restarted.commit, "generated-budget", plan.allocation_id) == (
        "grant_link_invalid"
    )
    assert restarted.commit("generated-budget", plan.allocation_id, now=TV_NOW)["used_units"] == 1
    assert restarted.live_lines("generated-budget")[0].authority == sa.GRANT_AUTHORITY


# X19 (PR #225 4f04a9b): an admitted child of a stale fit experiment covers nothing.
def test_covered_from_children_skips_fit_stale_admitted_children() -> None:
    recipe = {"recipe_id": R1}
    children = [
        {"disposition": "admitted", "root_ref": "generated-root-a", "recipe": recipe},
        {
            "disposition": "admitted",
            "root_ref": "generated-root-b",
            "recipe": recipe,
            "fit_stale": {"reservation_id": "fit-generated", "reason": "root_generated-reopen"},
        },
        {
            "disposition": "admitted",
            "root_ref": "generated-root-c",
            "recipe": recipe,
            "fit_stale": None,
        },
    ]
    assert covered_from_children(children) == {
        "generated-root-a": {R1: 1},
        "generated-root-c": {R1: 1},
    }


# X20 (PR #225 4f04a9b): a grant-bound budget binds to OPE-516's own mandate-store pin.
_PIN_SQL = (
    "CREATE TABLE IF NOT EXISTS authority_pin (slot INTEGER PRIMARY KEY CHECK (slot = 1),"
    " authority_identity TEXT NOT NULL, ledger_identity TEXT NOT NULL, record TEXT NOT NULL)"
)


def _set_fit_pin(path: Path, authority: str | None) -> None:
    """Simulate OPE-516 pinning (or a same-uid edit replacing) its mandate-store identity."""
    db = sqlite3.connect(path)
    db.execute(_PIN_SQL)
    db.execute("DELETE FROM authority_pin")
    if authority is not None:
        db.execute(
            "INSERT INTO authority_pin VALUES (1, ?, 'generated-ledger-identity', '{}')",
            (authority,),
        )
    db.commit()
    db.close()


def test_grant_budget_binds_to_the_ope516_mandate_store_pin(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0", "c0")
    led = AllocationLedger()
    grant_budget(led, path)  # no pin yet: the store is bound when OPE-516 first pins one
    sc = scope(path, seal)
    _set_fit_pin(path, "generated-store-1")
    a0 = allocate([family("a0")], policy(), verified_by=sc)
    led.reserve("generated-budget", a0, verified_by=sc, now=TV_NOW)
    _set_fit_pin(path, "generated-store-2")  # substituted mandate store
    b0 = allocate([family("b0")], policy(), verified_by=sc)
    with pytest.raises(AllocationError) as err:
        led.reserve("generated-budget", b0, verified_by=sc, now=TV_NOW)
    assert err.value.code == "ledger_identity_mismatch" and "fit_authority_pin" in str(err.value)
    assert code_of(led.commit, "generated-budget", a0.allocation_id, now=TV_NOW) == (
        "ledger_identity_mismatch"
    )
    _set_fit_pin(path, None)  # pin removed
    c0 = allocate([family("c0")], policy(), verified_by=sc)
    assert code_of(led.reserve, "generated-budget", c0, verified_by=sc, now=TV_NOW) == (
        "ledger_identity_mismatch"
    )
    _set_fit_pin(path, "generated-store-1")
    c0 = allocate([family("c0")], policy(), verified_by=sc)
    assert led.reserve("generated-budget", c0, verified_by=sc, now=TV_NOW)["used_units"] == 2
    assert led.commit("generated-budget", a0.allocation_id, now=TV_NOW)["used_units"] == 2
    # A pin present at open is bound at open.
    fresh = AllocationLedger()
    grant_budget(fresh, path)
    _set_fit_pin(path, "generated-store-3")
    assert code_of(grant_budget, fresh, path, "generated-budget-2") == "ledger_identity_mismatch"


# X21 (mutation survivors M19/M36): grant-ness cannot be dropped by deleting its membership row
# or by a recomputed partial downgrade of the grant columns.
def test_grant_binding_rows_are_cross_checked(tmp_path: Path) -> None:
    path, _ = world(tmp_path, "a0")
    db_path = tmp_path / "alloc.db"
    grant_budget(AllocationLedger(db_path), path)
    db = sqlite3.connect(db_path)
    fields = list(
        db.execute(
            "SELECT budget_ref, cap_units, cap_spend, unit_cost, cost_basis, grant_ref, ledger_id,"
            " grant_digest FROM budget"
        ).fetchone()
    )
    db.execute("DELETE FROM grant_budget")
    db.commit()
    assert code_of(AllocationLedger(db_path).status, "generated-budget") == (
        "ledger_integrity_failed"
    )
    partial = [*fields[:5], "", *fields[6:]]  # grant_ref blanked, digest recomputed
    db.execute("UPDATE budget SET grant_ref='', row_digest=?", (sa._budget_digest(*partial),))
    db.commit()
    db.close()
    asserted = allocate([family("p0")], policy())
    assert code_of(AllocationLedger(db_path).reserve, "generated-budget", asserted) == (
        "ledger_integrity_failed"
    )


# X22 (mutation survivor M35): OPE-516's manifest and its own roots must agree.
def test_manifest_that_disagrees_with_ope516_roots_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seal = world(tmp_path, "a0")
    real_manifest = tv.VariantLedger.manifest

    def widened(self: tv.VariantLedger, **kw: object) -> dict:
        body = real_manifest(self, **kw)  # type: ignore[arg-type]
        return {**body, "independent_families": [*body["independent_families"], "zz"]}

    monkeypatch.setattr(tv.VariantLedger, "manifest", widened)
    with pytest.raises(AllocationError) as err:
        allocate([family("a0"), family("zz")], policy(), verified_by=scope(path, seal))
    assert err.value.code == "verification_source_invalid"
    assert "manifest_disagrees_with_roots" in str(err.value)


# X23 (mutation survivor M07+M09): a family cannot claim another family's live root and case.
def test_family_cannot_claim_another_familys_live_root(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    sc = scope(path, seal)
    stolen = RootInput("generated-root-b0", "generated-digest-x", (R1,), 0, "generated-case-b0")
    plan = allocate([family("a0", roots=(stolen,))], policy(), verified_by=sc)
    assert zero_by_reason(plan)["a0"] == ("verification_uncorroborated", 0)
    led = AllocationLedger()
    grant_budget(led, path)
    honest = allocate([family("b0")], policy(), verified_by=sc)
    relabelled = replace(honest, lines=(replace(honest.lines[0], family_id="a0"),))
    relabelled = replace(relabelled, allocation_id=sa.plan_allocation_id(relabelled))
    with pytest.raises(AllocationError) as err:
        led.reserve("generated-budget", relabelled, verified_by=sc, now=TV_NOW)
    assert err.value.code == "verification_uncorroborated" and "root_not_live" in str(err.value)


# Y* (re-review of 03571f1, same reviewer). Each failed against 03571f1.
# Y1 / A (P2d, P2): first-presentation grants are labelled unverified, never authorized.
def test_p2d_first_presented_inflated_grant_is_labelled_unverified(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0")
    led = AllocationLedger()  # a new ledger file accepts any first grant
    opened = grant_budget(led, path, grant=replace(TV_GRANT, budget=10**9))
    assert opened["cap_spend"] == 10**9  # recorded limit: the first presentation is trusted
    assert opened["authority"] == "ope516_grant_unverified"
    assert opened["grant_basis"] == "first_presentation_unverified"
    assert opened["authorized_spend"] is False
    sc = scope(path, seal)
    got = led.reserve(
        "generated-budget",
        allocate([family("a0")], policy(), verified_by=sc),
        verified_by=sc,
        now=TV_NOW,
    )
    assert (
        got["grant_basis"] == "first_presentation_unverified" and got["authorized_spend"] is False
    )
    (line,) = led.live_lines("generated-budget")
    assert line.authority == "ope516_grant_unverified"
    assert line.grant_basis == "first_presentation_unverified"


def test_p2_fabricated_ledger_and_forged_grant_are_labelled_unverified(tmp_path: Path) -> None:
    fake_dir = tmp_path / "fabricated"
    fake_dir.mkdir(mode=0o700)
    forged = replace(TV_GRANT, grant_ref="generated-grant-forged", budget=10**6)
    seal = tv_seal("zz")
    fake = tv_ledger(fake_dir)
    tv_register(fake, "zz", seal, grant=forged)
    led = AllocationLedger()
    grant_budget(led, fake, grant=forged)
    sc = scope(fake, seal, forged)
    led.reserve(
        "generated-budget",
        allocate([family("zz")], policy(), verified_by=sc),
        verified_by=sc,
        now=TV_NOW,
    )
    assert led.status("generated-budget")["authority"] == "ope516_grant_unverified"
    assert [(x.authority, x.grant_basis) for x in led.live_lines("generated-budget")] == [
        ("ope516_grant_unverified", "first_presentation_unverified")
    ]
    plain = AllocationLedger()
    plain.open_budget("generated-plain", cap_units=1, cap_spend=1, unit_cost=1)
    status = plain.status("generated-plain")
    assert status["grant_basis"] == "none" and status["authorized_spend"] is False


# Y2 / B (P7): commit re-checks grant headroom against current OPE-516 spend, and expiry.
def test_p7_commit_rechecks_headroom_after_ope516_spend(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    led = AllocationLedger()
    grant_budget(led, path)
    sc = scope(path, seal)
    eight = allocate(
        [family("a0", recipes=tuple(f"simulated:r{i}" for i in range(8)))],
        policy(per_family_cap=8, per_root_cap=8, total_units=8, spend_ceiling=8),
        verified_by=sc,
    )
    assert led.reserve("generated-budget", eight, verified_by=sc, now=TV_NOW)["used_spend"] == 8
    tv_spend(path, "b0", seal, 9, "p7")  # OPE-516 cannot see the reservation and admits 9
    assert tv.VariantLedger(path).spent(TV_GRANT.grant_ref) == 9
    assert code_of(led.commit, "generated-budget", eight.allocation_id, now=TV_NOW) == (
        "grant_headroom_exceeded"
    )
    status = led.status("generated-budget")
    assert status["used_spend"] == 8
    assert led.live_lines("generated-budget")[0].authority == sa.GRANT_AUTHORITY


def test_commit_refuses_an_expired_grant(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0")
    led = AllocationLedger()
    grant_budget(led, path)
    sc = scope(path, seal)
    plan = allocate([family("a0")], policy(), verified_by=sc)
    led.reserve("generated-budget", plan, verified_by=sc, now=TV_NOW)
    WORKER_NOW["t"] = 1000
    assert code_of(led.commit, "generated-budget", plan.allocation_id, now=1000) == "grant_expired"
    assert code_of(led.commit, "generated-budget", plan.allocation_id, now=True) == (
        "grant_link_invalid"
    )
    # A back-dated caller time does not re-open the grant: the worker clock decides.
    assert code_of(led.commit, "generated-budget", plan.allocation_id, now=999) == "grant_expired"


# Y3 / D: ranking basis is re-derived from the bound cost basis; requests from the decisions.
def test_ranking_basis_and_requests_cannot_be_fabricated_with_a_recomputed_id() -> None:
    led = ledger(units=10, spend=10)

    def recomputed(plan: sa.AllocationPlan, **change: object) -> sa.AllocationPlan:
        changed = replace(plan, **change)  # type: ignore[arg-type]
        return replace(changed, allocation_id=sa.plan_allocation_id(changed))

    plan = plan_of("p", 2)
    assert code_of(
        led.reserve, "generated-budget", recomputed(plan, ranking_basis="cost_based")
    ) == (sa.BINDING_MISMATCH)
    forged = recomputed(plan, requests=({"kind": "owner_decision", "family_id": "p0"},))
    assert code_of(led.reserve, "generated-budget", forged) == "plan_invalid"
    dropped = allocate(
        [
            family("c", CELL_B, policy_conflict=True),
            family("x0"),
            family("x1"),
            family("lone", CELL_B),
        ],
        policy(min_support=2),
    )
    assert {r["kind"] for r in dropped.requests} == {"owner_decision", "real_label_request"}
    assert code_of(led.reserve, "generated-budget", recomputed(dropped, requests=())) == (
        "plan_invalid"
    )
    assert led.reserve("generated-budget", dropped)["used_units"] == 2
    measured = AllocationLedger()
    measured.open_budget(
        "generated-m", cap_units=5, cap_spend=5, unit_cost=1, cost_basis="measured"
    )
    assert measured.status("generated-m")["cost_basis"] == "measured"
    assert code_of(measured.reserve, "generated-m", plan) == sa.BINDING_MISMATCH
    assert (
        measured.reserve("generated-m", plan_of("m", 1, cost_basis="measured"))["used_units"] == 1
    )
    assert code_of(measured.open_budget, "generated-m", cap_units=5, cap_spend=5, unit_cost=1) == (
        "budget_conflict"
    )
    assert code_of(
        led.open_budget, "generated-x", cap_units=1, cap_spend=1, unit_cost=1, cost_basis="x"
    ) == ("budget_invalid")


# Y4 / E: a planning reservation never blocks the matching grant-bound line.
def test_planning_line_does_not_block_grant_bound_line(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0")
    led = AllocationLedger()
    led.open_budget("generated-plain", cap_units=5, cap_spend=5, unit_cost=1)
    led.open_budget("generated-plain-2", cap_units=5, cap_spend=5, unit_cost=1)
    grant_budget(led, path)
    grant_budget(led, path, "generated-budget-2")
    led.reserve("generated-plain", allocate([family("a0")], policy()))
    sc = scope(path, seal)
    plan = allocate([family("a0")], policy(), verified_by=sc)
    assert led.reserve("generated-budget", plan, verified_by=sc, now=TV_NOW)["used_units"] == 1
    # Within one authority class a root/recipe is still reserved at most once.
    assert code_of(led.reserve, "generated-budget-2", plan, verified_by=sc, now=TV_NOW) == (
        "line_already_allocated"
    )
    assert code_of(led.reserve, "generated-plain-2", allocate([family("a0")], policy())) == (
        "line_already_allocated"
    )


# Y5 (mutation survivors N08, N10): file and directory custody are each checked.
def test_custody_checks_uid_and_directory_mode(tmp_path: Path) -> None:
    from types import SimpleNamespace

    uid = os.getuid()
    good_file = SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=uid, st_nlink=1)
    good_dir = SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, st_uid=uid, st_nlink=2)
    assert sa._private(good_file, uid, directory=False)
    assert sa._private(good_dir, uid, directory=True)
    for bad, is_dir in (
        (replace_ns(good_file, st_uid=uid + 1), False),
        (replace_ns(good_dir, st_uid=uid + 1), True),
        (replace_ns(good_dir, st_mode=stat.S_IFDIR | 0o770), True),
        (replace_ns(good_dir, st_mode=stat.S_IFDIR | 0o702), True),
        (replace_ns(good_file, st_nlink=2), False),
        (good_dir, False),
        (good_file, True),
    ):
        assert not sa._private(bad, uid, directory=is_dir)
    path, seal = world(tmp_path, "a0")
    tmp_path.chmod(0o775)
    assert code_of(allocate, [family("a0")], policy(), verified_by=scope(path, seal)) == (
        "ledger_identity_invalid"
    )
    tmp_path.chmod(0o700)
    assert allocate([family("a0")], policy(), verified_by=scope(path, seal)).units == 1


def replace_ns(ns: object, **kw: object) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(**{**vars(ns), **kw})


# Y6 (mutation survivor N11): a ledger replaced while it is being read is refused.
def test_ledger_replaced_during_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seal = world(tmp_path, "a0")
    real_manifest = tv.VariantLedger.manifest

    def swapping(self: tv.VariantLedger, **kw: object) -> dict:
        body = real_manifest(self, **kw)  # type: ignore[arg-type]
        twin = tmp_path / "twin.db"
        shutil.copyfile(path, twin)
        os.replace(twin, path)  # same path, new inode
        return body

    monkeypatch.setattr(tv.VariantLedger, "manifest", swapping)
    with pytest.raises(AllocationError) as err:
        allocate([family("a0")], policy(), verified_by=scope(path, seal))
    assert err.value.code == "ledger_identity_mismatch" and "changed_during_read" in str(err.value)


# Y7 (mutation survivor N21): a one-column edit of the grant pin is caught by its row digest.
def test_grant_pin_fit_pin_edit_is_refused(tmp_path: Path) -> None:
    path, _ = world(tmp_path, "a0")
    db_path = tmp_path / "alloc.db"
    grant_budget(AllocationLedger(db_path), path)
    db = sqlite3.connect(db_path)
    db.execute("UPDATE grant_pin SET fit_pin='generated-forged-pin'")
    db.commit()
    db.close()
    assert code_of(AllocationLedger(db_path).status, "generated-budget") == (
        "ledger_integrity_failed"
    )


# Z* (delta re-review of 69e758c, same reviewer).
# Z1: expiry is judged on the worker clock; the caller's ``now`` can only narrow the window.
def test_caller_clock_cannot_go_back_or_revive_an_expired_grant(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    led = AllocationLedger()
    grant_budget(led, path)
    sc = scope(path, seal)
    plan = allocate([family("a0")], policy(), verified_by=sc)
    WORKER_NOW["t"] = 999
    led.reserve("generated-budget", plan, verified_by=sc, now=999)
    WORKER_NOW["t"] = 1000  # the real expiry passes on the worker's own clock
    # Repro 1: a back-dated commit (reserve at 999, commit at 5) is judged on the worker clock.
    assert code_of(led.commit, "generated-budget", plan.allocation_id, now=5) == "grant_expired"
    # Repro 2: reserving with now=0 after the real expiry is refused too.
    other = allocate([family("b0")], policy(), verified_by=sc)
    assert code_of(led.reserve, "generated-budget", other, verified_by=sc, now=0) == (
        "grant_expired"
    )
    # A future caller time cannot extend the grant either (and beyond the tolerance it is
    # refused outright).
    assert code_of(led.reserve, "generated-budget", other, verified_by=sc, now=1200) == (
        "grant_expired"
    )
    assert code_of(led.reserve, "generated-budget", other, verified_by=sc, now=10**9) == (
        "clock_skew_refused"
    )
    assert sa.grant_headroom(TV_GRANT, tv.VariantLedger(path), now=0) == 0
    assert code_of(grant_budget, AllocationLedger(), path, now=0) == "grant_unavailable"


def test_caller_time_past_expiry_still_ends_the_grant(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0")
    led = AllocationLedger(tmp_path / "alloc.db")
    grant_budget(led, path)
    sc = scope(path, seal)
    plan = allocate([family("a0")], policy(), verified_by=sc)
    WORKER_NOW["t"] = 800
    led.reserve("generated-budget", plan, verified_by=sc, now=800)
    # Worker clock still before expiry; the caller, within tolerance, says it is past: the
    # caller narrows the window.
    assert code_of(led.commit, "generated-budget", plan.allocation_id, now=1000) == "grant_expired"
    # A refusal stores nothing, so an honest caller time still commits.
    assert led.commit("generated-budget", plan.allocation_id, now=800)["used_units"] == 1


# Z2: a committed line whose child OPE-516 has charged is not counted a second time.
FULFIL = (TV_RECIPE.recipe_id,)


def ten_families(tmp: Path) -> tuple[Path, tv.SplitSeal]:
    return world(tmp, *(f"a{i}" for i in range(10)), "u0")


def five(sc: sa.VerificationScope, start: int) -> sa.AllocationPlan:
    fams = [family(f"a{i}", recipes=FULFIL) for i in range(start, start + 5)]
    return allocate(fams, policy(total_units=5, spend_ceiling=5), verified_by=sc)


def test_generated_lines_are_not_double_counted_after_commit(tmp_path: Path) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, cap_spend=5)
    grant_budget(led, path, "generated-budget-2", cap_spend=5)
    sc = scope(path, seal)
    b1, b2 = five(sc, 0), five(sc, 5)
    led.reserve("generated-budget", b1, verified_by=sc, now=TV_NOW)
    led.reserve("generated-budget-2", b2, verified_by=sc, now=TV_NOW)
    led.commit("generated-budget", b1.allocation_id, now=TV_NOW)
    for i in range(5):  # generate b1's five lines through OPE-516 (same root and recipe)
        tv_spend(path, f"a{i}", seal, 1, f"b1-{i}")
    assert tv.VariantLedger(path).spent(TV_GRANT.grant_ref) == 5
    # The true total is 5 spent + 5 reserved = 10 of 10: b2's commit must succeed.
    assert led.commit("generated-budget-2", b2.allocation_id, now=TV_NOW)["used_spend"] == 5
    for i in range(5, 10):
        tv_spend(path, f"a{i}", seal, 1, f"b2-{i}")
    # All ten are now spent in OPE-516 and fulfilled here: nothing is left on the grant.
    assert code_of(grant_budget, led, path, "generated-budget-3") == "grant_unavailable"


def test_child_made_before_commit_does_not_fulfil_the_line(tmp_path: Path) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, cap_spend=5)
    grant_budget(led, path, "generated-budget-2", cap_spend=5)
    sc = scope(path, seal)
    b1 = five(sc, 0)
    led.reserve("generated-budget", b1, verified_by=sc, now=TV_NOW)
    tv_spend(path, "a0", seal, 1, "early")  # charged before b1 commits: not b1's fulfilment
    led.commit("generated-budget", b1.allocation_id, now=TV_NOW)  # 5 live + 1 spent <= 10
    b2 = five(sc, 5)
    # Counted: 5 (b1, a0's line still unfulfilled) + 5 (b2) + 1 spent = 11 > 10.
    assert code_of(led.reserve, "generated-budget-2", b2, verified_by=sc, now=TV_NOW) == (
        "grant_headroom_exceeded"
    )


# Z3 (mutant D11): commit counts live spend across the whole grant, not just its own budget.
def test_commit_counts_live_spend_across_every_budget_of_the_grant(tmp_path: Path) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, cap_spend=5)
    grant_budget(led, path, "generated-budget-2", cap_spend=5)
    sc = scope(path, seal)
    b1, b2 = five(sc, 0), five(sc, 5)
    led.reserve("generated-budget", b1, verified_by=sc, now=TV_NOW)
    led.reserve("generated-budget-2", b2, verified_by=sc, now=TV_NOW)
    tv_spend(path, "u0", seal, 1, "unrelated")  # OPE-516 spend no allocation line accounts for
    # Per budget: 5 + 1 <= 10. Across the grant: 10 + 1 > 10.
    assert code_of(led.commit, "generated-budget", b1.allocation_id, now=TV_NOW) == (
        "grant_headroom_exceeded"
    )


# Z4: grant_headroom's docstring does not call a caller-supplied grant authorized.
def test_grant_headroom_docstring_says_unverified() -> None:
    doc = sa.grant_headroom.__doc__ or ""
    assert "unverified" in doc and "still authorizes" not in doc
    forged = replace(TV_GRANT, grant_ref="generated-grant-forged", budget=10**9)
    assert sa.grant_headroom(forged, tv.VariantLedger(), now=TV_NOW) == 10**9


# Z5 (mutation survivor F03): a reserved, uncommitted line always counts, even once generated.
def test_reserved_line_counts_even_after_its_child_is_generated(tmp_path: Path) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, cap_spend=5)
    grant_budget(led, path, "generated-budget-2", cap_spend=5)
    sc = scope(path, seal)
    led.reserve("generated-budget", five(sc, 0), verified_by=sc, now=TV_NOW)
    tv_spend(path, "a0", seal, 1, "uncommitted")  # generated before b1 commits
    # Counted: 5 (b1 reserved, none fulfilled) + 5 (b2) + 1 spent = 11 > 10.
    assert code_of(led.reserve, "generated-budget-2", five(sc, 5), verified_by=sc, now=TV_NOW) == (
        "grant_headroom_exceeded"
    )


# W* (delta review of 7514fda, same reviewer). Each failed against 7514fda unless noted.
def clock_mark(db_path: Path) -> list[tuple[object, ...]]:
    db = sqlite3.connect(db_path)
    try:
        return [tuple(r) for r in db.execute("SELECT * FROM grant_clock ORDER BY grant_ref")]
    finally:
        db.close()


# W1 / F1: a refused forged grant, or a huge caller time, cannot expire the real grant.
def test_f1_forged_grant_or_huge_now_cannot_expire_the_real_grant(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    db_path = tmp_path / "alloc.db"
    led = AllocationLedger(db_path)
    grant_budget(led, path)
    forged = replace(TV_GRANT, budget=10**9)
    assert code_of(grant_budget, led, path, "generated-budget-2", grant=forged, now=2**62) == (
        "clock_skew_refused"
    )
    assert code_of(grant_budget, led, path, "generated-budget-2", grant=forged) == (
        "grant_conflict"
    )
    sc = scope(path, seal)
    a0 = allocate([family("a0")], policy(), verified_by=sc)
    for huge in (2**62, TV_NOW + sa.CLOCK_TOLERANCE + 1):
        assert code_of(led.reserve, "generated-budget", a0, verified_by=sc, now=huge) == (
            "clock_skew_refused"
        )
    assert led.reserve("generated-budget", a0, verified_by=sc, now=TV_NOW)["used_units"] == 1
    assert code_of(led.commit, "generated-budget", a0.allocation_id, now=2**62) == (
        "clock_skew_refused"
    )
    assert led.commit("generated-budget", a0.allocation_id, now=TV_NOW)["used_units"] == 1
    b0 = allocate([family("b0")], policy(), verified_by=sc)
    assert led.reserve("generated-budget", b0, verified_by=sc, now=TV_NOW)["used_units"] == 2
    assert clock_mark(db_path)[0][1] == TV_NOW
    assert code_of(sa.grant_headroom, TV_GRANT, tv.VariantLedger(path), now=2**62) == (
        "clock_skew_refused"
    )


# W2 (mutant M18): a refused open, reserve or commit leaves the clock mark unchanged.
def test_refused_calls_leave_the_clock_mark_unchanged(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    db_path = tmp_path / "alloc.db"
    led = AllocationLedger(db_path)
    grant_budget(led, path, cap_spend=1)
    sc = scope(path, seal)
    a0 = allocate([family("a0")], policy(), verified_by=sc)
    led.reserve("generated-budget", a0, verified_by=sc, now=TV_NOW)
    before = clock_mark(db_path)
    assert before == [(TV_GRANT.grant_ref, TV_NOW, sa._clock_digest(TV_GRANT.grant_ref, TV_NOW))]
    later = TV_NOW + 200
    WORKER_NOW["t"] = later  # the worker clock advances: a persisted refusal would move the mark
    forged = replace(TV_GRANT, budget=10**9)
    assert code_of(grant_budget, led, path, "generated-budget-2", grant=forged, now=later) == (
        "grant_conflict"
    )
    b0 = allocate([family("b0")], policy(), verified_by=sc)
    assert code_of(led.reserve, "generated-budget", b0, verified_by=sc, now=later) == (
        "budget_exceeded"
    )
    tv_spend(path, "b0", seal, 10, "w2")  # OPE-516 spends the whole grant
    assert code_of(led.commit, "generated-budget", a0.allocation_id, now=later) == (
        "grant_headroom_exceeded"
    )
    assert clock_mark(db_path) == before


def test_worker_clock_stepping_back_is_refused(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0")
    led = AllocationLedger()
    WORKER_NOW["t"] = 900
    grant_budget(led, path, now=900)
    WORKER_NOW["t"] = 100  # the worker clock jumped back (snapshot restore, NTP step)
    sc = scope(path, seal)
    a0 = allocate([family("a0")], policy(), verified_by=sc)
    assert code_of(led.reserve, "generated-budget", a0, verified_by=sc, now=100) == (
        "clock_regressed"
    )


# W3 / F2: a cheap child frees only what it was charged.
def test_f2_child_frees_only_its_charged_cost(tmp_path: Path) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, unit_cost=5, cap_spend=5)
    sc = scope(path, seal)
    one = allocate(
        [family("a0", recipes=FULFIL)],
        policy(total_units=1, spend_ceiling=5, unit_cost=5),
        verified_by=sc,
    )
    led.reserve("generated-budget", one, verified_by=sc, now=TV_NOW)
    led.commit("generated-budget", one.allocation_id, now=TV_NOW)
    tv_spend(path, "a0", seal, 1, "cheap")  # charged 1 of the line's 5
    # Room: 10 - 1 spent - (5 - 1) still committed = 5, not 9.
    assert grant_budget(led, path, "generated-budget-2", unit_cost=5)["cap_spend"] == 5


# W4 / F3: the open path reads OPE-516's children before its spend.
def test_f3_open_reads_children_before_spend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, cap_spend=5)
    sc = scope(path, seal)
    b1 = five(sc, 0)
    led.reserve("generated-budget", b1, verified_by=sc, now=TV_NOW)
    led.commit("generated-budget", b1.allocation_id, now=TV_NOW)
    real = sa._read_charged

    def then_a_child_arrives(real_path: Path, grant_ref: str) -> object:
        got = real(real_path, grant_ref)
        tv_spend(path, "a0", seal, 1, "between-reads")
        return got

    monkeypatch.setattr(sa, "_read_charged", then_a_child_arrives)
    # The child is in spent (read after) but not fulfilment (read before): 10 - 1 - 5 = 4.
    assert grant_budget(led, path, "generated-budget-2")["cap_spend"] == 4


# W5 / F4: the clock rows are part of the integrity check.
@pytest.mark.parametrize(
    "edit",
    [
        "UPDATE grant_clock SET high_water='generated-not-an-int'",
        "UPDATE grant_clock SET high_water=5",
        "DELETE FROM grant_clock",
        "DROP TABLE grant_clock",
    ],
)
def test_f4_edited_or_dropped_clock_is_refused(tmp_path: Path, edit: str) -> None:
    path, _ = world(tmp_path, "a0")
    db_path = tmp_path / "alloc.db"
    grant_budget(AllocationLedger(db_path), path)
    db = sqlite3.connect(db_path)
    db.execute(edit)
    db.commit()
    db.close()
    assert code_of(AllocationLedger(db_path).status, "generated-budget") == (
        "ledger_integrity_failed"
    )


# W6 (mutant M1): the real worker clock is integer Unix seconds (not monkeypatched here).
def test_real_worker_clock_is_integer_unix_seconds() -> None:
    assert REAL_WORKER_NOW is not None
    got = REAL_WORKER_NOW()
    assert type(got) is int and abs(got - time.time()) < 5


# W7 (mutant M11): a zero-cost child carries no spend and frees nothing.
def test_zero_cost_child_does_not_fulfil_a_line(tmp_path: Path) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, cap_spend=5)
    sc = scope(path, seal)
    b1 = five(sc, 0)
    led.reserve("generated-budget", b1, verified_by=sc, now=TV_NOW)
    led.commit("generated-budget", b1.allocation_id, now=TV_NOW)
    tv_spend(path, "a0", seal, 0, "free")
    assert grant_budget(led, path, "generated-budget-2")["cap_spend"] == 5


# W8 (mutant M13): the open path nets generated lines out of the grant's live spend.
def test_open_accounts_for_generated_lines(tmp_path: Path) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, cap_spend=5)
    sc = scope(path, seal)
    b1 = five(sc, 0)
    led.reserve("generated-budget", b1, verified_by=sc, now=TV_NOW)
    led.commit("generated-budget", b1.allocation_id, now=TV_NOW)
    for i in range(5):
        tv_spend(path, f"a{i}", seal, 1, f"w8-{i}")
    # 10 - 5 spent - 0 unfulfilled = 5 (without netting it would be 0: grant_unavailable).
    assert grant_budget(led, path, "generated-budget-2")["cap_spend"] == 5


# W9 (mutation survivor C09): a non-integer clock value is refused even with a recomputed digest.
def test_non_integer_clock_with_recomputed_digest_is_refused(tmp_path: Path) -> None:
    path, _ = world(tmp_path, "a0")
    db_path = tmp_path / "alloc.db"
    grant_budget(AllocationLedger(db_path), path)
    bad = "generated-not-an-int"
    db = sqlite3.connect(db_path)
    db.execute(
        "UPDATE grant_clock SET high_water=?, row_digest=?",
        (bad, sa._clock_digest(TV_GRANT.grant_ref, bad)),
    )
    db.commit()
    db.close()
    assert code_of(AllocationLedger(db_path).status, "generated-budget") == (
        "ledger_integrity_failed"
    )


# V* (delta review of 2d513d3, same reviewer).
# V1 / N1: the mark stores only the worker clock, so a caller's lead cannot trip a step-back.
def test_n1_caller_lead_does_not_eat_the_step_back_tolerance(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0")
    db_path = tmp_path / "alloc.db"
    led = AllocationLedger(db_path)
    grant_budget(led, path, now=TV_NOW + sa.CLOCK_TOLERANCE)  # the first mark write has a lead
    assert clock_mark(db_path)[0][1] == TV_NOW
    sc = scope(path, seal)
    led.reserve(
        "generated-budget",
        allocate([family("a0")], policy(), verified_by=sc),
        verified_by=sc,
        now=TV_NOW + sa.CLOCK_TOLERANCE,  # 400: the caller is 300 s ahead
    )
    assert clock_mark(db_path)[0][1] == TV_NOW  # only the worker's 100 is stored
    WORKER_NOW["t"] = TV_NOW - 1  # a 1 s step back
    b0 = allocate([family("b0")], policy(), verified_by=sc)
    assert led.reserve("generated-budget", b0, verified_by=sc, now=TV_NOW - 1)["used_units"] == 2


# V2 (mutant N02): the skew boundary is inclusive at +300 s and refused at +301 s.
def test_caller_skew_boundary(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0")
    led = AllocationLedger()
    grant_budget(led, path)
    sc = scope(path, seal)
    plan = allocate([family("a0")], policy(), verified_by=sc)
    limit = TV_NOW + sa.CLOCK_TOLERANCE
    assert code_of(led.reserve, "generated-budget", plan, verified_by=sc, now=limit + 1) == (
        "clock_skew_refused"
    )
    assert led.reserve("generated-budget", plan, verified_by=sc, now=limit)["used_units"] == 1
    # A planning budget ignores ``now`` but still refuses one too far ahead (fails closed).
    led.open_budget("generated-plain", cap_units=1, cap_spend=1, unit_cost=1)
    plain = allocate([family("p0")], policy())
    assert code_of(led.reserve, "generated-plain", plain, now=limit + 1) == "clock_skew_refused"
    assert led.reserve("generated-plain", plain, now=limit)["used_units"] == 1


# V3 (mutants N07, N09): a step back within tolerance is allowed; the mark never decreases;
# a regression heals by itself while release, status, live_lines and retract_family keep working.
def test_step_back_tolerance_and_mark_never_decreases(tmp_path: Path) -> None:
    path, seal = world(tmp_path, "a0", "b0", "c0")
    db_path = tmp_path / "alloc.db"
    led = AllocationLedger(db_path)
    WORKER_NOW["t"] = 500
    grant_budget(led, path, now=500)
    grant_budget(led, path, "generated-budget-2", now=500)  # same grant, second budget
    assert clock_mark(db_path)[0][1] == 500
    sc = scope(path, seal)
    WORKER_NOW["t"] = 200  # exactly 300 s back: allowed
    a0 = allocate([family("a0")], policy(), verified_by=sc)
    assert led.reserve("generated-budget", a0, verified_by=sc, now=200)["used_units"] == 1
    c0 = allocate([family("c0")], policy(), verified_by=sc)
    assert led.reserve("generated-budget-2", c0, verified_by=sc, now=200)["used_units"] == 1
    assert clock_mark(db_path)[0][1] == 500  # never decreases
    WORKER_NOW["t"] = 199  # 301 s back: refused
    b0 = allocate([family("b0")], policy(), verified_by=sc)
    assert code_of(led.reserve, "generated-budget", b0, verified_by=sc, now=199) == (
        "clock_regressed"
    )
    assert code_of(led.commit, "generated-budget", a0.allocation_id, now=199) == "clock_regressed"
    assert code_of(led.commit, "generated-budget-2", c0.allocation_id, now=199) == (
        "clock_regressed"
    )
    assert led.status("generated-budget")["used_units"] == 1  # status keeps working
    # live_lines keeps working on the regressed grant.
    assert [x.family_id for x in led.live_lines("generated-budget")] == ["a0"]
    assert [x.family_id for x in led.live_lines("generated-budget-2")] == ["c0"]
    # retract_family keeps working on the regressed grant and frees the unspent line.
    retracted = led.retract_family("generated-budget-2", "c0", "generated-retraction")
    assert retracted["used_units"] == 0
    assert led.live_lines("generated-budget-2") == []
    assert led.status("generated-budget")["used_units"] == 1  # the other family is untouched
    assert led.release("generated-budget", a0.allocation_id)["used_units"] == 0
    WORKER_NOW["t"] = 600  # the worker clock catches up: it heals by itself
    assert led.reserve("generated-budget", b0, verified_by=sc, now=600)["used_units"] == 1
    assert clock_mark(db_path)[0][1] == 600


# V4 (mutant N15): a dearer child never makes a line count negative against other lines.
def test_dearer_child_does_not_undercount_other_lines(tmp_path: Path) -> None:
    path, seal = ten_families(tmp_path)
    led = AllocationLedger()
    grant_budget(led, path, cap_spend=1)
    grant_budget(led, path, "generated-budget-2", cap_spend=4)
    sc = scope(path, seal)
    one = allocate(
        [family("a0", recipes=FULFIL)], policy(total_units=1, spend_ceiling=1), verified_by=sc
    )
    led.reserve("generated-budget", one, verified_by=sc, now=TV_NOW)
    led.commit("generated-budget", one.allocation_id, now=TV_NOW)
    four = allocate(
        [family(f"a{i}", recipes=FULFIL) for i in range(1, 5)],
        policy(total_units=4, spend_ceiling=4),
        verified_by=sc,
    )
    led.reserve("generated-budget-2", four, verified_by=sc, now=TV_NOW)
    tv_spend(path, "a0", seal, 5, "dear")  # charged 5 for a line of cost 1
    # 10 - 5 spent - (0 for a0, floored) - 4 reserved = 1 (without the floor it would be 5).
    assert grant_budget(led, path, "generated-budget-3")["cap_spend"] == 1
