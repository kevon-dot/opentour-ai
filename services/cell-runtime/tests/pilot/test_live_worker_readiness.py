"""Actual export ingestion refreshes durable readiness; reads never run the worker."""

from opentour_cell.pilot import live_reader
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.readiness_selection import SCHEMA, open_readiness
from test_intake_mcp import _write_private
from test_live_evidence import DECISION, decision
from test_live_reader import Export


def configured(path):
    export = Export(path)
    export.case.body["actors"]["worker"] = ["intake", "inspect"]
    export.case.write()
    export.install(DECISION, [[export.row(decision(), 11)]])
    selection = dict(
        schema_version=SCHEMA,
        workflow_ref=export.case.workflow,
        worker_ref="worker",
        policy_file=None,
    )
    _write_private(export.directory / "readiness-selection.json", selection)
    return export, selection


def test_real_worker_records_readiness_and_retry_does_not_reread_source(tmp_path):
    export, _ = configured(tmp_path / "owner")
    run = export.submit(DECISION)
    assert live_reader.main([str(export.selection), run]) == 0
    components = open_readiness(export.directory, "org-a", export.case.workflow)
    receipt = components.readiness.read(export.case.workflow, actor=export.case.owner)
    assert receipt["reason_code"] == "no_verifier_bound"
    assert receipt["pending_count"] == 1 and receipt["verified_root_count"] == 0
    for page in export.directory.glob("live-export-page-*.json"):
        page.unlink()
    before = export.receipts()
    assert live_reader.main([str(export.selection), run]) == 0
    assert export.receipts() == before
    assert components.readiness.read(export.case.workflow, actor=export.case.owner) == receipt
    actor, intake = open_intake(export.selection)
    assert intake.get_run(actor, run).state == "materialized"


def test_retry_recovers_readiness_failure_after_source_commit(tmp_path):
    export, selection = configured(tmp_path / "owner")
    run = export.submit(DECISION)
    _write_private(
        export.directory / "readiness-selection.json", {**selection, "worker_ref": "not-authorized"}
    )
    assert live_reader.main([str(export.selection), run]) == 1
    assert export.snapshot().pending == 1
    before = export.receipts()
    _write_private(export.directory / "readiness-selection.json", selection)
    for page in export.directory.glob("live-export-page-*.json"):
        page.unlink()
    assert live_reader.main([str(export.selection), run]) == 0
    assert export.receipts() == before
    components = open_readiness(export.directory, "org-a", export.case.workflow)
    assert (
        components.readiness.read(export.case.workflow, actor=export.case.owner)["pending_count"]
        == 1
    )


def test_unavailable_post_commit_receipt_is_not_reported_as_complete(tmp_path, monkeypatch):
    from opentour_cell.pilot.live_readiness import LiveReadinessSource

    export, _ = configured(tmp_path / "owner")
    run = export.submit(DECISION)
    monkeypatch.setattr(LiveReadinessSource, "snapshot", lambda *args: None)
    assert live_reader.main([str(export.selection), run]) == 1


def test_other_workflow_readiness_selection_does_not_block_this_export(tmp_path):
    export, selection = configured(tmp_path / "owner")
    run = export.submit(DECISION)
    _write_private(
        export.directory / "readiness-selection.json",
        {**selection, "workflow_ref": "different-workflow"},
    )
    assert live_reader.main([str(export.selection), run]) == 0
    assert export.snapshot().pending == 1
