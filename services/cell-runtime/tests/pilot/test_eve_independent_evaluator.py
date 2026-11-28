"""OPE-573: the separate EVE scorer and the one staging gate, in process.

Generated sentinel data only; no network, provider, GPU or customer data. IDs are this file's own:
Q scorer qualification (the OPE-528 canary), K custody, I independence, R reproduced verdict and
private re-score, X what only the independent scorer catches, B binding before inference, H holds,
G the staging gate (worker rebuild, aggregator reconcile and independent verdict together).

Evidence rung: ``locally_keyed`` for a verdict re-read from custody, ``digest_self_consistent_only``
for an exported verdict document. Nothing here is proof to a third party.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import eve_aggregate as ag
from opentour_cell.pilot import eve_evaluator as ev
from opentour_cell.pilot import eve_replay as rp
from opentour_cell.pilot import eve_scorer as sc
from opentour_cell.pilot import eve_staging as st
from opentour_cell.pilot import eve_task as task
from test_eve_replay import (
    BASE,
    BASELINE,
    EVALUATOR_ID,
    PROTECTED,
    SENTINEL,
    SIMPLE,
    TUNED,
    Env,
    ScriptedInference,
    full_result,
    key,
    stage,
)


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def refuses(code: str) -> Any:
    return pytest.raises(task.EveRefusal, match=f"^{code}$")


def evaluate(
    env: Env,
    worker: rp.EveReplayWorker,
    result: dict[str, Any],
    *,
    evaluator: ev.EveEvaluator | None = None,
    replays: dict[str, Any] | None = None,
    registration: task.EveTaskRegistration | None = None,
    manifest: rp.ReplayManifest | None = None,
) -> dict[str, Any]:
    return (evaluator or env.evaluator()).evaluate(
        manifest=manifest or env.manifest,
        result=result,
        registration=registration or env.reg,
        journal=worker.journal,
        keys=worker.keys,
        replays=env.replays() if replays is None else replays,
    )


def staged(env: Env, worker: rp.EveReplayWorker, result: dict[str, Any], verdict: Any) -> Any:
    return st.stage_decision(
        result,
        recomputed_digest=result["result_digest"],
        manifest=env.manifest,
        journal=worker.journal,
        scorer=env.scorer,
        key=env.key,
        verdict=verdict,
    )


# ---------------------------------------------------------------- Q qualification
def test_q1_the_independent_scorer_passes_the_canary_with_the_exact_expected_counts() -> None:
    qualified, report = ev.qualify_scorer(ev.IndependentGenerativeScorer("generated-rubric-r1"))
    assert qualified and report == {"canary": 4, "permuted": 2, "controls": 9}


class _Wrong(ev.IndependentGenerativeScorer):
    """Deliberately wrong independent scorers: each misreports one rubric rule."""

    def __init__(self, flaw: str) -> None:
        super().__init__("generated-rubric-r1")
        self.flaw = flaw
        self.first_seen: dict[str, Any] = {}

    def judge(self, expected: Any, shown: frozenset[str], output: Any) -> str:
        if self.flaw == "stale_answer_key" and output is not None:
            # keeps the first answer key it saw for each case: it never reads a changed key
            expected = self.first_seen.setdefault((output.case_id, expected.expectation), expected)
        verdict = super().judge(expected, shown, output)
        if self.flaw == "answers_always_right" and output is not None and output.answer:
            return "correct_answer"
        if self.flaw == "ignores_citations" and verdict == "unverified":
            return "correct_answer" if output.answer == expected.answer else "serious_error"
        if self.flaw == "refusal_is_abstention" and verdict == "inappropriate_refusal":
            return "correct_abstention"
        if self.flaw == "missing_is_refusal" and verdict == "missing_output":
            return "inappropriate_refusal"
        if self.flaw == "raises":
            raise KeyError("generated")
        return verdict


@pytest.mark.parametrize(
    "flaw",
    [
        "answers_always_right",
        "ignores_citations",
        "refusal_is_abstention",
        "missing_is_refusal",
        "stale_answer_key",
        "raises",
    ],
)
def test_q2_a_deliberately_wrong_independent_scorer_fails_and_cannot_issue(
    env: Env, flaw: str
) -> None:
    assert ev.qualify_scorer(_Wrong(flaw))[0] is False
    with refuses("evaluator_scorer_unqualified"):
        env.evaluator(_Wrong(flaw))
    assert not (env.custody_dir / "verdicts").exists()


def test_q3_the_canary_reports_both_expected_values_for_the_permuted_key() -> None:
    qualified, report = ev.qualify_scorer(_Wrong("answers_always_right"))
    assert not qualified and report["canary"] == 4 and report["permuted"] == 4  # misreported
    # a stale answer-key cache passes every rubric control; only the permuted key exposes it
    qualified, report = ev.qualify_scorer(_Wrong("stale_answer_key"))
    assert not qualified and report == {"canary": 4, "permuted": 4, "controls": 9}


# ---------------------------------------------------------------- K custody
def _evaluator_kwargs(env: Env, **changes: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "evaluator_id": EVALUATOR_ID,
        "custody_dir": env.custody_dir,
        "worker_data_dirs": [env.root, env.keys_dir],
        "scorer": ev.IndependentGenerativeScorer(env.manifest.rubric_revision),
        "answer_key": key(),
        "cases": PROTECTED,
    }
    kwargs.update(changes)
    return kwargs


@pytest.mark.parametrize(
    ("changes", "code"),
    [
        pytest.param({"worker_data_dirs": []}, "worker_data_dirs_required", id="no-worker-dirs"),
        pytest.param(
            {"worker_data_dirs": "/tmp/worker"}, "worker_data_dirs_required", id="str-not-list"
        ),
        pytest.param({"evaluator_id": ""}, "invalid_evaluator_identity", id="blank-id"),
        pytest.param({"evaluator_id": "has space"}, "invalid_evaluator_identity", id="bad-id"),
        pytest.param({"custody_dir": Path("rel")}, "evaluator_custody_not_private", id="relative"),
        pytest.param({"retention_seconds": 31 * 86_400}, "evaluator_retention_invalid", id="long"),
        pytest.param({"validity_seconds": 0}, "evaluator_retention_invalid", id="no-validity"),
        pytest.param({"cases": PROTECTED[:-1]}, "evaluator_inputs_unbound", id="key-cases-differ"),
    ],
)
def test_k1_evaluator_configuration_fails_closed(
    env: Env, changes: dict[str, Any], code: str
) -> None:
    with refuses(code):
        ev.EveEvaluator(**_evaluator_kwargs(env, **changes))


def test_k2_custody_may_not_overlap_any_worker_data_directory(env: Env) -> None:
    for custody in (env.root, env.root / "custody", env.keys_dir / "custody", env.root.parent):
        with refuses("evaluator_custody_overlaps_worker_data"):
            ev.EveEvaluator(**_evaluator_kwargs(env, custody_dir=custody))


def test_k3_custody_that_others_can_read_is_refused_not_repaired(env: Env) -> None:
    env.custody_dir.mkdir(mode=0o755)
    os.chmod(env.custody_dir, 0o755)
    with refuses("evaluator_custody_not_private"):
        env.evaluator()
    assert env.custody_dir.stat().st_mode & 0o777 == 0o755  # never chmodded into shape


def test_k4_the_journal_or_key_store_may_not_sit_inside_custody(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    evaluator = env.evaluator()
    inside = rp.ReplayJournal(env.custody_dir / "journal.jsonl")
    with refuses("evaluator_custody_overlaps_worker_data"):
        evaluator.evaluate(
            manifest=env.manifest, result=result, registration=env.reg, journal=inside,
            keys=worker.keys, replays=env.replays(),
        )  # fmt: skip


# ---------------------------------------------------------------- I independence
def test_i1_an_evaluator_with_the_worker_scorers_identity_is_refused(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    same_id = ev.EveEvaluator(**_evaluator_kwargs(env, evaluator_id=env.manifest.scorer_id))
    with refuses("evaluator_not_independent"):
        evaluate(env, worker, result, evaluator=same_id)


def test_i2_the_evaluator_never_reads_the_worker_scorer_or_its_journaled_totals(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    verdict = evaluate(env, worker, result)
    assert verdict["outcome"] == "reproduced" and verdict["reasons"] == []
    assert verdict["scorer"]["scorer_id"] == ev.SCORER_ID != env.manifest.scorer_id
    assert verdict["worker_scorer"] == {
        "scorer_id": env.manifest.scorer_id,
        "version": env.manifest.scorer_version,
        "rubric_revision": env.manifest.rubric_revision,
    }
    assert verdict["verdicts_rechecked"] is True and verdict["basis"] == ev.BASIS


# ---------------------------------------------------------------- R reproduced and re-scored
def test_r1_a_genuine_result_is_reproduced_and_every_output_is_bound_to_its_commitment(
    env: Env,
) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    verdict = evaluate(env, worker, result)
    assert verdict["outcome"] == "reproduced"
    for name, arm in verdict["arms"].items():
        assert arm["replayed"] and arm["outputs_committed"] == 5, name
        assert arm["outputs_not_committed"] == 0 and arm["verdict_disagreements"] == 0
        assert arm["totals"] == result["body"]["arms"][name]
    assert verdict["paired_comparison"] == result["body"]["paired_comparison"]
    assert verdict["improvement_claim"] == "observed_net_fixes"
    assert verdict["verdict_digest"] == task.digest(
        {k: v for k, v in verdict.items() if k != "verdict_digest"}
    )


def test_r2_the_scorecard_is_recomputable_from_the_private_predictions_alone(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    verdict = evaluate(env, worker, result)
    evaluator = env.evaluator()  # a fresh process would construct exactly this
    calls_before = env.transport.seen
    again = evaluator.recompute(verdict["verdict_key"], accessor_id=EVALUATOR_ID)
    assert again["matches"] is True and env.transport.seen is calls_before  # no model call
    assert again["arms"] == {name: arm["totals"] for name, arm in verdict["arms"].items()}
    assert again["improvement_claim"] == "observed_net_fixes"


def test_r3_private_predictions_are_the_evaluators_own_and_retention_limited(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    verdict = evaluate(env, worker, result)
    evaluator = env.evaluator()
    with refuses("private_predictions_access_denied"):
        evaluator.private_predictions(verdict["verdict_key"], accessor_id="worker")
    held = evaluator.private_predictions(verdict["verdict_key"], accessor_id=EVALUATOR_ID)
    rows = [r for arm in held["record"]["arms"].values() for r in arm["rows"]]
    assert len(rows) == 15 and all(r["committed"] for r in rows)
    assert {r["scorer_id"] for r in rows} == {ev.SCORER_ID}
    assert any(r["prediction"] and SENTINEL in str(r["prediction"][0]) for r in rows)
    later = ev.EveEvaluator(
        **_evaluator_kwargs(env),
        clock=lambda: held["retention_until"] + 1,  # retention elapsed
    )
    with refuses("private_predictions_expired"):
        later.private_predictions(verdict["verdict_key"], accessor_id=EVALUATOR_ID)
    with refuses("private_predictions_expired"):
        later.recompute(verdict["verdict_key"], accessor_id=EVALUATOR_ID)
    assert later.purge_expired() == 1
    assert later.read_verdict(verdict["verdict_key"]) == verdict  # the verdict keeps its record
    with refuses("private_predictions_absent_or_purged"):
        evaluator.private_predictions(verdict["verdict_key"], accessor_id=EVALUATOR_ID)


def test_r4_a_tampered_custody_record_is_refused_on_read(env: Env) -> None:
    worker = env.worker()
    verdict = evaluate(env, worker, full_result(env, worker))
    key_ = verdict["verdict_key"]
    path = env.custody_dir / "verdicts" / f"{key_}.json"
    original = path.read_text()
    stored = json.loads(original)
    stored["document"]["improvement_claim"] = "no_net_fixes"
    path.write_text(json.dumps(stored))
    with refuses("stored_verdict_tampered"):
        env.evaluator().read_verdict(key_)
    path.write_text(original)
    assert env.evaluator().read_verdict(key_) == verdict
    private = env.custody_dir / "predictions" / f"{verdict['predictions_commitment']}.json"
    stored = json.loads(private.read_text())
    stored["record"]["arms"]["candidate"]["rows"][0]["verdict"] = "serious_error"
    private.write_text(json.dumps(stored))
    with refuses("private_predictions_tampered"):
        env.evaluator().private_predictions(key_, accessor_id=EVALUATOR_ID)
    with refuses("private_predictions_tampered"):
        env.evaluator().recompute(key_, accessor_id=EVALUATOR_ID)


def test_r6_retention_cannot_be_extended_by_editing_the_private_record(env: Env) -> None:
    worker = env.worker()
    verdict = evaluate(env, worker, full_result(env, worker))
    private = env.custody_dir / "predictions" / f"{verdict['predictions_commitment']}.json"
    stored = json.loads(private.read_text())
    assert stored["record"]["retention_until"] == verdict["retention_until"]
    stored["record"]["retention_until"] += 10 * 86_400
    private.write_text(json.dumps(stored))
    with refuses("private_predictions_tampered"):
        env.evaluator().private_predictions(verdict["verdict_key"], accessor_id=EVALUATOR_ID)
    assert env.evaluator().purge_expired() == 1  # retention fails toward deletion
    assert not private.exists()


def test_r5_evaluating_the_same_claim_again_returns_the_one_stored_verdict(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    first = evaluate(env, worker, result)
    replays = env.replays()
    replays["baseline"] = ScriptedInference(BASE, BASELINE)
    second = evaluate(env, worker, result, replays=replays)
    assert second == first and replays["baseline"].calls == 0  # no second replay
    stale = ev.EveEvaluator(**_evaluator_kwargs(env), clock=lambda: first["not_after"] + 1)
    fresh = evaluate(env, worker, result, evaluator=stale)
    assert fresh["issued_at"] > first["issued_at"] and fresh["outcome"] == "reproduced"
    assert list((env.custody_dir / "archive").iterdir())  # the stale one is kept, not deleted


# ---------------------------------------------------------------- X what only it catches
class Colluding(sc.DeterministicScorer):
    """Passes every generated self-check control, then marks real wrong answers correct."""

    def score(self, case: Any, expected: Any, output: Any) -> rp.Verdict:
        verdict = super().score(case, expected, output)
        if case.case_id.startswith("generated-p") and verdict is rp.Verdict.SERIOUS_ERROR:
            return rp.Verdict.CORRECT_ANSWER
        return verdict


def test_x1_a_colluding_worker_scorer_passes_rebuild_and_aggregator_but_not_the_evaluator(
    env: Env,
) -> None:
    colluding = Colluding(env.manifest.scorer_version, env.manifest.rubric_revision)
    assert rp.scorer_selfcheck(colluding)
    worker = env.worker()
    arms = {
        "baseline": env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE),
                               worker=worker, scorer_=colluding),
        "simple_repair": env.replay(env.arm("simple_repair"), ScriptedInference(BASE, SIMPLE),
                                    worker=worker, scorer_=colluding),
    }  # fmt: skip
    infer = env.candidate_inference(TUNED)
    arms["candidate"] = env.replay(
        env.arm("candidate", infer.served_revision), infer, worker=worker, scorer_=colluding
    )
    result = env.result(arms, journal=worker.journal, scorer_=colluding)
    # the rebuild and the aggregator both agree with the colluding verdicts ...
    assert ag.reconcile(result, journal_path=worker.journal.path, manifest=env.manifest).consistent
    without = staged(env, worker, result, None)
    assert without.reasons == ("independent_verdict_missing",)
    # ... only the independent re-score sees that case 2 and case 3 were never right
    verdict = evaluate(env, worker, result)
    assert verdict["outcome"] == "integrity_failure"
    assert "verdict_disagreement_baseline" in verdict["reasons"]
    assert "verdict_disagreement_simple_repair" in verdict["reasons"]
    assert verdict["arms"]["baseline"]["outputs_not_committed"] == 0  # the same outputs
    decision = staged(env, worker, result, verdict)
    assert not decision.stageable
    assert "independent_verdict_integrity_failure" in decision.reasons
    assert "independent_recount_mismatch" in decision.reasons


def test_x2_a_serving_difference_at_replay_is_caught_by_the_commitments(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    replays = env.replays()
    replays["baseline"] = ScriptedInference(BASE, {**BASELINE, 2: "correct"})
    verdict = evaluate(env, worker, result, replays=replays)
    assert verdict["outcome"] == "integrity_failure"
    assert verdict["arms"]["baseline"]["outputs_not_committed"] == 1
    assert {"outputs_not_committed_baseline", "verdict_disagreement_baseline",
            "claimed_totals_mismatch_baseline"} <= set(verdict["reasons"])  # fmt: skip


def test_x3_a_redigested_result_with_edited_totals_is_never_taken_as_expected(env: Env) -> None:
    worker = env.worker()
    genuine = full_result(env, worker)
    body = json.loads(json.dumps(genuine["body"]))
    body["arms"]["candidate"]["correct"] = 4
    forged = {"body": body, "result_digest": task.digest(body)}
    verdict = evaluate(env, worker, forged)
    assert verdict["outcome"] == "integrity_failure"
    assert verdict["reasons"] == ["claimed_totals_mismatch_candidate"]
    assert verdict["arms"]["candidate"]["totals"]["correct"] == 5  # its own recount


def test_x4_a_candidate_served_through_a_plain_inference_fails_lineage(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    replays = env.replays()
    served = replays["candidate"].served_revision
    replays["candidate"] = ScriptedInference(served, TUNED)  # right revision string, no lineage
    verdict = evaluate(env, worker, result, replays=replays)
    assert "candidate_lineage_unverified" in verdict["reasons"]
    assert replays["candidate"].calls == 0
    job = env.transport.jobs["generated-fit-intent-1"]
    swapped = replace(env.transport.collect(job), artifact_or_serving_revision="generated-other")
    with refuses("candidate_lineage_unverified"):
        ev.EvaluatorCandidateReplay(env.transport, job, swapped, env.reg)


# ---------------------------------------------------------------- B binding before inference
def _counting(env: Env) -> dict[str, Any]:
    replays = env.replays()
    replays["baseline"] = ScriptedInference(BASE, BASELINE)
    replays["simple_repair"] = ScriptedInference(BASE, SIMPLE)
    return replays


def test_b1_an_evaluator_holding_another_answer_key_runs_no_inference(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    entries = {
        c.case_id: rp.ProtectedAnswer(rp.Expectation.ANSWER, "generated-other", frozenset())
        for c in PROTECTED
    }
    other = ev.EveEvaluator(**_evaluator_kwargs(env, answer_key=rp.AnswerKey(entries)))
    replays = _counting(env)
    verdict = evaluate(env, worker, result, evaluator=other, replays=replays)
    assert verdict["outcome"] == "integrity_failure"
    assert {"replay_not_bound", "eligibility_mismatch"} <= set(verdict["reasons"])
    assert replays["baseline"].calls == replays["simple_repair"].calls == 0


def test_b2_a_rubric_or_registration_mismatch_runs_no_inference(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    replays = _counting(env)
    other_rubric = ev.EveEvaluator(
        **_evaluator_kwargs(env, scorer=ev.IndependentGenerativeScorer("generated-rubric-r9"))
    )
    verdict = evaluate(env, worker, result, evaluator=other_rubric, replays=replays)
    assert verdict["reasons"] == ["rubric_revision_mismatch"]
    other_model = replace(env.reg, model_ref="generated-eve-other")
    verdict = evaluate(env, worker, result, replays=replays, registration=other_model)
    assert verdict["reasons"] == ["registration_mismatch"]
    assert replays["baseline"].calls == 0


def test_b3_non_deterministic_decoding_is_held_before_any_replay(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    replays = _counting(env)
    sampled = replace(env.reg, decoding=task.DecodingPolicy(700, 900, 256, 7))
    verdict = evaluate(env, worker, result, replays=replays, registration=sampled)
    assert verdict["outcome"] == "integrity_failure"  # also a registration mismatch
    assert "stochastic_decoding_unsupported" in verdict["reasons"]
    assert replays["baseline"].calls == 0


def test_b4_an_arm_served_by_another_revision_is_refused_before_inference(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    replays = _counting(env)
    replays["baseline"] = ScriptedInference("b" * 40, BASELINE)
    verdict = evaluate(env, worker, result, replays=replays)
    assert "served_identity_mismatch_baseline" in verdict["reasons"]
    assert replays["baseline"].calls == 0


# ---------------------------------------------------------------- H holds are not verdicts
def test_h1_a_missing_replay_or_run_key_holds_and_a_hold_is_never_final(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    replays = env.replays()
    del replays["candidate"]
    held = evaluate(env, worker, result, replays=replays)
    assert held["outcome"] == "held"
    assert held["reasons"] == ["evaluator_replay_unavailable_candidate"]
    assert "independent_verdict_held" in staged(env, worker, result, held).reasons
    again = evaluate(env, worker, result)  # the hold is re-evaluated, not returned
    assert again["outcome"] == "reproduced" and again["issued_at"] >= held["issued_at"]
    assert staged(env, worker, result, again).stageable


def test_h2_a_missing_run_key_holds_and_restoring_the_key_recovers(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    store = worker.keys.path
    aside = store.with_name(store.name + ".aside")
    store.rename(aside)
    held = evaluate(env, worker, result)
    # nothing could be checked, so nothing is a stored failure
    assert held["outcome"] == "held"
    assert held["reasons"] == ["commitment_unverifiable_commitment_key_unavailable"]
    for arm in held["arms"].values():
        assert arm["outputs_unverifiable"] == 5
        assert arm["outputs_not_committed"] == 0 and arm["outputs_committed"] == 0
    assert "independent_verdict_held" in staged(env, worker, result, held).reasons
    aside.rename(store)
    recovered = evaluate(env, worker, result)
    assert recovered["outcome"] == "reproduced" and recovered["reasons"] == []
    assert all(a["outputs_committed"] == 5 for a in recovered["arms"].values())
    assert staged(env, worker, result, recovered).stageable


def test_h3_a_commitment_checked_and_not_matching_stays_a_stored_failure(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    replays = env.replays()
    replays["baseline"] = ScriptedInference(BASE, {**BASELINE, 2: "correct"})
    failed = evaluate(env, worker, result, replays=replays)
    assert failed["outcome"] == "integrity_failure"
    assert "outputs_not_committed_baseline" in failed["reasons"]
    honest = env.replays()
    again = evaluate(env, worker, result, replays=honest)
    assert again == failed and honest["baseline"].calls == 0  # sticky, not re-run


# ---------------------------------------------------------------- G the staging gate
def test_g1_staging_calls_reconcile_and_requires_all_three_checks(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    verdict = evaluate(env, worker, result)
    calls: list[Path] = []
    real = ag.reconcile

    def counting(*args: Any, **kwargs: Any) -> ag.Reconciliation:
        calls.append(kwargs["journal_path"])
        return real(*args, **kwargs)

    monkeypatch.setattr(ag, "reconcile", counting)
    assert staged(env, worker, result, verdict).stageable
    assert calls == [worker.journal.path]
    monkeypatch.setattr(ag, "reconcile", lambda *a, **k: ag.Reconciliation(False, ("arms.x",)))
    decision = staged(env, worker, result, verdict)
    assert decision.reasons == ("aggregate_inconsistent",)
    assert decision.aggregate_mismatches == ("arms.x",)


def test_g2_eve_replay_has_no_staging_decision_of_its_own() -> None:
    assert not hasattr(rp, "stage_decision") and not hasattr(rp, "StageDecision")
    assert not hasattr(rp, "_rebuild")


def test_g3_an_aggregation_bug_in_the_replay_module_cannot_stage(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = rp._arm_body

    def miscounting(manifest: rp.ReplayManifest, record: rp.ArmRecord) -> dict[str, Any]:
        body = real(manifest, record)
        return {**body, "serious_errors": max(0, body["serious_errors"] - 1)}

    monkeypatch.setattr(rp, "_arm_body", miscounting)
    worker = env.worker()
    result = full_result(env, worker)
    verdict = evaluate(env, worker, result)
    decision = staged(env, worker, result, verdict)
    assert not decision.stageable
    assert "aggregate_inconsistent" in decision.reasons
    assert "independent_verdict_integrity_failure" in decision.reasons
    assert any(m.endswith(".serious_errors") for m in decision.aggregate_mismatches)


VerdictEdit = Callable[[dict[str, Any]], None]


def _reseal(document: dict[str, Any]) -> dict[str, Any]:
    body = {k: v for k, v in document.items() if k != "verdict_digest"}
    return {**body, "verdict_digest": task.digest(body)}


def _set(**changes: Any) -> VerdictEdit:
    return lambda d: d.update(changes)


def _nested(part: str, **changes: Any) -> VerdictEdit:
    return lambda d: d[part].update(changes)


def _arm(name: str, **changes: Any) -> VerdictEdit:
    return lambda d: d["arms"][name].update(changes)


def _totals(name: str, **changes: Any) -> VerdictEdit:
    return lambda d: d["arms"][name]["totals"].update(changes)


OTHER = "sha256:" + "4" * 64
G4 = {
    "failed": (_set(outcome="integrity_failure"), "independent_verdict_integrity_failure"),
    "held": (_set(outcome="held"), "independent_verdict_held"),
    "reproduced-with-reasons": (
        _set(reasons=["generated"]),
        "independent_verdict_inconsistent_outcome",
    ),
    "another-result": (_set(result_digest=OTHER), "independent_verdict_for_another_claim"),
    "another-manifest": (_set(manifest_digest=OTHER), "independent_verdict_for_another_claim"),
    "worker-identity": (
        _set(evaluator_id="generated:opentour-eve-deterministic-stand-in"),
        "independent_verdict_not_independent",
    ),
    "worker-scorer-version": (
        _nested("scorer", version="generated-scorer-v1"),
        "independent_verdict_not_independent",
    ),
    "other-rubric": (_nested("scorer", rubric_revision="o"), "independent_verdict_not_independent"),
    "unqualified": (_nested("scorer", qualified=False), "independent_scorer_unqualified"),
    "separated-custody": (_set(custody_separated=True), "independent_verdict_overstated"),
    "customer-tier": (_set(evidence_tier="customer_witnessed"), "independent_verdict_overstated"),
    "not-rechecked": (_set(verdicts_rechecked=False), "independent_verdict_overstated"),
    "stale": (lambda d: d.update(not_after=d["issued_at"]), "independent_verdict_stale"),
    "arm-missing": (lambda d: d["arms"].pop("simple_repair"), "independent_verdict_incomplete"),
    "disagreement": (_arm("candidate", verdict_disagreements=1), "independent_verdict_incomplete"),
    "uncommitted": (_arm("baseline", outputs_not_committed=1), "independent_verdict_incomplete"),
    "not-replayed": (_arm("baseline", replayed=False), "independent_verdict_incomplete"),
    "recount": (_totals("baseline", correct=2), "independent_recount_mismatch"),
    "claim": (_set(improvement_claim="no_net_fixes"), "independent_recount_mismatch"),
    "schema": (_set(schema_version="other"), "independent_verdict_unrecognized"),
    "outcome": (_set(outcome="verified_gain"), "independent_verdict_unrecognized"),
    "malformed": (lambda d: d.pop("arms"), "independent_verdict_malformed"),
}


@pytest.mark.parametrize(("edit", "reason"), list(G4.values()), ids=list(G4))
def test_g4_a_verdict_document_that_does_not_reproduce_exactly_this_claim_cannot_stage(
    env: Env, edit: VerdictEdit, reason: str
) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    document = json.loads(json.dumps(evaluate(env, worker, result)))
    edit(document)
    decision = staged(env, worker, result, _reseal(document))
    assert not decision.stageable and reason in decision.reasons, decision.reasons
    unsealed = json.loads(json.dumps(evaluate(env, worker, result)))
    edit(unsealed)
    if "arms" in unsealed:
        assert "independent_verdict_inconsistent" in staged(env, worker, result, unsealed).reasons


def test_g5_known_limit_a_document_consistent_with_everything_cannot_be_told_from_the_real_one(
    env: Env,
) -> None:
    """Pinned on purpose: the exported document is digest_self_consistent_only (no OPE-566 key).

    Someone who can write files on this host can write a document that names the right claim and
    recounts to the journal; the gate cannot tell it from the evaluator's. What it can and does
    require is that such a document agree with the journal, the aggregator and the rebuild.
    """
    worker = env.worker()
    result = full_result(env, worker)
    genuine = evaluate(env, worker, result)
    forged = _reseal({**genuine, "evaluator_id": "generated-someone-else", "issued_at":
                      genuine["issued_at"]})  # fmt: skip
    assert staged(env, worker, result, forged).stageable
    assert genuine["export_tier"] == ev.EXPORT_TIER == "digest_self_consistent_only"


def test_g6_the_verdict_document_never_carries_a_protected_answer(env: Env) -> None:
    worker = env.worker()
    verdict = evaluate(env, worker, full_result(env, worker))
    assert SENTINEL not in json.dumps(verdict)
    assert env.key.leaks(verdict) is False


def test_g7_staging_through_the_shared_helper_uses_a_real_independent_verdict(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    assert stage(env, result, recomputed_digest=result["result_digest"]).stageable
    assert not stage(env, result, recomputed_digest=result["result_digest"], verdict=None).stageable
