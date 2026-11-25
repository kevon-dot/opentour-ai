# Agree on coverage and error before evaluating a repair

The workflow owner chooses a minimum automatic coverage and a maximum wrong-automatic rate for one frozen population. The operator binds both to the exact source, method references, engagement and owner attestation. A fresh repeat must use those same bindings. The standalone example below uses generated cases only.

## Try the two-of-ten example

Start inside the extracted Slack workspace kit with Python 3.12.16. The template has empty source and time fields so it cannot authorize a run as shipped. The following commands create an owner-only directory, bind a generated agreement for thirty minutes and compare a repair at the agreed operating point.

```sh
umask 077
mkdir -m 700 operating-demo
python -I -S -B opentour-slack.pyz evaluate --source-digest generated/coverage-floor.json > operating-demo/source.sha256
python - <<'PY'
from pathlib import Path
import json
import time

work = Path("operating-demo")
agreement = json.loads(Path("generated/operating-agreement.template.json").read_text())
agreement["source_digest"] = (work / "source.sha256").read_text().strip()
agreement["agreed_at"] = int(time.time())
agreement["expires_at"] = agreement["agreed_at"] + 1800
path = work / "agreement.json"
with path.open("x") as stream:
    json.dump(agreement, stream)
path.chmod(0o600)
PY
python -I -S -B opentour-slack.pyz evaluate --organization generated-demo \
  --engagement generated-coverage-demo --operating-agreement operating-demo/agreement.json \
  --report-dir operating-demo/first generated/coverage-floor.json
python -I -S -B opentour-slack.pyz evaluate --organization generated-demo \
  --engagement generated-coverage-demo --operating-agreement operating-demo/agreement.json \
  --report-dir operating-demo/repeat generated/coverage-floor.json
cmp operating-demo/first/decision-quality.json operating-demo/repeat/decision-quality.json
cmp operating-demo/first/decision-quality.md operating-demo/repeat/decision-quality.md
```

Open `operating-demo/first/decision-quality.md`. Both methods automated 2 of 10 cases against a minimum of 8 of 10. The incumbent made one wrong automatic decision out of two and the repair made zero. The repair meets the 5% error ceiling and misses the 80% minimum coverage. Its combined result is **Missed**. The original aggregate and brief remain available for independent comparison.

## Use an agreement on a customer pilot

The owner chooses the actual methods, eligible cohort, location, consent, destination, retention, coverage and error limits before anyone reviews the held-out answers. The custodian keeps the dated evidence in the approved private system. The digest command reads the selected input and prints its identity without showing scores. It cannot establish who previously viewed a file. An operator with only a post-hoc agreement should report the original result as descriptive and arrange a fresh cohort for a prospective decision.

For an MCP enrollment, prepare the customer-selected training or supplied-output envelope in the owner-only directory. Compute its source digest before adding the top-level `operating_agreement`, and use that envelope's digest in the agreement. Review the scope and obtain a distinct owner grant for that revision through the onboarding workflow. The store binds the criteria to the dataset and fresh score-only evidence. The standalone evaluator uses the digest of the bare v1 case manifest. Each path checks its own source.

When the owner changes the limits for the same engagement and methods, select a new dataset ID and a fresh held-out cohort. Add `supersedes` with the previous agreement's 64-character revision to the new agreement and repeat the custodian grant. A successful replacement retires the earlier selection while preserving its historical receipts. A failed replacement leaves the prior authority intact. Use the new evidence revision for the next Slack decision.

An observed improvement describes the tested comparison. Verify coverage, the error ceiling, missing or unknown observations and any switch in which cases were automated before drawing a conclusion. The permitted recipient reruns or witnesses the computation. The workflow owner records the business decision in a separate private receipt.
