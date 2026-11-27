"""Require a current human decision about the exact candidate before pilot handoff."""

from __future__ import annotations

import hmac
import json
import re
import time
from pathlib import Path
from typing import Any

from opentour_cell.pilot.repair_workflow import (
    _private_json,
    _record,
    _require,
    managed_review_evidence,
)
from opentour_cell.pilot.training import LocalTrainingStore

Json = dict[str, Any]


def _binding(
    directory: Path, organization: str, run_id: str, run_revision: str, artifact_sha: str
) -> tuple[Path, Json]:
    _require(re.fullmatch(r"run-[0-9a-f]{32}", run_id), "invalid_run")
    work = directory / "managed-outputs" / run_id
    run = _private_json(work / "run.json")
    _require(
        run["run_id"] == run_id
        and run["result_revision"] == run_revision
        and run["outcome"] in {"simple_repair", "model_candidate"},
        "candidate_not_releasable",
    )
    policy = run.get("release_review")
    _require(
        isinstance(policy, dict)
        and set(policy) - {"provider"} == {"reviewer", "team", "channel", "audience"}
        and policy.get("provider", "slack") in {"slack", "zapier_slack"},
        "release_policy_unavailable",
    )
    assert isinstance(policy, dict)
    evidence = managed_review_evidence(
        directory, organization, run_id, policy["audience"], policy["team"], policy["channel"]
    )
    _require(
        evidence["audit"]["candidate_digest"] == artifact_sha
        and evidence["audit"]["run_revision"] == run_revision,
        "superseded_candidate",
    )
    return work, policy


def check_release_inspection(
    directory: Path,
    organization: str,
    *,
    run_id: str,
    run_revision: str,
    artifact_sha: str,
    reviewer: str,
    team: str,
    channel: str,
    audience: str,
    inspection_ref: str,
) -> Json:
    _, policy = _binding(directory, organization, run_id, run_revision, artifact_sha)
    _require(
        all(
            policy[key] == value
            for key, value in {
                "reviewer": reviewer,
                "team": team,
                "channel": channel,
                "audience": audience,
            }.items()
        ),
        "release_reviewer_mismatch",
    )
    checked = _inspection(
        directory,
        organization,
        run_id,
        run_revision,
        artifact_sha,
        policy,
        inspection_ref,
        fresh=True,
    )
    del checked["presented_at"]
    return checked


def _inspection(
    directory: Path,
    organization: str,
    run_id: str,
    run_revision: str,
    artifact_sha: str,
    policy: Json,
    inspection_ref: str,
    *,
    fresh: bool,
    decision_body: Json | None = None,
) -> Json:
    from opentour_cell.pilot.receipt_recovery import _JournalSnapshot, _open_db
    from opentour_cell.pilot.slack_adapter import Settings

    ledger = directory / "slack-pilot.sqlite3"
    reviewer, team, channel, audience = (
        policy[k] for k in ("reviewer", "team", "channel", "audience")
    )
    with _open_db(ledger) as db:
        _require(db is not None, "review_journal_unavailable")
        assert db is not None
        binding = db.execute("SELECT binding FROM identity WHERE id=1").fetchone()
        _require(binding, "review_journal_unavailable")
        assert binding is not None
        journal = _JournalSnapshot(db, ledger, binding[0])
        journal.active(db)
        row = db.execute(
            "SELECT p.id,p.snapshot,p.expires,p.policy,p.presented FROM presentations p "
            "JOIN exposures e ON e.id=p.id "
            "WHERE p.actor=? AND (p.id=? OR p.revision=?) AND "
            "e.status='presented' ORDER BY p.presented DESC,p.rowid DESC LIMIT 1",
            (reviewer, inspection_ref, inspection_ref),
        ).fetchone()
        _require(
            row and (not fresh or row[2] > int(time.time())), "fresh_evidence_inspection_required"
        )
        assert row is not None
        journal._seal(db, "presentation", row[0])
        seen = json.loads(row[1])
        _require(
            seen.get("run_revision") == run_revision
            and seen.get("model_revision") == artifact_sha
            and seen.get("job_id") == run_id
            and seen.get("viewer") == reviewer
            and seen.get("team") == team
            and seen.get("channel") == channel
            and seen.get("recipient") == audience
            and isinstance(seen.get("release_settings_file"), str)
            and isinstance(seen.get("dataset_id"), str),
            "fresh_evidence_inspection_required",
        )
        cfg = Settings.load(Path(seen["release_settings_file"]))
        role = cfg.permission(reviewer, "review", seen["dataset_id"])
        cfg.permission(reviewer, "inspect", seen["dataset_id"])
        _require(
            cfg.binding == journal.binding
            and cfg.directory == directory
            and cfg.organization == organization
            and cfg.team == team
            and cfg.channel == channel
            and cfg.reviewer == reviewer
            and cfg.recipient == audience
            and cfg.explicit_recipient
            and cfg.approval_provider == policy.get("provider", "slack")
            and role["aggregate"]
            and journal.opaque("policy", [cfg.team, cfg.channel, role, cfg.reviewer, cfg.recipient])
            == row[3],
            "release_review_permission_changed",
        )
        result: Json = {
            "engagement": seen["engagement"],
            "expires_at": row[2],
            "presented_at": row[4],
            "provider": cfg.approval_provider,
            "presentation_id": row[0],
        }
        if decision_body is not None:
            result["decision_seal"] = journal.opaque(
                "release",
                [
                    "opentour.pilot.release-decision.v1",
                    journal.binding,
                    seen["engagement"],
                    decision_body,
                ],
            )
        return result


def _check_recorded_at(receipt: Json, presentation: Json) -> None:
    if "recorded_at" in receipt:
        recorded = receipt["recorded_at"]
        _require(
            type(recorded) is int
            and presentation["presented_at"] <= recorded < presentation["expires_at"]
            and recorded <= int(time.time()),
            "release_record_time_invalid",
        )


def _decision_body(receipt: Json) -> Json | None:
    if "recorded_at" not in receipt and "decision_seal" not in receipt:
        return None
    return {key: value for key, value in receipt.items() if key != "decision_seal"}


def _check_decision_seal(receipt: Json, presentation: Json) -> None:
    if "decision_seal" in presentation:
        supplied = receipt.get("decision_seal")
        _require(
            "recorded_at" in receipt
            and isinstance(supplied, str)
            and supplied.isascii()
            and hmac.compare_digest(supplied, presentation["decision_seal"]),
            "release_decision_integrity_unavailable",
        )


def record_release(
    directory: Path,
    organization: str,
    *,
    run_id: str,
    run_revision: str,
    artifact_sha: str,
    reviewer: str,
    team: str,
    channel: str,
    audience: str,
    decision: str,
    inspection_ref: str,
    provider: str,
) -> Json:
    _require(
        decision in {"approved", "changes_requested"} and provider in {"slack", "zapier_slack"},
        "release_reviewer_mismatch",
    )
    seen = check_release_inspection(
        directory,
        organization,
        run_id=run_id,
        run_revision=run_revision,
        artifact_sha=artifact_sha,
        reviewer=reviewer,
        team=team,
        channel=channel,
        audience=audience,
        inspection_ref=inspection_ref,
    )
    work, policy = _binding(directory, organization, run_id, run_revision, artifact_sha)
    _require(seen["provider"] == provider, "release_provider_mismatch")
    value = {
        "organization": organization,
        "run_id": run_id,
        "run_revision": run_revision,
        "artifact_sha": artifact_sha,
        **policy,
        "decision": decision,
        "provider": provider,
        "inspection_ref": inspection_ref,
        "presentation_id": seen["presentation_id"],
    }
    store = LocalTrainingStore(directory / "training.sqlite3", organization, existing_only=True)
    with store.engagement_guard(engagement_id=seen["engagement"]):
        _binding(directory, organization, run_id, run_revision, artifact_sha)
        path = work / "release-decision.json"
        try:
            saved = _private_json(path)
        except FileNotFoundError:
            create = True
            saved = value | {"recorded_at": int(time.time())}
        else:
            create = False
            _require(
                {k: v for k, v in saved.items() if k not in {"recorded_at", "decision_seal"}}
                == value,
                "existing_result_conflict",
            )
        presentation = _inspection(
            directory,
            organization,
            run_id,
            run_revision,
            artifact_sha,
            policy,
            seen["presentation_id"],
            fresh=True,
            decision_body=_decision_body(saved),
        )
        _check_recorded_at(saved, presentation)
        if create:
            saved["decision_seal"] = presentation["decision_seal"]
            _record(path, saved)
        else:
            # A retry must preserve the time of the first durable decision. Old
            # receipts remain usable for their existing approval-only pathways.
            _check_decision_seal(saved, presentation)
        return saved


def inspect_recorded_release(
    directory: Path, organization: str, run_id: str, run_revision: str, artifact_sha: str
) -> Json | None:
    """Read a scoped decision. A missing decision does not attest to delivery state."""
    work, policy = _binding(directory, organization, run_id, run_revision, artifact_sha)
    try:
        receipt = _private_json(work / "release-decision.json")
    except FileNotFoundError:
        return None
    _require(
        all(receipt.get(k) == v for k, v in policy.items())
        and receipt.get("decision") in {"approved", "changes_requested"}
        and receipt.get("provider") == policy.get("provider", "slack")
        and receipt.get("organization") == organization
        and receipt.get("run_id") == run_id
        and receipt.get("run_revision") == run_revision
        and receipt.get("artifact_sha") == artifact_sha,
        "human_release_approval_required",
    )
    presentation = _inspection(
        directory,
        organization,
        run_id,
        run_revision,
        artifact_sha,
        policy,
        receipt["presentation_id"],
        fresh=False,
        decision_body=_decision_body(receipt),
    )
    _check_recorded_at(receipt, presentation)
    _check_decision_seal(receipt, presentation)
    return receipt


def authorize_release(
    directory: Path, organization: str, run_id: str, run_revision: str, artifact_sha: str
) -> Json:
    store = LocalTrainingStore(directory / "training.sqlite3", organization, existing_only=True)
    with store.engagement_guard():
        receipt = inspect_recorded_release(
            directory, organization, run_id, run_revision, artifact_sha
        )
        _require(
            receipt is not None and receipt["decision"] == "approved",
            "human_release_approval_required",
        )
        assert receipt is not None
        return receipt
