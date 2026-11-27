# Historical benchmark worker producer (OPE-505 follow-on)

`LocalHistoricalBenchmark(selection, traces, corrections, agreement)` composes
the actual local historical authority, standing-mandate ledger, source-intake
freeze and managed protected comparison. These paths are worker configuration;
the intake wire does not accept them or any authority assertions.

The existing selected historical grant must authorize the exact actor,
engagement, workflow, source pair/revisions and evaluation purpose. Its standing
mandate pins the complete measurement agreement digest. The private agreement
uses `opentour.pilot.historical-benchmark-policy.v1` and contains the incumbent
revision, existing comparison terms and an explicitly declared rule. Coverage,
error and development/verified/residual/slice thresholds must be present. This
producer does not infer missing thresholds or fit a rule.

For baseline measurement alone, the standing agreement instead uses
`opentour.pilot.historical-baseline-policy.v1` with an incumbent revision and
measurement terms. It has no comparator or development/residual thresholds.
Its different policy digest creates a distinct authorized intake intent. The
result uses separate recorded-baseline plan/result schemas, contains only the
incumbent arm and an `observe` decision, and cannot claim improvement or
training eligibility. Zero development cases do not block an authorized baseline
when protected case support and recorded predictions are sufficient.

The producer freezes actual joined independent outcomes and grouped source
partitions, then uses the preregistered declared comparator and recorded incumbent
decisions in the existing protected comparison worker. Its manifest contains no
model profile, fit, Slack publication or release fields. Source grants and the
standing mandate are rechecked before and after execution. Missing predictions
or case support produce typed holds without a benchmark score.

The operation reference determines private output identity. Recovery verifies the
stored manifest, current selected source grant, frozen source marker, plan,
completed worker receipt and actual protected comparison registry. It never
reruns scoring. A registry mismatch or withdrawn mandate cannot publish a result.
Only opaque definition/result references and their verified digests enter the
benchmark ledger; protected rows remain inside the existing worker records.

The generated tests use real mandate enrollment/source authorization and actual
source/worker/SQLite implementations. The positive fixture reconciles four
protected cases, two recorded incumbent errors and zero declared-comparator
errors, with zero jobs, train-data rows, fit reservations, model artifacts or Slack
receipts. Tests also cover completed retry, crash/readback, missing predictions,
missing thresholds, policy change, withdrawal and restored registry corruption.
Baseline tests measure sixteen protected cases without a comparator or development
population, preserve a separately assessed repair hold, reject fabricated
eligibility on readback, and deny reuse of the observed cohort for a new repair
comparison. Both intents use the actual protected case/family reservation ledger.
Generated records are software evidence, not a customer or provider result.

This is still a scoped backend follow-on. It does not install the normal worker
factory or refresh loop, render Slack, supply starter adjudication/scoring, or
complete OPE-505. The existing managed comparison still holds at its development
gate. Only an explicitly authorized recorded-baseline intent can measure the
incumbent independently; it does not remove that hold or authorize a fit. The
normal worker must surface typed holds and reconcile unknown in-flight operations. No
unobserved operation is automatically retried.
