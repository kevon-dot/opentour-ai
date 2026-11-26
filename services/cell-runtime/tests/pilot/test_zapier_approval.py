"""The real evidence presentation, Zapier signing action and owner-local release boundary."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import release_gate, repair_workflow, source_intake, zapier_approval
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.slack_adapter import hook_fingerprint
from test_managed_slack import setup_workflow
from test_reviewer_workflow import buttons, restart, screen
from test_slack_adapter import ACTOR, CHANNEL, TEAM, Harness

Json = dict[str, Any]
HOOK = "https://hooks.zapier.com/hooks/catch/12345/GeneratedOnly/"
SIGNER = Path(__file__).resolve().parents[2] / "integrations/zapier/slack-approval-signer.cjs"
NODE = """const fs=require('node:fs'); const mod=require(process.argv[1]);
const b=JSON.parse(fs.readFileSync(0,'utf8')); mod.authentication.test({},b);
process.stdout.write(JSON.stringify(mod.action.operation.perform({},b)));"""


def private(path: Path, value: Json) -> None:
    fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(value, handle)


@dataclass
class Case:
    h: Harness
    plan: Json
    binding: Json
    bridge: zapier_approval.ApprovalBridge
    policy: Json
    key: str
    view: str
    revision: str

    @property
    def directory(self) -> Path:
        return self.h.path.parent

    def inspect(self) -> Json:
        self.h.action(self.view, "ot_evidence", self.revision)
        assert "ot_release_approve" not in buttons(screen(self.h, self.view))
        request = self.bridge.issue(**self.binding)
        assert len(list(self.directory.glob("zapier-approval-za-*-request.json"))) == 1
        return request

    def signed(self, wire: Json, status: str = "Approved", actor: str | None = ACTOR) -> Json:
        connection = {
            k: self.policy[k] for k in ("zap_id", "zap_version", "team", "channel", "reviewer")
        }
        result = subprocess.run(
            ["node", "-e", NODE, str(SIGNER)],
            input=json.dumps(
                {
                    "authData": {**connection, "approval_hmac_sha256": self.key},
                    "inputData": {**wire, "status": status, "actor_id": actor},
                }
            ),
            text=True,
            capture_output=True,
            check=True,
        )
        output = json.loads(result.stdout)
        assert self.key not in result.stdout
        envelope = json.loads(output["signed_decision"])
        assert envelope["event"]["event_id"] == output["id"]
        return envelope

    def authorize(self) -> Json:
        return release_gate.authorize_release(
            self.directory,
            self.h.store.organization,
            self.binding["run_id"],
            self.binding["run_revision"],
            self.binding["artifact_sha"],
        )


def make_case(
    tmp_path: Path, slot: str = "", route: str = "rule", *, audience: str | None = None
) -> Case:
    h, plan, run, outcome = setup_workflow(
        tmp_path, route, release=True, provider="zapier_slack", slot=slot, audience=audience
    )
    if route == "rule":
        assert outcome == "simple_repair"
    team, channel, reviewer = (h.config[k] for k in ("team", "channel", "reviewer"))
    h.config["approval_provider"] = "zapier_slack"
    h.save()
    restart(h)
    directory = h.path.parent
    audit = repair_workflow.managed_review_evidence(
        directory, h.store.organization, run, plan["audience"], team, channel
    )["audit"]
    key = secrets.token_hex(32)
    policy = {
        "schema_version": zapier_approval.POLICY,
        "enabled": True,
        "organization": h.store.organization,
        "engagement": h.store.engagement_id,
        "team": team,
        "channel": channel,
        "reviewer": reviewer,
        "audience": plan["audience"],
        "zap_id": "generated-zap",
        "zap_version": "version-1",
        "approver_mode": "verified_actor",
        "setup_attestation": "att-abcdef0123456789",
        "key_file": "zapier-signer.json",
        "expires_at": int(time.time()) + 600,
        "hook_fingerprint": hook_fingerprint(HOOK.replace("GeneratedOnly", "GeneratedOnly" + slot)),
    }
    cfg = directory / "zapier-approval.json"
    private(cfg, policy)
    private(directory / policy["key_file"], {"secret_hex": key})
    bridge = zapier_approval.ApprovalBridge(directory, h.store.organization, cfg)
    view = h.launch(reviewer)
    revision = buttons(screen(h, view))["ot_evidence"]
    return Case(
        h,
        plan,
        {
            "run_id": run,
            "run_revision": audit["run_revision"],
            "artifact_sha": audit.get("candidate_digest"),
            "inspection_ref": revision,
        },
        bridge,
        policy,
        key,
        view,
        revision,
    )


@pytest.fixture
def case(tmp_path: Path) -> Case:
    return make_case(tmp_path)


def deliver(case: Case, request: Json) -> list[Json]:
    observed: list[Json] = []

    def receive(url: str, payload: Json) -> int:
        assert url == HOOK
        observed.append(payload)
        return 202

    assert case.bridge.flush_ready(HOOK, receive) == {"accepted": 1, "held": 0}
    assert observed == [request]
    assert case.bridge.flush_ready(HOOK, receive) == {"accepted": 0, "held": 0}
    assert case.bridge.flush(request["request_id"], HOOK, receive)["transport"] == "accepted"
    assert observed == [request]
    return observed


def resign(case: Case, envelope: Json, changes: Json) -> Json:
    event = envelope["event"] | changes
    message = (zapier_approval.EVENT + "\n" + canonical(event)).encode()
    return {
        "event": event,
        "signature": hmac.new(bytes.fromhex(case.key), message, hashlib.sha256).hexdigest(),
    }


def test_real_evidence_and_signing_action_allow_only_exact_candidate(case: Case) -> None:
    with pytest.raises(ValueError, match="fresh_evidence_inspection_required"):
        case.bridge.issue(**case.binding)
    assert not list(case.directory.glob("zapier-approval-za-*-request.json"))
    request = case.inspect()
    assert set(request) == {
        "schema_version",
        "request_id",
        "nonce",
        "expires_at",
        "team",
        "channel",
        "reviewer",
        "run_revision",
        "artifact_sha",
        "audience",
        "request_signature",
    }
    assert "generated-repair" not in json.dumps(request) and case.key not in json.dumps(request)
    with pytest.raises((ValueError, OSError)):
        case.authorize()
    with pytest.raises(ValueError, match="release_provider_mismatch"):
        release_gate.record_release(
            case.directory,
            case.h.store.organization,
            **case.binding,
            reviewer=ACTOR,
            team=TEAM,
            channel=CHANNEL,
            audience=case.policy["audience"],
            provider="slack",
            decision="approved",
        )
    envelope = case.signed(request)
    with pytest.raises((OSError, ValueError)):
        case.bridge.consume(envelope)
    deliver(case, request)
    assert case.bridge.consume(case.signed(request, "pending", None))["state"] == "pending"
    with pytest.raises((ValueError, OSError)):
        case.authorize()
    private(case.directory / "signed-decision.json", envelope)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.zapier_approval",
            "--directory",
            str(case.directory),
            "--organization",
            case.h.store.organization,
            "--config",
            str(case.bridge.config),
            "import",
            "--input",
            str(case.directory / "signed-decision.json"),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    receipt = json.loads(result.stdout)
    assert receipt["state"] == "approved" and len(receipt["release_receipt_sha256"]) == 64
    assert case.authorize()["provider"] == "zapier_slack"
    assert case.bridge.consume(envelope) == receipt
    with pytest.raises(ValueError, match="conflicting_provider_event"):
        case.bridge.consume(case.signed(request, "Declined"))


def test_implicit_self_recipient_fences_queued_zapier_request_and_signed_release(
    tmp_path: Path,
) -> None:
    case = make_case(tmp_path, audience=ACTOR)
    h = case.h
    request = case.inspect()
    assert request["reviewer"] == request["audience"] == ACTOR
    run = case.binding["run_id"]
    work = case.directory / "managed-outputs" / run
    handoff_args = (case.directory, h.store.organization, case.plan, run)
    request_file = "zapier-approval-" + request["request_id"]
    admitted = case.directory / (request_file + "-admitted.json")
    observed: list[Json] = []

    def receive(url: str, payload: Json) -> int:
        assert url == HOOK
        observed.append(payload)
        return 202

    del h.config["recipient"]
    h.save()
    settings = h.app.settings()
    assert settings.recipient == settings.reviewer == ACTOR and not settings.explicit_recipient
    assert case.bridge.flush_ready(HOOK, receive) == {"accepted": 0, "held": 1}
    assert observed == [] and not admitted.exists() and not h.slack.post

    h.config["recipient"] = ACTOR
    h.save()
    assert case.inspect() == request
    assert case.bridge.flush_ready(HOOK, receive) == {"accepted": 1, "held": 0}
    assert observed == [request] and admitted.exists()
    envelope = case.signed(request)
    assert envelope["event"]["actor_id"] == ACTOR

    del h.config["recipient"]
    h.save()
    with pytest.raises(ValueError, match="release_review_permission_changed"):
        case.bridge.consume(envelope)
    assert not (case.directory / (request_file + "-event.json")).exists()
    assert not (work / "release-decision.json").exists()
    with pytest.raises((OSError, ValueError)):
        repair_workflow.prepare_handoff(*handoff_args)
    assert not (work / "handoff").exists() and observed == [request]

    h.config["recipient"] = ACTOR
    h.save()
    assert case.inspect() == request
    assert case.bridge.consume(envelope)["state"] == "approved"
    assert case.authorize()["provider"] == "zapier_slack"
    handoff = repair_workflow.prepare_handoff(*handoff_args)
    assert handoff["audience"] == ACTOR and handoff["disposition"] == "rule"
    staged = Path(handoff["spec_path"]).read_bytes()
    decision = (work / "release-decision.json").read_bytes()

    del h.config["recipient"]
    h.save()
    with pytest.raises(ValueError, match="release_review_permission_changed"):
        case.authorize()
    with pytest.raises(ValueError, match="release_review_permission_changed"):
        repair_workflow.prepare_handoff(*handoff_args)
    assert Path(handoff["spec_path"]).read_bytes() == staged
    assert (work / "release-decision.json").read_bytes() == decision
    assert observed == [request] and not h.slack.post
    h.app.pool.shutdown()


def test_stock_decline_means_request_changes_and_fallback_remains(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    assert case.bridge.consume(case.signed(wire, "Declined"))["state"] == "changes_requested"
    with pytest.raises(ValueError, match="human_release_approval_required"):
        case.authorize()


def test_named_destination_and_signed_snapshot_cannot_be_substituted(case: Case) -> None:
    wire = case.inspect()
    for field, replacement in (
        ("artifact_sha", "e" * 64),
        ("audience", "second-audience"),
        ("channel", "COTHER"),
        ("request_signature", "a" * 64),
    ):
        with pytest.raises(subprocess.CalledProcessError):
            case.signed(wire | {field: replacement})
    with pytest.raises(subprocess.CalledProcessError):
        case.signed(wire, actor="UOTHER")
    with pytest.raises(ValueError, match="plugin_destination_mismatch"):
        case.bridge.flush(wire["request_id"], HOOK.replace("12345", "54321"), lambda *_: 202)
    assert not list(case.directory.glob("zapier-approval-za-*-admitted.json"))
    with pytest.raises(ValueError):
        case.bridge.issue(**(case.binding | {"artifact_sha": "a" * 64}))


def test_unsigned_wrong_actor_provider_expiry_and_unknown_outcomes_are_refused(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    envelope = case.signed(wire)
    with pytest.raises(ValueError, match="provider_signature_invalid"):
        case.bridge.consume(envelope | {"signature": "0" * 64})
    for changes, reason in [
        ({"nonce": "e" * 64}, "provider_scope_mismatch"),
        ({"zap_id": "other"}, "provider_scope_mismatch"),
        ({"zap_version": "different"}, "provider_scope_mismatch"),
        ({"actor_id": "UOTHER"}, "named_approver_unverified"),
        ({"actor_id": None}, "named_approver_unverified"),
        ({"decided_at": wire["expires_at"]}, "invalid_provider_event"),
        ({"decided_at": 0}, "invalid_provider_event"),
        ({"status": "yes"}, "invalid_provider_status"),
    ]:
        with pytest.raises(ValueError, match=reason):
            case.bridge.consume(resign(case, envelope, changes))
    with pytest.raises((ValueError, OSError)):
        case.authorize()
    assert not list(case.directory.glob("zapier-approval-za-*-event.json"))


def test_attested_one_person_and_exact_version_are_required_without_actor_output(
    case: Case,
) -> None:
    case.policy["approver_mode"] = "verified_single_approver"
    private(case.bridge.config, case.policy)
    wire = case.inspect()
    deliver(case, wire)
    envelope = case.signed(wire, actor=None)
    denied = resign(case, envelope, {"actor_id": "UOTHER"})
    with pytest.raises(ValueError, match="named_approver_unverified"):
        case.bridge.consume(denied)
    assert case.bridge.consume(envelope)["state"] == "approved"


def test_admission_survives_timeout_and_concurrent_flush_without_duplicate(case: Case) -> None:
    wire = case.inspect()
    posts: list[Json] = []

    def lose_reply(url: str, payload: Json) -> int:
        posts.append(payload)
        raise TimeoutError("response lost after provider admission")

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(
            pool.map(lambda _: case.bridge.flush(wire["request_id"], HOOK, lose_reply), range(4))
        )
    assert {r["transport"] for r in results} == {"uncertain"} and posts == [wire]
    assert case.bridge.flush_ready(HOOK, lose_reply) == {"accepted": 0, "held": 0}
    assert case.bridge.consume(case.signed(wire))["state"] == "approved"
    assert len(posts) == 1


def test_withdrawal_refuses_new_request_submission_and_delayed_decision(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    envelope = case.signed(wire)
    original = json.loads((case.directory / "source-grant.json").read_text())
    source_intake.withdraw_intake(case.h.store, original["owner_attestation"])
    with pytest.raises(ValueError):
        case.bridge.consume(envelope)
    with pytest.raises(ValueError):
        case.bridge.issue(**case.binding)
    with pytest.raises(ValueError):
        case.authorize()


def test_config_turnoff_rotation_and_group_readable_return_fail_closed(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    envelope = case.signed(wire)
    private(case.bridge.config, case.policy | {"zap_version": "version-2"})
    with pytest.raises(ValueError, match="plugin_policy_changed"):
        case.bridge.consume(envelope)
    private(case.bridge.config, case.policy | {"enabled": False})
    with pytest.raises(ValueError, match="plugin_not_authorized"):
        case.bridge.consume(envelope)
    private(case.bridge.config, case.policy)
    path = case.directory / "open-return.json"
    private(path, envelope)
    path.chmod(0o644)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "opentour_cell.pilot.zapier_approval",
            "--directory",
            str(case.directory),
            "--organization",
            case.h.store.organization,
            "--config",
            str(case.bridge.config),
            "import",
            "--input",
            str(path),
        ],
        text=True,
        capture_output=True,
    )
    assert result.returncode == 2 and not result.stdout
    with pytest.raises((OSError, ValueError)):
        case.authorize()


def test_supervised_private_inbox_imports_only_the_real_signed_return(case: Case) -> None:
    assert case.bridge.import_ready() == {"processed": 0, "held": 0}
    wire = case.inspect()
    deliver(case, wire)
    signed = case.signed(wire)
    inbox = case.directory / "zapier-decisions"
    inbox.mkdir(mode=0o700)
    private(inbox / (wire["request_id"] + ".declined.json"), signed)
    bad = inbox / (wire["request_id"] + ".approved.json")
    private(bad, signed | {"signature": "a" * 64})
    assert case.bridge.import_ready() == {"processed": 0, "held": 2}
    with pytest.raises((OSError, ValueError)):
        case.authorize()
    private(bad, signed)
    assert case.bridge.import_ready() == {"processed": 1, "held": 1}
    assert case.authorize()["provider"] == "zapier_slack"
    assert case.bridge.import_ready() == {"processed": 0, "held": 1}
    inbox.chmod(0o755)
    with pytest.raises(ValueError):
        case.bridge.import_ready()


def test_private_return_rechecks_current_named_slack_reviewer(case: Case) -> None:
    wire = case.inspect()
    deliver(case, wire)
    signed = case.signed(wire)
    roles = case.h.config["users"][ACTOR]["roles"]
    case.h.config["users"][ACTOR]["roles"] = [role for role in roles if role != "review"]
    case.h.save()
    with pytest.raises(ValueError):
        case.bridge.consume(signed)
    with pytest.raises((OSError, ValueError)):
        case.authorize()


def test_repeat_inspection_coalesces_and_expiry_needs_a_new_sealed_exposure(
    case: Case, monkeypatch: pytest.MonkeyPatch
) -> None:
    from unittest.mock import patch

    now = int(time.time())
    monkeypatch.setattr(time, "time", lambda: now)
    with patch.object(zapier_approval.ApprovalBridge, "issue", return_value={}):
        case.h.action(case.view, "ot_evidence", case.revision)
    first = case.bridge.issue(**case.binding, ttl_seconds=2)
    assert case.bridge.issue(**case.binding) == first
    deliver(case, first)
    signed = case.signed(first)
    now += 3
    with pytest.raises(ValueError, match="request_expired"):
        case.bridge.issue(**case.binding)
    with pytest.raises(ValueError, match="request_expired"):
        case.bridge.consume(signed)
    view = case.h.launch()
    case.h.action(view, "ot_evidence", case.revision)
    second = case.bridge.issue(**case.binding)
    assert first["request_id"] != second["request_id"] and first["nonce"] != second["nonce"]
    third_view = case.h.launch()
    case.h.action(third_view, "ot_evidence", case.revision)
    assert case.bridge.issue(**case.binding) == second
    assert len(list(case.directory.glob("zapier-approval-za-*-request.json"))) == 2


@pytest.mark.parametrize("slot", ["alpha", "beta"])
def test_versioned_recipe_two_installations(tmp_path: Path, slot: str) -> None:
    from opentour_cell.pilot.approval_inbox import _request

    case = make_case(tmp_path, slot)
    team, channel, reviewer = (case.policy[k] for k in ("team", "channel", "reviewer"))

    def click(action: str, value: str) -> None:
        case.h.action(case.view, action, value, reviewer)

    case.policy.update(
        schema_version=zapier_approval.RECIPE_POLICY,
        zap_id=f"generated-{slot}",
        zap_version=f"version-{slot}",
    )
    private(case.bridge.config, case.policy)
    binding = {
        **{k: case.policy[k] for k in ("team", "channel", "reviewer", "zap_id", "zap_version")},
        **{f"{k}_connection": f"{slot}-{k}" for k in ("slack", "signer", "return")},
        "details_origin": f"https://{slot}.example.invalid",
    }
    click("ot_evidence", case.revision)
    actions = buttons(screen(case.h, case.view))
    assert "ot_plugin_continue" in actions and "ot_release_approve" not in actions
    assert not list(case.directory.glob("zapier-approval-za-*-request.json"))
    click("ot_plugin_continue", case.revision)
    assert not list(case.directory.glob("zapier-approval-za-*-request.json"))
    token = screen(case.h, case.view)["private_metadata"]
    context = case.h.app.journal.read_context(token, reviewer, channel, case.view)
    preview = json.loads(context["revision"])
    with pytest.raises(ValueError, match="confirmed_preview_required"):
        case.bridge.issue(**case.binding)
    with pytest.raises(ValueError, match="confirmed_preview_required"):
        case.bridge.issue(**case.binding, confirmed_preview=preview | {"audience": "other"})
    display = json.dumps(screen(case.h, case.view))
    for value in (
        "keep the incumbent",
        "evaluation handoff only",
        "Expires",
        case.policy["audience"],
        case.binding["artifact_sha"],
        case.binding["run_revision"],
    ):
        assert value in display
    confirmed_click = case.h.action(case.view, "ot_plugin_request", preview["request_id"], reviewer)
    case.h.send("interactive", confirmed_click)
    paths = list(case.directory.glob("zapier-approval-za-*-request.json"))
    assert len(paths) == 1
    case.view = case.h.launch(reviewer)
    click("ot_evidence", case.revision)
    assert buttons(screen(case.h, case.view))["ot_plugin_status"] == preview["request_id"]
    assert "ot_plugin_continue" not in buttons(screen(case.h, case.view))
    assert list(case.directory.glob("zapier-approval-za-*-request.json")) == paths
    wire = case.bridge.issue(**case.binding, confirmed_preview=preview)
    assert set(wire) == {"schema_version", "request_id", "nonce", "expires_at", "request_signature"}
    notice = _request("approval-" + "a" * 32)
    assert len(notice["blocks"]) == 1
    assert notice["blocks"][0]["elements"][0]["text"]["text"] == "Open evidence"
    assert len(notice["blocks"][0]["elements"]) == 1
    assert case.policy["audience"] not in json.dumps(notice)

    def node(action: str, *, settings: Json = binding, auth: Json = binding, **data: Any) -> Json:
        program = """
const fs=require('node:fs'), path=require('node:path');
const {verifyAction,referenceSigner}=require(process.argv[1]);
const {importRecipe}=require(path.join(path.dirname(process.argv[1]),'import-approval-recipe.cjs'));
const b=JSON.parse(fs.readFileSync(0,'utf8'));
const installed=importRecipe(b.settings,b.policy);
const bundle={authData:{...b.auth,approval_hmac_sha256:b.key},inputData:b.data};
const verified=verifyAction.operation.perform({},bundle);
const output=b.action==='render'?installed.render(verified):
  referenceSigner.operation.perform({},bundle);
process.stdout.write(JSON.stringify(output));
"""
        result = subprocess.run(
            ["node", "-e", program, str(SIGNER)],
            input=json.dumps(
                dict(
                    settings=settings,
                    auth=auth,
                    policy=case.policy,
                    key=case.key,
                    data=wire | data,
                    action=action,
                )
            ),
            capture_output=True,
            text=True,
            check=True,
        )
        assert case.key not in result.stdout
        return json.loads(result.stdout)

    rendered = node("render")
    fields = rendered["fields"]
    assert fields["Approvers"] == reviewer and fields["Channel"] == channel
    assert fields["Require confirmation"] == fields["Add a Decline button"] == "Yes"
    assert (
        fields["Details button URL"] == f"{binding['details_origin']}/reviews/{wire['request_id']}"
    )
    assert fields["Approval request message"] == rendered["expectedFallback"]
    public_text = json.dumps([v for k, v in fields.items() if k not in {"Approvers", "Channel"}])
    for sensitive in (
        case.policy["audience"],
        wire["nonce"],
        case.binding["artifact_sha"],
        wire["request_signature"],
        case.key,
        reviewer,
        channel,
    ):
        assert sensitive not in public_text
    for key, wrong in (("channel", "COTHER"), ("reviewer", "UOTHER"), ("zap_version", "v2")):
        with pytest.raises(subprocess.CalledProcessError):
            node("render", settings=binding | {key: wrong})
        with pytest.raises(subprocess.CalledProcessError):
            node("render", auth=binding | {key: wrong})
    with pytest.raises(subprocess.CalledProcessError):
        node("sign", status="Approved", actor_id="UOTHER")
    with pytest.raises(subprocess.CalledProcessError):
        node("render", request_signature="0" * 64)
    with pytest.raises(ValueError, match="reviewer_required"):
        case.bridge.details(wire["request_id"], team=team, actor="UOTHER")
    assert (
        case.bridge.details(wire["request_id"], team=team, actor=reviewer)["artifact_sha"]
        == (case.binding["artifact_sha"])
    )
    calls = []

    def deliver_or_timeout(url: str, packet: Json) -> int:
        calls.append(packet)
        if slot == "beta":
            raise TimeoutError
        return 202

    for _ in range(2):
        case.bridge.flush_ready(
            HOOK.replace("GeneratedOnly", "GeneratedOnly" + slot), deliver_or_timeout
        )
    assert calls == [wire]
    case.view = case.h.launch(reviewer)
    click("ot_evidence", case.revision)
    click("ot_plugin_status", wire["request_id"])
    state_text = "Dispatch is uncertain" if slot == "beta" else "provider accepted dispatch"
    assert state_text in json.dumps(screen(case.h, case.view))
    assert "ot_plugin_continue" not in buttons(screen(case.h, case.view))
    for status in ("Pending", "Declined"):
        with pytest.raises(ValueError):
            case.bridge.consume({"status": status})
    with pytest.raises((OSError, ValueError)):
        case.authorize()
    outputs = case.directory / "managed-outputs"

    def inventory() -> dict[Path, bytes]:
        return {p: p.read_bytes() for p in outputs.rglob("*") if p.is_file()}

    before = inventory()
    for status in ("Pending", "Approved" if slot == "alpha" else "Declined"):
        signed = json.loads(node("sign", status=status, actor_id=reviewer)["signed_decision"])
        receipt = case.bridge.consume(signed)
        assert case.bridge.consume(signed) == receipt
        if status == "Approved":
            assert case.authorize()["provider"] == "zapier_slack"
            assert receipt["release_receipt_sha256"]
        else:
            with pytest.raises((OSError, ValueError)):
                case.authorize()
    assert len(list(case.directory.glob("zapier-approval-za-*-event.json"))) == 1
    case.view = case.h.launch(reviewer)
    click("ot_evidence", case.revision)
    assert "ot_plugin_continue" not in buttons(screen(case.h, case.view))
    assert buttons(screen(case.h, case.view))["ot_plugin_receipt"] == wire["request_id"]
    click("ot_plugin_receipt", wire["request_id"])
    assert receipt["release_receipt_sha256"] in json.dumps(screen(case.h, case.view))
    assert len(list(case.directory.rglob("release-decision.json"))) == 1
    decision = next(case.directory.rglob("release-decision.json"))
    assert inventory() == before | {decision: decision.read_bytes()}
    with pytest.raises(ValueError, match="review_already_decided"):
        case.bridge.issue(**case.binding, preview_only=True)


def test_ineligible_managed_review_status_and_withdrawal(tmp_path: Path) -> None:
    case = make_case(tmp_path, route="unapproved_model")
    private(case.bridge.config, case.policy | {"schema_version": zapier_approval.RECIPE_POLICY})
    case.h.action(case.view, "ot_evidence", case.revision)
    actions = buttons(screen(case.h, case.view))
    assert "ot_plugin_continue" not in actions
    assert actions["ot_managed_review_status"] == case.revision
    case.h.action(case.view, "ot_managed_review_status", case.revision)
    text = json.dumps(screen(case.h, case.view))
    assert "Hold the handoff" in text and "The recurring error persisted" in text
    original = json.loads((case.directory / "source-grant.json").read_text())
    source_intake.withdraw_intake(case.h.store, original["owner_attestation"])
    case.h.action(case.view, "ot_managed_review_status", case.revision)
    denied = json.dumps(screen(case.h, case.view))
    assert "The recurring error persisted" not in denied and case.revision not in denied
    assert not list(case.directory.glob("zapier-approval-za-*.json"))
    assert not list(case.directory.rglob("release-decision.json"))


def test_cross_install_reference_and_private_return_have_zero_effects(tmp_path: Path) -> None:
    cases = [make_case(tmp_path / slot, slot) for slot in ("alpha", "beta")]
    requests = []
    for case in cases:
        cfg = case.policy
        cfg.update(
            schema_version=zapier_approval.RECIPE_POLICY,
            zap_id=cfg["organization"],
            zap_version=cfg["organization"] + "-v1",
        )
        private(case.bridge.config, cfg)
        case.h.action(case.view, "ot_evidence", case.revision, cfg["reviewer"])
        preview = case.bridge.issue(**case.binding, preview_only=True)
        packet = case.bridge.issue(**case.binding, confirmed_preview=preview)
        requests.append(packet)
        hook = HOOK.replace(
            "GeneratedOnly", "GeneratedOnly" + cfg["organization"].removeprefix("generated-")
        )
        assert (
            case.bridge.flush(packet["request_id"], hook, lambda *_: 503)["transport"] == "failed"
        )
        case.view = case.h.launch(cfg["reviewer"])
        case.h.action(case.view, "ot_evidence", case.revision, cfg["reviewer"])
        assert "Dispatch failed" in json.dumps(screen(case.h, case.view))
        assert "ot_plugin_continue" not in buttons(screen(case.h, case.view))
    for field in ("organization", "team", "channel", "reviewer", "audience", "hook_fingerprint"):
        assert cases[0].policy[field] != cases[1].policy[field]
    assert cases[0].key != cases[1].key
    for index, origin in enumerate(cases):
        receiver = cases[1 - index]
        packet = requests[index]
        with pytest.raises(subprocess.CalledProcessError):
            receiver.signed(packet, actor=receiver.policy["reviewer"])
        signed = origin.signed(packet, actor=origin.policy["reviewer"])
        with pytest.raises(ValueError, match="provider_signature_invalid"):
            receiver.bridge.consume(signed)
        assert not list(receiver.directory.glob("zapier-approval-za-*-event.json"))
        assert not list(receiver.directory.rglob("release-decision.json"))


def test_versioned_request_cannot_use_implicit_local_recipient(tmp_path: Path) -> None:
    case = make_case(tmp_path, audience=ACTOR)
    private(case.bridge.config, case.policy | {"schema_version": zapier_approval.RECIPE_POLICY})
    h = case.h
    h.action(case.view, "ot_evidence", case.revision)
    h.action(case.view, "ot_plugin_continue", case.revision)
    request_id = buttons(screen(h, case.view))["ot_plugin_request"]
    h.action(case.view, "ot_plugin_request", request_id)
    assert len(list(case.directory.glob("zapier-approval-za-*-request.json"))) == 1

    del h.config["recipient"]
    h.save()
    view = h.launch()
    h.action(view, "ot_evidence", case.revision)
    evidence = screen(h, view)
    assert evidence["callback_id"] == "ot_evidence_view"
    assert "Frozen workflow comparison" in json.dumps(evidence)
    assert (
        not {"ot_plugin_continue", "ot_plugin_request", "ot_plugin_receipt"}
        & buttons(evidence).keys()
    )
    h.action(view, "ot_plugin_continue", case.revision)
    assert "ot_plugin_request" not in buttons(screen(h, view))
    assert case.bridge.flush_ready(HOOK, lambda *_: pytest.fail("unauthorized dispatch")) == {
        "accepted": 0,
        "held": 1,
    }
    assert not list(case.directory.glob("zapier-approval-za-*-admitted.json"))
    assert not list(case.directory.rglob("release-decision.json"))
    assert not h.slack.post
