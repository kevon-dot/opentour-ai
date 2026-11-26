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
        self.readbacks = 0
        self.records = {}
        self.definition_digest = "a" * 64
        self.before_return = lambda: None

    def materialize(self, actor, work, *, operation_ref):
        self.calls += 1
        receipt = MaterializedBenchmark(
            self.state,
            "definition-1",
            self.definition_digest,
            work.revisions[0],
            "actual-result-1" if self.state == "scored" else None,
            "b" * 64 if self.state == "scored" else None,
        )
        self.records[operation_ref] = receipt
        self.before_return()
        return receipt

    def readback(self, actor, work, *, operation_ref):
        self.readbacks += 1
        return self.records.get(operation_ref)


class DiskProducer(Producer):
    """Generated durable producer receipt for process-recovery conformance only."""

    def __init__(self, directory):
        super().__init__("scored")
        self.directory = directory

    def materialize(self, actor, work, *, operation_ref):
        counter = self.directory / "generated-producer-count"
        count = int(counter.read_text()) if counter.exists() else 0
        counter.write_text(str(count + 1))
        counter.chmod(0o600)
        receipt = super().materialize(actor, work, operation_ref=operation_ref)
        path = self.directory / (operation_ref + ".json")
        path.write_text(json.dumps(asdict(receipt)))
        path.chmod(0o600)
        return receipt

    def readback(self, actor, work, *, operation_ref):
        path = self.directory / (operation_ref + ".json")
        return MaterializedBenchmark(**json.loads(path.read_text())) if path.exists() else None


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


def test_recover_old_admission_preserves_newer_version_and_parent(prepared):
    intake, _ = prepared
    old = intake.intake.submit_intake(ACTOR, STARTER)
    new = intake.submit_intake(
        ACTOR, replace(STARTER, source_revision="policy-v2", idempotency_key="next")
    )
    newer = intake.get_run(ACTOR, new.run_ref)
    assert newer.benchmark.version == 2
    intake.submit_intake(ACTOR, replace(STARTER, idempotency_key="retry-old"))
    older = intake.get_run(ACTOR, old.run_ref)
    assert intake.get_run(ACTOR, new.run_ref) == newer
    assert older.benchmark.benchmark_state == "superseded"
    assert older.benchmark.version == 1
    record = intake.inspect_benchmark(ACTOR, newer.workflow_ref, newer.benchmark.benchmark_ref)
    assert record["parent_ref"] == older.benchmark.benchmark_ref


def test_legacy_order_requires_reauthorization_in_durable_order(prepared):
    intake, _ = prepared
    old = intake.intake.submit_intake(ACTOR, STARTER)
    # Simulate a deployed admission which predates the ordering ledger.
    with sqlite3.connect(intake.intake.path) as db:
        db.execute("DELETE FROM intake_benchmark_admissions")
    newer_request = replace(STARTER, source_revision="policy-v2", idempotency_key="new")
    newer = intake.intake.submit_intake(ACTOR, newer_request)
    with pytest.raises(IntakeError, match="benchmark_admission_order_unavailable"):
        intake.submit_intake(ACTOR, newer_request)
    assert not intake.benchmarks.path.exists()
    # Reauthorize the earlier actual record; never infer its old epoch from a later grant.
    intake.submit_intake(ACTOR, replace(STARTER, idempotency_key="recover-legacy"))
    intake.submit_intake(ACTOR, newer_request)
    assert intake.get_run(ACTOR, old.run_ref).benchmark.version == 1
    assert intake.get_run(ACTOR, newer.run_ref).benchmark.version == 2


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
    assert intake.materialize(ACTOR, receipt.run_ref, producer, target_state="frozen_unscored")
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
    producer.records = {
        key: replace(value, definition_digest="a" * 64) for key, value in producer.records.items()
    }
    assert intake.materialize(ACTOR, receipt.run_ref, producer)
    assert intake.get_run(ACTOR, receipt.run_ref).benchmark.benchmark_state == "scored"


def test_revocation_during_producer_and_foreign_roles_do_not_publish(prepared):
    intake, authority = prepared
    receipt = intake.submit_intake(ACTOR, STARTER)
    producer = Producer()
    producer.before_return = lambda: setattr(authority, "allowed", False)
    with pytest.raises(IntakeError):
        intake.materialize(ACTOR, receipt.run_ref, producer, target_state="frozen_unscored")
    with sqlite3.connect(intake.benchmarks.path) as db:
        assert db.execute("SELECT state FROM benchmarks").fetchone()[0] == "draft"
    for actor in (replace(ACTOR, tenant="foreign"), replace(ACTOR, role="evaluation")):
        with pytest.raises(IntakeError):
            intake.get_run(actor, receipt.run_ref)


def test_scored_sequential_concurrent_restart_and_status_retries_do_zero_producer_work(prepared):
    intake, authority = prepared
    admission = intake.submit_intake(ACTOR, STARTER)
    producer = Producer("scored")
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(
            pool.map(lambda _: intake.materialize(ACTOR, admission.run_ref, producer), range(2))
        )
    assert producer.calls == 1 and producer.readbacks == 0
    reopened = BenchmarkIntake(DurableIntake(authority.directory, authority))
    assert reopened.materialize(ACTOR, admission.run_ref, producer)
    for _ in range(3):
        assert reopened.get_run(ACTOR, admission.run_ref).benchmark.benchmark_state == "scored"
    assert producer.calls == 1 and producer.readbacks == 0


@pytest.mark.parametrize("create_draft", [False, True])
def test_superseded_version_never_invokes_producer(prepared, create_draft):
    intake, _ = prepared
    original = intake.submit_intake(ACTOR, STARTER)
    request = replace(STARTER, source_revision="policy-v2", idempotency_key="next")
    (intake if create_draft else intake.intake).submit_intake(ACTOR, request)
    producer = Producer("scored")
    with pytest.raises(IntakeError, match="immutable_benchmark_version"):
        intake.materialize(ACTOR, original.run_ref, producer)
    assert producer.calls == producer.readbacks == 0
    status = intake.get_run(ACTOR, original.run_ref)
    assert status.benchmark.benchmark_state == "superseded"
    inspected = intake.inspect_benchmark(ACTOR, status.workflow_ref, status.benchmark.benchmark_ref)
    assert inspected["benchmark_state"] == "superseded"


def test_denied_materialization_creates_no_worker_operation_or_lock(prepared):
    intake, _ = prepared
    admission = intake.submit_intake(ACTOR, STARTER)
    for actor in (replace(ACTOR, tenant="foreign"), replace(ACTOR, role="evaluation")):
        with pytest.raises(IntakeError):
            intake.materialize(actor, admission.run_ref, Producer("scored"))
    assert not intake.benchmarks.path.with_name("benchmark-worker.lock").exists()
    with sqlite3.connect(intake.benchmarks.path) as db:
        assert db.execute("SELECT COUNT(*) FROM benchmark_operations").fetchone()[0] == 0


def test_producer_receipt_after_crash_is_recovered_without_second_dispatch(prepared, monkeypatch):
    intake, authority = prepared
    admission = intake.submit_intake(ACTOR, STARTER)
    producer = Producer("scored")
    original_publish = intake.benchmarks.publish

    def crash(*args, **kwargs):
        raise RuntimeError("generated crash after producer receipt")

    monkeypatch.setattr(intake.benchmarks, "publish", crash)
    with pytest.raises(RuntimeError):
        intake.materialize(ACTOR, admission.run_ref, producer)
    assert producer.calls == 1
    monkeypatch.setattr(intake.benchmarks, "publish", original_publish)
    reopened = BenchmarkIntake(DurableIntake(authority.directory, authority))
    assert reopened.materialize(ACTOR, admission.run_ref, producer)
    assert producer.calls == 1 and producer.readbacks == 1
    assert reopened.get_run(ACTOR, admission.run_ref).benchmark.benchmark_state == "scored"


def test_uncertain_operation_never_blindly_dispatches_again(prepared):
    intake, _ = prepared
    admission = intake.submit_intake(ACTOR, STARTER)
    producer = Producer("scored")

    def no_receipt():
        producer.records.clear()
        raise RuntimeError("generated interruption without a receipt")

    producer.before_return = no_receipt
    with pytest.raises(RuntimeError):
        intake.materialize(ACTOR, admission.run_ref, producer)
    assert not intake.materialize(ACTOR, admission.run_ref, producer)
    assert producer.calls == 1 and producer.readbacks == 1
    assert intake.get_run(ACTOR, admission.run_ref).benchmark.benchmark_state == "draft"
    with pytest.raises(IntakeError, match="benchmark_materialization_unresolved"):
        intake.materialize(ACTOR, admission.run_ref, Producer(), target_state="frozen_unscored")


def test_fresh_worker_process_recovers_actual_generated_receipt_without_redispatch(prepared):
    intake, authority = prepared
    admission = intake.submit_intake(ACTOR, STARTER)
    command = [sys.executable, __file__, str(authority.directory), "worker", admission.run_ref]
    failed = subprocess.run([*command, "crash"], capture_output=True, text=True)
    assert failed.returncode == 42 and not failed.stderr
    assert intake.get_run(ACTOR, admission.run_ref).benchmark.benchmark_state == "draft"
    for _ in range(2):
        result = subprocess.run(command, capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "true" and not result.stderr
    assert (authority.directory / "generated-producer-count").read_text() == "1"
    assert intake.get_run(ACTOR, admission.run_ref).benchmark.benchmark_state == "scored"


def test_explicit_freeze_to_score_dispatches_once_per_transition(prepared):
    intake, _ = prepared
    admission = intake.submit_intake(ACTOR, STARTER)
    producer = Producer()
    assert intake.materialize(ACTOR, admission.run_ref, producer, target_state="frozen_unscored")
    assert intake.materialize(ACTOR, admission.run_ref, producer, target_state="frozen_unscored")
    assert producer.calls == 1
    producer.state = "scored"
    assert intake.materialize(ACTOR, admission.run_ref, producer, target_state="scored")
    assert intake.materialize(ACTOR, admission.run_ref, producer, target_state="scored")
    assert producer.calls == 2 and len(producer.records) == 2


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
    if len(sys.argv) > 2 and sys.argv[2] == "worker":
        backend = BenchmarkIntake(DurableIntake(path, Authority(path)))
        if len(sys.argv) > 4 and sys.argv[4] == "crash":

            def crash_after_receipt(*args, **kwargs):
                raise SystemExit(42)

            backend.benchmarks.publish = crash_after_receipt
        print(json.dumps(backend.materialize(ACTOR, sys.argv[3], DiskProducer(path))))
        raise SystemExit(0)
    raise SystemExit(
        serve_intake(
            ACTOR,
            BenchmarkIntake(DurableIntake(path, Authority(path))),
            sys.stdin.buffer,
            sys.stdout.buffer,
        )
    )
