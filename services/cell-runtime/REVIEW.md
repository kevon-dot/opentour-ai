# Review offline evaluation exceptions

Run the command in the approved local environment with the same frozen v1 case
manifest used for the evaluation. With the cell package installed, use

```sh
python -m opentour_cell.pilot.review services/cell-runtime/examples/pilot-evaluation.json
```

Use `-` to read the manifest from stdin. The command writes JSON to stdout.
Keep this output with the permitted case files. It contains the exact input
file's SHA-256 digest and an ordered list of local case IDs.

| Worklist field | Local action |
| --- | --- |
| `review_reasons` includes `missing_independent_reference` | Look up the independent source for that case in the authorized case index. Leave the reference unknown if it cannot be established. |
| `review_reasons` includes `current_requested_review` or `repair_requested_review` | The named arm returned an explicit handoff. The designated reviewer checks that case against the applicable rule and independent source. |
| `missing_predictions` lists an arm | The operator investigates that arm's missing output. A missing output alone produces no human-review notification. |
| `paired_regression_case_ids` | Inspect the listed cases locally. Both arms automated, the current decision matched the known reference, and the repair was wrong. The number of case IDs equals `paired.regressions` in the readout for this snapshot. |

Each pending case appears once under `exceptions` and may have several reasons.
The separate list of confirmed regressions gives the operator a way to
investigate the repair using the original reference. That investigation leaves
the reference intact.

## Record a review

1. In the owner's approved environment, match the exact manifest digest and
   case ID to the authorized case index and its independent source record.
   The owner maintains that index. If the record is inaccessible or ambiguous,
   leave the case unresolved.
2. The authorized reviewer records the case ID, prior revision, decision or
   reason for leaving it unresolved, evidence reference, and reviewer identity
   in the existing review system. That system owns permissions and audit history.
3. Apply an accepted correction through the owner's existing supersession
   process or issue a separately authorized, immutable revised manifest.
   Keep the original snapshot and report. Run the evaluation on the revised
   snapshot separately, recording its digest and any held-out cases exposed
   to the review process.

## Preview an optional Slack reminder

```sh
python -m opentour_cell.pilot.review services/cell-runtime/examples/pilot-evaluation.json --slack-preview
```

This mode prints a fixed `text` message to stdout for a queue with human
review reasons. Empty queues, missing machine outputs alone, and confirmed
regressions alone yield empty stdout and exit code zero. The recipient opens the
approved review system independently. The preview runs entirely locally
and carries no case IDs, labels, digest, links, credentials or destination.
Live delivery requires a separately approved destination and integration.
