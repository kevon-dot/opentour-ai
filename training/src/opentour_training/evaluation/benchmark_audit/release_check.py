"""Worker-side check of an OPE-526 audit receipt before any release use.

A release consumer passes a receipt *id*. The receipt document is read from the worker's own
``AuditLedger``, never taken from the caller, and the caller supplies no verdict, flag or
boolean about it. The check then re-derives everything it relies on:

* the receipt was issued into this ledger and is still ``current`` there (not archived or
  superseded, and with no recorded successor);
* its ``receipt_digest`` and ``receipt_id`` recompute from the document body, so an edited
  verdict, binding or supersession field is detected;
* its verdict is exactly ``evaluation_allowed`` (claim-limited, starter-only, insufficient,
  invalid and stale are all refused);
* it is not stale: the frozen and current bindings inside the receipt agree, and both equal the
  bindings the worker recomputes now for the benchmark it is about to release against.

What it does NOT establish. No signing or key-custody contract exists yet (verifier OPE-566,
keys OPE-582), so the receipt issuer is not authenticated. A receipt that passes every check
above is therefore still *held*, with ``audit_receipt_issuer_unauthenticated``:
``release_permitted`` is always ``False`` in this revision. A generated-origin receipt is also
held, because local or simulated evidence cannot satisfy a real-population gate. The receipt's
``evidence_origin`` is itself caller-asserted at audit time, so ``observed`` is reported, not
trusted.

There is no release worker on ``main`` that consumes this check (see the OPE-526 decision
record). ``AuditLedger.promotion_allowed`` is not a release check: it admits claim-limited
receipts and verifies neither the digest nor the bindings.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from opentour_training.evaluation.benchmark_audit.ledger import AuditLedger
from opentour_training.evaluation.benchmark_audit.model import (
    RECEIPT_SCHEMA,
    BindingDigests,
    Verdict,
    canonical_digest,
)

CHECK_SCHEMA = "opentour.benchmark-audit-release-check.v1"
_SELF_FIELDS = ("receipt_digest", "receipt_id")
_VERDICT_REFUSAL = {
    Verdict.STARTER_ONLY.value: "audit_verdict_starter_only",
    Verdict.CLAIM_LIMITED.value: "audit_claim_limited_to_named_population",
    Verdict.INSUFFICIENT_EVIDENCE.value: "audit_verdict_insufficient_evidence",
    Verdict.INVALID.value: "audit_verdict_invalid",
    Verdict.STALE.value: "audit_receipt_stale",
}
REFUSAL_CODES: frozenset[str] = frozenset(
    {
        "audit_receipt_unknown_to_worker",
        "audit_receipt_integrity_failed",
        "audit_receipt_superseded",
        "audit_receipt_stale",
        "audit_verdict_unrecognized",
        *_VERDICT_REFUSAL.values(),
    }
)
HOLD_CODES: frozenset[str] = frozenset(
    {"audit_receipt_issuer_unauthenticated", "generated_fixture_evidence_only"}
)


@dataclass(frozen=True)
class ReleaseCheck:
    """Outcome of one worker check. ``refusals`` and ``holds`` use closed vocabularies."""

    receipt_id: str
    refusals: tuple[str, ...]
    holds: tuple[str, ...]
    verified_receipt_digest: str | None

    @property
    def outcome(self) -> str:
        if self.refusals:
            return "refused"
        return "held" if self.holds else "admitted"

    @property
    def release_permitted(self) -> bool:
        return not self.refusals and not self.holds

    def as_json(self) -> dict[str, Any]:
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
            "decision_basis": "worker_ledger_and_recomputed_bindings",
        }


def receipt_body_digest(receipt: Mapping[str, Any]) -> str:
    """Recompute the digest ``audit()`` seals: the document without its self-referencing ids."""
    return canonical_digest({k: v for k, v in receipt.items() if k not in _SELF_FIELDS})


def _integrity_ok(receipt: Mapping[str, Any], receipt_id: str) -> bool:
    try:
        recomputed = receipt_body_digest(receipt)
    except (TypeError, ValueError):
        return False
    supplied = receipt.get("receipt_digest")
    return (
        receipt.get("schema_version") == RECEIPT_SCHEMA
        and isinstance(supplied, str)
        and supplied == recomputed
        and receipt.get("receipt_id") == f"audit-{recomputed[:16]}" == receipt_id
    )


def _stale(receipt: Mapping[str, Any], current: BindingDigests) -> bool:
    bindings = receipt.get("bindings")
    if not isinstance(bindings, Mapping):
        return True
    worker = asdict(current)
    return not (bindings.get("frozen") == bindings.get("current") == worker)


def check_receipt_for_release(
    receipt_id: str, *, ledger: AuditLedger, current: BindingDigests
) -> ReleaseCheck:
    """Decide from worker-held state only.

    ``current`` must be the bindings the worker recomputes now for the benchmark it will release
    against (for example with ``audit.binding_digests`` over the current records). It is a
    worker computation, not a statement about the receipt.
    """
    stored = ledger.receipts.get(receipt_id)
    if stored is None:
        return ReleaseCheck(receipt_id, ("audit_receipt_unknown_to_worker",), (), None)
    # Work on a detached copy so later mutation of ledger state cannot change this decision.
    receipt: dict[str, Any] = json.loads(json.dumps(stored, sort_keys=True, allow_nan=False))
    if not _integrity_ok(receipt, receipt_id):
        return ReleaseCheck(receipt_id, ("audit_receipt_integrity_failed",), (), None)
    refusals: set[str] = set()
    if (
        ledger.status.get(receipt_id) != "current"
        or receipt_id in ledger.successor
        or receipt.get("superseded_by") is not None
    ):
        refusals.add("audit_receipt_superseded")
    if _stale(receipt, current):
        refusals.add("audit_receipt_stale")
    verdict = receipt.get("verdict")
    if verdict not in {v.value for v in Verdict}:
        refusals.add("audit_verdict_unrecognized")
    elif verdict != Verdict.EVALUATION_ALLOWED.value:
        refusals.add(_VERDICT_REFUSAL[verdict])
    holds = {"audit_receipt_issuer_unauthenticated"}
    if receipt.get("evidence_origin") != "observed":
        holds.add("generated_fixture_evidence_only")
    return ReleaseCheck(
        receipt_id,
        tuple(sorted(refusals)),
        tuple(sorted(holds)) if not refusals else (),
        receipt["receipt_digest"],
    )
