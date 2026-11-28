"""OPE-519 work after staging keyed by (source, bucket), quarantine, and one declared case field.

Fixture kind: generated. Every tenant, workflow, case and row is an invented `generated-*`
sentinel; the manifest-route cases use the sentinel containers of `test_bulk_manifest`, not
Parquet. Results prove software behavior only and are not provider or customer evidence.

These turn the independent review's probes R4 and R5 of PR #206 (head 414f605) and its F5
decision into assertions. Acceptance IDs (implementer-chosen; the issue names none):
W1 rows staged before a mutation is detected are quarantined and other partitions continue
(R4), W2 a successor re-reads a quarantined partition from its first row, W3 each work unit's
checkpoint survives a restart and a crash independently, W4 a failure in one bucket leaves the
others advancing, W5 work units are scheduled round-robin, W6 downstream readers see only
sealed rows, W7 one declared case field puts a case's rows in one bucket (R5), W8 the declared
case field is frozen with the run.

New module attributes are reached through `bx.` so that, on a tree without this change,
each test fails on its own instead of the whole file failing to import.
"""

from __future__ import annotations

import os
from dataclasses import astuple, replace
from pathlib import Path

import pytest
from opentour_cell.pilot import bulk_extraction as bx
from opentour_cell.pilot.bulk_extraction import (
    BulkExtraction,
    ExtractionError,
    RunLimits,
    SourceRow,
    family_id,
)
from opentour_cell.pilot.bulk_manifest import ManifestSource
from test_bulk_extraction import TENANT, GeneratedSource, Grants, decision, digest_of, history
from test_bulk_manifest import RES_D, RES_O, Export, SentinelDecoder, change, code_of, standard

WORKFLOW = "generated-workflow-1"


@pytest.fixture
def private(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    (tmp_path / "export").mkdir()
    (tmp_path / "state").mkdir(mode=0o700)
    return tmp_path


def engine_for(
    private: Path,
    source: GeneratedSource,
    *,
    name: str = "register.sqlite3",
    case_field: str = "decision_key",
    buckets: int = 4,
    **kwargs: object,
) -> BulkExtraction:
    return BulkExtraction(
        private / "state" / name,
        source,
        Grants({RES_D, RES_O}),
        tenant=TENANT,
        workflow=WORKFLOW,
        work_buckets=buckets,
        case_field=case_field,
        **kwargs,  # type: ignore[arg-type]
    )


def units(engine: BulkExtraction, run: str) -> dict[tuple[str, int | None], dict[str, object]]:
    return {(u["resource_id"], u["bucket"]): u for u in engine.work_units(run)}


# ---- W1 quarantine on mutation (R4) -------------------------------------------------------


def mutate_row_zero_while_decoding(export: Export, decoder: SentinelDecoder) -> str:
    """Change row 0's content digest in place (size kept, mtime restored) before page 1."""
    target = export.root / "parts/generated-decisions-0.parquet"
    info = target.stat()
    data = bytearray(target.read_bytes())
    good = decision(0).content_digest
    bad = ("0" if good[0] != "0" else "1") + good[1:]
    calls = {"n": 0}

    def tamper() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            index = data.index(good.encode())
            data[index : index + 64] = bad.encode()
            target.write_bytes(bytes(data))
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))

    decoder.before_decode = tamper
    return bad


def manifest_engine(private: Path, export: Export) -> tuple[BulkExtraction, str, SentinelDecoder]:
    standard(export)
    decoder = SentinelDecoder()
    source = ManifestSource(export.root, "manifest.json", decoder=decoder)
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    return engine, engine.start(RunLimits(page_size=2)), decoder


def test_rows_staged_before_a_mutation_is_detected_are_quarantined_and_others_continue(
    private: Path,
) -> None:
    export = Export(private / "export")
    engine, run, decoder = manifest_engine(private, export)
    bad = mutate_row_zero_while_decoding(export, decoder)
    engine.step(run)  # returns: the mutation fails one partition, it does not stop the step
    parts = {p["partition_id"]: p for p in engine.status(run)["partitions"]}
    mutated = parts.pop("generated-decisions-0")
    assert (mutated["state"], mutated["reason"]) == ("failed", "partition_mutated")
    # Every other partition advanced to completion in the same step: nothing is wedged.
    assert {p["state"] for p in parts.values()} == {"complete"}
    assert engine.status(run)["quarantined_rows"] == 2
    cited = engine.feature_citation(
        run, resource_id=RES_D, native_id="generated-decision-0", decision_time=10_000
    )
    assert cited.status == "excluded_quarantined" and cited.content_digest != bad
    staged = engine.semantic_manifest(run)["admissions"]
    quarantined = {a[0] for a in staged if a[3] == "quarantined"}
    assert family_id(TENANT, RES_D, "generated-decision-0") in quarantined
    # Quarantined rows reach no downstream work: not normalized, not joined, not paged out.
    for unit in engine.work_units(run):
        rows = engine.work_partition_rows(run, unit["resource_id"], unit["bucket"], limit=100)
        assert not {r["family_id"] for r in rows} & quarantined
    assert engine.reconcile_outcomes(run).decisions == 4  # partition 1's decisions only
    # A later contradicting delivery of a quarantined row cannot revive it as "disputed".
    export.append(change(11, 0, 1))
    engine.consume_changes(run, "decision")
    staged = {a[0]: a[3] for a in engine.semantic_manifest(run)["admissions"]}
    assert staged[family_id(TENANT, RES_D, "generated-decision-0")] == "quarantined"
    result = engine.publish(run)
    assert not result.published
    assert "partition_failed:generated-decisions-0" in result.reasons
    # A later step neither re-reads nor seals it: a failed partition stays failed.
    reads = engine.step(run)
    assert reads.pages == 0
    state = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert state["generated-decisions-0"] == "failed"
    assert not engine.publish(run).published


# ---- W2 a successor re-reads from the first row --------------------------------------------


def test_a_successor_rereads_a_quarantined_partition_from_its_first_row(private: Path) -> None:
    export = Export(private / "export")
    engine, run, decoder = manifest_engine(private, export)
    original = (export.root / "parts/generated-decisions-0.parquet").read_bytes()
    mutate_row_zero_while_decoding(export, decoder)
    engine.step(run)
    decoder.before_decode = None
    (export.root / "parts/generated-decisions-0.parquet").write_bytes(original)  # restored
    successor = engine.successor(run)
    engine.step(successor)
    assert engine.status(successor)["quarantined_rows"] == 0
    assert {p["state"] for p in engine.status(successor)["partitions"]} == {"complete"}
    cited = engine.feature_citation(
        successor, resource_id=RES_D, native_id="generated-decision-0", decision_time=10_000
    )
    assert (cited.status, cited.content_digest) == ("cited", decision(0).content_digest)
    assert engine.publish(successor).published


def test_a_resumed_run_is_bound_to_the_file_digests_it_first_froze(private: Path) -> None:
    class Reporting(GeneratedSource):
        """A file-reading source double that reports the digest it computed per file."""

        def __init__(self) -> None:
            super().__init__(history(8))
            self.files = {pid: "a" * 64 for pid in self.partitions}

        def manifest(self) -> bx.SourceManifest:
            base = super().manifest()
            refs = tuple(
                bx.PartitionRef(*astuple(r)[:5], self.files[r.partition_id])
                for r in base.partitions
            )
            return bx.SourceManifest(
                base.revision, base.mapping_revision, refs, base.high_water, base.capability
            )

    source = Reporting()
    engine = engine_for(private, source)
    run = engine.start(RunLimits(page_size=2))
    engine.step(run, max_pages=1)
    source.files["generated-decisions-1"] = "b" * 64  # different bytes, same revision
    assert code_of(lambda: engine_for(private, source).start(RunLimits(page_size=2))) == (
        "partition_mutated"
    )


# ---- W3 each unit's checkpoint survives restart and crash independently --------------------


class Crash(Exception):
    pass


def test_each_work_unit_checkpoint_survives_a_restart_and_a_crash_independently(
    private: Path,
) -> None:
    reference = engine_for(private, GeneratedSource(history(40)), name="ref.sqlite3")
    ref_run = reference.start(RunLimits(page_size=3))
    reference.step(ref_run)
    reference.normalize(ref_run)
    expected = units(reference, ref_run)
    assert len(expected) == 8 and all(u["behind"] == 0 for u in expected.values())

    first = engine_for(private, GeneratedSource(history(40)))
    run = first.start(RunLimits(page_size=3))
    first.step(run)
    first.normalize(run, max_pages=5)
    partial = units(first, run)
    assert {u["cursor"] for u in partial.values()} != {0}
    assert any(u["behind"] for u in partial.values())
    # A restart in a new engine object sees exactly the committed per-unit checkpoints.
    calls = {"n": 0}

    def fault(point: str) -> None:
        if point == "normalize_before_commit":
            calls["n"] += 1
            if calls["n"] == 3:
                raise Crash

    restarted = engine_for(private, GeneratedSource(history(40)), fault=fault)
    assert units(restarted, run) == partial
    with pytest.raises(Crash):
        restarted.normalize(run)
    after_crash = units(restarted, run)
    moved = [k for k in partial if after_crash[k]["cursor"] != partial[k]["cursor"]]
    assert len(moved) == 2  # two units committed a page; the crashed one rolled back whole
    resumed = engine_for(private, GeneratedSource(history(40)))
    resumed.normalize(run)
    assert units(resumed, run) == expected


# ---- W4 a failure in one bucket leaves the others advancing --------------------------------


def test_a_normalizer_failure_in_one_bucket_leaves_every_other_bucket_advancing(
    private: Path,
) -> None:
    probe = engine_for(private, GeneratedSource(history(40)), name="probe.sqlite3")
    probe_run = probe.start()
    probe.step(probe_run)
    probe.normalize(probe_run)
    clean = probe.reconcile_outcomes(probe_run)
    doomed = 2

    def normalizer(event: bx.StagedEvent) -> str:
        if event.bucket == doomed and event.resource_id == RES_D:
            raise ExtractionError("generated_parser_refused")
        return event.payload_digest

    engine = engine_for(private, GeneratedSource(history(40)), normalizer=normalizer)
    run = engine.start()
    engine.step(run)
    result = engine.normalize(run)
    assert result.failed_units == 1
    state = units(engine, run)
    failed = state.pop((RES_D, doomed))
    assert (failed["state"], failed["reason"]) == ("failed", "generated_parser_refused")
    assert all(u["state"] == "pending" and u["behind"] == 0 for u in state.values())
    assert state[(RES_O, doomed)]["count"] > 0  # the same bucket of another source advanced
    joined = engine.reconcile_outcomes(run)
    held = sum(
        1
        for i in range(40)
        if bx.work_bucket(TENANT, WORKFLOW, "decision", f"generated-case-{i}", 4) == doomed
    )
    assert joined.decisions == clean.decisions == 40
    assert joined.held_decisions == held > 0
    assert joined.verified == clean.verified - sum(
        1
        for i in range(0, 40, 2)
        if bx.work_bucket(TENANT, WORKFLOW, "decision", f"generated-case-{i}", 4) == doomed
    )
    assert joined.verified + joined.pending + joined.disputed == joined.decisions
    published = engine.publish(run)
    assert not published.published
    assert published.reasons == (f"work_unit_failed:{RES_D}:{doomed}",)
    assert engine.work_partition_rows(run, RES_D, doomed, limit=100) == []


def test_a_transient_normalizer_failure_is_retried_then_fails_only_its_unit(
    private: Path,
) -> None:
    attempts = {"n": 0}

    def flaky(event: bx.StagedEvent) -> str:
        if event.bucket == 1 and event.resource_id == RES_D:
            attempts["n"] += 1
            raise ExtractionError("normalizer_unavailable")
        return event.payload_digest

    engine = engine_for(private, GeneratedSource(history(40)), normalizer=flaky)
    run = engine.start(RunLimits(max_page_retries=2))
    engine.step(run)
    for _ in range(4):
        engine.normalize(run)
    state = units(engine, run)
    assert (state[(RES_D, 1)]["state"], state[(RES_D, 1)]["reason"]) == ("failed", "retry_limit")
    assert state[(RES_D, 1)]["failures"] == 3 and attempts["n"] == 3
    assert sum(1 for u in state.values() if u["state"] == "failed") == 1


# ---- W5 round-robin scheduling ------------------------------------------------------------


def test_work_units_are_scheduled_round_robin_so_none_starves(private: Path) -> None:
    engine = engine_for(private, GeneratedSource(history(80)))
    run = engine.start(RunLimits(page_size=2))
    engine.step(run)
    total = len(engine.work_units(run)) or len(engine.work_partitions(run))
    engine.normalize(run, max_pages=total)
    # One page per unit before any unit gets a second one.
    assert {u["count"] for u in engine.work_units(run)} == {2}


# ---- W6 downstream readers see only sealed rows ------------------------------------------


def test_work_partition_rows_never_hands_out_rows_of_an_unsealed_partition(
    private: Path,
) -> None:
    source = GeneratedSource(history(12))
    engine = engine_for(private, source)
    run = engine.start(RunLimits(page_size=2))
    engine.step(run, max_pages=3)  # partition 0 complete (4 rows), partition 1 half read
    states = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert (states["generated-decisions-0"], states["generated-decisions-1"]) == (
        "complete",
        "pending",
    )
    handed = {
        r["family_id"]
        for p in engine.work_partitions(run)
        for r in engine.work_partition_rows(run, p["resource_id"], p["bucket"], limit=100)
    }
    assert handed == {family_id(TENANT, RES_D, f"generated-decision-{i}") for i in range(4)}
    engine.normalize(run)
    assert sum(u["count"] for u in engine.work_units(run)) == 4  # the half-read rows wait
    assert any(u["behind"] for u in engine.work_units(run))


# ---- W7 one declared case field (R5) -------------------------------------------------------


def _case_rows() -> dict[str, tuple[str, str, list[SourceRow]]]:
    decided = SourceRow(
        "decision",
        "generated-decision-x",
        1,
        "generated/d.json",
        digest_of("d"),
        10,
        decision_key="generated-case-9",
        case_key="generated-case-9",
    )
    resolved = SourceRow(
        "outcome",
        "generated-outcome-x",
        1,
        "generated/o.json",
        digest_of("o"),
        10,
        decision_key="generated-case-9",
        authority="independent",
        label="approved",
    )
    return {
        "generated-decisions-0": (RES_D, "decision", [decided]),
        "generated-outcomes-0": (RES_O, "outcome", [resolved]),
    }


def test_one_declared_case_field_puts_a_decision_and_its_outcome_in_one_bucket(
    private: Path,
) -> None:
    d, o = (rows[0] for _, _, rows in _case_rows().values())
    # The review's probe: the same text under two fields once hashed to two buckets.
    assert bx.case_of(d, "decision_key") == bx.case_of(o, "decision_key")
    engine = engine_for(private, GeneratedSource(_case_rows()), buckets=64)
    run = engine.start()
    engine.step(run)
    buckets = {a[4]: a[5] for a in engine.semantic_manifest(run)["admissions"]}
    assert buckets[RES_D] == buckets[RES_O] is not None
    assert engine.reconcile_outcomes(run).verified == 1
    # Declaring the case key instead: the outcome lacks it, so it is unkeyed, never hashed
    # into some other bucket by a fallback to its decision key.
    assert bx.case_of(o, "case_key") is None
    keyed = engine_for(
        private, GeneratedSource(_case_rows()), name="c.sqlite3", case_field="case_key"
    )
    other = keyed.start()
    keyed.step(other)
    stamped = {a[4]: a[5] for a in keyed.semantic_manifest(other)["admissions"]}
    assert stamped[RES_O] is None and stamped[RES_D] is not None
    assert keyed.status(other)["case_field"] == "case_key"


# ---- W8 the case field is frozen with the run ------------------------------------------------


def test_resuming_with_a_different_declared_case_field_is_refused(private: Path) -> None:
    source = GeneratedSource(history())
    first = engine_for(private, source)
    run = first.start(RunLimits(page_size=2))
    first.step(run, max_pages=2)
    for field in ("case_key",):
        changed = engine_for(private, source, case_field=field)
        for attempt in (
            lambda e=changed: e.start(RunLimits(page_size=2)),
            lambda e=changed: e.step(run),
            lambda e=changed: e.normalize(run),
            lambda e=changed: e.successor(run),
        ):
            assert code_of(attempt) == "work_partitioning_changed"
    for bad in ("label", None):
        assert code_of(lambda b=bad: engine_for(private, source, case_field=b)) == (  # type: ignore[misc]
            "case_field_invalid"
        )
    same = engine_for(private, source)
    assert same.start(RunLimits(page_size=2)) == run


# ---- W9 a dispute survives quarantine and a successor (cross-session review H1) ------------


def contradiction(feed_revision: int = 11) -> dict[str, object]:
    """A change entry that contradicts decision 0 at the same revision."""
    entry = change(feed_revision, 0, 1)
    entry["row"] = {**entry["row"], "content_digest": digest_of("generated-contradiction")}
    return entry


def replace_with_identical_bytes(export: Export) -> None:
    """What an rsync does: the same bytes under a new inode."""
    target = export.root / "parts/generated-decisions-0.parquet"
    (target.parent / "rsync.tmp").write_bytes(target.read_bytes())
    os.replace(target.parent / "rsync.tmp", target)


def control_reconciliation(private: Path, export: Export) -> tuple[object, bool]:
    """The same export and change feed, read once on a fresh register with no quarantine."""
    source = ManifestSource(export.root, "manifest.json", decoder=SentinelDecoder())
    engine = BulkExtraction(
        private / "state" / "control.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start(RunLimits(page_size=2))
    engine.step(run)
    engine.consume_changes(run, "decision")
    return engine.reconcile_outcomes(run), engine.publish(run).published


def finish_with_successor(engine: BulkExtraction, run: str) -> str:
    successor = engine.successor(run)
    engine.step(successor)
    engine.consume_changes(successor, "decision")
    return successor


def decision_zero_state(engine: BulkExtraction, run: str) -> str:
    family = family_id(TENANT, RES_D, "generated-decision-0")
    return next(f["state"] for f in engine.families(run) if f["family_id"] == family)


def test_a_dispute_recorded_before_quarantine_survives_the_successor(private: Path) -> None:
    export = Export(private / "export")
    engine, run, _ = manifest_engine(private, export)
    engine.step(run, max_pages=1)  # decision 0 is staged, its partition still being read
    export.append(contradiction())
    engine.consume_changes(run, "decision")
    assert decision_zero_state(engine, run) == "disputed"
    replace_with_identical_bytes(export)
    engine.step(run)  # partition_mutated: quarantined, but the dispute must not be erased
    assert decision_zero_state(engine, run) == "disputed"
    assert not engine.publish(run).published
    successor = finish_with_successor(engine, run)
    control, control_published = control_reconciliation(private, export)
    result = engine.reconcile_outcomes(successor)
    assert (result.decisions, result.verified, result.disputed) == (8, 3, 1)
    assert result == control
    assert decision_zero_state(engine, successor) == "disputed"
    published = engine.publish(successor)
    assert published.published == control_published
    disputes = engine.semantic_manifest(successor)["disputes"]
    assert [d[0] for d in disputes] == [family_id(TENANT, RES_D, "generated-decision-0")]


def test_a_contradiction_received_while_quarantined_is_recorded_as_a_dispute(
    private: Path,
) -> None:
    export = Export(private / "export")
    engine, run, _ = manifest_engine(private, export)
    engine.step(run, max_pages=1)
    replace_with_identical_bytes(export)
    engine.step(run)
    state = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert state["generated-decisions-0"] == "failed"
    export.append(contradiction())
    engine.consume_changes(run, "decision")  # arrives during the quarantine window
    assert decision_zero_state(engine, run) == "disputed"
    assert not engine.publish(run).published
    successor = finish_with_successor(engine, run)
    result = engine.reconcile_outcomes(successor)
    assert (result.decisions, result.verified, result.disputed) == (8, 3, 1)
    assert result == control_reconciliation(private, export)[0]
    assert decision_zero_state(engine, successor) == "disputed"


# ---- W10 un-normalized rows are counted, never dropped (review M1) --------------------------


def test_rows_not_yet_normalized_are_counted_as_pending_with_their_reason(
    private: Path,
) -> None:
    stuck = family_id(TENANT, RES_D, "generated-decision-3")

    def flaky(event: bx.StagedEvent) -> str:
        if event.family_id == stuck:
            raise ExtractionError("normalizer_unavailable")
        return event.payload_digest

    engine = engine_for(private, GeneratedSource(history(8)), normalizer=flaky)
    run = engine.start(RunLimits(max_page_retries=50))
    engine.step(run)
    result = engine.reconcile_outcomes(run)
    assert result.decisions == 8  # not 7: the stuck decision is still an input decision
    assert result.verified + result.pending + result.disputed == result.decisions
    assert result.unnormalized_decisions >= 1 and result.held_decisions == 0
    assert result.pending >= result.unnormalized_decisions


# ---- W11 an in-place edit with the mtime restored fails the file (review L1) ---------------


def test_an_edit_that_keeps_size_and_restores_mtime_fails_the_file_and_a_successor_recovers(
    private: Path,
) -> None:
    export = Export(private / "export")
    standard(export)
    decoder = SentinelDecoder()
    source = ManifestSource(export.root, "manifest.json", decoder=decoder)
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start(RunLimits(page_size=1))
    target = export.root / "parts/generated-decisions-0.parquet"
    original = target.read_bytes()
    info = target.stat()
    good = decision(1).content_digest
    bad = ("0" if good[0] != "0" else "1") + good[1:]
    calls = {"n": 0}

    def edit_then_restore() -> None:
        calls["n"] += 1
        if calls["n"] in (2, 3):  # edited for page 2, restored for page 3; mtime kept each time
            target.write_bytes(
                original.replace(good.encode(), bad.encode()) if calls["n"] == 2 else original
            )
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))

    decoder.before_decode = edit_then_restore
    engine.step(run)
    part = {p["partition_id"]: p for p in engine.status(run)["partitions"]}["generated-decisions-0"]
    assert (part["state"], part["reason"]) == ("failed", "partition_mutated")
    assert engine.status(run)["quarantined_rows"] == 2
    decoder.before_decode = None
    successor = engine.successor(run)
    engine.step(successor)
    assert {p["state"] for p in engine.status(successor)["partitions"]} == {"complete"}
    cited = engine.feature_citation(
        successor, resource_id=RES_D, native_id="generated-decision-1", decision_time=10_000
    )
    assert (cited.status, cited.content_digest) == ("cited", good)
    assert engine.publish(successor).published


def test_a_row_digest_mismatch_quarantines_its_rows_and_a_successor_rereads_it(
    private: Path,
) -> None:
    class OnceWrong(GeneratedSource):
        """Delivers one altered row on the first read only, as a flaky transport might."""

        def __init__(self) -> None:
            super().__init__(history(8))
            self.altered = False

        def read_partition(self, partition_id: str, *, start: int, limit: int) -> bx.PartitionPage:
            page = super().read_partition(partition_id, start=start, limit=limit)
            if partition_id == "generated-decisions-0" and start == 0 and not self.altered:
                self.altered = True
                rows = (replace(page.rows[0], content_digest=digest_of("altered")),)
                return bx.PartitionPage(
                    page.partition_id,
                    page.start,
                    rows + page.rows[1:],
                    page.mapping_revision,
                    page.exhausted,
                )
            return page

    engine = engine_for(private, OnceWrong())
    run = engine.start()
    engine.step(run)
    part = {p["partition_id"]: p for p in engine.status(run)["partitions"]}["generated-decisions-0"]
    assert (part["state"], part["reason"]) == ("mismatch", "digest_mismatch")
    assert engine.status(run)["quarantined_rows"] == 4
    assert (
        engine.feature_citation(
            run, resource_id=RES_D, native_id="generated-decision-0", decision_time=10_000
        ).status
        == "excluded_quarantined"
    )
    successor = engine.successor(run)
    engine.step(successor)
    assert {p["state"] for p in engine.status(successor)["partitions"]} == {"complete"}
    assert engine.publish(successor).published
