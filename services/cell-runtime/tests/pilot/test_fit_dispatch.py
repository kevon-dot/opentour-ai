"""OPE-418: reserve, dispatch and settle one authorized fit, against a simulated backend.

Generated sentinel fixtures only. The ledger is the tests-tree durable double of the OPE-442 port
(``fit_dispatch_doubles.SqliteFitLedger``), the mandate store and the residual router are the
real ``LearningMandates`` and ``RepairRoutingStore``, and the backend is ``SimulatedBackend``
behind the real OPE-506 ``Preflight``. Nothing contacts a provider, a model or real weights.
"""

from __future__ import annotations

import hashlib
import threading
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fit_dispatch_doubles import (
    BackendConfig,
    FailingEarlyStop,
    FakeClock,
    OwnHold,
    SimJob,
    SimulatedBackend,
    SqliteFitLedger,
    WithoutAbsence,
    WithoutEarlyStop,
    WorkerCrash,
    nonfinite,
)
from opentour_cell.pilot import fit_dispatch as fd
from opentour_cell.pilot import learning_mandate as lm
from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot import open_weight
from opentour_cell.pilot import repair_routing as rr
from opentour_cell.pilot.connector_ext import Usage, UsageSource
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.simple_repair import declared_rule
from opentour_cell.pilot.training import LocalTrainingStore

Rows = Callable[[Mapping[str, Any]], tuple[Mapping[str, object], ...]]
ORG = "generated-org"
OWNER = "generated-owner"
DELEGATE = "local-worker"
MANDATE = "standing-one"
AS_OF = 10_000
PROFILE = open_weight.PROFILE
ROWS = tuple({"question": f"generated question {i}"} for i in range(24))
DEV_DIGEST = hashlib.sha256(canonical([dict(r) for r in ROWS]).encode()).hexdigest()
BOUND = mc.FitBound("USD", 0, 300, 24, 100)
PAID = mc.FitBound("USD", 500, 300, 24, 100)
PLATEAU = fd.DispatchRecipe(
    "generated-recipe",
    1,
    "grouped",
    "macro_f1",
    "maximize",
    fd.EARLY_STOP_PLATEAU,
    100,
    10,
    0.01,
    2,
)
FIXED = replace(PLATEAU, stopping_rule=fd.FIXED_STEPS)
TERMS = rr.RoutingTerms(
    error_family=("b", "a"),
    min_independent_groups=2,
    recurrence_window_seconds=5_000,
    material_residual_cases=2,
    min_coverage_bps=5_000,
    max_wrong_automatic_bps=0,
    critical_slices=(),
    min_development_cases=3,
    min_groups_per_label=2,
    documentation_revision="generated-docs-1",
)
RULE = declared_rule(
    feature="bucket",
    allowed_features=["bucket"],
    mapping={"x": "a"},
    fallback="incumbent",
    allowed_labels=["a", "b"],
)


def binding(route: mc.Route = mc.Route.LOCAL, **kw: Any) -> mc.ModelBinding:
    base = {
        "tenant": ORG,
        "model_ref": PROFILE,
        "immutable_revision": open_weight.BASE_SHA256,
        "task": "label_correction",
        "input_schema": "generated-input-v1",
        "output_schema": "generated-output-v1",
        "preprocessing_revision": "generated-preprocessing-v1",
        "route": route,
        "account_or_host": "generated-worker-host",
        "region": "customer-local",
        "credential_ref": "generated-credential-ref",
    }
    return mc.ModelBinding(**(base | kw))


MANAGED = binding(
    mc.Route.MANAGED,
    account_or_host="simulated:customer-provider-account",
    region="simulated-region-1",
)


def case(i: str, bucket: str, inc: str, ref: str) -> rr.DevelopmentCase:
    return rr.DevelopmentCase(
        case_id=f"generated-case-{i}",
        entity_id=f"generated-entity-{i}",
        family_id=f"generated-family-{i}",
        decided_at=9_000,
        features={"bucket": bucket},
        incumbent={"status": "automatic", "label": inc},
        reference=ref,
    )


def group(prefix: str, n: int, bucket: str, inc: str, ref: str) -> list[rr.DevelopmentCase]:
    return [case(f"{prefix}{k}", bucket, inc, ref) for k in range(n)]


RESIDUAL = (
    group("fx", 2, "x", "b", "a") + group("rz", 3, "z", "b", "a") + group("ok", 3, "y", "b", "b")
)
RULE_SUFFICES = group("fx", 4, "x", "b", "a") + group("ok", 3, "y", "b", "b")


class Verifier:
    def verify(self, policy: rr.SignedPolicy) -> bool:
        return True


class GrantBook:
    """Owner-local OPE-506 grants with a fixed expiry, so repeated lookups compare equal."""

    def __init__(self, clock: FakeClock, fit_bound: mc.FitBound) -> None:
        self.fit_bound = fit_bound
        self.expires = int(clock()) + 7_200
        self.revoked = False
        self.disclosure_fields = frozenset({"question"})

    def current(self, actor: str, model: mc.ModelBinding, purpose: mc.Purpose) -> mc.Grant | None:
        if self.revoked or actor != DELEGATE:
            return None
        if purpose not in (mc.Purpose.TRAINING, mc.Purpose.DISCLOSURE):
            return None
        return mc.Grant(
            actor,
            model,
            purpose,
            "generated-grant-1",
            self.expires,
            frozenset({"question"}) if purpose is mc.Purpose.TRAINING else self.disclosure_fields,
            self.expires,
            self.fit_bound if purpose is mc.Purpose.TRAINING else None,
            frozenset({"generated-benchmark-1"}),
        )


class AuthoritySeam:
    """Test-only: the mandate schema cannot hold a paid cap or a concurrency setting today."""

    def __init__(self, mandates: lm.LearningMandates, **overrides: Any) -> None:
        self.mandates = mandates
        self.caps = {
            k: v
            for k, v in overrides.items()
            if k.startswith("max_") and k != "max_concurrent_fits"
        }
        self.extra = {k: v for k, v in overrides.items() if k not in self.caps}

    def dispatch_authority(self, mandate_id: str, run_id: str, *, actor: str) -> Mapping[str, Any]:
        details = dict(self.mandates.dispatch_authority(mandate_id, run_id, actor=actor))
        details["caps"] = dict(details["caps"]) | self.caps
        return details | self.extra


class RoutingSpy:
    """The real router, recording when ``current_eligibility`` runs."""

    def __init__(self, store: rr.RepairRoutingStore, ledger: SqliteFitLedger, log: list[str]):
        self.store, self.ledger, self.log = store, ledger, log

    def read(self, evaluation_id: str) -> dict[str, Any] | None:
        return self.store.read(evaluation_id)

    def current_eligibility(self, evaluation_id: str, **kw: Any) -> dict[str, Any]:
        inside = getattr(self.ledger.in_reserve, "active", False)
        self.log.append("eligibility_in_reservation" if inside else "eligibility")
        return self.store.current_eligibility(evaluation_id, **kw)


def spec(**kw: Any) -> dict[str, Any]:
    base = {
        "mandate_id": MANDATE,
        "owner_id": OWNER,
        "delegate": DELEGATE,
        "mode": "autonomous",
        "workflow": "support",
        "connectors": ["approved-mcp"],
        "trace_source": "tickets",
        "correction_source": "outcomes",
        "field": "route",
        "fields": ["question"],
        "labels": ["a", "b"],
        "profile": PROFILE,
        "uses": ["inspect", "evaluate", "train"],
        "permitted_error_pairs": [["b", "a"]],
        "agreement_revision": digest("generated-operating-limits"),
        "trainer": DELEGATE,
        "payer": "generated-payer",
        "reviewer": "generated-reviewer",
        "audience": "generated-private-review",
        "destination": "customer-local",
        "expires_at": 4_000_000_000,
        "max_runs": 5,
        "max_fits": 2,
        "max_total_seconds": 900,
        "max_seconds_per_fit": 300,
        "max_rows_per_run": 40,
        "max_total_rows": 200,
        "max_cost_cents": 0,
    }
    return base | kw


class World:
    def __init__(
        self,
        tmp_path: Path,
        *,
        backend: type[SimulatedBackend] = SimulatedBackend,
        config: BackendConfig | None = None,
        mandate: Mapping[str, Any] | None = None,
        fit_bound: mc.FitBound = BOUND,
        **seam: Any,
    ) -> None:
        self.clock = FakeClock()
        self.dir = tmp_path / "engagement"
        self.dir.mkdir(mode=0o700, parents=True)
        self.store = LocalTrainingStore(self.dir / "training.sqlite3", ORG)
        self.mandates = lm.LearningMandates(self.dir, ORG, self.store.engagement_id, owner=OWNER)
        self.mandates.enroll(dict(mandate or spec()), actor=OWNER)
        (self.dir / "routing").mkdir(mode=0o700)
        self.router = rr.RepairRoutingStore(self.dir / "routing")
        self.snapshots: dict[str, rr.AdmittedSnapshot] = {}
        self.backend = backend(self.clock, config)
        self.grants = GrantBook(self.clock, fit_bound)
        self.preflight = mc.Preflight(self.backend, self.grants.current, clock=self.clock)
        self.ledger = SqliteFitLedger(self.dir / "fit-ledger.sqlite3", self.clock)
        self.log: list[str] = self.backend.log
        self.routing = RoutingSpy(self.router, self.ledger, self.log)
        self.authority: Any = AuthoritySeam(self.mandates, **seam) if seam else self.mandates
        self.rows: Rows = lambda record: ROWS

    def admit(
        self,
        idx: int = 1,
        *,
        cases: list[rr.DevelopmentCase] = RESIDUAL,
        recipe: fd.DispatchRecipe = PLATEAU,
        model_ref: str = PROFILE,
    ) -> tuple[str, dict[str, Any]]:
        run = self.mandates.authorize_source(
            MANDATE,
            {
                "source_revision": digest(["source", idx]),
                "trace_sha256": digest(["traces", idx]),
                "correction_sha256": digest(["outcomes", idx]),
                "workflow": "support",
                "connector": "approved-mcp",
                "trace_source": "tickets",
                "correction_source": "outcomes",
                "field": "route",
                "fields": ["question"],
                "rows": 24,
            },
            actor=DELEGATE,
        )["run_id"]
        snapshot = rr.AdmittedSnapshot(
            ORG,
            "generated-workflow",
            run,
            f"generated-snapshot-{idx}",
            "generated-evidence-1",
            "generated-docs-1",
            AS_OF,
        )
        self.snapshots[run] = snapshot
        scheduling = rr.ComparisonScheduling(
            digest("generated-intake"),
            ("generated-protected-1", "generated-protected-2"),
            "generated-incumbent-1",
            "generated-audience",
        )
        routing_recipe = rr.Recipe(
            recipe.recipe_id,
            recipe.version,
            models=frozenset({model_ref}),
            tasks=frozenset({"label_correction"}),
            split=recipe.validation_split,
            metric=recipe.metric,
            direction=recipe.direction,
            allowed_params=(),
            trial_limit=1,
            step_limit=recipe.step_limit,
            stopping_rule=recipe.stopping_rule,
            est_seconds_per_trial=100,
        )
        receipt = self.router.evaluate(
            rr.RoutingRequest(
                terms=TERMS,
                policy=rr.SignedPolicy(
                    "generated-policy-1", snapshot.snapshot_ref, TERMS.revision, "simulated:sig"
                ),
                verifier=Verifier(),
                snapshot=snapshot,
                mandate=rr.MandateView.from_inspection(self.mandates.inspect(MANDATE)),
                cases=cases,
                rule=RULE,
                recipes=(routing_recipe,),
                model=rr.ModelTarget(model_ref, "label_correction", "generated-capability-1"),
                scheduling=scheduling if cases is RULE_SUFFICES else None,
            )
        )
        return run, receipt

    def request(self, run: str, receipt: Mapping[str, Any], **kw: Any) -> fd.DispatchRequest:
        base: dict[str, Any] = {
            "tenant": ORG,
            "engagement": self.store.engagement_id,
            "actor": DELEGATE,
            "mandate_id": MANDATE,
            "run_id": run,
            "evaluation_id": receipt["evaluation_id"],
            "evaluation_revision": receipt["revision"],
            "plan_revision": digest(["plan", run]),
            "binding": binding(),
            "benchmark_revision": "generated-benchmark-1",
            "grant_revision": "generated-grant-1",
            "development_digest": DEV_DIGEST,
            "fields": frozenset({"question"}),
            "bound": BOUND,
            "recipe": PLATEAU,
            "license_ref": "generated-license-apache-2.0",
            "capability_receipt_ref": "generated-capability-1",
        }
        return fd.DispatchRequest(**(base | kw))

    def dispatcher(self, worker: str = "worker-a", *, ledger: Any = "default") -> fd.FitDispatcher:
        return fd.FitDispatcher(
            ledger=self.ledger if ledger == "default" else ledger,
            preflight=self.preflight,
            mandates=self.authority,
            routing=self.routing,
            evidence=self.snapshots.__getitem__,
            development=lambda record: self.rows(record),
            worker_id=worker,
            clock=self.clock,
        )

    def finish(
        self, dispatcher: fd.FitDispatcher, execution_id: str, ticks: int = 40
    ) -> fd.DispatchStatus:
        status = dispatcher.status(execution_id, actor=DELEGATE)
        for _ in range(ticks):
            status = dispatcher.advance(execution_id, actor=DELEGATE)
            if status.stage in ("held", "settled", "submission_uncertain"):
                return status
        return status


def started(world: World, idx: int = 1, **kw: Any) -> tuple[fd.FitDispatcher, fd.DispatchStatus]:
    run, receipt = world.admit(idx)
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt, **kw), client="generated-client", operation_key=f"op-{idx}"
    )
    return dispatcher, status


# --- Fail closed before any reservation -------------------------------------------------------


def test_without_a_ledger_nothing_is_reserved_or_sent(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()
    world.log.clear()
    status = world.dispatcher(ledger=None).dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert (status.learning_run_state, status.reason) == ("held", "fit_ledger_unavailable")
    assert (status.reservations, status.submissions, status.execution_id) == (0, 0, None)
    assert (status.hold_class, status.next_owner) == ("ledger", "operator")
    assert world.log == []  # no eligibility read, no backend metadata call, no submission


def test_a_rule_that_suffices_starts_zero_fits(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit(cases=RULE_SUFFICES)
    assert receipt["outcome"] == "simple_repair"
    status = world.dispatcher().dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert (status.learning_run_state, status.reason, status.reservations) == (
        "held",
        "not_fit_eligible",
        0,
    )
    assert world.ledger.count("entries") == 0
    assert world.backend.calls.get("submit", 0) == 0 and world.backend.jobs_created() == 0


def test_backend_without_training_is_benchmark_only_with_zero_fits(tmp_path: Path) -> None:
    world = World(tmp_path, config=BackendConfig(training_supported=False))
    _, status = started(world)
    assert (status.learning_run_state, status.reason) == ("benchmark_only", "training_unavailable")
    assert (status.reservations, status.submissions) == (0, 0)
    assert world.ledger.count("entries") == 0 and world.backend.jobs_created() == 0


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (BackendConfig(estimate_known=False), "known_training_cost_within_budget_required"),
        (BackendConfig(exact_revision=False), "exact_model_revision_unverified"),
        (BackendConfig(hard_cost_cap=False), "enforceable_training_budget_required"),
    ],
)
def test_unknown_cost_or_unsupported_hardware_is_a_setup_hold(
    tmp_path: Path, config: BackendConfig, reason: str
) -> None:
    world = World(tmp_path, config=config)
    _, status = started(world)
    assert (status.learning_run_state, status.reason, status.hold_class) == (
        "held",
        reason,
        "setup",
    )
    assert status.reservations == 0 and world.ledger.count("entries") == 0
    assert world.backend.calls.get("submit", 0) == 0


def test_recipe_model_capability_and_receipt_revisions_must_match(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()
    dispatcher = world.dispatcher()
    cases = {
        "recipe_mismatch": {"recipe": replace(PLATEAU, step_limit=90)},
        "model_mismatch": {"binding": binding(task="other_task")},
        "capability_mismatch": {"capability_receipt_ref": "generated-capability-2"},
        "evaluation_revision_changed": {"evaluation_revision": digest("stale")},
        "per_fit_budget_exceeded": {"bound": mc.FitBound("USD", 0, 301, 24, 100)},
        "money_cap_currency_mismatch": {"bound": mc.FitBound("EUR", 0, 300, 24, 100)},
        "mandate_cap_required": {"bound": PAID},
        "recipe_exceeds_fit_bound": {"bound": mc.FitBound("USD", 0, 300, 24, 90)},
        "tenant_mismatch": {"tenant": "generated-other-org"},
        "engagement_mismatch": {"engagement": "generated-other-engagement"},
    }
    for reason, change in cases.items():
        status = dispatcher.dispatch(
            world.request(run, receipt, **change), client="generated-client", operation_key=reason
        )
        assert (status.reason, status.reservations) == (reason, 0), reason
    assert world.ledger.count("entries") == 0 and world.backend.calls.get("submit", 0) == 0


def test_a_model_outside_the_mandate_profile_is_refused(tmp_path: Path) -> None:
    world = World(tmp_path)
    other = "simulated:other-model"
    run, receipt = world.admit(model_ref=other)
    status = world.dispatcher().dispatch(
        world.request(run, receipt, binding=binding(model_ref=other)),
        client="generated-client",
        operation_key="op-1",
    )
    assert (status.reason, status.hold_class, status.reservations) == (
        "model_outside_mandate_profile",
        "authority",
        0,
    )


def test_early_stop_recipe_holds_on_a_backend_without_early_stop(tmp_path: Path) -> None:
    world = World(tmp_path, backend=WithoutEarlyStop)
    _, status = started(world)
    assert (status.learning_run_state, status.reason) == ("held", "early_stop_unsupported")
    assert status.reservations == 0 and world.ledger.count("entries") == 0


# --- The OPE-417 contract: eligibility immediately before reserving and before starting ------


def test_eligibility_is_recomputed_inside_the_reservation_and_before_submission(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()
    world.log.clear()
    status = world.dispatcher().dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert status.learning_run_state == "running"
    assert "eligibility_in_reservation" in world.log
    before_submit = [x for x in world.log[: world.log.index("submit")] if x.startswith("elig")]
    assert before_submit[-1] == "eligibility"  # the submission guard's own recheck
    assert world.log.index("eligibility_in_reservation") < world.log.index("submit")


def test_a_fit_eligible_receipt_alone_never_reserves(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()

    def evidence_moves() -> None:
        world.snapshots[run] = replace(
            world.snapshots[run], evidence_revision="generated-evidence-2"
        )

    world.ledger.before_recheck = evidence_moves
    status = world.dispatcher().dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert (status.reason, status.reservations) == ("evidence_revision_changed", 0)
    assert world.ledger.count("entries") == 0 and world.backend.jobs_created() == 0


# --- Concurrency, budget and canonical intent ------------------------------------------------


def _race(world: World, runs: list[tuple[str, dict[str, Any]]]) -> list[fd.DispatchStatus]:
    barrier = threading.Barrier(len(runs))
    results: list[fd.DispatchStatus | None] = [None] * len(runs)

    def go(i: int) -> None:
        run, receipt = runs[i]
        dispatcher = world.dispatcher(f"worker-{i}")
        barrier.wait()
        results[i] = dispatcher.dispatch(
            world.request(run, receipt), client=f"generated-client-{i}", operation_key=f"op-{i}"
        )

    threads = [threading.Thread(target=go, args=(i,)) for i in range(len(runs))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert all(r is not None for r in results)
    return [r for r in results if r is not None]


def test_two_concurrent_dispatches_with_budget_for_one_admit_exactly_one(tmp_path: Path) -> None:
    world = World(tmp_path, mandate=spec(max_fits=1), max_concurrent_fits=2)
    statuses = _race(world, [world.admit(1), world.admit(2)])
    winners = [s for s in statuses if s.reservations == 1]
    losers = [s for s in statuses if s.reservations == 0]
    assert len(winners) == 1 and len(losers) == 1
    assert winners[0].learning_run_state == "running"
    assert (losers[0].learning_run_state, losers[0].hold_class) == ("held", "budget")
    assert losers[0].reason == "training_budget_required"
    assert world.ledger.count("entries") == 1 and world.backend.jobs_created() == 1


def test_concurrency_limit_holds_the_second_fit_until_the_first_settles(tmp_path: Path) -> None:
    world = World(tmp_path)  # budget for two fits, default concurrency of one
    first, second = world.admit(1), world.admit(2)
    statuses = _race(world, [first, second])
    winner = next(s for s in statuses if s.reservations == 1)
    loser = next(s for s in statuses if s.reservations == 0)
    assert (loser.reason, loser.next_action) == (
        "concurrent_fit_limit_reached",
        "wait_for_open_fit_to_settle",
    )
    assert winner.execution_id is not None
    assert world.finish(world.dispatcher(), winner.execution_id).stage == "settled"
    retry_run, retry_receipt = second if winner.lineage["run_id"] == first[0] else first
    again = world.dispatcher().dispatch(
        world.request(retry_run, retry_receipt), client="generated-client-9", operation_key="op-9"
    )
    assert (again.reservations, again.learning_run_state) == (1, "running")
    assert world.backend.jobs_created() == 2


def test_same_intent_under_two_keys_lost_response_and_restart_is_one_fit(tmp_path: Path) -> None:
    # Budget and concurrency for two: only canonical-intent deduplication keeps this at one.
    world = World(tmp_path, config=BackendConfig(lose_ack_on_submit=1), max_concurrent_fits=2)
    run, receipt = world.admit()
    request = world.request(run, receipt)
    first = world.dispatcher("worker-a").dispatch(
        request, client="generated-slack", operation_key="op-slack"
    )
    assert first.learning_run_state == "running"  # the lost acknowledgement was reconciled
    # The caller never saw that response. The worker restarts; the scheduler retries.
    restarted = world.dispatcher("worker-b")
    second = restarted.dispatch(request, client="generated-scheduler", operation_key="op-tick")
    assert second.execution_id == first.execution_id and second.replayed
    assert second.execution_id is not None
    final = world.finish(restarted, second.execution_id)
    assert (final.learning_run_state, final.stage) == ("review_ready", "settled")
    assert world.ledger.count("entries") == 1 and world.ledger.count("aliases") == 2
    assert world.backend.jobs_created() == 1 and world.ledger.settlements() == 1
    assert world.backend.calls["submit"] == 1
    replay = restarted.dispatch(request, client="generated-slack", operation_key="op-slack")
    assert (replay.execution_id, replay.stage, replay.replayed) == (
        first.execution_id,
        "settled",
        True,
    )
    changed = restarted.dispatch(
        replace(request, benchmark_revision="generated-benchmark-2"),
        client="generated-slack",
        operation_key="op-slack",
    )
    assert (changed.reason, changed.reservations) == ("fit_operation_key_conflict", 0)
    widened = restarted.dispatch(
        replace(request, bound=mc.FitBound("USD", 0, 299, 24, 100)),
        client="generated-other",
        operation_key="op-new",
    )
    assert (widened.reason, widened.reservations) == ("fit_intent_conflict", 0)
    assert world.ledger.count("entries") == 1 and world.backend.jobs_created() == 1


def test_operation_keys_are_scoped_to_the_authenticated_client(tmp_path: Path) -> None:
    world = World(tmp_path, max_concurrent_fits=2)
    a_run, a_receipt = world.admit(1)
    b_run, b_receipt = world.admit(2)
    dispatcher = world.dispatcher()
    a = dispatcher.dispatch(world.request(a_run, a_receipt), client="client-a", operation_key="k")
    b = dispatcher.dispatch(world.request(b_run, b_receipt), client="client-b", operation_key="k")
    assert a.execution_id != b.execution_id and a.reservations == b.reservations == 1
    clash = dispatcher.dispatch(
        world.request(b_run, b_receipt), client="client-a", operation_key="k"
    )
    assert clash.reason == "fit_operation_key_conflict"


def test_reservation_record_binds_model_benchmark_backend_and_limits(tmp_path: Path) -> None:
    world = World(tmp_path)
    reserved_before_submit: list[int] = []

    def check(intent: mc.FitIntent) -> None:
        entry = world.ledger.get(intent.idempotency_key)
        assert entry is not None
        reserved_before_submit.append(entry.execution.charged_minor_units)
        assert entry.execution.stage is fd.Stage.SUBMITTING

    world.backend.on_submit = check
    _, status = started(world)
    assert status.execution_id is not None and reserved_before_submit == [0]
    record = world.ledger.get(status.execution_id)
    assert record is not None
    r = record.record
    assert r["model"]["model_ref"] == PROFILE and r["model"]["route"] == "local_open_weight"
    assert r["model"]["account_or_host"] == "generated-worker-host"
    assert r["benchmark_revision"] == "generated-benchmark-1" and r["license_ref"]
    assert r["payer"] == "generated-payer" and r["owner_id"] == OWNER
    assert r["bound"] == {
        "currency": "USD",
        "maximum_minor_units": 0,
        "maximum_seconds": 300,
        "maximum_rows": 24,
        "maximum_steps": 100,
    }
    assert r["limits"] == {
        "currency": "USD",
        "max_minor_units": 0,
        "max_fits": 2,
        "max_total_seconds": 900,
        "max_seconds_per_fit": 300,
        "max_rows_per_fit": 40,
        "max_concurrent_fits": 1,
        "compute_unit": "bound_seconds_on_named_route",
        "period": "mandate_lineage",
    }
    assert r["recipe"]["patience"] == 2 and r["recipe"]["evaluation_every_steps"] == 10
    assert r["recipe_revision"] == PLATEAU.revision and r["stop_plan"] == "early_stopping"
    assert r["intent_key"] == fd.intent_key(
        ORG, r["mandate_revision"], r["snapshot_ref"], r["plan_revision"], r["model_digest"]
    )
    assert status.execution_id == fd.execution_id_for(r["intent_key"])
    assert fd.fit_intent(r).idempotency_key == status.execution_id


# --- Lost acknowledgement, uncertain submission and restart ----------------------------------


def test_lost_ack_with_no_visible_job_holds_until_the_backend_confirms(tmp_path: Path) -> None:
    world = World(
        tmp_path,
        backend=WithoutAbsence,
        config=BackendConfig(lose_ack_on_submit=1, hidden_lookups=3),
    )
    dispatcher, status = started(world)
    assert (status.learning_run_state, status.reason, status.hold_class) == (
        "held",
        "submission_unconfirmed",
        "submission",
    )
    assert status.execution_id is not None
    for _ in range(2):
        again = dispatcher.advance(status.execution_id, actor=DELEGATE)
        assert again.stage == "submission_uncertain"
    assert world.backend.calls["submit"] == 1  # never resent while uncertain
    adopted = dispatcher.advance(status.execution_id, actor=DELEGATE)  # the job becomes visible
    assert adopted.learning_run_state == "running" and adopted.job_ref
    assert world.finish(dispatcher, status.execution_id).learning_run_state == "review_ready"
    assert world.backend.jobs_created() == 1 and world.backend.calls["submit"] == 1


def test_uncertain_submission_without_absence_confirmation_stays_held(tmp_path: Path) -> None:
    world = World(tmp_path, backend=WithoutAbsence, config=BackendConfig(fail_before_accept=1))
    dispatcher, status = started(world)
    assert status.execution_id is not None and status.stage == "submission_uncertain"
    for _ in range(5):
        assert (
            dispatcher.advance(status.execution_id, actor=DELEGATE).stage == "submission_uncertain"
        )
    assert world.backend.calls["submit"] == 1 and world.backend.jobs_created() == 0
    assert world.ledger.settlements() == 0


def test_confirmed_absence_resubmits_once_under_the_same_reservation(tmp_path: Path) -> None:
    world = World(tmp_path, config=BackendConfig(fail_before_accept=1))
    dispatcher, status = started(world)
    assert status.execution_id is not None and status.stage == "submission_uncertain"
    resumed = dispatcher.advance(status.execution_id, actor=DELEGATE)
    assert resumed.learning_run_state == "running" and resumed.submissions == 2
    assert world.backend.calls["submit"] == 2 and world.backend.jobs_created() == 1
    assert world.ledger.count("entries") == 1
    assert world.finish(dispatcher, status.execution_id).learning_run_state == "review_ready"


def test_worker_crash_after_acceptance_restart_is_exactly_one_fit(tmp_path: Path) -> None:
    world = World(tmp_path, config=BackendConfig(crash_after_accept=1))
    run, receipt = world.admit()
    with pytest.raises(WorkerCrash):
        world.dispatcher("worker-a").dispatch(
            world.request(run, receipt), client="generated-client", operation_key="op-1"
        )
    assert world.backend.jobs_created() == 1 and world.ledger.stages() == ["submitting"]
    restarted = world.dispatcher("worker-b")
    entry_id = _only_execution(world)
    waiting = restarted.advance(entry_id, actor=DELEGATE)
    assert (waiting.reason, waiting.stage) == ("submission_in_progress", "submitting")
    assert world.backend.calls.get("lookup", 0) == 0  # the live lease fences other workers
    world.clock.advance(fd.LEASE_SECONDS + 1)
    resumed = restarted.advance(entry_id, actor=DELEGATE)
    assert resumed.learning_run_state == "running"
    final = world.finish(restarted, entry_id)
    assert (final.learning_run_state, final.submissions) == ("review_ready", 1)
    assert world.backend.jobs_created() == 1 and world.backend.calls["submit"] == 1
    assert world.ledger.settlements() == 1


def _only_execution(world: World) -> str:
    with world.ledger._db() as db:
        rows = db.execute("SELECT execution_id FROM entries").fetchall()
    assert len(rows) == 1
    return str(rows[0][0])


def test_a_restarted_worker_honours_its_own_unexpired_lease(tmp_path: Path) -> None:
    # A restart cannot be told apart from a slow submission under the same identity.
    world = World(tmp_path, config=BackendConfig(crash_after_accept=1))
    run, receipt = world.admit()
    with pytest.raises(WorkerCrash):
        world.dispatcher("worker-a").dispatch(
            world.request(run, receipt), client="generated-client", operation_key="op-1"
        )
    restarted = world.dispatcher("worker-a")
    entry_id = _only_execution(world)
    assert restarted.advance(entry_id, actor=DELEGATE).reason == "submission_in_progress"
    assert world.backend.calls.get("lookup", 0) == 0
    world.clock.advance(fd.LEASE_SECONDS + 1)
    assert restarted.advance(entry_id, actor=DELEGATE).learning_run_state == "running"
    assert world.backend.calls["submit"] == 1


def test_crash_before_submission_and_before_settlement_recover_once(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()

    def die_on_submitting(method: str, ex: fd.Execution) -> None:
        if ex.stage is fd.Stage.SUBMITTING:
            world.ledger.before_commit = None
            raise WorkerCrash("simulated death before the submission claim commits")

    world.ledger.before_commit = die_on_submitting
    with pytest.raises(WorkerCrash):
        world.dispatcher().dispatch(
            world.request(run, receipt), client="generated-client", operation_key="op-1"
        )
    assert world.ledger.stages() == ["reserved"] and world.backend.calls.get("submit", 0) == 0
    entry_id = _only_execution(world)
    dispatcher = world.dispatcher()
    assert dispatcher.advance(entry_id, actor=DELEGATE).learning_run_state == "running"

    def die_on_settle(method: str, ex: fd.Execution) -> None:
        if method == "settle":
            world.ledger.before_commit = None
            raise WorkerCrash("simulated death before the settlement commits")

    world.ledger.before_commit = die_on_settle
    with pytest.raises(WorkerCrash):
        world.finish(dispatcher, entry_id)
    assert world.ledger.settlements() == 0
    final = world.finish(world.dispatcher("worker-b"), entry_id)
    assert final.learning_run_state == "review_ready"
    assert world.ledger.settlements() == 1 and world.backend.jobs_created() == 1
    assert world.backend.calls["submit"] == 1


# --- Stopping under the immutable recipe ------------------------------------------------------


def test_plateau_stops_early_and_restores_the_best_validation_checkpoint(tmp_path: Path) -> None:
    world = World(tmp_path, config=BackendConfig(metrics=(0.50, 0.60, 0.605, 0.60, 0.70)))
    dispatcher, status = started(world)
    assert status.execution_id is not None
    final = world.finish(dispatcher, status.execution_id)
    assert (final.learning_run_state, final.reason) == ("review_ready", "plateau")
    assert final.selection_method == "best_validation_checkpoint"
    assert final.selected_checkpoint == "simulated:ckpt-20"
    assert final.candidate_ref == "simulated:artifact-ckpt-20"
    assert final.usage is not None and final.usage["steps"] == 40  # stopped, not run to 100
    assert world.backend.calls["stop_early"] == 1 and world.backend.calls.get("cancel", 0) == 0
    assert (final.handoff_authority, final.publication) == ("none", "independent_review_required")


def test_plateau_without_checkpoint_recovery_records_the_stop_checkpoint(tmp_path: Path) -> None:
    config = BackendConfig(metrics=(0.50, 0.60, 0.605, 0.60), checkpoint_recovery=False)
    world = World(tmp_path, config=config)
    dispatcher, status = started(world)
    assert status.execution_id is not None
    final = world.finish(dispatcher, status.execution_id)
    assert (final.reason, final.selection_method) == ("plateau", "checkpoint_at_stop")
    assert final.selected_checkpoint is None and final.candidate_ref == "simulated:artifact-ckpt-40"


def test_an_early_stop_the_backend_did_not_apply_is_recorded_as_a_completion(
    tmp_path: Path,
) -> None:
    # The backend reaches its last step in the same interval that shows the plateau.
    world = World(tmp_path, config=BackendConfig(metrics=(0.5, 0.6, 0.6, 0.6), run_steps=40))
    dispatcher, status = started(world)
    assert status.execution_id is not None
    final = world.finish(dispatcher, status.execution_id)
    assert (final.learning_run_state, final.reason, final.selection_method) == (
        "review_ready",
        "recipe_complete",
        "final_step",
    )
    assert final.selected_checkpoint is None and final.candidate_ref == "simulated:artifact-ckpt-40"
    assert world.backend.calls["stop_early"] == 1


def test_an_improving_run_completes_at_the_recipe_step_limit(tmp_path: Path) -> None:
    world = World(tmp_path, config=BackendConfig(metrics=tuple(0.5 + 0.02 * i for i in range(10))))
    dispatcher, status = started(world)
    assert status.execution_id is not None
    final = world.finish(dispatcher, status.execution_id)
    assert (final.learning_run_state, final.reason, final.selection_method) == (
        "review_ready",
        "recipe_complete",
        "final_step",
    )
    assert final.usage is not None and final.usage["steps"] == 100
    assert world.backend.calls.get("stop_early", 0) == 0


def test_unsupported_early_stop_runs_a_preapproved_fixed_length_recipe(tmp_path: Path) -> None:
    world = World(
        tmp_path, backend=WithoutEarlyStop, config=BackendConfig(metrics=(0.5, 0.6, 0.6, 0.6, 0.6))
    )
    dispatcher, status = started(world, recipe=replace(PLATEAU, fixed_length_fallback=True))
    # The fallback is a different recipe revision than the receipt's; the frozen fields match.
    assert status.execution_id is not None, status
    final = world.finish(dispatcher, status.execution_id)
    assert (final.learning_run_state, final.selection_method) == (
        "review_ready",
        "fixed_length_fallback",
    )
    assert final.usage is not None and final.usage["steps"] == 100


def test_a_failing_early_stop_callback_stops_with_no_candidate(tmp_path: Path) -> None:
    world = World(
        tmp_path, backend=FailingEarlyStop, config=BackendConfig(metrics=(0.5, 0.6, 0.6, 0.6))
    )
    dispatcher, status = started(world)
    assert status.execution_id is not None
    final = world.finish(dispatcher, status.execution_id)
    assert (final.learning_run_state, final.reason, final.candidate_ref) == (
        "canceled",
        "early_stop_unsupported",
        None,
    )
    assert world.backend.calls["stop_early"] == 1 and world.backend.calls["cancel"] == 1
    assert world.backend.calls.get("collect", 0) == 0


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (BackendConfig(metrics=(0.5, nonfinite())), "nonfinite_metric"),
        (BackendConfig(step_offset=5), "invalid_progress"),
        (
            BackendConfig(run_steps=200, metrics=tuple(0.5 + 0.02 * i for i in range(20))),
            "step_limit_exceeded",
        ),
        (
            BackendConfig(seconds_per_step=5, metrics=tuple(0.5 + 0.02 * i for i in range(20))),
            "time_limit_exceeded",
        ),
    ],
)
def test_nonfinite_invalid_or_over_limit_progress_stops_with_no_candidate(
    tmp_path: Path, config: BackendConfig, reason: str
) -> None:
    recipe = FIXED if reason in ("step_limit_exceeded", "time_limit_exceeded") else PLATEAU
    world = World(tmp_path, config=config)
    run, receipt = world.admit(recipe=recipe)
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt, recipe=recipe), client="generated-client", operation_key="op"
    )
    assert status.execution_id is not None
    final = world.finish(dispatcher, status.execution_id)
    if reason in ("step_limit_exceeded", "time_limit_exceeded"):
        # The backend went past a reserved dimension: OPE-508 holds it, nothing settles.
        assert (final.learning_run_state, final.reason) == ("held", "over_cap")
        assert final.usage is not None
    else:
        assert (final.learning_run_state, final.reason, final.candidate_ref) == (
            "canceled",
            reason,
            None,
        )
        if reason == "nonfinite_metric":
            # The last valid checkpoint is sealed as partial output; it is never publishable.
            assert final.sealed_partial_ref == "simulated:ckpt-10"
    assert world.backend.calls.get("collect", 0) == 0 and world.backend.calls["cancel"] == 1


def test_invalid_output_is_never_published(tmp_path: Path) -> None:
    world = World(tmp_path, config=BackendConfig(invalid_output=True))
    run, receipt = world.admit(recipe=FIXED)
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt, recipe=FIXED), client="generated-client", operation_key="op-1"
    )
    assert status.execution_id is not None
    final = world.finish(dispatcher, status.execution_id)
    assert (final.learning_run_state, final.reason, final.candidate_ref) == (
        "completed",
        "invalid_output",
        None,
    )


# --- Revocation during dispatch ----------------------------------------------------------------


def test_revocation_during_fit_cancels_and_never_publishes_or_exports(tmp_path: Path) -> None:
    world = World(tmp_path, config=BackendConfig(metrics=tuple(0.5 + 0.02 * i for i in range(10))))
    dispatcher, status = started(world)
    assert status.execution_id is not None
    assert dispatcher.advance(status.execution_id, actor=DELEGATE).learning_run_state == "running"
    world.mandates.withdraw(MANDATE, actor=OWNER)
    final = world.finish(dispatcher, status.execution_id)
    assert (final.learning_run_state, final.reason) == ("canceled", "standing_mandate_withdrawn")
    assert final.candidate_ref is None and final.handoff_authority == "none"
    assert final.usage is not None and 0 < final.usage["steps"] < 100  # actual partial spend
    assert world.backend.calls["cancel"] == 1 and world.backend.calls.get("collect", 0) == 0
    assert world.ledger.settlements() == 1
    # Nothing afterwards can publish: repeated steps change nothing, a replay is refused.
    events = world.ledger.count("events")
    for _ in range(3):
        assert dispatcher.advance(status.execution_id, actor=DELEGATE).candidate_ref is None
    assert world.ledger.count("events") == events and world.backend.calls.get("collect", 0) == 0
    run = final.lineage["run_id"] if final.lineage else ""
    replay = dispatcher.dispatch(
        world.request(run, world.router.read(final.lineage["evaluation_id"]) or {}),  # type: ignore[index]
        client="generated-client",
        operation_key="op-1",
    )
    assert (replay.reason, replay.reservations) == ("standing_mandate_withdrawn", 0)


@pytest.mark.parametrize("kind", ["evidence", "grant"])
def test_invalidated_evidence_or_revoked_grant_stops_an_in_flight_fit(
    tmp_path: Path, kind: str
) -> None:
    world = World(tmp_path)
    dispatcher, status = started(world)
    assert status.execution_id is not None and status.lineage is not None
    run = status.lineage["run_id"]
    if kind == "evidence":
        world.snapshots[run] = replace(
            world.snapshots[run], evidence_revision="generated-evidence-2"
        )
        expected = "evidence_revision_changed"
    else:
        world.grants.revoked = True
        expected = "training_grant_unavailable"
    final = world.finish(dispatcher, status.execution_id)
    assert (final.learning_run_state, final.reason, final.candidate_ref) == (
        "canceled",
        expected,
        None,
    )
    assert world.backend.calls.get("collect", 0) == 0


def test_revocation_between_reservation_and_submission_sends_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()

    def withdraw_then_rows(record: Mapping[str, Any]) -> tuple[Mapping[str, object], ...]:
        world.mandates.withdraw(MANDATE, actor=OWNER)
        return ROWS

    world.rows = withdraw_then_rows
    status = world.dispatcher().dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert (status.learning_run_state, status.reason) == ("refused", "standing_mandate_withdrawn")
    assert status.reservations == 1 and status.usage is not None and status.usage["seconds"] == 0
    assert world.backend.calls.get("submit", 0) == 0 and world.backend.jobs_created() == 0


# --- Money and over-cap holds (OPE-508, OPE-570) ---------------------------------------------


def _usage(job: SimJob, **kw: Any) -> Usage:
    base = {
        "job_ref": job.job_ref,
        "currency": "USD",
        "incurred_minor_units": 0,
        "seconds": 200,
        "rows": 24,
        "steps": 100,
        "source": UsageSource.MEASURED,
    }
    return Usage(**(base | kw))


@pytest.mark.parametrize(
    ("final", "reason"),
    [
        (lambda job: _usage(job, seconds=400), "over_cap"),
        (lambda job: None, "usage_unverifiable"),
        (lambda job: _usage(job, source=UsageSource.ESTIMATED), "usage_unverifiable"),
        (lambda job: _usage(job, currency="EUR"), "cap_unverifiable"),
        (lambda job: _usage(job, job_ref="simulated:other-job"), "usage_unverifiable"),
    ],
)
def test_over_cap_or_unverifiable_usage_is_held_and_never_collected(
    tmp_path: Path, final: Callable[[SimJob], Usage | None], reason: str
) -> None:
    world = World(tmp_path, config=BackendConfig(final_usage=final))
    run, receipt = world.admit(recipe=FIXED)
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt, recipe=FIXED), client="generated-client", operation_key="op"
    )
    assert status.execution_id is not None
    held = world.finish(dispatcher, status.execution_id)
    assert (held.learning_run_state, held.stage, held.reason, held.hold_class) == (
        "held",
        "held",
        reason,
        "usage",
    )
    assert held.next_action == "operator_incident_review_required"
    assert held.candidate_ref is None and world.backend.calls.get("collect", 0) == 0
    assert world.ledger.settlements() == 0
    if reason == "over_cap":
        assert held.usage is not None and held.usage["seconds"] == 400  # real figure, unclamped
    entry = world.ledger.get(status.execution_id)
    assert entry is not None
    with pytest.raises(fd.DispatchError, match="ledger_transition_refused"):
        world.ledger.settle(status.execution_id, version=entry.execution.version, settlement={})
    with pytest.raises(fd.DispatchError, match="ledger_transition_refused"):
        world.ledger.transition(
            status.execution_id,
            version=entry.execution.version,
            execution=replace(entry.execution, stage=fd.Stage.RUNNING),
            event="reopen",
        )


def test_paid_over_cap_is_charged_at_the_real_figure_and_blocks_the_next_fit(
    tmp_path: Path,
) -> None:
    config = BackendConfig(
        minor_units_per_step=8,
        metrics=tuple(0.5 + 0.02 * i for i in range(10)),
        final_usage=lambda job: _usage(job, incurred_minor_units=800, steps=job.steps),
    )
    world = World(
        tmp_path, config=config, fit_bound=PAID, max_cost_cents=1_000, max_concurrent_fits=2
    )
    run, receipt = world.admit(recipe=FIXED)
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt, recipe=FIXED, bound=PAID, binding=MANAGED),
        client="generated-client",
        operation_key="op-1",
    )
    assert status.execution_id is not None and status.charged is not None
    assert status.charged["minor_units"] == 500  # the upper bound, reserved before submission
    held = world.finish(dispatcher, status.execution_id)
    assert (held.reason, held.charged) == (
        "over_cap",
        {
            "currency": "USD",
            "minor_units": 800,
            "seconds": 300,
            "fits": 1,
            "period": "mandate_lineage",
        },
    )
    run2, receipt2 = world.admit(2, recipe=FIXED)
    second = dispatcher.dispatch(
        world.request(run2, receipt2, recipe=FIXED, bound=PAID, binding=MANAGED),
        client="generated-client",
        operation_key="op-2",
    )
    assert (second.reason, second.reservations) == ("money_budget_exhausted", 0)


def test_live_usage_above_the_bound_cancels_and_holds(tmp_path: Path) -> None:
    config = BackendConfig(
        minor_units_per_step=10, live_usage=True, metrics=tuple(0.5 + 0.02 * i for i in range(10))
    )
    world = World(tmp_path, config=config, fit_bound=PAID, max_cost_cents=1_000)
    run, receipt = world.admit(recipe=FIXED)
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt, recipe=FIXED, bound=PAID, binding=MANAGED),
        client="generated-client",
        operation_key="op-1",
    )
    assert status.execution_id is not None
    held = world.finish(dispatcher, status.execution_id)
    assert (held.learning_run_state, held.reason) == ("held", "over_cap")
    assert held.usage is not None and held.usage["incurred_minor_units"] > 500
    assert world.backend.calls["cancel"] == 1 and world.backend.calls.get("collect", 0) == 0


def test_a_connector_hold_reason_wins_over_usage_that_looks_within_bounds(tmp_path: Path) -> None:
    world = World(tmp_path, backend=OwnHold)
    run, receipt = world.admit(recipe=FIXED)
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt, recipe=FIXED), client="generated-client", operation_key="op"
    )
    assert status.execution_id is not None
    held = world.finish(dispatcher, status.execution_id)
    assert (held.stage, held.reason) == ("held", "over_cap")
    assert world.backend.calls.get("collect", 0) == 0


# --- Cancellation and read-only status ---------------------------------------------------------


def test_cancel_before_submission_releases_and_cancel_while_uncertain_is_pending(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()
    world.rows = lambda record: (_ for _ in ()).throw(LookupError("sealed manifest busy"))
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert (status.stage, status.learning_run_state, status.reason) == (
        "reserved",
        "held",
        "development_payload_unavailable",
    )
    assert status.execution_id is not None
    canceled = dispatcher.cancel(status.execution_id, actor=DELEGATE)
    assert (canceled.learning_run_state, canceled.reason) == (
        "canceled",
        "canceled_before_submission",
    )
    assert world.backend.calls.get("submit", 0) == 0

    other = World(
        tmp_path / "second",
        backend=WithoutAbsence,
        config=BackendConfig(lose_ack_on_submit=1, hidden_lookups=2),
    )
    d2, uncertain = started(other)
    assert uncertain.execution_id is not None and uncertain.stage == "submission_uncertain"
    pending = d2.cancel(uncertain.execution_id, actor=DELEGATE)
    assert (pending.learning_run_state, pending.cancel_pending) == ("held", True)
    still = d2.advance(uncertain.execution_id, actor=DELEGATE)  # the job is still invisible
    assert (still.stage, still.cancel_pending) == ("submission_uncertain", True)
    final = still
    for _ in range(5):  # the backend confirms the job; the pending cancel then stops it
        final = d2.advance(uncertain.execution_id, actor=DELEGATE)
        if final.stage == "settled":
            break
    assert (final.learning_run_state, final.reason) == ("canceled", "canceled_by_owner")
    assert other.backend.calls["cancel"] == 1 and other.backend.jobs_created() == 1


def test_development_payload_retry_is_bounded(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()
    attempts: list[int] = []

    def flaky(record: Mapping[str, Any]) -> tuple[Mapping[str, object], ...]:
        attempts.append(1)
        if len(attempts) == 1:
            raise LookupError("sealed manifest busy")
        return ROWS

    world.rows = flaky
    dispatcher = world.dispatcher()
    status = dispatcher.dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert status.execution_id is not None and status.stage == "reserved"
    retried = dispatcher.advance(status.execution_id, actor=DELEGATE)
    assert (retried.learning_run_state, retried.submissions) == ("running", 2)


def test_status_reads_have_no_effects(tmp_path: Path) -> None:
    world = World(tmp_path)
    dispatcher, status = started(world)
    assert status.execution_id is not None
    events, calls = world.ledger.count("events"), dict(world.backend.calls)
    for _ in range(5):
        assert dispatcher.status(status.execution_id, actor=DELEGATE).stage == "running"
    assert world.ledger.count("events") == events and world.backend.calls == calls
    foreign = dispatcher.status(status.execution_id, actor="generated-intruder")
    assert (foreign.reason, foreign.execution_id) == ("execution_unavailable", None)


def test_an_unavailable_collaborator_holds_with_nothing_reserved(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()

    def grants_down(*args: Any) -> mc.Grant | None:
        raise OSError("simulated owner grant store unavailable")

    world.preflight.current_grant = grants_down
    status = world.dispatcher().dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert (status.learning_run_state, status.reason, status.reservations) == (
        "held",
        "fit_dispatch_unavailable",
        0,
    )
    (world.dir / "routing" / "repair-routing.sqlite3").chmod(0o644)  # unsafe custody
    unreadable = world.dispatcher().dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-2"
    )
    assert (unreadable.reason, unreadable.reservations) == ("evaluation_unavailable", 0)
    assert world.ledger.count("entries") == 0 and world.backend.calls.get("submit", 0) == 0


def test_a_ledger_failure_never_submits(tmp_path: Path) -> None:
    world = World(tmp_path)
    run, receipt = world.admit()

    def ledger_down(method: str, ex: fd.Execution) -> None:
        raise OSError("simulated disk full")

    world.ledger.before_commit = ledger_down
    status = world.dispatcher().dispatch(
        world.request(run, receipt), client="generated-client", operation_key="op-1"
    )
    assert (status.learning_run_state, status.reason, status.stage) == (
        "held",
        "fit_ledger_unavailable",
        "reserved",
    )
    assert status.hold_class == "ledger" and world.backend.calls.get("submit", 0) == 0


def test_a_one_fit_mandate_is_one_logical_job_and_settlement_across_retries(
    tmp_path: Path,
) -> None:
    world = World(
        tmp_path,
        mandate=spec(max_fits=1),
        config=BackendConfig(lose_ack_on_submit=1, hidden_lookups=1),
    )
    run, receipt = world.admit()
    request = world.request(run, receipt)
    first = world.dispatcher("worker-a").dispatch(request, client="slack", operation_key="a")
    assert first.stage == "submission_uncertain" and first.execution_id is not None
    for worker, key in (("worker-b", "b"), ("worker-c", "c"), ("worker-a", "a")):
        again = world.dispatcher(worker).dispatch(request, client="scheduler", operation_key=key)
        assert again.execution_id == first.execution_id and again.replayed
    final = world.finish(world.dispatcher("worker-d"), first.execution_id)
    assert final.learning_run_state == "review_ready"
    assert world.ledger.count("entries") == 1 and world.ledger.settlements() == 1
    assert world.backend.jobs_created() == 1 and world.backend.calls["submit"] == 1
    run2, receipt2 = world.admit(2)
    more = world.dispatcher().dispatch(
        world.request(run2, receipt2), client="scheduler", operation_key="d"
    )
    assert (more.reason, more.hold_class, more.reservations) == (
        "training_budget_required",
        "budget",
        0,
    )


def test_a_managed_route_receives_only_the_authorized_development_payload(tmp_path: Path) -> None:
    world = World(tmp_path, fit_bound=PAID, max_cost_cents=1_000)
    world.grants.disclosure_fields = frozenset({"other_field"})
    run, receipt = world.admit(recipe=FIXED)
    status = world.dispatcher().dispatch(
        world.request(run, receipt, recipe=FIXED, bound=PAID, binding=MANAGED),
        client="generated-client",
        operation_key="op-1",
    )
    assert (status.reason, status.hold_class, status.reservations) == (
        "training_disclosure_scope_unavailable",
        "setup",
        0,
    )
    world.grants.disclosure_fields = frozenset({"question"})
    world.rows = lambda record: tuple(dict(r, secret="generated-sentinel") for r in ROWS)
    sent = world.dispatcher().dispatch(
        world.request(run, receipt, recipe=FIXED, bound=PAID, binding=MANAGED),
        client="generated-client",
        operation_key="op-2",
    )
    # Rows carrying a field outside the authorized projection never reach the backend.
    assert (sent.stage, sent.learning_run_state, sent.reason) == (
        "reserved",
        "held",
        "training_data_scope_unavailable",
    )
    assert (
        world.backend.calls.get("submit", 0) == 0 and world.backend.calls.get("serialize", 0) == 0
    )


# --- The mandate authority helper -----------------------------------------------------------


def test_dispatch_authority_reports_one_lineage_budget_and_refuses_lapsed_authority(
    tmp_path: Path,
) -> None:
    world = World(tmp_path)
    run, _ = world.admit()
    first = world.mandates.dispatch_authority(MANDATE, run, actor=DELEGATE)
    assert first["open_reserved_fits"] == 0 and first["cumulative_reserved_fits"] == 0
    assert first["caps"]["max_cost_cents"] == 0 and first["budget_scope"].startswith("budget-")
    with pytest.raises(lm.MandateError, match="standing_delegate_mismatch"):
        world.mandates.dispatch_authority(MANDATE, run, actor="generated-intruder")
    world.mandates.withdraw(MANDATE, actor=OWNER)
    with pytest.raises(lm.MandateError, match="standing_mandate_withdrawn"):
        world.mandates.dispatch_authority(MANDATE, run, actor=DELEGATE)
    world.mandates.reauthorize(MANDATE, spec(mandate_id="standing-two"), actor=OWNER)
    run2 = world.mandates.authorize_source(
        "standing-two",
        {
            "source_revision": digest(["source", 7]),
            "trace_sha256": digest(["traces", 7]),
            "correction_sha256": digest(["outcomes", 7]),
            "workflow": "support",
            "connector": "approved-mcp",
            "trace_source": "tickets",
            "correction_source": "outcomes",
            "field": "route",
            "fields": ["question"],
            "rows": 24,
        },
        actor=DELEGATE,
    )["run_id"]
    renewed = world.mandates.dispatch_authority("standing-two", run2, actor=DELEGATE)
    assert renewed["budget_scope"] == first["budget_scope"]  # one budget across the renewal
    with pytest.raises(lm.MandateError, match="delegated_run_unavailable"):
        world.mandates.dispatch_authority("standing-two", run, actor=DELEGATE)


def test_stopping_decision_is_pure_and_follows_the_recipe() -> None:
    def evals(*values: float) -> tuple[fd.Evaluation, ...]:
        return tuple(fd.Evaluation(10 * (i + 1), v, f"c{i}") for i, v in enumerate(values))

    assert fd.stopping_decision(PLATEAU, evals(0.5, 0.6, 0.7)) is None
    assert fd.stopping_decision(PLATEAU, evals(0.5, 0.6, 0.605, 0.6)) == (
        "plateau",
        evals(0.5, 0.6)[1],
    )
    minimize = replace(PLATEAU, direction="minimize")
    assert fd.stopping_decision(minimize, evals(1.0, 0.8, 0.6)) is None
    assert fd.stopping_decision(minimize, evals(1.0, 1.1, 1.2)) == ("plateau", evals(1.0)[0])
    assert fd.stopping_decision(FIXED, evals(0.5, 0.5, 0.5, 0.5)) is None
    assert fd.stopping_decision(FIXED, evals(0.5, float("inf"))) == ("nonfinite_metric", None)
    for bad in (
        replace(PLATEAU, fit_count=2),
        replace(PLATEAU, min_improvement=float("nan")),
        replace(PLATEAU, evaluation_every_steps=0),
        replace(PLATEAU, stopping_rule="best_of_three"),
    ):
        with pytest.raises(fd.DispatchError, match="invalid_fit_recipe"):
            bad.validate()


def test_usage_hold_matches_the_ope508_vocabulary() -> None:
    def usage(**kw: Any) -> Usage:
        base = {
            "job_ref": "simulated:job",
            "currency": "USD",
            "incurred_minor_units": 500,
            "seconds": 300,
            "rows": 24,
            "steps": 100,
            "source": UsageSource.PROVIDER_REPORTED,
        }
        return Usage(**(base | kw))

    assert fd.usage_hold(PAID, "simulated:job", usage()) is None  # exactly at every bound
    assert fd.usage_hold(PAID, "simulated:job", usage(incurred_minor_units=501)) == "over_cap"
    assert fd.usage_hold(PAID, "simulated:job", usage(steps=101, currency="EUR")) == "over_cap"
    assert fd.usage_hold(PAID, "simulated:job", usage(currency="usd")) == "cap_unverifiable"
    assert fd.usage_hold(PAID, "simulated:job", None) == "usage_unverifiable"
    assert fd.usage_hold(PAID, "simulated:job", usage(source=UsageSource.ESTIMATED)) == (
        "usage_unverifiable"
    )
    assert fd.usage_hold(PAID, "simulated:other", usage()) == "usage_unverifiable"

    class Reported(Usage):
        cap_status = "cap_unknown"

        @property
        def settleable(self) -> bool:
            return False

    unknown = Reported("simulated:job", "USD", 1, 1, 1, 1, UsageSource.PROVIDER_REPORTED)
    assert fd.usage_hold(PAID, "simulated:job", unknown) == "cap_unverifiable"


def test_the_route_connectors_expose_what_the_dispatcher_consumes() -> None:
    """Drift pin: the OPE-507/508/509 connectors keep the names the dispatcher reads."""
    from opentour_cell.pilot import (
        custom_trainer_connector,
        managed_connector,
        open_weight_connector,
    )

    assert callable(getattr(managed_connector.ManagedConnector, "hold_reason", None))
    for connector in (
        managed_connector.ManagedConnector,
        open_weight_connector.OpenWeightConnector,
        custom_trainer_connector.CustomTrainerConnector,
    ):
        assert callable(getattr(connector, "usage", None)), connector
    assert set(fd.USAGE_HOLDS) == {
        managed_connector.HOLD_OVER_CAP,
        managed_connector.HOLD_USAGE_UNVERIFIABLE,
        managed_connector.HOLD_CAP_UNVERIFIABLE,
    }
