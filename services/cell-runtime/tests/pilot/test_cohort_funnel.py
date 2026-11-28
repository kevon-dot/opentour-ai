"""OPE-455 private cohort funnel: positive and adverse cases on a real generated register.

Fixture kind: generated sentinels only (simulated). The register is built through the signed
OPE-451 qualification and OPE-452 lease entry points. These tests prove software behavior. They
are not customer, provider or installed evidence, and customer acceptance stays 0/10.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from cohort_funnel_fixture import (
    Clock,
    acquire,
    close,
    cost,
    ope522_readout,
    org,
    private_register,
    qualify,
    readiness_receipt,
    receipt,
)
from opentour_cell.pilot import cohort_funnel as funnel
from opentour_cell.pilot.cohort_funnel import FunnelError, build_report, verify_report, weekly_view
from opentour_cell.pilot.operating_point import digest

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[4]
GUIDE = ROOT / funnel.OPERATOR_GUIDE
TUNING_SOURCE = ROOT / "training/src/opentour_training/evaluation/tuning_benefit.py"
A, B, C, D, E = (org(number) for number in range(1, 6))
OUTSIDER = org(9)


@pytest.fixture
def register(tmp_path: Path) -> Path:
    return private_register(tmp_path)


@pytest.fixture
def clock() -> Clock:
    return Clock()


def build(register: Path, receipts: list[Json], clock: Clock, **options: Any) -> Json:
    options.setdefault("setup_kind", "generated")
    return build_report(register=register, receipts=receipts, as_of=clock.as_of, **options)


def counts(report: Json) -> dict[str, int]:
    return {name: block["count"] for name, block in report["columns"].items()}


def current(report: Json) -> dict[str, list[str]]:
    return {name: state["organizations"] for name, state in report["current"].items()}


def codes(report: Json) -> list[tuple[str, str | None]]:
    return [(item["code"], item["receipt_ref"]) for item in report["mismatches"]]


def cohort(register: Path, clock: Clock) -> list[Json]:
    """Five generated organizations across the funnel plus one receipt for an outsider."""
    charters = {name: qualify(register, name) for name in (A, B, C, D, E)}
    lease_a = acquire(register, A, charters[A])
    assert lease_a["disposition"] == "acquired"
    assert close(register, A, lease_a)["disposition"] == "closed"
    assert acquire(register, B, charters[B], generation=1)["disposition"] == "acquired"
    assert acquire(register, C, charters[C], generation=2)["disposition"] == "queued"
    qualify(register, D, intent="lost", source_revision=2, event_id="generated-lost")
    t = clock.at
    return [
        receipt("ready_to_result", A, "a-ready", at=t(3600), customer_wait_seconds=1800),
        receipt("starter_readout", A, "a-starter", at=t(5400), compute_seconds=120),
        receipt(
            "real_case_evaluation",
            A,
            "a-real-crm-1",
            at=t(9000),
            source="generated-crm-record-1",
            fit_count=0,
            cost=cost("12.50"),
        ),
        receipt(
            "real_case_evaluation",
            A,
            "a-real-crm-2",
            at=t(9100),
            source="generated-crm-record-2",
            fit_count=0,
            cost=cost("7.50"),
        ),
        receipt("owner_decision", A, "a-decision", at=t(10_000)),
        receipt("delivery", A, "a-delivery", at=t(11_000)),
        receipt("payment", A, "a-renewal", at=t(12_000)),
        receipt("ready_to_result", B, "b-ready", at=t(3600)),
        receipt(
            "real_case_evaluation",
            B,
            "b-real",
            at=t(7200),
            comparison="incumbent_vs_repair",
            fit_count=1,
            limit_exceeded=True,
        ),
        receipt(
            "owner_decision",
            B,
            "b-stop",
            at=t(8000),
            outcome="stop",
            stop_reason_ref="generated-stop-reason",
        ),
        receipt("fee_waiver", C, "c-waiver", at=t(2000)),
        receipt(
            "starter_readout", E, "e-starter", at=t(2000), readout_revision_ref="generated-e-1"
        ),
        receipt(
            "owner_decision",
            E,
            "e-stop",
            at=t(3000),
            readout_revision_ref="generated-e-1",
            outcome="stop",
            stop_reason_ref="generated-starter-stop",
        ),
        receipt("payment", OUTSIDER, "outsider-payment", at=t(4000)),
    ]


def test_empty_cohort_displays_zero_of_ten_and_keeps_every_unknown_unknown(
    register: Path, clock: Clock
) -> None:
    register.touch(mode=0o600)
    report = build(register, [], clock)
    assert report["headline"]["display"] == "0/10"
    assert report["headline"]["accepted"] == report["headline"]["paid_recurring"] == 0
    assert set(counts(report).values()) == {0}
    assert all(not orgs for orgs in current(report).values())
    assert report["costs"]["observed"] == {
        "accepted_organizations": 0,
        "organizations_with_known_cost": 0,
        "unknown_cost_organizations": 0,
        "state": "unavailable",
        "reason": "no_accepted_organizations",
    }
    assert report["timings"]["onboarding_wait"]["median_seconds"] is None
    assert report["runs"]["zero_fit"] == {"evaluations": 0, "known": 0, "unknown": 0, "zero_fit": 0}
    assert report["outcome_rollup"]["disclosure"] == "withheld"
    assert verify_report(report) == []
    view = weekly_view(report)
    assert view["headline"]["display"] == "0/10"
    assert set(view["columns"].values()) == {0}

    permitted = build(register, [], clock, outcome_disclosure_permitted=True)
    outcome = permitted["outcome_rollup"]
    assert outcome["readiness"] == {
        "state": "unavailable",
        "reason": "ope522_readiness_receipt_absent",
        "gate": "OPE-522",
    }
    assert outcome["tuning_benefit"]["claim"] == "Augmentation benefit unproven"
    assert outcome["tuning_benefit"]["reason"] == "ope517_result_absent"
    assert outcome["label_efficiency"]["claim"] == "Label savings not measured"
    assert permitted["columns"] == report["columns"]


def test_missing_or_shared_register_is_unavailable_never_an_empty_cohort(
    tmp_path: Path, clock: Clock
) -> None:
    directory = tmp_path / "private-cohort"
    directory.mkdir(mode=0o700)
    with pytest.raises(FunnelError) as missing:
        build(directory / "absent.sqlite3", [], clock)
    assert missing.value.code == "register_unavailable"
    shared = tmp_path / "shared"
    shared.mkdir(mode=0o755)
    (shared / "register.sqlite3").touch(mode=0o600)
    with pytest.raises(FunnelError) as unreadable:
        build(shared / "register.sqlite3", [], clock)
    assert unreadable.value.code == "register_unreadable"
    corrupt = directory / "corrupt.sqlite3"
    corrupt.write_bytes(b"not a database, generated")
    corrupt.chmod(0o600)
    with pytest.raises(FunnelError) as damaged:
        build(corrupt, [], clock)
    assert damaged.value.code == "register_unreadable"


def test_cohort_rollup_counts_each_organization_once_per_column(
    register: Path, clock: Clock
) -> None:
    report = build(register, cohort(register, clock), clock)
    assert counts(report) == {
        "qualified": 5,
        "enrolled": 2,
        "ready_to_result": 2,
        "starter_readout": 2,
        "real_case_evaluation": 2,
        "exact_fit": 0,
        "owner_decision": 3,
        "accepted": 2,
        "delivered": 1,
        "paid_recurring": 1,
        "fee_waiver_proposed": 1,
        "fee_waiver_agreed": 0,
    }
    assert current(report) == {
        "queued": [C],
        "queue_stale": [],
        "active": [B],
        "active_requires_reconciliation": [],
        "closed": [A],
        "withdrawn": [],
        "lost": [D],
        "deferred": [],
    }
    headline = report["headline"]
    assert (headline["display"], headline["active"], headline["paid_recurring"]) == ("2/10", 1, 1)
    assert headline["accepted_by_evidence_tier"]["locally_keyed"] == 2
    assert headline["externally_claimable_accepted"] == 0
    assert report["columns"]["accepted"]["organizations"] == [A, B]
    # Two CRM records for one organization: two evidence references, one organization.
    assert report["columns"]["real_case_evaluation"]["evidence"] == [
        "receipt:a-real-crm-1@1",
        "receipt:a-real-crm-2@1",
        "receipt:b-real@1",
    ]
    assert report["duplicate_source_records"] == {"organizations": [A], "count": 1}
    assert report["accepted_by_outcome"] == {
        "continue": 1,
        "revise": 0,
        "gather_evidence": 0,
        "stop": 1,
    }
    assert report["qualifying_stops"]["organizations"] == [B]
    assert report["non_qualifying_stops"]["organizations"] == [E]
    assert report["owner_decision_latest_outcome"]["stop"] == 2
    assert report["drop_off"]["counts"] == {
        "qualification_lost": 1,
        "engagement_withdrawn": 0,
        "queue_stale": 0,
        "owner_stop": 2,
        "owner_deferred": 0,
    }
    assert report["drop_off"]["stop_reason_refs"] == {
        B: "generated-stop-reason",
        E: "generated-starter-stop",
    }
    assert codes(report) == [("receipt_not_admitted", "outsider-payment@1")]
    assert report["mismatches"][0]["owner_role"] == "intake_owner"
    assert report["mismatches"][0]["operator_link"].endswith("#mismatch-codes")
    assert {
        item["organization_ref"]: item["next_stage"] for item in report["next_missing_stage"]
    } == {
        A: None,
        B: None,
        C: "ready_to_result",
        D: None,
        E: None,
    }
    assert report["runs"] == {
        "runs": 5,
        "limit_exceeded": {"known": 1, "unknown": 4, "exceeded": 1},
        "zero_fit": {"evaluations": 3, "known": 3, "unknown": 0, "zero_fit": 2},
    }
    assert report["costs"]["observed"]["reason"] == "no_complete_cost"
    assert report["setup_kind"] == "generated" and report["customer_credit"] is False
    assert report["claim_scope"] == "implementation_only"
    assert report["integrity"] == "digest_self_consistent_only"
    assert report["issuer_authenticated"] is False
    assert verify_report(report) == []


def test_paid_is_never_inferred_from_approval_decision_delivery_fit_or_waiver(
    register: Path, clock: Clock
) -> None:
    qualify(register, A)
    t = clock.at
    approvals = [
        receipt("real_case_evaluation", A, "real", at=t(100)),
        receipt("owner_decision", A, "approve", at=t(200), outcome="continue"),
        receipt("delivery", A, "handoff", at=t(300)),
        receipt("exact_fit", A, "fit", at=t(150)),
        receipt("fee_waiver", A, "waiver", at=t(250), status="agreed"),
    ]
    report = build(register, approvals, clock)
    assert counts(report)["accepted"] == 1
    assert counts(report)["fee_waiver_agreed"] == 1
    assert counts(report)["paid_recurring"] == report["headline"]["paid_recurring"] == 0
    paid = receipt("payment", A, "renewal", at=t(400))
    not_recurring = {**paid, "receipt_id": "one-off"}
    not_recurring["facts"] = {**paid["facts"], "recurring": False}
    no_evidence = {**paid, "receipt_id": "promise"}
    no_evidence["facts"] = {**paid["facts"], "payment_evidence_ref": None}
    amount_only = {**paid, "receipt_id": "amount"}
    amount_only["facts"] = {**paid["facts"], "amount": "100.00"}
    held = build(register, [*approvals, not_recurring, no_evidence, amount_only], clock)
    assert counts(held)["paid_recurring"] == 0
    assert [(item["code"], item["receipt_ref"]) for item in held["quarantined"]] == [
        ("receipt_value_invalid", "one-off@1"),
        ("receipt_value_invalid", "promise@1"),
        ("receipt_fields_invalid", "amount@1"),
    ]
    renewed = build(register, [*approvals, paid], clock)
    assert counts(renewed)["paid_recurring"] == 1
    assert counts(renewed)["accepted"] == counts(report)["accepted"]
    never = next(c for c in funnel.FIELD_MAP["columns"] if c["column"] == "paid_recurring")
    assert {"approval", "owner_decision", "delivery", "fee_waiver", "crm_stage"} <= set(
        never["never_from"]
    )


def test_unscored_starter_earns_its_readout_only_and_a_later_real_comparison_adds_one(
    register: Path, clock: Clock
) -> None:
    qualify(register, A)
    t = clock.at
    starter = [
        receipt("starter_readout", A, "starter", at=t(100), readout_revision_ref="starter-1"),
        receipt(
            "owner_decision",
            A,
            "starter-decision",
            at=t(200),
            readout_revision_ref="starter-1",
        ),
    ]
    report = build(register, starter, clock)
    assert counts(report) == {
        **dict.fromkeys(funnel.COLUMNS, 0),
        "qualified": 1,
        "starter_readout": 1,
        "owner_decision": 1,
    }
    assert report["headline"]["display"] == "0/10"
    real = [
        receipt("real_case_evaluation", A, "rule-only", at=t(300), comparison="rule_only"),
        receipt("real_case_evaluation", A, "rule-only-repeat", at=t(310)),
        receipt("owner_decision", A, "real-decision", at=t(400)),
    ]
    later = build(register, [*starter, *real], clock)
    assert counts(later)["real_case_evaluation"] == 1
    assert counts(later)["accepted"] == 1
    assert counts(later)["exact_fit"] == 0
    assert counts(later)["starter_readout"] == 1


def test_dispute_reversal_and_replacement_reconcile_only_their_own_columns(
    register: Path, clock: Clock
) -> None:
    qualify(register, A)
    qualify(register, B)
    t = clock.at
    witness = receipt("real_case_evaluation", A, "witness", at=t(100))
    decision = receipt("owner_decision", A, "decision", at=t(200))
    payment = receipt("payment", A, "payment", at=t(300))
    base = [witness, decision, payment]

    def correction(target: str, kind: str, revision: int = 1, at: int = 400) -> Json:
        return receipt(
            "correction",
            A,
            f"correct-{target}-{kind}-{revision}",
            at=t(at),
            target_receipt_id=target,
            target_revision=revision,
            kind=kind,
        )

    replacement = receipt("real_case_evaluation", A, "witness", at=t(100), revision=2)
    b_accepted = [
        receipt("real_case_evaluation", B, "b-witness", at=t(100)),
        receipt("owner_decision", B, "b-decision", at=t(200)),
    ]
    sequence = [
        base,
        [*base, decision],
        [*base, decision, correction("witness", "disputed")],
        [*base, decision, correction("witness", "disputed"), replacement],
        [*base, decision, correction("witness", "disputed"), replacement, *b_accepted],
    ]
    reports = [build(register, receipts, clock) for receipts in sequence]
    assert [counts(report)["accepted"] for report in reports] == [1, 1, 0, 1, 2]
    assert reports[1]["inputs"]["duplicate_deliveries"] == 1
    disputed = reports[2]
    assert counts(disputed)["real_case_evaluation"] == 0
    assert counts(disputed)["owner_decision"] == 1
    assert counts(disputed)["paid_recurring"] == 1
    assert disputed["corrections"] == [
        {"receipt_ref": "witness@1", "column": "real_case_evaluation", "kind": "disputed"}
    ]
    assert counts(reports[3])["real_case_evaluation"] == 1

    reversed_payment = build(register, [*base, correction("payment", "reversed")], clock)
    assert counts(reversed_payment)["paid_recurring"] == 0
    assert counts(reversed_payment)["accepted"] == 1
    disputed_decision = build(register, [*base, correction("decision", "disputed")], clock)
    assert counts(disputed_decision)["owner_decision"] == 0
    assert counts(disputed_decision)["accepted"] == 0
    assert counts(disputed_decision)["real_case_evaluation"] == 1
    history = build(register, [*base, replacement, correction("witness", "disputed")], clock)
    assert counts(history)["accepted"] == 1
    assert history["corrections"] == []


def test_a_decision_made_before_the_witnessed_readout_is_not_an_acceptance(
    register: Path, clock: Clock
) -> None:
    qualify(register, A)
    t = clock.at
    early = [
        receipt("owner_decision", A, "early-decision", at=t(200)),
        receipt("real_case_evaluation", A, "late-witness", at=t(300)),
    ]
    report = build(register, early, clock)
    assert counts(report)["owner_decision"] == 1
    assert counts(report)["real_case_evaluation"] == 1
    assert counts(report)["accepted"] == 0
    assert codes(report) == [("decision_precedes_readout", "early-decision@1")]
    after = build(register, [*early, receipt("owner_decision", A, "decision", at=t(400))], clock)
    assert counts(after)["accepted"] == 1
    assert codes(after) == []


def test_conflicting_changed_or_gapped_revisions_are_held_not_counted(
    register: Path, clock: Clock
) -> None:
    qualify(register, A)
    qualify(register, B)
    t = clock.at
    first = receipt("delivery", A, "conflict", at=t(100))
    rival = {**first, "evidence_digest": digest("generated-rival")}
    moved = receipt("delivery", B, "moved", at=t(100), revision=2)
    gap = receipt("exact_fit", A, "gap", at=t(100), revision=3)
    report = build(
        register,
        [
            first,
            rival,
            receipt("delivery", A, "moved", at=t(90)),
            moved,
            receipt("exact_fit", A, "gap", at=t(90)),
            gap,
            receipt("payment", A, "orphan", at=t(100), revision=2),
        ],
        clock,
    )
    assert counts(report)["delivered"] == 0
    assert counts(report)["exact_fit"] == 1
    assert counts(report)["paid_recurring"] == 0
    assert codes(report) == [
        ("receipt_identity_changed", "moved"),
        ("receipt_predecessor_missing", "gap@3"),
        ("receipt_predecessor_missing", "orphan@2"),
        ("receipt_revision_conflict", "conflict@1"),
    ]


def test_receipts_for_unadmitted_organizations_or_engagements_count_zero(
    register: Path, clock: Clock
) -> None:
    qualify(register, A)
    t = clock.at
    report = build(
        register,
        [
            receipt("real_case_evaluation", OUTSIDER, "outsider", at=t(100)),
            receipt("real_case_evaluation", A, "wrong-epoch", at=t(100), engagement=2),
            receipt("payment", A, "admitted", at=t(100)),
        ],
        clock,
    )
    assert counts(report)["real_case_evaluation"] == 0
    assert counts(report)["paid_recurring"] == 1
    assert codes(report) == [
        ("receipt_not_admitted", "wrong-epoch@1"),
        ("receipt_not_admitted", "outsider@1"),
    ]
    assert {item["organization_ref"] for item in report["mismatches"]} == {A, OUTSIDER}


def test_queue_lease_rename_and_reentry_keep_one_organization(register: Path, clock: Clock) -> None:
    charter_a = qualify(register, A)
    charter_b = qualify(register, B)
    lease_a = acquire(register, A, charter_a)
    assert lease_a["disposition"] == "acquired"
    assert acquire(register, B, charter_b)["disposition"] == "queued"
    two_queued = build(register, [], clock)
    assert current(two_queued)["active"] == [A] and current(two_queued)["queued"] == [B]
    assert close(register, A, lease_a, kind="withdrawal")["disposition"] == "closed"
    qualify(register, A, intent="lost", source_revision=2, event_id="generated-lost")
    qualify(register, A, revision=2, event_id="generated-return")
    t = clock.at
    receipts = [
        receipt("real_case_evaluation", A, "epoch-1", at=t(100), source="generated-old-name"),
        receipt("owner_decision", A, "epoch-1-decision", at=t(200)),
        receipt(
            "real_case_evaluation",
            A,
            "epoch-2",
            at=t(300),
            engagement=2,
            source="generated-new-name",
        ),
        receipt("owner_decision", A, "epoch-2-decision", at=t(400), engagement=2),
    ]
    report = build(register, receipts, clock)
    assert counts(report)["qualified"] == 2
    assert counts(report)["accepted"] == 1
    assert len(report["columns"]["accepted"]["evidence"]) == 2
    assert current(report)["withdrawn"] == [A]
    assert current(report)["lost"] == []
    assert current(report)["queued"] == [B]
    assert current(report)["active"] == []
    assert report["duplicate_source_records"]["organizations"] == [A]
    assert report["drop_off"]["counts"]["engagement_withdrawn"] == 1


def test_stale_queue_and_expired_lease_need_reconciliation(register: Path, clock: Clock) -> None:
    charter_a = qualify(register, A)
    charter_b = qualify(register, B)
    assert acquire(register, A, charter_a, expires_in=60)["disposition"] == "acquired"
    assert acquire(register, B, charter_b, expires_in=60)["disposition"] == "queued"
    report = build(register, [], clock)
    assert current(report)["active"] == [A]
    assert current(report)["active_requires_reconciliation"] == [A]
    assert current(report)["queue_stale"] == [B]
    assert current(report)["queued"] == []
    assert codes(report) == [("active_lease_needs_reconciliation", None)]
    assert report["drop_off"]["counts"]["queue_stale"] == 1


def test_register_is_read_only_and_rebuilds_reproduce_the_same_revision(
    register: Path, clock: Clock
) -> None:
    receipts = cohort(register, clock)
    before = register.read_bytes()
    untouched = copy.deepcopy(receipts)
    first = build(register, receipts, clock)
    second = build(register, receipts, clock, supersedes_report_revision=None)
    assert first["report_revision"] == second["report_revision"]
    assert register.read_bytes() == before
    assert receipts == untouched
    assert sorted(path.name for path in register.parent.iterdir()) == ["register.sqlite3"]
    chained = build(register, receipts, clock, supersedes_report_revision=first["report_revision"])
    assert chained["supersedes_report_revision"] == first["report_revision"]
    assert chained["report_revision"] != first["report_revision"]
    assert chained["columns"] == first["columns"]


def test_costs_preserve_unknowns_and_separate_observed_from_modeled(
    register: Path, clock: Clock
) -> None:
    for name in (A, B, C, D, E):
        qualify(register, name)
    t = clock.at

    def accepted(name: str, *costs: Json | None) -> list[Json]:
        runs = [
            receipt("real_case_evaluation", name, f"{name}-run-{index}", at=t(100), cost=value)
            for index, value in enumerate(costs)
        ]
        return [*runs, receipt("owner_decision", name, f"{name}-decision", at=t(200))]

    receipts = [
        *accepted(A, cost("10.00"), cost("2.50")),
        *accepted(B, cost("4.00"), None),
        *accepted(C, cost("3.00", basis="modeled")),
        *accepted(D, cost("1.00"), cost("1.00", basis="modeled")),
    ]
    report = build(register, receipts, clock)
    assert report["costs"]["observed"] == {
        "accepted_organizations": 4,
        "organizations_with_known_cost": 1,
        "unknown_cost_organizations": 3,
        "state": "available",
        "currency": "USD",
        "total": "12.500000",
        "per_accepted_where_known": "12.500000",
    }
    assert report["costs"]["modeled"]["organizations_with_known_cost"] == 1
    assert report["costs"]["modeled"]["total"] == "3.000000"
    euro = build(register, [*receipts, *accepted(E, cost("4.00", currency="EUR"))], clock)
    assert euro["costs"]["observed"]["state"] == "unavailable"
    assert euro["costs"]["observed"]["reason"] == "multiple_currencies"


def test_timings_split_onboarding_wait_from_result_time_and_compute(
    register: Path, clock: Clock
) -> None:
    qualify(register, A)
    qualify(register, B)
    qualified_at = {
        event["organization_ref"]: event["observed_at"]
        for event in funnel.read_register_snapshot(register)["qualification_events"]
    }
    t = clock.at
    receipts = [
        receipt("ready_to_result", A, "a-ready", at=t(3600), customer_wait_seconds=1800),
        receipt("starter_readout", A, "a-starter", at=t(5400), compute_seconds=120),
        receipt("real_case_evaluation", A, "a-real", at=t(9000)),
        receipt("ready_to_result", B, "b-ready", at=t(7200), operator_setup_seconds=600),
        receipt("real_case_evaluation", B, "b-real", at=t(10_800)),
    ]
    timings = build(register, receipts, clock)["timings"]
    waits = sorted([t(3600) - qualified_at[A], t(7200) - qualified_at[B]])
    assert timings["onboarding_wait"]["known"] == 2
    assert timings["onboarding_wait"]["max_seconds"] == waits[1]
    assert timings["customer_wait"] == {
        "population": 2,
        "known": 1,
        "unknown": 1,
        "median_seconds": "1800.000000",
        "max_seconds": 1800,
    }
    assert timings["operator_setup"]["known"] == 1
    assert timings["ready_to_first_readout"]["median_seconds"] == "2700.000000"
    assert timings["ready_to_first_real_evaluation"]["median_seconds"] == "4500.000000"
    assert timings["first_readout_compute"] == {
        "population": 2,
        "known": 1,
        "unknown": 1,
        "median_seconds": "120.000000",
        "max_seconds": 120,
    }
    early = build(
        register,
        [*receipts, receipt("ready_to_result", B, "b-ready-early", at=qualified_at[B] - 10)],
        clock,
    )
    assert ("timing_inconsistent", "b-ready-early@1") in codes(early)
    assert early["timings"]["onboarding_wait"]["unknown"] == 1


def test_evidence_tiers_are_capped_split_and_never_externally_claimable(
    register: Path, clock: Clock
) -> None:
    for name in (A, B, C):
        qualify(register, name)
    t = clock.at
    receipts = [
        receipt("real_case_evaluation", A, "a-real", at=t(100), tier="customer_witnessed"),
        receipt("owner_decision", A, "a-decision", at=t(200), tier="customer_witnessed"),
        receipt("real_case_evaluation", B, "b-real", at=t(100), tier="digest_self_consistent_only"),
        receipt("owner_decision", B, "b-decision", at=t(200)),
        receipt(
            "real_case_evaluation",
            C,
            "c-weak",
            at=t(100),
            tier="digest_self_consistent_only",
            readout_revision_ref="c-weak-readout",
        ),
        receipt(
            "owner_decision", C, "c-weak-decision", at=t(150), readout_revision_ref="c-weak-readout"
        ),
        receipt("real_case_evaluation", C, "c-strong", at=t(300)),
        receipt("owner_decision", C, "c-strong-decision", at=t(400)),
    ]
    report = build(register, receipts, clock, setup_kind="generated")
    tiers = report["headline"]["accepted_by_evidence_tier"]
    assert tiers == {
        "digest_self_consistent_only": 1,
        "locally_keyed": 2,
        "signed_separated_custody": 0,
        "provider_witnessed": 0,
        "customer_witnessed": 0,
    }
    assert report["headline"]["externally_claimable_accepted"] == 0
    assert report["inputs"]["tier_claims_capped"] == 2


def test_verify_report_recomputes_totals_from_evidence_and_detects_tampering(
    register: Path, clock: Clock
) -> None:
    report = build(register, cohort(register, clock), clock)
    assert verify_report(report) == []
    inflated = copy.deepcopy(report)
    inflated["columns"]["accepted"]["count"] = 3
    assert "column_count_mismatch:accepted" in verify_report(inflated)
    assert "report_revision_mismatch" in verify_report(inflated)
    headline = copy.deepcopy(report)
    headline["headline"]["display"] = "3/10"
    assert "headline_mismatch" in verify_report(headline)
    stripped = copy.deepcopy(report)
    stripped["columns"]["paid_recurring"]["evidence"] = []
    assert "column_evidence_missing:paid_recurring" in verify_report(stripped)
    external = copy.deepcopy(report)
    external["headline"]["externally_claimable_accepted"] = 2
    assert "external_claim_mismatch" in verify_report(external)
    assert verify_report({"schema_version": "other"}) == ["report_schema_unknown"]
    with pytest.raises(FunnelError) as refused:
        weekly_view(inflated)
    assert refused.value.code == "report_inconsistent"


def _strings(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for key, item in value.items() for s in (*_strings(key), *_strings(item))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def test_weekly_view_is_evidence_free(register: Path, clock: Clock) -> None:
    receipts = cohort(register, clock)
    readiness = readiness_receipt(ope522_readout())
    report = build(
        register, receipts, clock, outcome_disclosure_permitted=True, readiness=readiness
    )
    view = weekly_view(report)
    snapshot = funnel.read_register_snapshot(register)
    private = {A, B, C, D, E, OUTSIDER}
    for item in receipts:
        private |= {item["receipt_id"], item["evidence_digest"], f"{item['receipt_id']}@1"}
        private |= {s for s in _strings(item["facts"]) if s.startswith("generated-")}
        private |= {item["source_record_ref"]} - {None}
    for row in (*snapshot["qualification_events"], *snapshot["leases"], *snapshot["waiters"]):
        private |= {s for s in _strings(row) if s.startswith(("generated-", "qualification-"))}
        private |= {row.get("lease_ref"), row.get("event_digest"), row.get("qualification_digest")}
    private -= {None}
    exposed = _strings(view)
    assert not [text for text in exposed for secret in private if secret in text]
    assert "pending_ages_seconds" not in view["outcome_rollup"]["readiness"]
    assert view["columns"]["accepted"] == 2
    assert view["collapsed_duplicate_evidence"]["real_case_evaluation"] == 1
    assert view["mismatches_by_code"] == {"receipt_not_admitted": 1}
    assert view["next_missing_stage"] == {"none": 4, "ready_to_result": 1}
    assert view["report_revision"] == report["report_revision"]


def test_readiness_rollup_reconciles_workflows_to_the_cohort_total(
    register: Path, clock: Clock
) -> None:
    receipts = cohort(register, clock)
    base = build(register, receipts, clock)
    report = build(
        register,
        receipts,
        clock,
        outcome_disclosure_permitted=True,
        readiness=readiness_receipt(ope522_readout()),
    )
    readiness = report["outcome_rollup"]["readiness"]
    assert readiness["state"] == "available"
    assert readiness["fit_ready_display"] == "2/5"
    assert (readiness["simple_resolved"], readiness["still_waiting"]) == (1, 2)
    assert readiness["zero_seed_starters"] == 1
    assert readiness["median_seconds_to_ready"] == "7200.000000"
    assert readiness["unit"].startswith("workflow")
    assert report["columns"] == base["columns"]

    def held(**changes: Any) -> str:
        receipt_value = readiness_receipt(ope522_readout(**changes))
        rollup = build(
            register, receipts, clock, outcome_disclosure_permitted=True, readiness=receipt_value
        )["outcome_rollup"]["readiness"]
        assert rollup["state"] == "held" and rollup["gate"] == "OPE-522"
        return str(rollup["reason"])

    inconsistent = "ope522_readiness_receipt_inconsistent"
    assert held(workflows=6) == inconsistent
    assert held(pending_ages_seconds=[40_000]) == inconsistent
    assert held(ever_fit_ready=1) == inconsistent
    # No ready workflow can have a median time to readiness.
    assert held(ever_fit_ready=0, current=[["held", 2], ["simple_resolved", 3]]) == inconsistent
    invalid = "ope522_readiness_receipt_invalid"
    assert held(current=[["held", 2], ["fit_ready", 2], ["simple_resolved", 1]]) == invalid
    assert held(current=[["fit_ready", 2], ["held", 2], ["unknown_state", 1]]) == invalid
    assert held(window_end=200_000) == invalid
    assert held(extra_field=1) == invalid
    tampered = readiness_receipt(ope522_readout())
    tampered["readout"]["workflows"] = 7
    rollup = build(
        register, receipts, clock, outcome_disclosure_permitted=True, readiness=tampered
    )["outcome_rollup"]["readiness"]
    assert rollup["reason"] == invalid
    foreign = readiness_receipt(ope522_readout(), setup="real_customer")
    rollup = build(register, receipts, clock, outcome_disclosure_permitted=True, readiness=foreign)[
        "outcome_rollup"
    ]["readiness"]
    assert rollup["reason"] == "ope522_readiness_receipt_outside_setup_scope"
    withheld = build(register, receipts, clock, readiness=readiness_receipt(ope522_readout()))
    assert withheld["outcome_rollup"]["readiness"]["state"] == "withheld"


def _tuning_digest() -> Callable[[object], str]:
    """Execute OPE-517's own canonical/digest functions without importing its package."""
    tree = ast.parse(TUNING_SOURCE.read_text())
    wanted = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in ("canonical", "digest")
    ]
    assert {node.name for node in wanted} == {"canonical", "digest"}
    namespace: dict[str, Any] = {"json": json, "hashlib": hashlib}
    exec(compile(ast.Module(body=wanted, type_ignores=[]), str(TUNING_SOURCE), "exec"), namespace)
    return namespace["digest"]  # type: ignore[no-any-return]


def tuning_result(verdict: str = "inconclusive", **changes: Any) -> Json:
    claims = {
        "measured_benefit": "Measured benefit on untouched real groups (generated fixture only)",
        "no_material_benefit": "No material benefit; keep the cheaper real-only route",
        "regression": "Regression; no augmented promotion",
        "inconclusive": "Augmentation benefit unproven",
        "unrun": "Comparison unrun",
    }
    body = {
        "schema": "opentour.tuning-benefit-result.v1",
        "protocol_digest": "sha256:" + digest("generated-protocol"),
        "arm_seal_digest": "sha256:" + digest("generated-seal"),
        "evidence_tier": "generated",
        "claim_scope": "implementation_only",
        "verdict": verdict,
        "status": "scored",
        "reasons": [],
        "claim": claims[verdict],
        "improvement_claim_blocked": verdict != "measured_benefit",
        "significance_claim": False,
        "preferred_arm": "real_only",
        "promotion_allowed": verdict == "measured_benefit",
        "denominator": {"eligible_untouched": 40, "independent_groups": 20},
        "paired": None,
        "family_count_reconciliation": None,
        "unsatisfied_external_gates": ["authorized_real_heldout_workplace_cases_unavailable"],
        **changes,
    }
    return {"body": body, "result_digest": _tuning_digest()(body)}


def label_record(**changes: Any) -> Json:
    return {
        "schema": "opentour.label-efficiency.v1",
        "tested_budgets": [40, 100],
        "ope_522_handoff": [],
        "evidence_tier": "generated",
        "launches_job": False,
        "claim": "60% fewer real labels at the same confirmed target",
        "reduction_bps": 6000,
        "real_only_budget": 100,
        "augmented_budget": 40,
        "labor_delta_minutes": 20,
        "labor_note": "negative savings accepted; hours are never inferred from label counts",
        **changes,
    }


def test_tuning_and_label_outcomes_fail_closed_and_keep_negative_savings(
    register: Path, clock: Clock
) -> None:
    register.touch(mode=0o600)

    def outcome(**inputs: Any) -> Json:
        return build(register, [], clock, outcome_disclosure_permitted=True, **inputs)[
            "outcome_rollup"
        ]

    assert outcome(tuning_benefit=tuning_result())["tuning_benefit"]["claim"] == (
        "Augmentation benefit unproven"
    )
    assert outcome(tuning_benefit=tuning_result("unrun"))["tuning_benefit"]["claim"] == (
        "Augmentation benefit unproven"
    )
    measured = outcome(tuning_benefit=tuning_result("measured_benefit"))["tuning_benefit"]
    assert measured["state"] == "available" and measured["promotion_allowed"] is True
    assert measured["claim_scope"] == "implementation_only"
    regression = outcome(tuning_benefit=tuning_result("regression"))["tuning_benefit"]
    assert regression["claim"] == "Regression; no augmented promotion"
    forged = tuning_result()
    forged["body"]["verdict"] = "measured_benefit"
    assert outcome(tuning_benefit=forged)["tuning_benefit"]["reason"] == "ope517_result_invalid"
    rescoped = tuning_result("measured_benefit")
    rescoped["body"]["claim_scope"] = "customer_benefit"
    assert outcome(tuning_benefit=rescoped)["tuning_benefit"]["reason"] == ("ope517_result_invalid")
    extra = tuning_result(unreviewed_field=True)
    assert outcome(tuning_benefit=extra)["tuning_benefit"]["reason"] == "ope517_result_invalid"
    retained = tuning_result(retained_route="real_only")
    assert outcome(tuning_benefit=retained)["tuning_benefit"]["state"] == "available"
    real_scope = build(
        register,
        [],
        clock,
        setup_kind="real_customer",
        outcome_disclosure_permitted=True,
        tuning_benefit=tuning_result("measured_benefit"),
        label_efficiency=label_record(),
    )["outcome_rollup"]
    assert real_scope["tuning_benefit"]["reason"] == "ope517_result_outside_setup_scope"
    assert real_scope["tuning_benefit"]["claim"] == "Augmentation benefit unproven"
    assert real_scope["label_efficiency"]["reason"] == (
        "ope517_label_efficiency_outside_setup_scope"
    )

    label = outcome(label_efficiency=label_record())["label_efficiency"]
    assert label["claim"] == "60% fewer real labels at the same confirmed target"
    assert (label["expert_minutes_delta"], label["expert_minutes_direction"]) == (
        20,
        "additional_burden",
    )
    unmeasured = outcome(
        label_efficiency=label_record(
            claim="Label savings not measured",
            reason="single_checkpoint_same_label_quality_lift_only",
            labor_delta_minutes=None,
        )
    )["label_efficiency"]
    assert unmeasured["expert_minutes_direction"] == "not_measured"
    jobs = outcome(label_efficiency=label_record(launches_job=True))["label_efficiency"]
    assert jobs["state"] == "held" and jobs["claim"] == "Label savings not measured"


def test_malformed_receipts_fail_alone_and_the_rest_of_the_batch_counts(
    register: Path, clock: Clock
) -> None:
    qualify(register, A)
    t = clock.at
    good = receipt("payment", A, "good", at=t(100))

    def variant(**changes: Any) -> Json:
        value = copy.deepcopy(good)
        facts = changes.pop("facts", {})
        value.update({"receipt_id": "bad", **changes})
        value["facts"].update(facts)
        return value

    unexpected = variant()
    unexpected["protected_answer"] = "generated"
    cases: list[tuple[object, str]] = [
        ("not an object", "receipt_fields_invalid"),
        ({"receipt_id": float("nan")}, "receipt_not_canonical_json"),
        (object(), "receipt_not_canonical_json"),
        (
            variant(schema_version="opentour.pilot.cohort-stage-receipt.v0"),
            "receipt_schema_unknown",
        ),
        (unexpected, "receipt_fields_invalid"),
        (variant(stage="approval"), "receipt_value_invalid"),
        (variant(revision=True), "receipt_value_invalid"),
        (variant(revision=0), "receipt_value_invalid"),
        (variant(revision=2, supersedes_revision=True), "receipt_value_invalid"),
        (variant(engagement_revision=-1), "receipt_value_invalid"),
        (variant(organization_ref="Generated Name With Spaces"), "receipt_value_invalid"),
        (variant(evidence_digest="F" * 64), "receipt_value_invalid"),
        (variant(setup_kind="production"), "receipt_value_invalid"),
        (variant(effective_at=t(200)), "receipt_time_invalid"),
        (variant(recorded_at=-1), "receipt_time_invalid"),
        (variant(recorded_at=2**63), "receipt_time_invalid"),
        (variant(evidence_tier="worker_attested"), "receipt_tier_unknown"),
        (variant(setup_kind="real_customer"), "receipt_outside_setup_scope"),
        (variant(facts={"recurring": 1}), "receipt_value_invalid"),
    ]
    report = build(register, [good, *(case for case, _ in cases)], clock)
    assert counts(report)["paid_recurring"] == 1
    assert [item["code"] for item in report["quarantined"]] == [code for _, code in cases]
    assert [item["index"] for item in report["quarantined"]] == list(range(1, len(cases) + 1))
    assert report["inputs"]["receipts_read"] == len(cases) + 1
    decision = receipt("owner_decision", A, "stop-without-reason", at=t(100), outcome="stop")
    future = receipt("owner_decision", A, "decided-later", at=t(100), decided_on="2999-01-01")
    late = receipt("payment", A, "late", at=t(100), recorded=clock.as_of + 1)
    unbound = receipt("owner_decision", A, "unbound", at=t(100), readout_revision_ref="absent")
    later = build(register, [good, decision, future, late, unbound], clock)
    assert [item["code"] for item in later["quarantined"]] == [
        "receipt_value_invalid",
        "receipt_time_invalid",
    ]
    assert later["inputs"]["recorded_after_cutoff"] == 1
    assert codes(later) == [("decision_readout_unknown", "unbound@1")]


def test_report_arguments_and_batch_size_are_range_checked(register: Path, clock: Clock) -> None:
    register.touch(mode=0o600)
    for options, code in (
        ({"as_of": -1}, "as_of_invalid"),
        ({"as_of": True}, "as_of_invalid"),
        ({"setup_kind": "production"}, "setup_kind_invalid"),
        ({"outcome_disclosure_permitted": 1}, "outcome_disclosure_invalid"),
        ({"supersedes_report_revision": "previous"}, "previous_report_invalid"),
        ({"receipts": "not-a-list"}, "receipt_batch_invalid"),
        ({"receipts": [{}] * (funnel.MAX_RECEIPTS + 1)}, "receipt_batch_invalid"),
    ):
        arguments: dict[str, Any] = {
            "register": register,
            "receipts": [],
            "as_of": clock.as_of,
            "setup_kind": "generated",
            **options,
        }
        with pytest.raises(FunnelError) as refused:
            build_report(**arguments)
        assert refused.value.code == code


def test_field_map_is_versioned_and_an_unpublished_edit_refuses_reports(
    register: Path, clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    register.touch(mode=0o600)
    assert funnel.FIELD_MAP_DIGEST == digest(funnel.FIELD_MAP)
    assert funnel.FIELD_MAP["schema_version"] == funnel.FIELD_MAP_VERSION
    report = build(register, [], clock)
    assert (report["field_map_version"], report["field_map_digest"]) == (
        funnel.FIELD_MAP_VERSION,
        funnel.FIELD_MAP_DIGEST,
    )
    edited = copy.deepcopy(funnel.FIELD_MAP)
    edited["columns"][0]["credited_only_by"] = "a CRM stage"
    monkeypatch.setattr(funnel, "FIELD_MAP_DIGEST", digest(edited))
    with pytest.raises(FunnelError) as refused:
        build(register, [], clock)
    assert refused.value.code == "field_map_unpublished"
    for column in funnel.FIELD_MAP["columns"]:
        assert column["operator_link"] == f"{funnel.OPERATOR_GUIDE}#{column['column']}"
        assert column["owner_role"]
        assert "crm_stage" in column["never_from"]


def _module_strings(tree: ast.Module) -> dict[str, object]:
    values: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and isinstance(node.value, ast.Constant):
                values[target.id] = node.value.value
            if (
                isinstance(target, ast.Name)
                and target.id == "Verdict"
                and isinstance(node.value, ast.Subscript)
                and isinstance(node.value.slice, ast.Tuple)
            ):
                values["Verdict"] = tuple(
                    item.value for item in node.value.slice.elts if isinstance(item, ast.Constant)
                )
    return values


def _dict_keys(node: ast.AST) -> set[str]:
    if not isinstance(node, ast.Dict):
        return set()
    return {key.value for key in node.keys if isinstance(key, ast.Constant)}


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    found = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    if len(found) != 1:
        raise AssertionError(f"canonical OPE-517 definition not found: {name}")
    return found[0]


def tuning_vocabulary_drift(source: str) -> list[str]:
    """Compare this consumer's OPE-517 vocabulary with the producer's own source text."""
    tree = ast.parse(source)
    values = _module_strings(tree)
    missing = {"RESULT_SCHEMA", "LABEL_SCHEMA", "UNPROVEN", "NOT_MEASURED", "Verdict"} - set(values)
    if missing:
        raise AssertionError(f"canonical OPE-517 definition not found: {sorted(missing)}")
    body = set()
    for node in ast.walk(_function(tree, "_result")):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "body":
            body = _dict_keys(node.value)
    extras = set()
    for node in ast.walk(_function(tree, "measure")):
        if isinstance(node, ast.keyword) and node.arg == "extra":
            extras |= _dict_keys(node.value)
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", None) == "extra":
            extras |= _dict_keys(node.value)
    label_base: set[str] = set()
    label_extra: set[str] = set()
    for node in ast.walk(_function(tree, "label_efficiency")):
        if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", None) == "out":
            label_base = _dict_keys(node.value)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            label_extra |= _dict_keys(node.right)
    comparisons = {
        "result_schema": (values["RESULT_SCHEMA"], funnel.TUNING_RESULT_SCHEMA),
        "label_schema": (values["LABEL_SCHEMA"], funnel.LABEL_EFFICIENCY_SCHEMA),
        "unproven": (values["UNPROVEN"], funnel.AUGMENTATION_UNPROVEN),
        "not_measured": (values["NOT_MEASURED"], funnel.LABEL_SAVINGS_NOT_MEASURED),
        "verdicts": (values["Verdict"], funnel.TUNING_VERDICTS),
        "result_fields": (body, set(funnel.TUNING_RESULT_FIELDS)),
        "result_extras": (extras, set(funnel.TUNING_RESULT_EXTRAS)),
        "label_fields": (label_base, set(funnel.LABEL_EFFICIENCY_FIELDS)),
        "label_extras": (label_extra, set(funnel.LABEL_EFFICIENCY_EXTRAS)),
    }
    return sorted(
        name for name, (producer, consumer) in comparisons.items() if producer != consumer
    )


def test_ope517_vocabulary_matches_the_producer_source_and_the_check_catches_drift() -> None:
    source = TUNING_SOURCE.read_text()
    assert tuning_vocabulary_drift(source) == []
    renamed = source.replace(
        'RESULT_SCHEMA = "opentour.tuning-benefit-result.v1"',
        'RESULT_SCHEMA = "opentour.tuning-benefit-result.v2"',
    )
    assert renamed != source
    assert tuning_vocabulary_drift(renamed) == ["result_schema"]
    widened = source.replace(
        '"significance_claim": False,', '"significance_claim": False, "new": 1,'
    )
    assert widened != source
    assert tuning_vocabulary_drift(widened) == ["result_fields"]
    with pytest.raises(AssertionError, match="canonical OPE-517 definition not found"):
        tuning_vocabulary_drift(source.replace("UNPROVEN = ", "UNPROVEN_TEXT = "))
    producer = _tuning_digest()
    body = {"schema": "generated", "value": [1, "x"]}
    assert producer(body) == "sha256:" + digest(body)


def _slug(heading: str) -> str:
    text = heading.strip().lower().replace("`", "")
    return re.sub(r"\s+", "-", re.sub(r"[^\w\s-]", "", text))


def guide_drift(text: str) -> list[str]:
    """Compare the published guide's names with the implemented field map and codes."""
    sections = re.split(r"(?m)^## ", text)
    named = {part.split("\n", 1)[0].strip(): part for part in sections}
    for required in ("Columns", "Current states", "Stage receipt facts", "Mismatch codes"):
        if required not in named:
            raise AssertionError(f"guide section not found: {required}")
    columns = re.findall(r"(?m)^### `([a-z_]+)`$", named["Columns"])
    states = re.findall(r"(?m)^\| `([a-z_]+)` \|", named["Current states"])
    facts = {
        stage: re.findall(r"`([a-z_]+)`", cell)
        for stage, cell in re.findall(
            r"(?m)^\| `([a-z_]+)` \| (.+) \|$", named["Stage receipt facts"]
        )
    }
    mismatches = dict(re.findall(r"(?m)^\| `([a-z_]+)` \| `([a-z_]+)` \|", named["Mismatch codes"]))
    anchors = {_slug(line) for line in re.findall(r"(?m)^#{2,3} (.+)$", text)}
    links = {item["operator_link"].split("#", 1)[1] for item in funnel.FIELD_MAP["columns"]}
    problems = []
    if columns != list(funnel.COLUMNS):
        problems.append("columns")
    if states != list(funnel.CURRENT_STATES):
        problems.append("current_states")
    if facts != {stage: list(spec) for stage, spec in funnel._FACTS.items()}:
        problems.append("stage_facts")
    if mismatches != funnel.MISMATCH_OWNERS:
        problems.append("mismatch_codes")
    if not (links | {"mismatch-codes"}) <= anchors:
        problems.append("operator_links")
    for name in (
        funnel.FIELD_MAP_VERSION,
        funnel.RECEIPT_VERSION,
        funnel.BATCH_VERSION,
        funnel.READINESS_RECEIPT_VERSION,
        funnel.READINESS_SCHEMA,
        funnel.WEEKLY_VERSION,
    ):
        if f"`{name}`" not in text:
            problems.append(f"version:{name}")
    return problems


def test_operator_guide_matches_the_implementation_and_the_check_catches_drift() -> None:
    text = GUIDE.read_text()
    assert guide_drift(text) == []
    assert guide_drift(text.replace("### `exact_fit`", "### `model_fit`")) == [
        "columns",
        "operator_links",
    ]
    assert guide_drift(
        text.replace(
            "| `receipt_not_admitted` | `intake_owner` |",
            "| `receipt_not_admitted` | `cohort_operator` |",
        )
    ) == ["mismatch_codes"]
    assert guide_drift(
        text.replace("`fee_waiver` | `status`, `terms_ref`", "`fee_waiver` | `status`")
    ) == ["stage_facts"]
    with pytest.raises(AssertionError, match="guide section not found"):
        guide_drift(text.replace("## Mismatch codes", "## Codes"))


def _private_file(path: Path, value: object) -> Path:
    path.write_text(json.dumps(value))
    path.chmod(0o600)
    return path


def test_owner_run_command_prints_only_the_weekly_view_and_never_overwrites(
    tmp_path: Path,
) -> None:
    register = private_register(tmp_path)
    qualify(register, A)
    now = int(time.time())
    receipts = [
        receipt("real_case_evaluation", A, "cli-real", at=now),
        receipt("owner_decision", A, "cli-decision", at=now),
        receipt("payment", A, "cli-payment", at=now),
    ]
    directory = register.parent
    batch = _private_file(
        directory / "receipts.json",
        {"schema_version": funnel.BATCH_VERSION, "receipts": receipts},
    )
    output = directory / "report-1.json"
    environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

    def run(*arguments: object) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "opentour_cell.pilot.cohort_funnel", *map(str, arguments)],
            capture_output=True,
            text=True,
            check=False,
            env=environment,
        )

    as_of = int(time.time())
    common = ("--register", register, "--receipts", batch, "--as-of", as_of, "--setup-kind")
    first = run(*common, "generated", "--output", output)
    assert first.returncode == 0, first.stderr
    view = json.loads(first.stdout)
    assert view["schema_version"] == funnel.WEEKLY_VERSION
    assert view["headline"]["display"] == "1/10"
    assert A not in first.stdout and "cli-real" not in first.stdout
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    report = json.loads(output.read_text())
    assert verify_report(report) == []
    assert view == weekly_view(report)
    again = run(*common, "generated", "--output", output)
    assert again.returncode == 1 and again.stdout == ""
    assert json.loads(output.read_text()) == report
    chained = directory / "report-2.json"
    second = run(*common, "generated", "--output", chained, "--previous-report", output)
    assert second.returncode == 0, second.stderr
    assert (
        json.loads(chained.read_text())["supersedes_report_revision"] == (report["report_revision"])
    )
    future = run(
        "--register",
        register,
        "--receipts",
        batch,
        "--as-of",
        as_of + 3600,
        "--setup-kind",
        "generated",
        "--output",
        directory / "future.json",
    )
    assert (future.returncode, future.stdout) == (1, "")
    assert "as_of_in_future" in future.stderr
    absent = run(
        "--register",
        directory / "absent.sqlite3",
        "--receipts",
        batch,
        "--as-of",
        as_of,
        "--setup-kind",
        "generated",
        "--output",
        directory / "absent.json",
    )
    assert absent.returncode == 1 and "register_unavailable" in absent.stderr
    loose = _private_file(directory / "loose.json", {"receipts": receipts})
    invalid = run(
        "--register",
        register,
        "--receipts",
        loose,
        "--as-of",
        as_of,
        "--setup-kind",
        "generated",
        "--output",
        directory / "loose-report.json",
    )
    assert invalid.returncode == 1 and "receipt_batch_invalid" in invalid.stderr
    assert not (directory / "future.json").exists()
    assert not (directory / "loose-report.json").exists()
