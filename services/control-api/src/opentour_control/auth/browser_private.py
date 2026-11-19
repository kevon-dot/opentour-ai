"""Closed private browser-port dispatch. The listener owns workload authentication."""

from __future__ import annotations

import json
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from .browser_state import (
    BrowserAuthState,
    BrowserAuthTransaction,
    BrowserCredentials,
    BrowserSession,
)
from .config import AuthError, counter, utc

# Local dispatcher bounds. A listener must enforce its reviewed limits while reading the body.
MAX_REQUEST_BYTES = 65_536
MAX_RESPONSE_BYTES = 131_072
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_FIELDS = {
    "create_flow": {"flow"},
    "consume_flow": {"state", "browser"},
    "admit": {"credentials"},
    "current": {"handle"},
    "end": {"handle", "generation"},
}
_BINDING_FIELDS = {
    "sessionHandle",
    "subject",
    "sessionId",
    "generation",
    "selectionTarget",
    "selectionIntent",
}
_FLOW_FIELDS = {
    "state",
    "nonce",
    "browserDigest",
    "verifier",
    "expiresAt",
    "returnPath",
    "kind",
} | _BINDING_FIELDS
_CREDENTIAL_FIELDS = {"accessToken", "refreshToken", "userId"}
_DOMAIN_ERRORS = {
    401: {"CALLBACK_INVALID", "SESSION_INVALID", "SESSION_ENDED"},
    409: {"STALE_SESSION_CONTEXT", "SESSION_GENERATION_EXHAUSTED"},
    422: {"SCHEMA_INVALID", "SESSION_SEAL_INVALID"},
    503: {
        "IDENTITY_CLOCK_UNAVAILABLE",
        "IDENTITY_STATE_UNAVAILABLE",
        "IDENTITY_VERIFICATION_UNAVAILABLE",
        "SESSION_ADMISSION_UNCONFIRMED",
        "SESSION_CREDENTIALS_UNAVAILABLE",
        "SESSION_END_UNCONFIRMED",
    },
}


@dataclass(frozen=True)
class AuthenticatedBrowserCaller:
    """Request-scoped proof supplied by the trusted listener, never deserialized from HTTP."""

    principal: str
    audience: str
    operation: str
    expires_at: datetime


@dataclass(frozen=True)
class PrivateBrowserResponse:
    status: int
    body: bytes = field(repr=False)

    @property
    def headers(self) -> dict[str, str]:
        return {"Content-Type": "application/json", "Cache-Control": "no-store"}


def _closed(value: object, fields: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict) or value.keys() != fields:
        raise ValueError
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise ValueError
    return value


def _invalid_constant(_: str) -> Any:
    raise ValueError


def _credentials(value: object) -> BrowserCredentials:
    data = _closed(value, _CREDENTIAL_FIELDS)
    return BrowserCredentials(
        access_token=data["accessToken"],
        refresh_token=data["refreshToken"],
        user_id=data["userId"],
    )


def _flow(value: object) -> BrowserAuthTransaction:
    data = _closed(value, _FLOW_FIELDS)
    if data["kind"] != "signin" or any(data[key] is not None for key in _BINDING_FIELDS):
        raise ValueError
    expires = _EPOCH + timedelta(milliseconds=counter(data["expiresAt"]))
    return BrowserAuthTransaction(
        state=data["state"],
        nonce=data["nonce"],
        browser_digest=data["browserDigest"],
        verifier=data["verifier"],
        expires_at=expires,
        return_path=data["returnPath"],
        kind="signin",
        session_handle=None,
        subject=None,
        session_id=None,
        generation=None,
        selection_target=None,
    )


def _flow_wire(flow: BrowserAuthTransaction) -> dict[str, object]:
    if flow.kind != "signin":
        raise AuthError("CALLBACK_INVALID")
    elapsed = flow.expires_at - _EPOCH
    return {
        "state": flow.state,
        "nonce": flow.nonce,
        "browserDigest": flow.browser_digest,
        "verifier": flow.verifier,
        "expiresAt": elapsed // timedelta(milliseconds=1),
        "returnPath": flow.return_path,
        "kind": "signin",
        **dict.fromkeys(_BINDING_FIELDS),
    }


def _session_wire(session: BrowserSession) -> dict[str, object]:
    binding: dict[str, object] = {"handle": session.handle, "csrfToken": session.csrf_token}
    if session.ended:
        return {**binding, "ended": True, "generation": session.generation}
    identity, credentials = session.identity, session.credentials
    if identity is None or credentials is None:
        raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503)
    return {
        **binding,
        "ended": False,
        "reconciliationRequired": session.reconciliation_required,
        "identity": identity.wire(),
        "credentials": {
            "accessToken": credentials.access_token,
            "refreshToken": credentials.refresh_token,
            "userId": credentials.user_id,
        },
    }


def _response(status: int, value: object) -> PrivateBrowserResponse:
    return PrivateBrowserResponse(
        status, json.dumps(value, separators=(",", ":"), allow_nan=False).encode("utf-8")
    )


def _failure(error: AuthError) -> PrivateBrowserResponse:
    # Transport errors never instruct the caller to retry a mutation after an unknown result.
    safe = AuthError(error.code, error.status)
    return _response(safe.status, safe.wire(secrets.token_hex(16)))


def _unconfirmed(operation: str) -> PrivateBrowserResponse:
    code = {
        "admit": "SESSION_ADMISSION_UNCONFIRMED",
        "end": "SESSION_END_UNCONFIRMED",
    }.get(operation, "IDENTITY_STATE_UNAVAILABLE")
    return _failure(AuthError(code, 503))


class PrivateBrowserDispatch:
    """Dispatch five existing durable ports without creating an HTTP route or a trust source."""

    def __init__(
        self,
        browser: BrowserAuthState,
        *,
        principal: str,
        audience: str,
        clock: Callable[[], datetime],
    ) -> None:
        for identity in (principal, audience):
            if not isinstance(identity, str) or not identity or identity != identity.strip():
                raise ValueError("An explicit workload binding is required")
        self._browser = browser
        self._principal = principal
        self._audience = audience
        self._clock = clock

    def dispatch(
        self, operation: str, body: bytes, *, caller: AuthenticatedBrowserCaller | None
    ) -> PrivateBrowserResponse:
        try:
            if not self._matches(caller, operation):
                return _failure(AuthError("PRIVATE_CALLER_DENIED", 403))
        except Exception:
            return _failure(AuthError("IDENTITY_CLOCK_UNAVAILABLE", 503))
        fields = _FIELDS.get(operation)
        if fields is None:
            return _failure(AuthError("PRIVATE_OPERATION_NOT_FOUND", 404))
        if not isinstance(body, bytes):
            return _failure(AuthError("SCHEMA_INVALID", 422))
        if len(body) > MAX_REQUEST_BYTES:
            return _failure(AuthError("PRIVATE_BODY_TOO_LARGE", 413))
        try:
            data = _closed(
                json.loads(
                    body.decode("utf-8"),
                    object_pairs_hook=_unique,
                    parse_constant=_invalid_constant,
                ),
                fields,
            )
            # These codecs validate before dispatch. The domain still rechecks expiry and binding.
            if operation == "create_flow":
                flow = _flow(data["flow"])
            if operation == "admit":
                credentials = _credentials(data["credentials"])
        except AuthError as error:
            return _failure(error)
        except (ValueError, OverflowError, RecursionError):
            return _failure(AuthError("SCHEMA_INVALID", 422))
        try:
            if operation == "create_flow":
                self._browser.create_flow(flow)
                result: object = None
            elif operation == "consume_flow":
                result = _flow_wire(
                    self._browser.consume_flow(data["state"], data["browser"], signin_only=True)
                )
            elif operation == "admit":
                result = _session_wire(self._browser.admit(credentials))
            elif operation == "current":
                result = _session_wire(self._browser.current(data["handle"]))
            else:
                receipt = self._browser.end(data["handle"], data["generation"])
                result = {"sessionId": receipt.session_id, "generation": receipt.generation}
            response = _response(200, result)
            if len(response.body) <= MAX_RESPONSE_BYTES:
                return response
        except AuthError as error:
            if error.code in _DOMAIN_ERRORS.get(error.status, set()):
                return _failure(error)
        except Exception:
            # The durable mutation may already have taken effect. Do not infer or retry.
            pass
        return _unconfirmed(operation)

    def _matches(self, caller: AuthenticatedBrowserCaller | None, operation: str) -> bool:
        return (
            type(caller) is AuthenticatedBrowserCaller
            and type(operation) is str
            and caller.principal == self._principal
            and caller.audience == self._audience
            and caller.operation == operation
            and isinstance(caller.expires_at, datetime)
            and caller.expires_at.utcoffset() is not None
            and caller.expires_at > utc(self._clock())
        )
