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


def test_learning_fit_port_never_sends_on_reserve_or_settles_unverified_usage(tmp_path):
    from dataclasses import replace

    from opentour_cell.pilot import fit_dispatch as fd
    from opentour_cell.pilot import learning_runs as lr
    from opentour_cell.pilot.learning_fit_dispatch import LearningFitLedger

    world = World(tmp_path)
    run, receipt = world.admit()
    request = world.request(run, receipt)
    mandate = world.mandates.inspect(MANDATE)
    dispatcher = world.dispatcher(ledger=MandateFitLedger(world.mandates))
    learning_ref = "learning-run-one"
    adapter = LearningFitLedger(
        dispatcher,
        actor=DELEGATE,
        client="worker",
        resolve=lambda ref: request if ref == learning_ref else None,
    )
    intent = lr.FitIntent(
        world.store.organization,
        MANDATE,
        mandate["mandate_revision"],
        world.snapshots[run].snapshot_ref,
        "plan-" + request.plan_revision[:40],
        fd.model_digest(request.binding),
        learning_ref,
        request.evaluation_revision,
        request.bound.maximum_seconds,
    )
    assert adapter.lookup(intent) is None
    with pytest.raises(lr.LedgerHold):
        adapter.reserve(replace(intent, snapshot_ref="unrelated"), operation_key="bad")
    assert world.mandates.inspect(MANDATE)["reserved_fits"] == 0
    reserved = adapter.reserve(intent, operation_key="first")
    assert adapter.lookup(intent) == reserved
    with world.mandates._db(write=False) as db:
        assert db.execute("SELECT count(*) FROM dispatch_aliases").fetchone()[0] == 1
    assert dispatcher.status(reserved.reservation_ref, actor=DELEGATE).submissions == 0
    with pytest.raises(lr.LedgerHold):
        adapter.settle(reserved.reservation_ref, consumed_units=0)
    final = world.finish(dispatcher, reserved.reservation_ref)
    assert final.stage == "settled" and final.submissions == 1
    with pytest.raises(lr.LedgerHold):
        adapter.settle(reserved.reservation_ref, consumed_units=final.usage["seconds"] + 1)
    assert (
        adapter.settle(
            reserved.reservation_ref, consumed_units=final.usage["seconds"]
        ).consumed_units
        == final.usage["seconds"]
    )


@pytest.mark.parametrize("terminal", ["candidate", "invalid_output", "cancel_before_submit"])
def test_signed_learning_plan_scheduler_and_slack_share_one_dispatch(tmp_path, terminal):
    from dataclasses import replace

    from fit_dispatch_doubles import BackendConfig
    from opentour_cell.pilot import fit_dispatch as fd
    from opentour_cell.pilot import learning_runs as lr
    from opentour_cell.pilot.learning_fit_dispatch import LearningFitLedger
    from test_fit_dispatch import OWNER

    world = World(tmp_path, config=BackendConfig(invalid_output=terminal == "invalid_output"))
    run, receipt = world.admit()
    request = world.request(run, receipt)
    tenant = world.store.organization
    mandate = world.mandates.inspect(MANDATE)
    workflow = mandate["scope"]["workflow"]

    def authorize(channel, actor, role):
        path = world.dir / f"learning-{channel}.json"
        assert (
            lr.main(
                [
                    "authorize",
                    "--selection",
                    str(path),
                    "--tenant",
                    tenant,
                    "--owner",
                    OWNER,
                    "--actor",
                    actor,
                    "--role",
                    role,
                    "--channel",
                    channel,
                    "--client",
                    f"client-{channel}",
                    "--workflow",
                    workflow,
                    "--mandate",
                    MANDATE,
                    "--grant-ref",
                    f"grant-{channel}",
                    "--expires-at",
                    str(int(world.clock()) + 600),
                ]
            )
            == 0
        )
        return path

    slack_path = authorize("slack", OWNER, "owner")
    schedule_path = authorize("schedule", DELEGATE, "scheduler")
    plan = lr.LearningPlan(
        tenant,
        world.store.engagement_id,
        workflow,
        MANDATE,
        mandate["mandate_revision"],
        world.snapshots[run].snapshot_ref,
        "incumbent-1",
        request.benchmark_revision,
        "baseline-1",
        request.recipe.recipe_id,
        str(request.binding.route),
        True,
    )
    request = replace(request, plan_revision=digest(plan.document()))
    plan_ref = lr.publish_plan(world.dir, plan)
    resolved = {}
    dispatcher = world.dispatcher(ledger=MandateFitLedger(world.mandates))
    adapter = LearningFitLedger(
        dispatcher, actor=DELEGATE, client="local-worker", resolve=resolved.get
    )
    slack, service = lr.open_learning_runs(slack_path, ledger=adapter)
    scheduler, worker = lr.open_learning_runs(schedule_path, ledger=adapter)
    assert worker.schedule_tick(scheduler)["tick"] == "admitted"
    admitted = service.submit(
        slack, lr.SubmitRequest(plan_ref, "evaluate_and_improve", "slack-run")
    )
    claim = worker.claim(scheduler)
    assert claim and claim.run_ref == admitted["run_ref"]
    resolved[claim.run_ref] = request
    fit = dict(
        residual_ref=receipt["revision"],
        model_digest=fd.model_digest(request.binding),
        max_units=request.bound.maximum_seconds,
    )
    reserved = worker.reserve_fit(scheduler, claim, **fit)
    assert reserved["state"] == "reserved"
    assert worker.reserve_fit(scheduler, claim, **fit) == reserved
    execution = reserved["reservation_ref"]
    assert dispatcher.status(execution, actor=DELEGATE).submissions == 0
    if terminal == "cancel_before_submit":
        assert service.cancel(slack, claim.run_ref, "cancel")["cancellation"] == "pending"
        canceled = dispatcher.cancel(execution, actor=DELEGATE)
        assert canceled.stage == "settled" and canceled.usage["source"] == "not_submitted"
        assert (
            worker.confirm_cancel(scheduler, claim, consumed_units=0)["cancellation"] == "confirmed"
        )
        assert service.status(slack, claim.run_ref)["usage"]["consumed_units"] == 0
        assert world.backend.jobs_created() == 0
        return
    worker.begin_submission(scheduler, claim)
    started = dispatcher.advance(execution, actor=DELEGATE)
    assert started.submissions == 1 and started.job_ref
    worker.record_submission(scheduler, claim, execution_ref=started.job_ref)
    final = world.finish(dispatcher, execution)
    outcome = "completed"
    if terminal == "invalid_output":
        assert final.candidate_ref is None and final.reason == "invalid_output"
        for unwanted in ("review_ready", "completed"):
            with pytest.raises(lr.LearningRunError, match="fit_outcome_mismatch"):
                worker.finish(scheduler, claim, unwanted, consumed_units=final.usage["seconds"])
        outcome = "failed"
    worker.finish(scheduler, claim, outcome, consumed_units=final.usage["seconds"])
    current = service.status(slack, claim.run_ref)
    assert current["state"] == outcome
    assert current["usage"] == {
        "fit_reservations": 1,
        "reserved_units": request.bound.maximum_seconds,
        "consumed_units": final.usage["seconds"],
    }
    assert world.mandates.inspect(MANDATE)["reserved_fits"] == 1


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
