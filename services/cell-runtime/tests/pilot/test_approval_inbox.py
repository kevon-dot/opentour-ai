"""An opted-in channel gets one generic button. Only inspected evidence permits a handoff."""

from __future__ import annotations

import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import approval_inbox as inbox
from opentour_cell.pilot import release_gate, repair_workflow, source_intake
from opentour_cell.pilot.managed_run import plan_revision
from test_managed_slack import setup_workflow
from test_reviewer_workflow import buttons, restart, screen, text
from test_slack_adapter import ACTOR, APP, CHANNEL, OTHER, TEAM, Harness

Json = dict[str, Any]


def stage(
    path: Path, route: str = "rule", *, audience: str | None = None, slot: str = ""
) -> tuple[Harness, Json, str, str, Json]:
    h, plan, run, _ = setup_workflow(path, route, release=True, audience=audience, slot=slot)
    evidence = repair_workflow.managed_review_evidence(
        h.path.parent,
        h.store.organization,
        run,
        plan["audience"],
        h.config["team"],
        h.config["channel"],
    )
    audit = evidence["audit"]
    info = {
        "team": h.config["team"],
        "channel": h.config["channel"],
        "audience": plan["audience"],
        "reviewer": h.config["reviewer"],
        "run_revision": audit["run_revision"],
    }
    receipt = inbox.enqueue_ready(h.path.parent, h.store.organization, run, **info)["receipt"]
    assert (
        inbox.enqueue_ready(h.path.parent, h.store.organization, run, **info)["receipt"] == receipt
    )
    assert not h.slack.post
    return h, plan, run, receipt, audit


def callback(h: Harness, receipt: str, *, actor: str = ACTOR, ts: str = "101.101") -> Json:
    return {
        "api_app_id": APP,
        "team": {"id": h.config["team"]},
        "user": {"id": actor},
        "type": "block_actions",
        "trigger_id": "generated-trigger",
        "channel": {"id": h.config["channel"]},
        "container": {"type": "message", "channel_id": h.config["channel"], "message_ts": ts},
        "actions": [{"action_id": "ot_open_approval", "value": receipt}],
        "message": {"app_id": APP, "ts": ts, "blocks": deepcopy(h.slack.post["blocks"])},
    }


def opt_in(h: Harness) -> None:
    h.config["approval_requests"] = True
    h.save()


@pytest.mark.parametrize(
    ("route", "choice"),
    [("rule", "approved"), ("model", "approved"), ("rule", "changes_requested")],
)
def test_once_notification_opens_evidence_and_requires_exact_explicit_handoff(
    tmp_path: Path, route: str, choice: str
) -> None:
    h, plan, run, receipt, audit = stage(tmp_path, route)
    check = (
        h.path.parent,
        h.store.organization,
        run,
        audit["run_revision"],
        audit["candidate_digest"],
    )
    assert inbox.flush_approvals(h.app) == 0
    opt_in(h)
    with ThreadPoolExecutor(max_workers=3) as pool:
        counts = list(pool.map(lambda _: inbox.flush_approvals(h.app), range(3)))
    assert sum(counts) == 1
    post = json.dumps(h.slack.post)
    assert "Open evidence" in post and receipt in post
    for private in (
        run,
        plan["intake_revision"],
        audit["candidate_digest"],
        audit["run_revision"],
        plan["audience"],
        "description",
        "refund",
        "coverage",
        ACTOR,
    ):
        assert private not in post
    restart(h)
    assert inbox.flush_approvals(h.app) == 0 and not inbox.unresolved(h.app.settings())
    with pytest.raises((ValueError, FileNotFoundError)):
        release_gate.authorize_release(*check)
    # Capacity to approve presenting the readout does not release the repair.
    simple = h.launch()
    ref = buttons(screen(h, simple))["ot_evidence"]
    h.action(simple, "ot_evidence", ref)
    h.action(simple, "ot_home", "home")
    h.action(simple, "ot_approve", ref)
    with pytest.raises((ValueError, FileNotFoundError)):
        release_gate.authorize_release(*check)
    h.slack.calls.clear()
    h.send("interactive", callback(h, receipt))
    assert h.slack.calls[:3] == ["ack", "open", "info"]
    view = list(h.slack.screens)[-1]
    body = text(h, view)
    assert "Approve repair for handoff" in body and plan["audience"] in body
    assert audit["candidate_digest"] in body and "Agreed minimum" in body
    assert not (h.path.parent / "managed-outputs" / run / "release-decision.json").exists()
    h.action(view, "ot_release_approve" if choice == "approved" else "ot_release_changes", ref)
    assert ("Approved for handoff" if choice == "approved" else "Changes requested") in text(
        h, view
    )
    if choice == "approved":
        assert release_gate.authorize_release(*check)["provider"] == "slack"
        if route == "rule" or os.environ.get("OPENTOUR_TEST_MODEL_BASE"):
            assert (
                repair_workflow.prepare_handoff(h.path.parent, h.store.organization, plan, run)[
                    "disposition"
                ]
                == route
            )
    else:
        with pytest.raises(ValueError, match="human_release_approval_required"):
            release_gate.authorize_release(*check)


@pytest.mark.parametrize(
    "bad", ["public", "member", "role", "audience", "source", "grant", "reviewer", "wrong_revision"]
)
def test_notifications_recheck_scope_before_send(tmp_path: Path, bad: str) -> None:
    h, plan, run, receipt, audit = stage(tmp_path)
    opt_in(h)
    if bad == "public":
        h.slack.channel["is_private"] = False
    elif bad == "member":
        h.slack.members.remove(ACTOR)
    elif bad == "role":
        h.config["users"][ACTOR]["roles"].remove("review")
    elif bad == "audience":
        h.config["recipient"] = "UDIFFERENT"
    elif bad == "source":
        grant = json.loads((h.path.parent / "source-grant.json").read_text())
        source_intake.withdraw_intake(h.store, grant["owner_attestation"])
    elif bad == "grant":
        (h.path.parent / ("slack-review-" + plan_revision(plan) + ".json")).unlink()
    elif bad == "reviewer":
        h.config["reviewer"] = OTHER
    else:
        with pytest.raises(ValueError):
            inbox.enqueue_ready(
                h.path.parent,
                h.store.organization,
                run,
                run_revision="f" * 64,
                team=TEAM,
                channel=CHANNEL,
                audience=plan["audience"],
                reviewer=ACTOR,
            )
        p = h.path.parent / ("approval-ready-" + receipt + ".json")
        current = json.loads(p.read_text())
        current["run_revision"] = "f" * 64
        p.write_text(json.dumps(current))
    h.save()
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post


def test_ambiguous_provider_acceptance_reconciles_at_click_and_never_resends(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h, _, _, receipt, _ = stage(tmp_path)
    opt_in(h)
    calls = 0
    send = h.slack.chat_postMessage

    def ambiguous(**payload: Any) -> Json:
        nonlocal calls
        calls += 1
        send(**payload)
        raise TimeoutError

    monkeypatch.setattr(h.slack, "chat_postMessage", ambiguous)
    assert inbox.flush_approvals(h.app) == 0
    assert inbox.unresolved(h.app.settings()) == [{"receipt": receipt, "state": "uncertain"}]
    restart(h)
    assert inbox.flush_approvals(h.app) == 0 and calls == 1
    base = len(h.slack.screens)
    h.send("interactive", callback(h, receipt, actor=OTHER))
    forged = callback(h, receipt)
    forged["message"]["app_id"] = "AOTHER"
    h.send("interactive", forged)
    assert len(h.slack.screens) == base
    h.send("interactive", callback(h, receipt))
    assert "ot_release_approve" in buttons(screen(h, list(h.slack.screens)[-1]))
    assert not inbox.unresolved(h.app.settings())
    h.send("interactive", callback(h, receipt, ts="999.999"))
    assert len(h.slack.screens) == base + 1


def test_wrong_or_expired_presentation_cannot_release(tmp_path: Path) -> None:
    h, plan, run, receipt, _ = stage(tmp_path)
    opt_in(h)
    assert inbox.flush_approvals(h.app) == 1
    h.send("interactive", callback(h, receipt))
    view = list(h.slack.screens)[-1]
    revision = buttons(screen(h, view))["ot_release_approve"]
    with h.app.journal.db() as db:
        db.execute("UPDATE presentations SET expires=0")
    h.action(view, "ot_release_approve", revision)
    assert "Access unavailable" in text(h, view)
    assert not (h.path.parent / "managed-outputs" / run / "release-decision.json").exists()
    (h.path.parent / ("slack-review-" + plan_revision(plan) + ".json")).unlink()
    before = len(h.slack.screens)
    h.send("interactive", callback(h, receipt))
    assert len(h.slack.screens) == before + 1
    assert "Access unavailable" in text(h, list(h.slack.screens)[-1])
