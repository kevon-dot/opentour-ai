"""Actual recorded-baseline intent remains separate from repair and training eligibility."""

import json
import sqlite3

import pytest
from opentour_cell.pilot import comparison_plan, repair_workflow, simple_repair, source_intake
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import LocalHistoricalAuthority
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake
from test_historical_benchmarks import setup


def test_baseline_without_comparator_or_development_cases_preserves_measurement_only(
    tmp_path, monkeypatch
):
    intake, actor, request, producer, ledger, mandate = setup(
        tmp_path, baseline_only=True, no_development=True
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("baseline attempted repair, fit or model execution")

    monkeypatch.setattr(repair_workflow, "execute_managed", forbidden)
    monkeypatch.setattr(simple_repair, "fit_rule", forbidden)
    monkeypatch.setattr(repair_workflow, "_model_arm", forbidden)
    admission = intake.submit_intake(actor, request)
    assert intake.materialize(actor, admission.run_ref, producer)
    output = next((producer.directory / "managed-outputs").iterdir())
    result = json.loads((output / "comparison.json").read_text())
    plan = json.loads((output / "plan.json").read_text())
    frozen = source_intake.load_intake(
        producer.authority.store, next(producer.directory.glob("benchmark-intake-*.json"))
    )
    assert frozen["development"] == [] and frozen["training_case_ids"] == []
    assert result["schema_version"] == comparison_plan.BASELINE_RESULT
    assert result["evaluation_purpose"] == "recorded_baseline"
    assert result["protected_rows_evaluated"] == plan["protected_count"] == 16
    assert result["protected_membership_digest"] == frozen["protected_membership_digest"]
    assert set(result["arms"]) == {"incumbent"} and result["comparisons"] == {}
    assert result["arms"]["incumbent"]["metrics"]["incorrect_automatic"] == 8
    assert result["decision"] == {
        "disposition": "observe",
        "winner": None,
        "reason": "recorded_baseline_only",
    }
    assert result["outcome"] == "baseline_observed"
    assert (
        "development_decision" not in plan
        and "rule_revision" not in plan
        and "model_revision" not in plan
    )
    assert ledger.inspect(mandate["mandate_id"], actor=actor.actor)["reserved_fits"] == 0
    with sqlite3.connect(producer.authority.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM train_data").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM split_keys WHERE split='train'").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM protected_comparisons").fetchone()[0] == 1


def test_baseline_scores_while_bad_declared_repair_stays_held_and_cohort_cannot_be_reused(tmp_path):
    intake, actor, request, producer, _, _ = setup(tmp_path, baseline_only=True)
    admission = intake.submit_intake(actor, request)
    assert intake.materialize(actor, admission.run_ref, producer)
    frozen = source_intake.load_intake(
        producer.authority.store, next(producer.directory.glob("benchmark-intake-*.json"))
    )
    policy = json.loads(producer.agreement.read_text())
    terms = {**policy["terms"], "minimum_development_cases": 4, "minimum_residual_errors": 3}
    bad_rule = simple_repair.declared_rule(
        feature="event_type",
        allowed_features=frozen["allowed_features"],
        mapping={"refund": "sale", "sale": "sale"},
        fallback="incumbent",
    )
    held = simple_repair.assess_development(
        frozen["development"],
        bad_rule,
        terms=terms,
        model_authorized=False,
        compute_authorized=False,
    )
    assert held["route"] == "hold"
    rule = simple_repair.declared_rule(
        feature="event_type",
        allowed_features=frozen["allowed_features"],
        mapping={"refund": "refund", "sale": "sale"},
        fallback="incumbent",
    )
    decision = simple_repair.assess_development(
        frozen["development"], rule, terms=terms, model_authorized=False, compute_authorized=False
    )
    candidate = comparison_plan.freeze_plan(
        organization=actor.tenant,
        engagement=producer.authority.store.engagement_id,
        audience=actor.actor,
        intake_revision=frozen["intake_revision"],
        protected_ids=[row["case_id"] for row in frozen["protected"]],
        incumbent_revision=policy["incumbent_revision"],
        rule=rule,
        development_decision=decision,
        terms=terms,
    )
    with pytest.raises(ValueError, match="fresh_protected_cohort_required"):
        comparison_plan.run_comparison(producer.authority.store, candidate, frozen, rule=rule)


def test_baseline_crash_readback_never_repeats_score_and_rejects_invented_eligibility(
    tmp_path, monkeypatch
):
    intake, actor, request, producer, _, _ = setup(tmp_path, baseline_only=True)
    admission = intake.submit_intake(actor, request)

    def crash(*args, **kwargs):
        raise RuntimeError("generated crash before ledger publication")

    monkeypatch.setattr(intake.benchmarks, "publish", crash)
    with pytest.raises(RuntimeError):
        intake.materialize(actor, admission.run_ref, producer)
    with sqlite3.connect(producer.authority.store.path) as db:
        revision, original = db.execute(
            "SELECT revision,result FROM protected_comparisons"
        ).fetchone()
        changed = json.loads(original)
        changed["decision"] = {
            "disposition": "recommend",
            "winner": "incumbent",
            "reason": "invented_eligibility",
        }
        changed["evidence_revision"] = digest(
            {k: v for k, v in changed.items() if k != "evidence_revision"}
        )
        db.execute("UPDATE protected_comparisons SET result=?", (canonical(changed),))
    reopened = BenchmarkIntake(
        DurableIntake(producer.directory, LocalHistoricalAuthority(producer.authority.path))
    )

    def no_rescore(*args, **kwargs):
        raise AssertionError("receipt recovery attempted another protected observation")

    monkeypatch.setattr(comparison_plan, "run_recorded_baseline", no_rescore)
    with pytest.raises(ValueError, match="recorded_baseline_result_mismatch"):
        reopened.materialize(actor, admission.run_ref, producer)
    with sqlite3.connect(producer.authority.store.path) as db:
        db.execute(
            "UPDATE protected_comparisons SET result=? WHERE revision=?", (original, revision)
        )
    assert reopened.materialize(actor, admission.run_ref, producer)
    assert reopened.materialize(actor, admission.run_ref, producer)


def test_baseline_missing_recorded_prediction_cannot_publish_score(tmp_path):
    intake, actor, request, producer, _, _ = setup(
        tmp_path, baseline_only=True, missing_baseline=True
    )
    admission = intake.submit_intake(actor, request)
    with pytest.raises(IntakeError, match="recorded_baseline_predictions_required"):
        intake.materialize(actor, admission.run_ref, producer)
    assert intake.get_run(actor, admission.run_ref).benchmark.benchmark_state == "draft"
    assert not (producer.directory / "managed-outputs").exists()
