"""Read owner-approved workflow metadata from the private Slack pilot launch."""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from opentour_cell.pilot.intake_mcp import IntakePrincipal, require_ref
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.repair_bundle import _read
from opentour_cell.pilot.repair_workflow import _name
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope
from opentour_cell.pilot.workflow_index import (
    IndexScope,
    ProposalAccess,
    WorkflowLabel,
    WorkflowVisibility,
)

if TYPE_CHECKING:
    from opentour_cell.pilot.slack_adapter import Settings

PUBLICATION_PURPOSE = "opentour.pilot.workflow-index-publication.v1"
REGISTRY_FILE = "pilot-issuer-registry.json"
_PUBLICATION_FIELDS = {
    "profile_version",
    "tenant",
    "engagement_id",
    "issued_at",
    "not_after",
    "workflows",
}
_WORKFLOW_FIELDS = {"workflow_ref", "label", "aliases", "directory_viewers"}
_MAX_PUBLICATION_BYTES = 128 * 1024


def _label(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 0 < len(value) <= 64
        or value != value.strip()
        or not value.isprintable()
    ):
        raise ValueError("invalid_workflow_label")
    return value


def open_workflow_index_scope(
    cfg: Settings, actor: IntakePrincipal, proposals: tuple[ProposalAccess, ...]
) -> IndexScope | None:
    """Verify current naming and directory permissions without deriving proposal grants."""
    if not cfg.workflow_index_publication_file or actor.tenant != cfg.organization:
        return None
    if actor.role != "slack":
        return None

    # Older launches that do not configure an index never load the verifier dependencies.
    from opentour_journal.crypto import canonical, sha256

    from opentour_cell.pilot.signed_records import (
        MAX_CONFIG_BYTES,
        MonotonicRecord,
        PilotRecordRefused,
        PilotVerifier,
        TrustStateStore,
    )
    from opentour_cell.pilot.slack_adapter import _slack_id

    try:
        member = cfg.permission(actor.actor, "inspect")
        registry_path = cfg.directory / REGISTRY_FILE
        publication_path = cfg.directory / _name(cfg.workflow_index_publication_file)
        registry = _read(registry_path, MAX_CONFIG_BYTES)
        publication = _read(publication_path, _MAX_PUBLICATION_BYTES)
        payload = PilotVerifier(registry).verify(publication, PUBLICATION_PURPOSE)
        if (
            payload.keys() != _PUBLICATION_FIELDS
            or payload["profile_version"] != "1.0"
            or payload["tenant"] != actor.tenant
        ):
            return None
        epoch = require_ref(payload["engagement_id"])
        issued_at, not_after = payload["issued_at"], payload["not_after"]
        if not isinstance(issued_at, str) or not isinstance(not_after, str):
            return None
        valid_until = min(member["expires_at"], int(datetime.fromisoformat(not_after).timestamp()))
        if valid_until <= time.time():
            return None
        entries = payload["workflows"]
        if not isinstance(entries, list) or len(entries) > 64:
            return None
        owner_ref = digest([actor.tenant, epoch, "customer_owner"])
        labels = []
        workflows = []
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or entry.keys() != _WORKFLOW_FIELDS:
                return None
            workflow = require_ref(entry["workflow_ref"])
            aliases = entry["aliases"]
            viewers = entry["directory_viewers"]
            if (
                workflow in seen
                or not isinstance(aliases, list)
                or len(aliases) > 12
                or not isinstance(viewers, list)
                or len(viewers) > 128
                or any(not _slack_id(viewer, "UW") for viewer in viewers)
            ):
                return None
            seen.add(workflow)
            label = _label(entry["label"])
            terms = tuple(_label(alias) for alias in aliases)
            if len(set(terms)) != len(terms) or len(set(viewers)) != len(viewers):
                return None
            scope = BenchmarkScope(actor.tenant, epoch, workflow)
            labels.append(WorkflowLabel(scope, owner_ref, digest([issued_at, entry]), label, terms))
            if actor.actor in viewers:
                workflows.append(WorkflowVisibility(scope, actor.actor, valid_until))
        # Keep the signed publication head outside the worker volume. A valid old
        # document restored from a snapshot must not restore directory access.
        issue_time = datetime.fromisoformat(issued_at)
        sequence = (issue_time - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(milliseconds=1)
        floor = MonotonicRecord(
            "workflow_index_publication:" + digest([actor.tenant, epoch]),
            sequence,
            {"payload_sha256": sha256(canonical(payload))},
        )
        trust = TrustStateStore()
        with trust.locked():
            if (
                _read(registry_path, MAX_CONFIG_BYTES) != registry
                or _read(publication_path, _MAX_PUBLICATION_BYTES) != publication
            ):
                return None
            # The store enforces strict advancement or an identical payload retry.
            trust.advance(floor, lambda _previous, _proposed: None)
        return IndexScope(
            actor,
            epoch,
            digest([cfg.team, cfg.app, cfg.channel, actor.actor, member]),
            valid_until,
            tuple(labels),
            proposals,
            tuple(workflows),
        )
    except (OSError, ValueError, PilotRecordRefused):
        return None
