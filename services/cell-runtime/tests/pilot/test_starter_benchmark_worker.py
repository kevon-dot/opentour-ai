"""Generated signed installation and independent materialization worker."""

import json
import subprocess
import sys

import pytest
from opentour_cell.pilot import guided_policy
from opentour_cell.pilot import starter_benchmark_worker as worker
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake, WorkflowBenchmarks
from test_starter_map_confirmation import FiveFamilies, adjudicate, sign_case

pytest_plugins = ("test_starter_map_confirmation",)


def call(path, run):
    return subprocess.run(
        [sys.executable, "-B", "-m", worker.__name__, str(path), run],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_installed_worker_replays_each_observed_state(joined):
    h, owner, cap, _, setup = joined
    run = cap["run_ref"]

    def current():
        api = BenchmarkIntake(DurableIntake(h.path.parent, setup, existing_only=True))
        return api.get_run(setup.principal, run).benchmark

    original = current()
    owner.propose(FiveFamilies())
    for expected in ("needs_owner_review", "frozen_unscored"):
        if expected == "frozen_unscored":
            adjudicate(owner)
            owner.freeze(splits={f"family-{i}": "reserved" for i in range(5)}, expected_revision=5)
        for _ in range(2):
            result = call(h.path, run)
            assert (result.returncode, result.stderr) == (0, "")
            assert json.loads(result.stdout) == {"run_ref": run, "benchmark_state": expected}
            actual = current()
            assert (actual.benchmark_ref, actual.version, actual.benchmark_state) == (
                original.benchmark_ref,
                original.version,
                expected,
            )


def test_wrong_run_and_revoked_worker_cannot_read_source_or_write(joined, monkeypatch):
    h, owner, cap, _, _ = joined
    owner.propose(FiveFamilies())
    directory = h.path.parent

    def files():
        return {p.name: p.read_bytes() for p in directory.iterdir() if p.is_file()}

    def forbidden(*args, **kwargs):
        pytest.fail("Read policy for denied worker or run")

    monkeypatch.setattr(guided_policy, "_read_policy", forbidden)
    before = files()
    assert worker.main([str(h.path), "other-run"]) == 2 and files() == before
    sign_case(directory, cap | {"worker_purposes": {}})
    before = files()
    assert worker.main([str(h.path), cap["run_ref"]]) == 2 and files() == before
    sign_case(directory, cap)
    (directory / f"starter-case-withdrawn-{cap['grant_ref']}.json").touch(mode=0o600)
    before = files()
    assert worker.main([str(h.path), cap["run_ref"]]) == 2 and files() == before


def test_late_withdrawal_rolls_back_worker_publication(joined, monkeypatch):
    h, owner, cap, _, setup = joined
    owner.propose(FiveFamilies())
    original = WorkflowBenchmarks._receipt_revision

    def withdraw(row, encoded):
        value = original(row, encoded)
        (h.path.parent / "starter-case-grant.json").unlink(missing_ok=True)
        return value

    monkeypatch.setattr(WorkflowBenchmarks, "_receipt_revision", staticmethod(withdraw))
    assert worker.main([str(h.path), cap["run_ref"]]) == 2
    api = BenchmarkIntake(DurableIntake(h.path.parent, setup, existing_only=True))
    assert api.get_run(setup.principal, cap["run_ref"]).benchmark.benchmark_state == "draft"
