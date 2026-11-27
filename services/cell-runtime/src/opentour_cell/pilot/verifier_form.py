"""Authenticated named Slack verifier ingress for an existing custodian-saved binding."""

from __future__ import annotations

import hashlib
import hmac
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

from opentour_cell.pilot.intake_mcp import IntakePrincipal, require_ref
from opentour_cell.pilot.live_evidence import (
    FeedGrant,
    LiveEvidence,
    LiveScope,
    _canonical,
    _digest,
    _event,
    _require,
)


@dataclass(frozen=True)
class Choice:
    namespace: str
    decision: str
    revision: int
    token: str
    outcome_revision: int


class VerifierForm:
    def __init__(self, live: LiveEvidence, actor: IntakePrincipal, workflow: str):
        self.live, self.actor, self.workflow = live, actor, workflow

    def _choices(
        self, db: sqlite3.Connection, scope: LiveScope
    ) -> tuple[str, FeedGrant, list[Choice]]:
        _require(scope.can_verify and self.actor.role == "slack")
        bindings = db.execute(
            "SELECT ref FROM live_bindings WHERE tenant=? AND workflow=? AND epoch=? "
            "AND kind='independent_outcome' AND successor IS NULL",
            (scope.tenant, scope.workflow_ref, scope.engagement_epoch),
        ).fetchall()
        selected = []
        for row in bindings:
            body, successor = self.live._binding(db, row[0])
            grant = FeedGrant(**body["grant"])
            if (
                successor is None
                and self.live._same_scope(body, scope)
                and grant in scope.grants
                and body.get("slack_verifier") == self.actor.actor
                and grant.verifier == self.actor.actor
            ):
                selected.append((str(row[0]), grant))
        _require(len(selected) == 1)
        ref, grant = selected[0]
        targets: dict[tuple[str, str, int], str] = {}
        self.live._snapshot(db, scope, form_targets=targets, verifying_actor=self.actor.actor)
        previous = {}
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='live_form_receipts'").fetchone():
            previous = dict(
                db.execute(
                    "SELECT target,max(revision) FROM live_form_receipts WHERE binding=? "
                    "GROUP BY target",
                    (ref,),
                )
            )
        choices = []
        key = self.live._key(db)
        for (namespace, decision, revision), identity in sorted(targets.items()):
            if namespace != grant.deciding_namespace:
                continue
            target = _digest([namespace, decision, revision])
            version = previous.get(target, 0) + 1
            pin = hmac.new(
                key,
                _canonical(
                    [self.actor.actor, scope.authority_revision, ref, target, identity, version]
                ).encode(),
                hashlib.sha256,
            ).hexdigest()
            choices.append(Choice(namespace, decision, revision, pin, version))
        return ref, grant, choices

    def choices(self) -> tuple[Choice, ...]:
        with self.live._scope(self.actor, self.workflow) as scope, self.live._db() as db:
            return tuple(self._choices(db, scope)[2])

    def permits_receipt(self, receipt: str) -> bool:
        with self.live._scope(self.actor, self.workflow) as scope, self.live._db() as db:
            ref, _, choices = self._choices(db, scope)
            row = db.execute(
                "SELECT target,revision,authority_revision FROM live_form_receipts "
                "WHERE receipt=? AND binding=?",
                (receipt, ref),
            ).fetchone()
            return bool(
                row
                and row["authority_revision"] == scope.authority_revision
                and any(
                    _digest([c.namespace, c.decision, c.revision]) == row["target"]
                    and c.outcome_revision == row["revision"] + 1
                    for c in choices
                )
            )

    def submit(self, token: str, finality: str, action: str, basis: str, effective_at: int) -> str:
        _require(
            finality in {"final", "reopened"}
            and type(effective_at) is int
            and 0 < effective_at < 2**63
        )
        require_ref(basis)
        if finality == "final":
            require_ref(action)
        else:
            _require(not action)
        request = _digest([token, finality, action, basis, effective_at])
        # Check both the initial grant and the post-lock signed grant. Leaving the
        # inner guard rechecks custody while a failed transaction can still roll back.
        with (
            self.live._scope(self.actor, self.workflow) as initial,
            self.live._db(write=True) as db,
            self.live._scope(self.actor, self.workflow) as scope,
        ):
            _require(initial == scope)
            ref, grant, choices = self._choices(db, scope)
            db.execute("""CREATE TABLE IF NOT EXISTS live_form_receipts (
                pin TEXT PRIMARY KEY, tenant TEXT NOT NULL, workflow TEXT NOT NULL,
                epoch TEXT NOT NULL,
                dataset TEXT NOT NULL, dataset_revision TEXT NOT NULL,
                binding TEXT NOT NULL,
                target TEXT NOT NULL, revision INTEGER NOT NULL, fingerprint TEXT NOT NULL,
                authority_revision TEXT NOT NULL, basis TEXT NOT NULL,
                recorded_at INTEGER NOT NULL, event_id TEXT NOT NULL,
                receipt TEXT NOT NULL, UNIQUE(binding,target,revision))""")
            old = db.execute("SELECT * FROM live_form_receipts WHERE pin=?", (token,)).fetchone()
            if old is not None:
                _require(
                    old["fingerprint"] == request
                    and old["authority_revision"] == scope.authority_revision
                    and old["binding"] == ref
                    and any(
                        _digest([c.namespace, c.decision, c.revision]) == old["target"]
                        and c.outcome_revision == old["revision"] + 1
                        for c in choices
                    )
                )
                return str(old["receipt"])
            choice = next((c for c in choices if hmac.compare_digest(c.token, token)), None)
            _require(choice is not None)
            assert choice is not None
            _require(choice.outcome_revision > 1 or finality == "final")
            target = _digest([choice.namespace, choice.decision, choice.revision])
            recorded = int(time.time())
            event: dict[str, Any] = dict(
                kind="OutcomeVerified",
                event_id="form-event-" + _digest([ref, target, choice.outcome_revision])[:40],
                event_revision=1,
                decision_id=choice.decision,
                decision_revision=choice.revision,
                outcome_id="form-outcome-" + _digest([ref, target])[:40],
                outcome_revision=choice.outcome_revision,
                effective_at=effective_at,
                recorded_at=recorded,
                finality=finality,
                verified_action_ref=action or None,
                verifier=self.actor.actor,
            )
            receipt = "verification-" + _digest([ref, request, event])[:40]
            db.execute(
                "INSERT INTO live_form_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    token,
                    scope.tenant,
                    scope.workflow_ref,
                    scope.engagement_epoch,
                    scope.dataset_ref,
                    scope.dataset_revision,
                    ref,
                    target,
                    choice.outcome_revision,
                    request,
                    scope.authority_revision,
                    basis,
                    recorded,
                    event["event_id"],
                    receipt,
                ),
            )
            self.live._record_event(db, scope, grant, ref, _event(event, grant))
            self.live._archive(db, scope)
            return receipt
