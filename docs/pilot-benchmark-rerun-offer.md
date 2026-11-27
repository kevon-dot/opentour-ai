# Review an authorized pinned benchmark rerun

The current private score viewer can read a rerun offer after the installation
operator has separately authorized an evaluator invocation and its baseline model
permission. The offer is read-only. It identifies the original scored benchmark,
its exact frozen definition, and the newly authorized execution.

The private `benchmark-rerun-DIGEST.json` envelope uses
`opentour.pilot.benchmark-rerun-offer.v1`. Compute the slot digest from the tenant,
viewer, original evaluation and new evaluation. Seal canonical grant JSON with the
starter-intake key derived using that schema as the HMAC domain. The grant binds
the exact viewer inspection revision, scope and selected invocation digest.
The custodian can withdraw an offer by creating
`benchmark-rerun-withdrawn-GRANT_REF.json` in the private worker directory.

The reader checks current owner inspection, the benchmark ledger, the sealed
evaluator invocation and the separate current baseline grant. A generated test
scores the original, reads an unchanged offer twice, then runs the separately
authorized evaluator and retries it. Two executions retain one frozen benchmark.

Private confirmation and durable admission for Slack, the definition editor
and benchmark proof actions remain open under OPE-500.

The offer reader checks the evaluator invocation, execution permission, viewer
inspection and offer again after resolving the model. It also checks the earliest
applicable expiry, including both principals’ underlying case, map and source
grants, and detects changes to the private directory or registry during
these reads. A concurrent file replacement can temporarily hide the offer until
the next read establishes a consistent view.
