"""The private starter UI projects the benchmark created by ordinary local intake."""

import json
import sqlite3

import test_intake_mcp as stdio
from opentour_cell.pilot.live_authority import SELECTION_SCHEMA
from opentour_cell.pilot.operating_point import digest
from test_intake_mcp import _sealed_starter, _write_private
from test_live_settings import form, open_live, sign
from test_reviewer_workflow import buttons, restart, screen, text
from test_starter_setup import effects, open_starter

pytest_plugins = ("test_live_settings",)


def saved(h, monkeypatch):
    view = open_starter(h, monkeypatch)
    h.action(view, "ot_starter_save", buttons(screen(h, view))["ot_starter_save"])
    assert screen(h, view)["callback_id"] == "ot_starter_saved"
    return view


def records(h):
    with sqlite3.connect(h.path.parent / "workflow-benchmarks.sqlite3") as db:
        return db.execute(
            "SELECT workflow,run,reference,version,parent,state,superseded "
            "FROM benchmarks ORDER BY version"
        ).fetchall()


def test_normal_starter_and_successor_report_actual_benchmark(founder, monkeypatch):
    h = founder
    first = saved(h, monkeypatch)
    original = records(h)[0]
    assert original[2] in text(h, first) and "version 1" in text(h, first)
    assert original[5:] == ("draft", 0)
    assert "ot_starter_benchmark" not in buttons(screen(h, first))
    restart(h)
    assert original[2] in text(h, open_starter(h, monkeypatch))
    previous = [p.read_bytes() for p in h.path.parent.glob("source-intake-starter-draft-*.json")]
    selection = json.loads((h.path.parent / "selected.json").read_bytes())
    charter = json.loads((h.path.parent / "charter.json").read_bytes())
    grant_file = h.path.parent / "starter-intake-grant-policy-scope.json"
    first_grant = json.loads(grant_file.read_bytes())
    updated = _sealed_starter(
        selection, source_revision="new-policy-v2", setup_policy_revision=digest(charter)
    )
    _write_private(h.path.parent / "starter-intake-grant-policy-scope.json", updated)
    second = saved(h, monkeypatch)
    first_row, second_row = records(h)
    assert first_row[-1] == 1 and second_row[0] == first_row[0]
    assert second_row[3:6] == (2, first_row[2], "draft") and second_row[2] in text(h, second)
    assert previous[0] in [
        p.read_bytes() for p in h.path.parent.glob("source-intake-starter-draft-*.json")
    ]
    _write_private(grant_file, first_grant)
    old_view = open_starter(h, monkeypatch)
    assert original[2] in text(h, old_view) and "superseded" in text(h, old_view)
    assert second_row[2] not in text(h, old_view) and len(records(h)) == 2
    assert effects(h) == (0,) * 7


def test_legacy_saved_starter_reads_without_writing_and_explicitly_recovers_benchmark(
    founder, monkeypatch
):
    h = founder
    saved(h, monkeypatch)
    receipt = next(h.path.parent.glob("source-intake-starter-draft-*.json"))
    old = receipt.read_bytes()
    approval = json.loads((h.path.parent / "starter-intake-grant-policy-scope.json").read_bytes())[
        "grant"
    ]
    legacy = digest([h.store.organization, h.store.engagement_id, approval["creation_ref"]])
    path = h.path.parent / f"source-intake-starter-draft-{legacy}.json"
    receipt.rename(path)
    ledger = h.path.parent / "workflow-benchmarks.sqlite3"
    ledger.unlink()
    with sqlite3.connect(h.path.parent / "intake.sqlite3") as db:
        db.execute("DROP TABLE intake_benchmark_admissions")
    view = open_starter(h, monkeypatch)
    assert "no current benchmark receipt" in text(h, view) and not ledger.exists()
    h.action(view, "ot_starter_benchmark", buttons(screen(h, view))["ot_starter_benchmark"])
    (row,) = records(h)
    assert row[2] in text(h, view) and row[3] == 1 and path.read_bytes() == old
    assert effects(h) == (0,) * 7


def test_real_live_binding_leaves_starter_benchmark_bytes_unchanged(
    connected_settings, monkeypatch
):
    h, body = connected_settings
    ledger = h.path.parent / "workflow-benchmarks.sqlite3"
    before = ledger.read_bytes()
    (row,) = records(h)
    assert row[0] == body["workflow_ref"]
    view = open_live(h, monkeypatch)
    for i in (0, 0, 1):
        h.form(view, form(h, view, i))
    restart(h)
    view = open_starter(h, monkeypatch)
    assert row[2] in text(h, view) and records(h) == [row] and ledger.read_bytes() == before
    assert effects(h) == (0,) * 7


def test_actual_stdio_starter_and_live_status_preserve_one_benchmark(
    connected_settings, monkeypatch
):
    h, body = connected_settings
    (row,) = records(h)
    runner = stdio.TestIntakeMcp()
    starter = h.path.parent / "selected.json"
    status = runner.run_status(row[1], selection=starter)
    assert (
        status["workflow_ref"],
        status["benchmark_ref"],
        status["version"],
        status["benchmark_state"],
    ) == (row[0], row[2], 1, "draft")
    body["actors"]["feed-reader"] = ["intake"]
    sign(h, body)
    view = open_live(h, monkeypatch)
    h.form(view, form(h, view))
    with sqlite3.connect(h.path.parent / "live-evidence.sqlite3") as db:
        (feed,) = db.execute("SELECT ref FROM live_bindings").fetchone()
    selected = h.path.parent / "live-selection.json"
    _write_private(
        selected,
        {
            "schema_version": SELECTION_SCHEMA,
            "tenant": h.store.organization,
            "workflow_ref": row[0],
            "actor": "feed-reader",
        },
    )
    ledger = h.path.parent / "workflow-benchmarks.sqlite3"
    before = ledger.read_bytes()
    request = {
        "mode": "live",
        "source_revision": "source-v1",
        "feed_binding_ref": feed,
        "idempotency_key": "live-from-script",
    }
    first = runner.submit(request, selection=selected)
    duplicate = runner.submit(
        {**request, "idempotency_key": "retry-after-restart"}, selection=selected
    )
    assert first == duplicate
    live_status = runner.run_status(first["run_ref"], selection=selected)
    assert live_status["workflow_ref"] == row[0] and "benchmark_ref" not in live_status
    assert runner.run_status(row[1], selection=starter) == status
    assert records(h) == [row] and ledger.read_bytes() == before and effects(h) == (0,) * 7


def test_corrupted_intake_identity_reveals_no_benchmark_or_draft(founder, monkeypatch):
    h = founder
    saved(h, monkeypatch)
    (row,) = records(h)
    with sqlite3.connect(h.path.parent / "intake.sqlite3") as db:
        db.execute("UPDATE intake_runs SET intent=? WHERE run_ref=?", ("0" * 64, row[1]))
    view = open_starter(h, monkeypatch)
    assert row[2] not in text(h, view) and row[0] not in text(h, view)
    assert records(h) == [row] and effects(h) == (0,) * 7
