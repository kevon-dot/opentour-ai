"""Worker-side check of an OPE-526 audit receipt before any release use.

A release consumer passes a receipt *id* and the worker's configured store directory. The check
opens that store itself (``worker_store.read_worker_state``); the caller supplies no ledger, no
event list, no "current" bindings, and no verdict, flag or boolean about the receipt. In order:

1. **Store.** The store's identity (realpath, device and inode of its directory and SQLite file)
   matches the id pinned in it, and the event log's hash chain recomputes to the head the worker
   stored. A copied, restored or replaced store, or a dropped, reordered or edited event, is
   refused before any receipt is read.
2. **Receipt.** It was issued into this store, and the store id sealed into its issuance event is
   this store's identity now. Its ``receipt_digest`` and ``receipt_id`` recompute from the body.
3. **Status.** Replayed from the verified log: it is ``current``, has no recorded successor and
   does not name one itself.
4. **Verdict.** Exactly ``evaluation_allowed``; every other verdict is refused.
5. **Bindings.** Each of the six bindings is taken from the worker's benchmark state for the
   benchmark the receipt was issued against, with its basis reported per binding:
   split and label manifests are recomputed from the stored family records; the four opaque
   bindings are recomputed from stored material where the worker holds it, and otherwise are a
   stored, caller-asserted digest that is labelled as such (``audit_binding_caller_asserted``)
   and never reported as recomputed. Frozen, receipt-current and worker values must all agree.

What it does NOT establish. The event log and its head are unsigned (verifier OPE-566, keys
OPE-582), so receipt status rests on an unsigned, locally chained log and the receipt issuer is
not authenticated. A receipt that passes every check is therefore still *held*
(``audit_receipt_issuer_unauthenticated``, ``audit_ledger_unsigned``): ``release_permitted`` is
always ``False`` in this revision. Generated evidence is held too: the receipt's
``evidence_origin`` is caller-asserted at audit time, so a receipt is held as
``generated_fixture_evidence_only`` unless both it and the worker's benchmark record say
``observed``; a receipt that claims ``observed`` over a benchmark the worker recorded as generated
is additionally flagged ``evidence_origin_contradicts_worker_record``.

There is no release worker on ``main`` that consumes this check (see the OPE-526 decision
record). ``AuditLedger.promotion_allowed`` is not a release check: it admits claim-limited
receipts and verifies neither the digest nor the bindings.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentour_training.evaluation.benchmark_audit.ledger import AuditLedger
from opentour_training.evaluation.benchmark_audit.model import Verdict
from opentour_training.evaluation.benchmark_audit.worker_store import (
    BASIS_ABSENT,
    BASIS_ASSERTED,
    RECOMPUTED_BASES,
    STORE_FAILURES,
    StoreError,
    read_worker_state,
    receipt_body_digest,
    receipt_self_consistent,
)

__all__ = [
    "CHECK_SCHEMA",
    "HOLD_CODES",
    "REFUSAL_CODES",
    "ReleaseCheck",
    "check_receipt_for_release",
    "receipt_body_digest",
]

CHECK_SCHEMA = "opentour.benchmark-audit-release-check.v2"
_VERDICT_REFUSAL = {
    Verdict.STARTER_ONLY.value: "audit_verdict_starter_only",
    Verdict.CLAIM_LIMITED.value: "audit_claim_limited_to_named_population",
    Verdict.INSUFFICIENT_EVIDENCE.value: "audit_verdict_insufficient_evidence",
    Verdict.INVALID.value: "audit_verdict_invalid",
    Verdict.STALE.value: "audit_receipt_stale",
}
REFUSAL_CODES: frozenset[str] = frozenset(
    {
        *STORE_FAILURES,
        "audit_receipt_unknown_to_worker",
        "audit_receipt_integrity_failed",
        "audit_receipt_superseded",
        "audit_receipt_stale",
        "audit_benchmark_unknown_to_worker",
        "audit_binding_unavailable_to_worker",
        "audit_verdict_unrecognized",
        *_VERDICT_REFUSAL.values(),
    }
)
HOLD_CODES: frozenset[str] = frozenset(
    {
        "audit_receipt_issuer_unauthenticated",
        "audit_ledger_unsigned",
        "audit_binding_caller_asserted",
        "generated_fixture_evidence_only",
        "evidence_origin_contradicts_worker_record",
    }
)


@dataclass(frozen=True)
class ReleaseCheck:
    """Outcome of one worker check. ``refusals`` and ``holds`` use closed vocabularies."""

    receipt_id: str
    refusals: tuple[str, ...]
    holds: tuple[str, ...]
    verified_receipt_digest: str | None
    store_verified: bool = False
    binding_basis: tuple[tuple[str, str], ...] = ()

    @property
    def outcome(self) -> str:
        if self.refusals:
            return "refused"
        return "held" if self.holds else "admitted"

    @property
    def release_permitted(self) -> bool:
        return not self.refusals and not self.holds

    def as_json(self) -> dict[str, Any]:
        basis = dict(self.binding_basis)
        return {
            "schema_version": CHECK_SCHEMA,
            "receipt_id": self.receipt_id,
            "outcome": self.outcome,
            "release_permitted": self.release_permitted,
            "refusals": list(self.refusals),
            "holds": list(self.holds),
            "verified_receipt_digest": self.verified_receipt_digest,
            "integrity": "digest_self_consistent_only",
            "issuer_authenticated": False,
            "ledger_signed": False,
            "binding_basis": basis,
            "decision_basis": {
                "receipt_and_status": (
                    "read_by_check_from_worker_store_after_identity_chain_and_head_checks"
                    if self.store_verified
                    else "worker_store_not_verified_nothing_read"
                ),
                "event_log": "sha256_hash_chain_to_worker_stored_head_unsigned",
                "bindings_recomputed_by_worker": sorted(
                    k for k, v in basis.items() if v in RECOMPUTED_BASES
                ),
                "bindings_caller_asserted": sorted(
                    k for k, v in basis.items() if v == BASIS_ASSERTED
                ),
                "bindings_absent": sorted(k for k, v in basis.items() if v == BASIS_ABSENT),
            },
        }


def _superseded(ledger: AuditLedger, receipt_id: str, receipt: Mapping[str, Any]) -> bool:
    """Any one of: status other than current, a recorded successor, or a named successor."""
    return (
        ledger.status.get(receipt_id) != "current"
        or receipt_id in ledger.successor
        or receipt.get("superseded_by") is not None
    )


def _stale(receipt: Mapping[str, Any], worker: Mapping[str, str | None]) -> bool:
    bindings = receipt.get("bindings")
    if not isinstance(bindings, Mapping):
        return True
    return not (bindings.get("frozen") == bindings.get("current") == dict(worker))


def check_receipt_for_release(receipt_id: str, *, store_directory: Path | str) -> ReleaseCheck:
    """Decide from the worker store the check opens itself; see the module docstring."""
    try:
        state = read_worker_state(store_directory)
    except StoreError as exc:
        return ReleaseCheck(receipt_id, (exc.code,), (), None)
    stored = state.ledger.receipts.get(receipt_id)
    if stored is None:
        return ReleaseCheck(receipt_id, ("audit_receipt_unknown_to_worker",), (), None, True)
    # Work on a detached copy so nothing read later can change this decision.
    receipt: dict[str, Any] = json.loads(json.dumps(stored, sort_keys=True, allow_nan=False))
    if not receipt_self_consistent(receipt, receipt_id):
        return ReleaseCheck(receipt_id, ("audit_receipt_integrity_failed",), (), None, True)
    issuance = state.issuance.get(receipt_id) or {}
    if issuance.get("store_id") != state.store_id:
        return ReleaseCheck(receipt_id, ("audit_store_identity_mismatch",), (), None, True)
    refusals: set[str] = set()
    if _superseded(state.ledger, receipt_id, receipt):
        refusals.add("audit_receipt_superseded")
    benchmark = state.benchmarks.get(issuance.get("benchmark_id", ""))
    basis: dict[str, str] = {}
    if benchmark is None:
        refusals.add("audit_benchmark_unknown_to_worker")
    else:
        worker, basis = benchmark.bindings()
        if any(v is None for v in worker.values()):
            refusals.add("audit_binding_unavailable_to_worker")
        elif _stale(receipt, worker):
            refusals.add("audit_receipt_stale")
    verdict = receipt.get("verdict")
    if verdict not in {v.value for v in Verdict}:
        refusals.add("audit_verdict_unrecognized")
    elif verdict != Verdict.EVALUATION_ALLOWED.value:
        refusals.add(_VERDICT_REFUSAL[verdict])
    holds = {"audit_receipt_issuer_unauthenticated", "audit_ledger_unsigned"}
    if BASIS_ASSERTED in basis.values():
        holds.add("audit_binding_caller_asserted")
    worker_kind = benchmark.evidence_kind if benchmark is not None else None
    if receipt.get("evidence_origin") != "observed" or worker_kind != "observed":
        holds.add("generated_fixture_evidence_only")
    if receipt.get("evidence_origin") == "observed" and worker_kind != "observed":
        holds.add("evidence_origin_contradicts_worker_record")
    return ReleaseCheck(
        receipt_id,
        tuple(sorted(refusals)),
        tuple(sorted(holds)) if not refusals else (),
        receipt["receipt_digest"],
        True,
        tuple(sorted(basis.items())),
    )
