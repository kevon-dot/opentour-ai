"""Real signature checks with generated keys; not a live WorkOS qualification."""

import json
import threading
import time
import traceback
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import jwt
import pytest
from jsonschema import Draft202012Validator
from opentour_control.auth.config import AuthError
from opentour_control.auth.workos_provider import WorkOSMembershipProvider
from referencing import Registry, Resource


@pytest.mark.parametrize(
    "field,value",
    [
        ("iss", "https://foreign.invalid/"),
        ("aud", "foreign"),
        ("aud", None),
        ("aud", ["https://api.example.invalid"]),
        ("sub", {}),
        ("sid", ""),
        ("auth_time", None),
        ("auth_time", True),
        ("exp", 0),
        ("sub_profile", "ai_agent"),
        ("sub_type", "widget"),
    ],
)
def test_invalid_claims_deny_before_local_lookup(identity, field, value):
    calls = identity.repository.load_calls
    with pytest.raises(AuthError) as failure:
        identity.service.authorization_context(identity.token(**{field: value}), identity.handle)
    assert failure.value.status == 401
    assert identity.repository.load_calls == calls


def test_exact_expiry_and_client_clock_cannot_extend(identity):
    now = int(identity.now.timestamp())
    with pytest.raises(AuthError):
        identity.verifier.verify(identity.token(exp=now), identity.now)
    assert identity.verifier.verify(identity.token(exp=now + 1), identity.now).subject == "user_a"
    with pytest.raises(AuthError):
        identity.verifier.verify(identity.token(iat=now + 1), identity.now)


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
def test_algorithm_substitution(identity, algorithm):
    raw = jwt.encode(
        {"sub": "user_a"},
        "generated-hmac-fixture-at-least-32-bytes" if algorithm == "HS256" else None,
        algorithm=algorithm,
        headers={"kid": "key-a"},
    )
    with pytest.raises(AuthError):
        identity.verifier.verify(raw, identity.now)


def test_signature_tamper(identity):
    header, payload, signature = identity.token().split(".")
    tampered = ("a" if signature[0] != "a" else "b") + signature[1:]
    with pytest.raises(AuthError):
        identity.verifier.verify(f"{header}.{payload}.{tampered}", identity.now)


def test_unknown_key_refresh_is_bounded(identity):
    calls = len(identity.fetches)
    identity.verifier.last_attempt = -1
    token = jwt.encode(
        {"sub": "user_a"}, identity.key, algorithm="RS256", headers={"kid": "unknown"}
    )
    with pytest.raises(AuthError):
        identity.verifier.verify(token, identity.now)
    assert len(identity.fetches) == calls + 1
    with pytest.raises(AuthError):
        identity.verifier.verify(token, identity.now)
    assert len(identity.fetches) == calls + 1


@pytest.mark.parametrize("field", ["jku", "jwk", "x5u"])
def test_token_cannot_choose_key_endpoint(identity, field):
    token = jwt.encode(
        {},
        identity.key,
        algorithm="RS256",
        headers={"kid": "key-a", field: "https://foreign.invalid/key"},
    )
    calls = len(identity.fetches)
    with pytest.raises(AuthError):
        identity.verifier.verify(token, identity.now)
    assert len(identity.fetches) == calls


def test_duplicate_jwks_key_id_is_not_ambiguous_allow(identity):
    identity.keys["keys"].append(identity.public)
    identity.verifier.snapshot = None
    identity.verifier.last_attempt = -1
    with pytest.raises(AuthError, match="IDENTITY_VERIFICATION_UNAVAILABLE"):
        identity.verifier.verify(identity.token(), identity.now)


def test_slow_key_refresh_never_queues_or_admits_late_result(identity):
    from opentour_control.auth.workos_provider import WorkOSTokenVerifier

    started, release = threading.Event(), threading.Event()
    calls = []

    def slow():
        calls.append(True)
        started.set()
        release.wait(2)
        return identity.keys

    verifier = WorkOSTokenVerifier(
        replace(identity.config, jwks_timeout_seconds=0.05), fetch=slow, clock=lambda: identity.now
    )
    first = []

    def attempt():
        try:
            verifier.verify(identity.token(), identity.now)
        except AuthError as error:
            first.append(error.code)

    thread = threading.Thread(target=attempt)
    thread.start()
    try:
        assert started.wait(1)
        before = time.monotonic()
        with pytest.raises(AuthError, match="IDENTITY_VERIFICATION_UNAVAILABLE"):
            verifier.verify(identity.token(), identity.now)
        assert time.monotonic() - before < 0.2
        thread.join(0.5)
        assert first == ["IDENTITY_VERIFICATION_UNAVAILABLE"] and len(calls) == 1
        assert not verifier.close(0)
    finally:
        release.set()
        thread.join(1)
        assert verifier.close(1)
    assert verifier.loaded_at is None


def test_scoped_context_schema_and_unknown_assurance(identity):
    root = Path(__file__).resolve().parents[4]
    common = json.loads(
        (root / "packages/contracts/common/common-contract.schema.json").read_text()
    )
    schema = json.loads((root / "packages/contracts/platform/auth-context.schema.json").read_text())
    registry = Registry().with_resource(common["$id"], Resource.from_contents(common))
    validator = Draft202012Validator(schema, registry=registry)
    context = identity.service.authorization_context(identity.token(), identity.handle).wire()
    validator.validate(context)
    assert context["assurance_level"] == "unknown" and context["delegated_grant_id"] is None
    context["assurance_level"] = "aal2"
    assert list(validator.iter_errors(context))
    context["assurance_level"] = "unknown"
    context["actor"]["delegated_grant_id"] = "forged"
    assert list(validator.iter_errors(context))


def test_unknown_refresh_cannot_deny_current_cached_key(identity):
    started, release = threading.Event(), threading.Event()
    verifier = identity.verifier
    verifier.config = replace(identity.config, jwks_timeout_seconds=0.1)
    verifier.last_attempt = -1

    def slow():
        started.set()
        release.wait(1)
        return identity.keys

    verifier.fetch = slow
    unknown = jwt.encode({}, identity.key, algorithm="RS256", headers={"kid": "unknown"})
    failures = []

    def request_unknown():
        try:
            verifier.verify(unknown, identity.now)
        except AuthError as error:
            failures.append(error.code)

    thread = threading.Thread(target=request_unknown)
    thread.start()
    try:
        assert started.wait(1)
        assert verifier.verify(identity.token(), identity.now).subject == "user_a"
        assert verifier.snapshot is not None
        keys, loaded = verifier.snapshot
        verifier.snapshot = keys, loaded - identity.config.jwks_cache_seconds
        with pytest.raises(AuthError, match="IDENTITY_VERIFICATION_UNAVAILABLE"):
            verifier.verify(identity.token(), identity.now)
    finally:
        release.set()
        thread.join(1)
        assert verifier.close(1)
    assert failures


@pytest.mark.parametrize(
    "sensitive,age,allowed",
    [(True, 30, True), (True, 31, False), (False, 300, True), (False, 301, False)],
)
def test_membership_freshness_boundary(identity, sensitive, age, allowed):
    previous = identity.repository.observation(identity.issuer, "provider_a")
    identity.repository.observations[(identity.issuer, "provider_a")] = replace(
        previous, checked_at=identity.now - timedelta(seconds=age)
    )

    def outage(*args):
        raise AuthError("IDENTITY_FRESHNESS_UNAVAILABLE", 503, True)

    identity.provider.current_membership = outage
    if allowed:
        identity.service.authorization_context(
            identity.token(), identity.handle, sensitive=sensitive
        )
    else:
        with pytest.raises(AuthError, match="IDENTITY_FRESHNESS_UNAVAILABLE"):
            identity.service.authorization_context(
                identity.token(), identity.handle, sensitive=sensitive
            )
    assert not identity.repository.denials


def test_provider_role_and_email_do_not_grant(identity):
    identity.members.clear()
    with pytest.raises(AuthError, match="MEMBERSHIP_INACTIVE"):
        identity.service.authorization_context(
            identity.token(role="owner", email="owner@example.invalid"), identity.handle
        )


def test_sdk_fault_is_sanitized_in_formatted_traceback(identity):
    sentinel = "GENERATED_REFRESH_CREDENTIAL_SENTINEL"

    def fail(*args, **kwargs):
        raise RuntimeError(sentinel)

    provider = WorkOSMembershipProvider(
        SimpleNamespace(organization_membership=SimpleNamespace(get_organization_membership=fail)),
        identity.config,
    )
    try:
        provider.current_membership("provider_a", identity.now)
    except AuthError as error:
        assert sentinel not in "".join(traceback.format_exception(error))
        assert sentinel not in json.dumps(error.wire("request_fixture"))
    else:
        pytest.fail("SDK failure was accepted")


@pytest.mark.parametrize("shift", [2, -2])
def test_key_lookup_expiry_and_backward_clock_fail_closed(identity, shift):
    identity.verifier.snapshot = None
    identity.verifier.last_attempt = -1
    identity.verifier.clock = lambda: identity.now + timedelta(seconds=shift)
    with pytest.raises(AuthError):
        identity.verifier.verify(
            identity.token(exp=int(identity.now.timestamp()) + 1), identity.now
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("unexpected", True),
        ("session_generation", True),
        ("session_generation", 9_007_199_254_740_992),
        ("session_id", ""),
        ("identity_checked_at", "2026-11-15T12:00:00-03:00"),
        ("delegated_grant_id", "forged-grant"),
    ],
)
def test_closed_context_rejects_malformed_wire_fields(identity, field, value):
    root = Path(__file__).resolve().parents[4]
    common = json.loads(
        (root / "packages/contracts/common/common-contract.schema.json").read_text()
    )
    schema = json.loads((root / "packages/contracts/platform/auth-context.schema.json").read_text())
    validator = Draft202012Validator(
        schema, registry=Registry().with_resource(common["$id"], Resource.from_contents(common))
    )
    context = identity.service.authorization_context(identity.token(), identity.handle).wire()
    context[field] = value
    assert list(validator.iter_errors(context))


def test_missing_or_zero_credential_policy_fails_closed(identity):
    with pytest.raises(ValueError):
        replace(identity.config, credential_max_lifetime_seconds=0)


@pytest.mark.parametrize("status,size", [(302, 0), (200, 1_048_577), (500, 50)])
def test_bounded_sdk_backend_rejects_redirect_and_oversize(identity, status, size):
    import io
    from email.message import Message

    from opentour_control.auth.workos_provider import BoundedWorkOSBackend
    from workos import WorkOSClient

    class Response(io.BytesIO):
        headers = Message()

        def getcode(self):
            return status

    requests = []

    def open_response(request, timeout):
        requests.append(request)
        return Response(b"x" * size)

    backend = BoundedWorkOSBackend(SimpleNamespace(open=open_response))
    sdk = WorkOSClient(
        api_key="generated",
        client_id="client_fixture",
        max_retries=0,
        base_url="https://api.workos.com",
        http_client=backend,
    )
    provider = WorkOSMembershipProvider(sdk, identity.config)
    with pytest.raises(AuthError, match="IDENTITY_FRESHNESS_UNAVAILABLE"):
        provider.current_membership("provider_a", identity.now)
    assert len(requests) == 1 and requests[0].get_header("Accept-encoding") == "identity"
    assert backend.close_complete()


def test_sdk_backend_caller_deadline_and_worker_cleanup_are_distinct():
    from opentour_control.auth.workos_provider import BoundedWorkOSBackend
    from workos import TransportTimeout

    entered, release = threading.Event(), threading.Event()

    def slow(*args, **kwargs):
        entered.set()
        release.wait(1)
        raise OSError("generated private transport sentinel")

    backend = BoundedWorkOSBackend(SimpleNamespace(open=slow), wait_seconds=0.03)
    try:
        before = time.monotonic()
        with pytest.raises(TransportTimeout):
            backend.request(
                "GET", "https://api.workos.com/events", headers={}, content=None, timeout=5
            )
        assert time.monotonic() - before < 0.3 and entered.is_set()
        with pytest.raises(TransportTimeout):
            backend.request(
                "GET", "https://api.workos.com/events", headers={}, content=None, timeout=5
            )
        assert not backend.close_complete(0)
    finally:
        release.set()
        assert backend.close_complete(0.1)
