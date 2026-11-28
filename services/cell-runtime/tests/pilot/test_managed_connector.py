"""OPE-508 managed fine-tuning connector tests.

Acceptance IDs below are this ticket's own (no repository binding exists for OPE-508).
Fixture kind for every test: generated. The provider is an in-memory double; nothing here is
installed-provider or customer evidence.

M1  lifecycle: dispatch, poll, collect, handoff, usage, disposition, replay
M2  dispatch without a reservation is refused
M3  a second reservation for the same fit is refused; one execution
M4  lost acknowledgement does not double-execute (file and job creation)
M5  an unverifiable job is UNKNOWN, never success, never re-created; reconciled once visible
M6  a crash before the provider created anything is not silently retried
M7  a cap-exceeding job is refused before any provider call
M8  no enforceable hard cap is refused; an unenforced cap acknowledgement cancels the job
M9  cancel is durable first, retryable, yields no candidate, surfaces irreversible cost
M10 fence cancels live jobs, blocks new effects and survives restart
M11 missing credential fails closed and no secret appears anywhere
M12 late provider callbacks and a terminal job's later reports change nothing
M13 unrecognized provider state or success without a model id is UNKNOWN
M14 training-disabled model is benchmark-only; missing disclosure grant needs configuration
M15 protected answers never reach the provider; handoff only to a permitted recipient
M16 same key with a different intent is refused
M17 restart replays the same receipt without a second execution
M18 retention disposition recorded once; usage unknown is None, never zero
M19 concurrent identical dispatches execute once
M20 shared conformance suite passes against this connector
M21 binding for another account or region is refused; evidence tier is fixture
R1-R7 independent-review regressions (false-trust class): see the bottom of this file
"""

import json
import threading
from contextlib import contextmanager
from dataclasses import replace

import pytest
from opentour_cell.pilot import connector_conformance as conformance
from opentour_cell.pilot import managed_connector as managed
from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.connector_ext import EvidenceTier, Fenceable, UsageReporting, UsageSource

SECRET = "simulated:secret-value-must-never-leak"
ACTOR = "training"
FIELDS = frozenset({"input_text", "label"})
ROWS = tuple(
    {"input_text": f"generated-input-{i}", "label": f"generated-label-{i}"} for i in range(4)
)
BOUND = mc.FitBound("USD", 500, 60, 20, 100)
BINDING = mc.ModelBinding(
    tenant="generated-tenant",
    model_ref="simulated:provider-model",
    immutable_revision="simulated:model-version-1",
    task="invoice-route",
    input_schema="invoice-features-v1",
    output_schema="invoice-label-v1",
    preprocessing_revision="preprocessing-v1",
    route=mc.Route.MANAGED,
    account_or_host="generated-owned-account.invalid",
    region="simulated:region-1",
    credential_ref="simulated:secret-ref",
)
SETTINGS = managed.ManagedSettings(
    account=BINDING.account_or_host,
    region=BINDING.region,
    task=BINDING.task,
    preprocessing_revision=BINDING.preprocessing_revision,
    epochs=2,
    protected_fields=frozenset({"answer_key"}),
    permitted_recipients=frozenset({"simulated:recipient-1"}),
)


class FakeProvider:
    """Generated stand-in for the customer's provider account."""

    def __init__(self, autocomplete=True):
        self.autocomplete = autocomplete
        self.vault = {BINDING.credential_ref: SECRET}
        self.files = {}
        self.jobs = {}
        self.executions = 0
        self.uploaded = []
        self.deleted = []
        self.cancel_calls = 0
        self.drop_put_response = False
        self.fail_put_before = False
        self.drop_create_response = False
        self.fail_cancel = False
        self.blind = False
        self.model = managed.ProviderModel(BINDING.immutable_revision, True, True, True, True)
        self.cap_ack = "echo"
        self.report_usage = True
        self.final_state = "succeeded"
        self.tuned_id = "simulated:tuned-model-1"
        self.delete_ok = True
        self.predictions = []

    def _auth(self, binding):
        if self.vault.get(binding.credential_ref) is None:
            raise LookupError("credential_unavailable")

    def describe_model(self, binding):
        self._auth(binding)
        return self.model

    def quote(self, binding, rows, epochs):
        self._auth(binding)
        return managed.ProviderQuote("USD", rows * 5, 30, rows * epochs)

    def put_file(self, binding, client_token, data):
        self._auth(binding)
        if self.fail_put_before:
            raise OSError("simulated:network-down")
        if client_token not in self.files:
            self.files[client_token] = managed.ProviderFile(
                f"simulated:file-{len(self.files)}", client_token
            )
            self.uploaded.append(data)
        if self.drop_put_response:
            self.drop_put_response = False
            raise OSError("simulated:response-lost")
        return self.files[client_token]

    def find_file(self, binding, client_token):
        self._auth(binding)
        return self.files.get(client_token)

    def _state(self, job):
        return replace(
            job, state=self.final_state, tuned_model_id=self.tuned_id, usage=self._usage()
        )

    def _usage(self):
        if not self.report_usage:
            return None
        return managed.ProviderUsage("USD", 80, 25, 4, 12)

    def create_job(self, binding, client_token, intent_sha256, file_id, cap, epochs):
        self._auth(binding)
        job = self.jobs.get(client_token)
        if job is None:
            self.executions += 1
            cap_ack = cap if self.cap_ack == "echo" else self.cap_ack
            job = managed.ProviderJob(
                f"simulated:provider-job-{self.executions}",
                "running",
                client_token,
                intent_sha256,
                file_id,
                cap_ack,
            )
            if self.autocomplete:
                job = self._state(job)
            self.jobs[client_token] = job
        if self.drop_create_response:
            self.drop_create_response = False
            raise OSError("simulated:response-lost")
        return job

    def find_job(self, binding, client_token):
        self._auth(binding)
        return None if self.blind else self.jobs.get(client_token)

    def _by_id(self, job_id):
        for token, job in self.jobs.items():
            if job.job_id == job_id:
                return token, job
        raise LookupError("job_unknown")

    def get_job(self, binding, job_id):
        self._auth(binding)
        return self._by_id(job_id)[1]

    def cancel_job(self, binding, job_id):
        self._auth(binding)
        self.cancel_calls += 1
        if self.fail_cancel:
            raise OSError("simulated:network-down")
        token, job = self._by_id(job_id)
        if job.state in ("running", "queued"):
            job = replace(job, state="cancelled", usage=managed.ProviderUsage("USD", 30, 10, 0, 3))
            self.jobs[token] = job
        return job

    def delete_file(self, binding, file_id):
        self._auth(binding)
        self.deleted.append(file_id)
        return self.delete_ok

    def predict(self, binding, tuned_model_id, inputs):
        self._auth(binding)
        self.predictions.append((tuned_model_id, inputs))
        return tuple({"label": "generated-prediction"} for _ in inputs)


class Reservations:
    """Caller-owned (OPE-418) reservation double: exactly one per fit intent."""

    def __init__(self):
        self.held = set()
        self.entries = 0

    def reserve(self, intent):
        if intent.idempotency_key in self.held:
            raise RuntimeError("reservation_already_held")
        self.held.add(intent.idempotency_key)

    def guard_for(self, intent):
        @contextmanager
        def guard():
            if intent.idempotency_key not in self.held:
                raise RuntimeError("reservation_required")
            self.entries += 1
            yield

        return guard


def make_intent(key="generated-key", **changes):
    intent = mc.FitIntent(
        BINDING,
        "grant-v1",
        "benchmark-v1",
        managed.development_digest(ROWS),
        key,
        FIELDS,
        BOUND,
    )
    return replace(intent, **changes)


@pytest.fixture
def stack(tmp_path):
    provider = FakeProvider()
    connector = managed.ManagedConnector(provider, SETTINGS, tmp_path / "state")
    intent = make_intent()
    pre = mc.Preflight(connector, conformance.standard_grants(BINDING, intent))
    reservations = Reservations()
    return provider, connector, pre, intent, reservations


def dispatch(pre, intent, reservations, rows=ROWS):
    if intent.idempotency_key not in reservations.held:
        reservations.reserve(intent)
    return pre.submit_reserved_fit(
        ACTOR, intent, rows, reservation_guard=reservations.guard_for(intent)
    )


def journal_text(connector):
    return "".join(path.read_text() for path in (connector.root / "jobs").glob("*.jsonl"))


# ------------------------------------------------------------------ M1
def test_m1_full_lifecycle_through_preflight(stack):
    provider, connector, pre, intent, reservations = stack
    receipt = dispatch(pre, intent, reservations)
    assert receipt.state is mc.JobState.COMPLETED
    assert receipt.job_ref and receipt.evidence_ref
    assert connector.status(receipt) == receipt
    candidate = pre.collect_candidate(ACTOR, receipt)
    assert candidate.artifact_or_serving_revision == "simulated:tuned-model-1"
    assert candidate.parent_revision == BINDING.immutable_revision
    assert (
        connector.handoff_reference(candidate, "simulated:recipient-1") == "simulated:tuned-model-1"
    )
    usage = connector.usage(receipt)
    assert (usage.incurred_minor_units, usage.source, usage.irreversible) == (
        80,
        UsageSource.PROVIDER_REPORTED,
        False,
    )
    assert connector.predict_for_replay(candidate, ({"input_text": "generated-x"},))[0]["label"]
    assert provider.predictions[0][0] == "simulated:tuned-model-1"
    assert provider.executions == 1 and reservations.entries == 1


# ------------------------------------------------------------------ M2 / M3
def test_m2_dispatch_without_reservation_is_refused(stack):
    provider, connector, pre, intent, reservations = stack
    with pytest.raises(RuntimeError, match="reservation_required"):
        pre.submit_reserved_fit(
            ACTOR, intent, ROWS, reservation_guard=reservations.guard_for(intent)
        )
    with pytest.raises(TypeError):
        pre.submit_reserved_fit(ACTOR, intent, ROWS)
    assert provider.executions == 0 and provider.uploaded == []
    assert journal_text(connector) == ""


def test_m3_second_reservation_refused_and_one_execution(stack):
    provider, _, pre, intent, reservations = stack
    first = dispatch(pre, intent, reservations)
    with pytest.raises(RuntimeError, match="reservation_already_held"):
        reservations.reserve(intent)
    again = dispatch(pre, intent, reservations)
    assert again == first
    assert provider.executions == 1 and len(provider.uploaded) == 1


# ------------------------------------------------------------------ M4
@pytest.mark.parametrize("lost", ["drop_put_response", "drop_create_response"])
def test_m4_lost_acknowledgement_recovers_same_job_without_second_execution(stack, lost):
    provider, connector, pre, intent, reservations = stack
    setattr(provider, lost, True)
    recovered = dispatch(pre, intent, reservations)
    if lost == "drop_create_response":
        assert recovered.state is mc.JobState.COMPLETED
        assert connector.lookup(intent) == recovered
    else:
        # the file reached the provider but no job was verifiable: unknown, never success
        assert recovered.state is mc.JobState.UNKNOWN
    duplicate = dispatch(pre, intent, reservations)
    assert provider.executions <= 1 and len(provider.uploaded) == 1
    assert duplicate.state is recovered.state


# ------------------------------------------------------------------ M5 / M6
def test_m5_unverifiable_job_is_unknown_not_retried_and_reconciled_when_visible(stack):
    provider, connector, pre, intent, reservations = stack
    provider.blind = True
    provider.drop_create_response = True
    first = dispatch(pre, intent, reservations)
    second = dispatch(pre, intent, reservations)
    assert first.state is second.state is mc.JobState.UNKNOWN
    assert provider.executions == 1
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, first)
    provider.blind = False
    reconciled = pre.reconcile_fit(ACTOR, intent)
    assert reconciled.state is mc.JobState.COMPLETED
    assert provider.executions == 1
    assert pre.collect_candidate(ACTOR, reconciled).artifact_or_serving_revision


def test_m6_crash_before_provider_created_anything_is_not_silently_retried(stack):
    provider, connector, pre, intent, reservations = stack
    provider.fail_put_before = True
    first = dispatch(pre, intent, reservations)
    assert first.state is mc.JobState.UNKNOWN and provider.executions == 0
    provider.fail_put_before = False
    again = dispatch(pre, intent, reservations)
    assert again.state is mc.JobState.UNKNOWN and provider.executions == 0
    fresh = make_intent("generated-new-attempt")
    pre2 = mc.Preflight(connector, conformance.standard_grants(BINDING, fresh))
    assert dispatch(pre2, fresh, reservations).state is mc.JobState.COMPLETED
    assert provider.executions == 1


# ------------------------------------------------------------------ M7 / M8
def test_m7_cap_exceeding_job_is_refused_before_any_provider_call(stack):
    provider, connector, pre, intent, reservations = stack
    tight = replace(intent, bound=replace(BOUND, maximum_minor_units=50))
    pre = mc.Preflight(connector, conformance.standard_grants(BINDING, tight))
    with pytest.raises(mc.CapabilityError, match="known_training_cost_within_budget_required"):
        dispatch(pre, tight, reservations)
    assert provider.uploaded == [] and provider.executions == 0 and reservations.entries == 0
    provider.quote = lambda binding, rows, epochs: None
    unknown = replace(intent, idempotency_key="generated-unknown-cost")
    pre = mc.Preflight(connector, conformance.standard_grants(BINDING, unknown))
    with pytest.raises(mc.CapabilityError, match="known_training_cost_within_budget_required"):
        dispatch(pre, unknown, reservations)
    assert provider.executions == 0


def test_m8_no_enforceable_cap_refused_and_unenforced_ack_cancels(stack):
    provider, connector, pre, intent, reservations = stack
    provider.model = replace(provider.model, hard_cap_enforceable=False)
    assert pre.discover(ACTOR, BINDING, mc.Purpose.TRAINING).state is mc.State.NEEDS_CONFIGURATION
    with pytest.raises(mc.CapabilityError, match="enforceable_training_cap_unavailable"):
        dispatch(pre, intent, reservations)
    assert provider.executions == 0
    provider.model = replace(provider.model, hard_cap_enforceable=True)
    provider.cap_ack = None
    with pytest.raises(ValueError, match="provider_cap_not_enforced"):
        dispatch(pre, intent, reservations)
    assert provider.executions == 1 and provider.cancel_calls == 1
    job = connector.lookup(intent)
    assert job.state is mc.JobState.CANCELED
    with pytest.raises(mc.CapabilityError):
        pre.collect_candidate(ACTOR, job)


# ------------------------------------------------------------------ M9 / M10
def test_m9_cancel_is_durable_first_retryable_and_never_publishes(stack):
    provider, connector, pre, intent, reservations = stack
    provider.autocomplete = False
    job = dispatch(pre, intent, reservations)
    assert job.state is mc.JobState.RUNNING
    provider.fail_cancel = True
    with pytest.raises(OSError):
        connector.cancel(job)
    assert "cancel_requested" in journal_text(connector)
    # the intent is durable but the provider has not stopped: never reported as canceled
    pending = connector.lookup(intent)
    assert pending.state is mc.JobState.RUNNING and connector.usage(pending) is None
    with pytest.raises(ValueError, match="job_not_collectable"):
        connector.collect(pending)
    provider.fail_cancel = False
    canceled = connector.cancel(job)
    assert canceled.state is mc.JobState.CANCELED
    usage = connector.usage(canceled)
    assert usage.incurred_minor_units == 30 and usage.irreversible is True
    assert provider.executions == 1


def test_m9b_provider_completing_after_cancel_still_yields_no_candidate(stack):
    provider, connector, pre, intent, reservations = stack
    job = dispatch(pre, intent, reservations)  # provider already completed
    canceled = connector.cancel(job)
    assert canceled.state is mc.JobState.CANCELED
    with pytest.raises(ValueError, match="job_not_collectable"):
        connector.collect(canceled)


def test_m10_fence_cancels_live_jobs_blocks_effects_and_survives_restart(tmp_path):
    provider = FakeProvider(autocomplete=False)
    connector = managed.ManagedConnector(provider, SETTINGS, tmp_path / "s")
    assert isinstance(connector, Fenceable) and isinstance(connector, UsageReporting)
    intent = make_intent()
    pre = mc.Preflight(connector, conformance.standard_grants(BINDING, intent))
    reservations = Reservations()
    live = dispatch(pre, intent, reservations)
    receipt = connector.fence("generated-scope")
    assert receipt.canceled_job_refs == (live.job_ref,)
    restarted = managed.ManagedConnector(provider, SETTINGS, tmp_path / "s")
    later = make_intent("generated-after-fence")
    pre2 = mc.Preflight(restarted, conformance.standard_grants(BINDING, later))
    reservations.reserve(later)
    with pytest.raises(mc.CapabilityError):
        pre2.submit_reserved_fit(
            ACTOR, later, ROWS, reservation_guard=reservations.guard_for(later)
        )
    with pytest.raises(ValueError, match="connector_fenced"):
        restarted.predict_for_replay(
            mc.Candidate(intent, live.job_ref, BINDING.immutable_revision, "x", "y"), ()
        )
    assert provider.executions == 1
    assert restarted.fence("generated-scope").canceled_job_refs == ()


# ------------------------------------------------------------------ M11
def test_m11_missing_credential_fails_closed_and_nothing_leaks(stack):
    provider, connector, pre, intent, reservations = stack
    ok = dispatch(pre, make_intent("generated-ok"), reservations)
    assert ok.state is mc.JobState.COMPLETED
    provider.vault.clear()
    assert pre.discover(ACTOR, BINDING, mc.Purpose.TRAINING).state is mc.State.NEEDS_CONFIGURATION
    with pytest.raises(mc.CapabilityError) as refused:
        dispatch(pre, intent, reservations)
    assert SECRET not in str(refused.value) and provider.executions == 1
    with pytest.raises(LookupError) as raised:
        connector.dispose_training_file(ok)
    assert SECRET not in str(raised.value)
    assert SECRET not in journal_text(connector) and SECRET not in repr(ok)
    assert all(SECRET.encode() not in blob for blob in provider.uploaded)
    assert BINDING.credential_ref in journal_text(connector)  # a reference, never the value


# ------------------------------------------------------------------ M12 / M13
def test_m12_late_callbacks_and_later_reports_change_nothing(stack):
    provider, connector, pre, intent, reservations = stack
    done = dispatch(pre, intent, reservations)
    before = journal_text(connector)
    job_id = provider.jobs[f"opentour-{connector._key(intent)}"].job_id
    assert connector.ingest_provider_event(job_id, "failed") is False
    assert connector.ingest_provider_event(job_id, "running") is False
    assert connector.ingest_provider_event("simulated:unknown-job", "succeeded", "x") is False
    assert journal_text(connector) == before
    provider.final_state = "failed"
    token = f"opentour-{connector._key(intent)}"
    provider.jobs[token] = replace(provider.jobs[token], state="failed")
    assert connector.status(done) == done and connector.lookup(intent) == done
    assert pre.collect_candidate(ACTOR, done).artifact_or_serving_revision


def test_m12b_callback_applies_to_live_job_but_not_after_cancel(tmp_path):
    provider = FakeProvider(autocomplete=False)
    connector = managed.ManagedConnector(provider, SETTINGS, tmp_path / "s")
    intent = make_intent()
    pre = mc.Preflight(connector, conformance.standard_grants(BINDING, intent))
    reservations = Reservations()
    job = dispatch(pre, intent, reservations)
    token = f"opentour-{connector._key(intent)}"
    job_id = provider.jobs[token].job_id
    assert not connector.ingest_provider_event(job_id, "succeeded", "simulated:forged-model")
    assert connector.status(job).state is mc.JobState.RUNNING
    provider.jobs[token] = replace(
        provider.jobs[token], state="succeeded", tuned_model_id="simulated:tuned-model-2"
    )
    assert connector.ingest_provider_event(job_id, "running")
    assert connector.status(job).state is mc.JobState.COMPLETED
    other = make_intent("generated-other")
    pre2 = mc.Preflight(connector, conformance.standard_grants(BINDING, other))
    live = dispatch(pre2, other, reservations)
    connector.cancel(live)
    other_id = provider.jobs[f"opentour-{connector._key(other)}"].job_id
    assert (
        connector.ingest_provider_event(other_id, "succeeded", "simulated:tuned-model-3") is False
    )
    assert connector.lookup(other).state is mc.JobState.CANCELED


@pytest.mark.parametrize(
    "state, tuned", [("mystery", "x"), ("succeeded", ""), ("", "x"), ("SUCCEEDED", "x")]
)
def test_m13_unrecognized_or_incomplete_provider_state_is_unknown_never_success(
    stack, state, tuned
):
    provider, connector, pre, intent, reservations = stack
    provider.final_state = state
    provider.tuned_id = tuned
    receipt = dispatch(pre, intent, reservations)
    assert receipt.state is mc.JobState.UNKNOWN
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, receipt)
    with pytest.raises(ValueError, match="job_not_collectable"):
        connector.collect(receipt)
    assert connector.usage(receipt) is None


# ------------------------------------------------------------------ M14
def test_m14_training_disabled_and_missing_egress_grant_return_precise_states(stack):
    provider, connector, pre, intent, reservations = stack
    provider.model = replace(provider.model, training_supported=False)
    capability = pre.discover(ACTOR, BINDING, mc.Purpose.TRAINING)
    assert (capability.state, capability.reason) == (
        mc.State.BENCHMARK_ONLY,
        "training_unavailable",
    )
    provider.model = replace(provider.model, training_supported=True)
    base = conformance.standard_grants(BINDING, intent)

    def without_disclosure(actor, binding, purpose):
        return None if purpose is mc.Purpose.DISCLOSURE else base(actor, binding, purpose)

    no_egress = mc.Preflight(connector, without_disclosure)
    capability = no_egress.discover(ACTOR, BINDING, mc.Purpose.TRAINING)
    assert (capability.state, capability.reason) == (
        mc.State.NEEDS_CONFIGURATION,
        "training_disclosure_scope_unavailable",
    )
    with pytest.raises(mc.CapabilityError):
        dispatch(no_egress, intent, reservations)
    assert provider.executions == 0 and provider.uploaded == []
    provider.model = replace(provider.model, resolved_version="simulated:model-version-2")
    assert (
        pre.discover(ACTOR, BINDING, mc.Purpose.TRAINING).reason
        == "exact_model_revision_unverified"
    )


# ------------------------------------------------------------------ M15
def test_m15_protected_answers_never_reach_provider_and_handoff_is_scoped(stack):
    provider, connector, pre, intent, reservations = stack
    dispatch(pre, intent, reservations)
    payload = provider.uploaded[0].decode()
    assert "answer_key" not in payload and "generated-input-0" in payload
    leaking = replace(intent, fields=FIELDS | {"answer_key"})
    rows = tuple({**row, "answer_key": "generated-secret-answer"} for row in ROWS)
    with pytest.raises(ValueError, match="protected_field_in_payload"):
        connector.serialize_development(leaking, rows)
    receipt = connector.lookup(intent)
    candidate = pre.collect_candidate(ACTOR, receipt)
    with pytest.raises(ValueError, match="protected_field_in_payload"):
        connector.predict_for_replay(candidate, ({"input_text": "x", "answer_key": "y"},))
    with pytest.raises(ValueError, match="recipient_not_permitted"):
        connector.handoff_reference(candidate, "simulated:other-recipient")
    assert provider.predictions == []


# ------------------------------------------------------------------ M16 / M17
def test_m16_same_key_different_intent_is_refused(stack):
    provider, connector, pre, intent, reservations = stack
    dispatch(pre, intent, reservations)
    other = replace(intent, benchmark_revision="benchmark-other")
    with pytest.raises(ValueError, match="idempotency_conflict"):
        connector.lookup(other)
    with pytest.raises(ValueError, match="idempotency_conflict"):
        connector.submit(other, b"")
    assert provider.executions == 1


def test_m17_restart_replays_receipt_without_second_execution(stack, tmp_path):
    provider, connector, pre, intent, reservations = stack
    original = dispatch(pre, intent, reservations)
    restarted = managed.ManagedConnector(provider, SETTINGS, connector.root)
    pre2 = mc.Preflight(restarted, conformance.standard_grants(BINDING, intent))
    assert pre2.reconcile_fit(ACTOR, intent) == original
    assert dispatch(pre2, intent, reservations) == original
    assert pre2.collect_candidate(ACTOR, original).job_ref == original.job_ref
    assert provider.executions == 1 and len(provider.uploaded) == 1


# ------------------------------------------------------------------ M18
def test_m18_retention_disposition_recorded_once_and_unknown_usage_is_none(stack):
    provider, connector, pre, intent, reservations = stack
    provider.report_usage = False
    receipt = dispatch(pre, intent, reservations)
    assert connector.usage(receipt) is None
    assert connector.retention_disposition(receipt) == "unrecorded"
    assert connector.dispose_training_file(receipt) == "deleted"
    assert connector.dispose_training_file(receipt) == "deleted"
    assert len(provider.deleted) == 1
    provider.delete_ok = False
    other = make_intent("generated-retained")
    pre2 = mc.Preflight(connector, conformance.standard_grants(BINDING, other))
    kept = dispatch(pre2, other, reservations)
    assert connector.dispose_training_file(kept) == "retained"
    assert connector.retention_disposition(kept) == "retained"


def test_m18b_dispose_requires_a_terminal_job(tmp_path):
    provider = FakeProvider(autocomplete=False)
    connector = managed.ManagedConnector(provider, SETTINGS, tmp_path / "s")
    intent = make_intent()
    pre = mc.Preflight(connector, conformance.standard_grants(BINDING, intent))
    live = dispatch(pre, intent, Reservations())
    with pytest.raises(ValueError, match="job_not_terminal"):
        connector.dispose_training_file(live)
    assert provider.deleted == []


# ------------------------------------------------------------------ M19
def test_m19_concurrent_identical_dispatches_execute_once(stack):
    provider, _, pre, intent, reservations = stack
    reservations.reserve(intent)
    results = []

    def run():
        results.append(
            pre.submit_reserved_fit(
                ACTOR, intent, ROWS, reservation_guard=reservations.guard_for(intent)
            )
        )

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(results) == 8 and len(set(results)) == 1
    assert provider.executions == 1 and len(provider.uploaded) == 1


# ------------------------------------------------------------------ M20
PROVIDERS = {}


def _harness_connector(state, autocomplete=True):
    provider = PROVIDERS.setdefault(state, FakeProvider(autocomplete))
    return managed.ManagedConnector(provider, SETTINGS, state)


def test_m20_shared_conformance_suite_passes(tmp_path):
    def slow(state):
        PROVIDERS[state] = FakeProvider(autocomplete=False)
        return managed.ManagedConnector(PROVIDERS[state], SETTINGS, state)

    harness = conformance.ConnectorHarness(
        make_connector=_harness_connector,
        binding=BINDING,
        mismatched_binding=replace(BINDING, immutable_revision="simulated:model-version-2"),
        make_intent=make_intent,
        rows=ROWS,
        executions=lambda state: PROVIDERS[state].executions,
        make_slow_connector=slow,
    )
    results = conformance.run_conformance(harness, tmp_path / "conformance")
    assert len(results) == 10
    assert conformance.all_passed(results), [r for r in results if r.outcome.value != "passed"]


# ------------------------------------------------------------------ M21
def test_m21_foreign_binding_refused_and_evidence_tier_is_fixture(stack):
    provider, connector, pre, intent, reservations = stack
    for foreign in (
        replace(BINDING, account_or_host="generated-other-account.invalid"),
        replace(BINDING, region="simulated:region-2"),
        replace(BINDING, route=mc.Route.CUSTOM),
    ):
        with pytest.raises(ValueError, match="binding_not_served_by_connector"):
            connector.observe(foreign, mc.Purpose.TRAINING)
    assert connector.evidence_tier is EvidenceTier.FIXTURE
    assert "installed_provider_job_in_authorized_test_account" in managed.EXTERNAL_GATES
    assert provider.executions == 0
    observation = connector.observe(BINDING, mc.Purpose.TRAINING)
    assert observation.source_ref.startswith("simulated:") and observation.resolved_revision == (
        BINDING.immutable_revision
    )
    json.dumps(managed._binding_document(BINDING))


# ------------------------------------------------------------------ independent-review regressions
class StubbornProvider(FakeProvider):
    """A provider that acknowledges a cancel but leaves the job running."""

    def cancel_job(self, binding, job_id):
        self._auth(binding)
        self.cancel_calls += 1
        return self._by_id(job_id)[1]


def _live(tmp_path, provider):
    connector = managed.ManagedConnector(provider, SETTINGS, tmp_path / "s")
    intent = make_intent()
    pre = mc.Preflight(connector, conformance.standard_grants(BINDING, intent))
    return connector, pre, intent, dispatch(pre, intent, Reservations())


def test_r1_cancel_the_provider_did_not_honor_is_not_reported_canceled(tmp_path):
    provider = StubbornProvider(autocomplete=False)
    connector, _, intent, live = _live(tmp_path, provider)
    receipt = connector.cancel(live)
    assert receipt.state is mc.JobState.RUNNING  # still spending: the caller must keep its hold
    assert connector.lookup(intent).state is mc.JobState.RUNNING
    assert connector.usage(receipt) is None


def test_r2_fence_never_lists_a_job_the_provider_did_not_stop(tmp_path):
    provider = StubbornProvider(autocomplete=False)
    connector, pre, _, _ = _live(tmp_path, provider)
    with pytest.raises(ValueError, match="FENCE_JOB_NOT_STOPPED"):
        connector.fence("generated-scope")
    later = make_intent("generated-after-fence")  # the fence itself is durable anyway
    with pytest.raises(mc.CapabilityError):
        dispatch(
            mc.Preflight(connector, conformance.standard_grants(BINDING, later)),
            later,
            Reservations(),
        )
    assert provider.executions == 1


def test_r3_cost_incurred_by_a_job_that_completed_after_cancel_is_irreversible(stack):
    provider, connector, pre, intent, reservations = stack
    done = dispatch(pre, intent, reservations)  # provider completed and billed 80
    canceled = connector.cancel(done)
    usage = connector.usage(canceled)
    assert canceled.state is mc.JobState.CANCELED
    assert (usage.incurred_minor_units, usage.irreversible) == (80, True)
    assert provider.deleted == []  # declared gap: the tuned model is not deleted here


@pytest.mark.parametrize(
    "bad",
    [
        managed.ProviderUsage("USD", -5, 1, 1, 1),
        managed.ProviderUsage("", 5, 1, 1, 1),
        managed.ProviderUsage("USD", 5.5, 1, 1, 1),
        managed.ProviderUsage("USD", True, 1, 1, 1),
    ],
)
def test_r4_malformed_provider_usage_is_unknown_never_settleable(stack, bad):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: bad
    receipt = dispatch(pre, intent, reservations)
    assert receipt.state is mc.JobState.COMPLETED
    assert connector.usage(receipt) is None  # None holds the reservation; no estimate stands in


def test_r5_direct_submit_rechecks_the_bytes_that_leave(stack):
    provider, connector, _, intent, _ = stack
    rows = tuple({**row, "answer_key": "generated-secret-answer"} for row in ROWS)
    payload = managed.serialize_rows(rows)
    forged = replace(
        intent,
        development_digest=managed._sha(payload),
        fields=FIELDS | {"answer_key"},
    )
    with pytest.raises(ValueError, match="training_payload_invalid"):
        connector.submit(forged, payload)
    many = tuple({**ROWS[0], "label": f"generated-{i}"} for i in range(21))
    oversize = replace(intent, development_digest=managed.development_digest(many))
    with pytest.raises(ValueError, match="training_payload_invalid"):
        connector.submit(oversize, managed.serialize_rows(many))
    outer = b'{"fields":{"label":"billing","text":"generated"},"answer_key":"generated-secret"}\n'
    outer_intent = replace(intent, development_digest=managed._sha(outer))
    with pytest.raises(ValueError, match="training_payload_invalid"):
        connector.submit(outer_intent, outer)
    assert provider.uploaded == [] and provider.executions == 0
    assert journal_text(connector) == ""


def test_r6_handoff_and_replay_are_verified_against_the_journal(tmp_path):
    provider = FakeProvider()
    connector, pre, intent, done = _live(tmp_path, provider)
    candidate = pre.collect_candidate(ACTOR, done)
    forged = replace(candidate, artifact_or_serving_revision="simulated:tuned-model-other")
    with pytest.raises(ValueError, match="candidate_lineage_unverified"):
        connector.handoff_reference(forged, "simulated:recipient-1")
    foreign = replace(candidate, intent=replace(intent, benchmark_revision="benchmark-other"))
    with pytest.raises(ValueError, match="candidate_lineage_unverified"):
        connector.predict_for_replay(foreign, ())
    assert connector.handoff_reference(candidate, "simulated:recipient-1")
    connector.cancel(done)  # cancel after collection: the earlier candidate is now void
    with pytest.raises(ValueError, match="candidate_lineage_unverified"):
        connector.handoff_reference(candidate, "simulated:recipient-1")
    with pytest.raises(ValueError, match="candidate_lineage_unverified"):
        connector.predict_for_replay(candidate, ({"input_text": "x"},))
    assert provider.predictions == []


def test_r7_forged_receipt_changes_nothing(stack):
    provider, connector, pre, intent, reservations = stack
    real = dispatch(pre, intent, reservations)
    other = make_intent("generated-never-dispatched")
    forged = mc.JobReceipt(other, mc.JobState.COMPLETED, real.job_ref, real.evidence_ref)
    for call in (connector.status, connector.cancel):
        with pytest.raises(ValueError, match="job_receipt_mismatched"):
            call(forged)
    with pytest.raises(ValueError, match="job_not_collectable"):
        connector.collect(forged)
    assert provider.cancel_calls == 0 and connector.lookup(intent) == real


def test_lost_create_ack_does_not_bypass_required_provider_cap(stack):
    provider, connector, pre, intent, reservations = stack
    provider.cap_ack = None
    provider.drop_create_response = True
    receipt = dispatch(pre, intent, reservations)
    assert receipt.state is mc.JobState.CANCELED
    assert provider.executions == 1
    assert provider.cancel_calls == 1
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, receipt)
    assert connector.lookup(intent).state is mc.JobState.CANCELED


def test_callback_rechecks_provider_cap_before_adopting_candidate(tmp_path):
    provider = FakeProvider(autocomplete=False)
    connector = managed.ManagedConnector(provider, SETTINGS, tmp_path / "state")
    intent = make_intent()
    pre = mc.Preflight(connector, conformance.standard_grants(BINDING, intent))
    receipt = dispatch(pre, intent, Reservations())
    token = f"opentour-{connector._key(intent)}"
    provider.jobs[token] = replace(
        provider.jobs[token],
        state="succeeded",
        tuned_model_id="simulated:unbounded-candidate",
        cap_minor_units=intent.bound.maximum_minor_units + 1,
    )
    assert connector.ingest_provider_event(provider.jobs[token].job_id, "succeeded")
    assert provider.cancel_calls == 1
    assert connector.status(receipt).state is mc.JobState.CANCELED
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, receipt)


# --------------------------------------------------------- follow-up: usage cap, value types
@pytest.mark.parametrize(
    "minor_units, status, settleable",
    [
        (10**9, "over_cap", False),  # the reviewer's reproduction: 10**9 against a cap of 500
        (501, "over_cap", False),
        (500, "within_cap", True),  # exactly at the approved cap is not over it
        (499, "within_cap", True),
        (0, "within_cap", True),
    ],
)
def test_u1_provider_usage_is_compared_to_the_approved_cap_and_never_clamped(
    stack, minor_units, status, settleable
):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: managed.ProviderUsage("USD", minor_units, 25, 4, 12)
    receipt = dispatch(pre, intent, reservations)
    usage = connector.usage(receipt)
    assert usage.incurred_minor_units == minor_units  # the real figure, never clamped
    assert usage.source is UsageSource.PROVIDER_REPORTED
    assert usage.cap_status == status
    assert usage.approved_maximum_minor_units == 500
    assert usage.settleable is settleable


def test_u2_unknown_approved_maximum_is_not_settleable(stack):
    provider, connector, pre, intent, reservations = stack
    receipt = dispatch(pre, intent, reservations)
    path = connector._journal(connector._key(intent))
    rewritten = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        event.pop("maximum_minor_units", None)
        rewritten.append(json.dumps(event))
    path.write_text("\n".join(rewritten) + "\n")
    usage = connector.usage(receipt)
    assert usage.incurred_minor_units == 80 and usage.cap_status == "cap_unknown"
    assert usage.settleable is False


@pytest.mark.parametrize(
    "bad_row",
    [
        {"input_text": {"n": [1]}, "label": "b"},
        {"input_text": ["a"], "label": "b"},
        {"input_text": "a", "label": {"nested": "x"}},
        {"input_text": 7, "label": "b"},
        {"input_text": None, "label": "b"},
        {"input_text": True, "label": "b"},
    ],
)
def test_v1_non_string_field_values_are_refused_in_the_outbound_envelope(stack, bad_row):
    provider, connector, _, intent, _ = stack
    payload = managed.serialize_rows((bad_row,))
    forged = replace(intent, development_digest=managed._sha(payload))
    with pytest.raises(ValueError, match="training_payload_invalid"):
        connector.submit(forged, payload)
    with pytest.raises(ValueError, match="training_row_value_not_string"):
        connector.serialize_development(intent, (bad_row,))
    assert provider.uploaded == [] and provider.executions == 0
    assert journal_text(connector) == ""


def test_v2_ordinary_string_values_are_still_accepted(stack):
    provider, connector, pre, intent, reservations = stack
    rows = (
        {"input_text": "", "label": "unicode-é-ok"},
        {"input_text": "x" * 50, "label": "b"},
    )
    intent = replace(intent, development_digest=managed.development_digest(rows))
    reservations.reserve(intent)
    receipt = pre.submit_reserved_fit(
        ACTOR, intent, rows, reservation_guard=reservations.guard_for(intent)
    )
    assert receipt.state is mc.JobState.COMPLETED
    assert provider.uploaded == [managed.serialize_rows(rows)]


# ---------------------------------------------------- follow-up: currency must match the cap
@pytest.mark.parametrize(
    "currency, units, status, settleable",
    [
        ("USD", 400, "within_cap", True),
        ("USD", 501, "over_cap", False),
        ("JPY", 400, "cap_unknown", False),
        ("EUR", 400, "cap_unknown", False),
        ("usd", 400, "cap_unknown", False),  # exact match only: a lower-case code is not USD
        ("XXX", 400, "cap_unknown", False),
        ("JPY", 10**9, "cap_unknown", False),
    ],
)
def test_c1_currency_must_match_the_approved_currency(stack, currency, units, status, settleable):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: managed.ProviderUsage(currency, units, 25, 4, 12)
    usage = connector.usage(dispatch(pre, intent, reservations))
    assert usage.currency == currency and usage.incurred_minor_units == units  # real, unclamped
    assert usage.cap_status == status and usage.settleable is settleable
    assert usage.approved_currency == "USD"


def test_c2_older_journal_without_currency_reads_as_cap_unknown(stack):
    provider, connector, pre, intent, reservations = stack
    receipt = dispatch(pre, intent, reservations)
    path = connector._journal(connector._key(intent))
    lines = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        event.pop("currency", None)
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n")
    usage = connector.usage(receipt)  # reads, does not raise
    assert usage.incurred_minor_units == 80 and usage.currency == "USD"
    assert usage.cap_status == "cap_unknown" and usage.settleable is False
    assert usage.approved_currency is None


def test_c3_negative_journaled_maximum_reports_no_approved_maximum(stack):
    provider, connector, pre, intent, reservations = stack
    receipt = dispatch(pre, intent, reservations)
    path = connector._journal(connector._key(intent))
    lines = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if "maximum_minor_units" in event:
            event["maximum_minor_units"] = -1
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n")
    usage = connector.usage(receipt)
    assert usage.cap_status == "cap_unknown" and usage.settleable is False
    assert usage.approved_maximum_minor_units is None


# ------------------------------------------- follow-up: over-cap usage is a hard refusal (H1-H6)
# H1 a COMPLETED job reporting usage over its cap is held as ``over_cap``: no candidate, no handoff
# H2 missing, non-finite, negative, non-integer or bool usage is held as ``usage_unverifiable``
# H3 usage not comparable to the approved cap (currency, unrecorded maximum or currency) is held
#    as ``cap_unverifiable``
# H4 a job exactly at the cap is accepted
# H5 an under-cap job is unchanged
# H6 ``hold_reason`` checks the receipt and reports nothing for a job without a result
# Every hold refuses the same way; only the fixed reason differs, so an operator can tell
# "spent too much" from "cannot tell what was spent".
TUNED = "simulated:tuned-model-1"


def _hand_built(receipt):
    """What ``collect`` would return; built by hand so handoff is tested without ``collect``."""
    return mc.Candidate(
        receipt.intent,
        receipt.job_ref,
        receipt.intent.binding.immutable_revision,
        TUNED,
        "simulated:managed-replay:hand-built",
    )


def _assert_held(provider, connector, pre, receipt, reason):
    exact = f"^{reason}$"
    with pytest.raises(ValueError, match=exact):
        connector.collect(receipt)
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, receipt)
    candidate = _hand_built(receipt)
    with pytest.raises(ValueError, match=exact):
        connector.handoff_reference(candidate, "simulated:recipient-1")
    with pytest.raises(ValueError, match=exact):
        connector.predict_for_replay(candidate, ({"input_text": "generated-x"},))
    assert provider.predictions == []
    assert connector.hold_reason(receipt) == reason


def _assert_accepted(provider, connector, pre, receipt):
    assert receipt.state is mc.JobState.COMPLETED
    candidate = pre.collect_candidate(ACTOR, receipt)
    assert candidate == connector.collect(receipt)
    assert candidate.artifact_or_serving_revision == TUNED
    assert connector.handoff_reference(candidate, "simulated:recipient-1") == TUNED
    assert connector.predict_for_replay(candidate, ({"input_text": "generated-x"},))
    assert provider.predictions[-1][0] == TUNED
    assert connector.hold_reason(receipt) is None


@pytest.mark.parametrize("minor_units", [10**9, 501])
def test_h1_completed_job_over_its_cap_is_held_and_never_handed_off(stack, minor_units):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: managed.ProviderUsage("USD", minor_units, 25, 4, 12)
    receipt = dispatch(pre, intent, reservations)
    _assert_held(provider, connector, pre, receipt, "over_cap")
    usage = connector.usage(receipt)  # the real figure stays visible and unsettleable
    assert usage.incurred_minor_units == minor_units and usage.cap_status == "over_cap"
    assert usage.settleable is False
    # a restart replays the hold from the journal alone
    restarted = managed.ManagedConnector(provider, SETTINGS, connector.root)
    restarted_pre = mc.Preflight(restarted, pre.current_grant)
    _assert_held(provider, restarted, restarted_pre, receipt, "over_cap")


@pytest.mark.parametrize(
    "reported",
    [
        None,  # the provider reported no usage
        managed.ProviderUsage("USD", float("inf"), 25, 4, 12),
        managed.ProviderUsage("USD", float("nan"), 25, 4, 12),
        managed.ProviderUsage("USD", -1, 25, 4, 12),
        managed.ProviderUsage("USD", 5.5, 25, 4, 12),
        managed.ProviderUsage("USD", 400.0, 25, 4, 12),
        managed.ProviderUsage("USD", True, 25, 4, 12),
        managed.ProviderUsage("USD", "400", 25, 4, 12),
        managed.ProviderUsage("USD", 400, -1, 4, 12),
        managed.ProviderUsage("", 400, 25, 4, 12),
    ],
)
def test_h2_unverifiable_provider_usage_is_held(stack, reported):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: reported
    receipt = dispatch(pre, intent, reservations)
    assert connector.usage(receipt) is None  # unknown, never an invented zero
    _assert_held(provider, connector, pre, receipt, "usage_unverifiable")


@pytest.mark.parametrize(
    "journaled",
    [
        {"minor_units": "Infinity"},
        {"minor_units": "NaN"},
        {"minor_units": -1},
        {"minor_units": 5.5},
        {"minor_units": 400.0},
        {"minor_units": True},
        {"minor_units": "400"},
        {"minor_units": None},
        {"seconds": -1},
    ],
)
def test_h2b_unverifiable_usage_read_back_from_the_journal_is_held(stack, journaled):
    provider, connector, pre, intent, reservations = stack
    receipt = dispatch(pre, intent, reservations)  # usage 80, within the cap of 500
    path = connector._journal(connector._key(intent))
    lines = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        if event["event"] == "terminal":
            event["usage"].update(journaled)
        lines.append(json.dumps(event))
    text = "\n".join(lines) + "\n"
    text = text.replace('"Infinity"', "Infinity").replace('"NaN"', "NaN")  # non-finite JSON
    path.write_text(text)
    _assert_held(provider, connector, pre, receipt, "usage_unverifiable")


@pytest.mark.parametrize("currency", ["JPY", "usd", "XXX"])
def test_h3_usage_in_another_currency_is_held(stack, currency):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: managed.ProviderUsage(currency, 400, 25, 4, 12)
    receipt = dispatch(pre, intent, reservations)
    assert connector.usage(receipt).cap_status == "cap_unknown"
    _assert_held(provider, connector, pre, receipt, "cap_unverifiable")


@pytest.mark.parametrize("field", ["maximum_minor_units", "currency"])
def test_h3b_unrecorded_approved_cap_is_held(stack, field):
    provider, connector, pre, intent, reservations = stack
    receipt = dispatch(pre, intent, reservations)
    path = connector._journal(connector._key(intent))
    lines = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        event.pop(field, None)
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n")
    _assert_held(provider, connector, pre, receipt, "cap_unverifiable")


def test_h4_job_exactly_at_its_cap_is_accepted(stack):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: managed.ProviderUsage("USD", BOUND.maximum_minor_units, 25, 4, 12)
    receipt = dispatch(pre, intent, reservations)
    _assert_accepted(provider, connector, pre, receipt)
    assert connector.usage(receipt).settleable is True


@pytest.mark.parametrize("minor_units", [0, 80, 499])
def test_h5_under_cap_job_is_unchanged(stack, minor_units):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: managed.ProviderUsage("USD", minor_units, 25, 4, 12)
    receipt = dispatch(pre, intent, reservations)
    _assert_accepted(provider, connector, pre, receipt)
    usage = connector.usage(receipt)
    assert usage.incurred_minor_units == minor_units and usage.cap_status == "within_cap"


def test_h6_hold_reason_checks_the_receipt_and_is_none_for_jobs_without_a_result(stack):
    provider, connector, pre, intent, reservations = stack
    provider.autocomplete = False
    live = dispatch(pre, intent, reservations)
    assert live.state is mc.JobState.RUNNING and connector.hold_reason(live) is None
    forged = mc.JobReceipt(make_intent("generated-other"), live.state, live.job_ref, "x")
    with pytest.raises(ValueError, match="job_receipt_mismatched"):
        connector.hold_reason(forged)


def test_h7_unverifiable_usage_is_reported_before_an_unusable_cap(stack):
    provider, connector, pre, intent, reservations = stack
    provider._usage = lambda: None
    receipt = dispatch(pre, intent, reservations)
    path = connector._journal(connector._key(intent))
    lines = []
    for line in path.read_text().splitlines():
        event = json.loads(line)
        event.pop("currency", None)
        lines.append(json.dumps(event))
    path.write_text("\n".join(lines) + "\n")
    _assert_held(provider, connector, pre, receipt, "usage_unverifiable")
    reasons = (
        managed.HOLD_OVER_CAP,
        managed.HOLD_USAGE_UNVERIFIABLE,
        managed.HOLD_CAP_UNVERIFIABLE,
    )
    assert reasons == ("over_cap", "usage_unverifiable", "cap_unverifiable")
