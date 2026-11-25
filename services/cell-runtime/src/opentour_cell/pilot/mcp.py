"""Serve the aggregate readout of one operator-selected manifest over local MCP stdio."""

import json
import os
import stat
import sys
from typing import Any, BinaryIO, Never

from opentour_cell import __version__
from opentour_cell.pilot.readout import (
    MAX_MANIFEST_BYTES,
    ManifestError,
    evaluate_manifest,
    parse_manifest,
)

PROTOCOL_VERSION = "2025-11-25"
MAX_FRAME_BYTES = 64 * 1024
TOOL_NAME = "inspect_offline_evaluation"
TOOL = {
    "name": TOOL_NAME,
    "description": "Inspect aggregate case counts for the operator's fixed local evaluation.",
    "inputSchema": {"type": "object", "additionalProperties": False},
    "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
}


def _error(code: int, message: str) -> dict[str, Any]:
    return {"error": {"code": code, "message": message}}


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    obj = dict(pairs)
    if len(obj) != len(pairs):
        raise ValueError("Duplicate member")
    return obj


def _reject_constant(_: str) -> Never:
    raise ValueError("Invalid JSON number")


class _Session:
    def __init__(self, readout: dict[str, Any]) -> None:
        self.phase = "new"
        self.tool_result = {
            "structuredContent": readout,
            "content": [{"type": "text", "text": json.dumps(readout, allow_nan=False)}],
        }

    def respond(self, frame: bytes) -> dict[str, Any] | None:
        try:
            request = json.loads(
                frame.decode("utf-8"),
                object_pairs_hook=_unique_object,
                parse_constant=_reject_constant,
            )
        except (ValueError, RecursionError):
            return {"jsonrpc": "2.0", **_error(-32700, "Parse error")}

        if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
            return {"jsonrpc": "2.0", **_error(-32600, "Invalid request")}
        method = request.get("method")
        if not isinstance(method, str):
            # This server sends no requests. An incoming response needs no response of its own.
            if "id" in request and ("result" in request or "error" in request):
                return None
            return {"jsonrpc": "2.0", **_error(-32600, "Invalid request")}
        params = request.get("params", {})
        valid_params = isinstance(params, dict) and isinstance(params.get("_meta", {}), dict)
        if valid_params:
            params.pop("_meta", None)
        valid_envelope = not (request.keys() - {"jsonrpc", "id", "method", "params"})
        if "id" not in request:
            if (
                valid_envelope
                and method == "notifications/initialized"
                and self.phase == "negotiated"
                and params == {}
            ):
                self.phase = "ready"
            return None
        request_id = request["id"]
        if type(request_id) not in (str, int):
            return {"jsonrpc": "2.0", **_error(-32600, "Invalid request ID")}
        if not valid_envelope:
            response = _error(-32600, "Invalid request")
        elif not valid_params:
            response = _error(-32602, "Invalid params")
        else:
            response = self._dispatch(method, params)
        return {"jsonrpc": "2.0", "id": request_id, **response}

    def _dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "ping":
            return _error(-32602, "Invalid params") if params else {"result": {}}
        if method == "initialize":
            if self.phase != "new":
                return _error(-32600, "Already initialized")
            info = params.get("clientInfo")
            if not (
                isinstance(params.get("protocolVersion"), str)
                and isinstance(params.get("capabilities"), dict)
                and isinstance(info, dict)
                and isinstance(info.get("name"), str)
                and isinstance(info.get("version"), str)
            ):
                return _error(-32602, "Invalid initialization parameters")
            self.phase = "negotiated"
            result = {
                "protocolVersion": PROTOCOL_VERSION,
                "serverInfo": {"name": "opentour-offline-evaluation", "version": __version__},
                "capabilities": {"tools": {}},
            }
            return {"result": result}
        if self.phase != "ready":
            return _error(-32002, "Complete initialization before accessing tools")
        if method == "tools/list":
            if params:
                return _error(-32602, "Invalid listing parameters")
            return {"result": {"tools": [TOOL]}}
        if method != "tools/call":
            return _error(-32601, "Method not found")
        if params.keys() - {"name", "arguments"} or params.get("name") != TOOL_NAME:
            return _error(-32602, "Unknown tool or invalid call parameters")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return _error(-32602, "Tool arguments must be an object")
        if arguments:
            message = {"type": "text", "text": "This tool accepts an empty arguments object."}
            return {"result": {"isError": True, "content": [message]}}
        return {"result": self.tool_result}


def _serve(readout: dict[str, Any], source: BinaryIO, destination: BinaryIO) -> int:
    session = _Session(readout)
    while frame := source.readline(MAX_FRAME_BYTES + 1):
        response: dict[str, Any] | None
        if len(frame) > MAX_FRAME_BYTES:
            # Terminate rather than interpreting the rest of an oversized line as another request.
            response = {"jsonrpc": "2.0", **_error(-32600, "Message exceeds byte limit")}
        else:
            response = session.respond(frame)
        if response is not None:
            destination.write(json.dumps(response, allow_nan=False).encode("utf-8") + b"\n")
            destination.flush()
        if len(frame) > MAX_FRAME_BYTES:
            return 2
    return 0


def main() -> int:
    args = sys.argv[1:]
    if len(args) != 1 or args[0] in {"-", "-h", "--help"}:
        print("Usage: python -m opentour_cell.pilot.mcp MANIFEST_FILE", file=sys.stderr)
        return 0 if args in (["-h"], ["--help"]) else 2
    try:
        # Open once without waiting on a FIFO; only an operator-selected regular file is eligible.
        flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
        with os.fdopen(os.open(args[0], flags), "rb") as manifest:
            if not stat.S_ISREG(os.fstat(manifest.fileno()).st_mode):
                raise OSError("Regular file required")
            readout = evaluate_manifest(parse_manifest(manifest.read(MAX_MANIFEST_BYTES + 1)))
    except (OSError, ManifestError):
        print("Unable to load the approved local evaluation manifest.", file=sys.stderr)
        return 2
    try:
        return _serve(readout, sys.stdin.buffer, sys.stdout.buffer)
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
