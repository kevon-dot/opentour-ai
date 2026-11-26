"""Admit signed qualification references into a private cohort register."""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import stat
import sys
import time
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from opentour_cell.pilot.operating_point import canonical, digest

EVENT_VERSION = "opentour.pilot.qualification-event.v1"
AUTHORITY_VERSION = "opentour.pilot.qualification-authority.v1"
RECEIPT_VERSION = "opentour.pilot.qualification-receipt.v1"
_DISCOVERY = frozenset({"feedback", "research", "collateral", "interest"})
_MODES = frozenset({"starter", "historical"})
_EVENT_FIELDS = {
    "schema_version",
    "organization_ref",
    "engagement_revision",
    "event_id",
    "source_revision",
    "source_ref",
    "signer_ref",
    "route",
    "mapping_ref",
    "intent",
    "charter",
}
_CHARTER_FIELDS = {
    "owner_ref",
    "custodian_ref",
    "evaluation_purpose_ref",
    "local_location_ref",
    "intended_decision_ref",
}
_STARTER_FIELDS = {
    "policy_ref",
    "policy_revision",
    "policy_grant_ref",
    "verifier_role_ref",
    "verifier_plan_owner_ref",
    "verifier_next_check",
}
_HISTORICAL_FIELDS = {
    "decision_source_ref",
    "decision_revision",
    "decision_authority_ref",
    "correction_source_ref",
    "correction_revision",
    "correction_authority_ref",
}
_AUTHORITY_FIELDS = {
    "schema_version",
    "organization_ref",
    "signer_ref",
    "route",
    "mapping_ref",
    "expires_at",
    "verification_key_hex",
}
_MAX_BYTES = 65536
_SCHEMA = """
CREATE TABLE IF NOT EXISTS qualification_event (
    organization_ref TEXT NOT NULL,
    engagement_revision INTEGER NOT NULL,
    event_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    source_ref TEXT NOT NULL,
    signer_ref TEXT NOT NULL,
    route TEXT NOT NULL,
    mapping_ref TEXT,
    event_digest TEXT NOT NULL,
    observed_at TEXT NOT NULL,
    receipt TEXT NOT NULL,
    PRIMARY KEY (organization_ref, event_id),
    UNIQUE (organization_ref, engagement_revision, source_revision)
);
"""

Json = dict[str, Any]


class QualificationError(ValueError):
    """A private input or register could not be used."""


def _ref(value: object) -> bool:
    return (
        isinstance(value, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value) is not None
    )


def _revision(value: object) -> bool:
    return type(value) is int and 1 <= value <= 2**63 - 1


def _routed(route: object, mapping: object) -> bool:
    return (route == "manual" and mapping is None) or (route == "crm" and _ref(mapping))


@dataclass(frozen=True)
class Authority:
    organization_ref: str
    signer_ref: str
    route: str
    mapping_ref: str | None
    expires_at: int
    key: bytes = field(repr=False)

    @classmethod
    def parse(cls, value: Json) -> Authority:
        raw_key = value.get("verification_key_hex")
        if (
            set(value) != _AUTHORITY_FIELDS
            or value["schema_version"] != AUTHORITY_VERSION
            or not _ref(value["organization_ref"])
            or not _ref(value["signer_ref"])
            or not _routed(value["route"], value["mapping_ref"])
            or not _revision(value["expires_at"])
            or not isinstance(raw_key, str)
            or re.fullmatch(r"[0-9a-f]{64}", raw_key) is None
        ):
            raise QualificationError("invalid_qualification_authority")
        return cls(
            value["organization_ref"],
            value["signer_ref"],
            value["route"],
            value["mapping_ref"],
            value["expires_at"],
            bytes.fromhex(raw_key),
        )

    def verifies(self, event: Json, signature: object, now: int) -> bool:
        return (
            now < self.expires_at
            and len(self.key) == 32
            and (
                event["organization_ref"],
                event["signer_ref"],
                event["route"],
                event["mapping_ref"],
            )
            == (self.organization_ref, self.signer_ref, self.route, self.mapping_ref)
            and isinstance(signature, str)
            and re.fullmatch(r"[0-9a-f]{64}", signature) is not None
            and hmac.compare_digest(signature, _event_signature(event, self.key))
        )


def _event_signature(event: Json, key: bytes) -> str:
    payload = (EVENT_VERSION + "\n" + canonical(event)).encode("ascii")
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def _event(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _EVENT_FIELDS:
        return False
    intent = value["intent"]
    if not isinstance(intent, str) or intent not in _MODES | _DISCOVERY | {"lost"}:
        return False
    return (
        value["schema_version"] == EVENT_VERSION
        and all(
            _ref(value[name])
            for name in ("organization_ref", "event_id", "source_ref", "signer_ref")
        )
        and _revision(value["engagement_revision"])
        and _revision(value["source_revision"])
        and _routed(value["route"], value["mapping_ref"])
        and (value["charter"] is None or isinstance(value["charter"], dict))
        and (intent in _MODES or value["charter"] is None)
    )


def _date(value: object) -> bool:
    if not isinstance(value, str) or re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _charter_gaps(event: Json) -> list[str]:
    charter = event["charter"] or {}
    starter = event["intent"] == "starter"
    required = _CHARTER_FIELDS | (_STARTER_FIELDS if starter else _HISTORICAL_FIELDS)
    supported = required | ({"verifier_ref"} if starter else set())
    if set(charter) - supported:
        return ["charter"]
    gaps = {
        key
        for key in required
        if not (_date(charter.get(key)) if key == "verifier_next_check" else _ref(charter.get(key)))
    }
    if starter and charter.get("verifier_ref") is not None and not _ref(charter["verifier_ref"]):
        gaps.add("verifier_ref")
    if not starter and not gaps:
        if charter["decision_source_ref"] == charter["correction_source_ref"]:
            gaps.add("correction_source_ref")
        if charter["decision_authority_ref"] == charter["correction_authority_ref"]:
            gaps.add("correction_authority_ref")
    return sorted(gaps)


def _receipt(disposition: str, reason: str, **fields: object) -> Json:
    return {
        "schema_version": RECEIPT_VERSION,
        "disposition": disposition,
        "reason": reason,
        **fields,
    }


def _private_parent(path: Path) -> None:
    if not path.is_absolute():
        raise QualificationError("absolute_private_path_required")
    metadata = path.parent.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
    ):
        raise QualificationError("private_qualification_directory_required")


def _private_record(metadata: os.stat_result) -> None:
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or metadata.st_mode & 0o077
        or metadata.st_nlink != 1
    ):
        raise QualificationError("private_qualification_file_required")


def _json_object(entries: list[tuple[str, Any]]) -> Json:
    result: Json = {}
    for key, value in entries:
        if key in result:
            raise QualificationError("ambiguous_qualification_input")
        result[key] = value
    return result


def _private_json(path: Path) -> Json:
    _private_parent(path)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
    with os.fdopen(os.open(path, flags), "rb") as handle:
        _private_record(os.fstat(handle.fileno()))
        content = handle.read(_MAX_BYTES + 1)
    if len(content) > _MAX_BYTES:
        raise QualificationError("qualification_input_too_large")
    value = json.loads(content, object_pairs_hook=_json_object)
    if not isinstance(value, dict):
        raise QualificationError("invalid_qualification_input")
    return value


class QualificationRegister:
    """One owner-local ledger for qualification, without live action capabilities."""

    def __init__(self, path: Path):
        _private_parent(path)
        self.path = path

    def submit(self, envelope: Mapping[str, Any], authority: Authority) -> Json:
        event = envelope.get("event")
        signature = envelope.get("signature")
        if set(envelope) - {"event", "signature"} or not _event(event):
            return _receipt("held", "invalid_qualification_event")
        assert isinstance(event, dict)
        intent = event["intent"]
        if signature is None and intent in _DISCOVERY:
            return _receipt("discovery", "discovery_only")
        now = int(time.time())
        try:
            verified = authority.verifies(event, signature, now)
        except (ValueError, RecursionError):
            verified = False
        if not verified:
            return _receipt("held", "qualification_authority_unverified")
        if intent in _DISCOVERY:
            return _receipt("discovery", "discovery_only")
        if intent in _MODES and (missing := _charter_gaps(event)):
            return _receipt("held", "charter_incomplete", missing_fields=missing)
        return self._record(event, now)

    def _record(self, event: Json, now: int) -> Json:
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        try:
            os.close(os.open(self.path, flags, 0o600))
        except FileExistsError:
            pass
        _private_record(self.path.lstat())
        with closing(sqlite3.connect(self.path, timeout=5)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN IMMEDIATE")
            with db:
                db.execute(_SCHEMA)
                return self._admit(db, event, now)

    def _admit(self, db: sqlite3.Connection, event: Json, now: int) -> Json:
        org = event["organization_ref"]
        revision = event["engagement_revision"]
        source_revision = event["source_revision"]
        event_digest = digest(event)
        operation_ref = "qualification-" + event_digest
        current = db.execute(
            """SELECT * FROM qualification_event WHERE organization_ref = ?
               ORDER BY engagement_revision DESC, source_revision DESC LIMIT 1""",
            (org,),
        ).fetchone()
        prior = db.execute(
            "SELECT * FROM qualification_event WHERE organization_ref = ? AND event_id = ?",
            (org, event["event_id"]),
        ).fetchone()
        if prior is not None:
            if prior["event_digest"] != event_digest:
                return _receipt("held", "event_identity_conflict")
            if current is not None and current["event_id"] == prior["event_id"]:
                result: Json = json.loads(prior["receipt"])
                return result
            return _receipt("held", "stale_qualification_event")
        if current is None:
            if revision != 1 or event["intent"] == "lost":
                return _receipt("held", "engagement_open_missing")
        else:
            current_revision = current["engagement_revision"]
            current_source = current["source_revision"]
            current_disposition = json.loads(current["receipt"])["disposition"]
            if revision < current_revision or (
                revision == current_revision and source_revision < current_source
            ):
                return _receipt("held", "stale_qualification_event")
            if revision == current_revision and source_revision == current_source:
                return _receipt("held", "source_revision_conflict")
            if revision == current_revision and current_disposition == "lost":
                return _receipt("held", "engagement_terminal")
            if revision > current_revision and (
                revision != current_revision + 1
                or current_disposition != "lost"
                or event["intent"] == "lost"
            ):
                return _receipt("held", "engagement_reentry_unauthorized")
        if event["intent"] == "starter":
            next_check = date.fromisoformat(event["charter"]["verifier_next_check"])
            if next_check < datetime.fromtimestamp(now, UTC).date():
                return _receipt(
                    "held", "charter_incomplete", missing_fields=["verifier_next_check"]
                )
        if event["intent"] == "lost":
            result = _receipt("lost", "engagement_terminal", operation_ref=operation_ref)
        else:
            result = _receipt(
                "qualified",
                "evaluation_candidate",
                operation_ref=operation_ref,
                candidate_ref="candidate-" + digest([org, revision]),
                mode=event["intent"],
                engagement_revision=revision,
            )
        db.execute(
            """INSERT INTO qualification_event
               (organization_ref, engagement_revision, event_id, source_revision, source_ref,
                signer_ref, route, mapping_ref, event_digest, observed_at, receipt)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                org,
                revision,
                event["event_id"],
                source_revision,
                event["source_ref"],
                event["signer_ref"],
                event["route"],
                event["mapping_ref"],
                event_digest,
                datetime.fromtimestamp(now, UTC).isoformat(),
                canonical(result),
            ),
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--register", required=True, type=Path)
    parser.add_argument("--authority", required=True, type=Path)
    parser.add_argument("--submission", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        authority = Authority.parse(_private_json(args.authority))
        register = QualificationRegister(args.register)
        result = register.submit(_private_json(args.submission), authority)
    except (OSError, ValueError, RecursionError, sqlite3.Error):
        print("Qualification held: private register or input unavailable.", file=sys.stderr)
        return 1
    print(canonical(result))
    return int(result["disposition"] == "held")


if __name__ == "__main__":
    raise SystemExit(main())
