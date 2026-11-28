"""Declarative open-weight model-family profiles and their separate qualification records.

A profile pins one checkpoint, tokenizer, architecture, supported task, data formatter, tuning
method, layer targets, license and resource estimate. A qualification record belongs to exactly one
family revision and is never inherited by another family, and a customer model is never silently
replaced by a reference model.

Simulated: the unit tests exercise the record rules with generated sentinel fixtures.
Not proven: no real checkpoint, GPU or provider capacity is used here. A record built from a
generated fixture has ``EvidenceTier.FIXTURE`` and lists its unsatisfied external gates.

Trust boundary of ``QualificationRecord``: qualification here is caller-asserted and
self-consistent only, and there is no signing contract. Nothing in this module authenticates the
party that built a record, signs it, holds a key for it or checks it against an authority. What is
enforced is internal consistency: a tier above ``FIXTURE`` is refused unless the record is an
installed-model record of a pinned (40-hex) revision, names a family whose id is an exact ``str``
and not a fixture name (see ``is_fixture_family_id``), carries exactly one boolean outcome for
every check its role requires, lists no missing-pin gate, and (for PROVIDER_WITNESSED) carries a
``witness_ref`` of a defined shape. ``witness_ref`` is an unverified opaque string: its shape is
checked, its meaning never is. CUSTOMER_WITNESSED is unreachable (OPE-566 owner ruling): it needs
an ``opentour.pilot.customer-witness.v1`` record that only the OPE-566 ``PilotVerifier`` may
check, which has not landed, so a record is SOFTWARE_QUALIFIED at most and a CUSTOMER_WITNESSED
claim is refused with ``CUSTOMER_WITNESS_UNSTRUCTURED`` whatever ``witness_ref`` says. A
record is a frozen dataclass and Python cannot make that tamper-proof (``object.__setattr__``
bypasses it), so a consumer that needs authority must obtain it from a signed or journaled source
outside this module, not from a record's own fields.

Precondition on the caller: records are assumed NOT to cross a trust boundary. The checks above
hold only while the record is built and read inside one trusted process by code that does not
forge it. If a record is ever stored, transmitted or accepted from another party it needs signing
(signature and key custody), which this project does not have. These checks are a real floor
(coverage, normalized generated-family ban, ``witness_ref`` and ``revision`` shape), not a proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from opentour_cell.pilot.connector_ext import EvidenceTier
from opentour_cell.pilot.model_connector import ModelBinding

VERSION = "opentour.pilot.model-family.v1"
_DIGEST = re.compile("[0-9a-f]{64}")
_REVISION = re.compile("[0-9a-f]{40}")
# An opaque, unverified reference: one token, no whitespace or control characters, bounded length.
_WITNESS_REF = re.compile("[A-Za-z0-9][A-Za-z0-9._:/#@-]{0,255}")
_GENERATED = "generated"


def _identity_key(text: str) -> str:
    """Fold a family id the way the sibling modules fold identity keys.

    NFKC, drop invisible format characters (category Cf), casefold and strip surrounding
    whitespace. Deliberately nothing more: separators inside the id are kept, so distinct ids stay
    distinct.
    """
    folded = unicodedata.normalize("NFKC", text)
    return "".join(ch for ch in folded if unicodedata.category(ch) != "Cf").casefold().strip()


def is_generated_family_id(family_id: object) -> bool:
    """True when the id names a generated fixture family under any case, spacing or separator.

    ``generated`` followed by a non-alphanumeric separator (``-``, ``_``, a space, ...) or standing
    alone is a generated family. ``generatedx`` or ``regenerated-x`` is a different id and is not.

    This is the narrow ``generated`` prefix rule only. It is not the qualification gate: profiles
    and records use ``is_fixture_family_id``, which is wider and refuses anything but an exact
    ``str``.
    """
    if type(family_id) is not str:
        return False
    key = _identity_key(family_id)
    if not key.startswith(_GENERATED):
        return False
    rest = key[len(_GENERATED) :]
    return not rest or not rest[0].isalnum()


# ---- Fixture-name downgrade and the customer-witness cap (OPE-566 owner ruling) ----
#
# These mirror the OPE-509 rules in ``custom_trainer_connector`` (#218) so that a model family and
# a customer trainer are downgraded by one rule:
#
# * A family id must be an exact ``str``: a subclass could override ``casefold``, ``strip`` or
#   ``__eq__`` and so choose its own key. It is NFKC-normalised and casefolded, and an id carrying
#   invisible, control or non-ASCII characters is refused outright (FIXTURE), so case, fullwidth,
#   zero-width and homoglyph variants of a fixture marker cannot pass.
# * A family id CONTAINING a generated, simulated, reference, fixture or sentinel token, or naming
#   a ``.invalid`` host (with or without a trailing dot, port or path), is FIXTURE. This rule can
#   only lower a tier, never raise one; a real customer family whose id contains a token must be
#   renamed.
# * CUSTOMER_WITNESSED needs an ``opentour.pilot.customer-witness.v1`` record checked only by the
#   OPE-566 ``PilotVerifier``; nothing in this module may implement that check. ``PilotVerifier``
#   has not landed, and no model-family witness subject (which fields a witness of a family
#   qualification binds) is decided, so a ``QualificationRecord`` can reach SOFTWARE_QUALIFIED at
#   most. A ``witness_ref`` string is never a witness record: a CUSTOMER_WITNESSED claim that is
#   otherwise consistent is refused with ``CUSTOMER_WITNESS_UNSTRUCTURED`` (the #218
#   ``customer_witness_unstructured`` reason), and CUSTOMER_WITNESSED is unreachable.

# Any one of these tokens anywhere in a normalised family id marks a fixture family.
_FIXTURE_TOKENS = frozenset({"generated", "simulated", "reference", "fixture", "sentinel"})
# ``.invalid`` as a whole host label: followed by the end, a dot, a port, a path or anything that
# cannot continue a label.
_INVALID_HOST = re.compile(r"\.invalid(?![a-z0-9-])")
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Cc", "Co", "Cs", "Cn", "Zl", "Zp"})


def _invisible(text: str) -> bool:
    return any(unicodedata.category(ch) in _INVISIBLE_CATEGORIES for ch in text)


def _family_key(family_id: object) -> str | None:
    """NFKC, casefold and strip a family id; ``None`` when it is not a clean identity.

    Only an exact ``str`` is accepted: a subclass's overridden methods would otherwise run here.
    """
    if type(family_id) is not str or _invisible(family_id):
        return None
    key = unicodedata.normalize("NFKC", family_id).casefold().strip()
    if not key or _invisible(key) or not all(" " <= ch <= "~" for ch in key):
        return None
    return key


def is_fixture_family_id(family_id: object) -> bool:
    """True for an id containing a fixture token, a ``.invalid`` host, or an unclean identity."""
    key = _family_key(family_id)
    if key is None:
        return True
    return any(token in key for token in _FIXTURE_TOKENS) or _INVALID_HOST.search(key) is not None


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
        if not self.revision or not _REVISION.fullmatch(self.revision):
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
        """Fixture-named families are software fixtures and never production families."""
        return is_fixture_family_id(self.family_id)

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
    witness_ref: str = ""

    def __post_init__(self) -> None:
        """Refuse a tier the record's own evidence cannot support (fail closed).

        Choice: refuse at construction rather than only report ``qualified == False``. A record
        that claims ``SOFTWARE_QUALIFIED`` or above with no or partial check coverage is not a weak
        qualification, it is an inconsistent claim, and the existing tier rules already refuse
        inconsistent claims here. ``qualified`` re-checks coverage as defence in depth.
        """
        if not isinstance(self.tier, EvidenceTier):
            raise FamilyError("INVALID_EVIDENCE_TIER")
        if self.tier is EvidenceTier.FIXTURE:
            return
        witnessed = self.tier in (EvidenceTier.PROVIDER_WITNESSED, EvidenceTier.CUSTOMER_WITNESSED)
        if (
            self.fixture_kind is not FixtureKind.INSTALLED_MODEL
            or type(self.family_id) is not str
            or is_fixture_family_id(self.family_id)
            or type(self.revision) is not str
            or not _REVISION.fullmatch(self.revision)
            or any(gate.startswith("profile_pin_missing:") for gate in self.external_gates)
            or not self.covers_required_checks
            or not _witness_ref_ok(self.witness_ref, required=witnessed)
        ):
            raise FamilyError("EVIDENCE_TIER_UNSUPPORTED")
        if self.tier is EvidenceTier.CUSTOMER_WITNESSED:
            # A witness string is never an ``opentour.pilot.customer-witness.v1`` record, and only
            # the OPE-566 ``PilotVerifier`` (not landed) may accept one. The cap is
            # SOFTWARE_QUALIFIED.
            raise FamilyError("CUSTOMER_WITNESS_UNSTRUCTURED")

    @property
    def covers_required_checks(self) -> bool:
        """Exactly one boolean outcome for every check this role requires, and no others."""
        try:
            names = [name for name, _ in self.results]
            outcomes = [ok for _, ok in self.results]
            required = {check.value for check in required_checks_for_role(self.role)}
        except (TypeError, ValueError):
            return False
        return (
            len(names) == len(set(names))
            and set(names) == required
            and all(type(ok) is bool for ok in outcomes)
        )

    @property
    def passed(self) -> bool:
        """Every required check was recorded and passed. No recorded evidence is never a pass."""
        return self.covers_required_checks and all(ok for _, ok in self.results)

    @property
    def qualified(self) -> bool:
        return self.passed and self.tier is not EvidenceTier.FIXTURE


def _witness_ref_ok(value: object, *, required: bool) -> bool:
    """A witness reference must be a well-formed opaque exact ``str``; the witnessed tiers need one.

    Shape only. At CUSTOMER_WITNESSED a well-formed string is still refused (see ``__post_init__``).
    """
    if type(value) is not str:
        return False
    if not value:
        return not required
    return _WITNESS_REF.fullmatch(value) is not None


def required_checks_for_role(role: Role) -> tuple[Check, ...]:
    checks: list[Check] = [c for c in Check if c is not Check.REFERENCE_REPRODUCED]
    if Role(role) is Role.REFERENCE:
        checks.insert(0, Check.REFERENCE_REPRODUCED)
    return tuple(checks)


def required_checks(profile: ModelFamilyProfile) -> tuple[Check, ...]:
    return required_checks_for_role(profile.role)


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
    if profile.is_fixture:
        gates.append("generated_family_is_never_a_qualified_model")
    installed = (
        fixture_kind is FixtureKind.INSTALLED_MODEL and profile.pinned and not profile.is_fixture
    )
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
