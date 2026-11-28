"""Generated sentinel fixtures for the OPE-416 candidate-snapshot tests.

Fixture kind: generated (local). Every tenant, case, actor and value below is synthetic. Nothing
built from this module is provider, installed-Zap, Slack or customer evidence.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from opentour_cell.pilot.candidate_snapshot import SnapshotProducer
from opentour_cell.pilot.consumed_dependencies import DependencyRecorder
from opentour_cell.pilot.learning_profile import (
    Principal,
    SettingsBatchStore,
    SourceCapture,
    SourcePair,
    format_time,
)

TENANT_A = "generated-tenant-a"
TENANT_B = "generated-tenant-b"
WF = "generated-wf-refund"
LIVE, STARTER, OUTCOMES = "generated-dec-live", "generated-dec-starter", "generated-outcomes"
VERIFIER = "generated-verifier-1"
CUSTODIAN = Principal("generated-custodian-1", "data_custodian")
BASE = datetime(2026, 1, 1, tzinfo=UTC)
HELD_OUT = "generated-cust-held"

# Sentinels that must never leave the customer-local records.
PROTECTED_SENTINEL = "SENTINEL-PROTECTED-AGE-47"
LABEL_SENTINEL = "SENTINEL-LABEL-FREIGHT"


def at(hour: int, minute: int = 0, day: int = 0) -> str:
    return format_time(BASE + timedelta(days=day, hours=hour, minutes=minute))


def settings(tenant: str = TENANT_A, revision: int = 1, **over: Any) -> dict[str, Any]:
    raw: dict[str, Any] = {
        "schema": "opentour.pilot.learning-profile-settings.v1",
        "tenant": tenant,
        "workflow_ref": WF,
        "settings_revision": revision,
        "policy_revision": f"generated-policy-{revision}",
        "evaluation_policy_revision": "generated-eval-policy-1",
        "benchmark_ref": "generated-bench-1",
        "benchmark_version": 1,
        "engagement_epoch": 1,
        "grant_epoch": 3,
        "sources": [
            {"source_ref": LIVE, "role": "decision", "origin": "observed_decision"},
            {"source_ref": STARTER, "role": "decision", "origin": "starter_generated"},
            {"source_ref": OUTCOMES, "role": "correction", "origin": "independent_outcome"},
        ],
        "mapping": {
            "decision": {
                "decision_id": "ticket",
                "decision_revision": "ticket_rev",
                "case": "case_no",
                "entity": "customer",
                "group": "thread",
                "decision_time": "decided_at",
                "prediction": "action",
                "features_available_at": "inputs_at",
                "row_state": "state",
                "features": {"amount": "amount_text", "gateway_status": "gateway"},
            },
            "correction": {
                "correction_id": "cid",
                "decision_id": "ticket",
                "decision_revision": "ticket_rev",
                "case": "case_no",
                "correction_version": "ver",
                "corrected_value": "final_action",
                "status": "status",
                "provenance": "how",
                "verifier": "by",
                "correction_time": "at",
                "row_state": "state",
            },
        },
        "feature_allowlist": ["amount", "gateway_status"],
        "adjudicators": [{"actor_id": VERIFIER, "role": "verifier"}],
        "abstain_values": ["REVIEW"],
        "freshness_seconds": 86400,
        "maturity_seconds": 600,
        "split": {"mode": "heldout_customer", "customers": [HELD_OUT]},
        "uses": ["evaluate", "inspect", "train"],
        "receipt_counters": ["eligible", "pending", "quarantined", "disputed"],
    }
    for key, value in over.items():
        raw[key] = value
    return raw


def dec(
    ticket: str,
    decided: str,
    *,
    action: str = "DENY",
    rev: int = 1,
    case: str | None = None,
    customer: str = "generated-cust-1",
    thread: str | None = None,
    gateway: str = "ok",
    inputs_at: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "ticket": ticket,
        "ticket_rev": rev,
        "case_no": case or f"case-{ticket}",
        "customer": customer,
        "thread": thread or f"thr-{ticket}",
        "decided_at": decided,
        "action": action,
        "inputs_at": inputs_at or decided,
        "amount_text": f"amt {ticket}",
        "gateway": gateway,
        "state": "admitted",
        # Present in the source, never allowlisted: it must never be consumed or disclosed.
        "customer_age": PROTECTED_SENTINEL,
    }
    row.update(extra)
    return row


def cor(
    cid: str,
    ticket: str,
    when: str,
    *,
    value: str | None = "REFUND",
    ver: int = 1,
    rev: int = 1,
    case: str | None = None,
    status: str = "final",
    how: str = "independent_adjudication",
    by: str | None = VERIFIER,
    **extra: Any,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "cid": cid,
        "ticket": ticket,
        "ticket_rev": rev,
        "case_no": case or f"case-{ticket}",
        "ver": ver,
        "final_action": value,
        "status": status,
        "how": how,
        "by": by,
        "at": when,
        "state": "admitted",
    }
    row.update(extra)
    return row


def pair(
    tenant: str = TENANT_A,
    *,
    live: Iterable[Mapping[str, Any]] = (),
    starter: Iterable[Mapping[str, Any]] = (),
    outcomes: Iterable[Mapping[str, Any]] = (),
    captured: str | None = None,
    revision: int = 1,
) -> SourcePair:
    when = captured or at(12)
    return SourcePair(
        (
            SourceCapture(tenant, LIVE, revision, when, tuple(copy.deepcopy(list(live)))),
            SourceCapture(tenant, STARTER, revision, when, tuple(copy.deepcopy(list(starter)))),
            SourceCapture(tenant, OUTCOMES, revision, when, tuple(copy.deepcopy(list(outcomes)))),
        )
    )


class Clock:
    def __init__(self, moment: str) -> None:
        self.set(moment)

    def set(self, moment: str) -> None:
        self.now = datetime.strptime(moment, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


@dataclass
class World:
    root: Path
    clock: Clock
    store: SettingsBatchStore
    producer: SnapshotProducer
    recorder: DependencyRecorder


def world(root: Path, moment: str | None = None) -> World:
    clock = Clock(moment or at(12))
    store = SettingsBatchStore(root / "settings")
    producer = SnapshotProducer(root / "snapshots", store, clock=clock)
    recorder = DependencyRecorder(root / "dependencies", producer, clock=clock)
    return World(root, clock, store, producer, recorder)


def reader(source: SourcePair) -> Any:
    return lambda _settings: source
