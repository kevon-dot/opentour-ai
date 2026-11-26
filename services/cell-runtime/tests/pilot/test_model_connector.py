import sqlite3
from contextlib import closing, contextmanager, nullcontext
from dataclasses import replace
from hashlib import sha256
from unittest.mock import Mock

import pytest
from opentour_cell.pilot import model_connector as models
from opentour_cell.pilot import open_weight


@pytest.fixture(params=[models.Route.MANAGED, models.Route.CUSTOM])
def setup_connector(request):
    binding = models.ModelBinding(
        tenant="generated-tenant",
        model_ref="incumbent",
        immutable_revision="pinned-v1",
        task="invoice-route",
        input_schema="invoice-features-v1",
        output_schema="invoice-label-v1",
        preprocessing_revision="preprocessing-v1",
        route=request.param,
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
            frozenset({"benchmark-v1"}) if purpose is models.Purpose.TRAINING else frozenset(),
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


@pytest.fixture
def with_disclosure(setup_connector):
    _, _, grants, _, _ = setup_connector
    grants[models.Purpose.DISCLOSURE] = replace(grants[models.Purpose.DISCLOSURE], actor="training")
    return setup_connector


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


def test_lost_reply_recovers_same_operation_and_does_not_resubmit(setup_connector):
    preflight, adapter, grants, intent, _ = setup_connector
    job = models.JobReceipt(intent, models.JobState.COMPLETED, "original-job", "fit-evidence")
    candidate = models.Candidate(intent, job.job_ref, "pinned-v1", "artifact-v2", "replay")
    adapter.lookup.side_effect = [TimeoutError(), None, job, job]
    adapter.collect.return_value = candidate
    assert preflight.reconcile_fit("training", intent).state is models.JobState.UNKNOWN
    assert preflight.reconcile_fit("training", intent).state is models.JobState.UNKNOWN
    assert preflight.reconcile_fit("training", intent) == job
    assert preflight.collect_candidate("training", job) == candidate
    adapter.submit.assert_not_called()
    assert all(call.args == (intent,) for call in adapter.lookup.call_args_list)
    del grants[models.Purpose.TRAINING]
    with pytest.raises(models.CapabilityError, match="model_capability_unavailable"):
        preflight.reconcile_fit("training", intent)
    assert adapter.lookup.call_count == 4


def test_lookup_denies_foreign_benchmark_and_substituted_job_receipt(setup_connector):
    preflight, adapter, _, intent, _ = setup_connector
    foreign = replace(intent, benchmark_revision="foreign-benchmark")
    with pytest.raises(models.CapabilityError, match="training_data_scope_unavailable"):
        preflight.reconcile_fit("training", foreign)
    adapter.lookup.assert_not_called()
    adapter.lookup.return_value = models.JobReceipt(
        foreign, models.JobState.COMPLETED, "same-provider-id", "different-evidence"
    )
    with pytest.raises(models.CapabilityError, match="model_job_receipt_mismatched"):
        preflight.reconcile_fit("training", intent)
    adapter.collect.assert_not_called()
    adapter.submit.assert_not_called()


def test_generated_backend_recovery_after_committed_but_lost_reply(with_disclosure, tmp_path):
    initial, adapter, _, intent, clock = with_disclosure
    payload = b"generated-development"
    intent = replace(intent, development_digest=sha256(payload).hexdigest())
    store = tmp_path / "generated-provider.sqlite3"
    with closing(sqlite3.connect(store)) as db:
        db.execute(
            """
            CREATE TABLE admitted (
                tenant TEXT NOT NULL, operation TEXT NOT NULL, digest TEXT NOT NULL,
                job TEXT NOT NULL, PRIMARY KEY (tenant, operation)
            )
            """
        )

    def lookup(selected):
        with closing(sqlite3.connect(store)) as db:
            row = db.execute(
                "SELECT digest, job FROM admitted WHERE tenant=? AND operation=?",
                (selected.binding.tenant, selected.idempotency_key),
            ).fetchone()
        if row is None:
            return None
        if row[0] != selected.development_digest:
            raise ValueError("generated_intent_conflict")
        return models.JobReceipt(selected, models.JobState.QUEUED, row[1], "admission-evidence")

    def submit(selected, _):
        existing = lookup(selected)
        if existing is not None:
            return existing
        with closing(sqlite3.connect(store)) as db:
            with db:
                db.execute(
                    "INSERT INTO admitted VALUES (?, ?, ?, ?)",
                    (
                        selected.binding.tenant,
                        selected.idempotency_key,
                        selected.development_digest,
                        "generated-single-job",
                    ),
                )
        raise TimeoutError

    reserved = False

    @contextmanager
    def reservation_guard():
        nonlocal reserved
        reserved = True
        try:
            yield
        finally:
            reserved = False

    def submit_reserved(selected, development):
        assert reserved
        return submit(selected, development)

    adapter.serialize_development.return_value = payload
    adapter.submit.side_effect = submit_reserved
    adapter.lookup.side_effect = lookup
    admitted = initial.submit_reserved_fit(
        "training", intent, ({"permitted": "generated-value"},), reservation_guard=reservation_guard
    )
    assert admitted.job_ref == "generated-single-job"
    adapter.submit.assert_called_once_with(intent, payload)
    adapter.lookup.assert_called_once_with(intent)
    assert not reserved

    restarted_backend = Mock(spec=models.Connector)
    restarted_backend.lookup.side_effect = lookup
    restarted = models.Preflight(restarted_backend, initial.current_grant, clock=clock)
    assert restarted.reconcile_fit("training", intent) == admitted
    assert restarted.reconcile_fit("training", intent) == admitted
    assert submit(intent, payload) == admitted
    with pytest.raises(ValueError, match="generated_intent_conflict"):
        submit(replace(intent, development_digest="different-intent"), b"changed-development")
    restarted_backend.submit.assert_not_called()
    with closing(sqlite3.connect(store)) as db:
        assert db.execute("SELECT COUNT(*) FROM admitted").fetchone() == (1,)


def test_reserved_dispatch_rechecks_grant_before_transfer(with_disclosure):
    preflight, adapter, grants, intent, _ = with_disclosure
    payload = b"generated-development"
    intent = replace(intent, development_digest=sha256(payload).hexdigest())
    adapter.serialize_development.return_value = payload

    @contextmanager
    def revoked_reservation():
        del grants[models.Purpose.TRAINING]
        yield

    with pytest.raises(models.CapabilityError, match="model_capability_unavailable"):
        preflight.submit_reserved_fit(
            "training", intent, ({"permitted": "value"},), reservation_guard=revoked_reservation
        )
    adapter.submit.assert_not_called()
    adapter.lookup.assert_not_called()


def test_reserved_dispatch_rejects_wrong_payload_scope_and_digest(with_disclosure):
    preflight, adapter, _, intent, _ = with_disclosure
    adapter.serialize_development.return_value = b"different-generated-payload"
    with pytest.raises(models.CapabilityError, match="training_data_scope_unavailable"):
        preflight.submit_reserved_fit(
            "training",
            intent,
            ({"permitted": "value", "holdout": "private"},),
            reservation_guard=nullcontext,
        )
    adapter.serialize_development.assert_not_called()
    with pytest.raises(models.CapabilityError, match="training_payload_invalid"):
        preflight.submit_reserved_fit(
            "training", intent, ({"permitted": "value"},), reservation_guard=nullcontext
        )
    adapter.submit.assert_not_called()


def test_reserved_dispatch_with_no_verified_reply_remains_unknown(with_disclosure):
    preflight, adapter, _, intent, _ = with_disclosure
    payload = b"generated-development"
    intent = replace(intent, development_digest=sha256(payload).hexdigest())
    adapter.serialize_development.return_value = payload
    adapter.submit.side_effect = TimeoutError
    adapter.lookup.return_value = None
    state = preflight.submit_reserved_fit(
        "training", intent, ({"permitted": "value"},), reservation_guard=nullcontext
    )
    assert state == models.JobReceipt(intent, models.JobState.UNKNOWN, "", "")
    adapter.submit.assert_called_once_with(intent, payload)
    adapter.lookup.assert_called_once_with(intent)


def test_remote_dispatch_requires_own_disclosure_grant(setup_connector):
    preflight, adapter, _, intent, _ = setup_connector
    capability = preflight.discover("training", intent.binding, models.Purpose.TRAINING)
    assert (capability.state, capability.reason) == (
        models.State.NEEDS_CONFIGURATION,
        "training_disclosure_scope_unavailable",
    )
    with pytest.raises(models.CapabilityError, match="model_capability_unavailable"):
        preflight.submit_reserved_fit(
            "training", intent, ({"permitted": "value"},), reservation_guard=nullcontext
        )
    adapter.serialize_development.assert_not_called()
    adapter.submit.assert_not_called()


def test_remote_capability_with_exact_grant_and_disclosure_authority(with_disclosure):
    preflight, adapter, _, intent, _ = with_disclosure
    result = preflight.discover("training", intent.binding, models.Purpose.TRAINING)
    assert result.state is models.State.TRAINABLE
    assert result.exact_model_attribution
    adapter.serialize_development.assert_not_called()
    adapter.submit.assert_not_called()


def test_remote_dispatch_rechecks_disclosure_authority_under_reservation(with_disclosure):
    preflight, adapter, grants, intent, _ = with_disclosure
    payload = b"generated-development"
    intent = replace(intent, development_digest=sha256(payload).hexdigest())
    adapter.serialize_development.return_value = payload

    @contextmanager
    def rotated_disclosure():
        grant = grants[models.Purpose.DISCLOSURE]
        grants[grant.purpose] = replace(grant, revision="grant-v2")
        yield

    with pytest.raises(models.CapabilityError, match="training_disclosure_scope_unavailable"):
        preflight.submit_reserved_fit(
            "training", intent, ({"permitted": "value"},), reservation_guard=rotated_disclosure
        )
    adapter.submit.assert_not_called()


def test_training_cannot_expire_during_disclosure_lookup_at_dispatch(with_disclosure):
    preflight, adapter, grants, intent, clock = with_disclosure
    payload = b"generated-development"
    intent = replace(intent, development_digest=sha256(payload).hexdigest())
    adapter.serialize_development.return_value = payload
    grants[models.Purpose.DISCLOSURE] = replace(
        grants[models.Purpose.DISCLOSURE], expires_at=45, retention_until=45
    )
    lookup = preflight.current_grant
    reserved = False

    @contextmanager
    def reservation():
        nonlocal reserved
        reserved = True
        yield

    def current(actor, binding, purpose):
        if reserved and purpose is models.Purpose.DISCLOSURE:
            clock.return_value = grants[models.Purpose.TRAINING].valid_until
        return lookup(actor, binding, purpose)

    preflight.current_grant = current
    with pytest.raises(models.CapabilityError, match="training_data_scope_unavailable"):
        preflight.submit_reserved_fit(
            "training", intent, ({"permitted": "value"},), reservation_guard=reservation
        )
    adapter.submit.assert_not_called()


def test_disclosure_cannot_expire_during_final_training_read(with_disclosure):
    preflight, _, grants, intent, clock = with_disclosure
    grants[models.Purpose.DISCLOSURE] = replace(grants[models.Purpose.DISCLOSURE], expires_at=20)
    lookup = preflight.current_grant
    found_disclosure = False

    def current(actor, binding, purpose):
        nonlocal found_disclosure
        if purpose is models.Purpose.DISCLOSURE:
            found_disclosure = True
        elif found_disclosure:
            clock.return_value = 20
        return lookup(actor, binding, purpose)

    preflight.current_grant = current
    result = preflight.discover("training", intent.binding, models.Purpose.TRAINING)
    assert result.state is models.State.NEEDS_CONFIGURATION
    assert result.reason == "training_disclosure_scope_unavailable"


def test_capture_never_outlives_containing_observation_or_grant(setup_connector):
    preflight, adapter, grants, intent, _ = setup_connector
    field = models.CaptureField.REQUEST_WITNESS
    grants[models.Purpose.INSPECTION] = replace(
        grants[models.Purpose.INSPECTION], permitted_fields=frozenset({field})
    )
    proof = models.CaptureEvidence(models.Fidelity.OBSERVED, "application", "request", 90)
    for observation_end, want in ((20, 20), (50, 30)):
        adapter.observe.return_value = replace(
            adapter.observe.return_value, expires_at=observation_end, capture={field: proof}
        )
        visible = preflight.capture("capture_inspection", intent.binding)
        assert visible[field] == replace(proof, expires_at=want)


def test_discovery_requires_valid_grant_limits(setup_connector):
    preflight, _, grants, intent, _ = setup_connector
    grants[models.Purpose.TRAINING] = replace(
        grants[models.Purpose.TRAINING], fit_bound=replace(intent.bound, maximum_seconds=0)
    )
    result = preflight.discover("training", intent.binding, models.Purpose.TRAINING)
    assert result.state is models.State.NEEDS_CONFIGURATION
    assert result.reason == "enforceable_training_budget_required"


def test_discovery_with_evidence_expiring_during_authority_read_holds(setup_connector):
    preflight, adapter, _, intent, clock = setup_connector
    adapter.observe.return_value = replace(adapter.observe.return_value, expires_at=20)
    clock.side_effect = [10, 10, 19, 20]
    result = preflight.discover("training", intent.binding, models.Purpose.TRAINING)
    assert result.state is models.State.NEEDS_CONFIGURATION


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
