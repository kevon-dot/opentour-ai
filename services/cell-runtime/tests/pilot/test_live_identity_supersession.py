"""OPE-515 C identity fork through signed export, intake and reader entry points.

The existing reader test supersedes an already-disputed D1. This companion instead
supersedes a verified D1 directly while D2 shares its case/family and revision 1.
Generated custody fixtures are software evidence, not an installed/customer claim.
Positive: 2/1/1/1 -> 2/0/2/1, immutable as-of, retry/restart, zero jobs/reservations.
Adverse: a signed outcome naming the case ID cannot join either business decision.
"""

import json
import sqlite3
import subprocess
import sys
from contextlib import closing
from dataclasses import asdict

from opentour_cell.pilot.intake_mcp import DISCOVER, GET_RUN, SUBMIT
from opentour_cell.pilot.live_authority import PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from test_invoice_decision_emitter import sdk_actions
from test_live_evidence import DECISION, OUTCOME, decision, outcome
from test_live_reader import Export


def _calls(selection, calls, *, sdk):
    if sdk:
        results = sdk_actions(selection, calls)
        assert all(not result.isError for result in results)
        return [result.structuredContent for result in results]
    frames = [
        dict(
            id=1,
            method="initialize",
            params=dict(
                protocolVersion=PROTOCOL_VERSION,
                capabilities={},
                clientInfo=dict(name="generated-identity-fork", version="1"),
            ),
        ),
        dict(method="notifications/initialized"),
        dict(id=2, method="tools/list"),
    ]
    frames.extend(
        dict(id=i, method="tools/call", params=dict(name=n, arguments=a))
        for i, (n, a) in enumerate(calls, 3)
    )
    result = subprocess.run(
        [sys.executable, "-B", "-m", "opentour_cell.pilot.intake_local", str(selection)],
        input="".join(json.dumps(dict(jsonrpc="2.0", **frame)) + "\n" for frame in frames),
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0 and result.stderr == "", result.stderr
    messages = {r["id"]: r for r in map(json.loads, result.stdout.splitlines())}
    assert {t["name"] for t in messages[2]["result"]["tools"]} == {DISCOVER, SUBMIT, GET_RUN}
    values = [messages[i]["result"] for i in range(3, 3 + len(calls))]
    assert all(not item.get("isError") for item in values), values
    return [item["structuredContent"] for item in values]


def _read(export, run, *, state):
    result = subprocess.run(
        [sys.executable, "-B", "-m", "opentour_cell.pilot.live_reader", str(export.selection), run],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if state == "pending-page":
        assert (result.returncode, result.stdout, result.stderr) == (
            1,
            "",
            "live_export_unavailable\n",
        )
    else:
        assert (result.returncode, result.stdout.strip(), result.stderr) == (0, state, "")


def _admit(export, grant, key, *, sdk):
    binding = export.case.connect(grant.resource)
    request = dict(
        mode="live",
        feed_binding_ref=binding,
        source_revision=grant.source_revision,
        idempotency_key=key,
    )
    catalog, admitted = _calls(export.selection, [(DISCOVER, {}), (SUBMIT, request)], sdk=sdk)
    assert binding in {r["feed_binding_ref"] for r in catalog["sources"]}
    repeated, status = _calls(
        export.selection,
        [
            (SUBMIT, request | {"idempotency_key": key + "-transport-retry"}),
            (GET_RUN, {"run_ref": admitted["run_ref"]}),
        ],
        sdk=sdk,
    )
    assert repeated["run_ref"] == status["run_ref"] == admitted["run_ref"]
    assert status["workflow_ref"] == export.case.workflow
    return admitted["run_ref"]


def _snapshot(export):
    # Reopen actual persisted authority and live store for every observation.
    authority = PrivateLiveAuthority(export.directory, "org-a", export.case.workflow)
    return LiveEvidence(export.directory, authority).snapshot(
        export.case.owner, export.case.workflow
    )


def _counts(value):
    return value.decisions, value.verified, value.pending, value.families


def _recount(export):
    # Independently join persisted original targets by business ID AND revision.
    # No calls into the reducer or cached snapshot are used for this recount.
    with closing(sqlite3.connect(export.case.live.path)) as db:
        events = [json.loads(r[0]) for r in db.execute("SELECT payload FROM live_events")]
    decisions = {
        e["decision_id"]: e
        for e in sorted(
            (e for e in events if e["kind"] == "DecisionRecorded"),
            key=lambda e: e["decision_revision"],
        )
    }
    final_targets = {
        (e["decision_id"], e["decision_revision"])
        for e in events
        if e["kind"] == "OutcomeVerified" and e["finality"] == "final"
    }
    verified = sum(
        (identity, e["decision_revision"]) in final_targets for identity, e in decisions.items()
    )
    return (
        len(decisions),
        verified,
        len(decisions) - verified,
        len({e["family_id"] for e in decisions.values()}),
    )


def _zero_effects(export, benchmark):
    assert (export.directory / "workflow-benchmarks.sqlite3").read_bytes() == benchmark
    for filename, table in (
        ("training.sqlite3", "jobs"),
        ("learning-mandates.sqlite3", "reservations"),
    ):
        with closing(sqlite3.connect(export.directory / filename)) as db:
            assert db.execute("SELECT count(*) FROM " + table).fetchone()[0] == 0
    assert not list(export.directory.rglob("release-decision.json"))
    assert not list(export.directory.glob("approval-ready-*.json"))


def identity_supersession_probe(tmp_path, *, sdk=False):
    """The same companion can explicitly opt into the official installed SDK lane."""
    export = Export(tmp_path.resolve() / "worker")
    export.case.body["grants"] = [export.case.body["grants"][0]]
    export.case.write()
    benchmark = (export.directory / "workflow-benchmarks.sqlite3").read_bytes()
    d1 = decision(action_ref="supplies")
    d2 = decision("D2", action_ref="supplies")
    assert d1["case_id"] == d2["case_id"] and d1["family_id"] == d2["family_id"]
    assert d1["decision_revision"] == d2["decision_revision"] == 1
    pages = export.install(
        DECISION,
        [
            [export.row(d1, 11)] * 3 + [export.row(d2, 11)],
            [
                export.row(
                    decision(
                        event_id="D1-authoritative-rev2", decision_revision=2, action_ref="freight"
                    ),
                    12,
                )
            ],
        ],
    )
    delayed = export.directory / pages[1]["pin"]["file"]
    delayed_bytes = delayed.read_bytes()
    delayed.unlink()  # Declared source continuation is not available yet.
    run = _admit(export, DECISION, "fork-decisions", sdk=sdk)
    _read(export, run, state="pending-page")
    assert _counts(_snapshot(export)) == _recount(export) == (2, 0, 2, 1)
    assert len(export.receipts()) == 1 and not export.receipts()[0]["complete"]

    # Only now authorize and bind the separate independent outcome source.
    export.case.body["grants"].append(asdict(OUTCOME))
    export.case.write()
    export.install(OUTCOME, [[export.row(outcome(verified_action_ref="freight"), 21)]])
    truth_run = _admit(export, OUTCOME, "fork-outcome", sdk=sdk)
    _read(export, truth_run, state="materialized")
    prior = _snapshot(export)
    assert _counts(prior) == _recount(export) == (2, 1, 1, 1)
    assert [(r.decision_id, r.decision_revision, r.outcome_ref) for r in prior.verified_roots] == [
        ("D1", 1, "O1")
    ]
    assert prior.disputed == prior.unmatched_outcomes == 0
    with closing(sqlite3.connect(export.case.live.path)) as db:
        old_body = db.execute(
            "SELECT body FROM live_snapshots WHERE sequence=?", (prior.sequence,)
        ).fetchone()[0]
        assert db.execute("SELECT count(*) FROM live_events").fetchone()[0] == 3
    _zero_effects(export, benchmark)

    # Restart/replay both independent channels: no extra D1, D2 or O1.
    _read(export, truth_run, state="already_materialized")
    _read(export, run, state="pending-page")
    assert _snapshot(export) == prior

    delayed.write_bytes(delayed_bytes)
    delayed.chmod(0o600)
    _read(export, run, state="materialized")
    current = _snapshot(export)
    assert _counts(current) == _recount(export) == (2, 0, 2, 1)
    assert current.disputed == 0 and current.verified_roots == ()
    _read(export, run, state="already_materialized")
    _read(export, truth_run, state="already_materialized")
    assert _snapshot(export) == current
    reopened = LiveEvidence(
        export.directory,
        PrivateLiveAuthority(export.directory, "org-a", export.case.workflow),
    )
    assert (
        reopened.snapshot(export.case.owner, export.case.workflow, as_of_revision=prior.sequence)
        == prior
    )
    with closing(sqlite3.connect(export.case.live.path)) as db:
        assert (
            db.execute(
                "SELECT body FROM live_snapshots WHERE sequence=?", (prior.sequence,)
            ).fetchone()[0]
            == old_body
        )
        assert db.execute("SELECT count(*) FROM live_events").fetchone()[0] == 4
    _zero_effects(export, benchmark)
    return dict(
        kind="generated-software",
        transport="official-mcp-sdk" if sdk else "stdio-json-rpc",
        before=_counts(prior),
        after=_counts(current),
        historical=_counts(prior),
        decision_run=run,
        outcome_run=truth_run,
        zero_jobs=True,
        zero_reservations=True,
    )


def test_direct_verified_decision_supersession_via_real_reader(tmp_path):
    identity_supersession_probe(tmp_path)


def test_case_id_is_not_a_business_decision_target(tmp_path):
    export = Export(tmp_path.resolve() / "worker")
    benchmark = (export.directory / "workflow-benchmarks.sqlite3").read_bytes()
    export.install(DECISION, [[export.row(decision(), 11), export.row(decision("D2"), 11)]])
    export.install(OUTCOME, [[export.row(outcome(decision_id="invoice-1"), 21)]])
    decision_run = _admit(export, DECISION, "wrong-target-decisions", sdk=False)
    _read(export, decision_run, state="materialized")
    truth_run = _admit(export, OUTCOME, "wrong-case-target", sdk=False)
    _read(export, truth_run, state="materialized")
    snapshot = _snapshot(export)
    assert _counts(snapshot) == _recount(export) == (2, 0, 2, 1)
    assert snapshot.unmatched_outcomes == 1 and snapshot.verified_roots == ()
    _read(export, truth_run, state="already_materialized")
    assert _snapshot(export) == snapshot
    _zero_effects(export, benchmark)
