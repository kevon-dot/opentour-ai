"""Generated authority and exchange fixtures for the installed Slack-to-reservation contract."""

import hashlib
import json
import sys
import time
from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from opentour_cell.pilot import eve_learning_service as eve
from opentour_cell.pilot.eve_exchange import digest
from opentour_cell.pilot.slack_adapter import Journal, _json
from opentour_cell.pilot.slack_learning import LearningScope

pytest_plugins = ("test_home_readiness",)

START = "learning_start_" + "a" * 26
ACTION = "learning_control_" + "b" * 26
NOW = 1_900_000_000


class Authority:
    def __init__(self):
        self.scope = LearningScope("tenant", "epoch", "TGENERATED", "UREVIEW", "wf", "ds", "r1")
        self.grant = SimpleNamespace(
            plan_ref="plan-a",
            reservation_ref="budget-a",
            benchmark_ref="bench-a",
            max_cents=12,
            reviewer=self.scope.actor,
            can_control=True,
            spec={
                "budget": 12,
                "grant": "résumé",
                "reviewer": self.scope.actor,
                "reservation_ref": "budget-a",
                "steps": 16,
                "starts_at": NOW - 10,
                "deadline": NOW + 300,
            },
        )
        self.guards = []
        self.claim_calls = []
        self.claimed = {}
        self.active = True
        self.lock = False

    def claim(self, grant):
        assert self.lock and grant is self.grant
        self.claim_calls.append(grant.reservation_ref)
        version = digest(grant.spec)
        if self.claimed.setdefault(grant.reservation_ref, version) != version:
            raise ValueError("reservation_version_changed")

    @contextmanager
    def current(self, scope, *, write):
        self.guards.append((scope.actor, write))
        if not self.active or scope not in (self.scope, replace(self.scope, actor="UREADER")):
            raise PermissionError("denied")
        assert not self.lock
        self.lock = True
        try:
            yield self.grant
        finally:
            self.lock = False


class Exchange:
    def __init__(self, auth):
        self.auth = auth
        self.row = None
        self.decision = None
        self.starts = []
        self.actions = []

    def get(self, reservation):
        assert self.auth.lock and reservation == self.auth.grant.reservation_ref
        return self.row

    def admit(self, reservation, spec, *, actor, decision_key):
        assert self.auth.lock and self.auth.claimed[reservation] == digest(spec)
        if self.row is not None:
            if self.decision != (actor, decision_key):
                raise ValueError("decision_conflict")
            return self.row
        self.decision = (actor, decision_key)
        self.starts.append(self.decision)
        self.row = dict(
            run_ref="eve-run",
            state="queued",
            revision="revision-1",
            completed_steps=0,
            total_steps=16,
            checkpoint_step=None,
            active_seconds=0.0,
            heartbeat=0,
            deadline=NOW + 300,
            permit_until=NOW + 30,
            reservation_ref=reservation,
            spec_digest=hashlib.sha256(
                json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
            ).hexdigest(),
        )
        return self.row

    def control(self, reservation, action, revision, key):
        assert self.auth.lock and revision == self.row["revision"]
        self.actions.append((reservation, action, revision, key))
        self.row.update(
            revision="revision-next",
            state={"pause": "pausing", "resume": "resuming", "stop": "stopping"}[action],
        )
        return self.row


@pytest.fixture
def installed(monkeypatch, tmp_path):
    monkeypatch.setattr(eve.time, "time", lambda: NOW)
    authority = Authority()
    exchange = Exchange(authority)
    directory = tmp_path / "worker"
    directory.mkdir(mode=0o700)
    scope = authority.scope
    authority.journal = Journal(
        directory / "slack-pilot.sqlite3",
        _json([scope.organization, scope.workspace, "AAPP", "CROOM", str(directory)]),
    )
    return eve.EveLearningService(authority, exchange, directory, "CROOM"), authority, exchange


def admit(installed):
    service, auth, _ = installed
    operation = "evaluate_and_improve"
    key = auth.journal.opaque(
        "learning_start",
        [auth.scope.__dict__, auth.grant.plan_ref, operation, digest(auth.grant.spec)],
    )
    return service.start(auth.scope, auth.grant.plan_ref, operation, key)


def control_key(auth, receipt, action):
    run = auth.journal.opaque("learning_run", [auth.scope.__dict__, receipt.run_ref])
    version = auth.journal.opaque(
        "control", [run, auth.grant.plan_ref, receipt.revision, digest(auth.grant.spec)]
    )
    return auth.journal.opaque("learning_control", [run + "." + version, action])


def test_explicit_scoped_admission_replay_and_fresh_queued_stop(installed):
    service, auth, ex = installed
    plan = service.plan(auth.scope)
    assert plan.can_improve and not plan.can_benchmark and plan.max_fits == 1
    assert plan.model_route == "eve-24b-qlora" and plan.mandate_ref == "budget-a"
    assert plan.spend_cap_cents == 12 and not ex.starts
    first = admit(installed)
    assert first.state == "queued" and first.stale and not first.controls()
    assert first.completed_steps is None and first.active_seconds is None
    assert admit(installed) == first and len(ex.starts) == 1
    assert auth.claim_calls == [auth.grant.reservation_ref] * 2
    assert auth.claimed == {auth.grant.reservation_ref: digest(auth.grant.spec)}
    with pytest.raises(ValueError):
        service.start(
            auth.scope, plan.plan_ref, "evaluate_and_improve", "learning_start_" + "c" * 26
        )
    assert service.plan(auth.scope).current_run == first.run_ref
    assert not service.plan(auth.scope).can_improve
    ex.row["heartbeat"] = NOW
    status = service.status(auth.scope, first.run_ref)
    assert status.controls() == ("stop",)
    key = control_key(auth, status, "stop")
    pending = service.control(auth.scope, first.run_ref, "stop", status.revision, key)
    assert pending.state == "stopping" and not pending.controls()
    with pytest.raises(ValueError):
        service.control(auth.scope, first.run_ref, "stop", status.revision, key)
    assert len(ex.starts) == len(ex.actions) == 1


def test_retry_after_claim_before_exchange_admission_reuses_reservation(installed, monkeypatch):
    _, auth, ex = installed
    with monkeypatch.context() as patch:

        def unavailable(*args, **kwargs):
            raise OSError("generated interruption before admission")

        patch.setattr(ex, "admit", unavailable)
        with pytest.raises(OSError):
            admit(installed)
    assert len(auth.claimed) == 1 and not ex.starts
    receipt = admit(installed)
    assert receipt.state == "queued" and len(ex.starts) == 1 and len(auth.claimed) == 1


def test_cancel_benchmark_and_forged_or_foreign_start_never_admit(installed):
    service, auth, ex = installed
    for actor, plan, operation, key in (
        (auth.scope.actor, "foreign", "evaluate_and_improve", START),
        (auth.scope.actor, "plan-a", "benchmark_only", START),
        (auth.scope.actor, "plan-a", "evaluate_and_improve", "forged"),
        ("UREADER", "plan-a", "evaluate_and_improve", START),
        ("UOTHER", "plan-a", "evaluate_and_improve", START),
    ):
        with pytest.raises((PermissionError, ValueError)):
            service.start(replace(auth.scope, actor=actor), plan, operation, key)
    with pytest.raises(ValueError):
        service.cancel(auth.scope, "eve-run")
    assert not ex.starts and not ex.actions
    assert service.plan(replace(auth.scope, actor="UOTHER")) is None


def test_status_and_control_only_use_current_scoped_receipts(installed):
    service, auth, ex = installed
    admit(installed)
    ex.row.update(state="running", heartbeat=NOW - 45, completed_steps=3, active_seconds=7.75)
    current = service.status(auth.scope, "eve-run")
    assert current.completed_steps == 3 and current.active_seconds == 7
    assert current.controls() == ("pause", "stop")
    reader = replace(auth.scope, actor="UREADER")
    assert service.status(reader, current.run_ref).controls() == ()
    for actor, run, rev, key in (
        (reader, current.run_ref, current.revision, ACTION),
        (auth.scope, "other-run", current.revision, ACTION),
        (auth.scope, current.run_ref, "older", ACTION),
        (auth.scope, current.run_ref, current.revision, "forged"),
    ):
        with pytest.raises(ValueError):
            service.control(actor, run, "pause", rev, key)
    ex.row["heartbeat"] = NOW - 46
    stale = service.status(auth.scope, "eve-run")
    assert stale.stale and stale.completed_steps == 3 and not stale.controls()
    ex.row.update(state="paused", checkpoint_step=3)
    assert not service.status(auth.scope, "eve-run").controls()
    with pytest.raises(ValueError):
        service.control(auth.scope, "eve-run", "resume", current.revision, ACTION)
    ex.row["heartbeat"] = NOW
    assert service.status(auth.scope, "eve-run").controls() == ("resume", "stop")
    ex.row["state"] = "completed"
    ex.row["heartbeat"] = NOW - 200
    finished = service.status(auth.scope, "eve-run")
    assert not finished.stale and not finished.controls() and not ex.actions


@pytest.mark.parametrize(
    "change", ["grant_expired", "spec_changed", "wrong_reservation", "unknown_state"]
)
def test_expiry_and_mismatched_publication_fail_closed(installed, change):
    service, auth, ex = installed
    admit(installed)
    ex.row.update(state="running", heartbeat=NOW)
    if change == "grant_expired":
        auth.active = False
    if change == "spec_changed":
        auth.grant.spec = {**auth.grant.spec, "budget": 999}
    if change == "wrong_reservation":
        ex.row["reservation_ref"] = "unrelated"
    if change == "unknown_state":
        ex.row["state"] = "release_approved"
    assert service.plan(auth.scope) is None
    for attempt in (
        lambda: service.status(auth.scope, "eve-run"),
        lambda: service.control(auth.scope, "eve-run", "pause", "revision-1", ACTION),
        lambda: admit(installed),
    ):
        with pytest.raises((ValueError, PermissionError)):
            attempt()
    assert not ex.actions and len(ex.starts) == 1


def test_factory_opens_only_configured_customer_path(tmp_path, monkeypatch):
    factory_calls = []
    auth = SimpleNamespace(configured=False)

    def open_authority(*args):
        factory_calls.append(args)
        return auth

    def open_exchange(path):
        factory_calls.append(path)
        return object()

    monkeypatch.setitem(
        sys.modules,
        "opentour_cell.pilot.eve_authority",
        SimpleNamespace(EveAuthority=open_authority),
    )
    monkeypatch.setitem(
        sys.modules, "opentour_cell.pilot.eve_exchange", SimpleNamespace(Exchange=open_exchange)
    )
    args = (tmp_path, "tenant", "TGENERATED", "CPRIVATE")
    assert eve.open_eve_learning_service(*args) is None
    assert factory_calls == [args]
    auth.configured = True
    assert eve.open_eve_learning_service(*args).authority is auth
    assert factory_calls == [args, args, tmp_path / "eve-exchange"]


def test_read_only_journal_rejects_correct_shape_but_unissued_click_and_withdrawal(installed):
    service, auth, ex = installed
    with pytest.raises(ValueError):
        service.start(auth.scope, auth.grant.plan_ref, "evaluate_and_improve", START)
    assert not ex.starts and not auth.claim_calls
    receipt = admit(installed)
    ex.row.update(heartbeat=NOW, state="running")
    status = service.status(auth.scope, receipt.run_ref)
    good = control_key(auth, status, "pause")
    for key in (ACTION, control_key(auth, status, "stop")):
        with pytest.raises(ValueError):
            service.control(auth.scope, receipt.run_ref, "pause", status.revision, key)
    with auth.journal.db() as db:
        db.execute("UPDATE journal_control SET held=1 WHERE id=1")
    with pytest.raises(ValueError):
        service.control(auth.scope, receipt.run_ref, "pause", status.revision, good)
    assert not ex.actions


def test_real_exchange_reuses_reservation_and_applies_versioned_pause(installed):
    from opentour_cell.pilot.eve_exchange import Exchange as LocalExchange

    service, auth, _ = installed
    exchange = LocalExchange(service.directory / "eve-exchange")
    service.exchange = exchange
    first = admit(installed)
    assert admit(installed) == first
    row = exchange.get(auth.grant.reservation_ref)
    exchange.observe(row, "running", step=2, seconds=5)
    running = service.status(auth.scope, first.run_ref)
    key = control_key(auth, running, "pause")
    pending = service.control(auth.scope, first.run_ref, "pause", running.revision, key)
    assert pending.state == "pausing" and pending.checkpoint_step is None
    with pytest.raises(ValueError):
        service.control(auth.scope, first.run_ref, "pause", running.revision, key)
    row = exchange.get(auth.grant.reservation_ref)
    exchange.observe(row, "paused", step=3, checkpoint=3, seconds=8)
    confirmed = service.status(auth.scope, first.run_ref)
    assert confirmed.checkpoint_step == 3 and confirmed.active_seconds == 8
    with exchange.transaction() as db:
        assert db.execute("SELECT count(*) FROM eve_jobs").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM eve_controls").fetchone()[0] == 1


def test_same_plan_with_replacement_spend_mandate_requires_new_click(installed):
    service, auth, exchange = installed
    original = service.plan(auth.scope)
    key = auth.journal.opaque(
        "learning_start",
        [
            auth.scope.__dict__,
            original.plan_ref,
            "evaluate_and_improve",
            original.approval_revision,
        ],
    )
    auth.grant.spec = {**auth.grant.spec, "budget": 100}
    replacement = service.plan(auth.scope)
    assert (
        original.plan_ref == replacement.plan_ref
        and original.approval_revision != replacement.approval_revision
    )
    with pytest.raises(ValueError):
        service.start(auth.scope, replacement.plan_ref, "evaluate_and_improve", key)
    assert not exchange.starts


def _exercise_private_card(ready, monkeypatch):
    from opentour_cell.pilot.eve_exchange import Exchange as LocalExchange
    from test_app_home import click
    from test_reviewer_workflow import buttons, screen
    from test_slack_eve_controls import content, press_payload

    h = ready.h
    h.config.update(channel_aggregates=True, learning_thread_ts="100.001")
    h.save()
    cfg = h.app.settings()
    auth = Authority()
    actor = cfg.reviewer
    auth.scope = LearningScope(
        cfg.organization,
        h.store.engagement_id,
        cfg.team,
        actor,
        ready.workflow,
        ready.grant["dataset_ref"],
        ready.grant["dataset_revision"],
    )
    now = int(time.time())
    auth.grant.reviewer = actor
    auth.grant.spec.update(reviewer=actor, starts_at=now - 10, deadline=now + 300)
    exchange = LocalExchange(cfg.directory / "eve-exchange")
    h.app.learning_service = eve.EveLearningService(auth, exchange, cfg.directory, cfg.channel)

    posts, updates = [], []

    def post(**payload):
        assert payload["thread_ts"] == cfg.learning_thread_ts and payload["channel"] == cfg.channel
        posts.append(payload)
        return {"ok": True, "channel": cfg.channel, "ts": "101.101"}

    def update(**payload):
        assert payload["ts"] == "101.101" and payload["channel"] == cfg.channel
        updates.append(payload)
        return {"ok": True, "channel": cfg.channel, "ts": payload["ts"]}

    monkeypatch.setattr(h.slack, "chat_postMessage", post)
    monkeypatch.setattr(h.slack, "chat_update", update, raising=False)

    click(h, ready.home(actor), "ot_app_prompt", actor)
    view = list(h.slack.screens)[-1]
    h.form(view, {"command": {"command": {"value": "Why didn't it train?"}}}, actor)
    for action in ("ot_current_training", "ot_learning_preview", "ot_learning_improve"):
        submission = h.action(view, action, buttons(screen(h, view))[action], actor)
    row = exchange.get(auth.grant.reservation_ref)
    run_ref = row["run_ref"]
    assert row["state"] == "queued" and run_ref in str(screen(h, view))
    h.send("interactive", submission)

    def observe(state, step, seconds, checkpoint=None):
        current = exchange.get(auth.grant.reservation_ref)
        exchange.observe(current, state, step=step, checkpoint=checkpoint, seconds=seconds)

    def sync():
        h.app.sync_learning_card()
        card = (updates or posts)[-1]
        assert run_ref in content(card)
        return card

    def request(card, action, pending):
        payload = press_payload(card, "ot_learning_" + action)
        h.send("interactive", payload)
        h.send("interactive", payload)
        current = exchange.get(auth.grant.reservation_ref)
        assert current["run_ref"] == run_ref and current["state"] == pending
        assert (
            current["spec_digest"] == row["spec_digest"] and current["deadline"] == row["deadline"]
        )
        return sync()

    observe("running", 3, 7)
    running = sync()
    assert "3 of 16 steps" in content(running) and "7 seconds" in content(running)
    pausing = request(running, "pause", "pausing")
    assert "Pause requested" in content(pausing) and "Awaiting confirmation" in content(pausing)
    observe("paused", 4, 11, checkpoint=4)
    paused = sync()
    assert "Confirmed checkpoint · Step 4" in content(paused)
    assert set(buttons(paused)) == {"ot_learning_resume", "ot_learning_stop"}
    resuming = request(paused, "resume", "resuming")
    assert "Resume requested" in content(resuming) and "Step 4" in content(resuming)
    observe("running", 6, 20)
    resumed = sync()
    assert "6 of 16 steps" in content(resumed) and "20 seconds" in content(resumed)
    h.send("interactive", press_payload(running, "ot_learning_pause"))
    assert exchange.get(auth.grant.reservation_ref)["state"] == "running"
    observe("held", 7, 24)
    held = sync()
    assert "Held" in content(held) and set(buttons(held)) == {"ot_learning_stop"}
    stopping = request(held, "stop", "stopping")
    assert "Stop requested" in content(stopping)
    observe("stopped", 7, 24)
    stopped = sync()
    assert "Watching continues" in content(stopped) and not buttons(stopped)
    assert "Confirmed checkpoint · Step 4" in content(stopped)
    h.app.sync_learning_card()
    assert len(posts) == 1 and len(updates) == 7
    with exchange.transaction() as db:
        assert db.execute("SELECT count(*) FROM eve_jobs").fetchone()[0] == 1
        assert [r[0] for r in db.execute("SELECT action FROM eve_controls ORDER BY rowid")] == [
            "pause",
            "resume",
            "stop",
        ]


def test_home_and_private_card_control_same_exchange_run(ready, monkeypatch):
    _exercise_private_card(ready, monkeypatch)


def qualified_eve_sdk_card_controls(ready, monkeypatch):
    """Exercise the hash-pinned Slack SDK with an offline HTTP transport."""
    from slack_sdk import WebClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.version import __version__

    assert __version__ == "3.47.0"
    h = ready.h
    sdk = WebClient(token="xoxb-generated-offline", retry_handlers=[])
    api = []
    methods = {
        "views.publish": "views_publish",
        "views.open": "views_open",
        "views.update": "views_update",
        "conversations.info": "conversations_info",
        "conversations.members": "conversations_members",
        "chat.postMessage": "chat_postMessage",
        "chat.update": "chat_update",
    }

    def transport(*, url, args):
        method = url.rsplit("/", 1)[-1]
        api.append(method)
        params = args["json"] or args["params"]
        response = getattr(h.slack, methods[method])(**params)
        return {"status": 200, "headers": {}, "body": json.dumps({"ok": True, **response})}

    def send(kind, payload):
        req = SocketModeRequest.from_dict(
            {"type": kind, "payload": payload, "envelope_id": "generated"}
        )
        acks = []
        h.app.receive(
            req.type,
            req.payload,
            lambda body: acks.append(SocketModeResponse(req.envelope_id, body)),
        )
        assert len(acks) == 1 and acks[0].envelope_id == req.envelope_id

    monkeypatch.setattr(sdk, "_perform_urllib_http_request", transport)
    monkeypatch.setattr(h.app, "web", sdk)
    monkeypatch.setattr(h, "send", send)
    _exercise_private_card(ready, monkeypatch)
    assert api.count("chat.postMessage") == 1 and api.count("chat.update") == 7
    assert {"views.publish", "views.open", "views.update"} <= set(api)


@pytest.mark.parametrize(
    "problem", ["stale_heartbeat", "missing_heartbeat", "expired_permit", "read_only"]
)
def test_held_run_refuses_stop_with_missing_current_authority(installed, problem):
    from opentour_cell.pilot.eve_exchange import Exchange as LocalExchange

    service, auth, _ = installed
    exchange = LocalExchange(service.directory / "eve-exchange")
    service.exchange = exchange
    first = admit(installed)
    row = exchange.get(auth.grant.reservation_ref)
    exchange.observe(row, "held", step=4, checkpoint=3, seconds=10)
    fresh = service.status(auth.scope, first.run_ref)
    assert fresh.controls() == ("stop",)
    key = control_key(auth, fresh, "stop")
    if problem == "read_only":
        auth.grant.can_control = False
    else:
        with exchange.transaction() as db:
            if problem == "expired_permit":
                db.execute("UPDATE eve_jobs SET permit_until=?", (NOW,))
            else:
                db.execute(
                    "UPDATE eve_jobs SET heartbeat=?",
                    (NOW - 46 if problem == "stale_heartbeat" else 0,),
                )
    held = service.status(auth.scope, first.run_ref)
    assert not held.controls() and held.stale == (problem != "read_only")
    with pytest.raises(ValueError):
        service.control(auth.scope, first.run_ref, "stop", fresh.revision, key)
    with exchange.transaction() as db:
        assert db.execute("SELECT count(*) FROM eve_controls").fetchone()[0] == 0


def test_read_only_named_reviewer_cannot_start_or_control(installed):
    service, auth, ex = installed
    auth.grant.can_control = False
    assert not service.plan(auth.scope).can_improve
    with pytest.raises(ValueError):
        admit(installed)
    auth.grant.can_control = True
    receipt = admit(installed)
    ex.row.update(state="running", heartbeat=NOW)
    current = service.status(auth.scope, receipt.run_ref)
    key = control_key(auth, current, "pause")
    auth.grant.can_control = False
    assert not service.status(auth.scope, receipt.run_ref).controls()
    with pytest.raises(ValueError):
        service.control(auth.scope, receipt.run_ref, "pause", current.revision, key)
    assert not ex.actions
