# Compare registered fit intervals

A timing plan pins the model, training population, effective update budget,
hardware, runtime and clock. It registers baseline and candidate recipes, the
fit start and finish interval, at least five matched pairs in alternating
order and median, total and worst-pair limits. Cold and warm cache states use
separate plans. The independent receipt producer establishes those identities
before passing observations to the comparison.

The comparison retains every registered pair in the denominator. Missing
receipts and timeouts, out-of-memory failures, dropped examples or cap breaches
leave qualification held. Mixed hardware, overlapping fits, substituted replay
intervals and duplicate receipts are unavailable for comparison. Arm order
comes from the registered plan and observed start and finish times.

The median paired reduction, total candidate time divided by total baseline
time and maximum candidate-to-baseline pair ratio use exact rational
arithmetic. All three limits must pass. Preparation, replay, verification and
time to a verified verdict each have their own clock. Selecting a recipe also
requires the separately registered quality, cost and critical-slice evidence.

The generated arithmetic fixtures exercise the native 20% boundary and the
tail regression with a 4.48 total ratio. They measure qualification logic.
The worker needs actual authorized, current matched receipts before showing a
numeric fit-speed comparison.
