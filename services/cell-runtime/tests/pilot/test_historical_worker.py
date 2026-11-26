"""Actual historical worker command and generated private standing-mandate sources."""

import json
import sqlite3
import subprocess
import sys

import pytest
from opentour_cell.pilot import comparison_plan, historical_worker
from opentour_cell.pilot.workflow_benchmarks import WorkflowBenchmarks
from test_historical_benchmarks import setup, write


def prepared(tmp_path, **options):
    intake, actor, request, producer, ledger, mandate = setup(
        tmp_path, baseline_only=True, **options
    )
    admitted = intake.submit_intake(actor, request)
    config = producer.directory / "historical-worker.json"
    value = {
        "schema_version": historical_worker.CONFIG,
        "selection": "historical-selection.json",
        "traces": "trace.jsonl",
        "corrections": "corrections.jsonl",
        "agreement": "benchmark-policy.json",
    }
    write(config, value)
    return intake, actor, admitted, producer, ledger, mandate, config, value


def invoke(config, run):
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.historical_worker",
            "--config",
            str(config),
            "--run-ref",
            run,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not result.stderr
    return result.returncode, json.loads(result.stdout)


def test_worker_fresh_process_and_restarts_produce_one_actual_baseline_without_fits(tmp_path):
    intake, actor, admitted, producer, ledger, mandate, config, _ = prepared(
        tmp_path, no_development=True
    )
    first = invoke(config, admitted.run_ref)
    assert first[0] == 0 and first[1]["status"] == "completed"
    assert first[1]["benchmark"]["benchmark_state"] == "scored"
    assert invoke(config, admitted.run_ref) == first
    assert invoke(config, admitted.run_ref) == first
    assert intake.get_run(actor, admitted.run_ref).benchmark.version == 1
    with sqlite3.connect(producer.authority.store.path) as db:
        assert (
            db.execute(
                "SELECT count(*) FROM protected_comparisons WHERE result IS NOT NULL"
            ).fetchone()[0]
            == 1
        )
        for table in ("jobs", "train_data"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    assert ledger.inspect(mandate["mandate_id"], actor=actor.actor)["reserved_fits"] == 0
    results = list((producer.directory / "managed-outputs").glob("*/comparison.json"))
    assert len(results) == 1
    result = json.loads(results[0].read_bytes())
    assert result["decision"]["reason"] == "recorded_baseline_only"
    assert result["protected_rows_evaluated"] == 16


def test_worker_crash_recovers_actual_receipt_without_rescoring(tmp_path, monkeypatch):
    _, _, admitted, _, _, _, config, _ = prepared(tmp_path)
    original = WorkflowBenchmarks.publish

    def interrupted(*args, **kwargs):
        raise RuntimeError("generated interruption after actual result")

    monkeypatch.setattr(WorkflowBenchmarks, "publish", interrupted)
    with pytest.raises(RuntimeError):
        historical_worker.execute(config, admitted.run_ref)
    monkeypatch.setattr(WorkflowBenchmarks, "publish", original)

    def no_score(*args, **kwargs):
        raise AssertionError("retry attempted a second score")

    monkeypatch.setattr(comparison_plan, "run_recorded_baseline", no_score)
    assert historical_worker.execute(config, admitted.run_ref)["status"] == "completed"


def test_worker_missing_predictions_has_specific_safe_hold(tmp_path):
    intake, actor, admitted, _, _, _, config, _ = prepared(tmp_path, missing_baseline=True)
    code, result = invoke(config, admitted.run_ref)
    assert code == 2
    assert result == {
        "schema_version": historical_worker.RESULT,
        "status": "held",
        "reason": "recorded_baseline_predictions_required",
    }
    assert intake.get_run(actor, admitted.run_ref).benchmark.benchmark_state == "draft"


@pytest.mark.parametrize("change", ["source", "policy", "revocation", "foreign_run"])
def test_worker_current_authority_and_sources_are_rechecked(tmp_path, change):
    _, actor, admitted, producer, ledger, mandate, config, _ = prepared(tmp_path)
    run = admitted.run_ref
    if change == "source":
        producer.traces.write_bytes(producer.traces.read_bytes() + b"\n")
    elif change == "policy":
        producer.agreement.write_text("{}")
    elif change == "revocation":
        ledger.withdraw(mandate["mandate_id"], actor=mandate["owner_id"])
    else:
        run = "run-not-admitted"
    code, result = invoke(config, run)
    assert code == 2 and result["status"] == "held"
    assert "benchmark" not in result and str(config.parent) not in json.dumps(result)
    assert not (producer.directory / "managed-outputs").exists()


@pytest.mark.parametrize("change", ["traversal", "symlink", "extra_authority", "nonprivate"])
def test_worker_configuration_cannot_grant_rights_or_escape_private_directory(tmp_path, change):
    _, _, admitted, producer, _, _, config, value = prepared(tmp_path)
    if change == "traversal":
        value["traces"] = "../trace.jsonl"
        write(config, value)
    elif change == "symlink":
        alias = config.parent / "alias.json"
        alias.symlink_to(config)
        config = alias
    elif change == "extra_authority":
        value["evaluation_permission"] = True
        write(config, value)
    else:
        config.chmod(0o644)
    code, result = invoke(config, admitted.run_ref)
    assert code == 2 and result["reason"] == "historical_worker_unavailable"
    assert not (producer.directory / "managed-outputs").exists()
