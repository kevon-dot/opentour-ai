"""Explicit release check using the pinned Slack SDK with an offline HTTP transport."""

import hashlib
import importlib
import json
import os
from pathlib import Path
from typing import Any

import pytest
from test_chart_delivery import inspect
from test_managed_slack import setup_workflow
from test_reviewer_workflow import text
from test_slack_adapter import ACTOR, CHANNEL

Json = dict[str, Any]


def pinned_slack_sdk(monkeypatch):
    wheel_path = os.environ.get("OPENTOUR_TEST_SLACK_SDK_WHEEL")
    if wheel_path:
        wheel = Path(wheel_path)
        assert (
            hashlib.sha256(wheel.read_bytes()).hexdigest()
            == "8cfab3224d149962ef4313c1430d9ff65b8c3baf09f25988a920858ffa90aaa9"
        )
        monkeypatch.syspath_prepend(str(wheel))
    sdk = importlib.import_module("slack_sdk")
    assert importlib.import_module("slack_sdk.version").__version__ == "3.47.0"
    return sdk


def test_pinned_official_slack_client_serializes_only_ephemeral_charts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sdk = pinned_slack_sdk(monkeypatch)
    h, _, _, _ = setup_workflow(tmp_path, "rule", release=True)
    web = sdk.WebClient(token="generated-test-token", retry_handlers=[])
    messages = []

    def transport(*, url: str, args: Json) -> Json:
        method = url.rsplit("/", 1)[-1]
        params = args["json"] or args["params"]
        routes = {
            "conversations.info": h.slack.conversations_info,
            "conversations.members": h.slack.conversations_members,
            "views.open": h.slack.views_open,
            "views.update": h.slack.views_update,
        }
        if method == "chat.postEphemeral":
            messages.append(params)
            response = {"message_ts": "171.123"}
        else:
            assert method in routes
            response = routes[method](**params)
        return {"status": 200, "headers": {}, "body": json.dumps({"ok": True, **response})}

    monkeypatch.setattr(web, "_perform_urllib_http_request", transport)
    h.app.web = web
    view, revision = inspect(h)
    request_type = importlib.import_module("slack_sdk.socket_mode.request").SocketModeRequest
    payload = h.payload(view)
    payload.update(type="block_actions", actions=[{"action_id": "ot_charts", "value": revision}])
    envelope = request_type(type="interactive", envelope_id="generated-envelope", payload=payload)
    h.send(envelope.type, envelope.payload)
    assert len(messages) == 1
    assert (messages[0]["user"], messages[0]["channel"]) == (ACTOR, CHANNEL)
    assert sum(b["type"] == "data_visualization" for b in messages[0]["blocks"]) == 2
    assert "Slack accepted" in text(h, view) and not h.slack.post


def test_signed_settings_readback_through_offline_slack_sdk(tmp_path, monkeypatch):
    from test_owner_settings_home import exercise_private_settings

    sdk = pinned_slack_sdk(monkeypatch)
    published = []

    def connect(h):
        web = sdk.WebClient(token="generated-test-token", retry_handlers=[])
        methods = {
            "conversations.info": "conversations_info",
            "conversations.members": "conversations_members",
            "views.open": "views_open",
            "views.update": "views_update",
            "views.publish": "views_publish",
        }

        def transport(*, url, args):
            method = url.rsplit("/", 1)[-1]
            params = args["json"] or args["params"]
            if method == "views.publish":
                published.append(params["user_id"])
            response = getattr(h.slack, methods[method])(**params)
            return {"status": 200, "headers": {}, "body": json.dumps({"ok": True, **response})}

        monkeypatch.setattr(web, "_perform_urllib_http_request", transport)
        h.app.web = web

    exercise_private_settings(tmp_path, monkeypatch, connect=connect)
    assert len(published) == 7
