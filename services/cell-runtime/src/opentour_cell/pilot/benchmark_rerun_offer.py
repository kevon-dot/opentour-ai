"""Resolve an already provisioned, separately authorized pinned rerun."""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from opentour_cell.pilot import model_connector as model
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_mcp import IntakeError, require_ref
from opentour_cell.pilot.model_registry import ModelRegistry
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.starter_evaluation_authority import (
    StarterEvaluationAuthority,
    StarterInspectionAuthority,
)
from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
from opentour_cell.pilot.starter_evaluation_worker import _Invocation
from opentour_cell.pilot.starter_scoring import EvaluationPermission
from opentour_cell.pilot.workflow_benchmarks import (
    BenchmarkIntake,
    BenchmarkScope,
    WorkflowBenchmarks,
)

GRANT = "opentour.pilot.benchmark-rerun-offer.v1"
_FIELDS = {
    "schema_version",
    "grant_ref",
    "expires_at",
    "tenant",
    "actor",
    "epoch",
    "workflow_ref",
    "run_ref",
    "benchmark_ref",
    "benchmark_version",
    "freeze_ref",
    "freeze_revision",
    "original_execution_ref",
    "inspection_revision",
    "rerun_execution_ref",
    "invocation_file",
    "invocation_digest",
}
_BASENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json")
_SHA = re.compile(r"[a-f0-9]{64}")


@dataclass(frozen=True)
class RerunOffer:
    revision: str
    workflow_ref: str
    benchmark_ref: str
    benchmark_version: int
    freeze_ref: str
    freeze_revision: str
    original_execution_ref: str
    rerun_execution_ref: str


def _require(value: object) -> None:
    if not value:
        raise IntakeError("pinned_rerun_unavailable")


class BenchmarkRerunOffers:
    """Read current opaque offer metadata, separate from confirmation and execution."""

    def __init__(self, owner: StarterEvaluationSession):
        self.owner = owner
        self.directory = owner.directory

    def _baseline(
        self, invocation: _Invocation, permission: EvaluationPermission
    ) -> tuple[model.ModelBinding, model.Grant]:
        registry = ModelRegistry(self.directory / "model-registration.json", permission.tenant)
        binding = registry.binding(permission.model_ref)
        grant = registry.current_grant(invocation.actor.actor, binding, model.Purpose.BASELINE)
        _require(
            binding.immutable_revision == permission.model_revision
            and grant is not None
            and grant.revision == permission.baseline_grant_revision
            and time.time() < grant.valid_until
            and set(permission.input_fields) <= grant.permitted_fields
        )
        assert grant is not None
        return binding, grant

    def _grant(self, original: str, rerun: str, inspection: str) -> dict[str, Any]:
        actor = self.owner.actor
        filename = f"benchmark-rerun-{digest([actor.tenant, actor.actor, original, rerun])}.json"
        envelope = _read_private(self.directory / filename, 16384)
        _require(set(envelope) == {"grant", "seal_sha256"})
        grant, signature = envelope["grant"], envelope["seal_sha256"]
        _require(
            isinstance(grant, dict) and set(grant) == _FIELDS and grant["schema_version"] == GRANT
        )
        assert isinstance(grant, dict)
        keys = _read_private(self.directory / "starter-intake-key.json", 4096)
        _require(
            set(keys) == {"key"} and isinstance(keys["key"], str) and _SHA.fullmatch(keys["key"])
        )
        key = hmac.digest(bytes.fromhex(keys["key"]), GRANT.encode(), "sha256")
        expected = hmac.digest(key, canonical(grant).encode(), "sha256").hex()
        _require(isinstance(signature, str) and hmac.compare_digest(signature, expected))
        for field in _FIELDS - {"benchmark_version", "expires_at"}:
            require_ref(grant[field])
        _require(type(grant["benchmark_version"]) is int and grant["benchmark_version"] > 0)
        _require(type(grant["expires_at"]) is int and time.time() < grant["expires_at"] < 2**63)
        _require(_BASENAME.fullmatch(grant["invocation_file"]))
        _require(_SHA.fullmatch(grant["invocation_digest"]))
        _require(
            (
                grant["tenant"],
                grant["actor"],
                grant["original_execution_ref"],
                grant["rerun_execution_ref"],
                grant["inspection_revision"],
            )
            == (actor.tenant, actor.actor, original, rerun, inspection)
        )
        _require(
            not os.path.lexists(
                self.directory / f"benchmark-rerun-withdrawn-{grant['grant_ref']}.json"
            )
        )
        return grant

    def read(self, original: str, rerun: str, *, inspection_revision: str) -> RerunOffer | None:
        actor = self.owner.actor
        try:
            for value in (original, rerun, inspection_revision):
                require_ref(value)
            _require(actor.role == "slack" and rerun != original)
            self.owner.settings().permission(actor.actor, "inspect")
            cap = self._grant(original, rerun, inspection_revision)
            summary = self.owner.summary(original, expected_revision=inspection_revision)
            _require(summary["state"] == "scored")
            _require(
                all(
                    summary[field] == cap[field]
                    for field in ("workflow_ref", "freeze_ref", "freeze_revision")
                )
            )
            authority = StarterInspectionAuthority(self.owner.settings, actor)
            original_grant, permission = authority._grant(original)
            _require(digest(original_grant) == inspection_revision)
            _require((permission.epoch, permission.run_ref) == (cap["epoch"], cap["run_ref"]))
            setup = authority._metadata(original_grant, permission).setup
            intake = BenchmarkIntake(DurableIntake(self.directory, setup, existing_only=True))
            benchmark = intake.get_run(setup.principal, permission.run_ref).benchmark
            _require(
                benchmark is not None
                and benchmark.benchmark_ref == cap["benchmark_ref"]
                and benchmark.version == cap["benchmark_version"]
            )
            definition = WorkflowBenchmarks(self.directory).inspect(
                BenchmarkScope(actor.tenant, permission.epoch, permission.workflow_ref),
                cap["benchmark_ref"],
                current_run=permission.run_ref,
            )
            _require(definition is not None and definition["benchmark_state"] == "scored")
            assert definition is not None
            stored = definition["materialized"]
            _require(
                (stored["definition_ref"], stored["definition_digest"])
                == (cap["freeze_ref"], cap["freeze_revision"])
            )
            invocation = _Invocation(self.directory / cap["invocation_file"])
            _require(digest(invocation.envelope) == cap["invocation_digest"])
            _require(
                all(
                    invocation.cap[field] == cap[field]
                    for field in (
                        "tenant",
                        "epoch",
                        "workflow_ref",
                        "run_ref",
                        "freeze_ref",
                        "freeze_revision",
                    )
                )
                and invocation.cap["execution_ref"] == rerun
            )
            worker = StarterEvaluationAuthority(invocation.settings, invocation.actor)
            new_grant, new_permission = worker._grant(rerun)
            _require(
                (
                    new_permission.model_ref,
                    new_permission.model_revision,
                    new_permission.plan_digest,
                    new_permission.comparator_revision,
                    new_permission.input_fields,
                    new_permission.maximum_cases,
                )
                == (
                    permission.model_ref,
                    permission.model_revision,
                    permission.plan_digest,
                    permission.comparator_revision,
                    permission.input_fields,
                    permission.maximum_cases,
                )
            )
            work = worker.work(rerun)
            _require(work.benchmark_current and work.benchmark_version == cap["benchmark_version"])
            baseline = self._baseline(invocation, new_permission)
            _require(worker._grant(rerun) == (new_grant, new_permission))
            _require(self.owner.summary(original, expected_revision=inspection_revision) == summary)
            invocation.settings()
            _require(self._baseline(invocation, new_permission) == baseline)
            self.owner.settings().permission(actor.actor, "inspect")
            _require(self._grant(original, rerun, inspection_revision) == cap)
            return RerunOffer(
                digest(cap),
                cap["workflow_ref"],
                cap["benchmark_ref"],
                cap["benchmark_version"],
                cap["freeze_ref"],
                cap["freeze_revision"],
                original,
                rerun,
            )
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError):
            return None
