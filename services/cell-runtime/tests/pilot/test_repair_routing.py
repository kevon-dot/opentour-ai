"""Generated sentinel cases for the recurring-error routing ladder (OPE-417).

Local and simulated only: no provider, protected set, ledger reservation or fit is touched.
"""

from __future__ import annotations

import ast
import glob
import io
import json
import math
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import comparison_plan
from opentour_cell.pilot import repair_routing as rr
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.simple_repair import RepairError, declared_rule

AS_OF = 10_000
MODEL = rr.ModelTarget("simulated:model-a", "label_correction", "generated-capability-1")


class Verifier:
    def __init__(self, ok: bool = True) -> None:
        self.ok, self.calls = ok, 0

    def verify(self, policy: rr.SignedPolicy) -> bool:
        self.calls += 1
        return self.ok


def case(i: str, bucket: str, inc: str, ref: str | None, **kw: Any) -> rr.DevelopmentCase:
    return rr.DevelopmentCase(
        case_id=f"generated-case-{i}",
        entity_id=kw.pop("entity", f"generated-entity-{i}"),
        family_id=kw.pop("family", f"generated-family-{i}"),
        decided_at=kw.pop("at", 9_000),
        features={"bucket": bucket},
        incumbent={"status": "automatic", "label": inc},
        reference=ref,
        **kw,
    )


def group(prefix: str, n: int, bucket: str, inc: str, ref: str) -> list[rr.DevelopmentCase]:
    return [case(f"{prefix}{k}", bucket, inc, ref) for k in range(n)]


def terms(**kw: Any) -> rr.RoutingTerms:
    base = rr.RoutingTerms(
        error_family=("b", "a"),
        min_independent_groups=2,
        recurrence_window_seconds=5_000,
        material_residual_cases=2,
        min_coverage_bps=5_000,
        max_wrong_automatic_bps=0,
        critical_slices=(),
        min_development_cases=3,
        min_groups_per_label=2,
        documentation_revision="generated-docs-1",
    )
    return replace(base, **kw)


def rule(fallback: str = "incumbent") -> dict[str, Any]:
    return declared_rule(
        feature="bucket",
        allowed_features=["bucket"],
        mapping={"x": "a"},
        fallback=fallback,
        allowed_labels=["a", "b"],
    )


def mandate(**kw: Any) -> rr.MandateView:
    base = rr.MandateView(
        "generated-mandate-1",
        "generated-mrev-1",
        False,
        False,
        frozenset({"inspect", "evaluate", "train"}),
        3,
        0,
        3600,
        10800,
        0,
    )
    return replace(base, **kw)


def recipes() -> tuple[rr.Recipe, ...]:
    common: dict[str, Any] = {
        "models": frozenset({"simulated:model-a"}),
        "tasks": frozenset({"label_correction"}),
        "split": "grouped",
        "metric": "macro_f1",
        "direction": "maximize",
        "stopping_rule": "fixed_steps",
        "step_limit": 100,
    }
    return (
        rr.Recipe(
            "generated-recipe-linear",
            1,
            allowed_params=(),
            trial_limit=1,
            est_seconds_per_trial=600,
            **common,
        ),
        rr.Recipe(
            "generated-recipe-sweep",
            1,
            allowed_params=("lr",),
            trial_limit=4,
            est_seconds_per_trial=100,
            **common,
        ),
    )


def scheduling(*protected: str) -> rr.ComparisonScheduling:
    return rr.ComparisonScheduling(
        digest("generated-intake"),
        protected or ("generated-protected-1", "generated-protected-2"),
        "generated-incumbent-1",
        "generated-audience",
    )


def request(
    cases: list[rr.DevelopmentCase],
    *,
    t: rr.RoutingTerms | None = None,
    verifier: Verifier | None = None,
    snap: str = "generated-snapshot-1",
    run: str = "generated-run-1",
    **kw: Any,
) -> rr.RoutingRequest:
    t = t or terms()
    snapshot = rr.AdmittedSnapshot(
        "generated-org",
        "generated-workflow",
        run,
        snap,
        "generated-evidence-1",
        "generated-docs-1",
        AS_OF,
    )
    policy = rr.SignedPolicy("generated-policy-1", snap, t.revision, "simulated:signature")
    args: dict[str, Any] = {
        "terms": t,
        "policy": policy,
        "verifier": verifier or Verifier(),
        "snapshot": snapshot,
        "mandate": mandate(),
        "cases": cases,
        "rule": rule(),
        "recipes": recipes(),
        "model": MODEL,
    }
    return rr.RoutingRequest(**(args | kw))


def correct() -> list[rr.DevelopmentCase]:
    return group("ok", 3, "y", "b", "b")


@pytest.fixture
def store(tmp_path: Path) -> rr.RepairRoutingStore:
    d = tmp_path / "private"
    d.mkdir(mode=0o700)
    return rr.RepairRoutingStore(d)


def data_fix() -> list[rr.DevelopmentCase]:
    return group("fx", 4, "x", "b", "a") + correct()


def independent_residual() -> list[rr.DevelopmentCase]:
    return group("fx", 2, "x", "b", "a") + group("rz", 3, "z", "b", "a") + correct()


def same_entity_residual() -> list[rr.DevelopmentCase]:
    same = [case(f"se{k}", "z", "b", "a", entity="generated-entity-same") for k in range(3)]
    return group("fx", 2, "x", "b", "a") + same + correct()


def test_only_independent_residual_reaches_train_eligibility(store: rr.RepairRoutingStore) -> None:
    # Migrated for OPE-417 Decision 4: the data-access-only fix now carries a valid
    # ComparisonScheduling, because a sufficient simple repair with none is refused.
    fix = store.evaluate(request(data_fix(), run="generated-run-a", scheduling=scheduling()))
    res = store.evaluate(request(independent_residual(), run="generated-run-b"))
    dup = store.evaluate(request(same_entity_residual(), run="generated-run-c"))
    assert (fix["outcome"], fix["reason_code"]) == (
        "simple_repair",
        "simple_repair_meets_development_limits",
    )
    assert (res["outcome"], res["reason_code"]) == ("fit_eligible", "independent_material_residual")
    assert (dup["outcome"], dup["reason_code"]) == ("request_evidence", "residual_not_independent")
    for r in (fix, res, dup):
        assert (r["fit_starts"], r["reservations"], r["protected_reads"]) == (0, 0, 0)
        assert r["augmentation_state"] == "not_enabled"
    assert fix["residual_roots"] == [] and fix["independent_residual_groups"] == 0
    assert fix["frozen_arm"] == fix["rule_revision"] and fix["comparison_scheduled"] is True
    assert res["independent_residual_groups"] == 3 and dup["independent_residual_groups"] == 1
    assert res["train_eligible_policy_ref"] == "generated-policy-1"
    assert fix["train_eligible_policy_ref"] is None and dup["train_eligible_policy_ref"] is None
    assert res["recipe_selection"]["selected"]["capability_receipt_ref"] == "generated-capability-1"


def test_replayed_trigger_is_one_durable_evaluation(store: rr.RepairRoutingStore) -> None:
    first = store.evaluate(request(independent_residual()))
    again = store.evaluate(request(independent_residual()))
    restarted = rr.RepairRoutingStore(store.path.parent).evaluate(request(independent_residual()))
    assert (first["replayed"], again["replayed"], restarted["replayed"]) == (False, True, True)
    assert first["evaluation_id"] == again["evaluation_id"] == restarted["evaluation_id"]
    assert first["revision"] == restarted["revision"]
    assert store.count() == 1


def test_replay_ignores_mandate_counter_drift_but_rejects_changed_evidence(
    store: rr.RepairRoutingStore,
) -> None:
    first = store.evaluate(request(independent_residual()))
    drift = request(independent_residual(), mandate=mandate(reserved_fits=2))
    assert store.evaluate(drift)["evaluation_id"] == first["evaluation_id"]
    changed = independent_residual() + [case("extra", "z", "b", "a")]
    with pytest.raises(rr.RoutingError, match="EVALUATION_CONFLICT"):
        store.evaluate(request(changed))
    assert store.count() == 1


def test_new_snapshot_or_mandate_revision_is_a_new_evaluation(store: rr.RepairRoutingStore) -> None:
    a = store.evaluate(request(independent_residual()))
    b = store.evaluate(request(independent_residual(), snap="generated-snapshot-2"))
    c = store.evaluate(
        request(independent_residual(), mandate=mandate(mandate_revision="generated-mrev-2"))
    )
    assert len({a["evaluation_id"], b["evaluation_id"], c["evaluation_id"]}) == 3
    assert store.count() == 3


def test_interrupted_write_leaves_no_partial_evaluation(
    store: rr.RepairRoutingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(value: object) -> str:
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(rr, "canonical", boom)
    with pytest.raises(RuntimeError):
        store.evaluate(request(independent_residual()))
    assert store.count() == 0
    monkeypatch.undo()
    assert store.evaluate(request(independent_residual()))["replayed"] is False
    assert store.count() == 1


def test_low_coverage_holds_or_is_eligible_per_frozen_thresholds(
    store: rr.RepairRoutingStore,
) -> None:
    review = rule("review")
    many = group("rz", 3, "z", "b", "a") + correct()
    one = [case(f"se{k}", "z", "b", "a", entity="generated-entity-same") for k in range(3)]
    t = terms(min_coverage_bps=8_000)
    eligible = store.evaluate(request(many, t=t, rule=review, run="generated-run-1"))
    held = store.evaluate(request(one + correct(), t=t, rule=review, run="generated-run-2"))
    assert eligible["outcome"] == "fit_eligible"
    assert held["outcome"] == "request_evidence"
    assert held["reason_code"] in {
        "residual_not_independent",
        "insufficient_independent_development_evidence",
    }


def test_keep_incumbent_when_repair_only_ties(store: rr.RepairRoutingStore) -> None:
    cases = group("ok", 8, "y", "b", "b") + group("er", 1, "z", "b", "a")
    r = store.evaluate(request(cases, t=terms(max_wrong_automatic_bps=2_000)))
    assert (r["outcome"], r["reason_code"]) == ("keep_incumbent", "incumbent_tied_prefer_simpler")
    assert r["frozen_arm"] is None and r["fit_starts"] == 0


def test_protected_rows_are_refused_and_nothing_is_recorded(store: rr.RepairRoutingStore) -> None:
    leak = independent_residual() + [case("leak", "z", "b", "a", split="protected")]
    with pytest.raises(rr.RoutingError, match="PROTECTED_ROW_SUPPLIED"):
        store.evaluate(request(leak))
    assert store.count() == 0


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        ("stale", "stale_documentation"),
        ("missing", "missing_decision_time_context"),
        ("contradiction", "contradictory_labels"),
        ("unlabeled", "unusable_labels"),
    ],
)
def test_unusable_evidence_has_distinct_outcomes(
    store: rr.RepairRoutingStore, mutate: str, reason: str
) -> None:
    cases = independent_residual()
    docs = "generated-docs-1"
    if mutate == "stale":
        docs = "generated-docs-0"
    elif mutate == "missing":
        cases[0] = case("fx0", "x", "b", "a", context_state="missing")
    elif mutate == "contradiction":
        cases.append(case("fx0b", "x", "b", "b", entity="generated-entity-fx0"))
    else:
        cases.append(case("nolabel", "z", "b", None))
    req = request(cases)
    req = replace(req, snapshot=replace(req.snapshot, documentation_revision=docs))
    r = store.evaluate(req)
    assert (r["outcome"], r["reason_code"]) == ("request_evidence", reason)
    assert r["next_evidence"] and r["residual_roots"] == []


def test_label_imbalance_with_inadequate_group_support(store: rr.RepairRoutingStore) -> None:
    cases = group("fx", 2, "x", "b", "a") + group("rz", 3, "z", "b", "a")
    cases += group("ok", 1, "y", "b", "b")
    r = store.evaluate(request(cases))
    assert r["reason_code"] == "label_imbalance_inadequate_group_support"
    assert r["outcome"] == "request_evidence"


def test_starter_generated_and_out_of_window_cases_never_count(
    store: rr.RepairRoutingStore,
) -> None:
    extra = [case(f"st{k}", "z", "b", "a", origin="starter") for k in range(3)]
    extra += [case(f"ch{k}", "z", "b", "a", origin="generated_child") for k in range(3)]
    extra += [case(f"old{k}", "z", "b", "a", at=1) for k in range(3)]
    r = store.evaluate(request(same_entity_residual() + extra))
    assert r["reason_code"] == "residual_not_independent"
    assert r["independent_residual_groups"] == 1
    assert r["excluded_non_live_cases"] == 6


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"mandate": mandate(uses=frozenset({"evaluate"}))}, "training_rights_required"),
        ({"mandate": mandate(reserved_fits=3)}, "training_budget_required"),
        ({"model": None}, "qualified_model_required"),
        (
            {"model": rr.ModelTarget("simulated:model-a", "label_correction", None)},
            "qualified_model_required",
        ),
        ({"recipes": ()}, "unsupported_recipe"),
        (
            {"model": rr.ModelTarget("simulated:other", "label_correction", "generated-c")},
            "unsupported_recipe",
        ),
        ({"verifier": Verifier(False)}, "signed_policy_required"),
    ],
)
def test_eligible_residual_is_blocked_by_each_missing_authority(
    store: rr.RepairRoutingStore, kw: dict[str, Any], reason: str
) -> None:
    r = store.evaluate(request(independent_residual(), **kw))
    assert (r["outcome"], r["reason_code"]) == ("request_evidence", reason)
    assert r["train_eligible_policy_ref"] is None
    assert r["reservations"] == 0 and r["fit_starts"] == 0


def test_withdrawn_or_unauthorized_mandate_refuses_before_recording(
    store: rr.RepairRoutingStore,
) -> None:
    for bad in (mandate(withdrawn=True), mandate(expired=True), mandate(uses=frozenset())):
        with pytest.raises(rr.RoutingError, match="STANDING_MANDATE_UNAVAILABLE|EVALUATION_NOT"):
            store.evaluate(request(independent_residual(), mandate=bad))
    assert store.count() == 0


def test_policy_must_bind_this_snapshot_and_pinned_terms(store: rr.RepairRoutingStore) -> None:
    req = request(independent_residual())
    with pytest.raises(rr.RoutingError, match="POLICY_SNAPSHOT_MISMATCH"):
        store.evaluate(replace(req, policy=replace(req.policy, snapshot_ref="generated-other")))
    with pytest.raises(rr.RoutingError, match="POLICY_TERMS_MISMATCH"):
        store.evaluate(replace(req, terms=terms(min_independent_groups=3)))


def test_recipe_selection_is_deterministic_and_logged() -> None:
    a = rr.select_recipe(recipes(), MODEL, mandate())
    assert a == rr.select_recipe(tuple(reversed(recipes())), MODEL, mandate())
    assert a["selected"]["recipe"] == ["generated-recipe-sweep", 1]
    assert a["selected"]["budget_estimate_seconds"] == 600
    assert a["rejected"] == [
        {"recipe": ["generated-recipe-linear", 1], "reason": "higher_estimate"}
    ]
    one = rr.select_recipe(recipes(), MODEL, mandate(max_fits=1))
    assert one["selected"]["recipe"] == ["generated-recipe-linear", 1]
    assert one["selected"]["trial_limit"] == 1 and one["selected"]["allowed_params"] == []
    assert one["rejected"] == [
        {
            "recipe": ["generated-recipe-sweep", 1],
            "reason": "parameter_sweep_exceeds_owner_fit_limit",
        }
    ]


def test_budget_and_paid_probe_reject_recipes() -> None:
    tight = rr.select_recipe(recipes(), MODEL, mandate(max_seconds_per_fit=100))
    assert tight["selected"] is None
    assert {x["reason"] for x in tight["rejected"]} == {"estimate_exceeds_owner_budget"}
    paid = tuple(replace(r, paid_probe=True) for r in recipes())
    assert rr.select_recipe(paid, MODEL, mandate())["selected"] is None


def test_critical_slice_failure_demotes_simple_repair(store: rr.RepairRoutingStore) -> None:
    # Migrated for OPE-417 Decision 4: both cases now carry a valid ComparisonScheduling, so the
    # demotion is attributable to the critical slice rather than to a missing field.
    sched = scheduling()
    ok = store.evaluate(
        request(
            data_fix(),
            t=terms(critical_slices=("bucket:x",)),
            run="generated-run-1",
            scheduling=sched,
        )
    )
    bad = store.evaluate(
        request(
            data_fix(),
            t=terms(critical_slices=("bucket:q",)),
            run="generated-run-2",
            scheduling=sched,
        )
    )
    assert ok["outcome"] == "simple_repair" and ok["comparison_scheduled"] is True
    assert (bad["outcome"], bad["reason_code"]) == ("request_evidence", "critical_slice_unobserved")


def test_simple_route_freezes_comparison_plan_with_zero_fits(store: rr.RepairRoutingStore) -> None:
    sched = rr.ComparisonScheduling(
        digest("generated-intake"),
        ("generated-protected-1", "generated-protected-2"),
        "generated-incumbent-1",
        "generated-audience",
    )
    a = store.evaluate(request(data_fix(), scheduling=sched))
    assert a["comparison_scheduled"] is True and len(a["comparison_plan_revision"]) == 64
    assert a["fit_starts"] == 0 and a["protected_reads"] == 0
    again = store.evaluate(request(data_fix(), scheduling=sched))
    assert again["comparison_plan_revision"] == a["comparison_plan_revision"]


def test_readiness_handoff_and_current_eligibility(store: rr.RepairRoutingStore) -> None:
    r = store.evaluate(request(independent_residual()))
    h = r["readiness_handoff"]
    assert h["independent_residual_groups"] == 3 and h["owner_minimum"] == 2
    assert (h["fit_starts"], h["reservations"]) == (0, 0)
    assert h["evidence_revisions"]["evidence_revision"] == "generated-evidence-1"
    snap = request(independent_residual()).snapshot
    now = store.current_eligibility(r["evaluation_id"], snapshot=snap, mandate=mandate())
    assert now["eligible"] is True and now["automatic_promotion"] is False
    for kw, why in (
        ({"withdrawn": True}, "mandate_withdrawn_or_expired"),
        ({"mandate_revision": "generated-mrev-9"}, "mandate_revision_changed"),
        ({"reserved_fits": 3}, "training_budget_required"),
    ):
        got = store.current_eligibility(r["evaluation_id"], snapshot=snap, mandate=mandate(**kw))
        assert (got["eligible"], got["reason_code"]) == (False, why)
    reopened = replace(snap, evidence_revision="generated-evidence-2")
    got = store.current_eligibility(r["evaluation_id"], snapshot=reopened, mandate=mandate())
    assert got["reason_code"] == "evidence_revision_changed"


def test_augmentation_is_recorded_off_and_not_accepted_when_requested(
    store: rr.RepairRoutingStore,
) -> None:
    with pytest.raises(rr.RoutingError, match="AUGMENTATION_NOT_SUPPORTED"):
        store.evaluate(request(independent_residual(), augmentation="enabled"))


def test_store_is_private_and_holds_one_row(store: rr.RepairRoutingStore) -> None:
    store.evaluate(request(data_fix()))
    assert store.path.stat().st_mode & 0o077 == 0
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM evaluations").fetchone()[0] == 1


def test_grouping_is_transitive_and_case_insensitive(store: rr.RepairRoutingStore) -> None:
    # e1-f1, e2-f1, e2-f2 chain into one group; the casing variant joins it too.
    chain = [
        case("c1", "z", "b", "a", entity="generated-e1", family="generated-f1"),
        case("c2", "z", "b", "a", entity="generated-e2", family="generated-f1"),
        case("c3", "z", "b", "a", entity="Generated-E2", family="GENERATED-F2"),
        case("c4", "z", "b", "a", entity="GENERATED-E1", family="generated-f9"),
    ]
    r = store.evaluate(request(group("fx", 2, "x", "b", "a") + chain + correct()))
    assert (r["outcome"], r["reason_code"]) == ("request_evidence", "residual_not_independent")
    assert r["independent_residual_groups"] == 1
    assert (r["fit_starts"], r["reservations"], r["protected_reads"]) == (0, 0, 0)


def test_changed_clock_or_workflow_on_same_key_conflicts(store: rr.RepairRoutingStore) -> None:
    store.evaluate(request(independent_residual()))
    base = request(independent_residual())
    for snap in (
        replace(base.snapshot, as_of=AS_OF + 1),
        replace(base.snapshot, workflow_ref="generated-other-workflow"),
    ):
        with pytest.raises(rr.RoutingError, match="EVALUATION_CONFLICT"):
            store.evaluate(replace(base, snapshot=snap))
    assert store.count() == 1


def test_concurrent_triggers_record_one_evaluation(store: rr.RepairRoutingStore) -> None:
    out: list[dict[str, Any]] = []

    def go() -> None:
        out.append(rr.RepairRoutingStore(store.path.parent).evaluate(request(data_fix())))

    threads = [threading.Thread(target=go) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert len(out) == 6 and len({o["evaluation_id"] for o in out}) == 1
    assert sorted(o["replayed"] for o in out) == [False] + [True] * 5
    assert store.count() == 1


def test_current_eligibility_rejects_foreign_mandate_and_lost_training_right(
    store: rr.RepairRoutingStore,
) -> None:
    r = store.evaluate(request(independent_residual()))
    snap = request(independent_residual()).snapshot
    foreign = mandate(mandate_ref="generated-mandate-other")
    got = store.current_eligibility(r["evaluation_id"], snapshot=snap, mandate=foreign)
    assert (got["eligible"], got["reason_code"]) == (False, "mandate_revision_changed")
    no_train = mandate(uses=frozenset({"inspect", "evaluate"}))
    got = store.current_eligibility(r["evaluation_id"], snapshot=snap, mandate=no_train)
    assert (got["eligible"], got["reason_code"]) == (False, "training_rights_required")
    docs = replace(snap, documentation_revision="generated-docs-2")
    got = store.current_eligibility(r["evaluation_id"], snapshot=docs, mandate=mandate())
    assert got["reason_code"] == "evidence_revision_changed"
    assert got["reservations"] == 0 and got["automatic_promotion"] is False


def test_contradiction_check_uses_the_grouping_identity_for_entity_case(
    store: rr.RepairRoutingStore,
) -> None:
    # Same entity by the grouping rule (differs only in case), same features, conflicting labels.
    cases = independent_residual() + [
        case("ca", "x", "b", "a", entity="Entity-A"),
        case("cb", "x", "b", "b", entity="entity-a"),
    ]
    r = store.evaluate(request(cases))
    assert (r["outcome"], r["reason_code"]) == ("request_evidence", "contradictory_labels")
    assert r["next_evidence"] and r["residual_roots"] == []


def test_distinct_entities_with_conflicting_labels_are_not_a_contradiction(
    store: rr.RepairRoutingStore,
) -> None:
    # Normalization must not merge genuinely different ids: these differ by more than case.
    cases = independent_residual() + [
        case("da", "x", "b", "a", entity="Entity-A"),
        case("db", "x", "b", "b", entity="Entity-B"),
        case("dc", "x", "b", "b", entity="entity-a1"),
    ]
    r = store.evaluate(request(cases))
    assert r["reason_code"] != "contradictory_labels"


def test_same_entity_case_variants_with_agreeing_labels_are_not_a_contradiction(
    store: rr.RepairRoutingStore,
) -> None:
    cases = independent_residual() + [
        case("ea", "x", "b", "a", entity="Entity-A"),
        case("eb", "x", "b", "a", entity="entity-a"),
    ]
    r = store.evaluate(request(cases))
    assert r["reason_code"] != "contradictory_labels"


def test_sufficient_simple_repair_without_scheduling_is_refused(
    store: rr.RepairRoutingStore,
) -> None:
    # OPE-417 Decision 4: "A sufficient repair freezes the simple arm and schedules comparison
    # with zero fits." Whether that holds must not depend on the caller passing a field.
    held = store.evaluate(request(data_fix(), run="generated-run-a"))
    assert (held["outcome"], held["reason_code"]) == (
        "request_evidence",
        "comparison_scheduling_required",
    )
    assert held["next_evidence"] == ["provide_comparison_scheduling"]
    assert held["frozen_arm"] is None and held["comparison_scheduled"] is False
    assert held["comparison_plan_revision"] is None
    assert (held["fit_starts"], held["reservations"], held["protected_reads"]) == (0, 0, 0)
    # The same evidence with a protected population to compare against is the repair.
    scheduled = store.evaluate(request(data_fix(), run="generated-run-b", scheduling=scheduling()))
    assert (scheduled["outcome"], scheduled["reason_code"]) == (
        "simple_repair",
        "simple_repair_meets_development_limits",
    )
    assert scheduled["comparison_scheduled"] is True
    assert scheduled["frozen_arm"] == scheduled["rule_revision"]
    assert len(scheduled["comparison_plan_revision"]) == 64
    assert scheduled["fit_starts"] == 0


def test_an_empty_protected_population_is_not_a_scheduled_comparison(
    store: rr.RepairRoutingStore,
) -> None:
    # freeze_plan accepts an empty population, so without a check of its own the simple route
    # would report a comparison scheduled against a plan frozen over zero members.
    empty = rr.ComparisonScheduling(
        digest("generated-intake"), (), "generated-incumbent-1", "generated-audience"
    )
    held = store.evaluate(request(data_fix(), scheduling=empty))
    assert (held["outcome"], held["reason_code"]) == (
        "request_evidence",
        "comparison_scheduling_required",
    )
    assert held["comparison_scheduled"] is False and held["comparison_plan_revision"] is None
    assert held["frozen_arm"] is None


def test_a_slice_failure_also_names_the_missing_population(
    store: rr.RepairRoutingStore,
) -> None:
    # The slice failure keeps precedence, but a caller missing both learns both in one trip.
    both = store.evaluate(
        request(data_fix(), t=terms(critical_slices=("bucket:q",)), run="generated-run-a")
    )
    assert (both["outcome"], both["reason_code"]) == (
        "request_evidence",
        "critical_slice_unobserved",
    )
    assert both["next_evidence"] == [
        "supply_independent_evidence",
        "provide_comparison_scheduling",
    ]
    only_slice = store.evaluate(
        request(
            data_fix(),
            t=terms(critical_slices=("bucket:q",)),
            run="generated-run-b",
            scheduling=scheduling(),
        )
    )
    assert only_slice["next_evidence"] == ["supply_independent_evidence"]


@pytest.mark.parametrize(
    ("kw", "error", "code"),
    [
        ({"mandate": mandate(withdrawn=True)}, rr.RoutingError, "STANDING_MANDATE_UNAVAILABLE"),
        ({"mandate": mandate(expired=True)}, rr.RoutingError, "STANDING_MANDATE_UNAVAILABLE"),
        (
            {"mandate": mandate(uses=frozenset({"inspect"}))},
            rr.RoutingError,
            "EVALUATION_NOT_AUTHORIZED",
        ),
        ({"augmentation": "enabled"}, rr.RoutingError, "AUGMENTATION_NOT_SUPPORTED"),
    ],
)
def test_replay_cannot_return_what_a_fresh_decision_would_refuse(
    store: rr.RepairRoutingStore, kw: dict[str, Any], error: type[Exception], code: str
) -> None:
    # Withdrawal, expiry and the evaluate right are in neither the evaluation key nor the input
    # revision, so only a refusal that runs before the replay lookup can cover them.
    sched = scheduling()
    first = store.evaluate(request(data_fix(), scheduling=sched))
    assert first["outcome"] == "simple_repair"
    with pytest.raises(error, match=code):
        rr.decide(request(data_fix(), scheduling=sched, **kw))
    with pytest.raises(error, match=code):
        store.evaluate(request(data_fix(), scheduling=sched, **kw))
    assert store.count() == 1


def test_replay_refuses_a_protected_row_or_a_tampered_rule(
    store: rr.RepairRoutingStore,
) -> None:
    sched, cases = scheduling(), data_fix()
    store.evaluate(request(cases, scheduling=sched))
    tainted = [replace(cases[0], split="protected")] + cases[1:]
    for refused in (rr.decide, store.evaluate):
        with pytest.raises(rr.RoutingError, match="PROTECTED_ROW_SUPPLIED"):
            refused(request(tainted, scheduling=sched))
    forged = dict(rule(), mapping={"x": "b"})
    for refused in (rr.decide, store.evaluate):
        with pytest.raises(RepairError, match="rule_revision_mismatch"):
            refused(request(cases, scheduling=sched, rule=forged))
    assert store.count() == 1


def test_replayed_fit_eligibility_rechecks_the_signature(
    store: rr.RepairRoutingStore,
) -> None:
    # The verdict is a runtime check, not a digest input, so it is re-run rather than replayed.
    first = store.evaluate(request(independent_residual()))
    assert first["outcome"] == "fit_eligible"
    refusing = Verifier(False)
    with pytest.raises(rr.RoutingError, match="SIGNED_POLICY_REQUIRED"):
        store.evaluate(request(independent_residual(), verifier=refusing))
    assert refusing.calls == 1
    accepting = Verifier()
    again = store.evaluate(request(independent_residual(), verifier=accepting))
    assert (again["replayed"], again["outcome"]) == (True, "fit_eligible")
    assert accepting.calls == 1
    # A replayed non-fit receipt needs no signature, so the verifier is left alone.
    untouched = Verifier(False)
    sched = scheduling()
    store.evaluate(request(data_fix(), scheduling=sched, run="generated-run-s"))
    held = store.evaluate(
        request(data_fix(), scheduling=sched, run="generated-run-s", verifier=untouched)
    )
    assert (held["replayed"], held["outcome"]) == (True, "simple_repair")
    assert untouched.calls == 0


def test_a_stored_evaluation_from_an_older_router_is_never_replayed(
    store: rr.RepairRoutingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The input revision covers the request, not decide()'s logic, so a behaviour change would
    # otherwise keep handing back a receipt the current router would no longer produce.
    sched = scheduling()
    first = store.evaluate(request(data_fix(), scheduling=sched))
    assert first["router_revision"] == rr.ROUTER_REVISION
    monkeypatch.setattr(rr, "ROUTER_REVISION", "repair-routing.decide.generated-next")
    with pytest.raises(rr.RoutingError, match="EVALUATION_ROUTER_REVISION_CHANGED"):
        store.evaluate(request(data_fix(), scheduling=sched))
    assert store.count() == 1
    monkeypatch.undo()
    again = store.evaluate(request(data_fix(), scheduling=sched))
    assert (again["replayed"], again["outcome"]) == (True, "simple_repair")
    assert again["evaluation_id"] == first["evaluation_id"]


def test_replay_with_a_different_protected_population_conflicts(
    store: rr.RepairRoutingStore,
) -> None:
    # The receipt publishes a comparison plan digest over the population it was handed, so a
    # second trigger carrying a different one must not be answered with the first receipt.
    first = store.evaluate(request(data_fix(), scheduling=scheduling()))
    assert first["comparison_scheduled"] is True
    moved = scheduling("generated-protected-3", "generated-protected-4")
    with pytest.raises(rr.RoutingError, match="EVALUATION_CONFLICT"):
        store.evaluate(request(data_fix(), scheduling=moved))
    assert store.count() == 1


def test_replay_with_a_different_model_or_recipe_catalog_conflicts(
    store: rr.RepairRoutingStore,
) -> None:
    first = store.evaluate(request(independent_residual()))
    assert first["recipe_selection"]["selected"]["recipe"] == ["generated-recipe-sweep", 1]
    other = rr.ModelTarget("simulated:model-a", "label_correction", "generated-capability-9")
    drifted: list[dict[str, Any]] = [
        {"model": other},
        {"recipes": recipes()[:1]},
        {"recipes": (replace(recipes()[1], est_seconds_per_trial=120),)},
    ]
    for kw in drifted:
        with pytest.raises(rr.RoutingError, match="EVALUATION_CONFLICT"):
            store.evaluate(request(independent_residual(), **kw))
    assert store.count() == 1


def test_replay_with_an_unchanged_authority_still_returns_one_evaluation(
    store: rr.RepairRoutingStore,
) -> None:
    sched = scheduling()
    first = store.evaluate(request(data_fix(), scheduling=sched))
    again = store.evaluate(request(data_fix(), scheduling=sched))
    assert (first["replayed"], again["replayed"]) == (False, True)
    assert again["comparison_plan_revision"] == first["comparison_plan_revision"]
    assert store.count() == 1


# Observed through the interpreter's audit hook rather than by patching callables. Patching
# `builtins.open`, `os.open` and `sqlite3.connect` leaves `io.open` and `pathlib.Path.open` /
# `.read_text` / `.read_bytes` unobserved, so a read through any of those passed as zero
# protected reads. The `open` audit event is raised underneath all of them. Directory listing
# opens no file, so it is watched separately: `os.listdir`, `os.scandir` and `glob.glob` (and
# its `root_dir`/`dir_fd` form) each raise their own event, and `pathlib` iteration and globbing
# go through the first two. Process creation and every `socket.*` event (creation, bind,
# connect, name resolution, sends) are watched the same way.
_WATCHED = frozenset(
    {
        "open",
        "sqlite3.connect",
        "mmap.__new__",
        "os.listdir",
        "os.scandir",
        "glob.glob",
        "glob.glob/2",
        "subprocess.Popen",
        "os.system",
        "os.exec",
        "os.posix_spawn",
        "os.spawn",
        "os.fork",
        "os.forkpty",
    }
)
_WATCHED_PREFIXES = ("socket.",)
# Metadata probes raise no audit event, so these are wrapped for the duration of the window.
# `os.path.exists` / `isfile` / `isdir` and `pathlib.Path.stat` / `.exists` call through them.
_PROBES = ("stat", "lstat", "access")
_WATCHING: list[tuple[str, str]] | None = None


def _authorize(action: int, arg1: str | None, *_: object) -> int:
    # ATTACH opens a database file from C, under no `open` event, so it is seen here instead.
    # A filename passed as a bound parameter is not known when the statement is authorized.
    sink = _WATCHING
    if sink is not None and action == sqlite3.SQLITE_ATTACH:
        sink.append(("sqlite3.attach", "<bound parameter>" if arg1 is None else arg1))
    return sqlite3.SQLITE_OK


def _audit(event: str, args: tuple[object, ...]) -> None:
    sink = _WATCHING
    if sink is None:
        return
    if event not in _WATCHED and not event.startswith(_WATCHED_PREFIXES):
        return
    try:
        target = str(args[0]) if args else ""
        if event == "glob.glob/2" and args[2] is not None:
            # (pathname, recursive, root_dir, dir_fd): a relative pattern names nothing alone.
            target = os.path.join(str(args[2]), target)
    except Exception:  # pragma: no cover - a hook that raises would break the interpreter
        target = "<unprintable>"
    sink.append((event, target))


# An audit hook cannot be removed once added, so it stays installed for the session and is
# inert whenever `_WATCHING` is None.
sys.addaudithook(_audit)


class _Access:
    """Record every watched access while the context is open; see _WATCHED and _PROBES."""

    def __init__(self, patch: pytest.MonkeyPatch) -> None:
        self.seen: list[tuple[str, str]] = []
        # monkeypatch restores `_WATCHING` to None and the probes to the real functions on
        # context exit, re-arming the inert state without relying on this object's teardown.
        patch.setattr(sys.modules[__name__], "_WATCHING", self.seen)
        for name in _PROBES:
            patch.setattr(os, name, self._probe("os." + name, getattr(os, name)))
        # A connection opened in the window reports any ATTACH through its authorizer.
        connect = sqlite3.connect

        def connect_watched(*args: Any, **kw: Any) -> sqlite3.Connection:
            db = connect(*args, **kw)
            db.set_authorizer(_authorize)
            return db

        patch.setattr(sqlite3, "connect", connect_watched)

    def _probe(self, kind: str, real: Any) -> Any:
        def probe(path: Any, *args: Any, **kw: Any) -> Any:
            self.seen.append((kind, f"<fd {path}>" if type(path) is int else os.fsdecode(path)))
            return real(path, *args, **kw)

        return probe

    @property
    def kinds(self) -> set[str]:
        return {kind for kind, _ in self.seen}

    @property
    def targets(self) -> set[str]:
        return {target for _, target in self.seen}


# The durable path: create-or-open the journal, check its directory and file, connect.
DURABLE_KINDS = {"open", "os.stat", "sqlite3.connect"}


def test_routing_opens_nothing_and_reads_no_protected_rows(
    store: rr.RepairRoutingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Acceptance 7, observed rather than read back from the receipt's own zero: inside the
    # window the decision makes none of the watched calls (file open, sqlite connect or ATTACH
    # on a connection opened in the window, mmap, directory listing, os.stat / os.lstat /
    # os.access probes, process creation, any socket event), and the durable path touches only
    # the router's private evaluation journal and the directory that holds it.
    #
    # What this does not see, so the test does not claim it: reads on a descriptor or sqlite
    # connection that was already open before the window (including ATTACH on one, and a
    # descriptor inherited from a parent process), metadata probes made from C code (such as
    # DirEntry.stat or sqlite's own), any other syscall that raises no audit event, and
    # protected content already resident in memory. It shows none of the watched calls ran.
    sched = scheduling()
    req = request(data_fix(), scheduling=sched)
    with monkeypatch.context() as patch:
        watched = _Access(patch)
        receipt = rr.decide(req)
    assert watched.seen == []
    assert receipt["outcome"] == "simple_repair" and receipt["comparison_scheduled"] is True
    assert receipt["protected_reads"] == len(watched.seen) == 0
    body = canonical(receipt)
    assert not any(protected in body for protected in sched.protected_ids)

    with monkeypatch.context() as patch:
        watched = _Access(patch)
        stored = store.evaluate(req)
    assert watched.kinds == DURABLE_KINDS
    assert watched.targets == {str(store.path), str(store.path.parent)}
    assert (stored["protected_reads"], stored["fit_starts"], stored["reservations"]) == (0, 0, 0)


@pytest.mark.parametrize(
    ("how", "event"),
    [
        ("socket_new", "socket.__new__"),
        ("socket_bind", "socket.bind"),
        ("subprocess", "subprocess.Popen"),
        ("os_stat", "os.stat"),
        ("os_lstat", "os.lstat"),
        ("os_access", "os.access"),
        ("os_path_exists", "os.stat"),
        ("path_exists", "os.stat"),
        ("sqlite_attach", "sqlite3.attach"),
        ("sqlite_attach_bound", "sqlite3.attach"),
    ],
)
def test_the_monitor_catches_sockets_processes_probes_and_attach(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str, event: str
) -> None:
    # Each of these reaches outside the process or the file system without opening a file.
    sentinel = tmp_path / "generated-protected.sqlite3"
    sqlite3.connect(sentinel).close()
    real = comparison_plan.freeze_plan
    done: dict[str, object] = {}

    def leaky(**kw: Any) -> Any:
        if how == "socket_new":
            socket.socket(socket.AF_INET, socket.SOCK_STREAM).close()
        elif how == "socket_bind":
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.bind(("127.0.0.1", 0))
        elif how == "subprocess":
            subprocess.run([sys.executable, "-c", "pass"], check=True)
        elif how == "os_stat":
            os.stat(sentinel)
        elif how == "os_lstat":
            os.lstat(sentinel)
        elif how == "os_access":
            os.access(sentinel, os.R_OK)
        elif how == "os_path_exists":
            assert os.path.exists(sentinel)
        elif how == "path_exists":
            assert sentinel.exists()
        elif how == "sqlite_attach":
            with closing(sqlite3.connect(":memory:")) as db:
                db.execute(f"ATTACH DATABASE '{sentinel}' AS protected")
        else:
            with closing(sqlite3.connect(":memory:")) as db:
                db.execute("ATTACH DATABASE ? AS protected", (str(sentinel),))
        done["how"] = how
        return real(**kw)

    monkeypatch.setattr(comparison_plan, "freeze_plan", leaky)
    req = request(data_fix(), scheduling=scheduling())
    with monkeypatch.context() as patch:
        watched = _Access(patch)
        rr.decide(req)
    assert done["how"] == how
    assert event in watched.kinds, watched.seen
    if how.startswith(("os_", "path_")) or how == "sqlite_attach":
        assert (event, str(sentinel)) in watched.seen


@pytest.mark.parametrize("how", ["path_read_text", "path_read_bytes", "io_open", "path_open"])
def test_the_protected_read_monitor_catches_every_read_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str
) -> None:
    # Each of these four routes bypasses a patched `builtins.open` / `os.open`, so the monitor
    # has to observe the `open` audit event underneath them rather than the callables.
    sentinel = tmp_path / "generated-protected-rows.jsonl"
    sentinel.write_text('{"case_id":"generated-protected-1","label":"a"}\n')
    real = comparison_plan.freeze_plan
    read: dict[str, object] = {}

    def leaky(**kw: Any) -> Any:
        if how == "path_read_text":
            read["rows"] = sentinel.read_text()
        elif how == "path_read_bytes":
            read["rows"] = sentinel.read_bytes()
        elif how == "io_open":
            with io.open(sentinel) as handle:  # noqa: UP020 - the point is this exact route
                read["rows"] = handle.read()
        else:
            with sentinel.open() as handle:
                read["rows"] = handle.read()
        return real(**kw)

    monkeypatch.setattr(comparison_plan, "freeze_plan", leaky)
    req = request(data_fix(), scheduling=scheduling())
    with monkeypatch.context() as patch:
        watched = _Access(patch)
        rr.decide(req)
    assert read["rows"]
    assert ("open", str(sentinel)) in watched.seen


@pytest.mark.parametrize(
    ("how", "event"),
    [
        ("os_listdir", "os.listdir"),
        ("os_scandir", "os.scandir"),
        ("glob_glob", "glob.glob"),
        ("glob_glob_root_dir", "glob.glob/2"),
        ("path_iterdir", "os.listdir"),
        ("path_glob", "os.scandir"),
    ],
)
def test_the_monitor_catches_every_directory_listing_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, how: str, event: str
) -> None:
    # Listing a protected directory opens no file, so the `open` event alone would let a router
    # enumerate protected case ids unobserved. Each route must surface its own listing event.
    protected = tmp_path / "generated-protected"
    protected.mkdir()
    (protected / "generated-protected-1.jsonl").write_text("{}\n")
    real = comparison_plan.freeze_plan
    listed: dict[str, object] = {}

    def leaky(**kw: Any) -> Any:
        if how == "os_listdir":
            listed["names"] = os.listdir(protected)
        elif how == "os_scandir":
            with os.scandir(protected) as entries:
                listed["names"] = [e.name for e in entries]
        elif how == "glob_glob":
            listed["names"] = glob.glob(str(protected / "*.jsonl"))
        elif how == "glob_glob_root_dir":
            listed["names"] = glob.glob("*.jsonl", root_dir=protected)
        elif how == "path_iterdir":
            listed["names"] = [p.name for p in protected.iterdir()]
        else:
            listed["names"] = [p.name for p in protected.glob("*.jsonl")]
        return real(**kw)

    monkeypatch.setattr(comparison_plan, "freeze_plan", leaky)
    req = request(data_fix(), scheduling=scheduling())
    with monkeypatch.context() as patch:
        watched = _Access(patch)
        rr.decide(req)
    assert listed["names"]
    assert any(
        kind == event and target.startswith(str(protected)) for kind, target in watched.seen
    ), watched.seen


# OPE-568: grouped repair cases with identical features must agree. "Grouped" is the
# independence grouping (entity OR family, transitive, case-blind); case_id stays a
# case-sensitive row key.


def family_conflict(family_b: str = "Family-G") -> list[rr.DevelopmentCase]:
    """Two entities sharing only a family, identical features, conflicting references."""
    return independent_residual() + [
        case("ga", "x", "b", "a", entity="Entity-G1", family="Family-G"),
        case("gb", "x", "b", "b", entity="Entity-G2", family=family_b),
    ]


@pytest.mark.parametrize("family_b", ["Family-G", "family-g"])
def test_grouped_cases_sharing_only_a_family_must_agree(
    store: rr.RepairRoutingStore, family_b: str
) -> None:
    # Different entities, one family (also by case variant), identical features, conflicting
    # references: one group, so a conflict the customer must resolve before routing.
    r = store.evaluate(request(family_conflict(family_b)))
    assert (r["outcome"], r["reason_code"]) == ("request_evidence", "contradictory_labels")
    assert r["next_evidence"] == ["resolve_contradictory_labels"]
    assert r["residual_roots"] == [] and r["recipe_selection"] is None


def transitive_conflict() -> list[rr.DevelopmentCase]:
    """t1 and t3 share neither entity nor family; t2 chains them into one group."""
    return independent_residual() + [
        case("t1", "x", "b", "a", entity="generated-te1", family="generated-tf1"),
        case("t2", "w", "b", "a", entity="generated-te2", family="generated-tf1"),
        case("t3", "x", "b", "b", entity="generated-te2", family="generated-tf2"),
    ]


def test_transitively_grouped_cases_with_identical_features_must_agree(
    store: rr.RepairRoutingStore,
) -> None:
    r = store.evaluate(request(transitive_conflict()))
    assert (r["outcome"], r["reason_code"]) == ("request_evidence", "contradictory_labels")


def test_ungrouped_cases_with_identical_features_are_not_merged() -> None:
    # Distinct entity and distinct family: independent evidence, not a contradiction.
    base = independent_residual()
    extra = [
        case("ua", "x", "b", "a", entity="generated-ue1", family="generated-uf1"),
        case("ub", "x", "b", "b", entity="generated-ue2", family="generated-uf2"),
    ]
    before = rr.decide(request(base))
    r = rr.decide(request(base + extra))
    assert r["reason_code"] != "contradictory_labels"
    assert r["independent_groups"] == before["independent_groups"] + 2


@pytest.mark.parametrize(
    ("bucket_b", "ref_b"),
    [("w", "b"), ("x", "a")],
    ids=["different_features", "agreeing_labels"],
)
def test_grouped_cases_differing_in_features_or_agreeing_are_not_a_contradiction(
    bucket_b: str, ref_b: str
) -> None:
    cases = independent_residual() + [
        case("ha", "x", "b", "a", entity="Entity-H1", family="Family-H"),
        case("hb", bucket_b, "b", ref_b, entity="Entity-H2", family="family-h"),
    ]
    assert rr.decide(request(cases))["reason_code"] != "contradictory_labels"


def test_grouped_cases_matching_only_the_rule_feature_are_not_a_contradiction() -> None:
    # The key is the full feature map, not just the rule's feature: same bucket, a different
    # second feature, conflicting references, one group -> not identical evidence.
    cases = independent_residual() + [
        replace(
            case("ka", "x", "b", "a", entity="Entity-K1", family="Family-K"),
            features={"bucket": "x", "channel": "generated-p"},
        ),
        replace(
            case("kb", "x", "b", "b", entity="Entity-K2", family="family-k"),
            features={"bucket": "x", "channel": "generated-q"},
        ),
    ]
    assert rr.decide(request(cases))["reason_code"] != "contradictory_labels"


def unlabeled_family_sibling() -> list[rr.DevelopmentCase]:
    """A labeled case and an unlabeled one sharing only a family, with identical features."""
    return independent_residual() + [
        case("na", "x", "b", "a", entity="Entity-N1", family="Family-N"),
        case("nb", "x", "b", None, entity="Entity-N2", family="family-n"),
    ]


def test_an_unlabeled_grouped_sibling_holds_as_a_contradiction() -> None:
    # A missing reference disagrees with a present one inside a group, so this now holds as
    # contradictory_labels (checked first) rather than unusable_labels. Both are holds.
    r = rr.decide(request(unlabeled_family_sibling()))
    assert (r["outcome"], r["reason_code"]) == ("request_evidence", "contradictory_labels")
    # Ungrouped, the same unlabeled row is not a contradiction, only an unusable label.
    apart = independent_residual() + [
        case("na", "x", "b", "a", entity="Entity-N1", family="Family-N1"),
        case("nb", "x", "b", None, entity="Entity-N2", family="Family-N2"),
    ]
    r = rr.decide(request(apart))
    assert (r["outcome"], r["reason_code"]) == ("request_evidence", "unusable_labels")


def test_case_id_stays_a_case_sensitive_row_key() -> None:
    """Case-variant ids are separate rows, while grouping stays case-blind (decision 2).

    ``Case-1`` and ``case-1`` are two distinct development rows: case_id is a case-sensitive row
    key and nothing deduplicates them by entity and features. The independence grouping that
    the contradiction check keys on is case-blind, so the same two rows also fall into one group
    and must agree. That overlap is intended by decision 2 on OPE-568, not a defect: the rows
    stay countable as rows, and only their group membership is shared.
    """
    base = independent_residual()
    row = case("cs", "x", "b", "a", entity="generated-entity-cs", family="generated-family-cs")
    upper, lower = replace(row, case_id="Case-1"), replace(row, case_id="case-1")
    before = rr.decide(request(base))
    r = rr.decide(request(base + [upper, lower]))
    # Two rows, not deduplicated, and in one group with each other.
    assert r["development_cases"] == before["development_cases"] + 2
    assert r["independent_groups"] == before["independent_groups"] + 1
    assert r["reason_code"] != "contradictory_labels"
    with pytest.raises(rr.RoutingError, match="DUPLICATE_CASE"):
        rr.decide(request(base + [upper, replace(row, case_id="Case-1")]))
    conflicting = rr.decide(request(base + [upper, replace(lower, reference="b")]))
    assert conflicting["reason_code"] == "contradictory_labels"


def _typed_pair(a: Any, b: Any) -> list[rr.DevelopmentCase]:
    """Two grouped cases whose feature `n` differs only in type, with conflicting references."""
    return [
        replace(
            case("pa", "x", "b", "a", entity="Entity-P1", family="Family-P"),
            features={"bucket": "x", "n": a},
        ),
        replace(
            case("pb", "x", "b", "b", entity="Entity-P2", family="Family-P"),
            features={"bucket": "x", "n": b},
        ),
    ]


def _with_features(features: dict[Any, Any]) -> list[rr.DevelopmentCase]:
    return independent_residual() + [replace(case("ft", "z", "b", "a"), features=features)]


def _recipe_catalog() -> tuple[rr.Recipe, ...]:
    """One recipe per selection rejection reason, plus the two viable defaults."""
    linear = recipes()[0]
    return recipes() + (
        replace(linear, recipe_id="generated-recipe-other-model", models=frozenset({"other"})),
        replace(linear, recipe_id="generated-recipe-other-task", tasks=frozenset({"other"})),
        replace(linear, recipe_id="generated-recipe-paid", paid_probe=True),
        replace(linear, recipe_id="generated-recipe-slow", est_seconds_per_trial=100_000),
        replace(linear, recipe_id="generated-recipe-linear-b", est_seconds_per_trial=700),
    )


def _pinned_requests() -> list[tuple[str, rr.RoutingRequest]]:
    """The 37 pinned fixtures: every outcome, reason code, next action, recipe rejection reason
    and authority hold decide() can produce, each refusal code raised by the functions that
    test_every_code_the_router_can_emit_is_pinned reads, and each OPE-568 grouping and typing
    shape."""
    cs = case("cs", "x", "b", "a", entity="generated-entity-cs", family="generated-family-cs")
    upper, lower = replace(cs, case_id="Case-1"), replace(cs, case_id="case-1")
    second_feature = [
        replace(
            case("ka", "x", "b", "a", entity="Entity-K1", family="Family-K"),
            features={"bucket": "x", "channel": "generated-p"},
        ),
        replace(
            case("kb", "x", "b", "b", entity="Entity-K2", family="family-k"),
            features={"bucket": "x", "channel": "generated-q"},
        ),
    ]
    ungrouped = [
        case("ua", "x", "b", "a", entity="generated-ue1", family="generated-uf1"),
        case("ub", "x", "b", "b", entity="generated-ue2", family="generated-uf2"),
    ]
    empty = rr.ComparisonScheduling(
        digest("generated-intake"), (), "generated-incumbent-1", "generated-audience"
    )
    missing = independent_residual()
    missing[0] = case("fx0", "x", "b", "a", context_state="missing")
    slice_wrong = group("fx", 6, "x", "b", "a") + [case("sf", "x", "a", "b")] + correct()
    imbalance = group("fx", 2, "x", "b", "a") + group("rz", 3, "z", "b", "a")
    imbalance += group("ok", 1, "y", "b", "b")
    residual = independent_residual()
    return [
        # The 16 OPE-568 fixtures recorded with v2 and v3, unchanged.
        ("simple_repair", request(data_fix(), scheduling=scheduling())),
        ("simple_repair_unscheduled", request(data_fix())),
        ("simple_repair_empty_population", request(data_fix(), scheduling=empty)),
        ("fit_eligible", request(residual)),
        ("same_entity_residual", request(same_entity_residual())),
        ("correct_only", request(correct())),
        (
            "keep_incumbent",
            request(
                group("ok", 8, "y", "b", "b") + group("er", 1, "z", "b", "a"),
                t=terms(max_wrong_automatic_bps=2_000),
            ),
        ),
        ("slice_unobserved", request(data_fix(), t=terms(critical_slices=("bucket:q",)))),
        ("family_conflict_exact", request(family_conflict())),
        ("family_conflict_case_variant", request(family_conflict("family-g"))),
        ("transitive_conflict", request(transitive_conflict())),
        ("unlabeled_family_sibling", request(unlabeled_family_sibling())),
        ("ungrouped_identical_features", request(residual + ungrouped)),
        ("grouped_second_feature_differs", request(residual + second_feature)),
        ("case_variant_ids_agree", request(residual + [upper, lower])),
        (
            "case_variant_ids_conflict",
            request(residual + [upper, replace(lower, reference="b")]),
        ),
        # One per remaining reason code and authority hold.
        ("stale_documentation", request(residual, t=terms(documentation_revision="generated-d0"))),
        ("missing_decision_time_context", request(missing)),
        ("unusable_labels", request(residual + [case("nolabel", "z", "b", None)])),
        (
            "critical_slice_failed",
            request(
                slice_wrong,
                t=terms(critical_slices=("bucket:x",), max_wrong_automatic_bps=1_000),
                scheduling=scheduling(),
            ),
        ),
        (
            "insufficient_independent_development_evidence",
            request(residual, t=terms(min_development_cases=50)),
        ),
        ("residual_below_material_threshold", request(group("rz", 1, "z", "b", "a") + correct())),
        ("label_imbalance_inadequate_group_support", request(imbalance)),
        (
            "training_rights_required",
            request(residual, mandate=mandate(uses=frozenset({"inspect", "evaluate"}))),
        ),
        ("training_budget_required", request(residual, mandate=mandate(reserved_fits=3))),
        (
            "qualified_model_required",
            request(residual, model=rr.ModelTarget("simulated:model-a", "label_correction", None)),
        ),
        ("unsupported_recipe", request(residual, recipes=())),
        ("signed_policy_required", request(residual, verifier=Verifier(False))),
        (
            "recipe_rejection_reasons",
            request(residual, recipes=_recipe_catalog(), mandate=mandate(max_fits=1)),
        ),
        # Refusals raised by decide() itself, and the v4 feature typing.
        (
            "critical_slice_outside_permitted_feature",
            request(data_fix(), t=terms(critical_slices=("other:x",)), scheduling=scheduling()),
        ),
        ("typed_int_vs_float", request(residual + _typed_pair(1, 1.0))),
        ("typed_bool_vs_int", request(residual + _typed_pair(True, 1))),
        ("typed_zero_vs_negative_zero", request(residual + _typed_pair(0.0, -0.0))),
        ("typed_same_value_conflict", request(residual + _typed_pair(1, 1))),
        ("feature_tuple", request(_with_features({"bucket": "z", "n": (1, 2)}))),
        ("feature_nan", request(_with_features({"bucket": "z", "n": math.nan}))),
        ("feature_mixed_key", request(_with_features({"bucket": "z", 1: "a"}))),
    ]


def _pinned_result(req: rr.RoutingRequest) -> list[Any]:
    try:
        body = rr.decide(req)
    except rr.RoutingError as refused:
        return ["refused", refused.code.lower()]
    except (ValueError, TypeError) as error:
        # An untyped failure (what v3 raised for NaN or a mixed-type key); only its class is
        # recorded, because its message is the interpreter's and may change between versions.
        return ["error", type(error).__name__]
    # Both stamps move on a bump alone (`revision` digests a body that carries the router
    # revision), so neither is part of what the ledger pins.
    return ["decided", {k: v for k, v in body.items() if k not in ("router_revision", "revision")}]


def pinned_decisions() -> list[list[Any]]:
    """The routing outputs over the pinned fixtures, with no revision stamp in them.

    `router_revision` and `revision` are both left out, so this changes only when an outcome,
    reason code, recorded body or refusal changes, never merely because the constant moved.
    """
    return [[name, _pinned_result(req)] for name, req in _pinned_requests()]


# One row per router revision, oldest first: the digest of pinned_decisions() as that
# revision's router produced it. APPEND a row when ROUTER_REVISION is bumped.
#
# The rows are tied to this version of _pinned_requests(). Adding or changing a fixture means
# recomputing EVERY row by running each revision's router over the new set: v2 is the router
# as merged for OPE-417 (main at 629cd0e), v3 the OPE-568 router (9ce7d49), v4 this one. A row
# is never edited for any other reason.
_DECISION_LEDGER = {
    "repair-routing.decide.v2": "1d59ba305cc086b879d7ab4e603ea1455dbee106fb3ee255b05a5616e724e57d",
    "repair-routing.decide.v3": "386872152fdd0c978fd5f32fd38b5b2e08a9c22f8ff1c3f758b883a5c1ad35c6",
    "repair-routing.decide.v4": "839ae944fdca744cc095a601eeb93304c5a59e176d4f019f476f501ca2f13a75",
}


def test_routing_outputs_cannot_change_without_a_router_revision_bump() -> None:
    # A stored evaluation is refused on replay only when ROUTER_REVISION moved, so a change to
    # routing outputs under an unchanged revision would silently replay stale decisions.
    assert list(_DECISION_LEDGER)[-1] == rr.ROUTER_REVISION, (
        "ROUTER_REVISION changed: append its row to _DECISION_LEDGER"
    )
    assert digest(pinned_decisions()) == _DECISION_LEDGER[rr.ROUTER_REVISION], (
        "routing outputs changed on the pinned fixtures: bump ROUTER_REVISION and append a row"
    )
    # The digest carries no revision stamp, so a bump the fixtures cannot observe repeats the
    # previous row and fails here: the fixture set must grow to cover the change.
    assert len(set(_DECISION_LEDGER.values())) == len(_DECISION_LEDGER)


# The functions whose string literals are the router's emitted vocabulary.
_VOCABULARY_SOURCES = (
    "decide",
    "_slice_failure",
    "select_recipe",
    "_feature_value",
    "_feature_map",
)
# Literals in those functions that are dict keys, inputs or type tags, never an emitted code.
_STRUCTURAL = frozenset(
    {
        ":",
        "allowed_params",
        "assessed",
        "automatic",
        "bool",
        "budget_estimate_seconds",
        "capability_receipt_ref",
        "case_id",
        "critical_slices",
        "dict",
        "direction",
        "excluded_origin",
        "feature",
        "features",
        "float",
        "group_id",
        "incumbent",
        "int",
        "label",
        "list",
        "metric",
        "model_ref",
        "no_change",
        "none",
        "none_before_freeze",
        "observed",
        "protected_access",
        "reason",
        "recipe",
        "reference",
        "rejected",
        "residual",
        "residual_groups",
        "route",
        "selected",
        "selection",
        "simple",
        "status",
        "step_limit",
        "stopping_rule",
        "str",
        "task",
        "train",
        "trial_limit",
        "validation_split",
    }
)


def _source_literals() -> set[str]:
    tree = ast.parse(Path(rr.__file__).read_text())
    found: set[str] = set()
    functions = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in _VOCABULARY_SOURCES
    ]
    assert sorted(f.name for f in functions) == sorted(_VOCABULARY_SOURCES)
    for fn in functions:
        doc = ast.get_docstring(fn, clean=False)
        for node in ast.walk(fn):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value != doc:
                    found.add(node.value)
    return found


def _emitted(results: list[list[Any]]) -> set[str]:
    out: set[str] = set()
    for _, (kind, value) in results:
        if kind == "refused":
            out.add(value)
        elif kind == "decided":
            out |= {value["outcome"], value["reason_code"], *value["next_evidence"]}
            selection = value["recipe_selection"] or {"rejected": []}
            out |= {item["reason"] for item in selection["rejected"]}
    return out


def test_every_code_the_router_can_emit_is_pinned() -> None:
    # Every string literal in the routing functions is either structural or an outcome, reason
    # code, next action, recipe rejection reason or refusal code that some pinned fixture
    # produces. A new code fails here until a fixture produces it, and then moves the ledger.
    results = pinned_decisions()
    emitted = _emitted(results)
    literals = _source_literals()
    assert not emitted & _STRUCTURAL
    assert _STRUCTURAL <= literals, sorted(_STRUCTURAL - literals)
    vocabulary = literals - _STRUCTURAL
    assert vocabulary - emitted == set(), "unpinned codes"
    assert emitted - vocabulary == set(), "emitted codes the source does not name"
    assert set(rr.OUTCOMES) <= emitted
    # Under the current router every failure is a typed refusal.
    assert [name for name, (kind, _) in results if kind == "error"] == []
    assert len(results) == len({name for name, _ in results})


_V2_ROUTER_REVISION = "repair-routing.decide.v2"
# What the v2 router decided for family_conflict(), recorded by running it (OPE-417 as merged).
_V2_FAMILY_CONFLICT = {"outcome": "fit_eligible", "reason_code": "independent_material_residual"}


def test_a_decision_stored_by_the_v2_router_is_refused_not_reinterpreted(
    store: rr.RepairRoutingStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    # OPE-568 changes the outcome for a family-only conflict. A receipt the v2 router stored for
    # it must not be replayed, and must not be silently re-decided over the durable record.
    assert rr.ROUTER_REVISION != _V2_ROUTER_REVISION
    req = request(family_conflict())
    with monkeypatch.context() as patch:
        patch.setattr(rr, "ROUTER_REVISION", _V2_ROUTER_REVISION)
        written = store.evaluate(req)
    assert written["router_revision"] == _V2_ROUTER_REVISION
    # Give the row the outcome the v2 router produced for this request: v2 keyed the check on
    # entity plus features, so it missed the family-only conflict and routed the residual to fit.
    old = {k: v for k, v in written.items() if k != "replayed"} | _V2_FAMILY_CONFLICT
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute("UPDATE evaluations SET body=?", (canonical(old),))
    with closing(sqlite3.connect(store.path)) as db:
        before = db.execute("SELECT * FROM evaluations").fetchall()

    with pytest.raises(rr.RoutingError, match="EVALUATION_ROUTER_REVISION_CHANGED"):
        store.evaluate(req)
    assert store.count() == 1
    with closing(sqlite3.connect(store.path)) as db:
        assert db.execute("SELECT * FROM evaluations").fetchall() == before
    # The durable record is kept exactly as the old router left it ...
    kept = store.read(written["evaluation_id"])
    assert kept is not None and kept["router_revision"] == _V2_ROUTER_REVISION
    assert {k: kept[k] for k in _V2_FAMILY_CONFLICT} == _V2_FAMILY_CONFLICT
    # Nor does the eligibility read take the old router's fit_eligible as today's verdict.
    eligibility = store.current_eligibility(
        written["evaluation_id"], snapshot=req.snapshot, mandate=req.mandate
    )
    assert (eligibility["eligible"], eligibility["reason_code"]) == (
        False,
        "evaluation_router_revision_changed",
    )
    # ... and the current router, deciding afresh, holds the conflict instead.
    fresh = rr.decide(req)
    assert (fresh["outcome"], fresh["reason_code"]) == ("request_evidence", "contradictory_labels")


_ABSENT = object()
# The revision after the current one, derived so that it is never the current value itself.
_PREFIX, _, _NUMBER = rr.ROUTER_REVISION.rpartition(".v")
_NEXT_ROUTER_REVISION = f"{_PREFIX}.v{int(_NUMBER) + 1}"


@pytest.mark.parametrize(
    "stamp",
    [
        _ABSENT,
        None,
        [rr.ROUTER_REVISION],
        4,
        rr.ROUTER_REVISION.upper(),
        rr.ROUTER_REVISION + " ",
        _NEXT_ROUTER_REVISION,
        "repair-routing.decide.v3",
        _V2_ROUTER_REVISION,
    ],
    ids=["absent", "none", "list", "int", "upper", "trailing_space", "next", "v3", "v2"],
)
def test_only_the_exact_current_router_revision_is_replayed_or_eligible(
    store: rr.RepairRoutingStore, stamp: object
) -> None:
    # A row from before OPE-417 carries no stamp at all; anything but an exact, same-typed
    # match of the current value is another router's verdict, on replay and on eligibility.
    req = request(independent_residual())
    first = store.evaluate(req)
    assert first["outcome"] == "fit_eligible"
    assert store.evaluate(req)["replayed"] is True
    current = store.current_eligibility(
        first["evaluation_id"], snapshot=req.snapshot, mandate=req.mandate
    )
    assert current["eligible"] is True
    with closing(sqlite3.connect(store.path)) as db, db:
        (raw,) = db.execute("SELECT body FROM evaluations").fetchone()
        body = json.loads(raw)
        if stamp is _ABSENT:
            del body["router_revision"]
        else:
            body["router_revision"] = stamp
        db.execute("UPDATE evaluations SET body=?", (canonical(body),))

    with pytest.raises(rr.RoutingError, match="EVALUATION_ROUTER_REVISION_CHANGED"):
        store.evaluate(req)
    held = store.current_eligibility(
        first["evaluation_id"], snapshot=req.snapshot, mandate=req.mandate
    )
    assert (held["eligible"], held["reason_code"]) == (False, "evaluation_router_revision_changed")
    assert store.count() == 1


@pytest.mark.parametrize(
    ("a", "b"),
    [(1, 1.0), (True, 1), (0.0, -0.0), ([1], [1.0]), ({"k": 1}, {"k": True})],
    ids=["int_float", "bool_int", "zero_negative_zero", "nested_list", "nested_dict"],
)
def test_python_equal_features_of_different_types_are_different_evidence(a: Any, b: Any) -> None:
    # 1 == 1.0, True == 1 and 0.0 == -0.0 in Python, but they are different values of different
    # types (or signs), so the grouped pair is not identical evidence and cannot conflict.
    residual = independent_residual()
    assert a == b
    apart = rr.decide(request(residual + _typed_pair(a, b)))
    assert (apart["outcome"], apart["reason_code"]) == (
        "fit_eligible",
        "independent_material_residual",
    )
    # The same value of the same type, with conflicting references, is a contradiction.
    same = rr.decide(request(residual + _typed_pair(a, a)))
    assert (same["outcome"], same["reason_code"]) == ("request_evidence", "contradictory_labels")


def _nested(depth: int) -> Any:
    value: Any = "generated-leaf"
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    ("features", "code"),
    [
        ({"bucket": "z", "n": math.nan}, "INVALID_FEATURE_VALUE"),
        ({"bucket": "z", "n": math.inf}, "INVALID_FEATURE_VALUE"),
        ({"bucket": "z", "n": [1, -math.inf]}, "INVALID_FEATURE_VALUE"),
        ({"bucket": "z", "n": (1, 2)}, "INVALID_FEATURE_VALUE"),
        ({"bucket": "z", "n": {1, 2}}, "INVALID_FEATURE_VALUE"),
        ({"bucket": "z", "n": b"generated"}, "INVALID_FEATURE_VALUE"),
        ({"bucket": "z", "n": _nested(64)}, "INVALID_FEATURE_VALUE"),
        ({"bucket": "z", 1: "a"}, "INVALID_FEATURE_KEY"),
        ({"bucket": "z", "n": {1: "a", "k": "b"}}, "INVALID_FEATURE_KEY"),
    ],
    ids=[
        "nan",
        "inf",
        "nested_inf",
        "tuple",
        "set",
        "bytes",
        "too_deep",
        "mixed_key",
        "nested_mixed_key",
    ],
)
def test_unrepresentable_features_are_refused_with_a_typed_code(
    store: rr.RepairRoutingStore, features: dict[Any, Any], code: str
) -> None:
    # Refused in the preflight, so no receipt is decided, stored or replayed for these.
    req = request(_with_features(features))
    with pytest.raises(rr.RoutingError, match=code):
        rr.decide(req)
    with pytest.raises(rr.RoutingError, match=code):
        store.evaluate(req)
    assert store.count() == 0


def test_a_v3_receipt_over_a_tuple_feature_is_refused_on_replay(
    store: rr.RepairRoutingStore,
) -> None:
    # v3 routed a tuple feature as if it were a list. A receipt it stored is refused by the
    # preflight before the replay lookup, and the row is left as it was.
    req = request(_with_features({"bucket": "z", "n": (1, 2)}))
    key = rr.evaluation_key(req)
    old = {"router_revision": "repair-routing.decide.v3", "outcome": "fit_eligible"}
    store.count()  # creates the private journal
    with closing(sqlite3.connect(store.path)) as db, db:
        db.execute(
            "INSERT INTO evaluations VALUES(?,?,?,?)",
            (key, "eval-" + key[:40], "generated-v3-input", canonical(old)),
        )
    with pytest.raises(rr.RoutingError, match="INVALID_FEATURE_VALUE"):
        store.evaluate(req)
    assert store.read("eval-" + key[:40]) == old
