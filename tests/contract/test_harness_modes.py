"""C03 positive/adverse consumer probes. Product/real-provider acceptance is NOT_RUN."""

from dataclasses import replace
from pathlib import Path

import pytest

from tools.fixtures.cell_store import InjectedCommitFailure, LocalCellStore, generated_guard
from tools.fixtures.evidence import route_empirical
from tools.fixtures.ports import AcknowledgmentLost, FixtureRefusal
from tools.fixtures.registry import ISSUER, MODES, Clock, digest, generated_scope, load_registry
from tools.fixtures.replay import SubmissionProbe
from tools.fixtures.scenarios import run_fixture, submission
from tools.fixtures.scripted_providers import (
    ScriptedAWSQueue,
    ScriptedBatch,
    ScriptedMetronome,
    ScriptedS3,
    ScriptedStripe,
    ScriptedWorkOS,
)

REGISTRY = Path(__file__).resolve().parents[2] / "tools/fixtures/registry.json"


def fixture_for(mode, fault="provider_accept_before_ack"):
    return next(
        f
        for f in load_registry(REGISTRY)
        if f.scope.mode == mode and f.provider_behavior_script == (fault,)
    )


@pytest.mark.parametrize("mode", MODES)
def test_ac01_lost_ack_restarts_without_second_submit_or_absence_assumption(mode, tmp_path):
    fixture = fixture_for(mode)
    clock = Clock.from_wire(fixture.clock)
    request = submission(fixture)
    provider = ScriptedBatch(("ack_lost",), invisible_lookups=1)
    path = tmp_path / "cell.sqlite"
    with LocalCellStore(path, fixture.scope, clock.now) as store:
        first = SubmissionProbe(request, store, provider, 1, "1")
        assert first.run() == "outcome_unknown"
        assert provider.submit_count == 1
    with LocalCellStore(path, fixture.scope, clock.now) as recovered:
        retry = SubmissionProbe(request, recovered, provider, 1, "1")
        assert retry.run() == "outcome_unknown"  # An empty search is not nonacceptance.
        assert retry.run() == "applied"
        assert retry.provider_job_id == provider.jobs[0].provider_job_id
        assert provider.submit_count == 1


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("failure", ["before_commit_failure", "after_commit_failure"])
def test_interrupted_local_commit_reopens_and_records_one_effect(mode, failure, tmp_path):
    fixture = fixture_for(mode)
    clock, request = Clock.from_wire(fixture.clock), submission(fixture)
    path = tmp_path / "cell.sqlite"
    provider = ScriptedBatch()
    with LocalCellStore(path, fixture.scope, clock.now) as store:
        setattr(store, failure, True)
        with pytest.raises(InjectedCommitFailure):
            SubmissionProbe(request, store, provider, 1, "1").run()
        assert provider.submit_count == 0
    with LocalCellStore(path, fixture.scope, clock.now) as recovered:
        assert SubmissionProbe(request, recovered, provider, 1, "1").run() == "applied"
        assert [record[2] for record in recovered.ledger()] == [
            "prepared",
            "dispatching",
            "applied",
        ]
        assert provider.submit_count == 1


def test_ac02_both_modes_have_equal_semantics_and_distinct_identities(tmp_path):
    for fault in {f.provider_behavior_script[0] for f in load_registry(REGISTRY)}:
        observations = []
        scopes = []
        for mode in MODES:
            fixture = fixture_for(mode, fault)
            directory = tmp_path / fault / mode
            directory.mkdir(parents=True)
            result = run_fixture(fixture, directory)
            observations.append((result["observed_effects"], result["observed_ledger_entries"]))
            scopes.append(fixture.scope)
        assert observations[0] == observations[1]
        assert scopes[0] != scopes[1]
        assert scopes[0].organization_id != scopes[1].organization_id
        assert (
            submission(fixture_for(MODES[0], fault)).submission_id
            != submission(fixture_for(MODES[1], fault)).submission_id
        )


def test_ac03_all_twelve_directed_cross_cell_pairs_are_refused():
    scopes = [generated_scope(mode, tenant) for mode in MODES for tenant in (1, 2)]
    queue = ScriptedAWSQueue()
    for worker in scopes:
        for envelope in scopes:
            if worker == envelope:
                assert queue.deliver(worker, envelope, "simulated:same-visible-id", digest("same"))
            else:
                with pytest.raises(FixtureRefusal, match="RESOURCE_NOT_FOUND"):
                    queue.deliver(worker, envelope, "simulated:same-visible-id", digest("same"))
    assert len(queue.accepted) == 4
    assert len(queue.refused) == 12


@pytest.mark.parametrize("mode", MODES)
def test_ac03_guard_is_rechecked_between_prepare_and_dispatch(mode, tmp_path):
    scope = generated_scope(mode)
    clock = Clock.from_wire("2026-01-01T00:00:00.000Z")
    with LocalCellStore(tmp_path / "cell.sqlite", scope, clock.now) as store:
        assert store.prepare("simulated:effect", scope, 1, "1", digest("request"))
        newer = generated_guard(scope)
        newer.update(revocation_epoch=2, revision="2")
        store.install_guard(newer)
        with pytest.raises(FixtureRefusal, match="AUTHORITY_STALE"):
            store.resolve("simulated:effect", scope, "dispatching")
        assert store.state("simulated:effect", scope) == "prepared"
        store.resolve("simulated:effect", scope, "cancelled_before_dispatch")


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("expiry", ["2026-01-01T00:02:00Z", "2026-01-01T00:02:00.000Z"])
def test_guard_expiry_compares_instants_at_precision_boundary(mode, expiry, tmp_path):
    scope = generated_scope(mode)
    clock = Clock.from_wire("2026-01-01T00:01:59.999Z")
    with LocalCellStore(tmp_path / "cell.sqlite", scope, clock.now) as store:
        guard = generated_guard(scope)
        guard.update(revision="2", snapshot_sequence=2, valid_until=expiry)
        store.install_guard(guard)
        store.check(scope, 1, "2", clock.now())
        for now in ("2026-01-01T00:02:00.000Z", "2026-01-01T00:02:00Z", "2026-01-01T00:02:00.001Z"):
            with pytest.raises(FixtureRefusal, match="AUTHORITY_STALE"):
                store.check(scope, 1, "2", now)


@pytest.mark.parametrize("mode", MODES)
def test_lookup_filters_wrong_tags_and_paginates_before_binding(mode, tmp_path):
    fixture = fixture_for(mode)
    request = submission(fixture)
    clock = Clock.from_wire(fixture.clock)
    provider = ScriptedBatch(("ack_lost",), page_size=1)
    with LocalCellStore(tmp_path / "cell.sqlite", fixture.scope, clock.now) as store:
        probe = SubmissionProbe(request, store, provider, 1, "1")
        assert probe.run() == "outcome_unknown"
        actual = provider.jobs[0]
        provider.jobs.insert(
            0,
            replace(
                actual,
                provider_job_id="simulated:wrong-tags",
                submission=replace(request, request_digest=digest("other")),
            ),
        )
        assert probe.run() == "applied"
        assert probe.provider_job_id == actual.provider_job_id
        assert provider.lookup_count == 2


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("lost_ack", [False, True])
def test_cancel_acknowledgment_never_means_terminal(mode, lost_ack):
    request = submission(fixture_for(mode))
    provider = ScriptedBatch()
    job = provider.submit(request)
    provider.cancel_ack_lost = lost_ack
    acknowledgment = provider.cancel(job.provider_job_id, request.scope)
    assert acknowledgment.request_acknowledged is (None if lost_ack else True)
    assert not acknowledgment.terminal_observed
    assert not provider.observe_terminal(job.provider_job_id, request.scope)
    assert not provider.jobs[0].terminal
    provider.scripted_terminal_ids.add(job.provider_job_id)
    assert provider.observe_terminal(job.provider_job_id, request.scope)
    with pytest.raises(FixtureRefusal, match="RESOURCE_NOT_FOUND"):
        provider.cancel(job.provider_job_id, generated_scope(mode, 2))


@pytest.mark.parametrize("mode", MODES)
def test_billing_usage_and_objects_refuse_known_foreign_identity(mode):
    own, foreign = generated_scope(mode), generated_scope(mode, 2)
    usage = ScriptedMetronome()
    usage.transaction_owners["simulated:transaction"] = own
    usage.lose_ack = True
    with pytest.raises(AcknowledgmentLost):
        usage.export(own, "simulated:transaction", digest("usage"))
    assert usage.export(own, "simulated:transaction", digest("usage")) == "accepted"
    assert len(usage.accepted) == 1
    assert usage.export_attempts[0] == usage.export_attempts[1]
    with pytest.raises(FixtureRefusal, match="RESOURCE_NOT_FOUND"):
        usage.export(foreign, "simulated:transaction", digest("usage"))
    with pytest.raises(FixtureRefusal, match="USAGE_EVENT_CONFLICT"):
        usage.export(own, "simulated:transaction", digest("changed usage"))
    billing = ScriptedStripe()
    billing.customers[own, "simulated:customer"] = "cancelled"
    assert (
        billing.event(own, "simulated:old-paid", digest("old event"), "simulated:customer")
        == "cancelled"
    )
    with pytest.raises(FixtureRefusal, match="RESOURCE_NOT_FOUND"):
        billing.current_state(foreign, "simulated:customer")
    objects = ScriptedS3()
    checksum = objects.stage(own, "simulated:object", b"generated-own-tenant-sentinel")
    with pytest.raises(FixtureRefusal, match="RESOURCE_NOT_FOUND"):
        objects.read(foreign, "simulated:object", checksum)


@pytest.mark.parametrize("mode", MODES)
def test_event_digest_conflict_and_unknown_membership_never_restore_authority(mode):
    own, foreign = generated_scope(mode), generated_scope(mode, 2)
    queue = ScriptedAWSQueue()
    assert queue.deliver(own, own, "simulated:event", digest("first"))
    with pytest.raises(FixtureRefusal, match="IDEMPOTENCY_CONFLICT"):
        queue.deliver(own, own, "simulated:event", digest("changed"))
    identity = ScriptedWorkOS()
    identity.memberships["simulated:subject", own] = "active"
    assert identity.current_membership("simulated:subject", own) == "active"
    assert identity.current_membership("simulated:subject", foreign) == "reconciliation_unknown"
    identity.memberships["simulated:subject", own] = "removed"
    assert (
        identity.event("simulated:event", digest("removed"), "simulated:subject", own) == "removed"
    )
    with pytest.raises(FixtureRefusal, match="IDEMPOTENCY_CONFLICT"):
        identity.event("simulated:event", digest("older-active"), "simulated:subject", own)
    assert identity.current_membership("simulated:subject", own) == "removed"


@pytest.mark.parametrize("kind", ["simulated", "local_integration", "provider_integration"])
def test_simulated_receipts_never_reach_empirical_sink(kind):
    received = []
    with pytest.raises(FixtureRefusal, match="SIMULATED_EVIDENCE_FORBIDDEN"):
        route_empirical(
            {"kind": kind, "issuer": ISSUER, "resource_namespace": ISSUER}, received.append
        )
    assert not received
