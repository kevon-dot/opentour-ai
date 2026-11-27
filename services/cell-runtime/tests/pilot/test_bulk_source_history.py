"""OPE-519 source history in the register, change-feed identity and descriptor-walk confinement.

Fixture kind: generated. Every export, partition, change log and register is written at test
time under a private temp root and holds invented `generated-*` rows; partition files are the
sentinel containers of `test_bulk_manifest`, not Parquet. Results prove software behavior only
and are not provider or customer evidence.

These are the independent review's probes R1-R7 of PR #206 (head 414f605), turned into
fail-closed assertions, plus a test that the read-time identity check is load-bearing.
Acceptance IDs (implementer-chosen; the issue names none), continuing M1-M14:
H1 first-seen digests live in the register, not a caller-supplied ledger (R1), H2 a lost
history is a refusal, never a fresh start (R1b), H3 the history cannot be swapped beneath a
source, H4 the change chain survives a fresh source object (R2), H5 the chain is keyed by the
declared feed identity, not the log's path (R2b), H6 a new feed identity needs an explicit
recorded successor, H7 history is scoped by root and tenant (R7), H8 no symlink is followed
and there is no check/use gap (R3), H9 hard links are refused (R6), H10 the change log keeps
its opened identity, H11 the partition identity check is load-bearing.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot.bulk_extraction import BulkExtraction, RunLimits
from opentour_cell.pilot.bulk_manifest import CHANGE_MANIFEST_FORMAT, ManifestSource
from test_bulk_extraction import TENANT, Grants, decision
from test_bulk_manifest import (
    RES_D,
    RES_O,
    Export,
    SentinelDecoder,
    bind,
    change,
    code_of,
    source_for,
    standard,
)


@pytest.fixture
def private(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    (tmp_path / "export").mkdir()
    (tmp_path / "state").mkdir(mode=0o700)
    return tmp_path


@pytest.fixture
def export(private: Path) -> Export:
    return Export(private / "export")


def engine_for(
    private: Path, export: Export, *, name: str = "register.sqlite3", tenant: str = TENANT
) -> tuple[BulkExtraction, ManifestSource]:
    source = ManifestSource(export.root, "manifest.json", decoder=SentinelDecoder())
    engine = BulkExtraction(private / "state" / name, source, Grants({RES_D, RES_O}), tenant=tenant)
    return engine, source


def rewrite_partition_zero(export: Export) -> None:
    """The customer rewrites partition 0's bytes under the same ID, fully self-consistently."""
    export.partition(
        "generated-decisions-0",
        RES_D,
        "decision",
        [decision(i, path=f"generated/decisions/evil-{i}.json") for i in range(4)],
    )
    export.revision = "generated-manifest-2"
    export.write()


# ---- H1 first-seen digests live in the register (R1) -------------------------------------


def test_a_partition_rewritten_under_its_id_is_refused_by_a_fresh_source_on_the_same_register(
    private: Path, export: Export
) -> None:
    standard(export)
    engine, _ = engine_for(private, export)
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    first = engine.publish(run)
    assert first.published
    rewrite_partition_zero(export)
    # A brand-new source object and engine: nothing the caller holds remembers the bytes,
    # but the register does, so there is no ledger left for the caller to make fresh.
    again, fresh = engine_for(private, export)
    assert code_of(lambda: again.start(RunLimits(page_size=3))) == "partition_mutated"
    assert code_of(fresh.manifest) == "partition_mutated"
    assert again.fit_snapshot().snapshot_id == first.snapshot_id  # type: ignore[union-attr]
    # The only state the route keeps is the register itself: no side ledger file to delete.
    assert sorted(p.name for p in (private / "state").iterdir()) == ["register.sqlite3"]


# ---- H2 a lost history is a refusal (R1b) ------------------------------------------------


@pytest.mark.parametrize(
    "erase",
    [
        "DELETE FROM source_partitions",
        "DELETE FROM source_manifests",
        "DELETE FROM source_scopes",
        "DELETE FROM source_feeds",
        "DELETE FROM source_partitions WHERE partition_id='generated-decisions-0'",
        # Erased with its counter too: the run's own partition list still names the ID.
        "DELETE FROM source_partitions WHERE partition_id='generated-decisions-0';"
        " UPDATE source_scopes SET partitions=partitions-1",
    ],
)
def test_a_register_that_has_seen_a_source_refuses_it_once_its_history_is_gone(
    private: Path, export: Export, erase: str
) -> None:
    standard(export)
    engine, _ = engine_for(private, export)
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    assert engine.publish(run).published
    rewrite_partition_zero(export)
    db = sqlite3.connect(private / "state" / "register.sqlite3")
    db.executescript(erase)
    db.commit()
    db.close()
    # Not a fresh start: the register has runs for this source, so empty history is refused.
    again, fresh = engine_for(private, export)
    assert code_of(lambda: again.start(RunLimits(page_size=3))) == "source_history_missing"
    assert code_of(fresh.manifest) == "source_history_missing"


# ---- H3 the history cannot be swapped -----------------------------------------------------


def test_a_bound_source_refuses_any_other_register_and_an_unbound_one_reads_nothing(
    private: Path, export: Export
) -> None:
    standard(export)
    source = ManifestSource(export.root, "manifest.json", decoder=SentinelDecoder())
    assert code_of(source.manifest) == "source_history_unbound"
    assert code_of(lambda: source.read_changes("decision", after=0, limit=5)) == (
        "manifest_not_loaded"
    )
    bind(private, source, "a.sqlite3")
    bind(private, source, "a.sqlite3")  # the same register again is the same binding
    assert code_of(lambda: bind(private, source, "b.sqlite3")) == "source_history_conflict"
    other_tenant = lambda: BulkExtraction(  # noqa: E731
        private / "state" / "a.sqlite3", source, Grants({RES_D}), tenant="generated-tenant-b"
    )
    assert code_of(other_tenant) == "source_history_conflict"


# ---- H4 the change chain survives a fresh source object (R2) ------------------------------


def test_a_rewritten_tombstone_is_refused_by_a_fresh_source_on_the_same_register(
    private: Path, export: Export
) -> None:
    standard(export)
    export.append(change(11, 1, 2))
    export.append(change(12, 2, 2))
    first = source_for(private, export)
    first.manifest()
    assert len(first.read_changes("decision", after=10, limit=10).entries) == 2
    log = export.root / "changes.ndjson"
    lines = log.read_text().splitlines(keepends=True)
    lines[1] = json.dumps(change(11, 1, 2, "tombstone")) + "\n"
    log.write_text("".join(lines))
    fresh = source_for(private, export)
    fresh.manifest()
    assert code_of(lambda: fresh.read_changes("decision", after=10, limit=10)) == (
        "change_manifest_rewritten"
    )


# ---- H5 keyed by feed identity, never by path (R2b) ---------------------------------------


def test_renaming_the_change_log_continues_its_chain_and_must_reproduce_history(
    private: Path, export: Export
) -> None:
    standard(export)
    export.append(change(11, 1, 2))
    source = source_for(private, export)
    source.manifest()
    source.read_changes("decision", after=10, limit=10)
    header = json.dumps({"format": CHANGE_MANIFEST_FORMAT}) + "\n"
    # Rewritten history under a new file name is still the same feed's history: refused.
    (export.root / "changes2.ndjson").write_text(
        header + json.dumps(change(11, 1, 2, "tombstone")) + "\n"
    )
    export.change = "changes2.ndjson"
    export.revision = "generated-manifest-2"
    export.write()
    renamed = source_for(private, export)
    renamed.manifest()
    assert code_of(lambda: renamed.read_changes("decision", after=10, limit=10)) == (
        "change_manifest_rewritten"
    )
    # A faithful copy under a third name continues the chain, and its new appends are read.
    faithful = (export.root / "changes.ndjson").read_text() + json.dumps(change(12, 3, 2)) + "\n"
    (export.root / "changes3.ndjson").write_text(faithful)
    export.change = "changes3.ndjson"
    export.revision = "generated-manifest-3"
    export.write()
    moved = source_for(private, export)
    moved.manifest()
    page = moved.read_changes("decision", after=10, limit=10)
    assert [(e.feed_revision, e.op) for e in page.entries] == [(11, "upsert"), (12, "upsert")]
    assert moved.history.change_feed("generated-feed-1").lines == 3
    # A truncated copy under yet another name is refused, not treated as a new log.
    (export.root / "changes4.ndjson").write_text(header)
    export.change = "changes4.ndjson"
    export.revision = "generated-manifest-4"
    export.write()
    short = source_for(private, export)
    short.manifest()
    assert code_of(lambda: short.read_changes("decision", after=10, limit=10)) == (
        "change_manifest_truncated"
    )


# ---- H6 a new identity needs an explicit, recorded successor ------------------------------


def test_a_new_feed_identity_is_refused_unless_it_explicitly_succeeds_the_current_one(
    private: Path, export: Export
) -> None:
    standard(export)
    export.append(change(11, 1, 2))
    source = source_for(private, export)
    source.manifest()
    source.read_changes("decision", after=10, limit=10)

    def declare(revision: str, feed: dict[str, str]) -> ManifestSource:
        export.feed = feed
        export.revision = revision
        export.write()
        return source_for(private, export)

    renamed = declare("generated-manifest-2", {"id": "generated-feed-2"})
    assert code_of(renamed.manifest) == "change_feed_identity_changed"
    wrong = declare("generated-manifest-3", {"id": "generated-feed-2", "succeeds": "x"})
    assert code_of(wrong.manifest) == "change_feed_identity_changed"
    successor = declare(
        "generated-manifest-4", {"id": "generated-feed-2", "succeeds": "generated-feed-1"}
    )
    successor.manifest()
    # The successor is a new log with its own chain; the old identity is retired for good.
    header = json.dumps({"format": CHANGE_MANIFEST_FORMAT}) + "\n"
    (export.root / "changes.ndjson").write_text(header + json.dumps(change(12, 3, 2)) + "\n")
    assert [e.feed_revision for e in successor.read_changes("decision", after=10, limit=9).entries]
    back = declare("generated-manifest-5", {"id": "generated-feed-1"})
    assert code_of(back.manifest) == "change_feed_identity_changed"
    # Not even as a declared "successor" of the current one: a retired identity stays retired.
    revived = declare(
        "generated-manifest-7", {"id": "generated-feed-1", "succeeds": "generated-feed-2"}
    )
    assert code_of(revived.manifest) == "change_feed_identity_changed"
    # Dropping the change feed of a source that had one is refused too.
    export.capability = {"change_log": False, "deletion_feed": False}
    export.change = None
    export.revision = "generated-manifest-6"
    export.write()
    assert code_of(source_for(private, export).manifest) == "change_feed_identity_changed"


@pytest.mark.parametrize(
    "feed",
    [None, "generated-feed-1", {}, {"id": ""}, {"id": "a", "succeeds": "a"}, {"id": "a", "x": 1}],
)
def test_a_change_log_needs_one_well_formed_declared_feed_identity(
    private: Path, export: Export, feed: Any
) -> None:
    standard(export)
    export.top["change_feed"] = feed
    export.write()
    assert code_of(source_for(private, export).manifest) == "manifest_invalid"


# ---- H7 scoped by root and by tenant (R7) -------------------------------------------------


def test_two_roots_and_two_tenants_sharing_a_register_never_collide(private: Path) -> None:
    first = Export(private / "export")
    standard(first)
    (private / "export2").mkdir()
    second = Export(private / "export2")
    second.partition(
        "generated-decisions-0",
        RES_D,
        "decision",
        [decision(i, path=f"generated/other/{i}.json") for i in range(4)],
    )
    second.partition("generated-outcomes-0", RES_O, "outcome", [])
    second.write()
    # Same partition IDs, different bytes, different roots: two histories, no false mutation.
    assert len(source_for(private, first).manifest().partitions) == 3
    assert len(source_for(private, second).manifest().partitions) == 2
    # And a second tenant on the same register and root keeps its own history.
    rewrite_partition_zero(first)
    _, other = engine_for(private, first, tenant="generated-tenant-b")
    assert len(other.manifest().partitions) == 3
    _, same = engine_for(private, first)
    assert code_of(same.manifest) == "partition_mutated"


# ---- H8 no symlink is followed, no check/use gap (R3) -------------------------------------


def _logs_in_a_directory(private: Path, export: Export) -> Path:
    standard(export)
    outside = private / "outside"
    outside.mkdir()
    (outside / "changes.ndjson").write_text(
        json.dumps({"format": CHANGE_MANIFEST_FORMAT}) + "\n" + json.dumps(change(11, 7, 9)) + "\n"
    )
    logs = export.root / "logs"
    logs.mkdir()
    (logs / "changes.ndjson").write_text(json.dumps({"format": CHANGE_MANIFEST_FORMAT}) + "\n")
    export.change = "logs/changes.ndjson"
    export.write()
    return outside


def test_a_directory_swapped_for_a_symlink_mid_walk_is_refused_not_followed(
    private: Path, export: Export, monkeypatch: pytest.MonkeyPatch
) -> None:
    outside = _logs_in_a_directory(private, export)
    source = source_for(private, export)
    source.manifest()
    real_open = os.open
    armed = {"on": True}

    def racing_open(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        # Swap the directory for a symlink to outside the root exactly between opening the
        # root and opening the component: the instant a check-then-use reader would lose.
        if armed["on"] and path == "logs" and kwargs.get("dir_fd") is not None:
            armed["on"] = False
            os.rename(export.root / "logs", export.root / "logs_real")
            os.symlink(outside, export.root / "logs")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", racing_open)
    assert code_of(lambda: source.read_changes("decision", after=10, limit=10)) == (
        "partition_path_invalid"
    )
    assert armed["on"] is False  # the race really fired


def test_any_directory_symlink_is_refused_even_one_that_resolves_inside_the_root(
    private: Path, export: Export
) -> None:
    outside = _logs_in_a_directory(private, export)
    source = source_for(private, export)
    source.manifest()
    os.rename(export.root / "logs", export.root / "logs_real")
    os.symlink(outside, export.root / "logs")
    assert code_of(lambda: source.read_changes("decision", after=10, limit=10)) == (
        "partition_path_invalid"
    )
    (export.root / "logs").unlink()
    os.symlink(export.root / "logs_real", export.root / "logs")
    assert code_of(source_for(private, export).manifest) == "partition_path_invalid"


def test_an_approved_root_replaced_by_another_directory_is_refused(
    private: Path, export: Export
) -> None:
    standard(export)
    source = source_for(private, export)
    source.manifest()
    os.rename(export.root, private / "export-old")
    os.mkdir(export.root)
    for item in (private / "export-old").iterdir():
        os.rename(item, export.root / item.name)
    # Same path, same files, but not the directory that was approved.
    assert code_of(source.manifest) == "source_root_changed"


# ---- H9 hard links are refused (R6) -------------------------------------------------------


def test_a_hard_linked_change_log_or_partition_is_refused(private: Path, export: Export) -> None:
    standard(export)
    outside = private / "outside.ndjson"
    outside.write_text(
        json.dumps({"format": CHANGE_MANIFEST_FORMAT}) + "\n" + json.dumps(change(11, 7, 9)) + "\n"
    )
    os.link(outside, export.root / "hl.ndjson")
    export.change = "hl.ndjson"
    export.write()
    assert code_of(source_for(private, export).manifest) == "source_file_linked"
    export.change = "changes.ndjson"
    os.link(export.root / "parts/generated-outcomes-0.parquet", private / "outside-copy.parquet")
    export.write()
    assert code_of(source_for(private, export).manifest) == "source_file_linked"


# ---- H10 the change log keeps its opened identity ----------------------------------------


def test_a_change_log_replaced_by_another_file_is_refused_until_the_manifest_is_reloaded(
    private: Path, export: Export
) -> None:
    standard(export)
    export.append(change(11, 1, 2))
    source = source_for(private, export)
    source.manifest()
    log = export.root / "changes.ndjson"
    (export.root / "replacement.tmp").write_bytes(log.read_bytes())
    os.replace(export.root / "replacement.tmp", log)
    assert code_of(lambda: source.read_changes("decision", after=10, limit=10)) == (
        "change_manifest_replaced"
    )
    source.manifest()  # re-verified: the same committed history, so it continues
    assert len(source.read_changes("decision", after=10, limit=10).entries) == 1


# ---- H11 the partition identity check is load-bearing --------------------------------------


def _engine_with_tamper(
    private: Path, export: Export, page_size: int
) -> tuple[BulkExtraction, str, SentinelDecoder, Path]:
    standard(export)
    decoder = SentinelDecoder()
    source = ManifestSource(export.root, "manifest.json", decoder=decoder)
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start(RunLimits(page_size=page_size))
    return engine, run, decoder, export.root / "parts/generated-decisions-0.parquet"


def test_a_partition_replaced_by_identical_bytes_mid_read_is_refused(
    private: Path, export: Export
) -> None:
    engine, run, decoder, target = _engine_with_tamper(private, export, 2)
    calls = {"n": 0}

    def replace_with_same_bytes() -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            (target.parent / "swap.tmp").write_bytes(target.read_bytes())
            os.replace(target.parent / "swap.tmp", target)  # same bytes, a different file

    decoder.before_decode = replace_with_same_bytes
    # Only the inode/size/mtime check sees this: the bytes and their digest are unchanged.
    assert code_of(lambda: engine.step(run)) == "partition_mutated"
    parts = {p["partition_id"]: p for p in engine.status(run)["partitions"]}
    assert parts["generated-decisions-0"]["reason"] == "partition_mutated"
    assert not engine.publish(run).published


def test_a_transient_mutation_restored_before_the_last_page_is_still_caught(
    private: Path, export: Export
) -> None:
    engine, run, decoder, target = _engine_with_tamper(private, export, 1)
    original = target.read_bytes()
    info = target.stat()
    good = decision(1).content_digest
    bad = ("0" if good[0] != "0" else "1") + good[1:]
    calls = {"n": 0}

    def mutate_then_restore() -> None:
        calls["n"] += 1
        if calls["n"] == 2:  # while page 2 decodes: same size, new mtime
            target.write_bytes(original.replace(good.encode(), bad.encode()))
        elif calls["n"] == 3:  # restored, mtime too, before the terminal re-hash
            target.write_bytes(original)
            os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))

    decoder.before_decode = mutate_then_restore
    assert code_of(lambda: engine.step(run)) == "partition_mutated"
    parts = {p["partition_id"]: p for p in engine.status(run)["partitions"]}
    assert (parts["generated-decisions-0"]["reason"], parts["generated-decisions-0"]["cursor"]) == (
        "partition_mutated",
        2,
    )
    assert engine.status(run)["quarantined_rows"] == 2
    cited = engine.feature_citation(
        run, resource_id=RES_D, native_id="generated-decision-1", decision_time=10_000
    )
    assert cited.status == "excluded_quarantined"
