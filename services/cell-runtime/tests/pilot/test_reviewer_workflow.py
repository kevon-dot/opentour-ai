"""Exercise native review discovery through authorized callbacks and actual frozen reports."""

import json
import sqlite3
import time
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import slack_adapter, slack_views
from opentour_cell.pilot.operating_point import AGREEMENT_VERSION, source_digest
from opentour_cell.pilot.training_data import generated_evaluation_dataset
from test_slack_adapter import ACTOR, DATA, OTHER, Harness
from test_slack_views import readout

Json = dict[str, Any]


def buttons(view: Json) -> dict[str, str]:
    return {
        item["action_id"]: item["value"]
        for block in view["blocks"]
        for item in block.get("elements", [])
        if item.get("type") == "button"
    }


def supplied(
    h: Harness,
    *,
    qualified: bool = False,
    suffix: str = "",
    supersedes: str = "",
    expires_at: int = 4102444800,
) -> str:
    data = generated_evaluation_dataset("pilot")
    data["dataset_id"] += suffix
    for case in data["manifest"]["cases"]:
        case["case_id"] += suffix
    if qualified:
        data["operating_agreement"] = {
            "schema_version": AGREEMENT_VERSION,
            "organization": "pilot",
            "engagement": h.store.engagement_id,
            "source_digest": source_digest(data),
            "current_method": "private-method-a",
            "repair_method": "private-method-b",
            "owner_attestation": "att-0123456789abcdef",
            "agreed_at": 100000,
            "expires_at": expires_at,
            "state": "active",
            "basis": "predeclared",
            "minimum_coverage_bps": 9000,
            "error_ceiling_bps": data["manifest"]["error_ceiling_bps"],
        }
        if supersedes:
            data["operating_agreement"]["supersedes"] = supersedes
    h.store.register(data)
    return str(data["dataset_id"])


def screen(h: Harness, view: str) -> Json:
    return h.slack.screens[view]


def text(h: Harness, view: str) -> str:
    return json.dumps(screen(h, view), ensure_ascii=False)


def restart(h: Harness, *, lifecycle_authority=None) -> None:
    h.app.pool.shutdown()
    h.app = slack_adapter.Workbench(
        lambda: slack_adapter.Settings.load(h.path),
        h.slack,
        h.store,
        slack_views,
        submit=lambda call: call(),
        lifecycle_authority=lifecycle_authority,
    )


def test_supplied_approval_and_questions_recover_after_restart(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    ds = supplied(h, qualified=True)
    h.config["users"][ACTOR]["datasets"] = [ds]
    h.config["zapier"] = False
    h.save()
    assert "Choose the exact readout" in text(h, h.launch(prompt="ask coverage"))
    view = h.launch(prompt="status")
    assert "Ready for evidence review" in text(h, view)
    assert "ot_tune" not in buttons(screen(h, view))
    revision = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", revision)
    assert "Agreed minimum automated coverage ≥90%" in text(h, view)
    assert "private-method-a" not in text(h, view)
    h.action(view, "ot_question", revision)
    for topic, expected in (
        ("errors", "Incorrect automatic decisions"),
        ("coverage", "combined missed"),
        ("changes", "Paired fixes"),
        ("audience", "UAUDIENCE"),
    ):
        assert buttons(screen(h, view))["ot_question_" + topic] == revision
        h.action(view, "ot_question_" + topic, revision)
        assert expected in text(h, view) and revision in text(h, view)
        assert "ot_approve" not in buttons(screen(h, view))
        assert h.app.journal.decision(revision) == {}
    assert "Agreed minimum automated coverage ≥90%" in text(h, h.launch(prompt="ask threshold"))
    h.action(view, "ot_home", "home")
    click = h.action(view, "ot_changes", revision)
    h.send("interactive", click)
    receipt = h.app.journal.decision(revision)
    assert receipt["disposition"] == "changes_requested" and receipt["delivery"] == "local_only"
    recorded = h.app.journal.receipt(receipt["receipt"])
    assert recorded["evidence"]["operating_point"]["minimum_coverage_bps"] == 9000
    assert (
        recorded["evidence"]["decision_quality_digest"]
        == h.store.frozen_evaluation(ds)["decision_quality_digest"]
    )
    restart(h)
    for prompt in ("", "status", "review"):
        reopened = h.launch(prompt=prompt)
        assert "Changes requested" in text(h, reopened) and receipt["receipt"] in text(h, reopened)
        assert "ot_approve" not in buttons(screen(h, reopened))


def test_mixed_chooser_pins_selected_revision_and_keeps_results_independent(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _, job, trained_revision = h.completed()
    supplied_id = supplied(h)
    h.config["users"][ACTOR]["datasets"] = [DATA, supplied_id]
    h.config["zapier"] = False
    h.save()

    def choose(dataset: str) -> str:
        view = h.launch()
        rows = screen(h, view)["blocks"]
        assert screen(h, view)["callback_id"] == "ot_review_list"
        options = {
            b["text"]["text"].split(" · ")[0]: b["accessory"]["value"]
            for b in rows
            if "accessory" in b
        }
        assert set(options) == {DATA, supplied_id}
        h.action(view, "ot_result_open", options[dataset])
        return view

    training = choose(DATA)
    assert trained_revision in text(h, training) and job in text(h, training)
    h.action(training, "ot_evidence", trained_revision)
    h.action(training, "ot_question", trained_revision)
    for topic, phrase in (
        ("errors", "2/4 (50%)"),
        ("coverage", "4/4 (100%)"),
        ("changes", "regressions 0"),
    ):
        h.action(training, "ot_question_" + topic, trained_revision)
        assert phrase in text(h, training)
        if topic == "coverage":
            assert "Descriptive comparison" in text(h, training)
    assert "Completed" in text(h, h.launch(prompt="status " + job))
    assert "Choose the exact" in text(h, h.launch(prompt="ask errors"))
    evaluation = choose(supplied_id)
    supplied_revision = buttons(screen(h, evaluation))["ot_evidence"]
    assert supplied_revision != trained_revision
    h.action(evaluation, "ot_evidence", supplied_revision)
    h.action(evaluation, "ot_home", "home")
    h.action(evaluation, "ot_approve", supplied_revision)
    restart(h)
    assert "Cleared for readout" in text(h, choose(supplied_id))
    assert "Ready for evidence review" in text(h, choose(DATA))
    assert not h.app.journal.decision(trained_revision)
    assert not h.slack.post


@pytest.mark.parametrize("text_length", [0, 64, 65, 100, 129])
def test_search_boundary_and_recoverable_help(tmp_path: Path, text_length: int) -> None:
    h = Harness(tmp_path)
    query = "z" * text_length
    view = h.launch(prompt="data")
    assert screen(h, view)["blocks"][0]["element"]["max_length"] == 64
    h.form(view, {"query": {"query": {"value": query or None}}})
    for looked_up in (view, h.launch(prompt="search " + query)):
        if text_length > 64:
            assert "64 printable characters" in text(h, looked_up)
            h.action(looked_up, "ot_browse", "home")
            assert screen(h, looked_up)["callback_id"] == "ot_dataset_search"
        elif text_length:
            assert "No authorized datasets match" in text(h, looked_up)
        else:
            assert DATA in text(h, looked_up)
    assert screen(h, h.launch(prompt="help"))["callback_id"] == "ot_help_view"
    assert screen(h, h.launch(prompt="please approve everything"))["callback_id"] == "ot_help_view"
    assert "ot_approve" not in buttons(screen(h, h.launch(prompt="approve")))


def test_workspace_tune_role_alone_cannot_offer_or_create_proposal(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    home = h.launch(actor=OTHER)
    assert "ot_tune" not in buttons(screen(h, home))
    view = h.launch(actor=OTHER, prompt="data")
    h.action(view, "ot_dataset_open", DATA, actor=OTHER)
    assert "ot_tune_dataset" not in buttons(screen(h, view))
    h.action(view, "ot_tune_dataset", DATA, actor=OTHER)
    assert "Confirm training" not in text(h, view)
    assert not h.app.journal.latest([DATA])
    with pytest.raises(ValueError, match="training_not_authorized"):
        h.store.propose(DATA, authorized_trainer=OTHER)
    view, job = h.propose()
    assert h.store.status(job)["status"] == "awaiting_approval"
    assert screen(h, view)["callback_id"] == "ot_tune_start"


@pytest.mark.parametrize(
    "mode", ["aggregate", "audience", "membership", "data", "inspect_other", "failed", "withdrawal"]
)
def test_questions_fail_closed_for_stale_or_unpresented_evidence(tmp_path: Path, mode: str) -> None:
    h = Harness(tmp_path)
    view, _, revision = h.completed()
    h.slack.fail_evidence = mode == "failed"
    h.action(view, "ot_evidence", revision)
    if mode == "failed":
        assert not h.app.journal.inspected(ACTOR, revision, h.app._policy(h.app.settings(), ACTOR))
        assert "Choose the exact readout" in text(h, h.launch(prompt="ask errors"))
        return
    before = deepcopy(h.payload(view))
    if mode == "aggregate":
        h.config["users"][ACTOR]["aggregate"] = False
    elif mode == "audience":
        h.config["recipient"] = "UOTHER"
    elif mode == "membership":
        h.slack.members.remove(ACTOR)
    elif mode == "data":
        h.store.revoke(DATA)
    elif mode == "withdrawal":
        h.store.withdraw()
    h.save()
    if mode == "inspect_other":
        before["user"]["id"] = OTHER
    before.update(
        type="block_actions", actions=[{"action_id": "ot_question_errors", "value": revision}]
    )
    h.send("interactive", before)
    if mode != "inspect_other":
        assert screen(h, view).get("callback_id") != "ot_questions_view"
    assert not h.app.journal.decision(revision)


def test_superseding_agreement_requires_fresh_inspection_before_any_decision(
    tmp_path: Path,
) -> None:
    h = Harness(tmp_path)
    old = supplied(h, qualified=True)
    h.config["users"][ACTOR]["datasets"] = [old]
    h.save()
    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", revision)
    h.action(view, "ot_home", "home")
    assert buttons(screen(h, view))["ot_approve"] == revision
    previously_inspected = h.store.frozen_evaluation(old)["operating_point"]["agreement_revision"]
    fresh = supplied(h, qualified=True, suffix="-new", supersedes=previously_inspected)
    h.config["users"][ACTOR]["datasets"] = [old, fresh]
    h.save()
    h.action(view, "ot_approve", revision)
    assert not h.app.journal.decision(revision)
    next_view = h.launch()
    next_revision = buttons(screen(h, next_view))["ot_evidence"]
    assert next_revision != revision and "ot_approve" not in buttons(screen(h, next_view))
    assert "Choose the exact readout" in text(h, h.launch(prompt="ask coverage"))
    h.action(next_view, "ot_evidence", next_revision)
    h.action(next_view, "ot_home", "home")
    h.action(next_view, "ot_changes", next_revision)
    assert h.app.journal.decision(next_revision)["disposition"] == "changes_requested"


@pytest.mark.parametrize("failure", ["expired", "invalid_agreement", "invalid_frozen"])
def test_independent_granted_readout_survives_one_held_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    h = Harness(tmp_path)
    _, _, trained_revision = h.completed()
    expires = int(time.time()) + 300
    ds = supplied(h, qualified=True, suffix="-held", expires_at=expires)
    h.config["users"][ACTOR]["datasets"] = [DATA, ds]
    h.save()
    if failure == "expired":
        monkeypatch.setattr(time, "time", lambda: expires + 1)
    else:
        with sqlite3.connect(h.store.path) as db:
            if failure == "invalid_agreement":
                db.execute("UPDATE operating_agreements SET content='{}' WHERE dataset=?", (ds,))
            else:
                db.execute("UPDATE dataset_scope SET baseline_result='{}' WHERE dataset=?", (ds,))
    view = h.launch()
    if failure == "invalid_frozen":
        rows = [b for b in screen(h, view)["blocks"] if "accessory" in b]
        healthy, held = rows
        assert "Evidence unavailable" in held["text"]["text"]
        h.action(view, "ot_result_open", held["accessory"]["value"])
        assert "Pending verification" in text(h, view) and "ot_evidence" not in buttons(
            screen(h, view)
        )
        h.action(view, "ot_readouts", "home")
        h.action(view, "ot_result_open", healthy["accessory"]["value"])
    assert ds not in text(h, view)
    assert buttons(screen(h, view))["ot_evidence"] == trained_revision


def test_result_pagination_binds_choices_to_the_displayed_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(slack_adapter, "RESULT_PAGE_SIZE", 1)
    h = Harness(tmp_path)
    one, two = supplied(h, suffix="-a"), supplied(h, suffix="-b")
    h.config["users"][ACTOR]["datasets"] = [one, two]
    h.save()
    view = h.launch()
    first = next(b["accessory"]["value"] for b in screen(h, view)["blocks"] if "accessory" in b)
    assert one in text(h, view) and two not in text(h, view)
    assert "ot_result_prev" not in buttons(screen(h, view))
    h.action(view, "ot_result_next", "next")
    assert two in text(h, view) and one not in text(h, view)
    assert "ot_result_next" not in buttons(screen(h, view))
    h.action(view, "ot_result_prev", "previous")
    assert one in text(h, view) and two not in text(h, view)
    h.action(view, "ot_result_next", "next")
    h.action(view, "ot_result_open", first)
    assert "Access unavailable" in text(h, view)


def test_direct_job_access_respects_the_audience_bound_in_the_picker(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    _, job, revision = h.completed()
    h.config["recipient"] = "UOTHERBUYER"
    h.save()
    assert revision not in text(h, h.launch())
    for command in (job, "status " + job):
        view = h.launch(prompt=command)
        assert "Access unavailable" in text(h, view)
        assert revision not in text(h, view) and DATA not in text(h, view)
    assert not h.app.journal.decision(revision)


def test_question_answers_keep_undefined_error_and_changed_subsets_explicit() -> None:
    context, revision = "ctx_generated", "rev_generated"
    empty = slack_views.question_modal(
        context, revision, readout([]), {"recipient": "UOWNER"}, "errors"
    )
    assert "0/0 (undefined)" in json.dumps(empty) and "undetermined" in json.dumps(empty)
    cohort = readout([("a", "a", "review"), ("b", "review", "b"), ("a", "b", "a")])
    changed = slack_views.question_modal(
        context, revision, cohort, {"recipient": "UOWNER"}, "changes"
    )
    wording = json.dumps(changed)
    assert "Different cases were automated" in wording and "Eligible pairs 1/3" in wording
    assert "Fewer observed errors" not in wording


def test_regression_question_requires_scoped_choice_and_rechecks_selection(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    first, second = supplied(h, qualified=True), supplied(h, qualified=True, suffix="-second")
    h.config["users"][ACTOR]["datasets"] = [first, second]
    h.config["users"][OTHER]["datasets"] = [second]
    h.save()
    prompt = "Which cases regressed?"
    view = h.launch(prompt=prompt)
    assert "No currently inspected comparison" in text(h, view)
    h.action(view, "ot_readouts", "home")
    choices = {
        row["text"]["text"].split(" · ")[0]: row["accessory"]["value"]
        for row in screen(h, view)["blocks"]
        if "accessory" in row
    }
    revisions = []
    for dataset in (first, second):
        h.action(view, "ot_result_open", choices[dataset])
        revision = buttons(screen(h, view))["ot_evidence"]
        h.action(view, "ot_evidence", revision)
        revisions.append(revision)
        h.action(view, "ot_readouts", "home")

    foreign = h.launch(actor=OTHER, prompt=prompt)
    assert all(revision not in text(h, foreign) for revision in revisions)
    private = h.launch(prompt=prompt)
    h.action(private, "ot_result_open", revisions[0], actor=OTHER)
    assert "0 of 3 comparable" not in text(h, private)
    for selected in revisions:
        picker = h.launch(prompt=prompt)
        assert all(revision in text(h, picker) for revision in revisions)
        click = h.action(picker, "ot_result_open", selected)
        assert selected in text(h, picker) and "0 of 3 comparable" in text(h, picker)
        h.send("interactive", click)
        assert not h.app.journal.decision(selected)

    stale = h.launch(prompt=prompt)
    with h.app.journal.db() as db:
        db.execute("UPDATE presentations SET expires=0 WHERE revision=?", (revisions[0],))
    h.action(stale, "ot_result_open", revisions[0])
    assert "0 of 3 comparable" not in text(h, stale)
    assert revisions[0] not in text(h, h.launch(prompt=prompt))

    revoked = h.launch(prompt=prompt)
    h.config["users"][ACTOR]["datasets"] = [first]
    h.save()
    h.action(revoked, "ot_result_open", revisions[1])
    assert "0 of 3 comparable" not in text(h, revoked)
    assert not h.app.journal.latest([first, second]) and not h.slack.post
    assert all(not h.app.journal.decision(revision) for revision in revisions)


@pytest.mark.parametrize("withdrawal", ["dataset", "inspection", "actor"])
def test_regression_picker_rechecks_every_row_at_admission(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, withdrawal: str
) -> None:
    h = Harness(tmp_path)
    datasets = [supplied(h, qualified=True, suffix=suffix) for suffix in ("-first", "-last")]
    h.config["users"][ACTOR]["datasets"] = datasets
    h.save()
    revisions = []
    for dataset in datasets:
        view = h.launch()
        option = next(
            row["accessory"]["value"]
            for row in screen(h, view)["blocks"]
            if "accessory" in row and row["text"]["text"].startswith(dataset + " · ")
        )
        h.action(view, "ot_result_open", option)
        revision = buttons(screen(h, view))["ot_evidence"]
        h.action(view, "ot_evidence", revision)
        revisions.append(revision)
    original = h.app.views.regression_picker
    rendered = []

    def withdraw_before_admission(*args: Any, **kwargs: Any) -> Json:
        modal = original(*args, **kwargs)
        rendered.append(json.dumps(modal))
        if withdrawal == "dataset":
            h.store.revoke(datasets[-1])
        elif withdrawal == "inspection":
            with h.app.journal.db() as db:
                db.execute("UPDATE presentations SET expires=0 WHERE revision=?", (revisions[-1],))
        else:
            h.config["users"][ACTOR]["datasets"] = datasets[:1]
            h.save()
        return modal

    monkeypatch.setattr(h.app.views, "regression_picker", withdraw_before_admission)
    picker = h.launch(prompt="What got worse?")
    assert len(rendered) == 1 and all(revision in rendered[0] for revision in revisions)
    assert all(ref not in text(h, picker) for ref in datasets + revisions)
    assert "ot_result_open" not in buttons(screen(h, picker))
    with h.app.journal.db() as db:
        assert (
            db.execute(
                "SELECT COUNT(*) FROM exposures WHERE kind='ot_regression_picker'"
            ).fetchone()[0]
            == 0
        )
