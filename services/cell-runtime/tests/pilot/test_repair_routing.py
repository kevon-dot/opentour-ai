"""Generated sentinel cases for the recurring-error routing ladder (OPE-417).

Local and simulated only: no provider, protected set, ledger reservation or fit is touched.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import repair_routing as rr
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.simple_repair import declared_rule

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
    fix = store.evaluate(request(data_fix(), run="generated-run-a"))
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
    assert fix["frozen_arm"] == fix["rule_revision"]
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
    ok = store.evaluate(
        request(data_fix(), t=terms(critical_slices=("bucket:x",)), run="generated-run-1")
    )
    bad = store.evaluate(
        request(data_fix(), t=terms(critical_slices=("bucket:q",)), run="generated-run-2")
    )
    assert ok["outcome"] == "simple_repair"
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
