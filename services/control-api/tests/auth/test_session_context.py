"""Generated local ports and actual ASGI handler; no deployed tenant-isolation claim."""

from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from opentour_control.auth.config import AuthError
from opentour_control.auth.session import IdentityUse, create_session_router


def test_valid_session_has_exact_scope(identity):
    result = identity.service.authorization_context(identity.token(), identity.handle).wire()
    assert result["organization_id"] == "org_a" and result["membership_id"] == "member_a"
    assert result["session_generation"] == 0 and result["membership_status"] == "active"
    assert set(result) == {
        "actor",
        "session_id",
        "session_generation",
        "organization_id",
        "membership_id",
        "membership_status",
        "identity_checked_at",
        "authentication_time",
        "assurance_level",
        "authority_revision",
        "revocation_epoch",
        "delegated_grant_id",
    }


def test_member_of_b_cannot_reuse_a_context(identity):
    context = identity.service.authorization_context(identity.token(), identity.handle)
    with pytest.raises(AuthError, match="RESOURCE_NOT_FOUND"):
        context.require_organization("org_b")
    with pytest.raises(AuthError, match="CONTEXT_RECONCILIATION_REQUIRED"):
        identity.service.authorization_context(
            identity.token(org_id="identity_org_b"), identity.handle
        )


def test_zero_org_identity_has_finite_allowlist(identity):
    session = identity.repository.sessions["session_a"]
    identity.repository.sessions["session_a"] = replace(
        session, selected_organization_id=None, selected_membership_id=None
    )
    identity.members.clear()
    for use in IdentityUse:
        result = identity.service.verified_identity(identity.token(), identity.handle, use).wire()
        assert "organization_id" not in result and "delegated_grant_id" not in result
    with pytest.raises(AuthError, match="CAPABILITY_DENIED"):
        identity.service.verified_identity(identity.token(), identity.handle, "tenant_read")
    with pytest.raises(AuthError):
        identity.service.authorization_context(identity.token(), identity.handle)


@pytest.mark.parametrize(
    "state", ["unselected", "first_switch_pending", "mismatch", "pending", "removed"]
)
def test_current_context_never_fabricates_selection(identity, state):
    session = identity.repository.sessions["session_a"]
    token = identity.token()
    expected = "reconciliation_required"
    if state == "first_switch_pending":
        identity.repository.sessions["session_a"] = replace(
            session, selected_organization_id=None, selected_membership_id=None,
            reconciliation_required=True,
        )
    elif state == "unselected":
        identity.repository.sessions["session_a"] = replace(
            session, selected_organization_id=None, selected_membership_id=None
        )
        expected = "unselected"
    elif state == "mismatch":
        token = identity.token(org_id="identity_org_b")
    elif state == "pending":
        identity.repository.sessions["session_a"] = replace(session, reconciliation_required=True)
    else:
        identity.members["org_a"] = replace(identity.members["org_a"], active=False)
        expected = "unselected"
    result = identity.service.current_context(token, identity.handle, "request_a")
    assert result["state"] == expected
    assert result["organization_id"] is result["membership_id"] is None
    assert set(result) == {
        "state",
        "organization_id",
        "membership_id",
        "session_generation",
        "request_id",
    }


def test_session_ending_is_idempotent_and_other_sessions_untouched(identity):
    identity.service.admit(identity.token(sid="session_other"))
    result = identity.service.end(identity.token(), identity.handle)
    assert result["local_ended"] is True and result["session_generation"] == 1
    assert identity.service.end(identity.token(), identity.handle) == result
    with pytest.raises(AuthError, match="SESSION_ENDED"):
        identity.service.authorization_context(identity.token(), identity.handle)
    with pytest.raises(AuthError, match="SESSION_ENDED"):
        identity.service.admit(identity.token())
    assert identity.repository.sessions["session_other"].ended_at is None
    assert identity.members["org_a"].active


def test_denial_commit_unknown_is_not_success(identity):
    identity.repository.fail_commit = True
    with pytest.raises(AuthError, match="SESSION_END_UNCONFIRMED") as failure:
        identity.service.end(identity.token(), identity.handle)
    assert failure.value.status == 503
    assert identity.repository.sessions["session_a"].ended_at is None


def test_forged_or_foreign_binding_cannot_select_session(identity):
    with pytest.raises(AuthError, match="SESSION_INVALID"):
        identity.service.current_context(identity.token(), "x" * 43, "request_a")
    with pytest.raises(AuthError, match="SESSION_INVALID"):
        identity.service.current_context(identity.token(sub="user_b"), identity.handle, "request_a")


def test_old_generation_cannot_admit_delayed_request(identity):
    identity.repository.sessions["session_a"] = replace(
        identity.repository.sessions["session_a"], generation=1
    )
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT"):
        identity.service.authorization_context(identity.token(), identity.handle)


def test_actual_context_route_ignores_forwarded_context(identity):
    app = FastAPI()
    app.include_router(create_session_router(identity.service))
    with TestClient(app) as client:
        client.cookies.set("opentour-session", identity.handle)
        response = client.get(
            "/v1/session/context?organization_id=org_b&subject=user_b",
            headers={
                "Authorization": f"Bearer {identity.token()}",
                "X-Auth-Context": '{"organization_id":"org_b"}',
                "X-Request-ID": "caller-chosen",
            },
        )
    assert response.status_code == 200
    assert response.json()["organization_id"] == "org_a"
    assert response.json()["request_id"] != "caller-chosen"
    assert response.headers["cache-control"] == "no-store"


def test_invalid_route_identity_has_safe_closed_error(identity):
    app = FastAPI()
    app.include_router(create_session_router(identity.service))
    with TestClient(app) as client:
        response = client.get(
            "/v1/session/context", headers={"Authorization": "Bearer bad.jwt"}
        )
    assert response.status_code == 401 and "bad.jwt" not in response.text
    assert set(response.json()) == {
        "code",
        "message",
        "request_id",
        "retryable",
        "field_errors",
        "current_revision",
    }


@pytest.mark.parametrize("method", ["authorization", "current"])
@pytest.mark.parametrize(
    "change", ["ended", "generation", "selection", "reconciliation", "removed"]
)
def test_provider_wait_cannot_return_old_selected_context(identity, method, change):
    identity.repository.observations.clear()
    observed = identity.provider.current_membership("provider_a", identity.now)

    def raced(*args):
        session = identity.repository.sessions["session_a"]
        if change == "removed":
            identity.members["org_a"] = replace(identity.members["org_a"], active=False)
        elif change == "ended":
            identity.repository.end_session(session, identity.now)
        else:
            changes = {
                "generation": {"generation": session.generation + 1},
                "selection": {
                    "selected_organization_id": "org_b",
                    "selected_membership_id": "member_b",
                },
                "reconciliation": {"reconciliation_required": True},
            }
            identity.repository.sessions["session_a"] = replace(session, **changes[change])
        return observed

    identity.provider.current_membership = raced
    if method == "current" and change == "removed":
        result = identity.service.current_context(identity.token(), identity.handle, "request_race")
        assert result["state"] == "unselected"
        assert result["organization_id"] is None
    else:
        with pytest.raises(AuthError):
            if method == "current":
                identity.service.current_context(identity.token(), identity.handle, "request_race")
            else:
                identity.service.authorization_context(identity.token(), identity.handle)


def test_removed_selection_preserves_only_authenticated_recovery_binding(identity):
    _, other = identity.service.admit(identity.token())
    identity.members["org_a"] = replace(identity.members["org_a"], active=False)
    first = identity.service.current_context(identity.token(), identity.handle, "request_first")
    second = identity.service.current_context(identity.token(), identity.handle, "request_second")
    assert first["state"] == second["state"] == "unselected"
    assert first["session_generation"] == second["session_generation"] == 1
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT"):
        identity.service.current_context(identity.token(), other, "request_other")


@pytest.mark.parametrize("shift", [2, -2, 31])
def test_provider_wait_cannot_extend_expiry_or_freshness(identity, shift):
    from datetime import timedelta

    identity.repository.observations.clear()
    observed = identity.provider.current_membership("provider_a", identity.now)

    def crossed(*args):
        identity.service.clock = lambda: identity.now + timedelta(seconds=shift)
        return observed

    identity.provider.current_membership = crossed
    expiry = int(identity.now.timestamp()) + (1 if shift == 2 else 600)
    with pytest.raises(AuthError):
        identity.service.authorization_context(
            identity.token(exp=expiry), identity.handle, sensitive=True
        )


def test_actual_sdk_fault_body_never_reaches_api_or_logs(identity, caplog):
    import httpx2 as httpx
    from opentour_control.auth.workos_provider import WorkOSMembershipProvider
    from workos import WorkOSClient

    sentinel = "GENERATED_PROVIDER_CREDENTIAL_SENTINEL"
    calls = []

    def response(request):
        calls.append(request.url.path)
        return httpx.Response(500, json={"message": sentinel})

    identity.repository.observations.clear()
    with httpx.Client(transport=httpx.MockTransport(response)) as http:
        sdk = WorkOSClient(
            api_key="generated-api-key",
            client_id="client_fixture",
            http_client=http,
            max_retries=0,
            request_timeout=5,
        )
        identity.service.provider = WorkOSMembershipProvider(sdk, identity.config)
        app = FastAPI()
        app.include_router(create_session_router(identity.service))
        with TestClient(app) as client:
            client.cookies.set("opentour-session", identity.handle)
            result = client.get(
                "/v1/session/context", headers={"Authorization": f"Bearer {identity.token()}"}
            )
        assert result.status_code == 200 and result.json()["state"] == "reconciliation_required"
        assert sentinel not in result.text and sentinel not in caplog.text
        assert len(calls) == 1


@pytest.mark.parametrize("change", ["provider_denial", "expiry", "freshness"])
def test_final_state_read_cannot_hide_denial_or_time_passage(identity, change):
    from datetime import timedelta

    load = identity.repository.load_session
    reads = 0

    def changed(*args, **kwargs):
        nonlocal reads
        reads += 1
        result = load(*args, **kwargs)
        if reads == 2:
            if change == "provider_denial":
                observed = identity.repository.observation(identity.issuer, "provider_a")
                identity.repository.record_observation(
                    replace(observed, status="removed", locally_denied=True)
                )
            else:
                elapsed = 601 if change == "expiry" else 31
                identity.service.clock = lambda: identity.now + timedelta(seconds=elapsed)
        return result

    identity.repository.load_session = changed
    with pytest.raises(AuthError):
        identity.service.authorization_context(identity.token(), identity.handle, sensitive=True)


def test_admission_logs_only_safe_freshness_metadata(identity, caplog):
    import logging

    with caplog.at_level(logging.INFO, logger="opentour_control.auth.session"):
        identity.service.authorization_context(identity.token(), identity.handle, sensitive=True)
    record = next(r for r in caplog.records if r.message == "identity_admission_checked")
    assert record.identity_freshness_policy_seconds == 30
    assert record.identity_checked_at == record.identity_observed_at
    assert "user_a" not in caplog.text and identity.handle not in caplog.text


@pytest.mark.parametrize("status", ["pending_identity", "reconciliation_unknown"])
def test_pending_provider_context_never_fabricates_removal(identity, status):
    before = identity.repository.sessions["session_a"]
    observed = identity.repository.observation(identity.issuer, "provider_a")
    identity.repository.record_observation(replace(observed, status=status))
    context = identity.service.current_context(identity.token(), identity.handle, "request_pending")
    assert context["state"] == "reconciliation_required"
    assert context["organization_id"] is context["membership_id"] is None
    assert context["session_generation"] == before.generation
    assert identity.repository.sessions["session_a"] == before
    with pytest.raises(AuthError, match="CONTEXT_RECONCILIATION_REQUIRED"):
        identity.service.authorization_context(identity.token(), identity.handle)


@pytest.mark.parametrize("mapping", [None, ""])
def test_missing_provider_organization_mapping_never_falls_back_to_local_id(identity, mapping):
    identity.members["org_a"] = replace(
        identity.members["org_a"], identity_organization_id=mapping
    )
    with pytest.raises(AuthError, match="SCHEMA_INVALID"):
        identity.service.authorization_context(identity.token(org_id="org_a"), identity.handle)


@pytest.mark.parametrize(
    "disagreement", ["mapping", "jwt_local_id", "observation", "removed_foreign"]
)
def test_provider_mapping_disagreement_keeps_local_selection_for_reconciliation(
    identity, disagreement
):
    before = identity.repository.sessions["session_a"]
    token = identity.token()
    if disagreement == "mapping":
        identity.members["org_a"] = replace(
            identity.members["org_a"], identity_organization_id="identity_org_b"
        )
    elif disagreement == "jwt_local_id":
        token = identity.token(org_id="org_a")
    else:
        observation = identity.repository.observation(identity.issuer, "provider_a")
        identity.repository.observations[identity.issuer, "provider_a"] = replace(
            observation,
            organization_id="identity_org_b",
            status="removed" if disagreement == "removed_foreign" else "active",
            locally_denied=disagreement == "removed_foreign",
        )
    with pytest.raises(AuthError):
        identity.service.authorization_context(token, identity.handle)
    view = identity.service.current_context(token, identity.handle, "mapping_reconciliation")
    assert view["state"] == "reconciliation_required"
    assert view["organization_id"] is view["membership_id"] is None
    assert identity.repository.sessions["session_a"] == before


@pytest.mark.parametrize("method", ["authorization", "current"])
def test_provider_mapping_changed_during_fetch_cannot_return_old_context(identity, method):
    identity.repository.observations.clear()
    observation = identity.provider.current_membership("provider_a", identity.now)

    def changed(*args):
        identity.members["org_a"] = replace(
            identity.members["org_a"], identity_organization_id="identity_org_b"
        )
        return observation

    identity.provider.current_membership = changed
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT"):
        if method == "authorization":
            identity.service.authorization_context(identity.token(), identity.handle)
        else:
            identity.service.current_context(identity.token(), identity.handle, "changed_mapping")


@pytest.mark.parametrize("method", ["authorization", "current"])
@pytest.mark.parametrize("outcome", ["removed", "not_found"])
def test_removal_from_old_provider_mapping_cannot_deny_new_selection(identity, method, outcome):
    before = identity.repository.sessions["session_a"]
    observation = identity.repository.observations.pop((identity.issuer, "provider_a"))

    def changed(*args):
        identity.members["org_a"] = replace(
            identity.members["org_a"], identity_organization_id="identity_org_b"
        )
        if outcome == "not_found":
            raise AuthError("PROVIDER_MEMBERSHIP_REMOVED", 404)
        return replace(observation, status="removed", locally_denied=True)

    identity.provider.current_membership = changed
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT") as refused:
        if method == "authorization":
            identity.service.authorization_context(identity.token(), identity.handle)
        else:
            identity.service.current_context(identity.token(), identity.handle, "changed_mapping")
    assert refused.value.status == 409
    assert identity.repository.sessions["session_a"] == before
    assert not identity.repository.observations


def test_mapping_changed_while_reading_denial_preserves_current_selection(identity):
    before = identity.repository.sessions["session_a"]
    read_observation = identity.repository.observation
    observation = read_observation(identity.issuer, "provider_a")
    identity.repository.record_observation(
        replace(observation, status="removed", locally_denied=True)
    )

    def changed(*args):
        identity.members["org_a"] = replace(
            identity.members["org_a"], provider_membership_id="provider_b"
        )
        return read_observation(*args)

    identity.repository.observation = changed
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT") as refused:
        identity.service.current_context(identity.token(), identity.handle, "changed_mapping")
    assert refused.value.status == 409
    assert identity.repository.sessions["session_a"] == before


def test_locally_missing_membership_reappears_before_selection_clear(identity):
    before = identity.repository.sessions["session_a"]
    membership = identity.members.pop("org_a")
    repository = identity.repository
    load_session = repository.load_session
    first_request_load = repository.load_calls + 1

    def reappeared(*args, **kwargs):
        if repository.load_calls == first_request_load:
            identity.members["org_a"] = membership
        return load_session(*args, **kwargs)

    repository.load_session = reappeared
    with pytest.raises(AuthError, match="STALE_SESSION_CONTEXT") as refused:
        identity.service.current_context(identity.token(), identity.handle, "reappeared_membership")
    assert refused.value.status == 409
    assert repository.sessions["session_a"] == before


def test_provider_mapping_never_weakens_verified_issuer_binding(identity):
    identity.repository.observations.clear()
    observation = identity.provider.current_membership("provider_a", identity.now)
    identity.provider.current_membership = lambda *args: replace(
        observation, issuer="https://other-identity.example.invalid/"
    )
    with pytest.raises(AuthError, match="IDENTITY_MEMBERSHIP_CONFLICT"):
        identity.service.authorization_context(identity.token(), identity.handle)
    assert not identity.repository.observations


@pytest.mark.parametrize("local_change", ["unchanged", "inactive", "authority_revision"])
def test_provider_removed_response_uses_explicit_identity_organization_mapping(
    identity, local_change
):
    before = identity.repository.sessions["session_a"]
    identity.repository.observations.clear()

    def removed(*args):
        if local_change == "inactive":
            identity.members["org_a"] = replace(identity.members["org_a"], active=False)
        elif local_change == "authority_revision":
            identity.members["org_a"] = replace(
                identity.members["org_a"], authority_revision="authority_b", revocation_epoch=3
            )
        raise AuthError("PROVIDER_MEMBERSHIP_REMOVED", 404)

    identity.provider.current_membership = removed
    view = identity.service.current_context(identity.token(), identity.handle, "removed_mapping")
    assert view["state"] == "unselected"
    assert view["session_generation"] == before.generation + 1
    observed = identity.repository.observation(identity.issuer, "provider_a")
    assert observed.organization_id == "identity_org_a" and observed.locally_denied
