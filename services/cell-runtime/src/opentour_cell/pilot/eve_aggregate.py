"""Re-derive EVE scorecard totals from journaled verdicts, apart from the replay worker.

What it does: reads the replay journal file itself and sums the journaled verdict rows with code
written separately from ``eve_replay``'s own aggregation. ``reconcile`` then compares a result
body with the journal and the manifest, field by field:

* every aggregation field (arm totals, per-case outcomes, the paired comparison, the causal
  disclosure and the improvement claim) against values summed or derived from journal rows;
* every other field (schema version, benchmark, registration identity, scorer identity and
  self-check flags, evidence tier, claim scope, authorization flags, external gates, redacted
  exclusions) against literals written in this module or values derived from the manifest;
* the exact set of body keys, so an added claim is not silently ignored.

A result body edited after it was built (even when re-digested), a journal row edited after the
result, an arm left out of a result, and a bug in ``eve_replay``'s aggregation all surface as named
mismatches. It never trusts a total field: every total it compares is summed from rows.

What it cannot do: check any verdict. It reads only the worker's redacted journal and never sees a
candidate output or the answer key, so a verdict the scorer got wrong is summed exactly as
faithfully as one it got right. Re-scoring is the separate evaluator's job (OPE-528), not this
module's. It cannot re-run the staging-time scorer self-check either; it checks only that the body's
combined self-check flag agrees with the replay-time record and the body's own staging flag. Its
output is labelled ``aggregation re-derived from journaled verdicts`` and carries
``verdicts_rechecked: False``; no document, scorecard or Slack text built from it may word it as a
re-score or as proof to anyone else.

Readers reject, never accept: a row missing scorer id, version or rubric; a row or arm attributed
to anything other than what the manifest froze; an arm without its scorer record or replayed under
a failing self-check; and a row with no commitment present. A commitment's format is checked, not
its value: only ``eve_replay.verify_output``, holding the run key and the output, can check that.
The worker journal is unsigned (no OPE-566 custody is applied to it here), so anyone able to
rewrite the whole journal consistently can still pass this check.

The replay worker never imports this module, and this module never imports the scorer
(``tests/pilot/test_eve_import_boundary.py``). It imports nothing from ``eve_replay`` at run time;
its expected tier, schema version and external gates are its own literals.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentour_cell.pilot.eve_task import EveRefusal, canonical, digest

if TYPE_CHECKING:
    from opentour_cell.pilot.eve_replay import ReplayManifest

# What a result body must say, written here as literals. They are never read back from
# ``eve_replay``: a check that took its expectations from the code it checks could not catch a
# wrong tier, version or gate list there.
EXPECTED_SCHEMA_VERSION = "opentour.pilot.eve-protected-replay.v1"
EXPECTED_TIER = "generated"
EXPECTED_CLAIM_SCOPE = "implementation_only"
EXPECTED_EXTERNAL_GATES = (
    "real_eve_inference_capacity_unavailable",
    "provider_or_trainer_authorization_unavailable",
    "independent_evaluator_and_reviewer_unavailable",
    "installed_provider_and_customer_evidence_unavailable",
)

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
_CAUSAL_FIELDS = (
    ("source_snapshot_id", 2),
    ("retrieval_settings_id", 3),
    ("context_revision", 4),
    ("decoding_digest", 5),
)
_BODY_KEYS = frozenset(
    {
        "schema_version",
        "benchmark_id",
        "benchmark_revision",
        "manifest_digest",
        "registration_identity",
        "scorer",
        "evidence_tier",
        "claim_scope",
        "arms",
        "paired_comparison",
        "causal_tuning_comparison",
        "improvement_claim",
        "case_outcomes_redacted",
        "excluded_redacted",
        "unsatisfied_external_gates",
        "fit_authorized",
        "release_authorized",
    }
)
_SCORER_KEYS = frozenset(
    {
        "scorer_id",
        "version",
        "rubric_revision",
        "replay_selfcheck_passed",
        "staging_selfcheck_passed",
        "selfcheck_passed",
    }
)
_ABSENT = object()


def _same(left: Any, right: Any) -> bool:
    """Equal as JSON values, so ``0`` is not ``False`` and ``1`` is not ``True``."""
    if left is _ABSENT or right is _ABSENT:
        return left is right
    try:
        return canonical(left) == canonical(right)
    except (TypeError, ValueError):
        return False


@dataclass(frozen=True)
class JournaledRow:
    ref: str
    verdict: str
    answered: bool
    retrieval: str


@dataclass(frozen=True)
class JournaledArm:
    arm_id: str
    spec_digest: str
    spec: tuple[str, ...]
    rows: tuple[JournaledRow, ...]

    @property
    def served_revision(self) -> str:
        return self.spec[1]


@dataclass(frozen=True)
class Reconciliation:
    """Whether a result agrees with the journal and the manifest. Says nothing about any verdict."""

    consistent: bool
    mismatches: tuple[str, ...]
    basis: str = BASIS
    verdicts_rechecked: bool = False


def _case_ref(manifest_digest: str, case_id: str) -> str:
    return digest([manifest_digest, case_id])[7:23]


def _frozen(manifest: ReplayManifest) -> tuple[str, str, str]:
    return (manifest.scorer_id, manifest.scorer_version, manifest.rubric_revision)


def _read_row(raw: Any, frozen: tuple[str, str, str]) -> JournaledRow:
    if not isinstance(raw, list):
        raise EveRefusal("journal_row_malformed")
    if len(raw) in (4, 7):  # earlier shapes: no attribution, or no scorer identity
        raise EveRefusal("verdict_unattributed")
    if len(raw) != 8:
        raise EveRefusal("journal_row_malformed")
    ref, verdict, answered, retrieval, scorer_id, scorer_version, rubric_revision, mac = raw
    attribution = (scorer_id, scorer_version, rubric_revision)
    if not all(isinstance(v, str) and v for v in attribution):
        raise EveRefusal("verdict_unattributed")
    if not isinstance(mac, str) or not _COMMITMENT.fullmatch(mac):  # present, not verified
        raise EveRefusal("verdict_uncommitted")
    if attribution != frozen:
        raise EveRefusal("verdict_attribution_mismatch")
    if (
        not isinstance(ref, str)
        or verdict not in _VERDICTS
        or type(answered) is not bool
        or not isinstance(retrieval, str)
    ):
        raise EveRefusal("journal_row_malformed")
    return JournaledRow(ref, verdict, answered, retrieval)


def _check_scorer_record(raw: Any, frozen: tuple[str, str, str]) -> None:
    if (
        not isinstance(raw, list)
        or len(raw) != 4
        or not all(isinstance(v, str) and v for v in raw[:3])
        or type(raw[3]) is not bool
    ):
        raise EveRefusal("arm_scorer_unrecorded")
    if tuple(raw[:3]) != frozen:
        raise EveRefusal("verdict_attribution_mismatch")
    if raw[3] is not True:
        raise EveRefusal("arm_selfcheck_failed")


def read_arms(journal_path: Path, manifest: ReplayManifest) -> dict[str, JournaledArm]:
    """Every arm the journal holds for ``manifest``, keyed by arm spec digest, read strictly."""
    try:
        text = journal_path.read_text() if journal_path.exists() else ""
        events = [json.loads(line) for line in text.splitlines() if line]
    except (OSError, ValueError):
        raise EveRefusal("replay_journal_unavailable") from None
    frozen = _frozen(manifest)
    expected_refs = [_case_ref(manifest.digest, c) for c in manifest.eligible_case_ids]
    arms: dict[str, JournaledArm] = {}
    runs: set[str] = set()
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "arm":
            continue
        if event.get("manifest") != manifest.digest:
            continue
        try:
            run_id, record = event["run_id"], event["record"]
            spec, rows = record["spec"], record["rows"]
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
        _check_scorer_record(record.get("scorer") if isinstance(record, dict) else None, frozen)
        if [r.ref for r in parsed] != expected_refs:
            raise EveRefusal("journal_rows_incomplete")
        arms[spec_digest] = JournaledArm(spec[0], spec_digest, tuple(spec), parsed)
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


def causal_totals(control: JournaledArm, treatment: JournaledArm) -> dict[str, Any]:
    """Which settings besides the served model differ, and in how many cases retrieval moved."""
    changed = [name for name, i in _CAUSAL_FIELDS if control.spec[i] != treatment.spec[i]]
    left = {r.ref: r.retrieval for r in control.rows}
    right = {r.ref: r.retrieval for r in treatment.rows}
    moved = sum(1 for ref in left if right.get(ref) != left[ref])
    return {
        "available": not changed and moved == 0,
        "differing_settings": changed,
        "retrieval_changed_cases": moved,
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
            "scorer_id": manifest.scorer_id,
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


def _compare(mismatches: list[str], name: str, got: Any, want: Any) -> None:
    if isinstance(want, dict) and isinstance(got, dict):
        for field in sorted(set(want) | set(got)):
            if not _same(got.get(field, _ABSENT), want.get(field, _ABSENT)):
                mismatches.append(f"{name}.{field}")
    elif not _same(got, want):
        mismatches.append(name)


def _expected_comparison(
    chosen: dict[str, JournaledArm],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None, str]:
    """Control, treatment, paired, causal and claim, derived from the arms the body names."""
    treatment = chosen.get("candidate") or chosen.get("simple_repair")
    control = chosen.get("simple_repair") if "candidate" in chosen else chosen.get("baseline")
    if "candidate" in chosen and control is None:
        control = chosen.get("baseline")
    if treatment is None or control is None:
        claim = "none_baseline_only" if set(chosen) == {"baseline"} else "hold_missing_arm"
        return None, None, claim
    paired = paired_totals(control, treatment)
    causal = causal_totals(control, treatment)
    if paired is None:
        return None, causal, "blocked_changed_denominator"
    claim = "observed_net_fixes" if paired["fixes"] > paired["regressions"] else "no_net_fixes"
    return paired, causal, claim


def reconcile(result: Any, *, journal_path: Path, manifest: ReplayManifest) -> Reconciliation:
    """Compare a whole result body with the journal and the manifest, field by field."""
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
    if set(body) != _BODY_KEYS:
        mismatches.append("body.keys")

    for field, want in (
        ("schema_version", EXPECTED_SCHEMA_VERSION),
        ("benchmark_id", manifest.benchmark_id),
        ("benchmark_revision", manifest.benchmark_revision),
        ("manifest_digest", manifest.digest),
        ("registration_identity", manifest.registration_identity),
        ("evidence_tier", EXPECTED_TIER),
        ("claim_scope", EXPECTED_CLAIM_SCOPE),
        ("unsatisfied_external_gates", list(EXPECTED_EXTERNAL_GATES)),
        ("fit_authorized", False),
        ("release_authorized", False),
        (
            "excluded_redacted",
            [[_case_ref(manifest.digest, c), reason] for c, reason in manifest.excluded],
        ),
    ):
        _compare(mismatches, field, body.get(field, _ABSENT), want)

    # Scorer: identity from the manifest; the replay self-check from the journal (read_arms has
    # already refused any arm recorded under a failing one). The staging flag cannot be re-run
    # here, so only its type and its agreement with the combined flag are checked.
    if set(scorer) != _SCORER_KEYS:
        mismatches.append("scorer.keys")
    for field, want in (
        ("scorer_id", manifest.scorer_id),
        ("version", manifest.scorer_version),
        ("rubric_revision", manifest.rubric_revision),
        ("replay_selfcheck_passed", True),
    ):
        _compare(mismatches, f"scorer.{field}", scorer.get(field, _ABSENT), want)
    staging = scorer.get("staging_selfcheck_passed")
    if type(staging) is not bool:
        mismatches.append("scorer.staging_selfcheck_passed")
    elif not _same(scorer.get("selfcheck_passed", _ABSENT), staging):
        mismatches.append("scorer.selfcheck_passed")

    chosen: dict[str, JournaledArm] = {}
    for name, arm_body in sorted(body_arms.items()):
        journaled = (
            arms.get(str(arm_body.get("spec_digest"))) if isinstance(arm_body, dict) else None
        )
        if journaled is None or journaled.arm_id != name:
            mismatches.append(f"arms.{name}.not_journaled")
            continue
        chosen[name] = journaled
        _compare(mismatches, f"arms.{name}", arm_body, arm_totals(manifest, journaled))
    for arm_id in sorted({a.arm_id for a in arms.values()} - set(body_arms)):
        mismatches.append(f"arms.{arm_id}.absent")
    # Two journaled runs of one arm id under one manifest: no reader can say which one the
    # result stands for, whichever the body names.
    runs_per_arm: dict[str, int] = {}
    for journaled_arm in arms.values():
        runs_per_arm[journaled_arm.arm_id] = runs_per_arm.get(journaled_arm.arm_id, 0) + 1
    for arm_id in sorted(a for a, n in runs_per_arm.items() if n > 1):
        mismatches.append(f"arms.{arm_id}.ambiguous")

    outcomes = body.get("case_outcomes_redacted")
    outcomes = outcomes if isinstance(outcomes, dict) else {}
    for name in sorted(set(outcomes) | set(chosen)):
        want_rows = [[r.ref, r.verdict] for r in chosen[name].rows] if name in chosen else None
        if not _same(outcomes.get(name), want_rows):
            mismatches.append(f"case_outcomes_redacted.{name}")

    want_paired, want_causal, want_claim = _expected_comparison(chosen)
    _compare(mismatches, "paired_comparison", body.get("paired_comparison"), want_paired)
    _compare(
        mismatches, "causal_tuning_comparison", body.get("causal_tuning_comparison"), want_causal
    )
    _compare(mismatches, "improvement_claim", body.get("improvement_claim"), want_claim)
    return Reconciliation(not mismatches, tuple(mismatches))
