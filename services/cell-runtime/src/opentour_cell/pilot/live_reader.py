"""Customer-local read-only adapter for bounded, revision-pinned decision or outcome exports."""

from __future__ import annotations

import hmac
import os
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from opentour_cell.pilot.intake_admission import DurableIntake, IntakeWork
from opentour_cell.pilot.intake_local import open_intake
from opentour_cell.pilot.intake_mcp import CatalogFeed, IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.live_authority import (
    SELECTION_SCHEMA,
    LocalLiveIntake,
    PrivateLiveAuthority,
)
from opentour_cell.pilot.live_evidence import FeedGrant, LiveScope, _canonical, _digest
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.repair_workflow import _record

MAPPING = "opentour.pilot.live-export-mapping.v1"
CATALOG = "opentour.pilot.live-export-catalog.v1"
PAGE = "opentour.pilot.live-export-page.v1"
Json = dict[str, Any]


def _require(condition: object) -> None:
    if not condition:
        raise IntakeError("live_export_unavailable")


def _file_pin(value: Any, kind: str) -> None:
    _require(isinstance(value, dict) and set(value) == {"file", "sha256"})
    _require(
        isinstance(value["file"], str)
        and re.fullmatch(rf"live-export-{kind}-[a-zA-Z0-9_-]{{1,96}}\.json", value["file"])
    )
    _require(isinstance(value["sha256"], str) and re.fullmatch("[0-9a-f]{64}", value["sha256"]))


def _mapping(authority: PrivateLiveAuthority, scope: LiveScope) -> Json:
    config = _read_private(authority.directory / f"live-export-{scope.workflow_ref}.json", 32768)
    keyfile = _read_private(authority.directory / "live-authority-key.json", 4096)
    _require(set(config) == {"mapping", "seal_sha256"} and set(keyfile) == {"key"})
    mapping = config["mapping"]
    _require(
        isinstance(mapping, dict)
        and set(mapping) == {"schema_version", "authority_revision", "resources"}
    )
    seal = hmac.digest(bytes.fromhex(keyfile["key"]), _canonical(mapping).encode(), "sha256").hex()
    _require(
        isinstance(config["seal_sha256"], str) and hmac.compare_digest(config["seal_sha256"], seal)
    )
    _require(
        mapping["schema_version"] == MAPPING
        and mapping["authority_revision"] == scope.authority_revision
    )
    resources = mapping["resources"]
    _require(isinstance(resources, dict) and 0 < len(resources) <= 50)
    _require(set(resources) <= {g.resource for g in scope.grants})
    for pin in resources.values():
        _file_pin(pin, "catalog")
    return dict(resources)


def available(
    authority: PrivateLiveAuthority,
    actor: IntakePrincipal,
    workflow: str,
    resource: str | None = None,
) -> bool:
    """Inspect signed installation metadata only. No export files or events are read."""
    try:
        with authority.guard(actor, workflow) as scope:
            if not scope.can_ingest:
                return False
            resources = _mapping(authority, scope)
            if resource is not None:
                return resource in resources
            local = LocalLiveIntake(authority, actor.actor)
            page = local.discover(actor, limit=50, cursor=None)
            for entry in page.sources:
                if not isinstance(entry, CatalogFeed):
                    return False
                binding = local.resolve(entry.feed_binding_ref, actor)
                if binding is None or binding.resource not in resources:
                    return False
            return bool(page.sources) and page.next_cursor is None
    except (ValueError, TypeError, KeyError, OSError):
        return False


def _pinned(directory: Path, pin: Json, limit: int) -> Json:
    value = _read_private(directory / pin["file"], limit)
    _require(_digest(value) == pin["sha256"])
    return value


def _catalog(value: Json, scope: LiveScope, grant: FeedGrant) -> list[Json]:
    _require(set(value) == {"schema_version", "scope", "grant", "pages", "event_revisions"})
    _require(value["schema_version"] == CATALOG and value["grant"] == asdict(grant))
    _require(value["event_revisions"] in {"versioned", "immutable_event_id"})
    expected = {
        k: getattr(scope, k)
        for k in ("tenant", "workflow_ref", "engagement_epoch", "dataset_ref", "dataset_revision")
    }
    _require(value["scope"] == expected)
    pages = value["pages"]
    _require(isinstance(pages, list) and 0 < len(pages) <= 32)
    seen = set()
    previous = grant.checkpoint
    for index, p in enumerate(pages):
        _require(
            isinstance(p, dict)
            and set(p) == {"pin", "page_ref", "checkpoint", "read_metadata", "page_digest"}
        )
        _file_pin(p["pin"], "page")
        _require(
            isinstance(p["page_digest"], str) and re.fullmatch("[a-f0-9]{64}", p["page_digest"])
        )
        ref = require_ref(p["page_ref"])
        _require(
            ref not in seen and type(p["checkpoint"]) is int and previous <= p["checkpoint"] < 2**63
        )
        seen.add(ref)
        previous = p["checkpoint"]
        m = p["read_metadata"]
        _require(
            isinstance(m, dict)
            and set(m)
            == {"interval_start", "interval_end", "coverage_through", "complete", "next_cursor"}
        )
        start, end = m["interval_start"], m["interval_end"]
        _require(
            (start is None and end is None)
            or (type(start) is int and type(end) is int and 0 <= start <= end < 2**63)
        )
        _require(
            m["coverage_through"] is None
            or (
                type(m["coverage_through"]) is int
                and start is not None
                and start <= m["coverage_through"] <= end
            )
        )
        _require(type(m["complete"]) is bool)
        if index + 1 < len(pages):
            _require(
                not m["complete"]
                and isinstance(pages[index + 1], dict)
                and m["next_cursor"] == pages[index + 1].get("page_ref")
            )
        else:
            _require(m["next_cursor"] is None)

    return list(pages)


def materialize_live(selection: Path, run_ref: str) -> bool:
    """Process an already admitted operation. The intake worker fence owns retries."""
    actor, service = open_intake(selection)
    intake = getattr(service, "intake", service)
    _require(isinstance(intake, DurableIntake))
    assert isinstance(intake, DurableIntake)
    local = intake.authority
    _require(isinstance(local, LocalLiveIntake))
    assert isinstance(local, LocalLiveIntake)
    authority = local.authority

    def read(work: IntakeWork) -> str:
        _require(work.mode == "live" and len(work.revisions) == 2)
        ref, source_revision = work.revisions
        with authority.guard(actor, work.workflow_ref) as scope:
            grant = next((g for g in scope.grants if g.resource == work.resource.resource_id), None)
            _require(grant is not None and grant.source_revision == source_revision)
            assert grant is not None
            pin = _mapping(authority, scope).get(grant.resource)
            _require(isinstance(pin, dict))
            assert isinstance(pin, dict)

            def recheck() -> None:
                with authority.guard(actor, work.workflow_ref) as fresh:
                    _require(
                        fresh == scope
                        and fresh.can_ingest
                        and _mapping(authority, fresh).get(grant.resource) == pin
                    )
                    binding = local.resolve(ref, actor)
                    _require(
                        binding is not None
                        and binding.state == "current"
                        and binding.resource == grant.resource
                        and binding.source_revision == source_revision
                        and binding.grant_revision == grant.revision
                    )

            recheck()
            catalog = _pinned(authority.directory, pin, 65536)
            pages = _catalog(catalog, scope, grant)
            # Pin one catalog across restarts for the admitted source revision.
            recheck()
            _record(
                authority.directory
                / ("live-export-read-" + _digest([ref, source_revision]) + ".json"),
                {"catalog_sha256": pin["sha256"]},
            )
            receipts = []
            for p in pages:
                recheck()
                with local.live._db() as db:
                    previous = db.execute(
                        "SELECT digest,receipt_ref FROM live_pages "
                        "WHERE binding_ref=? AND page_ref=?",
                        (ref, p["page_ref"]),
                    ).fetchone()
                if previous is not None:
                    _require(previous[0] == p["page_digest"])
                    receipts.append(str(previous[1]))
                    continue
                page = _pinned(authority.directory, p["pin"], 262144)
                _require(
                    set(page) == {"schema_version", "page_ref", "rows"}
                    and page["schema_version"] == PAGE
                    and page["page_ref"] == p["page_ref"]
                )
                rows = page["rows"]
                _require(isinstance(rows, list) and len(rows) <= 1000)
                for row in rows:
                    _require(
                        isinstance(row, dict)
                        and set(row) == {"position", "epoch", "event"}
                        and isinstance(row["event"], dict)
                    )
                if catalog["event_revisions"] == "immutable_event_id":
                    # Only an explicitly immutable feed can omit the envelope revision.
                    for row in rows:
                        _require(row["event"].get("event_revision", 1) == 1)
                    events = tuple({"event_revision": 1, **r["event"]} for r in rows)
                else:
                    events = tuple(r["event"] for r in rows)
                positions = tuple(r["position"] for r in rows)
                epochs = tuple(r["epoch"] for r in rows)
                _require(
                    _digest(
                        [
                            source_revision,
                            events,
                            positions,
                            epochs,
                            p["checkpoint"],
                            p["read_metadata"],
                        ]
                    )
                    == p["page_digest"]
                )
                recheck()
                receipts.append(
                    local.live.ingest_page(
                        actor,
                        ref,
                        page_ref=p["page_ref"],
                        source_revision=source_revision,
                        events=events,
                        source_positions=positions,
                        event_epochs=epochs,
                        checkpoint=p["checkpoint"],
                        read_metadata=p["read_metadata"],
                    )
                )
            recheck()
            return "export-" + _digest(receipts)[:40]

    try:
        changed = intake.materialize(actor, run_ref, read)
        if intake.get_run(actor, run_ref).state == "materialized" and os.path.lexists(
            authority.directory / "readiness-selection.json"
        ):
            # An independently authorized worker refreshes durable readiness after
            # ingestion. A retry can recover this step without rereading sources.
            from opentour_cell.pilot.readiness_selection import open_readiness

            open_readiness(authority.directory, actor.tenant, authority.workflow_ref).refresh()
        return changed
    except (OSError, ValueError, KeyError, TypeError):
        raise IntakeError("live_export_unavailable") from None


def main(argv: list[str] | None = None) -> int:
    """Return bounded status to a local worker operator without disclosing input or paths."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: live_reader PRIVATE_SELECTION ADMITTED_RUN", file=sys.stderr)
        return 2
    try:
        selection, run_ref = Path(args[0]), require_ref(args[1])
        selected = _read_private(selection, 4096)
        _require(selected.get("schema_version") == SELECTION_SCHEMA)
        actor, service = open_intake(selection)
        status = service.get_run(actor, run_ref)
        if status.state in {"queued", "materialized"}:
            changed = materialize_live(selection, run_ref)
            if changed or status.state == "materialized":
                print("materialized" if changed else "already_materialized")
                return 0
    except Exception:
        # CLI boundary: even unexpected parser, filesystem or database errors may
        # contain private paths, field names or source values.
        pass
    print("live_export_unavailable", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
