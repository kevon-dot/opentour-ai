"""Grade the guided Slack route from actual worker attempts and independent store deltas."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from decimal import Decimal

import pytest
from agent_replay_oracle import Action, Effect, Observation, Scenario, grade, status
from opentour_cell.pilot.map_review import MapReviewService
from test_agent_replay_oracle import BoundaryTrace
from test_policy_map_review import actions, branch_values, enter_map, prepare_map
from test_reviewer_workflow import screen
from test_slack_adapter import ACTOR
from test_starter_setup import effects

pytest_plugins = ("test_starter_setup",)


def observed_guided_effects(h):
    root = h.path.parent
    result = dict.fromkeys(Effect, 0)
    with closing(sqlite3.connect((root / "intake.sqlite3").as_uri() + "?mode=ro", uri=True)) as db:
        result[Effect.WORKFLOW] = db.execute("SELECT count(*) FROM intake_workflows").fetchone()[0]
        result[Effect.INTAKE] = db.execute("SELECT count(*) FROM intake_runs").fetchone()[0]
    with closing(
        sqlite3.connect((root / "learning-mandates.sqlite3").as_uri() + "?mode=ro", uri=True)
    ) as db:
        # This generated local policy fixture has no authorized paid connector.
        result[Effect.RESERVATION] = db.execute("SELECT count(*) FROM reservations").fetchone()[0]
    map_path = root / "map-reviews.sqlite3"
    if map_path.exists():
        with closing(sqlite3.connect(map_path.as_uri() + "?mode=ro", uri=True)) as db:
            result[Effect.MAP_VERSION] = db.execute("SELECT count(*) FROM maps").fetchone()[0]
    result[Effect.RESULT] = len(list(root.glob("managed-outputs/run-*/run.json")))
    result[Effect.APPROVAL_POINTER] = len(list(root.glob("approval-ready-*.json")))
    result[Effect.RELEASE] = len(list(root.rglob("release-decision.json")))
    return result


@pytest.fixture
def map_trace(monkeypatch):
    trace = BoundaryTrace()
    trace.observe_fit_reservations(monkeypatch)
    for method, action in (
        ("preview", Action.PREVIEW_MAP),
        ("citation", Action.READ_POLICY_CITATION),
        ("save", Action.SAVE_MAP),
    ):
        original = getattr(MapReviewService, method)

        def observe_service(self, *args, _method=original, _action=action, **kwargs):
            return trace.call(_action, _method, self, *args, **kwargs)

        monkeypatch.setattr(MapReviewService, method, observe_service)
    return trace


def test_guided_citation_and_confirm_effects_are_independently_graded(
    founder, monkeypatch, map_trace
):
    h = founder
    prepare_map(h, monkeypatch)
    map_trace.attempts.clear()
    before = observed_guided_effects(h)
    outside_map = effects(h)
    view = enter_map(h, monkeypatch, ACTOR)
    branch = next(ref for action, ref in actions(screen(h, view)) if action == "ot_map_edit")
    h.action(view, "ot_map_citation", branch)
    assert screen(h, view)["callback_id"] == "ot_map_clause"
    view = enter_map(h, monkeypatch, ACTOR)
    h.action(view, "ot_map_edit", branch)
    h.form(view, branch_values(screen(h, view)))
    terminal = screen(h, view)["callback_id"]
    after = observed_guided_effects(h)
    scenario = Scenario(
        "guided-policy-citation-and-confirm-v1",
        frozenset({"ot_map_review"}),
        (
            (
                Action.PREVIEW_MAP,
                Action.PREVIEW_MAP,
                Action.READ_POLICY_CITATION,
                Action.PREVIEW_MAP,
                Action.PREVIEW_MAP,
                Action.SAVE_MAP,
                Action.PREVIEW_MAP,
            ),
        ),
        frozenset({Action.RESERVE_FIT}),
        {Effect.MAP_VERSION: 1},
        20,
    )
    actual = Observation(terminal, tuple(map_trace.attempts), before, after, True)
    accepted = grade(scenario, actual)
    observed_actions = ", ".join(x.action.value for x in map_trace.attempts)
    assert status([accepted]) == 0, f"{accepted.first_divergence} ({observed_actions})"
    assert accepted.effects[Effect.SPEND] == accepted.effects[Effect.RELEASE] == 0
    assert effects(h) == outside_map
    assert grade(scenario, replace(actual, attempts=actual.attempts[1:])).first_divergence == (
        "action_sequence_mismatch"
    )
    assert (
        grade(scenario, replace(actual, after=before)).first_divergence
        == "effect_count:map_version"
    )


def test_equal_authority_question_leaves_scoring_on_hold(founder, monkeypatch, map_trace):
    h = founder
    policy = b"# Exceptions\nIf total < 250, APPROVE.\nIf total < 250, REVIEW.\n"
    workflow = prepare_map(h, monkeypatch, policy)
    map_trace.attempts.clear()
    before = observed_guided_effects(h)
    outside_map = effects(h)
    view = enter_map(h, monkeypatch, ACTOR)
    for action, ref in actions(screen(h, view)):
        if action == "ot_map_edit":
            h.action(view, action, ref)
            h.form(view, branch_values(screen(h, view)))
    pending = screen(h, view)
    assert len([ref for action, ref in actions(pending) if action == "ot_map_precedence"]) == 1
    assert "Which matching branch takes precedence?" in str(pending)
    actual = Observation(
        pending["callback_id"],
        tuple(map_trace.attempts),
        before,
        observed_guided_effects(h),
        True,
    )
    scenario = Scenario(
        "guided-equal-authority-precedence-v1",
        frozenset({"ot_map_review"}),
        (
            (
                Action.PREVIEW_MAP,
                Action.PREVIEW_MAP,
                Action.SAVE_MAP,
                Action.PREVIEW_MAP,
                Action.PREVIEW_MAP,
                Action.SAVE_MAP,
                Action.PREVIEW_MAP,
            ),
        ),
        frozenset({Action.RESERVE_FIT}),
        {Effect.MAP_VERSION: 2},
        20,
    )
    verdict = grade(scenario, actual)
    assert status([verdict]) == 0, (
        verdict.first_divergence,
        [attempt.action.value for attempt in actual.attempts or ()],
    )
    assert effects(h) == outside_map
    service, principal = h.app._map_service(ACTOR, workflow)
    unresolved = service.preview(principal)
    assert unresolved.confirmed_ref is None
    assert unresolved.expected_action({"total": Decimal("50")})[0] is None
