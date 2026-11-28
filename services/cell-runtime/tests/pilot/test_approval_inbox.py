"""An opted-in channel gets one generic button. Only inspected evidence permits a handoff."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import approval_inbox as inbox
from opentour_cell.pilot import open_weight, release_gate, repair_workflow, source_intake
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.managed_run import plan_revision
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.slack_adapter import Refused
from opentour_cell.pilot.training import LocalTrainingStore
from test_autonomous_cycle import Customer, read, save
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


# --- OPE-577: authority withdrawn between enqueue_ready's authority read and its write. ---
#
# Generated cycle customers only. The withdrawal is injected deterministically through the
# pointer write itself (`approval_inbox._record`), which runs after enqueue_ready's authority
# read. Mandate withdrawal takes only the mandate database, so it commits right there.
# Engagement withdrawal serializes behind the engagement guard, so it is requested there and
# commits as soon as the guard is released, before enqueue_ready's fresh re-read.

HELD = "approval_authority_withdrawn"


def generated_trainer(
    bundle: Path,
    model_dir: Any,
    grant: Json,
    python: Any,
    request: Json,
    *,
    check_permission: Any,
    dispatch: Any,
) -> Json:
    """Generated numeric trainer transport. No model base, subprocess or GPU."""
    check_permission()
    with dispatch():
        if request["operation"] == "inspect_installation":
            return {
                "profile": open_weight.PROFILE,
                "checkpoint_revision": open_weight.REVISION,
                "verified_base_sha256": open_weight.BASE_SHA256,
                "verified_tokenizer_sha256": open_weight.TOKENIZER_SHA256,
            }
        if request["operation"] == "predict":
            return {
                "decisions": [
                    {
                        "status": "automatic",
                        "label": "refund" if "returned" in row["description"] else "sale",
                    }
                    for row in request["examples"]
                ]
            }
        assert request["operation"] == "fit"
        return {"adapter": {"a": "YQ=="}, "pid": 1, "elapsed_ms": 1700}


def cycled(root: Path, monkeypatch: pytest.MonkeyPatch, *, model: bool = False) -> Customer:
    """A generated cycle customer whose one iteration reaches the review queue."""
    if model:
        monkeypatch.setattr(open_weight, "_run", generated_trainer)
    c = Customer(root, model=model)
    assert c.run()["state"] == "pending_slack_review"
    return c


def queued(
    root: Path, monkeypatch: pytest.MonkeyPatch, *, model: bool = False
) -> tuple[Customer, Json, Path]:
    """A real cycle reaches the queue once; its pointer is then cleared for a direct enqueue."""
    c = cycled(root, monkeypatch, model=model)
    (path,) = c.owner.glob("approval-ready-*.json")
    item = json.loads(path.read_text())
    path.unlink()
    return c, item, path


def enqueue(directory: Path, organization: str, item: Json) -> Json:
    return inbox.enqueue_ready(
        directory,
        organization,
        item["run_id"],
        run_revision=item["run_revision"],
        team=item["team"],
        channel=item["channel"],
        audience=item["audience"],
        reviewer=item["reviewer"],
        provider=item.get("provider"),
    )


def between_check_and_write(monkeypatch: pytest.MonkeyPatch, action: Any) -> list[str]:
    """Run `action` after enqueue_ready's authority read, just before its pointer write."""
    write, fired = inbox._record, []

    def seam(path: Path, value: Json, **kwargs: Any) -> None:
        if path.name.startswith("approval-ready-") and not fired:
            fired.append(path.name)
            action()
        write(path, value, **kwargs)

    monkeypatch.setattr(inbox, "_record", seam)
    return fired


def undeletable(target: Path) -> None:
    raise PermissionError(target.name)


def withdraw_mandate(c: Customer) -> None:
    assert c.ledger.withdraw(c.mandate, actor="generated-owner")["withdrawn"] is True


def mandate_withdrawn(c: Customer) -> bool:
    return bool(LearningMandates(c.owner, c.org, c.eng).inspect(c.mandate)["withdrawn"])


def engagement_withdrawn(directory: Path) -> bool:
    uri = (directory / "training.sqlite3").as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        row = db.execute("SELECT withdrawn_at FROM engagement WHERE singleton=1").fetchone()
    return row[0] is not None


@pytest.mark.parametrize("model", (False, True), ids=("rule", "model"))
def test_mandate_withdrawn_inside_enqueue_removes_pointer_and_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: bool
) -> None:
    c, item, path = queued(tmp_path, monkeypatch, model=model)
    fired = between_check_and_write(monkeypatch, lambda: withdraw_mandate(c))
    result = enqueue(c.owner, c.org, item)
    assert fired == [path.name] and mandate_withdrawn(c)
    assert result.get("state") == "held" and result.get("reason") == HELD, result
    assert result["pointer"] == "removed"
    assert not list(c.owner.glob("approval-ready-*.json"))
    assert not list(c.owner.glob("approval-held-*.json"))


def manual_queued(root: Path) -> tuple[Path, str, Json, Path]:
    """A managed run outside any standing mandate: only the engagement governs it."""
    h, _, _, receipt, _ = stage(root)
    directory, org = h.path.parent, h.store.organization
    path = directory / ("approval-ready-" + receipt + ".json")
    item = json.loads(path.read_text())
    path.unlink()
    assert not list(directory.glob("cycle-plan-*.json"))
    return directory, org, item, path


@pytest.mark.parametrize("setup", ("cycle", "manual"))
def test_engagement_withdrawn_inside_enqueue_removes_pointer_and_holds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setup: str
) -> None:
    if setup == "cycle":
        c, item, path = queued(tmp_path, monkeypatch)
        directory, org = c.owner, c.org
    else:
        directory, org, item, path = manual_queued(tmp_path)
    store = LocalTrainingStore(directory / "training.sqlite3", org, existing_only=True)
    worker = threading.Thread(target=store.withdraw, daemon=True)
    blocked: list[bool] = []

    def request_withdrawal() -> None:
        worker.start()
        worker.join(0.3)
        # The engagement guard is held across the write, so the withdrawal waits for it.
        blocked.append(worker.is_alive() and not engagement_withdrawn(directory))

    fired = between_check_and_write(monkeypatch, request_withdrawal)
    recheck = getattr(inbox, "_authority_withdrawn", None)

    def committed_first(*args: Any) -> bool:
        # Deterministic: the re-read starts only once the released withdrawal has committed.
        worker.join(15)
        assert not worker.is_alive() and recheck is not None
        return bool(recheck(*args))

    monkeypatch.setattr(inbox, "_authority_withdrawn", committed_first, raising=False)
    result = enqueue(directory, org, item)
    worker.join(15)
    assert fired == [path.name] and blocked == [True] and engagement_withdrawn(directory)
    assert result.get("state") == "held" and result.get("reason") == HELD, result
    assert result["pointer"] == "removed"
    assert not list(directory.glob("approval-ready-*.json"))
    assert not list(directory.glob("approval-held-*.json"))


def test_engagement_rebound_inside_enqueue_counts_as_withdrawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fresh re-read pins the engagement identity, not only its withdrawal flag."""
    directory, org, item, path = manual_queued(tmp_path)
    (review,) = directory.glob("managed-outputs/run-*/review.json")
    between_check_and_write(
        monkeypatch, lambda: save(review, dict(read(review), engagement="generated-other"))
    )
    result = enqueue(directory, org, item)
    assert not engagement_withdrawn(directory)
    assert result.get("state") == "held" and result.get("reason") == HELD, result
    assert not path.exists()


def test_inconsistent_standing_plan_counts_as_withdrawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The re-read cannot confirm which mandate governs this run, so the pointer is removed."""
    c, item, path = queued(tmp_path, monkeypatch)
    (plan,) = c.owner.glob("cycle-plan-*.json")
    save(plan, dict(read(plan), unbound="generated"))
    result = enqueue(c.owner, c.org, item)
    assert not mandate_withdrawn(c) and not engagement_withdrawn(c.owner)
    assert result.get("state") == "held" and result.get("reason") == HELD, result
    assert not path.exists()


@pytest.mark.parametrize("model", (False, True), ids=("rule", "model"))
def test_unwithdrawn_enqueue_is_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: bool
) -> None:
    c, item, path = queued(tmp_path, monkeypatch, model=model)
    fired = between_check_and_write(monkeypatch, lambda: None)
    receipt = path.stem.removeprefix("approval-ready-")
    assert enqueue(c.owner, c.org, item) == {"receipt": receipt, "state": "pending_review"}
    assert fired == [path.name] and json.loads(path.read_text()) == item
    assert not list(c.owner.glob("approval-held-*.json"))
    # Idempotent: the identical pointer is accepted again.
    assert (
        enqueue(c.owner, c.org, item) == {"receipt": receipt, "state": "pending_review"}
        and path.exists()
    )


@pytest.mark.parametrize("error", (PermissionError, RuntimeError), ids=("os", "unexpected"))
@pytest.mark.parametrize("marker", ("written", "unwritable"))
def test_failed_pointer_removal_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str, error: type[Exception]
) -> None:
    c, item, path = queued(tmp_path, monkeypatch)
    between_check_and_write(monkeypatch, lambda: withdraw_mandate(c))

    def undeletable_by(target: Path) -> None:
        raise error(target.name)

    monkeypatch.setattr(inbox, "_discard", undeletable_by, raising=False)
    if marker == "unwritable":
        write = inbox._record

        def refuse_marker(target: Path, value: Json, **kwargs: Any) -> None:
            if target.name.startswith("approval-held-"):
                raise error(target.name)
            write(target, value, **kwargs)

        monkeypatch.setattr(inbox, "_record", refuse_marker)
    result = enqueue(c.owner, c.org, item)
    # Never reported as queued, whatever cleanup managed to do.
    assert result.get("state") == "held" and result.get("reason") == HELD, result
    held = c.owner / ("approval-held-" + path.stem.removeprefix("approval-ready-") + ".json")
    if marker == "written":
        assert result["pointer"] == "marked" and path.exists()
        receipt = path.stem.removeprefix("approval-ready-")
        assert json.loads(held.read_text()) == {
            "receipt": receipt,
            "reason": HELD,
            "pointer_digest": digest(item),
        }
        # Custody inventories the marker; it carries no Slack or run identifiers.
        text_ = held.read_text()
        for private in (
            item["organization"],
            item["run_id"],
            item["team"],
            item["channel"],
            item["audience"],
            item["reviewer"],
        ):
            assert private not in text_
    else:
        assert result["pointer"] == "unremoved" and path.exists() and not held.exists()


def cycle_inbox(c: Customer, root: Path) -> Harness:
    """A generated Slack workbench opted in to approval requests for this cycle customer."""
    (queued_path,) = c.owner.glob("approval-ready-*.json")
    item = json.loads(queued_path.read_text())
    evidence = repair_workflow.managed_review_evidence(
        c.owner, c.org, item["run_id"], item["audience"], item["team"], item["channel"]
    )
    h = Harness(root / "generated-ui", organization=c.org)
    h.path = c.owner / "slack-config.json"
    role = deepcopy(h.config["users"][ACTOR])
    role["datasets"] = [evidence["dataset_id"]]
    h.config.update(
        directory=str(c.owner),
        reviewer=item["reviewer"],
        recipient=item["audience"],
        team=item["team"],
        channel=item["channel"],
        users={item["reviewer"]: role},
        zapier=False,
        approval_provider=item["provider"],
        approval_requests=True,
    )
    h.slack.channel["id"] = item["channel"]
    h.slack.members = [item["reviewer"]]
    h.store = LocalTrainingStore(c.owner / "training.sqlite3", c.org)
    h.save()
    restart(h)
    return h


def posted(h: Harness) -> list[tuple[str, str]]:
    with h.app.journal.db() as db:
        return [(r[0], r[1]) for r in db.execute("SELECT receipt, state FROM approval_posts")]


@pytest.mark.parametrize("withdrawn", ("none", "before_flush", "inside_flush"))
@pytest.mark.parametrize("model", (False, True), ids=("rule", "model"))
def test_flush_rechecks_mandate_withdrawn_after_enqueue(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: bool, withdrawn: str
) -> None:
    c = cycled(tmp_path, monkeypatch, model=model)
    (path,) = c.owner.glob("approval-ready-*.json")
    receipt = path.stem.removeprefix("approval-ready-")
    h = cycle_inbox(c, tmp_path)
    if withdrawn == "none":
        # Control: the same workbench posts this exact pointer once.
        assert inbox.flush_approvals(h.app) == 1 and receipt in json.dumps(h.slack.post)
        assert posted(h) == [(receipt, "posted")]
        return
    if withdrawn == "before_flush":
        withdraw_mandate(c)
    else:
        # After flush_approvals' first evidence read and before its guarded re-check, so only
        # the re-check before posting can refuse it.
        channel, fired = h.app.private_channel, []

        def withdraw_then_check(*args: Any) -> None:
            if not fired:
                fired.append(True)
                withdraw_mandate(c)
            channel(*args)

        monkeypatch.setattr(h.app, "private_channel", withdraw_then_check)
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post
    assert mandate_withdrawn(c)
    assert posted(h) == []
    # Held: the pointer stays unposted on every later pass.
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post and path.exists()


def test_flush_refuses_held_marker_even_when_authority_reads_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = cycled(tmp_path, monkeypatch)
    (path,) = c.owner.glob("approval-ready-*.json")
    item = json.loads(path.read_text())
    receipt = path.stem.removeprefix("approval-ready-")
    held = c.owner / ("approval-held-" + receipt + ".json")
    h = cycle_inbox(c, tmp_path)
    # An unknown re-read counts as withdrawn; the pointer cannot be removed, so a marker is left.
    # The journal hold is unavailable here, so the marker is the only durable refusal.
    with monkeypatch.context() as m:
        m.setattr(inbox, "_authority_withdrawn", lambda *_: True, raising=False)
        m.setattr(inbox, "_discard", undeletable, raising=False)
        m.setattr(inbox, "_hold_in_journal", lambda *_: False, raising=False)
        first = enqueue(c.owner, c.org, item)
    assert first.get("state") == "held" and first.get("pointer") == "marked", first
    assert path.exists() and held.exists()
    # Authority now reads current, but the marker still refuses delivery and re-enqueue.
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post and posted(h) == []
    with monkeypatch.context() as m:
        m.setattr(inbox, "_discard", undeletable, raising=False)
        m.setattr(inbox, "_hold_in_journal", lambda *_: False, raising=False)
        assert enqueue(c.owner, c.org, item).get("state") == "held"
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post
    # Control: the marker was the only refusal.
    held.unlink()
    assert inbox.flush_approvals(h.app) == 1 and posted(h) == [(receipt, "posted")]


def after_claim(
    monkeypatch: pytest.MonkeyPatch, h: Harness, receipt: str, action: Any
) -> list[bool]:
    """Run `action` as soon as flush_approvals' claim transaction has committed."""
    real, fired = h.app.journal.db, []

    @contextmanager
    def db(*args: Any, **kwargs: Any) -> Iterator[sqlite3.Connection]:
        with real(*args, **kwargs) as conn:
            yield conn
        if not fired:
            with real() as check:
                row = check.execute(
                    "SELECT state FROM approval_posts WHERE receipt=?", (receipt,)
                ).fetchone()
            if row is not None and row[0] == "claimed":
                fired.append(True)
                action()

    monkeypatch.setattr(h.app.journal, "db", db)
    return fired


@pytest.mark.parametrize("kind", ("mandate", "engagement"))
@pytest.mark.parametrize("model", (False, True), ids=("rule", "model"))
def test_flush_rechecks_authority_withdrawn_after_claim_before_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, model: bool, kind: str
) -> None:
    c = cycled(tmp_path, monkeypatch, model=model)
    (path,) = c.owner.glob("approval-ready-*.json")
    receipt = path.stem.removeprefix("approval-ready-")
    h = cycle_inbox(c, tmp_path)
    if kind == "mandate":
        # Takes only the mandate database, so it commits while the evidence guard is held.
        fired = after_claim(monkeypatch, h, receipt, lambda: withdraw_mandate(c))
    else:
        store = LocalTrainingStore(c.owner / "training.sqlite3", c.org, existing_only=True)
        worker = threading.Thread(target=store.withdraw, daemon=True)
        fired = after_claim(monkeypatch, h, receipt, worker.start)
        recheck = getattr(inbox, "_authority_withdrawn", None)

        def committed_first(*args: Any) -> bool:
            # Deterministic: the guard is released, and the withdrawal commits, before the read.
            worker.join(15)
            assert not worker.is_alive() and recheck is not None
            return bool(recheck(*args))

        monkeypatch.setattr(inbox, "_authority_withdrawn", committed_first, raising=False)
    sent = inbox.flush_approvals(h.app)
    if kind == "engagement":
        worker.join(15)
        assert engagement_withdrawn(c.owner)
    else:
        assert mandate_withdrawn(c)
    assert fired == [True]
    assert sent == 0 and not h.slack.post
    # The claim is released as held: never retried, never reported as possibly sent.
    assert posted(h) == [(receipt, "held")]
    with h.app.journal.db() as db:
        statuses = [
            r[0]
            for r in db.execute("SELECT status FROM exposures WHERE kind=?", ("approval_request",))
        ]
    assert statuses == ["withheld"]
    if kind == "mandate":
        assert inbox.flush_approvals(h.app) == 0 and not h.slack.post
    else:
        # A withdrawn engagement refuses the whole workbench on every later pass.
        with pytest.raises(Refused):
            inbox.flush_approvals(h.app)
        assert not h.slack.post


@pytest.mark.parametrize("withdrawn", (False, True), ids=("current", "withdrawn"))
def test_click_after_post_rechecks_withdrawn_mandate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, withdrawn: bool
) -> None:
    """Residual window: a post that already went out is inert once the mandate is withdrawn."""
    c = cycled(tmp_path, monkeypatch)
    (path,) = c.owner.glob("approval-ready-*.json")
    item = json.loads(path.read_text())
    receipt = path.stem.removeprefix("approval-ready-")
    h = cycle_inbox(c, tmp_path)
    assert inbox.flush_approvals(h.app) == 1 and posted(h) == [(receipt, "posted")]
    with h.app.journal.db() as db:
        (revision,) = db.execute(
            "SELECT revision FROM approval_posts WHERE receipt=?", (receipt,)
        ).fetchone()
    reviewer = item["reviewer"]
    h.send("interactive", callback(h, receipt, actor=reviewer))
    opened = text(h, list(h.slack.screens)[-1])
    assert "Access unavailable" not in opened
    evidence = repair_workflow.managed_review_evidence(
        c.owner, c.org, item["run_id"], item["audience"], item["team"], item["channel"]
    )
    decision = {
        "run_id": item["run_id"],
        "run_revision": item["run_revision"],
        "artifact_sha": evidence["audit"]["candidate_digest"],
        "reviewer": reviewer,
        "team": item["team"],
        "channel": item["channel"],
        "audience": item["audience"],
        "decision": "approved",
        "inspection_ref": revision,
        "provider": item["provider"],
        # The plugin bridge supplies its sealed request deadline; generated here.
        "provider_request_expires_at": int(time.time()) + 30,
    }
    decided = c.owner / "managed-outputs" / item["run_id"] / "release-decision.json"
    if not withdrawn:
        # Control: the same decision records while authority is current.
        assert release_gate.record_release(c.owner, c.org, **decision)["decision"] == "approved"
        assert decided.exists()
        return
    withdraw_mandate(c)
    # Clicking the already-posted button again opens nothing.
    before = len(h.slack.screens)
    h.send("interactive", callback(h, receipt, actor=reviewer))
    assert len(h.slack.screens) == before + 1
    assert "Access unavailable" in text(h, list(h.slack.screens)[-1])
    # Recording the approval re-checks the authority at decision time and refuses.
    with pytest.raises(ValueError, match="^standing_mandate_withdrawn$"):
        release_gate.record_release(c.owner, c.org, **decision)
    assert not decided.exists()


# --- OPE-577 review round: closeout custody, unexpected errors, missing plan, journal hold. ---

CLOSEOUT = {
    "agreement": "generated-terms",
    "custodian": "generated-custodian",
    "support": "generated-support",
    "due": "2040-04-01",
    "trigger": "withdrawal",
}


def closeout_items(directory: Path, organization: str) -> set[str]:
    from opentour_cell.pilot.custody import Closeout

    report = Closeout(directory, organization).withdraw(**CLOSEOUT)
    return {item["item"] for item in report["items"]}


@pytest.mark.parametrize("marked", (False, True), ids=("no_marker", "orphan_marker"))
def test_closeout_inventory_lists_an_orphan_held_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marked: bool
) -> None:
    c, item, path = queued(tmp_path, monkeypatch)
    if marked:
        # An unknown re-read and a failed removal leave a marker; a later pass then removes
        # the pointer, so only the marker remains.
        with monkeypatch.context() as m:
            m.setattr(inbox, "_authority_withdrawn", lambda *_: True, raising=False)
            m.setattr(inbox, "_discard", undeletable, raising=False)
            assert enqueue(c.owner, c.org, item).get("pointer") == "marked"
        path.unlink()
        assert list(c.owner.glob("approval-held-*.json"))
    assert not list(c.owner.glob("approval-ready-*.json"))
    assert ("approval-requests" in closeout_items(c.owner, c.org)) is marked


@pytest.mark.parametrize(
    "fault",
    ("runtime", "attribute", "type", "sqlite", "missing_run", "missing_review"),
)
def test_unexpected_recheck_errors_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    """Whatever the fresh re-read raises after the write, the pointer goes and the run holds."""
    c, item, path = queued(tmp_path, monkeypatch)
    errors: dict[str, type[Exception]] = {
        "runtime": RuntimeError,
        "attribute": AttributeError,
        "type": TypeError,
        "sqlite": sqlite3.OperationalError,
    }
    work = c.owner / "managed-outputs" / item["run_id"]
    original = LearningMandates.check_run
    armed: list[bool] = []

    def check_run(self: LearningMandates, *args: Any, **kwargs: Any) -> Json:
        if armed:
            raise errors[fault]("generated")
        return dict(original(self, *args, **kwargs))

    def inject() -> None:
        if fault in errors:
            armed.append(True)
        else:
            (work / ("run.json" if fault == "missing_run" else "review.json")).unlink()

    monkeypatch.setattr(LearningMandates, "check_run", check_run)
    between_check_and_write(monkeypatch, inject)
    result = enqueue(c.owner, c.org, item)
    assert result.get("state") == "held" and result.get("reason") == HELD, result
    assert not path.exists() and not mandate_withdrawn(c)


def test_missing_standing_plan_counts_as_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The sealed intake shows a standing mandate governs this run, so its plan is required."""
    c, item, path = queued(tmp_path, monkeypatch)
    (plan,) = c.owner.glob("cycle-plan-*.json")
    between_check_and_write(monkeypatch, plan.unlink)
    result = enqueue(c.owner, c.org, item)
    assert not mandate_withdrawn(c) and not engagement_withdrawn(c.owner)
    assert result.get("state") == "held" and result.get("reason") == HELD, result
    assert not path.exists()


def test_manual_run_without_standing_plan_is_unchanged(tmp_path: Path) -> None:
    """Control: a sealed intake with no delegation is a manual run and needs no plan."""
    directory, org, item, path = manual_queued(tmp_path)
    receipt = path.stem.removeprefix("approval-ready-")
    assert enqueue(directory, org, item) == {"receipt": receipt, "state": "pending_review"}
    assert path.exists()
    # A stray standing plan beside a manual run is inconsistent, so the run holds.
    save(directory / ("cycle-plan-" + item["run_id"] + ".json"), {"generated": "stray"})
    assert enqueue(directory, org, item).get("state") == "held" and not path.exists()


def test_missing_standing_plan_after_claim_is_not_posted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = cycled(tmp_path, monkeypatch)
    (path,) = c.owner.glob("approval-ready-*.json")
    receipt = path.stem.removeprefix("approval-ready-")
    h = cycle_inbox(c, tmp_path)
    (plan,) = c.owner.glob("cycle-plan-*.json")
    fired = after_claim(monkeypatch, h, receipt, plan.unlink)
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post and fired == [True]
    assert posted(h) == [(receipt, "held")]


def test_journal_hold_refuses_delivery_without_a_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = cycled(tmp_path, monkeypatch)
    (path,) = c.owner.glob("approval-ready-*.json")
    item = json.loads(path.read_text())
    receipt = path.stem.removeprefix("approval-ready-")
    h = cycle_inbox(c, tmp_path)
    path.unlink()
    write = inbox._record

    def refuse_marker(target: Path, value: Json, **kwargs: Any) -> None:
        if target.name.startswith("approval-held-"):
            raise PermissionError(target.name)
        write(target, value, **kwargs)

    # Unknown re-read, failed removal and failed marker write: only the journal hold remains.
    with monkeypatch.context() as m:
        m.setattr(inbox, "_authority_withdrawn", lambda *_: True, raising=False)
        m.setattr(inbox, "_discard", undeletable, raising=False)
        m.setattr(inbox, "_record", refuse_marker)
        first = enqueue(c.owner, c.org, item)
    assert first.get("state") == "held" and first.get("pointer") == "marked", first
    assert path.exists() and not list(c.owner.glob("approval-held-*.json"))
    assert posted(h) == [(receipt, "held")]
    # Authority reads current again, but flush refuses and a re-enqueue reports held.
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post
    with monkeypatch.context() as m:
        m.setattr(inbox, "_discard", undeletable, raising=False)
        m.setattr(inbox, "_record", refuse_marker)
        assert enqueue(c.owner, c.org, item).get("state") == "held"
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post
    assert not list(c.owner.glob("approval-held-*.json"))
    # Control: the journal hold was the only refusal.
    with h.app.journal.db() as db:
        db.execute("DELETE FROM approval_posts WHERE receipt=?", (receipt,))
    assert inbox.flush_approvals(h.app) == 1 and posted(h) == [(receipt, "posted")]


def test_marker_written_after_claim_releases_the_claim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    c = cycled(tmp_path, monkeypatch)
    (path,) = c.owner.glob("approval-ready-*.json")
    receipt = path.stem.removeprefix("approval-ready-")
    h = cycle_inbox(c, tmp_path)
    held = c.owner / ("approval-held-" + receipt + ".json")
    fired = after_claim(monkeypatch, h, receipt, lambda: save(held, {"receipt": receipt}))
    assert inbox.flush_approvals(h.app) == 0 and not h.slack.post and fired == [True]
    assert posted(h) == [(receipt, "held")] and not mandate_withdrawn(c)


@pytest.mark.parametrize("point", ("before_claim", "after_claim"))
def test_unexpected_flush_error_fails_closed_per_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, point: str
) -> None:
    c = cycled(tmp_path, monkeypatch)
    (path,) = c.owner.glob("approval-ready-*.json")
    receipt = path.stem.removeprefix("approval-ready-")
    h = cycle_inbox(c, tmp_path)
    # A generated receipt that sorts before (or after) the real one; it never reaches Slack.
    other = "approval-" + ("0" if point == "before_claim" else "f") * 40
    save(c.owner / ("approval-ready-" + other + ".json"), {"generated": "unreadable"})
    target, seen = inbox._target, []

    def checked(app: Any, cfg: Any, value: str) -> Any:
        seen.append(value)
        if value == other:
            raise RuntimeError("generated")
        return target(app, cfg, value)

    monkeypatch.setattr(inbox, "_target", checked)
    if point == "after_claim":

        def broken(*_: Any) -> bool:
            raise RuntimeError("generated")

        monkeypatch.setattr(inbox, "_authority_withdrawn", broken, raising=False)
    sent = inbox.flush_approvals(h.app)
    # The pass continued past the failing receipt to every other receipt.
    assert sorted(seen) == sorted([receipt, other])
    if point == "before_claim":
        assert sent == 1 and posted(h) == [(receipt, "posted")]
    else:
        # Our claim is released as held, never left claimed or posted.
        assert sent == 0 and not h.slack.post and posted(h) == [(receipt, "held")]
        with h.app.journal.db() as db:
            statuses = [
                r[0]
                for r in db.execute("SELECT status FROM exposures WHERE kind='approval_request'")
            ]
        assert statuses == ["withheld"]


def test_unreadable_journal_hold_counts_as_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A journal whose hold cannot be read is unknown, so the pointer is not left deliverable."""
    c, item, path = queued(tmp_path, monkeypatch)
    journal = c.owner / "slack-pilot.sqlite3"
    journal.write_bytes(b"generated, not a database\n" * 64)
    journal.chmod(0o600)
    result = enqueue(c.owner, c.org, item)
    assert result.get("state") == "held" and result.get("reason") == HELD, result
    assert not path.exists() and not mandate_withdrawn(c)
