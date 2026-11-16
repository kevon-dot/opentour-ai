"""Actual ASGI/identity composition with generated nondurable admission ports."""

from dataclasses import replace
from datetime import timedelta

import psycopg
import pytest
from opentour_control.auth.config import AuthError
from opentour_control.auth.session import IdentityUse
from opentour_control.tenants.models import SetupError

BODY = {"display_name": "Generated buyer", "accepted_terms_revision": "terms_generated"}


def assert_error(response, status, code):
    assert response.status_code == status
    result = response.json()
    assert result["code"] == code
    assert set(result) == {
        "code",
        "message",
        "request_id",
        "retryable",
        "field_errors",
        "current_revision",
    }
    assert response.headers["cache-control"] == "no-store"
    assert result["request_id"]
    return result


def test_zero_org_verified_person_gets_pending_receipt_without_remote_mutation(setup):
    identity = setup.identity
    session = identity.repository.sessions["session_a"]
    identity.repository.sessions["session_a"] = replace(
        session, selected_organization_id=None, selected_membership_id=None
    )
    identity.members.clear()
    response = setup.client.post("/v1/organizations", json=BODY)
    assert response.status_code == 202
    result = response.json()
    assert set(result) == {"organization_id", "operation_id", "state", "revision", "request_id"}
    assert result["state"] == "identity_provisioning" and result["revision"] == "1"
    assert setup.provider.checked == ["user_a"] and len(setup.repository.admissions) == 1
    creator, body, environment, _ = setup.repository.admissions[0]
    assert creator.subject == "user_a" and creator.issuer == identity.issuer
    assert environment == "env_generated" and body.display_name == BODY["display_name"]


def test_exact_replay_returns_original_receipt_after_terms_and_progress_change(setup):
    first = setup.client.post("/v1/organizations", json=BODY)
    assert first.status_code == 202
    setup.repository.published = replace(setup.repository.published, revision="new_terms_generated")
    setup.repository.operation = replace(
        setup.operation,
        state="active",
        revision=7,
        provider_org_id="provider_generated",
        stage_outcome="observed",
    )
    setup.provider.verified = False
    replay = setup.client.post("/v1/organizations", json=BODY)
    assert replay.status_code == 202 and replay.json() == first.json()
    assert len(setup.repository.admissions) == 1 and setup.provider.checked == ["user_a"]
    progress = setup.client.get("/v1/operations/operation_generated")
    assert progress.status_code == 200 and progress.json()["state"] == "active"
    assert progress.json()["revision"] == "7" and "workspace_id" not in progress.json()


@pytest.mark.parametrize(
    "field,value",
    [("display_name", "Changed buyer"), ("accepted_terms_revision", "different_terms")],
)
def test_same_key_changed_body_conflicts_without_new_admission(setup, field, value):
    assert setup.client.post("/v1/organizations", json=BODY).status_code == 202
    assert_error(
        setup.client.post("/v1/organizations", json={**BODY, field: value}),
        409,
        "IDEMPOTENCY_CONFLICT",
    )
    assert len(setup.repository.admissions) == 1 and len(setup.provider.checked) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("organization_id", "foreign"),
        ("email", "buyer@example.invalid"),
        ("role", "owner"),
        ("actor", {"subject": "foreign"}),
        ("mode", "opentour_managed"),
    ],
)
def test_body_cannot_supply_tenant_identity_role_or_mode(setup, field, value):
    assert_error(
        setup.client.post("/v1/organizations", json={**BODY, field: value}), 422, "SCHEMA_INVALID"
    )
    assert not setup.repository.admissions and not setup.provider.checked


@pytest.mark.parametrize(
    "credential",
    ["absent", "malformed", "bad_signature", "wrong_audience", "wrong_issuer", "no_binding"],
)
def test_unverified_identity_never_reaches_provider_or_admission(setup, credential):
    if credential == "absent":
        del setup.client.headers["authorization"]
    elif credential == "malformed":
        setup.client.headers["authorization"] = "Basic generated"
    elif credential == "bad_signature":
        setup.client.headers["authorization"] = "Bearer x.y.z"
    elif credential == "wrong_audience":
        setup.client.headers["authorization"] = "Bearer " + setup.identity.token(
            aud="https://foreign.example.invalid"
        )
    elif credential == "wrong_issuer":
        setup.client.headers["authorization"] = "Bearer " + setup.identity.token(
            iss="https://foreign.example.invalid"
        )
    else:
        setup.client.cookies.clear()
    response = setup.client.post("/v1/organizations", json=BODY)
    assert response.status_code == 401 and response.json()["current_revision"] is None
    assert not setup.provider.checked and not setup.repository.admissions


def test_provider_verified_email_is_required_without_domain_inference(setup):
    setup.provider.verified = False
    result = assert_error(
        setup.client.post("/v1/organizations", json=BODY), 403, "EMAIL_VERIFICATION_REQUIRED"
    )
    assert result["current_revision"] is None and not setup.repository.admissions


@pytest.mark.parametrize("mutation", ["logout", "generation"])
def test_identity_change_during_email_lookup_refuses_admission(setup, mutation):
    def changed():
        session = setup.identity.repository.sessions["session_a"]
        setup.identity.repository.sessions["session_a"] = replace(
            session,
            **(
                {"ended_at": setup.identity.now}
                if mutation == "logout"
                else {"generation": session.generation + 1}
            ),
        )

    setup.provider.during_check = changed
    response = setup.client.post("/v1/organizations", json=BODY)
    assert response.status_code == (401 if mutation == "logout" else 409)
    assert not setup.repository.admissions


def test_provider_lookup_outage_is_safe_and_has_no_intent(setup):
    def unavailable():
        raise SetupError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, retryable=True)

    setup.provider.during_check = unavailable
    result = assert_error(
        setup.client.post("/v1/organizations", json=BODY), 503, "IDENTITY_VERIFICATION_UNAVAILABLE"
    )
    assert result["retryable"] and not setup.repository.admissions


def test_public_terms_need_no_session_and_never_invent_unpublished_revision(setup):
    setup.client.headers.pop("authorization")
    setup.client.cookies.clear()
    response = setup.client.get("/v1/organization-terms")
    assert response.status_code == 200 and set(response.json()) == {
        "revision",
        "document_url",
        "effective_at",
    }
    setup.repository.published = replace(
        setup.repository.published, effective_at=setup.identity.now + timedelta(seconds=1)
    )
    assert_error(setup.client.get("/v1/organization-terms"), 503, "TERMS_UNAVAILABLE")
    setup.repository.published = None
    assert_error(setup.client.get("/v1/organization-terms"), 503, "TERMS_UNAVAILABLE")


@pytest.mark.parametrize(
    "raw",
    [
        b'{"display_name":"x"}',
        b'{"display_name":"x","accepted_terms_revision":"t","accepted_terms_revision":"u"}',
        b"{" + b" " * 16_384 + b"}",
    ],
)
def test_missing_duplicate_or_oversize_terms_body_has_no_effect(setup, raw):
    assert_error(setup.client.post("/v1/organizations", content=raw), 422, "SCHEMA_INVALID")
    assert not setup.provider.checked and not setup.repository.admissions


def test_missing_idempotency_key_and_unconfigured_admission_policy_refuse(setup):
    del setup.client.headers["idempotency-key"]
    assert_error(setup.client.post("/v1/organizations", json=BODY), 422, "SCHEMA_INVALID")
    assert not setup.provider.checked
    setup.client.headers["idempotency-key"] = "generated-key"
    setup.repository.failure = SetupError("ADMISSION_POLICY_UNAVAILABLE", 503)
    assert_error(
        setup.client.post("/v1/organizations", json=BODY), 503, "ADMISSION_POLICY_UNAVAILABLE"
    )
    assert not setup.repository.admissions


def test_admission_failures_preserve_canonical_errors_and_retry_after(setup):
    setup.repository.failure = SetupError(
        "TERMS_REVISION_STALE", 422, field="accepted_terms_revision"
    )
    response = setup.client.post("/v1/organizations", json=BODY)
    result = assert_error(response, 422, "TERMS_REVISION_STALE")
    assert result["field_errors"] == [
        {"field": "accepted_terms_revision", "code": "TERMS_REVISION_STALE"}
    ]
    # Transport of an owner-supplied policy refusal; this does not choose counting semantics.
    setup.repository.failure = SetupError("ADMISSION_LIMIT", 429, retry_after=60)
    response = setup.client.post("/v1/organizations", json=BODY)
    assert_error(response, 429, "ADMISSION_LIMIT")
    assert response.headers["retry-after"] == "60" and not setup.repository.admissions


def test_database_commit_ambiguity_exposes_no_raw_error_or_success(setup):
    marker = "generated-private-database-marker"
    setup.repository.failure = psycopg.OperationalError(marker)
    response = setup.client.post("/v1/organizations", json=BODY)
    assert_error(response, 503, "SETUP_ADMISSION_UNCONFIRMED")
    assert marker not in response.text and not setup.repository.admissions


def test_progress_checks_exact_creator_before_disclosing_revision(setup):
    setup.repository.operation = replace(
        setup.operation, creator_subject="foreign_user", revision=19
    )
    result = assert_error(
        setup.client.get("/v1/operations/operation_generated"), 404, "RESOURCE_NOT_FOUND"
    )
    assert result["current_revision"] is None and "foreign_user" not in str(result)


@pytest.mark.parametrize("action,status", [("retry", 202), ("cancel", 200)])
def test_action_routes_pass_exact_fence_and_never_call_provider(setup, action, status):
    response = setup.client.post(
        f"/v1/operations/operation_generated/{action}",
        json={"expected_revision": "9007199254740993"},
    )
    assert response.status_code == status
    assert setup.repository.changes[0][:3] == ("operation_generated", action, "9007199254740993")
    assert not setup.provider.checked


@pytest.mark.parametrize(
    "raw",
    [
        b"{}",
        b"[]",
        b'{"expected_revision":1}',
        b'{"expected_revision":"01"}',
        b'{"expected_revision":"1","expected_revision":"2"}',
        b'{"expected_revision":"1","action":"cancel"}',
    ],
)
def test_action_body_is_closed_duplicate_free_and_requires_exact_revision(setup, raw):
    assert_error(
        setup.client.post("/v1/operations/operation_generated/retry", content=raw),
        422,
        "SCHEMA_INVALID",
    )
    assert not setup.repository.changes


def test_service_rejects_identity_context_from_different_issuer(setup, monkeypatch):
    verified = setup.identity.service.verified_identity(
        setup.identity.token(), setup.identity.handle, IdentityUse.ORGANIZATION_CREATE
    )
    foreign = replace(
        verified, actor={**verified.actor, "issuer": "https://foreign.example.invalid"}
    )
    monkeypatch.setattr(setup.identity.service, "verified_identity", lambda *args: foreign)
    with pytest.raises((AuthError, SetupError)):
        setup.service.progress("generated", "generated", "operation_generated", "request_generated")


@pytest.mark.parametrize("method", ["GET", "POST"])
@pytest.mark.parametrize(
    "bad_url", ["http://terms.example.invalid", "https://terms.example.invalid/#fragment"]
)
def test_malformed_published_terms_returns_safe_error_without_admission(
    setup, monkeypatch, method, bad_url
):
    setup.repository.published = replace(setup.repository.published, document_url=bad_url)

    def validate_admission(creator, body, key_hash, request_id, environment, now):
        # Generated repository boundary invokes the actual publication validator.
        setup.repository.published.current_view(now)
        pytest.fail("Malformed terms must not reach intent creation")

    monkeypatch.setattr(setup.repository, "create", validate_admission)
    response = (
        setup.client.get("/v1/organization-terms")
        if method == "GET"
        else setup.client.post("/v1/organizations", json=BODY)
    )
    assert_error(response, 503, "TERMS_UNAVAILABLE")
    assert not setup.repository.admissions and setup.repository.recorded is None
