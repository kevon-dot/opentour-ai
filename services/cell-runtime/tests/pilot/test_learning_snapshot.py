"""Exercise the receipt against actual admitted and materialized worker sources."""

import json
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest
import test_historical_benchmarks as historical_fixture
from opentour_cell.pilot import historical_worker, learning_snapshot, live_reader
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import IntakePrincipal
from opentour_cell.pilot.live_authority import SELECTION_SCHEMA, PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence
from test_historical_worker import prepared
from test_intake_mcp import _write_private
from test_live_authority import KEY, SCHEMA, SignedCase, request
from test_live_evidence import DECISION, OUTCOME, decision, outcome
from test_live_reader import Export

PRIVATE_ACTION = "fixture-private-action"
FIELDS = {
    "schema_version",
    "state",
    "origin",
    "snapshot_digest",
    "policy_revision",
    "verified_decisions",
    "pending_decisions",
    "disputed_decisions",
    "quarantined_events",
}


class LiveExport(Export):
    def __init__(self, directory, tenant, actor, workflow, epoch):
        # Only generate signed fixture settings here. Admission, signed lookup and
        # materialization use the same worker methods as the ordinary entry point.
        self.case = case = SignedCase.__new__(SignedCase)
        self.directory = case.directory = directory
        self.resources = {}
        case.workflow = workflow
        case.body = {
            "schema_version": SCHEMA,
            "tenant": tenant,
            "workflow_ref": workflow,
            "engagement_epoch": epoch,
            "dataset_ref": "generated-receipt-dataset",
            "dataset_revision": "generated-dataset-v1",
            "custodian": "generated-custodian",
            "expires_at": int(time.time()) + 3600,
            "state": "current",
            "actors": {
                actor: ["intake", "inspect"],
                "generated-custodian": ["inspect", "custodian"],
            },
            "grants": [asdict(DECISION), asdict(OUTCOME)],
        }
        _write_private(directory / "live-authority-key.json", {"key": KEY.hex()})
        case.write()
        case.authority = PrivateLiveAuthority(directory, tenant, workflow)
        case.live = LiveEvidence(directory, case.authority)
        case.custodian = IntakePrincipal(tenant, "generated-custodian", "inspect")
        case.owner = IntakePrincipal(tenant, actor, "inspect")
        self.selection = directory / "live-receipt-selection.json"
        _write_private(
            self.selection,
            {
                "schema_version": SELECTION_SCHEMA,
                "tenant": tenant,
                "workflow_ref": workflow,
                "actor": actor,
            },
        )


def joined(tmp_path, monkeypatch, *, tenant="generated-co", scored=True):
    tmp_path.mkdir(parents=True, exist_ok=True)

    # The checked-in generator accepts a tenant in its Python entry. The older
    # historical test helper's CLI pins its default, so parameterize that fixture
    # invocation only to build two independently authorized stores.
    def prepare_generated(command, **kwargs):
        _, program, directory, option, route = command
        assert option == "--route"
        invoke = (
            "import runpy, sys; "
            "from pathlib import Path; "
            "runpy.run_path(sys.argv[1])['prepare'](Path(sys.argv[2]), sys.argv[3], sys.argv[4])"
        )
        return subprocess.run(
            [sys.executable, "-c", invoke, program, directory, route, tenant], **kwargs
        )

    with monkeypatch.context() as local:
        local.setattr(historical_fixture, "subprocess", SimpleNamespace(run=prepare_generated))
        intake, actor, admitted, source, ledger, mandate, config, _ = prepared(
            tmp_path, no_development=True
        )
    if scored:
        assert historical_worker.execute(config, admitted.run_ref)["status"] == "completed"
    status = intake.get_run(actor, admitted.run_ref)
    export = LiveExport(
        source.directory, actor.tenant, actor.actor, status.workflow_ref, ledger.engagement
    )
    return export, admitted, source, config, ledger, mandate


def start(export):
    export.install(
        DECISION,
        [[export.row(decision(action_ref=PRIVATE_ACTION), 11), export.row(decision("D2"), 11)]],
    )
    run = export.submit(DECISION)
    assert live_reader.materialize_live(export.selection, run)
    return run


def run_receipt(export, run, source, benchmark, *, expected=None):
    cmd = [
        sys.executable,
        "-B",
        "-m",
        "opentour_cell.pilot.learning_snapshot",
        "--live-selection",
        str(export.selection),
        "--live-run",
        run,
        "--historical-selection",
        str(source.authority.path),
        "--benchmark-run",
        benchmark,
    ]
    if expected is not None:
        cmd += ["--expected-digest", expected]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    assert not result.stderr
    return result.returncode, json.loads(result.stdout)


def state(directory):
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*")
        if path.is_file()
    }


def test_current_receipt_normal_entry_and_restart_are_read_only(tmp_path, monkeypatch):
    export, admitted, producer, _, ledger, mandate = joined(tmp_path, monkeypatch)
    run = start(export)
    export.install(OUTCOME, [[export.row(outcome(), 21)]])
    truth = export.submit(OUTCOME)
    assert live_reader.materialize_live(export.selection, truth)

    actor, intake = open_intake(export.selection)
    ref = export.case.connect(DECISION.resource)
    repeated = intake.submit_intake(actor, request(ref, idempotency_key=DECISION.resource))
    retried = intake.submit_intake(actor, request(ref, idempotency_key="retried-trigger"))
    assert repeated.run_ref == retried.run_ref == run
    before = state(export.directory)
    code, receipt = run_receipt(export, run, producer, admitted.run_ref)
    assert code == 0 and set(receipt) == FIELDS
    assert receipt["schema_version"] == learning_snapshot.SCHEMA
    assert receipt["state"] == "observed" and receipt["origin"] == "observed_live"
    assert receipt["verified_decisions"] == receipt["pending_decisions"] == 1
    assert receipt["disputed_decisions"] == receipt["quarantined_events"] == 0
    assert receipt["policy_revision"] == producer.authority.selected.policy_revision
    wire = json.dumps(receipt)
    for private in (PRIVATE_ACTION, run, admitted.run_ref, "generated-co", "case-a", str(tmp_path)):
        assert private not in wire
    assert run_receipt(
        export, run, producer, admitted.run_ref, expected=receipt["snapshot_digest"]
    ) == (0, receipt)
    assert state(export.directory) == before

    with sqlite3.connect(producer.authority.store.path) as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 0
    assert (
        ledger.inspect(mandate["mandate_id"], actor=producer.authority.principal.actor)[
            "reserved_fits"
        ]
        == 0
    )
    assert not list(export.directory.glob("approval-ready-*.json"))


def test_reopened_truth_preserves_as_of_but_fences_previous_digest(tmp_path, monkeypatch):
    export, admitted, producer, _, _, _ = joined(tmp_path, monkeypatch)
    run = start(export)
    pages = export.install(
        OUTCOME,
        [
            [export.row(outcome(), 21)],
            [
                export.row(
                    outcome(
                        event_id="generated-reopened",
                        outcome_revision=2,
                        finality="reopened",
                        verified_action_ref=None,
                    ),
                    22,
                )
            ],
        ],
    )
    pending = export.directory / pages[1]["pin"]["file"]
    retained = pending.read_bytes()
    pending.unlink()
    truth = export.submit(OUTCOME)
    with pytest.raises(ValueError, match="live_export_unavailable"):
        live_reader.materialize_live(export.selection, truth)
    original = export.snapshot()
    code, first = run_receipt(export, run, producer, admitted.run_ref)
    assert code == 0 and first["verified_decisions"] == 1
    # An incomplete run with partial source effects cannot issue its own receipt.
    assert run_receipt(export, truth, producer, admitted.run_ref)[0] == 2

    pending.write_bytes(retained)
    pending.chmod(0o600)
    assert live_reader.materialize_live(export.selection, truth)
    code, reopened = run_receipt(export, run, producer, admitted.run_ref)
    assert code == 0 and reopened["verified_decisions"] == 0
    assert reopened["pending_decisions"] == 2
    assert reopened["snapshot_digest"] != first["snapshot_digest"]
    assert run_receipt(
        export, run, producer, admitted.run_ref, expected=first["snapshot_digest"]
    ) == (
        2,
        {
            "schema_version": learning_snapshot.SCHEMA,
            "state": "held",
            "reason": "live_snapshot_changed",
        },
    )
    assert (
        export.case.live.snapshot(
            export.case.owner, export.case.workflow, as_of_revision=original.sequence
        )
        == original
    )


def test_two_real_tenants_with_same_cases_keep_receipts_local(tmp_path, monkeypatch):
    a, first_bench, first_source, _, _, _ = joined(
        tmp_path / "a", monkeypatch, tenant="generated-a"
    )
    b, second_bench, second_source, _, _, _ = joined(
        tmp_path / "b", monkeypatch, tenant="generated-b"
    )
    first, second = start(a), start(b)
    first_code, first_receipt = run_receipt(a, first, first_source, first_bench.run_ref)
    second_code, second_receipt = run_receipt(b, second, second_source, second_bench.run_ref)
    assert first_code == second_code == 0
    assert first_receipt["pending_decisions"] == second_receipt["pending_decisions"] == 2
    assert first_receipt["snapshot_digest"] != second_receipt["snapshot_digest"]
    attempts = [
        (a, second, first_source, first_bench.run_ref),
        (b, first, second_source, second_bench.run_ref),
        (a, first, second_source, second_bench.run_ref),
        (b, second, first_source, first_bench.run_ref),
    ]
    expected = {
        "schema_version": learning_snapshot.SCHEMA,
        "state": "held",
        "reason": "live_snapshot_unavailable",
    }
    for args in attempts:
        assert run_receipt(*args) == (2, expected)


def test_without_real_benchmark_or_inspection_authority_no_receipt(tmp_path, monkeypatch):
    export, admitted, producer, config, _, _ = joined(tmp_path, monkeypatch, scored=False)
    run = start(export)
    assert run_receipt(export, run, producer, admitted.run_ref)[0] == 2
    assert historical_worker.execute(config, admitted.run_ref)["status"] == "completed"
    assert run_receipt(export, run, producer, admitted.run_ref)[0] == 0

    export.case.body["actors"][producer.authority.principal.actor] = ["intake"]
    export.case.write()
    export.sign()
    code, held = run_receipt(export, run, producer, admitted.run_ref)
    assert code == 2 and set(held) == {"schema_version", "state", "reason"}


def test_revocation_and_authority_change_between_reads_hold_without_mutating(tmp_path, monkeypatch):
    export, admitted, producer, _, ledger, mandate = joined(tmp_path, monkeypatch)
    run = start(export)
    assert run_receipt(export, run, producer, admitted.run_ref)[0] == 0
    capture = learning_snapshot._capture

    def revoke(*args):
        result = capture(*args)
        ledger.withdraw(mandate["mandate_id"], actor=mandate["owner_id"])
        return result

    with monkeypatch.context() as local:
        local.setattr(learning_snapshot, "_capture", revoke)
        with pytest.raises(ValueError):
            learning_snapshot.read_live_receipt(
                export.selection, run, producer.authority.path, admitted.run_ref
            )
    assert run_receipt(export, run, producer, admitted.run_ref)[0] == 2
    assert export.snapshot().pending == 2


def test_live_binding_revocation_never_serves_old_digest(tmp_path, monkeypatch):
    export, admitted, producer, _, _, _ = joined(tmp_path, monkeypatch)
    run = start(export)
    export.install(OUTCOME, [[export.row(outcome(), 21)]])
    truth = export.submit(OUTCOME)
    assert live_reader.materialize_live(export.selection, truth)
    code, first = run_receipt(export, run, producer, admitted.run_ref)
    assert code == 0 and first["verified_decisions"] == 1

    export.case.body["grants"] = [asdict(DECISION)]
    export.case.write()
    export.resources.pop(OUTCOME.resource)
    export.sign()
    before = state(export.directory)
    assert export.snapshot().verified == 0
    assert (
        run_receipt(export, run, producer, admitted.run_ref, expected=first["snapshot_digest"])[0]
        == 2
    )
    assert run_receipt(export, truth, producer, admitted.run_ref)[0] == 2
    assert state(export.directory) == before
