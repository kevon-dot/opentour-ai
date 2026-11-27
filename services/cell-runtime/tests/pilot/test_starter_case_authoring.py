"""Normal deterministic provider, actual map/owner admission, no inference fixture."""

from dataclasses import replace

import pytest
from opentour_cell.pilot.guided_decision_map import Condition
from opentour_cell.pilot.intake_mcp import IntakeError
from opentour_cell.pilot.starter_case_authoring import guided_case_provider

pytest_plugins = ("test_starter_setup", "test_starter_map_confirmation")


def test_normal_guided_provider_proposes_then_owner_explicitly_freezes(joined):
    _, session, _, _, _ = joined
    provider = guided_case_provider()
    reference = session.propose(provider)
    proposed = session.review()
    assert len(proposed["cases"]) == 6
    assert len({case["family_id"] for case in proposed["cases"]}) == 3
    assert all(case["owner_adjudicated_target"] is None for case in proposed["cases"])
    assert all(case["case_origin"] == "generated_starter" for case in proposed["cases"])
    assert {case["suggestion"] for case in proposed["cases"]} == {"APPROVE", "MANUAL", "REVIEW"}
    with pytest.raises(IntakeError, match="owner_adjudication_required"):
        session.freeze(
            splits={case["family_id"]: "reserved" for case in proposed["cases"]},
            expected_revision=0,
        )
    for revision, case in enumerate(proposed["cases"]):
        session.adjudicate(
            case_id=case["case_id"],
            target=case["suggestion"],
            reason="Generated owner verifies the cited policy independently",
            expected_revision=revision,
            operation_ref=f"accept-guided-{revision}",
        )
    frozen = session.freeze(
        splits={case["family_id"]: "reserved" for case in proposed["cases"]}, expected_revision=6
    )
    assert frozen.proposed == frozen.resolved == 6
    assert frozen.reserved == 3 and frozen.uncovered_critical == ()
    assert all(count == 1 for _, _, count in frozen.coverage)
    assert session.propose(guided_case_provider()) == reference


def test_branch_variants_never_multiply_independent_families(joined):
    _, session, _, maps, _ = joined
    snapshot = maps.preview(session.actor)
    first = guided_case_provider().propose(snapshot)
    repeated = guided_case_provider().propose(snapshot)
    assert first == repeated
    by_branch = {}
    for case in first:
        by_branch.setdefault(case.branch_id, set()).add(case.family_id)
        assert case.template_id == case.family_id
    assert len(by_branch) == 3 and all(len(families) == 1 for families in by_branch.values())
    assert len({case.decision_inputs for case in first}) == len(first)


@pytest.mark.parametrize("change", ["draft", "unresolved", "missing-trigger", "schema"])
def test_unavailable_map_never_produces_claimed_cases(joined, change):
    _, session, _, maps, _ = joined
    snapshot = maps.preview(session.actor)
    if change == "draft":
        snapshot = replace(snapshot, confirmed_ref=None)
    elif change == "unresolved":
        snapshot = replace(snapshot, question="Owner clarification required")
    elif change == "missing-trigger":
        snapshot = replace(snapshot, settings_trigger=None)
    else:
        snapshot = replace(snapshot, field_kinds={})
    with pytest.raises(IntakeError):
        guided_case_provider().propose(snapshot)


def test_unreachable_branch_or_fallback_is_not_silently_covered(joined):
    _, session, _, maps, _ = joined
    snapshot = maps.preview(session.actor)
    last = snapshot.branches[-1]
    unreachable = replace(
        last,
        branch_ref="generated-unreachable",
        action="MANUAL",
        citation_ref="generated-unreachable-citation",
        conditions=(Condition("total", "<", "250"),),
        precedence=100,
    )
    with pytest.raises(IntakeError, match="guided_authoring_uncovered_branch"):
        guided_case_provider().propose(
            replace(snapshot, branches=(*snapshot.branches, unreachable))
        )
    only_approve = next(row for row in snapshot.branches if row.action == "APPROVE")
    with pytest.raises(IntakeError, match="guided_authoring_uncovered_input"):
        guided_case_provider().propose(replace(snapshot, branches=(only_approve,)))


def test_large_profile_holds_instead_of_truncating_material_inputs(joined):
    _, session, _, maps, _ = joined
    snapshot = maps.preview(session.actor)
    branch = snapshot.branches[0]
    fields = {f"reference_{i}": "reference" for i in range(8)}
    conditions = tuple(Condition(name, "present") for name in fields)
    revised = replace(
        snapshot, branches=(replace(branch, conditions=conditions),), field_kinds=fields
    )
    with pytest.raises(IntakeError, match="guided_authoring_profile_exceeded"):
        guided_case_provider().propose(revised)
