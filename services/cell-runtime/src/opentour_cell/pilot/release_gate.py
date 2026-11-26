"""Require a current human decision about the exact candidate before pilot handoff."""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from typing import Any

from opentour_cell.pilot.repair_workflow import (
    _private_json,
    _record,
    _require,
    managed_review_evidence,
)
from opentour_cell.pilot.training import LocalTrainingStore, _check_file

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
    return _inspection(
        directory,
        organization,
        run_id,
        run_revision,
        artifact_sha,
        policy,
        inspection_ref,
        fresh=True,
    )


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
) -> Json:
    from opentour_cell.pilot.slack_adapter import Journal, Settings

    ledger = directory / "slack-pilot.sqlite3"
    _check_file(ledger)
    with closing(sqlite3.connect(ledger.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        binding = conn.execute("SELECT binding FROM identity WHERE id=1").fetchone()
    _require(binding, "review_journal_unavailable")
    assert binding is not None
    journal = Journal(ledger, binding[0])
    reviewer, team, channel, audience = (
        policy[k] for k in ("reviewer", "team", "channel", "audience")
    )
    with journal.db() as db:
        journal.active(db)
        row = db.execute(
            "SELECT p.id,p.snapshot,p.expires,p.policy FROM presentations p "
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
            and cfg.approval_provider == policy.get("provider", "slack")
            and role["aggregate"]
            and journal.opaque("policy", [cfg.team, cfg.channel, role, cfg.reviewer, cfg.recipient])
            == row[3],
            "release_review_permission_changed",
        )
        return {
            "engagement": seen["engagement"],
            "expires_at": row[2],
            "provider": cfg.approval_provider,
            "presentation_id": row[0],
        }


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
        _inspection(
            directory,
            organization,
            run_id,
            run_revision,
            artifact_sha,
            policy,
            seen["presentation_id"],
            fresh=True,
        )
        _record(work / "release-decision.json", value)
    return value


def authorize_release(
    directory: Path, organization: str, run_id: str, run_revision: str, artifact_sha: str
) -> Json:
    store = LocalTrainingStore(directory / "training.sqlite3", organization, existing_only=True)
    with store.engagement_guard():
        work, policy = _binding(directory, organization, run_id, run_revision, artifact_sha)
        receipt = _private_json(work / "release-decision.json")
        _require(
            all(receipt.get(k) == v for k, v in policy.items())
            and receipt["decision"] == "approved"
            and receipt.get("provider") == policy.get("provider", "slack")
            and receipt["organization"] == organization
            and receipt["run_id"] == run_id
            and receipt["run_revision"] == run_revision
            and receipt["artifact_sha"] == artifact_sha,
            "human_release_approval_required",
        )
        _inspection(
            directory,
            organization,
            run_id,
            run_revision,
            artifact_sha,
            policy,
            receipt["presentation_id"],
            fresh=False,
        )
    return receipt
