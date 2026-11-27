"""Generated signed decision exports on an admitted and independently scored starter workflow."""

import hmac
import json
import os
import sqlite3
import time
from contextlib import closing
from dataclasses import asdict, replace
from importlib.metadata import version
from types import SimpleNamespace

from opentour_cell.pilot import starter_benchmark_worker as worker
from opentour_cell.pilot.intake_mcp import GET_RUN, SUBMIT, IntakePrincipal
from opentour_cell.pilot.live_authority import SCHEMA, SELECTION_SCHEMA, PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence, _canonical
from opentour_cell.pilot.starter_evaluation_worker import main as evaluate
from test_intake_mcp import _write_private
from test_live_authority import KEY, enroll_ledger
from test_live_evidence import DECISION, decision
from test_live_identity_supersession import _calls, _read
from test_live_reader import Export
from test_live_settings import form, open_live
from test_reviewer_workflow import text
from test_slack_adapter import ACTOR

pytest_plugins = ("test_starter_completion",)


class _WorkflowExport(Export):
    """Attach only the existing export writer to the founder's actual workflow."""

    def __init__(self, directory, body, selection):
        self.directory = directory
        self.resources = {}
        self.case = SimpleNamespace(body=body, workflow=body["workflow_ref"])
        self.selection = selection


def test_scored_starter_and_two_decision_revisions(publication, installed, monkeypatch):
    sdk = os.environ.get("OPENTOUR_TEST_OFFICIAL_MCP") == "1"
    if sdk:
        assert version("mcp") == "1.30.0"
    directory, evaluator, adoption, cap, _, _ = publication
    h = installed[-1][0]
    workflow, run, tenant = cap["workflow_ref"], cap["run_ref"], cap["tenant"]
    starter = directory / "selected.json"

    def status(selection, ref):
        [result] = _calls(selection, [(GET_RUN, {"run_ref": ref})], sdk=sdk)
        assert result["workflow_ref"] == workflow and result["run_ref"] == ref
        return result

    def benchmark(state):
        return dict(
            benchmark_ref=cap["benchmark_ref"],
            version=cap["benchmark_version"],
            benchmark_state=state,
        )

    pending = status(starter, run)
    assert {k: pending[k] for k in benchmark("frozen_unscored")} == benchmark("frozen_unscored")
    assert evaluate(evaluator) == evaluate(evaluator + ["--publish-completion"]) == 0
    assert worker.main(adoption) == 0
    scored = status(starter, run)
    assert scored == pending | benchmark("scored")
    ledger = directory / "workflow-benchmarks.sqlite3"
    journal = directory / "starter-evaluations.sqlite3"
    completion = next(directory.glob("starter-completion-*.json"))
    immutable = {f: f.read_bytes() for f in (ledger, journal, completion)}
    receipt = json.loads(immutable[completion])["receipt"]
    assert receipt["benchmark_ref"] == scored["benchmark_ref"]
    assert receipt["result_ref"] == "starter-result-" + receipt["result_digest"][:40]

    with closing(sqlite3.connect(journal.as_uri() + "?mode=ro", uri=True)) as db:
        result = json.loads(
            db.execute(
                "SELECT result FROM evaluations WHERE ref=?", (cap["execution_ref"],)
            ).fetchone()[0]
        )
    assert (result["result_ref"], result["result_digest"], result["freeze_ref"]) == (
        receipt["result_ref"],
        receipt["result_digest"],
        cap["freeze_ref"],
    )

    body = dict(
        schema_version=SCHEMA,
        tenant=tenant,
        workflow_ref=workflow,
        engagement_epoch=cap["epoch"],
        dataset_ref="dataset-a",
        dataset_revision="dataset-v1",
        custodian=ACTOR,
        expires_at=int(time.time()) + 600,
        state="current",
        actors={ACTOR: ["custodian", "inspect"], cap["reader"]: ["intake"]},
        engagement_ledger_required=True,
    )
    _write_private(directory / "live-authority-key.json", {"key": KEY.hex()})
    enroll_ledger(directory, tenant, cap["epoch"], (DECISION,))
    selection = directory / "decision-intake.json"
    _write_private(
        selection,
        dict(
            schema_version=SELECTION_SCHEMA,
            tenant=tenant,
            workflow_ref=workflow,
            actor=cap["reader"],
        ),
    )
    export = _WorkflowExport(directory, body, selection)
    principal = IntakePrincipal(tenant, ACTOR, "slack")
    live = LiveEvidence(directory, PrivateLiveAuthority(directory, tenant, workflow))
    selector = directory / "benchmark-inspection-selection.json"
    read_grant = dict(
        schema_version="opentour.pilot.benchmark-inspection-selection.v1",
        source_selection_file=starter.name,
        benchmark_run_ref=run,
    )

    runs = []
    for count, grant in enumerate(
        (DECISION, replace(DECISION, source_revision="source-v2", revision="grant-v2")), 1
    ):
        body["grants"] = [asdict(grant)]
        _write_private(
            directory / f"live-authority-{workflow}.json",
            {
                "grant": body,
                "seal_sha256": hmac.digest(KEY, _canonical(body).encode(), "sha256").hex(),
            },
        )
        view = open_live(h, monkeypatch)
        h.form(view, form(h, view))
        assert "Saved binding" in text(h, view)
        [(binding, _)] = live.current_bindings(principal, workflow)
        # v2 rereads D1 plus the newly committed D2, in the same engagement interval.
        export.install(
            grant, [[export.row(decision(f"D{i}"), 10 + i)] for i in range(1, count + 1)]
        )
        request = dict(
            mode="live",
            source_revision=grant.source_revision,
            feed_binding_ref=binding,
            idempotency_key=f"delivery-{count}",
        )
        [admitted] = _calls(selection, [(SUBMIT, request)], sdk=sdk)
        live_run = admitted["run_ref"]
        assert live_run != run and live_run not in runs
        runs.append(live_run)
        _read(export, live_run, state="materialized")
        ordinary = status(selection, live_run)
        assert ordinary["state"] == "materialized" and benchmark("scored").keys().isdisjoint(
            ordinary
        )
        _write_private(selector, read_grant)
        assert status(selection, live_run) == ordinary | benchmark("scored")
        [retry] = _calls(
            selection, [(SUBMIT, request | {"idempotency_key": f"retry-{count}"})], sdk=sdk
        )
        assert (
            retry["run_ref"] == live_run and retry["correlation_ref"] == admitted["correlation_ref"]
        )
        _read(export, live_run, state="already_materialized")
        assert status(starter, run) == scored
        selector.unlink()
        assert status(selection, live_run) == ordinary
        snap = live.snapshot(principal, workflow)
        assert (snap.decisions, snap.verified, snap.pending) == (count, 0, count)
        assert {f: f.read_bytes() for f in immutable} == immutable
