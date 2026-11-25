# Compare two decision processes on held-out cases

Use the local readout when a workflow owner wants to know how many automated decisions were wrong and how many cases still needed human review. The owner selects one repeated decision, freezes a cohort, specifies an error ceiling, and supplies each method’s decision for each case. Compare the current process with one simple repair using a reference established independently for the same case.

Before using actual records, confirm who may use them, where they can be processed, which source establishes the answer, and who may receive the report. The cohort owner checks the time cutoff and case-family separation from any development data. The command runs offline on a CPU using the supplied reference labels and decisions.

## Run the generated example

From the project root, run the command with the cell package installed or on the Python import path.

```sh
PYTHONPATH=services/cell-runtime/src python -m opentour_cell.pilot.readout \
  services/cell-runtime/examples/pilot-evaluation.json
```

Replace the example filename with a local manifest, or use `-` to read standard input. The command writes one aggregate JSON object to standard output. An unreadable or invalid input exits with code 2, writes a diagnostic to standard error and produces no report. Replaying the same manifest produces identical output. Source files are opened for reading.

Add `--report-dir NEW_DIRECTORY` to write the same aggregate as `aggregate.json` alongside an owner-readable `brief.md`. Use a new directory for each run. The portable offline kit includes its own README and a generated example.

## Input contract

The UTF-8 JSON object has exactly these three fields.

| Field | Value |
| --- | --- |
| `schema_version` | `opentour.pilot.case-manifest.v1` |
| `error_ceiling_bps` | Integer from 0 to 10000 inclusive. One basis point is 0.01 percentage points. Agree on the ceiling before evaluating the cohort. |
| `cases` | An array with one row per case, up to 50000 rows. The file or standard input may contain at most 4 MiB. An empty cohort produces an insufficient-evidence comparison. |

Each case has four required fields. `case_id` is a unique, stable identifier. `reference` is the independently established label, or JSON `null` for unknown. `current` and `repair` each use one of the following objects.

| Decision | Meaning |
| --- | --- |
| `{"status":"automatic","label":"group-a"}` | The method made an automatic decision with this label. |
| `{"status":"review"}` | The method explicitly routed the case for human review. |
| `{"status":"missing"}` | The method supplied no prediction or review decision for the case. |

Identifiers and labels use 1–256 printable characters and have no leading or trailing whitespace. Equality uses the exact supplied string. Extra fields, omitted decisions, duplicate JSON members and duplicate case IDs are invalid. Correctness depends on supplying comparable labels and the right reference for each case.

## Interpret the readout

The output version is `opentour.pilot.readout.v1`. It contains aggregate results. Each rate includes its exact `numerator` and `denominator` and a fractional `value` from division. An empty denominator produces a `null` value.

| Output | Interpretation |
| --- | --- |
| `cohort_size`, `known_references`, `unknown_references` | Counts across all supplied cases. |
| `current`, `repair` | Each method reports `automatic`, `review`, and `missing`, plus automatic decisions with an unknown reference and known `incorrect_automatic` decisions. Coverage divides automatic decisions by cohort size. Observed error rate divides known incorrect automatic decisions by automatic decisions with a known reference. |
| `observed_ceiling_met` in each method | Compares its observed error rate with the pre-agreed ceiling, inclusively, using integer counts. `null` means there were zero verified automatic decisions or some automatic decisions still lack a reference. |
| `paired` | Both methods made an automatic decision and the reference was known on every comparable pair. A fix changes wrong to correct. A regression changes correct to wrong. Ties are counted separately for both correct and both wrong. All other cases contribute to `excluded`. |
| `changes` | Coverage change, review-count delta and missing-count delta use repair minus current. `current_only_automatic` and `repair_only_automatic` count changes in which cases were automated. Equal coverage percentages can conceal these switches. Review counts measure how many cases were routed to a person. |
| `observed_comparison` | `insufficient_evidence` takes precedence if no eligible pair exists, a prediction is missing, or any automatic decision has an unknown reference. Otherwise `different_automatic_cases` marks a change in which cases were automated. When both methods automate exactly the same cases, `improved`, `regressed`, and `equivalent` describe their observed net change in errors. Individual fixes and regressions may offset one another. |

The readout describes the supplied cohort. Estimating population risk or time saved requires additional evidence. The workflow owner makes the rollout decision separately.
