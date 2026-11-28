"""The separate EVE scorer: an independent evaluator with its own identity and custody (OPE-573).

This follows the OPE-574 ruling (A + C, following OPE-528). The independent evaluator replays the
frozen arms itself on the same sealed inputs, keeps the per-case predictions PRIVATELY under its
own custody with a retention limit, and re-scores them with its own implementation of the EVE
supported-answer rubric. The worker's replay journal stays redacted; this evaluator is what can
detect a wrong, biased or colluding worker scorer, which the aggregator (``eve_aggregate``)
cannot. The replay worker (``eve_replay``) and the aggregator never import this module, and this
module never imports the worker's scorer (``eve_scorer``) or the aggregator
(``tests/pilot/test_eve_import_boundary.py``).

What one evaluation checks, each as a named reason:

* Binding before any inference. The worker's frozen manifest must name this evaluator's own
  sealed cases and answer key (membership, content and answer-key digests), the eligible and
  excluded cases this evaluator derives from its own key, the same rubric revision, the
  registered model identity and deterministic decoding. The claimed result must be the one the
  manifest names. A mismatch holds or fails the claim before any model call.
* Serving identity. Each arm is replayed only through an inference whose served revision is the
  journaled one; the candidate arm only through a lineage-checked ``CandidateInference`` for this
  benchmark.
* The same outputs (the production caller of ``eve_replay.verify_output``). Every prediction the
  evaluator replays is checked against the worker's keyed commitment for that case. A serving
  difference, a substituted output or an edited journal row fails to verify.
* The same verdicts. The evaluator's own verdict for each case must equal the journaled verdict.
* The same totals. Arm totals, the paired comparison and the improvement claim are recounted from
  the evaluator's own verdicts and compared with the worker's result. Totals the worker asserts
  are only ever compared against the recount, never used as expected values.

The verdict states are ``reproduced``, ``held`` (could not be checked: no replay, no run key,
non-deterministic decoding) and ``integrity_failure`` (checked and wrong). Only ``reproduced``
lets ``eve_staging`` offer a result for Slack review, and never as a release authorization.
The evaluator's scorer must pass a fixture canary before it can issue anything: a deliberately
wrong independent scorer cannot issue a verdict.

Custody. The custody directory is created 0700, must not overlap any worker data directory (a
list that is mandatory, never skipped), and holds the evaluator's own 32-byte key, the sealed
verdicts and the private predictions. ``recompute`` re-scores from the private predictions alone,
within their retention limit (``OPE-528`` default, 30 days, never raised by a caller); expired
records are refused on read and ``purge_expired`` deletes them while verdicts keep the
commitment. ``accessor_id`` is a caller-asserted label, not authentication: it only stops an
accidental read through the worker's code path. Real access control is the file-system
permission on the custody directory and, later, separated custody (OPE-566).

Evidence rung, stated precisely. A verdict re-read by this evaluator from its own custody, after
checking its HMAC seal, is ``locally_keyed``: the operator holds this key on the same host as the
worker. An exported verdict document is ``digest_self_consistent_only``: its ``verdict_digest``
is a plain SHA-256 that anyone who can write files can recompute, so a consumer that has only the
document cannot tell this evaluator's verdict from a consistent forgery. Neither is proof to a
third party. Signing by the evaluator's own key (``signed_separated_custody``) is OPE-566 work and
nothing here fabricates a signature. ``evaluator_id`` and the scorer identity are declared
labels, checked to differ from the worker's scorer, not authenticated.

To verify commitments the evaluator reads the worker's per-run commitment keys (read-only). That
is what lets it bind its own predictions to the worker's verdict rows, and it is also why, at
``locally_keyed``, the evaluator could forge a commitment: it never writes the worker's journal,
and nothing here is evidence against a party who holds every key.

Simulated: tests use generated sentinel cases and answers, scripted generative doubles and the
generated reference backend. Not proven: no real EVE model, GPU, trainer, provider or human
evaluator exists here, and the rubric is an exact-match stand-in. Verdicts carry the
``generated`` evidence tier and claim scope ``implementation_only``.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import sys
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Never

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.eve_replay import (
    ARMS,
    AnswerKey,
    ArmOutput,
    ArmRecord,
    ArmSpec,
    CandidateInference,
    CommitmentKeyStore,
    Expectation,
    GenerativeInference,
    ProtectedAnswer,
    ReplayJournal,
    ReplayManifest,
    ReplayRequest,
    Retriever,
    cases_content_digest,
    check_egress,
    replay_membership,
    verify_output,
)
from opentour_cell.pilot.eve_task import (
    EveCase,
    EveRefusal,
    EveTaskRegistration,
    Passage,
    canonical,
    digest,
    parse_fit_record,
    parse_registration,
)

VERSION = "opentour.pilot.eve-independent-verdict.v1"
SCORER_ID = "opentour.pilot.eve-independent-generative-scorer"
SCORER_VERSION = "eve-independent-generative-scorer.code.v1"
EVIDENCE_TIER = "generated"
CLAIM_SCOPE = "implementation_only"
STORE_TIER = "locally_keyed"
EXPORT_TIER = "digest_self_consistent_only"
BASIS = "independent re-score from evaluator-held predictions"
PREDICTIONS_CUSTODY = "evaluator_private_retention_limited"
OUTCOMES = ("reproduced", "held", "integrity_failure")
DEFAULT_RETENTION_SECONDS = 30 * 86_400  # the OPE-528 default; a caller may only shorten it
DEFAULT_VALIDITY_SECONDS = 7 * 86_400  # D11 d11.v1 verdict_validity_seconds ceiling

# What a worker result must say, as this module's own literals: an independent check does not
# take its expectations from the code it checks.
EXPECTED_RESULT_SCHEMA = "opentour.pilot.eve-protected-replay.v1"
EXPECTED_RESULT_TIER = "generated"
EXPECTED_CLAIM_SCOPE = "implementation_only"

VERDICTS = (
    "correct_answer",
    "correct_abstention",
    "serious_error",
    "inappropriate_refusal",
    "unverified",
    "missing_output",
)
_CORRECT = frozenset({"correct_answer", "correct_abstention"})
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,127}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_INTEGRITY = "integrity_failure"
_HELD = "held"


# ---------------------------------------------------------------- the independent rubric
class IndependentGenerativeScorer:
    """A separately written implementation of the EVE supported-answer rubric.

    It shares no scoring code with the worker's scorer. Given the protected expectation, the
    passages the arm was actually shown and one output, it returns one verdict:

    1. no output: ``missing_output``;
    2. an abstention (``abstained`` or no answer): ``correct_abstention`` when abstaining was
       expected, otherwise ``inappropriate_refusal``;
    3. an answer citing nothing, or citing any passage it was not shown: ``unverified`` (an
       unverifiable claim never counts as correct);
    4. an answer where abstaining was expected: ``serious_error``;
    5. the exact protected answer citing every required passage: ``correct_answer``;
    6. anything else: ``serious_error``.

    Only ``answer`` and ``abstain`` expectations are scorable; unavailable or disputed labels are
    excluded before scoring and refused here.
    """

    scorer_id = SCORER_ID
    version = SCORER_VERSION

    def __init__(self, rubric_revision: str) -> None:
        if type(rubric_revision) is not str or not rubric_revision.strip():
            raise EveRefusal("evaluator_rubric_invalid")
        self.rubric_revision = rubric_revision

    def judge(
        self, expected: ProtectedAnswer, shown: frozenset[str], output: ArmOutput | None
    ) -> str:
        if output is None:
            return "missing_output"
        if expected.expectation is Expectation.ANSWER:
            answer_expected = True
        elif expected.expectation is Expectation.ABSTAIN:
            answer_expected = False
        else:
            raise EveRefusal("case_not_scorable")
        if output.abstained is True or output.answer is None:
            return "inappropriate_refusal" if answer_expected else "correct_abstention"
        citations = output.citations
        if len(citations) == 0 or any(citation not in shown for citation in citations):
            return "unverified"
        if not answer_expected:
            return "serious_error"
        if output.answer != expected.answer:
            return "serious_error"
        if any(required not in citations for required in expected.required_citations):
            return "serious_error"
        return "correct_answer"


def _canary_case(n: int) -> tuple[str, frozenset[str]]:
    passage = f"generated-canary-passage-{n}"
    return passage, frozenset({passage})


def qualify_scorer(scorer: Any) -> tuple[bool, dict[str, Any]]:
    """Run the fixture oracle. Misreporting any expected value fails qualification.

    The OPE-528 canary, in EVE form: truth ``[A, A, B, B]`` with frozen predictions
    ``[A, A, B, B]`` on four independent generated cases must score exactly 4 of 4; the deliberate
    answer-key permutation ``[A, B, A, B]`` must score exactly 2 of 4, the other two serious
    errors. Rubric controls then pin each verdict: an uncited answer and an answer citing an
    unshown passage are unverified, a refusal where an answer was expected is an inappropriate
    refusal, an expected abstention is correct, an answer where abstaining was expected is a
    serious error, the right answer without its required citation is a serious error, and a
    missing output is missing.
    """
    report: dict[str, Any] = {"canary": None, "permuted": None, "controls": None}
    try:
        identity = (scorer.scorer_id, scorer.version, scorer.rubric_revision)
        if not all(type(v) is str and v for v in identity):
            return False, report
        truth = (
            "generated-canary-A",
            "generated-canary-A",
            "generated-canary-B",
            "generated-canary-B",
        )
        permuted = (
            "generated-canary-A",
            "generated-canary-B",
            "generated-canary-A",
            "generated-canary-B",
        )

        def run(key: tuple[str, ...]) -> list[str]:
            verdicts = []
            for n, (want, said) in enumerate(zip(key, truth, strict=True)):
                passage, shown = _canary_case(n)
                expected = ProtectedAnswer(Expectation.ANSWER, want, frozenset({passage}))
                output = ArmOutput(f"generated-canary-{n}", said, (passage,), False)
                verdicts.append(scorer.judge(expected, shown, output))
            return verdicts

        canary, swapped = run(truth), run(permuted)
        report["canary"] = canary.count("correct_answer")
        report["permuted"] = swapped.count("correct_answer")
        passage, shown = _canary_case(9)
        answer = ProtectedAnswer(Expectation.ANSWER, "generated-canary-C", frozenset({passage}))
        abstain = ProtectedAnswer(Expectation.ABSTAIN, "", frozenset())
        other = "generated-canary-other-passage"
        wide = frozenset({passage, other})
        controls = (
            (answer, shown, ArmOutput("c", "generated-canary-C", (), False), "unverified"),
            (answer, shown, ArmOutput("c", "generated-canary-C", (other,), False), "unverified"),
            (answer, shown, ArmOutput("c", None, (), True), "inappropriate_refusal"),
            (abstain, shown, ArmOutput("c", None, (), True), "correct_abstention"),
            (abstain, shown, ArmOutput("c", "generated-guess", (passage,), False), "serious_error"),
            (answer, wide, ArmOutput("c", "generated-canary-C", (other,), False), "serious_error"),
            (
                answer,
                shown,
                ArmOutput("c", "generated-canary-D", (passage,), False),
                "serious_error",
            ),
            (
                answer,
                shown,
                ArmOutput("c", "generated-canary-C", (passage,), False),
                "correct_answer",
            ),
            (answer, shown, None, "missing_output"),
        )
        results = [scorer.judge(exp, seen, out) == want for exp, seen, out, want in controls]
        report["controls"] = sum(results)
        qualified = (
            canary == ["correct_answer"] * 4
            and swapped == ["correct_answer", "serious_error", "serious_error", "correct_answer"]
            and all(results)
        )
        return qualified, report
    except Exception:  # a scorer that raises on a control is not qualified, whatever it raised
        return False, report


# ---------------------------------------------------------------- custody primitives
def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = os.lstat(path)
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) & 0o077
    ):
        raise EveRefusal("evaluator_custody_not_private")


def _read_private(path: Path) -> bytes | None:
    """A regular file owned by this uid with no permission beyond 0600, or None if absent."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise EveRefusal("evaluator_custody_not_private") from None
        raise EveRefusal("evaluator_custody_unavailable") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) & 0o077
            or info.st_nlink != 1
        ):
            raise EveRefusal("evaluator_custody_not_private")
        return handle.read()


def _write_once(path: Path, data: bytes) -> bool:
    """Atomically create ``path`` 0600. Returns False if it already exists; never overwrites."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".pending-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        try:
            os.link(tmp, path)
        except FileExistsError:
            return False
        return True
    finally:
        os.unlink(tmp)


def _overlaps(left: Path, right: Path) -> bool:
    a, b = left.resolve(), right.resolve()
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def _case_ref(manifest_digest: str, case_id: str) -> str:
    return digest([manifest_digest, case_id])[7:23]


def _same(left: Any, right: Any) -> bool:
    """Equal as JSON values, so ``0`` is not ``False`` and a missing field is not ``None``."""
    try:
        return canonical(left) == canonical(right)
    except (TypeError, ValueError):
        return False


def _prediction(output: ArmOutput | None) -> list[Any] | None:
    if output is None:
        return None
    return [output.answer, list(output.citations), output.abstained]


def _output_from(case_id: str, raw: Any) -> ArmOutput | None:
    if raw is None:
        return None
    try:
        answer, citations, abstained = raw
        return ArmOutput(case_id, answer, tuple(citations), abstained)
    except (TypeError, ValueError):
        raise EveRefusal("private_predictions_corrupt") from None


def _ratio(num: int, den: int) -> list[int | None]:
    return [num, den if den else None]


def _totals(
    spec: ArmSpec, verdicts: Sequence[str], answered: Sequence[bool], eligible: int, excluded: int
) -> dict[str, Any]:
    """Arm totals from this evaluator's own verdicts, in the worker result's field names."""
    tally = Counter(verdicts)
    answered_count = sum(1 for flag in answered if flag)
    serious = tally["serious_error"]
    verified = tally["correct_answer"] + serious
    correct = tally["correct_answer"] + tally["correct_abstention"]
    return {
        "spec_digest": spec.digest,
        "served_revision": spec.served_revision,
        "eligible": eligible,
        "excluded_unscored": excluded,
        "missing_output": tally["missing_output"],
        "answered": answered_count,
        "verified_answered": verified,
        "unverified_answered": tally["unverified"],
        "correct_answer": tally["correct_answer"],
        "correct_abstention": tally["correct_abstention"],
        "inappropriate_refusal": tally["inappropriate_refusal"],
        "serious_errors": serious,
        "correct": correct,
        "coverage_answered_over_eligible": _ratio(answered_count, eligible),
        "serious_error_over_verified_answered": _ratio(serious, verified),
        "correct_over_eligible": _ratio(correct, eligible),
    }


def _comparison(verdicts: Mapping[str, Sequence[str]]) -> tuple[dict[str, Any] | None, str]:
    """The paired comparison and improvement claim, recounted from this evaluator's verdicts."""
    if "candidate" in verdicts:
        treatment = "candidate"
        control = "simple_repair" if "simple_repair" in verdicts else "baseline"
    else:
        treatment, control = "simple_repair", "baseline"
    if treatment not in verdicts or control not in verdicts:
        return None, ("none_baseline_only" if set(verdicts) == {"baseline"} else "hold_missing_arm")
    left, right = verdicts[control], verdicts[treatment]
    if [v == "missing_output" for v in left] != [v == "missing_output" for v in right]:
        return None, "blocked_changed_denominator"
    pairs = [(a in _CORRECT, b in _CORRECT) for a, b in zip(left, right, strict=True)]
    pairs = [p for p, a in zip(pairs, left, strict=True) if a != "missing_output"]
    fixes, regressions = pairs.count((False, True)), pairs.count((True, False))
    paired = {
        "control": control,
        "treatment": treatment,
        "paired_gradeable": len(pairs),
        "fixes": fixes,
        "regressions": regressions,
        "both_correct": pairs.count((True, True)),
        "both_wrong": pairs.count((False, False)),
    }
    return paired, ("observed_net_fixes" if fixes > regressions else "no_net_fixes")


@dataclass
class _Reasons:
    items: list[tuple[str, str]] = field(default_factory=list)

    def add(self, kind: str, reason: str) -> None:
        self.items.append((kind, reason))

    def outcome(self) -> str:
        kinds = {kind for kind, _ in self.items}
        if _INTEGRITY in kinds:
            return _INTEGRITY
        if _HELD in kinds:
            return _HELD
        return "reproduced"

    def reasons(self) -> list[str]:
        return sorted({reason for _, reason in self.items})


@dataclass
class _ArmCheck:
    spec: ArmSpec
    replayed: bool = False
    committed: int = 0
    not_committed: int = 0
    unverifiable: int = 0
    disagreements: int = 0
    verdicts: list[str] = field(default_factory=list)
    answered: list[bool] = field(default_factory=list)
    private_rows: list[dict[str, Any]] = field(default_factory=list)


# ---------------------------------------------------------- the evaluator's own candidate replay
_CONNECTOR_ROW_KEYS = frozenset({"case_id", "answer", "citations", "abstained"})


class EvaluatorCandidateReplay:
    """Replay a collected candidate for the evaluator, after re-checking its lineage itself.

    The evaluator holds no training grant, so it does not go through the worker's ``Preflight``.
    It asks the connector directly: the job must be recorded under exactly this intent and be
    complete, collecting it must hand back exactly this candidate, the candidate must be bound to
    the job (``verify_candidate``) and to the registered base, tokenizer/template fingerprint, model
    and task. Any difference is refused before a model call. Connector rows are read strictly.
    """

    def __init__(
        self,
        connector: mc.Connector,
        job: mc.JobReceipt,
        candidate: mc.Candidate,
        registration: EveTaskRegistration,
    ) -> None:
        try:
            mc.verify_candidate(job, candidate)
            recorded = connector.lookup(job.intent)
            collected = connector.collect(job)
        except (mc.CapabilityError, OSError, ValueError, LookupError):
            raise EveRefusal("candidate_lineage_unverified") from None
        binding = job.intent.binding
        if (
            recorded != job
            or collected != candidate
            or job.state is not mc.JobState.COMPLETED
            or candidate.parent_revision != registration.base_revision
            or binding.immutable_revision != registration.base_revision
            or binding.preprocessing_revision != registration.fingerprint
            or binding.model_ref != registration.model_ref
            or binding.task != registration.task
        ):
            raise EveRefusal("candidate_lineage_unverified")
        self._connector = connector
        self.candidate = candidate
        self.benchmark_revision = job.intent.benchmark_revision

    @property
    def served_revision(self) -> str:
        return self.candidate.artifact_or_serving_revision

    def generate(self, arm: ArmSpec, requests: tuple[ReplayRequest, ...]) -> tuple[ArmOutput, ...]:
        try:
            raw = self._connector.predict_for_replay(
                self.candidate, tuple(r.as_mapping() for r in requests)
            )
        except OSError:
            raise
        except (ValueError, LookupError):
            raise EveRefusal("candidate_lineage_unverified") from None
        if type(raw) is not tuple:
            raise EveRefusal("output_shape_invalid")
        outputs = []
        for row in raw:
            if not isinstance(row, Mapping) or set(row) != _CONNECTOR_ROW_KEYS:
                raise EveRefusal("output_shape_invalid")
            case_id: Any = row["case_id"]
            answer: Any = row["answer"]
            citations: Any = row["citations"]
            abstained: Any = row["abstained"]
            if not isinstance(citations, list | tuple):
                raise EveRefusal("output_shape_invalid")
            try:
                outputs.append(ArmOutput(case_id, answer, tuple(citations), abstained))
            except (TypeError, ValueError):
                raise EveRefusal("output_shape_invalid") from None
        return tuple(outputs)


# ---------------------------------------------------------------- the evaluator
class EveEvaluator:
    """The independent EVE evaluator: its own identity, sealed inputs, scorer and custody."""

    def __init__(
        self,
        *,
        evaluator_id: str,
        custody_dir: str | Path,
        worker_data_dirs: Sequence[str | Path],
        scorer: IndependentGenerativeScorer,
        answer_key: AnswerKey,
        cases: tuple[EveCase, ...],
        retention_seconds: int = DEFAULT_RETENTION_SECONDS,
        validity_seconds: int = DEFAULT_VALIDITY_SECONDS,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if type(evaluator_id) is not str or not _ID.fullmatch(evaluator_id):
            raise EveRefusal("invalid_evaluator_identity")
        if isinstance(worker_data_dirs, str | Path) or not worker_data_dirs:
            # The separation check is the only thing keeping custody off worker data; it is
            # mandatory, never skipped for an empty or missing list.
            raise EveRefusal("worker_data_dirs_required")
        root = Path(custody_dir)
        if not root.is_absolute():
            raise EveRefusal("evaluator_custody_not_private")
        for other in worker_data_dirs:
            if _overlaps(root, Path(other)):
                raise EveRefusal("evaluator_custody_overlaps_worker_data")
        qualified, _ = qualify_scorer(scorer)
        if not qualified:
            raise EveRefusal("evaluator_scorer_unqualified")
        if (
            type(retention_seconds) is not int
            or not 0 < retention_seconds <= DEFAULT_RETENTION_SECONDS
            or type(validity_seconds) is not int
            or not 0 < validity_seconds <= DEFAULT_VALIDITY_SECONDS
        ):
            raise EveRefusal("evaluator_retention_invalid")
        ids = [c.case_id for c in cases]
        if (
            type(answer_key) is not AnswerKey
            or not cases
            or len(set(ids)) != len(ids)
            or answer_key.case_ids() != frozenset(ids)
        ):
            raise EveRefusal("evaluator_inputs_unbound")
        self.evaluator_id = evaluator_id
        self.scorer = scorer
        self.retention_seconds = retention_seconds
        self.validity_seconds = validity_seconds
        self._clock = clock
        self._key = answer_key
        self._cases = {c.case_id: c for c in cases}
        self._worker_dirs = tuple(Path(d) for d in worker_data_dirs)
        self._root = root.resolve()
        for sub in ("", "verdicts", "predictions", "archive"):
            _private_dir(self._root / sub)
        key_path = self._root / "evaluator.key"
        if _read_private(key_path) is None:
            _write_once(key_path, secrets.token_bytes(32))
        key = _read_private(key_path)
        if key is None or len(key) != 32:
            raise EveRefusal("evaluator_key_corrupt")
        self._seal_key = key

    def __repr__(self) -> str:
        return f"EveEvaluator(evaluator_id={self.evaluator_id!r}, cases={len(self._cases)})"

    # ---- sealing
    def _seal(self, value: object) -> str:
        return hmac.new(self._seal_key, canonical(value), hashlib.sha256).hexdigest()

    def _now(self) -> int:
        return int(self._clock())

    def verdict_key(self, manifest_digest: str, result_digest: str) -> str:
        return digest(
            [
                VERSION,
                self.evaluator_id,
                self.scorer.scorer_id,
                self.scorer.version,
                self.scorer.rubric_revision,
                manifest_digest,
                result_digest,
            ]
        )[7:]

    def _verdict_path(self, key: str) -> Path:
        if not _HEX64.fullmatch(key):
            raise EveRefusal("invalid_verdict_key")
        return self._root / "verdicts" / f"{key}.json"

    def read_verdict(self, key: str) -> dict[str, Any] | None:
        """The verdict re-read from custody with its seal checked (``locally_keyed``)."""
        raw = _read_private(self._verdict_path(key))
        if raw is None:
            return None
        try:
            stored = json.loads(raw)
            document, seal = stored["document"], stored["local_seal"]
            body = {k: v for k, v in document.items() if k != "verdict_digest"}
            consistent = document["verdict_digest"] == digest(body)
        except (ValueError, KeyError, TypeError):
            raise EveRefusal("stored_verdict_corrupt") from None
        if not (
            consistent
            and isinstance(seal, str)
            and hmac.compare_digest(seal, self._seal({"document": document}))
            and document.get("verdict_key") == key
        ):
            raise EveRefusal("stored_verdict_tampered")
        return dict(document)

    def private_predictions(self, verdict_key: str, *, accessor_id: str) -> dict[str, Any]:
        """The private per-case record while it is within its retention limit.

        ``accessor_id`` is a caller-asserted label, not authentication (see the module notes).
        """
        if accessor_id != self.evaluator_id:
            raise EveRefusal("private_predictions_access_denied")
        document = self.read_verdict(verdict_key)
        if document is None:
            raise EveRefusal("verdict_absent")
        commitment = document["predictions_commitment"]
        if not isinstance(commitment, str) or not _HEX64.fullmatch(commitment):
            raise EveRefusal("stored_verdict_tampered")
        raw = _read_private(self._root / "predictions" / f"{commitment}.json")
        if raw is None:
            raise EveRefusal("private_predictions_absent_or_purged")
        try:
            record = json.loads(raw)["record"]
        except (ValueError, KeyError, TypeError):
            raise EveRefusal("private_predictions_corrupt") from None
        # The retention limit is inside the sealed record, so it cannot be extended by an edit.
        if not hmac.compare_digest(self._seal(record), commitment):
            raise EveRefusal("private_predictions_tampered")
        until = record.get("retention_until") if isinstance(record, dict) else None
        if type(until) is not int or until != document["retention_until"]:
            raise EveRefusal("private_predictions_tampered")
        if until <= self._now():
            raise EveRefusal("private_predictions_expired")
        return {"retention_until": until, "record": record}

    def purge_expired(self) -> int:
        """Delete private predictions past their retention limit; verdicts keep the commitment.

        A record whose seal no longer matches its name, or that cannot be read, is deleted too:
        retention fails toward deletion, never toward keeping a record nobody can vouch for.
        """
        removed = 0
        for path in sorted((self._root / "predictions").glob("*.json")):
            try:
                record = json.loads(_read_private(path) or b"")["record"]
                sealed = hmac.compare_digest(self._seal(record), path.stem)
                until = record["retention_until"]
                keep = sealed and type(until) is int and until > self._now()
            except (ValueError, KeyError, TypeError, EveRefusal):
                keep = False
            if not keep:
                path.unlink()
                removed += 1
        return removed

    def recompute(self, verdict_key: str, *, accessor_id: str) -> dict[str, Any]:
        """Re-score from the private predictions alone and compare with the sealed verdict.

        No model is called: this is the re-score from the predictions the evaluator holds, against
        its own sealed answer key, with its own scorer. ``matches`` is False when any arm's recount
        differs from the verdict's.
        """
        document = self.read_verdict(verdict_key)
        if document is None:
            raise EveRefusal("verdict_absent")
        record = self.private_predictions(verdict_key, accessor_id=accessor_id)["record"]
        if record.get("scorer") != [
            self.scorer.scorer_id,
            self.scorer.version,
            self.scorer.rubric_revision,
        ]:
            raise EveRefusal("evaluator_scorer_changed")
        arms: dict[str, Any] = {}
        all_verdicts: dict[str, list[str]] = {}
        try:
            for arm_id, entry in sorted(record["arms"].items()):
                spec = ArmSpec(*entry["spec"])
                verdicts, answered = [], []
                for row in entry["rows"]:
                    output = _output_from(row["case_id"], row["prediction"])
                    shown = frozenset(row["shown"])
                    verdict = self.scorer.judge(self._key.entry(row["case_id"]), shown, output)
                    verdicts.append(verdict)
                    answered.append(
                        output is not None and not output.abstained and output.answer is not None
                    )
                arms[arm_id] = _totals(
                    spec, verdicts, answered, entry["eligible"], entry["excluded"]
                )
                all_verdicts[arm_id] = verdicts
        except (KeyError, TypeError, ValueError):
            raise EveRefusal("private_predictions_corrupt") from None
        every = all(arm["replayed"] for arm in document["arms"].values())
        paired, claim = _comparison(all_verdicts) if all_verdicts and every else (None, None)
        stored_arms = {
            name: arm["totals"] for name, arm in document["arms"].items() if arm["replayed"]
        }
        matches = (
            _same(stored_arms, arms)
            and _same(document["paired_comparison"], paired)
            and _same(document["improvement_claim"], claim)
        )
        return {
            "verdict_key": verdict_key,
            "basis": "re-scored from evaluator-held predictions; no model call",
            "matches": matches,
            "arms": arms,
            "paired_comparison": paired,
            "improvement_claim": claim,
        }

    # ---- evaluation
    def _check_bound(
        self,
        manifest: ReplayManifest,
        body: Mapping[str, Any],
        supplied_digest: str,
        registration: EveTaskRegistration,
        reasons: _Reasons,
    ) -> bool:
        """Everything that must hold before any model call. False means do not replay."""
        before = len(reasons.items)
        cases = tuple(self._cases.values())
        if digest(body) != supplied_digest:
            reasons.add(_INTEGRITY, "result_digest_mismatch")
        if body.get("manifest_digest") != manifest.digest:
            reasons.add(_INTEGRITY, "result_manifest_mismatch")
        if (
            replay_membership(cases) != manifest.membership_digest
            or cases_content_digest(cases) != manifest.content_digest
            or self._key.digest != manifest.answer_key_digest
        ):
            reasons.add(_INTEGRITY, "replay_not_bound")
        eligible, excluded = [], []
        for case_id in sorted(self._cases):
            expectation = self._key.entry(case_id).expectation
            if expectation in (Expectation.ANSWER, Expectation.ABSTAIN):
                eligible.append(case_id)
            else:
                excluded.append((case_id, expectation.value))
        if tuple(eligible) != manifest.eligible_case_ids or tuple(excluded) != manifest.excluded:
            reasons.add(_INTEGRITY, "eligibility_mismatch")
        if manifest.rubric_revision != self.scorer.rubric_revision:
            reasons.add(_INTEGRITY, "rubric_revision_mismatch")
        if registration.identity != manifest.registration_identity:
            reasons.add(_INTEGRITY, "registration_mismatch")
        if registration.decoding.temperature_milli != 0:
            reasons.add(_HELD, "stochastic_decoding_unsupported")
        if (
            body.get("schema_version") != EXPECTED_RESULT_SCHEMA
            or body.get("evidence_tier") != EXPECTED_RESULT_TIER
            or body.get("claim_scope") != EXPECTED_CLAIM_SCOPE
            or body.get("fit_authorized") is not False
            or body.get("release_authorized") is not False
        ):
            reasons.add(_INTEGRITY, "claim_overstated")
        return len(reasons.items) == before

    def _journaled(
        self, manifest: ReplayManifest, journal: ReplayJournal, reasons: _Reasons
    ) -> dict[str, ArmRecord] | None:
        try:
            records = journal.arms_for(manifest.digest)
        except EveRefusal as refusal:
            reasons.add(_INTEGRITY, f"journal_refused_{refusal}")
            return None
        found: dict[str, ArmRecord] = {}
        frozen = (manifest.scorer_id, manifest.scorer_version, manifest.rubric_revision)
        expected_refs = [_case_ref(manifest.digest, c) for c in manifest.eligible_case_ids]
        for record in records:
            arm_id = record.spec.arm_id
            if arm_id in found:
                reasons.add(_INTEGRITY, "arm_ambiguous")
                return None
            recorded = record.scorer
            if (
                recorded is None
                or (recorded.scorer_id, recorded.scorer_version, recorded.rubric_revision) != frozen
                or recorded.selfcheck_passed is not True
                or any(
                    (r.scorer_id, r.scorer_version, r.rubric_revision) != frozen
                    for r in record.rows
                )
            ):
                reasons.add(_INTEGRITY, "verdict_attribution_mismatch")
                return None
            if [r.ref for r in record.rows] != expected_refs:
                reasons.add(_INTEGRITY, "journal_rows_incomplete")
                return None
            found[arm_id] = record
        return found

    def _replay_arm(
        self,
        manifest: ReplayManifest,
        registration: EveTaskRegistration,
        record: ArmRecord,
        inference: GenerativeInference | EveRefusal | None,
        retriever: Retriever | None,
        journal: ReplayJournal,
        keys: CommitmentKeyStore,
        reasons: _Reasons,
    ) -> _ArmCheck:
        spec = record.spec
        check = _ArmCheck(spec)
        arm = spec.arm_id
        if inference is None:
            reasons.add(_HELD, f"evaluator_replay_unavailable_{arm}")
            return check
        if isinstance(inference, EveRefusal):  # the replay could not even be built, and why
            reasons.add(_INTEGRITY, f"{inference}_{arm}")
            return check
        if spec.decoding_digest != registration.decoding.digest:
            reasons.add(_INTEGRITY, f"decoding_drift_{arm}")
            return check
        if inference.served_revision != spec.served_revision:
            reasons.add(_INTEGRITY, f"served_identity_mismatch_{arm}")
            return check
        if arm == "candidate":
            if (
                not isinstance(inference, CandidateInference | EvaluatorCandidateReplay)
                or type(inference) not in (CandidateInference, EvaluatorCandidateReplay)
                or inference.benchmark_revision != manifest.benchmark_revision
                or spec.served_revision == registration.base_revision
            ):
                reasons.add(_INTEGRITY, "candidate_lineage_unverified")
                return check
        elif spec.served_revision != registration.base_revision:
            reasons.add(_INTEGRITY, f"served_identity_mismatch_{arm}")
            return check

        rows_by_ref = {r.ref: r for r in record.rows}
        requests: list[ReplayRequest] = []
        shown: dict[str, frozenset[str]] = {}
        for case_id in manifest.eligible_case_ids:
            case = self._cases[case_id]
            try:
                passages: tuple[Passage, ...] = (
                    tuple(retriever.retrieve(spec, case)) if retriever is not None else case.context
                )
                if not all(type(p) is Passage for p in passages):
                    raise TypeError("not a passage")
            except Exception:  # a retriever that cannot reproduce the inputs fails the replay
                reasons.add(_INTEGRITY, f"evaluator_retrieval_failed_{arm}")
                return check
            if self._key.leaks([[p.passage_id, p.text] for p in passages]):
                reasons.add(_INTEGRITY, f"protected_answer_in_request_{arm}")
                return check
            retrieval = digest(sorted(p.passage_id for p in passages))
            if rows_by_ref[_case_ref(manifest.digest, case_id)].retrieval != retrieval:
                reasons.add(_INTEGRITY, f"retrieval_mismatch_{arm}")
                return check
            shown[case_id] = frozenset(p.passage_id for p in passages)
            requests.append(ReplayRequest(case_id, case.question, passages))
        if self._key.leaks([r.as_mapping() for r in requests]):
            reasons.add(_INTEGRITY, f"protected_answer_in_request_{arm}")
            return check

        try:
            raw = inference.generate(spec, tuple(requests))
        except OSError:
            reasons.add(_HELD, f"evaluator_inference_unavailable_{arm}")
            return check
        except Exception:  # any other failure to replay is a reproduction failure
            reasons.add(_INTEGRITY, f"evaluator_replay_failed_{arm}")
            return check
        outputs: dict[str, ArmOutput] = {}
        try:
            if type(raw) is not tuple:
                raise TypeError("not a tuple")
            for item in raw:
                if type(item) is not ArmOutput or type(item.citations) is not tuple:
                    raise TypeError("not an exact ArmOutput")
                rebuilt = ArmOutput(item.case_id, item.answer, item.citations, item.abstained)
                if rebuilt.case_id in outputs or rebuilt.case_id not in shown:
                    raise ValueError("unrequested or duplicate case")
                outputs[rebuilt.case_id] = rebuilt
        except (TypeError, ValueError):
            reasons.add(_INTEGRITY, f"evaluator_output_invalid_{arm}")
            return check

        check.replayed = True
        attribution = [self.scorer.scorer_id, self.scorer.version, self.scorer.rubric_revision]
        for case_id in manifest.eligible_case_ids:
            output = outputs.get(case_id)
            ref = _case_ref(manifest.digest, case_id)
            same: bool | None
            try:
                same = verify_output(
                    keys=keys,
                    journal=journal,
                    manifest=manifest,
                    arm=spec,
                    case_id=case_id,
                    presented=output,
                )
            except EveRefusal as refusal:
                # Could not check (for example the run key is missing): a hold, never a stored
                # failure, so restoring the key lets the next evaluation proceed. Only a
                # commitment that was checked and did not match is an integrity failure.
                reasons.add(_HELD, f"commitment_unverifiable_{refusal}")
                same = None
            if same is True:
                check.committed += 1
            elif same is False:
                check.not_committed += 1
            else:
                check.unverifiable += 1
            verdict = self.scorer.judge(self._key.entry(case_id), shown[case_id], output)
            if verdict not in VERDICTS:
                raise EveRefusal("evaluator_scorer_unqualified")
            if rows_by_ref[ref].verdict.value != verdict:
                check.disagreements += 1
            check.verdicts.append(verdict)
            check.answered.append(
                output is not None and not output.abstained and output.answer is not None
            )
            check.private_rows.append(
                {
                    "case_id": case_id,
                    "ref": ref,
                    "arm": arm,
                    "shown": sorted(shown[case_id]),
                    "prediction": _prediction(output),
                    "verdict": verdict,
                    "committed": same,
                    "scorer_id": attribution[0],
                    "scorer_version": attribution[1],
                    "rubric_revision": attribution[2],
                }
            )
        if check.not_committed:
            reasons.add(_INTEGRITY, f"outputs_not_committed_{arm}")
        if check.disagreements:
            reasons.add(_INTEGRITY, f"verdict_disagreement_{arm}")
        return check

    def evaluate(
        self,
        *,
        manifest: ReplayManifest,
        result: Mapping[str, Any],
        registration: EveTaskRegistration,
        journal: ReplayJournal,
        keys: CommitmentKeyStore,
        replays: Mapping[str, GenerativeInference | EveRefusal],
        retrievers: Mapping[str, Retriever] | None = None,
    ) -> dict[str, Any]:
        """Replay, bind, re-score and recount one claimed result; store and return the verdict.

        ``replays`` maps each arm to the inference that serves it; an ``EveRefusal`` in its place
        records why that replay could not be built (for example a candidate whose lineage did not
        verify), which is an integrity failure for the arm.

        One verdict exists per (evaluator, scorer, manifest, result) key. Evaluating the same
        claim again returns the stored verdict, re-read under its seal, while it is valid; a
        verdict past ``not_after`` is archived and the claim evaluated afresh.
        """
        for path in (journal.path, keys.path):
            if _overlaps(self._root, path.parent):
                raise EveRefusal("evaluator_custody_overlaps_worker_data")
        if (
            manifest.scorer_id in (self.evaluator_id, self.scorer.scorer_id)
            or manifest.scorer_version == self.scorer.version
        ):
            raise EveRefusal("evaluator_not_independent")
        try:
            body, supplied = result["body"], result["result_digest"]
        except (KeyError, TypeError):
            raise EveRefusal("claim_malformed") from None
        if not isinstance(body, Mapping) or type(supplied) is not str:
            raise EveRefusal("claim_malformed")
        key = self.verdict_key(manifest.digest, supplied)
        stored = self.read_verdict(key)
        if stored is not None:
            # A held verdict is never final (the check could not run); a stale one is re-run.
            if stored["outcome"] != _HELD and stored["not_after"] > self._now():
                return stored
            archived = self._root / "archive" / f"{key}.{stored['issued_at']}.json"
            os.replace(self._verdict_path(key), archived)

        reasons = _Reasons()
        checks: dict[str, _ArmCheck] = {}
        if self._check_bound(manifest, body, supplied, registration, reasons):
            journaled = self._journaled(manifest, journal, reasons)
            claimed = body.get("arms")
            if journaled is not None and isinstance(claimed, Mapping):
                if set(claimed) - set(journaled):
                    reasons.add(_INTEGRITY, "arm_not_journaled")
                if set(journaled) - set(claimed):
                    reasons.add(_INTEGRITY, "arm_omitted_from_result")
                for arm_id in [a for a in ARMS if a in claimed and a in journaled]:
                    record = journaled[arm_id]
                    claimed_arm = claimed[arm_id]
                    if not isinstance(claimed_arm, Mapping) or (
                        claimed_arm.get("spec_digest") != record.spec.digest
                    ):
                        reasons.add(_INTEGRITY, f"arm_identity_mismatch_{arm_id}")
                        continue
                    checks[arm_id] = self._replay_arm(
                        manifest,
                        registration,
                        record,
                        replays.get(arm_id),
                        (retrievers or {}).get(arm_id),
                        journal,
                        keys,
                        reasons,
                    )
            elif journaled is not None:
                reasons.add(_INTEGRITY, "claim_malformed")

        eligible, excluded = len(manifest.eligible_case_ids), len(manifest.excluded)
        arms_doc: dict[str, Any] = {}
        recounts: dict[str, list[str]] = {}
        for arm_id, check in checks.items():
            totals = (
                _totals(check.spec, check.verdicts, check.answered, eligible, excluded)
                if check.replayed
                else None
            )
            if totals is not None:
                recounts[arm_id] = check.verdicts
                claimed_totals = body["arms"][arm_id]
                if not all(_same(claimed_totals.get(f), v) for f, v in totals.items()) or set(
                    claimed_totals
                ) != set(totals):
                    reasons.add(_INTEGRITY, f"claimed_totals_mismatch_{arm_id}")
            arms_doc[arm_id] = {
                "spec_digest": check.spec.digest,
                "served_revision": check.spec.served_revision,
                "replayed": check.replayed,
                "outputs_committed": check.committed,
                "outputs_not_committed": check.not_committed,
                "outputs_unverifiable": check.unverifiable,
                "verdict_disagreements": check.disagreements,
                "totals": totals,
            }
        paired: dict[str, Any] | None = None
        claim: str | None = None
        if checks and all(c.replayed for c in checks.values()):
            paired, claim = _comparison(recounts)
            if not _same(body.get("paired_comparison"), paired) or not _same(
                body.get("improvement_claim"), claim
            ):
                reasons.add(_INTEGRITY, "claimed_comparison_mismatch")
        elif not reasons.items:
            reasons.add(_HELD, "no_arm_replayed")

        now = self._now()
        record_doc = {
            "retention_until": now + self.retention_seconds,
            "manifest_digest": manifest.digest,
            "result_digest": supplied,
            "scorer": [self.scorer.scorer_id, self.scorer.version, self.scorer.rubric_revision],
            "arms": {
                arm_id: {
                    "spec": [
                        check.spec.arm_id,
                        check.spec.served_revision,
                        check.spec.source_snapshot_id,
                        check.spec.retrieval_settings_id,
                        check.spec.context_revision,
                        check.spec.decoding_digest,
                    ],
                    "eligible": eligible,
                    "excluded": excluded,
                    "rows": check.private_rows,
                }
                for arm_id, check in checks.items()
                if check.replayed
            },
        }
        commitment = self._seal(record_doc)
        document: dict[str, Any] = {
            "schema_version": VERSION,
            "verdict_key": key,
            "evaluator_id": self.evaluator_id,
            "scorer": {
                "scorer_id": self.scorer.scorer_id,
                "version": self.scorer.version,
                "rubric_revision": self.scorer.rubric_revision,
                "qualified": True,
            },
            "worker_scorer": {
                "scorer_id": manifest.scorer_id,
                "version": manifest.scorer_version,
                "rubric_revision": manifest.rubric_revision,
            },
            "benchmark_id": manifest.benchmark_id,
            "benchmark_revision": manifest.benchmark_revision,
            "registration_identity": manifest.registration_identity,
            "manifest_digest": manifest.digest,
            "result_digest": supplied,
            "arms": arms_doc,
            "paired_comparison": paired,
            "improvement_claim": claim,
            "outcome": reasons.outcome(),
            "reasons": reasons.reasons(),
            "basis": BASIS,
            "verdicts_rechecked": True,
            "predictions_custody": PREDICTIONS_CUSTODY,
            "predictions_commitment": commitment,
            "retention_until": now + self.retention_seconds,
            "issued_at": now,
            "not_after": now + self.validity_seconds,
            "evidence_tier": EVIDENCE_TIER,
            "claim_scope": CLAIM_SCOPE,
            "custody_tier": STORE_TIER,
            "export_tier": EXPORT_TIER,
            "custody_separated": False,
        }
        document["verdict_digest"] = digest(document)
        check_egress(self._key, "verdict", document)
        _write_once(
            self._root / "predictions" / f"{commitment}.json", canonical({"record": record_doc})
        )
        sealed = {"document": document, "local_seal": self._seal({"document": document})}
        if not _write_once(self._verdict_path(key), canonical(sealed)):
            existing = self.read_verdict(key)  # a concurrent evaluation of the same claim won
            if existing is None:
                raise EveRefusal("evaluator_custody_unavailable")
            return existing
        return document


# ---------------------------------------------------------------- separate-process entry
CONFIG = "opentour.pilot.eve-evaluator-config.v1"
SEALED_INPUTS = "opentour.pilot.eve-sealed-inputs.v1"
RESULT = "opentour.pilot.eve-evaluator-result.v1"
EXPORT_NAME = "eve-independent-verdict.json"
_MAX_BYTES = 4 << 20
_CONFIG_KEYS = frozenset(
    {
        "schema_version",
        "evaluator_id",
        "rubric_revision",
        "custody_dir",
        "sealed_inputs",
        "worker",
        "export_dir",
    }
)
_WORKER_KEYS = frozenset(
    {"manifest", "result", "fit", "journal", "keys", "backend_dir", "data_dirs"}
)
_FIXED_REASON = re.compile(r"[a-z][a-z0-9_]{0,95}")


def _fixed_reason(error: BaseException) -> str:
    """A fixed lowercase refusal code, or a generic hold; a raw exception's text never prints."""
    from opentour_cell.pilot.eve_reference_backend import ReferenceBackendError

    if isinstance(error, EveRefusal | ReferenceBackendError | mc.CapabilityError):
        reason = str(error)
        if _FIXED_REASON.fullmatch(reason):
            return reason
    return "eve_evaluator_unavailable"


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    found: dict[str, Any] = {}
    for name, value in pairs:
        if name in found:
            raise ValueError("duplicate JSON member")
        found[name] = value
    return found


def _load(path: Path, missing: str) -> Any:
    raw = _read_private(path)
    if raw is None or len(raw) > _MAX_BYTES:
        raise EveRefusal(missing)
    try:
        return json.loads(raw, object_pairs_hook=_unique)
    except ValueError:
        raise EveRefusal(missing) from None


def _absolute(value: Any) -> Path:
    if type(value) is not str or not value or not Path(value).is_absolute():
        raise EveRefusal("invalid_evaluator_configuration")
    return Path(value)


def _parse_sealed(raw: Any) -> tuple[EveTaskRegistration, tuple[EveCase, ...], AnswerKey]:
    """The evaluator's own sealed copy: registration, cases and answer key, read strictly."""
    try:
        if (
            not isinstance(raw, dict)
            or set(raw) != {"schema_version", "registration", "cases", "answers"}
            or raw["schema_version"] != SEALED_INPUTS
        ):
            raise ValueError("sealed inputs")
        registration = parse_registration(raw["registration"])
        cases = []
        for item in raw["cases"]:
            if set(item) != {"case_id", "family_id", "question", "context"}:
                raise ValueError("case")
            passages = tuple(Passage(*p) for p in item["context"])
            if not all(type(p.passage_id) is str and type(p.text) is str for p in passages):
                raise ValueError("passage")
            values = (item["case_id"], item["family_id"], item["question"])
            if not all(type(v) is str and v for v in values):
                raise ValueError("case")
            cases.append(EveCase(*values, passages))
        entries = {}
        for case_id, entry in raw["answers"].items():
            if set(entry) != {"expectation", "answer", "required_citations"}:
                raise ValueError("answer")
            required = entry["required_citations"]
            if type(entry["answer"]) is not str or not all(type(c) is str for c in required):
                raise ValueError("answer")
            entries[case_id] = ProtectedAnswer(
                Expectation(entry["expectation"]), entry["answer"], frozenset(required)
            )
        return registration, tuple(cases), AnswerKey(entries)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise EveRefusal("evaluator_inputs_unbound") from None


@dataclass(frozen=True)
class EvaluatorSetup:
    """One evaluator process's configuration, read from its private config file.

    The config names the evaluator's identity and rubric, its custody directory, its own sealed
    inputs inside that directory, the worker files it reads (never writes) and the directory it
    exports its verdict document to. Every worker location is a worker data directory, so custody
    can overlap none of them.
    """

    evaluator_id: str
    rubric_revision: str
    custody_dir: Path
    sealed_inputs: Path
    worker: Mapping[str, Path]
    worker_data_dirs: tuple[Path, ...]
    export_dir: Path

    @staticmethod
    def load(config_path: Path) -> EvaluatorSetup:
        if not config_path.is_absolute():
            raise EveRefusal("invalid_evaluator_configuration")
        raw = _load(config_path, "invalid_evaluator_configuration")
        if (
            not isinstance(raw, dict)
            or set(raw) != _CONFIG_KEYS
            or raw["schema_version"] != CONFIG
            or not isinstance(raw["worker"], dict)
            or set(raw["worker"]) != _WORKER_KEYS
            or not isinstance(raw["worker"]["data_dirs"], list)
            or type(raw["rubric_revision"]) is not str
            or type(raw["evaluator_id"]) is not str
        ):
            raise EveRefusal("invalid_evaluator_configuration")
        custody = _absolute(raw["custody_dir"])
        sealed = _absolute(raw["sealed_inputs"])
        if sealed.parent.resolve() != custody.resolve():
            raise EveRefusal("invalid_evaluator_configuration")
        worker = {
            name: _absolute(raw["worker"][name])
            for name in ("manifest", "result", "fit", "journal", "keys", "backend_dir")
        }
        dirs = {_absolute(d) for d in raw["worker"]["data_dirs"]}
        dirs |= {worker["journal"].parent, worker["keys"].parent, worker["backend_dir"]}
        dirs |= {worker[name].parent for name in ("manifest", "result", "fit")}
        export_dir = _absolute(raw["export_dir"])
        if _overlaps(export_dir, custody):
            raise EveRefusal("evaluator_custody_overlaps_worker_data")
        return EvaluatorSetup(
            evaluator_id=raw["evaluator_id"],
            rubric_revision=raw["rubric_revision"],
            custody_dir=custody,
            sealed_inputs=sealed,
            worker=worker,
            worker_data_dirs=tuple(sorted(dirs)),
            export_dir=export_dir,
        )

    def evaluator(self) -> tuple[EveEvaluator, EveTaskRegistration]:
        registration, cases, key = _parse_sealed(
            _load(self.sealed_inputs, "evaluator_inputs_unbound")
        )
        evaluator = EveEvaluator(
            evaluator_id=self.evaluator_id,
            custody_dir=self.custody_dir,
            worker_data_dirs=self.worker_data_dirs,
            scorer=IndependentGenerativeScorer(self.rubric_revision),
            answer_key=key,
            cases=cases,
        )
        return evaluator, registration

    def evaluate(
        self, evaluator: EveEvaluator, registration: EveTaskRegistration
    ) -> dict[str, Any]:
        """Replay every journaled arm through the generated reference backend, read-only."""
        from opentour_cell.pilot.eve_reference_backend import ReferenceEveBackend

        manifest = ReplayManifest.parse(_load(self.worker["manifest"], "worker_outbox_unavailable"))
        result = _load(self.worker["result"], "worker_outbox_unavailable")
        backend = ReferenceEveBackend(self.worker["backend_dir"])
        replays: dict[str, GenerativeInference | EveRefusal] = {
            "baseline": backend.base_inference(),
            "simple_repair": backend.base_inference(),
        }
        fit = _read_private(self.worker["fit"])
        if fit is not None:
            try:
                job, candidate = parse_fit_record(json.loads(fit, object_pairs_hook=_unique))
                replays["candidate"] = EvaluatorCandidateReplay(
                    backend, job, candidate, registration
                )
            except EveRefusal as refusal:
                replays["candidate"] = refusal
            except ValueError:
                replays["candidate"] = EveRefusal("fit_record_malformed")
        return evaluator.evaluate(
            manifest=manifest,
            result=result,
            registration=registration,
            journal=ReplayJournal(self.worker["journal"]),
            keys=CommitmentKeyStore(self.worker["keys"]),
            replays=replays,
        )

    def export(self, document: Mapping[str, Any]) -> Path:
        """Write the verdict document for the worker. It is digest self-consistent only."""
        self.export_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        target = self.export_dir / EXPORT_NAME
        fd, tmp = tempfile.mkstemp(dir=self.export_dir, prefix=".pending-")
        with os.fdopen(fd, "wb") as handle:
            handle.write(canonical(dict(document)))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        return target


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise EveRefusal("invalid_evaluator_arguments")


def _emit(result: Mapping[str, Any]) -> None:
    print(canonical(dict(result)).decode(), flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the independent evaluator as its own process: ``evaluate`` or ``recompute``.

    Output is bounded: the verdict key, outcome, fixed reasons and counts. It never prints a
    case, a prediction, an answer or a raw exception.
    """
    parser = _Parser(description="Run the independent EVE evaluator as its own process.")
    parser.add_argument("command", choices=("evaluate", "recompute"))
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--verdict-key")
    try:
        args = parser.parse_args(argv)
        setup = EvaluatorSetup.load(args.config)
        evaluator, registration = setup.evaluator()
        if args.command == "recompute":
            if type(args.verdict_key) is not str:
                raise EveRefusal("invalid_evaluator_arguments")
            recomputed = evaluator.recompute(args.verdict_key, accessor_id=evaluator.evaluator_id)
            matches = recomputed["matches"] is True
            _emit(
                {
                    "schema_version": RESULT,
                    "status": "recomputed" if matches else "held",
                    "verdict_key": args.verdict_key,
                    "matches": matches,
                    "improvement_claim": recomputed["improvement_claim"],
                    "reason": None if matches else "evaluator_recompute_mismatch",
                }
            )
            return 0 if matches else 2
        document = setup.evaluate(evaluator, registration)
        setup.export(document)
        _emit(
            {
                "schema_version": RESULT,
                "status": document["outcome"],
                "verdict_key": document["verdict_key"],
                "manifest_digest": document["manifest_digest"],
                "result_digest": document["result_digest"],
                "improvement_claim": document["improvement_claim"],
                "reasons": document["reasons"],
                "outputs_committed": sum(a["outputs_committed"] for a in document["arms"].values()),
                "verdict_disagreements": sum(
                    a["verdict_disagreements"] for a in document["arms"].values()
                ),
            }
        )
        return 0 if document["outcome"] == "reproduced" else 2
    except (EveRefusal, OSError, ValueError, TypeError, LookupError) as error:
        _emit({"schema_version": RESULT, "status": "held", "reason": _fixed_reason(error)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
