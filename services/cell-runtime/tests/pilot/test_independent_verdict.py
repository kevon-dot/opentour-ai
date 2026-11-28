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
        comparators=("incumbent", "simple_repair"),
        family_size=1,
        slice_of=None,
        prefix="",
        family_prefix=None,
        family_of=None,
    ):
        family_prefix = prefix if family_prefix is None else family_prefix
        cases, labels = [], {}
        self.preds = {"candidate": {}, **{c: {} for c in comparators}}
        index = 0
        for codes, count in plan:
            if isinstance(codes, str):
                # A two-letter plan gives every comparator the same transition to the candidate.
                codes = {**{c: codes[0] for c in comparators}, "candidate": codes[1]}
            for _ in range(count):
                case_id = f"{prefix}case-{index:04d}"
                truth = A if index % 2 == 0 else B
                labels[case_id] = truth
                slices = slice_of(index, codes) if slice_of else ()
                cases.append(
                    iv.DecisionCase(
                        case_id,
                        f"{family_prefix}family-"
                        + (family_of(index) if family_of else f"{index // family_size:04d}"),
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
        "required_comparators": ("incumbent", "simple_repair"),
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
    verdict = (ev or evaluator(tmp_path)).evaluate(
        claim=claim(scenario, pol=pol, **claim_over),
        population=scenario.population,
        audit_receipt=receipt or receipt_for(scenario.population),
        policy=pol,
        replays=replays or scenario.replays(),
        fit_manifest=manifest,
    )
    # Invariant on every verdict here (review 577a774 F5): verified_gain iff reproduced.
    doc = verdict.document
    assert (doc["outcome"] == "verified_gain") == (doc["verdict"] == "reproduced")
    return verdict


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
    # A tuned candidate's win stands only with a separate win against the simple repair too.
    assert doc["comparisons"]["simple_repair"]["outcome"] == "verified_gain"
    assert doc["comparisons"]["simple_repair"]["required"] is True
    assert doc["policy"]["required_comparators"] == ["incumbent", "simple_repair"]
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
    # A tuned candidate is compared against incumbent and simple repair too; here every
    # comparator carries the B4 real-only arm, and the real-only comparison is the one checked.
    s = Scenario(B4, comparators=("incumbent", "simple_repair", "real_only"))
    pol = policy(required_comparators=("incumbent", "simple_repair", "real_only"))
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
            "comparator_digests": {"incumbent": CANDIDATE, "simple_repair": SIMPLE},
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
        "uncovered": 0,
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
    # A gain whose counts the worker never stated is not reproduced, so it is never emitted as
    # verified_gain (review 577a774 F5): outcome and verdict always agree.
    unstated = held(
        run(tmp_path / "u", s, claimed_paired={}),
        "inconclusive",
        "claimed_counts_not_stated_incumbent",
    )
    assert "claimed_counts_not_stated_simple_repair" in unstated["reasons"]
    assert unstated["claim_counts_stated"] is False
    assert unstated["statements"]["paired_gain_cleared_gates"]["status"] == "inconclusive"
    partial = {"incumbent": recompute_pairs(s, "incumbent")}
    doc = held(run(tmp_path / "p", s, claimed_paired=partial), "inconclusive")
    assert doc["reasons"] == ["claimed_counts_not_stated_simple_repair"]
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
    assert len(rows) == 600 and all(  # 200 cases x (candidate, incumbent, simple repair)
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
    # A tuned candidate must face both the incumbent and the simple repair (OPE-528 AC).
    only_incumbent = Scenario(CLEAN_WIN, comparators=("incumbent",))
    with pytest.raises(iv.VerdictInputError, match="requires_incumbent_and_simple_repair"):
        run(tmp_path, only_incumbent, pol=policy(required_comparators=("incumbent",)))
    with pytest.raises(iv.VerdictInputError, match="requires_incumbent_and_simple_repair"):
        run(tmp_path, s, pol=policy(required_comparators=("incumbent",)))  # policy declares fewer
    with pytest.raises(iv.VerdictInputError, match="requires_incumbent_and_simple_repair"):
        run(tmp_path, s, comparator_digests={"incumbent": INCUMBENT})  # claim declares fewer
    with pytest.raises(iv.VerdictInputError, match="requires_incumbent_and_simple_repair"):
        run(  # simple repair present only as an optional comparator
            tmp_path,
            s,
            pol=policy(
                required_comparators=("incumbent",), optional_comparators=("simple_repair",)
            ),
        )
    repair = Scenario(CLEAN_WIN, comparators=("incumbent",))
    no_fit = run(
        tmp_path / "repair",
        repair,
        pol=policy(required_comparators=("incumbent",)),
        manifest=None,
        candidate_kind="simple_repair",
        job_manifest_digest=None,
    )
    assert no_fit.outcome == "verified_gain"  # a no-fit repair is compared to the incumbent
    assert not (tmp_path / "evaluator-custody" / "verdicts").exists() or not list(
        (tmp_path / "evaluator-custody" / "verdicts").iterdir()
    )


def test_i2_exported_record_is_digest_only_and_consumable_by_the_packet_boundary(tmp_path):
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    v = run(tmp_path, s, ev=ev)
    doc = v.document
    assert v.signature is None
    assert doc["evidence"] == {
        "evidence_tier": "digest_self_consistent_only",
        "store_seal": "evaluator_hmac_held_in_custody_not_exported",
        "audit_receipt_tier": "self_sealed_digest_only",
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
    # Only a re-read from the evaluator's own store, with its HMAC seal checked, is keyed.
    assert v.custody_tier == "digest_self_consistent_only"
    reread = run(tmp_path, s, ev=ev)
    assert reread.replayed and reread.custody_tier == "locally_keyed"
    assert reread.document == doc  # the tier is never written into the document


def test_i2b_why_the_export_says_digest_only_a_recomputed_forgery_passes_the_digest_check(
    tmp_path,
):
    """Review 577a774 F4: anyone can rewrite the export and recompute its plain digest."""
    v = run(tmp_path, Scenario([("RW", 120), ("RR", 80)]))
    forged = {k: x for k, x in v.document.items() if k != "verdict_digest"}
    forged.update(outcome="verified_gain", verdict="reproduced")
    forged["verdict_digest"] = digest(forged)
    assert cp._verdict_ok(forged)  # a digest-only consumer cannot tell
    assert forged["evidence"]["evidence_tier"] == "digest_self_consistent_only"


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
    # A signature never self-upgrades the exported tier; raising it is the verifier's decision.
    assert v.document["evidence"]["evidence_tier"] == "digest_self_consistent_only"


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
    every = ("incumbent", "simple_repair", "real_only")
    s = Scenario(B4, comparators=every)
    strict = policy(required_comparators=every)
    loose = policy(
        required_comparators=every,
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


def test_q1b_declared_basis_never_waives_the_fresh_cohort_gate(tmp_path):
    """Review 577a774 F1: a reselected claim declared frozen_before_readout got verified_gain."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    first = run(tmp_path, s, ev=ev)
    assert first.outcome == "verified_gain"
    assert first.document["selection_basis_source"] == "caller_asserted"
    for basis in ("frozen_before_readout", "after_protected_readout"):
        doc = held(
            run(
                tmp_path,
                s,
                ev=ev,
                result_revision=sha("reselected-" + basis),
                selection_basis=basis,
            ),
            "inconclusive",
            "fresh_confirmation_cohort_required",
        )
        assert doc["selection_basis"] == basis
    # The claim's own key is still the one marker skipped, and a disjoint cohort still passes.
    assert run(tmp_path, s, ev=ev).document == first.document
    assert run(tmp_path, Scenario(CLEAN_WIN, prefix="new-"), ev=ev).outcome == "verified_gain"
    assert "caller-asserted" in (iv.IndependentEvaluator._check_selection.__doc__ or "")


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
        "uncovered": 0,
        "status": "veto",
    }
    assert "coverage_expansion_within_ceiling" not in json.dumps(doc["reasons"])


def test_b1_retention_limit_is_exclusive_at_retention_until(tmp_path, monkeypatch):
    """Mutant: retention ``<=`` -> ``<`` would leave a record readable at its limit."""
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    path = tmp_path / "evaluator-custody" / "predictions" / (v.verdict_key + ".json")
    until = json.loads(path.read_bytes())["retention_until"]
    monkeypatch.setattr(iv.time, "time", lambda: until - 1)
    assert ev.private_predictions(v.verdict_key, accessor_id=EVALUATOR)["retention_until"] == until
    assert ev.purge_expired() == 0
    monkeypatch.setattr(iv.time, "time", lambda: until)
    with pytest.raises(iv.CustodyError, match="private_predictions_expired"):
        ev.private_predictions(v.verdict_key, accessor_id=EVALUATOR)
    assert ev.purge_expired() == 1


@pytest.mark.parametrize(
    "lower,upper,outcome,reason",
    [
        ("0.020000", "0.500000", "inconclusive", "interval_spans_threshold"),  # lower == threshold
        ("0.020001", "0.500000", "verified_gain", None),
        ("-0.100000", "0.020000", "inconclusive", "interval_spans_threshold"),  # upper == it
        ("-0.100000", "0.019999", "no_material_gain", "upper_bound_below_threshold"),
    ],
)
def test_b2_interval_decision_boundaries_at_the_threshold(
    tmp_path, monkeypatch, lower, upper, outcome, reason
):
    """Mutant: ``lower > threshold`` -> ``>=`` must fail when lower equals the 200 bps threshold."""

    def fixed(effects, alpha_bps):
        return {
            "method": iv.INTERVAL_METHOD,
            "status": "available",
            "families": len(effects),
            "alpha_bps": alpha_bps,
            "estimate": "0.100000",
            "lower": lower,
            "upper": upper,
        }

    monkeypatch.setattr(iv, "_family_interval", fixed)
    v = run(tmp_path, Scenario(CLEAN_WIN))
    assert v.outcome == outcome
    if reason is not None:
        held(v, outcome, reason)


def test_b3_slice_veto_fires_above_a_tie_not_at_it(tmp_path):
    """Mutant: slice veto ``>`` -> ``>=`` must fail: 5 fixes and 5 new errors is not a veto."""
    for errors, status in ((5, "ok"), (6, "veto")):
        plan = [("WR", 115), ("RR", 80 - errors), ("WR", 5), ("RW", errors)]
        first = 195 - errors  # the WR x5 and RW x errors blocks are the critical slice
        s = Scenario(plan, slice_of=lambda i, c, first=first: (CRITICAL,) if i >= first else ())
        pol = policy(critical_slices=(CRITICAL,))
        doc = run(tmp_path / str(errors), s, pol=pol).document
        sl = doc["comparisons"]["incumbent"]["critical_slices"][CRITICAL]
        assert (sl["comparable"], sl["fixes"], sl["new_errors"]) == (5 + errors, 5, errors)
        assert sl["status"] == status
        assert ("critical_slice_veto" in json.dumps(doc["reasons"])) is (status == "veto")


def test_m6_review_f2_candidate_errors_on_comparator_gaps_reach_the_slice_veto(tmp_path):
    """Review 577a774 F2: WR120, RR77, RR3, MW5, last 8 in the slice, read comparable 3, ok."""

    def last_eight(index, codes):
        return (CRITICAL,) if index >= 197 else ()

    s = Scenario([("WR", 120), ("RR", 77), ("RR", 3), ("MW", 5)], slice_of=last_eight)
    pol = policy(critical_slices=(CRITICAL,), max_missing=5)
    doc = held(run(tmp_path, s, pol=pol), "regression", "critical_slice_veto")
    pair = doc["comparisons"]["incumbent"]
    assert pair["critical_slices"][CRITICAL] == {
        "comparable": 3,
        "fixes": 0,
        "new_errors": 5,
        "uncovered": 5,
        "status": "veto",
    }
    assert pair["comparator_gap_candidate_wrong"] == 5


def test_m7_review_f2_candidate_missing_slice_cases_hold_the_comparison(tmp_path):
    """Review 577a774 F2: RR3 + RM5 inside the slice gave verified_gain on 3 comparable cases."""

    def last_eight(index, codes):
        return (CRITICAL,) if index >= 197 else ()

    s = Scenario([("WR", 120), ("RR", 77), ("RR", 3), ("RM", 5)], slice_of=last_eight)
    pol = policy(critical_slices=(CRITICAL,), max_missing=5)
    doc = held(run(tmp_path, s, pol=pol), "inconclusive", "critical_slice_insufficient_comparable")
    slice_ = doc["comparisons"]["incumbent"]["critical_slices"][CRITICAL]
    assert slice_ == {
        "comparable": 3,
        "fixes": 0,
        "new_errors": 0,
        "uncovered": 5,
        "status": "insufficient_comparable",
    }
    # The share boundary: 7 comparable of 10 is exactly the 7000 bps floor and is not held;
    # 6 of 10 is below it and is.
    for comparable, status in ((7, "ok"), (6, "insufficient_comparable")):
        plan = [("WR", 120), ("RR", 70), ("RR", comparable), ("RM", 10 - comparable)]
        t = Scenario(plan, slice_of=lambda i, c: (CRITICAL,) if i >= 190 else ())
        loose = policy(critical_slices=(CRITICAL,), max_missing=4)
        out = run(tmp_path / f"share-{comparable}", t, pol=loose).document
        assert out["comparisons"]["incumbent"]["critical_slices"][CRITICAL]["status"] == status


def test_m8_a_slice_veto_is_not_masked_by_thin_support(tmp_path):
    """Slice errors regress even when the slice is below its minimum comparable cases."""
    t = {**terms_for((CRITICAL,)), "minimum_slice_cases": 50}
    p = policy(
        critical_slices=(CRITICAL,),
        operating_point_terms=t,
        operating_point_digest=digest(t),
    )
    s = Scenario(
        [("WR", 120), ("RR", 70), ("RW", 10)],
        slice_of=lambda i, c: (CRITICAL,) if i >= 190 else (),
    )
    doc = held(run(tmp_path, s, pol=p), "regression", "critical_slice_veto")
    assert doc["comparisons"]["incumbent"]["critical_slices"][CRITICAL]["comparable"] == 10


# ----------------------------------------------------------------- G: final delta-review tests


def test_g1_unoffset_candidate_errors_on_comparator_gaps_are_a_regression(tmp_path):
    """Mutant: turning the total-wrong regression into inconclusive must fail here."""
    s = Scenario([("RR", 150), ("MW", 5)])
    pol = policy(max_missing=5, **LOOSE)
    doc = held(run(tmp_path, s, pol=pol), "regression", "total_wrong_automatic_increased")
    pair = doc["comparisons"]["incumbent"]
    assert pair["comparator_gaps"] == 5 and pair["comparator_gap_candidate_wrong"] == 5
    assert pair["total_wrong_automatic"] == {"candidate": 5, "comparator": 0}
    assert (pair["paired"]["fixes"], pair["paired"]["new_errors"]) == (0, 0)


def test_g2_only_the_interval_gate_catches_errors_spread_across_families(tmp_path):
    """Mutant: removing the upper-bound-below-zero gate must fail here.

    Fixes and new errors tie at 50, so the shared-case and total-wrong gates do not fire, but
    the 50 fixes sit in one family and the 50 new errors in 50 families: the family mean is
    (1 - 50) / 51 and its whole interval lies below zero.
    """
    s = Scenario(
        [("WR", 50), ("RW", 50)],
        family_of=lambda i: "big" if i < 50 else f"{i:04d}",
    )
    doc = held(run(tmp_path, s), "regression", "upper_bound_below_zero")
    pair = doc["comparisons"]["incumbent"]
    assert (pair["paired"]["fixes"], pair["paired"]["new_errors"]) == (50, 50)
    assert pair["total_wrong_automatic"] == {"candidate": 50, "comparator": 50}
    assert pair["uncertainty"]["families"] == 51
    assert pair["uncertainty"]["upper"].startswith("-")


def test_g3_shared_case_gate_boundary(tmp_path):
    """Mutant: ``new_errors > fixes + 1`` must fail here (fires at fixes + 1, not at fixes)."""
    over = Scenario([("WR", 10), ("RW", 11), ("RR", 179)])
    doc = held(run(tmp_path / "over", over), "regression", "shared_case_errors_increased")
    assert doc["comparisons"]["incumbent"]["paired"]["fixes"] == 10
    assert doc["comparisons"]["incumbent"]["paired"]["new_errors"] == 11
    tie = Scenario([("WR", 10), ("RW", 10), ("RR", 180)])
    doc = held(run(tmp_path / "tie", tie), "inconclusive", "interval_spans_threshold")
    assert not any("shared_case_errors_increased" in r for r in doc["reasons"])


def test_g4_probe4_gap_errors_offset_by_deferrals_are_not_called_coverage_expansion(tmp_path):
    """Probe4 g_gap_offset_by_deferral: RR150, MW5, WD5, DR38, DW2 with max_missing 5."""
    s = Scenario([("RR", 150), ("MW", 5), ("WD", 5), ("DR", 38), ("DW", 2)])
    pol = policy(max_missing=5, **LOOSE)
    doc = held(run(tmp_path, s, pol=pol), "inconclusive", "comparator_gap_errors_offset")
    pair = doc["comparisons"]["incumbent"]
    assert pair["total_wrong_automatic"] == {"candidate": 7, "comparator": 5}
    assert pair["newly_automated"] == {"wrong": 2, "total": 40}
    assert pair["comparator_gap_candidate_wrong"] == 5
    assert not any("coverage_expansion_within_ceiling" in r for r in doc["reasons"])


def test_g5_residual_total_wrong_gate_boundary(tmp_path):
    """Mutant: ``excess > newly_wrong + 1`` must fail here (fires at newly_wrong + 1, not at it)."""
    pol = policy(max_missing=2, **LOOSE)
    # excess = 3 - 1 = 2 = newly_wrong + 1: one gap error is left unexplained.
    over = Scenario([("RR", 150), ("DW", 1), ("DR", 39), ("MW", 2), ("WD", 1)])
    doc = held(
        run(tmp_path / "over", over, pol=pol), "regression", "total_wrong_automatic_increased"
    )
    pair = doc["comparisons"]["incumbent"]
    assert pair["total_wrong_automatic"] == {"candidate": 3, "comparator": 1}
    assert pair["newly_automated"] == {"wrong": 1, "total": 40}
    # excess = 3 - 2 = 1 = newly_wrong: the residual gate must not fire.
    tie = Scenario([("RR", 150), ("DW", 1), ("DR", 39), ("MW", 2), ("WD", 2)])
    doc = held(run(tmp_path / "tie", tie, pol=pol), "inconclusive", "comparator_gap_errors_offset")
    assert doc["comparisons"]["incumbent"]["total_wrong_automatic"] == {
        "candidate": 3,
        "comparator": 2,
    }
    assert not any("total_wrong_automatic_increased" in r for r in doc["reasons"])


# ----------------------------------------------------------------- V: correction and revocation


def revoked_held(view, reason="claim_corrected"):
    """A revoked key reads as stale/revoked, never as its original outcome or effects."""
    assert view["schema_version"] == iv.HELD_VIEW
    assert view["outcome"] == "stale" and view["state"] == "revoked"
    assert view["verdict"] == "not_reproduced"
    assert view["reasons"] == ["revocation_reason_" + reason, "verdict_revoked"]
    assert view["effects"] == {
        "improvement_alert_eligible": False,
        "release_request_eligible": False,
        "handoff_request_eligible": False,
    }
    assert "verified_gain" not in json.dumps(view)
    assert not cp._verdict_ok(view)  # the OPE-421 packet boundary refuses it as unverified
    return view


def custody_files(tmp_path, sub):
    return sorted(p.name for p in (tmp_path / "evaluator-custody" / sub).glob("*.json"))


def test_v1_revoked_verified_gain_reads_as_stale_and_every_consumer_check_refuses(tmp_path):
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    replays = s.replays()
    v = run(tmp_path, s, ev=ev, replays=replays)
    assert v.outcome == "verified_gain"
    assert ev.authorize(v.document, effect="improvement_alert")["authorized"] is True
    assert ev.read_verdict(v.verdict_key) == v.document
    r = ev.revoke_verdict(v.verdict_key, reason="claim_corrected", issued_by=EVALUATOR)
    assert not r.replayed and r.notice is None
    rec = r.record
    assert rec["schema_version"] == iv.REVOCATION and rec["verdict_key"] == v.verdict_key
    assert rec["revoked_verdict_digests"] == [v.document["verdict_digest"]]
    assert rec["issued_by"] == EVALUATOR and rec["issued_by_authenticated"] is False
    assert rec["revocation_digest"] == digest(
        {k: x for k, x in rec.items() if k != "revocation_digest"}
    )
    revoked_held(ev.read_verdict(v.verdict_key))
    for effect in ("improvement_alert", "release_request", "handoff_request"):
        out = ev.authorize(v.document, effect=effect)
        assert out == {
            "authorized": False,
            "effect": effect,
            "reason": "verdict_revoked",
            "state": "revoked",
        }
    # Re-reading through evaluate returns the revoked view and never re-scores.
    again = run(tmp_path, s, ev=ev, replays=replays)
    assert again.replayed and again.outcome == "stale"
    revoked_held(again.document)
    assert len(replays["candidate"].calls) == 1
    # A restarted evaluator over the same custody reads the same revocation.
    revoked_held(evaluator(tmp_path).read_verdict(v.verdict_key))


def test_v2_revoking_twice_is_idempotent_and_the_first_record_is_final(tmp_path):
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    first = ev.revoke_verdict(v.verdict_key, reason="claim_corrected", issued_by=EVALUATOR)
    second = ev.revoke_verdict(v.verdict_key, reason="scorer_defect", issued_by="generated-other")
    assert second.replayed and second.record == first.record
    assert second.record["reason"] == "claim_corrected"
    assert custody_files(tmp_path, "revocations") == [v.verdict_key + ".json"]
    revoked_held(ev.read_verdict(v.verdict_key))


def test_v3_revoke_after_expiry_covers_live_archived_and_reissued_generations(
    tmp_path, monkeypatch
):
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    v1 = run(tmp_path, s, ev=ev)
    now = int(iv.time.time())
    monkeypatch.setattr(iv.time, "time", lambda: now + 8 * 86_400)
    expired = ev.read_verdict(v1.verdict_key)
    assert expired["state"] == "expired" and expired["outcome"] == "stale"
    assert ev.authorize(v1.document, effect="improvement_alert")["reason"] == "verdict_expired"
    # Re-evaluation after expiry archives generation one and issues generation two.
    monkeypatch.setattr(iv.time, "time", lambda: now + 8 * 86_400 + 5)
    v2 = run(tmp_path, s, ev=ev)
    assert not v2.replayed and v2.verdict_key == v1.verdict_key
    assert ev.authorize(v1.document, effect="improvement_alert")["reason"] == (
        "verdict_document_mismatch"  # an archived generation never regains authority
    )
    # Generation two expires too; the revocation still lands and covers both generations.
    monkeypatch.setattr(iv.time, "time", lambda: now + 20 * 86_400)
    r = ev.revoke_verdict(v1.verdict_key, reason="answer_key_defect", issued_by=EVALUATOR)
    assert r.record["revoked_verdict_digests"] == sorted(
        {v1.document["verdict_digest"], v2.document["verdict_digest"]}
    )
    revoked_held(ev.read_verdict(v1.verdict_key), reason="answer_key_defect")
    replays = s.replays()
    again = run(tmp_path, s, ev=ev, replays=replays)
    assert again.outcome == "stale" and replays["candidate"].calls == []  # never re-issued
    assert custody_files(tmp_path, "verdicts") == [v1.verdict_key + ".json"]
    assert ev.read_verdict(v1.verdict_key)["state"] == "revoked"


def test_v4_revoking_an_unknown_or_invalid_key_stores_nothing(tmp_path):
    ev = evaluator(tmp_path)
    run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    with pytest.raises(iv.VerdictInputError, match="revocation_target_unknown"):
        ev.revoke_verdict(sha("never-issued"), reason="claim_corrected", issued_by=EVALUATOR)
    for kwargs, reason in (
        ({"key": "not-a-key"}, "invalid_verdict_key"),
        ({"reason": "free text explaining why"}, "invalid_revocation_reason"),
        ({"issued_by": "has spaces"}, "invalid_revocation_issuer"),
        ({"watch_fired": 1}, "invalid_watch_fired_flag"),
    ):
        args = {
            "key": sha("never-issued"),
            "reason": "claim_corrected",
            "issued_by": EVALUATOR,
            **kwargs,
        }
        key = args.pop("key")
        with pytest.raises(iv.VerdictInputError, match=reason):
            ev.revoke_verdict(key, **args)
    with pytest.raises(iv.VerdictInputError, match="verdict_not_revoked"):
        ev.correction_notice(sha("never-issued"), watch_fired=True)
    with pytest.raises(iv.VerdictInputError, match="verdict_unknown"):
        ev.read_verdict(sha("never-issued"))
    assert custody_files(tmp_path, "revocations") == []
    assert custody_files(tmp_path, "notices") == []


@pytest.mark.parametrize(
    "tamper,error",
    [
        ("reason", "stored_revocation_tampered"),  # resealed digest without the evaluator key
        ("key", "stored_revocation_tampered"),  # pointed at another key
        ("seal", "stored_revocation_tampered"),  # local seal replaced
        ("garbage", "stored_revocation_corrupt"),
    ],
)
def test_v5_a_tampered_revocation_record_is_refused_everywhere(tmp_path, tamper, error):
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    v = run(tmp_path, s, ev=ev)
    ev.revoke_verdict(v.verdict_key, reason="claim_corrected", issued_by=EVALUATOR)
    path = tmp_path / "evaluator-custody" / "revocations" / (v.verdict_key + ".json")
    stored = json.loads(path.read_bytes())
    rec = stored["record"]
    if tamper == "reason":
        rec["reason"] = "evidence_withdrawn"
    elif tamper == "key":
        rec["verdict_key"] = sha("other-key")
    if tamper in ("reason", "key"):
        body = {k: x for k, x in rec.items() if k != "revocation_digest"}
        rec["revocation_digest"] = digest(body)
    if tamper == "seal":
        stored["local_seal"] = "0" * 64
    path.write_text("{not json" if tamper == "garbage" else json.dumps(stored))
    for call in (
        lambda: ev.read_verdict(v.verdict_key),
        lambda: ev.authorize(v.document, effect="improvement_alert"),
        lambda: run(tmp_path, s, ev=ev),
        lambda: ev.revoke_verdict(v.verdict_key, reason="claim_corrected", issued_by=EVALUATOR),
        lambda: ev.correction_notice(v.verdict_key, watch_fired=True),
    ):
        with pytest.raises(iv.CustodyError, match=error):
            call()
    assert custody_files(tmp_path, "notices") == []


def test_v6_exactly_one_correction_notice_only_when_a_watch_fired(tmp_path):
    ev = evaluator(tmp_path)
    quiet = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    r = ev.revoke_verdict(quiet.verdict_key, reason="claim_corrected", issued_by=EVALUATOR)
    assert r.notice is None and ev.correction_notice(quiet.verdict_key, watch_fired=False) is None
    assert custody_files(tmp_path, "notices") == []  # no watch fired: no notice owed
    fired = run(tmp_path, Scenario(CLEAN_WIN, prefix="fired-"), ev=ev)
    r = ev.revoke_verdict(
        fired.verdict_key, reason="scorer_defect", issued_by=EVALUATOR, watch_fired=True
    )
    assert r.notice is not None and not r.notice.replayed
    notice = r.notice.document
    assert notice["schema_version"] == iv.CORRECTION_NOTICE
    assert notice["revocation_digest"] == r.record["revocation_digest"]
    assert notice["revoked_verdict_digests"] == [fired.document["verdict_digest"]]
    assert notice["notice_id"].startswith("correction-")
    retries = [
        ev.correction_notice(fired.verdict_key, watch_fired=True),
        ev.correction_notice(fired.verdict_key, watch_fired=False),
        ev.revoke_verdict(
            fired.verdict_key, reason="scorer_defect", issued_by=EVALUATOR, watch_fired=True
        ).notice,
    ]
    assert all(n is not None and n.replayed and n.document == notice for n in retries)
    assert custody_files(tmp_path, "notices") == [fired.verdict_key + ".json"]
    assert "case-0000" not in json.dumps(notice) and A not in json.dumps(notice)


def test_v7_restart_between_revoke_and_notice_still_yields_exactly_one_notice(tmp_path):
    first = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=first)
    # The process stops after the revocation is durable and before any notice is written.
    first.revoke_verdict(v.verdict_key, reason="evidence_withdrawn", issued_by=EVALUATOR)
    assert custody_files(tmp_path, "notices") == []
    restarted = evaluator(tmp_path)
    retry = restarted.revoke_verdict(
        v.verdict_key, reason="evidence_withdrawn", issued_by=EVALUATOR, watch_fired=True
    )
    assert retry.replayed and retry.notice is not None and not retry.notice.replayed
    # A second restart, and a concurrent caller over the same custody, get the same notice.
    again = evaluator(tmp_path).correction_notice(v.verdict_key, watch_fired=True)
    other = evaluator(tmp_path).revoke_verdict(
        v.verdict_key, reason="evidence_withdrawn", issued_by=EVALUATOR, watch_fired=True
    )
    assert again is not None and again.replayed and again.document == retry.notice.document
    assert other.notice is not None and other.notice.document == retry.notice.document
    assert custody_files(tmp_path, "notices") == [v.verdict_key + ".json"]
    revoked_held(restarted.read_verdict(v.verdict_key), reason="evidence_withdrawn")


def test_v8_a_lost_write_race_returns_the_first_notice_and_revocation(tmp_path, monkeypatch):
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    real = iv._write_once
    raced: list[str] = []

    def racing(path, data):
        # Another process lands the same record first; this writer must lose and read it back.
        sub = path.parent.name
        if sub in ("revocations", "notices") and sub not in raced:
            raced.append(sub)
            monkeypatch.setattr(iv, "_write_once", real)
            other = evaluator(tmp_path)
            if sub == "revocations":
                other.revoke_verdict(v.verdict_key, reason="claim_corrected", issued_by="racer-1")
            else:
                other.correction_notice(v.verdict_key, watch_fired=True)
            monkeypatch.setattr(iv, "_write_once", racing)
        return real(path, data)

    monkeypatch.setattr(iv, "_write_once", racing)
    r = ev.revoke_verdict(
        v.verdict_key, reason="scorer_defect", issued_by=EVALUATOR, watch_fired=True
    )
    assert raced == ["revocations", "notices"]
    assert r.replayed and r.record["issued_by"] == "racer-1"
    assert r.notice is not None and r.notice.replayed
    assert custody_files(tmp_path, "notices") == [v.verdict_key + ".json"]


def test_v9_a_tampered_correction_notice_is_refused(tmp_path):
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    ev.revoke_verdict(
        v.verdict_key, reason="claim_corrected", issued_by=EVALUATOR, watch_fired=True
    )
    path = tmp_path / "evaluator-custody" / "notices" / (v.verdict_key + ".json")
    stored = json.loads(path.read_bytes())
    stored["notice"]["revocation_digest"] = sha("other-revocation")
    body = {k: x for k, x in stored["notice"].items() if k != "notice_digest"}
    stored["notice"]["notice_digest"] = digest(body)
    path.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_correction_notice_tampered"):
        ev.correction_notice(v.verdict_key, watch_fired=True)
    path.write_text("[]")
    with pytest.raises(iv.CustodyError, match="stored_correction_notice_corrupt"):
        ev.correction_notice(v.verdict_key, watch_fired=False)


def test_v11_each_seal_layer_is_checked_on_its_own(tmp_path):
    """Mutants: each of digest, HMAC, key and revocation binding must fail closed by itself."""
    ev = evaluator(tmp_path)
    a = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    b = run(tmp_path, Scenario(CLEAN_WIN, prefix="other-"), ev=ev)
    ev.revoke_verdict(
        a.verdict_key, reason="claim_corrected", issued_by=EVALUATOR, watch_fired=True
    )
    root = tmp_path / "evaluator-custody"
    rev_a = root / "revocations" / (a.verdict_key + ".json")
    good_rev = rev_a.read_bytes()
    # A writer holding the key but leaving a stale digest: only the digest check catches it.
    stored = json.loads(good_rev)
    stored["record"]["reason"] = "scorer_defect"
    stored["local_seal"] = ev._commit({"record": stored["record"]})
    rev_a.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_revocation_tampered"):
        ev.read_verdict(a.verdict_key)
    rev_a.write_bytes(good_rev)
    # A valid revocation copied under another key: only the key binding catches it.
    (root / "revocations" / (b.verdict_key + ".json")).write_bytes(good_rev)
    with pytest.raises(iv.CustodyError, match="stored_revocation_tampered"):
        ev.read_verdict(b.verdict_key)
    (root / "revocations" / (b.verdict_key + ".json")).unlink()
    assert ev.read_verdict(b.verdict_key) == b.document
    notice_a = root / "notices" / (a.verdict_key + ".json")
    good_notice = notice_a.read_bytes()
    # Resealed digest without the evaluator key: only the HMAC catches it.
    stored = json.loads(good_notice)
    stored["notice"]["reason"] = "scorer_defect"
    body = {k: x for k, x in stored["notice"].items() if k != "notice_digest"}
    stored["notice"]["notice_digest"] = digest(body)
    notice_a.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_correction_notice_tampered"):
        ev.correction_notice(a.verdict_key, watch_fired=True)
    # A fully keyed notice bound to another revocation: only the binding catches it.
    stored["notice"]["revocation_digest"] = sha("other-revocation")
    body = {k: x for k, x in stored["notice"].items() if k != "notice_digest"}
    stored["notice"]["notice_digest"] = digest(body)
    stored["local_seal"] = ev._commit({"notice": stored["notice"]})
    notice_a.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_correction_notice_tampered"):
        ev.correction_notice(a.verdict_key, watch_fired=True)
    notice_a.write_bytes(good_notice)
    assert ev.correction_notice(a.verdict_key, watch_fired=True) is not None


def test_v10_consumer_check_refuses_forged_unknown_and_ineligible_documents(tmp_path):
    ev = evaluator(tmp_path)
    win = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    non_win = run(tmp_path, Scenario(B4, prefix="b4-"), ev=ev)
    assert ev.authorize(non_win.document, effect="improvement_alert") == {
        "authorized": False,
        "effect": "improvement_alert",
        "reason": "effect_not_eligible",
        "state": "inconclusive",
    }
    assert ev.authorize(win.document, effect="release_request")["reason"] == "effect_not_eligible"
    forged = {**win.document, "outcome": "verified_gain", "reasons": []}
    forged["effects"] = {**forged["effects"], "release_request_eligible": True}
    assert ev.authorize(forged, effect="release_request")["reason"] == "verdict_document_mismatch"
    other = evaluator(tmp_path / "other")
    assert other.authorize(win.document, effect="improvement_alert")["reason"] == "verdict_unknown"
    view = ev.read_verdict(win.verdict_key)
    ev.revoke_verdict(win.verdict_key, reason="claim_corrected", issued_by=EVALUATOR)
    held_view = ev.read_verdict(win.verdict_key)
    for doc in (held_view, {}, None, {"schema_version": iv.VERDICT, "verdict_key": "x"}):
        assert ev.authorize(doc, effect="improvement_alert")["reason"] == "verdict_unverified"
    assert ev.authorize(view, effect="improvement_alert")["reason"] == "verdict_revoked"
    with pytest.raises(iv.VerdictInputError, match="unknown_consumer_effect"):
        ev.authorize(win.document, effect="slack_post")
