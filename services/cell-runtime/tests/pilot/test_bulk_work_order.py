"""OPE-572 canonical work order inside work units, its guards, and the Parquet gate.

Fixture kind: generated. Every tenant, workflow, case and row is an invented `generated-*`
sentinel; the manifest-route cases use the sentinel containers of `test_bulk_manifest`, not
Parquet. Results prove software behavior only and are not provider or customer evidence.

Acceptance IDs (implementer-chosen; the issue names none), continuing R, P, M, H and W:
O1 each unit's rows are worked in the canonical order (source file rank, stable hash of
tenant/workflow/case, family, revision; then change rows in feed order), checked against an
independent derivation, with units scheduled round-robin by source, then bucket;
O2 that order and every unit checkpoint are identical across insertion orders: a read retry
that reorders staging, the change feed read before the snapshot, the manifest's listing
order, page sizes, and normalization interleaved with reading;
O3 ...and across restarts and runs: a process killed mid-normalization and resumed under
another hash seed, a successor that re-reads a quarantined file, a clean successor;
O4 a unit never works past the first file of its source still being read, and a row that
two deliveries carry takes the earlier-ranked file's place whichever arrived first;
O5 fail-closed guards: a source read through two feeds is refused, a withdrawal is final for
the run on the change path, and a unit whose checkpoint stops accounting for its rows
refuses publication;
O6 no raw identifier, case hash or private order key reaches any view;
O7 checkpoints of the previous format (v5) are refused and left untouched;
G1 without a Parquet decoder, or with an unsupported format, the integrated manifest route
fails closed before staging a row.

New module attributes are reached through `bx.` so that, on a tree without this change,
each test fails on its own instead of the whole file failing to import.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import bulk_extraction as bx
from opentour_cell.pilot.bulk_extraction import (
    BulkExtraction,
    ChangeEntry,
    ExtractionError,
    RunLimits,
    SourceManifest,
    SourceRow,
    family_id,
)
from opentour_cell.pilot.bulk_manifest import ManifestSource
from test_bulk_extraction import (
    TENANT,
    GeneratedSource,
    Grants,
    change,
    decision,
    digest_of,
    history,
)
from test_bulk_extraction_partitioning import reference_key
from test_bulk_manifest import RES_D, RES_O, Export, SentinelDecoder, code_of, standard
from test_bulk_work_units import replace_with_identical_bytes

WORKFLOW = "generated-workflow-1"
Unit = tuple[str, int | None]


@pytest.fixture
def private(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    (tmp_path / "export").mkdir()
    (tmp_path / "state").mkdir(mode=0o700)
    return tmp_path


class Recorder:
    """A pure normalizer that remembers the order it was shown rows in."""

    def __init__(self) -> None:
        self.calls: list[tuple[Unit, tuple[str, int]]] = []

    def __call__(self, event: bx.StagedEvent) -> str:
        self.calls.append(((event.resource_id, event.bucket), (event.family_id, event.revision)))
        return event.payload_digest

    def per_unit(self) -> dict[Unit, list[tuple[str, int]]]:
        out: dict[Unit, list[tuple[str, int]]] = defaultdict(list)
        for unit, row in self.calls:
            out[unit].append(row)
        return dict(out)


def engine_for(
    private: Path,
    source: Any,
    *,
    name: str = "register.sqlite3",
    buckets: int = 4,
    normalizer: Any = None,
    grants: Grants | None = None,
) -> BulkExtraction:
    return BulkExtraction(
        private / "state" / name,
        source,
        grants or Grants({RES_D, RES_O}),
        tenant=TENANT,
        workflow=WORKFLOW,
        work_buckets=buckets,
        normalizer=normalizer,
    )


def doc(i: int) -> SourceRow:
    return SourceRow(
        "document", f"generated-doc-{i}", 1, f"generated/docs/{i}.md", digest_of(f"doc-{i}"), 50
    )


def content() -> dict[str, tuple[str, str, list[SourceRow]]]:
    """Six decision files and an outcome file, plus a later file holding a newer revision of
    a case first seen in file 0 and two keyless documents (the source's unkeyed unit)."""
    parts = history(24)
    parts["generated-decisions-6"] = (RES_D, "decision", [decision(2, revision=3), doc(1), doc(2)])
    return parts


CHANGES = (
    change(11, RES_D, 3, 2),
    change(12, RES_D, 40, 1),
    change(13, RES_D, 5, 2),
)


def bucket_of(row: SourceRow, buckets: int = 4) -> tuple[int | None, str]:
    if not row.decision_key:
        return None, ""
    key = reference_key(TENANT, WORKFLOW, "decision", row.decision_key)
    return int(key, 16) % buckets, key


def canonical(
    parts: dict[str, tuple[str, str, list[SourceRow]]],
    changes: tuple[ChangeEntry, ...] = (),
    buckets: int = 4,
) -> dict[Unit, list[tuple[str, int]]]:
    """An independent derivation of the documented order, by plain tuple sorting."""
    snapshot: dict[Unit, list[tuple[str, str, str, int]]] = defaultdict(list)
    for pid, (resource, _, rows) in parts.items():
        for row in rows:
            bucket, key = bucket_of(row, buckets)
            fam = family_id(TENANT, resource, row.native_id)
            snapshot[(resource, bucket)].append((pid, key, fam, row.revision))
    later: dict[Unit, list[tuple[int, str, int]]] = defaultdict(list)
    for entry in changes:
        assert entry.row is not None
        bucket, _ = bucket_of(entry.row, buckets)
        fam = family_id(TENANT, entry.resource_id, entry.native_id)
        later[(entry.resource_id, bucket)].append((entry.feed_revision, fam, entry.revision))
    return {
        unit: [(f, r) for _, _, f, r in sorted(snapshot[unit])]
        + [(f, r) for _, f, r in sorted(later[unit])]
        for unit in set(snapshot) | set(later)
    }


def round_robin(units: dict[Unit, list[tuple[str, int]]], page: int) -> list[Any]:
    """Units by source, then bucket (the unkeyed unit first), one page each per round."""
    order = sorted(units, key=lambda u: (u[0], -1 if u[1] is None else u[1]))
    out: list[Any] = []
    start = 0
    while any(len(units[u]) > start for u in order):
        for unit in order:
            out.extend((unit, row) for row in units[unit][start : start + page])
        start += page
    return out


def checkpoints(engine: BulkExtraction, run: str) -> dict[Unit, dict[str, Any]]:
    return {(u["resource_id"], u["bucket"]): u for u in engine.work_units(run)}


# ---- O1 the canonical order ---------------------------------------------------------------


def test_each_unit_works_its_rows_in_the_documented_canonical_order(private: Path) -> None:
    source = GeneratedSource(content())
    recorder = Recorder()
    engine = engine_for(private, source, normalizer=recorder)
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    for entry in CHANGES:
        source.push("decision", entry)
    engine.consume_changes(run, "decision")
    engine.normalize(run)
    expected = canonical(content(), CHANGES)
    assert recorder.per_unit() == expected
    # A case first seen in file 0 and revised in file 6 is worked file by file, and change
    # rows come after every snapshot row of their unit, in feed order.
    two = family_id(TENANT, RES_D, "generated-decision-2")
    unit = next(u for u, rows in expected.items() if (two, 3) in rows)
    assert expected[unit].index((two, 1)) < expected[unit].index((two, 3))
    assert (None in {b for _, b in expected}) and len(expected) > 4
    # Units are scheduled by source, then bucket, one page each per round.
    assert recorder.calls == round_robin(expected, 3)
    for key, unit_state in checkpoints(engine, run).items():
        assert (unit_state["cursor"], unit_state["count"], unit_state["behind"]) == (
            len(expected[key]),
            len(expected[key]),
            0,
        )


def test_order_keys_compare_exactly_as_their_fields_do() -> None:
    parts = ["a", "ab", "a!", "b", "é", "a\x00", "generated-decisions-10", "generated-decisions-9"]
    keys = ["", "0" * 64, "f" * 64]
    rows = []
    for pid in parts:
        for key in keys:
            for revision in (1, 2, 9, 10, 2**62):
                fam = family_id(TENANT, RES_D, f"generated-n-{revision % 3}")
                rows.append(
                    ((pid, key, fam, revision), bx._snapshot_order_key(pid, key, fam, revision))
                )
    by_text = [fields for fields, _ in sorted(rows, key=lambda r: r[1])]
    assert by_text == sorted(fields for fields, _ in rows)
    feed = [
        ((r, f, v), bx._change_order_key(r, f, v)) for r in (1, 9, 10, 99) for f, v in [("a", 2)]
    ]
    assert [x for x, _ in sorted(feed, key=lambda r: r[1])] == sorted(x for x, _ in feed)
    # Every snapshot position sorts before every change position.
    assert max(k for _, k in rows) < min(k for _, k in feed)
    with pytest.raises(ExtractionError) as error:
        bx._change_order_key(1 << 63, "a", 1)
    assert error.value.code == "order_key_out_of_range"


# ---- O2 insertion orders --------------------------------------------------------------------


class Reversed(GeneratedSource):
    """The same export, its manifest listing partitions in reverse order."""

    def manifest(self) -> SourceManifest:
        base = super().manifest()
        return replace(base, partitions=tuple(reversed(base.partitions)))


def drive(private: Path, name: str, variant: str, page: int = 3) -> tuple[Any, ...]:
    source = (Reversed if variant == "listing" else GeneratedSource)(content())
    recorder = Recorder()
    engine = engine_for(private, source, name=name, normalizer=recorder)
    run = engine.start(RunLimits(page_size=page))

    def changes() -> None:
        for entry in CHANGES:
            source.push("decision", entry)
        engine.consume_changes(run, "decision")

    if variant == "retry":
        source.transient["generated-decisions-0"] = 1  # file 0 is read last
        engine.step(run)
        engine.step(run)
        changes()
    elif variant == "changes_first":
        changes()
        engine.step(run)
    elif variant == "interleaved":
        engine.step(run, max_pages=2)
        engine.normalize(run, max_pages=1)
        engine.step(run, max_pages=3)
        engine.normalize(run)
        changes()
        engine.normalize(run, max_pages=2)
        engine.step(run)
    else:
        engine.step(run)
        changes()
    engine.normalize(run)
    return engine, run, recorder


@pytest.mark.parametrize("variant", ["retry", "changes_first", "listing", "interleaved", "page"])
def test_insertion_order_never_changes_the_work_order_or_a_unit_checkpoint(
    private: Path, variant: str
) -> None:
    engine, run, recorder = drive(private, "control.sqlite3", "control")
    other, other_run, other_recorder = drive(
        private, "variant.sqlite3", variant, page=7 if variant == "page" else 3
    )
    assert other_recorder.per_unit() == recorder.per_unit() == canonical(content(), CHANGES)
    assert checkpoints(other, other_run) == checkpoints(engine, run)
    assert other.semantic_manifest(other_run) == engine.semantic_manifest(run)
    if variant in ("retry", "changes_first", "listing"):
        assert other_recorder.calls == recorder.calls  # the whole schedule, not only per unit


# ---- O3 restarts and runs -------------------------------------------------------------------

CRASH_SCRIPT = """
import os, sys
sys.path.insert(0, sys.argv[1])
from pathlib import Path
from test_bulk_extraction import GeneratedSource, Grants, history, TENANT
from opentour_cell.pilot.bulk_extraction import BulkExtraction, RunLimits
n = {"v": 0}
def fault(point):
    if point == "normalize_before_commit":
        n["v"] += 1
        if n["v"] == 4:
            os._exit(17)
engine = BulkExtraction(
    Path(sys.argv[2]), GeneratedSource(history(40)),
    Grants({"generated-resource-decisions", "generated-resource-outcomes"}),
    tenant=TENANT, workflow="generated-workflow-1", work_buckets=4, fault=fault,
)
run = engine.start(RunLimits(page_size=3))
engine.step(run)
engine.normalize(run)
"""


def test_normalization_killed_under_one_hash_seed_resumes_into_the_same_order(
    private: Path,
) -> None:
    control = engine_for(private, GeneratedSource(history(40)), name="ref.sqlite3")
    ref_run = control.start(RunLimits(page_size=3))
    control.step(ref_run)
    control.normalize(ref_run)
    path = private / "state" / "killed.sqlite3"
    done = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(Path(__file__).parent), str(path)],
        capture_output=True,
        env={**os.environ, "PYTHONHASHSEED": "11"},
        check=False,
    )
    assert done.returncode == 17, done.stderr.decode()[-400:]
    recorder = Recorder()
    engine = engine_for(
        private, GeneratedSource(history(40)), name="killed.sqlite3", normalizer=recorder
    )
    run = engine.start(RunLimits(page_size=3))
    committed = {key: u["cursor"] for key, u in checkpoints(engine, run).items()}
    assert sum(committed.values()) == 9  # three pages of three rows committed, the fourth lost
    engine.normalize(run)
    assert checkpoints(engine, run) == checkpoints(control, ref_run)
    expected = canonical(history(40))
    for unit, rows in recorder.per_unit().items():
        assert rows == expected[unit][committed.get(unit, 0) :]  # resumed exactly where it stood


def manifest_engine(
    private: Path, export: Export, *, name: str = "register.sqlite3", normalizer: Any = None
) -> BulkExtraction:
    source = ManifestSource(export.root, "manifest.json", decoder=SentinelDecoder())
    return BulkExtraction(
        private / "state" / name,
        source,
        Grants({RES_D, RES_O}),
        tenant=TENANT,
        normalizer=normalizer,
    )


def test_a_successor_that_rereads_a_quarantined_file_works_exactly_like_a_clean_run(
    private: Path,
) -> None:
    export = Export(private / "export")
    standard(export, decisions=12)
    recorder = Recorder()
    engine = manifest_engine(private, export, normalizer=recorder)
    run = engine.start(RunLimits(page_size=2))
    engine.step(run, max_pages=1)  # file 0 half read
    replace_with_identical_bytes(export)  # a new inode under the same bytes: file 0 fails
    engine.step(run)
    engine.normalize(run)
    parts = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert parts["generated-decisions-0"] == "failed"
    successor = engine.successor(run)
    recorder.calls.clear()
    engine.step(successor)
    engine.normalize(successor)
    clean = Recorder()
    control = manifest_engine(private, export, name="control.sqlite3", normalizer=clean)
    ctrl = control.start(RunLimits(page_size=2))
    control.step(ctrl)
    control.normalize(ctrl)
    # The re-read file's rows were staged last, after every copied row; the order is not.
    assert recorder.per_unit() == clean.per_unit()
    assert recorder.calls == clean.calls
    assert checkpoints(engine, successor) == checkpoints(control, ctrl)
    assert engine.publish(successor).published and control.publish(ctrl).published


def test_a_clean_successor_repeats_its_predecessor_unit_for_unit(private: Path) -> None:
    source = GeneratedSource(content())
    engine = engine_for(private, source)
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    engine.normalize(run)
    successor = engine.successor(run)
    engine.step(successor)
    engine.normalize(successor)
    assert checkpoints(engine, successor) == checkpoints(engine, run)


# ---- O4 the frontier and redelivery ---------------------------------------------------------


def test_a_unit_never_works_past_the_first_file_of_its_source_still_being_read(
    private: Path,
) -> None:
    source = GeneratedSource(history(24))
    source.transient["generated-decisions-0"] = 1
    engine = engine_for(private, source)
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    parts = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert parts.pop("generated-decisions-0") == "pending"
    assert set(parts.values()) == {"complete"}
    engine.normalize(run)
    state = checkpoints(engine, run)
    decisions = [u for (resource, _), u in state.items() if resource == RES_D]
    # Files 1 to 5 are sealed, but file 0 may still add rows in front of theirs.
    assert decisions and all(u["cursor"] == 0 and u["behind"] > 0 for u in decisions)
    assert all(u["cursor"] > 0 for (resource, _), u in state.items() if resource == RES_O)
    engine.step(run)
    engine.normalize(run)
    control = engine_for(private, GeneratedSource(history(24)), name="control.sqlite3")
    ctrl = control.start(RunLimits(page_size=3))
    control.step(ctrl)
    control.normalize(ctrl)
    assert checkpoints(engine, run) == checkpoints(control, ctrl)


def test_a_row_in_two_files_belongs_to_the_earlier_file_whichever_was_read_first(
    private: Path,
) -> None:
    def build() -> GeneratedSource:
        parts = history(16)
        resource, feed, rows = parts["generated-decisions-3"]
        parts["generated-decisions-3"] = (resource, feed, [*rows, decision(1)])  # same bytes
        source = GeneratedSource(parts)
        # File 3 fails its declared row digest, so every row it owns is quarantined.
        source.declared_override["generated-decisions-3"] = digest_of("generated-wrong")
        return source

    def finish(name: str, file_zero_late: bool) -> tuple[BulkExtraction, str]:
        source = build()
        if file_zero_late:
            source.transient["generated-decisions-0"] = 1  # file 3 stages decision 1 first
        engine = engine_for(private, source, name=name)
        run = engine.start(RunLimits(page_size=2))
        engine.step(run)
        engine.step(run)
        engine.normalize(run)
        return engine, run

    engine, run = finish("control.sqlite3", False)
    late, late_run = finish("late.sqlite3", True)
    assert late.semantic_manifest(late_run) == engine.semantic_manifest(run)
    assert checkpoints(late, late_run) == checkpoints(engine, run)
    units = engine.work_units(run)
    # A unit's position counts every row it has consumed, worked or excluded.
    assert any(u["excluded"] for u in units)
    assert all(u["cursor"] == u["count"] + u["excluded"] for u in units)
    for e, r in ((engine, run), (late, late_run)):
        assert e.status(r)["quarantined_rows"] == 4  # file 3's own rows, not decision 1
        cited = e.feature_citation(
            r, resource_id=RES_D, native_id="generated-decision-1", decision_time=10_000
        )
        assert (cited.status, cited.content_digest) == ("cited", decision(1).content_digest)


def test_a_row_contradicted_across_two_files_keeps_the_earlier_file_position(
    private: Path,
) -> None:
    """Both orders dispute the row and work it at file 0's position. Which payload is kept
    is the first one staged (a known residual: the row is disputed and never verified)."""

    def finish(name: str, file_zero_late: bool) -> tuple[BulkExtraction, str, Recorder]:
        parts = history(16)
        resource, feed, rows = parts["generated-decisions-3"]
        rival = replace(decision(1), content_digest=digest_of("generated-contradiction"))
        parts["generated-decisions-3"] = (resource, feed, [*rows, rival])
        source = GeneratedSource(parts)
        if file_zero_late:
            source.transient["generated-decisions-0"] = 1
        recorder = Recorder()
        engine = engine_for(private, source, name=name, normalizer=recorder)
        run = engine.start(RunLimits(page_size=2))
        engine.step(run)
        engine.step(run)
        engine.normalize(run)
        return engine, run, recorder

    engine, run, recorder = finish("control.sqlite3", False)
    late, late_run, late_recorder = finish("late.sqlite3", True)
    assert late_recorder.per_unit() == recorder.per_unit()
    one = family_id(TENANT, RES_D, "generated-decision-1")
    for e, r in ((engine, run), (late, late_run)):
        assert [d[0] for d in e.semantic_manifest(r)["disputes"]] == [one]
        assert next(f["state"] for f in e.families(r) if f["family_id"] == one) == "disputed"


def test_a_change_repeating_a_snapshot_row_takes_the_snapshot_place_whichever_came_first(
    private: Path,
) -> None:
    repeat = ChangeEntry("decision", 11, "upsert", RES_D, "generated-decision-2", 1, decision(2))

    def finish(name: str, changes_first: bool) -> tuple[BulkExtraction, str]:
        source = GeneratedSource(history(12))
        # Another file of the source fails, so rows that depend on the whole source are
        # excluded: only a row that belongs to its snapshot file can still be worked.
        source.declared_override["generated-decisions-1"] = digest_of("generated-wrong")
        engine = engine_for(private, source, name=name)
        run = engine.start(RunLimits(page_size=3))
        if changes_first:
            source.push("decision", repeat)
            engine.consume_changes(run, "decision")
        engine.step(run)
        if not changes_first:
            source.push("decision", repeat)
            engine.consume_changes(run, "decision")
        engine.normalize(run)
        return engine, run

    engine, run = finish("control.sqlite3", False)
    first, first_run = finish("first.sqlite3", True)
    assert first.semantic_manifest(first_run) == engine.semantic_manifest(run)
    assert checkpoints(first, first_run) == checkpoints(engine, run)
    assert first.status(first_run)["duplicate_deliveries"] == 1
    assert engine.status(run)["duplicate_deliveries"] == 1


# ---- O5 fail-closed guards ------------------------------------------------------------------


def test_a_source_read_through_two_feeds_is_refused_before_anything_is_written(
    private: Path,
) -> None:
    parts = history(8)
    parts["generated-mixed-0"] = (RES_D, "outcome", [decision(30)])
    engine = engine_for(private, GeneratedSource(parts))
    assert code_of(lambda: engine.start()) == "source_feed_ambiguous"
    with sqlite3.connect(private / "state" / "register.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM partitions").fetchone()[0] == 0


def test_a_withdrawal_is_final_for_the_run_and_the_successor_reads_changes_in_order(
    private: Path,
) -> None:
    def fam(n: int) -> str:
        return family_id(TENANT, RES_D, f"generated-decision-{n}")

    source = GeneratedSource(history(8))
    grants = Grants({RES_D, RES_O})
    engine = engine_for(private, source, grants=grants)
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    source.push("decision", change(11, RES_D, 88, 1))
    source.push("decision", change(12, RES_D, 89, 1))
    grants.revoke(RES_D)
    engine.consume_changes(run, "decision")
    grants.book[RES_D] = "generated-grant-1"  # the same grant revision reads back again
    source.push("decision", change(13, RES_D, 90, 1))
    engine.consume_changes(run, "decision")
    staged = {f["family_id"] for f in engine.families(run)}
    assert not {fam(88), fam(89), fam(90)} & staged  # final for the run
    successor = engine.successor(run)
    engine.step(successor)
    engine.consume_changes(successor, "decision")
    engine.normalize(successor)
    live = {f["family_id"] for f in engine.families(successor) if f["state"] == "live"}
    assert {fam(88), fam(89), fam(90)} <= live
    clean = GeneratedSource(history(8))
    control = engine_for(private, clean, name="control.sqlite3")
    ctrl = control.start(RunLimits(page_size=3))
    control.step(ctrl)
    for feed_revision, native in ((11, 88), (12, 89), (13, 90)):
        clean.push("decision", change(feed_revision, RES_D, native, 1))
    control.consume_changes(ctrl, "decision")
    control.normalize(ctrl)
    assert checkpoints(engine, successor) == checkpoints(control, ctrl)


def test_a_row_that_turns_up_behind_a_unit_cursor_refuses_publication(private: Path) -> None:
    control = engine_for(private, GeneratedSource(history(12)), name="control.sqlite3")
    ctrl = control.start(RunLimits(page_size=2))
    control.step(ctrl)
    assert control.publish(ctrl).published
    engine = engine_for(private, GeneratedSource(history(12)))
    run = engine.start(RunLimits(page_size=2))
    engine.step(run)
    engine.normalize(run, max_pages=1)  # one unit has consumed one page
    path = private / "state" / "register.sqlite3"
    with sqlite3.connect(path) as db:
        resource, bucket, after = db.execute(
            "SELECT resource_id, bucket, after_key FROM work_units WHERE run_id=? AND"
            " after_key != ''",
            (run,),
        ).fetchone()
        # Simulate the hazard the order is built to exclude: a row lands behind the cursor.
        moved = db.execute(
            "UPDATE admissions SET order_key='0' WHERE rowid=(SELECT rowid FROM admissions"
            " WHERE run_id=? AND resource_id=? AND COALESCE(work_bucket, -1)=? AND order_key>?"
            " LIMIT 1)",
            (run, resource, bucket, after),
        ).rowcount
        assert moved == 1
    engine.normalize(run)
    result = engine.publish(run)
    shown = None if bucket == -1 else bucket
    assert not result.published
    assert f"work_unit_inconsistent:{resource}:{shown}" in result.reasons


# ---- O6 nothing private reaches a view ------------------------------------------------------


def test_no_view_carries_a_raw_identifier_a_case_hash_or_an_order_key(private: Path) -> None:
    source = GeneratedSource(content())
    seen: list[dict[str, Any]] = []

    def normalizer(event: bx.StagedEvent) -> str:
        seen.append(asdict(event))
        if event.resource_id == RES_O and event.bucket == 1:
            raise ExtractionError("generated_parser_refused")  # a failed unit, for its reason
        return event.payload_digest

    engine = engine_for(private, source, normalizer=normalizer)
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    for entry in CHANGES:
        source.push("decision", entry)
    contradicted = replace(decision(4), content_digest=digest_of("generated-contradiction"))
    source.push(
        "decision",
        ChangeEntry("decision", 14, "upsert", RES_D, contradicted.native_id, 1, contradicted),
    )
    engine.consume_changes(run, "decision")
    views: list[Any] = [
        engine.status(run),
        engine.work_units(run),
        engine.work_partitions(run),
        engine.semantic_manifest(run),
        engine.families(run),
        asdict(engine.normalize(run)),
        asdict(engine.reconcile_outcomes(run)),
        asdict(engine.publish(run)),
        engine.feature_coverage(
            run, [(RES_D, f"generated-decision-{i}", 10_000) for i in range(30)]
        ),
        [
            asdict(
                engine.feature_citation(
                    run, resource_id=RES_D, native_id=f"generated-doc-{i}", decision_time=10_000
                )
            )
            for i in (1, 2)
        ],
        seen,
    ]
    for unit in engine.work_partitions(run):
        views.append(
            engine.work_partition_rows(run, unit["resource_id"], unit["bucket"], limit=500)
        )
    text = json.dumps(views, default=str)
    assert "work_unit_failed" in text and "disputed" in text  # the rich paths were exercised
    raw = [
        TENANT,
        WORKFLOW,
        "generated-case-",
        "generated-decision-",
        "generated-outcome-",
        "generated-doc-",
        "generated/",
    ]
    with sqlite3.connect(private / "state" / "register.sqlite3") as db:
        private_values = [
            v
            for row in db.execute(
                "SELECT order_key, dispute_family, dispute_decision FROM admissions"
            )
            for v in row
            if v
        ] + [k for (k,) in db.execute("SELECT key FROM dispute_keys")]
        ledger = json.dumps(db.execute("SELECT key, run_id FROM dispute_keys").fetchall())
    cases = {r.decision_key for _, _, rows in content().values() for r in rows if r.decision_key}
    hashes = [reference_key(TENANT, WORKFLOW, "decision", c) for c in cases]
    assert private_values and hashes
    for needle in raw + hashes + private_values:
        assert needle not in text, needle
    # The dispute ledger outlives runs: it keeps digests only, never an identifier.
    for needle in raw + hashes:
        assert needle not in ledger, needle


# ---- O7 the previous checkpoint format -----------------------------------------------------


def test_a_checkpoint_of_the_previous_format_is_refused_and_left_untouched(private: Path) -> None:
    path = private / "state" / "v5.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    db.execute("INSERT INTO meta VALUES ('checkpoint_format', 'opentour.ope519.checkpoint.v5')")
    db.execute("CREATE TABLE work_units (run_id TEXT, cursor INTEGER)")
    db.commit()
    db.close()
    path.chmod(0o600)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert (
        code_of(lambda: BulkExtraction(path, GeneratedSource(history()), Grants([]), tenant=TENANT))
        == "checkpoint_format_unsupported"
    )
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert bx.CHECKPOINT_FORMAT == "opentour.ope519.checkpoint.v6"


# ---- G1 the Parquet gate fails closed ------------------------------------------------------


def test_without_a_parquet_decoder_the_route_stages_nothing_and_publishes_nothing(
    private: Path,
) -> None:
    export = Export(private / "export")
    standard(export)
    source = ManifestSource(export.root, "manifest.json")  # no decoder: the dependency gate
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start(RunLimits(page_size=2))
    assert code_of(lambda: engine.step(run)) == "parquet_reader_unavailable"
    status = engine.status(run)
    assert {p["state"] for p in status["partitions"]} == {"pending"}
    assert (status["rows_seen"], status["input_bytes"]) == (0, 0)
    assert engine.semantic_manifest(run)["admissions"] == []
    result = engine.publish(run)
    assert not result.published and engine.fit_snapshot() is None
    assert "partition_pending:generated-decisions-0" in result.reasons


@pytest.mark.parametrize(
    ("change_it", "code"),
    [
        (
            lambda e: e.top.update({"format": "opentour.ope519.partition-manifest.v0"}),
            "manifest_format_unsupported",
        ),
        (
            lambda e: (e.root / "parts/generated-decisions-1.parquet").write_bytes(b"PK\x03\x04"),
            "partition_not_parquet",
        ),
    ],
    ids=["unsupported_manifest_format", "partition_not_parquet"],
)
def test_an_unsupported_format_is_refused_before_a_run_exists(
    private: Path, change_it: Any, code: str
) -> None:
    export = Export(private / "export")
    standard(export)
    change_it(export)
    if code == "partition_not_parquet":
        data = (export.root / "parts/generated-decisions-1.parquet").read_bytes()
        export.parts["generated-decisions-1"].update(
            {"sha256": hashlib.sha256(data).hexdigest(), "byte_size": len(data)}
        )
    export.write()
    source = ManifestSource(export.root, "manifest.json", decoder=SentinelDecoder())
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    assert code_of(lambda: engine.start()) == code
    with sqlite3.connect(private / "state" / "register.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0
