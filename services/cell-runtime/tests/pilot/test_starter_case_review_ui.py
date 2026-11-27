"""Private owner case review through installed Slack actions."""

from test_app_home import click, publish
from test_policy_map_review import actions
from test_reviewer_workflow import screen, text
from test_slack_adapter import ACTOR, OTHER
from test_starter_setup import effects

pytest_plugins = ("test_starter_map_confirmation",)


def choose(h, view, action):
    emitted = dict(actions(screen(h, view)))
    h.action(view, action, emitted[action])


def select(value):
    return {"type": "static_select", "selected_option": {"value": value}}


def open_cases(h, monkeypatch):
    home = publish(h, monkeypatch, ACTOR)
    click(h, home, "ot_app_cases", ACTOR)
    view = list(h.slack.screens)[-1]
    assert screen(h, view)["callback_id"] == "ot_case_entry"
    return view


def test_guided_case_owner_adjudicates_then_freezes_whole_families(joined, monkeypatch):
    h, session, _, _, _ = joined
    assert "ot_app_cases" not in dict(actions(publish(h, monkeypatch, OTHER)))
    view = open_cases(h, monkeypatch)
    assert not session.core.path.exists()
    choose(h, view, "ot_case_generate")
    pack = session.review()
    assert len(pack["cases"]) == 6 and pack["revision"] == 0
    assert all(c["owner_adjudicated_target"] is None for c in pack["cases"])
    for index, case in enumerate(pack["cases"]):
        assert case["case_id"] in text(h, view) and "Suggested action" in text(h, view)
        choose(h, view, "ot_case_edit")
        options = pack["authority"]["allowed_actions"]
        target = case["suggestion"]
        h.form(
            view,
            {
                "target": {"target": select(str(options.index(target)))},
                "reason": {
                    "reason": {
                        "type": "plain_text_input",
                        "value": "Owner checked the generated input.",
                    }
                },
            },
        )
        assert session.review()["cases"][index]["owner_adjudicated_target"] == target
        if index + 1 < len(pack["cases"]):
            choose(h, view, "ot_case_page_next")
    choose(h, view, "ot_case_splits")
    families = sorted({c["family_id"] for c in pack["cases"]})
    assert len(families) == 3
    h.form(
        view,
        {
            f"family_{i}": {f"family_{i}": select(("development", "reserved", "excluded")[i])}
            for i, _ in enumerate(families)
        },
    )
    assert session.review()["frozen"] is None
    choose(h, view, "ot_case_commit")
    assert session.review()["frozen"]["state"] == "frozen_unscored"
    assert session.review()["frozen"]["excluded"] == 2
    assert "2 excluded cases" in text(h, view)
    assert "Independent families · Development 1 · Reserved 1 · Excluded 1" in text(h, view)
    assert "Frozen, unscored" in text(h, view)
    assert effects(h) == (0,) * 7


def test_case_review_rechecks_grant_at_disclosure_and_stale_submit_cannot_write(
    joined, monkeypatch
):
    from opentour_cell.pilot.starter_case_authoring import guided_case_provider
    from test_intake_mcp import _write_private

    h, session, cap, _, _ = joined
    session.propose(guided_case_provider())
    view = open_cases(h, monkeypatch)
    choose(h, view, "ot_case_review")
    choose(h, view, "ot_case_citation")
    assert "If " in text(h, view)
    choose(h, view, "ot_case_back")
    choose(h, view, "ot_case_edit")
    original = session.review()
    case = original["cases"][0]
    session.adjudicate(
        case_id=case["case_id"],
        target=None,
        reason="Owner needs independent evidence",
        expected_revision=0,
        operation_ref="earlier-owner-adjudication",
    )
    saved = session.review()
    h.form(
        view,
        {
            "target": {"target": select("0")},
            "reason": {"reason": {"type": "plain_text_input", "value": "stale attempt"}},
        },
    )
    assert session.review() == saved and saved["cases"][0]["status"] == "unresolved"
    view = open_cases(h, monkeypatch)
    render = h.app.views.starter_case_detail

    def revoke(*args, **kwargs):
        layout = render(*args, **kwargs)
        _write_private(h.path.parent / f"starter-case-withdrawn-{cap['grant_ref']}.json", {})
        return layout

    monkeypatch.setattr(h.app.views, "starter_case_detail", revoke)
    choose(h, view, "ot_case_review")
    assert case["case_id"] not in text(h, view) and "Owner needs independent evidence" not in text(
        h, view
    )
    assert "ot_app_cases" not in dict(actions(publish(h, monkeypatch, ACTOR)))
    assert effects(h) == (0,) * 7


def test_review_purpose_alone_cannot_generate_or_route_forged_action(joined, monkeypatch):
    from test_starter_map_confirmation import sign_case

    h, session, cap, _, _ = joined
    sign_case(h.path.parent, cap | {"owner_purposes": ["review"]})
    view = open_cases(h, monkeypatch)
    assert "ot_case_generate" not in dict(actions(screen(h, view)))
    h.action(view, "ot_case_generate", "generate")
    assert not session.core.path.exists()
    assert effects(h) == (0,) * 7


def test_wrong_suggestion_is_editable_and_unresolved_family_stays_outside_reserved(
    joined, monkeypatch
):
    from test_starter_map_confirmation import FiveFamilies

    h, session, _, _, _ = joined
    session.propose(FiveFamilies())
    view = open_cases(h, monkeypatch)
    choose(h, view, "ot_case_review")
    pack = session.review()
    choices = pack["authority"]["allowed_actions"]
    assert pack["cases"][2]["suggestion"] == "MANUAL"
    for index, label in enumerate((None, "REVIEW", "APPROVE", "MANUAL", "MANUAL")):
        choose(h, view, "ot_case_edit")
        value = "unresolved" if label is None else str(choices.index(label))
        h.form(
            view,
            {
                "target": {"target": select(value)},
                "reason": {
                    "reason": {
                        "type": "plain_text_input",
                        "value": "Resolved against the cited rule."
                        if label
                        else "Missing independent support.",
                    }
                },
            },
        )
        if index < 4:
            choose(h, view, "ot_case_page_next")
    reviewed = session.review()
    assert (
        reviewed["cases"][2]["suggestion"] == "MANUAL"
        and reviewed["cases"][2]["owner_adjudicated_target"] == "APPROVE"
    )
    assert (
        reviewed["cases"][2]["disposition"] == "edit"
        and reviewed["cases"][0]["status"] == "unresolved"
    )
    choose(h, view, "ot_case_splits")
    h.form(
        view,
        {
            f"family_{i}": {
                f"family_{i}": select(
                    "excluded" if i == 0 else "development" if i == 1 else "reserved"
                )
            }
            for i in range(5)
        },
    )
    assert "0 reserved" in text(h, view)
    choose(h, view, "ot_case_commit")
    frozen = session.review()["frozen"]
    assert (
        frozen["reserved"],
        frozen["development"],
        frozen["excluded"],
        frozen["unresolved"],
    ) == (3, 1, 1, 1)
    assert frozen["uncovered_critical"] and "Frozen, unscored" in text(h, view)
    assert "Owner decision" not in text(h, view) and "Suggested action" not in text(h, view)
    assert effects(h) == (0,) * 7
