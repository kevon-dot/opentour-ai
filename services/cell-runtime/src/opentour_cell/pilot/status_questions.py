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


# Match complete questions. Counts, names and other asserted facts in free text
# never populate the answer or select the result.
_QUESTIONS = {
    StatusTopic.TIMING: r"how much faster (?:is|was) (?:it|the fit|training)",
    StatusTopic.TRAINING: (
        r"why (?:didn't|did not) (?:it|the model) train" r"|is training worth it yet"
    ),
    StatusTopic.TRACE: r"what did (?:our|the) model see and say",
    StatusTopic.DELIVERY: r"did (?:it|the result) ship",
    StatusTopic.LIVE_ERRORS: r"(?:no errors|(?:were there|any) (?:any )?errors) this week",
}


def resolve_status(prompt: str) -> StatusTopic | None:
    question = " ".join(prompt.casefold().replace("’", "'").split()).removesuffix("?").strip()
    return next(
        (topic for topic, pattern in _QUESTIONS.items() if re.fullmatch(pattern, question)), None
    )


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
            "Verified live errors and the live denominator for this week are unavailable.",
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
