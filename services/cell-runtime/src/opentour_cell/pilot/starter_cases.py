"""Private, owner-adjudicated starter families; no default confirmation authority.

This core deliberately cannot turn a guided map draft into owner confirmation.
The installed OPE-538 producer must implement the pinned current-authority port.
No scoring, inference, prompt/training export, or live-evidence credit occurs here.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections.abc import Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot.intake_admission import IntakeWork, require_readonly_snapshot
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.training import _check_file
from opentour_cell.pilot.workflow_benchmarks import MaterializedBenchmark

Json = dict[str, Any]


def _require(ok: object, reason: str = "starter_cases_unavailable") -> None:
    if not ok:
        raise IntakeError(reason)


@dataclass(frozen=True)
class ConfirmedBranch:
    branch_id: str
    decision_fields: tuple[str, ...]
    allowed_actions: tuple[str, ...]
    citations: tuple[str, ...]
    scoring_rule: str
    critical: bool = False
    coverage_actions: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ConfirmedStarterMap:
    """Trusted producer result, never an authority claim accepted from a request."""

    tenant: str
    owner_id: str
    work: IntakeWork
    map_ref: str
    confirmation_ref: str
    source_digest: str
    policy_revision: str
    settings_revision: str
    branches: tuple[ConfirmedBranch, ...]
    frozen_plan: FrozenPlan


def _scope_record(scope: ConfirmedStarterMap) -> Json:
    """Preserve pre-coverage-contract identities when the optional field is absent."""
    record = asdict(scope)
    for branch in record["branches"]:
        if branch["coverage_actions"] is None:
            del branch["coverage_actions"]
    return record


class StarterConfirmation(Protocol):
    def guard(
        self, actor: IntakePrincipal, work: IntakeWork, *, purpose: str
    ) -> AbstractContextManager[ConfirmedStarterMap]:
        """Hold current scope, source/map confirmation and named role through commit.

        Resolve current actor rights independently of IntakePrincipal.role. Review
        requires the named owner; materialize permits only a current worker reader.
        Confirm decision-time fields, citations and family equivalence upstream.
        Deny owner mutations after output exposure; never yield a draft map here.
        Resolve the pinned plan references from current owner settings, not request claims.
        """
        ...


@dataclass(frozen=True)
class ProposedCase:
    case_id: str
    family_id: str
    template_id: str
    branch_id: str
    decision_inputs: tuple[tuple[str, str | int | float | bool | None], ...]
    suggestion: str
    rationale: str
    citation: str
    case_origin: str = "generated_starter"
    authoring_identity: str | None = None


@dataclass(frozen=True)
class FrozenPlan:
    baseline_ref: str
    comparator_ref: str
    scorer_ref: str
    abstention_ref: str
    version: str
    evaluator_ref: str | None = None
    evaluated_model_ref: str | None = None


@dataclass(frozen=True)
class FrozenStarter:
    reference: str
    revision: str
    proposed: int
    reviewed: int
    resolved: int
    unresolved: int
    development: int
    reserved: int
    excluded: int
    reserved_family_refs: tuple[str, ...]
    coverage: tuple[tuple[str, str, int], ...]
    uncovered_critical: tuple[str, ...]
    evidence_origin: str = "expert_reviewed_generated_starter"
    state: str = "frozen_unscored"


class StarterCases:
    def __init__(self, directory: Path, confirmation: StarterConfirmation | None = None):
        self.path = directory.absolute() / "starter-cases.sqlite3"
        self.confirmation = confirmation

    @contextmanager
    def _scope(
        self, actor: IntakePrincipal, work: IntakeWork, purpose: str = "review"
    ) -> Iterator[ConfirmedStarterMap]:
        _require(self.confirmation is not None, "confirmed_starter_map_unavailable")
        assert self.confirmation is not None
        with self.confirmation.guard(actor, work, purpose=purpose) as scope:
            for branch in scope.branches:
                _require(
                    branch.coverage_actions is None
                    or (
                        isinstance(branch.coverage_actions, tuple)
                        and bool(branch.coverage_actions)
                        and len(set(branch.coverage_actions)) == len(branch.coverage_actions)
                        and set(branch.coverage_actions) <= set(branch.allowed_actions)
                    ),
                    "invalid_starter_coverage_contract",
                )
            _require(
                scope.tenant == actor.tenant
                and scope.work == work
                and work.mode == "starter"
                and work.benchmark_current
                and work.revisions == (scope.policy_revision, scope.settings_revision)
                and (purpose == "materialize" or actor.actor == scope.owner_id),
                "starter_scope_changed",
            )
            for ref in (
                scope.map_ref,
                scope.confirmation_ref,
                scope.source_digest,
                scope.owner_id,
                scope.policy_revision,
                scope.settings_revision,
            ):
                require_ref(ref)
            _require(
                scope.branches and len({b.branch_id for b in scope.branches}) == len(scope.branches)
            )
            yield scope

    @contextmanager
    def _db(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        if write:
            p = self.path.parent
            _require(p.resolve(strict=True) == p and p.is_dir())
            _require(p.stat().st_uid == os.getuid() and not p.stat().st_mode & 0o077)
            try:
                os.close(
                    os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                )
            except FileExistsError:
                pass
        _check_file(self.path)
        if not write:
            require_readonly_snapshot(self.path)
        db = sqlite3.connect(
            self.path.as_uri() + ("?mode=rw" if write else "?mode=ro"),
            uri=True,
            isolation_level=None,
            timeout=10,
        )
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA trusted_schema=OFF")
            if not write:
                db.execute("PRAGMA query_only=ON")
            db.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            if write:
                db.execute(
                    "CREATE TABLE IF NOT EXISTS packs (ref TEXT PRIMARY KEY, "
                    "scope TEXT NOT NULL, proposal TEXT NOT NULL, body TEXT NOT NULL, "
                    "seal TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS freezes (ref TEXT PRIMARY KEY, seal TEXT NOT NULL)"
                )
                db.execute(
                    "CREATE TABLE IF NOT EXISTS families (tenant TEXT, epoch TEXT, "
                    "workflow TEXT, family TEXT, pack TEXT, split TEXT, "
                    "PRIMARY KEY(tenant,epoch,workflow,family))"
                )
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _ref(scope: ConfirmedStarterMap) -> str:
        return "starter-" + digest(_scope_record(scope))[:40]

    def _load(self, db: sqlite3.Connection, scope: ConfirmedStarterMap) -> Json:
        ref = self._ref(scope)
        row = db.execute("SELECT * FROM packs WHERE ref=?", (ref,)).fetchone()
        _require(row is not None and row["scope"] == canonical(_scope_record(scope)))
        assert row is not None
        return self._validated_pack(db, row)

    @staticmethod
    def _scope_key(scope: Json) -> tuple[str, str, str]:
        return (
            scope["tenant"],
            scope["work"]["resource"]["engagement_epoch"],
            scope["work"]["workflow_ref"],
        )

    def _validated_pack(self, db: sqlite3.Connection, row: sqlite3.Row) -> Json:
        ref = row["ref"]
        stored_scope = json.loads(row["scope"])
        _require(ref == "starter-" + digest(stored_scope)[:40], "starter_receipt_invalid")
        body: Json = json.loads(row["body"])
        _require(row["seal"] == digest(body), "starter_receipt_invalid")
        seal = db.execute("SELECT seal FROM freezes WHERE ref=?", (ref,)).fetchone()
        _require(
            (body["frozen"] is None and seal is None)
            or (body["frozen"] is not None and seal is not None and seal["seal"] == row["seal"]),
            "starter_receipt_invalid",
        )
        assigned = {tuple(r) for r in db.execute("SELECT * FROM families WHERE pack=?", (ref,))}
        expected = (
            {
                (*self._scope_key(stored_scope), token, ref, split)
                for token, split in self._assignments(body["cases"]).items()
            }
            if body["frozen"] is not None
            else set()
        )
        _require(assigned == expected, "starter_reservation_invalid")
        return body

    def _validate_prior_freezes(self, db: sqlite3.Connection, scope: ConfirmedStarterMap) -> None:
        """A successor must reconcile earlier reservations even if their scope was restored.

        Inspect only prior scope identities globally; protected case bodies are read
        only for the same tenant/epoch/workflow. A changed scope cannot hide a pack
        because its immutable reference is derived from that complete scope.
        """
        key = self._scope_key(_scope_record(scope))
        known = set()
        relevant = set()
        for row in db.execute("SELECT ref,scope FROM packs"):
            stored_scope = json.loads(row["scope"])
            _require(
                row["ref"] == "starter-" + digest(stored_scope)[:40], "starter_receipt_invalid"
            )
            known.add(row["ref"])
            if self._scope_key(stored_scope) == key:
                relevant.add(row["ref"])
        _require(
            all(r[0] in known for r in db.execute("SELECT ref FROM freezes")),
            "starter_receipt_invalid",
        )
        for ref in relevant:
            row = db.execute("SELECT * FROM packs WHERE ref=?", (ref,)).fetchone()
            assert row is not None
            self._validated_pack(db, row)
        _require(
            all(
                r[0] in relevant
                for r in db.execute(
                    "SELECT pack FROM families WHERE tenant=? AND epoch=? AND workflow=?", key
                )
            ),
            "starter_reservation_invalid",
        )

    @staticmethod
    def _assignments(cases: list[Json]) -> dict[str, str]:
        result: dict[str, str] = {}
        for case in cases:
            split = case["split_ref"]
            if split == "excluded":
                continue
            fingerprint = digest(dict(case["decision_inputs"]))
            for identity in (
                "family:" + case["family_id"],
                "template:" + case["template_id"],
                "input:" + fingerprint,
            ):
                _require(result.setdefault(identity, split) == split, "starter_family_conflict")
        return result

    def _save(self, db: sqlite3.Connection, scope: ConfirmedStarterMap, body: Json) -> None:
        db.execute(
            "UPDATE packs SET body=?,seal=? WHERE ref=?",
            (canonical(body), digest(body), self._ref(scope)),
        )

    @staticmethod
    def _cases(scope: ConfirmedStarterMap, cases: tuple[ProposedCase, ...]) -> list[Json]:
        _require(0 < len(cases) <= 1000, "invalid_starter_cases")
        branches = {b.branch_id: b for b in scope.branches}
        ids: set[str] = set()
        templates: dict[str, str] = {}
        inputs: dict[str, str] = {}
        result = []
        for case in cases:
            for value in (case.case_id, case.family_id, case.template_id, case.branch_id):
                require_ref(value)
            branch = branches.get(case.branch_id)
            _require(branch is not None and case.case_id not in ids, "invalid_starter_cases")
            assert branch is not None
            ids.add(case.case_id)
            identities = [
                scope.owner_id,
                case.authoring_identity,
                scope.frozen_plan.evaluator_ref,
                scope.frozen_plan.evaluated_model_ref,
            ]
            selected = [i for i in identities if i is not None]
            for identity in selected:
                require_ref(identity)
            _require(len(set(selected)) == len(selected), "starter_identity_conflict")
            _require(
                case.case_origin in {"generated_starter", "owner_counterexample"}
                and case.suggestion in branch.allowed_actions
                and case.citation in branch.citations
                and bool(case.rationale)
                and len(case.rationale) <= 2000,
                "unsupported_starter_case",
            )
            fields = dict(case.decision_inputs)
            _require(
                len(fields) == len(case.decision_inputs)
                and fields.keys() <= set(branch.decision_fields),
                "target_leakage",
            )
            fingerprint = digest(fields)
            _require(
                templates.setdefault(case.template_id, case.family_id) == case.family_id
                and inputs.setdefault(fingerprint, case.family_id) == case.family_id,
                "starter_family_conflict",
            )
            row = asdict(case)
            row.update(
                owner_adjudicated_target=None,
                adjudicator=None,
                status="proposed",
                adjudication_reason=None,
                adjudicated_at=None,
                split_ref=None,
            )
            result.append(row)
        return result

    def propose(
        self, actor: IntakePrincipal, work: IntakeWork, cases: tuple[ProposedCase, ...]
    ) -> str:
        with self._scope(actor, work, "propose") as scope:
            rows = self._cases(scope, cases)
            proposal = digest(rows)
            ref = self._ref(scope)
            with self._db(write=True) as db:
                prior = db.execute("SELECT proposal FROM packs WHERE ref=?", (ref,)).fetchone()
                if prior is not None:
                    _require(prior["proposal"] == proposal, "starter_successor_required")
                    self._load(db, scope)
                    return ref
                body = dict(cases=rows, revision=0, operations={}, frozen=None)
                db.execute(
                    "INSERT INTO packs VALUES(?,?,?,?,?)",
                    (ref, canonical(_scope_record(scope)), proposal, canonical(body), digest(body)),
                )
                return ref

    def adjudicate(
        self,
        actor: IntakePrincipal,
        work: IntakeWork,
        *,
        case_id: str,
        target: str | None,
        reason: str,
        expected_revision: int,
        operation_ref: str,
    ) -> int:
        require_ref(operation_ref)
        _require(bool(reason) and len(reason) <= 2000, "adjudication_reason_required")
        intent = digest([case_id, target, reason, expected_revision])
        with self._scope(actor, work, "adjudicate") as scope, self._db(write=True) as db:
            body = self._load(db, scope)
            replay = body["operations"].get(operation_ref)
            if replay is not None:
                _require(replay["intent"] == intent, "starter_action_conflict")
                return int(replay["revision"])
            _require(body["frozen"] is None, "starter_successor_required")
            _require(body["revision"] == expected_revision, "starter_revision_changed")
            case = next((c for c in body["cases"] if c["case_id"] == case_id), None)
            _require(case is not None, "invalid_starter_case")
            assert case is not None
            branch = next(b for b in scope.branches if b.branch_id == case["branch_id"])
            _require(target is None or target in branch.allowed_actions, "unsupported_target")
            case.update(
                owner_adjudicated_target=target,
                adjudicator=scope.owner_id,
                status="unresolved" if target is None else "resolved",
                disposition=(
                    "unresolved"
                    if target is None
                    else "accept"
                    if target == case["suggestion"]
                    else "edit"
                ),
                adjudication_reason=reason,
                adjudicated_at=datetime.now(UTC).isoformat(),
            )
            body["revision"] += 1
            body["operations"][operation_ref] = dict(intent=intent, revision=body["revision"])
            self._save(db, scope, body)
            return int(body["revision"])

    def review(self, actor: IntakePrincipal, work: IntakeWork) -> Json:
        """Named owner only. There is deliberately no prompt/training/export audience."""
        with self._scope(actor, work) as scope, self._db() as db:
            return self._load(db, scope)

    @staticmethod
    def _frozen(value: Json) -> FrozenStarter:
        data = dict(value)
        for key in ("reserved_family_refs", "uncovered_critical"):
            data[key] = tuple(data[key])
        data["coverage"] = tuple(tuple(c) for c in data["coverage"])
        return FrozenStarter(**data)

    def freeze(
        self,
        actor: IntakePrincipal,
        work: IntakeWork,
        *,
        splits: dict[str, str],
        plan: FrozenPlan,
        expected_revision: int,
    ) -> FrozenStarter:
        for value in asdict(plan).values():
            if value is not None:
                require_ref(value)
        with self._scope(actor, work, "freeze") as scope, self._db(write=True) as db:
            _require(scope.frozen_plan == plan, "starter_plan_changed")
            body = self._load(db, scope)
            self._validate_prior_freezes(db, scope)
            intent = digest([splits, asdict(plan), expected_revision])
            if body["frozen"] is not None:
                _require(body["freeze_intent"] == intent, "starter_successor_required")
                return self._frozen(body["frozen"])
            _require(body["revision"] == expected_revision, "starter_revision_changed")
            cases = body["cases"]
            families = {c["family_id"] for c in cases}
            _require(
                set(splits) == families
                and set(splits.values()) <= {"development", "reserved", "excluded"},
                "invalid_family_split",
            )
            targets: dict[str, str] = {}
            for case in cases:
                _require(case["status"] != "proposed", "owner_adjudication_required")
                _require(
                    case["status"] == "resolved" or splits[case["family_id"]] == "excluded",
                    "unresolved_family_must_be_excluded",
                )
                if case["status"] == "resolved":
                    fingerprint = digest(dict(case["decision_inputs"]))
                    target = case["owner_adjudicated_target"]
                    _require(
                        targets.setdefault(fingerprint, target) == target, "starter_truth_conflict"
                    )
                case["split_ref"] = splits[case["family_id"]]
            _require("reserved" in splits.values(), "reserved_families_required")
            ref = self._ref(scope)
            assignments = self._assignments(cases)
            for family, split in assignments.items():
                if split == "excluded":
                    continue
                key = scope.tenant, work.resource.engagement_epoch, work.workflow_ref, family
                prior = db.execute(
                    "SELECT pack FROM families WHERE tenant=? AND epoch=? "
                    "AND workflow=? AND family=?",
                    key,
                ).fetchone()
                _require(prior is None, "starter_family_already_assigned")
                db.execute("INSERT INTO families VALUES(?,?,?,?,?,?)", (*key, ref, split))
            coverage = tuple(
                (
                    b.branch_id,
                    action,
                    len(
                        {
                            c["family_id"]
                            for c in cases
                            if c["branch_id"] == b.branch_id
                            and c["owner_adjudicated_target"] == action
                            and c["split_ref"] == "reserved"
                        }
                    ),
                )
                for b in scope.branches
                for action in (
                    b.allowed_actions if b.coverage_actions is None else b.coverage_actions
                )
            )
            missing = tuple(
                sorted(
                    {
                        b.branch_id
                        for b in scope.branches
                        if b.critical
                        and any(n == 0 for branch, _, n in coverage if branch == b.branch_id)
                    }
                )
            )
            revision = digest([_scope_record(scope), cases, asdict(plan), splits])
            result = FrozenStarter(
                "starter-freeze-" + revision[:40],
                revision,
                len(cases),
                sum(c["adjudicator"] is not None for c in cases),
                sum(c["status"] == "resolved" for c in cases),
                sum(c["status"] == "unresolved" for c in cases),
                sum(s == "development" for s in splits.values()),
                sum(s == "reserved" for s in splits.values()),
                sum(c["split_ref"] == "excluded" for c in cases),
                tuple(
                    "starter-family-" + digest([ref, f])[:40]
                    for f in sorted(families)
                    if splits[f] == "reserved"
                ),
                coverage,
                missing,
            )
            body.update(frozen=asdict(result), freeze_intent=intent, plan=asdict(plan))
            self._save(db, scope, body)
            db.execute("INSERT INTO freezes VALUES(?,?)", (ref, digest(body)))
            return result

    def materialize(
        self, actor: IntakePrincipal, work: IntakeWork, *, operation_ref: str
    ) -> MaterializedBenchmark | None:
        """Adopt an actual owner freeze; never manufacture confirmation or execute a model."""
        return self.readback(actor, work, operation_ref=operation_ref)

    def readback(
        self, actor: IntakePrincipal, work: IntakeWork, *, operation_ref: str
    ) -> MaterializedBenchmark | None:
        require_ref(operation_ref)
        with self._scope(actor, work, "materialize") as scope:
            if not self.path.exists():
                return None
            with self._db() as db:
                body = self._load(db, scope)
                if body["frozen"] is None:
                    return None
                frozen = self._frozen(body["frozen"])
                return MaterializedBenchmark(
                    "frozen_unscored", frozen.reference, frozen.revision, work.revisions[0]
                )
