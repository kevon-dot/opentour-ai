"""Owner-local installation display labels can change independently of admitted workflow IDs."""

import json
import sqlite3
from contextlib import closing

from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.starter_setup import StarterSetup
from test_app_home import publish
from test_historical_worker import invoke, prepared
from test_intake_mcp import _sealed_starter, _write_private
from test_reviewer_workflow import restart
from test_slack_adapter import ACTOR, Harness
from test_starter_benchmark import records, saved

pytest_plugins = ("test_starter_setup",)


def display(h, monkeypatch, label):
    h.config["workflow_label"] = label
    h.save()
    restart(h)
    return json.dumps(publish(h, monkeypatch))


def test_rename_keeps_current_and_as_of_starter_identity(founder, monkeypatch):
    h = founder
    directory = h.path.parent
    saved(h, monkeypatch)
    selection = directory / "selected.json"
    grant = directory / "starter-intake-grant-policy-scope.json"
    initial = json.loads(grant.read_bytes())
    successor = _sealed_starter(
        json.loads(selection.read_bytes()),
        source_revision="policy-revision-two",
        setup_policy_revision=digest(json.loads((directory / "charter.json").read_bytes())),
    )
    _write_private(grant, successor)
    saved(h, monkeypatch)
    first, second = records(h)
    assert first[0] == second[0] and first[6] == 1 and second[4] == first[2]

    def status():
        results = []
        for permission, row, expected in (
            (initial, first, "superseded"),
            (successor, second, "draft"),
        ):
            # Reopen each original source permission before reading its admitted run.
            _write_private(grant, permission)
            actor, api = open_intake(selection)
            run = api.get_run(actor, row[1])
            preview = StarterSetup(h.app.settings, ACTOR).preview()
            assert run.workflow_ref == row[0] and run.benchmark.benchmark_state == expected
            assert preview.benchmark == run.benchmark
            assert preview.draft["run_ref"] == run.run_ref
            results.append((run, preview.draft))
        return results

    before = status()
    assert "First invoice route" in display(h, monkeypatch, "First invoice route")
    updated = display(h, monkeypatch, "Revised invoice route")
    assert "Revised invoice route" in updated and "First invoice route" not in updated
    assert status() == before and records(h) == [first, second]


def test_historical_scored_receipt_survives_installation_label_restart(tmp_path, monkeypatch):
    _, _, admission, producer, _, _, config, _ = prepared(tmp_path, no_development=True)
    assert invoke(config, admission.run_ref)[0] == 0
    directory = producer.directory
    h = Harness(tmp_path / "display", organization=producer.authority.principal.tenant)
    h.path = directory / "slack-settings.json"
    h.config.update(directory=str(directory), zapier=False)
    h.store = producer.authority.store
    with closing(sqlite3.connect(directory / "workflow-benchmarks.sqlite3")) as db:
        [(operation,)] = db.execute(
            "SELECT operation_ref FROM benchmark_operations WHERE run=? AND completed=1",
            (admission.run_ref,),
        ).fetchall()

    def historical():
        actor, api = open_intake(directory / "historical-selection.json")
        run = api.get_run(actor, admission.run_ref)
        with api.intake.admitted_work(actor, run.run_ref) as admitted_work:
            work = admitted_work
        receipt = producer.readback(actor, work, operation_ref=operation)
        assert run.benchmark.benchmark_state == receipt.state == "scored"
        assert receipt.result_ref and receipt.result_digest
        return run, receipt

    before = historical()
    assert "Original invoice route" in display(h, monkeypatch, "Original invoice route")
    updated = display(h, monkeypatch, "Renamed invoice route")
    assert "Renamed invoice route" in updated and "Original invoice route" not in updated
    assert historical() == before
