"""Independently validated synthetic training variants from verified workplace corrections.

A variant is a *child* of exactly one real, independently verified, training-partition root.
Children never become observed mistakes, never add independent families and never reach
protected evaluation. A generator's own label cannot admit a child: only a separate validator
that sees the policy and the changed fields, but never the proposed label, can.

Everything here is a worker-local, deterministic, no-spend port. Generators and validators are
supplied by the caller; this module performs no model calls and fits nothing.

Simulated, not proven: all generators, validators, grants and fit gates are caller-supplied
local doubles. Nothing here is provider, customer-evidence or fit-qualification evidence, and a
validated child is never a real observation.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any, Literal, Protocol

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
        "policy_revision_mismatch",
    }
)


@dataclass(frozen=True)
class Policy:
    """Customer-approved rule set bound to one revision.

    `clauses` maps a decision-time field to the target that applies when that field is absent.
    """

    revision: str
    decision_fields: tuple[str, ...]
    required_inputs: tuple[str, ...]
    clauses: Mapping[str, str]


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


@dataclass(frozen=True)
class SplitSeal:
    """Family-level split sealed before any seed is selected. Carries no labels or answers."""

    assignments: Mapping[str, tuple[str, ...]]  # family_id -> splits observed for that family
    digest: str

    @staticmethod
    def from_rows(rows: Iterable[tuple[str, str, str]]) -> SplitSeal:
        """Rows are (case_ref, family_id, split). Only family and split are retained."""
        seen: dict[str, set[str]] = {}
        for case_ref, family_id, split in rows:
            identifier(case_ref)
            identifier(family_id)
            if split not in {"training", "development", "protected"}:
                raise TrainingError("invalid_split")
            seen.setdefault(family_id, set()).add(split)
        assignments = {fam: tuple(sorted(s)) for fam, s in sorted(seen.items())}
        return SplitSeal(assignments, digest(["opentour.variants.split-seal.v1", assignments]))


@dataclass(frozen=True)
class FitGate:
    """Standing recurrence, budget, independent split and verifier gates, decided elsewhere."""

    open: bool
    reference: str


@dataclass(frozen=True)
class ProposedChild:
    recipe_id: str
    edits: Mapping[str, str | None]  # field -> new value; None removes the field
    proposed_label: str
    cost: int = 1


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
    labels = {label for name, label in policy.clauses.items() if not fields.get(name)}
    if not labels:
        return base_target
    return next(iter(labels)) if len(labels) == 1 else None


def _normal(fields: Mapping[str, str]) -> str:
    return digest(sorted((k, re.sub(r"[\W_]+", " ", v.lower()).strip()) for k, v in fields.items()))


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


def _seed_reason(
    seed: SeedRoot, policy: Policy, grant: Grant, seal: SplitSeal, now: int
) -> tuple[str, Json | None] | None:
    if seed.split == "protected":
        return "protected_seed", None
    if seed.split != "training":
        return "not_training_partition", None
    splits = seal.assignments.get(seed.family_id, ())
    if len(splits) != 1 or splits[0] != seed.split:
        return "cross_split_family", None
    if not (seed.target_verified and seed.target_verifier_ref and seed.verifier_independent):
        return "unverified_target", None
    if seed.policy_revision != policy.revision:
        return "policy_revision_mismatch", None
    if not (grant.generation_authorized and grant.training_authorized) or now >= grant.expires_at:
        return "grant_missing", None
    if not grant.execution_location_approved:
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
    root_ref TEXT PRIMARY KEY, family_id TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL
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
"""


class VariantLedger:
    """Durable, replay-safe store of root state, children, spend and manifests."""

    def __init__(self, path: Path | str = ":memory:") -> None:
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
        identifier(plan.plan_ref)
        if plan.mode not in {"preview", "training"}:
            raise TrainingError("invalid_mode")
        ids: list[str] = []
        created = replayed = not_attempted = 0
        stopped = False
        with self._tx() as db:
            root = plan.root
            row = db.execute(
                "SELECT state FROM roots WHERE root_ref=?", (root.root_ref,)
            ).fetchone()
            invalid_root = row is not None and row[0] != "live"
            seed_block = (
                None
                if invalid_root
                else _seed_reason(root, plan.policy, plan.grant, plan.seal, now)
            )
            if row is None and seed_block is None:
                db.execute(
                    "INSERT INTO roots VALUES (?,?,?,?)",
                    (root.root_ref, root.family_id, "live", ""),
                )
            for index, proposal in enumerate(plan.proposals):
                if stopped:
                    not_attempted += 1
                    continue
                key = self._key(plan, proposal)
                existing = db.execute(
                    "SELECT child_id, disposition, reason FROM children WHERE idem_key=?", (key,)
                ).fetchone()
                if existing is not None and existing[1] == REJECTED and existing[2] in _RETRYABLE:
                    db.execute("DELETE FROM children WHERE idem_key=?", (key,))
                    existing = None  # zero-cost rejection: re-evaluate under current state
                if existing is not None:
                    ids.append(str(existing[0]))
                    replayed += 1
                    continue
                base = self._base_record(plan, proposal, index)
                if invalid_root:
                    cid = self._insert(
                        db, key, plan, REJECTED, "root_invalidated", 0, self._bare(base)
                    )
                elif seed_block is not None:
                    extra: Json = {"request": seed_block[1]} if seed_block[1] else {}
                    cid = self._insert(
                        db, key, plan, REJECTED, seed_block[0], 0, {**self._bare(base), **extra}
                    )
                else:
                    reason, request, record = self._static_checks(db, plan, proposal, base)
                    cost = proposal.cost if reason is None else 0
                    if reason is None and (
                        cost < 0 or self._spent(db, plan.grant.grant_ref) + cost > plan.grant.budget
                    ):
                        reason, request, cost, stopped = "authorization_exhausted", None, 0, True
                    if request:
                        record = {**record, "request": request}
                    cid = self._insert(
                        db,
                        key,
                        plan,
                        HELD if reason is None else REJECTED,
                        reason or "independent_validation_absent",
                        cost,
                        record,
                    )
                ids.append(cid)
                created += 1
        return SubmitResult(tuple(ids), created, replayed, stopped, not_attempted)

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
                plan.generator.ref,
                plan.generator.revision,
                plan.mode,
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
            "policy_revision",
            "split_digest",
            "recipe",
            "generator",
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
            "split_digest": plan.seal.digest,
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
        db: sqlite3.Connection, plan: GenerationPlan, proposal: ProposedChild, base: Json
    ) -> tuple[str | None, Json | None, Json]:
        root, policy, gen = plan.root, plan.policy, plan.generator
        recipe = plan.recipes.get(proposal.recipe_id)
        if gen.external and gen.ref not in plan.grant.approved_external_teachers:
            return "unapproved_external_teacher", None, base
        if recipe is None:
            return "unapproved_recipe", None, base
        if plan.mode == "training" and not (plan.fit_gate and plan.fit_gate.open):
            return "fit_gate_closed", None, base
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
        # Near-copies of the root or of any live sibling under the same root are one example.
        if _normal(child) == _normal(root.fields):
            return "near_duplicate", None, base
        marks = ",".join("?" * (len(_LIVE) + 1))
        for (rec_json,) in db.execute(
            f"SELECT record FROM children WHERE root_ref=? AND disposition IN ({marks})",
            (root.root_ref, *_LIVE, QUARANTINED),
        ):
            if _normal(json.loads(rec_json).get("fields", {})) == _normal(child):
                return "near_duplicate", None, base
        marks = ",".join("?" * len(_LIVE))
        live = db.execute(
            f"SELECT COUNT(*) FROM children WHERE root_ref=? AND disposition IN ({marks})",
            (root.root_ref, *_LIVE),
        ).fetchone()[0]
        if live >= plan.max_children_per_root:
            return "root_cap_exceeded", None, base
        return None, None, base

    def validate(
        self, child_id: str, decision: ValidatorDecision | None, *, base_target: str
    ) -> str:
        """Apply an independent validator decision; return the child's resulting disposition.

        Serves first calls and late callbacks alike. An invalidated child or a retracted root
        can never be admitted by a callback.
        """
        with self._tx() as db:
            row = db.execute(
                "SELECT record, root_ref, mode FROM children WHERE child_id=?", (child_id,)
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
            if not decision.validator_ref or decision.validator_ref == record["generator"]["ref"]:
                raise TrainingError("self_validation_rejected")
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
            elif kind in INVARIANT_KINDS and decision.label != base_target:
                new, why = QUARANTINED, "invariant_label_changed"
            elif row[2] == "preview":
                new, why = PREVIEW_VALID, "preview_only"
            else:
                new, why = ADMITTED, "validated"
            self._update(db, child_id, new, why, record)
            if new == ADMITTED:
                self._reweigh(db, str(row[1]))
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
                        child["fields"],
                        tuple(child["changed_fields"]),
                    )
                )
            self.validate(child["child_id"], decision, base_target=plan.root.target)
        return result

    @staticmethod
    def _reweigh(db: sqlite3.Connection, root_ref: str) -> None:
        """Split a root's synthetic influence cap (one real-example unit) across its siblings."""
        rows = db.execute(
            "SELECT child_id, record FROM children WHERE root_ref=? AND disposition=? ORDER BY seq",
            (root_ref, ADMITTED),
        ).fetchall()
        for child_id, rec_json in rows:
            rec: Json = json.loads(rec_json)
            rec["weight"] = str(Fraction(1, len(rows)))
            rec["sibling_count"] = len(rows)
            db.execute(
                "UPDATE children SET record=? WHERE child_id=?",
                (json.dumps(rec, sort_keys=True), child_id),
            )

    def retract_root(self, root_ref: str, family_id: str, reason: str) -> tuple[str, ...]:
        """Reopen/revoke a parent: invalidate every descendant; nothing can re-admit them."""
        identifier(root_ref)
        identifier(family_id)
        identifier(reason)
        with self._tx() as db:
            db.execute(
                "INSERT INTO roots VALUES (?,?,'retracted',?) ON CONFLICT(root_ref)"
                " DO UPDATE SET state='retracted', reason=excluded.reason",
                (root_ref, family_id, reason),
            )
            prior: list[str] = []
            rows = db.execute(
                "SELECT child_id, record FROM children WHERE root_ref=? AND disposition<>?"
                " ORDER BY seq",
                (root_ref, INVALIDATED),
            ).fetchall()
            for child_id, rec_json in rows:
                rec: Json = json.loads(rec_json)
                rec["prior_disposition"] = rec["disposition"]
                rec["weight"] = None
                self._update(db, child_id, INVALIDATED, f"root_{reason}", rec)
                prior.append(str(child_id))
            return tuple(prior)

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
        real_roots: Sequence[SeedRoot],
        coverage_required_groups: int | None = None,
    ) -> Json:
        """Sealed admission/rejection manifest. Real counts come only from real roots."""
        rows = self.children()
        with self._tx() as db:
            retracted = {
                str(r[0]) for r in db.execute("SELECT root_ref FROM roots WHERE state<>'live'")
            }
        live_roots = [
            r
            for r in real_roots
            if r.root_ref not in retracted and r.split == "training" and r.target_verified
        ]
        by_state = Counter(r["disposition"] for r in rows)
        reasons = Counter(
            r["reason"] for r in rows if r["disposition"] in {REJECTED, QUARANTINED, INVALIDATED}
        )
        siblings = Counter(r["root_ref"] for r in rows if r["disposition"] == ADMITTED)
        families = sorted({r.family_id for r in live_roots})
        body: Json = {
            "schema_version": MANIFEST_SCHEMA,
            "mode": mode,
            "policy_revision": policy_revision,
            "split_digest": seal.digest,
            "generated_count": len(rows),
            "admitted_count": by_state[ADMITTED],
            "preview_valid_count": by_state[PREVIEW_VALID],
            "held_count": by_state[HELD],
            "quarantined_count": by_state[QUARANTINED],
            "rejected_count": by_state[REJECTED],
            "invalidated_count": by_state[INVALIDATED],
            "exclusion_reasons": dict(sorted(reasons.items())),
            "observed_mistake_count": len({r.root_ref for r in live_roots}),
            "independent_family_count": len(families),
            "independent_families": families,
            "children_in_independent_count": 0,
            "root_sibling_counts": dict(sorted(siblings.items())),
            "final_weights": {
                r["child_id"]: r["weight"] for r in rows if r["disposition"] == ADMITTED
            },
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
        body["manifest_digest"] = digest(body)
        body["sealed"] = True
        return body
