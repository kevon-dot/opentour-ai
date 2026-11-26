"""Owner-local preflight and typed model connector operations."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

VERSION = "opentour.pilot.model-connector.v1"


class Purpose(StrEnum):
    AUTHORING = "authoring_inference"
    BASELINE = "baseline_inference"
    TRAINING = "training"
    DISCLOSURE = "external_disclosure"
    INSPECTION = "capture_inspection"


class Route(StrEnum):
    LOCAL = "local_open_weight"
    MANAGED = "customer_managed"
    CUSTOM = "customer_trainer"


class State(StrEnum):
    BENCHMARK_ONLY = "benchmark_only"
    TRAINABLE = "trainable"
    NEEDS_CONFIGURATION = "needs_configuration"
    UNSUPPORTED = "unsupported"


class Fidelity(StrEnum):
    OBSERVED = "observed"
    RECONSTRUCTED = "reconstructed"
    UNAVAILABLE = "unavailable"


class CaptureField(StrEnum):
    REQUEST_WITNESS = "application_request_witness"
    PROVIDER_RECEIPT = "provider_request_response_receipt"
    SERVED_IDENTITY = "served_model_identity"
    INPUT_PROJECTION = "authorized_input_projection"
    PARSED_OUTPUT = "parsed_output"
    COMMITTED_ACTION = "committed_application_action"


@dataclass(frozen=True)
class ModelBinding:
    tenant: str
    model_ref: str
    immutable_revision: str
    task: str
    input_schema: str
    output_schema: str
    preprocessing_revision: str
    route: Route
    account_or_host: str
    region: str
    credential_ref: str

    def __post_init__(self) -> None:
        references = (
            self.tenant,
            self.model_ref,
            self.immutable_revision,
            self.task,
            self.input_schema,
            self.output_schema,
            self.preprocessing_revision,
            self.account_or_host,
            self.region,
            self.credential_ref,
        )
        if not isinstance(self.route, Route) or not all(
            isinstance(value, str) and value and value.strip() == value for value in references
        ):
            raise ValueError("invalid_model_binding")


@dataclass(frozen=True)
class Grant:
    actor: str
    binding: ModelBinding
    purpose: Purpose
    revision: str
    expires_at: int
    permitted_fields: frozenset[str]
    retention_until: int
    fit_bound: FitBound | None = None


@dataclass(frozen=True)
class CaptureEvidence:
    fidelity: Fidelity
    boundary: str
    provenance_ref: str
    expires_at: int


@dataclass(frozen=True)
class Observation:
    binding: ModelBinding
    source_ref: str
    observed_at: int
    expires_at: int
    resolved_revision: str | None
    inference_supported: bool
    training_supported: bool
    hard_cost_cap: bool
    cancellation: bool
    progress: bool
    checkpoint_recovery: bool
    capture: Mapping[CaptureField, CaptureEvidence]


@dataclass(frozen=True)
class FitBound:
    currency: str
    maximum_minor_units: int
    maximum_seconds: int
    maximum_rows: int
    maximum_steps: int


@dataclass(frozen=True)
class FitIntent:
    binding: ModelBinding
    grant_revision: str
    benchmark_revision: str
    development_digest: str
    idempotency_key: str
    fields: frozenset[str]
    bound: FitBound


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELED = "canceled"
    FAILED = "failed"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class JobReceipt:
    intent: FitIntent
    state: JobState
    job_ref: str
    evidence_ref: str


@dataclass(frozen=True)
class Candidate:
    intent: FitIntent
    job_ref: str
    parent_revision: str
    artifact_or_serving_revision: str
    replay_evidence_ref: str


class Connector(Protocol):
    def observe(self, binding: ModelBinding, purpose: Purpose) -> Observation: ...

    def serialize_development(
        self, intent: FitIntent, rows: tuple[Mapping[str, object], ...]
    ) -> bytes: ...

    def estimate(self, intent: FitIntent) -> FitBound | None: ...

    def submit(self, intent: FitIntent, development: bytes) -> JobReceipt: ...

    def lookup(self, intent: FitIntent) -> JobReceipt | None: ...

    def status(self, job: JobReceipt) -> JobReceipt: ...

    def cancel(self, job: JobReceipt) -> JobReceipt: ...

    def collect(self, job: JobReceipt) -> Candidate: ...

    def predict_for_replay(
        self, candidate: Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]: ...


class CapabilityError(ValueError):
    """A fixed owner-local preflight reason."""


@dataclass(frozen=True)
class Capability:
    state: State
    reason: str
    exact_model_attribution: bool = False
    evidence_ref: str | None = None
    schema_version: str = VERSION


class Preflight:
    """Use the live owner grant lookup before calling metadata or estimate operations."""

    def __init__(
        self,
        connector: Connector,
        current_grant: Callable[[str, ModelBinding, Purpose], Grant | None],
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.connector = connector
        self.current_grant = current_grant
        self.clock = clock

    def _grant(self, actor: str, binding: ModelBinding, purpose: Purpose) -> Grant:
        grant = self.current_grant(actor, binding, purpose)
        now = self.clock()
        if (
            grant is None
            or (grant.actor, grant.binding, grant.purpose) != (actor, binding, purpose)
            or not grant.revision
            or grant.expires_at <= now
            or grant.retention_until <= now
        ):
            raise CapabilityError("model_capability_unavailable")
        return grant

    def _observe(self, binding: ModelBinding, purpose: Purpose) -> Observation:
        try:
            observed = self.connector.observe(binding, purpose)
        except (OSError, ValueError, LookupError):
            raise CapabilityError("model_capability_evidence_unavailable") from None
        if (
            observed.binding != binding
            or not observed.source_ref
            or not observed.observed_at <= self.clock() < observed.expires_at
        ):
            raise CapabilityError("model_capability_evidence_stale_or_mismatched")
        return observed

    def discover(self, actor: str, binding: ModelBinding, purpose: Purpose) -> Capability:
        if purpose not in (Purpose.AUTHORING, Purpose.BASELINE, Purpose.TRAINING):
            raise CapabilityError("model_capability_unavailable")
        grant = self._grant(actor, binding, purpose)
        try:
            observed = self._observe(binding, purpose)
        except CapabilityError:
            if self._grant(actor, binding, purpose) != grant:
                raise CapabilityError("model_capability_unavailable") from None
            return Capability(State.NEEDS_CONFIGURATION, "model_capability_evidence_unavailable")
        if self._grant(actor, binding, purpose) != grant:
            raise CapabilityError("model_capability_unavailable")
        exact = observed.resolved_revision == binding.immutable_revision
        if purpose is not Purpose.TRAINING:
            state = State.BENCHMARK_ONLY if observed.inference_supported else State.UNSUPPORTED
            reason = (
                "inference_available" if observed.inference_supported else "inference_unavailable"
            )
            return Capability(state, reason, exact, observed.source_ref)
        if not exact:
            return Capability(State.NEEDS_CONFIGURATION, "exact_model_revision_unverified")
        if not observed.training_supported:
            return Capability(State.BENCHMARK_ONLY, "training_unavailable", exact)
        if grant.fit_bound is None or not observed.hard_cost_cap:
            return Capability(State.NEEDS_CONFIGURATION, "enforceable_training_budget_required")
        return Capability(
            State.TRAINABLE, "exact_training_target_observed", exact, observed.source_ref
        )

    def prepare(self, actor: str, intent: FitIntent) -> FitBound:
        grant = self._grant(actor, intent.binding, Purpose.TRAINING)
        if (
            grant.revision != intent.grant_revision
            or not intent.fields <= grant.permitted_fields
            or not intent.benchmark_revision
            or not intent.development_digest
            or not intent.idempotency_key
        ):
            raise CapabilityError("training_data_scope_unavailable")
        if grant.fit_bound is None or not _within(intent.bound, grant.fit_bound):
            raise CapabilityError("training_budget_not_authorized")
        observed = self._observe(intent.binding, Purpose.TRAINING)
        if (
            not observed.training_supported
            or observed.resolved_revision != intent.binding.immutable_revision
        ):
            raise CapabilityError("exact_training_target_unavailable")
        if not observed.hard_cost_cap:
            raise CapabilityError("enforceable_training_cap_unavailable")
        if self._grant(actor, intent.binding, Purpose.TRAINING) != grant:
            raise CapabilityError("training_data_scope_unavailable")
        try:
            estimate = self.connector.estimate(intent)
        except (OSError, ValueError, LookupError):
            raise CapabilityError("known_training_cost_within_budget_required") from None
        if estimate is None or not _within(estimate, intent.bound):
            raise CapabilityError("known_training_cost_within_budget_required")
        if self._grant(actor, intent.binding, Purpose.TRAINING) != grant:
            raise CapabilityError("training_data_scope_unavailable")
        if self.clock() >= observed.expires_at:
            raise CapabilityError("model_capability_evidence_stale_or_mismatched")
        return estimate

    def capture(self, actor: str, binding: ModelBinding) -> dict[CaptureField, CaptureEvidence]:
        grant = self._grant(actor, binding, Purpose.INSPECTION)
        observed = self._observe(binding, Purpose.INSPECTION)
        if self._grant(actor, binding, Purpose.INSPECTION) != grant:
            raise CapabilityError("model_capability_unavailable")
        now = self.clock()
        unavailable = CaptureEvidence(Fidelity.UNAVAILABLE, "unavailable", "", 0)
        return {
            field: observed.capture[field]
            if field in grant.permitted_fields
            and field in observed.capture
            and now < observed.capture[field].expires_at
            else unavailable
            for field in CaptureField
        }


def _within(estimate: FitBound, limit: FitBound) -> bool:
    dimensions = (
        (estimate.maximum_minor_units, limit.maximum_minor_units, 0),
        (estimate.maximum_seconds, limit.maximum_seconds, 1),
        (estimate.maximum_rows, limit.maximum_rows, 1),
        (estimate.maximum_steps, limit.maximum_steps, 1),
    )
    return (
        bool(limit.currency)
        and estimate.currency == limit.currency
        and all(
            type(amount) is int and type(maximum) is int and minimum <= amount <= maximum
            for amount, maximum, minimum in dimensions
        )
    )


def verify_candidate(job: JobReceipt, candidate: Candidate) -> None:
    """Require the collected or served artifact to be bound to the exact completed job."""
    if (
        job.state is not JobState.COMPLETED
        or candidate.intent != job.intent
        or candidate.job_ref != job.job_ref
        or not job.job_ref
        or not job.evidence_ref
        or candidate.parent_revision != job.intent.binding.immutable_revision
        or not candidate.artifact_or_serving_revision
        or not candidate.replay_evidence_ref
    ):
        raise CapabilityError("candidate_lineage_unverified")
