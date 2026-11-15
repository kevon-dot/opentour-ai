"""Deterministic test adapters. Never communicate with real provider services."""

from __future__ import annotations

import hashlib
from collections import deque
from dataclasses import replace
from datetime import datetime
from typing import Literal

from tools.fixtures.ports import (
    AcknowledgmentLost,
    CancelObservation,
    FixtureRefusal,
    LookupPage,
    Membership,
    ProviderJob,
    Scope,
    Submission,
)
from tools.fixtures.registry import ISSUER


class ScriptedBatch:
    issuer = ISSUER

    def __init__(
        self,
        outcomes: tuple[Literal["ack", "ack_lost", "reject"], ...] = ("ack",),
        *,
        page_size: int = 2,
        invisible_lookups: int = 0,
    ) -> None:
        if page_size < 1 or page_size > 100 or invisible_lookups < 0:
            raise ValueError("Invalid bounded lookup configuration")
        self.outcomes = deque(outcomes)
        self.jobs: list[ProviderJob] = []
        self.submit_count = 0
        self.lookup_count = 0
        self.page_size = page_size
        self.invisible_lookups = invisible_lookups
        self.cancel_ack_lost = False
        self.cancel_requests: set[str] = set()
        self.scripted_terminal_ids: set[str] = set()

    def submit(self, request: Submission) -> ProviderJob:
        if not request.submission_id.startswith("simulated:"):
            raise FixtureRefusal("SIMULATED_NAMESPACE_REQUIRED")
        self.submit_count += 1
        if not self.outcomes:
            raise FixtureRefusal("UNSCRIPTED_PROVIDER_CALL")
        outcome = self.outcomes.popleft()
        if outcome == "reject":
            raise FixtureRefusal("SIMULATED_PROVIDER_REJECTION")
        job = ProviderJob(f"{ISSUER}:batch-job:{len(self.jobs) + 1}", request)
        self.jobs.append(job)
        if outcome == "ack_lost":
            raise AcknowledgmentLost()
        return job

    def lookup(self, request: Submission, cursor: int | None = None) -> LookupPage:
        self.lookup_count += 1
        if self.invisible_lookups:
            self.invisible_lookups -= 1
            return LookupPage((), None)
        # Mimic the documented name search's broad candidate set. Exact matching belongs
        # to the consumer; the fake does not pre-filter wrong tags into a false guarantee.
        candidates = [
            j for j in self.jobs if j.submission.job_name.lower() == request.job_name.lower()
        ]
        start = 0 if cursor is None else cursor
        if type(start) is not int or start < 0 or start > len(candidates):
            raise FixtureRefusal("INVALID_FIXTURE_CURSOR")
        end = min(start + self.page_size, len(candidates))
        return LookupPage(tuple(candidates[start:end]), end if end < len(candidates) else None)

    def cancel(self, provider_job_id: str, scope: Scope) -> CancelObservation:
        for job in self.jobs:
            if job.provider_job_id == provider_job_id and job.submission.scope == scope:
                self.cancel_requests.add(provider_job_id)
                return CancelObservation(None if self.cancel_ack_lost else True, job.terminal)
        raise FixtureRefusal("RESOURCE_NOT_FOUND")

    def observe_terminal(self, provider_job_id: str, scope: Scope) -> bool:
        for index, job in enumerate(self.jobs):
            if job.provider_job_id == provider_job_id and job.submission.scope == scope:
                if provider_job_id in self.scripted_terminal_ids:
                    self.jobs[index] = replace(job, terminal=True)
                return self.jobs[index].terminal
        raise FixtureRefusal("RESOURCE_NOT_FOUND")


class ScriptedWorkOS:
    issuer = ISSUER

    def __init__(self) -> None:
        self.memberships: dict[tuple[str, Scope], Membership] = {}
        self.inbox: dict[tuple[Scope, str], str] = {}
        self.current_state_reads = 0

    def current_membership(self, subject: str, scope: Scope) -> Membership:
        self.current_state_reads += 1
        return self.memberships.get((subject, scope), "reconciliation_unknown")

    def event(self, event_id: str, body_digest: str, subject: str, scope: Scope) -> Membership:
        key = (scope, event_id)
        previous = self.inbox.get(key)
        if previous is not None and previous != body_digest:
            raise FixtureRefusal("IDEMPOTENCY_CONFLICT")
        self.inbox[key] = body_digest
        # Delivery order never authorizes applying an event's old membership value.
        return self.current_membership(subject, scope)


class ScriptedSource:
    issuer = ISSUER

    def __init__(self) -> None:
        self.outcomes: dict[tuple[Scope, str], tuple[str, str]] = {}

    def current_outcome(self, scope: Scope, operation_id: str, now: str) -> str:
        observation = self.outcomes.get((scope, operation_id))
        if observation is None or datetime.fromisoformat(now) < datetime.fromisoformat(
            observation[0]
        ):
            return "unknown"
        return observation[1]


class ScriptedS3:
    issuer = ISSUER

    def __init__(self) -> None:
        self.objects: dict[tuple[Scope, str], tuple[str, bytes]] = {}
        self.cleanup_fails = False
        self.quarantined: set[tuple[Scope, str]] = set()

    def stage(self, scope: Scope, object_id: str, content: bytes) -> str:
        key = (scope, object_id)
        value = (hashlib.sha256(content).hexdigest(), content)
        if key in self.objects and self.objects[key] != value:
            raise FixtureRefusal("IDEMPOTENCY_CONFLICT")
        self.objects[key] = value
        self.quarantined.add(key)
        return value[0]

    def read(self, scope: Scope, object_id: str, digest: str) -> bytes:
        value = self.objects.get((scope, object_id))
        if value is None or value[0] != digest:
            raise FixtureRefusal("RESOURCE_NOT_FOUND")
        return value[1]

    def cleanup(self, scope: Scope, object_id: str) -> bool:
        key = (scope, object_id)
        if self.cleanup_fails:
            self.quarantined.add(key)
            return False
        self.objects.pop(key, None)
        self.quarantined.discard(key)
        return True


class ScriptedAWSQueue:
    issuer = ISSUER

    def __init__(self) -> None:
        self.accepted: dict[tuple[Scope, str], str] = {}
        self.refused: list[str] = []

    def deliver(
        self, worker: Scope, envelope_scope: Scope, event_id: str, body_digest: str
    ) -> bool:
        if worker != envelope_scope:
            self.refused.append("RESOURCE_NOT_FOUND")
            raise FixtureRefusal("RESOURCE_NOT_FOUND")
        key = (worker, event_id)
        previous = self.accepted.get(key)
        if previous is not None and previous != body_digest:
            raise FixtureRefusal("IDEMPOTENCY_CONFLICT")
        self.accepted[key] = body_digest
        return previous is None


class ScriptedMetronome:
    issuer = ISSUER

    def __init__(self) -> None:
        self.accepted: dict[tuple[Scope, str], str] = {}
        self.transaction_owners: dict[str, Scope] = {}
        self.lose_ack = False
        self.export_attempts: list[tuple[Scope, str, str]] = []

    def export(self, scope: Scope, transaction_id: str, body_digest: str) -> str:
        if self.transaction_owners.get(transaction_id) != scope:
            raise FixtureRefusal("RESOURCE_NOT_FOUND")
        self.export_attempts.append((scope, transaction_id, body_digest))
        key = (scope, transaction_id)
        previous = self.accepted.get(key)
        if previous is not None and previous != body_digest:
            raise FixtureRefusal("USAGE_EVENT_CONFLICT")
        self.accepted[key] = body_digest
        if self.lose_ack:
            self.lose_ack = False
            raise AcknowledgmentLost()
        return "accepted"  # Not rated, invoice_reconciled, paid or an entitlement.


class ScriptedStripe:
    issuer = ISSUER

    def __init__(self) -> None:
        self.customers: dict[tuple[Scope, str], str] = {}
        self.inbox: dict[tuple[Scope, str], str] = {}
        self.available = True

    def current_state(self, scope: Scope, customer_id: str) -> str:
        if (scope, customer_id) not in self.customers:
            raise FixtureRefusal("RESOURCE_NOT_FOUND")
        if not self.available:
            return "reconciliation_pending"
        return self.customers[scope, customer_id]

    def event(self, scope: Scope, event_id: str, digest: str, customer_id: str) -> str:
        current = self.current_state(scope, customer_id)
        key = (scope, event_id)
        if key in self.inbox and self.inbox[key] != digest:
            raise FixtureRefusal("IDEMPOTENCY_CONFLICT")
        self.inbox[key] = digest
        return current
