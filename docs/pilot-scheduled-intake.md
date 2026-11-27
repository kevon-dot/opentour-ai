# Local scheduled admission component (OPE-439)

This component admits permitted opaque revisions through the existing OPE-430
`open_intake` factory and its durable admission service. It does not read source
pages, run a model, dispatch training or activate a Zap. Live admission preserves
the existing workflow and benchmark; historical admission retains the accepted
factory's benchmark bookkeeping. This is a partial software handoff, not OPE-439
acceptance or installed evidence.

The current source base is protected OPE-514 integration commit
`3888fcdc86e23f0c14b06b372fc172341be5d74f` (PR #140). The accepted intake factory
returns `BenchmarkIntake`, which composes the existing `DurableIntake`. The
scheduler uses its unchanged discovery and admission methods. A complete current
signed export can now be queued; a missing reader/export still holds pending.
Neither state means this scheduler has read a source page.

The earlier PR #136 evidence remains bound to
`decce6b2413a50038af0e2a276c86d1fb3b9070a` and its original factory base
`e3f422795fab2b1a31b6dd9f6dc340f213575c02`, where reader availability returned
false. Those historical receipts are not evidence for this newer integration.

## Current native contract and boundaries

The current [OPE-439 issue](https://linear.app/opentourai/issue/OPE-439),
[shared contract](https://linear.app/opentourai/document/first-ten-implementation-contract-intake-evidence-training-and-release-4e23522a139f),
[OPE-430](https://linear.app/opentourai/issue/OPE-430),
[OPE-514](https://linear.app/opentourai/issue/OPE-514),
[OPE-400](https://linear.app/opentourai/issue/OPE-400),
[OPE-402](https://linear.app/opentourai/issue/OPE-402), and
[OPE-519](https://linear.app/opentourai/issue/OPE-519) govern this handoff.
Full current issues and the mandatory PRD were inspected before implementation.
OPE-439's current native update was 2026-11-26T23:29:28.179Z; the shared contract
update was 2026-11-26T21:07:32.515Z. Later changes require renewed mapping.

| Native clause / fixture | This component and generated test | Gate still open |
| --- | --- | --- |
| Explicit scheduled route, custodian-approved cadence; starter-only tenants unaffected | Private immutable local configuration is disabled unless explicitly enabled; never selects starter intake. Disabled test opens no intake factory. | Authenticated custodian scheduling UI and installed approval/configuration revision. Local file access is not signed custodian authority. |
| Same intent across ticks / transport keys; legitimate new revision distinct | Deterministic key binds tenant, installation, immutable binding version and source revision, or both historical revisions plus policy. Real durable admission canonicalization also deduplicates the independent operator key. | Installed Zap replay through the hosted action. |
| Cursor owned by worker; crash before commit; retain unacknowledged items | SQLite stages a whole bounded discovery page before submission. Each acknowledged item is removed durably; cursor advances when that page is drained. An explicit superseded hold moves the exact unadmitted request to a retained disposition, never an acknowledgement, so other feeds can progress. Crash after admission replays the exact request and reuses the actual run. | Source-reader checkpoint/admission/materialization join. This is a discovery/admission cursor, not a source-page or committed-event cursor. Dispositions remain outstanding unadmitted history. |
| Stop midway through two-page batch, outage, overlapping backfill/ticks | Real signed decision and outcome bindings with page size 1, restart/outage and repeated sweeps recover both references. One local file lease serializes ticks and controls; concurrent attempted tick/control is busy. | Authorized historical revision inventory and source high-water backfill. Current discovery lists current live bindings, not every prior revision. |
| Per-tick page/operation/deadline bounds; monthly task allowance | Discovery attempt cap, cadence, conservative reserve-before-call local operation accounting, per-tick cap, calendar-month reservation quota and cooperative admission-start deadline. Budget-writer waits recheck expiry/deadline and select the month under lock; each call rechecks before starting. Timeout retains pending admission. | Actual Zapier task accounting, provider request rate and source-reader hard timeout/paging continuation. A local call already in progress can finish past the admission-start deadline. |
| Paused/revoked/expired feed, delayed Zap, tenant-ID collision | Local pause/revoke prevents opening the factory. Source authority is revalidated by the real factory and each actual admission; revocation between discovery and submit commits no run. Foreign tenant, removed actor, bad seal, expiry and selector replacement fail closed. Installation keys include tenant; separate generated workers remain isolated. | Installed independent tenant matrix and actor grants. No claim that local process tests qualify provider tenancy. |
| Decision feed proceeds before outcomes; outcome-only update reconciles older decision; independent cursors/counts | Decision-only admission works; adding independent outcome binding produces a distinct admission. No payload reader called. | Scheduled join to the accepted OPE-514 reader and per-feed committed cursors. D1/D2/outcome final/reopen/as-of counts must be verified through that normal scheduled entry. |
| Empty batch idle; quota held; zero rows in task history | Empty real binding store remains empty; idle costs 1 local discovery operation. One active single-page tick costs 2 local operations. Output is only allowlisted state and opaque tick receipt. | Installed redacted Zap History and measured provider task charges in OPE-435. |
| Explicit benchmark rerun never synthesized | Scheduler accepts only historical/live metadata; no rerun API, model/trainer or release path is called. Live tests preserve one pre-existing workflow and zero materialization/events. | Integrated benchmark invariance and explicit rerun eligibility remain with OPE-500 and integrated acceptance. |
| Versioned clonable hosted Zap, disabled template, remapping, rollback | Versioned disabled handoff recipe and local CLI instructions below. | This JSON is not a Zapier import/export artifact. No hosted scheduler action is exposed by the three-action private app. OPE-437 publication, authenticated hosted worker invocation, actual clonable template and installed trace remain required. Draft #126 is not publication. |
| Bulk manifest enqueue, checkpoints, overlap/quotas/deletion | No page rows or model calls travel through this scheduler. | OPE-519 manifest reference contract, immutable revision inventory, checkpoint monitoring and verified deletion strategy do not yet exist on this accepted factory. No bulk success is simulated. |
| Source/version/config revision and owner-local journal | Exact base above; immutable configuration digest, durable installation identity, pending envelopes, usage rows and tick receipts live in private SQLite. | Packaged builder integration, connector/configuration pin and installed trace. Existing kit does not automatically include this new module. |

## Accepted reader and remaining join

The accepted reader exposes `live_reader.materialize_live(selection, run_ref)`
for an already admitted immutable live operation. Its normal packaged command is
`live-reader SELECTION RUN_REF`. It rechecks current source authority, retains
per-page receipts and uses the intake materialization fence for retries. An
explicit reader invocation after a scheduler admission can therefore process the
same run without another admission. This is separate from an automatic tick.

There is no page allowance, deadline or continuation-result parameter on that
reader entry. It processes the selected catalog's complete page list, bounded by
32 pages, and completes the operation only after that work returns. The scheduler's
`pages_per_tick` currently bounds opaque discovery calls, not source pages.
Calling the whole reader once per admission would not enforce the configured
source-work limits. The missing join needs a reader-owned bounded invocation and
recoverable completion/continuation receipt, with source authority rechecked before
each read and commit, plus durable scheduler dispatch for acknowledged run refs.
It must distinguish admission from source completion and preserve already committed
pages on pause, outage or exhaustion. No replacement reader protocol is invented
by this admission-only component.

The builder owner must register `opentour_cell/pilot/scheduled_intake.py` and map
`scheduled-intake` to `("scheduled_intake", "main")`. That only packages the existing
admission CLI; it does not supply the missing reader join, signed custodian schedule
approval from OPE-426, OPE-519 bulk manifest route, optional OPE-531 mixed-source
route, hosted invocation or installed OPE-435 evidence. Protected #140 fixed the
earlier missing `live_authority` archive import; it does not package this module.

## Local configuration and invocation

An authorized owner-local operator supplies an existing private selection from
the accepted intake factory and a separate private scheduling configuration.
The scheduler cannot create or broaden the existing source grant. The worker
directory must already be canonical, owner-held mode 0700; files must be regular,
single-link owner-held mode 0600. Configuration and selection must be in that
same directory. The selector's digest is SHA-256 of sorted compact ASCII JSON,
matching `scheduled_intake._digest`; it is a local pin, not an authorization.

The exact configuration fields are:

| Field | Constraint |
| --- | --- |
| `schema_version` | `opentour.pilot.scheduled-intake.v1` |
| `installation_ref`, `tenant`, `actor` | Existing scoped opaque identifiers; actor/tenant must match the factory principal |
| `selection_file`, `selection_sha256` | Private `.json` basename and canonical selector digest |
| `enabled`, `expires_at` | Explicit boolean (default recipe false), future integer Unix deadline |
| `timezone`, `frequency_seconds` | IANA zone; minimum elapsed cadence 1–2678400 seconds |
| `page_size`, `pages_per_tick` | 1–50 opaque references per page; 1–32 discovery calls per tick |
| `operations_per_tick`, `monthly_operations` | 1–1000 per tick; 1–1000000 local calls per calendar month, including ambiguous/failed calls |
| `tick_seconds` | 1–300 cooperative seconds after initialization; checked under the budget writer, immediately before new operations and after completed calls; does not interrupt an already started call |
| `historical_intent` | Null for live; otherwise exact `pair_ref`, `source_revision`, `correction_revision`, `policy_revision` already authorized by the selected historical factory |

Every field is required; unknown fields fail. Configuration is immutable after
its first durable use. Changing a budget cannot reset usage for an existing
installation. A future authenticated update procedure must retain old usage and
pending work; deleting the database or inventing a replacement installation is
not an authorized quota-reset or migration strategy.

```sh
python -m opentour_cell.pilot.scheduled_intake --configuration /owner/private/schedule.json tick
python -m opentour_cell.pilot.scheduled_intake --configuration /owner/private/schedule.json pause
python -m opentour_cell.pilot.scheduled_intake --configuration /owner/private/schedule.json resume
python -m opentour_cell.pilot.scheduled_intake --configuration /owner/private/schedule.json revoke
```

`resume` requires originally enabled configuration, unexpired limits and a
currently authorized factory. Revocation is terminal. A busy control attempt
has not taken effect and must be retried. Once pause/revoke returns, later ticks
open no factory and perform no discovery/admission. An already admitted run is
retained for provenance; these controls do not delete history or withdraw a
source grant. The underlying grant's own pause/revocation remains authoritative.

The transport envelope contains only `state` and an opaque `receipt_ref` (or
only `state=unavailable` when no safe receipt can be recorded). Pending live
admission is never labeled materialized. The private journal contains opaque
admission metadata, not payload rows, and must remain inside the worker boundary.
The allowed state set is `enabled`, `paused`, `revoked`, `expired`, `cadence_held`,
`timeout`, `quota_held`, `held`, `page_limit`, `pending`, `admitted`, `idle`, and
`unavailable`. Local operation counts are not provider billing measurements.

For rollback, pause locally and confirm its receipt before disabling the external
trigger. Retain the private selector, immutable configuration, SQLite journal and
existing intake ledger. Revert executable deployment only to a reviewed version
that understands this schema, or keep the route paused. Never clear the cursor,
delete unacknowledged requests or change installation identity to force a retry.
If a live binding is superseded while queued, an explicit trusted
`binding_superseded` hold preserves the exact unadmitted item in
`schedule_dispositions`. Its hint never becomes an automatically submitted
successor. Other pending references drain and a bounded fresh authorized scan
can discover the current binding. A failed paged discovery retains its old
cursor and marks a fresh-scan retry; a failed fresh scan retains that state.
There is at most one fresh-scan recovery per tick, and every attempted discovery
counts toward page/operation limits. Generic discovery denial is not described
as confirmed cursor expiry. Source authority is checked again on recovery.
No automatic historical backfill or admission of the superseded intent is claimed.

## Reproducible generated validation

Use the repository-pinned Python 3.12.16 and dependencies. The focused suite uses
real signed private authority, actual durable binding/admission stores, an actual
CLI subprocess and a separately generated historical mandate/pair grant. Generated
keys/identifiers never represent installed customer authority.

```sh
PYTHONPATH=services/cell-runtime/src python -B -m pytest \
  services/cell-runtime/tests/pilot/test_scheduled_intake.py \
  services/cell-runtime/tests/pilot/test_live_reader.py \
  services/cell-runtime/tests/pilot/test_live_authority.py \
  services/cell-runtime/tests/pilot/test_live_evidence.py \
  services/cell-runtime/tests/pilot/test_intake_mcp.py -q -p no:cacheprovider
MYPYPATH=services/cell-runtime/src mypy services/cell-runtime/src/opentour_cell/pilot/scheduled_intake.py
ruff check services/cell-runtime/src/opentour_cell/pilot/scheduled_intake.py \
  services/cell-runtime/tests/pilot/test_scheduled_intake.py
```

The existing Python unit selector discovers the new test file. No workflow or
global wrapper is changed. Exact-commit independent content review is required
before publishing this partial draft; actual CI receipts follow on that draft,
and protected integration is required before acceptance. Full native OPE-439 acceptance
requires the open producer, hosted, installed and packaged joins listed above.
