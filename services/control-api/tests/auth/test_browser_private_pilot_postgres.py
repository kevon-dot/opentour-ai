"""Direct localhost mutual-TLS test with generated keys and disposable PostgreSQL."""

import asyncio
import json
import secrets
import socket
import ssl
from datetime import timedelta
from http.client import HTTPResponse
from uuid import uuid4

import pytest
import uvicorn
from opentour_control.auth.browser_private import MAX_RESPONSE_BYTES
from opentour_control.auth.browser_private_pilot_listener import (
    build_private_browser_pilot_listener,
)
from opentour_control.auth.config import digest
from test_browser_private_listener import HOST
from test_browser_private_listener import tls_fixture as tls_fixture
from test_identity_postgres import browser_state as browser_state
from test_identity_postgres import durable as durable
from test_identity_postgres import identity_postgres_fixture
from test_identity_postgres import mode as mode

pytestmark = pytest.mark.local_integration


@pytest.fixture(scope="module")
def postgres():
    with identity_postgres_fixture() as fixture:
        yield fixture


def test_private_pilot_listener_uses_mutual_tls_and_durable_identity(
    browser_state, durable, identity, tls_fixture
):
    _, options, authority, stage, wrong, _ = tls_fixture
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
    prefix = "/internal/browser-identity/v1"

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
                browser_state, **(options | {"port": port})
            )
            server = uvicorn.Server(config)
            task = asyncio.create_task(server.serve(sockets=[sock]))
            try:
                async with asyncio.timeout(4):
                    while not server.started:
                        await asyncio.sleep(0.01)

                async def post(path, body, pair=stage):
                    return await asyncio.wait_for(
                        asyncio.to_thread(exchange, port, prefix + path, body, pair), timeout=5
                    )

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
