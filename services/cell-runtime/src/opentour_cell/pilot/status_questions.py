"""Read-only questions about the evidence available for one selected frozen revision."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


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
    stage = {
        "approved": "Approval recorded",
        "changes_requested": "Changes requested",
        "staged": "Approval request staged",
        "accepted": "Approval request transport acknowledged",
        "failed": "Approval request transport failed",
        "uncertain": "Approval request transport unconfirmed",
        "expired": "Approval request expired",
    }[approval.stage]
    return StatusAnswer(
        "Handoff delivery is unconfirmed.",
        f"{stage}{time}. Intended recipient {audit['recipient']}. "
        f"Approval evidence {approval.reference}. "
        "A receiver readback for the selected revision is unavailable here.",
        "The release owner can reconcile the outbox with the intended recipient.",
    )
