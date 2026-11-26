from contextlib import nullcontext
from dataclasses import replace
from unittest.mock import Mock

import pytest
from opentour_cell.pilot import model_connector as models
from opentour_cell.pilot import open_weight


@pytest.fixture
def setup_connector():
    binding = models.ModelBinding(
        tenant="generated-tenant",
        model_ref="incumbent",
        immutable_revision="pinned-v1",
        task="invoice-route",
        input_schema="invoice-features-v1",
        output_schema="invoice-label-v1",
        preprocessing_revision="preprocessing-v1",
        route=models.Route.MANAGED,
        account_or_host="generated-owned-account",
        region="generated-region",
        credential_ref="owned-secret-reference",
    )
    observed = models.Observation(
        binding=binding,
        source_ref="capability-receipt",
        observed_at=5,
        expires_at=50,
        resolved_revision="pinned-v1",
        inference_supported=True,
        training_supported=True,
        hard_cost_cap=True,
        cancellation=True,
        progress=True,
        checkpoint_recovery=True,
        capture={},
    )
    limit = models.FitBound("USD", 500, 60, 20, 100)
    intent = models.FitIntent(
        binding,
        "grant-v1",
        "benchmark-v1",
        "sealed-development",
        "generated-job-intent",
        frozenset({"permitted"}),
        limit,
    )
    grants = {
        purpose: models.Grant(
            purpose.value,
            binding,
            purpose,
            "grant-v1",
            30,
            frozenset({"permitted"}),
            40,
            limit if purpose is models.Purpose.TRAINING else None,
        )
        for purpose in models.Purpose
    }

    def current_grant(actor, selected, purpose):
        grant = grants.get(purpose)
        return grant if grant and (grant.actor, grant.binding) == (actor, selected) else None

    clock = Mock(return_value=10)
    connector = Mock(spec=models.Connector, observe=Mock(return_value=observed))
    connector.estimate.return_value = models.FitBound("USD", 100, 40, 10, 50)
    return models.Preflight(connector, current_grant, clock=clock), connector, grants, intent, clock


def test_inference_only_authority_cannot_inspect_or_dispatch_training(setup_connector):
    preflight, adapter, grants, intent, _ = setup_connector
    del grants[models.Purpose.TRAINING]
    del grants[models.Purpose.INSPECTION]
    assert preflight.discover(
        "baseline_inference", intent.binding, models.Purpose.BASELINE
    ).state == (models.State.BENCHMARK_ONLY)
    for actor in ("baseline_inference", "authoring_inference", "external_disclosure"):
        with pytest.raises(models.CapabilityError, match="model_capability_unavailable"):
            preflight.prepare(actor, intent)
    with pytest.raises(models.CapabilityError, match="model_capability_unavailable"):
        preflight.capture("authoring_inference", intent.binding)
    adapter.observe.assert_called_once_with(intent.binding, models.Purpose.BASELINE)
    adapter.estimate.assert_not_called()
    adapter.submit.assert_not_called()


@pytest.mark.parametrize(
    ("change", "reason"),
    [
        ({"resolved_revision": None}, "exact_training_target_unavailable"),
        ({"resolved_revision": "changed-alias"}, "exact_training_target_unavailable"),
        ({"training_supported": False}, "exact_training_target_unavailable"),
        ({"hard_cost_cap": False}, "enforceable_training_cap_unavailable"),
        ({"expires_at": 10}, "model_capability_evidence_stale_or_mismatched"),
    ],
)
def test_stale_volatile_or_unbounded_target_holds_before_estimate(setup_connector, change, reason):
    preflight, adapter, _, intent, _ = setup_connector
    adapter.observe.return_value = replace(adapter.observe.return_value, **change)
    with pytest.raises(models.CapabilityError, match=reason):
        preflight.prepare("training", intent)
    adapter.estimate.assert_not_called()
    adapter.serialize_development.assert_not_called()
    adapter.submit.assert_not_called()


def test_mismatched_task_and_region_cannot_use_same_provider_model(setup_connector):
    preflight, adapter, _, intent, _ = setup_connector
    adapter.observe.return_value = replace(
        adapter.observe.return_value,
        binding=replace(intent.binding, task="different-task", region="foreign-region"),
    )
    with pytest.raises(models.CapabilityError, match="evidence_stale_or_mismatched"):
        preflight.prepare("training", intent)
    adapter.estimate.assert_not_called()


def test_preflight_rechecks_grant_after_estimate_and_before_preparing_payload(setup_connector):
    preflight, adapter, grants, intent, clock = setup_connector
    assert preflight.prepare("training", intent) == adapter.estimate.return_value

    def expires_during_estimate(_):
        clock.return_value = grants[models.Purpose.TRAINING].expires_at
        return adapter.estimate.return_value

    adapter.estimate.side_effect = expires_during_estimate
    with pytest.raises(models.CapabilityError, match="model_capability_unavailable"):
        preflight.prepare("training", intent)
    adapter.serialize_development.assert_not_called()
    adapter.submit.assert_not_called()


@pytest.mark.parametrize(
    "estimate",
    [
        None,
        models.FitBound("EUR", 100, 40, 10, 50),
        models.FitBound("USD", 501, 40, 10, 50),
        models.FitBound("USD", 100, 61, 10, 50),
        models.FitBound("USD", 100, 40, 21, 50),
        models.FitBound("USD", 100, 40, 10, 101),
    ],
)
def test_unknown_or_excess_cost_does_not_admit_fits(setup_connector, estimate):
    preflight, adapter, _, intent, _ = setup_connector
    adapter.estimate.return_value = estimate
    with pytest.raises(models.CapabilityError, match="known_training_cost_within_budget_required"):
        preflight.prepare("training", intent)
    adapter.submit.assert_not_called()


def test_request_cannot_widen_trusted_budget_or_field_mapping(setup_connector):
    preflight, adapter, _, intent, _ = setup_connector
    with pytest.raises(models.CapabilityError, match="training_budget_not_authorized"):
        preflight.prepare(
            "training", replace(intent, bound=replace(intent.bound, maximum_minor_units=501))
        )
    with pytest.raises(models.CapabilityError, match="training_data_scope_unavailable"):
        preflight.prepare("training", replace(intent, fields=frozenset({"outside-grant"})))
    adapter.observe.assert_not_called()
    adapter.estimate.assert_not_called()


def test_backend_fault_uses_fixed_reason_without_returning_private_payload(setup_connector):
    preflight, adapter, _, intent, _ = setup_connector
    adapter.observe.side_effect = ValueError("private-host-response")
    result = preflight.discover("training", intent.binding, models.Purpose.TRAINING)
    assert result.state is models.State.NEEDS_CONFIGURATION
    assert result.reason == "model_capability_evidence_unavailable"
    assert result.evidence_ref is None


def test_capability_must_remain_current_while_estimate_is_prepared(setup_connector):
    preflight, adapter, _, intent, clock = setup_connector
    adapter.observe.return_value = replace(adapter.observe.return_value, expires_at=15)
    good_estimate = adapter.estimate.return_value

    def delayed_estimate(_):
        clock.return_value = 15
        return good_estimate

    adapter.estimate.side_effect = delayed_estimate
    with pytest.raises(models.CapabilityError, match="evidence_stale_or_mismatched"):
        preflight.prepare("training", intent)
    adapter.submit.assert_not_called()


def test_capture_claims_require_separate_live_scope_and_field_evidence(setup_connector):
    preflight, adapter, grants, intent, _ = setup_connector
    seen = models.CaptureEvidence(models.Fidelity.OBSERVED, "application", "request", 30)
    expired = models.CaptureEvidence(models.Fidelity.OBSERVED, "provider", "receipt", 9)
    grants[models.Purpose.INSPECTION] = replace(
        grants[models.Purpose.INSPECTION],
        permitted_fields=frozenset(
            {models.CaptureField.REQUEST_WITNESS, models.CaptureField.PROVIDER_RECEIPT}
        ),
    )
    adapter.observe.return_value = replace(
        adapter.observe.return_value,
        capture={
            models.CaptureField.REQUEST_WITNESS: seen,
            models.CaptureField.PROVIDER_RECEIPT: expired,
            models.CaptureField.SERVED_IDENTITY: seen,
        },
    )
    result = preflight.capture("capture_inspection", intent.binding)
    assert result[models.CaptureField.REQUEST_WITNESS] == seen
    assert all(
        item.fidelity is models.Fidelity.UNAVAILABLE
        for field, item in result.items()
        if field is not models.CaptureField.REQUEST_WITNESS
    )
    with pytest.raises(models.CapabilityError, match="model_capability_unavailable"):
        preflight.capture("capture_inspection", replace(intent.binding, tenant="foreign"))


def test_unresolved_alias_preserves_observed_answer_without_crediting_served_model(setup_connector):
    preflight, adapter, grants, intent, _ = setup_connector
    served = models.CaptureField.SERVED_IDENTITY
    answer = models.CaptureField.PARSED_OUTPUT
    grants[models.Purpose.INSPECTION] = replace(
        grants[models.Purpose.INSPECTION], permitted_fields=frozenset({served, answer})
    )
    witness = models.CaptureEvidence(models.Fidelity.OBSERVED, "application", "result", 30)
    adapter.observe.return_value = replace(
        adapter.observe.return_value,
        resolved_revision=None,
        capture={served: witness, answer: witness},
    )
    visible = preflight.capture("capture_inspection", intent.binding)
    assert visible[served].fidelity is models.Fidelity.UNAVAILABLE
    assert visible[answer] == witness
    baseline = preflight.discover("baseline_inference", intent.binding, models.Purpose.BASELINE)
    assert baseline.state is models.State.BENCHMARK_ONLY
    assert baseline.exact_model_attribution is False


def test_result_must_match_completed_job_and_exact_immutable_incumbent(setup_connector):
    _, _, _, intent, _ = setup_connector
    job = models.JobReceipt(intent, models.JobState.COMPLETED, "job-a", "fit-evidence")
    result = models.Candidate(intent, "job-a", "pinned-v1", "artifact-v2", "replay-evidence")
    models.verify_candidate(job, result)
    for alternate in (
        replace(result, parent_revision="substituted"),
        replace(result, job_ref="other-job"),
        replace(result, replay_evidence_ref=""),
    ):
        with pytest.raises(models.CapabilityError, match="candidate_lineage_unverified"):
            models.verify_candidate(job, alternate)
    with pytest.raises(models.CapabilityError, match="candidate_lineage_unverified"):
        models.verify_candidate(replace(job, state=models.JobState.UNKNOWN), result)


def test_local_installation_probe_has_only_metadata_and_checks_pinned_hashes(monkeypatch, tmp_path):
    grant = {
        "profile": open_weight.PROFILE,
        "organization": "generated-tenant",
        "engagement": "generated-engagement",
        "trainer": "generated-trainer",
        "payer": "generated-custodian",
        "destination": "generated-owner",
        "rights_attestation": "generated-attestation",
        "expires_at": 4102444800,
        "residual_revision": "a" * 64,
        "features": ["description"],
        "labels": ["refund", "sale"],
        "max_seconds": 5,
        "max_cost_cents": 0,
        "hourly_cost_cents": 0,
        **dict.fromkeys(open_weight.GRANTS, True),
    }
    metadata = {
        "profile": open_weight.PROFILE,
        "checkpoint_revision": open_weight.REVISION,
        "verified_base_sha256": open_weight.BASE_SHA256,
        "verified_tokenizer_sha256": open_weight.TOKENIZER_SHA256,
    }
    worker = Mock(return_value=metadata)
    monkeypatch.setattr(open_weight, "_run", worker)
    permission = Mock()

    def inspect():
        return open_weight.inspect_open_weight_installation(
            tmp_path / "uncreated-bundle",
            tmp_path / "staged",
            grant=grant,
            check_permission=permission,
            dispatch=nullcontext,
        )

    assert inspect() == metadata
    assert worker.call_args.args[4] == {"operation": "inspect_installation"}
    assert worker.call_args.kwargs["check_permission"] is permission
    assert not (tmp_path / "uncreated-bundle").exists()
    for field in ("checkpoint_revision", "verified_base_sha256", "verified_tokenizer_sha256"):
        worker.return_value = {**metadata, field: "substituted"}
        with pytest.raises(open_weight.OpenWeightError, match="installation_evidence_invalid"):
            inspect()
