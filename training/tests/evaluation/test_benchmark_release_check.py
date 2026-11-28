"""OPE-526 release consumer: the worker refuses a receipt that is not allowed, stale or superseded.

Fixture kind: generated. The decision rests on the worker's ledger and recomputed bindings only;
no caller verdict or boolean is read. Receipts are unsigned (OPE-566/OPE-582), so even a receipt
that passes every check is held and release is never permitted in this revision.
"""

from __future__ import annotations

import inspect
from dataclasses import fields, replace
from typing import Any

import pytest
from audit_fixtures import (
    CONSUMED,
    baseline_records,
    commit,
    current_for,
    inputs_for,
)
from opentour_training.evaluation.benchmark_audit.audit import audit
from opentour_training.evaluation.benchmark_audit.ledger import AuditLedger
from opentour_training.evaluation.benchmark_audit.model import (
    BindingDigests,
    SamplingDesign,
    Verdict,
)
from opentour_training.evaluation.benchmark_audit.release_check import (
    HOLD_CODES,
    REFUSAL_CODES,
    ReleaseCheck,
    check_receipt_for_release,
    receipt_body_digest,
)

V = Verdict


def issued(receipt: dict[str, Any]) -> AuditLedger:
    ledger = AuditLedger()
    ledger.issue(f"issue:{receipt['receipt_id']}", receipt)
    return ledger


def check(receipt: dict[str, Any], ledger: AuditLedger | None = None, current: Any = None):
    return check_receipt_for_release(
        receipt["receipt_id"],
        ledger=ledger if ledger is not None else issued(receipt),
        current=current if current is not None else current_for(baseline_records()),
    )


def reseal(receipt: dict[str, Any]) -> dict[str, Any]:
    """Recompute a self-consistent digest, as a forger without a key could."""
    body = {k: v for k, v in receipt.items() if k not in {"receipt_digest", "receipt_id"}}
    digest = receipt_body_digest(body)
    return {**body, "receipt_digest": digest, "receipt_id": f"audit-{digest[:16]}"}


def test_allowed_current_receipt_passes_every_worker_check_but_is_held_unsigned() -> None:
    receipt = audit(inputs_for(baseline_records()))
    assert receipt["verdict"] == V.EVALUATION_ALLOWED.value
    result = check(receipt)
    assert result.refusals == ()
    assert result.outcome == "held"
    assert result.holds == (
        "audit_receipt_issuer_unauthenticated",
        "generated_fixture_evidence_only",
    )
    assert result.release_permitted is False
    assert result.verified_receipt_digest == receipt["receipt_digest"]
    as_json = result.as_json()
    assert as_json["issuer_authenticated"] is False
    assert as_json["integrity"] == "digest_self_consistent_only"


NOT_ALLOWED = {
    "claim_limited": (
        lambda: audit(
            inputs_for(
                tuple(
                    r
                    for r in baseline_records()
                    if r.split == "development" or r.outcome_kind == "correction"
                ),
                prereg_overrides={"sampling_design": SamplingDesign.CORRECTION_ONLY},
            )
        ),
        "audit_claim_limited_to_named_population",
    ),
    "starter_only": (
        lambda: audit(
            inputs_for((), prereg_overrides={"sampling_design": SamplingDesign.POLICY_ONLY})
        ),
        "audit_verdict_starter_only",
    ),
    "insufficient": (
        lambda: audit(inputs_for(baseline_records(), preprocessing=None)),
        "audit_verdict_insufficient_evidence",
    ),
    "invalid": (
        lambda: audit(
            inputs_for(baseline_records(), consumed_features=(*CONSUMED, "refund_settled_at"))
        ),
        "audit_verdict_invalid",
    ),
}


@pytest.mark.parametrize("name", sorted(NOT_ALLOWED))
def test_every_verdict_other_than_evaluation_allowed_is_refused(name: str) -> None:
    build, code = NOT_ALLOWED[name]
    receipt = build()
    assert receipt["verdict"] != V.EVALUATION_ALLOWED.value
    current = receipt["bindings"]["current"]
    result = check(receipt, current=BindingDigests(**current))
    assert code in result.refusals
    assert result.outcome == "refused" and result.release_permitted is False
    assert result.holds == ()


def test_post_freeze_relabel_stale_receipt_is_refused() -> None:
    frozen = baseline_records()
    relabeled = tuple(
        replace(r, final_label_digest=commit(r.family_id, "generated-changed-action"))
        if r.family_id == "generated-r03"
        else r
        for r in frozen
    )
    receipt = audit(inputs_for(relabeled, frozen_records=frozen))
    assert receipt["verdict"] == V.STALE.value
    result = check(receipt, current=current_for(relabeled))
    assert result.refusals == ("audit_receipt_stale",)


@pytest.mark.parametrize("field", [f.name for f in fields(BindingDigests)])
def test_allowed_receipt_is_stale_when_worker_recomputes_a_different_binding(field: str) -> None:
    receipt = audit(inputs_for(baseline_records()))
    moved = replace(current_for(baseline_records()), **{field: "f" * 64})
    result = check(receipt, current=moved)
    assert result.refusals == ("audit_receipt_stale",)
    assert result.release_permitted is False


def test_receipt_superseded_in_the_worker_ledger_is_refused_and_successor_is_checked() -> None:
    old = audit(inputs_for(baseline_records()))
    ledger = issued(old)
    fresh = tuple(replace(r, fresh_confirmation=True) for r in baseline_records())
    successor = audit(inputs_for(fresh, successor_of=old["receipt_id"]))
    assert successor["verdict"] == V.EVALUATION_ALLOWED.value
    ledger.supersede("supersede:1", old["receipt_id"], successor)
    current = current_for(baseline_records())
    stale_old = check_receipt_for_release(old["receipt_id"], ledger=ledger, current=current)
    assert stale_old.refusals == ("audit_receipt_superseded",)
    assert ledger.promotion_allowed(old["receipt_id"]) is False
    new = check_receipt_for_release(successor["receipt_id"], ledger=ledger, current=current)
    assert new.refusals == () and new.release_permitted is False


def test_receipt_that_names_its_own_successor_is_refused() -> None:
    receipt = audit(inputs_for(baseline_records(), superseded_by="generated-successor"))
    result = check(receipt, current=current_for(baseline_records()))
    assert "audit_receipt_superseded" in result.refusals
    assert "audit_receipt_stale" in result.refusals


def test_successor_link_alone_refuses_even_if_status_reads_current() -> None:
    receipt = audit(inputs_for(baseline_records()))
    ledger = issued(receipt)
    ledger.successor[receipt["receipt_id"]] = "audit-generated-next"
    assert check(receipt, ledger).refusals == ("audit_receipt_superseded",)


def test_receipt_unknown_to_the_worker_ledger_is_refused() -> None:
    receipt = audit(inputs_for(baseline_records()))
    result = check(receipt, AuditLedger())
    assert result.refusals == ("audit_receipt_unknown_to_worker",)
    assert result.verified_receipt_digest is None


@pytest.mark.parametrize(
    "edit",
    [
        {"verdict": V.EVALUATION_ALLOWED.value},
        {"reasons": []},
        {"schema_version": "opentour.benchmark-audit-receipt.v0"},
    ],
)
def test_edited_receipt_fails_integrity_before_any_field_is_trusted(edit: dict[str, Any]) -> None:
    receipt = audit(inputs_for(baseline_records(), unlogged_disclosures=1))
    ledger = issued(receipt)
    ledger.receipts[receipt["receipt_id"]] = {**receipt, **edit}
    result = check(receipt, ledger)
    assert result.refusals == ("audit_receipt_integrity_failed",)
    assert result.release_permitted is False


def test_clearing_supersession_from_a_superseded_receipt_fails_integrity() -> None:
    receipt = audit(inputs_for(baseline_records(), superseded_by="generated-successor"))
    ledger = issued(receipt)
    ledger.receipts[receipt["receipt_id"]] = {
        **receipt,
        "superseded_by": None,
        "verdict": V.EVALUATION_ALLOWED.value,
        "reasons": [],
    }
    assert check(receipt, ledger).refusals == ("audit_receipt_integrity_failed",)


def test_edited_bindings_fail_integrity() -> None:
    receipt = audit(inputs_for(baseline_records()))
    ledger = issued(receipt)
    frozen = {**receipt["bindings"]["frozen"], "answer_key": "0"}
    bindings = {**receipt["bindings"], "frozen": frozen}
    ledger.receipts[receipt["receipt_id"]] = {**receipt, "bindings": bindings}
    assert check(receipt, ledger).refusals == ("audit_receipt_integrity_failed",)


def test_receipt_stored_under_another_id_fails_integrity() -> None:
    receipt = audit(inputs_for(baseline_records()))
    ledger = AuditLedger()
    ledger.receipts["audit-0000000000000000"] = receipt
    ledger.status["audit-0000000000000000"] = "current"
    result = check_receipt_for_release(
        "audit-0000000000000000", ledger=ledger, current=current_for(baseline_records())
    )
    assert result.refusals == ("audit_receipt_integrity_failed",)


def test_resealed_forgery_is_still_held_because_the_issuer_is_unauthenticated() -> None:
    invalid = audit(inputs_for(baseline_records(), unlogged_disclosures=1))
    forged = reseal({**invalid, "verdict": V.EVALUATION_ALLOWED.value, "reasons": []})
    result = check(forged)
    assert result.refusals == ()
    assert "audit_receipt_issuer_unauthenticated" in result.holds
    assert result.release_permitted is False


def test_unrecognized_verdict_with_consistent_digest_is_refused() -> None:
    forged = reseal({**audit(inputs_for(baseline_records())), "verdict": "accepted"})
    assert check(forged).refusals == ("audit_verdict_unrecognized",)


def test_missing_bindings_are_stale() -> None:
    receipt = audit(inputs_for(baseline_records()))
    forged = reseal({k: v for k, v in receipt.items() if k != "bindings"})
    assert check(forged).refusals == ("audit_receipt_stale",)


def test_observed_origin_is_reported_not_trusted_and_still_held() -> None:
    forged = reseal({**audit(inputs_for(baseline_records())), "evidence_origin": "observed"})
    result = check(forged)
    assert result.holds == ("audit_receipt_issuer_unauthenticated",)
    assert result.release_permitted is False


def test_decision_takes_no_caller_verdict_or_boolean() -> None:
    params = inspect.signature(check_receipt_for_release).parameters
    assert list(params) == ["receipt_id", "ledger", "current"]
    # The ledger's promotion helper admits a claim-limited receipt; the release check does not.
    build, _ = NOT_ALLOWED["claim_limited"]
    receipt = build()
    ledger = issued(receipt)
    assert ledger.promotion_allowed(receipt["receipt_id"]) is True
    current = BindingDigests(**receipt["bindings"]["current"])
    result = check_receipt_for_release(receipt["receipt_id"], ledger=ledger, current=current)
    assert result.refusals == ("audit_claim_limited_to_named_population",)


def test_a_later_ledger_edit_cannot_change_a_returned_decision() -> None:
    receipt = audit(inputs_for(baseline_records()))
    ledger = issued(receipt)
    result = check(receipt, ledger)
    before = result.as_json()
    ledger.receipts[receipt["receipt_id"]]["verdict"] = V.INVALID.value
    assert result.as_json() == before
    assert check(receipt, ledger).refusals == ("audit_receipt_integrity_failed",)


def test_outcome_codes_are_closed_vocabularies() -> None:
    assert ReleaseCheck("r", ("audit_verdict_invalid",), (), None).outcome == "refused"
    assert set(REFUSAL_CODES).isdisjoint(HOLD_CODES)
    for build, code in NOT_ALLOWED.values():
        del build
        assert code in REFUSAL_CODES
