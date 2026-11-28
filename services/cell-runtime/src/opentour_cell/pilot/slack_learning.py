"""Private Slack projections of customer-worker learning plans and receipts."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal, Protocol

Control = Literal["pause", "resume", "stop"]
Operation = Literal["benchmark_only", "evaluate_and_improve"]


class ScanService(Protocol):
    def status(self, actor: str) -> dict[str, Any]: ...

    def act(self, actor: str, action: str, revision: str, key: str) -> dict[str, Any]: ...


@dataclass(frozen=True)
class ScanStatus:
    state: str
    revision: str

    @classmethod
    def from_status(cls, result: dict[str, Any]) -> ScanStatus:
        state, revision = result["state"], result["revision"]
        if (
            result["can_control"] is not True
            or state not in {"ready", "watching", "scanning", "parked", "held"}
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{64}", revision)
        ):
            raise ValueError("Scan access unavailable.")
        # The private scanner can return source metadata. Only the state and its fence leave it.
        return cls(state, revision)

    def controls(self) -> tuple[str, ...]:
        if self.state in {"ready", "parked"}:
            return ("start_scan",)
        if self.state in {"watching", "scanning"}:
            return ("another_scan", "done_for_now")
        return ()


@dataclass(frozen=True)
class LearningScope:
    organization: str
    engagement: str
    workspace: str
    actor: str
    workflow: str
    dataset: str
    dataset_revision: str


@dataclass(frozen=True)
class LearningPlan:
    scope: LearningScope
    plan_ref: str
    model_route: str
    benchmark_ref: str
    mandate_ref: str | None
    max_fits: int
    spend_cap_cents: int
    schedule: Literal["paused", "enabled", "off"]
    can_benchmark: bool
    can_improve: bool
    current_run: str | None
    approval_revision: str = ""


def bind_approval(terms: list[object], revision: str) -> list[object]:
    """Pin a separately signed fit reservation while retaining legacy decision receipts."""
    return [*terms, revision] if revision else terms


@dataclass(frozen=True)
class LearningStatus:
    run_ref: str
    state: str
    can_cancel: bool
    revision: str = ""
    completed_steps: int | None = None
    total_steps: int | None = None
    checkpoint_step: int | None = None
    active_seconds: int | None = None
    stale: bool = False
    can_pause: bool = False
    can_resume: bool = False
    can_stop: bool = False

    def controls(self) -> tuple[Control, ...]:
        if not self.revision or self.stale:
            return ()
        actions: list[Control] = []
        if self.can_pause and self.state == "running":
            actions.append("pause")
        if self.can_resume and self.state == "paused" and self.checkpoint_step is not None:
            actions.append("resume")
        if self.can_stop and self.state in {
            "queued",
            "running",
            "paused",
            "pausing",
            "resuming",
            "held",
        }:
            actions.append("stop")
        return tuple(actions)

    def validate(self) -> None:
        flags = (self.can_cancel, self.stale, self.can_pause, self.can_resume, self.can_stop)
        measures = (
            self.completed_steps,
            self.total_steps,
            self.checkpoint_step,
            self.active_seconds,
        )
        if (
            self.state not in STATE_COPY
            or any(type(flag) is not bool for flag in flags)
            or not isinstance(self.revision, str)
            or any(n is not None and (type(n) is not int or n < 0) for n in measures)
            or (
                self.completed_steps is not None
                and self.total_steps is not None
                and self.completed_steps > self.total_steps
            )
            or (
                self.checkpoint_step is not None
                and (self.completed_steps is None or self.checkpoint_step > self.completed_steps)
            )
        ):
            raise ValueError("Invalid learning status.")


class LearningService(Protocol):
    """All methods must authorize the current scope on the worker at the point of use."""

    def plan(self, scope: LearningScope) -> LearningPlan | None: ...

    def start(
        self, scope: LearningScope, plan_ref: str, operation: Operation, key: str
    ) -> LearningStatus: ...

    def status(self, scope: LearningScope, run_ref: str) -> LearningStatus: ...

    def cancel(self, scope: LearningScope, run_ref: str) -> LearningStatus: ...

    def control(
        self, scope: LearningScope, run_ref: str, action: Control, revision: str, key: str
    ) -> LearningStatus: ...


STATE_COPY = {
    "held": "Held · Ask the workflow owner to check eligibility.",
    "benchmark_only": "Benchmark only · No fit was requested.",
    "no_viable_fit": "No viable fit · Review the benchmark and simpler repair.",
    "queued": "Queued · Waiting for the worker.",
    "running": "Running · Check back for measured results.",
    "pausing": "Pause requested · Waiting for the trainer to confirm its checkpoint.",
    "paused": "Paused · Resume continues this run with its existing budget.",
    "resuming": "Resume requested · Waiting for the trainer to confirm it is running.",
    "stopping": "Stop requested · Waiting for the trainer to confirm it has stopped.",
    "stopped": "Stopped · Watching continues.",
    "expired": "Expired · The trainer reached its deadline.",
    "verifying": "Verifying · The worker is checking the result.",
    "review_ready": "Ready for review · Open the private reviewer inbox.",
    "completed": "Completed · Review the recorded result.",
    "refused": "Refused · Ask the owner to review the current grant.",
    "failed": "Failed · Ask the workflow owner to inspect the worker receipt.",
    "cancellation_pending": "Cancellation pending · Awaiting worker confirmation.",
    "canceled": "Canceled · The worker confirmed cancellation.",
}
