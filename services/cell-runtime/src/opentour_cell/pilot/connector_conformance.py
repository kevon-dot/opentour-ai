"""Reusable conformance and family-qualification harness for any ``model_connector.Connector``.

``run_conformance`` drives a connector only through ``Preflight`` (the sanctioned dispatch path)
and checks the lifecycle obligations every route shares: one execution per intent, recovery from a
lost acknowledgement, restart replay, cap refusal before dispatch, stale revision refusal, cancel
without a candidate and fence with zero further effects. ``qualify_family`` produces one separate
``QualificationRecord`` for one model family.

Simulated: the unit tests run these against generated toy families and in-memory doubles.
Not proven: passing here is software conformance (``EvidenceTier.FIXTURE`` for generated inputs).
It never stands in for a provider witness, installed model run or customer evidence. A check that
a harness cannot exercise is reported as NOT_EXERCISED and counts as not passing.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot.connector_ext import Fenceable
from opentour_cell.pilot.model_family import (
    Check,
    FixtureKind,
    ModelFamilyProfile,
    QualificationRecord,
    Role,
    build_record,
    verify_model_directory,
)

ACTOR = "training"


class Outcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    NOT_EXERCISED = "not_exercised"


@dataclass(frozen=True)
class ConformanceResult:
    name: str
    outcome: Outcome
    detail: str = ""


def all_passed(results: Sequence[ConformanceResult]) -> bool:
    return bool(results) and all(result.outcome is Outcome.PASSED for result in results)


@dataclass(frozen=True)
class ConnectorHarness:
    """What a route supplies so the shared suite can drive it."""

    make_connector: Callable[[Path], mc.Connector]
    binding: mc.ModelBinding
    mismatched_binding: mc.ModelBinding
    make_intent: Callable[[str], mc.FitIntent]
    rows: tuple[Mapping[str, object], ...]
    executions: Callable[[Path], int]
    make_slow_connector: Callable[[Path], mc.Connector] | None = None


def standard_grants(
    binding: mc.ModelBinding, intent: mc.FitIntent, *, now: float | None = None
) -> Callable[[str, mc.ModelBinding, mc.Purpose], mc.Grant | None]:
    """Generated owner grants for every purpose, expiring well after the test run."""
    expiry = int(time.time() if now is None else now) + 3600
    grants = {
        purpose: mc.Grant(
            ACTOR,
            binding,
            purpose,
            intent.grant_revision,
            expiry,
            intent.fields,
            expiry,
            intent.bound if purpose is mc.Purpose.TRAINING else None,
            frozenset({intent.benchmark_revision})
            if purpose is mc.Purpose.TRAINING
            else frozenset(),
        )
        for purpose in mc.Purpose
    }

    def current(actor: str, selected: mc.ModelBinding, purpose: mc.Purpose) -> mc.Grant | None:
        grant = grants.get(purpose)
        return grant if grant and (actor, selected) == (grant.actor, grant.binding) else None

    return current


class CountingGuard:
    """A reservation guard double. ``entries`` proves dispatch happened under the reservation."""

    def __init__(self) -> None:
        self.entries = 0

    def __call__(self) -> AbstractContextManager[None]:
        @contextmanager
        def guard() -> Iterator[None]:
            self.entries += 1
            yield

        return guard()


class _AckLoss:
    """Proxy: the backend accepts the job, then the acknowledgement is lost."""

    def __init__(self, inner: mc.Connector) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        self.inner.submit(intent, development)
        raise OSError("simulated:lost-acknowledgement")


class _Blind:
    """Proxy: lookup cannot verify anything (unknown remote state)."""

    def __init__(self, inner: mc.Connector) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        self.inner.submit(intent, development)
        raise OSError("simulated:lost-acknowledgement")

    def lookup(self, intent: mc.FitIntent) -> mc.JobReceipt | None:
        return None


class _WrongReceipt:
    """Proxy: the backend answers for a different intent."""

    def __init__(self, inner: mc.Connector) -> None:
        self.inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def submit(self, intent: mc.FitIntent, development: bytes) -> mc.JobReceipt:
        receipt = self.inner.submit(intent, development)
        return replace(receipt, intent=replace(intent, idempotency_key="generated-other-intent"))


def _preflight(
    connector: mc.Connector, harness: ConnectorHarness, intent: mc.FitIntent
) -> mc.Preflight:
    return mc.Preflight(connector, standard_grants(harness.binding, intent))


def _dispatch(
    preflight: mc.Preflight, harness: ConnectorHarness, intent: mc.FitIntent
) -> tuple[mc.JobReceipt, CountingGuard]:
    guard = CountingGuard()
    receipt = preflight.submit_reserved_fit(ACTOR, intent, harness.rows, reservation_guard=guard)
    return receipt, guard


def _refused(call: Callable[[], object], reason: str | None = None) -> bool:
    try:
        call()
    except (mc.CapabilityError, ValueError) as error:
        return reason is None or reason in str(error)
    return False


def _background_fit(
    pre: mc.Preflight, harness: ConnectorHarness, intent: mc.FitIntent
) -> tuple[threading.Thread, list[mc.JobReceipt]]:
    box: list[mc.JobReceipt] = []
    thread = threading.Thread(target=lambda: box.append(_dispatch(pre, harness, intent)[0]))
    thread.start()
    return thread, box


def _wait_for_job(connector: mc.Connector, intent: mc.FitIntent) -> mc.JobReceipt | None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        job = connector.lookup(intent)
        if job is not None:
            return job
        time.sleep(0.02)
    return None


def run_conformance(harness: ConnectorHarness, work_dir: Path) -> tuple[ConformanceResult, ...]:
    """Run every shared obligation in its own state directory.

    A check that raises is reported FAILED, so a broken connector cannot abort the suite.
    """

    def fresh(name: str) -> Path:
        path = work_dir / name
        path.mkdir(parents=True)
        return path

    def duplicate() -> tuple[bool, str]:
        state = fresh("duplicate")
        intent = harness.make_intent("generated-duplicate")
        pre = _preflight(harness.make_connector(state), harness, intent)
        first, guard = _dispatch(pre, harness, intent)
        second, _ = _dispatch(pre, harness, intent)
        count = harness.executions(state)
        return (
            first.job_ref == second.job_ref
            and first.state is mc.JobState.COMPLETED
            and count == 1
            and guard.entries == 1,
            f"executions={count}",
        )

    def lost_ack() -> tuple[bool, str]:
        state = fresh("lost-ack")
        intent = harness.make_intent("generated-lost-ack")
        pre = _preflight(_AckLoss(harness.make_connector(state)), harness, intent)
        recovered, _ = _dispatch(pre, harness, intent)
        return (
            recovered.state is mc.JobState.COMPLETED and harness.executions(state) == 1,
            f"state={recovered.state.value}",
        )

    def restart() -> tuple[bool, str]:
        state = fresh("restart")
        intent = harness.make_intent("generated-restart")
        original, _ = _dispatch(
            _preflight(harness.make_connector(state), harness, intent), harness, intent
        )
        restarted = _preflight(harness.make_connector(state), harness, intent)
        again = restarted.reconcile_fit(ACTOR, intent)
        candidate = restarted.collect_candidate(ACTOR, again) if again == original else None
        return again == original and candidate is not None and harness.executions(state) == 1, ""

    def unknown() -> tuple[bool, str]:
        state = fresh("unknown")
        intent = harness.make_intent("generated-unknown")
        blind = _preflight(_Blind(harness.make_connector(state)), harness, intent)
        receipt, _ = _dispatch(blind, harness, intent)
        return receipt.state is mc.JobState.UNKNOWN and harness.executions(state) == 1, ""

    def cap() -> tuple[bool, str]:
        state = fresh("cap")
        intent = harness.make_intent("generated-cap")
        tight = replace(intent, bound=replace(intent.bound, maximum_steps=1))
        pre = _preflight(harness.make_connector(state), harness, tight)
        guard = CountingGuard()
        refused = _refused(
            lambda: pre.submit_reserved_fit(ACTOR, tight, harness.rows, reservation_guard=guard)
        )
        return refused and harness.executions(state) == 0 and guard.entries == 0, ""

    def stale() -> tuple[bool, str]:
        state = fresh("stale")
        intent = replace(harness.make_intent("generated-stale"), binding=harness.mismatched_binding)
        pre = mc.Preflight(
            harness.make_connector(state), standard_grants(harness.mismatched_binding, intent)
        )
        capability = pre.discover(ACTOR, harness.mismatched_binding, mc.Purpose.TRAINING)
        return (
            capability.state is not mc.State.TRAINABLE
            and _refused(lambda: _dispatch(pre, harness, intent))
            and harness.executions(state) == 0,
            f"state={capability.state.value}",
        )

    def cancel() -> tuple[bool, str] | None:
        if harness.make_slow_connector is None:
            return None
        state = fresh("cancel")
        intent = harness.make_intent("generated-cancel")
        slow = harness.make_slow_connector(state)
        pre = _preflight(slow, harness, intent)
        thread, _ = _background_fit(pre, harness, intent)
        job = _wait_for_job(slow, intent)
        canceled = slow.cancel(job) if job is not None else None
        thread.join(timeout=30)
        final = slow.lookup(intent)
        return (
            canceled is not None
            and canceled.state is mc.JobState.CANCELED
            and final is not None
            and final.state is mc.JobState.CANCELED
            and _refused(lambda: pre.collect_candidate(ACTOR, final)),
            "",
        )

    def fence() -> tuple[bool, str] | None:
        state = fresh("fence")
        intent = harness.make_intent("generated-fence")
        connector = harness.make_connector(state)
        if not isinstance(connector, Fenceable):
            return None
        done, _ = _dispatch(_preflight(connector, harness, intent), harness, intent)
        connector.fence("generated-scope")
        later = replace(intent, idempotency_key="generated-after-fence")
        restarted = _preflight(harness.make_connector(state), harness, later)
        return (
            done.state is mc.JobState.COMPLETED
            and _refused(lambda: _dispatch(restarted, harness, later))
            and _refused(lambda: restarted.collect_candidate(ACTOR, done))
            and harness.executions(state) == 1,
            "",
        )

    def other_receipt() -> tuple[bool, str]:
        state = fresh("mismatch")
        intent = harness.make_intent("generated-mismatch")
        pre = _preflight(_WrongReceipt(harness.make_connector(state)), harness, intent)
        return (
            _refused(lambda: _dispatch(pre, harness, intent), "model_job_receipt_mismatched"),
            "",
        )

    def conflict() -> tuple[bool, str]:
        state = fresh("conflict")
        intent = harness.make_intent("generated-conflict")
        connector = harness.make_connector(state)
        _dispatch(_preflight(connector, harness, intent), harness, intent)
        other = replace(intent, benchmark_revision="generated-other-benchmark")
        other_pre = mc.Preflight(connector, standard_grants(harness.binding, other))
        return (
            _refused(lambda: _dispatch(other_pre, harness, other))
            and harness.executions(state) == 1,
            "",
        )

    checks: tuple[tuple[str, Callable[[], tuple[bool, str] | None]], ...] = (
        ("duplicate_dispatch_single_execution", duplicate),
        ("lost_acknowledgement_recovers_same_job", lost_ack),
        ("restart_replays_receipt_and_candidate", restart),
        ("unverifiable_lookup_is_unknown_and_not_retried", unknown),
        ("cap_exceeded_refused_before_dispatch", cap),
        ("revision_mismatch_needs_configuration_and_runs_nothing", stale),
        ("cancel_yields_no_candidate", cancel),
        ("fence_blocks_new_effects", fence),
        ("receipt_for_other_intent_rejected", other_receipt),
        ("idempotency_key_reuse_with_different_intent_refused", conflict),
    )
    results: list[ConformanceResult] = []
    for name, check in checks:
        try:
            outcome = check()
        except Exception as error:  # a broken connector must fail the check, not the suite
            results.append(ConformanceResult(name, Outcome.FAILED, type(error).__name__))
            continue
        if outcome is None:
            results.append(ConformanceResult(name, Outcome.NOT_EXERCISED))
        else:
            passed, detail = outcome
            results.append(
                ConformanceResult(name, Outcome.PASSED if passed else Outcome.FAILED, detail)
            )
    return tuple(results)


class FamilyConnector(mc.Connector, Protocol):
    """The connector surface the qualification harness needs beyond the base contract."""

    def evidence(self, job: mc.JobReceipt) -> Mapping[str, Any]: ...

    def promote(self, candidate: mc.Candidate) -> None: ...

    def rollback(self) -> str: ...

    def serve(self, inputs: tuple[Mapping[str, Any], ...]) -> tuple[Mapping[str, Any], ...]: ...

    def predict_base(
        self, inputs: tuple[Mapping[str, Any], ...]
    ) -> tuple[Mapping[str, Any], ...]: ...


@dataclass(frozen=True)
class FamilyScenario:
    """One model family's qualification inputs. Reference answers stay with the scorer."""

    profile: ModelFamilyProfile
    model_dir: Path
    fixture_kind: FixtureKind
    binding: mc.ModelBinding
    make_intent: Callable[[str], mc.FitIntent]
    training_rows: tuple[Mapping[str, object], ...]
    eval_inputs: tuple[Mapping[str, Any], ...]
    eval_references: tuple[str, ...]
    make_connector: Callable[[Path, Callable[[mc.FitIntent], bool] | None], FamilyConnector]
    make_incompatible_connector: Callable[[Path], FamilyConnector]
    make_slow_connector: Callable[[Path], FamilyConnector]


def _score(predictions: Sequence[Mapping[str, Any]], references: Sequence[str]) -> float:
    """The one exact-label scorer. Missing output counts as no match over the full denominator."""
    if len(predictions) != len(references):
        return -1.0
    hits = sum(1 for p, r in zip(predictions, references, strict=True) if p.get("label") == r)
    return hits / len(references)


def qualify_family(scenario: FamilyScenario, work_dir: Path) -> QualificationRecord:
    """Fit, replay, roll back, cancel and refuse for ONE family. Results are never shared."""
    results: dict[Check, bool] = {}
    details: dict[str, str] = {}
    profile = scenario.profile

    def intent(key: str) -> mc.FitIntent:
        return scenario.make_intent(key)

    def preflight(connector: mc.Connector, it: mc.FitIntent) -> mc.Preflight:
        return mc.Preflight(connector, standard_grants(scenario.binding, it))

    main = work_dir / "main"
    main.mkdir(parents=True)
    fit_intent = intent("generated-qualification-fit")
    connector = scenario.make_connector(main, None)
    pre = preflight(connector, fit_intent)
    guard = CountingGuard()
    receipt = pre.submit_reserved_fit(
        ACTOR, fit_intent, scenario.training_rows, reservation_guard=guard
    )
    completed = receipt.state is mc.JobState.COMPLETED and guard.entries == 1
    candidate = pre.collect_candidate(ACTOR, receipt)
    evidence = connector.evidence(receipt)

    results[Check.PARAMETER_UPDATE] = completed and int(evidence["parameters_changed"]) > 0
    details["parameters_changed"] = str(evidence["parameters_changed"])
    base_unchanged = (
        evidence["base_sha256_before"] == evidence["base_sha256_after"]
        and verify_model_directory(profile, scenario.model_dir) == profile.revision
    )
    results[Check.BASE_INTEGRITY] = base_unchanged

    baseline = connector.predict_base(scenario.eval_inputs)
    first = pre.connector.predict_for_replay(candidate, scenario.eval_inputs)
    second = scenario.make_connector(main, None).predict_for_replay(candidate, scenario.eval_inputs)
    results[Check.FRESH_PROCESS_INFERENCE] = first == second and len(first) == len(
        scenario.eval_inputs
    )
    base_score = _score(baseline, scenario.eval_references)
    tuned_score = _score(first, scenario.eval_references)
    details["scorer"] = profile.scorer
    details["baseline_score"] = f"{base_score:.4f}"
    details["tuned_score"] = f"{tuned_score:.4f}"
    results[Check.TASK_SCORER_MATCH] = base_score >= 0.0 and tuned_score >= 0.0

    connector.promote(candidate)
    served = connector.serve(scenario.eval_inputs)
    restored = connector.rollback()
    after = connector.serve(scenario.eval_inputs)
    results[Check.ROLLBACK] = (
        served == first
        and restored == "base"
        and after == baseline
        and verify_model_directory(profile, scenario.model_dir) == profile.revision
    )

    # Cancellation: a slow job is stopped and publishes nothing.
    cancel_dir = work_dir / "cancel"
    cancel_dir.mkdir()
    slow = scenario.make_slow_connector(cancel_dir)
    cancel_intent = intent("generated-qualification-cancel")
    cancel_pre = preflight(slow, cancel_intent)
    holder: dict[str, mc.JobReceipt] = {}
    thread = threading.Thread(
        target=lambda: holder.update(
            job=cancel_pre.submit_reserved_fit(
                ACTOR, cancel_intent, scenario.training_rows, reservation_guard=CountingGuard()
            )
        )
    )
    thread.start()
    deadline = time.monotonic() + 10
    live: mc.JobReceipt | None = None
    while time.monotonic() < deadline and live is None:
        live = slow.lookup(cancel_intent)
        time.sleep(0.02)
    stopped = slow.cancel(live) if live is not None else None
    thread.join(timeout=30)
    results[Check.CANCELLATION] = (
        stopped is not None
        and stopped.state is mc.JobState.CANCELED
        and not any((cancel_dir / "jobs").glob("*/artifact"))
    )

    # A sufficient simple repair authorizes zero fits.
    repair_dir = work_dir / "repair"
    repair_dir.mkdir()
    repair_intent = intent("generated-qualification-repair")
    repaired = scenario.make_connector(repair_dir, lambda _intent: False)
    repair_guard = CountingGuard()
    results[Check.SIMPLE_REPAIR_ZERO_FIT] = _refused(
        lambda: preflight(repaired, repair_intent).submit_reserved_fit(
            ACTOR, repair_intent, scenario.training_rows, reservation_guard=repair_guard
        )
    ) and not any((repair_dir / "jobs").iterdir())

    # An incompatible installation authorizes zero fits.
    bad_dir = work_dir / "incompatible"
    bad_dir.mkdir()
    bad = scenario.make_incompatible_connector(bad_dir)
    bad_intent = intent("generated-qualification-incompatible")
    bad_pre = preflight(bad, bad_intent)
    state = bad_pre.discover(ACTOR, scenario.binding, mc.Purpose.TRAINING).state
    results[Check.INCOMPATIBLE_MODEL_ZERO_FIT] = (
        state is not mc.State.TRAINABLE
        and _refused(
            lambda: bad_pre.submit_reserved_fit(
                ACTOR, bad_intent, scenario.training_rows, reservation_guard=CountingGuard()
            )
        )
        and not any((bad_dir / "jobs").iterdir())
    )

    if profile.role is Role.REFERENCE:
        again_dir = work_dir / "reproduce"
        again_dir.mkdir()
        again = scenario.make_connector(again_dir, None)
        again_intent = intent("generated-qualification-reproduce")
        again_pre = preflight(again, again_intent)
        rerun = again_pre.submit_reserved_fit(
            ACTOR, again_intent, scenario.training_rows, reservation_guard=CountingGuard()
        )
        results[Check.REFERENCE_REPRODUCED] = (
            rerun.state is mc.JobState.COMPLETED
            and again_pre.collect_candidate(ACTOR, rerun).artifact_or_serving_revision
            == candidate.artifact_or_serving_revision
        )
    return build_record(profile, scenario.fixture_kind, results, details)
