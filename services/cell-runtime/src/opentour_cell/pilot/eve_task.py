"""Versioned EVE question-and-evidence task registration and fit-row mapping.

Registers EVE as an addressable text-generation task behind the OPE-506 model connector contract.
The registration pins the exact base revision, tokenizer and chat-template fingerprints, replay
decoding parameters, the inference and trainer destinations (both distinct from the Slack-facing
run-control endpoint) and the capabilities EVE may exercise. A registration with no trainer
destination is inference-only and can never produce a fit. Development rows are mapped to an
assistant-response-only completion format, and a case or family that appears in the protected
membership can never become a fit row.

Simulated: the unit tests use generated sentinel cases, a file-backed registry and a deterministic
trainer-transport double. Not proven: no real EVE checkpoint, tokenizer, GPU, trainer, provider
or Pi School data is used, so nothing here is installed-provider or customer evidence. The EVE
base, tokenizer and template fingerprints in tests are generated placeholders.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from opentour_cell.pilot import model_connector as mc

VERSION = "opentour.pilot.eve-task.v1"
TASK = "eve.question-evidence.v1"
LOSS_MASK = "assistant_response_only"
ROW_FIELDS = frozenset({"question", "context", "completion", "loss_mask"})
_DIGEST = re.compile("sha256:[0-9a-f]{64}")
_REVISION = re.compile("[0-9a-f]{40}")


class EveRefusal(ValueError):
    """A fixed lowercase refusal reason. It never carries case text, answers or credentials."""


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def normalize_text(text: str) -> str:
    """Case, width and whitespace insensitive form used to compare text against protected text."""
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


class Capability(StrEnum):
    GENERATE = "generate"
    TRAIN = "train"


@dataclass(frozen=True)
class DecodingPolicy:
    temperature_milli: int
    top_p_milli: int
    max_new_tokens: int
    seed: int

    def __post_init__(self) -> None:
        if (
            any(type(v) is not int for v in (self.temperature_milli, self.top_p_milli))
            or type(self.max_new_tokens) is not int
            or type(self.seed) is not int
            or not 0 <= self.temperature_milli <= 2000
            or not 0 < self.top_p_milli <= 1000
            or not 0 < self.max_new_tokens <= 8192
        ):
            raise EveRefusal("invalid_decoding_policy")

    @property
    def digest(self) -> str:
        return digest([self.temperature_milli, self.top_p_milli, self.max_new_tokens, self.seed])


@dataclass(frozen=True)
class EveTaskRegistration:
    """One exact EVE task identity. Every field is pinned; none has a default."""

    model_ref: str
    base_revision: str
    tokenizer_digest: str
    template_digest: str
    decoding: DecodingPolicy
    inference_destination: str
    trainer_destination: str | None
    run_control_destination: str
    capabilities: frozenset[Capability]
    task: str = TASK

    def __post_init__(self) -> None:
        text = (self.model_ref, self.inference_destination, self.run_control_destination)
        if (
            not all(isinstance(v, str) and v and v.strip() == v for v in text)
            or not _REVISION.fullmatch(self.base_revision)
            or not _DIGEST.fullmatch(self.tokenizer_digest)
            or not _DIGEST.fullmatch(self.template_digest)
            or self.task != TASK
            or not self.capabilities
            or not all(isinstance(c, Capability) for c in self.capabilities)
        ):
            raise EveRefusal("invalid_eve_registration")
        if self.trainer_destination is not None and (
            not self.trainer_destination
            or self.trainer_destination.strip() != self.trainer_destination
        ):
            raise EveRefusal("invalid_eve_registration")
        if self.run_control_destination in {self.inference_destination, self.trainer_destination}:
            raise EveRefusal("run_control_endpoint_must_be_distinct")
        if (Capability.TRAIN in self.capabilities) != (self.trainer_destination is not None):
            raise EveRefusal("training_capability_requires_trainer_destination")

    @property
    def fingerprint(self) -> str:
        """The tokenizer and chat-template fingerprint carried as ``preprocessing_revision``."""
        return "eve-fp:" + digest([self.tokenizer_digest, self.template_digest])[7:]

    @property
    def identity(self) -> str:
        """Digest over every pinned field: the exact registered model, task and replay settings."""
        return digest(registration_document(self))


def registration_document(registration: EveTaskRegistration) -> dict[str, Any]:
    return {
        "schema_version": VERSION,
        "model_ref": registration.model_ref,
        "task": registration.task,
        "base_revision": registration.base_revision,
        "tokenizer_digest": registration.tokenizer_digest,
        "template_digest": registration.template_digest,
        "decoding": [
            registration.decoding.temperature_milli,
            registration.decoding.top_p_milli,
            registration.decoding.max_new_tokens,
            registration.decoding.seed,
        ],
        "inference_destination": registration.inference_destination,
        "trainer_destination": registration.trainer_destination,
        "run_control_destination": registration.run_control_destination,
        "capabilities": sorted(c.value for c in registration.capabilities),
    }


def parse_registration(raw: Any) -> EveTaskRegistration:
    if not isinstance(raw, dict) or raw.get("schema_version") != VERSION:
        raise EveRefusal("invalid_eve_registration")
    try:
        decoding = DecodingPolicy(*raw["decoding"])
        return EveTaskRegistration(
            model_ref=raw["model_ref"],
            base_revision=raw["base_revision"],
            tokenizer_digest=raw["tokenizer_digest"],
            template_digest=raw["template_digest"],
            decoding=decoding,
            inference_destination=raw["inference_destination"],
            trainer_destination=raw["trainer_destination"],
            run_control_destination=raw["run_control_destination"],
            capabilities=frozenset(Capability(c) for c in raw["capabilities"]),
            task=raw["task"],
        )
    except (KeyError, TypeError, ValueError):
        raise EveRefusal("invalid_eve_registration") from None


def binding_for(
    registration: EveTaskRegistration,
    *,
    tenant: str,
    route: mc.Route,
    region: str,
    credential_ref: str,
) -> mc.ModelBinding:
    """Derive the connector binding. The registration, not the caller, supplies identity."""
    destination = (
        registration.trainer_destination
        if route is mc.Route.CUSTOM and registration.trainer_destination
        else registration.inference_destination
    )
    return mc.ModelBinding(
        tenant=tenant,
        model_ref=registration.model_ref,
        immutable_revision=registration.base_revision,
        task=registration.task,
        input_schema="eve-question-evidence-v1",
        output_schema="eve-answer-citations-v1",
        preprocessing_revision=registration.fingerprint,
        route=route,
        account_or_host=destination,
        region=region,
        credential_ref=credential_ref,
    )


class EveRegistry:
    """A file-backed registry of exact EVE registrations that survives restart.

    A model that is not here cannot be replayed or fitted. Registering the same identity again is
    a no-op; a different identity for an existing ``model_ref`` is a conflict, never an overwrite.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def _load(self) -> dict[str, EveTaskRegistration]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_bytes())
            return {item["model_ref"]: parse_registration(item) for item in raw["registrations"]}
        except (OSError, ValueError, KeyError, TypeError):
            raise EveRefusal("eve_registry_unavailable") from None

    def register(self, registration: EveTaskRegistration) -> None:
        current = self._load()
        existing = current.get(registration.model_ref)
        if existing is not None:
            if existing.identity != registration.identity:
                raise EveRefusal("eve_registration_conflict")
            return
        current[registration.model_ref] = registration
        body = {"registrations": [registration_document(r) for r in current.values()]}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=self.path.parent, suffix=".new")
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(body))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, self.path)

    def require(self, model_ref: str) -> EveTaskRegistration:
        found = self._load().get(model_ref)
        if found is None:
            raise EveRefusal("model_unregistered")
        return found


@dataclass(frozen=True)
class Passage:
    passage_id: str
    text: str


@dataclass(frozen=True)
class EveCase:
    """One question with its approved context. It never carries a reference answer."""

    case_id: str
    family_id: str
    question: str
    context: tuple[Passage, ...]


@dataclass(frozen=True)
class DevelopmentExample:
    case: EveCase
    answer: str


@dataclass(frozen=True)
class Partition:
    """Disjoint development and protected membership, bound by a protected-membership digest.

    Overlap by case or by family is refused when the partition is built. Only the digest of the
    protected membership, never the ids, is carried to the fit side.
    """

    development: tuple[DevelopmentExample, ...]
    protected_case_ids: frozenset[str]
    protected_family_ids: frozenset[str]

    def __post_init__(self) -> None:
        dev_cases = {e.case.case_id for e in self.development}
        dev_families = {e.case.family_id for e in self.development}
        if (
            dev_cases & self.protected_case_ids
            or dev_families & self.protected_family_ids
            or len(dev_cases) != len(self.development)
        ):
            raise EveRefusal("protected_overlap")

    @property
    def membership_digest(self) -> str:
        return membership_digest(self.protected_case_ids, self.protected_family_ids)


def membership_digest(case_ids: frozenset[str], family_ids: frozenset[str]) -> str:
    return digest({"cases": sorted(case_ids), "families": sorted(family_ids)})


def render_context(case: EveCase) -> str:
    return "\n".join(f"[{p.passage_id}] {p.text}" for p in case.context)


def development_rows(partition: Partition) -> tuple[Mapping[str, object], ...]:
    """Map verified development examples to the text-generation format with completion masking."""
    if not partition.development:
        raise EveRefusal("no_development_rows")
    return tuple(
        {
            "question": e.case.question,
            "context": render_context(e.case),
            "completion": e.answer,
            "loss_mask": LOSS_MASK,
        }
        for e in sorted(partition.development, key=lambda e: e.case.case_id)
    )


def serialize_development(rows: tuple[Mapping[str, object], ...]) -> bytes:
    """Canonical fit payload. A connector for EVE must serialize exactly this."""
    if not rows or any(set(r) != ROW_FIELDS or r["loss_mask"] != LOSS_MASK for r in rows):
        raise EveRefusal("training_payload_invalid")
    return canonical([dict(r) for r in rows])


@dataclass(frozen=True)
class FitPlan:
    intent: mc.FitIntent
    rows: tuple[Mapping[str, object], ...]
    protected_membership_fingerprint: str


def plan_fit(
    *,
    registration: EveTaskRegistration,
    binding: mc.ModelBinding,
    capability: mc.Capability,
    partition: Partition,
    grant_revision: str,
    benchmark_revision: str,
    idempotency_key: str,
    bound: mc.FitBound,
    bound_membership_digest: str,
) -> FitPlan:
    """Build one fit intent, or refuse so that no fit can start.

    Only a TRAINABLE capability observed for this exact binding and a registration that declares
    the train capability can plan a fit. An inference-only target is a zero-fit outcome. The
    partition's protected membership must equal ``bound_membership_digest``, the membership frozen
    with the replay set, so a caller cannot declare a smaller protected set than the one replayed.
    """
    if partition.membership_digest != bound_membership_digest:
        raise EveRefusal("protected_membership_mismatch")
    if (
        binding.model_ref != registration.model_ref
        or binding.immutable_revision != registration.base_revision
        or binding.preprocessing_revision != registration.fingerprint
        or binding.task != registration.task
    ):
        raise EveRefusal("registration_binding_mismatch")
    if (
        capability.state is not mc.State.TRAINABLE
        or Capability.TRAIN not in registration.capabilities
    ):
        raise EveRefusal("fit_unavailable_inference_only")
    rows = development_rows(partition)
    payload = serialize_development(rows)
    intent = mc.FitIntent(
        binding=binding,
        grant_revision=grant_revision,
        benchmark_revision=benchmark_revision,
        development_digest=hashlib.sha256(payload).hexdigest(),
        idempotency_key=idempotency_key,
        fields=ROW_FIELDS,
        bound=bound,
    )
    return FitPlan(intent, rows, partition.membership_digest)


FIT_RECORD = "opentour.pilot.eve-fit-record.v1"


def _binding_document(binding: mc.ModelBinding) -> list[str]:
    return [
        binding.tenant,
        binding.model_ref,
        binding.immutable_revision,
        binding.task,
        binding.input_schema,
        binding.output_schema,
        binding.preprocessing_revision,
        binding.route.value,
        binding.account_or_host,
        binding.region,
        binding.credential_ref,
    ]


def _bound_document(bound: mc.FitBound) -> list[object]:
    return [
        bound.currency,
        bound.maximum_minor_units,
        bound.maximum_seconds,
        bound.maximum_rows,
        bound.maximum_steps,
    ]


def intent_document(intent: mc.FitIntent) -> dict[str, Any]:
    """Every field of a fit intent. It names the development payload by digest only."""
    return {
        "binding": _binding_document(intent.binding),
        "grant_revision": intent.grant_revision,
        "benchmark_revision": intent.benchmark_revision,
        "development_digest": intent.development_digest,
        "idempotency_key": intent.idempotency_key,
        "fields": sorted(intent.fields),
        "bound": _bound_document(intent.bound),
    }


def intent_document_keys() -> tuple[str, ...]:
    return (
        "binding",
        "grant_revision",
        "benchmark_revision",
        "development_digest",
        "idempotency_key",
        "fields",
        "bound",
    )


def intent_digest(intent: mc.FitIntent) -> str:
    return digest(intent_document(intent))


def _parse_intent(raw: Any) -> mc.FitIntent:
    binding = raw["binding"]
    bound = raw["bound"]
    if (
        not isinstance(binding, list)
        or len(binding) != 11
        or not isinstance(bound, list)
        or len(bound) != 5
        or type(bound[0]) is not str
        or not all(type(v) is int for v in bound[1:])
        or not isinstance(raw["fields"], list)
        or not all(type(f) is str for f in raw["fields"])
        or not all(
            type(raw[k]) is str and raw[k]
            for k in (
                "grant_revision",
                "benchmark_revision",
                "development_digest",
                "idempotency_key",
            )
        )
        or set(raw) != set(intent_document_keys())
    ):
        raise ValueError("invalid fit intent")
    route = mc.Route(binding[7])
    return mc.FitIntent(
        binding=mc.ModelBinding(
            tenant=binding[0],
            model_ref=binding[1],
            immutable_revision=binding[2],
            task=binding[3],
            input_schema=binding[4],
            output_schema=binding[5],
            preprocessing_revision=binding[6],
            route=route,
            account_or_host=binding[8],
            region=binding[9],
            credential_ref=binding[10],
        ),
        grant_revision=raw["grant_revision"],
        benchmark_revision=raw["benchmark_revision"],
        development_digest=raw["development_digest"],
        idempotency_key=raw["idempotency_key"],
        fields=frozenset(raw["fields"]),
        bound=mc.FitBound(*bound),
    )


def fit_record(job: mc.JobReceipt, candidate: mc.Candidate) -> dict[str, Any]:
    """The job and collected candidate a fit produced, without any development row."""
    if candidate.intent != job.intent:
        raise EveRefusal("candidate_lineage_unverified")
    return {
        "schema_version": FIT_RECORD,
        "intent": intent_document(job.intent),
        "job": [job.state.value, job.job_ref, job.evidence_ref],
        "candidate": [
            candidate.job_ref,
            candidate.parent_revision,
            candidate.artifact_or_serving_revision,
            candidate.replay_evidence_ref,
        ],
    }


def parse_fit_record(raw: Any) -> tuple[mc.JobReceipt, mc.Candidate]:
    """Read a fit record strictly. Its lineage is the reader's to re-verify with the connector."""
    try:
        if not isinstance(raw, dict) or set(raw) != {
            "schema_version",
            "intent",
            "job",
            "candidate",
        }:
            raise ValueError("invalid fit record")
        if raw["schema_version"] != FIT_RECORD:
            raise ValueError("invalid fit record")
        intent = _parse_intent(raw["intent"])
        state, job_ref, evidence_ref = raw["job"]
        job_ref_c, parent, artifact, replay_ref = raw["candidate"]
        text = (job_ref, evidence_ref, job_ref_c, parent, artifact, replay_ref)
        if not all(type(v) is str and v for v in text):
            raise ValueError("invalid fit record")
        job = mc.JobReceipt(intent, mc.JobState(state), job_ref, evidence_ref)
        return job, mc.Candidate(intent, job_ref_c, parent, artifact, replay_ref)
    except (KeyError, TypeError, ValueError):
        raise EveRefusal("fit_record_malformed") from None
