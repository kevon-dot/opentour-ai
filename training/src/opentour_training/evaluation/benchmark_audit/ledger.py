"""Append-only receipt ledger: archive, fence, successor and reconcile, with exact replay.

State is derived only from the event list, so a restart replays to the identical state and a
retried event key is a no-op. Superseded receipts stay readable as-of; they just cannot promote.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from opentour_training.evaluation.benchmark_audit.model import Verdict, canonical_digest

Event = dict[str, Any]


@dataclass(frozen=True)
class DependentEffect:
    effect_id: str
    receipt_id: str
    consumed: bool


class LedgerError(ValueError):
    pass


class AuditLedger:
    def __init__(self) -> None:
        self._events: list[Event] = []
        self._keys: set[str] = set()
        self.receipts: dict[str, dict[str, Any]] = {}
        self.status: dict[str, str] = {}
        self.successor: dict[str, str] = {}
        self.effects: dict[str, DependentEffect] = {}
        self.fenced: set[str] = set()
        self.reconciliation: dict[str, list[str]] = {}
        self.disclosures: dict[str, list[str]] = {}

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @classmethod
    def replay(cls, events: Iterable[Event]) -> AuditLedger:
        ledger = cls()
        for event in events:
            ledger._apply(event)
        return ledger

    def state_digest(self) -> str:
        return canonical_digest(
            {
                "status": self.status,
                "successor": self.successor,
                "fenced": sorted(self.fenced),
                "reconciliation": self.reconciliation,
                "disclosures": self.disclosures,
                "receipts": sorted(self.receipts),
            }
        )

    def _apply(self, event: Event) -> bool:
        key = str(event["key"])
        if key in self._keys:
            return False  # retried event
        self._keys.add(key)
        self._events.append(event)
        kind = event["kind"]
        if kind == "issued":
            receipt = event["receipt"]
            self.receipts[receipt["receipt_id"]] = receipt
            self.status.setdefault(receipt["receipt_id"], "current")
        elif kind == "effect_registered":
            self.effects[event["effect_id"]] = DependentEffect(
                event["effect_id"], event["receipt_id"], bool(event["consumed"])
            )
            if (
                self.status.get(event["receipt_id"]) == "archived_superseded"
                and not event["consumed"]
            ):
                self.fenced.add(event["effect_id"])
        elif kind == "disclosure":
            self.disclosures.setdefault(event["receipt_id"], []).extend(event["families"])
        elif kind == "superseded":
            old, new = event["old"], event["new"]
            self.status[old] = "archived_superseded"
            self.successor[old] = new
            for effect in self.effects.values():
                if effect.receipt_id == old and not effect.consumed:
                    self.fenced.add(effect.effect_id)
            self.reconciliation[old] = sorted(
                {e.effect_id for e in self.effects.values() if e.receipt_id == old and e.consumed}
                | {f"disclosure:{f}" for f in self.disclosures.get(old, [])}
            )
        else:
            raise LedgerError(f"unknown event kind {kind}")
        return True

    def issue(self, key: str, receipt: dict[str, Any]) -> bool:
        return self._apply({"key": key, "kind": "issued", "receipt": receipt})

    def register_effect(self, key: str, effect: DependentEffect) -> bool:
        return self._apply(
            {
                "key": key, "kind": "effect_registered", "effect_id": effect.effect_id,
                "receipt_id": effect.receipt_id, "consumed": effect.consumed,
            }
        )  # fmt: skip

    def log_disclosure(self, key: str, receipt_id: str, families: Iterable[str]) -> bool:
        return self._apply(
            {"key": key, "kind": "disclosure", "receipt_id": receipt_id,
             "families": sorted(families)}
        )  # fmt: skip

    def supersede(self, key: str, old_id: str, successor: dict[str, Any]) -> bool:
        """Archive the old verdict, fence unconsumed effects and issue the successor."""
        if old_id not in self.receipts:
            raise LedgerError("unknown receipt")
        applied = self.issue(f"{key}:issue", successor)
        return (
            self._apply(
                {"key": key, "kind": "superseded", "old": old_id, "new": successor["receipt_id"]}
            )
            or applied
        )

    def promotion_allowed(self, receipt_id: str) -> bool:
        receipt = self.receipts.get(receipt_id)
        return (
            receipt is not None
            and self.status.get(receipt_id) == "current"
            and receipt["verdict"]
            in (Verdict.EVALUATION_ALLOWED.value, Verdict.CLAIM_LIMITED.value)
        )

    def effect_runnable(self, effect_id: str) -> bool:
        return effect_id in self.effects and effect_id not in self.fenced
