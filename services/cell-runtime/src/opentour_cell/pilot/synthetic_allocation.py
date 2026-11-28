"""Allocate a fixed synthetic-example budget across verified, independent error families.

The allocator is a deterministic, worker-local, no-spend planner plus a restart-safe budget
ledger. Inputs are development-side facts only: family verification status, independence of
the verifier, residual support, uncovered approved transformations and cost. There is no field,
parameter or code path for a protected answer or protected score; ``family_from_record``
rejects records that carry one. Nothing here generates text, calls a model or fits anything.

Rules enforced:

* The budget is a hard cap on both example units and spend. A request that does not fit is
  refused whole (never rounded or partially filled), including under replay and concurrency.
* A budget is bound to the run profile's ``unit_cost`` when it is opened. ``reserve`` charges
  ``units x bound unit_cost`` (never the plan's own line costs), recomputes the plan's
  ``allocation_id`` from its content and requires a replay to match the stored plan exactly;
  any mismatch is refused as ``plan_binding_mismatch``.
* Only families that are verified by an independent verifier, in the real training partition,
  not disputed, not retracted, not protected-derived, not child-derived, not held out for
  confirmation and not in a policy conflict can receive allocation. Near-duplicate families
  collapse to one.
* A family is one independent unit however many roots, copies or children it has.
* Zero eligible families, zero budget, poor validation yield or an unfittable mandatory slice
  return an explicit hold rather than an empty success.
* Default spend is zero: an unset budget holds.

Verification basis. ``FamilyInput.verification`` and ``verifier_independent`` are supplied by
the caller. When ``allocate`` is given a ``VerificationScope`` it reads OPE-516's
``VariantLedger.manifest`` itself and a family is eligible only if OPE-516 holds a live,
training-sealed root for it and every one of its case refs is sealed under that family alone;
the caller's flags can then only narrow eligibility, never widen it. Without a scope the plan
is labelled ``verification_basis="caller_asserted"``, and a budget opened against an OPE-516
grant refuses such a plan. OPE-516's own record of verification comes from its ``SeedRoot``
flags and is unsigned; this is a worker record, not proof.

OPE-516 link (read-only import of ``training_variants``). ``open_budget_for_grant`` narrows the
allocation spend cap to the grant's remaining headroom (``Grant.budget`` minus
``VariantLedger.spent``) when the budget is opened. Open: that headroom is a snapshot; OPE-516
spend made after the open is not re-checked at ``reserve``; the two modules' cost units are
assumed equal ("use the same cost unit across the run") and no contract defines them.
``covered_from_children`` reads OPE-516 children (disposition ``admitted`` only); OPE-516's
``independent_family_count`` is not consumed, only its ``independent_families`` list.

Simulated, not proven: every family, verification, cost and yield value is a generated
sentinel input supplied by the caller. Tests prove software behavior only. Nothing here is
provider, customer-evidence, fit-qualification or ROI evidence; with a fixed unit cost the
ranking is count-based and ``roi_claim`` is always false. Plans carry no signature: a caller
can still build a self-consistent plan, but it is charged at the bound unit cost.

Not implemented here (recorded gaps): the confirmation and repeat-entity *estimates* (this
module partitions the cohort; the metric needs model outcomes and belongs to OPE-517);
sample inclusion probabilities (no sampling design is specified); routing of owner-decision
and real-label requests (emitted as records only).
"""

from __future__ import annotations

import hashlib
import sqlite3
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training_variants import Grant, SplitSeal, VariantLedger

Json = dict[str, Any]
PLAN_SCHEMA = "opentour.pilot.synthetic-allocation.v1"
PROFILE_SCHEMA = "opentour.pilot.synthetic-allocation-profile.v1"
COHORT_SCHEMA = "opentour.pilot.confirmation-cohort.v1"

VERIFIED = "verified"
TRAINING = "training"
SYNTHETIC_PREFIX = "var-"
CALLER_ASSERTED = "caller_asserted"
WORKER_RECORD = "ope516_variant_ledger"
BINDING_MISMATCH = "plan_binding_mismatch"
Cell = tuple[str, str]
Stratum = tuple[str, ...]  # a cell, or a cell subdivided by model revision
# Keys that must never reach the allocator: protected answers and scores are evaluator-only.
PROTECTED_KEYS = frozenset(
    {
        "protected_label",
        "protected_answer",
        "protected_score",
        "protected_scores",
        "expected_answer",
        "answer_key",
        "target_verdict",
        "evaluator_score",
        "score",
    }
)
_RECORD_KEYS = frozenset(
    {
        "family_id",
        "cell",
        "split",
        "verification",
        "verifier_independent",
        "policy_conflict",
        "protected_derived",
        "model_revision",
        "roots",
    }
)
_ROOT_KEYS = frozenset(
    {"root_ref", "content_digest", "uncovered_recipes", "admitted_children", "case_ref"}
)


class AllocationError(ValueError):
    """Fail-closed refusal carrying a lowercase reason code."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


@dataclass(frozen=True)
class RootInput:
    root_ref: str
    content_digest: str
    uncovered_recipes: tuple[str, ...] = ()
    admitted_children: int = 0
    case_ref: str = ""  # the real case behind the root; one case may back only one family


@dataclass(frozen=True)
class FamilyInput:
    """One candidate error family. Development-side facts only; no protected data."""

    family_id: str
    cell: Cell  # (error_class, policy_branch)
    split: str
    verification: str  # verified | unverified | disputed | retracted (caller-asserted)
    verifier_independent: bool  # caller-asserted; see VerificationScope
    roots: tuple[RootInput, ...]
    policy_conflict: bool = False
    protected_derived: bool = False
    model_revision: str = ""  # model revision whose residual this family is


@dataclass(frozen=True)
class AllocationPolicy:
    """Versioned run profile. Every number lives here; none is baked into the allocator."""

    profile_revision: str
    seed: str
    total_units: int = 0
    spend_ceiling: int = 0
    unit_cost: int = 1
    cost_basis: str = "fixed_unit"  # fixed_unit (count-based ranking) | measured
    per_family_cap: int = 1
    per_root_cap: int = 1
    batch_size: int = 1
    exploration_quota: int = 0
    min_support: int = 1
    critical_cells: tuple[tuple[Cell, int], ...] = ()
    min_yield_milli: int | None = None
    observed_yield_milli: int | None = None
    # Owner's minimum independent support per model revision before a cell is subdivided by
    # revision. 0 never subdivides.
    revision_min_support: int = 0

    def validate(self) -> None:
        ints = (
            self.total_units,
            self.spend_ceiling,
            self.per_family_cap,
            self.per_root_cap,
            self.batch_size,
            self.exploration_quota,
            self.min_support,
            self.unit_cost,
            self.revision_min_support,
        )
        if any(type(v) is not int or v < 0 for v in ints):
            raise AllocationError("policy_invalid", "counts must be non-negative integers")
        if self.unit_cost < 1 or self.batch_size < 1:
            raise AllocationError("policy_invalid", "unit_cost and batch_size must be positive")
        if self.cost_basis not in ("fixed_unit", "measured"):
            raise AllocationError("policy_invalid", "cost_basis")
        if not self.profile_revision or not self.seed:
            raise AllocationError("policy_invalid", "profile_revision and seed are required")


@dataclass(frozen=True)
class Line:
    family_id: str
    root_ref: str
    recipe_id: str
    cell: Cell
    phase: str  # minimum | exploration | ranked | uniform_control
    cost: int
    stratum: Stratum = ()


@dataclass(frozen=True)
class FamilyDecision:
    family_id: str
    cell: Cell
    eligible: bool
    reason: str  # "eligible" or the lowercase exclusion reason
    units: int = 0


@dataclass(frozen=True)
class AllocationPlan:
    allocation_id: str
    status: str  # allocated | hold
    hold_reason: str
    lines: tuple[Line, ...]
    decisions: tuple[FamilyDecision, ...]
    requests: tuple[Json, ...]
    cell_scores: tuple[tuple[Stratum, str], ...]
    ranking_basis: str  # count_based | cost_based
    stop_reasons: tuple[str, ...]
    policy_digest: str
    input_digest: str
    control: bool = False
    roi_claim: bool = False
    detail: Json = field(default_factory=dict)
    match_units: int | None = None
    verification_basis: str = CALLER_ASSERTED
    verification_grant_ref: str = ""
    verification_ref: str = ""  # OPE-516 manifest digest when verified from the worker record
    holdout_ref: str = ""  # confirmation cohort digest when one was held out

    @property
    def units(self) -> int:
        return len(self.lines)

    @property
    def spend(self) -> int:
        return sum(line.cost for line in self.lines)

    def as_record(self) -> Json:
        return {
            "schema_version": PLAN_SCHEMA,
            "allocation_id": self.allocation_id,
            "status": self.status,
            "hold_reason": self.hold_reason,
            "control": self.control,
            "match_units": self.match_units,
            "units": self.units,
            "spend": self.spend,
            "ranking_basis": self.ranking_basis,
            "roi_claim": self.roi_claim,
            "verification_basis": self.verification_basis,
            "verification_grant_ref": self.verification_grant_ref,
            "verification_ref": self.verification_ref,
            "holdout_ref": self.holdout_ref,
            "stop_reasons": list(self.stop_reasons),
            "cell_scores": [[list(c), s] for c, s in self.cell_scores],
            "lines": _line_rows(self.lines),
            "decisions": [
                [d.family_id, list(d.cell), d.eligible, d.reason, d.units] for d in self.decisions
            ],
            "requests": list(self.requests),
            "policy_digest": self.policy_digest,
            "input_digest": self.input_digest,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class VerificationScope:
    """Where ``allocate`` reads verification from: one OPE-516 ledger manifest scope."""

    variants: VariantLedger
    seal: SplitSeal
    policy_revision: str
    grant_ref: str
    mode: str = "training"


@dataclass(frozen=True)
class CohortCase:
    """A real decided case, for the authorized frame or for later-time confirmation."""

    case_ref: str
    entity_ref: str
    decided_at: int  # integer time in the run profile's unit
    content_digest: str
    template_ref: str = ""
    attachment_digests: tuple[str, ...] = ()


@dataclass(frozen=True)
class ConfirmationCohort:
    """Later-time real confirmation cohort, partitioned before any generation or selection."""

    cutoff: int
    maturity_window: int
    now: int
    independent: tuple[str, ...]  # case refs for the independent confirmation estimate
    repeat_entity: tuple[str, ...]  # case refs for a separate repeat-entity estimate
    immature: tuple[tuple[str, int], ...]  # (case_ref, next_check) awaiting outcome maturity
    excluded: tuple[tuple[str, str], ...]  # (case_ref, reason)
    held_out: frozenset[str]  # normalized refs of every later-time case: never allocatable
    frame_cases: frozenset[str]  # normalized refs of the frame the leakage checks used
    cohort_digest: str

    def as_record(self) -> Json:
        return {
            "schema_version": COHORT_SCHEMA,
            "cutoff": self.cutoff,
            "maturity_window": self.maturity_window,
            "now": self.now,
            "independent": list(self.independent),
            "repeat_entity": list(self.repeat_entity),
            "immature": [list(x) for x in self.immature],
            "excluded": [list(x) for x in self.excluded],
            "counts": {
                "independent": len(self.independent),
                "repeat_entity": len(self.repeat_entity),
                "immature": len(self.immature),
                "excluded": len(self.excluded),
            },
            "cohort_digest": self.cohort_digest,
        }


def _line_rows(lines: Iterable[Line]) -> list[list[Any]]:
    return [
        [x.family_id, x.root_ref, x.recipe_id, list(x.cell), x.phase, x.cost, list(x.stratum)]
        for x in lines
    ]


def _norm(value: str) -> str:
    """Identity key for independence checks: case, width, whitespace and invisibles removed.

    Two spellings of one root, case or digest must never count as two independent units.
    """
    text = unicodedata.normalize("NFKC", value)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Cf")
    return "".join(text.casefold().split())


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def _is_str_tuple(value: object) -> bool:
    return type(value) is tuple and all(type(x) is str for x in value)


def _check_family(fam: object) -> FamilyInput:
    """Exact types on identity, flag and count fields; a coerced or subclassed value is refused."""
    if type(fam) is not FamilyInput:
        raise AllocationError("input_invalid", "family")
    fid = fam.family_id if type(fam.family_id) is str else "?"
    if not all(
        type(v) is str for v in (fam.family_id, fam.split, fam.verification, fam.model_revision)
    ):
        raise AllocationError("input_invalid", f"{fid}: identity fields must be str")
    if not (_is_str_tuple(fam.cell) and len(fam.cell) == 2):
        raise AllocationError("input_invalid", f"{fid}: cell")
    if not all(
        type(v) is bool
        for v in (fam.verifier_independent, fam.policy_conflict, fam.protected_derived)
    ):
        raise AllocationError("input_invalid", f"{fid}: flags must be bool")
    if type(fam.roots) is not tuple:
        raise AllocationError("input_invalid", f"{fid}: roots")
    for root in fam.roots:
        if type(root) is not RootInput or not all(
            type(v) is str for v in (root.root_ref, root.content_digest, root.case_ref)
        ):
            raise AllocationError("input_invalid", f"{fid}: root identity fields must be str")
        if not _is_str_tuple(root.uncovered_recipes):
            raise AllocationError("input_invalid", f"{fid}: uncovered_recipes")
        if type(root.admitted_children) is not int or root.admitted_children < 0:
            raise AllocationError("input_invalid", f"{fid}: admitted_children")
    return fam


def family_from_record(record: Mapping[str, Any]) -> FamilyInput:
    """Build a family from a plain record, refusing any protected answer or score key.

    Identity and count fields are taken only at their exact type; nothing is coerced with
    ``str()`` or ``int()``.
    """
    if not isinstance(record, Mapping):
        raise AllocationError("input_invalid", "record")
    raw_roots = record.get("roots", ())
    if not isinstance(raw_roots, list | tuple) or not all(
        isinstance(r, Mapping) for r in raw_roots
    ):
        raise AllocationError("roots_invalid")
    root_records = list(raw_roots)
    leaked = sorted(set(record) & PROTECTED_KEYS)
    for root in root_records:
        leaked += sorted(set(root) & PROTECTED_KEYS)
    if leaked:
        raise AllocationError("protected_input_rejected", ",".join(leaked))
    unknown = sorted(set(record) - _RECORD_KEYS)
    for root in root_records:
        unknown += sorted(set(root) - _ROOT_KEYS)
    if unknown:
        raise AllocationError("unknown_field", ",".join(unknown))
    raw_cell = record.get("cell")
    if (
        not isinstance(raw_cell, list | tuple)
        or len(raw_cell) != 2
        or not all(type(x) is str for x in raw_cell)
    ):
        raise AllocationError("cell_invalid")

    def text(source: Mapping[str, Any], key: str, default: str | None = None) -> str:
        value = source.get(key, default)
        if type(value) is not str:
            raise AllocationError("input_invalid", key)
        return value

    def flag(key: str) -> bool:
        value = record.get(key, False)
        if type(value) is not bool:
            raise AllocationError("input_invalid", key)
        return value

    roots: list[RootInput] = []
    for r in root_records:
        recipes = r.get("uncovered_recipes", ())
        if not isinstance(recipes, list | tuple) or not all(type(x) is str for x in recipes):
            raise AllocationError("input_invalid", "uncovered_recipes")
        children = r.get("admitted_children", 0)
        if type(children) is not int:
            raise AllocationError("input_invalid", "admitted_children")
        roots.append(
            RootInput(
                root_ref=text(r, "root_ref"),
                content_digest=text(r, "content_digest"),
                uncovered_recipes=tuple(recipes),
                admitted_children=children,
                case_ref=text(r, "case_ref", ""),
            )
        )
    return _check_family(
        FamilyInput(
            family_id=text(record, "family_id"),
            cell=(raw_cell[0], raw_cell[1]),
            split=text(record, "split"),
            verification=text(record, "verification"),
            verifier_independent=flag("verifier_independent"),
            roots=tuple(roots),
            policy_conflict=flag("policy_conflict"),
            protected_derived=flag("protected_derived"),
            model_revision=text(record, "model_revision", ""),
        )
    )


def covered_from_children(children: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, int]]:
    """Per root, count admitted children by recipe id. Only ``admitted`` counts.

    Held, preview, quarantined, rejected and invalidated children cover nothing, and a child's
    own family claim is never used to group anything here.
    """
    out: dict[str, dict[str, int]] = {}
    for child in children:
        if child.get("disposition") != "admitted":
            continue
        recipe = child.get("recipe") or {}
        root = str(child.get("root_ref", ""))
        rid = str(recipe.get("recipe_id", ""))
        if root and rid:
            counts = out.setdefault(root, {})
            counts[rid] = counts.get(rid, 0) + 1
    return out


def confirmation_cohort(
    frame: Iterable[CohortCase],
    candidates: Iterable[CohortCase],
    *,
    cutoff: int,
    maturity_window: int,
    now: int,
) -> ConfirmationCohort:
    """Partition later-time real cases into a confirmation cohort. Deterministic, no spend.

    ``frame`` is the authorized development/training frame; every frame case must be decided
    at or before ``cutoff``. A candidate is later-time when decided after ``cutoff`` and mature
    when ``decided_at + maturity_window <= now``. Every later-time candidate is held out of
    allocation whatever its classification. Cutoff and window are run-profile values.
    """
    for name, value in (("cutoff", cutoff), ("maturity_window", maturity_window), ("now", now)):
        if type(value) is not int or value < 0:
            raise AllocationError("cohort_invalid", name)
    frame_list = [_check_case(c) for c in frame]
    cand_list = sorted((_check_case(c) for c in candidates), key=lambda c: _norm(c.case_ref))
    late = sorted(c.case_ref for c in frame_list if c.decided_at > cutoff)
    if late:
        raise AllocationError("cutoff_overlaps_frame", ",".join(late))
    refs = [_norm(c.case_ref) for c in cand_list]
    if len(set(refs)) != len(refs):
        raise AllocationError("duplicate_case_ref")
    f_cases = {_norm(c.case_ref) for c in frame_list}
    f_entities = {_norm(c.entity_ref) for c in frame_list if _norm(c.entity_ref)}
    f_digests = {_norm(c.content_digest) for c in frame_list if _norm(c.content_digest)}
    f_templates = {_norm(c.template_ref) for c in frame_list if _norm(c.template_ref)}
    f_attach = {_norm(a) for c in frame_list for a in c.attachment_digests if _norm(a)}
    independent: list[str] = []
    repeat: list[str] = []
    immature: list[tuple[str, int]] = []
    excluded: list[tuple[str, str]] = []
    seen_digest: set[str] = set()
    for case in cand_list:
        content = _norm(case.content_digest)
        attach = {_norm(a) for a in case.attachment_digests if _norm(a)}
        reason = ""
        if case.decided_at <= cutoff:
            reason = "not_later_time"
        elif not _norm(case.entity_ref):
            reason = "entity_ref_missing"
        elif not content:
            reason = "content_digest_missing"
        elif _norm(case.case_ref) in f_cases:
            reason = "leakage_case"
        elif content in f_digests:
            reason = "leakage_duplicate"
        elif _norm(case.template_ref) and _norm(case.template_ref) in f_templates:
            reason = "leakage_template"
        elif attach & f_attach:
            reason = "leakage_attachment"
        elif content in seen_digest:
            reason = "duplicate_in_cohort"
        if reason:
            excluded.append((case.case_ref, reason))
            continue
        seen_digest.add(content)
        matures_at = case.decided_at + maturity_window
        if matures_at > now:
            immature.append((case.case_ref, matures_at))
        elif _norm(case.entity_ref) in f_entities:
            repeat.append(case.case_ref)
        else:
            independent.append(case.case_ref)
    held = frozenset(_norm(c.case_ref) for c in cand_list if c.decided_at > cutoff)
    body = [
        COHORT_SCHEMA,
        cutoff,
        maturity_window,
        now,
        independent,
        repeat,
        [list(x) for x in immature],
        [list(x) for x in excluded],
        sorted(held),
        sorted(f_cases),
    ]
    return ConfirmationCohort(
        cutoff=cutoff,
        maturity_window=maturity_window,
        now=now,
        independent=tuple(independent),
        repeat_entity=tuple(repeat),
        immature=tuple(immature),
        excluded=tuple(excluded),
        held_out=held,
        frame_cases=frozenset(f_cases),
        cohort_digest=digest(body),
    )


def _check_case(case: object) -> CohortCase:
    if type(case) is not CohortCase:
        raise AllocationError("cohort_invalid", "case")
    if not all(
        type(v) is str
        for v in (case.case_ref, case.entity_ref, case.content_digest, case.template_ref)
    ):
        raise AllocationError("cohort_invalid", "identity fields must be str")
    if not _norm(case.case_ref) or not _is_str_tuple(case.attachment_digests):
        raise AllocationError("cohort_invalid", "case_ref or attachment_digests")
    if type(case.decided_at) is not int or case.decided_at < 0:
        raise AllocationError("cohort_invalid", "decided_at")
    return case


def _corroboration(scope: VerificationScope) -> tuple[frozenset[str], Mapping[str, Any], str]:
    """Read OPE-516's own manifest for this scope: (families, sealed cases, manifest digest)."""
    if (
        type(scope) is not VerificationScope
        or type(scope.variants) is not VariantLedger
        or type(scope.seal) is not SplitSeal
        or type(scope.policy_revision) is not str
        or type(scope.grant_ref) is not str
        or scope.mode not in ("preview", "training")
    ):
        raise AllocationError("verification_source_invalid")
    manifest = scope.variants.manifest(
        seal=scope.seal,
        policy_revision=scope.policy_revision,
        grant_ref=scope.grant_ref,
        mode="training" if scope.mode == "training" else "preview",
    )
    families = manifest.get("independent_families")
    if not isinstance(families, list) or not all(type(f) is str for f in families):
        raise AllocationError("verification_source_invalid", "independent_families")
    return frozenset(families), scope.seal.cases, str(manifest["manifest_digest"])


def _classify(
    families: list[FamilyInput],
    corroborated: frozenset[str] | None = None,
    sealed_cases: Mapping[str, Any] | None = None,
    holdout: ConfirmationCohort | None = None,
) -> tuple[dict[str, str], list[FamilyInput]]:
    """Independently validate grouping and eligibility. Returns reasons and eligible families."""
    for fam in families:
        _check_family(fam)
    ids = [f.family_id for f in families]
    if len(set(ids)) != len(ids) or any(not i for i in ids):
        raise AllocationError("duplicate_family_id")
    ordered = sorted(families, key=lambda f: f.family_id)
    owner: dict[str, str] = {}
    conflicted: set[str] = set()
    for fam in ordered:
        for root in fam.roots:
            other = owner.setdefault(_norm(root.root_ref), fam.family_id)
            if other != fam.family_id:
                conflicted.update((other, fam.family_id))
    case_owner: dict[str, str] = {}
    for fam in ordered:
        for root in fam.roots:
            if _norm(root.case_ref):
                other = case_owner.setdefault(_norm(root.case_ref), fam.family_id)
                if other != fam.family_id:
                    conflicted.update((other, fam.family_id))
    reasons: dict[str, str] = {}
    seen_digest: set[str] = set()
    for fam in ordered:
        reason = ""
        if fam.family_id in conflicted:
            reason = "family_identity_conflict"
        elif not fam.roots:
            reason = "no_roots"
        elif any(not _norm(r.case_ref) for r in fam.roots):
            reason = "case_ref_missing"
        elif len({_norm(r.root_ref) for r in fam.roots}) != len(fam.roots):
            reason = "duplicate_root"
        elif holdout is not None and any(_norm(r.case_ref) in holdout.held_out for r in fam.roots):
            reason = "confirmation_holdout"
        elif holdout is not None and any(
            _norm(r.case_ref) not in holdout.frame_cases for r in fam.roots
        ):
            reason = "outside_confirmation_frame"
        elif fam.protected_derived:
            reason = "protected_derived"
        elif fam.split == "protected":
            reason = "protected_split"
        elif fam.split != TRAINING:
            reason = "not_training_split"
        elif any(
            _norm(r.root_ref).startswith(SYNTHETIC_PREFIX)
            or _norm(r.content_digest).startswith(SYNTHETIC_PREFIX)
            for r in fam.roots
        ):
            reason = "child_derived"
        elif fam.verification == "retracted":
            reason = "retracted"
        elif fam.verification == "disputed":
            reason = "disputed"
        elif fam.verification != VERIFIED:
            reason = "unverified"
        elif not fam.verifier_independent:
            reason = "verifier_not_independent"
        elif corroborated is not None and (
            fam.family_id not in corroborated
            or any(
                tuple((sealed_cases or {}).get(r.case_ref, ())) != (fam.family_id,)
                for r in fam.roots
            )
        ):
            reason = "verification_uncorroborated"
        elif fam.policy_conflict:
            reason = "policy_conflict"
        if not reason:
            digests = {_norm(r.content_digest) for r in fam.roots}
            if digests & seen_digest:
                reason = "duplicate_family"
            else:
                seen_digest |= digests
        reasons[fam.family_id] = reason or "eligible"
    return reasons, [f for f in ordered if reasons[f.family_id] == "eligible"]


def _pairs(fam: FamilyInput, policy: AllocationPolicy, seed: str) -> list[tuple[str, str]]:
    """Remaining (root, recipe) slots for a family, in stable seeded order, within quotas."""
    pairs: list[tuple[str, str]] = []
    family_used = sum(r.admitted_children for r in fam.roots)
    for root in fam.roots:
        room = max(0, policy.per_root_cap - root.admitted_children)
        recipes = sorted(set(root.uncovered_recipes))
        pairs.extend((root.root_ref, rid) for rid in recipes[:room])
    pairs.sort(key=lambda p: _hash(seed, fam.family_id, p[0], p[1]))
    return pairs[: max(0, policy.per_family_cap - family_used)]


def _input_digest(families: list[FamilyInput]) -> str:
    return digest(
        [
            [
                f.family_id,
                list(f.cell),
                f.split,
                f.verification,
                f.verifier_independent,
                f.policy_conflict,
                f.protected_derived,
                f.model_revision,
                sorted(
                    [
                        r.root_ref,
                        r.content_digest,
                        r.case_ref,
                        sorted(r.uncovered_recipes),
                        r.admitted_children,
                    ]
                    for r in f.roots
                ),
            ]
            for f in sorted(families, key=lambda f: f.family_id)
        ]
    )


def _policy_digest(policy: AllocationPolicy) -> str:
    return digest(
        [
            PROFILE_SCHEMA,
            policy.profile_revision,
            policy.seed,
            policy.total_units,
            policy.spend_ceiling,
            policy.unit_cost,
            policy.cost_basis,
            policy.per_family_cap,
            policy.per_root_cap,
            policy.batch_size,
            policy.exploration_quota,
            policy.min_support,
            [[list(c), n] for c, n in sorted(policy.critical_cells)],
            policy.min_yield_milli,
            policy.observed_yield_milli,
            policy.revision_min_support,
        ]
    )


def _derive_allocation_id(
    *,
    input_digest: str,
    policy_digest: str,
    control: bool,
    match_units: int | None,
    verification: tuple[str, str, str],
    holdout_ref: str,
    lines: Iterable[Line],
) -> str:
    """The allocation id commits to the plan's inputs, profile, verification basis and lines."""
    return digest(
        [
            "synthetic-allocation.v2",
            input_digest,
            policy_digest,
            control,
            match_units,
            list(verification),
            holdout_ref,
            _line_rows(lines),
        ]
    )


def plan_allocation_id(plan: AllocationPlan) -> str:
    """Recompute the id a plan's own content implies (``reserve`` refuses any other id)."""
    return _derive_allocation_id(
        input_digest=plan.input_digest,
        policy_digest=plan.policy_digest,
        control=plan.control,
        match_units=plan.match_units,
        verification=(plan.verification_basis, plan.verification_grant_ref, plan.verification_ref),
        holdout_ref=plan.holdout_ref,
        lines=plan.lines,
    )


def _frac(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _revision_split(fams: list[FamilyInput], minimum: int) -> str:
    """Subdivide a cell by model revision only when every revision meets the owner's minimum."""
    if minimum == 0:
        return "disabled"
    revisions = [_norm(f.model_revision) for f in fams]
    if any(not r for r in revisions):
        return "revision_missing"
    counts = Counter(revisions)
    if len(counts) < 2:
        return "single_revision"
    if min(counts.values()) < minimum:
        return "support_below_minimum"
    return "subdivided"


def _scores(
    strata: dict[Stratum, list[FamilyInput]],
    queues: Mapping[str, list[tuple[str, str]]],
    policy: AllocationPolicy,
) -> dict[Stratum, Fraction]:
    """Score = independent verified residual families x uncovered transformations / unit cost."""
    out: dict[Stratum, Fraction] = {}
    for stratum, fams in strata.items():
        recipes = {p[1] for f in fams for p in queues[f.family_id]}
        out[stratum] = Fraction(len(fams) * len(recipes), policy.unit_cost)
    return out


def allocate(
    families: Iterable[FamilyInput],
    policy: AllocationPolicy,
    *,
    control: bool = False,
    match_units: int | None = None,
    verified_by: VerificationScope | None = None,
    holdout: ConfirmationCohort | None = None,
) -> AllocationPlan:
    """Plan an allocation. ``control=True`` is the seeded uniform comparator over the same roots.

    ``verified_by`` makes OPE-516's ledger the verification authority; without it the plan is
    labelled caller-asserted. ``holdout`` excludes every later-time confirmation case and every
    root outside the frame the cohort's leakage checks used.
    """
    policy.validate()
    if type(control) is not bool or not (
        match_units is None or (type(match_units) is int and match_units >= 0)
    ):
        raise AllocationError("input_invalid", "control or match_units")
    if holdout is not None and type(holdout) is not ConfirmationCohort:
        raise AllocationError("input_invalid", "holdout")
    fams = sorted(families, key=lambda f: str(getattr(f, "family_id", "")))
    corroborated: frozenset[str] | None = None
    sealed_cases: Mapping[str, Any] | None = None
    verification = (CALLER_ASSERTED, "", "")
    if verified_by is not None:
        corroborated, sealed_cases, manifest_digest = _corroboration(verified_by)
        verification = (WORKER_RECORD, verified_by.grant_ref, manifest_digest)
    reasons, eligible = _classify(fams, corroborated, sealed_cases, holdout)
    in_digest = _input_digest(fams)
    pol_digest = _policy_digest(policy)
    holdout_ref = holdout.cohort_digest if holdout is not None else ""
    requests: list[Json] = [
        {"kind": "owner_decision", "family_id": f.family_id}
        for f in fams
        if reasons[f.family_id] == "policy_conflict"
    ]
    budget = min(policy.total_units, policy.spend_ceiling // policy.unit_cost)
    basis = "cost_based" if policy.cost_basis == "measured" else "count_based"
    seed = policy.seed

    def plan_id(lines: Iterable[Line]) -> str:
        return _derive_allocation_id(
            input_digest=in_digest,
            policy_digest=pol_digest,
            control=control,
            match_units=match_units,
            verification=verification,
            holdout_ref=holdout_ref,
            lines=lines,
        )

    def decisions_for(units: Mapping[str, int]) -> tuple[FamilyDecision, ...]:
        return tuple(
            FamilyDecision(
                f.family_id,
                f.cell,
                reasons[f.family_id] == "eligible",
                reasons[f.family_id],
                units.get(f.family_id, 0),
            )
            for f in fams
        )

    def hold(reason: str, **detail: Any) -> AllocationPlan:
        return AllocationPlan(
            allocation_id=plan_id(()),
            status="hold",
            hold_reason=reason,
            lines=(),
            decisions=decisions_for({}),
            requests=tuple(requests),
            cell_scores=(),
            ranking_basis=basis,
            stop_reasons=(reason,),
            policy_digest=pol_digest,
            input_digest=in_digest,
            control=control,
            detail=dict(detail),
            match_units=match_units,
            verification_basis=verification[0],
            verification_grant_ref=verification[1],
            verification_ref=verification[2],
            holdout_ref=holdout_ref,
        )

    if budget <= 0:
        return hold("zero_budget")
    if (
        policy.min_yield_milli is not None
        and policy.observed_yield_milli is not None
        and policy.observed_yield_milli < policy.min_yield_milli
    ):
        return hold("poor_validation_yield")
    if not eligible:
        return hold("no_eligible_families")

    cells: dict[Cell, list[FamilyInput]] = {}
    for fam in eligible:
        cells.setdefault(fam.cell, []).append(fam)
    for cell in sorted(cells):
        if len(cells[cell]) < policy.min_support:
            for fam in cells[cell]:
                reasons[fam.family_id] = "below_min_support"
            requests.append(
                {"kind": "real_label_request", "cell": list(cell), "support": len(cells[cell])}
            )
            del cells[cell]
    if not cells:
        return hold("insufficient_support")
    eligible = [f for f in eligible if reasons[f.family_id] == "eligible"]

    stratum_of: dict[str, Stratum] = {}
    revision_split: list[list[Any]] = []
    for cell in sorted(cells):
        split = _revision_split(cells[cell], policy.revision_min_support)
        revision_split.append([list(cell), split])
        for fam in cells[cell]:
            stratum_of[fam.family_id] = (
                (*cell, _norm(fam.model_revision)) if split == "subdivided" else cell
            )
    strata: dict[Stratum, list[FamilyInput]] = {}
    for fam in eligible:
        strata.setdefault(stratum_of[fam.family_id], []).append(fam)

    queues = {f.family_id: _pairs(f, policy, seed) for f in eligible}
    by_id = {f.family_id: f for f in eligible}
    if sum(len(q) for q in queues.values()) == 0:
        return hold("no_uncovered_transformations")
    scores = _scores(strata, queues, policy)

    minima = dict(policy.critical_cells)
    for cell, minimum in sorted(minima.items()):
        capacity = sum(len(queues[f.family_id]) for f in cells.get(cell, []))
        if capacity < minimum:
            return hold("mandatory_slice_unfit", cell=list(cell), needed=minimum, capacity=capacity)
    reserved = sum(minima.values()) + policy.exploration_quota
    if not control and reserved > budget:
        return hold("constraints_conflict", reserved=reserved, budget=budget)

    limit = budget if match_units is None else min(budget, match_units)
    lines: list[Line] = []
    per_family: dict[str, int] = {}

    def take(fam_id: str, phase: str) -> bool:
        if len(lines) >= limit or not queues[fam_id]:
            return False
        root, recipe = queues[fam_id].pop(0)
        lines.append(
            Line(
                fam_id,
                root,
                recipe,
                by_id[fam_id].cell,
                phase,
                policy.unit_cost,
                stratum_of[fam_id],
            )
        )
        per_family[fam_id] = per_family.get(fam_id, 0) + 1
        return True

    def order(group: list[FamilyInput], tag: str) -> list[str]:
        return [f.family_id for f in sorted(group, key=lambda f: _hash(seed, tag, f.family_id))]

    if control:
        progress = True
        while len(lines) < limit and progress:
            progress = False
            for fid in order(eligible, "uniform"):
                progress = take(fid, "uniform_control") or progress
    else:
        for cell, minimum in sorted(minima.items()):
            got = 0
            moved = True
            while got < minimum and moved:
                moved = False
                for fid in order(cells[cell], "minimum"):
                    if got < minimum and take(fid, "minimum"):
                        got += 1
                        moved = True
        explored = 0
        moved = True
        while explored < policy.exploration_quota and moved:
            moved = False
            for fid in order(eligible, "exploration"):
                if explored < policy.exploration_quota and take(fid, "exploration"):
                    explored += 1
                    moved = True
        ranked = sorted(strata, key=lambda s: (-scores[s], _hash(seed, "cell", *s)))
        progress = True
        while len(lines) < limit and progress:
            progress = False
            for stratum in ranked:
                quota = policy.batch_size
                for fid in order(strata[stratum], "ranked"):
                    if quota > 0 and take(fid, "ranked"):
                        quota -= 1
                        progress = True
    if not lines:
        return hold("no_uncovered_transformations")
    stop = "budget_exhausted" if len(lines) >= limit else "capacity_exhausted"
    return AllocationPlan(
        allocation_id=plan_id(lines),
        status="allocated",
        hold_reason="",
        lines=tuple(lines),
        decisions=decisions_for(per_family),
        requests=tuple(requests),
        cell_scores=tuple((s, _frac(scores[s])) for s in sorted(scores)),
        ranking_basis=basis,
        stop_reasons=(stop,),
        policy_digest=pol_digest,
        input_digest=in_digest,
        control=control,
        detail={
            "budget_units": budget,
            "eligible_families": len(eligible),
            "revision_split": revision_split,
        },
        match_units=match_units,
        verification_basis=verification[0],
        verification_grant_ref=verification[1],
        verification_ref=verification[2],
        holdout_ref=holdout_ref,
    )


def grant_headroom(grant: Grant, variants: VariantLedger, *, now: int) -> int:
    """Spend OPE-516's grant still authorizes: ``Grant.budget - VariantLedger.spent``.

    An unauthorized, expired or location-unapproved grant authorizes nothing (mirrors OPE-516's
    own seed gate). Read-only: this only calls OPE-516's public ``spent``.
    """
    if type(grant) is not Grant or type(variants) is not VariantLedger:
        raise AllocationError("grant_link_invalid")
    if type(now) is not int or type(grant.budget) is not int or type(grant.expires_at) is not int:
        raise AllocationError("grant_link_invalid", "budget, expires_at and now must be int")
    if (
        not (
            grant.generation_authorized is True
            and grant.training_authorized is True
            and grant.execution_location_approved is True
        )
        or now >= grant.expires_at
    ):
        return 0
    return max(0, grant.budget - variants.spent(grant.grant_ref))


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS budget (
        budget_ref TEXT PRIMARY KEY, cap_units INTEGER NOT NULL, cap_spend INTEGER NOT NULL,
        unit_cost INTEGER NOT NULL, grant_ref TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS allocation (
        allocation_id TEXT NOT NULL, budget_ref TEXT NOT NULL, state TEXT NOT NULL,
        units INTEGER NOT NULL, spend INTEGER NOT NULL, record TEXT NOT NULL,
        PRIMARY KEY (budget_ref, allocation_id))""",
    """CREATE TABLE IF NOT EXISTS line (
        budget_ref TEXT NOT NULL, root_ref TEXT NOT NULL, recipe_id TEXT NOT NULL,
        family_id TEXT NOT NULL, allocation_id TEXT NOT NULL, cost INTEGER NOT NULL,
        state TEXT NOT NULL, PRIMARY KEY (root_ref, recipe_id))""",
    """CREATE TABLE IF NOT EXISTS retracted (
        family_id TEXT PRIMARY KEY, reason TEXT NOT NULL)""",
)
# Line states. reserved and committed count against the cap. A retraction frees an unspent
# reservation (invalidated) but keeps committed spend counted (invalidated_spent): spend is
# never refunded. released frees the cap (discarded unfinished selection).
_COUNTED = ("reserved", "committed", "invalidated_spent")


def _positive_ints(*values: object) -> bool:
    return all(type(v) is int and v >= 0 for v in values)


class AllocationLedger:
    """Atomic, replay-safe budget ledger. ``reserve`` is BEGIN IMMEDIATE, recount, insert."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._shared: sqlite3.Connection | None = None
        if self._path == ":memory:":
            self._shared = sqlite3.connect(":memory:", isolation_level=None)
        with self._tx(begin=False) as db:
            for stmt in _SCHEMA:
                db.execute(stmt)
            columns = {row[1] for row in db.execute("PRAGMA table_info(budget)")}
        if not {"unit_cost", "grant_ref"} <= columns:
            # A ledger from an earlier revision has unbound budgets; it is not migrated.
            raise AllocationError("ledger_schema_outdated", self._path)

    @contextmanager
    def _tx(self, *, begin: bool = True) -> Iterator[sqlite3.Connection]:
        db = self._shared or sqlite3.connect(self._path, isolation_level=None, timeout=30)
        try:
            if begin:
                db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                if begin:
                    db.execute("ROLLBACK")
                raise
            if begin:
                db.execute("COMMIT")
        finally:
            if self._shared is None:
                db.close()

    def open_budget(
        self, budget_ref: str, *, cap_units: int, cap_spend: int, unit_cost: int
    ) -> None:
        """Open a budget bound to the run profile's ``AllocationPolicy.unit_cost``. Immutable."""
        self._open(budget_ref, cap_units, cap_spend, unit_cost, "")

    def open_budget_for_grant(
        self,
        budget_ref: str,
        *,
        grant: Grant,
        variants: VariantLedger,
        now: int,
        cap_units: int,
        unit_cost: int,
        cap_spend: int | None = None,
    ) -> Json:
        """Open a budget whose spend cap cannot exceed the OPE-516 grant's current headroom.

        The cap is ``min(cap_spend, grant_headroom)`` at open time. Reopening the same budget
        for the same grant, unit cost and unit cap is idempotent and keeps the stored cap.
        Plans reserved against it must be verified from that grant's OPE-516 record.
        """
        if type(grant) is not Grant or type(grant.grant_ref) is not str or not grant.grant_ref:
            raise AllocationError("grant_link_invalid")
        headroom = grant_headroom(grant, variants, now=now)
        if cap_spend is not None and not _positive_ints(cap_spend):
            raise AllocationError("budget_invalid")
        with self._tx() as db:
            row = db.execute(
                "SELECT cap_units, unit_cost, grant_ref FROM budget WHERE budget_ref=?",
                (budget_ref,),
            ).fetchone()
            if row is not None:
                if tuple(row) != (cap_units, unit_cost, grant.grant_ref):
                    raise AllocationError("budget_conflict", budget_ref)
                return self._status(db, budget_ref)
        effective = headroom if cap_spend is None else min(cap_spend, headroom)
        if effective <= 0:
            raise AllocationError("grant_unavailable", grant.grant_ref)
        self._open(budget_ref, cap_units, effective, unit_cost, grant.grant_ref)
        return self.status(budget_ref)

    def _open(
        self, budget_ref: str, cap_units: int, cap_spend: int, unit_cost: int, grant_ref: str
    ) -> None:
        if type(budget_ref) is not str or not budget_ref:
            raise AllocationError("budget_invalid", "budget_ref")
        if not _positive_ints(cap_units, cap_spend, unit_cost) or unit_cost < 1:
            raise AllocationError("budget_invalid")
        with self._tx() as db:
            row = db.execute(
                "SELECT cap_units, cap_spend, unit_cost, grant_ref FROM budget WHERE budget_ref=?",
                (budget_ref,),
            ).fetchone()
            if row is None:
                db.execute(
                    "INSERT INTO budget VALUES (?,?,?,?,?)",
                    (budget_ref, cap_units, cap_spend, unit_cost, grant_ref),
                )
            elif tuple(row) != (cap_units, cap_spend, unit_cost, grant_ref):
                raise AllocationError("budget_conflict", budget_ref)

    @staticmethod
    def _used(db: sqlite3.Connection, budget_ref: str) -> tuple[int, int]:
        marks = ",".join("?" for _ in _COUNTED)
        row = db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(cost),0) FROM line "  # noqa: S608
            f"WHERE budget_ref=? AND state IN ({marks})",
            (budget_ref, *_COUNTED),
        ).fetchone()
        return int(row[0]), int(row[1])

    @staticmethod
    def _state(db: sqlite3.Connection, budget_ref: str, allocation_id: str) -> str:
        row = db.execute(
            "SELECT state FROM allocation WHERE allocation_id=? AND budget_ref=?",
            (allocation_id, budget_ref),
        ).fetchone()
        if row is None:
            raise AllocationError("allocation_unknown", allocation_id)
        return str(row[0])

    def reserve(
        self, budget_ref: str, plan: AllocationPlan, *, _fail_after: int | None = None
    ) -> Json:
        """Atomically reserve a whole plan or refuse it. Replaying the same plan is a no-op.

        The charge is re-derived as ``units x the budget's bound unit_cost``. A line priced at
        anything else, an ``allocation_id`` its content does not imply, or a replay whose
        content differs from the stored plan is refused as ``plan_binding_mismatch``.
        ``_fail_after`` is a test hook that raises mid-insert to prove rollback.
        """
        if plan.status != "allocated" or not plan.lines:
            raise AllocationError("allocation_held", plan.hold_reason)
        if plan.control:
            raise AllocationError("control_not_reservable")
        keys = {(x.root_ref, x.recipe_id) for x in plan.lines}
        if len(keys) != len(plan.lines) or any(
            type(x.cost) is not int or x.cost < 1 for x in plan.lines
        ):
            raise AllocationError("plan_invalid", "lines must be unique with positive cost")
        if plan_allocation_id(plan) != plan.allocation_id:
            raise AllocationError(BINDING_MISMATCH, "allocation_id")
        record = canonical(plan.as_record())
        with self._tx() as db:
            cap = db.execute(
                "SELECT cap_units, cap_spend, unit_cost, grant_ref FROM budget WHERE budget_ref=?",
                (budget_ref,),
            ).fetchone()
            if cap is None:
                raise AllocationError("budget_unknown", budget_ref)
            cap_units, cap_spend, unit_cost, grant_ref = cap
            if any(x.cost != unit_cost for x in plan.lines):
                raise AllocationError(BINDING_MISMATCH, f"unit_cost {unit_cost}")
            if grant_ref and (
                plan.verification_basis != WORKER_RECORD or plan.verification_grant_ref != grant_ref
            ):
                raise AllocationError("verification_uncorroborated", grant_ref)
            prior = db.execute(
                "SELECT state, record FROM allocation WHERE allocation_id=? AND budget_ref=?",
                (plan.allocation_id, budget_ref),
            ).fetchone()
            if prior is not None and prior[0] != "released":
                if prior[1] != record:
                    raise AllocationError(BINDING_MISMATCH, "replayed plan differs")
                return {"replayed": True, "state": prior[0], **self._status(db, budget_ref)}
            retracted = {r[0] for r in db.execute("SELECT family_id FROM retracted")}
            if any(line.family_id in retracted for line in plan.lines):
                raise AllocationError("family_retracted")
            charge = len(plan.lines) * unit_cost
            used_units, used_spend = self._used(db, budget_ref)
            if used_units + len(plan.lines) > cap_units or used_spend + charge > cap_spend:
                raise AllocationError(
                    "budget_exceeded", f"{used_units}+{len(plan.lines)}>{cap_units}"
                )
            db.execute(
                "INSERT OR REPLACE INTO allocation VALUES (?,?,?,?,?,?)",
                (plan.allocation_id, budget_ref, "reserved", len(plan.lines), charge, record),
            )
            db.execute("DELETE FROM line WHERE state='released'")
            for index, line in enumerate(plan.lines):
                if _fail_after is not None and index == _fail_after:
                    raise RuntimeError("simulated crash")
                try:
                    db.execute(
                        "INSERT INTO line VALUES (?,?,?,?,?,?,?)",
                        (
                            budget_ref,
                            line.root_ref,
                            line.recipe_id,
                            line.family_id,
                            plan.allocation_id,
                            unit_cost,
                            "reserved",
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AllocationError("line_already_allocated", line.root_ref) from exc
            return {"replayed": False, "state": "reserved", **self._status(db, budget_ref)}

    def commit(self, budget_ref: str, allocation_id: str) -> Json:
        with self._tx() as db:
            state = self._state(db, budget_ref, allocation_id)
            if state == "reserved":
                db.execute(
                    "UPDATE allocation SET state='committed' "
                    "WHERE allocation_id=? AND budget_ref=?",
                    (allocation_id, budget_ref),
                )
                db.execute(
                    "UPDATE line SET state='committed' "
                    "WHERE allocation_id=? AND budget_ref=? AND state='reserved'",
                    (allocation_id, budget_ref),
                )
            elif state not in ("committed", "invalidated"):
                raise AllocationError("allocation_not_commitable", state)
            return self._status(db, budget_ref)

    def release(self, budget_ref: str, allocation_id: str) -> Json:
        """Discard an unfinished (reserved) selection; a committed one cannot be released."""
        with self._tx() as db:
            state = self._state(db, budget_ref, allocation_id)
            if state == "committed":
                raise AllocationError("allocation_committed")
            if state == "reserved":
                db.execute(
                    "UPDATE allocation SET state='released' WHERE allocation_id=? AND budget_ref=?",
                    (allocation_id, budget_ref),
                )
                db.execute(
                    "UPDATE line SET state='released' WHERE allocation_id=? AND budget_ref=?",
                    (allocation_id, budget_ref),
                )
            return self._status(db, budget_ref)

    def retract_family(self, budget_ref: str, family_id: str, reason: str) -> Json:
        """Invalidate a retracted family's lines in every budget; spend is never refunded."""
        with self._tx() as db:
            db.execute("INSERT OR IGNORE INTO retracted VALUES (?,?)", (family_id, reason))
            db.execute(
                "UPDATE line SET state='invalidated_spent' WHERE family_id=? AND state='committed'",
                (family_id,),
            )
            db.execute(
                "UPDATE line SET state='invalidated' WHERE family_id=? AND state='reserved'",
                (family_id,),
            )
            return self._status(db, budget_ref)

    def live_lines(self, budget_ref: str) -> list[tuple[str, str, str]]:
        """Lines usable downstream: reserved or committed, never invalidated or released."""
        with self._tx(begin=False) as db:
            rows = db.execute(
                "SELECT family_id, root_ref, recipe_id FROM line WHERE budget_ref=? "
                "AND state IN ('reserved','committed') ORDER BY family_id, root_ref, recipe_id",
                (budget_ref,),
            ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def status(self, budget_ref: str) -> Json:
        with self._tx(begin=False) as db:
            return self._status(db, budget_ref)

    def _status(self, db: sqlite3.Connection, budget_ref: str) -> Json:
        cap = db.execute(
            "SELECT cap_units, cap_spend, unit_cost, grant_ref FROM budget WHERE budget_ref=?",
            (budget_ref,),
        ).fetchone()
        if cap is None:
            raise AllocationError("budget_unknown", budget_ref)
        units, spend = self._used(db, budget_ref)
        return {
            "cap_units": cap[0],
            "cap_spend": cap[1],
            "unit_cost": cap[2],
            "grant_ref": cap[3],
            "used_units": units,
            "used_spend": spend,
            "remaining_units": cap[0] - units,
            "remaining_spend": cap[1] - spend,
        }
