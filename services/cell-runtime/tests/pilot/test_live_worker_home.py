"""Local generated export, worker command and emitted Home against the same persisted store."""

import json
import sqlite3
import subprocess
import sys

from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import parse_intake
from opentour_cell.pilot.training import LocalTrainingStore
from test_app_home import publish
from test_intake_mcp import STARTER
from test_live_evidence import DECISION, OUTCOME, decision, outcome
from test_live_settings import form, open_live
from test_live_worker_readiness import configured
from test_reviewer_workflow import restart
from test_slack_adapter import ACTOR, Harness
from test_starter_setup import effects
from test_workflow_benchmarks import DiskProducer


def worker_home_witness(tmp_path, monkeypatch, command):
    sentinel = "GENERATED_PRIVATE_SOURCE_SENTINEL"
    export, _ = configured(tmp_path / sentinel)
    case = export.case
    h = Harness(tmp_path / "slack", organization="org-a")
    h.path = case.directory / "slack.json"
    h.config.update(directory=str(case.directory), live_workflow_ref=case.workflow)
    h.config["users"][ACTOR]["datasets"] = [case.body["dataset_ref"]]
    h.store = LocalTrainingStore(case.directory / "training.sqlite3", "org-a", existing_only=True)
    case.body["actors"][ACTOR] = ["custodian", "inspect"]
    case.body["custodian"] = ACTOR
    case.write()
    h.save()
    restart(h)
    actor, starter = open_intake(case.directory / "selection.json")
    original = starter.submit_intake(actor, parse_intake(STARTER))
    producer = DiskProducer(case.directory)
    producer.state = "frozen_unscored"
    assert starter.materialize(actor, original.run_ref, producer, target_state="frozen_unscored")
    frozen = starter.get_run(actor, original.run_ref).benchmark
    assert frozen.benchmark_state == "frozen_unscored"
    ledger = case.directory / "workflow-benchmarks.sqlite3"
    before = ledger.read_bytes()

    def ingest(grant, index, batches):
        view = open_live(h, monkeypatch)
        h.form(view, form(h, view, index))
        with sqlite3.connect(case.live.path) as db:
            (ref,) = db.execute(
                "SELECT ref FROM live_bindings WHERE kind=?", (grant.kind,)
            ).fetchone()
        export.install(grant, batches)
        worker, intake = open_intake(export.selection)
        receipt = intake.submit_intake(
            worker,
            parse_intake(
                {
                    "mode": "live",
                    "feed_binding_ref": ref,
                    "source_revision": grant.source_revision,
                    "idempotency_key": grant.resource,
                }
            ),
        )
        for expected in ("materialized", "already_materialized"):
            result = subprocess.run(
                [*command, str(export.selection), receipt.run_ref],
                text=True,
                capture_output=True,
                check=False,
                timeout=30,
            )
            assert (result.returncode, result.stdout.strip(), result.stderr) == (0, expected, "")
        restart(h)
        return json.dumps(publish(h, monkeypatch), ensure_ascii=False)

    pending_event = decision(decision_id="D2", input_ref=sentinel, input_fidelity="observed")
    pending = ingest(DECISION, 0, [[export.row(pending_event, 11)]])
    assert "1 decision awaiting verification" in pending and "No verifier bound" in pending
    assert "1 row, complete" in pending and sentinel not in pending
    first = outcome()
    second = outcome(event_id="different-delivery", outcome_id="O2")
    late = ingest(OUTCOME, 1, [[export.row(first, 21)], [export.row(second, 22)]])
    assert "Unmatched outcomes · 2" in late and "Awaiting outcome" in late and sentinel not in late
    assert "Coverage through 1970-01-01 00:01:40 UTC" in late
    assert (
        starter.get_run(actor, original.run_ref).benchmark == frozen
        and ledger.read_bytes() == before
    )
    assert effects(h) == (0,) * 7
    with sqlite3.connect(case.directory / "learning-mandates.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone() == (0,)
    assert sentinel not in json.dumps(h.slack.post)
    return frozen.benchmark_ref, len(export.receipts())


def test_source_worker_to_private_home_and_unchanged_frozen_benchmark(tmp_path, monkeypatch):
    _, receipts = worker_home_witness(
        tmp_path, monkeypatch, [sys.executable, "-m", "opentour_cell.pilot.live_reader"]
    )
    assert receipts == 3
