"""Qualify a source-cited retroactive date through the shipped owner review path."""

from datetime import date
from decimal import Decimal

import pytest
from opentour_cell.pilot.guided_decision_map import guided_draft
from opentour_cell.pilot.map_review import citation_ref
from test_guided_policy import ACTIONS, FIELDS, PURCHASE_POLICY
from test_policy_map_review import actions, branch_values, enter_map, prepare_map
from test_reviewer_workflow import screen
from test_slack_adapter import ACTOR
from test_starter_setup import effects

pytest_plugins = ("test_guided_policy", "test_starter_setup")


def approve_current_map(h, monkeypatch):
    view = enter_map(h, monkeypatch, ACTOR)
    for action, ref in actions(screen(h, view)):
        if action == "ot_map_edit":
            h.action(view, action, ref)
            h.form(view, branch_values(screen(h, view)))


def test_retroactive_policy_is_a_new_confirmed_successor(founder, monkeypatch):
    h = founder
    original_source = PURCHASE_POLICY.replace(
        b"# Ordering\n", b"# Ordering\nEffective from 2026-01-01.\n"
    )
    workflow = prepare_map(h, monkeypatch, original_source)
    approve_current_map(h, monkeypatch)
    original_service, owner = h.app._map_service(ACTOR, workflow)
    original = original_service.preview(owner)
    assert original.confirmed_ref and original.effective_from
    with original_service._db() as db:
        old_receipt = db.execute(
            "SELECT ref,body,seal FROM maps WHERE ref=?", (original.confirmed_ref,)
        ).fetchone()
    fields = {"po": "generated-po", "total": Decimal("225")}
    occurred = date(2026, 4, 15)
    assert original.expected_action(fields, decision_date=occurred)[0] == "APPROVE"

    successor = original_source.replace(b"2026-01-01", b"2026-03-01").replace(b"250", b"200")
    assert prepare_map(h, monkeypatch, successor) == workflow
    worker, owner = h.app._map_service(ACTOR, workflow)
    pending = worker.preview(owner)
    assert pending.effective_from and pending.effective_from.iso_date == "2026-03-01"
    assert pending.confirmed_ref is None and pending.map_revision != original.map_revision
    assert {branch.branch_ref for branch in pending.branches} <= set(pending.changed_branch_refs)
    assert pending.expected_action(fields, decision_date=occurred)[0] is None
    ref = citation_ref(pending.effective_from.citation)
    assert worker.citation(owner, expected_revision=pending.review_revision, citation_ref=ref) == (
        "Effective from 2026-03-01.\n"
    )
    approve_current_map(h, monkeypatch)
    reopened, owner = h.app._map_service(ACTOR, workflow)
    current = reopened.preview(owner)
    assert current.confirmed_ref and current.confirmed_ref != original.confirmed_ref
    assert current.source_revision != original.source_revision
    assert current.expected_action(fields, decision_date=occurred)[0] == "MANUAL"
    assert current.expected_action(fields, decision_date=date(2026, 3, 1))[0] == "MANUAL"
    assert current.expected_action(fields, decision_date=date(2026, 2, 28)) == (None, (ref,))
    assert current.expected_action(fields) == (None, (ref,))
    with reopened._db() as db:
        assert (
            db.execute(
                "SELECT ref,body,seal FROM maps WHERE ref=?", (original.confirmed_ref,)
            ).fetchone()
            == old_receipt
        )
    assert original.expected_action(fields, decision_date=occurred)[0] == "APPROVE"
    assert effects(h) == (0,) * 7


@pytest.mark.parametrize(
    "declaration",
    [
        b"Effective from 2026-02-30.\n",
        b"Effective from 2026-02-01.\nEffective from 2026-03-01.\n",
    ],
)
def test_invalid_or_competing_dates_get_no_guided_action(starter, declaration):
    source, workflow, _, _ = starter(declaration + PURCHASE_POLICY)
    snapshot = source.read(source.authority.principal, workflow)
    draft = guided_draft(snapshot, FIELDS, ACTIONS)
    assert draft.effective_from is None
    action, spans = draft.draft_action(
        {"po": "generated-po", "total": Decimal("225")}, decision_date=date(2026, 4, 15)
    )
    assert action is None
    assert "".join(snapshot.passage(span) for span in spans).encode() == declaration
