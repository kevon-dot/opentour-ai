"""Append-only local evidence with observed producers and explicit eligibility.

Imported PASS dictionaries are unverified history, never execution authority.
Provider/customer authentication is owned by real adapters; none is fabricated
here. This local store is not a tamper-proof audit journal or a signing service.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tools import schema_validate, test_runner
from tools.validate_spec import (
    TARGET,
    ConfigurationError,
    closed,
    digest,
    strings,
    validate_manifest,
)

KINDS = {"simulated", "local_integration", "provider_integration", "customer_evidence"}
MODES = {"customer_controlled", "opentour_managed"}
RECORD_FIELDS = (
    "requirement_ids issue_key test_id commit_sha spec_sha256 fixture_id fixture_version kind mode "
    "command started_at finished_at exit_code status output_digests limitations"
)
GATE_EXECUTION_STATES = {
    "NOT_RUN",
    "RUNNING",
    "PASS",
    "FAIL",
    "INCOMPLETE",
    "NOT_APPLICABLE_WITH_APPROVED_REASON",
}


class EvidenceError(ValueError):
    """Safe refusal code only."""


def validate_record(record: Any) -> None:
    try:
        closed(record, RECORD_FIELDS)
        strings(record["requirement_ids"], nonempty=True)
        strings(record["command"], nonempty=True)
        strings(record["limitations"])
    except ConfigurationError as exc:
        raise EvidenceError("INVALID_EVIDENCE_RECORD") from exc
    for key in ("issue_key", "test_id", "fixture_id", "fixture_version"):
        if not isinstance(record[key], str) or not record[key] or len(record[key]) > 2048:
            raise EvidenceError("INVALID_EVIDENCE_ID")
    for key, size in (("commit_sha", 40), ("spec_sha256", 64)):
        if not isinstance(record[key], str) or not re.fullmatch(f"[a-f0-9]{{{size}}}", record[key]):
            raise EvidenceError("INVALID_SOURCE_BINDING")
    registry = schema_validate.schema_registry()
    for key in ("started_at", "finished_at"):
        if not schema_validate.valid_instance(
            "urn:opentour:common-contract:1.0#/$defs/Timestamp", record[key], registry
        ):
            raise EvidenceError("INVALID_UTC_TIMESTAMP")
    if datetime.fromisoformat(record["finished_at"]) < datetime.fromisoformat(record["started_at"]):
        raise EvidenceError("REVERSED_EXECUTION_INTERVAL")
    if (
        record["kind"] not in KINDS
        or record["mode"] not in MODES | {None}
        or record["status"] not in {"PASS", "FAIL", "BLOCKED", "NOT_RUN"}
        or (record["exit_code"] is not None and type(record["exit_code"]) is not int)
    ):
        raise EvidenceError("INVALID_EVIDENCE_CLASSIFICATION")
    expected: dict[str, set[int | None]] = {"PASS": {0}, "BLOCKED": {3}, "NOT_RUN": {None}}
    if record["status"] in expected and record["exit_code"] not in expected[record["status"]]:
        raise EvidenceError("CONTRADICTORY_EXECUTION_STATUS")
    if record["status"] == "FAIL" and record["exit_code"] in {None, 0, 3}:
        raise EvidenceError("CONTRADICTORY_EXECUTION_STATUS")
    outputs = record["output_digests"]
    if not isinstance(outputs, dict) or any(
        not isinstance(k, str)
        or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", k)
        or not isinstance(v, str)
        or not re.fullmatch(r"[a-f0-9]{64}", v)
        for k, v in outputs.items()
    ):
        raise EvidenceError("INVALID_OUTPUT_DIGESTS")
    if record["status"] == "PASS" and not outputs:
        raise EvidenceError("PASS_WITHOUT_OUTPUT")


def eligibility(
    record: dict[str, Any],
    *,
    commit: str,
    spec: str,
    required_kind: str,
    required_requirements: set[str],
    test_id: str,
    mode: str | None,
    _provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Pure policy, with provenance supplied by a trusted store observation, never JSON flags."""
    validate_record(record)
    if required_kind not in KINDS or mode not in MODES | {None}:
        raise EvidenceError("INVALID_ELIGIBILITY_POLICY")
    reasons = []
    if (
        _provenance is None
        or _provenance.get("origin") != "observed_command"
        or not isinstance(_provenance.get("receipt"), dict)
        or digest(_provenance["receipt"]) != record["output_digests"].get("command_receipt")
        or record["kind"] not in {"simulated", "local_integration"}
    ):
        reasons.append("UNVERIFIED_PRODUCER")
    if record["status"] != "PASS" or record["exit_code"] != 0:
        reasons.append("EXECUTION_NOT_PASS")
    if record["commit_sha"] != commit:
        reasons.append("STALE_TESTED_COMMIT")
    if record["spec_sha256"] != spec:
        reasons.append("CHANGED_REQUIREMENT_SOURCE")
    if record["kind"] != required_kind:
        reasons.append("REQUIRED_EVIDENCE_KIND_MISMATCH")
    if not required_requirements <= set(record["requirement_ids"]):
        reasons.append("MISSING_REQUIRED_SECURITY_OR_ACCEPTANCE_CHECK")
    if record["test_id"] != test_id or record["mode"] != mode:
        reasons.append("TEST_OR_MODE_MISMATCH")
    if any(
        x in record["limitations"]
        for x in (
            "UNVERIFIED_IMPORTED_RECORD",
            "DIRTY_OR_CHANGED_SOURCE",
            "UNQUALIFIED_PROVIDER_VERIFIER",
        )
    ):
        reasons.append("UNQUALIFIED_SOURCE_OR_PRODUCER")
    return {
        "eligible": not reasons,
        "status": "PASS"
        if not reasons
        else record["status"]
        if record["status"] != "PASS"
        else "BLOCKED",
        "reasons": reasons,
    }


class EvidenceStore:
    """One private, caller-owned local directory; does not open arbitrary external stores."""

    def __init__(self, directory: Path) -> None:
        if directory.is_symlink() or not directory.is_absolute():
            raise EvidenceError("INVALID_EVIDENCE_DIRECTORY")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not directory.is_dir():
            raise EvidenceError("INVALID_EVIDENCE_DIRECTORY")
        self.directory = directory

    def _persist(self, attempt: str, record: dict[str, Any], provenance: dict[str, Any]) -> Path:
        validate_record(record)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", attempt):
            raise EvidenceError("INVALID_ATTEMPT_ID")
        envelope = {
            "schema_version": "opentour.evidence-history.v1",
            "attempt_id": attempt,
            "record": record,
            "record_sha256": digest(record),
            "provenance": provenance,
        }
        encoded = (json.dumps(envelope, sort_keys=True, separators=(",", ":")) + "\n").encode()
        path = self.directory / f"{attempt}.json"
        # No replace: hard-link publication makes concurrent same-identity writers safe.
        fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=self.directory)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
                directory_fd = os.open(self.directory, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except FileExistsError:
                if path.is_symlink() or path.read_bytes() != encoded:
                    raise EvidenceError("ATTEMPT_ID_CONTENT_CONFLICT") from None
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path

    def import_history(self, attempt: str, record: dict[str, Any]) -> Path:
        """Retain historical claims without upgrading their authenticity or destroying data."""
        validate_record(record)
        return self._persist(attempt, record, {"origin": "unverified_import", "receipt": None})

    def observe_command(
        self,
        attempt: str,
        *,
        command: str,
        args: Sequence[str],
        root: Path,
        requirement_ids: list[str],
        issue_key: str,
        test_id: str,
        spec_sha256: str,
        fixture_id: str,
        fixture_version: str,
        mode: str | None = None,
    ) -> Path:
        if command not in {"test-python", "test-web", "test-browser", "test-integration"}:
            raise EvidenceError("UNREGISTERED_OBSERVATION_COMMAND")
        # No executor injection or caller-supplied receipt. The owning supervisor
        # executes its finite registry route, validates nonce/inventory and cleanup.
        manifest = schema_validate.read_json(schema_validate.local_file(root, TARGET))
        if any(c.status != "PASS" for c in validate_manifest(manifest, root)):
            raise EvidenceError("UNQUALIFIED_TRACEABILITY_MANIFEST")
        owners = [i for i in manifest["source_projection"]["issues"] if i["issue_key"] == issue_key]
        bindings = [
            b
            for b in manifest["test_bindings"]
            if b["issue_key"] == issue_key
            and b["test_id"] == test_id
            and b["mode"] == mode
            and b["fixture_id"] == fixture_id
            and b["fixture_version"] == fixture_version
            and set(requirement_ids) <= set(b["requirement_ids"])
        ]
        if (
            len(owners) != 1
            or spec_sha256 != owners[0]["technical_capture_sha256"]
            or not requirement_ids
            or len(bindings) != 1
        ):
            raise EvidenceError("UNBOUND_REQUIREMENT_OR_TEST")
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", attempt):
            raise EvidenceError("INVALID_ATTEMPT_ID")
        existing = self.directory / f"{attempt}.json"
        if existing.exists() or existing.is_symlink():
            history = self.read(attempt)
            request = {
                "requirement_ids": requirement_ids,
                "issue_key": issue_key,
                "test_id": test_id,
                "spec_sha256": spec_sha256,
                "fixture_id": fixture_id,
                "fixture_version": fixture_version,
                "mode": mode,
                "command": [command, *args],
            }
            if history["provenance"]["origin"] != "observed_command" or any(
                history["record"][k] != v for k, v in request.items()
            ):
                raise EvidenceError("ATTEMPT_ID_CONTENT_CONFLICT")
            return existing
        receipt = test_runner.run_command(command, args, root)
        status, exit_code = receipt["status"], receipt["exit_code"]
        limitations = list(receipt["limitations"])
        if fixture_id != receipt["fixture_id"] or fixture_version != receipt["fixture_version"]:
            status, exit_code = "FAIL", 1
            limitations.append("FIXTURE_IDENTITY_MISMATCH")
        tests = [
            t
            for step in receipt["steps"]
            if isinstance(step.get("tests"), dict)
            for t in step["tests"].get("tests", [])
        ]
        selected = [
            t
            for t in tests
            if t["id"] == "sha256:" + hashlib.sha256(test_id.encode()).hexdigest()
            and t["file"] == test_id.split("::", 1)[0]
            and t["mode"] == mode
        ]
        if status == "PASS" and (len(selected) != 1 or selected[0]["status"] != "passed"):
            status, exit_code = "FAIL", 1
            limitations.append("REQUESTED_TEST_NOT_OBSERVED")
        if receipt["git_worktree_dirty"] or receipt["git_head_changed"]:
            limitations.append("DIRTY_OR_CHANGED_SOURCE")
        # Wrapper's C03 kind is local integration, but its scripted observations
        # remain simulated; this records the selected pytest case, not a provider fact.
        kind = receipt["kind"]
        if kind != bindings[0]["kind"] or kind not in {"simulated", "local_integration"}:
            status, exit_code = "BLOCKED", 3
            limitations.append("UNQUALIFIED_PROVIDER_VERIFIER")
        record = {
            "requirement_ids": requirement_ids,
            "issue_key": issue_key,
            "test_id": test_id,
            "commit_sha": receipt["commit_sha"] or "0" * 40,
            "spec_sha256": spec_sha256,
            "fixture_id": fixture_id,
            "fixture_version": fixture_version,
            "kind": kind,
            "mode": mode,
            "command": [command, *args],
            "started_at": receipt["started_at"],
            "finished_at": receipt["finished_at"],
            "exit_code": exit_code,
            "status": status,
            "output_digests": {"command_receipt": digest(receipt)},
            "limitations": limitations,
        }
        return self._persist(attempt, record, {"origin": "observed_command", "receipt": receipt})

    def missing_prerequisite(self, attempt: str, record: dict[str, Any]) -> Path:
        """No provider adapter is implemented here; an asserted signature is insufficient."""
        blocked = {
            **record,
            "status": "BLOCKED",
            "exit_code": 3,
            "limitations": [*record["limitations"], "UNQUALIFIED_PROVIDER_VERIFIER"],
        }
        return self._persist(attempt, blocked, {"origin": "missing_prerequisite", "receipt": None})

    def read(self, attempt: str) -> dict[str, Any]:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", attempt):
            raise EvidenceError("INVALID_ATTEMPT_ID")
        path = self.directory / f"{attempt}.json"
        if path.is_symlink() or path.stat().st_size > 8_000_000:
            raise EvidenceError("INVALID_HISTORY_FILE")
        value = schema_validate.read_json(path)
        closed(value, "schema_version attempt_id record record_sha256 provenance")
        if (
            value["schema_version"] != "opentour.evidence-history.v1"
            or value["attempt_id"] != attempt
            or value["record_sha256"] != digest(value["record"])
        ):
            raise EvidenceError("HISTORICAL_INTEGRITY_FAILURE")
        validate_record(value["record"])
        provenance = closed(value["provenance"], "origin receipt")
        if provenance["origin"] not in {
            "unverified_import",
            "observed_command",
            "missing_prerequisite",
        }:
            raise EvidenceError("INVALID_PROVENANCE")
        if provenance["origin"] == "observed_command":
            if not isinstance(provenance["receipt"], dict) or digest(
                provenance["receipt"]
            ) != value["record"]["output_digests"].get("command_receipt"):
                raise EvidenceError("HISTORICAL_RECEIPT_MISMATCH")
        return dict(value)

    def eligible(self, attempt: str, **policy: Any) -> dict[str, Any]:
        value = self.read(attempt)
        return eligibility(value["record"], **policy, _provenance=value["provenance"])


def gate_run(
    gate: str,
    evidence: Sequence[dict[str, Any]],
    *,
    commit: str,
    spec: str,
    required_checks: set[str],
    customer_mode: str | None = None,
) -> dict[str, Any]:
    """GateRun execution enum is separate from EvidenceRecord status.

    G1-G5 require actual qualification in both ownership modes; G6 uses the
    actual configured customer mode. There is no percentage-based shortcut.
    """
    if gate not in {"G1", "G2", "G3", "G4", "G5", "G6"} or not required_checks:
        raise EvidenceError("INVALID_GATE_REQUIREMENTS")
    if gate == "G6" and customer_mode not in MODES:
        raise EvidenceError("MISSING_CUSTOMER_MODE")
    modes = {str(customer_mode)} if gate == "G6" else MODES
    kind = "customer_evidence" if gate == "G6" else "provider_integration"
    missing = []
    for mode in sorted(modes):
        for check in sorted(required_checks):
            eligible = any(
                eligibility(
                    record,
                    commit=commit,
                    spec=spec,
                    required_kind=kind,
                    required_requirements={check},
                    test_id=record["test_id"],
                    mode=mode,
                )["eligible"]
                for record in evidence
            )
            if not eligible:
                missing.append({"mode": mode, "requirement_id": check})
    return {
        "gate": gate,
        "execution_status": "INCOMPLETE" if missing else "PASS",
        "product_acceptance": "NOT_RUN",
        "missing": missing,
    }


def parent_completion(
    mandatory_children: dict[str, str],
    accepted_children: dict[str, str],
    *,
    integration_eligible: bool,
) -> dict[str, Any]:
    missing = sorted(
        key for key, commit in mandatory_children.items() if accepted_children.get(key) != commit
    )
    return {
        "status": "PASS"
        if mandatory_children and not missing and integration_eligible
        else "BLOCKED",
        "missing_or_incompatible_children": missing,
        "integration_eligible": integration_eligible,
    }


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")
