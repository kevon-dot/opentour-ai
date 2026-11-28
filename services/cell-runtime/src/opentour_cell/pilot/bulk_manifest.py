"""OPE-519 first route: a customer-controlled partition manifest plus an append-only change log.

`ManifestSource` implements the `SnapshotSource` port of `bulk_extraction` over files under
one approved root:

* a JSON partition manifest naming immutable Parquet partition files, each with an ID, a
  declared file SHA-256, byte size, row count and row-chain digest;
* a companion NDJSON change manifest of revisioned upserts, reopens and tombstones, carried
  by a change feed whose stable identity the manifest declares (`change_feed`).

Nothing a manifest says about itself is taken as proof. The reader computes every file's
SHA-256 from its bytes and records it, the first time a partition ID is seen, in the source
history that lives IN the extraction register (`bulk_extraction.SourceHistory`), scoped by
tenant and source root. The engine binds that history into the source; the caller cannot
supply, swap or reset it, and a source with no bound history reads nothing
(`source_history_unbound`). The same ID with different bytes is a hard error
(`partition_mutated`), even when the manifest declares the new digest consistently. A
manifest revision rewritten in place is refused. The change manifest is verified as
append-only by a rolling digest over every committed line, keyed by the declared feed
identity and never by the log's path: renaming the log continues the same chain and must
reproduce committed history exactly, and a new identity is refused unless the manifest
declares it the explicit successor of the current one. A malformed line refuses the read
rather than being skipped.

Every file is opened by walking its path one component at a time from a descriptor on the
approved root, with `O_NOFOLLOW` on every component, so no symlink is ever followed and there
is no gap between checking a path and using it. The opened file must be a regular file with
exactly one link on the root's device: a hard link to a file outside the root is refused.

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
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import IO, Any, Protocol

from opentour_cell.pilot.bulk_extraction import (
    ChangeEntry,
    ChangeLogState,
    ChangePage,
    ExtractionError,
    PartitionPage,
    PartitionRef,
    SourceCapability,
    SourceHistory,
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
_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_LINK_ERRNOS = frozenset({errno.ELOOP, errno.ENOTDIR, errno.EMLINK})

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
        "change_feed",
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


def _feed_declaration(value: Any) -> tuple[str, str | None] | None:
    """`{"id": ...}` or `{"id": ..., "succeeds": <the current identity>}`; None if absent."""
    if value is None:
        return None
    if type(value) is not dict or not {"id"} <= set(value) <= {"id", "succeeds"}:
        raise ExtractionError("manifest_invalid")
    feed_id, succeeds = value["id"], value.get("succeeds")
    if not _is_text(feed_id) or not (succeeds is None or _is_text(succeeds)):
        raise ExtractionError("manifest_invalid")
    if succeeds == feed_id:
        raise ExtractionError("manifest_invalid")
    return feed_id, succeeds


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
    ctime_ns: int


# ---- the source --------------------------------------------------------------------------


class ManifestSource:
    """A `SnapshotSource` over a partition manifest and its append-only change manifest."""

    def __init__(
        self,
        root: Path,
        manifest_path: str,
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
        approved = os.stat(self._root)
        self._root_id = (approved.st_dev, approved.st_ino)
        self._manifest_path = _safe_relative(manifest_path)
        self._history: SourceHistory | None = None
        self._decoder = decoder
        self._max_manifest = max_manifest_bytes
        self._max_changes = max_change_manifest_bytes
        self._max_line = max_line_bytes
        self._verified: dict[str, VerifiedPartition] = {}
        self._mapping = ""
        self._change_path: PurePosixPath | None = None
        self._change_identity: tuple[int, int] | None = None
        self._feed_id: str | None = None
        self._feeds: frozenset[str] = frozenset()

    # ---- the history binding -------------------------------------------------------------

    def history_scope(self) -> str:
        """What the register scopes this source's history by: its approved root."""
        return "manifest-root:" + str(self._root)

    def bind_history(self, history: SourceHistory) -> None:
        """Called by the engine with its own register's history; never re-bound elsewhere."""
        if self._history is not None and self._history.binding != history.binding:
            raise ExtractionError("source_history_conflict")
        self._history = history

    @property
    def history(self) -> SourceHistory:
        if self._history is None:
            raise ExtractionError("source_history_unbound")
        return self._history

    # ---- files ---------------------------------------------------------------------------

    @staticmethod
    def _open_at(name: str, flags: int, directory: int) -> int:
        try:
            return os.open(name, flags, dir_fd=directory)
        except FileNotFoundError:
            raise ExtractionError("source_file_missing") from None
        except OSError as error:
            # ELOOP is a refused symlink (no component is ever followed); ENOTDIR a file used
            # as a directory. Anything else is an I/O failure the engine may retry.
            if error.errno in _LINK_ERRNOS:
                raise ExtractionError("partition_path_invalid") from None
            raise ExtractionError("source_unavailable") from None

    def _open(self, relative: PurePosixPath) -> tuple[IO[bytes], os.stat_result]:
        """Open a file under the root by descriptor, one component at a time, no symlinks."""
        try:
            root = os.open(self._root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError:
            raise ExtractionError("source_unavailable") from None
        opened = [root]
        try:
            root_info = os.fstat(root)
            if (root_info.st_dev, root_info.st_ino) != self._root_id:
                raise ExtractionError("source_root_changed")
            for part in relative.parts[:-1]:
                opened.append(self._open_at(part, _DIR_FLAGS, opened[-1]))
                if os.fstat(opened[-1]).st_dev != root_info.st_dev:
                    raise ExtractionError("partition_path_invalid")
            fd = self._open_at(relative.parts[-1], _FILE_FLAGS, opened[-1])
        finally:
            for directory in opened:
                os.close(directory)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_dev != root_info.st_dev:
                raise ExtractionError("partition_path_invalid")
            if info.st_nlink != 1:
                # Another name for the same inode may sit outside the root: refused outright.
                raise ExtractionError("source_file_linked")
            handle = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
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
        history = self.history
        handle, _ = self._open(self._manifest_path)
        with handle:
            raw = handle.read(self._max_manifest + 1)
        if len(raw) > self._max_manifest:
            raise ExtractionError("manifest_over_limit")
        try:
            body = _strict_json(raw)
        except ValueError:
            raise ExtractionError("manifest_invalid") from None
        revision, mapping, capability, high_water, entries, change, feed = self._shape(body)
        digest = hashlib.sha256(raw).hexdigest()
        known = history.manifest(revision)
        if known is not None and known != digest:
            raise ExtractionError("manifest_revision_rewritten")
        shaped = [self._entry_shape(entry, high_water) for entry in entries]
        ids = [entry["partition_id"] for entry, _ in shaped]
        paths = [path for _, path in shaped]
        if len(set(ids)) != len(ids) or len(set(paths)) != len(paths):
            raise ExtractionError("manifest_invalid")
        if change is not None and change in paths + [self._manifest_path]:
            raise ExtractionError("manifest_invalid")
        verified = [self._verify(entry, path, history) for entry, path in shaped]
        change_identity = None
        if change is not None:
            # The change log gets the same opened-descriptor checks as a partition, and its
            # identity is pinned so a replaced file is refused rather than re-read.
            log, info = self._open(change)
            log.close()
            change_identity = (info.st_dev, info.st_ino)
        # Recorded only after every partition verified, and re-checked inside the commit.
        history.record_manifest(
            revision,
            digest,
            [(v.ref.partition_id, v.sha256, v.byte_size) for v in verified],
            feed,
        )
        self._verified = {v.ref.partition_id: v for v in verified}
        self._mapping = mapping
        self._change_path = change
        self._change_identity = change_identity
        self._feed_id = None if feed is None else feed[0]
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
        tuple[str, str | None] | None,
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
        feed = _feed_declaration(body["change_feed"])
        # A source without a change log is snapshot-only and must not name a change feed; a
        # source that claims a change log must name the file that carries it AND the stable
        # identity of the feed, which the history keys its append-only chain by.
        if capability.change_log != (change is not None) or (change is None) != (feed is None):
            raise ExtractionError("manifest_invalid")
        return (
            revision,
            mapping,
            capability,
            dict(high_water),
            entries,
            None if change is None else _safe_relative(change),
            feed,
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

    def _verify(
        self, entry: dict[str, Any], path: PurePosixPath, history: SourceHistory
    ) -> VerifiedPartition:
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
        recorded = history.partition(entry["partition_id"])
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
            computed,
        )
        return VerifiedPartition(
            ref,
            path,
            computed,
            size,
            info.st_dev,
            info.st_ino,
            info.st_mtime_ns,
            info.st_ctime_ns,
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
            # ctime as well as mtime: an in-place edit that restores the mtime cannot restore the
            # ctime, so the file is failed here rather than surfacing only as a row mismatch.
            identity = (
                info.st_dev,
                info.st_ino,
                info.st_size,
                info.st_mtime_ns,
                info.st_ctime_ns,
            )
            if identity != (
                verified.device,
                verified.inode,
                verified.byte_size,
                verified.mtime_ns,
                verified.ctime_ns,
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
        if self._change_path is None or self._feed_id is None:
            raise ExtractionError("snapshot_only_source")
        if feed not in self._feeds:
            raise ExtractionError("feed_unknown")
        if not _is_int(after) or after < 0 or not _is_int(limit) or limit < 1:
            raise ExtractionError("invalid_limits")
        seen = self.history.change_feed(self._feed_id)
        handle, info = self._open(self._change_path)
        if (info.st_dev, info.st_ino) != self._change_identity:
            handle.close()
            raise ExtractionError("change_manifest_replaced")
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
            self.history.advance_change_feed(self._feed_id, seen, ChangeLogState(lines, chain))
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
