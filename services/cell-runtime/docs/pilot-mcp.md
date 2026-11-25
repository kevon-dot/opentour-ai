# Inspect an offline evaluation from an MCP client

The operator chooses a local case manifest before starting the server. The server computes the descriptive readout once, closes the file and exposes the aggregate to its parent MCP client. Starting a new process selects a new snapshot. Use a client and execution environment approved for the aggregate report.

From the repository root, this command prints the report directly.

```sh
PYTHONPATH=services/cell-runtime/src python -m opentour_cell.pilot.readout services/cell-runtime/examples/pilot-evaluation.json
```

To inspect the same report through MCP, configure the client to launch Python using stdio. Replace the two example paths with absolute paths on the operator's machine. Use the repository's configured interpreter when `python` resolves elsewhere.

```json
{
  "command": "python",
  "args": ["-m", "opentour_cell.pilot.mcp", "/approved/evaluation.json"],
  "env": {"PYTHONPATH": "/checkout/services/cell-runtime/src"}
}
```

After the client initializes and sends `notifications/initialized`, list the tools and call `inspect_offline_evaluation` with `{}`. The tool returns the local command's aggregate in `structuredContent` and its serialized JSON in `content`. Repeated calls return the snapshot selected at startup. Closing the client's input stream ends the process.

This adapter implements the `2025-11-25` protocol version and advertises the `tools` capability. Requests use one UTF-8 JSON object per line. The frame limit is 64 KiB including the newline. An oversized frame ends the session. Invalid manifests exit with code 2 and a generic error on stderr. The operator supplies a regular file up to 4 MiB. The adapter uses only local stdio. Case-level evidence stays in the approved environment. Any decision to run further evaluation, change the model or release a change belongs to the workflow owner.

[MCP lifecycle](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) · [stdio transport](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports) · [MCP tools](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
