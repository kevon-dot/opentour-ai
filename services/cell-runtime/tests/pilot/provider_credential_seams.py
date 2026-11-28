"""Test-only seams for ``provider_credential``. Nothing in the source tree imports this module.

``ProviderCapTestSeam`` wraps a real ``LearningMandates`` and supplies what its schema cannot hold
today: ``learning_mandate`` forces ``max_cost_cents == 0`` and records no provider account or cost
usage, so a real mandate can never authorize a paid provider dispatch. The seam adds a cap, a
provider account and an in-memory cost tally on top of the real store's identity, revision,
expiry, withdrawal state and fit/seconds reservation. ``register`` adds it to the module's
otherwise empty source registry; ``use_clock`` replaces the module's wall clock for one test.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from opentour_cell.pilot import provider_credential as pc
from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError


class ProviderCapTestSeam:
    def __init__(
        self,
        mandates: LearningMandates,
        *,
        cap_minor_units: Any,
        provider: str,
        account_id: str,
        consumed_minor_units: Any = 0,
        receipt_overrides: Mapping[str, Any] | None = None,
        scope_overrides: Mapping[str, Any] | None = None,
    ) -> None:
        self.mandates = mandates
        self.cap_minor_units = cap_minor_units
        self.provider, self.account_id = provider, account_id
        self.consumed_minor_units = consumed_minor_units
        self.receipt_overrides = dict(receipt_overrides or {})
        self.scope_overrides = dict(scope_overrides or {})
        self.costs: dict[str, int] = {}

    def inspect(self, mandate_id: str, *, actor: str | None = None) -> dict[str, Any]:
        receipt = dict(self.mandates.inspect(mandate_id, actor=actor))
        scope = dict(receipt["scope"], max_cost_cents=self.cap_minor_units)
        scope.update(self.scope_overrides)
        consumed: Any = self.consumed_minor_units
        if self.costs:
            consumed = consumed + sum(self.costs.values())
        receipt.update(
            scope=scope,
            provider=self.provider,
            provider_account_id=self.account_id,
            cumulative_reserved_cost_cents=consumed,
        )
        receipt.update(self.receipt_overrides)
        return receipt

    def reserve(
        self, mandate_id: str, request: Mapping[str, Any], actor: str, cost: int
    ) -> dict[str, Any]:
        used = self.consumed_minor_units + sum(self.costs.values())
        if used + cost > self.cap_minor_units:
            raise MandateError("standing_cost_budget_exhausted")
        result = dict(self.mandates.reserve(mandate_id, request, actor=actor))
        self.costs.setdefault(str(result.get("reservation_id")), cost)
        return result


def register() -> None:
    pc._SOURCE_REGISTRY[ProviderCapTestSeam] = pc._SourceHooks(
        underlying=lambda seam: seam.mandates,
        reserve=lambda seam, mandate_id, request, actor, cost: seam.reserve(
            mandate_id, request, actor, cost
        ),
    )


def use_clock(clock: Callable[[], float]) -> None:
    """Replace the module's wall clock. Tests restore it with an autouse fixture."""
    pc._wall_clock = clock
