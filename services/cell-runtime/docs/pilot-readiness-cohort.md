# Measure readiness and verified expert effort

The cohort projection starts with already authorized workflow records and
readiness events. The caller provides a fixed seed window and evaluation
cutoff. One workflow contributes one denominator entry when its first
verified seed falls in that window. Policy-only starters and unknown seed
state have separate counts. The lifetime first-ready transition and
current disposition are reported separately, so a reopened workflow can
return to held while the earlier milestone remains in its as-of history.
Events received after the cutoff do not alter that as-of result.
Authorized holds before the first verified seed stay in the source
history. Readiness and current state for the seeded cohort come from
observations at or after the seed.

Confirmed person-time comes from session intervals. Repeated receipts and
overlapping intervals for the same reviewer count once. Distinct reviewers
contribute their own time. Shared intervals count within each arm's
standalone total and once in the overall total. An absence of confirmed
intervals yields unavailable effort.

Matched checkpoints compare real label counts only after both arms reach
the same independently confirmed target on the same comparison scope. A
positive label reduction can accompany additional reviewer work.

The guarded tests in `tests/pilot/test_readiness_cohort.py` exercise the
five-workflow cohort, restart-compatible ordering, withheld results,
reopened evidence, interval overlap and the qualified 100 versus 40 label
comparison. The projection consumes trusted receipts. The worker and
Slack integrations must separately check current reporting authority and
source retention before collecting or displaying these results.
