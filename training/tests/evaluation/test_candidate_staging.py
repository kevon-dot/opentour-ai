"""OPE-545 staging gate: paired, meaningful, independently verified, idempotent.

Fixture kind: generated sentinel data only (``generated-*`` ids). These tests prove software
behaviour of the gate and ledger. They are not provider, customer or real-EVE evidence, the
OPE-526 receipt and OPE-555 scorecard are consumed as opaque digest-bound references (neither is
merged), and nothing here stages, promotes or requests release of anything real.
"""

from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_training.evaluation.candidate_staging import (
    FROZEN_STATE,
    INCUMBENT,
    SIMPLE,
    TUNED,
    ArmRun,
    CandidateRecord,
    CaseResult,
    FreshConfirmation,
    FrozenBenchmark,
    HmacVerdictAuthority,
    IndependentVerdict,
    PreregisteredRule,
    SimulatedCrash,
    StagingDecision,
    StagingError,
    StagingPacket,
    SupersessionReceipt,
    _same_actor,
    confirmation_body,
    identity_key,
    verdict_body,
)
from opentour_training.evaluation.candidate_staging import StagingLedger as _Ledger
from opentour_training.evaluation.candidate_staging import evaluate as _evaluate

# Sentinel trust root, held by the test harness and never placed in a packet.
ISSUERS: dict[str, tuple[str, bytes]] = {
    "generated-independent-verifier": ("independent_verifier_service", b"generated-key-1"),
    "generated-verifier-tuned": ("independent_verifier_service", b"generated-key-2"),
    "generated-verifier-incumbent": ("independent_verifier_service", b"generated-key-3"),
    "generated-verifier-simple": ("independent_verifier_service", b"generated-key-4"),
    "generated-trainer": ("independent_verifier_service", b"generated-key-5"),
    "generated-owner": ("authenticated_human", b"generated-key-6"),
    "generated-human": ("authenticated_human", b"generated-key-7"),
}
AUTH = HmacVerdictAuthority(ISSUERS)


def attest(v: IndependentVerdict) -> IndependentVerdict:
    entry = ISSUERS.get(v.issuer_id)
    key = entry[1] if entry else b"generated-unregistered-key"
    return replace(v, attestation=HmacVerdictAuthority.mac(key, verdict_body(v)))


def attest_confirmation(c: FreshConfirmation) -> FreshConfirmation:
    entry = ISSUERS.get(c.actor_id)
    key = entry[1] if entry else b"generated-unregistered-key"
    return replace(c, attestation=HmacVerdictAuthority.mac(key, confirmation_body(c)))


def evaluate(p: StagingPacket) -> StagingDecision:
    return _evaluate(p, AUTH)


def StagingLedger(path: Path | None = None, **kw: object) -> _Ledger:  # noqa: N802
    return _Ledger(path, authority=AUTH, **kw)  # type: ignore[arg-type]


FAMILIES = 100
AUTOMATIC_FAMILIES = 80  # 160 automatic of 200 cases, as in the B4 shape
SLICE = "generated-critical-slice"
SELECTION = "sha256:" + "5" * 64
IDENTITY = "sha256:" + "a" * 64
FROZEN_SEQ, PRODUCED_SEQ, VERDICT_SEQ = 10, 20, 30
ARM_IDENTITY = {TUNED: IDENTITY, INCUMBENT: "sha256:" + "b" * 64, SIMPLE: "sha256:" + "c" * 64}


def _cases(n_families: int = FAMILIES) -> list[tuple[str, str]]:
    return [
        (f"generated-case-{f:03d}-{k}", f"generated-family-{f:03d}")
        for f in range(n_families)
        for k in range(2)
    ]


def rule(**kw: object) -> PreregisteredRule:
    base: dict[str, object] = {
        "rule_id": "generated-rule-1",
        "alpha": 0.05,
        "sides": 1,
        "min_effect": 0.0,
        "min_families": 20,
    }
    return PreregisteredRule(**{**base, **kw})  # type: ignore[arg-type]


def bench(
    r: PreregisteredRule | None = None,
    cases: list[tuple[str, str]] | None = None,
    **kw: object,
) -> FrozenBenchmark:
    cases = cases if cases is not None else _cases()
    base: dict[str, object] = {
        "benchmark_id": "generated-benchmark-1",
        "state": FROZEN_STATE,
        "scorer_version": "generated-scorer-1",
        "operating_point_id": "generated-operating-point-1",
        "evidence_tier": "generated",
        "rule_digest": (r or rule()).digest,
        "case_families": tuple(cases),
        "eligible_case_ids": tuple(c for c, _ in cases),
        "critical_slices": (SLICE,),
        "preregistered_selection_digest": SELECTION,
        "audit_receipt_ref": "simulated:audit-receipt-1",
        "audit_receipt_digest": "sha256:" + "9" * 64,
        "audit_verdict": "evaluation_allowed",
        "frozen_seq": FROZEN_SEQ,
    }
    return FrozenBenchmark(**{**base, **kw})  # type: ignore[arg-type]


def run(
    arm: str,
    b: FrozenBenchmark,
    wrong: set[str],
    *,
    auto_families: int = AUTOMATIC_FAMILIES,
    seq: int = PRODUCED_SEQ,
    cost: float = 1.0,
) -> ArmRun:
    results = []
    for case_id, family in b.case_families:
        number = int(family.rsplit("-", 1)[1])
        auto = number < auto_families
        in_slice = case_id.endswith("-0") and number < 5
        results.append(
            CaseResult(case_id, auto, auto and case_id in wrong, cost, (SLICE,) if in_slice else ())
        )
    identity = ARM_IDENTITY.get(arm, "sha256:" + "d" * 64)
    return ArmRun(arm, b.digest, identity, seq, tuple(results))


def verdict(arm: str, r: ArmRun, b: FrozenBenchmark, **kw: object) -> IndependentVerdict:
    base: dict[str, object] = {
        "verdict_id": f"simulated:verdict-{arm}",
        "arm_id": arm,
        "subject_identity_digest": r.serving_identity_digest,
        "manifest_digest": b.digest,
        "run_digest": r.results_digest,
        "issuer_id": "generated-independent-verifier",
        "issuer_kind": "independent_verifier_service",
        "issuer_authenticated": True,
        "status": "issued",
        "outcome": "reproduced",
        "issued_seq": VERDICT_SEQ,
    }
    return attest(IndependentVerdict(**{**base, **kw}))  # type: ignore[arg-type]


def wrong_set(prefix_families: int) -> set[str]:
    return {f"generated-case-{f:03d}-0" for f in range(prefix_families)}


def packet(
    *,
    incumbent_wrong: set[str] | None = None,
    simple_wrong: set[str] | None = None,
    tuned_wrong: set[str] | None = None,
    r: PreregisteredRule | None = None,
    b: FrozenBenchmark | None = None,
) -> StagingPacket:
    r = r or rule()
    b = b or bench(r)
    runs = {
        INCUMBENT: run(INCUMBENT, b, wrong_set(80) if incumbent_wrong is None else incumbent_wrong),
        SIMPLE: run(SIMPLE, b, wrong_set(60) if simple_wrong is None else simple_wrong),
        TUNED: run(TUNED, b, wrong_set(4) if tuned_wrong is None else tuned_wrong),
    }
    verdicts = tuple(verdict(a, runs[a], b) for a in (TUNED, INCUMBENT, SIMPLE))
    cand = CandidateRecord(
        "generated-candidate-1",
        IDENTITY,
        "generated-trainer",
        "generated-selector",
        True,
        True,
        5,
        SELECTION,
    )
    return StagingPacket(b, r, cand, runs, verdicts, protected_view_seq=25)


def reasons(p: StagingPacket) -> set[str]:
    return set(evaluate(p).reasons)


def crash(point: str) -> None:
    raise SimulatedCrash(point)


# ------------------------------------------------------------ A: positive


def test_a1_clean_win_against_both_controls_is_eligible_with_full_report() -> None:
    d = evaluate(packet())
    assert d.outcome == "eligible" and d.reasons == ()
    assert d.improvement_claim == "cleared_preregistered_rule_against_both_controls"
    for control in (INCUMBENT, SIMPLE):
        c = d.comparisons[control]
        assert c["coverage"] == [160, 200] and c["families"] == 80
        assert c["interval"]["lower"] > 0 and c["interval"]["descriptive_only"] is True
        assert {"fixes", "regressions", "cost_per_1000", "critical_slices", "effect"} <= set(c)
    assert d.comparisons[INCUMBENT]["control_wrong"] == 80
    assert d.comparisons[SIMPLE]["tuned_wrong"] == 4
    assert d.claim_scope == "implementation_only"
    assert (d.alerts_emitted, d.release_requests) == (0, 0)
    assert d.release_authority is False and d.customer_win_claim is False
    assert evaluate(packet()).digest == d.digest  # deterministic


def test_a2_absent_augmentation_is_unrun_and_does_not_block() -> None:
    d = evaluate(packet())
    assert d.optional_comparisons["augmented_vs_real_only"] == {
        "status": "unrun",
        "blocks_base_route": False,
    }
    p = packet()
    p2 = replace(
        p,
        optional_runs={
            "augmented": run("augmented", p.benchmark, wrong_set(6)),
            "real_only": run("real_only", p.benchmark, wrong_set(24)),
        },
    )
    d2 = evaluate(p2)
    assert d2.outcome == "eligible"
    assert d2.optional_comparisons["augmented"]["wrong"] == 6
    assert d2.optional_comparisons["real_only"]["wrong"] == 24


# ------------------------------------------------------------ B: meaningful


def test_b1_positive_point_estimate_with_interval_including_no_effect_is_held() -> None:
    d = evaluate(
        packet(incumbent_wrong=wrong_set(30), simple_wrong=wrong_set(26), tuned_wrong=wrong_set(24))
    )
    assert d.outcome == "hold" and d.improvement_claim == "none"
    assert "interval_does_not_clear_threshold" in d.reasons
    c = d.comparisons[INCUMBENT]
    assert c["effect"] > 0 and c["interval"]["lower"] <= 0  # B4-shaped 30/26/24 of 160


def test_b2_threshold_above_zero_is_enforced() -> None:
    assert "interval_does_not_clear_threshold" in reasons(packet(r=rule(min_effect=0.6)))


def test_b3_tie_favors_simple_and_loss_to_simple_hold() -> None:
    tie = packet(simple_wrong=wrong_set(4), tuned_wrong=wrong_set(4))
    assert "tie_favors_simple" in reasons(tie)
    lose = packet(simple_wrong=wrong_set(2), tuned_wrong=wrong_set(4))
    assert "tuned_loses_to_simple" in reasons(lose)
    assert evaluate(tie).outcome == "hold"


def test_b4_inconclusive_with_too_few_families_holds() -> None:
    assert "inconclusive_support" in reasons(packet(r=rule(min_families=81)))


def test_b5_all_sale_model_loses_to_rule_holds_with_no_alert_or_release() -> None:
    cases = [(f"generated-case-{f:03d}-0", f"generated-family-{f:03d}") for f in range(8)]
    r = rule(min_families=2)
    b = bench(r, cases)
    ids = [c for c, _ in cases]
    # model 4/8 correct (0/4 paired contrasts), rule 5/8 correct, incumbent 3/8 correct
    runs = {
        TUNED: run(TUNED, b, set(ids[4:]), auto_families=8),
        SIMPLE: run(SIMPLE, b, set(ids[5:]), auto_families=8),
        INCUMBENT: run(INCUMBENT, b, set(ids[3:]), auto_families=8),
    }
    vs = tuple(verdict(a, runs[a], b) for a in runs)
    cand = CandidateRecord("generated-all-sale", IDENTITY, "p", "s", True, True, 5, SELECTION)
    d = evaluate(StagingPacket(b, r, cand, runs, vs, 25))
    assert d.outcome == "hold" and "tuned_loses_to_simple" in d.reasons
    assert (d.alerts_emitted, d.release_requests) == (0, 0)


# ------------------------------------------------------------ C: paired / frozen


def test_c1_changed_or_narrowed_denominator_refused() -> None:
    p = packet()
    narrowed = replace(p.runs[TUNED], results=p.runs[TUNED].results[:-2])
    out = evaluate(replace(p, runs={**p.runs, TUNED: narrowed}))
    assert out.outcome == "refused" and "denominator_changed" in out.reasons
    assert out.comparisons == {} and out.improvement_claim == "none"
    simple = p.runs[SIMPLE]
    extra = replace(simple, results=(*simple.results, CaseResult("generated-extra", True, False)))
    assert "denominator_changed" in reasons(replace(p, runs={**p.runs, SIMPLE: extra}))
    dup = replace(simple, results=(*simple.results[:-1], simple.results[0]))
    assert "denominator_changed" in reasons(replace(p, runs={**p.runs, SIMPLE: dup}))


def test_c2_arm_scored_against_other_manifest_refused() -> None:
    p = packet()
    other = replace(p.runs[INCUMBENT], manifest_digest="sha256:" + "0" * 64)
    assert "manifest_mismatch" in reasons(replace(p, runs={**p.runs, INCUMBENT: other}))


def test_c3_fixed_coverage_required() -> None:
    p = packet()
    shifted = run(SIMPLE, p.benchmark, wrong_set(60), auto_families=79)
    assert "coverage_not_fixed" in reasons(replace(p, runs={**p.runs, SIMPLE: shifted}))


def test_c4_win_measured_before_freeze_refused() -> None:
    p = packet()
    early = replace(p.runs[TUNED], produced_seq=FROZEN_SEQ)
    out = evaluate(replace(p, runs={**p.runs, TUNED: early}))
    assert out.outcome == "refused" and "win_measured_before_freeze" in out.reasons


def test_c5_unfrozen_benchmark_or_changed_rule_refused() -> None:
    assert "benchmark_not_frozen" in reasons(packet(b=bench(state="draft")))
    assert "rule_not_frozen" in reasons(replace(packet(), rule=rule(alpha=0.2)))


def test_c6_missing_or_rejected_audit_receipt_refused() -> None:
    missing = bench(audit_receipt_ref=None, audit_receipt_digest=None, audit_verdict=None)
    assert "audit_receipt_missing" in reasons(packet(b=missing))
    assert "audit_receipt_not_allowed" in reasons(packet(b=bench(audit_verdict="stale")))


def test_c7_zero_eligible_comparisons_is_explicit_hold() -> None:
    b = bench(cases=[])
    p = StagingPacket(
        b,
        rule(),
        packet().candidate,
        {a: run(a, b, set()) for a in (TUNED, INCUMBENT, SIMPLE)},
        (),
        25,
    )
    d = evaluate(p)
    assert "no_eligible_comparisons" in d.reasons and d.outcome != "eligible"
    # eligible cases but none inside automatic coverage: no gradeable family
    q = packet()
    none_auto = {a: run(a, q.benchmark, set(), auto_families=0) for a in (TUNED, INCUMBENT, SIMPLE)}
    vs = tuple(verdict(a, none_auto[a], q.benchmark) for a in none_auto)
    out = evaluate(replace(q, runs=none_auto, verdicts=vs))
    assert out.outcome == "hold" and "inconclusive_support" in out.reasons


# ------------------------------------------------------------ D: simple comparator mutation


def test_d1_mutation_removing_simple_comparator_fails() -> None:
    p = packet()
    runs = {k: v for k, v in p.runs.items() if k != SIMPLE}
    out = evaluate(replace(p, runs=runs))
    assert out.outcome == "refused" and "simple_comparator_missing" in out.reasons
    out2 = evaluate(replace(p, rule=rule(required_controls=(INCUMBENT,))))
    assert out2.outcome != "eligible" and "simple_comparator_missing" in out2.reasons
    gone = {k: v for k, v in p.runs.items() if k != INCUMBENT}
    assert "incumbent_run_missing" in reasons(replace(p, runs=gone))
    no_tuned = {k: v for k, v in p.runs.items() if k != TUNED}
    assert "tuned_run_missing" in reasons(replace(p, runs=no_tuned))


# ------------------------------------------------------------ E: independent verdict


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"issuer_id": "generated-trainer"}, "verdict_self_issued"),
        ({"issuer_id": "generated-selector"}, "verdict_self_issued"),
        ({"status": "pending"}, "verdict_not_issued"),
        ({"issuer_kind": "model"}, "verdict_issuer_not_authority"),
        ({"issuer_authenticated": False}, "verdict_unauthenticated"),
        ({"outcome": "not_reproduced"}, "verdict_not_reproduced"),
        ({"run_digest": "sha256:" + "1" * 64}, "verdict_stale"),
        ({"manifest_digest": "sha256:" + "1" * 64}, "verdict_stale"),
        ({"subject_identity_digest": "sha256:" + "1" * 64}, "verdict_stale"),
        ({"issued_seq": PRODUCED_SEQ}, "verdict_before_output"),
    ],
)
def test_e1_bad_verdict_refuses_staging(change: dict[str, object], code: str) -> None:
    p = packet()
    for arm in (TUNED, SIMPLE):  # the tuned arm and a control arm
        vs = tuple(
            attest(replace(v, **change)) if v.arm_id == arm else v  # type: ignore[arg-type]
            for v in p.verdicts
        )
        out = evaluate(replace(p, verdicts=vs))
        assert out.outcome == "refused" and code in out.reasons


def test_e2_missing_or_superseded_verdict_refuses() -> None:
    p = packet()
    no_incumbent = tuple(v for v in p.verdicts if v.arm_id != INCUMBENT)
    assert "verdict_missing" in reasons(replace(p, verdicts=no_incumbent))
    assert "verdict_missing" in reasons(replace(p, verdicts=()))
    old = tuple(
        replace(v, superseded_by="simulated:verdict-new") if v.arm_id == TUNED else v
        for v in p.verdicts
    )
    assert "verdict_superseded" in reasons(replace(p, verdicts=old))
    dup = (*p.verdicts, replace(p.verdicts[0], verdict_id="simulated:verdict-other"))
    assert "verdict_ambiguous" in reasons(replace(p, verdicts=dup))


def test_e3_distinct_independent_issuers_per_arm_are_accepted() -> None:
    p = packet()
    vs = tuple(attest(replace(v, issuer_id=f"generated-verifier-{v.arm_id}")) for v in p.verdicts)
    assert evaluate(replace(p, verdicts=vs)).outcome == "eligible"


# ------------------------------------------------------------ F: identity, selection, guards


def test_f1_candidate_must_be_qualified_registered_and_identity_current() -> None:
    p = packet()
    unqualified = replace(p.candidate, qualified=False)
    assert "candidate_unqualified" in reasons(replace(p, candidate=unqualified))
    unregistered = replace(p.candidate, registered=False)
    assert "candidate_unregistered" in reasons(replace(p, candidate=unregistered))
    moved = replace(p.candidate, identity_digest="sha256:" + "e" * 64)
    out = evaluate(replace(p, candidate=moved))
    assert out.outcome == "hold" and "stale_identity" in out.reasons


def test_f2_selection_after_viewing_protected_answers_holds() -> None:
    p = packet()
    out = evaluate(replace(p, candidate=replace(p.candidate, selection_seq=26)))
    assert out.outcome == "hold" and "selection_after_protected_view" in out.reasons


def test_f3_changed_choice_needs_fresh_confirmation_from_other_actor() -> None:
    p = packet()
    changed = replace(p.candidate, selection_digest="sha256:" + "6" * 64)
    assert "fresh_confirmation_required" in reasons(replace(p, candidate=changed))
    good = attest_confirmation(
        FreshConfirmation(
            "generated-owner",
            True,
            changed.selection_digest,
            p.benchmark.digest,
            candidate_id=changed.candidate_id,
            identity_digest=changed.identity_digest,
            arm_id=TUNED,
        )
    )
    assert evaluate(replace(p, candidate=changed, fresh_confirmation=good)).outcome == "eligible"
    for bad in (
        attest_confirmation(replace(good, authenticated=False)),
        attest_confirmation(replace(good, actor_id="generated-selector")),
        attest_confirmation(replace(good, selection_digest="sha256:" + "7" * 64)),
        attest_confirmation(replace(good, manifest_digest="sha256:" + "7" * 64)),
        replace(good, selection_digest="sha256:" + "7" * 64),  # tampered after attestation
        replace(good, attestation=""),
        attest_confirmation(replace(good, actor_id="generated-unregistered")),
        attest_confirmation(replace(good, candidate_id="generated-candidate-2")),
        attest_confirmation(replace(good, identity_digest="sha256:" + "e" * 64)),
        attest_confirmation(replace(good, arm_id=INCUMBENT)),
    ):
        assert "fresh_confirmation_required" in reasons(
            replace(p, candidate=changed, fresh_confirmation=bad)
        )


def test_f4_critical_veto_and_unverified_slice_hold() -> None:
    # tuned regresses on a critical-slice case that the simple repair gets right
    p = packet(simple_wrong=wrong_set(60) - {"generated-case-000-0"}, tuned_wrong=wrong_set(4))
    out = evaluate(p)
    assert out.outcome == "hold" and "critical_veto" in out.reasons
    unobserved = bench(critical_slices=(SLICE, "generated-unobserved-slice"))
    assert "critical_slice_unverified" in reasons(packet(b=unobserved))


def test_f5_cost_cap_holds_but_costs_are_reported() -> None:
    out = evaluate(packet(r=rule(max_cost_per_1000=500.0)))
    assert "cost_cap_exceeded" in out.reasons
    assert out.comparisons[SIMPLE]["cost_per_1000"] == {"tuned": 1000.0, "control": 1000.0}


@pytest.mark.parametrize(
    "bad", [{"alpha": 0.9}, {"sides": 3}, {"min_effect": -0.1}, {"min_families": 1}]
)
def test_f6_invalid_rule_values_refused(bad: dict[str, object]) -> None:
    assert "invalid_rule" in reasons(packet(r=rule(**bad)))


# ------------------------------------------------------------ G: ledger, replay, crash


def ledger(tmp_path: Path, **kw: object) -> StagingLedger:
    return StagingLedger(tmp_path / "staging.json", **kw)  # type: ignore[arg-type]


def test_g1_replay_stages_exactly_once_across_restart(tmp_path: Path) -> None:
    p = packet()
    assert ledger(tmp_path).stage(p).status == "staged"
    again = StagingLedger(tmp_path / "staging.json")  # process restart
    assert again.is_staged(p.candidate.candidate_id, IDENTITY)
    before = again.snapshot()
    assert again.stage(p).status == "already_staged"
    assert again.snapshot() == before and len(before["staged"]) == 1


def test_g2_refused_or_held_packets_leave_no_state(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    before = led.snapshot()
    for p in (packet(simple_wrong=wrong_set(4)), packet(b=bench(state="draft"))):
        res = led.stage(p)
        assert res.status == "not_staged" and res.reasons
    assert led.snapshot() == before and not led.is_staged("generated-candidate-1", IDENTITY)


def test_g3_crash_between_decision_and_staging_leaves_nothing_half_staged(tmp_path: Path) -> None:
    p = packet()
    with pytest.raises(SimulatedCrash):
        ledger(tmp_path, fault=crash).stage(p)
    restarted = StagingLedger(tmp_path / "staging.json")
    assert not restarted.is_staged(p.candidate.candidate_id, IDENTITY)
    assert len(restarted.pending()) == 1 and restarted.snapshot()["staged"] == {}
    assert restarted.stage(p).status == "staged"  # replay completes
    assert restarted.pending() == () and restarted.stage(p).status == "already_staged"
    assert len(restarted.snapshot()["staged"]) == 1


def test_g4_stale_pending_intent_is_dropped_when_gate_now_fails(tmp_path: Path) -> None:
    p = packet()
    with pytest.raises(SimulatedCrash):
        ledger(tmp_path, fault=crash).stage(p)
    restarted = StagingLedger(tmp_path / "staging.json")
    revoked = tuple(replace(v, superseded_by="simulated:new") for v in p.verdicts)
    assert restarted.stage(replace(p, verdicts=revoked)).status == "not_staged"
    assert restarted.pending() == () and not restarted.is_staged("generated-candidate-1", IDENTITY)


def test_g5_late_and_duplicate_callbacks_change_nothing(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    p = packet()
    assert led.acknowledge("simulated:cb-0", "sha256:" + "f" * 64) == "ignored"  # late
    decision = led.stage(p).decision
    assert led.acknowledge("simulated:cb-1", decision.digest) == "recorded"
    frozen = led.snapshot()
    assert led.acknowledge("simulated:cb-1", decision.digest) == "ignored"  # duplicate
    assert led.acknowledge("simulated:cb-2", decision.digest) == "ignored"  # second sender
    assert led.snapshot() == frozen and led.is_staged("generated-candidate-1", IDENTITY)
    fresh = StagingLedger()  # an ack never stages anything
    assert fresh.acknowledge("simulated:cb-3", decision.digest) == "ignored"
    assert not fresh.is_staged("generated-candidate-1", IDENTITY)


def test_g6_superseded_benchmark_blocks_promotion_and_queues_reconciliation(
    tmp_path: Path,
) -> None:
    led = ledger(tmp_path)
    p = packet()
    assert led.stage(p).status == "staged"
    receipt = SupersessionReceipt(
        "simulated:supersede-1", p.benchmark.digest, "sha256:" + "2" * 64, "generated-owner", True
    )
    items = led.supersede_benchmark(receipt)
    assert [i["candidate_id"] for i in items] == ["generated-candidate-1"]
    assert not led.is_staged("generated-candidate-1", IDENTITY)
    assert led.supersede_benchmark(receipt) == items  # idempotent
    assert len(led.reconciliation()) == 1
    other = replace(p, candidate=replace(p.candidate, candidate_id="generated-candidate-2"))
    res = led.stage(other)
    assert res.status == "not_staged" and "benchmark_superseded" in res.reasons
    assert led.stage(p).status == "not_staged"
    with pytest.raises(StagingError):
        led.supersede_benchmark(replace(receipt, receipt_id="", issuer_authenticated=False))
    with pytest.raises(StagingError):
        led.supersede_benchmark(replace(receipt, successor_manifest_digest=p.benchmark.digest))


def test_g7_supersession_cancels_pending_intent(tmp_path: Path) -> None:
    p = packet()
    with pytest.raises(SimulatedCrash):
        ledger(tmp_path, fault=crash).stage(p)
    led = StagingLedger(tmp_path / "staging.json")
    receipt = SupersessionReceipt(
        "simulated:s", p.benchmark.digest, "sha256:" + "2" * 64, "generated-owner", True
    )
    led.supersede_benchmark(receipt)
    assert led.pending() == () and led.reconciliation() == ()


def test_g8_same_identity_under_different_decision_is_not_restaged(tmp_path: Path) -> None:
    led = ledger(tmp_path)
    assert led.stage(packet()).status == "staged"
    variant = packet(tuned_wrong=wrong_set(5))  # still a win, different decision digest
    assert evaluate(variant).outcome == "eligible"
    res = led.stage(variant)
    assert res.status == "not_staged"
    assert res.reasons == ("candidate_already_staged_or_under_reconciliation",)
    assert len(led.snapshot()["staged"]) == 1


def test_g9_decision_object_is_not_release_authority() -> None:
    d: StagingDecision = evaluate(packet())
    body = d.body()
    assert body["release_authority"] is False and body["release_requests"] == 0
    assert body["alerts_emitted"] == 0 and body["customer_win_claim"] is False


# ---------------------------------------------------- H: forgery and laundering


def _forged(p: StagingPacket, issuer: str = "generated-anyone") -> tuple[IndependentVerdict, ...]:
    """Self-consistent hand-built verdicts: every binding re-derived, asserted flag True."""
    out = []
    for arm in (TUNED, INCUMBENT, SIMPLE):
        r = p.runs[arm]
        v = IndependentVerdict(
            f"simulated:forged-{arm}",
            arm,
            r.serving_identity_digest,
            p.benchmark.digest,
            r.results_digest,
            issuer,
            "authenticated_human",
            True,
            "issued",
            "reproduced",
            99,
        )
        out.append(replace(v, attestation=HmacVerdictAuthority.mac(b"attacker", verdict_body(v))))
    return tuple(out)


def test_h1_self_consistent_forged_verdict_without_trust_root_is_not_eligible(
    tmp_path: Path,
) -> None:
    p = packet()
    forged = replace(p, verdicts=_forged(p))  # plausible issuer, asserted flag, valid digests
    out = evaluate(forged)
    assert out.outcome == "refused" and "verdict_unattested" in out.reasons
    unsigned = replace(p, verdicts=tuple(replace(v, attestation="") for v in p.verdicts))
    assert "verdict_unattested" in reasons(unsigned)
    # right key, wrong registered kind; and a registered issuer's mac over a tampered body
    wrong_kind = tuple(attest(replace(v, issuer_kind="authenticated_human")) for v in p.verdicts)
    assert "verdict_unattested" in reasons(replace(p, verdicts=wrong_kind))
    tampered = tuple(replace(v, issued_seq=v.issued_seq + 1) for v in p.verdicts)
    assert "verdict_unattested" in reasons(replace(p, verdicts=tampered))
    # with NO authority nothing can be eligible, through the pure gate or the ledger
    bare = _evaluate(p)
    assert bare.outcome == "refused" and "verdict_authority_unavailable" in bare.reasons
    led = _Ledger(tmp_path / "s.json")
    assert led.stage(p).status == "not_staged" and led.snapshot()["staged"] == {}
    assert _Ledger(tmp_path / "s2.json", authority=AUTH).stage(forged).status == "not_staged"


@pytest.mark.parametrize(
    "alias",
    [
        "generated-trainer ",
        "GENERATED-TRAINER",
        "generated-trainer​",
        "generated-selector\t",
        "Generated-Selector",
        "ｇenerated-selector",
        " generated-­selector",
    ],
)
def test_h2_producer_or_selector_cannot_be_laundered_into_verdict_authority(alias: str) -> None:
    p = packet()
    vs = tuple(attest(replace(v, issuer_id=alias)) for v in p.verdicts)
    out = evaluate(replace(p, verdicts=vs))
    assert out.outcome == "refused" and "verdict_self_issued" in out.reasons
    changed = replace(p.candidate, selection_digest="sha256:" + "6" * 64)
    conf = attest_confirmation(
        FreshConfirmation(alias, True, changed.selection_digest, p.benchmark.digest)
    )
    assert "fresh_confirmation_required" in reasons(
        replace(p, candidate=changed, fresh_confirmation=conf)
    )
    assert identity_key("") == "" and identity_key("A b​C") == "abc"
    empty = tuple(attest(replace(v, issuer_id="  ")) for v in p.verdicts)
    assert "verdict_self_issued" in reasons(replace(p, verdicts=empty))


def test_h3_freeze_audit_and_run_provenance_are_bound_by_digests() -> None:
    p = packet()
    b = p.benchmark
    for change in (
        {"frozen_seq": 0},
        {"audit_receipt_ref": "simulated:other"},
        {"audit_verdict": "other"},
        {"state": "draft"},
    ):
        assert replace(b, **change).digest != b.digest  # type: ignore[arg-type]
    t = p.runs[TUNED]
    assert replace(t, produced_seq=t.produced_seq + 1).results_digest != t.results_digest
    other_identity = replace(t, serving_identity_digest="sha256:" + "1" * 64)
    assert other_identity.results_digest != t.results_digest
    # a run re-dated after its verdict was attested no longer matches the attested run digest
    redated = replace(t, produced_seq=t.produced_seq + 1)
    assert "verdict_stale" in reasons(replace(p, runs={**p.runs, TUNED: redated}))


def test_h4_run_must_be_the_arm_it_is_filed_under() -> None:
    p = packet()
    swapped = {**p.runs, SIMPLE: p.runs[INCUMBENT]}  # incumbent reused as the "simple" control
    out = evaluate(replace(p, runs=swapped))
    assert out.outcome == "refused" and "arm_identity_mismatch" in out.reasons


def test_h5_critical_slice_tags_cannot_be_stripped_by_one_arm() -> None:
    p = packet(simple_wrong=wrong_set(60) - {"generated-case-000-0"}, tuned_wrong=wrong_set(4))
    assert "critical_veto" in reasons(p)  # honest tags: veto
    t = p.runs[TUNED]
    stripped = replace(t, results=tuple(replace(r, critical_slices=()) for r in t.results))
    vs = tuple(
        attest(replace(v, run_digest=stripped.results_digest)) if v.arm_id == TUNED else v
        for v in p.verdicts
    )
    out = evaluate(replace(p, runs={**p.runs, TUNED: stripped}, verdicts=vs))
    assert out.outcome == "refused" and "critical_slice_membership_inconsistent" in out.reasons
    assert out.comparisons == {}


def test_h6_selection_at_or_after_protected_view_holds() -> None:
    p = packet()
    for seq in (25, 26):
        out = evaluate(replace(p, candidate=replace(p.candidate, selection_seq=seq)))
        assert out.outcome == "hold" and "selection_after_protected_view" in out.reasons


def test_h7_concurrent_ledgers_on_one_file_stage_exactly_once(tmp_path: Path) -> None:
    p = packet()
    path = tmp_path / "staging.json"
    stale = StagingLedger(path)  # loaded before anything is staged: stale in-memory copy
    results: list[str] = []

    def worker() -> None:
        results.append(StagingLedger(path).stage(p).status)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    assert sorted(results) == ["already_staged"] * 5 + ["staged"]
    assert stale.stage(p).status == "already_staged"  # reloads durable state under the lock
    assert len(StagingLedger(path).snapshot()["staged"]) == 1
    # a supersession written by another writer is seen by a stale instance before it stages
    other = StagingLedger(path)
    other.supersede_benchmark(
        SupersessionReceipt("simulated:s", p.benchmark.digest, "sha256:" + "2" * 64, "o", True)
    )
    again = replace(p, candidate=replace(p.candidate, candidate_id="generated-candidate-9"))
    assert stale.stage(again).status == "not_staged"


# ------------------------------------------------ I: OPE-545 follow-up (identity, supersession)

# Variants of "generated-selector" that NFKC + casefold + strip(Z, C) did not collapse.
_EXOTIC_ALIASES = {
    "combining_grapheme_joiner": "generated-sel͏ector",
    "variation_selector": "generated-selector️",
    "combining_accent": "generated-seléctor",
    "hangul_filler": "generated-selectorㅤ",
    "halfwidth_hangul_filler": "generated-selectorﾠ",
    "braille_blank": "generated-selector⠀",
    "cyrillic_homoglyph": "generated-seleсtor",  # Cyrillic es, not Latin c
}


@pytest.mark.parametrize("alias", list(_EXOTIC_ALIASES.values()), ids=list(_EXOTIC_ALIASES))
def test_i1_exotic_codepoint_aliases_are_treated_as_the_same_actor(alias: str) -> None:
    assert _same_actor(alias, "generated-trainer", "generated-selector")
    p = packet()
    vs = tuple(attest(replace(v, issuer_id=alias)) for v in p.verdicts)
    out = evaluate(replace(p, verdicts=vs))
    assert out.outcome == "refused" and "verdict_self_issued" in out.reasons
    changed = replace(p.candidate, selection_digest="sha256:" + "6" * 64)
    conf = attest_confirmation(
        FreshConfirmation(alias, True, changed.selection_digest, p.benchmark.digest)
    )
    assert "fresh_confirmation_required" in reasons(
        replace(p, candidate=changed, fresh_confirmation=conf)
    )


@pytest.mark.parametrize(
    "actor",
    [
        "generated-trainer",
        "generated-selector",
        "generated-verifier-tuned",
        "generated-owner",
        "simulated:verdict-1",
        "generated-unregistered",
        "svc_a.b:c/d@e-1",
    ],
)
def test_i2_well_formed_ids_stay_distinct_from_other_actors(actor: str) -> None:
    assert not _same_actor(actor, "generated-other-actor", "generated-another")
    assert _same_actor(actor, actor)  # an actor is still itself
    assert _same_actor(actor.upper(), actor)  # case remains a collapse (safe direction)


def test_i2_real_format_ids_are_accepted_end_to_end() -> None:
    p = packet()
    assert evaluate(p).outcome == "eligible"
    vs = tuple(attest(replace(v, issuer_id=f"generated-verifier-{v.arm_id}")) for v in p.verdicts)
    assert evaluate(replace(p, verdicts=vs)).outcome == "eligible"


def test_i3_clearing_superseded_by_after_attestation_fails_attestation() -> None:
    p = packet()
    base = p.verdicts[0]
    marked = attest(replace(base, superseded_by="simulated:verdict-new"))
    assert AUTH.verify_verdict(marked)
    assert not AUTH.verify_verdict(replace(marked, superseded_by=None))  # cleared
    assert not AUTH.verify_verdict(replace(base, superseded_by="simulated:verdict-new"))
    assert verdict_body(marked) != verdict_body(base)
    # end to end: a revoked tuned verdict with the marker cleared is not accepted
    vs = tuple(
        replace(attest(replace(v, superseded_by="simulated:verdict-new")), superseded_by=None)
        if v.arm_id == TUNED
        else v
        for v in p.verdicts
    )
    out = evaluate(replace(p, verdicts=vs))
    assert out.outcome == "refused" and "verdict_unattested" in out.reasons


def test_i4_honest_round_trip_still_stages(tmp_path: Path) -> None:
    p = packet()
    assert evaluate(p).outcome == "eligible"
    assert ledger(tmp_path).stage(p).status == "staged"


def test_i3_self_asserted_flags_are_inside_the_attested_bodies() -> None:
    """Family with i3: no self-asserted field may sit outside what the authority attests."""
    base = packet().verdicts[0]
    for start, flipped in ((False, True), (True, False)):
        att = attest(replace(base, issuer_authenticated=start))
        assert AUTH.verify_verdict(att)
        assert not AUTH.verify_verdict(replace(att, issuer_authenticated=flipped))  # flipped after
        other = attest(replace(base, issuer_authenticated=flipped))
        assert not AUTH.verify_verdict(replace(att, attestation=other.attestation))  # cross-value
    assert verdict_body(replace(base, issuer_authenticated=False))["issuer_authenticated"] is False

    conf = FreshConfirmation("generated-owner", False, SELECTION, packet().benchmark.digest)
    for start, flipped in ((False, True), (True, False)):
        att_c = attest_confirmation(replace(conf, authenticated=start))
        assert AUTH.verify_confirmation(att_c)
        assert not AUTH.verify_confirmation(replace(att_c, authenticated=flipped))
        other_c = attest_confirmation(replace(conf, authenticated=flipped))
        assert not AUTH.verify_confirmation(replace(att_c, attestation=other_c.attestation))
    assert confirmation_body(conf)["authenticated"] is False


def _other_candidate_packet(p: StagingPacket, *, new_identity: bool) -> StagingPacket:
    """A second candidate claiming the same selection digest (and optionally its own identity)."""
    cand = replace(p.candidate, candidate_id="generated-candidate-2")
    if not new_identity:
        return replace(p, candidate=cand)
    ident = "sha256:" + "e" * 64
    run_t = replace(p.runs[TUNED], serving_identity_digest=ident)
    runs = {**p.runs, TUNED: run_t}
    verdicts = tuple(
        verdict(TUNED, run_t, p.benchmark) if v.arm_id == TUNED else v for v in p.verdicts
    )
    cand = replace(cand, identity_digest=ident, producer_id="generated-trainer-2")
    return replace(p, candidate=cand, runs=runs, verdicts=verdicts)


def _bound(c: CandidateRecord, p: StagingPacket) -> FreshConfirmation:
    return attest_confirmation(
        FreshConfirmation(
            "generated-owner",
            True,
            c.selection_digest,
            p.benchmark.digest,
            candidate_id=c.candidate_id,
            identity_digest=c.identity_digest,
            arm_id=TUNED,
        )
    )


def test_j1_confirmation_cannot_be_replayed_against_another_candidate() -> None:
    p = packet()
    a = replace(p.candidate, selection_digest="sha256:" + "6" * 64)
    pa = replace(p, candidate=a, fresh_confirmation=_bound(a, p))
    assert evaluate(pa).outcome == "eligible"  # honest confirmation for its own candidate
    for new_identity in (False, True):
        other = _other_candidate_packet(pa, new_identity=new_identity)
        assert other.candidate.selection_digest == a.selection_digest
        out = evaluate(other)
        assert out.outcome == "hold" and "fresh_confirmation_required" in out.reasons
    # an unbound (legacy-shape) confirmation never confirms anything
    legacy = attest_confirmation(
        FreshConfirmation("generated-owner", True, a.selection_digest, p.benchmark.digest)
    )
    assert "fresh_confirmation_required" in reasons(replace(pa, fresh_confirmation=legacy))
    # binding fields are inside the attested body
    for field_name in ("candidate_id", "identity_digest", "arm_id"):
        assert field_name in confirmation_body(pa.fresh_confirmation)  # type: ignore[arg-type]
