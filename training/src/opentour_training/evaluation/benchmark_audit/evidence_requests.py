"""Next-evidence request wording per reason code. Static text; no case data."""

from __future__ import annotations

NEXT_EVIDENCE: dict[str, str] = {
    "preregistration_after_reveal": (
        "Seal a successor pre-registration before any candidate output is shown."
    ),
    "preregistration_incomplete": ("Commit every required pre-registration field before scoring."),
    "family_crosses_split": (
        "Reseal the split so no family root, duplicate, template, attachment or "
        "ancestry link spans development and protected cohorts."
    ),
    "future_only_field": (
        "Remove post-decision fields from decision-time inputs, or move them to "
        "adjudication only, and seal a successor."
    ),
    "protected_comparison_used_for_selection": (
        "Collect fresh protected evidence that no selection or threshold choice has seen."
    ),
    "preprocessing_fit_on_protected": (
        "Refit preprocessing on development partitions only and persist its revision."
    ),
    "preprocessing_not_verified": (
        "Verify that the persisted fitted preprocessing revision was applied to the "
        "protected population."
    ),
    "label_overturned_by_independent_record": (
        "Correct the labels from the independent record, seal a successor and run a "
        "fresh blinded audit."
    ),
    "label_error_exceeds_tolerance": (
        "Fix the label rule upstream, seal a successor and run a fresh blinded audit."
    ),
    "unlogged_case_level_disclosure": (
        "Register the disclosure, retire the exposed families and seal a successor "
        "protected manifest."
    ),
    "exposure_log_scope_mismatch": (
        "Supply the exposure guard's release history for every protected family in this audit."
    ),
    "duplicate_family_id": (
        "Resolve duplicate family identifiers and reconcile the denominator flow."
    ),
    "audit_sample_not_reproducible": (
        "Redraw the audit sample from the registered frame, seed and sample sizes."
    ),
    "bound_digest_changed": (
        "Archive this verdict and seal a successor, then obtain fresh independent confirmation."
    ),
    "superseded": ("Use the successor receipt; reconcile earlier disclosures made under this one."),
    "no_verified_families": ("Supply blinded independent verification for the audit sample."),
    "unresolved_ambiguity": (
        "Adjudicate the unresolved families under the registered disagreement rule."
    ),
    "grouping_unknown": ("Assign every family a group root before audit."),
    "preprocessing_revision_missing": ("Persist the fitted preprocessing revision."),
    "candidate_snapshot_receipt_missing": (
        "Supply the verified-candidate snapshot receipt (OPE-416) for this population."
    ),
    "fresh_independent_confirmation_missing": (
        "Run a fresh blinded independent confirmation of every family in the successor."
    ),
    "correction_only_sample": (
        "Add independently sampled correct cases and deferrals, with sampling "
        "probabilities, to speak to overall accuracy."
    ),
    "critical_coverage_gap": (
        "Verify the missing critical families independently to lift the critical- inclusive hold."
    ),
    "precision_cannot_rule_out_ceiling": (
        "Sample more independent families under the registered design."
    ),
    "policy_only_starter": (
        "No outcome history: keep the policy-only starter experience and collect "
        "authorized evidence."
    ),
}
