"""Generated upstream grants plus actual tenant-and-epoch scoped benchmark admissions."""

from contextlib import contextmanager
from dataclasses import replace

import pytest
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope, WorkflowBenchmarks
from opentour_cell.pilot.workflow_index import (
    CurrentIndexAuthority,
    IndexAuthority,
    IndexScope,
    ProposalAccess,
    WorkflowIndex,
    WorkflowLabel,
    WorkflowVisibility,
)
from test_workflow_benchmarks import ACTOR, HISTORY, STARTER

pytest_plugins = ("test_workflow_benchmarks",)


class GeneratedGrants(IndexAuthority):
    def __init__(self, current):
        self.current = current
        self.after_read = lambda: None

    @contextmanager
    def guard(self, actor):
        before = self.current
        if before is None or before.actor != actor:
            raise IntakeError("workflow_index_unavailable")
        yield before
        self.after_read()
        if before != self.current:
            raise IntakeError("workflow_index_unavailable")


@pytest.fixture
def index(prepared):
    intake, authority = prepared
    workflows = [
        intake.get_run(ACTOR, intake.submit_intake(ACTOR, v).run_ref).workflow_ref
        for v in (STARTER, HISTORY)
    ]
    scopes = [BenchmarkScope(ACTOR.tenant, authority.epoch, w) for w in workflows]
    labels = tuple(
        WorkflowLabel(s, "owner", "label-v1", "Purchasing", ("payments",)) for s in scopes
    )
    rows = tuple(
        ProposalAccess(
            s,
            ACTOR.actor,
            "grant-v1",
            f"proposal-{i}",
            "frozen-v1",
            "pending",
            100,
            deadline,
            400,
            "open_review",
            300,
        )
        for i, (s, deadline) in enumerate(zip(scopes, (210, 200), strict=True))
    )
    grants = GeneratedGrants(IndexScope(ACTOR, authority.epoch, "member-v1", 400, labels, rows))
    return WorkflowIndex(intake.benchmarks, grants, clock=lambda: 150), grants, labels, rows


def test_equal_labels_earliest_deadline_visible_pagination_and_restart(index):
    api, grants, labels, rows = index
    db = api.benchmarks.path
    before = db.read_bytes()
    first = api.page(ACTOR, size=1)
    assert first.rows[0].label == "Purchasing" and first.rows[0].group == "needs_you"
    assert first.rows[0].workflow_ref == labels[1].scope.workflow_ref
    assert first.next_cursor
    hidden = replace(rows[0], actor_ref="foreign", proposal_ref="hidden")
    grants.current = replace(grants.current, proposals=(*rows, hidden))
    assert api.page(ACTOR, size=1) == first
    fresh = WorkflowIndex(WorkflowBenchmarks(db.parent), grants, clock=lambda: 150)
    second = fresh.page(ACTOR, size=1, cursor=first.next_cursor)
    assert len(second.rows) == 1 and second.next_cursor is None
    assert second.rows[0].workflow_ref != first.rows[0].workflow_ref
    assert fresh.select(ACTOR, first.rows[0].selection) == first.rows[0]
    assert before == db.read_bytes()


def test_owner_rename_and_action_replacement_fence_old_handles(index):
    api, grants, labels, rows = index
    page = api.page(ACTOR, size=1)
    pin = page.rows[0].selection
    assert pin
    grants.current = replace(
        grants.current, labels=(labels[0], replace(labels[1], label="Orders", revision="label-v2"))
    )
    with pytest.raises(IntakeError):
        api.select(ACTOR, pin)
    with pytest.raises(IntakeError):
        api.page(ACTOR, size=1, cursor=page.next_cursor)
    new = api.page(ACTOR).rows[0]
    assert new.workflow_ref == page.rows[0].workflow_ref and new.label == "Orders"
    grants.current = replace(
        grants.current,
        proposals=(rows[0], replace(rows[1], action="open_status", grant_revision="grant-v2")),
    )
    with pytest.raises(IntakeError):
        api.select(ACTOR, new.selection)
    waiting = api.page(ACTOR).rows[-1]
    assert waiting.group == "waiting" and waiting.action == "open_status"


def test_missing_role_empty_visibility_foreign_epoch_and_revocation(index):
    api, grants, labels, rows = index
    valid = grants.current
    for scope in (
        replace(labels[0].scope, tenant="other"),
        replace(labels[0].scope, engagement_epoch="old"),
        replace(labels[0].scope, workflow_ref="unknown"),
    ):
        grants.current = replace(
            valid,
            labels=(replace(labels[0], scope=scope),),
            proposals=(replace(rows[0], scope=scope),),
        )
        assert api.page(ACTOR).rows == () and api.page(ACTOR).next_cursor is None
    grants.current = replace(valid, proposals=())
    assert api.page(ACTOR).rows == ()
    grants.current = None
    with pytest.raises(IntakeError):
        api.page(ACTOR)
    grants.current = valid
    with pytest.raises(IntakeError):
        api.page(replace(ACTOR, tenant="other"))
    pin = api.page(ACTOR).rows[0].selection
    grants.after_read = lambda: setattr(grants, "current", None)
    with pytest.raises(IntakeError):
        api.select(ACTOR, pin)


def test_expiry_recent_and_action_visibility_are_independent(index):
    api, grants, _, rows = index
    old = api.page(ACTOR).rows[0].selection
    grants.current = replace(
        grants.current,
        proposals=(
            replace(rows[0], state="approved", action="open_receipt"),
            replace(rows[1], deadline=150),
        ),
    )
    recent = api.page(ACTOR).rows
    assert all(r.group == "recent" for r in recent)
    assert {(r.state, r.action) for r in recent} == {
        ("approved", "open_receipt"),
        ("expired", None),
    }
    with pytest.raises(IntakeError):
        api.select(ACTOR, old)
    grants.current = replace(
        grants.current,
        proposals=(replace(rows[0], action_until=150), replace(rows[1], visible_until=149)),
    )
    assert [(r.group, r.action) for r in api.page(ACTOR).rows] == [("waiting", None)]


@pytest.mark.parametrize("operation", ["page", "select"])
@pytest.mark.parametrize("deadline", ["visible_until", "deadline", "action_until", "membership"])
def test_clock_crossing_guard_exit_denies_old_projection(index, operation, deadline):
    api, grants, _, rows = index
    if deadline == "membership":
        grants.current = replace(grants.current, valid_until=175)
    else:
        grants.current = replace(grants.current, proposals=(replace(rows[0], **{deadline: 175}),))
    now = [150]
    api.clock = lambda: now[0]
    pin = api.page(ACTOR).rows[0].selection
    grants.after_read = lambda: now.__setitem__(0, 175)
    with pytest.raises(IntakeError):
        if operation == "page":
            api.page(ACTOR)
        else:
            api.select(ACTOR, pin)


@pytest.mark.parametrize("bad", [False, float("nan"), float("inf"), "150", 0, -1, 2**63])
@pytest.mark.parametrize(
    "field", ["valid_until", "visible_until", "deadline", "action_until", "recorded_at"]
)
def test_malformed_authority_timestamp_never_discloses(index, field, bad):
    api, grants, _, rows = index
    pin = api.page(ACTOR).rows[0].selection
    if field == "valid_until":
        grants.current = replace(grants.current, valid_until=bad)
    else:
        grants.current = replace(grants.current, proposals=(replace(rows[1], **{field: bad}),))
    with pytest.raises(IntakeError):
        api.page(ACTOR)
    with pytest.raises(IntakeError):
        api.select(ACTOR, pin)


def test_concurrent_proposals_on_one_workflow_reopen_independently(index):
    api, grants, labels, rows = index
    first = rows[0]
    second = replace(
        first, proposal_ref="proposal-next", proposal_revision="frozen-v2", deadline=190
    )
    grants.current = replace(grants.current, proposals=(first, second))
    earlier, later = api.page(ACTOR).rows
    assert earlier.workflow_ref == later.workflow_ref == labels[0].scope.workflow_ref
    assert (earlier.proposal_ref, later.proposal_ref) == (second.proposal_ref, first.proposal_ref)
    assert earlier.selection != later.selection
    restarted = WorkflowIndex(
        WorkflowBenchmarks(api.benchmarks.path.parent), grants, clock=lambda: 150
    )
    assert restarted.select(ACTOR, later.selection) == later
    grants.current = replace(grants.current, proposals=(first,))
    with pytest.raises(IntakeError, match="workflow_index_unavailable"):
        restarted.select(ACTOR, earlier.selection)
    assert restarted.select(ACTOR, later.selection) == later
    grants.current = replace(grants.current, proposals=())
    assert restarted.page(ACTOR).rows == ()
    with pytest.raises(IntakeError, match="workflow_index_unavailable"):
        restarted.select(ACTOR, later.selection)


def test_current_authority_reloads_authorization_on_page_and_selection(index):
    api, grants, labels, _ = index
    current = grants.current
    protected = WorkflowIndex(
        api.benchmarks, CurrentIndexAuthority(lambda _: grants.current), clock=lambda: 150
    )
    page = protected.page(ACTOR, size=1)
    assert protected.select(ACTOR, page.rows[0].selection) == page.rows[0]
    for replacement in (
        None,
        replace(current, membership_revision="member-v2"),
        replace(current, labels=(labels[0], replace(labels[1], revision="label-v2"))),
    ):
        for read in (protected.page, lambda actor: protected.select(actor, page.rows[0].selection)):
            snapshots = iter((current, replacement))
            protected.authority = CurrentIndexAuthority(
                lambda _, snapshots=snapshots: next(snapshots)
            )
            with pytest.raises(IntakeError, match="workflow_index_unavailable"):
                read(ACTOR)


def test_list_without_visible_proposals_requires_a_separate_current_workflow_grant(index):
    api, grants, labels, proposals = index
    grants.current = replace(grants.current, proposals=(proposals[0],))
    assert [row.workflow_ref for row in api.page(ACTOR).rows] == [labels[0].scope.workflow_ref]
    visibility = WorkflowVisibility(labels[1].scope, ACTOR.actor, 200)
    grants.current = replace(
        grants.current,
        workflows=(replace(visibility, actor_ref="other"), replace(visibility, visible_until=150)),
    )
    assert len(api.page(ACTOR).rows) == 1
    grants.current = replace(grants.current, workflows=(visibility,))
    first = api.page(ACTOR, size=1)
    directory = api.page(ACTOR, size=1, cursor=first.next_cursor)
    (row,) = directory.rows
    assert row.workflow_ref == labels[1].scope.workflow_ref
    assert row.group == "waiting" and row.state == "no_visible_proposal"
    assert row.proposal_ref is row.proposal_revision is row.action is row.selection is None
    grants.current = replace(grants.current, workflows=())
    with pytest.raises(IntakeError, match="workflow_index_unavailable"):
        api.page(ACTOR, size=1, cursor=first.next_cursor)
