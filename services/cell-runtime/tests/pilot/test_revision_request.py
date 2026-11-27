"""Generated authenticated declines through the actual signer and local importer."""

from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from opentour_cell.pilot import revision_request
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.revision_request import RevisionRequests
from test_reviewer_workflow import restart
from test_slack_adapter import ACTOR, OTHER
from test_zapier_approval import Case, deliver, make_case, resign


def reader(case: Case, *, organization: str | None = None) -> RevisionRequests:
    return RevisionRequests(case.directory, organization or case.h.store.organization, case.h.path)


def inspect(case: Case, *, actor: str = ACTOR) -> dict:
    return reader(case).inspect(
        actor=actor, **{k: case.binding[k] for k in ("run_id", "run_revision", "artifact_sha")}
    )


@pytest.fixture
def declined(tmp_path: Path):
    case = make_case(tmp_path)
    wire = case.inspect()
    deliver(case, wire)
    envelope = case.signed(wire, "Declined")
    assert case.bridge.consume(envelope)["state"] == "changes_requested"
    yield case, wire, envelope
    case.h.app.pool.shutdown()


def test_decline_without_provider_note_reopens_closed_after_restart(declined) -> None:
    case, wire, envelope = declined
    path = case.directory / "managed-outputs" / case.binding["run_id"] / "release-decision.json"
    original = path.read_bytes()
    request = inspect(case)
    assert "note" not in envelope["event"]
    assert request["closed"] is True and request["state"] == "changes_requested"
    assert request["release_receipt_revision"] == digest(json.loads(original))
    assert request["run_revision"] == case.binding["run_revision"]
    assert request["artifact_sha"] == wire["artifact_sha"]
    assert request["destination"] == wire["audience"]
    assert request["provider"] == "zapier_slack"
    # A client may close/skip its optional dialog without calling any backend mutation.
    restart(case.h)
    assert inspect(case) == request
    assert case.bridge.consume(envelope)["state"] == "changes_requested"
    assert inspect(case) == request and path.read_bytes() == original
    with pytest.raises(ValueError, match="human_release_approval_required"):
        case.authorize()
    assert not (path.parent / "handoff").exists()
    assert len(list(path.parent.parent.glob("*/release-decision.json"))) == 1


def test_late_positive_cannot_reopen_declined_version(declined) -> None:
    case, wire, _ = declined
    before = inspect(case)
    with pytest.raises(ValueError):
        case.bridge.consume(case.signed(wire, "Approved"))
    assert inspect(case) == before
    with pytest.raises(ValueError, match="human_release_approval_required"):
        case.authorize()


@pytest.mark.parametrize("state", ["missing", "pending", "approved", "forged"])
def test_only_actual_authenticated_decline_produces_request(tmp_path: Path, state: str) -> None:
    case = make_case(tmp_path)
    wire = case.inspect()
    if state != "missing":
        deliver(case, wire)
        if state == "forged":
            with pytest.raises(ValueError, match="provider_signature_invalid"):
                case.bridge.consume(case.signed(wire, "Declined") | {"signature": "0" * 64})
        else:
            case.bridge.consume(case.signed(wire, state.title()))
    with pytest.raises(ValueError, match="authenticated_decline_required"):
        inspect(case)
    assert not list(case.directory.glob("**/revision-request*"))
    case.h.app.pool.shutdown()


def test_provider_identity_unknown_does_not_infer_negative(tmp_path: Path) -> None:
    case = make_case(tmp_path)
    wire = case.inspect()
    deliver(case, wire)
    signed = case.signed(wire, "Declined")
    with pytest.raises(ValueError, match="named_approver_unverified"):
        case.bridge.consume(resign(case, signed, {"actor_id": None}))
    with pytest.raises(ValueError, match="authenticated_decline_required"):
        inspect(case)
    case.h.app.pool.shutdown()


@pytest.mark.parametrize("change", ["reviewer_revoked", "source_revoked", "withdrawn", "expired"])
def test_original_authority_loss_holds_projection_and_preserves_decline(declined, change) -> None:
    case, _, _ = declined
    path = case.directory / "managed-outputs" / case.binding["run_id"] / "release-decision.json"
    original = path.read_bytes()
    if change == "reviewer_revoked":
        case.h.config["users"][ACTOR]["roles"].remove("review")
        case.h.save()
    elif change == "source_revoked":
        from opentour_cell.pilot import source_intake

        original_grant = json.loads((case.directory / "source-grant.json").read_text())
        source_intake.withdraw_intake(case.h.store, original_grant["owner_attestation"])
    elif change == "withdrawn":
        case.h.store.withdraw()
    else:
        case.h.config["users"][ACTOR]["expires_at"] = int(time.time()) - 1
        case.h.save()
    with pytest.raises((ValueError, OSError)):
        inspect(case)
    assert path.read_bytes() == original


@pytest.mark.parametrize("change", ["dataset", "aggregate", "role", "expired"])
def test_current_callers_own_read_permission_is_required(declined, change) -> None:
    case, _, _ = declined
    user = case.h.config["users"][ACTOR]
    assert inspect(case)["closed"]
    if change == "dataset":
        user["datasets"] = ["another-source"]
    elif change == "aggregate":
        user["aggregate"] = False
    elif change == "role":
        user["roles"] = []
    else:
        user["expires_at"] = int(time.time()) - 1
    case.h.save()
    with pytest.raises(ValueError):
        inspect(case)


def test_generic_aggregate_inspector_cannot_read_decision_history(declined) -> None:
    case, _, _ = declined
    case.h.config["users"][OTHER]["datasets"] = ["generated-repair"]
    case.h.save()
    with pytest.raises(ValueError, match="revision_request_unavailable"):
        inspect(case, actor=OTHER)


def test_alternate_settings_cannot_substitute_app_or_authoritative_grant(declined) -> None:
    case, _, _ = declined
    alternate = case.directory / "alternate-settings.json"
    alternate.write_text(case.h.path.read_text())
    alternate.chmod(0o600)
    selected = RevisionRequests(case.directory, case.h.store.organization, alternate)
    binding = {k: case.binding[k] for k in ("run_id", "run_revision", "artifact_sha")}
    changed = json.loads(alternate.read_text())
    changed["app"] = "ADIFFERENT"
    alternate.write_text(json.dumps(changed))
    with pytest.raises(ValueError, match="revision_request_unavailable"):
        selected.inspect(actor=ACTOR, **binding)
    alternate.write_text(case.h.path.read_text())
    case.h.config["users"][ACTOR]["roles"].remove("review")
    case.h.save()
    with pytest.raises(ValueError):
        selected.inspect(actor=ACTOR, **binding)


def test_nonmember_is_denied_before_proposal_lookup(declined, monkeypatch) -> None:
    case, _, _ = declined

    def never(*args, **kwargs):
        pytest.fail("nonmember must be refused before the proposal lookup")

    monkeypatch.setattr(revision_request, "inspect_recorded_release", never)
    for run_id in (case.binding["run_id"], "run-" + "0" * 32):
        with pytest.raises(ValueError):
            reader(case).inspect(
                actor="UNONMEMBER",
                run_id=run_id,
                run_revision=case.binding["run_revision"],
                artifact_sha=case.binding["artifact_sha"],
            )


@pytest.mark.parametrize("field", ["run_id", "run_revision", "artifact_sha"])
def test_wrong_exact_version_cannot_resolve_to_another_request(declined, field) -> None:
    case, _, _ = declined
    binding = {k: case.binding[k] for k in ("run_id", "run_revision", "artifact_sha")}
    binding[field] = "run-" + "0" * 32 if field == "run_id" else "0" * 64
    with pytest.raises((OSError, ValueError)):
        reader(case).inspect(actor=ACTOR, **binding)


def test_tenant_and_sealed_inspection_are_not_substitutable(declined) -> None:
    case, _, _ = declined
    binding = {k: case.binding[k] for k in ("run_id", "run_revision", "artifact_sha")}
    with pytest.raises(ValueError, match="revision_request_unavailable"):
        reader(case, organization="other-organization").inspect(actor=ACTOR, **binding)
    with sqlite3.connect(case.directory / "slack-pilot.sqlite3") as db:
        db.execute("UPDATE presentations SET rendered='{}'")
    with pytest.raises(ValueError):
        inspect(case)


def test_role_change_during_evidence_read_prevents_disclosure(declined, monkeypatch) -> None:
    case, _, _ = declined
    original = revision_request.managed_review_evidence

    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        case.h.config["users"][ACTOR]["aggregate"] = False
        case.h.save()
        return result

    monkeypatch.setattr(revision_request, "managed_review_evidence", revoke)
    with pytest.raises(ValueError):
        inspect(case)


def test_original_authority_is_rechecked_after_deferred_read(declined, monkeypatch) -> None:
    case, _, _ = declined
    alternate = case.directory / "equivalent-settings.json"
    alternate.write_bytes(case.h.path.read_bytes())
    alternate.chmod(0o600)
    selected = RevisionRequests(case.directory, case.h.store.organization, alternate)
    original = revision_request.managed_review_evidence

    def revoke(*args, **kwargs):
        result = original(*args, **kwargs)
        case.h.config["users"][ACTOR]["roles"].remove("review")
        case.h.save()
        return result

    monkeypatch.setattr(revision_request, "managed_review_evidence", revoke)
    with pytest.raises(ValueError):
        selected.inspect(
            actor=ACTOR,
            **{k: case.binding[k] for k in ("run_id", "run_revision", "artifact_sha")},
        )


def test_owner_local_source_entry_returns_projection_or_neutral_denial(declined) -> None:
    case, _, _ = declined
    args = [
        sys.executable,
        "-B",
        "-m",
        "opentour_cell.pilot.revision_request",
        "--directory",
        str(case.directory),
        "--organization",
        case.h.store.organization,
        "--settings",
        str(case.h.path),
        "--actor",
        ACTOR,
        "--run-id",
        case.binding["run_id"],
        "--run-revision",
        case.binding["run_revision"],
        "--artifact-sha",
        case.binding["artifact_sha"],
    ]
    result = subprocess.run(args, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout) == inspect(case)
    args[args.index(ACTOR)] = "UNONMEMBER"
    denied = subprocess.run(args, text=True, capture_output=True)
    assert denied.returncode == 2 and not denied.stdout
    assert denied.stderr == "revision_request_unavailable\n"
    args[args.index("UNONMEMBER")] = ACTOR
    with sqlite3.connect(case.directory / "slack-pilot.sqlite3") as db:
        db.execute("DROP TABLE presentations")
    unavailable = subprocess.run(args, text=True, capture_output=True)
    assert unavailable.returncode == 2 and not unavailable.stdout
    assert unavailable.stderr == "revision_request_unavailable\n"
