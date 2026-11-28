"""OPE-563 product test: the registered EVE task through the worker's normal entry point.

Fresh Python processes run the shipped modules exactly as an installed worker would:

* ``python -m opentour_cell.pilot.eve_worker run`` registers the typed task, freezes the
  protected replay, replays baseline and simple repair, dispatches the guarded fit through the
  reservation and job contract to an isolated trainer transport, collects and lineage-checks the
  candidate, replays it and settles the versioned result;
* ``python -m opentour_cell.pilot.eve_evaluator evaluate`` is the separate scorer process. It
  runs under its own identity and custody, replays every arm itself, checks every prediction
  against the worker's keyed commitments, re-scores and recounts;
* ``python -m opentour_cell.pilot.eve_evaluator recompute`` re-scores from the predictions the
  evaluator holds privately, with no model call: the independently recomputable scorecard;
* ``python -m opentour_cell.pilot.eve_worker stage`` is the one staging gate: worker rebuild,
  aggregator reconcile and independent verdict together.

Acceptance IDs are this file's own (OPE-563 registers none): P1 eligible run, exact candidate and
independently recomputable scorecard through normal entry; P2 zero fit on an inference-only
target, stale lineage or protected overlap; P3 protected answers never reach the model or fit
payloads or any output; P4 unverifiable claims never count as correct; P5 a broken or substituted
scorecard cannot stage; P6 retry and restart record one result and one fit; P7 the entry refuses
unsafe configuration without printing internals.

Fixture kind: generated sentinel data only, a scripted generated reference backend and an isolated
file-backed trainer transport. No real EVE model, GPU, trainer, provider, Slack or customer data.
The OPE-558 public QLoRA smoke and the OPE-561 Slack-to-GPU run are separate evidence, not
reproduced here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import eve_evaluator as ev
from opentour_cell.pilot import eve_reference_backend as rb
from opentour_cell.pilot import eve_task as task
from opentour_cell.pilot import eve_worker as wk
from test_eve_replay import BASE, BASELINE, BENCH, PROTECTED, SENTINEL, SIMPLE, TUNED, answer
from test_eve_replay import registration as eve_registration

FAR = 4_102_444_800  # 2100-01-01, a generated grant horizon
BOUND = ["USD", 0, 60, 16, 100]
ARM_SETTINGS = {
    "baseline": ["generated-snapshot-1", "generated-retrieval-1", "generated-context-plain"],
    "simple_repair": ["generated-snapshot-1", "generated-retrieval-1", "generated-context-repair"],
    "candidate": ["generated-snapshot-1", "generated-retrieval-1", "generated-context-repair"],
}
EVALUATOR_ID = "generated-independent-evaluator"


def _private(path: Path, value: Any) -> Path:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True))
    os.chmod(path, 0o600)
    return path


def _out(n: int, kind: str) -> dict[str, Any] | None:
    if kind == "correct":
        return {"answer": answer(n), "citations": [f"p{n}-a"], "abstained": False}
    if kind == "wrong":
        return {"answer": "generated-wrong-claim", "citations": [f"p{n}-a"], "abstained": False}
    if kind == "unverifiable":
        return {
            "answer": answer(n),
            "citations": ["generated-invented-passage"],
            "abstained": False,
        }
    if kind == "absent":
        return None
    return {"answer": None, "citations": [], "abstained": True}


def _script(kinds: dict[int, str]) -> dict[str, Any]:
    return {f"generated-p{n}": _out(n, kind) for n, kind in kinds.items()}


def _case_doc(case: task.EveCase) -> dict[str, Any]:
    return {
        "case_id": case.case_id,
        "family_id": case.family_id,
        "question": case.question,
        "context": [[p.passage_id, p.text] for p in case.context],
    }


def _answers() -> dict[str, Any]:
    entries = {}
    for n in range(1, 7):
        expectation = {4: "abstain", 6: "disputed"}.get(n, "answer")
        entries[f"generated-p{n}"] = {
            "expectation": expectation,
            "answer": "" if expectation == "abstain" else answer(n),
            "required_citations": [f"p{n}-a"] if expectation == "answer" else [],
        }
    return entries


def _development(*, overlap: bool = False) -> list[dict[str, Any]]:
    rows = []
    for n in range(1, 4):
        family = "generated-fam1" if overlap and n == 1 else f"generated-devfam{n}"
        rows.append(
            {
                "case_id": f"generated-d{n}",
                "family_id": family,
                "question": f"dev question {n}?",
                "context": [[f"d{n}-a", f"dev passage {n}"]],
                "answer": f"generated-dev-answer-{n}",
            }
        )
    return rows


class Installed:
    """A generated custodian's provisioning of one worker and one separate evaluator."""

    def __init__(
        self,
        root: Path,
        *,
        train: bool = True,
        overlap: bool = False,
        base_revision: str = BASE,
        tuned: dict[int, str] = TUNED,
        fit_state: str = "completed",
        inference_destination: str | None = None,
    ) -> None:
        self.root = root
        self.worker_dir = root / "worker"
        self.keys_dir = root / "worker-commitment-keys"
        self.backend_dir = root / "reference-backend"
        self.custody = root / "evaluator-custody"
        self.exchange = root / "exchange"
        for d in (self.worker_dir, self.keys_dir, self.backend_dir, self.exchange):
            d.mkdir(mode=0o700)
        self.registration = eve_registration(train=train)
        if inference_destination is not None:
            self.registration = replace(
                self.registration, inference_destination=inference_destination
            )
        cases = [_case_doc(c) for c in PROTECTED]
        self.inputs = _private(
            root / "worker-inputs" / "inputs.json",
            {
                "schema_version": wk.INPUTS,
                "cases": cases,
                "answers": _answers(),
                "development": _development(overlap=overlap),
            },
        )
        self.behaviour = _private(
            self.backend_dir / "behaviour.json",
            {
                "schema_version": rb.BEHAVIOUR,
                "base_revision": base_revision,
                "training_supported": True,
                "fit_state": fit_state,
                "estimate": ["USD", 0, 30, 8, 50],
                "base": {
                    "generated-context-plain": _script(BASELINE),
                    "generated-context-repair": _script(SIMPLE),
                },
                "adapter": _script(tuned),
            },
        )
        grants = [
            self._grant("baseline_inference", "baseline_inference", None, []),
            self._grant("training", "training", BOUND, [BENCH]),
        ]
        self.config = _private(
            self.worker_dir / "eve-worker.json",
            {
                "schema_version": wk.CONFIG,
                "tenant": "generated-tenant",
                "region": "simulated:region",
                "credential_ref": "simulated:credential-ref",
                "available_credentials": ["simulated:credential-ref"],
                "registration": task.registration_document(self.registration),
                "benchmark_id": "generated-benchmark",
                "benchmark_revision": BENCH,
                "scorer": [
                    "generated:opentour-eve-deterministic-stand-in",
                    "generated-scorer-v1",
                    "generated-rubric-r1",
                ],
                "arms": ARM_SETTINGS,
                "grants": grants,
                "fit": {
                    "grant_revision": "grant-v1",
                    "idempotency_key": "generated-fit-intent-1",
                    "bound": BOUND,
                },
                "inputs": str(self.inputs),
                "keys_dir": str(self.keys_dir),
                "backend_dir": str(self.backend_dir),
            },
        )
        sealed = _private(
            self.custody / "sealed-inputs.json",
            {
                "schema_version": ev.SEALED_INPUTS,
                "registration": task.registration_document(self.registration),
                "cases": cases,
                "answers": _answers(),
            },
        )
        outbox = self.worker_dir / wk.OUTBOX
        self.evaluator_config = _private(
            root / "evaluator-config" / "evaluator.json",
            {
                "schema_version": ev.CONFIG,
                "evaluator_id": EVALUATOR_ID,
                "rubric_revision": "generated-rubric-r1",
                "custody_dir": str(self.custody),
                "sealed_inputs": str(sealed),
                "worker": {
                    "manifest": str(outbox / "manifest.json"),
                    "result": str(outbox / "result.json"),
                    "fit": str(outbox / "fit.json"),
                    "journal": str(self.worker_dir / wk.JOURNAL),
                    "keys": str(self.keys_dir / wk.KEYS),
                    "backend_dir": str(self.backend_dir),
                    "data_dirs": [str(self.worker_dir), str(self.keys_dir)],
                },
                "export_dir": str(self.exchange),
            },
        )

    @staticmethod
    def _grant(actor: str, purpose: str, bound: Any, revisions: list[str]) -> dict[str, Any]:
        return {
            "actor": actor,
            "purpose": purpose,
            "revision": "grant-v1",
            "expires_at": FAR,
            "retention_until": FAR,
            "permitted_fields": sorted(task.ROW_FIELDS),
            "fit_bound": bound,
            "benchmark_revisions": revisions,
        }

    def edit_config(self, edit: Callable[[dict[str, Any]], None]) -> None:
        value = json.loads(self.config.read_text())
        edit(value)
        _private(self.config, value)

    def edit_behaviour(self, edit: Callable[[dict[str, Any]], None]) -> None:
        value = json.loads(self.behaviour.read_text())
        edit(value)
        _private(self.behaviour, value)

    @property
    def verdict(self) -> Path:
        return self.exchange / ev.EXPORT_NAME

    def outbox(self, name: str) -> Any:
        return json.loads((self.worker_dir / wk.OUTBOX / name).read_text())

    def executions(self) -> int:
        return len(list((self.backend_dir / "jobs").glob("*.json")))


def shipped(module: str, *args: str) -> tuple[int, dict[str, Any], str]:
    """Run a shipped module in a fresh interpreter; return exit code, parsed output, stderr."""
    done = subprocess.run(
        [sys.executable, "-m", f"opentour_cell.pilot.{module}", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    lines = done.stdout.strip().splitlines()
    assert len(lines) == 1, (done.stdout, done.stderr)
    return done.returncode, json.loads(lines[0]), done.stdout + done.stderr


def run_worker(installed: Installed) -> tuple[int, dict[str, Any], str]:
    return shipped("eve_worker", "run", "--config", str(installed.config))


def evaluate(installed: Installed) -> tuple[int, dict[str, Any], str]:
    return shipped("eve_evaluator", "evaluate", "--config", str(installed.evaluator_config))


def stage(installed: Installed) -> tuple[int, dict[str, Any], str]:
    return shipped(
        "eve_worker",
        "stage",
        "--config",
        str(installed.config),
        "--verdict",
        str(installed.verdict),
    )


@pytest.fixture
def installed(tmp_path: Path) -> Installed:
    return Installed(tmp_path)


# ---------------------------------------------------------------- P1 normal entry, end to end
def test_p1_registered_task_runs_through_normal_entry_to_a_recomputable_stageable_scorecard(
    installed: Installed,
) -> None:
    code, run, _ = run_worker(installed)
    assert code == 0 and run["status"] == "completed", run
    assert run["arms"] == ["baseline", "candidate", "simple_repair"]
    assert run["fit"] == {"state": "collected", "reason": None, "executions": 1}
    assert run["improvement_claim"] == "observed_net_fixes"
    assert run["fit_authorized"] is False and run["release_authorized"] is False
    assert installed.executions() == 1
    # the exact candidate: the collected adapter is the one the arm replayed
    fit = installed.outbox("fit.json")
    result = installed.outbox("result.json")
    artifact = fit["candidate"][2]
    assert artifact.startswith("generated-eve-adapter-")
    assert result["body"]["arms"]["candidate"]["served_revision"] == artifact
    assert result["body"]["arms"]["candidate"]["correct"] == 5
    assert result["body"]["arms"]["baseline"]["correct"] == 1
    assert result["body"]["causal_tuning_comparison"]["available"] is True

    code, verdict, _ = evaluate(installed)
    assert code == 0 and verdict["status"] == "reproduced", verdict
    assert verdict["manifest_digest"] == run["manifest_digest"]
    assert verdict["result_digest"] == run["result_digest"]
    assert verdict["outputs_committed"] == 15 and verdict["verdict_disagreements"] == 0
    document = json.loads(installed.verdict.read_text())
    assert document["evaluator_id"] == EVALUATOR_ID
    assert document["scorer"]["scorer_id"] != document["worker_scorer"]["scorer_id"]
    assert document["custody_separated"] is False and document["export_tier"] == ev.EXPORT_TIER

    code, again, _ = shipped(
        "eve_evaluator",
        "recompute",
        "--config",
        str(installed.evaluator_config),
        "--verdict-key",
        verdict["verdict_key"],
    )
    assert code == 0 and again["status"] == "recomputed" and again["matches"] is True, again
    assert again["improvement_claim"] == "observed_net_fixes"

    code, staged, _ = stage(installed)
    assert code == 0 and staged["status"] == "stageable", staged
    assert staged["reasons"] == [] and staged["release_authorized"] is False


def test_p1_staging_without_an_independent_verdict_is_held(installed: Installed) -> None:
    assert run_worker(installed)[0] == 0
    code, staged, _ = stage(installed)
    assert code == 2 and staged["status"] == "held"
    assert "independent_verdict_missing" in staged["reasons"]


# ---------------------------------------------------------------- P2 zero fit
def test_p2_inference_only_target_returns_baseline_scores_with_zero_fits(tmp_path: Path) -> None:
    installed = Installed(tmp_path, train=False)
    installed.edit_config(lambda c: c.update(fit=None))
    code, run, _ = run_worker(installed)
    assert code == 0, run
    zero = {"state": "zero_fit", "reason": "fit_unavailable_inference_only", "executions": 0}
    assert run["fit"] == zero
    assert run["arms"] == ["baseline", "simple_repair"] and installed.executions() == 0
    assert not (installed.worker_dir / wk.OUTBOX / "fit.json").exists()
    code, verdict, _ = evaluate(installed)
    assert code == 0 and verdict["status"] == "reproduced", verdict
    # the simple repair fixes the abstention case 4 and regresses nothing: a useful repair with
    # no fit is its own stageable proposal, scored against the baseline
    assert run["improvement_claim"] == "observed_net_fixes"
    code, staged, _ = stage(installed)
    assert code == 0 and staged["status"] == "stageable", staged


def test_p2_protected_overlap_is_a_zero_fit(tmp_path: Path) -> None:
    installed = Installed(tmp_path, overlap=True)
    code, run, _ = run_worker(installed)
    assert code == 0, run
    assert run["fit"] == {"state": "zero_fit", "reason": "protected_overlap", "executions": 0}
    assert installed.executions() == 0


def test_p2_stale_or_wrong_lineage_runs_nothing(tmp_path: Path) -> None:
    installed = Installed(tmp_path, base_revision="f" * 40)  # the backend serves another revision
    code, run, _ = run_worker(installed)
    assert code == 2 and run == {
        "schema_version": wk.RESULT,
        "status": "held",
        "reason": "exact_model_revision_unverified",
    }
    assert installed.executions() == 0
    assert not (installed.worker_dir / wk.OUTBOX / "result.json").exists()


def test_p2_a_dispatched_job_that_is_still_running_is_never_a_candidate(tmp_path: Path) -> None:
    installed = Installed(tmp_path, fit_state="running")
    code, run, _ = run_worker(installed)
    assert code == 0, run
    # a pending request is not an admitted job: it is reported, not collected or replayed
    assert run["fit"] == {"state": "running", "reason": "fit_not_completed", "executions": 1}
    assert run["arms"] == ["baseline", "simple_repair"]
    assert not (installed.worker_dir / wk.OUTBOX / "fit.json").exists()


def test_p2_a_withdrawn_training_grant_is_a_zero_fit(installed: Installed) -> None:
    def withdraw(config: dict[str, Any]) -> None:
        config["grants"] = [g for g in config["grants"] if g["actor"] != "training"]

    installed.edit_config(withdraw)
    code, run, _ = run_worker(installed)
    assert code == 0 and run["fit"]["state"] == "zero_fit", run
    assert run["fit"]["reason"] == "model_capability_unavailable"
    assert installed.executions() == 0


def test_p2_a_candidate_swapped_after_the_run_fails_lineage_at_the_evaluator(
    installed: Installed,
) -> None:
    assert run_worker(installed)[0] == 0
    fit_path = installed.worker_dir / wk.OUTBOX / "fit.json"
    fit = json.loads(fit_path.read_text())
    fit["candidate"][2] = "generated-eve-adapter-" + "0" * 40
    _private(fit_path, fit)
    code, verdict, _ = evaluate(installed)
    assert code == 2 and verdict["status"] == "integrity_failure"
    assert "candidate_lineage_unverified_candidate" in verdict["reasons"]
    code, staged, _ = stage(installed)
    assert code == 2 and "independent_verdict_integrity_failure" in staged["reasons"]


# ---------------------------------------------------------------- P3 protected answers
def test_p3_protected_answers_never_reach_the_model_side_or_any_output(
    installed: Installed,
) -> None:
    code, run, run_text = run_worker(installed)
    assert code == 0
    evaluate_text = evaluate(installed)[2]
    stage_text = stage(installed)[2]
    received = (installed.backend_dir / "received.jsonl").read_text()
    payloads = "".join(p.read_text() for p in (installed.backend_dir / "payloads").glob("*.json"))
    assert payloads and "generated-dev-answer-1" in payloads  # not vacuous: the fit payload
    kinds = {json.loads(line)["kind"] for line in received.splitlines()}
    assert kinds == {"base_replay", "fit_payload", "candidate_replay"}
    for text in (received, payloads, run_text, evaluate_text, stage_text):
        assert SENTINEL not in text
    outbox = installed.worker_dir / wk.OUTBOX
    surfaces = (installed.worker_dir / wk.JOURNAL, outbox / "result.json", outbox / "fit.json")
    for path in (*surfaces, installed.verdict):
        assert SENTINEL not in path.read_text(), path
    # the evaluator's private predictions do hold candidate outputs, under its own custody only
    private = "".join(p.read_text() for p in (installed.custody / "predictions").glob("*.json"))
    assert SENTINEL in private


# ---------------------------------------------------------------- P4 unverifiable claims
def test_p4_an_unverifiable_claim_never_counts_as_correct(installed: Installed) -> None:
    assert run_worker(installed)[0] == 0
    baseline = installed.outbox("result.json")["body"]["arms"]["baseline"]
    # case 3 carries the right text but cites an invented passage
    assert baseline["unverified_answered"] == 1 and baseline["correct_answer"] == 1
    code, verdict, _ = evaluate(installed)
    assert code == 0 and verdict["verdict_disagreements"] == 0


# ---------------------------------------------------------------- P5 broken scorecards
def test_p5_a_serving_difference_at_replay_is_an_integrity_failure(installed: Installed) -> None:
    assert run_worker(installed)[0] == 0
    # the served simple-repair model answers case 2 differently when the evaluator replays it
    installed.edit_behaviour(
        lambda b: b["base"]["generated-context-repair"].update({"generated-p2": _out(2, "correct")})
    )
    code, verdict, _ = evaluate(installed)
    assert code == 2 and verdict["status"] == "integrity_failure"
    assert "outputs_not_committed_simple_repair" in verdict["reasons"]
    assert "verdict_disagreement_simple_repair" in verdict["reasons"]
    code, staged, _ = stage(installed)
    assert code == 2 and staged["status"] == "held"


def test_p5_an_edited_journal_verdict_cannot_stage(installed: Installed) -> None:
    assert run_worker(installed)[0] == 0
    journal = installed.worker_dir / wk.JOURNAL
    lines = []
    for line in journal.read_text().splitlines():
        event = json.loads(line)
        if event["event"] == "arm" and event["record"]["spec"][0] == "baseline":
            event["record"]["rows"][1][1] = "correct_answer"  # case 2 was a serious error
        lines.append(json.dumps(event, sort_keys=True, separators=(",", ":")))
    journal.write_text("\n".join(lines) + "\n")
    code, verdict, _ = evaluate(installed)
    assert code == 2 and "outputs_not_committed_baseline" in verdict["reasons"]
    code, staged, _ = stage(installed)
    assert code == 2
    assert (
        "recompute_mismatch" in staged["reasons"] and "aggregate_inconsistent" in staged["reasons"]
    )


def test_p5_a_forged_verdict_document_inconsistent_with_the_journal_cannot_stage(
    installed: Installed,
) -> None:
    assert run_worker(installed)[0] == 0
    assert evaluate(installed)[0] == 0
    document = json.loads(installed.verdict.read_text())
    document["arms"]["candidate"]["totals"]["correct"] = 4
    body = {k: v for k, v in document.items() if k != "verdict_digest"}
    document["verdict_digest"] = task.digest(body)  # re-digested, so only the recount catches it
    _private(installed.verdict, document)
    code, staged, _ = stage(installed)
    assert code == 2 and "independent_recount_mismatch" in staged["reasons"]


# ---------------------------------------------------------------- P6 retry and restart
def test_p6_a_second_run_records_one_result_and_one_fit(installed: Installed) -> None:
    first = run_worker(installed)
    second = run_worker(installed)
    assert first[0] == second[0] == 0
    assert first[1]["result_digest"] == second[1]["result_digest"]
    assert first[1]["settlement"] == "recorded" and second[1]["settlement"] == "duplicate_ignored"
    assert second[1]["fit"]["executions"] == 0 and installed.executions() == 1
    one, two = evaluate(installed), evaluate(installed)
    assert one[1]["verdict_key"] == two[1]["verdict_key"] and one[1]["status"] == "reproduced"


# ---------------------------------------------------------------- P7 unsafe configuration
def test_p7_unsafe_configuration_is_refused_without_printing_internals(
    installed: Installed,
) -> None:
    os.chmod(installed.config, 0o644)
    code, held, text = run_worker(installed)
    assert code == 2 and held["reason"] == "invalid_worker_configuration"
    assert "Traceback" not in text and SENTINEL not in text
    os.chmod(installed.config, 0o600)
    installed.edit_config(lambda c: c.update(keys_dir=str(installed.worker_dir / "keys")))
    code, held, _ = run_worker(installed)
    assert code == 2 and held["reason"] == "commitment_key_colocated"
    code, held, _ = shipped("eve_worker", "run")
    assert code == 2 and held["reason"] == "invalid_worker_arguments"
    code, held, _ = shipped("eve_evaluator", "evaluate", "--config", "relative.json")
    assert code == 2 and held["reason"] == "invalid_evaluator_configuration"


def test_p7_the_reference_backend_never_stands_in_for_a_real_destination(tmp_path: Path) -> None:
    installed = Installed(tmp_path, inference_destination="eve-inference.example.invalid")
    code, held, _ = run_worker(installed)
    assert code == 2 and held["reason"] == "model_not_qualified", held
    assert not (installed.backend_dir / "received.jsonl").exists() and installed.executions() == 0


def test_p7_evaluator_custody_inside_worker_data_is_refused(installed: Installed) -> None:
    assert run_worker(installed)[0] == 0
    config = json.loads(installed.evaluator_config.read_text())
    config["worker"]["data_dirs"].append(str(installed.root))  # custody lies under this
    _private(installed.evaluator_config, config)
    code, held, _ = evaluate(installed)
    assert code == 2 and held["reason"] == "evaluator_custody_overlaps_worker_data"
