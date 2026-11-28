"""Bind the learning-run fit port to the local dispatcher's single reservation and settlement."""

from __future__ import annotations

from collections.abc import Callable

from opentour_cell.pilot import fit_dispatch as fd
from opentour_cell.pilot import learning_runs as lr


class LearningFitLedger:
    """A trusted worker resolves its own admitted learning run to its frozen dispatch request.

    Resolution has to recheck the learning-run grant and source on each lookup. Learning
    resource units on this route are seconds. The worker calls the dispatcher's advance or
    cancel separately; neither a lookup nor a reservation submits a backend job.
    """

    def __init__(
        self,
        dispatcher: fd.FitDispatcher,
        *,
        actor: str,
        client: str,
        resolve: Callable[[str], fd.DispatchRequest | None],
    ) -> None:
        self.dispatcher, self.actor, self.client, self.resolve = dispatcher, actor, client, resolve

    def _binding(self, intent: lr.FitIntent) -> tuple[fd.DispatchRequest, str]:
        request = self.resolve(intent.run_ref)
        if request is None:
            raise lr.LedgerHold("no_eligible_plan")
        request.validate()
        if (
            request.actor != self.actor
            or request.tenant != intent.tenant
            or request.mandate_id != intent.mandate_id
            or intent.plan_ref != "plan-" + request.plan_revision[:40]
            or fd.model_digest(request.binding) != intent.model_digest
            or request.evaluation_revision != intent.residual_ref
            or request.bound.maximum_seconds != intent.max_units
        ):
            raise lr.LedgerHold("fit_intent_requires_reconciliation")
        key = fd.intent_key(
            intent.tenant,
            intent.mandate_revision,
            intent.snapshot_ref,
            request.plan_revision,
            intent.model_digest,
        )
        return request, fd.execution_id_for(key)

    def lookup(self, intent: lr.FitIntent) -> lr.FitReservation | None:
        request, execution_id = self._binding(intent)
        try:
            entry = self.dispatcher.lookup_reserved(request, expected_id=execution_id)
        except fd.DispatchError as exc:
            raise lr.LedgerHold("fit_intent_requires_reconciliation") from exc
        if entry is None:
            return None
        return lr.FitReservation(entry.execution_id, entry.record["bound"]["maximum_seconds"])

    def reserve(self, intent: lr.FitIntent, *, operation_key: str) -> lr.FitReservation:
        request, execution_id = self._binding(intent)
        return self._reserve(request, execution_id, operation_key)

    def _reserve(
        self, request: fd.DispatchRequest, execution_id: str, key: str
    ) -> lr.FitReservation:
        status = self.dispatcher.reserve_only(
            request, client=self.client, operation_key=key, expected_id=execution_id
        )
        if status.execution_id != execution_id or not status.charged:
            raise lr.LedgerHold(
                "standing_fit_budget_exhausted"
                if status.reason
                in {
                    "fit_budget_exhausted",
                    "money_budget_exhausted",
                    "concurrent_fit_limit_reached",
                }
                else "fit_intent_requires_reconciliation"
            )
        return lr.FitReservation(execution_id, status.charged["seconds"])

    def settle(self, reservation_ref: str, *, consumed_units: int) -> lr.FitReservation:
        # A caller's measured count cannot create a terminal settlement. The dispatcher
        # verifies backend usage and records that settlement before the learning run finishes.
        status = self.dispatcher.status(reservation_ref, actor=self.actor)
        if (
            status.stage != "settled"
            or not status.usage
            or not status.charged
            or type(consumed_units) is not int
            or status.usage["seconds"] != consumed_units
        ):
            raise lr.LedgerHold("fit_intent_requires_reconciliation")
        if status.candidate_ref and status.learning_run_state == "review_ready":
            result = "candidate"
        elif status.learning_run_state == "canceled":
            result = "canceled"
        else:
            result = "unsuccessful"
        return lr.FitReservation(reservation_ref, status.charged["seconds"], consumed_units, result)
