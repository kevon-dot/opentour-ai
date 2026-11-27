"""Bind actual starter-owner actions and a separately sealed plan to a reviewed map."""

from __future__ import annotations

import hmac
import json
import os
import re
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from opentour_cell.pilot.guided_policy import GuidedPolicySource
from opentour_cell.pilot.intake_admission import DurableIntake, IntakeWork
from opentour_cell.pilot.intake_local import (
    LocalStarterAuthority,
    StarterSelection,
    VerifiedStarterGrant,
)
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.map_review import MapReviewService, MapReviewSnapshot, open_map_review
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _open_db
from opentour_cell.pilot.repair_workflow import _record
from opentour_cell.pilot.starter_cases import (
    ConfirmedBranch,
    ConfirmedStarterMap,
    FrozenPlan,
    FrozenStarter,
    ProposedCase,
    StarterCases,
)
from opentour_cell.pilot.starter_setup import StarterSetup
from opentour_cell.pilot.workflow_benchmarks import MaterializedBenchmark

if TYPE_CHECKING:
    from opentour_cell.pilot.slack_adapter import Settings

Json = dict[str, Any]
SELECTION = "opentour.pilot.starter-case-selection.v1"
GRANT = "opentour.pilot.starter-case-grant.v1"
_OWNER_PURPOSES = {"propose", "review", "adjudicate", "freeze"}
_CAP_KEYS = {
    "schema_version",
    "grant_ref",
    "tenant",
    "engagement_epoch",
    "workflow_ref",
    "run_ref",
    "starter_grant_revision",
    "source_revision",
    "settings_revision",
    "source_digest",
    "map_confirmation_ref",
    "map_settings_ref",
    "owner_ref",
    "owner_actor",
    "custodian_ref",
    "expires_at",
    "owner_purposes",
    "worker_purposes",
    "frozen_plan",
    "critical_branches",
}


def _require(ok: object, reason: str = "starter_case_authority_unavailable") -> None:
    if not ok:
        raise IntakeError(reason)


class StarterProposalProvider(Protocol):
    def propose(self, snapshot: MapReviewSnapshot) -> tuple[ProposedCase, ...]:
        """Return suggestions only; no model output or owner-adjudicated truth is supplied."""
        ...


@dataclass(frozen=True)
class StarterCaseEntry:
    workflow_ref: str
    run_ref: str
    revision: str
    can_propose: bool
    can_adjudicate: bool
    can_freeze: bool


class _ReadOnlySetup(StarterSetup):
    @contextmanager
    def setup_grant(
        self, actor: IntakePrincipal, *, read_only: bool = False
    ) -> Iterator[VerifiedStarterGrant]:
        current = self._grant.get()
        if current is not None:
            # Reuse only this instance's already-held real source/owner transaction.
            # Both the intake and map readers need it; reacquiring the owner's
            # SQLite writer guard on another connection would deadlock.
            _require(
                actor == self.principal
                and self._read_grant() == current
                and time.time() < current.expires_at
                and StarterSelection.read(self.path) == self.selected
            )
            self._current_owner()
            yield current
            _require(self._read_grant() == current and time.time() < current.expires_at)
            self._current_owner()
            return
        with super().setup_grant(actor, read_only=read_only) as grant:
            yield grant

    @contextmanager
    def guard(self, actor: IntakePrincipal) -> Iterator[None]:
        with self.setup_grant(actor, read_only=True):
            yield


class _ExistingSetup(_ReadOnlySetup):
    """Original source binding for independently authorized metadata readers, without DDL."""

    def __init__(self, settings: Callable[[], Settings], owner_actor: str):
        self.settings, self.slack_actor = settings, owner_actor
        cfg = settings()
        binding = cfg.starter_setup
        _require(binding is not None and binding.slack_owner == owner_actor)
        assert binding is not None
        self.binding, self.installation = binding, cfg.binding
        cfg.permission(owner_actor, "inspect")
        LocalStarterAuthority.__init__(self, cfg.directory / binding.selection_file, read_only=True)
        self._current_owner()


@dataclass(frozen=True)
class CaseAuthorityMetadata:
    capability: Json
    envelope: Json
    binding: Json
    setup: StarterSetup
    record: Json


def case_authority_metadata(
    settings: Callable[[], Settings],
    actor: IntakePrincipal,
    authority_ref: str,
    *,
    authorize: Callable[[], None],
) -> CaseAuthorityMetadata:
    """Read an existing owner receipt after a separate trusted worker authorization.

    This does not resolve the caller as an owner or permit any owner action.
    The caller supplies its independently sealed evaluator/inspection boundary.
    Existing owner APIs and the frozen authority identity remain unchanged.
    """
    authorize()
    _require(re.fullmatch(r"starter-case-authority-[0-9a-f]{40}", authority_ref))
    session = StarterCaseSession(settings, actor)
    cap, envelope = session._grant()
    with (
        _open_db(session.directory / "training.sqlite3"),
        _open_db(session.directory / "learning-mandates.sqlite3"),
    ):
        setup = _ExistingSetup(settings, cap["owner_actor"])
    cfg, grant = settings(), setup._read_grant()
    _require(
        setup.selected.owner_id == cap["owner_ref"]
        and setup.selected.engagement_epoch == cap["engagement_epoch"]
        and grant.revision == cap["starter_grant_revision"]
        and grant.source_revision == cap["source_revision"]
        and grant.setup_policy_revision == cap["settings_revision"]
        and grant.custodian_id == cap["custodian_ref"]
        and cfg.starter_setup is not None
    )
    assert cfg.starter_setup is not None
    binding = {
        "installation": cfg.binding,
        "binding": asdict(cfg.starter_setup),
        "authenticated_owner": cap["owner_actor"],
        "registered_owner": cap["owner_ref"],
    }
    record = {
        "schema_version": "opentour.pilot.starter-case-authority.v1",
        "envelope": envelope,
        "owner_binding": binding,
        "map_confirmation_ref": cap["map_confirmation_ref"],
        "map_settings_ref": cap["map_settings_ref"],
        "source_digest": cap["source_digest"],
    }
    _require(authority_ref == "starter-case-authority-" + digest(record)[:40])
    _require(_read_private(cfg.directory / (authority_ref + ".json"), 65536) == record)
    authorize()
    return CaseAuthorityMetadata(cap, envelope, binding, setup, record)


@dataclass(frozen=True)
class _Pinned:
    cap: Json
    envelope: Json
    setup: StarterSetup
    map_service: MapReviewService
    snapshot: MapReviewSnapshot
    scope: ConfirmedStarterMap
    principal: IntakePrincipal
    authenticated_actor: IntakePrincipal
    binding: Json
    purpose: str


class _Confirmation:
    def __init__(self, session: StarterCaseSession):
        self.session = session

    @contextmanager
    def guard(
        self, actor: IntakePrincipal, work: IntakeWork, *, purpose: str
    ) -> Iterator[ConfirmedStarterMap]:
        pinned = self.session._active.get()
        _require(pinned is not None)
        assert pinned is not None
        _require(
            actor == pinned.principal and work == pinned.scope.work and purpose == pinned.purpose
        )
        self.session._current(pinned)
        yield pinned.scope
        self.session._current(pinned)


class _GuardedCases(StarterCases):
    def __init__(self, directory: Path, session: StarterCaseSession):
        super().__init__(directory, _Confirmation(session))
        self.session = session

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        with super()._db(write=write) as db:
            yield db
            if write:
                pinned = self.session._active.get()
                _require(pinned is not None)
                assert pinned is not None
                # The core commits after this yield returns. A revoked save rolls back.
                self.session._current(pinned)


class StarterCaseSession:
    """Private service entry, supplied the transport-authenticated actor separately."""

    def __init__(self, settings: Callable[[], Settings], actor: IntakePrincipal):
        self.settings, self.actor = settings, actor
        cfg = settings()
        self.directory = cfg.directory
        _require(cfg.organization == actor.tenant)
        self.selection_path = self.directory / "starter-case-selection.json"
        self.selection = _read_private(self.selection_path, 8192)
        _require(
            set(self.selection) == {"schema_version", "grant_file"}
            and self.selection["schema_version"] == SELECTION
            and isinstance(self.selection["grant_file"], str)
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json", self.selection["grant_file"])
        )
        self._active: ContextVar[_Pinned | None] = ContextVar(
            "starter_case_authority", default=None
        )
        self.core = _GuardedCases(self.directory, self)

    def _grant(self) -> tuple[Json, Json]:
        _require(_read_private(self.selection_path, 8192) == self.selection)
        envelope = _read_private(self.directory / self.selection["grant_file"], 32768)
        _require(set(envelope) == {"grant", "seal_sha256"})
        cap, seal = envelope["grant"], envelope["seal_sha256"]
        _require(isinstance(cap, dict) and set(cap) == _CAP_KEYS)
        _require(isinstance(seal, str) and re.fullmatch(r"[0-9a-f]{64}", seal))
        key = _read_private(self.directory / "starter-intake-key.json", 4096)["key"]
        _require(
            hmac.compare_digest(
                seal, hmac.digest(bytes.fromhex(key), canonical(cap).encode(), "sha256").hex()
            )
        )
        _require(cap["schema_version"] == GRANT and cap["tenant"] == self.actor.tenant)
        for field in _CAP_KEYS - {
            "expires_at",
            "owner_purposes",
            "worker_purposes",
            "frozen_plan",
            "critical_branches",
        }:
            require_ref(cap[field])
        _require(type(cap["expires_at"]) is int and int(time.time()) < cap["expires_at"] < 2**63)
        _require(
            not os.path.lexists(self.directory / f"starter-case-withdrawn-{cap['grant_ref']}.json")
        )
        _require(
            isinstance(cap["owner_purposes"], list)
            and set(cap["owner_purposes"]) <= _OWNER_PURPOSES
            and len(set(cap["owner_purposes"])) == len(cap["owner_purposes"])
        )
        _require(isinstance(cap["worker_purposes"], dict) and len(cap["worker_purposes"]) <= 32)
        for worker, purposes in cap["worker_purposes"].items():
            require_ref(worker)
            _require(purposes == ["materialize"])
        _require(
            isinstance(cap["critical_branches"], list)
            and len(cap["critical_branches"]) <= 128
            and len(set(cap["critical_branches"])) == len(cap["critical_branches"])
        )
        for branch in cap["critical_branches"]:
            require_ref(branch)
        _require(
            isinstance(cap["frozen_plan"], dict)
            and set(cap["frozen_plan"]) == set(FrozenPlan.__dataclass_fields__)
        )
        for field, value in cap["frozen_plan"].items():
            if field not in {"evaluator_ref", "evaluated_model_ref"} or value is not None:
                require_ref(value)
        return dict(cap), envelope

    def _binding(self, cap: Json) -> tuple[StarterSetup, Json]:
        cfg = self.settings()
        binding = cfg.starter_setup
        _require(
            binding is not None
            and cfg.directory == self.directory
            and cfg.organization == cap["tenant"]
        )
        assert binding is not None
        _require(
            binding.slack_owner == cap["owner_actor"] and binding.owner_ref == cap["owner_ref"]
        )
        setup = _ReadOnlySetup(self.settings, cap["owner_actor"])
        grant = setup._read_grant()
        _require(
            setup.selected.owner_id == cap["owner_ref"]
            and setup.selected.engagement_epoch == cap["engagement_epoch"]
            and grant.revision == cap["starter_grant_revision"]
            and grant.source_revision == cap["source_revision"]
            and grant.setup_policy_revision == cap["settings_revision"]
            and grant.custodian_id == cap["custodian_ref"]
        )
        return setup, {
            "installation": cfg.binding,
            "binding": asdict(binding),
            "authenticated_owner": cap["owner_actor"],
            "registered_owner": cap["owner_ref"],
        }

    def _permit(self, cap: Json, purpose: str) -> IntakePrincipal:
        if purpose in _OWNER_PURPOSES:
            _require(
                self.actor.actor == cap["owner_actor"] and purpose in cap["owner_purposes"],
                "starter_case_owner_required",
            )
            return IntakePrincipal(self.actor.tenant, cap["owner_ref"], "resolved-case-owner")
        _require(
            purpose == "materialize"
            and purpose in cap["worker_purposes"].get(self.actor.actor, []),
            "starter_case_worker_required",
        )
        return self.actor

    def _current(self, pinned: _Pinned) -> None:
        cap, envelope = self._grant()
        _require(cap == pinned.cap and envelope == pinned.envelope)
        _require(self._permit(cap, pinned.purpose) == pinned.principal)
        _, binding = self._binding(cap)
        _require(binding == pinned.binding and self.actor == pinned.authenticated_actor)
        with pinned.map_service.disclosure(
            self.actor, expected_revision=pinned.snapshot.review_revision
        ) as current:
            _require(
                current.confirmed_ref == cap["map_confirmation_ref"]
                and current.source_digest == cap["source_digest"]
            )

    def entry(self) -> StarterCaseEntry | None:
        """Private navigation metadata; no policy bytes or case/target payload is read."""
        try:
            cap, _ = self._grant()
            self._permit(cap, "review")
            setup, binding = self._binding(cap)
            service = open_map_review(self.directory, self.actor.tenant, cap["workflow_ref"])
            _require(
                service.authority.path == setup.path
                and service.authority.selected == setup.selected
            )
            service.authority = setup
            service.source = GuidedPolicySource(setup, service.source.path)
            intake = DurableIntake(self.directory, setup, existing_only=True)
            with (
                _open_db(self.directory / "training.sqlite3"),
                intake.admitted_work(setup.principal, cap["run_ref"]) as work,
            ):
                _require(
                    work.mode == "starter"
                    and work.benchmark_current
                    and work.workflow_ref == cap["workflow_ref"]
                    and work.revisions == (cap["source_revision"], cap["settings_revision"])
                )
                entry = service.entry(self.actor)
                _require(
                    entry is not None
                    and self._grant()[0] == cap
                    and self._binding(cap)[1] == binding
                )
                return StarterCaseEntry(
                    work.workflow_ref,
                    work.run_ref,
                    digest([cap, binding, self.actor.actor, entry]),
                    "propose" in cap["owner_purposes"],
                    "adjudicate" in cap["owner_purposes"],
                    "freeze" in cap["owner_purposes"],
                )
        except (ValueError, OSError, sqlite3.Error, TypeError, KeyError):
            return None

    @contextmanager
    def _pin(self, purpose: str) -> Iterator[_Pinned]:
        cap, envelope = self._grant()
        principal = self._permit(cap, purpose)
        setup, binding = self._binding(cap)
        service = open_map_review(self.directory, self.actor.tenant, cap["workflow_ref"])
        _require(
            service.authority.path == setup.path and service.authority.selected == setup.selected
        )
        service.authority = setup
        service.source = GuidedPolicySource(setup, service.source.path)
        snapshot = service.preview(self.actor)
        _require(
            snapshot.confirmed_ref == cap["map_confirmation_ref"]
            and snapshot.source_revision == cap["source_revision"]
            and snapshot.source_digest == cap["source_digest"]
            and snapshot.settings_ref == cap["map_settings_ref"]
            and snapshot.settings_trigger is not None
            and snapshot.settings_support == "owner_assertion",
            "confirmed_starter_map_unavailable",
        )
        intake = DurableIntake(self.directory, setup, existing_only=True)
        with (
            _open_db(self.directory / "training.sqlite3"),
            intake.admitted_work(setup.principal, cap["run_ref"]) as work,
            service.disclosure(self.actor, expected_revision=snapshot.review_revision),
        ):
            _require(
                work.mode == "starter"
                and work.benchmark_current
                and work.workflow_ref == cap["workflow_ref"]
                and work.resource.engagement_epoch == cap["engagement_epoch"]
                and work.revisions == (cap["source_revision"], cap["settings_revision"])
            )
            plan = FrozenPlan(**cap["frozen_plan"])
            branches = tuple(
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
            )
            _require(branches and set(cap["critical_branches"]) <= {b.branch_id for b in branches})
            authority = {
                "schema_version": "opentour.pilot.starter-case-authority.v1",
                "envelope": envelope,
                "owner_binding": binding,
                "map_confirmation_ref": snapshot.confirmed_ref,
                "map_settings_ref": snapshot.settings_ref,
                "source_digest": snapshot.source_digest,
            }
            authority_ref = "starter-case-authority-" + digest(authority)[:40]
            scope = ConfirmedStarterMap(
                self.actor.tenant,
                cap["owner_ref"],
                work,
                snapshot.map_revision,
                authority_ref,
                snapshot.source_digest,
                cap["source_revision"],
                cap["settings_revision"],
                branches,
                plan,
            )
            pinned = _Pinned(
                cap,
                envelope,
                setup,
                service,
                snapshot,
                scope,
                principal,
                self.actor,
                binding,
                purpose,
            )
            self._current(pinned)
            if purpose in {"propose", "adjudicate", "freeze"}:
                _record(self.directory / (authority_ref + ".json"), authority)
            else:
                _require(
                    _read_private(self.directory / (authority_ref + ".json"), 65536) == authority
                )
            token = self._active.set(pinned)
            try:
                yield pinned
                self._current(pinned)
            finally:
                self._active.reset(token)

    def propose(self, provider: StarterProposalProvider) -> str:
        with self._pin("propose") as pinned:
            # The current source-owner guard serializes this admission. Completed
            # proposals, including frozen ones, do not call an authoring provider again.
            if self.core.path.exists():
                with self.core._db() as db:
                    ref = self.core._ref(pinned.scope)
                    if db.execute("SELECT 1 FROM packs WHERE ref=?", (ref,)).fetchone():
                        self.core._load(db, pinned.scope)
                        return ref
                    for row in db.execute("SELECT scope FROM packs"):
                        previous = json.loads(row[0])
                        _require(
                            previous["tenant"] != pinned.scope.tenant
                            or previous["work"]["run_ref"] != pinned.scope.work.run_ref,
                            "starter_benchmark_successor_required",
                        )
            cases = provider.propose(pinned.snapshot)
            self._current(pinned)
            _require(all(case.case_origin == "generated_starter" for case in cases))
            return self.core.propose(pinned.principal, pinned.scope.work, cases)

    def review(self) -> Json:
        with self._pin("review") as pinned:
            body = self.core.review(pinned.principal, pinned.scope.work)
            authority = {
                "workflow_ref": pinned.scope.work.workflow_ref,
                "run_ref": pinned.scope.work.run_ref,
                "revision": digest([pinned.cap, pinned.binding, self.actor.actor]),
                "map_confirmation_ref": pinned.snapshot.confirmed_ref,
                "map_review_revision": pinned.snapshot.review_revision,
                "map_settings_ref": pinned.snapshot.settings_ref,
                "allowed_actions": list(pinned.snapshot.actions),
                "branches": [asdict(branch) for branch in pinned.scope.branches],
                "can_propose": "propose" in pinned.cap["owner_purposes"],
                "can_adjudicate": "adjudicate" in pinned.cap["owner_purposes"],
                "can_freeze": "freeze" in pinned.cap["owner_purposes"],
            }
            return body | {"authority": authority}

    def adjudicate(
        self,
        *,
        case_id: str,
        target: str | None,
        reason: str,
        expected_revision: int,
        operation_ref: str,
    ) -> int:
        _require(type(expected_revision) is int and expected_revision >= 0)
        with self._pin("adjudicate") as pinned:
            return self.core.adjudicate(
                pinned.principal,
                pinned.scope.work,
                case_id=case_id,
                target=target,
                reason=reason,
                expected_revision=expected_revision,
                operation_ref=operation_ref,
            )

    def add_counterexample(
        self, *, case: ProposedCase, expected_revision: int, operation_ref: str
    ) -> int:
        with self._pin("adjudicate") as pinned:
            return self.core.add_counterexample(
                pinned.principal,
                pinned.scope.work,
                case=case,
                expected_revision=expected_revision,
                operation_ref=operation_ref,
            )

    def freeze(self, *, splits: dict[str, str], expected_revision: int) -> FrozenStarter:
        _require(type(expected_revision) is int and expected_revision >= 0)
        with self._pin("freeze") as pinned:
            return self.core.freeze(
                pinned.principal,
                pinned.scope.work,
                splits=splits,
                plan=pinned.scope.frozen_plan,
                expected_revision=expected_revision,
            )

    def materialize(
        self, actor: IntakePrincipal, work: IntakeWork, *, operation_ref: str
    ) -> MaterializedBenchmark | None:
        _require(actor == self.actor)
        with self._pin("materialize") as pinned:
            _require(work == pinned.scope.work)
            return self.core.materialize(actor, work, operation_ref=operation_ref)

    def readback(
        self, actor: IntakePrincipal, work: IntakeWork, *, operation_ref: str
    ) -> MaterializedBenchmark | None:
        return self.materialize(actor, work, operation_ref=operation_ref)
