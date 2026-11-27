"""Serve an owner-pinned pair of structured workplace exports over read-only MCP stdio."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import sys
import time
from pathlib import Path
from typing import Any

MANIFEST = "workplace-export-adapter.json"
VERSION_KEY = "workplace-export-version-key"
SCHEMA = "opentour.example.workplace-export-adapter.v1"
PROJECTION = "opentour.pilot.workplace-projection.v1"
META = "ai.opentour/workplace"
PROTOCOL = "2025-11-25"
PROVIDER = "opentour-structured-export"
ROLES = ("trace", "correction")
SCOPE = {"organization", "workflow", "field", "policy_version", "source_schema_version"}
BASENAME = re.compile(r"workplace-export-(trace|correction)-[A-Za-z0-9_-]{1,64}\.json")
TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
URI = re.compile(r"(workplace|https)://[a-z0-9][a-z0-9.-]*/[A-Za-z0-9_-]+(/[A-Za-z0-9_-]+)+")
Json = dict[str, Any]


def require(condition: object) -> None:
    if not condition:
        raise ValueError("workplace_export_unavailable")


def fields(pairs: list[tuple[str, Any]]) -> Json:
    value = dict(pairs)
    require(len(value) == len(pairs))
    return value


def reject_constant(_value: str) -> None:
    raise ValueError("workplace_export_unavailable")


def decode(raw: bytes) -> Json:
    value = json.loads(
        raw.decode("utf-8"), object_pairs_hook=fields, parse_constant=reject_constant
    )
    require(isinstance(value, dict))
    return dict(value)


def encode(value: Json) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def private_read(path: Path, limit: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        file = os.fstat(stream.fileno())
        require(
            stat.S_ISREG(file.st_mode)
            and file.st_uid == os.getuid()
            and file.st_nlink == 1
            and not file.st_mode & 0o077
            and file.st_size <= limit
        )
        raw = stream.read(limit + 1)
    require(len(raw) <= limit)
    return raw


def read_manifest(root: Path) -> bytes:
    return private_read(root / MANIFEST, 32 * 1024)


class ExportAdapter:
    def __init__(self, root: Path):
        info = root.stat()
        require(info.st_uid == os.getuid() and not info.st_mode & 0o077)
        self.root = root
        self.key_file = private_read(root / VERSION_KEY, 65)
        require(re.fullmatch(rb"[a-f0-9]{64}\n?", self.key_file))
        key = bytes.fromhex(self.key_file.decode())
        self.manifest = read_manifest(root)
        selected = decode(self.manifest)
        require(
            set(selected) == {"schema_version", "expires_at", "resources"}
            and selected["schema_version"] == SCHEMA
            and type(selected["expires_at"]) is int
            and 0 < selected["expires_at"] < 2**63
            and isinstance(selected["resources"], dict)
            and set(selected["resources"]) == set(ROLES)
        )
        self.expires_at = selected["expires_at"]
        self.resources: dict[str, Json] = selected["resources"]
        self.catalog: dict[str, Json] = {}
        for role in ROLES:
            pin = self.resources[role]
            require(isinstance(pin, dict) and set(pin) == {"uri", "file", "sha256", "projection"})
            basename = BASENAME.fullmatch(pin["file"])
            require(basename and basename[1] == role and URI.fullmatch(pin["uri"]))
            require(re.fullmatch(r"[a-f0-9]{64}", pin["sha256"]))
            projection = pin["projection"]
            fixed = {
                "schema_version": PROJECTION,
                "provider": PROVIDER,
                "role": role,
                "policy_status": "published",
            }
            identifiers = SCOPE | {"authority", "source_id", "version"}
            require(
                isinstance(projection, dict)
                and set(projection)
                == set(fixed) | identifiers | {"covered_through", "columns", "row_count"}
                and all(projection[key] == expected for key, expected in fixed.items())
                and all(TOKEN.fullmatch(projection[key]) for key in identifiers)
                and type(projection["covered_through"]) is int
                and 0 <= projection["covered_through"] <= time.time()
                and projection["covered_through"] < self.expires_at
                and type(projection["row_count"]) is int
                and 0 <= projection["row_count"] <= 5000
            )
            columns = projection["columns"]
            require(
                isinstance(columns, list)
                and 0 < len(columns) <= 128
                and all(TOKEN.fullmatch(name) for name in columns)
                and columns == sorted(set(columns))
            )
            # Bind discovery to exact privately pinned bytes without publishing their digest.
            version = "projection-" + hmac.digest(key, encode(pin).encode(), "sha256").hex()[:48]
            self.catalog[pin["uri"]] = {
                "uri": pin["uri"],
                "name": role,
                "mimeType": "application/json",
                "_meta": {META: projection | {"version": version}},
            }
        trace, correction = (self.resources[role]["projection"] for role in ROLES)
        require(
            len(self.catalog) == 2
            and all(trace[key] == correction[key] for key in SCOPE)
            and trace["authority"] != correction["authority"]
            and trace["source_id"] != correction["source_id"]
        )
        self.check_current()

    def check_current(self) -> None:
        require(
            self.expires_at > time.time()
            and read_manifest(self.root) == self.manifest
            and private_read(self.root / VERSION_KEY, 65) == self.key_file
        )

    def list(self, params: Json) -> Json:
        self.check_current()
        first, second = self.catalog.values()
        if not params:
            return {"resources": [first], "nextCursor": "correction"}
        require(params == {"cursor": "correction"})
        return {"resources": [second]}

    def read(self, params: Json) -> Json:
        self.check_current()
        require(set(params) == {"uri"} and isinstance(params["uri"], str))
        uri = params["uri"]
        require(uri in self.catalog)
        pin = self.resources[self.catalog[uri]["name"]]
        raw = private_read(self.root / pin["file"], 2 * 1024 * 1024)
        require(hashlib.sha256(raw).hexdigest() == pin["sha256"])
        body = decode(raw)
        columns = set(pin["projection"]["columns"])
        require(
            set(body) == {"projection", "rows"}
            and body["projection"] == pin["projection"]
            and isinstance(body["rows"], list)
            and len(body["rows"]) == pin["projection"]["row_count"]
            and all(isinstance(row, dict) and set(row) == columns for row in body["rows"])
        )
        self.check_current()
        body["projection"] = self.catalog[uri]["_meta"][META]
        return {"contents": [{"uri": uri, "mimeType": "application/json", "text": encode(body)}]}


def serve(adapter: ExportAdapter) -> None:
    started = ready = False
    while frame := sys.stdin.buffer.readline(32 * 1024 + 1):
        require(len(frame) <= 32 * 1024 and frame.endswith(b"\n"))
        request = decode(frame)
        require(request.get("jsonrpc") == "2.0" and isinstance(request.get("method"), str))
        method = request["method"]
        if "id" not in request:
            if method == "notifications/initialized" and started:
                ready = True
            continue
        identifier = request["id"]
        require(type(identifier) is int or isinstance(identifier, str))
        envelope = {"jsonrpc": "2.0", "id": identifier}
        try:
            params = request.get("params", {})
            require(isinstance(params, dict))
            if method == "initialize" and not started:
                require(params.get("protocolVersion") == PROTOCOL)
                adapter.check_current()
                started = True
                result = {
                    "protocolVersion": PROTOCOL,
                    "capabilities": {"resources": {}},
                    "serverInfo": {"name": PROVIDER, "version": "1"},
                }
            elif ready and method in {"resources/list", "resources/read"}:
                handler = adapter.list if method == "resources/list" else adapter.read
                result = handler(params)
            else:
                envelope["error"] = {"code": -32601, "message": "method_unavailable"}
                result = None
            if result is not None:
                envelope["result"] = result
        except (KeyError, OSError, TypeError, ValueError, RecursionError):
            envelope["error"] = {"code": -32000, "message": "workplace_export_unavailable"}
        print(json.dumps(envelope, allow_nan=False, ensure_ascii=False), flush=True)


def main() -> int:
    try:
        serve(ExportAdapter(Path.cwd()))
    except (OSError, TypeError, ValueError, KeyError, RecursionError):
        print("workplace_export_unavailable", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
