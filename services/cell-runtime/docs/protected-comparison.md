# Compare an incumbent, a simple repair and an optional model

The custodian selects one recurring decision and one field from decision-time inputs. The source intake records the allowed features and slices, independent corrections, cohort split, source versions and exclusions. The workflow owner chooses the audience, the minimum automated coverage and the maximum wrong-automatic rate before reviewing protected answers. The terms use integer basis points. Both bounds include exact equality.

## Select the repair on development data

An owner-declared rule maps exact values of one allowed feature to labels. A learned rule requires training permission. It gives each independent development group one consistent vote per observed value. A tie or insufficient support takes the agreed fallback. Choose `review` for an unrecognized value, or `incumbent` to retain the existing decision. When the incumbent is needed and its output is missing, the missing output remains visible.

The managed repair route accepts up to 5,000 records per source export. The development assessment holds one group out at a time for a learned lookup. The final lookup uses the authorized development partition. A simple repair that meets its limits with paired fixes and no regression avoids a model fit. An equivalent incumbent can be retained. A model is eligible only when the same residual error recurs in enough independent development groups and the operator has separately authorized data, model use and compute. Both permission references and the actual fitted model revision enter the managed workflow. When development evidence is inadequate, the operator can return a reasoned hold and keep protected answers sealed.

## Freeze and compare

The comparison plan fixes the organization, engagement, intended audience, intake revision, protected membership, incumbent and executable rule, development assessment and limits. For the model route it also binds the trained artifact, compute authorization and predictions made from decision-time features. Registering another rule, threshold, audience or model against already-consumed cases requires a fresh protected cohort. A same-plan restart can recover the original evidence.

The private report gives the verified wrong-automatic rate, automatic and review counts per 100 eligible cases, observed material automatic errors per 1,000 eligible cases, and unknown, missing and paired counts. Set `material_slices` to the permitted segments where an error is material, or leave it empty to count all verified wrong automatic decisions. Unknown automatic references are printed separately. Equal coverage percentages can describe different cases, so compare the reported intersection and the actual case-set change before interpreting an improvement.

For independent verified automatic cases, the report includes a 95% Wilson interval on the observed error proportion and marks fewer than 30 as a small sample. Repeated families make this case-level interval unavailable. Each named critical slice must have the required independent groups, meet its limits and show no paired regression or changed automatic subset. An empty or inconclusive slice holds that candidate. An observed rate and its interval describe this particular cohort.

The protected evaluator recommends a passing improvement. It retains the incumbent on a qualifying tie or returns a hold for incomplete evidence or an unapproved quality/cost trade-off. The permitted recipient may replay the frozen evidence. The workflow owner makes and records the separate operational decision.

## Supply optional timing and cost measurements

Place the measurement JSON in the authorized private directory and bind its digest to the managed plan. Use the method names `incumbent`, `simple` and, when selected, `model`. Each measured arm specifies the provenance and permission references, hardware, currency, unit, unit rate, UTC Unix-second meter window, and observations keyed by the same eligible case IDs. The following fragment is generated.

```json
{
  "incumbent": {
    "basis": "generated",
    "permission_ref": "generated-permission",
    "provenance_ref": "generated-meter",
    "hardware": "generated-cpu",
    "period_start": 100,
    "period_end": 160,
    "currency": "USD",
    "unit": "cpu_second",
    "rate_per_unit": "0.02",
    "cases": {
      "generated-case": {"review_seconds": "6", "latency_ms": "35", "usage": "0.50"}
    }
  }
}
```

An arm may report each field independently. Enter an observed zero when no review or usage occurred. An absent or null observation makes that measure unavailable for the whole-cohort normalized rate. All eligible cases, including abstentions and missing predictions, remain in the denominator. Review time appears in minutes per 100 and per 1,000. Latency appears as mean milliseconds and total milliseconds per 1,000. Cost per 1,000 is total measured usage multiplied by its unit rate and by 1,000, divided by the eligible count. The gate compares exact quantities before display rounding.

## Review the benchmark in Slack

In the private evidence view, the named reviewer can compare the incumbent with the proposed rule or model on the same frozen population. Decision quality, coverage, comparable pairs and regressions sit alongside the number routed to people per 100 eligible decisions. When the approved meter has complete values, the view also shows review minutes per 100 decisions, mean workflow latency in milliseconds and cost in the declared currency per 1,000. An incomplete arm shows how many eligible cases have a measurement. A different currency, unit, period or measurement basis leaves a cross-arm comparison unavailable.

The 95% interval describes verified automatic errors on independent cases. Small samples are labeled. Correlated groups or zero verified automatic cases leave this interval unavailable. The evaluator's ready-to-result clock describes its own execution. A protected benchmark does not establish realized post-adoption benefit. That requires a separate approved production comparison and customer witness. No production observations are collected by this view.

Use `observed` only for authorized measurements from the actual workflow and identified meter. `modeled` identifies estimates. `generated` identifies fixtures. Savings require comparable observed records in the same currency, unit and period. Comparisons with different measurement bases remain unavailable. A configured cost or latency ceiling needs complete observed evidence for the relevant arm. The private stage timestamps record evaluator execution. Workflow timing and commercial outcomes require their own evidence.

Private per-case predictions, independent answers and material exposures remain in owner custody. The team’s Slack and MCP results project the authorized aggregate. For handoff, retain the frozen rule or compatible model, the receipt, and the authorized fallback alongside the customer’s proof.
