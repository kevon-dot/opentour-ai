"""Stage one verified comparison packet and exactly one worker completion event (OPE-421).

Worker/backend side only. The hosted completion Zap and the version-bound Slack message are
owned by Codex (OPE-550 and the Slack approval listener); this module only produces the
least-disclosure packet and the single outbox event those consumers read.

What this module does:
  * assembles a packet from a frozen protected comparison (``comparison_plan`` RESULT) only when
    that comparison, its benchmark audit receipt (OPE-526 shape) and its independent verdict
    (OPE-528 interface) agree on exact digests, denominator and expiry;
  * refuses, with no packet, no event and no authority, anything missing, unverified, stale,
    superseded, scope-limited, expired, re-denominated or on a branch this ticket does not own;
  * commits the packet and its one outbox event in a single transaction, and delivers that event
    at-least-once to a sink that deduplicates on (resource, event id, event revision);
  * fences prior packets on a changed benchmark and reconciles earlier disclosures when a
    receipt is superseded.

What is simulated: every input is a generated sentinel. The audit receipt follows the OPE-526
receipt shape (which is not yet on ``main``); the independent verdict shape here is a PROPOSAL
pending OPE-528. The audit receipt is checked for digest self-consistency only: neither
document carries a signature, so authenticity of the issuer is NOT proven. The sink is a test
double.

What is NOT proven: no provider, Zapier, Slack or customer behavior; no installed-provider or
customer evidence; no OPE-526 receipt issued by a landed audit; no OPE-528 verdict from an
independent verifier. Passing tests prove software behavior over generated data only. A packet
is never a release decision, grants no release authority and triggers no delivery.

Exactly-one claim, stated precisely: the packet and its single outbox row commit in one
transaction (zero or one of each, never two). Delivery is AT-LEAST-ONCE under the stable key
(resource, event id, event revision). The exactly-one EFFECT therefore depends on the consumer
deduplicating on that key; here that is proven only against a test double, never against the
Codex-side consumer. This module does not and cannot claim exactly-once delivery.

Authenticity: digests alone cannot authenticate an auditor or independent verifier.
Improvement staging holds until this boundary can verify both issuers. An explicit fixture
option exercises the staging mechanics with generated evidence only.

DELIBERATE DECISION, NOT AN UNFINISHED FEATURE. No signing or key-custody contract exists in
this project, so no auditor or independent-verifier issuer can be verified here, and
``issuer_authenticated`` is hard-coded ``False``. Live (non-fixture) measured-improvement
staging therefore always holds with ``audit_and_verdict_issuers_unverified``. There is no
re-enable path short of a code change, and that is intended. It stands until a signing contract
lands. Lifting it requires, together: (1) a landed contract defining issuer keys, custody and
rotation for the OPE-526 audit receipt and the OPE-528 verdict; (2) signature verification of
both documents against pinned issuer keys inside this module, replacing the hard-coded
``False``; (3) tests that a forged, unsigned, wrong-key or replayed document is refused; and
(4) independent review of that change. Do not "fix" the hold by loosening the gate, widening
``allow_generated_fixture`` or trusting a self-asserted ``evidence_origin``. Hold and no-change
packets are not gated by this and are delivered.

Fixture flag: ``allow_generated_fixture`` exercises staging mechanics with generated evidence
only. It is honored solely in a test context (``PYTEST_CURRENT_TEST`` is set, as pytest does).
Outside one, ``PacketOutbox`` refuses to construct and ``assemble_packet`` refuses, with the
closed code ``generated_fixture_not_permitted_in_live_context``, so a misconfiguration is loud.

Free text: a sealed comparison proves only that its bytes are self-consistent, not that its
strings are safe. Every string that this module copies from a comparison into a packet (the
decision reason, fallback and disposition, the winner, the metric, each arm's hold reason, arm
and pair names, comparison verdicts, slice statuses, economics basis, uncertainty method) is
checked against a closed vocabulary derived from what ``comparison_plan`` can emit, on every
path including holds. A value outside its vocabulary refuses the comparison with the fixed code
``comparison_unverified``, which never echoes the offending value.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from collections.abc import Callable, Iterator, Mapping
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.comparison_plan import RESULT
from opentour_cell.pilot.operating_point import canonical, digest

Json = dict[str, Any]
PACKET = "opentour.pilot.comparison-packet.v1"
EVENT = "opentour.pilot.comparison-completed-event.v1"
VERDICT = "opentour.pilot.independent-verdict.v1"
RECEIPT = "opentour.benchmark-audit-receipt.v1"
EVENT_REVISION = 1
_FIXTURE_REFUSAL = "generated_fixture_not_permitted_in_live_context"
_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:@/-]{0,127}")
_SHA = re.compile(r"[0-9a-f]{64}")
_REPRODUCED = "reproduced"
_ALLOWED = "evaluation_allowed"
_ORIGINS = ("generated", "observed")
_VERDICTS = (
    "evaluation_allowed",
    "starter_only",
    "claim_limited_to_named_population",
    "insufficient_evidence",
    "invalid",
    "stale",
)
# Closed vocabularies for strings copied from a comparison into a packet. Derived from what
# ``comparison_plan`` (``_pick``, ``_checks``, ``development_hold``, ``_score``) and
# ``simple_repair.assess_development`` can emit. Tuples, not sets: membership must never raise
# on an unhashable value.
_HOLD_REASONS = (
    None,
    "insufficient_protected_support",
    "incomplete_protected_evidence",
    "operating_point_missed_or_undetermined",
    "critical_slice_failed_or_unverified",
    "cost_limit_failed_or_unverified",
    "latency_limit_failed_or_unverified",
)
_OBSERVED = ("improved", "regressed", "equivalent", "insufficient_evidence")
_DECISION_REASONS = (
    "protected_improvement_at_agreed_limits",
    "adequate_simple_repair_at_lower_observed_cost",
    "no_confirmed_improvement",
    "equivalent_to_incumbent",
    "quality_cost_tradeoff_requires_owner_choice",
    *(r for r in _HOLD_REASONS if r),
    *(r for r in _OBSERVED if r not in ("improved", "equivalent")),
    # Development-stage hold reasons carried by ``development_hold``.
    "insufficient_independent_development_evidence",
    "recurring_residual_requires_model_and_compute_rights",
    "simple_repair_unproven_and_residual_support_insufficient",
)
_FALLBACKS = ("incumbent",)
_METRICS = (None, "verified_wrong_automatic")  # the planner default; a custom metric is refused
_DISPOSITIONS = ("recommend", "hold", "no_change")
_WINNERS = (None, "incumbent", "simple", "model")
_ARM_NAMES = ("incumbent", "simple", "model")
_PAIR_NAMES = ("incumbent_vs_simple", "incumbent_vs_model", "simple_vs_model")
_SLICE_STATUSES = ("met", "missed", "undetermined")
_ECON_BASES = ("unavailable", "observed", "modeled", "generated")
_UNCERTAINTY = ("wilson_95_independent_verified_cases", "unavailable")
_PAIRED_FIELDS = (
    "comparable",
    "excluded",
    "fixes",
    "regressions",
    "ties_correct",
    "ties_incorrect",
)
_ARM_FIELDS = ("automatic", "incorrect_automatic", "review", "missing")
_ECON_FIELDS = (
    "basis",
    "cost_per_1000",
    "review_minutes_per_1000",
    "mean_latency_ms",
    "total_latency_ms_per_1000",
    "cost_cap",
    "latency_cap",
)


def _fixture_context() -> bool:
    """True only under a test runner. The repository has no other live/fixture switch."""
    return "PYTEST_CURRENT_TEST" in os.environ


class PacketStoreError(RuntimeError):
    """Safe diagnostic for a corrupt or unusable local outbox."""


@dataclass(frozen=True)
class PacketContext:
    """Bindings the worker resolves from its current connection and mandate, never the caller."""

    tenant: str
    recipient: str
    mandate_revision: str
    run_ref: str
    proposal_version: str
    benchmark_digest: str
    remedy_digest: str | None
    fallback_digest: str
    expires_at: int
    previous_deployed_version: str | None
    worker_id: str

    def valid(self) -> bool:
        ids = (
            self.tenant,
            self.recipient,
            self.mandate_revision,
            self.run_ref,
            self.proposal_version,
            self.worker_id,
        )
        digests = (self.benchmark_digest, self.fallback_digest)
        return (
            all(isinstance(v, str) and _ID.fullmatch(v) for v in ids)
            and all(isinstance(v, str) and _SHA.fullmatch(v) for v in digests)
            and (self.remedy_digest is None or bool(_SHA.fullmatch(self.remedy_digest)))
            and isinstance(self.expires_at, int)
            and not isinstance(self.expires_at, bool)
            and (
                self.previous_deployed_version is None
                or bool(_ID.fullmatch(self.previous_deployed_version))
            )
        )

    def as_json(self) -> Json:
        return {
            "tenant": self.tenant,
            "recipient": self.recipient,
            "mandate_revision": self.mandate_revision,
            "run_ref": self.run_ref,
            "proposal_version": self.proposal_version,
            "benchmark_digest": self.benchmark_digest,
            "remedy_digest": self.remedy_digest,
            "fallback_digest": self.fallback_digest,
            "expires_at": self.expires_at,
            "previous_deployed_version": self.previous_deployed_version,
        }


class AuthorityView(Protocol):
    """Injected boundary: the worker's current mandate. ``None`` means unavailable."""

    def current(self, run_ref: str) -> PacketContext | None: ...


class EventSink(Protocol):
    """Injected boundary: the consumer. It must deduplicate on the event's idempotency key."""

    def deliver(self, event: Json) -> None: ...


@dataclass(frozen=True)
class StageOutcome:
    status: str  # staged | already_staged | refused
    reason: str | None = None
    packet: Json | None = None
    event_id: str | None = None
    scoped_hold: Json | None = None


def _refuse(reason: str) -> StageOutcome:
    # A failed, missing or inconclusive receipt is displayed as a scoped hold; it acquires no
    # release authority and creates neither a packet nor an event.
    return StageOutcome(
        "refused",
        reason,
        scoped_hold={
            "scope": "measured_improvement_proposal",
            "state": "hold",
            "reason": reason,
            "release_authority": False,
        },
    )


def _sealed(value: Mapping[str, Any], field: str) -> bool:
    return value.get(field) == digest({k: v for k, v in value.items() if k != field})


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _digest_ok(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA.fullmatch(value))


def _arm_vocab_ok(arm: Any) -> bool:
    """Closed-vocabulary and type checks for everything ``_arm_aggregate`` copies."""
    if not isinstance(arm, dict) or not isinstance(arm.get("metrics"), dict):
        return False
    metrics = arm["metrics"]
    slices = arm.get("critical_slices")
    economics = metrics.get("economics")
    uncertainty = metrics.get("uncertainty")
    return (
        arm.get("hold_reason") in _HOLD_REASONS
        and _digest_ok(arm.get("revision"))
        and _digest_ok(arm.get("prediction_digest"))
        and all(_count(metrics.get(k)) for k in _ARM_FIELDS)
        and isinstance(slices, dict)
        and all(isinstance(v, dict) and v.get("status") in _SLICE_STATUSES for v in slices.values())
        and isinstance(economics, dict)
        and economics.get("basis") in _ECON_BASES
        and isinstance(uncertainty, dict)
        and uncertainty.get("method") in _UNCERTAINTY
    )


def _comparison_vocab_ok(comparison: Json) -> bool:
    """Free text cannot reach a packet or a refusal: every copied string is from a closed set.

    Runs on every path (hold, no-change and improvement) because a hold packet is delivered
    without any receipt or verdict.
    """
    decision = comparison["decision"]
    arms, pairs = comparison["arms"], comparison["comparisons"]
    return (
        decision.get("reason") in _DECISION_REASONS
        and decision.get("fallback") in _FALLBACKS
        and decision.get("disposition") in _DISPOSITIONS
        and decision.get("winner") in _WINNERS
        and comparison.get("metric") in _METRICS
        and all(name in _ARM_NAMES for name in arms)
        and all(_arm_vocab_ok(arm) for arm in arms.values())
        and all(name in _PAIR_NAMES for name in pairs)
        and all(
            isinstance(pair, dict)
            and pair.get("observed_comparison") in _OBSERVED
            and isinstance(pair.get("paired"), dict)
            and pair["paired"].keys() == set(_PAIRED_FIELDS)
            and all(_count(v) for v in pair["paired"].values())
            for pair in pairs.values()
        )
        and _digest_ok(comparison.get("plan_revision"))
        and _digest_ok(comparison.get("protected_membership_digest"))
    )


def _comparison_ok(comparison: Any) -> bool:
    return (
        isinstance(comparison, dict)
        and comparison.get("schema_version") == RESULT
        and _sealed(comparison, "evidence_revision")
        and isinstance(comparison.get("protected_rows_evaluated"), int)
        and isinstance(comparison.get("decision"), dict)
        and isinstance(comparison.get("arms"), dict)
        and isinstance(comparison.get("comparisons"), dict)
        and _comparison_vocab_ok(comparison)
    )


def _receipt_ok(receipt: Any) -> bool:
    if not isinstance(receipt, dict) or receipt.get("schema_version") != RECEIPT:
        return False
    body = {k: v for k, v in receipt.items() if k not in ("receipt_digest", "receipt_id")}
    bindings = receipt.get("bindings")
    return (
        isinstance(receipt.get("receipt_digest"), str)
        and receipt["receipt_digest"] == digest(body)
        and receipt.get("receipt_id") == "audit-" + receipt["receipt_digest"][:16]
        and receipt.get("verdict") in _VERDICTS
        and receipt.get("evidence_origin", "generated") in _ORIGINS
        and isinstance(bindings, dict)
        and isinstance(bindings.get("frozen"), dict)
        and isinstance(bindings.get("current"), dict)
    )


def _verdict_ok(verdict: Any) -> bool:
    return (
        isinstance(verdict, dict)
        and verdict.get("schema_version") == VERDICT
        and _sealed(verdict, "verdict_digest")
        and isinstance(verdict.get("denominator"), dict)
    )


def _denominator_ok(comparison: Json) -> bool:
    total = comparison["protected_rows_evaluated"]
    for arm in comparison["arms"].values():
        if arm["metrics"]["coverage"]["denominator"] != total:
            return False
        if arm["metrics"]["observed_error_rate"]["denominator"] > total:
            return False
    return all(
        pair["paired"]["comparable"] + pair["paired"]["excluded"] <= total
        for pair in comparison["comparisons"].values()
    )


def _arm_aggregate(arm: Json) -> Json:
    metrics = arm["metrics"]
    economics = metrics["economics"]
    return {
        "revision": arm["revision"],
        "prediction_digest": arm["prediction_digest"],
        "counts": {k: metrics[k] for k in _ARM_FIELDS},
        "coverage": [metrics["coverage"]["numerator"], metrics["coverage"]["denominator"]],
        "wrong_automatic": [
            metrics["observed_error_rate"]["numerator"],
            metrics["observed_error_rate"]["denominator"],
        ],
        "uncertainty": {
            k: metrics["uncertainty"].get(k) for k in ("method", "lower", "upper", "small_sample")
        },
        "operating_point": metrics["operating_point"]["combined"],
        "critical_slices": {k: v["status"] for k, v in arm["critical_slices"].items()},
        "hold_reason": arm["hold_reason"],
        "economics": {k: economics[k] for k in _ECON_FIELDS},
    }


def _missing_units(arms: Json) -> list[Json]:
    found = []
    for name, arm in sorted(arms.items()):
        economics = arm["economics"]
        if economics["basis"] == "unavailable":
            found.append({"arm": name, "unit": "review_time_runtime_and_spend"})
    return found


def _assemble(
    comparison: Json,
    receipt: Json | None,
    verdict: Json | None,
    ctx: PacketContext,
    *,
    allow_generated_fixture: bool,
) -> tuple[Json | None, str | None]:
    """Pure and deterministic: identical inputs reproduce the identical packet digest."""
    decision = comparison["decision"]
    winner = decision.get("winner")
    improvement = decision.get("disposition") == "recommend"
    if improvement and winner != "simple":
        return None, "model_winner_branch_not_in_this_packet"
    if improvement:
        failure = _improvement_checks(comparison, receipt, verdict, ctx, winner)
        if failure:
            return None, failure
        assert receipt is not None
        if not (allow_generated_fixture and receipt.get("evidence_origin") == "generated"):
            return None, "audit_and_verdict_issuers_unverified"
    arms = {name: _arm_aggregate(arm) for name, arm in sorted(comparison["arms"].items())}
    pairs = {
        name: {**pair["paired"], "observed_comparison": pair["observed_comparison"]}
        for name, pair in sorted(comparison["comparisons"].items())
    }
    origin = receipt["evidence_origin"] if receipt and "evidence_origin" in receipt else None
    packet: Json = {
        "schema_version": PACKET,
        "kind": "measured_improvement" if improvement else "hold",
        "binding": ctx.as_json(),
        "comparison": {
            "evidence_revision": comparison["evidence_revision"],
            "plan_revision": comparison["plan_revision"],
            "protected_membership_digest": comparison["protected_membership_digest"],
            "operating_point_digest": digest(comparison.get("terms", {})),
            "metric": comparison.get("metric"),
        },
        "denominator": {
            "protected_rows": comparison["protected_rows_evaluated"],
            "excluded_counts": dict(sorted(comparison.get("exclusions", {}).items())),
        },
        "arms": arms,
        "paired": pairs,
        "decision": {
            "disposition": decision["disposition"],
            "winner": winner,
            "reason": decision["reason"],
            "fallback": decision["fallback"],
        },
        "immature_outcome_data": bool(sum(comparison.get("exclusions", {}).values())),
        "missing_comparable_units": _missing_units(arms),
        "audit": {
            "receipt_id": receipt.get("receipt_id") if receipt else None,
            "receipt_digest": receipt.get("receipt_digest") if receipt else None,
            "verdict": receipt.get("verdict") if receipt else None,
            "independent_verdict_digest": verdict.get("verdict_digest") if verdict else None,
            # No signature exists on either document yet (OPE-526/OPE-528 unlanded).
            "issuer_authenticated": False,
            "integrity": "digest_self_consistent_only",
        },
        "claim": {
            "evidence_tier": "generated" if origin == "generated" else "none",
            "scope": "implementation_only",
            "provider_or_customer_evidence": False,
            "accuracy_claim_eligible": False,
        },
        "evidence_pointers": [
            {"kind": "protected_comparison", "digest": comparison["evidence_revision"]},
            {"kind": "benchmark_audit_receipt", "digest": _pointer(receipt, "receipt_digest")},
            {"kind": "independent_verdict", "digest": _pointer(verdict, "verdict_digest")},
            {"kind": "paired_case_set", "digest": digest(comparison.get("paired_cases", []))},
        ],
        "authority": {"release": False, "delivery": False},
        "next_action": "separate_human_decision_required"
        if improvement
        else "no_release_authority_held",
    }
    return {**packet, "packet_digest": digest(packet)}, None


def _pointer(document: Json | None, field: str) -> str | None:
    return document.get(field) if document else None


def _improvement_checks(
    comparison: Json,
    receipt: Json | None,
    verdict: Json | None,
    ctx: PacketContext,
    winner: str,
) -> str | None:
    if receipt is None:
        return "audit_receipt_missing"
    if not _receipt_ok(receipt):
        return "audit_receipt_unverified"
    if receipt.get("superseded_by") is not None:
        return "audit_receipt_superseded"
    bindings = receipt["bindings"]
    if receipt["verdict"] == "stale" or bindings["frozen"] != bindings["current"]:
        return "audit_receipt_stale"
    if receipt["verdict"] == "claim_limited_to_named_population":
        return "audit_claim_limited_to_named_population"
    if receipt["verdict"] != _ALLOWED:
        return "audit_verdict_" + re.sub(r"\W", "_", receipt["verdict"])
    if verdict is None:
        return "independent_verdict_missing"
    if not _verdict_ok(verdict):
        return "independent_verdict_unverified"
    if verdict.get("verdict") != _REPRODUCED:
        return "independent_verdict_not_reproduced"
    if (
        verdict.get("verifier_id") in (None, ctx.worker_id)
        or verdict.get("producer_id") != ctx.worker_id
    ):
        return "independent_verdict_not_independent"
    arms = comparison["arms"]
    if not _denominator_ok(comparison) or verdict["denominator"] != {
        "protected_rows": comparison["protected_rows_evaluated"],
        "membership_digest": comparison["protected_membership_digest"],
    }:
        return "denominator_changed"
    pair = comparison["comparisons"].get("incumbent_vs_" + winner)
    coverage = arms[winner]["metrics"]["coverage"]
    floor = comparison.get("terms", {}).get("minimum_coverage_bps", 0)
    if (
        arms[winner]["hold_reason"]
        or not isinstance(pair, dict)
        or pair.get("observed_comparison") != "improved"
        or coverage["numerator"] * 10_000 < floor * coverage["denominator"]
    ):
        return "decision_inconsistent_with_evidence"
    bound = {
        "audit_receipt_digest": receipt["receipt_digest"],
        "comparison_evidence_revision": comparison["evidence_revision"],
        "candidate_digest": arms[winner]["revision"],
        "comparator_digest": arms["incumbent"]["revision"],
        "operating_point_digest": digest(comparison["terms"]),
        "benchmark_digest": ctx.benchmark_digest,
    }
    if any(verdict.get(k) != v for k, v in bound.items()):
        return "verdict_binding_mismatch"
    if (
        ctx.remedy_digest != arms[winner]["revision"]
        or ctx.fallback_digest != (arms["incumbent"]["revision"])
    ):
        return "remedy_binding_mismatch"
    return None


def assemble_packet(
    comparison: Any,
    receipt: Json | None,
    verdict: Json | None,
    ctx: PacketContext,
    *,
    allow_generated_fixture: bool = False,
) -> tuple[Json | None, str | None]:
    """Return ``(packet, None)`` or ``(None, reason)``. Never raises on a governance failure."""
    if allow_generated_fixture and not _fixture_context():
        return None, _FIXTURE_REFUSAL
    if not ctx.valid():
        return None, "invalid_packet_context"
    if not _comparison_ok(comparison):
        return None, "comparison_unverified"
    if comparison.get("organization") != ctx.tenant:
        return None, "comparison_scope_mismatch"
    return _assemble(
        comparison, receipt, verdict, ctx, allow_generated_fixture=allow_generated_fixture
    )


def verify_packet(
    packet: Json,
    comparison: Json,
    receipt: Json | None,
    verdict: Json | None,
    ctx: PacketContext,
    *,
    allow_generated_fixture: bool = False,
) -> bool:
    """Independent consumer replay: re-derive the packet from its sources and compare digests."""
    rebuilt, reason = assemble_packet(
        comparison, receipt, verdict, ctx, allow_generated_fixture=allow_generated_fixture
    )
    return reason is None and rebuilt == packet and _sealed(packet, "packet_digest")


def event_for(packet: Json) -> Json:
    ctx = packet["binding"]
    improvement = packet["kind"] == "measured_improvement"
    return {
        "schema_version": EVENT,
        "resource": "comparison/" + ctx["run_ref"],
        "event_id": "evt-" + packet["packet_digest"][:32],
        "event_revision": EVENT_REVISION,
        "tenant": ctx["tenant"],
        "recipient": ctx["recipient"],
        "run_ref": ctx["run_ref"],
        "proposal_version": ctx["proposal_version"],
        "expires_at": ctx["expires_at"],
        "disposition": packet["decision"]["disposition"],
        "staged_for_human_decision": improvement,
        "packet_ref": {
            "packet_id": "pkt-" + packet["packet_digest"][:32],
            "packet_digest": packet["packet_digest"],
        },
        "authority": {"release": False, "delivery": False},
    }


_SCHEMA = """
CREATE TABLE IF NOT EXISTS packets(
 packet_id TEXT PRIMARY KEY, run_ref TEXT NOT NULL, proposal_version TEXT NOT NULL,
 benchmark_digest TEXT NOT NULL, receipt_id TEXT, packet TEXT NOT NULL,
 state TEXT NOT NULL, state_reason TEXT, UNIQUE(run_ref, proposal_version));
CREATE TABLE IF NOT EXISTS outbox(
 resource TEXT NOT NULL, event_id TEXT NOT NULL, event_revision INTEGER NOT NULL,
 packet_id TEXT NOT NULL REFERENCES packets(packet_id), event TEXT NOT NULL,
 state TEXT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0, receipt_ref TEXT,
 PRIMARY KEY(resource, event_id, event_revision));
CREATE TABLE IF NOT EXISTS superseded_receipts(receipt_id TEXT PRIMARY KEY, successor TEXT);
CREATE TABLE IF NOT EXISTS reconciliations(
 packet_id TEXT PRIMARY KEY REFERENCES packets(packet_id), receipt_id TEXT NOT NULL,
 event_delivered INTEGER NOT NULL, state TEXT NOT NULL);
"""
_TABLES = ("packets", "outbox", "superseded_receipts", "reconciliations")


class PacketOutbox:
    """File-backed transactional store. Reopening the file is the restart path."""

    def __init__(self, path: Path | str, *, allow_generated_fixture: bool = False):
        if allow_generated_fixture and not _fixture_context():
            raise PacketStoreError(_FIXTURE_REFUSAL)
        self.path = Path(path)
        self.allow_generated_fixture = allow_generated_fixture
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, isolation_level=None, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK")
                raise
            db.execute("COMMIT")

    def snapshot(self) -> Json:
        """Every durable row, for proving that a repeat, late or duplicate call changed nothing."""
        with self._db() as db:
            return {
                t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY 1, 2")]
                for t in _TABLES
            }

    def _fence(self, db: sqlite3.Connection, packet_ids: list[str], reason: str) -> None:
        for pid in packet_ids:
            db.execute(
                "UPDATE packets SET state='superseded', state_reason=? WHERE packet_id=?",
                (reason, pid),
            )
            db.execute(
                "UPDATE outbox SET state='fenced' WHERE packet_id=? AND state IN "
                "('pending','sending')",
                (pid,),
            )

    def stage(
        self,
        comparison: Any,
        receipt: Json | None,
        verdict: Json | None,
        *,
        authority: AuthorityView,
        run_ref: str,
        now: int,
        fault: Callable[[str], None] | None = None,
    ) -> StageOutcome:
        ctx = authority.current(run_ref) if _ID.fullmatch(run_ref or "") else None
        if ctx is None or ctx.run_ref != run_ref:
            return _refuse("authority_unavailable")
        packet, reason = assemble_packet(
            comparison, receipt, verdict, ctx, allow_generated_fixture=self.allow_generated_fixture
        )
        if packet is None:
            return _refuse(reason or "comparison_unverified")
        if ctx.expires_at <= now:
            return _refuse("packet_expired")
        event = event_for(packet)
        pid = event["packet_ref"]["packet_id"]
        with self._db() as db:
            if (
                receipt
                and db.execute(
                    "SELECT 1 FROM superseded_receipts WHERE receipt_id=?",
                    (receipt.get("receipt_id"),),
                ).fetchone()
            ):
                return _refuse("audit_receipt_superseded")
            prior = db.execute(
                "SELECT packet_id, packet, state FROM packets WHERE run_ref=? AND "
                "proposal_version=?",
                (run_ref, ctx.proposal_version),
            ).fetchone()
            if prior is not None:
                if prior["packet_id"] == pid and prior["state"] == "staged":
                    return StageOutcome("already_staged", None, packet, event["event_id"])
                return _refuse("restage_conflict")
            older = db.execute(
                "SELECT packet_id FROM packets WHERE run_ref=? AND state='staged' AND "
                "benchmark_digest<>?",
                (run_ref, ctx.benchmark_digest),
            ).fetchall()
            self._fence(db, [r[0] for r in older], "benchmark_changed")
            db.execute(
                "INSERT INTO packets VALUES(?,?,?,?,?,?,'staged',NULL)",
                (
                    pid,
                    run_ref,
                    ctx.proposal_version,
                    ctx.benchmark_digest,
                    receipt.get("receipt_id") if receipt else None,
                    canonical(packet),
                ),
            )
            if fault:
                fault("before_outbox")
            db.execute(
                "INSERT INTO outbox VALUES(?,?,?,?,?,'pending',0,NULL)",
                (
                    event["resource"],
                    event["event_id"],
                    event["event_revision"],
                    pid,
                    canonical(event),
                ),
            )
            if fault:
                fault("before_commit")
        return StageOutcome("staged", None, packet, event["event_id"])

    def packet_status(self, packet_id: str) -> str:
        """What an approval consumer reads: only ``current`` may be acted on."""
        with self._db() as db:
            row = db.execute("SELECT state FROM packets WHERE packet_id=?", (packet_id,)).fetchone()
        if row is None:
            return "unknown"
        return "current" if row[0] == "staged" else str(row[0])

    def supersede_benchmark(self, run_ref: str, benchmark_digest: str) -> list[str]:
        """A changed benchmark supersedes every packet bound to a different one."""
        with self._db() as db:
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT packet_id FROM packets WHERE run_ref=? AND state='staged' AND "
                    "benchmark_digest<>?",
                    (run_ref, benchmark_digest),
                )
            ]
            self._fence(db, ids, "benchmark_changed")
        return ids

    def record_receipt_supersession(
        self, receipt_id: str, successor: str | None = None
    ) -> list[str]:
        """Block new packets on this receipt and reconcile every earlier disclosure once."""
        with self._db() as db:
            db.execute(
                "INSERT OR IGNORE INTO superseded_receipts VALUES(?,?)", (receipt_id, successor)
            )
            rows = db.execute(
                "SELECT packet_id FROM packets WHERE receipt_id=?", (receipt_id,)
            ).fetchall()
            for (pid,) in rows:
                # 'sending' is an uncertain delivery: it may have disclosed, so treat it as such.
                delivered = db.execute(
                    "SELECT 1 FROM outbox WHERE packet_id=? AND state IN ('delivered','sending')",
                    (pid,),
                ).fetchone()
                db.execute(
                    "INSERT OR IGNORE INTO reconciliations VALUES(?,?,?,'open')",
                    (pid, receipt_id, int(delivered is not None)),
                )
            self._fence(db, [r[0] for r in rows], "audit_receipt_superseded")
        return [r[0] for r in rows]

    def reconciliations(self) -> list[Json]:
        with self._db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM reconciliations ORDER BY 1")]

    def dispatch(
        self,
        sink: EventSink,
        authority: AuthorityView,
        *,
        now: int,
        fault: Callable[[str], None] | None = None,
    ) -> list[tuple[str, str]]:
        """Deliver each live outbox row with its stable key. An unknown result stays pending."""
        with self._db() as db:
            todo = db.execute(
                "SELECT o.resource, o.event_id, o.event_revision, o.event, o.packet_id, p.packet "
                "FROM outbox o JOIN packets p USING(packet_id) "
                "WHERE o.state IN ('pending','sending') ORDER BY o.resource, o.event_id"
            ).fetchall()
        results = []
        for row in todo:
            key = (row["resource"], row["event_id"], row["event_revision"])
            results.append(
                (row["event_id"], self._dispatch_one(row, key, sink, authority, now, fault))
            )
        return results

    def _dispatch_one(
        self,
        row: sqlite3.Row,
        key: tuple[str, str, int],
        sink: EventSink,
        authority: AuthorityView,
        now: int,
        fault: Callable[[str], None] | None,
    ) -> str:
        packet = json.loads(row["packet"])
        if packet["kind"] == "measured_improvement" and not (
            self.allow_generated_fixture and packet["claim"]["evidence_tier"] == "generated"
        ):
            with self._db() as db:
                self._fence(db, [row["packet_id"]], "audit_and_verdict_issuers_unverified")
            return "fenced"
        bound = packet["binding"]
        current = authority.current(bound["run_ref"])
        if current is None:
            return "authority_unavailable"
        if bound["expires_at"] <= now or current.as_json() != bound:
            reason = "packet_expired" if bound["expires_at"] <= now else "binding_changed"
            with self._db() as db:
                self._fence(db, [row["packet_id"]], reason)
            return "fenced"
        with self._db() as db:
            live = db.execute(
                "UPDATE outbox SET state='sending', attempts=attempts+1 WHERE resource=? AND "
                "event_id=? AND event_revision=? AND state IN ('pending','sending')",
                key,
            ).rowcount
        if not live:
            return "skipped"
        try:
            sink.deliver(json.loads(row["event"]))
            if fault:
                fault("after_deliver")
        except Exception:
            return "uncertain"  # never inferred as success; the next dispatch retries the key
        with self._db() as db:
            db.execute(
                "UPDATE outbox SET state='delivered' WHERE resource=? AND event_id=? AND "
                "event_revision=? AND state='sending'",
                key,
            )
        return "delivered"

    def acknowledge(
        self, resource: str, event_id: str, event_revision: int, receipt_ref: str
    ) -> str:
        """Consumer callback. Late, duplicate, fenced or unknown callbacks change nothing."""
        with self._db() as db:
            row = db.execute(
                "SELECT state FROM outbox WHERE resource=? AND event_id=? AND event_revision=?",
                (resource, event_id, event_revision),
            ).fetchone()
            if row is None:
                return "unknown_ignored"
            if row[0] == "delivered":
                return "duplicate_ignored"
            if row[0] == "fenced":
                return "fenced_ignored"
            if row[0] != "sending":
                return "not_dispatched_ignored"
            if not isinstance(receipt_ref, str) or not _ID.fullmatch(receipt_ref):
                return "invalid_receipt_ignored"
            db.execute(
                "UPDATE outbox SET state='delivered', receipt_ref=? WHERE resource=? AND "
                "event_id=? AND event_revision=?",
                (receipt_ref, resource, event_id, event_revision),
            )
        return "acknowledged"
