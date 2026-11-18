"""Compose the five private pilot operations on the dedicated stage TLS listener."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import uvicorn
from cryptography import x509
from uvicorn._types import ASGIReceiveCallable, ASGISendCallable, HTTPScope, Scope

from .browser_private import (
    AuthenticatedBrowserCaller,
    PrivateBrowserDispatch,
    PrivateBrowserResponse,
    _failure,
    _unconfirmed,
)
from .browser_private_listener import (
    ReviewedStageReadback,
    StageClientBinding,
    build_private_browser_listener,
)
from .browser_private_pilot import PrivateBrowserPilotRoute, lookup_private_browser_pilot_route
from .browser_state import BrowserAuthState
from .config import AuthError

# These are local pilot bounds. A timed-out call can still commit in the durable writer.
BODY_READ_SECONDS = 5
DISPATCH_WAIT_SECONDS = 15


def _route(scope: HTTPScope) -> PrivateBrowserPilotRoute | None:
    raw_path = scope.get("raw_path")
    if not isinstance(raw_path, bytes) or scope.get("query_string") != b"":
        return None
    try:
        return lookup_private_browser_pilot_route(scope["method"], raw_path.decode("ascii"))
    except UnicodeError:
        return None


async def _read_body(receive: ASGIReceiveCallable, limit: int) -> bytes | None:
    body = bytearray()
    try:
        async with asyncio.timeout(BODY_READ_SECONDS):
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    return None
                if message["type"] != "http.request":
                    raise AuthError("SCHEMA_INVALID", 422)
                chunk = message.get("body", b"")
                if len(chunk) > limit - len(body):
                    raise AuthError("PRIVATE_BODY_TOO_LARGE", 413)
                body.extend(chunk)
                if not message.get("more_body", False):
                    return bytes(body)
    except TimeoutError:
        raise AuthError("PRIVATE_BODY_TIMEOUT", 408) from None


def _supported_media(scope: HTTPScope) -> bool:
    content_types = [value for name, value in scope["headers"] if name == b"content-type"]
    return content_types == [b"application/json"] and not any(
        name == b"content-encoding" for name, _ in scope["headers"]
    )


async def _send(send: ASGISendCallable, response: PrivateBrowserResponse) -> None:
    headers = [(name.lower().encode(), value.encode()) for name, value in response.headers.items()]
    if response.status >= 400:
        headers.append((b"connection", b"close"))
    await send({"type": "http.response.start", "status": response.status, "headers": headers})
    await send({"type": "http.response.body", "body": response.body})


def build_private_browser_pilot_listener(
    browser_state: BrowserAuthState,
    *,
    approved: StageClientBinding,
    readback: ReviewedStageReadback | None,
    stage_leaf_pem: bytes,
    server_certfile: Path,
    server_keyfile: Path,
    host: str,
    port: int,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> uvicorn.Config:
    """Return the dedicated TLS configuration. Never register it with the control app."""
    principal, audience = approved.stage_arn, approved.backend_server_name
    dispatcher = PrivateBrowserDispatch(
        browser_state, principal=principal, audience=audience, clock=clock
    )
    active = False
    worker: asyncio.Task[PrivateBrowserResponse] | None = None

    async def dispatch(
        route: PrivateBrowserPilotRoute, body: bytes, caller: AuthenticatedBrowserCaller
    ) -> PrivateBrowserResponse:
        try:
            response = await asyncio.to_thread(
                dispatcher.dispatch, route.operation, body, caller=caller
            )
            if len(response.body) <= route.max_response_bytes:
                return response
        except Exception:
            pass
        return _unconfirmed(route.operation)

    async def app(scope: Scope, receive: ASGIReceiveCallable, send: ASGISendCallable) -> None:
        nonlocal active, worker
        if scope["type"] != "http":
            return
        route = _route(scope)
        if route is None:
            await _send(send, _failure(AuthError("PRIVATE_OPERATION_NOT_FOUND", 404)))
            return
        if not _supported_media(scope):
            await _send(send, _failure(AuthError("PRIVATE_UNSUPPORTED_MEDIA", 415)))
            return
        if active or (worker is not None and not worker.done()):
            await _send(send, _failure(AuthError("IDENTITY_STATE_UNAVAILABLE", 503)))
            return

        active = True
        try:
            try:
                body = await _read_body(receive, route.max_request_bytes)
            except AuthError as error:
                await _send(send, _failure(error))
                return
            if body is None:
                return
            try:
                now = clock()
                live = now.utcoffset() is not None and verified_at <= now < valid_until
            except Exception:
                live = False
            if not live:
                await _send(send, _failure(AuthError("PRIVATE_CALLER_DENIED", 403)))
                return

            # Only the authenticated, still-live stage listener can invoke this closure.
            # BFF role admission belongs to the upstream IAM and endpoint perimeter.
            proof = AuthenticatedBrowserCaller(principal, audience, route.operation, valid_until)
            worker = asyncio.create_task(dispatch(route, body, proof))
            try:
                async with asyncio.timeout(DISPATCH_WAIT_SECONDS):
                    response = await asyncio.shield(worker)
            except TimeoutError:
                response = _unconfirmed(route.operation)
            await _send(send, response)
        finally:
            active = False

    config = build_private_browser_listener(
        app,
        approved=approved,
        readback=readback,
        stage_leaf_pem=stage_leaf_pem,
        server_certfile=server_certfile,
        server_keyfile=server_keyfile,
        host=host,
        port=port,
        clock=clock,
    )
    assert readback is not None
    verified_at = readback.verified_at
    leaf = x509.load_pem_x509_certificate(stage_leaf_pem)
    valid_until = min(readback.valid_until, leaf.not_valid_after_utc)
    config.workers = 1
    return config
