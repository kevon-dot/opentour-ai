"""EVE held-out baseline and simple-repair benchmark (OPE-555), pure CPU.

Everything here is deterministic arithmetic over supplied records. A result from this
module is only as strong as its ``evidence_tier``: generated fixtures prove software
behaviour, never real EVE capacity, provider authority or Pi School outcomes.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from opentour_training.evaluation.statistics_kernel import (
    binomial_lower_bound,
    binomial_upper_bound,
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
    "observed_net_fixes",
    "no_net_fixes",
]
Disposition = Literal[
    "benchmark_only", "adopt_test_simple_fix", "consider_tuning", "gather_more_evidence"
]
DISPOSITIONS: frozenset[str] = frozenset(
    {"benchmark_only", "adopt_test_simple_fix", "consider_tuning", "gather_more_evidence"}
)
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


def freeze_manifest(
    *,
    benchmark_id: str,
    evidence_tier: EvidenceTier,
    development_cases: Sequence[EveCase],
    heldout_cases: Sequence[EveCase],
    heldout_exclusions: Sequence[ExclusionRecord],
    heldout_key: Sequence[ReferenceJudgment],
    arms: Sequence[ArmConfig],
    rubric_version: str,
    rubric_text: str,
    alpha: float,
    materiality_min_errors: int,
    min_recurring_families: int,
    adjudicator_kind: Literal["generated_fixture", "pi_school_domain_reviewer"],
    ground_truth_audit_receipt_ref: str | None = None,
) -> FrozenManifest:
    """Freeze cases, families, rubric, scorer, arms and thresholds BEFORE any output exists."""
    _text(benchmark_id, "benchmark_id_invalid")
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
    _need(all(excl.values()), "exclusion_reason_missing")
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
            "case_digest": digest(sorted([c.case_id, c.family_id] for c in development_cases)),
        },
        "heldout": {
            "case_ids": sorted(held_ids),
            "family_ids": sorted(held_fams),
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
    for row in fit_rows:
        n_fit += 1
        if row.case_id in held_cases:
            violations.append(f"fit_row_heldout_case:{row.case_id}")
        if row.family_id in held_fams:
            violations.append(f"fit_row_heldout_family:{row.family_id}")
        if any(s in row.text for s in secrets):
            violations.append(f"fit_row_protected_answer:{row.case_id}")
    for text in prompt_texts:
        n_prompt += 1
        if any(s in text for s in secrets):
            violations.append(f"prompt_protected_answer:{n_prompt}")
    return IsolationReceipt(manifest.manifest_digest, n_fit, n_prompt, tuple(sorted(violations)))


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
    return DevelopmentFinding(
        manifest.manifest_digest,
        sum(1 for c in scorable if b[c] == "serious_error"),
        len(residual),
        len(scorable),
        fixes,
        regs,
        fams,
        material,
    )


# ---------------------------------------------------------------- held-out scorecard


@dataclass(frozen=True)
class Scorecard:
    body: Mapping[str, Any]
    scorecard_digest: str

    def to_json(self) -> dict[str, Any]:
        return {"body": dict(self.body), "scorecard_digest": self.scorecard_digest}


def _redact(manifest_digest: str, case_id: str) -> str:
    return "case:" + hashlib.sha256(f"{manifest_digest}|{case_id}".encode()).hexdigest()[:16]


def score_heldout(
    manifest: FrozenManifest,
    *,
    heldout_key: Sequence[ReferenceJudgment],
    isolation: IsolationReceipt,
    runs: Sequence[ArmRun],
) -> Scorecard:
    """Score the frozen held-out cases. Refuses unless freeze, key seal and isolation hold."""
    verify_manifest(manifest)
    _need(isolation.manifest_digest == manifest.manifest_digest, "isolation_not_bound_to_manifest")
    _need(isolation.clean, "isolation_violation", ",".join(isolation.violations))
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
                "note": "descriptive uncertainty only; no significance claim is made",
            },
            "fix_case_refs": [_redact(manifest.manifest_digest, c) for c in fixes],
            "regression_case_refs": [_redact(manifest.manifest_digest, c) for c in regs],
        }
        claim = "observed_net_fixes" if len(fixes) > len(regs) else "no_net_fixes"
    tier = manifest.body["evidence_tier"]
    audit = manifest.body["ground_truth_audit_receipt_ref"]
    gates = list(UNSATISFIED_EXTERNAL_GATES_GENERATED) if tier == "generated" else []
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
            "excluded": manifest.body["heldout"]["excluded"],
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
        "accuracy_claim_eligible": bool(audit) and not gates,
        "unsatisfied_external_gates": gates,
        "claim_scope": "implementation_only" if tier == "generated" else tier,
    }
    return Scorecard(body=body, scorecard_digest=digest(body))


# ---------------------------------------------------------------- disposition


def record_disposition(
    scorecard: Scorecard,
    finding: DevelopmentFinding | None,
    *,
    disposition: str,
    owner_id: str,
    recorded_at: str,
) -> dict[str, Any]:
    """The named owner records exactly one disposition, bound to the scorecard digest."""
    _need(disposition in DISPOSITIONS, "disposition_unknown", disposition)
    _text(owner_id, "owner_missing")
    _text(recorded_at, "recorded_at_missing")
    claim = scorecard.body["improvement_claim"]
    if disposition == "adopt_test_simple_fix":
        _need(claim == "observed_net_fixes", "adopt_requires_observed_net_fixes", claim)
    if disposition == "consider_tuning":
        _need(finding is not None, "tuning_requires_development_finding")
        assert finding is not None
        _need(
            finding.manifest_digest == scorecard.body["manifest_digest"],
            "finding_not_bound_to_manifest",
        )
        _need(finding.material_recurring_residual, "tuning_requires_material_recurring_residual")
    record = {
        "schema": DISPOSITION_SCHEMA,
        "scorecard_digest": scorecard.scorecard_digest,
        "manifest_digest": scorecard.body["manifest_digest"],
        "disposition": disposition,
        "owner_id": owner_id,
        "recorded_at": recorded_at,
        "fit_authorized": False,
    }
    return {**record, "record_digest": digest(record)}


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
