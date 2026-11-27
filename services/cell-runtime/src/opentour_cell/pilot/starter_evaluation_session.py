"""Normal local scoring composition; inspection never enters the execution path."""

from __future__ import annotations

import hmac
import re
import sqlite3
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal
from opentour_cell.pilot.model_registry import ModelRegistry
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _has_table, _open_db
from opentour_cell.pilot.starter_evaluation_authority import (
    StarterEvaluationAuthority,
    StarterInspectionAuthority,
)
from opentour_cell.pilot.starter_local_baseline import LocalStarterBaseline, _private_bytes
from opentour_cell.pilot.starter_map_comparator import StarterMapComparator
from opentour_cell.pilot.starter_score_inspection import StarterScoreInspection
from opentour_cell.pilot.starter_scoring import EvaluationPermission, StarterScoring

if TYPE_CHECKING:
    from opentour_cell.pilot.intake_admission import IntakeWork
    from opentour_cell.pilot.slack_adapter import Settings

Json = dict[str, Any]
RUNTIME = "opentour.pilot.starter-evaluation-runtime.v1"
_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}")
_COMPARATOR_KEY = b"opentour.starter-comparator-receipt-key.v1"


def _require(value: object) -> None:
    if not value:
        raise IntakeError("starter_evaluation_runtime_unavailable")


class StarterEvaluationSession:
    """The installed caller supplies an independently authenticated worker or owner.

    Runtime selectors name existing private files only. Neither a model selector,
    a grant subject nor an owner setting authenticates the caller of this service.
    """

    def __init__(self, settings: Callable[[], Settings], actor: IntakePrincipal):
        self.settings, self.actor = settings, actor
        cfg = settings()
        _require(cfg.organization == actor.tenant)
        self.directory = cfg.directory

    def _current(self) -> None:
        cfg = self.settings()
        _require(cfg.organization == self.actor.tenant and cfg.directory == self.directory)

    def entry(self, execution_ref: str) -> Json | None:
        """Current owner navigation only, without model or execution operations."""
        try:
            self._current()
            return StarterInspectionAuthority(self.settings, self.actor).entry(execution_ref)
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            return None

    def entries(self) -> tuple[Json, ...]:
        """Enumerate only currently authorized selected inspection navigation."""
        try:
            self._current()
            authority = StarterInspectionAuthority(self.settings, self.actor)
            entries = tuple(
                entry
                for execution in sorted(authority.selection["inspections"])
                if (entry := authority.entry(execution)) is not None
            )
            _require(all(authority.entry(entry["execution_ref"]) == entry for entry in entries))
            self._current()
            return entries
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            return ()

    def summary(self, execution_ref: str, *, expected_revision: str | None = None) -> Json:
        self._current()
        authority = StarterInspectionAuthority(self.settings, self.actor)
        entry = authority.entry(execution_ref)
        _require(entry is not None)
        assert entry is not None
        _require(expected_revision is None or expected_revision == entry["revision"])
        with authority.guard(self.actor, entry["workflow_ref"], execution_ref) as scope:
            _require(scope.inspection_revision == entry["revision"])
            result = StarterScoreInspection(self.directory, authority).summary(
                self.actor, entry["workflow_ref"], execution_ref
            )
            authority.revalidate(self.actor, scope)
            _require(result["freeze_revision"] == scope.permission.freeze_revision)
            return result | dict(
                workflow_ref=entry["workflow_ref"],
                execution_ref=execution_ref,
                model_ref=scope.permission.model_ref,
                model_revision=scope.permission.model_revision,
                comparator_ref=scope.comparator_ref,
                comparator_revision=scope.permission.comparator_revision,
            )

    def _hold(
        self,
        authority: StarterEvaluationAuthority,
        work: IntakeWork,
        permission: EvaluationPermission,
        reason: str,
    ) -> Json:
        """Preserve a started or uncertain execution when its runtime is unavailable."""
        frozen = authority.cases.evaluation_inputs(self.actor, work)
        inputs = {
            "starter-case-" + digest([frozen.freeze_revision, case.case_id])[:40]: dict(
                case.decision_inputs
            )
            for case in frozen.cases
        }
        definition = dict(
            permission=asdict(permission),
            input_digest=digest(inputs),
            comparator_revision=permission.comparator_revision,
        )
        result = StarterScoring._held(permission, reason)
        authority.revalidate(self.actor, work, permission)
        try:
            with _open_db(self.directory / "starter-evaluations.sqlite3") as db:
                if db is not None:
                    tables = {
                        row[0]
                        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
                    }
                    _require({"evaluations", "predictions"} <= tables)
                    row = db.execute(
                        "SELECT definition,result FROM evaluations WHERE ref=?",
                        (permission.execution_ref,),
                    ).fetchone()
                    attempts = db.execute(
                        "SELECT 1 FROM predictions "
                        "WHERE json_extract(request,'$.execution_ref')=? LIMIT 1",
                        (permission.execution_ref,),
                    ).fetchone()
                    if row is not None:
                        _require(row["definition"] == canonical(definition))
                        result.update(
                            state="held",
                            reason_code="existing_execution_requires_reconciliation",
                            next_action="reconcile_existing_execution_or_inspect_retained_result",
                        )
                    elif attempts is not None:
                        raise IntakeError("starter_execution_state_unavailable")
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            result.update(
                state="held",
                reason_code="execution_state_unavailable",
                next_action="reconcile_existing_execution",
            )
        authority.revalidate(self.actor, work, permission)
        return result

    def _ports(
        self,
        authority: StarterEvaluationAuthority,
        work: IntakeWork,
        permission: EvaluationPermission,
        registry: ModelRegistry,
    ) -> tuple[LocalStarterBaseline, StarterMapComparator]:
        document = _read_private(self.directory / "starter-evaluation-runtime.json", 16384)
        _require(
            set(document) == {"schema_version", "models"} and document["schema_version"] == RUNTIME
        )
        models = document["models"]
        _require(isinstance(models, dict) and 0 < len(models) <= 32)
        selected = models.get(permission.model_ref)
        _require(
            isinstance(selected, dict) and set(selected) == {"artifact_file", "receipt_key_file"}
        )
        for value in selected.values():
            _require(
                isinstance(value, str) and _BASENAME.fullmatch(value) and value not in {".", ".."}
            )
        _require(
            selected["artifact_file"].endswith(".json")
            and selected["artifact_file"] != selected["receipt_key_file"]
        )
        binding = registry.binding(permission.model_ref)
        _require(binding.immutable_revision == permission.model_revision)
        grant = registry.current_grant(self.actor.actor, binding, model.Purpose.BASELINE)
        _require(
            grant is not None
            and grant.revision == permission.baseline_grant_revision
            and grant.valid_until > time.time()
            and set(permission.input_fields) <= grant.permitted_fields
        )
        if binding.input_schema == "starter-structured-fields-v1":
            input_field = None
        else:
            match = re.fullmatch(r"starter-text-([a-z][a-z0-9_]{0,63})-v1", binding.input_schema)
            _require(match is not None)
            assert match is not None
            input_field = match[1]
        key_path = self.directory / selected["receipt_key_file"]
        key = _private_bytes(key_path, 32)
        _require(len(key) == 32)
        with _open_db(self.directory / "starter-comparator-receipts.sqlite3") as db:
            _require(db is not None and _has_table(db, "receipts"))
            assert db is not None
            _require(
                {r[1] for r in db.execute("PRAGMA table_info(receipts)")}
                == {"operation", "body", "seal"}
            )
        authority.revalidate(self.actor, work, permission)
        baseline = LocalStarterBaseline(
            self.directory,
            actor=self.actor,
            work=work,
            permission=permission,
            authority=authority,
            registry=registry,
            cases=authority.cases,
            artifact=self.directory / selected["artifact_file"],
            receipt_key=key_path,
            input_field=input_field,
        )
        # Metadata-only artifact/identity preflight happens before any prediction.
        baseline.observe(binding, model.Purpose.BASELINE)
        comparator = StarterMapComparator(
            self.directory,
            self.actor,
            work,
            authority.cases,
            authority,
            permission,
            authority.comparator_context(self.actor, work, permission).comparator_ref,
            hmac.digest(key, _COMPARATOR_KEY, "sha256"),
        )
        return baseline, comparator

    def run(self, execution_ref: str) -> Json:
        """Execute only the already selected sealed intent; never provision or train."""
        self._current()
        authority = StarterEvaluationAuthority(self.settings, self.actor)
        work = authority.work(execution_ref)
        registry = ModelRegistry(self.directory / "model-registration.json", self.actor.tenant)
        with authority.guard(self.actor, work, execution_ref) as permission:
            try:
                baseline, comparator = self._ports(authority, work, permission, registry)
            except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
                return self._hold(authority, work, permission, "starter_runtime_unavailable")
            authority.revalidate(self.actor, work, permission)
        scorer = StarterScoring(
            self.directory, authority.cases, authority, registry, baseline, comparator
        )
        try:
            result = scorer.evaluate(self.actor, work, execution_ref)
        except (ValueError, OSError, sqlite3.Error, KeyError, TypeError):
            with authority.guard(self.actor, work, execution_ref) as current:
                _require(current == permission)
                return self._hold(authority, work, permission, "starter_runtime_unavailable")
        if result["state"] != "scored":
            with authority.guard(self.actor, work, execution_ref) as current:
                _require(current == permission)
                return self._hold(authority, work, permission, result["reason_code"])
        return result
