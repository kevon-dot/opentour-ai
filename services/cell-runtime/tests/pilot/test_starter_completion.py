"""Generated separate evaluator/publication grants and receipt-only benchmark adoption."""

import hashlib
import hmac
import json
import sqlite3
import subprocess
from contextlib import closing
from dataclasses import asdict, replace

import pytest
from opentour_cell.pilot import starter_completion as completion
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.repair_bundle import _openssl
from opentour_cell.pilot.starter_benchmark_worker import main as materialize
from opentour_cell.pilot.starter_evaluation_worker import main as evaluate
from test_intake_mcp import _write_private
from test_starter_evaluation_authority import seal
from test_starter_evaluation_worker import provision

pytest_plugins = ("test_starter_evaluation_worker",)


@pytest.fixture
def publication(installed):
    d, _, _, p, _, _, authorized = installed
    h = authorized[0]
    args = [str(h.path), p.run_ref]
    assert materialize(args) == 0
    actor, api = open_intake(d / "selected.json")
    benchmark = api.get_run(actor, p.run_ref).benchmark
    assert benchmark.benchmark_state == "frozen_unscored"
    selector, inv, key = provision(installed)
    private = d / "starter-completion-signing.pem"
    public = d / "starter-completion-public.pem"
    subprocess.run(
        [_openssl(), "genrsa", "-out", str(private), "2048"], capture_output=True, check=True
    )
    private.chmod(0o600)
    public.write_bytes(
        subprocess.check_output(
            [_openssl(), "rsa", "-in", str(private), "-pubout"], stderr=subprocess.DEVNULL
        )
    )
    public.chmod(0o600)
    with api.intake.admitted_work(actor, p.run_ref) as work:
        work_digest = digest(asdict(work))
        source = work.revisions[0]
    cap = {k: v for k, v in inv.items() if k in completion._FIELDS}
    with closing(sqlite3.connect(d / "intake.sqlite3")) as db:
        correlation = db.execute(
            "SELECT correlation_ref FROM intake_runs WHERE run_ref=?", (p.run_ref,)
        ).fetchone()[0]
    cap.update(
        correlation_ref=correlation,
        invocation_digest=digest(json.loads((d / "invocation-a.json").read_bytes())),
        schema_version=completion.GRANT,
        grant_ref="publish-original",
        reader=actor.actor,
        benchmark_ref=benchmark.benchmark_ref,
        benchmark_version=benchmark.version,
        source_revision=source,
        admission_digest=work_digest,
        public_key_sha256=hashlib.sha256(public.read_bytes()).hexdigest(),
    )

    def grant(body):
        secret = hmac.digest(key, completion.GRANT.encode(), "sha256")
        _write_private(
            d / f"starter-publication-{digest(p.run_ref)}.json",
            dict(
                grant=body,
                seal_sha256=hmac.digest(secret, canonical(body).encode(), "sha256").hex(),
            ),
        )

    grant(cap)
    return (
        d,
        ["--invocation", str(selector)],
        ["--adopt-completion", *args],
        cap,
        grant,
        lambda: api.get_run(actor, p.run_ref).benchmark,
    )


def test_only_committed_score_is_attested_and_adopted(publication, monkeypatch):
    d, inv, worker, cap, grant, status = publication
    frozen = status()
    assert evaluate(inv + ["--publish-completion"]) == 2
    assert materialize(worker) == 2 and status() == frozen
    assert evaluate(inv) == 0
    assert status() == frozen and materialize(worker) == 2
    monkeypatch.setattr(
        completion.StarterEvaluationSession,
        "run",
        lambda *args: pytest.fail("attempted evaluation during reconciliation"),
    )
    for _ in range(2):
        assert evaluate(inv + ["--publish-completion"]) == 0
    receipt = next(d.glob("starter-completion-*.json"))
    packet = json.loads(receipt.read_bytes())
    assert set(packet["receipt"]) == {
        "schema_version",
        "publication_digest",
        "benchmark_ref",
        "result_ref",
        "result_digest",
    }
    assert materialize(worker) == 0
    scored = status()
    assert (
        scored.benchmark_state == "scored"
        and scored.benchmark_ref == frozen.benchmark_ref
        and scored.version == frozen.version
    )
    # Reader verifies the signature without opening the signing key or scorer journal.
    (d / "starter-completion-signing.pem").unlink()
    (d / "starter-evaluations.sqlite3").rename(d / "saved-scores.sqlite3")
    assert materialize(worker) == 0 and status() == scored
    (d / "starter-publication-withdrawn-publish-original.json").touch(mode=0o600)
    assert materialize(worker) == 2 and status() == scored


@pytest.mark.parametrize(
    "fault", ["reader", "freeze", "run", "version", "execution", "corrupt", "late"]
)
def test_completion_denial_keeps_same_frozen_benchmark(publication, fault, monkeypatch):
    d, inv, worker, cap, grant, status = publication
    frozen = status()
    assert evaluate(inv) == 0 and evaluate(inv + ["--publish-completion"]) == 0
    if fault == "corrupt":
        receipt = next(d.glob("starter-completion-*.json"))
        packet = json.loads(receipt.read_bytes())
        packet["receipt"].update(result_ref="starter-result-" + "0" * 40, result_digest="0" * 64)
        _write_private(receipt, packet)
    elif fault == "late":
        original = completion.WorkflowBenchmarks._receipt_revision

        def withdraw(row, encoded):
            value = original(row, encoded)
            (d / "starter-publication-withdrawn-publish-original.json").touch(mode=0o600)
            return value

        monkeypatch.setattr(
            completion.WorkflowBenchmarks, "_receipt_revision", staticmethod(withdraw)
        )
    else:
        field, value = {
            "reader": ("reader", "other"),
            "freeze": ("freeze_revision", "f" * 64),
            "run": ("run_ref", "another-run"),
            "version": ("benchmark_version", 2),
            "execution": ("execution_ref", "another-execution"),
        }[fault]
        grant(cap | {field: value})
    assert materialize(worker) == 2 and status() == frozen


def test_separately_authorized_rerun_cannot_replace_original(publication, installed):
    d, inv, worker, cap, _, status = publication
    assert evaluate(inv) == evaluate(inv + ["--publish-completion"]) == materialize(worker) == 0
    original = status()
    packet = next(d.glob("starter-completion-*.json"))
    saved = packet.read_bytes()
    p = replace(installed[3], execution_ref="execution-b", revision="evaluation-b")
    seal(d, "execution-b.json", installed[-1][6] | dict(grant_ref=p.revision, permission=asdict(p)))
    path = d / "starter-evaluation-selection.json"
    sel = json.loads(path.read_bytes())
    sel["executions"][p.execution_ref] = "execution-b.json"
    _write_private(path, sel)
    selector, _, _ = provision(
        installed, invocation_ref="invocation-b", permission=p, grant_file="execution-b.json"
    )
    assert evaluate(["--invocation", str(selector)]) == 0
    assert evaluate(["--invocation", str(selector), "--publish-completion"]) == 2
    assert materialize(worker) == 2 and status() == original and packet.read_bytes() == saved


def test_recovery_requires_both_independent_runtime_receipts(publication, monkeypatch):
    directory, invocation, worker, _, _, status = publication
    assert evaluate(invocation) == 0
    frozen = status()
    monkeypatch.setattr(
        completion.StarterEvaluationSession, "run", lambda *args: pytest.fail("rescore")
    )
    # Simulate an incomplete restore after the committed result and before signing.
    journal = directory / "starter-local-predictions.sqlite3"
    journal.rename(directory / "missing-predictions.sqlite3")
    assert evaluate(invocation + ["--publish-completion"]) == 2
    assert not list(directory.glob("starter-completion-*.json"))
    assert materialize(worker) == 2 and status() == frozen


def test_stale_correlation_or_signer_denies_before_export(publication):
    directory, invocation, worker, cap, grant, status = publication
    assert evaluate(invocation) == 0
    frozen = status()
    for change in ({"correlation_ref": "correlation-other"}, {"public_key_sha256": "0" * 64}):
        grant(cap | change)
        assert evaluate(invocation + ["--publish-completion"]) == 2
        assert not list(directory.glob("starter-completion-*.json"))
        assert materialize(worker) == 2 and status() == frozen


def test_two_materializer_processes_share_one_completed_operation(publication):
    import sys
    from concurrent.futures import ThreadPoolExecutor

    directory, invocation, worker, _, _, status = publication

    def process(module, args):
        completed = subprocess.run(
            [sys.executable, "-B", "-m", "opentour_cell.pilot." + module, *args],
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
        assert (completed.returncode, completed.stderr) == (0, "")
        return json.loads(completed.stdout)

    assert process("starter_evaluation_worker", invocation)["state"] == "scored"
    assert (
        process("starter_evaluation_worker", invocation + ["--publish-completion"])["reason_code"]
        == "completion_published"
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: process("starter_benchmark_worker", worker), range(2)))
    assert results[0] == results[1] == {"run_ref": worker[-1], "benchmark_state": "scored"}
    assert status().benchmark_state == "scored"
    with closing(sqlite3.connect(directory / "workflow-benchmarks.sqlite3")) as db:
        assert db.execute(
            "SELECT count(*),sum(completed) FROM benchmark_operations WHERE target='scored'"
        ).fetchone() == (1, 1)
