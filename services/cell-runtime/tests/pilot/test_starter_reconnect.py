"""Generated independently signed reconnect and readout authority checks."""

import hmac
import json
import sqlite3
from dataclasses import replace

from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_evaluation_authority import StarterInspectionAuthority
from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
from opentour_cell.pilot.starter_reconnect import GRANT, StarterReconnect
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake
from test_intake_mcp import _write_private
from test_starter_evaluation_worker import process, provision

pytest_plugins = ("test_starter_evaluation_session",)


def configure(installed):
    directory, _, reader, p, *_ = installed
    actor = replace(reader.actor, role="slack")
    reader = StarterEvaluationSession(reader.settings, actor)
    authority = StarterInspectionAuthority(reader.settings, actor)
    inspection, _ = authority._grant(p.execution_ref)
    setup = authority._metadata(inspection, p).setup
    api = BenchmarkIntake(DurableIntake(directory, setup, existing_only=True))
    benchmark = api.get_run(setup.principal, p.run_ref).benchmark
    cap = dict(
        schema_version=GRANT,
        grant_ref="generated-reconnect",
        tenant=actor.tenant,
        engagement_epoch=p.epoch,
        workflow_ref=p.workflow_ref,
        run_ref=p.run_ref,
        execution_ref=p.execution_ref,
        freeze_ref=p.freeze_ref,
        freeze_revision=p.freeze_revision,
        model_ref=p.model_ref,
        model_revision=p.model_revision,
        model_custodian=actor.actor,
        inspection_revision=digest(inspection),
        benchmark_ref=benchmark.benchmark_ref,
        benchmark_version=benchmark.version,
        expires_at=p.expires_at,
    )
    name = "starter-reconnect-" + digest([actor.tenant, actor.actor, p.execution_ref]) + ".json"
    key = bytes.fromhex(json.loads((directory / "starter-intake-key.json").read_bytes())["key"])
    secret = hmac.digest(key, GRANT.encode(), "sha256")

    def write(value):
        _write_private(
            directory / name,
            {
                "grant": value,
                "seal_sha256": hmac.digest(secret, canonical(value).encode(), "sha256").hex(),
            },
        )

    def inspect(service=None):
        return (service or StarterReconnect(reader)).entry(
            p.execution_ref, expected_revision=cap["inspection_revision"]
        )

    return cap, write, inspect


def test_reconnect_requires_separate_scope_and_preserves_all_private_state(installed):
    directory, _, reader, _, *_ = installed
    cap, write, inspect = configure(installed)
    assert inspect() is None
    write(cap)
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    entry = inspect()
    assert entry["state"] == "setup_required"
    assert entry["revision"] == digest(cap) and entry["custodian_ref"] == reader.actor.actor
    for field in (
        "workflow_ref",
        "run_ref",
        "freeze_ref",
        "freeze_revision",
        "model_ref",
        "model_revision",
        "benchmark_ref",
        "benchmark_version",
    ):
        assert entry[field] == cap[field]
    assert inspect() == entry
    assert before == {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    }
    for field in (
        "tenant",
        "engagement_epoch",
        "workflow_ref",
        "run_ref",
        "execution_ref",
        "freeze_ref",
        "freeze_revision",
        "model_ref",
        "model_revision",
        "model_custodian",
        "inspection_revision",
        "benchmark_ref",
    ):
        write(cap | {field: "rebound"})
        assert inspect() is None, field
    write(cap | {"benchmark_version": cap["benchmark_version"] + 1})
    assert inspect() is None
    write(cap | {"expires_at": 1})
    assert inspect() is None
    write(cap)
    other = StarterEvaluationSession(reader.settings, replace(reader.actor, role="intake"))
    assert inspect(StarterReconnect(other)) is None
    marker = directory / "starter-reconnect-withdrawn-generated-reconnect.json"
    marker.touch(mode=0o600)
    assert inspect() is None


def test_recorded_score_or_restored_adapter_receipt_denies_reconnect(installed):
    directory, _, _, p, *_ = installed
    cap, write, inspect = configure(installed)
    write(cap)
    assert inspect()["state"] == "setup_required"
    selector, *_ = provision(installed)
    assert process(selector)[0] == 0
    assert inspect() is None
    # Restore only the scorer database away. Existing independent arm receipts
    # still make the execution unsafe to describe as awaiting a connection.
    journal = directory / "starter-evaluations.sqlite3"
    journal.rename(directory / "old-score.sqlite3")
    with sqlite3.connect(directory / "starter-local-predictions.sqlite3") as db:
        assert db.execute(
            "SELECT 1 FROM receipts WHERE json_extract(body,'$.request.execution_ref')=?",
            (p.execution_ref,),
        ).fetchone() == (1,)
    assert inspect() is None


def test_withdrawal_during_readback_cannot_disclose_reconnect(installed, monkeypatch):
    directory, *_ = installed
    cap, write, inspect = configure(installed)
    write(cap)
    idle = StarterReconnect._idle

    def withdrawn(self, execution):
        idle(self, execution)
        (directory / "starter-reconnect-withdrawn-generated-reconnect.json").touch(mode=0o600)

    monkeypatch.setattr(StarterReconnect, "_idle", withdrawn)
    assert inspect() is None
