"""OPE-528 independent verdict core: claim chain, independent scorer, named controls, idempotency.

Generated sentinel fixtures only: case ids, features and labels are synthetic strings. Replay
arms are deterministic doubles; the open-weight adapter is exercised through injected entry
points because no SmolLM2 weights or numeric interpreter exist here. The ES256 key used in the
signing-seam test is an ephemeral test key, not an evaluator key. These tests prove software
behavior only and establish no provider, customer or separated-custody evidence.

Acceptance labels (C*, S*, K*, I*, X*) are this ticket's own mapping from the live OPE-528 text.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from opentour_cell.pilot import comparison_packet as cp
from opentour_cell.pilot import independent_verdict as iv
from opentour_cell.pilot import open_weight as ow
from opentour_cell.pilot.operating_point import digest

A, B = "SENTINEL_LABEL_ALPHA", "SENTINEL_LABEL_BRAVO"
WORKER = "generated-worker-1"
EVALUATOR = "generated-independent-evaluator-1"
PINS = {
    "base_sha256": ow.BASE_SHA256,
    "tokenizer_sha256": ow.TOKENIZER_SHA256,
    "inference_config_sha256": ow.PROFILE_SHA256,
    "reload": "fresh_process",
}


def sha(name: str) -> str:
    return digest(["generated-ope-528", name])


INCUMBENT = sha("incumbent-revision")
SIMPLE = sha("simple-repair-revision")
REAL_ONLY = sha("real-only-revision")
BENCHMARK = sha("benchmark-v1")
# Stand-ins for OPE-571 keyed slice digests. Neither these tests nor the evaluator hold the
# per-engagement slice key or the plain slice names; only substituted terms are exchanged.
CRITICAL = sha("keyed-slice-digest-critical-1")
ABSENT = sha("keyed-slice-digest-critical-absent")


def terms_for(critical=()):
    return {
        "minimum_coverage_bps": 7000,
        "error_ceiling_bps": 500,
        "minimum_verified_cases": 1,
        "minimum_slice_cases": 1,
        "critical_slices": sorted(critical),
        "material_slices": [],
    }


def fit_manifest(steps: int = 64, changed: list[int] | None = None, **identity_over):
    identity = {
        "profile": ow.PROFILE,
        "profile_sha256": ow.PROFILE_SHA256,
        "base_sha256": ow.BASE_SHA256,
        "revision": ow.REVISION,
        "source_sha256": sha("worker-source"),
        "adapter_sha256": sha("adapter-bytes"),
        "request_sha256": sha("fit-request"),
        "training_sha256": sha("training-rows"),
        "protected_partition_sha256": sha("protected"),
        "residual_revision": "generated-residual-r1",
        "grant_sha256": sha("grant"),
        **identity_over,
    }
    manifest = {
        "schema": "opentour.open_weight_fit.v1",
        "identity": identity,
        "artifact_sha256": ow._digest(identity),
        "training_rows": 32,
        "training": {"steps": steps, "changed_parameters": changed or [128, 64]},
        "incremental_compute_cost_cents": 0,
    }
    manifest["manifest_sha256"] = ow._digest(manifest)
    return manifest


MANIFEST = fit_manifest()
CANDIDATE = MANIFEST["artifact_sha256"]


# Transitions: first letter comparator, second candidate.
# R right automatic, W wrong automatic, D deferred, M missing, X malformed.
def _pred(code: str, truth: str):
    other = B if truth == A else A
    return {
        "R": {"status": "automatic", "label": truth},
        "W": {"status": "automatic", "label": other},
        "D": {"status": "review"},
        "M": None,
        "X": {"status": "automatic", "label": "not-a-registered-label"},
    }[code]


class Replay:
    def __init__(self, artifact, preds, *, served=None, alternate=None, manifest=None):
        self.artifact = artifact
        self.manifest = manifest or MANIFEST["manifest_sha256"]
        self.preds = preds
        self.served = served
        self.alternate = alternate
        self.calls: list[list[dict]] = []
        self.expected: list[str] = []

    def served_identity(self):
        if self.served is not None:
            return self.served
        return {"artifact_sha256": self.artifact, "manifest_sha256": self.manifest, **PINS}

    def predict(self, features, *, expected_artifact):
        self.calls.append([dict(f) for f in features])
        self.expected.append(expected_artifact)
        source = self.preds
        if self.alternate is not None and len(self.calls) % 2 == 0:
            source = self.alternate
        return [source[f["case_ref"]] for f in features]


class Scenario:
    """A generated population with per-arm transitions against one or more comparators."""

    def __init__(
        self,
        plan,
        *,
        comparators=("incumbent",),
        family_size=1,
        slice_of=None,
        prefix="",
        family_prefix=None,
    ):
        family_prefix = prefix if family_prefix is None else family_prefix
        cases, labels = [], {}
        self.preds = {"candidate": {}, **{c: {} for c in comparators}}
        index = 0
        for codes, count in plan:
            if isinstance(codes, str):
                codes = {comparators[0]: codes[0], "candidate": codes[1]}
            for _ in range(count):
                case_id = f"{prefix}case-{index:04d}"
                truth = A if index % 2 == 0 else B
                labels[case_id] = truth
                slices = slice_of(index, codes) if slice_of else ()
                cases.append(
                    iv.DecisionCase(
                        case_id,
                        f"{family_prefix}family-{index // family_size:04d}",
                        {"case_ref": case_id, "text": f"generated-input-{index}"},
                        tuple(slices),
                    )
                )
                for arm, code in codes.items():
                    self.preds[arm][case_id] = _pred(code, truth)
                index += 1
        self.population = iv.SealedPopulation(
            cases, labels, label_set=(A, B), benchmark_digest=BENCHMARK
        )
        self.comparators = comparators

    def replays(self, **over):
        digests = {"incumbent": INCUMBENT, "simple_repair": SIMPLE, "real_only": REAL_ONLY}
        out = {"candidate": Replay(CANDIDATE, self.preds["candidate"])}
        for c in self.comparators:
            out[c] = Replay(digests[c], self.preds[c])
        out.update(over)
        return out


def receipt_for(population, **over):
    current = {
        "canonical_inputs": population.inputs_digest,
        "answer_key": population.answer_key_digest,
        "split_manifest": sha("split"),
        "scoring_policy": sha("scoring-policy"),
    }
    body = {
        "schema_version": iv.RECEIPT,
        "verdict": "evaluation_allowed",
        "reasons": [],
        "evidence_origin": "generated",
        "bindings": {"frozen": dict(current), "current": dict(current)},
        "superseded_by": None,
        "successor_of": None,
    }
    body.update(over)
    body["receipt_digest"] = digest(body)
    body["receipt_id"] = "audit-" + body["receipt_digest"][:16]
    return body


def policy(**over):
    terms = terms_for(over.get("critical_slices", ()))
    values = {
        "policy_revision": "generated-policy-r1",
        "operating_point_digest": digest(terms),
        "operating_point_terms": terms,
        "minimum_improvement_bps": 200,
        "alpha_bps": 500,
        "coverage_floor_bps": 7000,
        "max_coverage_loss_bps": 500,
        "max_missing": 0,
        "min_families": 2,
        "required_comparators": ("incumbent",),
    }
    values.update(over)
    return iv.GatePolicy(**values)


def recompute_pairs(scenario, comparator):
    """Separate hand counter over transitions (the test oracle, not the module's aggregator)."""
    counts = {
        "comparable": 0,
        "fixes": 0,
        "new_errors": 0,
        "unchanged_right": 0,
        "unchanged_wrong": 0,
    }
    for case_id, truth in (
        (c.case_id, scenario.population.truth(c.case_id)) for c in scenario.population.cases
    ):
        before = scenario.preds[comparator][case_id]
        after = scenario.preds["candidate"][case_id]
        if not (before and after and before["status"] == after["status"] == "automatic"):
            continue
        if after["label"] not in (A, B) or before["label"] not in (A, B):
            continue
        counts["comparable"] += 1
        key = {
            (False, True): "fixes",
            (True, False): "new_errors",
            (True, True): "unchanged_right",
            (False, False): "unchanged_wrong",
        }[(before["label"] == truth, after["label"] == truth)]
        counts[key] += 1
    return counts


def claim(scenario, *, pol=None, **over):
    pol = pol or policy()
    digests = {"incumbent": INCUMBENT, "simple_repair": SIMPLE, "real_only": REAL_ONLY}
    values = {
        "tenant": "generated-tenant-1",
        "experiment_id": "generated-experiment-1",
        "producer_id": WORKER,
        "result_revision": sha("result-r1"),
        "candidate_kind": "open_weight_fit",
        "candidate_digest": CANDIDATE,
        "comparator_digests": {c: digests[c] for c in scenario.comparators},
        "benchmark_digest": BENCHMARK,
        "membership_digest": scenario.population.membership_digest,
        "protected_rows": len(scenario.population.cases),
        "operating_point_digest": pol.operating_point_digest,
        "policy_revision": pol.policy_revision,
        "policy_digest": pol.digest(),
        "claimed_paired": {
            c: recompute_pairs(scenario, c)
            for c in scenario.comparators
            if c in (*pol.required_comparators, *pol.optional_comparators)
        },
        "job_manifest_digest": MANIFEST["manifest_sha256"],
    }
    values.update(over)
    return iv.ClaimedResult(**values)


def evaluator(tmp_path, **kw):
    return iv.IndependentEvaluator(
        evaluator_id=EVALUATOR,
        custody_dir=tmp_path / "evaluator-custody",
        worker_data_dirs=[tmp_path / "worker-data"],
        **kw,
    )


def run(
    tmp_path,
    scenario,
    *,
    pol=None,
    replays=None,
    manifest=MANIFEST,
    receipt=None,
    ev=None,
    **claim_over,
):
    pol = pol or policy()
    return (ev or evaluator(tmp_path)).evaluate(
        claim=claim(scenario, pol=pol, **claim_over),
        population=scenario.population,
        audit_receipt=receipt or receipt_for(scenario.population),
        policy=pol,
        replays=replays or scenario.replays(),
        fit_manifest=manifest,
    )


CLEAN_WIN = [("WR", 120), ("RR", 80)]
B4 = [("WR", 19), ("WW", 5), ("RW", 1), ("RR", 135), ("DD", 40)]


def held(verdict, outcome, reason=None):
    doc = verdict.document
    assert doc["outcome"] == outcome
    if reason is not None:
        assert any(reason in r for r in doc["reasons"]), doc["reasons"]
    assert doc["effects"] == {
        "improvement_alert_eligible": False,
        "release_request_eligible": False,
        "handoff_request_eligible": False,
    }
    assert doc["verdict"] == "not_reproduced"
    return doc


# ----------------------------------------------------------------- C: claim-verification chain


def test_c1_clean_win_verifies_all_three_statements_and_is_alert_eligible(tmp_path):
    s = Scenario(CLEAN_WIN)
    v = run(tmp_path, s)
    doc = v.document
    assert doc["outcome"] == "verified_gain" and doc["verdict"] == "reproduced"
    st = doc["statements"]
    assert st["job_produced_candidate"]["status"] == "self_consistent_manifest"
    assert st["job_produced_candidate"]["manifest_states_training_steps"] == 64
    assert st["job_produced_candidate"]["manifest_states_changed_trainable_parameters"] == 192
    assert st["candidate_served_evaluation"]["status"] == "readback_consistent"
    assert st["candidate_served_evaluation"]["drift_checks"] == {
        "bundle_manifest": "checked",
        "reload": "checked",
        "tokenizer_sha256": "checked",
    }
    assert st["paired_gain_cleared_gates"]["status"] == "verified_gain"
    pair = doc["comparisons"]["incumbent"]
    assert pair["paired"] == {
        "comparable": 200,
        "fixes": 120,
        "new_errors": 0,
        "unchanged_right": 80,
        "unchanged_wrong": 0,
        "excluded": {},
    }
    assert pair["improvement"]["percentage_points"] == "60.0000"
    assert pair["uncertainty"]["families"] == 200
    assert float(pair["uncertainty"]["lower"]) > 0.02
    assert doc["effects"]["improvement_alert_eligible"] is True
    assert doc["effects"]["release_request_eligible"] is False


@pytest.mark.parametrize(
    "manifest,reason",
    [
        (fit_manifest(steps=0), "no_training_steps_or_changed_parameters"),
        (fit_manifest(changed=[0, 0]), "no_training_steps_or_changed_parameters"),
        ({**MANIFEST, "training_rows": 999}, "fit_manifest_not_self_consistent"),
        (None, "job_binding_missing"),
        ({"schema": "x"}, "fit_manifest_malformed"),
    ],
)
def test_c2_job_statement_refuses_unproven_fit(tmp_path, manifest, reason):
    s = Scenario(CLEAN_WIN)
    replays = s.replays()
    doc = held(run(tmp_path, s, manifest=manifest, replays=replays), "integrity_failure", reason)
    assert doc["statements"]["job_produced_candidate"]["status"] == "failed"
    assert replays["candidate"].calls == []  # no inference on an unverified artifact
    assert doc["comparisons"] == {}


def test_c3_job_not_on_pinned_base_or_not_the_designated_job(tmp_path):
    s = Scenario(CLEAN_WIN)
    other = fit_manifest(base_sha256=sha("other-base"))
    held(
        run(
            tmp_path / "a",
            s,
            manifest=other,
            candidate_digest=other["artifact_sha256"],
            replays=s.replays(candidate=Replay(other["artifact_sha256"], s.preds["candidate"])),
        ),
        "integrity_failure",
        "fit_not_on_pinned_base",
    )
    held(
        run(tmp_path / "b", s, job_manifest_digest=sha("another-job")),
        "integrity_failure",
        "designated_job_mismatch",
    )
    retrained = fit_manifest(adapter_sha256=sha("other-adapter"))
    held(
        run(
            tmp_path / "c", s, manifest=retrained, job_manifest_digest=retrained["manifest_sha256"]
        ),
        "integrity_failure",
        "job_artifact_is_not_the_candidate",
    )


def test_c4_swapped_artifact_with_predictions_held_constant_is_integrity_failure(tmp_path):
    s = Scenario(CLEAN_WIN)
    swapped = Replay(sha("swapped-artifact"), s.preds["candidate"])
    doc = held(
        run(tmp_path, s, replays=s.replays(candidate=swapped)),
        "integrity_failure",
        "candidate_served_other_artifact",
    )
    assert swapped.calls == []
    assert doc["statements"]["candidate_served_evaluation"]["status"] == "failed"
    assert doc["effects"]["handoff_request_eligible"] is False


def test_c5_mutable_alias_drifted_config_and_swapped_comparator_hold(tmp_path):
    s = Scenario(CLEAN_WIN)
    alias = Replay(CANDIDATE, s.preds["candidate"], served={"artifact_sha256": "latest", **PINS})
    held(
        run(tmp_path / "a", s, replays=s.replays(candidate=alias)),
        "integrity_failure",
        "served_identity_mutable_alias",
    )
    drift = Replay(
        CANDIDATE,
        s.preds["candidate"],
        served={"artifact_sha256": CANDIDATE, **PINS, "tokenizer_sha256": sha("other-tokenizer")},
    )
    held(
        run(tmp_path / "b", s, replays=s.replays(candidate=drift)),
        "integrity_failure",
        "served_configuration_drift",
    )
    comp = Replay(sha("other-incumbent"), s.preds["incumbent"])
    held(
        run(tmp_path / "c", s, replays=s.replays(incumbent=comp)),
        "integrity_failure",
        "incumbent_served_other_artifact",
    )


def test_c6_population_operating_point_and_audit_bindings(tmp_path):
    s = Scenario(CLEAN_WIN)
    held(
        run(tmp_path / "a", s, protected_rows=199),
        "integrity_failure",
        "population_or_denominator_mismatch",
    )
    held(
        run(tmp_path / "b", s, operating_point_digest=sha("other-point")),
        "integrity_failure",
        "operating_point_or_policy_mismatch",
    )
    bad = receipt_for(s.population, verdict="invalid")
    held(run(tmp_path / "c", s, receipt=bad), "integrity_failure", "benchmark_audit_invalid")
    forged = {**receipt_for(s.population), "verdict": "evaluation_allowed", "reasons": ["x"]}
    held(
        run(tmp_path / "d", s, receipt=forged),
        "integrity_failure",
        "benchmark_audit_receipt_unverified",
    )


# ----------------------------------------------------------------- S: independent scoring


def test_s1_scorer_canary_and_permutation_exact_counts():
    passed, observed = iv.qualify_scorer(iv.DecisionScorer())
    assert passed
    assert observed["canary_matches"] == 4
    assert observed["canary_permuted_matches"] == 2
    assert observed["b4"]["comparator_wrong"] == 24
    assert observed["b4"]["candidate_wrong"] == 6


class AlwaysRight(iv.DecisionScorer):
    def score_case(self, prediction, truth, label_set):
        return "correct_automatic" if prediction else "missing_output"


class CountsDeferralsAsCorrect(iv.DecisionScorer):
    def score_case(self, prediction, truth, label_set):
        out = super().score_case(prediction, truth, label_set)
        return "correct_automatic" if out == "deferred" else out


@pytest.mark.parametrize("wrong", [AlwaysRight(), CountsDeferralsAsCorrect()])
def test_s2_deliberately_wrong_scorer_fails_the_fixture_oracle(tmp_path, wrong):
    passed, _ = iv.qualify_scorer(wrong)
    assert not passed
    s = Scenario(CLEAN_WIN)
    doc = held(
        run(tmp_path, s, ev=evaluator(tmp_path, scorer=wrong)),
        "integrity_failure",
        "scorer_failed_fixture_oracle",
    )
    assert doc["scorer"]["qualification"] == "failed"


def test_s3_b4_paired_arithmetic_exact_and_inconclusive_under_registered_uncertainty(tmp_path):
    s = Scenario(B4, comparators=("real_only",))
    pol = policy(required_comparators=("real_only",))
    v = run(tmp_path, s, pol=pol)
    pair = v.document["comparisons"]["real_only"]
    assert pair["paired"] == {
        "comparable": 160,
        "fixes": 19,
        "new_errors": 1,
        "unchanged_right": 135,
        "unchanged_wrong": 5,
        "excluded": {"deferred_both": 40},
    }
    assert pair["wrong_on_comparable"] == {"candidate": [6, 160], "comparator": [24, 160]}
    assert pair["improvement"]["counts"] == [18, 160]
    assert pair["improvement"]["percentage_points"] == "11.2500"
    assert pair["candidate_arm"]["coverage"] == [160, 200]  # 80% automation coverage
    assert pair["candidate_arm"]["deferred"] == 40
    # 160 singleton families: Hoeffding radius sqrt(2 ln 40 / 160) = 0.214735 spans 2pp.
    assert pair["uncertainty"]["lower"] == "-0.102235"
    assert pair["uncertainty"]["upper"] == "0.327235"
    held(v, "inconclusive", "interval_spans_threshold")


def test_s4_headline_totals_from_the_claim_are_never_the_expected_value(tmp_path):
    s = Scenario(CLEAN_WIN)
    inflated = {"incumbent": {**recompute_pairs(s, "incumbent"), "fixes": 150}}
    doc = held(
        run(tmp_path, s, claimed_paired=inflated),
        "integrity_failure",
        "claimed_counts_not_reproduced_incumbent",
    )
    assert doc["comparisons"]["incumbent"]["paired"]["fixes"] == 120  # recomputed, visible


def test_s5_balanced_label_permutation_scores_exactly_half(tmp_path):
    s = Scenario([("RR", 8)])
    permuted = {
        cid: (B if s.population.truth(cid) == A else A) if i % 2 else s.population.truth(cid)
        for i, cid in enumerate(sorted(s.preds["candidate"]))
    }
    pop = iv.SealedPopulation(
        s.population.cases, permuted, label_set=(A, B), benchmark_digest=BENCHMARK
    )
    s.population = pop
    v = run(tmp_path, s)
    arm = v.document["comparisons"]["incumbent"]["candidate_arm"]
    assert (arm["automatic"], arm["wrong_automatic"]) == (8, 4)


def test_s6_answers_never_enter_inference_and_verdict_holds_no_case_content(tmp_path):
    s = Scenario(CLEAN_WIN)
    replays = s.replays()
    v = run(tmp_path, s, replays=replays)
    payload = json.dumps([r.calls for r in replays.values()])
    assert A not in payload and B not in payload
    assert replays["candidate"].expected == [CANDIDATE]
    doc = json.dumps(v.document)
    assert A not in doc and B not in doc and "case-0001" not in doc and "generated-input" not in doc


# ----------------------------------------------------------------- X: named adverse controls


def test_x1_copied_prediction_and_unchanged_incumbent_are_zero_improvement(tmp_path):
    s = Scenario(CLEAN_WIN)
    copy = Replay(CANDIDATE, s.preds["incumbent"])
    doc = held(
        run(tmp_path / "a", s, replays=s.replays(candidate=copy), claimed_paired={}),
        "no_material_gain",
        "predictions_identical_to_comparator",
    )
    assert doc["comparisons"]["incumbent"]["improvement"]["counts"][0] == 0
    same = iv.ClaimedResult(
        **{
            **claim(s).__dict__,
            "comparator_digests": {"incumbent": CANDIDATE},
            "claimed_paired": {},
        }
    )
    v = evaluator(tmp_path / "b").evaluate(
        claim=same,
        population=s.population,
        audit_receipt=receipt_for(s.population),
        policy=policy(),
        replays=s.replays(incumbent=Replay(CANDIDATE, s.preds["candidate"])),
        fit_manifest=MANIFEST,
    )
    held(v, "no_material_gain", "candidate_is_comparator")


def test_x2_stale_benchmark_and_superseded_audit_hold_as_stale(tmp_path):
    s = Scenario(CLEAN_WIN)
    replays = s.replays()
    held(
        run(tmp_path / "a", s, benchmark_digest=sha("benchmark-v0"), replays=replays),
        "stale",
        "benchmark_digest_mismatch",
    )
    assert replays["candidate"].calls == []
    held(
        run(tmp_path / "b", s, receipt=receipt_for(s.population, superseded_by=sha("r2"))),
        "stale",
        "benchmark_audit_stale_or_superseded",
    )
    other_key = receipt_for(s.population)
    other_key["bindings"]["current"]["answer_key"] = sha("other-key")
    other_key["bindings"]["frozen"]["answer_key"] = sha("other-key")
    body = {k: v for k, v in other_key.items() if k not in ("receipt_digest", "receipt_id")}
    other_key["receipt_digest"] = digest(body)
    other_key["receipt_id"] = "audit-" + other_key["receipt_digest"][:16]
    held(run(tmp_path / "c", s, receipt=other_key), "stale", "answer_key_not_the_audited_key")


def test_x3_missing_rows_show_exclusions_and_hold(tmp_path):
    s = Scenario([("WR", 110), ("WM", 10), ("RR", 80)])
    doc = held(run(tmp_path, s), "inconclusive", "missing_or_malformed_predictions")
    pair = doc["comparisons"]["incumbent"]
    assert pair["paired"]["excluded"] == {"missing_candidate": 10}
    assert pair["candidate_arm"]["missing"] == 10
    malformed = Scenario([("WR", 110), ("WX", 10), ("RR", 80)])
    doc = held(run(tmp_path / "b", malformed), "inconclusive")
    assert doc["comparisons"]["incumbent"]["paired"]["excluded"] == {"malformed_candidate": 10}


def test_x4_coverage_reduction_is_an_incompatible_comparison(tmp_path):
    s = Scenario([("WR", 100), ("RD", 60), ("RR", 40)])
    doc = held(run(tmp_path, s), "inconclusive", "coverage_incompatible")
    pair = doc["comparisons"]["incumbent"]
    assert pair["candidate_arm"]["coverage"] == [140, 200]
    assert pair["comparator_arm"]["coverage"] == [200, 200]
    assert pair["paired"]["excluded"] == {"deferred_by_candidate": 60}


def test_x5_critical_slice_regression_despite_aggregate_win(tmp_path):
    def critical(index, codes):
        return (CRITICAL,) if index >= 190 else ()

    s = Scenario([("WR", 120), ("RR", 70), ("RW", 10)], slice_of=critical)
    pol = policy(critical_slices=(CRITICAL,))
    doc = held(run(tmp_path, s, pol=pol), "regression", "critical_slice_veto")
    pair = doc["comparisons"]["incumbent"]
    assert pair["improvement"]["counts"] == [110, 200]  # aggregate still a win
    assert pair["critical_slices"][CRITICAL] == {
        "comparable": 10,
        "fixes": 0,
        "new_errors": 10,
        "status": "veto",
    }
    unsupported = policy(critical_slices=(ABSENT,))
    held(
        run(tmp_path / "b", Scenario(CLEAN_WIN), pol=unsupported),
        "inconclusive",
        "critical_slice_unsupported",
    )


def test_x6_real_regression_is_visible(tmp_path):
    s = Scenario([("RW", 120), ("RR", 80)])
    doc = held(run(tmp_path, s), "regression", "shared_case_errors_increased")
    assert doc["comparisons"]["incumbent"]["improvement"]["counts"] == [-120, 200]
    assert doc["comparisons"]["incumbent"]["uncertainty"]["upper"].startswith("-")


def test_x7_tuned_candidate_must_win_against_both_incumbent_and_simple_repair(tmp_path):
    plan = [
        ({"incumbent": "W", "simple_repair": "R", "candidate": "R"}, 120),
        ({"incumbent": "R", "simple_repair": "R", "candidate": "R"}, 80),
    ]
    s = Scenario(plan, comparators=("incumbent", "simple_repair"))
    pol = policy(required_comparators=("incumbent", "simple_repair"))
    doc = held(
        run(tmp_path, s, pol=pol),
        "no_material_gain",
        "simple_repair:predictions_identical_to_comparator",
    )
    assert doc["comparisons"]["incumbent"]["outcome"] == "verified_gain"
    assert doc["comparisons"]["simple_repair"]["outcome"] == "no_material_gain"
    win = [
        ({"incumbent": "W", "simple_repair": "W", "candidate": "R"}, 120),
        ({"incumbent": "R", "simple_repair": "R", "candidate": "R"}, 80),
    ]
    s2 = Scenario(win, comparators=("incumbent", "simple_repair"))
    v = run(tmp_path / "b", s2, pol=pol)
    assert v.outcome == "verified_gain"
    missing = s2.replays()
    del missing["simple_repair"]
    held(
        run(
            tmp_path / "c",
            s2,
            pol=pol,
            replays=missing,
            claimed_paired={"incumbent": recompute_pairs(s2, "incumbent")},
        ),
        "inconclusive",
        "required_comparison_missing_simple_repair",
    )


def test_x8_unrun_augmentation_and_simple_repair_without_fit(tmp_path):
    s = Scenario(CLEAN_WIN)
    pol = policy(optional_comparators=("real_only",))
    v = run(tmp_path, s, pol=pol)
    assert v.outcome == "verified_gain" and v.document["verdict"] == "reproduced"
    unstated = run(tmp_path / "u", s, claimed_paired={})
    assert unstated.outcome == "verified_gain" and unstated.document["verdict"] == "not_reproduced"
    assert unstated.document["claim_counts_stated"] is False
    assert unstated.document["effects"]["improvement_alert_eligible"] is False
    assert v.document["comparisons"]["real_only"] == {
        "outcome": "unrun",
        "required": False,
        "reason": "arm_not_run",
    }
    repair = run(
        tmp_path / "b", s, manifest=None, candidate_kind="simple_repair", job_manifest_digest=None
    )
    assert repair.outcome == "verified_gain"
    assert repair.document["statements"]["job_produced_candidate"] == {
        "status": "not_applicable",
        "reason": "no_fit_claimed",
    }
    held(
        run(tmp_path / "c", s, candidate_kind="simple_repair"),
        "integrity_failure",
        "fit_evidence_on_a_no_fit_repair",
    )


def test_x9_replay_drift_and_selection_after_readout(tmp_path):
    s = Scenario(CLEAN_WIN)
    drifting = Replay(CANDIDATE, s.preds["candidate"], alternate=s.preds["incumbent"])
    held(
        run(
            tmp_path,
            s,
            pol=policy(repeat_runs=2),
            replays=s.replays(candidate=drifting),
            claimed_paired={},
        ),
        "inconclusive",
        "candidate_replay_drift_beyond_tolerance",
    )
    ev = evaluator(tmp_path / "b")
    assert run(tmp_path, s, ev=ev).outcome == "verified_gain"
    held(
        run(
            tmp_path,
            s,
            ev=ev,
            result_revision=sha("reselected"),
            selection_basis="after_protected_readout",
        ),
        "inconclusive",
        "fresh_confirmation_cohort_required",
    )
    fresh = Scenario([("WR", 121), ("RR", 80)], prefix="fresh-")
    assert run(tmp_path, fresh, ev=ev, selection_basis="after_protected_readout").outcome == (
        "verified_gain"
    )


def test_x10_zero_alerts_and_release_requests_from_every_false_verdict(tmp_path):
    s = Scenario(CLEAN_WIN)
    adverse = [
        run(tmp_path / "1", s, replays=s.replays(candidate=Replay(sha("x"), s.preds["candidate"]))),
        run(tmp_path / "2", s, benchmark_digest=sha("benchmark-v0")),
        run(tmp_path / "3", s, ev=evaluator(tmp_path / "3", scorer=AlwaysRight())),
        run(tmp_path / "4", Scenario([("RW", 120), ("RR", 80)])),
        run(tmp_path / "5", Scenario(B4)),
        run(
            tmp_path / "6",
            s,
            replays=s.replays(candidate=Replay(CANDIDATE, s.preds["incumbent"])),
            claimed_paired={},
        ),
    ]
    assert [v.outcome for v in adverse] == [
        "integrity_failure",
        "stale",
        "integrity_failure",
        "regression",
        "inconclusive",
        "no_material_gain",
    ]
    effects = [v.document["effects"] for v in adverse]
    assert sum(e["improvement_alert_eligible"] for e in effects) == 0
    assert sum(e["release_request_eligible"] for e in effects) == 0
    assert sum(e["handoff_request_eligible"] for e in effects) == 0


# ----------------------------------------------------------------- K: idempotency and custody


def test_k1_one_idempotent_verdict_and_replay_returns_the_original(tmp_path):
    s = Scenario(CLEAN_WIN)
    ev = evaluator(tmp_path)
    replays = s.replays()
    first = run(tmp_path, s, ev=ev, replays=replays)
    second = run(tmp_path, s, ev=ev, replays=replays)
    assert not first.replayed and second.replayed
    assert second.document == first.document
    assert len(replays["candidate"].calls) == 1  # the replay did not re-run inference
    changed = run(tmp_path, s, ev=ev, result_revision=sha("result-r2"))
    assert changed.verdict_key != first.verdict_key and not changed.replayed
    # The key binds the whole claim (including its claimed counts), not replay output: the
    # same claim on the same frozen artifact and sealed inputs returns the first verdict even
    # if a replay would now differ.
    drifted = s.replays(candidate=Replay(CANDIDATE, s.preds["incumbent"]))
    assert run(tmp_path, s, ev=ev, replays=drifted).document == first.document
    assert drifted["candidate"].calls == []
    regressed = Scenario([("RW", 120), ("RR", 80)])
    non_win = run(tmp_path, regressed, ev=ev, result_revision=sha("result-r3"))
    again = run(tmp_path, regressed, ev=ev, result_revision=sha("result-r3"))
    assert (
        non_win.outcome == "regression" and again.replayed and again.document == (non_win.document)
    )


def test_k2_tampered_stored_verdict_is_refused(tmp_path):
    s = Scenario(CLEAN_WIN)
    ev = evaluator(tmp_path)
    v = run(tmp_path, s, ev=ev)
    path = tmp_path / "evaluator-custody" / "verdicts" / (v.verdict_key + ".json")
    stored = json.loads(path.read_bytes())
    stored["document"]["outcome"] = "verified_gain"
    stored["document"]["effects"]["release_request_eligible"] = True
    body = {k: x for k, x in stored["document"].items() if k != "verdict_digest"}
    stored["document"]["verdict_digest"] = digest(body)  # re-sealed without the evaluator key
    os.chmod(path, 0o600)
    path.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_verdict_tampered"):
        run(tmp_path, s, ev=ev)


def test_k3_private_predictions_are_attributed_committed_access_controlled_and_retained(
    tmp_path, monkeypatch
):
    s = Scenario(CLEAN_WIN)
    ev = evaluator(tmp_path)
    v = run(tmp_path, s, ev=ev)
    private = ev.private_predictions(v.verdict_key, accessor_id=EVALUATOR)
    rows = private["record"]["rows"]
    assert len(rows) == 400 and all(
        r["scorer_id"] == iv.SCORER_ID and r["rubric_revision"] == iv.RUBRIC_REVISION for r in rows
    )
    assert private["record"]["predictions"]["candidate"]["case-0000"]["status"] == "automatic"
    with pytest.raises(iv.CustodyError, match="access_denied"):
        ev.private_predictions(v.verdict_key, accessor_id=WORKER)
    mode = (tmp_path / "evaluator-custody" / "predictions").stat().st_mode
    assert mode & 0o077 == 0
    now = int(iv.time.time())
    monkeypatch.setattr(iv.time, "time", lambda: now + 31 * 86_400)
    assert ev.purge_expired() == 1
    with pytest.raises(iv.CustodyError, match="absent_or_purged"):
        ev.private_predictions(v.verdict_key, accessor_id=EVALUATOR)
    assert (
        run(tmp_path, s, ev=ev).document["predictions_commitment"]
        == (v.document["predictions_commitment"])
    )


def test_k4_custody_must_be_private_and_separate_from_worker_data(tmp_path):
    with pytest.raises(iv.CustodyError, match="overlaps_worker_data"):
        iv.IndependentEvaluator(
            evaluator_id=EVALUATOR,
            custody_dir=tmp_path / "w" / "eval",
            worker_data_dirs=[tmp_path / "w"],
        )
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    os.chmod(loose, 0o755)
    with pytest.raises(iv.CustodyError, match="not_private"):
        iv.IndependentEvaluator(
            evaluator_id=EVALUATOR, custody_dir=loose, worker_data_dirs=[tmp_path / "w"]
        )


# ----------------------------------------------------------------- I: identity, scope, signing


def test_i1_evaluator_must_not_be_the_producer_and_out_of_scope_routes_refuse(tmp_path):
    s = Scenario(CLEAN_WIN)
    with pytest.raises(iv.IndependenceError):
        run(tmp_path, s, producer_id=EVALUATOR)
    with pytest.raises(iv.VerdictInputError, match="unsupported_candidate_route"):
        run(tmp_path, s, candidate_kind="managed_provider")
    with pytest.raises(iv.VerdictInputError, match="stochastic_decoding_not_supported"):
        run(tmp_path, s, pol=policy(decoding="sampled"))
    assert not (tmp_path / "evaluator-custody" / "verdicts").exists() or not list(
        (tmp_path / "evaluator-custody" / "verdicts").iterdir()
    )


def test_i2_unsigned_record_is_locally_keyed_and_consumable_by_the_packet_boundary(tmp_path):
    v = run(tmp_path, Scenario(CLEAN_WIN))
    doc = v.document
    assert v.signature is None
    assert doc["evidence"] == {
        "evidence_tier": "locally_keyed",
        "evidence_origin": "generated",
        "producer_distinct_from_evaluator": "caller_asserted_identity_labels",
        "custody_separated": False,
        "issuer_authenticated": False,
        "claim_scope": "implementation_only",
        "accuracy_claim_eligible": False,
    }
    assert cp.VERDICT == iv.VERDICT and cp._verdict_ok(doc)
    assert doc["verifier_id"] == EVALUATOR and doc["producer_id"] == WORKER
    assert doc["denominator"]["protected_rows"] == 200


def test_i3_signing_seam_hands_the_journal_transcript_to_the_evaluator_key(tmp_path):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from opentour_journal.crypto import der_to_raw, raw_to_der, transcript

    test_key = ec.generate_private_key(ec.SECP256R1())  # ephemeral, test only
    seen: list[bytes] = []

    def sign(data: bytes) -> str:
        seen.append(data)
        return der_to_raw(test_key.sign(data, ec.ECDSA(hashes.SHA256())))

    signer = iv.EvaluatorSigner("generated-test-evaluator-key", "generated-registry-r1", sign)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=evaluator(tmp_path, signer=signer))
    assert seen == [transcript(iv.PURPOSE, v.document)]
    assert json.loads(seen[0]) == {"purpose": iv.PURPOSE, "payload": v.document}
    env = v.signature
    assert env is not None and {k: env[k] for k in env if k != "signature"} == {
        "profile_version": "1.0",
        "algorithm": "ES256",
        "encoding": "base64url_unpadded_r_s_64",
        "purpose": iv.PURPOSE,
        "key_id": "generated-test-evaluator-key",
        "key_registry_revision": "generated-registry-r1",
    }
    test_key.public_key().verify(raw_to_der(env["signature"]), seen[0], ec.ECDSA(hashes.SHA256()))
    assert v.document["evidence"]["evidence_tier"] == "locally_keyed"  # never self-upgraded


def test_i4_a_malformed_signature_is_refused_and_nothing_is_stored(tmp_path):
    signer = iv.EvaluatorSigner("generated-test-evaluator-key", "r1", lambda _: "not-a-sig")
    with pytest.raises(iv.SigningRefused):
        run(tmp_path, Scenario(CLEAN_WIN), ev=evaluator(tmp_path, signer=signer))
    assert list((tmp_path / "evaluator-custody" / "verdicts").iterdir()) == []


def test_i5_replay_worker_never_imports_the_independent_scorer():
    root = Path(iv.__file__).resolve().parent
    tree = ast.parse((root / "eve_replay.py").read_text())
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
        a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
    }
    assert not any(n and "independent_verdict" in n for n in names)
    probe = (
        "import sys\n"
        "import opentour_cell.pilot.eve_replay\n"
        "assert 'opentour_cell.pilot.independent_verdict' not in sys.modules\n"
        "import opentour_cell.pilot.independent_verdict\n"
        "assert 'opentour_cell.pilot.eve_replay' in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", probe], check=True)
    probe_reverse = (
        "import sys\n"
        "import opentour_cell.pilot.independent_verdict\n"
        "assert 'opentour_cell.pilot.eve_replay' not in sys.modules\n"
    )
    subprocess.run([sys.executable, "-c", probe_reverse], check=True)


# ----------------------------------------------------------------- open-weight adapter


def test_o1_open_weight_adapter_binds_lineage_and_batches_within_profile(tmp_path):
    s = Scenario([("WR", 80), ("RR", 50)])
    calls: list[dict] = []

    def inspect(bundle, *, grant):
        return MANIFEST

    def predict(bundle, features, *, model_dir, grant, python, expected_artifact_sha256):
        calls.append({"n": len(features), "expected": expected_artifact_sha256})
        return [s.preds["candidate"][f["case_ref"]] for f in features]

    replay = iv.OpenWeightReplay(
        tmp_path / "bundle",
        model_dir=tmp_path / "model",
        grant={"generated": True},
        inspect_fn=inspect,
        predict_fn=predict,
    )
    # Only what the bundle states: no hardcoded tokenizer digest, no assumed reload.
    assert replay.served_identity() == {
        "artifact_sha256": CANDIDATE,
        "manifest_sha256": MANIFEST["manifest_sha256"],
        "base_sha256": ow.BASE_SHA256,
        "inference_config_sha256": ow.PROFILE_SHA256,
    }
    v = run(tmp_path, s, replays=s.replays(candidate=replay))
    assert v.outcome == "verified_gain"
    assert v.document["statements"]["candidate_served_evaluation"]["drift_checks"] == {
        "bundle_manifest": "checked",
        "reload": "unavailable",
        "tokenizer_sha256": "unavailable",
    }
    assert [c["n"] for c in calls] == [64, 64, 2]
    assert {c["expected"] for c in calls} == {CANDIDATE}


def test_x11_non_json_or_non_mapping_outputs_are_recorded_as_malformed(tmp_path):
    s = Scenario(CLEAN_WIN)
    junk = dict(s.preds["candidate"])
    junk["case-0000"] = "automatic"
    junk["case-0001"] = {"status": "automatic", "label": {A}}
    doc = held(
        run(tmp_path, s, replays=s.replays(candidate=Replay(CANDIDATE, junk)), claimed_paired={}),
        "inconclusive",
        "missing_or_malformed_predictions",
    )
    assert doc["comparisons"]["incumbent"]["candidate_arm"]["malformed"] == 2


def test_c7_operating_point_is_bound_through_substituted_terms_without_the_slice_key(tmp_path):
    s = Scenario(CLEAN_WIN)
    pol = policy(critical_slices=(CRITICAL,))
    assert iv.operating_point_binding(pol.operating_point_terms, pol.operating_point_digest) is None
    edited = {**pol.operating_point_terms, "error_ceiling_bps": 9000}
    held(
        run(tmp_path / "a", s, pol=policy(operating_point_terms=edited)),
        "integrity_failure",
        "operating_point_digest_mismatch",
    )
    plain = terms_for(("critical-plain-name",))
    held(
        run(
            tmp_path / "b",
            s,
            pol=policy(operating_point_terms=plain, operating_point_digest=digest(plain)),
        ),
        "integrity_failure",
        "operating_point_slice_names_not_substituted",
    )
    unbound = policy(critical_slices=(CRITICAL,), operating_point_terms=terms_for())
    held(
        run(
            tmp_path / "c",
            s,
            pol=iv.GatePolicy(
                **{**unbound.__dict__, "operating_point_digest": digest(terms_for())}
            ),
        ),
        "integrity_failure",
        "critical_slices_not_bound_to_operating_point",
    )
    doc = run(tmp_path / "d", s, pol=pol).document
    assert doc["operating_point_digest"] == pol.operating_point_digest
    assert doc["policy"]["operating_point_terms"]["critical_slices"] == [CRITICAL]


# ----------------------------------------------------------------- R: review probes (P1-P9)


def test_r1_p1_comparator_missing_outputs_cannot_hide_candidate_errors(tmp_path):
    """Probe P1: WRx120 + MWx130. Candidate wrong 130 where the comparator emitted nothing."""
    s = Scenario([("WR", 120), ("MW", 130)])
    loose = policy(max_coverage_loss_bps=10000, coverage_floor_bps=0)
    for path, pol in ((tmp_path / "loose", loose), (tmp_path / "default", policy())):
        doc = held(run(path, s, pol=pol), "inconclusive", "comparator_missing_or_malformed_outputs")
        pair = doc["comparisons"]["incumbent"]
        assert pair["comparator_gaps"] == 130
        assert pair["total_wrong_automatic"] == {"candidate": 130, "comparator": 120}


def test_r2_p2_newly_automated_errors_count_against_the_candidate(tmp_path):
    """Probe P2: WRx120 + DWx130. Comparator deferred 130; candidate automated them wrong."""
    s = Scenario([("WR", 120), ("DW", 130)])
    loose = policy(max_coverage_loss_bps=10000, coverage_floor_bps=0)
    doc = held(run(tmp_path, s, pol=loose), "regression", "newly_automated_errors_exceed_ceiling")
    pair = doc["comparisons"]["incumbent"]
    assert pair["newly_automated"] == {"wrong": 130, "total": 130}
    assert pair["total_wrong_automatic"] == {"candidate": 130, "comparator": 120}
    assert pair["uncertainty"]["families"] == 250  # newly automated cases are in the effects
    assert pair["uncertainty"]["estimate"].startswith("-")
    # Fewer total errors, but the newly automated ones breach the 500 bps error ceiling.
    t = Scenario([("WR", 120), ("RR", 60), ("DW", 5), ("DR", 15)])
    doc = held(run(tmp_path / "b", t, pol=loose), "regression", "newly_automated_errors_exceed")
    assert doc["comparisons"]["incumbent"]["newly_automated"] == {"wrong": 5, "total": 20}


def test_r3_p3_overlapping_cohort_is_not_fresh_and_marker_follows_issue(tmp_path):
    """Probe P3: a 201-case superset of a scored 200-case cohort is not a fresh cohort."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    held(
        run(tmp_path, s, ev=ev, benchmark_digest=sha("benchmark-v0")),
        "stale",
        "benchmark_digest_mismatch",
    )
    # The stale verdict scored nothing, so it marked nothing as viewed.
    assert list((tmp_path / "evaluator-custody" / "cohorts").iterdir()) == []
    assert run(tmp_path, s, ev=ev, selection_basis="after_protected_readout").outcome == (
        "verified_gain"
    )
    superset = Scenario([("WR", 120), ("RR", 80), ("RR", 1)])
    held(
        run(
            tmp_path,
            superset,
            ev=ev,
            result_revision=sha("reselected"),
            selection_basis="after_protected_readout",
        ),
        "inconclusive",
        "fresh_confirmation_cohort_required",
    )
    disjoint = Scenario(CLEAN_WIN, prefix="disjoint-")
    assert run(tmp_path, disjoint, ev=ev, selection_basis="after_protected_readout").outcome == (
        "verified_gain"
    )
    assert "per custody directory" in (iv.__doc__ or "")


def test_r4_p4_a_different_claim_never_replays_another_claims_verdict(tmp_path):
    """Probe P4: same population/artifact, different kind, selection basis and claimed counts."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    first = run(
        tmp_path, s, ev=ev, manifest=None, candidate_kind="simple_repair", job_manifest_digest=None
    )
    assert first.outcome == "verified_gain" and first.document["candidate_kind"] == "simple_repair"
    inflated = {"incumbent": {**recompute_pairs(s, "incumbent"), "fixes": 999}}
    second = run(
        tmp_path, s, ev=ev, selection_basis="after_protected_readout", claimed_paired=inflated
    )
    assert not second.replayed and second.verdict_key != first.verdict_key
    doc = held(second, "integrity_failure", "claimed_counts_not_reproduced_incumbent")
    assert doc["candidate_kind"] == "open_weight_fit"
    # A stored verdict that no longer matches the claim under its key is refused distinctly.
    path = tmp_path / "evaluator-custody" / "verdicts" / (first.verdict_key + ".json")
    stored = json.loads(path.read_bytes())
    stored["document"]["claim_digest"] = sha("another-claim")
    body = {k: x for k, x in stored["document"].items() if k != "verdict_digest"}
    stored["document"]["verdict_digest"] = digest(body)
    stored["local_seal"] = ev._commit({"document": stored["document"], "signature": None})
    path.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_verdict_claim_mismatch"):
        run(
            tmp_path,
            s,
            ev=ev,
            manifest=None,
            candidate_kind="simple_repair",
            job_manifest_digest=None,
        )


def test_r5_p5_full_policy_and_operating_point_terms_are_bound_and_enforced(tmp_path):
    """Probe P5: same revision label, loosened thresholds; terms minimums were ignored."""
    s = Scenario(B4, comparators=("real_only",))
    strict = policy(required_comparators=("real_only",))
    loose = policy(
        required_comparators=("real_only",),
        alpha_bps=9999,
        minimum_improvement_bps=0,
        coverage_floor_bps=0,
    )
    assert strict.policy_revision == loose.policy_revision and strict.digest() != loose.digest()
    committed = claim(s, pol=strict)
    v = evaluator(tmp_path).evaluate(
        claim=committed,
        population=s.population,
        audit_receipt=receipt_for(s.population),
        policy=loose,
        replays=s.replays(),
        fit_manifest=MANIFEST,
    )
    held(v, "integrity_failure", "policy_digest_mismatch")
    # Terms minimums are enforced even when the gate policy is laxer.
    w = Scenario(CLEAN_WIN)
    for name, value, reason in (
        ("minimum_coverage_bps", 10001, "coverage_incompatible"),
        ("minimum_verified_cases", 1000, "below_minimum_verified_cases"),
    ):
        t = {**terms_for(), name: value}
        p = policy(coverage_floor_bps=0, operating_point_terms=t, operating_point_digest=digest(t))
        held(run(tmp_path / name, w, pol=p), "inconclusive", reason)

    def critical(index, codes):
        return (CRITICAL,) if index < 10 else ()

    sliced = Scenario(CLEAN_WIN, slice_of=critical)
    t = {**terms_for((CRITICAL,)), "minimum_slice_cases": 11}
    p = policy(
        critical_slices=(CRITICAL,), operating_point_terms=t, operating_point_digest=digest(t)
    )
    doc = held(run(tmp_path / "slice", sliced, pol=p), "inconclusive", "critical_slice_unsupported")
    assert doc["comparisons"]["incumbent"]["critical_slices"][CRITICAL]["comparable"] == 10


def test_r6_p6_slice_substitution_check_is_format_only_and_says_so():
    """Probe P6: a plain SHA-256 of a name is indistinguishable from a keyed digest here."""
    import hashlib

    plain = hashlib.sha256(b"generated-plain-slice-name").hexdigest()
    t = terms_for((plain,))
    assert iv.operating_point_binding(t, digest(t)) is None
    doc = iv.operating_point_binding.__doc__ or ""
    assert "FORMAT ONLY" in doc and "comparison_packet.operating_point_digest" in doc


def test_r7_p7_signature_is_inside_the_seal_and_expired_records_are_unreadable(
    tmp_path, monkeypatch
):
    """Probe P7: a swapped signature envelope was accepted; expired reads relied on purge."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    v = run(tmp_path, s, ev=ev)
    path = tmp_path / "evaluator-custody" / "verdicts" / (v.verdict_key + ".json")
    stored = json.loads(path.read_bytes())
    stored["signature"] = {"forged": True}
    path.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_verdict_tampered"):
        run(tmp_path, s, ev=ev)
    ev2 = evaluator(tmp_path / "second")
    v2 = run(tmp_path / "second", s, ev=ev2)
    now = int(iv.time.time())
    monkeypatch.setattr(iv.time, "time", lambda: now + 31 * 86_400)
    with pytest.raises(iv.CustodyError, match="private_predictions_expired"):
        ev2.private_predictions(v2.verdict_key, accessor_id=EVALUATOR)
    assert "caller-asserted" in (iv.IndependentEvaluator.private_predictions.__doc__ or "")


def test_r8_p8_job_statement_is_self_consistency_and_bundle_readback_must_match(tmp_path):
    """Probe P8: resealed training stats were reported as a verified job."""
    forged = dict(MANIFEST)
    forged["training"] = {"steps": 1, "changed_parameters": [1]}
    forged.pop("manifest_sha256")
    forged["manifest_sha256"] = ow._digest(forged)
    s = Scenario(CLEAN_WIN)
    doc = held(
        run(tmp_path, s, manifest=forged, job_manifest_digest=forged["manifest_sha256"]),
        "integrity_failure",
        "served_bundle_manifest_mismatch",
    )
    job = doc["statements"]["job_produced_candidate"]
    assert job["status"] == "self_consistent_manifest"
    assert job["evidence"] == "unauthenticated_manifest_self_consistency"
    # Even when the bundle reads back the same resealed manifest, nothing says "verified".
    same = Replay(CANDIDATE, s.preds["candidate"], manifest=forged["manifest_sha256"])
    v = run(
        tmp_path / "b",
        s,
        manifest=forged,
        job_manifest_digest=forged["manifest_sha256"],
        replays=s.replays(candidate=same),
    )
    st = v.document["statements"]
    assert "verified" not in json.dumps(
        [st["job_produced_candidate"], st["candidate_served_evaluation"]]
    )
    missing = Replay(CANDIDATE, s.preds["candidate"], served={"artifact_sha256": CANDIDATE, **PINS})
    held(
        run(tmp_path / "c", s, replays=s.replays(candidate=missing)),
        "integrity_failure",
        "served_bundle_manifest_unavailable",
    )


def test_r9_p9_issuer_time_is_in_the_payload_and_expiry_forces_a_fresh_verdict(
    tmp_path, monkeypatch
):
    """Probe P9: no not_after in the verdict payload."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    v = run(tmp_path, s, ev=ev)
    doc = v.document
    assert doc["not_after"] == doc["issued_at"] + policy().verdict_validity_seconds
    assert json.loads(iv.signing_transcript(doc))["payload"]["not_after"] == doc["not_after"]
    now = int(iv.time.time())
    monkeypatch.setattr(iv.time, "time", lambda: now + 8 * 86_400)
    fresh = run(tmp_path, s, ev=ev)
    assert not fresh.replayed and fresh.document["issued_at"] > doc["issued_at"]
    archived = sorted(p.name for p in (tmp_path / "evaluator-custody" / "expired").iterdir())
    assert archived == [
        f"predictions-{v.verdict_key}-{doc['issued_at']}.json",
        f"verdicts-{v.verdict_key}-{doc['issued_at']}.json",
    ]


def test_r10_worker_separation_is_mandatory_and_fit_digest_is_pinned(tmp_path):
    for dirs in ((), [], None):
        with pytest.raises((iv.CustodyError, TypeError)):
            iv.IndependentEvaluator(
                evaluator_id=EVALUATOR, custody_dir=tmp_path / "c", worker_data_dirs=dirs
            )
    with pytest.raises(TypeError):
        iv.IndependentEvaluator(evaluator_id=EVALUATOR, custody_dir=tmp_path / "c")
    samples = [MANIFEST, MANIFEST["identity"], {"non_ascii": "café", "n": [1, 2.5]}]
    assert [iv.fit_manifest_digest(x) for x in samples] == [ow._digest(x) for x in samples]


def test_r11_attributed_rows_carry_scorer_version(tmp_path):
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    rows = ev.private_predictions(v.verdict_key, accessor_id=EVALUATOR)["record"]["rows"]
    assert set(rows[0]) == {
        "ref",
        "arm",
        "verdict",
        "scorer_id",
        "scorer_version",
        "rubric_revision",
    }
    assert {r["scorer_version"] for r in rows} == {iv.SCORER_VERSION}
    assert {r["verdict"] for r in rows} <= set(iv.CASE_VERDICTS)
    assert v.document["scorer"]["scorer_version"] == iv.SCORER_VERSION


# ----------------------------------------------------------------- Q: re-review findings


def test_q1_expired_verdict_reevaluates_past_its_own_cohort_marker_only(tmp_path, monkeypatch):
    """Re-review 1: an expired verdict must not be held by the marker it recorded itself."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN, prefix="q1-")
    first = run(tmp_path, s, ev=ev, selection_basis="after_protected_readout")
    assert first.outcome == "verified_gain"
    now = int(iv.time.time())
    monkeypatch.setattr(iv.time, "time", lambda: now + 8 * 86_400)
    again = run(tmp_path, s, ev=ev, selection_basis="after_protected_readout")
    assert not again.replayed and again.verdict_key == first.verdict_key
    assert again.outcome == "verified_gain"
    # Any other claim over the same cohort is still refused.
    held(
        run(
            tmp_path,
            s,
            ev=ev,
            result_revision=sha("q1-other"),
            selection_basis="after_protected_readout",
        ),
        "inconclusive",
        "fresh_confirmation_cohort_required",
    )


def test_q2_newly_automated_without_an_error_ceiling_is_inconclusive_not_an_error(tmp_path):
    """Mutant: dropping the no-ceiling guard would multiply None and raise TypeError."""
    t = {k: v for k, v in terms_for().items() if k != "error_ceiling_bps"}
    p = policy(operating_point_terms=t, operating_point_digest=digest(t))
    s = Scenario([("WR", 120), ("RR", 60), ("DR", 20)])
    held(run(tmp_path, s, pol=p), "inconclusive", "newly_automated_without_error_ceiling")


def test_q3_family_only_overlap_refuses_a_post_readout_cohort(tmp_path):
    """Mutant: checking case ids only would accept disjoint cases from the same families."""
    ev = evaluator(tmp_path)
    assert run(tmp_path, Scenario(CLEAN_WIN), ev=ev).outcome == "verified_gain"
    siblings = Scenario(CLEAN_WIN, prefix="sibling-", family_prefix="")
    assert not {c.case_id for c in siblings.population.cases} & {
        c.case_id for c in Scenario(CLEAN_WIN).population.cases
    }
    held(
        run(
            tmp_path,
            siblings,
            ev=ev,
            result_revision=sha("q3-sibling"),
            selection_basis="after_protected_readout",
        ),
        "inconclusive",
        "fresh_confirmation_cohort_required",
    )


def test_q4_a_signed_verdict_reloads_intact_and_a_dropped_signature_is_detected(tmp_path):
    """Mutant: sealing with ``signature: None`` would let a signature be dropped or swapped."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from opentour_journal.crypto import der_to_raw

    test_key = ec.generate_private_key(ec.SECP256R1())  # ephemeral, test only

    def sign(data: bytes) -> str:
        return der_to_raw(test_key.sign(data, ec.ECDSA(hashes.SHA256())))

    signer = iv.EvaluatorSigner("generated-test-evaluator-key", "generated-registry-r1", sign)
    ev = evaluator(tmp_path, signer=signer)
    s = Scenario(CLEAN_WIN)
    v = run(tmp_path, s, ev=ev)
    assert v.signature is not None
    reloaded = run(tmp_path, s, ev=ev)
    assert reloaded.replayed and reloaded.signature == v.signature
    path = tmp_path / "evaluator-custody" / "verdicts" / (v.verdict_key + ".json")
    stored = json.loads(path.read_bytes())
    stored["signature"] = None
    path.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_verdict_tampered"):
        run(tmp_path, s, ev=ev)


def test_q5_coverage_expansion_within_the_ceiling_is_not_called_a_regression(tmp_path):
    """Re-review 3: 1 newly automated error in 30 (333 bps <= 500) is expansion, not regression."""
    s = Scenario([("RR", 170), ("DR", 29), ("DW", 1)])
    doc = held(run(tmp_path, s), "inconclusive", "coverage_expansion_within_ceiling")
    pair = doc["comparisons"]["incumbent"]
    assert pair["newly_automated"] == {"wrong": 1, "total": 30}
    assert pair["total_wrong_automatic"] == {"candidate": 1, "comparator": 0}
    assert doc["outcome"] != "regression"


# ----------------------------------------------------------------- M: delta-review probes


LOOSE = {"max_coverage_loss_bps": 10000, "coverage_floor_bps": 0}


def test_m3_shared_and_newly_automated_errors_mixed_is_a_regression(tmp_path):
    """Probe m3: RR150, RW3, DW2, DR45. Three new errors on shared cases, no fixes."""
    s = Scenario([("RR", 150), ("RW", 3), ("DW", 2), ("DR", 45)])
    doc = held(run(tmp_path, s, pol=policy(**LOOSE)), "regression", "shared_case_errors_increased")
    assert doc["comparisons"]["incumbent"]["newly_automated"] == {"wrong": 2, "total": 47}


def test_m4_candidate_deferrals_cannot_offset_shared_case_regressions(tmp_path):
    """Probe m4: RR100, WD40, RW40, DR19, DW1. 40 new shared errors, 0 fixes, upper < 0."""
    s = Scenario([("RR", 100), ("WD", 40), ("RW", 40), ("DR", 19), ("DW", 1)])
    doc = held(run(tmp_path, s, pol=policy(**LOOSE)), "regression", "shared_case_errors_increased")
    pair = doc["comparisons"]["incumbent"]
    assert (pair["paired"]["fixes"], pair["paired"]["new_errors"]) == (0, 40)
    # The comparator's 40 errors on cases the candidate deferred balance the totals exactly.
    assert pair["total_wrong_automatic"] == {"candidate": 41, "comparator": 40}
    assert pair["newly_automated"] == {"wrong": 1, "total": 20}
    assert pair["uncertainty"]["upper"].startswith("-")
    assert "coverage_expansion_within_ceiling" not in json.dumps(doc["reasons"])


def test_m5_candidate_deferrals_cannot_hide_a_critical_slice_veto(tmp_path):
    """Probe m5: 20 fixes offset 20 shared new errors that all fall in the critical slice."""

    def critical(index, codes):
        return (CRITICAL,) if 20 <= index < 40 else ()  # the RW block

    s = Scenario(
        [("WR", 20), ("RW", 20), ("RR", 140), ("WD", 1), ("DW", 2), ("DR", 38)],
        slice_of=critical,
    )
    pol = policy(critical_slices=(CRITICAL,), **LOOSE)
    doc = held(run(tmp_path, s, pol=pol), "regression", "critical_slice_veto")
    pair = doc["comparisons"]["incumbent"]
    assert pair["critical_slices"][CRITICAL] == {
        "comparable": 20,
        "fixes": 0,
        "new_errors": 20,
        "status": "veto",
    }
    assert "coverage_expansion_within_ceiling" not in json.dumps(doc["reasons"])
