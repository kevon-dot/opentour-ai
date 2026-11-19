"""Direct localhost mutual-TLS test with generated keys and disposable PostgreSQL."""

import asyncio
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
from dataclasses import replace
from datetime import timedelta
from http.client import HTTPResponse
from pathlib import Path
from uuid import uuid4

import pytest
import uvicorn
from cryptography.x509.oid import ExtendedKeyUsageOID
from opentour_control.auth.browser_private import MAX_RESPONSE_BYTES
from opentour_control.auth.browser_private_pilot import PrivateBrowserPilotPathProfile
from opentour_control.auth.browser_private_pilot_listener import (
    build_private_browser_pilot_listener,
)
from opentour_control.auth.config import digest
from test_browser_private_listener import HOST, cert, write_pair
from test_browser_private_listener import tls_fixture as tls_fixture
from test_identity_postgres import ROOT, identity_postgres_fixture
from test_identity_postgres import browser_state as browser_state
from test_identity_postgres import durable as durable
from test_identity_postgres import mode as mode

pytestmark = pytest.mark.local_integration


@pytest.fixture(scope="module")
def postgres():
    with identity_postgres_fixture() as fixture:
        yield fixture


@pytest.mark.parametrize(
    "path_profile",
    [PrivateBrowserPilotPathProfile.LOOPBACK, PrivateBrowserPilotPathProfile.DEV],
    ids=["loopback-paths", "dev-stage-paths"],
)
def test_private_pilot_listener_uses_mutual_tls_and_durable_identity(
    browser_state, durable, identity, tls_fixture, path_profile
):
    _, options, authority, stage, wrong, _ = tls_fixture
    loopback_prefix = "/internal/browser-identity/v1"
    dev_prefix = "/dev/internal/browser-identity/v1"
    prefix, opposite = loopback_prefix, dev_prefix
    if path_profile is PrivateBrowserPilotPathProfile.DEV:
        approved = options["approved"]
        assert approved.stage_arn.endswith("/fixture-stage")
        approved = replace(
            approved, stage_arn=approved.stage_arn.removesuffix("/fixture-stage") + "/dev"
        )
        options = {
            **options,
            "approved": approved,
            "readback": replace(options["readback"], binding=approved),
        }
        prefix, opposite = dev_prefix, loopback_prefix

    flow = {
        "state": secrets.token_urlsafe(32),
        "nonce": secrets.token_urlsafe(32),
        "browserDigest": digest(secrets.token_urlsafe(32)),
        "verifier": secrets.token_urlsafe(32),
        "expiresAt": int((identity.now + timedelta(seconds=120)).timestamp() * 1000),
        "returnPath": "/",
        "kind": "signin",
        "sessionHandle": None,
        "subject": None,
        "sessionId": None,
        "generation": None,
        "selectionTarget": None,
        "selectionIntent": None,
    }
    claim = {"state": f"{flow['state']}.{flow['nonce']}", "browser": flow["browserDigest"]}
    sid = f"generated-mtls-{uuid4().hex}"
    credentials = {
        "accessToken": identity.token(sid=sid),
        "refreshToken": "GENERATED_MTLS_REFRESH",
        "userId": durable.context.actor["subject"],
    }

    def exchange(port, path, body, pair):
        context = ssl.create_default_context(cafile=authority)
        if pair is not None:
            context.load_cert_chain(*pair)
        payload = json.dumps(body).encode()
        headers = (
            f"POST {path} HTTP/1.1\r\nHost: {HOST}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n"
        ).encode("ascii")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=4) as raw:
                with context.wrap_socket(raw, server_hostname=HOST) as peer:
                    peer.sendall(headers + payload)
                    with HTTPResponse(peer) as response:
                        response.begin()
                        data = response.read(MAX_RESPONSE_BYTES + 1)
                        assert len(data) <= MAX_RESPONSE_BYTES
                        assert response.getheader("Cache-Control") == "no-store"
                        assert response.getheader("Content-Type") == "application/json"
                        return response.status, json.loads(data)
        except (ssl.SSLError, ConnectionError):
            if pair == stage:
                raise
            return None

    async def exercise():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            config = build_private_browser_pilot_listener(
                browser_state, path_profile=path_profile, **(options | {"port": port})
            )
            server = uvicorn.Server(config)
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(4):
                    while not server.started:
                        await asyncio.sleep(0.01)

                async def post(path, body, pair=stage, *, path_prefix=prefix):
                    return await asyncio.wait_for(
                        asyncio.to_thread(exchange, port, path_prefix + path, body, pair),
                        timeout=5,
                    )

                status, error = await post("/flows/create", {"flow": flow}, path_prefix=opposite)
                assert (status, error["code"]) == (404, "PRIVATE_OPERATION_NOT_FOUND")
                assert await post("/flows/create", {"flow": flow}) == (200, None)
                assert await post("/flows/consume", claim, None) is None
                assert await post("/flows/consume", claim, wrong) is None
                with durable.connect() as connection:
                    row = connection.execute(
                        "SELECT consumed_at FROM opentour_identity.auth_transactions "
                        "WHERE state_sha256=%s",
                        (digest(claim["state"]),),
                    ).fetchone()
                    assert row == {"consumed_at": None}
                assert await post("/flows/consume", claim) == (200, flow)
                status, error = await post("/flows/consume", claim)
                assert (status, error["code"]) == (401, "CALLBACK_INVALID")

                expired = identity.token(sid=sid, exp=int(identity.now.timestamp()))
                status, error = await post(
                    "/sessions/admit",
                    {"credentials": {**credentials, "accessToken": expired}},
                )
                assert (status, error["code"]) == (401, "SESSION_INVALID")
                status, admitted = await post("/sessions/admit", {"credentials": credentials})
                assert status == 200 and admitted["identity"]["session_id"] == sid
                handle = admitted["handle"]
                assert await post("/sessions/current", {"handle": handle}) == (200, admitted)
                assert await post("/sessions/end", {"handle": handle, "generation": 0}) == (
                    200,
                    {"sessionId": sid, "generation": 1},
                )
                assert await post("/sessions/current", {"handle": handle}) == (
                    200,
                    {
                        "handle": handle,
                        "csrfToken": admitted["csrfToken"],
                        "ended": True,
                        "generation": 1,
                    },
                )
                ended = durable.repository.load_session(
                    digest(handle), identity.now, allow_ended=True
                )
                assert ended.ended_at == identity.now and ended.generation == 1
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=5)

    asyncio.run(exercise())


@pytest.fixture(scope="module")
def compiled_private_account_entry(tmp_path_factory):
    node, pnpm = shutil.which("node"), shutil.which("pnpm")
    assert node and pnpm, "Qualified pinned Node/pnpm are required by this local integration"
    directory = tmp_path_factory.mktemp("private-browser-node")
    directory.chmod(0o700)
    web = ROOT / "apps/web"
    output = directory / "compiled"
    project = directory / "tsconfig.json"
    project.write_text(
        json.dumps(
            {
                "extends": str(web / "lib/server/tsconfig.json"),
                "compilerOptions": {
                    "typeRoots": [str(ROOT / "node_modules/@types")],
                    "rootDir": str(web),
                    "outDir": str(output),
                    "noEmit": False,
                    "noEmitOnError": True,
                },
                "files": [
                    str(web / "features/account-entry/server.ts"),
                    str(web / "lib/server/private-gateway-signer.ts"),
                    str(web / "app/auth/signin/route.ts"),
                    str(web / "app/auth/callback/route.ts"),
                ],
                "include": [],
            }
        )
    )
    subprocess.run([pnpm, "exec", "tsc", "-p", str(project)], cwd=ROOT, check=True, timeout=30)
    (output / "package.json").write_text('{"type":"module"}')
    (output / "node_modules").symlink_to(web / "node_modules", target_is_directory=True)
    return node, output


def test_typescript_private_account_entry_uses_the_dev_tls_postgres_listener(
    browser_state, durable, identity, tls_fixture, compiled_private_account_entry, tmp_path
):
    now, options, _, stage, *_ = tls_fixture
    region, rest_api_id = "us-east-2", "fixtureapi"
    vpc_endpoint_id = "vpce-01234567"
    gateway_host = "fixtureapi-vpce-01234567.execute-api.us-east-2.amazonaws.com"
    issuer = cert("generated-gateway-test-ca", now, ca=True)
    server = cert(gateway_host, now, purpose=ExtendedKeyUsageOID.SERVER_AUTH, signer=issuer)
    authority, _ = write_pair(tmp_path, "generated-gateway-test-ca", issuer)
    server_certificate, server_key = write_pair(tmp_path, "generated-gateway-test", server)
    approved = replace(
        options["approved"],
        stage_arn=f"arn:aws:apigateway:{region}::/restapis/{rest_api_id}/stages/dev",
        backend_server_name=gateway_host,
    )
    options = {
        **options,
        "approved": approved,
        "readback": replace(options["readback"], binding=approved),
        "server_certfile": server_certificate,
        "server_keyfile": server_key,
    }
    node, output = compiled_private_account_entry
    probe = Path(__file__).with_name("private_browser_client_postgres_probe.mjs")
    sid = f"generated-node-mtls-{uuid4().hex}"

    async def exercise():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
            config = build_private_browser_pilot_listener(
                browser_state,
                path_profile=PrivateBrowserPilotPathProfile.DEV,
                **(options | {"port": port}),
            )
            server = uvicorn.Server(config)
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(4):
                    while not server.started:
                        await asyncio.sleep(0.01)
                request = {
                    "endpoint": f"https://{gateway_host}/dev",
                    "local_port": port,
                    "region": region,
                    "rest_api_id": rest_api_id,
                    "vpc_endpoint_id": vpc_endpoint_id,
                    "certificate_authority": str(authority),
                    "stage_certificate": str(stage[0]),
                    "stage_private_key": str(stage[1]),
                    "now": int(identity.now.timestamp() * 1000),
                    "issuer": identity.config.issuer,
                    "audience": identity.config.audience,
                    "jwks_url": identity.config.jwks_url,
                    "client_id": identity.config.client_id,
                    "jwks": identity.keys,
                    "access_token": identity.token(sid=sid),
                    "subject": durable.context.actor["subject"],
                    "session_id": sid,
                    "mode": durable.mode,
                }
                result = await asyncio.to_thread(
                    subprocess.run,
                    [node, "--conditions=react-server", str(probe), str(output)],
                    cwd=ROOT,
                    input=json.dumps(request).encode(),
                    env={"PATH": os.environ["PATH"], "LANG": "C", "TZ": "UTC"},
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    check=True,
                    timeout=25,
                )
                assert len(result.stdout) < 1024
                assert json.loads(result.stdout) == {
                    "account_entry_over_private_mtls": "PASS",
                    "pinned_signer": "PASS",
                    "provider_acceptance": "NOT_RUN",
                    "mode": durable.mode,
                }
            finally:
                server.should_exit = True
                await asyncio.wait_for(task, timeout=5)

    asyncio.run(exercise())
    with durable.connect() as connection:
        assert connection.execute(
            "SELECT generation, ended_at FROM opentour_identity.sessions "
            "WHERE issuer=%s AND session_id=%s",
            (identity.config.issuer, sid),
        ).fetchone() == {"generation": 1, "ended_at": identity.now}
