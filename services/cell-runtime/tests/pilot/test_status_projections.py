"""Typed, receipt-backed status projections. Generated sentinel fixtures and fixed clocks only.

Each projection must answer only from a current, in-scope, well-formed producer receipt and hold,
with an explicit reason, when that receipt is absent, stale, out of scope, malformed or unknown.
Where a producer exists, the receipts come from it: the training readiness worker, the release
outbox status type and live evidence source-read receipts. No provider, customer or installed
Slack evidence is claimed.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import live_evidence, training_readiness
from opentour_cell.pilot import status_questions as sq
from opentour_cell.pilot.intake_mcp import REF_PATTERN
from opentour_cell.pilot.release_outbox import ReleaseStatus
from opentour_cell.pilot.status_questions import (
    ApprovalEvidence,
    CommittedDecision,
    DeliveryState,
    Disposition,
    Fidelity,
    HoldReason,
    LaterVerification,
    LiveWindowReceipt,
    LiveWindowScope,
    LiveWindowState,
    OriginalCapture,
    ReadinessScope,
    ReadinessState,
    StatusTopic,
    TimedRun,
    TimingPair,
    TimingPlan,
    TimingReceipt,
    TimingStage,
    TimingState,
    TraceScope,
    TraceState,
)
from opentour_cell.pilot.training import LocalTrainingStore
from opentour_cell.pilot.training_readiness import ReadinessSnapshot, TrainingReadiness
from test_live_evidence import ACTOR as LIVE_ACTOR
from test_live_evidence import Case
from test_live_evidence import decision as live_decision
from test_training_readiness import ACTOR as READER
from test_training_readiness import Assessment, Source, assessment, observed

AS_OF = 1_800_000_000  # Fixed generated clock: 2027-01-15 08:00 UTC.
DAY = 24 * 60 * 60


def text(projection: sq.StatusProjection) -> str:
    answer = projection.answer
    return " ".join((answer.finding, answer.basis, answer.next_action))


def refs(projection: sq.StatusProjection) -> set[str]:
    return {receipt.reference for receipt in projection.receipts}


def assert_held(projection: sq.StatusProjection, reason: HoldReason) -> None:
    assert projection.disposition is Disposition.HELD and projection.held
    assert projection.hold_reason is reason
    assert projection.next_action and projection.next_owner and projection.answer.next_action


def assert_answered(projection: sq.StatusProjection, state: str) -> None:
    assert projection.disposition is Disposition.ANSWERED and not projection.held
    assert projection.hold_reason is None and projection.state == state
    assert projection.receipts, "An answer always names the receipt it was read from."


# ----- Shared contract -----------------------------------------------------------------------


def test_projection_invariant_ties_holds_to_exactly_one_reason() -> None:
    answer = sq.StatusAnswer("finding", "basis", "next")
    with pytest.raises(ValueError):
        sq.StatusProjection(
            StatusTopic.TIMING, Disposition.HELD, "x", None, AS_OF, (), None, "a", "o", answer
        )
    with pytest.raises(ValueError):
        sq.StatusProjection(
            StatusTopic.TIMING,
            Disposition.ANSWERED,
            "x",
            HoldReason.RECEIPT_ABSENT,
            AS_OF,
            (),
            None,
            "a",
            "o",
            answer,
        )


@pytest.mark.parametrize("clock", [True, -1, 1.5, "1800000000", 2**63])
def test_projection_clock_must_be_an_explicit_integer(clock: Any) -> None:
    with pytest.raises(ValueError):
        sq.project_timing(None, baseline_revision=None, candidate_revision=None, as_of=clock)


def test_projection_module_imports_no_producer_or_slack_module() -> None:
    """The Slack adapter imports this module, so it must stay free of producer imports."""
    probe = (
        "import sys; import opentour_cell.pilot.status_questions; "
        "loaded = sorted(m for m in sys.modules if m.startswith('opentour_cell.pilot.') "
        "and m != 'opentour_cell.pilot.status_questions'); print(loaded); "
        "raise SystemExit(1 if loaded else 0)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, timeout=60
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_reference_contract_admits_every_intake_reference() -> None:
    """Projection references admit every intake reference and refuse whitespace and controls."""
    intake = re.compile(REF_PATTERN)
    for value in ("ready-" + "a" * 40, "live-0123", "release-x:y.z", "a" * 128, "Z9_.:-"):
        assert intake.fullmatch(value) and sq._REFERENCE.fullmatch(value)
    for value in ("", "a b", "a\nb", "a\x00", "x" * 257):
        assert sq._REFERENCE.fullmatch(value) is None


# ----- Speed: "How much faster is it?" · OPE-533 S05 --------------------------------------------


def run(
    ref: str,
    stage: TimingStage,
    elapsed: int | None,
    *,
    status: str = "completed",
    condition: str = "cond-cpu-4vcpu-warm-64steps",
    start: str = "fit_started",
    end: str = "fit_finished",
) -> TimedRun:
    return TimedRun(ref, stage.value, status, elapsed, condition, start, end)


def pairs(
    stage: TimingStage,
    baseline: list[int],
    candidate: list[int],
    *,
    start: str = "fit_started",
    end: str = "fit_finished",
) -> tuple[TimingPair, ...]:
    return tuple(
        TimingPair(
            f"{stage.value}-pair-{index}",
            run(f"{stage.value}-b-{index}", stage, b, start=start, end=end),
            run(f"{stage.value}-c-{index}", stage, c, start=start, end=end),
        )
        for index, (b, c) in enumerate(zip(baseline, candidate, strict=True))
    )


BASELINE_E2E = [6000, 6100, 5900, 6050, 5950]
CANDIDATE_E2E = [5400, 5500, 5300, 5600, 5450]
BASELINE_FIT = [2200, 2250, 2150, 2300, 2100]
CANDIDATE_FIT = [2000, 2100, 2050, 2000, 1900]


def timing_receipt(**changes: Any) -> TimingReceipt:
    base = TimingReceipt(
        "timing-receipt-1",
        AS_OF - 3600,
        "recipe-incumbent-v1",
        "recipe-candidate-v2",
        (
            TimingPlan(TimingStage.END_TO_END.value, 5),
            TimingPlan(TimingStage.FIT.value, 5),
        ),
        pairs(
            TimingStage.END_TO_END,
            BASELINE_E2E,
            CANDIDATE_E2E,
            start="correction_ready",
            end="verdict_verified",
        )
        + pairs(TimingStage.FIT, BASELINE_FIT, CANDIDATE_FIT),
        True,
    )
    return replace(base, **changes)


def project_speed(receipt: TimingReceipt | None, **changes: Any) -> sq.StatusProjection:
    arguments: dict[str, Any] = {
        "baseline_revision": "recipe-incumbent-v1",
        "candidate_revision": "recipe-candidate-v2",
        "as_of": AS_OF,
        **changes,
    }
    return sq.project_timing(receipt, **arguments)


def stage_of(projection: sq.StatusProjection, stage: TimingStage) -> sq.StageComparison:
    assert isinstance(projection.facts, sq.TimingFacts)
    return next(item for item in projection.facts.stages if item.stage is stage)


def test_five_registered_comparable_pairs_answer_with_units_and_endpoints() -> None:
    projection = project_speed(timing_receipt())
    assert_answered(projection, TimingState.COMPARED)
    end_to_end = stage_of(projection, TimingStage.END_TO_END)
    assert (end_to_end.baseline_median_ms, end_to_end.candidate_median_ms) == (6000.0, 5450.0)
    assert end_to_end.pairs == end_to_end.registered_pairs == 5
    # Median of the five per-pair ratios, and the slowest candidate relative to its baseline.
    assert end_to_end.median_ratio == 0.9016 and end_to_end.worst_pair_ratio == 0.9256
    fit = stage_of(projection, TimingStage.FIT)
    assert fit.state is TimingState.COMPARED and fit.median_ratio == 0.9091
    preparation = stage_of(projection, TimingStage.PREPARATION)
    assert preparation.state is TimingState.UNAVAILABLE
    assert preparation.hold_reason is HoldReason.RECEIPT_ABSENT
    rendered = text(projection)
    # Time to a verified verdict leads; fit and preparation are distinguished, never merged.
    assert projection.answer.finding.startswith("Time to a verified verdict is 9.8% shorter")
    assert "from correction_ready to verdict_verified" in rendered and " ms" in rendered
    assert "Fit wall time: baseline median 2,200.0 ms" in rendered
    assert "Preparation time is unavailable: no registered pairs." in rendered
    assert projection.next_action == "show_timing_measurements"
    assert refs(projection) == {"timing-receipt-1"}


def test_incompatible_timing_inputs_and_comparable_pairs_answer_differently() -> None:
    """S05 paired state: the same question over incompatible inputs and five matched pairs."""
    comparable = project_speed(timing_receipt())
    mismatched = tuple(
        replace(pair, candidate=replace(pair.candidate, condition_ref="cond-gpu-cold-32steps"))
        for pair in timing_receipt().pairs
    )
    incompatible = project_speed(timing_receipt(pairs=mismatched))
    assert_answered(comparable, TimingState.COMPARED)
    assert_held(incompatible, HoldReason.TIMING_NOT_COMPARABLE)
    assert incompatible.answer.finding == "A speed comparison is unavailable."
    assert "%" not in incompatible.answer.finding and comparable.answer != incompatible.answer


@pytest.mark.parametrize("registered", [1, 2, 4])
def test_fewer_than_five_registered_pairs_never_make_a_speed_claim(registered: int) -> None:
    receipt = timing_receipt(
        plan=(TimingPlan(TimingStage.FIT.value, registered),),
        pairs=pairs(TimingStage.FIT, BASELINE_FIT[:registered], CANDIDATE_FIT[:registered]),
    )
    projection = project_speed(receipt)
    assert_held(projection, HoldReason.INSUFFICIENT_COMPARABLE_PAIRS)
    assert stage_of(projection, TimingStage.FIT).median_ratio is None
    assert sq.MIN_TIMING_PAIRS == 5


def test_a_failed_or_unknown_registered_run_is_retained_and_blocks_the_stage() -> None:
    fit = list(pairs(TimingStage.FIT, BASELINE_FIT, CANDIDATE_FIT))
    failed = replace(fit[2], candidate=replace(fit[2].candidate, status="failed", elapsed_ms=None))
    unknown = replace(
        fit[2], candidate=replace(fit[2].candidate, status="unknown", elapsed_ms=None)
    )
    for broken, reason in (
        (failed, HoldReason.TIMING_ATTEMPT_FAILED),
        (unknown, HoldReason.RESULT_UNKNOWN),
    ):
        receipt = timing_receipt(
            plan=(TimingPlan(TimingStage.FIT.value, 5),),
            pairs=(*fit[:2], broken, *fit[3:]),
        )
        projection = project_speed(receipt)
        assert_held(projection, reason)
        assert stage_of(projection, TimingStage.FIT).pairs == 5


def test_omitted_registered_runs_block_the_stage() -> None:
    receipt = timing_receipt(
        plan=(TimingPlan(TimingStage.FIT.value, 6),),
        pairs=pairs(TimingStage.FIT, BASELINE_FIT, CANDIDATE_FIT),
    )
    projection = project_speed(receipt)
    assert_held(projection, HoldReason.TIMING_ATTEMPTS_OMITTED)
    assert "registered runs are missing" in projection.answer.basis


def test_replay_time_is_never_used_as_fit_time() -> None:
    fit = list(pairs(TimingStage.FIT, BASELINE_FIT, CANDIDATE_FIT))
    as_replay = replace(fit[0], candidate=replace(fit[0].candidate, stage="replay"))
    receipt = timing_receipt(
        plan=(TimingPlan(TimingStage.FIT.value, 5), TimingPlan(TimingStage.REPLAY.value, 1)),
        pairs=(as_replay, *fit[1:]),
    )
    projection = project_speed(receipt)
    assert_held(projection, HoldReason.TIMING_STAGE_MISMATCH)
    assert "Replay time is not used as fit or speed time." in projection.answer.basis
    replay_only = timing_receipt(
        plan=(TimingPlan(TimingStage.REPLAY.value, 5),),
        pairs=pairs(TimingStage.REPLAY, BASELINE_FIT, CANDIDATE_FIT),
    )
    projection = project_speed(replay_only)
    assert_held(projection, HoldReason.RECEIPT_ABSENT)
    assert all(item.stage is not TimingStage.REPLAY for item in projection.facts.stages)


def test_differing_endpoints_are_not_comparable() -> None:
    fit = list(pairs(TimingStage.FIT, BASELINE_FIT, CANDIDATE_FIT))
    shifted = replace(fit[1], candidate=replace(fit[1].candidate, start_event="weights_loaded"))
    projection = project_speed(
        timing_receipt(plan=(TimingPlan("fit", 5),), pairs=(fit[0], shifted, *fit[2:]))
    )
    assert_held(projection, HoldReason.TIMING_NOT_COMPARABLE)


def test_absent_and_stale_timing_receipts_hold() -> None:
    supplied = sq.project_timing(None, baseline_revision=None, candidate_revision=None, as_of=AS_OF)
    assert_held(supplied, HoldReason.RECEIPT_ABSENT)
    assert "no fit interval" in supplied.answer.basis
    absent = project_speed(None)
    assert_held(absent, HoldReason.RECEIPT_ABSENT)
    assert "Replay timing is not fit timing." in absent.answer.basis
    stale = project_speed(timing_receipt(), candidate_revision="recipe-candidate-v3")
    assert_held(stale, HoldReason.RECEIPT_STALE)
    assert stale.facts is None


@pytest.mark.parametrize(
    "broken",
    [
        {"recorded_at": AS_OF + 1},
        {"source_clean": False},
        {"plan": ()},
        {"plan": (TimingPlan("fit", 5), TimingPlan("fit", 5))},
        {"plan": (TimingPlan("warmup", 5),)},
        {"plan": (TimingPlan("end_to_end", 5),)},  # fit pairs would be unregistered
        {"receipt_ref": "has space"},
    ],
)
def test_malformed_timing_receipts_are_invalid(broken: dict[str, Any]) -> None:
    assert_held(project_speed(timing_receipt(**broken)), HoldReason.RECEIPT_INVALID)


def test_duplicate_zero_and_surplus_runs_are_invalid() -> None:
    fit = list(pairs(TimingStage.FIT, BASELINE_FIT, CANDIDATE_FIT))
    duplicate = replace(fit[1], baseline=fit[0].baseline)
    zero = replace(fit[1], candidate=replace(fit[1].candidate, elapsed_ms=0))
    failed_with_time = replace(fit[1], candidate=replace(fit[1].candidate, status="failed"))
    for broken in (duplicate, zero, failed_with_time):
        receipt = timing_receipt(plan=(TimingPlan("fit", 5),), pairs=(fit[0], broken, *fit[2:]))
        assert_held(project_speed(receipt), HoldReason.RECEIPT_INVALID)
    surplus = timing_receipt(plan=(TimingPlan("fit", 4),), pairs=tuple(fit))
    assert_held(project_speed(surplus), HoldReason.RECEIPT_INVALID)


def test_speed_projection_is_deterministic() -> None:
    assert project_speed(timing_receipt()) == project_speed(timing_receipt())


# ----- Training readiness: "Why didn't it train?" · OPE-533 S06, OPE-542 ------------------------


@pytest.fixture
def readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[TrainingReadiness, Source, LocalTrainingStore]:
    """The actual worker readiness producer over a generated reducer, on a fixed clock."""
    monkeypatch.setattr(training_readiness.time, "time", lambda: AS_OF - 60)
    directory = tmp_path / "private"
    directory.mkdir(mode=0o700)
    store = LocalTrainingStore(directory / "training.sqlite3", READER.tenant)
    source = Source(
        ReadinessSnapshot(
            READER.tenant,
            "workflow-1",
            store.engagement_id,
            "evidence-0",
            0,
            0,
            0,
            0,
            (),
            False,
            None,
            True,
        )
    )
    registry = TrainingReadiness(directory, READER.tenant, store.engagement_id, source=source)
    return registry, source, store


def scope_for(store: LocalTrainingStore, source: Source, **changes: Any) -> ReadinessScope:
    assert source.state is not None
    base = ReadinessScope(
        READER.tenant, "workflow-1", store.engagement_id, None, None, source.state.evidence_revision
    )
    return replace(base, **changes)


def refreshed(registry: TrainingReadiness) -> dict[str, Any]:
    receipt = registry.refresh("workflow-1", actor=READER)
    assert receipt is not None
    return receipt


def test_one_root_hold_and_justified_residual_answer_differently(readiness) -> None:
    """S06 paired state: a one-root hold versus a justified residual with a current mandate."""
    registry, source, store = readiness
    source.state = observed(source, 1)
    registry.assessments = Assessment(assessment(source.state))
    one_root = sq.project_training_readiness(
        refreshed(registry), scope=scope_for(store, source), as_of=AS_OF
    )
    assert_answered(one_root, ReadinessState.TOO_EARLY_TO_TRAIN)
    facts = one_root.facts
    assert isinstance(facts, sq.ReadinessFacts)
    assert (facts.verified_roots, facts.independent_families, facts.residual_groups) == (1, 1, 0)
    assert facts.minimum_residual_groups == 3 and facts.verdict == "held"
    assert one_root.answer.finding == "Too early to train."
    assert "1 verified root in 1 independent family" in one_root.answer.basis
    assert "0 residual groups after simple repair · threshold 3" in one_root.answer.basis
    assert "Augmentation is not enabled." in one_root.answer.basis
    assert one_root.next_action == "supply_independent_evidence"

    source.state = observed(source, 3)
    registry.assessments = Assessment(assessment(source.state, residuals=3))
    eligible = sq.project_training_readiness(
        refreshed(registry), scope=scope_for(store, source), as_of=AS_OF
    )
    assert_answered(eligible, ReadinessState.FIT_ELIGIBLE)
    assert eligible.facts.mandate_ref == "mandate-1" and eligible.facts.verdict == "fit_eligible"
    assert eligible.next_action == "request_authorized_fit_admission"
    assert "A fit needs separate, authorized admission." in eligible.answer.basis
    assert "No fit has been admitted." in eligible.answer.finding
    assert one_root.answer != eligible.answer


def test_no_verifier_and_awaiting_outcome_are_distinct(readiness) -> None:
    registry, source, store = readiness
    source.state = observed(source, 0, pending=1)
    unbound = sq.project_training_readiness(
        refreshed(registry), scope=scope_for(store, source), as_of=AS_OF
    )
    assert_answered(unbound, ReadinessState.NO_VERIFIER_BOUND)
    assert unbound.answer.finding == "1 decision awaiting verification · No verifier bound."
    assert unbound.next_action == "bind_verifier"
    source.state = replace(source.state, verifier_bound=True, next_actor="fixture-verifier")
    awaiting = sq.project_training_readiness(
        refreshed(registry), scope=scope_for(store, source), as_of=AS_OF
    )
    assert_answered(awaiting, ReadinessState.AWAITING_OUTCOME)
    assert awaiting.next_action == "verify_outcome" and awaiting.next_owner == "fixture-verifier"


def test_absent_stale_and_foreign_readiness_receipts_hold(readiness) -> None:
    registry, source, store = readiness
    source.state = observed(source, 1)
    registry.assessments = Assessment(assessment(source.state))
    receipt = refreshed(registry)
    absent = sq.project_training_readiness(None, scope=scope_for(store, source), as_of=AS_OF)
    assert_held(absent, HoldReason.RECEIPT_ABSENT)
    assert absent.next_action == "request_readiness_receipt"

    # A reopened outcome moves the evidence revision; the earlier receipt is stale.
    stale = sq.project_training_readiness(
        receipt, scope=scope_for(store, source, evidence_revision="evidence-2-0"), as_of=AS_OF
    )
    assert_held(stale, HoldReason.RECEIPT_STALE)
    assert refs(stale) == {receipt["readiness_ref"]} and stale.facts is None
    assert "Too early" not in text(stale)

    for foreign in (
        {"workflow_ref": "workflow-2"},
        {"engagement": "engagement-other"},
        {"organization": "other-org"},
        {"dataset_ref": "dataset-1"},
    ):
        held = sq.project_training_readiness(
            receipt, scope=scope_for(store, source, **foreign), as_of=AS_OF
        )
        assert_held(held, HoldReason.SCOPE_MISMATCH)
        assert held.receipts == () and held.facts is None
        assert receipt["readiness_ref"] not in text(held)


def test_unrecognized_or_inconsistent_readiness_is_never_eligibility(readiness) -> None:
    registry, source, store = readiness
    source.state = observed(source, 1)
    registry.assessments = Assessment(assessment(source.state))
    receipt = refreshed(registry)
    scope = scope_for(store, source)
    unknown = sq.project_training_readiness(
        {**receipt, "reason_code": "future_reason"}, scope=scope, as_of=AS_OF
    )
    assert_held(unknown, HoldReason.RESULT_UNKNOWN)
    assert "not treated as eligibility" in unknown.answer.basis
    # A too-early receipt rewritten as eligible contradicts its own residual counts.
    promoted = {**receipt, "reason_code": "fit_eligible", "verdict": "fit_eligible"}
    assert_held(
        sq.project_training_readiness(promoted, scope=scope, as_of=AS_OF),
        HoldReason.RECEIPT_INVALID,
    )
    for broken in (
        {"schema_version": "opentour.pilot.training-readiness.v0"},
        {"readiness_ref": "ready-forged"},
        {"recorded_at": AS_OF + 1},
        {"decision_count": receipt["decision_count"] + 1},
        {"verdict": "fit_eligible"},
        {"next_action": "dispatch_fit"},
        {"verified_root_count": True},
        {"reason_code": "no_live_decisions"},
    ):
        assert_held(
            sq.project_training_readiness({**receipt, **broken}, scope=scope, as_of=AS_OF),
            HoldReason.RECEIPT_INVALID,
        )
    missing = dict(receipt)
    del missing["pending_count"]
    assert_held(
        sq.project_training_readiness(missing, scope=scope, as_of=AS_OF),
        HoldReason.RECEIPT_INVALID,
    )


# ----- Model trace: "What did our model see and say?" · OPE-533 S07, K5 -------------------------

TRACE_SCOPE = TraceScope("generated-org", "workflow-a", "epoch-a", "D1", 2)


def committed(**changes: Any) -> CommittedDecision:
    base = CommittedDecision(
        "generated-org",
        "workflow-a",
        "epoch-a",
        "D1",
        2,
        AS_OF - DAY,
        "defer",
        "model-v3",
        None,
        "unavailable",
        "live-page-receipt-7",
    )
    return replace(base, **changes)


def capture(**changes: Any) -> OriginalCapture:
    base = OriginalCapture(
        "capture-11", "D1", 2, "model-v3", "observed", "observed", AS_OF - DAY, True
    )
    return replace(base, **changes)


def trace(decision: CommittedDecision | None, **changes: Any) -> sq.StatusProjection:
    arguments: dict[str, Any] = {"scope": TRACE_SCOPE, "as_of": AS_OF, **changes}
    return sq.project_trace(decision, **arguments)


def test_committed_decision_without_original_versus_observed_capture() -> None:
    """S07 paired state: a permitted committed decision without the original prompt versus an
    authorized independently observed request/response capture."""
    only = trace(committed())
    assert_held(only, HoldReason.ORIGINAL_UNAVAILABLE)
    assert only.state == TraceState.COMMITTED_DECISION_ONLY
    facts = only.facts
    assert isinstance(facts, sq.TraceFacts)
    assert facts.committed_action_ref == "defer" and not facts.model_output_attributed
    assert facts.response_fidelity is Fidelity.UNAVAILABLE
    assert "Original capture is not enabled." in only.answer.basis
    assert "no model output is attributed" in only.answer.basis

    observed_capture = trace(committed(), capture=capture(), capture_enabled=True)
    assert_answered(observed_capture, TraceState.OBSERVED_CAPTURE)
    assert observed_capture.facts.model_output_attributed
    assert observed_capture.facts.request_fidelity is Fidelity.OBSERVED
    assert refs(observed_capture) == {"live-page-receipt-7", "capture-11"}
    assert observed_capture.next_action == "open_original_capture"
    assert only.answer != observed_capture.answer


@pytest.mark.parametrize(
    "changes",
    [
        {"request_fidelity": "reconstructed"},
        {"response_fidelity": "partial"},
        {"response_fidelity": "unavailable"},
        {"independently_observed": False},
    ],
)
def test_reconstructed_partial_or_self_reported_capture_never_attributes_output(
    changes: dict[str, Any],
) -> None:
    projection = trace(committed(), capture=capture(**changes), capture_enabled=True)
    assert_held(projection, HoldReason.ORIGINAL_NOT_OBSERVED)
    assert projection.state == TraceState.PARTIAL_CAPTURE
    assert not projection.facts.model_output_attributed
    assert "were independently observed" not in text(projection)


def test_enabled_capture_without_a_record_is_distinct_from_not_enabled() -> None:
    enabled = trace(
        committed(input_ref="input-ref-3", input_fidelity="observed"), capture_enabled=True
    )
    assert_held(enabled, HoldReason.ORIGINAL_UNAVAILABLE)
    assert "No original capture is recorded." in enabled.answer.basis
    assert "Decision-time input observed" in enabled.answer.basis
    assert enabled.facts.decision_input_fidelity is Fidelity.OBSERVED


def test_later_verification_is_shown_separately_and_must_match_the_revision() -> None:
    verification = LaterVerification("outcome-9", "D1", 2, "final", "approve", AS_OF - 600)
    shown = trace(committed(), capture=capture(), capture_enabled=True, verification=verification)
    assert_answered(shown, TraceState.OBSERVED_CAPTURE)
    assert "Later verification, recorded separately: final as approve" in shown.answer.basis
    assert shown.facts.verification == verification
    mismatched = trace(committed(), verification=replace(verification, decision_revision=1))
    assert_held(mismatched, HoldReason.SCOPE_MISMATCH)
    assert "outcome-9" not in refs(mismatched)


def test_absent_superseded_and_foreign_traces_hold() -> None:
    assert_held(trace(None), HoldReason.RECEIPT_ABSENT)
    stale = trace(committed(decision_revision=1))
    assert_held(stale, HoldReason.RECEIPT_STALE)
    assert "superseded by revision 2" in stale.answer.basis
    for foreign in (
        {"tenant": "other-org"},
        {"workflow_ref": "workflow-b"},
        {"engagement_epoch": "epoch-old"},
        {"decision_id": "D2"},
        {"decision_revision": 3},
    ):
        held = trace(committed(**foreign))
        assert_held(held, HoldReason.SCOPE_MISMATCH)
        assert held.receipts == () and held.facts is None
    for wrong in ({"decision_revision": 1}, {"decision_id": "D2"}, {"model_ref": "model-v2"}):
        held = trace(committed(), capture=capture(**wrong), capture_enabled=True)
        assert_held(held, HoldReason.SCOPE_MISMATCH)
        assert "capture-11" not in refs(held)
    not_enabled = trace(committed(), capture=capture(), capture_enabled=False)
    assert_held(not_enabled, HoldReason.SCOPE_MISMATCH)


@pytest.mark.parametrize(
    "broken",
    [
        {"input_fidelity": "partial"},  # the decision feed has no partial input
        {"input_fidelity": "observed"},  # observed input needs an input reference
        {"input_ref": "input-ref-3"},  # an input reference contradicts unavailable
        {"decided_at": AS_OF + 1},
        {"decision_revision": 0},
        {"action_ref": ""},
    ],
)
def test_malformed_decision_traces_are_invalid(broken: dict[str, Any]) -> None:
    assert_held(trace(committed(**broken)), HoldReason.RECEIPT_INVALID)


def test_decision_input_fidelity_matches_the_live_decision_feed(tmp_path: Path) -> None:
    """The projection accepts exactly the input fidelities that the live reducer admits."""
    for index, fidelity in enumerate(("observed", "reconstructed", "unavailable", "partial")):
        case = Case(tmp_path / str(index))
        reference = None if fidelity == "unavailable" else "input-ref-1"
        case.ingest(live_decision(input_fidelity=fidelity, input_ref=reference))
        admitted = case.snapshot().decisions == 1
        projected = trace(
            committed(decision_revision=1, input_fidelity=fidelity, input_ref=reference),
            scope=replace(TRACE_SCOPE, current_revision=1),
        )
        assert admitted == (projected.hold_reason is not HoldReason.RECEIPT_INVALID), fidelity


# ----- Delivery: "Did it ship?" · OPE-533 S14 ----------------------------------------------------

DESTINATION = "destination-ops-1"
APPROVED = ApprovalEvidence("approved", "approval-receipt-5", AS_OF - 7200)


def outbox(stage: str, **changes: Any) -> ReleaseStatus:
    delivered = stage == "delivered"
    base = ReleaseStatus(
        "release-0f00",
        DESTINATION,
        stage,
        AS_OF - 3600 if stage != "queued" else None,
        AS_OF - 60,
        "receiver-receipt-3" if delivered else None,
        AS_OF - 120 if delivered else None,
    )
    return replace(base, **changes)


def ship(**changes: Any) -> sq.StatusProjection:
    arguments: dict[str, Any] = {
        "destination_ref": DESTINATION,
        "approval": APPROVED,
        "as_of": AS_OF,
        **changes,
    }
    return sq.project_delivery(**arguments)


def test_no_release_unknown_write_and_confirmed_delivery_are_distinct() -> None:
    """S14 paired state: no release, a pending or unknown earlier write, confirmed delivery."""
    no_release = ship(destination_ref=None)
    unknown = ship(release=outbox("delivery_unconfirmed"))
    delivered = ship(release=outbox("delivered"), approved_at=AS_OF - 7200)
    assert_held(no_release, HoldReason.NO_DESTINATION)
    assert no_release.state == DeliveryState.NO_RELEASE
    assert_held(unknown, HoldReason.RESULT_UNKNOWN)
    assert unknown.state == DeliveryState.UNCONFIRMED
    assert "An unknown receiver result is not delivery." in unknown.answer.basis
    assert_answered(delivered, DeliveryState.DELIVERED)
    assert delivered.facts.receiver_receipt_ref == "receiver-receipt-3"
    assert refs(delivered) == {"approval-receipt-5", "release-0f00", "receiver-receipt-3"}
    assert "Delivery is not recipient adoption." in delivered.answer.basis
    assert len({no_release.state, unknown.state, delivered.state}) == 3


def test_removed_destination_discloses_no_earlier_status() -> None:
    held = ship(destination_ref=None, release=outbox("delivered"))
    assert held.receipts == () and held.facts == sq.DeliveryFacts(None)
    assert "approval-receipt-5" not in text(held) and "release-0f00" not in text(held)


@pytest.mark.parametrize(
    "stage,reason,state",
    [
        ("staged", HoldReason.APPROVAL_PENDING, DeliveryState.APPROVAL_PENDING),
        ("accepted", HoldReason.APPROVAL_PENDING, DeliveryState.APPROVAL_PENDING),
        ("failed", HoldReason.APPROVAL_REQUEST_FAILED, DeliveryState.APPROVAL_PENDING),
        ("uncertain", HoldReason.RESULT_UNKNOWN, DeliveryState.UNCONFIRMED),
        ("approval_unresolved", HoldReason.RESULT_UNKNOWN, DeliveryState.UNCONFIRMED),
        ("expired", HoldReason.APPROVAL_EXPIRED, DeliveryState.UNCONFIRMED),
        ("vote_maybe", HoldReason.RESULT_UNKNOWN, DeliveryState.UNCONFIRMED),
    ],
)
def test_provider_transport_never_attests_handoff(stage: str, reason: HoldReason, state) -> None:
    projection = ship(
        approval=ApprovalEvidence(stage, "request-77", AS_OF - 60), release=outbox("delivered")
    )
    assert_held(projection, reason)
    assert projection.state == state and "receiver-receipt-3" not in text(projection)
    assert refs(projection) == {"request-77"}


@pytest.mark.parametrize(
    "stage", ["changes_requested", "release_changes_requested", "readout_changes_requested"]
)
def test_declined_revision_answers_not_released(stage: str) -> None:
    projection = ship(approval=ApprovalEvidence(stage, "decision-4", AS_OF - 60))
    assert_answered(projection, DeliveryState.NOT_RELEASED)
    assert projection.answer.finding.startswith("Not released.")
    assert projection.next_action == "address_review_decision"


def test_approved_but_queued_or_missing_outbox_holds() -> None:
    assert_held(ship(approval=None), HoldReason.RECEIPT_ABSENT)
    missing = ship()
    assert_held(missing, HoldReason.RECEIPT_ABSENT)
    assert missing.state == DeliveryState.UNCONFIRMED
    assert missing.answer.finding == "Approval recorded. Handoff delivery is unconfirmed."
    queued = ship(release=outbox("queued"))
    assert_held(queued, HoldReason.AWAITING_DELIVERY)
    assert queued.state == DeliveryState.QUEUED


def test_foreign_release_or_destination_holds_without_disclosing_it() -> None:
    for held in (
        ship(release=outbox("delivered", destination_ref="destination-other")),
        ship(release=outbox("delivered"), expected_release_ref="release-1f11"),
    ):
        assert_held(held, HoldReason.SCOPE_MISMATCH)
        assert refs(held) == {"approval-receipt-5"} and held.facts.release_ref is None
        assert "receiver-receipt-3" not in text(held)


@pytest.mark.parametrize(
    "changes",
    [
        {"receiver_receipt_ref": None},
        {"received_at": None},
        {"received_at": AS_OF - 30},  # after the last check
        {"last_checked_at": AS_OF + 1},
        {"received_at": AS_OF - 8000},  # before the approval
        {"reference": "release with space"},
    ],
)
def test_delivered_stage_needs_a_consistent_receiver_receipt(changes: dict[str, Any]) -> None:
    projection = ship(release=outbox("delivered", **changes), approved_at=AS_OF - 7200)
    assert_held(projection, HoldReason.RECEIPT_INVALID)
    assert projection.state != DeliveryState.DELIVERED


def test_malformed_approval_receipt_is_invalid() -> None:
    for approval in (
        ApprovalEvidence("approved", "", AS_OF),
        ApprovalEvidence("approved", "approval-1", AS_OF + 1),
        ApprovalEvidence(7, "approval-1", AS_OF),
    ):
        assert_held(ship(approval=approval), HoldReason.RECEIPT_INVALID)


# ----- Weekly live evidence: "No errors this week?" · OPE-533 S16 -------------------------------

WEEK_START = AS_OF - 3 * DAY
WEEK_END = WEEK_START + 7 * DAY
WEEK = LiveWindowScope("workflow-a", "epoch-a", "dataset-a", "evidence-9", WEEK_START, WEEK_END)


def window(**changes: Any) -> LiveWindowReceipt:
    base = LiveWindowReceipt(
        "window-receipt-2",
        "workflow-a",
        "epoch-a",
        "dataset-a",
        "evidence-9",
        WEEK_START,
        WEEK_END,
        AS_OF - 1800,
        AS_OF - 1800,
        True,
        AS_OF - 900,
        6,
        4,
        2,
        0,
        3,
        1,
        AS_OF - 600,
    )
    return replace(base, **changes)


def week(receipt: LiveWindowReceipt | None, **changes: Any) -> sq.StatusProjection:
    arguments: dict[str, Any] = {
        "scope": WEEK,
        "decision_feed_bound": True,
        "verifier_bound": True,
        "as_of": AS_OF,
        **changes,
    }
    return sq.project_live_window(receipt, **arguments)


def test_quiet_window_verified_window_and_no_verified_evidence_are_distinct() -> None:
    quiet = week(
        window(decisions=0, verified=0, pending=0, verified_automatic=0, wrong_automatic=0)
    )
    assert_answered(quiet, LiveWindowState.QUIET)
    assert "A quiet window is not an accuracy result." in quiet.answer.basis

    verified = week(window())
    assert_answered(verified, LiveWindowState.VERIFIED)
    assert verified.answer.finding == (
        "1 of 3 verified automatic decisions were wrong in the covered window."
    )
    assert "2 pending decisions not counted as correct." in verified.answer.basis
    facts = verified.facts
    assert isinstance(facts, sq.LiveWindowFacts)
    assert (facts.wrong_automatic, facts.verified_automatic, facts.pending) == (1, 3, 2)
    assert facts.last_synced_at == AS_OF - 900 and facts.covered_through == AS_OF - 1800

    pending = window(decisions=2, verified=0, pending=2, verified_automatic=0, wrong_automatic=0)
    awaiting = week(pending)
    unbound = week(pending, verifier_bound=False, can_bind_verifier=True)
    asks = week(pending, verifier_bound=False)
    assert_held(awaiting, HoldReason.AWAITING_OUTCOME)
    assert_held(unbound, HoldReason.VERIFIER_NOT_BOUND)
    assert awaiting.state == unbound.state == LiveWindowState.NO_VERIFIED_EVIDENCE
    assert awaiting.next_action == "verify_outcome" and unbound.next_action == "bind_verifier"
    assert asks.next_action == "ask_data_custodian_to_bind_verifier"
    assert "Errors are unknown." in awaiting.answer.finding
    assert "100%" not in text(awaiting) + text(quiet) + text(verified)
    assert len({quiet.state, verified.state, awaiting.state}) == 3


def test_disputed_only_and_no_verified_automatic_hold() -> None:
    disputed = week(
        window(
            decisions=2, verified=0, pending=0, disputed=2, verified_automatic=0, wrong_automatic=0
        )
    )
    assert_held(disputed, HoldReason.DISPUTED_OUTCOMES)
    reviewed = week(window(verified_automatic=0, wrong_automatic=0))
    assert_held(reviewed, HoldReason.NO_VERIFIED_AUTOMATIC)
    assert "no denominator" in reviewed.answer.basis


@pytest.mark.parametrize(
    "changes",
    [
        {"decision_coverage_through": AS_OF - 2 * DAY},
        {"last_synced_at": AS_OF - 2 * DAY},
        {"decision_coverage_through": WEEK_START},  # covers no part of the window
    ],
)
def test_stale_decision_feed_holds_with_actual_window(changes: dict[str, Any]) -> None:
    projection = week(window(**changes))
    assert_held(projection, HoldReason.FEED_STALE)
    assert projection.state == LiveWindowState.STALE_FEED
    assert "Last successful sync" in projection.answer.basis
    assert projection.next_action == "check_feed_sync"


def test_stale_outcome_feed_holds_only_when_decisions_need_verification() -> None:
    stale_outcomes = window(outcome_coverage_through=AS_OF - 3 * DAY + 1)
    assert_held(week(stale_outcomes), HoldReason.FEED_STALE)
    quiet = replace(
        stale_outcomes,
        decisions=0,
        verified=0,
        pending=0,
        verified_automatic=0,
        wrong_automatic=0,
    )
    assert_answered(week(quiet), LiveWindowState.QUIET)
    assert_held(week(window(outcome_coverage_through=None)), HoldReason.FEED_STALE)


def test_partial_coverage_and_no_feed_hold() -> None:
    assert_held(week(window(complete=False)), HoldReason.COVERAGE_INCOMPLETE)
    no_feed = week(window(), decision_feed_bound=False)
    assert_held(no_feed, HoldReason.NO_FEED)
    assert no_feed.state == LiveWindowState.NO_FEED and no_feed.receipts == ()


def test_absent_window_receipt_holds_and_reports_the_actual_source_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The latest acknowledged page from the live evidence producer cannot answer the week."""
    monkeypatch.setattr(live_evidence.time, "time", lambda: AS_OF - 300)
    case = Case(tmp_path / "owner")
    metadata = {
        "interval_start": WEEK_START,
        "interval_end": AS_OF - 1200,
        "coverage_through": AS_OF - 1200,
        "complete": True,
        "next_cursor": None,
    }
    case.store.ingest_page(
        LIVE_ACTOR,
        case.decisions,
        page_ref="page-week",
        source_revision="source-v1",
        events=(),
        source_positions=(),
        event_epochs=(),
        checkpoint=10,
        read_metadata=metadata,
    )
    (read,) = case.store.read_receipts(LIVE_ACTOR, "workflow-a")
    assert read["polled_at"] == AS_OF - 300
    fresh = week(None, latest_read=read)
    assert_held(fresh, HoldReason.RECEIPT_ABSENT)
    assert "Totals across all observed dates are not a weekly result." in fresh.answer.basis
    assert "Last successful sync 2027-01-15 07:55 UTC." in fresh.answer.basis
    assert refs(fresh) == {read["receipt_ref"]}
    assert fresh.facts.covered_through == AS_OF - 1200

    # A completed page with unknown covered time never establishes a current or quiet week.
    empty = {**read, "interval_start": None, "interval_end": None, "coverage_through": None}
    never_quiet = week(None, latest_read=empty)
    assert_held(never_quiet, HoldReason.FEED_STALE)
    assert "Covered time is unknown" in never_quiet.answer.basis
    behind = {
        **read,
        "interval_end": AS_OF - 2 * DAY,
        "coverage_through": AS_OF - 2 * DAY,
    }
    assert_held(week(None, latest_read=behind), HoldReason.FEED_STALE)
    old_poll = {**read, "polled_at": AS_OF - 2 * DAY}
    assert_held(week(None, latest_read=old_poll), HoldReason.FEED_STALE)
    assert_held(week(None, latest_read={"receipt_ref": "live-x"}), HoldReason.RECEIPT_INVALID)
    future = {**read, "polled_at": AS_OF + 1}
    assert_held(week(None, latest_read=future), HoldReason.RECEIPT_INVALID)


def test_absent_window_receipt_without_a_verifier_asks_for_one() -> None:
    held = week(None, verifier_bound=False, can_bind_verifier=True)
    assert_held(held, HoldReason.RECEIPT_ABSENT)
    assert held.next_action == "bind_verifier"
    assert week(None).next_action == "refresh_window_coverage"


def test_foreign_and_stale_window_receipts_hold() -> None:
    for foreign in (
        {"engagement_epoch": "epoch-old"},
        {"workflow_ref": "workflow-b"},
        {"dataset_ref": "dataset-b"},
        {"window_start": WEEK_START - DAY},
    ):
        held = week(window(**foreign))
        assert_held(held, HoldReason.SCOPE_MISMATCH)
        assert held.receipts == () and "window-receipt-2" not in text(held)
    stale = week(window(evidence_revision="evidence-8"))
    assert_held(stale, HoldReason.RECEIPT_STALE)
    assert refs(stale) == {"window-receipt-2"}


@pytest.mark.parametrize(
    "broken",
    [
        {"decisions": 7},
        {"wrong_automatic": 4},
        {"verified_automatic": 5},
        {"last_synced_at": AS_OF + 1},
        {"recorded_at": AS_OF + 1},
        {"window_end": WEEK_START},
        {"complete": 1},
        {"pending": -1},
    ],
)
def test_malformed_window_receipts_are_invalid(broken: dict[str, Any]) -> None:
    scope = WEEK if "window_end" not in broken else replace(WEEK, window_end=WEEK_START + 1)
    assert_held(week(window(**broken), scope=scope), HoldReason.RECEIPT_INVALID)


def test_window_scope_errors_are_programming_errors() -> None:
    for scope in (replace(WEEK, window_start=AS_OF + 1), replace(WEEK, workflow_ref="")):
        with pytest.raises(ValueError):
            week(None, scope=scope)
    with pytest.raises(ValueError):
        week(None, max_feed_lag=0)


# ----- The five #87 probe questions, backend half ---------------------------------------------


@pytest.mark.parametrize(
    "prompt,topic,concepts",
    [
        (
            "How much faster is it?",
            StatusTopic.TIMING,
            [("timing", "speed", "time measurement"), ("unavailable", "unmeasured")],
        ),
        ("Why didn't it train?", StatusTopic.TRAINING, [("training", "fit"), ("unavailable",)]),
        (
            "What did our model see and say?",
            StatusTopic.TRACE,
            [("model input", "input capture", "decision-time"), ("output", "unavailable")],
        ),
        (
            "Did it ship?",
            StatusTopic.DELIVERY,
            [("delivery", "handoff"), ("unconfirmed", "no release")],
        ),
        (
            "No errors this week?",
            StatusTopic.LIVE_ERRORS,
            [("verified", "live evidence"), ("unavailable", "no live", "no verified")],
        ),
    ],
)
def test_probe_questions_resolve_to_a_typed_hold_with_a_reason(
    prompt: str, topic: StatusTopic, concepts: list[tuple[str, ...]]
) -> None:
    """The #87 tenant has no timing pairs, readiness, capture, approval or window coverage."""
    assert sq.resolve_status(prompt) is topic
    projection = {
        StatusTopic.TIMING: lambda: sq.project_timing(
            None, baseline_revision=None, candidate_revision=None, as_of=AS_OF
        ),
        StatusTopic.TRAINING: lambda: sq.project_training_readiness(
            None,
            scope=ReadinessScope("generated-org", "workflow-a", "epoch-a", None, None, "e1"),
            as_of=AS_OF,
        ),
        StatusTopic.TRACE: lambda: sq.project_trace(None, scope=TRACE_SCOPE, as_of=AS_OF),
        StatusTopic.DELIVERY: lambda: sq.project_delivery(
            destination_ref=DESTINATION, approval=None, as_of=AS_OF
        ),
        StatusTopic.LIVE_ERRORS: lambda: week(None),
    }[topic]()
    assert projection.topic is topic and projection.held and projection.hold_reason
    answer = text(projection).lower()
    assert all(any(term in answer for term in group) for group in concepts), answer
    assert "approve" not in projection.next_action and "fit" not in projection.next_action
