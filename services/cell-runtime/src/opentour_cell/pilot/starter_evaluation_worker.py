"""Execute one existing starter score through a separately sealed worker identity."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import re
import sqlite3
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Never

from opentour_cell.pilot.intake_mcp import IntakeError, IntakePrincipal, require_ref
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.readout import parse_manifest
from opentour_cell.pilot.slack_adapter import Settings
from opentour_cell.pilot.starter_evaluation_authority import EXECUTION, SELECTION
from opentour_cell.pilot.starter_evaluation_session import StarterEvaluationSession
from opentour_cell.pilot.starter_local_baseline import _private_bytes

INVOCATION = "opentour.pilot.starter-worker-invocation.v1"
SELECTOR = "opentour.pilot.starter-worker-selector.v1"
CURRENT = "opentour.pilot.starter-worker-current.v1"
RESULT = "opentour.pilot.starter-worker-result.v1"
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,95}\.json")
_HEX = re.compile(r"[0-9a-f]{64}")
_FIELDS = {
    "schema_version",
    "invocation_ref",
    "custodian_ref",
    "tenant",
    "evaluator",
    "epoch",
    "workflow_ref",
    "run_ref",
    "freeze_ref",
    "freeze_revision",
    "execution_ref",
    "execution_grant_file",
    "execution_grant_ref",
    "execution_grant_digest",
    "settings_file",
    "settings_sha256",
    "expires_at",
}


def _require(condition: object) -> None:
    if not condition:
        raise IntakeError("starter_worker_unavailable")


def _document(path: Path, maximum: int = 65536) -> dict[str, Any]:
    return parse_manifest(_private_bytes(path, maximum))


class _Invocation:
    def __init__(self, selector: Path):
        _require(selector.is_absolute() and selector.resolve(strict=True) == selector)
        self.path, self.directory = selector, selector.parent
        self.selector = _document(selector, 4096)
        _require(set(self.selector) == {"schema_version", "invocation_file"})
        _require(self.selector["schema_version"] == SELECTOR)
        filename = self.selector["invocation_file"]
        _require(isinstance(filename, str) and _NAME.fullmatch(filename))
        self.envelope_path = self.directory / filename
        self.envelope = _document(self.envelope_path)
        _require(set(self.envelope) == {"invocation", "seal_sha256"})
        cap = self.envelope["invocation"]
        _require(isinstance(cap, dict) and set(cap) == _FIELDS)
        _require(cap["schema_version"] == INVOCATION)
        for name, value in cap.items():
            if name == "expires_at":
                _require(type(value) is int and 0 < value < 2**63)
            else:
                require_ref(value)
        for name in ("execution_grant_file", "settings_file"):
            _require(_NAME.fullmatch(cap[name]))
        for name in ("freeze_revision", "execution_grant_digest", "settings_sha256"):
            _require(_HEX.fullmatch(cap[name]))
        _require(cap["freeze_ref"] == "starter-freeze-" + cap["freeze_revision"][:40])
        self.cap = dict(cap)
        self.key = self._key()
        self.current_path = self.directory / "starter-worker-current.json"
        self.current = _document(self.current_path, 16384)
        self._authenticate()
        # Only this separately authenticated envelope supplies the identity.
        self.actor = IntakePrincipal(cap["tenant"], cap["evaluator"], "sealed-starter-worker")

    def _key(self) -> bytes:
        doc = _document(self.directory / "starter-intake-key.json", 4096)
        _require(set(doc) == {"key"} and isinstance(doc["key"], str) and _HEX.fullmatch(doc["key"]))
        return hmac.digest(bytes.fromhex(doc["key"]), INVOCATION.encode(), "sha256")

    def _authenticate(self) -> None:
        cap = self.cap
        _require(self._key() == self.key)
        _require(_document(self.path, 4096) == self.selector)
        _require(_document(self.envelope_path) == self.envelope)
        seal = self.envelope["seal_sha256"]
        _require(isinstance(seal, str) and _HEX.fullmatch(seal))
        _require(
            hmac.compare_digest(
                seal, hmac.digest(self.key, canonical(cap).encode(), "sha256").hex()
            )
        )
        current = _document(self.current_path, 16384)
        _require(current == self.current and set(current) == {"schema_version", "invocations"})
        _require(current["schema_version"] == CURRENT)
        entries = current["invocations"]
        _require(isinstance(entries, dict) and 0 < len(entries) <= 64)
        for ref, value in entries.items():
            require_ref(ref)
            _require(isinstance(value, str) and _HEX.fullmatch(value))
        _require(entries.get(cap["invocation_ref"]) == digest(self.envelope))
        _require(time.time() < cap["expires_at"])
        _require(
            not os.path.lexists(
                self.directory / f"starter-worker-withdrawn-{cap['invocation_ref']}.json"
            )
        )
        # This is a metadata fence before Settings or any private policy read.
        # The normal session independently authenticates this grant and all its
        # original case/source/model authority; these pins grant no such rights.
        selected = _document(self.directory / "starter-evaluation-selection.json", 16384)
        _require(selected.get("schema_version") == SELECTION)
        _require(isinstance(selected.get("executions"), dict))
        _require(selected["executions"].get(cap["execution_ref"]) == cap["execution_grant_file"])
        envelope = _document(self.directory / cap["execution_grant_file"])
        _require(digest(envelope) == cap["execution_grant_digest"])
        grant = envelope.get("grant")
        _require(isinstance(grant, dict) and grant.get("schema_version") == EXECUTION)
        assert isinstance(grant, dict)
        _require(grant.get("grant_ref") == cap["execution_grant_ref"])
        _require(
            grant.get("custodian_ref") == cap["custodian_ref"]
            and grant.get("tenant") == cap["tenant"]
        )
        _require(type(grant.get("expires_at")) is int and time.time() < grant["expires_at"])
        _require(
            not os.path.lexists(
                self.directory / f"starter-evaluation-withdrawn-{cap['execution_grant_ref']}.json"
            )
        )
        permission = grant.get("permission")
        _require(isinstance(permission, dict))
        assert isinstance(permission, dict)
        for key in (
            "tenant",
            "evaluator",
            "epoch",
            "workflow_ref",
            "run_ref",
            "freeze_ref",
            "freeze_revision",
            "execution_ref",
        ):
            _require(permission.get(key) == cap[key])
        _require(permission.get("revision") == cap["execution_grant_ref"])
        _require(
            type(permission.get("expires_at")) is int and time.time() < permission["expires_at"]
        )

    def settings(self) -> Settings:
        self._authenticate()
        path = self.directory / self.cap["settings_file"]
        _require(
            hashlib.sha256(_private_bytes(path, 65536)).hexdigest() == self.cap["settings_sha256"]
        )
        cfg = Settings.load(path)
        _require(cfg.directory == self.directory and cfg.organization == self.cap["tenant"])
        _require(
            hashlib.sha256(_private_bytes(path, 65536)).hexdigest() == self.cap["settings_sha256"]
        )
        self._authenticate()
        return cfg


def execute(selector: Path) -> dict[str, Any]:
    """Authenticate one installed invocation; use the normal existing intent machinery."""
    invocation = _Invocation(selector)
    session = StarterEvaluationSession(invocation.settings, invocation.actor)
    result = session.run(invocation.cap["execution_ref"])
    invocation.settings()
    state = result.get("state")
    _require(state in {"scored", "frozen_unscored", "held"})
    # Do not serialize the session body, exception text or any case-level output.
    return dict(
        schema_version=RESULT,
        state=state,
        invocation_ref=invocation.cap["invocation_ref"],
        execution_ref=invocation.cap["execution_ref"],
        workflow_ref=invocation.cap["workflow_ref"],
        run_ref=invocation.cap["run_ref"],
        freeze_ref=invocation.cap["freeze_ref"],
        reason_code="scored"
        if state == "scored"
        else "existing_freeze_unscored"
        if state == "frozen_unscored"
        else "execution_requires_reconciliation",
        next_action="inspect_private_result"
        if state == "scored"
        else "check_current_execution_and_runtime",
    )


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise IntakeError("starter_worker_unavailable")


def main(argv: Sequence[str] | None = None) -> int:
    parser = _Parser(description=__doc__)
    parser.add_argument("--invocation", type=Path, required=True)
    parser.add_argument("--publish-completion", action="store_true")
    try:
        args = parser.parse_args(argv)
        if args.publish_completion:
            from opentour_cell.pilot.starter_completion import attest

            attest(args.invocation)
            result = dict(schema_version=RESULT, state="scored", reason_code="completion_published")
        else:
            result = execute(args.invocation)
    except (
        ValueError,
        TypeError,
        KeyError,
        OSError,
        RuntimeError,
        sqlite3.Error,
        subprocess.SubprocessError,
    ):
        result = dict(
            schema_version=RESULT,
            state="held",
            reason_code="starter_worker_unavailable",
            next_action="check_installed_invocation_and_current_permissions",
        )
    print(canonical(result))
    return 0 if result["state"] == "scored" else 2


if __name__ == "__main__":
    raise SystemExit(main())
