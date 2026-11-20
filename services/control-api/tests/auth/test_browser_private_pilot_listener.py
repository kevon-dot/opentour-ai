"""Generated loopback TLS checks for the private browser pilot ingress."""

from __future__ import annotations

import asyncio
import json
import socket
import ssl
import threading
from contextlib import asynccontextmanager
from dataclasses import replace
from http.client import HTTPResponse
from io import BytesIO
from unittest.mock import MagicMock, Mock

import psycopg
import pytest
import uvicorn
from cryptography.fernet import Fernet
from opentour_control.auth import browser_private_pilot_listener as pilot
from opentour_control.auth.browser_private_listener import PrivateListenerUnavailable
from opentour_control.auth.browser_private_pilot import PrivateBrowserPilotPathProfile
from opentour_control.auth.browser_state import BrowserAuthState, BrowserSession, BrowserSessionEnd
from opentour_control.auth.config import AuthError, FernetSealingPort, digest
from opentour_control.auth.repository import PostgresIdentityRepository
from opentour_control.auth.session import SessionService
from test_browser_private_listener import HOST
from test_browser_private_listener import tls_fixture as tls_fixture

CURRENT = "/internal/browser-identity/v1/sessions/current"
DEV_CURRENT = "/dev" + CURRENT
END = "/internal/browser-identity/v1/sessions/end"
HANDLE = "h" * 43
ERROR_FIELDS = {"code", "message", "request_id", "retryable", "field_errors", "current_revision"}
LOOKUP_SQL = (
    "SELECT s.*, b.generation AS binding_generation, b.expires_at AS binding_expiry "
    "FROM opentour_identity.sessions s JOIN opentour_identity.session_bindings b "
    "USING (issuer,session_id) WHERE b.binding_sha256=%s"
)


@asynccontextmanager
async def local_pilot(tls_fixture, browser, **settings):
    _, options, authority, stage, *_ = tls_fixture
    context = ssl.create_default_context(cafile=authority)
    context.load_cert_chain(*stage)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        config = pilot.build_private_browser_pilot_listener(
            browser, **(options | settings | {"port": port})
        )
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


@pytest.fixture
def dev_tls_fixture(tls_fixture):
    now, options, *material = tls_fixture
    approved = replace(
        options["approved"],
        stage_arn=options["approved"].stage_arn.removesuffix("/fixture-stage") + "/dev",
    )
    return (
        now,
        options
        | {
            "approved": approved,
            "readback": replace(options["readback"], binding=approved),
            "path_profile": PrivateBrowserPilotPathProfile.DEV,
        },
        *material,
    )


@pytest.fixture
def browser_with_database_cursor(identity):
    connection = MagicMock(spec=psycopg.Connection)
    connection.__enter__.return_value = connection
    connection.execute.return_value.fetchone.return_value = None
    connect = Mock(return_value=connection)
    repository = PostgresIdentityRepository(connect)
    sessions = SessionService(
        identity.config,
        identity.verifier,
        repository,
        identity.service.authority,
        identity.provider,
        clock=lambda: identity.now,
    )
    sealer = FernetSealingPort((Fernet.generate_key(),), 86_400)
    return BrowserAuthState(sessions, sealer, b"F" * 32), connection, connect


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


def test_tls_selects_only_the_path_profile_for_its_bound_stage(tls_fixture):
    _, options, *_ = tls_fixture
    original = options["approved"]
    stage_base = original.stage_arn.removesuffix("/fixture-stage")
    browser = Mock(spec=BrowserAuthState)
    for stage_arn, profile, host in (
        (original.stage_arn, PrivateBrowserPilotPathProfile.DEV, "127.0.0.1"),
        (f"{stage_base}/dev", PrivateBrowserPilotPathProfile.LOOPBACK, "127.0.0.1"),
        (f"{stage_base}/dev/", PrivateBrowserPilotPathProfile.DEV, "127.0.0.1"),
        (f"{stage_base}/another/stages/dev", PrivateBrowserPilotPathProfile.DEV, "127.0.0.1"),
        (original.stage_arn, PrivateBrowserPilotPathProfile.LOOPBACK, "10.0.0.8"),
    ):
        approved = replace(original, stage_arn=stage_arn)
        readback = replace(options["readback"], binding=approved)
        with pytest.raises(
            PrivateListenerUnavailable, match="^PRIVATE_BROWSER_LISTENER_UNAVAILABLE$"
        ):
            pilot.build_private_browser_pilot_listener(
                browser,
                **(options | {"approved": approved, "readback": readback, "host": host}),
                path_profile=profile,
            )
    assert not browser.mock_calls


def test_dev_tls_ingress_rejects_aliases_on_the_landed_request(dev_tls_fixture):
    browser = Mock(spec=BrowserAuthState)
    checkpoint = Mock()

    async def exercise():
        async with local_pilot(dev_tls_fixture, browser, readiness_checkpoint=checkpoint) as (
            port,
            ctx,
        ):
            for path in (
                CURRENT,
                "/prod" + CURRENT,
                "/%64ev" + CURRENT,
                "/dev/dev" + CURRENT,
                "/dev" + CURRENT + "/",
                "/dev" + CURRENT + "?probe=generated",
            ):
                assert_refusal(
                    await exchange(port, ctx, request(path, b"{}")),
                    404,
                    "PRIVATE_OPERATION_NOT_FOUND",
                )
            assert_refusal(
                await exchange(port, ctx, request(DEV_CURRENT, b"{}", content_type="text/plain")),
                415,
                "PRIVATE_UNSUPPORTED_MEDIA",
            )
        assert not browser.mock_calls
        checkpoint.assert_not_called()

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


@pytest.mark.parametrize(
    ("body", "status", "code", "expected", "database_called"),
    [
        (b"{}", 422, "SCHEMA_INVALID", (False, False), False),
        (b'{"handle":"generated-too-short"}', 401, "SESSION_INVALID", (False, True), False),
        (json.dumps({"handle": HANDLE}).encode(), 401, "SESSION_INVALID", (True, True), True),
        (
            json.dumps({"handle": HANDLE}).encode(),
            503,
            "IDENTITY_STATE_UNAVAILABLE",
            (False, False),
            True,
        ),
    ],
    ids=["closed-schema", "rejected-handle", "empty-query", "database-error"],
)
def test_dev_tls_checkpoint_reports_only_actual_lookup_and_canonical_response(
    dev_tls_fixture, browser_with_database_cursor, body, status, code, expected, database_called
):
    browser, connection, connect = browser_with_database_cursor
    checkpoint = Mock()
    if status == 503:
        connection.execute.return_value.fetchone.side_effect = psycopg.OperationalError(
            "generated-database-error-must-not-escape"
        )

    async def exercise():
        async with local_pilot(dev_tls_fixture, browser, readiness_checkpoint=checkpoint) as (
            port,
            ctx,
        ):
            assert_refusal(
                await exchange(port, ctx, request("/dev" + END, b"{}")), 422, "SCHEMA_INVALID"
            )
            checkpoint.assert_not_called()
            assert_refusal(await exchange(port, ctx, request(DEV_CURRENT, body)), status, code)
        checkpoint.assert_called_once_with(*expected)
        if database_called:
            connect.assert_called_once_with()
            connection.execute.assert_called_once_with(LOOKUP_SQL, (digest(HANDLE),))
            connection.execute.return_value.fetchone.assert_called_once_with()
        else:
            connect.assert_not_called()

    asyncio.run(exercise())


def test_loopback_current_ignores_an_explicit_checkpoint(tls_fixture):
    browser = Mock(spec=BrowserAuthState)
    browser.current.side_effect = AuthError("SESSION_INVALID")
    checkpoint = Mock()

    async def exercise():
        async with local_pilot(tls_fixture, browser, readiness_checkpoint=checkpoint) as (
            port,
            ctx,
        ):
            raw = request(CURRENT, json.dumps({"handle": HANDLE}).encode())
            assert_refusal(await exchange(port, ctx, raw), 401, "SESSION_INVALID")
        browser.current.assert_called_once_with(HANDLE)
        checkpoint.assert_not_called()

    asyncio.run(exercise())


@pytest.mark.parametrize("outcome", ["complete", "body-send-failed", "readback-expired"])
def test_dev_checkpoint_waits_for_both_asgi_sends(
    dev_tls_fixture, browser_with_database_cursor, outcome
):
    _, options, *_ = dev_tls_fixture
    browser, _, _ = browser_with_database_cursor
    checkpoint = Mock()
    clock = Mock(side_effect=options["clock"])
    config = pilot.build_private_browser_pilot_listener(
        browser, **(options | {"clock": clock}), readiness_checkpoint=checkpoint
    )
    scope = {
        "type": "http",
        "scheme": "https",
        "method": "POST",
        "path": DEV_CURRENT,
        "raw_path": DEV_CURRENT.encode(),
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": json.dumps({"handle": HANDLE}).encode()}

    async def send(message):
        checkpoint.assert_not_called()
        messages.append(message)
        if message["type"] == "http.response.body":
            if outcome == "body-send-failed":
                raise ConnectionError("generated send failure")
            if outcome == "readback-expired":
                clock.side_effect = None
                clock.return_value = options["readback"].valid_until

    async def exercise():
        if outcome == "body-send-failed":
            with pytest.raises(ConnectionError, match="generated send failure"):
                await config.app(scope, receive, send)
        else:
            await config.app(scope, receive, send)
        if outcome == "complete":
            checkpoint.assert_called_once_with(True, True)
        else:
            checkpoint.assert_not_called()
        assert [message["type"] for message in messages] == [
            "http.response.start",
            "http.response.body",
        ]
        assert messages[0]["status"] == 401
        assert (b"cache-control", b"no-store") in messages[0]["headers"]
        assert_refusal((401, json.loads(messages[1]["body"])), 401, "SESSION_INVALID")

    asyncio.run(exercise())


def test_dev_checkpoint_reports_unconfirmed_once_when_database_worker_returns_late(
    dev_tls_fixture, browser_with_database_cursor, monkeypatch
):
    browser, connection, _ = browser_with_database_cursor
    entered = threading.Event()
    release = threading.Event()
    returned = threading.Event()
    checkpoint = Mock()
    monkeypatch.setattr(pilot, "DISPATCH_WAIT_SECONDS", 0.25)

    def delayed_empty_lookup():
        entered.set()
        assert release.wait(timeout=3)
        returned.set()
        return None

    connection.execute.return_value.fetchone.side_effect = delayed_empty_lookup

    async def exercise():
        async with local_pilot(dev_tls_fixture, browser, readiness_checkpoint=checkpoint) as (
            port,
            ctx,
        ):
            try:
                raw = request(DEV_CURRENT, json.dumps({"handle": HANDLE}).encode())
                assert_refusal(await exchange(port, ctx, raw), 503, "IDENTITY_STATE_UNAVAILABLE")
                assert entered.is_set() and not returned.is_set()
                checkpoint.assert_called_once_with(False, False)
                assert_refusal(await exchange(port, ctx, raw), 503, "IDENTITY_STATE_UNAVAILABLE")
                connection.execute.assert_called_once_with(LOOKUP_SQL, (digest(HANDLE),))
            finally:
                release.set()

    asyncio.run(exercise())
    assert returned.is_set()
    checkpoint.assert_called_once_with(False, False)


def test_dev_checkpoint_fault_preserves_response_and_does_not_rearm(
    dev_tls_fixture, browser_with_database_cursor
):
    browser, connection, _ = browser_with_database_cursor
    checkpoint = Mock(side_effect=RuntimeError("generated checkpoint failure"))

    async def exercise():
        async with local_pilot(dev_tls_fixture, browser, readiness_checkpoint=checkpoint) as (
            port,
            ctx,
        ):
            raw = request(DEV_CURRENT, json.dumps({"handle": HANDLE}).encode())
            for _ in range(2):
                assert_refusal(await exchange(port, ctx, raw), 401, "SESSION_INVALID")
        checkpoint.assert_called_once_with(True, True)
        assert connection.execute.call_count == 2

    asyncio.run(exercise())
