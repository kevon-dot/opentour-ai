"""Generated P1 workflow continues from blind starter freeze to independent live truth."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

import pytest
from opentour_cell.pilot import live_reader, local_decision_context, model_connector, model_registry
from opentour_cell.pilot import text_classifier as cpu
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import DISCOVER, GET_RUN, SUBMIT, IntakePrincipal, parse_intake
from opentour_cell.pilot.live_authority import SCHEMA, SELECTION_SCHEMA, PrivateLiveAuthority
from opentour_cell.pilot.live_evidence import LiveEvidence, _canonical, _digest
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readiness_selection import SCHEMA as READINESS_SCHEMA
from opentour_cell.pilot.readiness_selection import open_readiness
from opentour_cell.pilot.repair_bundle import _bytes, _crypto
from test_app_home import publish
from test_intake_mcp import _write_private
from test_invoice_decision_emitter import (
    attempt,
    commit,
    emit,
    export_page,
    scope_file,
    sdk_actions,
)
from test_live_authority import enroll_ledger
from test_live_evidence import DECISION, OUTCOME
from test_live_readiness import Case
from test_live_settings import KEY, form, open_live, sign
from test_p1_starter_journey import _stored_score, score_p1_from_frozen
from test_reviewer_workflow import buttons, restart, screen, text
from test_slack_adapter import ACTOR, DATA, OTHER
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


def record_application_observation(tmp_path, scope, directory, permission, registration):
    producer = "generated-invoice-application"
    registry_path = directory / "model-registration.json"
    app_grant = registration["grants"][0] | {"actor": producer, "revision": "app-baseline"}
    _write_private(registry_path, registration | {"grants": [*registration["grants"], app_grant]})
    registry = model_registry.ModelRegistry(registry_path, permission.tenant)
    binding = registry.binding(permission.model_ref)
    current = registry.current_grant(producer, binding, model_connector.Purpose.BASELINE)
    assert current is not None and current.revision == "app-baseline"

    facts = {"category": "carrier_freight", "po": "po-a"}
    assert set(facts) == set(current.permitted_fields)
    raw = (directory / "generated-p1-model.json").read_bytes()
    assert "sha256-" + hashlib.sha256(raw).hexdigest() == binding.immutable_revision
    assert binding.preprocessing_revision == cpu.PROFILE
    action = cpu.predict(json.loads(raw), canonical(facts)).lower()
    assert action == "supplies"
    assert registry.current_grant(producer, binding, model_connector.Purpose.BASELINE) == current

    signer = tmp_path / "app-signing.pem"
    signer.touch(mode=0o600)
    signer.write_bytes(
        _crypto(["genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"], b"", "keygen")
    )
    public = _crypto(["pkey", "-in", str(signer), "-pubout"], b"", "public_key")
    public_file = scope.parent / "observation-public.pem"
    public_file.touch(mode=0o600)
    public_file.write_bytes(public)
    selection = json.loads(scope.read_bytes())
    identity = {
        key: selection[key]
        for key in ("tenant", "workflow", "engagement_epoch", "namespace", "policy_ref")
    } | {
        "grant_ref": "app-observation-v1",
        "producer_ref": producer,
        "model_ref": binding.model_ref,
        "model_revision": binding.immutable_revision,
    }
    _write_private(
        scope.parent / "invoice-observation-selection.json",
        identity
        | {
            "schema_version": "opentour.example.invoice-observation-selection.v1",
            "public_key_file": public_file.name,
            "public_key_sha256": hashlib.sha256(public).hexdigest(),
            "expires_at": int(time.time()) + 600,
        },
    )
    claim = identity | {
        "schema_version": "opentour.example.invoice-observation.v1",
        "operation": "operation-D1",
        "attempt": "attempt-1",
        "decision": "D1",
        "case": "generated-invoice-1",
        "family": "generated-supplier-1",
        "decision_at": 100,
        "action": action,
        "input_ref": "app-input-"
        + hmac.digest(secrets.token_bytes(32), _bytes(facts), "sha256").hex(),
        "receipt_ref": "application-observation-1",
    }
    signed = _crypto(["dgst", "-sha256", "-sign", str(signer)], _bytes(claim), "sign_observation")
    observation = "app-observation-receipt.json"
    _write_private(
        scope.parent / observation,
        {"claim": claim, "signature": base64.b64encode(signed).decode("ascii")},
    )
    assert not signer.is_relative_to(scope.parent)
    body = {
        "schema_version": local_decision_context.BODY,
        "records": [
            dict(
                decision_id=claim["decision"],
                decision_revision=1,
                case_id=claim["case"],
                family_id=claim["family"],
                input_ref=claim["input_ref"],
                action_ref=action,
                policy_ref=claim["policy_ref"],
                input_fidelity="observed",
                receipt_ref=claim["receipt_ref"],
                features=facts,
                incumbent={"status": "automatic", "label": action},
            )
        ],
    }
    _write_private(directory / "decision-context-body.json", body)
    return observation, claim, digest(body)


def configure_readiness(directory, tmp_path, scope, context_digest):
    workflow = scope["workflow_ref"]
    policy = dict(
        Case(tmp_path / "policy-template").policy,
        organization=scope["tenant"],
        workflow_ref=workflow,
        engagement=scope["engagement_epoch"],
        dataset_ref=scope["dataset_ref"],
        dataset_revision=scope["dataset_revision"],
        feature="category",
        allowed_features=["category", "po"],
        rule_mapping={"carrier_freight": "FREIGHT"},
        development_families=["generated-supplier-1"],
        labels=["supplies", "FREIGHT"],
    )
    _write_private(directory / "p1-repair-policy.json", policy)
    grant = dict(
        schema_version=local_decision_context.GRANT,
        grant_ref="p1-context-v1",
        authority_revision=digest(scope),
        tenant=scope["tenant"],
        workflow_ref=workflow,
        engagement_epoch=scope["engagement_epoch"],
        worker_ref="worker",
        resource=DECISION.resource,
        grant_revision=DECISION.revision,
        source_revision=DECISION.source_revision,
        deciding_namespace=DECISION.deciding_namespace,
        allowed_fields=["category", "po"],
        expires_at=int(time.time()) + 900,
        body={"file": "decision-context-body.json", "sha256": context_digest},
    )
    _write_private(
        directory / "decision-context-grant.json",
        {
            "grant": grant,
            "seal_sha256": hmac.digest(KEY, canonical(grant).encode(), "sha256").hex(),
        },
    )
    _write_private(
        directory / "readiness-selection.json",
        dict(
            schema_version=READINESS_SCHEMA,
            workflow_ref=workflow,
            worker_ref="worker",
            policy_file="p1-repair-policy.json",
            context_file="decision-context-grant.json",
        ),
    )


@pytest.mark.parametrize(
    ("observed", "p1_frozen_from_home"),
    [(False, False), (True, False), (True, True)],
    ids=["default-off", "signed-observation", "private-owner-recipient"],
    indirect=["p1_frozen_from_home"],
)
def test_p1_invoice_commit_then_independent_truth(
    p1_frozen_from_home, monkeypatch, tmp_path, observed, record_testsuite_property, request
):
    h, owner, draft, _maps, confirmed, pack = p1_frozen_from_home
    directory = h.path.parent
    initial_effects = effects(h)
    assert initial_effects == (0,) * 7
    workflow = draft["workflow_ref"]
    tenant, epoch = h.store.organization, h.store.engagement_id
    if observed:
        permission, registration, score = score_p1_from_frozen(p1_frozen_from_home, monkeypatch)
    h.config["live_workflow_ref"] = workflow
    # The originally empty installation has no aggregate access. The live
    # connection grants its owner a separate view of this generated dataset.
    h.config["users"][ACTOR].update(datasets=[DATA], aggregate=True)
    h.save()
    body = dict(
        schema_version=SCHEMA,
        tenant=tenant,
        workflow_ref=workflow,
        engagement_epoch=epoch,
        dataset_ref=DATA,
        dataset_revision="dataset-v1",
        custodian=ACTOR,
        expires_at=int(time.time()) + 3600,
        state="current",
        actors={ACTOR: ["custodian", "inspect"], "worker": ["intake", "inspect"]},
        grants=[asdict(DECISION)],
        engagement_ledger_required=True,
    )
    _write_private(directory / "live-authority-key.json", {"key": KEY.hex()})
    # The test later adds the outcome grant, so enroll both resources up front.
    enroll_ledger(directory, tenant, epoch, (DECISION, OUTCOME))
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
    commit(scope, "D1", "attempt-1", action="unsupported-invoice-action", success=False)
    if observed:
        observation, claim, context_digest = record_application_observation(
            tmp_path, scope, directory, permission, registration
        )
        command = ["commit"]
        for field in (
            "operation",
            "attempt",
            "decision",
            "case",
            "family",
            "action",
            "decision_at",
        ):
            command.extend(["--" + field.replace("_", "-"), claim[field]])
        command.extend(["--observation", observation])
        assert emit(scope, *command) == emit(scope, *command)
    else:
        commit(scope, "D1", "attempt-1", action="supplies")
    receipt, page = export_page(scope, "invoice-page", 10, 11)
    [row] = page["rows"]
    if observed:
        assert row["event"]["model_ref"] == claim["model_ref"]
        assert row["event"]["input_ref"] == claim["input_ref"]
        assert row["event"]["input_fidelity"] == "observed"
        assert row["event"]["actor_ref"] == claim["producer_ref"]
        assert "carrier_freight" not in json.dumps(page) and "po-a" not in json.dumps(page)
    else:
        assert row["event"]["input_fidelity"] == "unavailable"
    install_application_page(directory, body, scope, receipt, page)
    selection = directory / "live-worker.json"
    _write_private(
        selection,
        dict(schema_version=SELECTION_SCHEMA, tenant=tenant, workflow_ref=workflow, actor="worker"),
    )
    worker, intake = open_intake(selection)
    intake_request = dict(
        mode="live",
        feed_binding_ref=binding,
        source_revision=DECISION.source_revision,
        idempotency_key="p1-app-committed-decision",
    )
    if os.environ.get("OPENTOUR_P1_MCP_WITNESS") == "1":
        catalog, first_admission = sdk_actions(
            selection, [(DISCOVER, {}), (SUBMIT, intake_request)]
        )
        assert not catalog.isError and not first_admission.isError
        assert {s["feed_binding_ref"] for s in catalog.structuredContent["sources"]} == {binding}
        [repeat] = sdk_actions(
            selection, [(SUBMIT, intake_request | {"idempotency_key": "p1-app-second-sdk-session"})]
        )
        assert not repeat.isError
        run_ref = first_admission.structuredContent["run_ref"]
        assert repeat.structuredContent["run_ref"] == run_ref
        assert first_admission.structuredContent == repeat.structuredContent
    else:
        run_ref = intake.submit_intake(worker, parse_intake(intake_request)).run_ref
    kit = next((path for path in Path(live_reader.__file__).parents if path.suffix == ".pyz"), None)
    command = [str(kit), "live-reader"] if kit else ["-m", live_reader.__name__]

    def run_worker(status):
        completed = subprocess.run(
            [
                sys.executable,
                "-B",
                *command,
                str(selection),
                run_ref,
            ],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        assert (completed.returncode, completed.stdout.strip(), completed.stderr) == (0, status, "")

    for status in ("materialized", "already_materialized"):
        run_worker(status)
    if os.environ.get("OPENTOUR_P1_MCP_WITNESS") == "1":
        [readback] = sdk_actions(selection, [(GET_RUN, {"run_ref": run_ref})])
        assert not readback.isError
        assert readback.structuredContent["state"] == "materialized"
        assert readback.structuredContent["workflow_ref"] == workflow

    observed_counts = []

    def snapshot(counts):
        current = live.snapshot(actor, workflow)
        actual = (current.decisions, current.verified, current.pending, current.disputed)
        assert actual == counts
        observed_counts.append(actual)
        assert (current.workflow_ref, current.engagement_epoch) == (workflow, epoch)
        return current

    snapshot((1, 0, 1, 0))
    pending_home = json.dumps(publish(h, monkeypatch), ensure_ascii=False)
    assert "Live evidence · 1 decision" in pending_home
    assert "Verified 0 · Pending 1 · Disputed 0" in pending_home
    assert "Incumbent wrong / verified automatic decisions · Unavailable" in pending_home
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
        "supplies",
        "FREIGHT",
    )
    if observed:
        configure_readiness(directory, tmp_path, body, context_digest)

        def readiness():
            run_worker("already_materialized")
            components = open_readiness(directory, tenant, workflow)
            return components.readiness.read(workflow, actor=actor)

        repaired = readiness()
        assert (
            repaired["reason_code"],
            repaired["verified_root_count"],
            repaired["residual_group_count"],
            repaired["minimum_residual_groups"],
        ) == ("too_early_to_train", 1, 0, 3)
        assert repaired["incumbent_automatic"] == {"wrong": 1, "verified": 1}
        assert repaired["repair_route"] == "simple"
        first_home = json.dumps(publish(h, monkeypatch), ensure_ascii=False)
        assert "Too early to train" in first_home and "0 residual groups" in first_home
        assert "Verified 1 · Pending 0 · Disputed 0" in first_home
        assert "Incumbent wrong / verified automatic decisions · 1/1" in first_home
        assert "carrier_freight" not in first_home and "po-a" not in first_home
        assert "Incumbent wrong" not in json.dumps(publish(h, monkeypatch, OTHER))
    verify("reopened")
    snapshot((1, 0, 1, 0))
    if observed:
        pending = readiness()
        assert pending["pending_count"] == 1 and "incumbent_automatic" not in pending
        after_reopen = json.dumps(publish(h, monkeypatch), ensure_ascii=False)
        assert "Verified 0 · Pending 1 · Disputed 0" in after_reopen
        assert "Incumbent wrong / verified automatic decisions · Unavailable" in after_reopen
    h.send("interactive", original)
    snapshot((1, 0, 1, 0))
    restart(h)
    verify("final")
    assert snapshot((1, 1, 0, 0)).verified_roots == first.verified_roots
    if observed:
        reverified = readiness()
        assert reverified["reason_code"] == "too_early_to_train"
        assert reverified["incumbent_automatic"] == {"wrong": 1, "verified": 1}
        assert _stored_score(directory, permission.execution_ref) == score
    assert live.snapshot(actor, workflow, as_of_revision=first.sequence) == first
    assert owner.review() == pack
    final_effects = effects(h)
    assert final_effects == (0,) * 7
    record_testsuite_property(
        f"p1_{request.node.callspec.id}",
        json.dumps(
            {
                "fixture": "generated",
                "intake_transport": "mcp-sdk-stdio"
                if os.environ.get("OPENTOUR_P1_MCP_WITNESS") == "1"
                else "local-service",
                "workflow_ref_sha256": digest(workflow),
                "policy_sha256": confirmed.source_digest,
                "policy_revision": confirmed.source_revision,
                "map_revision": confirmed.map_revision,
                "benchmark_revision": pack["frozen"]["revision"],
                "decision_ref_sha256": digest(
                    [root.namespace, root.decision_id, root.decision_revision]
                ),
                "outcome_ref_sha256": digest(root.outcome_ref),
                "counts_decisions_verified_pending_disputed": observed_counts,
                "effect_order": [
                    "proposals",
                    "decisions",
                    "approval_posts",
                    "jobs",
                    "slack_posts",
                    "workplace_snapshots",
                    "release_decisions",
                ],
                "initial_effects": initial_effects,
                "final_effects": final_effects,
                "starter_metrics": score["metrics"] if observed else None,
            },
            sort_keys=True,
        ),
    )
