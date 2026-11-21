"""Maintained WorkOS SDK operations with strict OpenTour JWT admission."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any, Protocol, cast
from urllib.parse import urlsplit

import jwt
from workos import HTTPResponse, NotFoundError, TransportError, TransportTimeout, WorkOSClient

from .config import AuthError, IdentityConfig, identifier, utc
from .repository import IdentityStatus, Observation


@dataclass(frozen=True)
class VerifiedToken:
    issuer: str
    subject: str
    session_id: str
    organization_id: str | None
    authentication_time: datetime
    expires_at: datetime


class TokenVerifier(Protocol):
    def verify(self, token: str, now: datetime) -> VerifiedToken: ...


class MembershipProvider(Protocol):
    def current_membership(self, membership_id: str, now: datetime) -> Observation: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class BoundedWorkOSBackend:
    """SDK HTTPBackend for identity GETs: one worker, bounded body and caller wait.

    A timed-out DNS/socket worker remains owned and excludes replacement workers
    until it exits. close_complete reports this fact; this is not hard cancellation.
    """

    def __init__(self, opener: Any | None = None, wait_seconds: float = 5.0) -> None:
        if not 0 < wait_seconds <= 5:
            raise ValueError("Bounded identity transport configuration required")
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.wait_seconds = wait_seconds
        self.lock = threading.Lock()
        self.worker: threading.Thread | None = None
        self.closed = False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        parsed = urlsplit(url)
        if (
            self.closed
            or method != "GET"
            or parsed.scheme != "https"
            or parsed.netloc != "api.workos.com"
            or parsed.fragment
            or content
        ):
            raise TransportError("Identity transport refused request")
        if not self.lock.acquire(blocking=False):
            raise TransportTimeout("Identity transport is already retrieving")
        done = threading.Event()
        result: list[HTTPResponse] = []
        budget = min(self.wait_seconds, timeout)
        deadline = time.monotonic() + budget

        def retrieve() -> None:
            try:
                request = urllib.request.Request(
                    url, method=method, headers={**headers, "Accept-Encoding": "identity"}
                )
                try:
                    response = self.opener.open(request, timeout=budget)
                except urllib.error.HTTPError as error:
                    response = error
                with response:
                    status = response.getcode()
                    if (
                        300 <= status < 400
                        or response.headers.get("Content-Encoding", "identity") != "identity"
                    ):
                        return
                    data = response.read(1_048_577)
                    if len(data) > 1_048_576 or time.monotonic() >= deadline:
                        return
                    result.append(
                        HTTPResponse(
                            status, cast(Mapping[str, str], response.headers), data, method, url
                        )
                    )
            except Exception:
                pass  # Raw URL/body/SDK diagnostics never cross the transport error boundary.
            finally:
                self.lock.release()
                done.set()

        self.worker = threading.Thread(target=retrieve, daemon=True, name="opentour-workos-read")
        self.worker.start()
        if not done.wait(budget):
            raise TransportTimeout("Identity transport deadline exceeded")
        if not result:
            raise TransportError("Identity transport response unavailable")
        return result[0]

    def close_complete(self, timeout: float = 0.1) -> bool:
        self.closed = True
        if self.worker:
            self.worker.join(max(0, min(timeout, self.wait_seconds)))
            return not self.worker.is_alive()
        return True

    def close(self) -> None:
        if not self.close_complete():
            raise TransportTimeout("Identity transport shutdown is incomplete")


def create_workos_client(
    api_key: str, config: IdentityConfig
) -> tuple[WorkOSClient, BoundedWorkOSBackend]:
    """Composition owns both client and backend lifecycle; no environment URL override."""
    if not api_key:
        raise ValueError("Explicit provider credential required")
    backend = BoundedWorkOSBackend()
    client = WorkOSClient(
        api_key=api_key,
        client_id=config.client_id,
        base_url="https://api.workos.com",
        request_timeout=5,
        max_retries=0,
        jwt_issuer=config.issuer,
        http_client=backend,
    )
    return client, backend


def fetch_jwks(config: IdentityConfig) -> dict[str, Any]:
    """One configured HTTPS endpoint; response is bounded, redirects are refused."""
    try:
        opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(config.jwks_url, timeout=config.jwks_timeout_seconds) as response:
            data = response.read(65_537)
        if len(data) > 65_536:
            raise ValueError("JWKS size")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("JWKS object")
        return result
    except (OSError, ValueError, urllib.error.URLError):
        raise AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True) from None


class WorkOSTokenVerifier:
    """PyJWT is the maintained verifier used by WorkOS's Python SDK.

    The SDK's session helper disables audience checks. This adapter requires the
    provider-signed audience template, exact issuer and RS256 before any context.
    The only injectable clock/fetch seams belong to server construction, never HTTP input.
    """

    def __init__(
        self,
        config: IdentityConfig,
        fetch: Callable[[], dict[str, Any]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config = config
        self.fetch = fetch or (lambda: fetch_jwks(config))
        self.monotonic = monotonic
        self.clock = clock
        self.snapshot: tuple[Mapping[str, jwt.PyJWK], float] | None = None
        self.lock = threading.Lock()
        self.last_attempt: float | None = None
        self.last_failure: AuthError | None = None
        self.worker: threading.Thread | None = None

    def _refresh(self, deadline: float) -> None:
        document = self.fetch()
        raw_keys = document.get("keys")
        if not isinstance(raw_keys, list) or not 1 <= len(raw_keys) <= 32:
            raise AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True)
        keys: dict[str, jwt.PyJWK] = {}
        try:
            for value in raw_keys:
                if (
                    not isinstance(value, dict)
                    or value.get("kty") != "RSA"
                    or value.get("alg", "RS256") != "RS256"
                    or value.get("use", "sig") != "sig"
                    or value.get("key_ops", ["verify"]) != ["verify"]
                ):
                    raise ValueError("Unapproved key profile")
                kid = value.get("kid")
                if not isinstance(kid, str) or not 1 <= len(kid) <= 256 or kid in keys:
                    raise ValueError("Ambiguous key ID")
                keys[kid] = jwt.PyJWK.from_dict(value, algorithm="RS256")
        except (ValueError, jwt.PyJWTError):
            raise AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True) from None
        finished = self.monotonic()
        if finished >= deadline:
            raise AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True)
        self.snapshot = MappingProxyType(keys), finished

    @property
    def loaded_at(self) -> float | None:
        return self.snapshot[1] if self.snapshot else None

    def _key(self, kid: str) -> jwt.PyJWK:
        snapshot, now = self.snapshot, self.monotonic()
        if (
            snapshot is not None
            and snapshot[1] <= now
            and now - snapshot[1] < self.config.jwks_cache_seconds
            and kid in snapshot[0]
        ):
            # A still-current immutable snapshot does not wait behind an unknown-kid refresh.
            return snapshot[0][kid]
        # Never queue unauthenticated requests behind network I/O. One worker is
        # retained until it actually exits, including a timed-out DNS/socket call.
        if not self.lock.acquire(blocking=False):
            raise AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True)
        snapshot, now = self.snapshot, self.monotonic()
        fresh = (
            snapshot is not None
            and snapshot[1] <= now
            and now - snapshot[1] < self.config.jwks_cache_seconds
        )
        if fresh and snapshot is not None and kid in snapshot[0]:
            self.lock.release()
            return snapshot[0][kid]
        if (
            self.last_attempt is not None
            and now >= self.last_attempt
            and now - self.last_attempt < self.config.jwks_refresh_cooldown_seconds
        ):
            self.lock.release()
            if not fresh or self.last_failure:
                raise AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True)
            raise AuthError("SESSION_INVALID")
        self.last_attempt, self.last_failure = now, None
        completed = threading.Event()
        deadline = now + self.config.jwks_timeout_seconds

        def refresh() -> None:
            try:
                self._refresh(deadline)
            except Exception:
                self.last_failure = AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True)
            finally:
                self.lock.release()
                completed.set()

        self.worker = threading.Thread(target=refresh, daemon=True, name="opentour-jwks-refresh")
        self.worker.start()
        if not completed.wait(self.config.jwks_timeout_seconds) or self.last_failure:
            raise AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True)
        snapshot = self.snapshot
        key = snapshot[0].get(kid) if snapshot else None
        if key is None:
            raise AuthError("SESSION_INVALID")
        return key

    def close(self, timeout: float = 0.1) -> bool:
        """Report whether the owned retrieval exited; never claim cleanup of a live worker."""
        if self.worker:
            self.worker.join(timeout=max(0, min(timeout, self.config.jwks_timeout_seconds)))
            return not self.worker.is_alive()
        return True

    def verify(self, token: str, now: datetime) -> VerifiedToken:
        if not isinstance(token, str) or not 1 <= len(token) <= 16_384:
            raise AuthError("SESSION_INVALID")
        try:
            header = jwt.get_unverified_header(token)
            kid = header.get("kid")
            if (
                header.get("alg") != "RS256"
                or not isinstance(kid, str)
                or not 1 <= len(kid) <= 256
                or any(k in header for k in ("jku", "jwk", "x5u"))
            ):
                raise AuthError("SESSION_INVALID")
            key = self._key(kid)
            payload = jwt.decode(
                token,
                key.key,
                algorithms=["RS256"],
                issuer=self.config.issuer,
                audience=self.config.audience,
                options={
                    "require": ["iss", "aud", "sub", "sid", "iat", "exp", "auth_time"],
                    "strict_aud": True,
                    "verify_exp": False,
                    "verify_iat": False,
                    "verify_nbf": False,
                },
            )
            # A trusted server clock, never forwarded request timestamps, is used
            # so exact boundary/restart tests exercise the same production check.
            finished = utc(self.clock())
            if finished < utc(now):
                raise AuthError("IDENTITY_CLOCK_UNAVAILABLE", 503)
            seconds = finished.timestamp()
            for name in ("iat", "exp", "auth_time"):
                if type(payload[name]) is not int or not 0 <= payload[name] <= 253_402_300_799:
                    raise AuthError("SESSION_INVALID")
            if (
                seconds >= payload["exp"]
                or payload["iat"] > seconds
                or payload["auth_time"] > payload["iat"]
                or payload["exp"] <= payload["iat"]
            ):
                raise AuthError("SESSION_INVALID")
            if "nbf" in payload and (type(payload["nbf"]) is not int or payload["nbf"] > seconds):
                raise AuthError("SESSION_INVALID")
            if payload.get("sub_profile", "user") != "user" or "sub_type" in payload:
                raise AuthError("SESSION_INVALID")
            organization = payload.get("org_id")
            if organization is not None:
                identifier(organization)
            return VerifiedToken(
                self.config.issuer,
                identifier(payload["sub"]),
                identifier(payload["sid"]),
                organization,
                datetime.fromtimestamp(payload["auth_time"], UTC),
                datetime.fromtimestamp(payload["exp"], UTC),
            )
        except AuthError as error:
            if error.code == "SCHEMA_INVALID":
                raise AuthError("SESSION_INVALID") from None
            raise
        except (jwt.PyJWTError, ValueError, KeyError, TypeError, OverflowError):
            raise AuthError("SESSION_INVALID") from None


class WorkOSMembershipProvider:
    """Current provider identity only. Never derives local product grants from roles."""

    def __init__(self, client: WorkOSClient, config: IdentityConfig) -> None:
        self.client, self.config = client, config

    def verified_recipient_address(self, issuer: str, subject: str) -> str:
        """Read the current address for an already authenticated invitation recipient."""
        if (
            issuer != self.config.issuer
            or not isinstance(subject, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", subject)
        ):
            raise AuthError("SESSION_INVALID")

        try:
            user = self.client.user_management.get_user(
                subject, request_options={"timeout": 5, "max_retries": 0}
            )
            if user.id != subject or type(user.email_verified) is not bool:
                raise ValueError("Invalid provider user")
            if user.email_verified:
                address = user.email
                if (
                    not isinstance(address, str)
                    or len(address) > 254
                    or not address.isprintable()
                    or address != address.lower()
                    or not re.fullmatch(r"[^@\s]+@[^@\s]+", address)
                ):
                    raise ValueError("Invalid canonical provider address")
                return address
        except NotFoundError:
            pass
        except Exception:
            raise AuthError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, True) from None

        raise AuthError("EMAIL_VERIFICATION_REQUIRED", 403)

    def current_membership(self, membership_id: str, now: datetime) -> Observation:
        try:
            member = self.client.organization_membership.get_organization_membership(
                identifier(membership_id),
                request_options={"timeout": 5, "max_retries": 0},
            )
            status_map: dict[str, IdentityStatus] = {
                "active": "active",
                "inactive": "removed",
                "pending": "pending_identity",
            }
            status = status_map.get(member.status.value, "reconciliation_unknown")
            return Observation(
                self.config.issuer,
                identifier(member.id),
                identifier(member.organization_id),
                identifier(member.user_id),
                status,
                utc(now),
                utc(now),
                status == "removed",
            )
        except NotFoundError:
            raise AuthError("PROVIDER_MEMBERSHIP_REMOVED", 404) from None
        except AuthError:
            raise
        except Exception:
            # Do not expose SDK messages, response bodies, keys or customer identifiers.
            raise AuthError("IDENTITY_FRESHNESS_UNAVAILABLE", 503, True) from None
