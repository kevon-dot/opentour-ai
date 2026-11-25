"""OPE-533. Run with the pilot test directory on PYTHONPATH; examples are generated locally."""
import json
import re
import sqlite3

import pytest

from test_app_home import click, publish
from test_reviewer_workflow import buttons, restart, screen, supplied
from test_slack_adapter import ACTOR, OTHER, Harness


def visible_text(value):
    """Slack-rendered labels and values, excluding action IDs, hidden metadata and payloads."""
    if isinstance(value, dict):
        if value.get("type") in ("mrkdwn", "plain_text"):
            yield value["text"]
        else:
            for child in value.values():
                yield from visible_text(child)
    if isinstance(value, list):
        for child in value:
            yield from visible_text(child)


def rendered(view):
    return " ".join(visible_text(view))


def recorded_effects(h):
    directory = h.path.parent
    with sqlite3.connect(directory / "slack-pilot.sqlite3") as db:
        decisions, proposals, posts = (
            db.execute("select count(*) from " + table).fetchone()[0]
            for table in ("decisions", "proposals", "approval_posts")
        )
    with sqlite3.connect(directory / "training.sqlite3") as db:
        jobs = db.execute("select count(*) from jobs").fetchone()[0]
    return decisions, proposals, posts, jobs


@pytest.fixture
def observed(tmp_path):
    h = Harness(tmp_path)
    dataset = supplied(h, qualified=True)
    h.config["users"][ACTOR]["datasets"] = [dataset]
    h.config["zapier"] = False
    h.save()
    view = h.launch(prompt="status")
    revision = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", revision)
    yield h, view, revision
    h.app.pool.shutdown()


def ask(h, prompt, entry, monkeypatch):
    if entry == "slash":
        return screen(h, h.launch(prompt=prompt))
    home = publish(h, monkeypatch)
    assert "ot_app_prompt" in buttons(home)
    click(h, home, "ot_app_prompt")
    modal = list(h.slack.screens)[-1]
    h.form(modal, {"command": {"command": {"value": prompt}}})
    return screen(h, modal)


@pytest.mark.parametrize("entry", ["home", "slash"])
def test_explicit_regression_command(observed, monkeypatch, entry):
    h, _, revision = observed
    view = ask(h, "ask what got worse?", entry, monkeypatch)
    answer = rendered(view)
    assert recorded_effects(h) == (0, 0, 0, 0) and not h.slack.post
    assert revision in answer and "Paired fixes 1, regressions 0" in answer


@pytest.mark.parametrize("entry", ["home", "slash"])
@pytest.mark.parametrize("prompt", ["What got worse?", "Which decisions became incorrect?"])
def test_natural_regression_question(observed, monkeypatch, entry, prompt):
    h, _, revision = observed
    view = ask(h, prompt, entry, monkeypatch)
    answer = rendered(view).lower()
    assert recorded_effects(h) == (0, 0, 0, 0) and not h.slack.post
    # A single authorized, inspected result is present. Either resolve the result or
    # ask for explicit scope confirmation. Generic command help fails both branches.
    selectable = any(value == revision for value in buttons(view).values())
    scoped_choice = selectable and any(
        s in answer for s in ("choose the exact", "confirm the result", "use this result")
    )
    answered = revision in answer and (
        re.search(r"\b0 (?:of|out of) 3\b|\b0/3\b", answer)
        or ("regressions 0" in answer and "3 comparable" in answer)
    )
    assert scoped_choice or answered, f"Expected a scoped choice or paired 0/3 answer. Saw: {answer}"


@pytest.mark.parametrize("prompt", ["Looks good, ship it.", "Approve this benchmark and retrain.", "👍"])
def test_prose_does_not_vote_after_inspection(observed, monkeypatch, prompt):
    h, _, revision = observed
    ask(h, prompt, "home", monkeypatch)
    assert h.app.journal.decision(revision) == {}
    assert recorded_effects(h) == (0, 0, 0, 0) and not h.slack.post


def test_aggregate_actor_cannot_inherit_inspected_scope(observed):
    h, _, revision = observed
    view = h.launch(actor=OTHER, prompt="ask regressions")
    actual = rendered(screen(h, view))
    assert revision not in actual and "Paired fixes 1" not in actual
    assert not h.app.journal.decision(revision)
    assert recorded_effects(h) == (0, 0, 0, 0)


def test_cancelled_scope_rechecked_between_home_and_submit(observed, monkeypatch):
    h, _, revision = observed
    home = publish(h, monkeypatch)
    click(h, home, "ot_app_prompt")
    view = list(h.slack.screens)[-1]
    h.config["users"][ACTOR]["expires_at"] = 0
    h.save()
    h.form(view, {"command": {"command": {"value": "ask regressions"}}})
    answer = rendered(screen(h, view))
    assert revision not in answer and "Paired fixes" not in answer
    assert recorded_effects(h) == (0, 0, 0, 0)


def test_decline_survives_restart_and_late_positive(observed):
    h, view, revision = observed
    h.action(view, "ot_home", "home")
    assert "ot_changes" in buttons(screen(h, view))
    decline = h.action(view, "ot_changes", revision)
    original = h.app.journal.decision(revision)
    assert original["disposition"] == "changes_requested"
    restart(h)
    h.send("interactive", decline)
    reopened = h.launch()
    assert "Changes requested" in rendered(screen(h, reopened))
    h.action(reopened, "ot_approve", revision)
    assert h.app.journal.decision(revision) == original
    assert recorded_effects(h) == (1, 0, 0, 0) and not h.slack.post

@pytest.mark.parametrize(
    "prompt,concepts",
    [
        ("How much faster is it?", [("timing", "speed", "time measurement"), ("unavailable", "unmeasured", "no comparable")]),
        ("Why didn't it train?", [("training", "fit"), ("not authorized", "evaluation only", "evaluation-only", "training disabled", "no training rights")]),
        ("What did our model see and say?", [("model input", "input capture", "decision-time"), ("output", "prediction", "unavailable")]),
        ("Did it ship?", [("delivery", "handoff", "export"), ("not requested", "unconfirmed", "not sent", "no release", "no delivery")]),
        ("No errors this week?", [("verified", "live evidence"), ("unavailable", "no live", "no verified", "no observed", "no mature")]),
    ],
)
def test_status_questions_give_scoped_recovery(observed, monkeypatch, prompt, concepts):
    """This supplied-evaluation tenant has no live feed, delivery, trace capture or timing pairs."""
    h, _, revision = observed
    output = ask(h, prompt, "home", monkeypatch)
    answer = rendered(output).lower()
    assert recorded_effects(h) == (0, 0, 0, 0) and not h.slack.post
    assert output.get("callback_id") != "ot_help_view", f"Generic command help for {prompt!r}"
    # Accept the scoped fact/limitation or an actual current-result chooser.
    choice = revision in buttons(output).values() and any(
        word in answer for word in ("choose", "confirm", "select")
    )
    assert choice or all(any(term in answer for term in group) for group in concepts), answer
