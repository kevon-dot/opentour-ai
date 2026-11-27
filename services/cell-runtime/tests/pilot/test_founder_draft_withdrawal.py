"""Exercise custody after an actual founder draft is saved and resumed."""

import hashlib
import http.client
import json
import os
import sqlite3
import subprocess
import sys
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import workplace_context
from opentour_cell.pilot.custody import Closeout, CustodyError
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import GET_RUN, IntakeError, parse_intake
from opentour_cell.pilot.mcp import PROTOCOL_VERSION
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.training import LocalTrainingStore
from test_app_home import click, publish
from test_intake_mcp import STARTER
from test_pilot_custody import START, items, record
from test_reviewer_workflow import buttons, restart, screen, text
from test_starter_setup import configure_starter, open_starter
from test_workflow_benchmarks import DiskProducer
from test_workflow_chooser import Founder

Json = dict[str, Any]


def custody_cli(founder: Founder, command: str, **options: str) -> Json:
    args = [f"--{key.replace('_', '-')}={value}" for key, value in options.items()]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.custody",
            "--directory",
            str(founder.path.parent),
            "--org",
            founder.store.organization,
            command,
            *args,
        ],
        check=True,
        capture_output=True,
        timeout=20,
        text=True,
    )
    assert not result.stderr
    return json.loads(result.stdout)


def copy_database(source: Path, destination: Path) -> None:
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(destination)) as dst:
        src.backup(dst)


def intake_runs(directory: Path) -> list[tuple[str, str, str]]:
    with closing(sqlite3.connect(directory / "intake.sqlite3")) as db:
        assert db.execute("SELECT count(*) FROM intake_workflows").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM intake_keys").fetchone() == (1,)
        assert db.execute("SELECT count(*) FROM intake_materialization").fetchone() == (0,)
        return db.execute("SELECT tenant,workflow_ref,run_ref FROM intake_runs").fetchall()


def read_run(directory: Path, run_ref: str) -> subprocess.CompletedProcess[str]:
    frames = [
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "generated-founder-custody", "version": "1"},
            },
        },
        {"method": "notifications/initialized"},
        {
            "id": 2,
            "method": "tools/call",
            "params": {"name": GET_RUN, "arguments": {"run_ref": run_ref}},
        },
    ]
    stdin = "".join(json.dumps({"jsonrpc": "2.0", **frame}) + "\n" for frame in frames)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.intake_local",
            str(directory / "selected.json"),
        ],
        input=stdin,
        capture_output=True,
        timeout=20,
        text=True,
    )


def assert_queued(directory: Path, saved: Json) -> None:
    process = read_run(directory, saved["run_ref"])
    assert process.returncode == 0 and not process.stderr, process.stderr
    reply = json.loads(process.stdout.splitlines()[-1])
    assert reply["id"] == 2 and not reply["result"].get("isError")
    receipt = reply["result"]["structuredContent"]
    assert receipt["state"] == "queued"
    assert receipt["workflow_ref"] == saved["workflow_ref"]


def test_saved_founder_draft_stays_held_after_custody_and_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Founder(tmp_path / "a", local_recipient=False, organization="generated-draft-a")
    b = Founder(tmp_path / "b", local_recipient=False, organization="generated-draft-b")
    left, right = a.path.parent, b.path.parent
    destination = tmp_path / "generated-destination.sqlite3"
    with closing(sqlite3.connect(destination)) as db, db:
        db.execute("CREATE TABLE posts (tenant TEXT NOT NULL)")
        db.execute("CREATE TABLE private_views (tenant TEXT NOT NULL, kind TEXT NOT NULL)")

    def record_view(tenant: str, callback: str) -> None:
        with closing(sqlite3.connect(destination)) as db, db:
            db.execute("INSERT INTO private_views VALUES (?,?)", (tenant, callback))

    def record_home(h: Founder) -> Json:
        published = h.slack.views_publish
        published.assert_called_once()
        layout = published.call_args.kwargs["view"]
        assert layout["type"] == "home"
        assert published.return_value["ok"]
        record_view(h.store.organization, "app_home")
        return layout

    def install_transport_observers(h: Founder) -> None:
        tenant = h.store.organization
        post = h.slack.chat_postMessage
        update = h.slack.views_update

        def record_post(**payload: Any) -> Json:
            with closing(sqlite3.connect(destination)) as db, db:
                db.execute("INSERT INTO posts VALUES (?)", (tenant,))
            return post(**payload)

        def record_update(**payload: Any) -> Json:
            response = update(**payload)
            callback = payload["view"]["callback_id"]
            if response["ok"] and callback in ("ot_starter_preview", "ot_starter_saved"):
                record_view(tenant, callback)
            return response

        monkeypatch.setattr(h.slack, "chat_postMessage", record_post)
        monkeypatch.setattr(h.slack, "views_update", record_update)

    def deny_workplace_read(*args: Any, **kwargs: Any) -> None:
        pytest.fail("A policy-only draft may not dispatch a workplace reader.")

    for entry in ("discover_workplace", "acquire_workplace"):
        monkeypatch.setattr(workplace_context, entry, deny_workplace_read)
    try:
        for h in (a, b):
            policy = h.path.parent / "invoice-policy.md"
            policy.chmod(0o600)
            configure_starter(h, source_revision=hashlib.sha256(policy.read_bytes()).hexdigest())
            install_transport_observers(h)
            assert h.store.catalog() == []

        a_view = open_starter(a, monkeypatch)
        record_home(a)
        stale_save = a.action(
            a_view, "ot_starter_save", buttons(screen(a, a_view))["ot_starter_save"]
        )
        b_view = open_starter(b, monkeypatch)
        record_home(b)
        b.action(b_view, "ot_starter_save", buttons(screen(b, b_view))["ot_starter_save"])
        assert (
            screen(a, a_view)["callback_id"]
            == screen(b, b_view)["callback_id"]
            == "ot_starter_saved"
        )
        a_path = next(left.glob("source-intake-starter-draft-*.json"))
        b_path = next(right.glob("source-intake-starter-draft-*.json"))
        a_draft, b_draft = (json.loads(path.read_bytes()) for path in (a_path, b_path))
        selections = [json.loads((d / "selected.json").read_bytes()) for d in (left, right)]
        assert selections[0]["grant_ref"] == selections[1]["grant_ref"]
        assert a_draft["mandate"]["owner_ref"] == b_draft["mandate"]["owner_ref"]
        for key in ("workflow_ref", "run_ref", "draft_ref"):
            assert a_draft[key] != b_draft[key]
        [a_run], [b_run] = intake_runs(left), intake_runs(right)
        assert a_run == (a.store.organization, a_draft["workflow_ref"], a_draft["run_ref"])
        assert b_run == (b.store.organization, b_draft["workflow_ref"], b_draft["run_ref"])
        assert_queued(left, a_draft)
        assert_queued(right, b_draft)
        restart(a)
        resumed = open_starter(a, monkeypatch)
        old_home = record_home(a)
        assert a_draft["draft_ref"] in text(a, resumed)
        a.assert_no_effects()
        b.assert_no_effects()

        prior_training = tmp_path / "training-before-withdrawal.sqlite3"
        prior_intake = tmp_path / "intake-before-withdrawal.sqlite3"
        copy_database(a.store.path, prior_training)
        copy_database(left / "intake.sqlite3", prior_intake)
        authority_files = {
            path: (path.read_bytes(), path.stat().st_mode & 0o777)
            for path in (
                left / "selected.json",
                left / "starter-intake-key.json",
                left / "starter-intake-grant-policy-scope.json",
                left / "charter.json",
                a_path,
            )
        }
        epoch = a.store.engagement_id
        stopped = custody_cli(a, "withdraw", **START)
        assert stopped["engagement"] == epoch and stopped["reconciled"]
        assert not a.store.engagement_active() and b.store.engagement_active()
        assert items(stopped)["source-intake-copies"]["kind"] == "source"
        assert items(stopped)["slack-copies"]["kind"] == "destination"
        copies = stopped["delivery_inventory"]
        assert copies["slack_exposed"] == 5 and copies["slack_possible"] == 0
        assert copies["zapier_accepted"] == copies["zapier_uncertain"] == copies["decisions"] == 0

        # Restore the same pre-withdrawal working state without replacing current custody.
        copy_database(prior_training, a.store.path)
        copy_database(prior_intake, left / "intake.sqlite3")
        for path, (raw, mode) in authority_files.items():
            path.write_bytes(raw)
            path.chmod(mode)
        unread = (left / "invoice-policy.md", left / "charter.json", a_path)
        for path in unread:
            os.utime(path, ns=(1, path.stat().st_mtime_ns))
        a.store = LocalTrainingStore(a.store.path, a.store.organization)
        restart(a)
        before = dict(a.slack.screens)
        click(a, old_home, "ot_app_add_workflow")
        a.send("interactive", stale_save)
        home = publish(a, monkeypatch)
        assert a.slack.screens == before
        assert not home or "ot_app_add_workflow" not in buttons(home)
        home_text = json.dumps(home)
        assert all(
            a_draft[key] not in home_text for key in ("draft_ref", "workflow_ref", "run_ref")
        )
        denied = read_run(left, a_draft["run_ref"])
        assert denied.returncode == 2 and not denied.stdout
        assert denied.stderr.strip() == "private_intake_selection_unavailable"
        assert not a.store.engagement_active()
        assert intake_runs(left) == [a_run]
        assert all(path.stat().st_atime_ns == 1 for path in unread)

        restart(b)
        other = open_starter(b, monkeypatch)
        assert b_draft["draft_ref"] in text(b, other)
        assert a_draft["draft_ref"] not in text(b, other)
        assert b.store.engagement_active() and intake_runs(right) == [b_run]
        assert_queued(right, b_draft)
        assert all(path.stat().st_atime_ns == 1 for path in unread)
        for path in unread:
            path.read_bytes()
            assert path.stat().st_atime_ns > 1
        a.assert_no_effects()
        b.assert_no_effects()

        with closing(sqlite3.connect(destination)) as db:
            assert db.execute("SELECT tenant FROM posts").fetchall() == []
            views = db.execute(
                "SELECT kind FROM private_views WHERE tenant=? ORDER BY rowid",
                (a.store.organization,),
            ).fetchall()
        assert sorted(views) == [
            ("app_home",),
            ("app_home",),
            ("ot_starter_preview",),
            ("ot_starter_saved",),
            ("ot_starter_saved",),
        ]

        inventory_digest = digest(
            {
                "private_slack_views": views,
                "destination_posts": [],
                "local_draft": digest(a_draft),
                "retained_training": hashlib.sha256(prior_training.read_bytes()).hexdigest(),
                "retained_intake": hashlib.sha256(prior_intake.read_bytes()).hexdigest(),
            }
        )
        c = Closeout(left, a.store.organization)
        with pytest.raises(CustodyError, match="closeout_obligations_open"):
            c.finish(custodian=START["custodian"], evidence=inventory_digest)
        for item, kind in (
            ("private-starter-authority", "grant"),
            ("private-starter-input", "source"),
            ("prior-workspace-databases", "backup"),
        ):
            record(c, item, kind, "custody_transferred", evidence=inventory_digest)
        for item, row in items(stopped).items():
            kind = row["kind"]
            disposition = {
                "inventory": "inventory_verified",
                "support": "handoff_confirmed",
            }.get(kind, "custody_transferred")
            record(c, item, kind, disposition, evidence=inventory_digest)
        terminal = custody_cli(a, "finish", custodian=START["custodian"], evidence=inventory_digest)
        assert terminal["state"] == "closed" and terminal["outstanding"] == 0
        assert terminal["engagement"] == epoch and terminal["closure_evidence"] == inventory_digest
        assert "export_disposition" not in terminal
        assert items(terminal)["slack-copies"]["disposition"] == "custody_transferred"
        assert custody_cli(a, "status") == terminal
    finally:
        a.app.pool.shutdown()
        b.app.pool.shutdown()


def test_frozen_founder_benchmark_withdrawal_has_one_zero_export_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    a = Founder(tmp_path / "first", local_recipient=False, organization="founder-a")
    b = Founder(tmp_path / "other", local_recipient=False, organization="founder-b")
    destination = tmp_path / "generated-recipient.sqlite3"
    with closing(sqlite3.connect(destination)) as db, db:
        db.execute("CREATE TABLE copies (organization TEXT NOT NULL, reference TEXT NOT NULL)")

    def observe_copy(organization, reference):
        with closing(sqlite3.connect(destination)) as db, db:
            db.execute("INSERT INTO copies VALUES (?,?)", (organization, reference))

    def zapier_transport(*args, **kwargs):
        observe_copy("unknown", "zapier")
        pytest.fail("A frozen starter must not connect to a release destination.")

    def slack_transport(organization):
        def send(**payload):
            observe_copy(organization, "slack")
            pytest.fail("A frozen starter must not post to Slack.")

        return send

    monkeypatch.setattr(http.client, "HTTPSConnection", zapier_transport)
    for founder in (a, b):
        monkeypatch.setattr(
            founder.slack, "chat_postMessage", slack_transport(founder.store.organization)
        )

    def prepare(founder: Founder):
        directory = founder.path.parent
        policy = directory / "invoice-policy.md"
        revision = hashlib.sha256(policy.read_bytes()).hexdigest()
        policy.chmod(0o600)
        configure_starter(founder, source_revision=revision)
        principal, intake = open_intake(directory / "selected.json")
        request = parse_intake(
            STARTER
            | {
                "source_revision": revision,
                "setup_policy_revision": digest(
                    json.loads((directory / "charter.json").read_bytes())
                ),
            }
        )
        receipt = intake.submit_intake(principal, request)
        return principal, intake, request, receipt

    try:
        first_actor, first, first_request, first_run = prepare(a)
        other_actor, other, other_request, other_run = prepare(b)
        assert first_request == other_request and first_run.run_ref != other_run.run_ref
        directory = a.path.parent
        producer = DiskProducer(directory)
        producer.state = "frozen_unscored"
        assert first.materialize(
            first_actor, first_run.run_ref, producer, target_state="frozen_unscored"
        )
        frozen = first.get_run(first_actor, first_run.run_ref).benchmark
        assert frozen.benchmark_state == "frozen_unscored" and producer.calls == 1
        normal = read_run(directory, first_run.run_ref)
        assert normal.returncode == 0 and not normal.stderr
        current = json.loads(normal.stdout.splitlines()[-1])["result"]["structuredContent"]
        assert current["benchmark_ref"] == frozen.benchmark_ref
        assert current["benchmark_state"] == "frozen_unscored"

        backup = tmp_path / "generated-pre-withdrawal-training.sqlite3"
        copy_database(a.store.path, backup)
        ledger = directory / "workflow-benchmarks.sqlite3"
        frozen_before = ledger.read_bytes()
        publication_count = (directory / "generated-producer-count").read_text()
        epoch = a.store.engagement_id
        stopped = custody_cli(a, "withdraw", **START)
        assert stopped["engagement"] == epoch and stopped["reconciled"]
        assert items(stopped)["benchmark-records"]["disposition"] == "unresolved"
        assert not any(row["kind"] == "destination" for row in stopped["items"])
        assert stopped["delivery_inventory"]["decisions"] == 0
        copy_database(backup, a.store.path)
        a.store = LocalTrainingStore(a.store.path, a.store.organization)
        restart(a)

        denied = read_run(directory, first_run.run_ref)
        assert denied.returncode == 2 and not denied.stdout
        assert denied.stderr.strip() == "private_intake_selection_unavailable"
        with pytest.raises(IntakeError):
            first.materialize(
                first_actor, first_run.run_ref, producer, target_state="frozen_unscored"
            )
        with pytest.raises(IntakeError):
            first.submit_intake(first_actor, first_request)
        assert producer.calls == 1 and producer.readbacks == 0
        assert (directory / "generated-producer-count").read_text() == publication_count
        assert ledger.read_bytes() == frozen_before
        assert not a.store.engagement_active()
        assert other.submit_intake(other_actor, other_request) == other_run
        assert other.get_run(other_actor, other_run.run_ref).benchmark.benchmark_state == "draft"
        assert b.store.engagement_active()
        assert a.flush() == b.flush() == 0
        a.assert_no_effects()
        b.assert_no_effects()

        with closing(sqlite3.connect(destination)) as db:
            assert db.execute("SELECT organization,reference FROM copies").fetchall() == []
        evidence = digest(
            {
                "epoch": epoch,
                "frozen_register": hashlib.sha256(frozen_before).hexdigest(),
                "prior_training": hashlib.sha256(backup.read_bytes()).hexdigest(),
                "recipient_inventory": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )
        report = custody_cli(a, "reconcile")
        closeout = Closeout(directory, a.store.organization)
        record(
            closeout, "pre-withdrawal-backup", "backup", "custody_transferred", evidence=evidence
        )
        for item, row in items(report).items():
            disposition = {
                "inventory": "inventory_verified",
                "support": "handoff_confirmed",
            }.get(row["kind"], "custody_transferred")
            record(closeout, item, row["kind"], disposition, evidence=evidence)
        finished = custody_cli(a, "finish", custodian=START["custodian"], evidence=evidence)
        assert finished["engagement"] == epoch and finished["closure_evidence"] == evidence
        assert (finished["state"], finished["outstanding"], finished["export_disposition"]) == (
            "closed",
            0,
            "zero_exports",
        )
        assert custody_cli(a, "finish", custodian=START["custodian"], evidence=evidence) == finished
        assert custody_cli(a, "status") == finished
    finally:
        a.app.pool.shutdown()
        b.app.pool.shutdown()
