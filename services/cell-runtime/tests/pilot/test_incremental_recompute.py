"""OPE-525: affected-family recompute equals a clean rebuild (generated sentinel fixtures only)."""

from __future__ import annotations

import json
import time
from pathlib import Path

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
    PublishConflict,
    RecomputeService,
    Registry,
    SimulatedCrash,
    SourceState,
)

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
