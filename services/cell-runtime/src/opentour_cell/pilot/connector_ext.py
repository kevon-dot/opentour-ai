"""Additive, route-neutral extensions to the OPE-506 model connector contract.

Defines the evidence tiers, measured usage and fence receipts that the open-weight (OPE-507),
managed-provider (OPE-508) and customer-trainer (OPE-509) connectors share. This module adds
optional capability protocols only: ``model_connector.Connector`` and ``Preflight`` are unchanged.

Not proven here: nothing in this module contacts a provider, trainer or model. A value of
``EvidenceTier.FIXTURE`` never satisfies a provider or customer evidence gate.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from opentour_cell.pilot.model_connector import JobReceipt


class EvidenceTier(StrEnum):
    """Separate evidence levels. A lower tier never stands in for a higher one."""

    FIXTURE = "fixture"
    SOFTWARE_QUALIFIED = "software_qualified"
    PROVIDER_WITNESSED = "provider_witnessed"
    CUSTOMER_WITNESSED = "customer_witnessed"


class UsageSource(StrEnum):
    MEASURED = "measured"
    PROVIDER_REPORTED = "provider_reported"
    ESTIMATED = "estimated"


@dataclass(frozen=True)
class Usage:
    """Resources a job actually incurred. ``ESTIMATED`` usage can never settle as actual spend."""

    job_ref: str
    currency: str
    incurred_minor_units: int
    seconds: int
    rows: int
    steps: int
    source: UsageSource
    irreversible: bool = False

    @property
    def settleable(self) -> bool:
        return self.source is not UsageSource.ESTIMATED


@dataclass(frozen=True)
class FenceReceipt:
    scope: str
    canceled_job_refs: tuple[str, ...]
    evidence_ref: str


@runtime_checkable
class UsageReporting(Protocol):
    def usage(self, job: JobReceipt) -> Usage | None:
        """Return measured or provider-reported usage. ``None`` means unknown, never zero."""
        ...


@runtime_checkable
class Fenceable(Protocol):
    def fence(self, scope: str) -> FenceReceipt:
        """Durably block new work and stop live jobs before returning."""
        ...
