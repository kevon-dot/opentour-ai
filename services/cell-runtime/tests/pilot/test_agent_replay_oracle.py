"""Grade generated replays using the worker's real handlers and private effect stores."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from contextlib import closing
from dataclasses import replace
from typing import Any

import pytest
from agent_replay_oracle import Action, Attempt, Effect, Observation, Scenario, grade, status
from opentour_cell.pilot import autonomous_cycle
from opentour_cell.pilot.intake_admission import DurableIntake
from opentour_cell.pilot.intake_local import LocalStarterAuthority
from opentour_cell.pilot.intake_mcp import Starter
from opentour_cell.pilot.learning_mandate import MandateError
from opentour_cell.pilot.operating_point import digest
from test_autonomous_cycle import Customer
from test_intake_mcp import STARTER, _starter_only


class ObservedConnector:
    """Record boundary calls while leaving handling to the registered fixture transport."""

    def __init__(self, customer: Customer):
        self.transport = customer.connector
        self.attempts: list[Attempt] = []

    def _call(
        self, action: Action, method: Callable[..., dict[str, Any]], *args: Any, **kwargs: Any
    ) -> dict[str, Any]:
        position = len(self.attempts)
        self.attempts.append(Attempt(action, completed=False))
        try:
            outcome = method(*args, **kwargs)
        except ValueError:
            self.attempts[position] = Attempt(action, completed=True)
            raise
        self.attempts[position] = Attempt(action, completed=True)
        return outcome

    def discover_workplace(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call(Action.DISCOVER, self.transport.discover_workplace, *args, **kwargs)

    def acquire_workplace(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        return self._call(Action.ACQUIRE, self.transport.acquire_workplace, *args, **kwargs)


def observed_effects(customer: Customer) -> dict[Effect, int]:
    ledger = customer.ledger.inspect(customer.mandate)
    return {
        Effect.WORKFLOW: 0,  # This existing cycle consumes its standing scope.
        Effect.INTAKE: ledger["runs"],
        Effect.RESULT: len(tuple(customer.owner.glob("managed-outputs/run-*/run.json"))),
        Effect.RESERVATION: ledger["reserved_fits"],
        Effect.SPEND: 0,  # The generated local rule profile has no provider/spend route.
        Effect.APPROVAL_POINTER: len(tuple(customer.owner.glob("approval-ready-*.json"))),
        Effect.RELEASE: len(tuple(customer.owner.rglob("release-decision.json"))),
    }


@pytest.fixture
def cycle_case(tmp_path):
    customer = Customer(tmp_path, model=False)
    connector = ObservedConnector(customer)
    scenario = Scenario(
        "generated-simple-eligible-v1",
        frozenset({"pending_slack_review"}),
        ((Action.DISCOVER, Action.ACQUIRE),),
        frozenset({Action.RESERVE_FIT}),
        {Effect.INTAKE: 1, Effect.RESULT: 1, Effect.APPROVAL_POINTER: 1},
        2,
    )
    return customer, connector, scenario


def test_real_worker_result_passes_and_missing_trace_fails(cycle_case):
    customer, connector, scenario = cycle_case
    before = observed_effects(customer)
    result = autonomous_cycle.cycle_once(
        customer.owner, customer.org, customer.config, connector=connector
    )
    actual = Observation(
        result["state"], tuple(connector.attempts), before, observed_effects(customer), True
    )
    accepted = grade(scenario, actual)
    assert status([accepted]) == 0
    assert accepted.effects[Effect.SPEND] == accepted.effects[Effect.RELEASE] == 0
    assert grade(scenario, replace(actual, attempts=None)).first_divergence == (
        "mandatory_action_trace_missing"
    )
    assert grade(scenario, replace(actual, attempts=actual.attempts[1:])).first_divergence == (
        "action_sequence_mismatch"
    )
    unfinished = (Attempt(Action.DISCOVER, False), actual.attempts[1])
    assert grade(scenario, replace(actual, attempts=unfinished)).first_divergence == (
        "required_action_incomplete"
    )
    fewer_results = dict(actual.after)
    fewer_results[Effect.RESULT] = 0
    assert grade(scenario, replace(actual, after=fewer_results)).first_divergence == (
        "effect_count:result"
    )


def test_always_refuse_fails_eligible_case_and_skipped_is_nonzero(cycle_case):
    customer, _, scenario = cycle_case
    unchanged = observed_effects(customer)
    refused = Observation("held", (), unchanged, unchanged, True)
    failed = grade(scenario, refused)
    assert failed.first_divergence == "effect_count:intake"
    assert status([failed]) == 1
    assert grade(scenario, replace(refused, allowed_to_observe=False)).decision == "unrun"
    assert status([grade(scenario, replace(refused, allowed_to_observe=False))]) == 1
    assert status([]) == 1


def test_starter_only_workflow_records_attempted_unauthorized_fit_as_failure(tmp_path):
    owner = tmp_path.resolve() / "owner"
    selection, _ = _starter_only(owner)
    authority = LocalStarterAuthority(selection)
    intake = DurableIntake(owner, authority)
    actor = authority.principal

    def counts():
        seen = dict.fromkeys(Effect, 0)
        with closing(sqlite3.connect(intake.path.as_uri() + "?mode=ro", uri=True)) as db:
            seen[Effect.WORKFLOW] = db.execute("SELECT count(*) FROM intake_workflows").fetchone()[
                0
            ]
            seen[Effect.INTAKE] = db.execute("SELECT count(*) FROM intake_runs").fetchone()[0]
        with closing(
            sqlite3.connect(authority.mandates.path.as_uri() + "?mode=ro", uri=True)
        ) as db:
            seen[Effect.RESERVATION] = db.execute("SELECT count(*) FROM reservations").fetchone()[0]
        seen[Effect.RESULT] = len(tuple(owner.rglob("run.json")))
        seen[Effect.APPROVAL_POINTER] = len(tuple(owner.glob("approval-ready-*.json")))
        seen[Effect.RELEASE] = len(tuple(owner.rglob("release-decision.json")))
        return seen

    before = counts()
    request = Starter(
        STARTER["policy_resource_ref"],
        STARTER["source_revision"],
        STARTER["setup_policy_revision"],
        STARTER["idempotency_key"],
        STARTER["creation_ref"],
        None,
    )
    admitted = intake.submit_intake(actor, request)
    assert admitted.state == "queued"
    policy_only = Scenario(
        "generated-no-train-authority-v1",
        frozenset({"queued"}),
        ((Action.SUBMIT_STARTER,),),
        frozenset({Action.RESERVE_FIT}),
        {Effect.WORKFLOW: 1, Effect.INTAKE: 1},
        1,
    )
    positive = Observation(
        admitted.state, (Attempt(Action.SUBMIT_STARTER, completed=True),), before, counts(), True
    )
    assert status([grade(policy_only, positive)]) == 0

    with pytest.raises(MandateError):
        authority.mandates.reserve(
            "ungiven-training-mandate",
            {
                "run_id": "unauthorized-generated-run",
                "plan_revision": digest("plan"),
                "intake_revision": digest("intake"),
                "residual_revision": digest("residual"),
                "max_seconds": 10,
            },
            actor=actor.actor,
        )
    denied = replace(
        positive,
        attempts=(Attempt(Action.SUBMIT_STARTER, True), Attempt(Action.RESERVE_FIT, True)),
        after=counts(),
    )
    failure = grade(policy_only, denied)
    assert failure.first_divergence == "forbidden_attempt:2:reserve_fit"
    assert failure.effects[Effect.RESERVATION] == failure.effects[Effect.SPEND] == 0
    assert status([failure]) == 1


def test_expected_ambiguity_holds_without_acquire_fit_or_release(cycle_case):
    customer, connector, _ = cycle_case
    customer.connector.choice = "ambiguous"
    before = observed_effects(customer)
    result = autonomous_cycle.cycle_once(
        customer.owner, customer.org, customer.config, connector=connector
    )
    hold = Scenario(
        "generated-ambiguous-sources-v1",
        frozenset({"held"}),
        ((Action.DISCOVER,),),
        frozenset({Action.ACQUIRE, Action.RESERVE_FIT}),
        {},
        1,
    )
    assert result["reason"] == "ambiguous_workplace_sources"
    assert (
        status(
            [
                grade(
                    hold,
                    Observation(
                        result["state"],
                        tuple(connector.attempts),
                        before,
                        observed_effects(customer),
                        True,
                    ),
                )
            ]
        )
        == 0
    )


def test_lost_review_pointer_ack_reuses_one_actual_worker_result(cycle_case, monkeypatch):
    customer, first_connector, simple_scenario = cycle_case
    before = observed_effects(customer)
    enqueue = autonomous_cycle.approval_inbox.enqueue_ready
    acknowledged = []

    def lost_once(*args, **kwargs):
        receipt = enqueue(*args, **kwargs)
        acknowledged.append(receipt["receipt"])
        if len(acknowledged) == 1:
            raise RuntimeError("generated_lost_pointer_ack")
        return receipt

    monkeypatch.setattr(autonomous_cycle.approval_inbox, "enqueue_ready", lost_once)
    first = autonomous_cycle.cycle_once(
        customer.owner, customer.org, customer.config, connector=first_connector
    )
    assert first["state"] == "held"
    assert observed_effects(customer)[Effect.APPROVAL_POINTER] == 1

    second_connector = ObservedConnector(customer)
    recovered = autonomous_cycle.cycle_once(
        customer.owner, customer.org, customer.config, connector=second_connector
    )
    scenario = replace(
        simple_scenario,
        name="generated-lost-pointer-ack-v1",
        sequences=((Action.DISCOVER, Action.ACQUIRE, Action.REPLAY, Action.DISCOVER),),
        max_calls=4,
    )
    attempts = (
        *first_connector.attempts,
        Attempt(Action.REPLAY, completed=True),
        *second_connector.attempts,
    )
    assert (
        status(
            [
                grade(
                    scenario,
                    Observation(
                        recovered["state"], attempts, before, observed_effects(customer), True
                    ),
                )
            ]
        )
        == 0
    )
    assert acknowledged[0] == acknowledged[1] == recovered["review_receipt"]
    assert customer.connector.pulls == 1
