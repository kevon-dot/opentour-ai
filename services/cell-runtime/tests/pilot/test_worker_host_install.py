"""A generated customer-owned install with a real TLS client and managed queue."""

import argparse
import asyncio
import hashlib
import http.client
import json
import os
import runpy
import secrets
import shutil
import sqlite3
import ssl
import subprocess
import sys
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

ROOT = Path(__file__).resolve().parents[4]
PYTHON = [sys.executable, "-I", "-S", "-B"]


def run(*command, cwd=None, data=None):
    return subprocess.check_output(command, cwd=cwd, input=data, timeout=30)


@pytest.fixture(scope="module")
def kit(tmp_path_factory):
    work = tmp_path_factory.mktemp("installed")
    archive = work / "kit.zip"
    run(*PYTHON, ROOT / "tools/build_slack_pilot_kit.py", archive)
    with ZipFile(archive) as reader:
        reader.extractall(work)
    for entry in (work / "SHA256SUMS").read_text().splitlines():
        expected, name = entry.split("  ")
        assert hashlib.sha256((work / name).read_bytes()).hexdigest() == expected
    return work


def test_private_tls_rotation(kit, tmp_path):
    folder = kit / "integrations/worker-host"
    revision = json.loads((kit / "PROVENANCE.json").read_text())["source_commit"]
    config = json.loads((folder / "manifest.example.json").read_text())
    config.update(origin="https://127.0.0.1", source_commit=revision)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(config))
    credential = tmp_path / "credential"
    old, new = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    credential.touch(mode=0o600)
    credential.write_text(old)
    extensions = tmp_path / "extensions"
    extensions.write_text("subjectAltName=IP:127.0.0.1\nextendedKeyUsage=serverAuth\n")

    def openssl(*args):
        subprocess.run(
            ["openssl", *args], cwd=tmp_path, check=True, capture_output=True, timeout=20
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
        "/CN=localhost",
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
    start = [
        *PYTHON,
        folder / "health.py",
        "--config",
        manifest,
        "--key",
        "tls.key",
        "--cert",
        "tls.crt",
        "--token",
        credential,
        "--port",
        "0",
    ]
    manifest.write_text(json.dumps(config | {"release_version": "different-release"}))
    rejected = subprocess.run(start, cwd=tmp_path, capture_output=True, timeout=5)
    assert rejected.returncode != 0 and not rejected.stdout
    assert b"invalid origin or package binding" in rejected.stderr
    manifest.write_text(json.dumps(config))
    bind_server = runpy.run_path(str(folder / "health.py"))["bind_server"]

    async def check_listener():
        server = await bind_server(
            argparse.Namespace(
                config=manifest,
                cert=tmp_path / "tls.crt",
                key=tmp_path / "tls.key",
                token=credential,
                bind="127.0.0.1",
                port=0,
            )
        )
        async with server:
            await check_requests(server.sockets[0].getsockname()[1])

    async def check_requests(port):
        trusted = ssl.create_default_context(cafile=tmp_path / "ca.crt")

        def exchange(token, method="GET", path="/v1/health", ctx=trusted, **changes):
            headers = {
                "Host": "127.0.0.1",
                "Origin": config["origin"],
                "X-OpenTour-Audience": config["audience"],
                "Authorization": "Bearer " + token,
            }
            client = http.client.HTTPSConnection("127.0.0.1", port, context=ctx, timeout=3)
            try:
                client.request(method, path, headers=headers | changes)
                reply = client.getresponse()
                return reply.status, reply.read()
            finally:
                client.close()

        async def request(*args, **kwargs):
            return await asyncio.to_thread(exchange, *args, **kwargs)

        status, payload = await request(old)
        assert status == 200
        assert json.loads(payload) == {
            "status": "ready",
            "component": "health-endpoint",
            "site": config["site"],
            "revision": config["revision"],
            "release": json.loads((kit / "PROVENANCE.json").read_text())["kit_version"],
            "source_commit": revision,
        }
        with pytest.raises(ssl.SSLCertVerificationError):
            await request(old, ctx=ssl.create_default_context())
        for change in (
            {"Host": "other"},
            {"Origin": "https://other.invalid"},
            {"X-OpenTour-Audience": "other"},
            {"Authorization": ""},
        ):
            assert await request(old, **change) == (403, b"")
        for path in ("/v1/health?x", "/v1/source", "/v1/model", "/v1/submit_intake"):
            assert await request(old, path=path) == (404, b"")
        assert await request(old, method="POST") == (404, b"")
        replacement = tmp_path / "next"
        replacement.touch(mode=0o600)
        replacement.write_text(new)
        replacement.replace(credential)
        assert await request(old) == (403, b"")
        assert await request(new) == (200, payload)
        credential.chmod(0o640)
        assert await request(new) == (403, b"")
        credential.unlink()
        assert await request(new) == (403, b"")

    asyncio.run(check_listener())


def test_packaged_worker_restart_restore(kit, tmp_path):
    state = tmp_path / "private"
    selection = json.loads(run(*PYTHON, kit / "generated/prepare-repair-example.py", state))
    args = [
        "--directory",
        state,
        "--organization",
        selection["organization"],
        "--manifest",
        state / "managed-plan.json",
        "--authorization",
        state / "authorization.json",
    ]
    current = kit / "opentour-slack.pyz"
    # Qualification can supply the digest-verified prior kit for an actual downgrade.
    fallback = Path(os.environ.get("OPENTOUR_TEST_PRIOR_PYZ", current))

    def command(package, name, *extra):
        return [*PYTHON, package, name, *args, *extra]

    def mcp(package, name, arguments):
        messages = [
            {
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "generated", "version": "1"},
                },
            },
            {"method": "notifications/initialized"},
            {"id": 2, "method": "tools/call", "params": {"name": name, "arguments": arguments}},
        ]
        data = "".join(json.dumps(dict(jsonrpc="2.0", **m)) + "\n" for m in messages).encode()
        reply = json.loads(run(*command(package, "mcp-workflow"), data=data).splitlines()[-1])
        return json.loads(reply["result"]["content"][0]["text"])

    run(
        *command(
            fallback,
            "workflow-authorize",
            "--plan-revision",
            selection["plan_revision"],
            "--custodian-attestation",
            "att-0123456789abcdef",
            "--expires-at",
            str(int(time.time()) + 1800),
        )
    )
    assert json.loads(run(*command(fallback, "workflow-worker", "--once"))) == {"state": "idle"}
    queued = mcp(
        fallback,
        "start_offline_evaluation",
        {
            "plan_revision": selection["plan_revision"],
            "idempotency_ref": "generated-restart",
        },
    )
    assert queued["state"] == "queued"
    receipt = json.loads(run(*command(current, "workflow-worker", "--once")))
    assert receipt["state"] == "ready" and receipt["run_id"] == queued["run_id"]
    snapshot = tmp_path / "snapshot"
    shutil.copytree(state, snapshot)
    assert snapshot.stat().st_mode & 0o777 == 0o700
    state.rename(tmp_path / "pre-restore")
    shutil.copytree(snapshot, state)
    # Set old atimes so Linux relatime records even one read of either restored source.
    sources = list(state.glob("*.jsonl"))
    assert len(sources) == 2
    for source in sources:
        os.utime(source, ns=(1, source.stat().st_mtime_ns))
    assert json.loads(run(*command(fallback, "workflow-worker", "--once"))) == {"state": "idle"}
    assert mcp(fallback, "get_offline_evaluation", {"run_id": queued["run_id"]}) == receipt
    for source in sources:
        assert source.stat().st_atime_ns == 1
        source.read_bytes()
        assert source.stat().st_atime_ns > 1
    from opentour_cell.pilot.managed_run import QUEUE_FILE

    with sqlite3.connect(state / QUEUE_FILE) as db:
        assert db.execute("SELECT count(*),sum(attempts) FROM runs").fetchone() == (1, 1)
