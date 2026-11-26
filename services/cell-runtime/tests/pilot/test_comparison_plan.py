"""Freeze, evaluate and replay all arms once across legacy and managed registrations."""

import copy
import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from opentour_cell.pilot import comparison_plan as comparison
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.simple_repair import RepairError, assess_development, declared_rule
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_data import generated_evaluation_dataset


def isolated_score(*args, **kwargs):
    # Pure-domain tests supply a generated authority callback. The portable application
    # always rechecks its signed local source grant and the managed runner's authority.
    return comparison.run_comparison(*args, authorize=lambda: None, **kwargs)


def auto(label):
    return {"status": "automatic", "label": label}


def rows(prefix, refs=("a", "a", "a", "a")):
    return [
        {
            "case_id": f"{prefix}-{i}",
            "group_id": f"family-{prefix}-{i}",
            "features": {"category": "x"},
            "slices": ["critical"],
            "reference": ref,
            "incumbent": auto("b"),
            "outcome_id": f"out-{prefix}-{i}",
            "adjudicator_id": "independent",
            "verified_at": 100 + i,
        }
        for i, ref in enumerate(refs)
    ]


def enroll(store, *, protected=None, development=None):
    dev = rows("dev") if development is None else development
    test = rows("protected") if protected is None else protected
    data = {
        "schema_version": "opentour.pilot.source-intake.v1",
        "organization": store.organization,
        "engagement": store.engagement_id,
        "sources": {
            "trace": {"records_digest": digest("events")},
            "correction": {"records_digest": digest("corrections")},
        },
        "authority_revision": digest("owner"),
        "policy_version": "v3",
        "source_schema_version": "v5",
        "protected_membership_digest": digest(sorted(r["case_id"] for r in test)),
        "training_allowed": True,
        "training_expires_at": int(time.time()) + 7200,
        "expires_at": int(time.time()) + 7200,
        "allowed_features": ["category"],
        "allowed_slices": ["critical", "rare"],
        "error_class": {"from_label": "b", "to_label": "a"},
        "split": {"method": "time", "after": 90},
        "excluded_counts": {"late_feature": 1},
        "development": dev,
        "protected": test,
    }
    return data | {"intake_revision": digest(data)}


def setup(data, *, terms=None, mapping=None, model=None, measured=None):
    terms = {"minimum_coverage_bps": 10000, "error_ceiling_bps": 0} if terms is None else terms
    rule = declared_rule(
        feature="category",
        allowed_features=["category"],
        mapping={"x": "a"} if mapping is None else mapping,
    )
    dev = assess_development(
        data["development"],
        rule,
        terms=terms,
        model_authorized=model is not None,
        compute_authorized=model is not None,
    )
    plan = comparison.freeze_plan(
        organization=data["organization"],
        engagement=data["engagement"],
        audience="reviewer",
        intake_revision=data["intake_revision"],
        protected_ids=[r["case_id"] for r in data["protected"]],
        incumbent_revision=digest("incumbent"),
        rule=rule,
        development_decision=dev,
        terms=terms,
        model_revision=digest("frozen-model") if model is not None else None,
        model_predictions=model,
        model_budget_revision=digest("permitted-cap") if model is not None else None,
        measurements=measured,
    )
    return plan, rule


def test_zero_fit_rule_all_cases_then_independent_process_replay_and_cached_retry(tmp_path):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    data = enroll(store)
    plan, rule = setup(data)
    report = isolated_score(store, plan, data, rule=rule)
    assert report["outcome"] == "simple_repair" and report["decision"]["winner"] == "simple"
    assert report["arms"]["simple"]["metrics"]["incorrect_automatic"] == 0
    assert report["comparisons"]["incumbent_vs_simple"]["paired"]["fixes"] == 4
    assert report["arms"]["simple"]["metrics"]["economics"]["cost_per_1000"] is None
    assert report["arms"]["simple"]["prediction_digest"] == digest(
        {r["case_id"]: r["simple"] for r in report["paired_cases"]}
    )
    assert report["evidence_revision"] == digest(
        {k: v for k, v in report.items() if k != "evidence_revision"}
    )
    assert isolated_score(store, plan, data, rule=rule) == report
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM protected_comparisons").fetchone()[0] == 1
    command = """import json,sys
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.comparison_plan import run_comparison
q=json.load(sys.stdin)
out=run_comparison(LocalTrainingStore(sys.argv[1],'generated'),q['plan'],q['data'],rule=q['rule'],replay=True,authorize=lambda:None)
print(json.dumps(out,sort_keys=True))
"""
    proc = subprocess.run(
        [sys.executable, "-B", "-c", command, str(store.path)],
        input=json.dumps({"data": data, "plan": plan, "rule": rule}),
        text=True,
        capture_output=True,
        timeout=10,
        check=True,
    )
    assert json.loads(proc.stdout) == report


def test_eligible_frozen_model_compares_all_arms_once_and_blocks_changed_predictions(tmp_path):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    data = enroll(store)
    predictions = {r["case_id"]: auto("a") for r in data["protected"]}
    plan, rule = setup(data, mapping={"x": "b"}, model=predictions)
    damaged = {**predictions, next(iter(predictions)): auto("b")}
    with pytest.raises(RepairError, match="artifact_revision_mismatch"):
        isolated_score(store, plan, data, rule=rule, model_predictions=damaged)
    report = isolated_score(store, plan, data, rule=rule, model_predictions=predictions)
    assert report["outcome"] == "model_candidate" and set(report["arms"]) == {
        "incumbent",
        "simple",
        "model",
    }
    assert report["comparisons"]["simple_vs_model"]["paired"]["fixes"] == 4
    assert all(row["model"] == predictions[row["case_id"]] for row in report["paired_cases"])
    assert (
        isolated_score(store, plan, data, rule=rule, model_predictions=predictions, replay=True)
        == report
    )
    next_plan, _ = setup(data, mapping={"x": "b"}, model=damaged)
    with pytest.raises(RepairError, match="fresh_protected_cohort"):
        isolated_score(store, next_plan, data, rule=rule, model_predictions=damaged)


def test_changed_terms_or_overlapping_legacy_data_cannot_spend_another_test(tmp_path):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    old = generated_evaluation_dataset(store.organization)
    store.register(old)
    hidden = rows("private")
    hidden[0]["case_id"] = old["manifest"]["cases"][0]["case_id"]
    data = enroll(store, protected=hidden)
    plan, rule = setup(data)
    with pytest.raises(RepairError, match="fresh_protected_cohort"):
        isolated_score(store, plan, data, rule=rule)
    fresh = enroll(store)
    plan, rule = setup(fresh)
    isolated_score(store, plan, fresh, rule=rule)
    altered, _ = setup(fresh, terms={"minimum_coverage_bps": 8000, "error_ceiling_bps": 0})
    with pytest.raises(RepairError, match="fresh_protected_cohort"):
        isolated_score(store, altered, fresh, rule=rule)
    later = enroll(store, development=fresh["protected"], protected=rows("new"))
    another, _ = setup(later)
    with pytest.raises(RepairError, match="development_contains_previously_protected"):
        isolated_score(store, another, later, rule=rule)


def test_incorrect_scope_expiry_case_and_group_leakage_deny(tmp_path):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    foreign = LocalTrainingStore(tmp_path / "other.sqlite3", "other")
    data = enroll(store)
    plan, rule = setup(data)
    with pytest.raises(RepairError, match="scope_mismatch"):
        isolated_score(foreign, plan, data, rule=rule)
    changed = copy.deepcopy(data)
    changed["protected"][0]["reference"] = "changed"
    with pytest.raises(RepairError, match="intake_revision_mismatch"):
        isolated_score(store, plan, changed, rule=rule)
    mixed = rows("hidden")
    mixed[0]["group_id"] = data["development"][0]["group_id"]
    grouped = enroll(store, protected=mixed)
    p, r = setup(grouped)
    with pytest.raises(RepairError, match="protected_development_overlap"):
        isolated_score(store, p, grouped, rule=r)
    mixed[0]["case_id"] = data["development"][0]["case_id"]
    with pytest.raises(RepairError, match="protected_development_overlap"):
        setup(enroll(store, protected=mixed))
    timed = {k: v for k, v in data.items() if k != "intake_revision"} | {"expires_at": 0}
    timed["intake_revision"] = digest(timed)
    p, _ = setup(timed)
    with pytest.raises(RepairError, match="intake_expired"):
        isolated_score(store, p, timed, rule=rule)


def test_concurrent_one_claim_crash_after_claim_and_private_restore(tmp_path, monkeypatch):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    data = enroll(store)
    plan, rule = setup(data)
    with monkeypatch.context() as patch:
        patch.setattr(
            comparison, "_score", lambda *_: (_ for _ in ()).throw(RuntimeError("interrupted"))
        )
        with pytest.raises(RuntimeError, match="interrupted"):
            isolated_score(store, plan, data, rule=rule)
    altered, _ = setup(data, terms={"minimum_coverage_bps": 8000, "error_ceiling_bps": 0})
    with pytest.raises(RepairError, match="fresh_protected_cohort"):
        isolated_score(store, altered, data, rule=rule)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: isolated_score(store, plan, data, rule=rule), range(4)))
    assert len({x["evidence_revision"] for x in results}) == 1
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE protected_comparisons SET result='{}'")
    with pytest.raises(RepairError, match="digest_mismatch"):
        isolated_score(store, plan, data, rule=rule)
    store.withdraw()
    store.purge_withdrawn()
    with sqlite3.connect(store.path) as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='protected_comparisons'"
        ).fetchone()


@pytest.mark.parametrize(
    "shape,reason",
    [
        ("rare", "critical_slice_failed_or_unverified"),
        ("regression", "critical_slice_failed_or_unverified"),
        ("unknown", "incomplete_protected_evidence"),
        ("empty", "insufficient_protected_support"),
    ],
)
def test_critical_slices_unknown_references_or_empty_cases_stop_recommendation(
    tmp_path, shape, reason
):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    hidden = rows("hidden")
    terms = {
        "minimum_coverage_bps": 10000,
        "error_ceiling_bps": 10000,
        "critical_slices": ["critical"],
        "minimum_slice_cases": 2,
    }
    if shape == "rare":
        terms["critical_slices"] = ["rare"]
        hidden[0]["slices"] = ["rare"]
    elif shape == "regression":
        hidden[0]["reference"] = "b"
    elif shape == "unknown":
        hidden[0]["reference"] = None
    else:
        hidden = []
    data = enroll(store, protected=hidden)
    plan, rule = setup(data, terms=terms)
    result = isolated_score(store, plan, data, rule=rule)
    assert result["decision"] == {
        "disposition": "hold",
        "winner": None,
        "fallback": "incumbent",
        "reason": reason,
    }


def test_higher_real_cost_tradeoff_and_no_change_are_reported(tmp_path):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    data = enroll(store)

    def measured(amount):
        return {
            "basis": "observed",
            "permission_ref": "permit",
            "provenance_ref": "private-meter",
            "hardware": "cpu",
            "period_start": 1,
            "period_end": 2,
            "currency": "USD",
            "unit": "second",
            "rate_per_unit": "0.01",
            "cases": {r["case_id"]: {"usage": amount} for r in data["protected"]},
        }

    inputs = {"incumbent": measured("1"), "simple": measured("2")}
    plan, rule = setup(data, measured=inputs)
    result = isolated_score(store, plan, data, rule=rule, measurements=inputs)
    assert result["decision"]["reason"] == "quality_cost_tradeoff_requires_owner_choice"
    known = rows("new")
    for row in known:
        row["incumbent"] = auto("a")
    dev = rows("new-dev")
    for row in dev:
        row["incumbent"] = auto("a")
    neutral = enroll(store, development=dev, protected=known)
    p, r = setup(neutral)
    assert p["development_decision"]["route"] == "no_change"
    assert isolated_score(store, p, neutral, rule=r)["outcome"] == "no_change"


def test_three_arm_observed_cost_tradeoff_prefers_adequate_lower_cost_rule(tmp_path):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    dev = rows("dev")
    for r in dev:
        r["features"]["category"] = "uncorrected"
    hidden = rows("hidden")
    for r in hidden[:2]:
        r["features"]["category"] = "uncorrected"
    data = enroll(store, development=dev, protected=hidden)
    predicted = {r["case_id"]: auto("a") for r in hidden}

    def meter(count):
        return {
            "basis": "observed",
            "permission_ref": "generated-permit",
            "provenance_ref": "generated-meter",
            "hardware": "cpu",
            "period_start": 1,
            "period_end": 2,
            "currency": "USD",
            "unit": "cpu_second",
            "rate_per_unit": "0.1",
            "cases": {r["case_id"]: {"usage": count} for r in hidden},
        }

    metered = {arm: meter(n) for arm, n in (("incumbent", "5"), ("simple", "1"), ("model", "3"))}
    plan, rule = setup(
        data,
        terms={"minimum_coverage_bps": 10000, "error_ceiling_bps": 5000},
        mapping={"x": "a", "uncorrected": "b"},
        model=predicted,
        measured=metered,
    )
    result = isolated_score(
        store, plan, data, rule=rule, model_predictions=predicted, measurements=metered
    )
    assert result["decision"]["winner"] == "simple"
    assert result["decision"]["reason"] == "adequate_simple_repair_at_lower_observed_cost"
    assert result["comparisons"]["simple_vs_model"]["paired"]["fixes"] == 2


def test_early_preflight_and_valid_other_receipt_are_denied(tmp_path):
    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    data = enroll(store)
    p, r = setup(data)
    comparison.validate_development_partition(store, data["development"])
    with pytest.raises(RepairError, match="frozen_result_required"):
        isolated_score(store, p, data, rule=r, replay=True)
    first = isolated_score(store, p, data, rule=r)
    with pytest.raises(RepairError, match="development_contains_previously_protected"):
        comparison.validate_development_partition(store, data["protected"])
    second = enroll(store, protected=rows("other"))
    pp, rr = setup(second)
    isolated_score(store, pp, second, rule=rr)
    with sqlite3.connect(store.path) as db:
        db.execute(
            "UPDATE protected_comparisons SET result=? WHERE revision=?",
            (json.dumps(first), pp["revision"]),
        )
    with pytest.raises(RepairError, match="digest_mismatch"):
        isolated_score(store, pp, second, rule=rr)


def test_out_of_scope_model_error_class_and_missing_source_authority_fail(tmp_path):
    from opentour_cell.pilot.text_classifier import TrainingError

    store = LocalTrainingStore(tmp_path / "training.sqlite3", "generated")
    data = enroll(store)
    p, r = setup(data)
    with pytest.raises((TrainingError, OSError)):
        comparison.run_comparison(store, p, data, rule=r)
    damaged = data.copy()
    damaged["error_class"] = {"from_label": "other", "to_label": "unrelated"}
    del damaged["intake_revision"]
    damaged["intake_revision"] = digest(damaged)
    frozen = {row["case_id"]: auto("a") for row in damaged["protected"]}
    p, r = setup(damaged, mapping={"x": "b"}, model=frozen)
    with pytest.raises(RepairError, match="owner_confirmed_recurring"):
        isolated_score(store, p, damaged, rule=r, model_predictions=frozen)
