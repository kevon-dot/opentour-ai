# Local starter evaluation entry

`StarterEvaluationSession(settings, authenticated_actor)` joins the existing
owner-frozen pack, independently sealed evaluator/execution capability, model
registration, local CPU baseline, typed-map comparator and scoring journal.
The installed caller must authenticate the actor independently. This slice has
no CLI actor selector and never derives an evaluator from an owner or grant subject.

`run(execution_ref)` uses only that existing sealed intent. Its runtime selector
is private `starter-evaluation-runtime.json`, with schema
`opentour.pilot.starter-evaluation-runtime.v1` and a `models` map. The selected
model reference maps to exactly `artifact_file` and `receipt_key_file`, bounded
basenames beneath the existing private directory. These paths grant nothing.
The model registry remains `model-registration.json`. The registered schema
selects either the unchanged one-text-field encoding or the explicit structured
scalar encoding; the factory does not flatten or relabel frozen inputs.

The receipt key already exists as a private 32-byte file. A domain-separated
HMAC derives the comparator key with context
`opentour.starter-comparator-receipt-key.v1`. The comparator journal must already
have been provisioned using its explicit owner-local initializer. The factory
never creates keys, registrations, grants or comparator state, repairs a missing
journal, trains a model or changes a workflow. Artifact and key custody remain
installation responsibilities.

A missing runtime, baseline permission or comparator journal returns a verified
hold against the existing freeze. No result digest or metrics are invented. If
the execution journal shows that the same intent already started, or the journal
cannot be reconciled, the state remains held for reconciliation; it is not
reported as a fresh unscored execution. Retry uses the original references.
The scoring writer serializes on the existing private directory inode before
opening its DELETE-mode SQLite journal. Any WAL, shared-memory or rollback
sidecar (including a dangling link), incomplete schema or damaged existing
database holds for explicit recovery; the worker never rebuilds a restored
journal. Directory locking adds no new lock file or schema and is released
before either prediction adapter runs. Read-only Home never takes this writer lock.

`entries()` enumerates only currently authorized retained-owner navigation
metadata. `entry(execution_ref)` returns one such reference or unavailable.
Neither reads model/runtime files, policy bytes, case bodies or prediction
journals. A UI revalidates the opaque entry revision at disclosure. On an explicit
read, `summary(execution_ref, expected_revision=entry_revision)` uses the separate
retained-inspection producer (the revision keyword is optional for non-UI callers),
including independent frozen population validation. It does not call execution,
model discovery, inference or prediction lookup. Evaluator rights do not imply
owner inspection rights.

Acceptance mapping: actual owner confirmation and freeze to structured CPU model
and durable comparator; identical raw frozen inputs across both arms; exact-model
and current baseline grant; idempotent retry and explicit rerun; missing runtime,
key, model grant or comparator holds; prior unknown execution preserved; denied
owner-as-evaluator; inspection after inference withdrawal; read-only metadata
entry; no implicit provisioning, training or delivery. Fixtures use generated
pre-existing model parameters and purchasing cases. They do not replace the
separate native seven-family/four-reserved oracle, installed private UI, real model
qualification, provider evidence or customer acceptance.

| Native obligation | Scoped verification and remaining boundary |
| --- | --- |
| OPE-539 same reserved population, frozen targets and exact model/comparator | `test_actual_factory_scores_same_frozen_inputs_and_retries_without_dispatch` exercises actual owner confirmation, independent execution grant, CPU prediction and typed comparator. It compares the actual input dictionaries and independently checks fixture counts. The five generated purchasing cases are distinct from OPE-515's normative four reserved cases. |
| K3 explicit rerun versus redelivery | `test_explicit_new_execution_reuses_original_freeze` and the restart test require a new sealed execution for a second batch; redelivery preserves the result and predicts zero additional cases. |
| OPE-539 no model or inference, K4 unscored state | The parametrized missing-runtime test leaves the existing durable freeze unchanged, supplies a reconnect action, and emits no invented score or digest. The started/unknown, completed-but-runtime-missing, corrupt and empty journal controls preserve a reconciliation hold. |
| OPE-539 all-review arm | `test_actual_all_review_model_preserves_undefined_wrong_automatic` uses actual pre-existing CPU parameters, checks zero automatic coverage and an undefined wrong-automatic rate. Missing predictions and zero/unresolved denominators retain the separately reviewed scorer contract. |
| Private Home and retained readout | The metadata test forbids policy, case, model and prediction reads; the selected revision and withdrawal tests fence explicit disclosure. Actual retained summary remains readable after inference/execution withdrawal only while separate owner inspection permission remains current. Astra owns the installed UI join. |
| Current actor/source permissions and zero forbidden effects | Owner-as-worker, foreign tenant, unselected rerun, model field/schema mismatches, path/symlink, closed-WAL and withdrawal-during-prediction controls deny or hold. No fit, external delivery, model registration or workflow admission path is wired. |

This is a source-level normal worker factory, not a demonstrated independently
authenticated installed worker entry. Its caller must supply that authentication.
No command-line actor impersonation or implicit grant-subject selection is provided.
