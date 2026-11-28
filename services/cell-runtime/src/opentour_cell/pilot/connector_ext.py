"""Additive, route-neutral extensions to the OPE-506 model connector contract.

Defines the evidence tiers, measured usage, fence receipts and the usage-vs-cap hold that the
open-weight (OPE-507), managed-provider (OPE-508) and customer-trainer (OPE-509) connectors share.
This module adds optional capability protocols and pure helpers only: ``model_connector.Connector``
and ``Preflight`` are unchanged.

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

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol, runtime_checkable

from opentour_cell.pilot.model_connector import FitBound, JobReceipt


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


# ---- Usage-vs-cap hold, shared by the managed (OPE-508), open-weight (OPE-507) and
# customer-trainer (OPE-509) routes.
#
# Release rule (OPE-508 decision): a COMPLETED job's result is released only when its journaled
# usage is verifiably within the journaled cap on every dimension. Usage exactly at a maximum is
# accepted. Otherwise the job is held with one of three fixed reasons, which differ only in what an
# operator must investigate:
# * ``over_cap``: a verified figure is above its maximum. A verified excess wins over an unusable
#   cap on another dimension.
# * ``usage_unverifiable``: the usage is missing, non-integer, bool, negative, non-finite, has an
#   empty or non-string currency, or has the wrong shape. Reported first: a malformed record yields
#   no verified figure, so there is nothing to compare whatever the cap says.
# * ``cap_unverifiable``: the usage is in a currency other than the cap's (exact, case-sensitive),
#   or a journaled maximum or the currency is missing or not a plain non-negative ``int``.
# Each route decides what COMPLETED means and enforces the hold where its result would be released;
# ``hold_reason`` is ``None`` for a job that is not COMPLETED.
#
# Both sides of the comparison are read from the route's own journal: the cap is journaled at
# dispatch (``journal_bound``) and the usage at the terminal observation. Caller-supplied values are
# never compared at release. An intent bound whose maxima are not exactly ``int`` is refused before
# dispatch (``require_exact_bound``): an ``int`` subclass serializes like the plain int, so a digest
# alone cannot tell them apart (OPE-508 L1). A negative maximum or a currency that is not an
# ISO-4217-shaped code is refused there too (review of #241, F4: refuse before spend).
CAP_WITHIN = "within_cap"
CAP_OVER = "over_cap"
CAP_UNKNOWN = "cap_unknown"
HOLD_OVER_CAP = CAP_OVER
HOLD_USAGE_UNVERIFIABLE = "usage_unverifiable"
HOLD_CAP_UNVERIFIABLE = "cap_unverifiable"
BOUND_NOT_EXACT_INT = "fit_bound_not_exact_int"
BOUND_NEGATIVE = "fit_bound_negative"
BOUND_CURRENCY_INVALID = "fit_bound_currency_invalid"
_CURRENCY = re.compile(r"[A-Z]{3}")

# (usage field, cap field) for every bounded dimension. Money also needs a matching currency.
_DIMENSIONS = (
    ("minor_units", "maximum_minor_units"),
    ("seconds", "maximum_seconds"),
    ("rows", "maximum_rows"),
    ("steps", "maximum_steps"),
)
_MAXIMA = tuple(limit for _, limit in _DIMENSIONS)
USAGE_FIELDS = frozenset({"currency", "minor_units", "seconds", "rows", "steps"})


def require_exact_bound(bound: FitBound) -> None:
    """Refuse, before anything is journaled or sent, a bound that is not exactly well formed.

    Checked in this order, each with a fixed reason:
    * ``fit_bound_not_exact_int``: a maximum is not exactly ``int`` (bool and subclasses too);
    * ``fit_bound_negative``: a maximum is below zero (zero is accepted);
    * ``fit_bound_currency_invalid``: the currency is not an exact ``str`` of three ASCII
      uppercase letters (the ISO-4217 shape; whether the code is assigned is not checked).
    """
    maxima: list[object] = [getattr(bound, limit, None) for limit in _MAXIMA]
    counts = [maximum for maximum in maxima if type(maximum) is int]
    if len(counts) != len(maxima):
        raise ValueError(BOUND_NOT_EXACT_INT)
    if any(count < 0 for count in counts):
        raise ValueError(BOUND_NEGATIVE)
    currency = getattr(bound, "currency", None)
    if type(currency) is not str or _CURRENCY.fullmatch(currency) is None:
        raise ValueError(BOUND_CURRENCY_INVALID)


def journal_bound(bound: FitBound) -> dict[str, object]:
    """The cap document a route journals at dispatch: the currency and all four maxima."""
    require_exact_bound(bound)
    return {"currency": bound.currency, **{limit: getattr(bound, limit) for limit in _MAXIMA}}


@dataclass(frozen=True)
class JournaledCap:
    """A cap read back from a journal. Values are untyped until compared; absent is ``None``."""

    currency: object = None
    maximum_minor_units: object = None
    maximum_seconds: object = None
    maximum_rows: object = None
    maximum_steps: object = None

    @classmethod
    def from_document(cls, document: object) -> JournaledCap:
        if not isinstance(document, Mapping):
            return cls()
        return cls(**{name: document.get(name) for name in ("currency", *_MAXIMA)})


class UsageLike(Protocol):
    """Reported or measured usage: the five figures every route compares to the cap."""

    @property
    def currency(self) -> object: ...

    @property
    def minor_units(self) -> object: ...

    @property
    def seconds(self) -> object: ...

    @property
    def rows(self) -> object: ...

    @property
    def steps(self) -> object: ...


@dataclass(frozen=True)
class UsageFigures:
    """Usage read back from a journal. Values are untyped until ``usage_valid`` checks them."""

    currency: object
    minor_units: object
    seconds: object
    rows: object
    steps: object

    @classmethod
    def from_document(cls, document: object) -> UsageFigures | None:
        """Exactly the five usage fields, else ``None`` (unknown, never an error)."""
        if isinstance(document, Mapping) and set(document) == USAGE_FIELDS:
            return cls(**{name: document[name] for name in USAGE_FIELDS})
        return None


def usage_document(usage: UsageLike) -> dict[str, object]:
    """The journal form of a usage report."""
    return {name: getattr(usage, name) for name in sorted(USAGE_FIELDS)}


def usage_valid(usage: UsageLike | None) -> bool:
    """Usage with a non-empty string currency and non-negative ``int`` (never bool) figures."""
    if usage is None:
        return False
    counts = (usage.minor_units, usage.seconds, usage.rows, usage.steps)
    return (
        isinstance(usage.currency, str)
        and bool(usage.currency)
        and all(type(count) is int and count >= 0 for count in counts)
    )


def cap_limit(value: object) -> int | None:
    """A journaled maximum is a non-negative ``int`` (never bool or float); else ``None``."""
    return value if type(value) is int and value >= 0 else None


def cap_currency(cap: JournaledCap) -> str | None:
    """The journaled currency, or ``None`` when it is missing or not a non-empty string."""
    return cap.currency if isinstance(cap.currency, str) and cap.currency else None


def cap_status(usage: UsageLike | None, cap: JournaledCap) -> str | None:
    """Compare usage to every journaled maximum; ``None`` when the usage itself is invalid."""
    if usage is None or not usage_valid(usage):
        return None
    currency = cap_currency(cap)
    comparable = currency is not None and usage.currency == currency
    comparisons: list[tuple[int, int | None]] = []
    for used, limit in _DIMENSIONS:
        figure = getattr(usage, used)
        maximum = cap_limit(getattr(cap, limit))
        if used == "minor_units" and not comparable:
            maximum = None  # units differ or are unrecorded: money is not comparable
        comparisons.append((figure, maximum))
    if any(maximum is not None and figure > maximum for figure, maximum in comparisons):
        return CAP_OVER
    if any(maximum is None for _, maximum in comparisons):
        return CAP_UNKNOWN
    return CAP_WITHIN


def usage_hold(usage: UsageLike | None, cap: JournaledCap) -> str | None:
    """The fixed hold reason for a COMPLETED job's journaled usage and cap, else ``None``."""
    status = cap_status(usage, cap)
    if status is None:
        return HOLD_USAGE_UNVERIFIABLE
    if status == CAP_OVER:
        return HOLD_OVER_CAP
    if status == CAP_UNKNOWN:
        return HOLD_CAP_UNVERIFIABLE
    return None


@dataclass(frozen=True)
class CappedUsage(Usage):
    """Usage plus a closed-vocabulary comparison to the journaled cap.

    ``cap_status`` is ``within_cap``, ``over_cap`` or ``cap_unknown`` (see ``cap_status``). The
    figures are the real ones and are never clamped. Anything but ``within_cap`` is not
    ``settleable``, so ``settleable`` matches the release rule for a COMPLETED job.
    """

    cap_status: str = CAP_UNKNOWN
    approved_maximum_minor_units: int | None = None
    approved_currency: str | None = None

    @property
    def settleable(self) -> bool:
        return self.cap_status == CAP_WITHIN and super().settleable


def capped_usage(
    job_ref: str,
    usage: UsageLike,
    cap: JournaledCap,
    source: UsageSource,
    *,
    irreversible: bool = False,
) -> CappedUsage:
    """Build ``CappedUsage`` from valid journaled usage (``Usage`` validates the figures)."""
    return CappedUsage(
        job_ref,
        usage.currency,  # type: ignore[arg-type]
        usage.minor_units,  # type: ignore[arg-type]
        usage.seconds,  # type: ignore[arg-type]
        usage.rows,  # type: ignore[arg-type]
        usage.steps,  # type: ignore[arg-type]
        source,
        irreversible=irreversible,
        cap_status=cap_status(usage, cap) or CAP_UNKNOWN,
        approved_maximum_minor_units=cap_limit(cap.maximum_minor_units),
        approved_currency=cap_currency(cap),
    )
