"""Installed private scan interactions with generated owner, scanner and Slack receipts."""

import hashlib
import json
import sys
from types import SimpleNamespace

import pytest
from test_app_home import click, publish
from test_reviewer_workflow import buttons, restart, screen
from test_signed_records import kit, sign, stamp  # noqa: F401
from test_slack_adapter import ACTOR, APP, CHANNEL, OTHER, TEAM, Harness


class Scanner:
    def __init__(self, harness):
        self.h = harness
        self.state = "ready"
        self.version = 0
        self.allowed = True
        self.requests = []
        self.scans = 0

    @property
    def revision(self):
        return hashlib.sha256(str((self.state, self.version)).encode()).hexdigest()

    def status(self, actor):
        assert actor == ACTOR
        return {
            "state": self.state,
            "revision": self.revision,
            "can_control": self.allowed,
            "source_revision": hashlib.sha256(b"generated source").hexdigest(),
            "private_case": "GENERATED PRIVATE ANSWER",
        }

    def act(self, actor, action, revision, key):
        assert actor == ACTOR and self.allowed and revision == self.revision
        assert key == self.h.app.journal.opaque(
            "eve_scan", [self.h.store.organization, TEAM, CHANNEL, actor, action, revision]
        )
        self.requests.append(action)
        if action == "done_for_now":
            self.state = "parked"
        elif self.state != "scanning":
            self.state = "scanning"
            self.scans += 1
        self.version += 1
        return self.status(actor)


@pytest.fixture
def installed(tmp_path):
    h = Harness(tmp_path)
    scanner = Scanner(h)
    h.app.scan_service = scanner
    return h, scanner


def press(h, view_id, name, **overrides):
    action = "ot_eve_" + name
    body = h.payload(view_id)
    body.update(
        type="block_actions",
        container={"type": "view", "view_id": view_id},
        actions=[{"action_id": action, "value": buttons(screen(h, view_id))[action]}],
    )
    body.update(overrides)
    h.send("interactive", body)
    return body


def test_registered_channel_launcher_and_home_share_fenced_scan_controls(installed, monkeypatch):
    h, scanner = installed
    h.app.post_launcher()
    assert "Start scan" not in str(h.slack.post)
    launcher = {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": TEAM},
        "user": {"id": ACTOR},
        "channel": {"id": CHANNEL},
        "trigger_id": "open",
        "container": {"type": "message", "channel_id": CHANNEL, "message_ts": "101.101"},
        "actions": [{"action_id": "ot_open_workbench", "value": "open"}],
    }
    h.send("interactive", launcher)
    view = list(h.slack.screens)[-1]
    assert screen(h, view)["callback_id"] == "ot_eve_scan"
    assert "Ready to scan" in str(screen(h, view)) and "Start scan" in str(screen(h, view))
    assert "GENERATED PRIVATE ANSWER" not in json.dumps(screen(h, view))
    assert set(buttons(screen(h, view))) == {"ot_readouts", "ot_eve_start_scan"}
    first = press(h, view, "start_scan")
    h.send("interactive", first)
    assert "Scanning" in str(screen(h, view)) and scanner.requests == ["start_scan"]
    press(h, view, "another_scan")
    assert scanner.requests == ["start_scan", "another_scan"] and scanner.scans == 1
    scanner.state, scanner.version = "watching", scanner.version + 1
    press(h, view, "done_for_now")  # Stale pre-completion revision refreshes the modal.
    assert "Watching" in str(screen(h, view)) and len(scanner.requests) == 2
    parked = press(h, view, "done_for_now")
    assert "Parked" in str(screen(h, view)) and len(scanner.requests) == 3
    restart(h)
    h.app.scan_service = scanner
    h.send("interactive", first)
    h.send("interactive", parked)
    assert scanner.state == "parked" and len(scanner.requests) == 3
    home = publish(h, monkeypatch)
    assert "ot_app_eve_scan" in buttons(home)
    click(h, home, "ot_app_eve_scan")
    view = list(h.slack.screens)[-1]
    assert "Parked" in str(screen(h, view)) and "Start scan" in str(screen(h, view))
    press(h, view, "start_scan")
    assert scanner.scans == 2 and scanner.state == "scanning"
    h.action(view, "ot_readouts", "home")
    assert screen(h, view)["callback_id"] == "ot_home_view"


@pytest.mark.parametrize(
    "denial",
    [
        "actor",
        "team",
        "app",
        "channel",
        "container",
        "view",
        "callback",
        "pin",
        "expired",
        "role",
        "membership",
        "shared",
        "aggregate",
        "scope",
        "grant_revision",
        "journal_held",
    ],
)
def test_stale_or_unauthorized_private_scan_click_has_zero_effects(installed, denial):
    h, scanner = installed
    view = h.launch()
    changes = {}
    if denial in {"actor", "team", "channel"}:
        changes[{"actor": "user", "team": "team", "channel": "channel"}[denial]] = {"id": OTHER}
    elif denial == "app":
        changes["api_app_id"] = "AFOREIGN"
    elif denial == "container":
        changes["container"] = {"type": "view", "view_id": "VFOREIGN"}
    elif denial in {"view", "callback"}:
        changes["view"] = {
            **h.payload(view)["view"],
            **({"app_id": "AFOREIGN"} if denial == "view" else {"callback_id": "ot_home_view"}),
        }
    elif denial == "pin":
        changes["actions"] = [{"action_id": "ot_eve_start_scan", "value": "invented"}]
    elif denial in {"expired", "journal_held"}:
        with h.app.journal.db() as db:
            if denial == "expired":
                db.execute("UPDATE contexts SET expires=0")
            else:
                db.execute("UPDATE journal_control SET held=1")
    elif denial == "role":
        h.config["users"][ACTOR]["roles"].remove("review")
        h.save()
    elif denial == "membership":
        h.slack.members.remove(ACTOR)
    elif denial == "shared":
        h.slack.channel["is_shared"] = True
    elif denial == "aggregate":
        h.config["users"][ACTOR]["aggregate"] = False
        h.save()
    elif denial == "scope":
        scanner.allowed = False
    elif denial == "grant_revision":
        scanner.version += 1
    old = press(h, view, "start_scan", **changes)
    if denial == "scope":
        scanner.allowed = True
        h.send("interactive", old)
    assert scanner.state == "ready" and not scanner.requests and not scanner.scans


@pytest.mark.parametrize("accepted", [False, True])
def test_uncertain_scan_acknowledgement_rereads_durable_status(installed, monkeypatch, accepted):
    h, scanner = installed
    view = h.launch()
    act = scanner.act

    def interrupt(*args):
        if accepted:
            act(*args)
        raise TimeoutError()

    with monkeypatch.context() as patch:
        patch.setattr(scanner, "act", interrupt)
        old = press(h, view, "start_scan")
    h.send("interactive", old)
    if accepted:
        assert "Scanning" in str(screen(h, view)) and scanner.scans == 1
    else:
        assert "Ready to scan" in str(screen(h, view))
        press(h, view, "start_scan")
        assert scanner.scans == 1


def test_ineligible_or_held_scan_never_offers_work(installed, monkeypatch):
    h, scanner = installed
    assert "ot_app_eve_scan" not in buttons(publish(h, monkeypatch, OTHER))
    assert screen(h, h.launch(OTHER))["callback_id"] == "ot_home_view"
    scanner.state = "held"
    view = h.launch()
    assert "Scan held" in str(screen(h, view)) and set(buttons(screen(h, view))) == {"ot_readouts"}
    scanner.state = "GENERATED PRIVATE ANSWER"
    assert "ot_app_eve_scan" not in buttons(publish(h, monkeypatch))
    assert "GENERATED PRIVATE ANSWER" not in str(h.slack.screens)


@pytest.mark.parametrize(
    ("state", "reason", "show_notice"),
    [
        ("watching", "source_unavailable", True),
        ("watching", "source_unavailable: GENERATED PRIVATE PATH", False),
        ("watching", {"error": "GENERATED PRIVATE PATH"}, False),
        ("parked", "source_unavailable", False),
    ],
)
def test_scan_error_projection_uses_only_known_watching_reason(
    installed, monkeypatch, state, reason, show_notice
):
    h, scanner = installed
    scanner.state = state
    status = scanner.status(ACTOR)
    status.update(reason=reason, detail="GENERATED PRIVATE PATH", source_revision="b" * 64)
    monkeypatch.setattr(scanner, "status", lambda actor: status)
    rendered = str(screen(h, h.launch()))
    assert ("Last scan could not read the source. Watching will retry." in rendered) == show_notice
    assert "GENERATED PRIVATE PATH" not in rendered and "b" * 64 not in rendered


def test_signed_scan_before_fit_uses_durable_scanner_through_private_workbench(
    tmp_path,
    monkeypatch,
    kit,  # noqa: F811
):
    import test_learning_runs as learning
    from opentour_cell.pilot import eve_scan, signed_records

    monkeypatch.setattr(learning, "OWNER", ACTOR)
    site = learning.Site(tmp_path)
    site.authorize(eve_scan.SELECTION.removesuffix(".json"))
    registry = kit.registry()
    (site.directory / "pilot-issuer-registry.json").write_bytes(registry)
    (site.directory / "pilot-issuer-registry.json").chmod(0o600)
    signed_records.PilotVerifier(registry).pin_customer_anchor(
        kit.enrolment(engagement_id=site.engagement)
    )
    grant = {
        "profile_version": "1.0",
        "kind": "eve_source_discovery",
        "tenant": site.tenant,
        "engagement_id": site.engagement,
        "workspace": TEAM,
        "channel": CHANNEL,
        "reviewer": ACTOR,
        "workflow": learning.WORKFLOW,
        "mandate_id": learning.MANDATE,
        "connection_revision": "1" * 64,
        "issued_at": stamp(-60),
        "not_after": stamp(600),
    }
    path = site.directory / eve_scan.DISCOVERY_FILE
    path.write_bytes(sign(grant, signed_records.GRANT_PURPOSE, kit.customer, kit.operator))
    path.chmod(0o600)
    h = Harness(tmp_path, organization=site.tenant)
    h.store, h.path = site.store, site.directory / "slack-config.json"
    h.config["directory"] = str(site.directory)
    h.save()

    def reopen():
        restart(h)
        h.app.scan_service = eve_scan.open_eve_scan_service(
            site.directory, site.tenant, TEAM, CHANNEL
        )
        return h.app.scan_service

    scan = reopen()
    view = installed_startup_view(h, monkeypatch)
    first = press(h, view, "start_scan")
    assert "Watching" in str(screen(h, view))
    h.send("interactive", first)
    start = scan.status(ACTOR)
    assert start["state"] == "watching" and start["source_revision"] is None
    config = site.directory / "cycle.json"
    notice = "Last scan could not read the source. Watching will retry."
    empty = scan.tick(config, 0, lambda *_: None)
    assert empty["reason"] is None and notice not in str(screen(h, h.launch()))

    def unreadable(*_args):
        raise OSError("GENERATED PRIVATE SOURCE PATH")

    failed = scan.tick(config, 0, unreadable)
    assert failed["reason"] == "source_unavailable" and failed["state"] == "watching"
    failed_view = screen(h, h.launch())
    assert notice in str(failed_view) and "GENERATED PRIVATE SOURCE PATH" not in str(failed_view)
    assert {"ot_eve_another_scan", "ot_eve_done_for_now"} <= set(buttons(failed_view))
    seen = []

    def discover(*_args):
        seen.append("batch-A")
        active = h.launch()
        assert "Scanning" in str(screen(h, active))
        press(h, active, "another_scan")
        return "a" * 64

    result = scan.tick(site.directory / "cycle.json", 300, discover)
    assert (
        result["state"] == "watching"
        and result["source_revision"] == "a" * 64
        and seen == ["batch-A"]
    )
    view = h.launch()
    assert notice not in str(screen(h, view)) and result["source_revision"] not in str(
        screen(h, view)
    )
    done = press(h, view, "done_for_now")
    assert "Parked" in str(screen(h, view))
    scan = reopen()
    h.send("interactive", first)
    h.send("interactive", done)
    assert scan.tick(site.directory / "cycle.json", 300, discover)[
        "state"
    ] == "parked" and seen == ["batch-A"]
    view = h.launch()
    stale = press(h, view, "start_scan")
    assert scan.status(ACTOR)["state"] == "watching"
    with scan.db() as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 4
    site.registry.withdraw(learning.MANDATE, actor=ACTOR)
    h.send("interactive", stale)
    assert screen(h, h.launch())["callback_id"] == "ot_home_view"
    with scan.db() as db:
        assert db.execute("SELECT count(*) FROM decisions").fetchone()[0] == 4


def installed_startup_view(h, monkeypatch):
    from opentour_cell.pilot import slack_adapter

    acknowledgements = []
    socket = SimpleNamespace(
        socket_mode_request_listeners=[],
        close=lambda: None,
        send_socket_mode_response=acknowledgements.append,
    )

    def connect():
        req = SimpleNamespace(
            type="slash_commands",
            envelope_id="generated-startup",
            payload={
                "api_app_id": APP,
                "team_id": TEAM,
                "channel_id": CHANNEL,
                "user_id": ACTOR,
                "command": "/opentour",
                "trigger_id": "generated",
                "text": "",
            },
        )
        socket.socket_mode_request_listeners[0](socket, req)
        raise KeyboardInterrupt

    socket.connect = connect
    with monkeypatch.context() as patch:
        patch.setenv("SLACK_BOT_TOKEN", "xoxb-generated-offline")
        patch.setenv("SLACK_APP_TOKEN", "xapp-generated-offline")
        patch.setattr(
            h.slack, "auth_test", lambda: {"team_id": TEAM, "bot_id": "BGENERATED"}, raising=False
        )
        patch.setitem(sys.modules, "slack_sdk", SimpleNamespace(WebClient=lambda **kw: h.slack))
        patch.setitem(
            sys.modules,
            "slack_sdk.socket_mode",
            SimpleNamespace(SocketModeClient=lambda **kw: socket),
        )
        patch.setitem(
            sys.modules,
            "slack_sdk.socket_mode.response",
            SimpleNamespace(SocketModeResponse=SimpleNamespace),
        )
        assert slack_adapter.main([str(h.path)]) == 0
    assert len(acknowledgements) == 1 and acknowledgements[0].envelope_id == "generated-startup"
    return list(h.slack.screens)[-1]


def qualified_eve_scan_sdk(installed, monkeypatch):
    """The exact installed Slack SDK, with all HTTP calls contained in a generated transport."""
    from slack_sdk import WebClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.version import __version__

    assert __version__ == "3.47.0"
    h, scanner = installed
    sdk = WebClient(token="xoxb-generated-offline", retry_handlers=[])
    api = []
    methods = {
        "views.open": h.slack.views_open,
        "views.update": h.slack.views_update,
        "conversations.info": h.slack.conversations_info,
        "conversations.members": h.slack.conversations_members,
    }

    def transport(*, url, args):
        method = url.rsplit("/", 1)[-1]
        api.append(method)
        result = methods[method](**(args["json"] or args["params"]))
        return {"status": 200, "headers": {}, "body": json.dumps({"ok": True, **result})}

    def send(kind, payload):
        req = SocketModeRequest.from_dict(
            {"type": kind, "payload": payload, "envelope_id": "generated"}
        )
        acks = []

        def ack(body):
            h.slack.calls.append("ack")
            acks.append(SocketModeResponse(req.envelope_id, body))

        h.app.receive(req.type, req.payload, ack)
        assert len(acks) == 1 and acks[0].envelope_id == "generated"

    monkeypatch.setattr(sdk, "_perform_urllib_http_request", transport)
    monkeypatch.setattr(h.app, "web", sdk)
    monkeypatch.setattr(h, "send", send)
    view = h.launch()
    press(h, view, "start_scan")
    press(h, view, "another_scan")
    replay = press(h, view, "done_for_now")
    h.send("interactive", replay)
    assert scanner.scans == 1 and scanner.requests == ["start_scan", "another_scan", "done_for_now"]
    assert api.count("views.open") == 1 and api.count("views.update") == 4
    assert "Parked" in str(screen(h, view))
