"""Allocate a fixed synthetic-example budget across verified, independent error families.

The allocator is a deterministic, worker-local, no-spend planner plus a restart-safe budget
ledger. Inputs are development-side facts only: family verification status, independence of
the verifier, residual support, uncovered approved transformations and cost. There is no field,
parameter or code path for a protected answer or protected score; ``family_from_record``
rejects records that carry one. Nothing here generates text, calls a model or fits anything.

Rules enforced:

* The budget is a hard cap on both example units and spend. A request that does not fit is
  refused whole (never rounded or partially filled), including under replay and concurrency.
* Only families that are verified by an independent verifier, in the real training partition,
  not disputed, not retracted, not protected-derived, not child-derived and not in a policy
  conflict can receive allocation. Near-duplicate families collapse to one.
* A family is one independent unit however many roots, copies or children it has.
* Zero eligible families, zero budget, poor validation yield or an unfittable mandatory slice
  return an explicit hold rather than an empty success.
* Default spend is zero: an unset budget holds.

Simulated, not proven: every family, verification, cost and yield value is a generated
sentinel input supplied by the caller. Tests prove software behavior only. Nothing here is
provider, customer-evidence, fit-qualification or ROI evidence; with a fixed unit cost the
ranking is count-based and ``roi_claim`` is always false. Children produced by OPE-516 are
read only through ``covered_from_children`` (disposition ``admitted`` only); OPE-516's
``independent_family_count`` is deliberately not consumed and must be reconciled once its
correction lands.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest

Json = dict[str, Any]
PLAN_SCHEMA = "opentour.pilot.synthetic-allocation.v1"
PROFILE_SCHEMA = "opentour.pilot.synthetic-allocation-profile.v1"

VERIFIED = "verified"
TRAINING = "training"
SYNTHETIC_PREFIX = "var-"
Cell = tuple[str, str]
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
    verification: str  # verified | unverified | disputed | retracted
    verifier_independent: bool
    roots: tuple[RootInput, ...]
    policy_conflict: bool = False
    protected_derived: bool = False


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
        )
        if any(isinstance(v, bool) or not isinstance(v, int) or v < 0 for v in ints):
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
    cell_scores: tuple[tuple[Cell, str], ...]
    ranking_basis: str  # count_based | cost_based
    stop_reasons: tuple[str, ...]
    policy_digest: str
    input_digest: str
    control: bool = False
    roi_claim: bool = False
    detail: Json = field(default_factory=dict)

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
            "units": self.units,
            "spend": self.spend,
            "ranking_basis": self.ranking_basis,
            "roi_claim": self.roi_claim,
            "stop_reasons": list(self.stop_reasons),
            "cell_scores": [[list(c), s] for c, s in self.cell_scores],
            "lines": [
                [x.family_id, x.root_ref, x.recipe_id, list(x.cell), x.phase, x.cost]
                for x in self.lines
            ],
            "decisions": [
                [d.family_id, list(d.cell), d.eligible, d.reason, d.units] for d in self.decisions
            ],
            "requests": list(self.requests),
            "policy_digest": self.policy_digest,
            "input_digest": self.input_digest,
            "detail": self.detail,
        }


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


def family_from_record(record: Mapping[str, Any]) -> FamilyInput:
    """Build a family from a plain record, refusing any protected answer or score key."""
    root_records = list(record.get("roots", ()))
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
    roots = tuple(
        RootInput(
            root_ref=str(r["root_ref"]),
            content_digest=str(r["content_digest"]),
            uncovered_recipes=tuple(str(x) for x in r.get("uncovered_recipes", ())),
            admitted_children=int(r.get("admitted_children", 0)),
            case_ref=str(r.get("case_ref", "")),
        )
        for r in root_records
    )
    cell = tuple(str(x) for x in record["cell"])
    if len(cell) != 2:
        raise AllocationError("cell_invalid")
    return FamilyInput(
        family_id=str(record["family_id"]),
        cell=(cell[0], cell[1]),
        split=str(record["split"]),
        verification=str(record["verification"]),
        verifier_independent=record.get("verifier_independent") is True,
        roots=roots,
        policy_conflict=record.get("policy_conflict") is True,
        protected_derived=record.get("protected_derived") is True,
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


def _classify(families: list[FamilyInput]) -> tuple[dict[str, str], list[FamilyInput]]:
    """Independently validate grouping and eligibility. Returns reasons and eligible families."""
    ids = [f.family_id for f in families]
    if len(set(ids)) != len(ids) or any(not i for i in ids):
        raise AllocationError("duplicate_family_id")
    ordered = sorted(families, key=lambda f: f.family_id)
    owner: dict[str, str] = {}
    conflicted: set[str] = set()
    for fam in ordered:
        for root in fam.roots:
            other = owner.setdefault(root.root_ref, fam.family_id)
            if other != fam.family_id:
                conflicted.update((other, fam.family_id))
    case_owner: dict[str, str] = {}
    for fam in ordered:
        for root in fam.roots:
            if root.case_ref:
                other = case_owner.setdefault(root.case_ref, fam.family_id)
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
        elif any(not r.case_ref for r in fam.roots):
            reason = "case_ref_missing"
        elif fam.protected_derived:
            reason = "protected_derived"
        elif fam.split == "protected":
            reason = "protected_split"
        elif fam.split != TRAINING:
            reason = "not_training_split"
        elif any(
            r.root_ref.startswith(SYNTHETIC_PREFIX) or r.content_digest.startswith(SYNTHETIC_PREFIX)
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
        elif fam.policy_conflict:
            reason = "policy_conflict"
        if not reason:
            digests = {r.content_digest for r in fam.roots}
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
        ]
    )


def _frac(value: Fraction) -> str:
    return f"{value.numerator}/{value.denominator}"


def _scores(
    cells: dict[Cell, list[FamilyInput]],
    queues: Mapping[str, list[tuple[str, str]]],
    policy: AllocationPolicy,
) -> dict[Cell, Fraction]:
    """Score = independent verified residual families x uncovered transformations / unit cost."""
    out: dict[Cell, Fraction] = {}
    for cell, fams in cells.items():
        recipes = {p[1] for f in fams for p in queues[f.family_id]}
        out[cell] = Fraction(len(fams) * len(recipes), policy.unit_cost)
    return out


def allocate(
    families: Iterable[FamilyInput],
    policy: AllocationPolicy,
    *,
    control: bool = False,
    match_units: int | None = None,
) -> AllocationPlan:
    """Plan an allocation. ``control=True`` is the seeded uniform comparator over the same roots."""
    policy.validate()
    fams = sorted(families, key=lambda f: f.family_id)
    reasons, eligible = _classify(fams)
    in_digest = _input_digest(fams)
    pol_digest = _policy_digest(policy)
    requests: list[Json] = [
        {"kind": "owner_decision", "family_id": f.family_id}
        for f in fams
        if reasons[f.family_id] == "policy_conflict"
    ]
    budget = min(policy.total_units, policy.spend_ceiling // policy.unit_cost)
    basis = "cost_based" if policy.cost_basis == "measured" else "count_based"
    seed = policy.seed
    allocation_id = digest(["synthetic-allocation", in_digest, pol_digest, control, match_units])

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
            allocation_id=allocation_id,
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

    queues = {f.family_id: _pairs(f, policy, seed) for f in eligible}
    by_id = {f.family_id: f for f in eligible}
    if sum(len(q) for q in queues.values()) == 0:
        return hold("no_uncovered_transformations")
    scores = _scores(cells, queues, policy)

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
        lines.append(Line(fam_id, root, recipe, by_id[fam_id].cell, phase, policy.unit_cost))
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
        ranked = sorted(cells, key=lambda c: (-scores[c], _hash(seed, "cell", *c)))
        progress = True
        while len(lines) < limit and progress:
            progress = False
            for cell in ranked:
                quota = policy.batch_size
                for fid in order(cells[cell], "ranked"):
                    if quota > 0 and take(fid, "ranked"):
                        quota -= 1
                        progress = True
    if not lines:
        return hold("no_uncovered_transformations")
    stop = "budget_exhausted" if len(lines) >= limit else "capacity_exhausted"
    return AllocationPlan(
        allocation_id=allocation_id,
        status="allocated",
        hold_reason="",
        lines=tuple(lines),
        decisions=decisions_for(per_family),
        requests=tuple(requests),
        cell_scores=tuple((c, _frac(scores[c])) for c in sorted(scores)),
        ranking_basis=basis,
        stop_reasons=(stop,),
        policy_digest=pol_digest,
        input_digest=in_digest,
        control=control,
        detail={"budget_units": budget, "eligible_families": len(eligible)},
    )


_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS budget (
        budget_ref TEXT PRIMARY KEY, cap_units INTEGER NOT NULL, cap_spend INTEGER NOT NULL)""",
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

    def open_budget(self, budget_ref: str, *, cap_units: int, cap_spend: int) -> None:
        if cap_units < 0 or cap_spend < 0:
            raise AllocationError("budget_invalid")
        with self._tx() as db:
            row = db.execute(
                "SELECT cap_units, cap_spend FROM budget WHERE budget_ref=?", (budget_ref,)
            ).fetchone()
            if row is None:
                db.execute("INSERT INTO budget VALUES (?,?,?)", (budget_ref, cap_units, cap_spend))
            elif tuple(row) != (cap_units, cap_spend):
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

        ``_fail_after`` is a test hook that raises mid-insert to prove rollback.
        """
        if plan.status != "allocated" or not plan.lines:
            raise AllocationError("allocation_held", plan.hold_reason)
        if plan.control:
            raise AllocationError("control_not_reservable")
        with self._tx() as db:
            cap = db.execute(
                "SELECT cap_units, cap_spend FROM budget WHERE budget_ref=?", (budget_ref,)
            ).fetchone()
            if cap is None:
                raise AllocationError("budget_unknown", budget_ref)
            prior = db.execute(
                "SELECT state FROM allocation WHERE allocation_id=? AND budget_ref=?",
                (plan.allocation_id, budget_ref),
            ).fetchone()
            if prior is not None and prior[0] != "released":
                return {"replayed": True, "state": prior[0], **self._status(db, budget_ref)}
            retracted = {r[0] for r in db.execute("SELECT family_id FROM retracted")}
            if any(line.family_id in retracted for line in plan.lines):
                raise AllocationError("family_retracted")
            used_units, used_spend = self._used(db, budget_ref)
            if used_units + plan.units > cap[0] or used_spend + plan.spend > cap[1]:
                raise AllocationError("budget_exceeded", f"{used_units}+{plan.units}>{cap[0]}")
            db.execute(
                "INSERT OR REPLACE INTO allocation VALUES (?,?,?,?,?,?)",
                (
                    plan.allocation_id,
                    budget_ref,
                    "reserved",
                    plan.units,
                    plan.spend,
                    canonical(plan.as_record()),
                ),
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
                            line.cost,
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
            "SELECT cap_units, cap_spend FROM budget WHERE budget_ref=?", (budget_ref,)
        ).fetchone()
        if cap is None:
            raise AllocationError("budget_unknown", budget_ref)
        units, spend = self._used(db, budget_ref)
        return {
            "cap_units": cap[0],
            "cap_spend": cap[1],
            "used_units": units,
            "used_spend": spend,
            "remaining_units": cap[0] - units,
            "remaining_spend": cap[1] - spend,
        }
