"""Scope private Slack confirmation to an existing managed plan and operator grant."""

from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from opentour_cell.pilot.managed_run import ManagedSelection
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.repair_workflow import (
    ATTESTATION,
    MODEL_PROFILE,
    _context,
    _model_grant_path,
    _name,
    _private_json,
    _record,
    _require,
    inspect_managed,
)
from opentour_cell.pilot.training_data import identifier

Json = dict[str, Any]


def grant_slack_launch(
    directory: Path,
    organization: str,
    manifest_path: Path,
    authorization_path: Path,
    *,
    actor: str,
    team: str,
    channel: str,
    attestation: str,
    expires_at: int,
) -> str:
    selection = ManagedSelection(
        directory, organization, manifest_path, authorization_path, prerequisites=inspect_managed
    )
    manifest = selection.read()
    current = selection.grant(manifest)
    store, intake = _context(directory, organization, manifest)
    _require(
        ATTESTATION.fullmatch(attestation)
        and type(expires_at) is int
        and time.time() < expires_at <= min(current["expires_at"], intake["expires_at"]),
        "launch_permission_unavailable",
    )
    item = {
        "organization": organization,
        "dataset_id": intake["dataset_id"],
        "engagement": store.engagement_id,
        "plan_revision": digest(manifest),
        "authorization_revision": digest(current),
        "expires_at": expires_at,
        "trainer": identifier(actor),
        "team": identifier(team),
        "channel": identifier(channel),
        "attestation": attestation,
        "manifest": manifest_path.name,
        "authorization": authorization_path.name,
    }
    with store.engagement_guard(engagement_id=store.engagement_id):
        _record(directory / ("slack-launch-" + item["dataset_id"] + ".json"), item)
    return digest(item)


def _selected(
    directory: Path, organization: str, dataset: str, actor: str, team: str, channel: str
) -> tuple[ManagedSelection, Json]:
    item = _private_json(directory / ("slack-launch-" + identifier(dataset) + ".json"))
    _require(
        (organization, dataset, actor, team, channel)
        == tuple(item[k] for k in ("organization", "dataset_id", "trainer", "team", "channel"))
        and time.time() < item["expires_at"],
        "launch_permission_unavailable",
    )
    sel = ManagedSelection(
        directory,
        organization,
        directory / _name(item["manifest"]),
        directory / _name(item["authorization"]),
        prerequisites=inspect_managed,
    )
    manifest = sel.read()
    store, intake = _context(directory, organization, manifest)
    _require(
        digest(manifest) == item["plan_revision"]
        and digest(sel.grant(manifest)) == item["authorization_revision"]
        and intake["dataset_id"] == dataset
        and store.engagement_id == item["engagement"],
        "stale_launch",
    )
    model = manifest.get("open_weight_profile") == MODEL_PROFILE
    if model:
        grant = _private_json(_model_grant_path(directory, manifest))
        _require(
            grant["trainer"] == actor
            and grant["plan_revision"] == item["plan_revision"]
            and grant["expires_at"] > time.time(),
            "qualified_trainer_required",
        )
    return sel, {
        "dataset_id": dataset,
        "plan_revision": item["plan_revision"],
        "trainer": actor,
        "scope": "conditional_open_weight" if model else "simple_repair",
        "expires_at": item["expires_at"],
    }


def managed_launch_summary(
    directory: Path, organization: str, dataset: str, actor: str, team: str, channel: str
) -> Json:
    return _selected(directory, organization, dataset, actor, team, channel)[1]


def start_managed_from_slack(
    directory: Path,
    organization: str,
    dataset: str,
    actor: str,
    team: str,
    channel: str,
    plan_revision: str,
    idempotency_ref: str,
) -> Json:
    sel, scope = _selected(directory, organization, dataset, actor, team, channel)
    _require(scope["plan_revision"] == plan_revision, "stale_launch")
    result = sel.start(plan_revision, idempotency_ref)
    return {"run_id": result["run_id"], "state": result["state"]}


def managed_status_from_slack(
    directory: Path,
    organization: str,
    dataset: str,
    actor: str,
    team: str,
    channel: str,
    run_id: str,
) -> Json:
    sel, _ = _selected(directory, organization, dataset, actor, team, channel)
    result = sel.status(run_id)
    return {"run_id": result["run_id"], "state": result["state"]}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("directory", "manifest", "authorization"):
        p.add_argument("--" + name, required=True, type=Path)
    for name in ("organization", "actor", "team", "channel", "attestation"):
        p.add_argument("--" + name, required=True)
    p.add_argument("--expires-at", required=True, type=int)
    a = p.parse_args()
    try:
        revision = grant_slack_launch(
            a.directory,
            a.organization,
            a.manifest,
            a.authorization,
            actor=a.actor,
            team=a.team,
            channel=a.channel,
            attestation=a.attestation,
            expires_at=a.expires_at,
        )
    except (OSError, ValueError, KeyError):
        p.exit(2, "private_launch_permission_unavailable\n")
    print(canonical({"permission_revision": revision}))
    return 0
