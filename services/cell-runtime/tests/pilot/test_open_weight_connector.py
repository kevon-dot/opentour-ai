"""Open-weight connector behavior on generated toy families (software conformance only)."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import toy_families as toy
from opentour_cell.pilot import connector_ext as ext
from opentour_cell.pilot import model_connector as mc
from opentour_cell.pilot import open_weight_connector as owc
from opentour_cell.pilot import open_weight_reference as ref
from opentour_cell.pilot.connector_conformance import ACTOR, CountingGuard, standard_grants
from opentour_cell.pilot.connector_ext import CappedUsage, Fenceable, UsageReporting, UsageSource
from opentour_cell.pilot.model_family import FamilyError, verify_model_directory

ARCHITECTURES = ["generated-toy-linear", "generated-toy-normmlp"]


@pytest.fixture(params=ARCHITECTURES)
def family(request: pytest.FixtureRequest, tmp_path: Path) -> toy.Family:
    return toy.Family(tmp_path, request.param)


def dispatch(
    family: toy.Family, connector: owc.OpenWeightConnector, key: str
) -> tuple[mc.Preflight, mc.FitIntent, mc.JobReceipt]:
    intent = family.intent(key)
    pre = mc.Preflight(connector, standard_grants(family.binding, intent))
    receipt = pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard())
    return pre, intent, receipt


def test_connector_implements_the_shared_contract_and_optional_capabilities(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    assert isinstance(connector, Fenceable)
    assert isinstance(connector, UsageReporting)
    for name in (
        "observe",
        "serialize_development",
        "estimate",
        "submit",
        "lookup",
        "status",
        "cancel",
        "collect",
        "predict_for_replay",
    ):
        assert callable(getattr(connector, name))


def test_actual_parameter_update_changes_adapter_and_never_the_pinned_base(
    family: toy.Family, tmp_path: Path
) -> None:
    before = {p.name: p.read_bytes() for p in family.model_dir.iterdir()}
    connector = family.connector(tmp_path / "state")
    pre, _, receipt = dispatch(family, connector, "generated-update")
    assert receipt.state is mc.JobState.COMPLETED
    evidence = connector.evidence(receipt)
    assert evidence["parameters_changed"] > 0
    assert evidence["loss_last"] < evidence["loss_first"]
    assert evidence["base_sha256_before"] == evidence["base_sha256_after"]
    assert {p.name: p.read_bytes() for p in family.model_dir.iterdir()} == before
    assert verify_model_directory(family.profile, family.model_dir) == family.profile.revision
    candidate = pre.collect_candidate(ACTOR, receipt)
    assert candidate.parent_revision == family.profile.revision
    tuned = connector.predict_for_replay(candidate, toy.EVAL_INPUTS)
    base = connector.predict_base(toy.EVAL_INPUTS)
    assert tuned != base  # the learned parameters actually change inference


def test_pinned_base_tamper_after_fit_blocks_replay_and_every_further_fit(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    pre, _, receipt = dispatch(family, connector, "generated-tamper")
    candidate = pre.collect_candidate(ACTOR, receipt)
    (family.model_dir / "base.json").write_text("{}")
    with pytest.raises(ValueError, match="INSTALLED_MODEL_MISMATCH"):
        connector.predict_for_replay(candidate, toy.EVAL_INPUTS)
    with pytest.raises(ValueError, match="INSTALLED_MODEL_MISMATCH"):
        connector.predict_base(toy.EVAL_INPUTS)
    with pytest.raises(mc.CapabilityError, match="exact_training_target_unavailable"):
        dispatch(family, connector, "generated-after-tamper")


def test_fresh_process_replay_is_byte_identical_across_connector_restarts(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state)
    pre, _, receipt = dispatch(family, connector, "generated-replay")
    candidate = pre.collect_candidate(ACTOR, receipt)
    first = connector.predict_for_replay(candidate, toy.EVAL_INPUTS)
    reopened = family.connector(state)
    assert reopened.predict_for_replay(candidate, toy.EVAL_INPUTS) == first
    forged = replace(candidate, artifact_or_serving_revision="0" * 64)
    with pytest.raises(ValueError, match="CANDIDATE_LINEAGE_UNVERIFIED"):
        reopened.predict_for_replay(forged, toy.EVAL_INPUTS)


def test_corrupt_or_swapped_artifact_is_refused(family: toy.Family, tmp_path: Path) -> None:
    state = tmp_path / "state"
    connector = family.connector(state)
    pre, _, receipt = dispatch(family, connector, "generated-corrupt")
    candidate = pre.collect_candidate(ACTOR, receipt)
    adapter = next((state / "jobs").glob("*/artifact/adapter.json"))
    adapter.write_text(adapter.read_text() + " ")
    with pytest.raises(ValueError, match="ARTIFACT_DIGEST_MISMATCH"):
        connector.predict_for_replay(candidate, toy.EVAL_INPUTS)
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, receipt)


def test_duplicate_dispatch_runs_one_execution_and_returns_the_same_receipt(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state)
    _, intent, first = dispatch(family, connector, "generated-duplicate")
    pre = mc.Preflight(family.connector(state), standard_grants(family.binding, intent))
    second = pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard())
    assert second == first
    assert len(list((state / "jobs").glob("*/journal.jsonl"))) == 1
    events = [
        json.loads(x)["event"]
        for x in next((state / "jobs").glob("*/journal.jsonl")).read_text().splitlines()
    ]
    assert events == ["accepted", "started", "completed"]


def test_concurrent_duplicate_dispatch_still_runs_once(family: toy.Family, tmp_path: Path) -> None:
    state = tmp_path / "state"
    intent = family.intent("generated-concurrent")
    receipts: list[mc.JobReceipt] = []

    def submit() -> None:
        connector = family.connector(state)
        pre = mc.Preflight(connector, standard_grants(family.binding, intent))
        receipts.append(
            pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard())
        )

    threads = [threading.Thread(target=submit) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert len(receipts) == 3 and len({r.job_ref for r in receipts}) == 1
    assert len(list((state / "jobs").glob("*/journal.jsonl"))) == 1
    journal = next((state / "jobs").glob("*/journal.jsonl")).read_text().splitlines()
    assert [json.loads(x)["event"] for x in journal].count("started") == 1


def test_idempotency_key_cannot_be_reused_for_a_different_request(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    _, intent, _ = dispatch(family, connector, "generated-key")
    other = replace(intent, benchmark_revision="generated-other-benchmark")
    pre = mc.Preflight(connector, standard_grants(family.binding, other))
    with pytest.raises(owc.ConnectorRefusal, match="IDEMPOTENCY_CONFLICT"):
        pre.submit_reserved_fit(ACTOR, other, toy.TRAIN, reservation_guard=CountingGuard())


def test_interrupted_job_after_a_crash_is_failed_and_never_silently_re_run(
    family: toy.Family, tmp_path: Path
) -> None:
    """Uncertain job: the process died mid-fit. Reconcile first; a retry must not re-execute."""
    state = tmp_path / "state"
    connector = family.connector(state)

    class Crash(BaseException):
        pass

    def crash(*_args: Any, **_kwargs: Any) -> owc.FitOutput:
        raise Crash

    connector.runner.fit = crash  # type: ignore[method-assign]
    intent = family.intent("generated-crash")
    pre = mc.Preflight(connector, standard_grants(family.binding, intent))
    with pytest.raises(Crash):
        pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard())
    restarted = family.connector(state)
    pre = mc.Preflight(restarted, standard_grants(family.binding, intent))
    reconciled = pre.reconcile_fit(ACTOR, intent)
    assert reconciled.state is mc.JobState.FAILED
    # Corrected by the OPE-507 hardening: a FAILED job's usage is unknown, never a settleable zero.
    assert restarted.usage(reconciled) is None
    retry = pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard())
    assert retry == reconciled
    assert len(list((state / "jobs").glob("*/journal.jsonl"))) == 1
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, retry)


def test_lost_poll_status_is_a_read_only_recovery(family: toy.Family, tmp_path: Path) -> None:
    state = tmp_path / "state"
    connector = family.connector(state)
    pre, intent, receipt = dispatch(family, connector, "generated-poll")
    for _ in range(3):  # repeated or lost polls never change anything
        assert connector.status(receipt) == receipt
        assert family.connector(state).lookup(intent) == receipt
    assert family.connector(state).lookup(family.intent("generated-never-submitted")) is None
    with pytest.raises(ValueError, match="JOB_REFERENCE_UNKNOWN"):
        connector.status(replace(receipt, job_ref="simulated:other"))


def test_cap_exceeded_is_refused_before_a_reservation_guard_or_process(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state)
    base = family.intent("generated-cap")
    for field, value in (
        ("maximum_steps", 1),
        ("maximum_rows", 2),
        ("maximum_seconds", 1),
    ):
        intent = replace(base, bound=replace(base.bound, **{field: value}))
        pre = mc.Preflight(connector, standard_grants(family.binding, intent))
        guard = CountingGuard()
        with pytest.raises(mc.CapabilityError, match="known_training_cost_within_budget_required"):
            pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=guard)
        assert guard.entries == 0
    assert not list((state / "jobs").iterdir())


def test_connector_itself_refuses_a_bound_below_its_estimate_without_preflight(
    family: toy.Family, tmp_path: Path
) -> None:
    """Defense in depth: a caller that bypasses Preflight still cannot exceed the bound."""
    connector = family.connector(tmp_path / "state")
    intent = family.intent("generated-bypass")
    tight = replace(intent, bound=replace(intent.bound, maximum_steps=3))
    payload = connector.serialize_development(tight, toy.TRAIN)
    tight = replace(tight, development_digest=owc._sha(payload))
    with pytest.raises(owc.ConnectorRefusal, match="BOUND_EXCEEDED_BEFORE_DISPATCH"):
        connector.submit(tight, payload)
    assert not list((tmp_path / "state" / "jobs").iterdir())


def test_wall_clock_deadline_stops_the_worker_and_publishes_no_candidate(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    ticks = iter(range(0, 10_000, 100))
    connector = family.connector(state, slow=True)
    connector.clock = lambda: float(next(ticks))
    intent = family.intent("generated-deadline")
    payload = connector.serialize_development(intent, toy.TRAIN)
    receipt = connector.submit(intent, payload)
    assert receipt.state is mc.JobState.FAILED
    journal = next((state / "jobs").glob("*/journal.jsonl")).read_text()
    assert "DEADLINE_EXCEEDED" in journal
    assert not list((state / "jobs").glob("*/artifact"))
    # Corrected by the OPE-507 hardening: the old assertion required a MEASURED (settleable) zero
    # for a FAILED job, which contradicts the UsageReporting contract ("None means unknown").
    assert connector.usage(receipt) is None


def test_cancel_during_a_live_fit_publishes_nothing_and_blocks_collection(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state, slow=True)
    intent = family.intent("generated-cancel")
    pre = mc.Preflight(connector, standard_grants(family.binding, intent))
    outcome: list[mc.JobReceipt] = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard())
        )
    )
    thread.start()
    live = None
    deadline = time.monotonic() + 10
    while live is None and time.monotonic() < deadline:
        live = connector.lookup(intent)
        time.sleep(0.02)
    assert live is not None and live.state in (mc.JobState.QUEUED, mc.JobState.RUNNING)
    canceled = connector.cancel(live)
    thread.join(timeout=30)
    assert canceled.state is mc.JobState.CANCELED
    assert outcome and outcome[0].state is mc.JobState.CANCELED
    assert not list((state / "jobs").glob("*/artifact"))
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, canceled)
    # Cancel is idempotent and survives restart.
    assert family.connector(state).cancel(canceled).state is mc.JobState.CANCELED


def test_cancel_after_completion_cannot_unpublish_or_fake_a_cancel(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    pre, _, receipt = dispatch(family, connector, "generated-late-cancel")
    assert connector.cancel(receipt).state is mc.JobState.COMPLETED
    assert pre.collect_candidate(ACTOR, receipt).job_ref == receipt.job_ref


def test_fence_survives_restart_blocks_new_collect_and_replay_and_cancels_live_work(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state, slow=True)
    intent = family.intent("generated-fenced-live")
    pre = mc.Preflight(connector, standard_grants(family.binding, intent))
    outcome: list[mc.JobReceipt] = []
    thread = threading.Thread(
        target=lambda: outcome.append(
            pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard())
        )
    )
    thread.start()
    deadline = time.monotonic() + 10
    while connector.lookup(intent) is None and time.monotonic() < deadline:
        time.sleep(0.02)
    receipt = connector.fence("generated-withdrawal")
    thread.join(timeout=30)
    assert len(receipt.canceled_job_refs) == 1
    assert outcome[0].state is mc.JobState.CANCELED
    reopened = family.connector(state)
    later = family.intent("generated-after-fence")
    later_pre = mc.Preflight(reopened, standard_grants(family.binding, later))
    with pytest.raises(owc.ConnectorRefusal, match="CONNECTOR_FENCED"):
        later_pre.submit_reserved_fit(ACTOR, later, toy.TRAIN, reservation_guard=CountingGuard())
    with pytest.raises(owc.ConnectorRefusal, match="CONNECTOR_FENCED"):
        reopened.serve(toy.EVAL_INPUTS)
    assert not list((state / "jobs").glob("*/artifact"))
    # Reads stay available so the owner can still see what happened.
    assert reopened.lookup(intent) is not None


def test_rollback_restores_the_pinned_base_and_last_authorized_target(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state)
    pre, _, receipt = dispatch(family, connector, "generated-rollback")
    candidate = pre.collect_candidate(ACTOR, receipt)
    baseline = connector.serve(toy.EVAL_INPUTS)
    assert connector.serving() == "base"
    with pytest.raises(owc.ConnectorRefusal, match="NOTHING_TO_ROLL_BACK"):
        connector.rollback()
    connector.promote(candidate)
    assert connector.serving() == candidate.artifact_or_serving_revision
    assert family.connector(state).serving() == candidate.artifact_or_serving_revision  # durable
    assert connector.serve(toy.EVAL_INPUTS) != baseline
    assert connector.rollback() == "base"
    assert connector.serve(toy.EVAL_INPUTS) == baseline
    forged = replace(candidate, artifact_or_serving_revision="1" * 64)
    with pytest.raises(owc.ConnectorRefusal, match="CANDIDATE_LINEAGE_UNVERIFIED"):
        connector.promote(forged)


def test_no_automatic_substitution_a_different_revision_is_held_not_swapped(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    other = replace(family.binding, immutable_revision="a" * 40)
    pre = mc.Preflight(
        connector, standard_grants(other, family.intent("generated-sub", binding=other))
    )
    capability = pre.discover(ACTOR, other, mc.Purpose.TRAINING)
    assert capability.state is mc.State.NEEDS_CONFIGURATION
    assert capability.reason == "exact_model_revision_unverified"
    exact = mc.Preflight(connector, standard_grants(family.binding, family.intent("generated-ok")))
    assert exact.discover(ACTOR, family.binding, mc.Purpose.TRAINING).state is mc.State.TRAINABLE


def test_incompatible_installation_is_not_trainable_and_runs_zero_fits(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.incompatible_connector(state)
    intent = family.intent("generated-incompatible")
    pre = mc.Preflight(connector, standard_grants(family.binding, intent))
    capability = pre.discover(ACTOR, family.binding, mc.Purpose.TRAINING)
    assert capability.state is mc.State.NEEDS_CONFIGURATION
    guard = CountingGuard()
    with pytest.raises(mc.CapabilityError):
        pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=guard)
    assert guard.entries == 0 and not list((state / "jobs").iterdir())
    # Inference is also withheld rather than served from a model we did not pin.
    observed = connector.observe(family.binding, mc.Purpose.BASELINE)
    assert not observed.inference_supported and observed.resolved_revision is None


def test_sufficient_simple_repair_produces_zero_fits(family: toy.Family, tmp_path: Path) -> None:
    state = tmp_path / "state"
    connector = family.connector(state, residual_gate=lambda _intent: False)
    intent = family.intent("generated-repair-sufficient")
    pre = mc.Preflight(connector, standard_grants(family.binding, intent))
    with pytest.raises(owc.ConnectorRefusal, match="APPROVED_RESIDUAL_REQUIRED"):
        pre.submit_reserved_fit(ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard())
    assert not list((state / "jobs").iterdir())


def test_fixture_family_is_not_usable_unless_fixtures_are_explicitly_allowed(
    family: toy.Family, tmp_path: Path
) -> None:
    strict = owc.OpenWeightConnector(
        owc.SubprocessFamilyRunner(
            family.profile, family.model_dir, toy.WORKER, family.profile_path
        ),
        tmp_path / "state",
    )
    with pytest.raises(ValueError, match="MODEL_FAMILY_UNSUPPORTED"):
        strict.observe(family.binding, mc.Purpose.TRAINING)
    pre = mc.Preflight(strict, standard_grants(family.binding, family.intent("generated-prod")))
    assert pre.discover(ACTOR, family.binding, mc.Purpose.TRAINING).state is (
        mc.State.NEEDS_CONFIGURATION
    )


def test_only_the_local_route_task_and_formatter_are_supported(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    for change in (
        {"route": mc.Route.MANAGED},
        {"task": "generated-other-task"},
        {"preprocessing_revision": "generated-other-formatter"},
    ):
        with pytest.raises(ValueError, match="MODEL_FAMILY_UNSUPPORTED"):
            connector.observe(replace(family.binding, **change), mc.Purpose.TRAINING)


def test_development_payload_is_scoped_digested_and_carries_only_permitted_fields(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    intent = family.intent("generated-payload")
    payload = connector.serialize_development(intent, toy.TRAIN)
    assert owc._sha(payload) == intent.development_digest
    assert connector.serialize_development(intent, tuple(reversed(toy.TRAIN))) == payload
    with pytest.raises(ValueError, match="TRAINING_ROW_FIELDS_OUTSIDE_SCOPE"):
        connector.serialize_development(
            intent, ({**toy.TRAIN[0], "protected_reference": "generated-secret"},)
        )
    with pytest.raises(ValueError, match="TRAINING_ROWS_OUTSIDE_PROFILE"):
        connector.serialize_development(intent, ())
    with pytest.raises(ValueError, match="TRAINING_ROWS_OUTSIDE_PROFILE"):
        connector.serialize_development(intent, toy.TRAIN * 3)
    tampered = payload.replace(b"billing", b"shipping", 1)
    with pytest.raises(owc.ConnectorRefusal, match="DEVELOPMENT_DIGEST_MISMATCH"):
        connector.submit(intent, tampered)
    assert not list((tmp_path / "state/jobs").iterdir())


def test_protected_answers_never_enter_the_worker_request(
    family: toy.Family, tmp_path: Path
) -> None:
    seen: list[dict[str, Any]] = []
    connector = family.connector(tmp_path / "state")
    real = connector.runner._call  # type: ignore[attr-defined]

    def spy(request: dict[str, Any], seconds: int, control: Any) -> dict[str, Any]:
        seen.append(json.loads(json.dumps(request)))
        return real(request, seconds, control)  # type: ignore[no-any-return]

    connector.runner._call = spy  # type: ignore[attr-defined]
    pre, _, receipt = dispatch(family, connector, "generated-protected")
    connector.predict_for_replay(pre.collect_candidate(ACTOR, receipt), toy.EVAL_INPUTS)
    protected = set(toy.EVAL_REFERENCES) | {"protected_reference"}
    encoded = json.dumps(seen)
    assert all(request["operation"] in ("fit", "predict") for request in seen)
    replay = next(r for r in seen if r["operation"] == "predict" and r["adapter"] is not None)
    assert all(set(item) == {"text"} for item in replay["inputs"])
    assert not any(label in json.dumps(replay) for label in protected)
    assert "protected_reference" not in encoded


def test_usage_is_measured_zero_cost_and_unknown_until_the_job_is_terminal(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    _, _, receipt = dispatch(family, connector, "generated-usage")
    usage = connector.usage(receipt)
    assert usage is not None
    assert (usage.incurred_minor_units, usage.rows, usage.steps) == (0, 8, 40)
    assert usage.source is UsageSource.MEASURED and not usage.irreversible
    assert connector.usage(replace(receipt, job_ref="simulated:other")) is None


def test_corrupt_journal_is_unknown_not_a_second_execution(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state)
    pre, intent, _ = dispatch(family, connector, "generated-journal")
    journal = next((state / "jobs").glob("*/journal.jsonl"))
    journal.write_text("not json\n")
    reopened = family.connector(state)
    pre = mc.Preflight(reopened, standard_grants(family.binding, intent))
    assert pre.reconcile_fit(ACTOR, intent).state is mc.JobState.UNKNOWN


# ---------------------------------------------------------------------------------------------
# Reference wrapper (SmolLM2 decision adapter). Uses fakes: no weights are available here.
# ---------------------------------------------------------------------------------------------


def _reference_runner(tmp_path: Path, **fakes: Any) -> ref.ReferenceDecisionRunner:
    from opentour_cell.pilot.model_family import load_profile

    profile = load_profile(ref.reference_profile_path())
    return ref.ReferenceDecisionRunner(
        profile,
        tmp_path / "model",
        tmp_path / "work",
        grant={"generated": "grant"},
        residual={"generated": "residual"},
        protected_fingerprints=("0" * 64,),
        **fakes,
    )


class _Never:
    def stop_reason(self) -> str | None:
        return None


def test_reference_runner_wraps_the_qualified_fit_and_maps_evidence(tmp_path: Path) -> None:
    calls: list[dict[str, Any]] = []

    def fake_fit(bundle: Path, rows: list[dict[str, Any]], **kwargs: Any) -> dict[str, Any]:
        calls.append({"rows": rows, **kwargs})
        bundle.mkdir(parents=True)
        (bundle / "adapter.json").write_text("{}")
        (bundle / "manifest.json").write_text("{}")
        return {
            "identity": {"base_sha256": "5" * 64},
            "training": {
                "initial_loss": 2.0,
                "final_loss": 0.5,
                "changed_parameters": [10, 20],
                "steps": 64,
            },
        }

    runner = _reference_runner(tmp_path, fit_fn=fake_fit)
    rows = ({"case_id": "generated-c1", "family_id": "generated-f1", "features": {}, "label": "a"},)
    output = runner.fit(rows, 64, 300, _Never())
    assert set(output.files) == {"adapter.json", "manifest.json"}
    assert output.evidence["parameters_changed"] == 30
    assert output.evidence["base_sha256_before"] == output.evidence["base_sha256_after"]
    assert calls[0]["rows"] == [dict(rows[0])] and calls[0]["protected_fingerprints"] == ("0" * 64,)


def test_reference_runner_cancellation_and_failures_are_fail_closed(tmp_path: Path) -> None:
    from opentour_cell.pilot import open_weight as ow

    class Stop:
        def stop_reason(self) -> str | None:
            return "JOB_CANCELED"

    def interrupted(bundle: Path, rows: Any, *, check_permission: Any, **_kw: Any) -> Any:
        check_permission()
        raise AssertionError("unreachable")

    runner = _reference_runner(tmp_path, fit_fn=interrupted)
    with pytest.raises(owc.RunInterrupted) as stopped:
        runner.fit((), 64, 300, Stop())
    assert stopped.value.reason == "JOB_CANCELED"

    def refused(*_a: Any, **_kw: Any) -> Any:
        raise ow.OpenWeightError("model_compute_data_and_destination_approval_required")

    with pytest.raises(ValueError, match="^MODEL_COMPUTE_DATA_AND_DESTINATION_APPROVAL_REQUIRED$"):
        _reference_runner(tmp_path, fit_fn=refused).fit((), 64, 300, _Never())
    with pytest.raises(ValueError, match="BASE_INFERENCE_NOT_SUPPORTED"):
        runner.predict(None, ())
    with pytest.raises(ValueError, match="INSTALLED_MODEL_FILE_MISSING"):
        runner.installed_revision()  # no staged weights in the control workspace


def test_reference_runner_replay_passes_only_the_artifact_and_features(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    def fake_predict(artifact: Path, features: list[dict[str, str]], **kwargs: Any) -> Any:
        seen.update(artifact=artifact, features=features, kwargs=kwargs)
        return [{"status": "automatic", "label": "x"}]

    runner = _reference_runner(tmp_path, predict_fn=fake_predict)
    out = runner.predict(tmp_path / "artifact", ({"question": "generated"},))
    assert out == ({"status": "automatic", "label": "x"},)
    assert seen["features"] == [{"question": "generated"}]
    assert set(seen["kwargs"]) == {"model_dir", "grant", "python"}


def test_unstaged_reference_checkpoint_is_an_unsatisfied_external_gate_not_a_pass(
    tmp_path: Path,
) -> None:
    """The real SmolLM2 reproduction needs staged weights; absence is NEEDS_CONFIGURATION."""
    from opentour_cell.pilot.model_family import load_profile

    profile = load_profile(ref.reference_profile_path())
    runner = ref.ReferenceDecisionRunner(
        profile,
        tmp_path / "absent",
        tmp_path / "work",
        grant={},
        residual={},
        protected_fingerprints=("0" * 64,),
    )
    connector = owc.OpenWeightConnector(runner, tmp_path / "state")
    binding = replace(toy.Family._binding(profile), model_ref=profile.family_id)
    observed = connector.observe(binding, mc.Purpose.TRAINING)
    assert observed.resolved_revision is None and not observed.training_supported
    pre = mc.Preflight(
        connector,
        standard_grants(
            binding,
            mc.FitIntent(
                binding,
                "generated-grant-v1",
                "generated-benchmark-v1",
                "0" * 64,
                "generated-key",
                frozenset({"case_id"}),
                mc.FitBound("USD", 0, 300, 32, 64),
            ),
        ),
    )
    assert pre.discover(ACTOR, binding, mc.Purpose.TRAINING).state is mc.State.NEEDS_CONFIGURATION


def test_unpinned_second_family_cannot_be_trained(tmp_path: Path) -> None:
    from opentour_cell.pilot.model_family import load_profile

    candidate = load_profile(
        ref.reference_profile_path().with_name("pythia_160m_neox_candidate.family.json")
    )
    with pytest.raises(FamilyError, match="FAMILY_NOT_PINNED"):
        verify_model_directory(candidate, tmp_path)
    runner = owc.SubprocessFamilyRunner(candidate, tmp_path, toy.WORKER, tmp_path / "p.json")
    with pytest.raises(ValueError, match="FAMILY_NOT_PINNED"):
        runner.installed_revision()


def test_fence_never_reports_a_job_stopped_when_its_worker_still_holds_it(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    connector.cancel_wait_seconds = 0.2
    job_dir = connector.root / "jobs" / ("a" * 32)
    job_dir.mkdir(mode=0o700)
    owc._append(job_dir / "journal.jsonl", {"event": "accepted", "intent_sha256": "0" * 64})
    owner = owc._Lock(job_dir / "lock")
    assert owner.acquire()
    try:
        with pytest.raises(owc.ConnectorRefusal, match="FENCE_JOB_NOT_STOPPED"):
            connector.fence("generated-stuck")
        # The fence itself is durable even though the stop could not be confirmed.
        assert (connector.root / "FENCED").exists()
    finally:
        owner.release()


# ---------------------------------------------------------------------------------------------
# OPE-507 hardening: usage never reports a settleable zero for work whose usage is unknown.
# ---------------------------------------------------------------------------------------------


def test_usage_is_unknown_not_a_settleable_zero_for_failed_jobs(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    ticks = iter(range(0, 10_000, 100))
    connector = family.connector(state, slow=True)
    connector.clock = lambda: float(next(ticks))
    intent = family.intent("generated-failed-usage")
    receipt = connector.submit(intent, connector.serialize_development(intent, toy.TRAIN))
    assert receipt.state is mc.JobState.FAILED
    assert connector.usage(receipt) is None


def test_usage_is_unknown_not_a_settleable_zero_for_canceled_jobs(
    family: toy.Family, tmp_path: Path
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state, slow=True)
    intent = family.intent("generated-canceled-usage")
    pre = mc.Preflight(connector, standard_grants(family.binding, intent))
    thread = threading.Thread(
        target=lambda: pre.submit_reserved_fit(
            ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard()
        )
    )
    thread.start()
    live = None
    deadline = time.monotonic() + 10
    while live is None and time.monotonic() < deadline:
        live = connector.lookup(intent)
        time.sleep(0.02)
    assert live is not None
    canceled = connector.cancel(live)
    thread.join(timeout=30)
    assert canceled.state is mc.JobState.CANCELED
    assert connector.usage(canceled) is None


@pytest.mark.parametrize("field", ["seconds", "rows", "steps"])
@pytest.mark.parametrize("bad", ["abc", None, -1, 1.5, True])
def test_a_corrupt_completed_event_makes_usage_unknown_instead_of_raising(
    family: toy.Family, tmp_path: Path, field: str, bad: object
) -> None:
    state = tmp_path / "state"
    connector = family.connector(state)
    _, _, receipt = dispatch(family, connector, "generated-corrupt-usage")
    assert connector.usage(receipt) is not None
    journal = next((state / "jobs").glob("*/journal.jsonl"))
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    for event in events:
        if event["event"] == "completed":
            event[field] = bad
    journal.write_text("".join(json.dumps(event) + "\n" for event in events))
    assert connector.usage(receipt) is None
    assert connector.hold_reason(receipt) == "usage_unverifiable"


# ---------------------------------------------------------------------------------------------
# OPE-508 follow-up: the open-weight route adopts the managed route's usage-vs-cap hold.
# W1 measured usage above the journaled cap is held ``over_cap``: no candidate, no replay, no
#    promotion or serving, ``hold_reason`` reports it and ``settleable`` is False
# W2 usage exactly at the cap is accepted and released
# W3 missing or malformed journaled usage is held ``usage_unverifiable``
# W4 a journaled cap that is missing or not a plain int is held ``cap_unverifiable``
# W5 dispatch journals all four maxima and the currency; release compares to those
# W6 ``hold_reason`` is None unless the job is COMPLETED, and checks the receipt
# W7 an intent bound that is not exactly ``int`` (an ``int`` subclass, bool, float) is refused
# ---------------------------------------------------------------------------------------------
OW_MAXIMA = ("maximum_minor_units", "maximum_seconds", "maximum_rows", "maximum_steps")


class SneakyInt(int):
    """An ``int`` subclass. It serializes like the plain int, so it matches the intent digest."""


class _ClockFreeRunner:
    """The real subprocess runner, except that ``fit`` never consults the connector's clock.

    ``_execute`` then reads the clock exactly three times (start, the stop check after the fit and
    the measurement), so a scripted clock can make the measured wall time land where a test wants.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.profile = inner.profile

    def installed_revision(self) -> str:
        return str(self.inner.installed_revision())

    def fit(self, rows: Any, steps: int, seconds: int, control: Any) -> owc.FitOutput:
        return self.inner.fit(rows, steps, seconds, _Never())  # type: ignore[no-any-return]

    def predict(self, artifact_dir: Path | None, inputs: Any) -> Any:
        return self.inner.predict(artifact_dir, inputs)


def _measured(
    family: toy.Family, state: Path, key: str, seconds: float
) -> tuple[owc.OpenWeightConnector, mc.FitIntent, mc.JobReceipt]:
    """A real fit whose measured wall time is ``seconds`` (the deadline is never reached)."""
    connector = family.connector(state)
    connector.runner = _ClockFreeRunner(connector.runner)  # type: ignore[assignment]
    ticks = iter([0.0, 0.0])
    connector.clock = lambda: next(ticks, seconds)
    intent = family.intent(key)
    receipt = connector.submit(intent, connector.serialize_development(intent, toy.TRAIN))
    connector.clock = time.time
    return connector, intent, receipt


def _journal_of(connector: owc.OpenWeightConnector) -> Path:
    return next((connector.root / "jobs").glob("*/journal.jsonl"))


def _rewrite(connector: owc.OpenWeightConnector, edit: Any) -> None:
    journal = _journal_of(connector)
    events = [json.loads(line) for line in journal.read_text().splitlines()]
    for event in events:
        edit(event)
    journal.write_text("".join(json.dumps(event) + "\n" for event in events))


def _hand_built(connector: owc.OpenWeightConnector, receipt: mc.JobReceipt) -> mc.Candidate:
    """What ``collect`` would return, built from the journal without calling ``collect``."""
    events = [json.loads(line) for line in _journal_of(connector).read_text().splitlines()]
    artifact = next(e["artifact_sha256"] for e in events if e["event"] == "completed")
    return mc.Candidate(
        receipt.intent,
        receipt.job_ref,
        receipt.intent.binding.immutable_revision,
        artifact,
        "simulated:hand-built",
    )


def _assert_ow_held(
    family: toy.Family,
    connector: owc.OpenWeightConnector,
    receipt: mc.JobReceipt,
    reason: str,
) -> None:
    exact = f"^{reason}$"
    assert receipt.state is mc.JobState.COMPLETED  # the receipt stays the job's own state
    # The evidence ref hashes the journal bytes, so a rewritten journal needs a fresh receipt.
    receipt = connector.status(receipt)
    assert receipt.state is mc.JobState.COMPLETED
    assert connector.hold_reason(receipt) == reason
    with pytest.raises(ValueError, match=exact):
        connector.collect(receipt)
    pre = mc.Preflight(connector, standard_grants(family.binding, receipt.intent))
    with pytest.raises(mc.CapabilityError, match="candidate_lineage_unverified"):
        pre.collect_candidate(ACTOR, receipt)
    candidate = _hand_built(connector, receipt)
    with pytest.raises(ValueError, match=exact):
        connector.predict_for_replay(candidate, toy.EVAL_INPUTS)
    with pytest.raises(ValueError, match=exact):
        connector.promote(candidate)
    assert connector.serving() == "base"
    # A target promoted before the hold existed is never served either.
    owc._append(
        connector.root / "serving.jsonl",
        {
            "event": "promote",
            "artifact_sha256": candidate.artifact_or_serving_revision,
            "job_ref": candidate.job_ref,
        },
    )
    with pytest.raises(ValueError, match=exact):
        connector.serve(toy.EVAL_INPUTS)
    usage = connector.usage(receipt)
    if reason == "usage_unverifiable":
        assert usage is None
    else:
        assert isinstance(usage, CappedUsage) and usage.settleable is False
        assert usage.cap_status == ("over_cap" if reason == "over_cap" else "cap_unknown")
    # A restart replays the hold from the journal alone.
    assert family.connector(connector.root).hold_reason(receipt) == reason


def _assert_ow_released(
    family: toy.Family, connector: owc.OpenWeightConnector, receipt: mc.JobReceipt
) -> None:
    assert receipt.state is mc.JobState.COMPLETED
    assert connector.hold_reason(receipt) is None
    pre = mc.Preflight(connector, standard_grants(family.binding, receipt.intent))
    candidate = pre.collect_candidate(ACTOR, receipt)
    assert candidate == connector.collect(receipt)
    assert connector.predict_for_replay(candidate, toy.EVAL_INPUTS)
    connector.promote(candidate)
    assert connector.serving() == candidate.artifact_or_serving_revision
    assert connector.serve(toy.EVAL_INPUTS)
    usage = connector.usage(receipt)
    assert isinstance(usage, CappedUsage) and usage.settleable is True
    assert usage.cap_status == "within_cap"


@pytest.mark.parametrize("seconds", [61.0, 10_000.0])
def test_w1_measured_usage_above_the_journaled_cap_is_held(
    family: toy.Family, tmp_path: Path, seconds: float
) -> None:
    connector, _, receipt = _measured(family, tmp_path / "state", "generated-over", seconds)
    _assert_ow_held(family, connector, receipt, "over_cap")
    usage = connector.usage(receipt)
    assert usage is not None and usage.seconds == int(seconds)  # the real figure, never clamped


def test_w2_usage_exactly_at_the_cap_is_released(family: toy.Family, tmp_path: Path) -> None:
    connector, intent, receipt = _measured(family, tmp_path / "state", "generated-at-cap", 60.0)
    assert intent.bound.maximum_seconds == 60 and intent.bound.maximum_minor_units == 0
    _assert_ow_released(family, connector, receipt)
    usage = connector.usage(receipt)
    assert usage is not None and (usage.seconds, usage.incurred_minor_units) == (60, 0)


@pytest.mark.parametrize(
    "field, value",
    [
        ("seconds", None),
        ("rows", -1),
        ("steps", 1.5),
        ("minor_units", True),
        ("minor_units", "drop"),
        ("currency", "drop"),
        ("currency", ""),
    ],
)
def test_w3_missing_or_malformed_journaled_usage_is_usage_unverifiable(
    family: toy.Family, tmp_path: Path, field: str, value: object
) -> None:
    connector = family.connector(tmp_path / "state")
    _, _, receipt = dispatch(family, connector, "generated-unverifiable")

    def corrupt(event: dict[str, Any]) -> None:
        if event["event"] == "completed":
            if value == "drop":
                event.pop(field, None)
            else:
                event[field] = value

    _rewrite(connector, corrupt)
    _assert_ow_held(family, connector, receipt, "usage_unverifiable")


@pytest.mark.parametrize(
    "field, value",
    [
        ("currency", "drop"),
        ("maximum_minor_units", "drop"),
        ("maximum_seconds", "drop"),
        ("maximum_rows", "drop"),
        ("maximum_steps", "drop"),
        ("maximum_steps", True),
        ("maximum_seconds", 60.0),
        ("maximum_rows", -1),
        ("currency", "EUR"),
    ],
)
def test_w4_unusable_journaled_cap_is_cap_unverifiable(
    family: toy.Family, tmp_path: Path, field: str, value: object
) -> None:
    connector = family.connector(tmp_path / "state")
    _, _, receipt = dispatch(family, connector, "generated-cap-unverifiable")

    def corrupt(event: dict[str, Any]) -> None:
        if event["event"] == "accepted":
            if value == "drop":
                event.pop(field, None)
            else:
                event[field] = value

    _rewrite(connector, corrupt)
    _assert_ow_held(family, connector, receipt, "cap_unverifiable")


def test_w5_dispatch_journals_all_four_maxima_and_release_compares_to_them(
    family: toy.Family, tmp_path: Path
) -> None:
    connector = family.connector(tmp_path / "state")
    _, intent, receipt = dispatch(family, connector, "generated-journaled-maxima")
    first = json.loads(_journal_of(connector).read_text().splitlines()[0])
    assert first["event"] == "accepted"
    assert {name: first.get(name) for name in ("currency", *OW_MAXIMA)} == {
        "currency": "USD",
        "maximum_minor_units": 0,
        "maximum_seconds": 60,
        "maximum_rows": 16,
        "maximum_steps": 64,
    }
    assert connector.hold_reason(receipt) is None
    steps = family.profile.resource.max_steps

    def lower(event: dict[str, Any]) -> None:
        if event["event"] == "accepted":
            event["maximum_steps"] = steps - 1  # the caller's bound (64) still allows 40 steps

    _rewrite(connector, lower)
    assert intent.bound.maximum_steps >= steps
    _assert_ow_held(family, connector, receipt, "over_cap")


def test_w6_hold_reason_is_none_unless_completed_and_checks_the_receipt(
    family: toy.Family, tmp_path: Path
) -> None:
    ticks = iter(range(0, 10_000, 100))
    connector = family.connector(tmp_path / "failed", slow=True)
    connector.clock = lambda: float(next(ticks))
    intent = family.intent("generated-failed-hold")
    failed = connector.submit(intent, connector.serialize_development(intent, toy.TRAIN))
    assert failed.state is mc.JobState.FAILED and connector.hold_reason(failed) is None
    done_connector = family.connector(tmp_path / "done")
    _, _, done = dispatch(family, done_connector, "generated-done-hold")
    other = replace(done, intent=family.intent("generated-other-intent"))
    with pytest.raises(owc.ConnectorRefusal, match="IDEMPOTENCY_CONFLICT"):
        done_connector.hold_reason(other)
    with pytest.raises(ValueError, match="JOB_REFERENCE_UNKNOWN"):
        done_connector.hold_reason(replace(done, job_ref="simulated:other"))


def test_w6_canceled_job_reports_no_hold(family: toy.Family, tmp_path: Path) -> None:
    connector = family.connector(tmp_path / "state", slow=True)
    intent = family.intent("generated-canceled-hold")
    pre = mc.Preflight(connector, standard_grants(family.binding, intent))
    thread = threading.Thread(
        target=lambda: pre.submit_reserved_fit(
            ACTOR, intent, toy.TRAIN, reservation_guard=CountingGuard()
        )
    )
    thread.start()
    live = None
    deadline = time.monotonic() + 10
    while live is None and time.monotonic() < deadline:
        live = connector.lookup(intent)
        time.sleep(0.02)
    assert live is not None
    canceled = connector.cancel(live)
    thread.join(timeout=30)
    assert canceled.state is mc.JobState.CANCELED and connector.hold_reason(canceled) is None


@pytest.mark.parametrize("limit", OW_MAXIMA)
@pytest.mark.parametrize("kind", ["subclass", "bool", "float"])
def test_w7_intent_bound_that_is_not_exactly_int_is_refused_before_dispatch(
    family: toy.Family, tmp_path: Path, limit: str, kind: str
) -> None:
    connector = family.connector(tmp_path / "state")
    intent = family.intent("generated-sneaky")
    value = getattr(intent.bound, limit)
    forged_value = {"subclass": SneakyInt(value), "bool": bool(value), "float": float(value)}[kind]
    forged = replace(intent, bound=replace(intent.bound, **{limit: forged_value}))
    payload = connector.serialize_development(forged, toy.TRAIN)
    with pytest.raises(ValueError, match="^fit_bound_not_exact_int$"):
        connector.submit(forged, payload)
    # Only submit refuses it (F1-241): the digest applies no shape check. A subclass digests like
    # the plain int; a bool or float digests differently, so it can match no journaled job.
    same = owc.intent_digest(forged) == owc.intent_digest(intent)
    assert same is (kind == "subclass")
    assert not list((connector.root / "jobs").iterdir())


@pytest.mark.parametrize("limit", OW_MAXIMA)
def test_w7_int_subclass_receipt_is_refused_and_cannot_change_the_hold_reason(
    family: toy.Family, tmp_path: Path, limit: str
) -> None:
    connector, intent, receipt = _measured(family, tmp_path / "state", "generated-l1", 61.0)
    sneaky = replace(
        intent, bound=replace(intent.bound, **{limit: SneakyInt(getattr(intent.bound, limit))})
    )
    forged = replace(receipt, intent=sneaky)
    # F1-241: it names the same journaled job and reads exactly what the genuine receipt reads;
    # release compares only the journaled cap, so the genuine ``over_cap`` stands.
    assert connector.hold_reason(forged) == "over_cap"
    assert connector.status(forged).state is mc.JobState.COMPLETED
    assert connector.lookup(sneaky).state is mc.JobState.COMPLETED
    with pytest.raises(ValueError, match="^over_cap$"):
        connector.collect(forged)
    usage = connector.usage(forged)
    assert usage is not None and usage.cap_status == "over_cap" and usage.settleable is False
    assert connector.hold_reason(receipt) == "over_cap"


# Review of #241. F3 (owner-delegate decision: round up): measured seconds are rounded up, so any
# fraction of a second over the cap is ``over_cap``. F4 (owner-delegate decision: refuse before
# spend): a negative maximum or a currency that is not an ISO-4217-shaped code is refused before
# anything is journaled or run.
@pytest.mark.parametrize("seconds", [60.001, 60.5, 60.999])
def test_w8_a_fraction_of_a_second_over_the_cap_is_over_cap(
    family: toy.Family, tmp_path: Path, seconds: float
) -> None:
    connector, _, receipt = _measured(family, tmp_path / "state", "generated-fraction", seconds)
    _assert_ow_held(family, connector, receipt, "over_cap")
    usage = connector.usage(receipt)
    assert usage is not None and usage.seconds == 61  # rounded up, never down


@pytest.mark.parametrize("seconds", [59.001, 59.999, 60.0])
def test_w8_measured_seconds_up_to_the_cap_are_within(
    family: toy.Family, tmp_path: Path, seconds: float
) -> None:
    connector, _, receipt = _measured(family, tmp_path / "state", "generated-within", seconds)
    _assert_ow_released(family, connector, receipt)
    usage = connector.usage(receipt)
    assert usage is not None and usage.seconds == 60


@pytest.mark.parametrize("limit", OW_MAXIMA)
def test_w9_negative_intent_bound_is_refused_before_dispatch(
    family: toy.Family, tmp_path: Path, limit: str
) -> None:
    connector = family.connector(tmp_path / "state")
    intent = family.intent("generated-negative")
    forged = replace(intent, bound=replace(intent.bound, **{limit: -1}))
    payload = connector.serialize_development(forged, toy.TRAIN)
    with pytest.raises(ValueError, match="^fit_bound_negative$"):
        connector.submit(forged, payload)
    assert not list((connector.root / "jobs").iterdir())


@pytest.mark.parametrize("currency", ["", "usd", "US", "USDX", None])
def test_w9_invalid_intent_currency_is_refused_before_dispatch(
    family: toy.Family, tmp_path: Path, currency: object
) -> None:
    connector = family.connector(tmp_path / "state")
    intent = family.intent("generated-currency")
    forged = replace(intent, bound=replace(intent.bound, currency=currency))
    payload = connector.serialize_development(forged, toy.TRAIN)
    with pytest.raises(ValueError, match="^fit_bound_currency_invalid$"):
        connector.submit(forged, payload)
    assert connector.lookup(forged) is None  # F1-241: only submit applies the shape check
    assert not list((connector.root / "jobs").iterdir())


# Review of #241 at 7ae8613, F1-241 (owner-delegate decision): the bound shape is validated only
# on submit, where a new digest is journaled; the digest of an intent that is already journaled
# applies no shape check, so a job dispatched under an older rule stays reachable. On this route a
# negative maximum never dispatched even then (the resource check refuses it), so the legacy bound
# that reached the runner is a currency that is not ISO-4217-shaped. Its release stays closed.
# G1 a RUNNING legacy job answers lookup, status, hold_reason, usage and collect, and cancels
# G2 a COMPLETED legacy job is held ``cap_unverifiable``, although its measured usage repeats the
#    legacy currency (without the journaled-currency shape check it would release)
# G4 a receipt whose bound cannot be serialized names no job: a fixed refusal, never a TypeError
OW_LEGACY_CURRENCIES = ["usd", "US"]


def _old_rule(bound: mc.FitBound) -> None:
    """The rule an older build applied at dispatch: exact ``int`` maxima only, any code."""
    if any(type(getattr(bound, limit)) is not int for limit in OW_MAXIMA):
        raise ValueError("fit_bound_not_exact_int")


@contextmanager
def _older_rule(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    with monkeypatch.context() as patch:
        patch.setattr(ext, "require_exact_bound", _old_rule)
        patch.setattr(owc, "require_exact_bound", _old_rule)
        yield


def _legacy_intent(family: toy.Family, key: str, currency: str) -> mc.FitIntent:
    intent = family.intent(key)
    return replace(intent, bound=replace(intent.bound, currency=currency))


@pytest.mark.parametrize("currency", OW_LEGACY_CURRENCIES)
def test_g1_running_job_with_a_legacy_bound_stays_reachable(
    family: toy.Family, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, currency: str
) -> None:
    connector = family.connector(tmp_path / "state", slow=True)
    intent = _legacy_intent(family, "generated-legacy-running", currency)
    payload = connector.serialize_development(intent, toy.TRAIN)
    journal = connector.root / "jobs" / connector._key(intent) / "journal.jsonl"
    thread = threading.Thread(target=lambda: connector.submit(intent, payload))
    with _older_rule(monkeypatch):
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and not (
            journal.is_file() and '"started"' in journal.read_text()
        ):
            time.sleep(0.02)
    with pytest.raises(ValueError, match="^fit_bound_currency_invalid$"):
        owc.require_exact_bound(intent.bound)  # the current rule is back
    live = connector.lookup(intent)
    assert live is not None and live.state is mc.JobState.RUNNING
    assert connector.status(live) == live
    assert connector.hold_reason(live) is None
    assert connector.usage(live) is None
    with pytest.raises(ValueError, match="^CANDIDATE_LINEAGE_UNVERIFIED$"):
        connector.collect(live)
    canceled = connector.cancel(live)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert canceled.state is mc.JobState.CANCELED
    assert connector.status(canceled).state is mc.JobState.CANCELED
    assert connector.hold_reason(canceled) is None and connector.usage(canceled) is None


@pytest.mark.parametrize("currency", OW_LEGACY_CURRENCIES)
def test_g2_completed_job_with_a_legacy_bound_is_held_and_never_released(
    family: toy.Family, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, currency: str
) -> None:
    connector = family.connector(tmp_path / "state")
    intent = _legacy_intent(family, "generated-legacy-done", currency)
    payload = connector.serialize_development(intent, toy.TRAIN)
    with _older_rule(monkeypatch):
        receipt = connector.submit(intent, payload)
    assert receipt.state is mc.JobState.COMPLETED
    events = [json.loads(line) for line in _journal_of(connector).read_text().splitlines()]
    assert events[0]["currency"] == events[-1]["currency"] == currency  # same legacy code
    assert connector.status(receipt) == receipt
    _assert_ow_held(family, connector, receipt, "cap_unverifiable")


@pytest.mark.parametrize(
    "limit, value",
    [
        ("maximum_steps", Decimal(64)),
        ("maximum_seconds", float("nan")),
        ("maximum_rows", float("inf")),
        ("maximum_minor_units", object()),
    ],
    ids=["decimal", "nan", "inf", "object"],
)
def test_g4_a_receipt_whose_bound_cannot_be_serialized_names_no_job(
    family: toy.Family, tmp_path: Path, limit: str, value: object
) -> None:
    connector = family.connector(tmp_path / "state")
    _, intent, receipt = dispatch(family, connector, "generated-unserializable")
    forged_intent = replace(intent, bound=replace(intent.bound, **{limit: value}))
    assert owc.intent_digest(forged_intent) == ext.UNSERIALIZABLE_DIGEST
    forged = replace(receipt, intent=forged_intent)
    for call in (
        lambda: connector.status(forged),
        lambda: connector.hold_reason(forged),
        lambda: connector.collect(forged),
        lambda: connector.lookup(forged_intent),
    ):
        with pytest.raises(owc.ConnectorRefusal, match="^IDEMPOTENCY_CONFLICT$"):
            call()
    assert connector.usage(forged) is None  # an unusable receipt is unknown, never an error
    with pytest.raises(ValueError, match="^fit_bound_not_exact_int$"):
        connector.submit(forged_intent, connector.serialize_development(intent, toy.TRAIN))
    assert connector.hold_reason(receipt) is None  # the genuine receipt is untouched
