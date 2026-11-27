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
6. Run `connector_conformance.run_conformance` and `qualify_family` on the staged installation. Only a pinned profile
   with `FixtureKind.INSTALLED_MODEL` reaches `SOFTWARE_QUALIFIED`. Both families must use the same scorer and
   benchmark contract (`require_distinct_families_on_one_benchmark`).

## Qualification matrix (current)

| Family | Pins | Tier reachable now | Real fit | Notes |
| --- | --- | --- | --- | --- |
| `smollm2_135m_lora_v1` (reference, Llama-style) | complete | FIXTURE in this workspace | Not run here | Needs staged weights and the pinned numeric interpreter. |
| `pythia_160m_neox_candidate` (GPT-NeoX-style) | revision, license and all file digests missing | FIXTURE only | Not run | Candidate chosen as a different architecture. No numeric worker yet. |
| `generated-toy-linear-v1`, `generated-toy-normmlp-v1` | generated | FIXTURE | Real update on generated parameters only | Software conformance of the harness. Not a model. |

A fixture result never satisfies a provider, installed-model or customer gate.
