"""Independently validated synthetic training variants from verified workplace corrections.

A variant is a *child* of exactly one real, independently verified, training-partition root.
Children never become observed mistakes, never add independent families and never reach
protected evaluation. A generator's own label cannot admit a child: only a separate validator
that sees the policy and the changed fields, but never the proposed label, can.

Everything here is a worker-local, deterministic, no-spend port. Generators and validators are
supplied by the caller; this module performs no model calls and fits nothing.

Fit gate: a training-mode plan names a fit *reservation*; whether that fit is open is read,
read-only, from the worker's own `LearningMandates` ledger (the standing learning mandate's
reservation, which already enforces recurrence class confirmation and the fit budget). The
caller cannot declare a fit open, and this module never reserves a fit, so variants can neither
unlock a fit nor request a second one. One root's training augmentation binds to at most one
in-flight fit experiment. The reservation, the experiment state and the pinned store identity
are re-checked at admission, so a late validation callback cannot admit into a withdrawn,
stale or substituted fit. Retracting or reopening a root marks every experiment derived from it
stale, invalidates its pending proposals, re-holds co-member roots' held children and excludes
their admitted children from fit counts; final outcomes and earlier records are kept.

Trust labels (see `TRUST`): `target_verified`, `verifier_independent`, the `Grant`,
`approved_validators` and the split seal are caller-asserted; no worker record of target
verification or of the generation grant exists in this cell. None of them can make a child
fit-eligible on its own: admission also needs the worker-held fit reservation and agreement with
the ledger's own policy re-derivation, and a verifier identical to the generator or to the fit's
delegate/trainer is refused whatever the flags say. The manifest digest is self-consistent only
and unsigned; no signing or key-custody contract exists.

Recorded gaps: (R2) a retracted root keeps its case/label, so a case cannot be re-registered
under a new root_ref after reopening (needs an explicit reopen operation the spec does not
define); (R4) the seal digest is not part of the root pin, so children of a later plan carry
that plan's split_digest while manifests use the root's stored one; (R5) ledger files made by
earlier revisions are not migrated (nothing is released); (G1) `LearningMandates` exposes no
read-only reservation lifecycle state (`grant` and `inspect` omit reserved/running/settled), so
a child can still bind to a reservation whose fit was already claimed or settled; (G2) the
reservation exposes only an opaque `residual_revision`, not its residual family set, so it is
recorded but not bound to the root's family; (G3) the store identity is pinned on a ledger's
first use, so a *fresh* variant ledger file trusts whichever mandate store it first sees, and
two separate ledger files can bind one root to two reservations (a store-side binding needs a
`learning_mandate` change); run one variant ledger per engagement directory. Nothing signs any
of this.

Simulated, not proven: all generators, validators and grants are caller-supplied local doubles.
Nothing here is provider, customer-evidence or fit-qualification evidence, and a validated child
is never a real observation.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, Protocol

from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.simple_repair import identifier
from opentour_cell.pilot.text_classifier import TrainingError

Json = dict[str, Any]
SCHEMA = "opentour.pilot.training-variants.v1"
MANIFEST_SCHEMA = "opentour.pilot.training-variant-manifest.v1"

Split = Literal["training", "development", "protected"]
Mode = Literal["preview", "training"]
Kind = Literal["paraphrase", "formatting", "distractor", "counterfactual"]
INVARIANT_KINDS: frozenset[str] = frozenset({"paraphrase", "formatting", "distractor"})
DEFAULT_MAX_CHILDREN_PER_ROOT = 2

# Dispositions of a stored child.
HELD = "held"  # passed static checks, awaiting independent validation: zero fit admission
ADMITTED = "admitted"
PREVIEW_VALID = "validated_preview"  # validated, but a preview can never feed a fit
QUARANTINED = "quarantined"
REJECTED = "rejected"
INVALIDATED = "invalidated"
_LIVE = (HELD, ADMITTED, PREVIEW_VALID)
# Rejections that depend on state which can change (grants, gates, budget, missing inputs).
# Replaying the same plan re-evaluates these in place; all other rejections are final.
_RETRYABLE = frozenset(
    {
        "fit_gate_closed",
        "grant_missing",
        "execution_location_unapproved",
        "authorization_exhausted",
        "decision_input_missing",
        "policy_clause_missing",
        "unverified_target",
        "verifier_not_independent",
        "policy_revision_mismatch",
        "fit_already_bound",
        "fit_experiment_stale",
        "fit_authority_mismatch",
    }
)
# A pending proposal: awaiting validation, or a rejection a replay would re-evaluate.
_PENDING_REJECTIONS = _RETRYABLE

# Fit experiment states (one per worker-held fit reservation).
EXPERIMENT_IN_FLIGHT = "in_flight"
EXPERIMENT_STALE = "stale"
FIT_GATE_SOURCE = "learning_mandate_reservation"
FIT_AUTHORITY_MISMATCH = "fit_authority_mismatch"
# Class-level behaviour of the worker's mandate ledger; an instance may shadow none of it.
_AUTHORITY_METHODS: frozenset[str] = frozenset(
    name
    for name, value in vars(LearningMandates).items()
    if callable(value) or isinstance(value, staticmethod | classmethod | property)
)

# What each input's authority actually is. Reported verbatim in every manifest.
TRUST: Mapping[str, str] = MappingProxyType(
    {
        "fit_gate": "worker_ledger:" + FIT_GATE_SOURCE,
        "target_verified": "caller_asserted",
        "verifier_independent": "caller_asserted_refuted_by_generator_or_fit_delegate_identity",
        "grant": "caller_asserted",
        "approved_validators": "caller_asserted_pinned_at_first_registration",
        "validator_label": "checked_against_ledger_policy_rederivation",
        "split_seal": "caller_asserted_pinned_at_first_registration",
    }
)
SEAL_KIND = "digest_self_consistent_only"


@dataclass(frozen=True)
class Policy:
    """Customer-approved rule set bound to one revision.

    `clauses` maps a decision-time field to the target that applies when that field is absent.
    """

    revision: str
    decision_fields: tuple[str, ...]
    required_inputs: tuple[str, ...]
    clauses: Mapping[str, str]

    def __post_init__(self) -> None:
        # A validator receives this object: never share a mutable mapping with it.
        object.__setattr__(self, "clauses", MappingProxyType(dict(self.clauses)))


@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    revision: str
    kind: Kind
    editable_fields: frozenset[str]
    max_edits: int = 1


@dataclass(frozen=True)
class Grant:
    grant_ref: str
    generation_authorized: bool
    training_authorized: bool
    execution_location_approved: bool
    budget: int
    expires_at: int
    approved_external_teachers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Generator:
    ref: str
    revision: str
    external: bool = False


@dataclass(frozen=True)
class SeedRoot:
    root_ref: str
    case_ref: str
    label_ref: str
    family_id: str
    split: Split
    policy_revision: str
    source_revision: str
    fields: Mapping[str, str]
    prediction: str
    target: str
    target_verified: bool
    target_verifier_ref: str
    verifier_independent: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))


@dataclass(frozen=True)
class SplitSeal:
    """Case/family/split map sealed before any seed is selected. Carries no labels or answers."""

    assignments: Mapping[str, tuple[str, ...]]  # family_id -> splits observed for that family
    cases: Mapping[str, tuple[str, ...]]  # case_ref -> families it was sealed under
    digest: str

    @staticmethod
    def from_rows(rows: Iterable[tuple[str, str, str]]) -> SplitSeal:
        """Rows are (case_ref, family_id, split). No label or answer is retained."""
        seen: dict[str, set[str]] = {}
        case_families: dict[str, set[str]] = {}
        for case_ref, family_id, split in rows:
            identifier(case_ref)
            identifier(family_id)
            if split not in {"training", "development", "protected"}:
                raise TrainingError("invalid_split")
            seen.setdefault(family_id, set()).add(split)
            case_families.setdefault(case_ref, set()).add(family_id)
        assignments = {fam: tuple(sorted(s)) for fam, s in sorted(seen.items())}
        cases = {c: tuple(sorted(f)) for c, f in sorted(case_families.items())}
        sealed = digest(["opentour.variants.split-seal.v2", assignments, cases])
        return SplitSeal(assignments, cases, sealed)


@dataclass(frozen=True)
class FitGate:
    """Reference to a fit reservation held by the worker's `LearningMandates` ledger.

    Deliberately carries no open/closed state: the ledger reads the reservation (standing
    recurrence class, fit budget, expiry, withdrawal and delegate) from the worker's own
    mandate record. A caller can name a reservation, never open one.
    """

    reservation_id: str
    actor: str


@dataclass(frozen=True)
class ProposedChild:
    recipe_id: str
    edits: Mapping[str, str | None]  # field -> new value; None removes the field
    proposed_label: str
    cost: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "edits", MappingProxyType(dict(self.edits)))


@dataclass(frozen=True)
class GenerationPlan:
    plan_ref: str
    mode: Mode
    root: SeedRoot
    policy: Policy
    grant: Grant
    seal: SplitSeal
    recipes: Mapping[str, Recipe]
    generator: Generator
    proposals: tuple[ProposedChild, ...]
    fit_gate: FitGate | None = None
    max_children_per_root: int = DEFAULT_MAX_CHILDREN_PER_ROOT
    max_children_per_family: int = DEFAULT_MAX_CHILDREN_PER_ROOT
    # Independent validators this plan authorizes; empty means nothing can be admitted.
    approved_validators: frozenset[str] = frozenset()


@dataclass(frozen=True)
class ValidationRequest:
    """Everything an independent validator may read. There is deliberately no label field."""

    policy: Policy
    base_target: str
    child_fields: Mapping[str, str]
    changed_fields: tuple[str, ...]


@dataclass(frozen=True)
class ValidatorDecision:
    validator_ref: str
    label: str | None  # None means the validator could not derive one (ambiguous)


class Validator(Protocol):
    def validate(self, request: ValidationRequest) -> ValidatorDecision: ...


@dataclass(frozen=True)
class PolicyValidator:
    """Deterministic rule-derived validator: re-derives the label from the bound policy."""

    validator_ref: str

    def validate(self, request: ValidationRequest) -> ValidatorDecision:
        label = derive_label(request.policy, request.base_target, request.child_fields)
        return ValidatorDecision(self.validator_ref, label)


@dataclass(frozen=True)
class SubmitResult:
    child_ids: tuple[str, ...]
    created: int
    replayed: int
    stopped: bool
    not_attempted: int


def derive_label(policy: Policy, base_target: str, fields: Mapping[str, str]) -> str | None:
    """Label from customer policy only. None when the policy cannot decide one answer."""
    return _derive(policy.clauses, base_target, fields)


def _derive(clauses: Mapping[str, str], base_target: str, fields: Mapping[str, str]) -> str | None:
    labels = {label for name, label in clauses.items() if not fields.get(name)}
    if not labels:
        return base_target
    return next(iter(labels)) if len(labels) == 1 else None


def _identity(value: str) -> str:
    """Canonical form for comparing actor identities (width, case, invisible characters)."""
    text = unicodedata.normalize("NFKC", value)
    text = "".join(c for c in text if unicodedata.category(c) not in {"Cf", "Cc"})
    return text.strip().casefold()


def _tokens(value: str) -> str:
    """Order-, case-, width-, accent- and punctuation-insensitive token multiset."""
    text = unicodedata.normalize("NFKD", value)
    text = "".join(c for c in text if unicodedata.category(c) not in {"Mn", "Cf", "Cc"})
    return " ".join(sorted(re.findall(r"[^\W_]+", text.casefold())))


def _normal(fields: Mapping[str, str]) -> str:
    return digest(sorted((_identity(k), _tokens(v)) for k, v in fields.items()))


def render_with_spans(policy: Policy, fields: Mapping[str, str]) -> tuple[str, list[Json]]:
    """Rebuild the decision text and each permitted field's evidence span from scratch."""
    parts: list[str] = []
    spans: list[Json] = []
    cursor = 0
    for name in policy.decision_fields:
        value = fields.get(name)
        if value is None:
            spans.append({"field": name, "start": None, "end": None, "present": False})
            continue
        line = f"{name}: {value}"
        start = cursor + len(name) + 2
        spans.append({"field": name, "start": start, "end": start + len(value), "present": True})
        parts.append(line)
        cursor += len(line) + 1
    return "\n".join(parts), spans


def _ident(value: object) -> str:
    """An identity is an exact `str` (no subclass can override equality or hashing)."""
    if type(value) is not str:
        raise TrainingError("invalid_identity_type")
    return identifier(value)


def _text_field(value: object) -> str:
    if type(value) is not str:
        raise TrainingError("invalid_identity_type")
    return value


def _flag(value: object) -> bool:
    if type(value) is not bool:
        raise TrainingError("invalid_flag_type")
    return value


def _int(value: object) -> int:
    if type(value) is not int:
        raise TrainingError("invalid_integer_type")
    return value


def _check_policy(policy: object) -> None:
    if type(policy) is not Policy:
        raise TrainingError("invalid_plan_type")
    _text_field(policy.revision)
    for seq in (policy.decision_fields, policy.required_inputs):
        if type(seq) is not tuple:
            raise TrainingError("invalid_plan_type")
        for name in seq:
            _text_field(name)
    for key, value in policy.clauses.items():
        _text_field(key)
        _text_field(value)


def _check_seal(seal: object) -> None:
    if type(seal) is not SplitSeal:
        raise TrainingError("invalid_plan_type")
    _text_field(seal.digest)
    for table in (seal.assignments, seal.cases):
        for key, values in table.items():
            _text_field(key)
            for value in values:
                _text_field(value)


def _check_plan(plan: GenerationPlan, now: object) -> None:
    """Reject look-alike identities and truthy non-booleans before anything is compared."""
    if type(plan) is not GenerationPlan:
        raise TrainingError("invalid_plan_type")
    _int(now)
    _ident(plan.plan_ref)
    root, grant, gen = plan.root, plan.grant, plan.generator
    if type(root) is not SeedRoot or type(grant) is not Grant or type(gen) is not Generator:
        raise TrainingError("invalid_plan_type")
    for name in ("root_ref", "case_ref", "label_ref", "family_id"):
        _ident(getattr(root, name))
    for name in ("policy_revision", "source_revision", "prediction", "target"):
        _text_field(getattr(root, name))
    _text_field(root.target_verifier_ref)
    _flag(root.target_verified)
    _flag(root.verifier_independent)
    _ident(grant.grant_ref)
    for flag in (
        grant.generation_authorized,
        grant.training_authorized,
        grant.execution_location_approved,
    ):
        _flag(flag)
    _int(grant.budget)
    _int(grant.expires_at)
    if type(grant.approved_external_teachers) is not frozenset:
        raise TrainingError("invalid_identity_type")
    for ref in grant.approved_external_teachers:
        _text_field(ref)
    _text_field(gen.ref)
    _text_field(gen.revision)
    _flag(gen.external)
    if type(plan.approved_validators) is not frozenset:
        raise TrainingError("invalid_identity_type")
    for ref in plan.approved_validators:
        _text_field(ref)
    _int(plan.max_children_per_root)
    _int(plan.max_children_per_family)
    _text_field(plan.mode)
    for key, value in root.fields.items():
        _text_field(key)
        _text_field(value)
    _check_policy(plan.policy)
    _check_seal(plan.seal)
    for recipe_id, recipe in plan.recipes.items():
        _text_field(recipe_id)
        if type(recipe) is not Recipe or type(recipe.editable_fields) is not frozenset:
            raise TrainingError("invalid_plan_type")
        for value in (recipe.recipe_id, recipe.revision, recipe.kind, *recipe.editable_fields):
            _text_field(value)
        _int(recipe.max_edits)
    if type(plan.proposals) is not tuple:
        raise TrainingError("invalid_plan_type")
    for proposal in plan.proposals:
        if type(proposal) is not ProposedChild:
            raise TrainingError("invalid_plan_type")
        _int(proposal.cost)
        _text_field(proposal.recipe_id)
        _text_field(proposal.proposed_label)
        for key, edit in proposal.edits.items():
            _text_field(key)
            if edit is not None:
                _text_field(edit)
    if plan.fit_gate is not None:
        if type(plan.fit_gate) is not FitGate:
            raise TrainingError("invalid_fit_gate")
        _ident(plan.fit_gate.reservation_id)
        _ident(plan.fit_gate.actor)


def _pin(plan: GenerationPlan) -> str:
    """Digest of everything about a root that must never change after first registration."""
    root, policy = plan.root, plan.policy
    return digest(
        [
            "opentour.variants.root-pin.v1",
            root.root_ref,
            root.case_ref,
            root.label_ref,
            root.family_id,
            root.source_revision,
            root.prediction,
            root.target,
            sorted(root.fields.items()),
            policy.revision,
            list(policy.decision_fields),
            list(policy.required_inputs),
            sorted(policy.clauses.items()),
            sorted(plan.approved_validators),
        ]
    )


def _verifier_reason(seed: SeedRoot, generator: Generator, fit: Json | None) -> str | None:
    """Refute a caller-asserted independent verification from identities the ledger knows."""
    if not (
        seed.target_verified is True
        and seed.verifier_independent is True
        and _identity(seed.target_verifier_ref)
    ):
        return "unverified_target"
    who = _identity(seed.target_verifier_ref)
    dependent = {_identity(generator.ref)}
    if fit is not None:
        dependent |= {_identity(str(fit.get(k, ""))) for k in ("delegate", "trainer")}
    return "verifier_not_independent" if who in dependent else None


def _seed_reason(
    seed: SeedRoot,
    policy: Policy,
    grant: Grant,
    seal: SplitSeal,
    now: int,
    generator: Generator,
    fit: Json | None,
) -> tuple[str, Json | None] | None:
    if seed.split == "protected":
        return "protected_seed", None
    if seed.split != "training":
        return "not_training_partition", None
    splits = seal.assignments.get(seed.family_id, ())
    if len(splits) != 1 or splits[0] != seed.split:
        return "cross_split_family", None
    if seal.cases.get(seed.case_ref) != (seed.family_id,):
        return "case_family_mismatch", None
    verifier = _verifier_reason(seed, generator, fit)
    if verifier is not None:
        return verifier, None
    if seed.policy_revision != policy.revision:
        return "policy_revision_mismatch", None
    if (
        not (grant.generation_authorized is True and grant.training_authorized is True)
        or now >= grant.expires_at
    ):
        return "grant_missing", None
    if grant.execution_location_approved is not True:
        return "execution_location_unapproved", None
    if any(name not in policy.decision_fields for name in seed.fields):
        return "field_not_permitted", None
    for name in policy.required_inputs:
        if not seed.fields.get(name):
            return "decision_input_missing", {
                "kind": "data_context_request",
                "field": name,
                "accountable_role": "data_custodian",
            }
    if derive_label(policy, seed.target, seed.fields) != seed.target:
        return "seed_inconsistent_with_policy", None
    return None


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS roots (
    root_ref TEXT PRIMARY KEY, family_id TEXT NOT NULL, state TEXT NOT NULL,
    reason TEXT NOT NULL, policy_revision TEXT NOT NULL, seal_digest TEXT NOT NULL,
    cap_root INTEGER NOT NULL, cap_family INTEGER NOT NULL,
    case_ref TEXT NOT NULL, label_ref TEXT NOT NULL, pin_digest TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS families (
    family_id TEXT PRIMARY KEY, cap_family INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS root_scopes (
    root_ref TEXT NOT NULL, mode TEXT NOT NULL, grant_ref TEXT NOT NULL,
    PRIMARY KEY (root_ref, mode, grant_ref)
);
CREATE TABLE IF NOT EXISTS children (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id TEXT NOT NULL UNIQUE,
    idem_key TEXT NOT NULL UNIQUE,
    root_ref TEXT NOT NULL,
    family_id TEXT NOT NULL,
    grant_ref TEXT NOT NULL,
    plan_ref TEXT NOT NULL,
    mode TEXT NOT NULL,
    disposition TEXT NOT NULL,
    reason TEXT NOT NULL,
    cost INTEGER NOT NULL,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiments (
    reservation_id TEXT PRIMARY KEY, state TEXT NOT NULL, reason TEXT NOT NULL,
    record TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS experiment_roots (
    reservation_id TEXT NOT NULL, root_ref TEXT NOT NULL,
    PRIMARY KEY (reservation_id, root_ref)
);
CREATE TABLE IF NOT EXISTS authority_pin (
    slot INTEGER PRIMARY KEY CHECK (slot = 1),
    authority_identity TEXT NOT NULL, ledger_identity TEXT NOT NULL, record TEXT NOT NULL
);
"""


class VariantLedger:
    """Durable, replay-safe store of root state, children, spend, experiments and manifests.

    `fit_authority` is the worker's own learning-mandate ledger. Without it, no training-mode
    child can ever pass the fit gate. On first use the ledger pins that store's identity
    (realpaths and device/inode of its directory and databases, organization, engagement and
    scope) together with this ledger file's own identity; any later mismatch, on bind or on
    admission, is `fit_authority_mismatch`. Run one variant ledger per engagement directory.
    """

    def __init__(
        self, path: Path | str = ":memory:", *, fit_authority: LearningMandates | None = None
    ) -> None:
        if fit_authority is not None and (
            type(fit_authority) is not LearningMandates
            or _AUTHORITY_METHODS & set(vars(fit_authority))
        ):
            raise TrainingError("fit_authority_must_be_worker_mandate_ledger")
        self._fit_authority = fit_authority
        self._path = str(path)
        self._shared: sqlite3.Connection | None = None
        if self._path == ":memory:":
            self._shared = sqlite3.connect(":memory:", isolation_level=None)
        db = self._shared or sqlite3.connect(self._path, isolation_level=None, timeout=30)
        try:
            db.executescript(_SCHEMA_SQL)
        finally:
            if self._shared is None:
                db.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        db = self._shared or sqlite3.connect(self._path, isolation_level=None, timeout=30)
        try:
            db.execute("BEGIN IMMEDIATE")
        except BaseException:
            if self._shared is None:
                db.close()
            raise
        try:
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            if self._shared is None:
                db.close()

    @staticmethod
    def _spent(db: sqlite3.Connection, grant_ref: str) -> int:
        row = db.execute(
            "SELECT COALESCE(SUM(cost), 0) FROM children WHERE grant_ref = ?", (grant_ref,)
        ).fetchone()
        return int(row[0])

    @staticmethod
    def _insert(
        db: sqlite3.Connection,
        key: str,
        plan: GenerationPlan,
        disposition: str,
        reason: str,
        cost: int,
        record: Json,
    ) -> str:
        child_id = "var-" + key[:24]
        record = {**record, "child_id": child_id, "disposition": disposition, "reason": reason}
        db.execute(
            "INSERT INTO children (child_id, idem_key, root_ref, family_id, grant_ref, plan_ref,"
            " mode, disposition, reason, cost, record) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                child_id,
                key,
                plan.root.root_ref,
                plan.root.family_id,
                plan.grant.grant_ref,
                plan.plan_ref,
                plan.mode,
                disposition,
                reason,
                cost,
                json.dumps(record, sort_keys=True),
            ),
        )
        return child_id

    @staticmethod
    def _update(
        db: sqlite3.Connection, child_id: str, disposition: str, reason: str, record: Json
    ) -> None:
        record = {**record, "disposition": disposition, "reason": reason}
        db.execute(
            "UPDATE children SET disposition=?, reason=?, record=? WHERE child_id=?",
            (disposition, reason, json.dumps(record, sort_keys=True), child_id),
        )

    def submit(self, plan: GenerationPlan, *, now: int) -> SubmitResult:
        """Record each proposal once. Replaying the same plan adds no rows and spends nothing."""
        _check_plan(plan, now)
        if plan.mode not in {"preview", "training"}:
            raise TrainingError("invalid_mode")
        # Read the worker-held reservation before taking this ledger's write lock.
        gate_status, fit, authority_id = self._resolve_fit_gate(plan)
        fit_info: Json | None = None
        if plan.mode == "training":
            fit_info = {
                "source": FIT_GATE_SOURCE,
                "reservation_id": plan.fit_gate.reservation_id if plan.fit_gate else None,
                "status": gate_status or "reserved",
            }
        ids: list[str] = []
        created = replayed = not_attempted = 0
        stopped = False
        with self._tx() as db:
            root = plan.root
            row = db.execute(
                "SELECT state, family_id, pin_digest FROM roots WHERE root_ref=?",
                (root.root_ref,),
            ).fetchone()
            invalid_root = row is not None and row[0] != "live"
            seed_block: tuple[str, Json | None] | None = None
            if invalid_root:
                pass
            elif row is not None and row[1] != root.family_id:
                seed_block = ("root_identity_conflict", None)
            elif row is not None and row[2] != _pin(plan):
                # Target, fields, case, policy and validators are fixed at first registration.
                seed_block = ("root_pin_mismatch", None)
            else:
                seed_block = (
                    self._lineage_block(db, root)
                    or _seed_reason(
                        root, plan.policy, plan.grant, plan.seal, now, plan.generator, fit
                    )
                    or (self._duplicate_case(db, root) if row is None else None)
                )
            if row is None and seed_block is None:
                db.execute(
                    "INSERT INTO roots (root_ref, family_id, state, reason, policy_revision,"
                    " seal_digest, cap_root, cap_family, case_ref, label_ref, pin_digest)"
                    " VALUES (?,?,'live','',?,?,?,?,?,?,?)",
                    (
                        root.root_ref,
                        root.family_id,
                        plan.policy.revision,
                        plan.seal.digest,
                        plan.max_children_per_root,
                        plan.max_children_per_family,
                        root.case_ref,
                        root.label_ref,
                        _pin(plan),
                    ),
                )
                db.execute(
                    "INSERT OR IGNORE INTO families (family_id, cap_family) VALUES (?,?)",
                    (root.family_id, plan.max_children_per_family),
                )
            if not invalid_root and seed_block is None:
                db.execute(
                    "INSERT OR IGNORE INTO root_scopes (root_ref, mode, grant_ref) VALUES (?,?,?)",
                    (root.root_ref, plan.mode, plan.grant.grant_ref),
                )
            fit_reason: str | None = None
            if plan.mode == "training":
                if fit is None or authority_id is None:
                    mismatch = gate_status == FIT_AUTHORITY_MISMATCH
                    fit_reason = FIT_AUTHORITY_MISMATCH if mismatch else "fit_gate_closed"
                elif not invalid_root and seed_block is None:
                    fit_reason = self._bind_experiment(db, plan, fit, authority_id)
                    if fit_reason == FIT_AUTHORITY_MISMATCH and fit_info is not None:
                        fit_info = {**fit_info, "status": FIT_AUTHORITY_MISMATCH}
            for index, proposal in enumerate(plan.proposals):
                if stopped:
                    not_attempted += 1
                    continue
                key = self._key(plan, proposal)
                existing = db.execute(
                    "SELECT child_id, disposition, reason, record FROM children WHERE idem_key=?",
                    (key,),
                ).fetchone()
                if existing is not None and not (
                    existing[1] == REJECTED and existing[2] in _RETRYABLE
                ):
                    ids.append(str(existing[0]))
                    replayed += 1
                    continue
                base = {**self._base_record(plan, proposal, index), "fit_gate": fit_info}
                disposition, reason, cost, record, hit_stop = self._evaluate(
                    db, plan, proposal, base, invalid_root, seed_block, fit_reason
                )
                stopped = stopped or hit_stop
                if existing is None:
                    cid = self._insert(db, key, plan, disposition, reason, cost, record)
                    created += 1
                else:
                    # Re-evaluate a state-dependent rejection in place; keep the audit trail.
                    cid = str(existing[0])
                    if reason == existing[2] and disposition == REJECTED:
                        replayed += 1
                    else:
                        prior: Json = json.loads(existing[3])
                        history = [
                            *prior.get("history", []),
                            {
                                "disposition": existing[1],
                                "reason": existing[2],
                                "plan_ref": prior.get("plan_ref"),
                            },
                        ]
                        record = {
                            **record,
                            "child_id": cid,
                            "history": history,
                            "disposition": disposition,
                            "reason": reason,
                        }
                        db.execute(
                            "UPDATE children SET disposition=?, reason=?, cost=?, plan_ref=?,"
                            " grant_ref=?, mode=?, record=? WHERE child_id=?",
                            (
                                disposition,
                                reason,
                                cost,
                                plan.plan_ref,
                                plan.grant.grant_ref,
                                plan.mode,
                                json.dumps(record, sort_keys=True),
                                cid,
                            ),
                        )
                        created += 1
                ids.append(cid)
        return SubmitResult(tuple(ids), created, replayed, stopped, not_attempted)

    def _authority_identity(self) -> str | None:
        """Identity of the wired-in mandate store as it is on disk now; None if unusable."""
        auth = self._fit_authority
        if type(auth) is not LearningMandates or _AUTHORITY_METHODS & set(vars(auth)):
            return None
        try:
            directory, path = Path(auth.directory), Path(auth.path)
            if path != directory / "learning-mandates.sqlite3":
                return None
            parts: list[object] = [
                "opentour.variants.fit-authority.v1",
                auth.organization,
                auth.engagement,
                auth.scope,
            ]
            for item in (directory, path, directory / "training.sqlite3"):
                info = os.stat(item, follow_symlinks=False)
                parts += [os.path.realpath(item), info.st_dev, info.st_ino]
        except (OSError, TypeError, ValueError):
            return None
        return digest(parts)

    def _ledger_identity(self) -> str:
        """This ledger file's own identity, so a copied or recreated ledger is detected."""
        if self._shared is not None:
            return "memory"
        try:
            info = os.stat(self._path)
        except OSError:
            return "missing"
        return digest(
            ["opentour.variants.ledger.v1", os.path.realpath(self._path), info.st_dev, info.st_ino]
        )

    def _check_pin(
        self, db: sqlite3.Connection, authority_id: str, *, create: bool = True
    ) -> str | None:
        """Pin the authority on first bind; afterwards any other store is a mismatch.

        A missing pin is itself a mismatch once any experiment exists (the pin was cleared),
        and admission never creates one.
        """
        mine = self._ledger_identity()
        row = db.execute(
            "SELECT authority_identity, ledger_identity FROM authority_pin WHERE slot=1"
        ).fetchone()
        if row is None:
            if not create or db.execute("SELECT 1 FROM experiments LIMIT 1").fetchone():
                return FIT_AUTHORITY_MISMATCH
            auth = self._fit_authority
            record = {
                "organization": getattr(auth, "organization", None),
                "engagement": getattr(auth, "engagement", None),
                "directory": os.path.realpath(getattr(auth, "directory", "")),
            }
            db.execute(
                "INSERT INTO authority_pin (slot, authority_identity, ledger_identity, record)"
                " VALUES (1,?,?,?)",
                (authority_id, mine, json.dumps(record, sort_keys=True)),
            )
            return None
        return None if (row[0], row[1]) == (authority_id, mine) else FIT_AUTHORITY_MISMATCH

    def _read_reservation(
        self, reservation_id: str, actor: str
    ) -> tuple[str | None, Json | None, str | None]:
        """Read one reservation read-only. Returns (why_closed, permission, authority_id)."""
        if self._fit_authority is None:
            return "no_worker_fit_authority", None, None
        authority_id = self._authority_identity()
        if authority_id is None:
            return FIT_AUTHORITY_MISMATCH, None, None
        try:
            # Through the class, never an instance attribute the caller could have replaced.
            fit = LearningMandates.grant(self._fit_authority, reservation_id, actor=actor)
        except MandateError as exc:
            return str(exc) or "fit_reservation_unavailable", None, None
        if fit.get("reservation_id") != reservation_id:
            return "fit_reservation_outside_training_scope", None, None
        if "train" not in fit.get("allowed_uses", ()):
            return "fit_reservation_outside_training_scope", None, None
        return None, fit, authority_id

    def _resolve_fit_gate(self, plan: GenerationPlan) -> tuple[str | None, Json | None, str | None]:
        """Read the plan's fit reservation from the worker's mandate ledger (read-only).

        Never reserves or claims a fit.
        """
        if plan.mode != "training":
            return None, None, None
        if self._fit_authority is None:
            return "no_worker_fit_authority", None, None
        gate = plan.fit_gate
        if gate is None:
            return "fit_reservation_absent", None, None
        return self._read_reservation(gate.reservation_id, gate.actor)

    def _bind_experiment(
        self, db: sqlite3.Connection, plan: GenerationPlan, fit: Json, authority_id: str
    ) -> str | None:
        """Bind this root's training augmentation to one in-flight fit experiment.

        A stale experiment never takes new children, and a root already bound to another
        in-flight reservation cannot be bound to a second one (no doubled fit). Stale
        experiments do not count as a binding, so a still-permitted root can move to a fresh
        reservation after a co-member was retracted.
        """
        if self._check_pin(db, authority_id) is not None:
            return FIT_AUTHORITY_MISMATCH
        rid = str(fit["reservation_id"])
        root_ref = plan.root.root_ref
        found = db.execute(
            "SELECT state, record FROM experiments WHERE reservation_id=?", (rid,)
        ).fetchone()
        if found is not None and json.loads(found[1]).get("authority_identity") != authority_id:
            return FIT_AUTHORITY_MISMATCH
        if found is not None and found[0] != EXPERIMENT_IN_FLIGHT:
            return "fit_experiment_stale"
        other = db.execute(
            "SELECT e.reservation_id FROM experiment_roots r JOIN experiments e"
            " ON e.reservation_id=r.reservation_id WHERE r.root_ref=? AND e.state=?"
            " AND e.reservation_id<>? LIMIT 1",
            (root_ref, EXPERIMENT_IN_FLIGHT, rid),
        ).fetchone()
        if other is not None:
            return "fit_already_bound"
        if found is None:
            record: Json = {
                "reservation_id": rid,
                "source": FIT_GATE_SOURCE,
                "authority_identity": authority_id,
                "actor": plan.fit_gate.actor if plan.fit_gate else None,
                "mandate_id": fit.get("mandate_id"),
                "mandate_revision": fit.get("mandate_revision"),
                "run_id": fit.get("run_id"),
                "plan_revision": fit.get("plan_revision"),
                "intake_revision": fit.get("intake_revision"),
                "residual_revision": fit.get("residual_revision"),
                "delegate": fit.get("delegate"),
                "opened_by_plan_ref": plan.plan_ref,
                "state": EXPERIMENT_IN_FLIGHT,
                "reason": "",
                "history": [],
            }
            db.execute(
                "INSERT INTO experiments (reservation_id, state, reason, record) VALUES (?,?,'',?)",
                (rid, EXPERIMENT_IN_FLIGHT, json.dumps(record, sort_keys=True)),
            )
        db.execute(
            "INSERT OR IGNORE INTO experiment_roots (reservation_id, root_ref) VALUES (?,?)",
            (rid, root_ref),
        )
        return None

    def _admission_fit_reason(
        self, db: sqlite3.Connection, record: Json, root_ref: str
    ) -> str | None:
        """Re-check the bound fit at admission: in-flight, same store, reservation still held."""
        rid = (record.get("fit_gate") or {}).get("reservation_id")
        found = db.execute(
            "SELECT e.state, e.record FROM experiments e JOIN experiment_roots r"
            " ON r.reservation_id=e.reservation_id WHERE e.reservation_id=? AND r.root_ref=?",
            (rid, root_ref),
        ).fetchone()
        if found is None:
            return "fit_gate_closed"
        if found[0] != EXPERIMENT_IN_FLIGHT:
            return "fit_experiment_stale"
        experiment: Json = json.loads(found[1])
        why, fit, authority_id = self._read_reservation(str(rid), str(experiment["actor"]))
        if why == FIT_AUTHORITY_MISMATCH:
            return FIT_AUTHORITY_MISMATCH
        if fit is None or authority_id is None:
            return "fit_gate_closed"
        if (
            self._check_pin(db, authority_id, create=False) is not None
            or experiment.get("authority_identity") != authority_id
        ):
            return FIT_AUTHORITY_MISMATCH
        return None

    @staticmethod
    def _duplicate_case(db: sqlite3.Connection, root: SeedRoot) -> tuple[str, Json | None] | None:
        """One real case or label is one root: re-registering it never adds a mistake."""
        found = db.execute(
            "SELECT 1 FROM roots WHERE root_ref<>? AND (case_ref IN (?,?) OR label_ref IN (?,?))"
            " LIMIT 1",
            (root.root_ref, root.case_ref, root.label_ref, root.case_ref, root.label_ref),
        ).fetchone()
        return ("duplicate_case", None) if found else None

    @staticmethod
    def _lineage_block(db: sqlite3.Connection, root: SeedRoot) -> tuple[str, Json | None] | None:
        """A synthetic child (or a copy of one) can never be registered as a real root."""
        refs = (root.root_ref, root.case_ref, root.label_ref)
        if (
            root.root_ref.startswith("var-")
            or db.execute(
                "SELECT 1 FROM children WHERE child_id IN (?,?,?) LIMIT 1", refs
            ).fetchone()
        ):
            return "synthetic_lineage_seed", None
        want = _normal(root.fields)
        for (rec_json,) in db.execute(
            "SELECT record FROM children WHERE root_ref<>? AND disposition IN (?,?,?)",
            (root.root_ref, *_LIVE),
        ):
            if _normal(json.loads(rec_json).get("fields", {})) == want:
                return "synthetic_lineage_seed", None
        return None

    def _evaluate(
        self,
        db: sqlite3.Connection,
        plan: GenerationPlan,
        proposal: ProposedChild,
        base: Json,
        invalid_root: bool,
        seed_block: tuple[str, Json | None] | None,
        fit_reason: str | None,
    ) -> tuple[str, str, int, Json, bool]:
        if invalid_root:
            return REJECTED, "root_invalidated", 0, self._bare(base), False
        if seed_block is not None:
            extra: Json = {"request": seed_block[1]} if seed_block[1] else {}
            return REJECTED, seed_block[0], 0, {**self._bare(base), **extra}, False
        reason, request, record = self._static_checks(db, plan, proposal, base, fit_reason)
        cost = proposal.cost if reason is None else 0
        stop = False
        if reason is None and (
            cost < 0 or self._spent(db, plan.grant.grant_ref) + cost > plan.grant.budget
        ):
            reason, request, cost, stop = "authorization_exhausted", None, 0, True
        if request:
            record = {**record, "request": request}
        disposition = HELD if reason is None else REJECTED
        return disposition, reason or "independent_validation_absent", cost, record, stop

    @staticmethod
    def _key(plan: GenerationPlan, proposal: ProposedChild) -> str:
        recipe = plan.recipes.get(proposal.recipe_id)
        return digest(
            [
                "opentour.variants.child.v1",
                plan.root.root_ref,
                proposal.recipe_id,
                recipe.revision if recipe else None,
                sorted(proposal.edits.items()),
                proposal.proposed_label,
                plan.generator.ref,
                plan.generator.revision,
                plan.mode,
                _pin(plan),  # a corrected plan is a new attempt, not a replay of a rejected one
            ]
        )

    @staticmethod
    def _bare(base: Json) -> Json:
        """Seed-level rejections keep refs only: no field values from a refused root."""
        keep = (
            "schema_version",
            "origin",
            "mode",
            "plan_ref",
            "parent_case_ref",
            "parent_label_ref",
            "root_ref",
            "family_id",
            "grant_ref",
            "policy_revision",
            "split_digest",
            "recipe",
            "generator",
            "fit_gate",
        )
        return {k: base[k] for k in keep}

    @staticmethod
    def _base_record(plan: GenerationPlan, proposal: ProposedChild, index: int) -> Json:
        recipe = plan.recipes.get(proposal.recipe_id)
        return {
            "schema_version": SCHEMA,
            "origin": "synthetic",
            "mode": plan.mode,
            "plan_ref": plan.plan_ref,
            "proposal_index": index,
            "parent_case_ref": plan.root.case_ref,
            "parent_label_ref": plan.root.label_ref,
            "root_ref": plan.root.root_ref,
            "family_id": plan.root.family_id,
            "source_revision": plan.root.source_revision,
            "policy_revision": plan.policy.revision,
            "grant_ref": plan.grant.grant_ref,
            "split_digest": plan.seal.digest,
            "base_target": plan.root.target,
            "policy_clauses": dict(plan.policy.clauses),
            "approved_validators": sorted(plan.approved_validators),
            "recipe": {
                "recipe_id": proposal.recipe_id,
                "revision": recipe.revision if recipe else None,
                "kind": recipe.kind if recipe else None,
            },
            "generator": {
                "ref": plan.generator.ref,
                "revision": plan.generator.revision,
                "external": plan.generator.external,
            },
            "proposed_label": proposal.proposed_label,
            "cost": proposal.cost,
            "changed_fields": sorted(proposal.edits),
            "invariant_fields": sorted(set(plan.root.fields) - set(proposal.edits)),
            "weight": None,
            "validator_ref": None,
            "derived_label": None,
            "request": None,
        }

    @staticmethod
    def _static_checks(
        db: sqlite3.Connection,
        plan: GenerationPlan,
        proposal: ProposedChild,
        base: Json,
        fit_reason: str | None,
    ) -> tuple[str | None, Json | None, Json]:
        root, policy, gen = plan.root, plan.policy, plan.generator
        recipe = plan.recipes.get(proposal.recipe_id)
        if gen.external and gen.ref not in plan.grant.approved_external_teachers:
            return "unapproved_external_teacher", None, base
        if recipe is None:
            return "unapproved_recipe", None, base
        if plan.mode == "training" and fit_reason is not None:
            # Decided only by the worker-held reservation and this ledger's experiments.
            return fit_reason, None, base
        if any(name not in root.fields for name in proposal.edits):
            return "invented_field", None, base
        if (
            not proposal.edits
            or len(proposal.edits) > recipe.max_edits
            or any(name not in recipe.editable_fields for name in proposal.edits)
            or (recipe.kind != "counterfactual" and None in proposal.edits.values())
        ):
            return "multi_edit_outside_recipe", None, base
        if recipe.kind == "counterfactual":
            unclaused = [n for n in sorted(proposal.edits) if n not in policy.clauses]
            if unclaused:
                request: Json = {
                    "kind": "owner_decision",
                    "field": unclaused[0],
                    "accountable_role": "owner",
                }
                return "policy_clause_missing", request, base
        child = dict(root.fields)
        for name, value in proposal.edits.items():
            if value is None:
                child.pop(name, None)
            else:
                child[name] = value
        text, spans = render_with_spans(policy, child)
        base = {**base, "fields": child, "evidence_spans": spans, "rendered_text": text}
        # Near-copies of the root or of any live sibling in the family (same mode) are one example.
        if _normal(child) == _normal(root.fields):
            return "near_duplicate", None, base
        marks = ",".join("?" * len(_LIVE))
        for (rec_json,) in db.execute(
            "SELECT record FROM children WHERE family_id=? AND mode=?"
            f" AND disposition IN ({marks})",
            (root.family_id, plan.mode, *_LIVE),
        ):
            if _normal(json.loads(rec_json).get("fields", {})) == _normal(child):
                return "near_duplicate", None, base
        caps = db.execute(
            "SELECT cap_root FROM roots WHERE root_ref=?", (root.root_ref,)
        ).fetchone()
        fam = db.execute(
            "SELECT cap_family FROM families WHERE family_id=?", (root.family_id,)
        ).fetchone()
        cap_root = min(plan.max_children_per_root, caps[0])
        cap_family = min(plan.max_children_per_family, fam[0])
        live_root = db.execute(
            "SELECT COUNT(*) FROM children WHERE root_ref=? AND mode=?"
            f" AND disposition IN ({marks})",
            (root.root_ref, plan.mode, *_LIVE),
        ).fetchone()[0]
        if live_root >= cap_root:
            return "root_cap_exceeded", None, base
        live_family = db.execute(
            "SELECT COUNT(*) FROM children WHERE family_id=? AND mode=?"
            f" AND disposition IN ({marks})",
            (root.family_id, plan.mode, *_LIVE),
        ).fetchone()[0]
        if live_family >= cap_family:
            return "family_cap_exceeded", None, base
        return None, None, base

    def validate(self, child_id: str, decision: ValidatorDecision | None) -> str:
        """Apply an independent validator decision; return the child's resulting disposition.

        The base target, policy clauses and authorized validators come from the stored record,
        never from the caller. Serves first calls and late callbacks alike: an invalidated child
        or a retracted root can never be admitted by a callback.
        """
        _ident(child_id)
        if decision is not None:
            if type(decision) is not ValidatorDecision:
                raise TrainingError("invalid_validator_decision")
            _text_field(decision.validator_ref)
            if decision.label is not None:
                _text_field(decision.label)
        with self._tx() as db:
            row = db.execute(
                "SELECT record, root_ref, mode, family_id FROM children WHERE child_id=?",
                (child_id,),
            ).fetchone()
            if row is None:
                raise TrainingError("unknown_variant")
            record: Json = json.loads(row[0])
            state = db.execute("SELECT state FROM roots WHERE root_ref=?", (row[1],)).fetchone()
            current = str(record["disposition"])
            if current != HELD:
                return current  # decided or invalidated: a replay or late callback is a no-op
            if state is None or state[0] != "live":
                return current
            if decision is None:
                return HELD  # validation absent: hold, never pass
            ref = _identity(decision.validator_ref)
            if not ref or ref == _identity(record["generator"]["ref"]):
                raise TrainingError("self_validation_rejected")
            if ref not in {_identity(v) for v in record["approved_validators"]}:
                raise TrainingError("validator_not_authorized")
            base_target = str(record["base_target"])
            own = _derive(record["policy_clauses"], base_target, record["fields"])
            record = {
                **record,
                "validator_ref": decision.validator_ref,
                "derived_label": decision.label,
            }
            kind = record["recipe"]["kind"]
            if decision.label is None:
                new, why = QUARANTINED, "ambiguous_target"
            elif decision.label != record["proposed_label"]:
                new, why = QUARANTINED, "label_disagreement"
            elif decision.label != own:
                new, why = QUARANTINED, "validator_policy_disagreement"
            elif kind in INVARIANT_KINDS and decision.label != base_target:
                new, why = QUARANTINED, "invariant_label_changed"
            elif row[2] == "preview":
                new, why = PREVIEW_VALID, "preview_only"
            else:
                new, why = ADMITTED, "validated"
            if new == ADMITTED:
                # The fit gate is re-read at admission, not trusted from submit time.
                blocked = self._admission_fit_reason(db, record, str(row[1]))
                if blocked is not None:
                    entry = {
                        "disposition": HELD,
                        "reason": record.get("reason"),
                        "plan_ref": record.get("plan_ref"),
                    }
                    record = {**record, "history": [*record.get("history", []), entry]}
                    new, why = REJECTED, blocked
            self._update(db, child_id, new, why, record)
            if new == ADMITTED:
                self._reweigh(db, str(row[3]))
            return new

    def run(self, plan: GenerationPlan, validator: Validator | None, *, now: int) -> SubmitResult:
        """Submit, then ask the independent validator about every still-held child."""
        result = self.submit(plan, now=now)
        for child in self.children(plan.root.root_ref):
            if child["child_id"] not in result.child_ids or child["disposition"] != HELD:
                continue
            decision = None
            if validator is not None:
                decision = validator.validate(
                    ValidationRequest(
                        plan.policy,
                        plan.root.target,
                        MappingProxyType(dict(child["fields"])),
                        tuple(child["changed_fields"]),
                    )
                )
            self.validate(child["child_id"], decision)
        return result

    @staticmethod
    def _reweigh(db: sqlite3.Connection, family_id: str) -> None:
        """Split a family's synthetic influence cap (one real-example unit) across its children.

        Admitted children of a stale fit experiment take no share and keep no weight.
        """
        rows = [
            (str(child_id), json.loads(rec_json))
            for child_id, rec_json in db.execute(
                "SELECT child_id, record FROM children WHERE family_id=? AND disposition=?"
                " ORDER BY seq",
                (family_id, ADMITTED),
            ).fetchall()
        ]
        counted = [(child_id, rec) for child_id, rec in rows if not rec.get("fit_stale")]
        for child_id, rec in counted:
            rec["weight"] = str(Fraction(1, len(counted)))
            rec["sibling_count"] = len(counted)
            db.execute(
                "UPDATE children SET record=? WHERE child_id=?",
                (json.dumps(rec, sort_keys=True), child_id),
            )

    def retract_root(self, root_ref: str, family_id: str, reason: str) -> tuple[str, ...]:
        """Reopen/revoke a parent: invalidate its live descendants and pending proposals, and mark
        every fit experiment derived from it stale. Nothing can re-admit them.

        Final outcomes (quarantined or finally rejected children) are left exactly as recorded,
        and every changed record keeps its prior disposition, reason and history. Returns the
        invalidated child ids; `experiments()` shows the stale experiments.
        """
        _ident(root_ref)
        _ident(family_id)
        _ident(reason)
        with self._tx() as db:
            stored = db.execute(
                "SELECT family_id FROM roots WHERE root_ref=?", (root_ref,)
            ).fetchone()
            if stored is not None and stored[0] != family_id:
                raise TrainingError("root_identity_conflict")
            db.execute(
                "INSERT INTO roots (root_ref, family_id, state, reason, policy_revision,"
                " seal_digest, cap_root, cap_family, case_ref, label_ref, pin_digest)"
                " VALUES (?,?,'retracted',?,'','',0,0,'','','')"
                " ON CONFLICT(root_ref) DO UPDATE SET state='retracted', reason=excluded.reason",
                (root_ref, family_id, reason),
            )
            prior: list[str] = []
            rows = db.execute(
                "SELECT child_id, disposition, reason, record FROM children WHERE root_ref=?"
                " ORDER BY seq",
                (root_ref,),
            ).fetchall()
            for child_id, disposition, why, rec_json in rows:
                live = disposition in _LIVE
                pending = disposition == REJECTED and why in _PENDING_REJECTIONS
                if not (live or pending):
                    continue  # final outcomes and earlier invalidations are not rewritten
                rec: Json = json.loads(rec_json)
                rec["prior_disposition"] = disposition
                rec["prior_reason"] = why
                rec["history"] = [
                    *rec.get("history", []),
                    {"disposition": disposition, "reason": why, "plan_ref": rec.get("plan_ref")},
                ]
                rec["weight"] = None
                self._update(db, child_id, INVALIDATED, f"root_{reason}", rec)
                prior.append(str(child_id))
            self._stale_experiments(db, root_ref, f"root_{reason}")
            self._reweigh(db, family_id)
            return tuple(prior)

    def _stale_experiments(self, db: sqlite3.Connection, root_ref: str, why: str) -> None:
        """Every in-flight experiment that includes this root can no longer be committed.

        Co-member roots stay live, but their children bound to the stale experiment cannot feed
        it: held children are re-held (rejected `fit_experiment_stale`, retryable under a fresh
        reservation) and admitted children are marked `fit_stale` and excluded from fit counts,
        with their disposition, reason and history left as they were.
        """
        families: set[str] = set()
        for rid, state, rec_json in db.execute(
            "SELECT e.reservation_id, e.state, e.record FROM experiments e JOIN experiment_roots r"
            " ON r.reservation_id=e.reservation_id WHERE r.root_ref=? AND e.state=?"
            " ORDER BY e.reservation_id",
            (root_ref, EXPERIMENT_IN_FLIGHT),
        ).fetchall():
            rec: Json = json.loads(rec_json)
            rec["history"] = [*rec.get("history", []), {"state": state, "reason": rec["reason"]}]
            rec.update(state=EXPERIMENT_STALE, reason=why, stale_root_ref=root_ref)
            reheld: list[str] = []
            marked: list[str] = []
            for child_id, disposition, child_why, family, child_json in db.execute(
                "SELECT c.child_id, c.disposition, c.reason, c.family_id, c.record"
                " FROM children c JOIN experiment_roots r ON r.root_ref=c.root_ref"
                " WHERE r.reservation_id=? AND c.root_ref<>? AND c.mode='training'"
                " AND c.disposition IN (?,?) ORDER BY c.seq",
                (rid, root_ref, HELD, ADMITTED),
            ).fetchall():
                child: Json = json.loads(child_json)
                if (child.get("fit_gate") or {}).get("reservation_id") != rid:
                    continue
                if disposition == HELD:
                    child["prior_disposition"] = disposition
                    child["prior_reason"] = child_why
                    child["history"] = [
                        *child.get("history", []),
                        {
                            "disposition": disposition,
                            "reason": child_why,
                            "plan_ref": child.get("plan_ref"),
                        },
                    ]
                    self._update(db, child_id, REJECTED, "fit_experiment_stale", child)
                    reheld.append(str(child_id))
                elif not child.get("fit_stale"):
                    child["fit_stale"] = {"reservation_id": rid, "reason": why}
                    child["prior_weight"] = child.get("weight")
                    child["weight"] = None
                    db.execute(
                        "UPDATE children SET record=? WHERE child_id=?",
                        (json.dumps(child, sort_keys=True), child_id),
                    )
                    marked.append(str(child_id))
                    families.add(str(family))
            rec["reheld_child_ids"] = reheld
            rec["stale_admitted_child_ids"] = marked
            db.execute(
                "UPDATE experiments SET state=?, reason=?, record=? WHERE reservation_id=?",
                (EXPERIMENT_STALE, why, json.dumps(rec, sort_keys=True), rid),
            )
        for family in sorted(families):
            self._reweigh(db, family)

    def experiments(self) -> list[Json]:
        """Fit experiments with their member roots, in reservation order."""
        with self._tx() as db:
            rows = db.execute(
                "SELECT reservation_id, record FROM experiments ORDER BY reservation_id"
            ).fetchall()
            out = []
            for rid, rec_json in rows:
                roots = [
                    str(r[0])
                    for r in db.execute(
                        "SELECT root_ref FROM experiment_roots WHERE reservation_id=?"
                        " ORDER BY root_ref",
                        (rid,),
                    )
                ]
                out.append({**json.loads(rec_json), "root_refs": roots})
            return out

    def children(self, root_ref: str | None = None) -> list[Json]:
        with self._tx() as db:
            if root_ref is None:
                rows = db.execute("SELECT record FROM children ORDER BY seq").fetchall()
            else:
                rows = db.execute(
                    "SELECT record FROM children WHERE root_ref=? ORDER BY seq", (root_ref,)
                ).fetchall()
            return [json.loads(r[0]) for r in rows]

    def spent(self, grant_ref: str) -> int:
        with self._tx() as db:
            return self._spent(db, grant_ref)

    def manifest(
        self,
        *,
        seal: SplitSeal,
        policy_revision: str,
        grant_ref: str,
        mode: Mode,
        coverage_required_groups: int | None = None,
    ) -> Json:
        """Manifest for one (seal, policy revision, grant, mode) scope.

        Its `manifest_digest` is self-consistent only and unsigned (see `seal`).

        Family and mistake counts are derived from the ledger's own admitted roots and the
        sealed split, never from caller-supplied roots.
        """
        every = self.children()
        rows = [
            r
            for r in every
            if r["mode"] == mode
            and r["policy_revision"] == policy_revision
            and r["grant_ref"] == grant_ref
            and r["split_digest"] == seal.digest
        ]
        with self._tx() as db:
            roots = db.execute(
                "SELECT r.root_ref, r.family_id FROM roots r JOIN root_scopes s"
                " ON s.root_ref=r.root_ref WHERE r.state='live' AND r.policy_revision=?"
                " AND r.seal_digest=? AND s.mode=? AND s.grant_ref=?",
                (policy_revision, seal.digest, mode, grant_ref),
            ).fetchall()
        live_roots = [
            (str(a), str(b)) for a, b in roots if seal.assignments.get(b) == ("training",)
        ]
        child_ids = {r["child_id"] for r in every}
        # Admitted children of a stale fit experiment never count toward a fit.
        stale_admitted = sorted(
            r["child_id"] for r in rows if r["disposition"] == ADMITTED and r.get("fit_stale")
        )
        fit_admitted = [r for r in rows if r["disposition"] == ADMITTED and not r.get("fit_stale")]
        by_state = Counter(r["disposition"] for r in rows)
        reasons = Counter(
            r["reason"] for r in rows if r["disposition"] in {REJECTED, QUARANTINED, INVALIDATED}
        )
        siblings = Counter(r["root_ref"] for r in fit_admitted)
        family_siblings = Counter(r["family_id"] for r in fit_admitted)
        families = sorted({fam for _, fam in live_roots})
        body: Json = {
            "schema_version": MANIFEST_SCHEMA,
            "mode": mode,
            "policy_revision": policy_revision,
            "grant_ref": grant_ref,
            "split_digest": seal.digest,
            "generated_count": len(rows),
            "admitted_count": len(fit_admitted),
            "stale_admitted_count": len(stale_admitted),
            "stale_admitted_child_ids": stale_admitted,
            "preview_valid_count": by_state[PREVIEW_VALID],
            "held_count": by_state[HELD],
            "quarantined_count": by_state[QUARANTINED],
            "rejected_count": by_state[REJECTED],
            "invalidated_count": by_state[INVALIDATED],
            "exclusion_reasons": dict(sorted(reasons.items())),
            "observed_mistake_count": len(live_roots),
            "independent_family_count": len(families),
            "independent_families": families,
            "children_in_independent_count": sum(
                1 for ref, _ in live_roots if ref in child_ids or ref.startswith("var-")
            ),
            "root_sibling_counts": dict(sorted(siblings.items())),
            "family_sibling_counts": dict(sorted(family_siblings.items())),
            "final_weights": {r["child_id"]: r["weight"] for r in fit_admitted},
            "invalidated_child_ids": sorted(
                r["child_id"] for r in rows if r["disposition"] == INVALIDATED
            ),
            "requests": [r["request"] for r in rows if r.get("request")],
            "spend": self.spent(grant_ref),
            "children": [
                {
                    k: r.get(k)
                    for k in (
                        "child_id",
                        "root_ref",
                        "disposition",
                        "reason",
                        "recipe",
                        "validator_ref",
                        "weight",
                        "fit_stale",
                    )
                }
                for r in rows
            ],
        }
        if coverage_required_groups is not None:
            body["coverage"] = {
                "required_independent_groups": coverage_required_groups,
                "real_independent_groups": len(families),
                "filled": len(families) >= coverage_required_groups,
            }
        scoped_roots = {r["root_ref"] for r in rows} | {ref for ref, _ in live_roots}
        body["experiments"] = [
            {k: e.get(k) for k in ("reservation_id", "state", "reason", "root_refs")}
            for e in (self.experiments() if mode == "training" else [])
            if scoped_roots & set(e["root_refs"])
        ]
        body["trust"] = dict(TRUST)
        # Exactly what the "seal" is: a digest over this body, recomputable by anyone. Nothing
        # signs it and no key custody exists, so it proves self-consistency only.
        body["seal"] = {"kind": SEAL_KIND, "signed": False, "digest_field": "manifest_digest"}
        body["manifest_digest"] = digest(body)
        return body
