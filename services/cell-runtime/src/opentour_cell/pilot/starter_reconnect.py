"""Authorize a private reconnect inspection without changing model configuration."""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
import time
from typing import Any

from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_mcp import IntakeError, require_ref
from opentour_cell.pilot.onboarding_mcp import _read_private
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.receipt_recovery import _has_table, _open_db
from opentour_cell.pilot.starter_evaluation_authority import StarterInspectionAuthority
from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
from opentour_cell.pilot.workflow_benchmarks import BenchmarkIntake

GRANT = "opentour.pilot.starter-model-reconnect-grant.v1"
_TARGET = (
    "tenant",
    "engagement_epoch",
    "workflow_ref",
    "run_ref",
    "execution_ref",
    "freeze_ref",
    "freeze_revision",
    "model_ref",
    "model_revision",
    "benchmark_ref",
    "benchmark_version",
)
_FIELDS = {
    *_TARGET,
    "schema_version",
    "grant_ref",
    "model_custodian",
    "inspection_revision",
    "expires_at",
}
Json = dict[str, Any]


def _require(value: object) -> None:
    if not value:
        raise IntakeError("starter_reconnect_unavailable")


class StarterReconnect:
    """Additional role and target check for an already authorized private score viewer."""

    def __init__(self, session: StarterEvaluationSession):
        self.session = session
        self.directory = session.directory

    def _grant(self, execution: str, inspection: str) -> Json:
        actor = self.session.actor
        name = "starter-reconnect-" + digest([actor.tenant, actor.actor, execution]) + ".json"
        envelope = _read_private(self.directory / name, 16384)
        _require(set(envelope) == {"grant", "seal_sha256"})
        cap, seal = envelope["grant"], envelope["seal_sha256"]
        _require(isinstance(cap, dict) and set(cap) == _FIELDS and cap["schema_version"] == GRANT)
        assert isinstance(cap, dict)
        key = _read_private(self.directory / "starter-intake-key.json", 4096)["key"]
        _require(isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key))
        scoped = hmac.digest(bytes.fromhex(key), GRANT.encode(), "sha256")
        expected = hmac.digest(scoped, canonical(cap).encode(), "sha256").hex()
        _require(isinstance(seal, str) and hmac.compare_digest(seal, expected))
        for name in _FIELDS - {"expires_at", "benchmark_version"}:
            require_ref(cap[name])
        _require(
            (
                cap["tenant"],
                cap["model_custodian"],
                cap["execution_ref"],
                cap["inspection_revision"],
            )
            == (actor.tenant, actor.actor, execution, inspection)
        )
        _require(type(cap["benchmark_version"]) is int and cap["benchmark_version"] > 0)
        _require(type(cap["expires_at"]) is int and time.time() < cap["expires_at"] < 2**63)
        _require(
            not os.path.lexists(
                self.directory / f"starter-reconnect-withdrawn-{cap['grant_ref']}.json"
            )
        )
        return cap

    def _idle(self, execution: str) -> None:
        # An edited adapter identity cannot prove a surviving receipt unrelated.
        with _open_db(self.directory / "starter-evaluations.sqlite3") as scoring:
            for filename in (
                "starter-local-predictions.sqlite3",
                "starter-comparator-receipts.sqlite3",
            ):
                with _open_db(self.directory / filename) as db:
                    if db is None:
                        continue
                    _require(_has_table(db, "receipts"))
                    for row in db.execute(
                        "SELECT operation, json_extract(body, '$.request') AS request FROM receipts"
                    ):
                        _require(scoring is not None)
                        assert scoring is not None
                        original = scoring.execute(
                            "SELECT request, json_extract(request, '$.execution_ref') AS execution "
                            "FROM predictions WHERE ref=?",
                            (row["operation"],),
                        ).fetchone()
                        _require(
                            original is not None
                            and row["request"] == original["request"]
                            and isinstance(original["execution"], str)
                            and original["execution"] != execution
                        )

    def entry(self, execution_ref: str, *, expected_revision: str) -> Json | None:
        try:
            require_ref(execution_ref)
            require_ref(expected_revision)
            actor = self.session.actor
            _require(actor.role == "slack")
            cap = self._grant(execution_ref, expected_revision)
            cfg = self.session.settings()
            cfg.permission(actor.actor, "inspect")
            authority = StarterInspectionAuthority(self.session.settings, actor)
            grant, permission = authority._grant(execution_ref)
            _require(digest(grant) == expected_revision)
            metadata = authority._metadata(grant, permission)
            setup = metadata.setup
            intake = BenchmarkIntake(DurableIntake(self.directory, setup, existing_only=True))
            current = intake.get_run(setup.principal, permission.run_ref)
            benchmark = current.benchmark
            _require(
                benchmark is not None
                and benchmark.benchmark_ref == cap["benchmark_ref"]
                and benchmark.version == cap["benchmark_version"]
                and benchmark.benchmark_state in {"draft", "needs_owner_review", "frozen_unscored"}
            )
            _require(
                (permission.tenant, permission.epoch, permission.run_ref)
                == (cap["tenant"], cap["engagement_epoch"], cap["run_ref"])
            )
            summary = self.session.summary(execution_ref, expected_revision=expected_revision)
            _require(summary["state"] == "frozen_unscored")
            _require(
                all(
                    summary[name] == cap[name]
                    for name in (
                        "workflow_ref",
                        "execution_ref",
                        "freeze_ref",
                        "freeze_revision",
                        "model_ref",
                        "model_revision",
                    )
                )
            )
            self._idle(execution_ref)
            _require(intake.get_run(setup.principal, permission.run_ref) == current)
            _require(
                self.session.summary(execution_ref, expected_revision=expected_revision) == summary
            )
            _require(self._grant(execution_ref, expected_revision) == cap)
            self.session.settings().permission(actor.actor, "inspect")
            return {
                **{name: cap[name] for name in _TARGET if name != "tenant"},
                "revision": digest(cap),
                "custodian_ref": cap["model_custodian"],
                "state": "setup_required",
            }
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError):
            return None
