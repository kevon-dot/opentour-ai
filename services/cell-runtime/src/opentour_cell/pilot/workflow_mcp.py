"""Local MCP and operator commands for one frozen, previously authorized evaluation."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

from opentour_cell.pilot.managed_run import ManagedError, ManagedSelection
from opentour_cell.pilot.mcp import serve_tools, tool_error, tool_result
from opentour_cell.pilot.slack_adapter import Refused
from opentour_cell.pilot.text_classifier import TrainingError

START = "start_offline_evaluation"
STATUS = "get_offline_evaluation"
TOOLS = [
    {
        "name": START,
        "description": "Queue the owner's fixed evaluation using its existing private grant.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "plan_revision": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
                "idempotency_ref": {"type": "string", "minLength": 1, "maxLength": 96},
            },
            "required": ["plan_revision", "idempotency_ref"],
        },
        "annotations": {
            "readOnlyHint": False,
            "idempotentHint": True,
            "destructiveHint": False,
            "openWorldHint": False,
        },
    },
    {
        "name": STATUS,
        "description": "Read a scoped run's state, elapsed time and aggregate result reference.",
        "inputSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"run_id": {"type": "string", "pattern": "^run-[0-9a-f]{32}$"}},
            "required": ["run_id"],
        },
        "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
    },
]


def _arguments(mode: str) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--authorization", required=True, type=Path)
    if mode == "authorize":
        parser.add_argument("--plan-revision", required=True)
        parser.add_argument("--custodian-attestation", required=True)
        parser.add_argument("--expires-at", required=True, type=int)
        parser.add_argument("--customer-wait-seconds", type=int)
    if mode == "worker":
        parser.add_argument("--once", required=True, action="store_true")
    return parser.parse_args()


def _operator(mode: str) -> int:
    args = _arguments(mode)
    try:
        selection = ManagedSelection(
            args.directory, args.organization, args.manifest, args.authorization
        )
        if mode == "worker":
            from opentour_cell.pilot.repair_workflow import execute_managed

            value = selection.once(execute_managed)
        else:
            from opentour_cell.pilot.repair_workflow import inspect_managed

            if mode == "authorize":
                selection.authorize(
                    args.plan_revision,
                    args.custodian_attestation,
                    args.expires_at,
                    args.customer_wait_seconds,
                    inspect_managed,
                )
                value = {"state": "authorized", "plan_revision": args.plan_revision}
            else:
                _, revision = selection.inspect(inspect_managed)
                value = {"state": "prepared", "plan_revision": revision}
        print(json.dumps(value, allow_nan=False))
        return 0
    except ManagedError as error:
        print(str(error), file=sys.stderr)
    except (OSError, ValueError, sqlite3.Error):
        print("private_selection_unavailable", file=sys.stderr)
    return 2


def authorize_main() -> int:
    return _operator("authorize")


def inspect_main() -> int:
    return _operator("inspect")


def worker_main() -> int:
    return _operator("worker")


def main() -> int:
    args = _arguments("mcp")
    try:
        selected = ManagedSelection(
            args.directory, args.organization, args.manifest, args.authorization
        )
    except (OSError, ValueError):
        print("private_selection_unavailable", file=sys.stderr)
        return 2

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            if name == START and set(arguments) == {"plan_revision", "idempotency_ref"}:
                return tool_result(
                    selected.start(arguments["plan_revision"], arguments["idempotency_ref"])
                )
            if name == STATUS and set(arguments) == {"run_id"}:
                return tool_result(selected.status(arguments["run_id"]))
            return tool_error("invalid_arguments")
        except ManagedError as error:
            if name == START:
                return tool_result({"state": "held", "reason": str(error)})
            return tool_error(str(error))
        except (OSError, sqlite3.Error, Refused, TrainingError):
            return tool_error("private_selection_unavailable")

    try:
        return serve_tools(
            "opentour-managed-evaluation", TOOLS, call, sys.stdin.buffer, sys.stdout.buffer
        )
    except BrokenPipeError:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
