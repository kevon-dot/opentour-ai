"""Worker-local scheduled approval polling; no hosted transport or notification delivery."""

from __future__ import annotations

import argparse
import json
import secrets
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.zapier_approval import (
    REQ,
    ApprovalBridge,
    ApprovalError,
    Json,
    ReconciliationJournal,
    require,
)

SCHEMA = "opentour.approval-reconciler.v1"


class _BoundBridge(ApprovalBridge):
    """Restrict existing local operations to this tick's actual installed scope."""

    def __init__(self, bridge: ApprovalBridge, expected: Json, deadline: float):
        super().__init__(bridge.directory, bridge.organization, bridge.config)
        self.expected = expected
        self.deadline = deadline

    @contextmanager
    def _serialized(
        self, *, expected: Json | None = None, deadline: float | None = None
    ) -> Iterator[ReconciliationJournal]:
        require(expected is None or expected == self.expected, "reconciliation_binding_changed")
        require(deadline is None or deadline == self.deadline, "invalid_reconciliation_deadline")
        with super()._serialized(expected=self.expected, deadline=self.deadline) as journal:
            yield journal


class ApprovalReconciler:
    """Persist paging in the already-accounted-for private approval ledger.

    One bridge writer spans a committed progress claim, the actual reconciliation
    core and its ACK. SQLite transactions are never held across the core call.
    Different units release the writer for callbacks, cutover and other pollers.
    """

    def __init__(
        self,
        bridge: ApprovalBridge,
        *,
        installation_epoch: str,
        enabled: bool = False,
        poll_interval: int = 60,
        reminder_interval: int = 3600,
        page_size: int = 25,
        max_pages: int = 4,
        max_requests: int = 50,
        tick_seconds: int = 10,
    ):
        require(type(enabled) is bool, "invalid_schedule_configuration")
        limits = {
            "poll_interval": (poll_interval, 86400),
            "reminder_interval": (reminder_interval, 86400),
            "page_size": (page_size, 50),
            "max_pages": (max_pages, 50),
            "max_requests": (max_requests, 1000),
            "tick_seconds": (tick_seconds, 300),
        }
        require(
            all(type(value) is int and 1 <= value <= maximum for value, maximum in limits.values()),
            "invalid_schedule_configuration",
        )
        self.bridge = bridge
        self.epoch = installation_epoch
        self.enabled = enabled
        self.settings = {name: value for name, (value, _) in limits.items()}

    def _load(self, journal: ReconciliationJournal, scope: str, configuration: str) -> Json:
        with journal.db() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS approval_schedule_progress "
                "(scope TEXT PRIMARY KEY,configuration TEXT NOT NULL,document TEXT NOT NULL)"
            )
            row = db.execute(
                "SELECT configuration,document FROM approval_schedule_progress WHERE scope=?",
                (scope,),
            ).fetchone()
            if row is not None:
                require(row[0] == configuration, "schedule_configuration_changed")
                value: Json = json.loads(row[1])
                return value
            initial: Json = {
                "sequence": 0,
                "cursor": None,
                "pending": [],
                "next_cursor": None,
                "page_loaded": False,
                "active": None,
                "sweep_ref": None,
                "started_at": None,
                "completed_at": None,
                "next_sweep_at": 0,
                "observed_requests": 0,
                "acknowledged_requests": 0,
            }
            db.execute(
                "INSERT INTO approval_schedule_progress VALUES (?,?,?)",
                (scope, configuration, canonical(initial)),
            )
            return initial

    def _save(self, journal: ReconciliationJournal, scope: str, prior: Json, changes: Json) -> Json:
        updated = {**prior, **changes, "sequence": prior["sequence"] + 1}
        with journal.db() as db:
            changed = db.execute(
                "UPDATE approval_schedule_progress SET document=? WHERE scope=? AND document=?",
                (canonical(updated), scope, canonical(prior)),
            ).rowcount
            require(changed == 1, "schedule_progress_changed")
        return updated

    def tick(self) -> Json:
        counters = {"pages": 0, "attempts": 0, "acknowledged": 0}
        document: Json | None = None

        def result(state: str, *, completed: bool = False) -> Json:
            return {
                "schema_version": SCHEMA,
                "state": state,
                "tenant_ref": digest(self.bridge.organization),
                "installation_epoch": self.epoch,
                **counters,
                "coverage": {
                    "kind": "local_request_inventory",
                    "sweep_ref": document["sweep_ref"] if document else None,
                    "started_at": document["started_at"] if document else None,
                    "completed_at": document["completed_at"] if document else None,
                    "completed_this_tick": completed,
                    "observed_requests": document["observed_requests"] if document else 0,
                    "acknowledged_requests": document["acknowledged_requests"] if document else 0,
                    "empty": completed
                    and document is not None
                    and document["observed_requests"] == 0,
                },
            }

        if not self.enabled:
            return result("disabled")
        deadline = time.monotonic() + self.settings["tick_seconds"]
        try:
            policy, _ = self.bridge._policy()
            expected = {"installation_epoch": self.epoch, "policy_revision": digest(policy)}
            bound = _BoundBridge(self.bridge, expected, deadline)
            scope = digest([self.bridge.organization, expected])
            configuration = digest([SCHEMA, self.settings, self.bridge.config.name, expected])
            while True:
                with bound._serialized() as journal:
                    bound._assert_installed_policy(journal)
                    document = self._load(journal, scope, configuration)
                    now = int(time.time())
                    require(time.monotonic() < deadline, "reconciliation_deadline")
                    if document["page_loaded"]:
                        pending = document["pending"]
                        due = next((item for item in pending if item["retry_at"] <= now), None)
                        if due is not None:
                            if counters["attempts"] >= self.settings["max_requests"]:
                                return result("request_limit")
                            document = self._save(
                                journal,
                                scope,
                                document,
                                {
                                    "active": {
                                        "request_id": due["request_id"],
                                        "action": due["action"],
                                    }
                                },
                            )
                            require(time.monotonic() < deadline, "reconciliation_deadline")
                            counters["attempts"] += 1
                            try:
                                response = bound._reconcile(
                                    journal,
                                    request_id=due["request_id"],
                                    poll_interval=self.settings["poll_interval"],
                                    reminder_interval=self.settings["reminder_interval"],
                                    schedule_action=due["action"],
                                )
                                require(
                                    isinstance(response, dict)
                                    and response.get("state") in {"reconciled", "throttled"},
                                    "schedule_response_unavailable",
                                )
                                if response["state"] == "reconciled":
                                    require(
                                        response.get("installation_epoch") == self.epoch
                                        and response.get("tenant_ref")
                                        == digest(self.bridge.organization),
                                        "reconciliation_binding_changed",
                                    )
                                    pending = [item for item in pending if item != due]
                                    acknowledged = document["acknowledged_requests"] + 1
                                else:
                                    delay = response.get("retry_after_seconds")
                                    require(
                                        type(delay) is int and 1 <= delay <= 86400,
                                        "schedule_response_unavailable",
                                    )
                                    pending = [item for item in pending if item != due] + [
                                        {
                                            **due,
                                            "retry_at": int(time.time()) + cast(int, delay),
                                        }
                                    ]
                                    acknowledged = document["acknowledged_requests"]
                            except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
                                pending = [item for item in pending if item != due] + [
                                    {
                                        **due,
                                        "retry_at": int(time.time())
                                        + self.settings["poll_interval"],
                                    }
                                ]
                                acknowledged = document["acknowledged_requests"]
                            advanced = acknowledged > document["acknowledged_requests"]
                            document = self._save(
                                journal,
                                scope,
                                document,
                                {
                                    "pending": pending,
                                    "active": None,
                                    "acknowledged_requests": acknowledged,
                                },
                            )
                            counters["acknowledged"] += int(advanced)
                            continue
                        if pending:
                            return result("deferred")
                        cursor = document["next_cursor"]
                        finished = cursor is None
                        advance: Json = {
                            "cursor": cursor,
                            "next_cursor": None,
                            "page_loaded": False,
                        }
                        if finished:
                            advance.update(
                                completed_at=now, next_sweep_at=now + self.settings["poll_interval"]
                            )
                        document = self._save(
                            journal,
                            scope,
                            document,
                            advance,
                        )
                        if finished:
                            return result(
                                "idle" if not document["observed_requests"] else "sweep_complete",
                                completed=True,
                            )
                    if counters["pages"] >= self.settings["max_pages"]:
                        return result("page_limit")
                    if document["cursor"] is None and document["next_sweep_at"] > now:
                        return result("cadence_held")
                    prior = document
                # Real public summary acquires its own writer through the bound
                # adapter. A concurrent tick may win staging; the CAS below detects it.
                counters["pages"] += 1
                page = bound.pending_summary(
                    limit=self.settings["page_size"], cursor=prior["cursor"]
                )
                with bound._serialized() as journal:
                    document = self._load(journal, scope, configuration)
                    if document != prior:
                        continue
                    require(
                        isinstance(page, dict)
                        and page.get("installation_epoch") == self.epoch
                        and page.get("tenant_ref") == digest(self.bridge.organization)
                        and isinstance(page.get("items"), list)
                        and all(isinstance(item, dict) for item in page["items"])
                        and len(page["items"]) <= self.settings["page_size"]
                        and (
                            page.get("next_cursor") is None
                            or (
                                isinstance(page["next_cursor"], str)
                                and len(page["next_cursor"]) <= 2048
                            )
                        ),
                        "schedule_response_unavailable",
                    )
                    references = [item.get("request_id") for item in page["items"]]
                    require(
                        all(isinstance(ref, str) and REQ.fullmatch(ref) for ref in references)
                        and len(references) == len(set(references)),
                        "schedule_response_unavailable",
                    )
                    sweep_ref = prior["sweep_ref"]
                    if prior["cursor"] is None:
                        sweep_ref = "sweep-" + secrets.token_hex(16)
                    changes = {
                        "pending": [
                            {
                                "request_id": ref,
                                "retry_at": 0,
                                "action": digest([scope, sweep_ref, ref]),
                            }
                            for ref in references
                        ],
                        "next_cursor": page["next_cursor"],
                        "page_loaded": True,
                        "active": None,
                        "observed_requests": document["observed_requests"] + len(references),
                    }
                    if prior["cursor"] is None:
                        changes.update(
                            sweep_ref=sweep_ref,
                            started_at=int(time.time()),
                            completed_at=None,
                            observed_requests=len(references),
                            acknowledged_requests=0,
                        )
                    document = self._save(journal, scope, document, changes)
        except ApprovalError as error:
            return result(
                {
                    "reconciliation_deadline": "timeout",
                    "reconciliation_binding_changed": "binding_held",
                    "schedule_configuration_changed": "configuration_held",
                }.get(str(error), "unavailable")
            )
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            return result("unavailable")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--organization", required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--installation-epoch", required=True)
    parser.add_argument("--enabled", action="store_true")
    for name, default in (
        ("poll-interval", 60),
        ("reminder-interval", 3600),
        ("page-size", 25),
        ("max-pages", 4),
        ("max-requests", 50),
        ("tick-seconds", 10),
    ):
        parser.add_argument("--" + name, type=int, default=default)
    args = vars(parser.parse_args())
    try:
        bridge = ApprovalBridge(args.pop("directory"), args.pop("organization"), args.pop("config"))
        value = ApprovalReconciler(bridge, **args).tick()
    except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
        value = {"schema_version": SCHEMA, "state": "unavailable"}
    print(canonical(value))
    return 0 if value["state"] not in {"unavailable", "binding_held", "configuration_held"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
