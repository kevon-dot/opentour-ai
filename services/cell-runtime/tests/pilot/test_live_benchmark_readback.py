"""Real live intake may disclose exactly one independently authorized benchmark."""

import json
import os
import sqlite3
import time
from pathlib import Path

import pytest
import test_intake_mcp as stdio
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import SELECTION_VERSION, LocalStarterAuthority, open_intake
from opentour_cell.pilot.intake_mcp import IntakeError, parse_intake
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.live_authority import SELECTION_SCHEMA
from opentour_cell.pilot.workflow_benchmarks import WorkflowBenchmarks
from test_intake_mcp import _write_private
from test_live_settings import form, open_live, sign
from test_starter_benchmark import records
from test_starter_setup import effects

pytest_plugins = ("test_live_settings",)


@pytest.fixture
def readback(connected_settings, monkeypatch):
    h, body = connected_settings
    directory = h.path.parent
    selected = json.loads((directory / "selected.json").read_bytes())
    body["actors"][selected["actor"]] = ["intake"]
    sign(h, body)
    view = open_live(h, monkeypatch)
    h.form(view, form(h, view))
    with sqlite3.connect(directory / "live-evidence.sqlite3") as db:
        (feed,) = db.execute("SELECT ref FROM live_bindings").fetchone()
    path = directory / "live-selection.json"
    _write_private(
        path,
        {
            "schema_version": SELECTION_SCHEMA,
            "tenant": h.store.organization,
            "actor": selected["actor"],
            "workflow_ref": body["workflow_ref"],
        },
    )
    actor, live = open_intake(path)
    receipt = live.submit_intake(
        actor,
        parse_intake(
            {
                "mode": "live",
                "feed_binding_ref": feed,
                "source_revision": "source-v1",
                "idempotency_key": "first-live",
            }
        ),
    )
    (row,) = records(h)
    selector = directory / "benchmark-inspection-selection.json"
    config = {
        "schema_version": "opentour.pilot.benchmark-inspection-selection.v1",
        "source_selection_file": "selected.json",
        "benchmark_run_ref": row[1],
    }
    return h, path, actor, live, receipt.run_ref, row, selector, config


def test_two_independent_grants_expose_existing_benchmark_and_keep_ledger_unchanged(readback):
    h, path, actor, live, run, row, selector, config = readback
    ledger = h.path.parent / "workflow-benchmarks.sqlite3"
    before = ledger.read_bytes()
    ordinary = live.get_run(actor, run)
    assert ordinary.benchmark is None and ordinary.workflow_ref == row[0]
    _write_private(selector, config)
    visible = live.get_run(actor, run)
    assert (
        visible.benchmark.benchmark_ref,
        visible.benchmark.version,
        visible.benchmark.benchmark_state,
    ) == (row[2], 1, "draft")
    restarted = stdio.TestIntakeMcp().run_status(run, selection=path)
    assert restarted["benchmark_ref"] == row[2] and restarted["workflow_ref"] == row[0]
    assert live.inspect_benchmark(actor, row[0], row[2]) is None
    (h.path.parent / "starter-intake-withdrawn-policy-scope.json").touch(mode=0o600)
    assert live.get_run(actor, run) == ordinary
    assert ledger.read_bytes() == before and records(h) == [row] and effects(h) == (0,) * 7


@pytest.mark.parametrize(
    "fault",
    [
        "unknown_run",
        "live_run",
        "foreign_actor",
        "withdrawn",
        "path_escape",
        "symlink",
        "mutable_selector",
        "altered_intent",
    ],
)
def test_ineligible_secondary_keeps_ordinary_live_status(readback, fault):
    h, _, actor, live, run, row, selector, config = readback
    ordinary = live.get_run(actor, run)
    ledger = h.path.parent / "workflow-benchmarks.sqlite3"
    before = ledger.read_bytes()
    if fault == "unknown_run":
        config["benchmark_run_ref"] = "run-" + "0" * 32
    elif fault == "live_run":
        config["benchmark_run_ref"] = run
    elif fault == "path_escape":
        config["source_selection_file"] = "../selected.json"
    elif fault == "foreign_actor":
        source = json.loads((h.path.parent / "selected.json").read_bytes())
        _write_private(h.path.parent / "selected.json", {**source, "actor": "another-reader"})
    elif fault == "withdrawn":
        (h.path.parent / "starter-intake-withdrawn-policy-scope.json").touch(mode=0o600)
    elif fault == "altered_intent":
        with sqlite3.connect(h.path.parent / "intake.sqlite3") as db:
            db.execute("UPDATE intake_runs SET intent=? WHERE run_ref=?", ("0" * 64, row[1]))
    if fault == "symlink":
        other = h.path.parent / "private-redirect.json"
        _write_private(other, config)
        selector.symlink_to(other)
    else:
        _write_private(selector, config)
    if fault == "mutable_selector":
        os.chmod(selector, 0o644)
    assert live.get_run(actor, run) == ordinary
    assert ledger.read_bytes() == before and effects(h) == (0,) * 7


def test_selection_withdrawn_at_projection_is_omitted(readback, monkeypatch):
    h, _, actor, live, run, _, selector, config = readback
    _write_private(selector, config)
    original = WorkflowBenchmarks.for_run

    def withdraw(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        (h.path.parent / "starter-intake-withdrawn-policy-scope.json").touch(mode=0o600)
        return result

    monkeypatch.setattr(WorkflowBenchmarks, "for_run", withdraw)
    assert live.get_run(actor, run).benchmark is None


def test_read_only_handle_cannot_recreate_or_migrate(readback):
    h, _, _, _, _, _, _, _ = readback
    root = h.path.parent
    authority = LocalStarterAuthority(root / "selected.json")
    existing = DurableIntake(root, authority, existing_only=True)
    with pytest.raises(IntakeError, match="private_intake_read_only"), existing._db():
        pass
    os.rename(existing.path, root / "intake-saved.sqlite3")
    with pytest.raises((OSError, IntakeError)), existing._db(write=False):
        pass
    with pytest.raises(IntakeError):
        DurableIntake(root, authority, existing_only=True)
    assert not existing.path.exists()


def test_missing_prior_lineage_preserves_live_status_without_migration(readback):
    h, _, actor, live, run, _, selector, config = readback
    ordinary = live.get_run(actor, run)
    _write_private(selector, config)
    with sqlite3.connect(h.path.parent / "intake.sqlite3") as db:
        db.execute("DROP TABLE intake_benchmark_admissions")
    assert live.get_run(actor, run) == ordinary
    with sqlite3.connect(h.path.parent / "intake.sqlite3") as db:
        assert not db.execute(
            "SELECT name FROM sqlite_master WHERE name='intake_benchmark_admissions'"
        ).fetchall()


def test_selection_changed_during_projection_does_not_disclose_old_benchmark(readback, monkeypatch):
    _, _, actor, live, run, _, selector, config = readback
    _write_private(selector, config)
    original = WorkflowBenchmarks.for_run

    def replace_selection(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        _write_private(selector, {**config, "benchmark_run_ref": "different-run"})
        return result

    monkeypatch.setattr(WorkflowBenchmarks, "for_run", replace_selection)
    assert live.get_run(actor, run).benchmark is None


@pytest.mark.parametrize("same_workflow", [True, False])
def test_historical_authority_and_workflow_are_independent(readback, same_workflow):
    h, _, actor, live, run, row, selector, config = readback
    root = h.path.parent
    examples = Path(stdio.__file__).resolve().parents[2] / "examples"
    scope = json.loads((examples / "pilot-learning-mandate.template.json").read_text())
    scope.update(
        mandate_id="historical-fixture",
        owner_id="workflow-owner",
        workflow=row[0] if same_workflow else "other-workflow",
        delegate=actor.actor,
        trainer=actor.actor,
        expires_at=int(time.time()) + 900,
        agreement_revision="e" * 64,
    )
    mandates = LearningMandates(root, actor.tenant, h.store.engagement_id)
    enrolled = mandates.enroll(scope, actor=scope["owner_id"])
    source = {
        **{
            key: scope[key]
            for key in ("workflow", "trace_source", "correction_source", "field", "fields")
        },
        "connector": scope["connectors"][0],
        "source_revision": "d" * 64,
        "trace_sha256": "a" * 64,
        "correction_sha256": "b" * 64,
        "rows": 2,
    }
    grant = mandates.authorize_source(enrolled["mandate_id"], source, actor=actor.actor)
    selected = {
        "schema_version": SELECTION_VERSION,
        "tenant": actor.tenant,
        "actor": actor.actor,
        "engagement_epoch": h.store.engagement_id,
        "mandate_id": enrolled["mandate_id"],
        "mandate_revision": enrolled["mandate_revision"],
        "source_run_id": grant["run_id"],
        "workflow_ref": scope["workflow"],
        "trace_source": scope["trace_source"],
        "correction_source": scope["correction_source"],
        "pair_ref": "selected-pair",
        "source_revision": source["trace_sha256"],
        "correction_revision": source["correction_sha256"],
        "policy_revision": scope["agreement_revision"],
    }
    selection_path = root / "prior-historical.json"
    _write_private(selection_path, selected)
    bound = json.loads((examples / "pilot-source-grant.template.json").read_text())
    bound.update(
        organization=actor.tenant,
        engagement=h.store.engagement_id,
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
            "authority_kind": grant["authority_kind"],
            "run_id": grant["run_id"],
            "delegate": actor.actor,
        },
    )
    _write_private(root / f"cycle-source-{grant['run_id']}.json", bound)
    principal, intake = open_intake(selection_path)
    request = {
        key: selected[key]
        for key in ("pair_ref", "source_revision", "correction_revision", "policy_revision")
    }
    prior = intake.submit_intake(
        principal, parse_intake({**request, "idempotency_key": "historical"})
    )
    history = intake.get_run(principal, prior.run_ref)
    assert history.benchmark is not None
    _write_private(
        selector,
        {
            **config,
            "source_selection_file": selection_path.name,
            "benchmark_run_ref": prior.run_ref,
        },
    )
    observed = live.get_run(actor, run)
    assert observed.benchmark == (history.benchmark if same_workflow else None)
    if same_workflow:
        assert observed.benchmark.version == 2
        _write_private(selector, config)
        assert live.get_run(actor, run).benchmark is None
