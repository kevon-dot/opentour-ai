"""CA-verified installed connector, normal packaged launcher and cross-transport admission."""

import argparse
import asyncio
import http.client
import importlib.util
import json
import secrets
import sqlite3
import ssl
import subprocess
import sys
import tempfile
import time
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

import pytest
import test_intake_mcp as vectors
import test_learning_runs as runs
from opentour_cell.pilot import learning_runs as lr
from opentour_cell.pilot.historical_worker import CONFIG as HISTORICAL_WORKER_CONFIG
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import parse_intake
from opentour_cell.pilot.operating_point import digest
from test_historical_benchmarks import setup as setup_historical
from test_historical_benchmarks import write
from test_live_evidence import DECISION, OUTCOME, decision, outcome
from test_live_worker_readiness import configured
from test_worker_host_install import PYTHON, kit  # noqa: F401
from test_workflow_benchmarks import DiskProducer

DISCOVER, SUBMIT, STATUS = vectors.DISCOVER, vectors.SUBMIT, vectors.GET_RUN


class Connector:
    def __init__(self, package, work):
        self.kit = package
        self.work = work
        self.token = secrets.token_urlsafe(32)
        credential = work / "credential"
        credential.touch(mode=0o600)
        credential.write_text(self.token)
        self.server = package / "integrations/worker-host/health.py"
        self.manifest = work / "manifest.json"

        def openssl(*args):
            subprocess.run(
                ["openssl", *args], cwd=work, check=True, capture_output=True, timeout=15
            )

        (work / "extensions").write_text(
            "subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n"
        )
        openssl(
            "req",
            "-x509",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-days",
            "1",
            "-subj",
            "/CN=Generated CA",
            "-keyout",
            "ca.key",
            "-out",
            "ca.crt",
        )
        openssl(
            "req",
            "-newkey",
            "rsa:2048",
            "-nodes",
            "-subj",
            "/CN=Generated site",
            "-keyout",
            "tls.key",
            "-out",
            "tls.csr",
        )
        openssl(
            "x509",
            "-req",
            "-in",
            "tls.csr",
            "-CA",
            "ca.crt",
            "-CAkey",
            "ca.key",
            "-CAcreateserial",
            "-days",
            "1",
            "-extfile",
            "extensions",
            "-out",
            "tls.crt",
        )
        self.trust = ssl.create_default_context(cafile=work / "ca.crt")
        self.config = json.loads((self.server.parent / "manifest.example.json").read_text())
        self.config.update(
            source_commit=json.loads((package / "PROVENANCE.json").read_text())["source_commit"],
            origin="https://127.0.0.1",
            intake_actor="worker",
            intake_tenant="org-a",
        )
        self.manifest.write_text(json.dumps(self.config))

    @contextmanager
    def listen(self, selection, *, fixture=False):
        spec = importlib.util.spec_from_file_location("installed_gateway", self.server)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if fixture:
            from opentour_cell.pilot.intake_mcp import intake_dispatch

            def connection(path, config):
                assert (config["intake_tenant"], config["intake_actor"]) == ("org-a", "worker")
                return intake_dispatch(
                    vectors.ACTOR_A, vectors.DurableIntake(path, vectors._GeneratedAuthority(path))
                )

            module.intake_connection = connection
        args = argparse.Namespace(
            config=self.manifest,
            cert=self.work / "tls.crt",
            key=self.work / "tls.key",
            token=self.work / "credential",
            bind="127.0.0.1",
            port=0,
            selection=selection,
        )
        ready = Future()

        async def run():
            try:
                async with await module.bind_server(args) as listener:
                    stop = asyncio.Event()
                    ready.set_result(
                        (listener.sockets[0].getsockname()[1], asyncio.get_running_loop(), stop)
                    )
                    await stop.wait()
            except Exception as error:
                if not ready.done():
                    ready.set_exception(error)
                raise

        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(asyncio.run, run())
            self.port, loop, stop = ready.result(timeout=10)
            try:
                yield
            finally:
                loop.call_soon_threadsafe(stop.set)
                running.result(timeout=10)

    def post(self, name, args, *, drop=False, headers=None):
        client = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.trust, timeout=3)
        try:
            client.request(
                "POST",
                "/v1/" + name,
                json.dumps(args).encode(),
                {
                    "Host": "127.0.0.1",
                    "Origin": "https://127.0.0.1",
                    "X-OpenTour-Audience": self.config["audience"],
                    "Authorization": "Bearer " + self.token,
                    "Content-Type": "application/json",
                }
                | (headers or {}),
            )
            if drop:
                return
            result = client.getresponse()
            data = result.read()
            return result.status, json.loads(data) if data else None
        finally:
            client.close()


@pytest.fixture
def connector(kit, tmp_path):  # noqa: F811
    return Connector(kit, tmp_path)


def stdio(command, name, args):
    frames = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": vectors.PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "generated", "version": "1"},
            },
        },
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/call", "params": {"name": name, "arguments": args}},
    ]
    data = "".join(json.dumps({"jsonrpc": "2.0", **f}) + "\n" for f in frames)
    proc = subprocess.run(
        command, input=data, text=True, capture_output=True, timeout=10, check=True
    )
    result = json.loads(proc.stdout.splitlines()[-1])["result"]
    return (
        {"error": result["content"][0]["text"]}
        if result.get("isError")
        else result["structuredContent"]
    )


def counts(directory):
    with sqlite3.connect(directory / "intake.sqlite3") as db:
        return tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("intake_runs", "intake_workflows", "intake_materialization")
        )


def test_packaged_starter_cross_transport_and_lost_reply(connector):
    home = connector.work / "owner"
    selection, _ = vectors._starter_only(home)
    command = [*PYTHON, connector.kit / "opentour-slack.pyz", "mcp-intake", selection]
    with connector.listen(selection):
        assert connector.post(DISCOVER, {}) == (200, stdio(command, DISCOVER, {}))
        assert counts(home) == (0, 0, 0)
        connector.post(SUBMIT, vectors.STARTER, drop=True)
        for _ in range(100):
            if counts(home) == (1, 1, 0):
                break
            time.sleep(0.01)
        assert counts(home) == (1, 1, 0)
    receipt = stdio(command, SUBMIT, vectors.STARTER | {"idempotency_key": "reconnected"})
    with connector.listen(selection):
        for key in ("starter", "retry-twice"):
            assert connector.post(SUBMIT, vectors.STARTER | {"idempotency_key": key}) == (
                200,
                receipt,
            )
        args = {"run_ref": receipt["run_ref"]}
        assert connector.post(STATUS, args) == (200, stdio(command, STATUS, args))
        assert counts(home) == (1, 1, 0)
        (home / "starter-intake-withdrawn-registered-starter.json").touch(mode=0o600)
        assert connector.post(SUBMIT, vectors.STARTER) == (403, {"error": "intake_unavailable"})
        assert connector.post(STATUS, args) == (403, {"error": "intake_unavailable"})


def starter_profile_isolation(package, root, connector_type=Connector):
    profiles = []
    signing_keys = [secrets.token_hex(32) for _ in range(10)]
    assert len(set(signing_keys)) == 10
    for index, key in enumerate(signing_keys):
        tenant = f"generated-tenant-{index}"
        work = root / tenant
        work.mkdir(mode=0o700)
        connector = connector_type(package, work)
        connector.config.update(intake_tenant=tenant, audience=f"generated-audience-{index}")
        connector.manifest.write_text(json.dumps(connector.config))
        home = work / "owner"
        selection, _ = vectors._starter_only(home, tenant=tenant, signing_key=key)
        command = [*PYTHON, package / "opentour-slack.pyz", "mcp-intake", selection]
        with connector.listen(selection):
            catalog = stdio(command, DISCOVER, {})
            assert catalog["sources"] == [
                {
                    "source_kind": "policy",
                    "source_ref": vectors.STARTER["policy_resource_ref"],
                    "source_revision": vectors.STARTER["source_revision"],
                }
            ]
            assert connector.post(DISCOVER, {}) == (200, catalog)
            assert counts(home) == (0, 0, 0)
            code, admitted = connector.post(SUBMIT, vectors.STARTER)
            assert code == 200 and admitted["state"] == "queued"
            lookup = {"run_ref": admitted["run_ref"]}
            status = stdio(command, STATUS, lookup)
            assert status["state"] == "queued"
            assert connector.post(STATUS, lookup) == (200, status)
            assert counts(home) == (1, 1, 0)
        profiles.append((tenant, connector, selection, command, admitted, status))

    assert len({connector.token for _, connector, *_ in profiles}) == 10
    assert len({admitted["run_ref"] for *_, admitted, status in profiles}) == 10
    assert len({status["workflow_ref"] for *_, status in profiles}) == 10
    replay = vectors.STARTER | {"idempotency_key": "restarted"}
    denied = {"error": "intake_unavailable"}
    for index, (tenant, connector, selection, command, admitted, status) in enumerate(profiles):
        home = selection.parent
        lookup = {"run_ref": admitted["run_ref"]}
        with connector.listen(selection):
            assert connector.post(STATUS, lookup) == (200, status)
            assert stdio(command, STATUS, lookup) == status
            assert connector.post(STATUS, {"run_ref": "missing-generated-run"}) == (403, denied)
            # Probe both directions around the ring. Keep the target audience
            # when changing the bearer to isolate credential validation.
            for offset in (-1, 1):
                _, other, _, _, other_run, other_status = profiles[(index + offset) % len(profiles)]
                foreign_auth = {"Authorization": "Bearer " + other.token}
                assert connector.post(STATUS, lookup, headers=foreign_auth) == (403, None)
                assert connector.post(SUBMIT, replay, headers=foreign_auth) == (403, None)
                foreign_audience = {"X-OpenTour-Audience": other.config["audience"]}
                assert connector.post(STATUS, lookup, headers=foreign_audience) == (403, None)
                foreign_run = {"run_ref": other_run["run_ref"]}
                assert connector.post(STATUS, foreign_run) == (403, denied)
                assert stdio(command, STATUS, foreign_run) == denied
                foreign_workflow = replay | {"workflow_ref": other_status["workflow_ref"]}
                del foreign_workflow["creation_ref"]
                assert connector.post(SUBMIT, foreign_workflow) == (403, denied)
                assert stdio(command, SUBMIT, foreign_workflow) == denied
            with sqlite3.connect(home / "intake.sqlite3") as db:
                assert db.execute("SELECT count(*) FROM intake_keys").fetchone() == (1,)
            assert connector.post(SUBMIT, replay) == (200, admitted)
            assert stdio(command, SUBMIT, replay) == admitted
            assert counts(home) == (1, 1, 0)
        with sqlite3.connect(home / "intake.sqlite3") as db:
            assert db.execute("SELECT tenant,run_ref,workflow_ref FROM intake_runs").fetchall() == [
                (tenant, admitted["run_ref"], status["workflow_ref"])
            ]
            assert db.execute("SELECT tenant,creation_ref FROM intake_workflows").fetchall() == [
                (tenant, vectors.STARTER["creation_ref"])
            ]
            assert db.execute("SELECT count(*) FROM intake_keys").fetchone() == (2,)
        with sqlite3.connect(home / "learning-mandates.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM reservations").fetchone() == (0,)
        with sqlite3.connect(home / "training.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    return len(profiles)


def test_ten_packaged_starter_profiles_are_isolated(kit, tmp_path):  # noqa: F811
    assert starter_profile_isolation(kit, tmp_path) == 10


def test_tls_limits_before_admission(connector):
    selection, _ = vectors._starter_only(connector.work / "limits")
    with connector.listen(selection):
        # The advertised body never arrives. Refuse it immediately from the headers.
        assert connector.post(SUBMIT, {}, headers={"Content-Length": "4097"}) == (413, None)
        assert connector.post(SUBMIT, {}, headers={"Content-Type": "text/plain"}) == (415, None)
        for _ in range(120):
            assert connector.post(DISCOVER, {"limit": 1})[0] == 200
        assert connector.post(SUBMIT, vectors.STARTER) == (429, {"error": "intake_rate_limited"})
        assert counts(selection.parent) == (0, 0, 0)


def test_packaged_historical_pair_cross_transport_and_restart(connector):
    _, actor, pair, producer, mandates, mandate = setup_historical(
        connector.work, baseline_only=True, no_development=True
    )
    home = producer.directory
    selection = producer.authority.path
    connector.config.update(intake_tenant=actor.tenant, intake_actor=actor.actor)
    connector.manifest.write_text(json.dumps(connector.config))
    launcher = [*PYTHON, connector.kit / "opentour-slack.pyz"]
    command = [*launcher, "mcp-intake", selection]
    request = asdict(pair)
    with connector.listen(selection):
        catalog = {
            "sources": [
                {
                    "source_kind": "historical",
                    "source_ref": pair.pair_ref,
                    "source_revision": pair.source_revision,
                }
            ],
            "next_cursor": None,
        }
        assert connector.post(DISCOVER, {}) == (200, catalog)
        assert stdio(command, DISCOVER, {}) == catalog
        assert counts(home) == (0, 0, 0)
        code, admitted = connector.post(SUBMIT, request)
        assert code == 200 and admitted["state"] == "queued"
        assert stdio(command, SUBMIT, request | {"idempotency_key": "stdio-replay"}) == admitted
        lookup = {"run_ref": admitted["run_ref"]}
        queued = stdio(command, STATUS, lookup)
        assert connector.post(STATUS, lookup) == (200, queued)
        assert queued["state"] == "queued" and queued["benchmark_state"] == "draft"
        assert queued["workflow_ref"] == producer.authority.selected.workflow_ref
        assert counts(home) == (1, 0, 0)

    config = home / "historical-worker.json"
    write(
        config,
        {
            "schema_version": HISTORICAL_WORKER_CONFIG,
            "selection": selection.name,
            "traces": producer.traces.name,
            "corrections": producer.corrections.name,
            "agreement": producer.agreement.name,
        },
    )

    def run_worker():
        result = subprocess.run(
            [*launcher, "historical-worker", "--config", config, "--run-ref", lookup["run_ref"]],
            text=True,
            capture_output=True,
            check=True,
            timeout=30,
        )
        assert not result.stderr
        return json.loads(result.stdout)

    completed = run_worker()
    assert completed["status"] == "completed" and completed["run_ref"] == lookup["run_ref"]
    assert completed["benchmark"] == {
        "benchmark_ref": queued["benchmark_ref"],
        "benchmark_state": "scored",
        "version": 1,
    }
    [comparison_path] = (home / "managed-outputs").glob("*/comparison.json")
    original_comparison = comparison_path.read_bytes()
    original_write_time = comparison_path.stat().st_mtime_ns
    assert run_worker() == completed

    with connector.listen(selection):
        status = stdio(command, STATUS, lookup)
        assert connector.post(STATUS, lookup) == (200, status)
        assert status["state"] == "materialized"
        assert status["workflow_ref"] == queued["workflow_ref"]
        assert all(status[key] == value for key, value in completed["benchmark"].items())
        assert connector.post(SUBMIT, request | {"idempotency_key": "https-restart"}) == (
            200,
            admitted | {"state": "materialized"},
        )
        assert connector.post(STATUS, lookup, headers={"Authorization": "Bearer invalid"}) == (
            403,
            None,
        )
        mandates.withdraw(mandate["mandate_id"], actor=mandate["owner_id"])
        assert connector.post(STATUS, lookup) == (403, {"error": "intake_unavailable"})
        assert connector.post(SUBMIT, request) == (403, {"error": "intake_unavailable"})

    revoked = subprocess.run(command, input="", text=True, capture_output=True, timeout=10)
    assert (revoked.returncode, revoked.stdout, revoked.stderr.strip()) == (
        2,
        "",
        "private_intake_selection_unavailable",
    )
    assert counts(home) == (1, 0, 1)
    assert list((home / "managed-outputs").glob("*/comparison.json")) == [comparison_path]
    assert comparison_path.stat().st_mtime_ns == original_write_time
    assert comparison_path.read_bytes() == original_comparison
    comparison = json.loads(original_comparison)
    assert comparison["decision"]["reason"] == "recorded_baseline_only"
    assert comparison["protected_rows_evaluated"] == 16
    with sqlite3.connect(home / "workflow-benchmarks.sqlite3") as db:
        [(record,)] = db.execute("SELECT materialized FROM benchmarks").fetchall()
        assert json.loads(record)["result_digest"] == digest(comparison)
        assert db.execute("SELECT completed FROM benchmark_operations").fetchall() == [(1,)]
        assert db.execute("SELECT run FROM benchmark_receipts").fetchall() == [(lookup["run_ref"],)]
    with sqlite3.connect(home / "training.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM protected_comparisons").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM jobs").fetchone() == (0,)
    with sqlite3.connect(home / "learning-mandates.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone() == (0,)


def live_cross_transport(connector):
    export, _ = configured(connector.work / "owner")
    home, selection = export.directory, export.selection
    launcher = [*PYTHON, connector.kit / "opentour-slack.pyz"]
    command = [*launcher, "mcp-intake", selection]

    original_selection = home / "selection.json"
    actor, starter = open_intake(original_selection)
    original = starter.submit_intake(actor, parse_intake(vectors.STARTER))
    producer = DiskProducer(home)
    producer.state = "frozen_unscored"
    assert starter.materialize(actor, original.run_ref, producer, target_state="frozen_unscored")
    frozen = starter.get_run(actor, original.run_ref).benchmark
    assert frozen is not None and frozen.benchmark_state == "frozen_unscored"
    ledger = home / "workflow-benchmarks.sqlite3"
    frozen_bytes = ledger.read_bytes()
    vectors._write_private(
        home / "benchmark-inspection-selection.json",
        {
            "schema_version": "opentour.pilot.benchmark-inspection-selection.v1",
            "source_selection_file": original_selection.name,
            "benchmark_run_ref": original.run_ref,
        },
    )
    assert counts(home) == (1, 1, 1)

    def refresh(grant, expected_feeds, expected_total):
        ref = export.case.connect(grant.resource)
        request = {
            "mode": "live",
            "feed_binding_ref": ref,
            "source_revision": grant.source_revision,
            "idempotency_key": grant.resource,
        }
        with connector.listen(selection):
            catalog = stdio(command, DISCOVER, {})
            assert connector.post(DISCOVER, {}) == (200, catalog)
            assert {source["feed_kind"] for source in catalog["sources"]} == expected_feeds
            code, admitted = connector.post(SUBMIT, request)
            assert code == 200 and admitted["state"] == "queued"
            cross_key = request | {"idempotency_key": grant.resource + "-stdio"}
            assert stdio(command, SUBMIT, cross_key) == admitted
            lookup = {"run_ref": admitted["run_ref"]}
            assert connector.post(STATUS, lookup, headers={"Authorization": "Bearer invalid"}) == (
                403,
                None,
            )
            queued = stdio(command, STATUS, lookup)
            assert connector.post(STATUS, lookup) == (200, queued)
            assert queued["state"] == "queued" and queued["workflow_ref"] == export.case.workflow
            assert queued["benchmark_ref"] == frozen.benchmark_ref
            assert counts(home) == (1 + expected_total, 1, expected_total)
            assert len(export.receipts()) == expected_total - 1

        def materialize(expected):
            result = subprocess.run(
                [*launcher, "live-reader", selection, admitted["run_ref"]],
                text=True,
                capture_output=True,
                check=True,
                timeout=30,
            )
            assert (result.stdout.strip(), result.stderr) == (expected, "")

        materialize("materialized")
        recorded = export.receipts()
        assert len(recorded) == expected_total
        page = recorded[-1]
        assert (page["binding_ref"], page["source_revision"], page["checkpoint"]) == (
            ref,
            grant.source_revision,
            grant.checkpoint + 1,
        )
        assert page["row_count"] == 1 and page["complete"] is True
        catalog_file = export.resources[grant.resource]["file"]
        (pinned,) = json.loads((home / catalog_file).read_text())["pages"]
        assert page["page_ref"] == pinned["page_ref"]
        with sqlite3.connect(export.case.live.path) as db:
            assert db.execute(
                "SELECT digest,receipt_ref FROM live_pages WHERE binding_ref=? AND page_ref=?",
                (ref, pinned["page_ref"]),
            ).fetchall() == [(pinned["page_digest"], page["receipt_ref"])]
        materialize("already_materialized")

        with connector.listen(selection):
            settled = stdio(command, STATUS, lookup)
            assert connector.post(STATUS, lookup) == (200, settled)
            assert (
                settled["state"] == "materialized"
                and settled["workflow_ref"] == queued["workflow_ref"]
            )
            assert settled["benchmark_ref"] == frozen.benchmark_ref
            replay = admitted | {"state": "materialized"}
            assert connector.post(SUBMIT, cross_key) == (200, replay)
            assert stdio(command, SUBMIT, request) == replay
        assert export.receipts() == recorded
        assert counts(home) == (1 + expected_total, 1, 1 + expected_total)
        assert ledger.read_bytes() == frozen_bytes
        return admitted["run_ref"]

    decisions = refresh(DECISION, {"decision"}, 1)
    pending = export.snapshot()
    assert (pending.decisions, pending.pending, pending.verified) == (1, 1, 0)
    assert (home / "generated-producer-count").read_text() == "1"
    assert len(export.receipts()) == 1

    export.install(OUTCOME, [[export.row(outcome(), 21)]])
    outcomes = refresh(OUTCOME, {"decision", "independent_outcome"}, 2)
    assert decisions != outcomes
    verified = export.snapshot()
    assert (verified.decisions, verified.pending, verified.verified) == (1, 0, 1)
    assert verified.verified_roots[0].decision_id == decision()["decision_id"]
    assert len({page["receipt_ref"] for page in export.receipts()}) == 2
    assert (home / "generated-producer-count").read_text() == "1"
    with sqlite3.connect(home / "learning-mandates.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM reservations").fetchone() == (0,)
    with sqlite3.connect(home / "training.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone() == (0,)


def test_enabled_live_reader_cross_transport_and_restart(connector):
    live_cross_transport(connector)


def test_historical_live_replay_authority_and_bounds(connector):
    root = connector.work / "fixture"
    root.mkdir(mode=0o700)
    flags = root / "fixture-state.json"

    def change(**values):
        flags.write_text(json.dumps({"missing_reader": True, **values}))

    change()
    vectors._mandate(root, "org-a")
    command = [sys.executable, vectors.__file__, str(root), "org-a", "intake", "0"]
    with connector.listen(root, fixture=True):
        assert connector.post(DISCOVER, {"limit": 1}) == (
            200,
            stdio(command, DISCOVER, {"limit": 1}),
        )
        cases = [
            (SUBMIT, vectors.PAIR | {"tenant": "other"}, 400),
            (SUBMIT, vectors.LIVE | {"correction_revision": "correction"}, 400),
            (SUBMIT, vectors.STARTER | {"workflow_ref": "extra"}, 400),
            (DISCOVER, {"limit": 51}, 400),
            ("source", {}, 404),
            (SUBMIT, {"large": "a" * 5000}, 413),
        ]
        for name, args, code in cases:
            assert connector.post(name, args)[0] == code
        for invalid in (
            {"Origin": "https://foreign.invalid"},
            {"X-OpenTour-Audience": "wrong"},
            {"Authorization": "Bearer invalid"},
        ):
            assert connector.post(SUBMIT, vectors.PAIR, headers=invalid) == (403, None)
        assert counts(root) == (0, 0, 0)
        for expected, arguments in enumerate((vectors.PAIR, vectors.LIVE), start=1):
            first = stdio(command, SUBMIT, arguments)
            assert connector.post(
                SUBMIT, arguments | {"idempotency_key": "other-" + str(expected)}
            ) == (200, first)
            lookup = {"run_ref": first["run_ref"]}
            view = stdio(command, STATUS, lookup)
            assert connector.post(STATUS, lookup) == (200, view)
            assert counts(root) == (expected, 0, 0)
        assert view["state"] == "pending" and view["reason_code"] == "live_reader_unavailable"
        assert view["benchmark_ref"] == "frozen-benchmark" and view["version"] == 3
        assert connector.post(SUBMIT, vectors.PAIR | {"source_revision": "trace-v2"}) == (
            409,
            {"error": "idempotency_conflict"},
        )
        for permitted in (False, True):
            change(superseded=True, permit_successor=permitted)
            hold = stdio(command, SUBMIT, vectors.LIVE)
            assert hold["reason_code"] == "binding_superseded"
            assert ("successor_feed_binding_ref" in hold) == permitted
            assert connector.post(SUBMIT, vectors.LIVE) == (200, hold)
        queue = vectors.DurableIntake(root, vectors._GeneratedAuthority(root))
        reads = []

        def read(work):
            reads.append(work)
            return "generated-source-receipt"

        change()
        assert not queue.materialize(vectors.ACTOR_A, lookup["run_ref"], read)
        for refusal in ({"revoked": True}, {"active_epoch": "epoch-b"}, {"foreign_binding": True}):
            change(**refusal)
            for name, args in ((SUBMIT, vectors.LIVE), (STATUS, lookup)):
                denied = stdio(command, name, args)
                assert denied == {"error": "intake_unavailable"}
                assert connector.post(name, args) == (403, denied)
            with pytest.raises(vectors.IntakeError, match="intake_unavailable"):
                queue.materialize(vectors.ACTOR_A, lookup["run_ref"], read)
        assert reads == []
        assert counts(root) == (2, 0, 0)
        with sqlite3.connect(root / "learning-mandates.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM reservations").fetchone()[0] == 0


# ---- OPE-559: learning-run routes on the installed gateway, under a separate grant ------------


class LearningConnector(Connector):
    """The installed gateway with its own learning-run credential, audience and selection."""

    def __init__(self, package, work, site, selection, client, audience):
        work.mkdir(mode=0o700)
        super().__init__(package, work)
        self.selection = selection
        actor = json.loads(selection.read_text())["actor"]
        self.config.update(
            audience=audience,
            learning_tenant=site.tenant,
            learning_actor=actor,
            learning_client=client,
        )
        self.manifest.write_text(json.dumps(self.config))

    @contextmanager
    def serve(self, *, ledger=None, selection=None, learning=True):
        spec = importlib.util.spec_from_file_location("installed_gateway", self.server)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if ledger is not None:
            # Generated OPE-442 stand-in; the installed default has no ledger and holds fits.
            installed = module.learning_connection
            module.learning_connection = lambda path, config: installed(path, config, ledger=ledger)
        args = argparse.Namespace(
            config=self.manifest,
            cert=self.work / "tls.crt",
            key=self.work / "tls.key",
            token=self.work / "credential",
            bind="127.0.0.1",
            port=0,
            selection=selection,
            learning_selection=self.selection if learning else None,
        )
        ready = Future()

        async def run():
            try:
                async with await module.bind_server(args) as listener:
                    stop = asyncio.Event()
                    ready.set_result(
                        (listener.sockets[0].getsockname()[1], asyncio.get_running_loop(), stop)
                    )
                    await stop.wait()
            except Exception as error:
                if not ready.done():
                    ready.set_exception(error)
                raise

        with ThreadPoolExecutor(max_workers=1) as executor:
            running = executor.submit(asyncio.run, run())
            self.port, loop, stop = ready.result(timeout=10)
            try:
                yield
            finally:
                loop.call_soon_threadsafe(stop.set)
                running.result(timeout=10)

    def request(self, method, path, body=None, *, drop=False, headers=None, raw=None):
        client = http.client.HTTPSConnection("127.0.0.1", self.port, context=self.trust, timeout=5)
        try:
            sent = {
                "Host": "127.0.0.1",
                "Origin": "https://127.0.0.1",
                "X-OpenTour-Audience": self.config["audience"],
                "Authorization": "Bearer " + self.token,
            }
            payload = (
                raw if raw is not None else None if body is None else json.dumps(body).encode()
            )
            if payload is not None:
                sent["Content-Type"] = "application/json"
            client.request(method, path, payload, sent | (headers or {}))
            if drop:
                return None
            result = client.getresponse()
            data = result.read()
            return result.status, json.loads(data) if data else None
        finally:
            client.close()


def packaged(package, *args):
    return subprocess.run(
        [*PYTHON, package / "opentour-slack.pyz", *args],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def packaged_grant(package, site, name, *, role="owner", channel="slack", client=None):
    selection = site.directory / f"{name}.json"
    done = packaged(
        package,
        "learning-runs",
        "authorize",
        "--selection",
        str(selection),
        "--tenant",
        site.tenant,
        "--owner",
        runs.OWNER,
        "--actor",
        runs.DELEGATE if role == "scheduler" else runs.OWNER if role == "owner" else role,
        "--role",
        role,
        "--channel",
        channel,
        "--client",
        client or f"client-{name}",
        "--workflow",
        runs.WORKFLOW,
        "--mandate",
        runs.MANDATE,
        "--grant-ref",
        f"grant-{name}",
        "--expires-at",
        str(int(time.time()) + 3600),
    )
    assert done.returncode == 0, done.stdout + done.stderr
    return selection


def learning_counts(site):
    counted = site.counts()
    return counted["lr_runs"], len(site.ledger.reservations), site.trainer.submissions


RUNS = "/v1/learning-runs"


def test_learning_routes_replace_404_under_a_scoped_grant(kit, tmp_path):  # noqa: F811
    site = runs.Site(tmp_path)
    selection = packaged_grant(kit, site, "slack-owner", client="slack-hosted-1")
    gateway = LearningConnector(
        kit, tmp_path / "learning-tls", site, selection, "slack-hosted-1", "learning-audience"
    )
    # The positive legacy control: the intake connection still serves only its three routes.
    home = tmp_path / "intake-owner"
    intake_selection, _ = vectors._starter_only(home)
    intake = Connector(kit, tmp_path)
    with intake.listen(intake_selection):
        assert intake.post(DISCOVER, {})[0] == 200
        for path in (RUNS, RUNS + "/lrun-1/cancel"):
            assert intake.post(path.removeprefix("/v1/"), {"idempotency_key": "k"}) == (404, None)
    assert counts(home) == (0, 0, 0)
    body = {"plan_ref": "plan-unknown", "operation": "evaluate_and_improve", "idempotency_key": "a"}
    with gateway.serve():
        held = gateway.request("POST", RUNS, body)
        assert held[0] == 200 and held[1]["reason_code"] == "no_eligible_plan"
        assert "run_ref" not in held[1]
        plan_ref = site.publish()
        body["plan_ref"] = plan_ref
        # The installed default has no OPE-442 ledger, so fit-capable work is held, not admitted.
        code, held = gateway.request("POST", RUNS, body)
        assert (code, held["state"], held["reason_code"]) == (200, "held", "fit_ledger_unavailable")
        benchmark = body | {"operation": "benchmark_only", "idempotency_key": "bench-1"}
        code, admitted = gateway.request("POST", RUNS, benchmark)
        assert code == 200 and admitted["state"] == "benchmark_only"
        run = admitted["run_ref"]
        assert gateway.request("POST", RUNS, benchmark) == (200, admitted)
        assert gateway.request("POST", RUNS, benchmark | {"idempotency_key": "bench-2"}) == (
            200,
            admitted,
        )
        assert gateway.request("GET", f"{RUNS}/{run}") == (200, admitted)
        changed = benchmark | {"operation": "evaluate_and_improve"}
        assert gateway.request("POST", RUNS, changed) == (409, {"error": "idempotency_conflict"})
        for extra in (
            {"backend_url": "https://x.invalid"},
            {"release": "approve"},
            {"tenant": "b"},
        ):
            assert gateway.request("POST", RUNS, benchmark | extra) == (
                400,
                {"error": "invalid_arguments"},
            )
        assert gateway.request("GET", f"{RUNS}/lrun-missing") == (
            403,
            {"error": "learning_run_unavailable"},
        )
        assert gateway.request("GET", f"{RUNS}/{run}", raw=b"{}")[0] == 400
        assert (
            gateway.request(
                "POST", f"{RUNS}/{run}/cancel", {"run_ref": run, "idempotency_key": "c"}
            )[0]
            == 400
        )
        for path in ("/v1/discover_sources", "/v1/get_run", "/v1/health", f"{RUNS}/{run}/approve"):
            assert gateway.request("POST", path, {"idempotency_key": "x"}) == (404, None)
        assert gateway.request("GET", "/v1/health") == (404, None)
        assert gateway.request("GET", f"{RUNS}/{run}", headers={"Authorization": "Bearer x"}) == (
            403,
            None,
        )
        wrong_audience = {"X-OpenTour-Audience": "intake-audience"}
        assert gateway.request("GET", f"{RUNS}/{run}", headers=wrong_audience) == (403, None)
        canceled = gateway.request("POST", f"{RUNS}/{run}/cancel", {"idempotency_key": "c"})
        assert canceled[0] == 200
        assert (canceled[1]["cancellation"], canceled[1]["state"]) == ("confirmed", "canceled")
        assert gateway.request("POST", f"{RUNS}/{run}/cancel", {"idempotency_key": "c"}) == canceled
    # Revoking the grant ends access at once and reveals no earlier run reference.
    with gateway.serve():
        revoked = packaged(
            kit, "learning-runs", "withdraw", "--selection", str(selection), "--owner", runs.OWNER
        )
        assert revoked.returncode == 0
        denied = (403, {"error": "learning_run_unavailable"})
        assert gateway.request("POST", RUNS, benchmark) == denied
        assert gateway.request("GET", f"{RUNS}/{run}") == denied
        assert gateway.request("POST", f"{RUNS}/{run}/cancel", {"idempotency_key": "c"}) == denied
    assert site.counts()["lr_runs"] == 1
    # A health-only listener still answers health and serves none of the learning-run routes.
    with gateway.serve(learning=False):
        assert gateway.request("GET", "/v1/health")[0] == 200
        assert gateway.request("GET", f"{RUNS}/{run}") == (404, None)
        assert gateway.request("POST", RUNS, benchmark) == (404, None)
    # One listener never serves intake and learning-run control together.
    with pytest.raises(ValueError, match="separate listeners"):
        with gateway.serve(selection=intake_selection):
            pass


def test_tls_slack_zapier_and_schedule_converge_on_one_run_and_one_fit(kit, tmp_path):  # noqa: F811
    site = runs.Site(tmp_path)
    slack_selection = packaged_grant(kit, site, "slack-owner", client="slack-hosted-1")
    zap_selection = packaged_grant(kit, site, "zap-owner", channel="zapier", client="zap-1")
    packaged_grant(kit, site, "learning-run-schedule", role="scheduler", channel="schedule")
    scheduler, worker = site.open(site.directory / "learning-run-schedule.json")
    slack = LearningConnector(
        kit, tmp_path / "slack-tls", site, slack_selection, "slack-hosted-1", "slack-audience"
    )
    zap = LearningConnector(kit, tmp_path / "zap-tls", site, zap_selection, "zap-1", "zap-audience")
    plan_ref = site.publish()
    body = {"plan_ref": plan_ref, "operation": "evaluate_and_improve", "idempotency_key": "s-1"}
    with slack.serve(ledger=site.ledger):
        # Run improvement; the reply is lost after durable admission.
        slack.request("POST", RUNS, body, drop=True)
        for _ in range(200):
            if site.counts()["lr_runs"] == 1:
                break
            time.sleep(0.01)
    assert learning_counts(site) == (1, 0, 0)
    # The schedule tick, the restarted listener, Zapier and Socket Mode all reach the same run.
    tick = worker.schedule_tick(scheduler)
    assert (tick["tick"], tick["reason_code"]) == ("no_change", "already_admitted")
    with slack.serve(ledger=site.ledger), zap.serve(ledger=site.ledger):
        code, again = slack.request("POST", RUNS, body)
        assert code == 200 and again["run_ref"] == tick["run_ref"]
        code, zapped = zap.request("POST", RUNS, body | {"idempotency_key": "zap-trigger-1"})
        assert code == 200 and zapped["run_ref"] == again["run_ref"]
        local_principal, local_service = site.open(slack_selection)
        local = lr.learning_run_dispatch(local_principal, local_service)
        socket = local(lr.SUBMIT, body | {"idempotency_key": "socket-1"})["structuredContent"]
        assert socket["run_ref"] == again["run_ref"]
        claim = runs.drive_to_review(site, worker, scheduler)
        assert claim.run_ref == again["run_ref"]
        run = again["run_ref"]
        code, status = slack.request("GET", f"{RUNS}/{run}")
        assert code == 200 and status["state"] == "review_ready"
        assert status["usage"] == {
            "fit_reservations": 1,
            "reserved_units": 90,
            "consumed_units": 42,
        }
        assert zap.request("GET", f"{RUNS}/{run}") == (200, status)
        late = zap.request("POST", f"{RUNS}/{run}/cancel", {"idempotency_key": "late"})
        assert (late[0], late[1]["cancellation"]) == (200, "not_applicable")
        # The scheduled path cannot start a second run while this one awaits review.
        assert packaged(
            kit,
            "cycle-worker",
            "--directory",
            str(site.directory),
            "--organization",
            site.tenant,
            "--config",
            str(site.directory / "cycle.json"),
            "--interval",
            "300",
            "--once",
        ).stdout.strip() == json.dumps(
            {"state": "no_change", "reason": "already_admitted", "run_id": run}
        )
    assert learning_counts(site) == (1, 1, 1)
    with sqlite3.connect(site.directory / lr.DATABASE) as db:
        triggers = db.execute(
            "SELECT channel,count(*) FROM lr_triggers GROUP BY channel"
        ).fetchall()
    assert dict(triggers) == {"slack": 2, "zapier": 1}
    assert site.ticks() == [("no_change", "already_admitted")] * 2


def test_tls_role_tenant_and_revocation_boundaries(kit, tmp_path):  # noqa: F811
    site = runs.Site(tmp_path)
    other = runs.Site(tmp_path, tenant="org-b")
    owner = packaged_grant(kit, site, "slack-owner", client="slack-hosted-1")
    viewer = packaged_grant(kit, site, "slack-reviewer", role="reviewer", client="slack-viewer")
    foreign = packaged_grant(kit, other, "slack-owner", client="slack-hosted-1")
    owner_gw = LearningConnector(kit, tmp_path / "owner-tls", site, owner, "slack-hosted-1", "a1")
    viewer_gw = LearningConnector(kit, tmp_path / "viewer-tls", site, viewer, "slack-viewer", "a2")
    foreign_gw = LearningConnector(
        kit, tmp_path / "foreign-tls", other, foreign, "slack-hosted-1", "a3"
    )
    plan_ref = site.publish()
    body = {"plan_ref": plan_ref, "operation": "benchmark_only", "idempotency_key": "k"}
    with owner_gw.serve(), viewer_gw.serve(), foreign_gw.serve():
        code, admitted = owner_gw.request("POST", RUNS, body)
        assert code == 200
        run = admitted["run_ref"]
        role = (403, {"error": "learning_run_role_required"})
        assert viewer_gw.request("POST", RUNS, body | {"idempotency_key": "v"}) == role
        assert viewer_gw.request("POST", f"{RUNS}/{run}/cancel", {"idempotency_key": "v"}) == role
        code, seen = viewer_gw.request("GET", f"{RUNS}/{run}")
        assert code == 200 and "usage" not in seen and seen["state"] == "benchmark_only"
        unavailable = (403, {"error": "learning_run_unavailable"})
        assert foreign_gw.request("GET", f"{RUNS}/{run}") == unavailable
        assert foreign_gw.request("POST", f"{RUNS}/{run}/cancel", {"idempotency_key": "f"}) == (
            unavailable
        )
        # Each listener accepts only its own credential.
        stolen = {"Authorization": "Bearer " + owner_gw.token}
        assert foreign_gw.request("GET", f"{RUNS}/{run}", headers=stolen) == (403, None)
        assert viewer_gw.request("POST", RUNS, body, headers=stolen) == (403, None)
        # A paused mandate admits nothing and fences the queued run before dispatch.
        site.pause()
        code, paused = owner_gw.request(
            "POST", RUNS, body | {"plan_ref": site.publish(site.plan("v2")), "idempotency_key": "p"}
        )
        assert (code, paused["reason_code"]) == (200, "standing_mandate_paused")
        code, fenced = owner_gw.request("GET", f"{RUNS}/{run}")
        assert (fenced["state"], fenced["reason_code"]) == ("refused", "standing_mandate_paused")
    assert site.counts()["lr_runs"] == 1 and other.counts()["lr_runs"] == 0


class PackagedProcess(Connector):
    """Additional direct CLI qualification, separately from supervised loopback tests."""

    @contextmanager
    def listen(self, selection, *, fixture=False):
        assert not fixture
        proc = subprocess.Popen(
            [
                *PYTHON,
                self.server,
                "--config",
                self.manifest,
                "--cert",
                "tls.crt",
                "--key",
                "tls.key",
                "--token",
                "credential",
                "--port",
                "0",
                "--selection",
                selection,
            ],
            cwd=self.work,
            stdout=subprocess.PIPE,
            text=True,
        )
        try:
            self.port = int(proc.stdout.readline())
            yield
        finally:
            proc.terminate()
            proc.wait(timeout=5)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--packaged-proof", type=Path, required=True)
    package = parser.parse_args().packaged_proof.resolve(strict=True)
    with tempfile.TemporaryDirectory() as work:
        test_packaged_starter_cross_transport_and_lost_reply(PackagedProcess(package, Path(work)))
    with tempfile.TemporaryDirectory() as work:
        test_packaged_historical_pair_cross_transport_and_restart(
            PackagedProcess(package, Path(work))
        )
    with tempfile.TemporaryDirectory() as work:
        live_cross_transport(PackagedProcess(package, Path(work)))
    with tempfile.TemporaryDirectory() as work:
        profile_count = starter_profile_isolation(package, Path(work), PackagedProcess)
    print(
        json.dumps(
            {
                "fixture": "generated",
                "tls_cli_restarts": 1,
                "starter_runs": 1,
                "isolated_starter_profiles": profile_count,
                "isolated_starter_profile_admissions": profile_count,
                "isolated_starter_profile_restarts_verified": True,
                "isolated_starter_profile_neighbor_probes_denied": True,
                "lost_reply_recovered": True,
                "withdrawn_retry_denied": True,
                "historical_admissions": 1,
                "historical_baselines": 1,
                "historical_restarts_verified": True,
                "historical_revoked_retry_denied": True,
                "live_admissions": 2,
                "live_source_receipts": 2,
                "verified_roots": 1,
                "live_restarts_verified": True,
                "frozen_benchmark_preserved": True,
                "fit_reservations": 0,
            }
        )
    )
