"""Read-only disclosure must not create SQLite WAL files after a clean shutdown."""

import sqlite3

import pytest
from test_live_readiness import ACTOR, Case, decision, outcome


@pytest.mark.parametrize("kind", ["live", "ready", "assessments"])
def test_actual_read_does_not_create_closed_wal_sidecars(tmp_path, kind):
    case = Case(tmp_path / "owner")
    case.bind_verifier()
    case.ingest(decision())
    case.ingest(outcome(), truth=True)
    case.refresh()
    snapshot = case.source.snapshot(ACTOR, "workflow")
    path = getattr(case, kind).path
    db = sqlite3.connect(path)
    assert db.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db.close()
    before = {f.name: f.read_bytes() for f in path.parent.glob(path.name + "*")}
    assert list(before) == [path.name]
    try:
        if kind == "live":
            case.live.snapshot(ACTOR, "workflow")
        elif kind == "ready":
            case.ready.read("workflow", actor=ACTOR)
        else:
            case.assessments.assessment(snapshot)
    except (OSError, ValueError, sqlite3.Error):
        pass
    assert {f.name: f.read_bytes() for f in path.parent.glob(path.name + "*")} == before
