# Private pilot qualification

Use the qualification register to record a signed candidate or a terminal Lost disposition before
reserving a live engagement. Keep the legal-entity directory, original evidence, signed charter
and signer authorization in approved operator custody. Assign one stable opaque organization
reference per legal entity and reuse it across contacts, source routes and re-entry.

## Manual entry

Use an owner-controlled mode-0700 directory. Put the authority configuration and signed
submission in separate mode-0600 files. Use a distinct 32-byte key per signer, organization,
route and mapping. The designated signing adapter attests the charter references after checking
the actual private mandate and related facts. HMAC authenticates the adapter sharing the key
with the verifier. Keep that verification configuration outside the submitted envelope. Revoking
the adapter's authorized configuration must stop new submissions before resuming intake.

From the extracted kit directory with Python 3.12.16, run

```sh
python opentour-slack.pyz qualification \
  --register /private/cohort/qualification.sqlite3 \
  --authority /private/cohort/authority.json \
  --submission /private/cohort/submission.json
```

In the source environment, use `python -m opentour_cell.pilot.qualification` with the same arguments.

The authority object has schema version `opentour.pilot.qualification-authority.v1` and the fields
`organization_ref`, `signer_ref`, `route`, `mapping_ref`, `expires_at` (Unix seconds), and
`verification_key_hex` (64 lowercase hex digits). For manual submission, use `route=manual`
and a JSON null mapping. Pass only local paths to the command.

The submission is an object with `event` and `signature`. The signature is lowercase hexadecimal
HMAC-SHA-256 over the event schema version, a newline, and the event's canonical JSON. Canonical
JSON sorts object keys, ASCII-escapes strings, omits whitespace between tokens and rejects
non-finite numbers. The event schema version is `opentour.pilot.qualification-event.v1`.

| Event fields | Values |
| --- | --- |
| `organization_ref`, `signer_ref` | Opaque identifiers from the operator-controlled legal entity directory and authorization |
| `engagement_revision`, `source_revision` | Positive integers from the authoritative qualification stream. Engage first at 1. Source revision advances within an engagement. |
| `event_id`, `source_ref` | Original stable delivery identifier and private opaque charter or source record reference |
| `route`, `mapping_ref` | Manual with null mapping, or a separately authorized CRM route and exact mapping reference |
| `intent` | `starter`, `historical`, `lost`, `feedback`, `research`, `collateral`, or `interest` |
| `charter` | Object of the fields below for eligibility, null for Lost or discovery |

Both starter and historical charters use `owner_ref`, `custodian_ref`, `evaluation_purpose_ref`,
`local_location_ref` and `intended_decision_ref`. Starter additionally requires `policy_ref`,
`policy_revision`, `policy_grant_ref`, `verifier_role_ref`, `verifier_plan_owner_ref` and an ISO date
in `verifier_next_check`. The optional `verifier_ref` can remain null. Historical mode additionally
requires `decision_source_ref`, `decision_revision`, `decision_authority_ref`,
`correction_source_ref`, `correction_revision` and `correction_authority_ref`. Decision and
correction sources and authorities must each be distinct. The signed material and authorization
must substantiate permission and corrective independence before the issuer submits the references.

The command gives an opaque candidate and operation reference, a bounded disposition, or the
field-level gaps for a verified issuer to resolve. The ledger preserves original delivery IDs,
source revisions and dated dispositions. Identical current delivery returns the original receipt.
Signing authority must still be current after the register lock is acquired, including on receipt
replay. A first starter admission checks its verifier date against that same current UTC date.
A signed Lost event makes its engagement terminal. Re-entry requires the next engagement
revision and a freshly signed complete charter. A stale delivery cannot reopen a closed engagement.

Qualification grants no live lease, customer read, fit spend or export. Separate gates own
active leases, actual data access, the payer and limits for any fit, and any delivery consent.
The CLI and its database operate entirely on the owner machine.

## CRM route evidence

The route is qualified separately. GTM operations must sign the exact authorized production
board, stage and column mapping, the installed and redacted trace with operation identifier,
and the owner and disable or rollback path. The CRM signing adapter must supply its exact
authorized mapping and monotonic source revision for the same organization and engagement.
Without those real receipts, the installed CRM-to-Zap route remains unverified. A generated or
manual exercise demonstrates the common handler only. The GTM operations owner can check the
installed path when production mapping and scoped access are approved.

## Verification

From the repository root with the pinned environment, run
`just test-python services/cell-runtime/tests/pilot/test_qualification.py`. The generated
fixtures exercise the CLI in fresh processes, compare concurrent replays, inspect the local
ledger's exact saved references and observed UTC times, and verify scope denial, discovery,
Lost, stale delivery, re-entry, incomplete authority and independent history cases.
An independent SQLite writer delays admission across signing expiry and UTC midnight to verify
that stale preflight time cannot authorize a write or disclose a prior candidate receipt.
