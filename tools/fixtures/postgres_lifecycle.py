"""Registered adverse test of only this fixture's explicitly owned cluster."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from typing import Any

from tools.fixtures.postgres import DisposablePostgres, PostgresFixtureFailure


def _processes() -> dict[int, tuple[int, str]]:
    result = subprocess.run(
        ["/bin/ps", "-axo", "pid=,ppid=,stat="],
        check=True,
        capture_output=True,
        text=True,
        timeout=3,
        env={"PATH": "/usr/bin:/bin", "LC_ALL": "C"},
    )
    return {
        int(parts[0]): (int(parts[1]), parts[2])
        for row in result.stdout.splitlines()
        if len(parts := row.split()) == 3
    }


def abnormal_shutdown_check() -> dict[str, Any]:
    """Stop owned children first, proving postmaster exit alone is insufficient.

    Recovery signals only children whose parentage was established while the
    owned postmaster was alive and which remain stopped/alive throughout. No PID
    discovery from a database URL, service or preexisting data directory occurs.
    """
    database = DisposablePostgres().__enter__()
    assert database.process is not None and database.root is not None
    parent = database.process.pid
    children = [pid for pid, (ppid, _) in _processes().items() if ppid == parent]
    stopped: list[int] = []
    recovered = False
    try:
        assert children, "Actual PostgreSQL background children were not observed"
        for pid in children:
            os.kill(pid, signal.SIGSTOP)
            stopped.append(pid)
        database.process.kill()
        database.process.wait(timeout=3)
        try:
            database.close()
        except PostgresFixtureFailure:
            pass
        else:
            raise AssertionError("Abnormal shutdown was incorrectly accepted as clean")
        observed = _processes()
        assert any(pid in observed and "T" in observed[pid][1] for pid in stopped)
        assert database.root.is_dir()
        assert database.receipt["recovery_required"] is True
        assert database.receipt["cleanup_complete"] is False
    finally:
        # Stopped children cannot exit/reuse their PID between discovery and resume.
        # Once resumed we never signal their PID again, avoiding PID reuse hazards.
        observed = _processes()
        for pid in stopped:
            if pid in observed and "T" in observed[pid][1]:
                os.kill(pid, signal.SIGCONT)
        if database.process.poll() is None:
            database.process.terminate()
            database.process.wait(timeout=8)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            observed = _processes()
            if not any(pid in observed and "Z" not in observed[pid][1] for pid in children):
                recovered = True
                break
            time.sleep(0.05)
        if database.log is not None:
            database.log.close()
        if recovered:
            marker = database.root / ".owned-c03-fixture"
            assert not database.root.is_symlink() and marker.read_text() == database.root.name
            shutil.rmtree(database.root)
    assert recovered, "Owned abnormal cluster retained; background termination is uncertain"
    assert not database.root.exists()
    return {
        "fixture_id": "postgres-abnormal-shutdown",
        "fixture_version": "1",
        "kind": "local_integration",
        "product_acceptance": "NOT_RUN",
        "live_children_observed": len(children),
        "abnormal_cleanup_refused": True,
        "directory_retained_until_children_exited": True,
        "recovery_cleanup_complete": True,
    }
