# Read an observed live learning snapshot

The owner-local reader reports a current, verifiable observation for one completed
live intake run in a workflow with a scored historical benchmark. It uses the live
worker's private selection and the historical benchmark worker's existing selection.
The selected actor must already have live intake and inspection permission plus
current historical evaluation permission. A selection supplies locations and refs;
the signed live authority, standing evaluation grant and admitted runs must resolve
inside the worker.

Run the source entry point in the pinned worker environment:

```sh
python -m opentour_cell.pilot.learning_snapshot \
  --live-selection /private/tenant/live-worker.json \
  --live-run run-of-completed-feed-refresh \
  --historical-selection /private/tenant/historical-selection.json \
  --benchmark-run run-of-scored-historical-benchmark
```

The observation has version `opentour.pilot.live-learning-snapshot.v1`. Its digest
covers the tenant and engagement, both admitted run bindings, historical
benchmark and policy revisions, current live authority and the current archived
reducer evidence. The reducer has its own archive and current eligibility. This
reader does not create a second database, alter old snapshots, run a fit or open
protected answers. It observes the workflow's current committed reducer after
checking a completed live intake. The live intake receipt does not identify the
archive sequence produced by an entire source pull.

Recheck a previously observed digest by adding `--expected-digest SHA256`. The
command returns the same receipt while the same evidence and scope are current.
After a reopened correction, a changed policy or a revoked grant, the old digest
cannot authorize reuse. A held response has exit code 2 and a fixed reason.
Successful output has the digest, policy revision and counts of verified, pending
and disputed decisions and quarantined events. The output contains neither source
rows nor protected features and labels. Decision counts are not case counts,
protected denominators or eligible training examples. This is a local entry
point, and the result does not grant training or release authority.

## Acceptance and remaining boundaries

| Native requirement | Local witness or remaining dependency |
| --- | --- |
| Use the authorized source and bind the receipt to the intake-created benchmark, source and policy revisions | Actual historical worker scores parsed, authorized recorded cases. Actual signed live reader admits and materializes decision and outcome feeds. The receipt names only the policy revision and opaque digest. A draft, foreign run and unmaterialized live run cannot produce it. |
| Repeat delivery and segregate tenants and live evidence origin | The same admitted run under a second operation key and a fresh CLI process returns the same result. Two separately authorized stores use overlapping generated decisions and reject each other's run and benchmark refs. The receipt reports observed live decisions independently of historical scores and starter cases. |
| Reopening removes current credit and leaves the original as-of history intact | The actual reducer receives a reopened correction. Current verified decisions fall to zero while the previous archived reducer value remains equal. Revalidating the original digest holds. Revoking historical authority between reads withholds a receipt. |
| Maintain explicit decision-time and verification lineage for every consumed field, root and partition | The existing archive preserves recorded decision input and verification identity. A producer port is still needed to bind source-page/run publication to its exact archive sequence and to supply actual per-field consumption, independent truth time, verifier rule revision, split-family digest and purpose. Never construct those from post hoc lookup. This includes the generated refund timeout-at-10:00 and settlement-at-10:15 fixture and the historical provenance fixture with downstream consumers offline. |
| Exclude bad source joins and report distinct case denominators, incumbent errors and post-repair residuals | The actual signed live reducer owns event attribution and quarantine. A distinct-case classification and a current explicit development residual assessment are separate inputs. An absent assessment or zero denominator earns no training eligibility. The full duplicate/contradictory/late/unattested historical source-pair acceptance, current split and configured freshness/maturity remain with producer integration. |
| Training-parent manifest, optional bulk input and revocation propagation | No training-parent or augmentation manifest is emitted here. OPE-516 consumes only independently verified roots. OPE-519 owns optional bulk seams. OPE-544 and OPE-525 consume recorded forward lineage when it exists. |
| Hosted output contains allowlisted metadata only | The local output allowlist is exercised with generated fixtures. The installed task-history witness requires separate authority and the OPE-435 matrix. OPE-437's existing intake-only app contract does not publish this action. |

The tests in `test_learning_snapshot.py` exercise the source entry with generated
tenant fixtures through actual worker rights and storage. They do not establish
installed, customer or provider qualification.
