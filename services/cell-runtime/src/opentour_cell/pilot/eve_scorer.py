"""The EVE scorer implementation, kept out of the replay worker's import graph.

``eve_replay`` defines only the ``Scorer`` protocol and receives a scorer instance from its caller;
it never imports this module, and ``tests/pilot/test_eve_import_boundary.py`` fails if it does.
That boundary is structural only: whoever wires the run still hands this scorer and the answer key
to the replay worker's process. This is therefore not the separate evaluator that OPE-528 defines
(own identity, per-case predictions kept privately under its own custody); that scorer is a
follow-up slice of OPE-573.

``DeterministicScorer`` is an exact-match stand-in for the evaluator, over generated sentinel data.
Unverifiable claims never score as correct.
"""

from __future__ import annotations

from opentour_cell.pilot.eve_replay import ArmOutput, Expectation, ProtectedAnswer, Verdict
from opentour_cell.pilot.eve_task import EveCase, EveRefusal


class DeterministicScorer:
    """Exact-match stand-in for the independent evaluator. Unverifiable claims never score."""

    def __init__(self, version: str, rubric_revision: str) -> None:
        self.version = version
        self.rubric_revision = rubric_revision

    def score(self, case: EveCase, expected: ProtectedAnswer, output: ArmOutput | None) -> Verdict:
        if output is None:
            return Verdict.MISSING
        if expected.expectation not in (Expectation.ANSWER, Expectation.ABSTAIN):
            raise EveRefusal("case_not_scorable")
        if output.abstained or output.answer is None:
            if expected.expectation is Expectation.ABSTAIN:
                return Verdict.CORRECT_ABSTENTION
            return Verdict.INAPPROPRIATE_REFUSAL
        approved = {p.passage_id for p in case.context}
        if not output.citations or not set(output.citations) <= approved:
            return Verdict.UNVERIFIED
        if expected.expectation is Expectation.ABSTAIN:
            return Verdict.SERIOUS_ERROR
        if output.answer == expected.answer and expected.required_citations <= set(
            output.citations
        ):
            return Verdict.CORRECT_ANSWER
        return Verdict.SERIOUS_ERROR
