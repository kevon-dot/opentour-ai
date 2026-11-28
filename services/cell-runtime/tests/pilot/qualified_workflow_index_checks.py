"""Exercise the production private Slack entry with a registered publication purpose."""

import importlib
import json

from opentour_cell.pilot import signed_records as records
from opentour_cell.pilot import slack_adapter
from opentour_cell.pilot.workflow_index_authority import PUBLICATION_PURPOSE, REGISTRY_FILE
from test_managed_slack import setup_workflow
from test_reviewer_workflow import screen
from test_signed_records import kit as signer_kit  # noqa: F401
from test_signed_records import sign, stamp
from test_slack_adapter import ACTOR
from test_status_questions import counts
from test_workflow_index_home import admit_workflow_label


def test_signed_publication_drives_actual_socket_mode_home_and_private_selection(
    tmp_path,
    monkeypatch,
    signer_kit,  # noqa: F811
):
    sdk = importlib.import_module("slack_sdk")
    socket_mode = importlib.import_module("slack_sdk.socket_mode")
    request_type = importlib.import_module("slack_sdk.socket_mode.request").SocketModeRequest
    assert importlib.import_module("slack_sdk.version").__version__ == "3.47.0"
    assert records.REQUIRED_ROLES[PUBLICATION_PURPOSE] == {
        records.CUSTOMER_OWNER,
        records.OPERATOR_CUSTODIAN,
    }

    h, plan, run, _ = setup_workflow(tmp_path / "slack", "rule", release=True)
    label = admit_workflow_label(h, plan)
    cfg = h.app.settings()
    h.config["workflow_index_publication_file"] = "workflow-index.json"
    h.save()
    kit = signer_kit
    if PUBLICATION_PURPOSE not in kit.operator.purposes:
        kit.operator.purposes.append(PUBLICATION_PURPOSE)
    registry = kit.registry()
    registry_path = cfg.directory / REGISTRY_FILE
    registry_path.write_bytes(registry)
    registry_path.chmod(0o600)
    kit.verifier(registry).pin_customer_anchor(kit.enrolment(engagement_id=h.store.engagement_id))
    path = cfg.directory / h.config["workflow_index_publication_file"]

    def publish(label_text, offset):
        payload = {
            "profile_version": "1.0",
            "tenant": cfg.organization,
            "engagement_id": h.store.engagement_id,
            "issued_at": stamp(offset),
            "not_after": stamp(600),
            "workflows": [
                {
                    "workflow_ref": label.scope.workflow_ref,
                    "label": label_text,
                    "aliases": ["Purchasing"],
                    "directory_viewers": [],
                }
            ],
        }
        path.write_bytes(sign(payload, PUBLICATION_PURPOSE, kit.customer, kit.operator))
        path.chmod(0o600)

    publish("Orders", -25)

    homes = []

    def transport(self, *, url, args):
        method = url.rsplit("/", 1)[-1]
        params = args["json"] or args["params"]
        if method == "auth.test":
            result = {"team_id": cfg.team, "bot_id": "generated"}
        elif method == "views.publish":
            assert params["user_id"] == ACTOR
            homes.append(params["view"])
            result = {}
        else:
            handler = {
                "conversations.info": h.slack.conversations_info,
                "conversations.members": h.slack.conversations_members,
                "views.open": h.slack.views_open,
                "views.update": h.slack.views_update,
            }[method]
            result = handler(**params)
        return {"status": 200, "headers": {}, "body": json.dumps({"ok": True, **result})}

    monkeypatch.setattr(sdk.WebClient, "_perform_urllib_http_request", transport)

    class OfflineSocket:
        def __init__(self, **kwargs):
            self.socket_mode_request_listeners = []
            self.acks = []

        def send_socket_mode_response(self, response):
            self.acks.append(response.envelope_id)

        def send(self, kind, payload):
            before = len(self.acks)
            self.socket_mode_request_listeners[0](
                self, request_type(type=kind, envelope_id="offline", payload=payload)
            )
            assert self.acks[before:] == ["offline"]

        def home(self):
            self.send(
                "events_api",
                {
                    "api_app_id": cfg.app,
                    "team_id": cfg.team,
                    "type": "event_callback",
                    "event": {"type": "app_home_opened", "user": ACTOR, "tab": "home"},
                },
            )
            return homes[-1]

        def choose(self, home):
            button = next(b["accessory"] for b in home["blocks"] if "accessory" in b)
            self.send(
                "interactive",
                {
                    "api_app_id": cfg.app,
                    "team": {"id": cfg.team},
                    "user": {"id": ACTOR},
                    "container": {"type": "view", "view_id": "VHOME"},
                    "view": {**home, "id": "VHOME"},
                    "type": "block_actions",
                    "trigger_id": "generated",
                    "actions": [{"action_id": button["action_id"], "value": button["value"]}],
                },
            )
            return screen(h, list(h.slack.screens)[-1])

        def connect(self):
            original = self.home()
            assert run in json.dumps(original) and "Orders" in json.dumps(original)
            assert self.choose(original)["callback_id"] == "ot_evidence_view"
            prior = path.read_bytes()
            publish("Invoices", -10)
            assert "Invoices" in json.dumps(self.home())
            assert "ot_evidence_view" not in json.dumps(self.choose(original))
            path.write_bytes(prior)
            denied = json.dumps(self.home())
            assert "Workflow queue access is unavailable" in denied and run not in denied
            raise KeyboardInterrupt

        def close(self):
            pass

    monkeypatch.setattr(socket_mode, "SocketModeClient", OfflineSocket)
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-generated")
    monkeypatch.setenv("SLACK_APP_TOKEN", "xapp-generated")
    before = counts(h)
    assert slack_adapter.main([str(h.path)]) == 0
    assert counts(h) == before
