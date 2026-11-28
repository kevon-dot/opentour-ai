# Track application capture stream completeness

The application-side capture register records evidence metadata under a
current optional grant. The grant binds the authenticated actor, tenant,
engagement, workflow, policy and three independently scoped source roles.
Capture settings start off. The metadata-only setting records event and
attempt references, terminal status, chunk indices and a private keyed
fingerprint for duplicate detection. Keep the fingerprint key in the
customer's private custody.

An application's retry is another observed attempt. An authoritative app
commit carries its own source and commit reference. This preserves the
distinction between an invocation, its terminal marker, and the committed
decision. A missing or conflicting chunk leaves raw replay unavailable. A
terminal marker for indices 0 through 2 with only 0 and 2 present leaves a
gap at 1. An identical delivery fills that gap when it arrives. Competing app
payload identities for the same source-authoritative decision ID and
revision are counted as disputed across operation IDs. The application
provides an opaque reference for the canonical committed payload separately
from the delivery event and commit receipt; repeated references with
different receipts are redundant evidence. Distinct decisions can share a
case and family. Within an inspected operation, the highest observed commit
revision of each decision determines its current count. Earlier attempts
remain available as provenance. Use the live reducer for deduplicated
workflow totals. Inspection exposes the dispute count without returning
the competing operation's metadata.
The current grant is checked again in the write transaction after
projection. Consent, source or expiry changes roll back the new observation.

Read results are role-scoped and contain attempt state and gap metadata.
Readers without application-commit permission see unknown decision counts.
The customer-local verified outcome reducer continues to own verified truth
and the current live evidence counts.

The application may supply the originally observed requested model,
projection revision and prompt template reference. The response boundary
may independently supply a receipt, served model and parsed-output
references. These optional fields carry their observation time and are
sealed with the current grant and attempt before persistence. A private
inspector sees only the permitted boundary. A requested alias never
fills an unknown served-model reference, and a response never creates
an application commit. Metadata-only observations leave exact replay
unavailable.

The [generated stream tests](../tests/pilot/test_capture_streams.py) exercise
off-to-metadata opt-in, restart with a gap, duplicate and conflicting
chunks, a two-attempt operation with one app commit, competing app commits,
scope denial, and a whitespace-free synthetic content sentinel.

The customer worker can resolve an explicit metadata-only grant with
`LocalCaptureAuthority`. The local custody operator controls its private key
and the signed record for one tenant and workflow. The record names an actor
and source for each recording boundary, the boundaries each inspector can
read, the active engagement, purpose, policy, mode and expiry. A separately
retained withdrawal marker fences an old restored signature. Every lookup
reads current authority. Key rotation also creates a separate capture
revision. The authenticated application or viewer supplies its actor after
verification at its own boundary.

## Sealed rows and the missing-chunk control

Every chunk, final-body and commit row is sealed under the grant key with its
boundary, source, event, attempt identity and observation time. Inspection
refuses the whole operation if a row was copied, edited or forged, so a
database writer without the key cannot fill a gap or invent a commit.

Each attempt reports `received_indices`, `missing_indices`, `terminal_seen`
and `exact_stream`. Chunks 0 and 2 with a finished terminal declaring three
chunks report the final receipt and the gap at 1, and the exact stream stays
unavailable. The gap closes in one of two ways:

- the identical chunk 1 arrives from the authorized response source; or
- that source delivers an authenticated complete final body
  (`CaptureStreams.final_body`). Only keyed references to each declared slice
  are kept. The body completes the stream only when every chunk that was
  observed separately has the identical reference and the finished terminal
  declares the same count. The view then reports `completed_by: final_body`
  and keeps chunk 1 in `missing_indices`.

A different slice, a later chunk that differs from the body, or a body beside
a timeout terminal makes the attempt disputed. A transcript, a tool summary
or a guessed completion is not a response observation. The adapter
dead-letters those record kinds. Any other source or actor is refused before
anything is written.

## Exact projections

In metadata-only mode the ledger keeps keyed references, never bodies, so
the view always reports `exact_replay: unavailable`.
`CaptureStreams.verify_projection` compares bytes the customer kept against
what one boundary observed. It is meant to run in a fresh session.

- **Request (`canonical-json-v1`).** The bytes must be UTF-8 JSON with
  exactly the declared top-level components, sorted keys, compact separators,
  unescaped non-ASCII text and no duplicate keys or non-finite numbers. They
  must re-serialize to themselves. A missing component fails
  `missing_component` and an extra one fails `undeclared_component`. A
  duplicate key, reordered keys, extra whitespace or an alternative escape
  fails `normalization_collision`, because two distinct byte strings would
  otherwise denote one projection. The sealed projection revision and the
  keyed reference recorded at observation time must match.
- **Response (`raw-chunks-v1`).** One byte string per declared chunk. Every
  index needs a reference recorded at observation time, from a chunk or from
  an authenticated final body. Bytes alone never fill an unobserved index.

A verdict states `integrity: locally_keyed` and `issuer_authenticated: false`.

## Gateway/provider log and application decision journal

`CaptureSourceAdapter` reads pages that an authenticated collector fetched.
The worker supplies the page's source, collector, cursor and coverage
window; a record never names its own source.

- `opentour.capture.gateway-log.v1` records are `request`, `chunk`,
  `terminal` and `final_body`. They map to the application-request and
  provider-response boundaries.
- `opentour.capture.decision-journal.v1` records are `commit` and map to the
  application-commit boundary.

Every record carries `event_id`, `event_revision`, `observed_at` and the
composite identity: operation, attempt, case, family, decision and decision
revision. Content travels base64-encoded (`request_projection_b64`,
`data_b64`, `body_b64`) and reaches the ledger only as keyed references. The
field allowlist
(`opentour.capture.gateway-projection.v1`,
`opentour.capture.journal-projection.v1`) is applied before anything is read
beyond it. Credentials, headers, contact fields and nested debug objects are
dropped and only counted.

An unusable record is dead-lettered with only its position and a reason code:

- `malformed_record`, `duplicate_key`, `oversized_record`;
- `unsupported_record` (including transcripts and tool summaries);
- `unattributable`, `invalid_field`, `future_observation`;
- `boundary_mismatch`, `capture_event_scope_changed`,
  `capture_operation_limit`.

Each boundary acknowledges a page with a receipt holding counts and a keyed
page reference. A restart before acknowledgement re-reads the page.
Identical observations are no-ops, so an app retry still counts one
committed decision. A replayed acknowledged page is a no-op. A different
page under an acknowledged cursor is refused with `capture_cursor_conflict`
before any write. A disabled, expired, revoked or withdrawn grant stops the
page unacknowledged with no capture effects. The adapter has no network
client and never retries or initiates a provider call.

`dry_run_capture` validates the same bytes with no grant, store or key and
writes nothing.

## Pinned historical export

`HistoricalImport` reads an `opentour.capture.historical-export.v1` export
only when its SHA-256 equals the pin the custodian declared separately. It
records under the historical-export boundary. Each decision keeps its
retention (`content`, `customer_local_pointer` or `metadata_only`) and a
fidelity for each component:

- `observed`;
- `reconstructed` from a pinned `opentour.capture.as-of-archive.v1` entry
  that was in force at the decision time;
- `unavailable` with a reason: `retention`, `opt_out`, `redaction`,
  `opaque_vendor`, `not_in_export`, `unresolved_alias` or
  `ambiguous_as_of_source`.

Only policy, template and attachment revisions may be reconstructed, and
only when the export omitted them or lost them to retention. A redaction or
opt-out stays unavailable. The current folder never fills a field. A
requested alias never fills the served model.

Two exports that disagree about one decision revision leave it disputed, with
no winner, and each original row stays byte-identical. The import view
counts observed decisions, disputed decisions and decisions eligible for
replay separately. A decision with an observed committed label supports
observed agreement only. Exact-model replay needs observed policy, template,
served model, request projection and response output, and then still needs
projection verification. Exact-target training is always unavailable from
capture, which carries no training purpose. `dry_run_historical` reports pin,
fidelity and window locally and writes nothing.

## Coverage and scoring boundaries

`sampling_manifest` parses a pinned upstream count of eligible operations
from a source independent of the traces. `capture_coverage` reports, for
example, 2/5 and labels agreement as observed on the captured subset. With
no manifest, a manifest from the trace source itself, or traces outside it,
coverage is unavailable.

`score_boundary` scores one declared boundary per run, and each decision
revision counts once. Independent truth comes from the OPE-514 live
reducer's verified roots.

- `provider_outcomes` uses the raw parsed output of the attempt the
  application committed.
- `application_outcomes` and `historical_outcomes` use the committed action.

Truth REVIEW, raw SUPPLIES and committed REVIEW give 0/1 at the provider
boundary and 1/1 at the application boundary. Mixed boundaries are refused.
A decision without an application commit makes the application metric
unavailable rather than zero.

## Independent original-record verifier

`capture_originals.py` imports only the standard library. A separate
evaluator runs it as `python -I capture_originals.py FIXTURE.json EXPORT_DIR`.
Its inputs are:

- the sealed fixture, retained apart from the export, with pinned original
  digests and the expected values for the scenario;
- the export's manifest: collector, run, tenant, engagement, workflow, grant
  and policy revisions, source cursors, coverage window, redaction
  revisions, operations, attempts, commits and file digests;
- the original gateway log and decision journal;
- every emitted surface.

The verifier checks the manifest against the fixture, then each file against
both digests, before trusting any counter. From the originals it recomputes:

- attempts and their terminal state;
- gaps and final bodies;
- conflicts and committed decisions;
- unjoinable commits;
- per-source admitted, quarantined and dropped counts;
- field provenance.

Every counter the product declares must equal the recomputed value, and both
must equal the fixture's expectation. With the fixture key it recomputes
each keyed page receipt. Rows from another tenant, engagement, grant, policy
or operation fail.

It scans every surface recursively, including keys, nested values, raw bytes
and text fallbacks. It looks for each restricted value in plain,
case-folded, UTF-16, hex, JSON-escaped and base64 forms at any alignment, and
as an unkeyed MD5, SHA-1, SHA-2, SHA-3 or BLAKE2 digest. An optional key
allowlist flags any new field on a reference-only surface. A surface the
fixture marks unavailable makes the scan `partial`, never "zero leaks".

The result always reports `installed_witness: unavailable`, so generated
receipts cannot qualify a live installation. Its integrity is
`digest_self_consistent_only`, or `locally_keyed` with receipt keys; it never
authenticates an issuer.

The [generated qualification](../tests/pilot/test_capture_originals.py) runs
fixture W through the real adapters with a restart before the commit
acknowledgement. It exports the worker log, exceptions (including suppressed
context), dead letters, the metadata store as rows and as raw bytes, and the
inspection views. Each of these faults turns the verifier red, and restoring
it turns the verifier green:

- a sentinel under a new nested field or key, or encoded, in every surface;
- an unapproved field;
- receipts, rows or a run id from another run;
- a removed, linked or replaced original;
- a duplicated commit with a conflicting winner;
- a selectively omitted chunk;
- a foreign record;
- a tampered declared counter;
- an unbuilt surface claimed as scanned.

Slack body, accessible fallback and Zap history are not built in this slice.
They are marked unavailable.

## Remaining gates

- An installed gateway or provider integration, for a provider selected and
  approved by the customer.
- Separately authorized original-content inspection in the private worker
  view, and the Slack S07 surface.
- Tool-call request and result references.
- A rung 3 or higher signing contract (OPE-566).

All evidence here is generated fixture evidence.
