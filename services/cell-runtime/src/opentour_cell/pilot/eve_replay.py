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

Simulated: tests use generated sentinel cases and answers, a deterministic scorer, a deterministic
inference double and an isolated trainer-transport double. Not proven: no real EVE inference, GPU,
trainer, provider or Pi School reviewer exists here, the scorer is an exact-match stand-in and not
the independent human or model evaluator, and no statistical interval is computed. Results carry
the ``generated`` evidence tier, claim scope ``implementation_only`` and list their unsatisfied
external gates. They are software behavior only and never provider or customer evidence.
"""

from __future__ import annotations

import hashlib
import json
import os
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


def _checked_arm_fields(
    case_id: object, answer: object, citations: object, abstained: object
) -> tuple[str, str | None, tuple[str, ...], bool]:
    """The one rule set for an arm output's fields; returns them normalised.

    Citations are materialised exactly once and the materialised tuple is what is validated and
    returned, so an iterable that yields different content on a second pass cannot pass the
    check and then deliver something else. The other fields are plain values read once by the
    caller. Raises ``TypeError`` for a wrong type and ``ValueError`` for an empty ``case_id``.
    """
    if not isinstance(case_id, str):
        raise TypeError("ArmOutput.case_id must be a str")
    if not case_id:
        raise ValueError("ArmOutput.case_id must not be empty")
    if answer is not None and not isinstance(answer, str):
        raise TypeError("ArmOutput.answer must be a str or None")
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
    version: str
    rubric_revision: str

    def score(
        self, case: EveCase, expected: ProtectedAnswer, output: ArmOutput | None
    ) -> Verdict: ...


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
    return ReplayManifest(
        benchmark_id,
        benchmark_revision,
        registration.identity,
        scorer.version,
        scorer.rubric_revision,
        replay_membership(cases),
        cases_content_digest(cases),
        key.digest,
        tuple(eligible),
        tuple(excluded),
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


@dataclass(frozen=True)
class ScoredRow:
    ref: str
    verdict: Verdict
    answered: bool
    retrieval: str


@dataclass(frozen=True)
class ArmRecord:
    spec: ArmSpec
    rows: tuple[ScoredRow, ...]

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
            "rows": [[r.ref, r.verdict.value, r.answered, r.retrieval] for r in self.rows],
        }

    @staticmethod
    def parse(raw: Any) -> ArmRecord:
        return ArmRecord(
            ArmSpec(*raw["spec"]),
            tuple(ScoredRow(r[0], Verdict(r[1]), bool(r[2]), r[3]) for r in raw["rows"]),
        )


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
            return [json.loads(line) for line in self.path.read_text().splitlines() if line]
        except (OSError, ValueError):
            raise EveRefusal("replay_journal_unavailable") from None

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


class EveReplayWorker:
    """Replay one arm at a time against the frozen protected set and journal exactly one result."""

    def __init__(
        self,
        *,
        registry: EveRegistry,
        preflight: mc.Preflight,
        credentials: CredentialSource,
        journal: ReplayJournal,
        log: Callable[[str], None] = lambda _line: None,
    ) -> None:
        self.registry = registry
        self.preflight = preflight
        self.credentials = credentials
        self.journal = journal
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
            self._emit(key, f"arm_replay_reused arm={arm.arm_id} run={run_id[7:23]}")
            return done

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
            case = EveCase(case_id, by_id[case_id].family_id, "", request_by_id[case_id].context)
            verdict = scorer.score(case, key.entry(case_id), output)
            rows.append(
                ScoredRow(
                    _ref(manifest, case_id),
                    verdict,
                    output is not None and not output.abstained and output.answer is not None,
                    retrieval[case_id],
                )
            )
        record = ArmRecord(arm, tuple(rows))
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
    """Recomputable versioned result. Counts only; no per-case answer or question text.

    The scorer self-check is run here, never accepted from the caller, and every arm must be the
    record the replay worker journaled for this manifest, so a hand-built arm cannot be scored.
    """
    if not arms or any(k != r.spec.arm_id or k not in ARMS for k, r in arms.items()):
        raise EveRefusal("invalid_arm_spec")
    if (scorer.version, scorer.rubric_revision) != (
        manifest.scorer_version,
        manifest.rubric_revision,
    ):
        raise EveRefusal("scorer_version_drift")
    for record in arms.values():
        if journal.arm(run_id_for(manifest, record.spec)) != record:
            raise EveRefusal("arm_not_journaled")
    scorer_selfcheck_passed = scorer_selfcheck(scorer)
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
            "version": manifest.scorer_version,
            "rubric_revision": manifest.rubric_revision,
            "selfcheck_passed": scorer_selfcheck_passed,
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


def stage_decision(result: Mapping[str, Any], *, recomputed_digest: str) -> StageDecision:
    """Whether a result may be offered for Slack review. It is never a release authorization.

    ``recomputed_digest`` must come from rebuilding the result with a fresh journal and scorer;
    a digest alone proves only that the body is self-consistent, so the body's tier, scope and
    authorization flags are checked as well.
    """
    try:
        body = result["body"]
        reasons = []
        if not body["scorer"]["selfcheck_passed"]:
            reasons.append("scorer_selfcheck_failed")
        if result["result_digest"] != digest(body) or recomputed_digest != result["result_digest"]:
            reasons.append("recompute_mismatch")
        if (
            body["schema_version"] != VERSION
            or body["evidence_tier"] != TIER
            or body["claim_scope"] != "implementation_only"
        ):
            reasons.append("result_not_generated_implementation_only")
        if body["fit_authorized"] is not False or body["release_authorized"] is not False:
            reasons.append("authorization_flag_set")
        if body["improvement_claim"] != "observed_net_fixes":
            reasons.append(f"improvement_{body['improvement_claim']}")
    except (KeyError, TypeError):
        return StageDecision(False, ("result_malformed",))
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
