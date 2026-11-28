"""Reserve, dispatch and settle one authorized fit on the customer's worker (OPE-418).

The dispatcher is a durable state machine over three boundaries it does not own.

* ``FitLedger`` is the narrow port onto the OPE-442 customer-local ledger. In ONE durable
  operation the ledger calls this module's recheck inside its own writer transaction (standing
  mandate, residual receipt, training rights, freshness and remaining funds), reserves the upper
  bound and emits one execution ID per canonical fit intent. It aliases client-scoped operation
  keys to that intent, compare-and-sets every later stage change, only ever raises a charge and
  settles once. There is no default ledger: without one every dispatch holds as
  ``fit_ledger_unavailable`` with zero reservations and zero submissions. OPE-442 owns the durable
  implementation; the tests drive a durable double that lives in the tests tree only.
* ``model_connector.Preflight`` is the OPE-506 backend boundary. Capability discovery, estimation,
  submission, reconciliation and candidate collection go through it. Status, cancellation, usage,
  progress, early stop and absence checks call the connector directly, because ``Preflight`` has
  no such operations; the last three are optional capability protocols defined here.
* ``repair_routing.RepairRoutingStore.current_eligibility`` is called inside the reservation
  recheck and again immediately before the backend submission, as the OPE-417 decision (PR #205)
  requires, and on every supervision tick of a live fit. A stored ``fit_eligible`` receipt, fresh
  or replayed, never authorizes a fit on its own.

Stages: ``reserved`` -> ``submitting`` -> ``running`` -> ``stopping`` -> ``settled``. Two durable
holds: ``submission_uncertain`` (the backend has neither confirmed the job nor confirmed that none
exists) and ``held`` (usage over a reserved bound, or not verifiable). Every backend effect follows
a compare-and-set that only one worker can win, so a duplicate trigger, a lost acknowledgement or
a restarted worker never sends a second job. A submission whose outcome is unknown is reconciled
through a read-only lookup before anything else; it is resubmitted (at most ``MAX_SUBMISSIONS``
attempts in all, under the same reservation and provider idempotency key) only after the backend
confirms that no job exists. A backend that cannot confirm absence leaves the reservation held for
a custodian.

Money and holds (OPE-508 #217, OPE-570): a reservation is charged its full upper bound whatever it
later uses, as the mandate ledger and OPE-578 already do. A terminal job settles only when its
usage is verifiably within every reserved dimension. Otherwise it is held as ``over_cap``,
``usage_unverifiable`` or ``cap_unverifiable``: the reservation stays charged (raised to the real
figure when that is higher, never clamped), the tuned-model or artifact reference is never
collected, and nothing settles. Releasing a held reservation needs the OPE-570 operator decision,
which does not exist yet, so the ledger refuses every transition out of ``held``.

Stopping is bound to the immutable recipe: metric direction, evaluation cadence, minimum
improvement, patience and the hard step, time and cost ceilings of the reserved bound. A nonfinite
metric, an invalid progress report, an invalid output or revoked authority stops the fit with no
candidate. A recipe early stop (``plateau``) is a completion: it restores the best validation
checkpoint only when the backend supports checkpoint recovery and records the actual selection
method. A backend without progress and early-stop controls runs a preapproved fixed-length recipe
or holds as ``early_stop_unsupported``.

Training completion confers no handoff, publication or export authority. A collected candidate
only enters the existing private review and exact-version release flow.

Simulated: the tests drive generated in-memory backends and the ledger double. Evidence tier is
``EvidenceTier.FIXTURE``. Not proven here: no real pinned weights, sandbox, provider account,
billing or spend. ``EXTERNAL_GATES`` lists what remains.
"""

from __future__ import annotations

import math
import re
import secrets
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot import repair_routing as rr
from opentour_cell.pilot.connector_ext import EvidenceTier, Usage, UsageReporting
from opentour_cell.pilot.operating_point import digest

Json = dict[str, Any]
SCHEMA = "opentour.pilot.fit-dispatch.v1"
INTENT_SCHEMA = "opentour.pilot.fit-intent.v1"
RECORD_SCHEMA = "opentour.pilot.fit-reservation-record.v1"
EVIDENCE_TIER = EvidenceTier.FIXTURE
# The standing mandate's money cap is ``max_cost_cents``; owner settings accept USD only. A fit
# bound in another currency is not comparable to it, so it is refused (exact, case-sensitive).
MANDATE_CURRENCY = "USD"
# Decided under owner delegation, overnight sprint 2026-11-28 (reversible by the owner): when no
# owner setting names a maximum concurrent fit count, one fit per mandate lineage may be open.
DEFAULT_MAX_CONCURRENT_FITS = 1
# Decided under owner delegation, overnight sprint 2026-11-28 (reversible by the owner): an
# execution may make at most two submission attempts, and the second only after the backend
# confirms that the first created no job.
MAX_SUBMISSIONS = 2
LEASE_SECONDS = 60
# Caps are cumulative over the renewal lineage of the standing mandate; there is no periodic reset.
BUDGET_PERIOD = "mandate_lineage"
FIXED_STEPS = "fixed_steps"
EARLY_STOP_PLATEAU = "early_stop_plateau"
STOPPING_RULES = (FIXED_STEPS, EARLY_STOP_PLATEAU)
USAGE_HOLDS = ("over_cap", "usage_unverifiable", "cap_unverifiable")
EXTERNAL_GATES = (
    "ope442_durable_customer_local_fit_ledger",
    "real_pinned_weights_sandbox_fit_with_nonzero_adapter_change_and_fresh_process_replay",
    "real_provider_spend_billing_and_charge_reconciliation",
    "ope570_operator_release_of_held_over_cap_reservations",
    "ope566_signed_reservation_and_settlement_receipts",
    "ope426_owner_settings_for_concurrency_and_paid_caps",
    "ope377_customer_live_model_arm",
)

_REASON = re.compile(r"[a-z][a-z0-9_]{2,95}")
_IDENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_HEX = re.compile(r"[0-9a-f]{64}")
_CAPS = (
    "max_cost_cents",
    "max_fits",
    "max_rows_per_run",
    "max_runs",
    "max_seconds_per_fit",
    "max_total_rows",
    "max_total_seconds",
)


class DispatchError(ValueError):
    """A fixed lowercase reason code. Never carries rows, labels, secrets or provider text."""

    def __init__(self, reason: str) -> None:
        self.reason = reason if _REASON.fullmatch(reason) else "fit_dispatch_unavailable"
        super().__init__(self.reason)


def _need(condition: object, reason: str) -> None:
    if not condition:
        raise DispatchError(reason)


def _reason(exc: BaseException, default: str) -> str:
    """The fixed code of a collaborator's refusal (mandate, capability or routing)."""
    raw = getattr(exc, "reason", None) or getattr(exc, "code", None)
    if raw is None and exc.args:
        raw = exc.args[0]
    text = raw.lower() if isinstance(raw, str) else ""
    return text if _REASON.fullmatch(text) else default


def _ident(value: object, reason: str = "invalid_fit_dispatch_request") -> str:
    _need(isinstance(value, str) and _IDENT.fullmatch(value), reason)
    assert isinstance(value, str)
    return value


def _hex(value: object, reason: str = "invalid_fit_dispatch_request") -> str:
    _need(isinstance(value, str) and _HEX.fullmatch(value), reason)
    assert isinstance(value, str)
    return value


def _count(value: object, low: int, high: int, reason: str) -> int:
    _need(type(value) is int and low <= value <= high, reason)
    assert isinstance(value, int)
    return value


class Stage(StrEnum):
    RESERVED = "reserved"
    SUBMITTING = "submitting"
    SUBMISSION_UNCERTAIN = "submission_uncertain"
    RUNNING = "running"
    STOPPING = "stopping"
    HELD = "held"
    SETTLED = "settled"


OPEN_STAGES = frozenset(
    {Stage.RESERVED, Stage.SUBMITTING, Stage.SUBMISSION_UNCERTAIN, Stage.RUNNING, Stage.STOPPING}
)
# Every stage change a ledger may accept through ``transition``. ``settled`` is reached only
# through ``settle``. Nothing leaves ``held`` until OPE-570 defines the operator decision.
TRANSITIONS: Mapping[Stage, frozenset[Stage]] = {
    Stage.RESERVED: frozenset({Stage.SUBMITTING}),
    Stage.SUBMITTING: frozenset(
        {Stage.SUBMITTING, Stage.RESERVED, Stage.SUBMISSION_UNCERTAIN, Stage.RUNNING}
    ),
    Stage.SUBMISSION_UNCERTAIN: frozenset(
        {Stage.SUBMISSION_UNCERTAIN, Stage.RESERVED, Stage.RUNNING}
    ),
    Stage.RUNNING: frozenset({Stage.RUNNING, Stage.STOPPING, Stage.HELD}),
    Stage.STOPPING: frozenset({Stage.STOPPING, Stage.HELD}),
    Stage.HELD: frozenset(),
    Stage.SETTLED: frozenset(),
}


# --- The immutable recipe and its stopping rule -------------------------------------------------


@dataclass(frozen=True)
class DispatchRecipe:
    """The stopping terms of one fit, bound into the reservation by ``revision``.

    Recipe identity, split, metric, direction, step limit, stopping rule and fit count must match
    the frozen selection in the residual receipt; cadence, minimum improvement and patience are
    the parameters the receipt does not carry, so the reservation pins them.
    """

    recipe_id: str
    version: int
    validation_split: str
    metric: str
    direction: str
    stopping_rule: str
    step_limit: int
    evaluation_every_steps: int
    min_improvement: float
    patience: int
    fit_count: int = 1
    fixed_length_fallback: bool = False

    def validate(self) -> DispatchRecipe:
        reason = "invalid_fit_recipe"
        for value in (self.recipe_id, self.validation_split, self.metric):
            _ident(value, reason)
        _count(self.version, 1, 1_000_000, reason)
        _need(self.direction in ("maximize", "minimize"), reason)
        _need(self.stopping_rule in STOPPING_RULES, reason)
        _count(self.step_limit, 1, 10_000_000, reason)
        _count(self.evaluation_every_steps, 1, self.step_limit, reason)
        improvement = self.min_improvement
        _need(
            type(improvement) in (int, float) and math.isfinite(improvement) and improvement >= 0,
            reason,
        )
        _count(self.patience, 1, 1000, reason)
        # One execution is one fit: a parameter sweep needs separately authorized intents.
        _need(self.fit_count == 1 and type(self.fit_count) is int, reason)
        _need(type(self.fixed_length_fallback) is bool, reason)
        return self

    def as_json(self) -> Json:
        return {
            "recipe": [self.recipe_id, self.version],
            "validation_split": self.validation_split,
            "metric": self.metric,
            "direction": self.direction,
            "stopping_rule": self.stopping_rule,
            "step_limit": self.step_limit,
            "evaluation_every_steps": self.evaluation_every_steps,
            "min_improvement": float(self.min_improvement),
            "patience": self.patience,
            "fit_count": self.fit_count,
            "fixed_length_fallback": self.fixed_length_fallback,
        }

    @property
    def revision(self) -> str:
        return digest(["opentour.pilot.fit-recipe.v1", self.as_json()])

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> DispatchRecipe:
        recipe_id, version = value["recipe"]
        return cls(
            recipe_id,
            version,
            value["validation_split"],
            value["metric"],
            value["direction"],
            value["stopping_rule"],
            value["step_limit"],
            value["evaluation_every_steps"],
            value["min_improvement"],
            value["patience"],
            value["fit_count"],
            value["fixed_length_fallback"],
        ).validate()


@dataclass(frozen=True)
class Evaluation:
    """One development-validation measurement. Protected or held-out answers never appear."""

    step: int
    value: float
    checkpoint_ref: str


@dataclass(frozen=True)
class ProgressReport:
    steps: int
    seconds: int
    evaluations: tuple[Evaluation, ...]


def _improves(recipe: DispatchRecipe, value: float, best: float) -> bool:
    if recipe.direction == "maximize":
        return value >= best + recipe.min_improvement
    return value <= best - recipe.min_improvement


def stopping_decision(
    recipe: DispatchRecipe, evaluations: tuple[Evaluation, ...]
) -> tuple[str, Evaluation | None] | None:
    """Pure: ``(reason, best)`` when the recipe stops the fit now, else ``None``.

    A nonfinite metric always stops, whatever the rule. Under ``early_stop_plateau`` the fit
    stops once ``patience`` consecutive evaluations fail to improve on the best by at least
    ``min_improvement`` in the recipe's direction; ``best`` is the best evaluation so far.
    """
    if any(not math.isfinite(item.value) for item in evaluations):
        return ("nonfinite_metric", None)
    if recipe.stopping_rule != EARLY_STOP_PLATEAU:
        return None
    best: Evaluation | None = None
    stale = 0
    for item in evaluations:
        if best is None or _improves(recipe, item.value, best.value):
            best, stale = item, 0
        else:
            stale += 1
            if stale >= recipe.patience:
                return ("plateau", best)
    return None


def _check_progress(report: object, recipe: DispatchRecipe) -> ProgressReport:
    """A report that does not follow the recipe's cadence is invalid, not ignorable."""
    reason = "invalid_progress"
    _need(isinstance(report, ProgressReport), reason)
    assert isinstance(report, ProgressReport)
    _count(report.steps, 0, 10**9, reason)
    _count(report.seconds, 0, 10**9, reason)
    _need(isinstance(report.evaluations, tuple), reason)
    previous = 0
    for item in report.evaluations:
        _need(isinstance(item, Evaluation), reason)
        _count(item.step, 1, report.steps, reason)
        _need(item.step > previous and item.step % recipe.evaluation_every_steps == 0, reason)
        _need(type(item.value) in (int, float), reason)
        _need(item.checkpoint_ref == "" or _IDENT.fullmatch(item.checkpoint_ref), reason)
        previous = item.step
    return report


# --- Optional backend capabilities beyond the OPE-506 connector protocol ------------------------


@runtime_checkable
class ProgressReporting(Protocol):
    def progress(self, job: mc.JobReceipt) -> ProgressReport | None:
        """Observed steps, seconds and development-validation history; ``None`` when unknown."""
        ...


@dataclass(frozen=True)
class EarlyStopAck:
    """What a backend actually did with an early-stop request.

    ``applied`` is ``True`` only when this request ended the job; a job that had already
    finished, failed or been canceled reports ``False``. ``restored_checkpoint`` names the
    checkpoint the backend restored, or ``""`` when it kept the checkpoint at the stop.
    """

    job: mc.JobReceipt
    applied: bool
    restored_checkpoint: str = ""


@runtime_checkable
class EarlyStopping(Protocol):
    def stop_early(self, job: mc.JobReceipt, checkpoint_ref: str | None) -> EarlyStopAck:
        """Finish the job as completed, restoring ``checkpoint_ref`` when one is given."""
        ...


@runtime_checkable
class AbsenceConfirming(Protocol):
    def confirm_absent(self, intent: mc.FitIntent) -> bool:
        """``True`` only when the backend affirmatively knows no job exists for this intent."""
        ...


@runtime_checkable
class HoldReporting(Protocol):
    def hold_reason(self, job: mc.JobReceipt) -> str | None:
        """The connector's own reason to hold a COMPLETED result (OPE-508), else ``None``."""
        ...


# --- Collaborator ports -------------------------------------------------------------------------


class MandateAuthority(Protocol):
    """``LearningMandates`` satisfies this; see its ``dispatch_authority``."""

    def dispatch_authority(
        self, mandate_id: str, run_id: str, *, actor: str
    ) -> Mapping[str, Any]: ...


class EligibilitySource(Protocol):
    """``repair_routing.RepairRoutingStore`` satisfies this."""

    def read(self, evaluation_id: str) -> Json | None: ...

    def current_eligibility(
        self, evaluation_id: str, *, snapshot: rr.AdmittedSnapshot, mandate: rr.MandateView
    ) -> Json: ...


@dataclass(frozen=True)
class LedgerUsage:
    """What a ledger already holds for one budget scope, inside its writer transaction.

    Every entry counts one fit and its full reserved seconds; money counts each entry's
    ``charged_minor_units``. ``open_fits`` counts entries not yet ``settled`` or ``held``.
    """

    scope: str
    open_fits: int
    reserved_fits: int
    charged_seconds: int
    charged_minor_units: int


@dataclass(frozen=True)
class Execution:
    """The mutable facts of one execution. Each change is one compare-and-set on ``version``."""

    stage: Stage
    version: int
    job_ref: str = ""
    evidence_ref: str = ""
    submissions: int = 0
    lease_owner: str = ""
    lease_until: float = 0.0
    submitted_at: float = 0.0
    cancel_requested: bool = False
    stop_kind: str = ""
    stop_reason: str = ""
    selected_checkpoint: str = ""
    selection_method: str = ""
    hold_reason: str = ""
    observed: Json | None = None
    charged_minor_units: int = 0
    progress: Json | None = None
    settlement: Json | None = None


@dataclass(frozen=True)
class LedgerEntry:
    execution_id: str
    record: Json
    execution: Execution
    created_at: float
    updated_at: float
    replayed: bool = False
    aliases: int = 1


class FitLedger(Protocol):
    """The narrow OPE-442 fit-ledger port. Every method is one durable transaction.

    ``reserve``: look up the alias ``(record.tenant, record.actor, client, operation_key)`` and
    then ``record.intent_key``. A known alias or intent key returns its existing entry with
    ``replayed=True`` (adding the alias when new) as long as ``record.intent_digest`` matches,
    and raises ``fit_operation_key_conflict`` or ``fit_intent_conflict`` when it does not. Only for
    a new intent: compute ``LedgerUsage`` for ``record.budget_scope``, call ``recheck(usage)``
    inside the same writer transaction (it raises ``DispatchError`` to refuse), then insert one
    entry in ``reserved`` with ``charged_minor_units`` equal to the bound and commit the alias
    with it. Nothing is written when anything raises.

    ``transition``: compare-and-set from ``version`` to ``execution`` (stored at ``version + 1``).
    Raise ``ledger_version_conflict`` when the stored version differs, and refuse a stage change
    outside ``TRANSITIONS``, a lower ``charged_minor_units`` or ``submissions``, a changed
    ``job_ref`` once set, and any ``settlement``. ``settle``: compare-and-set from an open stage
    to ``settled`` with the settlement, once; an identical repeat returns the entry and a
    different one raises ``settlement_conflict``. Any other exception means the ledger is
    unavailable, and the dispatcher then sends nothing.
    """

    def reserve(
        self,
        record: Json,
        *,
        client: str,
        operation_key: str,
        recheck: Callable[[LedgerUsage], None],
    ) -> LedgerEntry: ...

    def get(self, execution_id: str) -> LedgerEntry | None: ...

    def transition(
        self, execution_id: str, *, version: int, execution: Execution, event: str
    ) -> LedgerEntry: ...

    def settle(self, execution_id: str, *, version: int, settlement: Json) -> LedgerEntry: ...


# --- Request, canonical intent and reservation record ------------------------------------------


@dataclass(frozen=True)
class DispatchRequest:
    """One fit intent, resolved server-side from the authorized plan (OPE-559 owns resolution)."""

    tenant: str
    engagement: str
    actor: str
    mandate_id: str
    run_id: str
    evaluation_id: str
    evaluation_revision: str
    plan_revision: str
    binding: mc.ModelBinding
    benchmark_revision: str
    grant_revision: str
    development_digest: str
    fields: frozenset[str]
    bound: mc.FitBound
    recipe: DispatchRecipe
    license_ref: str
    capability_receipt_ref: str

    def validate(self) -> DispatchRequest:
        for value in (
            self.tenant,
            self.engagement,
            self.actor,
            self.mandate_id,
            self.run_id,
            self.evaluation_id,
            self.benchmark_revision,
            self.grant_revision,
            self.license_ref,
            self.capability_receipt_ref,
        ):
            _ident(value)
        for value in (self.evaluation_revision, self.plan_revision, self.development_digest):
            _hex(value)
        _need(isinstance(self.binding, mc.ModelBinding), "invalid_fit_dispatch_request")
        _need(self.binding.tenant == self.tenant, "tenant_mismatch")
        _need(
            isinstance(self.fields, frozenset)
            and 0 < len(self.fields) <= 64
            and all(isinstance(f, str) and _IDENT.fullmatch(f) for f in self.fields),
            "invalid_fit_dispatch_request",
        )
        bound = self.bound
        _need(isinstance(bound, mc.FitBound), "invalid_fit_bound")
        _ident(bound.currency, "invalid_fit_bound")
        _count(bound.maximum_minor_units, 0, 10**12, "invalid_fit_bound")
        for amount in (bound.maximum_seconds, bound.maximum_rows, bound.maximum_steps):
            _count(amount, 1, 10**9, "invalid_fit_bound")
        _need(isinstance(self.recipe, DispatchRecipe), "invalid_fit_recipe")
        self.recipe.validate()
        _need(self.recipe.step_limit <= bound.maximum_steps, "recipe_exceeds_fit_bound")
        return self


def _binding_json(binding: mc.ModelBinding) -> Json:
    return {
        "tenant": binding.tenant,
        "model_ref": binding.model_ref,
        "immutable_revision": binding.immutable_revision,
        "task": binding.task,
        "input_schema": binding.input_schema,
        "output_schema": binding.output_schema,
        "preprocessing_revision": binding.preprocessing_revision,
        "route": str(binding.route),
        "account_or_host": binding.account_or_host,
        "region": binding.region,
        "credential_ref": binding.credential_ref,
    }


def _bound_json(bound: mc.FitBound) -> Json:
    return {
        "currency": bound.currency,
        "maximum_minor_units": bound.maximum_minor_units,
        "maximum_seconds": bound.maximum_seconds,
        "maximum_rows": bound.maximum_rows,
        "maximum_steps": bound.maximum_steps,
    }


def model_digest(binding: mc.ModelBinding) -> str:
    return digest(["opentour.pilot.fit-model.v1", _binding_json(binding)])


def intent_key(tenant: str, mandate_revision: str, snapshot_ref: str, plan: str, model: str) -> str:
    """The durable uniqueness key of one fit intent (tenant, mandate, snapshot, plan, model)."""
    return digest(
        ["opentour.pilot.fit-intent-key.v1", tenant, mandate_revision, snapshot_ref, plan, model]
    )


def execution_id_for(key: str) -> str:
    return "fitexec-" + key[:40]


def _canonical_intent(
    request: DispatchRequest, mandate_revision: str, snapshot_ref: str, payer: str
) -> Json:
    return {
        "schema_version": INTENT_SCHEMA,
        "tenant": request.tenant,
        "engagement": request.engagement,
        "actor": request.actor,
        "mandate_id": request.mandate_id,
        "mandate_revision": mandate_revision,
        "run_id": request.run_id,
        "snapshot_ref": snapshot_ref,
        "evaluation_id": request.evaluation_id,
        "evaluation_revision": request.evaluation_revision,
        "plan_revision": request.plan_revision,
        "model": _binding_json(request.binding),
        "model_digest": model_digest(request.binding),
        "benchmark_revision": request.benchmark_revision,
        "grant_revision": request.grant_revision,
        "development_digest": request.development_digest,
        "fields": sorted(request.fields),
        "bound": _bound_json(request.bound),
        "recipe": request.recipe.as_json(),
        "recipe_revision": request.recipe.revision,
        "license_ref": request.license_ref,
        "capability_receipt_ref": request.capability_receipt_ref,
        "payer": payer,
    }


def _binding_from(record: Mapping[str, Any]) -> mc.ModelBinding:
    model = dict(record["model"])
    model["route"] = mc.Route(model["route"])
    return mc.ModelBinding(**model)


def _bound_from(record: Mapping[str, Any]) -> mc.FitBound:
    return mc.FitBound(**record["bound"])


def fit_intent(record: Mapping[str, Any]) -> mc.FitIntent:
    """The OPE-506 fit intent of a reservation. The provider idempotency key is the execution ID."""
    return mc.FitIntent(
        binding=_binding_from(record),
        grant_revision=record["grant_revision"],
        benchmark_revision=record["benchmark_revision"],
        development_digest=record["development_digest"],
        idempotency_key=record["execution_id"],
        fields=frozenset(record["fields"]),
        bound=_bound_from(record),
    )


# --- Authority -------------------------------------------------------------------------------


@dataclass(frozen=True)
class _Authority:
    revision: str
    budget_scope: str
    owner_id: str
    delegate: str
    payer: str
    profile: str
    uses: frozenset[str]
    caps: Mapping[str, int]
    run_rows: int
    fits: int
    seconds: int
    open_fits: int
    max_concurrent_fits: int


def _authority_view(
    details: Mapping[str, Any], scope: tuple[str, str], mandate_id: str, run_id: str
) -> _Authority:
    """Type-check every value a mandate store returned; anything malformed refuses."""
    reason = "mandate_view_invalid"
    try:
        _need(details["mandate_id"] == mandate_id, reason)
        _need((details["organization"], details["engagement"]) == scope, "engagement_mismatch")
        caps = details["caps"]
        _need(isinstance(caps, Mapping) and set(caps) == set(_CAPS), reason)
        limits = {key: _count(caps[key], 0, 10_000_000, reason) for key in _CAPS}
        run = details["run"]
        _need(isinstance(run, Mapping) and run["run_id"] == run_id, reason)
        uses = details["uses"]
        _need(isinstance(uses, list) and all(isinstance(u, str) for u in uses), reason)
        profile = details["profile"]
        _need(isinstance(profile, str) and profile, reason)
        return _Authority(
            revision=_hex(details["mandate_revision"], reason),
            budget_scope=_ident(details["budget_scope"], reason),
            owner_id=_ident(details["owner_id"], reason),
            delegate=_ident(details["delegate"], reason),
            payer=_ident(details["payer"], reason),
            profile=profile,
            uses=frozenset(uses),
            caps=limits,
            run_rows=_count(run["rows"], 1, 10_000_000, reason),
            fits=_count(details["cumulative_reserved_fits"], 0, 10_000_000, reason),
            seconds=_count(details["cumulative_reserved_seconds"], 0, 10**12, reason),
            open_fits=_count(details["open_reserved_fits"], 0, 10_000_000, reason),
            max_concurrent_fits=_count(
                details.get("max_concurrent_fits", DEFAULT_MAX_CONCURRENT_FITS), 1, 64, reason
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, DispatchError):
            raise
        raise DispatchError(reason) from None


# --- Usage holds (OPE-508) -------------------------------------------------------------------


def usage_hold(bound: mc.FitBound, job_ref: str, usage: object) -> str | None:
    """``None`` only when usage is verifiably within every reserved dimension.

    Consistent with OPE-508: a verified excess on any dimension is ``over_cap`` even when money
    is not comparable; usage that is missing, estimated, for another job or malformed is
    ``usage_unverifiable``; money in another currency is ``cap_unverifiable``. Usage exactly at a
    bound is accepted. A connector's own ``cap_status`` other than ``within_cap`` is honoured.
    """
    if not isinstance(usage, Usage) or usage.job_ref != job_ref:
        return "usage_unverifiable"
    status = getattr(usage, "cap_status", None)
    if status == "over_cap":
        return "over_cap"
    if not usage.settleable:
        # Estimated usage never settles as actual spend; a connector that could not compare its
        # figure to the cap says so through ``cap_unknown``.
        return "cap_unverifiable" if status == "cap_unknown" else "usage_unverifiable"
    comparable = usage.currency == bound.currency
    over = (
        usage.seconds > bound.maximum_seconds
        or usage.rows > bound.maximum_rows
        or usage.steps > bound.maximum_steps
        or (comparable and usage.incurred_minor_units > bound.maximum_minor_units)
    )
    if over:
        return "over_cap"
    if not comparable or status == "cap_unknown":
        return "cap_unverifiable"
    return None


def _usage_json(usage: Usage | None) -> Json | None:
    if not isinstance(usage, Usage):
        return None
    return {
        "currency": usage.currency,
        "incurred_minor_units": usage.incurred_minor_units,
        "seconds": usage.seconds,
        "rows": usage.rows,
        "steps": usage.steps,
        "source": str(usage.source),
    }


def _zero_usage(currency: str) -> Json:
    return {
        "currency": currency,
        "incurred_minor_units": 0,
        "seconds": 0,
        "rows": 0,
        "steps": 0,
        "source": "not_submitted",
    }


def _check_fit(model_ref: str, bound: Mapping[str, Any], authority: _Authority) -> None:
    """Per-fit limits that need no ledger usage: model, currency, money cap, seconds and rows."""
    caps = authority.caps
    _need(model_ref == authority.profile, "model_outside_mandate_profile")
    _need(bound["currency"] == MANDATE_CURRENCY, "money_cap_currency_mismatch")
    _need(bound["maximum_minor_units"] == 0 or caps["max_cost_cents"] > 0, "mandate_cap_required")
    _need(
        bound["maximum_seconds"] <= caps["max_seconds_per_fit"]
        and bound["maximum_rows"] <= min(caps["max_rows_per_run"], authority.run_rows),
        "per_fit_budget_exceeded",
    )


# --- Status projection -----------------------------------------------------------------------

_HOLD_CLASS: Mapping[str, tuple[str, str, str]] = {
    "fit_ledger_unavailable": ("ledger", "install_customer_local_fit_ledger", "operator"),
    "ledger_version_conflict": ("ledger", "retry_status", "worker"),
    "submission_unconfirmed": ("submission", "await_backend_confirmation", "custodian"),
    "submission_in_progress": ("submission", "await_submission", "worker"),
    "over_cap": ("usage", "operator_incident_review_required", "operator"),
    "usage_unverifiable": ("usage", "operator_incident_review_required", "operator"),
    "cap_unverifiable": ("usage", "operator_incident_review_required", "operator"),
    "concurrent_fit_limit_reached": ("budget", "wait_for_open_fit_to_settle", "worker"),
    "fit_budget_exhausted": ("budget", "owner_budget_decision_required", "owner"),
    "money_budget_exhausted": ("budget", "owner_budget_decision_required", "owner"),
    "mandate_cap_required": ("budget", "owner_budget_decision_required", "owner"),
    "money_cap_currency_mismatch": ("budget", "owner_budget_decision_required", "owner"),
    "per_fit_budget_exceeded": ("budget", "submit_smaller_bound", "caller"),
    "training_budget_required": ("budget", "owner_budget_decision_required", "owner"),
    "fit_operation_key_conflict": ("conflict", "use_new_operation_key", "caller"),
    "fit_intent_conflict": ("conflict", "submit_new_intent", "caller"),
    "recipe_mismatch": ("conflict", "submit_new_intent", "caller"),
    "recipe_exceeds_fit_bound": ("conflict", "submit_new_intent", "caller"),
    "model_mismatch": ("conflict", "submit_new_intent", "caller"),
    "capability_mismatch": ("conflict", "submit_new_intent", "caller"),
    "tenant_mismatch": ("conflict", "submit_new_intent", "caller"),
    "engagement_mismatch": ("conflict", "submit_new_intent", "caller"),
    "invalid_fit_dispatch_request": ("conflict", "submit_new_intent", "caller"),
    "invalid_fit_bound": ("conflict", "submit_new_intent", "caller"),
    "invalid_fit_recipe": ("conflict", "submit_new_intent", "caller"),
    "execution_unavailable": ("conflict", "none", "caller"),
    "early_stop_unsupported": ("setup", "approve_fixed_length_recipe_or_backend", "ml_owner"),
}
_ELIGIBILITY = frozenset(
    {
        "not_fit_eligible",
        "evaluation_unavailable",
        "evaluation_router_revision_changed",
        "evaluation_revision_changed",
        "mandate_withdrawn_or_expired",
        "mandate_revision_changed",
        "evidence_revision_changed",
        "evidence_unavailable",
        "training_rights_required",
    }
)
_AUTHORITY = frozenset(
    {
        "standing_mandate_withdrawn",
        "standing_mandate_expired",
        "standing_mandate_unavailable",
        "standing_delegate_mismatch",
        "manual_learning_only",
        "model_outside_prospective_scope",
        "model_outside_mandate_profile",
        "delegated_run_unavailable",
        "standing_engagement_unavailable",
        "mandate_view_invalid",
        "mandate_lineage_unverified",
        "registered_mandate_owner_required",
        "training_grant_unavailable",
        "payer_mismatch",
        "budget_scope_mismatch",
    }
)


def classify(reason: str) -> tuple[str, str, str]:
    """``(hold_class, next_action, next_owner)`` for a fixed reason code."""
    if reason in _HOLD_CLASS:
        return _HOLD_CLASS[reason]
    if reason in _ELIGIBILITY:
        return ("eligibility", "re_evaluate_residual", "worker")
    if reason in _AUTHORITY:
        return ("authority", "owner_reauthorization_required", "owner")
    return ("setup", "complete_backend_setup", "ml_owner")


@dataclass(frozen=True)
class DispatchStatus:
    """Read-only projection for get_run. Never carries rows, metric values or provider text."""

    execution_id: str | None
    learning_run_state: str
    stage: str | None
    reason: str
    hold_class: str | None
    next_action: str
    next_owner: str
    reservations: int
    submissions: int
    replayed: bool = False
    job_ref: str | None = None
    candidate_ref: str | None = None
    selection_method: str | None = None
    selected_checkpoint: str | None = None
    sealed_partial_ref: str | None = None
    usage: Json | None = None
    charged: Json | None = None
    lineage: Json | None = None
    cancel_pending: bool = False
    handoff_authority: str = "none"
    publication: str = "independent_review_required"
    protected_reads: int = 0
    evidence_tier: str = str(EVIDENCE_TIER)
    schema_version: str = SCHEMA

    def as_json(self) -> Json:
        return asdict(self)


def _without_entry(state: str, reason: str) -> DispatchStatus:
    """A refusal or hold before any reservation: zero reservations and zero submissions."""
    if state == "benchmark_only":
        return DispatchStatus(None, state, None, reason, None, "none", "ml_owner", 0, 0)
    hold, action, owner = classify(reason)
    return DispatchStatus(None, state, None, reason, hold, action, owner, 0, 0)


# Notes that describe a step still under way; any other note means the step could not proceed.
_PROGRESS_NOTES = frozenset(
    {
        "submission_in_progress",
        "backend_state_unknown",
        "backend_receipt_mismatched",
        "stop_request_pending",
    }
)
_LINEAGE = (
    "tenant",
    "mandate_id",
    "mandate_revision",
    "run_id",
    "snapshot_ref",
    "plan_revision",
    "evaluation_id",
    "model_digest",
    "benchmark_revision",
    "recipe_revision",
    "license_ref",
    "payer",
)
_OWNER_CANCELED = ("canceled_before_submission", "canceled_by_owner")


def status_of(entry: LedgerEntry, *, note: str | None = None) -> DispatchStatus:
    """Project one ledger entry. Reads nothing else and has no effect."""
    record, ex = entry.record, entry.execution
    settlement = ex.settlement or {}
    candidate = settlement.get("candidate_ref")
    hold: str | None = None
    if ex.stage is Stage.SETTLED:
        reason = str(settlement.get("reason"))
        outcome = settlement.get("outcome")
        if outcome == "completed":
            state = "review_ready" if candidate else "completed"
        elif outcome == "stopped":
            state = "completed"
        elif outcome == "released":
            state = "canceled" if reason in _OWNER_CANCELED else "refused"
        elif outcome == "canceled":
            state = "canceled"
        else:
            state = "failed"
        action, owner = ("private_review", "reviewer") if candidate else ("none", "worker")
    elif ex.stage is Stage.HELD:
        state, reason = "held", ex.hold_reason
        hold, action, owner = classify(reason)
    elif ex.stage is Stage.SUBMISSION_UNCERTAIN:
        state, reason = "held", "submission_unconfirmed"
        hold, action, owner = classify(reason)
    elif ex.stage in (Stage.RESERVED, Stage.SUBMITTING) and ex.hold_reason:
        state, reason = "held", ex.hold_reason
        hold, action, owner = classify(reason)
    elif ex.stage in (Stage.RESERVED, Stage.SUBMITTING):
        state, reason, action, owner = "queued", str(ex.stage), "await_submission", "worker"
    else:
        state, reason, action, owner = "running", str(ex.stage), "await_completion", "worker"
        if ex.stop_reason:
            reason = ex.stop_reason
    if note is not None:
        reason = note
        if note not in _PROGRESS_NOTES and ex.stage in OPEN_STAGES:
            state = "held"
            hold, action, owner = classify(note)
    return DispatchStatus(
        execution_id=entry.execution_id,
        learning_run_state=state,
        stage=str(ex.stage),
        reason=reason,
        hold_class=hold,
        next_action=action,
        next_owner=owner,
        reservations=1,
        submissions=ex.submissions,
        replayed=entry.replayed,
        job_ref=ex.job_ref or None,
        candidate_ref=candidate,
        selection_method=settlement.get("selection_method"),
        selected_checkpoint=settlement.get("selected_checkpoint"),
        sealed_partial_ref=settlement.get("sealed_partial_ref"),
        usage=settlement.get("usage") or ex.observed,
        charged={
            "currency": record["bound"]["currency"],
            "minor_units": ex.charged_minor_units,
            "seconds": record["bound"]["maximum_seconds"],
            "fits": 1,
            "period": record["limits"]["period"],
        },
        lineage={key: record[key] for key in _LINEAGE},
        cancel_pending=ex.cancel_requested and ex.stage in OPEN_STAGES,
    )


# --- The dispatcher ----------------------------------------------------------------------------


class _Outcome(Exception):
    """A non-error answer before any reservation (``benchmark_only``)."""

    def __init__(self, state: str, reason: str) -> None:
        super().__init__(reason)
        self.state = state
        self.reason = reason if _REASON.fullmatch(reason) else "training_unavailable"


class _SubmitWitness:
    """Delegates to the real connector and records whether ``submit`` was ever entered."""

    def __init__(self, connector: mc.Connector) -> None:
        self.connector = connector
        self.submit_entered = False

    def observe(self, binding: mc.ModelBinding, purpose: mc.Purpose) -> mc.Observation:
        return self.connector.observe(binding, purpose)

    def serialize_development(
        self, intent: mc.FitIntent, rows: tuple[Mapping[str, object], ...]
    ) -> bytes:
        return self.connector.serialize_development(intent, rows)

    def estimate(self, intent: mc.FitIntent) -> mc.FitBound | None:
        return self.connector.estimate(intent)

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        self.submit_entered = True
        return self.connector.submit(intent, development)

    def lookup(self, intent: mc.FitIntent) -> mc.JobReceipt | None:
        return self.connector.lookup(intent)

    def status(self, job: mc.JobReceipt) -> mc.JobReceipt:
        return self.connector.status(job)

    def cancel(self, job: mc.JobReceipt) -> mc.JobReceipt:
        return self.connector.cancel(job)

    def collect(self, job: mc.JobReceipt) -> mc.Candidate:
        return self.connector.collect(job)

    def predict_for_replay(
        self, candidate: mc.Candidate, inputs: tuple[Mapping[str, object], ...]
    ) -> tuple[Mapping[str, object], ...]:
        return self.connector.predict_for_replay(candidate, inputs)


_TERMINAL_JOBS = (mc.JobState.COMPLETED, mc.JobState.CANCELED, mc.JobState.FAILED)
_BACKEND_ERRORS = (OSError, ValueError, LookupError)


def _confirmed(intent: mc.FitIntent, job: object) -> mc.JobReceipt | None:
    """A backend receipt that names a real job for exactly this intent, else ``None``."""
    if (
        isinstance(job, mc.JobReceipt)
        and job.intent == intent
        and isinstance(job.state, mc.JobState)
        and job.state is not mc.JobState.UNKNOWN
        and isinstance(job.job_ref, str)
        and job.job_ref
        and isinstance(job.evidence_ref, str)
        and job.evidence_ref
    ):
        return job
    return None


class FitDispatcher:
    """Reserve, submit, supervise and settle fits. Holds no state outside the ledger.

    Every public method returns a ``DispatchStatus`` and never raises for a refusal. ``status``
    is read-only. ``dispatch``, ``advance`` and ``cancel`` are idempotent: repeat them freely,
    from any worker and after any restart.
    """

    def __init__(
        self,
        *,
        ledger: FitLedger | None,
        preflight: mc.Preflight,
        mandates: MandateAuthority,
        routing: EligibilitySource,
        evidence: Callable[[str], rr.AdmittedSnapshot],
        development: Callable[[Mapping[str, Any]], tuple[Mapping[str, object], ...]],
        worker_id: str,
        clock: Callable[[], float] = time.time,
        lease_seconds: int = LEASE_SECONDS,
    ) -> None:
        self.ledger = ledger
        self.preflight = preflight
        self.connector = preflight.connector
        self.mandates = mandates
        self.routing = routing
        self.evidence = evidence
        self.development = development
        self.worker_id = _ident(worker_id, "invalid_worker_identity")
        # A restarted process cannot be told apart from a slow one, so every dispatcher instance
        # holds its own lease token and honours any other token's lease until it expires.
        self.lease_token = self.worker_id + ":" + secrets.token_hex(8)
        self.clock = clock
        self.lease_seconds = _count(lease_seconds, 1, 3600, "invalid_lease")

    # Public operations ----------------------------------------------------------------------

    def dispatch(
        self, request: DispatchRequest, *, client: str, operation_key: str
    ) -> DispatchStatus:
        """Admit one fit intent: at most one reservation and one job across every retry."""
        try:
            request.validate()
            _ident(client)
            _ident(operation_key)
        except DispatchError as exc:
            return _without_entry("refused", exc.reason)
        if self.ledger is None:
            return _without_entry("held", "fit_ledger_unavailable")
        try:
            entry = self._admit(request, client, operation_key)
        except _Outcome as outcome:
            return _without_entry(outcome.state, outcome.reason)
        except DispatchError as exc:
            return _without_entry("held", exc.reason)
        except mc.CapabilityError as exc:
            return _without_entry("held", _reason(exc, "model_capability_unavailable"))
        status = self._advance(entry)
        if status.execution_id == entry.execution_id:
            status = replace(status, replayed=entry.replayed)
        return status

    def advance(self, execution_id: str, *, actor: str) -> DispatchStatus:
        """One supervision step: submit, reconcile, enforce limits, stop or settle."""
        try:
            entry = self._entry(execution_id, actor)
        except DispatchError as exc:
            return _without_entry("held", exc.reason)
        return self._advance(entry)

    def cancel(self, execution_id: str, *, actor: str) -> DispatchStatus:
        """Authorized cancellation. Stays pending while a submission is uncertain."""
        try:
            entry = self._entry(execution_id, actor)
            ex = entry.execution
            if ex.stage is Stage.RESERVED:
                return self._release(entry, "canceled_before_submission")
            if ex.stage in (Stage.SUBMITTING, Stage.SUBMISSION_UNCERTAIN):
                if not ex.cancel_requested:
                    moved = self._transition(entry, replace(ex, cancel_requested=True), "cancel")
                    entry = moved or self._latest(entry)
                return status_of(entry)
            if ex.stage is Stage.RUNNING:
                return self._stop(entry, self._job(entry), "cancel", "canceled_by_owner")
            return status_of(entry)
        except DispatchError as exc:
            return _without_entry("held", exc.reason)

    def status(self, execution_id: str, *, actor: str) -> DispatchStatus:
        """Read-only. Never calls the backend and never writes the ledger."""
        try:
            return status_of(self._entry(execution_id, actor))
        except DispatchError as exc:
            return _without_entry("held", exc.reason)

    # Admission ------------------------------------------------------------------------------

    def _admit(self, request: DispatchRequest, client: str, operation_key: str) -> LedgerEntry:
        receipt = self._receipt(request)
        authority = self._authority(
            {
                "tenant": request.tenant,
                "engagement": request.engagement,
                "mandate_id": request.mandate_id,
                "run_id": request.run_id,
                "actor": request.actor,
            }
        )
        snapshot_ref = receipt["snapshot_ref"]
        canonical = _canonical_intent(request, authority.revision, snapshot_ref, authority.payer)
        key = intent_key(
            request.tenant,
            authority.revision,
            snapshot_ref,
            request.plan_revision,
            canonical["model_digest"],
        )
        record: Json = {
            **canonical,
            "schema_version": RECORD_SCHEMA,
            "execution_id": execution_id_for(key),
            "intent_key": key,
            "intent_digest": digest(canonical),
            "budget_scope": authority.budget_scope,
            "owner_id": authority.owner_id,
            "limits": self._limits(authority),
        }
        existing = self._get(record["execution_id"])
        if existing is not None:
            # A replay: the ledger returns the existing entry, or refuses a changed intent.
            record["stop_plan"] = existing.record.get("stop_plan")
            record["capability_evidence_ref"] = existing.record.get("capability_evidence_ref")
        else:
            _check_fit(request.binding.model_ref, record["bound"], authority)
            empty = LedgerUsage(authority.budget_scope, 0, 0, 0, 0)
            self._eligibility(record, authority, empty)
            observation = self._capability(request)
            record["stop_plan"] = self._stop_plan(request.recipe, observation)
            record["capability_evidence_ref"] = observation.source_ref
            # Known cost within the bound before any reservation; unknown cost is a setup hold.
            self.preflight.prepare(request.actor, fit_intent(record))
        assert self.ledger is not None
        try:
            return self.ledger.reserve(
                record,
                client=client,
                operation_key=operation_key,
                recheck=lambda usage: self._recheck(record, usage),
            )
        except DispatchError:
            raise
        except Exception:  # noqa: BLE001 - any other ledger failure reserves nothing
            raise DispatchError("fit_ledger_unavailable") from None

    def _receipt(self, request: DispatchRequest) -> Json:
        """The residual receipt this intent cites, matched to its model, recipe and split."""
        try:
            receipt = self.routing.read(request.evaluation_id)
        except rr.RoutingError as exc:
            raise DispatchError(_reason(exc, "evaluation_unavailable")) from None
        _need(isinstance(receipt, dict), "evaluation_unavailable")
        assert receipt is not None
        _need(receipt.get("revision") == request.evaluation_revision, "evaluation_revision_changed")
        _need(receipt.get("organization") == request.tenant, "tenant_mismatch")
        _need(
            receipt.get("run_id") == request.run_id
            and receipt.get("mandate_ref") == request.mandate_id,
            "evaluation_unavailable",
        )
        _need(receipt.get("outcome") == "fit_eligible", "not_fit_eligible")
        selection = receipt.get("recipe_selection")
        selected = selection.get("selected") if isinstance(selection, dict) else None
        _need(isinstance(selected, dict), "recipe_mismatch")
        assert isinstance(selected, dict)
        binding, recipe = request.binding, request.recipe
        _need(
            selected.get("model_ref") == binding.model_ref and selected.get("task") == binding.task,
            "model_mismatch",
        )
        _need(
            selected.get("capability_receipt_ref") == request.capability_receipt_ref,
            "capability_mismatch",
        )
        _need(
            selected.get("recipe") == [recipe.recipe_id, recipe.version]
            and selected.get("validation_split") == recipe.validation_split
            and selected.get("metric") == recipe.metric
            and selected.get("direction") == recipe.direction
            and selected.get("step_limit") == recipe.step_limit
            and selected.get("stopping_rule") == recipe.stopping_rule
            and selected.get("trial_limit") == recipe.fit_count
            and selected.get("allowed_params") == [],
            "recipe_mismatch",
        )
        _ident(receipt.get("snapshot_ref"), "evaluation_unavailable")
        return receipt

    def _authority(self, record: Mapping[str, Any]) -> _Authority:
        """The current standing mandate behind a request or reservation record."""
        mandate_id, run_id, actor = record["mandate_id"], record["run_id"], record["actor"]
        try:
            details = self.mandates.dispatch_authority(mandate_id, run_id, actor=actor)
        except (ValueError, OSError) as exc:
            raise DispatchError(_reason(exc, "standing_mandate_unavailable")) from None
        scope = (record["tenant"], record["engagement"])
        authority = _authority_view(details, scope, mandate_id, run_id)
        _need(authority.delegate == actor, "standing_delegate_mismatch")
        _need("train" in authority.uses, "training_rights_required")
        return authority

    @staticmethod
    def _limits(authority: _Authority) -> Json:
        """The policy field mapping persisted in the reservation record."""
        caps = authority.caps
        return {
            "currency": MANDATE_CURRENCY,
            "max_minor_units": caps["max_cost_cents"],
            "max_fits": caps["max_fits"],
            "max_total_seconds": caps["max_total_seconds"],
            "max_seconds_per_fit": caps["max_seconds_per_fit"],
            "max_rows_per_fit": caps["max_rows_per_run"],
            "max_concurrent_fits": authority.max_concurrent_fits,
            "compute_unit": "bound_seconds_on_named_route",
            "period": BUDGET_PERIOD,
        }

    def _eligibility(
        self, record: Mapping[str, Any], authority: _Authority, usage: LedgerUsage | None
    ) -> None:
        """``current_eligibility`` against today's evidence and mandate; refuse unless eligible.

        With ``usage`` the mandate view carries every fit and second already charged (direct
        mandate reservations plus the ledger's), so the receipt's budget gate sees real use. An
        in-flight check passes ``None``: the ledger already holds this fit's budget, so only the
        conditions that are not budget (withdrawal, expiry, changed revisions, lost training
        rights) can stop it.
        """
        try:
            snapshot = self.evidence(record["run_id"])
        except (OSError, ValueError, LookupError):
            raise DispatchError("evidence_unavailable") from None
        _need(isinstance(snapshot, rr.AdmittedSnapshot), "evidence_unavailable")
        caps = authority.caps
        view = rr.MandateView(
            mandate_ref=record["mandate_id"],
            mandate_revision=authority.revision,
            withdrawn=False,
            expired=False,
            uses=authority.uses,
            max_fits=caps["max_fits"],
            reserved_fits=0 if usage is None else authority.fits + usage.reserved_fits,
            max_seconds_per_fit=caps["max_seconds_per_fit"],
            max_total_seconds=caps["max_total_seconds"],
            reserved_seconds=0 if usage is None else authority.seconds + usage.charged_seconds,
        )
        try:
            result = self.routing.current_eligibility(
                record["evaluation_id"], snapshot=snapshot, mandate=view
            )
        except rr.RoutingError as exc:
            raise DispatchError(_reason(exc, "evaluation_unavailable")) from None
        _need(isinstance(result, dict), "evaluation_unavailable")
        if result.get("eligible") is not True:
            code = result.get("reason_code")
            raise DispatchError(code if isinstance(code, str) else "not_fit_eligible")

    def _grant_current(self, record: Mapping[str, Any]) -> None:
        binding = _binding_from(record)
        try:
            grant = self.preflight.current_grant(record["actor"], binding, mc.Purpose.TRAINING)
        except _BACKEND_ERRORS:
            grant = None
        _need(
            grant is not None
            and grant.actor == record["actor"]
            and grant.binding == binding
            and grant.purpose is mc.Purpose.TRAINING
            and grant.revision == record["grant_revision"]
            and self.clock() < grant.valid_until,
            "training_grant_unavailable",
        )

    def _capability(self, request: DispatchRequest) -> mc.Observation:
        capability = self.preflight.discover(request.actor, request.binding, mc.Purpose.TRAINING)
        if capability.state is mc.State.BENCHMARK_ONLY:
            raise _Outcome("benchmark_only", capability.reason)
        _need(capability.state is mc.State.TRAINABLE, capability.reason)
        try:
            observation = self.connector.observe(request.binding, mc.Purpose.TRAINING)
        except _BACKEND_ERRORS:
            raise DispatchError("model_capability_evidence_unavailable") from None
        _need(
            observation.binding == request.binding
            and observation.observed_at <= self.clock() < observation.expires_at
            and observation.training_supported
            and observation.hard_cost_cap,
            "model_capability_evidence_stale_or_mismatched",
        )
        return observation

    def _stop_plan(self, recipe: DispatchRecipe, observation: mc.Observation) -> str:
        """Fixed length, early stopping, or the recipe's preapproved fixed-length fallback."""
        if recipe.stopping_rule == FIXED_STEPS:
            return "fixed_length"
        controls = (
            observation.progress
            and observation.cancellation
            and isinstance(self.connector, ProgressReporting)
            and isinstance(self.connector, EarlyStopping)
        )
        if controls:
            return "early_stopping"
        _need(recipe.fixed_length_fallback, "early_stop_unsupported")
        return "fixed_length_fallback"

    def _recheck(self, record: Json, usage: LedgerUsage) -> None:
        """Inside the ledger's writer transaction: authority, residual receipt, rights, funds."""
        authority = self._authority(record)
        _need(authority.revision == record["mandate_revision"], "mandate_revision_changed")
        _need(
            usage.scope == record["budget_scope"] == authority.budget_scope,
            "budget_scope_mismatch",
        )
        _need(authority.payer == record["payer"], "payer_mismatch")
        _check_fit(record["model"]["model_ref"], record["bound"], authority)
        # The OPE-417 contract: eligibility is recomputed immediately before reserving.
        self._eligibility(record, authority, usage)
        self._grant_current(record)
        bound, caps = record["bound"], authority.caps
        _need(
            authority.open_fits + usage.open_fits < authority.max_concurrent_fits,
            "concurrent_fit_limit_reached",
        )
        _need(
            authority.fits + usage.reserved_fits + 1 <= caps["max_fits"]
            and authority.seconds + usage.charged_seconds + bound["maximum_seconds"]
            <= caps["max_total_seconds"],
            "fit_budget_exhausted",
        )
        _need(
            usage.charged_minor_units + bound["maximum_minor_units"] <= caps["max_cost_cents"],
            "money_budget_exhausted",
        )

    def _in_flight_problem(self, record: Mapping[str, Any]) -> str | None:
        """Why a reserved or live fit may no longer proceed, else ``None``."""
        try:
            authority = self._authority(record)
            _need(authority.revision == record["mandate_revision"], "mandate_revision_changed")
            self._eligibility(record, authority, None)
            self._grant_current(record)
        except DispatchError as exc:
            return exc.reason
        return None

    # Ledger access: anything but a typed refusal means the ledger is unavailable --------------

    def _get(self, execution_id: str) -> LedgerEntry | None:
        assert self.ledger is not None
        try:
            return self.ledger.get(execution_id)
        except DispatchError:
            raise
        except Exception:  # noqa: BLE001 - fail closed
            raise DispatchError("fit_ledger_unavailable") from None

    def _latest(self, entry: LedgerEntry) -> LedgerEntry:
        latest = self._get(entry.execution_id)
        _need(latest is not None, "fit_ledger_unavailable")
        assert latest is not None
        return latest

    def _entry(self, execution_id: str, actor: str) -> LedgerEntry:
        _need(self.ledger is not None, "fit_ledger_unavailable")
        _ident(execution_id, "execution_unavailable")
        entry = self._get(execution_id)
        # One reason for an unknown execution and for someone else's: least disclosure.
        _need(entry is not None and entry.record.get("actor") == actor, "execution_unavailable")
        assert entry is not None
        return entry

    def _transition(
        self, entry: LedgerEntry, execution: Execution, event: str
    ) -> LedgerEntry | None:
        """Compare-and-set; ``None`` when another worker moved the entry first."""
        assert self.ledger is not None
        try:
            return self.ledger.transition(
                entry.execution_id,
                version=entry.execution.version,
                execution=execution,
                event=event,
            )
        except DispatchError as exc:
            if exc.reason == "ledger_version_conflict":
                return None
            raise
        except Exception:  # noqa: BLE001 - fail closed
            raise DispatchError("fit_ledger_unavailable") from None

    def _settle(self, entry: LedgerEntry, settlement: Json) -> DispatchStatus:
        assert self.ledger is not None
        try:
            settled = self.ledger.settle(
                entry.execution_id, version=entry.execution.version, settlement=settlement
            )
        except DispatchError as exc:
            if exc.reason != "ledger_version_conflict":
                raise
            return status_of(self._latest(entry))
        except Exception:  # noqa: BLE001 - fail closed
            raise DispatchError("fit_ledger_unavailable") from None
        return status_of(settled)

    # Submission and reconciliation ----------------------------------------------------------

    def _advance(self, entry: LedgerEntry) -> DispatchStatus:
        try:
            ex = entry.execution
            if ex.stage in (Stage.HELD, Stage.SETTLED):
                return status_of(entry)
            if ex.stage is Stage.RESERVED:
                return self._submit(entry)
            if ex.stage is Stage.SUBMITTING:
                if self.clock() < ex.lease_until:
                    return status_of(entry, note="submission_in_progress")
                return self._reconcile(entry)
            if ex.stage is Stage.SUBMISSION_UNCERTAIN:
                return self._reconcile(entry)
            return self._supervise(entry)
        except DispatchError as exc:
            return status_of(entry, note=exc.reason)

    def _submit(self, entry: LedgerEntry) -> DispatchStatus:
        ex, record = entry.execution, entry.record
        if ex.cancel_requested:
            return self._release(entry, "canceled_before_submission")
        if ex.submissions >= MAX_SUBMISSIONS:
            return self._release(entry, ex.hold_reason or "submission_attempts_exhausted")
        now = self.clock()
        claimed = self._transition(
            entry,
            replace(
                ex,
                stage=Stage.SUBMITTING,
                submissions=ex.submissions + 1,
                lease_owner=self.lease_token,
                lease_until=now + self.lease_seconds,
                submitted_at=now,
                hold_reason="",
            ),
            "submitting",
        )
        if claimed is None:
            return status_of(self._latest(entry))
        witness = _SubmitWitness(self.connector)
        preflight = mc.Preflight(witness, self.preflight.current_grant, clock=self.preflight.clock)

        @contextmanager
        def guard() -> Iterator[None]:
            # Inside Preflight, after its own checks and immediately before the submission.
            latest = self._latest(claimed)
            _need(not latest.execution.cancel_requested, "canceled_before_submission")
            _need(
                latest.execution.stage is Stage.SUBMITTING
                and latest.execution.lease_owner == self.lease_token
                and self.clock() < latest.execution.lease_until,
                "submission_lease_lost",
            )
            problem = self._in_flight_problem(record)
            _need(problem is None, problem or "fit_dispatch_unavailable")
            yield

        try:
            rows = self.development(record)
        except (OSError, ValueError, LookupError, TypeError, KeyError):
            return self._unsent(claimed, "development_payload_unavailable")
        try:
            receipt = preflight.submit_reserved_fit(
                record["actor"], fit_intent(record), rows, reservation_guard=guard
            )
        except (DispatchError, mc.CapabilityError, *_BACKEND_ERRORS) as exc:
            if witness.submit_entered:
                # The backend may hold a job: reconcile it, never resend blindly.
                return self._record_job(claimed, None)
            return self._unsent(claimed, _reason(exc, "backend_unavailable"))
        return self._record_job(claimed, receipt)

    def _unsent(self, claimed: LedgerEntry, reason: str) -> DispatchStatus:
        """The backend was provably not called. Revocation releases; anything else may retry."""
        latest = self._latest(claimed)
        if reason == "submission_lease_lost" or latest.execution.stage is not Stage.SUBMITTING:
            return status_of(latest)
        if reason == "canceled_before_submission" or classify(reason)[0] in (
            "authority",
            "eligibility",
        ):
            return self._release(latest, reason)
        back = self._transition(
            latest,
            replace(
                latest.execution,
                stage=Stage.RESERVED,
                lease_owner="",
                lease_until=0.0,
                hold_reason=reason,
            ),
            "submission_not_sent",
        )
        return status_of(back or self._latest(latest))

    def _record_job(self, entry: LedgerEntry, receipt: object) -> DispatchStatus:
        """Record a confirmed job, or hold the submission as unconfirmed."""
        job = _confirmed(fit_intent(entry.record), receipt)
        for _ in range(3):
            ex = entry.execution
            if ex.stage not in (Stage.SUBMITTING, Stage.SUBMISSION_UNCERTAIN) or ex.job_ref:
                return status_of(entry)
            if job is None and ex.stage is Stage.SUBMISSION_UNCERTAIN:
                return status_of(entry)
            if job is None:
                target = replace(
                    ex,
                    stage=Stage.SUBMISSION_UNCERTAIN,
                    lease_owner="",
                    lease_until=0.0,
                    hold_reason="submission_unconfirmed",
                )
            else:
                target = replace(
                    ex,
                    stage=Stage.RUNNING,
                    job_ref=job.job_ref,
                    evidence_ref=job.evidence_ref,
                    lease_owner="",
                    lease_until=0.0,
                    hold_reason="",
                )
            moved = self._transition(entry, target, "job_confirmed" if job else "unconfirmed")
            if moved is not None:
                return status_of(moved)
            entry = self._latest(entry)
        return status_of(entry)

    def _reconcile(self, entry: LedgerEntry) -> DispatchStatus:
        """Query the existing execution first; resubmit only after the backend confirms absence."""
        record, ex = entry.record, entry.execution
        intent = fit_intent(record)
        receipt: object
        try:
            receipt = self.preflight.reconcile_fit(record["actor"], intent)
        except mc.CapabilityError:
            # Authority to look up has lapsed; a read-only lookup still finds a job to stop.
            try:
                receipt = self.connector.lookup(intent)
            except _BACKEND_ERRORS:
                receipt = None
        except _BACKEND_ERRORS:
            receipt = None
        if _confirmed(intent, receipt) is not None:
            return self._record_job(entry, receipt)
        absent = False
        if isinstance(self.connector, AbsenceConfirming):
            try:
                absent = self.connector.confirm_absent(intent) is True
            except _BACKEND_ERRORS:
                absent = False
        if not absent:
            return self._record_job(entry, None)
        if ex.cancel_requested:
            return self._release(entry, "canceled_before_submission")
        if ex.submissions >= MAX_SUBMISSIONS:
            return self._release(entry, "submission_attempts_exhausted")
        back = self._transition(
            entry,
            replace(ex, stage=Stage.RESERVED, lease_owner="", lease_until=0.0, hold_reason=""),
            "absence_confirmed",
        )
        if back is None:
            return status_of(self._latest(entry))
        return self._submit(back)

    def _release(self, entry: LedgerEntry, reason: str) -> DispatchStatus:
        """Settle an execution that provably started nothing: zero use, charge retained."""
        return self._settle(
            entry,
            {
                "outcome": "released",
                "reason": reason,
                "usage": _zero_usage(entry.record["bound"]["currency"]),
                "candidate_ref": None,
                "replay_evidence_ref": None,
                "selection_method": None,
                "selected_checkpoint": None,
                "sealed_partial_ref": None,
                "job_ref": None,
                "evidence_ref": None,
                "settled_at": self.clock(),
                "handoff_authority": "none",
            },
        )

    # Supervision of a live fit --------------------------------------------------------------

    def _job(self, entry: LedgerEntry) -> mc.JobReceipt:
        ex = entry.execution
        return mc.JobReceipt(
            fit_intent(entry.record), mc.JobState.RUNNING, ex.job_ref, ex.evidence_ref
        )

    def _supervise(self, entry: LedgerEntry) -> DispatchStatus:
        record, ex = entry.record, entry.execution
        known = self._job(entry)
        try:
            current = self.connector.status(known)
        except _BACKEND_ERRORS:
            return status_of(entry, note="backend_state_unknown")
        if (
            not isinstance(current, mc.JobReceipt)
            or current.intent != known.intent
            or current.job_ref != ex.job_ref
        ):
            return status_of(entry, note="backend_receipt_mismatched")
        if current.state in _TERMINAL_JOBS:
            return self._finish(entry, current)
        if current.state is mc.JobState.UNKNOWN:
            return status_of(entry, note="backend_state_unknown")
        if ex.stage is Stage.STOPPING:
            return self._issue_stop(entry, current)
        if ex.cancel_requested:
            return self._stop(entry, current, "cancel", "canceled_by_owner")
        problem = self._in_flight_problem(record)
        if problem is not None:
            return self._stop(entry, current, "cancel", problem)
        recipe = DispatchRecipe.from_json(record["recipe"])
        bound = record["bound"]
        report: ProgressReport | None = None
        if isinstance(self.connector, ProgressReporting):
            try:
                raw = self.connector.progress(current)
            except _BACKEND_ERRORS:
                raw = None
            if raw is not None:
                try:
                    report = _check_progress(raw, recipe)
                except DispatchError:
                    return self._stop(entry, current, "cancel", "invalid_progress")
        if report is not None:
            if report.steps > bound["maximum_steps"]:
                return self._stop(entry, current, "cancel", "step_limit_exceeded")
            if report.seconds > bound["maximum_seconds"]:
                return self._stop(entry, current, "cancel", "time_limit_exceeded")
            decision = stopping_decision(recipe, report.evaluations)
            if decision is not None and decision[0] == "nonfinite_metric":
                return self._stop(entry, current, "cancel", "nonfinite_metric")
            if (
                decision is not None
                and decision[0] == "plateau"
                and record.get("stop_plan") == "early_stopping"
            ):
                best = decision[1]
                restore = best is not None and best.checkpoint_ref and self._restorable(record)
                checkpoint = best.checkpoint_ref if restore and best is not None else ""
                method = "best_validation_checkpoint" if checkpoint else "checkpoint_at_stop"
                return self._stop(entry, current, "early", "plateau", checkpoint, method)
        if isinstance(self.connector, UsageReporting):
            try:
                live = self.connector.usage(current)
            except _BACKEND_ERRORS:
                live = None
            if isinstance(live, Usage) and live.job_ref == ex.job_ref:
                if usage_hold(_bound_from(record), ex.job_ref, live) == "over_cap":
                    return self._stop(entry, current, "cancel", "usage_limit_exceeded")
        if report is not None:
            summary: Json = {
                "steps": report.steps,
                "seconds": report.seconds,
                "evaluations": len(report.evaluations),
                "last_checkpoint": report.evaluations[-1].checkpoint_ref
                if report.evaluations
                else "",
            }
            if summary != ex.progress:
                moved = self._transition(entry, replace(ex, progress=summary), "progress")
                return status_of(moved or self._latest(entry))
        return status_of(entry)

    def _restorable(self, record: Mapping[str, Any]) -> bool:
        """Best-checkpoint restore only when the backend currently declares support."""
        try:
            observation = self.connector.observe(_binding_from(record), mc.Purpose.TRAINING)
        except _BACKEND_ERRORS:
            return False
        return bool(observation.checkpoint_recovery) and self.clock() < observation.expires_at

    def _stop(
        self,
        entry: LedgerEntry,
        job: mc.JobReceipt,
        kind: str,
        reason: str,
        checkpoint: str = "",
        method: str = "",
    ) -> DispatchStatus:
        """Record the stop durably first (one winner), then ask the backend."""
        moved = self._transition(
            entry,
            replace(
                entry.execution,
                stage=Stage.STOPPING,
                stop_kind=kind,
                stop_reason=reason,
                selected_checkpoint=checkpoint,
                selection_method=method,
            ),
            "stop_" + kind,
        )
        if moved is None:
            return status_of(self._latest(entry))
        return self._issue_stop(moved, job)

    def _issue_stop(self, entry: LedgerEntry, job: mc.JobReceipt) -> DispatchStatus:
        """Idempotent. A failed cancel stays ``stopping`` and is reissued on the next step.

        A recipe early stop the backend cannot perform (no control, or the callback fails)
        becomes a cancellation with no candidate, because the recipe's checkpoint selection can
        then no longer be shown to have happened.
        """
        ex = entry.execution
        if ex.stop_kind == "early":
            try:
                if not isinstance(self.connector, EarlyStopping):
                    raise LookupError("early_stop_unsupported")
                ack = self.connector.stop_early(job, ex.selected_checkpoint or None)
                if not isinstance(ack, EarlyStopAck) or ack.job.job_ref != ex.job_ref:
                    raise LookupError("early_stop_unconfirmed")
                restored = ack.restored_checkpoint if ack.applied else ""
                actual = replace(
                    ex,
                    # The backend finished first: this is an ordinary completion, not a stop.
                    stop_kind="early" if ack.applied else "",
                    stop_reason="plateau" if ack.applied else "",
                    selected_checkpoint=restored,
                    selection_method=(
                        ""
                        if not ack.applied
                        else "best_validation_checkpoint"
                        if restored
                        else "checkpoint_at_stop"
                    ),
                )
                if actual != ex:
                    moved = self._transition(entry, actual, "early_stop_acknowledged")
                    return status_of(moved or self._latest(entry))
                return status_of(entry)
            except _BACKEND_ERRORS:
                moved = self._transition(
                    entry,
                    replace(
                        ex,
                        stop_kind="cancel",
                        stop_reason="early_stop_unsupported",
                        selected_checkpoint="",
                        selection_method="",
                    ),
                    "stop_cancel",
                )
                entry = moved or self._latest(entry)
        try:
            self.connector.cancel(job)
        except _BACKEND_ERRORS:
            return status_of(entry, note="stop_request_pending")
        return status_of(entry)

    def _finish(self, entry: LedgerEntry, job: mc.JobReceipt) -> DispatchStatus:
        """Settle a terminal job once, or hold it when its usage is not verifiably in bounds."""
        record, ex = entry.record, entry.execution
        usage: Usage | None = None
        if isinstance(self.connector, UsageReporting):
            try:
                reported = self.connector.usage(job)
            except _BACKEND_ERRORS:
                reported = None
            usage = reported if isinstance(reported, Usage) else None
        hold = usage_hold(_bound_from(record), job.job_ref, usage)
        if job.state is mc.JobState.COMPLETED and isinstance(self.connector, HoldReporting):
            # The connector's journal-based reason (OPE-508) is authoritative when it has one;
            # a local excess still holds when it has none.
            try:
                own = self.connector.hold_reason(job)
            except _BACKEND_ERRORS:
                own = "usage_unverifiable"
            if own is not None:
                hold = own if own in USAGE_HOLDS else "usage_unverifiable"
        if hold is not None:
            return self._hold(entry, hold, usage)
        candidate: mc.Candidate | None = None
        if job.state is mc.JobState.COMPLETED and ex.stop_kind == "cancel":
            # A fit stopped for revocation, a limit or an invalid metric publishes nothing,
            # even when the backend finished first.
            outcome, reason = "stopped", ex.stop_reason or "stopped"
        elif job.state is mc.JobState.COMPLETED:
            outcome = "completed"
            reason = self._in_flight_problem(record) or (
                "plateau" if ex.stop_kind == "early" else "recipe_complete"
            )
            if reason in ("plateau", "recipe_complete"):
                try:
                    candidate = self.preflight.collect_candidate(record["actor"], job)
                except (mc.CapabilityError, *_BACKEND_ERRORS):
                    reason = "invalid_output"
        elif job.state is mc.JobState.CANCELED:
            outcome, reason = "canceled", ex.stop_reason or "backend_canceled"
        else:
            outcome, reason = "failed", "backend_failed"
        method = None
        if candidate is not None:
            plan = record.get("stop_plan")
            method = ex.selection_method or (
                "fixed_length_fallback" if plan == "fixed_length_fallback" else "final_step"
            )
        sealed = None
        if candidate is None and isinstance(ex.progress, dict):
            sealed = ex.progress.get("last_checkpoint") or None
        return self._settle(
            entry,
            {
                "outcome": outcome,
                "reason": reason,
                "usage": _usage_json(usage),
                "candidate_ref": candidate.artifact_or_serving_revision if candidate else None,
                "replay_evidence_ref": candidate.replay_evidence_ref if candidate else None,
                "selection_method": method,
                "selected_checkpoint": (ex.selected_checkpoint or None) if candidate else None,
                "sealed_partial_ref": sealed,
                "job_ref": job.job_ref,
                "evidence_ref": job.evidence_ref,
                "settled_at": self.clock(),
                "handoff_authority": "none",
            },
        )

    def _hold(self, entry: LedgerEntry, reason: str, usage: Usage | None) -> DispatchStatus:
        """Keep the reservation charged and record the real figure, never clamped (OPE-570)."""
        ex, record = entry.execution, entry.record
        charge = ex.charged_minor_units
        if isinstance(usage, Usage) and usage.currency == record["bound"]["currency"]:
            charge = max(charge, usage.incurred_minor_units)
        moved = self._transition(
            entry,
            replace(
                ex,
                stage=Stage.HELD,
                hold_reason=reason,
                observed=_usage_json(usage),
                charged_minor_units=charge,
            ),
            "held_" + reason,
        )
        return status_of(moved or self._latest(entry))
