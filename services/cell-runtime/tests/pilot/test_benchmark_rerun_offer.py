"""Use a real score and separately provisioned local evaluator to read a rerun offer."""

import hmac
import json
import sqlite3
from dataclasses import asdict, replace

from opentour_cell.pilot.benchmark_rerun_offer import GRANT, BenchmarkRerunOffers
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_benchmark_worker import main as publish
from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
from opentour_cell.pilot.starter_evaluation_worker import main as evaluate
from test_intake_mcp import _write_private
from test_starter_evaluation_authority import seal
from test_starter_evaluation_worker import process, provision

pytest_plugins = ("test_starter_completion",)


def prepare(publication, installed):
    directory, original, adoption, cap, _, _ = publication
    assert (
        evaluate(original)
        == evaluate(original + ["--publish-completion"])
        == publish(adoption)
        == 0
    )
    _, _, reader, permission, _, _, authorized = installed
    actor = replace(reader.actor, role="slack")
    owner = StarterEvaluationSession(reader.settings, actor)
    entry = owner.entry(permission.execution_ref)
    second = replace(permission, execution_ref="generated-rerun", revision="rerun-execution-grant")
    grant = dict(authorized[6], grant_ref=second.revision, permission=asdict(second))
    seal(directory, "generated-rerun.json", grant)
    selection = directory / "starter-evaluation-selection.json"
    selected = json.loads(selection.read_bytes())
    selected["executions"][second.execution_ref] = "generated-rerun.json"
    _write_private(selection, selected)
    selector, invocation, key = provision(
        installed,
        invocation_ref="rerun-worker",
        permission=second,
        grant_file="generated-rerun.json",
    )
    request = {
        name: cap[name]
        for name in (
            "tenant",
            "epoch",
            "workflow_ref",
            "run_ref",
            "benchmark_ref",
            "benchmark_version",
            "freeze_ref",
            "freeze_revision",
        )
    } | dict(
        schema_version=GRANT,
        grant_ref="generated-rerun-offer",
        actor=actor.actor,
        expires_at=second.expires_at,
        original_execution_ref=permission.execution_ref,
        rerun_execution_ref=second.execution_ref,
        inspection_revision=entry["revision"],
        invocation_file=selector.name,
        invocation_digest=digest(
            json.loads((directory / (invocation["invocation_ref"] + ".json")).read_bytes())
        ),
    )
    slot_digest = digest(
        [actor.tenant, actor.actor, permission.execution_ref, second.execution_ref]
    )
    secret = hmac.digest(key, GRANT.encode(), "sha256")

    def write(value):
        _write_private(
            directory / f"benchmark-rerun-{slot_digest}.json",
            dict(
                grant=value,
                seal_sha256=hmac.digest(secret, canonical(value).encode(), "sha256").hex(),
            ),
        )

    service = BenchmarkRerunOffers(owner)

    def read():
        return service.read(
            permission.execution_ref, second.execution_ref, inspection_revision=entry["revision"]
        )

    return request, write, read, selector, service


def test_rerun_offer_preserves_original_and_second_execution_is_separately_authorized(
    publication, installed
):
    directory = publication[0]
    request, write, read, selector, _ = prepare(publication, installed)
    assert read() is None
    write(request)
    before = {f.name: f.read_bytes() for f in directory.iterdir() if f.is_file()}
    offer = read()
    assert offer.revision == digest(request) and offer.freeze_revision == request["freeze_revision"]
    assert offer.benchmark_ref == request["benchmark_ref"] and read() == offer
    assert {f.name: f.read_bytes() for f in directory.iterdir() if f.is_file()} == before
    code, result = process(selector)
    assert code == 0 and result["state"] == "scored"
    assert process(selector) == (code, result)
    assert (
        result["freeze_ref"] == offer.freeze_ref
        and result["execution_ref"] == offer.rerun_execution_ref
    )
    with sqlite3.connect(directory / "starter-evaluations.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM evaluations").fetchone() == (2,)
    assert (directory / "workflow-benchmarks.sqlite3").read_bytes() == before[
        "workflow-benchmarks.sqlite3"
    ]


def test_offer_denies_mismatched_or_withdrawn_independent_authority(publication, installed):
    directory = publication[0]
    request, write, read, _, service = prepare(publication, installed)
    for field in (
        "tenant",
        "actor",
        "epoch",
        "workflow_ref",
        "run_ref",
        "benchmark_ref",
        "freeze_revision",
        "inspection_revision",
        "original_execution_ref",
        "rerun_execution_ref",
        "invocation_digest",
    ):
        write(request | {field: "wrong-ref"})
        assert read() is None, field
    write(request)
    assert read() is not None
    assert (
        service.read(
            request["original_execution_ref"],
            request["original_execution_ref"],
            inspection_revision=request["inspection_revision"],
        )
        is None
    )
    registry = directory / "model-registration.json"
    saved = registry.read_bytes()
    _write_private(registry, json.loads(saved) | {"grants": []})
    assert read() is None
    registry.write_bytes(saved)
    assert read() is not None
    (directory / "benchmark-rerun-withdrawn-generated-rerun-offer.json").touch(mode=0o600)
    assert read() is None


def test_offer_withdrawal_during_final_model_check_is_held(publication, installed, monkeypatch):
    directory = publication[0]
    request, write, read, _, service = prepare(publication, installed)
    write(request)
    assert read() is not None
    original = service._baseline
    checks = []

    def withdraw(*args):
        current = original(*args)
        checks.append(current)
        if len(checks) == 2:
            (directory / "benchmark-rerun-withdrawn-generated-rerun-offer.json").touch(mode=0o600)
        return current

    monkeypatch.setattr(service, "_baseline", withdraw)
    assert read() is None and len(checks) == 2
