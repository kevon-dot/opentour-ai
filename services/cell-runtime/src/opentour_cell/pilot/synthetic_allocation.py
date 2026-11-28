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
  ``allocation_id`` from its whole record (every field that is stored or read) and requires a
  replay to match the stored plan exactly; any mismatch is refused as ``plan_binding_mismatch``.
  Every count, cap and cost is bounded to the sqlite int64 range and refused cleanly beyond it.
* Only families that are verified by an independent verifier, in the real training partition,
  not disputed, not retracted, not protected-derived, not child-derived, not held out for
  confirmation and not in a policy conflict can receive allocation. Near-duplicate families
  collapse to one.
* A family is one independent unit however many roots, copies or children it has.
* Zero eligible families, zero budget, poor validation yield or an unfittable mandatory slice
  return an explicit hold rather than an empty success.
* Default spend is zero: an unset budget holds.

Two kinds of budget (OPE-520 owner decision: caller-asserted allocations are planning records
only; spend goes through the OPE-516 grant-bound budget). **No line on either kind is authorized
spend yet**: OPE-516's ``Grant`` has no worker-held record, so a grant-bound budget trusts the
first grant presented to its own ledger file, which anyone can make. Until a worker-held grant
record exists (OPE-516 / owner decision), grant-bound lines are labelled
``ope516_grant_unverified`` with ``grant_basis="first_presentation_unverified"``, and
``status()`` reports ``authorized_spend=False`` for every budget.

* ``open_budget`` opens a *planning* budget. It accepts caller-asserted plans, but
  ``status()`` reports ``authority="planning_record_only"`` and every ``live_lines()`` row
  carries ``authority="planning_record"``, an empty ``grant_ref`` and the plan's own unchecked
  ``verification_basis`` label, so no consumer can mistake it for authorized spend.
* ``open_budget_for_grant`` opens a *grant-bound* budget (``authority="ope516_grant_unverified"``,
  ``grant_basis="first_presentation_unverified"`` in ``status()``, every ``LiveLine`` and every
  ``reserve`` result). It binds
  to one file-backed OPE-516 ``VariantLedger``: the resolved real path plus device and inode of
  the sqlite file and of its directory, with private-custody checks modelled on
  ``LearningMandates`` (regular file, not a symlink, one link, owned by this uid, not group- or
  world-writable). ``reserve`` on it needs a ``VerificationScope`` and ``now`` and trusts no plan
  field: it re-reads OPE-516 itself and refuses another or copied ledger
  (``ledger_identity_mismatch``), a seal whose digest does not recompute, a manifest digest
  that is not OPE-516's current one (``verification_stale``), preview mode, and any line whose
  root is not a live OPE-516 root of that family, in that scope, with its stored case sealed
  under that family alone. ``commit`` needs ``now`` and re-derives the same root liveness,
  expiry (``grant_expired``) and grant headroom (``grant_headroom_exceeded``: pinned budget minus
  current OPE-516 spend minus every live reservation on the grant, this one included). A commit
  therefore belongs *before* its lines are generated through OPE-516.
* Both kinds bind the run profile's ``unit_cost`` and ``cost_basis``; ``reserve`` re-derives
  ``ranking_basis`` from the bound ``cost_basis`` (``fixed_unit`` forces ``count_based``) and
  requires ``requests`` to be exactly what the plan's own decisions imply. The decisions
  themselves are not re-derivable at reserve (that needs the caller's families).
* Line uniqueness is per authority class (``planning_record`` vs ``ope516_grant_unverified``):
  a planning reservation never blocks the matching grant-bound line, while one root/recipe
  still cannot be reserved twice within a class.
  Where the OPE-516 ledger pins its worker mandate store (``authority_pin``, PR #225: store
  realpaths, device/inode, organization, engagement, scope and the ledger file's identity), the
  budget also binds to that pin, read-only, from the first open, reserve or commit that sees it;
  a later change or removal is ``ledger_identity_mismatch``. OPE-516's private identity
  helpers are not called, and a ledger without the table binds to file identity only.

Verification basis. ``FamilyInput.verification`` and ``verifier_independent`` are supplied by
the caller. When ``allocate`` is given a ``VerificationScope`` it reads OPE-516's
``VariantLedger.manifest`` and the live roots of that scope itself; a family is eligible only if
OPE-516 holds a live, training-sealed root for it, every one of its roots is such a root of
that family, each root's case ref is the one OPE-516 stored and is sealed under that family
alone; the caller's flags can then only narrow eligibility, never widen it. The plan records the
basis, grant, manifest digest, mode and a scope digest (ledger identity, seal, policy revision,
grant, mode). Without a scope the plan is labelled ``verification_basis="caller_asserted"`` and
a grant-bound budget refuses it. OPE-516's own record of verification comes from its
``SeedRoot`` flags and is unsigned; this is a worker record, not proof. ``VariantLedger`` has no
public root listing, so live roots are read read-only from its ``roots``/``root_scopes`` tables
with the same scope query ``VariantLedger.manifest`` uses, and must agree with the manifest's
``independent_families`` (otherwise ``verification_source_invalid``).

OPE-516 link (read-only import of ``training_variants``; nothing here writes OPE-516 state).

* Spend cap of a grant-bound budget: ``min(cap_spend, Grant.budget - VariantLedger.spent - live
  spend on every budget of that grant in this allocation ledger)``, at open and again at every
  ``reserve`` with the caller's ``now`` (expiry and authorization flags re-checked). Live spend
  is reserved, committed and spent-invalidated lines.
* Reservations here do **not** reduce OPE-516's headroom: OPE-516's ``submit`` checks only its
  own children's cost against the ``Grant`` it is handed, so it can still accept spend this
  ledger has reserved. A line later generated through OPE-516 is then counted twice here (once
  as a live line, once in ``VariantLedger.spent``), which is conservative, never permissive.
  Closing this needs an OPE-516 change (it must read live reservations, and a generated child
  must name the line it fulfils); this module does not edit OPE-516.
* The ``Grant`` is caller-asserted in OPE-516 too: no worker record of the generation grant
  exists, and ``LearningMandates`` carries no synthetic-generation budget. This ledger pins the
  first ``Grant`` presented per ``grant_ref`` (with the bound ledger identity) and refuses any
  other (``grant_conflict``); it cannot verify the first one. That pin is per allocation-ledger
  file, so a new file accepts any first grant (an inflated budget, a new ``grant_ref``): hence
  the ``_unverified`` labels above.
* Time authority (as OPE-544): grant expiry is judged on ``max(_worker_now(), caller now)``.
  ``_worker_now`` is the worker's own clock in integer Unix seconds, the unit
  ``LearningMandates`` uses for ``expires_at``. OPE-516 defines no unit; if ``expires_at`` were in
  milliseconds the check would fail open, so the assumption is load-bearing. A caller ``now``
  more than ``CLOCK_TOLERANCE`` (300 s) ahead of the worker clock is refused
  (``clock_skew_refused``) and never stored, so a caller can neither extend a grant nor end it
  early by more than that. A per-grant high-water mark (digest-covered, one row per pinned
  grant) is written only inside an operation that succeeds, so a refused open, reserve or
  commit never moves it; it detects a worker clock that stepped back (``clock_regressed``).
* A committed line whose generated child OPE-516 has charged is not counted again, up to the
  charged cost: children for the same ``(grant_ref, root_ref, recipe_id)`` that did not exist at
  its commit reduce its counted cost by what they were charged, never below zero. Children do
  not name their line, so that triple is the only link: an unrelated post-commit child of that
  root and recipe also frees the line's committed cost (OPE-516's own admission check still caps
  real spend). The OPE-516 change above, a child naming its line, would make it exact. Reserved
  lines always count, so a reservation generated *before* its commit is still counted twice
  and the commit is refused: commit first, then generate.
* Inherent race until OPE-516 sees reservations: OPE-516 can admit a child between a
  ``reserve``/``commit``'s final read of its spend and the end of that transaction (for example
  8 committed plus 9 newly spent against 10). Nothing in this module can prevent that.
* Separate allocation-ledger files on one grant cannot see each other's reservations.
* The two modules' cost units are assumed equal ("use the same cost unit across the run") and no
  contract defines them. ``covered_from_children`` reads OPE-516 children (disposition
  ``admitted`` only, never one marked ``fit_stale``); OPE-516's ``independent_family_count`` is
  not consumed. Nothing here depends on ``LearningMandates`` reservation lifecycle state or on a
  reservation's residual family set (neither is readable; #225 gaps G1/G2).

Integrity limits (no key custody exists). Plan ids, budget and grant row digests and the cohort
digest are plain SHA-256 over content: self-consistent only, unsigned. A process running as the
same uid can edit either sqlite file and recompute them. What is detected: a one-column edit
(caps, unit cost, ``grant_ref``), downgrading a grant-bound budget to a planning budget,
schema drift (exact column sets), a forged seal or cohort whose digest does not recompute, and a
hand-built cohort with nothing held out. Caps of a grant-bound budget are re-checked against the
pinned grant and live OPE-516 spend at every reserve, so a recomputed cap edit still cannot
exceed the grant's headroom; the pinned grant itself lives in this file.

Simulated, not proven: every family, verification, cost and yield value is a generated
sentinel input supplied by the caller. Tests prove software behavior only. Nothing here is
provider, customer-evidence, fit-qualification or ROI evidence; with a fixed unit cost the
ranking is count-based and ``roi_claim`` is always false (``reserve`` refuses a plan claiming
otherwise). Plans carry no signature: a caller can still build a self-consistent plan, but it is
charged at the bound unit cost and, on a grant-bound budget, re-derived from OPE-516.

Leakage exclusion in ``confirmation_cohort`` compares identities after NFKC, case, width,
whitespace and invisible-character normalisation and refuses any identity that is not ASCII
after it (the repo has no confusable-folding helper, so homoglyphs are refused, not folded). It
does not detect near-duplicate content under different digests, nor two later-time cases that
share a template or attachment absent from the frame; the cohort is caller-built and its digest
is self-consistent only. A hand-built cohort holding out one dummy ref with a recomputed digest
is accepted: no ledger here records later-time cases (OPE-516 holds only registered training
roots), so there is nothing to check held-out refs against.

Not implemented here (recorded gaps): the confirmation and repeat-entity *estimates* (this
module partitions the cohort; the metric needs model outcomes and belongs to OPE-517);
sample inclusion probabilities (no sampling design is specified); routing of owner-decision
and real-label requests (emitted as records only).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import stat
import time
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from fractions import Fraction
from pathlib import Path
from typing import Any, NamedTuple

from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training_variants import Grant, SplitSeal, VariantLedger

Json = dict[str, Any]
PLAN_SCHEMA = "opentour.pilot.synthetic-allocation.v1"
PROFILE_SCHEMA = "opentour.pilot.synthetic-allocation-profile.v1"
COHORT_SCHEMA = "opentour.pilot.confirmation-cohort.v1"
LEDGER_SCHEMA = "opentour.pilot.synthetic-allocation-ledger.v5"
SEAL_SCHEMA = "opentour.variants.split-seal.v2"  # OPE-516's own SplitSeal.from_rows digest tag

VERIFIED = "verified"
TRAINING = "training"
SYNTHETIC_PREFIX = "var-"
CALLER_ASSERTED = "caller_asserted"
WORKER_RECORD = "ope516_variant_ledger"
BINDING_MISMATCH = "plan_binding_mismatch"
# A line on a grant-bound budget: re-derived from OPE-516 at reserve and commit, but its grant is
# only the first one presented to this ledger file, so it is not authorized spend either.
GRANT_AUTHORITY = "ope516_grant_unverified"
PLANNING_RECORD = "planning_record"  # a line on a planning budget: never authorized spend
GRANT_BASIS_UNVERIFIED = "first_presentation_unverified"  # no worker-held grant record exists
NO_GRANT = "none"
RANKING_FOR_COST_BASIS = {"fixed_unit": "count_based", "measured": "cost_based"}
INT64_MAX = 2**63 - 1  # sqlite INTEGER range; larger values are refused, never wrapped
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
        if not _int64(*ints):
            raise AllocationError("policy_invalid", "counts must be int64 non-negative integers")
        if self.unit_cost < 1 or self.batch_size < 1:
            raise AllocationError("policy_invalid", "unit_cost and batch_size must be positive")
        for opt in (self.min_yield_milli, self.observed_yield_milli):
            if opt is not None and not _int64(opt):
                raise AllocationError("policy_invalid", "yield values must be int64 or None")
        if type(self.critical_cells) is not tuple or not all(
            type(item) is tuple
            and len(item) == 2
            and _is_str_tuple(item[0])
            and len(item[0]) == 2
            and _int64(item[1])
            for item in self.critical_cells
        ):
            raise AllocationError("policy_invalid", "critical_cells")
        if self.cost_basis not in ("fixed_unit", "measured"):
            raise AllocationError("policy_invalid", "cost_basis")
        if (
            type(self.profile_revision) is not str
            or type(self.seed) is not str
            or not self.profile_revision
            or not self.seed
        ):
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
    verification_mode: str = ""  # OPE-516 scope mode (training | preview) of the worker record
    verification_scope: str = ""  # digest of ledger identity, seal, policy revision, grant, mode

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
            "verification_mode": self.verification_mode,
            "verification_scope": self.verification_scope,
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
    """Where verification is read from: one scope of one file-backed OPE-516 ledger.

    ``variants`` is the path of the OPE-516 ``VariantLedger`` sqlite file (an in-memory or
    caller-held ledger object cannot be identified, so it is refused). The ledger is opened and
    identified here; the caller never hands over a ledger object.
    """

    variants: str | Path
    seal: SplitSeal
    policy_revision: str
    grant_ref: str
    mode: str = "training"


class LiveLine(NamedTuple):
    """A live allocation line and the authority behind it.

    ``ope516_grant_unverified`` lines sit on a grant-bound budget and were re-derived from
    OPE-516 at reserve, but their grant is unverified (``grant_basis``); ``planning_record``
    lines are planning records only. No line is authorized spend.
    """

    family_id: str
    root_ref: str
    recipe_id: str
    grant_ref: str
    verification_basis: str
    authority: str
    grant_basis: str  # first_presentation_unverified on a grant-bound budget, else none


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


def _int64(*values: object) -> bool:
    """Exact non-negative ``int`` within sqlite's INTEGER range (no bool, no float)."""
    return all(type(v) is int and 0 <= v <= INT64_MAX for v in values)


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

    Held, preview, quarantined, rejected and invalidated children cover nothing, nor does an
    admitted child OPE-516 marked ``fit_stale`` (its fit experiment went stale when a co-member
    root was retracted; OPE-516 excludes it from ``admitted_count`` and weights). A child's own
    family claim is never used to group anything here.
    """
    out: dict[str, dict[str, int]] = {}
    for child in children:
        if child.get("disposition") != "admitted" or child.get("fit_stale"):
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
        if not _int64(value):
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
    out = ConfirmationCohort(
        cutoff=cutoff,
        maturity_window=maturity_window,
        now=now,
        independent=tuple(independent),
        repeat_entity=tuple(repeat),
        immature=tuple(immature),
        excluded=tuple(excluded),
        held_out=held,
        frame_cases=frozenset(f_cases),
        cohort_digest="",
    )
    return replace(out, cohort_digest=_cohort_digest(out))


def _cohort_digest(cohort: ConfirmationCohort) -> str:
    return digest(
        [
            COHORT_SCHEMA,
            cohort.cutoff,
            cohort.maturity_window,
            cohort.now,
            list(cohort.independent),
            list(cohort.repeat_entity),
            [list(x) for x in cohort.immature],
            [list(x) for x in cohort.excluded],
            sorted(cohort.held_out),
            sorted(cohort.frame_cases),
        ]
    )


def _check_holdout(cohort: object) -> ConfirmationCohort:
    """Refuse a hand-built cohort that is not internally consistent.

    The digest must recompute from the cohort's own content, something must be held out, and
    every confirmation, repeat-entity and immature case must be in the held-out set. The digest
    is self-consistent only (unsigned): a caller can still build a consistent cohort.
    """
    if type(cohort) is not ConfirmationCohort:
        raise AllocationError("holdout_invalid", "type")
    if (
        type(cohort.held_out) is not frozenset
        or type(cohort.frame_cases) is not frozenset
        or not all(type(x) is str for x in cohort.held_out | cohort.frame_cases)
        or not _is_str_tuple(cohort.independent)
        or not _is_str_tuple(cohort.repeat_entity)
        or type(cohort.immature) is not tuple
        or not all(
            type(x) is tuple and len(x) == 2 and type(x[0]) is str and _int64(x[1])
            for x in cohort.immature
        )
        or type(cohort.excluded) is not tuple
        or not all(type(x) is tuple and len(x) == 2 and _is_str_tuple(x) for x in cohort.excluded)
        or not _int64(cohort.cutoff, cohort.maturity_window, cohort.now)
        or type(cohort.cohort_digest) is not str
    ):
        raise AllocationError("holdout_invalid", "shape")
    if not cohort.held_out:
        raise AllocationError("holdout_invalid", "nothing_held_out")
    members = [*cohort.independent, *cohort.repeat_entity, *(ref for ref, _ in cohort.immature)]
    if any(_norm(ref) not in cohort.held_out for ref in members):
        raise AllocationError("holdout_invalid", "partition_outside_held_out")
    if _cohort_digest(cohort) != cohort.cohort_digest:
        raise AllocationError("holdout_invalid", "cohort_digest")
    return cohort


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
    identities = (
        case.case_ref,
        case.entity_ref,
        case.content_digest,
        case.template_ref,
        *case.attachment_digests,
    )
    # No confusable-folding helper exists in this repo: a homoglyph is refused, never folded.
    if not all(_norm(v).isascii() for v in identities):
        raise AllocationError("cohort_invalid", "non_ascii_identity")
    if not _int64(case.decided_at):
        raise AllocationError("cohort_invalid", "decided_at")
    return case


def _private(info: Any, uid: int, *, directory: bool) -> bool:
    """Owned by ``uid``, not group/world-writable; a file must be regular with one link."""
    kind = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return bool(
        kind
        and info.st_uid == uid
        and not info.st_mode & 0o022
        and (directory or info.st_nlink == 1)
    )


def _ledger_identity(path: object) -> tuple[str, Json]:
    """Identify one file-backed OPE-516 ledger: real path plus device/inode of file and dir.

    Custody checks follow ``LearningMandates``: the file is a regular, single-link file, not a
    symlink, owned by this uid and not group- or world-writable; so is its directory. A copy has
    another inode, and a ledger object or ``:memory:`` cannot be identified at all.
    """
    if not isinstance(path, str | Path) or str(path) in ("", ":memory:"):
        raise AllocationError("ledger_identity_invalid", "a file-backed OPE-516 ledger path")
    raw = Path(path)
    try:
        link = raw.lstat()
        real = Path(os.path.realpath(raw))
        info = real.stat()
        dir_info = real.parent.stat()
    except OSError as exc:
        raise AllocationError("ledger_identity_invalid", "unreadable") from exc
    uid = os.getuid()
    if (
        stat.S_ISLNK(link.st_mode)
        or not _private(info, uid, directory=False)
        or not _private(dir_info, uid, directory=True)
    ):
        raise AllocationError("ledger_identity_invalid", "custody")
    identity: Json = {
        "path": str(real),
        "dev": info.st_dev,
        "ino": info.st_ino,
        "dir_dev": dir_info.st_dev,
        "dir_ino": dir_info.st_ino,
    }
    return digest(["opentour.pilot.ope516-ledger-identity.v1", identity]), identity


def _check_seal(seal: object) -> SplitSeal:
    """A seal is accepted only if OPE-516's own digest recomputes from its content."""
    if type(seal) is not SplitSeal:
        raise AllocationError("verification_source_invalid", "seal")
    parts = (seal.assignments, seal.cases)
    if type(seal.digest) is not str or not all(
        isinstance(m, Mapping)
        and all(
            type(k) is str and _is_str_tuple(v) and list(v) == sorted(set(v)) and v
            for k, v in m.items()
        )
        for m in parts
    ):
        raise AllocationError("verification_source_invalid", "seal_shape")
    recomputed = digest([SEAL_SCHEMA, dict(seal.assignments), dict(seal.cases)])
    if recomputed != seal.digest:
        raise AllocationError("verification_source_invalid", "seal_digest")
    return seal


@dataclass(frozen=True)
class _Corroboration:
    """What OPE-516 itself holds for one scope, read by this module (never by the caller)."""

    families: frozenset[str]
    roots: Mapping[str, tuple[str, str]]  # live root_ref -> (family_id, stored case_ref)
    cases: Mapping[str, tuple[str, ...]]  # digest-verified seal: case_ref -> families
    manifest_digest: str
    ledger_id: str
    identity: Json
    scope_digest: str
    variants: VariantLedger
    fit_pin: str  # digest of OPE-516's own mandate-store pin row, "" where none exists
    charged: Charged  # (child_id, root_ref, recipe_id, cost) of OPE-516's charged children

    def root_reason(self, family_id: str, root_ref: str, case_ref: str | None = None) -> str:
        """Empty when ``root_ref`` is a live OPE-516 root of ``family_id`` sealed to it alone."""
        found = self.roots.get(root_ref)
        if family_id not in self.families or found is None or found[0] != family_id:
            return "root_not_live_in_ope516"
        if case_ref is not None and case_ref != found[1]:
            return "case_ref_differs_from_ope516"
        if tuple(self.cases.get(found[1], ())) != (family_id,):
            return "case_not_sealed_to_family"
        return ""


def _fit_pin(db: sqlite3.Connection) -> str:
    """Digest of OPE-516's ``authority_pin`` row, read-only; "" when the table or row is absent.

    Where OPE-516 pins its worker mandate store (PR #225: realpaths and device/inode of the store
    directory and databases, organization, engagement, scope, plus the variant ledger's own file
    identity), a grant-bound budget binds to that pin too instead of inventing a second one.
    OPE-516 revisions without the table have nothing to bind.
    """
    if not db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='authority_pin'"
    ).fetchone():
        return ""
    row = db.execute(
        "SELECT authority_identity, ledger_identity FROM authority_pin WHERE slot=1"
    ).fetchone()
    return (
        "" if row is None else digest(["opentour.pilot.ope516-fit-pin.v1", [str(v) for v in row]])
    )


Charged = tuple[tuple[str, str, str, int], ...]  # (child_id, root_ref, recipe_id, cost > 0)


def _charged_children(db: sqlite3.Connection, grant_ref: str) -> Charged:
    """OPE-516 children that carry spend for this grant, with the cost each one was charged.

    Read read-only from the same ``cost`` column ``VariantLedger.spent`` sums; a zero-cost
    child carries no spend and is not returned. Children do not name the allocation line they
    fulfil; ``(grant_ref, root_ref, recipe_id)`` is the only link.
    """
    out: list[tuple[str, str, str, int]] = []
    for child_id, root_ref, cost, record in db.execute(
        "SELECT child_id, root_ref, cost, record FROM children WHERE grant_ref=? AND cost>0"
        " ORDER BY seq",
        (grant_ref,),
    ).fetchall():
        recipe = (json.loads(record).get("recipe") or {}).get("recipe_id")
        out.append((str(child_id), str(root_ref), str(recipe), int(cost)))
    return tuple(out)


def _read_charged(real: Path, grant_ref: str) -> Charged:
    db = sqlite3.connect(real.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        return _charged_children(db, grant_ref)
    finally:
        db.close()


def _read_fit_pin(real: Path) -> str:
    db = sqlite3.connect(real.as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        return _fit_pin(db)
    finally:
        db.close()


_LIVE_ROOTS_SQL = (
    "SELECT r.root_ref, r.family_id, r.case_ref FROM roots r JOIN root_scopes s"
    " ON s.root_ref=r.root_ref WHERE r.state='live' AND r.policy_revision=?"
    " AND r.seal_digest=? AND s.mode=? AND s.grant_ref=?"
)


def _corroborate(scope: object) -> _Corroboration:
    """Open the scope's OPE-516 ledger by path and read its manifest and live roots."""
    if (
        type(scope) is not VerificationScope
        or type(scope.policy_revision) is not str
        or type(scope.grant_ref) is not str
        or not scope.grant_ref
        or scope.mode not in ("preview", "training")
    ):
        raise AllocationError("verification_source_invalid")
    seal = _check_seal(scope.seal)
    ledger_id, identity = _ledger_identity(scope.variants)
    real = Path(identity["path"])
    try:
        variants = VariantLedger(real)
        manifest = variants.manifest(
            seal=seal,
            policy_revision=scope.policy_revision,
            grant_ref=scope.grant_ref,
            mode="training" if scope.mode == "training" else "preview",
        )
        db = sqlite3.connect(real.as_uri() + "?mode=ro", uri=True, timeout=30)
        try:
            rows = db.execute(
                _LIVE_ROOTS_SQL, (scope.policy_revision, seal.digest, scope.mode, scope.grant_ref)
            ).fetchall()
            fit_pin = _fit_pin(db)
            charged = _charged_children(db, scope.grant_ref)
        finally:
            db.close()
    except (sqlite3.Error, OSError, ValueError) as exc:
        raise AllocationError("verification_source_invalid", "ope516_ledger_unreadable") from exc
    if _ledger_identity(real)[0] != ledger_id:  # replaced while being read
        raise AllocationError("ledger_identity_mismatch", "changed_during_read")
    if not all(type(v) is str for row in rows for v in row):
        raise AllocationError("verification_source_invalid", "roots")
    roots = {
        str(ref): (str(fam), str(case))
        for ref, fam, case in rows
        if tuple(seal.assignments.get(fam, ())) == (TRAINING,)
    }
    families = manifest.get("independent_families")
    if not isinstance(families, list) or not all(type(f) is str for f in families):
        raise AllocationError("verification_source_invalid", "independent_families")
    if sorted({fam for fam, _ in roots.values()}) != sorted(families):
        raise AllocationError("verification_source_invalid", "manifest_disagrees_with_roots")
    mode = scope.mode
    return _Corroboration(
        families=frozenset(families),
        roots=roots,
        cases={k: tuple(v) for k, v in seal.cases.items()},
        manifest_digest=str(manifest["manifest_digest"]),
        ledger_id=ledger_id,
        identity=identity,
        scope_digest=digest(
            [
                "opentour.pilot.allocation-verification-scope.v1",
                ledger_id,
                seal.digest,
                scope.policy_revision,
                scope.grant_ref,
                mode,
            ]
        ),
        variants=variants,
        fit_pin=fit_pin,
        charged=charged,
    )


def _classify(
    families: list[FamilyInput],
    corroboration: _Corroboration | None = None,
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
        elif corroboration is not None and any(
            corroboration.root_reason(fam.family_id, r.root_ref, r.case_ref) for r in fam.roots
        ):
            # Every root must be a live OPE-516 root of this family, with the case OPE-516
            # stored for it sealed under this family alone.
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


def plan_allocation_id(plan: AllocationPlan) -> str:
    """The id a plan's whole record implies; ``reserve`` refuses any other id.

    It covers every field ``as_record`` stores or ``reserve`` reads: status, lines, decisions,
    requests, scores, ranking basis, stop reasons, detail, ROI flag, digests, verification
    basis/grant/manifest/mode/scope and holdout. A plain digest, not a signature.
    """
    record = plan.as_record()
    record.pop("allocation_id")
    return digest(["synthetic-allocation.v3", record])


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
    if holdout is not None:
        _check_holdout(holdout)
    fams = sorted(families, key=lambda f: str(getattr(f, "family_id", "")))
    corroboration: _Corroboration | None = None
    verification = (CALLER_ASSERTED, "", "", "", "")
    if verified_by is not None:
        corroboration = _corroborate(verified_by)
        verification = (
            WORKER_RECORD,
            verified_by.grant_ref,
            corroboration.manifest_digest,
            verified_by.mode,
            corroboration.scope_digest,
        )
    reasons, eligible = _classify(fams, corroboration, holdout)
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

    def sealed(plan: AllocationPlan) -> AllocationPlan:
        return replace(plan, allocation_id=plan_allocation_id(plan))

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
        return sealed(
            AllocationPlan(
                allocation_id="",
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
                verification_mode=verification[3],
                verification_scope=verification[4],
            )
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
    return sealed(
        AllocationPlan(
            allocation_id="",
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
            verification_mode=verification[3],
            verification_scope=verification[4],
        )
    )


CLOCK_TOLERANCE = 300  # seconds a caller's ``now`` may run ahead of the worker clock


def _clock_digest(grant_ref: str, high_water: object) -> str:
    return digest(["opentour.pilot.allocation-grant-clock.v1", grant_ref, high_water])


def _check_caller_now(now: object) -> int:
    """The caller's time is an int within ``CLOCK_TOLERANCE`` of the worker clock, or refused.

    A caller time only ever narrows the window, so a far-future value adds nothing and is
    refused (``clock_skew_refused``) before anything is read or stored.
    """
    if not _int64(now):
        raise AllocationError("grant_link_invalid", "now")
    assert isinstance(now, int)
    if now > _worker_now() + CLOCK_TOLERANCE:
        raise AllocationError("clock_skew_refused", f"now is more than {CLOCK_TOLERANCE}s ahead")
    return now


def _worker_now() -> int:
    """The worker's own clock, in integer Unix seconds: the authority on grant expiry.

    Deliberately not a parameter of any entry point, so no caller can supply or replace it.
    Tests pin it by patching this module attribute (a test-only seam, as in OPE-544).
    OPE-516's ``Grant.expires_at`` is an unspecified int; Unix seconds is the unit the repo's
    ``LearningMandates`` uses for ``expires_at``, and is assumed here. The assumption matters:
    an ``expires_at`` in milliseconds would look ~1000x later and expiry would fail open.
    """
    return int(time.time())


def _check_grant(grant: object) -> Grant:
    """Exact types on every ``Grant`` field; counts within the sqlite int64 range."""
    if type(grant) is not Grant or type(grant.grant_ref) is not str or not grant.grant_ref:
        raise AllocationError("grant_link_invalid")
    flags = (
        grant.generation_authorized,
        grant.training_authorized,
        grant.execution_location_approved,
    )
    if not all(type(f) is bool for f in flags):
        raise AllocationError("grant_link_invalid", "flags must be bool")
    if not _int64(grant.budget, grant.expires_at):
        raise AllocationError("grant_link_invalid", "budget, expires_at and now must be int")
    teachers = grant.approved_external_teachers
    if type(teachers) is not frozenset or not all(type(t) is str for t in teachers):
        raise AllocationError("grant_link_invalid", "approved_external_teachers")
    return grant


def _grant_record(grant: Grant) -> Json:
    return {
        "grant_ref": grant.grant_ref,
        "generation_authorized": grant.generation_authorized,
        "training_authorized": grant.training_authorized,
        "execution_location_approved": grant.execution_location_approved,
        "budget": grant.budget,
        "expires_at": grant.expires_at,
        "approved_external_teachers": sorted(grant.approved_external_teachers),
    }


def _record_headroom(record: Mapping[str, Any], spent: int, now: int) -> int:
    """Mirror of OPE-516's seed gate: unauthorized, expired or unapproved authorizes nothing."""
    if (
        not (
            record["generation_authorized"] is True
            and record["training_authorized"] is True
            and record["execution_location_approved"] is True
        )
        or now >= record["expires_at"]
    ):
        return 0
    return max(0, int(record["budget"]) - spent)


def grant_headroom(grant: Grant, variants: VariantLedger, *, now: int) -> int:
    """Arithmetic headroom of a *caller-supplied, unverified* grant: ``budget - spent``.

    Nothing here verifies the ``Grant``: a forged one with ``budget=10**9`` returns about
    ``10**9``. An unauthorized, expired or location-unapproved grant gives 0 (mirrors OPE-516's
    seed gate); expiry is judged on ``max(_worker_now(), now)``, so ``now`` can only narrow it.
    Read-only: this only calls OPE-516's public ``spent``. It does not subtract reservations
    held in an ``AllocationLedger`` (the ledger does that itself).
    """
    _check_grant(grant)
    if type(variants) is not VariantLedger:
        raise AllocationError("grant_link_invalid")
    if not _int64(now):
        raise AllocationError("grant_link_invalid", "budget, expires_at and now must be int")
    effective = max(_worker_now(), _check_caller_now(now))
    return _record_headroom(_grant_record(grant), variants.spent(grant.grant_ref), effective)


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
    """CREATE TABLE IF NOT EXISTS budget (
        budget_ref TEXT PRIMARY KEY, cap_units INTEGER NOT NULL, cap_spend INTEGER NOT NULL,
        unit_cost INTEGER NOT NULL, cost_basis TEXT NOT NULL, grant_ref TEXT NOT NULL,
        ledger_id TEXT NOT NULL, grant_digest TEXT NOT NULL, row_digest TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS grant_pin (
        grant_ref TEXT PRIMARY KEY, ledger_id TEXT NOT NULL, identity TEXT NOT NULL,
        grant_record TEXT NOT NULL, grant_digest TEXT NOT NULL, fit_pin TEXT NOT NULL,
        row_digest TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS grant_budget (
        budget_ref TEXT PRIMARY KEY, grant_ref TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS allocation (
        allocation_id TEXT NOT NULL, budget_ref TEXT NOT NULL, state TEXT NOT NULL,
        units INTEGER NOT NULL, spend INTEGER NOT NULL, record TEXT NOT NULL,
        scope TEXT NOT NULL, PRIMARY KEY (budget_ref, allocation_id))""",
    """CREATE TABLE IF NOT EXISTS line (
        budget_ref TEXT NOT NULL, root_ref TEXT NOT NULL, recipe_id TEXT NOT NULL,
        family_id TEXT NOT NULL, allocation_id TEXT NOT NULL, cost INTEGER NOT NULL,
        state TEXT NOT NULL, grant_ref TEXT NOT NULL, verification_basis TEXT NOT NULL,
        authority TEXT NOT NULL, prior_children TEXT NOT NULL,
        PRIMARY KEY (authority, root_ref, recipe_id))""",
    """CREATE TABLE IF NOT EXISTS grant_clock (
        grant_ref TEXT PRIMARY KEY, high_water INTEGER NOT NULL, row_digest TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS retracted (
        family_id TEXT PRIMARY KEY, reason TEXT NOT NULL)""",
)
_TABLES = (
    "meta",
    "budget",
    "grant_pin",
    "grant_budget",
    "allocation",
    "line",
    "retracted",
    "grant_clock",
)
# Line states. reserved and committed count against the cap. A retraction frees an unspent
# reservation (invalidated) but keeps committed spend counted (invalidated_spent): spend is
# never refunded. released frees the cap (discarded unfinished selection).
_COUNTED = ("reserved", "committed", "invalidated_spent")


def _schema_shape(db: sqlite3.Connection) -> dict[str, list[tuple[Any, ...]]]:
    """Exact column set (name, type, not-null, default, primary-key position) of every table."""
    return {
        table: [tuple(row[1:6]) for row in db.execute(f"PRAGMA table_info({table})")]
        for table in _TABLES
    }


def _expected_shape() -> dict[str, list[tuple[Any, ...]]]:
    db = sqlite3.connect(":memory:")
    try:
        for stmt in _SCHEMA:
            db.execute(stmt)
        return _schema_shape(db)
    finally:
        db.close()


def _budget_digest(*fields: object) -> str:
    return digest(["opentour.pilot.allocation-budget-row.v2", list(fields)])


def _pin_digest(*fields: object) -> str:
    return digest(["opentour.pilot.allocation-grant-pin.v1", list(fields)])


def _check_budget_args(
    budget_ref: object,
    cap_units: object,
    cap_spend: object,
    unit_cost: object,
    cost_basis: object = "fixed_unit",
) -> None:
    if type(budget_ref) is not str or not budget_ref:
        raise AllocationError("budget_invalid", "budget_ref")
    if not _int64(cap_units, cap_spend, unit_cost) or unit_cost < 1:  # type: ignore[operator]
        raise AllocationError("budget_invalid")
    if type(cost_basis) is not str or cost_basis not in RANKING_FOR_COST_BASIS:
        raise AllocationError("budget_invalid", "cost_basis")


def _expected_requests(plan: AllocationPlan) -> list[Json]:
    """The requests ``allocate`` derives from a plan's own decisions.

    Owner-decision requests for policy conflicts (in decision order), then one real-label
    request per cell whose families fell below the minimum support (in cell order). The
    decisions themselves cannot be re-derived at reserve: that needs the caller's families.
    """
    owner: list[Json] = [
        {"kind": "owner_decision", "family_id": d.family_id}
        for d in plan.decisions
        if d.reason == "policy_conflict"
    ]
    low = Counter(tuple(d.cell) for d in plan.decisions if d.reason == "below_min_support")
    labels: list[Json] = [
        {"kind": "real_label_request", "cell": list(cell), "support": n}
        for cell, n in sorted(low.items())
    ]
    return [*owner, *labels]


@dataclass(frozen=True)
class _Budget:
    ref: str
    cap_units: int
    cap_spend: int
    unit_cost: int
    cost_basis: str
    grant_ref: str
    ledger_id: str
    grant_digest: str
    grant: Json | None  # pinned grant record (grant-bound budgets only)
    identity: Json | None  # pinned OPE-516 ledger identity (grant-bound budgets only)


class AllocationLedger:
    """Atomic, replay-safe budget ledger. ``reserve`` is BEGIN IMMEDIATE, recount, insert."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._shared: sqlite3.Connection | None = None
        if self._path == ":memory:":
            self._shared = sqlite3.connect(":memory:", isolation_level=None)
        with self._tx() as db:
            for stmt in _SCHEMA:
                db.execute(stmt)
            db.execute("INSERT OR IGNORE INTO meta VALUES ('schema', ?)", (LEDGER_SCHEMA,))
            version = db.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
            if _schema_shape(db) != _expected_shape() or version != (LEDGER_SCHEMA,):
                # A ledger from an earlier revision (or an edited one) is not migrated; the
                # transaction rolls back, so this check never alters it.
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

    # -- integrity -----------------------------------------------------------------------

    @staticmethod
    def _load_pin(
        db: sqlite3.Connection, grant_ref: str
    ) -> tuple[str, Json, Json, str, str] | None:
        row = db.execute(
            "SELECT ledger_id, identity, grant_record, grant_digest, fit_pin, row_digest"
            " FROM grant_pin WHERE grant_ref=?",
            (grant_ref,),
        ).fetchone()
        if row is None:
            return None
        ledger_id, identity_text, record_text, grant_dg, fit_pin, row_dg = row
        if row_dg != _pin_digest(
            grant_ref, ledger_id, identity_text, record_text, grant_dg, fit_pin
        ):
            raise AllocationError("ledger_integrity_failed", f"grant_pin {grant_ref}")
        try:
            identity, record = json.loads(identity_text), json.loads(record_text)
        except (TypeError, ValueError) as exc:
            raise AllocationError("ledger_integrity_failed", f"grant_pin {grant_ref}") from exc
        if (
            digest(record) != grant_dg
            or digest(["opentour.pilot.ope516-ledger-identity.v1", identity]) != ledger_id
            or record.get("grant_ref") != grant_ref
        ):
            raise AllocationError("ledger_integrity_failed", f"grant_pin {grant_ref}")
        return str(ledger_id), identity, record, str(grant_dg), str(fit_pin)

    def _load_budget(self, db: sqlite3.Connection, budget_ref: str) -> _Budget:
        """Read one budget and refuse it unless every integrity cross-check holds.

        Grant-ness is carried by four places at once (``grant_ref``, ``ledger_id`` and
        ``grant_digest`` columns, the ``grant_budget`` membership row and the pinned grant),
        all covered by row digests; blanking one column does not make a planning budget.
        """
        row = db.execute(
            "SELECT cap_units, cap_spend, unit_cost, cost_basis, grant_ref, ledger_id,"
            " grant_digest, row_digest FROM budget WHERE budget_ref=?",
            (budget_ref,),
        ).fetchone()
        if row is None:
            raise AllocationError("budget_unknown", budget_ref)
        cap_units, cap_spend, unit_cost, cost_basis, grant_ref, ledger_id, grant_dg, row_dg = row
        if (
            not _int64(cap_units, cap_spend, unit_cost)
            or unit_cost < 1
            or cost_basis not in RANKING_FOR_COST_BASIS
        ):
            raise AllocationError("ledger_integrity_failed", f"budget {budget_ref}")
        fields = (
            budget_ref,
            cap_units,
            cap_spend,
            unit_cost,
            cost_basis,
            grant_ref,
            ledger_id,
            grant_dg,
        )
        if row_dg != _budget_digest(*fields):
            raise AllocationError("ledger_integrity_failed", f"budget {budget_ref}")
        if len({bool(grant_ref), bool(ledger_id), bool(grant_dg)}) != 1:
            raise AllocationError("ledger_integrity_failed", f"budget {budget_ref} grant columns")
        member = db.execute(
            "SELECT grant_ref FROM grant_budget WHERE budget_ref=?", (budget_ref,)
        ).fetchone()
        authority = GRANT_AUTHORITY if grant_ref else PLANNING_RECORD
        if (
            (member is None) == bool(grant_ref)
            or (member is not None and member[0] != grant_ref)
            or db.execute(
                "SELECT 1 FROM line WHERE budget_ref=? AND (authority<>? OR grant_ref<>?) LIMIT 1",
                (budget_ref, authority, grant_ref),
            ).fetchone()
            or db.execute(
                "SELECT 1 FROM allocation WHERE budget_ref=? AND (scope<>'')<>? LIMIT 1",
                (budget_ref, bool(grant_ref)),
            ).fetchone()
        ):
            raise AllocationError("ledger_integrity_failed", f"budget {budget_ref} grant binding")
        grant: Json | None = None
        identity: Json | None = None
        if grant_ref:
            pin = self._load_pin(db, grant_ref)
            if pin is None or pin[0] != ledger_id or pin[3] != grant_dg:
                raise AllocationError("ledger_integrity_failed", f"budget {budget_ref} grant pin")
            identity, grant = pin[1], pin[2]
            self._load_clock(db, grant_ref)  # a dropped or edited clock row is refused
        return _Budget(
            budget_ref,
            cap_units,
            cap_spend,
            unit_cost,
            cost_basis,
            grant_ref,
            ledger_id,
            grant_dg,
            grant,
            identity,
        )

    def _bind_fit_pin(self, db: sqlite3.Connection, grant_ref: str, current: str) -> None:
        """Hold OPE-516's mandate-store pin fixed once seen (first sight is trusted, as in #225).

        A pin recorded here that OPE-516 no longer holds, or holds differently, is
        ``ledger_identity_mismatch``. An OPE-516 ledger that pins its store only after this
        budget opened is bound at the first reserve, open or commit that sees the pin.
        """
        pin = self._load_pin(db, grant_ref)
        if pin is None:
            raise AllocationError("ledger_integrity_failed", f"grant_pin {grant_ref}")
        stored = pin[4]
        if stored == current:
            return
        if stored:
            raise AllocationError("ledger_identity_mismatch", f"{grant_ref} fit_authority_pin")
        row = db.execute(
            "SELECT ledger_id, identity, grant_record, grant_digest FROM grant_pin"
            " WHERE grant_ref=?",
            (grant_ref,),
        ).fetchone()
        db.execute(
            "UPDATE grant_pin SET fit_pin=?, row_digest=? WHERE grant_ref=?",
            (current, _pin_digest(grant_ref, *row, current), grant_ref),
        )

    def _put_budget(
        self,
        db: sqlite3.Connection,
        budget_ref: str,
        cap_units: int,
        cap_spend: int,
        unit_cost: int,
        cost_basis: str,
        grant_ref: str = "",
        ledger_id: str = "",
        grant_dg: str = "",
    ) -> None:
        fields = (
            budget_ref,
            cap_units,
            cap_spend,
            unit_cost,
            cost_basis,
            grant_ref,
            ledger_id,
            grant_dg,
        )
        db.execute(
            "INSERT INTO budget VALUES (?,?,?,?,?,?,?,?,?)", (*fields, _budget_digest(*fields))
        )
        if grant_ref:
            db.execute("INSERT INTO grant_budget VALUES (?,?)", (budget_ref, grant_ref))

    def _grant_live(
        self,
        db: sqlite3.Connection,
        grant_ref: str,
        charged: Charged = (),
    ) -> int:
        """Live spend on every budget of this grant in this ledger, not yet in OPE-516's total.

        A committed (or spent-invalidated) line is *fulfilled up to the charged cost*: OPE-516
        children for the same ``(grant_ref, root_ref, recipe_id)`` that did not exist when the
        line was committed (``prior_children``) reduce the line's counted cost by their charged
        cost, never below zero (``max(0, cost - new charged cost)``). That spend is already in
        ``VariantLedger.spent``. Children do not name their line, so this is the best link that
        exists, not a reliable one: a post-commit child of that root and recipe made outside the
        allocation also frees the line's cost (OPE-516's own admission check still caps real
        spend). Grant lines are unique per ``(root_ref, recipe_id)`` here, so a child reduces
        at most one line. Reserved lines always count in full.
        """
        new_by_key: dict[tuple[str, str], dict[str, int]] = {}
        for child_id, root_ref, recipe_id, child_cost in charged:
            new_by_key.setdefault((root_ref, recipe_id), {})[child_id] = child_cost
        total = 0
        marks = ",".join("?" for _ in _COUNTED)
        for (ref,) in db.execute(
            "SELECT budget_ref FROM grant_budget WHERE grant_ref=? ORDER BY budget_ref",
            (grant_ref,),
        ).fetchall():
            self._load_budget(db, str(ref))
            for root_ref, recipe_id, cost, state, prior_text in db.execute(
                "SELECT root_ref, recipe_id, cost, state, prior_children FROM line"  # noqa: S608
                f" WHERE budget_ref=? AND state IN ({marks})",
                (str(ref), *_COUNTED),
            ).fetchall():
                if state != "reserved":
                    try:
                        prior = set(json.loads(prior_text)) if prior_text else set()
                    except ValueError as exc:
                        raise AllocationError(
                            "ledger_integrity_failed", f"line {root_ref}"
                        ) from exc
                    new = new_by_key.get((str(root_ref), str(recipe_id)), {})
                    generated = sum(c for cid, c in new.items() if cid not in prior)
                    total += max(0, int(cost) - generated)  # generated part is in OPE-516's total
                    continue
                total += int(cost)
        return total

    @staticmethod
    def _load_clock(db: sqlite3.Connection, grant_ref: str) -> int:
        """The grant's high-water time. Every pinned grant has exactly one, digest-covered."""
        row = db.execute(
            "SELECT high_water, row_digest FROM grant_clock WHERE grant_ref=?", (grant_ref,)
        ).fetchone()
        if row is None or not _int64(row[0]) or row[1] != _clock_digest(grant_ref, row[0]):
            raise AllocationError("ledger_integrity_failed", f"grant_clock {grant_ref}")
        return int(row[0])

    @staticmethod
    def _put_clock(db: sqlite3.Connection, grant_ref: str, high_water: int) -> None:
        db.execute(
            "INSERT INTO grant_clock VALUES (?,?,?) ON CONFLICT(grant_ref)"
            " DO UPDATE SET high_water=excluded.high_water, row_digest=excluded.row_digest",
            (grant_ref, high_water, _clock_digest(grant_ref, high_water)),
        )

    def _clock(self, db: sqlite3.Connection, grant_ref: str, caller_now: int) -> int:
        """Time used for expiry: ``max(_worker_now(), caller_now)``, as in OPE-544.

        ``caller_now`` was refused before (``_check_caller_now``) if it is more than
        ``CLOCK_TOLERANCE`` ahead of the worker clock, so it can only narrow the window by that
        much. The per-grant high-water mark detects a worker clock that stepped back
        (``clock_regressed``). It is written inside the caller's transaction, so a refused
        operation never moves it; a stored mark is therefore always before ``expires_at`` and
        never ends a grant by itself.
        """
        worker = _worker_now()
        stored = self._load_clock(db, grant_ref)
        if worker + CLOCK_TOLERANCE < stored:
            raise AllocationError("clock_regressed", f"{grant_ref}: {worker} < {stored}")
        effective = max(worker, caller_now)
        if effective > stored:
            self._put_clock(db, grant_ref, effective)
        return effective

    # -- budgets -------------------------------------------------------------------------

    def open_budget(
        self,
        budget_ref: str,
        *,
        cap_units: int,
        cap_spend: int,
        unit_cost: int,
        cost_basis: str = "fixed_unit",
    ) -> None:
        """Open a *planning* budget bound to the run profile's ``unit_cost`` and ``cost_basis``.

        Immutable. It accepts caller-asserted plans; nothing on it is authorized spend (see
        ``status``).
        """
        _check_budget_args(budget_ref, cap_units, cap_spend, unit_cost, cost_basis)
        with self._tx() as db:
            if db.execute("SELECT 1 FROM budget WHERE budget_ref=?", (budget_ref,)).fetchone():
                b = self._load_budget(db, budget_ref)
                if (b.cap_units, b.cap_spend, b.unit_cost, b.cost_basis, b.grant_ref) != (
                    cap_units,
                    cap_spend,
                    unit_cost,
                    cost_basis,
                    "",
                ):
                    raise AllocationError("budget_conflict", budget_ref)
                return
            self._put_budget(db, budget_ref, cap_units, cap_spend, unit_cost, cost_basis)

    def open_budget_for_grant(
        self,
        budget_ref: str,
        *,
        grant: Grant,
        variants: str | Path,
        now: int,
        cap_units: int,
        unit_cost: int,
        cap_spend: int | None = None,
        cost_basis: str = "fixed_unit",
    ) -> Json:
        """Open a budget bound to one OPE-516 ledger file and one pinned grant.

        The grant is pinned on its first presentation *to this ledger file* and is unverified:
        anyone can make that first presentation, so the budget and its lines are labelled
        ``ope516_grant_unverified`` / ``grant_basis="first_presentation_unverified"`` and none
        of it is authorized spend until a worker-held grant record exists (OPE-516 / owner).

        ``variants`` is the path of the file-backed OPE-516 ``VariantLedger``. The first grant
        presented for a ``grant_ref`` is pinned with that ledger's identity; any other grant
        content (``grant_conflict``) or ledger (``ledger_identity_mismatch``) is refused later.
        The cap is ``min(cap_spend, grant.budget - OPE-516 spent - live spend on every budget of
        this grant here)``. Reopening the same budget for the same grant, ledger, unit cost and
        unit cap is idempotent and keeps the stored cap.
        """
        _check_grant(grant)
        if not _int64(now):
            raise AllocationError("grant_link_invalid", "budget, expires_at and now must be int")
        _check_budget_args(budget_ref, cap_units, 0, unit_cost, cost_basis)
        if cap_spend is not None and not _int64(cap_spend):
            raise AllocationError("budget_invalid")
        ledger_id, identity = _ledger_identity(variants)
        record = _grant_record(grant)
        grant_dg = digest(record)
        now = _check_caller_now(now)
        try:
            # Children first, then spent (as reserve and commit do): a child landing between
            # the two reads is then counted in spent but not as fulfilment, never the reverse.
            charged = _read_charged(Path(identity["path"]), grant.grant_ref)
            spent = VariantLedger(Path(identity["path"])).spent(grant.grant_ref)
            fit_pin = _read_fit_pin(Path(identity["path"]))
        except sqlite3.Error as exc:
            raise AllocationError(
                "verification_source_invalid", "ope516_ledger_unreadable"
            ) from exc
        with self._tx() as db:
            pin = self._load_pin(db, grant.grant_ref)
            if pin is None:
                identity_text, record_text = canonical(identity), canonical(record)
                db.execute(
                    "INSERT INTO grant_pin VALUES (?,?,?,?,?,?,?)",
                    (
                        grant.grant_ref,
                        ledger_id,
                        identity_text,
                        record_text,
                        grant_dg,
                        fit_pin,
                        _pin_digest(
                            grant.grant_ref,
                            ledger_id,
                            identity_text,
                            record_text,
                            grant_dg,
                            fit_pin,
                        ),
                    ),
                )
                self._put_clock(db, grant.grant_ref, 0)  # every pinned grant has a clock row
            elif pin[0] != ledger_id:
                raise AllocationError("ledger_identity_mismatch", grant.grant_ref)
            elif pin[3] != grant_dg:
                raise AllocationError("grant_conflict", grant.grant_ref)
            else:
                self._bind_fit_pin(db, grant.grant_ref, fit_pin)
            if db.execute("SELECT 1 FROM budget WHERE budget_ref=?", (budget_ref,)).fetchone():
                b = self._load_budget(db, budget_ref)
                if (b.cap_units, b.unit_cost, b.cost_basis, b.grant_ref, b.ledger_id) != (
                    cap_units,
                    unit_cost,
                    cost_basis,
                    grant.grant_ref,
                    ledger_id,
                ):
                    raise AllocationError("budget_conflict", budget_ref)
                return self._status(db, budget_ref)
            effective_now = self._clock(db, grant.grant_ref, now)
            headroom = _record_headroom(record, spent, effective_now) - self._grant_live(
                db, grant.grant_ref, charged
            )
            effective = headroom if cap_spend is None else min(cap_spend, headroom)
            if effective <= 0:
                raise AllocationError("grant_unavailable", grant.grant_ref)
            self._put_budget(
                db,
                budget_ref,
                cap_units,
                effective,
                unit_cost,
                cost_basis,
                grant.grant_ref,
                ledger_id,
                grant_dg,
            )
            return self._status(db, budget_ref)

    @staticmethod
    def _used(db: sqlite3.Connection, budget_ref: str) -> tuple[int, int]:
        marks = ",".join("?" for _ in _COUNTED)
        rows = db.execute(
            f"SELECT cost FROM line WHERE budget_ref=? AND state IN ({marks})",  # noqa: S608
            (budget_ref, *_COUNTED),
        ).fetchall()
        return len(rows), sum(int(r[0]) for r in rows)

    @staticmethod
    def _state(db: sqlite3.Connection, budget_ref: str, allocation_id: str) -> str:
        row = db.execute(
            "SELECT state FROM allocation WHERE allocation_id=? AND budget_ref=?",
            (allocation_id, budget_ref),
        ).fetchone()
        if row is None:
            raise AllocationError("allocation_unknown", allocation_id)
        return str(row[0])

    # -- reserve / commit ----------------------------------------------------------------

    def reserve(
        self,
        budget_ref: str,
        plan: AllocationPlan,
        *,
        verified_by: VerificationScope | None = None,
        now: int | None = None,
        _fail_after: int | None = None,
    ) -> Json:
        """Atomically reserve a whole plan or refuse it. Replaying the same plan is a no-op.

        The charge is re-derived as ``units x the budget's bound unit_cost``. A line priced at
        anything else, an ``allocation_id`` its whole record does not imply, or a replay whose
        content differs from the stored plan is refused as ``plan_binding_mismatch``.

        On a grant-bound budget ``verified_by`` (the OPE-516 scope) and ``now`` are required and
        no plan field is trusted: OPE-516 is re-read here (see ``_authorize_grant``), and the
        grant-wide spend is re-checked against the pinned grant and live OPE-516 spend.
        ``_fail_after`` is a test hook that raises mid-insert to prove rollback.
        """
        if type(plan) is not AllocationPlan:
            raise AllocationError("plan_invalid", "plan")
        if plan.status != "allocated" or not plan.lines:
            raise AllocationError("allocation_held", str(plan.hold_reason))
        if plan.control:
            raise AllocationError("control_not_reservable")
        if type(plan.lines) is not tuple or not all(type(x) is Line for x in plan.lines):
            raise AllocationError("plan_invalid", "lines")
        keys = {(x.root_ref, x.recipe_id) for x in plan.lines}
        if len(keys) != len(plan.lines) or any(
            not _int64(x.cost) or x.cost < 1 for x in plan.lines
        ):
            raise AllocationError("plan_invalid", "lines must be unique with positive cost")
        try:
            implied = plan_allocation_id(plan)
            record = canonical(plan.as_record())
        except (TypeError, ValueError) as exc:
            raise AllocationError("plan_invalid", "record") from exc
        if implied != plan.allocation_id:
            raise AllocationError(BINDING_MISMATCH, "allocation_id")
        if plan.roi_claim is not False:
            raise AllocationError("plan_invalid", "roi_claim is always false here")
        if now is not None:
            now = _check_caller_now(now)
        with self._tx() as db:
            budget = self._load_budget(db, budget_ref)
            unit_cost = budget.unit_cost
            if any(x.cost != unit_cost for x in plan.lines):
                raise AllocationError(BINDING_MISMATCH, f"unit_cost {unit_cost}")
            if plan.ranking_basis != RANKING_FOR_COST_BASIS[budget.cost_basis]:
                raise AllocationError(BINDING_MISMATCH, f"ranking_basis for {budget.cost_basis}")
            try:
                requests_ok = [dict(r) for r in plan.requests] == _expected_requests(plan)
            except (TypeError, ValueError, AttributeError):
                requests_ok = False
            if not requests_ok:
                raise AllocationError("plan_invalid", "requests are not the plan's own derivation")
            grant_ref = budget.grant_ref
            if grant_ref:
                if plan.verification_basis != WORKER_RECORD:
                    raise AllocationError("verification_uncorroborated", grant_ref)
                if plan.verification_grant_ref != grant_ref:
                    raise AllocationError("verification_grant_mismatch", grant_ref)
                if plan.verification_mode == "preview":
                    raise AllocationError("verification_preview_only", grant_ref)
                if plan.verification_mode != TRAINING:
                    raise AllocationError("verification_uncorroborated", "mode")
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
            if (
                used_units + len(plan.lines) > budget.cap_units
                or used_spend + charge > budget.cap_spend
            ):
                raise AllocationError(
                    "budget_exceeded", f"{used_units}+{len(plan.lines)}>{budget.cap_units}"
                )
            scope_text = ""
            if grant_ref:
                scope_text = self._authorize_grant(db, budget, plan, verified_by, now, charge)
            authority = GRANT_AUTHORITY if grant_ref else PLANNING_RECORD
            db.execute(
                "INSERT OR REPLACE INTO allocation VALUES (?,?,?,?,?,?,?)",
                (
                    plan.allocation_id,
                    budget_ref,
                    "reserved",
                    len(plan.lines),
                    charge,
                    record,
                    scope_text,
                ),
            )
            db.execute("DELETE FROM line WHERE state='released'")
            for index, line in enumerate(plan.lines):
                if _fail_after is not None and index == _fail_after:
                    raise RuntimeError("simulated crash")
                try:
                    db.execute(
                        "INSERT INTO line VALUES (?,?,?,?,?,?,?,?,?,?,'')",
                        (
                            budget_ref,
                            line.root_ref,
                            line.recipe_id,
                            line.family_id,
                            plan.allocation_id,
                            unit_cost,
                            "reserved",
                            grant_ref,
                            str(plan.verification_basis),
                            authority,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    raise AllocationError("line_already_allocated", line.root_ref) from exc
            return {"replayed": False, "state": "reserved", **self._status(db, budget_ref)}

    def _authorize_grant(
        self,
        db: sqlite3.Connection,
        budget: _Budget,
        plan: AllocationPlan,
        verified_by: VerificationScope | None,
        now: int | None,
        charge: int,
    ) -> str:
        """Re-derive a grant-bound plan from OPE-516 itself; return the scope to store."""
        grant_ref = budget.grant_ref
        if type(verified_by) is not VerificationScope:
            raise AllocationError("verification_uncorroborated", "verified_by scope required")
        if not _int64(now):
            raise AllocationError("grant_link_invalid", "now")
        assert now is not None and budget.grant is not None  # narrowed above / by _load_budget
        if verified_by.grant_ref != grant_ref:
            raise AllocationError("verification_grant_mismatch", grant_ref)
        if verified_by.mode != TRAINING:
            raise AllocationError("verification_preview_only", grant_ref)
        corr = _corroborate(verified_by)
        if corr.ledger_id != budget.ledger_id:
            raise AllocationError("ledger_identity_mismatch", grant_ref)
        self._bind_fit_pin(db, grant_ref, corr.fit_pin)
        if plan.verification_scope != corr.scope_digest:
            raise AllocationError("verification_scope_mismatch", grant_ref)
        if plan.verification_ref != corr.manifest_digest:
            raise AllocationError("verification_stale", "OPE-516 manifest changed since the plan")
        for line in plan.lines:
            why = corr.root_reason(line.family_id, line.root_ref)
            if why:
                raise AllocationError("verification_uncorroborated", f"{line.family_id}: {why}")
        self._check_headroom(db, budget, corr, now, charge)
        seal = verified_by.seal
        return canonical(
            {
                "policy_revision": verified_by.policy_revision,
                "grant_ref": grant_ref,
                "mode": verified_by.mode,
                "seal": {
                    "assignments": {k: list(v) for k, v in seal.assignments.items()},
                    "cases": {k: list(v) for k, v in seal.cases.items()},
                    "digest": seal.digest,
                },
            }
        )

    def _check_headroom(
        self, db: sqlite3.Connection, budget: _Budget, corr: _Corroboration, now: int, extra: int
    ) -> None:
        """Pinned grant budget - OPE-516 spent - unfulfilled live spend on every budget of it.

        ``extra`` is spend not yet counted as live (the plan being reserved; 0 at commit, whose
        own lines are already live). Expiry is judged on ``_clock`` (worker clock, caller time
        within the skew tolerance, whichever is later). Expired is ``grant_expired``;
        unauthorized flags are ``grant_unavailable``; no room is ``grant_headroom_exceeded``.
        Inherent race: OPE-516 can still admit a child between this read and the end of the
        transaction, because it does not see reservations.
        """
        grant = budget.grant
        assert grant is not None  # grant-bound: pinned by _load_budget
        effective = self._clock(db, budget.grant_ref, now)
        if effective >= grant["expires_at"]:
            raise AllocationError("grant_expired", budget.grant_ref)
        if _record_headroom(grant, 0, effective) == 0:
            raise AllocationError("grant_unavailable", f"{budget.grant_ref} unauthorized")
        headroom = _record_headroom(grant, corr.variants.spent(budget.grant_ref), effective)
        live = self._grant_live(db, budget.grant_ref, corr.charged)
        if live + extra > headroom:
            raise AllocationError("grant_headroom_exceeded", f"{live}+{extra}>{headroom}")

    def _recheck_grant(
        self, db: sqlite3.Connection, budget: _Budget, allocation_id: str, now: int
    ) -> None:
        """At commit: roots still live in the bound OPE-516 ledger, and the grant still has room.

        The room check counts this allocation as live and OPE-516's current spend, so a commit
        must come *before* its lines are generated through OPE-516 (afterwards that spend would
        be counted twice and the commit refused, which is conservative).
        """
        row = db.execute(
            "SELECT scope FROM allocation WHERE allocation_id=? AND budget_ref=?",
            (allocation_id, budget.ref),
        ).fetchone()
        assert budget.identity is not None  # grant-bound: pinned by _load_budget
        try:
            stored = json.loads(row[0])
            seal = SplitSeal(
                {k: tuple(v) for k, v in stored["seal"]["assignments"].items()},
                {k: tuple(v) for k, v in stored["seal"]["cases"].items()},
                stored["seal"]["digest"],
            )
            scope = VerificationScope(
                budget.identity["path"],
                seal,
                stored["policy_revision"],
                stored["grant_ref"],
                stored["mode"],
            )
        except (TypeError, ValueError, KeyError, AttributeError) as exc:
            raise AllocationError("ledger_integrity_failed", f"allocation {allocation_id}") from exc
        if scope.grant_ref != budget.grant_ref or scope.mode != TRAINING:
            raise AllocationError("ledger_integrity_failed", f"allocation {allocation_id}")
        corr = _corroborate(scope)
        if corr.ledger_id != budget.ledger_id:
            raise AllocationError("ledger_identity_mismatch", budget.grant_ref)
        self._bind_fit_pin(db, budget.grant_ref, corr.fit_pin)
        for fam, root in db.execute(
            "SELECT family_id, root_ref FROM line WHERE allocation_id=? AND budget_ref=?"
            " AND state='reserved' ORDER BY family_id, root_ref",
            (allocation_id, budget.ref),
        ).fetchall():
            why = corr.root_reason(str(fam), str(root))
            if why:
                raise AllocationError("verification_uncorroborated", f"{fam}: {why}")
        self._check_headroom(db, budget, corr, now, 0)
        # Snapshot the charged children that already exist for each line, so that only a child
        # made after this commit can later count as having fulfilled it.
        for root, recipe in db.execute(
            "SELECT root_ref, recipe_id FROM line WHERE allocation_id=? AND budget_ref=?"
            " AND state='reserved'",
            (allocation_id, budget.ref),
        ).fetchall():
            prior = sorted(c for c, r, x, _ in corr.charged if (r, x) == (root, recipe))
            db.execute(
                "UPDATE line SET prior_children=? WHERE allocation_id=? AND budget_ref=?"
                " AND root_ref=? AND recipe_id=?",
                (canonical(prior), allocation_id, budget.ref, root, recipe),
            )

    def commit(self, budget_ref: str, allocation_id: str, *, now: int | None = None) -> Json:
        """Commit a reservation: the selection is final and its spend is never refunded.

        On a grant-bound budget ``now`` is required and OPE-516 is re-read first: roots still
        live, grant not expired, and pinned budget minus current OPE-516 spend minus every live
        reservation on the grant still covers this one. Commit before generating the lines.
        """
        if now is not None:
            now = _check_caller_now(now)
        with self._tx() as db:
            budget = self._load_budget(db, budget_ref)
            state = self._state(db, budget_ref, allocation_id)
            if state == "reserved":
                if budget.grant_ref:
                    if not _int64(now):
                        raise AllocationError("grant_link_invalid", "now")
                    assert now is not None
                    self._recheck_grant(db, budget, allocation_id, now)
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
            self._load_budget(db, budget_ref)
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

    def live_lines(self, budget_ref: str) -> list[LiveLine]:
        """Lines usable downstream (reserved or committed), each with its authority.

        ``authority="planning_record"`` lines (planning budget, caller-asserted or unchecked
        verification) are planning records; ``"ope516_grant_unverified"`` lines were re-derived
        from OPE-516 at reserve but rest on an unverified first-presentation grant
        (``grant_basis``). Neither is authorized spend.
        """
        with self._tx(begin=False) as db:
            self._load_budget(db, budget_ref)
            rows = db.execute(
                "SELECT family_id, root_ref, recipe_id, grant_ref, verification_basis, authority"
                " FROM line WHERE budget_ref=? AND state IN ('reserved','committed')"
                " ORDER BY family_id, root_ref, recipe_id",
                (budget_ref,),
            ).fetchall()
        return [
            LiveLine(
                family_id=str(fam),
                root_ref=str(root),
                recipe_id=str(recipe),
                grant_ref=str(grant_ref),
                verification_basis=str(basis),
                authority=str(authority),
                grant_basis=GRANT_BASIS_UNVERIFIED if authority == GRANT_AUTHORITY else NO_GRANT,
            )
            for fam, root, recipe, grant_ref, basis, authority in rows
        ]

    def status(self, budget_ref: str) -> Json:
        with self._tx(begin=False) as db:
            return self._status(db, budget_ref)

    def _status(self, db: sqlite3.Connection, budget_ref: str) -> Json:
        b = self._load_budget(db, budget_ref)
        units, spend = self._used(db, budget_ref)
        marks = ",".join("?" for _ in _COUNTED)
        by_basis = db.execute(
            f"SELECT verification_basis, COUNT(*) FROM line WHERE budget_ref=?"  # noqa: S608
            f" AND state IN ({marks}) GROUP BY verification_basis ORDER BY verification_basis",
            (budget_ref, *_COUNTED),
        ).fetchall()
        return {
            "cap_units": b.cap_units,
            "cap_spend": b.cap_spend,
            "unit_cost": b.unit_cost,
            "grant_ref": b.grant_ref,
            "authority": GRANT_AUTHORITY if b.grant_ref else "planning_record_only",
            "grant_basis": GRANT_BASIS_UNVERIFIED if b.grant_ref else NO_GRANT,
            "authorized_spend": False,
            "cost_basis": b.cost_basis,
            "verification_required": WORKER_RECORD if b.grant_ref else "none",
            "ledger_id": b.ledger_id,
            "lines_by_verification_basis": {str(k): int(n) for k, n in by_basis},
            "used_units": units,
            "used_spend": spend,
            "remaining_units": b.cap_units - units,
            "remaining_spend": b.cap_spend - spend,
        }
