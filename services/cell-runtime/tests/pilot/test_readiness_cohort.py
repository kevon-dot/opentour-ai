"""Recount the generated readiness and person-time cohort independently."""

import time
from dataclasses import replace
from fractions import Fraction

import pytest
from opentour_cell.pilot.readiness_cohort import (
    CohortWorkflow,
    ExpertInterval,
    LabelCheckpoint,
    ReadinessTransition,
    compare_checkpoints,
    project_cohort,
    verified_person_seconds,
)
from opentour_cell.pilot.training_readiness import TrainingReadiness
from test_live_readiness import ACTOR, Case, decision, outcome


def workflow(ref, seed, *, policy_only=False):
    return CohortWorkflow(
        "generated-tenant",
        ref,
        "epoch-a",
        seed,
        None if seed is None else f"verified-{ref}",
        policy_only,
    )


def transition(member, revision, when, disposition, *, observed_at=None):
    return ReadinessTransition(
        member.scope,
        f"{member.workflow}-ready-{revision}",
        revision,
        when,
        when if observed_at is None else observed_at,
        disposition,
    )


def test_cohort_has_five_seeded_workflows_and_tracks_reopened_state_as_of_cutoff():
    first, second, simple, waiting_a, waiting_b = (
        workflow("fast", 0),
        workflow("slow", 0),
        workflow("simple", 0),
        workflow("held-a", 1800),
        workflow("held-b", 3600),
    )
    audience = (
        first,
        second,
        simple,
        waiting_a,
        waiting_b,
        workflow("policy-only", None, policy_only=True),
    )
    events = [
        transition(first, 2, 12_000, "held", observed_at=15_000),
        transition(second, 1, 10_800, "fit_ready"),
        transition(simple, 1, 300, "simple_resolved"),
        transition(first, 1, 3600, "fit_ready"),
        transition(waiting_a, 1, 2000, "held"),
        transition(waiting_b, 1, 4500, "held"),
    ]

    def report(members, observations, cutoff):
        return project_cohort(
            members, observations, window_start=0, window_end=5400, cutoff=cutoff, may_disclose=True
        )

    first_view = report(audience, events + [events[1]], 14_400)
    assert (first_view.ever_fit_ready, first_view.workflows) == (2, 5)
    assert dict(first_view.current) == {"fit_ready": 2, "held": 2, "simple_resolved": 1}
    assert first_view.policy_only == 1 and first_view.unknown_seed == 0
    assert first_view.median_seconds_to_ready == 2 * 3600
    assert first_view.pending_ages_seconds == (10_800, 12_600)

    rebuilt = report(tuple(reversed(audience)), list(reversed(events)), 14_400)
    assert rebuilt == first_view and rebuilt.revision == first_view.revision
    reopened = report(audience, events, 16_000)
    assert reopened.ever_fit_ready == 2
    assert dict(reopened.current) == {"fit_ready": 1, "held": 3, "simple_resolved": 1}
    assert reopened.revision != first_view.revision


def test_unknown_seed_denied_scope_conflicting_receipts_and_boundary_hold():
    seed = workflow("valid", 5)
    settings = {"window_start": 0, "window_end": 10, "cutoff": 20, "may_disclose": True}
    empty = project_cohort((workflow("unknown", None), workflow("next-window", 10)), (), **settings)
    assert (empty.workflows, empty.unknown_seed, empty.median_seconds_to_ready) == (0, 1, None)
    disputed = project_cohort((seed,), (transition(seed, 1, 8, "disputed"),), **settings)
    assert dict(disputed.current) == {"disputed": 1} and disputed.pending_ages_seconds == (15,)
    assert project_cohort(None, None, **(settings | {"may_disclose": False})) is None

    current = transition(seed, 1, 8, "fit_ready")
    foreign = replace(current, scope=("outside", seed.workflow, seed.engagement))
    for receipts in ((foreign,), (current, replace(current, disposition="held"))):
        with pytest.raises(ValueError, match="readiness_cohort_evidence_unavailable"):
            project_cohort((seed,), receipts, **settings)


def test_rebuild_includes_actual_worker_holds_before_the_first_verified_seed(tmp_path, monkeypatch):
    case = Case(tmp_path / "private")
    start = int(time.time())
    now = [start]
    monkeypatch.setattr(time, "time", lambda: now[0])
    empty = case.ready.refresh("workflow", actor=ACTOR)
    assert empty["reason_code"] == "no_live_decisions"
    now[0] += 1
    case.ingest(decision())
    pending = case.ready.refresh("workflow", actor=ACTOR)
    assert pending["reason_code"] == "no_verifier_bound"
    now[0] += 1
    case.bind_verifier()
    case.ingest(outcome(), truth=True)
    seeded = case.refresh()
    assert seeded["verified_root_count"] == 1

    reopened = TrainingReadiness(
        case.ready.directory,
        case.ready.organization,
        case.ready.engagement,
        source=case.ready.source,
        assessments=case.ready.assessments,
    )
    history = reopened.history("workflow", actor=ACTOR)
    assert [row["readiness_ref"] for row in history] == [
        empty["readiness_ref"],
        pending["readiness_ref"],
        seeded["readiness_ref"],
    ]
    assert reopened.read("workflow", actor=ACTOR) == seeded
    member = CohortWorkflow(
        ACTOR.tenant, "workflow", case.ready.engagement, now[0], seeded["readiness_ref"]
    )
    observations = tuple(
        ReadinessTransition(
            member.scope,
            row["readiness_ref"],
            row["version"],
            row["recorded_at"],
            row["recorded_at"],
            row["verdict"],
        )
        for row in (empty, pending, seeded)
    )
    settings = {
        "window_start": start,
        "window_end": start + 3,
        "cutoff": start + 4,
        "may_disclose": True,
    }
    report = project_cohort((member,), observations, **settings)
    assert (report.workflows, report.ever_fit_ready, dict(report.current)) == (1, 0, {"held": 1})
    assert (
        report.revision
        == project_cohort((member,), tuple(reversed(observations)), **settings).revision
    )
    assert dict(project_cohort((member,), observations[:2], **settings).current) == {"pending": 1}
    impossible = replace(observations[0], disposition="fit_ready")
    with pytest.raises(ValueError, match="readiness_cohort_evidence_unavailable"):
        project_cohort((member,), (impossible, *observations[1:]), **settings)


def test_expert_time_is_person_time_and_shared_effort_counts_once():
    shared = ExpertInterval("one", "shared", "adjudicate-real", 0, 3600, None)
    overlapping = ExpertInterval("one", "base", "real-review", 1800, 3600, "real")
    separate = ExpertInterval("two", "aug", "validate-synthetic", 900, 4500, "augmented")
    receipts = [shared, overlapping, shared, separate]
    assert verified_person_seconds(receipts) == 7200
    assert verified_person_seconds(receipts, arm="real") == 3600
    assert verified_person_seconds(receipts, arm="augmented") == 7200
    assert verified_person_seconds(()) is None
    with pytest.raises(ValueError, match="readiness_cohort_evidence_unavailable"):
        verified_person_seconds([shared, replace(shared, finished_at=7200)])


def test_matched_label_savings_reports_extra_measured_expert_burden():
    real = LabelCheckpoint("qualified-target", "paired-scope", True, 100, 120 * 60)
    augmented = replace(real, verified_labels=40, expert_person_seconds=140 * 60)
    comparison = compare_checkpoints(real, augmented)
    assert comparison.label_reduction == Fraction(3, 5)
    assert comparison.expert_person_seconds_saved == -20 * 60
    assert compare_checkpoints(real, replace(augmented, target_confirmed=False)) is None
    assert compare_checkpoints(real, replace(augmented, comparison_scope="other-data")) is None
    assert compare_checkpoints(replace(real, verified_labels=0), augmented) is None
    missing_effort = compare_checkpoints(real, replace(augmented, expert_person_seconds=None))
    assert missing_effort.label_reduction == Fraction(3, 5)
    assert missing_effort.expert_person_seconds_saved is None
