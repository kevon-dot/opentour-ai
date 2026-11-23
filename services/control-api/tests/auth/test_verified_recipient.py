"""Generated WorkOS SDK responses through the existing bounded transport."""

import json
from email.message import Message
from io import BytesIO
from types import SimpleNamespace
from urllib.response import addinfourl

import pytest
from opentour_control.auth.config import AuthError
from opentour_control.auth.workos_provider import WorkOSMembershipProvider, create_workos_client

SUBJECT = "user_fixture"
ADDRESS = "générée+invite@example.invalid"
PROVIDER_DETAIL = "generated-private-provider-detail"


@pytest.fixture
def recipient_workos(identity):
    client, backend = create_workos_client("generated-api-key", identity.config)
    response = SimpleNamespace(
        status=200,
        body={
            "object": "user",
            "id": SUBJECT,
            "email": ADDRESS,
            "email_verified": True,
            "first_name": None,
            "last_name": None,
            "profile_picture_url": None,
            "external_id": None,
            "last_sign_in_at": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    calls = []

    def open_response(request, timeout):
        calls.append((request.get_method(), request.full_url))
        assert 0 < timeout <= 5
        headers = Message()
        headers["Content-Type"] = "application/json"
        return addinfourl(
            BytesIO(json.dumps(response.body).encode()), headers, request.full_url, response.status
        )

    backend.opener = SimpleNamespace(open=open_response)
    try:
        yield WorkOSMembershipProvider(client, identity.config), response, calls
    finally:
        client.close()
        assert backend.close_complete()


def test_verified_recipient_gets_one_current_exact_user(recipient_workos, identity):
    provider, _, calls = recipient_workos

    assert provider.verified_recipient_address(identity.issuer, SUBJECT) == ADDRESS
    assert calls == [("GET", "https://api.workos.com/user_management/users/user_fixture")]


@pytest.mark.parametrize(
    ("foreign_issuer", "subject"),
    [(True, SUBJECT), (False, "user/foreign"), (False, "user%2Fforeign"), (False, "")],
)
def test_verified_recipient_refuses_unbound_lookup(
    recipient_workos, identity, foreign_issuer, subject
):
    provider, _, calls = recipient_workos
    issuer = "https://foreign.example.invalid" if foreign_issuer else identity.issuer

    with pytest.raises(AuthError, match="^SESSION_INVALID$") as failure:
        provider.verified_recipient_address(issuer, subject)

    assert failure.value.status == 401
    assert calls == []


@pytest.mark.parametrize(
    ("status", "overrides", "code", "error_status", "retryable"),
    [
        (404, {}, "EMAIL_VERIFICATION_REQUIRED", 403, False),
        (200, {"email_verified": False}, "EMAIL_VERIFICATION_REQUIRED", 403, False),
        (200, {"email_verified": 1}, "IDENTITY_VERIFICATION_UNAVAILABLE", 503, True),
        (200, {"id": "user_foreign"}, "IDENTITY_VERIFICATION_UNAVAILABLE", 503, True),
        (200, {"email": None}, "IDENTITY_VERIFICATION_UNAVAILABLE", 503, True),
        (200, {"email": f" {ADDRESS}"}, "IDENTITY_VERIFICATION_UNAVAILABLE", 503, True),
        (200, {"email": ADDRESS.upper()}, "IDENTITY_VERIFICATION_UNAVAILABLE", 503, True),
        (
            200,
            {"email": "name\u0085@example.invalid"},
            "IDENTITY_VERIFICATION_UNAVAILABLE",
            503,
            True,
        ),
        (200, {"email": "a@b@example.invalid"}, "IDENTITY_VERIFICATION_UNAVAILABLE", 503, True),
        (
            200,
            {"email": "x" * 240 + "@example.invalid"},
            "IDENTITY_VERIFICATION_UNAVAILABLE",
            503,
            True,
        ),
        (503, {}, "IDENTITY_VERIFICATION_UNAVAILABLE", 503, True),
    ],
)
def test_verified_recipient_fails_safely(
    recipient_workos, identity, caplog, status, overrides, code, error_status, retryable
):
    provider, response, calls = recipient_workos
    response.status = status
    response.body.update({"message": PROVIDER_DETAIL} | overrides)

    with pytest.raises(AuthError, match=f"^{code}$") as failure:
        provider.verified_recipient_address(identity.issuer, SUBJECT)

    assert (failure.value.status, failure.value.retryable) == (error_status, retryable)
    assert calls == [("GET", "https://api.workos.com/user_management/users/user_fixture")]
    serialized = json.dumps(failure.value.wire("request_fixture"))
    assert all(
        secret not in serialized and secret not in caplog.text
        for secret in (PROVIDER_DETAIL, ADDRESS, SUBJECT, "generated-api-key")
    )
