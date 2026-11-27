"""Managed fine-tuning connector: the customer-provider-account route behind OPE-506.

Implements ``model_connector.Connector`` (plus ``UsageReporting`` and ``Fenceable``) over a
provider-neutral ``ManagedTransport`` port. The money, the training file and the credentials belong
to the customer's own provider account: the connector holds only a ``credential_ref`` and passes it
to the transport, which resolves the secret inside the customer's secret store. Nothing here ever
reads, stores, logs or returns a secret value.

Guarantees implemented here (software behavior only):
* A durable append-only journal, keyed by sha256(idempotency_key), is written BEFORE any provider
  call. A duplicate dispatch, a retry after a lost acknowledgement or a restart never starts a
  second provider execution: the provider also receives the key as a client token.
* ``lookup`` is read-only toward the provider (find by client token) and never creates a job.
* A dispatch whose outcome cannot be verified is never retried by this connector: the journal keeps
  the intent, ``lookup`` reports ``None`` (UNKNOWN to ``Preflight``) and a new attempt needs a new
  fit intent and therefore a new reservation owned by the caller. The connector never reserves or
  settles money. It enforces the bound it is given, reports provider-reported usage and never
  invents a zero.
* An unrecognised or incomplete provider state is UNKNOWN, never success. A terminal state is
  final: a late provider event for a settled, canceled or fenced job changes nothing.
* Cancel records durable intent before asking the provider. A canceled or fenced job never yields a
  candidate. ``fence`` is durable before it returns and survives restart.
* Protected answers are refused in training payloads and replay inputs; a tuned-model reference is
  handed off only to a permitted recipient; the training file's retention disposition is recorded.

Simulated: tests drive this connector through a generated in-memory provider double (the transport
port is the only boundary). Every receipt, evidence ref and tuned-model id is a ``simulated:*``
sentinel.
Not proven: no real provider account, API, billing, cap enforcement, deletion, region or
tuned-model inference has been exercised. These tests prove software behavior only and establish
no installed-provider or customer evidence. The connector's evidence tier is
``EvidenceTier.FIXTURE`` and ``EXTERNAL_GATES`` lists what remains unsatisfied. Provider selection
is pending the customer binding, so provider qualification is open.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.connector_ext import EvidenceTier, FenceReceipt, Usage, UsageSource

VERSION = "opentour.pilot.managed-connector.v1"
EVIDENCE_TIER = EvidenceTier.FIXTURE
EXTERNAL_GATES = (
    "provider_selection_pending_customer_binding",
    "installed_provider_job_in_authorized_test_account",
    "provider_reported_usage_and_deletion_witness",
    "recipient_identity_inference_of_tuned_model",
    "enforceable_provider_cap_witnessed",
)
_STATES = {
    "queued": mc.JobState.QUEUED,
    "validating": mc.JobState.QUEUED,
    "running": mc.JobState.RUNNING,
    "succeeded": mc.JobState.COMPLETED,
    "failed": mc.JobState.FAILED,
    "canceled": mc.JobState.CANCELED,
    "cancelled": mc.JobState.CANCELED,
}
_TERMINAL = {mc.JobState.COMPLETED, mc.JobState.FAILED, mc.JobState.CANCELED}
_REF_PREFIX = "simulated:managed-job:"


# --------------------------------------------------------------------------- provider port
@dataclass(frozen=True)
class ProviderModel:
    resolved_version: str | None
    training_supported: bool
    hard_cap_enforceable: bool
    cancellation: bool
    progress: bool


@dataclass(frozen=True)
class ProviderQuote:
    currency: str
    minor_units: int
    seconds: int
    steps: int


@dataclass(frozen=True)
class ProviderUsage:
    currency: str
    minor_units: int
    seconds: int
    rows: int
    steps: int


@dataclass(frozen=True)
class ProviderFile:
    file_id: str
    client_token: str


@dataclass(frozen=True)
class ProviderJob:
    job_id: str
    state: str
    client_token: str
    intent_sha256: str
    file_id: str
    cap_minor_units: int | None
    tuned_model_id: str | None = None
    usage: ProviderUsage | None = None


class ManagedTransport(Protocol):
    """The customer's provider account. Every call is authorized by ``binding.credential_ref``.

    A transport resolves the credential inside the customer's secret store and raises
    ``LookupError`` when it is unavailable. Create operations are idempotent on ``client_token``.
    Only ``OSError | ValueError | LookupError`` may be raised.
    """

    def describe_model(self, binding: mc.ModelBinding) -> ProviderModel: ...

    def quote(self, binding: mc.ModelBinding, rows: int, epochs: int) -> ProviderQuote | None: ...

    def put_file(
        self, binding: mc.ModelBinding, client_token: str, data: bytes
    ) -> ProviderFile: ...

    def find_file(self, binding: mc.ModelBinding, client_token: str) -> ProviderFile | None: ...

    def create_job(
        self,
        binding: mc.ModelBinding,
        client_token: str,
        intent_sha256: str,
        file_id: str,
        cap_minor_units: int,
        epochs: int,
    ) -> ProviderJob: ...

    def find_job(self, binding: mc.ModelBinding, client_token: str) -> ProviderJob | None: ...

    def get_job(self, binding: mc.ModelBinding, job_id: str) -> ProviderJob: ...

    def cancel_job(self, binding: mc.ModelBinding, job_id: str) -> ProviderJob: ...

    def delete_file(self, binding: mc.ModelBinding, file_id: str) -> bool: ...

    def predict(
        self, binding: mc.ModelBinding, tuned_model_id: str, inputs: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]: ...


@dataclass(frozen=True)
class ManagedSettings:
    """Fixed, connector-owned settings. Callers cannot widen them through an intent."""

    account: str
    region: str
    task: str
    preprocessing_revision: str
    epochs: int = 1
    observation_seconds: int = 300
    protected_fields: frozenset[str] = frozenset()
    permitted_recipients: frozenset[str] = frozenset()


# --------------------------------------------------------------------------- helpers
def _canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def serialize_rows(rows: tuple[Mapping[str, object], ...]) -> bytes:
    """Canonical, order-independent JSON-lines encoding of development rows."""
    lines = sorted(_canonical({"fields": dict(row)}).decode() for row in rows)
    return ("\n".join(lines) + "\n").encode()


def development_digest(rows: tuple[Mapping[str, object], ...]) -> str:
    return _sha(serialize_rows(rows))


def _binding_document(binding: mc.ModelBinding) -> dict[str, str]:
    return {
        "tenant": binding.tenant,
        "model_ref": binding.model_ref,
        "immutable_revision": binding.immutable_revision,
        "task": binding.task,
        "input_schema": binding.input_schema,
        "output_schema": binding.output_schema,
        "preprocessing_revision": binding.preprocessing_revision,
        "route": binding.route.value,
        "account_or_host": binding.account_or_host,
        "region": binding.region,
        "credential_ref": binding.credential_ref,
    }


def intent_digest(intent: mc.FitIntent) -> str:
    bound = intent.bound
    return _sha(
        _canonical(
            [
                _binding_document(intent.binding),
                intent.grant_revision,
                intent.benchmark_revision,
                intent.development_digest,
                intent.idempotency_key,
                sorted(intent.fields),
                [
                    bound.currency,
                    bound.maximum_minor_units,
                    bound.maximum_seconds,
                    bound.maximum_rows,
                    bound.maximum_steps,
                ],
            ]
        )
    )


def _append(path: Path, event: Mapping[str, Any]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.write(descriptor, _canonical(dict(event)) + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@dataclass
class _View:
    """Journal replay. The journal is the only memory; there is no in-process cache."""

    exists: bool = False
    intent_sha: str = ""
    binding: dict[str, str] | None = None
    file_id: str = ""
    job_id: str = ""
    state: mc.JobState = mc.JobState.UNKNOWN
    terminal: bool = False
    tuned_model_id: str = ""
    usage: ProviderUsage | None = None
    cancel_requested: bool = False
    disposition: str = ""


class ManagedConnector:
    """``model_connector.Connector`` for a customer-owned managed fine-tuning account."""

    evidence_tier = EVIDENCE_TIER

    def __init__(
        self,
        transport: ManagedTransport,
        settings: ManagedSettings,
        state_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.transport = transport
        self.settings = settings
        self.root = state_dir
        self.clock = clock
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.root / "jobs").mkdir(mode=0o700, exist_ok=True)

    # ------------------------------------------------------------------ guards
    def _fenced(self) -> bool:
        return (self.root / "FENCED").exists()

    def _require_open(self) -> None:
        if self._fenced():
            raise ValueError("connector_fenced")

    def _require_binding(self, binding: mc.ModelBinding) -> None:
        settings = self.settings
        if (
            binding.route is not mc.Route.MANAGED
            or binding.account_or_host != settings.account
            or binding.region != settings.region
            or binding.task != settings.task
            or binding.preprocessing_revision != settings.preprocessing_revision
        ):
            raise ValueError("binding_not_served_by_connector")

    # ------------------------------------------------------------------ capability
    def observe(self, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Observation:
        self._require_open()
        self._require_binding(binding)
        model = self.transport.describe_model(binding)
        now = int(self.clock())
        ready = model.resolved_version is not None
        return mc.Observation(
            binding=binding,
            source_ref=f"simulated:managed-provider:{_sha(_canonical(_binding_document(binding)))[:16]}",
            observed_at=now,
            expires_at=now + self.settings.observation_seconds,
            resolved_revision=model.resolved_version,
            inference_supported=ready,
            training_supported=ready and model.training_supported,
            hard_cost_cap=model.hard_cap_enforceable,
            cancellation=model.cancellation,
            progress=model.progress,
            checkpoint_recovery=False,
            capture={},
        )

    def estimate(self, intent: mc.FitIntent) -> mc.FitBound | None:
        self._require_open()
        self._require_binding(intent.binding)
        rows = intent.bound.maximum_rows
        quote = self.transport.quote(intent.binding, rows, self.settings.epochs)
        if quote is None:
            return None
        return mc.FitBound(quote.currency, quote.minor_units, quote.seconds, rows, quote.steps)

    def serialize_development(
        self, intent: mc.FitIntent, rows: tuple[Mapping[str, object], ...]
    ) -> bytes:
        if not rows:
            raise ValueError("training_rows_empty")
        for row in rows:
            if set(row) & self.settings.protected_fields:
                raise ValueError("protected_field_in_payload")
            if set(row) != set(intent.fields):
                raise ValueError("training_row_fields_outside_scope")
        return serialize_rows(rows)

    # ------------------------------------------------------------------ journal
    def _key(self, intent: mc.FitIntent) -> str:
        return _sha(intent.idempotency_key.encode())[:32]

    def _journal(self, key: str) -> Path:
        return self.root / "jobs" / f"{key}.jsonl"

    def _job_ref(self, key: str) -> str:
        return f"{_REF_PREFIX}{key}"

    def _key_from_ref(self, job_ref: str) -> str:
        key = job_ref.removeprefix(_REF_PREFIX)
        if not job_ref.startswith(_REF_PREFIX) or len(key) != 32 or not key.isalnum():
            raise LookupError("job_reference_unknown")
        return key

    @contextmanager
    def _locked(self, key: str) -> Iterator[None]:
        descriptor = os.open(
            self.root / "jobs" / f"{key}.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            os.close(descriptor)

    def _view(self, key: str) -> _View:
        try:
            raw = self._journal(key).read_bytes()
        except FileNotFoundError:
            return _View()
        view = _View(exists=True)
        for line in raw.splitlines():
            event = json.loads(line)
            kind = event.get("event") if isinstance(event, dict) else None
            if kind == "dispatching":
                view.intent_sha = event["intent_sha256"]
                view.binding = event["binding"]
            elif kind == "file":
                view.file_id = event["file_id"]
            elif kind == "job":
                view.job_id = event["job_id"]
            elif kind == "progress" and not view.terminal:
                view.state = mc.JobState(event["state"])
            elif kind == "terminal" and not view.terminal:
                view.terminal = True
                view.state = mc.JobState(event["state"])
                view.tuned_model_id = event.get("tuned_model_id", "")
                usage = event.get("usage")
                view.usage = ProviderUsage(**usage) if usage else None
            elif kind == "cancel_requested":
                view.cancel_requested = True
            elif kind == "disposition":
                view.disposition = event["value"]
            else:
                raise ValueError("journal_corrupt")
        if not view.intent_sha:
            raise ValueError("journal_corrupt")
        return view

    def _binding_of(self, view: _View) -> mc.ModelBinding:
        if view.binding is None:
            raise ValueError("journal_corrupt")
        document = dict(view.binding)
        return mc.ModelBinding(**{**document, "route": mc.Route(document["route"])})  # type: ignore[arg-type]

    def _state_of(self, view: _View) -> mc.JobState:
        if view.cancel_requested:
            return mc.JobState.CANCELED
        return view.state

    def _receipt(self, intent: mc.FitIntent, key: str, view: _View) -> mc.JobReceipt:
        return mc.JobReceipt(
            intent,
            self._state_of(view),
            self._job_ref(key),
            f"simulated:managed-receipt:{key}",
        )

    # ------------------------------------------------------------------ provider state
    def _absorb(self, key: str, view: _View, job: ProviderJob) -> None:
        """Fold one provider observation into the journal. A terminal state is final."""
        if view.terminal:
            return
        state = _STATES.get(job.state, mc.JobState.UNKNOWN)
        if state is mc.JobState.COMPLETED and not job.tuned_model_id:
            state = mc.JobState.UNKNOWN  # success without a usable tuned-model id is not success
        if state in _TERMINAL:
            event: dict[str, Any] = {"event": "terminal", "state": state.value}
            if state is mc.JobState.COMPLETED:
                event["tuned_model_id"] = job.tuned_model_id
            if job.usage is not None:
                event["usage"] = {
                    "currency": job.usage.currency,
                    "minor_units": job.usage.minor_units,
                    "seconds": job.usage.seconds,
                    "rows": job.usage.rows,
                    "steps": job.usage.steps,
                }
            _append(self._journal(key), event)
        elif state is not view.state:
            _append(self._journal(key), {"event": "progress", "state": state.value})

    def _adopt(self, key: str, view: _View, job: ProviderJob) -> None:
        """Record a provider job found by client token, refusing a different intent's job."""
        if job.intent_sha256 != view.intent_sha or job.client_token != self._token(key):
            raise ValueError("idempotency_conflict")
        if not view.job_id:
            _append(self._journal(key), {"event": "job", "job_id": job.job_id})
        if job.file_id and not view.file_id:
            _append(self._journal(key), {"event": "file", "file_id": job.file_id})

    def _token(self, key: str) -> str:
        return f"opentour-{key}"

    def _reconcile(self, key: str, view: _View) -> _View | None:
        """Read-only toward the provider: find by client token. ``None`` means unverifiable."""
        binding = self._binding_of(view)
        job = self.transport.find_job(binding, self._token(key))
        if job is None:
            return None
        self._adopt(key, view, job)
        view = self._view(key)
        self._absorb(key, view, job)
        view = self._view(key)
        if view.cancel_requested and not view.terminal:
            self._cancel_at_provider(key, view, binding)
            view = self._view(key)
        return view

    def _cancel_at_provider(self, key: str, view: _View, binding: mc.ModelBinding) -> None:
        if view.terminal or not view.job_id:
            return
        job = self.transport.cancel_job(binding, view.job_id)
        self._absorb(key, view, job)

    # ------------------------------------------------------------------ dispatch
    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        self._require_open()
        self._require_binding(intent.binding)
        key = self._key(intent)
        digest = intent_digest(intent)
        with self._locked(key):
            view = self._view(key)
            if view.exists:
                if view.intent_sha != digest:
                    raise ValueError("idempotency_conflict")
                if not view.job_id:
                    reconciled = self._reconcile(key, view)
                    if reconciled is None:
                        raise OSError("dispatch_outcome_unknown")
                    view = reconciled
                return self._receipt(intent, key, view)
            if _sha(development) != intent.development_digest:
                raise ValueError("training_payload_invalid")
            _append(
                self._journal(key),
                {
                    "event": "dispatching",
                    "intent_sha256": digest,
                    "binding": _binding_document(intent.binding),
                },
            )
            token = self._token(key)
            uploaded = self.transport.put_file(intent.binding, token, development)
            _append(self._journal(key), {"event": "file", "file_id": uploaded.file_id})
            job = self.transport.create_job(
                intent.binding,
                token,
                digest,
                uploaded.file_id,
                intent.bound.maximum_minor_units,
                self.settings.epochs,
            )
            _append(self._journal(key), {"event": "job", "job_id": job.job_id})
            view = self._view(key)
            if (
                job.cap_minor_units is None
                or job.cap_minor_units > intent.bound.maximum_minor_units
            ):
                self._cancel_unbounded(key, view, intent.binding)
                raise ValueError("provider_cap_not_enforced")
            self._absorb(key, view, job)
            return self._receipt(intent, key, self._view(key))

    def _cancel_unbounded(self, key: str, view: _View, binding: mc.ModelBinding) -> None:
        _append(self._journal(key), {"event": "cancel_requested"})
        self._cancel_at_provider(key, self._view(key), binding)

    def lookup(self, intent: mc.FitIntent) -> mc.JobReceipt | None:
        key = self._key(intent)
        digest = intent_digest(intent)
        with self._locked(key):
            view = self._view(key)
            if not view.exists:
                return None  # nothing was ever dispatched from here; lookup never creates
            if view.intent_sha != digest:
                raise ValueError("idempotency_conflict")
            if not view.job_id:
                reconciled = self._reconcile(key, view)
                if reconciled is None:
                    return None
                view = reconciled
            return self._receipt(intent, key, view)

    def status(self, job: mc.JobReceipt) -> mc.JobReceipt:
        key = self._key_from_ref(job.job_ref)
        with self._locked(key):
            view = self._view(key)
            if view.intent_sha != intent_digest(job.intent):
                raise ValueError("job_receipt_mismatched")
            if not view.terminal and view.job_id:
                provider = self.transport.get_job(self._binding_of(view), view.job_id)
                self._absorb(key, view, provider)
                view = self._view(key)
            return self._receipt(job.intent, key, view)

    def cancel(self, job: mc.JobReceipt) -> mc.JobReceipt:
        key = self._key_from_ref(job.job_ref)
        with self._locked(key):
            view = self._view(key)
            if view.intent_sha != intent_digest(job.intent):
                raise ValueError("job_receipt_mismatched")
            if not view.cancel_requested:
                _append(self._journal(key), {"event": "cancel_requested"})  # durable intent first
                view = self._view(key)
            self._cancel_at_provider(key, view, self._binding_of(view))
            return self._receipt(job.intent, key, self._view(key))

    # ------------------------------------------------------------------ results
    def _completed(self, job: mc.JobReceipt) -> tuple[str, _View]:
        self._require_open()
        key = self._key_from_ref(job.job_ref)
        view = self._view(key)
        if (
            view.intent_sha != intent_digest(job.intent)
            or view.cancel_requested
            or not view.terminal
            or view.state is not mc.JobState.COMPLETED
            or not view.tuned_model_id
        ):
            raise ValueError("job_not_collectable")
        return key, view

    def collect(self, job: mc.JobReceipt) -> mc.Candidate:
        key, view = self._completed(job)
        return mc.Candidate(
            job.intent,
            job.job_ref,
            job.intent.binding.immutable_revision,
            view.tuned_model_id,
            f"simulated:managed-replay:{key}",
        )

    def handoff_reference(self, candidate: mc.Candidate, recipient: str) -> str:
        """Release the tuned-model id only to a recipient the customer permitted."""
        self._require_open()
        if recipient not in self.settings.permitted_recipients:
            raise ValueError("recipient_not_permitted")
        return candidate.artifact_or_serving_revision

    def predict_for_replay(
        self, candidate: mc.Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        self._require_open()
        key = self._key_from_ref(candidate.job_ref)
        view = self._view(key)
        if (
            view.cancel_requested
            or view.state is not mc.JobState.COMPLETED
            or view.tuned_model_id != candidate.artifact_or_serving_revision
        ):
            raise ValueError("candidate_lineage_unverified")
        if any(set(row) & self.settings.protected_fields for row in inputs):
            raise ValueError("protected_field_in_payload")
        return self.transport.predict(candidate.intent.binding, view.tuned_model_id, inputs)

    # ------------------------------------------------------------------ usage, disposition
    def usage(self, job: mc.JobReceipt) -> Usage | None:
        """Provider-reported usage once the job is terminal; ``None`` is unknown, never zero."""
        key = self._key_from_ref(job.job_ref)
        view = self._view(key)
        if not view.terminal or view.usage is None:
            return None
        reported = view.usage
        state = view.state
        return Usage(
            self._job_ref(key),
            reported.currency,
            reported.minor_units,
            reported.seconds,
            reported.rows,
            reported.steps,
            UsageSource.PROVIDER_REPORTED,
            irreversible=reported.minor_units > 0 and state is not mc.JobState.COMPLETED,
        )

    def dispose_training_file(self, job: mc.JobReceipt) -> str:
        """Delete the provider training file after a terminal state and record the disposition."""
        key = self._key_from_ref(job.job_ref)
        with self._locked(key):
            view = self._view(key)
            if not view.terminal and not view.cancel_requested:
                raise ValueError("job_not_terminal")
            if view.disposition:
                return view.disposition
            binding = self._binding_of(view)
            file_id = view.file_id
            if not file_id:
                found = self.transport.find_file(binding, self._token(key))
                file_id = found.file_id if found else ""
            if not file_id:
                return "unverified"
            value = "deleted" if self.transport.delete_file(binding, file_id) else "retained"
            _append(self._journal(key), {"event": "disposition", "value": value})
            return value

    def retention_disposition(self, job: mc.JobReceipt) -> str:
        return self._view(self._key_from_ref(job.job_ref)).disposition or "unrecorded"

    # ------------------------------------------------------------------ events, fencing
    def ingest_provider_event(
        self,
        job_id: str,
        state: str,
        tuned_model_id: str | None = None,
        usage: ProviderUsage | None = None,
    ) -> bool:
        """Apply a late provider callback only to a live job. Settled work changes nothing."""
        for path in sorted((self.root / "jobs").glob("*.jsonl")):
            key = path.stem
            with self._locked(key):
                view = self._view(key)
                if view.job_id != job_id:
                    continue
                if view.terminal or view.cancel_requested or self._fenced():
                    return False
                self._absorb(
                    key,
                    view,
                    ProviderJob(
                        job_id,
                        state,
                        "",
                        view.intent_sha,
                        view.file_id,
                        None,
                        tuned_model_id,
                        usage,
                    ),
                )
                return True
        return False

    def fence(self, scope: str) -> FenceReceipt:
        """Durably block new work, then stop live jobs. Repeatable if the provider is down."""
        marker = self.root / "FENCED"
        try:
            descriptor = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            pass
        else:
            try:
                os.write(descriptor, _canonical({"scope": scope}))
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        canceled: list[str] = []
        for path in sorted((self.root / "jobs").glob("*.jsonl")):
            key = path.stem
            with self._locked(key):
                view = self._view(key)
                if view.terminal:
                    continue
                if not view.cancel_requested:
                    _append(self._journal(key), {"event": "cancel_requested"})
                    view = self._view(key)
                binding = self._binding_of(view)
                if not view.job_id:
                    found = self._reconcile(key, view)
                    view = found if found is not None else view
                self._cancel_at_provider(key, view, binding)
                canceled.append(self._job_ref(key))
        return FenceReceipt(
            scope, tuple(canceled), f"simulated:managed-fence:{_sha(scope.encode())[:16]}"
        )
