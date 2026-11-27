"""Read a private proposal index under independent, current workflow permissions."""

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Literal, Protocol

from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope, WorkflowBenchmarks

Group = Literal["needs_you", "waiting", "recent"]
Action = Literal["open_review", "open_status", "open_receipt"]


@dataclass(frozen=True)
class WorkflowLabel:
    """Owner-approved naming metadata for an admitted workflow."""

    scope: BenchmarkScope
    owner_ref: str
    revision: str
    label: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class ProposalAccess:
    """One actor's independent proposal visibility and optional navigation right."""

    scope: BenchmarkScope
    actor_ref: str
    grant_revision: str
    proposal_ref: str
    proposal_revision: str
    state: Literal["pending", "approved", "changes_requested", "withdrawn", "expired"]
    recorded_at: int
    deadline: int | None
    visible_until: int
    action: Action | None
    action_until: int | None


@dataclass(frozen=True)
class IndexScope:
    actor: IntakePrincipal
    epoch: str
    membership_revision: str
    valid_until: int
    labels: tuple[WorkflowLabel, ...]
    proposals: tuple[ProposalAccess, ...]


class IndexAuthority(Protocol):
    def guard(self, actor: IntakePrincipal) -> AbstractContextManager[IndexScope]:
        """Verify index membership, owner labels and separate proposal grants.

        An unassigned eligible actor gets an empty scope. Missing index rights
        must deny. Implementors must revalidate current authority at guard exit.
        """
        ...


@dataclass(frozen=True)
class IndexRow:
    workflow_ref: str
    label: str
    aliases: tuple[str, ...]
    proposal_ref: str
    proposal_revision: str
    state: str
    deadline: int | None
    recorded_at: int
    group: Group
    action: Action | None
    selection: str | None


@dataclass(frozen=True)
class IndexPage:
    rows: tuple[IndexRow, ...]
    next_cursor: str | None


def _deny() -> None:
    raise IntakeError("workflow_index_unavailable")


class WorkflowIndex:
    """No default authority, listing by filename, or action execution."""

    def __init__(
        self,
        benchmarks: WorkflowBenchmarks,
        authority: IndexAuthority,
        *,
        clock: Callable[[], float] = time.time,
    ):
        self.benchmarks = benchmarks
        self.authority = authority
        self.clock = clock

    def _rows(self, actor: IntakePrincipal, scope: IndexScope) -> tuple[IndexRow, ...]:
        now = self.clock()
        if scope.actor != actor or now >= scope.valid_until:
            _deny()
        labels = {}
        for approved in scope.labels:
            if (
                approved.scope.tenant != actor.tenant
                or approved.scope.engagement_epoch != scope.epoch
            ):
                continue
            if approved.scope in labels:
                _deny()
            labels[approved.scope] = approved
        next_expiry = scope.valid_until
        rows = []
        seen = set()
        for grant in scope.proposals:
            label = labels.get(grant.scope)
            if label is None or grant.actor_ref != actor.actor or now >= grant.visible_until:
                continue
            if not self.benchmarks.observed_workflow(grant.scope):
                continue
            for expiry in (grant.visible_until, grant.deadline, grant.action_until):
                if expiry is not None and now < expiry:
                    next_expiry = min(next_expiry, expiry)
            key = (grant.scope.workflow_ref, grant.proposal_ref)
            if key in seen:
                _deny()
            seen.add(key)
            if grant.state not in {
                "pending",
                "approved",
                "changes_requested",
                "withdrawn",
                "expired",
            } or grant.action not in {None, "open_review", "open_status", "open_receipt"}:
                _deny()
            state = grant.state
            if state == "pending" and grant.deadline is None:
                _deny()
            if state == "pending" and grant.deadline is not None and now >= grant.deadline:
                state = "expired"
            action = grant.action if grant.action_until and now < grant.action_until else None
            if state != "pending" and action == "open_review":
                action = None
            if state == "pending" and action == "open_receipt":
                action = None
            group: Group = "recent"
            if state == "pending":
                group = "needs_you" if action == "open_review" else "waiting"
            pin = None
            if action:
                pin = digest(
                    [
                        actor.tenant,
                        actor.actor,
                        scope.epoch,
                        scope.membership_revision,
                        *key,
                        label.owner_ref,
                        label.revision,
                        grant.grant_revision,
                        grant.proposal_revision,
                        state,
                        action,
                        label.label,
                        label.aliases,
                        grant.deadline,
                        grant.visible_until,
                        grant.action_until,
                    ]
                )
            rows.append(
                IndexRow(
                    grant.scope.workflow_ref,
                    label.label,
                    label.aliases,
                    grant.proposal_ref,
                    grant.proposal_revision,
                    state,
                    grant.deadline,
                    grant.recorded_at,
                    group,
                    action,
                    pin,
                )
            )
        groups = {"needs_you": 0, "waiting": 1, "recent": 2}
        rows.sort(
            key=lambda row: (
                groups[row.group],
                row.deadline
                if row.group == "needs_you" and row.deadline is not None
                else -row.recorded_at,
                row.workflow_ref,
                row.proposal_ref,
            )
        )
        if self.clock() >= next_expiry:
            _deny()
        return tuple(rows)

    def page(
        self, actor: IntakePrincipal, *, size: int = 10, cursor: str | None = None
    ) -> IndexPage:
        if type(size) is not int or not 1 <= size <= 25:
            _deny()
        with self.authority.guard(actor) as scope:
            rows = self._rows(actor, scope)
            # Bind continuations only to this actor's visible, ordered projection.
            generation = digest(
                [
                    actor.tenant,
                    actor.actor,
                    scope.epoch,
                    scope.membership_revision,
                    [
                        [
                            r.workflow_ref,
                            r.label,
                            r.aliases,
                            r.proposal_ref,
                            r.proposal_revision,
                            r.state,
                            r.recorded_at,
                            r.deadline,
                            r.selection,
                        ]
                        for r in rows
                    ],
                ]
            )
            offsets = {digest([generation, size, i]): i for i in range(size, len(rows), size)}
            if cursor is not None and cursor not in offsets:
                _deny()
            start = offsets[cursor] if cursor is not None else 0
            end = start + size
            return IndexPage(
                rows[start:end], digest([generation, size, end]) if end < len(rows) else None
            )

    def select(self, actor: IntakePrincipal, selection: str) -> IndexRow:
        """Re-resolve a prior visible navigation handle against current authority."""
        require_ref(selection)
        with self.authority.guard(actor) as scope:
            for row in self._rows(actor, scope):
                if row.selection == selection:
                    return row
        raise IntakeError("workflow_index_unavailable")
