"""Private inspection of frozen starter evaluation receipts from Slack Home."""

import json

from test_app_home import click, publish
from test_intake_mcp import _write_private
from test_policy_map_review import actions
from test_reviewer_workflow import screen, text
from test_slack_adapter import ACTOR, OTHER
from test_starter_setup import effects

pytest_plugins = ("test_starter_evaluation_authority",)


def open_summary(h, monkeypatch):
    home = publish(h, monkeypatch, ACTOR)
    click(h, home, "ot_app_starter_score", ACTOR)
    view = list(h.slack.screens)[-1]
    assert screen(h, view)["callback_id"] == "ot_starter_score"
    return view


def test_retained_owner_score_opens_after_inference_withdrawal_without_execution(
    scorer, monkeypatch
):
    from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
    from test_model_registry import write

    authorized, evaluator, registration, path = scorer
    h, _, _, _, authority, p, *_ = authorized
    result = evaluator.evaluate(authority.actor, authority.work(p.execution_ref), p.execution_ref)
    assert result["state"] == "scored"
    registration["grants"] = []
    write(path, registration)
    _write_private(h.path.parent / "starter-evaluation-withdrawn-evaluation-a.json", {})

    def forbidden(*args, **kwargs):
        raise AssertionError("Home attempted evaluation")

    monkeypatch.setattr(StarterEvaluationSession, "run", forbidden)
    db = h.path.parent / "starter-evaluations.sqlite3"
    before = db.read_bytes()
    view = open_summary(h, monkeypatch)
    visible = text(h, view)
    assert "Expert-reviewed starter benchmark · generated examples" in visible
    assert "Exact action agreement · 4/5" in visible and "Exact action agreement · 5/5" in visible
    assert (
        "Wrong automatic decisions · 1/3" in visible
        and "Wrong automatic decisions · 0/3" in visible
    )
    assert (
        "Fixed 1 · Regressed 0" in visible
        and p.execution_ref in visible
        and p.freeze_revision in visible
    )
    assert "case-0" not in visible and not actions(screen(h, view))
    assert "ot_app_starter_score" not in dict(actions(publish(h, monkeypatch, OTHER)))
    assert (
        db.read_bytes() == before
        and len(evaluator.baseline.calls) == len(evaluator.comparator.calls) == 1
    )
    assert effects(h) == (0,) * 7


def test_unscored_home_and_late_owner_revocation_never_expose_a_score(authorized, monkeypatch):
    from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession

    h, _, _, _, _, p, *_ = authorized
    with monkeypatch.context() as patch:

        def forbidden(*args, **kwargs):
            raise AssertionError("Opening Home read case data or score bodies")

        patch.setattr(StarterEvaluationSession, "summary", forbidden)
        home = publish(h, patch, ACTOR)
        assert "ot_app_starter_score" in dict(actions(home))
    view = open_summary(h, monkeypatch)
    assert "Frozen, unscored" in text(h, view) and "Exact action agreement" not in text(h, view)
    real = h.app.views.starter_score_readout

    def withdrawn(*args, **kwargs):
        layout = real(*args, **kwargs)
        _write_private(h.path.parent / "starter-evaluation-withdrawn-inspection-a.json", {})
        return layout

    monkeypatch.setattr(h.app.views, "starter_score_readout", withdrawn)
    home = publish(h, monkeypatch, ACTOR)
    click(h, home, "ot_app_starter_score", ACTOR)
    denied = list(h.slack.screens)[-1]
    assert p.freeze_revision not in text(
        h, denied
    ) and "Expert-reviewed starter benchmark" not in text(h, denied)
    assert not (h.path.parent / "starter-evaluations.sqlite3").exists() and effects(h) == (0,) * 7


def test_explicit_choice_rechecks_two_separately_selected_inspection_grants(
    authorized, monkeypatch
):
    from test_starter_evaluation_authority import seal

    h, _, _, _, _, p, _, inspection, *_ = authorized
    directory = h.path.parent
    second = inspection | {
        "grant_ref": "inspection-b",
        "permission": inspection["permission"]
        | {"execution_ref": "execution-b", "revision": "evaluation-b"},
    }
    seal(directory, "inspection-b.json", second)
    path = directory / "starter-evaluation-selection.json"
    selection = json.loads(path.read_text())
    selection["inspections"]["execution-b"] = "inspection-b.json"
    _write_private(path, selection)
    home = publish(h, monkeypatch, ACTOR)
    click(h, home, "ot_app_starter_score", ACTOR)
    view = list(h.slack.screens)[-1]
    layout = screen(h, view)
    assert layout["callback_id"] == "ot_starter_scores" and p.freeze_revision not in text(h, view)
    assert len(layout["blocks"]) == 2
    item = next(b for b in layout["blocks"] if "execution-b" in b["text"]["text"])
    choice = item["accessory"]
    h.action(view, choice["action_id"], choice["value"])
    assert "Evaluation execution-b" in text(h, view) and "Frozen, unscored" in text(h, view)
    assert effects(h) == (0,) * 7
