"""Separate, sealed execution and retained-inspection rights for existing starter freezes."""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentour_cell.pilot.guided_decision_map import Condition, FieldKind, guided_draft
from opentour_cell.pilot.guided_policy import _read_policy
from opentour_cell.pilot.intake_admission import DurableIntake, IntakeWork
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.map_review import (
    MapReviewService,
    MapReviewSnapshot,
    _Context,
    open_map_review,
)
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.starter_cases import (
    ConfirmedBranch,
    ConfirmedStarterMap,
    FrozenEvaluationInputs,
    FrozenPlan,
    FrozenStarter,
    StarterCases,
    _scope_record,
)
from opentour_cell.pilot.starter_map_confirmation import (
    CaseAuthorityMetadata,
    case_authority_metadata,
)
from opentour_cell.pilot.starter_score_inspection import InspectionScope
from opentour_cell.pilot.starter_scoring import EvaluationPermission

if TYPE_CHECKING:
    from opentour_cell.pilot.slack_adapter import Settings

Json = dict[str, Any]
SELECTION = "opentour.pilot.starter-evaluation-selection.v1"
EXECUTION = "opentour.pilot.starter-execution-grant.v1"
INSPECTION = "opentour.pilot.starter-inspection-grant.v1"
_GRANT_KEYS = {
    "schema_version",
    "grant_ref",
    "tenant",
    "custodian_ref",
    "case_authority_ref",
    "case_envelope_digest",
    "permission",
    "expires_at",
}


def _require(value: object, reason: str = "starter_evaluation_authority_unavailable") -> None:
    if not value:
        raise IntakeError(reason)


@dataclass(frozen=True)
class ComparatorBranch:
    branch_ref: str
    conditions: tuple[Condition, ...]
    action: str
    precedence: int | None


@dataclass(frozen=True)
class ComparatorContext:
    comparator_ref: str
    map_revision: str
    confirmed_map_ref: str
    settings_ref: str
    source_revision: str
    source_digest: str
    field_kinds: tuple[tuple[str, str], ...]
    branches: tuple[ComparatorBranch, ...]
    fallback_action: str | None

    @property
    def comparator_revision(self) -> str:
        return "starter-comparator-" + digest(
            {"schema_version": "opentour.pilot.starter-comparator-context.v1", **asdict(self)}
        )


def _context(snapshot: MapReviewSnapshot, plan: FrozenPlan) -> ComparatorContext:
    _require(snapshot.confirmed_ref and snapshot.settings_ref)
    _require(getattr(snapshot, "effective_from", None) is None, "decision_date_context_required")
    active = tuple(row for row in snapshot.branches if row.disposition != "out_of_scope")
    _require(active and all(row.confirmed and row.action is not None for row in active))
    assert snapshot.confirmed_ref and snapshot.settings_ref
    return ComparatorContext(
        plan.comparator_ref,
        snapshot.map_revision,
        snapshot.confirmed_ref,
        snapshot.settings_ref,
        snapshot.source_revision,
        snapshot.source_digest,
        tuple(sorted(snapshot.field_kinds.items())),
        tuple(
            ComparatorBranch(r.branch_ref, r.conditions, r.action, r.precedence)
            for r in active
            if r.action is not None
        ),
        snapshot.fallback_action,
    )


@dataclass(frozen=True)
class _Pinned:
    grant: Json
    permission: EvaluationPermission
    metadata: CaseAuthorityMetadata
    scope: ConfirmedStarterMap
    context: ComparatorContext


def _frozen_body(directory: Path, pinned: _Pinned) -> tuple[Json, FrozenStarter]:
    """Reconcile the signed freeze identity with actual rows, not journal claims."""
    core = StarterCases(directory)
    with core._db() as db:
        body = core._load(db, pinned.scope)
        core._validate_prior_freezes(db, pinned.scope)
        _require(body["frozen"] is not None)
        frozen = core._frozen(body["frozen"])
        rows = body["cases"]
        splits = {row["family_id"]: row["split_ref"] for row in rows}
        _require(all(splits[row["family_id"]] == row["split_ref"] for row in rows))
        revision = digest(
            [_scope_record(pinned.scope), rows, asdict(pinned.scope.frozen_plan), splits]
        )
        _require(
            body["plan"] == asdict(pinned.scope.frozen_plan)
            and revision == frozen.revision == pinned.permission.freeze_revision
            and frozen.reference
            == "starter-freeze-" + revision[:40]
            == pinned.permission.freeze_ref
        )
        _require(
            frozen.unresolved == sum(row["status"] == "unresolved" for row in rows)
            and frozen.excluded == sum(row["split_ref"] == "excluded" for row in rows)
        )
        reserved = [
            row for row in rows if row["split_ref"] == "reserved" and row["status"] == "resolved"
        ]
        _require(len(reserved) <= pinned.permission.maximum_cases)
        _require(
            {key for row in reserved for key, _ in row["decision_inputs"]}
            <= set(pinned.permission.input_fields)
        )
        return body, frozen


class _Authority:
    kind: str

    def __init__(self, settings: Callable[[], Settings], actor: IntakePrincipal):
        self.settings, self.actor = settings, actor
        cfg = settings()
        self.directory = cfg.directory
        _require(cfg.organization == actor.tenant)
        self.path = self.directory / "starter-evaluation-selection.json"
        self.selection = _read_private(self.path, 16384)
        _require(set(self.selection) == {"schema_version", "executions", "inspections"})
        _require(self.selection["schema_version"] == SELECTION)
        for kind in ("executions", "inspections"):
            entries = self.selection[kind]
            _require(isinstance(entries, dict) and len(entries) <= 64)
            for execution, filename in entries.items():
                require_ref(execution)
                _require(
                    isinstance(filename, str)
                    and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json", filename)
                )
        self._active: ContextVar[_Pinned | None] = ContextVar(
            "starter_evaluation_pin", default=None
        )

    def _sealed(self, filename: str) -> Json:
        envelope = _read_private(self.directory / filename, 65536)
        _require(set(envelope) == {"grant", "seal_sha256"})
        cap, seal = envelope["grant"], envelope["seal_sha256"]
        _require(isinstance(cap, dict) and isinstance(seal, str))
        key = _read_private(self.directory / "starter-intake-key.json", 4096)["key"]
        _require(isinstance(key, str) and re.fullmatch(r"[0-9a-f]{64}", key))
        _require(
            hmac.compare_digest(
                seal, hmac.digest(bytes.fromhex(key), canonical(cap).encode(), "sha256").hex()
            )
        )
        return dict(cap)

    def _grant(self, execution_ref: str) -> tuple[Json, EvaluationPermission]:
        require_ref(execution_ref)
        cfg = self.settings()
        _require(cfg.directory == self.directory and cfg.organization == self.actor.tenant)
        _require(_read_private(self.path, 16384) == self.selection)
        selected = "executions" if self.kind == EXECUTION else "inspections"
        filename = self.selection[selected].get(execution_ref)
        _require(filename is not None, "starter_execution_not_selected")
        grant = self._sealed(filename)
        expected = _GRANT_KEYS | (
            {"viewer_actor", "owner_ref"} if self.kind == INSPECTION else set()
        )
        _require(set(grant) == expected and grant["schema_version"] == self.kind)
        for field in expected - {"permission", "expires_at"}:
            require_ref(grant[field])
        _require(grant["tenant"] == self.actor.tenant)
        _require(type(grant["expires_at"]) is int and time.time() < grant["expires_at"] < 2**63)
        _require(
            not os.path.lexists(
                self.directory / f"starter-evaluation-withdrawn-{grant['grant_ref']}.json"
            )
        )
        value = grant["permission"]
        _require(
            isinstance(value, dict) and set(value) == set(EvaluationPermission.__dataclass_fields__)
        )
        fields = value["input_fields"]
        _require(
            isinstance(fields, list) and 0 < len(fields) <= 32 and len(set(fields)) == len(fields)
        )
        _require(
            all(isinstance(f, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", f) for f in fields)
        )
        for field, item in value.items():
            if field not in {"input_fields", "maximum_cases", "expires_at"}:
                require_ref(item)
        _require(type(value["maximum_cases"]) is int and 0 < value["maximum_cases"] <= 1000)
        _require(type(value["expires_at"]) is int and 0 < value["expires_at"] < 2**63)
        permission = EvaluationPermission(**(value | {"input_fields": tuple(fields)}))
        _require(
            permission.tenant == self.actor.tenant and permission.execution_ref == execution_ref
        )
        if self.kind == EXECUTION:
            _require(
                permission.evaluator == self.actor.actor and time.time() < permission.expires_at
            )
            _require(permission.revision == grant["grant_ref"])
        else:
            _require(grant["viewer_actor"] == self.actor.actor)
        return grant, permission

    def _authorize(self, grant: Json, permission: EvaluationPermission) -> None:
        _require(self._grant(permission.execution_ref) == (grant, permission))

    def _metadata(self, grant: Json, permission: EvaluationPermission) -> CaseAuthorityMetadata:
        def authorize() -> None:
            self._authorize(grant, permission)

        metadata = case_authority_metadata(
            self.settings, self.actor, grant["case_authority_ref"], authorize=authorize
        )
        cap = metadata.capability
        _require(digest(metadata.envelope) == grant["case_envelope_digest"])
        _require(grant["custodian_ref"] == cap["custodian_ref"])
        _require(
            (permission.workflow_ref, permission.run_ref, permission.epoch)
            == (cap["workflow_ref"], cap["run_ref"], cap["engagement_epoch"])
        )
        if self.kind == INSPECTION:
            _require(
                (grant["viewer_actor"], grant["owner_ref"])
                == (cap["owner_actor"], cap["owner_ref"])
            )
        # These identities are already available from the sealed original case
        # envelope. Reject a rebound evaluator/plan before opening policy bytes.
        plan = FrozenPlan(**cap["frozen_plan"])
        _require(
            plan.evaluator_ref == permission.evaluator
            and digest(asdict(plan)) == permission.plan_digest
            and plan.baseline_ref in {"baseline-pending", permission.model_ref}
            and plan.evaluated_model_ref in {None, permission.model_ref}
            and re.fullmatch(r"[0-9a-f]{64}", permission.freeze_revision)
            and permission.freeze_ref == "starter-freeze-" + permission.freeze_revision[:40]
        )
        return metadata

    def _map_capability(self, service: MapReviewService, metadata: CaseAuthorityMetadata) -> Json:
        cap = metadata.capability
        selection_file = getattr(service, "selection_file", "map-review-selection.json")
        _require(_read_private(self.directory / selection_file, 8192) == service.selection)
        map_cap = self._sealed(service.selection["grant_file"])
        _require(type(map_cap["expires_at"]) is int and time.time() < map_cap["expires_at"])
        _require(
            not os.path.lexists(
                self.directory / f"map-review-withdrawn-{map_cap['grant_ref']}.json"
            )
        )
        _require(
            (
                map_cap["tenant"],
                map_cap["workflow_ref"],
                map_cap["engagement_epoch"],
                map_cap["starter_grant_revision"],
                map_cap["source_revision"],
                map_cap["owner_ref"],
                map_cap["custodian_ref"],
            )
            == (
                cap["tenant"],
                cap["workflow_ref"],
                cap["engagement_epoch"],
                cap["starter_grant_revision"],
                cap["source_revision"],
                cap["owner_ref"],
                cap["custodian_ref"],
            )
        )
        _require(map_cap["schema_version"] == "opentour.pilot.map-review-grant.v1")
        _require(map_cap["policy_resource_ref"] == metadata.setup._read_grant().policy_resource_ref)
        return map_cap

    @contextmanager
    def _pin(self, execution_ref: str) -> Iterator[_Pinned]:
        grant, permission = self._grant(execution_ref)

        def authorize() -> None:
            self._authorize(grant, permission)

        metadata = self._metadata(grant, permission)
        active = self._active.get()
        if active is not None:
            _require(active.grant == grant and active.permission == permission)
            _require(active.metadata.record == metadata.record)
            # Re-enter only this authority's already-held original-source guard.
            # The registered-owner writer cannot be acquired on a second connection.
            metadata = replace(metadata, setup=active.metadata.setup)
        cap, setup = metadata.capability, metadata.setup
        intake = DurableIntake(self.directory, setup, existing_only=True)
        with (
            _open_db(self.directory / "training.sqlite3"),
            _open_db(self.directory / "learning-mandates.sqlite3"),
            intake.admitted_work(setup.principal, cap["run_ref"]) as work,
        ):
            _require(work.mode == "starter" and work.benchmark_current)
            _require(
                work.workflow_ref == cap["workflow_ref"]
                and work.resource.engagement_epoch == cap["engagement_epoch"]
            )
            _require(work.revisions == (cap["source_revision"], cap["settings_revision"]))
            authorize()
            service = open_map_review(self.directory, self.actor.tenant, work.workflow_ref)
            _require(
                service.authority.path == setup.path
                and service.authority.selected == setup.selected
            )
            map_cap = self._map_capability(service, metadata)
            with service._db() as db:
                records = service._records(db)
            _require(records and records[-1]["ref"] == cap["map_confirmation_ref"])
            record = records[-1]
            _require(record["capability_revision"] == digest(map_cap))
            _require(set(permission.input_fields) <= set(map_cap["field_kinds"]))
            authorize()
            _require(self._metadata(grant, permission).record == metadata.record)
            _require(self._map_capability(service, metadata) == map_cap)
            source_grant = setup._read_grant()
            policy = _read_policy(service.source.path, source_grant)
            draft = guided_draft(
                policy,
                {k: FieldKind(v) for k, v in map_cap["field_kinds"].items()},
                frozenset(map_cap["actions"]),
            )
            snapshot = service._snapshot(
                self.actor, _Context(map_cap, policy, draft, False), record
            )
            _require(
                snapshot.confirmed_ref == cap["map_confirmation_ref"]
                and snapshot.source_digest == cap["source_digest"]
                and snapshot.settings_ref == cap["map_settings_ref"]
                and snapshot.settings_trigger is not None
            )
            plan = FrozenPlan(**cap["frozen_plan"])
            context = _context(snapshot, plan)
            _require(
                plan.evaluator_ref == permission.evaluator
                and digest(asdict(plan)) == permission.plan_digest
                and plan.baseline_ref in {"baseline-pending", permission.model_ref}
                and plan.evaluated_model_ref in {None, permission.model_ref}
            )
            _require(context.comparator_revision == permission.comparator_revision)
            _require(set(permission.input_fields) <= set(snapshot.field_kinds))
            scope = ConfirmedStarterMap(
                self.actor.tenant,
                cap["owner_ref"],
                work,
                snapshot.map_revision,
                grant["case_authority_ref"],
                snapshot.source_digest,
                cap["source_revision"],
                cap["settings_revision"],
                tuple(
                    ConfirmedBranch(
                        row.branch_ref,
                        tuple(sorted(snapshot.field_kinds)),
                        snapshot.actions,
                        (row.citation_ref,),
                        plan.scorer_ref,
                        row.branch_ref in cap["critical_branches"],
                        coverage_actions=(row.action,),
                    )
                    for row in snapshot.branches
                    if row.confirmed and row.action is not None
                ),
                plan,
            )
            _require(permission.freeze_ref == "starter-freeze-" + permission.freeze_revision[:40])
            pinned = _Pinned(grant, permission, metadata, scope, context)
            authorize()
            _require(
                setup._read_grant() == source_grant
                and _read_policy(service.source.path, source_grant) == policy
            )
            token = self._active.set(pinned)
            try:
                yield pinned
                authorize()
            finally:
                self._active.reset(token)

    def _current(self, pinned: _Pinned) -> None:
        with self._pin(pinned.permission.execution_ref) as current:
            _require(
                current.grant == pinned.grant
                and current.permission == pinned.permission
                and current.metadata.record == pinned.metadata.record
                and current.scope == pinned.scope
                and current.context == pinned.context
            )


class _EvaluationCases:
    def __init__(self, authority: StarterEvaluationAuthority):
        self.authority = authority

    @contextmanager
    def guard(
        self, actor: IntakePrincipal, work: IntakeWork, *, purpose: str
    ) -> Iterator[ConfirmedStarterMap]:
        pinned = self.authority._active.get()
        _require(pinned is not None and purpose == "evaluate")
        assert pinned is not None
        self.authority.revalidate(actor, work, pinned.permission)
        _frozen_body(self.authority.directory, pinned)
        yield pinned.scope
        _frozen_body(self.authority.directory, pinned)
        self.authority.revalidate(actor, work, pinned.permission)


class StarterEvaluationAuthority(_Authority):
    kind = EXECUTION

    def __init__(self, settings: Callable[[], Settings], actor: IntakePrincipal):
        super().__init__(settings, actor)
        self.cases = StarterCases(self.directory, _EvaluationCases(self))

    def work(self, execution_ref: str) -> IntakeWork:
        with self._pin(execution_ref) as pinned:
            self._current(pinned)
            return pinned.scope.work

    def revalidate(
        self, actor: IntakePrincipal, work: IntakeWork, permission: EvaluationPermission
    ) -> None:
        pinned = self._active.get()
        _require(pinned is not None and actor == self.actor)
        assert pinned is not None
        _require(work == pinned.scope.work and permission == pinned.permission)
        self._current(pinned)

    @contextmanager
    def guard(
        self, actor: IntakePrincipal, work: IntakeWork, execution_ref: str
    ) -> Iterator[EvaluationPermission]:
        _require(actor == self.actor)
        with self._pin(execution_ref) as pinned:
            _require(work == pinned.scope.work)
            frozen = self.cases.evaluation_inputs(actor, work)
            self._frozen(pinned.permission, frozen)
            yield pinned.permission
            self.revalidate(actor, work, pinned.permission)

    @staticmethod
    def _frozen(permission: EvaluationPermission, frozen: FrozenEvaluationInputs) -> None:
        _require(
            frozen.freeze_ref == permission.freeze_ref
            and frozen.freeze_revision == permission.freeze_revision
            and digest(asdict(frozen.plan)) == permission.plan_digest
            and len(frozen.cases) <= permission.maximum_cases
        )
        _require(
            {k for case in frozen.cases for k, _ in case.decision_inputs}
            <= set(permission.input_fields)
        )

    def comparator_context(
        self, actor: IntakePrincipal, work: IntakeWork, permission: EvaluationPermission
    ) -> ComparatorContext:
        self.revalidate(actor, work, permission)
        pinned = self._active.get()
        assert pinned is not None
        return pinned.context


class StarterInspectionAuthority(_Authority):
    kind = INSPECTION

    def entry(self, execution_ref: str) -> Json | None:
        """Metadata navigation only: no policy/case bodies, score journal or writes."""
        try:
            grant, permission = self._grant(execution_ref)
            metadata = self._metadata(grant, permission)
            cap, setup = metadata.capability, metadata.setup
            intake = DurableIntake(self.directory, setup, existing_only=True)
            with (
                _open_db(self.directory / "training.sqlite3"),
                _open_db(self.directory / "learning-mandates.sqlite3"),
                intake.admitted_work(setup.principal, cap["run_ref"]) as work,
            ):
                _require(work.benchmark_current and work.workflow_ref == permission.workflow_ref)
                service = open_map_review(self.directory, self.actor.tenant, work.workflow_ref)
                _require(
                    service.authority.selected == setup.selected
                    and service.authority.path == setup.path
                )
                map_cap = self._map_capability(service, metadata)
                with service._db() as db:
                    records = service._records(db)
                _require(
                    records
                    and records[-1]["ref"] == cap["map_confirmation_ref"]
                    and records[-1]["capability_revision"] == digest(map_cap)
                )
                _require(self._metadata(grant, permission).record == metadata.record)
                _require(self._map_capability(service, metadata) == map_cap)
                return {
                    "workflow_ref": work.workflow_ref,
                    "execution_ref": execution_ref,
                    "revision": digest(grant),
                }
        except (ValueError, OSError, sqlite3.Error, TypeError, KeyError):
            return None

    def _inspection(self, pinned: _Pinned) -> InspectionScope:
        p = pinned.permission
        body, frozen = _frozen_body(self.directory, pinned)
        rows = [
            row
            for row in body["cases"]
            if row["split_ref"] == "reserved" and row["status"] == "resolved"
        ]
        _require(len(rows) <= p.maximum_cases)
        inputs = {
            "starter-case-" + digest([p.freeze_revision, row["case_id"]])[:40]: dict(
                row["decision_inputs"]
            )
            for row in rows
        }
        _require({key for fields in inputs.values() for key in fields} <= set(p.input_fields))
        population = [
            dict(
                case_id=row["case_id"],
                family_id=row["family_id"],
                origin=row["case_origin"],
                expected=dict(
                    target=row["owner_adjudicated_target"],
                    adjudicator=row["adjudicator"],
                    adjudicated_at=row["adjudicated_at"],
                    evidence_ref=row["citation"],
                ),
            )
            for row in rows
        ]
        return InspectionScope(
            self.actor,
            digest(pinned.grant),
            pinned.grant["expires_at"],
            p,
            digest(inputs),
            pinned.context.comparator_ref,
            digest(population),
            len(rows),
            frozen.unresolved,
            frozen.excluded,
        )

    @contextmanager
    def guard(
        self, actor: IntakePrincipal, workflow_ref: str, execution_ref: str
    ) -> Iterator[InspectionScope]:
        _require(actor == self.actor)
        with self._pin(execution_ref) as pinned:
            _require(workflow_ref == pinned.scope.work.workflow_ref)
            scope = self._inspection(pinned)
            self.revalidate(actor, scope)
            yield scope
            self.revalidate(actor, scope)

    def revalidate(self, actor: IntakePrincipal, scope: InspectionScope) -> None:
        pinned = self._active.get()
        _require(pinned is not None and actor == self.actor == scope.viewer)
        assert pinned is not None
        self._current(pinned)
        _require(scope == self._inspection(pinned))
