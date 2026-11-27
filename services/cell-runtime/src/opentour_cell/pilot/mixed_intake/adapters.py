"""Two read-only source adapters over one manifest model.

* `LocalDirectoryAdapter`: a customer-local directory or export described by a manifest file.
* `McpResourceAdapter`: a paginated, read-only MCP resource server reached through a transport.

Both expose the same declared capability matrix and the same `SourceAdapter` surface. Neither
ever opens a shortcut target, and neither returns a name or content for a provider-denied child.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from opentour_cell.pilot.mixed_intake.model import MANIFEST_VERSION, Json, digest

ADAPTER_REVISIONS = {
    "local-directory": "opentour.adapter.local-directory@1.0.0",
    "mcp-resource": "opentour.adapter.mcp-resource@1.0.0",
}
KINDS = frozenset({"folder", "file", "native", "shortcut"})


class AdapterError(Exception):
    """A safe adapter diagnostic. Never carries names or content of a restricted object."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class AdapterUnavailable(AdapterError):
    """The route is not available. It must never block the base starter paths."""

    def __init__(self) -> None:
        super().__init__("adapter_unavailable")


class ShortcutBlocked(AdapterError):
    def __init__(self) -> None:
        super().__init__("shortcut_not_followed")


@dataclass(frozen=True)
class Capabilities:
    """Declared per adapter. A snapshot-only source reports that limit explicitly."""

    listing: bool = True
    read: bool = True
    version: bool = True
    pagination: bool = True
    delta: bool = False
    deletion: bool = False
    permission: bool = True
    rendition: bool = False

    @property
    def snapshot_only(self) -> bool:
        return not self.delta

    def matrix(self) -> Json:
        return {
            "list": self.listing,
            "read": self.read,
            "version": self.version,
            "pagination": self.pagination,
            "delta": self.delta,
            "deletion": self.deletion,
            "permission": self.permission,
            "approved_rendition": self.rendition,
            "snapshot_only": self.snapshot_only,
        }

    def limits_reported(self) -> list[str]:
        limits = []
        if self.snapshot_only:
            limits.append("snapshot_only:no_delta_feed")
        if not self.deletion:
            limits.append("no_deletion_feed:disappearance_needs_complete_reconciliation")
        if not self.rendition:
            limits.append("no_approved_rendition")
        return limits


@dataclass(frozen=True)
class RenditionOffer:
    format: str
    revision: str


@dataclass(frozen=True)
class Resource:
    id: str
    kind: str
    name: str | None
    parent_id: str | None
    revision: str
    access: str = "granted"
    opaque: bool = False
    path: str | None = None
    size: int | None = None
    target_id: str | None = None
    renditions: tuple[RenditionOffer, ...] = ()
    original_revision: str | None = None
    original_digest: str | None = None


@dataclass(frozen=True)
class Page:
    resources: tuple[Resource, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class Tombstone:
    id: str
    revision: str


@dataclass(frozen=True)
class ChangePage:
    upserts: tuple[Resource, ...]
    tombstones: tuple[Tombstone, ...]
    next_token: str
    complete: bool


@dataclass(frozen=True)
class ConnectorInfo:
    tenant: str
    connection: str
    root_id: str
    access_epoch: int
    as_of: str
    capabilities: Capabilities
    transport: str


@dataclass(frozen=True)
class RenditionBytes:
    data: bytes
    revision: str


@dataclass
class Counters:
    """Grant/read counters for evidence. Content reads are counted per resource id."""

    list_calls: int = 0
    content_reads: dict[str, int] = field(default_factory=dict)
    rendition_reads: dict[str, int] = field(default_factory=dict)
    shortcut_target_reads: int = 0
    blocked_shortcut_attempts: int = 0

    def total_reads(self) -> int:
        return sum(self.content_reads.values()) + sum(self.rendition_reads.values())


class SourceAdapter(Protocol):
    counters: Counters

    def describe(self) -> ConnectorInfo: ...

    def list_children(self, parent_id: str, cursor: str | None, limit: int) -> Page: ...

    def read(self, resource_id: str, revision: str) -> bytes: ...

    def read_rendition(self, resource_id: str, fmt: str) -> RenditionBytes: ...

    def changes(self, token: str | None) -> ChangePage: ...


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(f"c{offset}".encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        assert raw.startswith("c")
        offset = int(raw[1:])
        assert offset >= 0
        return offset
    except (ValueError, AssertionError, binascii.Error) as error:
        raise AdapterError("invalid_cursor") from error


def _capabilities(value: Json) -> Capabilities:
    names = {
        "list",
        "read",
        "version",
        "pagination",
        "delta",
        "deletion",
        "permission",
        "approved_rendition",
    }
    if not set(value) >= names - {"approved_rendition"}:
        raise AdapterError("invalid_capabilities")
    return Capabilities(
        **{
            {"approved_rendition": "rendition", "list": "listing"}.get(k, k): bool(value[k])
            for k in names
            if k in value
        }
    )


# ------------------------------------------------------------------------------- local


class LocalDirectoryAdapter:
    """Customer-local directory/export described by an owner-supplied manifest.

    Every file is opened through one guarded door that refuses symlinks and any path that
    resolves outside the directory, so a shortcut can never be read through this adapter.
    """

    def __init__(self, manifest_path: Path, root: Path):
        try:
            self.manifest: Json = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise AdapterUnavailable() from error
        if self.manifest.get("schema_version") != MANIFEST_VERSION:
            raise AdapterError("invalid_manifest")
        self.root = root
        self.counters = Counters()
        self._nodes: dict[str, Json] = {n["id"]: n for n in self.manifest["nodes"]}
        self._capabilities = _capabilities(self.manifest["capabilities"])

    def describe(self) -> ConnectorInfo:
        m = self.manifest
        return ConnectorInfo(
            m["tenant"],
            m["connection"],
            m["root_id"],
            int(m["access_epoch"]),
            m["as_of"],
            self._capabilities,
            "customer-local-directory",
        )

    @staticmethod
    def _resource(node: Json) -> Resource:
        denied = node.get("access", "granted") != "granted"
        opaque = bool(node.get("opaque")) and denied
        return Resource(
            node["id"],
            "unknown" if opaque else node["kind"],
            None if opaque else node.get("name"),
            node.get("parent"),
            node.get("revision", ""),
            "denied" if denied else "granted",
            opaque,
            None if opaque else node.get("path"),
            None if opaque else node.get("size"),
            None if opaque else node.get("target"),
            tuple(RenditionOffer(r["format"], r["revision"]) for r in node.get("renditions", ())),
            (node.get("native") or {}).get("revision"),
            (node.get("native") or {}).get("digest"),
        )

    def list_children(self, parent_id: str, cursor: str | None, limit: int) -> Page:
        self.counters.list_calls += 1
        children = [n for n in self.manifest["nodes"] if n.get("parent") == parent_id]
        links = self._nodes.get(parent_id, {}).get("links", ())
        children += [self._nodes[i] for i in links if i in self._nodes]
        start = _decode_cursor(cursor)
        chunk = children[start : start + limit]
        more = start + limit < len(children)
        return Page(
            tuple(self._resource(n) for n in chunk),
            _encode_cursor(start + limit) if more else None,
        )

    def _open(self, relative: str) -> bytes:
        """The single guarded door to the filesystem."""
        candidate = self.root / relative
        try:
            info = os.lstat(candidate)
        except OSError as error:
            raise AdapterError("object_unavailable") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            self.counters.blocked_shortcut_attempts += 1
            raise ShortcutBlocked()
        resolved = candidate.resolve(strict=True)
        if self.root.resolve(strict=True) not in resolved.parents:
            self.counters.blocked_shortcut_attempts += 1
            raise ShortcutBlocked()
        with open(resolved, "rb") as handle:
            return handle.read()

    def read(self, resource_id: str, revision: str) -> bytes:
        node = self._nodes.get(resource_id)
        if (
            node is None
            or node.get("access", "granted") != "granted"
            or node["kind"] not in ("file", "native")
        ):
            raise AdapterError("object_not_readable")
        if node.get("revision") != revision:
            raise AdapterError("revision_changed")
        self.counters.content_reads[resource_id] = (
            self.counters.content_reads.get(resource_id, 0) + 1
        )
        return self._open(node["path"])

    def read_rendition(self, resource_id: str, fmt: str) -> RenditionBytes:
        node = self._nodes.get(resource_id)
        if node is None or node.get("access", "granted") != "granted":
            raise AdapterError("object_not_readable")
        for rendition in node.get("renditions", ()):
            if rendition["format"] == fmt:
                self.counters.rendition_reads[resource_id] = (
                    self.counters.rendition_reads.get(resource_id, 0) + 1
                )
                return RenditionBytes(self._open(rendition["path"]), rendition["revision"])
        raise AdapterError("rendition_unavailable")

    def changes(self, token: str | None) -> ChangePage:
        feed = self.manifest.get("change_feed")
        if not self._capabilities.delta or feed is None:
            raise AdapterError("delta_not_supported")
        since = int(token or 0)
        events = [e for e in feed["events"] if e["seq"] > since]
        upserts = tuple(self._resource(self._nodes[e["upsert"]]) for e in events if "upsert" in e)
        tombstones = tuple(
            Tombstone(e["tombstone"], e["revision"]) for e in events if "tombstone" in e
        )
        last = max([since, *(e["seq"] for e in events)])
        return ChangePage(upserts, tombstones, str(last), bool(feed.get("complete", True)))


# ------------------------------------------------------------------------------- MCP


class McpTransport(Protocol):
    """A JSON-RPC style request/response transport. The adapter only ever sends read methods."""

    def request(self, method: str, params: Json) -> Json: ...


class McpResourceAdapter:
    """Read-only, paginated MCP resource adapter.

    `resources/list` and `resources/read` are standard MCP methods. Capabilities and the change
    feed use two read-only `opentour/` extension methods because the base protocol has no
    delta or permission-denial vocabulary; a server without them is treated as snapshot-only.
    """

    ALLOWED_METHODS = frozenset(
        {"resources/list", "resources/read", "opentour/capabilities", "opentour/changes"}
    )

    def __init__(self, transport: McpTransport, *, connection: str):
        self.transport = transport
        self.connection = connection
        self.counters = Counters()
        self.methods_called: list[str] = []
        self._info: ConnectorInfo | None = None

    def _call(self, method: str, params: Json) -> Json:
        if method not in self.ALLOWED_METHODS:
            raise AdapterError("write_or_unknown_method_refused")
        self.methods_called.append(method)
        try:
            return self.transport.request(method, params)
        except AdapterError:
            raise
        except (ConnectionError, TimeoutError, OSError) as error:
            raise AdapterUnavailable() from error

    def describe(self) -> ConnectorInfo:
        if self._info is None:
            raw = self._call("opentour/capabilities", {})
            self._info = ConnectorInfo(
                raw["tenant"],
                self.connection,
                raw["root"],
                int(raw["accessEpoch"]),
                raw["asOf"],
                _capabilities(raw["capabilities"]),
                "mcp-resource-generated-transport",
            )
        return self._info

    @staticmethod
    def _resource(item: Json) -> Resource:
        meta: Json = item.get("_meta", {})
        denied = meta.get("access", "granted") != "granted"
        original: Json = meta.get("original") or {}
        return Resource(
            item["uri"],
            meta.get("kind", "file"),
            None if denied else item.get("name"),
            meta.get("parent"),
            meta.get("revision", ""),
            "denied" if denied else "granted",
            bool(meta.get("opaque")) and denied,
            None if denied else meta.get("path"),
            None if denied else item.get("size"),
            None if denied else meta.get("target"),
            tuple(RenditionOffer(r["format"], r["revision"]) for r in meta.get("renditions", ())),
            original.get("revision"),
            original.get("digest"),
        )

    def list_children(self, parent_id: str, cursor: str | None, limit: int) -> Page:
        self.counters.list_calls += 1
        raw = self._call("resources/list", {"parent": parent_id, "cursor": cursor, "limit": limit})
        return Page(tuple(self._resource(item) for item in raw["resources"]), raw.get("nextCursor"))

    @staticmethod
    def _blob(raw: Json) -> tuple[bytes, str]:
        content = raw["contents"][0]
        try:
            return base64.b64decode(content["blob"], validate=True), content["_meta"]["revision"]
        except (KeyError, binascii.Error) as error:
            raise AdapterError("invalid_read_response") from error

    def read(self, resource_id: str, revision: str) -> bytes:
        self.counters.content_reads[resource_id] = (
            self.counters.content_reads.get(resource_id, 0) + 1
        )
        data, got = self._blob(
            self._call("resources/read", {"uri": resource_id, "revision": revision})
        )
        if got != revision:
            raise AdapterError("revision_changed")
        return data

    def read_rendition(self, resource_id: str, fmt: str) -> RenditionBytes:
        self.counters.rendition_reads[resource_id] = (
            self.counters.rendition_reads.get(resource_id, 0) + 1
        )
        data, revision = self._blob(
            self._call("resources/read", {"uri": resource_id, "rendition": fmt})
        )
        return RenditionBytes(data, revision)

    def changes(self, token: str | None) -> ChangePage:
        if not self.describe().capabilities.delta:
            raise AdapterError("delta_not_supported")
        raw = self._call("opentour/changes", {"since": token})
        return ChangePage(
            tuple(self._resource(i) for i in raw["upserts"]),
            tuple(Tombstone(t["uri"], t["revision"]) for t in raw["tombstones"]),
            raw["nextToken"],
            bool(raw.get("complete", True)),
        )


def public_ref(tenant: str, resource_id: str) -> str:
    """Opaque stand-in used wherever a restricted object must not be named."""
    return "opaque-" + digest(["opaque-ref", tenant, resource_id])[:16]
