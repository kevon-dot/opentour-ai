"""Generated qualification fixtures exercise only the private local register."""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.qualification import (
    AUTHORITY_VERSION,
    EVENT_VERSION,
    Authority,
    QualificationRegister,
)

KEY = bytes(range(32))
ORG = "generated-legal-organization"
SIGNER = "generated-signer"
MODULE = "opentour_cell.pilot.qualification"


@pytest.fixture
def directory(tmp_path: Path) -> Path:
    path = tmp_path / "private-cohort"
    path.mkdir(mode=0o700)
    return path


def authority(route: str = "manual", mapping_ref: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": AUTHORITY_VERSION,
        "organization_ref": ORG,
        "signer_ref": SIGNER,
        "route": route,
        "mapping_ref": mapping_ref,
        "expires_at": int(time.time()) + 600,
        "verification_key_hex": KEY.hex(),
    }


def event(**values: Any) -> dict[str, Any]:
    base = {
        "schema_version": EVENT_VERSION,
        "organization_ref": ORG,
        "event_id": "generated-event-1",
        "engagement_revision": 1,
        "source_revision": 1,
        "source_ref": "generated-charter-1",
        "signer_ref": SIGNER,
        "route": "manual",
        "mapping_ref": None,
        "intent": "starter",
        "charter": {
            "owner_ref": "generated-owner",
            "custodian_ref": "generated-custodian",
            "evaluation_purpose_ref": "generated-evaluation",
            "local_location_ref": "generated-local-location",
            "intended_decision_ref": "generated-decision",
            "policy_ref": "generated-policy",
            "policy_revision": "generated-policy-v1",
            "policy_grant_ref": "generated-policy-grant",
            "verifier_role_ref": "generated-verifier-role",
            "verifier_plan_owner_ref": "generated-verifier-plan-owner",
            "verifier_next_check": (datetime.now(UTC).date() + timedelta(days=7)).isoformat(),
            "verifier_ref": None,
        },
    }
    return {**base, **values}


def sign(value: dict[str, Any], key: bytes = KEY) -> dict[str, Any]:
    material = f"{EVENT_VERSION}\n{canonical(value)}".encode("ascii")
    return {"event": value, "signature": hmac.digest(key, material, "sha256").hex()}


def private_file(path: Path, value: dict[str, Any]) -> Path:
    path.touch(mode=0o600)
    path.write_text(canonical(value), encoding="ascii")
    return path


def history(path: Path) -> list[tuple[Any, ...]]:
    with closing(sqlite3.connect(path)) as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        assert tables == [("qualification_event",)]
        return db.execute(
            """SELECT organization_ref, event_id, engagement_revision, source_revision,
                      source_ref, observed_at, receipt FROM qualification_event
               ORDER BY engagement_revision, source_revision"""
        ).fetchall()


def test_manual_cli_delivery_loss_stale_reentry_and_private_evidence(directory: Path) -> None:
    register = directory / "cohort.sqlite3"
    policy = private_file(directory / "authority.json", authority())
    request = directory / "event.json"

    def invoke(value: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        private_file(request, sign(value))
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                MODULE,
                "--register",
                str(register),
                "--authority",
                str(policy),
                "--submission",
                str(request),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.stderr == ""
        return completed.returncode, json.loads(completed.stdout)

    starter = event()
    accepted = invoke(starter)
    assert accepted[0] == 0
    receipt = accepted[1]
    assert receipt["disposition"] == "qualified"
    assert invoke(starter) == invoke(starter) == accepted
    assert len(history(register)) == 1
    assert not register.stat().st_mode & 0o077

    lost = event(event_id="generated-lost", source_revision=2, intent="lost", charter=None)
    assert invoke(lost)[1]["disposition"] == "lost"
    assert invoke(starter)[1]["reason"] == "stale_qualification_event"
    same_engagement = event(event_id="generated-event-stale-new-id", source_revision=3)
    assert invoke(same_engagement)[1]["reason"] == "engagement_terminal"
    reentry = event(
        event_id="generated-event-2",
        source_ref="generated-charter-2",
        engagement_revision=2,
    )
    second = invoke(reentry)[1]
    assert second["disposition"] == "qualified"
    assert second["candidate_ref"] != receipt["candidate_ref"]
    rows = history(register)
    assert {row[0] for row in rows} == {ORG}
    assert [row[1:5] for row in rows] == [
        ("generated-event-1", 1, 1, "generated-charter-1"),
        ("generated-lost", 1, 2, "generated-charter-1"),
        ("generated-event-2", 2, 1, "generated-charter-2"),
    ]
    assert all(row[5].endswith("+00:00") for row in rows)
    assert [json.loads(row[6])["disposition"] for row in rows] == [
        "qualified",
        "lost",
        "qualified",
    ]
    assert reentry["source_ref"] not in canonical(second)


def test_three_concurrent_deliveries_create_one_candidate(directory: Path) -> None:
    register = directory / "cohort.sqlite3"
    credentials = Authority.parse(authority())

    def deliver(_: int) -> dict[str, Any]:
        return QualificationRegister(register).submit(sign(event()), credentials)

    with ThreadPoolExecutor(max_workers=3) as pool:
        receipts = list(pool.map(deliver, range(3)))
    assert all(result == receipts[0] for result in receipts)
    assert receipts[0]["disposition"] == "qualified"
    assert len(history(register)) == 1


@pytest.mark.parametrize(
    "envelope",
    [
        {"event": event()},
        sign(event(), hashlib.sha256(b"generated-forged-key").digest()),
        sign(event(organization_ref="generated-foreign-organization")),
        sign(event(signer_ref="generated-foreign-signer")),
        sign(event(route="crm", mapping_ref="generated-unbound-mapping")),
    ],
)
def test_unverified_charters_hold_without_creating_the_register(
    directory: Path, envelope: dict[str, Any]
) -> None:
    register = directory / "cohort.sqlite3"
    result = QualificationRegister(register).submit(envelope, Authority.parse(authority()))
    assert result["reason"] == "qualification_authority_unverified"
    assert set(result) == {"schema_version", "disposition", "reason"}
    assert not register.exists()


def test_expired_and_unsigned_discovery_have_no_register_effect(directory: Path) -> None:
    path = directory / "cohort.sqlite3"
    register = QualificationRegister(path)
    credentials = Authority.parse({**authority(), "expires_at": 1})
    assert register.submit(sign(event()), credentials)["disposition"] == "held"
    for intent in ("feedback", "research", "collateral", "interest"):
        assert (
            register.submit({"event": event(intent=intent, charter=None)}, credentials)[
                "disposition"
            ]
            == "discovery"
        )
    assert not path.exists()


def test_starter_next_check_must_be_current_on_first_admission(
    directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = directory / "cohort.sqlite3"
    register = QualificationRegister(path)
    now = int(datetime(2027, 1, 2, tzinfo=UTC).timestamp())
    monkeypatch.setattr("opentour_cell.pilot.qualification.time", SimpleNamespace(time=lambda: now))
    credentials = Authority.parse({**authority(), "expires_at": now + 3 * 86400})

    starter = event()
    starter["charter"]["verifier_next_check"] = "2027-01-01"
    refused = register.submit(sign(starter), credentials)
    assert refused["reason"] == "charter_incomplete"
    assert refused["missing_fields"] == ["verifier_next_check"]
    assert history(path) == []

    starter["charter"]["verifier_next_check"] = "2027-01-02"
    envelope = sign(starter)
    accepted = register.submit(envelope, credentials)
    assert accepted["disposition"] == "qualified"
    now += 86400
    assert register.submit(envelope, credentials) == accepted
    assert len(history(path)) == 1


def test_missing_starter_policy_grant_returns_field_level_gap(directory: Path) -> None:
    path = directory / "cohort.sqlite3"
    submission = event()
    del submission["charter"]["policy_grant_ref"]
    result = QualificationRegister(path).submit(sign(submission), Authority.parse(authority()))
    assert result["disposition"] == "held"
    assert result["missing_fields"] == ["policy_grant_ref"]
    assert not path.exists()


def test_historical_requires_independent_truth(directory: Path) -> None:
    path = directory / "cohort.sqlite3"
    starter = event()["charter"]
    charter = {
        name: starter[name]
        for name in (
            "owner_ref",
            "custodian_ref",
            "evaluation_purpose_ref",
            "local_location_ref",
            "intended_decision_ref",
        )
    }
    charter.update(
        decision_source_ref="generated-decision-feed",
        decision_revision="generated-decision-v1",
        decision_authority_ref="generated-decider",
        correction_source_ref="generated-decision-feed",
        correction_revision="generated-truth-v1",
        correction_authority_ref="generated-decider",
    )
    register = QualificationRegister(path)
    credentials = Authority.parse(authority())
    submission = event(intent="historical", charter=charter)
    assert register.submit(sign(submission), credentials)["missing_fields"] == [
        "correction_authority_ref",
        "correction_source_ref",
    ]
    assert not path.exists()
    charter.update(
        correction_source_ref="generated-truth-feed",
        correction_authority_ref="generated-independent-verifier",
    )
    assert register.submit(sign(submission), credentials)["mode"] == "historical"


def test_different_event_cannot_replace_same_source_or_skip_engagement(directory: Path) -> None:
    path = directory / "cohort.sqlite3"
    register = QualificationRegister(path)
    credentials = Authority.parse(authority())
    current = sign(event())
    assert register.submit(current, credentials)["disposition"] == "qualified"
    assert (
        register.submit(sign(event(source_ref="generated-different")), credentials)["reason"]
        == "event_identity_conflict"
    )
    assert (
        register.submit(sign(event(event_id="generated-different")), credentials)["reason"]
        == "source_revision_conflict"
    )
    skip = event(event_id="generated-skip", engagement_revision=3)
    assert register.submit(sign(skip), credentials)["reason"] == "engagement_reentry_unauthorized"
    assert len(history(path)) == 1


def test_crm_binding_is_separate_from_manual_signature_scope(directory: Path) -> None:
    path = directory / "cohort.sqlite3"
    register = QualificationRegister(path)
    submission = sign(event(route="crm", mapping_ref="generated-approved-map"))
    assert register.submit(submission, Authority.parse(authority()))["disposition"] == "held"
    crm = Authority.parse(authority("crm", "generated-approved-map"))
    assert register.submit(submission, crm)["disposition"] == "qualified"
    assert len(history(path)) == 1


def test_cli_rejects_broadly_readable_authority(directory: Path) -> None:
    register = directory / "cohort.sqlite3"
    policy = private_file(directory / "authority.json", authority())
    policy.chmod(0o644)
    request = private_file(directory / "event.json", sign(event()))
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            MODULE,
            "--register",
            str(register),
            "--authority",
            str(policy),
            "--submission",
            str(request),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert completed.stderr == "Qualification held: private register or input unavailable.\n"
    assert not register.exists()
