"""Private starter charter review and idempotent draft admission from one authorized policy."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentour_cell.pilot.intake_admission import AuthorizedSource, DurableIntake
from opentour_cell.pilot.intake_local import (
    LocalStarterAuthority,
    StarterSelection,
    VerifiedStarterGrant,
)
from opentour_cell.pilot.intake_mcp import AdmissionReceipt, IntakeError, IntakePrincipal, Starter
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.repair_workflow import _record

if TYPE_CHECKING:
    from opentour_cell.pilot.slack_adapter import Settings


def _require(condition: object) -> None:
    if not condition:
        raise IntakeError("starter_setup_unavailable")


@dataclass(frozen=True)
class StarterBinding:
    slack_owner: str
    owner_ref: str
    selection_file: str
    charter_file: str

    @classmethod
    def parse(cls, value: object) -> StarterBinding:
        _require(isinstance(value, dict) and value.keys() == cls.__dataclass_fields__.keys())
        assert isinstance(value, dict)
        _require(all(isinstance(v, str) for v in value.values()))
        _require(re.fullmatch(r"[UW][A-Z0-9]{2,31}", value["slack_owner"]))
        _require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value["owner_ref"]))
        for key in ("selection_file", "charter_file"):
            _require(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,80}\.json", value[key]))
        return cls(**value)


@dataclass(frozen=True)
class StarterCharter:
    intended_decision: str
    local_purpose: str
    verification_plan: str
    responsible_person: str
    next_check: str
    verifier: str | None

    @classmethod
    def read(cls, path: Path, revision: str) -> StarterCharter:
        raw = _read_private(path, 8192)
        _require(
            set(raw) == {"schema_version", *cls.__dataclass_fields__}
            and raw["schema_version"] == "opentour.pilot.starter-charter.v1"
            and digest(raw) == revision
        )
        fields = {key: raw[key] for key in cls.__dataclass_fields__}
        for field, value in fields.items():
            if field == "verifier" and value is None:
                continue
            _require(
                isinstance(value, str)
                and 0 < len(value) <= 160
                and value == value.strip()
                and value.isprintable()
            )
        _require(date.fromisoformat(fields["next_check"]).isoformat() == fields["next_check"])
        return cls(**fields)

    def require_current_plan(self) -> None:
        _require(date.fromisoformat(self.next_check) >= datetime.now(UTC).date())


@dataclass(frozen=True)
class StarterPreview:
    revision: str
    owner: str
    custodian: str
    policy: str
    policy_revision: str
    charter: StarterCharter
    draft: dict[str, Any] | None


class StarterSetup(LocalStarterAuthority):
    """Apply the installation's owner mapping at every admission and disclosure."""

    def __init__(self, settings: Callable[[], Settings], actor: str):
        self.settings = settings
        self.slack_actor = actor
        cfg = settings()
        binding = cfg.starter_setup
        _require(binding is not None and binding.slack_owner == actor)
        assert binding is not None
        self.binding = binding
        self.installation = cfg.binding
        cfg.permission(actor, "inspect")
        super().__init__(cfg.directory / self.binding.selection_file)
        self._current_owner()

    def _current_owner(self) -> None:
        cfg = self.settings()
        cfg.permission(self.slack_actor, "inspect")
        _require(
            cfg.binding == self.installation
            and cfg.starter_setup == self.binding
            and self.selected.tenant == cfg.organization
            and self.selected.owner_id == self.binding.owner_ref
        )

    def starter(self, actor: IntakePrincipal, request: Starter) -> AuthorizedSource | None:
        # DurableIntake resolves again after acquiring its own write transaction.
        self._current_owner()
        current = self._read_grant()
        _require(
            current == self._pinned
            and time.time() < current.expires_at
            and StarterSelection.read(self.path) == self.selected
        )
        charter = StarterCharter.read(
            self.path.parent / self.binding.charter_file, current.setup_policy_revision
        )
        charter.require_current_plan()
        return super().starter(actor, request)

    def _draft_path(self, creation: str) -> Path:
        identity = digest([self.selected.tenant, self.selected.engagement_epoch, creation])
        return self.path.parent / f"source-intake-starter-draft-{identity}.json"

    def _receipt(
        self, grant: VerifiedStarterGrant, charter: StarterCharter, workflow: str, run: str
    ) -> dict[str, Any]:
        _require(
            re.fullmatch(r"workflow-[0-9a-f]{32}", workflow)
            and re.fullmatch(r"run-[0-9a-f]{32}", run)
        )
        scope = digest([self.installation, self.slack_actor, grant.revision])
        return {
            "schema_version": "opentour.pilot.starter-draft.v1",
            "scope_revision": scope,
            "settings_revision": grant.setup_policy_revision,
            "workflow_ref": workflow,
            "run_ref": run,
            "draft_ref": "draft-" + digest([scope, workflow, run])[:32],
            "mandate": {
                "purpose": "starter_policy_draft",
                "owner_ref": self.selected.owner_id,
                "custodian_ref": grant.custodian_id,
                "policy_resource_ref": grant.policy_resource_ref,
                "source_revision": grant.source_revision,
                "charter": asdict(charter),
            },
        }

    def _check_admission(self, grant: VerifiedStarterGrant, draft: dict[str, Any]) -> None:
        # The separate draft may survive a partial restore of the intake register.
        # Opening a saved card must not initialize, migrate or repair that register.
        from opentour_cell.pilot.receipt_recovery import _open_db

        with _open_db(self.path.parent / "intake.sqlite3") as db:
            _require(db is not None)
            assert db is not None
            row = db.execute(
                "SELECT r.request FROM intake_runs r JOIN intake_workflows w "
                "ON r.tenant=w.tenant AND r.workflow_ref=w.workflow_ref "
                "WHERE r.tenant=? AND r.workflow_ref=? AND r.run_ref=? AND r.grant_revision=? "
                "AND w.creation_ref=? AND w.resource=? AND w.epoch=?",
                (
                    self.selected.tenant,
                    draft["workflow_ref"],
                    draft["run_ref"],
                    grant.revision,
                    grant.creation_ref,
                    grant.policy_resource_ref,
                    self.selected.engagement_epoch,
                ),
            ).fetchone()
            _require(
                row is not None
                and json.loads(row[0])
                == {
                    "mode": "starter",
                    "creation_ref": grant.creation_ref,
                    "policy_resource_ref": grant.policy_resource_ref,
                    "source_revision": grant.source_revision,
                    "setup_policy_revision": grant.setup_policy_revision,
                }
            )

    def preview(self) -> StarterPreview:
        with self.setup_grant(self.principal, read_only=True) as grant:
            self._current_owner()
            charter = StarterCharter.read(
                self.path.parent / self.binding.charter_file, grant.setup_policy_revision
            )
            revision = digest([self.installation, self.slack_actor, grant.revision])
            try:
                draft = _read_private(self._draft_path(grant.creation_ref), 8192)
            except FileNotFoundError:
                draft = None
            if draft is not None:
                self._check_admission(grant, draft)
                _require(
                    draft == self._receipt(grant, charter, draft["workflow_ref"], draft["run_ref"])
                )
            return StarterPreview(
                revision,
                self.selected.owner_id,
                grant.custodian_id,
                grant.policy_resource_ref,
                grant.source_revision,
                charter,
                draft,
            )

    def save(self, expected: str, operation_key: str) -> StarterPreview:
        preview = self.preview()
        _require(preview.revision == expected)
        if preview.draft:
            return preview
        preview.charter.require_current_plan()
        with self.setup_grant(self.principal) as grant:
            self._current_owner()
            admission = DurableIntake(self.path.parent, self)
            request = Starter(
                grant.policy_resource_ref,
                grant.source_revision,
                grant.setup_policy_revision,
                operation_key,
                grant.creation_ref,
                None,
            )
        receipt = admission.submit_intake(self.principal, request)
        _require(isinstance(receipt, AdmissionReceipt))
        assert isinstance(receipt, AdmissionReceipt)
        run = admission.get_run(self.principal, receipt.run_ref)
        with self.setup_grant(self.principal):
            self._current_owner()
            charter = StarterCharter.read(
                self.path.parent / self.binding.charter_file, grant.setup_policy_revision
            )
            charter.require_current_plan()
            _record(
                self._draft_path(grant.creation_ref),
                self._receipt(grant, preview.charter, run.workflow_ref, receipt.run_ref),
            )
        return self.preview()
