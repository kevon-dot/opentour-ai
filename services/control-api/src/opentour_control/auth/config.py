"""Explicit nonsecret identity profile and safe errors. No permissive defaults."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken, MultiFernet

MAX_COUNTER = 9_007_199_254_740_991


class AuthError(Exception):
    def __init__(self, code: str, status: int = 401, retryable: bool = False) -> None:
        super().__init__(code)
        self.code, self.status, self.retryable = code, status, retryable

    def wire(self, request_id: str) -> dict[str, object]:
        return {
            "code": self.code,
            "message": "The identity operation could not be completed.",
            "request_id": identifier(request_id),
            "retryable": self.retryable,
            "field_errors": [],
            "current_revision": None,
        }


def identifier(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 256:
        raise AuthError("SCHEMA_INVALID", 422)
    return value


def counter(value: int) -> int:
    if type(value) is not int or not 0 <= value <= MAX_COUNTER:
        raise AuthError("SCHEMA_INVALID", 422)
    return value


def utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise AuthError("IDENTITY_CLOCK_UNAVAILABLE", 503)
    return value.astimezone(UTC)


def timestamp(value: datetime) -> str:
    return utc(value).isoformat().replace("+00:00", "Z")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ValueError("Identity profile requires an explicit trusted HTTPS URL")
    return value


class FernetSealingPort:
    """Maintained authenticated encryption with an explicitly injected server key ring.

    First key writes; remaining keys permit bounded rotation. Scope and absolute
    expiry are encrypted/authenticated with the payload. Ciphertext is never authority.
    Clock inputs belong to server construction, never a request parameter.
    """

    def __init__(self, keys: tuple[bytes, ...], max_lifetime_seconds: int) -> None:
        if not 1 <= len(keys) <= 4 or not 1 <= max_lifetime_seconds <= 2_147_483_647:
            raise ValueError("Explicit bounded sealing configuration required")
        self._fernet = MultiFernet([Fernet(key) for key in keys])
        self.max_lifetime_seconds = max_lifetime_seconds

    def seal(self, payload: str, scope: str, now: datetime, expires: datetime) -> str:
        now, expires = utc(now), utc(expires)
        if (
            not isinstance(payload, str)
            or len(payload.encode()) > 32_768
            or not 1 <= len(scope) <= 2048
            or not 0 < (expires - now).total_seconds() <= self.max_lifetime_seconds
        ):
            raise AuthError("SESSION_SEAL_INVALID", 422)
        document = json.dumps(
            {
                "v": 1,
                "scope": scope,
                "created": now.timestamp(),
                "expires": expires.timestamp(),
                "payload": payload,
            },
            separators=(",", ":"),
            allow_nan=False,
        ).encode()
        return self._fernet.encrypt_at_time(document, int(now.timestamp())).decode("ascii")

    def open(self, sealed: str, scope: str, now: datetime) -> str:
        try:
            if not isinstance(sealed, str) or len(sealed) > 65_536:
                raise ValueError()
            document = json.loads(
                self._fernet.decrypt_at_time(
                    sealed,
                    self.max_lifetime_seconds,
                    int(utc(now).timestamp()),
                )
            )
            if (
                set(document) != {"v", "scope", "created", "expires", "payload"}
                or document["v"] != 1
                or document["scope"] != scope
                or not isinstance(document["expires"], (int, float))
                or not isinstance(document["created"], (int, float))
                or now.timestamp() < document["created"]
                or now.timestamp() >= document["expires"]
                or not isinstance(document["payload"], str)
            ):
                raise ValueError()
            return document["payload"]
        except (InvalidToken, ValueError, TypeError, UnicodeError):
            raise AuthError("SESSION_CREDENTIALS_UNAVAILABLE", 503) from None


@dataclass(frozen=True)
class IdentityConfig:
    issuer: str
    audience: str
    jwks_url: str
    client_id: str
    credential_max_lifetime_seconds: int
    jwks_timeout_seconds: float = 5.0
    jwks_cache_seconds: int = 300
    jwks_refresh_cooldown_seconds: int = 30
    sensitive_freshness_seconds: int = 30
    metadata_freshness_seconds: int = 300
    poll_seconds: int = 30

    def __post_init__(self) -> None:
        for url in (self.issuer, self.audience, self.jwks_url):
            https_url(url)
        identifier(self.client_id)
        if (
            type(self.credential_max_lifetime_seconds) is not int
            or not 1 <= self.credential_max_lifetime_seconds <= 2_147_483_647
        ):
            raise ValueError("Configure maximum lifetime of all session credentials")
        if not 0 < self.jwks_timeout_seconds <= 10 or not 1 <= self.jwks_cache_seconds <= 300:
            raise ValueError("JWKS retrieval must be bounded")
        if self.jwks_refresh_cooldown_seconds != 30:
            raise ValueError("Key refresh admission uses the reviewed30second cooldown")
        if (
            self.sensitive_freshness_seconds != 30
            or self.metadata_freshness_seconds != 300
            or self.poll_seconds != 30
        ):
            raise ValueError("Identity freshness must match the reviewed policy")
