"""Declarative open-weight model-family profiles and their separate qualification records.

A profile pins one checkpoint, tokenizer, architecture, supported task, data formatter, tuning
method, layer targets, license and resource estimate. A qualification record belongs to exactly one
family revision and is never inherited by another family, and a customer model is never silently
replaced by a reference model.

Simulated: the unit tests exercise the record rules with generated sentinel fixtures.
Not proven: no real checkpoint, GPU or provider capacity is used here. A record built from a
generated fixture has ``EvidenceTier.FIXTURE`` and lists its unsatisfied external gates.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from opentour_cell.pilot.connector_ext import EvidenceTier
from opentour_cell.pilot.model_connector import ModelBinding

VERSION = "opentour.pilot.model-family.v1"
_DIGEST = re.compile("[0-9a-f]{64}")


class FamilyError(ValueError):
    """A fixed, fail-closed reason code."""


class Role(StrEnum):
    REFERENCE = "reference"
    CANDIDATE = "candidate"


class Check(StrEnum):
    REFERENCE_REPRODUCED = "reference_reproduced"
    PARAMETER_UPDATE = "parameter_update"
    BASE_INTEGRITY = "base_integrity"
    FRESH_PROCESS_INFERENCE = "fresh_process_inference"
    TASK_SCORER_MATCH = "task_scorer_match"
    ROLLBACK = "rollback"
    CANCELLATION = "cancellation"
    SIMPLE_REPAIR_ZERO_FIT = "simple_repair_zero_fit"
    INCOMPATIBLE_MODEL_ZERO_FIT = "incompatible_model_zero_fit"


class FixtureKind(StrEnum):
    GENERATED = "generated"
    INSTALLED_MODEL = "installed_model"


@dataclass(frozen=True)
class ResourceEstimate:
    max_seconds: int
    max_rows: int
    max_steps: int
    max_address_space_bytes: int
    incremental_cost_minor_units: int = 0


@dataclass(frozen=True)
class ModelFamilyProfile:
    family_id: str
    role: Role
    architecture: str
    publisher_ref: str
    revision: str | None
    license: str
    task: str
    formatter: str
    tuning_method: str
    layer_targets: tuple[str, ...]
    tokenizer_file: str
    files: tuple[tuple[str, str | None], ...]
    resource: ResourceEstimate
    scorer: str
    benchmark_contract: str

    @property
    def pin_gaps(self) -> tuple[str, ...]:
        """Name every missing pin. A profile with a gap can never be called qualified."""
        gaps = []
        if not self.revision or not re.fullmatch("[0-9a-f]{40}", self.revision):
            gaps.append("revision")
        if not self.license:
            gaps.append("license")
        if not self.layer_targets:
            gaps.append("layer_targets")
        if not any(name == self.tokenizer_file for name, _ in self.files):
            gaps.append("tokenizer_file")
        gaps.extend(
            f"digest:{name}"
            for name, digest in self.files
            if digest is None or not _DIGEST.fullmatch(digest)
        )
        return tuple(gaps)

    @property
    def pinned(self) -> bool:
        return not self.pin_gaps

    @property
    def is_fixture(self) -> bool:
        """Generated families are software fixtures and never production families."""
        return self.family_id.startswith("generated-")

    @property
    def identity(self) -> str:
        """Stable digest over every pinned field: the family's exact identity."""
        document = json.dumps(profile_document(self), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(document.encode()).hexdigest()


def profile_document(profile: ModelFamilyProfile) -> dict[str, Any]:
    return {
        "schema_version": VERSION,
        "family_id": profile.family_id,
        "role": profile.role.value,
        "architecture": profile.architecture,
        "publisher_ref": profile.publisher_ref,
        "revision": profile.revision,
        "license": profile.license,
        "task": profile.task,
        "formatter": profile.formatter,
        "tuning_method": profile.tuning_method,
        "layer_targets": list(profile.layer_targets),
        "tokenizer_file": profile.tokenizer_file,
        "files": dict(profile.files),
        "resource": {
            "max_seconds": profile.resource.max_seconds,
            "max_rows": profile.resource.max_rows,
            "max_steps": profile.resource.max_steps,
            "max_address_space_bytes": profile.resource.max_address_space_bytes,
            "incremental_cost_minor_units": profile.resource.incremental_cost_minor_units,
        },
        "scorer": profile.scorer,
        "benchmark_contract": profile.benchmark_contract,
    }


_FIELDS = frozenset(
    {
        "schema_version",
        "family_id",
        "role",
        "architecture",
        "publisher_ref",
        "revision",
        "license",
        "task",
        "formatter",
        "tuning_method",
        "layer_targets",
        "tokenizer_file",
        "files",
        "resource",
        "scorer",
        "benchmark_contract",
    }
)


def parse_profile(raw: Any) -> ModelFamilyProfile:
    """Strictly parse a declarative profile document. Unknown or missing fields are refused."""
    if (
        not isinstance(raw, dict)
        or raw.keys() != _FIELDS
        or raw["schema_version"] != VERSION
        or not isinstance(raw["files"], dict)
        or not isinstance(raw["resource"], dict)
        or not isinstance(raw["layer_targets"], list)
    ):
        raise FamilyError("INVALID_FAMILY_PROFILE")
    res = raw["resource"]
    try:
        resource = ResourceEstimate(
            max_seconds=_positive(res["max_seconds"]),
            max_rows=_positive(res["max_rows"]),
            max_steps=_positive(res["max_steps"]),
            max_address_space_bytes=_positive(res["max_address_space_bytes"]),
            incremental_cost_minor_units=_positive(res["incremental_cost_minor_units"], 0),
        )
        return ModelFamilyProfile(
            family_id=_text(raw["family_id"]),
            role=Role(raw["role"]),
            architecture=_text(raw["architecture"]),
            publisher_ref=_text(raw["publisher_ref"]),
            revision=None if raw["revision"] is None else _text(raw["revision"]),
            license=_text(raw["license"], allow_empty=True),
            task=_text(raw["task"]),
            formatter=_text(raw["formatter"]),
            tuning_method=_text(raw["tuning_method"]),
            layer_targets=tuple(_text(item) for item in raw["layer_targets"]),
            tokenizer_file=_text(raw["tokenizer_file"]),
            files=tuple(
                (_text(name), None if digest is None else _text(digest))
                for name, digest in sorted(raw["files"].items())
            ),
            resource=resource,
            scorer=_text(raw["scorer"]),
            benchmark_contract=_text(raw["benchmark_contract"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise FamilyError("INVALID_FAMILY_PROFILE") from exc


def load_profile(path: Path) -> ModelFamilyProfile:
    try:
        return parse_profile(json.loads(path.read_bytes()))
    except (OSError, ValueError) as exc:
        if isinstance(exc, FamilyError):
            raise
        raise FamilyError("INVALID_FAMILY_PROFILE") from exc


def _text(value: Any, *, allow_empty: bool = False) -> str:
    if (
        not isinstance(value, str)
        or (not value and not allow_empty)
        or len(value) > 256
        or value.strip() != value
    ):
        raise FamilyError("INVALID_FAMILY_PROFILE")
    return value


def _positive(value: Any, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise FamilyError("INVALID_FAMILY_PROFILE")
    return value


def verify_model_directory(profile: ModelFamilyProfile, directory: Path) -> str:
    """Hash every pinned file in an approved model directory. Returns the pinned revision."""
    if not profile.pinned or profile.revision is None:
        raise FamilyError("FAMILY_NOT_PINNED")
    for name, expected in profile.files:
        source = directory / name
        if source.is_symlink() or not source.is_file():
            raise FamilyError("INSTALLED_MODEL_FILE_MISSING")
        digest = hashlib.sha256()
        with source.open("rb") as handle:
            while chunk := handle.read(1 << 20):
                digest.update(chunk)
        if digest.hexdigest() != expected:
            raise FamilyError("INSTALLED_MODEL_MISMATCH")
    return profile.revision


class ProfileRegistry:
    """Map an owner's model reference to exactly one family. Never substitutes another model."""

    def __init__(self) -> None:
        self._by_ref: dict[str, ModelFamilyProfile] = {}

    def register(self, model_ref: str, profile: ModelFamilyProfile) -> None:
        if model_ref in self._by_ref:
            raise FamilyError("AMBIGUOUS_FAMILY_REGISTRATION")
        self._by_ref[model_ref] = profile

    def resolve(self, binding: ModelBinding) -> ModelFamilyProfile:
        profile = self._by_ref.get(binding.model_ref)
        if profile is None:
            raise FamilyError("MODEL_FAMILY_NOT_REGISTERED")
        if (
            binding.immutable_revision != profile.revision
            or binding.task != profile.task
            or binding.preprocessing_revision != profile.formatter
        ):
            raise FamilyError("MODEL_SUBSTITUTION_REFUSED")
        return profile


@dataclass(frozen=True)
class QualificationRecord:
    family_id: str
    family_identity: str
    revision: str | None
    architecture: str
    role: Role
    fixture_kind: FixtureKind
    benchmark_contract: str
    scorer: str
    results: tuple[tuple[str, bool], ...]
    tier: EvidenceTier
    external_gates: tuple[str, ...]
    details: tuple[tuple[str, str], ...] = ()

    @property
    def passed(self) -> bool:
        return all(ok for _, ok in self.results)

    @property
    def qualified(self) -> bool:
        return self.passed and self.tier is not EvidenceTier.FIXTURE


def required_checks(profile: ModelFamilyProfile) -> tuple[Check, ...]:
    checks: list[Check] = [c for c in Check if c is not Check.REFERENCE_REPRODUCED]
    if profile.role is Role.REFERENCE:
        checks.insert(0, Check.REFERENCE_REPRODUCED)
    return tuple(checks)


def build_record(
    profile: ModelFamilyProfile,
    fixture_kind: FixtureKind,
    results: Mapping[Check, bool],
    details: Mapping[str, str] | None = None,
) -> QualificationRecord:
    """Combine observed check outcomes. A missing check is a failure, never a skip."""
    required = required_checks(profile)
    unknown = set(results) - set(Check)
    if unknown:
        raise FamilyError("UNKNOWN_QUALIFICATION_CHECK")
    ordered = tuple((check.value, results.get(check) is True) for check in required)
    gates: list[str] = ["customer_witness_not_recorded"]
    if fixture_kind is FixtureKind.GENERATED:
        gates.append("installed_model_and_gpu_or_cpu_run_not_performed")
    gates.extend(f"profile_pin_missing:{gap}" for gap in profile.pin_gaps)
    installed = fixture_kind is FixtureKind.INSTALLED_MODEL and profile.pinned
    tier = EvidenceTier.SOFTWARE_QUALIFIED if installed else EvidenceTier.FIXTURE
    return QualificationRecord(
        family_id=profile.family_id,
        family_identity=profile.identity,
        revision=profile.revision,
        architecture=profile.architecture,
        role=profile.role,
        fixture_kind=fixture_kind,
        benchmark_contract=profile.benchmark_contract,
        scorer=profile.scorer,
        results=ordered,
        tier=tier,
        external_gates=tuple(gates),
        details=tuple(sorted((details or {}).items())),
    )


def require_distinct_families_on_one_benchmark(
    first: QualificationRecord, second: QualificationRecord
) -> None:
    """Two records qualify two different architectures against one benchmark contract."""
    if (
        first.family_id == second.family_id
        or first.architecture == second.architecture
        or first.family_identity == second.family_identity
    ):
        raise FamilyError("SECOND_FAMILY_NOT_DISTINCT")
    if (first.benchmark_contract, first.scorer) != (second.benchmark_contract, second.scorer):
        raise FamilyError("BENCHMARK_CONTRACT_MISMATCH")
