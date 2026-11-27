"""Bind generative EVE scoring to one frozen protected replay.

A replay manifest freezes, before any arm runs, the registered model identity, scorer and rubric
revisions, protected-membership digest, answer-key digest, eligible and excluded cases and the
decoding policy. An arm runs only when the model, scorer, replay set and answer key all match that
manifest; any drift is refused, never coerced. Replay workers receive the untouched question and
approved context only. Protected answers reach only the scorer, and every outbound surface
(inference request, retrieval result, fit payload, log line, journal record and result record)
is checked for protected-answer text before it leaves. Arm runs are journaled by an exact run
key, so a retry, a restart or a duplicate dispatch records one result, and a late or duplicate
scoring callback cannot change a settled scorecard. The versioned result carries counts that map
onto the OPE-555 scorecard arm fields; a fix, a benchmark score and a release remain separate
records.

Worker journal (OPE-574, owner ruling A + C, following OPE-528): the replay journal stays
redacted and holds no candidate output. Each journaled verdict row instead carries the scorer and
rubric that produced it and a keyed commitment over the exact output that was scored (HMAC-SHA256
under a per-run key, never a bare digest, because short answers are guessable by enumeration).
Given an output presented later and the run key, ``verify_output`` detects substitution. Nothing in
this module or in ``eve_aggregate`` re-scores a verdict; what they offer is "aggregation re-derived
from journaled verdicts". Re-scoring belongs to the separate evaluator that OPE-528 defines, which
replays under its own identity and keeps per-case predictions privately under its own custody; it
is not built here. The scorer implementation lives in ``eve_scorer`` and the aggregator in
``eve_aggregate``; this module imports neither.

The run key lives in a ``CommitmentKeyStore`` file, never in the journal row it protects, and the
worker refuses a key store under, or linked to, the journal. This is the ``locally_keyed`` evidence
rung: the same party holds the key and the record, so a commitment is tamper evidence for
ourselves, not proof to a third party, and relocating the key does not change that (custody is
OPE-566's). Journal readers check that a commitment is present, never its value.

Simulated: tests use generated sentinel cases and answers, a deterministic scorer, a deterministic
inference double and an isolated trainer-transport double. Not proven: no real EVE inference, GPU,
trainer, provider or Pi School reviewer exists here, the scorer is an exact-match stand-in and not
the independent human or model evaluator, and no statistical interval is computed. Results carry
the ``generated`` evidence tier, claim scope ``implementation_only`` and list their unsatisfied
external gates. They are software behavior only and never provider or customer evidence.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.eve_task import (
    Capability,
    EveCase,
    EveRefusal,
    EveRegistry,
    EveTaskRegistration,
    FitPlan,
    Passage,
    binding_for,
    canonical,
    digest,
    normalize_text,
    plan_fit,
)

VERSION = "opentour.pilot.eve-protected-replay.v1"
TIER = "generated"
EXTERNAL_GATES = (
    "real_eve_inference_capacity_unavailable",
    "provider_or_trainer_authorization_unavailable",
    "independent_evaluator_and_reviewer_unavailable",
    "installed_provider_and_customer_evidence_unavailable",
)
ARMS = ("baseline", "simple_repair", "candidate")


class Expectation(StrEnum):
    ANSWER = "answer"
    ABSTAIN = "abstain"
    UNAVAILABLE = "unavailable"
    DISPUTED = "disputed"


class Verdict(StrEnum):
    CORRECT_ANSWER = "correct_answer"
    CORRECT_ABSTENTION = "correct_abstention"
    SERIOUS_ERROR = "serious_error"
    INAPPROPRIATE_REFUSAL = "inappropriate_refusal"
    UNVERIFIED = "unverified"
    MISSING = "missing_output"


_CORRECT = {Verdict.CORRECT_ANSWER, Verdict.CORRECT_ABSTENTION}


@dataclass(frozen=True)
class ProtectedAnswer:
    expectation: Expectation
    answer: str = field(repr=False)
    required_citations: frozenset[str]


class AnswerKey:
    """Evaluator-only protected answers. Its representation never shows an answer."""

    def __init__(self, entries: Mapping[str, ProtectedAnswer]) -> None:
        self._entries = dict(entries)
        self.digest = digest(
            {
                case_id: [
                    e.expectation.value,
                    hashlib.sha256(e.answer.encode()).hexdigest(),
                    sorted(e.required_citations),
                ]
                for case_id, e in sorted(self._entries.items())
            }
        )

    def __repr__(self) -> str:
        return f"AnswerKey(cases={len(self._entries)}, digest={self.digest})"

    def case_ids(self) -> frozenset[str]:
        return frozenset(self._entries)

    def entry(self, case_id: str) -> ProtectedAnswer:
        try:
            return self._entries[case_id]
        except KeyError:
            raise EveRefusal("replay_not_bound") from None

    def leaks(self, data: object) -> bool:
        """Whether any string inside ``data`` carries a protected answer or its digest.

        Strings are compared as they are and in a case, width and whitespace normalized form, and
        every string leaf is read directly so JSON escaping cannot hide a quote, newline or
        non-ASCII character. The scan is a best-effort floor: it cannot see a paraphrase.
        """
        raw = "\n".join(_strings(data))
        folded = normalize_text(raw)
        for entry in self._entries.values():
            wanted = normalize_text(entry.answer)
            if not wanted:
                continue
            hashes = (
                hashlib.sha256(entry.answer.encode()).hexdigest(),
                hashlib.sha256(wanted.encode()).hexdigest(),
            )
            if entry.answer in raw or wanted in folded or any(h in folded for h in hashes):
                return True
        return False


def _strings(data: object) -> Iterator[str]:
    if isinstance(data, str):
        yield data
    elif isinstance(data, bytes | bytearray):
        yield bytes(data).decode(errors="replace")
    elif isinstance(data, Mapping):
        for k, v in data.items():
            yield from _strings(k)
            yield from _strings(v)
    elif isinstance(data, list | tuple | set | frozenset):
        for item in data:
            yield from _strings(item)
    else:
        yield str(data)


def check_egress(key: AnswerKey, surface: str, data: object) -> None:
    """Refuse an outbound payload that contains a protected answer. The reason names the surface."""
    if key.leaks(data):
        raise EveRefusal(f"protected_answer_exposed_{surface}")


@dataclass(frozen=True)
class ArmSpec:
    arm_id: str
    served_revision: str
    source_snapshot_id: str
    retrieval_settings_id: str
    context_revision: str
    decoding_digest: str

    def __post_init__(self) -> None:
        if self.arm_id not in ARMS or not all(
            isinstance(v, str) and v
            for v in (
                self.served_revision,
                self.source_snapshot_id,
                self.retrieval_settings_id,
                self.context_revision,
                self.decoding_digest,
            )
        ):
            raise EveRefusal("invalid_arm_spec")

    @property
    def digest(self) -> str:
        return digest(
            [
                self.arm_id,
                self.served_revision,
                self.source_snapshot_id,
                self.retrieval_settings_id,
                self.context_revision,
                self.decoding_digest,
            ]
        )


@dataclass(frozen=True)
class ReplayRequest:
    case_id: str
    question: str
    context: tuple[Passage, ...]

    def as_mapping(self) -> Mapping[str, object]:
        return {
            "case_id": self.case_id,
            "question": self.question,
            "context": [[p.passage_id, p.text] for p in self.context],
        }


# Visible-looking blanks outside the Z*/Cc/Cf categories (fillers and the blank braille cell).
_BLANK_LETTERS = frozenset("\u115f\u1160\u3164\uffa0\u2800")


def _carries_content(text: str) -> bool:
    """False for text with nothing a reader could see: empty, whitespace (any Unicode space,
    including non-breaking), control, or zero-width and other format characters only."""
    return any(
        not ch.isspace()
        and ch not in _BLANK_LETTERS
        and unicodedata.category(ch)[0] != "Z"
        and unicodedata.category(ch) not in ("Cc", "Cf")
        for ch in text
    )


_ROW_KEYS = frozenset({"case_id", "answer", "citations", "abstained"})


def _checked_arm_fields(
    case_id: object, answer: object, citations: object, abstained: object
) -> tuple[str, str | None, tuple[str, ...], bool]:
    """The one rule set for an arm output's fields; returns them normalised.

    Citations are materialised exactly once and the materialised tuple is what is validated and
    returned, so an iterable that yields different content on a second pass cannot pass the
    check and then deliver something else. The other fields are plain values read once by the
    caller. Raises ``TypeError`` for a wrong type and ``ValueError`` for an empty ``case_id`` or
    an ``answer`` with no visible content, empty or blank (a genuine abstention is ``None``).
    """
    if not isinstance(case_id, str):
        raise TypeError("ArmOutput.case_id must be a str")
    if not case_id:
        raise ValueError("ArmOutput.case_id must not be empty")
    if answer is not None and not isinstance(answer, str):
        raise TypeError("ArmOutput.answer must be a str or None")
    if answer is not None and not _carries_content(answer):
        raise ValueError("ArmOutput.answer carries no content; use None to abstain")
    if not isinstance(citations, list | tuple):
        raise TypeError("ArmOutput.citations must be a list or tuple of str")
    try:
        cites = tuple(citations)
    except Exception:
        raise TypeError("ArmOutput.citations could not be read as a sequence") from None
    if not all(isinstance(citation, str) for citation in cites):
        raise TypeError("ArmOutput.citations must contain only str")
    if type(abstained) is not bool:
        raise TypeError("ArmOutput.abstained must be a bool")
    return case_id, answer, cites, abstained


@dataclass(frozen=True)
class ArmOutput:
    """One arm's output for one case. Invalid fields cannot be constructed, whichever arm builds it.

    A malformed instance is a programming error in the producing inference, not untrusted input
    at this point, so construction raises ``TypeError`` or ``ValueError`` and a mapping from an
    untrusted connector is translated to ``EveRefusal`` where that connector row is read.
    """

    case_id: str
    answer: str | None
    citations: tuple[str, ...]
    abstained: bool

    def __post_init__(self) -> None:
        case_id, answer, citations, abstained = _checked_arm_fields(
            self.case_id, self.answer, self.citations, self.abstained
        )
        object.__setattr__(self, "case_id", case_id)
        object.__setattr__(self, "answer", answer)
        object.__setattr__(self, "citations", citations)
        object.__setattr__(self, "abstained", abstained)


class GenerativeInference(Protocol):
    @property
    def served_revision(self) -> str: ...

    def generate(
        self, arm: ArmSpec, requests: tuple[ReplayRequest, ...]
    ) -> tuple[ArmOutput, ...]: ...


class Retriever(Protocol):
    def retrieve(self, arm: ArmSpec, case: EveCase) -> tuple[Passage, ...]: ...


class CredentialSource(Protocol):
    def is_available(self, credential_ref: str) -> bool: ...


class Scorer(Protocol):
    """``scorer_id`` names the attributed party, ``version`` its code revision."""

    scorer_id: str
    version: str
    rubric_revision: str

    def score(
        self, case: EveCase, expected: ProtectedAnswer, output: ArmOutput | None
    ) -> Verdict: ...


def scorer_attribution(scorer: object) -> tuple[str, str, str]:
    """``(scorer_id, version, rubric_revision)`` read from the scorer, or a refusal if any is
    missing or blank. Attribution is what the scorer declares; it is checked against the manifest,
    not authenticated (a signed evaluator identity is OPE-566 and OPE-528 work)."""
    values = tuple(
        getattr(scorer, name, None) for name in ("scorer_id", "version", "rubric_revision")
    )
    if not all(isinstance(v, str) and v for v in values):
        raise EveRefusal("scorer_unattributed")
    return values  # type: ignore[return-value]


def scorer_selfcheck(scorer: Scorer) -> bool:
    """Run generated positive and negative controls. A scorer that fails them cannot stage."""
    passage = Passage("generated-control-passage", "generated-control-text")
    case = EveCase("generated-control", "generated-control-family", "generated-q", (passage,))
    answer = ProtectedAnswer(
        Expectation.ANSWER, "generated-control-answer", frozenset({passage.passage_id})
    )
    abstain = ProtectedAnswer(Expectation.ABSTAIN, "", frozenset())
    cite = (passage.passage_id,)
    controls = (
        (answer, ArmOutput("c", "generated-control-answer", cite, False), Verdict.CORRECT_ANSWER),
        (answer, ArmOutput("c", "generated-wrong", cite, False), Verdict.SERIOUS_ERROR),
        (
            answer,
            ArmOutput("c", "generated-control-answer", ("elsewhere",), False),
            Verdict.UNVERIFIED,
        ),
        (answer, ArmOutput("c", None, (), True), Verdict.INAPPROPRIATE_REFUSAL),
        (abstain, ArmOutput("c", None, (), True), Verdict.CORRECT_ABSTENTION),
        (abstain, ArmOutput("c", "generated-guess", cite, False), Verdict.SERIOUS_ERROR),
        (answer, None, Verdict.MISSING),
    )
    try:
        return all(scorer.score(case, exp, out) is want for exp, out, want in controls)
    except (EveRefusal, ValueError, KeyError):
        return False


@dataclass(frozen=True)
class ReplayManifest:
    """Frozen before any arm runs. Every field is compared at run time."""

    benchmark_id: str
    benchmark_revision: str
    registration_identity: str
    scorer_id: str
    scorer_version: str
    rubric_revision: str
    membership_digest: str
    content_digest: str
    answer_key_digest: str
    eligible_case_ids: tuple[str, ...]
    excluded: tuple[tuple[str, str], ...]

    @property
    def digest(self) -> str:
        return digest(
            [
                self.benchmark_id,
                self.benchmark_revision,
                self.registration_identity,
                self.scorer_id,
                self.scorer_version,
                self.rubric_revision,
                self.membership_digest,
                self.content_digest,
                self.answer_key_digest,
                list(self.eligible_case_ids),
                [list(e) for e in self.excluded],
            ]
        )


def replay_membership(cases: tuple[EveCase, ...]) -> str:
    return digest(
        {
            "cases": sorted(c.case_id for c in cases),
            "families": sorted({c.family_id for c in cases}),
        }
    )


def cases_content_digest(cases: tuple[EveCase, ...]) -> str:
    """Bind the untouched question and approved context of every case, not only its id."""
    return digest(
        [
            [c.case_id, c.family_id, c.question, [[p.passage_id, p.text] for p in c.context]]
            for c in sorted(cases, key=lambda c: c.case_id)
        ]
    )


def freeze(
    *,
    benchmark_id: str,
    benchmark_revision: str,
    registration: EveTaskRegistration,
    scorer: Scorer,
    key: AnswerKey,
    cases: tuple[EveCase, ...],
) -> ReplayManifest:
    ids = [c.case_id for c in cases]
    if not cases or len(set(ids)) != len(ids) or key.case_ids() != frozenset(ids):
        raise EveRefusal("replay_not_bound")
    eligible, excluded = [], []
    for case in sorted(cases, key=lambda c: c.case_id):
        expectation = key.entry(case.case_id).expectation
        if expectation in (Expectation.ANSWER, Expectation.ABSTAIN):
            eligible.append(case.case_id)
        else:
            excluded.append((case.case_id, expectation.value))
    scorer_id, scorer_version, rubric_revision = scorer_attribution(scorer)
    return ReplayManifest(
        benchmark_id=benchmark_id,
        benchmark_revision=benchmark_revision,
        registration_identity=registration.identity,
        scorer_id=scorer_id,
        scorer_version=scorer_version,
        rubric_revision=rubric_revision,
        membership_digest=replay_membership(cases),
        content_digest=cases_content_digest(cases),
        answer_key_digest=key.digest,
        eligible_case_ids=tuple(eligible),
        excluded=tuple(excluded),
    )


def verify_bound(
    manifest: ReplayManifest,
    registration: EveTaskRegistration,
    scorer: Scorer,
    cases: tuple[EveCase, ...],
    key: AnswerKey,
) -> None:
    """Refuse on any drift between registered model, scorer, replay set and answer key."""
    if manifest.registration_identity != registration.identity:
        raise EveRefusal("model_version_drift")
    if (scorer.version, scorer.rubric_revision) != (
        manifest.scorer_version,
        manifest.rubric_revision,
    ):
        raise EveRefusal("scorer_version_drift")
    if (
        replay_membership(cases) != manifest.membership_digest
        or cases_content_digest(cases) != manifest.content_digest
    ):
        raise EveRefusal("replay_not_bound")
    if key.digest != manifest.answer_key_digest:
        raise EveRefusal("answer_key_drift")
    if key.case_ids() != frozenset(c.case_id for c in cases):
        raise EveRefusal("replay_not_bound")


COMMITMENT_SCHEME = "hmac-sha256"
_COMMITMENT = re.compile(r"hmac-sha256:[0-9a-f]{64}")
_COMMITMENT_DOMAIN = "opentour.pilot.eve-output-commitment.v1"


@dataclass(frozen=True)
class ScoredRow:
    """One journaled verdict: who scored it, under which rubric, and a commitment to the output.

    ``scorer_id`` is the attributed party and ``scorer_version`` its code revision. The row never
    holds the output. ``commitment`` is an HMAC under the run's key (held in a
    ``CommitmentKeyStore``, never in this row) over the run, this row's other fields and the exact
    output the scorer was handed. A row missing any attribution field, or with no commitment
    present, cannot be constructed. Construction and the journal readers check only that a
    commitment is present and well formed, never that it is correct: only ``verify_output``,
    holding the run key and the output, can check its value.
    """

    ref: str
    verdict: Verdict
    answered: bool
    retrieval: str
    scorer_id: str
    scorer_version: str
    rubric_revision: str
    commitment: str

    def __post_init__(self) -> None:
        attribution = (self.scorer_id, self.scorer_version, self.rubric_revision)
        if not all(isinstance(v, str) and v for v in attribution):
            raise EveRefusal("verdict_unattributed")
        if not isinstance(self.commitment, str) or not _COMMITMENT.fullmatch(self.commitment):
            raise EveRefusal("verdict_uncommitted")
        if (
            not isinstance(self.ref, str)
            or not isinstance(self.verdict, Verdict)
            or type(self.answered) is not bool
            or not isinstance(self.retrieval, str)
        ):
            raise EveRefusal("journal_row_malformed")


@dataclass(frozen=True)
class ScorerRecord:
    """The scorer an arm was replayed with and the self-check result the worker computed for it.

    Recorded by ``replay_arm`` itself, never supplied by a caller. Readers require it to name the
    manifest's scorer and to show a passing self-check, so a scorer handed in later (at staging)
    cannot stand in for the one that produced the verdicts.
    """

    scorer_id: str
    scorer_version: str
    rubric_revision: str
    selfcheck_passed: bool

    def __post_init__(self) -> None:
        attribution = (self.scorer_id, self.scorer_version, self.rubric_revision)
        if not all(isinstance(v, str) and v for v in attribution):
            raise EveRefusal("arm_scorer_unrecorded")
        if type(self.selfcheck_passed) is not bool:
            raise EveRefusal("arm_scorer_unrecorded")

    def document(self) -> list[Any]:
        return [self.scorer_id, self.scorer_version, self.rubric_revision, self.selfcheck_passed]


@dataclass(frozen=True)
class ArmRecord:
    spec: ArmSpec
    rows: tuple[ScoredRow, ...]
    scorer: ScorerRecord | None = None  # None only for a record never journaled; readers refuse it

    def document(self) -> dict[str, Any]:
        return {
            "spec": [
                self.spec.arm_id,
                self.spec.served_revision,
                self.spec.source_snapshot_id,
                self.spec.retrieval_settings_id,
                self.spec.context_revision,
                self.spec.decoding_digest,
            ],
            "rows": [
                [
                    r.ref,
                    r.verdict.value,
                    r.answered,
                    r.retrieval,
                    r.scorer_id,
                    r.scorer_version,
                    r.rubric_revision,
                    r.commitment,
                ]
                for r in self.rows
            ],
            "scorer": self.scorer.document() if self.scorer is not None else None,
        }

    @staticmethod
    def parse(raw: Any) -> ArmRecord:
        """Read a journaled record strictly.

        A row missing scorer id, version or rubric, a row with no commitment present, and an arm
        without its scorer record are refused. The commitment's value is not checked here.
        """
        try:
            spec = ArmSpec(*raw["spec"])
            rows = list(raw["rows"])
            scorer_raw = raw.get("scorer")
        except (KeyError, TypeError, AttributeError):
            raise EveRefusal("journal_row_malformed") from None
        parsed = []
        for r in rows:
            if not isinstance(r, list):
                raise EveRefusal("journal_row_malformed")
            if len(r) in (4, 7):  # earlier shapes: no attribution, or no scorer identity
                raise EveRefusal("verdict_unattributed")
            if len(r) != 8:
                raise EveRefusal("journal_row_malformed")
            ref, verdict, answered, retrieval, scorer_id, scorer_version, rubric_revision, mac = r
            attribution = (scorer_id, scorer_version, rubric_revision)
            if not all(isinstance(v, str) and v for v in attribution):
                raise EveRefusal("verdict_unattributed")
            if not isinstance(mac, str) or not _COMMITMENT.fullmatch(mac):
                raise EveRefusal("verdict_uncommitted")
            try:
                verdict = Verdict(verdict)
            except ValueError:
                raise EveRefusal("journal_row_malformed") from None
            parsed.append(ScoredRow(ref, verdict, answered, retrieval, *attribution, mac))
        if not isinstance(scorer_raw, list) or len(scorer_raw) != 4:
            raise EveRefusal("arm_scorer_unrecorded")
        return ArmRecord(spec, tuple(parsed), ScorerRecord(*scorer_raw))


def _require_attributed(manifest: ReplayManifest, record: ArmRecord) -> None:
    """The arm and every row must name the scorer id, version and rubric the manifest froze, and
    the arm must have been replayed under a passing self-check. Presence is not enough."""
    frozen = (manifest.scorer_id, manifest.scorer_version, manifest.rubric_revision)
    if record.scorer is None:
        raise EveRefusal("arm_scorer_unrecorded")
    recorded = (
        record.scorer.scorer_id,
        record.scorer.scorer_version,
        record.scorer.rubric_revision,
    )
    if recorded != frozen or any(
        (r.scorer_id, r.scorer_version, r.rubric_revision) != frozen for r in record.rows
    ):
        raise EveRefusal("verdict_attribution_mismatch")
    if record.scorer.selfcheck_passed is not True:
        raise EveRefusal("arm_selfcheck_failed")


def _output_message(output: ArmOutput | None) -> list[Any] | None:
    if output is None:
        return None
    return [output.case_id, output.answer, list(output.citations), output.abstained]


def _commit(run_key: bytes, run_id: str, fields: list[Any], output: list[Any] | None) -> str:
    message = canonical([_COMMITMENT_DOMAIN, run_id, fields, output])
    return f"{COMMITMENT_SCHEME}:" + hmac.new(run_key, message, hashlib.sha256).hexdigest()


def _row_fields(row: ScoredRow) -> list[Any]:
    return [
        row.ref,
        row.verdict.value,
        row.answered,
        row.retrieval,
        row.scorer_id,
        row.scorer_version,
        row.rubric_revision,
    ]


def output_commitment(
    run_key: bytes, *, run_id: str, row: ScoredRow, output: ArmOutput | None
) -> str:
    """The commitment ``row`` would carry had ``output`` been the output scored for it.

    The message binds the run (manifest and arm), the row's ref, verdict, answered flag, retrieval
    digest and scorer attribution, and the output's case id, answer, citations and abstention, so
    an output, a verdict or a commitment moved to another row all fail to verify.
    """
    return _commit(run_key, run_id, _row_fields(row), _output_message(output))


class CommitmentKeyStore:
    """Per-run HMAC keys for output commitments, held apart from the journal they protect.

    One 32-byte key is drawn from ``secrets`` per run id on first use and appended to this file
    (mode 0600). The key is never written into the journal. ``EveReplayWorker`` refuses a key store
    anywhere under the journal's directory, one that is the journal file under another name (same
    inode or a symlink), and a key store or journal with a second hard link; the store also refuses
    to append to a file with more than one link. That check is structural only: it keeps the key
    out of the record, not out of the same operator's reach.

    Evidence rung ``locally_keyed``: the same party holds this file and the journal, so a
    commitment is tamper evidence for ourselves, not proof to a third party. Moving this file to
    another disk or host does not change the rung; only separated custody under OPE-566 would.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def __repr__(self) -> str:
        return f"CommitmentKeyStore(path={str(self.path)!r})"

    def _keys(self) -> dict[str, bytes]:
        if not self.path.exists():
            return {}
        keys: dict[str, bytes] = {}
        try:
            for line in self.path.read_text().splitlines():
                if not line:
                    continue
                entry = json.loads(line)
                run_id, key = entry["run_id"], bytes.fromhex(entry["key"])
                if len(key) != 32 or keys.get(run_id, key) != key:
                    raise ValueError("bad key entry")
                keys[run_id] = key
        except (OSError, ValueError, KeyError, TypeError):
            raise EveRefusal("commitment_key_unavailable") from None
        return keys

    def key_for(self, run_id: str) -> bytes:
        """The run's key, created on first use."""
        existing = self._keys().get(run_id)
        if existing is not None:
            return existing
        key = secrets.token_bytes(32)
        if _hard_linked(self.path):
            raise EveRefusal("commitment_key_colocated")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(canonical({"run_id": run_id, "key": key.hex()}) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        return key

    def require(self, run_id: str) -> bytes:
        """The run's key, or a refusal: a commitment cannot be checked without it."""
        key = self._keys().get(run_id)
        if key is None:
            raise EveRefusal("commitment_key_unavailable")
        return key


def _hard_linked(path: Path) -> bool:
    try:
        return path.exists() and path.stat().st_nlink > 1
    except OSError:
        return True


def _keys_colocated(journal: Path, keys: Path) -> bool:
    """Whether the key store could end up in, beside or as the journal it protects."""
    journal_path, keys_path = journal.resolve(), keys.resolve()
    if keys_path == journal_path or keys_path.is_relative_to(journal_path.parent):
        return True
    if _hard_linked(journal) or _hard_linked(keys):
        return True
    try:
        return journal.exists() and keys.exists() and os.path.samefile(journal, keys)
    except OSError:
        return True


_EVENT_FIELDS = {"arm": ("run_id", "manifest", "record"), "settle": ("manifest", "result")}


class Settlement(StrEnum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate_ignored"


class ReplayJournal:
    """Append-only file journal: arm records by run key and one final scorecard per manifest."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def _events(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            events = [json.loads(line) for line in self.path.read_text().splitlines() if line]
        except (OSError, ValueError):
            raise EveRefusal("replay_journal_unavailable") from None
        for event in events:  # a foreign line is a clean refusal, never a raw KeyError later
            kind = event.get("event") if isinstance(event, dict) else None
            fields = _EVENT_FIELDS.get(kind) if isinstance(kind, str) else None
            if fields is None or any(f not in event for f in fields):
                raise EveRefusal("replay_journal_unavailable")
        return events

    def _append(self, event: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "ab") as handle:
            handle.write(canonical(event) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())

    def arm(self, run_id: str) -> ArmRecord | None:
        for event in self._events():
            if event["event"] == "arm" and event["run_id"] == run_id:
                return ArmRecord.parse(event["record"])
        return None

    def put_arm(self, run_id: str, manifest_digest: str, record: ArmRecord) -> ArmRecord:
        existing = self.arm(run_id)
        if existing is not None:
            if existing != record:
                raise EveRefusal("replay_result_conflict")
            return existing
        self._append(
            {
                "event": "arm",
                "run_id": run_id,
                "manifest": manifest_digest,
                "record": record.document(),
            }
        )
        return record

    def arms_for(self, manifest_digest: str) -> tuple[ArmRecord, ...]:
        """Every arm record journaled under ``manifest_digest``, in journal order."""
        return tuple(
            ArmRecord.parse(e["record"])
            for e in self._events()
            if e["event"] == "arm" and e["manifest"] == manifest_digest
        )

    def known_manifest(self, manifest_digest: str) -> bool:
        return any(e.get("manifest") == manifest_digest for e in self._events())

    def settle(self, manifest_digest: str, result_digest: str) -> Settlement:
        """First scorecard wins. A late or duplicate callback cannot change it."""
        if not self.known_manifest(manifest_digest):
            raise EveRefusal("stale_scoring_callback")
        for event in self._events():
            if event["event"] == "settle" and event["manifest"] == manifest_digest:
                if event["result"] == result_digest:
                    return Settlement.DUPLICATE
                raise EveRefusal("scoring_already_final")
        self._append({"event": "settle", "manifest": manifest_digest, "result": result_digest})
        return Settlement.RECORDED


class CandidateInference:
    """Replay a collected candidate after re-verifying its lineage with the connector.

    A caller-built job and candidate that agree with each other are not enough: the connector must
    still report the same recorded job and hand back the same candidate through ``Preflight``.
    """

    def __init__(
        self,
        preflight: mc.Preflight,
        actor: str,
        job: mc.JobReceipt,
        candidate: mc.Candidate,
        registration: EveTaskRegistration,
    ) -> None:
        try:
            mc.verify_candidate(job, candidate)
        except mc.CapabilityError:
            raise EveRefusal("candidate_lineage_unverified") from None
        binding = job.intent.binding
        if (
            candidate.parent_revision != registration.base_revision
            or binding.immutable_revision != registration.base_revision
            or binding.preprocessing_revision != registration.fingerprint
            or binding.model_ref != registration.model_ref
            or binding.task != registration.task
        ):
            raise EveRefusal("candidate_lineage_unverified")
        try:
            recorded = preflight.collect_candidate(actor, job)
        except mc.CapabilityError:
            raise EveRefusal("candidate_lineage_unverified") from None
        if recorded != candidate:
            raise EveRefusal("candidate_lineage_unverified")
        self.connector = preflight.connector
        self.candidate = candidate
        self.benchmark_revision = job.intent.benchmark_revision

    @property
    def served_revision(self) -> str:
        return self.candidate.artifact_or_serving_revision

    def generate(self, arm: ArmSpec, requests: tuple[ReplayRequest, ...]) -> tuple[ArmOutput, ...]:
        try:
            raw = self.connector.predict_for_replay(
                self.candidate, tuple(r.as_mapping() for r in requests)
            )
        except OSError:
            raise EveRefusal("inference_unavailable") from None
        except (ValueError, LookupError):
            raise EveRefusal("candidate_lineage_unverified") from None
        if not isinstance(raw, tuple):
            raise EveRefusal("output_shape_invalid")
        outputs = []
        for item in raw:
            if not isinstance(item, Mapping):
                raise EveRefusal("output_shape_invalid")
            if set(item) != _ROW_KEYS:
                raise EveRefusal("output_shape_invalid")
            try:
                case_id, answer = item["case_id"], item["answer"]
                citations, abstained = item["citations"], item["abstained"]
            except KeyError:
                raise EveRefusal("output_shape_invalid") from None
            try:
                fields = _checked_arm_fields(case_id, answer, citations, abstained)
                outputs.append(ArmOutput(*fields))
            except (TypeError, ValueError):
                raise EveRefusal("output_shape_invalid") from None
        return tuple(outputs)


def run_id_for(manifest: ReplayManifest, arm: ArmSpec) -> str:
    return digest([manifest.digest, arm.digest])


def _ref(manifest: ReplayManifest, case_id: str) -> str:
    return digest([manifest.digest, case_id])[7:23]


def verify_output(
    *,
    keys: CommitmentKeyStore,
    journal: ReplayJournal,
    manifest: ReplayManifest,
    arm: ArmSpec,
    case_id: str,
    presented: ArmOutput | None,
) -> bool:
    """Whether ``presented`` is exactly the output scored for ``case_id`` in this journaled arm.

    The row is read from the journal, never taken from the caller, its attribution is checked
    against the manifest, and the commitment is recomputed under the run key and compared in
    constant time. False means a substituted output, or a verdict, attribution or commitment
    edited in the journal since. It says nothing about whether the verdict was right.
    """
    run_id = run_id_for(manifest, arm)
    record = journal.arm(run_id)
    if record is None:
        raise EveRefusal("arm_not_journaled")
    _require_attributed(manifest, record)
    ref = _ref(manifest, case_id)
    rows = [r for r in record.rows if r.ref == ref]
    if len(rows) != 1:
        raise EveRefusal("case_not_scored")
    expected = output_commitment(keys.require(run_id), run_id=run_id, row=rows[0], output=presented)
    return hmac.compare_digest(expected, rows[0].commitment)


class EveReplayWorker:
    """Replay one arm at a time against the frozen protected set and journal exactly one result."""

    def __init__(
        self,
        *,
        registry: EveRegistry,
        preflight: mc.Preflight,
        credentials: CredentialSource,
        journal: ReplayJournal,
        keys: CommitmentKeyStore,
        log: Callable[[str], None] = lambda _line: None,
    ) -> None:
        if _keys_colocated(journal.path, keys.path):
            raise EveRefusal("commitment_key_colocated")
        self.registry = registry
        self.preflight = preflight
        self.credentials = credentials
        self.journal = journal
        self.keys = keys
        self._log = log

    def _emit(self, key: AnswerKey, line: str) -> None:
        check_egress(key, "log", line)
        self._log(line)

    def replay_arm(
        self,
        *,
        actor: str,
        binding: mc.ModelBinding,
        manifest: ReplayManifest,
        arm: ArmSpec,
        cases: tuple[EveCase, ...],
        scorer: Scorer,
        key: AnswerKey,
        inference: GenerativeInference,
        retriever: Retriever | None = None,
    ) -> ArmRecord:
        registration = self.registry.require(binding.model_ref)
        if Capability.GENERATE not in registration.capabilities:
            raise EveRefusal("model_not_qualified")
        expected = binding_for(
            registration,
            tenant=binding.tenant,
            route=binding.route,
            region=binding.region,
            credential_ref=binding.credential_ref,
        )
        if binding != expected:
            raise EveRefusal("registration_binding_mismatch")
        if not self.credentials.is_available(binding.credential_ref):
            raise EveRefusal("credential_unavailable")
        try:
            capability = self.preflight.discover(actor, binding, mc.Purpose.BASELINE)
        except mc.CapabilityError:
            raise EveRefusal("model_not_qualified") from None
        if capability.state not in (mc.State.BENCHMARK_ONLY, mc.State.TRAINABLE):
            raise EveRefusal("model_not_qualified")
        if not capability.exact_model_attribution:
            raise EveRefusal("exact_model_revision_unverified")
        verify_bound(manifest, registration, scorer, cases, key)
        # A scorer that fails its controls never produces a journaled verdict (OPE-573 F1).
        if not scorer_selfcheck(scorer):
            raise EveRefusal("scorer_selfcheck_failed")
        attribution = scorer_attribution(scorer)
        if attribution != (manifest.scorer_id, manifest.scorer_version, manifest.rubric_revision):
            raise EveRefusal("scorer_identity_drift")
        if arm.decoding_digest != registration.decoding.digest:
            raise EveRefusal("decoding_drift")
        if inference.served_revision != arm.served_revision:
            raise EveRefusal("served_revision_mismatch")
        if arm.arm_id != "candidate" and arm.served_revision != registration.base_revision:
            raise EveRefusal("served_revision_mismatch")
        if arm.arm_id == "candidate":
            if not isinstance(inference, CandidateInference):
                raise EveRefusal("candidate_lineage_unverified")
            if inference.benchmark_revision != manifest.benchmark_revision:
                raise EveRefusal("candidate_benchmark_mismatch")
            if arm.served_revision == registration.base_revision:
                raise EveRefusal("served_revision_mismatch")

        run_id = run_id_for(manifest, arm)
        done = self.journal.arm(run_id)
        if done is not None:
            _require_attributed(manifest, done)
            self._emit(key, f"arm_replay_reused arm={arm.arm_id} run={run_id[7:23]}")
            return done
        run_key = self.keys.key_for(run_id)

        by_id = {c.case_id: c for c in cases}
        requests, retrieval = [], {}
        for case_id in manifest.eligible_case_ids:
            case = by_id[case_id]
            passages = retriever.retrieve(arm, case) if retriever is not None else case.context
            check_egress(key, "retrieval", [[p.passage_id, p.text] for p in passages])
            retrieval[case_id] = digest(sorted(p.passage_id for p in passages))
            requests.append(ReplayRequest(case_id, case.question, tuple(passages)))
        check_egress(key, "request", [r.as_mapping() for r in requests])

        try:
            outputs = inference.generate(arm, tuple(requests))
        except OSError:
            raise EveRefusal("inference_unavailable") from None
        requested = {r.case_id for r in requests}
        got = [o.case_id for o in outputs]
        if len(set(got)) != len(got) or not set(got) <= requested:
            raise EveRefusal("output_case_mismatch")
        output_by_id = {o.case_id: o for o in outputs}
        request_by_id = {r.case_id: r for r in requests}
        rows = []
        for case_id in manifest.eligible_case_ids:
            output = output_by_id.get(case_id)
            # Captured before scoring: the commitment is over exactly what the scorer was handed.
            committed_output = _output_message(output)
            case = EveCase(case_id, by_id[case_id].family_id, "", request_by_id[case_id].context)
            verdict = Verdict(scorer.score(case, key.entry(case_id), output))
            ref = _ref(manifest, case_id)
            answered = output is not None and not output.abstained and output.answer is not None
            fields = [ref, verdict.value, answered, retrieval[case_id], *attribution]
            rows.append(
                ScoredRow(
                    ref,
                    verdict,
                    answered,
                    retrieval[case_id],
                    *attribution,
                    _commit(run_key, run_id, fields, committed_output),
                )
            )
        record = ArmRecord(arm, tuple(rows), ScorerRecord(*attribution, True))
        check_egress(key, "journal", record.document())
        stored = self.journal.put_arm(run_id, manifest.digest, record)
        self._emit(key, f"arm_replayed arm={arm.arm_id} run={run_id[7:23]} cases={len(rows)}")
        return stored


def _pair(num: int, den: int) -> list[int | None]:
    return [num, den if den else None]


def _arm_body(manifest: ReplayManifest, record: ArmRecord) -> dict[str, Any]:
    count = {v: sum(1 for r in record.rows if r.verdict is v) for v in Verdict}
    eligible = len(manifest.eligible_case_ids)
    answered = sum(1 for r in record.rows if r.answered)
    serious = count[Verdict.SERIOUS_ERROR]
    verified_answered = count[Verdict.CORRECT_ANSWER] + serious
    correct = count[Verdict.CORRECT_ANSWER] + count[Verdict.CORRECT_ABSTENTION]
    return {
        "spec_digest": record.spec.digest,
        "served_revision": record.spec.served_revision,
        "eligible": eligible,
        "excluded_unscored": len(manifest.excluded),
        "missing_output": count[Verdict.MISSING],
        "answered": answered,
        "verified_answered": verified_answered,
        "unverified_answered": count[Verdict.UNVERIFIED],
        "correct_answer": count[Verdict.CORRECT_ANSWER],
        "correct_abstention": count[Verdict.CORRECT_ABSTENTION],
        "inappropriate_refusal": count[Verdict.INAPPROPRIATE_REFUSAL],
        "serious_errors": serious,
        "correct": correct,
        "coverage_answered_over_eligible": _pair(answered, eligible),
        "serious_error_over_verified_answered": _pair(serious, verified_answered),
        "correct_over_eligible": _pair(correct, eligible),
    }


def _missing(rows: Mapping[str, ScoredRow]) -> set[str]:
    return {ref for ref, row in rows.items() if row.verdict is Verdict.MISSING}


def _paired(control: ArmRecord, treatment: ArmRecord) -> dict[str, Any] | None:
    control_rows = {r.ref: r for r in control.rows}
    treatment_rows = {r.ref: r for r in treatment.rows}
    if control_rows.keys() != treatment_rows.keys() or _missing(control_rows) != _missing(
        treatment_rows
    ):
        return None
    gradeable = [r for r in control_rows if r not in _missing(control_rows)]
    fixes = sum(
        1
        for r in gradeable
        if control_rows[r].verdict not in _CORRECT and treatment_rows[r].verdict in _CORRECT
    )
    regressions = sum(
        1
        for r in gradeable
        if control_rows[r].verdict in _CORRECT and treatment_rows[r].verdict not in _CORRECT
    )
    both_correct = sum(
        1
        for r in gradeable
        if control_rows[r].verdict in _CORRECT and treatment_rows[r].verdict in _CORRECT
    )
    return {
        "control": control.spec.arm_id,
        "treatment": treatment.spec.arm_id,
        "paired_gradeable": len(gradeable),
        "fixes": fixes,
        "regressions": regressions,
        "both_correct": both_correct,
        "both_wrong": len(gradeable) - fixes - regressions - both_correct,
    }


def _causal(control: ArmRecord, treatment: ArmRecord) -> dict[str, Any]:
    """Disclose which settings changed besides the served model, and how many passages moved."""
    fields = ("source_snapshot_id", "retrieval_settings_id", "context_revision", "decoding_digest")
    changed = [f for f in fields if getattr(control.spec, f) != getattr(treatment.spec, f)]
    left = {r.ref: r.retrieval for r in control.rows}
    right = {r.ref: r.retrieval for r in treatment.rows}
    moved = sum(1 for ref in left if right.get(ref) != left[ref])
    return {
        "available": not changed and moved == 0,
        "differing_settings": changed,
        "retrieval_changed_cases": moved,
    }


def build_result(
    manifest: ReplayManifest,
    arms: Mapping[str, ArmRecord],
    *,
    scorer: Scorer,
    journal: ReplayJournal,
    key: AnswerKey,
) -> dict[str, Any]:
    """Versioned result. Counts only; no per-case answer, question text or commitment.

    The scorer self-check is run here, never accepted from the caller, and every arm must be the
    record the replay worker journaled for this manifest, so a hand-built arm cannot be scored.
    Every arm and verdict row must be attributed to the scorer id, version and rubric the manifest
    froze, and every arm must record that its replay ran under a passing self-check; the self-check
    of the ``scorer`` passed here is an additional gate and never stands in for that record. Its
    aggregation can be re-derived from the journal by ``eve_aggregate``; nothing here re-scores a
    verdict, because the worker journal holds no candidate output.
    """
    if not arms or any(k != r.spec.arm_id or k not in ARMS for k, r in arms.items()):
        raise EveRefusal("invalid_arm_spec")
    if (scorer.version, scorer.rubric_revision) != (
        manifest.scorer_version,
        manifest.rubric_revision,
    ):
        raise EveRefusal("scorer_version_drift")
    for record in arms.values():
        journaled = journal.arm(run_id_for(manifest, record.spec))
        if journaled is not None:
            _require_attributed(manifest, journaled)
        if journaled != record:
            raise EveRefusal("arm_not_journaled")
    replay_selfcheck_passed = all(
        r.scorer is not None and r.scorer.selfcheck_passed is True for r in arms.values()
    )
    # The staging scorer must be the scorer the manifest names, as replay requires. A declared
    # other identity is refused; an undeclared one cannot pass the staging gate.
    try:
        staging_attribution: tuple[str, str, str] | None = scorer_attribution(scorer)
    except EveRefusal:
        staging_attribution = None
    frozen = (manifest.scorer_id, manifest.scorer_version, manifest.rubric_revision)
    if staging_attribution is not None and staging_attribution != frozen:
        raise EveRefusal("scorer_identity_drift")
    staging_selfcheck_passed = staging_attribution is not None and scorer_selfcheck(scorer)
    treatment = arms.get("candidate") or arms.get("simple_repair")
    control = arms.get("simple_repair") if "candidate" in arms else arms.get("baseline")
    if "candidate" in arms and control is None:
        control = arms.get("baseline")
    paired = causal = None
    if treatment is None or control is None:
        claim = "none_baseline_only" if set(arms) == {"baseline"} else "hold_missing_arm"
    else:
        paired = _paired(control, treatment)
        causal = _causal(control, treatment)
        if paired is None:
            claim = "blocked_changed_denominator"
        else:
            claim = (
                "observed_net_fixes" if paired["fixes"] > paired["regressions"] else "no_net_fixes"
            )
    body = {
        "schema_version": VERSION,
        "benchmark_id": manifest.benchmark_id,
        "benchmark_revision": manifest.benchmark_revision,
        "manifest_digest": manifest.digest,
        "registration_identity": manifest.registration_identity,
        "scorer": {
            "scorer_id": manifest.scorer_id,
            "version": manifest.scorer_version,
            "rubric_revision": manifest.rubric_revision,
            "replay_selfcheck_passed": replay_selfcheck_passed,
            "staging_selfcheck_passed": staging_selfcheck_passed,
            "selfcheck_passed": replay_selfcheck_passed and staging_selfcheck_passed,
        },
        "evidence_tier": TIER,
        "claim_scope": "implementation_only",
        "arms": {name: _arm_body(manifest, arms[name]) for name in ARMS if name in arms},
        "paired_comparison": paired,
        "causal_tuning_comparison": causal,
        "improvement_claim": claim,
        "case_outcomes_redacted": {
            name: [[r.ref, r.verdict.value] for r in arms[name].rows]
            for name in ARMS
            if name in arms
        },
        "excluded_redacted": [[_ref(manifest, c), reason] for c, reason in manifest.excluded],
        "unsatisfied_external_gates": list(EXTERNAL_GATES),
        "fit_authorized": False,
        "release_authorized": False,
    }
    check_egress(key, "result", body)
    return {"body": body, "result_digest": digest(body)}


@dataclass(frozen=True)
class StageDecision:
    stageable: bool
    reasons: tuple[str, ...]


def _rebuild(
    manifest: ReplayManifest, journal: ReplayJournal, scorer: Scorer, key: AnswerKey
) -> dict[str, Any]:
    """Rebuild the result from every arm the journal holds for ``manifest``, one per arm id."""
    arms: dict[str, ArmRecord] = {}
    for record in journal.arms_for(manifest.digest):
        if record.spec.arm_id in arms:
            raise EveRefusal("arm_ambiguous")
        arms[record.spec.arm_id] = record
    if not arms:
        raise EveRefusal("arm_not_journaled")
    return build_result(manifest, arms, scorer=scorer, journal=journal, key=key)


def stage_decision(
    result: Mapping[str, Any],
    *,
    recomputed_digest: str,
    manifest: ReplayManifest,
    journal: ReplayJournal,
    scorer: Scorer,
    key: AnswerKey,
) -> StageDecision:
    """Whether a result may be offered for Slack review. It is never a release authorization.

    The result is derived here, never taken from the caller: it is rebuilt from every arm the
    journal holds for ``manifest`` (re-running the scorer self-check and the attribution checks),
    and the supplied result must equal that rebuild exactly. ``recomputed_digest`` is the digest
    the caller expects, for example the one a reviewer was shown; it is compared with the derived
    digest and refused on mismatch, and never stands in for the derivation. The self-check and
    improvement claim are read from the rebuilt body, so a flag edited into a re-digested body
    cannot make a result stageable.

    Limits: the rebuild reads verdicts back from the journal, so it re-derives aggregation and
    checks body and journal consistency, and cannot check any verdict. It shares ``build_result``'s
    aggregation code, so an aggregation bug is caught only by the separate ``eve_aggregate``. Every
    contributing arm must record a passing replay-time self-check under the manifest's scorer
    identity; the ``scorer`` passed here must carry that same identity (another declared identity
    is refused, an undeclared one is not stageable) and is self-checked as well, but a passing
    scorer handed in at staging cannot stand in for a failing one at replay. Scorer identity is
    declared and checked against the manifest, not authenticated. Two journaled runs of one arm id
    are ambiguous and not stageable.
    """
    try:
        rebuilt = _rebuild(manifest, journal, scorer, key)
    except EveRefusal as refusal:
        return StageDecision(False, (f"rebuild_refused_{refusal}",))
    try:
        scorer_attribution(scorer)
        unattributed: tuple[str, ...] = ()
    except EveRefusal:
        unattributed = ("staging_scorer_unattributed",)
    body, derived = rebuilt["body"], rebuilt["result_digest"]
    try:
        supplied_body, supplied_digest = result["body"], result["result_digest"]
        reasons = []
        selfchecks = (
            body["scorer"]["selfcheck_passed"],
            supplied_body["scorer"]["selfcheck_passed"],
        )
        if not all(selfchecks):
            reasons.append("scorer_selfcheck_failed")
        if (
            supplied_digest != derived
            or recomputed_digest != derived
            or digest(supplied_body) != derived
        ):
            reasons.append("recompute_mismatch")
        if (
            supplied_body["schema_version"] != VERSION
            or supplied_body["evidence_tier"] != TIER
            or supplied_body["claim_scope"] != "implementation_only"
        ):
            reasons.append("result_not_generated_implementation_only")
        if (
            supplied_body["fit_authorized"] is not False
            or supplied_body["release_authorized"] is not False
        ):
            reasons.append("authorization_flag_set")
    except (KeyError, TypeError):
        return StageDecision(False, ("result_malformed",))
    if body["improvement_claim"] != "observed_net_fixes":
        reasons.append(f"improvement_{body['improvement_claim']}")
    reasons.extend(unattributed)
    return StageDecision(not reasons, tuple(reasons))


def guard_fit_payload(key: AnswerKey, plan: FitPlan) -> None:
    """Refuse a fit plan whose rows or intent carry protected answer text."""
    check_egress(key, "fit_payload", [dict(r) for r in plan.rows])
    check_egress(key, "fit_payload", [plan.intent.development_digest, sorted(plan.intent.fields)])


def plan_guarded_fit(
    *,
    key: AnswerKey,
    manifest: ReplayManifest,
    protected_cases: tuple[EveCase, ...],
    **plan_arguments: Any,
) -> FitPlan:
    """The sanctioned way to plan a fit: bound to the frozen replay set and scanned for answers.

    The protected cases must be exactly the ones the manifest froze, the partition must declare
    that same protected membership, no development question may repeat a protected question under
    a new id, and no protected answer may appear in a row.
    """
    if (
        replay_membership(protected_cases) != manifest.membership_digest
        or cases_content_digest(protected_cases) != manifest.content_digest
    ):
        raise EveRefusal("replay_not_bound")
    plan = plan_fit(bound_membership_digest=manifest.membership_digest, **plan_arguments)
    protected_questions = {normalize_text(c.question) for c in protected_cases}
    if any(normalize_text(str(row["question"])) in protected_questions for row in plan.rows):
        raise EveRefusal("protected_overlap")
    guard_fit_payload(key, plan)
    return plan
