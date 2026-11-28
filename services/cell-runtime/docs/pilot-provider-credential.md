# Customer fine-tuning provider credential: references and lifecycle (OPE-578)

`opentour_cell.pilot.provider_credential` records **references** to the customer's own
fine-tuning provider credential and the lifecycle of those references. It never stores a
credential value. The value is read from the customer's secret-store locator at the moment of
use, only for a dispatch authorization this module journaled, and handed to the caller wrapped
in a redacting `SecretValue`.

Evidence tier: `EvidenceTier.FIXTURE`. Every test uses generated sentinel secrets, generated
accounts, a generated provider name and a real `LearningMandates` ledger in a temporary
directory.

## What it does

| Part | Behavior |
| --- | --- |
| `CredentialReference` | Provider, customer account id, owner and payer references, scope (`fine_tune`, `model_read` only), locator (`file` path or `store_key` name), optional provider budget id, `issued_at`/`not_after`. Refuses a field that looks like a value: a known key prefix, a high-entropy run, a UUID or digest outside a locator, or a field over its length bound (64; locator 200). A canonical UUID is allowed as a locator path segment only; the key-prefix screen still applies to it. Unknown fields such as `value` or `api_key` are refused. |
| `authorize_dispatch` | Reads the standing mandate itself, from an exact `LearningMandates` (`inspect`), and type-checks every value it returns. Requires an active autonomous mandate, the worker as its delegate, a positive cost cap, a provider account and cost usage on the mandate, the same owner and payer on the request, mandate and credential reference, the same provider account, and a bound within the cap. The bound counts against `max(this ledger's journaled maxima, the mandate's reported usage)`. It then reserves through the mandate ledger (`LearningMandates.reserve`, which enforces its own fit and seconds budgets) and journals an authorization whose id is this module's digest of its body. A refusal writes nothing. The caller never supplies the mandate's revision, owner, payer or cap. |
| `CredentialResolver.resolve` | Requires a journaled authorization id and its job, not fenced and with no recorded outcome; an active reference inside its window; and the mandate still active at the same revision (re-read from the ledger). Walks the locator with no-follow directory descriptors and refuses any link (final, parent or hard link), a non-regular file, a mode other than `0600`, an owner other than the expected uid, a group- or world-writable parent, an empty, oversized or non-printable value. Refusals carry a fixed code only. The value is read in a frame that never raises. |
| `CredentialLedger` | Durable, append-only, hash-chained JSONL journal (`0600` in a `0700` directory, `fsync` per event, `flock` serialized). An edited, reordered, non-canonical, removed-interior or newline-less final line fails closed as `credential_ledger_corrupt`, as do forged events that break a lifecycle rule. |
| `rotate` | Issues the new reference and activates it in one lock; the previous reference is `retired` and refused locally from any process. Its locator can never be reissued. |
| `revoke`, `withdraw`, `reenroll` | One durable event revokes the references and fences every in-flight job bound to them. `withdraw` holds the lock across the snapshot and the event, revokes every unrevoked reference of the account, and blocks new references until an explicit, journaled `reenroll` by the account owner. A fenced job has no outcome until `record_outcome` records what was observed (`unknown` is a valid record). A job with a recorded outcome is never re-authorized. |
| `probe` CLI | `python -m opentour_cell.pilot.provider_credential probe` resolves one authorization in a fresh process and prints only a result code and `provider_side_refusal`. It never echoes an input. |

## Test seams

`CredentialLedger(test_clock=...)` and `ProviderCapTestSeam` are refused unless
`enable_test_seams()` was called, which itself is refused unless pytest is loaded. Production
uses `time.time` and an exact `LearningMandates`; a subclass or look-alike is refused.
`ProviderCapTestSeam` wraps a real `LearningMandates` and supplies the cap, provider account and
an in-memory cost tally that the mandate schema cannot hold today. This is a guard against
accidental use, not a security boundary.

## What this does NOT prove

* **No provider call.** No provider account, API, key, budget or usage limit is touched. The
  least-privilege credential in the customer's own account (item 1), and any provider-side
  budget, need a real customer account. A test account owned by OpenTour does not count.
* **No provider-side refusal.** The rotation drill proves only a *local* refusal: the ledger
  refuses the old reference and its locator from a fresh process. Every receipt says
  `not_attempted_out_of_scope`. Copying an old value to a new locator is caught only
  provider-side, by a provider refusing the old credential.
* **Rotation does not fence** jobs authorized under the retired reference. At the provider, both
  credentials stay valid until the old one is revoked there.
* **No provider-side revocation.** `revoke` and `withdraw` write a durable local record and fence
  jobs. They do not revoke anything at the provider, and they do not cancel a provider job.
* **A value resolved before a revocation** stays usable by whoever holds the `SecretValue`.
  Revocation only stops further resolution.
* **Outcomes are the caller's statement.** `ObservedBy.PROVIDER_REPORTED` and `evidence_ref` are
  taken from the caller and not verified against the provider.
* **The customer's secret store is not exercised.** Only a `file` locator is resolved. A
  `store_key` locator is refused with `locator_kind_unsupported`.
* **The same uid `/proc` exposure remains.** Both worker units (`opentour-cycle` and
  `opentour-worker`) run as the same uid. The managed worker can read the cycle unit's
  `/proc/<pid>/environ`, which holds the `/etc/opentour-worker/cycle.env` credentials. Reading
  at use keeps the value out of `environ`, but a process with the same uid can also read the
  secret file and may be able to read the resolving process's memory. Closing this needs a
  separate dispatch uid (the reserved `opentour-signer` user) or `PrivatePIDs` (systemd 257).
* **The ledger is tamper-evident, not tamper-proof.** Anyone who can write the ledger directory
  as this uid can rewrite the whole chain consistently, or edit or drop the final events: with
  no external anchor, removing a trailing revocation is not detected (a test pins this). An
  anchor outside this uid's reach (OPE-566 anti-rollback) is needed.
* **The value screen is a heuristic.** It reliably refuses random tokens of 20 or more
  characters, key prefixes, bare digests and UUIDs outside locators. A short or low-entropy
  secret can pass, and a bare UUID-shaped credential used as a locator segment would pass.
* **The value is a Python string.** It cannot be scrubbed from memory after use.

## Gaps against the current code

* **No standing mandate can carry a provider cap today.** `learning_mandate._normalized` requires
  `max_cost_cents == 0`, and the mandate records no provider account or cost usage. With a real
  `LearningMandates`, every dispatch holds with `mandate_cap_required`. Item 5 is blocked until
  OPE-426 adds a provider cap, provider account and cost reservation to the mandate. The
  production `LearningMandates.reserve` call is in place but unreachable until then.
* **Credential withdrawal is not linked to `LearningMandates.withdraw`.** Withdrawing the
  mandate stops `authorize_dispatch` and `resolve` (both re-read it), but it does not write a
  credential withdrawal, and a credential withdrawal does not withdraw the mandate. Linking them
  needs the owner's authority on both and is a follow-up.
* **Not wired into a connector.** `managed_connector` and `custom_trainer_connector` still take
  an opaque `credential_ref` and resolve it through their own transport.

## External gates

`EXTERNAL_GATES` in the module lists what stays open: the customer-created least-privilege
credential, a provider-side budget, the customer's secret store, provider-side refusal after
rotation, provider-side revocation, a mandate with a provider cap, account and cost usage, the
link to mandate withdrawal, and a separate uid or private PIDs between the two worker units.
