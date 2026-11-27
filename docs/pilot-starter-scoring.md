# Frozen starter evaluation core

`StarterScoring` compares a selected local model adapter and a declared comparator
on the same reserved, owner-adjudicated starter families. It does not fit a model,
change a benchmark definition, create a release or count generated cases as live
evidence.

The private confirmation producer must separately authorize the named evaluator
under its `evaluate` purpose. Materialization, owner editing and model discovery
are different permissions. `StarterCases.evaluation_inputs` validates the current
freeze and family reservations and returns input-only records. The answer key is
available only through the separate evaluator target method. Scalar input types
are checked both when proposing cases and when reopening persisted packs.

The evaluation authority supplies an explicit execution identity, exact freeze,
plan and comparator revision, model identity, baseline grant revision, allowed
fields, row bound and expiry. A new transport key cannot create a rerun. The
authority must separately admit a new execution against the unchanged freeze.
Its metadata-only `revalidate` method must observe current revocation, actor
binding, source epoch and intent revision under the held guard. The worker calls
it after lookup and at journal commit boundaries; returning a captured capability
without checking current state does not satisfy this port.
The normal model registry must still contain a current baseline-inference grant.
This initial adapter accepts local execution with exact observed model identity;
remote execution needs a separately bounded processor/disclosure adapter.

The prediction port receives opaque per-freeze case handles and decision-time
fields only. It never receives owner labels, author suggestions, case/family
names or adjudication reasons. Both arms receive identical input bytes. The
worker persists the operation before calling the adapter. After uncertain
acceptance it looks up that exact operation; an unknown result stays unscored
and cannot blindly trigger another execution. Cached predictions are compared
with the independently reconciled adapter receipt before scoring.
An execution response must already be queryable through lookup before its first
score can publish. An automatic action labeled REVIEW is a malformed response
under the frozen review policy and cannot inflate automation coverage.

The evaluator opens the answer key only after both prediction receipts exist.
Per-case output preserves generated origin and separates automatic, REVIEW and
missing predictions. Correct REVIEW counts as exact action agreement; it does
not increase automation coverage. With no automatic decisions the wrong-automatic
denominator is zero and its value is unavailable. Fixed/regressed counts use
the same paired cases and report missing-output exclusions.

`LocalStarterBaseline` provides a durable adapter for the existing `cpu_text_v1`
profile. It loads pre-existing owner-private parameters and verifies their exact
SHA-256 against the registered immutable model revision. It never trains,
accepts a fitted-candidate substitute, or sends a network request. Its one text
field, classification schema and action output schema must match the registry
and current baseline grant. It rechecks evaluator authority and the actual frozen
input projection before each bounded prediction and receipt commit. A REVIEW
label remains an abstention, including when selected by model parameters.

The separately registered `starter-structured-fields-v1` input schema accepts
the complete authorized scalar field map when `input_field=None`. It applies
canonical JSON serialization before the unchanged CPU feature extractor; field
names, missing values, and decision-time values remain explicit. Every field
must be in the current execution and model grants. The existing one-text-field
schema keeps its original encoding. Both profiles retain the 48-token and
128-case bounds. This encoding does not add targets, suggestions, case identities,
or a policy classifier. Its tests prove byte fidelity and receipt reuse using
generated parameters, not the native seven-family end-to-end benchmark scores.
Receipts bind the input encoding as well as the model artifact. Changing a
registered schema holds an earlier operation instead of reinterpreting its
predictions; a separately authorized new execution is required. Historical
single-field receipts remain readable only under the unchanged single-field
encoding.

The adapter stores authenticated receipts in `starter-local-predictions.sqlite3`.
The custodian supplies a separate private 32-byte receipt key; the worker does not
create or distribute that key. Changed inputs, model bytes, scope or operation
identity are rejected. Restart and lost acknowledgment reconcile the exact
receipt, and concurrent callers share one committed local prediction batch.
Lookup does not create a journal or sidecars. The model, receipt key and journal
must be included in the installed custody inventory before this route is wired.
Tests use generated pre-existing CPU parameters, with no training or actual-model
qualification claim. The comparator remains a separate typed port.

This module is a backend core, not a complete installed route. The current
owner-confirmed OPE-538 map producer, explicit evaluator/execution capability,
normal factory/custody integration and private read-only Home composition are still needed
for normal-entry integration. Generated deterministic adapters test the required
four-case scores and recovery protocol; they do not claim a real selected-model
or customer evaluation. OPE-406/W2 owns the separate actual-model witness.
