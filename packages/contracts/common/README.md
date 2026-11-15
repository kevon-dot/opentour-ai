# Command foundation

OPE-123 owns the public wrappers, registry and local schema checks. The accepted
OPE-122 setup implementation remains the `just setup` entry. Shared-file ownership
transferred at merge `6a72788d64a85513267839894d8234ef690c9cee`, whose tree is identical
to reviewed base `3a7e224aa14c5870fd156a7152b17090519310d1`.

Run qualified setup first and use its pinned tools on PATH. The just recipes pass
arguments positionally; they never interpolate a supplied path into shell code.
Each wrapper emits one bounded JSON command-evidence document to stdout and returns
the public exit code. The accepted setup entry emits its OPE-122 receipt directly.

| Command | Current execution |
| --- | --- |
| `just setup` | Accepted OPE-122 setup; no provider provisioning |
| `just lint` | Pinned Ruff over the repository |
| `just typecheck` | Pinned mypy service/training/tools check and the existing TypeScript boundary |
| `just test-python PATH` | Exact existing Python file/directory; declared uv workspace package, separate training project, or root contract scope |
| `just test-web PATH` | Existing `.test.ts`/`.test.tsx` file; private exact-include Vitest configuration, no retries/only/zero-test pass |
| `just test-browser PATH` | Declared `.spec.ts` file/suite with an admitted profile and actual OPE-127 adapter |
| `just test-contract` | Hash-verified local schema examples, then actual contract tests |
| `just validate-spec` | Exit 2/NOT_RUN until OPE-126 validator integration |
| `just test-integration PROFILE FILTER` | `bootstrap-contract all`, or registered OPE-124 `local harness-smoke` with actual local prerequisites |
| `just test-isolation SUITE MODE` | Declared ISO and OPS-02/OPS-03 suites remain BLOCKED without owning real environment/harness |
| `just test-acceptance GATE MODE` | G1–G6 remain BLOCKED without owning actual environment/customer evidence |

Exit 0 means actual passing checks; 1 means assertion/execution/required-skip failure;
2 means invalid arguments, configuration or zero collection; 3 means a verified
missing external prerequisite. Unknown profiles are configuration errors. A runtime
credential string cannot upgrade an unimplemented integration or gate to PASS.
The fixture kind in command receipts is bound to the admitted execution profile;
`product_acceptance` remains `NOT_RUN`. The local profile's real PostgreSQL and
local-cell checks are `local_integration`; its separately retained scripted provider
observations remain `simulated`.

The initial Vitest configuration is a bounded Node unit profile with exact file
selection and no automatic project/config discovery. Application-specific Vite
plugins, DOM setup or future test projects require an explicit reviewed profile
integration. They cannot silently broaden file selection or disable the boundary.
Python unit/contract calls deny TCP, UDP and DNS through a test-process guard.
Vitest receives only the wrapper-owned Node network preload. These are test
discipline boundaries, not protection against hostile code. Browser local fixture
networking belongs to OPE-127; actual provider integration belongs to its owner.
The standard-library-only Python startup preload loads before initial conftest
imports, remains active through teardown and propagates to ordinary Python helpers
through the wrapper-owned PYTHONPATH. A failed installation exits before user code;
the pytest plugin also verifies the current process loaded the guard. Python
subprocess session/group detachment and Node detached-child APIs
are denied in these default unit profiles before configuration/test code executes.
Wrapper-managed child groups are bounded and leftover group members are killed.
This does not establish containment of arbitrary native programs deliberately
bypassing those process APIs. A future local service adapter must supply its own
explicit lifecycle authority and exact cleanup evidence; it does not inherit a
general network/process override from a unit receipt.
The pytest reporting plugin itself also imports only the standard library. Schema
libraries load only in the parent command wrapper, so separate training routing
executes with its approved pytest-only development dependencies. Actual training
pass/failure/required-skip fixtures verify the interpreter environment and absence
of jsonschema; no training dependency expansion is required.

`browser-adapter-v1.md` and `test-result.schema.json` define the OPE-127 seam.
The exact 63-path source inventory is preserved separately from admitted execution
profiles; currently only the C06 deterministic harness profile is admitted. Future
declared paths are not executed or reclassified as simulated merely by existing.

`schema-registry.json` binds the exact approved common and authority schema bytes.
It also binds the owned runner transport schema; malformed, mismatched or unresolved
transport schemas produce a structured configuration failure.
`local-observations-binding.json` pins OPE-124's owned observation schema without
copying it. Local execution retains the bounded, schema-validated observation
sidecar content and exact SHA-256 in its command receipt before temporary cleanup.
Passing local execution must include both deployment modes and nonempty observations.
References resolve only through this local registry; HTTP/file fallback is denied.
The canonical ResourceRevision pattern enforces positive decimal strings through
`9223372036854775807`, distinct from JSON-safe event/epoch integers. Examples cover
direct, nested and authority-alias overflow, invalid dates and canonical errors.
Schema validity does not prove signatures, authority or disclosure behavior: an
unauthorized Error.current_revision must remain null in its owning domain.

Command evidence records source identity before and after execution, dirty state,
the native source revision/spec digest, actual raw/normalized outcomes and stable
hashed test IDs. Rejected arbitrary arguments and raw logs are not copied into the
receipt. OPE-126 must integrate durable retention, EvidenceRecord validation and
source/acceptance traceability; stdout transport is not that completed collector.

## Acceptance mapping and remaining integration

`tests/contract/test_command_contract.py` maps AC01 to all eleven command dispatches
and exact exits, AC02 to traversal/metacharacter/unknown selections rejected before
execution, and AC03 to G1/G6 in both modes remaining BLOCKED without actual evidence.
Named fixtures cover spaces, missing/failing/zero/skipped tests, missing provider
authority, forbidden network and actual one-test success. Adverse transport tests
cover wrong selection, stale identity, count mismatch, cleanup, process descendants,
schema resolution and evidence privacy.

Foundation review is separate from full acceptance. OPE-127 must run the actual
browser consumer checks. The finite OPE-124 `local harness-smoke` integration invokes
only `tools.fixtures.local_harness` and binds the exact target
`tests/integration/test_failure_harness.py`. That adapter owns exact PostgreSQL
prerequisites, foreground fixture lifecycle and actual pytest mode/kind observations.
Its explicit BLOCKED exit3 is preserved separately from raw pytest internal-error
codes. Other local/provider adapters cannot gain authority by editing registry
text alone. OPE-126 validator/evidence and actual CI coverage also remain
open. A reviewed foundation may admit compatible dependent work while OPE-123 stays
In Progress; it does not waive any acceptance criterion.
