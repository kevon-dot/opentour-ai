"""Additive, route-neutral extensions to the OPE-506 model connector contract.

Defines the evidence tiers, measured usage and fence receipts that the open-weight (OPE-507),
managed-provider (OPE-508) and customer-trainer (OPE-509) connectors share. This module adds
optional capability protocols only: ``model_connector.Connector`` and ``Preflight`` are unchanged.

Not proven here: nothing in this module contacts a provider, trainer or model. A value of
``EvidenceTier.FIXTURE`` never satisfies a provider or customer evidence gate.

Trust boundary: an ``EvidenceTier`` is a label a caller asserts. Qualification built on it (see
``model_family.QualificationRecord``) is caller-asserted and self-consistent only, with no signing
contract, no key custody and no authority that confirms it. ``Usage`` is likewise a plain value:
``settleable`` is a guard for a consumer to apply, and no code in this package consumes it yet.
The frozen dataclasses here cannot be made tamper-proof in Python (``object.__setattr__``
bypasses ``frozen``), so neither type is a proof of anything on its own.

Precondition on the caller: these objects are assumed NOT to cross a trust boundary. The
guarantees below hold only while every ``Usage`` and ``QualificationRecord`` is built and read
inside one trusted process by code that does not forge them. If they are ever stored, sent or
accepted from another party, they need a signing contract (signature plus key custody) that does
not exist in this project; until then, treat any received instance as unverified.
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
    """Resources a job actually incurred. ``ESTIMATED`` usage can never settle as actual spend.

    Enforcement status: this type only records the settleable/estimated distinction. No code in
    ``src`` reads ``settleable`` or ``UsageSource`` today, so the guard protects nothing until a
    settlement path checks it. Any future settlement path MUST check ``settleable``.

    What construction does enforce: a known ``UsageSource``, non-empty string ``job_ref`` and
    ``currency``, non-negative ``int`` counts and a ``bool`` ``irreversible``. Known limits: there
    is no upper bound (``incurred_minor_units=10**30`` is accepted), ``currency`` and ``job_ref``
    have no format check (``"x"`` and ``" "`` pass), and the instance is a caller-asserted value
    that ``object.__setattr__`` can alter.
    """

    job_ref: str
    currency: str
    incurred_minor_units: int
    seconds: int
    rows: int
    steps: int
    source: UsageSource
    irreversible: bool = False

    def __post_init__(self) -> None:
        # A plain string such as "estimated" must not slip past the identity check below.
        try:
            object.__setattr__(self, "source", UsageSource(self.source))
        except ValueError:
            raise ValueError("INVALID_USAGE") from None
        counts = (self.incurred_minor_units, self.seconds, self.rows, self.steps)
        if (
            not isinstance(self.job_ref, str)
            or not self.job_ref
            or not isinstance(self.currency, str)
            or not self.currency
            or any(type(count) is not int or count < 0 for count in counts)
            or type(self.irreversible) is not bool
        ):
            raise ValueError("INVALID_USAGE")

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
