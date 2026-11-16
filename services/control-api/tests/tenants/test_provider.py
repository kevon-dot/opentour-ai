"""Pinned WorkOS SDK and owned transport with simulated responses; no provider calls."""

from __future__ import annotations

import json
import threading
import traceback
from dataclasses import replace
from types import SimpleNamespace

import pytest
from opentour_control.tenants.models import SetupError
from opentour_control.tenants.provider import (
    ProviderRejected,
    ProviderUnknown,
    ProvisioningHTTPBackend,
    WorkOSProvisioningProvider,
)
from workos import HTTPResponse, TransportError, TransportTimeout, WorkOSClient

DATE = "2026-11-15T12:00:00.000Z"


def user(**changes):
    return {
        "object": "user",
        "id": "user_a",
        "first_name": "Generated",
        "last_name": None,
        "profile_picture_url": None,
        "email": "generated@example.invalid",
        "email_verified": True,
        "external_id": None,
        "last_sign_in_at": None,
        "created_at": DATE,
        "updated_at": DATE,
        **changes,
    }


def organization(op, **changes):
    return {
        "object": "organization",
        "id": "org_generated",
        "name": op.display_name,
        "domains": [],
        "metadata": {},
        "external_id": op.correlation,
        "created_at": DATE,
        "updated_at": DATE,
        **changes,
    }


def membership(**changes):
    return {
        "object": "organization_membership",
        "id": "member_generated",
        "user_id": "user_a",
        "organization_id": "org_generated",
        "status": "active",
        "directory_managed": False,
        "created_at": DATE,
        "updated_at": DATE,
        "role": {"slug": "member"},
        "roles": [{"slug": "member"}],
        "user": user(),
        **changes,
    }


class SDKTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, content, timeout):
        self.calls.append((method, url, content, timeout))
        assert self.responses, "No automatic resubmission is allowed"
        status, body = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        return HTTPResponse(status, {"content-type": "application/json"}, data, method, url)

    def close(self):
        pass


def sdk_provider(operation, responses):
    backend = SDKTransport(responses)
    # Deliberately keep SDK defaults; each production provider call must override retries.
    client = WorkOSClient(
        api_key="generated_key", client_id="client_generated", http_client=backend
    )
    return WorkOSProvisioningProvider(
        client, "env_generated", "member", clock=lambda: operation.updated_at
    ), backend


def test_sdk_org_and_member_create_use_persisted_scope_and_no_domain_or_owner_inference(operation):
    provider, backend = sdk_provider(
        operation, [(200, organization(operation)), (200, membership())]
    )
    observed = provider.create_organization(operation)
    assert (
        observed.organization_id == "org_generated"
        and observed.correlation == operation.correlation
    )
    member = provider.create_membership(
        replace(operation, provider_org_id=observed.organization_id)
    )
    assert member.status == "active" and member.subject == operation.creator_subject
    org_body, member_body = (json.loads(call[2]) for call in backend.calls)
    assert org_body == {"name": operation.display_name, "external_id": operation.correlation}
    assert member_body == {
        "user_id": "user_a",
        "organization_id": "org_generated",
        "role_slug": "member",
    }
    assert all(0 < call[3] <= 5 for call in backend.calls)


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 429, 500, 503])
@pytest.mark.parametrize("action", ["organization", "membership"])
def test_mutations_never_retry_and_only_definitive_validation_rejections_are_rejected(
    operation, status, action
):
    provider, backend = sdk_provider(
        operation, [(status, {"message": "generated-private-provider-marker"})]
    )
    expected = ProviderRejected if status in {400, 422} else ProviderUnknown
    call = provider.create_organization if action == "organization" else provider.create_membership
    with pytest.raises(expected) as error:
        call(replace(operation, provider_org_id="org_generated"))
    assert len(backend.calls) == 1
    assert "generated-private-provider-marker" not in "".join(
        traceback.format_exception(error.value)
    )


@pytest.mark.parametrize(
    "response", [b"{bad-json", b"{}", TransportTimeout("generated private timeout")]
)
def test_invalid_or_lost_provider_response_remains_unknown(operation, response):
    provider, backend = sdk_provider(operation, [(200, response)])
    with pytest.raises(ProviderUnknown):
        provider.create_organization(operation)
    assert len(backend.calls) == 1


@pytest.mark.parametrize("change", [{"external_id": "ot_" + "f" * 60}, {"id": "foreign/id"}])
def test_foreign_or_invalid_org_observation_is_not_binding_authority(operation, change):
    provider, _ = sdk_provider(operation, [(200, organization(operation, **change))])
    with pytest.raises(ProviderUnknown):
        provider.create_organization(operation)


def test_wrong_environment_refuses_mutations_before_sdk_io(operation):
    provider, backend = sdk_provider(operation, [])
    foreign = replace(operation, provider_environment="env_other", provider_org_id="org_generated")
    for call in (
        provider.create_organization,
        provider.create_membership,
        provider.find_membership,
    ):
        with pytest.raises(ProviderUnknown):
            call(foreign)
    assert not backend.calls


def test_inventory_absence_is_observed_without_a_create_request(operation):
    provider, backend = sdk_provider(operation, [(404, {"message": "not found"})])
    assert provider.find_organization(operation) is None
    assert len(backend.calls) == 1 and backend.calls[0][0].lower() == "get"


@pytest.mark.parametrize(
    "changes,expected",
    [({}, True), ({"email_verified": False}, False), ({"id": "user_foreign"}, None)],
)
def test_current_verified_email_requires_exact_provider_subject(operation, changes, expected):
    provider, backend = sdk_provider(operation, [(200, user(**changes))])
    if expected is None:
        with pytest.raises(SetupError, match="IDENTITY_VERIFICATION_UNAVAILABLE"):
            provider.email_verified("user_a")
    else:
        assert provider.email_verified("user_a") is expected
    assert len(backend.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"user_id": "other"},
        {"organization_id": "other"},
        {"status": "unexpected"},
        {"id": "invalid/id"},
    ],
)
def test_foreign_or_malformed_membership_is_not_accepted(operation, changes):
    provider, _ = sdk_provider(operation, [(200, membership(**changes))])
    with pytest.raises(ProviderUnknown):
        provider.create_membership(replace(operation, provider_org_id="org_generated"))


@pytest.mark.parametrize("status", ["pending", "inactive"])
def test_nonactive_membership_observation_remains_nonactive_for_saga_decision(operation, status):
    provider, _ = sdk_provider(operation, [(200, membership(status=status))])
    assert (
        provider.create_membership(replace(operation, provider_org_id="org_generated")).status
        == status
    )


@pytest.mark.parametrize(
    "variant", ["multiple", "empty_with_cursor", "repeated_cursor", "multiple_pages"]
)
def test_incomplete_or_ambiguous_membership_inventory_never_means_absence(operation, variant):
    op = replace(operation, provider_org_id="org_generated")
    member = SimpleNamespace(
        id="member_generated", user_id="user_a", organization_id="org_generated", status="active"
    )
    calls = []

    def page(**kwargs):
        calls.append(kwargs)
        if variant == "multiple":
            return SimpleNamespace(data=[member, member], after=None)
        if variant == "empty_with_cursor":
            return SimpleNamespace(data=[], after="cursor_generated")
        return SimpleNamespace(
            data=[member],
            after="cursor_generated" if variant == "repeated_cursor" else f"cursor_{len(calls)}",
        )

    client = SimpleNamespace(
        organization_membership=SimpleNamespace(list_organization_memberships=page)
    )
    provider = WorkOSProvisioningProvider(
        client, "env_generated", "member", clock=lambda: op.updated_at
    )
    with pytest.raises(ProviderUnknown):
        provider.find_membership(op)
    assert 1 <= len(calls) <= 10
    assert all(call["request_options"]["max_retries"] == 0 for call in calls)


class Response:
    def __init__(self, data=b"{}", status=200, encoding="identity"):
        self.data, self.status, self.headers = data, status, {"Content-Encoding": encoding}
        self.closed = False
        self.read_limits = []

    def getcode(self):
        return self.status

    def read(self, limit):
        self.read_limits.append(limit)
        return self.data[:limit]

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True


class Opener:
    def __init__(self, response, release=None):
        self.response, self.release, self.calls = response, release, []

    def open(self, request, timeout):
        self.calls.append(request)
        if self.release:
            assert self.release.wait(1), "Test must release its controlled worker"
        return self.response


@pytest.mark.parametrize(
    "method,url,body",
    [
        ("POST", "http://api.workos.com/organizations", b"{}"),
        ("POST", "https://api.workos.com.evil.invalid/organizations", b"{}"),
        ("POST", "https://api.workos.com/organizations?redirect=other", b"{}"),
        ("DELETE", "https://api.workos.com/organizations/org_generated", None),
        ("GET", "https://api.workos.com/user_management/users/user_generated?token=secret", None),
        ("GET", "https://api.workos.com/user_management/users/user_generated", b"{}"),
        (
            "GET",
            "https://api.workos.com/user_management/organization_memberships?organization_id=org_generated&user_id=user_a&statuses=active,inactive,pending&limit=100&order=asc",
            None,
        ),
        (
            "GET",
            "https://api.workos.com/user_management/organization_memberships?organization_id=org_generated&user_id=user_a&statuses=active,inactive,pending&limit=100&order=desc&organization_id=foreign",
            None,
        ),
        (
            "POST",
            "https://api.workos.com/organizations",
            b'{"name":"Generated","external_id":"ot_bad","domains":["example.invalid"]}',
        ),
        (
            "POST",
            "https://api.workos.com/user_management/organization_memberships",
            b'{"user_id":"u","organization_id":"o","role_slug":"member","status":"active"}',
        ),
    ],
)
def test_transport_refuses_unregistered_origins_routes_methods_and_payloads(method, url, body):
    opener = Opener(Response())
    backend = ProvisioningHTTPBackend(opener)
    with pytest.raises(TransportError):
        backend.request(method, url, headers={}, content=body, timeout=1)
    assert not opener.calls and backend.close_complete()


@pytest.mark.parametrize(
    "status,encoding,data",
    [(302, "identity", b"{}"), (200, "gzip", b"{}"), (200, "identity", b"x" * 1_048_577)],
)
def test_transport_refuses_redirect_compression_and_oversize_closes_response(
    status, encoding, data
):
    response = Response(data, status, encoding)
    backend = ProvisioningHTTPBackend(Opener(response))
    with pytest.raises(TransportError):
        backend.request(
            "GET",
            "https://api.workos.com/user_management/users/user_generated",
            headers={},
            content=None,
            timeout=1,
        )
    assert response.closed and backend.close_complete()
    assert not response.read_limits or response.read_limits == [1_048_577]


def test_timeout_retains_worker_ownership_and_never_enables_second_request():
    release = threading.Event()
    opener = Opener(Response(), release)
    backend = ProvisioningHTTPBackend(opener, wait_seconds=0.02)
    try:
        with pytest.raises(TransportTimeout):
            backend.request(
                "GET",
                "https://api.workos.com/user_management/users/user_generated",
                headers={},
                content=None,
                timeout=1,
            )
        with pytest.raises(TransportTimeout):
            backend.request(
                "GET",
                "https://api.workos.com/user_management/users/user_generated",
                headers={},
                content=None,
                timeout=1,
            )
        assert len(opener.calls) == 1
        assert backend.close_complete(timeout=0) is False
    finally:
        release.set()
        assert backend.close_complete(timeout=0.02)
    assert opener.response.closed


def test_completed_close_cannot_be_followed_by_an_unpublished_worker_dispatch(monkeypatch):
    original_thread = threading.Thread
    constructing, release = threading.Event(), threading.Event()
    opener = Opener(Response())
    backend = ProvisioningHTTPBackend(opener)
    outcomes = []

    def paused_thread(*args, **kwargs):
        constructing.set()
        assert release.wait(2)
        return original_thread(*args, **kwargs)

    def request():
        try:
            backend.request(
                "GET",
                "https://api.workos.com/user_management/users/user_generated",
                headers={},
                content=None,
                timeout=1,
            )
            outcomes.append("returned")
        except TransportError:
            outcomes.append("refused")

    monkeypatch.setattr(threading, "Thread", paused_thread)
    caller = original_thread(target=request)
    caller.start()
    try:
        assert constructing.wait(1)
        assert backend.close_complete(timeout=0.01) is True
    finally:
        release.set()
        caller.join(2)
    assert not caller.is_alive() and outcomes == ["refused"] and opener.calls == []
    assert backend.close_complete()


def test_allowed_sdk_requests_flow_through_the_actual_bounded_backend(operation):
    response = Response(json.dumps(organization(operation)).encode())
    opener = Opener(response)
    backend = ProvisioningHTTPBackend(opener)
    client = WorkOSClient(
        api_key="generated_key", client_id="client_generated", http_client=backend
    )
    provider = WorkOSProvisioningProvider(client, "env_generated", "member")
    try:
        assert provider.create_organization(operation).organization_id == "org_generated"
        assert len(opener.calls) == 1
        assert json.loads(opener.calls[0].data) == {
            "name": operation.display_name,
            "external_id": operation.correlation,
        }
    finally:
        assert backend.close_complete()
    assert response.closed


@pytest.mark.parametrize("action", ["email", "organization_lookup", "membership_inventory"])
def test_actual_sdk_read_contracts_pass_the_registered_transport_and_parse_exact_results(
    operation, action
):
    body = user() if action == "email" else organization(operation)
    if action == "membership_inventory":
        body = {
            "object": "list",
            "data": [membership()],
            "list_metadata": {"before": None, "after": None},
        }
    response = Response(json.dumps(body).encode())
    opener = Opener(response)
    backend = ProvisioningHTTPBackend(opener)
    client = WorkOSClient(
        api_key="generated_key", client_id="client_generated", http_client=backend
    )
    provider = WorkOSProvisioningProvider(
        client, "env_generated", "member", clock=lambda: operation.updated_at
    )
    try:
        if action == "email":
            assert provider.email_verified("user_a") is True
        elif action == "organization_lookup":
            assert provider.find_organization(operation).organization_id == "org_generated"
        else:
            result = provider.find_membership(replace(operation, provider_org_id="org_generated"))
            assert result is not None and result.membership_id == "member_generated"
        assert len(opener.calls) == 1 and opener.calls[0].method == "GET"
    finally:
        assert backend.close_complete()
    assert response.closed
