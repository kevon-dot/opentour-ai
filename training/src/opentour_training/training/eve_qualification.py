"""Checkpoint and training-backend qualification record for the EVE fit route (OPE-556).

Produces a digest-bound qualification record that pins an exact checkpoint, an exact backend,
the selected route, verified capabilities, a resource envelope and a dry-run receipt, plus an
admission gate that lets a later fit (OPE-557) run only against a qualified, current, undrifted
pair.

SIMULATED: every input in tests is a generated sentinel descriptor. The checks are software
checks over descriptors and generated rows (digest pinning, capability set, cap enforceability,
serialization dry run, authority completeness). The dry run submits nothing and records zero
training submissions.

NOT PROVEN: no real checkpoint was loaded, no GPU or provider was contacted, no trainer or
serving path was executed, no memory, time or cost figure was measured, and no Pi School model,
trainer, data, authority or person was involved. A record is "qualified" only when an injected
`WitnessVerifier` attests an external provider or customer witness; the default verifier
(`UnavailableWitnessVerifier`) never does, so by default nothing here can be qualified.
Admission additionally requires the caller to name the verifier ids it trusts. Tests prove
software behaviour only and establish no provider or customer evidence. The record never
authorizes a fit (`fit_authorized` is always false): authority, cap, rights and reservation
belong to OPE-557 and the learning mandate. The public-checkpoint rehearsal scope (OPE-558)
cannot admit a customer fit, and a mixture-of-experts checkpoint needs its own qualification.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

SCHEMA = "opentour.eve-checkpoint-backend-qualification.v1"
Json = dict[str, Any]

ROUTES = ("customer_trainer", "open_weight", "managed")
SCOPES = ("public_rehearsal", "customer")
# Tiers match the OPE-555 benchmark manifest vocabulary.
WITNESS_TIERS = ("observed", "installed-provider")
REQUIRED_CAPABILITIES = (
    "idempotent_submit",
    "job_receipt",
    "progress",
    "cancel",
    "candidate_identity",
    "parent_lineage",
    "fresh_replay",
)
AUTHORITY_FIELDS = (
    "model_rights",
    "data_rights",
    "execution_environment",
    "egress",
    "payer",
    "cost_cap",
    "recipient",
    "retention",
    "training_decision_owner",
    "release_decision_owner",
)
FORBIDDEN_ROW_KEYS = frozenset({"expected_answer", "reference_answer", "answer_key", "label"})
STATES = ("benchmark_only_hold", "software_checked_unwitnessed", "qualified")
_REVISION = re.compile(r"[0-9a-f]{40}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


class QualificationInputError(ValueError):
    """Malformed descriptor; the message is a fixed lowercase code and never echoes input."""


class AdmissionRefused(Exception):
    """Fail-closed refusal of a fit against a qualification; `code` is fixed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def canonical(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode()


def digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _text(value: object, code: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise QualificationInputError(code)
    return value


@dataclass(frozen=True)
class CheckpointDescriptor:
    """Exact immutable model identity; any change to any field changes `checkpoint_digest`."""

    model_ref: str
    immutable_revision: str
    architecture: str
    tokenizer_digest: str
    chat_template_digest: str
    input_schema_digest: str
    output_schema_digest: str
    formatter_id: str
    weight_file_digests: tuple[str, ...]
    mixture_of_experts: bool = False

    def validate(self) -> None:
        for name in ("model_ref", "architecture", "formatter_id"):
            _text(getattr(self, name), f"checkpoint_{name}_invalid")
        if not _REVISION.fullmatch(self.immutable_revision):
            raise QualificationInputError("checkpoint_revision_not_immutable")
        for name in (
            "tokenizer_digest",
            "chat_template_digest",
            "input_schema_digest",
            "output_schema_digest",
        ):
            if not _DIGEST.fullmatch(getattr(self, name)):
                raise QualificationInputError(f"checkpoint_{name}_invalid")
        if not self.weight_file_digests or not all(
            _DIGEST.fullmatch(d) for d in self.weight_file_digests
        ):
            raise QualificationInputError("checkpoint_weight_digests_invalid")
        if len(set(self.weight_file_digests)) != len(self.weight_file_digests):
            raise QualificationInputError("checkpoint_weight_digests_duplicated")

    def body(self) -> Json:
        return {
            "model_ref": self.model_ref,
            "immutable_revision": self.immutable_revision,
            "architecture": self.architecture,
            "tokenizer_digest": self.tokenizer_digest,
            "chat_template_digest": self.chat_template_digest,
            "input_schema_digest": self.input_schema_digest,
            "output_schema_digest": self.output_schema_digest,
            "formatter_id": self.formatter_id,
            "weight_file_digests": sorted(self.weight_file_digests),
            "mixture_of_experts": self.mixture_of_experts,
        }

    @property
    def checkpoint_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True)
class BackendDescriptor:
    """Exact backend identity as observed on the approved host or account."""

    backend_id: str
    version: str
    route: str
    dependency_revisions: Mapping[str, str]
    capabilities: frozenset[str]
    hard_cost_cap: bool
    host_ref: str
    available: bool = True

    def validate(self) -> None:
        _text(self.backend_id, "backend_id_invalid")
        _text(self.version, "backend_version_invalid")
        _text(self.host_ref, "backend_host_invalid")
        if self.route not in ROUTES:
            raise QualificationInputError("backend_route_unknown")
        if not self.dependency_revisions or not all(
            type(k) is str and k and type(v) is str and v
            for k, v in self.dependency_revisions.items()
        ):
            raise QualificationInputError("backend_dependency_revisions_invalid")

    def body(self) -> Json:
        return {
            "backend_id": self.backend_id,
            "version": self.version,
            "route": self.route,
            "dependency_revisions": dict(sorted(self.dependency_revisions.items())),
            "capabilities": sorted(self.capabilities),
            "hard_cost_cap": self.hard_cost_cap,
            "host_ref": self.host_ref,
            "available": self.available,
        }

    @property
    def backend_digest(self) -> str:
        return digest(self.body())


@dataclass(frozen=True)
class ResourceEstimate:
    """Estimated envelope on the named host; None means unknown, which is never zero."""

    memory_gib: int | None
    seconds: int | None
    cost_minor_units: int | None
    currency: str | None

    def missing(self) -> list[str]:
        gaps = []
        if self.memory_gib is None:
            gaps.append("memory_gib")
        if self.seconds is None:
            gaps.append("seconds")
        if self.cost_minor_units is None or self.currency is None:
            gaps.append("cost")
        return gaps

    def body(self) -> Json:
        return {
            "memory_gib": self.memory_gib,
            "seconds": self.seconds,
            "cost_minor_units": self.cost_minor_units,
            "currency": self.currency,
        }


@dataclass(frozen=True)
class Witness:
    """An external attestation that the pair ran on the real host or account."""

    verifier_id: str
    witness_ref: str
    tier: str


class WitnessVerifier(Protocol):
    """Boundary to whoever can attest a real provider or customer qualification run."""

    def verify(self, checkpoint_digest: str, backend_digest: str) -> Witness | None: ...


class UnavailableWitnessVerifier:
    """Default: no real GPU/provider witness exists, so nothing can reach `qualified`."""

    def verify(self, checkpoint_digest: str, backend_digest: str) -> Witness | None:
        return None


@dataclass(frozen=True)
class QualificationRequest:
    checkpoint: CheckpointDescriptor
    backend: BackendDescriptor
    scope: str
    authority: Mapping[str, str | None]
    estimate: ResourceEstimate
    development_rows: tuple[Mapping[str, object], ...]
    benchmark_manifest_digest: str | None = None
    inference_api_key_only: bool = False
    external_gates: tuple[str, ...] = field(default=())


def pair_key(checkpoint: CheckpointDescriptor, backend: BackendDescriptor, scope: str) -> str:
    """Stable identity of a (model, backend, route, scope); digests are deliberately excluded."""
    return digest(
        {
            "model_ref": checkpoint.model_ref,
            "backend_id": backend.backend_id,
            "route": backend.route,
            "scope": scope,
        }
    )


def serialization_dry_run(
    checkpoint: CheckpointDescriptor, rows: Sequence[Mapping[str, object]]
) -> Json:
    """Format permitted rows locally as prompt/completion pairs; nothing is sent anywhere."""
    if not rows:
        raise QualificationInputError("dry_run_rows_missing")
    formatted = []
    for row in rows:
        if set(row) & FORBIDDEN_ROW_KEYS:
            raise QualificationInputError("dry_run_protected_field_present")
        if set(row) != {"prompt", "completion"}:
            raise QualificationInputError("dry_run_row_shape_invalid")
        prompt = _text(row["prompt"], "dry_run_row_shape_invalid")
        completion = _text(row["completion"], "dry_run_row_shape_invalid")
        # Completion-only loss: the mask covers the completion span exclusively.
        formatted.append(
            {"formatter": checkpoint.formatter_id, "prompt": prompt, "loss_on": completion}
        )
    return {
        "rows_checked": len(formatted),
        "serialization_digest": digest(formatted),
        "loss_scope": "completion_only",
        "submitted": False,
        "training_submissions": 0,
    }


def qualify(request: QualificationRequest, verifier: WitnessVerifier | None = None) -> Json:
    """Run the software checks and return a digest-bound record. Deterministic; no I/O."""
    cp, be = request.checkpoint, request.backend
    cp.validate()
    be.validate()
    if request.scope not in SCOPES:
        raise QualificationInputError("scope_unknown")
    if set(request.authority) - set(AUTHORITY_FIELDS):
        raise QualificationInputError("authority_field_unknown")
    verifier = verifier or UnavailableWitnessVerifier()

    holds: list[str] = []
    checks: list[Json] = []

    def check(check_id: str, passed: bool, hold: str | None = None) -> None:
        checks.append({"id": check_id, "passed": passed})
        if not passed and hold:
            holds.append(hold)

    check("backend_available", be.available, "backend_unavailable")
    missing_caps = sorted(set(REQUIRED_CAPABILITIES) - be.capabilities)
    check("required_capabilities", not missing_caps, "capability_missing")
    check("hard_cost_cap_enforceable", be.hard_cost_cap, "enforceable_cap_unavailable")
    check("moe_scoped", not cp.mixture_of_experts, "mixture_of_experts_needs_own_qualification")
    check("resource_estimate_known", not request.estimate.missing(), "resource_estimate_unknown")
    authority = {name: request.authority.get(name) for name in AUTHORITY_FIELDS}
    missing_authority = [name for name, ref in authority.items() if not ref]
    check("authority_complete", not missing_authority, "authority_incomplete")
    check(
        "not_inference_key_only",
        not request.inference_api_key_only,
        "inference_key_alone_supports_scoring_only",
    )
    dry_run: Json | None
    try:
        dry_run = serialization_dry_run(cp, request.development_rows)
        check("serialization_dry_run", True)
    except QualificationInputError as error:
        dry_run = None
        check("serialization_dry_run", False, str(error))

    witness = None if holds else verifier.verify(cp.checkpoint_digest, be.backend_digest)
    if witness is not None and witness.tier not in WITNESS_TIERS:
        witness = None
    external_gates = list(request.external_gates)
    if witness is None:
        external_gates.append("real_gpu_provider_witness_unsatisfied")
    external_gates.extend(f"authority_missing:{name}" for name in missing_authority)

    if holds:
        state = "benchmark_only_hold"
    elif witness is None:
        state = "software_checked_unwitnessed"
    else:
        state = "qualified"
    body: Json = {
        "schema": SCHEMA,
        "pair_key": pair_key(cp, be, request.scope),
        "scope": request.scope,
        "route": be.route,
        "checkpoint": cp.body(),
        "checkpoint_digest": cp.checkpoint_digest,
        "backend": be.body(),
        "backend_digest": be.backend_digest,
        "capabilities": {
            "required": list(REQUIRED_CAPABILITIES),
            "verified": sorted(set(REQUIRED_CAPABILITIES) & be.capabilities),
            "missing": missing_caps,
        },
        "hard_cost_cap_enforceable": be.hard_cost_cap,
        "resource_estimate": request.estimate.body(),
        "authority": authority,
        "missing_authority": missing_authority,
        "dry_run": dry_run,
        "checks": checks,
        "holds": sorted(set(holds)),
        "state": state,
        "evidence_tier": witness.tier if witness else "generated",
        "witness": (
            {"verifier_id": witness.verifier_id, "witness_ref": witness.witness_ref}
            if witness
            else None
        ),
        "benchmark_manifest_digest": request.benchmark_manifest_digest,
        "external_gates": sorted(set(external_gates)),
        "training_submissions": 0,
        "fit_authorized": False,
        "claim_scope": "readiness_only",
    }
    return {"body": body, "record_digest": digest(body)}


def verify_record(record: Mapping[str, Any]) -> Json:
    """Recompute the digest and reject a record whose body was altered."""
    body = record.get("body")
    if not isinstance(body, dict) or record.get("record_digest") != digest(body):
        raise AdmissionRefused("record_digest_mismatch")
    if (
        body.get("schema") != SCHEMA
        or body.get("fit_authorized") is not False
        or body.get("state") not in STATES
    ):
        raise AdmissionRefused("record_schema_invalid")
    return body


class QualificationRegistry:
    """Append-only, file-backed journal of qualification records; replayed on construction."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._entries: list[Json] = []
        if path.exists():
            for line in path.read_text().splitlines():
                entry = json.loads(line)
                verify_record(entry["record"])
                self._entries.append(entry)

    def active(self, key: str) -> Json | None:
        for entry in reversed(self._entries):
            if entry["record"]["body"]["pair_key"] == key:
                result: Json = entry["record"]
                return result
        return None

    def find(self, record_digest: str) -> Json | None:
        for entry in self._entries:
            if entry["record"]["record_digest"] == record_digest:
                result: Json = entry["record"]
                return result
        return None

    def register(self, record: Mapping[str, Any]) -> Json:
        """Idempotent for the identical record; a different record for the pair supersedes."""
        body = verify_record(record)
        current = self.active(body["pair_key"])
        if current is not None and current["record_digest"] == record["record_digest"]:
            return {"result": "unchanged", "record_digest": record["record_digest"]}
        entry = {
            "seq": len(self._entries) + 1,
            "supersedes": current["record_digest"] if current else None,
            "record": dict(record),
        }
        line = json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n"
        with self.path.open("ab") as handle:
            handle.write(line.encode())
            handle.flush()
            os.fsync(handle.fileno())
        self._entries.append(entry)
        return {
            "result": "superseded" if current else "registered",
            "record_digest": record["record_digest"],
        }


def admit_fit(
    registry: QualificationRegistry,
    record_digest: str,
    checkpoint: CheckpointDescriptor,
    backend: BackendDescriptor,
    *,
    scope: str,
    hard_cap_mandate: bool,
    trusted_verifiers: frozenset[str],
) -> Json:
    """Refuse unless the presented pair is exactly the current qualified pair.

    Admission is readiness only. It returns no authority: the caller must still hold a grant,
    a reservation and a cap, and `fit_authorized` stays false.
    """
    presented = registry.find(record_digest)
    if presented is None:
        raise AdmissionRefused("pair_unqualified")
    body = verify_record(presented)
    key = pair_key(checkpoint, backend, scope)
    if body["pair_key"] != key:
        same_pair = body["pair_key"] == pair_key(checkpoint, backend, body["scope"])
        raise AdmissionRefused("scope_mismatch" if same_pair else "pair_unqualified")
    current = registry.active(key)
    if current is None or current["record_digest"] != record_digest:
        raise AdmissionRefused("qualification_superseded")
    if body["state"] == "benchmark_only_hold":
        raise AdmissionRefused("pair_unqualified")
    if body["state"] != "qualified":
        raise AdmissionRefused("witness_missing")
    witness = body["witness"]
    if not witness or witness["verifier_id"] not in trusted_verifiers:
        raise AdmissionRefused("witness_verifier_untrusted")
    if checkpoint.checkpoint_digest != body["checkpoint_digest"]:
        raise AdmissionRefused("checkpoint_digest_drift")
    if not backend.available:
        raise AdmissionRefused("backend_unavailable")
    pinned = body["backend"]
    if (backend.backend_id, backend.version) != (pinned["backend_id"], pinned["version"]):
        raise AdmissionRefused("backend_version_drift")
    if not set(body["capabilities"]["verified"]) <= backend.capabilities:
        raise AdmissionRefused("capability_missing")
    if hard_cap_mandate and not (backend.hard_cost_cap and body["hard_cost_cap_enforceable"]):
        raise AdmissionRefused("enforceable_cap_unavailable")
    if backend.backend_digest != body["backend_digest"]:
        raise AdmissionRefused("backend_digest_drift")
    return {
        "admitted_for": "separately_authorized_fit",
        "record_digest": record_digest,
        "pair_key": key,
        "fit_authorized": False,
    }
