"""Executable generated fault scenarios, independent of registry expectations."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

from tools.fixtures.cell_store import (
    InjectedCommitFailure,
    LocalCellStore,
    ScriptedJournal,
    generated_guard,
)
from tools.fixtures.evidence import observation
from tools.fixtures.ports import FixtureRefusal, Submission
from tools.fixtures.registry import ISSUER, Clock, Fixture, digest, generated_scope
from tools.fixtures.replay import SubmissionProbe, guarded_effect_probe
from tools.fixtures.scripted_providers import (
    ScriptedAWSQueue,
    ScriptedBatch,
    ScriptedS3,
    ScriptedSource,
    ScriptedWorkOS,
)


def submission(fixture: Fixture) -> Submission:
    return Submission(
        fixture.scope.cell_id + ":submission",
        fixture.scope.cell_id + ":attempt",
        fixture.scope,
        digest({"input": "generated"}),
        "simulated:account",
        "simulated:region",
        "simulated:queue",
        "simulated:definition:1",
        "simulated-job",
        fixture.clock,
    )


def run_fixture(fixture: Fixture, directory: Path) -> dict[str, Any]:
    if len(fixture.provider_behavior_script) != 1:
        raise ValueError("Version 1 scenarios require exactly one registered fault point")
    fault = fixture.provider_behavior_script[0]
    clock = Clock.from_wire(fixture.clock)
    effects: list[str] = []
    ledger: list[str] = []
    kind = "simulated"
    scope = fixture.scope
    local_faults = {
        "normal",
        "before_local_commit",
        "after_local_commit",
        "provider_accept_before_ack",
        "lost_heartbeat",
        "expired_guard",
        "duplicate_provider_acceptance",
        "revoked_epoch",
        "late_receipt",
    }
    if fault in local_faults:
        kind = "local_integration"
        with LocalCellStore(directory / "cell.sqlite", scope, clock.now) as store:
            provider = ScriptedBatch(("ack",) if fault == "normal" else ("ack_lost",))
            request = submission(fixture)
            probe = SubmissionProbe(request, store, provider, 1, "1")
            try:
                if fault == "before_local_commit":
                    store.before_commit_failure = True
                if fault == "after_local_commit":
                    store.after_commit_failure = True
                if fault == "expired_guard":
                    clock.advance(120)
                if fault in {"revoked_epoch", "late_receipt"}:
                    guard = generated_guard(scope)
                    guard.update(revocation_epoch=2, revision="2", snapshot_sequence=1)
                    if fault == "late_receipt":
                        effects.append(probe.run())
                    store.install_guard(guard)
                    if fault == "late_receipt":
                        # Historical reconciliation records the actual prior acceptance;
                        # it does not supply current authority for publication.
                        effects.append(probe.run())
                    store.check(scope, 1, "1", clock.now())
                effects.append(probe.run())
            except (InjectedCommitFailure, FixtureRefusal) as exc:
                effects.append(str(exc))
            if fault == "after_local_commit":
                effects.append(store.state(request.submission_id, scope) or "absent")
            if fault in {"provider_accept_before_ack", "duplicate_provider_acceptance"}:
                if fault == "duplicate_provider_acceptance":
                    provider.jobs.append(
                        replace(provider.jobs[0], provider_job_id=ISSUER + ":job-2")
                    )
                # Restart only the consumer; provider and durable local state survive.
                recovered = SubmissionProbe(request, store, provider, 1, "1")
                effects.append(recovered.run())
                assert provider.submit_count == 1, "Replay submitted a second paid operation"
                if fault == "duplicate_provider_acceptance":
                    assert len(recovered.quarantined_ids) == 2
            if fault == "lost_heartbeat":
                # Missing contact cannot change a persisted ambiguous outcome or invent usage.
                assert store.state(request.submission_id, scope) == "outcome_unknown"
            ledger = [row[2] for row in store.ledger()]
    elif fault in {"duplicate_event", "cross_tenant"}:
        queue = ScriptedAWSQueue()
        try:
            target = generated_scope(scope.mode, 2) if fault == "cross_tenant" else scope
            created = queue.deliver(target, scope, "simulated:event", digest("generated"))
            effects.append("created" if created else "duplicate")
            duplicate = queue.deliver(target, scope, "simulated:event", digest("generated"))
            effects.append("created" if duplicate else "duplicate")
        except FixtureRefusal as exc:
            effects.append(exc.code)
    elif fault in {"out_of_order_event", "unknown_membership"}:
        identity = ScriptedWorkOS()
        if fault == "out_of_order_event":
            identity.memberships[("simulated:subject", scope)] = "removed"
            for event in ("removed", "older-active"):
                effects.append(identity.event(event, digest(event), "simulated:subject", scope))
        else:
            membership = identity.current_membership("simulated:subject", scope)
            effects.append(membership)
            if membership != "active":
                effects.append("IDENTITY_FRESHNESS_UNAVAILABLE")
    elif fault in {"unavailable_journal_head", "missing_ledger_sequence"}:
        journal = ScriptedJournal()
        if fault == "unavailable_journal_head":
            journal.available = False
        else:
            del journal.entries[1]
        dispatched: list[str] = []
        with LocalCellStore(directory / "cell.sqlite", scope, clock.now) as store:
            try:
                guarded_effect_probe(
                    store,
                    journal,
                    scope,
                    1,
                    "1",
                    clock.now(),
                    lambda: dispatched.append("dispatched"),
                )
            except FixtureRefusal as exc:
                effects.append(exc.code)
        assert not dispatched, "Missing journal evidence admitted a dependent effect"
    elif fault == "delayed_source_outcome":
        # Source timing is a scripted input; missing outcome is explicitly unknown.
        source = ScriptedSource()
        source.outcomes[scope, "simulated:operation"] = ("2026-01-01T00:00:30Z", "available")
        effects.append(source.current_outcome(scope, "simulated:operation", clock.now()))
        clock.advance(30)
        effects.append(source.current_outcome(scope, "simulated:operation", clock.now()))
    elif fault == "cleanup_failure":
        objects = ScriptedS3()
        objects.stage(scope, "simulated:object", b"generated fixture")
        objects.cleanup_fails = True
        cleaned = objects.cleanup(scope, "simulated:object")
        effects.append("cleaned" if cleaned else "quarantined")
        assert (scope, "simulated:object") in objects.quarantined
    else:
        raise ValueError("Scenario is not implemented")
    return observation(
        fixture.fixture_id, fixture.version, fixture.seed, scope.mode, kind, effects, ledger
    )
