# Open-weight connector and model-family qualification

The open-weight connector is the local route behind the shared model connector contract
(`services/cell-runtime/src/opentour_cell/pilot/model_connector.py`, OPE-506). It trains and serves one
pinned model family on the customer's own approved machine. It never reserves or settles money: the
caller owns the one reservation and the one settlement for a fit intent and dispatches only through
`Preflight.submit_reserved_fit`.

## What is where

| Module | Role |
| --- | --- |
| `pilot/model_connector.py` | The shared contract (OPE-506, not edited here). |
| `pilot/connector_ext.py` | Evidence tiers, measured `Usage`, `FenceReceipt`, optional `UsageReporting` and `Fenceable`. |
| `pilot/model_family.py` | Declarative `ModelFamilyProfile`, strict loader, directory verification, `ProfileRegistry`, `QualificationRecord`. |
| `pilot/open_weight_connector.py` | `OpenWeightConnector`, the durable job journal, `SubprocessFamilyRunner`. |
| `pilot/open_weight_reference.py` | Wraps the qualified SmolLM2 decision adapter (`open_weight.py`) as a family runner. |
| `pilot/connector_conformance.py` | `run_conformance` (any connector) and `qualify_family` (one family). |
| `training/profiles/families/*.family.json` | The checked-in profiles. |

Placement: control-side logic lives in `services/cell-runtime` beside the connector contract and the existing
`open_weight.py`, because it must stay stdlib-only and importable by the worker. Numeric execution stays in the
separate training project and private interpreter, exactly as for the reference adapter.

## Lifecycle and recovery rules

* `submit` is idempotent on the intent's idempotency key. The journal records `accepted` before any process starts,
  then `started`, then one terminal event (`completed`, `failed` or `canceled`). A duplicate dispatch returns the
  existing receipt and starts nothing. The same key with a different intent is refused (`IDEMPOTENCY_CONFLICT`).
* A job with no terminal event whose owning process is gone is reported `FAILED`
  (`INTERRUPTED_BEFORE_COMPLETION`). It is never re-run. A retry needs a new fit intent and a new reservation.
* `lookup` and `status` are read-only. An unreadable journal reads as `UNKNOWN`.
* Bounds are enforced twice: `Preflight` compares the profile's estimate to the owner's bound, and the connector
  refuses a bound below its estimate before any dispatch (`BOUND_EXCEEDED_BEFORE_DISPATCH`). The wall-clock bound is
  enforced by the supervisor, which kills the worker; a job that overruns is `FAILED` and publishes nothing.
* `cancel` writes a durable marker first. The supervisor and the publish step both honor it, so a canceled job never
  publishes a candidate. Cancel after completion changes nothing.
* `fence` is durable, stops live jobs, and blocks new submits, collection, replay and serving. Reads stay available.
* `promote`, `rollback` and `serve` keep a durable serving pointer. Rollback restores the previous authorized target and
  ultimately the unmodified pinned base.
* No automatic substitution: `observe` reports the revision that is actually installed. A binding for another
  revision, task or formatter is held (`NEEDS_CONFIGURATION`), never swapped for the reference model.

## Add another compatible model family

1. Choose a permitted, genuinely different architecture. Record its license from the publisher.
2. Copy a file in `training/profiles/families/` and fill in every field: exact publisher revision, SHA-256 of every
   file the numeric worker reads, tokenizer file, task, data formatter id, tuning method, configurable layer targets,
   resource estimate (the connector enforces these as the ceiling), scorer id and the shared benchmark contract.
   Leave a field `null` or empty if it is not yet verified: `pin_gaps` lists it and the family cannot qualify.
3. Stage the files in a private directory and check them with `model_family.verify_model_directory`.
4. Provide a worker for the family (one JSON request on stdin, one JSON reply on stdout; `fit` returns the adapter
   and evidence with `parameters_changed`, `base_sha256_before` and `base_sha256_after`; `predict` accepts
   `adapter: null` for the base). `SubprocessFamilyRunner` supervises it. Do not install CUDA in the control workspace.
5. Register the family with `ProfileRegistry` under the owner's model reference.
6. Run `connector_conformance.run_conformance` and `qualify_family` on the staged installation. Only a pinned, non-generated profile
   with `FixtureKind.INSTALLED_MODEL` is recorded at `SOFTWARE_QUALIFIED`. `QualificationRecord` refuses a tier above
   `FIXTURE` unless its own fields are mutually consistent (see "What qualification does and does not establish"). Both
   families must use the same scorer and benchmark contract (`require_distinct_families_on_one_benchmark`).

## What qualification does and does not establish

Qualification is caller-asserted and digest self-consistent only. There is no signing contract: nothing signs a
`QualificationRecord`, no key is held for it, and no authority confirms it. The record is a consistency check over
fields the caller supplied, not a verification of any run.

What a record above `FIXTURE` does enforce, at construction (`EVIDENCE_TIER_UNSUPPORTED` otherwise):

* `fixture_kind` is `INSTALLED_MODEL`, and no `profile_pin_missing:*` gate is listed.
* The family id is an exact `str` (a subclass is refused) and not a fixture name (`is_fixture_family_id`, the same rule
  as the OPE-509 trainer downgrade). The id is NFKC-normalised and casefolded. An id with invisible, control or non-ASCII
  characters is a fixture. So is an id that CONTAINS a `generated`, `simulated`, `reference`, `fixture` or `sentinel`
  token, or names a `.invalid` host, so `Generated-x`, `generatedx`, `regenerated-x` and `acme-reference-linear` are all
  refused. The rule can only lower a tier: a real customer family whose id contains a token must be renamed.
* `revision` is an exact 40-hex `str`.
* `results` holds exactly one boolean outcome for every check the record's role requires, with no duplicates and no
  unknown names. An empty or partial result set is refused and can never read as qualified; `passed` and `qualified`
  re-check coverage.
* `PROVIDER_WITNESSED` carries a `witness_ref`: one exact `str` token of letters, digits and `._:/#@-`, at most 256
  characters, starting with a letter or digit.
* `CUSTOMER_WITNESSED` is unreachable. Under the owner ruling recorded on OPE-566 it needs an
  `opentour.pilot.customer-witness.v1` record that only the OPE-566 `PilotVerifier` may check, and that verifier has not
  landed. A record is therefore `SOFTWARE_QUALIFIED` at most. A `CUSTOMER_WITNESSED` claim that is otherwise consistent
  is refused with `CUSTOMER_WITNESS_UNSTRUCTURED` whatever `witness_ref` says, because a string is never a witness record.

What it does not establish:

* `witness_ref` is an unverified opaque string. Its shape is checked; nobody confirms that a witness exists or what it saw.
  It never raises a record to `CUSTOMER_WITNESSED`.
* Check outcomes, the revision and the digest are asserted by the caller. A record proves internal consistency, not that
  a run happened.
* Frozen dataclasses are not tamper-proof. `object.__setattr__(record, "tier", ...)` changes a frozen record, and the same
  holds for `Usage`. This is inherent to Python and is not defended against here. A consumer that needs authority must
  take it from a signed or journaled source outside this module.

## Qualification matrix (current)

| Family | Pins | Tier reachable now | Real fit | Notes |
| --- | --- | --- | --- | --- |
| `smollm2_135m_lora_v1` (reference, Llama-style) | complete | FIXTURE in this workspace | Not run here | Needs staged weights and the pinned numeric interpreter. |
| `pythia_160m_neox_candidate` (GPT-NeoX-style) | revision, license and all file digests missing | FIXTURE only | Not run | Candidate chosen as a different architecture. No numeric worker yet. |
| `generated-toy-linear-v1`, `generated-toy-normmlp-v1` | generated | FIXTURE | Real update on generated parameters only | Software conformance of the harness. Not a model. |

A fixture result never satisfies a provider, installed-model or customer gate.

## Known contract gaps (recorded, not invented)

* `Preflight` has no distinct "credential missing" reason. A connector that cannot obtain a credential raises
  `LookupError`, which `Preflight` reports as `model_capability_evidence_unavailable`. A distinct reason needs a change
  to `model_connector.py` (OPE-506).
* `fence` writes its durable marker first. If a live job cannot be confirmed stopped within `cancel_wait_seconds` it
  raises `FENCE_JOB_NOT_STOPPED` instead of returning a receipt that claims the job was canceled. Jobs are visited in
  sorted order, so when the first stuck job raises, later live jobs do not receive their `CANCEL` marker during that call.
  They stay protected by the `FENCED` marker (new work, collection, replay and serving are refused), and calling `fence`
  again retries. This is documented behavior, not a defect fixed here.
* `usage` returns `None` (unknown, never zero) for a `FAILED` or `CANCELED` job, because the journal records wall time,
  rows and steps only on completion. A corrupt or malformed `completed` event also reads as `None`. Only a `COMPLETED`
  job reports `MEASURED` usage.

### Precondition on the caller

The guarantees above assume `QualificationRecord` and `Usage` objects do not cross a trust boundary: they are built and
read inside one trusted process by code that does not forge them. There is no signature and no key custody in this
project, so the ceiling is caller-asserted and digest self-consistent. If these objects are ever stored, sent or accepted
from another party, they need a signing contract first. The field checks above are a real floor against malformed or
inconsistent records; they are not proof that any run happened.

`Usage.settleable` and `UsageSource` are currently consumed by nothing in `src`. The type records the settleable/estimated
distinction, but no caller acts on it, so any future settlement path must check it. `Usage` also accepts an unbounded
`incurred_minor_units` and has no format check on `currency` or `job_ref` (known limits, not fixed here).

## Error codes

Besides the connector's own refusal codes, these are raised by the shared value types:

| Code | Raised by | Meaning |
| --- | --- | --- |
| `INVALID_USAGE` | `Usage` (`ValueError`) | Unknown `source`, empty or non-string `job_ref` or `currency`, a negative or non-`int` count, or a non-`bool` `irreversible`. |
| `INVALID_EVIDENCE_TIER` | `QualificationRecord` (`FamilyError`) | `tier` is not an `EvidenceTier` member (a plain string is refused). |
| `EVIDENCE_TIER_UNSUPPORTED` | `QualificationRecord` (`FamilyError`) | The record claims a tier above `FIXTURE` that its own fields cannot support: wrong fixture kind, a fixture-named or non-`str` family id, a non-40-hex or non-`str` revision, missing or partial check coverage, a missing-pin gate, or a missing or malformed `witness_ref`. |
| `CUSTOMER_WITNESS_UNSTRUCTURED` | `QualificationRecord` (`FamilyError`) | An otherwise consistent record claims `CUSTOMER_WITNESSED` with a `witness_ref` string. A string is never an `opentour.pilot.customer-witness.v1` record, and only the OPE-566 `PilotVerifier` (not landed) may accept one. Mirrors `customer_witness_unstructured` in `custom_trainer_connector`. |

## Open items (recorded, not fixed here)

* Frozen dataclasses cannot be made tamper-proof (see above).
* `Usage.incurred_minor_units` has no upper bound (`10**30` is accepted), and `currency` and `job_ref` have no format check
  beyond being non-empty strings.
* Nothing in `src` consumes `Usage.settleable` or `UsageSource` yet, so the rule that estimated usage never settles is not
  enforced by any caller.
