"""OPE-519 first route: a customer-controlled partition manifest plus an append-only change log.

`ManifestSource` implements the `SnapshotSource` port of `bulk_extraction` over files under
one approved root:

* a JSON partition manifest naming immutable Parquet partition files, each with an ID, a
  declared file SHA-256, byte size, row count and row-chain digest;
* a companion NDJSON change manifest of revisioned upserts, reopens and tombstones.

Nothing a manifest says about itself is taken as proof. The reader computes every file's
SHA-256 from its bytes and records it in a private `SourceLedger` the first time a partition
ID is seen. The same ID with different bytes is a hard error (`partition_mutated`), even when
the manifest declares the new digest consistently. A manifest revision rewritten in place is
refused, and the change manifest is verified as append-only by a rolling digest over every
committed line: a rewritten, removed or reordered earlier line is refused, never re-read. A
malformed line refuses the read rather than being skipped.

What is NOT built: the Parquet row decoder. No Parquet-capable library is a locked dependency
of this workspace, and adding one needs the lockfile owner. `read_partition` therefore raises
`parquet_reader_unavailable` unless a `PartitionDecoder` is supplied. The file checks here
(magic bytes, size, digest, identity) do not decode Parquet and do not prove a file is a
well-formed Parquet file. All fixtures are generated sentinels; nothing here is provider or
customer evidence.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sqlite3
import stat
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, Protocol

from opentour_cell.pilot.bulk_extraction import (
    ChangeEntry,
    ChangePage,
    ExtractionError,
    PartitionPage,
    PartitionRef,
    SourceCapability,
    SourceManifest,
    SourceRow,
)

PARTITION_MANIFEST_FORMAT = "opentour.ope519.partition-manifest.v1"
CHANGE_MANIFEST_FORMAT = "opentour.ope519.change-manifest.v1"
PARQUET_MAGIC = b"PAR1"
CHANGE_OPS = ("upsert", "reopen", "tombstone")
_CHUNK = 1 << 20
_GENESIS = "0" * 64
_HEX = frozenset("0123456789abcdef")

_PARTITION_KEYS = frozenset(
    {
        "partition_id",
        "resource_id",
        "feed",
        "path",
        "sha256",
        "byte_size",
        "row_count",
        "row_digest",
    }
)
_MANIFEST_KEYS = frozenset(
    {
        "format",
        "revision",
        "mapping_revision",
        "capability",
        "high_water",
        "partitions",
        "change_manifest",
    }
)
_CAPABILITY_REQUIRED = frozenset({"change_log", "deletion_feed"})
_CAPABILITY_OPTIONAL = frozenset(
    {"timestamp_only", "tie_breaker", "overlap", "full_reconciliation_done"}
)
_ENTRY_KEYS = frozenset({"feed", "feed_revision", "op", "resource_id", "native_id", "revision"})
_ROW_REQUIRED = {
    "kind": str,
    "native_id": str,
    "revision": int,
    "path": str,
    "content_digest": str,
    "size_bytes": int,
}
_ROW_OPTIONAL = {
    "available_at": int,
    "occurred_at": int,
    "decision_key": str,
    "authority": str,
    "label": str,
    "case_key": str,
}


# ---- strict parsing ----------------------------------------------------------------------


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _reject_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def _strict_json(data: bytes) -> Any:
    """UTF-8 JSON with no duplicate keys and no NaN/Infinity; raises ValueError otherwise."""
    return json.loads(
        data.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_reject_constant
    )


def _is_int(value: object) -> bool:
    return type(value) is int


def _is_text(value: object) -> bool:
    return type(value) is str and bool(value)


def _is_hex64(value: object) -> bool:
    return type(value) is str and len(value) == 64 and set(value) <= _HEX


def _safe_relative(value: object) -> PurePosixPath:
    """A relative POSIX path with no traversal, no empty or dot parts and no backslash."""
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ExtractionError("partition_path_invalid")
    if value.startswith("/") or any(part in ("", ".", "..") for part in value.split("/")):
        raise ExtractionError("partition_path_invalid")
    return PurePosixPath(value)


# ---- private ledger ------------------------------------------------------------------------


def _private_file(path: Path) -> None:
    directory = path.absolute().parent
    info = directory.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ExtractionError("private_store_required")
    os.close(os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600))


@dataclass(frozen=True)
class ChangeLogState:
    """How many committed lines of a change manifest were verified, and their rolling digest."""

    lines: int
    chain: str


class SourceLedger:
    """What the reader itself computed from bytes it read, kept across manifest revisions.

    Only computed digests are stored; a digest a manifest declares is never recorded as fact.
    """

    def __init__(self, path: Path) -> None:
        _private_file(path)
        self._path = path
        with self._txn() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS partitions (partition_id TEXT PRIMARY KEY,"
                " sha256 TEXT NOT NULL, byte_size INTEGER NOT NULL, first_revision TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS manifests (revision TEXT PRIMARY KEY,"
                " sha256 TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS change_logs (log TEXT PRIMARY KEY,"
                " lines INTEGER NOT NULL, chain TEXT NOT NULL)"
            )

    @contextmanager
    def _txn(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self._path, timeout=10, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def partition(self, partition_id: str) -> tuple[str, int] | None:
        with self._txn() as db:
            row = db.execute(
                "SELECT sha256, byte_size FROM partitions WHERE partition_id=?", (partition_id,)
            ).fetchone()
        return (row[0], row[1]) if row else None

    def manifest(self, revision: str) -> str | None:
        with self._txn() as db:
            row = db.execute(
                "SELECT sha256 FROM manifests WHERE revision=?", (revision,)
            ).fetchone()
        return row[0] if row else None

    def record_manifest(
        self, revision: str, digest: str, partitions: list[tuple[str, str, int]]
    ) -> None:
        """Record a verified manifest all at once, re-checking inside the commit scope."""
        with self._txn() as db:
            known = db.execute(
                "SELECT sha256 FROM manifests WHERE revision=?", (revision,)
            ).fetchone()
            if known is not None and known[0] != digest:
                raise ExtractionError("manifest_revision_rewritten")
            for partition_id, sha, size in partitions:
                row = db.execute(
                    "SELECT sha256, byte_size FROM partitions WHERE partition_id=?",
                    (partition_id,),
                ).fetchone()
                if row is not None and (row[0], row[1]) != (sha, size):
                    raise ExtractionError("partition_mutated")
                db.execute(
                    "INSERT OR IGNORE INTO partitions VALUES (?,?,?,?)",
                    (partition_id, sha, size, revision),
                )
            db.execute("INSERT OR IGNORE INTO manifests VALUES (?,?)", (revision, digest))

    def change_log(self, log: str) -> ChangeLogState:
        with self._txn() as db:
            row = db.execute("SELECT lines, chain FROM change_logs WHERE log=?", (log,)).fetchone()
        if row is None:
            return ChangeLogState(0, _GENESIS)
        return ChangeLogState(row[0], row[1])

    def advance_change_log(self, log: str, seen: ChangeLogState, new: ChangeLogState) -> None:
        """Compare-and-set: the ledger only moves forward from exactly the state verified."""
        with self._txn() as db:
            row = db.execute("SELECT lines, chain FROM change_logs WHERE log=?", (log,)).fetchone()
            current = (row[0], row[1]) if row else (0, _GENESIS)
            if current != (seen.lines, seen.chain):
                raise ExtractionError("ledger_conflict")
            if new.lines < seen.lines:
                raise ExtractionError("change_manifest_truncated")
            db.execute(
                "INSERT INTO change_logs VALUES (?,?,?) ON CONFLICT(log) DO UPDATE SET"
                " lines=excluded.lines, chain=excluded.chain",
                (log, new.lines, new.chain),
            )


# ---- decoder port ------------------------------------------------------------------------


@dataclass(frozen=True)
class DecodedPage:
    rows: tuple[SourceRow, ...]
    exhausted: bool
    mapping_revision: str  # the mapping the decoder actually applied, from the file's schema


class PartitionDecoder(Protocol):
    """Decodes one bounded page of rows from an opened, already digest-verified partition.

    The production implementation needs a Parquet library, which is not a locked dependency.
    """

    def decode(self, handle: IO[bytes], *, size: int, start: int, limit: int) -> DecodedPage: ...


@dataclass(frozen=True)
class VerifiedPartition:
    ref: PartitionRef
    path: PurePosixPath
    sha256: str
    byte_size: int
    device: int
    inode: int
    mtime_ns: int


# ---- the source --------------------------------------------------------------------------


class ManifestSource:
    """A `SnapshotSource` over a partition manifest and its append-only change manifest."""

    def __init__(
        self,
        root: Path,
        manifest_path: str,
        ledger: SourceLedger,
        *,
        decoder: PartitionDecoder | None = None,
        max_manifest_bytes: int = 16 << 20,
        max_change_manifest_bytes: int = 1 << 30,
        max_line_bytes: int = 1 << 20,
    ) -> None:
        info = os.stat(root, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode):
            raise ExtractionError("source_root_invalid")
        self._root = Path(os.path.realpath(root))
        self._manifest_path = _safe_relative(manifest_path)
        self._ledger = ledger
        self._decoder = decoder
        self._max_manifest = max_manifest_bytes
        self._max_changes = max_change_manifest_bytes
        self._max_line = max_line_bytes
        self._verified: dict[str, VerifiedPartition] = {}
        self._mapping = ""
        self._change_path: PurePosixPath | None = None
        self._feeds: frozenset[str] = frozenset()

    # ---- files ---------------------------------------------------------------------------

    def _open(self, relative: PurePosixPath) -> tuple[IO[bytes], os.stat_result]:
        full = self._root.joinpath(*relative.parts)
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            raise ExtractionError("source_file_missing") from None
        except OSError as error:
            # ELOOP is the refused final-component symlink; NotADirectory a file used as a
            # directory. Anything else is an I/O failure the engine may retry.
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise ExtractionError("partition_path_invalid") from None
            raise ExtractionError("source_unavailable") from None
        handle = os.fdopen(fd, "rb")
        try:
            info = os.fstat(handle.fileno())
            resolved = Path(os.path.realpath(full))
            if not stat.S_ISREG(info.st_mode) or not resolved.is_relative_to(self._root):
                raise ExtractionError("partition_path_invalid")
        except BaseException:
            handle.close()
            raise
        return handle, info

    @staticmethod
    def _digest(handle: IO[bytes]) -> tuple[str, int]:
        handle.seek(0)
        digest = hashlib.sha256()
        size = 0
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
            size += len(chunk)
        return digest.hexdigest(), size

    # ---- manifest ------------------------------------------------------------------------

    def manifest(self) -> SourceManifest:
        handle, _ = self._open(self._manifest_path)
        with handle:
            raw = handle.read(self._max_manifest + 1)
        if len(raw) > self._max_manifest:
            raise ExtractionError("manifest_over_limit")
        try:
            body = _strict_json(raw)
        except ValueError:
            raise ExtractionError("manifest_invalid") from None
        revision, mapping, capability, high_water, entries, change = self._shape(body)
        digest = hashlib.sha256(raw).hexdigest()
        known = self._ledger.manifest(revision)
        if known is not None and known != digest:
            raise ExtractionError("manifest_revision_rewritten")
        shaped = [self._entry_shape(entry, high_water) for entry in entries]
        ids = [entry["partition_id"] for entry, _ in shaped]
        paths = [path for _, path in shaped]
        if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
            raise ExtractionError("manifest_invalid")
        verified = [self._verify(entry, path) for entry, path in shaped]
        # Recorded only after every partition verified, and re-checked inside the commit.
        self._ledger.record_manifest(
            revision, digest, [(v.ref.partition_id, v.sha256, v.byte_size) for v in verified]
        )
        self._verified = {v.ref.partition_id: v for v in verified}
        self._mapping = mapping
        self._change_path = change
        self._feeds = frozenset(high_water)
        return SourceManifest(
            revision,
            mapping,
            tuple(v.ref for v in verified),
            tuple(sorted(high_water.items())),
            capability,
        )

    @staticmethod
    def _shape(
        body: Any,
    ) -> tuple[
        str,
        str,
        SourceCapability,
        dict[str, int],
        list[dict[str, Any]],
        PurePosixPath | None,
    ]:
        if type(body) is not dict or set(body) != _MANIFEST_KEYS:
            raise ExtractionError("manifest_invalid")
        if body["format"] != PARTITION_MANIFEST_FORMAT:
            raise ExtractionError("manifest_format_unsupported")
        revision, mapping = body["revision"], body["mapping_revision"]
        if not _is_text(revision) or not _is_text(mapping):
            raise ExtractionError("manifest_invalid")
        cap = body["capability"]
        if (
            type(cap) is not dict
            or not _CAPABILITY_REQUIRED <= set(cap) <= _CAPABILITY_REQUIRED | _CAPABILITY_OPTIONAL
            or any(type(v) is not bool for v in cap.values())
        ):
            raise ExtractionError("manifest_invalid")
        capability = SourceCapability(**cap)
        high_water = body["high_water"]
        if (
            type(high_water) is not dict
            or not high_water
            or any(not _is_text(k) or not _is_int(v) or v < 0 for k, v in high_water.items())
        ):
            raise ExtractionError("manifest_invalid")
        entries = body["partitions"]
        if type(entries) is not list or not entries:
            raise ExtractionError("manifest_invalid")
        change = body["change_manifest"]
        # A source without a change log is snapshot-only and must not name a change feed; a
        # source that claims a change log must name the file that carries it.
        if capability.change_log != (change is not None):
            raise ExtractionError("manifest_invalid")
        return (
            revision,
            mapping,
            capability,
            dict(high_water),
            entries,
            None if change is None else _safe_relative(change),
        )

    def _entry_shape(
        self, entry: Any, high_water: dict[str, int]
    ) -> tuple[dict[str, Any], PurePosixPath]:
        if type(entry) is not dict or set(entry) != _PARTITION_KEYS:
            raise ExtractionError("manifest_invalid")
        if not all(_is_text(entry[k]) for k in ("partition_id", "resource_id", "feed")):
            raise ExtractionError("manifest_invalid")
        if entry["feed"] not in high_water:
            raise ExtractionError("manifest_invalid")
        if not _is_hex64(entry["sha256"]) or not _is_hex64(entry["row_digest"]):
            raise ExtractionError("manifest_invalid")
        if not all(_is_int(entry[k]) and entry[k] >= 0 for k in ("byte_size", "row_count")):
            raise ExtractionError("manifest_invalid")
        path = _safe_relative(entry["path"])
        if path == self._manifest_path:
            raise ExtractionError("manifest_invalid")
        return entry, path

    def _verify(self, entry: dict[str, Any], path: PurePosixPath) -> VerifiedPartition:
        handle, info = self._open(path)
        with handle:
            computed, size = self._digest(handle)
            magic = b""
            if size >= 12:
                handle.seek(0)
                head = handle.read(4)
                handle.seek(size - 4)
                magic = head + handle.read(4)
        # Immutability is judged on the digest computed here, against the digest computed
        # the first time this ID was read; what the manifest declares cannot excuse a change.
        recorded = self._ledger.partition(entry["partition_id"])
        if recorded is not None and recorded != (computed, size):
            raise ExtractionError("partition_mutated")
        if size != entry["byte_size"]:
            raise ExtractionError("partition_size_mismatch")
        if computed != entry["sha256"]:
            raise ExtractionError("partition_digest_mismatch")
        if magic != PARQUET_MAGIC * 2:
            raise ExtractionError("partition_not_parquet")
        ref = PartitionRef(
            entry["partition_id"],
            entry["resource_id"],
            entry["feed"],
            entry["row_digest"],
            entry["row_count"],
        )
        return VerifiedPartition(
            ref, path, computed, size, info.st_dev, info.st_ino, info.st_mtime_ns
        )

    # ---- partition rows -----------------------------------------------------------------

    def read_partition(self, partition_id: str, *, start: int, limit: int) -> PartitionPage:
        verified = self._verified.get(partition_id)
        if verified is None:
            raise ExtractionError("partition_unknown")
        if self._decoder is None:
            # Blocked: decoding Parquet rows needs a library that is not a locked dependency.
            raise ExtractionError("parquet_reader_unavailable")
        handle, info = self._open(verified.path)
        with handle:
            identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if identity != (
                verified.device,
                verified.inode,
                verified.byte_size,
                verified.mtime_ns,
            ):
                raise ExtractionError("partition_mutated")
            handle.seek(0)
            page = self._decoder.decode(handle, size=verified.byte_size, start=start, limit=limit)
            if len(page.rows) > limit:
                raise ExtractionError("page_over_limit")
            if page.exhausted and self._digest(handle)[0] != verified.sha256:
                # Re-hashed before the terminal page is returned, so a partition whose bytes
                # changed while it was being read can never seal as complete.
                raise ExtractionError("partition_mutated")
        return PartitionPage(partition_id, start, page.rows, page.mapping_revision, page.exhausted)

    # ---- change manifest ----------------------------------------------------------------

    def read_changes(self, feed: str, *, after: int, limit: int) -> ChangePage:
        if not self._feeds:
            raise ExtractionError("manifest_not_loaded")
        if self._change_path is None:
            raise ExtractionError("snapshot_only_source")
        if feed not in self._feeds:
            raise ExtractionError("feed_unknown")
        if not _is_int(after) or after < 0 or not _is_int(limit) or limit < 1:
            raise ExtractionError("invalid_limits")
        log = str(self._change_path)
        seen = self._ledger.change_log(log)
        handle, _ = self._open(self._change_path)
        chain, lines, total = _GENESIS, 0, 0
        heads: dict[str, int] = {}
        entries: list[ChangeEntry] = []
        more = False
        head = after
        with handle:
            while True:
                raw = handle.readline(self._max_line + 1)
                if not raw:
                    break
                if not raw.endswith(b"\n"):
                    if len(raw) > self._max_line:
                        raise ExtractionError("change_manifest_line_over_limit")
                    break  # an append still in progress: not committed, not recorded
                total += len(raw)
                if total > self._max_changes:
                    raise ExtractionError("change_manifest_over_limit")
                line = hashlib.sha256(raw).hexdigest()
                chain = hashlib.sha256((chain + line).encode()).hexdigest()
                lines += 1
                if lines == seen.lines and chain != seen.chain:
                    raise ExtractionError("change_manifest_rewritten")
                try:
                    if lines == 1:
                        self._header(raw)
                        continue
                    entry = self._entry(raw)
                    if entry.feed_revision <= heads.get(entry.feed, 0):
                        raise ExtractionError("change_manifest_out_of_order")
                except ExtractionError as error:
                    # A line already verified cannot become invalid unless it was rewritten.
                    if lines <= seen.lines:
                        raise ExtractionError("change_manifest_rewritten") from error
                    raise
                heads[entry.feed] = entry.feed_revision
                if entry.feed != feed or entry.feed_revision <= after:
                    continue
                if len(entries) < limit:
                    entries.append(entry)
                    head = entry.feed_revision
                else:
                    more = True
        if lines < seen.lines:
            raise ExtractionError("change_manifest_truncated")
        if lines > seen.lines:
            self._ledger.advance_change_log(log, seen, ChangeLogState(lines, chain))
        through = head if more else max(after, heads.get(feed, 0))
        return ChangePage(feed, after, through, tuple(entries))

    @staticmethod
    def _header(raw: bytes) -> None:
        try:
            header = _strict_json(raw)
        except ValueError:
            raise ExtractionError("change_manifest_malformed") from None
        if header != {"format": CHANGE_MANIFEST_FORMAT}:
            raise ExtractionError("change_manifest_format_unsupported")

    def _entry(self, raw: bytes) -> ChangeEntry:
        try:
            body = _strict_json(raw)
        except ValueError:
            raise ExtractionError("change_manifest_malformed") from None
        if type(body) is not dict or set(body) - {"row"} != _ENTRY_KEYS:
            raise ExtractionError("change_manifest_malformed")
        if body["feed"] not in self._feeds:
            raise ExtractionError("change_manifest_malformed")
        if not all(_is_text(body[k]) for k in ("resource_id", "native_id")):
            raise ExtractionError("change_manifest_malformed")
        if not all(_is_int(body[k]) and body[k] >= 1 for k in ("feed_revision", "revision")):
            raise ExtractionError("change_manifest_malformed")
        op = body["op"]
        if op not in CHANGE_OPS:
            raise ExtractionError("change_manifest_malformed")
        row: SourceRow | None = None
        if op == "tombstone":
            if body.get("row") is not None:
                raise ExtractionError("change_manifest_malformed")
        else:
            row = self._row(body.get("row"))
            if row.native_id != body["native_id"] or row.revision != body["revision"]:
                raise ExtractionError("change_manifest_malformed")
        return ChangeEntry(
            body["feed"],
            body["feed_revision"],
            op,
            body["resource_id"],
            body["native_id"],
            body["revision"],
            row,
        )

    @staticmethod
    def _row(value: Any) -> SourceRow:
        if type(value) is not dict:
            raise ExtractionError("change_manifest_malformed")
        keys = set(value)
        if not set(_ROW_REQUIRED) <= keys <= set(_ROW_REQUIRED) | set(_ROW_OPTIONAL):
            raise ExtractionError("change_manifest_malformed")
        for key, kind in _ROW_REQUIRED.items():
            if type(value[key]) is not kind or (kind is str and not value[key]):
                raise ExtractionError("change_manifest_malformed")
        for key, kind in _ROW_OPTIONAL.items():
            if key in value and value[key] is not None and type(value[key]) is not kind:
                raise ExtractionError("change_manifest_malformed")
        return SourceRow(**value)
