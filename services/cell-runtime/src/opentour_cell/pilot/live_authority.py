"""Resolve live authority from a custodian-signed local configuration and current custody."""

from __future__ import annotations

import hmac
import os
import re
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_mcp import (
    CatalogFeed,
    CatalogPage,
    HistoricalPair,
    IntakeError,
    IntakePrincipal,
    LiveBinding,
    Starter,
    require_ref,
)
from opentour_cell.pilot.live_evidence import (
    FeedGrant,
    LiveEvidence,
    LiveScope,
    _canonical,
    _digest,
)
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.training import LocalTrainingStore

SCHEMA = "opentour.pilot.live-authority.v1"
SELECTION_SCHEMA = "opentour.pilot.local-live-selection.v1"
_FIELDS = {
    "schema_version",
    "tenant",
    "workflow_ref",
    "engagement_epoch",
    "dataset_ref",
    "dataset_revision",
    "custodian",
    "expires_at",
    "state",
    "actors",
    "grants",
}
_ROLES = {"inspect", "intake", "custodian"}


def _require(value: object) -> None:
    if not value:
        raise IntakeError("live_evidence_unavailable")


class PrivateLiveAuthority:
    """Consume an existing grant; setup never mints custodian or source authority.

    Configuration replacement belongs to the local custody writer and must use
    the existing engagement guard. The file itself is bounded, private and
    authenticated. Every operation reloads it; no UI role or event can override it.
    """

    def __init__(self, directory: Path, tenant: str, workflow_ref: str):
        self.directory = directory
        self.tenant = require_ref(tenant)
        self.workflow_ref = require_ref(workflow_ref)
        _require(directory.is_absolute() and directory.resolve(strict=True) == directory)
        self.path = directory / f"live-authority-{workflow_ref}.json"

    def _document(self) -> dict[str, Any]:
        keys = _read_private(self.directory / "live-authority-key.json", 4096)
        envelope = _read_private(self.path, 32768)
        _require(set(keys) == {"key"} and set(envelope) == {"grant", "seal_sha256"})
        key, body, signature = keys["key"], envelope["grant"], envelope["seal_sha256"]
        _require(isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key))
        _require(isinstance(signature, str) and re.fullmatch(r"[0-9a-f]{64}", signature))
        _require(isinstance(body, dict) and set(body) == _FIELDS)
        _require(
            hmac.compare_digest(
                signature,
                hmac.digest(bytes.fromhex(key), _canonical(body).encode(), "sha256").hex(),
            )
        )
        _require(body["schema_version"] == SCHEMA and body["state"] == "current")
        _require(body["tenant"] == self.tenant and body["workflow_ref"] == self.workflow_ref)
        _require(type(body["expires_at"]) is int and int(time.time()) < body["expires_at"] < 2**63)
        for name in ("engagement_epoch", "dataset_ref", "dataset_revision", "custodian"):
            require_ref(body[name])
        _require(isinstance(body["actors"], dict) and len(body["actors"]) <= 128)
        for actor, roles in body["actors"].items():
            require_ref(actor)
            _require(isinstance(roles, list) and all(isinstance(r, str) for r in roles))
            _require(set(roles) <= _ROLES and len(roles) == len(set(roles)))
        _require("custodian" in body["actors"].get(body["custodian"], []))
        _require(isinstance(body["grants"], list) and 0 < len(body["grants"]) <= 50)
        resources = set()
        for raw in body["grants"]:
            _require(isinstance(raw, dict) and set(raw) == FeedGrant.__dataclass_fields__.keys())
            _require(raw["kind"] in {"decision", "independent_outcome"})
            for name in (
                "resource",
                "revision",
                "source_revision",
                "deciding_namespace",
                "lease_ref",
            ):
                require_ref(raw[name])
            _require(raw["resource"] not in resources)
            resources.add(raw["resource"])
            _require(type(raw["checkpoint"]) is int and 0 <= raw["checkpoint"] < 2**63)
            if raw["kind"] == "independent_outcome":
                require_ref(raw["verifier"])
            else:
                _require(raw["verifier"] is None)
        return dict(body)

    @contextmanager
    def guard(self, actor: IntakePrincipal, workflow_ref: str) -> Iterator[LiveScope]:
        _require(actor.tenant == self.tenant and workflow_ref == self.workflow_ref)
        training = LocalTrainingStore(
            self.directory / "training.sqlite3", self.tenant, existing_only=True
        )
        with training.engagement_guard(read_only=True):
            body = self._document()
            _require(body["engagement_epoch"] == training.engagement_id)
            roles = body["actors"].get(actor.actor, [])
            _require(bool(roles))
            # This binding cannot invent a workflow. Starter admission owns identity.
            with _open_db(self.directory / "intake.sqlite3") as db:
                _require(db is not None)
                assert db is not None
                existing = db.execute(
                    "SELECT epoch FROM intake_workflows WHERE tenant=? AND workflow_ref=?",
                    (self.tenant, self.workflow_ref),
                ).fetchone()
                _require(existing and existing[0] == body["engagement_epoch"])
            scope = LiveScope(
                self.tenant,
                self.workflow_ref,
                body["engagement_epoch"],
                body["dataset_ref"],
                body["dataset_revision"],
                tuple(FeedGrant(**grant) for grant in body["grants"]),
                "custodian" in roles,
                "intake" in roles,
                "inspect" in roles,
                body["custodian"],
                body["expires_at"],
                _digest(body),
            )
            yield scope
            _require(self._document() == body)


class LocalLiveIntake:
    """The OPE-430 typed admission port backed by the actual durable binding store."""

    def __init__(self, authority: PrivateLiveAuthority, actor: str):
        self.authority = authority
        self.principal = IntakePrincipal(authority.tenant, require_ref(actor), "intake")
        self.live = LiveEvidence(authority.directory, authority)

    def historical(self, actor: IntakePrincipal, request: HistoricalPair) -> None:
        return None

    def starter(self, actor: IntakePrincipal, request: Starter) -> None:
        return None

    def benchmark(self, actor: IntakePrincipal, workflow_ref: str) -> None:
        return None

    @contextmanager
    def guard(self, actor: IntakePrincipal) -> Iterator[None]:
        _require(actor == self.principal)
        with self.authority.guard(actor, self.authority.workflow_ref) as scope:
            _require(scope.can_ingest)
            yield

    def resolve(
        self, feed_binding_ref: str, authenticated_actor: IntakePrincipal
    ) -> LiveBinding | None:
        if authenticated_actor != self.principal:
            return None
        return self.live.resolve(feed_binding_ref, authenticated_actor)

    def permits_live_revision(
        self, actor: IntakePrincipal, binding: LiveBinding, source_revision: str
    ) -> bool:
        with self.authority.guard(actor, self.authority.workflow_ref) as scope:
            return scope.can_ingest and any(
                grant.resource == binding.resource
                and grant.revision == binding.grant_revision
                and grant.source_revision == source_revision == binding.source_revision
                for grant in scope.grants
            )

    def discover(self, actor: IntakePrincipal, *, limit: int, cursor: str | None) -> CatalogPage:
        _require(type(limit) is int and 1 <= limit <= 50)
        with self.authority.guard(actor, self.authority.workflow_ref) as scope:
            _require(scope.can_ingest)
            try:
                with self.live._db() as db:
                    rows = db.execute(
                        "SELECT ref FROM live_bindings WHERE tenant=? AND workflow=? AND epoch=? "
                        "AND successor IS NULL ORDER BY ref",
                        (scope.tenant, scope.workflow_ref, scope.engagement_epoch),
                    ).fetchall()
            except FileNotFoundError:
                _require(
                    not any(
                        os.path.lexists(str(self.live.path) + suffix)
                        for suffix in ("-wal", "-shm", "-journal")
                    )
                )
                _require(cursor is None)
                return CatalogPage(())
            feeds = []
            for row in rows:
                binding = self.resolve(row[0], actor)
                if binding and binding.state == "current":
                    feeds.append(
                        CatalogFeed(
                            row[0],
                            binding.binding_version,
                            binding.feed_kind,
                            binding.source_revision,
                        )
                    )
            if cursor is not None:
                positions = [i for i, feed in enumerate(feeds) if feed.feed_binding_ref == cursor]
                _require(len(positions) == 1)
                feeds = feeds[positions[0] + 1 :]
            page = feeds[:limit]
            return CatalogPage(
                tuple(page), page[-1].feed_binding_ref if len(feeds) > limit else None
            )

    def live_reader_available(self, actor: IntakePrincipal, workflow_ref: str) -> bool:
        # Admission alone does not attest to an installed source adapter. The
        # source-reader composition must override this with actual configured capability.
        return False


def open_live_intake(selection: Path) -> tuple[IntakePrincipal, DurableIntake]:
    body = _read_private(selection, 4096)
    _require(set(body) == {"schema_version", "tenant", "workflow_ref", "actor"})
    _require(body["schema_version"] == SELECTION_SCHEMA)
    authority = PrivateLiveAuthority(selection.parent, body["tenant"], body["workflow_ref"])
    intake = LocalLiveIntake(authority, body["actor"])
    with intake.guard(intake.principal):
        return intake.principal, DurableIntake(selection.parent, intake)
