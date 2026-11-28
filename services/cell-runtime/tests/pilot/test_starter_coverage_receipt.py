"""Per-branch coverage receipt: generated owner fixtures, explicit expected counts.

The expected numbers below are written by hand from each fixture's construction.
They are the independent oracle; `verify_receipt` is only a tamper check.
"""

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_cases import ProposedCase, StarterCases
from opentour_cell.pilot.starter_coverage_receipt import (
    NEXT_EVIDENCE,
    build_receipt,
    handoff,
    owner_coverage_receipt,
    session_coverage_receipt,
    verify_receipt,
)
from opentour_cell.pilot.starter_map_confirmation import StarterCaseSession
from opentour_cell.pilot.starter_scoring import _metrics
from test_starter_case_authoring import P1_OWNER_LABELS, SevenInvoiceFamilies
from test_starter_cases import OWNER, PLAN, WORK, WORKER, adjudicate_all, case, freeze_seven
from test_starter_map_confirmation import FiveFamilies, adjudicate

pytest_plugins = (
    "test_starter_cases",
    "test_starter_setup",
    "test_starter_map_confirmation",
    "test_starter_case_authoring",
)

HANDOFF_KEYS = {
    "schema_version",
    "freeze_ref",
    "freeze_revision",
    "coverage_receipt_ref",
    "coverage_receipt_digest",
    "reserved_family_refs",
    "reserved_families",
    "reserved_cases",
    "unresolved",
    "excluded",
    "state",
    "evidence_origin",
    "training_credit",
}


def replay(cases, actor, work):
    """The same owner scope and sealed pack the receipt was computed from."""
    with cases._scope(actor, work, "review") as scope, cases._db() as db:
        return scope, StarterCases._ref(scope), cases._load(db, scope)


def session_replay(session):
    with session._pin("review") as pinned:
        return replay(session.core, pinned.principal, pinned.scope.work)


def by_action(session, receipt):
    """Map each single-action map branch row to its confirmed material action."""
    actions = {
        b["branch_id"]: b["coverage_actions"][0] for b in session.review()["authority"]["branches"]
    }
    return {actions[row["branch_id"]]: row for row in receipt["branches"]}


def counts(row):
    return (row["numerator"], row["denominator"], row["reserved_cases"], row["state"])


def assert_handoff_discloses_no_labels(receipt, forbidden):
    value = handoff(receipt)
    assert set(value) == HANDOFF_KEYS
    assert value["coverage_receipt_digest"] == receipt["receipt_digest"]
    assert value["reserved_family_refs"] == receipt["reserved_family_refs"]
    assert value["training_credit"] == 0
    text = json.dumps(value)
    for token in forbidden:
        assert token not in text, token
    return value


def test_core_seven_family_partial_branch_is_held_and_counted_per_family(core):
    service, _ = core
    frozen, _ = freeze_seven(service)
    receipt = owner_coverage_receipt(service, OWNER, WORK)
    assert receipt["totals"] == {
        "proposed": 7,
        "reviewed": 7,
        "resolved": 6,
        "unresolved": 1,
        "development": 2,
        "reserved": 4,
        "excluded": 1,
    }
    (route,) = receipt["branches"]
    assert counts(route) == (4, 7, 4, "partially_covered")
    assert {c["action"]: c["reserved_families"] for c in route["cells"]} == {
        "SOFTWARE": 1,
        "FREIGHT": 1,
        "SUPPLIES": 1,
        "UTILITIES": 0,
        "TAX": 0,
        "REVIEW": 1,
    }
    assert route["verdict"] == "held" and route["next_evidence"] == NEXT_EVIDENCE
    assert receipt["claims"] == {
        "all_branches_verdict": False,
        "verdict_scope": [],
        "held_branches": ["route"],
        "training_credit": 0,
        "live_recurrence": 0,
    }
    assert tuple(receipt["claims"]["held_branches"]) == frozen.uncovered_critical
    assert receipt["reserved_family_refs"] == list(frozen.reserved_family_refs)
    dispositions = {row["case_id"]: row["disposition"] for row in receipt["cases"]}
    assert dispositions == {
        "software": "reserved_evaluation",
        "freight": "reserved_evaluation",
        "supplies": "reserved_evaluation",
        "review": "reserved_evaluation",
        "utilities": "development",
        "tax": "development",
        "mixed": "excluded_unresolved",
    }
    adjudications = {row["case_id"]: row["adjudication"] for row in receipt["cases"]}
    assert adjudications["freight"] == "edit" and adjudications["mixed"] == "unresolved"
    assert receipt["audit"]["edited_labels"] == 1
    assert receipt["audit"]["adjudicators"] == [OWNER.actor]
    verify_receipt(receipt, *replay(service, OWNER, WORK))
    before = service.path.read_bytes()
    assert (
        owner_coverage_receipt(StarterCases(service.path.parent, core[1]), OWNER, WORK) == receipt
    )
    assert service.path.read_bytes() == before
    assert sorted(p.name for p in service.path.parent.iterdir()) == ["starter-cases.sqlite3"]


def test_receipt_needs_a_freeze_and_the_named_owner(core):
    service, _ = core
    cases = (case("SOFTWARE"),)
    service.propose(OWNER, WORK, cases)
    with pytest.raises(IntakeError, match="starter_freeze_required"):
        owner_coverage_receipt(service, OWNER, WORK)
    adjudicate_all(service, cases)
    service.freeze(OWNER, WORK, splits={"software": "reserved"}, plan=PLAN, expected_revision=1)
    for actor in (WORKER, replace(OWNER, tenant="foreign")):
        with pytest.raises(IntakeError):
            owner_coverage_receipt(service, actor, WORK)
    assert owner_coverage_receipt(service, OWNER, WORK)["totals"]["reserved"] == 1


@pytest.mark.parametrize(
    "tamper",
    ["count", "reserved_ref", "coverage_cell", "extra_cell", "target", "split", "critical"],
)
def test_pack_that_disagrees_with_its_freeze_holds(core, tamper):
    service, _ = core
    freeze_seven(service)
    scope, ref, pack = replay(service, OWNER, WORK)
    pack = json.loads(canonical(pack))
    frozen = pack["frozen"]
    if tamper == "count":
        frozen["reserved"] = 5
    elif tamper == "reserved_ref":
        frozen["reserved_family_refs"][0] = "starter-family-" + "0" * 40
    elif tamper == "coverage_cell":
        frozen["coverage"][0][2] += 1
    elif tamper == "extra_cell":
        frozen["coverage"].append(["unknown-branch", "SOFTWARE", 0])
    elif tamper == "target":
        next(c for c in pack["cases"] if c["case_id"] == "software")["owner_adjudicated_target"] = (
            "TAX"
        )
    elif tamper == "split":
        next(c for c in pack["cases"] if c["case_id"] == "mixed")["split_ref"] = "reserved"
    else:
        scope = replace(scope, branches=(replace(scope.branches[0], critical=False),))
    with pytest.raises(IntakeError):
        build_receipt(scope, ref, pack)


def test_verify_detects_an_edited_receipt_even_with_a_recomputed_digest(core):
    service, _ = core
    freeze_seven(service)
    scope, ref, pack = replay(service, OWNER, WORK)
    receipt = build_receipt(scope, ref, pack)
    verify_receipt(receipt, scope, ref, pack)
    edited = json.loads(canonical(receipt))
    edited["branches"][0]["numerator"] = 7
    with pytest.raises(IntakeError, match="mismatch"):
        verify_receipt(edited, scope, ref, pack)
    body = {k: v for k, v in edited.items() if k not in {"receipt_digest", "receipt_ref"}}
    rebuilt = dict(body)
    rebuilt["receipt_digest"] = digest(body)
    rebuilt["receipt_ref"] = "starter-coverage-receipt-" + rebuilt["receipt_digest"][:40]
    with pytest.raises(IntakeError, match="mismatch"):
        verify_receipt(rebuilt, scope, ref, pack)


def test_owner_counterexamples_keep_origin_and_never_multiply_a_family(core):
    service, _ = core
    generated = (case("SOFTWARE"), case("FREIGHT", suggestion="FREIGHT"))
    service.propose(OWNER, WORK, generated)
    same_family = ProposedCase(
        "software-owner-variant",
        "software",
        "software",
        "route",
        (("category", "SOFTWARE"), ("po", "owner-po")),
        "SOFTWARE",
        "Owner variant of the same template",
        "policy-section-1",
    )
    new_family = ProposedCase(
        "owner-tax",
        "owner-tax",
        "owner-tax",
        "route",
        (("category", "TAX"),),
        "TAX",
        "Owner counterexample",
        "policy-section-1",
    )
    for revision, item in enumerate((same_family, new_family)):
        service.add_counterexample(
            OWNER,
            WORK,
            case=item,
            expected_revision=revision,
            operation_ref=f"counterexample-{revision}",
        )
    rows = service.review(OWNER, WORK)["cases"]
    for revision, row in enumerate(rows, start=2):
        service.adjudicate(
            OWNER,
            WORK,
            case_id=row["case_id"],
            target=row["suggestion"],
            reason="Owner checked the cited policy",
            expected_revision=revision,
            operation_ref=f"label-{revision}",
        )
    service.freeze(
        OWNER,
        WORK,
        splits={"software": "reserved", "freight": "reserved", "owner-tax": "reserved"},
        plan=PLAN,
        expected_revision=6,
    )
    receipt = owner_coverage_receipt(service, OWNER, WORK)
    families = {row["family_id"]: row for row in receipt["families"]}
    assert families["software"]["cases"] == 2 and families["software"]["counted_as"] == 1
    assert families["software"]["origins"] == ["generated_starter", "owner_counterexample"]
    assert families["owner-tax"]["origins"] == ["owner_counterexample"]
    assert receipt["duplicate_cases_collapsed"] == 1
    (route,) = receipt["branches"]
    assert (route["numerator"], route["denominator"], route["reserved_cases"]) == (3, 3, 4)
    origins = {row["case_id"]: row["origin"] for row in receipt["cases"]}
    assert origins["software-owner-variant"] == origins["owner-tax"] == "owner_counterexample"


@pytest.mark.parametrize("omit_missing_po", [False, True])
def test_five_family_purchase_receipt_and_missing_po_removal(joined, omit_missing_po):
    h, session, _, _, _ = joined
    session.propose(FiveFamilies())
    blind = session.review()
    assert all(row["owner_adjudicated_target"] is None for row in blind["cases"])
    with pytest.raises(IntakeError, match="starter_freeze_required"):
        session_coverage_receipt(session)
    adjudicate(session)
    split = {
        f"family-{i}": "excluded" if omit_missing_po and i in (0, 1) else "reserved"
        for i in range(5)
    }
    frozen = session.freeze(splits=split, expected_revision=5)
    before = {p.name: p.read_bytes() for p in h.path.parent.iterdir() if p.is_file()}
    receipt = session_coverage_receipt(session)
    rows = by_action(session, receipt)
    if omit_missing_po:
        assert counts(rows["REVIEW"]) == (0, 2, 0, "uncovered")
        assert rows["REVIEW"]["excluded_families"] == 2
        assert rows["REVIEW"]["verdict"] == "held"
        assert rows["REVIEW"]["per_branch_rates"] == "unavailable"
        assert rows["REVIEW"]["next_evidence"] == NEXT_EVIDENCE
        # Zero reserved REVIEW cases is an unavailable rate under the shared arithmetic.
        assert _metrics([])["model"]["wrong_automatic"]["value"] is None
        assert _metrics([])["model"]["exact_action"]["value"] is None
        assert receipt["claims"]["held_branches"] == [rows["REVIEW"]["branch_id"]]
        assert not receipt["claims"]["all_branches_verdict"]
        missing_po = {
            row["disposition"]
            for row in receipt["cases"]
            if row["family_id"] in {"family-0", "family-1"}
        }
        assert missing_po == {"excluded_by_owner"}
    else:
        assert counts(rows["REVIEW"]) == (2, 2, 2, "covered")
        assert receipt["claims"]["all_branches_verdict"]
        assert receipt["claims"]["held_branches"] == []
    assert counts(rows["APPROVE"]) == (1, 1, 1, "covered")
    assert counts(rows["MANUAL"]) == (2, 2, 2, "covered")
    assert receipt["claims"]["training_credit"] == 0
    assert tuple(receipt["claims"]["held_branches"]) == frozen.uncovered_critical
    assert receipt["reserved_family_refs"] == list(frozen.reserved_family_refs)
    edited = [row["case_id"] for row in receipt["cases"] if row["adjudication"] == "edit"]
    assert edited == ["case-2"]
    verify_receipt(receipt, *session_replay(session))
    assert {p.name: p.read_bytes() for p in h.path.parent.iterdir() if p.is_file()} == before
    assert_handoff_discloses_no_labels(
        receipt, ("REVIEW", "APPROVE", "MANUAL", *(row["branch_id"] for row in receipt["branches"]))
    )


class HundredApproveParaphrases:
    """One APPROVE family of 100 paraphrases plus one REVIEW and one MANUAL family."""

    def propose(self, snapshot):
        cases = []

        def add(case_id, family, po, total):
            fields = {"po": po, "total": Decimal(str(total))}
            action, citations = snapshot.expected_action(fields)
            branch = next(r for r in snapshot.branches if r.citation_ref == citations[0])
            cases.append(
                ProposedCase(
                    case_id,
                    family,
                    family,
                    branch.branch_ref,
                    (("po", po), ("total", total)),
                    action,
                    "Generated paraphrase" if family == "approve" else "Generated boundary",
                    citations[0],
                )
            )

        for i in range(100):
            add(f"approve-{i:03d}", "approve", f"generated-po-{i:03d}", 249.99)
        add("review-0", "review", None, 50)
        add("manual-0", "manual", "generated-po-manual", 250)
        return tuple(cases)


def test_hundred_paraphrases_of_one_approve_case_count_one_family(joined):
    _, session, _, _, _ = joined
    session.propose(HundredApproveParaphrases())
    rows = session.review()["cases"]
    assert len(rows) == 102
    for revision, row in enumerate(rows):
        session.adjudicate(
            case_id=row["case_id"],
            target=row["suggestion"],
            reason="Owner checked the cited policy",
            expected_revision=revision,
            operation_ref=f"paraphrase-label-{revision}",
        )
    frozen = session.freeze(
        splits={"approve": "reserved", "review": "reserved", "manual": "reserved"},
        expected_revision=102,
    )
    assert frozen.reserved == 3
    receipt = session_coverage_receipt(session)
    rows = by_action(session, receipt)
    assert counts(rows["APPROVE"]) == (1, 1, 100, "covered")
    assert counts(rows["REVIEW"]) == (1, 1, 1, "covered")
    assert counts(rows["MANUAL"]) == (1, 1, 1, "covered")
    families = {row["family_id"]: row for row in receipt["families"]}
    assert families["approve"]["cases"] == 100 and families["approve"]["counted_as"] == 1
    assert receipt["duplicate_cases_collapsed"] == 99
    assert receipt["totals"]["reserved"] == 3
    value = assert_handoff_discloses_no_labels(receipt, ("APPROVE", "generated-po-"))
    assert (value["reserved_families"], value["reserved_cases"]) == (3, 102)


def test_p1_seven_family_receipt_hands_four_immutable_references_to_scoring(p1_session):
    session = p1_session
    session.propose(SevenInvoiceFamilies())
    for revision, row in enumerate(session.review()["cases"]):
        session.adjudicate(
            case_id=row["case_id"],
            target=P1_OWNER_LABELS[row["case_id"]],
            reason="Policy is silent" if row["case_id"] == "mixed" else "Owner checked passage",
            expected_revision=revision,
            operation_ref="p1-label-" + row["case_id"],
        )
    splits = {
        "family-mixed": "excluded",
        "family-utilities": "development",
        "family-tax": "development",
        "family-subscription": "reserved",
        "family-freight": "reserved",
        "family-supplies": "reserved",
        "family-no-po": "reserved",
    }
    frozen = session.freeze(splits=splits, expected_revision=7)
    receipt = session_coverage_receipt(session)
    assert receipt["totals"] == {
        "proposed": 7,
        "reviewed": 7,
        "resolved": 6,
        "unresolved": 1,
        "development": 2,
        "reserved": 4,
        "excluded": 1,
    }
    rows = by_action(session, receipt)
    assert counts(rows["SOFTWARE"]) == (1, 2, 1, "covered")
    assert rows["SOFTWARE"]["unresolved_cases"] == 1 and rows["SOFTWARE"]["excluded_families"] == 1
    for action in ("FREIGHT", "SUPPLIES", "REVIEW"):
        assert counts(rows[action]) == (1, 1, 1, "covered")
        assert rows[action]["critical"] and rows[action]["verdict"] == "in_scope"
    for action in ("UTILITIES", "TAX"):
        assert counts(rows[action]) == (0, 1, 0, "uncovered")
        assert rows[action]["development_families"] == 1
        assert not rows[action]["critical"] and rows[action]["verdict"] == "out_of_scope"
        assert rows[action]["next_evidence"] == NEXT_EVIDENCE
    claims = receipt["claims"]
    assert not claims["all_branches_verdict"] and claims["held_branches"] == []
    assert sorted(claims["verdict_scope"]) == sorted(
        rows[a]["branch_id"] for a in ("SOFTWARE", "FREIGHT", "SUPPLIES", "REVIEW")
    )
    assert claims["training_credit"] == 0 and claims["live_recurrence"] == 0
    freight = next(row for row in receipt["cases"] if row["case_id"] == "freight")
    assert freight["adjudication"] == "edit" and freight["disposition"] == "reserved_evaluation"
    mixed = next(row for row in receipt["cases"] if row["case_id"] == "mixed")
    assert mixed["disposition"] == "excluded_unresolved"
    assert len(receipt["reserved_family_refs"]) == 4
    assert receipt["reserved_family_refs"] == list(frozen.reserved_family_refs)
    value = assert_handoff_discloses_no_labels(
        receipt,
        (
            "FREIGHT",
            "SOFTWARE",
            "carrier_freight",
            *(row["branch_id"] for row in receipt["branches"]),
        ),
    )
    assert (value["reserved_families"], value["reserved_cases"]) == (4, 4)
    assert (value["unresolved"], value["excluded"]) == (1, 1)
    reopened = StarterCaseSession(session.settings, session.actor)
    assert session_coverage_receipt(reopened) == receipt
    verify_receipt(receipt, *session_replay(reopened))


def test_unresolved_case_inside_a_reserved_family_holds_even_when_counts_agree(core):
    service, _ = core
    freeze_seven(service)
    scope, ref, pack = replay(service, OWNER, WORK)
    pack = json.loads(canonical(pack))
    software = next(c for c in pack["cases"] if c["case_id"] == "software")
    pack["cases"].append(
        software
        | {"case_id": "software-variant", "status": "unresolved", "owner_adjudicated_target": None}
    )
    for key in ("proposed", "reviewed", "unresolved"):
        pack["frozen"][key] += 1
    with pytest.raises(IntakeError, match="starter_coverage_receipt_invalid"):
        build_receipt(scope, ref, pack)
