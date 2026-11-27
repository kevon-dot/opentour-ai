"""Inspect an actual four-case scoring journal using a separately resolved viewer grant."""

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import replace

import pytest
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_score_inspection import InspectionScope, StarterScoreInspection
from test_starter_cases import OWNER, WORK
from test_starter_scoring import EVALUATOR, run, scored_setup  # noqa: F401


class Reader:
    def __init__(self, scope):
        self.scope = scope
        self.checks = 0
        self.withdraw_on = None

    @contextmanager
    def guard(self, actor, workflow, execution):
        yield self.scope

    def revalidate(self, actor, scope):
        self.checks += 1
        if self.withdraw_on == self.checks:
            raise IntakeError("material_withdrawn")


@pytest.fixture
def view(scored_setup, tmp_path):  # noqa: F811
    scorer, execution, *_ = scored_setup
    frozen = scorer.cases.evaluation_inputs(EVALUATOR, WORK)
    targets = scorer.cases.evaluation_targets(
        EVALUATOR, WORK, freeze_revision=frozen.freeze_revision
    )
    inputs = {
        "starter-case-" + digest([frozen.freeze_revision, c.case_id])[:40]: dict(c.decision_inputs)
        for c in frozen.cases
    }
    population = [
        dict(case_id=c.case_id, family_id=c.family_id, origin=c.origin, expected=targets[c.case_id])
        for c in frozen.cases
    ]
    reader = Reader(
        InspectionScope(
            OWNER,
            "inspect-v1",
            int(time.time()) + 120,
            execution.permission,
            digest(inputs),
            scorer.comparator.reference,
            digest(population),
            len(population),
            frozen.unresolved,
            frozen.excluded,
        )
    )
    return StarterScoreInspection(tmp_path, reader), reader, scored_setup


def read(item):
    service, authority, _ = item
    return service.summary(OWNER, WORK.workflow_ref, authority.scope.permission.execution_ref)


def test_reopen_is_aggregate_only_with_no_inference_or_writes(view, monkeypatch):
    _, _, setup = view
    result = run(setup)
    scorer, execution, *_ = setup
    execution.allowed = False

    def forbidden(*args):
        pytest.fail("inspection called evaluation machinery")

    for port in (scorer.baseline, scorer.comparator):
        monkeypatch.setattr(port, "lookup", forbidden)
        monkeypatch.setattr(port, "execute", forbidden)
    path = scorer.path
    before = path.read_bytes(), set(path.parent.iterdir()), path.stat().st_mtime_ns
    for _ in range(2):
        summary = read(view)
        assert summary["metrics"] == result["metrics"]
        assert summary["reserved"] == 4
        assert set(summary) == {
            "state",
            "evidence_origin",
            "freeze_ref",
            "freeze_revision",
            "reserved",
            "unresolved",
            "excluded",
            "metrics",
        }
    assert before == (path.read_bytes(), set(path.parent.iterdir()), path.stat().st_mtime_ns)


def test_absent_and_pending_are_distinct(view):
    service, _, setup = view
    assert read(view)["state"] == "frozen_unscored" and not service.path.exists()
    setup[0].baseline.unknown = True
    run(setup)
    summary = read(view)
    assert summary["state"] == "held" and "metrics" not in summary


@pytest.mark.parametrize(
    "damage", ["target", "prediction", "metrics", "receipt", "orphan", "population"]
)
def test_tampered_or_restored_rows_hold_even_if_result_rehashed(view, damage):
    service, reader, setup = view
    result = run(setup)
    if damage == "population":
        reader.scope = replace(reader.scope, population_digest="a" * 64)
    with sqlite3.connect(service.path) as db:
        if damage == "orphan":
            db.execute("DELETE FROM evaluations")
        elif damage == "receipt":
            db.execute("DELETE FROM predictions")
        else:
            if damage == "target":
                result["rows"][0]["expected"]["target"] = "GENERATED"
            if damage == "prediction":
                result["rows"][0]["model"] = {"status": "missing"}
            if damage == "metrics":
                result["metrics"]["paired"]["fixed"] = True
            result.pop("result_ref")
            result.pop("result_digest")
            result["result_digest"] = digest(result)
            result["result_ref"] = "starter-result-" + result["result_digest"][:40]
            db.execute("UPDATE evaluations SET result=?", (canonical(result),))
    with pytest.raises(IntakeError):
        read(view)


def test_recheck_current_viewer_and_expiry(view):
    service, reader, setup = view
    run(setup)
    with pytest.raises(IntakeError):
        service.summary(EVALUATOR, WORK.workflow_ref, reader.scope.permission.execution_ref)
    reader.withdraw_on = reader.checks + 2
    with pytest.raises(IntakeError, match="material_withdrawn"):
        read(view)
    reader.scope = replace(reader.scope, inspection_expires_at=int(time.time()))
    with pytest.raises(IntakeError):
        read(view)


def test_orphan_sidecar_is_never_initialized(view):
    service, *_ = view
    sidecar = service.path.with_suffix(".sqlite3-wal")
    sidecar.write_bytes(b"generated")
    with pytest.raises(IntakeError):
        read(view)
    assert not service.path.exists() and sidecar.read_bytes() == b"generated"


@pytest.mark.parametrize("mode", ["all_review", "missing"])
def test_undefined_and_missing_are_preserved(view, mode):
    _, _, setup = view
    setattr(setup[0].baseline, mode, True)
    result = run(setup)
    assert read(view)["metrics"] == result["metrics"]


def test_closed_wal_is_held_without_creating_sidecars(view):
    service, _, setup = view
    run(setup)
    db = sqlite3.connect(service.path)
    try:
        assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    finally:
        db.close()
    before = set(service.path.parent.iterdir())
    with pytest.raises(IntakeError):
        read(view)
    assert set(service.path.parent.iterdir()) == before
