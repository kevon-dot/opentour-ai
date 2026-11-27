"""OPE-519 restartable snapshot and change feed over generated sentinel histories.

Fixture kind: generated. All rows, paths and grants are invented (`generated-*`); volume is
synthesized at test time and nothing large is committed. Results prove software behavior only
and are not provider or customer evidence.

Implementer-chosen acceptance IDs (no authoritative requirement IDs exist for this ticket):
R1 snapshot+publish, R2 crash/atomic checkpoint, R3 replay/idempotency, R4 cursor monotonicity,
R5 change feed/tombstones, R6 rename identity, R7 deletion semantics, R8 incomplete scans,
R9 limits, R10 withdrawal, R11 disputes/drift/mismatch/retry, R12 as-of features and
outcome reconciliation, R13 privacy/status, R14 scale/bounded memory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest
from opentour_cell.pilot.bulk_extraction import (
    BulkExtraction,
    ChangeEntry,
    ChangePage,
    ExtractionError,
    PartitionPage,
    PartitionRef,
    RunLimits,
    SourceCapability,
    SourceManifest,
    SourceRow,
    family_id,
    partition_digest,
)

TENANT = "generated-tenant-a"
MAPPING = "generated-mapping-1"
FULL = SourceCapability(change_log=True, deletion_feed=True)
SNAPSHOT_ONLY = SourceCapability(change_log=False, deletion_feed=False)


def digest_of(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()


def decision(i: int, *, revision: int = 1, path: str | None = None, at: int = 100) -> SourceRow:
    return SourceRow(
        "decision",
        f"generated-decision-{i}",
        revision,
        path or f"generated/decisions/{i}.json",
        digest_of(f"decision-{i}-{revision}"),
        200,
        available_at=at,
        occurred_at=at + 5,
        decision_key=f"generated-case-{i}",
    )


def outcome(i: int, label: str = "approved", *, authority: str = "independent") -> SourceRow:
    return SourceRow(
        "outcome",
        f"generated-outcome-{i}-{authority}",
        1,
        f"generated/outcomes/{i}.json",
        digest_of(f"outcome-{i}-{label}"),
        120,
        available_at=900,
        occurred_at=900,
        decision_key=f"generated-case-{i}",
        authority=authority,
        label=label,
    )


class GeneratedSource:
    """A generated customer export: immutable partitions plus an append-only change feed."""

    def __init__(
        self,
        partitions: dict[str, tuple[str, str, list[SourceRow]]],
        *,
        capability: SourceCapability = FULL,
        revision: str = "generated-manifest-1",
        mapping: str = MAPPING,
        high_water: tuple[tuple[str, int], ...] = (("decision", 10), ("outcome", 10)),
    ) -> None:
        self.partitions = partitions  # id -> (resource, feed, rows)
        self.capability = capability
        self.revision = revision
        self.mapping = mapping
        self.high_water = high_water
        self.changes: dict[str, list[ChangeEntry]] = {"decision": [], "outcome": []}
        self.head = dict(high_water)
        self.reads: list[tuple[str, int]] = []
        self.change_reads: list[tuple[str, int]] = []
        self.before_return: Callable[[str, int], None] | None = None
        self.transient: dict[str, int] = {}
        self.overlap = 0
        self.declared_override: dict[str, str] = {}

    def manifest(self) -> SourceManifest:
        refs = tuple(
            PartitionRef(
                pid,
                resource,
                feed,
                self.declared_override.get(pid, partition_digest(rows)),
                len(rows),
            )
            for pid, (resource, feed, rows) in sorted(self.partitions.items())
        )
        return SourceManifest(self.revision, self.mapping, refs, self.high_water, self.capability)

    def read_partition(self, partition_id: str, *, start: int, limit: int) -> PartitionPage:
        if self.transient.get(partition_id, 0) > 0:
            self.transient[partition_id] -= 1
            raise ExtractionError("source_unavailable")
        self.reads.append((partition_id, start))
        _, _, rows = self.partitions[partition_id]
        chunk = tuple(rows[start : start + limit])
        if self.before_return:
            self.before_return(partition_id, start)
        return PartitionPage(
            partition_id, start, chunk, self.mapping, start + len(chunk) >= len(rows)
        )

    def read_changes(self, feed: str, *, after: int, limit: int) -> ChangePage:
        self.change_reads.append((feed, after))
        lower = max(after - self.overlap, 0)
        entries = [e for e in self.changes[feed] if e.feed_revision > lower][:limit]
        through = entries[-1].feed_revision if entries else max(self.head[feed], after)
        return ChangePage(feed, lower, through, tuple(entries))

    def push(self, feed: str, entry: ChangeEntry) -> None:
        self.changes[feed].append(entry)
        self.head[feed] = max(self.head[feed], entry.feed_revision)


class Grants:
    def __init__(self, resources: Iterable[str]) -> None:
        self.book = {r: "generated-grant-1" for r in resources}

    def current(self, resource_id: str) -> str | None:
        return self.book.get(resource_id)

    def revoke(self, resource_id: str) -> None:
        self.book.pop(resource_id, None)


def history(
    decisions: int = 12, *, per_partition: int = 4, outcomes: bool = True
) -> dict[str, tuple[str, str, list[SourceRow]]]:
    parts: dict[str, tuple[str, str, list[SourceRow]]] = {}
    for n in range(0, decisions, per_partition):
        parts[f"generated-decisions-{n // per_partition}"] = (
            "generated-resource-decisions",
            "decision",
            [decision(i) for i in range(n, min(n + per_partition, decisions))],
        )
    outs = [outcome(i) for i in range(0, decisions, 2)] if outcomes else []
    parts["generated-outcomes-0"] = ("generated-resource-outcomes", "outcome", outs)
    return parts


@pytest.fixture
def private(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    return tmp_path


def make(
    private: Path,
    source: GeneratedSource,
    grants: Grants | None = None,
    *,
    name: str = "register.sqlite3",
    clock: Callable[[], float] | None = None,
    fault: Callable[[str], None] | None = None,
) -> tuple[BulkExtraction, Grants]:
    grants = grants or Grants({"generated-resource-decisions", "generated-resource-outcomes"})
    kwargs: dict[str, object] = {"tenant": TENANT, "fault": fault}
    if clock:
        kwargs["clock"] = clock
    return BulkExtraction(private / name, source, grants, **kwargs), grants  # type: ignore[arg-type]


def finished(private: Path, source: GeneratedSource, **limits: int) -> tuple[BulkExtraction, str]:
    engine, _ = make(private, source)
    run = engine.start(RunLimits(page_size=3, **limits))
    engine.step(run)
    return engine, run


# ---- R1 snapshot and publication ------------------------------------------------------


def test_complete_snapshot_publishes_only_when_paired_and_reconciled(private: Path) -> None:
    engine, run = finished(private, GeneratedSource(history()))
    status = engine.status(run)
    assert status["snapshot_state"] == "complete"
    assert [p["state"] for p in status["partitions"]] == ["complete"] * 4
    assert status["rows_seen"] == 12 + 6
    published = engine.publish(run)
    assert published.published and published.snapshot_id
    assert engine.fit_snapshot() is not None
    assert engine.fit_snapshot().run_id == run  # type: ignore[union-attr]
    assert engine.status(run)["fit_usable"] is True
    # Publishing the same content again is idempotent: same snapshot, not a second one.
    assert engine.publish(run).snapshot_id == published.snapshot_id


def test_unpaired_snapshot_is_refused(private: Path) -> None:
    parts = {k: v for k, v in history().items() if v[1] == "decision"}
    engine, run = finished(private, GeneratedSource(parts))
    result = engine.publish(run)
    assert not result.published and "pair_incomplete" in result.reasons
    assert engine.fit_snapshot() is None


# ---- R2 crash safety ------------------------------------------------------------------


class Crash(BaseException):
    pass


@pytest.mark.parametrize("point", ["after_rows", "before_commit", "after_commit"])
def test_crash_mid_page_then_resume_has_zero_duplicates_and_zero_skips(
    private: Path, point: str
) -> None:
    clean_engine, clean_run = finished(
        private,
        GeneratedSource(history(20)),
    )
    expected = clean_engine.semantic_manifest(clean_run)

    calls = {"n": 0}

    def fault(name: str) -> None:
        if name == point:
            calls["n"] += 1
            if calls["n"] == 3:
                raise Crash

    source = GeneratedSource(history(20))
    engine, _ = make(private, source, name="crashy.sqlite3", fault=fault)
    run = engine.start(RunLimits(page_size=3))
    with pytest.raises(Crash):
        engine.step(run)
    if point != "after_commit":
        # Rolled back whole: the third page left neither rows nor a moved checkpoint.
        rows = engine.semantic_manifest(run)["admissions"]
        cursors = {p[0]: p[2] for p in engine.semantic_manifest(run)["partitions"]}
        assert len(rows) == sum(cursors.values()) == 4
    resumed, _ = make(private, source, name="crashy.sqlite3")
    assert resumed.start(RunLimits(page_size=3)) == run
    resumed.step(run)
    manifest = resumed.semantic_manifest(run)
    assert manifest["digest"] != "" and len(manifest["admissions"]) == 20 + 10
    assert [m for m in manifest["admissions"]] == expected["admissions"]
    assert manifest["partitions"] == expected["partitions"]
    assert resumed.publish(run).published


CRASH_SCRIPT = """
import os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from test_bulk_extraction import GeneratedSource, Grants, history, TENANT
from opentour_cell.pilot.bulk_extraction import BulkExtraction, RunLimits
n = {"v": 0}
def fault(point):
    if point == "before_commit":
        n["v"] += 1
        if n["v"] == 4:
            os._exit(17)
engine = BulkExtraction(
    Path(sys.argv[2]), GeneratedSource(history(40)),
    Grants({"generated-resource-decisions", "generated-resource-outcomes"}),
    tenant=TENANT, fault=fault,
)
engine.step(engine.start(RunLimits(page_size=3)))
"""


def test_killed_process_resumes_from_sealed_checkpoint(private: Path) -> None:
    reference, ref_run = finished(private, GeneratedSource(history(40)))
    path = private / "killed.sqlite3"
    done = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(Path(__file__).parent), str(path)],
        capture_output=True,
        env={**os.environ},
        check=False,
    )
    assert done.returncode == 17, done.stderr.decode()[-400:]
    source = GeneratedSource(history(40))
    engine, _ = make(private, source, name="killed.sqlite3")
    run = engine.start(RunLimits(page_size=3))
    assert sum(p[2] for p in engine.semantic_manifest(run)["partitions"]) == 7  # 3 pages sealed
    engine.step(run)
    # Resume re-read nothing before the sealed cursor of the first partition.
    assert source.reads[0] == ("generated-decisions-1", 3)
    assert (
        engine.semantic_manifest(run)["admissions"]
        == (reference.semantic_manifest(ref_run)["admissions"])
    )
    assert engine.status(run)["duplicate_deliveries"] == 0


# ---- R3 replay -------------------------------------------------------------------------


def test_replaying_a_page_twice_yields_identical_manifest_and_no_new_admissions(
    private: Path,
) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start(RunLimits(page_size=2))
    pid = "generated-decisions-0"
    page = source.read_partition(pid, start=0, limit=2)
    first = engine.accept_partition_page(run, page)
    assert (first.status, first.admitted) == ("applied", 2)
    before = engine.semantic_manifest(run)
    second = engine.accept_partition_page(run, page)
    assert (second.status, second.admitted, second.duplicates) == ("replayed", 0, 2)
    assert engine.semantic_manifest(run) == before
    assert engine.status(run)["duplicate_deliveries"] == 2  # telemetry, not semantics


def test_overlapping_boundary_page_applies_only_the_unseen_suffix(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start(RunLimits(page_size=4))
    pid = "generated-decisions-0"
    engine.accept_partition_page(run, source.read_partition(pid, start=0, limit=2))
    outcome = engine.accept_partition_page(run, source.read_partition(pid, start=0, limit=4))
    assert (outcome.admitted, outcome.duplicates) == (2, 2)
    assert len(engine.semantic_manifest(run)["admissions"]) == 4


def test_semantic_manifest_is_independent_of_page_size(private: Path) -> None:
    digests = set()
    for size in (1, 3, 7, 50):
        engine, _ = make(private, GeneratedSource(history(30)), name=f"p{size}.sqlite3")
        run = engine.start(RunLimits(page_size=size))
        engine.step(run)
        digests.add(engine.semantic_manifest(run)["digest"])
    assert len(digests) == 1


# ---- R4 cursor monotonicity -------------------------------------------------------------


def test_out_of_order_page_is_held_not_skipped_and_cursor_never_regresses(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start(RunLimits(page_size=2))
    pid = "generated-decisions-0"
    late = source.read_partition(pid, start=2, limit=2)
    early = source.read_partition(pid, start=0, limit=2)
    assert engine.accept_partition_page(run, late).status == "cursor_gap"
    assert engine.status(run)["partitions"][0]["cursor"] == 0
    assert engine.semantic_manifest(run)["admissions"] == []
    assert engine.accept_partition_page(run, early).status == "applied"
    assert engine.accept_partition_page(run, late).status == "applied"
    assert engine.accept_partition_page(run, early).status == "replayed"
    assert engine.status(run)["partitions"][0]["cursor"] == 4
    assert engine.status(run)["partitions"][0]["state"] == "complete"


def test_feed_cursor_is_monotonic_under_replay_and_reorder(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start()
    engine.step(run)
    for rev in (11, 12, 13):
        source.push("decision", change(rev, "generated-resource-decisions", 20 + rev, rev))
    first = source.read_changes("decision", after=10, limit=2)
    second = source.read_changes("decision", after=first.through, limit=2)
    assert engine.accept_change_page(run, second).status == "cursor_gap"
    assert engine.accept_change_page(run, first).status == "applied"
    assert engine.accept_change_page(run, second).status == "applied"
    cursor = engine.status(run)["feeds"][0]["cursor"]
    assert cursor == 13
    assert engine.accept_change_page(run, first).status == "applied"  # replay of an old page
    assert engine.status(run)["feeds"][0]["cursor"] == cursor


def change(
    feed_revision: int, resource: str, native: int, revision: int, op: str = "upsert"
) -> ChangeEntry:
    row = decision(native, revision=revision) if op != "tombstone" else None
    return ChangeEntry(
        "decision", feed_revision, op, resource, f"generated-decision-{native}", revision, row
    )


# ---- R5 change feed ---------------------------------------------------------------------


def test_changes_after_the_cut_apply_upserts_reopen_and_tombstones(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start(RunLimits(page_size=2))
    engine.step(run)
    res = "generated-resource-decisions"
    source.push("decision", change(11, res, 1, 2))  # revised
    source.push("decision", change(12, res, 2, 2, "tombstone"))
    source.push("decision", change(13, res, 2, 3, "reopen"))
    source.push("decision", change(14, res, 99, 1))  # new object
    engine.consume_changes(run, "decision")
    states = {f["family_id"]: f for f in engine.families(run)}

    def fam(n: int) -> str:
        return family_id(TENANT, res, f"generated-decision-{n}")

    assert states[fam(1)]["revision"] == 2
    assert states[fam(2)]["state"] == "live" and states[fam(2)]["revision"] == 3
    assert states[fam(99)]["state"] == "live"
    manifest = engine.semantic_manifest(run)
    assert [t[1] for t in manifest["tombstones"]] == [2]
    assert engine.status(run)["feeds"][0]["cursor"] == 14


def test_out_of_order_revisions_and_stale_tombstone_do_not_regress_state(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start()
    engine.step(run)
    res = "generated-resource-decisions"
    source.push("decision", change(11, res, 1, 3))
    source.push("decision", change(12, res, 1, 2))  # older revision arrives later
    source.push("decision", change(13, res, 1, 2, "tombstone"))  # stale: below current
    engine.consume_changes(run, "decision")
    entry = next(
        f
        for f in engine.families(run)
        if f["family_id"] == family_id(TENANT, res, "generated-decision-1")
    )
    assert (entry["revision"], entry["state"]) == (3, "live")


def test_tombstone_arriving_before_its_object_still_applies(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start()
    engine.step(run)
    res = "generated-resource-decisions"
    source.push("decision", change(11, res, 50, 2, "tombstone"))
    source.push("decision", change(12, res, 50, 1))  # revision below the tombstone
    engine.consume_changes(run, "decision")
    entry = next(
        f
        for f in engine.families(run)
        if f["family_id"] == family_id(TENANT, res, "generated-decision-50")
    )
    assert entry["state"] == "deleted"


def test_overlapping_boundary_changes_are_idempotent_and_never_readmitted(private: Path) -> None:
    source = GeneratedSource(history())
    source.overlap = 3
    engine, _ = make(private, source)
    run = engine.start()
    engine.step(run)
    res = "generated-resource-decisions"
    for rev in (11, 12, 13):
        source.push("decision", change(rev, res, 60 + rev, 1))
    engine.consume_changes(run, "decision")
    engine.consume_changes(run, "decision")
    assert len([a for a in engine.semantic_manifest(run)["admissions"]]) == 12 + 6 + 3
    assert engine.status(run)["duplicate_deliveries"] > 0


def test_change_before_the_cut_that_the_snapshot_never_held_is_rejected(private: Path) -> None:
    source = GeneratedSource(history())
    source.overlap = 100
    engine, _ = make(private, source)
    run = engine.start()
    engine.step(run)
    source.push("decision", change(5, "generated-resource-decisions", 70, 1))
    engine.consume_changes(run, "decision")
    status = engine.status(run)
    assert status["rejected_rows"] == {"before_cut_not_in_snapshot": 1}


def test_empty_poll_records_a_read_receipt_with_coverage(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start()
    engine.step(run)
    engine.consume_changes(run, "outcome")
    assert engine.status(run)["read_receipts"] == [["outcome", 10, 10, 0]]


def test_snapshot_only_source_has_no_change_feed_and_says_so(private: Path) -> None:
    source = GeneratedSource(history(), capability=SNAPSHOT_ONLY)
    engine, run = finished(private, source)
    with pytest.raises(ExtractionError) as error:
        engine.consume_changes(run, "decision")
    assert error.value.code == "snapshot_only_source"
    assert engine.status(run)["completeness"] == "snapshot_only"
    assert engine.publish(run).published


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        (SourceCapability(True, False), "unknown"),
        (SourceCapability(True, True, timestamp_only=True), "unknown"),
        (SourceCapability(True, True, True, True, True, False), "unknown"),
        (SourceCapability(True, True, True, True, True, True), "complete"),
        (SourceCapability(True, True), "complete"),
    ],
)
def test_completeness_is_unknown_without_deletion_feed_or_timestamp_safeguards(
    private: Path, capability: SourceCapability, expected: str
) -> None:
    engine, run = finished(private, GeneratedSource(history(), capability=capability))
    assert engine.status(run)["completeness"] == expected


# ---- R6 rename ------------------------------------------------------------------------


def test_rename_keeps_the_family_and_path_alone_confers_no_identity(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start()
    engine.step(run)
    res = "generated-resource-decisions"
    before = {f["family_id"] for f in engine.families(run)}
    moved = ChangeEntry(
        "decision",
        11,
        "upsert",
        res,
        "generated-decision-3",
        2,
        decision(3, revision=2, path="generated/moved/renamed.json"),
    )
    lookalike = ChangeEntry(  # a different native ID reusing an old display path
        "decision",
        12,
        "upsert",
        res,
        "generated-decision-777",
        1,
        decision(777, path="generated/decisions/3.json"),
    )
    source.push("decision", moved)
    source.push("decision", lookalike)
    engine.consume_changes(run, "decision")
    after = {f["family_id"] for f in engine.families(run)}
    assert after - before == {family_id(TENANT, res, "generated-decision-777")}
    assert len(engine.semantic_manifest(run)["renames"]) == 1
    assert family_id(TENANT, res, "generated-decision-3") in after


def test_same_native_id_in_two_tenants_never_merges() -> None:
    assert family_id("generated-tenant-a", "r", "n") != family_id("generated-tenant-b", "r", "n")


# ---- R7 deletion ------------------------------------------------------------------------


def test_absence_from_a_snapshot_never_declares_deletion(private: Path) -> None:
    source = GeneratedSource(history(), capability=SNAPSHOT_ONLY)
    engine, run = finished(private, source)
    first = {f["family_id"]: f["state"] for f in engine.families(run)}
    # A later export without some objects is a different manifest, never a deletion record.
    source.partitions["generated-decisions-0"] = (
        "generated-resource-decisions",
        "decision",
        [decision(i) for i in range(1, 4)],
    )
    source.revision = "generated-manifest-2"
    engine2, run2 = finished(private, source)
    assert engine2.deletion_semantics(run2) == "reconciliation_only"
    assert {f["state"] for f in engine2.families(run2)} == {"live"}
    assert set(first.values()) == {"live"}
    assert engine2.semantic_manifest(run2)["tombstones"] == []


def test_reconciliation_requires_authorization_and_completeness(private: Path) -> None:
    engine, run = finished(private, GeneratedSource(history()))
    res = "generated-resource-decisions"
    listing = [(res, f"generated-decision-{i}", 1) for i in range(12) if i != 5]
    denied = engine.reconcile_deletions(run, listing, authorized=False, complete=True)
    partial = engine.reconcile_deletions(run, listing, authorized=True, complete=False)
    assert (denied.applied, partial.applied) == (False, False)
    assert engine.semantic_manifest(run)["tombstones"] == []
    ok = engine.reconcile_deletions(run, listing, authorized=True, complete=True)
    assert ok.applied and ok.deleted >= 1
    states = {f["family_id"]: f["state"] for f in engine.families(run)}
    assert states[family_id(TENANT, res, "generated-decision-5")] == "deleted"
    assert states[family_id(TENANT, res, "generated-decision-6")] == "live"


# ---- R8 incomplete ----------------------------------------------------------------------


def test_partial_scan_is_incomplete_and_keeps_the_last_verified_snapshot(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    snapshot = engine.publish(run)
    assert snapshot.published
    source.revision = "generated-manifest-2"
    source.partitions["generated-decisions-0"][2].append(decision(500))
    nxt = engine.start(RunLimits(page_size=3))
    stepped = engine.step(nxt, max_pages=2)
    assert stepped.stopped == "page_budget"
    status = engine.status(nxt)
    assert status["snapshot_state"] == "incomplete" and status["fit_usable"] is False
    refused = engine.publish(nxt)
    assert not refused.published and any(r.startswith("partition_pending") for r in refused.reasons)
    assert engine.fit_snapshot().snapshot_id == snapshot.snapshot_id  # type: ignore[union-attr]


# ---- R9 limits --------------------------------------------------------------------------


def test_per_object_limit_rejects_visibly_and_replays_stably(private: Path) -> None:
    parts = history(4, per_partition=4)
    big = SourceRow("document", "generated-doc-big", 1, "generated/big.md", digest_of("big"), 5_000)
    parts["generated-decisions-0"][2].append(big)
    engine, run = finished(private, GeneratedSource(parts), max_object_bytes=1000)
    status = engine.status(run)
    assert status["rejected_rows"] == {"object_over_limit": 1}
    assert status["limits"]["max_object_bytes"] == 1000
    assert all(f["kind"] != "document" for f in engine.families(run))
    assert [p["state"] for p in status["partitions"]] == ["complete", "complete"]


def test_per_run_row_limit_is_exact_and_marks_incomplete(private: Path) -> None:
    engine, run = finished(private, GeneratedSource(history()), max_run_rows=7)
    status = engine.status(run)
    assert status["rows_seen"] == 7
    assert (status["state"], status["reason"]) == ("incomplete", "run_limit_rows")
    assert status["snapshot_state"] == "incomplete"
    assert not engine.publish(run).published


def test_per_run_byte_limit_stops_before_the_row_that_would_exceed_it(private: Path) -> None:
    engine, run = finished(private, GeneratedSource(history()), max_run_bytes=650)
    status = engine.status(run)
    assert status["input_bytes"] == 600 and status["rows_seen"] == 3
    assert status["reason"] == "run_limit_bytes"


def test_per_run_time_limit_uses_the_injected_clock(private: Path) -> None:
    ticks = iter(range(0, 1000, 10))
    engine, _ = make(private, GeneratedSource(history()), clock=lambda: float(next(ticks)))
    run = engine.start(RunLimits(page_size=2, max_run_seconds=25))
    result = engine.step(run)
    assert result.stopped == "run_limit_seconds"
    assert engine.status(run)["reason"] == "run_limit_seconds"


def test_oversized_source_page_is_refused_rather_than_buffered(private: Path) -> None:
    class Greedy(GeneratedSource):
        def read_partition(self, partition_id: str, *, start: int, limit: int) -> PartitionPage:
            rows = self.partitions[partition_id][2]
            return PartitionPage(partition_id, start, tuple(rows[start:]), self.mapping, True)

    engine, _ = make(private, Greedy(history()))
    run = engine.start(RunLimits(page_size=2))
    with pytest.raises(ExtractionError) as error:
        engine.step(run)
    assert error.value.code == "page_over_limit"


def test_invalid_limits_and_store_are_refused(private: Path) -> None:
    engine, _ = make(private, GeneratedSource(history()))
    with pytest.raises(ExtractionError):
        engine.start(RunLimits(page_size=10_000))
    open_dir = private / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    with pytest.raises(ExtractionError) as error:
        BulkExtraction(open_dir / "x.sqlite3", GeneratedSource(history()), Grants([]), tenant="t")
    assert error.value.code == "private_store_required"


# ---- R10 withdrawal -----------------------------------------------------------------------


def test_withdrawn_grant_before_a_read_means_zero_reads_and_unrelated_work_continues(
    private: Path,
) -> None:
    source = GeneratedSource(history())
    engine, grants = make(private, source)
    run = engine.start(RunLimits(page_size=3))
    grants.revoke("generated-resource-outcomes")
    engine.step(run)
    assert not [r for r in source.reads if r[0] == "generated-outcomes-0"]
    states = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert states["generated-outcomes-0"] == "withdrawn"
    assert {v for k, v in states.items() if k != "generated-outcomes-0"} == {"complete"}
    assert not engine.publish(run).published
    assert all(a[0] for a in engine.semantic_manifest(run)["admissions"])
    assert engine.reconcile_outcomes(run).verified == 0


def test_grant_withdrawn_between_staging_and_commit_discards_the_page(private: Path) -> None:
    source = GeneratedSource(history())
    engine, grants = make(private, source)
    run = engine.start(RunLimits(page_size=2))
    prior = engine.publish(engine.start(RunLimits(page_size=2))).snapshot_id
    assert prior is None  # nothing complete yet; no snapshot exists to leak
    source.before_return = lambda pid, start: (
        grants.revoke("generated-resource-decisions")
        if (pid, start) == ("generated-decisions-0", 2)
        else None
    )
    engine.step(run)
    status = engine.status(run)
    first = next(p for p in status["partitions"] if p["partition_id"] == "generated-decisions-0")
    assert (first["state"], first["cursor"]) == ("withdrawn", 2)  # page at 2 never admitted
    assert status["discarded_pages"] >= 1
    admitted = engine.semantic_manifest(run)["admissions"]
    assert len(admitted) == 2 + 0 + 6  # first page only for decisions; outcomes continued
    assert not engine.publish(run).published


def test_successor_resumes_over_only_still_authorized_rows_and_prior_snapshot_is_stable(
    private: Path,
) -> None:
    source = GeneratedSource(history())
    engine, grants = make(private, source)
    base = engine.start(RunLimits(page_size=3))
    engine.step(base)
    prior = engine.publish(base)
    prior_digest = engine.fit_snapshot().manifest_digest  # type: ignore[union-attr]
    grants.revoke("generated-resource-outcomes")
    again = engine.publish(base)  # a withdrawn partition blocks any new publication
    assert not again.published
    successor = engine.successor(base)
    engine.step(successor)
    status = engine.status(successor)
    assert status["excluded_unauthorized"] == 1
    assert [p["partition_id"] for p in status["partitions"]] == [
        f"generated-decisions-{n}" for n in range(3)
    ]
    kept = engine.semantic_manifest(successor)["admissions"]
    assert len(kept) == 12 and all(a for a in kept)
    assert engine.reconcile_outcomes(successor).unmatched_outcomes == 0
    assert engine.fit_snapshot().manifest_digest == prior_digest  # type: ignore[union-attr]
    assert engine.fit_snapshot().snapshot_id == prior.snapshot_id  # type: ignore[union-attr]


def test_withdrawn_resource_changes_are_not_admitted(private: Path) -> None:
    source = GeneratedSource(history())
    engine, grants = make(private, source)
    run = engine.start()
    engine.step(run)
    source.push("decision", change(11, "generated-resource-decisions", 88, 1))
    grants.revoke("generated-resource-decisions")
    engine.consume_changes(run, "decision")
    ids = {f["family_id"] for f in engine.families(run)}
    assert family_id(TENANT, "generated-resource-decisions", "generated-decision-88") not in ids


# ---- R11 disputes, drift, mismatch, retry --------------------------------------------------


def test_contradictory_payload_at_the_same_revision_is_disputed(private: Path) -> None:
    source = GeneratedSource(history())
    engine, _ = make(private, source)
    run = engine.start()
    engine.step(run)
    res = "generated-resource-decisions"
    conflicting = SourceRow(
        "decision",
        "generated-decision-1",
        1,
        "generated/decisions/1.json",
        digest_of("different"),
        200,
        available_at=100,
        occurred_at=105,
        decision_key="generated-case-1",
    )
    source.push(
        "decision",
        ChangeEntry("decision", 11, "upsert", res, "generated-decision-1", 1, conflicting),
    )
    source.overlap = 100
    engine.consume_changes(run, "decision")
    entry = next(
        f
        for f in engine.families(run)
        if f["family_id"] == family_id(TENANT, res, "generated-decision-1")
    )
    assert entry["state"] == "disputed"
    assert engine.reconcile_outcomes(run).decisions == 11  # the disputed decision is excluded


def test_schema_drift_pauses_only_the_affected_partition(private: Path) -> None:
    class Drifting(GeneratedSource):
        def read_partition(self, partition_id: str, *, start: int, limit: int) -> PartitionPage:
            page = super().read_partition(partition_id, start=start, limit=limit)
            if partition_id == "generated-decisions-1":
                return PartitionPage(
                    page.partition_id, page.start, page.rows, "generated-mapping-2", page.exhausted
                )
            return page

    engine, run = finished(private, Drifting(history()))
    states = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert states["generated-decisions-1"] == "paused_drift"
    assert {v for k, v in states.items() if k != "generated-decisions-1"} == {"complete"}
    assert not engine.publish(run).published


def test_digest_or_count_mismatch_blocks_publication(private: Path) -> None:
    source = GeneratedSource(history())
    source.declared_override["generated-decisions-0"] = digest_of("wrong")
    engine, run = finished(private, source)
    status = engine.status(run)
    first = status["partitions"][0]
    assert (first["state"], first["partition_id"]) == ("mismatch", "generated-decisions-0")
    result = engine.publish(run)
    assert not result.published and "partition_mismatch:generated-decisions-0" in result.reasons


def test_transient_source_failures_retry_then_hold(private: Path) -> None:
    source = GeneratedSource(history())
    source.transient["generated-decisions-0"] = 2
    engine, _ = make(private, source)
    run = engine.start(RunLimits(page_size=2, max_page_retries=3))
    for _ in range(3):
        engine.step(run)
    assert engine.status(run)["snapshot_state"] == "complete"
    assert engine.status(run)["retries"] == 2
    source2 = GeneratedSource(history())
    source2.transient["generated-decisions-0"] = 99
    engine2, _ = make(private, source2, name="b.sqlite3")
    run2 = engine2.start(RunLimits(page_size=2, max_page_retries=1))
    for _ in range(4):
        engine2.step(run2)
    states = {p["partition_id"]: p["state"] for p in engine2.status(run2)["partitions"]}
    assert states["generated-decisions-0"] == "failed"
    assert not engine2.publish(run2).published


# ---- R12 as-of features and outcomes ---------------------------------------------------------


def test_feature_snapshot_cites_the_revision_known_at_decision_time(private: Path) -> None:
    res = "generated-resource-decisions"
    a1 = SourceRow(
        "document",
        "generated-doc-a1",
        1,
        "generated/invoice.pdf",
        digest_of("a1"),
        100,
        available_at=9 * 3600 + 55 * 60,
    )
    a2 = SourceRow(
        "document",
        "generated-doc-a2",
        1,
        "generated/invoice.pdf",
        digest_of("a2-final"),
        100,
        available_at=11 * 3600,
    )
    unknown = SourceRow(
        "document",
        "generated-doc-u",
        1,
        "generated/policy.md",
        digest_of("u"),
        100,
        available_at=None,
    )
    parts = {
        "generated-decisions-0": (res, "decision", [a1, a2, unknown]),
        "generated-outcomes-0": ("generated-resource-outcomes", "outcome", []),
    }
    source = GeneratedSource(parts)
    engine, run = finished(private, source)
    ten = 10 * 3600
    cited = engine.feature_citation(
        run, resource_id=res, native_id="generated-doc-a1", decision_time=ten
    )
    assert (cited.status, cited.content_digest) == ("cited", digest_of("a1"))
    later = engine.feature_citation(
        run, resource_id=res, native_id="generated-doc-a2", decision_time=ten
    )
    assert later.status == "excluded_not_yet_available"
    # Moving A1 to a new path under the same native ID and replaying keeps the citation.
    moved = SourceRow(
        "document",
        "generated-doc-a1",
        2,
        "generated/archive/invoice.pdf",
        digest_of("a1"),
        100,
        available_at=12 * 3600,
    )
    entry = ChangeEntry("decision", 11, "upsert", res, "generated-doc-a1", 2, moved)
    source.push("decision", entry)
    engine.consume_changes(run, "decision")
    again = engine.feature_citation(
        run, resource_id=res, native_id="generated-doc-a1", decision_time=ten
    )
    assert (again.revision, again.content_digest) == (1, digest_of("a1"))
    coverage = engine.feature_coverage(
        run,
        [
            (res, "generated-doc-a1", ten),
            (res, "generated-doc-a2", ten),
            (res, "generated-doc-u", ten),
            (res, "generated-doc-zz", ten),
        ],
    )
    assert coverage == {
        "cited": 1,
        "excluded_not_yet_available": 1,
        "excluded_unknown_visibility": 1,
        "unknown_object": 1,
    }


def test_outcome_reconciliation_counts_one_final_and_ignores_non_authoritative_notes(
    private: Path,
) -> None:
    res_d, res_o = "generated-resource-decisions", "generated-resource-outcomes"
    outs = [
        outcome(1, "approved"),
        SourceRow(
            "outcome",
            "generated-mirror-1",
            1,
            "generated/mirror/1.json",
            digest_of("mirror"),
            100,
            decision_key="generated-case-1",
            authority="independent",
            label="approved",
        ),  # connector mirror, same final
        outcome(2, "approved", authority="free_text_note"),
        outcome(3, "approved"),
        outcome(3, "rejected", authority="independent_b"),
        SourceRow(
            "outcome",
            "generated-orphan",
            1,
            "generated/o.json",
            digest_of("o"),
            100,
            decision_key="generated-case-404",
            authority="independent",
            label="approved",
        ),
    ]
    parts = {
        "generated-decisions-0": (
            res_d,
            "decision",
            [decision(1), decision(2), decision(3), decision(4)],
        ),
        "generated-outcomes-0": (res_o, "outcome", outs),
    }
    engine, run = finished(private, GeneratedSource(parts))
    result = engine.reconcile_outcomes(run)
    assert (result.decisions, result.verified, result.pending, result.disputed) == (4, 2, 2, 0)
    assert (result.unmatched_outcomes, result.non_authoritative_outcomes) == (1, 2)


# ---- R13 privacy ---------------------------------------------------------------------------


def test_status_discloses_counts_only_never_paths_or_native_ids(private: Path) -> None:
    engine, grants = make(private, GeneratedSource(history()))
    run = engine.start(RunLimits(page_size=3))
    grants.revoke("generated-resource-outcomes")
    engine.step(run)
    text = json.dumps(engine.status(run))
    for needle in ("generated/decisions", "generated-decision-", "generated-outcome-", "outcomes/"):
        assert needle not in text
    assert (private / "register.sqlite3").stat().st_mode & 0o077 == 0


def test_tenant_is_required() -> None:
    with pytest.raises(ExtractionError):
        BulkExtraction(Path("/nonexistent/x"), GeneratedSource(history()), Grants([]), tenant="")


# ---- R14 scale and bounded memory ------------------------------------------------------------


def test_large_generated_history_streams_in_bounded_pages_and_resumes(private: Path) -> None:
    total = 20_000
    source = GeneratedSource(history(total, per_partition=4_000, outcomes=False))
    engine, _ = make(private, source)
    run = engine.start(RunLimits(page_size=500))
    first = engine.step(run, max_pages=15)  # interrupted by a page budget, not by completion
    assert first.stopped == "page_budget" and first.peak_page_rows <= 500
    assert engine.status(run)["snapshot_state"] == "incomplete"
    rest = engine.step(run)
    assert rest.peak_page_rows <= 500
    status = engine.status(run)
    assert status["rows_seen"] == total and status["duplicate_deliveries"] == 0
    assert len(engine.semantic_manifest(run)["admissions"]) == total
    assert max(s for _, s in source.reads) % 500 == 0  # every read began on a sealed cursor
    assert len(source.reads) == len({(p, s) for p, s in source.reads})  # no page read twice
