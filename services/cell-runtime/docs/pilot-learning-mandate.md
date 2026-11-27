# Enroll a standing learning mandate

After the customer has enrolled its organization, engagement and read-only MCP connector, the customer owner can approve recurring work on one workflow. The worker can inspect new source revisions, confirm only the named recurring error pairs and fit a local candidate within the limits. A customer reviewer still decides whether to approve the exact result presented in Slack. The mandate grants zero publication or production-routing authority.

Copy `templates/pilot-learning-mandate.template.json` from the kit to the customer’s private directory. The source version is `services/cell-runtime/examples/pilot-learning-mandate.template.json`. Enter the owner and worker identities, permitted trace and independently verified outcome source, connector allowlist, decision field, decision-time inputs, allowed labels and error direction. Match the digest of the customer-approved operating agreement used by the autonomous cycle. Enter a future Unix expiry, an independent Slack reviewer and private audience, the named payer and owner-local destination. Retain the file with mode 0600 in a mode-0700 directory.

The enrolled field list controls which decision-time information the worker can inspect. Optional `model_fields` can narrow what the local pretrained model receives. Set this projection before the protected cohort is opened. A new projection requires a new prospective mandate and fresh protected cases.

From the unpacked kit, run the following owner-local commands with the same directory and engagement used for the customer’s MCP onboarding.

```sh
python -I -S -B opentour-slack.pyz learning-mandate enroll --directory /private/engagement \
  --organization generated-org --engagement generated-engagement --owner generated-owner \
  --spec /private/engagement/standing.json

python -I -S -B opentour-slack.pyz learning-mandate status --directory /private/engagement \
  --organization generated-org --engagement generated-engagement --actor generated-owner \
  --mandate first-mandate
```

First enrollment pins the local operator’s registered owner for the engagement. Customer-controlled local filesystem access authenticates this setup step. The MCP agent lacks an enrollment or limit-increase tool. Subsequent operations must match that owner or the recorded worker identity.

Enroll only against the exact active engagement in the customer’s `training.sqlite3`. The customer custodian’s engagement closeout stops standing source admissions and fit reservations and claims before reconciliation. An older restored training or mandate file stays held while the current custody ledger survives. The owner can still inspect historical usage and settle an already-claimed fit without recording an artifact.

The ledger counts a unique admitted source pair once and reserves compute before fitting. A model attempt consumes one fit and its full reserved seconds. A timeout, unknown completion or interrupted process retains those charges. Retrying the identical source/reservation returns its existing identity. A completed artifact can be replayed without training again. A successful rule comparison spends zero fit budget. The model grant and customer source/class confirmations record delegated provenance and bind the original mandate. Status reports runs, rows, reserved fits and seconds.

A local fit reservation binds the current mandate revision and pinned model
profile to one exact intake snapshot, comparison plan, residual and compute
limit. Equivalent calls reuse the original reservation and admitted run.
Worker adapters may attach a retry key scoped to the authenticated client and
delegate. The alias and reservation commit together. A changed intent under
the same client key conflicts. If restored history contains competing
reservations for one canonical fit, reconciliation is required before
reserving or reporting a match.

Withdraw the current mandate to stop new work, prevent later grant replay and signal any active bound model fit. A fresh owner grant can supersede the withdrawn or expired mandate while preserving the earlier budget ledger.

```sh
python -I -S -B opentour-slack.pyz learning-mandate withdraw --directory /private/engagement \
  --organization generated-org --engagement generated-engagement --owner generated-owner \
  --mandate first-mandate

python -I -S -B opentour-slack.pyz learning-mandate reauthorize --directory /private/engagement \
  --organization generated-org --engagement generated-engagement --owner generated-owner \
  --supersedes first-mandate --spec /private/engagement/new-standing.json
```

To keep the workflow manual, set `mode` to `manual`. To authorize automatic evaluation and rules alone, keep `mode` as `autonomous`, use `["inspect","evaluate"]`, set `profile` to null, omit `model_fields` and set every model fit/time cap to 0. Both modes preserve human release approval.
