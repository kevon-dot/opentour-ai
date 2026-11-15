"""Closed deterministic fixture registry; only generated simulated identities."""

from __future__ import annotations

import hashlib
import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from tools import schema_validate
from tools.fixtures.ports import Mode, Scope

MODES: tuple[Mode, ...] = ("customer_controlled", "opentour_managed")
ISSUER = "simulated:opentour:c03"
FIXTURE_VERSION = "1"
FAULTS = (
    "before_local_commit",
    "after_local_commit",
    "provider_accept_before_ack",
    "duplicate_event",
    "out_of_order_event",
    "lost_heartbeat",
    "expired_guard",
    "delayed_source_outcome",
    "unknown_membership",
    "unavailable_journal_head",
    "missing_ledger_sequence",
    "cleanup_failure",
    "duplicate_provider_acceptance",
    "cross_tenant",
    "revoked_epoch",
    "late_receipt",
    "normal",
)


def digest(value: Any) -> str:
    """Stable fixture serialization only, never production RFC8785 signing."""
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()


@dataclass
class Clock:
    instant: datetime

    @classmethod
    def from_wire(cls, value: str) -> Clock:
        if not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", value):
            raise ValueError("Fixture clock must be an explicit UTC millisecond timestamp")
        return cls(datetime.fromisoformat(value))

    def now(self) -> str:
        return (
            self.instant.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )

    def advance(self, seconds: int) -> None:
        if type(seconds) is not int or seconds < 0:
            raise ValueError("Clock steps must be nonnegative integer seconds")
        self.instant += timedelta(seconds=seconds)


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    version: str
    seed: int
    clock: str
    scope: Scope
    provider_behavior_script: tuple[str, ...]
    expected_effects: tuple[str, ...]
    expected_ledger_entries: tuple[str, ...]

    def order(self, values: list[str]) -> list[str]:
        shuffled = values.copy()
        random.Random(self.seed).shuffle(shuffled)
        return shuffled


def load_registry(path: Path) -> tuple[Fixture, ...]:
    document = schema_validate.read_json(path)
    if not isinstance(document, list) or not document:
        raise ValueError("Fixture registry must be a nonempty array")
    results = []
    seen: set[tuple[str, str, str]] = set()
    fields = {
        "fixture_id",
        "version",
        "seed",
        "clock",
        "organization_id",
        "environment_id",
        "cell_id",
        "mode",
        "provider_behavior_script",
        "expected_effects",
        "expected_ledger_entries",
    }
    for value in document:
        if not isinstance(value, dict) or set(value) != fields:
            raise ValueError("Invalid closed fixture fields")
        if any(
            not isinstance(value[k], str) or not re.fullmatch(r"[a-z0-9:_-]{1,256}", value[k])
            for k in ("fixture_id", "organization_id", "environment_id", "cell_id")
        ):
            raise ValueError("Invalid fixture identity")
        if any(
            not value[k].startswith("simulated:")
            for k in ("organization_id", "environment_id", "cell_id")
        ):
            raise ValueError("Fixture identities must be explicitly simulated")
        if value["version"] != FIXTURE_VERSION or value["mode"] not in MODES:
            raise ValueError("Unsupported fixture version or deployment mode")
        if type(value["seed"]) is not int or not 0 <= value["seed"] <= 9007199254740991:
            raise ValueError("Invalid recorded random seed")
        if not isinstance(value["clock"], str):
            raise ValueError("Invalid fixture clock")
        Clock.from_wire(value["clock"])
        for name in ("provider_behavior_script", "expected_effects", "expected_ledger_entries"):
            if not isinstance(value[name], list) or any(
                not isinstance(x, str) for x in value[name]
            ):
                raise ValueError("Fixture scripts and expectations must be string arrays")
        if not value["provider_behavior_script"] or any(
            step not in FAULTS for step in value["provider_behavior_script"]
        ):
            raise ValueError("Unregistered scripted fault")
        key = (value["fixture_id"], value["version"], value["mode"])
        if key in seen:
            raise ValueError("Duplicate fixture identity/version/mode")
        seen.add(key)
        results.append(
            Fixture(
                value["fixture_id"],
                value["version"],
                value["seed"],
                value["clock"],
                Scope(
                    value["organization_id"],
                    value["environment_id"],
                    value["cell_id"],
                    cast(Mode, value["mode"]),
                ),
                tuple(value["provider_behavior_script"]),
                tuple(value["expected_effects"]),
                tuple(value["expected_ledger_entries"]),
            )
        )
    return tuple(results)


def generated_scope(mode: Mode, tenant: int = 1) -> Scope:
    if mode not in MODES or type(tenant) is not int or tenant < 1:
        raise ValueError("Invalid generated tenant configuration")
    prefix = f"simulated:{mode}:tenant-{tenant}"
    return Scope(prefix, prefix + ":environment", prefix + ":cell", mode)
