"""OPE-526 exposure falsification case, supersession ledger, retry and replay.

Fixture kind: generated. Private Slack and worker UI are modelled as two channel labels over the
one guard; no real Slack, UI or provider is exercised, so surface parity with the real
applications remains an external integration gate.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest
from audit_fixtures import baseline_records, commit, inputs_for
from opentour_training.evaluation.benchmark_audit.audit import audit
from opentour_training.evaluation.benchmark_audit.exposure import (
    Channel,
    ExposureGuard,
    ProtectedManifest,
    QueryRequest,
    Surface,
    isolated_families,
    replay_exposure,
    successor_protected_manifest,
)
from opentour_training.evaluation.benchmark_audit.ledger import (
    AuditLedger,
    DependentEffect,
    LedgerError,
)
from opentour_training.evaluation.benchmark_audit.model import (
    ExposureRecord,
    ReleaseEntry,
    Verdict,
)

UNIVERSE = frozenset(f"generated-f{i:02d}" for i in range(1, 7))
DISAGREE = "generated-f03"
DUAL = frozenset({"evaluator", "selector"})


def request(
    query_id: str,
    cell: set[str],
    surface: Surface = Surface.CHART,
    channel: Channel = Channel.WORKER_UI,
    roles: frozenset[str] = DUAL,
) -> QueryRequest:
    return QueryRequest(query_id, surface, channel, frozenset(cell), roles)


def guard() -> ExposureGuard:
    return ExposureGuard(UNIVERSE, frozenset({DISAGREE}))


@pytest.mark.parametrize("surface", list(Surface))
def test_single_family_cell_where_arms_disagree_is_suppressed_on_every_surface(
    surface: Surface,
) -> None:
    g = guard()
    computed: list[object] = []
    decision, value = g.release(
        request("q1", {DISAGREE}, surface), lambda cell: computed.append(cell) or "answer"
    )
    assert not decision.allowed and decision.reason.startswith("suppressed_answer_revealing")
    assert value is None and computed == []
    assert g.exposed_families == frozenset()


def test_complementary_and_differencing_queries_are_suppressed() -> None:
    g = guard()
    assert not g.evaluate(request("comp", set(UNIVERSE - {DISAGREE}))).allowed
    assert g.evaluate(request("wide", {"generated-f01", DISAGREE, "generated-f02"})).allowed
    differencing = g.evaluate(request("narrow", {"generated-f01", "generated-f02"}))
    assert not differencing.allowed
    assert differencing.reason == "suppressed_answer_revealing_arm_disagreement"
    assert DISAGREE in isolated_families(
        [frozenset({"generated-f01", "generated-f02", DISAGREE})]
        + [frozenset({"generated-f01", "generated-f02"})],
        UNIVERSE,
    )


def test_private_slack_and_worker_ui_decide_identically_and_every_decision_is_logged() -> None:
    g = guard()
    results = [
        g.evaluate(request(f"q-{channel.value}", {DISAGREE}, Surface.NL_FILTER, channel))
        for channel in Channel
    ]
    assert [(r.allowed, r.reason) for r in results][0] == [(r.allowed, r.reason) for r in results][
        1
    ]
    allowed = g.evaluate(request("ok", {"generated-f01", "generated-f02", "generated-f04"}))
    assert allowed.allowed
    log = g.log
    assert [e.index for e in log] == [0, 1, 2]
    assert [e.channel for e in log[:2]] == [c.value for c in Channel]
    assert [e.decision for e in log] == ["suppressed", "suppressed", "released"]
    assert all(e.cell_digest and e.query_id for e in log)


def test_non_dual_role_viewers_are_logged_but_not_blocked() -> None:
    g = guard()
    decision = g.evaluate(request("reader", {DISAGREE}, roles=frozenset({"reader"})))
    assert decision.allowed and g.log[-1].decision == "released"


def test_malformed_cell_outside_the_protected_universe_is_denied() -> None:
    g = guard()
    assert not g.evaluate(request("bad", {"generated-unknown"})).allowed
    assert not g.evaluate(request("empty", set())).allowed


def test_authorized_disclosure_retires_families_and_successor_manifest_excludes_them() -> None:
    g = guard()
    manifest = ProtectedManifest(1, UNIVERSE)
    with pytest.raises(ValueError):
        g.authorize_disclosure(request("anon", {DISAGREE}), " ")
    decision = g.authorize_disclosure(request("auth", {DISAGREE}), "generated-owner")
    assert decision.allowed and decision.reason == "authorized_disclosure"
    assert DISAGREE in g.exposed_families
    assert g.log[-1].authorized_by == "generated-owner"
    successor = successor_protected_manifest(manifest, g.exposed_families)
    assert DISAGREE not in successor.family_ids and successor.version == 2
    assert successor.predecessor_digest == manifest.digest()
    assert DISAGREE in manifest.family_ids and manifest.version == 1  # previous as-of untouched
    assert not (successor.family_ids & g.exposed_families)


def test_exposed_family_is_retired_from_the_next_audit_without_erasing_the_prior_one() -> None:
    prior = audit(inputs_for(baseline_records()))
    exposed = frozenset({"generated-c01"})
    after = audit(inputs_for(baseline_records(), exposed_families=exposed))
    assert prior["fixture_agreement"]["verified"] == 12 and prior["verdict"] == "evaluation_allowed"
    assert after["denominator_flow"]["excluded"] == 1
    assert after["verdict"] == Verdict.CLAIM_LIMITED.value  # critical coverage falls to 3/4
    assert after["coverage"]["critical"]["label"] == "3/4"


# ---- Ledger: supersede, fence, reconcile, retry and replay ----


def relabeled_successor(ledger_old: dict[str, Any]) -> dict[str, Any]:
    frozen = baseline_records()
    changed = tuple(
        replace(r, final_label_digest=commit(r.family_id, "generated-changed-action"))
        if r.family_id == "generated-r03"
        else r
        for r in frozen
    )
    # Successor is sealed afresh over the new labels and every family is reconfirmed.
    confirmed = tuple(
        replace(
            r,
            fresh_confirmation=True,
            adjudicator_label_digest=r.final_label_digest,
            independent_label_digest=r.final_label_digest,
        )
        if r.split == "protected"
        else r
        for r in changed
    )
    return audit(inputs_for(confirmed, successor_of=ledger_old["receipt_id"]))


def test_post_freeze_relabel_archives_fences_and_issues_a_successor() -> None:
    frozen = baseline_records()
    old = audit(inputs_for(frozen))
    relabeled = tuple(
        replace(r, final_label_digest=commit(r.family_id, "generated-changed-action"))
        if r.family_id == "generated-r03"
        else r
        for r in frozen
    )
    stale = audit(inputs_for(relabeled, frozen_records=frozen))
    assert stale["verdict"] == Verdict.STALE.value

    ledger = AuditLedger()
    ledger.issue("issue-old", old)
    ledger.log_disclosure("disclose-1", old["receipt_id"], ["generated-f03"])
    ledger.register_effect("fx-a", DependentEffect("effect-consumed", old["receipt_id"], True))
    ledger.register_effect("fx-b", DependentEffect("effect-pending", old["receipt_id"], False))
    assert ledger.promotion_allowed(old["receipt_id"])

    successor = relabeled_successor(old)
    assert successor["successor_of"] == old["receipt_id"]
    assert successor["verdict"] == Verdict.EVALUATION_ALLOWED.value
    assert successor["receipt_id"] != old["receipt_id"]
    ledger.supersede("rel-1", old["receipt_id"], successor)

    assert ledger.status[old["receipt_id"]] == "archived_superseded"
    assert ledger.receipts[old["receipt_id"]] == old  # old as-of verdict preserved verbatim
    assert not ledger.promotion_allowed(old["receipt_id"])
    assert ledger.promotion_allowed(successor["receipt_id"])
    assert not ledger.effect_runnable("effect-pending")  # unconsumed effect fenced
    assert ledger.effect_runnable("effect-consumed")  # consumed effect is reconciled, not fenced
    assert ledger.reconciliation[old["receipt_id"]] == [
        "disclosure:generated-f03",
        "effect-consumed",
    ]
    ledger.register_effect("fx-late", DependentEffect("effect-late", old["receipt_id"], False))
    assert not ledger.effect_runnable("effect-late")


def test_successor_without_fresh_independent_confirmation_is_insufficient() -> None:
    old = audit(inputs_for(baseline_records()))
    receipt = audit(inputs_for(baseline_records(), successor_of=old["receipt_id"]))
    assert receipt["verdict"] == Verdict.INSUFFICIENT_EVIDENCE.value
    assert receipt["reasons"][0]["code"] == "fresh_independent_confirmation_missing"


def test_retry_is_idempotent_and_restart_replays_to_identical_state() -> None:
    old = audit(inputs_for(baseline_records()))
    successor = relabeled_successor(old)
    ledger = AuditLedger()
    assert ledger.issue("k1", old) is True
    assert ledger.issue("k1", old) is False  # retried delivery
    ledger.register_effect("fx", DependentEffect("e1", old["receipt_id"], False))
    ledger.supersede("s1", old["receipt_id"], successor)
    assert ledger.supersede("s1", old["receipt_id"], successor) is False
    assert len(ledger.events) == 4
    restarted = AuditLedger.replay(ledger.events)
    assert restarted.state_digest() == ledger.state_digest()
    assert restarted.fenced == ledger.fenced and not restarted.promotion_allowed(old["receipt_id"])
    # Replaying the same events again on the restarted ledger changes nothing.
    for event in ledger.events:
        assert restarted._apply(event) is False


def test_unknown_receipt_cannot_be_superseded_and_held_verdicts_never_promote() -> None:
    ledger = AuditLedger()
    with pytest.raises(LedgerError):
        ledger.supersede("s", "missing", {"receipt_id": "x", "verdict": "invalid"})
    held = audit(inputs_for(baseline_records(), consumed_features=("refund_settled_at",)))
    ledger.issue("held", held)
    assert not ledger.promotion_allowed(held["receipt_id"])


def test_non_nested_pairwise_cells_that_solve_one_family_are_suppressed() -> None:
    """a+b, b+c and a+c together reveal each family by linear algebra; no cell is a subset."""
    g = guard()
    first = g.evaluate(request("ab", {"generated-f01", "generated-f02"}))
    second = g.evaluate(request("bc", {"generated-f02", DISAGREE}))
    third = g.evaluate(request("ac", {"generated-f01", DISAGREE}))
    assert first.allowed and second.allowed
    assert not third.allowed and third.reason == "suppressed_answer_revealing_arm_disagreement"
    assert [e.decision for e in g.log] == ["released", "released", "suppressed"]
    three = [
        frozenset({"generated-f01", "generated-f02"}),
        frozenset({"generated-f02", DISAGREE}),
        frozenset({"generated-f01", DISAGREE}),
    ]
    assert isolated_families(three[:2], UNIVERSE) == frozenset()
    assert DISAGREE in isolated_families(three, UNIVERSE)


def test_late_consumed_effect_and_disclosure_after_supersession_are_reconciled() -> None:
    old = audit(inputs_for(baseline_records()))
    successor = relabeled_successor(old)
    ledger = AuditLedger()
    ledger.issue("k1", old)
    ledger.supersede("s1", old["receipt_id"], successor)
    ledger.register_effect("fx-consumed", DependentEffect("e-late", old["receipt_id"], True))
    ledger.log_disclosure("d1", old["receipt_id"], ["generated-f03"])
    assert ledger.reconciliation[old["receipt_id"]] == ["disclosure:generated-f03", "e-late"]
    assert AuditLedger.replay(ledger.events).state_digest() == ledger.state_digest()


# ---- Fail-open fix: any selector or evaluator role, judged by the cell released ----

SELECTOR = frozenset({"selector"})
EVALUATOR = frozenset({"evaluator"})
READER = frozenset({"reader"})
PROTECTED_ROLE_SETS = {
    "selector_only": SELECTOR,
    "evaluator_only": EVALUATOR,
    "selector_with_reader": frozenset({"selector", "reader"}),
    "dual": DUAL,
}


@pytest.mark.parametrize("name", sorted(PROTECTED_ROLE_SETS))
def test_any_selector_or_evaluator_role_is_denied_single_family_and_complement_cells(
    name: str,
) -> None:
    roles = PROTECTED_ROLE_SETS[name]
    g = guard()
    computed: list[object] = []
    decision, value = g.release(
        request("one", {DISAGREE}, roles=roles), lambda cell: computed.append(cell) or "answer"
    )
    assert not decision.allowed and value is None and computed == []
    assert decision.reason == "suppressed_answer_revealing_arm_disagreement"
    single = g.evaluate(request("other", {"generated-f01"}, roles=roles))
    assert not single.allowed and single.reason == "suppressed_single_family_isolation"
    complement = g.evaluate(request("comp", set(UNIVERSE - {DISAGREE}), roles=roles))
    assert not complement.allowed
    assert [e.decision for e in g.log] == ["suppressed"] * 3
    assert g.record().releases == () and replay_exposure(g.record()) == (frozenset(), 0)


@pytest.mark.parametrize("roles", [SELECTOR, EVALUATOR], ids=["selector", "evaluator"])
def test_differencing_query_pair_is_suppressed_for_a_single_protected_role(
    roles: frozenset[str],
) -> None:
    g = guard()
    wide = g.evaluate(request("wide", {"generated-f01", "generated-f02", DISAGREE}, roles=roles))
    narrow = g.evaluate(request("narrow", {"generated-f01", "generated-f02"}, roles=roles))
    assert wide.allowed and not narrow.allowed
    assert narrow.reason == "suppressed_answer_revealing_arm_disagreement"
    assert replay_exposure(g.record()) == (frozenset(), 0)


def test_differencing_pair_already_released_elsewhere_is_still_suppressed_for_a_selector() -> None:
    """The pair isolates the family before the selector asks; each cell still reveals it."""
    g = guard()
    wide = {"generated-f01", "generated-f02", DISAGREE}
    narrow = {"generated-f01", "generated-f02"}
    assert g.evaluate(request("r-wide", wide, roles=READER)).allowed
    assert g.evaluate(request("r-narrow", narrow, roles=READER)).allowed
    assert DISAGREE in isolated_families([frozenset(wide), frozenset(narrow)], UNIVERSE)
    for query_id, cell in (("s-narrow", narrow), ("s-wide", wide)):
        decision = g.evaluate(request(query_id, cell, roles=SELECTOR))
        assert not decision.allowed, query_id
        assert decision.reason == "suppressed_answer_revealing_arm_disagreement"


def test_repeat_release_of_an_already_isolated_cell_is_suppressed_or_logged() -> None:
    g = guard()
    first = g.authorize_disclosure(request("auth-1", {DISAGREE}), "generated-owner")
    assert first.allowed and g.exposed_families == {DISAGREE}
    for query_id, roles in (("again-dual", DUAL), ("again-selector", SELECTOR)):
        repeat = g.evaluate(request(query_id, {DISAGREE}, roles=roles))
        assert not repeat.allowed, query_id
        assert repeat.reason == "suppressed_answer_revealing_arm_disagreement"
    again = g.authorize_disclosure(request("auth-2", {DISAGREE}), "generated-owner")
    assert again.isolated_digest == first.isolated_digest  # the repeat is recorded as exposure
    assert [e.decision for e in g.log] == [
        "authorized_disclosure",
        "suppressed",
        "suppressed",
        "authorized_disclosure",
    ]
    assert replay_exposure(g.record()) == (frozenset({DISAGREE}), 0)


def test_cell_isolated_by_an_unprotected_release_is_still_suppressed_for_a_selector() -> None:
    g = guard()
    reader = g.evaluate(request("reader", {"generated-f01"}, roles=READER))
    assert reader.allowed and reader.reason == "released_isolating_cell_to_unprotected_role"
    assert reader.isolated_digest is not None
    assert g.log[-1].reason == "released_isolating_cell_to_unprotected_role"
    for roles in (SELECTOR, DUAL):
        repeat = g.evaluate(request("again", {"generated-f01"}, roles=roles))
        assert not repeat.allowed and repeat.reason == "suppressed_single_family_isolation"
        # The complement is a different cell that isolates the same family on its own.
        complement = g.evaluate(request("comp", set(UNIVERSE - {"generated-f01"}), roles=roles))
        assert not complement.allowed
    assert [e.protected_role for e in g.record().releases] == [False]


def test_malformed_cell_is_denied_for_every_role_and_never_enters_the_history() -> None:
    g = guard()
    for roles in (READER, SELECTOR):
        for query_id, cell in (("unknown", {"generated-unknown"}), ("empty", set())):
            decision = g.evaluate(request(query_id, cell, roles=roles))
            assert not decision.allowed and decision.reason == "suppressed_malformed_cell"
    assert g.record().releases == ()


def test_protected_roles_still_receive_cells_that_reveal_no_family() -> None:
    g = guard()
    ab, bc = {"generated-f01", "generated-f02"}, {"generated-f02", DISAGREE}
    for query_id, cell in (("ab", ab), ("bc", bc), ("ab-again", ab), ("total", set(UNIVERSE))):
        decision = g.evaluate(request(query_id, cell, roles=SELECTOR))
        assert decision.allowed and decision.reason == "released", query_id
    assert [e.protected_role for e in g.record().releases] == [True] * 4
    assert replay_exposure(g.record()) == (frozenset(), 0)
    h = guard()
    assert h.authorize_disclosure(request("auth", {"generated-f05"}), "generated-owner").allowed
    wider = h.evaluate(request("wider", {"generated-f05", "generated-f04", "generated-f06"}))
    assert wider.allowed  # f05 is isolated without this cell; the cell reveals nothing new
    assert replay_exposure(h.record()) == (frozenset({"generated-f05"}), 0)


# ---- Caller-asserted exposure fields cannot override the guard's own history ----

BASELINE_PROTECTED = frozenset(r.family_id for r in baseline_records() if r.split == "protected")


def test_caller_asserting_no_exposure_cannot_override_the_release_history() -> None:
    g = ExposureGuard(BASELINE_PROTECTED, frozenset())
    g.authorize_disclosure(request("auth", {"generated-c01"}), "generated-owner")
    asserted = audit(inputs_for(baseline_records(), exposed_families=frozenset()))
    assert asserted["verdict"] == Verdict.EVALUATION_ALLOWED.value
    assert asserted["provenance"]["exposed_families"] == "caller_asserted"
    derived = audit(
        inputs_for(
            baseline_records(),
            exposed_families=frozenset(),
            unlogged_disclosures=0,
            exposure_record=g.record(),
        )
    )
    assert derived["verdict"] == Verdict.CLAIM_LIMITED.value
    assert derived["coverage"]["critical"]["label"] == "3/4"
    assert derived["excluded_reasons"] == ["exposed_answer_key_retired"]
    assert derived["provenance"]["exposed_families"] == "worker_derived_from_exposure_log"


def test_caller_asserting_zero_unlogged_disclosures_cannot_override_the_release_history() -> None:
    # The history the pre-fix guard produced: a selector-only release of a single-family cell.
    failopen = ExposureRecord(
        BASELINE_PROTECTED, (ReleaseEntry(frozenset({"generated-r02"}), True, None),)
    )
    assert replay_exposure(failopen) == (frozenset({"generated-r02"}), 1)
    receipt = audit(
        inputs_for(baseline_records(), unlogged_disclosures=0, exposure_record=failopen)
    )
    assert receipt["verdict"] == Verdict.INVALID.value
    assert "unlogged_case_level_disclosure" in {r["code"] for r in receipt["reasons"]}
    assert receipt["denominator_flow"]["excluded"] == 1
    assert receipt["claims"]["broad_improvement_alert_permitted"] is False
    clean = ExposureGuard(BASELINE_PROTECTED, frozenset())
    assert not clean.evaluate(request("s", {"generated-r02"}, roles=SELECTOR)).allowed
    assert clean.evaluate(request("ok", {"generated-r01", "generated-r02"}, roles=SELECTOR)).allowed
    allowed = audit(inputs_for(baseline_records(), exposure_record=clean.record()))
    assert allowed["verdict"] == Verdict.EVALUATION_ALLOWED.value
    assert allowed["provenance"]["unlogged_disclosures"] == "worker_derived_from_exposure_log"


def test_exposure_log_that_does_not_cover_every_protected_family_holds() -> None:
    partial = ExposureGuard(BASELINE_PROTECTED - {"generated-c04"}, frozenset())
    receipt = audit(inputs_for(baseline_records(), exposure_record=partial.record()))
    assert receipt["verdict"] == Verdict.INSUFFICIENT_EVIDENCE.value
    assert [r["code"] for r in receipt["reasons"]] == ["exposure_log_scope_mismatch"]
    assert receipt["provenance"]["exposure_log_covers_protected_families"] is False
    assert receipt["next_evidence_request"]
