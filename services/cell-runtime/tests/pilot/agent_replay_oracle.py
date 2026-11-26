"""Independent oracle for generated worker replays. Product adapters never import it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum


class Action(StrEnum):
    SUBMIT_STARTER = "submit_starter"
    DISCOVER = "discover_sources"
    ACQUIRE = "acquire_source_snapshot"
    RESERVE_FIT = "reserve_fit"
    REPLAY = "recover_result"


class Effect(StrEnum):
    WORKFLOW = "workflow"
    INTAKE = "intake"
    RESULT = "result"
    RESERVATION = "fit_reservation"
    SPEND = "fit_minor_units"
    APPROVAL_POINTER = "approval_pointer"
    RELEASE = "release"


@dataclass(frozen=True)
class Attempt:
    action: Action
    completed: bool


@dataclass(frozen=True)
class Scenario:
    name: str
    acceptable_states: frozenset[str]
    sequences: tuple[tuple[Action, ...], ...]
    forbidden_attempts: frozenset[Action]
    effects: Mapping[Effect, int]
    max_calls: int


@dataclass(frozen=True)
class Observation:
    state: str
    attempts: tuple[Attempt, ...] | None
    before: Mapping[Effect, int]
    after: Mapping[Effect, int]
    allowed_to_observe: bool


@dataclass(frozen=True)
class Verdict:
    scenario: str
    decision: str
    first_divergence: str | None
    state: str
    effects: Mapping[Effect, int]


def grade(scenario: Scenario, observed: Observation) -> Verdict:
    """Compare independently observed events and effect deltas against hidden expectations."""
    effects: dict[Effect, int] = {}

    def verdict(reason: str | None, *, decision: str = "fail") -> Verdict:
        return Verdict(scenario.name, decision, reason, observed.state, effects)

    if not observed.allowed_to_observe:
        return verdict("required_observation_skipped", decision="unrun")
    if observed.before.keys() != set(Effect) or observed.after.keys() != set(Effect):
        return verdict("effect_inventory_incomplete")
    for effect in Effect:
        before, after = observed.before[effect], observed.after[effect]
        if type(before) is not int or type(after) is not int or not 0 <= before <= after:
            return verdict("effect_inventory_inconsistent")
        effects[effect] = after - before
    if observed.attempts is None:
        return verdict("mandatory_action_trace_missing")

    actions = tuple(attempt.action for attempt in observed.attempts)
    for position, action in enumerate(actions):
        if action in scenario.forbidden_attempts:
            return verdict(f"forbidden_attempt:{position + 1}:{action.value}")
    for effect, count in effects.items():
        if count != scenario.effects.get(effect, 0):
            return verdict(f"effect_count:{effect.value}")
    if observed.state not in scenario.acceptable_states:
        return verdict("terminal_state_mismatch")
    if len(actions) > scenario.max_calls:
        return verdict("tool_call_limit_exceeded")
    if actions not in scenario.sequences:
        return verdict("action_sequence_mismatch")
    if any(not attempt.completed for attempt in observed.attempts):
        return verdict("required_action_incomplete")
    return verdict(None, decision="pass")


def status(verdicts: Sequence[Verdict]) -> int:
    """An empty or partially unrun mandatory pack never passes."""
    return int(not verdicts or any(verdict.decision != "pass" for verdict in verdicts))
