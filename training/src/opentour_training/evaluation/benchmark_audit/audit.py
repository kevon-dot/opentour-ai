"""Compute the version-bound benchmark audit receipt and one of six verdicts.

Pure and deterministic: identical inputs reproduce the identical receipt digest. The audit never
sees a protected label, only salted commitments, and the receipt carries counts and digests only.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, replace
from typing import Any

from opentour_training.evaluation.benchmark_audit.evidence_requests import (
    ALERT_EVIDENCE,
    NEXT_EVIDENCE,
)
from opentour_training.evaluation.benchmark_audit.exposure import replay_exposure
from opentour_training.evaluation.benchmark_audit.inference import (
    design_weighted_error,
    families_needed_for_ceiling,
    upper_error,
)
from opentour_training.evaluation.benchmark_audit.model import (
    CANDIDATE_SNAPSHOT_SCHEMA,
    RECEIPT_SCHEMA,
    AuditInputs,
    BindingDigests,
    CandidateSnapshotBinding,
    FamilyRecord,
    PopulationKind,
    Reason,
    SamplingDesign,
    Verdict,
    canonical_digest,
)

_HEX64 = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class _Row:
    record: FamilyRecord
    status: str  # excluded | immature | verified | unresolved | unverified
    verifier: str | None
    verifier_label: str | None
    mismatch: bool


def label_manifest_digest(records: Iterable[FamilyRecord]) -> str:
    return canonical_digest(sorted((r.family_id, r.final_label_digest) for r in records))


def split_manifest_digest(records: Iterable[FamilyRecord]) -> str:
    return canonical_digest(sorted((r.family_id, r.split) for r in records))


def draw_audit_sample(
    frame: Sequence[tuple[str, str]], sizes: Sequence[tuple[str, int]], seed: int
) -> frozenset[str]:
    """Reproducible stratified draw: rank by SHA-256(seed, id) inside each stratum."""
    chosen: set[str] = set()
    for stratum, size in sizes:
        members = sorted(
            (hashlib.sha256(f"{seed}\x1f{fid}".encode()).hexdigest(), fid)
            for fid, kind in frame
            if kind == stratum
        )
        chosen.update(fid for _, fid in members[:size])
    return frozenset(chosen)


def _verifier(r: FamilyRecord) -> tuple[str | None, str | None]:
    if r.independent_source and r.independent_label_digest:
        return "independent_record", r.independent_label_digest
    if (
        r.adjudicator_id
        and r.adjudicator_label_digest
        and r.adjudicator_id != r.first_labeler_id
        and not r.adjudicator_saw_first_label
        and not r.adjudicator_saw_predictions
    ):
        return "blinded_adjudicator", r.adjudicator_label_digest
    return None, None


def _classify(inputs: AuditInputs, exposed: frozenset[str]) -> list[_Row]:
    prereg = inputs.prereg
    rows: list[_Row] = []
    for r in inputs.records:
        if r.split != "protected":
            continue
        excluded = (
            r.excluded_reason is not None
            or r.decided_day > prereg.temporal_cutoff_day
            or r.family_id in exposed
        )
        kind, label = _verifier(r)
        if excluded:
            status = "excluded"
        elif r.decided_day + prereg.maturity_window_days > inputs.as_of_day:
            status = "immature"
        elif label is not None:
            status = "verified"
        elif r.unresolved:
            status = "unresolved"
        else:
            status = "unverified"
        rows.append(
            _Row(r, status, kind, label, label is not None and label != r.final_label_digest)
        )
    return rows


def _exclusion_reason(r: FamilyRecord, prereg_cutoff: int, exposed: frozenset[str]) -> str:
    if r.family_id in exposed:
        return "exposed_answer_key_retired"
    if r.excluded_reason is not None:
        return r.excluded_reason
    return "after_temporal_cutoff" if r.decided_day > prereg_cutoff else "other"


def _units(rows: Sequence[_Row]) -> tuple[int, int]:
    """Collapse to independent units by group root; a unit errs if any member mismatches."""
    errs: dict[str, bool] = defaultdict(bool)
    for row in rows:
        key = row.record.group_root or row.record.family_id
        errs[key] = errs[key] or row.mismatch
    return len(errs), sum(errs.values())


def _overlaps(records: Sequence[FamilyRecord]) -> tuple[dict[str, int], int]:
    keys = ("group_root", "exact_hash", "template_group", "attachment_hash", "ancestry_root")
    development = [r for r in records if r.split == "development"]
    protected = [r for r in records if r.split == "protected"]
    found: dict[str, int] = {}
    for key in keys:
        shared = {getattr(r, key) for r in development} & {getattr(r, key) for r in protected}
        shared.discard(None)
        found[key] = len(shared)
    unknown = sum(1 for r in records for k in keys if getattr(r, k) is None)
    return found, unknown


_WORKER_DERIVED = "worker_derived_from_exposure_log"


def _upstream_receipt_verified_by_worker(snapshot: CandidateSnapshotBinding | None) -> bool:
    """Whether the worker itself verified the OPE-416 verified-candidate snapshot receipt.

    Held gap: OPE-416 has not landed, so no verifiable upstream receipt type or issuer key exists
    and nothing here can verify one. A well-formed binding is only an opaque reference, and the
    caller's ``upstream_snapshot_verified`` boolean is deliberately not read. Until a real
    verifier lands, this is always false and the customer accuracy alert stays off.
    """
    del snapshot
    return False


@dataclass(frozen=True)
class _Exposure:
    families: frozenset[str]
    unlogged: int
    basis: str
    scope_ok: bool


def _exposure(inputs: AuditInputs) -> _Exposure:
    """Exposure the worker derives from the guard's release history, plus caller additions.

    A caller's list or count can only add to what the replayed history implies. Without a
    history, exposure is caller-asserted and the receipt says so.
    """
    record = inputs.exposure_record
    if record is None:
        return _Exposure(
            inputs.exposed_families, inputs.unlogged_disclosures, "caller_asserted", True
        )
    derived, unlogged = replay_exposure(record)
    protected = {r.family_id for r in inputs.records if r.split == "protected"}
    return _Exposure(
        inputs.exposed_families | derived,
        max(inputs.unlogged_disclosures, unlogged),
        _WORKER_DERIVED,
        protected <= record.universe,
    )


def _independence(rows: Sequence[_Row]) -> tuple[str, dict[str, int]]:
    """State blinding only when every recorded adjudication meets the independence conditions."""
    adjudicated = [r.record for r in rows if r.record.adjudicator_id]
    counts = {
        "adjudicated": len(adjudicated),
        "distinct_from_first_labeler": sum(
            1 for r in adjudicated if r.adjudicator_id != r.first_labeler_id
        ),
        "recorded_blind_to_first_label": sum(
            1 for r in adjudicated if not r.adjudicator_saw_first_label
        ),
        "recorded_blind_to_predictions": sum(
            1 for r in adjudicated if not r.adjudicator_saw_predictions
        ),
    }
    if not adjudicated:
        return "no_adjudication_recorded", counts
    if all(v == len(adjudicated) for v in counts.values()):
        return "blinded_to_first_label_and_predictions", counts
    return "independence_not_shown_for_every_adjudication", counts


def _preregistration_gaps(inputs: AuditInputs) -> list[str]:
    p = inputs.prereg
    texts = (
        p.population_frame, p.intended_decision, p.policy_revision, p.label_authority,
        p.grouping_method, p.scorer_version, p.operating_point, p.interval_method,
        p.disagreement_rule,
    )  # fmt: skip
    gaps = [f"text_{i}" for i, t in enumerate(texts) if not t.strip()]
    if not p.feature_contract:
        gaps.append("feature_contract")
    if not p.critical_slices:
        gaps.append("critical_slices")
    if p.independent_unit != "independent_family":
        gaps.append("independent_unit")
    if not 0.0 < p.alpha < 1.0 or not 0.0 < p.acceptable_error_ceiling < 1.0:
        gaps.append("tolerances")
    return gaps


def audit(inputs: AuditInputs) -> dict[str, Any]:
    """Return the audit receipt. ``verdict`` is one of the six registered verdict strings."""
    p = inputs.prereg
    exposure = _exposure(inputs)
    exposed = exposure.families
    rows = _classify(inputs, exposed)
    by_status: dict[str, list[_Row]] = defaultdict(list)
    for row in rows:
        by_status[row.status].append(row)
    # Label-quality gates read every protected family, exposure-retired or not: retiring a
    # family must not launder its disputed label or its unresolved ambiguity.
    gate_rows = _classify(inputs, frozenset())
    gate_verified = [r for r in gate_rows if r.status == "verified"]
    gate_unresolved = sum(1 for r in gate_rows if r.status == "unresolved")
    verified = by_status["verified"]
    mature = verified + by_status["unresolved"] + by_status["unverified"]
    invalid: list[Reason] = []
    stale: list[Reason] = []
    insufficient: list[Reason] = []
    limited: list[Reason] = []

    # ---- Invalid evidence first ----
    if inputs.candidate_outputs_revealed_day is not None and (
        p.frozen_day >= inputs.candidate_outputs_revealed_day
    ):
        invalid.append(Reason("preregistration_after_reveal", "frozen on or after output reveal"))
    if gaps := _preregistration_gaps(inputs):
        invalid.append(Reason("preregistration_incomplete", ",".join(gaps)))
    ids = [r.family_id for r in inputs.records]
    if len(ids) != len(set(ids)):
        invalid.append(Reason("duplicate_family_id", "family ids are not unique"))
    overlaps, unknown_links = _overlaps(inputs.records)
    if any(overlaps.values()):
        shared = ",".join(f"{k}={v}" for k, v in overlaps.items() if v)
        invalid.append(Reason("family_crosses_split", shared))
    declared = {f.name: f.available_at for f in p.feature_contract}
    future = [
        f for f in inputs.consumed_features if declared.get(f, "post_decision") == "post_decision"
    ]
    leaked_inputs = sorted({f for r in inputs.records for f in r.post_decision_fields_in_input})
    if future or leaked_inputs:
        invalid.append(
            Reason("future_only_field", ",".join(sorted(set(future) | set(leaked_inputs))))
        )
    if any(use.partition == "protected" for use in inputs.selection_log):
        invalid.append(Reason("protected_comparison_used_for_selection", "protected partition"))
    pre = inputs.preprocessing
    if pre is not None and pre.fit_partition != "development":
        invalid.append(Reason("preprocessing_fit_on_protected", pre.revision))
    elif pre is not None and pre.applied_to_protected_revision != pre.revision:
        invalid.append(Reason("preprocessing_not_verified", pre.revision))
    if exposure.unlogged > 0:
        invalid.append(Reason("unlogged_case_level_disclosure", str(exposure.unlogged)))
    if p.sampling_design is SamplingDesign.STRATIFIED_RANDOM:
        frame = [(r.record.family_id, r.record.stratum) for r in rows]
        expected = draw_audit_sample(frame, p.sample_sizes, p.sampling_seed)
        selected = frozenset(
            r.record.family_id
            for r in rows
            if r.record.adjudicator_id or r.record.independent_source
        )
        if selected != expected:
            invalid.append(Reason("audit_sample_not_reproducible", "selection differs from draw"))

    unit_n, unit_errors = _units(verified)
    verified_n = len(verified)
    disputed = [r for r in verified if r.mismatch]
    overturned = [
        r for r in disputed
        if r.verifier == "independent_record"
        and r.record.adjudicator_label_digest == r.record.final_label_digest
    ]  # fmt: skip
    gate_overturned = [
        r for r in gate_verified
        if r.mismatch and r.verifier == "independent_record"
        and r.record.adjudicator_label_digest == r.record.final_label_digest
    ]  # fmt: skip
    gate_units, gate_errors = _units(gate_verified)

    # ---- Staleness: version binding ----
    current = replace(
        inputs.current,
        split_manifest=split_manifest_digest(inputs.records),
        label_manifest=label_manifest_digest(inputs.records),
    )
    changed = sorted(k for k, v in asdict(p.frozen).items() if asdict(current)[k] != v)
    if changed:
        stale.append(Reason("bound_digest_changed", ",".join(changed)))
    if inputs.superseded_by is not None:
        stale.append(Reason("superseded", inputs.superseded_by))

    # Verifier agreement is judged against the frozen label manifest. After a post-freeze
    # relabel the old verifications no longer speak to the new labels: the receipt is stale.
    if "label_manifest" not in changed:
        if gate_overturned:
            invalid.append(
                Reason("label_overturned_by_independent_record", f"{len(gate_overturned)} families")
            )
        elif gate_units and gate_errors / gate_units > p.tolerable_label_error:
            invalid.append(Reason("label_error_exceeds_tolerance", f"{gate_errors}/{gate_units}"))

    # ---- Coverage, unresolved and precision ----
    # A retired (exposed) critical family still counts against coverage: retiring it must not
    # silently shrink the critical denominator.
    mature_critical = [
        r
        for r in rows
        if r.record.stratum == "critical" and (r in mature or r.record.family_id in exposed)
    ]
    verified_critical = [r for r in verified if r.record.stratum == "critical"]
    routine_verified = [r for r in verified if r.record.stratum == "routine"]
    targets = dict(p.sample_sizes) if p.sampling_design is SamplingDesign.STRATIFIED_RANDOM else {}
    mature_routine = [r for r in mature if r.record.stratum == "routine"]
    critical_total = targets.get("critical", len(mature_critical))
    routine_total = targets.get("routine", len(mature_routine))
    critical_verified = len(verified_critical)
    critical_required = critical_total > 0 and (
        critical_verified / critical_total < p.min_critical_coverage
    )
    n_all, e_all = unit_n, unit_errors
    n_critical, e_critical = _units(verified_critical)
    upper_all = upper_error(e_all, n_all, p.alpha)
    upper_critical = upper_error(e_critical, n_critical, p.alpha)
    unresolved_n = len(by_status["unresolved"])
    if not exposure.scope_ok:
        insufficient.append(Reason("exposure_log_scope_mismatch", "protected family not covered"))
    correction_only = bool(verified) and (
        p.sampling_design is SamplingDesign.CORRECTION_ONLY
        or all(r.record.outcome_kind == "correction" for r in verified)
    )
    if any(r.record.group_root is None for r in rows):
        insufficient.append(Reason("grouping_unknown", "family without group root"))
    if p.sampling_design is not SamplingDesign.POLICY_ONLY:
        if not verified:
            insufficient.append(Reason("no_verified_families", "empty audit or no verifier"))
        if gate_unresolved > p.max_unresolved:
            insufficient.append(Reason("unresolved_ambiguity", f"{gate_unresolved} unresolved"))
        if pre is None:
            insufficient.append(Reason("preprocessing_revision_missing", "none persisted"))
        snap = inputs.candidate_snapshot
        if p.evidence_origin == "observed" and (
            snap is None
            or snap.schema_version != CANDIDATE_SNAPSHOT_SCHEMA
            or not _HEX64.fullmatch(snap.snapshot_digest)
        ):
            insufficient.append(Reason("candidate_snapshot_receipt_missing", "OPE-416 binding"))
        if inputs.successor_of is not None and any(
            not r.record.fresh_confirmation for r in verified
        ):
            insufficient.append(Reason("fresh_independent_confirmation_missing", "successor"))
    real = p.population_kind is PopulationKind.REAL_SAMPLE
    precision_ok = upper_all is not None and upper_all <= p.acceptable_error_ceiling
    if n_critical:
        precision_ok = precision_ok and (
            upper_critical is not None and upper_critical <= p.acceptable_error_ceiling
        )
    if real and verified and not precision_ok and not correction_only:
        insufficient.append(Reason("precision_cannot_rule_out_ceiling", f"upper={upper_all}"))
    if correction_only:
        limited.append(Reason("correction_only_sample", "error-family claim only"))
    elif critical_required:
        limited.append(Reason("critical_coverage_gap", f"{critical_verified}/{critical_total}"))
    if p.sampling_design is SamplingDesign.POLICY_ONLY:
        starter = [Reason("policy_only_starter", "no outcome history")]
    else:
        starter = []

    # ---- Verdict ----
    verdict, reasons = _decide(invalid, stale, starter, insufficient, limited, bool(verified))
    ceiling = p.acceptable_error_ceiling
    requests = [NEXT_EVIDENCE[r.code] for r in reasons if r.code in NEXT_EVIDENCE]
    if verdict is Verdict.INSUFFICIENT_EVIDENCE and any(
        r.code == "precision_cannot_rule_out_ceiling" for r in reasons
    ):
        need = families_needed_for_ceiling(ceiling, p.alpha)
        requests.append(f"At least {need} independent families with zero errors are needed.")

    flow = _flow(inputs, rows, by_status, mature, verified, disputed)
    weighted = (
        design_weighted_error(
            [r.mismatch for r in verified], [r.record.sampling_probability for r in verified]
        )
        if p.sampling_design is SamplingDesign.STRATIFIED_RANDOM
        else None
    )
    matches = sum(1 for r in verified if not r.mismatch)
    independence, independence_counts = _independence(rows)
    eligible = verdict is Verdict.EVALUATION_ALLOWED and real and precision_ok
    # The alert needs worker-held evidence, never a caller boolean: an upstream receipt the
    # worker verified itself and exposure replayed from a history covering every family.
    alert_blockers: list[str] = []
    if not eligible:
        alert_blockers.append("overall_accuracy_not_eligible")
    if p.evidence_origin != "observed":
        alert_blockers.append("evidence_origin_not_observed")
    if not _upstream_receipt_verified_by_worker(inputs.candidate_snapshot):
        alert_blockers.append("upstream_receipt_not_worker_verified")
    if exposure.basis != _WORKER_DERIVED or not exposure.scope_ok:
        alert_blockers.append("exposure_not_derived_from_covering_history")
    alert_ok = not alert_blockers
    if eligible:
        requests.extend(ALERT_EVIDENCE[b] for b in alert_blockers if b in ALERT_EVIDENCE)
    # Caller assertions a permitted claim currently rests on; the worker cannot verify these.
    dependencies: set[str] = set()
    if eligible or alert_ok:
        if exposure.basis == "caller_asserted":
            dependencies.update({"exposed_families", "unlogged_disclosures"})
        if any(r.verifier == "blinded_adjudicator" for r in verified):
            dependencies.add("adjudicator_blinding_flags")
    if alert_ok:
        dependencies.add("evidence_origin")
    receipt: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA,
        "verdict": verdict.value,
        "reasons": [asdict(r) for r in reasons],
        "next_evidence_request": requests,
        "evidence_origin": p.evidence_origin,
        "population_kind": p.population_kind.value,
        "preregistration_digest": p.digest(),
        "bindings": {"frozen": asdict(p.frozen), "current": asdict(current)},
        "denominator_flow": flow,
        "label_audit": {
            "independent_sources": sorted(
                {r.record.independent_source or "" for r in verified} - {""}
            ),
            "adjudicators": sorted({r.record.adjudicator_id or "" for r in verified} - {""}),
            "blinded_verification_required": True,
            "disagreement_matrix": {
                "agree": matches,
                "disagree": len(disputed),
                "overturned_after_two_reviewers_agreed": len(overturned),
                "unresolved": unresolved_n,
            },
            "raw_error": {"errors": e_all, "families": n_all},
            "design_weighted_error": weighted,
            "design_weighted_status": (
                "available" if weighted is not None else "unavailable_or_not_applicable"
            ),
            "tolerable_label_error": p.tolerable_label_error,
            "disagreement_rule": p.disagreement_rule,
            "label_quality_gate": {
                "basis": "all_protected_families_including_exposure_retired",
                "overturned": len(gate_overturned),
                "errors": gate_errors,
                "families": gate_units,
                "unresolved": gate_unresolved,
            },
        },
        "coverage": {
            "routine": {
                "verified": len(routine_verified),
                "total": routine_total,
                "label": f"{len(routine_verified)}/{routine_total}",
            },
            "critical": {
                "verified": critical_verified,
                "total": critical_total,
                "label": f"{critical_verified}/{critical_total}",
                "error_rate": (e_critical / n_critical) if n_critical else None,
                "upper_error": upper_critical,
                "status": "unavailable" if not n_critical else "measured",
            },
        },
        "contamination": {
            "cross_split_overlaps": overlaps,
            "unknown_links_recorded": unknown_links,
            "exposed_families_retired": len(exposed),
        },
        "fixture_agreement": {"matches": matches, "verified": verified_n},
        "inference_to_future_cases": {
            "applicable": real,
            "sampling_unit": p.independent_unit,
            "interval_method": p.interval_method,
            "alpha": p.alpha,
            "errors": e_all,
            "families": n_all,
            "upper_error": upper_all,
            "ceiling": ceiling,
            "conclusion": (
                "unavailable"
                if upper_all is None
                else ("ruled_out_at_ceiling" if upper_all <= ceiling else "inconclusive")
            ),
        },
        "claims": {
            "error_family_claim": "permitted"
            if correction_only and not invalid
            else "not_applicable",
            "critical_inclusive_claim": (
                "held"
                if critical_required or not critical_verified
                else "permitted_if_precision_met"
            ),
            "overall_accuracy_claim": (
                "eligible_pending_upstream_receipt" if eligible else "not_established"
            ),
            "overall_accuracy_alerts_emitted": 0,
            "broad_improvement_alerts_emitted": 0,
            "customer_accuracy_alert_permitted": alert_ok,
            "broad_improvement_alert_permitted": alert_ok,
            "customer_accuracy_alert_blocked_by": alert_blockers,
            "caller_asserted_dependencies": sorted(dependencies),
        },
        "audit_selection": {
            "frame_digest": canonical_digest(sorted(r.record.family_id for r in rows)),
            "seed": p.sampling_seed,
            "design": p.sampling_design.value,
            "sample_sizes": [list(s) for s in p.sample_sizes],
            "adjudicator_independence": independence,
            "adjudicator_independence_recorded": independence_counts,
            "time_cutoff_day": p.temporal_cutoff_day,
            "scoring_version": p.scorer_version,
        },
        "upstream": {
            "candidate_snapshot": asdict(inputs.candidate_snapshot)
            if inputs.candidate_snapshot
            else None,
            "verification": (
                "caller_asserted_by_consumer"
                if inputs.upstream_snapshot_verified
                else "unverified_dependency_ope_416"
            ),
        },
        "provenance": {
            "exposed_families": exposure.basis,
            "unlogged_disclosures": exposure.basis,
            "exposure_log_covers_protected_families": exposure.scope_ok,
            "upstream_snapshot_verified": "caller_asserted",
            "evidence_origin": "caller_asserted",
            "adjudicator_blinding_flags": "caller_asserted",
            "adjudicator_distinct_from_first_labeler": "worker_compared_supplied_ids",
        },
        "superseded_by": inputs.superseded_by,
        "successor_of": inputs.successor_of,
        "excluded_reasons": sorted(
            {
                _exclusion_reason(r.record, p.temporal_cutoff_day, exposed)
                for r in by_status["excluded"]
            }
        ),
    }
    receipt["receipt_digest"] = canonical_digest(receipt)
    receipt["receipt_id"] = f"audit-{receipt['receipt_digest'][:16]}"
    return receipt


def mapper_view(records: Sequence[FamilyRecord]) -> list[dict[str, Any]]:
    """What the mapper may see: no protected label, verifier or answer commitment."""
    hidden = {
        "final_label_digest", "adjudicator_label_digest", "independent_label_digest",
        "adjudicator_id", "independent_source", "unresolved",
    }  # fmt: skip
    return [
        {k: v for k, v in asdict(r).items() if r.split == "development" or k not in hidden}
        for r in records
    ]


def _decide(
    invalid: list[Reason],
    stale: list[Reason],
    starter: list[Reason],
    insufficient: list[Reason],
    limited: list[Reason],
    any_verified: bool,
) -> tuple[Verdict, list[Reason]]:
    if invalid:
        return Verdict.INVALID, invalid + stale
    if stale:
        return Verdict.STALE, stale
    if starter:
        return Verdict.STARTER_ONLY, starter
    if insufficient:
        return Verdict.INSUFFICIENT_EVIDENCE, insufficient
    if limited and any_verified:
        return Verdict.CLAIM_LIMITED, limited
    return Verdict.EVALUATION_ALLOWED, []


def _flow(
    inputs: AuditInputs,
    rows: Sequence[_Row],
    by_status: dict[str, list[_Row]],
    mature: Sequence[_Row],
    verified: Sequence[_Row],
    disputed: Sequence[_Row],
) -> dict[str, int]:
    live = [r for r in rows if r.status != "excluded"]
    return {
        "eligible": len(rows),
        "excluded": len(by_status["excluded"]),
        "immature": len(by_status["immature"]),
        "mature": len(mature),
        "verified": len(verified),
        "disputed": len(disputed),
        "unresolved": len(by_status["unresolved"]),
        "unverified": len(by_status["unverified"]),
        "scoreable": len(verified) - len(disputed),
        "auto_acted": sum(1 for r in live if r.record.auto_acted),
        "correct": sum(1 for r in verified if r.record.outcome_kind == "correct"),
        "correction": sum(1 for r in verified if r.record.outcome_kind == "correction"),
        "deferral": sum(1 for r in verified if r.record.outcome_kind == "deferral"),
        "reconciles": int(
            len(rows) == len(by_status["excluded"]) + len(by_status["immature"]) + len(mature)
        ),
    }


def binding_digests(
    records: Sequence[FamilyRecord],
    *,
    canonical_inputs: str,
    source_revisions: str,
    scoring_policy: str,
    answer_key: str,
) -> BindingDigests:
    """Compose the frozen or current binding set; split and label digests come from records."""
    return BindingDigests(
        canonical_inputs=canonical_inputs,
        source_revisions=source_revisions,
        split_manifest=split_manifest_digest(records),
        scoring_policy=scoring_policy,
        label_manifest=label_manifest_digest(records),
        answer_key=answer_key,
    )
