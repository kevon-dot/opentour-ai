# Deterministic browser contract fixture

Owner: OPE-127. This is a visibly simulated application that exercises browser
interaction contracts. It does not implement the product UI, contact a provider,
mint authority receipts, or establish G1 / customer evidence. Named domain fixtures
remain with their domain owners; none is created or replaced here.

Run `just test-browser apps/web/tests/browser-harness.browser.spec.ts` from a
qualified checkout. Install only the locked browser with:

```sh
PLAYWRIGHT_BROWSERS_PATH="$PWD/.toolchain/playwright" pnpm exec playwright install chromium
```

The runner requires the wrapper's nonce, exact target and fresh report destination.
Temporary browser profiles live inside that private result directory, so the
surviving wrapper removes them even when the adapter cannot run its finally block.
It starts a unique loopback fixture listener, checks its run identity, uses one
worker and no retries, and writes the closed transport report after cleanup.
Missing application/browser, zero tests and required skips are failures. Internal
negative probe files cannot be selected through the public command manifest.

The synthetic server uses common Error and ResourceRevision shapes from the
accepted common contract. Its control/cell routes are fixture-only projections,
not a second production API or an implementation of the authority algorithm.
Fixtures retain revision strings and simulated effect identities. They model
server refusal, same-intent replay and lost acknowledgment; these observations
do not prove real transactional/provider semantics.

Browser contexts deny unexpected fetches, redirects, popups and WebSockets;
service workers are disabled. The Node preload denies nonfixture sockets and DNS,
including API-request-context access. This is a discipline boundary for reviewed
test code, not hostile-code containment or provider isolation. No personal browser
profile, storage state or inherited credential environment is used.

The preload also forces managed Playwright descendant spawns to retain the outer
supervisor's process group. Playwright negative-group termination is translated
only to registered live ChildProcess handles. Sampled PID/start-time observations
are used for failure detection, never as authority to signal an unrelated PID.
The actual OPE-123 supervisor is exercised with both timeout and adapter SIGKILL
after Chromium has rendered; tests check group membership, no running survivors
and listener closure. Uncertain cleanup remains a nonpass.

## Acceptance map

Every UI row executes in `customer_controlled` and `opentour_managed`. Runner
mechanism probes are mode-inapplicable and explicitly record null. All rows are
scoped to OT-BOOT-C06-AC01; authority/revision/replay rows also cover AC02.

| Test ID | Positive and adverse observations |
| --- | --- |
| BH-STATES | Loading, empty, pending, unknown, denied, ready and recovery |
| BH-TENANT-RACE | Current tenant renders; delayed former-tenant response cannot restore stale data |
| BH-AUTHORITY | Enabled control meets later server denial; failed switch grants no authority |
| BH-REVOCATION | 202 remains waiting for cells; refresh retains existing operation |
| BH-REVISION | Exact large decimal strings; invalid/overflow values refused; stale draft retained; foreign404 nondisclosing |
| BH-REPLAY | Lost acknowledgment remains unknown; identical retry has one simulated effect; changed intent body conflicts |
| BH-CELL-OFFLINE / BH-ERROR | Private-network help and recovery; safe error text; malformed, 429 and503 responses never become success |
| BH-A11Y | Named controls, keyboard/focus, dialog dismissal, disabled actions; no certification claim |
| BH-RESET | Fresh context cannot read previous account/organization storage |
| BH-DETERMINISM / BH-RESPONSIVE / BH-BILLING | Fixed clock/font/seed, four widths, two routes, labelled figures and unknown distinct from zero |
| BH-DISPATCH | Exact anchored selection, spaces, traversal, symlink and cross-runner rejection |
| BH-LIFECYCLE | Real one-browser pass/fail/skip/expected-failure/zero/network/setup/timeout/interrupt probes; explicit cleanup fault; outer supervisor crash recovery |
| BH-TRANSPORT | Mismatched invocation and preexisting report refused without overwrite |

The OPE-123 consumer suite additionally owns malformed/count-inconsistent/stale
transport envelope rejection. Those controlled wrapper fixtures do not substitute
for this suite's actual Chromium execution.

## Visual review and evidence

`baseline-review.json` binds initial candidates to the fixture design revision,
font, browser and exact image hashes. Ordinary runs use `updateSnapshots: none`,
zero changed pixels, zero pixel threshold and no masks. Missing baselines fail.
Pinned Chromium launches with `--disable-skia-runtime-opts` to avoid observed
CPU-optimization rounding differences at card corners. The qualified raster path
retains all sixteen approved images byte-for-byte, including both modes and all
four widths. The browser version, font and comparison settings remain pinned.

Baseline authoring is a separate, explicit operation:

```sh
node apps/web/tests/fixtures/harness/author-baselines.ts 'review reason' 'design revision'
```

That command creates review candidates; it never grants approval. A reason,
design revision and independent review are required for every baseline change.
Initial Darwin/arm64 images have the independent review recorded in baseline-review.json. Linux
rendering is a distinct qualification and must not silently reuse Darwin pixels.
OPE-125 integrates the `browser-fixtures` CI job with locked dependencies/browser,
the same public command, bounded resources and sanitized artifact retention.

Local outputs live in ignored `evidence/OT-BOOT-C06/runs/<nonce>/`: screenshots,
synthetic-only traces, runner diagnostics and SHA-256 artifact inventory. Evidence
records actual test inventory/mode, source commit and dirty state, fixture/spec
revision, rendering inputs and limitations. OPE-126 owns durable EvidenceRecord
aggregation and source traceability; these files do not replace its collector.
G1, AC-22-01 and AC-23-01 remain source references with real owning acceptance
separate from this simulated fixture. `just validate-spec` must report its actual
available integration status; no local placeholder is a pass.

Rollback consists of reverting the OPE-127 source commit; no product data or
database migration exists. Remove only this checkout's ignored browser/evidence
directories when reclaiming space. Never delete another user's browser cache or
terminate unrelated processes.
