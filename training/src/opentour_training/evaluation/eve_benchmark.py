"""EVE held-out baseline and simple-repair benchmark (OPE-555), pure CPU.

Everything here is deterministic arithmetic over supplied records. A result from this
module is only as strong as its ``evidence_tier``: generated fixtures prove software
behaviour, never real EVE capacity, provider authority or Pi School outcomes.

Disposition provenance. ``record_disposition`` re-derives the scorecard digest from its body
and requires an in-process provenance seal that only ``score_heldout`` and
``assess_development`` stamp, so a hand-built ``Scorecard`` or ``DevelopmentFinding`` is
refused even when its digest is self-consistent. The seal is an HMAC under a key generated
per process and never persisted: it is not a signature, does not survive serialization, and
is not a cross-process trust root. A scorecard reloaded from JSON cannot take a disposition.

Owner. The owner id comes from the owner named in the frozen manifest (``named_owner_id``),
bound by the manifest digest before any output exists. Without one, the caller's id is
recorded as ``caller_asserted`` and ``named_owner_gate_satisfied`` is false. Neither path
authenticates a person: no identity provider is exercised here. One disposition per
scorecard is enforced by ``record_one_disposition``, whose path derives from the scorecard
digest rather than from the caller.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from opentour_training.evaluation.statistics_kernel import (
    binomial_lower_bound,
    binomial_upper_bound,
    independent_family_interval,
    paired_binary_interval,
)

MANIFEST_SCHEMA = "opentour.eve-benchmark-manifest.v1"
SCORECARD_SCHEMA = "opentour.eve-benchmark-scorecard.v1"
DISPOSITION_SCHEMA = "opentour.eve-benchmark-disposition.v1"
SCORER_VERSION = "eve-scorer-1.0.0"

EvidenceTier = Literal["generated", "owner-reviewed", "observed", "installed-provider"]
Outcome = Literal[
    "correct_answer",
    "correct_abstention",
    "serious_error",
    "inappropriate_refusal",
    "missing_output",
    "unresolved",
]
ImprovementClaim = Literal[
    "none_baseline_only",
    "hold_missing_arm",
    "blocked_changed_denominator",
    "directional_only_interval_includes_zero",
    "improvement_supported_by_interval",
    "no_net_fixes",
]
Disposition = Literal[
    "benchmark_only", "adopt_test_simple_fix", "consider_tuning", "gather_more_evidence"
]
DISPOSITIONS: frozenset[str] = frozenset(
    {"benchmark_only", "adopt_test_simple_fix", "consider_tuning", "gather_more_evidence"}
)
EXCLUSION_REASON_CODES: frozenset[str] = frozenset(
    {
        "context_not_authorized",
        "reference_unavailable",
        "duplicate_or_overlap",
        "out_of_scope",
        "other",
    }
)
DIGEST_SHAPE = re.compile(r"^sha256:[0-9a-f]{64}$")
BASELINE_ARM = "baseline"
REPAIR_ARM = "simple_repair"
UNSATISFIED_EXTERNAL_GATES_GENERATED: tuple[str, ...] = (
    "real_eve_inference_outputs_unavailable",
    "pi_school_domain_reviewer_adjudication_unavailable",
    "real_pi_school_cases_and_references_unavailable",
    "provider_authorization_not_granted",
)


class BenchmarkError(ValueError):
    """Closed refusal. ``code`` is the stable machine reason; nothing is scored after it."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def _need(condition: bool, code: str, detail: str = "") -> None:
    if not condition:
        raise BenchmarkError(code, detail)


def _text(value: object, code: str) -> str:
    _need(type(value) is str and bool(value) and len(value) <= 4096, code)
    assert isinstance(value, str)
    return value


# Process-local provenance key: generated at import, never written anywhere. See module doc.
_PROVENANCE_KEY = secrets.token_bytes(32)


def _seal(kind: str, value: object) -> str:
    return hmac.new(_PROVENANCE_KEY, canonical([kind, value]), hashlib.sha256).hexdigest()


def _sealed(kind: str, value: object, seal: object) -> bool:
    return type(seal) is str and hmac.compare_digest(seal, _seal(kind, value))


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class EveCase:
    """Authorized question and context only. Never carries a reference answer."""

    case_id: str
    family_id: str
    question: str
    context_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReferenceJudgment:
    """Evaluator-only. Supplied by the independent domain reviewer."""

    case_id: str
    should_abstain: bool
    answer: str | None
    supporting_citations: tuple[str, ...]
    disputed: bool = False


@dataclass(frozen=True)
class ArmConfig:
    """The frozen, versioned way an arm is produced, so a changed arm is interpretable."""

    arm_id: str
    change: str
    served_revision: str
    source_snapshot_id: str
    retrieval_settings_id: str


@dataclass(frozen=True)
class ArmOutput:
    case_id: str
    answer: str | None
    citations: tuple[str, ...]
    abstained: bool
    served_revision: str
    source_snapshot_id: str
    retrieval_settings_id: str


@dataclass(frozen=True)
class ArmRun:
    """One arm's outputs, bound to the frozen manifest it was produced under."""

    arm_id: str
    manifest_digest: str
    declared_case_ids: tuple[str, ...]
    outputs: tuple[ArmOutput, ...]


@dataclass(frozen=True)
class ExclusionRecord:
    """``reason`` is a closed code (see EXCLUSION_REASON_CODES), never free text."""

    case_id: str
    reason: str


@dataclass(frozen=True)
class FrozenManifest:
    """Immutable freeze. Construct only through :func:`freeze_manifest`."""

    body: Mapping[str, Any]
    manifest_digest: str

    @property
    def heldout_eligible(self) -> tuple[str, ...]:
        return tuple(self.body["heldout"]["eligible_case_ids"])

    @property
    def arms(self) -> dict[str, Any]:
        return dict(self.body["arms"])


# ---------------------------------------------------------------- freeze


def reference_key_digest(key: Sequence[ReferenceJudgment]) -> str:
    return digest(
        sorted(
            (
                [k.case_id, k.should_abstain, k.answer, sorted(k.supporting_citations), k.disputed]
                for k in key
            ),
            key=lambda row: str(row[0]),
        )
    )


def case_content_digest(cases: Sequence[EveCase]) -> str:
    """Digest of the full authorized case text, so a changed question changes the freeze."""
    return digest(sorted([c.case_id, c.family_id, c.question, list(c.context_ids)] for c in cases))


def freeze_manifest(
    *,
    benchmark_id: str,
    evidence_tier: str,
    development_cases: Sequence[EveCase],
    heldout_cases: Sequence[EveCase],
    heldout_exclusions: Sequence[ExclusionRecord],
    heldout_key: Sequence[ReferenceJudgment],
    development_key: Sequence[ReferenceJudgment],
    arms: Sequence[ArmConfig],
    rubric_version: str,
    rubric_text: str,
    alpha: float,
    materiality_min_errors: int,
    min_recurring_families: int,
    adjudicator_kind: Literal["generated_fixture", "pi_school_domain_reviewer"],
    ground_truth_audit_receipt_ref: str | None = None,
    named_owner_id: str | None = None,
) -> FrozenManifest:
    """Freeze cases, families, rubric, scorer, arms and thresholds BEFORE any output exists.

    ``named_owner_id`` binds the owner who may record the disposition into the freeze. It is
    only added to the body when given, so manifests that name no owner keep their digests.
    """
    _text(benchmark_id, "benchmark_id_invalid")
    if named_owner_id is not None:
        _text(named_owner_id, "named_owner_invalid")
    # This module cannot verify a provider or customer receipt, so it only ever freezes
    # generated-tier evidence. Higher tiers need a verifier owned by the provider gate.
    _need(evidence_tier == "generated", "evidence_tier_unverifiable_here", str(evidence_tier))
    _need(
        ground_truth_audit_receipt_ref is None
        or (
            type(ground_truth_audit_receipt_ref) is str
            and DIGEST_SHAPE.match(ground_truth_audit_receipt_ref) is not None
        ),
        "audit_receipt_ref_shape_invalid",
    )
    _need(0 < alpha < 1, "alpha_invalid")
    _need(materiality_min_errors >= 1 and min_recurring_families >= 1, "thresholds_invalid")
    _need(bool(development_cases) and bool(heldout_cases), "empty_split")
    dev_ids = [c.case_id for c in development_cases]
    held_ids = [c.case_id for c in heldout_cases]
    _need(len(set(dev_ids)) == len(dev_ids), "duplicate_development_case")
    _need(len(set(held_ids)) == len(held_ids), "duplicate_heldout_case")
    _need(not set(dev_ids) & set(held_ids), "split_case_overlap")
    dev_fams = {c.family_id for c in development_cases}
    held_fams = {c.family_id for c in heldout_cases}
    _need(not dev_fams & held_fams, "split_family_overlap")
    excl = {e.case_id: e.reason for e in heldout_exclusions}
    _need(set(excl) <= set(held_ids), "exclusion_unknown_case")
    _need(all(v in EXCLUSION_REASON_CODES for v in excl.values()), "exclusion_reason_invalid")
    dev_key_ids = [k.case_id for k in development_key]
    _need(sorted(dev_key_ids) == sorted(dev_ids), "development_key_not_aligned")
    eligible = [i for i in held_ids if i not in excl]
    _need(bool(eligible), "no_eligible_heldout_cases")
    key_ids = [k.case_id for k in heldout_key]
    _need(sorted(key_ids) == sorted(held_ids), "key_not_aligned_to_heldout_cases")
    _need(
        all(k.should_abstain == (k.answer is None) or k.disputed for k in heldout_key),
        "reference_inconsistent",
    )
    arm_ids = [a.arm_id for a in arms]
    _need(BASELINE_ARM in arm_ids and len(set(arm_ids)) == len(arm_ids), "arms_invalid")
    _need(set(arm_ids) <= {BASELINE_ARM, REPAIR_ARM}, "arms_unsupported")
    for a in arms:
        for f in (a.change, a.served_revision, a.source_snapshot_id, a.retrieval_settings_id):
            _text(f, "arm_config_invalid")
    _text(rubric_version, "rubric_invalid")
    _text(rubric_text, "rubric_invalid")
    body: dict[str, Any] = {
        "schema": MANIFEST_SCHEMA,
        "benchmark_id": benchmark_id,
        "evidence_tier": evidence_tier,
        "state": "frozen_before_candidate_output",
        "scorer_version": SCORER_VERSION,
        "rubric": {"version": rubric_version, "digest": digest(rubric_text)},
        "alpha": alpha,
        "thresholds": {
            "materiality_min_errors": materiality_min_errors,
            "min_recurring_families": min_recurring_families,
        },
        "development": {
            "case_ids": sorted(dev_ids),
            "family_ids": sorted(dev_fams),
            "content_digest": case_content_digest(development_cases),
            "key_digest": reference_key_digest(development_key),
        },
        "heldout": {
            "case_ids": sorted(held_ids),
            "family_ids": sorted(held_fams),
            "content_digest": case_content_digest(heldout_cases),
            "eligible_case_ids": sorted(eligible),
            "excluded": sorted([[k, v] for k, v in excl.items()]),
            "answer_key_digest": reference_key_digest(heldout_key),
            "adjudicator_kind": adjudicator_kind,
        },
        "arms": {
            a.arm_id: {
                "change": a.change,
                "served_revision": a.served_revision,
                "source_snapshot_id": a.source_snapshot_id,
                "retrieval_settings_id": a.retrieval_settings_id,
            }
            for a in arms
        },
        "ground_truth_audit_receipt_ref": ground_truth_audit_receipt_ref,
    }
    if named_owner_id is not None:
        body["named_owner"] = {"owner_id": named_owner_id}
    return FrozenManifest(body=body, manifest_digest=digest(body))


def verify_manifest(manifest: FrozenManifest) -> None:
    _need(
        manifest.body.get("state") == "frozen_before_candidate_output"
        and digest(dict(manifest.body)) == manifest.manifest_digest,
        "manifest_not_frozen_or_tampered",
    )


def manifest_from_json(raw: Mapping[str, Any]) -> FrozenManifest:
    body = dict(raw["body"])
    m = FrozenManifest(body=body, manifest_digest=str(raw["manifest_digest"]))
    verify_manifest(m)
    return m


def manifest_to_json(manifest: FrozenManifest) -> dict[str, Any]:
    return {"body": dict(manifest.body), "manifest_digest": manifest.manifest_digest}


# ---------------------------------------------------------------- isolation


@dataclass(frozen=True)
class FitRow:
    case_id: str
    family_id: str
    text: str


@dataclass(frozen=True)
class IsolationReceipt:
    manifest_digest: str
    checked_fit_rows: int
    checked_prompts: int
    fit_rows_digest: str
    prompts_digest: str
    violations: tuple[str, ...]

    @property
    def clean(self) -> bool:
        return not self.violations


def verify_isolation(
    manifest: FrozenManifest,
    *,
    fit_rows: Iterable[FitRow],
    prompt_texts: Iterable[str],
    heldout_key: Sequence[ReferenceJudgment],
) -> IsolationReceipt:
    """Held-out cases, families or protected answers must not reach fit inputs or prompts."""
    verify_manifest(manifest)
    held_cases = set(manifest.body["heldout"]["case_ids"])
    held_fams = set(manifest.body["heldout"]["family_ids"])
    secrets = {k.answer for k in heldout_key if k.answer} | {
        c for k in heldout_key for c in k.supporting_citations
    }
    violations: list[str] = []
    n_fit = n_prompt = 0
    rows, prompts = tuple(fit_rows), tuple(prompt_texts)
    for row in rows:
        n_fit += 1
        if row.case_id in held_cases:
            violations.append(f"fit_row_heldout_case:{row.case_id}")
        if row.family_id in held_fams:
            violations.append(f"fit_row_heldout_family:{row.family_id}")
        if any(s in row.text for s in secrets):
            violations.append(f"fit_row_protected_answer:{row.case_id}")
    for text in prompts:
        n_prompt += 1
        if any(s in text for s in secrets):
            violations.append(f"prompt_protected_answer:{n_prompt}")
    return IsolationReceipt(
        manifest.manifest_digest,
        n_fit,
        n_prompt,
        digest(sorted([r.case_id, r.family_id, r.text] for r in rows)),
        digest(list(prompts)),
        tuple(sorted(violations)),
    )


# ---------------------------------------------------------------- grading


def grade(ref: ReferenceJudgment, out: ArmOutput | None) -> Outcome:
    if ref.disputed:
        return "unresolved"
    if out is None:
        return "missing_output"
    if out.abstained:
        return "correct_abstention" if ref.should_abstain else "inappropriate_refusal"
    if ref.should_abstain:
        return "serious_error"  # unsupported answer where abstention was right
    answer_ok = out.answer == ref.answer
    cites = set(out.citations)
    cite_ok = bool(cites) and cites <= set(ref.supporting_citations)
    return "correct_answer" if answer_ok and cite_ok else "serious_error"


def _is_correct(o: Outcome) -> bool:
    return o in ("correct_answer", "correct_abstention")


def _check_run(manifest: FrozenManifest, run: ArmRun) -> dict[str, ArmOutput]:
    _need(run.manifest_digest == manifest.manifest_digest, "run_not_bound_to_frozen_manifest")
    cfg = manifest.arms.get(run.arm_id)
    _need(cfg is not None, "arm_not_in_frozen_manifest", run.arm_id)
    assert cfg is not None
    by_case: dict[str, ArmOutput] = {}
    for o in run.outputs:
        _need(o.case_id not in by_case, "duplicate_output", o.case_id)
        _need(
            (o.served_revision, o.source_snapshot_id, o.retrieval_settings_id)
            == (cfg["served_revision"], cfg["source_snapshot_id"], cfg["retrieval_settings_id"]),
            "arm_attribution_mismatch",
            o.case_id,
        )
        by_case[o.case_id] = o
    _need(
        set(by_case) <= set(run.declared_case_ids),
        "output_outside_declared_cases",
    )
    return by_case


def _arm_stats(
    manifest: FrozenManifest,
    key: Mapping[str, ReferenceJudgment],
    outputs: Mapping[str, ArmOutput],
    case_ids: Sequence[str],
) -> tuple[dict[str, Any], dict[str, Outcome]]:
    alpha = float(manifest.body["alpha"])
    outcomes: dict[str, Outcome] = {c: grade(key[c], outputs.get(c)) for c in case_ids}
    vals = list(outcomes.values())

    def count(*names: str) -> int:
        return sum(1 for v in vals if v in names)

    eligible = len(case_ids)
    unresolved = count("unresolved")
    verified = eligible - unresolved
    answered_verified = count("correct_answer", "serious_error")
    serious = count("serious_error")
    correct = count("correct_answer", "correct_abstention")
    stats: dict[str, Any] = {
        "eligible": eligible,
        "unresolved": unresolved,
        "verified": verified,
        "missing_output": count("missing_output"),
        "answered_verified": answered_verified,
        "correct_answer": count("correct_answer"),
        "correct_abstention": count("correct_abstention"),
        "inappropriate_refusal": count("inappropriate_refusal"),
        "serious_errors": serious,
        "correct": correct,
        "coverage_answered_over_verified": [answered_verified, verified],
        "serious_error_over_answered_verified": [serious, answered_verified],
        "correct_over_verified": [correct, verified],
        "serious_error_interval": _two_sided(serious, answered_verified, alpha),
        "correct_interval": _two_sided(correct, verified, alpha),
    }
    return stats, outcomes


def _two_sided(k: int, n: int, alpha: float) -> dict[str, Any]:
    if n == 0:
        return {
            "k": k,
            "n": n,
            "lower": None,
            "upper": None,
            "note": "denominator_zero_unavailable",
        }
    return {
        "k": k,
        "n": n,
        "lower": binomial_lower_bound(k, n, alpha / 2),
        "upper": binomial_upper_bound(k, n, alpha / 2),
        "method": "clopper_pearson_exact_per_arm",
        "alpha_two_sided": alpha,
    }


# ---------------------------------------------------------------- development


@dataclass(frozen=True)
class DevelopmentFinding:
    """Incremental residual after the one simple change, on development evidence only."""

    manifest_digest: str
    baseline_serious: int
    repair_serious: int
    verified: int
    fixes: int
    regressions: int
    residual_error_families: tuple[str, ...]
    material_recurring_residual: bool
    seal: str = field(default="", repr=False)  # set only by assess_development; see module doc

    def to_json(self) -> dict[str, Any]:
        return {
            "manifest_digest": self.manifest_digest,
            "baseline_serious": self.baseline_serious,
            "repair_serious": self.repair_serious,
            "verified": self.verified,
            "fixes": self.fixes,
            "regressions": self.regressions,
            "residual_error_families": list(self.residual_error_families),
            "material_recurring_residual": self.material_recurring_residual,
        }


def assess_development(
    manifest: FrozenManifest,
    *,
    development_cases: Sequence[EveCase],
    development_key: Sequence[ReferenceJudgment],
    baseline: ArmRun,
    repair: ArmRun,
) -> DevelopmentFinding:
    """Development evidence may be inspected; thresholds were frozen in the manifest."""
    verify_manifest(manifest)
    ids = [c.case_id for c in development_cases]
    _need(sorted(ids) == manifest.body["development"]["case_ids"], "development_cases_not_frozen")
    _need(
        case_content_digest(development_cases) == manifest.body["development"]["content_digest"],
        "development_content_not_the_frozen_content",
    )
    _need(
        reference_key_digest(development_key) == manifest.body["development"]["key_digest"],
        "development_key_not_the_frozen_key",
    )
    fam = {c.case_id: c.family_id for c in development_cases}
    key = {k.case_id: k for k in development_key}
    _need(set(key) == set(ids), "development_key_misaligned")
    b_out = _check_run(manifest, baseline)
    r_out = _check_run(manifest, repair)
    _need(baseline.arm_id == BASELINE_ARM and repair.arm_id == REPAIR_ARM, "dev_arms_invalid")
    b = {c: grade(key[c], b_out.get(c)) for c in ids}
    r = {c: grade(key[c], r_out.get(c)) for c in ids}
    scorable = [c for c in ids if b[c] != "unresolved"]
    fixes = sum(1 for c in scorable if _is_correct(r[c]) and not _is_correct(b[c]))
    regs = sum(1 for c in scorable if _is_correct(b[c]) and not _is_correct(r[c]))
    residual = [c for c in scorable if r[c] == "serious_error"]
    fams = tuple(sorted({fam[c] for c in residual}))
    th = manifest.body["thresholds"]
    material = (
        len(residual) >= th["materiality_min_errors"] and len(fams) >= th["min_recurring_families"]
    )
    finding = DevelopmentFinding(
        manifest.manifest_digest,
        sum(1 for c in scorable if b[c] == "serious_error"),
        len(residual),
        len(scorable),
        fixes,
        regs,
        fams,
        material,
    )
    return replace(finding, seal=_seal("development_finding", finding.to_json()))


# ---------------------------------------------------------------- held-out scorecard


@dataclass(frozen=True)
class Scorecard:
    body: Mapping[str, Any]
    scorecard_digest: str
    seal: str = field(default="", repr=False)  # set only by score_heldout; see module doc

    def to_json(self) -> dict[str, Any]:
        return {"body": dict(self.body), "scorecard_digest": self.scorecard_digest}


def _redact(manifest_digest: str, case_id: str) -> str:
    return "case:" + hashlib.sha256(f"{manifest_digest}|{case_id}".encode()).hexdigest()[:16]


def score_heldout(
    manifest: FrozenManifest,
    *,
    heldout_cases: Sequence[EveCase],
    heldout_key: Sequence[ReferenceJudgment],
    fit_rows: Sequence[FitRow],
    prompt_texts: Sequence[str],
    isolation: IsolationReceipt,
    runs: Sequence[ArmRun],
) -> Scorecard:
    """Score the frozen held-out cases. Refuses unless freeze, key seal and isolation hold."""
    verify_manifest(manifest)
    _need(isolation.manifest_digest == manifest.manifest_digest, "isolation_not_bound_to_manifest")
    # The receipt is re-derived from the actual inputs; a hand-built receipt cannot pass.
    recomputed = verify_isolation(
        manifest, fit_rows=fit_rows, prompt_texts=prompt_texts, heldout_key=heldout_key
    )
    _need(recomputed == isolation, "isolation_receipt_not_from_these_inputs")
    _need(isolation.clean, "isolation_violation", ",".join(isolation.violations))
    _need(
        case_content_digest(heldout_cases) == manifest.body["heldout"]["content_digest"],
        "heldout_content_not_the_frozen_content",
    )
    _need(
        reference_key_digest(heldout_key) == manifest.body["heldout"]["answer_key_digest"],
        "answer_key_not_the_frozen_key",
    )
    _need(bool(runs), "no_arm_runs")
    key = {k.case_id: k for k in heldout_key}
    eligible = manifest.heldout_eligible
    arm_ids = [r.arm_id for r in runs]
    _need(len(set(arm_ids)) == len(arm_ids), "duplicate_arm_run")
    _need(BASELINE_ARM in arm_ids, "baseline_missing")
    arms_out: dict[str, Any] = {}
    outcomes: dict[str, dict[str, Outcome]] = {}
    denominator_ok = True
    for run in sorted(runs, key=lambda r: r.arm_id):
        outs = _check_run(manifest, run)
        if sorted(run.declared_case_ids) != sorted(eligible) or not set(outs) <= set(eligible):
            denominator_ok = False
        stats, oc = _arm_stats(manifest, key, outs, eligible)
        arms_out[run.arm_id] = stats
        outcomes[run.arm_id] = oc
    paired: dict[str, Any] | None = None
    claim: ImprovementClaim
    if REPAIR_ARM not in outcomes:
        claim = "none_baseline_only" if REPAIR_ARM not in manifest.arms else "hold_missing_arm"
    elif not denominator_ok:
        claim = "blocked_changed_denominator"
    else:
        b, r = outcomes[BASELINE_ARM], outcomes[REPAIR_ARM]
        scor = [c for c in eligible if b[c] != "unresolved"]
        fixes = sorted(c for c in scor if _is_correct(r[c]) and not _is_correct(b[c]))
        regs = sorted(c for c in scor if _is_correct(b[c]) and not _is_correct(r[c]))
        both_ok = sum(1 for c in scor if _is_correct(b[c]) and _is_correct(r[c]))
        both_bad = len(scor) - len(fixes) - len(regs) - both_ok
        iv = paired_binary_interval(
            len(fixes), len(regs), both_ok, both_bad, float(manifest.body["alpha"]), 2
        )
        fam_of = {c.case_id: c.family_id for c in heldout_cases}
        by_family: dict[str, list[int]] = {}
        for c in scor:
            by_family.setdefault(fam_of[c], []).append(
                int(_is_correct(r[c])) - int(_is_correct(b[c]))
            )
        fiv = independent_family_interval(
            [(f, sum(v) / len(v)) for f, v in sorted(by_family.items())],
            float(manifest.body["alpha"]),
            2,
        )
        paired = {
            "paired_gradeable": len(scor),
            "fixes": len(fixes),
            "regressions": len(regs),
            "both_correct": both_ok,
            "both_wrong": both_bad,
            "difference_in_correct_rate": iv.value,
            "interval": {
                "method": iv.method,
                "lower": iv.lower,
                "upper": iv.upper,
                "undefined_reason": iv.undefined_reason,
                "alpha_two_sided": float(manifest.body["alpha"]),
                "note": "case-level interval assumes cases are independent; "
                "descriptive uncertainty only, no significance claim is made",
                "case_independence_assumed": True,
            },
            "family_interval": {
                "method": fiv.method,
                "unit": "family",
                "units": len(by_family),
                "mean_family_effect": fiv.value,
                "lower": fiv.lower,
                "upper": fiv.upper,
                "undefined_reason": fiv.undefined_reason,
                "alpha_two_sided": float(manifest.body["alpha"]),
                "note": "families are the independent units; no case independence assumed",
            },
            "fix_case_refs": [_redact(manifest.manifest_digest, c) for c in fixes],
            "regression_case_refs": [_redact(manifest.manifest_digest, c) for c in regs],
        }
        if len(fixes) <= len(regs):
            claim = "no_net_fixes"
        elif all(x is not None and x > 0 for x in (iv.lower, fiv.lower)):
            claim = "improvement_supported_by_interval"
        else:
            claim = "directional_only_interval_includes_zero"
    tier = manifest.body["evidence_tier"]
    audit = manifest.body["ground_truth_audit_receipt_ref"]
    gates = list(UNSATISFIED_EXTERNAL_GATES_GENERATED)
    if manifest.body["heldout"]["adjudicator_kind"] != "pi_school_domain_reviewer":
        gates.append("independent_domain_reviewer_not_pi_school")
    if audit is None:
        gates.append("ground_truth_audit_receipt_absent")
    body: dict[str, Any] = {
        "schema": SCORECARD_SCHEMA,
        "manifest_digest": manifest.manifest_digest,
        "benchmark_id": manifest.body["benchmark_id"],
        "evidence_tier": tier,
        "scorer_version": SCORER_VERSION,
        "rubric": manifest.body["rubric"],
        "heldout": {
            "eligible": len(eligible),
            "excluded": sorted(
                [_redact(manifest.manifest_digest, k), v]
                for k, v in manifest.body["heldout"]["excluded"]
            ),
            "isolation": {
                "fit_rows_digest": isolation.fit_rows_digest,
                "prompts_digest": isolation.prompts_digest,
                "checked_fit_rows": isolation.checked_fit_rows,
                "checked_prompts": isolation.checked_prompts,
            },
            "unresolved": arms_out[BASELINE_ARM]["unresolved"],
            "abstained_correct_by_arm": {
                a: s["correct_abstention"] for a, s in sorted(arms_out.items())
            },
        },
        "arms": arms_out,
        "arm_configs": {a: manifest.arms[a] for a in sorted(arms_out)},
        "paired_comparison": paired,
        "improvement_claim": claim,
        "denominators_identical_across_arms": denominator_ok,
        "case_outcomes_redacted": {
            a: {_redact(manifest.manifest_digest, c): o for c, o in sorted(oc.items())}
            for a, oc in sorted(outcomes.items())
        },
        "accuracy_claim_eligible": False,
        "unsatisfied_external_gates": gates,
        "claim_scope": "implementation_only" if tier == "generated" else tier,
    }
    if "named_owner" in manifest.body:
        body["named_owner"] = dict(manifest.body["named_owner"])
    card_digest = digest(body)
    return Scorecard(body=body, scorecard_digest=card_digest, seal=_seal("scorecard", card_digest))


# ---------------------------------------------------------------- disposition


def _genuine_finding(finding: object) -> bool:
    """Exact type, exact field types, and the seal ``assess_development`` stamped."""
    if type(finding) is not DevelopmentFinding:
        return False
    counts = (
        finding.baseline_serious,
        finding.repair_serious,
        finding.verified,
        finding.fixes,
        finding.regressions,
    )
    return (
        type(finding.manifest_digest) is str
        and all(type(n) is int for n in counts)
        and type(finding.material_recurring_residual) is bool
        and type(finding.residual_error_families) is tuple
        and all(type(f) is str for f in finding.residual_error_families)
        and _sealed("development_finding", finding.to_json(), finding.seal)
    )


def record_disposition(
    scorecard: Scorecard,
    finding: DevelopmentFinding | None,
    *,
    disposition: str,
    recorded_at: str,
    owner_id: str | None = None,
) -> dict[str, Any]:
    """Build one disposition record bound to a scorecard produced by ``score_heldout``.

    The scorecard digest is re-derived from its body and its provenance seal checked, and a
    supplied finding must carry ``assess_development``'s seal. The owner id is the one named in
    the frozen manifest when there is one (a different ``owner_id`` is refused); otherwise the
    caller's ``owner_id`` is recorded as caller-asserted and never satisfies the named-owner
    gate. Persist with ``record_one_disposition`` to enforce one disposition per scorecard.
    """
    _need(
        type(scorecard) is Scorecard and type(scorecard.scorecard_digest) is str,
        "scorecard_not_from_scoring",
    )
    # One canonical snapshot: the digest is checked over it and every later read uses it, so a
    # body Mapping that answers differently on a second read cannot change what is recorded.
    body: dict[str, Any] = json.loads(canonical(dict(scorecard.body)))
    _need(digest(body) == scorecard.scorecard_digest, "scorecard_digest_mismatch")
    _need(
        _sealed("scorecard", scorecard.scorecard_digest, scorecard.seal),
        "scorecard_not_from_scoring",
    )
    _need(disposition in DISPOSITIONS, "disposition_unknown", disposition)
    _text(recorded_at, "recorded_at_missing")
    named = body.get("named_owner")
    if named is not None:
        bound = named["owner_id"]
        _need(owner_id is None or owner_id == bound, "owner_not_the_named_owner")
        owner, binding = bound, "named_in_frozen_manifest"
    else:
        owner, binding = _text(owner_id, "owner_missing"), "caller_asserted"
    claim = body["improvement_claim"]
    if disposition == "adopt_test_simple_fix":
        _need(
            claim == "improvement_supported_by_interval",
            "adopt_requires_interval_supported_improvement",
            claim,
        )
    if disposition == "consider_tuning":
        _need(finding is not None, "tuning_requires_development_finding")
        assert finding is not None
        _need(
            finding.manifest_digest == body["manifest_digest"],
            "finding_not_bound_to_manifest",
        )
        _need(finding.material_recurring_residual, "tuning_requires_material_recurring_residual")
    if finding is not None:  # any supplied finding must be genuine, used or not
        _need(_genuine_finding(finding), "finding_not_from_assessment")
        _need(finding.manifest_digest == body["manifest_digest"], "finding_not_bound_to_manifest")
    record = {
        "schema": DISPOSITION_SCHEMA,
        "scorecard_digest": scorecard.scorecard_digest,
        "manifest_digest": body["manifest_digest"],
        "disposition": disposition,
        "owner_id": owner,
        "owner_binding": binding,
        "named_owner_gate_satisfied": binding == "named_in_frozen_manifest",
        "owner_identity_authenticated": False,
        "recorded_at": recorded_at,
        "fit_authorized": False,
    }
    return {**record, "record_digest": digest(record)}


def record_one_disposition(
    store: Path,
    scorecard: Scorecard,
    finding: DevelopmentFinding | None,
    *,
    disposition: str,
    recorded_at: str,
    owner_id: str | None = None,
) -> bool:
    """Worker step: record and persist at most one disposition per scorecard in ``store``.

    The file name derives from the scorecard digest, so the caller cannot pick a second path
    for the same scorecard. An identical retry is a no-op (``False``); any different record
    for an already-dispositioned scorecard, including a later ``recorded_at``, is refused.
    """
    record = record_disposition(
        scorecard, finding, disposition=disposition, recorded_at=recorded_at, owner_id=owner_id
    )
    card = scorecard.scorecard_digest
    _need(DIGEST_SHAPE.match(card) is not None, "scorecard_digest_mismatch")
    path = store / f"disposition-{card.removeprefix('sha256:')}.json"
    try:
        return write_once(path, record)
    except BenchmarkError as exc:
        if exc.code == "artifact_conflict":
            raise BenchmarkError("disposition_already_recorded", card) from None
        raise


# ---------------------------------------------------------------- persistence


def write_once(path: Path, value: object) -> bool:
    """Atomic idempotent write. Identical content is a no-op, differing content is refused."""
    data = canonical(value)
    if path.exists():
        _need(path.read_bytes() == data, "artifact_conflict", path.name)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.link(tmp, path)  # fails if another writer won the race
    except FileExistsError:
        _need(path.read_bytes() == data, "artifact_conflict", path.name)
        return False
    finally:
        os.unlink(tmp)
    return True


@dataclass
class BenchmarkResult:
    manifest: FrozenManifest
    finding: DevelopmentFinding | None
    scorecard: Scorecard
    notes: list[str] = field(default_factory=list)
