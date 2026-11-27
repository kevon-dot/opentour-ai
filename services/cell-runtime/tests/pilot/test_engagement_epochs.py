"""Generated sentinel fixtures only; no provider, customer or cross-CRM claim (OPE-543)."""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot.engagement_epochs import (
    EngagementError,
    EngagementHold,
    EngagementLedger,
)
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.live_evidence import LiveEvidence
from test_live_evidence import ACTOR, DECISION, OUTCOME, Authority, decision, outcome, totals

ORG = "generated-org-1"
E1, E2, E3 = "epoch-1", "epoch-2", "epoch-3"
RES = ("decisions", "truth")


def sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def private(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True)
    return path


def enroll(ledger, epoch=E1, lease="lease-a", checkpoint=10, **overrides):
    values = dict(
        lease_ref=lease,
        authority_digest=sha("authority-" + epoch + lease),
        resources=RES,
        lineage_ref="generated-lineage",
        checkpoint=checkpoint,
        budget_limit=100,
    )
    return ledger.record_engagement(ORG, epoch, **{**values, **overrides})


def retire(ledger, epoch, revocation):
    ledger.withdraw(ORG, epoch, revocation_epoch=revocation)
    return ledger.release_slot(ORG, epoch)


def reenter(ledger, *, first=E1, second=E2, checkpoint=40):
    enroll(ledger, first)
    ledger.activate(ORG, first)
    retire(ledger, first, 1)
    enroll(ledger, second, "lease-b", checkpoint)
    return ledger.activate(ORG, second)


class LiveCase:
    """Real LiveEvidence reducer bound to the real engagement ledger."""

    def __init__(self, root: Path):
        self.root = private(root)
        self.ledger = EngagementLedger(self.root)
        self.authority = Authority()
        self.store = LiveEvidence(self.root, self.authority, self.ledger)
        self.store.initialize()
        self.pages = 0
        self.refs: dict[str, str] = {}

    def enter(self, epoch, lease, checkpoint, **overrides):
        enroll(self.ledger, epoch, lease, checkpoint, **overrides)
        self.ledger.activate(ORG, epoch)
        self.bind(epoch, lease, checkpoint)

    def bind(self, epoch, lease, checkpoint):
        decision_grant = replace(
            DECISION, revision="grant-" + epoch, lease_ref=lease, checkpoint=checkpoint
        )
        outcome_grant = replace(
            OUTCOME, revision="truth-" + epoch, lease_ref=lease, checkpoint=checkpoint
        )
        self.authority.scope = replace(
            self.authority.scope,
            tenant=ORG,
            engagement_epoch=epoch,
            grants=(decision_grant, outcome_grant),
        )
        self.refs = {
            "decision": self.store.save_binding(
                replace(ACTOR, tenant=ORG), "workflow-a", "decisions"
            ),
            "outcome": self.store.save_binding(replace(ACTOR, tenant=ORG), "workflow-a", "truth"),
        }

    def ingest(self, *events, position, epoch=None, outcomes=False):
        self.pages += 1
        scope_epoch = epoch or self.authority.scope.engagement_epoch
        return self.store.ingest_page(
            replace(ACTOR, tenant=ORG),
            self.refs["outcome" if outcomes else "decision"],
            page_ref=f"page-{self.pages}",
            source_revision="truth-v1" if outcomes else "source-v1",
            events=events,
            source_positions=tuple(position for _ in events),
            event_epochs=tuple(scope_epoch for _ in events),
            checkpoint=max(position, self.authority.scope.grants[0].checkpoint),
        )

    def snapshot(self):
        return self.store.snapshot(replace(ACTOR, tenant=ORG), "workflow-a")

    def reads(self):
        return self.store.read_receipts(replace(ACTOR, tenant=ORG), "workflow-a")

    def page_rows(self):
        with sqlite3.connect(self.store.path) as db:
            return db.execute("SELECT count(*) FROM live_pages").fetchone()[0]


def test_new_epoch_binding_excludes_prior_rows(tmp_path):
    case = LiveCase(tmp_path / "owner")
    case.enter(E1, "lease-a", 10)
    case.ingest(decision(), position=31)
    assert totals(case.snapshot()) == (1, 0, 1, 0, 1)
    retire(case.ledger, E1, 1)

    # Re-enter: the new live binding is held until its checkpoint is durably recorded.
    case.authority.scope = replace(case.authority.scope, engagement_epoch=E2)
    with pytest.raises(IntakeError, match="engagement_epoch_hold"):
        case.bind(E2, "lease-b", 40)
    case.enter(E2, "lease-b", 40)
    assert case.ledger.get(ORG, E2).checkpoint == 40

    # Same resource re-bound: old-epoch D1 is absent from every new-epoch count.
    assert totals(case.snapshot()) == (0, 0, 0, 0, 0)
    case.ingest(decision(), position=35)  # old row redelivered at/before the checkpoint
    assert totals(case.snapshot()) == (0, 0, 0, 0, 0)
    assert case.snapshot().quarantined == 1

    # Late old-epoch outcome at a LATER source position changes nothing at all.
    before = (case.snapshot(), case.reads(), case.page_rows(), case.ledger.counts(ORG, E2))
    with pytest.raises(IntakeError, match="engagement_epoch_fenced"):
        case.ingest(outcome(), position=90, epoch=E1, outcomes=True)
    with pytest.raises(IntakeError, match="engagement_epoch_fenced"):
        case.ingest(decision("D9"), position=91, epoch=E1)
    after = (case.snapshot(), case.reads(), case.page_rows(), case.ledger.counts(ORG, E2))
    assert after == before
    assert totals(case.snapshot()) == (0, 0, 0, 0, 0)

    # One permitted new commit raises pending by exactly one.
    case.ingest(decision("D2", case_id="invoice-2"), position=92)
    assert totals(case.snapshot()) == (1, 0, 1, 0, 1)
    assert case.snapshot().pending == 1
    case.ingest(decision("D2", case_id="invoice-2"), position=93)  # redelivery counts once
    assert case.snapshot().pending == 1
    # The earlier epoch's allowed evidence remains separately visible, never mixed in.
    (history,) = case.ledger.historical(ORG, before=E2)
    assert history["epoch"] == E1 and history["historical"] is True


def test_live_binding_and_ingest_hold_without_recorded_active_epoch(tmp_path):
    case = LiveCase(tmp_path / "owner")
    with pytest.raises(IntakeError, match="engagement_epoch_hold"):
        case.bind(E1, "lease-a", 10)  # nothing recorded
    enroll(case.ledger, E1, "lease-a", 10)
    with pytest.raises(IntakeError, match="engagement_epoch_hold"):
        case.bind(E1, "lease-a", 10)  # recorded but not activated
    case.ledger.activate(ORG, E1)
    case.bind(E1, "lease-a", 10)
    with pytest.raises(IntakeError, match="engagement_epoch_hold"):  # wrong lease
        case.bind(E1, "lease-other", 10)
    with pytest.raises(IntakeError, match="engagement_epoch_hold"):  # below recorded checkpoint
        case.bind(E1, "lease-a", 5)
    case.bind(E1, "lease-a", 10)  # restore the signed authority; same refs, idempotent
    case.ingest(decision(), position=20)
    retire(case.ledger, E1, 1)
    with pytest.raises(IntakeError, match="engagement_epoch_hold"):
        case.ingest(decision("D2"), position=21)  # fenced epoch cannot continue
    assert totals(case.snapshot()) == (1, 0, 1, 0, 1)


def test_one_organization_two_epochs_separate_budgets_and_no_inherited_approvals(tmp_path):
    ledger = EngagementLedger(private(tmp_path / "owner"))
    enroll(ledger, E1)
    ledger.activate(ORG, E1)
    ledger.grant_approval(ORG, E1, "approval-1")
    assert ledger.reserve_budget(ORG, E1, "fit-1", 90)
    assert not ledger.reserve_budget(ORG, E1, "fit-2", 20)  # adverse: over the epoch limit
    ledger.bind(ORG, E1, "queue", "queue-1")
    retire(ledger, E1, 1)
    enroll(ledger, E2, "lease-b", 40, budget_limit=50)
    ledger.activate(ORG, E2)
    assert ledger.organizations() == (ORG,)
    first, second = ledger.engagements(ORG)
    assert (first.epoch, second.epoch) == (E1, E2)
    assert (first.budget_spent, second.budget_spent, second.budget_limit) == (90, 0, 50)
    assert not ledger.has_approval(ORG, E2, "approval-1")
    assert not ledger.has_approval(ORG, E1, "approval-1")  # terminal epoch grants nothing
    assert not ledger.authorize(ORG, E2, "queue", "queue-1")
    assert not ledger.authorize(ORG, E1, "queue", "queue-1")
    assert ledger.reserve_budget(ORG, E2, "fit-1", 50)  # same key, separate ledger
    assert ledger.reserve_budget(ORG, E2, "fit-1", 50)  # retry idempotent
    assert ledger.engagements(ORG)[1].budget_spent == 50
    with pytest.raises(EngagementError, match="reservation_conflict"):
        ledger.reserve_budget(ORG, E2, "fit-1", 10)
    with pytest.raises(EngagementError, match="epoch_not_live"):
        ledger.reserve_budget(ORG, E1, "late", 1)
    with pytest.raises(EngagementError, match="epoch_not_live"):
        ledger.grant_approval(ORG, E1, "approval-late")


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"checkpoint": None}, "lineage_or_checkpoint_missing"),
        ({"lineage_ref": None}, "lineage_or_checkpoint_missing"),
        ({"checkpoint": 20}, "checkpoint_before_prior_high_water"),
        ({"checkpoint": 31}, "checkpoint_not_after_prior_rows"),
    ],
)
def test_missing_or_unreliable_lineage_or_checkpoint_holds(tmp_path, overrides, reason):
    ledger = EngagementLedger(private(tmp_path / "owner"))
    enroll(ledger, E1)
    ledger.activate(ORG, E1)
    ledger.observe_positions(ORG, E1, 31)
    retire(ledger, E1, 1)
    with pytest.raises(EngagementHold, match=reason):
        enroll(ledger, E2, "lease-b", **{"checkpoint": 40, **overrides})
    assert len(ledger.engagements(ORG)) == 1  # nothing partially recorded
    enroll(ledger, E2, "lease-b", 32 if reason.endswith("rows") else 40)  # a good one succeeds


def test_reentry_requires_persisted_terminal_released_slot_and_fresh_authority(tmp_path):
    ledger = EngagementLedger(private(tmp_path / "owner"))
    enroll(ledger, E1)
    ledger.activate(ORG, E1)
    with pytest.raises(EngagementHold, match="prior_engagement_not_terminal"):
        enroll(ledger, E2, "lease-b", 40)
    with pytest.raises(EngagementHold, match="terminal_state_not_persisted"):
        ledger.release_slot(ORG, E1)  # cannot free the slot before terminal state persists
    ledger.withdraw(ORG, E1, revocation_epoch=1)
    terminal = ledger.get(ORG, E1)
    assert (terminal.state, terminal.revocation_epoch, terminal.slot_released) == (
        "terminal",
        1,
        False,
    )
    with pytest.raises(EngagementHold, match="live_slot_not_released"):
        enroll(ledger, E2, "lease-b", 40)
    ledger.release_slot(ORG, E1)
    with pytest.raises(EngagementError, match="fresh_authority_required"):
        enroll(ledger, E2, "lease-a", 40)  # reused lease
    with pytest.raises(EngagementError, match="fresh_authority_required"):
        enroll(ledger, E2, "lease-b", 40, authority_digest=sha("authority-" + E1 + "lease-a"))
    enroll(ledger, E2, "lease-b", 40)


def test_repeated_enrollment_is_idempotent_and_renamed_labels_are_not_identity(tmp_path):
    ledger = EngagementLedger(private(tmp_path / "owner"))
    first = enroll(ledger, E1, label="Display Name A")
    again = enroll(ledger, E1, label="Renamed Display Name")
    assert again == first and again.label == "Display Name A"
    assert ledger.activate(ORG, E1) == ledger.activate(ORG, E1)
    retire(ledger, E1, 1)
    retire(ledger, E1, 1)  # repeated withdrawal and release are no-ops
    second = enroll(ledger, E2, "lease-b", 40, label="Another Label")
    assert enroll(ledger, E2, "lease-b", 40, label="Third Label") == second
    ledger.activate(ORG, E2)
    ledger.activate(ORG, E2)
    assert ledger.organizations() == (ORG,) and len(ledger.engagements(ORG)) == 2
    with pytest.raises(EngagementError, match="engagement_conflict"):
        enroll(ledger, E2, "lease-b", 99)  # same epoch, different checkpoint
    with pytest.raises(EngagementError, match="revocation_epoch_conflict"):
        ledger.withdraw(ORG, E1, revocation_epoch=7)
    with pytest.raises(EngagementError, match="epoch_terminal"):
        ledger.bind(ORG, E1, "queue", "late-queue")


def test_withdrawal_fence_persists_across_restart_and_older_backup_restore(tmp_path):
    root = private(tmp_path / "owner")
    ledger = EngagementLedger(root)
    enroll(ledger, E1)
    ledger.activate(ORG, E1)
    backup = tmp_path / "older.sqlite3"
    ledger.backup(backup)  # taken while E1 was still live
    ledger.withdraw(ORG, E1, revocation_epoch=1)
    assert EngagementLedger(root).get(ORG, E1).state == "terminal"  # restart

    ledger.restore_backup(backup)
    restored = ledger.get(ORG, E1)
    assert (restored.state, restored.revocation_epoch) == ("terminal", 1)
    with pytest.raises(EngagementHold, match="epoch_terminal"):
        ledger.live_gate(ORG, E1, "decisions", "lease-a")
    with pytest.raises(EngagementError, match="epoch_fenced"):
        ledger.activate(ORG, E1)
    assert ledger.record_effect(ORG, E1, "decision", "late-1") is False
    assert not ledger.authorize(ORG, E1, "queue", "queue-1")


def test_restore_of_a_backup_older_than_the_epoch_cannot_forge_lineage(tmp_path):
    root = private(tmp_path / "owner")
    ledger = EngagementLedger(root)
    empty = tmp_path / "empty.sqlite3"
    ledger.backup(empty)  # before E1 existed
    enroll(ledger, E1)
    ledger.activate(ORG, E1)
    retire(ledger, E1, 1)
    ledger.restore_backup(empty)
    with pytest.raises(EngagementError, match="epoch_fenced"):
        enroll(ledger, E1)  # a withdrawn epoch is never re-created as live
    with pytest.raises(EngagementHold, match="prior_lineage_missing"):
        enroll(ledger, E2, "lease-b", 40)  # no reliable prior checkpoint: hold


def test_tampered_or_truncated_fence_journal_holds(tmp_path):
    root = private(tmp_path / "owner")
    ledger = EngagementLedger(root)
    enroll(ledger, E1)
    ledger.activate(ORG, E1)
    retire(ledger, E1, 1)
    journal = root / "engagement-fences.jsonl"
    original = journal.read_text()
    journal.write_text(original.replace('"revocation_epoch": 1', '"revocation_epoch": 0'))
    with pytest.raises(EngagementHold, match="fence_journal_broken"):
        EngagementLedger(root)
    journal.write_text(original)
    assert EngagementLedger(root).get(ORG, E1).state == "terminal"


def test_migrated_single_engagement_records_keep_original_scope(tmp_path):
    ledger = EngagementLedger(private(tmp_path / "owner"))
    legacy = (("queue", "queue-1"), ("session", "session-1"), ("receipt", "receipt-1"))
    kwargs = dict(
        lease_ref="lease-a",
        authority_digest=sha("legacy"),
        resources=RES,
        lineage_ref="generated-lineage",
        checkpoint=10,
        budget_limit=100,
        records=legacy,
    )
    migrated = ledger.migrate_single_engagement(ORG, E1, **kwargs)
    assert migrated.ordinal == 1 and migrated.state == "active"
    assert ledger.migrate_single_engagement(ORG, E1, **kwargs) == migrated  # idempotent
    assert all(ledger.authorize(ORG, E1, kind, ref) for kind, ref in legacy)
    with pytest.raises(EngagementError, match="migration_requires_first_engagement"):
        ledger.migrate_single_engagement(
            ORG, E2, **{**kwargs, "lease_ref": "lease-z", "authority_digest": sha("z")}
        )
    retire(ledger, E1, 1)
    enroll(ledger, E2, "lease-b", 40)
    ledger.activate(ORG, E2)
    for kind, ref in legacy:
        assert not ledger.authorize(ORG, E2, kind, ref)
        with pytest.raises(EngagementError, match="binding_belongs_to_other_epoch"):
            ledger.bind(ORG, E2, kind, ref)


def test_late_old_epoch_effects_change_zero_decisions_reads_fits_or_exports(tmp_path):
    ledger = EngagementLedger(private(tmp_path / "owner"))
    reenter(ledger)
    for kind in ("decision", "read", "fit", "export"):
        assert ledger.record_effect(ORG, E2, kind, kind + "-1") is True
        assert ledger.record_effect(ORG, E2, kind, kind + "-1") is False  # replay counts once
    expected = dict(decision=1, read=1, fit=1, export=1)
    assert ledger.counts(ORG, E2) == expected
    for kind in ("decision", "read", "fit", "export"):
        assert ledger.record_effect(ORG, E1, kind, "late-" + kind) is False  # old callback
    assert ledger.counts(ORG, E2) == expected
    assert ledger.counts(ORG, E1) == dict(decision=0, read=0, fit=0, export=0)
    with pytest.raises(EngagementError, match="unknown_effect_kind"):
        ledger.record_effect(ORG, E2, "email", "x")


def test_admit_row_verdicts_cover_checkpoint_prior_epoch_and_hold(tmp_path):
    ledger = EngagementLedger(private(tmp_path / "owner"))
    enroll(ledger, E1)
    kwargs = dict(resource="decisions", lease_ref="lease-a", row_epoch=E1)
    assert ledger.admit_row(ORG, E1, position=11, **kwargs).verdict == "hold"  # not active
    ledger.activate(ORG, E1)
    assert ledger.admit_row(ORG, E1, position=10, **kwargs).reason == "at_or_before_checkpoint"
    assert ledger.admit_row(ORG, E1, position=11, **kwargs).verdict == "accept"
    assert ledger.admit_row(ORG, E1, position=11, **{**kwargs, "lease_ref": "x"}).verdict == "hold"
    retire(ledger, E1, 1)
    enroll(ledger, E2, "lease-b", 40)
    ledger.activate(ORG, E2)
    new = dict(resource="decisions", lease_ref="lease-b")
    assert ledger.admit_row(ORG, E2, row_epoch=E1, position=999, **new).reason == "prior_epoch"
    assert ledger.admit_row(ORG, E2, row_epoch="epoch-unknown", position=999, **new).reason == (
        "epoch_mismatch"
    )
    assert ledger.admit_row(ORG, E2, row_epoch=E2, position=40, **new).verdict == "deny"
    assert ledger.admit_row(ORG, E2, row_epoch=E2, position=41, **new).verdict == "accept"
    assert ledger.admit_row(ORG, E1, position=999, **kwargs).verdict == "hold"


def test_three_epochs_keep_historical_evidence_separate_and_state_survives_restart(tmp_path):
    root = private(tmp_path / "owner")
    ledger = EngagementLedger(root)
    reenter(ledger)
    ledger.record_effect(ORG, E2, "decision", "d-2")
    ledger.observe_positions(ORG, E2, 55)
    retire(ledger, E2, 2)
    retire(ledger, E1, 1)  # repeating an older, already-persisted withdrawal stays a no-op
    enroll(ledger, E3, "lease-c", 60)
    ledger.activate(ORG, E3)
    with pytest.raises(EngagementError, match="revocation_epoch_not_monotonic"):
        ledger.withdraw(ORG, E3, revocation_epoch=2)
    restarted = EngagementLedger(root)
    assert [e.epoch for e in restarted.engagements(ORG)] == [E1, E2, E3]
    assert restarted.counts(ORG, E3) == dict(decision=0, read=0, fit=0, export=0)
    history = restarted.historical(ORG, before=E3)
    assert [h["epoch"] for h in history] == [E1, E2]
    assert history[1]["counts"]["decision"] == 1 and all(h["historical"] for h in history)
    retire(restarted, E3, 3)
    with pytest.raises(EngagementHold, match="checkpoint_before_prior_high_water"):
        enroll(restarted, "epoch-4", "lease-d", 50)


def test_private_store_and_reference_validation(tmp_path):
    loose = tmp_path / "loose"
    loose.mkdir(mode=0o755)
    with pytest.raises(EngagementError, match="private_store_required"):
        EngagementLedger(loose)
    with pytest.raises(EngagementError, match="absolute_private_directory_required"):
        EngagementLedger(Path("relative"))
    ledger = EngagementLedger(private(tmp_path / "owner"))
    with pytest.raises(EngagementError, match="invalid_reference"):
        enroll(ledger, "bad epoch")
    with pytest.raises(EngagementError, match="invalid_authority_digest"):
        enroll(ledger, E1, authority_digest="nothex")
    with pytest.raises(EngagementError, match="unknown_binding_kind"):
        enroll(ledger, E1)
        ledger.bind(ORG, E1, "mystery", "x")


def test_torn_fence_journal_tail_holds_instead_of_lifting_the_fence(tmp_path):
    root = private(tmp_path / "owner")
    ledger = EngagementLedger(root)
    enroll(ledger, E1)
    ledger.activate(ORG, E1)
    retire(ledger, E1, 1)
    journal = root / "engagement-fences.jsonl"
    original = journal.read_text()
    journal.write_text(original[:-20])  # crash mid-append: a partial final line
    with pytest.raises(EngagementHold, match="fence_journal_broken"):
        EngagementLedger(root)
    journal.write_text(original)
    assert EngagementLedger(root).get(ORG, E1).state == "terminal"


def test_withdrawal_waits_until_live_page_commit(tmp_path, monkeypatch):
    case = LiveCase(tmp_path / "owner")
    enroll(case.ledger, E1, "lease-a", 10)
    case.ledger.activate(ORG, E1)
    case.bind(E1, "lease-a", 10)

    started = threading.Event()
    withdrawn = threading.Event()
    receipt_count = []
    original = case.store._record_event

    def withdraw():
        started.set()
        case.ledger.withdraw(ORG, E1, revocation_epoch=1)
        receipt_count.append(case.page_rows())
        withdrawn.set()

    thread = threading.Thread(target=withdraw)

    def record(*args):
        thread.start()
        assert started.wait(2)
        assert not withdrawn.wait(0.1)
        original(*args)

    monkeypatch.setattr(case.store, "_record_event", record)
    try:
        assert case.ingest(decision(), position=20).startswith("live-")
    finally:
        thread.join(timeout=2)
    assert withdrawn.is_set()
    assert receipt_count == [1]
