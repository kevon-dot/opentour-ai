"""OPE-525: affected-family recompute equals a clean rebuild (generated sentinel fixtures only)."""

from __future__ import annotations

import dataclasses
import json
import time
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import incremental_recompute as ir
from opentour_cell.pilot.incremental_recompute import (
    BLOCKED,
    BUILT,
    DEVELOPMENT,
    HELD,
    PROTECTED,
    ChangeEvent,
    Family,
    FieldRef,
    LeaseLost,
    Manifest,
    PublishConflict,
    RecomputeError,
    RecomputeService,
    Registry,
    SimulatedCrash,
    SourceState,
)
from opentour_cell.pilot.operating_point import canonical, digest

TENANT, PURPOSE = "generated-tenant-a", "generated-purpose"
SRC = "simulated:source-r1"
FREIGHT, DATE, MEMO = (FieldRef(SRC, n) for n in ("freight", "date", "memo"))
SECRET = "generated-protected-answer-sentinel"


def fams(tenant: str = TENANT) -> list[Family]:
    return [
        Family("A", tenant, PURPOSE, DEVELOPMENT, (FREIGHT,)),
        Family("A-child-1", tenant, PURPOSE, DEVELOPMENT, (), parent_id="A"),
        Family("A-child-2", tenant, PURPOSE, DEVELOPMENT, (), parent_id="A"),
        Family("B", tenant, PURPOSE, DEVELOPMENT, (FREIGHT,)),
        Family("C", tenant, PURPOSE, DEVELOPMENT, (DATE,)),
        Family("D", tenant, PURPOSE, DEVELOPMENT, (), lineage_complete=False),
        Family("E", tenant, PURPOSE, DEVELOPMENT, (MEMO,)),
        Family("P", tenant, PURPOSE, PROTECTED, (FREIGHT,)),
    ]


def base_state() -> SourceState:
    return SourceState(
        values={
            FREIGHT.key(): (1, 10),
            DATE.key(): (1, "generated-date-1"),
            MEMO.key(): (1, "generated-memo-1"),
        },
        grants={(TENANT, PURPOSE): 1},
    )


def upsert(eid: str, rev: int, field: FieldRef, value: object) -> ChangeEvent:
    return ChangeEvent(eid, rev, "upsert", field.source_ref, field.field_path, value)


@pytest.fixture
def svc(tmp_path: Path) -> RecomputeService:
    service = RecomputeService(str(tmp_path / "r.db"), Registry(fams()))
    service.bootstrap(base_state())
    return service


def parity(service: RecomputeService) -> None:
    snap = service.snapshot()
    assert snap is not None
    clean = ir.full_rebuild(service.registry, service.source_state())
    assert snap.semantic == ir.semantic_digest(clean)


def test_ae_fixture_dirty_reusable_held_sets_and_parity(svc: RecomputeService) -> None:
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    report = svc.run("worker-1", now=0)
    plan = report.plan
    assert set(plan.dirty) == {"A", "B", "A-child-1", "A-child-2", "P"}  # P is protected
    assert set(plan.reusable) == {"C", "E"}
    assert plan.unknown == ("D",) and report.manifest.results["D"].status == HELD
    assert report.manifest.results["D"].reasons == ("lineage_incomplete",)
    assert plan.fit_authorized is False
    assert report.jobs_started == 5 and len(plan.dirty) < len(svc.registry.families)
    parity(svc)


def test_unrelated_field_does_not_invalidate_other_families(svc: RecomputeService) -> None:
    svc.submit([upsert("e1", 2, DATE, "generated-date-2")])
    report = svc.run("w", now=0)
    assert report.plan.dirty == ("C",)
    assert {"A", "B", "E"} <= set(report.plan.reusable)
    parity(svc)


def test_child_is_dirtied_through_ancestor(svc: RecomputeService) -> None:
    event = upsert("e1", 2, FREIGHT, 12)
    svc.submit([event])
    plan = svc.run("w", now=0).plan
    assert {"A-child-1", "A-child-2"} <= set(plan.dirty)
    assert "ancestor_dirty" in plan.reasons["A-child-1"]


def test_development_and_protected_indexes_are_queried_separately(svc: RecomputeService) -> None:
    event = upsert("e1", 2, FREIGHT, 12)
    assert "P" not in svc.registry.lookup(event, DEVELOPMENT)
    assert svc.registry.lookup(event, PROTECTED) == {"P"}


def test_manifest_never_exposes_protected_answers(tmp_path: Path) -> None:
    state = base_state()
    state = SourceState({**state.values, FREIGHT.key(): (1, SECRET)}, grants=state.grants)
    service = RecomputeService(str(tmp_path / "p.db"), Registry(fams()))
    manifest = service.bootstrap(state)
    assert SECRET not in json.dumps(manifest.to_json())
    assert manifest.to_json()["fit_authorized"] is False


def test_stale_claims_immediately_and_unverified_results_not_promoted(
    svc: RecomputeService,
) -> None:
    for claim, family in (("claim-a", "A"), ("claim-c", "C"), ("claim-child", "A-child-1")):
        svc.register_claim(claim, family)
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    assert svc.current_claims() == ["claim-c"]  # stale before any recompute
    assert svc.admit("A") is False  # published artifact superseded
    svc.run("w", now=0)
    assert svc.admit("A") is False  # rebuilt but its claim is still unverified
    assert svc.verify_claim("claim-a") is True and svc.admit("A") is True
    assert svc.admit("C") is True
    assert svc.admit("D") is False  # held lineage is never promoted


def test_duplicate_event_creates_zero_jobs_and_admissions(svc: RecomputeService) -> None:
    event = upsert("e1", 2, FREIGHT, 11)
    assert svc.submit([event]) == {"accepted": 1, "duplicates": 0}
    svc.run("w", now=0)
    assert svc.admit("C") and svc.admit("E")
    jobs, admissions, rev = svc.job_count(), svc.admission_count(), svc.revision()
    assert svc.submit([event, event]) == {"accepted": 0, "duplicates": 2}
    report = svc.run("w", now=1)
    assert report.plan.dirty == () and report.jobs_started == 0
    assert svc.job_count() == jobs and svc.admission_count() == admissions
    assert svc.revision() == rev + 1
    parity(svc)


def test_repeated_recompute_is_idempotent(svc: RecomputeService) -> None:
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    first = svc.run("w", now=0)
    jobs = svc.job_count()
    second = svc.run("w", now=1)
    assert second.plan.dirty == () and second.jobs_started == 0 and svc.job_count() == jobs
    assert second.manifest.semantic == first.manifest.semantic


def test_withdrawal_during_publish_fences_successor_and_children(svc: RecomputeService) -> None:
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    admissions = svc.admission_count()
    withdraw = ChangeEvent("w1", 3, "family_withdrawal", family_ids=("A",))
    with pytest.raises(PublishConflict) as exc:
        svc.run("w", now=0, before_publish=lambda: svc.submit([withdraw]))
    assert exc.value.code == "LATE_CHANGE" and svc.revision() == 1
    assert svc.admit("A") is False and svc.admission_count() == admissions
    report = svc.run("w", now=1)
    for family in ("A", "A-child-1", "A-child-2"):
        assert report.manifest.results[family].status == BLOCKED
        assert "family_withdrawn" in report.manifest.results[family].reasons
        assert svc.admit(family) is False
    assert svc.admission_count() == admissions
    parity(svc)


def test_late_change_after_checkpoint_forces_replan(svc: RecomputeService) -> None:
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    late = upsert("e2", 3, DATE, "generated-x")
    with pytest.raises(PublishConflict):
        svc.run("w", now=0, before_publish=lambda: svc.submit([late]))
    report = svc.run("w", now=1)
    assert set(report.plan.dirty) == {"A", "B", "A-child-1", "A-child-2", "P", "C"}
    parity(svc)


def test_crash_mid_recompute_resumes_without_duplicate_jobs(tmp_path: Path) -> None:
    path = str(tmp_path / "crash.db")
    first = RecomputeService(path, Registry(fams()), batch_size=2)
    first.bootstrap(base_state())
    first.submit([upsert("e1", 2, FREIGHT, 11)])
    before = first.job_count()
    with pytest.raises(SimulatedCrash):
        first.run("w1", now=0, crash_after=2)
    assert first.revision() == 1  # nothing published by the dead run
    first.close()
    restarted = RecomputeService(path, Registry(fams()), batch_size=2)
    report = restarted.run("w2", now=100)  # first worker's lease expired
    assert report.cache_hits == 2 and report.jobs_started == 3
    assert restarted.job_count() == before + 5
    parity(restarted)
    again = restarted.run("w2", now=101)
    assert again.jobs_started == 0 and restarted.job_count() == before + 5


def test_lost_lease_publishes_nothing(svc: RecomputeService) -> None:
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    with pytest.raises(LeaseLost):
        svc.run("w1", now=0, before_publish=lambda: svc.acquire_lease("w2", now=1000))
    assert svc.revision() == 1
    with pytest.raises(LeaseLost):
        svc.acquire_lease("w3", now=1001)  # w2 holds a live lease


def test_high_fanout_policy_change_falls_back_to_bounded_full_rebuild(tmp_path: Path) -> None:
    service = RecomputeService(str(tmp_path / "f.db"), Registry(fams()), max_fanout=3)
    service.bootstrap(base_state())
    service.submit([ChangeEvent("pol1", 2, "policy", source_ref=SRC)])
    report = service.run("w", now=0)
    assert report.plan.mode == "bounded_full_rebuild"
    assert set(report.plan.dirty) == set(service.registry.families)
    parity(service)


def test_ambiguous_split_merge_blocks_both_sides(svc: RecomputeService) -> None:
    svc.submit([ChangeEvent("m1", 2, "merge", family_ids=("A", "P"))])
    report = svc.run("w", now=0)
    for family in ("A", "P", "A-child-1"):
        assert report.manifest.results[family].status == BLOCKED
        assert "split_merge_ambiguous" in report.manifest.results[family].reasons
    assert report.manifest.results["B"].status == BUILT
    parity(svc)


def test_same_split_merge_is_not_ambiguous(svc: RecomputeService) -> None:
    svc.submit([ChangeEvent("m1", 2, "merge", family_ids=("A", "B"))])
    assert svc.run("w", now=0).manifest.results["A"].status == BUILT


def test_grant_revocation_blocks_reuse_and_is_tenant_scoped(tmp_path: Path) -> None:
    other = "generated-tenant-b"
    fam_list = fams() + [
        Family("X", other, PURPOSE, DEVELOPMENT, (FREIGHT,)),
        Family("Y", TENANT, PURPOSE, DEVELOPMENT, (FREIGHT,)),
    ]
    state = SourceState(base_state().values, grants={(TENANT, PURPOSE): 1, (other, PURPOSE): 1})
    service = RecomputeService(str(tmp_path / "g.db"), Registry(fam_list))
    boot = service.bootstrap(state)
    assert boot.results["X"].cache_key != boot.results["Y"].cache_key  # identical content
    service.submit([ChangeEvent("g1", 2, "grant", tenant=TENANT, purpose=PURPOSE, epoch=None)])
    report = service.run("w", now=0)
    assert report.manifest.results["C"].status == BLOCKED
    assert report.manifest.results["X"].status == BUILT and "X" in report.plan.reusable
    assert service.admit("C") is False and service.admit("X") is True
    parity(service)


def test_grant_epoch_change_forces_rebuild_with_new_cache_key(svc: RecomputeService) -> None:
    before = svc.snapshot()
    assert before is not None
    svc.submit([ChangeEvent("g1", 2, "grant", tenant=TENANT, purpose=PURPOSE, epoch=2)])
    report = svc.run("w", now=0)
    assert report.manifest.results["C"].cache_key != before.results["C"].cache_key
    assert "D" in report.plan.unknown
    parity(svc)


def test_coalescing_keeps_newest_upsert_and_never_regresses(svc: RecomputeService) -> None:
    svc.submit([upsert("e2", 3, FREIGHT, 30), upsert("e1", 2, FREIGHT, 20)])
    assert svc.source_state().values[FREIGHT.key()] == (3, 30)  # lower revision ignored
    svc.run("w", now=0)
    parity(svc)


def test_withdrawal_fence_survives_coalescing(svc: RecomputeService) -> None:
    svc.submit([ChangeEvent("w1", 2, "withdrawal", FREIGHT.source_ref, FREIGHT.field_path)])
    svc.submit([upsert("e1", 3, FREIGHT, 40)])
    states = [r[0] for r in svc.db.execute("select state from events order by cursor")]
    assert states == ["pending", "pending"]  # withdrawal never superseded away
    assert svc.run("w", now=0).manifest.results["A"].status == BUILT
    parity(svc)


def test_withdrawn_input_blocks_dependents_with_parity(svc: RecomputeService) -> None:
    svc.submit([ChangeEvent("w1", 2, "withdrawal", FREIGHT.source_ref, FREIGHT.field_path)])
    report = svc.run("w", now=0)
    assert report.manifest.results["A-child-2"].reasons == ("input_missing_or_withdrawn",)
    assert report.manifest.results["C"].status == BUILT
    parity(svc)


def test_under_invalidating_index_is_detected_by_parity(svc: RecomputeService) -> None:
    svc.submit([upsert("e1", 2, FREIGHT, 11)])

    def broken(event: ChangeEvent, split: str) -> set[str]:
        return {f for f in svc.registry.lookup(event, split) if f != "B"}  # drops one edge

    report = svc.run("w", now=0, lookup=broken)
    clean = ir.semantic_digest(ir.full_rebuild(svc.registry, svc.source_state()))
    assert report.manifest.semantic != clean  # the parity check has teeth


def test_over_invalidation_is_bounded_to_affected_families(svc: RecomputeService) -> None:
    svc.submit([upsert("e1", 2, MEMO, "generated-memo-2")])
    assert svc.run("w", now=0).plan.dirty == ("E",)


def test_sparse_one_percent_fixture_parity_and_set_sizes(tmp_path: Path) -> None:
    count = 1000
    families = [
        Family(f"generated-fam-{i:04d}", TENANT, PURPOSE, DEVELOPMENT, (FieldRef(SRC, f"f{i}"),))
        for i in range(count)
    ]
    values = {FieldRef(SRC, f"f{i}").key(): (1, i) for i in range(count)}
    service = RecomputeService(str(tmp_path / "s.db"), Registry(families), batch_size=50)
    service.bootstrap(SourceState(values, grants={(TENANT, PURPOSE): 1}))
    service.submit([upsert(f"e{i}", 2, FieldRef(SRC, f"f{i}"), -i) for i in range(0, count, 100)])
    start = time.perf_counter()
    report = service.run("w", now=0)
    assert time.perf_counter() - start >= 0  # timing recorded in evidence, never asserted
    assert len(report.plan.dirty) == 10 and len(report.plan.reusable) == 990
    assert report.jobs_started == 10
    parity(service)


def test_registry_change_across_restart_forces_full_rebuild_with_parity(tmp_path: Path) -> None:
    path = str(tmp_path / "reg.db")
    first = RecomputeService(path, Registry(fams()))
    first.bootstrap(base_state())
    first.close()
    revised = [
        f
        if f.family_id != "C"
        else Family("C", TENANT, PURPOSE, DEVELOPMENT, (DATE,), revision="family-r2")
        for f in fams()
        if f.family_id != "E"  # a removed family must not linger in the successor
    ] + [Family("N", TENANT, PURPOSE, DEVELOPMENT, (MEMO,))]
    restarted = RecomputeService(path, Registry(revised))
    report = restarted.run("w", now=0)
    assert report.plan.mode == "bounded_full_rebuild"
    assert "registry_changed" in report.plan.reasons["C"]
    assert "E" not in report.manifest.results and "N" in report.manifest.results
    parity(restarted)
    assert restarted.run("w", now=1).plan.dirty == ()  # fingerprint stored: incremental again


def test_randomized_event_sequences_match_a_clean_rebuild(tmp_path: Path) -> None:
    """Seeded property check over random graphs and events; catches real under-invalidation."""
    import random

    fields = [FieldRef("simulated:prop", f"f{i}") for i in range(4)]
    kinds = ("upsert", "withdrawal", "family_withdrawal", "merge", "grant", "policy")
    for seed in range(60):
        rng = random.Random(seed)
        count = rng.randint(3, 12)
        family_list: list[Family] = []
        for i in range(count):
            tenant, split = rng.choice(("a", "b")), rng.choice((DEVELOPMENT, PROTECTED))
            consumes = tuple(rng.sample(fields, rng.randint(0, 2)))
            parent = family_list[rng.randrange(i)] if i and rng.random() < 0.4 else None
            if parent is not None:  # the registry refuses cross-tenant or cross-split lineage
                tenant, split = parent.tenant, parent.split
            family_list.append(
                Family(
                    f"F{i}",
                    tenant,
                    "p",
                    split,
                    consumes,
                    parent_id=parent.family_id if parent else None,
                    lineage_complete=rng.random() > 0.15,
                )
            )
        state = SourceState(
            {f.key(): (1, rng.randint(0, 9)) for f in fields}, grants={("a", "p"): 1, ("b", "p"): 1}
        )
        service = RecomputeService(str(tmp_path / f"prop{seed}.db"), Registry(family_list), 3)
        service.bootstrap(state)
        revision = 1
        for step in range(5):
            events = []
            for _ in range(rng.randint(1, 3)):
                revision += 1
                ref = rng.choice(fields)
                events.append(
                    ChangeEvent(
                        f"e{seed}-{revision}",
                        revision,
                        rng.choice(kinds),
                        ref.source_ref,
                        ref.field_path,
                        rng.randint(0, 9),
                        tuple(rng.sample([f.family_id for f in family_list], 2)),
                        rng.choice(("a", "b")),
                        "p",
                        rng.choice((None, 1, 2)),
                    )
                )
            service.submit(events)
            report = service.run("w", now=step * 1000)
            assert report.plan.mode == "incremental"  # event-driven state is never "rewritten"
            parity(service)
        service.close()


@pytest.mark.parametrize("epoch", [None, 2])
def test_authority_moved_before_publish_publishes_nothing(
    svc: RecomputeService, epoch: int | None
) -> None:
    """A grant that moves outside the event log is caught by the publish-point recheck.

    ``submit`` always advances the cursor, so an event-driven grant change is refused as
    LATE_CHANGE first. The AUTHORITY_MOVED recheck guards the remaining path: source state
    rewritten without a new event (another writer, a restore). Simulate exactly that.
    """
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    admissions, cursor = svc.admission_count(), svc.cursor()
    moved = SourceState(svc.source_state().values, grants={(TENANT, PURPOSE): epoch})

    def rewrite_authority() -> None:
        svc._put_state(moved)
        assert svc.cursor() == cursor  # no event: the cursor check cannot see this

    with pytest.raises(PublishConflict) as exc:
        svc.run("w", now=0, before_publish=rewrite_authority)
    assert exc.value.code == "AUTHORITY_MOVED"
    assert svc.revision() == 1  # nothing published under the stale epoch
    assert svc.admit("C") is False and svc.admission_count() == admissions
    report = svc.run("w", now=1)  # a fresh plan sees the moved grant and rebuilds
    assert report.manifest.results["C"].grant_epoch == epoch
    parity(svc)


@pytest.mark.parametrize(
    "constant", ["TOOL_VERSION", "VERIFIER_VERSION", "ARTIFACT_SCHEMA_VERSION"]
)
def test_cache_key_binds_tool_verifier_and_artifact_schema_versions(
    monkeypatch: pytest.MonkeyPatch, constant: str
) -> None:
    registry, state = Registry(fams()), base_state()
    before = ir.evaluate_family(registry, "C", state)
    monkeypatch.setattr(ir, constant, getattr(ir, constant) + "-next")
    after = ir.evaluate_family(registry, "C", state)
    assert before.status == after.status == BUILT
    assert after.cache_key != before.cache_key
    assert after.digest != before.digest


@pytest.mark.parametrize(
    "constant", ["TOOL_VERSION", "VERIFIER_VERSION", "ARTIFACT_SCHEMA_VERSION"]
)
def test_version_bump_invalidates_cache_and_prior_snapshot_across_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constant: str
) -> None:
    path = str(tmp_path / "ver.db")
    first = RecomputeService(path, Registry(fams()))
    boot = first.bootstrap(base_state())
    jobs = first.job_count()
    first.close()
    monkeypatch.setattr(ir, constant, getattr(ir, constant) + "-next")
    restarted = RecomputeService(path, Registry(fams()))
    report = restarted.run("w", now=0)  # no change events: only the version moved
    built = [f for f, r in report.manifest.results.items() if r.status == BUILT]
    assert report.plan.mode == "bounded_full_rebuild"
    assert set(report.plan.dirty) == set(restarted.registry.families)
    assert report.cache_hits == 0 and report.jobs_started == len(built) > 0
    assert restarted.job_count() == jobs + len(built)
    for family in built:
        assert report.manifest.results[family].cache_key != boot.results[family].cache_key
    parity(restarted)
    assert restarted.admit("C") is True
    assert restarted.run("w", now=1).plan.dirty == ()  # versions stored: incremental again


OTHER_TENANT = "generated-tenant-b"
OTHER_FIELD = FieldRef(SRC, "generated-other-field")


@pytest.fixture
def two_scopes(tmp_path: Path) -> RecomputeService:
    """Tenant B's family X reads a field nothing in tenant A reads, so A's changes never dirty X."""
    families = fams() + [Family("X", OTHER_TENANT, PURPOSE, DEVELOPMENT, (OTHER_FIELD,))]
    state = SourceState(
        {**base_state().values, OTHER_FIELD.key(): (1, "generated-other-1")},
        grants={(TENANT, PURPOSE): 1, (OTHER_TENANT, PURPOSE): 1},
    )
    service = RecomputeService(str(tmp_path / "two.db"), Registry(families))
    service.bootstrap(state)
    return service


def rewrite_state(service: RecomputeService, state: SourceState) -> None:
    """Another writer or a restore: state changes, no event is appended, the cursor stays."""
    cursor = service.cursor()
    service._put_state(state)
    assert service.cursor() == cursor


@pytest.mark.parametrize("epoch", [None, 2])
def test_publish_rechecks_authority_of_reused_results_not_only_built_ones(
    two_scopes: RecomputeService, monkeypatch: pytest.MonkeyPatch, epoch: int | None
) -> None:
    """Only the non-dirty scope's grant moves, so only a reused result can see it."""
    svc = two_scopes
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    admissions = svc.admission_count()
    current = svc.source_state()
    moved = SourceState(current.values, grants={**current.grants, (OTHER_TENANT, PURPOSE): epoch})
    captured: list[ir.Plan] = []
    original = ir.make_plan

    def spy(*args: Any, **kwargs: Any) -> ir.Plan:
        captured.append(original(*args, **kwargs))
        return captured[-1]

    monkeypatch.setattr(ir, "make_plan", spy)
    with pytest.raises(PublishConflict) as exc:
        svc.run("w", now=0, before_publish=lambda: rewrite_state(svc, moved))
    monkeypatch.undo()
    assert "X" in captured[0].reusable and "X" not in captured[0].dirty  # X is reuse-only
    assert {"A", "B", "P"} <= set(captured[0].dirty)  # tenant A's grant did not move
    assert exc.value.code == "AUTHORITY_MOVED"
    assert svc.revision() == 1 and svc.admit("X") is False
    assert svc.admission_count() == admissions
    report = svc.run("w", now=1)
    assert report.manifest.results["X"].grant_epoch == epoch
    parity(svc)


def test_missing_registry_fingerprint_row_forces_full_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No stored fingerprint means nothing proves prior results match this registry/version."""
    path = str(tmp_path / "nofp.db")
    first = RecomputeService(path, Registry(fams()))
    boot = first.bootstrap(base_state())
    first.db.execute("delete from meta where k='registry'")
    first.close()
    monkeypatch.setattr(ir, "TOOL_VERSION", ir.TOOL_VERSION + "-next")  # what the row would catch
    restarted = RecomputeService(path, Registry(fams()))
    report = restarted.run("w", now=0)
    assert report.plan.mode == "bounded_full_rebuild"
    assert set(report.plan.dirty) == set(restarted.registry.families)
    assert all("registry_changed" in report.plan.reasons[f] for f in report.plan.dirty)
    for family, result in report.manifest.results.items():
        if result.status == BUILT:
            assert result.cache_key != boot.results[family].cache_key
    parity(restarted)
    assert restarted.run("w", now=1).plan.dirty == ()  # row written back: incremental again


def test_input_rewritten_before_publish_is_refused_and_next_run_rebuilds(
    svc: RecomputeService,
) -> None:
    """An input of a reused family changes without an event while the run is in flight."""
    boot = svc.snapshot()
    assert boot is not None
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    current = svc.source_state()
    moved = SourceState(
        {**current.values, DATE.key(): (1, "generated-date-rewritten")}, grants=current.grants
    )
    with pytest.raises(PublishConflict) as exc:
        svc.run("w", now=0, before_publish=lambda: rewrite_state(svc, moved))
    assert exc.value.code == "SOURCE_STATE_MOVED"
    assert svc.revision() == 1 and svc.admit("C") is False
    report = svc.run("w", now=1)  # no new event: only the rewrite can make C dirty
    assert report.plan.mode == "bounded_full_rebuild"
    assert "source_state_rewritten" in report.plan.reasons["C"]
    assert report.manifest.results["C"].cache_key != boot.results["C"].cache_key
    parity(svc)
    assert svc.admit("C") is True
    assert svc.run("w", now=2).plan.dirty == ()  # published state stored: incremental again


def test_state_rewritten_between_runs_forces_full_rebuild(svc: RecomputeService) -> None:
    current = svc.source_state()
    rewrite_state(
        svc,
        SourceState({**current.values, MEMO.key(): (1, "generated-memo-x")}, grants=current.grants),
    )
    svc.submit([upsert("e1", 2, DATE, "generated-date-2")])  # an unrelated, legitimate event
    report = svc.run("w", now=0)
    assert report.plan.mode == "bounded_full_rebuild"
    assert "source_state_rewritten" in report.plan.reasons["E"]
    parity(svc)


def test_missing_published_state_row_forces_full_rebuild(svc: RecomputeService) -> None:
    """A store written before the published-state row existed rebuilds once, then is incremental."""
    svc.db.execute("delete from meta where k='published_state'")
    report = svc.run("w", now=0)
    assert report.plan.mode == "bounded_full_rebuild"
    assert set(report.plan.dirty) == set(svc.registry.families)
    parity(svc)
    assert svc.run("w", now=1).plan.dirty == ()


def lineage_fams(a_revision: str = "family-r1", child_parent: str = "A") -> list[Family]:
    """A and B consume the same field, so moving a child between them leaves its inputs alone."""
    return [
        Family("A", TENANT, PURPOSE, DEVELOPMENT, (FREIGHT,), revision=a_revision),
        Family("B", TENANT, PURPOSE, DEVELOPMENT, (FREIGHT,)),
        Family("A-child", TENANT, PURPOSE, DEVELOPMENT, (), parent_id=child_parent),
        Family("A-grandchild", TENANT, PURPOSE, DEVELOPMENT, (), parent_id="A-child"),
        Family("C", TENANT, PURPOSE, DEVELOPMENT, (DATE,)),
    ]


def restart_with(tmp_path: Path, revised: list[Family]) -> tuple[Manifest, ir.RunReport, int]:
    path = str(tmp_path / "lineage.db")
    first = RecomputeService(path, Registry(lineage_fams()))
    boot = first.bootstrap(base_state())
    jobs = first.job_count()
    first.close()
    restarted = RecomputeService(path, Registry(revised))
    report = restarted.run("w", now=0)
    parity(restarted)
    assert restarted.job_count() == jobs + report.jobs_started
    assert restarted.run("w", now=1).plan.dirty == ()
    restarted.close()
    return boot, report, jobs


def test_cache_key_binds_the_ordered_ancestor_chain() -> None:
    registry, state = Registry(lineage_fams()), base_state()
    dev = [TENANT, PURPOSE, DEVELOPMENT]
    assert registry.lineage("A-grandchild") == [
        ["A-child", "family-r1", *dev],
        ["A", "family-r1", *dev],
    ]
    assert registry.lineage("A") == []
    for revised in (lineage_fams(a_revision="family-r2"), lineage_fams(child_parent="B")):
        other = Registry(revised)
        for family in ("A-child", "A-grandchild"):
            before = ir.evaluate_family(registry, family, state)
            after = ir.evaluate_family(other, family, state)
            assert other.effective_consumes(family) == registry.effective_consumes(family)
            assert before.status == after.status == BUILT
            assert after.cache_key != before.cache_key


def test_parent_revision_bump_with_unchanged_inputs_misses_the_cache(tmp_path: Path) -> None:
    boot, report, _ = restart_with(tmp_path, lineage_fams(a_revision="family-r2"))
    assert report.plan.mode == "bounded_full_rebuild"
    results = report.manifest.results
    for family in ("A", "A-child", "A-grandchild"):  # the grandchild proves the whole chain
        assert results[family].status == BUILT
        assert results[family].cache_key != boot.results[family].cache_key
    for family in ("B", "C"):  # outside A's lineage: reused as cache hits
        assert results[family].cache_key == boot.results[family].cache_key
    assert report.jobs_started == 3 and report.cache_hits == 2


def test_reparent_with_identical_inputs_misses_the_cache(tmp_path: Path) -> None:
    boot, report, _ = restart_with(tmp_path, lineage_fams(child_parent="B"))
    assert report.plan.mode == "bounded_full_rebuild"
    results = report.manifest.results
    for family in ("A-child", "A-grandchild"):
        assert results[family].status == BUILT
        assert results[family].cache_key != boot.results[family].cache_key
    assert report.jobs_started == 2 and report.cache_hits == 3


# Deliberate change-detector. If this fails, the cache-key shape or a bound version changed.
# Bump TOOL_VERSION first (stores keyed under the old shape must rebuild, not reuse), then
# update both golden values. Never update GOLDEN_KEYS alone.
GOLDEN_TOOL_VERSION = "opentour.recompute.v2"
GOLDEN_KEYS = {
    "A": "b2eb0d171ce4ea69e16f820849348816f0e237c2e17d6bf0af35c05489b043e8",
    "A-child-1": "6a3d760fd40b4f631c34b9d8970c2238774e315d3503a175e486108527e28d54",
    "P": "9a144608c0eb3efa162970718b62c8808b5f881a0d01bd559d05727fe139b9da",
}


def test_cache_key_golden_change_detector() -> None:
    assert ir.TOOL_VERSION == GOLDEN_TOOL_VERSION
    registry, state = Registry(fams()), base_state()
    keys = {f: ir.evaluate_family(registry, f, state).cache_key for f in GOLDEN_KEYS}
    assert keys == GOLDEN_KEYS


LEGACY_TOOL_VERSION = "opentour.recompute.v1"


def legacy_cache_key(registry: Registry, family_id: str, state: SourceState) -> str:
    """The pre-v2 (107a56c) key: the direct ``parent`` id only, no ancestor chain."""
    fam = registry.families[family_id]
    inputs = [
        [ref.source_ref, ref.field_path, *state.values.get(ref.key(), (0, None))]
        for ref in registry.effective_consumes(family_id)
    ]
    return digest({
        "tenant": fam.tenant, "purpose": fam.purpose,
        "epoch": state.grants.get((fam.tenant, fam.purpose)), "inputs": inputs,
        "family": fam.family_id, "family_revision": fam.revision, "parent": fam.parent_id,
        "split": fam.split, "tool": LEGACY_TOOL_VERSION, "verifier": ir.VERIFIER_VERSION,
        "artifact_schema": ir.ARTIFACT_SCHEMA_VERSION,
    })  # fmt: skip


def legacy_fingerprint(registry: Registry) -> str:
    """The pre-v2 (107a56c) stored registry fingerprint."""
    return digest({
        "versions": [LEGACY_TOOL_VERSION, ir.VERIFIER_VERSION, ir.ARTIFACT_SCHEMA_VERSION],
        "families": [
            [
                f.family_id, f.tenant, f.purpose, f.split, f.parent_id, f.lineage_complete,
                f.revision, [ref.key() for ref in f.consumes],
            ]
            for f in (registry.families[k] for k in sorted(registry.families))
        ],
    })  # fmt: skip


def write_legacy_store(path: str, families: list[Family], state: SourceState) -> Manifest:
    """A store as 107a56c left it: v1 keys in the snapshot and job cache, v1 fingerprint."""
    service = RecomputeService(path, Registry(families))
    boot = service.bootstrap(state)
    results = {}
    for family, result in boot.results.items():
        if result.status == BUILT:
            key = legacy_cache_key(service.registry, family, state)
            content = digest({"cache_key": key, "kind": "family-artifact"})
            result = dataclasses.replace(result, cache_key=key, digest=content)
        results[family] = result
    legacy = Manifest(1, None, None, results)
    db = service.db
    db.execute(
        "update snapshots set manifest=?, semantic=? where revision=1",
        (canonical(legacy.to_json()), legacy.semantic),
    )
    db.execute("delete from jobs")
    for result in results.values():
        if result.status == BUILT:
            db.execute(
                "insert into jobs values(?,?,?)",
                (result.cache_key, result.family_id, canonical(result.to_json())),
            )
    db.execute(
        "insert or replace into meta values('registry',?)", (legacy_fingerprint(service.registry),)
    )
    service.close()
    return legacy


def test_pre_v2_store_runs_one_bounded_rebuild_then_admits(tmp_path: Path) -> None:
    path = str(tmp_path / "legacy.db")
    legacy = write_legacy_store(path, fams(), base_state())
    service = RecomputeService(path, Registry(fams()))
    assert service.admit("A") is False  # v1 keys never match a v2 evaluation
    report = service.run("w", now=0)
    built = [f for f, r in report.manifest.results.items() if r.status == BUILT]
    assert report.plan.mode == "bounded_full_rebuild"
    assert all("registry_changed" in report.plan.reasons[f] for f in service.registry.families)
    assert report.cache_hits == 0 and report.jobs_started == len(built) > 0
    for family in built:
        assert report.manifest.results[family].cache_key != legacy.results[family].cache_key
    parity(service)
    assert service.admit("A") is True and service.admit("A-child-1") is True
    assert service.run("w", now=1).plan.dirty == ()  # v2 fingerprint stored: incremental again


def chain_registry(root: str, middle: str) -> Registry:
    return Registry([
        Family(root, TENANT, PURPOSE, DEVELOPMENT, (FREIGHT,)),
        Family(middle, TENANT, PURPOSE, DEVELOPMENT, (FREIGHT,), parent_id=root),
        Family("G", TENANT, PURPOSE, DEVELOPMENT, (), parent_id=middle),
    ])  # fmt: skip


def test_ancestor_order_is_bound_not_only_the_ancestor_set() -> None:
    """Swapping parent and grandparent keeps G's ancestor set and inputs; the key must move."""
    first, swapped, state = chain_registry("P", "Q"), chain_registry("Q", "P"), base_state()
    assert first.effective_consumes("G") == swapped.effective_consumes("G")
    assert sorted(first.lineage("G")) == sorted(swapped.lineage("G"))
    before = ir.evaluate_family(first, "G", state)
    after = ir.evaluate_family(swapped, "G", state)
    assert before.status == after.status == BUILT
    assert after.cache_key != before.cache_key


def test_registry_refuses_self_parenting() -> None:
    with pytest.raises(RecomputeError) as exc:
        Registry([Family("S", TENANT, PURPOSE, DEVELOPMENT, (FREIGHT,), parent_id="S")])
    assert exc.value.code == "SELF_PARENT"


@pytest.mark.parametrize("length", [2, 3, 4, 50])
def test_registry_refuses_lineage_cycles(length: int) -> None:
    names = [f"cycle-{i}" for i in range(length)]
    families = [
        Family(name, TENANT, PURPOSE, DEVELOPMENT, (FREIGHT,), parent_id=names[i - 1])
        for i, name in enumerate(names)
    ]
    with pytest.raises(RecomputeError) as exc:
        Registry(families)
    assert exc.value.code == "LINEAGE_CYCLE"


OTHER_PURPOSE = "generated-purpose-b"


def move_scope(family: Family, scope: str) -> Family:
    """The same family moved to another tenant, purpose or split."""
    if scope == "tenant":
        return dataclasses.replace(family, tenant=OTHER_TENANT)
    if scope == "purpose":
        return dataclasses.replace(family, purpose=OTHER_PURPOSE)
    return dataclasses.replace(family, split=PROTECTED)


@pytest.mark.parametrize(
    ("scope", "code"),
    [
        ("tenant", "LINEAGE_CROSS_TENANT"),
        ("purpose", "LINEAGE_CROSS_PURPOSE"),
        ("split", "LINEAGE_CROSS_SPLIT"),
    ],
)
def test_registry_refuses_cross_scope_lineage(scope: str, code: str) -> None:
    """A protected (or other-tenant/purpose) parent must not feed a development child."""
    parent = move_scope(Family("A", TENANT, PURPOSE, DEVELOPMENT, (FREIGHT,)), scope)
    child = Family("A-child", TENANT, PURPOSE, DEVELOPMENT, (), parent_id="A")
    with pytest.raises(RecomputeError) as exc:
        Registry([parent, child])
    assert exc.value.code == code


def test_lineage_split_move_invalidates_the_child(tmp_path: Path) -> None:
    """Moving a lineage to the protected split rebuilds the child under a new key."""
    protected = [
        dataclasses.replace(f, split=PROTECTED) if f.family_id in ("A", "A-child") else f
        for f in lineage_fams()
        if f.family_id != "A-grandchild"
    ]
    boot, report, _ = restart_with(tmp_path, protected)
    assert report.plan.mode == "bounded_full_rebuild"
    assert report.manifest.results["A-child"].split == PROTECTED
    assert report.manifest.results["A-child"].cache_key != boot.results["A-child"].cache_key


class _UnguardedRegistry(Registry):
    """Skips the scope guard, to pin that the key itself binds each ancestor's scope."""

    def _validate_lineage(self) -> None:
        pass


@pytest.mark.parametrize("scope", ["tenant", "purpose", "split"])
def test_ancestor_scope_move_alone_changes_the_child_key(scope: str) -> None:
    """Defence in depth: if the guard were bypassed, a moved ancestor still misses the cache."""
    families = lineage_fams()
    moved = [move_scope(f, scope) if f.family_id == "A" else f for f in families]
    state = SourceState(
        base_state().values,
        grants={(TENANT, PURPOSE): 1, (OTHER_TENANT, PURPOSE): 1, (TENANT, OTHER_PURPOSE): 1},
    )
    before = ir.evaluate_family(_UnguardedRegistry(families), "A-child", state)
    after = ir.evaluate_family(_UnguardedRegistry(moved), "A-child", state)
    assert before.status == after.status == BUILT
    assert after.cache_key != before.cache_key


@pytest.mark.parametrize(
    "corrupt",
    [
        "generated-not-json",
        "[]",
        '{"values": {}}',
        '{"values": {"k": 5}, "withdrawn": [], "merges": [], "grants": []}',
    ],
)
def test_corrupt_published_state_row_forces_bounded_rebuild(
    svc: RecomputeService, corrupt: str
) -> None:
    svc.db.execute("insert or replace into meta values('published_state',?)", (corrupt,))
    report = svc.run("w", now=0)
    assert report.plan.mode == "bounded_full_rebuild"
    assert all("source_state_rewritten" in report.plan.reasons[f] for f in svc.registry.families)
    parity(svc)
    assert svc.run("w", now=1).plan.dirty == ()  # publish wrote a fresh row


def published_state_row(svc: RecomputeService) -> dict[str, Any]:
    row = svc.db.execute("select v from meta where k='published_state'").fetchone()
    assert row is not None
    loaded: dict[str, Any] = json.loads(row[0])
    return loaded


def assert_recovers_from_published_state(svc: RecomputeService, corrupt: str) -> None:
    svc.db.execute("insert or replace into meta values('published_state',?)", (corrupt,))
    report = svc.run("w", now=0)  # must not raise: a corrupt row is treated as rewritten
    assert report.plan.mode == "bounded_full_rebuild"
    assert all("source_state_rewritten" in report.plan.reasons[f] for f in svc.registry.families)
    parity(svc)
    assert svc.run("w", now=1).plan.dirty == ()  # publish wrote a fresh, valid row
    parity(svc)


def test_published_state_with_wrong_container_type_recovers(svc: RecomputeService) -> None:
    raw = published_state_row(svc)
    raw["values"] = []  # list, not object: SourceState.from_json raises AttributeError
    assert_recovers_from_published_state(svc, json.dumps(raw))


def test_published_state_failing_during_replay_recovers(svc: RecomputeService) -> None:
    """A string revision only fails when a pending event on that field is replayed onto it."""
    svc.submit([upsert("e1", 2, FREIGHT, 11)])
    raw = published_state_row(svc)
    raw["values"][FREIGHT.key()] = ["generated-not-a-revision", 1]
    assert_recovers_from_published_state(svc, json.dumps(raw))


def test_deeply_nested_published_state_recovers(svc: RecomputeService) -> None:
    depth = 100_000  # beyond the JSON decoder's recursion limit: RecursionError
    assert_recovers_from_published_state(svc, "[" * depth + "]" * depth)


@pytest.mark.parametrize("where", ["value_entry", "top_level"])
def test_published_state_outside_the_exact_shape_counts_as_rewritten(
    svc: RecomputeService, where: str
) -> None:
    """Parses and replays to the live state, but is not what to_json writes: do not trust it."""
    raw = published_state_row(svc)
    if where == "value_entry":
        raw["values"][FREIGHT.key()] = [*raw["values"][FREIGHT.key()], "generated-extra"]
    else:
        raw["generated-extra-key"] = []
    assert_recovers_from_published_state(svc, json.dumps(raw))
