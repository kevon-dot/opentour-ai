"""Checks for the unpublished control session OpenAPI registration."""

from collections.abc import Iterator
from typing import Any
from unittest.mock import Mock

import pytest
from cryptography.fernet import Fernet
from fastapi.openapi.models import OpenAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from opentour_control.api.schema import create_control_api
from opentour_control.auth import workos_provider
from opentour_control.auth.config import IdentityConfig
from opentour_control.auth.runtime import ControlAuthRuntime, create_control_auth_runtime

from tools.schema_validate import schema_registry

_CONTEXT = "/v1/session/context"
_SELECTION = "/v1/session:select-organization"
_VIEW = "urn:opentour:auth-context:1.0#/$defs/SessionContextView"
_ERROR = "urn:opentour:common-contract:1.0#/$defs/Error"


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> Iterator[ControlAuthRuntime]:
    forbidden = Mock(side_effect=AssertionError("This check must stay offline"))
    monkeypatch.setattr(workos_provider, "fetch_jwks", forbidden)
    monkeypatch.setattr(workos_provider.BoundedWorkOSBackend, "request", forbidden)
    runtime = create_control_auth_runtime(
        config=IdentityConfig(
            issuer="https://identity.example/",
            audience="https://control.example/",
            jwks_url="https://identity.example/jwks",
            client_id="generated-client",
            credential_max_lifetime_seconds=600,
        ),
        workos_api_key="generated-workos-key",
        sealing_keys=(Fernet.generate_key(),),
        csrf_key=b"f" * 32,
        identity_connect=forbidden,
        selection_connect=forbidden,
    )
    try:
        yield runtime
    finally:
        assert runtime.close()
        forbidden.assert_not_called()


def _schema(response: dict[str, Any]) -> dict[str, Any]:
    schema: dict[str, Any] = response["content"]["application/json"]["schema"]
    return schema


def test_session_registration_declares_two_control_routes_with_combined_security(
    runtime: ControlAuthRuntime,
) -> None:
    document = create_control_api(runtime).openapi()
    OpenAPI.model_validate(document)
    paths = document["paths"]
    assert {path: set(operations) for path, operations in paths.items()} == {
        _CONTEXT: {"get"},
        _SELECTION: {"post"},
    }
    assert document["components"]["securitySchemes"] == {
        "HTTPBearer": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        "APIKeyCookie": {"type": "apiKey", "in": "cookie", "name": "opentour-session"},
    }
    get, post = paths[_CONTEXT]["get"], paths[_SELECTION]["post"]
    for operation in (get, post):
        assert operation["x-opentour-plane"] == "control"
        assert operation["security"] == [{"HTTPBearer": [], "APIKeyCookie": []}]
        assert "operationId" not in operation and "x-opentour-capability" not in operation
    assert "requestBody" not in get and "parameters" not in get
    assert set(get["responses"]) == {"200", "401", "403", "409", "503"}
    assert set(post["responses"]) == {"200", "401", "403", "404", "409", "422", "503"}
    assert _schema(get["responses"]["200"]) == {"$ref": _VIEW}
    for operation in (get, post):
        for status, response in operation["responses"].items():
            if status != "200":
                assert _schema(response) == {"$ref": _ERROR}
            assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"
            assert response["headers"]["Vary"]["schema"]["const"] == "Authorization, Cookie"


def test_selection_schema_keeps_closed_request_and_active_response(
    runtime: ControlAuthRuntime,
) -> None:
    post = create_control_api(runtime).openapi()["paths"][_SELECTION]["post"]
    registry = schema_registry()
    assert post["requestBody"]["required"] is True
    request = Draft202012Validator(_schema(post["requestBody"]), registry=registry)
    data = {"organization_id": "organization", "expected_session_generation": 9_007_199_254_740_991}
    assert request.is_valid(data)
    assert not request.is_valid({**data, "workspace_id": "unapproved"})
    assert not request.is_valid({**data, "organization_id": ""})
    assert not request.is_valid({**data, "expected_session_generation": 9_007_199_254_740_992})
    assert not request.is_valid({**data, "expected_session_generation": "1"})
    assert not request.is_valid({**data, "expected_session_generation": True})

    (key,) = post["parameters"]
    assert (key["name"], key["in"], key["required"]) == ("Idempotency-Key", "header", True)
    header = Draft202012Validator(key["schema"])
    assert header.is_valid("!" + "~" * 255)
    for invalid in ("", "key\n", "key with space", "a" * 257):
        assert not header.is_valid(invalid)

    response = Draft202012Validator(_schema(post["responses"]["200"]), registry=registry)
    active = {
        "state": "active",
        "organization_id": "organization",
        "membership_id": "membership",
        "session_generation": 1,
        "request_id": "request",
    }
    assert response.is_valid(active)
    unselected = dict(active, state="unselected", organization_id=None, membership_id=None)
    assert not response.is_valid(unselected)
    assert not response.is_valid(dict(active, membership_id=None))


def test_mounted_handlers_still_deny_missing_credentials_with_canonical_error(
    runtime: ControlAuthRuntime,
) -> None:
    app = create_control_api(runtime)
    registry = schema_registry()
    with TestClient(app) as client:
        for method, path in (("GET", _CONTEXT), ("POST", _SELECTION)):
            response = client.request(method, path)
            assert (response.status_code, response.json()["code"]) == (401, "SESSION_INVALID")
            operation = app.openapi()["paths"][path][method.lower()]
            error = Draft202012Validator(_schema(operation["responses"]["401"]), registry=registry)
            assert error.is_valid(response.json())
            assert response.headers["Cache-Control"] == "no-store"
            assert response.headers["Vary"] == "Authorization, Cookie"
        assert client.get("/openapi.json").status_code == 404


@pytest.mark.parametrize(
    ("path", "include_in_schema"),
    [
        ("/unexpected-generated-path", True),
        ("/unexpected-generated-path", False),
        (_CONTEXT, True),
        (_CONTEXT, False),
    ],
    ids=["extra", "hidden-extra", "duplicate", "hidden-duplicate"],
)
def test_unregistered_runtime_route_refuses_control_app_composition(
    runtime: ControlAuthRuntime,
    path: str,
    include_in_schema: bool,
) -> None:
    @runtime.router.get(path, include_in_schema=include_in_schema)
    def unexpected() -> None:
        pass

    with pytest.raises(ValueError, match="do not match their registration"):
        create_control_api(runtime)
