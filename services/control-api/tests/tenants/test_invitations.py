import json
from email.message import Message
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import create_autospec
from urllib.response import addinfourl

import psycopg
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from opentour_control.auth.config import AuthError, digest
from opentour_control.auth.workos_provider import WorkOSMembershipProvider, create_workos_client
from opentour_control.tenants.invitations import (
    InvitationAcceptanceReceipt,
    InvitationAcceptanceStore,
    VerifiedRecipientProvider,
    create_invitation_acceptance_router,
)
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[4]
CONTRACTS = ROOT / "packages" / "contracts"
INVITATION = json.loads((CONTRACTS / "platform" / "invitation.schema.json").read_text())
COMMON = json.loads((CONTRACTS / "common" / "common-contract.schema.json").read_text())
REGISTRY = Registry().with_resource(COMMON["$id"], Resource.from_contents(COMMON))


def validator(definition: str) -> Draft202012Validator:
    schema = {**INVITATION, "$ref": f"#/$defs/{definition}"}
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, registry=REGISTRY)


@pytest.fixture
def receipt() -> dict[str, object]:
    return {
        "invitation_id": "invite_generated",
        "membership_id": "member_generated",
        "operation_id": "operation_generated",
        "state": "awaiting_identity",
        "revision": "9007199254740993",
        "request_id": "request_generated",
    }


@pytest.mark.parametrize("token", ["generated", "x" * 512])
def test_accept_request_accepts_opaque_token(token: str) -> None:
    validator("AcceptRequest").validate({"token": token})


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        "generated",
        {},
        {"invitation_token": "generated"},
        {"token": "generated", "invitation_id": "invite_generated"},
        {"token": "generated", "organization_id": "other_org"},
        {"token": ""},
        {"token": "x" * 513},
        {"token": 1},
        {"token": "generated\u0000"},
        {"token": "generated\n"},
        {"token": "generated\u007f"},
        {"token": "generated\u0085"},
    ],
)
def test_accept_request_rejects_invalid_body(body: object) -> None:
    assert not validator("AcceptRequest").is_valid(body)


@pytest.mark.parametrize(
    ("state", "operation_id", "receipt_definition"),
    [
        ("awaiting_identity", "operation_generated", "AcceptPendingReceipt"),
        ("active", "operation_generated", "AcceptConfirmedReceipt"),
        ("already_member", None, "AcceptConfirmedReceipt"),
    ],
)
def test_receipt_is_only_valid_for_its_http_status(
    receipt: dict[str, object],
    state: str,
    operation_id: str | None,
    receipt_definition: str,
) -> None:
    receipt.update(state=state, operation_id=operation_id)
    validator(receipt_definition).validate(receipt)
    other = (
        "AcceptConfirmedReceipt"
        if receipt_definition == "AcceptPendingReceipt"
        else "AcceptPendingReceipt"
    )
    assert not validator(other).is_valid(receipt)


@pytest.mark.parametrize(
    "changed_fields",
    [
        {"operation_id": None},
        {"state": "active", "operation_id": None},
        {"state": "already_member", "operation_id": "operation_generated"},
        {"state": "provider_outcome_unknown"},
        {"invitation_id": ""},
        {"membership_id": ""},
        {"operation_id": ""},
        {"request_id": ""},
        {"revision": 1},
        {"revision": "01"},
        {"revision": "9223372036854775808"},
        {"token": "generated"},
        {"workspace_id": "workspace_generated"},
    ],
)
def test_receipt_rejects_invalid_projection(
    receipt: dict[str, object], changed_fields: dict[str, object]
) -> None:
    assert not validator("AcceptReceipt").is_valid({**receipt, **changed_fields})


def test_receipt_requires_all_six_fields(receipt: dict[str, object]) -> None:
    accept_receipt = validator("AcceptReceipt")
    for field in receipt:
        assert not accept_receipt.is_valid(
            {key: value for key, value in receipt.items() if key != field}
        )


@pytest.mark.parametrize("body", [None, [], "active"])
def test_receipt_requires_an_object(body: object) -> None:
    assert not validator("AcceptReceipt").is_valid(body)


ROUTE = "/v1/invitations:accept"
INVITE = " generated-invitation "
ADDRESS = "generated-recipient@example.invalid"


@pytest.fixture
def acceptance(identity, receipt):
    token = identity.token(sub="user_invitee", sid="session_invitee", org_id=None)
    context, binding = identity.service.admit(token)
    assert identity.repository.sessions[context.session_id].selected_organization_id is None
    recipients = create_autospec(VerifiedRecipientProvider, instance=True)
    recipients.verified_recipient_address.return_value = ADDRESS
    store = create_autospec(InvitationAcceptanceStore, instance=True)
    store.accept.return_value = InvitationAcceptanceReceipt.model_validate(receipt)

    app = FastAPI()
    app.include_router(create_invitation_acceptance_router(identity.service, recipients, store))
    with TestClient(app) as client:
        yield SimpleNamespace(
            client=client,
            headers={"Authorization": f"Bearer {token}", "Cookie": f"opentour-session={binding}"},
            token=token,
            binding=binding,
            context=context,
            recipients=recipients,
            store=store,
            identity=identity,
        )


@pytest.mark.parametrize(
    ("state", "operation", "status", "response_definition"),
    [
        ("awaiting_identity", "operation_generated", 202, "AcceptPendingReceipt"),
        ("active", "operation_generated", 200, "AcceptConfirmedReceipt"),
        ("already_member", None, 200, "AcceptConfirmedReceipt"),
    ],
)
def test_acceptance_uses_preorganization_identity_and_digest_only(
    acceptance, receipt, state, operation, status, response_definition
) -> None:
    receipt.update(state=state, operation_id=operation)
    acceptance.store.accept.return_value = InvitationAcceptanceReceipt.model_validate(receipt)
    response = acceptance.client.post(ROUTE, headers=acceptance.headers, json={"token": INVITE})

    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Vary"] == "Authorization, Cookie"
    assert response.json() == receipt
    validator(response_definition).validate(response.json())
    acceptance.recipients.verified_recipient_address.assert_called_once_with(
        acceptance.identity.issuer, "user_invitee"
    )
    acceptance.store.accept.assert_called_once()
    args, kwargs = acceptance.store.accept.call_args
    assert args == (acceptance.context,)
    assert kwargs.pop("request_id")
    assert kwargs == {
        "session_binding": acceptance.binding,
        "recipient_address": ADDRESS,
        "token_sha256": digest(INVITE),
    }
    assert ADDRESS not in response.text and INVITE not in response.text


@pytest.mark.parametrize(
    "body",
    [
        '{"token":"a","token":"b"}',
        '{"invitation_token":"a"}',
        '{"token":"a","recipient_email":"generated@example.invalid"}',
        '{"token":"a","organization_id":"foreign_org"}',
        '{"token":"\\u0085"}',
        '{"token":"\\ud800"}',
        '{"token":true}',
        '{"token":',
    ],
)
def test_acceptance_denies_unsafe_bodies_before_recipient_or_storage(acceptance, body) -> None:
    response = acceptance.client.post(ROUTE, headers=acceptance.headers, content=body)
    assert response.status_code == 422
    assert response.json()["code"] == "SCHEMA_INVALID"
    acceptance.recipients.verified_recipient_address.assert_not_called()
    acceptance.store.accept.assert_not_called()


def test_acceptance_rejects_envelope_limit_and_url_token(acceptance) -> None:
    oversized = acceptance.client.post(
        ROUTE,
        headers=acceptance.headers,
        content=(" " * 16_384) + '{"token":"bounded-but-envelope-is-not"}',
    )
    query = acceptance.client.post(
        ROUTE, headers=acceptance.headers, params={"token": INVITE}, json={"token": INVITE}
    )
    alias = acceptance.client.post(
        "/v1/invitations/generated:accept", headers=acceptance.headers, json={"token": INVITE}
    )
    assert (oversized.status_code, query.status_code, alias.status_code) == (422, 422, 404)
    acceptance.recipients.verified_recipient_address.assert_not_called()
    acceptance.store.accept.assert_not_called()


@pytest.mark.parametrize(
    "fault", ["signature", "mismatched_binding", "duplicate_cookie", "no_bearer"]
)
def test_acceptance_rejects_invalid_session_before_provider(acceptance, fault) -> None:
    headers = dict(acceptance.headers)
    if fault == "signature":
        headers["Authorization"] = "Bearer generated_invalid"
    elif fault == "mismatched_binding":
        headers["Cookie"] = f"opentour-session={acceptance.identity.handle}"
    elif fault == "duplicate_cookie":
        headers["Cookie"] += f"; opentour-session={acceptance.identity.handle}"
    else:
        del headers["Authorization"]
    response = acceptance.client.post(ROUTE, headers=headers, json={"token": INVITE})
    assert response.status_code == 401 and response.json()["code"] == "SESSION_INVALID"
    acceptance.recipients.verified_recipient_address.assert_not_called()
    acceptance.store.accept.assert_not_called()


@pytest.mark.parametrize("fault", ["ended", "generation_changed"])
def test_acceptance_rechecks_session_after_provider_io(acceptance, fault) -> None:
    def observe(issuer, subject):
        if fault == "ended":
            acceptance.identity.service.end(acceptance.token, acceptance.binding)
        else:
            repository = acceptance.identity.repository
            session = repository.sessions[acceptance.context.session_id]
            repository.clear_selection(session, digest(acceptance.binding))
        return ADDRESS

    acceptance.recipients.verified_recipient_address.side_effect = observe
    response = acceptance.client.post(ROUTE, headers=acceptance.headers, json={"token": INVITE})
    expected = (401, "SESSION_ENDED") if fault == "ended" else (409, "STALE_SESSION_CONTEXT")
    assert (response.status_code, response.json()["code"]) == expected
    acceptance.store.accept.assert_not_called()


@pytest.mark.parametrize(
    ("code", "status"),
    [("EMAIL_VERIFICATION_REQUIRED", 403), ("IDENTITY_VERIFICATION_UNAVAILABLE", 503)],
)
def test_acceptance_propagates_safe_provider_refusal_before_any_write(
    acceptance, code, status
) -> None:
    acceptance.recipients.verified_recipient_address.side_effect = AuthError(code, status)
    response = acceptance.client.post(ROUTE, headers=acceptance.headers, json={"token": INVITE})
    assert response.status_code == status and response.json()["code"] == code
    validator_common = Draft202012Validator(
        {"$ref": COMMON["$id"] + "#/$defs/Error"}, registry=REGISTRY
    )
    validator_common.validate(response.json())
    acceptance.store.accept.assert_not_called()


@pytest.mark.parametrize("verified", [True, False])
def test_protected_route_reads_the_exact_workos_recipient(acceptance, verified) -> None:
    profile = acceptance.identity.config
    client, backend = create_workos_client("generated-provider-key", profile)
    calls = []

    def get_user(request, timeout):
        calls.append((request.get_method(), request.full_url))
        assert 0 < timeout <= 5
        payload = {
            "object": "user",
            "id": "user_invitee",
            "email": ADDRESS,
            "email_verified": verified,
            "first_name": None,
            "last_name": None,
            "profile_picture_url": None,
            "external_id": None,
            "last_sign_in_at": None,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
        headers = Message()
        headers["Content-Type"] = "application/json"
        return addinfourl(BytesIO(json.dumps(payload).encode()), headers, request.full_url, 200)

    backend.opener = SimpleNamespace(open=get_user)
    app = FastAPI()
    app.include_router(
        create_invitation_acceptance_router(
            acceptance.identity.service, WorkOSMembershipProvider(client, profile), acceptance.store
        )
    )
    try:
        with TestClient(app) as protected:
            response = protected.post(ROUTE, headers=acceptance.headers, json={"token": INVITE})
        assert calls == [("GET", "https://api.workos.com/user_management/users/user_invitee")]
        if verified:
            assert response.status_code == 202
            assert acceptance.store.accept.call_args.kwargs["recipient_address"] == ADDRESS
            assert acceptance.store.accept.call_args.kwargs["token_sha256"] == digest(INVITE)
        else:
            assert response.status_code == 403
            assert response.json()["code"] == "EMAIL_VERIFICATION_REQUIRED"
            acceptance.store.accept.assert_not_called()
        assert ADDRESS not in response.text and INVITE not in response.text
    finally:
        client.close()
        assert backend.close_complete()


@pytest.mark.parametrize("failure", ["database", "invalid_projection"])
def test_acceptance_refuses_an_unconfirmed_write_without_credential_leak(
    acceptance, failure
) -> None:
    if failure == "database":
        acceptance.store.accept.side_effect = psycopg.OperationalError(f"{ADDRESS} {INVITE}")
    else:
        acceptance.store.accept.return_value = {"email": ADDRESS, "token": INVITE}
    response = acceptance.client.post(ROUTE, headers=acceptance.headers, json={"token": INVITE})
    assert response.status_code == 503 and response.json()["code"] == "IDENTITY_STATE_UNAVAILABLE"
    assert response.headers["Cache-Control"] == "no-store"
    assert ADDRESS not in response.text and INVITE not in response.text
