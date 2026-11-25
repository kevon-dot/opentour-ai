"""Protocol regression tests and a separate official SDK probe for a real prepared workflow."""

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from opentour_cell.pilot.managed_run import plan_revision
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from opentour_cell.pilot.workflow_mcp import START, STATUS

MODULE = "opentour_cell.pilot.workflow_mcp"


def args_for(directory, organization, manifest, authorization):
    return [
        "--directory",
        str(directory),
        "--organization",
        organization,
        "--manifest",
        str(manifest),
        "--authorization",
        str(authorization),
    ]


class TestManagedMcp(unittest.TestCase):
    def test_new_process_lists_required_tools_and_cannot_self_authorize_or_return_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = {
                "schema_version": "opentour.pilot.managed-repair.v1",
                "organization": "org",
                "engagement": "trial",
                "intake_revision": "b" * 64,
                "private": "protected-case-sentinel",
            }
            path = root / "plan.json"
            path.write_text(json.dumps(data))
            path.chmod(0o600)
            rev = plan_revision(data)
            entries = [
                {
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": PROTOCOL_VERSION,
                        "capabilities": {},
                        "clientInfo": {"name": "generated", "version": "1"},
                    },
                },
                {"method": "notifications/initialized"},
                {"id": 2, "method": "tools/list"},
                {
                    "id": 3,
                    "method": "tools/call",
                    "params": {
                        "name": START,
                        "arguments": {"plan_revision": rev, "idempotency_ref": "one"},
                    },
                },
                {
                    "id": 4,
                    "method": "tools/call",
                    "params": {
                        "name": START,
                        "arguments": {
                            "plan_revision": rev,
                            "idempotency_ref": "one",
                            "allow_training": True,
                        },
                    },
                },
                {
                    "id": 5,
                    "method": "tools/call",
                    "params": {"name": STATUS, "arguments": {"run_id": "run-" + "1" * 32}},
                },
            ]
            raw = "".join(json.dumps({"jsonrpc": "2.0", **entry}) + "\n" for entry in entries)
            command = [
                sys.executable,
                "-m",
                MODULE,
                *args_for(root, "org", path, root / "grant.json"),
            ]
            result = subprocess.run(
                command, input=raw, text=True, capture_output=True, timeout=10, check=True
            )
            records = {
                row["id"]: row["result"] for row in map(json.loads, result.stdout.splitlines())
            }
            self.assertEqual([t["name"] for t in records[2]["tools"]], [START, STATUS])
            self.assertTrue(records[2]["tools"][1]["annotations"]["readOnlyHint"])
            self.assertEqual(
                records[3]["structuredContent"],
                {"state": "held", "reason": "authorization_required"},
            )
            self.assertTrue(records[4]["isError"])
            self.assertTrue(records[5]["isError"])
            self.assertNotIn("protected-case-sentinel", result.stdout)
            self.assertEqual(set(root.iterdir()), {path})

    def test_fresh_process_start_authorized_comparison_and_scope_revocation(self):
        from opentour_cell.pilot.managed_run import ManagedSelection
        from opentour_cell.pilot.repair_workflow import execute_managed, inspect_managed
        from opentour_cell.pilot.source_intake import withdraw_intake
        from opentour_cell.pilot.training import LocalTrainingStore

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "customer"
            fixture = Path(__file__).resolve().parents[2] / "examples" / "prepare-repair-example.py"
            setup = subprocess.run(
                [sys.executable, str(fixture), str(directory), "--route", "rule"],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertTrue(json.loads(setup.stdout))
            plan = directory / "managed-plan.json"
            grant = directory / "workflow-permission.json"
            scope = ManagedSelection(directory, "generated-co", plan, grant)
            _, revision = scope.inspect(inspect_managed)
            scope.authorize(
                revision, "att-0123456789abcdef", int(time.time()) + 1800, None, inspect_managed
            )

            def call(name, arguments):
                frames = [
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "protocolVersion": PROTOCOL_VERSION,
                            "capabilities": {},
                            "clientInfo": {"name": "fresh", "version": "1"},
                        },
                    },
                    {"method": "notifications/initialized"},
                    {
                        "id": 2,
                        "method": "tools/call",
                        "params": {"name": name, "arguments": arguments},
                    },
                ]
                raw = "".join(json.dumps({"jsonrpc": "2.0", **item}) + "\n" for item in frames)
                result = subprocess.run(
                    [
                        sys.executable,
                        "-m",
                        MODULE,
                        *args_for(directory, "generated-co", plan, grant),
                    ],
                    input=raw,
                    text=True,
                    capture_output=True,
                    timeout=10,
                    check=True,
                )
                return json.loads(result.stdout.splitlines()[-1])["result"]

            accepted = call(START, {"plan_revision": revision, "idempotency_ref": "once"})[
                "structuredContent"
            ]
            self.assertEqual(accepted["state"], "queued")
            complete = scope.once(execute_managed)
            self.assertEqual(complete["state"], "ready")
            self.assertEqual(complete["result"]["case_count"], 4)
            self.assertEqual(complete["result"]["outcome"], "simple_repair")
            self.assertEqual(
                call(STATUS, {"run_id": accepted["run_id"]})["structuredContent"], complete
            )
            before = scope.path.read_bytes()
            source = json.loads((directory / "source-grant.json").read_bytes())
            withdraw_intake(
                LocalTrainingStore(directory / "training.sqlite3", "generated-co"),
                source["owner_attestation"],
            )
            self.assertTrue(call(STATUS, {"run_id": accepted["run_id"]})["isError"])
            held = call(START, {"plan_revision": revision, "idempotency_ref": "second"})[
                "structuredContent"
            ]
            self.assertEqual(held["state"], "held")
            self.assertEqual(before, scope.path.read_bytes())


async def official_probe(options):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    path = options.manifest
    revision = plan_revision(json.loads(path.read_text()))
    base = args_for(options.directory, options.organization, path, options.authorization)
    env = {"PATH": os.environ.get("PATH", ""), "PYTHONDONTWRITEBYTECODE": "1"}
    if options.source_root:
        env["PYTHONPATH"] = str(options.source_root.resolve())
    if options.bundle:
        server = ["-I", "-S", "-B", str(options.bundle), "mcp-workflow", *base]
        worker = ["-I", "-S", "-B", str(options.bundle), "workflow-worker", *base, "--once"]
    else:
        server = ["-B", "-m", MODULE, *base]
        worker = [
            "-B",
            "-c",
            "from opentour_cell.pilot.workflow_mcp import worker_main\n"
            "raise SystemExit(worker_main())",
            *base,
            "--once",
        ]

    async def call(name, arguments):
        params = StdioServerParameters(command=options.operator_python, args=server, env=env)
        async with (
            stdio_client(params) as (reader, writer),
            ClientSession(reader, writer) as session,
        ):
            await session.initialize()
            names = [tool.name for tool in (await session.list_tools()).tools]
            assert set(names) == {START, STATUS}
            answer = await session.call_tool(name, arguments)
            assert answer.isError is False and answer.structuredContent, answer
            return answer.structuredContent

    request = {"plan_revision": revision, "idempotency_ref": options.key}
    accepted = await call(START, request)
    assert accepted["state"] == "queued", accepted
    duplicates = await asyncio.gather(call(START, request), call(START, request))
    assert all(item == accepted for item in duplicates)
    raw = subprocess.run(
        [options.operator_python, *worker],
        env=env,
        capture_output=True,
        timeout=options.timeout,
        check=True,
        text=True,
    )
    finished = json.loads(raw.stdout)
    got = await call(STATUS, {"run_id": accepted["run_id"]})
    assert got == finished and got["state"] == "ready", got
    assert set(got["result"]) == {"result_revision", "case_count", "outcome"}
    print(
        json.dumps(
            {
                "fixture": "generated",
                "official_sdk_sessions": 4,
                "concurrent_duplicate_starts": 2,
                "real_customer_wait_measured": False,
                "run": got,
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--directory", required=True, type=Path)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--authorization", required=True, type=Path)
    parser.add_argument("--operator-python", required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--key", required=True)
    parser.add_argument("--timeout", type=int, default=600)
    asyncio.run(official_probe(parser.parse_args()))
