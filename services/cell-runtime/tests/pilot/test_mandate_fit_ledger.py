"""The dispatcher uses the standing mandate database across restart and retry."""

import pytest
from opentour_cell.pilot import learning_mandate as lm
from opentour_cell.pilot.mandate_fit_ledger import MandateFitLedger
from opentour_cell.pilot.operating_point import digest
from test_fit_dispatch import DELEGATE, MANDATE, World, spec


def test_worker_retries_settle_once_in_existing_mandate_database(tmp_path):
    world = World(tmp_path)
    run, receipt = world.admit()
    request = world.request(run, receipt)
    worker = world.dispatcher(ledger=MandateFitLedger(world.mandates))
    first = worker.dispatch(request, client="scheduler", operation_key="first")
    assert first.stage == "running", first
    reopened = lm.LearningMandates(world.dir, world.store.organization, world.store.engagement_id)
    worker = world.dispatcher(ledger=MandateFitLedger(reopened))
    retry = worker.dispatch(request, client="slack", operation_key="retry")
    assert retry.execution_id == first.execution_id and retry.replayed
    assert world.finish(worker, first.execution_id).stage == "settled"
    assert worker.advance(first.execution_id, actor=DELEGATE).stage == "settled"
    assert reopened.inspect(MANDATE)["cumulative_reserved_fits"] == 1
    with reopened._db(write=False) as db:
        assert db.execute("SELECT count(*) FROM dispatch_aliases").fetchone()[0] == 2
        assert (
            db.execute("SELECT count(*) FROM dispatch_events WHERE stage='settled'").fetchone()[0]
            == 1
        )


def direct_request(world, run):
    pair = ["b", "a"]
    world.mandates.confirm_class(
        run,
        actor=DELEGATE,
        discovery_revision=digest(["class", run]),
        candidate={
            "class_id": "class-" + digest(pair)[:24],
            "from_label": "b",
            "to_label": "a",
            "count": 4,
            "groups": 3,
        },
    )
    return dict(
        run_id=run,
        plan_revision=digest(["plan", run]),
        intake_revision=digest(["intake", run]),
        residual_revision=digest(["residual", run]),
        max_seconds=10,
    )


@pytest.mark.parametrize("direct_first", [True, False])
def test_direct_and_dispatch_reconcile_the_same_budget_and_fit(tmp_path, direct_first):
    world = World(tmp_path, mandate=spec(max_fits=1))
    run, receipt = world.admit()
    other_run, _ = world.admit(2)
    direct = direct_request(world, run)
    other = direct_request(world, other_run)
    worker = world.dispatcher(ledger=MandateFitLedger(world.mandates))
    if direct_first:
        world.mandates.reserve(MANDATE, direct, actor=DELEGATE)
        assert (
            worker.dispatch(
                world.request(run, receipt), client="worker", operation_key="retry"
            ).reservations
            == 0
        )
    else:
        result = worker.dispatch(
            world.request(run, receipt), client="worker", operation_key="first"
        )
        assert result.stage == "running"
        with pytest.raises(lm.MandateError, match="fit_reservation_conflict"):
            world.mandates.reserve(MANDATE, direct, actor=DELEGATE)
        with pytest.raises(lm.MandateError, match="concurrent_fit_limit_reached"):
            world.mandates.reserve(MANDATE, other, actor=DELEGATE)
        assert world.finish(worker, result.execution_id).stage == "settled"
        with pytest.raises(lm.MandateError, match="standing_fit_budget_exhausted"):
            world.mandates.reserve(MANDATE, other, actor=DELEGATE)
    details = world.mandates.inspect(MANDATE)
    assert details["cumulative_reserved_fits"] == details["reserved_fits"] == 1


def test_unacknowledged_submission_recovers_from_restored_mandate_database(tmp_path):
    import sqlite3

    from fit_dispatch_doubles import BackendConfig, WorkerCrash
    from opentour_cell.pilot import fit_dispatch as fd

    world = World(tmp_path, config=BackendConfig(crash_after_accept=1))
    run, receipt = world.admit()
    first = world.dispatcher(ledger=MandateFitLedger(world.mandates))
    with pytest.raises(WorkerCrash):
        first.dispatch(world.request(run, receipt), client="generated", operation_key="first")
    with world.mandates._db(write=False) as db:
        execution = db.execute("SELECT id FROM dispatch_fits").fetchone()[0]
    # SQLite online backup includes the reservation, state, alias and journal together.
    backup = tmp_path / "generated-backup.sqlite3"
    with sqlite3.connect(world.mandates.path) as source, sqlite3.connect(backup) as image:
        source.backup(image)
    with sqlite3.connect(backup) as image, sqlite3.connect(world.mandates.path) as restored:
        image.backup(restored)
    worker = world.dispatcher("restarted", ledger=MandateFitLedger(world.mandates))
    assert worker.advance(execution, actor=DELEGATE).reason == "submission_in_progress"
    assert world.backend.jobs_created() == world.backend.calls["submit"] == 1
    world.clock.advance(fd.LEASE_SECONDS + 1)
    assert worker.advance(execution, actor=DELEGATE).learning_run_state == "running"
    assert world.finish(worker, execution).stage == "settled"
    assert world.backend.calls["submit"] == 1
    with world.mandates._db(write=False) as db:
        assert (
            db.execute("SELECT count(*) FROM dispatch_events WHERE stage='settled'").fetchone()[0]
            == 1
        )


def test_failed_alias_write_rolls_back_entire_reservation(tmp_path):
    world = World(tmp_path)
    run, receipt = world.admit()
    worker = world.dispatcher(ledger=MandateFitLedger(world.mandates))
    with world.mandates._db() as db:
        db.execute("""CREATE TRIGGER refuse_alias BEFORE INSERT ON dispatch_aliases
            BEGIN SELECT raise(ABORT,'generated storage fault'); END""")
    request = world.request(run, receipt)
    rejected = worker.dispatch(request, client="generated", operation_key="first")
    assert rejected.reservations == world.backend.jobs_created() == 0
    with world.mandates._db() as db:
        for table in ("dispatch_fits", "dispatch_events", "dispatch_aliases"):
            assert db.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        db.execute("DROP TRIGGER refuse_alias")
    admitted = worker.dispatch(request, client="generated", operation_key="first")
    assert admitted.reservations == world.backend.jobs_created() == 1
