"""Generated proofs for learning-run admission, convergence, the schedule tick and cancellation.

Every authority below is a generated sentinel fixture: a local training store, a real enrolled
standing mandate, owner-sealed learning-run grants and a sealed plan. The fit ledger and trainer
are generated stand-ins that follow the OPE-442 and OPE-418 contracts. None of this is evidence
of an installed provider, a customer outcome or a GPU fit.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot import learning_mandate as mandate_module
from opentour_cell.pilot import learning_runs as lr
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.operating_point import canonical
from opentour_cell.pilot.training import LocalTrainingStore

TENANT = "org-a"
OWNER = "owner"
DELEGATE = "worker"
WORKFLOW = "workflow-learning"
MANDATE = "standing-1"
TEMPLATE = Path(__file__).resolve().parents[2] / "examples/pilot-learning-mandate.template.json"
DOC = Path(__file__).resolve().parents[2] / "docs/pilot-learning-runs.md"


class GeneratedLedger:
    """Stand-in for the OPE-442 ledger: one reservation per fit identity across keys."""

    def __init__(self, max_fits: int = 1, units: int = 90) -> None:
        self.max_fits, self.units = max_fits, units
        self.reservations: dict[str, lr.FitReservation] = {}
        self.keys: dict[str, str] = {}
        self.settled: dict[str, int] = {}
        self.reserve_calls = self.lookup_calls = self.settle_calls = 0
        self.available = True
        self.lock = threading.Lock()

    def lookup(self, intent: lr.FitIntent) -> lr.FitReservation | None:
        self.lookup_calls += 1
        return self.reservations.get(intent.identity)

    def reserve(self, intent: lr.FitIntent, *, operation_key: str) -> lr.FitReservation:
        with self.lock:
            self.reserve_calls += 1
            if not self.available:
                raise OSError("generated ledger offline")
            if self.keys.get(operation_key, intent.identity) != intent.identity:
                raise lr.LedgerHold("fit_intent_requires_reconciliation")
            if intent.identity in self.reservations:
                return self.reservations[intent.identity]
            if len(self.reservations) >= self.max_fits:
                raise lr.LedgerHold("standing_fit_budget_exhausted")
            found = lr.FitReservation(
                "fitres-" + intent.identity[:24], min(self.units, intent.max_units)
            )
            self.reservations[intent.identity] = found
            self.keys[operation_key] = intent.identity
            return found

    def settle(self, reservation_ref: str, *, consumed_units: int) -> lr.FitReservation:
        with self.lock:
            self.settle_calls += 1
            if not self.available:
                raise OSError("generated ledger offline")
            reserved = next(
                r for r in self.reservations.values() if r.reservation_ref == reservation_ref
            )
            # Settle once; a replay returns the original settlement. No refunds below it.
            charged = self.settled.setdefault(reservation_ref, max(consumed_units, 0))
            return lr.FitReservation(reservation_ref, reserved.reserved_units, charged)


class GeneratedTrainer:
    """Stand-in trainer that counts submissions per deterministic submission key."""

    def __init__(self) -> None:
        self.jobs: dict[str, str] = {}
        self.submissions = 0

    def submit(self, key: str) -> str:
        self.submissions += 1
        return self.jobs.setdefault(key, "job-" + key[-12:])

    def find(self, key: str) -> str | None:
        return self.jobs.get(key)


class Site:
    """One generated owner directory with a real enrolled standing mandate."""

    def __init__(self, root: Path, *, tenant: str = TENANT, **mandate: object) -> None:
        self.tenant = tenant
        self.directory = root / tenant
        self.directory.mkdir(mode=0o700)
        self.store = LocalTrainingStore(self.directory / "training.sqlite3", tenant)
        self.engagement = self.store.engagement_id
        self.registry = LearningMandates(self.directory, tenant, self.engagement, owner=OWNER)
        self.receipt = self.registry.enroll(self.spec(MANDATE, **mandate), actor=OWNER)
        self.ledger = GeneratedLedger()
        self.trainer = GeneratedTrainer()

    @staticmethod
    def spec(mandate_id: str, **changes: object) -> dict[str, object]:
        scope = json.loads(TEMPLATE.read_text())
        scope.update(
            mandate_id=mandate_id,
            workflow=WORKFLOW,
            owner_id=OWNER,
            delegate=DELEGATE,
            trainer=DELEGATE,
            reviewer="reviewer",
            expires_at=int(time.time()) + 3600,
            agreement_revision="e" * 64,
        )
        if changes.pop("train", True) is False:
            scope.pop("model_fields")
            scope.update(
                uses=["inspect", "evaluate"],
                profile=None,
                max_fits=0,
                max_total_seconds=0,
                max_seconds_per_fit=0,
            )
        scope.update(changes)
        return scope

    def authorize(
        self,
        name: str,
        *,
        role: str = "owner",
        channel: str = "slack",
        actor: str | None = None,
        client: str | None = None,
        workflow: str = WORKFLOW,
        expires_at: int | None = None,
        permissions: tuple[str, ...] = (),
        mandate_id: str = MANDATE,
        expected_exit: int = 0,
    ) -> Path:
        selection = self.directory / f"{name}.json"
        argv = [
            "authorize",
            "--selection",
            str(selection),
            "--tenant",
            self.tenant,
            "--owner",
            OWNER,
            "--actor",
            actor or (DELEGATE if role == "scheduler" else OWNER),
            "--role",
            role,
            "--channel",
            channel,
            "--client",
            client or f"client-{name}",
            "--workflow",
            workflow,
            "--mandate",
            mandate_id,
            "--grant-ref",
            f"grant-{name}",
            "--expires-at",
            str(expires_at or int(time.time()) + 3600),
        ]
        for permission in permissions:
            argv += ["--permission", permission]
        assert lr.main(argv) == expected_exit
        return selection

    def open(self, selection: Path, *, ledger: object = "default", plans=None):
        chosen = self.ledger if ledger == "default" else ledger
        return lr.open_learning_runs(selection, ledger=chosen, plans=plans)

    def plan(
        self, source: str = "source-v1", *, capable: bool = True, receipt=None
    ) -> lr.LearningPlan:
        receipt = receipt or self.registry.inspect(MANDATE)
        return lr.LearningPlan(
            self.tenant,
            self.engagement,
            WORKFLOW,
            receipt["mandate_id"],
            receipt["mandate_revision"],
            source,
            "incumbent-v1",
            "benchmark-v1",
            "baseline-v1",
            "smollm2_135m_lora_v1",
            "local-open-weight" if capable else "inference-only-route",
            capable,
        )

    def publish(self, plan: lr.LearningPlan | None = None) -> str:
        return lr.publish_plan(self.directory, plan or self.plan())

    def counts(self) -> dict[str, int]:
        with sqlite3.connect(self.directory / lr.DATABASE) as db:
            return {
                table: db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("lr_runs", "lr_keys", "lr_triggers", "lr_cancellations", "lr_ticks")
            }

    def runs(self) -> list[tuple[str, str, str | None]]:
        with sqlite3.connect(self.directory / lr.DATABASE) as db:
            return db.execute(
                "SELECT run_ref,state,reason_code FROM lr_runs ORDER BY created_at,rowid"
            ).fetchall()

    def ticks(self) -> list[tuple[str, str | None]]:
        with sqlite3.connect(self.directory / lr.DATABASE) as db:
            return db.execute("SELECT outcome,reason_code FROM lr_ticks ORDER BY seq").fetchall()

    def pause(self) -> None:
        """Generated owner-settings head in the shape OwnerSettings.save writes on pause."""
        self.registry.withdraw(MANDATE, actor=OWNER)
        with sqlite3.connect(self.directory / "learning-mandates.sqlite3") as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS owner_settings_heads "
                "(scope TEXT PRIMARY KEY, revision TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS owner_settings_revisions (scope TEXT, "
                "revision TEXT, packet TEXT, receipt TEXT, PRIMARY KEY(scope,revision))"
            )
            receipt = {"revision": "paused-1", "mandate_id": MANDATE, "state": "pause"}
            db.execute(
                "INSERT INTO owner_settings_revisions VALUES (?,?,?,?)",
                (self.registry.scope, "paused-1", "{}", canonical(receipt)),
            )
            db.execute(
                "INSERT OR REPLACE INTO owner_settings_heads VALUES (?,?)",
                (self.registry.scope, "paused-1"),
            )


@pytest.fixture
def site(tmp_path):
    return Site(tmp_path)


def submit(service, principal, plan_ref, key, operation="evaluate_and_improve"):
    return service.submit(principal, lr.SubmitRequest(plan_ref, operation, key))


def drive_to_review(site: Site, worker, principal, *, consumed: int = 42) -> lr.Claim:
    """The generated executor: discovery, one fit reservation, one submission, verification."""
    claim = worker.claim(principal)
    assert claim is not None and claim.cancel_requested is False
    worker.advance(principal, claim, stage="discovery", progress=(1, 4))
    reserved = worker.reserve_fit(
        principal, claim, residual_ref="residual-1", model_digest="base-digest-1", max_units=90
    )
    assert reserved["state"] == "reserved"
    key = worker.begin_submission(principal, claim)
    worker.record_submission(principal, claim, execution_ref=site.trainer.submit(key))
    worker.advance(principal, claim, state="verifying", stage="verification", progress=(4, 4))
    worker.finish(
        principal, claim, "review_ready", consumed_units=consumed, evidence_refs=("candidate-1",)
    )
    return claim


def test_reference_run_reaches_review_with_one_reservation_and_one_submission(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    plan_ref = site.publish()
    admitted = submit(service, slack, plan_ref, "click-1")
    assert admitted["state"] == "queued" and admitted["next_action"] == "wait_for_worker"
    assert admitted["effective_operation"] == "evaluate_and_improve"
    assert admitted["owner"] == OWNER and admitted["plan_ref"] == plan_ref
    assert admitted["release_authority"] == lr.RELEASE_AUTHORITY
    assert admitted["usage"] == {"fit_reservations": 0, "reserved_units": 0, "consumed_units": None}
    drive_to_review(site, worker, scheduler)
    status = service.status(slack, admitted["run_ref"])
    assert status["state"] == "review_ready" and status["next_actor"] == "reviewer"
    assert status["evidence_refs"] == ["candidate-1"]
    assert status["progress"] == {"completed": 4, "total": 4}
    assert status["usage"] == {"fit_reservations": 1, "reserved_units": 90, "consumed_units": 42}
    # Run, reservation and submission counts.
    assert site.counts()["lr_runs"] == 1
    assert len(site.ledger.reservations) == 1 and site.ledger.reserve_calls == 1
    assert site.trainer.submissions == 1
    assert set(status) - {"usage"} <= {
        "contract",
        "run_ref",
        "state",
        "stage",
        "operation",
        "effective_operation",
        "reason_code",
        "next_action",
        "next_actor",
        "owner",
        "plan_ref",
        "workflow_ref",
        "progress",
        "evidence_refs",
        "cancellation",
        "release_authority",
    }


def test_slack_zapier_and_schedule_converge_on_one_run_and_one_reservation(site):
    slack, slack_service = site.open(site.authorize("slack-owner"))
    zap, zap_service = site.open(site.authorize("zap-owner", channel="zapier"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    plan_ref = site.publish()
    tick = worker.schedule_tick(scheduler)
    assert tick["tick"] == "admitted"
    from_slack = submit(slack_service, slack, plan_ref, "slack-interaction-1")
    from_zap = submit(zap_service, zap, plan_ref, "zap-trigger-1")
    assert from_slack["run_ref"] == from_zap["run_ref"] == tick["run_ref"]
    # The next tick sees the same intent's run and admits nothing new.
    again = worker.schedule_tick(scheduler)
    assert (again["tick"], again["reason_code"]) == ("no_change", "already_admitted")
    assert again["run_ref"] == tick["run_ref"]
    # A newer plan waits while the earlier run is unfinished.
    site.publish(site.plan("source-v2"))
    assert worker.schedule_tick(scheduler)["reason_code"] == "run_in_progress"
    site.publish(site.plan("source-v1"))
    drive_to_review(site, worker, scheduler)
    assert submit(zap_service, zap, plan_ref, "zap-trigger-2")["state"] == "review_ready"
    counts = site.counts()
    assert counts["lr_runs"] == 1 and counts["lr_triggers"] == 4
    assert len(site.ledger.reservations) == 1 and site.trainer.submissions == 1
    with sqlite3.connect(site.directory / lr.DATABASE) as db:
        channels = {row[0] for row in db.execute("SELECT channel FROM lr_triggers")}
    assert channels == {"slack", "zapier", "schedule"}


def test_tick_racing_a_slack_run_improvement_admits_one_run(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    plan_ref = site.publish()
    clicked, ticked = race(
        lambda: submit(service, slack, plan_ref, "click"),
        lambda: worker.schedule_tick(scheduler),
    )
    assert ticked["run_ref"] == clicked["run_ref"]
    assert ticked["tick"] in ("admitted", "no_change")
    assert site.counts()["lr_runs"] == 1


def test_paused_mandate_admits_nothing_and_fences_queued_runs(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    queued = submit(service, slack, site.publish(), "before-pause")
    assert queued["state"] == "queued"
    site.pause()
    # Status shows the fence without writing it.
    status = service.status(slack, queued["run_ref"])
    assert (status["state"], status["reason_code"]) == ("refused", "standing_mandate_paused")
    assert status["next_action"] == "resume_mandate" and site.runs()[0][1] == "queued"
    held = submit(service, slack, site.publish(site.plan("source-v2")), "after-pause")
    assert held["state"] == "held" and "run_ref" not in held
    assert held["reason_code"] == "standing_mandate_paused"
    tick = worker.schedule_tick(scheduler)
    assert (tick["tick"], tick["reason_code"]) == ("held", "standing_mandate_paused")
    assert worker.claim(scheduler) is None
    assert site.runs() == [(queued["run_ref"], "refused", "standing_mandate_paused")]
    assert site.counts()["lr_runs"] == 1 and site.ledger.reserve_calls == 0


def test_cap_of_one_fit_with_two_eligible_residuals_gives_one_fit_and_one_hold(site):
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    site.publish()
    assert worker.schedule_tick(scheduler)["tick"] == "admitted"
    claim = worker.claim(scheduler)
    first = worker.reserve_fit(
        scheduler, claim, residual_ref="residual-1", model_digest="base-digest-1", max_units=90
    )
    assert first["state"] == "reserved"
    key = worker.begin_submission(scheduler, claim)
    worker.record_submission(scheduler, claim, execution_ref=site.trainer.submit(key))
    worker.finish(scheduler, claim, "completed", consumed_units=40)
    # A second eligible residual arrives on a new verified source revision.
    site.publish(site.plan("source-v2"))
    second = worker.schedule_tick(scheduler)
    assert second["tick"] == "admitted"
    claim = worker.claim(scheduler)
    worker.advance(scheduler, claim, stage="fit")
    held = worker.reserve_fit(
        scheduler, claim, residual_ref="residual-2", model_digest="base-digest-1", max_units=90
    )
    assert held == {"state": "held", "reason_code": "standing_fit_budget_exhausted"}
    worker.finish(scheduler, claim, "completed", reason_code="standing_fit_budget_exhausted")
    assert len(site.ledger.reservations) == 1 and site.trainer.submissions == 1
    with sqlite3.connect(site.directory / lr.DATABASE) as db:
        holds = db.execute(
            "SELECT run_ref FROM lr_events WHERE kind='fit_held' "
            "AND reason_code='standing_fit_budget_exhausted'"
        ).fetchall()
        usage = db.execute("SELECT count(fit_reservation) FROM lr_runs").fetchone()[0]
    assert holds == [(second["run_ref"],)] and usage == 1


def test_schedule_reads_fit_count_and_records_a_hold_once_the_cap_is_spent(tmp_path):
    site = Site(tmp_path)
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    site.publish()
    worker.schedule_tick(scheduler)
    claim = worker.claim(scheduler)
    # The real standing mandate records the fit reservation; the tick rereads that usage.
    run = site.registry.authorize_source(
        MANDATE,
        {
            "source_revision": "a" * 64,
            "trace_sha256": "b" * 64,
            "correction_sha256": "c" * 64,
            "workflow": WORKFLOW,
            "connector": "replace-with-approved-readonly-mcp-connection",
            "trace_source": "workflow-trace",
            "correction_source": "verified-outcomes",
            "field": "disposition",
            "fields": ["description", "event_type"],
            "rows": 4,
        },
        actor=DELEGATE,
    )
    site.registry.confirm_class(
        run["run_id"],
        actor=DELEGATE,
        discovery_revision="d" * 64,
        candidate={
            "from_label": "purchase",
            "to_label": "loan",
            "class_id": "class-" + mandate_module.digest(["purchase", "loan"])[:24],
            "count": 2,
            "groups": 2,
        },
    )
    site.registry.reserve(
        MANDATE,
        {
            "run_id": run["run_id"],
            "plan_revision": "1" * 64,
            "intake_revision": "2" * 64,
            "residual_revision": "3" * 64,
            "max_seconds": 90,
        },
        actor=DELEGATE,
    )
    worker.finish(scheduler, claim, "completed")
    site.publish(site.plan("source-v2"))
    tick = worker.schedule_tick(scheduler)
    assert (tick["tick"], tick["reason_code"]) == ("held", "standing_fit_budget_exhausted")
    assert site.counts()["lr_runs"] == 1


def test_replayed_key_with_changed_intent_conflicts_and_double_click_converges(site):
    slack, service = site.open(site.authorize("slack-owner"))
    plan_ref = site.publish()
    first = submit(service, slack, plan_ref, "click")
    assert submit(service, slack, plan_ref, "click") == first
    assert submit(service, slack, plan_ref, "second-click")["run_ref"] == first["run_ref"]
    with pytest.raises(lr.LearningRunError, match="idempotency_conflict"):
        submit(service, slack, plan_ref, "click", operation="benchmark_only")
    benchmark = submit(service, slack, plan_ref, "benchmark", operation="benchmark_only")
    assert benchmark["run_ref"] != first["run_ref"] and benchmark["state"] == "benchmark_only"
    assert site.counts()["lr_runs"] == 2


def test_restart_after_a_lost_reply_returns_the_same_run(site):
    selection = site.authorize("slack-owner")
    slack, service = site.open(selection)
    plan_ref = site.publish()
    admitted = submit(service, slack, plan_ref, "lost-reply")
    del service
    slack, restarted = site.open(selection)
    assert submit(restarted, slack, plan_ref, "lost-reply") == admitted
    assert (
        submit(restarted, slack, plan_ref, "retry-with-new-key")["run_ref"] == admitted["run_ref"]
    )
    assert site.counts()["lr_runs"] == 1


def test_wrong_role_cross_tenant_and_foreign_workflow_are_refused(tmp_path):
    site = Site(tmp_path)
    other = Site(tmp_path, tenant="org-b")
    slack, service = site.open(site.authorize("slack-owner"))
    reviewer, reviewer_service = site.open(
        site.authorize("slack-reviewer", role="reviewer", actor="reviewer")
    )
    operator, operator_service = site.open(
        site.authorize("slack-operator", role="operator", actor="custodian")
    )
    assert not site.authorize("other-flow", workflow="workflow-other", expected_exit=2).exists()
    plan_ref = site.publish()
    run = submit(service, slack, plan_ref, "click")["run_ref"]
    for principal, handler in ((reviewer, reviewer_service), (operator, operator_service)):
        with pytest.raises(lr.LearningRunError, match="learning_run_role_required"):
            submit(handler, principal, plan_ref, "not-allowed")
        with pytest.raises(lr.LearningRunError, match="learning_run_role_required"):
            handler.cancel(principal, run, "not-allowed")
    # The reviewer reads status without aggregate usage; the operator sees usage.
    assert "usage" not in reviewer_service.status(reviewer, run)
    assert "usage" in operator_service.status(operator, run)
    other_slack, other_service = other.open(other.authorize("slack-owner"))
    with pytest.raises(lr.LearningRunError, match="learning_run_unavailable"):
        other_service.status(other_slack, run)
    held = submit(other_service, other_slack, plan_ref, "foreign-plan")
    assert held["state"] == "held" and held["reason_code"] in ("no_eligible_plan", "plan_stale")
    # An owner grant for someone who is not the mandate owner cannot start or read.
    impostor, impostor_service = site.open(site.authorize("impostor", role="operator", actor="x"))
    with pytest.raises(lr.LearningRunError, match="learning_run_role_required"):
        submit(impostor_service, impostor, plan_ref, "impostor")
    assert site.counts()["lr_runs"] == 1 and other.counts()["lr_runs"] == 0


def test_withdrawn_or_expired_grant_reveals_nothing(site, monkeypatch):
    selection = site.authorize("slack-owner")
    slack, service = site.open(selection)
    plan_ref = site.publish()
    run = submit(service, slack, plan_ref, "click")["run_ref"]
    assert lr.main(["withdraw", "--selection", str(selection), "--owner", OWNER]) == 0
    for operation in (
        lambda: submit(service, slack, plan_ref, "click"),
        lambda: service.status(slack, run),
        lambda: service.cancel(slack, run, "cancel"),
    ):
        with pytest.raises(lr.LearningRunError, match="learning_run_unavailable"):
            operation()
    with pytest.raises(lr.LearningRunError):
        site.open(selection)
    expiring = site.authorize("expiring", expires_at=int(time.time()) + 60)
    principal, expiring_service = site.open(expiring)
    monkeypatch.setattr(lr, "time", LaterClock)
    with pytest.raises(lr.LearningRunError, match="learning_run_expired"):
        expiring_service.status(principal, run)
    assert site.counts()["lr_runs"] == 1


def test_owner_cannot_authorize_a_grant_wider_than_the_role(site, capsys):
    selection = site.directory / "wide.json"
    argv = [
        "authorize",
        "--selection",
        str(selection),
        "--tenant",
        TENANT,
        "--owner",
        OWNER,
        "--actor",
        "reviewer",
        "--role",
        "reviewer",
        "--channel",
        "slack",
        "--client",
        "c",
        "--workflow",
        WORKFLOW,
        "--mandate",
        MANDATE,
        "--grant-ref",
        "wide",
        "--expires-at",
        str(int(time.time()) + 60),
        "--permission",
        "start",
    ]
    assert lr.main(argv) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "learning_run_role_required"
    argv[argv.index("--owner") + 1] = "not-the-owner"
    argv[-2:] = ["--permission", "read"]
    assert lr.main(argv) == 2
    assert not selection.exists()
    scheduler_argv = argv.copy()
    scheduler_argv[scheduler_argv.index("--owner") + 1] = OWNER
    scheduler_argv[scheduler_argv.index("--role") + 1] = "scheduler"
    scheduler_argv[scheduler_argv.index("--channel") + 1] = "schedule"
    assert lr.main(scheduler_argv) == 2  # the scheduler actor must be the mandate delegate


def test_revoked_or_renewed_mandate_fences_queued_intents_before_dispatch(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    queued = submit(service, slack, site.publish(), "click")
    site.registry.withdraw(MANDATE, actor=OWNER)
    assert worker.claim(scheduler) is None
    assert site.runs() == [(queued["run_ref"], "refused", "standing_mandate_withdrawn")]
    successor = site.registry.reauthorize(MANDATE, site.spec("standing-2"), actor=OWNER)
    renewed = submit(service, slack, site.publish(site.plan(receipt=successor)), "after-renewal")
    assert renewed["state"] == "queued" and renewed["run_ref"] != queued["run_ref"]
    old = service.status(slack, queued["run_ref"])
    assert old["state"] == "refused" and site.ledger.reserve_calls == 0


def test_running_fit_stops_when_the_mandate_is_withdrawn(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    submit(service, slack, site.publish(), "click")
    claim = worker.claim(scheduler)
    worker.reserve_fit(
        scheduler, claim, residual_ref="residual-1", model_digest="base-digest-1", max_units=90
    )
    site.registry.withdraw(MANDATE, actor=OWNER)
    with pytest.raises(lr.LearningRunError, match="standing_mandate_withdrawn"):
        worker.begin_submission(scheduler, claim)
    worker.finish(
        scheduler, claim, "refused", reason_code="standing_mandate_withdrawn", consumed_units=0
    )
    assert site.trainer.submissions == 0 and site.ledger.settled


def test_cancellation_is_idempotent_and_confirms_only_after_settlement(site):
    site.ledger.max_fits = 5
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    # Queued with no backend work: confirmed at once, nothing consumed.
    queued = submit(service, slack, site.publish(), "click")["run_ref"]
    first = service.cancel(slack, queued, "cancel-1")
    assert (first["cancellation"], first["state"]) == ("confirmed", "canceled")
    assert service.cancel(slack, queued, "cancel-1") == first
    assert service.cancel(slack, queued, "cancel-2") == first
    second = submit(service, slack, site.publish(site.plan("source-v2")), "click-2")["run_ref"]
    with pytest.raises(lr.LearningRunError, match="idempotency_conflict"):
        service.cancel(slack, second, "cancel-1")
    # Running with a live worker: pending until the worker stops and the ledger settles.
    claim = worker.claim(scheduler)
    worker.reserve_fit(
        scheduler, claim, residual_ref="residual-1", model_digest="base-digest-1", max_units=90
    )
    pending = service.cancel(slack, second, "cancel-3")
    assert (pending["cancellation"], pending["state"]) == ("pending", "cancellation_pending")
    with pytest.raises(lr.LearningRunError, match="learning_run_cancel_requested"):
        worker.advance(scheduler, claim, stage="fit")
    with pytest.raises(lr.LearningRunError, match="learning_run_cancel_requested"):
        worker.finish(scheduler, claim, "completed", consumed_units=5)
    site.ledger.available = False
    with pytest.raises(lr.LearningRunError, match="fit_ledger_unavailable"):
        worker.confirm_cancel(scheduler, claim, consumed_units=7)
    assert service.status(slack, second)["state"] == "cancellation_pending"
    site.ledger.available = True
    # The lease was released before the ledger call; the cancel retry settles once.
    confirmed = service.cancel(slack, second, "cancel-3")
    assert (confirmed["cancellation"], confirmed["state"]) == ("confirmed", "canceled")
    status = service.status(slack, second)
    assert status["usage"]["consumed_units"] == 7 and status["next_action"] == "none"
    assert list(site.ledger.settled.values()) == [7]
    assert site.trainer.submissions == 0
    # Cancellation never reaches into finished work or release.
    done = submit(service, slack, site.publish(site.plan("source-v3")), "click-3")["run_ref"]
    drive_to_review(site, worker, scheduler)
    late = service.cancel(slack, done, "cancel-4")
    assert (late["cancellation"], late["state"]) == ("not_applicable", "review_ready")


def race(*actions):
    start = threading.Barrier(len(actions))
    results = [None] * len(actions)

    def runner(index, action):
        start.wait()
        results[index] = action()

    threads = [
        threading.Thread(target=runner, args=(index, action))
        for index, action in enumerate(actions)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    return results


def test_cancel_racing_claim_and_reservation_never_dispatches_after_cancel(tmp_path):
    # Runs that were dispatched count against max_runs even when canceled, so allow six.
    site = Site(tmp_path, max_runs=6)
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    for attempt in range(6):
        plan_ref = site.publish(site.plan(f"source-{attempt}"))
        run = submit(service, slack, plan_ref, f"k{attempt}")["run_ref"]
        key = f"cancel-{attempt}"
        _, claim = race(
            lambda run=run, key=key: service.cancel(slack, run, key),
            lambda: worker.claim(scheduler),
        )
        if claim is not None:
            # The claim won: any further effect sees the cancellation and stops.
            with pytest.raises(lr.LearningRunError, match="learning_run_cancel_requested"):
                worker.reserve_fit(
                    scheduler, claim, residual_ref="r", model_digest="m", max_units=9
                )
            worker.confirm_cancel(scheduler, claim, consumed_units=0)
        assert service.status(slack, run)["state"] == "canceled"
    assert site.ledger.reserve_calls == 0 and site.trainer.submissions == 0


def test_uncertain_trainer_submission_is_reconciled_before_any_retry(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    run = submit(service, slack, site.publish(), "click")["run_ref"]
    claim = worker.claim(scheduler)
    worker.reserve_fit(
        scheduler, claim, residual_ref="residual-1", model_digest="base-digest-1", max_units=90
    )
    key = worker.begin_submission(scheduler, claim)
    site.trainer.submit(key)  # the trainer accepted the job; the acknowledgment was lost
    worker.record_submission(scheduler, claim, execution_ref=None, uncertain=True)
    status = service.status(slack, run)
    assert (status["state"], status["reason_code"]) == ("held", "trainer_submission_uncertain")
    assert status["next_action"] == "reconcile_trainer_submission"
    with pytest.raises(lr.LearningRunError, match="trainer_submission_uncertain"):
        worker.begin_submission(scheduler, claim)
    with pytest.raises(lr.LearningRunError, match="trainer_submission_uncertain"):
        worker.finish(scheduler, claim, "completed", consumed_units=1)
    worker.reconcile_submission(scheduler, claim, execution_ref=site.trainer.find(key))
    assert service.status(slack, run)["state"] == "running"
    with pytest.raises(lr.LearningRunError, match="trainer_job_exists"):
        worker.begin_submission(scheduler, claim)
    assert site.trainer.submissions == 1 and len(site.ledger.reservations) == 1


def test_worker_restart_after_a_submission_attempt_holds_until_reconciled(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    clock = [time.time()]
    worker.clock = lambda: clock[0]
    run = submit(service, slack, site.publish(), "click")["run_ref"]
    claim = worker.claim(scheduler)
    worker.reserve_fit(
        scheduler, claim, residual_ref="residual-1", model_digest="base-digest-1", max_units=90
    )
    worker.begin_submission(scheduler, claim)  # the worker dies before the trainer answers
    clock[0] += lr.LEASE_SECONDS + 1
    reclaimed = worker.claim(scheduler)
    assert reclaimed.run_ref == run and reclaimed.lease != claim.lease
    assert reclaimed.submission == "uncertain" and reclaimed.fit_reservation is not None
    with pytest.raises(lr.LearningRunError, match="learning_run_lease_lost"):
        worker.advance(scheduler, claim, stage="fit")
    # The backend confirms no job exists, so the same deterministic key may be submitted once.
    worker.reconcile_submission(scheduler, reclaimed, execution_ref=None)
    key = worker.begin_submission(scheduler, reclaimed)
    worker.record_submission(scheduler, reclaimed, execution_ref=site.trainer.submit(key))
    assert site.trainer.submissions == 1
    assert site.ledger.reserve_calls == 1 and site.ledger.lookup_calls >= 1


def test_zero_fit_outcomes_are_explicit(tmp_path):
    site = Site(tmp_path)
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    requested = submit(service, slack, site.publish(), "benchmark", operation="benchmark_only")
    assert (requested["state"], requested["reason_code"]) == (
        "benchmark_only",
        "benchmark_only_requested",
    )
    claim = worker.claim(scheduler)
    with pytest.raises(lr.LearningRunError, match="fit_outside_run_scope"):
        worker.reserve_fit(scheduler, claim, residual_ref="r", model_digest="m", max_units=9)
    worker.finish(scheduler, claim, "completed")
    route = submit(service, slack, site.publish(site.plan("source-v2", capable=False)), "route")
    assert (route["state"], route["reason_code"]) == (
        "benchmark_only",
        "training_unavailable_for_route",
    )
    claim = worker.claim(scheduler)
    assert claim.run_ref == route["run_ref"] and claim.operation == "benchmark_only"
    worker.finish(scheduler, claim, "completed", evidence_refs=("benchmark-result-1",))
    no_rights = Site(tmp_path, tenant="org-c", train=False)
    principal, rules = no_rights.open(no_rights.authorize("slack-owner"))
    outcome = submit(rules, principal, no_rights.publish(), "click")
    assert (outcome["state"], outcome["reason_code"]) == (
        "benchmark_only",
        "training_outside_mandate",
    )
    no_ledger = Site(tmp_path, tenant="org-d")
    principal, fails_closed = no_ledger.open(no_ledger.authorize("slack-owner"), ledger=None)
    plan_ref = no_ledger.publish()
    assert (
        submit(fails_closed, principal, plan_ref, "click")["reason_code"]
        == "fit_ledger_unavailable"
    )
    assert submit(fails_closed, principal, plan_ref, "b", operation="benchmark_only")["state"] == (
        "benchmark_only"
    )
    # A simple repair that suffices finishes with zero fits and an explicit reason.
    adequate = submit(service, slack, site.publish(site.plan("source-v3")), "adequate")
    claim = worker.claim(scheduler)
    assert claim.run_ref == adequate["run_ref"]
    worker.finish(scheduler, claim, "completed", evidence_refs=("simple-repair-1",))
    final = service.status(slack, adequate["run_ref"])
    assert final["state"] == "completed" and final["usage"]["fit_reservations"] == 0
    assert site.ledger.reserve_calls == 0 and no_ledger.counts()["lr_runs"] == 1


def test_status_reads_and_holds_create_no_effects(site):
    slack, service = site.open(site.authorize("slack-owner"))
    plan_ref = site.publish()
    run = submit(service, slack, plan_ref, "click")["run_ref"]
    before = site.counts()
    for _ in range(3):
        service.status(slack, run)
    assert site.counts() == before
    stale = submit(service, slack, "plan-" + "0" * 40, "stale")
    assert stale["state"] == "held" and stale["reason_code"] == "plan_stale"
    assert site.counts() == before


@pytest.mark.parametrize(
    "extra",
    [
        {"backend_url": "https://trainer.invalid"},
        {"command": "rm"},
        {"examples": "case"},
        {"answer_key": "a"},
        {"release": "approve"},
        {"tenant": "org-b"},
        {"actor": OWNER},
        {"mandate_id": MANDATE},
    ],
)
def test_requests_cannot_carry_backend_command_data_release_or_authority(site, extra):
    slack, service = site.open(site.authorize("slack-owner"))
    call = lr.learning_run_dispatch(slack, service)
    body = {"plan_ref": site.publish(), "operation": "evaluate_and_improve", "idempotency_key": "k"}
    refused = call(lr.SUBMIT, body | extra)
    assert refused["isError"] and refused["content"][0]["text"] == "invalid_arguments"
    assert (
        call(lr.STATUS, {"run_ref": "lrun-x", **extra})["content"][0]["text"] == "invalid_arguments"
    )
    assert site.counts()["lr_runs"] == 0


def test_local_socket_mode_adapter_uses_the_same_handlers(site):
    slack, service = site.open(site.authorize("slack-owner"))
    call = lr.learning_run_dispatch(slack, service)
    plan_ref = site.publish()
    body = {"plan_ref": plan_ref, "operation": "evaluate_and_improve", "idempotency_key": "socket"}
    admitted = call(lr.SUBMIT, body)["structuredContent"]
    assert submit(service, slack, plan_ref, "direct")["run_ref"] == admitted["run_ref"]
    status = call(lr.STATUS, {"run_ref": admitted["run_ref"]})["structuredContent"]
    assert status["state"] == "queued"
    unknown = call(lr.STATUS, {"run_ref": "lrun-unknown"})
    assert unknown["content"][0]["text"] == "learning_run_unavailable"
    receipt = call(lr.CANCEL, {"run_ref": admitted["run_ref"], "idempotency_key": "c"})
    assert receipt["structuredContent"]["cancellation"] == "confirmed"
    assert call("discover_sources", {})["content"][0]["text"] == "invalid_arguments"
    for _ in range(lr.CALLS_PER_MINUTE):
        call(lr.STATUS, {"run_ref": admitted["run_ref"]})
    assert call(lr.STATUS, {"run_ref": admitted["run_ref"]})["content"][0]["text"] == (
        "learning_run_rate_limited"
    )
    scheduler = site.authorize("schedule", role="scheduler", channel="schedule")
    principal, schedule_service = site.open(scheduler)
    with pytest.raises(lr.LearningRunError):
        lr.learning_run_dispatch(principal, schedule_service)


def test_stale_or_missing_plans_are_held_and_ticks_record_no_change(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    assert worker.schedule_tick(scheduler)["reason_code"] == "no_eligible_plan"
    old = site.publish()
    site.publish(site.plan("source-v2"))
    assert submit(service, slack, old, "stale-modal")["reason_code"] == "plan_stale"
    tampered = json.loads(lr.plan_record_path(site.directory, TENANT, WORKFLOW).read_text())
    tampered["plan"]["incumbent_ref"] = "other-incumbent"
    lr._write_private(lr.plan_record_path(site.directory, TENANT, WORKFLOW), tampered)
    assert worker.schedule_tick(scheduler) | {"plan_ref": None} == {
        "contract": lr.CONTRACT,
        "tick": "held",
        "reason_code": "plan_source_unavailable",
        "run_ref": None,
        "plan_ref": None,
        "mandate_revision": site.registry.inspect(MANDATE)["mandate_revision"],
    }
    site.publish(replace(site.plan("source-v3"), mandate_revision="f" * 64))
    assert worker.schedule_tick(scheduler)["reason_code"] == "no_eligible_plan"
    assert site.counts()["lr_runs"] == 0
    assert [outcome for outcome, _ in site.ticks()] == ["no_change", "held", "no_change"]


def test_manual_mandate_admits_nothing(tmp_path):
    site = Site(tmp_path, mode="manual")
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    plan_ref = site.publish()
    assert submit(service, slack, plan_ref, "click")["reason_code"] == "manual_learning_only"
    assert worker.schedule_tick(scheduler)["reason_code"] == "manual_learning_only"
    assert site.counts()["lr_runs"] == 0


class LaterClock:
    """A module-local clock far in the future; the global clock is untouched."""

    @staticmethod
    def time() -> float:
        return float(2**52)


def test_expired_mandate_admits_nothing(site, monkeypatch):
    slack, service = site.open(site.authorize("slack-owner", expires_at=2**53 - 1))
    plan_ref = site.publish()
    monkeypatch.setattr(mandate_module, "time", LaterClock)
    assert submit(service, slack, plan_ref, "click")["reason_code"] == "standing_mandate_expired"
    assert site.counts()["lr_runs"] == 0


def _documented(text: str) -> dict[str, object]:
    def table(heading: str) -> list[list[str]]:
        section = text.split(heading, 1)[1].split("\n\n", 2)[1]
        rows = [line.strip("|").split("|") for line in section.splitlines()[2:]]
        return [[cell.strip().strip("`") for cell in row] for row in rows]

    return {
        "routes": tuple(tuple(row) for row in table("## Routes\n\nThe TLS gateway builds")),
        "errors": {code: int(status) for code, status in table("## Errors")},
        "states": tuple(row[0] for row in table("## States")),
        "roles": {
            role: tuple(
                re.findall(r"`([a-z]+)`", text.split(f"| `{role}` |", 1)[1].split("\n", 1)[0])
            )
            for role in lr.ROLE_PERMISSIONS
        },
        "holds": {
            reason: (action, None if actor == "-" else actor)
            for reason, action, actor in table("### Holds and their next actions")
        },
    }


def check_documented_vocabulary(text: str) -> None:
    found = _documented(text)
    assert found["routes"] == lr.ROUTES
    assert found["errors"] == lr.ERRORS
    assert found["states"] == lr.STATES
    assert found["roles"] == lr.ROLE_PERMISSIONS
    for reason, expected in found["holds"].items():
        assert lr.REASONS[reason] == expected


def test_documented_vocabulary_matches_the_module_and_detects_drift():
    text = DOC.read_text()
    check_documented_vocabulary(text)
    for before, after in (
        ("| `cancellation_pending` |", "| `cancel_pending` |"),
        ("| `idempotency_conflict` | 409 |", "| `idempotency_conflict` | 400 |"),
        ("`/v1/learning-runs/{run_ref}/cancel`", "`/v1/learning-runs/{run_ref}/abort`"),
        ("| `reviewer` | `read` |", "| `reviewer` | `read`, `cancel` |"),
        ("| `plan_stale` | `refresh_plan` | owner |", "| `plan_stale` | `retry` | owner |"),
    ):
        assert before in text
        with pytest.raises(AssertionError):
            check_documented_vocabulary(text.replace(before, after))
    # The gateway derives its exact request lines from the same table.
    lines = [pattern.pattern for pattern, _ in lr.http_routes()]
    assert len(lines) == 3 and all(line.endswith(rb" HTTP/1\.1") for line in lines)
    assert lr.http_routes()[1][0].fullmatch(b"GET /v1/learning-runs/lrun-1 HTTP/1.1")
    assert not lr.http_routes()[1][0].fullmatch(b"GET /v1/learning-runs/../x HTTP/1.1")


def worker_once(directory: Path) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        "-B",
        "-m",
        "opentour_cell.pilot.cycle_worker",
        "--directory",
        str(directory),
        "--organization",
        TENANT,
        "--config",
        str(directory / "cycle.json"),
        "--interval",
        "300",
        "--once",
    ]
    return subprocess.run(command, capture_output=True, text=True, timeout=60, check=False)


def test_cycle_worker_runs_the_learning_tick_once_the_owner_opted_in(tmp_path):
    site = Site(tmp_path, train=False)
    site.authorize("learning-run-schedule", role="scheduler", channel="schedule")
    site.publish()
    first = worker_once(site.directory)
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert (report["state"], report["reason"]) == ("admitted", "benchmark_only_requested")
    again = worker_once(site.directory)
    assert json.loads(again.stdout) == {
        "state": "no_change",
        "reason": "already_admitted",
        "run_id": report["run_id"],
    }
    site.pause()
    paused = worker_once(site.directory)
    assert paused.returncode == 2
    assert json.loads(paused.stdout) == {
        "state": "held",
        "reason": "standing_mandate_paused",
        "run_id": None,
    }
    assert site.runs() == [(report["run_id"], "refused", "standing_mandate_paused")]


def test_installed_tick_holds_fit_capable_work_until_a_ledger_is_attached(tmp_path):
    site = Site(tmp_path)
    site.authorize("learning-run-schedule", role="scheduler", channel="schedule")
    site.publish()
    held = worker_once(site.directory)
    assert held.returncode == 2
    assert json.loads(held.stdout) == {
        "state": "held",
        "reason": "fit_ledger_unavailable",
        "run_id": None,
    }
    assert site.counts()["lr_runs"] == 0
    # Without the owner's scheduler selection the unit keeps its existing direct cycle.
    (site.directory / lr.SCHEDULE_SELECTION).unlink()
    legacy = worker_once(site.directory)
    assert legacy.returncode == 2 and not legacy.stdout
    assert json.loads(legacy.stderr)["reason"] == "check_private_configuration_and_source"


def test_abandoned_fit_under_a_revoked_mandate_is_reclaimed_and_settled(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    clock = [time.time()]
    worker.clock = lambda: clock[0]
    run = submit(service, slack, site.publish(), "click")["run_ref"]
    queued = submit(service, slack, site.publish(site.plan("source-v2")), "later")["run_ref"]
    claim = worker.claim(scheduler)
    worker.reserve_fit(
        scheduler, claim, residual_ref="residual-1", model_digest="base-digest-1", max_units=90
    )
    key = worker.begin_submission(scheduler, claim)
    worker.record_submission(scheduler, claim, execution_ref=site.trainer.submit(key))
    # The worker dies mid-fit, then the owner revokes the mandate.
    site.registry.withdraw(MANDATE, actor=OWNER)
    clock[0] += lr.LEASE_SECONDS + 1
    reclaimed = worker.claim(scheduler)
    assert reclaimed is not None and reclaimed.run_ref == run
    assert reclaimed.execution_ref == site.trainer.find(key)
    with pytest.raises(lr.LearningRunError, match="standing_mandate_withdrawn"):
        worker.advance(scheduler, reclaimed, stage="fit")
    worker.finish(
        scheduler, reclaimed, "refused", reason_code="standing_mandate_withdrawn", consumed_units=30
    )
    assert service.status(slack, run)["usage"]["consumed_units"] == 30
    # The queued run was fenced, never dispatched, and nothing else can be claimed.
    assert service.status(slack, queued)["state"] == "refused"
    assert worker.claim(scheduler) is None
    assert site.trainer.submissions == 1 and list(site.ledger.settled.values()) == [30]


def test_stopped_trainer_job_settles_after_a_ledger_outage(site):
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    run = submit(service, slack, site.publish(), "click")["run_ref"]
    claim = worker.claim(scheduler)
    worker.reserve_fit(
        scheduler, claim, residual_ref="residual-1", model_digest="base-digest-1", max_units=90
    )
    key = worker.begin_submission(scheduler, claim)
    worker.record_submission(scheduler, claim, execution_ref=site.trainer.submit(key))
    assert service.cancel(slack, run, "cancel")["cancellation"] == "pending"
    site.ledger.available = False
    with pytest.raises(lr.LearningRunError, match="fit_ledger_unavailable"):
        worker.confirm_cancel(scheduler, claim, consumed_units=11)
    assert service.status(slack, run)["state"] == "cancellation_pending"
    site.ledger.available = True
    # No worker is needed any more: the schedule tick (or a cancel retry) settles it.
    tick = worker.schedule_tick(scheduler)
    assert tick["tick"] == "no_change"
    status = service.status(slack, run)
    assert (status["state"], status["usage"]["consumed_units"]) == ("canceled", 11)
    assert service.cancel(slack, run, "cancel")["cancellation"] == "confirmed"
    assert site.trainer.submissions == 1 and list(site.ledger.settled.values()) == [11]


def test_run_cap_counts_the_whole_renewal_lineage(tmp_path):
    site = Site(tmp_path, max_runs=1)
    slack, service = site.open(site.authorize("slack-owner"))
    scheduler, worker = site.open(site.authorize("schedule", role="scheduler", channel="schedule"))
    submit(service, slack, site.publish(), "click", operation="benchmark_only")
    worker.finish(scheduler, worker.claim(scheduler), "completed")
    site.registry.withdraw(MANDATE, actor=OWNER)
    successor = site.registry.reauthorize(MANDATE, site.spec("standing-2", max_runs=1), actor=OWNER)
    renewed = submit(
        service,
        slack,
        site.publish(site.plan(receipt=successor)),
        "after-renewal",
        operation="benchmark_only",
    )
    assert renewed["state"] == "held" and renewed["reason_code"] == "standing_run_budget_exhausted"
    assert site.counts()["lr_runs"] == 1


def test_second_mandate_on_same_workflow_cannot_read_or_control_prior_run(site):
    owner, original = site.open(site.authorize("slack-original"))
    plan_ref = site.publish()
    run = submit(original, owner, plan_ref, "click")["run_ref"]
    site.registry.enroll(site.spec("other-mandate"), actor=OWNER)
    foreign, service = site.open(
        site.authorize("different-owner-grant", mandate_id="other-mandate")
    )
    for operation in (
        lambda: service.status(foreign, run),
        lambda: service.cancel(foreign, run, "cancel"),
        lambda: submit(service, foreign, plan_ref, "retry"),
    ):
        with pytest.raises(lr.LearningRunError):
            operation()
    assert original.status(owner, run)["state"] == "queued"


def test_read_only_preview_resolves_exact_plan_and_latest_authorized_run(site):
    path = site.authorize("slack-owner")
    owner, service = site.open(path, ledger=None)
    plan_ref = site.publish()
    before = service.preview(owner)
    assert before["plan_ref"] == plan_ref and before["can_benchmark"] and not before["can_improve"]
    assert before["current_run"] is None and site.counts()["lr_runs"] == 0
    run = service.submit(owner, lr.SubmitRequest(plan_ref, "benchmark_only", "preview-start"))
    _, restarted = site.open(path, ledger=None)
    assert restarted.preview(owner)["current_run"] == run["run_ref"]
    assert site.counts()["lr_runs"] == 1


def test_missing_mandate_after_admission_denies_disclosure_cancellation_and_replay(site):
    owner, service = site.open(site.authorize("private-owner"))
    ref = site.publish()
    run = submit(service, owner, ref, "first")["run_ref"]

    def counts():
        with sqlite3.connect(site.directory / lr.DATABASE) as db:
            return tuple(
                db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in (
                    "lr_runs",
                    "lr_keys",
                    "lr_triggers",
                    "lr_events",
                    "lr_cancellations",
                )
            )

    before = counts()
    site.registry.path.rename(site.directory / "unavailable-mandate.sqlite3")
    for operation in (
        lambda: service.status(owner, run),
        lambda: service.cancel(owner, run, "cancel"),
    ):
        with pytest.raises(lr.LearningRunError, match="learning_run_unavailable"):
            operation()
    held = submit(service, owner, ref, "retry")
    assert held["reason_code"] == "standing_mandate_unavailable" and "run_ref" not in held
    assert counts() == before
