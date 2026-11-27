"""Exercise release identity, uncertainty and recovery against two durable local journals."""

import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest
from opentour_cell.pilot.custody import Closeout
from opentour_cell.pilot.release_outbox import (
    ApprovedRelease,
    ReceiverObservation,
    ReleaseOutbox,
    ReleaseOutboxError,
    ReleaseSelection,
)
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
from test_pilot_custody import START


class Approvals:
    def __init__(self, grant):
        self.current = {grant.selection: grant}

    def resolve(self, db, selection, actor, action):
        permitted = {
            "inspect": {"inspector", "reviewer"},
            "reserve": {"operator"},
            "claim": {"operator"},
            "reconcile": {"operator"},
        }
        if actor in permitted[action]:
            return self.current.get(selection)
        return None


class Recipient:
    def __init__(self, path: Path, *, observed_at: int = 103, received_at: int = 102):
        self.path = path
        self.observed_at = observed_at
        self.received_at = received_at
        self.calls = []
        self.before_reply: Callable[[], None] = lambda: None
        self.unknown = False
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "CREATE TABLE deliveries(ref TEXT PRIMARY KEY, tenant TEXT, destination TEXT,"
                " artifact TEXT, receipt TEXT, received_at INTEGER)"
            )

    def send_then_lose_reply(self, grant):
        with closing(sqlite3.connect(self.path)) as db, db:
            db.execute(
                "INSERT INTO deliveries VALUES (?,?,?,?,?,?)",
                (
                    grant.reference,
                    grant.tenant,
                    grant.destination_ref,
                    grant.artifact_sha256,
                    "receiver-delivery",
                    self.received_at,
                ),
            )
        raise TimeoutError("generated loss after receiver commit")

    def lookup(self, grant, lookup_ref):
        self.calls.append(grant.reference)
        self.before_reply()
        if self.unknown:
            raise TimeoutError("generated receiver timeout")
        with closing(sqlite3.connect(self.path)) as db:
            row = db.execute(
                "SELECT receipt,received_at FROM deliveries WHERE ref=? AND tenant=? "
                "AND destination=? AND artifact=?",
                (
                    grant.reference,
                    grant.tenant,
                    grant.destination_ref,
                    grant.artifact_sha256,
                ),
            ).fetchone()
        return ReceiverObservation(
            "delivered" if row else "absent",
            grant.reference,
            grant.destination_ref,
            grant.artifact_sha256,
            self.observed_at,
            lookup_ref,
            *(row or (None, None)),
        )


def backup(source, destination):
    with (
        closing(sqlite3.connect(source)) as original,
        closing(sqlite3.connect(destination)) as restored,
    ):
        original.backup(restored)


def create(tmp_path, tenant="generated-release"):
    directory = tmp_path / tenant
    directory.mkdir(mode=0o700)
    store = LocalTrainingStore(directory / "training.sqlite3", tenant)
    grant = ApprovedRelease(
        tenant,
        store.engagement_id,
        ReleaseSelection("workflow-a", "proposal-a", "approval-a"),
        "1" * 64,
        "2" * 64,
        "release-grant-a",
        "receiver-a",
        100,
        200,
    )
    permission = Approvals(grant)
    recipient = Recipient(directory / "generated-recipient.sqlite3")
    return ReleaseOutbox(store, permission, lambda: 103), grant, permission, recipient


def test_exact_approval_retries_and_two_claimants_leave_one_receiver_effect(tmp_path):
    queue, grant, authority, receiver = create(tmp_path)
    assert queue.status(grant.selection, "inspector") is None
    with queue.store._db() as db:
        assert (
            db.execute("SELECT name FROM sqlite_master WHERE name='release_outbox'").fetchone()
            is None
        )
    with pytest.raises(ReleaseOutboxError):
        queue.reserve(grant.selection, "inspector", "initial")
    receipts = [queue.reserve(grant.selection, "operator", f"operation-{i}") for i in range(3)]
    assert receipts[0] == receipts[1] == receipts[2]
    assert receipts[0].stage == "queued" and receiver.calls == []
    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = tuple(
            pool.map(lambda _: queue.claim(grant.selection, "operator", receiver), range(2))
        )
    assert sum(try_.dispatch for try_ in attempts) == 1
    assert all(try_.status.stage == "delivery_unconfirmed" for try_ in attempts)
    with pytest.raises(TimeoutError):
        receiver.send_then_lose_reply(grant)
    resumed = ReleaseOutbox(
        LocalTrainingStore(queue.store.path, grant.tenant, existing_only=True),
        authority,
        lambda: 103,
    )
    previous_calls = len(receiver.calls)
    assert not resumed.claim(grant.selection, "operator", receiver).dispatch
    for _ in range(2):
        checked = resumed.status(grant.selection, "reviewer")
        assert checked.stage == "delivery_unconfirmed" and checked.receiver_receipt_ref is None
    assert len(receiver.calls) == previous_calls
    final = resumed.reconcile(grant.selection, "operator", receiver)
    assert final.stage == "delivered" and final.receiver_receipt_ref == "receiver-delivery"
    assert final.received_at == 102 and final.last_checked_at == 103
    assert resumed.status(grant.selection, "inspector") == final
    assert resumed.reserve(grant.selection, "operator", "operation-3") == final
    with queue.store._db() as db:
        assert db.execute("SELECT count(*) FROM release_outbox").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM release_operation_keys").fetchone()[0] == 4
    with closing(sqlite3.connect(receiver.path)) as db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone() == (1,)


def test_restored_local_preclaim_never_resends_an_independently_received_item(tmp_path):
    queue, grant, authority, receiver = create(tmp_path)
    accepted = queue.reserve(grant.selection, "operator", "operation-before")
    before = tmp_path / "preclaim.sqlite3"
    backup(queue.store.path, before)
    assert queue.claim(grant.selection, "operator", receiver).dispatch
    with pytest.raises(TimeoutError):
        receiver.send_then_lose_reply(grant)
    backup(before, queue.store.path)
    restored = ReleaseOutbox(
        LocalTrainingStore(queue.store.path, grant.tenant, existing_only=True),
        authority,
        lambda: 103,
    )
    assert restored.status(grant.selection, "inspector") == accepted
    result = restored.claim(grant.selection, "operator", receiver)
    assert not result.dispatch and result.status.stage == "delivered"
    with closing(sqlite3.connect(receiver.path)) as db:
        assert db.execute("SELECT ref,destination FROM deliveries").fetchall() == [
            (accepted.reference, accepted.destination_ref)
        ]


def test_unknown_wrong_scope_stale_binding_and_idempotency_conflict_fail_closed(tmp_path):
    queue, grant, authority, receiver = create(tmp_path)
    original = queue.reserve(grant.selection, "operator", "existing")
    receiver.unknown = True
    held = queue.claim(grant.selection, "operator", receiver)
    assert not held.dispatch and held.status.stage == "delivery_unconfirmed"
    assert held.status.claimed_at is None
    with closing(sqlite3.connect(receiver.path)) as db:
        assert db.execute("SELECT count(*) FROM deliveries").fetchone() == (0,)

    class Foreign:
        def lookup(self, current, lookup_ref):
            return ReceiverObservation(
                "delivered",
                current.reference,
                "wrong-destination",
                current.artifact_sha256,
                103,
                lookup_ref,
                "foreign-receipt",
                102,
            )

    with pytest.raises(ReleaseOutboxError, match="receiver_observation_scope_mismatch"):
        queue.reconcile(grant.selection, "operator", Foreign())
    assert queue.status(grant.selection, "inspector") == held.status
    authority.current[grant.selection] = replace(grant, artifact_sha256="3" * 64)
    with pytest.raises(ReleaseOutboxError, match="release_intent_changed"):
        queue.reserve(grant.selection, "operator", "different-key")
    with pytest.raises(ReleaseOutboxError, match="release_idempotency_conflict"):
        queue.reserve(grant.selection, "operator", "existing")
    successor = replace(
        grant,
        selection=replace(grant.selection, approval_ref="approval-b"),
        artifact_sha256="3" * 64,
    )
    authority.current[successor.selection] = successor
    assert (
        queue.reserve(successor.selection, "operator", "successor").reference != original.reference
    )


def test_permission_and_withdrawal_are_rechecked_after_receiver_lookup(tmp_path):
    queue, grant, authority, receiver = create(tmp_path)
    queue.reserve(grant.selection, "operator", "initial")
    assert queue.claim(grant.selection, "operator", receiver).dispatch
    with pytest.raises(TimeoutError):
        receiver.send_then_lose_reply(grant)

    def revoke():
        authority.current.clear()

    receiver.before_reply = revoke
    with pytest.raises(ReleaseOutboxError):
        queue.reconcile(grant.selection, "operator", receiver)
    authority.current[grant.selection] = grant
    status = queue.status(grant.selection, "inspector")
    assert status.stage == "delivery_unconfirmed"
    receiver.before_reply = lambda: Closeout(
        queue.store.path.parent, queue.store.organization
    ).withdraw(**START)
    with pytest.raises(TrainingError, match="engagement_withdrawn"):
        queue.reconcile(grant.selection, "operator", receiver)
    with pytest.raises(TrainingError, match="engagement_withdrawn"):
        queue.status(grant.selection, "inspector")
    with queue.store._db(allow_withdrawn=True) as db:
        assert db.execute("SELECT receiver_receipt_ref FROM release_outbox").fetchone()[0] is None


def test_cached_absence_cannot_authorize_a_new_dispatch(tmp_path):
    queue, grant, _, _ = create(tmp_path)
    original = queue.reserve(grant.selection, "operator", "initial")

    class Cached:
        def lookup(self, current, lookup_ref):
            return ReceiverObservation(
                "absent",
                current.reference,
                current.destination_ref,
                current.artifact_sha256,
                102,
                lookup_ref,
            )

    outcome = queue.claim(grant.selection, "operator", Cached())
    assert not outcome.dispatch and outcome.status == original

    class PreviousLookup:
        def lookup(self, current, _lookup_ref):
            return ReceiverObservation(
                "absent",
                current.reference,
                current.destination_ref,
                current.artifact_sha256,
                103,
                "prior-lookup",
            )

    with pytest.raises(ReleaseOutboxError, match="receiver_observation_scope_mismatch"):
        queue.claim(grant.selection, "operator", PreviousLookup())
    assert queue.status(grant.selection, "inspector") == original


def test_neighbor_and_wrong_digest_readback_cannot_confirm(tmp_path):
    first, left, _, receiver = create(tmp_path, "generated-first")
    second, right, _, other = create(tmp_path, "generated-second")
    assert left.selection == right.selection
    first.reserve(left.selection, "operator", "same-operation")
    second.reserve(right.selection, "operator", "same-operation")
    assert first.claim(left.selection, "operator", receiver).dispatch
    assert second.claim(right.selection, "operator", other).dispatch
    with pytest.raises(TimeoutError):
        receiver.send_then_lose_reply(left)
    valid = receiver.lookup(left, "generated-lookup")

    class SuppliedReceipt:
        def __init__(self, observation):
            self.observation = observation

        def lookup(self, _grant, lookup_ref):
            return replace(self.observation, lookup_ref=lookup_ref)

    for observation in (
        valid,
        replace(valid, release_ref=right.reference, artifact_sha256="e" * 64),
    ):
        with pytest.raises(ReleaseOutboxError, match="receiver_observation_scope_mismatch"):
            second.reconcile(right.selection, "operator", SuppliedReceipt(observation))
    assert second.status(right.selection, "inspector").stage == "delivery_unconfirmed"
    assert first.reconcile(left.selection, "operator", receiver).stage == "delivered"


def test_closed_wal_inspection_does_not_create_sidecars(tmp_path, monkeypatch):
    queue, grant, authority, _ = create(tmp_path)
    queue.reserve(grant.selection, "operator", "initial")
    with closing(sqlite3.connect(queue.store.path)) as db:
        assert db.execute("pragma journal_mode=wal").fetchone() == ("wal",)
    effects = [Path(str(queue.store.path) + ext) for ext in ("-wal", "-shm", "-journal")]
    assert not any(path.exists() for path in effects)

    def unexpected(*args):
        pytest.fail("A closed WAL must hold before invoking the authority port.")

    monkeypatch.setattr(authority, "resolve", unexpected)
    with pytest.raises(ReleaseOutboxError, match="release_outbox_unavailable"):
        queue.status(grant.selection, "inspector")
    assert not any(path.exists() for path in effects)


def test_incomplete_restored_delivery_fails_before_disclosure_or_lookup(tmp_path):
    queue, grant, _, receiver = create(tmp_path)
    queue.reserve(grant.selection, "operator", "initial")
    with queue.store._db(write=True) as db:
        db.execute("UPDATE release_outbox SET stage='delivered'")
    for check in (
        lambda: queue.status(grant.selection, "inspector"),
        lambda: queue.claim(grant.selection, "operator", receiver),
        lambda: queue.reconcile(grant.selection, "operator", receiver),
    ):
        with pytest.raises(ReleaseOutboxError, match="release_stage_integrity_unavailable"):
            check()
    assert receiver.calls == []


def test_authenticated_clients_share_canonical_release_and_isolate_operation_keys(tmp_path):
    queue, grant, authority, _ = create(tmp_path)
    successor = replace(
        grant,
        selection=replace(grant.selection, approval_ref="approval-b", proposal_ref="proposal-b"),
    )
    authority.current[successor.selection] = successor
    a = queue.reserve(grant.selection, "operator", "request-1", authenticated_client="install-a")
    b = queue.reserve(
        successor.selection, "operator", "request-1", authenticated_client="install-b"
    )
    retry = queue.reserve(
        grant.selection, "operator", "request-2", authenticated_client="install-b"
    )
    assert a != b and retry == a
    with pytest.raises(ReleaseOutboxError, match="release_idempotency_conflict"):
        queue.reserve(grant.selection, "operator", "request-1", authenticated_client="install-b")
