"""Bounded Events API reconciliation. Webhooks only wake this durable cursor consumer."""

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from workos import WorkOSClient

from .config import AuthError, IdentityConfig, identifier, utc
from .repository import IdentityEvent, IdentityRepository, Observation, SessionRevocationEvent
from .workos_provider import MembershipProvider

EVENT_TYPES = (
    "organization_membership.created",
    "organization_membership.updated",
    "organization_membership.deleted",
    "session.revoked",
)


@dataclass(frozen=True)
class ProviderEvent:
    event_id: str
    kind: str
    body_sha256: str
    resource_id: str
    subject: str
    organization_id: str | None


class EventProvider(Protocol):
    def page(self, after: str | None) -> Sequence[ProviderEvent]: ...


class WorkOSEventProvider:
    def __init__(self, client: WorkOSClient) -> None:
        self.client = client

    def page(self, after: str | None) -> Sequence[ProviderEvent]:
        try:
            page = self.client.events.list_events(
                after=after,
                limit=100,
                events=list(EVENT_TYPES),
                request_options={"timeout": 5, "max_retries": 0},
            )
            result: list[ProviderEvent] = []
            # One page only. No auto-paging that silently grows this transaction.
            for event in page.data:
                raw: dict[str, Any] = event.to_dict()
                body = json.dumps(
                    raw, sort_keys=True, separators=(",", ":"), allow_nan=False
                ).encode()
                if len(body) > 65_536 or raw["event"] not in EVENT_TYPES:
                    raise ValueError("Event contract")
                data = raw["data"]
                organization = data.get("organization_id")
                if organization is not None:
                    identifier(organization)
                if raw["event"] != "session.revoked" and organization is None:
                    raise ValueError("Missing membership scope")
                result.append(
                    ProviderEvent(
                        identifier(raw["id"]),
                        raw["event"],
                        hashlib.sha256(body).hexdigest(),
                        identifier(data["id"]),
                        identifier(data["user_id"]),
                        organization,
                    )
                )
            if len(result) > 100:
                raise ValueError("Event page limit")
            return result
        except Exception:
            raise AuthError("IDENTITY_EVENTS_UNAVAILABLE", 503, True) from None


class IdentitySynchronizer:
    def __init__(
        self,
        config: IdentityConfig,
        repository: IdentityRepository,
        events: EventProvider,
        membership: MembershipProvider,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.config, self.repository = config, repository
        self.events, self.membership, self.clock = events, membership, clock
        self.wakeup = threading.Event()

    def wake_after_verified_webhook(self) -> None:
        """Caller verifies webhook signature with the SDK; webhook body never mutates state."""
        self.wakeup.set()

    def poll_once(self) -> int:
        cursor = self.repository.cursor(self.config.issuer)
        page = self.events.page(cursor)
        if len(page) > 100:
            raise AuthError("IDENTITY_EVENTS_INVALID", 503)
        events: list[IdentityEvent | SessionRevocationEvent] = []
        now = utc(self.clock())
        for event in page:
            identifier(event.event_id)
            if event.kind not in EVENT_TYPES or len(event.body_sha256) != 64:
                raise AuthError("IDENTITY_EVENTS_INVALID", 503)
            if event.kind == "session.revoked":
                events.append(
                    SessionRevocationEvent(
                        event.event_id,
                        event.body_sha256,
                        now,
                        self.config.issuer,
                        event.resource_id,
                        event.subject,
                        now + timedelta(seconds=self.config.credential_max_lifetime_seconds),
                    )
                )
                continue
            assert event.organization_id is not None
            try:
                observation = self.membership.current_membership(event.resource_id, now)
            except AuthError as error:
                if error.code == "PROVIDER_MEMBERSHIP_REMOVED":
                    observation = Observation(
                        self.config.issuer,
                        event.resource_id,
                        event.organization_id,
                        event.subject,
                        "removed",
                        now,
                        now,
                        locally_denied=True,
                    )
                else:
                    # A current-state fetch outage does not erase the event or advance its cursor.
                    raise
            if (
                observation.issuer,
                observation.membership_id,
                observation.subject,
                observation.organization_id,
            ) != (self.config.issuer, event.resource_id, event.subject, event.organization_id):
                raise AuthError("IDENTITY_EVENT_SCOPE_CONFLICT", 409)
            # Current provider state is used even for out-of-order event timestamps.
            # A trusted removal event additionally persists denial; late active cannot undo it.
            if event.kind == "organization_membership.deleted":
                observation = Observation(
                    observation.issuer,
                    observation.membership_id,
                    observation.organization_id,
                    observation.subject,
                    "removed",
                    now,
                    now,
                    locally_denied=True,
                )
            events.append(IdentityEvent(event.event_id, event.body_sha256, now, observation))
        if page:
            self.repository.commit_batch(self.config.issuer, cursor, page[-1].event_id, events)
        return len(page)

    def run(self, stop: threading.Event, failure: Callable[[str], None]) -> None:
        """Deployment owns the worker lifecycle; no import-time thread/network is started."""
        while not stop.is_set():
            try:
                self.poll_once()
            except Exception:
                failure("IDENTITY_SYNC_UNAVAILABLE")
            # Small bounded waits let the owner's shutdown finish promptly.
            for _ in range(self.config.poll_seconds):
                if stop.wait(1) or self.wakeup.is_set():
                    break
            self.wakeup.clear()
