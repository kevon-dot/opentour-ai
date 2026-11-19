"""ASGI contract checks using generated authority evidence and recorded port calls."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, create_autospec

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentour_control.auth.config import AuthError, timestamp
from opentour_control.auth.session import VerifiedIdentityContext
from opentour_control.auth.workos_provider import VerifiedToken
from opentour_control.tenants.session_selection import (
    PostgresSessionSelection,
    SelectionReceipt,
    SelectionTarget,
    SessionSelectionRequest,
)
from opentour_control.tenants.session_selection_route import create_session_selection_router

ROUTE = "/v1/session:select-organization"
TOKEN = "generated-provider-bearer"
BINDING = "binding_" + "a" * 32
KEY = "generated-selection-key"
ISSUER = "https://identity.example.invalid"
BODY = {"organization_id": "local_org", "expected_session_generation": 0}
HEADERS = {
    "authorization": f"Bearer {TOKEN}",
    "cookie": f"opentour-session={BINDING}",
    "idempotency-key": KEY,
}
ERROR_FIELDS = {"code", "message", "request_id", "retryable", "field_errors", "current_revision"}


@pytest.fixture
def route():
    now = datetime(2026, 11, 18, tzinfo=UTC)
    subject = "generated_subject"
    session_id = "generated_session"
    identity = VerifiedIdentityContext(
        {"kind": "human", "issuer": ISSUER, "subject": subject},
        session_id,
        0,
        timestamp(now),
        timestamp(now),
    )
    claims = VerifiedToken(
        ISSUER, subject, session_id, "provider_org", now, now + timedelta(minutes=5)
    )
    sealed_digest = "a" * 64
    evidence = Mock(return_value=(identity, claims, sealed_digest))
    selections = create_autospec(PostgresSessionSelection, instance=True)
    target = SelectionTarget("local_org", "provider_org", ISSUER, "member", "authority_rev")
    receipt = SelectionReceipt("local_org", "member", 1, "generated_receipt")
    selections.admit.return_value = target
    selections.commit.return_value = receipt
    app = FastAPI()
    app.include_router(
        create_session_selection_router(
            SimpleNamespace(selection_evidence=evidence),
            selections,
        )
    )
    return SimpleNamespace(
        client=TestClient(app),
        evidence=evidence,
        selections=selections,
        identity=identity,
        claims=claims,
        sealed_digest=sealed_digest,
        target=target,
        receipt=receipt,
    )


def test_selection_uses_verified_cookie_bearer_and_sealed_record(route):
    response = route.client.post(ROUTE, headers=HEADERS, json=BODY)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Authorization, Cookie"
    assert response.json() == route.receipt.wire()
    route.evidence.assert_called_once_with(TOKEN, BINDING)
    intent = SessionSelectionRequest.parse(BODY)
    route.selections.admit.assert_called_once_with(route.identity, BINDING, intent, KEY)
    route.selections.commit.assert_called_once()
    args = route.selections.commit.call_args.args
    assert args[:-1] == (
        route.identity,
        BINDING,
        intent,
        KEY,
        route.target,
        route.claims,
        route.sealed_digest,
    )
    assert len(args[-1]) == 32


def test_exact_historical_replay_rechecks_identity_without_committing(route):
    route.identity = replace(route.identity, session_generation=3)
    route.evidence.return_value = (route.identity, route.claims, route.sealed_digest)
    route.selections.admit.return_value = route.receipt

    response = route.client.post(ROUTE, headers=HEADERS, json=BODY)

    assert response.status_code == 200
    assert response.json() == route.receipt.wire()
    assert [call.args for call in route.evidence.call_args_list] == [(TOKEN, BINDING)] * 2
    route.selections.commit.assert_not_called()


@pytest.mark.parametrize("fault", ["ended", "generation", "other_binding"])
def test_replay_cannot_outlive_current_authenticated_binding(route, fault):
    original = route.evidence.return_value
    if fault == "ended":
        refreshed = AuthError("SESSION_INVALID")
    elif fault == "generation":
        refreshed = (replace(route.identity, session_generation=1), *original[1:])
    else:
        refreshed = (replace(route.identity, session_id="foreign_session"), *original[1:])
    route.evidence.side_effect = [original, refreshed]
    route.selections.admit.return_value = route.receipt

    response = route.client.post(ROUTE, headers=HEADERS, json=BODY)
    expected = (409, "STALE_SESSION_CONTEXT") if fault == "generation" else (401, "SESSION_INVALID")
    assert (response.status_code, response.json()["code"]) == expected
    route.selections.commit.assert_not_called()


@pytest.mark.parametrize(
    "raw",
    [
        b'{"organization_id":"local_org"}',
        b'{"organization_id":"local_org","expected_session_generation":true}',
        b'{"organization_id":"local_org","expected_session_generation":"0"}',
        b'{"organization_id":"local_org","expected_session_generation":9007199254740992}',
        b'{"organization_id":"local_org","expected_session_generation":0,"scope":"foreign"}',
        b'{"organization_id":"local_org","organization_id":"other","expected_session_generation":0}',
        rb'{"organization_id":"\u0000","expected_session_generation":0}',
        rb'{"organization_id":"\ud800","expected_session_generation":0}',
        b'{"organization_id":',
        b" " * 16_385,
    ],
)
def test_rejects_noncanonical_body_before_any_port_call(route, raw):
    response = route.client.post(ROUTE, headers=HEADERS, content=raw)
    assert response.status_code == 422
    assert response.json()["code"] == "SCHEMA_INVALID"
    assert set(response.json()) == ERROR_FIELDS
    route.evidence.assert_not_called()
    route.selections.admit.assert_not_called()


@pytest.mark.parametrize("header", ["authorization", "cookie", "idempotency-key"])
@pytest.mark.parametrize("duplicate", [False, True])
def test_refuses_missing_or_ambiguous_credentials_and_key(route, header, duplicate):
    values = list(HEADERS.items())
    if duplicate:
        values.append((header, HEADERS[header]))
    else:
        values = [item for item in values if item[0] != header]
    response = route.client.post(ROUTE, headers=values, json=BODY)
    expected = (422, "SCHEMA_INVALID") if header == "idempotency-key" else (401, "SESSION_INVALID")
    assert (response.status_code, response.json()["code"]) == expected
    route.evidence.assert_not_called()
    route.selections.admit.assert_not_called()


@pytest.mark.parametrize("route_path, key", [(ROUTE + "?actor=foreign", KEY), (ROUTE, "bad key")])
def test_refuses_query_intent_or_invalid_key(route, route_path, key):
    response = route.client.post(route_path, headers={**HEADERS, "idempotency-key": key}, json=BODY)
    assert response.status_code == 422
    assert response.json()["code"] == "SCHEMA_INVALID"
    route.evidence.assert_not_called()


@pytest.mark.parametrize(
    "status, code",
    [
        (403, "MEMBERSHIP_INACTIVE"),
        (404, "RESOURCE_NOT_FOUND"),
        (409, "STALE_SESSION_CONTEXT"),
        (409, "CONTEXT_RECONCILIATION_REQUIRED"),
        (503, "IDENTITY_FRESHNESS_UNAVAILABLE"),
    ],
)
def test_returns_canonical_safe_refusals(route, status, code):
    route.selections.admit.side_effect = AuthError(code, status, status == 503)
    response = route.client.post(ROUTE, headers=HEADERS, json=BODY)
    body = response.json()
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"
    assert body["code"] == code
    assert body["current_revision"] is None
    assert body["retryable"] == (status == 503)
    assert set(body) == ERROR_FIELDS
    route.selections.commit.assert_not_called()


def test_unconfirmed_database_result_does_not_disclose_details_or_succeed(route):
    route.selections.commit.side_effect = psycopg.OperationalError("generated private detail")
    response = route.client.post(ROUTE, headers=HEADERS, json=BODY)
    assert response.status_code == 503
    assert response.json()["code"] == "IDENTITY_STATE_UNAVAILABLE"
    assert response.json()["retryable"] is True
    assert "generated private detail" not in response.text
