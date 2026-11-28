"""Generated-sentinel checks of the EVE benchmark harness. They prove software behaviour only,
never EVE capacity, Pi School results, provider authority or customer evidence."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from opentour_training.evaluation import eve_benchmark as eb
from opentour_training.evaluation import eve_fixtures as fx

ROOT = Path(__file__).resolve().parents[2]
ENV = {"PYTHONPATH": str(ROOT / "src")}
CLI = [sys.executable, "-I", "-m", "opentour_training.evaluation.eve_fixtures", "run-generated"]


def _setup(include_repair: bool = True) -> Any:
    f = fx.build_fixture_set()
    m = eb.freeze_manifest(
        benchmark_id="generated-eve-test",
        evidence_tier="generated",
        development_cases=f.development_cases,
        heldout_cases=f.heldout_cases,
        heldout_exclusions=f.exclusions,
        heldout_key=f.heldout_key,
        development_key=f.development_key,
        arms=fx.ARM_CONFIGS,
        rubric_version="generated-rubric-1",
        rubric_text=fx.RUBRIC_TEXT,
        alpha=0.1,
        materiality_min_errors=2,
        min_recurring_families=2,
        adjudicator_kind="generated_fixture",
    )
    cfg = {c.arm_id: c for c in fx.ARM_CONFIGS}
    elig = tuple(c for c in f.heldout_cases if c.case_id in m.heldout_eligible)
    runs = [fx.simulate_arm(cfg[eb.BASELINE_ARM], elig, m)]
    if include_repair:
        runs.append(fx.simulate_arm(cfg[eb.REPAIR_ARM], elig, m))
    iso = eb.verify_isolation(
        m, fit_rows=[], prompt_texts=_prompts(f, m), heldout_key=f.heldout_key
    )
    return f, m, cfg, elig, runs, iso


def _prompts(f: Any, m: eb.FrozenManifest) -> list[str]:
    return [c.question for c in f.heldout_cases if c.case_id in m.heldout_eligible]


def _score_with(
    m: eb.FrozenManifest,
    f: Any,
    *,
    heldout_key: Any,
    isolation: eb.IsolationReceipt,
    runs: Any,
    fit_rows: Any = (),
    heldout_cases: Any = None,
) -> eb.Scorecard:
    return eb.score_heldout(
        m,
        heldout_cases=f.heldout_cases if heldout_cases is None else heldout_cases,
        heldout_key=heldout_key,
        fit_rows=list(fit_rows),
        prompt_texts=_prompts(f, m),
        isolation=isolation,
        runs=runs,
    )


def _score(include_repair: bool = True) -> eb.Scorecard:
    f, m, _, _, runs, iso = _setup(include_repair)
    return _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=runs)


def _code(fn: Callable[[], object]) -> str:
    with pytest.raises(eb.BenchmarkError) as exc:
        fn()
    return exc.value.code


def _dispose(card: eb.Scorecard, finding: Any, name: str) -> dict[str, Any]:
    return eb.record_disposition(
        card, finding, disposition=name, owner_id="generated-owner-1", recorded_at="2026-01-01"
    )


# ---- freeze before candidate output


def test_freeze_records_disjoint_splits_exclusions_and_seals_key() -> None:
    f, m, *_ = _setup()
    h = m.body["heldout"]
    assert not set(m.body["development"]["family_ids"]) & set(h["family_ids"])
    assert h["excluded"] == [["HOLD-011", "context_not_authorized"]]
    assert len(h["eligible_case_ids"]) == len(h["case_ids"]) - 1
    assert h["answer_key_digest"] == eb.reference_key_digest(f.heldout_key)
    assert m.body["state"] == "frozen_before_candidate_output"


def test_scoring_before_freeze_is_refused() -> None:
    f, m, _, _, runs, iso = _setup()
    thawed = eb.FrozenManifest(body={**m.body, "state": "draft"}, manifest_digest=m.manifest_digest)
    code = _code(
        lambda: _score_with(thawed, f, heldout_key=f.heldout_key, isolation=iso, runs=runs)
    )
    assert code == "manifest_not_frozen_or_tampered"


def test_outputs_not_bound_to_frozen_manifest_are_refused() -> None:
    f, m, _, _, runs, iso = _setup()
    stray = dataclasses.replace(runs[0], manifest_digest="sha256:" + "0" * 64)
    code = _code(
        lambda: _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=[stray, runs[1]])
    )
    assert code == "run_not_bound_to_frozen_manifest"


def test_freeze_rejects_split_overlap_and_misaligned_key() -> None:
    f = fx.build_fixture_set()
    common: dict[str, Any] = {
        "benchmark_id": "b",
        "evidence_tier": "generated",
        "arms": fx.ARM_CONFIGS,
        "rubric_version": "r",
        "rubric_text": "t",
        "alpha": 0.1,
        "materiality_min_errors": 1,
        "min_recurring_families": 1,
        "adjudicator_kind": "generated_fixture",
        "development_cases": f.development_cases,
        "heldout_exclusions": (),
        "development_key": f.development_key,
    }
    clash = dataclasses.replace(f.heldout_cases[0], family_id=f.development_cases[0].family_id)
    held = (clash, *f.heldout_cases[1:])
    assert (
        _code(lambda: eb.freeze_manifest(heldout_cases=held, heldout_key=f.heldout_key, **common))
        == "split_family_overlap"
    )
    assert (
        _code(
            lambda: eb.freeze_manifest(
                heldout_cases=f.heldout_cases, heldout_key=f.heldout_key[1:], **common
            )
        )
        == "key_not_aligned_to_heldout_cases"
    )


def test_changed_key_after_freeze_is_refused() -> None:
    f, m, _, _, runs, iso = _setup()
    bad = (dataclasses.replace(f.heldout_key[0], answer="ANS-GEN-CHANGED"), *f.heldout_key[1:])
    code = _code(lambda: _score_with(m, f, heldout_key=bad, isolation=iso, runs=runs))
    assert code == "answer_key_not_the_frozen_key"


# ---- leakage and protected answers


def test_heldout_case_in_fit_set_invalidates_scoring() -> None:
    f, m, _, _, runs, _ = _setup()
    row = eb.FitRow(f.heldout_cases[3].case_id, "GEN-FAM-OTHER", "generated row")
    iso = eb.verify_isolation(
        m, fit_rows=[row], prompt_texts=_prompts(f, m), heldout_key=f.heldout_key
    )
    assert not iso.clean
    code = _code(
        lambda: _score_with(
            m, f, heldout_key=f.heldout_key, isolation=iso, runs=runs, fit_rows=[row]
        )
    )
    assert code == "isolation_violation"


def test_heldout_family_and_protected_answer_in_prompt_or_fit_text_are_flagged() -> None:
    f, m, *_ = _setup()
    fam = f.heldout_cases[0].family_id
    iso = eb.verify_isolation(
        m,
        fit_rows=[
            eb.FitRow("DEV-000", fam, "x"),
            eb.FitRow("DEV-001", "GEN-FAM-DEV-00", "ANS-GEN-HOLD-004"),
        ],
        prompt_texts=["question with EV-HOLD-004 leaked"],
        heldout_key=f.heldout_key,
    )
    kinds = {v.split(":")[0] for v in iso.violations}
    assert kinds == {
        "fit_row_heldout_family",
        "fit_row_protected_answer",
        "prompt_protected_answer",
    }


def test_clean_isolation_counts_checked_rows() -> None:
    _, _, _, elig, _, iso = _setup()
    assert iso.clean and iso.checked_prompts == len(elig)


def test_isolation_receipt_from_another_manifest_is_refused() -> None:
    f, m, _, _, runs, iso = _setup()
    other = dataclasses.replace(iso, manifest_digest="sha256:" + "1" * 64)
    code = _code(lambda: _score_with(m, f, heldout_key=f.heldout_key, isolation=other, runs=runs))
    assert code == "isolation_not_bound_to_manifest"


def test_prepared_export_adapter_refuses_extra_reference_fields(tmp_path: Path) -> None:
    row = {
        "case_id": "HOLD-000",
        "answer": "a",
        "citations": ["c"],
        "abstained": False,
        "served_revision": "r",
        "source_snapshot_id": "s",
        "retrieval_settings_id": "t",
    }
    ok = tmp_path / "ok.jsonl"
    ok.write_text(json.dumps(row) + "\n")
    assert fx.load_prepared_export(ok, "baseline", "sha256:x").outputs[0].case_id == "HOLD-000"
    bad = tmp_path / "bad.jsonl"
    bad.write_text(json.dumps({**row, "reference_answer": "ANS"}) + "\n")
    assert _code(lambda: fx.load_prepared_export(bad, "baseline", "sha256:x")) == (
        "export_row_shape_invalid"
    )


# ---- grading and denominators


def test_serious_error_rules_and_unresolved_visible_outside_denominator() -> None:
    card = _score()
    b = card.body["arms"]["baseline"]
    assert b["eligible"] == 29 and b["unresolved"] == 1 and b["verified"] == 28
    assert b["coverage_answered_over_verified"] == [17, 28]
    assert b["serious_error_over_answered_verified"] == [5, 17]
    assert b["inappropriate_refusal"] == 6 and b["missing_output"] == 0
    assert card.body["heldout"]["unresolved"] == 1


def test_missing_output_counts_as_no_match_and_is_shown_separately() -> None:
    f, m, _, _, runs, iso = _setup()
    short = dataclasses.replace(runs[0], outputs=runs[0].outputs[1:])
    card = _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=[short, runs[1]])
    assert card.body["arms"]["baseline"]["missing_output"] == 1
    assert card.body["arms"]["baseline"]["eligible"] == 29


def test_unverifiable_citation_is_a_serious_error_not_correct() -> None:
    ref = eb.ReferenceJudgment("c", False, "A", ("E1",))
    out = eb.ArmOutput("c", "A", ("E-OTHER",), False, "r", "s", "t")
    assert eb.grade(ref, out) == "serious_error"
    assert eb.grade(ref, dataclasses.replace(out, citations=())) == "serious_error"
    assert eb.grade(dataclasses.replace(ref, disputed=True), out) == "unresolved"


def test_changed_denominator_blocks_improvement_claim() -> None:
    f, m, _, _, runs, iso = _setup()
    narrowed = dataclasses.replace(
        runs[1],
        declared_case_ids=runs[1].declared_case_ids[:-1],
        outputs=runs[1].outputs[:-1],
    )
    card = _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=[runs[0], narrowed])
    assert card.body["improvement_claim"] == "blocked_changed_denominator"
    assert card.body["paired_comparison"] is None
    assert card.body["denominators_identical_across_arms"] is False


def test_arm_attribution_change_is_refused() -> None:
    f, m, _, _, runs, iso = _setup()
    o = dataclasses.replace(runs[1].outputs[0], served_revision="gen-rev-2")
    moved = dataclasses.replace(runs[1], outputs=(o, *runs[1].outputs[1:]))
    code = _code(
        lambda: _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=[runs[0], moved])
    )
    assert code == "arm_attribution_mismatch"


# ---- baseline-only, missing arm, paired comparison


def test_baseline_only_run_has_no_improvement_claim() -> None:
    f = fx.build_fixture_set()
    m = eb.freeze_manifest(
        benchmark_id="generated-baseline-only",
        evidence_tier="generated",
        development_cases=f.development_cases,
        heldout_cases=f.heldout_cases,
        heldout_exclusions=f.exclusions,
        heldout_key=f.heldout_key,
        development_key=f.development_key,
        arms=fx.ARM_CONFIGS[:1],
        rubric_version="r",
        rubric_text="t",
        alpha=0.1,
        materiality_min_errors=1,
        min_recurring_families=1,
        adjudicator_kind="generated_fixture",
    )
    elig = tuple(c for c in f.heldout_cases if c.case_id in m.heldout_eligible)
    run = fx.simulate_arm(fx.ARM_CONFIGS[0], elig, m)
    iso = eb.verify_isolation(
        m, fit_rows=[], prompt_texts=_prompts(f, m), heldout_key=f.heldout_key
    )
    card = _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=[run])
    assert card.body["improvement_claim"] == "none_baseline_only"
    assert card.body["paired_comparison"] is None


def test_missing_frozen_arm_holds_rather_than_reporting_a_win() -> None:
    card = _score(include_repair=False)  # repair arm was frozen but its run is absent
    assert card.body["improvement_claim"] == "hold_missing_arm"
    assert card.body["paired_comparison"] is None


def test_baseline_run_is_required() -> None:
    f, m, _, _, runs, iso = _setup()
    code = _code(
        lambda: _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=[runs[1]])
    )
    assert code == "baseline_missing"


def test_paired_counts_fixes_regressions_and_interval_without_significance_claim() -> None:
    p = _score().body["paired_comparison"]
    assert (p["paired_gradeable"], p["fixes"], p["regressions"]) == (28, 11, 3)
    assert p["fixes"] + p["regressions"] + p["both_correct"] + p["both_wrong"] == 28
    assert p["interval"]["lower"] < 0 < p["interval"]["upper"]  # small sample: honestly wide
    assert "no significance claim" in p["interval"]["note"]
    assert len(p["fix_case_refs"]) == 11 and all(r.startswith("case:") for r in p["fix_case_refs"])


def test_redacted_scorecard_has_no_case_text_or_reference_answers() -> None:
    blob = json.dumps(_score().body)
    assert "generated question" not in blob
    assert "ANS-GEN" not in blob
    assert "HOLD-" not in blob and "DEV-" not in blob  # every case id, excluded ones too
    assert "context_not_authorized" in blob  # closed reason code only, no free text


def test_generated_tier_lists_every_unsatisfied_external_gate() -> None:
    body = _score().body
    assert body["claim_scope"] == "implementation_only"
    assert body["accuracy_claim_eligible"] is False
    for gate in eb.UNSATISFIED_EXTERNAL_GATES_GENERATED:
        assert gate in body["unsatisfied_external_gates"]
    assert "ground_truth_audit_receipt_absent" in body["unsatisfied_external_gates"]


# ---- determinism, replay, restart


def test_rescore_of_identical_inputs_reproduces_the_verdict() -> None:
    a, b = _score(), _score()
    assert a.scorecard_digest == b.scorecard_digest and a.body == b.body


def test_replay_in_fresh_process_reproduces_scorecard_digest(tmp_path: Path) -> None:
    digests = []
    for n in range(2):
        out = tmp_path / f"run{n}"
        proc = subprocess.run(
            [*CLI, "--out", str(out)], capture_output=True, text=True, check=False, env=ENV
        )
        assert proc.returncode == 0, proc.stderr
        digests.append(json.loads((out / "scorecard.json").read_text())["scorecard_digest"])
    assert digests[0] == digests[1]


def test_rerun_into_same_directory_is_idempotent(tmp_path: Path) -> None:
    for _ in range(2):  # restart after a completed run must not conflict
        proc = subprocess.run(
            [*CLI, "--out", str(tmp_path)], capture_output=True, text=True, check=False, env=ENV
        )
        assert proc.returncode == 0, proc.stderr


def test_manifest_round_trips_and_tamper_is_detected() -> None:
    _, m, *_ = _setup()
    raw = json.loads(json.dumps(eb.manifest_to_json(m)))
    assert eb.manifest_from_json(raw).manifest_digest == m.manifest_digest
    raw["body"]["alpha"] = 0.5
    assert _code(lambda: eb.manifest_from_json(raw)) == "manifest_not_frozen_or_tampered"


def test_write_once_is_idempotent_and_refuses_conflicting_rewrite(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    assert eb.write_once(p, {"a": 1}) is True
    assert eb.write_once(p, {"a": 1}) is False  # retry after a lost reply
    assert _code(lambda: eb.write_once(p, {"a": 2})) == "artifact_conflict"
    assert not list(tmp_path.glob(".tmp-*"))


# ---- development finding and disposition


def test_development_residual_decides_whether_tuning_may_be_considered() -> None:
    r = fx.run_fixture_benchmark()
    assert r.finding is not None
    assert (r.finding.baseline_serious, r.finding.repair_serious) == (4, 2)
    assert r.finding.material_recurring_residual is True


def test_disposition_rules() -> None:
    r = fx.run_fixture_benchmark()
    ok = _dispose(r.scorecard, r.finding, "consider_tuning")
    assert ok["fit_authorized"] is False and ok["scorecard_digest"] == r.scorecard.scorecard_digest
    assert _code(lambda: _dispose(r.scorecard, r.finding, "ship_it")) == "disposition_unknown"
    base = _score(include_repair=False)
    assert _code(lambda: _dispose(base, None, "adopt_test_simple_fix")) == (
        "adopt_requires_interval_supported_improvement"
    )
    assert _code(lambda: _dispose(base, None, "consider_tuning")) == (
        "tuning_requires_development_finding"
    )
    assert r.finding is not None
    weak = dataclasses.replace(r.finding, material_recurring_residual=False)
    assert _code(lambda: _dispose(r.scorecard, weak, "consider_tuning")) == (
        "tuning_requires_material_recurring_residual"
    )
    for d in ("benchmark_only", "gather_more_evidence"):
        assert _dispose(base, None, d)["disposition"] == d


def test_cli_exit_codes_and_refusal(tmp_path: Path) -> None:
    good = subprocess.run(
        [*CLI, "--out", str(tmp_path / "a"), "--disposition", "benchmark_only"],
        capture_output=True,
        text=True,
        env=ENV,
        check=False,
    )
    assert good.returncode == 0 and "tier=generated" in good.stdout
    bad = subprocess.run(
        [
            *CLI,
            "--out",
            str(tmp_path / "b"),
            "--baseline-only",
            "--disposition",
            "adopt_test_simple_fix",
        ],
        capture_output=True,
        text=True,
        env=ENV,
        check=False,
    )
    assert bad.returncode == 2 and "adopt_requires_interval_supported_improvement" in bad.stderr


# ---- review findings: F1 content freeze


def _freeze_with(f: Any, **over: Any) -> eb.FrozenManifest:
    args: dict[str, Any] = {
        "benchmark_id": "generated-eve-test",
        "evidence_tier": "generated",
        "development_cases": f.development_cases,
        "heldout_cases": f.heldout_cases,
        "heldout_exclusions": f.exclusions,
        "heldout_key": f.heldout_key,
        "development_key": f.development_key,
        "arms": fx.ARM_CONFIGS,
        "rubric_version": "generated-rubric-1",
        "rubric_text": fx.RUBRIC_TEXT,
        "alpha": 0.1,
        "materiality_min_errors": 2,
        "min_recurring_families": 2,
        "adjudicator_kind": "generated_fixture",
    }
    args.update(over)
    return eb.freeze_manifest(**args)


def test_case_text_and_development_key_are_part_of_the_freeze() -> None:
    f = fx.build_fixture_set()
    base = _freeze_with(f).manifest_digest
    changed_held = tuple(dataclasses.replace(c, question="CHANGED") for c in f.heldout_cases)
    assert _freeze_with(f, heldout_cases=changed_held).manifest_digest != base
    changed_dev = tuple(dataclasses.replace(c, question="CHANGED") for c in f.development_cases)
    assert _freeze_with(f, development_cases=changed_dev).manifest_digest != base
    other_ctx = tuple(dataclasses.replace(c, context_ids=("EV-X",)) for c in f.heldout_cases)
    assert _freeze_with(f, heldout_cases=other_ctx).manifest_digest != base
    bad_key = (dataclasses.replace(f.development_key[0], answer="ANS-X"), *f.development_key[1:])
    assert _freeze_with(f, development_key=bad_key).manifest_digest != base
    assert _freeze_with(f).manifest_digest == base  # unchanged inputs stay stable


def test_scoring_refuses_heldout_text_changed_after_freeze() -> None:
    f, m, _, _, runs, iso = _setup()
    swapped = tuple(dataclasses.replace(c, question="CHANGED") for c in f.heldout_cases)
    code = _code(
        lambda: _score_with(
            m, f, heldout_key=f.heldout_key, isolation=iso, runs=runs, heldout_cases=swapped
        )
    )
    assert code == "heldout_content_not_the_frozen_content"


def test_development_assessment_refuses_changed_cases_or_key() -> None:
    f, m, cfg, _, _, _ = _setup()
    base = fx.simulate_arm(cfg[eb.BASELINE_ARM], f.development_cases, m)
    rep = fx.simulate_arm(cfg[eb.REPAIR_ARM], f.development_cases, m)
    changed = (
        dataclasses.replace(f.development_cases[0], question="CHANGED"),
        *f.development_cases[1:],
    )
    bad = (dataclasses.replace(f.development_key[0], answer="ANS-X"), *f.development_key[1:])

    def assess(cases: Any, key: Any) -> object:
        return eb.assess_development(
            m, development_cases=cases, development_key=key, baseline=base, repair=rep
        )

    assert _code(lambda: assess(changed, f.development_key)) == (
        "development_content_not_the_frozen_content"
    )
    assert _code(lambda: assess(f.development_cases, bad)) == "development_key_not_the_frozen_key"


# ---- review findings: F2 claim follows uncertainty


def test_net_fixes_with_interval_including_zero_is_only_directional() -> None:
    card = _score()
    p = card.body["paired_comparison"]
    assert p["fixes"] > p["regressions"] and p["interval"]["lower"] < 0
    assert card.body["improvement_claim"] == "directional_only_interval_includes_zero"
    assert _code(lambda: _dispose(card, None, "adopt_test_simple_fix")) == (
        "adopt_requires_interval_supported_improvement"
    )


def test_single_fix_without_regression_is_not_an_improvement_claim() -> None:
    f, m, cfg, _, runs, iso = _setup()
    repair_settings = cfg[eb.REPAIR_ARM].retrieval_settings_id
    fixed = {o.case_id: o for o in runs[1].outputs}
    outs = tuple(
        dataclasses.replace(
            fixed[o.case_id] if o.case_id == "HOLD-001" else o,
            retrieval_settings_id=repair_settings,
        )
        for o in runs[0].outputs
    )  # repair equals baseline everywhere except one case
    one = dataclasses.replace(runs[1], outputs=outs)
    card = _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=[runs[0], one])
    p = card.body["paired_comparison"]
    assert (p["fixes"], p["regressions"]) == (1, 0)
    assert card.body["improvement_claim"] == "directional_only_interval_includes_zero"


def test_large_sample_with_both_intervals_above_zero_supports_improvement() -> None:
    r = fx.run_fixture_benchmark(heldout_cases=400)
    p = r.scorecard.body["paired_comparison"]
    assert p["interval"]["lower"] > 0 and p["family_interval"]["lower"] > 0
    assert r.scorecard.body["improvement_claim"] == "improvement_supported_by_interval"
    rec = _dispose(r.scorecard, r.finding, "adopt_test_simple_fix")
    assert rec["disposition"] == "adopt_test_simple_fix" and rec["fit_authorized"] is False


def test_family_interval_uses_families_as_units_and_states_case_independence() -> None:
    p = _score().body["paired_comparison"]
    assert p["interval"]["case_independence_assumed"] is True
    fam = p["family_interval"]
    assert fam["unit"] == "family" and fam["units"] == 15 and fam["units"] < p["paired_gradeable"]
    assert fam["method"] == "bounded_independent_family_hoeffding_v1"
    assert fam["lower"] < p["interval"]["lower"] < p["interval"]["upper"] < fam["upper"]


def test_cli_line_carries_the_interval_caveat_beside_the_claim(tmp_path: Path) -> None:
    out = subprocess.run(
        [*CLI, "--out", str(tmp_path)], capture_output=True, text=True, env=ENV, check=False
    ).stdout
    assert "claim=directional_only_interval_includes_zero" in out
    assert "assumes case independence" in out and "descriptive-only" in out


# ---- review findings: F3 gates cannot be asserted by the caller


@pytest.mark.parametrize("tier", ["installed-provider", "observed", "owner-reviewed"])
def test_higher_evidence_tier_is_refused_without_a_verifier(tier: str) -> None:
    f = fx.build_fixture_set()
    assert _code(lambda: _freeze_with(f, evidence_tier=tier)) == "evidence_tier_unverifiable_here"


def test_audit_ref_shape_is_validated_and_never_clears_the_gates() -> None:
    f = fx.build_fixture_set()
    assert _code(lambda: _freeze_with(f, ground_truth_audit_receipt_ref="x")) == (
        "audit_receipt_ref_shape_invalid"
    )
    ref = "sha256:" + "a" * 64
    m = _freeze_with(
        f, ground_truth_audit_receipt_ref=ref, adjudicator_kind="pi_school_domain_reviewer"
    )
    elig = tuple(c for c in f.heldout_cases if c.case_id in m.heldout_eligible)
    run = fx.simulate_arm(fx.ARM_CONFIGS[0], elig, m)
    iso = eb.verify_isolation(
        m, fit_rows=[], prompt_texts=_prompts(f, m), heldout_key=f.heldout_key
    )
    body = _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=[run]).body
    assert body["accuracy_claim_eligible"] is False
    assert body["claim_scope"] == "implementation_only"
    assert set(eb.UNSATISFIED_EXTERNAL_GATES_GENERATED) <= set(body["unsatisfied_external_gates"])


def test_forged_isolation_receipt_is_refused() -> None:
    f, m, _, _, runs, _ = _setup()
    forged = eb.IsolationReceipt(m.manifest_digest, 0, 0, "d", "d", ())
    code = _code(lambda: _score_with(m, f, heldout_key=f.heldout_key, isolation=forged, runs=runs))
    assert code == "isolation_receipt_not_from_these_inputs"


def test_isolation_receipt_for_other_inputs_is_refused() -> None:
    f, m, _, _, runs, iso = _setup()
    extra = [eb.FitRow("DEV-000", "GEN-FAM-DEV-00", "benign")]
    code = _code(
        lambda: _score_with(
            m, f, heldout_key=f.heldout_key, isolation=iso, runs=runs, fit_rows=extra
        )
    )
    assert code == "isolation_receipt_not_from_these_inputs"
    other = eb.verify_isolation(
        m, fit_rows=extra, prompt_texts=_prompts(f, m), heldout_key=f.heldout_key
    )
    assert iso.fit_rows_digest != other.fit_rows_digest


# ---- review findings: F4/F5 adapter fails closed on type; F6 exclusions redacted


def _export_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "case_id": "HOLD-000",
        "answer": "a",
        "citations": ["c"],
        "abstained": False,
        "served_revision": "r",
        "source_snapshot_id": "s",
        "retrieval_settings_id": "t",
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    "bad",
    [
        {"abstained": "false"},
        {"abstained": 0},
        {"citations": "EV-HOLD-000"},
        {"citations": ["ok", 3]},
        {"citations": None},
        {"answer": 7},
        {"answer": ["a"]},
        {"case_id": ""},
        {"served_revision": None},
    ],
)
def test_prepared_export_adapter_refuses_malformed_types(
    tmp_path: Path, bad: dict[str, Any]
) -> None:
    path = tmp_path / "bad.jsonl"
    path.write_text(json.dumps(_export_row(**bad)) + "\n")
    assert _code(lambda: fx.load_prepared_export(path, "baseline", "sha256:x")) == (
        "export_row_type_invalid"
    )


@pytest.mark.parametrize("text", ["{not json", "[1, 2]"])
def test_prepared_export_adapter_refuses_non_json_and_non_object(tmp_path: Path, text: str) -> None:
    path = tmp_path / "x.jsonl"
    path.write_text(text + "\n")
    assert _code(lambda: fx.load_prepared_export(path, "baseline", "sha256:x")) in (
        "export_row_not_json",
        "export_row_shape_invalid",
    )


def test_prepared_export_adapter_accepts_null_answer_and_abstention(tmp_path: Path) -> None:
    path = tmp_path / "ok.jsonl"
    path.write_text(json.dumps(_export_row(answer=None, citations=[], abstained=True)) + "\n")
    out = fx.load_prepared_export(path, "baseline", "sha256:x").outputs[0]
    assert out.abstained is True and out.answer is None and out.citations == ()


def test_exclusion_reason_must_be_a_closed_code_and_ids_are_hashed_in_scorecard() -> None:
    f = fx.build_fixture_set()
    free = (eb.ExclusionRecord("HOLD-011", "free text with detail"),)
    assert _code(lambda: _freeze_with(f, heldout_exclusions=free)) == "exclusion_reason_invalid"
    excluded = _score().body["heldout"]["excluded"]
    assert len(excluded) == 1 and excluded[0][0].startswith("case:")
    assert excluded[0][1] == "context_not_authorized"


# ---- post-merge audit follow-up (2026-11-28): disposition provenance, owner, one per card


def test_disposition_refuses_a_scorecard_whose_body_does_not_match_its_digest() -> None:
    card = _score()
    assert card.body["improvement_claim"] == "directional_only_interval_includes_zero"
    upgraded = {**card.body, "improvement_claim": "improvement_supported_by_interval"}
    forged = dataclasses.replace(card, body=upgraded)  # stale digest, genuine seal copied
    for name in ("adopt_test_simple_fix", "benchmark_only"):
        assert _code(lambda n=name: _dispose(forged, None, n)) == "scorecard_digest_mismatch"


def test_disposition_refuses_a_hand_built_scorecard_even_with_a_recomputed_digest() -> None:
    card = _score()
    upgraded = {**card.body, "improvement_claim": "improvement_supported_by_interval"}
    for forged in (
        eb.Scorecard(body=upgraded, scorecard_digest=eb.digest(upgraded)),
        dataclasses.replace(card, body=upgraded, scorecard_digest=eb.digest(upgraded)),
        eb.Scorecard(body=dict(card.body), scorecard_digest=card.scorecard_digest),  # no seal
    ):
        assert eb.digest(dict(forged.body)) == forged.scorecard_digest  # self-consistent
        assert _code(lambda f=forged: _dispose(f, None, "benchmark_only")) == (
            "scorecard_not_from_scoring"
        )
    injected = {**card.body, "named_owner": {"owner_id": "generated-attacker"}}
    hand = eb.Scorecard(body=injected, scorecard_digest=eb.digest(injected))
    assert _code(lambda: _dispose(hand, None, "benchmark_only")) == "scorecard_not_from_scoring"
    assert _dispose(card, None, "benchmark_only")["scorecard_digest"] == card.scorecard_digest


def test_disposition_refuses_a_hand_built_development_finding() -> None:
    r = fx.run_fixture_benchmark()
    assert r.finding is not None and r.finding.material_recurring_residual is True
    genuine = _dispose(r.scorecard, r.finding, "consider_tuning")
    assert genuine["disposition"] == "consider_tuning"
    raw = r.finding.to_json()
    hand_built = eb.DevelopmentFinding(
        raw["manifest_digest"],
        raw["baseline_serious"],
        raw["repair_serious"],
        raw["verified"],
        raw["fixes"],
        raw["regressions"],
        tuple(raw["residual_error_families"]),
        True,
    )
    edited = dataclasses.replace(r.finding, fixes=r.finding.fixes + 7)  # seal copied, body moved
    for forged in (hand_built, edited):
        for name in ("consider_tuning", "benchmark_only"):
            assert _code(lambda f=forged, n=name: _dispose(r.scorecard, f, n)) == (
                "finding_not_from_assessment"
            )


def _named_owner_score(owner: str) -> eb.Scorecard:
    f, _, cfg, _, _, _ = _setup()
    m = _freeze_with(f, named_owner_id=owner)
    elig = tuple(c for c in f.heldout_cases if c.case_id in m.heldout_eligible)
    runs = [fx.simulate_arm(cfg[a], elig, m) for a in (eb.BASELINE_ARM, eb.REPAIR_ARM)]
    iso = eb.verify_isolation(
        m, fit_rows=[], prompt_texts=_prompts(f, m), heldout_key=f.heldout_key
    )
    return _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=runs)


def test_owner_id_comes_from_the_owner_named_in_the_frozen_manifest() -> None:
    card = _named_owner_score("generated-named-owner")
    assert card.body["named_owner"] == {"owner_id": "generated-named-owner"}
    rec = eb.record_disposition(
        card, None, disposition="benchmark_only", recorded_at="2026-01-01"
    )  # no owner id supplied: it is taken from the bound record
    assert rec["owner_id"] == "generated-named-owner"
    assert rec["owner_binding"] == "named_in_frozen_manifest"
    assert rec["named_owner_gate_satisfied"] is True
    same = eb.record_disposition(
        card,
        None,
        disposition="benchmark_only",
        owner_id="generated-named-owner",
        recorded_at="2026-01-01",
    )
    assert same == rec
    for other in ("generated-other-owner", "Generated-Named-Owner", "generated-named-owner "):
        assert _code(
            lambda o=other: eb.record_disposition(
                card, None, disposition="benchmark_only", owner_id=o, recorded_at="2026-01-01"
            )
        ) == ("owner_not_the_named_owner")
    fixtures = fx.build_fixture_set()
    for bad in ("", 7, b"generated-named-owner"):
        assert _code(lambda b=bad: _freeze_with(fixtures, named_owner_id=b)) == (
            "named_owner_invalid"
        )


def test_caller_asserted_owner_is_labelled_and_cannot_satisfy_the_named_owner_gate(
    tmp_path: Path,
) -> None:
    card = _score()
    assert "named_owner" not in card.body
    rec = _dispose(card, None, "benchmark_only")
    assert rec["owner_id"] == "generated-owner-1"
    assert rec["owner_binding"] == "caller_asserted"
    assert rec["named_owner_gate_satisfied"] is False
    assert _code(
        lambda: eb.record_disposition(
            card, None, disposition="benchmark_only", recorded_at="2026-01-01"
        )
    ) == ("owner_missing")
    proc = subprocess.run(
        [*CLI, "--out", str(tmp_path), "--disposition", "benchmark_only"],
        capture_output=True,
        text=True,
        env=ENV,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    (written,) = list(tmp_path.glob("disposition-*.json"))
    cli_rec = json.loads(written.read_text())
    assert cli_rec["owner_binding"] == "caller_asserted"
    assert cli_rec["named_owner_gate_satisfied"] is False


def test_worker_enforces_one_disposition_per_scorecard(tmp_path: Path) -> None:
    r = fx.run_fixture_benchmark()
    store = tmp_path / "store"

    def once(card: eb.Scorecard, name: str, at: str = "2026-01-01") -> bool:
        return eb.record_one_disposition(
            store, card, None, disposition=name, owner_id="generated-owner-1", recorded_at=at
        )

    assert once(r.scorecard, "benchmark_only") is True
    assert once(r.scorecard, "benchmark_only") is False  # identical retry is a no-op
    for name, at in (("gather_more_evidence", "2026-01-01"), ("benchmark_only", "2026-01-02")):
        assert _code(lambda n=name, a=at: once(r.scorecard, n, a)) == (
            "disposition_already_recorded"
        )
    other = _score(include_repair=False)  # a different scorecard may take its own disposition
    assert other.scorecard_digest != r.scorecard.scorecard_digest
    assert once(other, "gather_more_evidence") is True
    files = sorted(store.glob("disposition-*.json"))
    assert len(files) == 2
    recorded = {json.loads(p.read_text())["scorecard_digest"] for p in files}
    assert recorded == {r.scorecard.scorecard_digest, other.scorecard_digest}
    assert not list(store.glob(".tmp-*"))


def test_cli_refuses_a_second_disposition_for_the_same_scorecard(tmp_path: Path) -> None:
    def cli(name: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*CLI, "--out", str(tmp_path), "--disposition", name],
            capture_output=True,
            text=True,
            env=ENV,
            check=False,
        )

    assert cli("benchmark_only").returncode == 0
    assert cli("benchmark_only").returncode == 0  # rerun of the same disposition is idempotent
    second = cli("gather_more_evidence")
    assert second.returncode == 2 and "disposition_already_recorded" in second.stderr
    assert len(list(tmp_path.glob("disposition-*.json"))) == 1


def test_family_lower_bound_alone_withholds_the_improvement_claim() -> None:
    """Pins both bounds: case-level lower > 0, family-level lower <= 0, so no improvement."""
    f = fx.build_fixture_set(heldout_cases=400)
    wide = tuple(
        dataclasses.replace(c, family_id=f"GEN-FAM-HOLD-W{n // 40:02d}")
        for n, c in enumerate(f.heldout_cases)
    )  # same 400 cases, 10 families of 40 instead of 200 of 2
    f = dataclasses.replace(f, heldout_cases=wide)
    m = _freeze_with(f)
    cfg = {c.arm_id: c for c in fx.ARM_CONFIGS}
    elig = tuple(c for c in wide if c.case_id in m.heldout_eligible)
    runs = [fx.simulate_arm(cfg[a], elig, m) for a in (eb.BASELINE_ARM, eb.REPAIR_ARM)]
    prompts = [c.question for c in wide if c.case_id in m.heldout_eligible]
    iso = eb.verify_isolation(m, fit_rows=[], prompt_texts=prompts, heldout_key=f.heldout_key)
    card = eb.score_heldout(
        m,
        heldout_cases=wide,
        heldout_key=f.heldout_key,
        fit_rows=[],
        prompt_texts=prompts,
        isolation=iso,
        runs=runs,
    )
    p = card.body["paired_comparison"]
    assert p["family_interval"]["units"] == 10
    assert p["fixes"] > p["regressions"]
    assert p["interval"]["lower"] > 0  # the case-level bound alone would support a claim
    assert p["family_interval"]["lower"] <= 0
    assert card.body["improvement_claim"] == "directional_only_interval_includes_zero"
    assert _code(lambda: _dispose(card, None, "adopt_test_simple_fix")) == (
        "adopt_requires_interval_supported_improvement"
    )


class _ShiftyBody(dict):  # type: ignore[type-arg]
    """Copies as the genuine body but answers an upgraded claim on direct lookup."""

    def __getitem__(self, key: object) -> Any:
        if key == "improvement_claim":
            return "improvement_supported_by_interval"
        return super().__getitem__(key)


def test_disposition_reads_only_the_digest_checked_snapshot_of_the_body() -> None:
    card = _score()
    shifty = dataclasses.replace(card, body=_ShiftyBody(card.body))  # genuine digest and seal
    assert shifty.body["improvement_claim"] == "improvement_supported_by_interval"
    assert eb.digest(dict(shifty.body)) == card.scorecard_digest
    assert _code(lambda: _dispose(shifty, None, "adopt_test_simple_fix")) == (
        "adopt_requires_interval_supported_improvement"
    )


def test_a_finding_subclass_cannot_borrow_a_genuine_seal() -> None:
    f, _, cfg, _, _, _ = _setup()
    m = _freeze_with(f, materiality_min_errors=99)  # genuine finding: residual not material
    dev = {a: fx.simulate_arm(cfg[a], f.development_cases, m) for a in cfg}
    genuine = eb.assess_development(
        m,
        development_cases=f.development_cases,
        development_key=f.development_key,
        baseline=dev[eb.BASELINE_ARM],
        repair=dev[eb.REPAIR_ARM],
    )
    assert genuine.material_recurring_residual is False
    elig = tuple(c for c in f.heldout_cases if c.case_id in m.heldout_eligible)
    runs = [fx.simulate_arm(cfg[a], elig, m) for a in (eb.BASELINE_ARM, eb.REPAIR_ARM)]
    iso = eb.verify_isolation(
        m, fit_rows=[], prompt_texts=_prompts(f, m), heldout_key=f.heldout_key
    )
    card = _score_with(m, f, heldout_key=f.heldout_key, isolation=iso, runs=runs)

    class Borrowed(eb.DevelopmentFinding):
        def to_json(self) -> dict[str, Any]:
            return genuine.to_json()  # what the seal was computed over

    fields = {k: getattr(genuine, k) for k in genuine.__dataclass_fields__}
    borrowed = Borrowed(**{**fields, "material_recurring_residual": True})
    assert borrowed.seal == genuine.seal
    assert _code(lambda: _dispose(card, borrowed, "consider_tuning")) == (
        "finding_not_from_assessment"
    )
    assert _code(lambda: _dispose(card, genuine, "consider_tuning")) == (
        "tuning_requires_material_recurring_residual"
    )
    assert _dispose(card, genuine, "benchmark_only")["disposition"] == "benchmark_only"


def test_case_lower_bound_alone_withholds_the_improvement_claim() -> None:
    """Mirror of the family pin: family-level lower > 0, case-level lower <= 0, no claim."""
    f = fx.build_fixture_set()
    fixes_first = tuple(
        dataclasses.replace(
            c,
            family_id=(
                f"GEN-FAM-HOLD-S{c.case_id}"
                if int(c.case_id.split("-")[1]) % 5 in (1, 4)  # the cases the repair fixes
                else "GEN-FAM-HOLD-REST"
            ),
        )
        for c in f.heldout_cases
    )  # every fix is its own family; everything else, regressions included, shares one
    f = dataclasses.replace(f, heldout_cases=fixes_first)
    m = _freeze_with(f)
    cfg = {c.arm_id: c for c in fx.ARM_CONFIGS}
    elig = tuple(c for c in fixes_first if c.case_id in m.heldout_eligible)
    runs = [fx.simulate_arm(cfg[a], elig, m) for a in (eb.BASELINE_ARM, eb.REPAIR_ARM)]
    prompts = [c.question for c in elig]
    iso = eb.verify_isolation(m, fit_rows=[], prompt_texts=prompts, heldout_key=f.heldout_key)
    card = eb.score_heldout(
        m,
        heldout_cases=fixes_first,
        heldout_key=f.heldout_key,
        fit_rows=[],
        prompt_texts=prompts,
        isolation=iso,
        runs=runs,
    )
    p = card.body["paired_comparison"]
    assert (p["fixes"], p["regressions"]) == (11, 3)
    assert p["family_interval"]["lower"] > 0  # the family bound alone would support a claim
    assert p["interval"]["lower"] <= 0
    assert card.body["improvement_claim"] == "directional_only_interval_includes_zero"
