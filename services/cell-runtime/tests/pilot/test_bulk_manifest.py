"""OPE-519 first route: partition manifest validation, immutability and the change manifest.

Fixture kind: generated. Every file is written at test time under a private temp root and
holds invented `generated-*` rows. The partition files are SENTINEL CONTAINERS, not Parquet:
they carry the Parquet magic bytes around an NDJSON body so the manifest checks can run, and
`SentinelDecoder` is a test double for the Parquet row decoder that is not built (no Parquet
library is a locked dependency). Results prove software behavior only and are not provider
or customer evidence.

Acceptance IDs (implementer-chosen; the issue names none):
M1 end-to-end manifest route into the engine, M2 Parquet row decoding reported blocked,
M3 immutability judged on computed digests, M4 declared digest/size checked against bytes,
M5 manifest revision rewrite refused, M6 approved-root path confinement, M7 strict manifest
shape, M8 Parquet magic, M9 bounded change pages, M10 append-only change manifest,
M11 snapshot-only, M12 mutation during a read never seals, M13 private ledger,
M14 strict change entries.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import IO, Any

import pytest
from opentour_cell.pilot.bulk_extraction import (
    BulkExtraction,
    ExtractionError,
    RunLimits,
    SourceRow,
    family_id,
    partition_digest,
)
from opentour_cell.pilot.bulk_manifest import (
    CHANGE_MANIFEST_FORMAT,
    PARTITION_MANIFEST_FORMAT,
    DecodedPage,
    ManifestSource,
)
from test_bulk_extraction import TENANT, Grants, decision, outcome

MAPPING = "generated-mapping-1"
RES_D = "generated-resource-decisions"
RES_O = "generated-resource-outcomes"


class SentinelDecoder:
    """Test double for the missing Parquet decoder: reads the NDJSON sentinel body."""

    def __init__(self) -> None:
        self.before_decode: Any = None

    def decode(self, handle: IO[bytes], *, size: int, start: int, limit: int) -> DecodedPage:
        if self.before_decode:
            self.before_decode()
        handle.seek(4)
        body = handle.read(size - 8).decode()
        header, *lines = body.splitlines()
        rows = [SourceRow(**json.loads(line)) for line in lines]
        chunk = tuple(rows[start : start + limit])
        return DecodedPage(chunk, start + len(chunk) >= len(rows), json.loads(header)["mapping"])


def container(rows: list[SourceRow], mapping: str = MAPPING) -> bytes:
    lines = [json.dumps({"mapping": mapping})] + [json.dumps(asdict(r)) for r in rows]
    return b"PAR1" + ("\n".join(lines) + "\n").encode() + b"PAR1"


class Export:
    """A generated customer export folder: partition files, a manifest and a change log."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.parts: dict[str, dict[str, Any]] = {}
        self.revision = "generated-manifest-1"
        self.capability: dict[str, bool] = {"change_log": True, "deletion_feed": True}
        self.high_water = {"decision": 10, "outcome": 10}
        self.change: str | None = "changes.ndjson"
        self.feed: dict[str, str] = {"id": "generated-feed-1"}
        self.overrides: dict[str, dict[str, Any]] = {}
        self.top: dict[str, Any] = {}
        (root / "changes.ndjson").write_text(json.dumps({"format": CHANGE_MANIFEST_FORMAT}) + "\n")

    def partition(self, pid: str, resource: str, feed: str, rows: list[SourceRow]) -> None:
        data = container(rows)
        path = f"parts/{pid}.parquet"
        (self.root / "parts").mkdir(exist_ok=True)
        (self.root / path).write_bytes(data)
        self.parts[pid] = {
            "partition_id": pid,
            "resource_id": resource,
            "feed": feed,
            "path": path,
            "sha256": hashlib.sha256(data).hexdigest(),
            "byte_size": len(data),
            "row_count": len(rows),
            "row_digest": partition_digest(rows),
        }

    def body(self) -> dict[str, Any]:
        return {
            "format": PARTITION_MANIFEST_FORMAT,
            "revision": self.revision,
            "mapping_revision": MAPPING,
            "capability": self.capability,
            "high_water": self.high_water,
            "partitions": [
                {**p, **self.overrides.get(k, {})} for k, p in sorted(self.parts.items())
            ],
            "change_manifest": self.change,
            "change_feed": self.feed if self.change is not None else None,
            **self.top,
        }

    def write(self, text: str | None = None) -> None:
        (self.root / "manifest.json").write_text(text or json.dumps(self.body()))

    def append(self, entry: dict[str, Any]) -> None:
        with (self.root / "changes.ndjson").open("a") as handle:
            handle.write(json.dumps(entry) + "\n")


def standard(export: Export, decisions: int = 8) -> None:
    for n in range(0, decisions, 4):
        export.partition(
            f"generated-decisions-{n // 4}",
            RES_D,
            "decision",
            [decision(i) for i in range(n, n + 4)],
        )
    export.partition(
        "generated-outcomes-0", RES_O, "outcome", [outcome(i) for i in range(0, decisions, 2)]
    )
    export.write()


def change(feed_revision: int, native: int, revision: int, op: str = "upsert") -> dict[str, Any]:
    row = asdict(decision(native, revision=revision)) if op != "tombstone" else None
    return {
        "feed": "decision",
        "feed_revision": feed_revision,
        "op": op,
        "resource_id": RES_D,
        "native_id": f"generated-decision-{native}",
        "revision": revision,
        "row": row,
    }


@pytest.fixture
def private(tmp_path: Path) -> Path:
    tmp_path.chmod(0o700)
    (tmp_path / "export").mkdir()
    (tmp_path / "state").mkdir(mode=0o700)
    return tmp_path


@pytest.fixture
def export(private: Path) -> Export:
    return Export(private / "export")


def bind(private: Path, source: ManifestSource, name: str = "register.sqlite3") -> BulkExtraction:
    """The engine binds its own register's history into the source; nothing else can."""
    return BulkExtraction(private / "state" / name, source, Grants({RES_D, RES_O}), tenant=TENANT)


def source_for(
    private: Path, export: Export, decoder: SentinelDecoder | None = None
) -> ManifestSource:
    source = ManifestSource(export.root, "manifest.json", decoder=decoder)
    bind(private, source)
    return source


def code_of(call: Any) -> str:
    with pytest.raises(ExtractionError) as error:
        call()
    return error.value.code


# ---- M1 end to end ------------------------------------------------------------------------


def test_a_verified_manifest_drives_the_engine_to_a_published_snapshot(
    private: Path, export: Export
) -> None:
    standard(export)
    source = source_for(private, export, SentinelDecoder())
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start(RunLimits(page_size=3))
    engine.step(run)
    status = engine.status(run)
    assert [p["state"] for p in status["partitions"]] == ["complete"] * 3
    assert engine.publish(run).published
    export.append(change(11, 1, 2))
    export.append(change(12, 2, 2, "tombstone"))
    export.append(change(13, 2, 3, "reopen"))
    export.append(change(14, 99, 1))
    engine.consume_changes(run, "decision")
    states = {f["family_id"]: f for f in engine.families(run)}
    assert states[family_id(TENANT, RES_D, "generated-decision-1")]["revision"] == 2
    reopened = states[family_id(TENANT, RES_D, "generated-decision-2")]
    assert (reopened["state"], reopened["revision"]) == ("live", 3)
    assert states[family_id(TENANT, RES_D, "generated-decision-99")]["state"] == "live"
    assert engine.status(run)["feeds"][0]["cursor"] == 14


# ---- M2 blocked decoder -------------------------------------------------------------------


def test_without_a_parquet_decoder_rows_are_reported_blocked_not_faked(
    private: Path, export: Export
) -> None:
    standard(export)
    source = source_for(private, export)
    manifest = source.manifest()
    assert [p.partition_id for p in manifest.partitions] == [
        "generated-decisions-0",
        "generated-decisions-1",
        "generated-outcomes-0",
    ]
    assert code_of(lambda: source.read_partition("generated-decisions-0", start=0, limit=3)) == (
        "parquet_reader_unavailable"
    )
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start()
    assert code_of(lambda: engine.step(run)) == "parquet_reader_unavailable"
    assert all(p["state"] == "pending" for p in engine.status(run)["partitions"])
    assert not engine.publish(run).published


# ---- M3 immutability ----------------------------------------------------------------------


def test_new_bytes_under_an_old_partition_id_are_a_hard_error_even_when_declared(
    private: Path, export: Export
) -> None:
    standard(export)
    source_for(private, export).manifest()
    original = (export.root / "parts/generated-decisions-0.parquet").read_bytes()
    # The customer rewrites the partition and publishes a fully self-consistent new manifest
    # revision that declares the new digest, size and row digest under the SAME ID.
    export.partition(
        "generated-decisions-0", RES_D, "decision", [decision(i, revision=2) for i in range(4)]
    )
    export.revision = "generated-manifest-2"
    export.write()
    assert code_of(source_for(private, export).manifest) == "partition_mutated"
    # Restoring the original bytes is accepted: the ID is bound to what was first read.
    (export.root / "parts/generated-decisions-0.parquet").write_bytes(original)
    export.parts["generated-decisions-0"].update(
        sha256=hashlib.sha256(original).hexdigest(),
        byte_size=len(original),
        row_digest=partition_digest([decision(i) for i in range(4)]),
    )
    export.write()
    assert len(source_for(private, export).manifest().partitions) == 3


def test_a_failed_manifest_records_nothing_in_the_ledger(private: Path, export: Export) -> None:
    standard(export)
    export.overrides["generated-outcomes-0"] = {"sha256": "0" * 64}
    export.write()
    source = source_for(private, export)
    assert code_of(source.manifest) == "partition_digest_mismatch"
    assert source.history.partition("generated-decisions-0") is None
    assert source.history.manifest("generated-manifest-1") is None


# ---- M4 declared values checked against bytes ---------------------------------------------------


@pytest.mark.parametrize(
    ("override", "code"),
    [
        ({"sha256": "a" * 64}, "partition_digest_mismatch"),
        ({"byte_size": 5}, "partition_size_mismatch"),
    ],
)
def test_a_declared_digest_or_size_that_does_not_match_the_bytes_is_refused(
    private: Path, export: Export, override: dict[str, Any], code: str
) -> None:
    standard(export)
    export.overrides["generated-decisions-1"] = override
    export.write()
    assert code_of(source_for(private, export).manifest) == code


def test_a_declared_row_digest_is_rechecked_by_the_engine_from_the_rows(
    private: Path, export: Export
) -> None:
    standard(export)
    export.overrides["generated-decisions-1"] = {"row_digest": "b" * 64}
    export.write()
    source = source_for(private, export, SentinelDecoder())
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start()
    engine.step(run)
    states = {p["partition_id"]: p["state"] for p in engine.status(run)["partitions"]}
    assert states["generated-decisions-1"] == "mismatch"
    assert not engine.publish(run).published


# ---- M5 manifest revision -----------------------------------------------------------------------


def test_a_manifest_revision_rewritten_in_place_is_refused(private: Path, export: Export) -> None:
    standard(export)
    source_for(private, export).manifest()
    export.high_water = {"decision": 9, "outcome": 10}
    export.write()
    assert code_of(source_for(private, export).manifest) == "manifest_revision_rewritten"


# ---- M6 path confinement ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["../outside.parquet", "/etc/passwd", "parts//x.parquet", "./parts/x.parquet", "a\\b"]
)
def test_partition_paths_must_stay_inside_the_approved_root(
    private: Path, export: Export, path: str
) -> None:
    standard(export)
    export.overrides["generated-decisions-0"] = {"path": path}
    export.write()
    assert code_of(source_for(private, export).manifest) == "partition_path_invalid"


def test_symlinks_cannot_lead_a_partition_out_of_the_root(private: Path, export: Export) -> None:
    standard(export)
    outside = private / "outside.parquet"
    outside.write_bytes((export.root / "parts/generated-decisions-0.parquet").read_bytes())
    (export.root / "parts/link.parquet").symlink_to(outside)
    export.overrides["generated-decisions-0"] = {"path": "parts/link.parquet"}
    export.write()
    assert code_of(source_for(private, export).manifest) == "partition_path_invalid"
    (private / "elsewhere").mkdir()
    os.replace(outside, private / "elsewhere/generated-decisions-0.parquet")
    (export.root / "linked").symlink_to(private / "elsewhere")
    export.overrides["generated-decisions-0"] = {"path": "linked/generated-decisions-0.parquet"}
    export.write()
    assert code_of(source_for(private, export).manifest) == "partition_path_invalid"


# ---- M7 strict manifest shape -------------------------------------------------------------------


def _mutations() -> list[tuple[str, Any]]:
    return [
        ("format", lambda e: e.top.update(format="opentour.other")),
        ("unknown_key", lambda e: e.top.update(immutable=True)),
        ("bool_count", lambda e: e.overrides.update({"generated-outcomes-0": {"row_count": True}})),
        (
            "bad_hex",
            lambda e: e.overrides.update({"generated-outcomes-0": {"row_digest": "Z" * 64}}),
        ),
        ("extra_part_key", lambda e: e.overrides.update({"generated-outcomes-0": {"x": 1}})),
        ("unknown_feed", lambda e: e.overrides.update({"generated-outcomes-0": {"feed": "x"}})),
        ("change_log_without_file", lambda e: setattr(e, "change", None)),
        ("snapshot_only_with_file", lambda e: e.capability.update(change_log=False)),
        ("non_bool_capability", lambda e: e.capability.update(deletion_feed=1)),
        ("negative_high_water", lambda e: e.high_water.update(decision=-1)),
        (
            "duplicate_path",
            lambda e: e.overrides.update(
                {"generated-outcomes-0": {"path": e.parts["generated-decisions-0"]["path"]}}
            ),
        ),
    ]


@pytest.mark.parametrize(("name", "mutate"), _mutations(), ids=[n for n, _ in _mutations()])
def test_malformed_manifests_are_refused(
    private: Path, export: Export, name: str, mutate: Any
) -> None:
    standard(export)
    mutate(export)
    export.write()
    assert code_of(source_for(private, export).manifest) in {
        "manifest_invalid",
        "manifest_format_unsupported",
    }


@pytest.mark.parametrize(
    "text",
    [
        '{"format": "a", "format": "b"}',
        '{"revision": NaN}',
        "not json",
        "\udc80",
    ],
)
def test_manifests_that_are_not_strict_json_are_refused(
    private: Path, export: Export, text: str
) -> None:
    (export.root / "manifest.json").write_bytes(text.encode("utf-8", "surrogateescape"))
    assert code_of(source_for(private, export).manifest) == "manifest_invalid"


def test_duplicate_partition_ids_are_refused(private: Path, export: Export) -> None:
    standard(export)
    body = export.body()
    body["partitions"].append(dict(body["partitions"][0]))
    export.write(json.dumps(body))
    assert code_of(source_for(private, export).manifest) == "manifest_invalid"


# ---- M8 Parquet magic ---------------------------------------------------------------------------


def test_a_file_without_parquet_magic_is_refused(private: Path, export: Export) -> None:
    standard(export)
    data = b"generated-not-parquet-at-all"
    (export.root / "parts/generated-outcomes-0.parquet").write_bytes(data)
    export.overrides["generated-outcomes-0"] = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "byte_size": len(data),
    }
    export.write()
    assert code_of(source_for(private, export).manifest) == "partition_not_parquet"


# ---- M9 bounded change pages ---------------------------------------------------------------------


def test_change_pages_are_bounded_and_strictly_after_the_cursor(
    private: Path, export: Export
) -> None:
    standard(export)
    for rev in range(3, 9):
        export.append(change(rev, 20 + rev, 1))
    outcome_entry = change(4, 50, 1)
    outcome_entry.update(feed="outcome", resource_id=RES_O)
    export.append(outcome_entry)
    source = source_for(private, export)
    source.manifest()
    first = source.read_changes("decision", after=4, limit=2)
    assert [e.feed_revision for e in first.entries] == [5, 6]
    assert (first.after, first.through) == (4, 6)
    rest = source.read_changes("decision", after=first.through, limit=10)
    assert [e.feed_revision for e in rest.entries] == [7, 8] and rest.through == 8
    empty = source.read_changes("decision", after=12, limit=10)
    assert (empty.entries, empty.through) == ((), 12)
    assert [e.feed for e in source.read_changes("outcome", after=0, limit=5).entries] == ["outcome"]
    assert code_of(lambda: source.read_changes("other", after=0, limit=5)) == "feed_unknown"


# ---- M10 append-only change manifest -------------------------------------------------------------


def _loaded(private: Path, export: Export) -> ManifestSource:
    source = source_for(private, export)
    source.manifest()
    return source


def test_a_rewritten_earlier_change_entry_is_refused(private: Path, export: Export) -> None:
    standard(export)
    for rev in (11, 12, 13):
        export.append(change(rev, rev, 1))
    assert len(_loaded(private, export).read_changes("decision", after=10, limit=9).entries) == 3
    log = export.root / "changes.ndjson"
    lines = log.read_text().splitlines(keepends=True)
    lines[2] = json.dumps(change(12, 12, 1, "tombstone")) + "\n"  # same feed revision, new op
    log.write_text("".join(lines))
    source = _loaded(private, export)
    assert code_of(lambda: source.read_changes("decision", after=10, limit=9)) == (
        "change_manifest_rewritten"
    )


def test_a_reformatted_earlier_line_is_a_rewrite_too(private: Path, export: Export) -> None:
    standard(export)
    export.append(change(11, 11, 1))
    _loaded(private, export).read_changes("decision", after=10, limit=9)
    log = export.root / "changes.ndjson"
    lines = log.read_text().splitlines(keepends=True)
    lines[1] = json.dumps(json.loads(lines[1]), indent=None, separators=(",", ":")) + "\n"
    log.write_text("".join(lines))
    source = _loaded(private, export)
    assert code_of(lambda: source.read_changes("decision", after=10, limit=9)) == (
        "change_manifest_rewritten"
    )


def test_a_truncated_change_manifest_is_refused(private: Path, export: Export) -> None:
    standard(export)
    for rev in (11, 12):
        export.append(change(rev, rev, 1))
    _loaded(private, export).read_changes("decision", after=10, limit=9)
    log = export.root / "changes.ndjson"
    log.write_text("".join(log.read_text().splitlines(keepends=True)[:2]))
    source = _loaded(private, export)
    assert code_of(lambda: source.read_changes("decision", after=10, limit=9)) == (
        "change_manifest_truncated"
    )


def test_appends_are_read_and_an_unterminated_tail_waits(private: Path, export: Export) -> None:
    standard(export)
    export.append(change(11, 11, 1))
    source = _loaded(private, export)
    assert len(source.read_changes("decision", after=10, limit=9).entries) == 1
    log = export.root / "changes.ndjson"
    partial = json.dumps(change(12, 12, 1))
    with log.open("a") as handle:
        handle.write(partial[:20])
    assert [
        e.feed_revision for e in source.read_changes("decision", after=10, limit=9).entries
    ] == [11]
    with log.open("a") as handle:
        handle.write(partial[20:] + "\n")
    assert [
        e.feed_revision for e in source.read_changes("decision", after=10, limit=9).entries
    ] == [
        11,
        12,
    ]


def test_an_appended_entry_that_goes_back_in_feed_order_is_refused(
    private: Path, export: Export
) -> None:
    standard(export)
    export.append(change(12, 12, 1))
    source = _loaded(private, export)
    source.read_changes("decision", after=10, limit=9)
    export.append(change(11, 11, 1))
    assert code_of(lambda: source.read_changes("decision", after=10, limit=9)) == (
        "change_manifest_out_of_order"
    )


def test_a_malformed_line_refuses_the_read_and_never_advances_the_ledger(
    private: Path, export: Export
) -> None:
    standard(export)
    export.append(change(11, 11, 1))
    log = export.root / "changes.ndjson"
    with log.open("a") as handle:
        handle.write('{"feed": "decision", "feed_revision": 12, "op": "upsert"}\n')
    export.append(change(13, 13, 1))
    source = _loaded(private, export)
    assert code_of(lambda: source.read_changes("decision", after=10, limit=9)) == (
        "change_manifest_malformed"
    )
    assert source.history.change_feed("generated-feed-1").lines == 0
    # Never verified, so correcting the unrecorded line is not a rewrite.
    lines = log.read_text().splitlines(keepends=True)
    lines[2] = json.dumps(change(12, 12, 1)) + "\n"
    log.write_text("".join(lines))
    assert len(source.read_changes("decision", after=10, limit=9).entries) == 3


# ---- M11 snapshot-only ------------------------------------------------------------------------


def test_a_snapshot_only_export_names_no_change_feed(private: Path, export: Export) -> None:
    export.capability = {"change_log": False, "deletion_feed": False}
    export.change = None
    standard(export)
    source = source_for(private, export, SentinelDecoder())
    assert source.manifest().capability.change_log is False
    assert code_of(lambda: source.read_changes("decision", after=0, limit=5)) == (
        "snapshot_only_source"
    )
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start()
    engine.step(run)
    assert engine.status(run)["completeness"] == "snapshot_only"
    assert engine.publish(run).published


# ---- M12 mutation during a read ------------------------------------------------------------------


@pytest.mark.parametrize("keep_size", [True, False])
def test_bytes_changed_during_a_read_never_seal_the_partition(
    private: Path, export: Export, keep_size: bool
) -> None:
    standard(export)
    decoder = SentinelDecoder()
    source = source_for(private, export, decoder)
    engine = BulkExtraction(
        private / "state" / "register.sqlite3", source, Grants({RES_D, RES_O}), tenant=TENANT
    )
    run = engine.start(RunLimits(page_size=2))
    target = export.root / "parts/generated-decisions-0.parquet"
    info = target.stat()
    data = bytearray(target.read_bytes())
    calls = {"n": 0}

    def tamper() -> None:
        calls["n"] += 1
        if calls["n"] == 1:  # while the first page is decoded, before it is admitted
            if keep_size:
                index = data.index(b"generated/decisions/3.json")
                data[index : index + 26] = b"generated/decisions/X.json"
                target.write_bytes(bytes(data))
                os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
            else:
                target.write_bytes(bytes(data) + b"\n")

    decoder.before_decode = tamper
    # Corrected (F4): this once asserted step() raised and the partition stayed "pending",
    # which wedged every other partition. A mutated file fails only itself, with its reason;
    # step() returns, the others complete, its staged rows are quarantined, nothing seals.
    engine.step(run)
    parts = {p["partition_id"]: p for p in engine.status(run)["partitions"]}
    mutated = parts.pop("generated-decisions-0")
    assert (mutated["state"], mutated["reason"]) == ("failed", "partition_mutated")
    assert {p["state"] for p in parts.values()} == {"complete"}
    assert engine.status(run)["quarantined_rows"] == 2
    result = engine.publish(run)
    assert not result.published
    assert "partition_failed:generated-decisions-0" in result.reasons


# ---- M13 private ledger --------------------------------------------------------------------------


def test_the_ledger_must_live_in_a_private_directory(private: Path, export: Export) -> None:
    # The ledger is the extraction register itself, so its private-directory rule applies,
    # and a source no register has bound reads nothing at all.
    open_dir = private / "open"
    open_dir.mkdir()
    open_dir.chmod(0o755)
    source = ManifestSource(export.root, "manifest.json")
    assert (
        code_of(
            lambda: BulkExtraction(
                open_dir / "ledger.sqlite3", source, Grants({RES_D}), tenant=TENANT
            )
        )
        == "private_store_required"
    )
    assert code_of(source.manifest) == "source_history_unbound"


# ---- M14 strict change entries -------------------------------------------------------------------


def _bad_entries() -> list[tuple[str, dict[str, Any]]]:
    good = change(11, 11, 1)
    row = dict(good["row"])
    return [
        ("bool_feed_revision", {**good, "feed_revision": True}),
        ("zero_revision", {**good, "revision": 0}),
        ("unknown_op", {**good, "op": "delete"}),
        ("tombstone_with_row", {**change(11, 11, 1, "tombstone"), "row": row}),
        ("upsert_without_row", {**good, "row": None}),
        ("row_id_mismatch", {**good, "row": {**row, "native_id": "generated-decision-0"}}),
        ("row_revision_mismatch", {**good, "row": {**row, "revision": 2}}),
        ("row_unknown_key", {**good, "row": {**row, "immutable": True}}),
        ("row_bad_type", {**good, "row": {**row, "size_bytes": "200"}}),
        ("entry_unknown_key", {**good, "trusted": True}),
        ("unknown_feed", {**good, "feed": "generated-other"}),
    ]


@pytest.mark.parametrize(("name", "entry"), _bad_entries(), ids=[n for n, _ in _bad_entries()])
def test_malformed_change_entries_are_refused_not_skipped(
    private: Path, export: Export, name: str, entry: dict[str, Any]
) -> None:
    standard(export)
    export.append(entry)
    source = _loaded(private, export)
    assert code_of(lambda: source.read_changes("decision", after=10, limit=9)) == (
        "change_manifest_malformed"
    )
