"""Resolve live authority from a locally sealed configuration and current custody.

Integrity here is a self-consistency check, not issuer authentication: see `PrivateLiveAuthority`.
"""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from opentour_cell.pilot.engagement_epochs import EngagementLedger
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
from opentour_cell.pilot.receipt_recovery import _has_table, _open_db
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.workflow_benchmarks import BenchmarkScope, WorkflowBenchmarks

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
LEDGER_REQUIRED_MISSING = "engagement_ledger_required_missing"
# Shown to the authenticated caller. It names exactly what a grant must carry.
LEDGER_REQUIRED_MISSING_MESSAGE = (
    LEDGER_REQUIRED_MISSING + ': the grant body must carry "engagement_ledger_required": true'
)
_ROLES = {"inspect", "intake", "custodian", "verify", "workflow_owner"}


def _require(value: object) -> None:
    if not value:
        raise IntakeError("live_evidence_unavailable")


class PrivateLiveAuthority:
    """Consume an existing grant; setup never mints custodian or source authority.

    Configuration replacement belongs to the local custody writer and must use
    the existing engagement guard. The file itself is bounded and private. Every
    operation reloads it; no UI role or event can override it.

    What the "seal" is, and is not. `seal_sha256` is an HMAC-SHA256 over the canonical
    grant body, keyed by the symmetric hex key in `live-authority-key.json`, which sits in
    the same private directory as the grant and the engagement ledger. It proves the grant
    is self-consistent with that key. It does not authenticate an issuer: anyone able to
    write this directory can mint or alter a grant. No key-custody or custodian-identity
    contract exists yet.

    Epoch enforcement is mandatory; there is no opt-out. A grant MUST carry
    `"engagement_ledger_required": true` in its body (the sealed `grant` object, beside
    `state` and `grants`). A grant without the field is refused with
    `engagement_ledger_required_missing`; `false` and any other value are refused with
    `live_evidence_unavailable`. With `true`, the engagement-epoch ledger must exist and be
    enrolled, else the operation holds with `engagement_epoch_hold`. To migrate an older
    grant, add that one field and re-seal it. An engagement that does not want epoch
    enforcement must not use live evidence.
    """

    def __init__(self, directory: Path, tenant: str, workflow_ref: str):
        self.directory = directory
        self.tenant = require_ref(tenant)
        self.workflow_ref = require_ref(workflow_ref)
        _require(directory.is_absolute() and directory.resolve(strict=True) == directory)
        self.path = directory / f"live-authority-{workflow_ref}.json"

    @staticmethod
    def _archive_mac(key: bytes, revision: str, body: dict[str, Any]) -> str:
        purpose = hmac.digest(key, b"opentour.live-aggregate-archive.v1", "sha256")
        return hmac.digest(purpose, _canonical([revision, body]).encode(), "sha256").hex()

    def _seal_snapshot_archive(self, scope: LiveScope, body: dict[str, Any]) -> dict[str, str]:
        document, key = self._authenticated_document()
        _require(scope.authority_revision == _digest(document))
        mac = self._archive_mac(key, scope.authority_revision, body)
        # Check again inside the live writer, before it can commit the page or form.
        _require(self._authenticated_document() == (document, key))
        return {
            "version": "v1",
            "authority_revision": scope.authority_revision,
            "mac": mac,
        }

    def _verify_snapshot_archive(self, body: dict[str, Any], seal: Any) -> Callable[[], None]:
        _require(isinstance(seal, dict) and set(seal) == {"version", "authority_revision", "mac"})
        _require(
            seal["version"] == "v1"
            and all(
                isinstance(seal[k], str) and re.fullmatch("[0-9a-f]{64}", seal[k])
                for k in ("authority_revision", "mac")
            )
        )
        document, key = self._authenticated_document()
        _require(
            hmac.compare_digest(
                seal["mac"], self._archive_mac(key, seal["authority_revision"], body)
            )
        )

        def revalidate() -> None:
            _require(self._authenticated_document() == (document, key))

        revalidate()
        return revalidate

    def _authenticated_document(self) -> tuple[dict[str, Any], bytes]:
        keys = _read_private(self.directory / "live-authority-key.json", 4096)
        envelope = _read_private(self.path, 32768)
        _require(set(keys) == {"key"} and set(envelope) == {"grant", "seal_sha256"})
        key, body, signature = keys["key"], envelope["grant"], envelope["seal_sha256"]
        _require(isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key))
        _require(isinstance(signature, str) and re.fullmatch(r"[0-9a-f]{64}", signature))
        _require(
            isinstance(body, dict)
            and _FIELDS <= body.keys()
            and body.keys() <= _FIELDS | {"slack_verifiers", "engagement_ledger_required"}
        )
        _require(body.get("engagement_ledger_required", True) is True)
        _require(
            hmac.compare_digest(
                signature,
                hmac.digest(bytes.fromhex(key), _canonical(body).encode(), "sha256").hex(),
            )
        )
        if "engagement_ledger_required" not in body:
            # Checked after the seal so the guidance is only given for a self-consistent grant.
            raise IntakeError(LEDGER_REQUIRED_MISSING_MESSAGE)
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
        assignments = body.get("slack_verifiers", {})
        _require(isinstance(assignments, dict) and len(assignments) <= 50)
        for resource, subject in assignments.items():
            _require(isinstance(subject, str) and re.fullmatch(r"[UW][A-Z0-9]{2,31}", subject))
            _require("verify" in body["actors"].get(subject, []))
            _require(
                any(
                    g["resource"] == resource
                    and g["kind"] == "independent_outcome"
                    and g["verifier"] == subject
                    for g in body["grants"]
                )
            )
        return dict(body), bytes.fromhex(key)

    def _document(self) -> dict[str, Any]:
        return self._authenticated_document()[0]

    def engagement_gate(self, scope: LiveScope) -> EngagementLedger:
        """Open the engagement ledger; always enforcing, never None.

        `_document` has already refused a grant whose `engagement_ledger_required` is absent
        (`engagement_ledger_required_missing`) or anything other than true. The seal is a
        locally keyed HMAC with a co-located key; it proves self-consistency only, so anyone
        with write access to the directory can mint or alter a grant (though one without the
        requirement is refused), and could also replace the ledger itself.
        """
        body = self._document()
        _require(_digest(body) == scope.authority_revision)
        try:
            ledger = EngagementLedger(self.directory, existing_only=True)
            for grant in scope.grants:
                checkpoint = ledger.live_gate(
                    scope.tenant, scope.engagement_epoch, grant.resource, grant.lease_ref
                )
                if grant.checkpoint < checkpoint:
                    raise ValueError("source_checkpoint_mismatch")
        except (ValueError, sqlite3.Error):
            raise IntakeError("engagement_epoch_hold") from None
        return ledger

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
            # Check admitted identity separately from the signed live permissions.
            with _open_db(self.directory / "intake.sqlite3") as db:
                _require(db is not None)
                assert db is not None
                existing = db.execute(
                    "SELECT epoch FROM intake_workflows WHERE tenant=? AND workflow_ref=?",
                    (self.tenant, self.workflow_ref),
                ).fetchone()
                if existing is not None:
                    _require(existing[0] == body["engagement_epoch"])
                else:
                    _require(_has_table(db, "intake_benchmark_admissions"))
                    historical = db.execute(
                        "SELECT 1 FROM intake_benchmark_admissions b JOIN intake_runs r "
                        "USING(tenant,run_ref,workflow_ref) "
                        "WHERE b.tenant=? AND b.workflow_ref=? AND b.epoch=? "
                        "AND json_type(r.request,'$.pair_ref')='text' LIMIT 1",
                        (self.tenant, workflow_ref, body["engagement_epoch"]),
                    ).fetchone()
                    _require(
                        historical is not None
                        and WorkflowBenchmarks(self.directory).observed_workflow(
                            BenchmarkScope(self.tenant, body["engagement_epoch"], workflow_ref)
                        )
                    )
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
                tuple(sorted(body.get("slack_verifiers", {}).items())),
                "verify" in roles and actor.role == "slack",
                "workflow_owner" in roles and actor.role == "slack",
            )
            self.engagement_gate(scope)
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
        from opentour_cell.pilot.live_reader import available

        return available(self.authority, actor, workflow_ref)


def open_live_intake(selection: Path) -> tuple[IntakePrincipal, DurableIntake]:
    body = _read_private(selection, 4096)
    _require(set(body) == {"schema_version", "tenant", "workflow_ref", "actor"})
    _require(body["schema_version"] == SELECTION_SCHEMA)
    authority = PrivateLiveAuthority(selection.parent, body["tenant"], body["workflow_ref"])
    intake = LocalLiveIntake(authority, body["actor"])
    with intake.guard(intake.principal):
        return intake.principal, DurableIntake(selection.parent, intake)
