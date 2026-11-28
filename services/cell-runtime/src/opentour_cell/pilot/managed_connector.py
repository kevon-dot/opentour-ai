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

Trust boundary (stated assumption): the journal has NO integrity protection. Anyone who can write
the journal directory can set ``cap_verified`` or ``maximum_minor_units`` directly, and ``usage``,
``_verified`` and the callback path all trust those journaled values. Every guarantee above assumes
the journal is trusted storage that only this connector can write. Closing that would need
authenticated events (an HMAC or signature over each event, keyed outside the state directory).

Over-cap usage is a hard refusal. Every bounded dimension is compared: money against the
journaled approved maximum, and seconds, rows and steps against the intent's ``FitBound``. A
COMPLETED job with any verified dimension above its bound is held with the fixed reason
``over_cap``: ``collect`` builds no candidate, and ``handoff_reference`` and
``predict_for_replay`` never use its tuned-model id. Usage that cannot be shown to be within
every bound is held the same way (fail closed), under its own fixed reason:
``usage_unverifiable`` when the usage is missing, non-finite, negative, non-integer or bool, or
has the wrong shape (including when read back from the journal), and ``cap_unverifiable`` when it
is in a currency other than the cap's or a bound is missing or invalid. A verified excess wins
over an unusable bound. Usage exactly at a bound is accepted. ``hold_reason`` reports the specific
reason, and ``None`` for a job that is not COMPLETED. The receipt state stays the provider's own
(COMPLETED); the hold is enforced where the result would be released. ``usage`` still returns
the real figure with ``cap_status == "over_cap"`` and ``settleable is False``; no code in this
repository reconciles it, so the reservation owner must handle it (see ``ReportedUsage``).

Known limits of the hold: a terminal observation is final, so usage that arrives after the
provider reports success is never re-read. A job held as ``usage_unverifiable`` therefore has no
recovery path here, and a later upward revision by the provider is not seen. A held job's
tuned-model id is never returned, but it is still written to the 0600 on-disk journal.

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


CAP_WITHIN = "within_cap"
CAP_OVER = "over_cap"
CAP_UNKNOWN = "cap_unknown"


@dataclass(frozen=True)
class ReportedUsage(Usage):
    """Provider-reported usage plus a closed-vocabulary comparison to the approved cap.

    ``incurred_minor_units`` is always the provider's real figure and is never clamped, so genuine
    overspend stays visible, with the provider's own currency. Every bounded dimension is
    compared: money against the journaled approved maximum (same currency only; the match is
    exact and case-sensitive, so ``usd`` is not ``USD``), and seconds, rows and steps against the
    intent's ``FitBound`` (bound to the journal by the intent digest). ``cap_status`` is
    ``over_cap`` when any comparable dimension is above its bound, else ``cap_unknown`` when any
    dimension cannot be compared (no valid bound, no journaled currency, or a different
    currency), else ``within_cap`` (every dimension at or under its bound).
    Anything but ``within_cap`` is not ``settleable``: it must not flow to settlement as ordinary
    actual spend. A consumer must gate on ``settleable`` or ``cap_status``, never on ``source`` or
    ``incurred_minor_units``, which look ordinary on an over-cap job. Nothing in this repository
    consumes ``settleable`` yet, so the guard is advisory until a reservation owner reads it. The
    forward path for an over-cap job (hold the reservation, overspend incident, explicit operator
    record) is tracked as OPE-570; this module offers no reconcile step.
    """

    cap_status: str = CAP_UNKNOWN
    approved_maximum_minor_units: int | None = None
    approved_currency: str | None = None

    @property
    def settleable(self) -> bool:
        return self.cap_status == CAP_WITHIN and super().settleable


def _usage_valid(usage: ProviderUsage) -> bool:
    """Provider-reported usage that is malformed is unknown (``None``), never settleable."""
    counts = (usage.minor_units, usage.seconds, usage.rows, usage.steps)
    return (
        isinstance(usage.currency, str)
        and bool(usage.currency)
        and all(type(count) is int and count >= 0 for count in counts)
    )


# Fixed hold reasons. Each holds a COMPLETED result the same way; they differ only in what an
# operator must investigate: spend above the cap, or spend that cannot be established at all.
HOLD_OVER_CAP = CAP_OVER  # verified usage above the approved cap
HOLD_USAGE_UNVERIFIABLE = "usage_unverifiable"  # usage missing or malformed
HOLD_CAP_UNVERIFIABLE = "cap_unverifiable"  # usage not comparable to the approved cap


# Usage dimensions bounded by ``FitBound`` besides money, as (usage field, bound field). Money is
# compared separately because it also needs a matching currency. Nothing else is compared.
_BOUNDED_DIMENSIONS = (
    ("seconds", "maximum_seconds"),
    ("rows", "maximum_rows"),
    ("steps", "maximum_steps"),
)
_USAGE_FIELDS = frozenset({"currency", "minor_units", "seconds", "rows", "steps"})


def _limit(value: object) -> int | None:
    """A bound is a non-negative ``int`` (never ``bool`` or ``float``); anything else is invalid."""
    return value if type(value) is int and value >= 0 else None


def _approved_cap(view: _View) -> tuple[int | None, str | None]:
    """The journaled approved maximum and currency, or ``None`` for one that is not valid."""
    currency = view.currency if isinstance(view.currency, str) and view.currency else None
    return _limit(view.maximum_minor_units), currency


def _cap_status(view: _View, bound: mc.FitBound) -> str | None:
    """Compare journaled usage to every bound; ``None`` when the usage itself is invalid.

    A verified excess on any dimension is ``over_cap`` even when another dimension cannot be
    compared; otherwise a dimension that cannot be compared makes the result ``cap_unknown``.
    """
    reported = view.usage
    if reported is None or not _usage_valid(reported):
        return None
    approved, approved_currency = _approved_cap(view)
    # Units differ or are unrecorded (exact, case-sensitive match): money is not comparable.
    comparable = approved_currency is not None and reported.currency == approved_currency
    money = approved if comparable else None
    comparisons = [(reported.minor_units, money)] + [
        (getattr(reported, used), _limit(getattr(bound, limit)))
        for used, limit in _BOUNDED_DIMENSIONS
    ]
    if any(limit is not None and used > limit for used, limit in comparisons):
        return CAP_OVER
    if any(limit is None for _, limit in comparisons):
        return CAP_UNKNOWN
    return CAP_WITHIN


def _hold(view: _View, bound: mc.FitBound) -> str | None:
    """A COMPLETED result is released only when its usage is verifiably within every bound.

    Only a job whose receipt reads COMPLETED is held; a CANCELED or UNKNOWN job never yields a
    result anyway and reports no hold. Invalid usage is reported first: a malformed record
    yields no verified figure, so there is nothing to compare whatever the bounds say.
    """
    if not (
        view.terminal
        and view.state is mc.JobState.COMPLETED
        and view.cap_verified
        and not view.cancel_requested
    ):
        return None
    status = _cap_status(view, bound)
    if status is None:
        return HOLD_USAGE_UNVERIFIABLE
    if status == CAP_OVER:
        return HOLD_OVER_CAP
    if status == CAP_UNKNOWN:
        return HOLD_CAP_UNVERIFIABLE
    return None


def _journaled_usage(value: object) -> ProviderUsage | None:
    """Usage read back from the journal; any other shape is unknown, never an error."""
    if isinstance(value, dict) and set(value) == _USAGE_FIELDS:
        return ProviderUsage(**value)
    return None


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
    cap_verified: bool = False
    maximum_minor_units: int | None = None
    currency: str | None = None
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

    @contextmanager
    def _gate(self, *, exclusive: bool) -> Iterator[None]:
        """Orders a new dispatch's first journal write against ``fence`` (no missed job)."""
        descriptor = os.open(
            self.root / "fence.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600
        )
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            yield
        finally:
            os.close(descriptor)

    @staticmethod
    def _write_marker(marker: Path, scope: str) -> None:
        try:
            descriptor = os.open(
                marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        except FileExistsError:
            return
        try:
            os.write(descriptor, _canonical({"scope": scope}))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

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
            if not all(type(value) is str for value in row.values()):
                raise ValueError("training_row_value_not_string")
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
                view.maximum_minor_units = event.get("maximum_minor_units")
                view.currency = event.get("currency")  # absent in older journals
            elif kind == "file":
                view.file_id = event["file_id"]
            elif kind == "job":
                view.job_id = event["job_id"]
            elif kind == "cap_verified":
                view.cap_verified = True
            elif kind == "progress" and not view.terminal:
                view.state = mc.JobState(event["state"])
            elif kind == "terminal" and not view.terminal:
                view.terminal = True
                view.state = mc.JobState(event["state"])
                view.tuned_model_id = event.get("tuned_model_id", "")
                view.usage = _journaled_usage(event.get("usage"))
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
        """Report only what the provider confirmed. A pending cancel is not a stopped job."""
        if view.cancel_requested and view.terminal:
            return mc.JobState.CANCELED  # cancel wins a race with completion: no candidate
        if view.state is mc.JobState.COMPLETED and not view.cap_verified:
            return mc.JobState.UNKNOWN
        return view.state

    def _receipt(self, intent: mc.FitIntent, key: str, view: _View) -> mc.JobReceipt:
        return mc.JobReceipt(
            intent,
            self._state_of(view),
            self._job_ref(key),
            f"simulated:managed-receipt:{key}",
        )

    # ------------------------------------------------------------------ provider state
    def _require_same_job(self, key: str, view: _View, job: ProviderJob) -> None:
        if (
            job.job_id != view.job_id
            or job.client_token != self._token(key)
            or job.intent_sha256 != view.intent_sha
            or (view.file_id and view.file_id != job.file_id)
        ):
            raise ValueError("provider_job_identity_mismatched")

    def _absorb(self, key: str, view: _View, job: ProviderJob) -> None:
        """Fold one provider observation into the journal. A terminal state is final."""
        if view.terminal:
            return
        self._require_same_job(key, view, job)
        state = _STATES.get(job.state, mc.JobState.UNKNOWN)
        if state is mc.JobState.COMPLETED and not job.tuned_model_id:
            state = mc.JobState.UNKNOWN  # success without a usable tuned-model id is not success
        if state in _TERMINAL:
            event: dict[str, Any] = {"event": "terminal", "state": state.value}
            if state is mc.JobState.COMPLETED:
                event["tuned_model_id"] = job.tuned_model_id
            if job.usage is not None and _usage_valid(job.usage):
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
        if (
            not job.job_id
            or job.intent_sha256 != view.intent_sha
            or job.client_token != self._token(key)
            or (view.job_id and view.job_id != job.job_id)
            or (view.file_id and view.file_id != job.file_id)
        ):
            raise ValueError("idempotency_conflict")
        if not view.job_id:
            _append(self._journal(key), {"event": "job", "job_id": job.job_id})
        if job.file_id and not view.file_id:
            _append(self._journal(key), {"event": "file", "file_id": job.file_id})

    def _token(self, key: str) -> str:
        return f"opentour-{key}"

    def _reconcile(
        self, key: str, view: _View, maximum_minor_units: int | None = None
    ) -> _View | None:
        """Read-only toward the provider: find by client token. ``None`` means unverifiable."""
        binding = self._binding_of(view)
        job = self.transport.find_job(binding, self._token(key))
        if job is None:
            return None
        self._adopt(key, view, job)
        view = self._view(key)
        if maximum_minor_units is not None and not view.cancel_requested:
            if not self._confirm_cap(key, view, job, maximum_minor_units):
                return self._view(key)
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
                if not view.job_id or (not view.cap_verified and not view.terminal):
                    reconciled = self._reconcile(key, view, intent.bound.maximum_minor_units)
                    if reconciled is None:
                        raise OSError("dispatch_outcome_unknown")
                    view = reconciled
                return self._receipt(intent, key, view)
            if _sha(development) != intent.development_digest:
                raise ValueError("training_payload_invalid")
            self._check_payload(intent, development)
            with self._gate(exclusive=False):
                self._require_open()  # re-checked: a fence may have landed since the first check
                _append(
                    self._journal(key),
                    {
                        "event": "dispatching",
                        "intent_sha256": digest,
                        "binding": _binding_document(intent.binding),
                        "maximum_minor_units": intent.bound.maximum_minor_units,
                        "currency": intent.bound.currency,
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
            self._adopt(key, self._view(key), job)
            view = self._view(key)
            if not self._confirm_cap(key, view, job, intent.bound.maximum_minor_units):
                raise ValueError("provider_cap_not_enforced")
            self._absorb(key, view, job)
            return self._receipt(intent, key, self._view(key))

    def _check_payload(self, intent: mc.FitIntent, development: bytes) -> None:
        """Defense in depth: the bytes about to leave are re-checked, not trusted by digest."""
        try:
            rows = [json.loads(line)["fields"] for line in development.decode().splitlines()]
        except (ValueError, KeyError, TypeError):
            raise ValueError("training_payload_invalid") from None
        if (
            not rows
            or len(rows) > intent.bound.maximum_rows
            or any(
                not isinstance(row, dict)
                or set(row) != set(intent.fields)
                or set(row) & self.settings.protected_fields
                or not all(type(value) is str for value in row.values())
                for row in rows
            )
        ):
            raise ValueError("training_payload_invalid")
        try:
            canonical_payload = serialize_rows(tuple(rows))
        except (ValueError, TypeError):
            raise ValueError("training_payload_invalid") from None
        if development != canonical_payload:
            raise ValueError("training_payload_invalid")

    def _confirm_cap(
        self, key: str, view: _View, job: ProviderJob, maximum_minor_units: int
    ) -> bool:
        self._require_same_job(key, view, job)
        cap = job.cap_minor_units
        if type(cap) is not int or not 0 <= cap <= maximum_minor_units:
            self._cancel_unbounded(key, view, self._binding_of(view))
            return False
        if not view.cap_verified:
            _append(self._journal(key), {"event": "cap_verified"})
        return True

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
            if not view.job_id or (not view.cap_verified and not view.terminal):
                reconciled = self._reconcile(key, view, intent.bound.maximum_minor_units)
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
                if view.cancel_requested or self._confirm_cap(
                    key, view, provider, job.intent.bound.maximum_minor_units
                ):
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
            or not view.cap_verified
            or view.state is not mc.JobState.COMPLETED
            or not view.tuned_model_id
        ):
            raise ValueError("job_not_collectable")
        hold = _hold(view, job.intent.bound)  # the bound is the journal's, by intent digest
        if hold is not None:
            raise ValueError(hold)
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

    def _verified(self, candidate: mc.Candidate) -> _View:
        """A candidate is only as good as the journal: no hand-built or stale candidate passes."""
        key = self._key_from_ref(candidate.job_ref)
        view = self._view(key)
        if (
            not view.exists
            or view.intent_sha != intent_digest(candidate.intent)
            or view.cancel_requested
            or not view.terminal
            or not view.cap_verified
            or view.state is not mc.JobState.COMPLETED
            or not view.tuned_model_id
            or view.tuned_model_id != candidate.artifact_or_serving_revision
            or candidate.parent_revision != candidate.intent.binding.immutable_revision
        ):
            raise ValueError("candidate_lineage_unverified")
        hold = _hold(view, candidate.intent.bound)
        if hold is not None:
            raise ValueError(hold)  # a held job's tuned-model id is never used or released
        return view

    def hold_reason(self, job: mc.JobReceipt) -> str | None:
        """The fixed reason a COMPLETED job's result is held, else ``None``.

        One of ``over_cap``, ``usage_unverifiable`` or ``cap_unverifiable``. A job whose receipt
        does not read COMPLETED (running, CANCELED, FAILED or UNKNOWN) reports ``None``: it
        yields no result to hold.
        """
        view = self._view(self._key_from_ref(job.job_ref))
        if view.intent_sha != intent_digest(job.intent):
            raise ValueError("job_receipt_mismatched")
        return _hold(view, job.intent.bound)

    def handoff_reference(self, candidate: mc.Candidate, recipient: str) -> str:
        """Release the tuned-model id only to a recipient the customer permitted."""
        self._require_open()
        if recipient not in self.settings.permitted_recipients:
            raise ValueError("recipient_not_permitted")
        return self._verified(candidate).tuned_model_id

    def predict_for_replay(
        self, candidate: mc.Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        self._require_open()
        view = self._verified(candidate)
        if any(set(row) & self.settings.protected_fields for row in inputs):
            raise ValueError("protected_field_in_payload")
        return self.transport.predict(self._binding_of(view), view.tuned_model_id, inputs)

    # ------------------------------------------------------------------ usage, disposition
    def usage(self, job: mc.JobReceipt) -> Usage | None:
        """Provider-reported usage once the job is terminal; ``None`` is unknown, never zero."""
        key = self._key_from_ref(job.job_ref)
        view = self._view(key)
        if view.intent_sha != intent_digest(job.intent):
            raise ValueError("job_receipt_mismatched")
        if not view.terminal or view.usage is None or not _usage_valid(view.usage):
            return None
        reported = view.usage
        state = view.state
        approved, approved_currency = _approved_cap(view)
        status = _cap_status(view, job.intent.bound) or CAP_UNKNOWN
        return ReportedUsage(
            self._job_ref(key),
            reported.currency,
            reported.minor_units,
            reported.seconds,
            reported.rows,
            reported.steps,
            UsageSource.PROVIDER_REPORTED,
            irreversible=reported.minor_units > 0
            and (state is not mc.JobState.COMPLETED or view.cancel_requested),
            cap_status=status,
            approved_maximum_minor_units=approved,
            approved_currency=approved_currency,
        )

    def dispose_training_file(self, job: mc.JobReceipt) -> str:
        """Delete the provider training file after a terminal state and record the disposition."""
        key = self._key_from_ref(job.job_ref)
        with self._locked(key):
            view = self._view(key)
            if view.intent_sha != intent_digest(job.intent):
                raise ValueError("job_receipt_mismatched")
            if not view.terminal:
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
        view = self._view(self._key_from_ref(job.job_ref))
        if view.intent_sha != intent_digest(job.intent):
            raise ValueError("job_receipt_mismatched")
        return view.disposition or "unrecorded"

    # ------------------------------------------------------------------ events, fencing
    def ingest_provider_event(
        self,
        job_id: str,
        state: str,
        tuned_model_id: str | None = None,
        usage: ProviderUsage | None = None,
    ) -> bool:
        """Treat an event for a live job as a hint to get a credentialed observation.

        Only ``job_id`` is used. ``state``, ``tuned_model_id`` and ``usage`` are accepted for call
        compatibility and are IGNORED: the job is re-read from the provider with the credential and
        only that observation can change journaled state. There is no production caller yet; do
        not wire one without authentication and rate limiting.

        PRECONDITION: do not wire this to an inbound route until (1) the callback is authenticated
        and (2) calls are rate limited per source and per job. Today anyone who knows or guesses a
        live job id can force a credentialed ``get_job`` under the per-key lock (quota and
        latency cost), the boolean result is an oracle for whether a job exists and changed state,
        and every call scans and parses all journals (O(n)); a job-id to key index is also needed.
        """
        for path in sorted((self.root / "jobs").glob("*.jsonl")):
            key = path.stem
            with self._locked(key):
                view = self._view(key)
                if view.job_id != job_id:
                    continue
                if (
                    view.terminal
                    or view.cancel_requested
                    or not view.cap_verified
                    or view.maximum_minor_units is None
                    or self._fenced()
                ):
                    return False
                try:
                    observed = self.transport.get_job(self._binding_of(view), job_id)
                    if self._confirm_cap(key, view, observed, view.maximum_minor_units):
                        self._absorb(key, view, observed)
                except (OSError, LookupError, ValueError):
                    return False
                refreshed = self._view(key)
                return refreshed.state != view.state or refreshed.terminal != view.terminal
        return False

    def fence(self, scope: str) -> FenceReceipt:
        """Durably block new work, then stop live jobs. Repeatable if the provider is down."""
        with self._gate(exclusive=True):  # waits for any dispatch that has not journaled yet
            self._write_marker(self.root / "FENCED", scope)
        canceled: list[str] = []
        unstopped: list[str] = []
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
                if self._view(key).terminal:
                    canceled.append(self._job_ref(key))
                else:
                    unstopped.append(self._job_ref(key))
        if unstopped:
            # The fence itself is durable; never report a job stopped that the provider has not.
            raise ValueError("FENCE_JOB_NOT_STOPPED")
        return FenceReceipt(
            scope, tuple(canceled), f"simulated:managed-fence:{_sha(scope.encode())[:16]}"
        )
