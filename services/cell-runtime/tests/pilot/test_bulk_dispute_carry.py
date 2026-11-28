"""OPE-572: disputes carry forward across manifest revisions (OPE-519 decision, 2026-11-28).

The decision on OPE-519 (cross-session re-review follow-up L1): disputes carry forward across
manifest revisions for the same tenant, source and decision key; a fresh run starts with every
prior dispute for those keys, and only the dispute-resolution path may clear one. No
resolution path exists, so nothing clears a dispute.

Fixture kind: generated. Every tenant, case and row is an invented `generated-*` sentinel;
the manifest route uses the sentinel containers of `test_bulk_manifest`, not Parquet. Results
prove software behavior only and are not provider or customer evidence.

Acceptance IDs (implementer-chosen; the issue names none):
D1 a dispute survives a new manifest revision whose files already hold the contradicting
payload and whose high-water covers the contradicting change (the L1 repro);
D2 bumping the row's revision, or re-issuing the decision under a new object ID, cannot
launder it: it is carried by the decision key;
D3 re-keying the decision cannot launder it: it is carried by the evidence family, which also
carries disputes of rows that have no decision key;
D4 it stays inside its tenant and source, and moving the export to another root does not
shed it;
D5 the outcome never depends on arrival order: rows staged before the dispute, in this run
or in a concurrent run of the tenant, are held exactly like rows staged after it;
D6 the ledger holds digests only, and no later manifest, revision, successor or quarantine
clears a dispute;
D7 a disputed object is reported as a disputed exclusion, never as an unknown object.

New module attributes are reached through `bx.` so that, on a tree without this change,
each test fails on its own instead of the whole file failing to import.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from opentour_cell.pilot import bulk_extraction as bx
from opentour_cell.pilot.bulk_extraction import (
    BulkExtraction,
    ChangeEntry,
    RunLimits,
    SourceRow,
    family_id,
)
from opentour_cell.pilot.bulk_manifest import ManifestSource
from test_bulk_extraction import TENANT, GeneratedSource, Grants, decision, digest_of, history
from test_bulk_manifest import RES_D, RES_O, Export, SentinelDecoder, change, standard

RES_D2 = "generated-resource-decisions-2"
OTHER_TENANT = "generated-tenant-b"
CONTRADICTED = replace(decision(0), content_digest=digest_of("generated-contradiction"))


@pytest.fixture
def private(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    (tmp_path / "export").mkdir()
    (tmp_path / "state").mkdir(mode=0o700)
    return tmp_path


def engine_for(
    private: Path,
    root: Path,
    *,
    name: str = "register.sqlite3",
    tenant: str = TENANT,
) -> BulkExtraction:
    source = ManifestSource(root, "manifest.json", decoder=SentinelDecoder())
    return BulkExtraction(
        private / "state" / name, source, Grants({RES_D, RES_O, RES_D2}), tenant=tenant
    )


def contradiction(native: int = 0, feed_revision: int = 11) -> dict[str, object]:
    """A change entry that contradicts a decision at the same revision."""
    entry = change(feed_revision, native, 1)
    entry["row"] = {**entry["row"], "content_digest": digest_of("generated-contradiction")}
    return entry


def swap_inode(export: Export, relative: str) -> None:
    """What an rsync does: the same bytes under a new inode, which fails a file mid-read."""
    target = export.root / relative
    (target.parent / "swap.tmp").write_bytes(target.read_bytes())
    os.replace(target.parent / "swap.tmp", target)


def finish(engine: BulkExtraction) -> str:
    run = engine.start(RunLimits(page_size=2))
    engine.step(run)
    engine.consume_changes(run, "decision")
    return run


def disputed_first_run(private: Path, export: Export) -> tuple[BulkExtraction, str]:
    """Manifest revision 1, then a change that contradicts decision 0 at its revision."""
    standard(export)
    engine = engine_for(private, export.root)
    run = finish(engine)
    export.append(contradiction())
    engine.consume_changes(run, "decision")
    assert state_of(engine, run, "generated-decision-0") == "disputed"
    return engine, run


def revise(
    export: Export,
    rows: dict[int, SourceRow],
    *,
    revision: str = "generated-manifest-2",
    pid: str = "generated-decisions-0-r2",
) -> None:
    """A re-export: decision file 0 replaced by a new file, high-water past the change."""
    for old in [k for k in export.parts if k.startswith("generated-decisions-0")]:
        export.parts.pop(old)
    export.partition(pid, RES_D, "decision", [rows.get(i, decision(i)) for i in range(4)])
    export.revision = revision
    export.high_water = {"decision": 11, "outcome": 10}
    export.write()


def state_of(engine: BulkExtraction, run: str, native: str, resource: str = RES_D) -> str:
    family = family_id(TENANT, resource, native)
    return next(f["state"] for f in engine.families(run) if f["family_id"] == family)


def counts(engine: BulkExtraction, run: str) -> tuple[int, int, int, int]:
    result = engine.reconcile_outcomes(run)
    assert result.verified + result.pending + result.disputed == result.decisions
    return result.decisions, result.verified, result.disputed, result.unmatched_outcomes


# ---- D1 the laundering repro ----------------------------------------------------------------


def test_a_dispute_survives_a_new_manifest_revision_that_covers_the_contradiction(
    private: Path,
) -> None:
    export = Export(private / "export")
    engine, first = disputed_first_run(private, export)
    revise(export, {0: CONTRADICTED})  # the re-export already carries the contradicting payload
    run = finish(engine)
    assert run != first
    assert state_of(engine, run, "generated-decision-0") == "disputed"
    assert counts(engine, run) == (8, 3, 1, 0)
    published = engine.publish(run)
    assert published.published  # disputes are reported, not gated, as before
    disputes = [d[0] for d in engine.semantic_manifest(run)["disputes"]]
    assert disputes == [family_id(TENANT, RES_D, "generated-decision-0")]
    # Exactly what the ledger prevents: a register that never saw the dispute verifies it.
    control = engine_for(private, export.root, name="control.sqlite3")
    ctrl = finish(control)
    assert counts(control, ctrl) == (8, 4, 0, 0)
    assert control.publish(ctrl).snapshot_id != published.snapshot_id


# ---- D2 revision bump and re-issued object ---------------------------------------------------


@pytest.mark.parametrize(
    "reissued",
    [
        replace(decision(0, revision=2), content_digest=digest_of("generated-bumped")),
        replace(decision(0), native_id="generated-decision-0-reissued"),
    ],
    ids=["revision_bumped", "object_reissued"],
)
def test_neither_a_revision_bump_nor_a_reissued_object_launders_a_dispute(
    private: Path, reissued: SourceRow
) -> None:
    export = Export(private / "export")
    engine, _ = disputed_first_run(private, export)
    revise(export, {0: reissued})
    run = finish(engine)
    assert state_of(engine, run, reissued.native_id) == "disputed"
    assert counts(engine, run) == (8, 3, 1, 0)


# ---- D3 re-keying, and rows with no decision key ---------------------------------------------


def doc(revision: int, content: str) -> SourceRow:
    return SourceRow(
        "document",
        "generated-doc-policy",
        revision,
        "generated/policy.md",
        digest_of(content),
        40,
        available_at=50,
    )


def test_re_keying_cannot_launder_a_dispute_and_keyless_rows_carry_by_family(
    private: Path,
) -> None:
    export = Export(private / "export")
    standard(export)
    export.partition("generated-docs-0", RES_D, "decision", [doc(1, "generated-policy-a")])
    export.write()
    engine = engine_for(private, export.root)
    first = finish(engine)
    export.append(contradiction())
    rival = {
        "feed": "decision",
        "feed_revision": 12,
        "op": "upsert",
        "resource_id": RES_D,
        "native_id": "generated-doc-policy",
        "revision": 1,
        "row": asdict(doc(1, "generated-policy-b")),
    }
    export.append(rival)
    engine.consume_changes(first, "decision")
    assert state_of(engine, first, "generated-doc-policy") == "disputed"
    rekeyed = replace(decision(0, revision=2), decision_key="generated-case-0-rekeyed")
    export.parts.pop("generated-docs-0")
    export.partition("generated-docs-0-r2", RES_D, "decision", [doc(2, "generated-policy-c")])
    revise(export, {0: rekeyed})
    export.high_water = {"decision": 12, "outcome": 10}
    export.write()
    run = finish(engine)
    assert state_of(engine, run, "generated-decision-0") == "disputed"
    # The re-keyed decision is the disputed one; outcome 0 now names no decision at all.
    assert counts(engine, run) == (8, 3, 1, 1)
    assert state_of(engine, run, "generated-doc-policy") == "disputed"
    cited = engine.feature_citation(
        run, resource_id=RES_D, native_id="generated-doc-policy", decision_time=10_000
    )
    assert cited.status == "excluded_disputed"


# ---- D4 scope -----------------------------------------------------------------------------


def test_a_carried_dispute_stays_in_its_tenant_and_source_and_ignores_the_export_root(
    private: Path,
) -> None:
    export = Export(private / "export")
    engine, _ = disputed_first_run(private, export)
    revise(export, {0: CONTRADICTED})
    # Another source of the same tenant uses the same decision key: not disputed.
    export.partition("generated-elsewhere-0", RES_D2, "decision", [decision(0)])
    export.write()
    run = finish(engine)
    assert state_of(engine, run, "generated-decision-0") == "disputed"
    assert state_of(engine, run, "generated-decision-0", resource=RES_D2) == "live"
    # Another tenant on the same register: not disputed.
    other = engine_for(private, export.root, tenant=OTHER_TENANT)
    theirs = finish(other)
    family = family_id(OTHER_TENANT, RES_D, "generated-decision-0")
    assert next(f["state"] for f in other.families(theirs) if f["family_id"] == family) == "live"
    # The same tenant's source re-exported under another root: still disputed, because the
    # dispute names the evidence, and a path or root confers no identity. (Its manifest
    # revision is its own: the same revision under another root is refused as a resume of
    # the run bound to the first root, `source_scope_changed`.)
    (private / "moved").mkdir()
    moved = Export(private / "moved")
    standard(moved)
    revise(moved, {0: CONTRADICTED}, revision="generated-manifest-moved")
    elsewhere = engine_for(private, moved.root)
    there = finish(elsewhere)
    assert state_of(elsewhere, there, "generated-decision-0") == "disputed"
    assert counts(elsewhere, there) == (8, 3, 1, 0)


# ---- D5 arrival order and concurrent runs -------------------------------------------------


def rival_of(native: int, feed_revision: int) -> ChangeEntry:
    row = replace(decision(native), content_digest=digest_of("generated-contradiction"))
    return ChangeEntry("decision", feed_revision, "upsert", RES_D, row.native_id, 1, row)


def revision_two(native: int, feed_revision: int) -> ChangeEntry:
    row = replace(decision(native, revision=2), content_digest=digest_of("generated-revised"))
    return ChangeEntry("decision", feed_revision, "upsert", RES_D, row.native_id, 2, row)


@pytest.mark.parametrize("dispute_first", [False, True])
def test_rows_staged_before_a_dispute_are_held_exactly_like_rows_staged_after(
    private: Path, dispute_first: bool
) -> None:
    source = GeneratedSource(history(8))
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    entries = [rival_of(2, 11), revision_two(2, 12)]
    if not dispute_first:
        entries = [revision_two(2, 11), rival_of(2, 12)]
    for entry in entries:
        source.push("decision", entry)
    engine.consume_changes(run, "decision")
    # Decision 2 has an independent outcome: without the carry, revision 2 would verify it.
    assert state_of(engine, run, "generated-decision-2") == "disputed"
    assert counts(engine, run) == (8, 3, 1, 0)


def test_a_dispute_recorded_in_one_run_holds_rows_already_staged_in_a_concurrent_run(
    private: Path,
) -> None:
    path = private / "state" / "register.sqlite3"
    first_source = GeneratedSource(history(8))
    second_source = GeneratedSource(history(8), revision="generated-manifest-2")
    first = BulkExtraction(path, first_source, Grants({RES_D, RES_O}), tenant=TENANT)
    second = BulkExtraction(path, second_source, Grants({RES_D, RES_O}), tenant=TENANT)
    run_one, run_two = first.start(), second.start()
    first.step(run_one)
    second.step(run_two)
    assert counts(second, run_two) == (8, 4, 0, 0)
    first_source.push("decision", rival_of(2, 11))
    first.consume_changes(run_one, "decision")
    assert state_of(second, run_two, "generated-decision-2") == "disputed"
    assert counts(second, run_two) == (8, 3, 1, 0)
    assert [d[0] for d in second.semantic_manifest(run_two)["disputes"]] == [
        family_id(TENANT, RES_D, "generated-decision-2")
    ]


# ---- D6 the ledger and permanence ---------------------------------------------------------


def test_the_ledger_keeps_digests_only_and_nothing_clears_a_dispute(private: Path) -> None:
    export = Export(private / "export")
    engine, first = disputed_first_run(private, export)
    with sqlite3.connect(private / "state" / "register.sqlite3") as db:
        ledger = db.execute("SELECT tenant, resource_id, key, run_id FROM dispute_keys").fetchall()
    assert sorted(key for _, _, key, _ in ledger) == sorted(
        [
            bx.dispute_key(TENANT, RES_D, "family", "generated-decision-0"),
            bx.dispute_key(TENANT, RES_D, "decision", "generated-case-0"),
        ]
    )
    assert {(t, r, run) for t, r, _, run in ledger} == {(TENANT, RES_D, first)}
    text = json.dumps(ledger)
    for needle in ("generated-decision-0", "generated-case-0", "generated/", decision(0).path):
        assert needle not in text
    # Manifest 2 with its file failing mid-read (quarantine), a clean later revision in the
    # change feed, the successor that re-reads the file, then manifest 3: still disputed.
    revise(export, {0: CONTRADICTED})
    run = engine.start(RunLimits(page_size=2))
    engine.step(run, max_pages=1)  # decision 0 is staged, its file still being read
    swap_inode(export, "parts/generated-decisions-0-r2.parquet")
    engine.step(run)
    parts = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert parts["generated-decisions-0-r2"] == "failed"
    assert state_of(engine, run, "generated-decision-0") == "disputed"
    export.append(change(12, 0, 3))  # a clean revision 3 of the same decision
    engine.consume_changes(run, "decision")
    assert state_of(engine, run, "generated-decision-0") == "disputed"
    successor = engine.successor(run)
    engine.step(successor)
    engine.consume_changes(successor, "decision")
    assert state_of(engine, successor, "generated-decision-0") == "disputed"
    assert counts(engine, successor) == (8, 3, 1, 0)
    revise(
        export,
        {0: decision(0, revision=4)},
        revision="generated-manifest-3",
        pid="generated-decisions-0-r3",
    )
    export.high_water = {"decision": 12, "outcome": 10}
    export.write()
    third = finish(engine)
    assert state_of(engine, third, "generated-decision-0") == "disputed"
    assert counts(engine, third) == (8, 3, 1, 0)


# ---- D7 a disputed object is not an unknown object ------------------------------------------


def test_a_disputed_object_is_a_disputed_exclusion_never_an_unknown_one(private: Path) -> None:
    source = GeneratedSource(history(8))
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start()
    engine.step(run)
    source.push("decision", rival_of(1, 11))
    engine.consume_changes(run, "decision")
    coverage = engine.feature_coverage(
        run,
        [
            (RES_D, "generated-decision-1", 10_000),
            (RES_D, "generated-decision-3", 10_000),
            (RES_D, "generated-decision-404", 10_000),
        ],
    )
    assert coverage == {"cited": 1, "excluded_disputed": 1, "unknown_object": 1}
