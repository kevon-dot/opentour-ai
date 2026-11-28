"""OPE-526 steps 1-4: redacted run inventory, comparable groups, branch choice, decision record.

Pure functions over audit inputs and the receipts ``audit()`` returned. They read receipts and
inputs only; they never change a verdict and grant no authority. The document they build is
evidence *about* runs, labelled with the fixture kind it was produced from.

Redaction: no family id, label, label commitment or verifier identity is copied. Verifier
identities are reduced to short digests and counts; only aggregate counts, digests and
registered reason codes appear.

Evidence level: every run produced from the repository's generated sentinel fixtures is
``generated_fixture_evidence``. Such a run can never satisfy a real-population, provider or
customer-evidence gate, so the step-2 "audit, scope and precision are adequate" branch is
unreachable from it by construction.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from opentour_training.evaluation.benchmark_audit.audit import mapper_view
from opentour_training.evaluation.benchmark_audit.inference import families_needed_for_ceiling
from opentour_training.evaluation.benchmark_audit.model import (
    AuditInputs,
    Verdict,
    canonical_digest,
)

INVENTORY_SCHEMA = "opentour.ope526.run-inventory.v1"
GENERATED_KIND = "generated_sentinel"
EVIDENCE_LABEL = "generated_fixture_evidence"
EVIDENCE_NOTICE = (
    "Generated sentinel fixtures only. Local or simulated runs cannot satisfy real-population, "
    "provider or customer-evidence gates, and nothing here is an accuracy claim."
)
ROLES = ("baseline", "rerun", "mutation", "pack", "adverse")
_PROTECTED_HIDDEN = (
    "final_label_digest", "adjudicator_label_digest", "independent_label_digest",
    "adjudicator_id", "independent_source", "unresolved",
)  # fmt: skip
# Row-2 classes of the step-2 table: disagreement, future-only field, cross-split family.
ROW2_CODES = frozenset(
    {
        "label_overturned_by_independent_record",
        "label_error_exceeds_tolerance",
        "future_only_field",
        "family_crosses_split",
    }
)
BRANCHES = {
    "missing_audit": (
        "Missing audit, selective corrections or thin critical slices: scope the claim to what "
        "was sampled or return insufficient evidence; set a blinded, stratified adjudication "
        "plan against the intended population and collect the missing independent cases under "
        "the current grant."
    ),
    "disagreement_or_leak": (
        "Disagreement, future-only field or cross-split family: quarantine the affected "
        "version, trace examples to policy, decision time and family root, fix upstream, seal "
        "a successor and preserve the old verdict."
    ),
    "adequate": (
        "Audit, scope and precision are adequate: keep the benchmark unchanged, publish its "
        "audit receipt and proceed to independent result verification."
    ),
}
MISSING_FOR_EVERY_RUN = (
    "independent_customer_label_audit_receipt",
    "customer_approved_sampling_frame_and_tolerances",
    "second_qualified_adjudicator_or_independent_authoritative_record_outside_fixtures",
    "upstream_candidate_snapshot_receipt_ope_416",
    "receipt_issuer_signature_ope_566_ope_582",
    "independent_result_verification_ope_528",
    "real_stage_times_or_effects_beyond_local_audit_cpu_time",
)


@dataclass(frozen=True)
class Measurement:
    """Where and when a run was measured. Supplied by the generator, never by the receipt."""

    timestamp: str
    git_sha: str
    dirty: bool


@dataclass(frozen=True)
class RunSpec:
    name: str
    role: str
    expected_verdict: str
    rerun_of: str | None = None


def _short(value: str) -> str:
    return "sha256:" + canonical_digest(value)[:12]


def run_id(spec: RunSpec, receipt: Mapping[str, Any] | None) -> str:
    """Deterministic: the same run over the same inputs gets the same id."""
    digest = receipt["receipt_digest"][:12] if receipt is not None else "failed"
    return f"ope526-fixture-{spec.name}-{digest}"


def _missing(receipt: Mapping[str, Any], inputs: AuditInputs) -> list[str]:
    missing: list[str] = []
    critical = receipt["coverage"]["critical"]
    if critical["error_rate"] is None:
        missing.append("critical_error_rate")
    if receipt["inference_to_future_cases"]["upper_error"] is None:
        missing.append("overall_upper_error")
    if receipt["label_audit"]["design_weighted_error"] is None:
        missing.append("design_weighted_error")
    if inputs.prereg.sampling_design.value == "stratified_random" and any(
        r.sampling_probability is None
        for r in inputs.records
        if r.adjudicator_id or r.independent_source
    ):
        missing.append("sampling_probabilities")
    if receipt["upstream"]["candidate_snapshot"] is None:
        missing.append("candidate_snapshot_binding")
    if receipt["upstream"]["verification"] != "worker_verified":
        missing.append("upstream_snapshot_verification")
    if inputs.preprocessing is None:
        missing.append("preprocessing_revision")
    return missing


def inventory_row(
    spec: RunSpec,
    inputs: AuditInputs,
    receipt: Mapping[str, Any] | None,
    measurement: Measurement,
    *,
    elapsed_ms: float,
    failure: str | None = None,
    release_check: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """One redacted inventory row. ``receipt`` is ``None`` only for a failed run."""
    p = inputs.prereg
    base: dict[str, Any] = {
        "run_id": run_id(spec, receipt),
        "timestamp": measurement.timestamp,
        "measured_git_sha": measurement.git_sha,
        "dirty": measurement.dirty,
        "observed_audit_cpu_ms": round(elapsed_ms, 3),
        "population": spec.name,
        "role": spec.role,
        "rerun_of": spec.rerun_of,
        "evidence": {
            "fixture_kind": GENERATED_KIND,
            "evidence_label": EVIDENCE_LABEL,
            "evidence_origin_declared": p.evidence_origin,
            "evidence_origin_basis": "caller_asserted_in_preregistration",
            "real_population_gate_satisfied": False,
            "customer_scope": None,
        },
        "expected_verdict": spec.expected_verdict,
    }
    if receipt is None:
        return base | {
            "status": "failed",
            "failure": failure or "unknown",
            "held": True,
            "verdict": None,
            "expectation_met": False,
            "missing_fields": ["receipt"],
        }
    frozen, current = receipt["bindings"]["frozen"], receipt["bindings"]["current"]
    label_audit, claims = receipt["label_audit"], receipt["claims"]
    view = mapper_view(inputs.records)
    leaked_fields = sum(
        1
        for row, record in zip(view, inputs.records, strict=True)
        if record.split == "protected"
        for key in _PROTECTED_HIDDEN
        if key in row
    )
    inference = receipt["inference_to_future_cases"]
    return base | {
        "status": "completed",
        "held": receipt["verdict"] != Verdict.EVALUATION_ALLOWED.value,
        "verdict": receipt["verdict"],
        "reason_codes": sorted({r["code"] for r in receipt["reasons"]}),
        "expectation_met": receipt["verdict"] == spec.expected_verdict,
        "receipt": {
            "receipt_id": receipt["receipt_id"],
            "receipt_digest": receipt["receipt_digest"],
            "schema_version": receipt["schema_version"],
        },
        "sampling_frame": {
            "population_kind": p.population_kind.value,
            "population_frame": p.population_frame,
            "intended_decision": p.intended_decision,
            "design": p.sampling_design.value,
            "frame_digest": receipt["audit_selection"]["frame_digest"],
            "seed": p.sampling_seed,
            "sample_sizes": [list(s) for s in p.sample_sizes],
            "eligible_families": receipt["denominator_flow"]["eligible"],
        },
        "verifier": {
            "label_authority_digest": _short(p.label_authority),
            "independent_sources": len(label_audit["independent_sources"]),
            "independent_source_digests": [_short(s) for s in label_audit["independent_sources"]],
            "adjudicators": len(label_audit["adjudicators"]),
            "adjudicator_digests": [_short(a) for a in label_audit["adjudicators"]],
            "adjudicator_independence": receipt["audit_selection"]["adjudicator_independence"],
            "disagreement_matrix": label_audit["disagreement_matrix"],
        },
        "cutoff": {
            "frozen_day": p.frozen_day,
            "temporal_cutoff_day": p.temporal_cutoff_day,
            "maturity_window_days": p.maturity_window_days,
            "as_of_day": inputs.as_of_day,
            "candidate_outputs_revealed_day": inputs.candidate_outputs_revealed_day,
        },
        "split": {
            "development_families": sum(1 for r in inputs.records if r.split == "development"),
            "protected_families": sum(1 for r in inputs.records if r.split == "protected"),
            "split_manifest_unchanged": frozen["split_manifest"] == current["split_manifest"],
            "cross_split_overlaps": receipt["contamination"]["cross_split_overlaps"],
            "unknown_links_recorded": receipt["contamination"]["unknown_links_recorded"],
        },
        "answer_key_exposure": {
            "answer_key_unchanged": frozen["answer_key"] == current["answer_key"],
            "exposed_families_retired": receipt["contamination"]["exposed_families_retired"],
            "unlogged_disclosures_supplied": inputs.unlogged_disclosures,
            "protected_selection_uses": sum(
                1 for u in inputs.selection_log if u.partition == "protected"
            ),
            "mapper_view_protected_label_fields": leaked_fields,
        },
        "inputs_and_configuration": {
            "preregistration_digest": receipt["preregistration_digest"],
            "policy_revision": p.policy_revision,
            "scorer_version": p.scorer_version,
            "operating_point": p.operating_point,
            "interval_method": p.interval_method,
            "alpha": p.alpha,
            "acceptable_error_ceiling": p.acceptable_error_ceiling,
            "tolerable_label_error": p.tolerable_label_error,
            "min_critical_coverage": p.min_critical_coverage,
            "max_unresolved": p.max_unresolved,
            "binding_digests": {k: v[:12] for k, v in asdict(p.frozen).items()},
        },
        "metric": {
            "name": "one_sided_exact_upper_label_error_per_independent_family",
            "errors": inference["errors"],
            "denominator_families": inference["families"],
            "upper_error": inference["upper_error"],
            "ceiling": inference["ceiling"],
            "conclusion": inference["conclusion"],
            "fixture_agreement": receipt["fixture_agreement"],
        },
        "denominator": receipt["denominator_flow"],
        "coverage": {
            "routine": receipt["coverage"]["routine"]["label"],
            "critical": receipt["coverage"]["critical"]["label"],
            "critical_status": receipt["coverage"]["critical"]["status"],
        },
        "claims": {
            "overall_accuracy_claim": claims["overall_accuracy_claim"],
            "error_family_claim": claims["error_family_claim"],
            "critical_inclusive_claim": claims["critical_inclusive_claim"],
            "customer_accuracy_alert_permitted": claims["customer_accuracy_alert_permitted"],
            "broad_improvement_alert_permitted": claims["broad_improvement_alert_permitted"],
            "alerts_emitted": claims["overall_accuracy_alerts_emitted"]
            + claims["broad_improvement_alerts_emitted"],
        },
        "release_check": dict(release_check) if release_check is not None else None,
        "missing_fields": _missing(receipt, inputs),
    }


def group_runs(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Comparable = same population kind, frame, intended decision, policy and scorer."""
    groups: dict[str, dict[str, Any]] = {}
    for row in rows:
        frame = row.get("sampling_frame")
        config = row.get("inputs_and_configuration")
        if frame is None or config is None:
            key = ["failed", row["population"]]
        else:
            key = [
                frame["population_kind"],
                frame["population_frame"],
                frame["intended_decision"],
                config["policy_revision"],
                config["scorer_version"],
            ]
        gid = "group-" + canonical_digest(key)[:12]
        group = groups.setdefault(gid, {"group_id": gid, "comparable_on": key, "run_ids": []})
        group["run_ids"].append(row["run_id"])
    by_id = {r["run_id"]: r for r in rows}
    for group in groups.values():
        members = [by_id[i] for i in group["run_ids"]]
        baselines = [m for m in members if m["role"] == "baseline" and m["status"] == "completed"]
        group["baseline_run_id"] = baselines[0]["run_id"] if baselines else None
        group["comparable_baseline_present"] = bool(baselines)
        group["next_step_if_missing"] = (
            None if baselines else "instrument_or_run_smallest_authorized_baseline_first"
        )
    return sorted(groups.values(), key=lambda g: g["group_id"])


def _unsafe(row: Mapping[str, Any]) -> list[str]:
    found = []
    if row["status"] != "completed":
        found.append("run_failed")
    if not row["expectation_met"]:
        found.append("verdict_differs_from_hand_checked_expectation")
    claims = row.get("claims") or {}
    if claims.get("alerts_emitted", 0):
        found.append("alert_emitted")
    if row.get("verdict") != Verdict.EVALUATION_ALLOWED.value and (
        claims.get("customer_accuracy_alert_permitted")
        or claims.get("broad_improvement_alert_permitted")
    ):
        found.append("alert_permitted_on_held_population")
    if (row.get("release_check") or {}).get("release_permitted"):
        found.append("release_permitted_without_signed_receipt")
    return found


def select_branch(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Pick the first applicable step-2 branch. Unsafe effects precede the table."""
    unsafe = {r["run_id"]: codes for r in rows if (codes := _unsafe(r))}
    if unsafe:
        return {
            "branch": "unsafe_effect_hold",
            "action": "Hold every claim and fix the audit software before choosing a table row.",
            "triggers": unsafe,
        }
    baselines = [r for r in rows if r["role"] == "baseline"]
    independent = [r for r in rows if r["evidence"]["fixture_kind"] != GENERATED_KIND]
    triggers: dict[str, Any] = {}
    if not independent:
        triggers["no_independent_customer_label_audit"] = (
            f"0 of {len(rows)} runs carry independent customer evidence; all are {GENERATED_KIND}"
        )
    for r in baselines:
        found = set(r.get("reason_codes", ()))
        if "correction_only_sample" in found:
            triggers.setdefault("selective_corrections", []).append(r["run_id"])
        if "critical_coverage_gap" in found or r["coverage"]["critical_status"] != "measured":
            triggers.setdefault("thin_critical_slices", []).append(r["run_id"])
    if triggers:
        return {
            "branch": "missing_audit",
            "action": BRANCHES["missing_audit"],
            "triggers": triggers,
        }
    leaks = {
        r["run_id"]: sorted(set(r.get("reason_codes", ())) & ROW2_CODES)
        for r in baselines
        if set(r.get("reason_codes", ())) & ROW2_CODES
    }
    if leaks:
        return {
            "branch": "disagreement_or_leak",
            "action": BRANCHES["disagreement_or_leak"],
            "triggers": leaks,
        }
    return {"branch": "adequate", "action": BRANCHES["adequate"], "triggers": {}}


def injected_findings(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Row-2-class codes in deliberately mutated runs, with whether each was caught."""
    found = []
    for r in rows:
        codes = sorted(set(r.get("reason_codes", ())) & ROW2_CODES)
        if codes and r["role"] != "baseline":
            found.append(
                {
                    "run_id": r["run_id"],
                    "codes": codes,
                    "disposition": "detected_as_designed"
                    if r["expectation_met"]
                    else "not_as_expected",
                }
            )
    return found


def decision_record(
    rows: Sequence[Mapping[str, Any]],
    branch: Mapping[str, Any],
    measurement: Measurement,
    *,
    baseline_population: str = "baseline",
) -> dict[str, Any]:
    """Step 4. Names the baseline and rerun ids and compares expected with observed."""
    by_name = {r["population"]: r for r in rows}
    baseline = by_name.get(baseline_population)
    if baseline is not None and baseline["role"] != "baseline":
        baseline = None
    reruns = [r for r in rows if r["role"] == "rerun" and r["rerun_of"] == baseline_population]
    held = [r for r in rows if r["held"]]
    invalid_like = [r for r in rows if r["role"] in {"mutation", "pack", "adverse"}]
    ceiling = baseline["metric"]["ceiling"] if baseline and "metric" in baseline else 0.05
    alpha = (
        baseline["inputs_and_configuration"]["alpha"]
        if baseline and "inputs_and_configuration" in baseline
        else 0.05
    )
    observed = {
        "baseline_verdict": baseline["verdict"] if baseline else None,
        "baseline_customer_accuracy_alert_permitted": (
            baseline["claims"]["customer_accuracy_alert_permitted"]
            if baseline and "claims" in baseline
            else None
        ),
        "baseline_upper_error": baseline["metric"]["upper_error"]
        if baseline and "metric" in baseline
        else None,
        "rerun_digests_match_baseline": bool(reruns)
        and all(
            baseline is not None
            and "receipt" in r
            and r["receipt"]["receipt_digest"] == baseline["receipt"]["receipt_digest"]
            for r in reruns
        ),
        "held_or_invalid_populations_with_alert_permitted": sum(
            1
            for r in invalid_like
            if r.get("claims", {}).get("customer_accuracy_alert_permitted")
            or r.get("claims", {}).get("broad_improvement_alert_permitted")
        ),
        "runs_matching_hand_checked_verdict": sum(1 for r in rows if r["expectation_met"]),
        "runs": len(rows),
        "release_checks_permitting_release": sum(
            1 for r in rows if (r.get("release_check") or {}).get("release_permitted")
        ),
    }
    expected = {
        "baseline_verdict": Verdict.EVALUATION_ALLOWED.value,
        "baseline_customer_accuracy_alert_permitted": False,
        "baseline_upper_error": "greater_than_ceiling_so_inconclusive_for_real_inference",
        "rerun_digests_match_baseline": True,
        "held_or_invalid_populations_with_alert_permitted": 0,
        "runs_matching_hand_checked_verdict": len(rows),
        "runs": len(rows),
        "release_checks_permitting_release": 0,
    }
    upper = observed["baseline_upper_error"]
    met = (
        all(observed[k] == expected[k] for k in expected if k != "baseline_upper_error")
        and isinstance(upper, float)
        and upper > ceiling
    )
    return {
        "baseline_run_id": baseline["run_id"] if baseline else None,
        "rerun_ids": [r["run_id"] for r in reruns],
        "hypothesis": (
            "The 12-family generated fixture supports only fixture agreement, never customer "
            "accuracy. Scoping its claim to that and returning insufficient evidence for any "
            "real-population claim, until a blinded, stratified independent label audit of the "
            "intended population exists, is the correct disposition. Rescoring identical inputs "
            "reproduces the verdict, and every held or invalid population permits no alert."
        ),
        "change_revision": measurement.git_sha,
        "change_revision_dirty": measurement.dirty,
        "change_under_test": (
            "No benchmark or label change. This revision adds the run inventory and the "
            "worker-side release check of the audit receipt."
        ),
        "expected": expected,
        "observed": observed,
        "expectation_met": met,
        "step3_rerun": {
            "fresh_blinded_audit_sample": (
                "not_run: no repair was made from prior findings and no independent population "
                "exists to sample; a fresh sample needs the customer-approved frame"
            ),
            "denominator_reconciliation": all(
                r["denominator"]["reconciles"] == 1 for r in rows if "denominator" in r
            ),
            "overlap_checks": "recomputed_in_every_run",
            "critical_slice_coverage": baseline["coverage"]["critical"]
            if baseline and "coverage" in baseline
            else None,
            "corrected_disputed_excluded_unresolved": {
                k: baseline["denominator"][k]
                for k in ("correction", "disputed", "excluded", "unresolved")
            }
            if baseline and "denominator" in baseline
            else None,
            "scorer_run_alone_lifts_hold": False,
        },
        "trade_offs": [
            "Holding the real-population claim delays any customer accuracy alert until "
            "independent evidence exists; the starter and fixture-agreement views stay "
            "available.",
            "The release check refuses or holds every receipt today, because receipts are "
            "digest-consistent only and unsigned.",
        ],
        "evidence_level": EVIDENCE_LABEL,
        "decision": "hold_for_specified_evidence",
        "software_disposition": "keep_current_behavior",
        "branch": branch["branch"],
        "held_for": list(MISSING_FOR_EVERY_RUN),
        "preserved_unsuccessful_trials": [r["run_id"] for r in held],
        "next_iteration": {
            "justified": True,
            "measured_cause": (
                "no independent customer label-audit receipt exists for any eligible run"
            ),
            "owner": "named customer evidence owner (sampling frame, tolerances, adjudicators)",
            "stop_condition": (
                "a blinded, stratified independent audit of the customer-approved frame whose "
                "critical strata meet the registered coverage and whose exact upper error is at "
                f"or below the ceiling; at zero errors that needs "
                f"{families_needed_for_ceiling(ceiling, alpha)} independent families per "
                "claimed stratum; or the owner declines, which keeps the hold"
            ),
        },
    }
