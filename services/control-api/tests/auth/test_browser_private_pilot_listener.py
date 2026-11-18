"""Generated loopback TLS checks for the private browser pilot ingress."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
from contextlib import asynccontextmanager
from http.client import HTTPResponse
from io import BytesIO
from unittest.mock import Mock

import uvicorn
from opentour_control.auth import browser_private_pilot_listener as pilot
from opentour_control.auth.browser_state import BrowserAuthState, BrowserSession, BrowserSessionEnd
from test_browser_private_listener import HOST
from test_browser_private_listener import tls_fixture as tls_fixture

CURRENT = "/internal/browser-identity/v1/sessions/current"
END = "/internal/browser-identity/v1/sessions/end"
HANDLE = "h" * 43
ERROR_FIELDS = {"code", "message", "request_id", "retryable", "field_errors", "current_revision"}


@asynccontextmanager
async def local_pilot(tls_fixture, browser):
    _, options, authority, stage, *_ = tls_fixture
    context = ssl.create_default_context(cafile=authority)
    context.load_cert_chain(*stage)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = pilot.build_private_browser_pilot_listener(browser, **(options | {"port": port}))
        server = uvicorn.Server(config)
        task = asyncio.create_task(server.serve(sockets=[sock]))
        try:
            async with asyncio.timeout(3):
                while not server.started:
                    await asyncio.sleep(0.01)
            yield port, context
        finally:
            server.should_exit = True
            await asyncio.wait_for(task, timeout=3)


def request(
    path: str,
    body: bytes,
    *,
    method: str = "POST",
    content_type: str = "application/json",
    content_encoding: str | None = None,
    framing: str | None = None,
) -> bytes:
    headers = [
        f"{method} {path} HTTP/1.1",
        f"Host: {HOST}",
        "Connection: close",
        f"Content-Type: {content_type}",
        framing or f"Content-Length: {len(body)}",
    ]
    if content_encoding is not None:
        headers.append(f"Content-Encoding: {content_encoding}")
    return ("\r\n".join(headers) + "\r\n\r\n").encode("ascii") + body


async def exchange(port: int, context: ssl.SSLContext, raw: bytes):
    async with asyncio.timeout(3):
        reader, writer = await asyncio.open_connection(
            "127.0.0.1", port, ssl=context, server_hostname=HOST
        )
        try:
            writer.write(raw)
            await writer.drain()
            received = bytearray()
            while chunk := await reader.read(8192):
                received.extend(chunk)
                assert len(received) < 136_192
        finally:
            writer.close()
            await writer.wait_closed()

    fixture_socket = Mock(makefile=Mock(return_value=BytesIO(received)))
    with HTTPResponse(fixture_socket) as response:
        response.begin()
        assert response.getheader("Cache-Control") == "no-store"
        assert response.getheader("Content-Type") == "application/json"
        return response.status, json.loads(response.read())


def assert_refusal(result, status, code):
    actual_status, body = result
    assert actual_status == status
    assert set(body) == ERROR_FIELDS
    assert body["code"] == code
    assert isinstance(body["request_id"], str) and body["request_id"]
    assert body["retryable"] is False
    assert body["field_errors"] == []
    assert body["current_revision"] is None


def test_tls_ingress_refuses_raw_aliases_and_disabled_routes(tls_fixture):
    browser = Mock(spec=BrowserAuthState)

    async def exercise():
        async with local_pilot(tls_fixture, browser) as (port, ctx):
            invalid = [
                ("GET", CURRENT),
                ("post", CURRENT),
                ("POST", CURRENT.upper()),
                ("POST", CURRENT + "/"),
                ("POST", CURRENT.replace("/internal/", "/%69nternal/", 1)),
                ("POST", CURRENT + "?probe=generated"),
                ("POST", "/dev" + CURRENT),
                ("POST", "/internal/browser-identity/v1/sessions/refresh"),
                ("POST", "/internal/browser-identity/v1/selection/authorize"),
            ]
            for method, path in invalid:
                result = await exchange(port, ctx, request(path, b"{}", method=method))
                assert_refusal(result, 404, "PRIVATE_OPERATION_NOT_FOUND")
        assert not browser.mock_calls

    asyncio.run(exercise())


def test_tls_ingress_bounds_actual_chunked_and_incomplete_bodies(tls_fixture, monkeypatch):
    browser = Mock(spec=BrowserAuthState)
    monkeypatch.setattr(pilot, "BODY_READ_SECONDS", 0.25)
    chunks = (b" " * 32_768, b" " * 32_768, b" ")
    chunk_wire = b"".join(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n" for chunk in chunks)

    async def exercise():
        async with local_pilot(tls_fixture, browser) as (port, ctx):
            oversized = request(
                CURRENT, chunk_wire + b"0\r\n\r\n", framing="Transfer-Encoding: chunked"
            )
            assert_refusal(await exchange(port, ctx, oversized), 413, "PRIVATE_BODY_TOO_LARGE")
            incomplete = request(CURRENT, b"{", framing="Content-Length: 2")
            assert_refusal(await exchange(port, ctx, incomplete), 408, "PRIVATE_BODY_TIMEOUT")
        assert not browser.mock_calls

    asyncio.run(exercise())


def test_tls_ingress_requires_single_exact_unencoded_json_content_type(tls_fixture):
    browser = Mock(spec=BrowserAuthState)
    body = json.dumps({"handle": HANDLE}).encode()

    async def exercise():
        async with local_pilot(tls_fixture, browser) as (port, ctx):
            correct = request(CURRENT, body)
            invalid = (
                correct.replace(b"Content-Type: application/json\r\n", b""),
                correct.replace(
                    b"Content-Type: application/json",
                    b"Content-Type: application/json\r\nContent-Type: application/json",
                ),
                request(CURRENT, body, content_type="application/json; charset=utf-8"),
                request(CURRENT, body, content_encoding="identity"),
                request(CURRENT, body, content_encoding="gzip"),
            )
            for raw in invalid:
                assert_refusal(await exchange(port, ctx, raw), 415, "PRIVATE_UNSUPPORTED_MEDIA")
        assert not browser.mock_calls

    asyncio.run(exercise())


def test_tls_dispatch_timeout_keeps_worker_admission_closed_until_return(tls_fixture, monkeypatch):
    browser = Mock(spec=BrowserAuthState)
    release = threading.Event()
    entered = threading.Event()
    monkeypatch.setattr(pilot, "DISPATCH_WAIT_SECONDS", 0.25)

    def end(_handle, _generation):
        entered.set()
        assert release.wait(timeout=3)
        return BrowserSessionEnd("generated-session", 1)

    browser.end.side_effect = end
    browser.current.return_value = BrowserSession(
        handle=HANDLE,
        csrf_token="generated-csrf",
        session_id="generated-session",
        generation=1,
        ended=True,
        reconciliation_required=False,
        identity=None,
        credentials=None,
    )
    end_request = request(END, json.dumps({"handle": HANDLE, "generation": 0}).encode())
    read_request = request(CURRENT, json.dumps({"handle": HANDLE}).encode())

    async def exercise():
        async with local_pilot(tls_fixture, browser) as (port, ctx):
            try:
                result = await exchange(port, ctx, end_request)
                assert entered.is_set() and not release.is_set()
                assert_refusal(result, 503, "SESSION_END_UNCONFIRMED")
                assert_refusal(
                    await exchange(port, ctx, read_request), 503, "IDENTITY_STATE_UNAVAILABLE"
                )
                browser.end.assert_called_once_with(HANDLE, 0)
                browser.current.assert_not_called()
            finally:
                release.set()

            async with asyncio.timeout(2):
                while True:
                    status, body = await exchange(port, ctx, read_request)
                    if status == 200:
                        break
                    assert_refusal((status, body), 503, "IDENTITY_STATE_UNAVAILABLE")
                    await asyncio.sleep(0.01)
            assert body == {
                "handle": HANDLE,
                "csrfToken": "generated-csrf",
                "ended": True,
                "generation": 1,
            }
        browser.end.assert_called_once_with(HANDLE, 0)
        browser.current.assert_called_once_with(HANDLE)

    asyncio.run(exercise())
