"""Use the executable invoice example and independent customer-local intake fixtures."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict, replace
from importlib.metadata import version
from pathlib import Path

from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import DISCOVER, GET_RUN, SUBMIT, parse_intake
from opentour_cell.pilot.live_evidence import _digest
from opentour_cell.pilot.training import LocalTrainingStore
from test_intake_mcp import STARTER, _write_private
from test_live_evidence import DECISION, OUTCOME, outcome
from test_live_reader import Export
from test_live_settings import form, open_live
from test_reviewer_workflow import restart, text
from test_slack_adapter import ACTOR, Harness
from test_starter_setup import effects
from test_workflow_benchmarks import DiskProducer

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/invoice_decision_emitter.py"
SENTINEL = "GENERATED_PRIVATE_INVOICE_PAYLOAD"


def scope_file(directory, *, workflow="generated-invoice-flow", epoch="generated-epoch"):
    directory.mkdir(mode=0o700)
    configured = directory / "invoice-emitter-scope.json"
    _write_private(
        configured,
        {
            "schema_version": "opentour.example.invoice-decision-emitter.v1",
            "tenant": "org-a",
            "workflow": workflow,
            "engagement_epoch": epoch,
            "namespace": DECISION.deciding_namespace,
            "policy_ref": "invoice-policy-v1",
            "checkpoint": 10,
        },
    )
    return configured


def emit(scope, *args, success=True):
    output = subprocess.run(
        [sys.executable, "-B", str(EXAMPLE), "--scope", str(scope), *map(str, args)],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert SENTINEL not in output.stdout + output.stderr
    if not success:
        assert output.returncode == 2 and output.stdout == ""
        assert output.stderr == "invoice_decision_emitter_unavailable\n"
        return None
    assert output.returncode == 0, output.stderr
    return json.loads(output.stdout)


def attempt(scope, operation, ref):
    return emit(scope, "attempt", "--operation", operation, "--attempt", ref)


def commit(scope, decision, provider_attempt, *, action="freight", success=True):
    return emit(
        scope,
        "commit",
        "--operation",
        f"operation-{decision}",
        "--attempt",
        provider_attempt,
        "--decision",
        decision,
        "--case",
        "generated-invoice-1",
        "--family",
        "generated-supplier-1",
        "--action",
        action,
        "--decision-at",
        100,
        success=success,
    )


def export_page(scope, ref, after, through, *, success=True):
    receipt = emit(
        scope, "export", "--page-ref", ref, "--after", after, "--through", through, success=success
    )
    if receipt is None:
        return None
    path = scope.parent / receipt["file"]
    assert path.stat().st_mode & 0o077 == 0
    content = path.read_text()
    assert SENTINEL not in content
    return receipt, json.loads(content)


def test_example_distinguishes_attempts_and_commits_through_restart(tmp_path):
    scope = scope_file(tmp_path.resolve() / "application")
    emit(scope, "initialize")
    attempt(scope, "operation-D1", "attempt-1")
    attempt(scope, "operation-D1", "attempt-1")
    attempt(scope, "operation-D1", "attempt-2")
    empty_receipt, empty = export_page(scope, "empty-page", 10, 10)
    assert empty_receipt["commits"] == 0 and empty["rows"] == []
    commit(scope, "D1", "unknown-attempt", success=False)

    first = commit(scope, "D1", "attempt-1")
    assert commit(scope, "D1", "attempt-2") == first
    commit(scope, "D1", "attempt-2", action="review", success=False)
    exported = export_page(scope, "first-page", 10, 11)
    receipt, page = exported
    [row] = page["rows"]
    assert receipt["commits"] == 1 and row["position"] == first["checkpoint"] == 11
    assert row["event"]["event_id"] == first["event_ref"]
    assert row["event"]["input_ref"] is None and row["event"]["model_ref"] is None

    attempt(scope, "operation-D2", "attempt-3")
    second = commit(scope, "D2", "attempt-3")
    assert second["checkpoint"] == 12 and second["event_ref"] != first["event_ref"]
    assert export_page(scope, "first-page", 10, 11) == exported
    export_page(scope, "first-page", 10, 12, success=False)
    _, second_page = export_page(scope, "second-page", 11, 12)
    [second_row] = second_page["rows"]
    assert second_row["event"]["case_id"] == row["event"]["case_id"]
    assert second_row["event"]["family_id"] == row["event"]["family_id"]
    assert second_row["event"]["decision_revision"] == 1

    emit(
        scope,
        "attempt",
        "--operation",
        "opaque",
        "--attempt",
        "opaque-2",
        "--raw",
        SENTINEL,
        success=False,
    )
    commit(scope, "D1", "attempt-1", action=SENTINEL, success=False)
    scope_record = json.loads(scope.read_text())
    _write_private(scope, scope_record | {"tenant": "foreign-tenant"})
    export_page(scope, "foreign-page", 10, 12, success=False)
    with closing(sqlite3.connect(scope.parent / "invoice-decisions.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone() == (3,)
        assert db.execute("SELECT count(*) FROM commits").fetchone() == (2,)


def sdk_actions(selection, actions):
    """Use a new official SDK session for each observation of worker durability."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def invoke():
        options = StdioServerParameters(
            command=sys.executable,
            args=["-B", "-m", "opentour_cell.pilot.intake_local", str(selection)],
            env={
                key: os.environ[key]
                for key in ("PYTHONPATH", "OPENTOUR_UNIT_GUARD")
                if key in os.environ
            },
        )
        async with (
            stdio_client(options) as (reader, writer),
            ClientSession(reader, writer) as client,
        ):
            await client.initialize()
            listed = await client.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            assert set(tools) == {DISCOVER, SUBMIT, GET_RUN}
            live_schema = tools[SUBMIT].inputSchema
            assert '"feed_binding_ref"' in json.dumps(live_schema)
            assert '"source_revision"' in json.dumps(live_schema)
            return [await client.call_tool(tool, args) for tool, args in actions]

    return asyncio.run(invoke())


def invoice_connection_probe(tmp_path, monkeypatch):
    """Run separately in the local environment containing the pinned official MCP SDK."""
    export = Export(tmp_path.resolve() / "worker")
    case = export.case
    case.body["grants"] = [asdict(DECISION)]
    case.body["actors"][ACTOR] = ["custodian", "inspect"]
    case.body["custodian"] = ACTOR
    case.write()
    h = Harness(tmp_path / "slack", organization="org-a")
    h.path = case.directory / "slack.json"
    h.config.update(
        directory=str(case.directory), live_workflow_ref=case.workflow, reviewer="UUNBOUND"
    )
    del h.config["recipient"]
    h.config["users"][ACTOR]["datasets"] = [case.body["dataset_ref"]]
    h.store = LocalTrainingStore(case.directory / "training.sqlite3", "org-a", existing_only=True)
    h.save()
    restart(h)
    owner, starter = open_intake(case.directory / "selection.json")
    original = starter.submit_intake(owner, parse_intake(STARTER))
    producer = DiskProducer(case.directory)
    producer.state = "frozen_unscored"
    assert starter.materialize(owner, original.run_ref, producer, target_state="frozen_unscored")
    frozen = starter.get_run(owner, original.run_ref).benchmark
    assert frozen.benchmark_state == "frozen_unscored"

    def bind(grant, index=0):
        for _ in range(2):
            view = open_live(h, monkeypatch)
            h.form(view, form(h, view, index))
            assert "Saved binding" in text(h, view)
            restart(h)
        with closing(sqlite3.connect(case.live.path)) as db:
            [(ref,)] = db.execute(
                "SELECT ref FROM live_bindings WHERE kind=? AND successor IS NULL",
                (grant.kind,),
            ).fetchall()
        assert ref in text(h, open_live(h, monkeypatch))
        return ref

    def install_app_page(grant, receipt, page):
        [entry] = export.install(grant, [page["rows"]])
        pin = export.resources[grant.resource]
        catalog = json.loads((export.directory / pin["file"]).read_bytes())
        rows = page["rows"]
        entry.update(
            page_ref=page["page_ref"],
            pin={"file": receipt["file"], "sha256": receipt["sha256"]},
            checkpoint=receipt["checkpoint"],
            page_digest=_digest(
                [
                    grant.source_revision,
                    [row["event"] for row in rows],
                    [row["position"] for row in rows],
                    [row["epoch"] for row in rows],
                    receipt["checkpoint"],
                    entry["read_metadata"],
                ]
            ),
        )
        shutil.copyfile(scope.parent / receipt["file"], export.directory / receipt["file"])
        (export.directory / receipt["file"]).chmod(0o600)
        export.resources[grant.resource] = export.pin(pin["file"], catalog | {"pages": [entry]})
        export.sign()

    def materialize(ref, expected="materialized"):
        output = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "opentour_cell.pilot.live_reader",
                str(export.selection),
                ref,
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        assert (output.returncode, output.stdout.strip(), output.stderr) == (0, expected, "")

    scope = scope_file(
        tmp_path.resolve() / "application",
        workflow=export.case.workflow,
        epoch=export.case.body["engagement_epoch"],
    )
    emit(scope, "initialize")
    attempt(scope, "operation-D1", "attempt-1")
    attempt(scope, "operation-D1", "attempt-2")
    commit(scope, "D1", "attempt-2")
    first_page_receipt, page = export_page(scope, "first-page", 10, 11)

    # The separately scoped custodian installs and pins pages. The example app has no custodian key.
    install_app_page(DECISION, first_page_receipt, page)
    decision_binding = bind(DECISION)
    ledger = export.directory / "workflow-benchmarks.sqlite3"
    original_benchmark = ledger.read_bytes()
    request = {
        "mode": "live",
        "feed_binding_ref": decision_binding,
        "source_revision": DECISION.source_revision,
        "idempotency_key": "application-live-1",
    }
    catalog, queued = sdk_actions(export.selection, [(DISCOVER, {}), (SUBMIT, request)])
    assert not catalog.isError and not queued.isError
    assert {source["feed_binding_ref"] for source in catalog.structuredContent["sources"]} == {
        decision_binding
    }
    run_ref = queued.structuredContent["run_ref"]
    materialize(run_ref)
    snapshot = export.snapshot()
    assert (snapshot.decisions, snapshot.verified, snapshot.pending, snapshot.families) == (
        1,
        0,
        1,
        1,
    )
    reopened, first_repeat, second_repeat = sdk_actions(
        export.selection,
        [
            (GET_RUN, {"run_ref": run_ref}),
            (SUBMIT, request | {"idempotency_key": "application-live-1-retry"}),
            (SUBMIT, request | {"idempotency_key": "application-live-1-retry-2"}),
        ],
    )
    assert not reopened.isError and reopened.structuredContent["workflow_ref"] == case.workflow
    for repeated in (first_repeat, second_repeat):
        assert not repeated.isError
        assert repeated.structuredContent["run_ref"] == run_ref
    assert first_repeat.structuredContent == second_repeat.structuredContent
    materialize(run_ref, "already_materialized")

    attempt(scope, "operation-D2", "attempt-3")
    commit(scope, "D2", "attempt-3")
    # The successor is a current snapshot of the same permitted engagement interval.
    # Its overlapping D1 is the same app event and must add no duplicate.
    second_page_receipt, page2 = export_page(scope, "second-page", 10, 12)
    revised = replace(DECISION, revision="grant-v2", source_revision="source-v2")
    export.case.body["grants"] = [asdict(revised)]
    export.case.write()
    install_app_page(revised, second_page_receipt, page2)
    new_binding = bind(revised)
    before_denied = export.receipts()
    stale, foreign = sdk_actions(
        export.selection,
        [(SUBMIT, request), (SUBMIT, request | {"feed_binding_ref": "feed-foreign"})],
    )
    assert not stale.isError
    assert stale.structuredContent["reason_code"] == "binding_superseded"
    assert "run_ref" not in stale.structuredContent
    assert foreign.isError and "run_ref" not in (foreign.structuredContent or {})
    assert export.receipts() == before_denied
    (next_receipt,) = sdk_actions(
        export.selection,
        [
            (
                SUBMIT,
                request
                | {
                    "feed_binding_ref": new_binding,
                    "source_revision": "source-v2",
                    "idempotency_key": "application-live-2",
                },
            )
        ],
    )
    assert not next_receipt.isError
    materialize(next_receipt.structuredContent["run_ref"])
    snapshot = export.snapshot()
    assert (snapshot.decisions, snapshot.verified, snapshot.pending, snapshot.families) == (
        2,
        0,
        2,
        1,
    )

    export.case.body["grants"].append(asdict(OUTCOME))
    export.case.write()
    export.install(OUTCOME, [[export.row(outcome(), OUTCOME.checkpoint + 1)]])
    outcome_binding = bind(OUTCOME, 1)
    (truth,) = sdk_actions(
        export.selection,
        [
            (
                SUBMIT,
                request
                | {
                    "feed_binding_ref": outcome_binding,
                    "source_revision": OUTCOME.source_revision,
                    "idempotency_key": "application-truth-1",
                },
            )
        ],
    )
    assert not truth.isError
    materialize(truth.structuredContent["run_ref"])
    snapshot = export.snapshot()
    assert (snapshot.decisions, snapshot.verified, snapshot.pending, snapshot.families) == (
        2,
        1,
        1,
        1,
    )
    assert ledger.read_bytes() == original_benchmark
    assert starter.get_run(owner, original.run_ref).benchmark == frozen
    assert not list(export.directory.rglob("release-decision.json"))
    assert not list(export.directory.glob("approval-ready-*.json"))
    assert effects(h) == (0,) * 7
    assert SENTINEL not in json.dumps(h.slack.post)
    with closing(sqlite3.connect(scope.parent / "invoice-decisions.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM attempts").fetchone() == (3,)
        assert db.execute("SELECT count(*) FROM commits").fetchone() == (2,)
    with closing(sqlite3.connect(export.directory / "learning-mandates.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone() == (0,)
    with closing(sqlite3.connect(export.directory / "training.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    return {
        "kind": "local_integration",
        "sdk_version": version("mcp"),
        "attempts": 3,
        "application_commits": 2,
        "first_run_ref": run_ref,
        "second_run_ref": next_receipt.structuredContent["run_ref"],
        "independent_outcome_run_ref": truth.structuredContent["run_ref"],
        "decisions": snapshot.decisions,
        "verified": snapshot.verified,
        "pending": snapshot.pending,
        "families": snapshot.families,
        "frozen_benchmark_ref": frozen.benchmark_ref,
        "effect_counts": effects(h),
    }
