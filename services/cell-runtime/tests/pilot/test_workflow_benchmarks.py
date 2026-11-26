"""Generated admission/versioning contracts; frozen producer qualification is separate."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from opentour_cell.pilot.intake_admission import AuthorizedSource, DurableIntake
from opentour_cell.pilot.intake_mcp import (
    CatalogPage,
    HistoricalPair,
    IntakeError,
    IntakePrincipal,
    LiveBinding,
    LiveRefresh,
    Starter,
    serve_intake,
)
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.workflow_benchmarks import (
    BenchmarkIntake,
    BenchmarkScope,
    MaterializedBenchmark,
)

ACTOR = IntakePrincipal("fixture-org", "fixture-operator", "intake")
STARTER = Starter("policy", "policy-v1", "setup-v1", "key-1", "creation-1", None)
HISTORY = HistoricalPair("pair", "trace-v1", "truth-v1", "policy-v1", "history-1")


class Authority:
    def __init__(self, directory: Path):
        self.directory = directory
        self.training = LocalTrainingStore(
            directory / "training.sqlite3", ACTOR.tenant, existing_only=True
        )
        self.epoch = self.training.engagement_id
        self.allowed = True

    @contextmanager
    def guard(self, actor):
        if actor != ACTOR or not self.allowed:
            raise IntakeError("intake_unavailable")
        with self.training.engagement_guard(read_only=True):
            yield

    def historical(self, actor, request):
        if request.pair_ref != "pair" or request.source_revision not in {"trace-v1", "trace-v2"}:
            return None
        return AuthorizedSource("historical-workflow", self.epoch, "evaluation", "pair", "grant-1")

    def starter(self, actor, request):
        if request.policy_resource_ref != "policy" or request.source_revision not in {
            "policy-v1",
            "policy-v2",
        }:
            return None
        return AuthorizedSource(
            request.workflow_ref,
            self.epoch,
            "starter",
            "policy",
            "grant-1",
            creation_ref=request.creation_ref,
        )

    def discover(self, actor, *, limit, cursor):
        return CatalogPage(())

    def resolve(self, reference, actor):
        if reference != "feed":
            return None
        return LiveBinding(
            ACTOR.tenant,
            "historical-workflow",
            self.epoch,
            "binding",
            "v1",
            "decision",
            "decisions",
            "live-grant",
            "current",
            "feed-v1",
        )

    def permits_live_revision(self, actor, binding, source_revision):
        return source_revision in {"feed-v1", "feed-v2"}

    def live_reader_available(self, actor, workflow):
        return True

    def benchmark(self, actor, workflow):
        return None


class Inspection:
    def __init__(self, authority: Authority):
        self.authority = authority

    @contextmanager
    def guard(self, actor, workflow):
        with self.authority.guard(actor):
            yield BenchmarkScope(actor.tenant, self.authority.epoch, workflow)


class Producer:
    def __init__(self, state="frozen_unscored"):
        self.state = state
        self.calls = 0
        self.definition_digest = "a" * 64
        self.before_return = lambda: None

    def materialize(self, actor, work):
        self.calls += 1
        self.before_return()
        return MaterializedBenchmark(
            self.state,
            "definition-1",
            self.definition_digest,
            work.revisions[0],
            "actual-result-1" if self.state == "scored" else None,
            "b" * 64 if self.state == "scored" else None,
        )


@pytest.fixture
def prepared(tmp_path: Path):
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    LocalTrainingStore(directory / "training.sqlite3", ACTOR.tenant)
    authority = Authority(directory)
    return BenchmarkIntake(
        DurableIntake(directory, authority), inspection=Inspection(authority)
    ), authority


def test_equivalent_new_keys_concurrent_and_restart_keep_one_draft(prepared):
    intake, authority = prepared
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(
            pool.map(
                lambda key: intake.submit_intake(ACTOR, replace(STARTER, idempotency_key=key)),
                ["key-a", "key-b"],
            )
        )
    assert receipts[0] == receipts[1]
    first = intake.get_run(ACTOR, receipts[0].run_ref)
    assert first.benchmark is not None and first.benchmark.benchmark_state == "draft"
    reopened = BenchmarkIntake(DurableIntake(authority.directory, Authority(authority.directory)))
    assert reopened.get_run(ACTOR, receipts[0].run_ref) == first
    with sqlite3.connect(intake.benchmarks.path) as db:
        assert db.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0] == 1
        assert db.execute("SELECT materialized FROM benchmarks").fetchone()[0] is None
    with sqlite3.connect(authority.training.path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


def test_crash_after_admission_before_draft_recovers_same_identity(prepared, monkeypatch):
    intake, authority = prepared

    def interrupted(*args):
        raise RuntimeError("generated crash before draft")

    monkeypatch.setattr(intake.benchmarks, "admit", interrupted)
    with pytest.raises(RuntimeError):
        intake.submit_intake(ACTOR, STARTER)
    with sqlite3.connect(intake.intake.path) as db:
        original_run, original_workflow = db.execute(
            "SELECT run_ref,workflow_ref FROM intake_runs"
        ).fetchone()
    assert not intake.benchmarks.path.exists()
    reopened = BenchmarkIntake(DurableIntake(authority.directory, authority))
    receipt = reopened.submit_intake(ACTOR, replace(STARTER, idempotency_key="retry-new-key"))
    status = reopened.get_run(ACTOR, receipt.run_ref)
    assert (receipt.run_ref, status.workflow_ref) == (original_run, original_workflow)
    assert status.benchmark.version == 1


def test_changed_intent_conflicts_and_fresh_policy_creates_one_successor(prepared):
    intake, _ = prepared
    original = intake.submit_intake(ACTOR, STARTER)
    first = intake.get_run(ACTOR, original.run_ref)
    with pytest.raises(IntakeError, match="idempotency_conflict"):
        intake.submit_intake(ACTOR, replace(STARTER, source_revision="policy-v2"))
    successor = intake.submit_intake(
        ACTOR, replace(STARTER, source_revision="policy-v2", idempotency_key="successor")
    )
    second = intake.get_run(ACTOR, successor.run_ref)
    assert second.workflow_ref == first.workflow_ref
    assert second.benchmark.version == 2
    assert intake.get_run(ACTOR, original.run_ref).benchmark.benchmark_state == "superseded"
    record = intake.inspect_benchmark(ACTOR, second.workflow_ref, second.benchmark.benchmark_ref)
    assert record["parent_ref"] == first.benchmark.benchmark_ref
    prior = intake.inspect_benchmark(ACTOR, first.workflow_ref, first.benchmark.benchmark_ref)
    assert prior["as_of_state"] == "draft"


def test_live_admission_leaves_benchmark_unchanged(prepared):
    intake, authority = prepared
    receipt = intake.submit_intake(ACTOR, HISTORY)
    assert intake.materialize(ACTOR, receipt.run_ref, Producer("scored"))
    status = intake.get_run(ACTOR, receipt.run_ref)
    with sqlite3.connect(intake.benchmarks.path) as db:
        original = db.execute("SELECT * FROM benchmarks").fetchall()
    for revision in ("feed-v1", "feed-v2"):
        live = intake.submit_intake(ACTOR, LiveRefresh("feed", revision, "key-" + revision))
        assert intake.get_run(ACTOR, live.run_ref).benchmark == status.benchmark
    reopened = BenchmarkIntake(
        DurableIntake(authority.directory, authority), inspection=Inspection(authority)
    )
    assert reopened.get_run(ACTOR, live.run_ref).benchmark == status.benchmark
    with sqlite3.connect(intake.benchmarks.path) as db:
        assert db.execute("SELECT * FROM benchmarks").fetchall() == original
    with pytest.raises(IntakeError, match="live_cannot_materialize_benchmark"):
        reopened.materialize(ACTOR, live.run_ref, Producer())


def test_live_only_never_creates_a_benchmark_or_implies_inspection(prepared):
    intake, authority = prepared
    receipt = intake.submit_intake(ACTOR, LiveRefresh("feed", "feed-v1", "only-live"))
    assert intake.get_run(ACTOR, receipt.run_ref).benchmark is None
    assert not intake.benchmarks.path.exists()
    history = intake.submit_intake(ACTOR, HISTORY)
    assert intake.get_run(ACTOR, history.run_ref).benchmark is not None
    limited = BenchmarkIntake(DurableIntake(authority.directory, authority))
    assert limited.get_run(ACTOR, receipt.run_ref).benchmark is None


def test_observed_workflow_requires_current_exact_scope_and_never_creates_identity(prepared):
    intake, authority = prepared
    scope = BenchmarkScope(ACTOR.tenant, authority.epoch, "historical-workflow")
    assert not intake.benchmarks.observed_workflow(scope)
    assert not intake.benchmarks.path.exists()
    intake.submit_intake(ACTOR, LiveRefresh("feed", "feed-v1", "only-live"))
    assert not intake.benchmarks.observed_workflow(scope)
    assert not intake.benchmarks.path.exists()
    intake.submit_intake(ACTOR, HISTORY)
    assert intake.benchmarks.observed_workflow(scope)
    for other in (
        replace(scope, tenant="foreign"),
        replace(scope, engagement_epoch="old-epoch"),
        replace(scope, workflow_ref="other-workflow"),
    ):
        assert not intake.benchmarks.observed_workflow(other)
    reopened = BenchmarkIntake(DurableIntake(authority.directory, Authority(authority.directory)))
    assert reopened.benchmarks.observed_workflow(scope)


@pytest.mark.parametrize("state", ["needs_owner_review", "frozen_unscored"])
def test_unscored_receipt_rejects_partial_result_metadata(prepared, state):
    intake, _ = prepared
    receipt = intake.submit_intake(ACTOR, STARTER)
    with intake.intake.admitted_work(ACTOR, receipt.run_ref) as work:
        for fields in ({"result_ref": "fake-result"}, {"result_digest": "b" * 64}):
            unscored = MaterializedBenchmark(state, "definition", "a" * 64, work.revisions[0])
            with pytest.raises(IntakeError):
                intake.benchmarks.publish(ACTOR.tenant, work, replace(unscored, **fields))
    assert intake.get_run(ACTOR, receipt.run_ref).benchmark.benchmark_state == "draft"


def test_unscored_has_no_fake_result_and_frozen_definition_cannot_change(prepared):
    intake, _ = prepared
    receipt = intake.submit_intake(ACTOR, STARTER)
    producer = Producer()
    assert intake.materialize(ACTOR, receipt.run_ref, producer)
    status = intake.get_run(ACTOR, receipt.run_ref)
    record = intake.inspect_benchmark(ACTOR, status.workflow_ref, status.benchmark.benchmark_ref)
    assert record["materialized"]["result_ref"] is None
    assert record["materialized"]["result_digest"] is None
    assert status.state == "materialized"
    assert status.benchmark.benchmark_state == "frozen_unscored"
    producer.state = "scored"
    producer.definition_digest = "c" * 64
    with pytest.raises(IntakeError, match="frozen_benchmark_changed"):
        intake.materialize(ACTOR, receipt.run_ref, producer)
    producer.definition_digest = "a" * 64
    assert intake.materialize(ACTOR, receipt.run_ref, producer)
    assert intake.get_run(ACTOR, receipt.run_ref).benchmark.benchmark_state == "scored"


def test_revocation_during_producer_and_foreign_roles_do_not_publish(prepared):
    intake, authority = prepared
    receipt = intake.submit_intake(ACTOR, STARTER)
    producer = Producer()
    producer.before_return = lambda: setattr(authority, "allowed", False)
    with pytest.raises(IntakeError):
        intake.materialize(ACTOR, receipt.run_ref, producer)
    with sqlite3.connect(intake.benchmarks.path) as db:
        assert db.execute("SELECT state FROM benchmarks").fetchone()[0] == "draft"
    for actor in (replace(ACTOR, tenant="foreign"), replace(ACTOR, role="evaluation")):
        with pytest.raises(IntakeError):
            intake.get_run(actor, receipt.run_ref)


def test_two_fresh_stdio_processes_reuse_draft(prepared):
    intake, authority = prepared
    request = {"mode": "starter", **{k: v for k, v in asdict(STARTER).items() if v is not None}}

    def invoke(arguments):
        messages = [
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "generated-benchmark-client", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "submit_intake", "arguments": arguments},
            },
        ]
        completed = subprocess.run(
            [sys.executable, __file__, str(authority.directory)],
            input="".join(json.dumps(m) + "\n" for m in messages),
            capture_output=True,
            text=True,
            check=True,
        )
        output = [json.loads(line) for line in completed.stdout.splitlines()]
        assert not completed.stderr
        result = output[-1]["result"]
        assert not result.get("isError", False)
        return json.loads(result["content"][0]["text"])

    first = invoke(request)
    second = invoke({**request, "idempotency_key": "another-transport-key"})
    assert first == second
    assert intake.get_run(ACTOR, first["run_ref"]).benchmark.version == 1


if __name__ == "__main__":
    path = Path(sys.argv[1])
    raise SystemExit(
        serve_intake(
            ACTOR,
            BenchmarkIntake(DurableIntake(path, Authority(path))),
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    )
