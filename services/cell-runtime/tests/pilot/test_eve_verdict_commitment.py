"""OPE-573: keyed output commitment, scorer attribution, a separate aggregator, honest staging.

Generated sentinel data only; no network, provider, GPU or customer data. IDs are this file's own:
C keyed commitment, A scorer attribution, G separate aggregator, S stage_decision derives the
result itself.

Evidence rung: ``locally_keyed``. The commitment key and the journal are held by the same party, so
everything here is tamper evidence for ourselves, never proof to a third party.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import eve_aggregate as ag
from opentour_cell.pilot import eve_replay as rp
from opentour_cell.pilot import eve_task as task
from test_eve_replay import BASE, BASELINE, SIMPLE, Env, ScriptedInference, full_result, output

COMMITMENT = re.compile(r"hmac-sha256:[0-9a-f]{64}")


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def refuses(code: str) -> Any:
    return pytest.raises(task.EveRefusal, match=f"^{code}$")


def rewrite_rows(path: Path, edit: Callable[[str, list[list[Any]]], list[list[Any]]]) -> None:
    """Edit journaled arm rows in place, as someone with write access to the file could."""
    lines = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if event["event"] == "arm":
            event["record"]["rows"] = edit(event["record"]["spec"][0], event["record"]["rows"])
        lines.append(json.dumps(event, sort_keys=True, separators=(",", ":")))
    path.write_text("\n".join(lines) + "\n")


def baseline_run(env: Env) -> tuple[rp.EveReplayWorker, rp.ArmRecord]:
    worker = env.worker()
    return worker, env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE), worker=worker)


def verify(
    env: Env,
    worker: rp.EveReplayWorker,
    n: int,
    presented: rp.ArmOutput | None,
    keys: rp.CommitmentKeyStore | None = None,
) -> bool:
    return rp.verify_output(
        keys=keys or worker.keys,
        journal=worker.journal,
        manifest=env.manifest,
        arm=env.arm("baseline"),
        case_id=f"generated-p{n}",
        presented=presented,
    )


# ---------------------------------------------------------------- C keyed commitment
def test_c1_every_scored_output_carries_a_commitment_that_verifies_the_actual_output(
    env: Env,
) -> None:
    worker, record = baseline_run(env)
    assert len(record.rows) == len(env.manifest.eligible_case_ids) == 5
    assert all(COMMITMENT.fullmatch(row.commitment) for row in record.rows)
    for n, kind in BASELINE.items():
        assert verify(env, worker, n, output(n, kind)), n


def test_c2_a_substituted_output_is_detected(env: Env) -> None:
    worker, _ = baseline_run(env)
    genuine = output(1, "correct")
    substitutes = (
        output(1, "wrong"),
        output(1, "unverifiable"),
        output(1, "refuse"),
        replace(genuine, citations=("p1-a", "p1-b")),
        replace(genuine, answer=genuine.answer + " "),
        replace(genuine, answer=genuine.answer.upper()),
        None,
    )
    assert verify(env, worker, 1, genuine)
    for substitute in substitutes:
        assert not verify(env, worker, 1, substitute), substitute
    # an output that was really scored for another case does not verify here either
    assert not verify(env, worker, 1, replace(output(2, "wrong"), case_id="generated-p1"))


def test_c3_commitment_is_keyed_per_run_not_a_guessable_bare_digest(env: Env) -> None:
    worker, record = baseline_run(env)
    row, out = record.rows[0], output(1, "correct")
    mac = row.commitment.split(":", 1)[1]
    fields = [out.case_id, out.answer, list(out.citations), out.abstained]
    bare = {
        hashlib.sha256(str(out.answer).encode()).hexdigest(),
        hashlib.sha256(task.canonical(fields)).hexdigest(),
        task.digest(fields).split(":", 1)[1],
        task.digest(out.answer).split(":", 1)[1],
    }
    assert mac not in bare
    # Enumerating low-entropy candidates without the run key confirms nothing.
    run_id = rp.run_id_for(env.manifest, env.arm("baseline"))
    guesses = [rp.output_commitment(bytes(32), run_id=run_id, row=row, output=out)]
    guesses += [
        rp.output_commitment(k, run_id=run_id, row=row, output=out)
        for k in (b"", b"generated-guess", run_id.encode())
    ]
    assert row.commitment not in guesses
    # Each run has its own key: two arms, and the same arm in a second store, differ.
    simple = env.replay(env.arm("simple_repair"), ScriptedInference(BASE, SIMPLE), worker=worker)
    simple_id = rp.run_id_for(env.manifest, env.arm("simple_repair"))
    assert worker.keys.require(run_id) != worker.keys.require(simple_id)
    assert simple.rows[0].verdict is row.verdict  # same output, same verdict ...
    assert simple.rows[0].commitment != row.commitment  # ... different commitment
    other = env.worker("other.jsonl")
    again = env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE), worker=other)
    assert [r.verdict for r in again.rows] == [r.verdict for r in record.rows]
    assert all(a.commitment != b.commitment for a, b in zip(again.rows, record.rows, strict=True))
    assert other.keys.require(run_id) != worker.keys.require(run_id)


def test_c4_the_run_key_never_lives_in_the_journal_row_it_protects(env: Env) -> None:
    worker, _ = baseline_run(env)
    run_key = worker.keys.require(rp.run_id_for(env.manifest, env.arm("baseline")))
    assert len(run_key) == 32
    journal_bytes = worker.journal.path.read_bytes()
    assert run_key not in journal_bytes and run_key.hex().encode() not in journal_bytes
    assert worker.keys.path.parent.resolve() != worker.journal.path.parent.resolve()
    assert run_key.hex() not in repr(worker.keys)
    assert stat.S_IMODE(worker.keys.path.stat().st_mode) == 0o600
    common = {"registry": env.registry, "preflight": env.preflight, "credentials": env.credentials}
    same = env.root / "j.jsonl"
    for keys_path in (same, env.root / "j.keys"):
        with refuses("commitment_key_colocated"):
            rp.EveReplayWorker(
                **common, journal=rp.ReplayJournal(same), keys=rp.CommitmentKeyStore(keys_path)
            )


def test_c5_verifying_needs_the_run_key_which_survives_a_restart(env: Env) -> None:
    worker, _ = baseline_run(env)
    elsewhere = rp.CommitmentKeyStore(env.root / "elsewhere" / "k.keys")
    with refuses("commitment_key_unavailable"):
        verify(env, worker, 1, output(1, "correct"), keys=elsewhere)
    reopened = rp.CommitmentKeyStore(worker.keys.path)
    assert verify(env, worker, 1, output(1, "correct"), keys=reopened)
    # a restarted worker reuses the journaled arm and the same key
    restarted = env.worker()
    infer = ScriptedInference(BASE, BASELINE)
    env.replay(env.arm("baseline"), infer, worker=restarted)
    assert infer.calls == 0
    assert verify(env, restarted, 1, output(1, "correct"))


def test_c6_an_edited_verdict_or_moved_commitment_fails_against_the_genuine_output(
    env: Env,
) -> None:
    worker, _ = baseline_run(env)
    assert verify(env, worker, 2, output(2, "wrong"))

    def promote(arm: str, rows: list[list[Any]]) -> list[list[Any]]:
        rows[1][1] = rp.Verdict.CORRECT_ANSWER.value  # case p2 was a serious error
        return rows

    rewrite_rows(worker.journal.path, promote)
    assert not verify(env, worker, 2, output(2, "wrong"))

    env2 = Env(env.root / "swap")
    worker2, _ = baseline_run(env2)

    def swap(arm: str, rows: list[list[Any]]) -> list[list[Any]]:
        rows[0][6], rows[1][6] = rows[1][6], rows[0][6]
        return rows

    rewrite_rows(worker2.journal.path, swap)
    assert not verify(env2, worker2, 1, output(1, "correct"))
    assert not verify(env2, worker2, 2, output(2, "wrong"))


def test_c7_commitment_text_leaks_no_protected_answer(env: Env) -> None:
    worker = env.worker()
    full_result(env, worker)
    journal = worker.journal.path.read_bytes()
    assert b"generated-protected-answer-sentinel" not in journal
    for line in journal.splitlines():
        event = json.loads(line)
        if event["event"] == "arm":
            assert all(COMMITMENT.fullmatch(r[6]) for r in event["record"]["rows"])


# ---------------------------------------------------------------- A scorer attribution
def test_a1_every_verdict_row_is_attributed_to_the_frozen_scorer_and_rubric(env: Env) -> None:
    worker = env.worker()
    full_result(env, worker)
    frozen = (env.manifest.scorer_version, env.manifest.rubric_revision)
    records = worker.journal.arms_for(env.manifest.digest)
    assert {r.spec.arm_id for r in records} == {"baseline", "simple_repair", "candidate"}
    for record in records:
        assert all((r.scorer_version, r.rubric_revision) == frozen for r in record.rows)
    assert frozen == ("generated-scorer-v1", "generated-rubric-r1")


def test_a2_every_reader_rejects_an_unattributed_row(env: Env) -> None:
    worker = env.worker()
    arms = env.baseline_and_simple(worker)
    rewrite_rows(worker.journal.path, lambda arm, rows: [r[:4] for r in rows])  # legacy shape
    run_id = rp.run_id_for(env.manifest, env.arm("baseline"))
    with refuses("verdict_unattributed"):
        worker.journal.arm(run_id)
    with refuses("verdict_unattributed"):
        env.replay(env.arm("baseline"), ScriptedInference(BASE, BASELINE), worker=worker)
    with refuses("verdict_unattributed"):
        env.result(arms, journal=worker.journal)
    with refuses("verdict_unattributed"):
        ag.rederive(worker.journal.path, env.manifest)
    decision = rp.stage_decision(
        {"body": {}, "result_digest": "x"}, recomputed_digest="x", **env.staging(worker.journal)
    )
    assert not decision.stageable and decision.reasons == ("rebuild_refused_verdict_unattributed",)


@pytest.mark.parametrize(
    ("index", "value", "code"),
    [
        pytest.param(4, "", "verdict_unattributed", id="blank-scorer"),
        pytest.param(5, "", "verdict_unattributed", id="blank-rubric"),
        pytest.param(4, None, "verdict_unattributed", id="null-scorer"),
        pytest.param(5, 7, "verdict_unattributed", id="int-rubric"),
        pytest.param(6, "", "verdict_uncommitted", id="blank-commitment"),
        pytest.param(6, "sha256:" + "0" * 64, "verdict_uncommitted", id="bare-digest-commitment"),
    ],
)
def test_a3_blank_or_mistyped_attribution_or_commitment_is_rejected(
    env: Env, index: int, value: Any, code: str
) -> None:
    worker, _ = baseline_run(env)

    def blank(arm: str, rows: list[list[Any]]) -> list[list[Any]]:
        rows[2][index] = value
        return rows

    rewrite_rows(worker.journal.path, blank)
    with refuses(code):
        worker.journal.arm(rp.run_id_for(env.manifest, env.arm("baseline")))
    with refuses(code):
        ag.rederive(worker.journal.path, env.manifest)
    mac = "hmac-sha256:" + "a" * 64
    row = rp.ScoredRow("r", rp.Verdict.CORRECT_ANSWER, True, "d", "s", "r1", mac)
    with refuses("verdict_unattributed"):
        replace(row, scorer_version="")
    with refuses("verdict_uncommitted"):
        replace(row, commitment="not-a-commitment")


def test_a4_a_row_attributed_to_another_scorer_or_rubric_is_rejected_not_accepted(
    env: Env,
) -> None:
    for index, value in ((4, "generated-scorer-v2"), (5, "generated-rubric-r9")):
        e = Env(env.root / f"attr-{index}")
        worker = e.worker()
        arms = e.baseline_and_simple(worker)
        result = e.result(arms, journal=worker.journal)

        def relabel(arm: str, rows: list[list[Any]], i: int = index, v: str = value) -> Any:
            rows[0][i] = v
            return rows

        rewrite_rows(worker.journal.path, relabel)
        # the row still parses: attribution is present, so presence alone is not the check
        assert worker.journal.arm(rp.run_id_for(e.manifest, e.arm("baseline"))) is not None
        with refuses("verdict_attribution_mismatch"):
            e.result(arms, journal=worker.journal)
        with refuses("verdict_attribution_mismatch"):
            e.replay(e.arm("baseline"), ScriptedInference(BASE, BASELINE), worker=worker)
        with refuses("verdict_attribution_mismatch"):
            ag.rederive(worker.journal.path, e.manifest)
        decision = rp.stage_decision(
            result, recomputed_digest=result["result_digest"], **e.staging(worker.journal)
        )
        assert decision.reasons == ("rebuild_refused_verdict_attribution_mismatch",)


# ---------------------------------------------------------------- G separate aggregator
def test_g1_aggregator_rederives_the_scorecard_totals_from_journaled_verdicts(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    derived = ag.rederive(worker.journal.path, env.manifest)
    assert derived["basis"] == "aggregation re-derived from journaled verdicts"
    assert derived["verdicts_rechecked"] is False
    assert derived["scorer"] == {
        "version": "generated-scorer-v1",
        "rubric_revision": "generated-rubric-r1",
    }
    by_arm = {a["arm_id"]: a for a in derived["arms"].values()}
    body = result["body"]
    for name, arm_body in body["arms"].items():
        for field, value in arm_body.items():
            assert by_arm[name]["totals"][field] == value, (name, field)
    check = ag.reconcile(result, journal_path=worker.journal.path, manifest=env.manifest)
    assert check.consistent and check.mismatches == ()
    assert check.basis == ag.BASIS and check.verdicts_rechecked is False


def test_g2_aggregator_catches_an_aggregation_bug_in_the_replay_module(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_arm, real_paired = rp._arm_body, rp._paired  # noqa: SLF001

    def inflated(manifest: rp.ReplayManifest, record: rp.ArmRecord) -> dict[str, Any]:
        body = real_arm(manifest, record)
        body["correct"] += 1
        return body

    def flattering(control: rp.ArmRecord, treatment: rp.ArmRecord) -> dict[str, Any] | None:
        pair = real_paired(control, treatment)
        if pair is not None:
            pair["regressions"] = 0
            pair["fixes"] += 1
        return pair

    monkeypatch.setattr(rp, "_arm_body", inflated)
    monkeypatch.setattr(rp, "_paired", flattering)
    worker = env.worker()
    result = full_result(env, worker)
    assert result["result_digest"] == task.digest(result["body"])  # self-consistent, still wrong
    check = ag.reconcile(result, journal_path=worker.journal.path, manifest=env.manifest)
    assert not check.consistent
    assert {"arms.baseline.correct", "arms.candidate.correct", "paired_comparison.fixes"} <= set(
        check.mismatches
    )


def test_g3_aggregator_catches_journal_tampering_after_the_result(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)

    def demote(arm: str, rows: list[list[Any]]) -> list[list[Any]]:
        if arm == "candidate":
            rows[0][1] = rp.Verdict.SERIOUS_ERROR.value
        return rows

    rewrite_rows(worker.journal.path, demote)
    check = ag.reconcile(result, journal_path=worker.journal.path, manifest=env.manifest)
    assert not check.consistent
    assert "arms.candidate.correct" in check.mismatches
    assert "case_outcomes_redacted.candidate" in check.mismatches
    decision = rp.stage_decision(
        result, recomputed_digest=result["result_digest"], **env.staging(worker.journal)
    )
    assert not decision.stageable and "recompute_mismatch" in decision.reasons


def test_g4_aggregator_does_not_trust_a_total_field_in_an_edited_redigested_body(
    env: Env,
) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    arms = {**result["body"]["arms"]}
    arms["baseline"] = {**arms["baseline"], "serious_errors": 0}
    body = {**result["body"], "arms": arms}
    forged = {"body": body, "result_digest": task.digest(body)}
    check = ag.reconcile(forged, journal_path=worker.journal.path, manifest=env.manifest)
    assert check.mismatches == ("arms.baseline.serious_errors",)
    dropped = {**result["body"]["arms"]}
    del dropped["simple_repair"]
    body = {**result["body"], "arms": dropped}
    check = ag.reconcile(
        {"body": body, "result_digest": task.digest(body)},
        journal_path=worker.journal.path,
        manifest=env.manifest,
    )
    assert "arms.simple_repair.absent" in check.mismatches
    unsigned = {"body": result["body"], "result_digest": "sha256:" + "0" * 64}
    check = ag.reconcile(unsigned, journal_path=worker.journal.path, manifest=env.manifest)
    assert check.mismatches == ("result_digest",)


def test_g5_aggregator_refuses_an_arm_event_that_does_not_match_its_run(env: Env) -> None:
    worker, _ = baseline_run(env)
    lines = []
    for line in worker.journal.path.read_text().splitlines():
        event = json.loads(line)
        event["record"]["spec"][1] = "f" * 40  # claim another served revision, same run id
        lines.append(json.dumps(event))
    worker.journal.path.write_text("\n".join(lines) + "\n")
    with refuses("journal_arm_run_mismatch"):
        ag.rederive(worker.journal.path, env.manifest)


def test_g6_nothing_the_aggregator_emits_claims_independence(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    derived = ag.rederive(worker.journal.path, env.manifest)
    check = ag.reconcile(result, journal_path=worker.journal.path, manifest=env.manifest)
    emitted = (task.canonical(derived).decode() + repr(check) + ag.BASIS).casefold()
    for phrase in ("independent", "recomputable", "evaluator outcome"):
        assert phrase not in emitted, phrase


# ---------------------------------------------------------------- S stage_decision
def test_s1_a_redigested_forged_claim_is_not_stageable_whatever_digest_the_caller_supplies(
    env: Env,
) -> None:
    arms = env.baseline_and_simple()
    infer = env.candidate_inference(SIMPLE)
    arms["candidate"] = env.replay(env.arm("candidate", infer.served_revision), infer)
    result = env.result(arms)
    assert result["body"]["improvement_claim"] == "no_net_fixes"
    body = {**result["body"], "improvement_claim": "observed_net_fixes"}
    forged = {"body": body, "result_digest": task.digest(body)}
    # Before OPE-573 this was stageable: the caller's digest stood in for a recompute.
    decision = rp.stage_decision(forged, recomputed_digest=forged["result_digest"], **env.staging())
    assert not decision.stageable
    assert "recompute_mismatch" in decision.reasons
    assert "improvement_no_net_fixes" in decision.reasons


def test_s2_the_body_selfcheck_flag_is_not_trusted(env: Env) -> None:
    class AlwaysCorrect:
        version = "generated-scorer-v1"
        rubric_revision = "generated-rubric-r1"

        def score(self, case_, expected, out) -> rp.Verdict:  # type: ignore[no-untyped-def]
            return rp.Verdict.CORRECT_ANSWER

    worker = env.worker()
    result = full_result(env, worker)
    body = {**result["body"], "scorer": {**result["body"]["scorer"], "selfcheck_passed": True}}
    claimed = {"body": body, "result_digest": task.digest(body)}
    decision = rp.stage_decision(
        claimed,
        recomputed_digest=claimed["result_digest"],
        **env.staging(worker.journal, AlwaysCorrect()),
    )
    assert not decision.stageable and "scorer_selfcheck_failed" in decision.reasons


def test_s3_a_result_that_omits_a_journaled_arm_is_not_stageable(env: Env) -> None:
    worker = env.worker()
    full_result(env, worker)
    records = {r.spec.arm_id: r for r in worker.journal.arms_for(env.manifest.digest)}
    picked = env.result(
        {"baseline": records["baseline"], "candidate": records["candidate"]},
        journal=worker.journal,
    )
    assert picked["body"]["improvement_claim"] == "observed_net_fixes"
    decision = rp.stage_decision(
        picked, recomputed_digest=picked["result_digest"], **env.staging(worker.journal)
    )
    assert not decision.stageable and decision.reasons == ("recompute_mismatch",)


def test_s4_two_journaled_runs_of_one_arm_are_ambiguous_and_not_stageable(env: Env) -> None:
    worker = env.worker()
    result = full_result(env, worker)
    infer = env.candidate_inference()
    env.replay(
        env.arm("candidate", infer.served_revision, context_revision="generated-context-2"),
        infer,
        worker=worker,
    )
    decision = rp.stage_decision(
        result, recomputed_digest=result["result_digest"], **env.staging(worker.journal)
    )
    assert not decision.stageable and decision.reasons == ("rebuild_refused_arm_ambiguous",)


def test_s5_a_journal_holding_no_arm_for_the_manifest_cannot_stage_a_result(env: Env) -> None:
    result = full_result(env)
    decision = rp.stage_decision(
        result,
        recomputed_digest=result["result_digest"],
        **env.staging(rp.ReplayJournal(env.root / "empty.jsonl")),
    )
    assert not decision.stageable
    assert decision.reasons == ("rebuild_refused_arm_not_journaled",)
