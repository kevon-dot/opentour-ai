"""Exercise the official MCP SDK and the real Slack catalog adapter against one local store."""

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = "opentour_cell.pilot.onboarding_mcp"
TRAINING = "opentour_cell.pilot.training"
PREVIEW = "preview_dataset_onboarding"
CONFIRM = "confirm_dataset_onboarding"
ACTOR, DENIED, CHAN, TEAM, APP = "UREVIEWER", "UOTHER", "CPRIVATE", "TOWNER", "AOWNER"
ATTESTATION = "att-0123456789abcdef"


class RecordingSlack:
    """Only the Slack Web API boundary is simulated. All views and grants come from the app."""

    def __init__(self) -> None:
        self.screens: dict[str, dict] = {}

    def conversations_info(self, *, channel: str) -> dict:
        assert channel == CHAN
        return {"channel": {"id": CHAN, "is_private": True, "is_shared": False}}

    def conversations_members(self, *, channel: str, limit: int, cursor: str) -> dict:
        assert channel == CHAN and limit and not cursor
        return {"members": [ACTOR, DENIED]}

    def views_open(self, *, trigger_id: str, view: dict) -> dict:
        assert trigger_id
        key = f"V{len(self.screens) + 1:04}"
        self.screens[key] = view
        return {"ok": True, "view": {"id": key}}

    def views_update(self, *, view_id: str, view: dict, hash: str = "") -> dict:
        self.screens[view_id] = view
        return {"ok": True, "view": {"id": view_id}}


def verify_slack(root: Path, org: str, dataset: str, revision: str, *, train: bool) -> None:
    from opentour_cell.pilot import slack_adapter, slack_views
    from opentour_cell.pilot.text_classifier import TrainingError
    from opentour_cell.pilot.training import LocalTrainingStore

    config = root / "workspace.json"
    permissions = {
        "roles": ["inspect", "tune"],
        "datasets": [dataset],
        "preview": False,
        "aggregate": False,
        "expires_at": int(time.time()) + 300,
    }
    config.write_text(
        json.dumps(
            {
                "organization": org,
                "app": APP,
                "team": TEAM,
                "channel": CHAN,
                "directory": str(root),
                "reviewer": ACTOR,
                "recipient": "UAUDIENCE",
                "users": {
                    ACTOR: permissions,
                    DENIED: {**permissions, "datasets": ["different-dataset"]},
                },
            }
        )
    )
    config.chmod(0o600)
    web = RecordingSlack()
    store = LocalTrainingStore(root / "training.sqlite3", org)
    workbench = slack_adapter.Workbench(
        lambda: slack_adapter.Settings.load(config), web, store, slack_views, submit=lambda f: f()
    )

    def send(kind: str, body: dict) -> None:
        acknowledgments: list[Any] = []
        workbench.receive(kind, body, acknowledgments.append)
        assert len(acknowledgments) == 1

    def browse(actor: str) -> str:
        send(
            "slash_commands",
            {
                "api_app_id": APP,
                "team_id": TEAM,
                "channel_id": CHAN,
                "user_id": actor,
                "trigger_id": "synthetic-trigger",
                "command": "/opentour",
                "text": "data",
            },
        )
        return next(reversed(web.screens))

    def choose(view: str, actor: str, action: str) -> None:
        send(
            "interactive",
            {
                "api_app_id": APP,
                "team": {"id": TEAM},
                "user": {"id": actor},
                "view": {"id": view, **web.screens[view]},
                "type": "block_actions",
                "actions": [{"action_id": action, "value": dataset}],
            },
        )

    allowed = browse(ACTOR)
    assert dataset in json.dumps(web.screens[allowed])
    choose(allowed, ACTOR, "ot_dataset_open")
    screen = json.dumps(web.screens[allowed])
    assert web.screens[allowed]["callback_id"] == "ot_dataset_detail" and revision in screen
    assert ("ot_tune_dataset" in screen) == train
    assert all(secret not in screen for secret in ["invoice billing statement", "supplied-1"])
    if not train:
        choose(allowed, ACTOR, "ot_tune_dataset")
        assert "unavailable" in json.dumps(web.screens[allowed]).lower()
        try:
            store.propose(dataset)
        except TrainingError as error:
            assert str(error) == "training_not_authorized"
        else:
            raise AssertionError("Evaluation-only data entered the training proposal path.")

    unrelated = browse(DENIED)
    before = json.dumps(web.screens[unrelated])
    assert "ot_dataset_open" not in before and dataset not in before and revision not in before
    choose(unrelated, DENIED, "ot_dataset_open")
    refused = json.dumps(web.screens[unrelated])
    assert dataset not in refused and revision not in refused and "unavailable" in refused.lower()
    with sqlite3.connect(root / "training.sqlite3") as db:
        assert db.execute("SELECT COUNT(*) FROM datasets").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0


async def verify_selection(
    python: str,
    server: list[str],
    owner: list[str],
    trainer: list[str],
    environment: dict[str, str],
    *,
    train: bool,
) -> None:
    from opentour_cell.pilot.custody import Closeout

    org = "generated-training-org" if train else "generated-evaluation-org"
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        db, manifest, grant = root / "training.sqlite3", root / "dataset.json", root / "grant.json"
        flags = [
            "--organization",
            org,
            "--store",
            str(db),
            "--manifest",
            str(manifest),
            "--authorization",
            str(grant),
            "--custodian-attestation",
            ATTESTATION,
        ]
        env = {**os.environ, **environment}
        fixture = (
            ["generated-fixture", "--reviewer", ACTOR]
            if train
            else ["generated-evaluation-fixture"]
        )
        data = json.loads(
            subprocess.check_output(
                [python, *trainer, "--org", org, "--db", str(db), *fixture], env=env, timeout=10
            )
        )
        canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        revision = hashlib.sha256(canonical.encode()).hexdigest()
        manifest.write_text(canonical, encoding="utf-8")
        manifest.chmod(0o600)
        params = StdioServerParameters(command=python, args=[*server, *flags], env=environment)
        arguments = {"dataset_revision": revision}
        expected = {"registration": "registered", **arguments}
        expires = int(time.time()) + 300
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            init = await client.initialize()
            assert init.serverInfo.name == "opentour-dataset-onboarding"
            assert {x.name for x in (await client.list_tools()).tools} == {PREVIEW, CONFIRM}
            preview = await client.call_tool(PREVIEW, {})
            info = preview.structuredContent
            assert not preview.isError and info["dataset_revision"] == revision
            assert info["mode"] == ("training" if train else "evaluation_only")
            assert info["purpose"] == ("local_text_classification" if train else "evaluation_only")
            assert ("train_cpu_text" in info["permitted_actions"]) == train
            if not train:
                assert info["training_rows"] == 0 and info["permitted_actions"] == [
                    "inspect_metadata",
                    "score_frozen",
                ]
            assert info["permitted_fields"] == sorted(data["visible_fields"])
            assert (
                info["retention"] == data["retention"]
                and info["destination"] == data["destination"]
            )
            assert (
                info["grant_expires_at"] is None
                and info["authorization"] == "authorization_required"
            )
            assert info["registration"] == "absent" and not db.exists()
            denied = await client.call_tool(CONFIRM, arguments)
            extra = await client.call_tool(CONFIRM, {**arguments, "approved": True})
            assert denied.isError and extra.isError and not db.exists()
            subprocess.run(
                [
                    python,
                    *owner,
                    *flags,
                    "--dataset-revision",
                    revision,
                    "--expires-at",
                    str(expires),
                ],
                env=env,
                capture_output=True,
                check=True,
                timeout=10,
            )
            allowed = await client.call_tool(PREVIEW, {})
            assert allowed.structuredContent["authorization"] == "authorized"
            assert allowed.structuredContent["grant_expires_at"] == expires
            mismatch = await client.call_tool(CONFIRM, {"dataset_revision": "0" * 64})
            assert mismatch.isError and not db.exists()
            confirmed = await client.call_tool(CONFIRM, arguments)
            repeated = await client.call_tool(CONFIRM, arguments)
            assert not confirmed.isError and confirmed.structuredContent == expected
            assert not repeated.isError and repeated.structuredContent == expected
            for value in [preview, denied, extra, allowed, mismatch, confirmed, repeated]:
                assert all(
                    private not in value.model_dump_json()
                    for private in [
                        org,
                        ACTOR,
                        folder,
                        ATTESTATION,
                        data["dataset_id"],
                        "invoice billing statement",
                        "train-family-0",
                        "supplied-1",
                        "route-b",
                    ]
                )

        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            await client.initialize()
            assert (await client.call_tool(CONFIRM, arguments)).isError
            current = await client.call_tool(PREVIEW, {})
            assert current.structuredContent["registration"] == "registered"
            again = await client.call_tool(CONFIRM, arguments)
            assert not again.isError and again.structuredContent == expected
            old_authority = grant.read_bytes()
            grant.unlink()
            assert (await client.call_tool(CONFIRM, arguments)).isError
        verify_slack(root, org, data["dataset_id"], revision, train=train)
        backup = root / "before-closeout.sqlite3"
        shutil.copyfile(db, backup)
        Closeout(root, org).withdraw(
            agreement="sdk-terms",
            custodian="sdk-custodian",
            support="sdk-support",
            due="2040-04-01",
            trigger="withdrawal",
        )
        shutil.copyfile(backup, db)
        grant.write_bytes(old_authority)
        grant.chmod(0o600)
        manifest.unlink()
        async with stdio_client(params) as streams, ClientSession(*streams) as client:
            await client.initialize()
            held = await client.call_tool(PREVIEW, {})
            assert held.structuredContent == {"registration": "unavailable"}
            assert (await client.call_tool(CONFIRM, arguments)).isError
            assert revision not in held.model_dump_json()


async def verify(python: str, bundle: Path | None) -> None:
    if bundle:
        module_root = str(bundle.resolve(strict=True))
        start = ["-I", "-S", "-B", module_root]
        server, owner, trainer = (
            [*start, "mcp-onboard"],
            [*start, "onboarding-authorize"],
            [*start, "data"],
        )
        environment = {}
    else:
        module_root = str(Path(__file__).resolve().parents[2] / "src")
        server, trainer = ["-m", SERVER], ["-m", TRAINING]
        owner = ["-c", f"from {SERVER} import authorize_main\nraise SystemExit(authorize_main())"]
        environment = {"PYTHONPATH": module_root}
    sys.path.insert(0, module_root)
    from opentour_cell.pilot import slack_adapter

    assert slack_adapter.__file__.startswith(module_root + "/")
    for train in (False, True):
        await verify_selection(python, server, owner, trainer, environment, train=train)
    print(
        "Official MCP SDK initialized two independent modes, denied missing and mismatched grants, "
        "confirmed once per revision and reconnected. The Slack app exposed each revision only "
        "to its permitted member. Evaluation-only data had no training action or job. "
        "Restoring the old database and grant after withdrawal exposed no metadata."
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-python", required=True)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args()
    asyncio.run(verify(args.server_python, args.bundle))
