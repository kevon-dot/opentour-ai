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
from pathlib import Path

import pytest
import test_intake_mcp as vectors
from test_worker_host_install import PYTHON, kit  # noqa: F401

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
    print(
        json.dumps(
            {
                "fixture": "generated",
                "tls_cli_restarts": 1,
                "starter_runs": 1,
                "lost_reply_recovered": True,
                "withdrawn_retry_denied": True,
            }
        )
    )
