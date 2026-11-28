"""Typed inputs and outputs of the benchmark audit. Frozen, explicit, JSON-serializable."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Literal

RECEIPT_SCHEMA = "opentour.benchmark-audit-receipt.v1"
# Interface name published by the open OPE-416 draft; consumed here only as an opaque binding.
CANDIDATE_SNAPSHOT_SCHEMA = "opentour.pilot.live-learning-snapshot.v1"

Split = Literal["development", "protected"]
Stratum = Literal["routine", "critical"]
OutcomeKind = Literal["correct", "correction", "deferral"]
EvidenceOrigin = Literal["generated", "observed"]


class Verdict(StrEnum):
    EVALUATION_ALLOWED = "evaluation_allowed"
    STARTER_ONLY = "starter_only"
    CLAIM_LIMITED = "claim_limited_to_named_population"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"
    INVALID = "invalid"
    STALE = "stale"


class SamplingDesign(StrEnum):
    POLICY_ONLY = "policy_only"
    FULL_ENUMERATION = "full_enumeration"
    CORRECTION_ONLY = "correction_only"
    STRATIFIED_RANDOM = "stratified_random"


class PopulationKind(StrEnum):
    ENUMERATED_FIXTURE = "enumerated_fixture"
    REAL_SAMPLE = "real_sample"


def canonical_digest(value: object) -> str:
    """SHA-256 of canonical JSON. Dataclasses must be converted with ``asdict`` first."""
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    )
    return hashlib.sha256(encoded.encode("ascii")).hexdigest()


def label_commitment(salt: str, family_id: str, label: str) -> str:
    """Salted commitment so the audit compares labels without ever holding them."""
    return hashlib.sha256(f"{salt}\x1f{family_id}\x1f{label}".encode()).hexdigest()


@dataclass(frozen=True)
class FeatureSpec:
    name: str
    available_at: Literal["decision_time", "post_decision"]


@dataclass(frozen=True)
class BindingDigests:
    """Everything a receipt is bound to. Any difference after freeze makes it stale."""

    canonical_inputs: str
    source_revisions: str
    split_manifest: str
    scoring_policy: str
    label_manifest: str
    answer_key: str


@dataclass(frozen=True)
class PreRegistration:
    """Committed before candidate outputs are revealed."""

    registration_id: str
    frozen_day: int
    evidence_origin: EvidenceOrigin
    population_kind: PopulationKind
    population_frame: str
    intended_decision: str
    policy_revision: str
    feature_contract: tuple[FeatureSpec, ...]
    label_authority: str
    maturity_window_days: int
    grouping_method: str
    temporal_cutoff_day: int
    scorer_version: str
    sampling_design: SamplingDesign
    sampling_seed: int
    sample_sizes: tuple[tuple[str, int], ...]
    critical_slices: tuple[str, ...]
    operating_point: str
    decision_margin: float
    interval_method: str
    independent_unit: Literal["independent_family"]
    tolerable_label_error: float
    acceptable_error_ceiling: float
    alpha: float
    min_critical_coverage: float
    max_unresolved: int
    disagreement_rule: str
    frozen: BindingDigests

    def digest(self) -> str:
        return canonical_digest(asdict(self))


@dataclass(frozen=True)
class FamilyRecord:
    """One independent family. Labels appear only as commitments."""

    family_id: str
    group_root: str | None
    split: Split
    stratum: Stratum
    slice_id: str
    outcome_kind: OutcomeKind
    decided_day: int
    final_label_digest: str
    exact_hash: str | None
    template_group: str | None
    attachment_hash: str | None
    ancestry_root: str | None
    first_labeler_id: str
    auto_acted: bool = False
    adjudicator_id: str | None = None
    adjudicator_saw_first_label: bool = False
    adjudicator_saw_predictions: bool = False
    adjudicator_label_digest: str | None = None
    independent_source: str | None = None
    independent_label_digest: str | None = None
    unresolved: bool = False
    excluded_reason: str | None = None
    sampling_probability: float | None = None
    post_decision_fields_in_input: tuple[str, ...] = ()
    fresh_confirmation: bool = False


@dataclass(frozen=True)
class PreprocessingRecord:
    revision: str
    fit_partition: Split
    applied_to_protected_revision: str | None


@dataclass(frozen=True)
class SelectionUse:
    """A comparison that informed choosing the candidate or threshold."""

    decision: str
    partition: Split


@dataclass(frozen=True)
class CandidateSnapshotBinding:
    """Opaque reference to the OPE-416 verified-candidate snapshot receipt (unlanded)."""

    schema_version: str
    snapshot_digest: str


@dataclass(frozen=True)
class ReleaseEntry:
    """One cell the exposure guard actually released, as the guard recorded it."""

    cell: frozenset[str]
    protected_role: bool  # the guard treated the roles as protected (informational in replay)
    authorized_by: str | None = None


def _digest_part(value: object) -> object:
    if type(value) is str:
        return value
    if type(value) in (frozenset, tuple, list, set):
        return sorted(str(_digest_part(v)) for v in value)  # type: ignore[attr-defined]
    return f"<{type(value).__name__}:{value!r}>"


def exposure_record_digest(universe: object, releases: object) -> str:
    """Content digest of a release history; any later change to it changes the digest."""
    entries = [
        [_digest_part(e.cell), repr(e.protected_role), _digest_part(e.authorized_by)]
        for e in (releases if type(releases) is tuple else ())
    ]
    return canonical_digest(
        {
            "universe": _digest_part(universe),
            "releases_type": type(releases).__name__,
            "releases": entries,
        }
    )


@dataclass(frozen=True)
class ExposureRecord:
    """An exposure guard's release history, from which the audit re-derives exposure.

    The audit replays this history instead of trusting a caller's list of exposed families or a
    caller's count of unlogged disclosures. Every history is treated as supplied and unsigned:
    nothing binds it to the guard that actually served the traffic or to the protected manifest,
    so a caller can hand over a fresh guard's harmless history. A worker-held history requires a
    signed, manifest-bound guard record (OPE-566); it must never be inferred from object
    identity. ``content_digest`` only detects a record changed after it was sealed.
    """

    universe: frozenset[str]
    releases: tuple[ReleaseEntry, ...]
    content_digest: str | None = None

    @classmethod
    def sealed(cls, universe: frozenset[str], releases: tuple[ReleaseEntry, ...]) -> ExposureRecord:
        return cls(universe, releases, exposure_record_digest(universe, releases))

    def intact(self) -> bool:
        return self.content_digest is not None and self.content_digest == exposure_record_digest(
            self.universe, self.releases
        )


@dataclass(frozen=True)
class AuditInputs:
    prereg: PreRegistration
    records: tuple[FamilyRecord, ...]
    current: BindingDigests
    as_of_day: int
    candidate_outputs_revealed_day: int | None = None
    consumed_features: tuple[str, ...] = ()
    preprocessing: PreprocessingRecord | None = None
    selection_log: tuple[SelectionUse, ...] = ()
    # Caller-asserted. The audit can only add to them from ``exposure_record``; a caller's empty
    # set or zero never overrides exposure that the guard's release history implies.
    exposed_families: frozenset[str] = frozenset()
    unlogged_disclosures: int = 0
    exposure_record: ExposureRecord | None = None
    candidate_snapshot: CandidateSnapshotBinding | None = None
    # Caller-asserted contract-test hook for the future OPE-416 consumer; the worker cannot
    # verify it and the receipt labels it so. Never true from local evidence.
    upstream_snapshot_verified: bool = False
    superseded_by: str | None = None
    successor_of: str | None = None


@dataclass(frozen=True)
class Reason:
    code: str
    detail: str
