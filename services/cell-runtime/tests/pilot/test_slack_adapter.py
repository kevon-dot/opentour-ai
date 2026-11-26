"""Socket Mode interaction tests using genuine generated training data and no network."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

import pytest
from opentour_cell.pilot import slack_adapter, slack_views
from opentour_cell.pilot.brief import decision_brief
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_data import generated_dataset, generated_evaluation_dataset

Json = dict[str, Any]
ACTOR = "UREVIEWER"
OTHER = "UINSPECTOR"
CHANNEL = "CPRIVATE"
TEAM = "TOWNER"
APP = "AOWNER"
DATA = "generated-intake"
HOOK = "https://hooks.zapier.com/hooks/catch/123/opaque/"


class Slack:
    def __init__(self) -> None:
        self.screens: dict[str, Json] = {}
        self.members = [ACTOR, OTHER]
        self.channel: Json = {"id": CHANNEL, "is_shared": False, "is_private": True}
        self.fail_evidence = False
        self.post: Json = {}
        self.calls: list[str] = []

    def conversations_info(self, *, channel: str) -> Json:
        self.calls.append("info")
        assert channel == self.channel["id"]
        return {"channel": self.channel}

    def conversations_members(self, *, channel: str, limit: int, cursor: str) -> Json:
        assert channel == self.channel["id"] and limit == 200 and not cursor
        return {"members": self.members}

    def views_open(self, *, trigger_id: str, view: Json) -> Json:
        self.calls.append("open")
        view_id = f"V{len(self.screens) + 1:03}"
        self.screens[view_id] = view
        return {"ok": True, "view": {"id": view_id}}

    def views_update(self, *, view_id: str, view: Json, hash: str = "") -> Json | None:
        del hash
        if self.fail_evidence and view.get("callback_id") == "ot_evidence_view":
            return None
        self.screens[view_id] = view
        return {"ok": True, "view": {"id": view_id}}

    def chat_postMessage(self, **payload: Any) -> Json:
        self.post = payload
        return {"ok": True, "channel": payload["channel"], "ts": "101.101"}


class Harness:
    def __init__(self, path: Path, organization: str = "pilot") -> None:
        directory = path / "store"
        directory.mkdir(parents=True, mode=0o700)
        self.path = directory / "config.json"
        self.config: Json = {
            "organization": organization,
            "app": APP,
            "team": TEAM,
            "channel": CHANNEL,
            "directory": str(directory),
            "reviewer": ACTOR,
            "recipient": "UAUDIENCE",
            "zapier": True,
            "zapier_destination_sha256": slack_adapter.hook_fingerprint(HOOK),
            "users": {
                actor: {
                    "roles": ["inspect", "review", "tune"],
                    "datasets": [DATA],
                    "preview": actor == ACTOR,
                    "aggregate": True,
                    "expires_at": 4102444800,
                }
                for actor in (ACTOR, OTHER)
            },
        }
        self.save()
        self.store = LocalTrainingStore(directory / "training.sqlite3", organization)
        self.store.register(generated_dataset(organization, ACTOR))
        self.slack = Slack()
        self.app = slack_adapter.Workbench(
            lambda: slack_adapter.Settings.load(self.path),
            self.slack,
            self.store,
            slack_views,
            submit=lambda call: call(),
        )

    def save(self) -> None:
        self.path.write_text(json.dumps(self.config))
        self.path.chmod(0o600)

    def flush(self, hook: str = HOOK) -> int:
        return slack_adapter.flush_receipts(
            self.app.journal,
            hook,
            config=lambda: slack_adapter.Settings.load(self.path),
            training=self.store,
        )

    def send(self, typ: str, payload: Json) -> None:
        acks = []

        def ack(value: Json | None) -> None:
            self.slack.calls.append("ack")
            acks.append(value)

        self.app.receive(typ, payload, ack)
        assert len(acks) == 1

    def launch(self, actor: str = ACTOR, prompt: str = "") -> str:
        self.slack.calls.clear()
        self.send(
            "slash_commands",
            {
                "api_app_id": APP,
                "team_id": self.config["team"],
                "channel_id": self.config["channel"],
                "user_id": actor,
                "trigger_id": "test-trigger",
                "command": "/opentour",
                "text": prompt,
            },
        )
        assert self.slack.calls[:2] == ["ack", "open"]
        return list(self.slack.screens)[-1]

    def payload(self, view: str, actor: str = ACTOR) -> Json:
        return {
            "api_app_id": APP,
            "team": {"id": self.config["team"]},
            "user": {"id": actor},
            "view": {"id": view, **deepcopy(self.slack.screens[view])},
        }

    def action(self, view: str, name: str, value: str, actor: str = ACTOR) -> Json:
        body = self.payload(view, actor)
        body.update(type="block_actions", actions=[{"action_id": name, "value": value}])
        self.send("interactive", body)
        return body

    def form(self, view: str, values: Json | None = None, actor: str = ACTOR) -> Json:
        body = self.payload(view, actor)
        body["type"] = "view_submission"
        body["view"]["state"] = {"values": values or {}}
        self.send("interactive", body)
        return body

    def propose(self) -> tuple[str, str]:
        view = self.launch(prompt="search label")
        assert DATA in json.dumps(self.slack.screens[view])
        self.action(view, "ot_dataset_open", DATA)
        preview = json.dumps(self.slack.screens[view])
        assert "invoice billing statement" in preview and "parcel delivery delayed" not in preview
        self.action(view, "ot_tune_dataset", DATA)
        values = {
            k: {k: {"selected_option": {"value": v}}}
            for k, v in (("dataset", DATA), ("profile", "cpu_text_v1"), ("epochs", "3"))
        }
        submission = self.form(view, values)
        self.send("interactive", submission)
        assert self.slack.screens[view]["callback_id"] == "ot_tune_start"
        job = self.app.journal.latest([DATA])
        assert self.store.status(job)["status"] == "awaiting_approval"
        return view, job

    def completed(self) -> tuple[str, str, str]:
        view, job = self.propose()
        body = self.form(view)
        self.send("interactive", body)
        assert self.store.status(job)["status"] == "completed"
        assert self.store.status(job)["result"]["changed_weights"] > 0
        self.action(view, "ot_status", job)
        screen = self.slack.screens[view]
        assert screen["callback_id"] == "ot_status_view"
        revision = next(
            b["value"]
            for row in screen["blocks"]
            for b in row.get("elements", [])
            if b.get("action_id") == "ot_evidence"
        )
        assert revision.startswith("rev_") and revision != self.store.status(job)["revision"]
        return view, job, revision

    def approve(self) -> tuple[str, str]:
        view, job, revision = self.completed()
        self.action(view, "ot_evidence", revision)
        self.action(view, "ot_home", "home")
        self.action(view, "ot_approve", revision)
        return job, self.app.journal.decision(revision)["receipt"]


@pytest.mark.parametrize(
    "action,expected", [("ot_approve", "approved"), ("ot_changes", "changes_requested")]
)
def test_fit_inspect_and_decide_once(tmp_path: Path, action: str, expected: str) -> None:
    h = Harness(tmp_path)
    view, job, revision = h.completed()
    assert h.app.journal.decision(revision) == {}
    h.action(view, "ot_evidence", revision)
    screen = json.dumps(h.slack.screens[view])
    assert "Readout evidence" in screen and "UAUDIENCE" in screen and revision in screen
    h.action(view, "ot_home", "home")
    body = h.action(view, action, revision)
    recorded = h.app.journal.decision(revision)
    assert recorded["disposition"] == expected and recorded["actor"] == ACTOR
    assert recorded["audience"] == "UAUDIENCE" and recorded["receipt"] in json.dumps(
        h.slack.screens[view]
    )
    h.send("interactive", body)
    assert h.app.journal.decision(revision) == recorded
    assert h.store.status(job)["result"]["heldout_count"] == 4
    reopened = h.launch(OTHER)
    h.action(reopened, "ot_evidence", revision, OTHER)
    h.action(reopened, "ot_home", "home", OTHER)
    assert "ot_approve" not in json.dumps(h.slack.screens[reopened])
    h.action(reopened, "ot_changes", revision, OTHER)
    assert h.app.journal.decision(revision) == recorded


def test_wrong_actor_channel_team_and_stale_context(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    view = h.launch()
    body = h.payload(view)
    body.update(type="block_actions", actions=[{"action_id": "ot_browse", "value": "home"}])
    initial = deepcopy(h.slack.screens[view])
    for change in (
        {"team": {"id": "TOTHER"}},
        {"api_app_id": "AOTHER"},
        {"user": {"id": OTHER}},
        {"channel": {"id": "COTHER"}},
    ):
        h.send("interactive", {**body, **change})
        assert h.slack.screens[view] == initial
    h.send("interactive", body)
    latest = deepcopy(h.slack.screens[view])
    h.send("interactive", body)
    assert h.slack.screens[view] == latest and latest != initial
    h.slack.members.remove(ACTOR)
    h.action(view, "ot_dataset_open", DATA)
    assert "invoice" not in json.dumps(h.slack.screens[view])
    h.slack.members.append(ACTOR)
    h.slack.channel["is_ext_shared"] = True
    view = h.launch()
    assert "ot_home_view" not in json.dumps(h.slack.screens[view])


def test_review_requires_real_successful_evidence_and_current_grant(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    view, job, revision = h.completed()
    view = h.launch(prompt=job)
    assert "ot_approve" not in json.dumps(h.slack.screens[view])
    h.action(view, "ot_approve", revision)
    assert not h.app.journal.decision(revision)

    h.slack.fail_evidence = True
    view = h.launch()
    h.action(view, "ot_evidence", revision)
    assert not h.app.journal.inspected(ACTOR, revision, h.app._policy(h.app.settings(), ACTOR))
    h.slack.fail_evidence = False
    view = h.launch()
    h.action(view, "ot_evidence", revision)
    h.action(view, "ot_home", "home")
    h.config["users"][ACTOR]["expires_at"] = 0
    h.save()
    h.action(view, "ot_approve", revision)
    assert not h.app.journal.decision(revision)


def test_config_requires_operator_owned_regular_file(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    assert slack_adapter.main([str(h.path), "--check-config"]) == 0
    h.path.chmod(0o666)
    assert slack_adapter.main([str(h.path), "--check-config"]) == 1
    h.path.chmod(0o600)
    alias = h.path.parent / "alias"
    alias.symlink_to(h.path)
    assert slack_adapter.main([str(alias), "--check-config"]) == 1
    h.path.parent.chmod(0o777)
    assert slack_adapter.main([str(h.path), "--check-config"]) == 1


def test_only_originating_trainer_confirms_and_prompt_never_approves(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    view, job = h.propose()
    foreign = h.launch(OTHER, prompt=f"status {job}")
    assert h.slack.screens[foreign]["callback_id"] == "ot_status_view"
    h.launch(prompt="approve")
    assert h.store.status(job)["status"] == "awaiting_approval"
    h.form(view, actor=OTHER)
    assert h.store.status(job)["status"] == "awaiting_approval"


@pytest.mark.parametrize(
    "share_before,share_now", [(False, False), (False, True), (True, False), (True, True)]
)
def test_team_bound_launcher_and_explicit_zapier_delivery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, share_before: bool, share_now: bool
) -> None:
    h = Harness(tmp_path)
    h.config["zapier_disposition"] = share_before
    h.save()
    h.app.post_launcher()
    assert "generated-intake" not in json.dumps(h.slack.post)
    launcher: Json = {
        "type": "block_actions",
        "api_app_id": APP,
        "team": {"id": TEAM},
        "user": {"id": ACTOR},
        "channel": {"id": CHANNEL},
        "trigger_id": "open",
        "container": {"type": "message", "channel_id": CHANNEL, "message_ts": "wrong"},
        "actions": [{"action_id": "ot_open_workbench", "value": "open"}],
    }
    h.send("interactive", launcher)
    assert h.slack.screens == {}
    launcher["container"]["message_ts"] = "101.101"
    h.send("interactive", launcher)
    assert h.slack.screens["V001"]["callback_id"] == "ot_home_view"
    view, _, revision = h.completed()
    h.action(view, "ot_evidence", revision)
    h.action(view, "ot_home", "home")
    h.action(view, "ot_approve", revision)
    receipt = h.app.journal.decision(revision)["receipt"]
    events = []

    class Connection:
        status = 200

        def __init__(self, host: str, timeout: int):
            assert host == "hooks.zapier.com" and timeout == 5

        def request(self, method: str, path: str, *, body: str, headers: Json) -> None:
            assert method == "POST" and path == "/hooks/catch/123/opaque/"
            events.append(json.loads(body))

        def getresponse(self) -> Connection:
            return self

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    with pytest.raises(slack_adapter.Refused):
        slack_adapter.flush_receipts(h.app.journal, "https://example.org/path")
    hook = "https://hooks.zapier.com/hooks/catch/123/opaque/"
    h.config["zapier_disposition"] = share_now
    h.save()
    assert h.flush(hook) == 1
    expected = {"event": "pilot_decision_recorded", "receipt": receipt}
    if share_before and share_now:
        expected["disposition"] = "approved_for_readout"
    assert events == [expected]
    assert h.flush(hook) == 0


def test_reopened_private_evidence_chain_and_tamper_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    job, receipt = h.approve()
    journal = slack_adapter.Journal(h.app.journal.path, slack_adapter.Settings.load(h.path).binding)
    restored = journal.receipt(receipt)
    evidence = restored["evidence"]
    result = h.store.status(job)["result"]
    assert (evidence["job_id"], evidence["model_revision"], evidence["cohort_revision"]) == (
        job,
        result["artifact_digest"],
        result["cohort_digest"],
    )
    assert evidence["viewer"] == ACTOR and evidence["reviewer"] == ACTOR
    assert (evidence["team"], evidence["channel"], evidence["recipient"]) == (
        TEAM,
        CHANNEL,
        "UAUDIENCE",
    )
    assert evidence["readout"] == result["readout"] and evidence["population"] == 4
    assert evidence["aggregate_digest"] == result["aggregate_digest"]
    assert evidence["epochs"] == 3 and evidence["cpu_limit_seconds"] == 3
    assert restored["presented_at"] <= restored["decision"]["decided"]
    assert "private_metadata" not in restored["rendered"]
    assert "route-a" not in json.dumps(restored)
    with journal.db() as db:
        db.execute("DELETE FROM contexts")
    with monkeypatch.context() as clock:
        clock.setattr("time.time", lambda: restored["actionable_until"] + 1)
        assert not journal.inspected(
            ACTOR, restored["decision"]["revision"], h.app._policy(h.app.config(), ACTOR)
        )
        assert journal.receipt(receipt) == restored
    assert journal.receipt(receipt) == restored
    read_again = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.slack_adapter",
            str(h.path),
            "--receipt",
            receipt,
        ],
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTHONPATH": str(Path(slack_adapter.__file__).resolve().parents[2])},
    )
    assert json.loads(read_again.stdout) == restored
    with journal.db() as db:
        db.execute("UPDATE presentations SET rendered='{}' WHERE id=?", (restored["presentation"],))
    with pytest.raises(slack_adapter.Refused):
        journal.receipt(receipt)


def test_evaluation_only_native_evidence_has_no_fit_or_weights(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    data = generated_evaluation_dataset("pilot")
    dataset = data["dataset_id"]
    h.store.register(data)
    h.config["users"][ACTOR]["datasets"] = [dataset]
    h.save()
    view = h.launch()
    assert "ot_tune" not in json.dumps(h.slack.screens[view])
    h.action(view, "ot_browse", "home")
    h.action(view, "ot_dataset_open", dataset)
    screen = h.slack.screens[view]
    assert "ot_tune_dataset" not in json.dumps(screen)
    revision = next(
        b["value"]
        for row in screen["blocks"]
        for b in row.get("elements", [])
        if b.get("action_id") == "ot_evidence"
    )
    h.action(view, "ot_evidence", revision)
    assert "perceptron" not in json.dumps(h.slack.screens[view]).lower()
    h.action(view, "ot_home", "home")
    h.action(view, "ot_changes", revision)
    saved = h.app.journal.receipt(h.app.journal.decision(revision)["receipt"])
    assert saved["evidence"]["mode"] == "evaluation_only"
    assert saved["evidence"]["job_id"] is None and saved["evidence"]["model_revision"] is None


@pytest.mark.parametrize(
    "withdraw",
    ["dataset", "engagement", "recipient", "reviewer", "expiry", "destination", "disable"],
)
def test_queued_decision_checked_again_before_each_export(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, withdraw: str
) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    hook = HOOK
    if withdraw == "dataset":
        h.store.revoke(DATA)
    elif withdraw == "engagement":
        h.store.withdraw()
    elif withdraw == "recipient":
        h.config["recipient"] = "UNEW"
    elif withdraw == "reviewer":
        h.config["reviewer"] = OTHER
    elif withdraw == "expiry":
        h.config["users"][ACTOR]["expires_at"] = 0
    elif withdraw == "destination":
        hook = "https://hooks.zapier.com/hooks/catch/222/rotated/"
        h.config["zapier_destination_sha256"] = slack_adapter.hook_fingerprint(hook)
    else:
        h.config["zapier"] = False
    h.save()

    def unexpected(*a: Any, **kw: Any) -> None:
        pytest.fail("An export was admitted after a hold.")

    monkeypatch.setattr(http.client, "HTTPSConnection", unexpected)
    assert h.flush(hook) == 0
    saved = h.app.journal.receipt(receipt)
    assert saved["decision"]["delivery"] == "suppressed" and saved["decision"]["attempts"] == 0
    assert h.flush(hook) == 0


@pytest.mark.parametrize("ambiguous", [False, True])
def test_withdraw_while_request_in_flight_and_reconcile_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ambiguous: bool
) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    admitted = []

    class Connection:
        status = 200

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> None:
            admitted.append(json.loads(kwargs["body"]))
            h.store.withdraw()
            inventory = slack_adapter.withdraw_for_custody(h.path.parent, "pilot")
            assert inventory["zapier_uncertain"] == 1 and inventory["slack_exposed"] >= 1

        def getresponse(self) -> Connection:
            if ambiguous:
                raise ConnectionResetError()
            return self

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Connection)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: h.flush(), range(2)))
    assert sum(results) == int(not ambiguous) and len(admitted) == 1
    status = "uncertain" if ambiguous else "accepted"
    assert h.app.journal.receipt(receipt)["decision"]["delivery"] == status
    assert h.flush() == 0
    assert (
        slack_adapter.main(
            [str(h.path), "--confirm-zapier", receipt, "--downstream-ref", "owner-log-7"]
        )
        == 0
    )
    assert h.app.journal.receipt(receipt)["decision"]["delivery"] == "downstream_confirmed"
    h.app.journal.confirm_delivery(receipt, "owner-log-7")
    with pytest.raises(slack_adapter.Refused):
        h.app.journal.confirm_delivery(receipt, "another-log")
    assert slack_adapter.withdraw_for_custody(h.path.parent, "pilot")["zapier_accepted"] == 1
    with pytest.raises(slack_adapter.Refused):
        slack_adapter.withdraw_for_custody(h.path.parent, "wrong-organization")


def test_unbound_legacy_journal_holds_pending_and_preserves_possible_copies(tmp_path: Path) -> None:
    root = tmp_path / "legacy"
    root.mkdir(mode=0o700)
    path = root / "slack-pilot.sqlite3"
    identity = json.dumps(["pilot", TEAM, APP, CHANNEL, str(root)], separators=(",", ":"))
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE identity (id INTEGER PRIMARY KEY,binding TEXT,key BLOB);
            CREATE TABLE decisions (revision TEXT PRIMARY KEY, receipt TEXT UNIQUE,actor TEXT,
                disposition TEXT,decided INTEGER,exportable INTEGER,audience TEXT,attempts INTEGER,
                delivery TEXT);
        """)
        db.execute("INSERT INTO identity VALUES(1,?,?)", (identity, b"a" * 32))
        for state in ("pending", "sending", "accepted"):
            db.execute(
                "INSERT INTO decisions VALUES(?,?,?,'approved',7,1,'UAUDIENCE',0,?)",
                (state, state, ACTOR, state),
            )
    path.chmod(0o600)
    journal = slack_adapter.Journal(path, identity)
    assert journal.receipt("pending")["decision"]["delivery"] == "legacy_hold"
    assert journal.receipt("sending")["decision"]["delivery"] == "uncertain"
    assert journal.receipt("accepted")["verification"] == "legacy_unqualified"
    details = slack_adapter.withdraw_for_custody(root, "pilot")
    assert (
        details["legacy_unknown"] == details["zapier_uncertain"] == details["zapier_accepted"] == 1
    )
    assert details == slack_adapter.withdraw_for_custody(root, "pilot")


def test_cli_preserves_legacy_hyphenated_receipts_and_prefixes_new_ones(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    h = Harness(tmp_path)
    monkeypatch.setattr(
        "secrets.token_urlsafe",
        lambda n: "-" + token_urlsafe(n)[1:] if n == 18 else token_urlsafe(n),
    )
    _, receipt = h.approve()
    assert receipt.startswith("receipt_-")
    old = receipt.removeprefix("receipt_")
    journal = h.app.journal
    with journal.db() as db:
        db.execute(
            "UPDATE decisions SET receipt=?,delivery='accepted',attempts=1 WHERE receipt=?",
            (old, receipt),
        )
        db.execute(
            "UPDATE authorizations SET receipt=?,transmitted=decided FROM "
            "(SELECT decided FROM decisions WHERE receipt=?) WHERE receipt=?",
            (old, old, receipt),
        )
        db.execute("DELETE FROM integrity WHERE kind='decision' AND id=?", (receipt,))
        journal._seal(db, "decision", old, create=True)
    assert slack_adapter.main([str(h.path), "--receipt", old]) == 0
    assert json.loads(capsys.readouterr().out)["decision"]["receipt"] == old
    assert (
        slack_adapter.main([str(h.path), "--confirm-zapier", old, "--downstream-ref", "seen-1"])
        == 0
    )
    assert journal.receipt(old)["decision"]["delivery"] == "downstream_confirmed"


def test_changed_audience_holds_confirmed_proposal_and_previous_evidence(tmp_path: Path) -> None:
    h = Harness(tmp_path)
    view, job = h.propose()
    h.config["recipient"] = "UOTHERBUYER"
    h.save()
    h.form(view)
    assert h.store.status(job)["status"] == "awaiting_approval"
    h.config["recipient"] = "UAUDIENCE"
    h.save()
    view = h.launch(prompt=f"status {job}")
    h.form(view)
    h.action(view, "ot_status", job)
    revision = next(
        b["value"]
        for row in h.slack.screens[view]["blocks"]
        for b in row.get("elements", [])
        if b.get("action_id") == "ot_evidence"
    )
    h.action(view, "ot_evidence", revision)
    h.action(view, "ot_home", "home")
    h.config["recipient"] = "UOTHERBUYER"
    h.save()
    h.action(view, "ot_approve", revision)
    assert not h.app.journal.decision(revision)


def test_withdrawal_isolated_for_two_organizations_with_same_ids(tmp_path: Path) -> None:
    a = Harness(tmp_path / "a", "buyer-a")
    b = Harness(tmp_path / "b", "buyer-b")
    _, a_receipt = a.approve()
    _, b_receipt = b.approve()
    a.store.withdraw()
    assert slack_adapter.withdraw_for_custody(a.path.parent, "buyer-a")["zapier_suppressed"] == 1
    assert a.app.journal.receipt(a_receipt)["decision"]["delivery"] == "suppressed"
    assert b.app.journal.receipt(b_receipt)["decision"]["delivery"] == "pending"
    assert b.store.dataset_summary(DATA)["can_train"]
    with pytest.raises(slack_adapter.Refused):
        slack_adapter.withdraw_for_custody(b.path.parent, "buyer-a")


@pytest.mark.parametrize("damage", ["missing", "tampered", "heldout", "consistent_wrong_cache"])
@pytest.mark.parametrize("when", ["before_open", "after_open", "already_recorded"])
def test_local_model_must_still_match_when_evidence_or_decision_is_requested(
    tmp_path: Path, damage: str, when: str
) -> None:
    h = Harness(tmp_path)
    view, job, revision = h.completed()
    if when != "before_open":
        h.action(view, "ot_evidence", revision)
        h.action(view, "ot_home", "home")
    if when == "already_recorded":
        h.action(view, "ot_approve", revision)
    with sqlite3.connect(h.store.path) as db:
        if damage == "missing":
            db.execute("DELETE FROM artifacts")
        elif damage == "tampered":
            db.execute("UPDATE artifacts SET model='{}'")
        elif damage == "heldout":
            db.execute("UPDATE heldout_data SET content='[]'")
        else:
            altered = json.loads(
                db.execute("SELECT result FROM jobs WHERE id=?", (job,)).fetchone()[0]
            )
            report = altered["readout"]
            report["error_ceiling_bps"] += 1
            altered["aggregate_digest"] = hashlib.sha256(
                (json.dumps(report, sort_keys=True) + "\n").encode()
            ).hexdigest()
            altered["brief_digest"] = hashlib.sha256(decision_brief(report).encode()).hexdigest()
            db.execute("UPDATE jobs SET result=? WHERE id=?", (json.dumps(altered), job))
    if when == "before_open":
        h.action(view, "ot_evidence", revision)
        assert h.slack.screens[view].get("callback_id") != "ot_evidence_view"
    elif when == "after_open":
        h.action(view, "ot_approve", revision)
    saved = h.app.journal.decision(revision)
    if when == "already_recorded":
        archived = h.app.journal.receipt(saved["receipt"])
        assert archived["evidence"]["job_id"] == job
    else:
        assert not saved
    view = h.launch(prompt=f"status {job}")
    screen = json.dumps(h.slack.screens[view])
    assert "Pending verification" in screen and "ot_evidence" not in screen


def test_auto_dispatch_runs_after_decision_off_callback_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    started, release = threading.Event(), threading.Event()
    bodies = []

    class Receiver:
        status = 200

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> None:
            bodies.append(json.loads(kwargs["body"]))
            started.set()
            assert release.wait(4)

        def getresponse(self) -> Receiver:
            return self

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Receiver)
    drainer = slack_adapter.ReceiptDrainer(
        h.app.journal, h.app.config, h.store, lambda: HOOK, interval=60
    )
    h.app.on_decision = drainer.wake
    drainer.start()
    try:
        _, receipt = h.approve()
        assert started.wait(3)
        saved = h.app.journal.receipt(receipt)
        assert saved["decision"]["disposition"] == "approved"
        assert saved["decision"]["delivery"] == "sending"
    finally:
        release.set()
        drainer.close()
    assert bodies == [{"event": "pilot_decision_recorded", "receipt": receipt}]
    assert h.app.journal.receipt(receipt)["decision"]["delivery"] == "accepted"


@pytest.mark.parametrize(
    "field", ["readout_revision", "viewer", "presented", "disposition", "destination"]
)
def test_damaged_private_chain_cannot_authorize_export(
    tmp_path: Path, field: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    with h.app.journal.db() as db:
        if field in {"readout_revision", "viewer"}:
            proof = db.execute("SELECT snapshot FROM presentations").fetchone()
            changed = json.loads(proof[0])
            changed[field] = "unrelated"
            db.execute("UPDATE presentations SET snapshot=?", (json.dumps(changed),))
        elif field == "presented":
            db.execute("UPDATE presentations SET presented=presented+7200")
        elif field == "destination":
            db.execute('UPDATE authorizations SET fields=\'["event","receipt","disposition"]\'')
        else:
            db.execute("UPDATE decisions SET disposition='changes_requested'")
    with pytest.raises(slack_adapter.Refused):
        h.app.journal.receipt(receipt)

    def stopped(*a: Any, **kw: Any) -> None:
        pytest.fail("Damaged evidence authorized an export.")

    monkeypatch.setattr(http.client, "HTTPSConnection", stopped)
    assert h.flush() == 0


@pytest.mark.parametrize("mode", ["uncertain", "withdrawn", "disabled", "missing_secret"])
def test_background_startup_rechecks_pending_and_never_retries_an_admitted_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    assert (
        h.app.on_decision is None
        and h.app.journal.receipt(receipt)["decision"]["delivery"] == "pending"
    )
    hook = HOOK
    if mode == "withdrawn":
        h.store.withdraw()
    elif mode == "disabled":
        h.config["zapier"] = False
        h.save()
    elif mode == "missing_secret":
        hook = ""
    calls = []

    class Receiver:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> None:
            calls.append(json.loads(kwargs["body"]))
            raise ConnectionResetError()

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Receiver)
    completed = threading.Event()
    actual_dispatcher = slack_adapter.flush_receipts

    def observe(*args: Any, **kwargs: Any) -> int:
        try:
            return actual_dispatcher(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(slack_adapter, "flush_receipts", observe)
    for _ in range(2):
        completed.clear()
        journal = slack_adapter.Journal(
            h.app.journal.path, slack_adapter.Settings.load(h.path).binding
        )
        drain = slack_adapter.ReceiptDrainer(
            journal, h.app.config, h.store, lambda: hook, interval=60
        )
        drain.start()
        try:
            assert completed.wait(3)
        finally:
            drain.close()
    assert len(calls) == int(mode == "uncertain")
    expected = {"uncertain": "uncertain", "missing_secret": "pending"}.get(mode, "suppressed")
    assert h.app.journal.receipt(receipt)["decision"]["delivery"] == expected


def test_automatic_worker_recovers_after_a_malformed_local_grant(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = Harness(tmp_path)
    _, receipt = h.approve()
    original = h.config["directory"]
    h.config["directory"] = False
    h.save()
    scanned = threading.Event()
    submitted = []

    class Receiver:
        status = 200

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def request(self, *args: Any, **kwargs: Any) -> None:
            submitted.append(json.loads(kwargs["body"]))

        def getresponse(self) -> Receiver:
            return self

        def close(self) -> None:
            pass

    monkeypatch.setattr(http.client, "HTTPSConnection", Receiver)
    dispatcher = slack_adapter.flush_receipts

    def observe(*args: Any, **kw: Any) -> int:
        try:
            return dispatcher(*args, **kw)
        finally:
            scanned.set()

    monkeypatch.setattr(slack_adapter, "flush_receipts", observe)
    worker = slack_adapter.ReceiptDrainer(
        h.app.journal, h.app.config, h.store, lambda: HOOK, interval=60
    )
    worker.start()
    try:
        assert scanned.wait(3) and not submitted
        scanned.clear()
        h.config["directory"] = original
        h.save()
        worker.wake()
        assert scanned.wait(3)
    finally:
        worker.close()
    assert submitted == [{"event": "pilot_decision_recorded", "receipt": receipt}]
