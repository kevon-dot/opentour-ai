"""Read-only questions about the evidence available for one selected frozen revision.

The second half of this module holds typed, receipt-backed status projections for the five
status families: speed, training readiness, model trace, delivery and weekly live evidence.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from fractions import Fraction
from statistics import median
from typing import Any, Protocol


class StatusTopic(StrEnum):
    TIMING = "timing"
    TRAINING = "training"
    TRACE = "trace"
    DELIVERY = "delivery"
    LIVE_ERRORS = "live_errors"


# Resolve a single read-only clause using its grammatical form and subject. An
# unrecognized scope, actor, modifier or second intent never gets silently discarded.
_QUESTION_START = set(
    "what which why when where who how is was were are am did does do has have had any no "
    "can could would may tell show".split()
)
_COMMON = (
    set(
        "i me my we us our you your it its this that these those they them their there "
        "the a an and or to at of as in on from by with for about into out up than "
        "between during over under since until yet now actually already really ever just "
        "still again then much more less any some anything something anyone someone all "
        "each one many before after new old current previous last first same latest "
        "selected inspected frozen original other compared versus against comparison "
        "compare change changed difference result results run runs job jobs candidate "
        "benchmark workflow please tell show know find see whether big small specifically "
        "measured happened happen got get getting been being be having had has have not "
        "no should would could can will did does do is are am was were only even also "
        "exactly here take taken took long actual observed recorded version status".split()
    )
    | _QUESTION_START
)

_VOCAB = {
    StatusTopic.TIMING: set(
        "timing timings time times faster fast speed gain advantage speeds speedup "
        "speedups quicker quick quickly slowly slower slow accelerate accelerated "
        "acceleration save saved saving savings improvement improved gains performance "
        "duration elapsed latency wall clock baseline original prior previous incumbent "
        "candidate old new model models fit fits training train retraining tuning "
        "evaluation end preparation replay complete finish finished completed process "
        "processing runtime reduce reduced reduction shorten shorter longer length "
        "seconds minutes hours milliseconds measured measurement measurements difference "
        "delta relative human versus vs than better".split()
    ),
    StatusTopic.TRAINING: set(
        "fit fits fitted fitting train trains trained trainer training retrain retrained "
        "retraining tune tuned tuning finetune finetuning fine model models learning "
        "learn learned new start started starting happen happened happening kick kicked "
        "off launch launched run skipped skip prevented prevent held holding hold back "
        "kept keeps keep stopping stopped caused cause causes unable "
        "blocked blocking blocker blockers stopped stopping stop missing needed needs "
        "need reason reasons due eligible eligibility qualified qualify gating "
        "qualification ready readiness worth worthwhile enough too early authorized "
        "authorization permitted allowed enabled disabled refused rejected deferred "
        "delayed proceed trigger triggered go ahead pass considered performed completed "
        "occur occurred attempted attempt decide decided decision stay stayed leave left "
        "untrained instead updated update weight weights evidence data gate gates allow".split()
    ),
    StatusTopic.TRACE: set(
        "model models assistant predictor prediction predictions predicted predict "
        "classifier decision decisions deciding response responses respond responded "
        "reply replied raw returned answer answers answered output outputs return emit "
        "emitted generate generated produce produced say said send sent wrote write input "
        "inputs prompt prompts request requests text message messages fields values "
        "context data information receive received given give saw see read shown fed feed "
        "went came go come back look looked first classify classified choose chosen "
        "picked suggested original exact time capture captured trace inference call calls "
        "payload boundary system presented available recommend recommended before "
        "correction later".split()
    ),
    StatusTopic.DELIVERY: set(
        "ship ships shipped shipping send sends sent sending deliver delivered delivery "
        "receiver reach reached receive received receiving recipient recipients "
        "destination downstream arrive arrived arrival land landed handoff handoffs hand "
        "handed handedoff off over release released package packet artifact file handover "
        "handovers go gone went make made through ack acknowledged acknowledgement "
        "confirmed confirm confirmation independent readback dispatched dispatch get copy "
        "customer their requested intended external evaluation transfer transferred "
        "published actually successfully successful otherside side complete completed".split()
    ),
    StatusTopic.LIVE_ERRORS: set(
        "week weekly weeks weeklong seven 7 days day past so far live feed production "
        "verified confirmed observed recorded decisions outcomes recent actual business "
        "since monday calendar across errors error mistakes mistake misses miss failures "
        "failure incorrect wrong wrongly misclassified misclassifications bad problems "
        "issues incidents clean quiet free false alarms zero none count counts number see "
        "seen saw detect detected spot spotted find found make made occur occurred look "
        "looking clear go gone without everything right anything during really yet "
        "flagged happen happened".split()
    ),
}


def resolve_status(prompt: str) -> StatusTopic | None:
    text = prompt.casefold().replace("’", "'").strip().removesuffix("?").strip()
    if len(text) > 200 or not re.fullmatch(r"[a-z0-9', /-]+", text):
        return None
    text = re.sub(r"n['’]t\b", " not", text)
    text = re.sub(r"\b(what|where|how|who|it|that)['’]s\b", r"\1 is", text)
    text = re.sub(r"\b(we|they|you)['’]ve\b", r"\1 have", text)
    tokens = re.findall(r"[a-z0-9]+", text)
    if tokens[:1] == ["please"]:
        tokens = tokens[1:]
    if not tokens or len(tokens) > 34 or tokens[0] not in _QUESTION_START:
        return None
    if tokens[0] in {"show", "tell"} and tokens[1:2] != ["me"]:
        return None
    if tokens[:2] in (["can", "you"], ["could", "you"], ["would", "you"]):
        if tokens[2:3] not in (["tell"], ["show"], ["see"], ["confirm"]):
            return None
    if tokens[0] in {"can", "could", "would", "may"} and tokens[1:2] in (["i"], ["we"]):
        if tokens[2:3] not in (["see"], ["know"], ["tell"], ["confirm"]):
            return None
    # A question about a completed action can contain an action verb. A second
    # imperative or a request for instructions cannot be interpreted as the same read.
    if re.search(
        r"\b(?:and|or|then|please) "
        r"(?:approve|deliver|dispatch|fit|release|retrain|send|ship|train)\b"
        r"|\bhow (?:can|do|should) (?:i|we|you)\b|\bhow to\b",
        " ".join(tokens),
    ):
        return None
    words = set(tokens)

    def mentions(vocabulary: str) -> bool:
        return not words.isdisjoint(vocabulary.split())

    timing = mentions(
        "timing timings faster speed speedup quicker quickly slower speedups latency runtime "
        "duration elapsed shorter longer savings acceleration"
    ) or (
        mentions("time times long")
        and mentions("fit training take taken took save saved reduce reduced")
    )
    training = mentions(
        "train training trained untrained fit fitted fitting retrain retraining tuning finetune "
        "finetuning learning trained weights"
    ) and mentions(
        "why what when did was has is not ready readiness eligible worth worthwhile held holding "
        "blocked blocker prevented skip skipped reason reasons missing enough started deferred"
    )
    trace = (
        mentions("input inputs prompt prompts context capture payload saw fed")
        or (
            mentions("model assistant classifier decision")
            and mentions(
                "say said see shown read respond responded reply replied output outputs return "
                "returned predict predicted prediction decisions answers answer "
                "chose receive received"
            )
        )
        or (mentions("went came") and {"in", "out"} <= words)
    )
    delivery = (
        mentions(
            "ship shipped shipping handoff handoffs handover sent delivered delivery dispatched "
            "destination recipient recipients receiver received readback arrived landed "
            "reach reached transferred"
        )
        or (mentions("go went gone made") and mentions("out through"))
        or (mentions("hand handed") and mentions("off over"))
    )
    weekly = (
        mentions("weekly week")
        or ("days" in words and mentions("seven 7"))
        or {"since", "monday"} <= words
    )
    errors = mentions(
        "error errors mistakes mistake misses miss failures failure wrong wrongly incorrect "
        "misclassified misclassifications issues incidents clean quiet problems zero flagged right"
    )
    matching = [
        topic
        for topic, matches in (
            (StatusTopic.TIMING, timing),
            (StatusTopic.TRAINING, training),
            (StatusTopic.TRACE, trace),
            (StatusTopic.DELIVERY, delivery),
            (StatusTopic.LIVE_ERRORS, weekly and errors),
        )
        if matches and words <= _COMMON | _VOCAB[topic]
    ]
    return matching[0] if len(matching) == 1 else None


@dataclass(frozen=True)
class ApprovalEvidence:
    stage: str
    reference: str
    recorded_at: int | None = None


@dataclass(frozen=True)
class StatusAnswer:
    finding: str
    basis: str
    next_action: str


def read_status(
    topic: StatusTopic,
    audit: Mapping[str, Any],
    *,
    managed_outcome: str = "",
    approval: ApprovalEvidence | None = None,
    destination_configured: bool = True,
) -> StatusAnswer:
    mode = audit["mode"]
    if mode not in {"evaluation_only", "training", "managed_model", "managed_rule"}:
        raise ValueError("Unknown readout mode.")
    supplied = mode == "evaluation_only"
    local_fit = mode == "training"
    if topic == StatusTopic.TRAINING:
        if supplied:
            return StatusAnswer(
                "Training was not authorized for this evaluation-only dataset.",
                "The worker scored supplied outputs against the frozen cohort.",
                "The workflow owner can review the evaluation scope with the data custodian.",
            )
        if local_fit:
            return StatusAnswer(
                "The selected CPU training job completed.",
                f"The frozen result records {audit['epochs']} training epochs.",
                "Open the evidence to inspect held-out changes before considering another fit.",
            )
        return StatusAnswer(
            "This result includes a model comparison."
            if mode == "managed_model"
            else "This result records a rule comparison.",
            managed_outcome or "The run has no published readiness reason.",
            "The data custodian can inspect the independent residual groups and compute grant.",
        )
    if topic == StatusTopic.TIMING:
        basis = (
            "The selected result scored supplied outputs and contains no fit interval."
            if supplied
            else "This completed result has no registered baseline/candidate fit-time pairs."
        )
        return StatusAnswer(
            "A fit-speed comparison is unavailable.",
            basis,
            "The training operator can register matched fit start/end measurements. "
            "Preparation, replay and end-to-end verification need their own measured intervals.",
        )
    if topic == StatusTopic.TRACE:
        return StatusAnswer(
            "Decision-time model input and original model output are unavailable in this readout.",
            "The worker compared supplied outputs."
            if supplied
            else "The worker scored a frozen cohort. That replay measures a different boundary.",
            "The authorized source custodian can provide the committed decision and permitted "
            "original capture, then link any later verification separately.",
        )
    if topic == StatusTopic.LIVE_ERRORS:
        return StatusAnswer(
            "Verified live errors and the denominator for the requested window are unavailable.",
            f"This frozen evaluation covers {audit['population']} cohort cases. "
            "It has no weekly feed checkpoint or outcome-verification window.",
            "The feed owner can check the last sync and covered window with the verifier.",
        )
    if topic != StatusTopic.DELIVERY:
        raise ValueError("Unsupported status topic.")
    if not destination_configured:
        return StatusAnswer(
            "No release is authorized by the current configuration.",
            "This result has no configured handoff destination. Earlier delivery is unconfirmed.",
            "The workflow owner can check the current handoff settings.",
        )
    if approval is None:
        return StatusAnswer(
            "Handoff delivery is unconfirmed.",
            "No verified approval receipt is available here for this result.",
            "The release owner can check the outbox and the intended recipient's readback.",
        )
    time = (
        " at " + datetime.fromtimestamp(approval.recorded_at, UTC).strftime("%Y-%m-%d %H:%M UTC")
        if approval.recorded_at is not None
        else ""
    )
    if approval.stage in {"release_changes_requested", "readout_changes_requested"}:
        subject = "Release" if approval.stage == "release_changes_requested" else "Readout"
        return StatusAnswer(
            f"{subject} is held pending changes.",
            f"Changes requested{time}. Intended recipient {audit['recipient']}. "
            f"Approval evidence {approval.reference}. Earlier delivery is unconfirmed.",
            "The workflow owner can open the selected evidence and address the review decision.",
        )
    if approval.stage in {"expired", "approval_unresolved"}:
        expiry = approval.stage == "expired"
        finding = (
            "Approval request expired." if expiry else "Approval outcome awaits reconciliation."
        )
        next_action = (
            "The release owner can inspect the current handoff evidence."
            if expiry
            else "The release owner can reconcile the approval request against provider history."
        )
        return StatusAnswer(
            finding,
            f"Approval evidence {approval.reference}{time}. "
            f"Intended recipient {audit['recipient']}. Delivery remains unconfirmed.",
            next_action,
        )
    stage = {
        "approved": "Approval recorded",
        "changes_requested": "Changes requested",
        "staged": "Approval request staged",
        "accepted": "Approval request transport acknowledged",
        "failed": "Approval request transport failed",
        "uncertain": "Approval request transport unconfirmed",
    }[approval.stage]
    return StatusAnswer(
        "Handoff delivery is unconfirmed.",
        f"{stage}{time}. Intended recipient {audit['recipient']}. "
        f"Approval evidence {approval.reference}. "
        "A receiver readback for the selected revision is unavailable here.",
        "The release owner can reconcile the outbox with the intended recipient.",
    )


# ---------------------------------------------------------------------------------------------
# Typed, receipt-backed status projections.
#
# Each projection reads one producer's receipt for the exact scope of a status question and
# returns a typed answer. It answers only from a current, in-scope, well-formed receipt. An
# absent, stale, out-of-scope, malformed or unrecognized receipt is an explicit hold with a
# reason. An unknown result never becomes success. Projections are pure functions of their
# typed inputs and an explicit clock: they read, write and dispatch nothing. The producers and
# their authority checks stay with their owners: training readiness (training_readiness.py),
# the release outbox (release_outbox.py), live evidence (live_evidence.py), registered stage
# timing (OPE-530) and the optional original capture (OPE-532). This half of the module imports
# only the standard library, so the Slack adapter can import it without an import cycle.


class Disposition(StrEnum):
    ANSWERED = "answered"
    HELD = "held"


class HoldReason(StrEnum):
    RECEIPT_ABSENT = "receipt_absent"
    RECEIPT_INVALID = "receipt_invalid"
    RECEIPT_STALE = "receipt_stale"
    SCOPE_MISMATCH = "scope_mismatch"
    RESULT_UNKNOWN = "result_unknown"
    TIMING_NOT_COMPARABLE = "timing_not_comparable"
    TIMING_STAGE_MISMATCH = "timing_stage_mismatch"
    TIMING_ATTEMPT_FAILED = "timing_attempt_failed"
    TIMING_ATTEMPTS_OMITTED = "timing_attempts_omitted"
    INSUFFICIENT_COMPARABLE_PAIRS = "insufficient_comparable_pairs"
    ORIGINAL_UNAVAILABLE = "original_unavailable"
    ORIGINAL_NOT_OBSERVED = "original_not_observed"
    NO_DESTINATION = "no_destination"
    APPROVAL_PENDING = "approval_pending"
    APPROVAL_REQUEST_FAILED = "approval_request_failed"
    APPROVAL_EXPIRED = "approval_expired"
    AWAITING_DELIVERY = "awaiting_delivery"
    NO_FEED = "no_feed"
    FEED_STALE = "feed_stale"
    COVERAGE_INCOMPLETE = "coverage_incomplete"
    VERIFIER_NOT_BOUND = "verifier_not_bound"
    AWAITING_OUTCOME = "awaiting_outcome"
    DISPUTED_OUTCOMES = "disputed_outcomes"
    NO_VERIFIED_AUTOMATIC = "no_verified_automatic"


@dataclass(frozen=True)
class ReceiptRef:
    """The producer receipt an answer or hold was read from. Opaque references only."""

    kind: str
    reference: str
    revision: str | None = None
    recorded_at: int | None = None


@dataclass(frozen=True)
class StatusProjection:
    """One typed status answer. `answer` carries the same facts as prose for existing views."""

    topic: StatusTopic
    disposition: Disposition
    state: str
    hold_reason: HoldReason | None
    as_of: int
    receipts: tuple[ReceiptRef, ...]
    facts: ProjectionFacts | None
    next_action: str
    next_owner: str
    answer: StatusAnswer

    def __post_init__(self) -> None:
        if (self.disposition is Disposition.HELD) != (self.hold_reason is not None):
            raise ValueError("A hold carries exactly one reason and an answer carries none.")

    @property
    def held(self) -> bool:
        return self.disposition is Disposition.HELD


class _Invalid(Exception):
    """A supplied receipt broke its typed contract. Projected as receipt_invalid."""


_REFERENCE = re.compile(r"[^\s\x00-\x1f\x7f]{1,256}")
_INSTANT_LIMIT = 2**63

# Each next action names a code, its responsible role and the prose for existing views.
_NEXT_ACTIONS: dict[str, tuple[str, str]] = {
    "register_matched_timing": (
        "training_operator",
        "The training operator can register matched baseline and candidate measurements. "
        "Fit, preparation and end-to-end time each need their own measured intervals.",
    ),
    "show_timing_measurements": (
        "workflow_owner",
        "Open the registered measurements to inspect every matched pair and retained run.",
    ),
    "request_readiness_receipt": (
        "workflow_owner",
        "Ask the workflow owner to check for a current worker readiness receipt.",
    ),
    "refresh_readiness": (
        "worker_operator",
        "The worker operator can refresh the readiness receipt for the current evidence.",
    ),
    "instrument_original_capture": (
        "source_custodian",
        "The source custodian can provide the committed decision and a permitted original "
        "capture, then link any later verification separately.",
    ),
    "reconcile_decision_trace": (
        "source_custodian",
        "The source custodian can reconcile the decision trace with the current decision revision.",
    ),
    "open_original_capture": (
        "source_custodian",
        "The source custodian can open the permitted capture privately.",
    ),
    "check_handoff_settings": (
        "workflow_owner",
        "The workflow owner can check the current handoff settings.",
    ),
    "check_release_approval": (
        "release_owner",
        "The release owner can check the outbox and the intended recipient's readback.",
    ),
    "address_review_decision": (
        "workflow_owner",
        "The workflow owner can open the selected evidence and address the review decision.",
    ),
    "reconcile_approval": (
        "release_owner",
        "The release owner can reconcile the approval request against provider history.",
    ),
    "reconcile_delivery": (
        "handoff_operator",
        "The authorized handoff operator can reconcile delivery with the intended recipient.",
    ),
    "replay_artifact": ("recipient", "The recipient can independently replay the artifact."),
    "connect_decision_feed": (
        "data_custodian",
        "The data custodian can connect an authorized live decision feed.",
    ),
    "check_feed_sync": (
        "data_custodian",
        "The feed owner can check the last sync and the covered window with the verifier.",
    ),
    "refresh_window_coverage": (
        "worker_operator",
        "The worker operator can record decision and outcome coverage for the requested window.",
    ),
    "bind_verifier": ("data_custodian", "Choose the authorized independent outcome feed to bind."),
    "ask_data_custodian_to_bind_verifier": (
        "data_custodian",
        "Ask the data custodian to bind a verifier.",
    ),
    "verify_outcome": ("verifier", "Record an independent outcome."),
    "inspect_live_evidence": ("workflow_owner", "Open the live evidence for this window."),
    "await_next_sync": (
        "workflow_owner",
        "No action is needed. The next sync extends the covered window.",
    ),
}


def _reference(value: object) -> str:
    if not isinstance(value, str) or _REFERENCE.fullmatch(value) is None:
        raise _Invalid
    return value


def _optional_reference(value: object) -> str | None:
    return None if value is None else _reference(value)


def _count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value < _INSTANT_LIMIT:
        raise _Invalid
    return value


def _optional_count(value: object) -> int | None:
    return None if value is None else _count(value)


def _instant(value: object, as_of: int) -> int:
    """A recorded time later than the projection clock cannot support a current answer."""
    instant = _count(value)
    if instant > as_of:
        raise _Invalid
    return instant


def _optional_instant(value: object, as_of: int) -> int | None:
    return None if value is None else _instant(value, as_of)


def _flag(value: object) -> bool:
    if not isinstance(value, bool):
        raise _Invalid
    return value


def _clock(as_of: object) -> int:
    if isinstance(as_of, bool) or not isinstance(as_of, int) or not 0 <= as_of < _INSTANT_LIMIT:
        raise ValueError("A projection needs an integer Unix-second clock.")
    return as_of


def _scope_reference(value: object, name: str) -> str:
    try:
        return _reference(value)
    except _Invalid:
        raise ValueError(f"Invalid {name} for a status projection scope.") from None


def _utc(instant: int) -> str:
    return datetime.fromtimestamp(instant, UTC).strftime("%Y-%m-%d %H:%M UTC")


def _plural(count: int, singular: str, plural: str = "") -> str:
    return f"{count} {singular if count == 1 else plural or singular + 's'}"


def _projection(
    topic: StatusTopic,
    state: str,
    *,
    as_of: int,
    finding: str,
    basis: str,
    action: str,
    hold: HoldReason | None = None,
    receipts: tuple[ReceiptRef, ...] = (),
    facts: ProjectionFacts | None = None,
    owner: str = "",
    action_text: str = "",
) -> StatusProjection:
    default_owner, default_text = _NEXT_ACTIONS.get(action, ("", ""))
    next_owner, text = owner or default_owner, action_text or default_text
    if not next_owner or not text:
        raise ValueError("Every status projection names a next action and its owner.")
    return StatusProjection(
        topic,
        Disposition.HELD if hold is not None else Disposition.ANSWERED,
        state,
        hold,
        as_of,
        receipts,
        facts,
        action,
        next_owner,
        StatusAnswer(finding, basis, text),
    )


# ----- Speed: "How much faster is it?" (OPE-533 S05, OPE-530 stage timing) --------------------


class TimingStage(StrEnum):
    PREPARATION = "preparation"
    FIT = "fit"
    END_TO_END = "end_to_end"
    REPLAY = "replay"


class TimingState(StrEnum):
    COMPARED = "compared"
    UNAVAILABLE = "unavailable"


# Decided under owner delegation, overnight sprint 2026-11-28 (reversible by the owner): a stage
# needs at least five registered matched pairs before it supports a speed answer. One run never
# does. Five matches the OPE-530 repeats per condition and the OPE-533 S05 comparable fixture.
MIN_TIMING_PAIRS = 5
# Time to a verified verdict leads (owner decision D4). Replay time is never fit or speed time.
SPEED_STAGES = (TimingStage.END_TO_END, TimingStage.FIT, TimingStage.PREPARATION)
_STAGE_LABELS = {
    TimingStage.END_TO_END: "Time to a verified verdict",
    TimingStage.FIT: "Fit wall time",
    TimingStage.PREPARATION: "Preparation time",
}
_RUN_STATUSES = frozenset({"completed", "failed", "unknown"})
_TIMING_REASONS = {
    HoldReason.RECEIPT_ABSENT: "no registered pairs",
    HoldReason.TIMING_STAGE_MISMATCH: "a pair compares different stages",
    HoldReason.RESULT_UNKNOWN: "a registered run has an unknown result",
    HoldReason.TIMING_ATTEMPT_FAILED: "a registered run failed",
    HoldReason.TIMING_NOT_COMPARABLE: "conditions or endpoints differ",
    HoldReason.TIMING_ATTEMPTS_OMITTED: "registered runs are missing",
    HoldReason.INSUFFICIENT_COMPARABLE_PAIRS: (
        f"fewer than {MIN_TIMING_PAIRS} registered matched pairs"
    ),
}


@dataclass(frozen=True)
class TimedRun:
    """One registered measurement. A failed or unknown run stays in the receipt."""

    run_ref: str
    stage: str
    status: str
    elapsed_ms: int | None
    # Digest of task, hardware, cache state, exposure and effective update budget.
    condition_ref: str
    start_event: str
    end_event: str


@dataclass(frozen=True)
class TimingPair:
    pair_ref: str
    baseline: TimedRun
    candidate: TimedRun


@dataclass(frozen=True)
class TimingPlan:
    stage: str
    registered_pairs: int


@dataclass(frozen=True)
class TimingReceipt:
    """Preregistered baseline and candidate stage timing for one exact pair of revisions."""

    receipt_ref: str
    recorded_at: int
    baseline_revision: str
    candidate_revision: str
    plan: tuple[TimingPlan, ...]
    pairs: tuple[TimingPair, ...]
    source_clean: bool


@dataclass(frozen=True)
class StageComparison:
    stage: TimingStage
    state: TimingState
    hold_reason: HoldReason | None
    pairs: int
    registered_pairs: int
    start_event: str | None = None
    end_event: str | None = None
    baseline_median_ms: float | None = None
    candidate_median_ms: float | None = None
    # Median and maximum of the per-pair candidate/baseline elapsed-time ratios.
    median_ratio: float | None = None
    worst_pair_ratio: float | None = None


@dataclass(frozen=True)
class TimingFacts:
    baseline_revision: str
    candidate_revision: str
    stages: tuple[StageComparison, ...]


def _timed_run(run: object) -> TimedRun:
    if not isinstance(run, TimedRun) or run.status not in _RUN_STATUSES:
        raise _Invalid
    _reference(run.run_ref)
    try:
        TimingStage(run.stage)
    except ValueError:
        raise _Invalid from None
    if run.status == "completed":
        if _count(run.elapsed_ms) == 0:
            raise _Invalid
    elif run.elapsed_ms is not None:
        raise _Invalid
    for reference in (run.condition_ref, run.start_event, run.end_event):
        _reference(reference)
    return run


def _validated_timing(receipt: object, as_of: int) -> TimingReceipt:
    if not isinstance(receipt, TimingReceipt):
        raise _Invalid
    for reference in (receipt.receipt_ref, receipt.baseline_revision, receipt.candidate_revision):
        _reference(reference)
    _instant(receipt.recorded_at, as_of)
    _flag(receipt.source_clean)
    if not isinstance(receipt.plan, tuple) or not receipt.plan:
        raise _Invalid
    registered: dict[str, int] = {}
    for entry in receipt.plan:
        if not isinstance(entry, TimingPlan) or entry.stage in registered:
            raise _Invalid
        try:
            TimingStage(entry.stage)
        except ValueError:
            raise _Invalid from None
        if _count(entry.registered_pairs) == 0:
            raise _Invalid
        registered[entry.stage] = entry.registered_pairs
    if not isinstance(receipt.pairs, tuple):
        raise _Invalid
    pair_refs: set[str] = set()
    run_refs: set[str] = set()
    touching: dict[str, int] = dict.fromkeys(registered, 0)
    for pair in receipt.pairs:
        if not isinstance(pair, TimingPair) or _reference(pair.pair_ref) in pair_refs:
            raise _Invalid
        pair_refs.add(pair.pair_ref)
        runs = (_timed_run(pair.baseline), _timed_run(pair.candidate))
        for run in runs:
            if run.run_ref in run_refs or run.stage not in registered:
                # A duplicated or unregistered measurement could select the favorable runs.
                raise _Invalid
            run_refs.add(run.run_ref)
        for stage in {run.stage for run in runs}:
            touching[stage] += 1
    if any(touching[stage] > count for stage, count in registered.items()):
        raise _Invalid
    return receipt


def _elapsed(run: TimedRun) -> int:
    if run.elapsed_ms is None:
        raise _Invalid
    return run.elapsed_ms


def _compare_stage(
    stage: TimingStage, registered: int, pairs: tuple[TimingPair, ...]
) -> StageComparison:
    touching = [pair for pair in pairs if stage in (pair.baseline.stage, pair.candidate.stage)]
    runs = [run for pair in touching for run in (pair.baseline, pair.candidate)]
    reason: HoldReason | None = None
    if not registered:
        reason = HoldReason.RECEIPT_ABSENT
    elif any(pair.baseline.stage != pair.candidate.stage for pair in touching):
        reason = HoldReason.TIMING_STAGE_MISMATCH
    elif any(run.status == "unknown" for run in runs):
        reason = HoldReason.RESULT_UNKNOWN
    elif any(run.status == "failed" for run in runs):
        reason = HoldReason.TIMING_ATTEMPT_FAILED
    elif (
        any(pair.baseline.condition_ref != pair.candidate.condition_ref for pair in touching)
        or len({(run.start_event, run.end_event) for run in runs}) > 1
    ):
        reason = HoldReason.TIMING_NOT_COMPARABLE
    elif len(touching) < registered:
        reason = HoldReason.TIMING_ATTEMPTS_OMITTED
    elif registered < MIN_TIMING_PAIRS:
        reason = HoldReason.INSUFFICIENT_COMPARABLE_PAIRS
    if reason is not None:
        return StageComparison(stage, TimingState.UNAVAILABLE, reason, len(touching), registered)
    baseline = [_elapsed(pair.baseline) for pair in touching]
    candidate = [_elapsed(pair.candidate) for pair in touching]
    ratios = [Fraction(c, b) for b, c in zip(baseline, candidate, strict=True)]
    return StageComparison(
        stage,
        TimingState.COMPARED,
        None,
        len(touching),
        registered,
        touching[0].baseline.start_event,
        touching[0].baseline.end_event,
        round(float(median(baseline)), 1),
        round(float(median(candidate)), 1),
        round(float(median(ratios)), 4),
        round(float(max(ratios)), 4),
    )


def _speed_finding(comparison: StageComparison) -> str:
    assert comparison.median_ratio is not None
    label, ratio = _STAGE_LABELS[comparison.stage], comparison.median_ratio
    pairs = _plural(comparison.pairs, "matched pair")
    if ratio < 1:
        return f"{label} is {(1 - ratio) * 100:.1f}% shorter at the median of {pairs}."
    if ratio > 1:
        return f"{label} is {(ratio - 1) * 100:.1f}% longer at the median of {pairs}."
    return f"{label} is unchanged at the median of {pairs}."


def _speed_line(comparison: StageComparison) -> str:
    return (
        f"{_STAGE_LABELS[comparison.stage]}: baseline median {comparison.baseline_median_ms:,.1f} "
        f"ms, candidate median {comparison.candidate_median_ms:,.1f} ms over "
        f"{_plural(comparison.pairs, 'matched pair')}, measured from {comparison.start_event} to "
        f"{comparison.end_event}. Worst pair ratio {comparison.worst_pair_ratio:.2f}."
    )


def project_timing(
    receipt: TimingReceipt | None,
    *,
    baseline_revision: str | None,
    candidate_revision: str | None,
    as_of: int,
) -> StatusProjection:
    """Compare registered stage times for exactly the selected baseline and candidate."""
    as_of = _clock(as_of)
    unavailable = "A speed comparison is unavailable."

    def held(
        reason: HoldReason, basis: str, *refs: ReceiptRef, facts: TimingFacts | None = None
    ) -> StatusProjection:
        return _projection(
            StatusTopic.TIMING,
            TimingState.UNAVAILABLE,
            as_of=as_of,
            finding=unavailable,
            basis=basis,
            action="register_matched_timing",
            hold=reason,
            receipts=refs,
            facts=facts,
        )

    if baseline_revision is None or candidate_revision is None:
        return held(
            HoldReason.RECEIPT_ABSENT,
            "The selected result has no fitted candidate and baseline pair, so it has no fit "
            "interval to compare.",
        )
    baseline_revision = _scope_reference(baseline_revision, "baseline revision")
    candidate_revision = _scope_reference(candidate_revision, "candidate revision")
    if receipt is None:
        return held(
            HoldReason.RECEIPT_ABSENT,
            "No registered baseline and candidate timing receipt exists for this result. "
            "Replay timing is not fit timing.",
        )
    try:
        _validated_timing(receipt, as_of)
    except _Invalid:
        return held(HoldReason.RECEIPT_INVALID, "The timing receipt failed its integrity checks.")
    ref = ReceiptRef(
        "stage_timing", receipt.receipt_ref, receipt.candidate_revision, receipt.recorded_at
    )
    if not receipt.source_clean:
        return held(
            HoldReason.RECEIPT_INVALID,
            "The timing receipt was measured on an unrecorded or modified source.",
            ref,
        )
    if (receipt.baseline_revision, receipt.candidate_revision) != (
        baseline_revision,
        candidate_revision,
    ):
        return held(
            HoldReason.RECEIPT_STALE,
            "The timing receipt measured a different baseline or candidate than this result.",
            ref,
        )
    plan = {TimingStage(entry.stage): entry.registered_pairs for entry in receipt.plan}
    stages = tuple(
        _compare_stage(stage, plan.get(stage, 0), receipt.pairs) for stage in SPEED_STAGES
    )
    facts = TimingFacts(baseline_revision, candidate_revision, stages)
    compared = [stage for stage in stages if stage.state is TimingState.COMPARED]
    missing = " ".join(
        f"{_STAGE_LABELS[stage.stage]} is unavailable: {_TIMING_REASONS[stage.hold_reason]}."
        for stage in stages
        if stage.hold_reason is not None
    )
    replay = " Replay time is not used as fit or speed time." if TimingStage.REPLAY in plan else ""
    if not compared:
        # Report the most specific reason; a stage that was never registered is the weakest.
        reasons = [
            stage.hold_reason
            for stage in stages
            if stage.hold_reason is not None and stage.hold_reason is not HoldReason.RECEIPT_ABSENT
        ]
        reason = reasons[0] if reasons else HoldReason.RECEIPT_ABSENT
        return held(reason, f"{missing}{replay}", ref, facts=facts)
    return _projection(
        StatusTopic.TIMING,
        TimingState.COMPARED,
        as_of=as_of,
        finding=_speed_finding(compared[0]),
        basis=" ".join([*(_speed_line(stage) for stage in compared), missing]).strip() + replay,
        action="show_timing_measurements",
        receipts=(ref,),
        facts=facts,
    )


# ----- Training readiness: "Why didn't it train?" (OPE-533 S06, OPE-542) -------------------------

READINESS_SCHEMA = "opentour.pilot.training-readiness.v1"


class ReadinessState(StrEnum):
    NO_LIVE_DECISIONS = "no_live_decisions"
    NO_VERIFIER_BOUND = "no_verifier_bound"
    AWAITING_OUTCOME = "awaiting_outcome"
    DISPUTED_OUTCOMES = "disputed_outcomes"
    TOO_EARLY_TO_TRAIN = "too_early_to_train"
    SIMPLER_ROUTE_SUFFICIENT = "simpler_route_sufficient"
    FIT_ELIGIBLE = "fit_eligible"
    COMMITTED_ACTION_CLASSIFICATION_REQUIRED = "committed_action_classification_required"
    DECISION_TIME_INPUT_REQUIRED = "decision_time_input_required"
    TRAINING_MANDATE_REQUIRED = "training_mandate_required"
    TRAINING_RIGHTS_REQUIRED = "training_rights_required"
    TRAINING_BUDGET_REQUIRED = "training_budget_required"
    UNTOUCHED_HOLDOUT_REQUIRED = "untouched_holdout_required"
    QUALIFIED_MODEL_REQUIRED = "qualified_model_required"
    DEVELOPMENT_EVIDENCE_REQUIRED = "development_evidence_required"
    REPAIR_ASSESSMENT_REQUIRED = "repair_assessment_required"
    UNAVAILABLE = "unavailable"


_READINESS_FINDINGS = {
    ReadinessState.NO_LIVE_DECISIONS: "No live decisions yet.",
    ReadinessState.DISPUTED_OUTCOMES: "Independent outcomes are disputed.",
    ReadinessState.TOO_EARLY_TO_TRAIN: "Too early to train.",
    ReadinessState.SIMPLER_ROUTE_SUFFICIENT: "The simpler repair is sufficient.",
    ReadinessState.FIT_ELIGIBLE: "The worker recorded fit eligibility. No fit has been admitted.",
    ReadinessState.COMMITTED_ACTION_CLASSIFICATION_REQUIRED: (
        "Missing observed action classification."
    ),
    ReadinessState.DECISION_TIME_INPUT_REQUIRED: "The original decision input is missing.",
    ReadinessState.TRAINING_MANDATE_REQUIRED: "Training needs a current mandate.",
    ReadinessState.TRAINING_RIGHTS_REQUIRED: "Training rights are missing.",
    ReadinessState.TRAINING_BUDGET_REQUIRED: "Training budget is unavailable.",
    ReadinessState.UNTOUCHED_HOLDOUT_REQUIRED: "An independent holdout is missing.",
    ReadinessState.QUALIFIED_MODEL_REQUIRED: "The selected model needs qualification.",
    ReadinessState.DEVELOPMENT_EVIDENCE_REQUIRED: (
        "More independent development evidence is needed."
    ),
    ReadinessState.REPAIR_ASSESSMENT_REQUIRED: "A simple-repair assessment is needed.",
}
_READINESS_ACTIONS = {
    "bind_verifier": "Choose the authorized independent outcome feed to bind.",
    "ask_data_custodian_to_bind_verifier": "Ask the data custodian to bind a verifier.",
    "verify_outcome": "Record an independent outcome.",
    "observe_committed_decisions": "Record the first committed decision.",
    "request_authorized_fit_admission": "Request separate fit admission.",
    "inspect_simple_repair": "Inspect the simpler repair.",
    "supply_independent_evidence": "Supply independent evidence.",
    "provide_observed_action_classification": "Supply the observed action classification.",
    "provide_decision_time_input": "Provide permitted decision-time input.",
    "authorize_training_scope": "Authorize a training mandate.",
    "authorize_training_rights": "Review the requested training rights.",
    "review_training_budget": "Review the training budget.",
    "supply_independent_holdout": "Supply an independent holdout.",
    "qualify_selected_model": "Qualify the selected model.",
    "assess_simple_repair": "Assess the simpler repair.",
}


@dataclass(frozen=True)
class ReadinessScope:
    """The current workflow evidence the question is about, resolved under current authority."""

    organization: str
    workflow_ref: str
    engagement: str
    dataset_ref: str | None
    dataset_revision: str | None
    evidence_revision: str


@dataclass(frozen=True)
class ReadinessFacts:
    readiness_ref: str
    version: int
    evidence_revision: str
    verdict: str
    decisions: int
    verified: int
    pending: int
    disputed: int
    verified_roots: int
    independent_families: int
    residual_groups: int | None
    minimum_residual_groups: int | None
    verifier_state: str
    augmentation_state: str
    missing_gates: tuple[str, ...]
    mandate_ref: str | None
    assessment_ref: str | None


@dataclass(frozen=True)
class _Readiness:
    facts: ReadinessFacts
    scope: tuple[str, str, str, str | None, str | None]
    reason: str
    next_action: str
    next_owner: str
    recorded_at: int
    revision: str


def _parsed_readiness(receipt: Mapping[str, Any], as_of: int) -> _Readiness:
    """Parse a worker readiness receipt and recheck the producer's own invariants."""
    try:
        if not isinstance(receipt, Mapping) or receipt["schema_version"] != READINESS_SCHEMA:
            raise _Invalid
        revision = _reference(receipt["revision"])
        readiness_ref = _reference(receipt["readiness_ref"])
        if readiness_ref != "ready-" + revision[:40]:
            raise _Invalid
        version = _count(receipt["version"])
        decisions, verified, pending, disputed, roots, families = (
            _count(receipt[key])
            for key in (
                "decision_count",
                "verified_decision_count",
                "pending_count",
                "disputed_count",
                "verified_root_count",
                "independent_family_count",
            )
        )
        residuals = _optional_count(receipt["residual_group_count"])
        minimum = _optional_count(receipt["minimum_residual_groups"])
        verifier, verdict = receipt["verifier_state"], receipt["verdict"]
        gates = receipt["missing_gates"]
        reason = receipt["reason_code"]
        if (
            version == 0
            or decisions != verified + pending + disputed
            or not families <= roots <= verified
            or (residuals is not None and residuals > families)
            or minimum == 0
            or verifier not in {"bound", "not_bound"}
            or verdict not in {"held", "fit_eligible"}
            or (verdict == "fit_eligible") != (reason == "fit_eligible")
            or not isinstance(gates, list | tuple)
            or not isinstance(reason, str)
        ):
            raise _Invalid
        facts = ReadinessFacts(
            readiness_ref,
            version,
            _reference(receipt["evidence_revision"]),
            verdict,
            decisions,
            verified,
            pending,
            disputed,
            roots,
            families,
            residuals,
            minimum,
            verifier,
            _reference(receipt["augmentation_state"]),
            tuple(_reference(gate) for gate in gates),
            _optional_reference(receipt["mandate_ref"]),
            _optional_reference(receipt["assessment_ref"]),
        )
        scope = (
            _reference(receipt["organization"]),
            _reference(receipt["workflow_ref"]),
            _reference(receipt["engagement"]),
            _optional_reference(receipt["dataset_ref"]),
            _optional_reference(receipt["dataset_revision"]),
        )
        next_action, next_owner = receipt["next_action"], _reference(receipt["next_owner"])
        if next_action not in _READINESS_ACTIONS:
            raise _Invalid
        recorded_at = _instant(receipt["recorded_at"], as_of)
    except (KeyError, TypeError):
        raise _Invalid from None
    _check_readiness_reason(facts, reason)
    return _Readiness(facts, scope, reason, next_action, next_owner, recorded_at, revision)


def _check_readiness_reason(facts: ReadinessFacts, reason: str) -> None:
    """A known reason must agree with the receipt's own counts; eligibility needs every input."""
    if reason not in ReadinessState or reason == ReadinessState.UNAVAILABLE:
        return  # Projected as result_unknown. An unrecognized reason is never eligibility.
    pending_only = facts.verified == 0 and facts.pending > 0
    consistent = {
        ReadinessState.NO_LIVE_DECISIONS: facts.decisions == 0,
        ReadinessState.NO_VERIFIER_BOUND: pending_only and facts.verifier_state == "not_bound",
        ReadinessState.AWAITING_OUTCOME: pending_only and facts.verifier_state == "bound",
        ReadinessState.DISPUTED_OUTCOMES: (
            facts.verified == 0 and facts.pending == 0 and facts.disputed > 0
        ),
        ReadinessState.TOO_EARLY_TO_TRAIN: (
            facts.verified > 0
            and facts.residual_groups is not None
            and facts.minimum_residual_groups is not None
            and facts.residual_groups < facts.minimum_residual_groups
        ),
        ReadinessState.FIT_ELIGIBLE: (
            facts.verified > 0
            and facts.residual_groups is not None
            and facts.minimum_residual_groups is not None
            and facts.residual_groups >= facts.minimum_residual_groups
            and facts.mandate_ref is not None
            and facts.assessment_ref is not None
            and not facts.missing_gates
        ),
    }
    # Every other known reason comes from an assessment, which follows a verified decision.
    if not consistent.get(ReadinessState(reason), facts.verified > 0):
        raise _Invalid


def project_training_readiness(
    receipt: Mapping[str, Any] | None, *, scope: ReadinessScope, as_of: int
) -> StatusProjection:
    """Explain the current training hold from the worker's readiness receipt for `scope`."""
    as_of = _clock(as_of)
    topic, unavailable = StatusTopic.TRAINING, "Current training readiness is unavailable."
    expected = (
        _scope_reference(scope.organization, "organization"),
        _scope_reference(scope.workflow_ref, "workflow"),
        _scope_reference(scope.engagement, "engagement"),
        scope.dataset_ref,
        scope.dataset_revision,
    )
    current_evidence = _scope_reference(scope.evidence_revision, "evidence revision")
    if receipt is None:
        return _projection(
            topic,
            ReadinessState.UNAVAILABLE,
            as_of=as_of,
            finding=unavailable,
            basis="No current worker readiness receipt exists for this workflow's evidence.",
            action="request_readiness_receipt",
            hold=HoldReason.RECEIPT_ABSENT,
        )
    try:
        parsed = _parsed_readiness(receipt, as_of)
    except _Invalid:
        return _projection(
            topic,
            ReadinessState.UNAVAILABLE,
            as_of=as_of,
            finding=unavailable,
            basis="The readiness receipt failed its integrity checks. It is not eligibility.",
            action="refresh_readiness",
            hold=HoldReason.RECEIPT_INVALID,
        )
    facts = parsed.facts
    ref = ReceiptRef("training_readiness", facts.readiness_ref, parsed.revision, parsed.recorded_at)

    def held(reason: HoldReason, basis: str) -> StatusProjection:
        # An out-of-scope receipt discloses nothing. A stale or unrecognized one names only
        # its reference, so its counts are never read as current.
        return _projection(
            topic,
            ReadinessState.UNAVAILABLE,
            as_of=as_of,
            finding=unavailable,
            basis=basis,
            action="refresh_readiness",
            hold=reason,
            receipts=() if reason is HoldReason.SCOPE_MISMATCH else (ref,),
        )

    if parsed.scope != expected:
        return held(
            HoldReason.SCOPE_MISMATCH,
            "The readiness receipt belongs to a different workflow, engagement or dataset "
            "revision.",
        )
    if facts.evidence_revision != current_evidence:
        return held(
            HoldReason.RECEIPT_STALE,
            f"The readiness receipt was computed for evidence {facts.evidence_revision}. "
            f"Current evidence is {current_evidence}.",
        )
    if parsed.reason not in ReadinessState or parsed.reason == ReadinessState.UNAVAILABLE:
        return held(
            HoldReason.RESULT_UNKNOWN,
            "The readiness receipt records a reason this reader does not recognize. It is not "
            "treated as eligibility.",
        )
    state = ReadinessState(parsed.reason)
    if state in {ReadinessState.NO_VERIFIER_BOUND, ReadinessState.AWAITING_OUTCOME}:
        bound = state is ReadinessState.AWAITING_OUTCOME
        suffix = "Awaiting outcome" if bound else "No verifier bound"
        finding = f"{_plural(facts.pending, 'decision')} awaiting verification · {suffix}."
    else:
        finding = _READINESS_FINDINGS[state]
    basis = (
        f"{_plural(facts.verified_roots, 'verified root')} in "
        f"{_plural(facts.independent_families, 'independent family', 'independent families')}."
    )
    if facts.residual_groups is not None and facts.minimum_residual_groups is not None:
        basis += (
            f" {_plural(facts.residual_groups, 'residual group')} after simple repair · "
            f"threshold {facts.minimum_residual_groups}."
        )
    elif facts.verified_roots:
        basis += " Independent residual groups are unmeasured."
    if facts.augmentation_state == "not_enabled":
        basis += " Augmentation is not enabled."
    if state is ReadinessState.FIT_ELIGIBLE:
        basis += " A fit needs separate, authorized admission."
    basis += (
        f" Readiness receipt {facts.readiness_ref} · version {facts.version} · evidence "
        f"{facts.evidence_revision}."
    )
    return _projection(
        topic,
        state,
        as_of=as_of,
        finding=finding,
        basis=basis,
        action=parsed.next_action,
        owner=parsed.next_owner,
        action_text=f"{_READINESS_ACTIONS[parsed.next_action]} Responsible · {parsed.next_owner}.",
        receipts=(ref,),
        facts=facts,
    )


# ----- Model trace: "What did our model see and say?" (OPE-533 S07, contract K5, OPE-532) -------


class Fidelity(StrEnum):
    OBSERVED = "observed"
    RECONSTRUCTED = "reconstructed"
    PARTIAL = "partial"
    UNAVAILABLE = "unavailable"


class TraceState(StrEnum):
    OBSERVED_CAPTURE = "observed_capture"
    PARTIAL_CAPTURE = "partial_capture"
    COMMITTED_DECISION_ONLY = "committed_decision_only"
    UNAVAILABLE = "unavailable"


# The live decision feed records decision-time input as observed, reconstructed or unavailable.
_DECISION_INPUT_FIDELITY = frozenset(
    {Fidelity.OBSERVED, Fidelity.RECONSTRUCTED, Fidelity.UNAVAILABLE}
)
_FINALITY = frozenset({"final", "reopened", "withdrawn"})


@dataclass(frozen=True)
class CommittedDecision:
    """One committed decision revision read from the authorized live decision feed."""

    tenant: str
    workflow_ref: str
    engagement_epoch: str
    decision_id: str
    decision_revision: int
    decided_at: int | None
    action_ref: str
    model_ref: str | None
    input_ref: str | None
    input_fidelity: str
    source_receipt_ref: str


@dataclass(frozen=True)
class OriginalCapture:
    """An authorized capture of the model's request and response for one decision revision."""

    capture_ref: str
    decision_id: str
    decision_revision: int
    model_ref: str
    request_fidelity: str
    response_fidelity: str
    captured_at: int
    independently_observed: bool


@dataclass(frozen=True)
class LaterVerification:
    """A later independent outcome for the same decision revision. Never decision-time input."""

    outcome_ref: str
    decision_id: str
    decision_revision: int
    finality: str
    verified_action_ref: str | None
    recorded_at: int | None


@dataclass(frozen=True)
class TraceScope:
    tenant: str
    workflow_ref: str
    engagement_epoch: str
    decision_id: str
    # The decision revision the current reducer holds for `decision_id`.
    current_revision: int


@dataclass(frozen=True)
class TraceFacts:
    decision_id: str
    decision_revision: int
    decided_at: int | None
    committed_action_ref: str
    model_ref: str | None
    # One fidelity per boundary: the feed's decision-time input, then the captured model
    # request and response. Without an enabled capture both captured boundaries are unavailable.
    decision_input_fidelity: Fidelity
    request_fidelity: Fidelity
    response_fidelity: Fidelity
    model_output_attributed: bool
    capture_enabled: bool
    capture_ref: str | None
    capture_independent: bool | None
    verification: LaterVerification | None


def _fidelity(value: object, allowed: frozenset[Fidelity] = frozenset(Fidelity)) -> Fidelity:
    if not isinstance(value, str) or value not in allowed:
        raise _Invalid
    return Fidelity(value)


def _revision(value: object) -> int:
    revision = _count(value)
    if revision == 0:
        raise _Invalid
    return revision


def _validated_decision(decision: object, as_of: int) -> CommittedDecision:
    if not isinstance(decision, CommittedDecision):
        raise _Invalid
    for reference in (
        decision.tenant,
        decision.workflow_ref,
        decision.engagement_epoch,
        decision.decision_id,
        decision.action_ref,
        decision.source_receipt_ref,
    ):
        _reference(reference)
    _revision(decision.decision_revision)
    _optional_instant(decision.decided_at, as_of)
    _optional_reference(decision.model_ref)
    _optional_reference(decision.input_ref)
    fidelity = _fidelity(decision.input_fidelity, _DECISION_INPUT_FIDELITY)
    if (decision.input_ref is None) != (fidelity is Fidelity.UNAVAILABLE):
        raise _Invalid
    return decision


def _validated_capture(capture: object, as_of: int) -> OriginalCapture:
    if not isinstance(capture, OriginalCapture):
        raise _Invalid
    for reference in (capture.capture_ref, capture.decision_id, capture.model_ref):
        _reference(reference)
    _revision(capture.decision_revision)
    _fidelity(capture.request_fidelity)
    _fidelity(capture.response_fidelity)
    _instant(capture.captured_at, as_of)
    _flag(capture.independently_observed)
    return capture


def _validated_verification(verification: object, as_of: int) -> LaterVerification:
    if not isinstance(verification, LaterVerification) or verification.finality not in _FINALITY:
        raise _Invalid
    _reference(verification.outcome_ref)
    _reference(verification.decision_id)
    _revision(verification.decision_revision)
    _optional_instant(verification.recorded_at, as_of)
    if (verification.finality == "final") != (verification.verified_action_ref is not None):
        raise _Invalid
    _optional_reference(verification.verified_action_ref)
    return verification


def project_trace(
    decision: CommittedDecision | None,
    *,
    scope: TraceScope,
    as_of: int,
    capture: OriginalCapture | None = None,
    capture_enabled: bool = False,
    verification: LaterVerification | None = None,
) -> StatusProjection:
    """Show what the decision-time trace supports; attribute model output only when observed."""
    as_of = _clock(as_of)
    topic = StatusTopic.TRACE
    expected = (
        _scope_reference(scope.tenant, "tenant"),
        _scope_reference(scope.workflow_ref, "workflow"),
        _scope_reference(scope.engagement_epoch, "engagement epoch"),
        _scope_reference(scope.decision_id, "decision"),
    )
    if isinstance(scope.current_revision, bool) or not isinstance(scope.current_revision, int):
        raise ValueError("Invalid current decision revision for a trace projection scope.")
    unavailable = (
        "Decision-time model input and original model output are unavailable for this decision."
    )

    def held(
        reason: HoldReason,
        basis: str,
        *refs: ReceiptRef,
        state: TraceState = TraceState.UNAVAILABLE,
        finding: str = unavailable,
        facts: TraceFacts | None = None,
        action: str = "reconcile_decision_trace",
    ) -> StatusProjection:
        return _projection(
            topic,
            state,
            as_of=as_of,
            finding=finding,
            basis=basis,
            action=action,
            hold=reason,
            receipts=refs,
            facts=facts,
        )

    if decision is None:
        return held(
            HoldReason.RECEIPT_ABSENT,
            "No committed decision from the authorized decision feed is available here.",
            action="instrument_original_capture",
        )
    try:
        _validated_decision(decision, as_of)
        if capture is not None:
            _validated_capture(capture, as_of)
        if verification is not None:
            _validated_verification(verification, as_of)
    except _Invalid:
        return held(HoldReason.RECEIPT_INVALID, "The decision trace failed its integrity checks.")
    refs: tuple[ReceiptRef, ...] = (
        ReceiptRef(
            "committed_decision",
            decision.source_receipt_ref,
            f"{decision.decision_id}@{decision.decision_revision}",
            decision.decided_at,
        ),
    )
    found = (
        decision.tenant,
        decision.workflow_ref,
        decision.engagement_epoch,
        decision.decision_id,
    )
    if found != expected or decision.decision_revision > scope.current_revision:
        return held(
            HoldReason.SCOPE_MISMATCH,
            "The committed decision does not belong to the selected workflow decision.",
        )
    if decision.decision_revision < scope.current_revision:
        return held(
            HoldReason.RECEIPT_STALE,
            f"Decision revision {decision.decision_revision} was superseded by revision "
            f"{scope.current_revision}.",
            *refs,
        )
    revision = (decision.decision_id, decision.decision_revision)
    if capture is not None:
        if (
            not capture_enabled
            or (capture.decision_id, capture.decision_revision) != revision
            or capture.model_ref != decision.model_ref
        ):
            return held(
                HoldReason.SCOPE_MISMATCH,
                "The original capture is not an enabled capture of this decision revision and "
                "model.",
                *refs,
            )
        refs += (
            ReceiptRef(
                "original_capture",
                capture.capture_ref,
                f"{capture.decision_id}@{capture.decision_revision}",
                capture.captured_at,
            ),
        )
    if verification is not None:
        if (verification.decision_id, verification.decision_revision) != revision:
            return held(
                HoldReason.SCOPE_MISMATCH,
                "The later verification targets a different decision revision.",
                *refs,
            )
        refs += (
            ReceiptRef(
                "independent_outcome",
                verification.outcome_ref,
                f"{verification.decision_id}@{verification.decision_revision}",
                verification.recorded_at,
            ),
        )
    decision_input = Fidelity(decision.input_fidelity)
    request = response = Fidelity.UNAVAILABLE
    observed = False
    if capture is not None:
        request = Fidelity(capture.request_fidelity)
        response = Fidelity(capture.response_fidelity)
        observed = (
            capture.independently_observed
            and request is Fidelity.OBSERVED
            and response is Fidelity.OBSERVED
        )
    facts = TraceFacts(
        decision.decision_id,
        decision.decision_revision,
        decision.decided_at,
        decision.action_ref,
        decision.model_ref,
        decision_input,
        request,
        response,
        observed,
        capture_enabled,
        capture.capture_ref if capture else None,
        capture.independently_observed if capture else None,
        verification,
    )
    when = f" at {_utc(decision.decided_at)}" if decision.decided_at is not None else ""
    model = f" by model {decision.model_ref}" if decision.model_ref else ""
    committed = (
        f"Committed action {decision.action_ref}{model}{when} · decision {decision.decision_id} "
        f"revision {decision.decision_revision}."
    )
    later = ""
    if verification is not None:
        later = (
            f" Later verification, recorded separately: {verification.finality}"
            + (
                f" as {verification.verified_action_ref}"
                if verification.verified_action_ref
                else ""
            )
            + f" · outcome {verification.outcome_ref}."
        )
    boundaries = (
        f" Decision-time input {decision_input.value} · model request {request.value} · "
        f"model output {response.value}."
    )
    if observed:
        assert capture is not None
        return _projection(
            topic,
            TraceState.OBSERVED_CAPTURE,
            as_of=as_of,
            finding="The original model request and response were independently observed.",
            basis=f"{committed}{boundaries} Capture {capture.capture_ref}.{later}",
            action="open_original_capture",
            receipts=refs,
            facts=facts,
        )
    if capture is None:
        note = (
            " Original capture is not enabled."
            if not capture_enabled
            else " No original capture is recorded."
        )
        return held(
            HoldReason.ORIGINAL_UNAVAILABLE,
            f"{committed}{boundaries}{note} Only the observed committed action is "
            f"available; no model output is attributed.{later}",
            *refs,
            state=TraceState.COMMITTED_DECISION_ONLY,
            finding="The committed decision is available. The original model output is not.",
            facts=facts,
            action="instrument_original_capture",
        )
    independent = "" if capture.independently_observed else " It is not independently observed."
    return held(
        HoldReason.ORIGINAL_NOT_OBSERVED,
        f"{committed}{boundaries} Capture {capture.capture_ref}.{independent} A reconstructed "
        f"or partial original supports only the committed action; no model output is "
        f"attributed.{later}",
        *refs,
        state=TraceState.PARTIAL_CAPTURE,
        finding="The original capture is partial. No model output is attributed.",
        facts=facts,
        action="instrument_original_capture",
    )


# ----- Delivery: "Did it ship?" (OPE-533 S14, release outbox, OPE-415/OPE-442) ------------------


class DeliveryState(StrEnum):
    DELIVERED = "delivered"
    NOT_RELEASED = "not_released"
    NO_RELEASE = "no_release"
    APPROVAL_PENDING = "approval_pending"
    QUEUED = "queued"
    UNCONFIRMED = "unconfirmed"
    UNAVAILABLE = "unavailable"


class ReleaseStatusLike(Protocol):
    """The release outbox's status for one approved release (release_outbox.ReleaseStatus)."""

    @property
    def reference(self) -> str: ...

    @property
    def destination_ref(self) -> str: ...

    @property
    def stage(self) -> str: ...

    @property
    def claimed_at(self) -> int | None: ...

    @property
    def last_checked_at(self) -> int | None: ...

    @property
    def receiver_receipt_ref(self) -> str | None: ...

    @property
    def received_at(self) -> int | None: ...


_DECLINED = frozenset(
    {"changes_requested", "release_changes_requested", "readout_changes_requested"}
)
_REQUESTED = frozenset({"staged", "accepted"})
_APPROVAL_STAGES = (
    _DECLINED
    | _REQUESTED
    | {
        "approved",
        "failed",
        "uncertain",
        "expired",
        "approval_unresolved",
    }
)


@dataclass(frozen=True)
class DeliveryFacts:
    destination_ref: str | None
    approval_stage: str | None = None
    approval_ref: str | None = None
    approval_recorded_at: int | None = None
    release_ref: str | None = None
    release_stage: str | None = None
    last_checked_at: int | None = None
    receiver_receipt_ref: str | None = None
    received_at: int | None = None


def _release_facts(release: ReleaseStatusLike, as_of: int) -> tuple[str, str, str, int | None]:
    try:
        reference = _reference(release.reference)
        destination = _reference(release.destination_ref)
        stage = release.stage
        if not isinstance(stage, str):
            raise _Invalid
        _optional_instant(release.claimed_at, as_of)
        checked = _optional_instant(release.last_checked_at, as_of)
        _optional_reference(release.receiver_receipt_ref)
        _optional_instant(release.received_at, as_of)
    except AttributeError:
        raise _Invalid from None
    return reference, destination, stage, checked


def project_delivery(
    *,
    destination_ref: str | None,
    approval: ApprovalEvidence | None,
    as_of: int,
    release: ReleaseStatusLike | None = None,
    approved_at: int | None = None,
    expected_release_ref: str | None = None,
) -> StatusProjection:
    """Report the selected result's handoff only as far as its receipts establish it."""
    as_of = _clock(as_of)
    topic = StatusTopic.DELIVERY
    unconfirmed = "Handoff delivery is unconfirmed."

    def held(
        reason: HoldReason,
        state: DeliveryState,
        finding: str,
        basis: str,
        action: str,
        *refs: ReceiptRef,
        facts: DeliveryFacts | None = None,
    ) -> StatusProjection:
        return _projection(
            topic,
            state,
            as_of=as_of,
            finding=finding,
            basis=basis,
            action=action,
            hold=reason,
            receipts=refs,
            facts=facts,
        )

    if destination_ref is None:
        # Current settings authorize no release. Earlier status is not disclosed.
        return held(
            HoldReason.NO_DESTINATION,
            DeliveryState.NO_RELEASE,
            "No release is authorized by the current configuration.",
            "This result has no configured handoff destination. Earlier delivery is unconfirmed.",
            "check_handoff_settings",
            facts=DeliveryFacts(None),
        )
    destination_ref = _scope_reference(destination_ref, "destination")
    if expected_release_ref is not None:
        expected_release_ref = _scope_reference(expected_release_ref, "release")
    if approval is None:
        return held(
            HoldReason.RECEIPT_ABSENT,
            DeliveryState.UNAVAILABLE,
            unconfirmed,
            "No verified approval receipt is available here for this result.",
            "check_release_approval",
            facts=DeliveryFacts(destination_ref),
        )
    try:
        approval_ref = _reference(approval.reference)
        approval_time = _optional_instant(approval.recorded_at, as_of)
        if approved_at is not None:
            _instant(approved_at, as_of)
        if not isinstance(approval.stage, str):
            raise _Invalid
        known = approval.stage in _APPROVAL_STAGES
    except _Invalid:
        return held(
            HoldReason.RECEIPT_INVALID,
            DeliveryState.UNAVAILABLE,
            unconfirmed,
            "The approval receipt failed its integrity checks.",
            "reconcile_approval",
            facts=DeliveryFacts(destination_ref),
        )
    approval_receipt = ReceiptRef("approval", approval_ref, approval.stage, approval_time)
    facts = DeliveryFacts(destination_ref, approval.stage, approval_ref, approval_time)
    when = f" at {_utc(approval_time)}" if approval_time is not None else ""
    if not known:
        return held(
            HoldReason.RESULT_UNKNOWN,
            DeliveryState.UNCONFIRMED,
            unconfirmed,
            f"Approval evidence {approval_ref} records an unrecognized stage. It is not treated "
            "as approval.",
            "reconcile_approval",
            approval_receipt,
            facts=facts,
        )
    if approval.stage in _DECLINED:
        subject = "Readout" if approval.stage == "readout_changes_requested" else "Release"
        return _projection(
            topic,
            DeliveryState.NOT_RELEASED,
            as_of=as_of,
            finding=f"Not released. {subject} is held pending changes.",
            basis=f"Changes requested{when}. Approval evidence {approval_ref}. This revision was "
            "not approved for handoff.",
            action="address_review_decision",
            receipts=(approval_receipt,),
            facts=facts,
        )
    if approval.stage != "approved":
        reason, state, finding, action = {
            "expired": (
                HoldReason.APPROVAL_EXPIRED,
                DeliveryState.UNCONFIRMED,
                "Approval request expired.",
                "check_release_approval",
            ),
            "approval_unresolved": (
                HoldReason.RESULT_UNKNOWN,
                DeliveryState.UNCONFIRMED,
                "Approval outcome awaits reconciliation.",
                "reconcile_approval",
            ),
            "uncertain": (
                HoldReason.RESULT_UNKNOWN,
                DeliveryState.UNCONFIRMED,
                "Approval request transport is unconfirmed.",
                "reconcile_approval",
            ),
            "failed": (
                HoldReason.APPROVAL_REQUEST_FAILED,
                DeliveryState.APPROVAL_PENDING,
                "Approval request transport failed.",
                "check_release_approval",
            ),
        }.get(
            approval.stage,
            (
                HoldReason.APPROVAL_PENDING,
                DeliveryState.APPROVAL_PENDING,
                "Approval has not been recorded.",
                "check_release_approval",
            ),
        )
        return held(
            reason,
            state,
            finding,
            f"Approval evidence {approval_ref}{when} · stage {approval.stage}. Provider approval "
            "and transport never attest artifact handoff. Delivery remains unconfirmed.",
            action,
            approval_receipt,
            facts=facts,
        )
    if release is None:
        return held(
            HoldReason.RECEIPT_ABSENT,
            DeliveryState.UNCONFIRMED,
            f"Approval recorded. {unconfirmed}",
            f"Approval evidence {approval_ref}{when}. No release outbox receipt is available for "
            "this approval.",
            "reconcile_delivery",
            approval_receipt,
            facts=facts,
        )
    try:
        release_ref, release_destination, stage, checked = _release_facts(release, as_of)
    except _Invalid:
        return held(
            HoldReason.RECEIPT_INVALID,
            DeliveryState.UNCONFIRMED,
            f"Approval recorded. {unconfirmed}",
            "The release outbox receipt failed its integrity checks.",
            "reconcile_delivery",
            approval_receipt,
            facts=facts,
        )
    release_receipt = ReceiptRef("release_outbox", release_ref, stage, checked)
    facts = DeliveryFacts(
        destination_ref,
        approval.stage,
        approval_ref,
        approval_time,
        release_ref,
        stage,
        checked,
        release.receiver_receipt_ref,
        release.received_at,
    )
    refs = (approval_receipt, release_receipt)
    if release_destination != destination_ref or (
        expected_release_ref is not None and release_ref != expected_release_ref
    ):
        return held(
            HoldReason.SCOPE_MISMATCH,
            DeliveryState.UNCONFIRMED,
            f"Approval recorded. {unconfirmed}",
            "The release outbox receipt is for a different release or destination.",
            "reconcile_delivery",
            approval_receipt,
            facts=DeliveryFacts(destination_ref, approval.stage, approval_ref, approval_time),
        )
    checked_text = (
        f" Last checked {_utc(checked)}." if checked is not None else " No check time recorded."
    )
    if stage == "queued":
        return held(
            HoldReason.AWAITING_DELIVERY,
            DeliveryState.QUEUED,
            "Approval recorded. Awaiting delivery.",
            f"Release {release_ref} to destination {release_destination} is queued.{checked_text}",
            "reconcile_delivery",
            *refs,
            facts=facts,
        )
    if stage != "delivered":
        return held(
            HoldReason.RESULT_UNKNOWN,
            DeliveryState.UNCONFIRMED,
            f"Approval recorded. {unconfirmed}",
            f"Release {release_ref} to destination {release_destination} · stage {stage}. An "
            f"unknown receiver result is not delivery.{checked_text}",
            "reconcile_delivery",
            *refs,
            facts=facts,
        )
    receiver, received = release.receiver_receipt_ref, release.received_at
    if (
        receiver is None
        or received is None
        or checked is None
        or received > checked
        or (approved_at is not None and received < approved_at)
    ):
        return held(
            HoldReason.RECEIPT_INVALID,
            DeliveryState.UNCONFIRMED,
            f"Approval recorded. {unconfirmed}",
            "The delivered stage lacks a consistent receiver receipt.",
            "reconcile_delivery",
            *refs,
            facts=facts,
        )
    return _projection(
        topic,
        DeliveryState.DELIVERED,
        as_of=as_of,
        finding="Delivered to the selected destination.",
        basis=f"Release {release_ref} to destination {release_destination}. Receiver receipt "
        f"{receiver} at {_utc(received)}.{checked_text} Approval evidence {approval_ref}{when}. "
        "Delivery is not recipient adoption.",
        action="replay_artifact",
        receipts=(*refs, ReceiptRef("receiver", receiver, release_ref, received)),
        facts=facts,
    )


# ----- Weekly live evidence: "No errors this week?" (OPE-533 S16, OPE-514, OPE-542) ------------


class LiveWindowState(StrEnum):
    QUIET = "quiet_window"
    VERIFIED = "verified_window"
    NO_VERIFIED_EVIDENCE = "no_verified_evidence"
    NO_VERIFIED_AUTOMATIC = "no_verified_automatic"
    STALE_FEED = "stale_feed"
    INCOMPLETE = "incomplete_coverage"
    NO_FEED = "no_feed"
    UNAVAILABLE = "unavailable"


# Decided under owner delegation, overnight sprint 2026-11-28 (reversible by the owner): a feed
# is stale for a window when its covered time or its last successful sync is more than this far
# behind the end of the window (or the clock, if earlier). Every answer states the covered
# window and the last sync. A binding's own declared cadence may pass a tighter bound.
DEFAULT_FEED_LAG_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class SourceRead:
    """One acknowledged source page from live_evidence.LiveEvidence.read_receipts()."""

    receipt_ref: str
    polled_at: int
    interval_start: int | None
    interval_end: int | None
    coverage_through: int | None
    complete: bool
    row_count: int

    @classmethod
    def from_receipt(cls, receipt: Mapping[str, Any]) -> SourceRead:
        try:
            return cls(
                receipt["receipt_ref"],
                receipt["polled_at"],
                receipt["interval_start"],
                receipt["interval_end"],
                receipt["coverage_through"],
                receipt["complete"],
                receipt["row_count"],
            )
        except (KeyError, TypeError):
            raise ValueError("Not a live source-read receipt.") from None


@dataclass(frozen=True)
class LiveWindowReceipt:
    """Joined decision and outcome coverage for one requested window (OPE-514/OPE-518)."""

    receipt_ref: str
    workflow_ref: str
    engagement_epoch: str
    dataset_ref: str
    evidence_revision: str
    window_start: int
    window_end: int
    decision_coverage_through: int
    # None when no independent outcome feed covers the window.
    outcome_coverage_through: int | None
    complete: bool
    last_synced_at: int
    decisions: int
    verified: int
    pending: int
    disputed: int
    verified_automatic: int
    wrong_automatic: int
    recorded_at: int


@dataclass(frozen=True)
class LiveWindowScope:
    workflow_ref: str
    engagement_epoch: str
    dataset_ref: str
    evidence_revision: str
    window_start: int
    window_end: int


@dataclass(frozen=True)
class LiveWindowFacts:
    window_start: int
    window_end: int
    covered_through: int | None
    last_synced_at: int | None
    verifier_bound: bool
    decisions: int | None = None
    verified: int | None = None
    pending: int | None = None
    disputed: int | None = None
    verified_automatic: int | None = None
    wrong_automatic: int | None = None


def _validated_read(read: object, as_of: int) -> SourceRead:
    if not isinstance(read, SourceRead):
        raise _Invalid
    _reference(read.receipt_ref)
    _instant(read.polled_at, as_of)
    start, end = _optional_count(read.interval_start), _optional_count(read.interval_end)
    coverage = _optional_count(read.coverage_through)
    _flag(read.complete)
    _count(read.row_count)
    if (start is None) != (end is None) or (start is not None and end is not None and start > end):
        raise _Invalid
    if coverage is not None and (start is None or end is None or not start <= coverage <= end):
        raise _Invalid
    return read


def _validated_window(receipt: object, as_of: int) -> LiveWindowReceipt:
    if not isinstance(receipt, LiveWindowReceipt):
        raise _Invalid
    for reference in (
        receipt.receipt_ref,
        receipt.workflow_ref,
        receipt.engagement_epoch,
        receipt.dataset_ref,
        receipt.evidence_revision,
    ):
        _reference(reference)
    start, end = _count(receipt.window_start), _count(receipt.window_end)
    _count(receipt.decision_coverage_through)
    _optional_count(receipt.outcome_coverage_through)
    _flag(receipt.complete)
    _instant(receipt.last_synced_at, as_of)
    _instant(receipt.recorded_at, as_of)
    decisions, verified, pending, disputed, automatic, wrong = (
        _count(value)
        for value in (
            receipt.decisions,
            receipt.verified,
            receipt.pending,
            receipt.disputed,
            receipt.verified_automatic,
            receipt.wrong_automatic,
        )
    )
    if (
        start >= end
        or decisions != verified + pending + disputed
        or not wrong <= automatic <= verified
    ):
        raise _Invalid
    return receipt


def project_live_window(
    receipt: LiveWindowReceipt | None,
    *,
    scope: LiveWindowScope,
    decision_feed_bound: bool,
    verifier_bound: bool,
    as_of: int,
    latest_read: SourceRead | Mapping[str, Any] | None = None,
    can_bind_verifier: bool = False,
    max_feed_lag: int = DEFAULT_FEED_LAG_SECONDS,
) -> StatusProjection:
    """Answer a window error question only from current, complete, joined coverage."""
    as_of = _clock(as_of)
    topic = StatusTopic.LIVE_ERRORS
    expected = (
        _scope_reference(scope.workflow_ref, "workflow"),
        _scope_reference(scope.engagement_epoch, "engagement epoch"),
        _scope_reference(scope.dataset_ref, "dataset"),
    )
    current_evidence = _scope_reference(scope.evidence_revision, "evidence revision")
    try:
        start, requested_end = _count(scope.window_start), _count(scope.window_end)
        lag = _count(max_feed_lag)
        _flag(decision_feed_bound)
        _flag(verifier_bound)
        _flag(can_bind_verifier)
    except _Invalid:
        raise ValueError(
            "Invalid window, feed state or lag for a live window projection."
        ) from None
    end = min(requested_end, as_of)
    if lag == 0 or start >= end:
        raise ValueError("A live window projection needs a started window and a positive lag.")
    window = f"{_utc(start)} to {_utc(requested_end)}"
    unavailable = (
        "Verified live errors and the denominator for the requested window are unavailable."
    )
    bind = "bind_verifier" if can_bind_verifier else "ask_data_custodian_to_bind_verifier"

    def held(
        reason: HoldReason,
        state: LiveWindowState,
        basis: str,
        action: str,
        *refs: ReceiptRef,
        finding: str = unavailable,
        facts: LiveWindowFacts | None = None,
    ) -> StatusProjection:
        return _projection(
            topic,
            state,
            as_of=as_of,
            finding=finding,
            basis=basis,
            action=action,
            hold=reason,
            receipts=refs,
            facts=facts or LiveWindowFacts(start, requested_end, None, None, verifier_bound),
        )

    if not decision_feed_bound:
        return held(
            HoldReason.NO_FEED,
            LiveWindowState.NO_FEED,
            f"No authorized live decision feed is connected for {window}.",
            "connect_decision_feed",
            finding="No live decision feed is connected.",
        )
    if receipt is None:
        read: SourceRead | None = None
        if latest_read is not None:
            try:
                read = _validated_read(
                    latest_read
                    if isinstance(latest_read, SourceRead)
                    else SourceRead.from_receipt(latest_read),
                    as_of,
                )
            except (_Invalid, ValueError):
                return held(
                    HoldReason.RECEIPT_INVALID,
                    LiveWindowState.UNAVAILABLE,
                    "The latest source-read receipt failed its integrity checks.",
                    "check_feed_sync",
                )
        refs: tuple[ReceiptRef, ...] = ()
        read_facts: LiveWindowFacts | None = None
        if read is not None:
            refs = (ReceiptRef("live_source_read", read.receipt_ref, None, read.polled_at),)
            covered = read.coverage_through
            read_facts = LiveWindowFacts(
                start, requested_end, covered, read.polled_at, verifier_bound
            )
            # A page with unknown covered time, including a completed empty page, never
            # establishes that the window is current.
            if (
                read.polled_at < end - lag
                or covered is None
                or covered < end - lag
                or covered <= start
            ):
                through = (
                    f"Coverage through {_utc(covered)}"
                    if covered is not None
                    else "Covered time is unknown"
                )
                return held(
                    HoldReason.FEED_STALE,
                    LiveWindowState.STALE_FEED,
                    f"{through}. Last successful sync {_utc(read.polled_at)}. The feed is "
                    f"behind the requested window {window}.",
                    "check_feed_sync",
                    *refs,
                    finding="The live feed is stale for the requested window.",
                    facts=read_facts,
                )
        synced = f" Last successful sync {_utc(read.polled_at)}." if read is not None else ""
        return held(
            HoldReason.RECEIPT_ABSENT,
            LiveWindowState.UNAVAILABLE,
            f"No linked decision and outcome coverage receipt exists for {window}. Totals across "
            f"all observed dates are not a weekly result.{synced}",
            "refresh_window_coverage" if verifier_bound else bind,
            *refs,
            facts=read_facts,
        )
    try:
        _validated_window(receipt, as_of)
    except _Invalid:
        return held(
            HoldReason.RECEIPT_INVALID,
            LiveWindowState.UNAVAILABLE,
            "The window coverage receipt failed its integrity checks.",
            "refresh_window_coverage",
        )
    ref = ReceiptRef(
        "live_window", receipt.receipt_ref, receipt.evidence_revision, receipt.recorded_at
    )
    found = (receipt.workflow_ref, receipt.engagement_epoch, receipt.dataset_ref)
    if found != expected or (receipt.window_start, receipt.window_end) != (start, requested_end):
        return held(
            HoldReason.SCOPE_MISMATCH,
            LiveWindowState.UNAVAILABLE,
            "The window coverage receipt is for a different workflow, engagement, dataset or "
            "window.",
            "refresh_window_coverage",
        )
    if receipt.evidence_revision != current_evidence:
        return held(
            HoldReason.RECEIPT_STALE,
            LiveWindowState.UNAVAILABLE,
            f"The window coverage receipt was computed for evidence {receipt.evidence_revision}. "
            f"Current evidence is {current_evidence}.",
            "refresh_window_coverage",
            ref,
        )
    outcomes = receipt.outcome_coverage_through
    decisions_through = receipt.decision_coverage_through
    covered = (
        decisions_through
        if outcomes is None or not verifier_bound
        else min(decisions_through, outcomes)
    )
    facts = LiveWindowFacts(
        start,
        requested_end,
        covered,
        receipt.last_synced_at,
        verifier_bound,
        receipt.decisions,
        receipt.verified,
        receipt.pending,
        receipt.disputed,
        receipt.verified_automatic,
        receipt.wrong_automatic,
    )
    synced = f"Last successful sync {_utc(receipt.last_synced_at)}."
    actual = (
        f"Covered {_utc(start)} to {_utc(min(covered, requested_end))}. {synced}"
        if covered > start
        else f"No part of the requested window is covered. {synced}"
    )
    # A feed is current only when it covers part of the window and its covered time and last
    # sync are both within the lag of the window's end (or the clock, if earlier).
    if (
        receipt.last_synced_at < end - lag
        or decisions_through < end - lag
        or decisions_through <= start
    ):
        return held(
            HoldReason.FEED_STALE,
            LiveWindowState.STALE_FEED,
            f"{actual} The decision feed is behind the requested window {window}.",
            "check_feed_sync",
            ref,
            finding="The live feed is stale for the requested window.",
            facts=facts,
        )
    if not receipt.complete:
        return held(
            HoldReason.COVERAGE_INCOMPLETE,
            LiveWindowState.INCOMPLETE,
            f"{actual} The source reported partial coverage for the window.",
            "check_feed_sync",
            ref,
            facts=facts,
        )
    counts = (
        f"{_plural(receipt.decisions, 'committed decision')} · {receipt.verified} verified · "
        f"{receipt.pending} pending · {receipt.disputed} disputed."
    )
    if receipt.decisions == 0:
        return _projection(
            topic,
            LiveWindowState.QUIET,
            as_of=as_of,
            finding="Quiet window. No committed decisions were recorded.",
            basis=f"{actual} {counts} A quiet window is not an accuracy result.",
            action="await_next_sync",
            receipts=(ref,),
            facts=facts,
        )
    if verifier_bound and (outcomes is None or outcomes < end - lag or outcomes <= start):
        return held(
            HoldReason.FEED_STALE,
            LiveWindowState.STALE_FEED,
            f"{actual} {counts} The independent outcome feed is behind the requested window.",
            "check_feed_sync",
            ref,
            finding="The outcome feed is stale for the requested window.",
            facts=facts,
        )
    if receipt.verified == 0:
        if receipt.pending:
            reason, action, detail = (
                (HoldReason.AWAITING_OUTCOME, "verify_outcome", "A verifier is bound and awaits")
                if verifier_bound
                else (HoldReason.VERIFIER_NOT_BOUND, bind, "No verifier is bound for")
            )
            basis = f"{actual} {counts} {detail} the pending outcomes."
        else:
            reason, action = HoldReason.DISPUTED_OUTCOMES, "verify_outcome"
            basis = f"{actual} {counts} Every decision in the window is disputed."
        return held(
            reason,
            LiveWindowState.NO_VERIFIED_EVIDENCE,
            basis,
            action,
            ref,
            finding="No verified evidence for the window. Errors are unknown.",
            facts=facts,
        )
    if receipt.verified_automatic == 0:
        return held(
            HoldReason.NO_VERIFIED_AUTOMATIC,
            LiveWindowState.NO_VERIFIED_AUTOMATIC,
            f"{actual} {counts} No verified automatic decision exists, so the wrong automatic "
            "rate has no denominator.",
            "inspect_live_evidence",
            ref,
            finding="Wrong automatic decisions are unavailable for the window.",
            facts=facts,
        )
    pending = (
        f" {_plural(receipt.pending, 'pending decision')} not counted as correct."
        if receipt.pending
        else ""
    )
    return _projection(
        topic,
        LiveWindowState.VERIFIED,
        as_of=as_of,
        finding=f"{receipt.wrong_automatic} of {receipt.verified_automatic} verified automatic "
        "decisions were wrong in the covered window.",
        basis=f"{actual} {counts}{pending}",
        action="inspect_live_evidence",
        receipts=(ref,),
        facts=facts,
    )


ProjectionFacts = TimingFacts | ReadinessFacts | TraceFacts | DeliveryFacts | LiveWindowFacts
