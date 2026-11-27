# Serve an owner-controlled structured export pair

The [structured export adapter](../examples/workplace_export_adapter.py) serves a
versioned decision trace and a separately authorized correction ledger from the
customer's private worker directory. Install it on the approved Linux x86-64 worker
with the pinned Python runtime. The workplace client verifies the launcher hash
and executes the verified file through its descriptor.

## Install the launcher

Choose the absolute Python 3.12.16 executable and the enrolled private worker directory.
Use one directory per scoped installation.

```sh
adapter="$PILOT_DIR/workplace-structured-export"
{
  printf '#!%s\n' "$PILOT_PYTHON"
  cat services/cell-runtime/examples/workplace_export_adapter.py
} > "$adapter"
chmod 700 "$adapter"
sha256sum "$adapter"
```

On first setup create a mode-0600 per-installation version key and retain it
with the other private installation material.

```sh
(umask 077; set -C; openssl rand -hex 32 > "$PILOT_DIR/workplace-export-version-key")
```

Set the workplace connection's reader provider to `opentour-structured-export`,
its executable to the absolute launcher path, its SHA-256 to the value above,
and its `env_names` to an empty array. Keep the signed/admitted business scope and
the explicit scope for each role from the
[workplace reader guide](pilot-workplace-context.md). Set
`watermark_mode` to `provider_verified`.

## Publish the two scoped resources

Create the JSON files under the mode-0700 worker directory with mode 0600. The
owner's export task should stage the body files first, hash their exact UTF-8
bytes, and then atomically publish `workplace-export-adapter.json`.

| Manifest field | Value |
| --- | --- |
| `schema_version` | `opentour.example.workplace-export-adapter.v1` |
| `expires_at` | Unix seconds within the owner-approved connection period |
| `resources.trace` | Trace URI, private file name, SHA-256, and its complete projection |
| `resources.correction` | Correction URI, private file name, SHA-256, and its complete projection |

Each resource entry has precisely `uri`, `file`, `sha256` and `projection`.
Use a distinct admitted resource URI, source ID and independently authorized
issuer for each role. Use file names such as
`workplace-export-trace-2026-11-26.json` and
`workplace-export-correction-2026-11-26.json`. Both projections carry the same
tenant, workflow, field, policy version and schema version. The projection
uses provider `opentour-structured-export` and a distinct source version
for each change to that resource's contents or cutoff.

The adapter derives the catalog's opaque version from the private key, the
issuer projection and the exact file pin. It returns that same version in the
MCP projection after validating the original bytes. A new body pinned under
the same issuer version still changes the catalog version. Keep the version
key stable so exact retries resolve the same pair.

Each resource file contains that exact `projection` plus its `rows`. The
projection's sorted `columns` and `row_count` describe the file. For example,
the trace can map `decision_at` to `src_decision_at` and the approved
`description` feature to `at_decision_description`. The independent
correction ledger maps `verified_at` to `src_verified_at`; the workplace reader
uses the earlier verified coverage cutoff when joining both streams. Keep
decision-time features within the trace projection and put outcomes in the
independent ledger. Use a zero row count with an empty array for an empty stream
whose coverage is established.

Discovery enumerates the two scoped descriptors before reading a resource body.
Acquisition reads only the selected URIs, checks each exact byte pin and row
projection, and rechecks the manifest on every request. A changed or incomplete
pair is held. The last successful worker snapshot remains available for retry under
current authority.

## Verify using generated data

The [descriptor conformance witness](../tests/pilot/test_workplace_export_adapter.py)
builds matching private manifests and projections from the checked-in generated
trace, correction and grant examples. It runs the adapter through the real
workplace worker CLI on Linux. A duplicate trace and duplicate correction count
once. With a trace cutoff of 320 and correction cutoff of 310, a final verified
at 320 stays deferred. Advancing the independently issued correction version
and its cutoff includes that result in a successor snapshot. A missing second
resource and a stale expected revision preserve the last complete receipt.
