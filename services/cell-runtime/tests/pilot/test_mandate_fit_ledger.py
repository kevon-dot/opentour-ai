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
