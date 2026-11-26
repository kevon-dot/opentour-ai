"""Discover and acquire two projected resources from an owner-approved, local MCP reader."""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import select
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, ExitStack, contextmanager
from pathlib import Path
from typing import Any, BinaryIO, NoReturn, cast
from urllib.parse import urlsplit

from opentour_cell.pilot import source_intake as intake
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import MAX_MANIFEST_BYTES, ManifestError, parse_manifest
from opentour_cell.pilot.simple_repair import RepairError, validate_features
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_data import identifier

Json = dict[str, Any]
CONNECTION = "opentour.pilot.workplace-connection.v1"
TRAINING = "opentour.pilot.workplace-training.v1"
RESOURCE = "opentour.pilot.workplace-projection.v1"
META = "ai.opentour/workplace"
PROTOCOL = "2025-11-25"
ROLES = ("trace", "correction")
MAX_PAGES = 8
MAX_RESOURCES = 256
CATALOG_LIMIT = 256 * 1024
ROW_PAYLOAD_LIMIT = 2 * 1024 * 1024
SESSION_SECONDS = 20
_BINDINGS = {"organization", "workflow", "field", "policy_version", "source_schema_version"}
_DERIVED = {
    "schema_version",
    "trace_source",
    "correction_source",
    "trace_sha256",
    "correction_sha256",
}
_PRIVATE_FIELD = re.compile(
    r"(?:^|[_.:-])(?:password|secret|token|email|phone|ssn|medical|diagnosis|salary|"
    r"outcome|reference|verifier|verified|adjudicator|label|target|protected|human_only|"
    r"correction)(?:$|[_.:-])",
    re.I,
)


class WorkplaceError(TrainingError):
    """A fixed, public-safe reason that contains no provider content."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _need(value: object, reason: str) -> None:
    if not value:
        raise WorkplaceError(reason)


def _sha(contents: bytes) -> str:
    return hashlib.sha256(contents).hexdigest()


def _hex(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"[0-9a-f]{64}", value))


def _uri(value: Any, *, namespace: bool = False) -> str:
    _need(isinstance(value, str) and len(value) <= 512, "invalid_workplace_namespace")
    assert isinstance(value, str)
    # The process treats admitted URIs as opaque identifiers inside an authorized namespace.
    valid = re.fullmatch(r"[a-z][a-z0-9+.-]*://[a-z0-9][a-z0-9.-]*/[A-Za-z0-9_./-]+", value)
    _need(valid, "invalid_workplace_namespace")
    parts = urlsplit(value)
    segments = parts.path.strip("/").split("/")
    _need(
        parts.scheme not in {"file", "data", "http", "ftp"}
        and all(s and s not in {".", ".."} for s in segments)
        and len(segments) >= 2
        and parts.path.endswith("/") == namespace,
        "invalid_workplace_namespace",
    )
    return value


def _private_feature(name: str) -> bool:
    words = re.sub(r"(?<=[a-z])(?=[A-Z])", "_", name)
    return bool(_PRIVATE_FIELD.search(words))


def _validate_mapping(role: str, spec: Json, scope: Json) -> None:
    base = {"namespace", "authority", "columns"}
    allowed = base | ({"feature_columns", "policy_context"} if role == "trace" else set())
    _need(isinstance(spec, dict) and base <= spec.keys() <= allowed, "invalid_workplace_mapping")
    _uri(spec["namespace"], namespace=True)
    identifier(spec["authority"])
    cols = spec["columns"]
    required = intake.TRACE - {"features"} if role == "trace" else intake.OUTCOME
    _need(
        isinstance(cols, dict)
        and required <= cols.keys() <= required | ({"slices"} if role == "trace" else set()),
        "invalid_workplace_mapping",
    )
    wires = [identifier(v) for v in cols.values()]
    if role == "trace":
        features = spec.get("feature_columns")
        _need(
            isinstance(features, dict)
            and features
            and set(features) == set(scope["permitted_features"]),
            "invalid_workplace_feature_projection",
        )
        assert isinstance(features, dict)
        validate_features(next(iter(features)), list(features))
        for feature, wire in features.items():
            identifier(wire)
            _need(
                not _private_feature(wire) and not _private_feature(feature),
                "sensitive_workplace_feature",
            )
            wires.append(wire)
        policy = spec.get("policy_context", {})
        _need(isinstance(policy, dict), "invalid_workplace_policy_context")
        terms = {
            f for f in features if re.search(r"(?:^|[_.:-])(?:sop|policy)(?:$|[_.:-])", f, re.I)
        }
        if policy:
            _need(
                set(policy) == {"feature", "effective_at_column"}
                and policy["feature"] in features
                and terms <= {policy["feature"]},
                "invalid_workplace_policy_context",
            )
            wires.append(identifier(policy["effective_at_column"]))
        else:
            _need(not terms, "invalid_workplace_policy_context")
    _need(len(set(wires)) == len(wires), "workplace_projection_alias")


def _projection(role: str, cfg: Json) -> list[str]:
    spec = cfg["roles"][role]
    wires = list(spec["columns"].values()) + list(spec.get("feature_columns", {}).values())
    if spec.get("policy_context"):
        wires.append(spec["policy_context"]["effective_at_column"])
    return sorted(wires)


def _source_grant(cfg: Json, selected: Json, hashes: Json) -> Json:
    return {
        **cfg["scope"],
        "schema_version": intake.GRANT_VERSION,
        **{role + "_source": selected[role]["source_id"] for role in ROLES},
        **{role + "_sha256": hashes[role] for role in ROLES},
    }


def _validated_grant(store: LocalTrainingStore, grant: Json) -> Json:
    # Reuse the original source intake's authority, split and withdrawal validation before I/O.
    with tempfile.NamedTemporaryFile(dir=store.path.parent, prefix=".workplace-scope-") as f:
        f.write(canonical(grant).encode())
        f.flush()
        return intake._scope(store, Path(f.name))


def _connection(store: LocalTrainingStore, connection: Path) -> Json:
    _need(connection.parent == store.path.parent, "workplace_connection_not_local")
    cfg = parse_manifest(intake._read(connection, 64 * 1024))
    _need(
        set(cfg) - {"watermark_mode"}
        == {
            "schema_version",
            "reader",
            "scope",
            "roles",
            "purposes",
            "destination",
            "data_location",
        }
        and ("watermark_mode" not in cfg or cfg["watermark_mode"] == "provider_verified")
        and cfg["schema_version"] == CONNECTION
        and cfg["purposes"] == ["decision_context", "independent_verification"]
        and cfg["destination"] == "owner_local"
        and cfg["data_location"] == "owner_local",
        "workplace_connection_scope_mismatch",
    )
    scope, roles, reader = cfg["scope"], cfg["roles"], cfg["reader"]
    _need(
        isinstance(scope, dict) and set(scope) == intake.SCOPE - _DERIVED,
        "workplace_connection_scope_mismatch",
    )
    _need(isinstance(roles, dict) and set(roles) == set(ROLES), "invalid_workplace_mapping")
    # Validate the scope before even inspecting the reader. The identities and digests
    # will be replaced by the discovered, uniquely matching pair on admission.
    _validated_grant(
        store,
        _source_grant(cfg, {r: {"source_id": r} for r in ROLES}, dict.fromkeys(ROLES, "0" * 64)),
    )
    for role in ROLES:
        _validate_mapping(role, roles[role], scope)
    left, right = [roles[r] for r in ROLES]
    _need(
        left["authority"] != right["authority"]
        and not left["namespace"].startswith(right["namespace"])
        and not right["namespace"].startswith(left["namespace"]),
        "independent_workplace_authority_required",
    )
    _need(
        isinstance(reader, dict)
        and set(reader) == {"provider", "executable", "sha256", "env_names"}
        and _hex(reader["sha256"]),
        "invalid_workplace_reader",
    )
    identifier(reader["provider"])
    names = reader["env_names"]
    _need(
        isinstance(names, list)
        and len(names) <= 8
        and all(
            isinstance(n, str) and re.fullmatch(r"OPENTOUR_WORKPLACE_[A-Z][A-Z0-9_]{0,60}", n)
            for n in names
        )
        and len(set(names)) == len(names),
        "invalid_workplace_environment",
    )
    _need(
        isinstance(reader["executable"], str) and len(reader["executable"]) <= 1024,
        "invalid_workplace_reader",
    )
    exe = Path(reader["executable"])
    _need(exe.is_absolute() and exe == exe.resolve(), "invalid_workplace_reader")
    return cfg


@contextmanager
def _launcher(reader: Json) -> Iterator[BinaryIO]:
    # Hold the verified inode across exec, including the interpreter's read of a script.
    with os.fdopen(
        os.open(reader["executable"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
    ) as file:
        info = os.fstat(file.fileno())
        _need(
            stat.S_ISREG(info.st_mode)
            and info.st_uid in {0, os.getuid()}
            and not info.st_mode & 0o022
            and info.st_mode & 0o111
            and info.st_size <= 32 * 1024 * 1024
            and hashlib.file_digest(file, "sha256").hexdigest() == reader["sha256"],
            "workplace_reader_identity_mismatch",
        )
        file.seek(0)
        yield file


class _Stdio:
    def __init__(
        self,
        child: subprocess.Popen[bytes],
        deadline: float,
        dispatch: Callable[[], AbstractContextManager[None]],
    ):
        self.child = child
        self.deadline = deadline
        self.pending = bytearray()
        self.number = 0
        self.dispatch = dispatch
        assert child.stdin is not None
        os.set_blocking(child.stdin.fileno(), False)

    def send(self, message: Json) -> None:
        assert self.child.stdin is not None
        payload = (canonical({"jsonrpc": "2.0", **message}) + "\n").encode()
        with self.dispatch():
            while payload:
                remaining = self.deadline - time.monotonic()
                _need(
                    remaining > 0
                    and select.select([], [self.child.stdin], [], max(0, remaining))[1],
                    "workplace_reader_timeout",
                )
                try:
                    sent = os.write(self.child.stdin.fileno(), payload)
                except BlockingIOError:
                    continue
                _need(sent, "workplace_reader_disconnected")
                payload = payload[sent:]

    def receive(self, limit: int) -> Json:
        assert self.child.stdout is not None
        while b"\n" not in self.pending:
            _need(len(self.pending) < limit, "workplace_response_too_large")
            remaining = self.deadline - time.monotonic()
            _need(
                remaining > 0 and select.select([self.child.stdout], [], [], max(0, remaining))[0],
                "workplace_reader_timeout",
            )
            chunk = os.read(self.child.stdout.fileno(), min(65536, limit + 1 - len(self.pending)))
            _need(chunk, "workplace_reader_disconnected")
            self.pending.extend(chunk)
        line, _, self.pending = self.pending.partition(b"\n")
        _need(len(line) <= limit, "workplace_response_too_large")
        return parse_manifest(bytes(line))

    def call(self, method: str, params: Json, *, limit: int = CATALOG_LIMIT) -> Json:
        self.number += 1
        self.send({"id": self.number, "method": method, "params": params})
        for _ in range(16):
            frame = self.receive(limit)
            _need(frame.get("jsonrpc") == "2.0", "invalid_workplace_protocol")
            if (
                "id" not in frame
                and isinstance(frame.get("method"), str)
                and frame["method"].startswith("notifications/")
            ):
                continue
            _need(
                type(frame.get("id")) is int
                and frame["id"] == self.number
                and isinstance(frame.get("result"), dict)
                and "error" not in frame
                and "method" not in frame,
                "workplace_request_denied",
            )
            return cast(Json, frame["result"])
        raise WorkplaceError("workplace_notification_limit")


@contextmanager
def _session(
    cfg: Json, root: Path, dispatch: Callable[[], AbstractContextManager[None]]
) -> Iterator[_Stdio]:
    reader = cfg["reader"]
    _need(
        all(n in os.environ for n in reader["env_names"]),
        "workplace_connection_credentials_unavailable",
    )
    with _launcher(reader) as launcher, ExitStack() as session:
        with dispatch():
            child = session.enter_context(
                subprocess.Popen(
                    [reader["executable"]],
                    executable=f"/dev/fd/{launcher.fileno()}",
                    pass_fds=(launcher.fileno(),),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    cwd=root,
                    env={n: os.environ[n] for n in reader["env_names"]},
                )
            )
        try:
            client = _Stdio(child, time.monotonic() + SESSION_SECONDS, dispatch)
            hello = client.call(
                "initialize",
                {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {},
                    "clientInfo": {"name": "opentour-workplace", "version": "1"},
                },
            )
            _need(
                hello.get("protocolVersion") in {PROTOCOL, "2025-06-18", "2025-03-26", "2024-11-05"}
                and isinstance(hello.get("capabilities"), dict)
                and isinstance(hello["capabilities"].get("resources"), dict)
                and isinstance(hello.get("serverInfo"), dict)
                and hello["serverInfo"].get("name") == reader["provider"],
                "workplace_provider_mismatch",
            )
            client.send({"method": "notifications/initialized"})
            yield client
        finally:
            # MCP stdio shutdown starts with EOF. Escalate only for this exact child,
            # preserving the operator's process supervisor and its entire process group.
            assert child.stdin is not None
            child.stdin.close()
            try:
                child.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                child.terminate()
                try:
                    child.wait(timeout=0.2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()


def _candidate(item: Any, cfg: Json) -> tuple[str, Json] | None:
    _need(isinstance(item, dict) and isinstance(item.get("uri"), str), "invalid_workplace_catalog")
    uri = item["uri"]
    role = next((r for r in ROLES if uri.startswith(cfg["roles"][r]["namespace"])), None)
    if role is None:
        return None
    _uri(uri)
    meta = item.get("_meta", {})
    _need(isinstance(meta, dict), "invalid_workplace_catalog")
    tags = meta.get(META)
    if not isinstance(tags, dict) or item.get("mimeType") != "application/json":
        return None
    expected = {k: cfg["scope"][k] for k in _BINDINGS}
    expected.update(
        role=role,
        authority=cfg["roles"][role]["authority"],
        provider=cfg["reader"]["provider"],
        schema_version=RESOURCE,
        policy_status="published",
    )
    if any(tags.get(k) != v for k, v in expected.items()):
        return None
    if cfg.get("watermark_mode") == "provider_verified" or "covered_through" in tags:
        horizon = tags.get("covered_through")
        _need(
            type(horizon) is int
            and cfg["scope"]["as_of"] <= horizon <= time.time()
            and horizon < cfg["scope"]["expires_at"],
            "workplace_watermark_unverified",
        )
        expected["covered_through"] = horizon
    if tags.get("columns") != _projection(role, cfg):
        return None
    identifier(tags.get("source_id"))
    identifier(tags.get("version"))
    count = tags.get("row_count")
    _need(type(count) is int and 0 <= count <= intake.MAX_ROWS, "workplace_row_limit")
    return role, {
        **expected,
        "uri": uri,
        "source_id": tags["source_id"],
        "version": tags["version"],
        "columns": tags["columns"],
        "row_count": count,
    }


def _catalog(client: _Stdio, cfg: Json, max_total_rows: int) -> tuple[Json, Json]:
    _need(
        type(max_total_rows) is int and 1 <= max_total_rows <= intake.MAX_ROWS * 2,
        "workplace_row_limit",
    )
    candidates: dict[str, list[Json]] = {r: [] for r in ROLES}
    seen: set[str] = set()
    cursors: set[str] = set()
    cursor = None
    for _ in range(MAX_PAGES):
        page = client.call("resources/list", {"cursor": cursor} if cursor is not None else {})
        items = page.get("resources")
        _need(
            isinstance(items, list) and len(seen) + len(items) <= MAX_RESOURCES,
            "workplace_catalog_limit",
        )
        assert isinstance(items, list)
        for item in items:
            match = _candidate(item, cfg)
            _need(item["uri"] not in seen, "workplace_catalog_duplicate")
            seen.add(item["uri"])
            if match:
                candidates[match[0]].append(match[1])
        cursor = page.get("nextCursor")
        if cursor is None:
            break
        _need(
            isinstance(cursor, str)
            and 0 < len(cursor) <= 256
            and cursor.isprintable()
            and cursor not in cursors,
            "workplace_catalog_cursor_invalid",
        )
        cursors.add(cursor)
    else:
        raise WorkplaceError("workplace_catalog_limit")
    _need(all(candidates.values()), "workplace_resource_pair_missing")
    _need(all(len(rows) == 1 for rows in candidates.values()), "ambiguous_resource_pair")
    pair = {r: rows[0] for r, rows in candidates.items()}
    _need(
        pair["trace"]["source_id"] != pair["correction"]["source_id"],
        "independent_workplace_authority_required",
    )
    counts = {r + "_rows": pair[r]["row_count"] for r in ROLES}
    _need(sum(counts.values()) <= max_total_rows, "workplace_row_limit")
    # Unrelated catalog entries never enter identity, logging, or a future read request.
    pair_revision = digest({"connection": digest(cfg), "resources": pair})
    summary = {
        "state": "ready",
        "catalog_revision": digest({"scope": digest(cfg["roles"]), "matches": pair}),
        "pair_revision": pair_revision,
        "counts": counts,
        "sources": {r: {k: pair[r][k] for k in ("source_id", "version")} for r in ROLES},
    }
    return pair, summary


def _read_rows(client: _Stdio, cfg: Json, role: str, chosen: Json) -> tuple[bytes, str]:
    reply = client.call("resources/read", {"uri": chosen["uri"]}, limit=MAX_MANIFEST_BYTES)
    contents = reply.get("contents")
    _need(
        isinstance(contents, list) and len(contents) == 1 and isinstance(contents[0], dict),
        "invalid_workplace_resource",
    )
    assert isinstance(contents, list)
    part = contents[0]
    _need(
        part.keys() <= {"uri", "mimeType", "text", "_meta"}
        and part.get("uri") == chosen["uri"]
        and part.get("mimeType") == "application/json"
        and isinstance(part.get("text"), str),
        "workplace_resource_identity_mismatch",
    )
    raw = part["text"].encode()
    _need(len(raw) <= ROW_PAYLOAD_LIMIT, "workplace_response_too_large")
    envelope = parse_manifest(raw)
    _need(
        set(envelope) == {"projection", "rows"}
        and envelope["projection"] == {k: v for k, v in chosen.items() if k != "uri"},
        "workplace_resource_version_mismatch",
    )
    rows = envelope["rows"]
    _need(
        isinstance(rows, list) and len(rows) == chosen["row_count"],
        "workplace_resource_count_mismatch",
    )
    normalized = []
    spec = cfg["roles"][role]
    for row in rows:
        _need(
            isinstance(row, dict) and set(row) == set(chosen["columns"]),
            "workplace_unapproved_fields",
        )
        result = {dst: row[src] for dst, src in spec["columns"].items()}
        _need(
            all(result[k] == cfg["scope"][k] for k in ("organization", "workflow", "field"))
            and result["source"] == chosen["source_id"],
            "workplace_row_scope_mismatch",
        )
        if role == "trace":
            result["features"] = {f: row[c] for f, c in spec["feature_columns"].items()}
            if policy := spec.get("policy_context"):
                # A later SOP revision is unavailable at decision time and is excluded by intake.
                result["features_available_at"] = max(
                    intake._time(result["features_available_at"]),
                    intake._time(row[policy["effective_at_column"]]),
                )
        normalized.append(canonical(result))
    output = "".join(line + "\n" for line in sorted(normalized)).encode()
    _need(len(output) <= MAX_MANIFEST_BYTES, "workplace_response_too_large")
    return output, _sha(raw)


def _mkdir(path: Path) -> None:
    path.mkdir(mode=0o700, exist_ok=True)
    info = path.lstat()
    _need(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid == os.getuid()
        and not info.st_mode & 0o077
        and path.resolve() == path,
        "workplace_storage_unavailable",
    )


def _save_export(path: Path, data: bytes) -> None:
    with os.fdopen(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "wb") as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_DIRECTORY | os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _training_right(
    store: LocalTrainingStore, path: Path, grant: Json, snapshot_revision: str
) -> Json:
    _need(path.parent == store.path.parent, "workplace_training_grant_not_local")
    right = parse_manifest(intake._read(path, 4096))
    _need(
        set(right)
        == {
            "schema_version",
            "organization",
            "engagement",
            "owner_id",
            "owner_attestation",
            "snapshot_revision",
            "training_attestation",
            "training_expires_at",
        }
        and right["schema_version"] == TRAINING
        and right["snapshot_revision"] == snapshot_revision
        and all(
            right[k] == grant[k]
            for k in ("organization", "engagement", "owner_id", "owner_attestation")
        )
        and type(right["training_expires_at"]) is int
        and time.time() < right["training_expires_at"] <= grant["expires_at"]
        and intake._attest(right["training_attestation"])
        and right["training_attestation"] != grant["owner_attestation"],
        "stale_workplace_training_confirmation",
    )
    _need(
        not os.path.lexists(intake._revocation(store, right["training_attestation"])),
        "workplace_training_grant_withdrawn",
    )
    return {k: right[k] for k in ("training_attestation", "training_expires_at")}


def _live(store: LocalTrainingStore, path: Path, expected: str | None) -> Json:
    cfg = _connection(store, path)
    if expected is not None:
        _need(_hex(expected) and digest(cfg) == expected, "workplace_connection_changed")
    return cfg


def _snapshot(
    store: LocalTrainingStore,
    context: Path,
    cfg: Json,
    selected: Json,
    summary: Json,
    rows: dict[str, bytes],
    wire_hashes: Json,
    timing: Json,
    training: Path | None,
) -> Json:
    directory = store.path.parent
    hashes = {r: _sha(rows[r]) for r in ROLES}
    grant = _source_grant(cfg, selected, hashes)
    if cfg.get("watermark_mode") == "provider_verified":
        grant["as_of"] = min(selected[r]["covered_through"] for r in ROLES)
    pair_revision = summary["pair_revision"]
    identity = {
        "schema_version": "opentour.pilot.workplace-snapshot.v1",
        "pair_revision": pair_revision,
        "connection_revision": digest(cfg),
        "normalized_sha256": hashes,
    }
    snapshot_revision = digest(identity)
    with store.engagement_guard(engagement_id=cfg["scope"]["engagement"]):
        _live(store, context, digest(cfg))
        if training is not None:
            grant.update(_training_right(store, training, grant, snapshot_revision))
        grant = _validated_grant(store, grant)
        snapshots = directory / "workplace-snapshots"
        _mkdir(snapshots)
        location = snapshots / snapshot_revision
        with tempfile.TemporaryDirectory(dir=snapshots, prefix=".fetch-") as tmp:
            staging = Path(tmp)
            for role in ROLES:
                _save_export(staging / (role + ".jsonl"), rows[role])
            joined = intake._build(grant, staging / "trace.jsonl", staging / "correction.jsonl")
            discovery = intake._discovery(joined)
            _need(
                discovery["development_count"] + discovery["protected_count"] > 0,
                "workplace_no_verified_pairs",
            )
            counts = {
                **summary["counts"],
                "development_rows": discovery["development_count"],
                "protected_rows": discovery["protected_count"],
                "excluded_counts": discovery["excluded_counts"],
                "unmatched_outcomes": len(joined["unmatched_outcome_ids"]),
                "duplicates": {r: joined["sources"][r]["duplicate_rows"] for r in ROLES},
            }
            # Each declared source version is immutable, even when its paired resource changes.
            bindings = []
            for role in ROLES:
                descriptor = selected[role]
                binding = {
                    k: descriptor[k]
                    for k in (
                        "organization",
                        "provider",
                        "authority",
                        "source_id",
                        "uri",
                        "version",
                        "columns",
                    )
                }
                version_file = snapshots / ("version-" + digest(binding) + ".json")
                record = {
                    "resource_binding": digest(binding),
                    "descriptor_revision": digest(descriptor),
                    "normalized_sha256": hashes[role],
                }
                if os.path.lexists(version_file):
                    _need(
                        parse_manifest(intake._read(version_file, 1024)) == record,
                        "workplace_version_content_changed",
                    )
                bindings.append((version_file, record))
            receipt = {
                **identity,
                "snapshot_revision": snapshot_revision,
                "resources": selected,
                "wire_sha256": wire_hashes,
                "fetch": timing,
                "counts": counts,
            }
            if os.path.lexists(location):
                _mkdir(location)
                saved = parse_manifest(intake._read(location / "receipt.json", 32 * 1024))
                _need(
                    all(
                        saved.get(k) == receipt[k]
                        for k in (*identity, "snapshot_revision", "resources", "counts")
                    )
                    and all(
                        _sha(intake._read(location / (r + ".jsonl"))) == hashes[r] for r in ROLES
                    ),
                    "workplace_snapshot_integrity_failed",
                )
            else:
                intake._publish(staging / "receipt.json", receipt)
                # Recheck authority after validating both streams, before exposing the pair.
                _live(store, context, digest(cfg))
                os.rename(staging, location)
                _fsync_dir(snapshots)
            _live(store, context, digest(cfg))
            if training is not None:
                _training_right(store, training, grant, snapshot_revision)
            for version_file, record in bindings:
                intake._publish(version_file, record)
            grant_path = directory / ("workplace-source-" + digest(grant) + ".json")
            intake._publish(grant_path, grant)
        return {
            **summary,
            "connection_revision": digest(cfg),
            "snapshot_revision": snapshot_revision,
            "trace_path": str(location / "trace.jsonl"),
            "correction_path": str(location / "correction.jsonl"),
            "grant_path": str(grant_path),
            "receipt_path": str(location / "receipt.json"),
            "source_proposal": discovery,
            "counts": counts,
        }


def _operate(
    store: LocalTrainingStore,
    connection: Path,
    *,
    acquire: bool,
    expected_pair_revision: str | None,
    expected_connection_revision: str | None,
    training_confirmation: Path | None,
    max_total_rows: int,
) -> Json:
    try:
        with store.engagement_guard():
            cfg = _live(store, connection, expected_connection_revision)
        revision = digest(cfg)

        @contextmanager
        def dispatch() -> Iterator[None]:
            # Fence the actual outbound request. The provider reply is read outside the guard.
            with store.engagement_guard(engagement_id=cfg["scope"]["engagement"]):
                _live(store, connection, revision)
                yield

        with _session(cfg, store.path.parent, dispatch) as client:
            pair, summary = _catalog(client, cfg, max_total_rows)
            summary["connection_revision"] = revision
            if expected_pair_revision is not None:
                _need(
                    _hex(expected_pair_revision)
                    and summary["pair_revision"] == expected_pair_revision,
                    "workplace_selected_pair_changed",
                )
            if not acquire:
                with dispatch():
                    return summary
            rows, wire_hashes, timing = {}, {}, {}
            for role in ROLES:
                started = time.monotonic_ns()
                observed = int(time.time())
                rows[role], wire_hashes[role] = _read_rows(client, cfg, role, pair[role])
                timing[role] = {
                    "started_at": observed,
                    "finished_at": int(time.time()),
                    "elapsed_ns": time.monotonic_ns() - started,
                }
            again, _ = _catalog(client, cfg, max_total_rows)
            _need(again == pair, "workplace_selected_pair_changed")
        return _snapshot(
            store, connection, cfg, pair, summary, rows, wire_hashes, timing, training_confirmation
        )
    except WorkplaceError:
        raise
    except TrainingError as error:
        # The intake validator produces fixed reason codes; provider errors never reach this branch.
        raise WorkplaceError(str(error)) from None
    except RepairError:
        raise WorkplaceError("invalid_workplace_feature_projection") from None
    except (OSError, ValueError, TypeError, KeyError, RecursionError, sqlite3.Error):
        raise WorkplaceError("workplace_source_unavailable") from None


def discover_workplace(
    store: LocalTrainingStore,
    connection: Path,
    *,
    expected_connection_revision: str | None = None,
    max_total_rows: int = 2 * intake.MAX_ROWS,
) -> Json:
    """Inspect a bounded resource catalog and return an opaque proposal without reading rows."""
    return _operate(
        store,
        connection,
        acquire=False,
        expected_pair_revision=None,
        expected_connection_revision=expected_connection_revision,
        training_confirmation=None,
        max_total_rows=max_total_rows,
    )


def acquire_workplace(
    store: LocalTrainingStore,
    connection: Path,
    *,
    expected_pair_revision: str | None = None,
    expected_connection_revision: str | None = None,
    training_confirmation: Path | None = None,
    max_total_rows: int = 2 * intake.MAX_ROWS,
) -> Json:
    """Read only the uniquely selected pair and publish an evaluation-only snapshot by default."""
    return _operate(
        store,
        connection,
        acquire=True,
        expected_pair_revision=expected_pair_revision,
        expected_connection_revision=expected_connection_revision,
        training_confirmation=training_confirmation,
        max_total_rows=max_total_rows,
    )


class _Arguments(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        self.exit(2, "invalid_workplace_arguments\n")


def main(argv: list[str] | None = None) -> int:
    parser = _Arguments(description=__doc__)
    verbs = parser.add_subparsers(dest="action", required=True)
    for action in ("discover", "acquire"):
        cmd = verbs.add_parser(action)
        cmd.add_argument("--directory", required=True, type=Path)
        cmd.add_argument("--org", required=True)
        cmd.add_argument("--grant", required=True, type=Path)
        cmd.add_argument("--expected-connection-revision")
        cmd.add_argument("--max-total-rows", type=int, default=2 * intake.MAX_ROWS)
        if action == "acquire":
            cmd.add_argument("--expected-pair-revision")
            cmd.add_argument("--training-confirmation", type=Path)
    args = parser.parse_args(argv)
    try:
        store = LocalTrainingStore(
            args.directory / "training.sqlite3", args.org, existing_only=True
        )
        if args.action == "acquire":
            result = acquire_workplace(
                store,
                args.grant,
                expected_connection_revision=args.expected_connection_revision,
                expected_pair_revision=args.expected_pair_revision,
                training_confirmation=args.training_confirmation,
                max_total_rows=args.max_total_rows,
            )
        else:
            result = discover_workplace(
                store,
                args.grant,
                expected_connection_revision=args.expected_connection_revision,
                max_total_rows=args.max_total_rows,
            )
        print(canonical(result))
        return 0
    except (TrainingError, OSError, sqlite3.Error, ManifestError) as error:
        print(
            error.reason if isinstance(error, WorkplaceError) else "workplace_source_unavailable",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
