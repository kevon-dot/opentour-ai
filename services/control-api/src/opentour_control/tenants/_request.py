"""Shared request admission for the two protected OPE-172 operations."""

from __future__ import annotations

import json
import re

from fastapi import Request

from opentour_control.auth.config import AuthError


def credentials(request: Request) -> tuple[str, str]:
    authorization = request.headers.getlist("authorization")
    bindings = [
        cookie.partition("=")[2].strip()
        for header in request.headers.getlist("cookie")
        for cookie in header.split(";")
        if cookie.partition("=")[0].strip() == "opentour-session"
    ]
    if (
        len(authorization) != 1
        or not authorization[0].startswith("Bearer ")
        or not authorization[0][7:]
        or len(bindings) != 1
        or not re.fullmatch(r"[A-Za-z0-9_-]{32,128}", bindings[0])
    ):
        raise AuthError("SESSION_INVALID")
    return authorization[0][7:], bindings[0]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value = dict(pairs)
    if len(value) != len(pairs):
        raise AuthError("SCHEMA_INVALID", 422)
    return value


async def json_body(request: Request) -> object:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 16_384:
            raise AuthError("SCHEMA_INVALID", 422)
        raw.extend(chunk)
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object)
    except (ValueError, RecursionError):
        raise AuthError("SCHEMA_INVALID", 422) from None
