"""Generated sentinel fixtures for the benchmark audit. Nothing here is customer evidence.

Fixture kind: ``generated``. Labels are salted commitments of invented strings. These builders
cannot satisfy a provider or customer-evidence gate.
"""

from __future__ import annotations

from dataclasses import replace

from opentour_training.evaluation.benchmark_audit.audit import binding_digests
from opentour_training.evaluation.benchmark_audit.model import (
    AuditInputs,
    BindingDigests,
    CandidateSnapshotBinding,
    FamilyRecord,
    FeatureSpec,
    PopulationKind,
    PreprocessingRecord,
    PreRegistration,
    SamplingDesign,
    canonical_digest,
    label_commitment,
)

SALT = "generated-sentinel-salt"
INDEPENDENT = "generated-authoritative-record"
CANONICAL = canonical_digest("generated-canonical-inputs")
SOURCES = canonical_digest("generated-source-revisions")
SCORING = canonical_digest("generated-scoring-policy")
ANSWER_KEY = canonical_digest("generated-sealed-answer-key")
FEATURES = (
    FeatureSpec("ticket_text", "decision_time"),
    FeatureSpec("customer_tier", "decision_time"),
    FeatureSpec("refund_settled_at", "post_decision"),
)
CONSUMED = ("ticket_text", "customer_tier")
OUTCOMES = ("correction",) * 3 + ("correct",) * 3 + ("deferral",) * 2


def commit(family_id: str, label: str) -> str:
    return label_commitment(SALT, family_id, label)


def family(
    family_id: str,
    stratum: str,
    outcome: str,
    *,
    split: str = "protected",
    root: str | None = None,
    label: str | None = None,
) -> FamilyRecord:
    """A fully verified family whose key label matches the independent record."""
    label = label or f"generated-action-{family_id}"
    digest = commit(family_id, label)
    protected = split == "protected"
    return FamilyRecord(
        family_id=family_id,
        group_root=root or f"generated-root-{family_id}",
        split=split,  # type: ignore[arg-type]
        stratum=stratum,  # type: ignore[arg-type]
        slice_id=f"generated-slice-{stratum}",
        outcome_kind=outcome,  # type: ignore[arg-type]
        decided_day=120,
        final_label_digest=digest,
        exact_hash=canonical_digest(f"generated-exact-{family_id}"),
        template_group=f"generated-template-{family_id}",
        attachment_hash=canonical_digest(f"generated-attachment-{family_id}"),
        ancestry_root=f"generated-ancestry-{family_id}",
        first_labeler_id="generated-labeler-a",
        auto_acted=outcome == "correct",
        adjudicator_id="generated-adjudicator-b" if protected else None,
        adjudicator_label_digest=digest if protected else None,
        independent_source=INDEPENDENT if protected else None,
        independent_label_digest=digest if protected else None,
    )


def baseline_records() -> tuple[FamilyRecord, ...]:
    routine = [family(f"generated-r{i:02d}", "routine", OUTCOMES[i - 1]) for i in range(1, 9)]
    critical = [
        family("generated-c01", "critical", "correction"),
        *[family(f"generated-c{i:02d}", "critical", "correct") for i in range(2, 5)],
    ]
    development = [
        family(f"generated-d{i:02d}", "routine", "correct", split="development")
        for i in range(1, 4)
    ]
    return (*routine, *critical, *development)


def prereg_for(records: tuple[FamilyRecord, ...], **overrides: object) -> PreRegistration:
    base = PreRegistration(
        registration_id="generated-prereg-1",
        frozen_day=100,
        evidence_origin="generated",
        population_kind=PopulationKind.ENUMERATED_FIXTURE,
        population_frame="generated-frame:12-family-enumeration",
        intended_decision="generated-decision:refund-routing",
        policy_revision="generated-policy-r1",
        feature_contract=FEATURES,
        label_authority="generated-authoritative-record",
        maturity_window_days=30,
        grouping_method="generated-root-grouping-v1",
        temporal_cutoff_day=200,
        scorer_version="generated-scorer-v1",
        sampling_design=SamplingDesign.FULL_ENUMERATION,
        sampling_seed=7,
        sample_sizes=(),
        critical_slices=("generated-slice-critical",),
        operating_point="generated-operating-point",
        decision_margin=0.0,
        interval_method="binomial_clopper_pearson_one_sided_v1",
        independent_unit="independent_family",
        tolerable_label_error=0.0,
        acceptable_error_ceiling=0.05,
        alpha=0.05,
        min_critical_coverage=1.0,
        max_unresolved=0,
        disagreement_rule="independent_record_overrules",
        frozen=binding_digests(
            records,
            canonical_inputs=CANONICAL,
            source_revisions=SOURCES,
            scoring_policy=SCORING,
            answer_key=ANSWER_KEY,
        ),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def current_for(records: tuple[FamilyRecord, ...]) -> BindingDigests:
    return binding_digests(
        records,
        canonical_inputs=CANONICAL,
        source_revisions=SOURCES,
        scoring_policy=SCORING,
        answer_key=ANSWER_KEY,
    )


def inputs_for(
    records: tuple[FamilyRecord, ...],
    *,
    frozen_records: tuple[FamilyRecord, ...] | None = None,
    prereg_overrides: dict[str, object] | None = None,
    **overrides: object,
) -> AuditInputs:
    """Freeze over ``frozen_records`` (default: the audited records), then audit ``records``."""
    prereg = prereg_for(frozen_records or records, **(prereg_overrides or {}))
    base = AuditInputs(
        prereg=prereg,
        records=records,
        current=current_for(records),
        as_of_day=300,
        candidate_outputs_revealed_day=150,
        consumed_features=CONSUMED,
        preprocessing=PreprocessingRecord("generated-prep-r1", "development", "generated-prep-r1"),
    )
    return replace(base, **overrides)  # type: ignore[arg-type]


def real_sample_records(
    routine_sampled: int = 60, critical_sampled: int = 60
) -> tuple[tuple[FamilyRecord, ...], tuple[tuple[str, int], ...]]:
    """Generated stand-in for a stratified real sample; unsampled families stay unaudited."""
    from opentour_training.evaluation.benchmark_audit.audit import draw_audit_sample

    frame = [(f"generated-sr{i:03d}", "routine") for i in range(routine_sampled + 40)]
    frame += [(f"generated-sc{i:03d}", "critical") for i in range(critical_sampled + 20)]
    sizes = (("routine", routine_sampled), ("critical", critical_sampled))
    chosen = draw_audit_sample(frame, sizes, 7)
    records: list[FamilyRecord] = []
    for fid, stratum in frame:
        record = family(fid, stratum, "correct" if stratum == "routine" else "correction")
        if fid not in chosen:
            record = replace(
                record,
                adjudicator_id=None,
                adjudicator_label_digest=None,
                independent_source=None,
                independent_label_digest=None,
            )
        else:
            total = {"routine": routine_sampled + 40, "critical": critical_sampled + 20}[stratum]
            size = routine_sampled if stratum == "routine" else critical_sampled
            record = replace(record, sampling_probability=size / total)
        records.append(record)
    records.append(family("generated-d01", "routine", "correct", split="development"))
    return tuple(records), sizes


OBSERVED_SNAPSHOT = CandidateSnapshotBinding(
    "opentour.pilot.live-learning-snapshot.v1", canonical_digest("generated-snapshot-digest")
)
