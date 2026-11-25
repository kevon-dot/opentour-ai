"""Exercise the operator entry point over actual newline-delimited stdio."""

import json
import os
import select
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from opentour_cell.pilot.mcp import MAX_FRAME_BYTES, MAX_MANIFEST_BYTES, PROTOCOL_VERSION, TOOL_NAME

MCP = "opentour_cell.pilot.mcp"
CLI = "opentour_cell.pilot.readout"
GENERATED_MANIFEST = {
    "schema_version": "opentour.pilot.case-manifest.v1",
    "error_ceiling_bps": 500,
    "cases": [
        {
            "case_id": "generated-private-case",
            "reference": "generated-private-label",
            "current": {"status": "review"},
            "repair": {"status": "automatic", "label": "generated-private-label"},
        }
    ],
}
INIT_PARAMS = {
    "protocolVersion": PROTOCOL_VERSION,
    "clientInfo": {"name": "generated-test", "version": "1"},
    "capabilities": {},
}
INIT_NOTIFICATION = {"jsonrpc": "2.0", "method": "notifications/initialized"}


def request(request_id, method, **params):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def frame(message):
    return json.dumps(message).encode() + b"\n"


class TestPilotMCP(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "generated-private-path.json"
        self.path.write_text(json.dumps(GENERATED_MANIFEST), encoding="utf-8")
        self.command = [sys.executable, "-m", MCP, str(self.path)]

    def exchange(self, data):
        proc = subprocess.run(self.command, input=data, capture_output=True, timeout=10)
        for channel in (proc.stderr, proc.stdout):
            self.assertNotIn(b"generated-private", channel)
            self.assertNotIn(str(self.path.parent).encode(), channel)
        return proc, [json.loads(line) for line in proc.stdout.splitlines()]

    def test_negotiation_ping_and_list(self):
        messages = [
            request(1, "ping"),
            request(2, "tools/list"),
            request(3, "initialize", **(INIT_PARAMS | {"protocolVersion": "future-version"})),
            request(4, "tools/list"),
            INIT_NOTIFICATION,
            request(5, "tools/list", _meta={}),
            request(6, "initialize", **INIT_PARAMS),
            request(7, "ping"),
        ]
        proc, replies = self.exchange(b"".join(map(frame, messages)))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stderr, b"")
        self.assertEqual([r["id"] for r in replies], list(range(1, 8)))
        self.assertEqual(replies[0]["result"], {})
        self.assertEqual(replies[1]["error"]["code"], -32002)
        self.assertEqual(replies[2]["result"]["protocolVersion"], PROTOCOL_VERSION)
        self.assertEqual(replies[2]["result"]["capabilities"], {"tools": {}})
        self.assertEqual(replies[3]["error"]["code"], -32002)
        tools = replies[4]["result"]["tools"]
        self.assertEqual([t["name"] for t in tools], [TOOL_NAME])
        self.assertEqual(tools[0]["inputSchema"], {"type": "object", "additionalProperties": False})
        self.assertTrue(tools[0]["annotations"]["readOnlyHint"])
        self.assertEqual(replies[5]["error"]["code"], -32600)
        self.assertEqual(replies[6]["result"], {})

    def test_pinned_snapshot_matches_cli_after_manifest_is_replaced(self):
        cli = subprocess.run(
            [sys.executable, "-m", CLI, str(self.path)], check=True, capture_output=True, timeout=10
        )
        expected = json.loads(cli.stdout)
        with subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        ) as server:
            try:
                server.stdin.write(frame(request(1, "initialize", **INIT_PARAMS)))
                server.stdin.flush()
                self.assertTrue(select.select([server.stdout], [], [], 10)[0])
                self.assertIn("result", json.loads(server.stdout.readline()))
                self.path.write_text("{}", encoding="utf-8")
                tool_calls = (
                    request("first", "tools/call", name=TOOL_NAME, arguments={}),
                    request("replay", "tools/call", name=TOOL_NAME),
                )
                out, err = server.communicate(
                    frame(INIT_NOTIFICATION) + b"".join(map(frame, tool_calls)), timeout=10
                )
            finally:
                if server.poll() is None:
                    server.kill()
                    server.communicate()
        self.assertEqual(server.returncode, 0)
        self.assertEqual(err, b"")
        self.assertNotIn(b"generated-private", out)
        replies = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([r["id"] for r in replies], ["first", "replay"])
        for reply in replies:
            result = reply["result"]
            self.assertEqual(result["structuredContent"], expected)
            self.assertEqual(json.loads(result["content"][0]["text"]), expected)

    def test_arguments_and_unsupported_methods_are_rejected(self):
        messages = [
            request(1, "initialize", **INIT_PARAMS),
            INIT_NOTIFICATION,
            request(2, "tools/call", name=TOOL_NAME, arguments={"path": str(self.path)}),
            request(3, "tools/call", name="generated-private-name"),
            request(4, "resources/read", uri="file:///generated-private-path"),
            request(5, "tools/list", cursor="generated-private-cursor"),
            request(6, "tools/call", name=TOOL_NAME, arguments=None),
            request(7, "tools/call", name=TOOL_NAME, task={}),
            {"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 2}},
            request(8, "ping"),
        ]
        proc, replies = self.exchange(b"".join(map(frame, messages)))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual([r["id"] for r in replies], list(range(1, 9)))
        self.assertTrue(replies[1]["result"]["isError"])
        self.assertNotIn("structuredContent", replies[1]["result"])
        self.assertEqual(
            [r["error"]["code"] for r in replies[2:7]], [-32602, -32601, -32602, -32602, -32602]
        )
        self.assertEqual(replies[-1]["result"], {})

    def test_malformed_frames_do_not_initialize_and_connection_recovers(self):
        bad_lines = b'{"a": "generated-private"\n\xff\n{"a": 1, "a": 2}\n[]\n'
        messages = [
            request(True, "initialize", **INIT_PARAMS),
            request(1, "initialize", clientInfo=[]),
            INIT_NOTIFICATION,
            request(2, "tools/list"),
            request(3, "initialize", **INIT_PARAMS),
            INIT_NOTIFICATION,
            request(4, "tools/list"),
        ]
        proc, replies = self.exchange(bad_lines + b"".join(map(frame, messages)))
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(
            [r["error"]["code"] for r in replies[:7]],
            [-32700, -32700, -32700, -32600, -32600, -32602, -32002],
        )
        self.assertIn("tools", replies[-1]["result"])

    def test_oversized_frame_terminates_without_processing_tail(self):
        proc, replies = self.exchange(b" " * MAX_FRAME_BYTES + frame(request(1, "ping")))
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0]["error"]["code"], -32600)

    def test_bad_oversized_absent_or_special_file_refuses_startup(self):
        for payload in (b'{"case_id":"generated-private"}', b" " * (MAX_MANIFEST_BYTES + 1), None):
            with self.subTest(size=len(payload) if payload is not None else None):
                if payload is None:
                    self.path.unlink()
                else:
                    self.path.write_bytes(payload)
                proc, replies = self.exchange(frame(request(1, "initialize", **INIT_PARAMS)))
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(replies, [])
                self.assertEqual(
                    proc.stderr, b"Unable to load the approved local evaluation manifest.\n"
                )
        if hasattr(os, "mkfifo"):
            os.mkfifo(self.path)
            proc, replies = self.exchange(b"")
            self.assertEqual(proc.returncode, 2)
            self.assertEqual(replies, [])
