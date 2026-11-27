# Authenticated starter evaluator invocation

OPE-539's one-shot worker supplies the independently authenticated evaluator to
the normal `StarterEvaluationSession`. It does not enroll an identity, create a
grant, admit a run, create a freeze, initialize a comparator journal, fit a model,
send Slack messages or provision a host.

Acceptance map before implementation:

| Contract | Positive software witness | Adverse witness |
| --- | --- | --- |
| Implementation §1: authenticated installation, separate evaluation permission | A custodian-sealed invocation authenticates the exact evaluator; the ordinary session independently admits its existing execution and BASELINE grant | Tampered seal; other tenant, evaluator, workflow, epoch, run or freeze; changed settings or execution grant; no policy reads or predictions |
| OPE-539 / §7: normal entry and retry | Fresh Python processes run the shipped module; transport retry preserves one score and arm receipts | Stale current pointer, withdrawn invocation, unsafe file/path and extra CLI actor/run arguments deny |
| OPE-539: explicit rerun | A second separately authorized execution and separately sealed invocation on the same freeze produce one new execution | Merely renaming the first execution or replaying the earlier envelope cannot authorize a rerun |
| Current authority at reads and effects | The settings resolver rechecks the immutable invocation, current pointer and execution-grant identity throughout the existing authority guards | Withdrawal after initial construction and during runtime preflight prevents dispatch; withdrawal after an arm starts preserves uncertain state |
| Private output and evidence level | CLI emits bounded state, permitted refs and fixed reason/action only | No inputs, predictions, targets, metrics, secrets or raw exception text in stdout/stderr |

The custodian separately provisions a private selector, immutable invocation
envelope and current pointer. All files live beside the existing private settings
and `starter-intake-key.json`. The selector only locates `invocation_file`; it
grants nothing. The fixed `starter-worker-current.json` maps active invocation
references to exact canonical envelope digests. Removing the entry or creating
`starter-worker-withdrawn-<invocation_ref>.json` withdraws that invocation.

The envelope's purpose is `opentour.pilot.starter-worker-invocation.v1`. Its seal
uses HMAC-SHA256 over the canonical invocation with a key derived by HMAC-SHA256
from the existing custodian key and that exact purpose string. This separates
worker authentication from the existing execution-grant signature domain. The
invocation names its own tenant/evaluator, custodian, epoch, workflow/run,
freeze/reference/revision, execution, exact execution grant file/reference/digest,
settings basename/byte digest, invocation reference and expiry. It carries no
additional product permissions. Existing execution, source, case, model and
engagement guards still decide admission. No production signing or provisioning
command is supplied here.

Run the installed source with only the private selector:

```sh
python -m opentour_cell.pilot.starter_evaluation_worker --invocation /private/worker/invocation.json
```

The wrapper verifies no-follow, bounded owner-private regular files in a canonical
private directory. It never accepts an actor, tenant, execution or key on the
command line. A changed envelope/current pointer/settings file during an active
process is denied, including replacement with another valid invocation. Existing
execution intent and scoring journals own idempotency and recovery; the wrapper
does not reinterpret an uncertain execution as a new unscored freeze.

Generated process tests qualify this software authentication and execution entry.
The five-family generated CPU fixture is distinct from the seven-family P1 oracle,
installed customer-host witness and OPE-406/W2 actual-model evidence. Root's kit
owner separately packages this module; this source change does not deploy it.
