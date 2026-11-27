# Browser adapter v1

This is the OPE-123/OPE-127 runner transport, not a product/provider API or an
EvidenceRecord substitute. OPE-123 owns this protocol, its result schema and wrapper
validation. OPE-127 owns Playwright configuration, fixture application, runner and
real browser execution. OPE-126 owns durable evidence aggregation and traceability.

## Invocation and selection

The wrapper uses structured argv without a shell:

```text
node tools/browser-test-runner.ts PATH --result-file REPORT
```

The current working directory is the repository root. PATH is one exact canonical
repository-relative file or registered suite directory. Ordinary `.spec.ts` names
are supported; `.browser.spec.ts` and `.visual.spec.ts` are not exclusive suffixes.
Both sides reject traversal, symlinks, options, cross-runner paths and missing files.
The adapter must escape and anchor Playwright's regular-expression file selector,
then report the actual selected files. File selections may contain only that exact
file; directory selections may contain only descendants of the requested directory.
Prefix/substring collisions must not execute or count as the requested test.

The 63 declarations in `browser-suite-inventory.json` are source inventory, not
proof of implementation. `command-manifest.json` separately admits execution
profiles. Initially only the C06 deterministic browser-harness fixture is admitted
with expected kind `simulated`. Other declared paths require an explicitly reviewed
profile before execution. Future provider/customer profiles need their owning
authority/prerequisite checks; a report cannot grant itself that authority.

The scoped presentation profile runs the metric, status-copy and operation-state
suites owned by OPE-304, OPE-306 and OPE-308. Each reports once for the generated
fixtures. The adapter writes child-specific evidence under `evidence/OPE-<id>/runs`.
Status copy compares its screenshots with the reviewed platform images.

REPORT is a fresh absent file inside a private wrapper-created directory. The wrapper
sets `OPENTOUR_RESULT_NONCE` to a fresh 32-character lowercase hexadecimal nonce,
`OPENTOUR_RESULT_FILE` to REPORT, `OPENTOUR_TEST_TARGET` to PATH, and
`OPENTOUR_REPOSITORY_ROOT` to the repository root. The adapter must require matching
argv/environment and refuse an existing report or symlink. It writes complete JSON
atomically after cleanup. The wrapper accepts only its chosen regular report file,
up to 8 MiB, bound to this invocation. The nonce provides identity binding, not
authentication. Child output is bounded to 1 MiB and execution to 300 seconds.

The wrapper strips inherited provider credentials, NODE_OPTIONS and PYTEST_ADDOPTS.
The browser harness owns its fixture application's explicit local-network allowlist,
credential/config isolation and cleanup. No real account is needed for C06. A locked
browser installation and the application fixture must actually exist before tests
can pass; a globally cached browser is not an assumed prerequisite.

## Result contract

`test-result.schema.json` is the complete closed shape, version
`opentour.test-result.v1`. For this adapter `runner` is `playwright`, `target` is
PATH, `binding_nonce` matches the wrapper, and `kind` matches the wrapper-owned
execution profile. The common vocabulary remains `simulated`, `local_integration`,
`provider_integration`, `customer_evidence`; this vocabulary does not admit future
profiles automatically.

`mode` is `customer_controlled`, `opentour_managed` or null. Aggregate null denotes
mixed or mode-inapplicable execution. Each actual UI test records its actual mode;
null per test is limited to mode-inapplicable runner/setup probes and explicitly
registered presentation fixtures whose local props have no deployment mode. Test IDs must be
unique across test, project and mode. `tests` records every collected test as
`{id,file,status,mode}`. Status is `passed`, `failed` or `skipped`. Collected but
unexecuted cases are skipped; failed attempts, timeout, interruption and expected
failure cannot become green through retries or expected-failure annotations.

`collected` equals inventory length and `passed + failed + skipped`. All counts are
nonnegative JSON-safe integers. Missing tests, zero collection and required skips
never pass. `cleanup_complete` must truthfully cover contexts, fixture processes,
listeners and temporary resources. The wrapper also kills surviving members of its
own process group and treats incomplete cleanup as failure. Detached descendant
groups require the adapter's explicit lifecycle tracking and cleanup; leader exit
alone cannot establish that they stopped, especially after abnormal termination.

The raw child exit is retained alongside the normalized public exit. A report never
converts nonzero exit into PASS. PASS requires exit zero, a nonempty all-passed
inventory, correct identity/counts and complete cleanup. PASS has null reason.

| Public exit | Meaning and registered reason codes |
| --- | --- |
| 0 | Actual passing execution; reason null |
| 1 | ASSERTION_FAILED, REQUIRED_TEST_SKIPPED, RUNNER_TIMEOUT, RUNNER_INTERRUPTED, CLEANUP_FAILED, RUNNER_ERROR |
| 2 | NO_TESTS, INVALID_CONFIGURATION, MISSING_BROWSER, MISSING_APPLICATION |
| 3 | MISSING_EXTERNAL_PREREQUISITE with explicit BLOCKED report and raw exit 3 |

Malformed/missing reports are configuration failure, never inferred BLOCKED. A
configuration/setup failure may have zero inventory and FAIL. External prerequisites
must not mask an assertion failure or skipped required case. Local missing browser
or fixture application is exit 2. Real external prerequisites remain exit 3.

No raw exceptions, credentials, customer data or provider logs enter this envelope.
Ordinary runs do not update baselines. OPE-127 separately records the established
source/commit, fixture/rendering metadata and sanitized artifact hashes for OPE-126.

## Consumer acceptance still required

The wrapper's controlled transport fixtures do not establish actual browser
integration. OPE-127 must exercise real one-test pass/failure, zero/required skip,
wrong/out-of-target selection, stale nonce, malformed/count-inconsistent result,
missing browser/application, timeout/interrupt, cleanup failure, spaces, traversal,
symlink escape, both deployment modes and forbidden external network. Negative
probes assert subprocess failure; required top-level coverage cannot be skipped.
An independently reviewed OPE-123 foundation may admit that work while OPE-123
remains In Progress for real integration and CI acceptance.

## Source binding

This protocol implements native OPE-123 at `2026-11-15T20:57:13.808Z` and OPE-127 at
`2026-11-15T05:00:07.626Z`, without changing their public commands or acceptance. The OPE-123 timestamp refresh
records the accepted OPE-122 handoff checkpoint; its bounded behavior is unchanged.
The exact recovered audit inventory has SHA-256
`46f968237d2e0e9621a0f897725bb6031e2c7d861c5cd9956bed299b7cc76817`:
`acceptance/browser-suite-inventory.json` inside
`opentour-final-e2e-audit-supplement-2026-11-14.zip`. Current native issue changes
override historical inventory through reviewed registry updates, never inferred
filename declarations.
