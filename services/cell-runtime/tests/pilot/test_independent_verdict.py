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
        content_prefix=None,
    ):
        family_prefix = prefix if family_prefix is None else family_prefix
        # A prefixed scenario is new decision content unless the test says otherwise.
        content_prefix = prefix if content_prefix is None else content_prefix
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
                        {"case_ref": case_id, "text": f"generated-input-{content_prefix}{index}"},
                        tuple(slices),
                    )
                )
                for arm, code in codes.items():
                    self.preds[arm][case_id] = _pred(code, truth)
                index += 1
        self.population = iv.SealedPopulation(
            cases,
            labels,
            label_set=(A, B),
            benchmark_digest=BENCHMARK,
            reference_fields=("case_ref",),  # the generated schema's declared id field
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
    provider=None,
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
        provider_evidence=provider,
    )
    # Invariant on every verdict here (review 577a774 F5): verified_gain iff reproduced.
    doc = verdict.document
    assert (doc["outcome"] == "verified_gain") == (doc["verdict"] == "reproduced")
    if iv.REGISTERED_POLICY_FLOOR is None:
        # Re-review M2: with no D11 floor registered, no verdict here makes any effect eligible.
        assert not any(doc["effects"].values())
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
    # The verdict's own gates cleared, but no D11 policy floor is registered, so no effect.
    assert doc["alert_gates"] == {
        "cleared": True,
        "policy_floor": "unset",
        "policy_floor_hold": "policy_floor_unset",
    }
    assert doc["effects"]["improvement_alert_eligible"] is False
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


def respelled(scenario, features_of, reference_fields=None):
    """The same scenario with each case's features rewritten by ``features_of(case)``."""
    cases = [
        iv.DecisionCase(c.case_id, c.family_id, features_of(c), c.slices)
        for c in scenario.population.cases
    ]
    labels = {c.case_id: scenario.population.truth(c.case_id) for c in cases}
    scenario.population = iv.SealedPopulation(
        cases,
        labels,
        label_set=(A, B),
        benchmark_digest=BENCHMARK,
        reference_fields=(
            scenario.population.reference_fields if reference_fields is None else reference_fields
        ),
    )
    return scenario


def renamed_to(scenario, id_of):
    """The same scenario with every case id renamed by ``id_of(case)``; content unchanged."""
    new_ids = {c.case_id: id_of(c) for c in scenario.population.cases}
    cases = [
        iv.DecisionCase(
            new_ids[c.case_id],
            c.family_id,
            {**c.features, "case_ref": new_ids[c.case_id]},
            c.slices,
        )
        for c in scenario.population.cases
    ]
    labels = {new_ids[c]: scenario.population.truth(c) for c in new_ids}
    scenario.preds = {
        arm: {new_ids[c]: p for c, p in preds.items()} for arm, preds in scenario.preds.items()
    }
    scenario.population = iv.SealedPopulation(
        cases,
        labels,
        label_set=(A, B),
        benchmark_digest=BENCHMARK,
        reference_fields=scenario.population.reference_fields,
    )
    return scenario


def test_q1c_review_m1_renamed_ids_over_scored_content_are_held(tmp_path):
    """Re-review M1: renamed ids with identical decision text came back verified_gain."""
    ev = evaluator(tmp_path)
    assert run(tmp_path, Scenario(CLEAN_WIN), ev=ev).outcome == "verified_gain"
    renamed = Scenario(CLEAN_WIN, prefix="renamed-", content_prefix="")
    assert not {c.case_id for c in renamed.population.cases} & {
        c.case_id for c in Scenario(CLEAN_WIN).population.cases
    }
    doc = held(
        run(tmp_path, renamed, ev=ev, result_revision=sha("renamed")),
        "inconclusive",
        "decision_content_seen_in_scored_cohort",
    )
    assert "fresh_confirmation_cohort_required" not in doc["reasons"]  # ids did not overlap


@pytest.mark.parametrize(
    "respell,refs",
    [
        # Feature names renamed and re-cased (keys must stay ASCII identifiers).
        (lambda c: {"case_ref": c.features["case_ref"], "TEXT_Body": c.features["text"]}, None),
        # Feature order reversed.
        (lambda c: dict(reversed(list(c.features.items()))), None),
        # A declared extra reference field carrying the (renamed) id in another spelling.
        (
            lambda c: {**c.features, "source": "ref:" + c.case_id + ".json"},
            ("case_ref", "source"),
        ),
        # A declared field carrying the family id.
        (lambda c: {**c.features, "group": c.family_id}, ("case_ref", "group")),
        # A3: insignificant surrounding whitespace on a value.
        (lambda c: {**c.features, "text": " \t" + c.features["text"] + " \n"}, None),
    ],
    ids=["renamed_keys", "reordered", "id_in_reference_field", "family_id_field", "a3_spacing"],
)
def test_q1d_nothing_a_producer_can_rename_evades_the_content_register(tmp_path, respell, refs):
    ev = evaluator(tmp_path)
    assert run(tmp_path, Scenario(CLEAN_WIN), ev=ev).outcome == "verified_gain"
    moved = respelled(Scenario(CLEAN_WIN, prefix="moved-", content_prefix=""), respell, refs)
    held(
        run(tmp_path, moved, ev=ev, result_revision=sha("moved")),
        "inconclusive",
        "decision_content_seen_in_scored_cohort",
    )


def test_q1g_feature_order_never_changes_content_with_several_values(tmp_path):
    """Mutant: an unsorted value list must fail when two content values swap positions."""

    def two_values(c):
        return {**c.features, "context": "generated-context-" + c.features["text"][-4:]}

    ev = evaluator(tmp_path)
    assert run(tmp_path, respelled(Scenario(CLEAN_WIN), two_values), ev=ev).outcome == (
        "verified_gain"
    )
    swapped = respelled(
        Scenario(CLEAN_WIN, prefix="swapped-", content_prefix=""),
        lambda c: dict(reversed(list(two_values(c).items()))),
    )
    held(
        run(tmp_path, swapped, ev=ev, result_revision=sha("swapped")),
        "inconclusive",
        "decision_content_seen_in_scored_cohort",
    )


def test_q1e_internal_decision_content_is_not_normalised(tmp_path):
    """Changed decision text is a new input, so it is a fresh cohort; only edges are trimmed."""
    ev = evaluator(tmp_path)
    assert run(tmp_path, Scenario(CLEAN_WIN), ev=ev).outcome == "verified_gain"
    for name, change in (
        ("upper", str.upper),
        ("inner-space", lambda t: t.replace("-", " - ", 1)),
        ("appended", lambda t: t + "!"),
    ):
        fresh = respelled(
            Scenario(CLEAN_WIN, prefix=name + "-", content_prefix=""),
            lambda c, change=change: {**c.features, "text": change(c.features["text"])},
        )
        assert run(tmp_path, fresh, ev=ev).outcome == "verified_gain"
    refs = ("case_ref",)
    a = Scenario(CLEAN_WIN).population.cases[0]
    b = iv.DecisionCase("other-id", "other-family", {"text": a.features["text"]}, ())
    assert iv.decision_content_digest(a, refs) == iv.decision_content_digest(b, refs)
    assert iv.decision_content_digest(a, refs) == iv.decision_content_digest(
        iv.DecisionCase("other-id", "f", {"body": "  " + a.features["text"] + "\t"}, ()), refs
    )
    assert iv.decision_content_digest(a, refs) != iv.decision_content_digest(
        iv.DecisionCase("other-id", "other-family", {"text": a.features["text"] + "!"}, ()), refs
    )
    # Internal whitespace is content: a run of two spaces is not one space.
    one, two = (iv.DecisionCase("x", "f", {"text": t}, ()) for t in ("a b", "a  b"))
    assert iv.decision_content_digest(one) != iv.decision_content_digest(two)
    # Exclusion is by declared field name only, never by what a value contains (A2).
    c = iv.DecisionCase("input-1", "f", {"text": "generated-input-1"}, ())
    assert iv.decision_content_digest(c, ()) is not None


def test_q1h_review_a2_ids_renamed_to_a_substring_of_their_own_text_are_held(tmp_path):
    """Re-review A2: ids renamed to substrings of their own text dropped every value."""
    ev = evaluator(tmp_path)
    assert run(tmp_path, Scenario(CLEAN_WIN), ev=ev).outcome == "verified_gain"
    a2 = renamed_to(
        Scenario(CLEAN_WIN, prefix="a2-", content_prefix=""),
        lambda c: c.features["text"][len("generated-") :],  # "input-<n>", inside its own text
    )
    case = a2.population.cases[0]
    assert case.case_id in case.features["text"]
    doc = held(
        run(tmp_path, a2, ev=ev, result_revision=sha("a2")),
        "inconclusive",
        "decision_content_seen_in_scored_cohort",
    )
    assert "decision_content_unverifiable" not in doc["reasons"]


@pytest.mark.parametrize(
    "features,refs",
    [
        (lambda c: {"case_ref": c.case_id}, ("case_ref",)),  # only declared references
        (lambda c: {**c.features, "text": "   "}, ("case_ref",)),  # blank decision text
        (lambda c: dict(c.features), ("case_ref", "text")),  # the text declared a reference
    ],
    ids=["only_references", "blank_text", "text_declared_as_reference"],
)
def test_q1i_an_empty_content_digest_never_counts_as_fresh(tmp_path, features, refs):
    """An all-excluded case cannot be compared, so it is held even on a first claim."""
    s = respelled(Scenario(CLEAN_WIN, prefix="empty-"), features, refs)
    doc = held(run(tmp_path, s), "inconclusive", "decision_content_unverifiable")
    assert doc["answer_key_content_digest"] is None


def test_q1k_declared_reference_fields_are_part_of_the_audited_inputs():
    """Changing which fields are declared references changes the audited inputs digest."""
    s = Scenario(CLEAN_WIN)
    cases = s.population.cases
    labels = {c.case_id: s.population.truth(c.case_id) for c in cases}

    def pop(refs):
        return iv.SealedPopulation(
            cases, labels, label_set=(A, B), benchmark_digest=BENCHMARK, reference_fields=refs
        )

    assert pop(("case_ref",)).inputs_digest == s.population.inputs_digest
    assert pop(()).inputs_digest != s.population.inputs_digest
    assert pop(("case_ref", "text")).inputs_digest != s.population.inputs_digest
    for bad, error in (
        ("case_ref", "invalid_sealed_population"),  # a bare string, not a sequence of names
        (("case_ref", "case_ref"), "invalid_sealed_population"),
        ((7,), "invalid_sealed_population"),
        (("has spaces",), "feature_key_unverifiable"),
    ):
        with pytest.raises(iv.VerdictInputError, match="^" + error + "$"):
            pop(bad)


CYRILLIC_A = "а"  # looks like the Latin "a"


def test_d2_review_r4_an_undeclared_id_field_with_renamed_ids_is_held(tmp_path):
    """Round 4 D2: reference_fields=() with renamed ids came back verified_gain."""
    ev = evaluator(tmp_path)
    assert run(tmp_path, Scenario(CLEAN_WIN), ev=ev).outcome == "verified_gain"
    d2 = respelled(
        renamed_to(
            Scenario(CLEAN_WIN, prefix="d2-", content_prefix=""), lambda c: "r-" + c.case_id
        ),
        lambda c: dict(c.features),
        reference_fields=(),
    )
    assert d2.population.reference_fields == ()
    held(
        run(tmp_path, d2, ev=ev, result_revision=sha("d2")),
        "inconclusive",
        "undeclared_reference_field",
    )
    # Held even as the first claim in a fresh custody: the cohort cannot be compared.
    held(run(tmp_path / "first", d2), "inconclusive", "undeclared_reference_field")


@pytest.mark.parametrize(
    "value",
    [
        lambda c: "ref:" + c.case_id + ".json",  # the id inside a longer value
        lambda c: c.case_id.upper(),  # compared after casefold
        lambda c: "group " + c.family_id,  # the family id
        lambda c: c.case_id.replace("-", "－"),  # a fullwidth hyphen, compared after NFKC
    ],
    ids=["substring", "upper_case", "family_id", "nfkc_fullwidth"],
)
def test_d2b_any_undeclared_value_containing_the_own_id_holds(tmp_path, value):
    s = respelled(Scenario(CLEAN_WIN), lambda c: {**c.features, "source": value(c)})
    held(run(tmp_path, s), "inconclusive", "undeclared_reference_field")


def test_d2c_positive_controls_honest_schemas_are_not_held(tmp_path):
    ev = evaluator(tmp_path)
    honest = respelled(
        Scenario(CLEAN_WIN, prefix="honest-"),
        lambda c: {**c.features, "source": "ref:" + c.case_id},
        reference_fields=("case_ref", "source"),
    )
    assert run(tmp_path, honest, ev=ev).outcome == "verified_gain"
    # Another case's id, non-ASCII decision text and a different case are all just content.
    neighbour = respelled(
        Scenario(CLEAN_WIN, prefix="nb-"),
        lambda c: {
            **c.features,
            "previous": "nb-case-9999" if c.case_id == "nb-case-0000" else "nb-case-0000",
            "note": "naïve café " + c.features["text"],
        },
    )
    doc = run(tmp_path, neighbour, ev=ev).document
    assert doc["outcome"] == "verified_gain"
    assert "undeclared_reference_field" not in doc["reasons"]


@pytest.mark.parametrize(
    "features,refs,error",
    [
        # D3: the declared name differs from the feature key by case.
        (lambda c: dict(c.features), ("Case_Ref",), "reference_field_undeclared_in_schema"),
        # D3b: a Cyrillic look-alike key beside the declared ASCII name.
        (
            lambda c: {**c.features, "c" + CYRILLIC_A + "se_ref": c.case_id},
            ("case_ref",),
            "feature_key_unverifiable",
        ),
        # A non-ASCII declared name.
        (lambda c: dict(c.features), ("c" + CYRILLIC_A + "se_ref",), "feature_key_unverifiable"),
        # Keys that are not identifiers at all.
        (lambda c: {**c.features, " text ": "x"}, ("case_ref",), "feature_key_unverifiable"),
        # Two keys that collide after casefold.
        (lambda c: {**c.features, "CASE_REF": c.case_id}, ("case_ref",), "feature_key_collision"),
        # A declared name present in only some cases.
        (
            lambda c: {
                k: v for k, v in c.features.items() if c.case_id != "case-0000" or k != "case_ref"
            },
            ("case_ref",),
            "reference_field_undeclared_in_schema",
        ),
    ],
    ids=["d3_case", "d3b_cyrillic_key", "cyrillic_declared", "spaced_key", "collision", "partial"],
)
def test_d3_review_r4_unverifiable_schema_declarations_are_refused(features, refs, error):
    with pytest.raises(iv.VerdictInputError, match="^" + error + "$"):
        respelled(Scenario(CLEAN_WIN), features, reference_fields=refs)


def test_d3c_keys_colliding_across_cases_are_refused():
    s = Scenario(CLEAN_WIN)

    def mixed(c):
        key = "Text" if c.case_id.endswith("1") else "text"
        return {"case_ref": c.case_id, key: c.features["text"]}

    with pytest.raises(iv.VerdictInputError, match="^feature_key_collision$"):
        respelled(s, mixed)


def test_e1_review_r4_renamed_ids_and_an_undeclared_reference_cannot_escape_both(
    tmp_path, registered_floor
):
    """Round 4 E1: with the answer key revoked, this combination came back verified_gain."""
    ev = evaluator(tmp_path)
    hit = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    ev.revoke_scope(
        "answer_key_digest",
        hit.document["answer_key_digest"],
        reason="answer_key_defect",
        issued_by=EVALUATOR,
    )
    e1 = respelled(
        renamed_to(
            Scenario(CLEAN_WIN, prefix="e1-", content_prefix=""), lambda c: "r-" + c.case_id
        ),
        lambda c: {**c.features, "source": "ref:" + c.case_id},
    )
    v = run(tmp_path, e1, ev=ev, result_revision=sha("e1"))
    assert v.outcome != "verified_gain"
    doc = held(v, "inconclusive", "undeclared_reference_field")
    assert not any(doc["effects"].values())
    assert ev.authorize(doc, effect="improvement_alert")["authorized"] is False


def test_v23_an_answer_key_content_digest_may_be_revoked_pre_emptively(tmp_path):
    """Round 4 LOW: a revoker can compute this digest from a defective key itself."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    content = s.population.answer_key_content_digest
    assert content is not None
    r = ev.revoke_scope(
        "answer_key_content_digest", content, reason="answer_key_defect", issued_by=EVALUATOR
    )
    assert r.record["matched_verdicts"] == 0
    replays = s.replays()
    v = run(tmp_path, s, ev=ev, replays=replays)
    assert v.outcome == "stale" and replays["candidate"].calls == []
    assert v.document["reasons"][-1] == "verdict_revoked_by_scope_answer_key_content_digest"
    # The other scopes still refuse a value no verdict here carries.
    with pytest.raises(iv.VerdictInputError, match="^revocation_scope_unknown$"):
        ev.revoke_scope(
            "answer_key_digest",
            s.population.answer_key_digest,
            reason="answer_key_defect",
            issued_by=EVALUATOR,
        )
    with pytest.raises(iv.VerdictInputError, match="^invalid_revocation_scope_value$"):
        ev.revoke_scope(
            "answer_key_content_digest", "not-a-digest", reason="scorer_defect", issued_by=EVALUATOR
        )


def test_v24_documented_limit_relabelling_the_partition_falls_to_the_cohort_gate(tmp_path):
    """Round 4 LOW: swapping label names escapes the content match; the cohort gate holds it."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    hit = run(tmp_path, s, ev=ev)
    ev.revoke_scope(
        "answer_key_digest",
        hit.document["answer_key_digest"],
        reason="answer_key_defect",
        issued_by=EVALUATOR,
    )
    relabelled = Scenario(CLEAN_WIN, prefix="rl-", content_prefix="")
    swap = {A: B, B: A}
    cases = relabelled.population.cases
    labels = {c.case_id: swap[relabelled.population.truth(c.case_id)] for c in cases}
    relabelled.population = iv.SealedPopulation(
        cases, labels, label_set=(A, B), benchmark_digest=BENCHMARK, reference_fields=("case_ref",)
    )
    relabelled.preds = {
        arm: {
            k: ({**p, "label": swap[p["label"]]} if p and "label" in p else p)
            for k, p in preds.items()
        }
        for arm, preds in relabelled.preds.items()
    }
    assert (
        relabelled.population.answer_key_content_digest != hit.document["answer_key_content_digest"]
    )
    doc = run(tmp_path, relabelled, ev=ev, result_revision=sha("relabel")).document
    assert doc["outcome"] != "stale"  # the content match does not follow a relabelling
    assert "decision_content_seen_in_scored_cohort" in doc["reasons"]  # the backstop holds
    text = " ".join((iv.IndependentEvaluator.revoke_scope.__doc__ or "").split())
    assert "relabelling the same partition" in text and "Trade-off" in text


def test_q1j_documented_residual_a1_a_fresh_non_id_field_is_new_content(tmp_path):
    """Residual A1, pinned so the docstring stays true: a varying non-id field is not caught."""
    import hashlib

    ev = evaluator(tmp_path)
    assert run(tmp_path, Scenario(CLEAN_WIN), ev=ev).outcome == "verified_gain"
    salted = respelled(
        Scenario(CLEAN_WIN, prefix="salted-", content_prefix="", family_prefix="s-"),
        lambda c: {
            **c.features,
            "salt": hashlib.sha256(c.case_id.encode()).hexdigest()[:12],  # not the id itself
        },
    )
    doc = run(tmp_path, salted, ev=ev, result_revision=sha("salted")).document
    assert "decision_content_seen_in_scored_cohort" not in doc["reasons"]
    assert "undeclared_reference_field" not in doc["reasons"]
    text = " ".join((iv.decision_content_digest.__doc__ or "").split())
    assert "Residual, NOT closed here" in text
    assert all(tag in text for tag in ("A1", "A2", "A3", "D2/E1", "D3/D3b", "D5"))


def test_q1f_a_register_marker_without_content_is_corrupt(tmp_path):
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    marker = tmp_path / "evaluator-custody" / "cohorts" / (v.verdict_key + ".json")
    seen = json.loads(marker.read_bytes())
    assert len(seen["contents"]) == 200
    del seen["contents"]
    marker.write_text(json.dumps(seen))
    with pytest.raises(iv.CustodyError, match="cohort_register_corrupt"):
        run(tmp_path, Scenario(CLEAN_WIN, prefix="next-"), ev=ev)


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


TEST_FLOOR = iv.PolicyFloor(
    floor_id="generated-test-floor-r1",
    max_alpha_bps=500,
    min_improvement_bps=200,
    min_families=2,
    min_coverage_floor_bps=7000,
    max_missing=5,
)


@pytest.fixture
def registered_floor(monkeypatch):
    """Stands in for a future owner D11 decision. Test-only: the shipped registry is empty."""
    monkeypatch.setattr(iv, "REGISTERED_POLICY_FLOOR", TEST_FLOOR)
    return TEST_FLOOR


def test_v1_revoked_verified_gain_reads_as_stale_and_every_consumer_check_refuses(
    tmp_path, registered_floor
):
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    replays = s.replays()
    v = run(tmp_path, s, ev=ev, replays=replays)
    assert v.outcome == "verified_gain"
    # With a floor registered, the gain authorizes an alert until it is revoked.
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


def test_v12_review_m2_no_effect_on_a_gain_while_the_policy_floor_is_unset(tmp_path):
    """Re-review M2: authorize(improvement_alert) returned True with D11 undecided."""
    assert iv.REGISTERED_POLICY_FLOOR is None  # the shipped registry is empty
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    assert v.outcome == "verified_gain" and v.document["alert_gates"]["cleared"] is True
    for effect in ("improvement_alert", "release_request", "handoff_request"):
        assert ev.authorize(v.document, effect=effect) == {
            "authorized": False,
            "effect": effect,
            "reason": "policy_floor_unset",
            "state": "verified_gain",
        }
    # A non-win is not a gain: its refusal stays effect_not_eligible.
    b4 = run(tmp_path, Scenario(B4, prefix="b4-"), ev=ev)
    assert ev.authorize(b4.document, effect="improvement_alert")["reason"] == "effect_not_eligible"


def test_v13_no_caller_supplied_policy_lifts_the_floor_hold(tmp_path):
    """The floor is the owner's, read from the module: nothing a producer sends can lift it."""
    import inspect

    assert list(inspect.signature(iv.IndependentEvaluator.authorize).parameters) == [
        "self",
        "document",
        "effect",
    ]
    for name in ("evaluate", "__init__"):
        params = inspect.signature(getattr(iv.IndependentEvaluator, name)).parameters
        assert not any("floor" in p for p in params)
    ev = evaluator(tmp_path)
    # The strictest policy a producer could preregister, plus terms naming a "floor".
    t = {**terms_for(), "policy_floor": "satisfied", "registered_policy_floor": "d11-approved"}
    strict = policy(
        alpha_bps=1,
        minimum_improvement_bps=2000,
        min_families=200,
        coverage_floor_bps=10000,
        max_coverage_loss_bps=0,
        max_missing=0,
        operating_point_terms=t,
        operating_point_digest=digest(t),
    )
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev, pol=strict)
    assert v.outcome == "verified_gain"
    assert ev.authorize(v.document, effect="improvement_alert")["reason"] == "policy_floor_unset"
    # A document rewritten to claim the floor is met is not the stored verdict.
    forged = {
        **v.document,
        "alert_gates": {"cleared": True, "policy_floor": "satisfied", "policy_floor_hold": None},
        "effects": {**v.document["effects"], "improvement_alert_eligible": True},
    }
    assert ev.authorize(forged, effect="improvement_alert")["reason"] == (
        "verdict_document_mismatch"
    )
    # Re-reading the stored verdict does not lift it either.
    again = run(tmp_path, Scenario(CLEAN_WIN), ev=ev, pol=strict)
    assert again.replayed
    assert ev.authorize(again.document, effect="improvement_alert")["reason"] == (
        "policy_floor_unset"
    )


def test_v14_the_d11_seam_applies_a_registered_floor_and_refuses_a_laxer_policy(
    tmp_path, monkeypatch
):
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    held_before = run(tmp_path, s, ev=ev)  # issued while no floor existed
    monkeypatch.setattr(iv, "REGISTERED_POLICY_FLOOR", TEST_FLOOR)
    # A floor registered later applies to the earlier verdict without re-scoring it.
    out = ev.authorize(held_before.document, effect="improvement_alert")
    assert out["authorized"] is True and out["reason"] == "verdict_current_and_eligible"
    assert ev.authorize(held_before.document, effect="release_request")["reason"] == (
        "effect_not_eligible"
    )
    fresh = run(tmp_path / "fresh", s)
    assert fresh.document["alert_gates"]["policy_floor"] == "satisfied"
    assert fresh.document["effects"]["improvement_alert_eligible"] is True
    lax = policy(alpha_bps=900)
    lax_v = run(tmp_path / "lax", s, pol=lax)
    assert lax_v.outcome == "verified_gain"
    assert lax_v.document["alert_gates"]["policy_floor_hold"] == (
        "policy_floor_below_registered_floor"
    )
    assert evaluator(tmp_path / "lax").authorize(lax_v.document, effect="improvement_alert")[
        "reason"
    ] == ("policy_below_registered_floor")
    assert TEST_FLOOR.shortfalls(lax.as_json()) == ["alpha_bps"]
    assert TEST_FLOOR.shortfalls({}) == [
        "alpha_bps",
        "minimum_improvement_bps",
        "min_families",
        "coverage_floor_bps",
        "max_missing",
    ]
    for name, value in (
        ("minimum_improvement_bps", 199),
        ("min_families", 1),
        ("coverage_floor_bps", 6999),
        ("max_missing", 6),
        ("alpha_bps", 501),
    ):
        assert TEST_FLOOR.shortfalls({**policy().as_json(), name: value}) == [name]
    assert TEST_FLOOR.shortfalls(policy().as_json()) == []
    # Each bound is inclusive at the floor's own value.
    at_floor = {
        "alpha_bps": 500,
        "minimum_improvement_bps": 200,
        "min_families": 2,
        "coverage_floor_bps": 7000,
        "max_missing": 5,
    }
    assert TEST_FLOOR.shortfalls(at_floor) == []
    assert TEST_FLOOR.shortfalls({**at_floor, "max_missing": "5"}) == ["max_missing"]


def test_v15_review_r5_a_scorer_defect_revokes_every_verdict_of_that_scorer_version(
    tmp_path, registered_floor
):
    """Re-review L2/R5: after a per-key scorer_defect revocation, a sibling still authorized."""
    ev = evaluator(tmp_path)
    a = run(tmp_path, Scenario(CLEAN_WIN, prefix="a-"), ev=ev)
    b = run(tmp_path, Scenario(CLEAN_WIN, prefix="b-"), ev=ev)
    ev.revoke_verdict(a.verdict_key, reason="scorer_defect", issued_by=EVALUATOR)
    assert ev.authorize(b.document, effect="improvement_alert")["authorized"] is True  # R5
    r = ev.revoke_scope(
        "scorer_version", iv.SCORER_VERSION, reason="scorer_defect", issued_by=EVALUATOR
    )
    assert not r.replayed and r.record["schema_version"] == iv.SCOPE_REVOCATION
    assert r.record["issued_by_authenticated"] is False
    assert ev.authorize(b.document, effect="improvement_alert") == {
        "authorized": False,
        "effect": "improvement_alert",
        "reason": "verdict_revoked_by_scope",
        "state": "revoked",
    }
    view = ev.read_verdict(b.verdict_key)
    assert view["state"] == "revoked" and view["outcome"] == "stale"
    assert view["reasons"] == [
        "revocation_reason_scorer_defect",
        "verdict_revoked_by_scope_scorer_version",
    ]
    assert view["revoked_scope"] == {"scope": "scorer_version", "value": iv.SCORER_VERSION}
    assert not cp._verdict_ok(view) and not any(view["effects"].values())
    # The revoked scorer never scores another claim, here or after a restart.
    s = Scenario(CLEAN_WIN, prefix="c-")
    replays = s.replays()
    c = run(tmp_path, s, ev=evaluator(tmp_path), replays=replays)
    assert c.outcome == "stale" and replays["candidate"].calls == []
    assert list((tmp_path / "evaluator-custody" / "verdicts").glob("*.json")) != []
    again = ev.revoke_scope(
        "scorer_version", iv.SCORER_VERSION, reason="claim_corrected", issued_by="other-1"
    )
    assert again.replayed and again.record == r.record  # idempotent, first record final


@pytest.mark.parametrize("scope", ["answer_key_digest", "audit_receipt_digest"])
def test_v16_answer_key_and_audit_receipt_scopes_revoke_matching_verdicts_only(
    tmp_path, registered_floor, scope
):
    ev = evaluator(tmp_path)
    hit_s = Scenario(CLEAN_WIN, prefix="hit-")
    miss_s = Scenario(CLEAN_WIN, prefix="miss-")
    hit = run(tmp_path, hit_s, ev=ev)
    miss = run(tmp_path, miss_s, ev=ev)
    value = hit.document[scope]
    assert value != miss.document[scope]
    ev.revoke_scope(scope, value, reason="answer_key_defect", issued_by=EVALUATOR)
    assert ev.authorize(hit.document, effect="improvement_alert")["reason"] == (
        "verdict_revoked_by_scope"
    )
    assert ev.authorize(miss.document, effect="improvement_alert")["authorized"] is True
    assert ev.read_verdict(miss.verdict_key) == miss.document
    # The same population re-submitted under a new claim is refused before scoring.
    replays = hit_s.replays()
    again = run(tmp_path, hit_s, ev=ev, replays=replays, result_revision=sha("retry"))
    assert again.outcome == "stale" and replays["candidate"].calls == []
    assert again.document["reasons"][-1] == "verdict_revoked_by_scope_" + scope


def test_v17_scope_revocations_are_sealed_validated_and_refuse_unknown_values(tmp_path):
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    w = run(tmp_path, Scenario(CLEAN_WIN, prefix="w-"), ev=ev)
    for scope, value, reason in (
        ("tenant", "generated-tenant-1", "invalid_revocation_scope"),
        ("scorer_version", "has spaces", "invalid_revocation_scope_value"),
        ("answer_key_digest", "not-a-digest", "invalid_revocation_scope_value"),
        ("audit_receipt_digest", None, "invalid_revocation_scope_value"),
        # Re-review LOW: a well-formed value this custody never scored matches nothing.
        ("scorer_version", "a-future-scorer.v9", "revocation_scope_unknown"),
        ("answer_key_digest", sha("never-scored-key"), "revocation_scope_unknown"),
        ("audit_receipt_digest", sha("never-scored-receipt"), "revocation_scope_unknown"),
    ):
        with pytest.raises(iv.VerdictInputError, match="^" + reason + "$"):
            ev.revoke_scope(scope, value, reason="scorer_defect", issued_by=EVALUATOR)
    with pytest.raises(iv.VerdictInputError, match="invalid_revocation_reason"):
        ev.revoke_scope("scorer_version", "x.v9", reason="free text", issued_by=EVALUATOR)
    folder = tmp_path / "evaluator-custody" / "revoked_scopes"
    assert list(folder.glob("*.json")) == []  # nothing stored for any refusal
    # A known value: the record says how many verdicts it matched; others are untouched.
    r = ev.revoke_scope(
        "answer_key_digest",
        w.document["answer_key_digest"],
        reason="answer_key_defect",
        issued_by=EVALUATOR,
    )
    assert r.record["matched_verdicts"] == 1
    assert r.record["answer_key_content_digests"] == [w.document["answer_key_content_digest"]]
    s = ev.revoke_scope(
        "scorer_version", iv.SCORER_VERSION, reason="scorer_defect", issued_by=EVALUATOR
    )
    assert s.record["matched_verdicts"] == 2 and s.record["answer_key_content_digests"] == []
    (
        tmp_path
        / "evaluator-custody"
        / "revoked_scopes"
        / ev._scope_file("scorer_version", iv.SCORER_VERSION)
    ).unlink()
    assert ev.read_verdict(v.verdict_key) == v.document
    (path,) = list(folder.glob("*.json"))
    good = path.read_bytes()
    # Each seal layer on its own: a resealed digest without the key (only the HMAC catches it)
    # and a keyed record with a stale digest (only the digest catches it).
    only_hmac = json.loads(good)
    only_hmac["record"]["reason"] = "claim_corrected"
    body = {k: x for k, x in only_hmac["record"].items() if k != "revocation_digest"}
    only_hmac["record"]["revocation_digest"] = digest(body)
    only_digest = json.loads(good)
    only_digest["record"]["reason"] = "claim_corrected"
    only_digest["local_seal"] = ev._commit({"record": only_digest["record"]})
    for forged in (only_hmac, only_digest):
        path.write_text(json.dumps(forged))
        with pytest.raises(iv.CustodyError, match="stored_scope_revocation_tampered"):
            ev.read_verdict(v.verdict_key)
    stored = json.loads(good)
    # Pointed at the live scorer version and resealed without the key: refused.
    stored["record"]["scope_value"] = iv.SCORER_VERSION
    body = {k: x for k, x in stored["record"].items() if k != "revocation_digest"}
    stored["record"]["revocation_digest"] = digest(body)
    path.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_scope_revocation_tampered"):
        ev.read_verdict(v.verdict_key)
    # Keyed and sealed, but filed under another scope's name: refused.
    stored["local_seal"] = ev._commit({"record": stored["record"]})
    path.write_text(json.dumps(stored))
    with pytest.raises(iv.CustodyError, match="stored_scope_revocation_tampered"):
        ev.authorize(v.document, effect="improvement_alert")
    path.write_text("[]")
    with pytest.raises(iv.CustodyError, match="stored_scope_revocation_corrupt"):
        ev.read_verdict(v.verdict_key)
    path.write_bytes(good)
    assert ev.read_verdict(v.verdict_key) == v.document


def test_v20_review_c2_renamed_ids_do_not_escape_an_answer_key_revocation(
    tmp_path, registered_floor
):
    """Re-review C2: renaming every id changed the answer-key digest and escaped revocation."""
    ev = evaluator(tmp_path)
    original = Scenario(CLEAN_WIN)
    hit = run(tmp_path, original, ev=ev)
    renamed = renamed_to(
        Scenario(CLEAN_WIN, prefix="c2-", content_prefix=""), lambda c: "r-" + c.case_id
    )
    earlier = run(tmp_path / "other", renamed)  # issued in another custody before revocation
    assert earlier.document["answer_key_digest"] != hit.document["answer_key_digest"]
    assert (
        earlier.document["answer_key_content_digest"] == hit.document["answer_key_content_digest"]
    )
    ev.revoke_scope(
        "answer_key_digest",
        hit.document["answer_key_digest"],
        reason="answer_key_defect",
        issued_by=EVALUATOR,
    )
    replays = renamed.replays()
    again = run(tmp_path, renamed, ev=ev, replays=replays, result_revision=sha("c2"), receipt=None)
    assert again.outcome == "stale" and replays["candidate"].calls == []
    assert again.document["reasons"][-1] == "verdict_revoked_by_scope_answer_key_digest"
    # Different labels on the same content are a different key and are not caught.
    flipped = Scenario(CLEAN_WIN, prefix="flip-", content_prefix="")
    flipped = renamed_to(flipped, lambda c: "f-" + c.case_id)
    labels = {
        c.case_id: (B if flipped.population.truth(c.case_id) == A else A)
        for c in flipped.population.cases
    }
    assert (
        iv.SealedPopulation(
            flipped.population.cases,
            labels,
            label_set=(A, B),
            benchmark_digest=BENCHMARK,
            reference_fields=("case_ref",),
        ).answer_key_content_digest
        != hit.document["answer_key_content_digest"]
    )


def test_v21_documented_limit_c1_a_reminted_receipt_escapes_a_receipt_revocation(tmp_path):
    """Re-review C1/C4: the self-sealed OPE-526 receipt can be re-minted. Pinned, not closed."""
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    v = run(tmp_path, s, ev=ev)
    ev.revoke_scope(
        "audit_receipt_digest",
        v.document["audit_receipt_digest"],
        reason="benchmark_audit_superseded",
        issued_by=EVALUATOR,
    )
    reminted = receipt_for(s.population, reasons=["generated-reminted"])
    assert reminted["receipt_digest"] != v.document["audit_receipt_digest"]
    doc = run(tmp_path, s, ev=ev, receipt=reminted, result_revision=sha("c1")).document
    assert doc["outcome"] != "stale"  # the receipt revocation does not follow a re-mint
    # The cohort gate still holds the re-minted claim (the A2 fix closes the C4 combination).
    assert "decision_content_seen_in_scored_cohort" in doc["reasons"]
    text = " ".join((iv.__doc__ or "").split())
    assert "Residual, NOT closed" in text and "C1 (nothing here can tell" in text
    assert "self-sealed" in text and "while reference fields are declared honestly" in text


def test_v22_matched_verdicts_counts_keys_not_generations(tmp_path, monkeypatch):
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    first = run(tmp_path, s, ev=ev)
    now = int(iv.time.time())
    monkeypatch.setattr(iv.time, "time", lambda: now + 8 * 86_400)
    again = run(tmp_path, s, ev=ev)  # re-issued: one key, two generations
    assert not again.replayed and again.verdict_key == first.verdict_key
    r = ev.revoke_scope(
        "scorer_version", iv.SCORER_VERSION, reason="scorer_defect", issued_by=EVALUATOR
    )
    assert r.record["matched_verdicts"] == 1
    # An archived generation alone is enough to make a value known.
    (tmp_path / "evaluator-custody" / "verdicts" / (first.verdict_key + ".json")).unlink()
    assert (
        ev.revoke_scope(
            "answer_key_digest",
            first.document["answer_key_digest"],
            reason="answer_key_defect",
            issued_by=EVALUATOR,
        ).record["matched_verdicts"]
        == 1
    )


def test_v18_one_correction_notice_per_scope_revoked_verdict_that_fired(tmp_path):
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    with pytest.raises(iv.VerdictInputError, match="verdict_not_revoked"):
        ev.correction_notice(v.verdict_key, watch_fired=True)
    scope = ev.revoke_scope(
        "scorer_version", iv.SCORER_VERSION, reason="scorer_defect", issued_by=EVALUATOR
    )
    n = ev.correction_notice(v.verdict_key, watch_fired=True)
    assert n is not None and not n.replayed
    assert n.document["revocation_digest"] == scope.record["revocation_digest"]
    assert n.document["revoked_scope"] == {"scope": "scorer_version", "value": iv.SCORER_VERSION}
    # A later key revocation of the same verdict neither unbinds nor duplicates the notice.
    later = ev.revoke_verdict(
        v.verdict_key, reason="scorer_defect", issued_by=EVALUATOR, watch_fired=True
    )
    assert later.notice is not None and later.notice.replayed
    assert later.notice.document == n.document
    restarted = evaluator(tmp_path).correction_notice(v.verdict_key, watch_fired=True)
    assert restarted is not None and restarted.document == n.document
    assert len(list((tmp_path / "evaluator-custody" / "notices").glob("*.json"))) == 1
    # The other order: a notice bound to a key revocation survives a later scope revocation.
    ev2 = evaluator(tmp_path / "second")
    w = run(tmp_path / "second", Scenario(CLEAN_WIN), ev=ev2)
    first = ev2.revoke_verdict(
        w.verdict_key, reason="claim_corrected", issued_by=EVALUATOR, watch_fired=True
    )
    assert first.notice is not None
    assert first.notice.document["revocation_digest"] == first.record["revocation_digest"]
    ev2.revoke_scope(
        "answer_key_digest",
        w.document["answer_key_digest"],
        reason="answer_key_defect",
        issued_by=EVALUATOR,
    )
    kept = ev2.correction_notice(w.verdict_key, watch_fired=True)
    assert kept is not None and kept.replayed and kept.document == first.notice.document


def test_v19_documented_limits_deleting_a_revocation_restores_authority(tmp_path):
    """INFO R2/R6/R7, pinned so the documentation stays true: these are known limits."""
    ev = evaluator(tmp_path)
    v = run(tmp_path, Scenario(CLEAN_WIN), ev=ev)
    ev.revoke_verdict(v.verdict_key, reason="claim_corrected", issued_by="anyone-at-all")
    scope = ev.revoke_scope(
        "scorer_version", iv.SCORER_VERSION, reason="scorer_defect", issued_by="anyone-at-all"
    )
    assert scope.record["issued_by"] == "anyone-at-all"  # R6: a label, never authenticated
    assert ev.read_verdict(v.verdict_key)["state"] == "revoked"
    root = tmp_path / "evaluator-custody"
    (root / "revocations" / (v.verdict_key + ".json")).unlink()
    for path in (root / "revoked_scopes").glob("*.json"):
        path.unlink()
    # R2: whoever can delete files in custody restores the original verdict's authority.
    assert ev.read_verdict(v.verdict_key) == v.document
    doc = " ".join((iv.__doc__ or "").split())
    assert "can also delete a revocation, and deleting it restores" in doc
    assert "issued_by" in doc and "unauthenticated" in doc  # R6, R7


def test_v10_consumer_check_refuses_forged_unknown_and_ineligible_documents(
    tmp_path, registered_floor
):
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


# ----------------------------------------------------------------- P: managed/custom route
# Generated sentinel job records and provider doubles only; no real provider, account or job.

PARENT = "generated-provider-base-r20260101"
CANDIDATE_IDS = {
    "managed_provider": "ft:generated-provider-base:sentinel-org::cand0001",
    "custom_trainer": "generated-trainer-artifact-0001",
}
KINDS = {"managed_provider": "managed_fit", "custom_trainer": "custom_trainer_fit"}
DONE = {"managed_provider": "succeeded", "custom_trainer": "completed"}
TRAINER_DIGEST = sha("trainer-reported-artifact-bytes")


def provider_job(route, /, **over):
    values = {
        "route": route,
        "job_id": "generated-provider-job-0001",
        "client_token": "generated-client-token-0001",
        "intent_sha256": sha("provider-fit-intent"),
        "state": DONE[route],
        "parent_revision": PARENT,
        "candidate_id": CANDIDATE_IDS[route],
        "artifact_digest": TRAINER_DIGEST if route == "custom_trainer" else None,
        "reporter_version": "generated-trainer-v1" if route == "custom_trainer" else None,
    }
    values.update(over)
    return iv.ProviderJobRecord(**values)


def readback_for(route, /, **over):
    out = {
        "route": route,
        "served_model_id": CANDIDATE_IDS[route],
        "identity_kind": "immutable_model_id",
        "parent_revision": PARENT,
    }
    if route == "custom_trainer":
        out["artifact_digest"] = TRAINER_DIGEST
    out.update(over)
    return {k: v for k, v in out.items() if v is not None}


class ProviderDouble:
    """A generated provider: serves the scenario's candidate predictions under one model id."""

    def __init__(self, preds, *, served_by=None, fail=False):
        self.preds, self.served_by, self.fail = preds, served_by, fail
        self.calls: list[tuple[str, int]] = []

    def predict(self, model_id, inputs):
        if self.fail:
            raise ConnectionError("generated_provider_unavailable")
        self.calls.append((model_id, len(inputs)))
        return [self.preds[f["case_ref"]] for f in inputs], self.served_by or model_id


def provider_run(tmp_path, s, route, *, job="default", evidence=None, readback=None, **over):
    """Run the provider route; any piece can be replaced to make an adverse case."""
    job = provider_job(route) if job == "default" else job
    evidence = evidence or iv.ProviderEvidence(job, PARENT, CANDIDATE_IDS[route])
    double = over.pop("double", None) or ProviderDouble(s.preds["candidate"])
    rb = readback_for(route) if readback is None else readback
    replay = iv.ProviderReplay(
        route=route,
        parent_model_id=PARENT,
        candidate_id=CANDIDATE_IDS[route],
        readback_fn=rb if callable(rb) else (lambda: rb),
        predict_fn=double.predict,
    )
    values = {
        "candidate_kind": KINDS[route],
        "candidate_digest": iv.provider_candidate_digest(route, PARENT, CANDIDATE_IDS[route]),
        "job_manifest_digest": job.digest() if job is not None else None,
        "manifest": None,
        "replays": s.replays(candidate=replay),
        "provider": evidence,
        **over,
    }
    return run(tmp_path, s, **values), double


@pytest.mark.parametrize("route", ["managed_provider", "custom_trainer"])
def test_p1_provider_route_win_is_provider_reported_never_verified(tmp_path, route):
    s = Scenario([("WR", 80), ("RR", 50)])
    v, double = provider_run(tmp_path, s, route)
    doc = v.document
    assert v.outcome == "verified_gain" and doc["verdict"] == "reproduced"
    job = doc["statements"]["job_produced_candidate"]
    reporter = {"managed_provider": "provider_reported", "custom_trainer": "trainer_reported"}
    assert job["status"] == reporter[route]
    assert job["parent_revision"] == PARENT and job["candidate_id"] == CANDIDATE_IDS[route]
    assert job["weights"] == "provider_attested_opaque"
    assert job["tensor_evidence"] == "not_available"
    assert job["evidence"] == "unauthenticated_provider_statement"
    assert "manifest_states_training_steps" not in job  # no invented training evidence
    served = doc["statements"]["candidate_served_evaluation"]
    assert served["status"] == "readback_consistent"
    assert served["serving_evidence"] == "provider_reported_readback"
    assert served["drift_checks"] == {
        # Statements matched against statements: never "checked" or "verified".
        "artifact_digest": "matches_job_record" if route == "custom_trainer" else "unavailable",
        "identity_kind": "provider_self_claim",
        "parent_revision": "matches_supplied_expectation",
        "served_model_id": "matches_supplied_expectation",
    }
    # The evaluator holds no fit-intent record of its own here, and says so.
    assert job["job_intent_binding"] == "unchecked_no_expectation_supplied"
    statements = json.dumps([job, served])
    assert "verified" not in statements and "sha256" not in json.dumps(job["weights"])
    # Immutability is the provider's claim, so no label asserts it.
    assert job["reason"] == "job_record_names_supplied_parent_and_candidate"
    assert "immutable" not in statements
    assert doc["candidate_kind"] == KINDS[route]
    assert doc["alert_gates"]["cleared"] is True  # same gates as open weight; D11 floor unset
    assert doc["effects"]["improvement_alert_eligible"] is False
    assert {m for m, _ in double.calls} == {CANDIDATE_IDS[route]}
    assert [n for _, n in double.calls] == [64, 64, 2]


@pytest.mark.parametrize("route", ["managed_provider", "custom_trainer"])
@pytest.mark.parametrize(
    "plan,pol_over,expected",
    [
        (CLEAN_WIN, {}, "verified_gain"),
        (B4, {}, "inconclusive"),
        ([("RW", 120), ("RR", 80)], {}, "regression"),
        ([("WR", 120), ("RR", 77), ("RR", 3), ("MW", 5)], {"max_missing": 5}, "regression"),
        ([("RR", 170), ("DR", 29), ("DW", 1)], {}, "regression"),  # slice veto on DW
        ([("WR", 110), ("WM", 10), ("RR", 80)], {}, "inconclusive"),
    ],
)
def test_p2_scoring_and_gates_are_identical_to_the_open_weight_path(
    tmp_path, route, plan, pol_over, expected
):
    total = sum(count for _, count in plan)

    def last_eight(index, codes):
        return (CRITICAL,) if index >= total - 8 else ()

    s = Scenario(plan, slice_of=last_eight)
    pol = policy(critical_slices=(CRITICAL,), **pol_over)
    open_weight = run(tmp_path / "open", s, pol=pol).document
    provider, _ = provider_run(tmp_path / "provider", s, route, pol=pol)
    doc = provider.document
    assert open_weight["outcome"] == expected
    for field_name in ("outcome", "verdict", "reasons", "comparisons", "effects"):
        assert doc[field_name] == open_weight[field_name], field_name
    assert (
        doc["statements"]["paired_gain_cleared_gates"]
        == (open_weight["statements"]["paired_gain_cleared_gates"])
    )


@pytest.mark.parametrize("route", ["managed_provider", "custom_trainer"])
@pytest.mark.parametrize(
    "case,reason",
    [
        ("job_missing", "job_binding_missing"),
        ("claim_names_no_job", "job_binding_missing"),
        ("evidence_missing", "job_binding_missing"),
        ("job_running", "job_not_completed"),
        ("job_other_route", "job_route_mismatch"),
        ("claim_names_other_job", "designated_job_mismatch"),
        ("parent_mismatch", "parent_mismatch"),
        ("parent_alias", "parent_mutable_alias"),
        ("candidate_alias", "candidate_id_mutable_alias"),
        ("candidate_bare_alias", "candidate_id_mutable_alias"),
        ("job_other_candidate", "job_candidate_is_not_the_frozen_candidate"),
        ("claim_other_candidate", "job_artifact_is_not_the_candidate"),
        ("malformed_record", "provider_job_record_malformed"),
        ("fit_manifest_given", "fit_manifest_on_a_provider_route"),
    ],
)
def test_p3_job_binding_refusals_hold_before_any_inference(tmp_path, route, case, reason):
    s = Scenario(CLEAN_WIN)
    other = {"managed_provider": "custom_trainer", "custom_trainer": "managed_provider"}[route]
    alias = CANDIDATE_IDS[route].rsplit(":", 1)[0] + ":latest"
    jobs = {
        "job_running": provider_job(route, state="running"),
        "job_other_route": provider_job(route, route=other, state=DONE[other]),
        "parent_mismatch": provider_job(route, parent_revision="generated-provider-base-r2"),
        "job_other_candidate": provider_job(route, candidate_id="generated-other-candidate-9"),
        "malformed_record": provider_job(route, intent_sha256="not-a-digest"),
    }
    over: dict = {}
    if case == "job_missing":
        over = {"job": None, "evidence": iv.ProviderEvidence(None, PARENT, CANDIDATE_IDS[route])}
    elif case == "claim_names_no_job":
        over = {"job_manifest_digest": None}
    elif case == "evidence_missing":
        over = {"provider": None}
    elif case == "claim_names_other_job":
        over = {"job_manifest_digest": provider_job(route, job_id="generated-job-2").digest()}
    elif case == "parent_alias":
        job = provider_job(route, parent_revision="generated-provider-base:latest")
        over = {
            "job": job,
            "evidence": iv.ProviderEvidence(
                job, "generated-provider-base:latest", CANDIDATE_IDS[route]
            ),
        }
    elif case in ("candidate_alias", "candidate_bare_alias"):
        cid = alias if case == "candidate_alias" else "latest"
        job = provider_job(route, candidate_id=cid)
        over = {
            "job": job,
            "evidence": iv.ProviderEvidence(job, PARENT, cid),
            "candidate_digest": iv.provider_candidate_digest(route, PARENT, cid),
        }
    elif case == "claim_other_candidate":
        over = {"candidate_digest": iv.provider_candidate_digest(route, PARENT, "generated-x-1")}
    elif case == "fit_manifest_given":
        over = {"manifest": MANIFEST}
    elif case in jobs:
        over = {"job": jobs[case]}
    v, double = provider_run(tmp_path, s, route, **over)
    doc = held(v, "integrity_failure", "job_" + reason)
    assert doc["statements"]["job_produced_candidate"] == {"status": "failed", "reason": reason}
    assert double.calls == [] and doc["comparisons"] == {}
    if case == "claim_other_candidate":
        # Each statement is reported on its own: the readback also refuses the claimed digest.
        assert doc["statements"]["candidate_served_evaluation"] == {
            "status": "failed",
            "reason": "candidate_served_other_artifact",
        }


@pytest.mark.parametrize("route", ["managed_provider", "custom_trainer"])
@pytest.mark.parametrize(
    "readback,reason",
    [
        ({"identity_kind": "mutable_alias"}, "served_identity_mutable_alias"),
        ({"served_model_id": "generated-provider-base:latest"}, "served_identity_mutable_alias"),
        ({"identity_kind": None}, "served_identity_unverifiable"),
        ({"identity_kind": "endpoint"}, "served_identity_unverifiable"),
        ({"served_model_id": None}, "served_identity_unverifiable"),
        (
            {"served_model_id": "generated-other-revision-2"},
            "served_candidate_served_other_revision",
        ),
        ({"parent_revision": "generated-provider-base-r2"}, "served_parent_mismatch"),
        ({"route": "open_weight"}, "served_route_mismatch"),
    ],
)
def test_p4_serving_readback_must_name_the_evaluated_immutable_revision(
    tmp_path, route, readback, reason
):
    s = Scenario(CLEAN_WIN)
    v, double = provider_run(tmp_path, s, route, readback=readback_for(route, **readback))
    doc = held(v, "integrity_failure", reason)
    assert doc["statements"]["candidate_served_evaluation"]["status"] == "failed"
    # The job statement stands on its own: a failed readback limits the claim to the job.
    assert doc["statements"]["job_produced_candidate"]["status"].endswith("_reported")
    assert double.calls == []


def test_p5_trainer_digest_mismatch_and_unavailable_provider_hold(tmp_path):
    s = Scenario(CLEAN_WIN)
    rb = readback_for("custom_trainer", artifact_digest=sha("other-trainer-bytes"))
    held(
        provider_run(tmp_path / "a", s, "custom_trainer", readback=rb)[0],
        "integrity_failure",
        "served_candidate_served_other_revision",
    )

    def unavailable():
        raise ConnectionError("generated_provider_unavailable")

    for route in ("managed_provider", "custom_trainer"):
        held(
            provider_run(tmp_path / ("b" + route), s, route, readback=unavailable)[0],
            "integrity_failure",
            "served_serving_readback_unavailable",
        )
        down = ProviderDouble(s.preds["candidate"], fail=True)
        held(
            provider_run(tmp_path / ("c" + route), s, route, double=down)[0],
            "integrity_failure",
            "candidate_replay_failed",
        )
        # A batch the provider says another model served is refused, never scored.
        swapped = ProviderDouble(s.preds["candidate"], served_by="generated-other-revision-2")
        v, _ = provider_run(tmp_path / ("d" + route), s, route, double=swapped)
        doc = held(v, "integrity_failure", "candidate_replay_failed")
        assert doc["comparisons"] == {}


def test_p8_review_l3_a_trainer_route_without_an_artifact_digest_fails_closed(tmp_path):
    """Re-review L3: a trainer readback that omitted artifact_digest passed as 'unavailable'."""
    s = Scenario(CLEAN_WIN)
    v, double = provider_run(
        tmp_path / "a",
        s,
        "custom_trainer",
        readback=readback_for("custom_trainer", artifact_digest=None),
    )
    held(v, "integrity_failure", "served_trainer_artifact_digest_unavailable")
    assert double.calls == []
    job = provider_job("custom_trainer", artifact_digest=None)
    v, double = provider_run(tmp_path / "b", s, "custom_trainer", job=job)
    doc = held(v, "integrity_failure", "job_trainer_artifact_digest_missing")
    assert "served_trainer_artifact_digest_unavailable" in doc["reasons"]
    assert double.calls == []
    # A managed provider's weights stay opaque: no digest is required or invented there.
    managed, _ = provider_run(tmp_path / "c", s, "managed_provider")
    drift = managed.document["statements"]["candidate_served_evaluation"]["drift_checks"]
    assert managed.outcome == "verified_gain" and drift["artifact_digest"] == "unavailable"
    # Where a managed provider does report a digest on both sides, they must agree.
    reporting = provider_job("managed_provider", artifact_digest=TRAINER_DIGEST)
    for name, served_digest, outcome in (
        ("same", TRAINER_DIGEST, "verified_gain"),
        ("other", sha("other-managed-bytes"), "integrity_failure"),
    ):
        v, _ = provider_run(
            tmp_path / name,
            s,
            "managed_provider",
            job=reporting,
            readback=readback_for("managed_provider", artifact_digest=served_digest),
        )
        assert v.outcome == outcome
        if outcome == "integrity_failure":
            held(v, outcome, "served_candidate_served_other_revision")
        else:
            served = v.document["statements"]["candidate_served_evaluation"]
            assert served["drift_checks"]["artifact_digest"] == "matches_job_record"


@pytest.mark.parametrize("route", ["managed_provider", "custom_trainer"])
def test_p9_review_l3_intent_and_client_token_bind_only_when_the_evaluator_holds_them(
    tmp_path, route
):
    s = Scenario(CLEAN_WIN)
    job = provider_job(route)

    def evidence(**over):
        return iv.ProviderEvidence(job, PARENT, CANDIDATE_IDS[route], **over)

    both = evidence(
        expected_intent_sha256=job.intent_sha256, expected_client_token=job.client_token
    )
    v, _ = provider_run(tmp_path / "both", s, route, evidence=both)
    assert v.outcome == "verified_gain"
    assert v.document["statements"]["job_produced_candidate"]["job_intent_binding"] == (
        "checked_against_supplied_expectation"
    )
    one = evidence(expected_intent_sha256=job.intent_sha256)
    v, _ = provider_run(tmp_path / "one", s, route, evidence=one)
    assert v.document["statements"]["job_produced_candidate"]["job_intent_binding"] == (
        "partially_checked_against_supplied_expectation"
    )
    for name, over, reason in (
        ("intent", {"expected_intent_sha256": sha("other-intent")}, "job_intent_mismatch"),
        ("token", {"expected_client_token": "generated-other-token"}, "job_client_token_mismatch"),
        ("bad", {"expected_intent_sha256": "not-a-digest"}, "provider_expectation_malformed"),
        ("bad-token", {"expected_client_token": "has spaces"}, "provider_expectation_malformed"),
    ):
        v, double = provider_run(tmp_path / name, s, route, evidence=evidence(**over))
        held(v, "integrity_failure", "job_" + reason)
        assert double.calls == []


def test_p10_review_l3_the_immutability_self_claim_never_raises_a_tier(tmp_path):
    s = Scenario(CLEAN_WIN)
    open_weight = run(tmp_path / "open", s)
    for route in ("managed_provider", "custom_trainer"):
        v, _ = provider_run(tmp_path / route, s, route)
        doc = v.document
        served = doc["statements"]["candidate_served_evaluation"]
        assert served["drift_checks"]["identity_kind"] == "provider_self_claim"
        assert served["serving_evidence"] == "provider_reported_readback"
        assert doc["evidence"] == open_weight.document["evidence"]
        assert doc["evidence"]["evidence_tier"] == "digest_self_consistent_only"
        assert v.custody_tier == open_weight.custody_tier == "digest_self_consistent_only"
        assert "verified" not in json.dumps(served) and "immutable_verified" not in json.dumps(doc)


def test_p6_retry_after_a_lost_result_returns_the_stored_verdict_without_a_provider_call(
    tmp_path,
):
    ev = evaluator(tmp_path)
    s = Scenario(CLEAN_WIN)
    first, d1 = provider_run(tmp_path, s, "managed_provider", ev=ev)
    again, d2 = provider_run(tmp_path, s, "managed_provider", ev=ev)
    assert not first.replayed and again.replayed and again.document == first.document
    assert d1.calls and d2.calls == []


def test_p7_route_scope_and_tuned_comparator_rule(tmp_path):
    s = Scenario(CLEAN_WIN)
    evidence = iv.ProviderEvidence(
        provider_job("managed_provider"), PARENT, CANDIDATE_IDS["managed_provider"]
    )
    held(
        run(tmp_path / "a", s, provider=evidence),
        "integrity_failure",
        "job_provider_evidence_on_a_non_provider_route",
    )
    only_incumbent = Scenario(CLEAN_WIN, comparators=("incumbent",))
    with pytest.raises(iv.VerdictInputError, match="requires_incumbent_and_simple_repair"):
        provider_run(
            tmp_path / "b",
            only_incumbent,
            "managed_provider",
            pol=policy(required_comparators=("incumbent",)),
        )
    with pytest.raises(iv.VerdictInputError, match="unsupported_candidate_route"):
        run(tmp_path / "c", s, candidate_kind="managed_provider")  # a route name, not a kind
    replay = iv.ProviderReplay(
        route="managed_provider",
        parent_model_id=PARENT,
        candidate_id=CANDIDATE_IDS["managed_provider"],
        readback_fn=dict,
        predict_fn=ProviderDouble(s.preds["candidate"]).predict,
    )
    with pytest.raises(ValueError, match="provider_candidate_lineage_mismatch"):
        replay.predict([{"case_ref": "case-0000"}], expected_artifact=CANDIDATE)
    with pytest.raises(iv.VerdictInputError, match="invalid_provider_replay"):
        iv.ProviderReplay(
            route="open_weight",
            parent_model_id=PARENT,
            candidate_id="x",
            readback_fn=dict,
            predict_fn=ProviderDouble({}).predict,
        )
