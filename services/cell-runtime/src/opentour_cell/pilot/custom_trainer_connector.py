"""Customer-trainer connector: the ``Route.CUSTOM`` route behind the OPE-506 connector contract.

A customer who already runs a trainer plugs it in by implementing ``TrainerTransport`` (the public,
versioned job adapter, ``ADAPTER_VERSION``). ``CustomTrainerConnector`` implements
``model_connector.Connector`` over that transport and is driven only through ``Preflight``. The
trainer runs in the customer-controlled boundary and its lifecycle is not ours: lost or ambiguous
job state is the dominant risk, so every uncertain outcome is reconciled through a read-only
``lookup`` by client token and never retried or inferred as success.

Versioning is explicit. The adapter version, trainer version, task serializer version, scorer
version and replay declaration a trainer reports must match what the tenant registered
(``TrainerRegistration``). Any mismatch or unknown value refuses with a fixed lowercase reason and
is surfaced by ``check_compatibility``; nothing is coerced, guessed or silently degraded.

Durability: a file-backed append-only journal keyed by ``sha256(idempotency_key)`` records intent
before any call to the trainer, the trainer acknowledgement, cancel intent before cancellation,
terminal state, collection and scope fences. Terminal state is final: a late callback or poll for a
settled job changes nothing. A job whose submit outcome is unconfirmed is never resent.

Simulated: tests drive this module with a generated sentinel trainer written independently of it
(``reference_customer_trainer``) and with fault-injecting wrappers. Credentials are generated
placeholders resolved server-side and never logged or placed in errors.
Not proven: no real customer trainer, GPU, paid compute, provider or real credential is touched.
Tests prove software behavior only and establish no provider or customer evidence. Passing here
yields ``EvidenceTier.FIXTURE`` at most (see ``integration_tier``); real-trainer qualification and
customer witnessing are unsatisfied external gates. ``CUSTOMER_WITNESSED`` is unreachable until an
issuer-authenticated ``CustomerWitnessVerifier`` is injected (pending OPE-566 and OPE-582).
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import time
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.connector_ext import EvidenceTier, FenceReceipt, Usage, UsageSource

VERSION = "opentour.pilot.custom-trainer-connector.v1"
ADAPTER_VERSION = "opentour.pilot.trainer-adapter.v1"
_TERMINAL = frozenset({"completed", "failed", "canceled"})
_REPLAY_MODES = frozenset({"deterministic", "tolerance"})


class Credential:
    """A server-side secret handle. The value never appears in ``repr`` or errors."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    @property
    def value(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "Credential(<redacted>)"

    __str__ = __repr__


@dataclass(frozen=True)
class TrainerManifest:
    """What the customer's trainer declares about itself. Never trusted without a registration."""

    adapter_version: str
    trainer_version: str
    model_revision: str
    supported_tasks: frozenset[str]
    serializer_version: str
    scorer_version: str
    replay_mode: str
    replay_tolerance: float
    training: bool
    cancellation: bool
    progress: bool
    stop_semantics: bool
    hard_cost_cap: bool
    checkpoint_recovery: bool


@dataclass(frozen=True)
class TrainerJobRequest:
    adapter_version: str
    client_token: str
    task: str
    model_revision: str
    serializer_version: str
    scorer_version: str
    benchmark_revision: str
    development_digest: str
    development: bytes
    bound: mc.FitBound


@dataclass(frozen=True)
class TrainerUsage:
    currency: str
    minor_units: int
    seconds: int
    rows: int
    steps: int


@dataclass(frozen=True)
class TrainerView:
    job_id: str
    client_token: str
    state: str
    usage: TrainerUsage | None = None


@dataclass(frozen=True)
class TrainerResult:
    job_id: str
    client_token: str
    trainer_version: str
    parent_revision: str
    artifact_ref: str
    artifact_digest: str
    signature: str


class TrainerTransport(Protocol):
    """The public job adapter. Every call is authorized by a server-side ``Credential``."""

    def describe(self, credential: Credential) -> TrainerManifest: ...

    def quote(self, credential: Credential, task: str, bound: mc.FitBound) -> mc.FitBound | None:
        """Return the trainer's own maximum for this bound, or ``None`` when it cannot bound it."""
        ...

    def start(self, credential: Credential, request: TrainerJobRequest) -> TrainerView:
        """Idempotent on ``request.client_token``: a duplicate starts no second execution."""
        ...

    def find(self, credential: Credential, client_token: str) -> TrainerView | None:
        """Read-only lookup by client token. Never creates or resends a job."""
        ...

    def poll(self, credential: Credential, job_id: str) -> TrainerView: ...

    def request_cancel(self, credential: Credential, job_id: str) -> TrainerView: ...

    def fetch_result(self, credential: Credential, job_id: str) -> TrainerResult: ...

    def predict(
        self, credential: Credential, artifact_ref: str, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]: ...


@dataclass(frozen=True)
class TrainerRegistration:
    """A tenant-registered destination and the exact versions the owner accepts for it."""

    tenant: str
    account_or_host: str
    supported_adapter_versions: frozenset[str]
    supported_trainer_versions: frozenset[str]
    serializer_version: str
    scorer_version: str
    replay_mode: str
    replay_tolerance: float = 0.0
    protected_fields: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Compatibility:
    ok: bool
    reason: str
    adapter_version: str
    trainer_version: str


def check_compatibility(
    registration: TrainerRegistration, manifest: TrainerManifest
) -> Compatibility:
    """Compare declared versions with the registration. Never coerces; returns a fixed reason."""

    def verdict(reason: str) -> Compatibility:
        return Compatibility(
            reason == "compatible", reason, manifest.adapter_version, manifest.trainer_version
        )

    if not manifest.adapter_version:
        return verdict("adapter_version_unknown")
    if manifest.adapter_version not in registration.supported_adapter_versions:
        return verdict("adapter_version_mismatch")
    if not manifest.trainer_version:
        return verdict("trainer_version_unknown")
    if manifest.trainer_version not in registration.supported_trainer_versions:
        return verdict("trainer_version_unsupported")
    if manifest.serializer_version != registration.serializer_version:
        return verdict("serializer_version_mismatch")
    if manifest.scorer_version != registration.scorer_version:
        return verdict("scorer_version_mismatch")
    if (
        registration.replay_mode not in _REPLAY_MODES
        or manifest.replay_mode != registration.replay_mode
        or manifest.replay_tolerance != registration.replay_tolerance
    ):
        return verdict("replay_declaration_mismatch")
    return verdict("compatible")


# ---- Integration evidence tier ----
#
# The tiers are FIXTURE < SOFTWARE_QUALIFIED < CUSTOMER_WITNESSED. A caller can claim anything, so
# every step up fails closed:
#
# * A generated, simulated, reference or fixture trainer is FIXTURE whatever is supplied. The
#   identity is NFKC-normalised and casefolded before it is matched, and an identity carrying
#   invisible, control or non-ASCII characters is refused outright (FIXTURE), so case, fullwidth,
#   zero-width and homoglyph variants of a fixture marker cannot pass as a customer trainer.
# * SOFTWARE_QUALIFIED needs a non-blank qualification reference (still caller-asserted).
# * CUSTOMER_WITNESSED needs software qualification plus a structured ``CustomerWitness`` record
#   (never a bare string) bound to the exact tenant, engagement, trainer identity, trainer revision
#   and job or run under classification, naming the customer-owner witness role, and authenticated
#   by an injected ``CustomerWitnessVerifier``. No issuer-authenticated witness exists yet (the
#   OPE-566 verifier and the OPE-582 issuer keys are pending), so no verifier is wired and this
#   module never builds one: without a verifier, or when the verifier refuses or raises, the result
#   is at most SOFTWARE_QUALIFIED with the reason ``customer_witness_unverifiable`` or
#   ``customer_witness_refused``.

WITNESS_ROLE_CUSTOMER_OWNER = "customer_owner"
_WITNESS_ROLES = frozenset({WITNESS_ROLE_CUSTOMER_OWNER})
# Any one of these tokens anywhere in a normalised trainer identity marks a fixture trainer.
_FIXTURE_TOKENS = frozenset({"generated", "simulated", "reference", "fixture", "sentinel"})
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Co", "Cs", "Cn", "Zl", "Zp"})


@dataclass(frozen=True)
class WitnessSubject:
    """The exact integration a witness must be about, supplied by the classifying caller."""

    tenant: str
    engagement: str
    trainer_id: str
    trainer_revision: str
    job_ref: str


@dataclass(frozen=True)
class CustomerWitness:
    """A structured customer-witness record. Unverified data until a verifier authenticates it.

    ``attestation`` is the opaque issuer evidence (for example signatures under the OPE-566
    profile) that a ``CustomerWitnessVerifier`` checks. This module never interprets it.
    """

    tenant: str
    engagement: str
    trainer_id: str
    trainer_revision: str
    job_ref: str
    witness_role: str
    attestation: str


class CustomerWitnessVerifier(Protocol):
    """Injection seam for an issuer-authenticated customer-witness verifier.

    Must return exactly ``True`` only when ``witness`` is authenticated by its issuer and is about
    ``subject``. Any other return value, and any exception, is a refusal. No implementation exists
    in this module; one is pending OPE-566 and the OPE-582 key ceremony.
    """

    def verify_customer_witness(
        self, witness: CustomerWitness, subject: WitnessSubject
    ) -> bool: ...


@dataclass(frozen=True)
class IntegrationVerdict:
    tier: EvidenceTier
    reason: str


def _invisible(text: str) -> bool:
    return any(unicodedata.category(ch) in _INVISIBLE_CATEGORIES for ch in text)


def _trainer_key(origin: object) -> str | None:
    """NFKC, casefold and strip a trainer identity; ``None`` when it is not a clean identity."""
    if not isinstance(origin, str) or _invisible(origin):
        return None
    key = unicodedata.normalize("NFKC", origin).casefold().strip()
    if not key or _invisible(key) or not all(" " <= ch <= "~" for ch in key):
        return None
    return key


def is_fixture_trainer(origin: object) -> bool:
    """True for a generated, simulated, reference or fixture trainer, or an unclean identity."""
    key = _trainer_key(origin)
    if key is None:
        return True
    tokens = {token for token in re.split(r"[^a-z0-9]+", key) if token}
    return bool(tokens & _FIXTURE_TOKENS) or key.rstrip(".").endswith(".invalid")


def _clean_text(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and not _invisible(value)


def classify_integration(
    trainer_origin: str,
    qualification_ref: str | None,
    customer_witness: CustomerWitness | None = None,
    *,
    subject: WitnessSubject | None = None,
    witness_verifier: CustomerWitnessVerifier | None = None,
) -> IntegrationVerdict:
    """Classify an integration with a fixed reason. Fails closed at every step up the ladder."""
    if _trainer_key(trainer_origin) is None:
        return IntegrationVerdict(EvidenceTier.FIXTURE, "trainer_identity_unverifiable")
    if is_fixture_trainer(trainer_origin):
        return IntegrationVerdict(EvidenceTier.FIXTURE, "fixture_trainer")
    if not _clean_text(qualification_ref):
        return IntegrationVerdict(EvidenceTier.FIXTURE, "software_qualification_absent")

    def capped(reason: str) -> IntegrationVerdict:
        return IntegrationVerdict(EvidenceTier.SOFTWARE_QUALIFIED, reason)

    witness = customer_witness
    if witness is None:
        return capped("customer_witness_absent")
    if type(witness) is not CustomerWitness:
        return capped("customer_witness_unstructured")
    bound = (
        witness.tenant,
        witness.engagement,
        witness.trainer_id,
        witness.trainer_revision,
        witness.job_ref,
    )
    if not all(_clean_text(value) for value in (*bound, witness.witness_role, witness.attestation)):
        return capped("customer_witness_malformed")
    if witness.witness_role not in _WITNESS_ROLES:
        return capped("customer_witness_role_refused")
    if (
        type(subject) is not WitnessSubject
        or subject.trainer_id != trainer_origin
        or bound
        != (
            subject.tenant,
            subject.engagement,
            subject.trainer_id,
            subject.trainer_revision,
            subject.job_ref,
        )
    ):
        return capped("customer_witness_binding_mismatch")
    if witness_verifier is None:
        return capped("customer_witness_unverifiable")
    try:
        verified = witness_verifier.verify_customer_witness(witness, subject)
    except Exception:  # any verifier failure is a refusal, never "skip and continue"
        return capped("customer_witness_refused")
    if verified is not True:
        return capped("customer_witness_refused")
    return IntegrationVerdict(EvidenceTier.CUSTOMER_WITNESSED, "customer_witness_verified")


def integration_tier(
    trainer_origin: str,
    qualification_ref: str | None,
    customer_witness: CustomerWitness | None = None,
    *,
    subject: WitnessSubject | None = None,
    witness_verifier: CustomerWitnessVerifier | None = None,
) -> EvidenceTier:
    """The tier alone; ``classify_integration`` holds the rules and gives the reason."""
    return classify_integration(
        trainer_origin,
        qualification_ref,
        customer_witness,
        subject=subject,
        witness_verifier=witness_verifier,
    ).tier


def sign_result(secret: str, result: TrainerResult | Mapping[str, str]) -> str:
    """The signature a trainer must attach to its result (HMAC-SHA256 under the shared secret)."""
    data = result if isinstance(result, Mapping) else asdict(result)
    # Length-unambiguous encoding: a delimiter inside one field cannot shift into the next.
    body = _canon(
        [
            data[key]
            for key in (
                "job_id",
                "client_token",
                "trainer_version",
                "parent_revision",
                "artifact_ref",
                "artifact_digest",
            )
        ]
    )
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def replay_matches(
    first: Sequence[Mapping[str, object]],
    second: Sequence[Mapping[str, object]],
    mode: str,
    tolerance: float,
) -> bool:
    """Deterministic equality, or equality within the declared numeric tolerance.

    Fails closed: an unknown mode, a negative or non-finite tolerance, and NaN or infinite drift
    never match (``abs(nan) > tolerance`` is False, so the comparison is written as ``not <=``).
    """
    if mode not in _REPLAY_MODES or not math.isfinite(tolerance) or tolerance < 0:
        return False
    if len(first) != len(second):
        return False
    for left, right in zip(first, second, strict=True):
        if left.keys() != right.keys():
            return False
        for key, value in left.items():
            other = right[key]
            numeric = all(
                isinstance(item, (int, float)) and not isinstance(item, bool)
                for item in (value, other)
            )
            if mode == "tolerance" and numeric:
                if not abs(float(value) - float(other)) <= tolerance:  # type: ignore[arg-type]
                    return False
            elif value != other:
                return False
    return True


def _canon(value: Any) -> str:
    def normal(item: Any) -> Any:
        if isinstance(item, (set, frozenset)):
            return sorted(normal(entry) for entry in item)
        if isinstance(item, StrEnum):
            return item.value
        if isinstance(item, Mapping):
            return {str(key): normal(entry) for key, entry in item.items()}
        if isinstance(item, (list, tuple)):
            return [normal(entry) for entry in item]
        return item

    return json.dumps(normal(value), sort_keys=True, separators=(",", ":"))


def _token(intent: mc.FitIntent) -> str:
    return hashlib.sha256(intent.idempotency_key.encode()).hexdigest()


def _intent_digest(intent: mc.FitIntent) -> str:
    return hashlib.sha256(_canon(asdict(intent)).encode()).hexdigest()


@dataclass
class _Entry:
    tenant: str = ""
    credential_ref: str = ""
    intent_digest: str = ""
    job_id: str = ""
    trainer_version: str = ""
    state: str = ""
    cancel_requested: bool = False
    artifact_ref: str = ""
    artifact_digest: str = ""


@dataclass
class _Fold:
    jobs: dict[str, _Entry] = field(default_factory=dict)
    fenced: set[str] = field(default_factory=set)


class _Journal:
    """Append-only JSONL, flock-serialized; re-folding after a restart yields the same state."""

    def __init__(self, root: Path) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "trainer-journal.jsonl"
        self.path.touch(exist_ok=True)

    def _read(self) -> _Fold:
        fold = _Fold()
        for line in self.path.read_text().splitlines():
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue  # a torn final write carries no durable fact
            kind = record.get("t")
            if kind == "fence":
                fold.fenced.add(record["scope"])
                continue
            entry = fold.jobs.setdefault(record["k"], _Entry())
            if kind == "intent":
                entry.tenant, entry.intent_digest = record["tenant"], record["digest"]
                entry.credential_ref = record["credential_ref"]
            elif kind == "accepted" and not entry.job_id:
                entry.job_id, entry.trainer_version = record["job_id"], record["trainer_version"]
            elif kind == "cancel_intent":
                entry.cancel_requested = True
            elif kind == "state" and entry.state not in _TERMINAL:
                entry.state = record["state"]
            elif kind == "collected" and not entry.artifact_ref:  # first collection is final
                entry.artifact_ref, entry.artifact_digest = record["ref"], record["digest"]
        return fold

    def read(self) -> _Fold:
        with self.path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            return self._read()

    def append(self, record: dict[str, Any]) -> _Fold:
        return self.transact(lambda fold: [record])

    def transact(self, plan: Callable[[_Fold], list[dict[str, Any]]]) -> _Fold:
        """Decide and append atomically under an exclusive lock."""
        with self.path.open("a+") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            fold = self._read()
            records = plan(fold)
            if records:
                for record in records:
                    handle.write(_canon(record) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
                fold = self._read()
            return fold


_STATE = {
    "queued": mc.JobState.QUEUED,
    "running": mc.JobState.RUNNING,
    "completed": mc.JobState.COMPLETED,
    "canceled": mc.JobState.CANCELED,
    "failed": mc.JobState.FAILED,
}


class CustomTrainerConnector:
    """``model_connector.Connector`` for a customer-operated trainer. See the module docstring."""

    def __init__(
        self,
        transport: TrainerTransport,
        registrations: Sequence[TrainerRegistration],
        credentials: Callable[[str, str], Credential | None],
        state_dir: Path,
        *,
        clock: Callable[[], float] = time.time,
        observation_ttl_seconds: int = 300,
    ) -> None:
        self.transport = transport
        self.registrations = tuple(registrations)
        self.credentials = credentials
        self.clock = clock
        self.ttl = observation_ttl_seconds
        self.journal = _Journal(state_dir)

    # ---- resolution (tenant-registered destination, server-side credential) ----

    def _registration(self, binding: mc.ModelBinding) -> TrainerRegistration:
        for registration in self.registrations:
            if (registration.tenant, registration.account_or_host) == (
                binding.tenant,
                binding.account_or_host,
            ):
                return registration
        raise ValueError("destination_not_registered")

    def _credential(self, binding: mc.ModelBinding) -> Credential:
        try:
            credential = self.credentials(binding.tenant, binding.credential_ref)
        except (OSError, LookupError):
            credential = None
        if credential is None or not credential.value:
            raise ValueError("credential_unavailable")
        return credential

    def _require_custom(self, binding: mc.ModelBinding) -> None:
        if binding.route is not mc.Route.CUSTOM:
            raise ValueError("route_not_supported")

    def _gate(
        self, binding: mc.ModelBinding
    ) -> tuple[TrainerRegistration, Credential, TrainerManifest]:
        """Resolve, authorize and re-verify versions. Fails closed with a fixed reason."""
        self._require_custom(binding)
        registration = self._registration(binding)
        credential = self._credential(binding)
        manifest = self.transport.describe(credential)
        compatibility = check_compatibility(registration, manifest)
        if not compatibility.ok:
            raise ValueError(compatibility.reason)
        return registration, credential, manifest

    def check_compatibility(self, binding: mc.ModelBinding) -> Compatibility:
        """Surface the exact version verdict for an owner; transport faults still raise OSError."""
        self._require_custom(binding)
        registration = self._registration(binding)
        manifest = self.transport.describe(self._credential(binding))
        return check_compatibility(registration, manifest)

    def _open(self, binding: mc.ModelBinding) -> None:
        if binding.tenant in self.journal.read().fenced:
            raise ValueError("connector_fenced")

    # ---- Connector ----

    def observe(self, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Observation:
        _, _, manifest = self._gate(binding)
        supported = binding.task in manifest.supported_tasks and bool(manifest.model_revision)
        # Honest capability: the cap is hard only when the trainer can stop and cancel the job.
        hard_cap = manifest.hard_cost_cap and manifest.stop_semantics and manifest.cancellation
        now = int(self.clock())
        return mc.Observation(
            binding=binding,
            source_ref=f"trainer-manifest:{manifest.adapter_version}:{manifest.trainer_version}",
            observed_at=now,
            expires_at=now + self.ttl,
            resolved_revision=manifest.model_revision or None,
            inference_supported=supported,
            training_supported=supported and manifest.training,
            hard_cost_cap=hard_cap,
            cancellation=manifest.cancellation,
            progress=manifest.progress,
            checkpoint_recovery=manifest.checkpoint_recovery,
            capture={},
        )

    def serialize_development(
        self, intent: mc.FitIntent, rows: tuple[Mapping[str, object], ...]
    ) -> bytes:
        registration = self._registration(intent.binding)
        if intent.fields & registration.protected_fields:
            raise ValueError("protected_field_in_development")
        if not rows or any(set(row) != set(intent.fields) for row in rows):
            raise ValueError("development_rows_invalid")
        payload = {
            "serializer": registration.serializer_version,
            "task": intent.binding.task,
            "input_schema": intent.binding.input_schema,
            "output_schema": intent.binding.output_schema,
            "preprocessing": intent.binding.preprocessing_revision,
            "fields": sorted(intent.fields),
            "row_count": len(rows),
            "rows": [dict(row) for row in rows],
        }
        return _canon(payload).encode()

    @staticmethod
    def _check_payload(
        intent: mc.FitIntent, registration: TrainerRegistration, development: bytes
    ) -> None:
        """Re-validate the outbound bytes: no protected field, in-bound rows, exact shape."""
        if intent.fields & registration.protected_fields:
            raise ValueError("protected_field_in_development")
        try:
            payload = json.loads(development)
        except (ValueError, UnicodeDecodeError):
            raise ValueError("development_payload_invalid") from None
        keys = {
            "serializer",
            "task",
            "input_schema",
            "output_schema",
            "preprocessing",
            "fields",
            "row_count",
            "rows",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) != keys
            or _canon(payload).encode() != development
            or payload["serializer"] != registration.serializer_version
            or payload["task"] != intent.binding.task
            or payload["input_schema"] != intent.binding.input_schema
            or payload["output_schema"] != intent.binding.output_schema
            or payload["preprocessing"] != intent.binding.preprocessing_revision
            or payload["fields"] != sorted(intent.fields)
        ):
            raise ValueError("development_payload_invalid")
        rows = payload["rows"]
        if (
            not isinstance(rows, list)
            or not 0 < len(rows) <= intent.bound.maximum_rows
            or payload["row_count"] != len(rows)
            or any(not isinstance(row, dict) or set(row) != set(intent.fields) for row in rows)
        ):
            raise ValueError("development_rows_invalid")

    def estimate(self, intent: mc.FitIntent) -> mc.FitBound | None:
        _, credential, _ = self._gate(intent.binding)
        return self.transport.quote(credential, intent.binding.task, intent.bound)

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        binding = intent.binding
        self._open(binding)
        registration, credential, manifest = self._gate(binding)
        if hashlib.sha256(development).hexdigest() != intent.development_digest:
            raise ValueError("development_digest_mismatch")
        if not manifest.training or binding.task not in manifest.supported_tasks:
            raise ValueError("task_unsupported")
        if manifest.model_revision != binding.immutable_revision:
            raise ValueError("installed_model_mismatch")
        # The digest only proves the bytes are the ones the caller meant; check what they contain.
        self._check_payload(intent, registration, development)
        token, digest = _token(intent), _intent_digest(intent)

        created = False

        def plan(fold: _Fold) -> list[dict[str, Any]]:
            nonlocal created
            entry = fold.jobs.get(token)
            if entry is not None and entry.intent_digest != digest:
                raise ValueError("idempotency_conflict")
            if entry is not None:
                return []
            if binding.tenant in fold.fenced:  # decided under the same lock as the fence marker
                raise ValueError("connector_fenced")
            created = True
            return [
                {
                    "t": "intent",
                    "k": token,
                    "tenant": binding.tenant,
                    "credential_ref": binding.credential_ref,
                    "digest": digest,
                }
            ]

        entry = self.journal.transact(plan).jobs[token]
        if not created:
            # Duplicate dispatch: never start a second execution.
            return self._duplicate(intent, token, entry, credential)
        view = self.transport.start(  # OSError here leaves intent-only: reconciled, never resent
            credential,
            TrainerJobRequest(
                ADAPTER_VERSION,
                token,
                binding.task,
                binding.immutable_revision,
                registration.serializer_version,
                registration.scorer_version,
                intent.benchmark_revision,
                intent.development_digest,
                development,
                intent.bound,
            ),
        )
        return self._adopt(intent, token, view, manifest.trainer_version)

    def _duplicate(
        self, intent: mc.FitIntent, token: str, entry: _Entry, credential: Credential
    ) -> mc.JobReceipt:
        if not entry.job_id:
            found = self.transport.find(credential, token)
            if found is None:
                raise OSError("dispatch_unconfirmed")
            return self._adopt(intent, token, found, self._trainer_version(intent))
        return self._receipt(intent, token, entry)

    def _trainer_version(self, intent: mc.FitIntent) -> str:
        return self._gate(intent.binding)[2].trainer_version

    def _adopt(
        self, intent: mc.FitIntent, token: str, view: TrainerView, trainer_version: str
    ) -> mc.JobReceipt:
        if view.client_token != token or not view.job_id:
            raise ValueError("trainer_identity_mismatch")
        fold = self.journal.append(
            {"t": "accepted", "k": token, "job_id": view.job_id, "trainer_version": trainer_version}
        )
        entry = fold.jobs[token]
        if entry.job_id != view.job_id:
            raise ValueError("trainer_identity_mismatch")
        return self._settle(intent, token, entry, view)

    def _receipt(
        self, intent: mc.FitIntent, token: str, entry: _Entry, live: str | None = None
    ) -> mc.JobReceipt:
        name = entry.state if entry.state in _TERMINAL else (live or "queued")
        state = _STATE.get(name, mc.JobState.UNKNOWN)
        if state is mc.JobState.UNKNOWN:
            return mc.JobReceipt(intent, state, entry.job_id, "")
        return mc.JobReceipt(intent, state, entry.job_id, f"trainer-receipt:{token[:16]}:{name}")

    def _settle(
        self, intent: mc.FitIntent, token: str, entry: _Entry, view: TrainerView
    ) -> mc.JobReceipt:
        """Fold one observed trainer view into the journal. Terminal state is never reopened."""
        if entry.state in _TERMINAL:
            return self._receipt(intent, token, entry)
        if view.client_token != token or view.job_id != entry.job_id:
            raise ValueError("trainer_identity_mismatch")
        observed = view.state if view.state in _STATE else "unknown"
        if view.usage is not None and not _within(view.usage, intent.bound):
            # Over the bound, malformed, or in a foreign currency: the cap can no longer be
            # verified. Stop the job. FAILED is claimed only once the trainer says it stopped:
            # an unhonored stop leaves the job RUNNING (still spending) with durable cancel intent.
            self.journal.append({"t": "cancel_intent", "k": token})
            stopped = self.transport.request_cancel(self._credential(intent.binding), entry.job_id)
            self._same_job(stopped, token, entry)
            if observed in _TERMINAL or stopped.state in _TERMINAL:
                observed = "failed"
        elif entry.cancel_requested and observed == "completed":
            observed = "canceled"  # a stopped job never publishes a winner
        if observed in _TERMINAL:
            entry = self.journal.append({"t": "state", "k": token, "state": observed}).jobs[token]
        return self._receipt(intent, token, entry, live=observed)

    @staticmethod
    def _same_job(view: TrainerView, token: str, entry: _Entry) -> None:
        """A trainer reply about some other job proves nothing about this one."""
        if view.client_token != token or view.job_id != entry.job_id:
            raise ValueError("trainer_identity_mismatch")

    def lookup(self, intent: mc.FitIntent) -> mc.JobReceipt | None:
        """Read-only. Recovers a lost acknowledgement from the trainer by client token."""
        binding = intent.binding
        self._require_custom(binding)
        credential = self._credential(binding)
        token = _token(intent)
        entry = self.journal.read().jobs.get(token)
        if entry is not None and entry.intent_digest != _intent_digest(intent):
            raise ValueError("idempotency_conflict")
        if entry is not None and entry.state in _TERMINAL:
            return self._receipt(intent, token, entry)
        view = self.transport.find(credential, token)
        if view is None:
            return None  # cannot prove the trainer never got it: UNKNOWN, never a retry
        if entry is None:
            return None  # an unrecorded intent is not ours to adopt
        if view.client_token != token or not view.job_id:
            raise ValueError("trainer_identity_mismatch")  # never journal a foreign identity
        if not entry.job_id:
            self.journal.append(
                {
                    "t": "accepted",
                    "k": token,
                    "job_id": view.job_id,
                    "trainer_version": self._trainer_version(intent),
                }
            )
            entry = self.journal.read().jobs[token]
        if view.client_token != token or view.job_id != entry.job_id:
            raise ValueError("trainer_identity_mismatch")
        return self._receipt(intent, token, entry, live=view.state if view.state in _STATE else "")

    def status(self, job: mc.JobReceipt) -> mc.JobReceipt:
        intent = job.intent
        token = _token(intent)
        entry = self._known(job, token)
        if entry.state in _TERMINAL:
            return self._receipt(intent, token, entry)
        view = self.transport.poll(self._credential(intent.binding), entry.job_id)
        return self._settle(intent, token, entry, view)

    def _known(self, job: mc.JobReceipt, token: str) -> _Entry:
        entry = self.journal.read().jobs.get(token)
        if (
            entry is None
            or not entry.job_id
            or entry.job_id != job.job_ref
            or entry.intent_digest != _intent_digest(job.intent)
        ):
            raise LookupError("job_unknown")
        return entry

    def cancel(self, job: mc.JobReceipt) -> mc.JobReceipt:
        intent = job.intent
        token = _token(intent)
        entry = self._known(job, token)
        if entry.state in _TERMINAL:
            return self._receipt(intent, token, entry)
        # Stopping spend must never wait on version agreement: only the destination and the
        # credential are resolved, and the trainer must still declare that it can cancel.
        self._require_custom(intent.binding)
        self._registration(intent.binding)
        credential = self._credential(intent.binding)
        if not self.transport.describe(credential).cancellation:
            raise ValueError("cancellation_unsupported")
        self.journal.append({"t": "cancel_intent", "k": token})  # durable before the stop
        view = self.transport.request_cancel(credential, entry.job_id)
        self._same_job(view, token, entry)
        outcome = {"canceled": "canceled", "completed": "canceled", "failed": "failed"}.get(
            view.state
        )
        if outcome:
            entry = self.journal.append({"t": "state", "k": token, "state": outcome}).jobs[token]
        else:
            entry = self.journal.read().jobs[token]
        return self._receipt(intent, token, entry, live="running")

    def collect(self, job: mc.JobReceipt) -> mc.Candidate:
        intent = job.intent
        token = _token(intent)
        entry = self._known(job, token)
        if intent.binding.tenant in self.journal.read().fenced:
            raise ValueError("connector_fenced")
        if entry.cancel_requested or entry.state == "canceled":
            raise ValueError("job_canceled")
        if entry.state != "completed":
            raise ValueError("job_not_completed")
        # Versions are re-verified with the trainer now, not taken from the result it hands back.
        _, credential, manifest = self._gate(intent.binding)
        if manifest.trainer_version != entry.trainer_version:
            raise ValueError("trainer_version_changed")
        result = self.transport.fetch_result(credential, entry.job_id)
        if (
            result.job_id != entry.job_id
            or result.client_token != token
            or result.trainer_version != entry.trainer_version
        ):
            raise ValueError("trainer_identity_mismatch")
        if result.parent_revision != intent.binding.immutable_revision:
            raise ValueError("parent_revision_mismatch")
        if not result.artifact_ref or not result.artifact_digest:
            raise ValueError("candidate_artifact_missing")
        if not hmac.compare_digest(result.signature, sign_result(credential.value, result)):
            raise ValueError("result_signature_invalid")
        if entry.artifact_ref and (
            (result.artifact_ref, result.artifact_digest)
            != (entry.artifact_ref, entry.artifact_digest)
        ):
            raise ValueError("candidate_artifact_changed")  # the first collection is final
        if not entry.artifact_ref:
            self.journal.append(
                {
                    "t": "collected",
                    "k": token,
                    "ref": result.artifact_ref,
                    "digest": result.artifact_digest,
                }
            )
        evidence = hashlib.sha256(result.signature.encode()).hexdigest()[:32]
        return mc.Candidate(
            intent,
            entry.job_id,
            result.parent_revision,
            result.artifact_ref,
            f"replay-manifest:{evidence}",
        )

    def predict_for_replay(
        self, candidate: mc.Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        intent = candidate.intent
        binding = intent.binding
        self._open(binding)
        entry = self.journal.read().jobs.get(_token(intent))
        if (
            entry is None
            or entry.intent_digest != _intent_digest(intent)
            or entry.job_id != candidate.job_ref
            or not entry.artifact_ref
            or entry.artifact_ref != candidate.artifact_or_serving_revision
        ):
            raise ValueError("candidate_not_collected")
        if entry.cancel_requested or entry.state != "completed":
            raise ValueError("job_canceled")
        registration, credential, _ = self._gate(binding)
        first = self.transport.predict(credential, entry.artifact_ref, inputs)
        second = self.transport.predict(credential, entry.artifact_ref, inputs)
        if len(first) != len(inputs) or not replay_matches(
            first, second, registration.replay_mode, registration.replay_tolerance
        ):
            raise ValueError("replay_nondeterministic")
        return first

    # ---- shared usage and fencing capabilities ----

    def usage(self, job: mc.JobReceipt) -> Usage | None:
        """Trainer-reported usage. ``None`` means unknown, never zero."""
        intent = job.intent
        entry = self._known(job, _token(intent))
        view = self.transport.poll(self._credential(intent.binding), entry.job_id)
        if view.usage is None or view.job_id != entry.job_id or view.client_token != _token(intent):
            return None
        reported = view.usage
        return Usage(  # raises ValueError("INVALID_USAGE") on malformed trainer values
            entry.job_id,
            reported.currency,
            reported.minor_units,
            reported.seconds,
            reported.rows,
            reported.steps,
            UsageSource.PROVIDER_REPORTED,
        )

    def fence(self, scope: str) -> FenceReceipt:
        """Durably block new submit/collect/replay for a tenant, then stop its live jobs.

        ``canceled_job_refs`` lists only jobs the trainer itself confirmed stopped (a reply for
        that exact job). If any live job, including one whose dispatch was never acknowledged,
        cannot be confirmed stopped, the durable marker stays and ``FENCE_JOB_NOT_STOPPED`` is
        raised instead of returning a receipt that would over-report.
        """
        fold = self.journal.append({"t": "fence", "scope": scope})
        canceled: list[str] = []
        unresolved = 0
        for token, entry in fold.jobs.items():
            if entry.tenant != scope or entry.state in _TERMINAL:
                continue
            self.journal.append({"t": "cancel_intent", "k": token})
            try:
                credential = self._credential_by_ref(scope, entry.credential_ref)
                job_id = entry.job_id
                if not job_id:  # lost acknowledgement: find (read-only) it, then stop it
                    found = self.transport.find(credential, token)
                    if found is None or found.client_token != token or not found.job_id:
                        unresolved += 1
                        continue
                    job_id = found.job_id
                view = self.transport.request_cancel(credential, job_id)
                if view.client_token != token or view.job_id != job_id:
                    raise ValueError("trainer_identity_mismatch")
            except (OSError, LookupError, ValueError):
                unresolved += 1  # intent stays durable; collect is already refused
                continue
            if view.state in ("canceled", "completed"):
                self.journal.append({"t": "state", "k": token, "state": "canceled"})
                canceled.append(job_id)
            else:
                unresolved += 1
        if unresolved:
            raise ValueError("FENCE_JOB_NOT_STOPPED")
        evidence = hashlib.sha256(_canon([scope, sorted(canceled)]).encode()).hexdigest()[:32]
        return FenceReceipt(scope, tuple(sorted(canceled)), f"fence-receipt:{evidence}")

    def _credential_by_ref(self, tenant: str, credential_ref: str) -> Credential:
        credential = self.credentials(tenant, credential_ref)
        if credential is None or not credential.value:
            raise ValueError("credential_unavailable")
        return credential

    def usable_target(self, binding: mc.ModelBinding) -> str:
        """The prior usable target. A fenced, canceled or uncollected job never replaces it."""
        return binding.immutable_revision


def _within(usage: TrainerUsage, bound: mc.FitBound) -> bool:
    """True only for well-formed usage in the bound's currency and inside every limit.

    Malformed values (non-int, bool, negative, NaN) or a foreign currency return False: a cap
    that cannot be verified is treated as breached, never as satisfied.
    """
    counts = (usage.minor_units, usage.seconds, usage.rows, usage.steps)
    if any(type(count) is not int or count < 0 for count in counts):
        return False
    return (
        isinstance(usage.currency, str)
        and usage.currency == bound.currency
        and usage.minor_units <= bound.maximum_minor_units
        and usage.seconds <= bound.maximum_seconds
        and usage.rows <= bound.maximum_rows
        and usage.steps <= bound.maximum_steps
    )


def settle_callback(
    connector: CustomTrainerConnector, intent: mc.FitIntent, view: TrainerView
) -> bool:
    """Treat an asynchronous trainer callback as a wake-up hint. Returns whether it settled a job.

    The callback is not authenticated (its client token is derived from the idempotency key, not a
    secret), so its claimed state is never applied. The authoritative state is re-read from the
    trainer under the server-side credential. A callback for an unknown job, a mismatched client
    token, or a job that is already settled changes nothing.
    """
    token = _token(intent)
    entry = connector.journal.read().jobs.get(token)
    if (
        entry is None
        or not entry.job_id
        or entry.state in _TERMINAL
        or view.client_token != token
        or view.job_id != entry.job_id
    ):
        return False
    before = entry.state
    try:
        authoritative = connector.transport.poll(
            connector._credential(intent.binding), entry.job_id
        )
    except (OSError, LookupError, ValueError):
        return False  # nothing confirmed: nothing changes
    receipt = connector._settle(intent, token, entry, authoritative)
    return receipt.state.value in _TERMINAL and before not in _TERMINAL
