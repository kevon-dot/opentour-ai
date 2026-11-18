"""Finite subprocess fixture for the real browser-state service over private test pipes."""

import hmac
import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import Mock

from opentour_control.auth.browser_state import (
    BrowserAuthState,
    BrowserAuthTransaction,
    BrowserCredentials,
    BrowserSelectionIntent,
    BrowserSelectionTarget,
    BrowserSession,
)
from opentour_control.auth.config import AuthError, FernetSealingPort, IdentityConfig
from opentour_control.auth.repository import PostgresIdentityRepository, connection_factory
from opentour_control.auth.session import SessionService
from opentour_control.auth.workos_provider import WorkOSTokenVerifier

_MAX_REQUESTS = 64
_MAX_INPUT_FRAME = 65_536
_MAX_OUTPUT_FRAME = 131_072


def _clock_time(epoch_ms):
    return datetime.fromtimestamp(epoch_ms / 1000, UTC)


def _fixture_dsn(value: object) -> str:
    if not isinstance(value, str):
        raise AuthError("TEST_FIXTURE_INVALID", 422)
    temporary_root = re.escape(str(Path("/tmp").resolve()))
    match = re.fullmatch(
        rf"host=({temporary_root}/opentour-c03-[a-z0-9_]{{8}}/socket) "
        r"port=5432 dbname=postgres user=c03_runtime",
        value,
    )
    if match is None:
        raise AuthError("TEST_FIXTURE_INVALID", 422)
    socket = Path(match[1])
    marker = socket.parent / ".owned-c03-fixture"
    if (
        not socket.is_dir()
        or socket.resolve() != socket
        or not marker.is_file()
        or marker.is_symlink()
        or marker.read_text() != socket.parent.name
    ):
        raise AuthError("TEST_FIXTURE_INVALID", 422)
    return value


def _credentials(value):
    return BrowserCredentials(
        access_token=value["accessToken"],
        refresh_token=value["refreshToken"],
        user_id=value["userId"],
    )


def _flow(value):
    target = value["selectionTarget"]
    intent = value["selectionIntent"]
    return BrowserAuthTransaction(
        state=value["state"],
        nonce=value["nonce"],
        browser_digest=value["browserDigest"],
        verifier=value["verifier"],
        expires_at=_clock_time(value["expiresAt"]),
        return_path=value["returnPath"],
        kind=value["kind"],
        session_handle=value["sessionHandle"],
        subject=value["subject"],
        session_id=value["sessionId"],
        generation=value["generation"],
        selection_target=None
        if target is None
        else BrowserSelectionTarget(
            organization_id=target["organizationId"],
            identity_organization_id=target["identityOrganizationId"],
            identity_issuer=target["identityIssuer"],
            membership_id=target["membershipId"],
            authority_revision=target["authorityRevision"],
        ),
        selection_intent=None
        if intent is None
        else BrowserSelectionIntent(
            organization_id=intent["organizationId"],
            expected_generation=intent["expectedGeneration"],
            idempotency_key=intent["idempotencyKey"],
        ),
    )


def _flow_wire(flow: BrowserAuthTransaction):
    target = flow.selection_target
    intent = flow.selection_intent
    return {
        "state": flow.state,
        "nonce": flow.nonce,
        "browserDigest": flow.browser_digest,
        "verifier": flow.verifier,
        "expiresAt": int(flow.expires_at.timestamp() * 1000),
        "returnPath": flow.return_path,
        "kind": flow.kind,
        "sessionHandle": flow.session_handle,
        "subject": flow.subject,
        "sessionId": flow.session_id,
        "generation": flow.generation,
        "selectionTarget": None
        if target is None
        else {
            "organizationId": target.organization_id,
            "identityOrganizationId": target.identity_organization_id,
            "identityIssuer": target.identity_issuer,
            "membershipId": target.membership_id,
            "authorityRevision": target.authority_revision,
        },
        "selectionIntent": None
        if intent is None
        else {
            "organizationId": intent.organization_id,
            "expectedGeneration": intent.expected_generation,
            "idempotencyKey": intent.idempotency_key,
        },
    }


def _session_wire(session: BrowserSession):
    binding = {"handle": session.handle, "csrfToken": session.csrf_token}
    if session.ended:
        return {**binding, "ended": True, "generation": session.generation}
    assert session.identity is not None and session.credentials is not None
    view = {
        **binding,
        "ended": False,
        "reconciliationRequired": session.reconciliation_required,
        "identity": session.identity.wire(),
        "credentials": {
            "accessToken": session.credentials.access_token,
            "refreshToken": session.credentials.refresh_token,
            "userId": session.credentials.user_id,
        },
    }
    if session.credential_digest is not None:
        view["credentialDigest"] = session.credential_digest
    return view


def _dispatch(browser: BrowserAuthState, command):
    action = command["action"]
    if action == "create_flow":
        browser.create_flow(_flow(command["flow"]))
        return None
    if action == "consume_flow":
        return _flow_wire(browser.consume_flow(command["state"], command["browser"]))
    if action in {"admit", "admit_unknown"}:
        session = browser.admit(_credentials(command["credentials"]))
        if action == "admit_unknown":
            raise AuthError("SESSION_ADMISSION_UNCONFIRMED", 503)
        return _session_wire(session)
    if action == "current":
        return _session_wire(browser.current(command["handle"]))
    if action == "replace":
        return browser.replace_credentials(
            command["handle"], command["generation"], _credentials(command["credentials"])
        )
    if action == "selection_digest":
        _, _, digest = browser.selection_evidence(command["accessToken"], command["handle"])
        return digest
    if action in {"end", "end_unknown"}:
        ended = browser.end(command["handle"], command["generation"])
        if action == "end_unknown":
            raise AuthError("SESSION_END_UNCONFIRMED", 503)
        return {"sessionId": ended.session_id, "generation": ended.generation}
    if action == "reconcile":
        browser.require_reconciliation(command["handle"], command["generation"])
        return None
    if action == "inspect":
        with browser.repository.connect() as connection:
            return connection.execute(
                "SELECT count(*) AS count, bool_and(sealed_payload NOT LIKE %s) AS sealed "
                "FROM opentour_identity.session_credentials",
                ("%SENTINEL%",),
            ).fetchone()
    raise AuthError("TEST_OPERATION_INVALID", 422)


def _write_frame(value):
    frame = json.dumps(value).encode("utf-8") + b"\n"
    if len(frame) > _MAX_OUTPUT_FRAME:
        raise AuthError("TEST_OUTPUT_LIMIT", 503)
    sys.stdout.buffer.write(frame)
    sys.stdout.buffer.flush()


def _serve(browser: BrowserAuthState):
    for request in range(_MAX_REQUESTS + 1):
        raw = sys.stdin.buffer.readline(_MAX_INPUT_FRAME + 1)
        if not raw:
            return
        if request == _MAX_REQUESTS or len(raw) > _MAX_INPUT_FRAME or not raw.endswith(b"\n"):
            raise AuthError("TEST_INPUT_LIMIT", 422)
        try:
            value = _dispatch(browser, json.loads(raw))
        except AuthError as error:
            _write_frame({"error": {"code": error.code, "status": error.status}})
        else:
            _write_frame({"value": value})


def _run(config, jwks):
    def clock():
        return _clock_time(config["now"])

    profile = IdentityConfig(
        config["issuer"],
        "https://api.example.invalid",
        "https://api.workos.com/sso/jwks/client_fixture",
        "client_fixture",
        86_400,
    )
    repository = PostgresIdentityRepository(connection_factory(_fixture_dsn(config["dsn"])))
    key = config["key"].encode()
    sealer = FernetSealingPort((key,), profile.credential_max_lifetime_seconds)
    csrf_key = hmac.digest(key, b"generated-fixture-browser-csrf", "sha256")
    unused = Mock(
        side_effect=AssertionError("The private browser probe does not resolve membership")
    )
    authority = Mock(current=unused)
    provider = Mock(current_membership=unused)
    verifier = WorkOSTokenVerifier(profile, fetch=lambda: jwks, clock=clock)
    try:
        service = SessionService(profile, verifier, repository, authority, provider, clock=clock)
        _serve(BrowserAuthState(service, sealer, csrf_key))
    finally:
        if not verifier.close():
            raise AuthError("TEST_PORT_FAILED", 503)


if __name__ == "__main__":
    # libpq supports ambient overrides including PGHOSTADDR and PGSERVICE.
    for name in tuple(os.environ):
        if name.startswith("PG"):
            del os.environ[name]
    try:
        fixture_path = Path(sys.argv[1])
        config = json.loads(fixture_path.read_text())
        jwks = json.loads(fixture_path.with_name("generated-jwks.json").read_text())
        _run(config, jwks)
    except AuthError as error:
        _write_frame({"error": {"code": error.code, "status": error.status}})
        sys.exit(1)
    except Exception:
        _write_frame({"error": {"code": "TEST_PORT_FAILED", "status": 503}})
        sys.exit(1)
