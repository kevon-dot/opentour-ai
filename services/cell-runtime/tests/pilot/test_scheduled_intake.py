"""Generated normal-factory admission proof, never installed source-page qualification."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from opentour_cell.pilot import live_reader
from opentour_cell.pilot import scheduled_intake as scheduled
from opentour_cell.pilot.intake_local import SELECTION_VERSION, open_intake
from opentour_cell.pilot.intake_mcp import IntakeError, parse_intake
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.live_authority import SELECTION_SCHEMA
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake
from test_intake_mcp import _mandate, _write_private
from test_live_authority import SignedCase
from test_live_evidence import DECISION, OUTCOME, decision, outcome
from test_live_reader import Export


class Clock:
    def __init__(self):
        self.wall = time.time()
        self.elapsed = 0.0

    def advance(self, seconds=2):
        self.wall += seconds
        self.elapsed += seconds


class Case:
    def __init__(self, path: Path, **changes):
        self.signed = SignedCase(path)
        self.selected = path / "selected-live.json"
        selection = {
            "schema_version": SELECTION_SCHEMA,
            "tenant": "org-a",
            "workflow_ref": self.signed.workflow,
            "actor": "worker",
        }
        _write_private(self.selected, selection)
        self.body = {
            "schema_version": scheduled.SCHEMA,
            "installation_ref": "generated-installation",
            "tenant": "org-a",
            "actor": "worker",
            "selection_file": self.selected.name,
            "selection_sha256": scheduled._digest(selection),
            "enabled": True,
            "expires_at": int(time.time()) + 3000,
            "timezone": "UTC",
            "frequency_seconds": 1,
            "page_size": 1,
            "pages_per_tick": 1,
            "operations_per_tick": 20,
            "monthly_operations": 100,
            "tick_seconds": 10,
            "historical_intent": None,
            **changes,
        }
        self.configuration = path / "schedule.json"
        _write_private(self.configuration, self.body)

    def worker(self):
        return scheduled.ScheduledIntake(self.configuration)

    def live_runs(self):
        with sqlite3.connect(self.signed.directory / "intake.sqlite3") as db:
            return db.execute(
                'SELECT run_ref,request FROM intake_runs WHERE request LIKE \'%"mode":"live"%\''
            ).fetchall()

    def no_reader_effects(self):
        with self.signed.live._db() as db:
            assert db.execute("SELECT count(*) FROM live_events").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM live_pages").fetchone()[0] == 0
        with sqlite3.connect(self.signed.directory / "intake.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM intake_materialization").fetchone()[0] == 0
            assert db.execute("SELECT count(*) FROM intake_workflows").fetchone()[0] == 1


@pytest.fixture
def clock(monkeypatch):
    value = Clock()
    monkeypatch.setattr(
        scheduled, "time", SimpleNamespace(time=lambda: value.wall, monotonic=lambda: value.elapsed)
    )
    return value


def test_two_pages_restart_outage_overlap_backfill_and_operator_retry(tmp_path, clock):
    case = Case(tmp_path / "worker")
    decision = case.signed.connect()
    case.signed.connect("truth")
    first = case.worker().tick()
    assert first["state"] == "page_limit"
    assert len(case.live_runs()) == 1
    assert case.worker()._head()["cursor"] is not None
    clock.advance(120)
    second = case.worker().tick()
    assert second["state"] == "pending"
    assert len(case.live_runs()) == 2
    assert case.worker()._head()["cursor"] is None
    actor, intake = open_intake(case.selected)
    original = intake.submit_intake(
        actor,
        parse_intake(
            {
                "mode": "live",
                "feed_binding_ref": decision,
                "source_revision": "source-v1",
                "idempotency_key": "different-operator-key",
            }
        ),
    )
    assert original.run_ref in {row[0] for row in case.live_runs()}
    assert intake.get_run(actor, original.run_ref).reason_code == "live_reader_unavailable"
    for _ in range(2):
        clock.advance()
        case.worker().tick()
    assert len(case.live_runs()) == 2
    assert first["receipt_ref"] != second["receipt_ref"]
    case.no_reader_effects()


def test_crash_after_admission_before_cursor_commit_reuses_actual_run(tmp_path, clock, monkeypatch):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    worker = case.worker()
    original = worker._ack

    def crash(_pending, _result):
        raise RuntimeError("generated crash after durable admission")

    monkeypatch.setattr(worker, "_ack", crash)
    with pytest.raises(RuntimeError):
        worker.tick()
    assert len(case.live_runs()) == 1
    assert len(json.loads(worker._head()["pending"])) == 1
    assert worker._head()["cursor"] is None
    clock.advance()
    monkeypatch.setattr(worker, "_ack", original)
    assert worker.tick()["state"] == "pending"
    assert len(case.live_runs()) == 1
    assert json.loads(worker._head()["pending"]) == []
    with worker._db() as db:
        assert db.execute("SELECT operations FROM schedule_usage").fetchone()[0] == 3
    case.no_reader_effects()


def test_same_source_revision_new_authorized_binding_and_outcome_are_distinct(tmp_path, clock):
    case = Case(tmp_path / "worker", pages_per_tick=4)
    case.signed.connect()
    assert case.worker().tick()["state"] == "pending"
    case.signed.body["grants"][0]["source_revision"] = "source-v2"
    case.signed.body["grants"][0]["revision"] = "grant-v2"
    case.signed.write()
    case.signed.connect()
    clock.advance()
    assert case.worker().tick()["state"] == "pending"
    assert len(case.live_runs()) == 2
    case.signed.connect("truth")
    clock.advance()
    assert case.worker().tick()["state"] == "pending"
    assert len(case.live_runs()) == 3
    case.no_reader_effects()  # Outcome admission is NOT reducer reconciliation.


def test_empty_and_disabled_do_not_create_bindings_or_admit(tmp_path, clock):
    case = Case(tmp_path / "empty")
    result = case.worker().tick()
    assert result["state"] == "idle" and not case.signed.live.path.exists()
    with case.worker()._db() as db:
        assert db.execute("SELECT operations FROM schedule_usage").fetchone()[0] == 1
    disabled = Case(tmp_path / "disabled", enabled=False)
    disabled.signed.connect()
    assert disabled.worker().tick()["state"] == "paused"
    with pytest.raises(IntakeError):
        disabled.worker().control("enabled")
    assert disabled.live_runs() == []


def test_per_tick_quota_keeps_page_until_later_ack(tmp_path, clock):
    case = Case(tmp_path / "worker", operations_per_tick=1)
    case.signed.connect()
    assert case.worker().tick()["state"] == "quota_held"
    assert case.live_runs() == []
    assert len(json.loads(case.worker()._head()["pending"])) == 1
    clock.advance()
    assert case.worker().tick()["state"] == "pending"
    assert len(case.live_runs()) == 1


def test_monthly_allowance_reserves_failure_and_survives_restart(tmp_path, clock):
    case = Case(tmp_path / "worker", monthly_operations=1)
    case.signed.connect()
    assert case.worker().tick()["state"] == "quota_held"
    clock.advance(60)
    assert case.worker().tick()["state"] == "quota_held"
    assert case.live_runs() == []
    clock.advance(32 * 86400)
    assert case.worker().tick()["state"] == "expired"  # Expiry cannot be bypassed by a new month.


def test_timeout_preserves_unacknowledged_page(tmp_path, clock, monkeypatch):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    actor, intake = open_intake(case.selected)
    discover = intake.discover_sources

    def slow(*args, **kwargs):
        page = discover(*args, **kwargs)
        clock.advance(11)
        return page

    monkeypatch.setattr(intake, "discover_sources", slow)
    monkeypatch.setattr(scheduled, "open_intake", lambda _path: (actor, intake))
    assert case.worker().tick()["state"] == "timeout"
    assert case.live_runs() == []
    assert len(json.loads(case.worker()._head()["pending"])) == 1
    assert case.worker().tick()["state"] == "pending"


def test_pause_revoke_and_busy_are_serialized_before_new_reads(tmp_path, clock, monkeypatch):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    worker = case.worker()
    with worker._lease():
        with pytest.raises(IntakeError, match="schedule_busy"):
            case.worker().tick()
        with pytest.raises(IntakeError, match="schedule_busy"):
            case.worker().control("revoked")
    assert worker.control("paused")["state"] == "paused"
    monkeypatch.setattr(
        scheduled, "open_intake", lambda _path: pytest.fail("paused/revoked must not open factory")
    )
    assert case.worker().tick()["state"] == "paused"
    assert worker.control("revoked")["state"] == "revoked"
    assert case.worker().tick()["state"] == "revoked"
    with pytest.raises(IntakeError):
        worker.control("enabled")
    assert case.live_runs() == []


@pytest.mark.parametrize(
    "fault", ["revoked", "paused", "expired", "foreign", "removed_actor", "bad_seal"]
)
def test_current_signed_authority_denies_pending_resume_without_effects(tmp_path, clock, fault):
    case = Case(tmp_path / "worker", operations_per_tick=1)
    case.signed.connect()
    assert case.worker().tick()["state"] == "quota_held"
    if fault in {"revoked", "paused"}:
        case.signed.body["state"] = fault
    elif fault == "expired":
        case.signed.body["expires_at"] = 1
    elif fault == "foreign":
        case.signed.body["tenant"] = "org-b"
    elif fault == "removed_actor":
        del case.signed.body["actors"]["worker"]
    case.signed.write()
    if fault == "bad_seal":
        _write_private(
            case.signed.authority.path, {"grant": case.signed.body, "seal_sha256": "0" * 64}
        )
    clock.advance()
    before = (case.signed.directory / "intake.sqlite3").read_bytes()
    assert case.worker().tick()["state"] == "unavailable"
    assert before == (case.signed.directory / "intake.sqlite3").read_bytes()
    assert len(json.loads(case.worker()._head()["pending"])) == 1
    case.no_reader_effects()


def test_grant_revoked_between_discovery_and_submit_cannot_commit(tmp_path, clock, monkeypatch):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    actor, intake = open_intake(case.selected)
    discover = intake.discover_sources

    def revoke(*args, **kwargs):
        page = discover(*args, **kwargs)
        case.signed.body["state"] = "revoked"
        case.signed.write()
        return page

    monkeypatch.setattr(intake, "discover_sources", revoke)
    monkeypatch.setattr(scheduled, "open_intake", lambda _path: (actor, intake))
    assert case.worker().tick()["state"] == "unavailable"
    assert case.live_runs() == []
    case.no_reader_effects()


def test_actual_cli_and_allowlisted_history(tmp_path):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    command = [
        sys.executable,
        "-B",
        "-m",
        "opentour_cell.pilot.scheduled_intake",
        "--configuration",
        str(case.configuration),
    ]
    for action, expected in [
        ("tick", "pending"),
        ("pause", "paused"),
        ("tick", "paused"),
        ("revoke", "revoked"),
        ("tick", "revoked"),
    ]:
        result = subprocess.run(
            [*command, action],
            env=dict(os.environ),
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        output = json.loads(result.stdout)
        assert set(output) == {"state", "receipt_ref"}
        assert output["state"] == expected and result.stderr == ""
        assert "org-a" not in result.stdout and str(tmp_path) not in result.stdout
        assert "source-v1" not in result.stdout and "feed-" not in result.stdout
    assert len(case.live_runs()) == 1
    case.no_reader_effects()


@pytest.mark.parametrize(
    "fault", ["tenant", "actor", "selection", "configuration", "world_readable", "symlink"]
)
def test_configuration_cannot_borrow_other_actor_or_mutate_existing_installation(tmp_path, fault):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    worker = case.worker()
    if fault == "tenant":
        case.body["tenant"] = "org-b"
    elif fault == "actor":
        case.body["actor"] = "data-custodian"
    elif fault == "configuration":
        case.body["monthly_operations"] = 101
    elif fault == "selection":
        selection = json.loads(case.selected.read_text())
        selection["actor"] = "data-custodian"
        _write_private(case.selected, selection)
    elif fault == "world_readable":
        case.configuration.chmod(0o644)
    elif fault == "symlink":
        other = case.configuration.with_name("other.json")
        case.configuration.rename(other)
        case.configuration.symlink_to(other)
    if fault in {"tenant", "actor", "configuration"}:
        _write_private(case.configuration, case.body)
    with pytest.raises((IntakeError, OSError, ValueError)):
        worker.tick()
    assert case.live_runs() == []


def test_same_installation_identifiers_on_two_workers_never_cross_scope(tmp_path, clock):
    first = Case(tmp_path / "a")
    second = Case(tmp_path / "b")
    first.signed.connect()
    second.signed.connect()
    assert first.worker().tick()["state"] == second.worker().tick()["state"] == "pending"
    assert first.live_runs()[0][0] != second.live_runs()[0][0]
    assert first.signed.workflow != second.signed.workflow
    assert scheduled.deterministic_key(
        "org-a", "same", {"source": "v1"}
    ) != scheduled.deterministic_key("org-b", "same", {"source": "v1"})


def test_cadence_and_immutable_config_cannot_reset_used_allowance(tmp_path, clock):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    assert case.worker().tick()["state"] == "pending"
    assert case.worker().tick()["state"] == "cadence_held"
    case.body["monthly_operations"] = 999
    _write_private(case.configuration, case.body)
    clock.advance()
    with pytest.raises(IntakeError, match="schedule_configuration_changed"):
        case.worker().tick()
    with case.worker()._db() as db:
        assert db.execute("SELECT operations FROM schedule_usage").fetchone()[0] == 2


def test_month_rollover_preserves_pending_and_resets_only_period_allowance(tmp_path, clock):
    case = Case(tmp_path / "worker", monthly_operations=1, expires_at=int(time.time()) + 65 * 86400)
    case.signed.connect()
    assert case.worker().tick()["state"] == "quota_held"
    clock.advance(32 * 86400)
    assert case.worker().tick()["state"] == "pending"
    with case.worker()._db() as db:
        assert [row[0] for row in db.execute("SELECT operations FROM schedule_usage")] == [1, 1]
    assert len(case.live_runs()) == 1


def test_actual_historical_factory_requires_both_pinned_revisions(tmp_path, clock):
    # Independent generated mandate and pair grant, consumed by the real factory.
    root = tmp_path / "historical"
    root.mkdir(mode=0o700)
    grant = _mandate(root, "org-a", max_runs=1)
    epoch = LocalTrainingStore(root / "training.sqlite3", "org-a", existing_only=True).engagement_id
    ledger = LearningMandates(root, "org-a", epoch)
    scope = grant["scope"]
    source = {
        "source_revision": "d" * 64,
        "trace_sha256": "a" * 64,
        "correction_sha256": "b" * 64,
        "workflow": scope["workflow"],
        "connector": scope["connectors"][0],
        "trace_source": scope["trace_source"],
        "correction_source": scope["correction_source"],
        "field": scope["field"],
        "fields": scope["fields"],
        "rows": 2,
    }
    approved = ledger.authorize_source(grant["mandate_id"], source, actor="worker")
    selection = {
        "schema_version": SELECTION_VERSION,
        "tenant": "org-a",
        "actor": "worker",
        "engagement_epoch": epoch,
        "mandate_id": grant["mandate_id"],
        "mandate_revision": grant["mandate_revision"],
        "source_run_id": approved["run_id"],
        "workflow_ref": scope["workflow"],
        "trace_source": scope["trace_source"],
        "correction_source": scope["correction_source"],
        "pair_ref": "owner-pair",
        "source_revision": "a" * 64,
        "correction_revision": "b" * 64,
        "policy_revision": scope["agreement_revision"],
    }
    _write_private(root / "selection.json", selection)
    examples = Path(__file__).resolve().parents[2] / "examples"
    source_grant = json.loads((examples / "pilot-source-grant.template.json").read_text())
    source_grant.update(
        organization="org-a",
        engagement=epoch,
        owner_id=scope["owner_id"],
        expires_at=scope["expires_at"],
        permitted_features=scope["fields"],
        **{
            key: source[key]
            for key in (
                "workflow",
                "field",
                "trace_source",
                "correction_source",
                "trace_sha256",
                "correction_sha256",
            )
        },
        delegation={
            "authority_kind": approved["authority_kind"],
            "run_id": approved["run_id"],
            "delegate": "worker",
        },
    )
    _write_private(root / ("cycle-source-" + approved["run_id"] + ".json"), source_grant)
    template = Case(tmp_path / "template")
    intent = {
        key: selection[key]
        for key in ("pair_ref", "source_revision", "correction_revision", "policy_revision")
    }
    config = {
        **template.body,
        "selection_file": "selection.json",
        "selection_sha256": scheduled._digest(selection),
        "historical_intent": intent,
    }
    configuration = root / "schedule.json"
    _write_private(configuration, config)
    worker = scheduled.ScheduledIntake(configuration)
    assert worker.tick()["state"] == "admitted"
    clock.advance()
    assert scheduled.ScheduledIntake(configuration).tick()["state"] == "admitted"
    actor, intake = open_intake(root / "selection.json")
    assert isinstance(intake, BenchmarkIntake)
    with sqlite3.connect(intake.intake.path) as db:
        rows = db.execute("SELECT run_ref,request FROM intake_runs").fetchall()
        assert len(rows) == 1 and json.loads(rows[0][1])["correction_revision"] == "b" * 64
    assert intake.get_run(actor, rows[0][0]).state == "queued"
    wrong = {**intent, "correction_revision": "c" * 64}
    assert scheduled.deterministic_key("org-a", "same", intent) != scheduled.deterministic_key(
        "org-a", "same", wrong
    )
    with pytest.raises(IntakeError):
        intake.submit_intake(actor, parse_intake({**wrong, "idempotency_key": "wrong-revision"}))
    assert ledger.inspect(grant["mandate_id"], actor="worker")["reserved_fits"] == 0


def rotate(case, reference):
    binding = case.signed.live.resolve(reference, case.signed.actor)
    assert binding is not None
    grant = next(g for g in case.signed.body["grants"] if g["resource"] == binding.resource)
    grant["revision"] = "rotated-grant"
    grant["source_revision"] = "rotated-source"
    case.signed.write()
    return case.signed.connect(binding.resource)


def test_superseded_pending_is_retained_without_starving_other_feed(tmp_path, clock):
    case = Case(tmp_path / "worker", page_size=2, pages_per_tick=4, operations_per_tick=1)
    case.signed.connect()
    case.signed.connect("truth")
    assert case.worker().tick()["state"] == "quota_held"
    original = json.loads(case.worker()._head()["pending"])[0]
    successor = rotate(case, original["feed_binding_ref"])
    for _ in range(7):
        clock.advance()
        case.worker().tick()
    requests = [json.loads(row[1]) for row in case.live_runs()]
    assert len(requests) == 2
    assert successor in {row["feed_binding_ref"] for row in requests}
    assert original["feed_binding_ref"] not in {row["feed_binding_ref"] for row in requests}
    with case.worker()._db() as db:
        held = db.execute(
            "SELECT request,reason,successor_ref FROM schedule_dispositions"
        ).fetchall()
        assert len(held) == 1 and json.loads(held[0][0]) == original
        assert (held[0][1], held[0][2]) == ("binding_superseded", successor)
    case.no_reader_effects()


def test_retired_discovery_cursor_recovers_bounded_current_scan(tmp_path, clock):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    case.signed.connect("truth")
    assert case.worker().tick()["state"] == "page_limit"
    old_cursor = case.worker()._head()["cursor"]
    successor = rotate(case, old_cursor)
    clock.advance()
    assert case.worker().tick()["state"] == "page_limit"
    head = case.worker()._head()
    assert head["cursor"] == old_cursor and head["rescan"] == 1
    for _ in range(2):
        clock.advance()
        case.worker().tick()
    assert len(case.live_runs()) == 3
    assert successor in {json.loads(row[1])["feed_binding_ref"] for row in case.live_runs()}
    with case.worker()._db() as db:
        assert db.execute("SELECT cursor,attempts FROM schedule_restarts").fetchone()[:] == (
            old_cursor,
            1,
        )
    assert case.worker()._head()["cursor"] is None


def test_denied_fresh_scan_keeps_recovery_state_and_cannot_promote_successor(tmp_path, clock):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    case.signed.connect("truth")
    case.worker().tick()
    cursor = case.worker()._head()["cursor"]
    rotate(case, cursor)
    clock.advance()
    case.worker().tick()
    assert case.worker()._head()["rescan"] == 1
    case.signed.body["state"] = "revoked"
    case.signed.write()
    before = (case.signed.directory / "intake.sqlite3").read_bytes()
    clock.advance()
    assert case.worker().tick()["state"] == "unavailable"
    assert case.worker()._head()["cursor"] == cursor
    assert case.worker()._head()["rescan"] == 1
    assert before == (case.signed.directory / "intake.sqlite3").read_bytes()


def test_budget_writer_wait_rechecks_deadline_and_reservation_month(tmp_path, clock, monkeypatch):
    case = Case(tmp_path / "worker", expires_at=2**63 - 1)
    worker = case.worker()
    with worker._lease():
        worker._initialize()
    original = worker._db

    @contextmanager
    def delayed():
        with original() as db:
            clock.advance(2)
            yield db

    monkeypatch.setattr(worker, "_db", delayed)
    assert worker._reserve(0, clock.elapsed + 1) == "timeout"
    with original() as db:
        assert db.execute("SELECT count(*) FROM schedule_usage").fetchone()[0] == 0
    clock.wall = datetime(2026, 11, 30, 23, 59, 59, tzinfo=UTC).timestamp()
    assert worker._reserve(0, clock.elapsed + 5) is None
    with original() as db:
        assert db.execute("SELECT month,operations FROM schedule_usage").fetchone()[:] == (
            "2026-12",
            1,
        )


def test_deadline_after_reservation_starts_no_discovery(tmp_path, clock, monkeypatch):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    worker = case.worker()
    reserve = worker._reserve

    def delay(used, deadline):
        value = reserve(used, deadline)
        clock.advance(11)
        return value

    monkeypatch.setattr(worker, "_reserve", delay)
    assert worker.tick()["state"] == "timeout"
    assert worker._head()["page_loaded"] == 0
    assert case.live_runs() == []
    with worker._db() as db:
        assert db.execute("SELECT operations FROM schedule_usage").fetchone()[0] == 1


def test_fresh_wrong_actor_does_not_seal_installation_before_validation(tmp_path):
    case = Case(tmp_path / "worker", actor="data-custodian")
    case.signed.connect()
    with pytest.raises(IntakeError):
        case.worker().tick()
    case.body["actor"] = "worker"
    _write_private(case.configuration, case.body)
    assert case.worker().tick()["state"] == "pending"


def retained(worker):
    with worker._db() as db:
        return [
            dict(row)
            for row in db.execute(
                "SELECT * FROM schedule_work WHERE identity=? ORDER BY run_ref", (worker.identity,)
            )
        ]


def export_schedule(tmp_path, **changes):
    export = Export(tmp_path / "export")
    template = Case(tmp_path / "template")
    configuration = export.directory / "schedule.json"
    _write_private(
        configuration,
        {
            **template.body,
            "selection_file": export.selection.name,
            "selection_sha256": scheduled._digest(json.loads(export.selection.read_text())),
            "pages_per_tick": 4,
            **changes,
        },
    )
    return export, configuration


def test_ack_handoff_survives_lost_tick_receipt_and_duplicate_admission(
    tmp_path, clock, monkeypatch
):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    worker = case.worker()
    monkeypatch.setattr(
        worker, "_finish", lambda *_args: (_ for _ in ()).throw(RuntimeError("crash"))
    )
    with pytest.raises(RuntimeError, match="crash"):
        worker.tick()
    rows = retained(case.worker())
    assert len(rows) == 1 and rows[0]["run_ref"] == case.live_runs()[0][0]
    assert rows[0]["admitted_state"] == rows[0]["observed_state"] == "pending"
    assert json.loads(worker._head()["pending"]) == []
    assert rows[0]["request"] == scheduled._canonical(json.loads(rows[0]["request"]))
    clock.advance()
    case.worker().tick()
    assert retained(case.worker()) == rows and len(case.live_runs()) == 1
    case.no_reader_effects()


def test_handoff_insert_and_pending_removal_are_one_transaction(tmp_path, clock):
    case = Case(tmp_path / "worker", operations_per_tick=1)
    case.signed.connect()
    worker = case.worker()
    assert worker.tick()["state"] == "quota_held"
    with worker._db() as db:
        db.execute("""CREATE TRIGGER fail_ack BEFORE UPDATE OF pending ON schedule_heads
            WHEN NEW.pending='[]' BEGIN SELECT RAISE(ABORT, 'generated ACK crash'); END""")
    clock.advance()
    with pytest.raises(sqlite3.IntegrityError, match="generated ACK crash"):
        worker.tick()
    assert retained(worker) == [] and len(json.loads(worker._head()["pending"])) == 1
    assert len(case.live_runs()) == 1  # Actual admission committed in its own ledger.
    with worker._db() as db:
        db.execute("DROP TRIGGER fail_ack")
    clock.advance()
    assert case.worker().tick()["state"] == "pending"
    assert len(retained(worker)) == len(case.live_runs()) == 1


def test_reconcile_actual_reader_partial_page_restart_and_later_truth(tmp_path, clock):
    export, config = export_schedule(tmp_path)
    pages = export.install(
        DECISION,
        [[export.row(decision(), 11)], [export.row(decision("D2"), 12)]],
    )
    export.case.connect()
    worker = scheduled.ScheduledIntake(config)
    benchmark = export.directory / "workflow-benchmarks.sqlite3"
    original_benchmark = benchmark.read_bytes()
    assert worker.tick()["state"] == "admitted"
    run = retained(worker)[0]["run_ref"]
    assert retained(worker)[0]["observed_state"] == "queued"
    second = export.directory / pages[1]["pin"]["file"]
    content = second.read_bytes()
    second.unlink()
    with pytest.raises(IntakeError, match="live_export_unavailable"):
        live_reader.materialize_live(export.selection, run)
    assert (export.snapshot().decisions, export.snapshot().pending) == (1, 1)
    clock.advance()
    assert scheduled.ScheduledIntake(config).reconcile()["state"] == "pending"
    assert retained(worker)[0]["observed_state"] == "queued"
    # Actual committed-page continuation skips the missing first page on restart.
    (export.directory / pages[0]["pin"]["file"]).unlink()
    second.write_bytes(content)
    second.chmod(0o600)
    assert live_reader.materialize_live(export.selection, run)
    clock.advance()
    assert scheduled.ScheduledIntake(config).reconcile()["state"] == "reconciled"
    assert retained(worker)[0]["observed_state"] == "materialized"
    assert (export.snapshot().decisions, export.snapshot().pending) == (2, 2)
    export.install(OUTCOME, [[export.row(outcome(), 21)]])
    export.case.connect("truth")
    clock.advance()
    assert scheduled.ScheduledIntake(config).tick()["state"] == "admitted"
    truth = next(row for row in retained(worker) if row["run_ref"] != run)["run_ref"]
    assert live_reader.materialize_live(export.selection, truth)
    clock.advance()
    assert scheduled.ScheduledIntake(config).reconcile()["state"] == "reconciled"
    assert (export.snapshot().decisions, export.snapshot().verified, export.snapshot().pending) == (
        2,
        1,
        1,
    )
    assert len(export.receipts()) == 3 and benchmark.read_bytes() == original_benchmark
    assert all(row["observed_state"] == "materialized" for row in retained(worker))
    before = retained(worker)
    clock.advance()
    assert scheduled.ScheduledIntake(config).reconcile()["state"] == "idle"
    assert retained(worker) == before


@pytest.mark.parametrize("failure", [IntakeError, sqlite3.OperationalError, OSError])
def test_reconcile_unknown_does_not_erase_or_starve_other_current_run(
    tmp_path, clock, monkeypatch, failure
):
    case = Case(tmp_path / "worker", pages_per_tick=4)
    case.signed.connect()
    case.signed.connect("truth")
    worker = case.worker()
    worker.tick()
    before = retained(worker)
    actor, intake = open_intake(case.selected)
    get_run = intake.get_run

    def unavailable(actor, reference):
        if reference == before[0]["run_ref"]:
            raise failure("GENERATED_PRIVATE_SENTINEL")
        return get_run(actor, reference)

    monkeypatch.setattr(intake, "get_run", unavailable)
    monkeypatch.setattr(scheduled, "open_intake", lambda _path: (actor, intake))
    clock.advance()
    result = case.worker().reconcile()
    assert result["state"] == "unavailable" and set(result) == {"state", "receipt_ref"}
    after = retained(worker)
    assert [row["checks"] for row in after] == [1, 1]
    assert [row["availability"] for row in after] == ["unavailable", "available"]
    assert after[0]["observed_at"] == before[0]["observed_at"]
    assert after[0]["checked_at"] > before[0]["checked_at"]
    assert after[0]["observed_state"] == before[0]["observed_state"] == "pending"
    assert "GENERATED_PRIVATE_SENTINEL" not in worker.path.read_bytes().decode(errors="ignore")
    case.no_reader_effects()


def test_reconcile_monthly_quota_retains_run_without_status_call(tmp_path, clock, monkeypatch):
    case = Case(tmp_path / "worker", monthly_operations=2)
    case.signed.connect()
    assert case.worker().tick()["state"] == "pending"
    actor, intake = open_intake(case.selected)
    monkeypatch.setattr(intake, "get_run", lambda *_args: pytest.fail("quota exhausted"))
    monkeypatch.setattr(scheduled, "open_intake", lambda _path: (actor, intake))
    before = retained(case.worker())
    clock.advance()
    assert case.worker().reconcile()["state"] == "quota_held"
    assert retained(case.worker()) == before


def test_reconcile_quota_fairness_and_shared_cadence(tmp_path, clock):
    case = Case(tmp_path / "worker", pages_per_tick=4, operations_per_tick=1)
    case.signed.connect()
    case.signed.connect("truth")
    for _ in range(4):
        case.worker().tick()
        clock.advance()
    worker = case.worker()
    assert len(retained(worker)) == 2
    assert worker.reconcile()["state"] == "pending"
    assert sorted(row["checks"] for row in retained(worker)) == [0, 1]
    assert worker.tick()["state"] == worker.reconcile()["state"] == "cadence_held"
    clock.advance()
    assert worker.reconcile()["state"] == "pending"
    assert [row["checks"] for row in retained(worker)] == [1, 1]
    with worker._db() as db:
        assert db.execute("SELECT operations FROM schedule_usage").fetchone()[0] == 6


@pytest.mark.parametrize("control", ["paused", "revoked", "expired"])
def test_reconcile_local_control_opens_no_factory(tmp_path, clock, monkeypatch, control):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    case.worker().tick()
    before = retained(case.worker())
    if control == "expired":
        clock.advance(4000)
    else:
        case.worker().control(control)
        clock.advance()
    monkeypatch.setattr(scheduled, "open_intake", lambda _path: pytest.fail("controlled"))
    assert case.worker().reconcile()["state"] == control
    assert retained(case.worker()) == before


def test_reconcile_revoked_source_retains_handoff_and_commits_no_read(tmp_path, clock):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    case.worker().tick()
    before = retained(case.worker())
    case.signed.body["state"] = "revoked"
    case.signed.write()
    clock.advance()
    assert case.worker().reconcile()["state"] == "unavailable"
    after = retained(case.worker())
    assert after[0]["run_ref"] == before[0]["run_ref"]
    assert after[0]["observed_at"] == before[0]["observed_at"]
    assert after[0]["observed_state"] == "pending"
    case.no_reader_effects()


def test_reconcile_deadline_retains_unchecked_run(tmp_path, clock, monkeypatch):
    case = Case(tmp_path / "worker", pages_per_tick=4)
    case.signed.connect()
    case.signed.connect("truth")
    case.worker().tick()
    actor, intake = open_intake(case.selected)
    get_run = intake.get_run

    def slow(*args):
        result = get_run(*args)
        clock.advance(11)
        return result

    monkeypatch.setattr(intake, "get_run", slow)
    monkeypatch.setattr(scheduled, "open_intake", lambda _path: (actor, intake))
    clock.advance()
    assert case.worker().reconcile()["state"] == "timeout"
    assert sorted(row["checks"] for row in retained(case.worker())) == [0, 1]
    case.no_reader_effects()


def test_old_journal_adds_handoff_without_reinterpreting_historical_receipts(tmp_path, clock):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    case.worker().tick()
    with case.worker()._db() as db:
        db.execute("DROP TABLE schedule_work")  # Previous additive journal schema.
        usage = db.execute("SELECT operations FROM schedule_usage").fetchone()[0]
    clock.advance()
    assert case.worker().reconcile()["state"] == "idle"
    assert retained(case.worker()) == []  # Old tick references aren't fabricated source receipts.
    clock.advance()
    assert case.worker().tick()["state"] == "pending"
    assert len(retained(case.worker())) == len(case.live_runs()) == 1
    with case.worker()._db() as db:
        assert db.execute("SELECT operations FROM schedule_usage").fetchone()[0] == usage + 2


def test_actual_reconcile_cli_returns_only_state_and_receipt(tmp_path):
    case = Case(tmp_path / "worker")
    case.signed.connect()
    worker = case.worker()
    worker.tick()
    with worker._db() as db:
        db.execute("UPDATE schedule_heads SET last_start=0")
    result = subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "opentour_cell.pilot.scheduled_intake",
            "--configuration",
            str(case.configuration),
            "reconcile",
        ],
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert set(json.loads(result.stdout)) == {"state", "receipt_ref"}
    assert json.loads(result.stdout)["state"] == "pending" and result.stderr == ""
    assert all(
        value not in result.stdout for value in ("org-a", "feed-", "source-v1", str(tmp_path))
    )
    assert retained(worker)[0]["checks"] == 1
    case.no_reader_effects()
    assert len(case.live_runs()) == 1
