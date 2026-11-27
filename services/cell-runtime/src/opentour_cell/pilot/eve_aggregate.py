"""Re-derive EVE scorecard totals from journaled verdicts, apart from the replay worker.

What it does: reads the replay journal file itself and sums the journaled verdict rows with code
written separately from ``eve_replay``'s own aggregation. A result body edited after it was built
(even when re-digested), a journal row edited after the result, an arm left out of a result, and a
bug in ``eve_replay``'s aggregation all surface as named mismatches. It never trusts a total field:
every total it compares is summed from rows.

What it cannot do: check any verdict. Under the OPE-574 decision the evaluator retains no candidate
outputs, and this module never sees an output or the answer key, so a verdict the scorer got wrong
is summed exactly as faithfully as one it got right. Its output is labelled
``aggregation re-derived from journaled verdicts`` and carries ``verdicts_rechecked: False``; no
document, scorecard or Slack text built from it may word it as a re-score or as proof to anyone
else.

Readers reject, never accept, a row without scorer attribution, a row attributed to a scorer or
rubric other than the one the manifest froze, and a row without a keyed commitment. The journal is
unsigned until OPE-566 defines signing and key custody, so anyone able to rewrite the whole journal
consistently can still pass this check.

The replay worker never imports this module, and this module never imports the scorer
(``tests/pilot/test_eve_import_boundary.py``).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentour_cell.pilot.eve_task import EveRefusal, digest

if TYPE_CHECKING:
    from opentour_cell.pilot.eve_replay import ReplayManifest

BASIS = "aggregation re-derived from journaled verdicts"
LIMITATION = "verdicts are summed as journaled; no verdict is re-scored and no output is seen"

_VERDICTS = frozenset(
    {
        "correct_answer",
        "correct_abstention",
        "serious_error",
        "inappropriate_refusal",
        "unverified",
        "missing_output",
    }
)
_CORRECT = frozenset({"correct_answer", "correct_abstention"})
_COMMITMENT = re.compile(r"hmac-sha256:[0-9a-f]{64}")
_ABSENT = object()


@dataclass(frozen=True)
class JournaledRow:
    ref: str
    verdict: str
    answered: bool


@dataclass(frozen=True)
class JournaledArm:
    arm_id: str
    spec_digest: str
    served_revision: str
    rows: tuple[JournaledRow, ...]


@dataclass(frozen=True)
class Reconciliation:
    """Whether a result's aggregation agrees with the journal. Says nothing about any verdict."""

    consistent: bool
    mismatches: tuple[str, ...]
    basis: str = BASIS
    verdicts_rechecked: bool = False


def _case_ref(manifest_digest: str, case_id: str) -> str:
    return digest([manifest_digest, case_id])[7:23]


def _read_row(raw: Any, frozen: tuple[str, str]) -> JournaledRow:
    if not isinstance(raw, list):
        raise EveRefusal("journal_row_malformed")
    if len(raw) == 4:  # the pre-OPE-573 shape: a verdict with no scorer attribution
        raise EveRefusal("verdict_unattributed")
    if len(raw) != 7:
        raise EveRefusal("journal_row_malformed")
    ref, verdict, answered, retrieval, scorer_version, rubric_revision, commitment = raw
    if not all(isinstance(v, str) and v for v in (scorer_version, rubric_revision)):
        raise EveRefusal("verdict_unattributed")
    if not isinstance(commitment, str) or not _COMMITMENT.fullmatch(commitment):
        raise EveRefusal("verdict_uncommitted")
    if (scorer_version, rubric_revision) != frozen:
        raise EveRefusal("verdict_attribution_mismatch")
    if (
        not isinstance(ref, str)
        or verdict not in _VERDICTS
        or type(answered) is not bool
        or not isinstance(retrieval, str)
    ):
        raise EveRefusal("journal_row_malformed")
    return JournaledRow(ref, verdict, answered)


def read_arms(journal_path: Path, manifest: ReplayManifest) -> dict[str, JournaledArm]:
    """Every arm the journal holds for ``manifest``, keyed by arm spec digest, read strictly."""
    try:
        text = journal_path.read_text() if journal_path.exists() else ""
        events = [json.loads(line) for line in text.splitlines() if line]
    except (OSError, ValueError):
        raise EveRefusal("replay_journal_unavailable") from None
    frozen = (manifest.scorer_version, manifest.rubric_revision)
    expected_refs = [_case_ref(manifest.digest, c) for c in manifest.eligible_case_ids]
    arms: dict[str, JournaledArm] = {}
    runs: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "arm":
            continue
        if event.get("manifest") != manifest.digest:
            continue
        try:
            run_id, spec, rows = event["run_id"], event["record"]["spec"], event["record"]["rows"]
        except (KeyError, TypeError):
            raise EveRefusal("journal_row_malformed") from None
        if (
            not isinstance(spec, list)
            or len(spec) != 6
            or not all(isinstance(s, str) and s for s in spec)
            or not isinstance(rows, list)
        ):
            raise EveRefusal("journal_row_malformed")
        spec_digest = digest(spec)
        if run_id != digest([manifest.digest, spec_digest]):
            raise EveRefusal("journal_arm_run_mismatch")
        if run_id in runs:
            raise EveRefusal("journal_duplicate_arm")
        runs.add(run_id)
        parsed = tuple(_read_row(row, frozen) for row in rows)
        if [r.ref for r in parsed] != expected_refs:
            raise EveRefusal("journal_rows_incomplete")
        arms[spec_digest] = JournaledArm(spec[0], spec_digest, spec[1], parsed)
    return arms


def _ratio(num: int, den: int) -> list[int | None]:
    return [num, den if den else None]


def arm_totals(manifest: ReplayManifest, arm: JournaledArm) -> dict[str, Any]:
    """Scorecard totals for one arm, summed from its journaled rows."""
    tally = {v: 0 for v in _VERDICTS}
    for row in arm.rows:
        tally[row.verdict] += 1
    eligible = len(manifest.eligible_case_ids)
    answered = sum(1 for row in arm.rows if row.answered)
    serious = tally["serious_error"]
    verified = tally["correct_answer"] + serious
    correct = tally["correct_answer"] + tally["correct_abstention"]
    return {
        "spec_digest": arm.spec_digest,
        "served_revision": arm.served_revision,
        "eligible": eligible,
        "excluded_unscored": len(manifest.excluded),
        "missing_output": tally["missing_output"],
        "answered": answered,
        "verified_answered": verified,
        "unverified_answered": tally["unverified"],
        "correct_answer": tally["correct_answer"],
        "correct_abstention": tally["correct_abstention"],
        "inappropriate_refusal": tally["inappropriate_refusal"],
        "serious_errors": serious,
        "correct": correct,
        "coverage_answered_over_eligible": _ratio(answered, eligible),
        "serious_error_over_verified_answered": _ratio(serious, verified),
        "correct_over_eligible": _ratio(correct, eligible),
    }


def paired_totals(control: JournaledArm, treatment: JournaledArm) -> dict[str, Any] | None:
    """Paired fixes and regressions over cases both arms graded; None when denominators differ."""
    left = {r.ref: r.verdict for r in control.rows}
    right = {r.ref: r.verdict for r in treatment.rows}
    left_missing = {ref for ref, v in left.items() if v == "missing_output"}
    right_missing = {ref for ref, v in right.items() if v == "missing_output"}
    if left.keys() != right.keys() or left_missing != right_missing:
        return None
    graded = [ref for ref in left if ref not in left_missing]
    pairs = [(left[ref] in _CORRECT, right[ref] in _CORRECT) for ref in graded]
    return {
        "control": control.arm_id,
        "treatment": treatment.arm_id,
        "paired_gradeable": len(graded),
        "fixes": pairs.count((False, True)),
        "regressions": pairs.count((True, False)),
        "both_correct": pairs.count((True, True)),
        "both_wrong": pairs.count((False, False)),
    }


def rederive(journal_path: Path, manifest: ReplayManifest) -> dict[str, Any]:
    """Totals for every arm journaled under ``manifest``. Aggregation only; no verdict checked."""
    arms = read_arms(journal_path, manifest)
    return {
        "basis": BASIS,
        "limitation": LIMITATION,
        "verdicts_rechecked": False,
        "manifest_digest": manifest.digest,
        "scorer": {
            "version": manifest.scorer_version,
            "rubric_revision": manifest.rubric_revision,
        },
        "arms": {
            spec_digest: {
                "arm_id": arm.arm_id,
                "totals": arm_totals(manifest, arm),
                "case_outcomes": [[r.ref, r.verdict] for r in arm.rows],
            }
            for spec_digest, arm in sorted(arms.items())
        },
    }


def reconcile(result: Any, *, journal_path: Path, manifest: ReplayManifest) -> Reconciliation:
    """Compare a result's aggregation with totals summed from the journal, field by field."""
    arms = read_arms(journal_path, manifest)
    try:
        body, supplied = result["body"], result["result_digest"]
        body_arms = dict(body["arms"])
        scorer = dict(body["scorer"])
    except (KeyError, TypeError, ValueError):
        return Reconciliation(False, ("result_malformed",))
    mismatches: list[str] = []
    if supplied != digest(body):
        mismatches.append("result_digest")
    if body.get("manifest_digest") != manifest.digest:
        mismatches.append("manifest_digest")
    if (scorer.get("version"), scorer.get("rubric_revision")) != (
        manifest.scorer_version,
        manifest.rubric_revision,
    ):
        mismatches.append("scorer")

    chosen: dict[str, JournaledArm] = {}
    for name, arm_body in sorted(body_arms.items()):
        journaled = (
            arms.get(str(arm_body.get("spec_digest"))) if isinstance(arm_body, dict) else None
        )
        if journaled is None or journaled.arm_id != name:
            mismatches.append(f"arms.{name}.not_journaled")
            continue
        chosen[name] = journaled
        want = arm_totals(manifest, journaled)
        for field in sorted(set(want) | set(arm_body)):
            if arm_body.get(field, _ABSENT) != want.get(field, _ABSENT):
                mismatches.append(f"arms.{name}.{field}")
    for arm_id in sorted({a.arm_id for a in arms.values()} - set(body_arms)):
        mismatches.append(f"arms.{arm_id}.absent")

    outcomes = body.get("case_outcomes_redacted")
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    for name in sorted(set(outcomes) | set(chosen)):
        want_rows = [[r.ref, r.verdict] for r in chosen[name].rows] if name in chosen else None
        if outcomes.get(name) != want_rows:
            mismatches.append(f"case_outcomes_redacted.{name}")

    paired, claim = body.get("paired_comparison"), body.get("improvement_claim")
    if isinstance(paired, dict):
        control = chosen.get(str(paired.get("control")))
        treatment = chosen.get(str(paired.get("treatment")))
        want_pair = None
        if control is None or treatment is None:
            mismatches.append("paired_comparison.arms")
        else:
            want_pair = paired_totals(control, treatment)
            if want_pair is None:
                mismatches.append("paired_comparison.denominator")
            else:
                for field in sorted(set(want_pair) | set(paired)):
                    if paired.get(field, _ABSENT) != want_pair.get(field, _ABSENT):
                        mismatches.append(f"paired_comparison.{field}")
        if want_pair is not None:
            expected = (
                "observed_net_fixes"
                if want_pair["fixes"] > want_pair["regressions"]
                else "no_net_fixes"
            )
            if claim != expected:
                mismatches.append("improvement_claim")
    elif claim in ("observed_net_fixes", "no_net_fixes"):
        mismatches.append("improvement_claim")
    return Reconciliation(not mismatches, tuple(mismatches))
