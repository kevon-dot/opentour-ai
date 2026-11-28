# Customer fine-tuning provider credential: references and lifecycle (OPE-578)

`opentour_cell.pilot.provider_credential` records **references** to the customer's own
fine-tuning provider credential and the lifecycle of those references. It never stores a
credential value. The value is read from the customer's secret-store locator at the moment of
use and handed to the caller wrapped in a redacting `SecretValue`.

Evidence tier: `EvidenceTier.FIXTURE`. Every test uses generated sentinel secrets, generated
accounts and a generated provider name.

## What it does

| Part | Behavior |
| --- | --- |
| `CredentialReference` | Provider, customer account id, owner and payer references, scope (`fine_tune`, `model_read` only), locator (`file` path or `store_key` name), optional provider budget id, `issued_at`/`not_after`. Refuses a field that looks like a value: a known key prefix, a high-entropy run, or a field over its length bound (64; locator 200). Unknown fields such as `value` or `api_key` are refused. |
| `CredentialResolver.resolve` | Requires an active reference inside its window. Walks the locator with no-follow directory descriptors and refuses any link (final, parent or hard link), a non-regular file, a mode other than `0600`, an owner other than the expected uid, a group- or world-writable parent, an empty, oversized or non-printable value. Refusals carry a fixed code only. The value is read in a frame that never raises, so it does not reach an exception, traceback, log or record. |
| `CredentialLedger` | Durable, append-only, hash-chained JSONL journal (`0600` in a `0700` directory, `fsync` per event, `flock` serialized). Any edited, reordered, removed-interior or partial line fails closed as `credential_ledger_corrupt`. A ledger line that carries a value-like field is refused on read. |
| `rotate` | Issues the new reference and activates it in one lock; the previous reference is `retired`. A retired or revoked reference is refused locally from any process, and its locator can never be reissued. |
| `revoke`, `withdraw` | One durable event revokes the references and lists every in-flight job bound to them as fenced. A fenced job has no outcome until `record_outcome` records what was observed (`unknown` is a valid record). Nothing is assumed. |
| `authorize_dispatch` | Reads an injected standing-mandate view fresh, and requires: an active mandate, owner, payer, currency and a positive cap; an exact mandate id and revision match; the same owner and payer on the request, the mandate and the credential reference; the same provider account; and a bound within the remaining cap. A refusal writes nothing. It does not reserve or settle budget. |
| `python -m opentour_cell.pilot.provider_credential probe` | Resolves one reference in a fresh process and prints only the reference, a result code and `provider_side_refusal`. Used by the rotation drill. |

## What this does NOT prove

* **No provider call.** No provider account, API, key, budget or usage limit is touched. The
  least-privilege credential in the customer's own account (item 1), and any provider-side
  budget, need a real customer account. A test account owned by OpenTour does not count.
* **No provider-side refusal.** The rotation drill proves only a *local* refusal: the ledger
  refuses the old reference and its locator from a fresh process. It does not prove the provider
  refuses the old credential. Every receipt says `not_attempted_out_of_scope`.
* **No provider-side revocation.** `revoke` and `withdraw` write a durable local record and fence
  jobs. They do not revoke anything at the provider, and they do not cancel a provider job. The
  fenced job's provider outcome must be observed and recorded by the connector that owns it.
* **The customer's secret store is not exercised.** Only a `file` locator is resolved. A
  `store_key` locator is recorded but refused with `locator_kind_unsupported` until a resolver
  for the customer's secret store exists.
* **The same uid `/proc` exposure remains.** Both worker units (`opentour-cycle` and
  `opentour-worker`) run as the same uid. The managed worker can read the cycle unit's
  `/proc/<pid>/environ`, which holds the `/etc/opentour-worker/cycle.env` credentials. Reading
  at use instead of from the environment keeps the value out of `environ`, but it does not fix the
  boundary. A process with the same uid can also read the secret file itself, and may be able to
  read the resolving process's memory depending on ptrace policy. Closing this needs a separate
  dispatch uid (the reserved `opentour-signer` user) or `PrivatePIDs` (systemd 257).
* **The ledger is tamper-evident, not tamper-proof.** Anyone who can write the ledger directory
  as this uid can rewrite the whole chain consistently, or edit or drop the final events: with
  no external anchor, removing a trailing revocation is not detected (a test pins this). An
  anchor outside this uid's reach (OPE-566 anti-rollback) is needed to close it.
* **The value screen is a heuristic.** It reliably refuses random tokens of 20 or more
  characters and known key prefixes. A short or low-entropy secret can pass. Readable references
  are lowercase words joined by `-`, `_`, `.` or `:`; a provider id that is itself random-looking
  is refused and must be recorded under a readable alias.
* **The value is a Python string.** It cannot be scrubbed from memory after use.

## Gaps against the current code

* **No standing mandate can carry a provider cap today.** `learning_mandate._normalized` requires
  `max_cost_cents == 0`, so the standing mandate cannot hold a nonzero approved cap for a paid
  provider job. `authorize_dispatch` refuses a missing or zero cap with `mandate_cap_required`, so
  a mandate built from the current schema can never authorize a managed dispatch. Item 5 is
  blocked until the mandate schema (OPE-426) records a provider cap, owner and payer for the
  provider account.
* **Not wired into a connector.** `managed_connector` and `custom_trainer_connector` still take
  an opaque `credential_ref` and resolve it through their own transport. Adopting this resolver
  and `authorize_dispatch` there is separate work, owned elsewhere.

## External gates

`EXTERNAL_GATES` in the module lists what stays open: the customer-created least-privilege
credential, a provider-side budget, the customer's secret store, provider-side refusal after
rotation, provider-side revocation, a mandate with a nonzero provider cap, and a separate uid
or private PIDs between the two worker units.
