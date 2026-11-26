"""Actual private SQLite reads must not initialize closed WAL sidecars."""

import sqlite3

import pytest
from opentour_cell.pilot.intake_admission import DurableIntake
from test_workflow_benchmarks import ACTOR, STARTER

pytest_plugins = ("test_workflow_benchmarks",)


@pytest.mark.parametrize("register", ["intake", "benchmarks"])
def test_closed_wal_read_holds_without_creating_files(prepared, register):
    backend, authority = prepared
    admission = backend.submit_intake(ACTOR, STARTER)
    path = getattr(backend, register).path
    with sqlite3.connect(path) as writer:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    writer.close()
    before = sorted(p.name for p in path.parent.glob(path.name + "*"))
    assert before == [path.name]
    reader = DurableIntake(authority.directory, authority, existing_only=True)
    for _ in range(2):
        with pytest.raises(OSError):
            if register == "intake":
                with reader._db(write=False) as db:
                    db.execute("SELECT count(*) FROM intake_runs").fetchone()
            else:
                backend.get_run(ACTOR, admission.run_ref)
        assert sorted(p.name for p in path.parent.glob(path.name + "*")) == before


@pytest.mark.parametrize("register", ["intake", "benchmarks"])
def test_worker_opened_wal_snapshot_remains_readable(prepared, register):
    backend, authority = prepared
    admission = backend.submit_intake(ACTOR, STARTER)
    original = backend.get_run(ACTOR, admission.run_ref)
    path = getattr(backend, register).path
    writer = sqlite3.connect(path)
    try:
        assert writer.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
        writer.execute("BEGIN IMMEDIATE")
        assert path.with_name(path.name + "-wal").is_file()
        assert path.with_name(path.name + "-shm").is_file()
        reader = DurableIntake(authority.directory, authority, existing_only=True)
        with reader._db(write=False) as db:
            assert db.execute("SELECT count(*) FROM intake_runs").fetchone()[0] == 1
        assert backend.get_run(ACTOR, admission.run_ref) == original
    finally:
        writer.rollback()
        writer.close()
