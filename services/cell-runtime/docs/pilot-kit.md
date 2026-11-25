# OpenTour pilot operator guide

Use this kit to compare an incumbent and one proposed repair on one frozen cohort. The operator supplies both methods’ case-aligned outputs and independently established references. When candidate predictions are missing, the named operator runs the agreed repair in the authorized environment and freezes its outputs before scoring.

## Run the generated example

Use Python 3.12.16 on the approved machine. Check the outer ZIP’s SHA-256 against the digest received through the approved support route, then unpack it into a new directory. The kit uses the Python standard library and runs with networking off.

In the extracted directory, run one command.

```sh
python -I -S -B opentour-pilot.pyz --report-dir sample-run example.json
```

Open `sample-run/brief.md` for the workflow owner and `sample-run/aggregate.json` for the exact counts. Both files should match the included `example-readout/` versions byte for byte. For a replay, choose a new output directory, such as `sample-rerun`. A malformed manifest returns exit 2, writes a diagnostic to standard error and produces no report. A pre-existing output location is refused.

The Slack workspace kit includes the same scorer under `evaluate`. From that extracted kit, run `python -I -S -B opentour-slack.pyz evaluate --report-dir sample-run generated/readout-input.json`. Compare the two outputs with `generated/expected-supplied/`. Its `README.md` covers local enrollment, training, Slack setup and custody.

Use a private parent directory. On POSIX the operator receives an owner-only report directory. An interruption while reserving its name can leave an empty directory. Choose a fresh directory for the replay and use the report only when both files are present. The release-candidate exercise used Linux x86_64. Verify the local replay on the recipient’s approved platform.

To obtain only the aggregate JSON on standard output, run `python -I -S -B opentour-pilot.pyz example.json`. Replace the filename with `-` to read the same bounded JSON from standard input.

## Run one approved workflow

1. Use a separate owner-approved working directory for each organization. Keep source records, the intake receipt and owner decisions there. Confirm the named operator and reviewer, the governing permission, method versions, holdout and pre-agreed operating point.
2. Follow `manifest-guide.md` for the exact fields. Represent unknown labels, missing decisions and explicit human review separately. Record upstream exclusions and unmatched case counts in the authorized private intake record.
3. Substitute the approved manifest in the example command and choose a fresh output directory. Review the fractions and paired counts together. Ask the recipient to rerun or witness the comparison. The workflow owner records their own decision and next action in the approved location.

## Make a private intake and replay receipt

Keep one receipt for the engagement in the owner's approved record system. The same opaque organization key follows a revised report, an additional workflow and a return engagement. The operating schedule allows one active pilot at a time.

| When | What the operator records privately |
| --- | --- |
| Before opening the held-out answers | Organization and engagement reference, workflow owner, custodian, permitted operator, recipient and support route. Record the recurring decision, who can verify the answer, the decision date, allowed processing location, fields, purpose, recipients, retention and any external destination. Record training permission separately when requested. |
| Freeze the comparison | Case unit, time window, family grouping, cutoff, exclusions with counts, missing or ambiguous joins and how independence was verified. Record the source and version of the incumbent, repair and reference labels, cohort and configuration digests, required coverage and maximum wrong-automatic rate. A missing prediction stays explicit. Resolve or hold a contaminated cohort before use. |
| Run and independently repeat | Kit revision and ZIP digest, exact command, frozen input or model and cohort digests, output digests, permitted recipient, date and time, and which person ran or witnessed the fresh computation. Record any discrepancy and its disposition. |
| Review with the owner | Automated decisions and verified errors with denominators for each arm, review and missing burdens, coverage, the intersection of decided cases, paired fixes/regressions, operating ceiling, limitations and observed operator effort. Keep estimated savings distinct from measured time or cost. |
| Record the decision | Owner, evidence revision, date and explicit continue, revise, collect evidence or stop. Include the next responsible person, next date or explicit closeout. The named reviewer's Slack presentation receipt can be attached separately. |

For supplied decisions, run the same approved manifest twice in separate processes and fresh directories. Verify that both members match byte for byte.

```sh
python -I -S -B opentour-slack.pyz evaluate --report-dir run-1 approved.json
python -I -S -B opentour-slack.pyz evaluate --report-dir run-2 approved.json
cmp run-1/aggregate.json run-2/aggregate.json
cmp run-1/brief.md run-2/brief.md
```

For a trained candidate, the operator gives the recipient the frozen job reference, cohort, artifact digest and approved local workspace. Use the workspace kit's `data` command and `score-only --job` on a new process to load those weights and score the held-out inputs. An enrolled evaluation-only dataset uses `score-only --dataset`. See the workspace kit README for exact commands. Compare the recomputed report to the approved result. A different revision, revoked permission, missing artifact or a mismatch holds the decision and requires investigation.

## Count customers and close each engagement

1. A recipient has independently rerun or witnessed an actual comparison, and the authorized owner has recorded a dated decision against the same revision. Enter one valid acceptance for that organization in the existing private register. An accepted stop can count. Invitations, synthetic exercises, a delivered report alone or a Slack presentation approval leave the acceptance count unchanged.
2. A disputed receipt loses its credit while the owner investigates. Preserve its permitted history. A valid replacement can restore one credit to the same organization. In a generated register rehearsal, an accepted first organization, a duplicate, a dispute, its valid replacement and a different accepted organization yield running totals of 1, 1, 0, 1 and 2. A pre-readout withdrawal leaves the count unchanged.
3. Record distinct accepted organizations, installed-and-used Slack organizations, authorized completed training jobs and organizations, observed improvement at the agreed coverage, and evidenced paid commitments separately. Close the cohort on ten distinct valid customer receipts.
4. On a withdrawal or deadline, stop access and pending handoffs for that engagement. Inventory source data, models, output files, intake/decision records and recipient or backup copies that were used. Give each item an owner, agreed disposition, due date and confirmation or unresolved obligation. Track support independently. Use the workspace kit's custody commands for its managed local store.
5. An account owner follows up from a current decision receipt. Record a declined improvement, a request for more evidence, an explicit stop and a paid commitment as different outcomes. An unanswered message or a pricing conversation stays unconfirmed. Pause dependent claims if the receipt is disputed. Obtain separate scope, costs, permission, site and a release owner before further work or rollout.

## Version, integrity and support

Run `python -I -S -B opentour-pilot.pyz --version` to print the kit version and exact source commit. `PROVENANCE.json` identifies the build, required Python and generated example. `SHA256SUMS` lists the SHA-256 of every other member. Systems with the standard checksum utility can run `sha256sum -c SHA256SUMS` from the extracted directory.

Contact the named pilot operator using the private support route agreed at intake. Provide the kit version, source commit, digest and a redacted error or generated reproducer. Keep customer cases, labels and credentials in their approved location.

If the revision, calculation or permission is uncertain, hold the handoff. Keep the permitted receipt under the agreed retention policy. To roll back, use the previous independently verified and authorized kit, when available, in a fresh directory. Otherwise keep the result on hold until the operator supplies a verified replacement. Follow the owner’s deletion terms on withdrawal.

## Rebuild from source

Maintainers can reproduce the archive at its recorded Git revision with Python 3.12.16. From that checkout, run `python -I -S -B tools/build_pilot_kit.py OpenTour_Offline_Pilot_Kit.zip` with a new output path. Compare the resulting digest to the distributed candidate.
