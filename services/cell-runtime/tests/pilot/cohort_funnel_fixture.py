"""Generated sentinel register and stage receipts for the OPE-455 cohort funnel tests.

Every organization, person, record, key and evidence reference here is a generated sentinel.
The register is a real private qualification and lease register built through the signed
OPE-451 and OPE-452 entry points, so the funnel reads exactly what those routes store.
"""

from __future__ import annotations

import hmac
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opentour_cell.pilot.cohort_funnel import RECEIPT_VERSION
from opentour_cell.pilot.cohort_lease import (
    AUTHORITY_VERSION,
    COMMAND_VERSION,
    CohortLeaseRegister,
    LeaseAuthority,
)
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.qualification import (
    EVENT_VERSION,
    Authority,
    Json,
    QualificationRegister,
)

QUALIFICATION_KEY = bytes(range(32))
_ROLES = {"acquire": "operator", "begin": "operator", "reconcile": "reconciler", "close": "closer"}
_AUTHORITY_SECONDS = 7 * 86_400
LEASE_SECONDS = 3 * 86_400


def org(number: int) -> str:
    return f"generated-org-{number:02d}"


def private_register(root: Path) -> Path:
    directory = root / "private-cohort"
    directory.mkdir(mode=0o700)
    return directory / "register.sqlite3"


def qualify(
    path: Path,
    organization: str,
    *,
    intent: str = "starter",
    revision: int = 1,
    source_revision: int = 1,
    event_id: str | None = None,
) -> str:
    """Admit one signed qualification event and return its event digest."""
    charter = {
        "owner_ref": "generated-owner",
        "custodian_ref": "generated-custodian",
        "evaluation_purpose_ref": "generated-evaluation",
        "local_location_ref": "generated-private-location",
        "intended_decision_ref": "generated-decision",
        "policy_ref": "generated-policy",
        "policy_revision": "generated-policy-v1",
        "policy_grant_ref": "generated-policy-grant",
        "verifier_role_ref": "generated-verifier-role",
        "verifier_plan_owner_ref": "generated-verifier-plan-owner",
        "verifier_next_check": (datetime.now(UTC).date() + timedelta(days=7)).isoformat(),
    }
    event = {
        "schema_version": EVENT_VERSION,
        "organization_ref": organization,
        "event_id": event_id or f"generated-qualify-{revision}-{source_revision}",
        "engagement_revision": revision,
        "source_revision": source_revision,
        "source_ref": "generated-source",
        "signer_ref": "generated-qualification-operator",
        "route": "manual",
        "mapping_ref": None,
        "intent": intent,
        "charter": charter if intent == "starter" else None,
    }
    signature = hmac.digest(
        QUALIFICATION_KEY, f"{EVENT_VERSION}\n{canonical(event)}".encode("ascii"), "sha256"
    ).hex()
    authority = Authority(
        organization,
        "generated-qualification-operator",
        "manual",
        None,
        int(time.time()) + 300,
        QUALIFICATION_KEY,
    )
    result = QualificationRegister(path).submit({"event": event, "signature": signature}, authority)
    assert result["disposition"] in ("qualified", "lost"), result
    return digest(event)


def _key(organization: str, role: str) -> bytes:
    return bytes.fromhex(digest(["generated-funnel-key", organization, role]))


def _lease(path: Path, command: Json) -> Json:
    role = _ROLES[command["action"]]
    organization = command["organization_ref"]
    authority = LeaseAuthority.parse(
        {
            "schema_version": AUTHORITY_VERSION,
            "organization_ref": organization,
            "signer_ref": f"generated-manual-{role}",
            "route": "manual",
            "expires_at": int(time.time()) + _AUTHORITY_SECONDS,
            "verification_key_hex": _key(organization, role).hex(),
            "authorized_actions": [command["action"]],
        }
    )
    payload = f"{COMMAND_VERSION}\n{canonical(command)}".encode("ascii")
    signature = hmac.digest(_key(organization, role), payload, "sha256").hex()
    return CohortLeaseRegister(QualificationRegister(path)).submit(
        {"command": command, "signature": signature}, authority
    )


def _command(organization: str, action: str, **fields: Any) -> Json:
    return {
        "schema_version": COMMAND_VERSION,
        "event_id": f"generated-{action}",
        "action": action,
        "organization_ref": organization,
        "engagement_revision": 1,
        "generation": 0,
        "slot": 1,
        "signer_ref": f"generated-manual-{_ROLES[action]}",
        "route": "manual",
        "evidence_digest": digest(["generated-evidence", organization, action]),
        **fields,
    }


def acquire(
    path: Path,
    organization: str,
    qualification_digest: str,
    *,
    generation: int = 0,
    engagement: int = 1,
    event_id: str = "generated-acquire",
    expires_in: int = LEASE_SECONDS,
) -> Json:
    return _lease(
        path,
        _command(
            organization,
            "acquire",
            event_id=event_id,
            engagement_revision=engagement,
            generation=generation,
            qualification_digest=qualification_digest,
            expires_at=int(time.time()) + expires_in,
        ),
    )


def close(
    path: Path, organization: str, lease: Json, *, engagement: int = 1, kind: str = "closeout"
) -> Json:
    return _lease(
        path,
        _command(
            organization,
            "close",
            event_id=f"generated-{kind}-{engagement}",
            engagement_revision=engagement,
            generation=lease["generation"],
            lease_ref=lease["lease_ref"],
            terminal_kind=kind,
            reconciliation_revision=lease["reconciliation_revision"],
            reconciliation_digest=lease["reconciliation_digest"],
        ),
    )


_DEFAULT_FACTS: dict[str, Json] = {
    "ready_to_result": {"customer_wait_seconds": None, "operator_setup_seconds": None},
    "starter_readout": {
        "readout_revision_ref": "generated-starter-readout-1",
        "benchmark_state": "frozen_unscored",
        "compute_seconds": None,
        "limit_exceeded": None,
        "cost": None,
    },
    "real_case_evaluation": {
        "readout_revision_ref": "generated-real-readout-1",
        "population": "real_permitted",
        "comparison": "rule_only",
        "witness_ref": "generated-recipient",
        "compute_seconds": None,
        "fit_count": None,
        "limit_exceeded": None,
        "cost": None,
    },
    "exact_fit": {
        "fit_job_ref": "generated-fit-job",
        "artifact_digest": digest("generated-adapter"),
    },
    "owner_decision": {
        "readout_revision_ref": "generated-real-readout-1",
        "decided_on": "2026-11-28",
        "owner_ref": "generated-workflow-owner",
        "outcome": "continue",
        "stop_reason_ref": None,
    },
    "delivery": {
        "artifact_digest": digest("generated-repair"),
        "destination_receipt_ref": "generated-destination-receipt",
    },
    "payment": {
        "continuation_ref": "generated-signed-continuation",
        "payment_evidence_ref": "generated-payment-evidence",
        "recurring": True,
    },
    "fee_waiver": {"status": "proposed", "terms_ref": "generated-waiver-terms"},
    "correction": {
        "target_receipt_id": "generated-target",
        "target_revision": 1,
        "kind": "disputed",
        "reason_ref": "generated-dispute-reason",
    },
}


class Clock:
    """Receipt instants relative to the register's real admission time."""

    def __init__(self) -> None:
        self.base = int(time.time())

    def at(self, offset: int) -> int:
        return self.base + offset

    @property
    def as_of(self) -> int:
        return self.base + 86_400

    @property
    def day(self) -> str:
        return datetime.fromtimestamp(self.as_of, UTC).date().isoformat()


def receipt(
    stage: str,
    organization: str,
    receipt_id: str,
    *,
    at: int,
    revision: int = 1,
    engagement: int = 1,
    recorded: int | None = None,
    tier: str = "locally_keyed",
    setup: str = "generated",
    source: str | None = None,
    **facts: Any,
) -> Json:
    body = {**_DEFAULT_FACTS[stage], **facts}
    if stage == "owner_decision" and "decided_on" not in facts:
        body["decided_on"] = datetime.fromtimestamp(at, UTC).date().isoformat()
    return {
        "schema_version": RECEIPT_VERSION,
        "receipt_id": receipt_id,
        "revision": revision,
        "supersedes_revision": revision - 1 if revision > 1 else None,
        "stage": stage,
        "organization_ref": organization,
        "engagement_revision": engagement,
        "recorded_at": at if recorded is None else recorded,
        "effective_at": at,
        "evidence_digest": digest(["generated-stage-evidence", receipt_id, revision]),
        "evidence_tier": tier,
        "setup_kind": setup,
        "source_record_ref": source,
        "facts": body,
    }


def cost(value: str, *, currency: str = "USD", basis: str = "observed") -> Json:
    return {"amount": value, "currency": currency, "basis": basis}


def readiness_receipt(readout: Json, *, setup: str = "generated") -> Json:
    """Wrap an OPE-522 ``CohortReadout`` (its ``asdict`` JSON form) in the consumed envelope."""
    return {
        "schema_version": "opentour.pilot.readiness-cohort-receipt.v1",
        "setup_kind": setup,
        "readout": readout,
        "revision": digest(readout),
    }


def ope522_readout(**changes: Any) -> Json:
    """The OPE-522 acceptance fixture: 2/5 ready at 1h and 3h, one rule, two held, one starter."""
    return {
        "schema_version": "opentour.pilot.readiness-cohort.v1",
        "window_start": 1_000,
        "window_end": 90_000,
        "cutoff": 100_000,
        "source_revision": digest("generated-readiness-evidence"),
        "workflows": 5,
        "ever_fit_ready": 2,
        "current": [["fit_ready", 2], ["held", 2], ["simple_resolved", 1]],
        "policy_only": 1,
        "unknown_seed": 0,
        "median_seconds_to_ready": 7200.0,
        "pending_ages_seconds": [40_000, 60_000],
        **changes,
    }
