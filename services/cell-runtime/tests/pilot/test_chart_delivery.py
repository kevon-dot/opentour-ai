"""Exercise the private comparison charts from a signed-in Slack user's actual modal routes."""

import json
from copy import deepcopy
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from opentour_cell.pilot import repair_workflow, source_intake
from opentour_cell.pilot.managed_run import plan_revision
from test_app_home import publish
from test_managed_slack import setup_workflow
from test_reviewer_workflow import buttons, restart, screen, text
from test_slack_adapter import ACTOR, CHANNEL, OTHER, Harness
from test_slack_views import readout

Json = dict[str, Any]


def inspect(h: Harness) -> tuple[str, str]:
    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", revision)
    assert buttons(screen(h, view))["ot_charts"] == revision
    return view, revision


def protected_counts(h: Harness) -> tuple[int, ...]:
    with h.app.journal.db() as db:
        return tuple(
            db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("presentations", "decisions", "authorizations", "jobs", "proposals")
        )


@pytest.mark.parametrize("route", ["rule", "model", "measured_hold"])
def test_named_reviewer_gets_one_aggregate_ephemeral_without_changing_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str
) -> None:
    h, _, run, outcome = setup_workflow(tmp_path, route, release=True)
    h.config["workflow_label"] = "Invoice classification"
    h.save()
    view, revision = inspect(h)
    assert "Invoice classification" in text(h, view)
    assert "data_visualization" not in text(h, view)
    before = protected_counts(h)
    output = h.path.parent / "managed-outputs" / run
    original = {p.name: p.read_bytes() for p in output.glob("*.json")}
    deliver = MagicMock(return_value={"ok": True, "message_ts": "101.102"})
    monkeypatch.setattr(h.slack, "chat_postEphemeral", deliver, raising=False)
    click = h.action(view, "ot_charts", revision)
    restart(h)
    h.send("interactive", click)
    assert deliver.call_count == 1
    payload = deliver.call_args.kwargs
    assert payload.keys() == {"channel", "user", "text", "blocks"}
    assert (payload["channel"], payload["user"]) == (CHANNEL, ACTOR)
    assert payload["blocks"][0]["text"]["text"] == payload["text"]
    charts = [b for b in payload["blocks"] if b["type"] == "data_visualization"]
    assert len(charts) == (1 if outcome == "hold" else 2)
    assert "Current workflow" in payload["text"] and "Simple repair" in payload["text"]
    assert ("Tuned model" in payload["text"]) == (route != "rule")
    frozen = json.loads(original["comparison.json"])
    for arm, label in (
        ("incumbent", "Current workflow"),
        ("simple", "Simple repair"),
        ("model", "Tuned model"),
    ):
        if arm not in frozen["arms"]:
            continue
        rows = [r for r in frozen["paired_cases"] if r[arm]["status"] == "automatic"]
        known = [r for r in rows if r["reference"] is not None]
        wrong = sum(r[arm]["label"] != r["reference"] for r in known)
        assert (
            f"{label} · {len(rows)}/{len(frozen['paired_cases'])} eligible cases automated"
            in payload["text"]
        )
        assert f"{wrong}/{len(known)} verified automated decisions wrong" in payload["text"]
    for excluded in (
        "generated-repair",
        "case_id",
        "paired_cases",
        "att-",
        "UAUDIENCE",
        "customer cancelled",
        "handoff_disposition",
        "workflow_benchmark",
        "reviewer_minutes",
    ):
        assert excluded not in json.dumps(payload)
    assert screen(h, view)["callback_id"] == "ot_chart_status"
    assert any(b.get("text", {}).get("text") == payload["text"] for b in screen(h, view)["blocks"])
    assert "data_visualization" not in text(h, view) and "Slack accepted" in text(h, view)
    assert before == protected_counts(h) and not h.slack.post
    assert original == {p.name: p.read_bytes() for p in output.glob("*.json")}
    h.action(view, "ot_evidence", revision)
    assert screen(h, view)["callback_id"] == "ot_evidence_view"


@pytest.mark.parametrize(
    "block",
    [
        "actor",
        "team",
        "channel",
        "audience",
        "aggregate",
        "role",
        "expired",
        "source",
        "grant",
        "revision",
        "missing_inspection",
        "corrupt_seal",
        "wrong_surface",
        "wrong_modal_channel",
        "wrong_modal_team",
        "before_inspection",
    ],
)
def test_stale_or_unauthorized_callback_acknowledges_without_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, block: str
) -> None:
    h, plan, _, _ = setup_workflow(tmp_path, "rule")
    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    if block != "before_inspection":
        h.action(view, "ot_evidence", revision)
    click = h.payload(view, OTHER if block == "actor" else ACTOR)
    click.update(type="block_actions", actions=[{"action_id": "ot_charts", "value": revision}])
    if block == "team":
        click["team"]["id"] = "TOTHER"
    elif block == "channel":
        click["channel"] = {"id": "CELSEWHERE"}
    elif block == "audience":
        h.config["recipient"] = "UOTHER"
    elif block == "aggregate":
        h.config["users"][ACTOR]["aggregate"] = False
    elif block == "role":
        h.config["users"][ACTOR]["roles"] = ["inspect"]
    elif block == "expired":
        h.config["users"][ACTOR]["expires_at"] = 1
    elif block == "source":
        grant = json.loads((h.path.parent / "source-grant.json").read_text())
        source_intake.withdraw_intake(h.store, grant["owner_attestation"])
    elif block == "grant":
        (h.path.parent / ("slack-review-" + plan_revision(plan) + ".json")).unlink()
    elif block == "revision":
        click["actions"][0]["value"] = "rev_" + "0" * 26
    elif block in {"missing_inspection", "corrupt_seal"}:
        with h.app.journal.db() as db:
            if block == "missing_inspection":
                db.execute("DELETE FROM presentations")
            else:
                db.execute("UPDATE presentations SET rendered='{}'")
    elif block == "wrong_surface":
        click["container"] = {"type": "message", "channel_id": CHANNEL, "message_ts": "1.1"}
    elif block == "wrong_modal_channel":
        click["container"] = {"type": "view", "view_id": view, "channel_id": "CELSEWHERE"}
    elif block == "wrong_modal_team":
        click["view"]["team_id"] = "TOTHER"
    h.save()
    h.app.web = MagicMock()
    before = protected_counts(h)
    h.send("interactive", click)
    assert not h.app.web.method_calls and before == protected_counts(h)


@pytest.mark.parametrize(
    "failure",
    ["unsupported", "rejected", "timeout", "public", "membership", "revoked_during_check"],
)
def test_denied_or_uncertain_delivery_keeps_the_figures_private(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    h, plan, _, _ = setup_workflow(tmp_path, "rule")
    view, revision = inspect(h)
    before = protected_counts(h)
    post = MagicMock(return_value={"ok": False, "error": "invalid_blocks"})
    if failure == "timeout":
        post.side_effect = TimeoutError("generated only")
    if failure == "public":
        h.slack.channel["is_private"] = False
    if failure == "membership":
        h.slack.members = [OTHER]
    if failure == "revoked_during_check":

        def revoke(**_kwargs: Any) -> Json:
            (h.path.parent / ("slack-review-" + plan_revision(plan) + ".json")).unlink()
            return {"members": [ACTOR]}

        monkeypatch.setattr(h.slack, "conversations_members", revoke)
    if failure != "unsupported":
        monkeypatch.setattr(h.slack, "chat_postEphemeral", post, raising=False)
    click = h.action(view, "ot_charts", revision)
    h.send("interactive", click)
    assert before == protected_counts(h) and not h.slack.post
    if failure in {"public", "membership", "revoked_during_check"}:
        assert post.call_count == 0 and screen(h, view)["callback_id"] == "ot_evidence_view"
    else:
        assert post.call_count == int(failure != "unsupported")
        assert "Slack could not confirm" in text(h, view)
        assert "verified automated decisions wrong" in text(h, view)
        assert buttons(screen(h, view))["ot_evidence"] == revision


@pytest.mark.parametrize("cohorts", [None, {"rule": "a" * 64}, {"model": "a" * 64}])
def test_missing_or_mismatched_arm_membership_leaves_verified_numeric_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cohorts: Json | None
) -> None:
    h, _, _, _ = setup_workflow(tmp_path, "rule")
    real = repair_workflow.managed_review_evidence

    def legacy(*args: Any) -> Json:
        data = deepcopy(real(*args))
        if cohorts is None:
            data["audit"].pop("comparison_cohorts")
        else:
            data["audit"]["comparison_cohorts"] = cohorts
        return data

    monkeypatch.setattr(repair_workflow, "managed_review_evidence", legacy)
    view = h.launch()
    ref = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", ref)
    assert "Incorrect automatic decisions" in text(h, view)
    assert "ot_charts" not in buttons(screen(h, view))
    h.app.web = MagicMock()
    h.action(view, "ot_charts", ref)
    assert not h.app.web.method_calls


def test_home_and_nonnamed_inspectors_receive_no_chart_surface(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, _, _, _ = setup_workflow(tmp_path, "rule")
    h.config["users"][OTHER]["datasets"] = ["generated-repair"]
    h.save()
    assert "No repair needs your review" in json.dumps(publish(h, monkeypatch))
    assert "needs your review" not in json.dumps(publish(h, monkeypatch, actor=OTHER))
    view = h.launch(actor=OTHER)
    ref = buttons(screen(h, view))["ot_evidence"]
    h.action(view, "ot_evidence", ref, actor=OTHER)
    assert "ot_charts" not in buttons(screen(h, view))
    assert "data_visualization" not in text(h, view)


def test_readout_receipt_still_verifies_separate_chart_and_release_inspection(
    tmp_path: Path,
) -> None:
    h, _, run, _ = setup_workflow(tmp_path, "rule", release=True)
    view, revision = inspect(h)
    h.action(view, "ot_home", "home")
    h.action(view, "ot_approve", revision)
    decision = h.app.journal.decision(revision)
    receipt = h.app.journal.receipt(decision["receipt"])
    assert receipt["evidence"]["readout_revision"] == revision
    assert receipt["evidence"]["chart_context"] and receipt["evidence"]["release_settings_file"]
    assert "release_settings_file" not in json.dumps(receipt["rendered"])
    assert not (h.path.parent / "managed-outputs" / run / "release-decision.json").exists()


@pytest.mark.parametrize("empty", [True, False])
def test_held_sparse_evidence_uses_numeric_modal_and_names_limited_comparability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, empty: bool
) -> None:
    h, _, _, _ = setup_workflow(tmp_path, "rule")
    actual = repair_workflow.managed_review_evidence

    def limited(*args: Any) -> Json:
        data = deepcopy(actual(*args))
        cases = [] if empty else [("a", "a", "review"), (None, "review", "a")]
        report = readout(cases)
        data["readout"] = report
        data["audit"]["handoff_disposition"] = "hold"
        data["audit"]["comparisons"] = {"rule": report}
        data["audit"].pop("workflow_benchmark", None)
        data["audit"].pop("operating_point", None)
        return data

    monkeypatch.setattr(repair_workflow, "managed_review_evidence", limited)
    view, revision = inspect(h)
    post = MagicMock(return_value={"ok": True, "message_ts": "101.102"})
    monkeypatch.setattr(h.slack, "chat_postEphemeral", post, raising=False)
    before = protected_counts(h)
    h.action(view, "ot_charts", revision)
    assert before == protected_counts(h)
    assert "No comparable paired cases" in text(h, view)
    assert "verified error rate unavailable" in text(h, view)
    if empty:
        assert not post.called and "No eligible protected cases" in text(h, view)
    else:
        blocks = post.call_args.kwargs["blocks"]
        assert len([b for b in blocks if b["type"] == "data_visualization"]) == 1
        assert "Different cases were automated" in text(h, view)
        assert "Tuned model" not in text(h, view)
