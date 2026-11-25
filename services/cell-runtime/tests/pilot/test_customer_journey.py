"""Customer-entry probes for published candidate 829d864. Uses a synthetic Slack transport."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from opentour_cell.pilot import slack_adapter, slack_views
from opentour_cell.pilot.training import LocalTrainingStore
from test_autonomous_cycle import Customer
from test_slack_adapter import ACTOR, APP, CHANNEL, TEAM, Slack


@pytest.mark.parametrize("bind_local_recipient", [False, True], ids=["no-recipient", "local-evaluation-owner"])
def test_one_policy_fresh_home_offers_workflow_creation(tmp_path: Path, bind_local_recipient: bool):
    root = tmp_path / "customer"
    root.mkdir(mode=0o700)
    policy = root / "invoice-policy.md"
    policy.write_text("Carrier charges on invoices with a purchase order are freight.")
    config = root / "settings.json"
    settings = {
        "organization": "founder", "team": TEAM, "app": APP, "channel": CHANNEL,
        "reviewer": ACTOR, "directory": str(root),
        "users": {ACTOR: {"roles": ["inspect", "review"], "datasets": [],
                         "preview": True, "aggregate": True, "expires_at": 4102444800}},
    }
    if bind_local_recipient:
        settings["recipient"] = ACTOR
    config.write_text(json.dumps(settings))
    config.chmod(0o600)
    slack_adapter.Settings.load(config)
    store = LocalTrainingStore(root / "training.sqlite3", "founder")
    assert store.catalog() == []
    transport = Slack()
    transport.views_publish = MagicMock(return_value={"ok": True})
    app = slack_adapter.Workbench(lambda: slack_adapter.Settings.load(config), transport,
                                  store, slack_views, submit=lambda work: work())
    acknowledgments = []
    try:
        app.receive("events_api", {"api_app_id": APP, "team_id": TEAM,
                    "type": "event_callback", "event": {"type": "app_home_opened",
                    "user": ACTOR, "tab": "home"}}, lambda *args: acknowledgments.append(args))
        assert len(acknowledgments) == 1
        view = transport.views_publish.call_args.kwargs["view"]
        buttons = [element for block in view["blocks"] for element in block.get("elements", [])
                   if element.get("type") == "button"]
        labels = [button["text"]["text"] for button in buttons]
        actions = [b for b in buttons if "add workflow" in b["text"]["text"].casefold()]
        assert len(actions) == 1, labels
        action = actions[0]
        app.receive("interactive", {
            "api_app_id": APP, "team": {"id": TEAM}, "user": {"id": ACTOR},
            "type": "block_actions", "trigger_id": "generated-starter",
            "container": {"type": "view", "view_id": "VHOME"},
            "view": {**view, "id": "VHOME"},
            "actions": [{"action_id": action["action_id"], "value": action.get("value", "")}],
        }, lambda *args: acknowledgments.append(args))
        assert len(acknowledgments) == 2
        assert transport.screens, "Add workflow must open private intake"
        modal = list(transport.screens.values())[-1]
        assert modal.get("type") == "modal"
        options = [element for block in modal.get("blocks", []) for element in
                   [*block.get("elements", []), block.get("element", {}), block.get("accessory", {})]]
        choices = [str(element.get("text", {}).get("text", "")) for element in options
                   if isinstance(element.get("text"), dict) and element.get("action_id")]
        choices += [str(option.get("text", {}).get("text", "")) for element in options
                    if element.get("action_id") for option in element.get("options", [])]
        assert any("starting from scratch" in choice.casefold() for choice in choices), choices
        assert store.catalog() == [] and not transport.post
    finally:
        app.pool.shutdown()


def test_historical_simple_repair_holds_for_slack_then_withdraws(tmp_path: Path):
    customer = Customer(tmp_path, model=False, real_reader=True)
    first = customer.run()
    assert first["state"] == "pending_slack_review" and first["outcome"] == "simple_repair", first
    assert customer.run() == first
    output_dir = customer.owner / "managed-outputs" / first["run_id"]
    assert not (output_dir / "model").exists()
    assert not (output_dir / "release-decision.json").exists()
    customer.ledger.withdraw(customer.mandate, actor="generated-owner")
    assert customer.run()["state"] == "held"
    assert not (output_dir / "release-decision.json").exists()
    assert sum(x["method"] == "resources/read" for x in customer.real_reader.received()) == 2
