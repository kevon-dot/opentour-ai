"""Generated sources, actual standing mandate, source freeze and protected comparison."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot import repair_workflow, simple_repair
from opentour_cell.pilot.historical_benchmarks import (
    BASELINE_POLICY,
    POLICY,
    LocalHistoricalBenchmark,
)
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import SELECTION_VERSION, LocalHistoricalAuthority
from opentour_cell.pilot.intake_mcp import HistoricalPair, IntakeError
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake, BenchmarkScope

EXAMPLES = Path(__file__).resolve().parents[2] / "examples"


def write(path, value):
    path.write_text(canonical(value))
    path.chmod(0o600)


def setup(
    tmp_path,
    *,
    missing_baseline=False,
    missing_threshold=False,
    baseline_only=False,
    no_development=False,
    evaluation_delegate="generated-worker",
):
    directory = tmp_path / "private"
    subprocess.run(
        [
            sys.executable,
            str(EXAMPLES / "prepare-repair-example.py"),
            str(directory),
            "--route",
            "rule",
        ],
        capture_output=True,
        check=True,
    )
    old = json.loads((directory / "managed-plan.json").read_text())
    policy = {
        "schema_version": POLICY,
        "incumbent_revision": old["incumbent_revision"],
        "rule": old["rule"],
        "terms": {
            **old["terms"],
            "minimum_development_cases": 4,
            "minimum_verified_cases": 4,
            "minimum_residual_errors": 3,
            "minimum_slice_cases": 1,
        },
    }
    if missing_threshold:
        del policy["terms"]["minimum_verified_cases"]
    if baseline_only:
        policy["schema_version"] = BASELINE_POLICY
        del policy["rule"]
        del policy["terms"]["minimum_development_cases"]
        del policy["terms"]["minimum_residual_errors"]
    agreement = directory / "benchmark-policy.json"
    write(agreement, policy)
    grant = json.loads((directory / "source-grant.json").read_text())
    if no_development:
        grant["split"]["cutoff"] = 1
    if missing_baseline:
        import hashlib

        path = directory / "trace.jsonl"
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records[-1 if baseline_only else 0]["incumbent"] = {"status": "missing"}
        path.write_text("".join(canonical(row) + "\n" for row in records))
        grant["trace_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    store = LocalTrainingStore(
        directory / "training.sqlite3", grant["organization"], existing_only=True
    )
    ledger = LearningMandates(
        directory, store.organization, store.engagement_id, owner=grant["owner_id"]
    )
    mandate = json.loads((EXAMPLES / "pilot-learning-mandate.template.json").read_text())
    mandate.pop("model_fields")
    source_scope = {
        key: grant[key] for key in ("workflow", "field", "trace_source", "correction_source")
    }
    mandate.update(
        **source_scope,
        mandate_id="generated-benchmark-mandate",
        owner_id=grant["owner_id"],
        delegate=evaluation_delegate,
        trainer=evaluation_delegate,
        connectors=["generated-local"],
        fields=grant["permitted_features"],
        labels=["sale", "refund"],
        permitted_error_pairs=[["sale", "refund"]],
        agreement_revision=digest(policy),
        uses=["inspect", "evaluate"],
        profile=None,
        max_fits=0,
        max_total_seconds=0,
        max_seconds_per_fit=0,
        expires_at=int(time.time()) + 600,
        max_runs=1,
        max_rows_per_run=32,
        max_total_rows=32,
    )
    enrolled = ledger.enroll(mandate, actor=grant["owner_id"])
    source = ledger.authorize_source(
        enrolled["mandate_id"],
        {
            **source_scope,
            "connector": "generated-local",
            "fields": grant["permitted_features"],
            "rows": 32,
            "source_revision": digest([grant["trace_sha256"], grant["correction_sha256"]]),
            "trace_sha256": grant["trace_sha256"],
            "correction_sha256": grant["correction_sha256"],
        },
        actor=mandate["delegate"],
    )
    grant.update(
        expires_at=mandate["expires_at"],
        delegation={
            "authority_kind": source["authority_kind"],
            "run_id": source["run_id"],
            "delegate": mandate["delegate"],
        },
    )
    write(directory / f"cycle-source-{source['run_id']}.json", grant)
    selection = directory / "historical-selection.json"
    request = HistoricalPair(
        "generated-pair", grant["trace_sha256"], grant["correction_sha256"], digest(policy), "first"
    )
    write(
        selection,
        {
            "schema_version": SELECTION_VERSION,
            "tenant": store.organization,
            "actor": mandate["delegate"],
            "engagement_epoch": store.engagement_id,
            "mandate_id": enrolled["mandate_id"],
            "mandate_revision": enrolled["mandate_revision"],
            "source_run_id": source["run_id"],
            "workflow_ref": grant["workflow"],
            "pair_ref": request.pair_ref,
            "trace_source": grant["trace_source"],
            "correction_source": grant["correction_source"],
            "source_revision": request.source_revision,
            "correction_revision": request.correction_revision,
            "policy_revision": request.policy_revision,
        },
    )
    authority = LocalHistoricalAuthority(selection)
    intake = BenchmarkIntake(DurableIntake(directory, authority))
    producer = LocalHistoricalBenchmark(
        selection, directory / "trace.jsonl", directory / "corrections.jsonl", agreement
    )
    return intake, authority.principal, request, producer, ledger, mandate


def test_actual_standing_mandate_freezes_and_scores_recorded_predictions_without_fits(
    tmp_path, monkeypatch
):
    intake, actor, request, producer, ledger, mandate = setup(tmp_path)

    def forbidden(*args, **kwargs):
        raise AssertionError("fit, model or Slack publication was attempted")

    monkeypatch.setattr(repair_workflow, "_model_arm", forbidden)
    monkeypatch.setattr(repair_workflow, "_publication_permission", forbidden)
    monkeypatch.setattr(simple_repair, "fit_rule", forbidden)
    admission = intake.submit_intake(actor, request)
    assert intake.materialize(actor, admission.run_ref, producer)
    status = intake.get_run(actor, admission.run_ref)
    assert status.state == "materialized" and status.benchmark.benchmark_state == "scored"
    with sqlite3.connect(intake.benchmarks.path) as db:
        record = json.loads(db.execute("SELECT materialized FROM benchmarks").fetchone()[0])
    output = next((producer.directory / "managed-outputs").glob("historical-benchmark-*"))
    comparison = json.loads((output / "comparison.json").read_text())
    plan = json.loads((output / "plan.json").read_text())
    assert record["definition_digest"] == plan["revision"]
    assert record["result_digest"] == digest(comparison)
    scope = BenchmarkScope(
        actor.tenant, producer.authority.store.engagement_id, status.workflow_ref
    )
    assert (
        intake.benchmarks.match_scored_comparison(scope, 1, digest(comparison))
        == status.benchmark.benchmark_ref
    )
    assert intake.benchmarks.match_scored_comparison(scope, 2, digest(comparison)) is None
    assert intake.benchmarks.match_scored_comparison(scope, 1, "0" * 64) is None
    assert (
        intake.benchmarks.match_scored_comparison(
            replace(scope, workflow_ref="other-workflow"), 1, digest(comparison)
        )
        is None
    )
    assert comparison["protected_rows_evaluated"] == plan["protected_count"] == 4
    assert comparison["protected_membership_digest"] == plan["protected_membership_digest"]
    assert comparison["arms"]["incumbent"]["metrics"]["incorrect_automatic"] == 2
    assert comparison["arms"]["simple"]["metrics"]["incorrect_automatic"] == 0
    assert plan["model_revision"] is None and not (output / "model").exists()
    assert (
        not (output / "review.json").exists()
        and not (producer.directory / "slack-pilot.sqlite3").exists()
    )
    assert ledger.inspect(mandate["mandate_id"], actor=actor.actor)["reserved_fits"] == 0
    with sqlite3.connect(producer.authority.store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM train_data").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM protected_comparisons").fetchone()[0] == 1
    # Completed retry and readback remain pure receipt reads, even with execution disabled.
    monkeypatch.setattr(repair_workflow, "execute_managed", forbidden)
    assert intake.materialize(actor, admission.run_ref, producer)
    assert intake.submit_intake(actor, replace(request, idempotency_key="another")) == replace(
        admission, state="materialized"
    )


def test_real_producer_crash_after_comparison_recovers_from_actual_registry(tmp_path, monkeypatch):
    intake, actor, request, producer, _, _ = setup(tmp_path)
    admission = intake.submit_intake(actor, request)

    def crash(*args, **kwargs):
        raise RuntimeError("generated crash before benchmark publication")

    monkeypatch.setattr(intake.benchmarks, "publish", crash)
    with pytest.raises(RuntimeError):
        intake.materialize(actor, admission.run_ref, producer)
    assert intake.get_run(actor, admission.run_ref).benchmark.benchmark_state == "draft"
    authority = LocalHistoricalAuthority(producer.authority.path)
    reopened = BenchmarkIntake(DurableIntake(producer.directory, authority))

    def no_rescore(*args, **kwargs):
        raise AssertionError("crash recovery attempted another score")

    monkeypatch.setattr(repair_workflow, "execute_managed", no_rescore)
    assert reopened.materialize(actor, admission.run_ref, producer)
    assert reopened.get_run(actor, admission.run_ref).benchmark.benchmark_state == "scored"


@pytest.mark.parametrize(
    ("option", "reason"),
    [
        ("missing_baseline", "recorded_baseline_predictions_required"),
        ("missing_threshold", "benchmark_thresholds_required"),
    ],
)
def test_missing_baseline_or_threshold_has_specific_hold_without_score(tmp_path, option, reason):
    intake, actor, request, producer, _, _ = setup(tmp_path, **{option: True})
    admission = intake.submit_intake(actor, request)
    with pytest.raises(IntakeError, match=reason):
        intake.materialize(actor, admission.run_ref, producer)
    assert intake.get_run(actor, admission.run_ref).benchmark.benchmark_state == "draft"
    assert not (producer.directory / "managed-outputs").exists()


def test_changed_policy_and_withdrawn_mandate_deny_before_source_freeze(tmp_path):
    intake, actor, request, producer, ledger, mandate = setup(tmp_path)
    admission = intake.submit_intake(actor, request)
    policy = json.loads(producer.agreement.read_text())
    write(producer.agreement, {**policy, "incumbent_revision": "f" * 64})
    with pytest.raises(IntakeError, match="benchmark_policy_changed"):
        intake.materialize(actor, admission.run_ref, producer)
    assert not list(producer.directory.glob("benchmark-intake-*.json"))
    write(producer.agreement, policy)
    ledger.withdraw(mandate["mandate_id"], actor=mandate["owner_id"])
    with pytest.raises(IntakeError):
        intake.materialize(actor, admission.run_ref, producer)


def test_revocation_after_actual_score_prevents_benchmark_publication(tmp_path, monkeypatch):
    intake, actor, request, producer, ledger, mandate = setup(tmp_path)
    admission = intake.submit_intake(actor, request)
    execute = repair_workflow.execute_managed

    def revoke_after_score(*args, **kwargs):
        result = execute(*args, **kwargs)
        ledger.withdraw(mandate["mandate_id"], actor=mandate["owner_id"])
        return result

    monkeypatch.setattr(repair_workflow, "execute_managed", revoke_after_score)
    with pytest.raises(IntakeError):
        intake.materialize(actor, admission.run_ref, producer)
    with sqlite3.connect(intake.benchmarks.path) as db:
        assert db.execute("SELECT state,materialized FROM benchmarks").fetchone() == ("draft", None)


def test_recovery_rejects_mismatched_actual_registry_without_rescoring(tmp_path, monkeypatch):
    intake, actor, request, producer, _, _ = setup(tmp_path)
    admission = intake.submit_intake(actor, request)

    def crash(*args, **kwargs):
        raise RuntimeError("generated crash before publication")

    monkeypatch.setattr(intake.benchmarks, "publish", crash)
    with pytest.raises(RuntimeError):
        intake.materialize(actor, admission.run_ref, producer)
    with sqlite3.connect(producer.authority.store.path) as db:
        revision, original = db.execute(
            "SELECT revision,result FROM protected_comparisons"
        ).fetchone()
        changed = json.loads(original)
        changed["protected_rows_evaluated"] = 999
        db.execute("UPDATE protected_comparisons SET result=?", (canonical(changed),))
    authority = LocalHistoricalAuthority(producer.authority.path)
    reopened = BenchmarkIntake(DurableIntake(producer.directory, authority))

    def no_rescore(*args, **kwargs):
        raise AssertionError("receipt reconciliation tried another score")

    monkeypatch.setattr(repair_workflow, "execute_managed", no_rescore)
    with pytest.raises(ValueError, match="protected_result_digest_mismatch"):
        reopened.materialize(actor, admission.run_ref, producer)
    with sqlite3.connect(producer.authority.store.path) as db:
        db.execute(
            "UPDATE protected_comparisons SET result=? WHERE revision=?", (original, revision)
        )
    assert reopened.materialize(actor, admission.run_ref, producer)


@pytest.mark.parametrize(
    "same_audience", [True, False], ids=["same-audience", "separate-audiences"]
)
def test_historical_worker_and_separately_granted_review_join_only_exact_frozen_result(
    tmp_path, same_audience
):
    from opentour_cell.pilot.managed_run import ManagedSelection, plan_revision
    from test_reviewer_workflow import buttons, restart, screen
    from test_slack_adapter import ACTOR, Harness
    from test_status_questions import counts

    h = Harness(tmp_path / "workspace")
    recipient = h.config["recipient"]
    intake, actor, request, producer, _, _ = setup(
        tmp_path, evaluation_delegate=recipient if same_audience else "generated-worker"
    )
    admitted = intake.submit_intake(actor, request)
    assert intake.materialize(actor, admitted.run_ref, producer)
    directory = producer.directory
    work = next((directory / "managed-outputs").glob("historical-benchmark-*"))
    historical = json.loads((work / "comparison.json").read_bytes())
    manifest = json.loads((work / "benchmark-manifest.json").read_bytes())
    assert historical["audience"] == actor.actor
    h.path = directory / "slack-config.json"
    h.store = producer.authority.store
    h.config.update(directory=str(directory), organization=actor.tenant)
    h.config["users"][ACTOR]["datasets"] = ["generated-repair"]
    h.save()
    restart(h)
    question = "Graph baseline versus rule on v1"
    before = counts(h)
    # Historical scoring alone grants neither discovery nor Slack disclosure.
    assert screen(h, h.launch(prompt=question))["callback_id"] == "ot_query_hold"
    manifest.update(
        audience=recipient,
        slack_review={
            "team": h.config["team"],
            "channel": h.config["channel"],
            "audience": recipient,
            "expires_at": int(time.time()) + 420,
            "owner_attestation": "att-abcdef0123456789",
            "aggregate": True,
        },
    )
    path = directory / "review-managed.json"
    write(path, manifest)
    repair_workflow.grant_slack_review(directory, actor.tenant, manifest)
    queue = ManagedSelection(directory, actor.tenant, path, directory / "review-queue-grant.json")
    revision = plan_revision(manifest)
    queue.authorize(
        revision,
        "att-abcddcbaabcddcba",
        int(time.time()) + 400,
        None,
        repair_workflow.inspect_managed,
    )
    started = queue.start(revision, "generated-rule-review")
    state = queue.once(repair_workflow.execute_managed)["state"]
    view = h.launch(prompt=question)
    if same_audience:
        assert state == "ready"
        reviewed = json.loads(
            (directory / "managed-outputs" / started["run_id"] / "comparison.json").read_bytes()
        )
        assert digest(reviewed) == digest(historical)
        selected = buttons(screen(h, view))["ot_result_open"]
        h.action(view, "ot_result_open", selected)
        assert screen(h, view)["callback_id"] == "ot_arm_chart"
        view = h.launch(prompt=question)
        selected = buttons(screen(h, view))["ot_result_open"]
        (directory / f"slack-review-{revision}.json").unlink()
        h.action(view, "ot_result_open", selected)
        assert screen(h, view).get("callback_id") != "ot_arm_chart"
    else:
        assert state == "held"
        assert screen(h, view)["callback_id"] == "ot_query_hold"
    assert counts(h) == before
