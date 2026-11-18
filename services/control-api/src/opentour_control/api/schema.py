"""Unpublished OpenAPI registration for the control session routes."""

from __future__ import annotations

from http import HTTPStatus
from typing import Any

from fastapi import FastAPI
from opentour_control.auth.runtime import ControlAuthRuntime

_COMMON = "urn:opentour:common-contract:1.0#/$defs/"
_CONTEXT = "urn:opentour:auth-context:1.0#/$defs/SessionContextView"
_CONTEXT_PATH = "/v1/session/context"
_SELECTION_PATH = "/v1/session:select-organization"
_BEARER = "HTTPBearer"
_COOKIE = "APIKeyCookie"


def _responses(success: dict[str, Any], errors: tuple[int, ...]) -> dict[str, Any]:
    return {
        str(status): {
            "description": HTTPStatus(status).phrase,
            "headers": {
                "Cache-Control": {"schema": {"type": "string", "const": "no-store"}},
                "Vary": {"schema": {"type": "string", "const": "Authorization, Cookie"}},
            },
            "content": {
                "application/json": {
                    "schema": success if status == 200 else {"$ref": _COMMON + "Error"}
                }
            },
        }
        for status in (200, *errors)
    }


def _register_session_operations(document: dict[str, Any]) -> None:
    paths = document["paths"]
    if {path: set(operations) for path, operations in paths.items()} != {
        _CONTEXT_PATH: {"get"},
        _SELECTION_PATH: {"post"},
    }:
        raise ValueError("Control session routes do not match their registration")

    context, selection = paths[_CONTEXT_PATH]["get"], paths[_SELECTION_PATH]["post"]
    document.setdefault("components", {})["securitySchemes"] = {
        _BEARER: {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"},
        _COOKIE: {"type": "apiKey", "in": "cookie", "name": "opentour-session"},
    }
    for operation in (context, selection):
        # Stable IDs, capability and resource-scope registry identifiers are pending.
        operation.pop("operationId", None)
        operation["x-opentour-plane"] = "control"
        operation["security"] = [{_BEARER: [], _COOKIE: []}]

    context["responses"] = _responses({"$ref": _CONTEXT}, (401, 403, 409, 503))
    selection["responses"] = _responses(
        {"allOf": [{"$ref": _CONTEXT}, {"properties": {"state": {"const": "active"}}}]},
        (401, 403, 404, 409, 422, 503),
    )
    selection["parameters"] = [
        {
            "name": "Idempotency-Key",
            "in": "header",
            "required": True,
            "description": "Reuse only for an exact replay in the verified session.",
            "schema": {
                "type": "string",
                "minLength": 1,
                "maxLength": 256,
                "not": {"pattern": "[^!-~]"},
            },
        }
    ]
    selection["requestBody"] = {
        "required": True,
        "description": "The handler rejects duplicate members and non-integer generation syntax.",
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["organization_id", "expected_session_generation"],
                    "properties": {
                        "organization_id": {"$ref": _COMMON + "Id"},
                        "expected_session_generation": {
                            "type": "integer",
                            "minimum": 0,
                            "maximum": 9_007_199_254_740_991,
                        },
                    },
                }
            }
        },
    }


def create_control_api(runtime: ControlAuthRuntime) -> FastAPI:
    """Mount the configured session router. The caller owns its startup and shutdown."""
    app = FastAPI(
        title="OpenTour Control API",
        version="unpublished",
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
    )
    app.include_router(runtime.router)
    # FastAPI caches this document. Register before returning the configured app.
    _register_session_operations(app.openapi())
    return app
