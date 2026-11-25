# OpenTour offline pilot kit

Use this kit to compare an incumbent and one proposed repair on one frozen cohort. The operator supplies both methods’ case-aligned outputs and independently established references. When candidate predictions are missing, the named operator runs the agreed repair in the authorized environment and freezes its outputs before scoring.

## Run the generated example

Use Python 3.12.16 on the approved machine. Check the outer ZIP’s SHA-256 against the digest received through the approved support route, then unpack it into a new directory. The kit uses the Python standard library and runs with networking off.

In the extracted directory, run one command.

```sh
python -I -S -B opentour-pilot.pyz --report-dir sample-run example.json
```

Open `sample-run/brief.md` for the workflow owner and `sample-run/aggregate.json` for the exact counts. Both files should match the included `example-readout/` versions byte for byte. For a replay, choose a new output directory, such as `sample-rerun`. A malformed manifest returns exit 2, writes a diagnostic to standard error and produces no report. A pre-existing output location is refused.

Use a private parent directory. On POSIX the operator receives an owner-only report directory. An interruption while reserving its name can leave an empty directory. Choose a fresh directory for the replay and use the report only when both files are present. The release-candidate exercise used Linux x86_64. Verify the local replay on the recipient’s approved platform.

To obtain only the aggregate JSON on standard output, run `python -I -S -B opentour-pilot.pyz example.json`. Replace the filename with `-` to read the same bounded JSON from standard input.

## Run one approved workflow

1. Use a separate owner-approved working directory for each organization. Keep source records, the intake receipt and owner decisions there. Confirm the named operator and reviewer, the governing permission, method versions, holdout and pre-agreed operating point.
2. Follow `manifest-guide.md` for the exact fields. Represent unknown labels, missing decisions and explicit human review separately. Record upstream exclusions and unmatched case counts in the authorized private intake record.
3. Substitute the approved manifest in the example command and choose a fresh output directory. Review the fractions and paired counts together. Ask the recipient to rerun or witness the comparison. The workflow owner records their own decision and next action in the approved location.

## Version, integrity and support

Run `python -I -S -B opentour-pilot.pyz --version` to print the kit version and exact source commit. `PROVENANCE.json` identifies the build, required Python and generated example. `SHA256SUMS` lists the SHA-256 of every other member. Systems with the standard checksum utility can run `sha256sum -c SHA256SUMS` from the extracted directory.

Contact the named pilot operator using the private support route agreed at intake. Provide the kit version, source commit, digest and a redacted error or generated reproducer. Keep customer cases, labels and credentials in their approved location.

If the revision, calculation or permission is uncertain, hold the handoff. Keep the permitted receipt under the agreed retention policy. To roll back, use the previous independently verified and authorized kit, when available, in a fresh directory. Otherwise keep the result on hold until the operator supplies a verified replacement. Follow the owner’s deletion terms on withdrawal.

## Rebuild from source

Maintainers can reproduce the archive at its recorded Git revision with Python 3.12.16. From that checkout, run `python -I -S -B tools/build_pilot_kit.py OpenTour_Offline_Pilot_Kit.zip` with a new output path. Compare the resulting digest to the distributed candidate.
