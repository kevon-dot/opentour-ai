"""OPE-519 work partitioning: by source, then by a stable hash of tenant/workflow/case.

Fixture kind: generated. Every tenant, workflow, case and row is an invented `generated-*`
sentinel. Results prove software behavior only and are not provider or customer evidence.

Acceptance IDs (implementer-chosen; the issue names none), continuing R1-R14:
P1 stable digest (known answer, independent re-implementation, other hash seeds),
P2 unambiguous encoding, P3 every admission stamped with source then bucket,
P4 bounded work-partition paging covers each admission once, P5 separate from family
assignment, P6 assignment survives crash and resume in another process, P7 a changed scheme
is refused on resume, P8 pre-format checkpoints refused and untouched, P9 no builtin hash().

New module attributes are reached through `bx.` so that, on a tree without this change,
each test fails on its own instead of the whole file failing to import.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot import bulk_extraction as bx
from opentour_cell.pilot.bulk_extraction import (
    BulkExtraction,
    ChangeEntry,
    ExtractionError,
    RunLimits,
    SourceRow,
    family_id,
)
from test_bulk_extraction import (
    TENANT,
    GeneratedSource,
    Grants,
    decision,
    digest_of,
    history,
)

WORKFLOW = "generated-workflow-1"
RES_D = "generated-resource-decisions"
RES_O = "generated-resource-outcomes"
SCHEME = "opentour.ope519.work-partition.sha256-lp.v1"


@pytest.fixture
def private(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    return tmp_path


def engine_for(
    private: Path,
    source: GeneratedSource,
    *,
    name: str = "register.sqlite3",
    workflow: str | None = WORKFLOW,
    buckets: int = 8,
) -> BulkExtraction:
    kwargs: dict[str, object] = {"tenant": TENANT, "workflow": workflow, "work_buckets": buckets}
    return BulkExtraction(private / name, source, Grants({RES_D, RES_O}), **kwargs)  # type: ignore[arg-type]


def reference_key(tenant: str, workflow: str | None, kind: str, case: str) -> str:
    """An independent re-implementation of the documented encoding, to pin it."""
    fields = (SCHEME, tenant, workflow, kind, case)
    blob = len(fields).to_bytes(4, "big")
    for value in fields:
        if value is None:
            blob += b"\x00"
        else:
            data = value.encode("utf-8")
            blob += b"\x01" + len(data).to_bytes(8, "big") + data
    return hashlib.sha256(blob).hexdigest()


# ---- P1 stable digest ----


def test_work_partition_key_matches_a_known_answer_and_an_independent_reimplementation() -> None:
    # Known answers pin the digest across machines and Python versions: if the encoding ever
    # drifts, every resumed run would reassign its rows, so this must fail loudly.
    assert (
        bx.work_partition_key("generated-tenant-a", WORKFLOW, "case", "generated-case-1")
        == "471eeeb0f55e3e8220924de2dfdb83ed0aa3b21b9d2db059ad41142d8a504346"
    )
    assert (
        bx.work_partition_key("generated-tenant-a", None, "decision", "generated-case-1")
        == "1ec2c51b422809a4c61efddb27a89b023cca880d003643fd34a8d1de1a9562da"
    )
    assert bx.work_bucket("generated-tenant-a", WORKFLOW, "case", "generated-case-1", 64) == 6
    for i in range(200):
        case = f"generated-case-{i}-é"
        expected = reference_key(TENANT, WORKFLOW, "case", case)
        assert bx.work_partition_key(TENANT, WORKFLOW, "case", case) == expected
        assert bx.work_bucket(TENANT, WORKFLOW, "case", case, 37) == int(expected, 16) % 37


HASH_SEED_SCRIPT = """
import json, sys
from opentour_cell.pilot.bulk_extraction import work_bucket
cases = json.loads(sys.argv[1])
print(json.dumps([work_bucket("generated-tenant-a", "generated-workflow-1", "case", c, 64)
                  for c in cases]))
"""


@pytest.mark.parametrize("seed", ["0", "1", "4242", "random"])
def test_buckets_are_identical_in_processes_with_different_hash_seeds(seed: str) -> None:
    cases = [f"generated-case-{i}" for i in range(64)]
    local = [bx.work_bucket("generated-tenant-a", WORKFLOW, "case", c, 64) for c in cases]
    done = subprocess.run(
        [sys.executable, "-c", HASH_SEED_SCRIPT, json.dumps(cases)],
        capture_output=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()[-400:]
    assert json.loads(done.stdout) == local
    # The same inputs under builtin hash() would differ between seeds; the bucket set is
    # also genuinely spread, not a constant.
    assert len(set(local)) > 16


# ---- P2 unambiguous encoding ----


def test_the_encoding_is_unambiguous_across_field_boundaries() -> None:
    key = bx.work_partition_key
    assert key("a|b", "c", "case", "x") != key("a", "b|c", "case", "x")
    assert key("t", "a|b", "case", "c") != key("t", "a", "case", "b|c")
    assert key("t", "ab", "case", "c") != key("t", "a", "case", "bc")
    assert key("t", None, "case", "c") != key("t", "None", "case", "c")
    assert key("t", "w", "case", "c") != key("t", "w", "decision", "c")
    assert key("t\x00", "w", "case", "c") != key("t", "\x00w", "case", "c")
    for bad in (
        ("", "w", "case", "c"),
        ("t", "", "case", "c"),
        ("t", "w", "case", ""),
        ("t", "w", "other", "c"),
        ("t", "w", "case", "\udc80"),
        ("t", 7, "case", "c"),
    ):
        with pytest.raises(ExtractionError) as error:
            key(*bad)
        assert error.value.code == "work_partition_key_invalid"
    with pytest.raises(ExtractionError):
        bx.work_bucket("t", "w", "case", "c", 0)
    with pytest.raises(ExtractionError):
        bx.work_bucket("t", "w", "case", "c", True)


# ---- P3 every admission is stamped ----


def test_every_admission_is_stamped_with_its_source_then_its_stable_bucket(private: Path) -> None:
    engine = engine_for(private, GeneratedSource(history(40)))
    run = engine.start(RunLimits(page_size=7))
    engine.step(run)
    expected: dict[str, tuple[str, int]] = {}
    for i in range(40):
        bucket = bx.work_bucket(TENANT, WORKFLOW, "decision", f"generated-case-{i}", 8)
        expected[family_id(TENANT, RES_D, f"generated-decision-{i}")] = (RES_D, bucket)
        if i % 2 == 0:
            fam = family_id(TENANT, RES_O, f"generated-outcome-{i}-independent")
            expected[fam] = (RES_O, bucket)  # the outcome lands in its decision's bucket
    stamped = {a[0]: (a[4], a[5]) for a in engine.semantic_manifest(run)["admissions"]}
    assert stamped == expected
    assert len({b for _, b in stamped.values()}) > 1
    status = engine.status(run)
    assert status["work_partitioning"] == {
        "scheme": SCHEME,
        "buckets": 8,
        "workflow_registered": True,
    }
    assert sum(p["admissions"] for p in status["work_partitions"]) == 60
    assert [p["resource_id"] for p in status["work_partitions"]] == sorted(
        p["resource_id"] for p in status["work_partitions"]
    )
    assert WORKFLOW not in json.dumps(status) and "generated-case-" not in json.dumps(status)


def test_an_explicit_case_key_wins_and_rows_with_no_case_are_kept_unkeyed(private: Path) -> None:
    cased = SourceRow(
        "decision",
        "generated-decision-c",
        1,
        "generated/c.json",
        digest_of("c"),
        10,
        decision_key="generated-decision-key-c",
        case_key="generated-case-shared",
    )
    loose = SourceRow("document", "generated-doc-loose", 1, "generated/l.md", digest_of("l"), 10)
    blank = SourceRow(
        "decision", "generated-decision-b", 1, "generated/b.json", digest_of("b"), 10, case_key=""
    )
    parts = {
        "generated-decisions-0": (RES_D, "decision", [cased, loose, blank]),
        "generated-outcomes-0": (RES_O, "outcome", []),
    }
    engine = engine_for(private, GeneratedSource(parts))
    run = engine.start()
    engine.step(run)
    stamped = {a[0]: a[5] for a in engine.semantic_manifest(run)["admissions"]}
    assert stamped[family_id(TENANT, RES_D, "generated-decision-c")] == bx.work_bucket(
        TENANT, WORKFLOW, "case", "generated-case-shared", 8
    )
    assert stamped[family_id(TENANT, RES_D, "generated-doc-loose")] is None
    # An empty case key is malformed, not silently treated as "no case".
    assert family_id(TENANT, RES_D, "generated-decision-b") not in stamped
    assert engine.status(run)["rejected_rows"] == {"malformed_row": 1}
    assert {"resource_id": RES_D, "bucket": None, "admissions": 1} in engine.work_partitions(run)


# ---- P4 bounded paging per work partition ----


def test_work_partition_pages_are_bounded_and_cover_every_admission_once(private: Path) -> None:
    engine = engine_for(private, GeneratedSource(history(30)), buckets=4)
    run = engine.start()
    engine.step(run)
    seen: list[tuple[str, int]] = []
    for part in engine.work_partitions(run):
        after = None
        while True:
            page = engine.work_partition_rows(
                run, part["resource_id"], part["bucket"], after=after, limit=2
            )
            if not page:
                break
            assert len(page) <= 2
            assert {r["work_bucket"] for r in page} == {part["bucket"]}
            seen.extend((r["family_id"], r["revision"]) for r in page)
            after = (page[-1]["family_id"], page[-1]["revision"])
    admissions = [(a[0], a[1]) for a in engine.semantic_manifest(run)["admissions"]]
    assert sorted(seen) == sorted(admissions) and len(seen) == len(set(seen)) == 45
    with pytest.raises(ExtractionError):
        engine.work_partition_rows(run, RES_D, 0, limit=10_000)


# ---- P5 separate from family assignment ----


def test_the_bucket_follows_the_case_never_the_evidence_family(private: Path) -> None:
    source = GeneratedSource(history(4))
    engine = engine_for(private, source)
    run = engine.start()
    engine.step(run)
    moved = SourceRow(
        "decision",
        "generated-decision-1",
        2,
        "generated/decisions/1.json",
        digest_of("decision-1-2"),
        200,
        available_at=100,
        decision_key="generated-case-1",
        case_key="generated-case-elsewhere",
    )
    source.push("decision", ChangeEntry("decision", 11, "upsert", RES_D, moved.native_id, 2, moved))
    engine.consume_changes(run, "decision")
    fam = family_id(TENANT, RES_D, "generated-decision-1")
    by_revision = {a[1]: a[5] for a in engine.semantic_manifest(run)["admissions"] if a[0] == fam}
    assert by_revision == {
        1: bx.work_bucket(TENANT, WORKFLOW, "decision", "generated-case-1", 8),
        2: bx.work_bucket(TENANT, WORKFLOW, "case", "generated-case-elsewhere", 8),
    }
    # Family identity ignores the workflow registration; the work bucket does not.
    other = engine_for(private, GeneratedSource(history(40)), name="o.sqlite3", workflow="w-2")
    base = engine_for(private, GeneratedSource(history(40)), name="b.sqlite3")
    runs = [(e, e.start()) for e in (other, base)]
    for e, r in runs:
        e.step(r)
    a, b = ({x[0]: x[5] for x in e.semantic_manifest(r)["admissions"]} for e, r in runs)
    assert a.keys() == b.keys()
    assert a != b
    # The work key is domain-separated from the family identity's digest of the same text.
    assert bx.work_partition_key(TENANT, WORKFLOW, "case", "n") not in {
        hashlib.sha256(json.dumps([TENANT, WORKFLOW, "n"]).encode()).hexdigest(),
        hashlib.sha256(
            json.dumps([TENANT, RES_D, "n"], separators=(",", ":")).encode()
        ).hexdigest(),
    }


def test_a_different_case_at_the_same_revision_is_disputed_not_moved(private: Path) -> None:
    source = GeneratedSource(history(4))
    engine = engine_for(private, source)
    run = engine.start()
    engine.step(run)
    original = decision(1)
    recased = replace(original, case_key="generated-case-other")
    source.push(
        "decision", ChangeEntry("decision", 11, "upsert", RES_D, original.native_id, 1, recased)
    )
    source.overlap = 100
    engine.consume_changes(run, "decision")
    fam = family_id(TENANT, RES_D, "generated-decision-1")
    rows = [a for a in engine.semantic_manifest(run)["admissions"] if a[0] == fam]
    assert [(a[1], a[3], a[5]) for a in rows] == [
        (1, "disputed", bx.work_bucket(TENANT, WORKFLOW, "decision", "generated-case-1", 8))
    ]


# ---- P6 crash and resume in another process ----

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
        if n["v"] == 5:
            os._exit(17)
engine = BulkExtraction(
    Path(sys.argv[2]), GeneratedSource(history(40)),
    Grants({"generated-resource-decisions", "generated-resource-outcomes"}),
    tenant=TENANT, workflow="generated-workflow-1", work_buckets=8, fault=fault,
)
engine.step(engine.start(RunLimits(page_size=3)))
"""


def test_a_run_killed_under_one_hash_seed_resumes_identically_under_another(
    private: Path,
) -> None:
    reference = engine_for(private, GeneratedSource(history(40)), name="ref.sqlite3")
    ref_run = reference.start(RunLimits(page_size=3))
    reference.step(ref_run)
    path = private / "killed.sqlite3"
    done = subprocess.run(
        [sys.executable, "-c", CRASH_SCRIPT, str(Path(__file__).parent), str(path)],
        capture_output=True,
        env={**os.environ, "PYTHONHASHSEED": "7"},
        check=False,
    )
    assert done.returncode == 17, done.stderr.decode()[-400:]
    engine = engine_for(private, GeneratedSource(history(40)), name="killed.sqlite3")
    run = engine.start(RunLimits(page_size=3))
    sealed = engine.semantic_manifest(run)["admissions"]
    assert 0 < len(sealed) < 60
    engine.step(run)
    assert engine.semantic_manifest(run) == reference.semantic_manifest(ref_run)
    assert engine.status(run)["duplicate_deliveries"] == 0


# ---- P7 a changed scheme is refused on resume ----


@pytest.mark.parametrize(
    ("workflow", "buckets"), [("generated-workflow-2", 8), (WORKFLOW, 16), (None, 8)]
)
def test_resuming_with_a_different_partitioning_is_refused(
    private: Path, workflow: str | None, buckets: int
) -> None:
    source = GeneratedSource(history())
    first = engine_for(private, source)
    run = first.start(RunLimits(page_size=2))
    first.step(run, max_pages=2)
    page = source.read_partition("generated-decisions-1", start=0, limit=2)
    changed = engine_for(private, source, workflow=workflow, buckets=buckets)
    for attempt in (
        lambda: changed.start(RunLimits(page_size=2)),
        lambda: changed.step(run),
        lambda: changed.accept_partition_page(run, page),
        lambda: changed.successor(run),
    ):
        with pytest.raises(ExtractionError) as error:
            attempt()
        assert error.value.code == "work_partitioning_changed"
    same = engine_for(private, source)
    assert same.start(RunLimits(page_size=2)) == run
    same.step(run)
    assert same.status(run)["snapshot_state"] == "complete"


def test_invalid_partitioning_settings_are_refused(private: Path) -> None:
    for kwargs, code in (
        ({"workflow": ""}, "work_partition_key_invalid"),
        ({"work_buckets": 0}, "work_buckets_invalid"),
        ({"work_buckets": 1 << 20}, "work_buckets_invalid"),
    ):
        with pytest.raises(ExtractionError) as error:
            BulkExtraction(
                private / "bad.sqlite3",
                GeneratedSource(history()),
                Grants([]),
                tenant=TENANT,
                **kwargs,
            )
        assert error.value.code == code


# ---- P8 checkpoints written before this format ----

# The exact `runs` and `admissions` shapes written by the previous checkpoint format.
LEGACY_DDL = (
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, manifest_revision TEXT NOT NULL, mapping_revision TEXT NOT NULL,
        limits TEXT NOT NULL, capability TEXT NOT NULL, state TEXT NOT NULL, reason TEXT NOT NULL,
        predecessor TEXT, rows_seen INTEGER NOT NULL DEFAULT 0,
        input_bytes INTEGER NOT NULL DEFAULT 0, duplicate_deliveries INTEGER NOT NULL DEFAULT 0,
        pages INTEGER NOT NULL DEFAULT 0, retries INTEGER NOT NULL DEFAULT 0,
        discarded_pages INTEGER NOT NULL DEFAULT 0, elapsed REAL NOT NULL DEFAULT 0,
        excluded_unauthorized INTEGER NOT NULL DEFAULT 0)""",
    """CREATE TABLE admissions (
        run_id TEXT NOT NULL, family_id TEXT NOT NULL, revision INTEGER NOT NULL,
        resource_id TEXT NOT NULL, partition_id TEXT NOT NULL, kind TEXT NOT NULL,
        native_id TEXT NOT NULL, path TEXT NOT NULL, content_digest TEXT NOT NULL,
        payload_digest TEXT NOT NULL, size_bytes INTEGER NOT NULL, available_at INTEGER,
        occurred_at INTEGER, decision_key TEXT, authority TEXT, label TEXT,
        state TEXT NOT NULL, PRIMARY KEY(run_id, family_id, revision))""",
    "INSERT INTO runs(run_id, manifest_revision, mapping_revision, limits, capability, state,"
    " reason) VALUES ('run-generated-legacy', 'generated-manifest-1', 'generated-mapping-1',"
    " '{}', '{}', 'active', '')",
)


@pytest.mark.parametrize(
    "extra",
    [
        (),
        ("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",),
        (
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO meta VALUES ('checkpoint_format', 'opentour.ope519.checkpoint.v1')",
        ),
        (
            "CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
            "INSERT INTO meta VALUES ('checkpoint_format', 'opentour.ope519.checkpoint.v3')",
        ),
    ],
)
def test_a_checkpoint_from_before_this_format_is_refused_and_left_untouched(
    private: Path, extra: tuple[str, ...]
) -> None:
    path = private / "legacy.sqlite3"
    db = sqlite3.connect(path)
    for statement in (*LEGACY_DDL, *extra):
        db.execute(statement)
    db.commit()
    db.close()
    path.chmod(0o600)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ExtractionError) as error:
        BulkExtraction(path, GeneratedSource(history()), Grants({RES_D}), tenant=TENANT)
    assert error.value.code == "checkpoint_format_unsupported"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_a_foreign_sqlite_file_is_refused_not_adopted(private: Path) -> None:
    path = private / "foreign.sqlite3"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE generated_unrelated (x INTEGER)")
    db.commit()
    db.close()
    with pytest.raises(ExtractionError) as error:
        BulkExtraction(path, GeneratedSource(history()), Grants([]), tenant=TENANT)
    assert error.value.code == "checkpoint_format_unsupported"


# ---- P9 no builtin hash() ----


def test_the_module_never_calls_the_per_process_builtin_hash() -> None:
    tree = ast.parse(inspect.getsource(bx))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "hash"
    ]
    assert calls == []
    assert not any(isinstance(n, ast.Name) and n.id == "hash" for n in ast.walk(tree))
