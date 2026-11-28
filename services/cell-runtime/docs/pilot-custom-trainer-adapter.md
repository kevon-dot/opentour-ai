# Customer trainer: versioned job adapter (OPE-509)

A customer who already has a trainer connects it by implementing the public
`TrainerTransport` contract in `opentour_cell.pilot.custom_trainer_connector`
(adapter version `opentour.pilot.trainer-adapter.v1`). `CustomTrainerConnector` implements the
OPE-506 `model_connector.Connector` for `Route.CUSTOM` and is driven only through `Preflight`.
The trainer runs in the customer-controlled boundary. OpenTour never reserves or settles money
through it: the caller reserves once, `Preflight.submit_reserved_fit` dispatches once, and the
caller settles once.

## What is and is not proven

Everything here is exercised against a generated sentinel trainer
(`reference_customer_trainer.ReferenceTrainer`). That is `EvidenceTier.FIXTURE`. No real customer
trainer, GPU, paid compute, provider or credential was used. Real-trainer qualification and
customer witnessing are open external gates, and `integration_tier` refuses to rate a generated
or simulated trainer above FIXTURE however many references are claimed.

Under the owner ruling recorded on OPE-566:

- A trainer name that contains a generated, simulated, reference, fixture or sentinel token, in any
  case or Unicode-compatible form, is FIXTURE. So is a `.invalid` host name, with or without a
  port or path. This rule can only lower a tier. A real customer trainer whose name contains one
  of these tokens must be renamed.
- A customer witness is an `opentour.pilot.customer-witness.v1` record. It binds the engagement,
  trainer id, job id, artifact revision and observed outcome, and only `customer_owner` may sign
  it.
- Only the OPE-566 `PilotVerifier` may accept a customer witness, and it has not landed. Until it
  does, `classify_integration` and `integration_tier` accept no verifier. A well-formed, bound
  witness therefore classifies as SOFTWARE_QUALIFIED (`customer_witness_unverifiable`), and
  CUSTOMER_WITNESSED is unreachable from production code.

## The adapter contract

| Call | Rule |
| --- | --- |
| `describe` | Declares adapter, trainer, serializer and scorer versions, replay mode, model revision, supported tasks, and whether it can cancel, stop and hard-cap cost. |
| `quote` | The trainer's own maximum for the authorized bound. `None` means it cannot bound the job, and the fit is refused. |
| `start` | Idempotent on `client_token` (the SHA-256 of the fit intent's idempotency key). A duplicate starts no second execution. |
| `find` | Read-only lookup by client token. Never creates or resends. This is how a lost acknowledgement is recovered. |
| `poll`, `request_cancel` | Report state; stop a job. |
| `fetch_result` | A result signed with HMAC-SHA256 under the tenant credential, bound to the job, client token, trainer version and parent revision. |
| `predict` | Replay for the frozen benchmark. Deterministic, or within the tolerance declared in the registration. |

## Versions are explicit

The owner registers each destination (`TrainerRegistration`): accepted adapter versions, accepted
trainer versions, serializer and scorer versions, and replay declaration. Anything else is refused
with a fixed reason and surfaced by `check_compatibility`: `adapter_version_unknown`,
`adapter_version_mismatch`, `trainer_version_unknown`, `trainer_version_unsupported`,
`serializer_version_mismatch`, `scorer_version_mismatch`, `replay_declaration_mismatch`.
Versions are re-checked at every dispatch and again at collection. There is no coercion and no
silent degradation.

## Lifecycle and uncertainty

1. Intent is journaled before any call to the trainer (file-backed, append-only, restart-safe).
2. If `start` fails with a transport error, the next step is `find` by client token. A trainer
   that cannot confirm the job yields `UNKNOWN`. `UNKNOWN` never authorizes a retry and is never
   read as success. A retry needs a new fit intent and therefore a new reservation by the caller.
3. An unrecognized trainer state is `UNKNOWN`. A terminal state is final: a late callback or poll
   for a settled job changes nothing (`settle_callback` returns `False`). A callback is only a
   wake-up hint: its claimed state is never applied; the connector re-reads the job from the
   trainer under the server-side credential.
4. Cancel records its intent durably before the stop is sent. A canceled job never yields a
   candidate, even if the trainer finishes anyway.
5. Usage the trainer reports above the authorized bound, malformed, or in another currency stops
   the job. It is reported `FAILED` only once the trainer confirms the stop; an unhonored stop
   stays `RUNNING` with a durable cancel intent. Malformed usage raises `INVALID_USAGE`.
6. `fence(scope)` (the scope is the tenant) is durable across restart: it blocks new submit, collect and replay and stops
   live jobs. `canceled_job_refs` lists only jobs the trainer confirmed stopped for that exact
   job; if any live job (including one whose dispatch was never acknowledged) cannot be confirmed
   stopped, `fence` raises `FENCE_JOB_NOT_STOPPED` and the marker stays. `usable_target` keeps
   returning the prior pinned revision.
7. Versions are re-verified with the trainer at dispatch and at collect (never taken from the
   result). Cancel and fence do not wait on version agreement. Result signatures use a
   length-unambiguous encoding under the server-side credential; the first collected artifact is
   final.

Credentials are resolved server-side from `binding.credential_ref`, wrapped in a redacting
`Credential`, and never placed in errors, payloads or journals. A missing credential fails closed
with `credential_unavailable`. An unregistered destination is `destination_not_registered`.
Protected answer fields registered for the destination are refused in the development payload.

## Onboarding example

`ReferenceTrainer` is a complete, small customer-side implementation (a token-count classifier
whose update really changes the artifact). Copy its shape, then run the suite against your own
transport:

```python
results = run_trainer_conformance(
    TrainerHarness(make_transport, credential, registration, request,
                   complete_pending, inputs, task, model_revision),
    workdir,
)
assert all_passed(results)
```

The suite checks: declared and registered versions, idempotent start, read-only `find`, lost
acknowledgement recovery, cancel fencing, signed parent-bound results, deterministic replay,
wrong-credential refusal, and state surviving a restart. A check that crashes is a failure and an
empty result is not a pass.

## Open gates

- The OPE-507 shared definitions are in use. `Usage`, `FenceReceipt`, `EvidenceTier` and
  `UsageSource` are imported from `opentour_cell.pilot.connector_ext`, and the local mirrors have
  been removed. The shared `connector_conformance.run_conformance` suite also runs against this
  connector, alongside `run_trainer_conformance`.
- A real customer trainer has not been qualified (software-qualified tier) or witnessed
  (customer-witnessed tier).
