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


@pytest.fixture
def case(tmp_path: Path) -> Case:
    h, plan, run, outcome = setup_workflow(tmp_path, "rule", release=True, provider="zapier_slack")
    assert outcome == "simple_repair"
    h.config["approval_provider"] = "zapier_slack"
    h.save()
    restart(h)
    directory = h.path.parent
    audit = repair_workflow.managed_review_evidence(
        directory, h.store.organization, run, plan["audience"], TEAM, CHANNEL
    )["audit"]
    key = secrets.token_hex(32)
    policy = {
        "schema_version": zapier_approval.POLICY,
        "enabled": True,
        "organization": h.store.organization,
        "engagement": h.store.engagement_id,
        "team": TEAM,
        "channel": CHANNEL,
        "reviewer": ACTOR,
        "audience": plan["audience"],
        "zap_id": "generated-zap",
        "zap_version": "version-1",
        "approver_mode": "verified_actor",
        "setup_attestation": "att-abcdef0123456789",
        "key_file": "zapier-signer.json",
        "expires_at": int(time.time()) + 600,
        "hook_fingerprint": hook_fingerprint(HOOK),
    }
    cfg = directory / "zapier-approval.json"
    private(cfg, policy)
    private(directory / policy["key_file"], {"secret_hex": key})
    bridge = zapier_approval.ApprovalBridge(directory, h.store.organization, cfg)
    view = h.launch()
    revision = buttons(screen(h, view))["ot_evidence"]
    return Case(
        h,
        plan,
        {
            "run_id": run,
            "run_revision": audit["run_revision"],
            "artifact_sha": audit["candidate_digest"],
            "inspection_ref": revision,
        },
        bridge,
        policy,
        key,
        view,
        revision,
    )


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
