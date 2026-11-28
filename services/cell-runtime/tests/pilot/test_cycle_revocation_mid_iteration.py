"""OPE-577 item 4: authority revoked while one installed cycle iteration is in progress.

Every case drives the packaged entry point (`cycle_worker.run`, which calls the real
`autonomous_cycle.cycle_once` with its default `workplace_context` connector). Only two seams
are generated: the workplace provider's two network calls are answered by the generated
catalog from `test_autonomous_cycle`, and the numeric trainer subprocess (`open_weight._run`)
is a generated transport. Everything between them, including the open-weight bundle logic, the
mandate ledger, the engagement store and the Slack queue, is the shipped code. The stage hooks
below wrap shipped functions only to inject the revocation; each still calls the original.

The revocation lands at one stage of the iteration:

* `source_read`: while the provider read is in flight (after it returned bytes, before the
  cycle admits them);
* `intake_to_fit`: after the intake is frozen and inspected, before any fit permission;
* `during_fit`: requested while the trainer holds the fit's dispatch guard, committed as soon
  as the guard allows and before the trainer's result returns;
* `after_fit`: after the managed comparison (and fit) returned, before the Slack decision;
* `before_enqueue`: immediately before `approval_inbox.enqueue_ready` is called;
* `inside_enqueue` (mandate only): after `enqueue_ready`'s own authority read, before its
  pointer write. This is a known gap, pinned by a strict xfail (see `ENQUEUE_GAP`).

Three authorities are revoked: the standing mandate (owner withdrawal), the engagement
(store withdrawal) and the owner's source grant (source withdrawal). Fixtures are generated
sentinels; the trainer is simulated, so this proves the package, not a customer host.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import (
    approval_inbox,
    cycle_worker,
    open_weight,
    repair_workflow,
    source_intake,
    workplace_context,
)
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.training import LocalTrainingStore
from test_autonomous_cycle import Customer, read

Json = dict[str, Any]
KINDS = ("mandate", "engagement", "source_grant")
STAGES = ("source_read", "intake_to_fit", "during_fit", "after_fit", "before_enqueue")
EFFECTS = (
    "approval-ready-*.json",
    "**/release-decision.json",
    "slack-review-*.json",
    "model-grant-*.json",
    "managed-outputs/run-*/run.json",
    "managed-outputs/run-*/review.json",
    "managed-outputs/run-*/model/manifest.json",
)
# Every reason a held iteration may give once that authority is gone. Anything else would
# mean the iteration stopped for an unrelated cause and proved nothing about revocation.
REASONS = {
    "mandate": {"standing_mandate_unavailable", "standing_mandate_withdrawn"},
    "engagement": {
        "engagement_unavailable",
        "engagement_withdrawn",
        "standing_engagement_unavailable",
    },
    "source_grant": {"source_grant_withdrawn"},
}


class Iteration:
    """One customer, its generated seams and the record of what happened after revocation."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, *, model: bool, kind: str):
        self.c = Customer(root, model=model)
        self.kind = kind
        self.calls: list[str] = []
        self.after_revocation: list[str] = []
        self.revoked_at: frozenset[str] | None = None
        self.threads: list[threading.Thread] = []
        self.blocked_by_guard: bool | None = None
        self.stage: str | None = None
        self.armed = True
        catalog = self.c.connector
        monkeypatch.setattr(workplace_context, "discover_workplace", catalog.discover_workplace)
        monkeypatch.setattr(workplace_context, "acquire_workplace", self._acquire)
        monkeypatch.setattr(open_weight, "_run", self._trainer)
        self.attestation = read(self.c.owner / "source-grant.json")["owner_attestation"]

    # -- revocation ---------------------------------------------------------------------

    def _store(self) -> LocalTrainingStore:
        return LocalTrainingStore(self.c.owner / "training.sqlite3", self.c.org, existing_only=True)

    def _revoke_now(self) -> None:
        if self.kind == "mandate":
            receipt = LearningMandates(self.c.owner, self.c.org, self.c.eng).withdraw(
                self.c.mandate, actor="generated-owner"
            )
            assert receipt["withdrawn"] is True and receipt["pending_fit_destinations"] == 0
        elif self.kind == "engagement":
            assert self._store().withdraw()["status"] == "withdrawn"
        else:
            source_intake.withdraw_intake(self._store(), self.attestation)

    def denial_is_durable(self) -> bool:
        """Read the denial back through fresh handles, as a restarted worker would."""
        if self.kind == "mandate":
            return bool(
                LearningMandates(self.c.owner, self.c.org, self.c.eng).inspect(self.c.mandate)[
                    "withdrawn"
                ]
            )
        if self.kind == "engagement":
            with closing(
                sqlite3.connect((self.c.owner / "training.sqlite3").as_uri() + "?mode=ro", uri=True)
            ) as db:
                (withdrawn,) = db.execute(
                    "SELECT withdrawn_at FROM engagement WHERE singleton=1"
                ).fetchone()
            return withdrawn is not None
        return (self.c.owner / ("source-withdrawn-" + self.attestation + ".json")).exists()

    def _mark_revoked(self) -> None:
        assert self.denial_is_durable()
        self.revoked_at = self.effects()

    def revoke(self, stage: str) -> None:
        if self.armed and self.stage == stage:
            self.armed = False
            self._revoke_now()
            self._mark_revoked()

    # -- generated seams ---------------------------------------------------------------

    def _acquire(self, *args: Any, **kwargs: Any) -> Json:
        acquired = self.c.connector.acquire_workplace(*args, **kwargs)
        self.revoke("source_read")
        return acquired

    def _trainer(
        self,
        bundle: Path,
        model_dir: Any,
        grant: Json,
        python: Any,
        request: Json,
        *,
        check_permission: Callable[[], None],
        dispatch: Callable[[], Any],
    ) -> Json:
        check_permission()
        operation = request["operation"]
        with dispatch():
            self.calls.append(operation)
            if self.revoked_at is not None:
                self.after_revocation.append(operation)
            if operation == "inspect_installation":
                return {
                    "profile": open_weight.PROFILE,
                    "checkpoint_revision": open_weight.REVISION,
                    "verified_base_sha256": open_weight.BASE_SHA256,
                    "verified_tokenizer_sha256": open_weight.TOKENIZER_SHA256,
                }
            if operation == "predict":
                return {
                    "decisions": [
                        {
                            "status": "automatic",
                            "label": "refund" if "returned" in row["description"] else "sale",
                        }
                        for row in request["examples"]
                    ]
                }
            assert operation == "fit"
            revoking = self.armed and self.stage == "during_fit"
            if revoking:
                self.armed = False
                worker = threading.Thread(target=self._revoke_now, daemon=True)
                self.threads.append(worker)
                worker.start()
                worker.join(0.3)
                # Engagement and source withdrawal serialize behind the fit's dispatch guard.
                self.blocked_by_guard = not self.denial_is_durable()
            fitted = {"adapter": {"a": "YQ=="}, "pid": 1, "elapsed_ms": 1700}
        if revoking:
            deadline = time.monotonic() + 10
            while not self.denial_is_durable():
                assert time.monotonic() < deadline, "revocation_never_committed"
                time.sleep(0.02)
            self._mark_revoked()
        return fitted

    # -- observation -------------------------------------------------------------------

    def effects(self) -> frozenset[str]:
        found = {str(p.relative_to(self.c.owner)) for g in EFFECTS for p in self.c.owner.glob(g)}
        with closing(sqlite3.connect(self.c.ledger.path.as_uri() + "?mode=ro", uri=True)) as db:
            for rid, state, settlement in db.execute(
                "SELECT id, state, settlement FROM reservations"
            ):
                found.add(f"reservation:{rid}:{state}")
                if settlement and json.loads(settlement)["artifact_sha256"]:
                    found.add(f"settled-artifact:{rid}")
        return frozenset(found)

    def worker(self, capsys: pytest.CaptureFixture[str]) -> tuple[int, Json]:
        """One supervised iteration through the packaged cycle worker."""
        code = cycle_worker.run(
            self.c.owner,
            self.c.org,
            self.c.owner / "cycle.json",
            5,
            once=True,
            stopped=threading.Event(),
        )
        out = capsys.readouterr().out.strip().splitlines()
        assert len(out) == 1, out
        return code, json.loads(out[0])

    def join(self) -> None:
        for worker in self.threads:
            worker.join(15)
            assert not worker.is_alive()


@pytest.fixture
def hooks(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Iteration]]:
    made: list[Iteration] = []

    def make(root: Path, *, model: bool, kind: str, stage: str) -> Iteration:
        it = Iteration(root, monkeypatch, model=model, kind=kind)
        it.stage = stage
        inspect, execute, enqueue, evidence = (
            source_intake.inspect_intake,
            repair_workflow.execute_managed,
            approval_inbox.enqueue_ready,
            approval_inbox.managed_review_evidence,
        )

        def inspected(*args: Any, **kwargs: Any) -> Json:
            result = inspect(*args, **kwargs)
            it.revoke("intake_to_fit")
            return result

        def executed(*args: Any, **kwargs: Any) -> Json:
            result = execute(*args, **kwargs)
            it.revoke("after_fit")
            return result

        def enqueued(*args: Any, **kwargs: Any) -> Json:
            it.revoke("before_enqueue")
            return enqueue(*args, **kwargs)

        def checked(*args: Any, **kwargs: Any) -> Json:
            result = evidence(*args, **kwargs)
            it.revoke("inside_enqueue")
            return result

        monkeypatch.setattr(approval_inbox, "managed_review_evidence", checked)
        monkeypatch.setattr(source_intake, "inspect_intake", inspected)
        monkeypatch.setattr(repair_workflow, "execute_managed", executed)
        monkeypatch.setattr(approval_inbox, "enqueue_ready", enqueued)
        made.append(it)
        return it

    yield make
    for it in made:
        it.join()


def check_held_after_revocation(
    it: Iteration, capsys: pytest.CaptureFixture[str], *, pointer_free: bool = True
) -> None:
    code, report = it.worker(capsys)
    it.join()
    assert it.revoked_at is not None, "the revocation hook never fired"
    # The installed worker reports a hold and exits non-zero for --once.
    assert code == 2 and report["state"] == "held", report
    assert report["reason"] in REASONS[it.kind] | {"open_weight_authority_withdrawn"}, report
    # No fit or model use starts after the revocation committed.
    assert it.after_revocation == [], it.after_revocation
    # No publication, Slack review grant, review pointer or accepted artifact appears after it.
    added = it.effects() - it.revoked_at
    if pointer_free:
        assert not added, sorted(added)
        assert not list(it.c.owner.glob("approval-ready-*.json"))
    else:
        assert all(e.startswith("approval-ready-") for e in added), sorted(added)
    assert not list(it.c.owner.rglob("release-decision.json"))
    # The denial is durable: a fresh handle (a restarted worker) still reads it.
    assert it.denial_is_durable()
    # A later iteration of the same installed worker stays held and does nothing new.
    calls, before = list(it.calls), it.effects()
    later_code, later = it.worker(capsys)
    assert later_code == 2 and later["state"] == "held", later
    assert later["reason"] in REASONS[it.kind] | {"open_weight_authority_withdrawn"}, later
    assert it.calls == calls and it.effects() == before


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("stage", STAGES)
def test_model_route_revocation_mid_iteration_holds(
    tmp_path: Path,
    hooks: Callable[..., Iteration],
    capsys: pytest.CaptureFixture[str],
    stage: str,
    kind: str,
) -> None:
    it = hooks(tmp_path, model=True, kind=kind, stage=stage)
    check_held_after_revocation(it, capsys)
    if stage == "during_fit":
        assert it.calls[-1] == "fit"
        # The fit's result is never published as a model bundle, and the reservation is
        # never settled with an accepted artifact.
        assert not list(it.c.owner.glob("managed-outputs/run-*/model/manifest.json"))
        assert not any(e.startswith("settled-artifact:") for e in it.effects())
        if kind != "mandate":
            assert it.blocked_by_guard is True
        else:
            # Mandate withdrawal signals the claimed fit destination durably.
            assert list(it.c.owner.glob("managed-outputs/run-*/.model.withdrawn"))
    if stage in ("source_read", "intake_to_fit"):
        assert "fit" not in it.calls


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("stage", ("source_read", "intake_to_fit", "after_fit", "before_enqueue"))
def test_rule_route_revocation_mid_iteration_holds(
    tmp_path: Path,
    hooks: Callable[..., Iteration],
    capsys: pytest.CaptureFixture[str],
    stage: str,
    kind: str,
) -> None:
    it = hooks(tmp_path, model=False, kind=kind, stage=stage)
    check_held_after_revocation(it, capsys)
    assert it.calls == []


ENQUEUE_GAP = (
    "approval_inbox.enqueue_ready (approval_inbox.py:51-73) checks the standing mandate "
    "through managed_review_evidence (approval_inbox.py:52) in its own read, then writes the "
    "approval-ready pointer (approval_inbox.py:73) holding only the engagement guard. "
    "LearningMandates.withdraw (learning_mandate.py:873) takes only the mandate database, not "
    "that guard, so a withdrawal can commit between the check and the write. "
    "autonomous_cycle.py:451 re-checks only after the pointer is durable and does not remove it."
)


@pytest.mark.parametrize("model", (True, False), ids=("model", "rule"))
def test_mandate_withdrawn_inside_enqueue_holds_and_pointer_cannot_open_evidence(
    tmp_path: Path,
    hooks: Callable[..., Iteration],
    capsys: pytest.CaptureFixture[str],
    model: bool,
) -> None:
    """Withdrawal after enqueue_ready's authority read: the cycle holds, the pointer is inert."""
    it = hooks(tmp_path, model=model, kind="mandate", stage="inside_enqueue")
    check_held_after_revocation(it, capsys, pointer_free=False)
    # The Slack poster opens every pointer through managed_review_evidence before posting,
    # and that read now refuses the withdrawn mandate. Only this read is exercised here.
    (pointer,) = it.c.owner.glob("approval-ready-*.json")
    item = read(pointer)
    with pytest.raises(ValueError):
        approval_inbox.managed_review_evidence(
            it.c.owner, it.c.org, item["run_id"], item["audience"], item["team"], item["channel"]
        )


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=ENQUEUE_GAP)
@pytest.mark.parametrize("model", (True, False), ids=("model", "rule"))
def test_gap_mandate_withdrawn_inside_enqueue_writes_no_pointer(
    tmp_path: Path,
    hooks: Callable[..., Iteration],
    capsys: pytest.CaptureFixture[str],
    model: bool,
) -> None:
    """Strict xfail: documents a real gap. It is not a pass."""
    it = hooks(tmp_path, model=model, kind="mandate", stage="inside_enqueue")
    code, report = it.worker(capsys)
    assert it.revoked_at is not None and code == 2 and report["state"] == "held", report
    assert not list(it.c.owner.glob("approval-ready-*.json")), "pointer written after withdrawal"


def test_unrevoked_iteration_reaches_the_slack_queue(
    tmp_path: Path, hooks: Callable[..., Iteration], capsys: pytest.CaptureFixture[str]
) -> None:
    """Control: with no revocation the same seams reach the review queue exactly once."""
    it = hooks(tmp_path, model=True, kind="mandate", stage="none")
    code, report = it.worker(capsys)
    assert code == 0 and report["state"] == "pending_slack_review", report
    assert it.calls == ["inspect_installation", "fit", "predict"]
    assert len(list(it.c.owner.glob("approval-ready-*.json"))) == 1
