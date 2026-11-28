"""Fail-closed gate and idempotent ledger for staging one tuned candidate (OPE-545).

Staging is a privileged but narrow effect: it records that one exact candidate identity
may be *offered for a later human release decision*. It is not release authority, it
requests no release, sends no alert, and is never a real or customer win.

A candidate stages only when every gate holds affirmatively:

* paired: three arms (tuned, incumbent, credible simpler repair) scored the identical frozen
  denominator at the identical fixed automatic coverage;
* meaningful: the preregistered rule's lower interval bound over per-family effects, against
  BOTH controls, exceeds the preregistered threshold (``independent_family_interval``,
  family = sampling unit). A positive point estimate is never enough, a tie favors the simpler
  repair, and the numbers make no significance claim beyond "this preregistered rule cleared";
* independent verdict: every arm has a current, issued verdict from an issuer that is neither
  the candidate's producer nor its selector, bound to the frozen manifest, the arm's result
  digest and its serving identity, and attested by the injected ``VerdictAuthority`` (see
  below). The ceiling is "authority-attested if the injected authority is real"; a packet's own
  flag is never evidence. Model text and a pending request grant nothing.

The trust root is NOT in the packet. Verdict and confirmation authenticity is decided by an
injected ``VerdictAuthority`` (the OPE-528/OPE-421/OPE-418 boundary, none implemented yet): a
packet's own ``issuer_authenticated`` flag is never sufficient, and with no authority supplied
nothing can be eligible (``verdict_authority_unavailable``). Verdict bindings to the manifest,
the arm's result digest and the serving identity are re-derived from the frozen benchmark and
the arm run, and the authority must attest the whole verdict body.

Anything absent, stale, superseded, inconsistent or unqualified yields ``hold`` or ``refused``
with explicit reason codes, never a silent pass. Zero eligible comparisons is an explicit hold.

What is simulated: all inputs here are caller-supplied records (dataclasses). The
OPE-526 audit receipt and OPE-555 scorecard are consumed only as opaque, digest-bound
references (neither is merged); OPE-528 verdict issuance and OPE-421 packet staging are
modelled by the ``IndependentVerdict`` and ledger records only.

What is NOT proven: tests use generated sentinel data and prove software behaviour only. No
provider, GPU, customer or real-EVE evidence exists here; a generated result can never satisfy
a provider or customer gate. The only authority implementation here is a
sentinel HMAC registry; no identity provider is exercised. Protected-view and critical-slice
facts (``protected_view_seq``, per-case slice tags) remain caller-supplied.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import tempfile
import threading
import unicodedata
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

try:  # POSIX advisory lock for cross-process ledger writers
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]

from opentour_training.evaluation.statistics_kernel import independent_family_interval

DECISION_SCHEMA = "opentour.candidate-staging-decision.v1"
FROZEN_STATE = "frozen_before_candidate_output"
TUNED, INCUMBENT, SIMPLE = "tuned", "incumbent", "simple"
CONTROLS: tuple[str, str] = (INCUMBENT, SIMPLE)
INDEPENDENT_ISSUER_KINDS: frozenset[str] = frozenset(
    {"authenticated_human", "independent_verifier_service"}
)
INTERVAL_METHOD = "bounded_independent_family_hoeffding_v1"

Outcome = Literal["eligible", "hold", "refused"]

# Evidence is merely insufficient or adverse: hold, nothing staged, nothing requested.
HOLD_CODES: frozenset[str] = frozenset(
    {
        "no_eligible_comparisons",
        "inconclusive_support",
        "interval_does_not_clear_threshold",
        "tie_favors_simple",
        "tuned_loses_to_simple",
        "critical_veto",
        "critical_slice_unverified",
        "stale_identity",
        "selection_after_protected_view",
        "fresh_confirmation_required",
        "cost_cap_exceeded",
    }
)


class StagingError(ValueError):
    """Closed refusal of a ledger operation. ``code`` is the stable lowercase reason."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class SimulatedCrash(RuntimeError):
    """Raised by a test fault hook to model a process dying between ledger writes."""


def canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode()


def digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


# ---------------------------------------------------------------- records


@dataclass(frozen=True)
class PreregisteredRule:
    """Predeclared paired fixed-coverage decision rule; its digest is frozen in the manifest."""

    rule_id: str
    alpha: float
    sides: int
    min_effect: float
    min_families: int
    required_controls: tuple[str, ...] = CONTROLS
    max_cost_per_1000: float | None = None

    @property
    def digest(self) -> str:
        return digest(
            [
                self.rule_id,
                self.alpha,
                self.sides,
                self.min_effect,
                self.min_families,
                sorted(self.required_controls),
                self.max_cost_per_1000,
            ]
        )


@dataclass(frozen=True)
class FrozenBenchmark:
    """Everything fixed before any candidate output. The digest binds all of it."""

    benchmark_id: str
    state: str
    scorer_version: str
    operating_point_id: str
    evidence_tier: str
    rule_digest: str
    case_families: tuple[tuple[str, str], ...]  # (case_id, family_id) for every frozen case
    eligible_case_ids: tuple[str, ...]
    critical_slices: tuple[str, ...]
    preregistered_selection_digest: str
    audit_receipt_ref: str | None  # OPE-526 receipt id; opaque here
    audit_receipt_digest: str | None
    audit_verdict: str | None
    frozen_seq: int

    @property
    def digest(self) -> str:
        return digest(
            {
                "benchmark_id": self.benchmark_id,
                "scorer_version": self.scorer_version,
                "operating_point_id": self.operating_point_id,
                "evidence_tier": self.evidence_tier,
                "rule_digest": self.rule_digest,
                "case_families": sorted(self.case_families),
                "eligible_case_ids": sorted(self.eligible_case_ids),
                "critical_slices": sorted(self.critical_slices),
                "preregistered_selection_digest": self.preregistered_selection_digest,
                "audit_receipt_digest": self.audit_receipt_digest,
                "audit_receipt_ref": self.audit_receipt_ref,
                "audit_verdict": self.audit_verdict,
                "state": self.state,
                "frozen_seq": self.frozen_seq,
            }
        )


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    automatic: bool
    wrong: bool
    cost: float = 0.0
    critical_slices: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArmRun:
    arm_id: str
    manifest_digest: str
    serving_identity_digest: str
    produced_seq: int
    results: tuple[CaseResult, ...]

    @property
    def results_digest(self) -> str:
        return digest(
            {
                "arm_id": self.arm_id,
                "manifest_digest": self.manifest_digest,
                "serving_identity_digest": self.serving_identity_digest,
                "produced_seq": self.produced_seq,
                "results": sorted(
                    [r.case_id, r.automatic, r.wrong, r.cost, sorted(r.critical_slices)]
                    for r in self.results
                ),
            }
        )


@dataclass(frozen=True)
class IndependentVerdict:
    verdict_id: str
    arm_id: str
    subject_identity_digest: str
    manifest_digest: str
    run_digest: str
    issuer_id: str
    issuer_kind: str
    issuer_authenticated: bool
    status: str  # "issued" only; "pending" and anything else grants nothing
    outcome: str  # "reproduced" only
    issued_seq: int
    superseded_by: str | None = None
    attestation: str = ""  # authority proof over ``verdict_body``; never self-sufficient


def verdict_body(v: IndependentVerdict) -> dict[str, Any]:
    """Everything an authority must attest (all fields except the attestation itself)."""
    return {
        "verdict_id": v.verdict_id,
        "arm_id": v.arm_id,
        "subject_identity_digest": v.subject_identity_digest,
        "manifest_digest": v.manifest_digest,
        "run_digest": v.run_digest,
        "issuer_id": v.issuer_id,
        "issuer_kind": v.issuer_kind,
        "status": v.status,
        "outcome": v.outcome,
        "issued_seq": v.issued_seq,
        "issuer_authenticated": v.issuer_authenticated,
        "superseded_by": v.superseded_by,
    }


@dataclass(frozen=True)
class CandidateRecord:
    candidate_id: str
    identity_digest: str  # OPE-528 independently checked serving/result identity
    producer_id: str
    selector_id: str
    qualified: bool
    registered: bool
    selection_seq: int
    selection_digest: str


@dataclass(frozen=True)
class FreshConfirmation:
    actor_id: str
    authenticated: bool
    selection_digest: str
    manifest_digest: str
    attestation: str = ""
    # Binding to the exact candidate and arm confirmed; empty never matches a real candidate.
    candidate_id: str = ""
    identity_digest: str = ""
    arm_id: str = ""


def confirmation_body(c: FreshConfirmation) -> dict[str, Any]:
    return {
        "actor_id": c.actor_id,
        "authenticated": c.authenticated,
        "selection_digest": c.selection_digest,
        "manifest_digest": c.manifest_digest,
        "candidate_id": c.candidate_id,
        "identity_digest": c.identity_digest,
        "arm_id": c.arm_id,
    }


class VerdictAuthority(Protocol):
    """Trust root for verdicts and confirmations, held OUTSIDE the packet (OPE-528 boundary)."""

    def verify_verdict(self, verdict: IndependentVerdict) -> bool: ...

    def verify_confirmation(self, confirmation: FreshConfirmation) -> bool: ...


class HmacVerdictAuthority:
    """Sentinel authority: issuer id -> (kind, secret). Not an identity provider.

    Verifies the issuer is registered with the claimed kind and that the HMAC over the full
    verdict body matches. A hand-built record without the key cannot verify.
    """

    def __init__(self, issuers: Mapping[str, tuple[str, bytes]]) -> None:
        self._issuers = dict(issuers)

    @staticmethod
    def mac(secret: bytes, body: object) -> str:
        return hmac.new(secret, canonical(body), hashlib.sha256).hexdigest()

    def verify_verdict(self, verdict: IndependentVerdict) -> bool:
        entry = self._issuers.get(verdict.issuer_id)
        if entry is None or entry[0] != verdict.issuer_kind or not verdict.attestation:
            return False
        return hmac.compare_digest(self.mac(entry[1], verdict_body(verdict)), verdict.attestation)

    def verify_confirmation(self, confirmation: FreshConfirmation) -> bool:
        entry = self._issuers.get(confirmation.actor_id)
        if entry is None or not confirmation.attestation:
            return False
        return hmac.compare_digest(
            self.mac(entry[1], confirmation_body(confirmation)), confirmation.attestation
        )


def identity_key(value: str) -> str:
    """Normalise an actor id so case, whitespace, format characters and compat forms collapse."""
    folded = unicodedata.normalize("NFKC", value).casefold()
    return "".join(
        ch for ch in folded if unicodedata.category(ch)[0] not in {"Z", "C"} and not ch.isspace()
    )


# Ids in this system are generated machine identifiers (e.g. ``generated-verifier-tuned``,
# ``simulated:verdict-1``). Anything outside this small ASCII allowlist is not a well-formed id,
# and a denylist of invisible, blank, combining and look-alike code points cannot be complete.
_ACTOR_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@-]{0,127}")


def _same_actor(actor: str, *others: str) -> bool:
    """True when ``actor`` is, or cannot be told apart from, any of ``others``.

    Fails closed: an id (the actor or any compared id) outside the printable-ASCII allowlist is
    never trusted to be distinct, so it counts as the same actor. That can only cause a false
    self-issue refusal, never admit a self-issued verdict. ``identity_key`` still collapses case,
    whitespace and compatibility forms for ids that are well formed.
    """
    if not all(_ACTOR_ID_PATTERN.fullmatch(i) for i in (actor, *others)):
        return True
    key = identity_key(actor)
    return not key or any(key == identity_key(other) for other in others)


@dataclass(frozen=True)
class StagingPacket:
    benchmark: FrozenBenchmark
    rule: PreregisteredRule
    candidate: CandidateRecord
    runs: Mapping[str, ArmRun]
    verdicts: tuple[IndependentVerdict, ...]
    protected_view_seq: int | None = None
    fresh_confirmation: FreshConfirmation | None = None
    optional_runs: Mapping[str, ArmRun] = field(default_factory=dict)  # e.g. augmented, real_only


@dataclass(frozen=True)
class StagingDecision:
    outcome: Outcome
    reasons: tuple[str, ...]
    candidate_id: str
    identity_digest: str
    manifest_digest: str
    rule_digest: str
    evidence_tier: str
    comparisons: dict[str, Any]
    optional_comparisons: dict[str, Any]
    verdict_ids: tuple[str, ...]
    improvement_claim: str

    # Constants of the contract, not options: staging never grants or requests release.
    alerts_emitted: int = 0
    release_requests: int = 0
    release_authority: bool = False
    customer_win_claim: bool = False

    @property
    def claim_scope(self) -> str:
        return "implementation_only" if self.evidence_tier == "generated" else "tier_limited"

    def body(self) -> dict[str, Any]:
        return {
            "schema_version": DECISION_SCHEMA,
            "outcome": self.outcome,
            "reasons": list(self.reasons),
            "candidate_id": self.candidate_id,
            "identity_digest": self.identity_digest,
            "manifest_digest": self.manifest_digest,
            "rule_digest": self.rule_digest,
            "evidence_tier": self.evidence_tier,
            "claim_scope": self.claim_scope,
            "comparisons": self.comparisons,
            "optional_comparisons": self.optional_comparisons,
            "verdict_ids": list(self.verdict_ids),
            "improvement_claim": self.improvement_claim,
            "alerts_emitted": self.alerts_emitted,
            "release_requests": self.release_requests,
            "release_authority": self.release_authority,
            "customer_win_claim": self.customer_win_claim,
        }

    @property
    def digest(self) -> str:
        return digest(self.body())


# ---------------------------------------------------------------- evaluation


def _valid_rule(rule: PreregisteredRule, reasons: set[str]) -> None:
    controls = set(rule.required_controls)
    if SIMPLE not in controls:
        reasons.add("simple_comparator_missing")
    if INCUMBENT not in controls:
        reasons.add("incumbent_comparator_missing")
    ok = (
        controls == set(CONTROLS)
        and isinstance(rule.alpha, float)
        and math.isfinite(rule.alpha)
        and 0 < rule.alpha < 0.5
        and rule.sides in (1, 2)
        and isinstance(rule.min_effect, float)
        and math.isfinite(rule.min_effect)
        and 0.0 <= rule.min_effect < 1.0
        and rule.min_families >= 2
        and (rule.max_cost_per_1000 is None or rule.max_cost_per_1000 >= 0)
    )
    if not ok:
        reasons.add("invalid_rule")


def _run_map(run: ArmRun, bench: FrozenBenchmark, reasons: set[str]) -> dict[str, CaseResult]:
    mapped: dict[str, CaseResult] = {}
    for item in run.results:
        valid = (
            math.isfinite(item.cost) and item.cost >= 0 and not (item.wrong and not item.automatic)
        )
        if not valid:
            reasons.add("invalid_arm_result")
        if item.case_id in mapped:
            reasons.add("denominator_changed")
        mapped[item.case_id] = item
    if run.manifest_digest != bench.digest:
        reasons.add("manifest_mismatch")
    if set(mapped) != set(bench.eligible_case_ids) or len(run.results) != len(
        bench.eligible_case_ids
    ):
        reasons.add("denominator_changed")
    if run.produced_seq <= bench.frozen_seq:
        reasons.add("win_measured_before_freeze")
    return mapped


def _check_verdicts(
    packet: StagingPacket,
    runs: Mapping[str, ArmRun],
    reasons: set[str],
    authority: VerdictAuthority | None,
) -> tuple[str, ...]:
    cand, bench = packet.candidate, packet.benchmark
    chosen: list[str] = []
    for arm in (TUNED, INCUMBENT, SIMPLE):
        mine = [v for v in packet.verdicts if v.arm_id == arm]
        live = [v for v in mine if v.superseded_by is None]
        if not mine:
            reasons.add("verdict_missing")
            continue
        if not live:
            reasons.add("verdict_superseded")
            continue
        if len(live) > 1:
            reasons.add("verdict_ambiguous")
            continue
        verdict = live[0]
        chosen.append(verdict.verdict_id)
        if verdict.status != "issued":
            reasons.add("verdict_not_issued")
        if verdict.issuer_kind not in INDEPENDENT_ISSUER_KINDS:
            reasons.add("verdict_issuer_not_authority")
        if not verdict.issuer_authenticated:
            reasons.add("verdict_unauthenticated")
        if _same_actor(verdict.issuer_id, cand.producer_id, cand.selector_id):
            reasons.add("verdict_self_issued")
        if authority is None:
            reasons.add("verdict_authority_unavailable")
        elif not authority.verify_verdict(verdict):
            reasons.add("verdict_unattested")
        if verdict.outcome != "reproduced":
            reasons.add("verdict_not_reproduced")
        run = runs.get(arm)
        if run is not None and (
            verdict.manifest_digest != bench.digest
            or verdict.run_digest != run.results_digest
            or verdict.subject_identity_digest != run.serving_identity_digest
        ):
            reasons.add("verdict_stale")
        if run is not None and verdict.issued_seq <= run.produced_seq:
            reasons.add("verdict_before_output")
    return tuple(sorted(chosen))


def _compare(
    control: str,
    tuned: Mapping[str, CaseResult],
    other: Mapping[str, CaseResult],
    bench: FrozenBenchmark,
    rule: PreregisteredRule,
) -> dict[str, Any]:
    family_of = dict(bench.case_families)
    per_family: dict[str, list[int]] = {}  # family -> [automatic, control_wrong, tuned_wrong]
    fixes = regressions = 0
    for case_id in sorted(tuned):
        t, c = tuned[case_id], other[case_id]
        if not t.automatic:
            continue
        cell = per_family.setdefault(family_of[case_id], [0, 0, 0])
        cell[0] += 1
        cell[1] += int(c.wrong)
        cell[2] += int(t.wrong)
        fixes += int(c.wrong and not t.wrong)
        regressions += int(t.wrong and not c.wrong)
    effects = [(fam, (cell[1] - cell[2]) / cell[0]) for fam, cell in sorted(per_family.items())]
    automatic = sum(cell[0] for cell in per_family.values())
    point = lower = upper = None
    unavailable: str | None = None
    if len(effects) >= 2:
        try:
            est = independent_family_interval(effects, rule.alpha, rule.sides)  # type: ignore[arg-type]
            point, lower, upper = est.value, est.lower, est.upper
            unavailable = est.undefined_reason
        except (ValueError, ArithmeticError):
            unavailable = "kernel_refused"
    else:
        unavailable = "no_gradeable_units" if not effects else "singleton"
    slices: dict[str, Any] = {}
    for name in sorted(bench.critical_slices):
        members = [c for c in tuned if name in tuned[c].critical_slices and tuned[c].automatic]
        slices[name] = {
            "automatic_cases": len(members),
            "tuned_wrong": sum(int(tuned[c].wrong) for c in members),
            "control_wrong": sum(int(other[c].wrong) for c in members),
            "regressions": sum(int(tuned[c].wrong and not other[c].wrong) for c in members),
        }

    def per_1000(arm: Mapping[str, CaseResult]) -> float | None:
        auto = [r.cost for r in arm.values() if r.automatic]
        return round(1000 * sum(auto) / len(auto), 6) if auto else None

    return {
        "control": control,
        "eligible_cases": len(tuned),
        "automatic_cases": automatic,
        "coverage": [automatic, len(tuned)],
        "families": len(effects),
        "tuned_wrong": sum(cell[2] for cell in per_family.values()),
        "control_wrong": sum(cell[1] for cell in per_family.values()),
        "fixes": fixes,
        "regressions": regressions,
        "effect": point,
        "interval": {
            "method": INTERVAL_METHOD,
            "alpha": rule.alpha,
            "sides": rule.sides,
            "lower": lower,
            "upper": upper,
            "undefined_reason": unavailable,
            "descriptive_only": True,
        },
        "threshold": rule.min_effect,
        "critical_slices": slices,
        "cost_per_1000": {"tuned": per_1000(tuned), "control": per_1000(other)},
    }


def _optional(packet: StagingPacket, eligible: set[str]) -> dict[str, Any]:
    report: dict[str, Any] = {}
    if not packet.optional_runs:
        return {"augmented_vs_real_only": {"status": "unrun", "blocks_base_route": False}}
    for arm_id, run in sorted(packet.optional_runs.items()):
        same = {r.case_id for r in run.results} == eligible and len(run.results) == len(eligible)
        auto = [r for r in run.results if r.automatic]
        report[arm_id] = {
            "status": "reported_not_gating",
            "same_denominator": same,
            "automatic": len(auto),
            "wrong": sum(int(r.wrong) for r in auto),
            "blocks_base_route": False,
        }
    return report


def evaluate(packet: StagingPacket, authority: VerdictAuthority | None = None) -> StagingDecision:
    """Pure gate. Default is not staged; ``eligible`` requires every gate affirmatively."""
    bench, rule, cand = packet.benchmark, packet.rule, packet.candidate
    reasons: set[str] = set()
    _valid_rule(rule, reasons)
    if bench.state != FROZEN_STATE:
        reasons.add("benchmark_not_frozen")
    if rule.digest != bench.rule_digest:
        reasons.add("rule_not_frozen")
    if bench.audit_receipt_ref is None or bench.audit_receipt_digest is None:
        reasons.add("audit_receipt_missing")
    elif bench.audit_verdict != "evaluation_allowed":
        reasons.add("audit_receipt_not_allowed")
    frozen_cases = {case for case, _ in bench.case_families}
    if (
        len(frozen_cases) != len(bench.case_families)
        or not set(bench.eligible_case_ids) <= frozen_cases
        or len(set(bench.eligible_case_ids)) != len(bench.eligible_case_ids)
    ):
        reasons.add("denominator_changed")
    if not bench.eligible_case_ids:
        reasons.add("no_eligible_comparisons")
    if not cand.qualified:
        reasons.add("candidate_unqualified")
    if not cand.registered:
        reasons.add("candidate_unregistered")

    for arm, code in ((TUNED, "tuned_run_missing"), (INCUMBENT, "incumbent_run_missing")):
        if arm not in packet.runs:
            reasons.add(code)
    if SIMPLE not in packet.runs:
        reasons.add("simple_comparator_missing")
    maps = {arm: _run_map(run, bench, reasons) for arm, run in packet.runs.items()}
    if any(run.arm_id != arm for arm, run in packet.runs.items()):
        reasons.add("arm_identity_mismatch")
    tuned_run = packet.runs.get(TUNED)
    if tuned_run is not None and tuned_run.serving_identity_digest != cand.identity_digest:
        reasons.add("stale_identity")
    verdict_ids = _check_verdicts(packet, packet.runs, reasons, authority)

    if packet.protected_view_seq is not None and cand.selection_seq >= packet.protected_view_seq:
        reasons.add("selection_after_protected_view")
    if cand.selection_digest != bench.preregistered_selection_digest:
        fresh = packet.fresh_confirmation
        if (
            fresh is None
            or not fresh.authenticated
            or fresh.selection_digest != cand.selection_digest
            or fresh.manifest_digest != bench.digest
            or not cand.candidate_id
            or fresh.candidate_id != cand.candidate_id
            or fresh.identity_digest != cand.identity_digest
            or fresh.arm_id != TUNED
            or _same_actor(fresh.actor_id, cand.producer_id, cand.selector_id)
            or authority is None
            or not authority.verify_confirmation(fresh)
        ):
            reasons.add("fresh_confirmation_required")

    comparisons: dict[str, Any] = {}
    structural = {
        "denominator_changed",
        "manifest_mismatch",
        "invalid_arm_result",
        "tuned_run_missing",
        "incumbent_run_missing",
        "simple_comparator_missing",
        "arm_identity_mismatch",
    }
    if len(maps) == 3 and not reasons & (structural | {"no_eligible_comparisons"}):
        autos = {arm: {c for c, r in m.items() if r.automatic} for arm, m in maps.items()}
        tags = {
            arm: {c: frozenset(r.critical_slices) for c, r in m.items()} for arm, m in maps.items()
        }
        if not (tags[TUNED] == tags[INCUMBENT] == tags[SIMPLE]):
            reasons.add("critical_slice_membership_inconsistent")
        elif not (autos[TUNED] == autos[INCUMBENT] == autos[SIMPLE]):
            reasons.add("coverage_not_fixed")
        else:
            for control in CONTROLS:
                comparisons[control] = _compare(control, maps[TUNED], maps[control], bench, rule)
            _judge(comparisons, rule, reasons)

    claim = "none"
    if not reasons:
        claim = "cleared_preregistered_rule_against_both_controls"
    outcome: Outcome = (
        "eligible" if not reasons else ("hold" if reasons <= HOLD_CODES else "refused")
    )
    return StagingDecision(
        outcome=outcome,
        reasons=tuple(sorted(reasons)),
        candidate_id=cand.candidate_id,
        identity_digest=cand.identity_digest,
        manifest_digest=bench.digest,
        rule_digest=rule.digest,
        evidence_tier=bench.evidence_tier,
        comparisons=comparisons,
        optional_comparisons=_optional(packet, set(bench.eligible_case_ids)),
        verdict_ids=verdict_ids,
        improvement_claim=claim,
    )


def _judge(comparisons: dict[str, Any], rule: PreregisteredRule, reasons: set[str]) -> None:
    for control, report in comparisons.items():
        interval = report["interval"]
        if report["families"] < rule.min_families or interval["lower"] is None:
            reasons.add("inconclusive_support")
            continue
        point = report["effect"]
        if control == SIMPLE and point <= 0:
            reasons.add("tie_favors_simple" if point == 0 else "tuned_loses_to_simple")
        elif not interval["lower"] > rule.min_effect:
            reasons.add("interval_does_not_clear_threshold")
        for cell in report["critical_slices"].values():
            if cell["automatic_cases"] == 0:
                reasons.add("critical_slice_unverified")
            if cell["regressions"] > 0:
                reasons.add("critical_veto")
        cost = report["cost_per_1000"]["tuned"]
        if rule.max_cost_per_1000 is not None and (cost is None or cost > rule.max_cost_per_1000):
            reasons.add("cost_cap_exceeded")


# ---------------------------------------------------------------- ledger


@dataclass(frozen=True)
class SupersessionReceipt:
    receipt_id: str
    superseded_manifest_digest: str
    successor_manifest_digest: str
    issuer_id: str
    issuer_authenticated: bool


@dataclass(frozen=True)
class StageResult:
    status: Literal["staged", "already_staged", "not_staged"]
    decision: StagingDecision
    reasons: tuple[str, ...]


def _key(candidate_id: str, identity_digest: str) -> str:
    return f"{candidate_id}@{identity_digest}"


class StagingLedger:
    """Durable, idempotent staging state. ``staged`` is visible only after one atomic write.

    ``stage`` writes a ``decided`` intent and then the ``staged`` record in a second atomic
    write. A crash between them leaves the candidate NOT staged; replaying the same packet
    completes it exactly once. Duplicate or late callbacks never alter state.
    """

    def __init__(
        self,
        path: Path | None = None,
        *,
        fault: Callable[[str], None] | None = None,
        authority: VerdictAuthority | None = None,
    ) -> None:
        self._path = path
        self._fault = fault
        self._authority = authority
        self._lock = threading.Lock()
        self._memory: dict[str, Any] = self._empty()
        if path is not None and path.exists():
            self._memory = json.loads(path.read_text())

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"decided": {}, "staged": {}, "superseded": {}, "reconciliation": [], "acks": {}}

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialise writers across threads AND processes, then re-read the durable state.

        Without the reload a second process holding a stale in-memory copy could restage an
        already-staged candidate or overwrite another writer's record (lost update).
        """
        with self._lock:
            if self._path is None or fcntl is None:
                yield
                return
            lock_path = self._path.with_name(self._path.name + ".lock")
            with open(lock_path, "a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                try:
                    if self._path.exists():
                        self._memory = json.loads(self._path.read_text())
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def _save(self, state: dict[str, Any]) -> None:
        if self._path is not None:
            fd, tmp = tempfile.mkstemp(dir=self._path.parent, prefix=".staging-")
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(canonical(state).decode())
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self._path)
                dir_fd = os.open(self._path.parent, os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise
        self._memory = state

    def _copy(self) -> dict[str, Any]:
        copied: dict[str, Any] = json.loads(canonical(self._memory))
        return copied

    def _hook(self, point: str) -> None:
        if self._fault is not None:
            self._fault(point)

    def snapshot(self) -> dict[str, Any]:
        return self._copy()

    def is_staged(self, candidate_id: str, identity_digest: str) -> bool:
        record = self._memory["staged"].get(_key(candidate_id, identity_digest))
        return bool(record and record["status"] == "staged")

    def pending(self) -> tuple[str, ...]:
        """Decided-but-not-staged intents left by a crash. They are not staged."""
        return tuple(sorted(self._memory["decided"]))

    def reconciliation(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._memory["reconciliation"])

    def stage(self, packet: StagingPacket) -> StageResult:
        decision = evaluate(packet, self._authority)
        with self._exclusive():
            state = self._copy()
            key = _key(decision.candidate_id, decision.identity_digest)
            reasons = list(decision.reasons)
            if decision.manifest_digest in state["superseded"]:
                reasons = sorted({*reasons, "benchmark_superseded"})
            if reasons:
                if key in state["decided"]:  # a stale intent must not survive a failed gate
                    del state["decided"][key]
                    self._save(state)
                return StageResult("not_staged", decision, tuple(reasons))
            existing = state["staged"].get(key)
            if existing is not None:
                if existing["decision_digest"] == decision.digest and (
                    existing["status"] == "staged"
                ):
                    return StageResult("already_staged", decision, ())
                return StageResult(
                    "not_staged", decision, ("candidate_already_staged_or_under_reconciliation",)
                )
            state["decided"][key] = {
                "decision_digest": decision.digest,
                "manifest_digest": decision.manifest_digest,
            }
            self._save(state)
            self._hook("after_decided")
            state = self._copy()
            del state["decided"][key]
            state["staged"][key] = {
                "candidate_id": decision.candidate_id,
                "identity_digest": decision.identity_digest,
                "decision_digest": decision.digest,
                "manifest_digest": decision.manifest_digest,
                "status": "staged",
            }
            self._save(state)
            return StageResult("staged", decision, ())

    def acknowledge(self, callback_id: str, decision_digest: str) -> Literal["recorded", "ignored"]:
        """Record a worker staging callback once. Late or duplicate callbacks change nothing."""
        with self._exclusive():
            state = self._copy()
            live = [
                k
                for k, rec in state["staged"].items()
                if rec["decision_digest"] == decision_digest and rec["status"] == "staged"
            ]
            if not live or decision_digest in state["acks"]:
                return "ignored"
            state["acks"][decision_digest] = callback_id
            self._save(state)
            return "recorded"

    def supersede_benchmark(self, receipt: SupersessionReceipt) -> tuple[dict[str, Any], ...]:
        """Block new promotion on a superseded benchmark and queue earlier disclosures."""
        if (
            not receipt.issuer_authenticated
            or not receipt.receipt_id
            or receipt.superseded_manifest_digest == receipt.successor_manifest_digest
        ):
            raise StagingError("invalid_supersession_receipt")
        with self._exclusive():
            state = self._copy()
            old = receipt.superseded_manifest_digest
            if old in state["superseded"]:
                return tuple(i for i in state["reconciliation"] if i["manifest_digest"] == old)
            state["superseded"][old] = {
                "receipt_id": receipt.receipt_id,
                "successor_manifest_digest": receipt.successor_manifest_digest,
            }
            for key in [k for k, v in state["decided"].items() if v["manifest_digest"] == old]:
                del state["decided"][key]
            items = []
            for rec in state["staged"].values():
                if rec["manifest_digest"] == old and rec["status"] == "staged":
                    rec["status"] = "reconcile_required"
                    items.append(
                        {
                            "candidate_id": rec["candidate_id"],
                            "identity_digest": rec["identity_digest"],
                            "decision_digest": rec["decision_digest"],
                            "manifest_digest": old,
                            "successor_manifest_digest": receipt.successor_manifest_digest,
                            "reason": "benchmark_superseded_review_prior_disclosure",
                        }
                    )
            state["reconciliation"].extend(sorted(items, key=lambda i: i["candidate_id"]))
            self._save(state)
            return tuple(sorted(items, key=lambda i: i["candidate_id"]))
