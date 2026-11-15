"""Typed test seams for future OPE-144/146/149 and identity/billing consumers.

These interfaces do not assert that production adapters already exist. Fixture
implementations are never registered as real provider or signing authorities.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Literal, Protocol

Mode = Literal["customer_controlled", "opentour_managed"]
Kind = Literal["simulated", "local_integration", "provider_integration", "customer_evidence"]
Membership = Literal["active", "removed", "reconciliation_unknown"]
EffectState = Literal[
    "prepared",
    "dispatching",
    "applied",
    "refused",
    "cancelled_before_dispatch",
    "outcome_unknown",
    "reconciling",
    "compensated",
]


@dataclass(frozen=True)
class Scope:
    organization_id: str
    environment_id: str
    cell_id: str
    mode: Mode

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


@dataclass(frozen=True)
class Submission:
    """Frozen OPE-149 request identity; provider job names are not unique keys."""

    submission_id: str
    attempt_id: str
    scope: Scope
    request_digest: str
    account_id: str
    region: str
    queue: str
    definition_revision: str
    job_name: str
    created_at: str


@dataclass(frozen=True)
class ProviderJob:
    provider_job_id: str
    submission: Submission
    terminal: bool = False


@dataclass(frozen=True)
class LookupPage:
    jobs: tuple[ProviderJob, ...]
    next_cursor: int | None


@dataclass(frozen=True)
class CancelObservation:
    request_acknowledged: bool | None
    terminal_observed: bool


class AcknowledgmentLost(Exception):
    """Acceptance is ambiguous; this exception never authorizes resubmission."""


class FixtureRefusal(ValueError):
    def __init__(self, code: str):
        # Only registered fixed codes may enter public fixture evidence.
        super().__init__(code)
        self.code = code


class BatchPort(Protocol):
    def submit(self, request: Submission) -> ProviderJob: ...
    def lookup(self, request: Submission, cursor: int | None = None) -> LookupPage: ...
    def cancel(self, provider_job_id: str, scope: Scope) -> CancelObservation: ...
    def observe_terminal(self, provider_job_id: str, scope: Scope) -> bool: ...


class IdentityPort(Protocol):
    def current_membership(self, subject: str, scope: Scope) -> Membership: ...


class JournalPort(Protocol):
    def current_frontier(self) -> tuple[int, str] | None: ...
    def contiguous(self, sequence: int, digest: str) -> bool: ...


class SourceOutcomePort(Protocol):
    def current_outcome(self, scope: Scope, operation_id: str, now: str) -> str: ...


class AuthorityPort(Protocol):
    def check(self, scope: Scope, epoch: int, revision: str, now: str) -> None: ...


class ObjectPort(Protocol):
    def stage(self, scope: Scope, object_id: str, content: bytes) -> str: ...
    def read(self, scope: Scope, object_id: str, digest: str) -> bytes: ...
    def cleanup(self, scope: Scope, object_id: str) -> bool: ...


class UsagePort(Protocol):
    def export(self, scope: Scope, transaction_id: str, body_digest: str) -> str: ...


class BillingPort(Protocol):
    def current_state(self, scope: Scope, customer_id: str) -> str: ...


class CellTransactionPort(Protocol):
    def prepare(
        self, effect_id: str, scope: Scope, epoch: int, revision: str, request_digest: str
    ) -> bool: ...
    def state(self, effect_id: str, scope: Scope) -> EffectState | None: ...
    def resolve(self, effect_id: str, scope: Scope, state: EffectState) -> None: ...
