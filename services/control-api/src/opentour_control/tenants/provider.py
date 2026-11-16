"""Bounded WorkOS provisioning. An uncertain mutation is never retried here."""

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
from enum import Enum
from typing import Any, Literal, Protocol, cast
from urllib.parse import parse_qs, urlsplit

from workos import (
    BadRequestError,
    HTTPResponse,
    NotFoundError,
    RequestOptions,
    TransportError,
    TransportTimeout,
    UnprocessableEntityError,
    WorkOSClient,
)
from workos.organization_membership import RoleSingle

from .models import Operation, SetupError, server_time, validate_display_name


def provider_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", value):
        raise SetupError("PROVIDER_RESPONSE_INVALID", 503, retryable=True)
    return value


class ProviderUnknown(Exception):
    """The effect cannot be ruled out. Reconcile the original intent, do not resubmit."""


class ProviderRejected(Exception):
    """An explicit request validation rejection; no automatic new request."""


@dataclass(frozen=True)
class OrganizationObservation:
    organization_id: str
    correlation: str
    environment: str


@dataclass(frozen=True)
class MembershipObservation:
    membership_id: str
    organization_id: str
    subject: str
    status: Literal["active", "inactive", "pending"]
    checked_at: datetime


class ProvisioningProvider(Protocol):
    environment: str

    def email_verified(self, subject: str) -> bool: ...
    def find_organization(self, operation: Operation) -> OrganizationObservation | None: ...
    def create_organization(self, operation: Operation) -> OrganizationObservation: ...
    def find_membership(self, operation: Operation) -> MembershipObservation | None: ...
    def create_membership(self, operation: Operation) -> MembershipObservation: ...


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


class ProvisioningHTTPBackend:
    """One owned worker with bounded bytes and caller time, no arbitrary HTTP surface.

    Timed-out work retains the mutex until its real exit. Closing reports whether
    that worker stopped; no false cancellation permits concurrent resubmission.
    """

    def __init__(self, opener: Any | None = None, wait_seconds: float = 5.0) -> None:
        if not 0 < wait_seconds <= 5:
            raise ValueError("Bounded provisioning transport required")
        self.opener = opener or urllib.request.build_opener(_NoRedirect())
        self.wait_seconds = wait_seconds
        self.lock = threading.Lock()
        self.lifecycle = threading.Lock()
        self.worker: threading.Thread | None = None
        self.closed = False

    @staticmethod
    def _allowed(method: str, url: str, content: bytes | None) -> bool:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.workos.com" or parsed.fragment:
            return False
        path = parsed.path
        if method == "GET":
            if content:
                return False
            if re.fullmatch(r"/user_management/users/[A-Za-z0-9_-]{1,256}", path):
                return not parsed.query
            if re.fullmatch(r"/organizations/external_id/ot_[0-9a-f]{60}", path):
                return not parsed.query
            if path != "/user_management/organization_memberships":
                return False
            query = parse_qs(parsed.query, keep_blank_values=True)
            if not {"organization_id", "user_id", "statuses", "limit"} <= set(query) or set(
                query
            ) - {"organization_id", "user_id", "statuses", "limit", "after", "order"}:
                return False
            if (
                any(len(values) != 1 for values in query.values())
                or query["statuses"] != ["active,inactive,pending"]
                or query["limit"] != ["100"]
            ):
                return False
            if "order" in query and query["order"] != ["desc"]:
                return False
            return all(
                re.fullmatch(r"[A-Za-z0-9_-]{1,256}", query[k][0])
                for k in ("organization_id", "user_id", *(["after"] if "after" in query else []))
            )
        if method != "POST" or parsed.query or content is None or len(content) > 16_384:
            return False
        try:
            body = json.loads(content)
            if path == "/organizations" and set(body) == {"name", "external_id"}:
                return validate_display_name(body["name"]) == body["name"] and bool(
                    re.fullmatch(r"ot_[0-9a-f]{60}", body["external_id"])
                )
            if path == "/user_management/organization_memberships" and set(body) == {
                "user_id",
                "organization_id",
                "role_slug",
            }:
                return all(re.fullmatch(r"[A-Za-z0-9_-]{1,256}", body[k]) for k in body)
        except (ValueError, TypeError, UnicodeError):
            return False
        return False

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        content: bytes | None,
        timeout: float,
    ) -> HTTPResponse:
        if not self._allowed(method.upper(), url, content) or not 0 < timeout:
            raise TransportError("Provisioning transport refused request")
        budget = min(timeout, self.wait_seconds)
        deadline = time.monotonic() + budget
        done = threading.Event()
        result: list[HTTPResponse] = []

        def send() -> None:
            try:
                req = urllib.request.Request(
                    url,
                    data=content,
                    method=method.upper(),
                    headers={**headers, "Accept-Encoding": "identity"},
                )
                try:
                    response = self.opener.open(req, timeout=budget)
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
                pass  # Never expose provider bodies, headers, credential-bearing URLs or errors.
            finally:
                self.lock.release()
                done.set()

        worker = threading.Thread(target=send, name="opentour-provisioning-request", daemon=True)
        # Publish and start the exact owned worker atomically with shutdown
        # admission. Constructing a Thread has no remote side effect and occurs
        # before admission, so a simultaneous close can refuse it truthfully.
        with self.lifecycle:
            if self.closed:
                raise TransportError("Provisioning transport is closed")
            if not self.lock.acquire(blocking=False):
                raise TransportTimeout("Provisioning transport already owns a request")
            self.worker = worker
            try:
                worker.start()
            except Exception:
                self.worker = None
                self.lock.release()
                raise TransportError("Provisioning request did not start") from None
        if not done.wait(budget):
            raise TransportTimeout("Provisioning outcome remains unknown")
        if not result:
            raise TransportError("Provisioning response unavailable")
        return result[0]

    def close_complete(self, timeout: float = 0.1) -> bool:
        self.closed = True
        deadline = time.monotonic() + max(0, min(timeout, self.wait_seconds))
        if not self.lifecycle.acquire(timeout=max(0, deadline - time.monotonic())):
            return False
        try:
            worker = self.worker
        finally:
            self.lifecycle.release()
        if worker:
            worker.join(max(0, deadline - time.monotonic()))
            return not worker.is_alive()
        return True

    def close(self) -> None:
        if not self.close_complete():
            raise TransportTimeout("Provisioning transport shutdown incomplete")


class WorkOSProvisioningProvider:
    def __init__(
        self,
        client: WorkOSClient,
        environment: str,
        role_slug: str,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.client, self.environment = client, provider_id(environment)
        self.role_slug, self.clock = provider_id(role_slug), clock

    @staticmethod
    def _options(deadline: float) -> RequestOptions:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderUnknown()
        return {"max_retries": 0, "timeout": min(5.0, remaining)}

    def email_verified(self, subject: str) -> bool:
        try:
            user = self.client.user_management.get_user(
                provider_id(subject), request_options=self._options(time.monotonic() + 5)
            )
            if user.id != subject or type(user.email_verified) is not bool:
                raise ValueError()
            return user.email_verified
        except Exception:
            raise SetupError("IDENTITY_VERIFICATION_UNAVAILABLE", 503, retryable=True) from None

    def _organization(self, result: Any, op: Operation) -> OrganizationObservation:
        if op.provider_environment != self.environment or result.external_id != op.correlation:
            raise ProviderUnknown()
        return OrganizationObservation(provider_id(result.id), op.correlation, self.environment)

    def find_organization(self, operation: Operation) -> OrganizationObservation | None:
        try:
            found = self.client.organizations.get_organization_by_external_id(
                operation.correlation, request_options=self._options(time.monotonic() + 5)
            )
            return self._organization(found, operation)
        except NotFoundError:
            # Absence never proves that an in-flight create cannot finish.
            return None
        except Exception:
            raise ProviderUnknown() from None

    def create_organization(self, operation: Operation) -> OrganizationObservation:
        if operation.provider_environment != self.environment:
            raise ProviderUnknown()
        try:
            found = self.client.organizations.create_organization(
                name=operation.display_name,
                external_id=operation.correlation,
                request_options=self._options(time.monotonic() + 5),
            )
            return self._organization(found, operation)
        except (BadRequestError, UnprocessableEntityError):
            raise ProviderRejected() from None
        except Exception:
            raise ProviderUnknown() from None

    def _membership(self, result: Any, op: Operation) -> MembershipObservation:
        status = result.status.value if isinstance(result.status, Enum) else result.status
        if (
            op.provider_environment != self.environment
            or result.user_id != op.creator_subject
            or result.organization_id != op.provider_org_id
            or status not in {"active", "inactive", "pending"}
        ):
            raise ProviderUnknown()
        return MembershipObservation(
            provider_id(result.id),
            provider_id(result.organization_id),
            provider_id(result.user_id),
            status,
            server_time(self.clock()),
        )

    def find_membership(self, operation: Operation) -> MembershipObservation | None:
        if operation.provider_org_id is None or operation.provider_environment != self.environment:
            raise ProviderUnknown()
        deadline = time.monotonic() + 5
        cursor: str | None = None
        seen: set[str] = set()
        candidates: list[MembershipObservation] = []
        try:
            for _ in range(10):
                page = self.client.organization_membership.list_organization_memberships(
                    organization_id=provider_id(operation.provider_org_id),
                    user_id=provider_id(operation.creator_subject),
                    statuses=["active", "inactive", "pending"],
                    limit=100,
                    order="desc",
                    after=cursor,
                    request_options=self._options(deadline),
                )
                candidates.extend(self._membership(member, operation) for member in page.data)
                if len(candidates) > 1:
                    raise ProviderUnknown()
                if page.after is None:
                    return candidates[0] if candidates else None
                cursor = provider_id(page.after)
                if cursor in seen or not page.data:
                    raise ProviderUnknown()
                seen.add(cursor)
        except Exception:
            raise ProviderUnknown() from None
        raise ProviderUnknown()

    def create_membership(self, operation: Operation) -> MembershipObservation:
        if operation.provider_org_id is None or operation.provider_environment != self.environment:
            raise ProviderUnknown()
        try:
            found = self.client.organization_membership.create_organization_membership(
                user_id=provider_id(operation.creator_subject),
                organization_id=provider_id(operation.provider_org_id),
                role=RoleSingle(role_slug=self.role_slug),
                request_options=self._options(time.monotonic() + 5),
            )
            return self._membership(found, operation)
        except (BadRequestError, UnprocessableEntityError):
            raise ProviderRejected() from None
        except Exception:
            raise ProviderUnknown() from None


def create_provisioning_provider(
    api_key: str, client_id: str, environment: str, role_slug: str
) -> tuple[WorkOSProvisioningProvider, ProvisioningHTTPBackend]:
    if not api_key or not client_id:
        raise ValueError("Explicit provider configuration required")
    backend = ProvisioningHTTPBackend()
    client = WorkOSClient(
        api_key=api_key,
        client_id=client_id,
        base_url="https://api.workos.com",
        request_timeout=5,
        max_retries=0,
        http_client=backend,
    )
    return WorkOSProvisioningProvider(client, environment, role_slug), backend
