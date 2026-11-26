"""Owner-local stdio entry for an explicitly selected historical or starter intake grant."""

from __future__ import annotations

import argparse
import hmac
import os
import re
import sqlite3
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from opentour_cell.pilot import source_intake
from opentour_cell.pilot.intake_admission import AuthorizedSource, DurableIntake
from opentour_cell.pilot.intake_mcp import (
    CatalogPage,
    CatalogSource,
    HistoricalPair,
    IntakeError,
    IntakePrincipal,
    LiveBinding,
    Starter,
    require_ref,
    serve_intake,
)
from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.readout import ManifestError
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore

SELECTION_VERSION = "opentour.pilot.local-intake-selection.v1"
STARTER_SELECTION_VERSION = "opentour.pilot.local-starter-selection.v1"
STARTER_GRANT_VERSION = "opentour.pilot.owner-starter-intake-grant.v1"
STARTER_PURPOSE = "starter_policy_draft"
MAX_SELECTION_BYTES = 4096


class HistoricalSourceExpired(IntakeError):
    """The authenticated launcher's selected historical source grant expired."""


def _selected_json(path: Path) -> dict[str, Any]:
    if (
        not path.is_absolute()
        or path.suffix != ".json"
        or path.parent.resolve(strict=True) != path.parent
    ):
        raise IntakeError("private_intake_selection_required")
    return _read_private(path, MAX_SELECTION_BYTES)


@dataclass(frozen=True)
class HistoricalSelection:
    tenant: str
    actor: str
    engagement_epoch: str
    mandate_id: str
    mandate_revision: str
    source_run_id: str
    workflow_ref: str
    pair_ref: str
    trace_source: str
    correction_source: str
    source_revision: str
    correction_revision: str
    policy_revision: str

    @classmethod
    def read(cls, path: Path) -> HistoricalSelection:
        try:
            data = _selected_json(path)
            if set(data) != {"schema_version", *cls.__dataclass_fields__}:
                raise IntakeError("private_intake_selection_required")
            if data["schema_version"] != SELECTION_VERSION:
                raise IntakeError("private_intake_selection_required")
            values = {field: require_ref(data[field]) for field in cls.__dataclass_fields__}
            if (
                re.fullmatch("[0-9a-f]{32}", values["engagement_epoch"]) is None
                or any(
                    re.fullmatch("[0-9a-f]{64}", values[field]) is None
                    for field in (
                        "mandate_revision",
                        "source_revision",
                        "correction_revision",
                        "policy_revision",
                    )
                )
                or values["trace_source"] == values["correction_source"]
                or values["source_revision"] == values["correction_revision"]
            ):
                raise IntakeError("private_intake_selection_required")
            return cls(**values)
        except (OSError, RuntimeError, ManifestError, TrainingError) as error:
            raise IntakeError("private_intake_selection_required") from error


class _LocalAuthority:
    def historical(
        self, actor: IntakePrincipal, request: HistoricalPair
    ) -> AuthorizedSource | None:
        return None

    def starter(self, actor: IntakePrincipal, request: Starter) -> AuthorizedSource | None:
        return None

    def resolve(self, feed_binding_ref: str, authenticated_actor: IntakePrincipal) -> None:
        return None

    def permits_live_revision(
        self, actor: IntakePrincipal, binding: LiveBinding, source_revision: str
    ) -> bool:
        return False

    def live_reader_available(self, actor: IntakePrincipal, workflow_ref: str) -> bool:
        return False

    def benchmark(self, actor: IntakePrincipal, workflow_ref: str) -> None:
        return None


class LocalHistoricalAuthority(_LocalAuthority):
    """Use existing local custody and mandate records; the wire carries no authority fields."""

    def __init__(self, path: Path):
        self.path = path
        self.selected = HistoricalSelection.read(path)
        selection = self.selected
        self.principal = IntakePrincipal(selection.tenant, selection.actor, "intake")
        self.store = LocalTrainingStore(
            path.parent / "training.sqlite3",
            selection.tenant,
            existing_only=True,
        )
        self.mandates = LearningMandates(path.parent, selection.tenant, selection.engagement_epoch)
        self._scope: ContextVar[AuthorizedSource | None] = ContextVar(
            "intake_historical_scope",
            default=None,
        )

    @contextmanager
    def guard(self, actor: IntakePrincipal) -> Iterator[None]:
        selection = self.selected
        if actor != self.principal or HistoricalSelection.read(self.path) != selection:
            raise IntakeError("intake_unavailable")
        try:
            # The existing custody guard comes before the mandate and intake queue locks.
            with (
                self.store.engagement_guard(engagement_id=selection.engagement_epoch),
                self.mandates.guard_evaluation_source(
                    selection.mandate_id, selection.source_run_id, actor=actor.actor
                ) as grant,
            ):
                source = grant["source_request"]
                if (
                    grant["mandate_revision"] != selection.mandate_revision
                    or grant["agreement_revision"] != selection.policy_revision
                    or source["workflow"] != selection.workflow_ref
                    or source["trace_source"] != selection.trace_source
                    or source["correction_source"] != selection.correction_source
                    or source["trace_sha256"] != selection.source_revision
                    or source["correction_sha256"] != selection.correction_revision
                ):
                    raise IntakeError("intake_unavailable")
                bound = source_intake._BoundEngagement(
                    self.store.path, selection.tenant, selection.engagement_epoch
                )
                try:
                    source_intake._scope(
                        bound,
                        self.path.parent / f"cycle-source-{selection.source_run_id}.json",
                        expected_delegation={
                            "authority_kind": grant["authority_kind"],
                            "run_id": selection.source_run_id,
                            "delegate": actor.actor,
                        },
                    )
                except source_intake.BoundSourceGrantExpired as error:
                    if HistoricalSelection.read(self.path) != selection:
                        raise IntakeError("intake_unavailable") from error
                    raise HistoricalSourceExpired("intake_expired") from error
                if HistoricalSelection.read(self.path) != selection:
                    raise IntakeError("intake_unavailable")
                token = self._scope.set(
                    AuthorizedSource(
                        selection.workflow_ref,
                        selection.engagement_epoch,
                        "evaluation",
                        require_ref(grant["delegation_id"]),
                        selection.mandate_revision,
                    )
                )
                try:
                    yield
                finally:
                    self._scope.reset(token)
        except MandateError as error:
            code = (
                "intake_expired"
                if str(error) == "standing_mandate_expired"
                else "intake_unavailable"
            )
            raise IntakeError(code) from error
        except (OSError, sqlite3.Error, TrainingError) as error:
            raise IntakeError("intake_unavailable") from error

    def discover(self, actor: IntakePrincipal, *, limit: int, cursor: str | None) -> CatalogPage:
        if cursor is not None or limit < 1:
            raise IntakeError("invalid_arguments")
        scope = self._scope.get()
        if actor != self.principal or scope is None:
            raise IntakeError("intake_unavailable")
        return CatalogPage(
            (
                CatalogSource(
                    "historical",
                    self.selected.pair_ref,
                    self.selected.source_revision,
                ),
            )
        )

    def historical(
        self, actor: IntakePrincipal, request: HistoricalPair
    ) -> AuthorizedSource | None:
        selection = self.selected
        if (
            actor == self.principal
            and request.pair_ref == selection.pair_ref
            and request.source_revision == selection.source_revision
            and request.correction_revision == selection.correction_revision
            and request.policy_revision == selection.policy_revision
        ):
            return self._scope.get()
        return None


@dataclass(frozen=True)
class StarterSelection:
    tenant: str
    actor: str
    engagement_epoch: str
    owner_id: str
    grant_ref: str

    @classmethod
    def read(cls, path: Path) -> StarterSelection:
        data = _selected_json(path)
        if (
            set(data) != {"schema_version", *cls.__dataclass_fields__}
            or data["schema_version"] != STARTER_SELECTION_VERSION
        ):
            raise IntakeError("private_intake_selection_required")
        values = {field: require_ref(data[field]) for field in cls.__dataclass_fields__}
        if re.fullmatch("[0-9a-f]{32}", values["engagement_epoch"]) is None:
            raise IntakeError("private_intake_selection_required")
        return cls(**values)


@dataclass(frozen=True)
class VerifiedStarterGrant:
    policy_resource_ref: str
    source_revision: str
    setup_policy_revision: str
    creation_ref: str
    revision: str


class LocalStarterAuthority(_LocalAuthority):
    """Consume a sealed setup grant from the owner-local authority producer."""

    def __init__(self, path: Path):
        self.path = path
        self.selected = StarterSelection.read(path)
        selection = self.selected
        self.principal = IntakePrincipal(selection.tenant, selection.actor, "intake")
        self.store = LocalTrainingStore(
            path.parent / "training.sqlite3", selection.tenant, existing_only=True
        )
        self.mandates = LearningMandates(path.parent, selection.tenant, selection.engagement_epoch)
        self._grant: ContextVar[VerifiedStarterGrant | None] = ContextVar(
            "intake_starter_grant", default=None
        )
        self._pinned = self._read_grant()

    def _read_grant(self) -> VerifiedStarterGrant:
        wanted = self.selected
        directory = self.path.parent
        key_record = _read_private(directory / "starter-intake-key.json", MAX_SELECTION_BYTES)
        key = key_record.get("key")
        record = _read_private(
            directory / f"starter-intake-grant-{wanted.grant_ref}.json", MAX_SELECTION_BYTES
        )
        body, seal = record.get("grant"), record.get("seal_sha256")
        if (
            set(key_record) != {"key"}
            or not isinstance(key, str)
            or re.fullmatch("[0-9a-f]{64}", key) is None
            or set(record) != {"grant", "seal_sha256"}
            or not isinstance(body, dict)
            or not isinstance(seal, str)
            or re.fullmatch("[0-9a-f]{64}", seal) is None
            or set(body)
            != {
                "schema_version",
                "purpose",
                "tenant",
                "actor",
                "owner_id",
                "custodian_id",
                "engagement_epoch",
                "grant_ref",
                "policy_resource_ref",
                "source_revision",
                "setup_policy_revision",
                "creation_ref",
                "expires_at",
            }
            or body["schema_version"] != STARTER_GRANT_VERSION
            or body["purpose"] != STARTER_PURPOSE
            or any(body[field] != getattr(wanted, field) for field in wanted.__dataclass_fields__)
            or not hmac.compare_digest(
                hmac.digest(bytes.fromhex(key), canonical(body).encode(), "sha256").hex(), seal
            )
        ):
            raise IntakeError("intake_unavailable")
        if os.path.lexists(directory / f"starter-intake-withdrawn-{wanted.grant_ref}.json"):
            raise IntakeError("intake_unavailable")
        if type(body["expires_at"]) is not int:
            raise IntakeError("intake_unavailable")
        if body["expires_at"] <= time.time():
            raise IntakeError("intake_expired")
        require_ref(body["custodian_id"])
        return VerifiedStarterGrant(
            require_ref(body["policy_resource_ref"]),
            require_ref(body["source_revision"]),
            require_ref(body["setup_policy_revision"]),
            require_ref(body["creation_ref"]),
            seal,
        )

    @contextmanager
    def guard(self, actor: IntakePrincipal) -> Iterator[None]:
        selection = self.selected
        try:
            with (
                self.store.engagement_guard(engagement_id=selection.engagement_epoch),
                self.mandates.guard_registered_owner(selection.owner_id),
            ):
                if actor != self.principal or StarterSelection.read(self.path) != selection:
                    raise IntakeError("intake_unavailable")
                grant = self._read_grant()
                if grant != self._pinned:
                    raise IntakeError("intake_unavailable")
                token = self._grant.set(grant)
                try:
                    yield
                finally:
                    self._grant.reset(token)
        except (OSError, sqlite3.Error, ManifestError, MandateError, TrainingError) as error:
            raise IntakeError("intake_unavailable") from error

    def discover(self, actor: IntakePrincipal, *, limit: int, cursor: str | None) -> CatalogPage:
        if cursor is not None or limit < 1:
            raise IntakeError("invalid_arguments")
        grant = self._grant.get()
        if actor != self.principal or grant is None:
            raise IntakeError("intake_unavailable")
        return CatalogPage(
            (CatalogSource("policy", grant.policy_resource_ref, grant.source_revision),)
        )

    def starter(self, actor: IntakePrincipal, request: Starter) -> AuthorizedSource | None:
        grant = self._grant.get()
        if (
            actor != self.principal
            or grant is None
            or (request.policy_resource_ref, request.source_revision, request.setup_policy_revision)
            != (grant.policy_resource_ref, grant.source_revision, grant.setup_policy_revision)
            or (request.creation_ref is not None and request.creation_ref != grant.creation_ref)
        ):
            return None
        return AuthorizedSource(
            request.workflow_ref,
            self.selected.engagement_epoch,
            STARTER_PURPOSE,
            grant.policy_resource_ref,
            grant.revision,
            creation_ref=grant.creation_ref,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("selection", type=Path)
    args = parser.parse_args(argv)
    try:
        authority: LocalHistoricalAuthority | LocalStarterAuthority
        if _selected_json(args.selection).get("schema_version") == STARTER_SELECTION_VERSION:
            authority = LocalStarterAuthority(args.selection)
        else:
            authority = LocalHistoricalAuthority(args.selection)
        with authority.guard(authority.principal):
            admission = DurableIntake(args.selection.parent, authority)
        return serve_intake(authority.principal, admission, sys.stdin.buffer, sys.stdout.buffer)
    except HistoricalSourceExpired as error:
        print(str(error), file=sys.stderr)
        return 2
    except (IntakeError, ManifestError, MandateError, OSError, sqlite3.Error, TrainingError):
        print("private_intake_selection_unavailable", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
