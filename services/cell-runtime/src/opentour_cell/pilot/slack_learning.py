"""Private Slack projections of customer-worker learning plans and receipts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

Operation = Literal["benchmark_only", "evaluate_and_improve"]


@dataclass(frozen=True)
class LearningScope:
    organization: str
    engagement: str
    workspace: str
    actor: str
    workflow: str


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


@dataclass(frozen=True)
class LearningStatus:
    run_ref: str
    state: str
    can_cancel: bool


class LearningService(Protocol):
    """All methods must authorize the current scope on the worker at the point of use."""

    def plan(self, scope: LearningScope) -> LearningPlan | None: ...

    def start(
        self, scope: LearningScope, plan_ref: str, operation: Operation, key: str
    ) -> LearningStatus: ...

    def status(self, scope: LearningScope, run_ref: str) -> LearningStatus: ...

    def cancel(self, scope: LearningScope, run_ref: str) -> LearningStatus: ...


STATE_COPY = {
    "held": "Held · Ask the workflow owner to check eligibility.",
    "benchmark_only": "Benchmark only · No fit was requested.",
    "no_viable_fit": "No viable fit · Review the benchmark and simpler repair.",
    "queued": "Queued · Waiting for the worker.",
    "running": "Running · Check back for measured results.",
    "verifying": "Verifying · The worker is checking the result.",
    "review_ready": "Ready for review · Open the private reviewer inbox.",
    "completed": "Completed · Review the recorded result.",
    "refused": "Refused · Ask the owner to review the current grant.",
    "failed": "Failed · Ask the workflow owner to inspect the worker receipt.",
    "cancellation_pending": "Cancellation pending · Awaiting worker confirmation.",
    "canceled": "Canceled · The worker confirmed cancellation.",
}
