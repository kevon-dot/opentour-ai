"""Stage private approval requests and send each once from the customer's Slack worker."""

from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.repair_workflow import _private_json, _record, managed_review_evidence
from opentour_cell.pilot.training import LocalTrainingStore, _check_file

if TYPE_CHECKING:
    from opentour_cell.pilot.slack_adapter import Settings, Target, Workbench

Json = dict[str, Any]
RECEIPT = re.compile(r"approval-[0-9a-f]{40}")
TS = re.compile(r"[0-9]{1,15}\.[0-9]{1,12}")


def _required(condition: object) -> None:
    if not condition:
        raise ValueError("approval_request_unavailable")


def _receipt(pointer: Json) -> str:
    return "approval-" + digest(pointer)[:40]


WITHDRAWN = "approval_authority_withdrawn"


def _held_marker(directory: Path, receipt: str) -> Path:
    return directory / ("approval-held-" + receipt + ".json")


def _discard(path: Path) -> None:
    path.unlink(missing_ok=True)


def _journal_held(directory: Path, receipt: str) -> bool:
    """Read the Slack worker's own durable hold for this receipt. Unreadable counts as held."""
    path = directory / "slack-pilot.sqlite3"
    try:
        if not os.path.lexists(path):
            return False
        _check_file(path)
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)) as db:
            if not db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_posts'"
            ).fetchone():
                return False
            row = db.execute(
                "SELECT state FROM approval_posts WHERE receipt=?", (receipt,)
            ).fetchone()
    except Exception:
        return True
    return row is not None and row[0] == "held"


def _hold_in_journal(directory: Path, receipt: str) -> bool:
    """Record the hold where flush_approvals refuses it even without a marker file."""
    path = directory / "slack-pilot.sqlite3"
    try:
        if not os.path.lexists(path):
            return False
        _check_file(path)
        with closing(sqlite3.connect(path, timeout=5)) as db:
            with db:
                db.execute("BEGIN IMMEDIATE")
                if not db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_posts'"
                ).fetchone():
                    return False
                # Never rewrite a claimed, uncertain or posted row: only an unclaimed receipt.
                db.execute(
                    "INSERT OR IGNORE INTO approval_posts (receipt, revision, state, exposure) "
                    "VALUES(?,'','held','')",
                    (receipt,),
                )
    except Exception:
        return False
    return True


def _authority_withdrawn(directory: Path, organization: str, run_id: str) -> bool:
    """Re-read the engagement and any standing mandate through fresh handles.

    The sealed intake authority is the worker-held record of whether a standing mandate
    governs this run; a delegated intake re-reads that mandate through a fresh ledger. A
    governed run must also have its exact standing plan. Any unreadable, inconsistent or
    unexpected result counts as withdrawn. The mandate lock is never held across the
    training database, so this cannot deadlock with a withdrawal.
    """
    from opentour_cell.pilot import source_intake

    try:
        run_id = run_id.removeprefix("managed:")
        _required(re.fullmatch(r"run-[0-9a-f]{32}", run_id))
        work = directory / "managed-outputs" / run_id
        run = _private_json(work / "run.json")
        review = _private_json(work / "review.json")
        engagement = review["engagement"]
        # A fresh store refuses a withdrawn engagement or a committed closeout. The sealed
        # intake below then pins this run to that same engagement.
        LocalTrainingStore(directory / "training.sqlite3", organization, existing_only=True)
        bound = source_intake._BoundEngagement(
            directory / "training.sqlite3", organization, engagement
        )
        _, grant = source_intake._verified_marker(bound, review["dataset_revision"])
        plan_path = directory / ("cycle-plan-" + run_id + ".json")
        governed = "delegation" in grant
        _required(governed == os.path.lexists(plan_path))
        if governed:
            plan = _private_json(plan_path)
            _required(
                run["plan_revision"] == digest(plan)
                and plan["organization"] == organization
                and plan["engagement"] == engagement
            )
    except Exception:
        return True
    return False


def enqueue_ready(
    directory: Path,
    organization: str,
    run_id: str,
    *,
    run_revision: str,
    team: str,
    channel: str,
    audience: str,
    reviewer: str,
    provider: str | None = None,
) -> Json:
    """No provider call. Pin the exact frozen review and its separately granted reviewer."""
    store = LocalTrainingStore(directory / "training.sqlite3", organization, existing_only=True)
    policy = {"team": team, "channel": channel, "audience": audience, "reviewer": reviewer}
    _required(isinstance(reviewer, str) and re.fullmatch(r"[UW][A-Z0-9]{2,31}", reviewer))
    with store.engagement_guard():
        evidence = managed_review_evidence(directory, organization, run_id, audience, team, channel)
        _required(
            isinstance(evidence.get("release_review"), dict)
            and all(evidence["release_review"].get(k) == v for k, v in policy.items())
            and evidence["audit"]["run_revision"] == run_revision
            and evidence["audit"]["handoff_disposition"] in {"simple_repair", "model_candidate"}
        )
        frozen_provider = evidence["release_review"].get("provider", "slack")
        _required(
            frozen_provider in {"slack", "zapier_slack"}
            and (provider is None or provider == frozen_provider)
        )
        pointer = {
            "organization": organization,
            "run_id": evidence["audit"]["run_id"],
            "run_revision": run_revision,
            **policy,
        }
        if "provider" in evidence["release_review"]:
            pointer["provider"] = frozen_provider
        receipt = _receipt(pointer)
        path = directory / ("approval-ready-" + receipt + ".json")
        _record(path, pointer)
    # A mandate withdrawal takes only the mandate database, so it can commit between the
    # authority read above and the write. Re-read both authorities after the pointer is
    # durable; a withdrawn or unknown authority never leaves a deliverable pointer.
    marker = _held_marker(directory, receipt)
    if (
        not os.path.lexists(marker)
        and not _journal_held(directory, receipt)
        and not _authority_withdrawn(directory, organization, pointer["run_id"])
    ):
        return {"receipt": receipt, "state": "pending_review"}
    try:
        _discard(path)
        state = "removed"
    except Exception:
        # Fail closed: flush_approvals refuses a held marker or a held journal row. The
        # marker names only the receipt, so custody can inventory it without Slack ids.
        state = "unremoved"
        try:
            _record(
                marker, {"receipt": receipt, "reason": WITHDRAWN, "pointer_digest": digest(pointer)}
            )
            state = "marked"
        except Exception:
            pass
        if _hold_in_journal(directory, receipt):
            state = "marked"
    return {"receipt": receipt, "state": "held", "reason": WITHDRAWN, "pointer": state}


def _read(cfg: Settings, receipt: str) -> Json:
    _required(RECEIPT.fullmatch(receipt))
    item = _private_json(cfg.directory / ("approval-ready-" + receipt + ".json"))
    _required(
        _receipt(item) == receipt
        and cfg.approval_requests
        and item.get("provider", "slack") == cfg.approval_provider
        and (cfg.organization, cfg.team, cfg.channel, cfg.recipient, cfg.reviewer)
        == tuple(item[k] for k in ("organization", "team", "channel", "audience", "reviewer"))
    )
    return item


def _target(app: Workbench, cfg: Settings, receipt: str) -> tuple[Json, Target]:
    item = _read(cfg, receipt)
    target = app._managed(cfg, cfg.reviewer, "managed:" + item["run_id"])
    entry = cfg.permission(cfg.reviewer, "review", target.dataset)
    _required(
        entry["aggregate"]
        and target.audit.get("release_reviewer") == cfg.reviewer
        and target.audit["run_revision"] == item["run_revision"]
    )
    return item, target


def _request(receipt: str) -> Json:
    return {
        "text": (
            "An OpenTour repair is ready for its named reviewer. "
            "Choose Open evidence to inspect it privately."
        ),
        "blocks": [
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "action_id": "ot_open_approval",
                        "text": {"type": "plain_text", "text": "Open evidence"},
                        "value": receipt,
                    }
                ],
            }
        ],
        "unfurl_links": False,
        "unfurl_media": False,
    }


def _release_claim(app: Workbench, receipt: str, exposure: str, state: str) -> None:
    """End our own claim without sending: held before any send attempt, else uncertain."""
    with app.journal.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE approval_posts SET state=? WHERE receipt=? AND state='claimed'",
            (state, receipt),
        )
        db.execute(
            "UPDATE exposures SET status=?,completed=? WHERE id=? AND status='admitted'",
            ("withheld" if state == "held" else "uncertain", int(time.time()), exposure),
        )


def _flush_one(app: Workbench, cfg: Settings, receipt: str, claim: Json) -> bool:
    from opentour_cell.pilot.slack_adapter import _evidence_guard

    marker = _held_marker(cfg.directory, receipt)
    if os.path.lexists(marker):
        return False
    with app.journal.db() as db:
        if db.execute("SELECT 1 FROM approval_posts WHERE receipt=?", (receipt,)).fetchone():
            return False
    try:
        current = app.settings()
        item, target = _target(app, current, receipt)
        try:
            app.private_channel(current, current.reviewer)
        except Exception:
            # The provider may be unavailable before admission. A later pass rechecks it.
            return False
        with _evidence_guard(
            app.store,
            target.dataset,
            target.dataset_revision,
            target.audit["engagement"],
            target.audit,
        ):
            live = app.settings(check_engagement=False)
            _read(live, receipt)
            _required(live.permission(live.reviewer, "review", target.dataset)["aggregate"])
            live.permission(live.reviewer, "inspect", target.dataset)
            decided = (
                current.directory / "managed-outputs" / item["run_id"] / "release-decision.json"
            )
            if decided.exists():
                return False
            with app.journal.db() as db:
                db.execute("BEGIN IMMEDIATE")
                app.journal.active(db)
                exposure = app.journal.opaque("approval", receipt)
                new = db.execute(
                    "INSERT OR IGNORE INTO approval_posts "
                    "(receipt, revision, state, exposure) VALUES(?,?,'claimed',?)",
                    (receipt, target.revision, exposure),
                ).rowcount
                if not new:
                    return False
                db.execute(
                    "INSERT INTO exposures VALUES(?,?,'admitted',?,NULL)",
                    (exposure, "approval_request", int(time.time())),
                )
            claim["exposure"] = exposure
    except (ValueError, KeyError, OSError):
        if "exposure" not in claim:
            return False
        raise
    # A mandate withdrawal never takes the evidence guard, so it can commit after the claim,
    # and enqueue_ready can mark a receipt held concurrently. Re-check the marker and re-read
    # both authorities through fresh handles immediately before the network call; a marker
    # or a withdrawn or unknown authority releases the claim as held and nothing is sent.
    # Residual: a withdrawal landing after this read can still produce one button-only
    # post. Opening that button and recording any decision re-check the authority.
    if os.path.lexists(marker) or _authority_withdrawn(
        current.directory, current.organization, item["run_id"]
    ):
        _release_claim(app, receipt, exposure, "held")
        claim.clear()
        return False
    # A provider might accept a message before its response is received. A claimed or
    # uncertain request is never retried, including after process death.
    claim["sending"] = True
    ts = ""
    try:
        response = app.web.chat_postMessage(channel=current.channel, **_request(receipt))
        _required(
            response.get("ok") is True
            and response.get("channel") == current.channel
            and TS.fullmatch(response.get("ts", ""))
        )
        ts = response["ts"]
    except Exception:
        pass
    with app.journal.db() as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute(
            "UPDATE approval_posts SET state=?,ts=? WHERE receipt=? AND state='claimed'",
            ("posted" if ts else "uncertain", ts, receipt),
        )
        db.execute(
            "UPDATE exposures SET status=?,completed=? WHERE id=? AND status='admitted'",
            ("presented" if ts else "uncertain", int(time.time()), exposure),
        )
    claim.clear()
    return bool(ts)


def flush_approvals(app: Workbench) -> int:
    cfg = app.settings()
    if not cfg.approval_requests:
        return 0
    sent = 0
    for path in sorted(cfg.directory.glob("approval-ready-approval-*.json")):
        receipt = path.stem.removeprefix("approval-ready-")
        claim: Json = {}
        try:
            sent += _flush_one(app, cfg, receipt, claim)
        except Exception:
            # Fail closed for this receipt only: an unexpected error never leaves our claim
            # live and never stops the remaining receipts in this pass.
            if "exposure" in claim:
                try:
                    _release_claim(
                        app,
                        receipt,
                        claim["exposure"],
                        "uncertain" if claim.get("sending") else "held",
                    )
                except Exception:
                    # A claim that cannot be released stays claimed; restart marks it uncertain.
                    pass
            continue
        if sent >= 50:
            break
    return sent


def open_approval(app: Workbench, cfg: Settings, body: Json, receipt: str) -> None:
    """A Slack-authenticated button opens exactly the presented candidate for its named reviewer."""
    actor = body["user"]["id"]
    box = body["container"]
    _required(
        actor == cfg.reviewer
        and box["channel_id"] == cfg.channel
        and body["channel"]["id"] == cfg.channel
    )
    _required(cfg.approval_requests and RECEIPT.fullmatch(receipt))
    cfg.permission(actor, "review")
    ts = box["message_ts"]
    _required(isinstance(ts, str) and TS.fullmatch(ts))
    with app.journal.db() as db:
        db.execute("BEGIN IMMEDIATE")
        app.journal.active(db)
        row = db.execute("SELECT * FROM approval_posts WHERE receipt=?", (receipt,)).fetchone()
        _required(row)
        if row["state"] in {"claimed", "uncertain"}:
            # An authenticated callback on our app's exact posted block can independently
            # reconcile an ambiguous transmission without a duplicate channel message.
            message = body.get("message", {})
            _required(
                message.get("app_id") == cfg.app
                and message.get("ts") == ts
                and message.get("blocks") == _request(receipt)["blocks"]
            )
            db.execute(
                "UPDATE approval_posts SET state='posted',ts=? WHERE receipt=?", (ts, receipt)
            )
            db.execute(
                "UPDATE exposures SET status='presented',completed=? WHERE id=?",
                (int(time.time()), row["exposure"]),
            )
        else:
            _required(row["state"] == "posted" and row["ts"] == ts)
    view = app.web.views_open(
        trigger_id=body["trigger_id"], view=app.views.notice_modal("checking")
    )
    view_id = view["view"]["id"]
    try:
        # Consume Slack's short-lived trigger before independent network and evidence checks.
        app.private_channel(cfg, actor)
        item, target = _target(app, cfg, receipt)
        _required(row["revision"] == target.revision)
        app._evidence(
            cfg,
            actor,
            view_id,
            {"job": "managed:" + item["run_id"], "revision": target.revision},
            "",
        )
    except Exception:
        app._safe_notice(view_id)
        raise


def unresolved(cfg: Settings) -> list[Json]:
    path = cfg.directory / "slack-pilot.sqlite3"
    if not path.exists():
        return []
    _check_file(path)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        _required(
            db.execute("SELECT binding FROM identity WHERE id=1").fetchone() == (cfg.binding,)
        )
        if not db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='approval_posts'"
        ).fetchone():
            return []
        return [
            {"receipt": receipt, "state": state}
            for receipt, state in db.execute(
                "SELECT receipt, state FROM approval_posts WHERE state IN ('claimed','uncertain') "
                "ORDER BY receipt LIMIT 100"
            )
        ]


def delivery_cycle(app: Workbench) -> Json:
    """Deliver eligible invitations and supervise the selected plugin's independent return path."""
    from opentour_cell.pilot.zapier_approval import ApprovalBridge

    result: Json = {"posted": flush_approvals(app)}
    current = app.settings()
    if not current.approval_requests or current.approval_provider != "zapier_slack":
        return result
    bridge = ApprovalBridge(
        current.directory, current.organization, current.directory / "zapier-approval.json"
    )
    hook = os.environ.get("OPENTOUR_ZAPIER_APPROVAL_HOOK_URL", "")
    if hook:
        try:
            result["plugin_outbound"] = bridge.flush_ready(hook)
        except (ValueError, OSError, KeyError, sqlite3.Error):
            result["plugin_outbound"] = {"state": "setup_or_policy_unavailable"}
    else:
        result["plugin_outbound"] = {"state": "hook_unavailable"}
    # An already signed response can still arrive while the outbound connection is disabled.
    current = app.settings()
    if current.approval_requests and current.approval_provider == "zapier_slack":
        try:
            result["plugin_returns"] = bridge.import_ready()
        except (ValueError, OSError, KeyError, sqlite3.Error):
            result["plugin_returns"] = {"state": "setup_or_policy_unavailable"}
    return result


class ApprovalDrainer:
    def __init__(self, app: Workbench):
        self.app = app
        self.closed = threading.Event()
        self.thread = threading.Thread(target=self._run, name="pilot-approval-inbox", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def _run(self) -> None:
        while not self.closed.is_set():
            try:
                delivery_cycle(self.app)
            except (ValueError, KeyError, OSError, sqlite3.Error):
                pass
            if self.closed.wait(30):
                return

    def close(self) -> None:
        self.closed.set()
        self.thread.join()
