"""Small test consumers of typed ports; no production service registration.

OPE-149 will supply the production reconciliation consumer. These test mechanics
exercise adapter observations without assuming idempotent Batch submission.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from tools.fixtures.ports import (
    AcknowledgmentLost,
    AuthorityPort,
    BatchPort,
    CellTransactionPort,
    EffectState,
    FixtureRefusal,
    JournalPort,
    ProviderJob,
    Scope,
    Submission,
)


def guarded_effect_probe(
    authority: AuthorityPort,
    journal: JournalPort,
    scope: Scope,
    epoch: int,
    revision: str,
    now: str,
    effect: Callable[[], None],
) -> None:
    """Test consumer only; no claim of production atomic journal admission."""
    frontier = journal.current_frontier()
    if frontier is None or not journal.contiguous(*frontier):
        raise FixtureRefusal("AUTHORITY_UNAVAILABLE")
    authority.check(scope, epoch, revision, now)
    effect()


@dataclass
class SubmissionProbe:
    request: Submission
    store: CellTransactionPort
    provider: BatchPort
    epoch: int
    revision: str
    provider_job_id: str | None = None
    quarantined_ids: list[str] = field(default_factory=list)

    def run(self) -> EffectState:
        effect = self.request.submission_id
        scope = self.request.scope
        state = self.store.state(effect, scope)
        if state is None:
            self.store.prepare(
                effect, scope, self.epoch, self.revision, self.request.request_digest
            )
            state = "prepared"
        if state == "prepared":
            self.store.resolve(effect, scope, "dispatching")
            try:
                job = self.provider.submit(self.request)
            except AcknowledgmentLost:
                self.store.resolve(effect, scope, "outcome_unknown")
                return "outcome_unknown"
            except FixtureRefusal:
                self.store.resolve(effect, scope, "refused")
                return "refused"
            self.provider_job_id = job.provider_job_id
            self.store.resolve(effect, scope, "applied")
            return "applied"
        if state in {"dispatching", "outcome_unknown", "reconciling"}:
            matches: list[ProviderJob] = []
            cursor = None
            seen: set[int] = set()
            for _ in range(100):
                page = self.provider.lookup(self.request, cursor)
                matches.extend(job for job in page.jobs if job.submission == self.request)
                cursor = page.next_cursor
                if cursor is None:
                    break
                if cursor in seen:
                    raise FixtureRefusal("INVALID_FIXTURE_CURSOR")
                seen.add(cursor)
            else:
                raise FixtureRefusal("LOOKUP_BOUND_EXCEEDED")
            if len(matches) == 1:
                self.provider_job_id = matches[0].provider_job_id
                self.store.resolve(effect, scope, "applied")
                return "applied"
            if len(matches) > 1:
                self.quarantined_ids = [job.provider_job_id for job in matches]
                self.store.resolve(effect, scope, "reconciling")
                return "reconciling"
            self.store.resolve(effect, scope, "outcome_unknown")
            return "outcome_unknown"
        return state
