"""Incremental tuning benefit on untouched real cases (OPE-517), pure CPU.

What this module does: freezes a pre-registered protocol and the untouched case set, seals
both arms' predictions before any protected label is opened, then scores real-only (A)
against real-plus-synthetic (B) on the same untouched groups and returns one of
measured_benefit / no_material_benefit / regression / inconclusive / unrun. Refusing to
report a benefit when the conditions are not met is as much the output as computing one.

What is SIMULATED: every case, arm, prediction, key, child record and cost is a supplied
generated sentinel. Nothing here fits, calls a provider, reads a customer record or opens a
real protected label. Child records are plain mappings in the OPE-516 variant-record shape
(``disposition``, ``root_ref``, ``family_id`` ...); this module never imports OPE-516.

What is NOT proven: tests prove software behaviour only. No customer-benefit, provider,
label-efficiency or population-accuracy evidence exists here; a fixture result stays
``evidence_tier == "generated"`` and ``claim_scope == "implementation_only"``.
No significance claim is made: the family interval is descriptive uncertainty, and the
spec prescribes no method, so the conservative gate below is a recorded open product input.

Dedup is exact normalized token-multiset equality only: homoglyphs and padded paraphrases
pass, so dedup is NOT the independence control (recorded gap: paraphrase-distance dedup).
Independent families are derived here from the frozen cases, never taken from a variant
manifest's ``independent_family_count`` (reconcile once OPE-516's corrected count lands).
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Literal

from opentour_training.evaluation.statistics_kernel import independent_family_interval

PROTOCOL_SCHEMA = "opentour.tuning-benefit-protocol.v1"
ARM_SEAL_SCHEMA = "opentour.tuning-benefit-arm-seal.v1"
RESULT_SCHEMA = "opentour.tuning-benefit-result.v1"
LABEL_SCHEMA = "opentour.label-efficiency.v1"
UNCERTAINTY_RULE = "family_hoeffding_two_sided_lower_gt_zero_v1"
UNPROVEN = "Augmentation benefit unproven"
NOT_MEASURED = "Label savings not measured"
REAL_ONLY = "real_only"
AUGMENTED = "real_plus_synthetic"
SYNTHETIC_ORIGIN = "synthetic"
REAL_ORIGIN = "real_untouched"
UNSATISFIED_EXTERNAL_GATES: tuple[str, ...] = (
    "authorized_real_heldout_workplace_cases_unavailable",
    "eligible_customer_model_artifacts_unavailable",
    "independent_exact_commit_review_of_result_pending",
    "ope_516_family_count_reconciliation_pending",
    "ope_526_audit_verdict_pending",
    "ope_528_independent_recomputation_pending",
    "uncertainty_method_not_prescribed_by_spec",
)

Verdict = Literal["measured_benefit", "no_material_benefit", "regression", "inconclusive", "unrun"]
Stage = Literal["variant_generation", "threshold_selection", "model_choice", "fitting"]
STAGES: frozenset[str] = frozenset(
    {"variant_generation", "threshold_selection", "model_choice", "fitting"}
)


class TuningBenefitError(ValueError):
    """Closed refusal with a stable lowercase ``code`` (matches the pilot convention)."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _need(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise TuningBenefitError(code, detail)


def _ident(value: object, code: str) -> str:
    _need(type(value) is str and bool(value) and len(value) <= 256, code)
    assert isinstance(value, str)
    return value


def _count(value: object, code: str) -> int:
    _need(type(value) is int and value >= 0, code)
    assert isinstance(value, int)
    return value


def _unit(value: object, code: str) -> float:
    _need(type(value) in (int, float) and 0.0 <= float(value) <= 1.0, code)  # type: ignore[arg-type]
    return float(value)  # type: ignore[arg-type]


_PUNCT_OR_FORMAT = re.compile(r"[\W_]+", re.UNICODE)


def fingerprint(text: str) -> str:
    """Accent/case/zero-width/punctuation/order-insensitive token multiset digest.

    Not covered: cross-script homoglyphs and true paraphrase similarity.
    """
    folded = unicodedata.normalize("NFKD", text).casefold()
    kept = "".join(
        ch
        for ch in folded
        if not unicodedata.combining(ch) and unicodedata.category(ch) not in {"Cf", "Mn"}
    )
    tokens = sorted(t for t in _PUNCT_OR_FORMAT.split(kept) if t)
    return hashlib.sha256("\x1f".join(tokens).encode()).hexdigest()


# ---------------------------------------------------------------- inputs


@dataclass(frozen=True)
class CaseRef:
    """Decision-time facts only. There is deliberately no label field on a case."""

    case_id: str
    family_id: str
    text: str
    slices: tuple[str, ...] = ()
    resolved: bool = True


@dataclass(frozen=True)
class SelectionUse:
    """One recorded use of data to build, select or tune: it may never be an untouched case."""

    stage: str
    case_ids: tuple[str, ...] = ()
    family_ids: tuple[str, ...] = ()
    used_protected_results: bool = False


@dataclass(frozen=True)
class Preregistration:
    protocol_id: str
    model_ref: str
    base_revision: str
    task_ref: str
    scorer_version: str
    operating_point_ref: str
    recipe_id: str
    seed: int
    fit_steps: int
    critical_slices: tuple[str, ...]
    practical_threshold: float
    critical_slice_tolerance: float
    min_slice_cases: int
    min_independent_families: int
    alpha: float
    fits_authorized: int
    spend_cap: int
    reference_arms: tuple[str, ...] = ()
    uncertainty_rule: str = UNCERTAINTY_RULE
    evidence_tier: str = "generated"


@dataclass(frozen=True)
class FrozenProtocol:
    body: Mapping[str, Any]
    protocol_digest: str

    @property
    def eligible(self) -> tuple[str, ...]:
        return tuple(self.body["untouched"]["eligible"])

    @property
    def group_of(self) -> dict[str, str]:
        return dict(self.body["untouched"]["group_of"])

    @property
    def slices_of(self) -> dict[str, list[str]]:
        return {k: list(v) for k, v in self.body["untouched"]["slices_of"].items()}

    @property
    def admitted_children(self) -> frozenset[str]:
        return frozenset(self.body["variants"]["admitted_child_ids"])


@dataclass(frozen=True)
class ArmRecord:
    arm_id: str
    status: str  # completed | interrupted | revoked
    model_ref: str
    base_revision: str
    recipe_id: str
    seed: int
    steps: int
    real_row_ids: tuple[str, ...]
    synthetic_child_ids: tuple[str, ...]
    fit_cost: int
    generation_cost: int
    predictions: Mapping[str, str]
    declared_case_ids: tuple[str, ...]
    scored_origin: str = REAL_ORIGIN


@dataclass(frozen=True)
class ProtectedKey:
    """Evaluator-only. ``None`` means the case is unresolved and stays outside the denominator."""

    answers: Mapping[str, str | None]


@dataclass(frozen=True)
class ArmSeal:
    body: Mapping[str, Any]
    seal_digest: str


# ---------------------------------------------------------------- freeze


def _validate_prereg(p: Preregistration) -> None:
    for name in (
        "protocol_id model_ref base_revision task_ref scorer_version operating_point_ref recipe_id"
    ).split():
        _ident(getattr(p, name), f"prereg_{name}_missing")
    _need(p.evidence_tier in {"generated", "owner-reviewed", "observed"}, "prereg_tier_unknown")
    _need(p.uncertainty_rule == UNCERTAINTY_RULE, "prereg_uncertainty_rule_unsupported")
    _unit(p.practical_threshold, "prereg_practical_threshold_invalid")
    _need(p.practical_threshold > 0, "prereg_practical_threshold_invalid")
    _unit(p.critical_slice_tolerance, "prereg_slice_tolerance_invalid")
    _need(0 < p.alpha < 1, "prereg_alpha_invalid")
    for name in ("seed", "fit_steps", "min_slice_cases", "min_independent_families"):
        _count(getattr(p, name), f"prereg_{name}_invalid")
    _need(p.fit_steps > 0 and p.min_independent_families >= 2, "prereg_budget_invalid")
    _count(p.fits_authorized, "prereg_fits_invalid")
    _count(p.spend_cap, "prereg_spend_cap_invalid")
    _need(len(set(p.critical_slices)) == len(p.critical_slices), "prereg_slices_duplicate")
    _need(REAL_ONLY not in p.reference_arms and AUGMENTED not in p.reference_arms, "prereg_arms")


def key_digest(key: ProtectedKey) -> str:
    return digest(sorted((k, v) for k, v in key.answers.items()))


def _components(cases: Sequence[CaseRef]) -> dict[str, str]:
    """Group id per case: same family_id OR identical fingerprint merge (union-find)."""
    parent: dict[str, str] = {}

    def find(x: str) -> str:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    first_by_print: dict[str, str] = {}
    for c in cases:
        find("f:" + c.family_id)
        fp = fingerprint(c.text)
        if fp in first_by_print:
            union("f:" + c.family_id, first_by_print[fp])
        else:
            first_by_print[fp] = "f:" + c.family_id
    return {c.case_id: "g:" + find("f:" + c.family_id)[2:] for c in cases}


def freeze_protocol(
    prereg: Preregistration,
    *,
    candidates: Sequence[CaseRef],
    real_training_roots: Sequence[CaseRef],
    development: Sequence[CaseRef],
    variant_children: Sequence[Mapping[str, Any]],
    selection_uses: Sequence[SelectionUse],
    answer_key_digest: str,
    declared_independent_family_count: int | None = None,
) -> FrozenProtocol:
    """Fix the untouched set and every registered term before any protected label opens.

    A candidate is excluded (and stays visible) if its case, family or normalized content was
    used for training, development, ANY variant generation (whatever the child disposition),
    threshold selection or model choice. Only ``admitted`` children are ever fit material.
    """
    _validate_prereg(prereg)
    _need(answer_key_digest.startswith("sha256:"), "answer_key_digest_missing")
    touched_cases: dict[str, str] = {}
    touched_families: dict[str, str] = {}
    touched_prints: dict[str, str] = {}

    def touch(case: str | None, family: str | None, text: str | None, why: str) -> None:
        if case:
            touched_cases.setdefault(case, why)
        if family:
            touched_families.setdefault(family, why)
        if text:
            touched_prints.setdefault(fingerprint(text), why)

    root_family: dict[str, str] = {}
    for c in real_training_roots:
        # One real case registered under two families would inflate any family count.
        _need(
            root_family.setdefault(c.case_id, c.family_id) == c.family_id, "root_identity_conflict"
        )
        touch(c.case_id, c.family_id, c.text, "real_training_root")
    for c in development:
        touch(c.case_id, c.family_id, c.text, "development_case")
    admitted: list[str] = []
    child_counts: dict[str, int] = {}
    for child in variant_children:
        _need(child.get("origin") == SYNTHETIC_ORIGIN, "child_origin_not_synthetic")
        disposition = str(child.get("disposition"))
        child_counts[disposition] = child_counts.get(disposition, 0) + 1
        for ref in ("root_ref", "parent_case_ref"):
            touch(str(child[ref]) if child.get(ref) else None, None, None, "variant_generation")
        touch(
            None,
            str(child.get("family_id") or "") or None,
            child.get("rendered_text"),
            "variant_generation",
        )
        if disposition == "admitted":
            _need(child.get("mode") == "training", "admitted_child_not_training_mode")
            admitted.append(_ident(child.get("child_id"), "child_id_missing"))
    for use in selection_uses:
        _need(use.stage in STAGES, "selection_stage_unknown", use.stage)
        for cid in use.case_ids:
            touch(cid, None, None, use.stage)
        for fid in use.family_ids:
            touch(None, fid, None, use.stage)

    # Propagate to a fixpoint: a touched case touches its family and normalized content, a
    # touched family touches every known member and its content, and touched content touches
    # every known case carrying it. Case-only uses (threshold selection, model choice, a
    # variant root) therefore cannot leave a sibling or a near-duplicate eligible.
    known = [*real_training_roots, *development, *candidates]
    changed = True
    while changed:
        changed = False
        for k in known:
            fp = fingerprint(k.text)
            why = touched_cases.get(k.case_id) or touched_families.get(k.family_id)
            why = why or touched_prints.get(fp)
            if why is None:
                continue
            for table, key in (
                (touched_cases, k.case_id),
                (touched_families, k.family_id),
                (touched_prints, fp),
            ):
                if key not in table:
                    table[key] = why
                    changed = True

    seen: set[str] = set()
    eligible: list[CaseRef] = []
    excluded: dict[str, str] = {}
    unresolved: list[str] = []
    for c in candidates:
        _ident(c.case_id, "case_id_missing")
        _ident(c.family_id, "family_id_missing")
        if c.case_id in seen:
            excluded[c.case_id] = "duplicate_case_id"
            continue
        seen.add(c.case_id)
        reason = (
            touched_cases.get(c.case_id)
            or touched_families.get(c.family_id)
            or touched_prints.get(fingerprint(c.text))
        )
        if reason is not None:
            excluded[c.case_id] = "touched_by_" + reason
        elif not c.resolved:
            unresolved.append(c.case_id)
        else:
            eligible.append(c)
    groups = _components(eligible)
    group_ids = sorted(set(groups.values()))
    derived = len(group_ids)
    body: dict[str, Any] = {
        "schema": PROTOCOL_SCHEMA,
        "protocol_id": prereg.protocol_id,
        "registered": {
            k: v
            for k, v in sorted(prereg.__dict__.items())
            if k not in {"critical_slices", "reference_arms"}
        }
        | {
            "critical_slices": sorted(prereg.critical_slices),
            "reference_arms": sorted(prereg.reference_arms),
        },
        "answer_key_digest": answer_key_digest,
        "untouched": {
            "eligible": sorted(c.case_id for c in eligible),
            "excluded": sorted([k, v] for k, v in excluded.items()),
            "unresolved": sorted(unresolved),
            "group_of": {c.case_id: groups[c.case_id] for c in eligible},
            "slices_of": {c.case_id: sorted(c.slices) for c in eligible},
            "family_of": {c.case_id: c.family_id for c in eligible},
            "independent_group_count": derived,
            "denominator": len(eligible),
        },
        "variants": {
            "admitted_child_ids": sorted(admitted),
            "child_counts_by_disposition": dict(sorted(child_counts.items())),
        },
        "family_count_reconciliation": {
            "derived_here": derived,
            "declared_by_variant_manifest": declared_independent_family_count,
            "agrees": declared_independent_family_count in (None, derived),
            "note": "derived count governs; reconcile when OPE-516 corrected count lands",
        },
        "real_training_roots": sorted(c.case_id for c in real_training_roots),
    }
    return FrozenProtocol(body=body, protocol_digest=digest(body))


# ---------------------------------------------------------------- seal


def seal_arms(protocol: FrozenProtocol, arms: Sequence[ArmRecord]) -> ArmSeal:
    """Freeze every arm's provenance and predictions BEFORE the protected key is opened."""
    ids = [a.arm_id for a in arms]
    _need(len(set(ids)) == len(ids), "duplicate_arm")
    body = {
        "schema": ARM_SEAL_SCHEMA,
        "protocol_digest": protocol.protocol_digest,
        "arms": {a.arm_id: digest(_arm_json(a)) for a in sorted(arms, key=lambda x: x.arm_id)},
    }
    return ArmSeal(body=body, seal_digest=digest(body))


def _arm_json(a: ArmRecord) -> dict[str, Any]:
    return {
        "arm_id": a.arm_id,
        "status": a.status,
        "model_ref": a.model_ref,
        "base_revision": a.base_revision,
        "recipe_id": a.recipe_id,
        "seed": a.seed,
        "steps": a.steps,
        "real_row_ids": sorted(a.real_row_ids),
        "synthetic_child_ids": sorted(a.synthetic_child_ids),
        "fit_cost": a.fit_cost,
        "generation_cost": a.generation_cost,
        "predictions": dict(sorted(a.predictions.items())),
        "declared_case_ids": sorted(a.declared_case_ids),
        "scored_origin": a.scored_origin,
    }


# ---------------------------------------------------------------- measurement


def _result(
    protocol: FrozenProtocol,
    seal: ArmSeal,
    *,
    verdict: Verdict,
    status: str,
    reasons: Sequence[str],
    claim: str,
    paired: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    u = protocol.body["untouched"]
    body: dict[str, Any] = {
        "schema": RESULT_SCHEMA,
        "protocol_digest": protocol.protocol_digest,
        "arm_seal_digest": seal.seal_digest,
        "evidence_tier": protocol.body["registered"]["evidence_tier"],
        "claim_scope": "implementation_only"
        if protocol.body["registered"]["evidence_tier"] == "generated"
        else protocol.body["registered"]["evidence_tier"],
        "verdict": verdict,
        "status": status,
        "reasons": sorted(set(reasons)),
        "claim": claim,
        "improvement_claim_blocked": verdict != "measured_benefit",
        "significance_claim": False,
        "preferred_arm": AUGMENTED if verdict == "measured_benefit" else REAL_ONLY,
        "promotion_allowed": verdict == "measured_benefit",
        "denominator": {
            "eligible_untouched": u["denominator"],
            "independent_groups": u["independent_group_count"],
            "excluded_visible": u["excluded"],
            "unresolved_visible": u["unresolved"],
        },
        "paired": paired,
        "family_count_reconciliation": protocol.body["family_count_reconciliation"],
        "unsatisfied_external_gates": list(UNSATISFIED_EXTERNAL_GATES),
    }
    body.update(dict(extra or {}))
    return {"body": body, "result_digest": digest(body)}


def _correct(arm: ArmRecord, key: ProtectedKey, case: str) -> bool:
    return arm.predictions.get(case) == key.answers[case]


def measure(
    protocol: FrozenProtocol,
    seal: ArmSeal,
    arms: Sequence[ArmRecord],
    key: ProtectedKey,
    *,
    post_freeze_uses: Sequence[SelectionUse] = (),
) -> dict[str, Any]:
    """Open the protected key once and return the preregistered verdict, or refuse to."""
    _need(digest(protocol.body) == protocol.protocol_digest, "protocol_digest_mismatch")
    _need(seal.body["protocol_digest"] == protocol.protocol_digest, "seal_not_for_protocol")
    _need(key_digest(key) == protocol.body["answer_key_digest"], "answer_key_not_the_frozen_key")
    by_id = {a.arm_id: a for a in arms}
    _need(len(by_id) == len(arms), "duplicate_arm")
    sealed = seal.body["arms"]
    for a in arms:
        _need(sealed.get(a.arm_id) == digest(_arm_json(a)), "arm_changed_after_seal", a.arm_id)
    reg = protocol.body["registered"]
    eligible = protocol.eligible
    eligible_set = set(eligible)

    # 1. Contamination invalidates the measurement outright; no number is produced.
    leaks: list[str] = []
    for use in post_freeze_uses:
        if set(use.case_ids) & eligible_set:
            leaks.append(f"untouched_case_used_in_{use.stage}")
        raw_families = set(protocol.body["untouched"].get("family_of", {}).values())
        if set(use.family_ids) & (raw_families | set(protocol.group_of.values())):
            leaks.append(f"untouched_family_used_in_{use.stage}")
        if use.used_protected_results:
            leaks.append(f"protected_results_used_in_{use.stage}")
    families_by_case = protocol.group_of
    for a in arms:
        rows = set(a.real_row_ids)
        if rows & eligible_set:
            leaks.append(f"untouched_case_in_fit_rows:{a.arm_id}")
        if rows & set(key.answers):
            leaks.append(f"protected_case_in_fit_rows:{a.arm_id}")
    if leaks:
        return _result(
            protocol,
            seal,
            verdict="inconclusive",
            status="invalidated",
            reasons=leaks,
            claim=UNPROVEN + ": untouched set was touched; measurement invalidated",
        )

    # 2. Missing / interrupted / revoked arms hold; the eligible real-only route is retained.
    required = [REAL_ONLY, AUGMENTED, *reg["reference_arms"]]
    unavailable = [
        f"arm_{'missing' if r not in by_id else by_id[r].status}:{r}"
        for r in required
        if r not in by_id or by_id[r].status != "completed"
    ]
    if unavailable:
        retained = REAL_ONLY in by_id and by_id[REAL_ONLY].status == "completed"
        return _result(
            protocol,
            seal,
            verdict="unrun",
            status="held",
            reasons=unavailable,
            claim=UNPROVEN,
            extra={"retained_route": REAL_ONLY if retained else None},
        )
    a, b = by_id[REAL_ONLY], by_id[AUGMENTED]

    # 3. Comparability of the two fits.
    broken: list[str] = []
    for arm in (a, b):
        for field, expected in (
            ("model_ref", reg["model_ref"]),
            ("base_revision", reg["base_revision"]),
            ("recipe_id", reg["recipe_id"]),
            ("seed", reg["seed"]),
            ("steps", reg["fit_steps"]),
        ):
            if getattr(arm, field) != expected:
                broken.append(f"{field}_differs_from_protocol:{arm.arm_id}")
    if sorted(a.real_row_ids) != sorted(b.real_row_ids):
        broken.append("real_rows_not_identical")
    if a.synthetic_child_ids:
        broken.append("real_only_contains_synthetic")
    if not b.synthetic_child_ids:
        broken.append("augmented_has_no_synthetic")
    if set(b.synthetic_child_ids) - protocol.admitted_children:
        broken.append("synthetic_child_not_admitted")
    if broken:
        return _result(
            protocol,
            seal,
            verdict="inconclusive",
            status="complete",
            reasons=["noncomparable_arms", *broken],
            claim=UNPROVEN,
        )

    # 4. One denominator, real untouched origin only.
    denominator_ok = True
    reasons: list[str] = []
    for arm in required:
        r = by_id[arm]
        if sorted(r.declared_case_ids) != list(eligible) or not set(r.predictions) <= eligible_set:
            denominator_ok = False
            reasons.append(f"changed_denominator:{arm}")
        if r.scored_origin != REAL_ORIGIN:
            denominator_ok = False
            reasons.append(f"non_real_origin_scores:{arm}")
    if not denominator_ok:
        return _result(
            protocol,
            seal,
            verdict="inconclusive",
            status="complete",
            reasons=reasons,
            claim=UNPROVEN + ": improvement claim blocked by non-identical denominators",
        )

    # 5. Score only resolved untouched cases; unresolved stay visible, outside.
    _need(set(eligible) <= set(key.answers), "key_missing_eligible_cases")
    scorable = [c for c in eligible if key.answers[c] is not None]
    unresolved_by_key = sorted(c for c in eligible if key.answers[c] is None)
    groups = sorted({families_by_case[c] for c in scorable})
    missing = {
        arm: sorted(c for c in scorable if c not in by_id[arm].predictions) for arm in required
    }
    if not scorable or len(groups) < reg["min_independent_families"]:
        return _result(
            protocol,
            seal,
            verdict="inconclusive",
            status="complete",
            reasons=["insufficient_independent_real_cases"],
            claim=UNPROVEN,
            paired={
                "scorable_cases": len(scorable),
                "independent_groups": len(groups),
                "unresolved_by_key_outside_denominator": unresolved_by_key,
            },
        )
    fixes = sorted(c for c in scorable if _correct(b, key, c) and not _correct(a, key, c))
    new_errors = sorted(c for c in scorable if _correct(a, key, c) and not _correct(b, key, c))
    effect_by_group: dict[str, list[int]] = {}
    for c in scorable:
        effect_by_group.setdefault(families_by_case[c], []).append(
            int(_correct(b, key, c)) - int(_correct(a, key, c))
        )
    iv = independent_family_interval(
        [(g, sum(v) / len(v)) for g, v in sorted(effect_by_group.items())], reg["alpha"], 2
    )
    slice_rows: dict[str, dict[str, Any]] = {}
    unknown_slices: list[str] = []
    regressed_slices: list[str] = []
    for s in reg["critical_slices"]:
        members = [c for c in scorable if s in protocol.slices_of[c]]
        if len(members) < reg["min_slice_cases"] or not members:
            unknown_slices.append(s)
            slice_rows[s] = {"cases": len(members), "status": "unknown_small_or_empty"}
            continue
        delta = Fraction(
            sum(int(_correct(b, key, c)) - int(_correct(a, key, c)) for c in members), len(members)
        )
        bad = -float(delta) > reg["critical_slice_tolerance"]
        slice_rows[s] = {"cases": len(members), "delta": float(delta), "regressed": bad}
        if bad:
            regressed_slices.append(s)
    paired: dict[str, Any] = {
        "scorable_cases": len(scorable),
        "independent_groups": len(groups),
        "unresolved_by_key_outside_denominator": unresolved_by_key,
        "fixes": len(fixes),
        "new_errors": len(new_errors),
        "missing_output": {k: len(v) for k, v in sorted(missing.items())},
        "mean_group_effect": iv.value,
        "interval": {
            "method": iv.method,
            "lower": iv.lower,
            "upper": iv.upper,
            "alpha_two_sided": reg["alpha"],
            "unit": "independent_group",
            "undefined_reason": iv.undefined_reason,
        },
        "critical_slices": slice_rows,
        "fix_refs": [_ref(protocol, c) for c in fixes],
        "new_error_refs": [_ref(protocol, c) for c in new_errors],
    }
    cost = {
        "real_only": a.fit_cost,
        "augmented_fit": b.fit_cost,
        "augmented_generation_separate": b.generation_cost,
        "incremental": b.fit_cost + b.generation_cost - a.fit_cost,
    }
    extra = {"cost": cost}
    if regressed_slices:
        return _result(
            protocol,
            seal,
            verdict="regression",
            status="complete",
            reasons=[f"critical_slice_regression:{s}" for s in regressed_slices],
            claim="Critical regression blocks promotion",
            paired=paired,
            extra=extra,
        )
    if unknown_slices or iv.value is None or iv.lower is None or iv.upper is None:
        return _result(
            protocol,
            seal,
            verdict="inconclusive",
            status="complete",
            reasons=[f"critical_slice_unknown:{s}" for s in unknown_slices]
            + (["decisive_metric_unknown"] if iv.value is None else []),
            claim=UNPROVEN,
            paired=paired,
            extra=extra,
        )
    thr = reg["practical_threshold"]
    if iv.upper < 0:
        verdict: Verdict = "regression"
        why = "aggregate_regression_interval_below_zero"
    elif iv.lower > 0 and iv.value >= thr:
        verdict, why = "measured_benefit", "interval_lower_above_zero_and_point_meets_threshold"
    elif iv.upper < thr:
        verdict, why = "no_material_benefit", "interval_upper_below_practical_threshold"
    else:
        verdict, why = "inconclusive", "interval_straddles_zero_or_threshold_descriptive_only"
    claim = {
        "measured_benefit": "Measured benefit on untouched real groups (generated fixture only "
        "unless tier is observed)",
        "no_material_benefit": "No material benefit; keep the cheaper real-only route",
        "regression": "Regression; no augmented promotion",
        "inconclusive": UNPROVEN,
    }[verdict]
    return _result(
        protocol,
        seal,
        verdict=verdict,
        status="complete",
        reasons=[why],
        claim=claim,
        paired=paired,
        extra=extra,
    )


def _ref(protocol: FrozenProtocol, case_id: str) -> str:
    return (
        "case:" + hashlib.sha256(f"{protocol.protocol_digest}|{case_id}".encode()).hexdigest()[:16]
    )


# ---------------------------------------------------------------- planning and spend


def plan_arms(
    prereg: Preregistration, *, simple_fix_succeeded: bool, matching_prior_arm: bool = False
) -> dict[str, Any]:
    """Decide which fits may run. A one-fit grant never silently becomes two fits."""
    _validate_prereg(prereg)
    if simple_fix_succeeded:
        return {"fits": [], "comparison": "unrun", "reason": "simple_fix_succeeded_zero_fits"}
    fits = prereg.fits_authorized
    if matching_prior_arm and fits >= 1:
        return {"fits": [AUGMENTED], "comparison": "run", "reason": "real_only_arm_reused"}
    if fits >= 2:
        return {"fits": [REAL_ONLY, AUGMENTED], "comparison": "run", "reason": "two_fits"}
    if fits == 1:
        return {"fits": [REAL_ONLY], "comparison": "unrun", "reason": "one_fit_grant"}
    return {"fits": [], "comparison": "unrun", "reason": "no_fit_authorized"}


_KEY_SEPARATOR = "|"


def _ledger_key(protocol_digest: object, arm_id: object, kind: str) -> str:
    """``digest|arm|kind``. Each part must be a non-empty str without the separator.

    Otherwise ``("d|a", "b")`` and ``("d", "a|b")`` would share a key and read as "replayed",
    and ``from_json`` could not tell a forged key from an honest one.
    """
    for part in (protocol_digest, arm_id):
        _need(
            type(part) is str and bool(part) and _KEY_SEPARATOR not in part,
            "reservation_key_invalid",
        )
    return f"{protocol_digest}{_KEY_SEPARATOR}{arm_id}{_KEY_SEPARATOR}{kind}"


class SpendLedger:
    """Reserve each distinct cost once; replay of the same key never double charges."""

    def __init__(self, *, spend_cap: int, fits_authorized: int) -> None:
        self.spend_cap = _count(spend_cap, "spend_cap_invalid")
        self.fits_authorized = _count(fits_authorized, "fits_invalid")
        self._reserved: dict[str, tuple[str, int]] = {}
        self._settled: dict[str, int] = {}

    def reserve(self, protocol_digest: str, arm_id: str, kind: str, amount: int) -> str:
        _need(kind in {"fit", "generation"}, "reservation_kind_unknown")
        _count(amount, "reservation_amount_invalid")
        key = _ledger_key(protocol_digest, arm_id, kind)
        if key in self._reserved:
            _need(self._reserved[key][1] == amount, "reservation_conflict", key)
            return "replayed"
        fits = sum(1 for _, (k, _) in self._reserved.items() if k == "fit")
        _need(kind != "fit" or fits < self.fits_authorized, "fit_grant_exceeded")
        _need(
            sum(v for _, v in self._reserved.values()) + amount <= self.spend_cap,
            "spend_cap_exceeded",
        )
        self._reserved[key] = (kind, amount)
        return "reserved"

    def settle(self, protocol_digest: str, arm_id: str, kind: str, actual: int) -> str:
        key = _ledger_key(protocol_digest, arm_id, kind)
        _count(actual, "settlement_amount_invalid")
        _need(key in self._reserved, "settle_without_reservation")
        _need(actual <= self._reserved[key][1], "actual_exceeds_reservation")
        if key in self._settled:
            _need(self._settled[key] == actual, "settlement_conflict", key)
            return "replayed"
        self._settled[key] = actual
        return "settled"

    @property
    def charged(self) -> int:
        return sum(self._settled.values())

    def to_json(self) -> dict[str, Any]:
        return {
            "spend_cap": self.spend_cap,
            "fits_authorized": self.fits_authorized,
            "reserved": {k: list(v) for k, v in sorted(self._reserved.items())},
            "settled": dict(sorted(self._settled.items())),
        }

    @classmethod
    def from_json(cls, raw: Mapping[str, Any]) -> SpendLedger:
        ledger = cls(spend_cap=raw["spend_cap"], fits_authorized=raw["fits_authorized"])
        reserved: dict[str, tuple[str, int]] = {}
        for k, v in raw["reserved"].items():
            _need(isinstance(v, (list, tuple)) and len(v) == 2, "ledger_forged_reservation", k)
            _need(v[0] in {"fit", "generation"}, "ledger_forged_reservation", k)
            parts = k.split(_KEY_SEPARATOR) if type(k) is str else []
            _need(
                len(parts) == 3 and all(parts) and parts[2] == v[0],
                "ledger_forged_reservation",
                k,
            )
            _count(v[1], "ledger_forged_reservation")
            reserved[k] = (v[0], v[1])
        settled: dict[str, int] = {}
        for k, amount in raw["settled"].items():
            _need(k in reserved, "ledger_forged_settlement", k)
            _need(_count(amount, "ledger_forged_settlement") <= reserved[k][1], "ledger_forged", k)
            settled[k] = amount
        _need(
            sum(1 for kind, _ in reserved.values() if kind == "fit") <= ledger.fits_authorized,
            "ledger_forged_fit_count",
        )
        _need(sum(v for _, v in reserved.values()) <= ledger.spend_cap, "ledger_forged_over_cap")
        ledger._reserved = reserved
        ledger._settled = settled
        return ledger


# ---------------------------------------------------------------- label efficiency


@dataclass(frozen=True)
class Checkpoint:
    arm_id: str
    unique_real_labels: int
    independent_groups: int
    target_met: bool | None
    confirmed: bool
    expert_minutes: int | None
    synthetic_review_minutes: int | None
    design: str = "retrospective"
    later_window_result: str | None = None


def label_efficiency(checkpoints: Iterable[Checkpoint]) -> dict[str, Any]:
    """Report label efficiency only at tested, comparably confirmed budgets."""
    cps = list(checkpoints)
    for c in cps:
        _need(c.arm_id in {REAL_ONLY, AUGMENTED}, "checkpoint_arm_unknown")
        _need(c.design in {"retrospective", "prospective"}, "checkpoint_design_unknown")
    confirmed = [c for c in cps if c.confirmed and c.target_met is not None]
    budgets = sorted(
        {
            c.unique_real_labels
            for c in confirmed
            if {x.arm_id for x in confirmed if x.unique_real_labels == c.unique_real_labels}
            == {REAL_ONLY, AUGMENTED}
        }
    )
    handoff = [
        {
            "arm_id": c.arm_id,
            "unique_real_labels": c.unique_real_labels,
            "independent_groups": c.independent_groups,
            "target_met": c.target_met,
            "status": "confirmed" if c.confirmed else "pending",
            "expert_minutes": c.expert_minutes,
            "synthetic_review_minutes": c.synthetic_review_minutes,
            "later_window_result": c.later_window_result,
            "design": c.design,
            "units": "labels_and_minutes_comparable_only_within_this_record",
        }
        for c in sorted(cps, key=lambda x: (x.unique_real_labels, x.arm_id))
    ]
    out: dict[str, Any] = {
        "schema": LABEL_SCHEMA,
        "tested_budgets": budgets,
        "ope_522_handoff": handoff,
        "evidence_tier": "generated",
        "launches_job": False,
    }
    if len(budgets) == 0:
        return out | {"claim": NOT_MEASURED, "reason": "no_paired_confirmed_checkpoint"}
    if len(budgets) == 1:
        return out | {
            "claim": NOT_MEASURED,
            "reason": "single_checkpoint_same_label_quality_lift_only",
        }

    def lowest(arm: str) -> int | None:
        hits = [
            c.unique_real_labels
            for c in confirmed
            if c.arm_id == arm and c.target_met and c.unique_real_labels in budgets
        ]
        return min(hits) if hits else None

    ro, aug = lowest(REAL_ONLY), lowest(AUGMENTED)
    if ro is None:
        return out | {
            "claim": "Real-only never reached target in the tested range",
            "tested_bound": max(budgets),
            "reduction_bps": None,
        }
    if aug is None or aug >= ro:
        return out | {"claim": "No label reduction at tested budgets", "reduction_bps": 0}
    minutes: dict[str, int | None] = {}
    for arm, budget in ((REAL_ONLY, ro), (AUGMENTED, aug)):
        row = next(c for c in confirmed if c.arm_id == arm and c.unique_real_labels == budget)
        parts = (row.expert_minutes, row.synthetic_review_minutes or 0)
        minutes[arm] = None if parts[0] is None else parts[0] + parts[1]
    labor = None if None in minutes.values() else minutes[AUGMENTED] - minutes[REAL_ONLY]  # type: ignore[operator]
    return out | {
        "claim": f"{(ro - aug) * 100 // ro}% fewer real labels at the same confirmed target",
        "reduction_bps": (ro - aug) * 10000 // ro,
        "real_only_budget": ro,
        "augmented_budget": aug,
        "labor_delta_minutes": labor,
        "labor_note": "negative savings accepted; hours are never inferred from label counts",
    }
