"""Generated P1 workflow continues from blind starter freeze to independent live truth."""

import hmac
import json
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, replace

from opentour_cell.pilot import live_reader
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import IntakePrincipal, parse_intake
from opentour_cell.pilot.live_authority import SCHEMA, SELECTION_SCHEMA, PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence, _canonical, _digest
from test_app_home import publish
from test_intake_mcp import _write_private
from test_invoice_decision_emitter import attempt, commit, emit, export_page, scope_file
from test_live_evidence import DECISION, OUTCOME
from test_live_settings import KEY, form, open_live, sign
from test_reviewer_workflow import buttons, restart, screen, text
from test_slack_adapter import ACTOR, OTHER
from test_starter_setup import effects
from test_verifier_form import open_form, submission

pytest_plugins = ("test_starter_setup", "test_p1_starter_journey")


def install_application_page(directory, body, source, receipt, page):
    filename = receipt["file"]
    shutil.copyfile(source.parent / filename, directory / filename)
    (directory / filename).chmod(0o600)
    rows = page["rows"]
    metadata = dict(
        interval_start=50, interval_end=100, coverage_through=100, complete=True, next_cursor=None
    )
    entry = {
        "pin": {"file": filename, "sha256": receipt["sha256"]},
        "page_ref": page["page_ref"],
        "checkpoint": receipt["checkpoint"],
        "read_metadata": metadata,
        "page_digest": _digest(
            [
                DECISION.source_revision,
                [r["event"] for r in rows],
                [r["position"] for r in rows],
                [r["epoch"] for r in rows],
                receipt["checkpoint"],
                metadata,
            ]
        ),
    }
    catalog = {
        "schema_version": live_reader.CATALOG,
        "scope": {
            k: body[k]
            for k in (
                "tenant",
                "workflow_ref",
                "engagement_epoch",
                "dataset_ref",
                "dataset_revision",
            )
        },
        "grant": asdict(DECISION),
        "event_revisions": "versioned",
        "pages": [entry],
    }
    file = "live-export-catalog-decisions.json"
    _write_private(directory / file, catalog)
    mapping = {
        "schema_version": live_reader.MAPPING,
        "authority_revision": _digest(body),
        "resources": {DECISION.resource: {"file": file, "sha256": _digest(catalog)}},
    }
    _write_private(
        directory / f"live-export-{body['workflow_ref']}.json",
        {
            "mapping": mapping,
            "seal_sha256": hmac.digest(KEY, _canonical(mapping).encode(), "sha256").hex(),
        },
    )


def test_p1_invoice_commit_then_independent_truth(p1_frozen_from_home, monkeypatch, tmp_path):
    h, owner, draft, _maps, _confirmed, pack = p1_frozen_from_home
    directory = h.path.parent
    workflow = draft["workflow_ref"]
    tenant, epoch = h.store.organization, h.store.engagement_id
    h.config["live_workflow_ref"] = workflow
    h.save()
    body = dict(
        schema_version=SCHEMA,
        tenant=tenant,
        workflow_ref=workflow,
        engagement_epoch=epoch,
        dataset_ref="dataset-a",
        dataset_revision="dataset-v1",
        custodian=ACTOR,
        expires_at=int(time.time()) + 3600,
        state="current",
        actors={ACTOR: ["custodian", "inspect"], "worker": ["intake"]},
        grants=[asdict(DECISION)],
    )
    _write_private(directory / "live-authority-key.json", {"key": KEY.hex()})
    sign(h, body)
    view = open_live(h, monkeypatch)
    h.form(view, form(h, view))
    assert "Saved binding" in text(h, view)
    authority = PrivateLiveAuthority(directory, tenant, workflow)
    actor = IntakePrincipal(tenant, ACTOR, "slack")
    live = LiveEvidence(directory, authority)
    [(binding, _)] = live.current_bindings(actor, workflow)
    scope = scope_file(tmp_path / "application", workflow=workflow, epoch=epoch)
    selected = json.loads(scope.read_bytes())
    _write_private(scope, selected | {"tenant": tenant})
    emit(scope, "initialize")
    attempt(scope, "operation-D1", "attempt-1")
    commit(scope, "D1", "attempt-1", action="SUPPLIES", success=False)
    commit(scope, "D1", "attempt-1", action="review")
    receipt, page = export_page(scope, "invoice-page", 10, 11)
    install_application_page(directory, body, scope, receipt, page)
    selection = directory / "live-worker.json"
    _write_private(
        selection,
        dict(schema_version=SELECTION_SCHEMA, tenant=tenant, workflow_ref=workflow, actor="worker"),
    )
    worker, intake = open_intake(selection)
    request = dict(
        mode="live",
        feed_binding_ref=binding,
        source_revision=DECISION.source_revision,
        idempotency_key="p1-app-committed-decision",
    )
    admitted = intake.submit_intake(worker, parse_intake(request))
    for status in ("materialized", "already_materialized"):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                "-m",
                "opentour_cell.pilot.live_reader",
                str(selection),
                admitted.run_ref,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert (completed.returncode, completed.stdout.strip(), completed.stderr) == (0, status, "")

    def snapshot(counts):
        current = live.snapshot(actor, workflow)
        assert (current.decisions, current.verified, current.pending, current.disputed) == counts
        assert (current.workflow_ref, current.engagement_epoch) == (workflow, epoch)
        return current

    snapshot((1, 0, 1, 0))
    assert "ot_app_verify_outcome" not in buttons(publish(h, monkeypatch, OTHER))
    body["grants"].append(asdict(replace(OUTCOME, verifier=OTHER)))
    body["actors"][OTHER] = ["verify"]
    body["slack_verifiers"] = {OUTCOME.resource: OTHER}
    sign(h, body)
    view = open_live(h, monkeypatch)
    h.form(view, form(h, view, 1))
    assert "Saved binding" in text(h, view)
    restart(h)

    class VerifierHome:
        def __init__(self):
            self.h = h

        def home(self, user):
            return publish(h, monkeypatch, user)

    verifier = VerifierHome()

    def verify(state):
        view = open_form(verifier)
        data = submission(verifier, view, state=state)
        if state == "final":
            data["action"]["action"]["value"] = "FREIGHT"
        result = h.form(view, data, OTHER)
        assert screen(h, view)["callback_id"] == "ot_verified_outcome"
        return result

    original = verify("final")
    first = snapshot((1, 1, 0, 0))
    [root] = first.verified_roots
    assert (root.decision_id, root.action_ref, root.verified_action_ref) == (
        "D1",
        "review",
        "FREIGHT",
    )
    verify("reopened")
    snapshot((1, 0, 1, 0))
    h.send("interactive", original)
    snapshot((1, 0, 1, 0))
    restart(h)
    verify("final")
    assert snapshot((1, 1, 0, 0)).verified_roots == first.verified_roots
    assert live.snapshot(actor, workflow, as_of_revision=first.sequence) == first
    assert owner.review() == pack
    assert effects(h) == (0,) * 7
