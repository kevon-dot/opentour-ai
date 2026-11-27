"""Route a recurring error to the lowest-cost credible repair on development evidence only.

Four outcomes exist: ``keep_incumbent``, ``simple_repair``, ``request_evidence`` and
``fit_eligible``. The router never receives protected rows, scores or labels, never
reserves a fit and never starts one; ``fit_eligible`` is a status that references a
verified policy, not an authorization. One durable evaluation is keyed by the accepted
run, the worker-local snapshot reference and the standing mandate revision, so a replayed
trigger or a restarted worker returns the stored receipt instead of a second evaluation.

Limits of what this proves: inputs are the caller's already-admitted development cases.
Nothing here reads a provider, a live workplace or a protected store.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import comparison_plan, simple_repair
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.simple_repair import RepairError
from opentour_cell.pilot.training import _check_file

Json = dict[str, Any]
SCHEMA = "opentour.pilot.repair-routing.v1"
OUTCOMES = ("keep_incumbent", "simple_repair", "request_evidence", "fit_eligible")
_ORIGINS_COUNTED = ("live",)
_DEV = "development"


class RoutingError(ValueError):
    """Fail-closed refusal with a fixed reason code; never carries row content."""

    def __init__(self, code: str) -> None:
        self.code = code.upper()
        super().__init__(self.code)


def _need(condition: object, code: str) -> None:
    if not condition:
        raise RoutingError(code)


def _ident(value: Any) -> str:
    try:
        return simple_repair.identifier(value)
    except RepairError:
        raise RoutingError("invalid_scope_or_identifier") from None


def _int(value: Any, low: int, high: int, code: str) -> int:
    _need(type(value) is int and low <= value <= high, code)
    return int(value)


@dataclass(frozen=True)
class RoutingTerms:
    """Thresholds the owner pinned before any protected set was viewed."""

    error_family: tuple[str, str]
    min_independent_groups: int
    recurrence_window_seconds: int
    material_residual_cases: int
    min_coverage_bps: int
    max_wrong_automatic_bps: int
    critical_slices: tuple[str, ...]
    min_development_cases: int
    min_groups_per_label: int
    documentation_revision: str
    prefer_simpler_on_tie: bool = True

    def validate(self) -> RoutingTerms:
        _need(len(self.error_family) == 2, "invalid_error_family")
        for label in self.error_family:
            _ident(label)
        _int(self.min_independent_groups, 2, 5000, "invalid_group_minimum")
        _int(self.recurrence_window_seconds, 1, 10**9, "invalid_recurrence_window")
        _int(self.material_residual_cases, 1, 5000, "invalid_material_residual")
        _int(self.min_coverage_bps, 0, 10_000, "invalid_coverage")
        _int(self.max_wrong_automatic_bps, 0, 10_000, "invalid_wrong_automatic_ceiling")
        _int(self.min_development_cases, 1, 5000, "invalid_development_minimum")
        _int(self.min_groups_per_label, 1, 5000, "invalid_label_group_minimum")
        _need(len(self.critical_slices) <= 32, "invalid_critical_slices")
        _need(
            len(set(self.critical_slices)) == len(self.critical_slices), "invalid_critical_slices"
        )
        for item in self.critical_slices:
            _need(":" in item, "invalid_critical_slices")
            _ident(item)
        _ident(self.documentation_revision)
        _need(self.prefer_simpler_on_tie is True, "simpler_tie_preference_required")
        return self

    def as_json(self) -> Json:
        return {
            "error_family": list(self.error_family),
            "min_independent_groups": self.min_independent_groups,
            "recurrence_window_seconds": self.recurrence_window_seconds,
            "material_residual_cases": self.material_residual_cases,
            "min_coverage_bps": self.min_coverage_bps,
            "max_wrong_automatic_bps": self.max_wrong_automatic_bps,
            "critical_slices": sorted(self.critical_slices),
            "min_development_cases": self.min_development_cases,
            "min_groups_per_label": self.min_groups_per_label,
            "documentation_revision": self.documentation_revision,
            "prefer_simpler_on_tie": self.prefer_simpler_on_tie,
        }

    @property
    def revision(self) -> str:
        return digest(self.as_json())

    def comparison_terms(self) -> Json:
        """The subset the shared comparison code understands."""
        return {
            "minimum_coverage_bps": self.min_coverage_bps,
            "error_ceiling_bps": self.max_wrong_automatic_bps,
            "minimum_development_cases": self.min_development_cases,
            "minimum_residual_errors": self.min_independent_groups,
        }


@dataclass(frozen=True)
class DevelopmentCase:
    case_id: str
    entity_id: str
    family_id: str
    decided_at: int
    features: Mapping[str, Any]
    incumbent: Mapping[str, Any]
    reference: str | None
    split: str = _DEV
    origin: str = "live"
    context_state: str = "observed"


@dataclass(frozen=True)
class AdmittedSnapshot:
    organization: str
    workflow_ref: str
    run_id: str
    snapshot_ref: str
    evidence_revision: str
    documentation_revision: str
    as_of: int


@dataclass(frozen=True)
class MandateView:
    """Resolved standing-mandate facts. Carries no authority by itself."""

    mandate_ref: str
    mandate_revision: str
    withdrawn: bool
    expired: bool
    uses: frozenset[str]
    max_fits: int
    reserved_fits: int
    max_seconds_per_fit: int
    max_total_seconds: int
    reserved_seconds: int

    @classmethod
    def from_inspection(cls, details: Mapping[str, Any]) -> MandateView:
        """Adapt ``LearningMandates.inspect`` output."""
        scope = details["scope"]
        return cls(
            details["mandate_id"],
            details["mandate_revision"],
            bool(details["withdrawn"]),
            bool(details["expired"]),
            frozenset(scope["uses"]),
            scope["max_fits"],
            details["reserved_fits"],
            scope["max_seconds_per_fit"],
            scope["max_total_seconds"],
            details["reserved_seconds"],
        )

    @property
    def active(self) -> bool:
        return not (self.withdrawn or self.expired)

    @property
    def has_budget(self) -> bool:
        return (
            self.reserved_fits < self.max_fits
            and self.reserved_seconds + self.max_seconds_per_fit <= self.max_total_seconds
        )


@dataclass(frozen=True)
class SignedPolicy:
    policy_ref: str
    snapshot_ref: str
    terms_revision: str
    signature: str


class PolicyVerifier(Protocol):
    def verify(self, policy: SignedPolicy) -> bool:
        """True only if the signature covers this policy's snapshot and terms revision."""
        ...


@dataclass(frozen=True)
class Recipe:
    recipe_id: str
    version: int
    models: frozenset[str]
    tasks: frozenset[str]
    split: str
    metric: str
    direction: str
    allowed_params: tuple[str, ...]
    trial_limit: int
    step_limit: int
    stopping_rule: str
    est_seconds_per_trial: int
    paid_probe: bool = False

    def budget_estimate(self) -> int:
        # Conservative: 50 percent above the declared per-trial estimate, every allowed trial.
        return (self.est_seconds_per_trial * self.trial_limit * 3 + 1) // 2


@dataclass(frozen=True)
class ModelTarget:
    model_ref: str
    task: str
    capability_receipt_ref: str | None


@dataclass(frozen=True)
class ComparisonScheduling:
    intake_revision: str
    protected_ids: tuple[str, ...]
    incumbent_revision: str
    audience: str


@dataclass(frozen=True)
class RoutingRequest:
    terms: RoutingTerms
    policy: SignedPolicy
    verifier: PolicyVerifier
    snapshot: AdmittedSnapshot
    mandate: MandateView
    cases: Sequence[DevelopmentCase]
    rule: Json
    recipes: Sequence[Recipe] = ()
    model: ModelTarget | None = None
    scheduling: ComparisonScheduling | None = None
    augmentation: str = "not_enabled"


def _root(case_id: str) -> str:
    return "root-" + digest(["case", case_id])[:40]


def _components(cases: Sequence[DevelopmentCase]) -> dict[str, str]:
    """Cases sharing an entity or a family are one independent group (union-find)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for case in cases:
        a, b = find("e:" + case.entity_id), find("f:" + case.family_id)
        if a != b:
            parent[max(a, b)] = min(a, b)
    return {c.case_id: "grp-" + digest(find("e:" + c.entity_id))[:24] for c in cases}


def _validate_cases(cases: Sequence[DevelopmentCase]) -> None:
    _need(len(cases) <= simple_repair.MAX_ROWS, "too_many_cases")
    seen: set[str] = set()
    for c in cases:
        _need(c.split == _DEV, "protected_row_supplied")
        _ident(c.case_id)
        _ident(c.entity_id)
        _ident(c.family_id)
        _need(c.case_id not in seen, "duplicate_case")
        seen.add(c.case_id)
        _need(c.context_state in ("observed", "missing"), "invalid_context_state")
        _need(c.origin in ("live", "starter", "generated_child"), "invalid_origin")
        _need(type(c.decided_at) is int and c.decided_at >= 0, "invalid_decision_time")


def _input_revision(request: RoutingRequest) -> str:
    return digest(
        [
            request.terms.revision,
            request.policy.policy_ref,
            request.snapshot.evidence_revision,
            request.snapshot.documentation_revision,
            request.rule["revision"],
            sorted(
                [
                    c.case_id,
                    c.entity_id,
                    c.family_id,
                    c.decided_at,
                    dict(c.features),
                    dict(c.incumbent),
                    c.reference,
                    c.origin,
                    c.context_state,
                ]
                for c in request.cases
            ),
        ]
    )


def evaluation_key(request: RoutingRequest) -> str:
    s, m = request.snapshot, request.mandate
    return digest([s.organization, s.run_id, s.snapshot_ref, m.mandate_ref, m.mandate_revision])


def _repair_wrong(rule: Json, case: DevelopmentCase) -> tuple[Json, bool]:
    out = simple_repair.apply_frozen_rule(rule, dict(case.features), dict(case.incumbent))
    inc, ref = case.incumbent, case.reference
    residual = (out["status"] == "automatic" and out["label"] != ref) or (
        out["status"] != "automatic" and inc["status"] == "automatic" and inc["label"] != ref
    )
    return out, residual


def _slice_failure(rule: Json, terms: RoutingTerms, cases: Sequence[DevelopmentCase]) -> str | None:
    feature = rule["feature"]
    for item in sorted(terms.critical_slices):
        name, value = item.split(":", 1)
        _need(name == feature, "critical_slice_outside_permitted_feature")
        members = [c for c in cases if c.features.get(name) == value]
        if not members:
            return "critical_slice_unobserved"
        wrong = regress = 0
        for c in members:
            out, _ = _repair_wrong(rule, c)
            bad = out["status"] == "automatic" and out["label"] != c.reference
            wrong += bad
            was = c.incumbent["status"] == "automatic" and c.incumbent["label"] == c.reference
            regress += bad and was
        if regress or wrong * 10_000 > len(members) * terms.max_wrong_automatic_bps:
            return "critical_slice_failed"
    return None


def select_recipe(
    recipes: Sequence[Recipe], model: ModelTarget | None, mandate: MandateView
) -> Json:
    """Deterministic lowest-estimate selection. Every alternative is logged with its reason."""
    rejected: list[Json] = []
    viable: list[Recipe] = []
    for r in sorted(recipes, key=lambda r: (r.recipe_id, r.version)):
        why = None
        if model is None or model.model_ref not in r.models:
            why = "unsupported_model"
        elif model.task not in r.tasks:
            why = "unsupported_task"
        elif r.paid_probe:
            why = "paid_probe_requires_separate_authority"
        elif mandate.max_fits == 1 and (r.trial_limit > 1 or r.allowed_params):
            why = "parameter_sweep_exceeds_owner_fit_limit"
        elif r.budget_estimate() > mandate.max_seconds_per_fit:
            why = "estimate_exceeds_owner_budget"
        if why:
            rejected.append({"recipe": [r.recipe_id, r.version], "reason": why})
        else:
            viable.append(r)
    viable.sort(key=lambda r: (r.budget_estimate(), r.trial_limit, r.recipe_id, r.version))
    chosen = viable[0] if viable else None
    for r in viable[1:]:
        rejected.append({"recipe": [r.recipe_id, r.version], "reason": "higher_estimate"})
    frozen = None
    if chosen is not None and model is not None:
        frozen = {
            "recipe": [chosen.recipe_id, chosen.version],
            "model_ref": model.model_ref,
            "task": model.task,
            "validation_split": chosen.split,
            "metric": chosen.metric,
            "direction": chosen.direction,
            "allowed_params": list(chosen.allowed_params),
            "trial_limit": chosen.trial_limit,
            "step_limit": chosen.step_limit,
            "stopping_rule": chosen.stopping_rule,
            "capability_receipt_ref": model.capability_receipt_ref,
            "budget_estimate_seconds": chosen.budget_estimate(),
            "protected_access": "none_before_freeze",
        }
    return {
        "selected": frozen,
        "rejected": sorted(rejected, key=lambda x: (x["recipe"], x["reason"])),
    }


def decide(request: RoutingRequest) -> Json:
    """Pure, deterministic routing decision. No clock, store or protected input."""
    terms, snapshot, mandate = request.terms.validate(), request.snapshot, request.mandate
    for item in (snapshot.organization, snapshot.workflow_ref, snapshot.run_id):
        _ident(item)
    _ident(snapshot.snapshot_ref)
    _need(request.policy.snapshot_ref == snapshot.snapshot_ref, "policy_snapshot_mismatch")
    _need(request.policy.terms_revision == terms.revision, "policy_terms_mismatch")
    _need(mandate.active, "standing_mandate_unavailable")
    _need("evaluate" in mandate.uses, "evaluation_not_authorized")
    _need(request.augmentation == "not_enabled", "augmentation_not_supported_on_this_branch")
    _validate_cases(request.cases)
    rule = simple_repair.validate_rule(request.rule)

    live = [c for c in request.cases if c.origin in _ORIGINS_COUNTED]
    excluded_origin = len(request.cases) - len(live)
    low = snapshot.as_of - terms.recurrence_window_seconds
    window = [c for c in live if low <= c.decided_at <= snapshot.as_of]
    groups = _components(window)
    family = list(terms.error_family)

    def hold(reason: str, missing: Sequence[str] = ()) -> Json:
        return _body(request, "request_evidence", reason, rule, window, groups, None, missing)

    # Distinct, unusable-evidence outcomes. None of them is a statement about the repair.
    if snapshot.documentation_revision != terms.documentation_revision:
        return hold("stale_documentation", ["refresh_workflow_documentation"])
    feature = rule["feature"]
    if any(c.context_state != "observed" or c.features.get(feature) is None for c in window):
        return hold("missing_decision_time_context", ["provide_decision_time_input"])
    by_entity: defaultdict[str, set[str | None]] = defaultdict(set)
    for c in window:
        by_entity[digest([c.entity_id, dict(c.features)])].add(c.reference)
    if any(len(v) > 1 for v in by_entity.values()):
        return hold("contradictory_labels", ["resolve_contradictory_labels"])
    if any(c.reference is None for c in window):
        return hold("unusable_labels", ["supply_verified_labels"])
    if not any(c.incumbent.get("label") == family[0] and c.reference == family[1] for c in window):
        return hold("error_family_not_observed", ["supply_independent_evidence"])

    rows = [
        {
            "case_id": c.case_id,
            "group_id": groups[c.case_id],
            "features": dict(c.features),
            "incumbent": simple_repair.decision(dict(c.incumbent)),
            "reference": c.reference,
        }
        for c in window
    ]
    # Rights are judged below, never by the shared comparison, so a hold cannot hide a route.
    assessed = simple_repair.assess_development(
        rows,
        rule,
        terms=terms.comparison_terms() | {"critical_slices": sorted(terms.critical_slices)},
        model_authorized=False,
        compute_authorized=False,
    )
    residual = [c for c in window if c.reference == family[1] and _repair_wrong(rule, c)[1]]
    residual = [c for c in residual if c.incumbent.get("label") == family[0]]
    res_groups = {groups[c.case_id] for c in residual}
    route = assessed["route"]
    ctx: Json = {
        "assessed": assessed,
        "residual": residual,
        "residual_groups": len(res_groups),
        "excluded_origin": excluded_origin,
    }
    if route == "no_change":
        return _body(
            request,
            "keep_incumbent",
            "incumbent_tied_prefer_simpler",
            rule,
            window,
            groups,
            ctx,
            [],
        )
    if route == "simple":
        failed = _slice_failure(rule, terms, window)
        if failed:
            return _body(
                request,
                "request_evidence",
                failed,
                rule,
                window,
                groups,
                ctx,
                ["supply_independent_evidence"],
            )
        return _body(
            request,
            "simple_repair",
            "simple_repair_meets_development_limits",
            rule,
            window,
            groups,
            ctx,
            [],
        )
    if assessed["reason"] == "insufficient_independent_development_evidence":
        return _body(
            request,
            "request_evidence",
            assessed["reason"],
            rule,
            window,
            groups,
            ctx,
            ["supply_independent_evidence"],
        )
    # No credible simple repair. Only an independent, material residual may become fit-eligible.
    if len(residual) < terms.material_residual_cases:
        return _body(
            request,
            "request_evidence",
            "residual_below_material_threshold",
            rule,
            window,
            groups,
            ctx,
            ["supply_independent_evidence"],
        )
    if len(res_groups) < terms.min_independent_groups:
        return _body(
            request,
            "request_evidence",
            "residual_not_independent",
            rule,
            window,
            groups,
            ctx,
            ["supply_independent_evidence"],
        )
    label_groups: defaultdict[str, set[str]] = defaultdict(set)
    for c in window:
        label_groups[str(c.reference)].add(groups[c.case_id])
    if any(len(g) < terms.min_groups_per_label for g in label_groups.values()):
        return _body(
            request,
            "request_evidence",
            "label_imbalance_inadequate_group_support",
            rule,
            window,
            groups,
            ctx,
            ["supply_independent_evidence"],
        )
    gate: str | None = None
    if "train" not in mandate.uses:
        gate = "training_rights_required"
    elif not mandate.has_budget:
        gate = "training_budget_required"
    elif request.model is None or request.model.capability_receipt_ref is None:
        gate = "qualified_model_required"
    selection = select_recipe(request.recipes, request.model, mandate)
    ctx["selection"] = selection
    if gate is None and selection["selected"] is None:
        gate = "unsupported_recipe"
    if gate is None and not request.verifier.verify(request.policy):
        gate = "signed_policy_required"
    if gate is not None:
        return _body(request, "request_evidence", gate, rule, window, groups, ctx, [gate])
    return _body(
        request, "fit_eligible", "independent_material_residual", rule, window, groups, ctx, []
    )


def _body(
    request: RoutingRequest,
    outcome: str,
    reason: str,
    rule: Json,
    window: Sequence[DevelopmentCase],
    groups: Mapping[str, str],
    ctx: Json | None,
    missing: Sequence[str],
) -> Json:
    s, m, terms = request.snapshot, request.mandate, request.terms
    residual = ctx["residual"] if ctx else []
    plan: str | None = None
    if outcome == "simple_repair" and ctx is not None and request.scheduling is not None:
        sch = request.scheduling
        plan = comparison_plan.freeze_plan(
            organization=s.organization,
            engagement=s.workflow_ref,
            audience=sch.audience,
            intake_revision=sch.intake_revision,
            protected_ids=list(sch.protected_ids),
            incumbent_revision=sch.incumbent_revision,
            rule=rule,
            development_decision=ctx["assessed"],
            terms=terms.comparison_terms() | {"critical_slices": sorted(terms.critical_slices)},
        )["revision"]
    selection = ctx.get("selection") if ctx else None
    body: Json = {
        "schema_version": SCHEMA,
        "outcome": outcome,
        "reason_code": reason,
        "next_evidence": list(dict.fromkeys(missing)),
        "organization": s.organization,
        "workflow_ref": s.workflow_ref,
        "run_id": s.run_id,
        "snapshot_ref": s.snapshot_ref,
        "evidence_revision": s.evidence_revision,
        "documentation_revision": s.documentation_revision,
        "mandate_ref": m.mandate_ref,
        "mandate_revision": m.mandate_revision,
        "policy_ref": request.policy.policy_ref,
        "terms_revision": terms.revision,
        "terms": terms.as_json(),
        "rule_revision": rule["revision"],
        "development_cases": len(window),
        "independent_groups": len(set(groups.values())),
        "excluded_non_live_cases": ctx["excluded_origin"] if ctx else None,
        "residual_roots": sorted(_root(c.case_id) for c in residual),
        "independent_residual_groups": len({groups[c.case_id] for c in residual}),
        "development_decision_revision": ctx["assessed"]["revision"] if ctx else None,
        "comparison_plan_revision": plan,
        "comparison_scheduled": plan is not None,
        "frozen_arm": rule["revision"] if outcome == "simple_repair" else None,
        "recipe_selection": selection,
        "train_eligible_policy_ref": request.policy.policy_ref
        if outcome == "fit_eligible"
        else None,
        "augmentation_state": request.augmentation,
        "fit_starts": 0,
        "reservations": 0,
        "protected_reads": 0,
    }
    ident = digest(body)
    return dict(body, revision=ident)


def _readiness_handoff(receipt: Json) -> Json:
    """The exact facts OPE-542 persists; this module does not own the readiness transition."""
    return {
        "evaluation_id": receipt["evaluation_id"],
        "outcome": receipt["outcome"],
        "reason_code": receipt["reason_code"],
        "independent_residual_groups": receipt["independent_residual_groups"],
        "owner_minimum": receipt["terms"]["min_independent_groups"],
        "residual_roots": list(receipt["residual_roots"]),
        "evidence_revisions": {
            "evidence_revision": receipt["evidence_revision"],
            "documentation_revision": receipt["documentation_revision"],
            "snapshot_ref": receipt["snapshot_ref"],
            "mandate_revision": receipt["mandate_revision"],
        },
        "next_evidence": list(receipt["next_evidence"]),
        "fit_starts": 0,
        "reservations": 0,
        "augmentation_state": receipt["augmentation_state"],
    }


class RepairRoutingStore:
    """One durable, immutable evaluation per accepted run, snapshot and mandate revision."""

    def __init__(self, directory: Path) -> None:
        _need(directory.is_absolute() and directory.resolve() == directory, "private_dir_required")
        self.path = directory / "repair-routing.sqlite3"

    def _connect(self) -> sqlite3.Connection:
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            pass
        else:
            os.close(fd)
        _check_file(self.path)
        db = sqlite3.connect(self.path, isolation_level=None, timeout=30)
        db.execute(
            "CREATE TABLE IF NOT EXISTS evaluations(key TEXT PRIMARY KEY, evaluation_id TEXT "
            "UNIQUE NOT NULL, input_revision TEXT NOT NULL, body TEXT NOT NULL)"
        )
        return db

    def evaluate(self, request: RoutingRequest) -> Json:
        key, inputs = evaluation_key(request), _input_revision(request)
        with closing(self._connect()) as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute(
                    "SELECT input_revision, body FROM evaluations WHERE key=?", (key,)
                ).fetchone()
                if row is not None:
                    _need(row[0] == inputs, "evaluation_conflict")
                    db.execute("COMMIT")
                    import json

                    return dict(json.loads(row[1]), replayed=True)
                receipt = decide(request)
                receipt = dict(receipt, evaluation_id="eval-" + key[:40])
                receipt["readiness_handoff"] = _readiness_handoff(receipt)
                db.execute(
                    "INSERT INTO evaluations VALUES(?,?,?,?)",
                    (key, receipt["evaluation_id"], inputs, canonical(receipt)),
                )
                db.execute("COMMIT")
            except BaseException:
                db.execute("ROLLBACK")
                raise
        return dict(receipt, replayed=False)

    def read(self, evaluation_id: str) -> Json | None:
        import json

        with closing(self._connect()) as db:
            row = db.execute(
                "SELECT body FROM evaluations WHERE evaluation_id=?", (evaluation_id,)
            ).fetchone()
        return None if row is None else dict(json.loads(row[0]))

    def count(self) -> int:
        with closing(self._connect()) as db:
            return int(db.execute("SELECT count(*) FROM evaluations").fetchone()[0])

    def current_eligibility(
        self, evaluation_id: str, *, snapshot: AdmittedSnapshot, mandate: MandateView
    ) -> Json:
        """Eligibility is recomputed against today's evidence and mandate, never cached."""
        receipt = self.read(evaluation_id)
        _need(receipt is not None, "evaluation_unavailable")
        assert receipt is not None
        reason = None
        if receipt["outcome"] != "fit_eligible":
            reason = "not_fit_eligible"
        elif not mandate.active:
            reason = "mandate_withdrawn_or_expired"
        elif mandate.mandate_revision != receipt["mandate_revision"]:
            reason = "mandate_revision_changed"
        elif (
            snapshot.evidence_revision != receipt["evidence_revision"]
            or snapshot.snapshot_ref != receipt["snapshot_ref"]
        ):
            reason = "evidence_revision_changed"
        elif not mandate.has_budget:
            reason = "training_budget_required"
        return {
            "evaluation_id": evaluation_id,
            "eligible": reason is None,
            "reason_code": reason or "fit_eligible",
            "reservations": 0,
            "automatic_promotion": False,
        }
