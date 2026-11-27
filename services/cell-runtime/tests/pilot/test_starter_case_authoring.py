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


def test_generic_categories_are_bounded_witnesses_of_material_branches(joined):
    _, session, _, maps, _ = joined
    snapshot = maps.preview(session.actor)
    missing, exceptional, other = snapshot.branches
    snapshot = replace(
        snapshot,
        field_kinds={"class_ref": "reference"},
        branches=(
            replace(missing, conditions=(Condition("class_ref", "missing"),), precedence=1),
            replace(
                exceptional,
                conditions=(Condition("class_ref", "==", "generated-present"),),
                precedence=2,
            ),
            replace(other, conditions=(Condition("class_ref", "present"),), precedence=3),
        ),
    )
    generated = guided_case_provider().propose(snapshot)
    assert {dict(c.decision_inputs)["class_ref"] for c in generated} == {
        None,
        "generated-present",
        "generated-present-0",
    }
    for case in generated:
        expected, citations = snapshot.expected_action(dict(case.decision_inputs))
        assert (case.suggestion, case.citation) == (expected, citations[0])
    assert len({c.family_id for c in generated}) == 3
    uncovered = replace(snapshot, branches=(snapshot.branches[1],))
    with pytest.raises(IntakeError, match="guided_authoring_uncovered_input"):
        guided_case_provider().propose(uncovered)


# Deterministic provider for the P1 test fixture. The normal provider remains generic.
P1_POLICY = b"""# Invoice coding
## Subscriptions
If category equals "annual_software_subscription" and po is present, SOFTWARE.
## Carrier charges
If category equals "carrier_freight" and po is present, FREIGHT.
## Office supplies
If category equals "office_supplies" and po is present, SUPPLIES.
## Utility and tax payments
If category equals "utility_service" and po is present, UTILITIES.
If category equals "tax_remittance" and po is present, TAX.
## Missing purchase order
If po is missing, REVIEW.
"""

# The harness's owner oracle is independent of the provider's draft answers.
P1_OWNER_LABELS = {
    "subscription": "SOFTWARE",
    "freight": "FREIGHT",
    "supplies": "SUPPLIES",
    "utilities": "UTILITIES",
    "tax": "TAX",
    "no-po": "REVIEW",
    "mixed": None,
}


class SevenInvoiceFamilies:
    def propose(self, snapshot):
        from opentour_cell.pilot.starter_cases import ProposedCase

        examples = (
            ("subscription", "annual_software_subscription", "po-a", "SOFTWARE", "SOFTWARE"),
            ("freight", "carrier_freight", "po-b", "FREIGHT", "SUPPLIES"),
            ("supplies", "office_supplies", "po-c", "SUPPLIES", "SUPPLIES"),
            ("utilities", "utility_service", "po-d", "UTILITIES", "UTILITIES"),
            ("tax", "tax_remittance", "po-e", "TAX", "TAX"),
            ("no-po", "annual_software_subscription", None, "REVIEW", "REVIEW"),
            ("mixed", "mixed_invoice", "po-f", "SOFTWARE", "REVIEW"),
        )
        cases = []
        for name, category, po, cited_action, suggestion in examples:
            (branch,) = (r for r in snapshot.branches if r.action == cited_action and r.confirmed)
            fields = (("category", category), ("po", po))
            if name == "mixed":
                assert snapshot.expected_action(dict(fields))[0] is None
                rationale = (
                    "Generated counterexample to the single-category rule. "
                    "The policy is silent on mixed invoices. REVIEW is an unverified suggestion."
                )
            else:
                rationale = "Generated draft for owner verification against the cited invoice rule."
            cases.append(
                ProposedCase(
                    name,
                    "family-" + name,
                    "template-" + name,
                    branch.branch_ref,
                    fields,
                    suggestion,
                    rationale,
                    branch.citation_ref,
                )
            )
        return tuple(cases)


@pytest.fixture
def p1_session(founder):
    import hashlib
    import json
    import time
    from dataclasses import asdict

    from opentour_cell.pilot.guided_policy import GuidedPolicySource
    from opentour_cell.pilot.intake_mcp import IntakePrincipal
    from opentour_cell.pilot.map_review import BranchEdit, MapSettingsEdit
    from opentour_cell.pilot.slack_adapter import Settings
    from opentour_cell.pilot.starter_cases import FrozenPlan
    from opentour_cell.pilot.starter_map_confirmation import GRANT, SELECTION, StarterCaseSession
    from opentour_cell.pilot.starter_setup import StarterSetup
    from test_intake_mcp import _sealed_starter, _write_private
    from test_map_review import stage_map_review
    from test_slack_adapter import ACTOR
    from test_starter_map_confirmation import sign_case

    home = founder.path.parent
    selected = json.loads((home / "selected.json").read_bytes())
    policy_file = home / "policy.md"
    policy_file.write_bytes(P1_POLICY)
    policy_file.chmod(0o600)
    grant_path = home / "starter-intake-grant-policy-scope.json"
    template = json.loads(grant_path.read_bytes())["grant"]
    _write_private(
        grant_path,
        _sealed_starter(
            selected,
            source_revision=hashlib.sha256(P1_POLICY).hexdigest(),
            setup_policy_revision=template["setup_policy_revision"],
        ),
    )

    def settings():
        return Settings.load(founder.path)

    actor = IntakePrincipal(founder.store.organization, ACTOR, "ignored-wire-role")
    setup = StarterSetup(settings, ACTOR)
    draft = setup.save(setup.preview().revision, "admit-p1").draft
    maps, _ = stage_map_review(
        GuidedPolicySource(setup, policy_file),
        draft["workflow_ref"],
        editors={ACTOR: "workflow-owner"},
        field_kinds={"category": "reference", "po": "reference"},
        actions=sorted(set(P1_OWNER_LABELS.values()) - {None}),
    )
    snapshot = maps.preview(actor)
    confirmed = maps.save(
        actor,
        expected_revision=snapshot.review_revision,
        operation_ref="confirm-p1",
        changes=tuple(
            BranchEdit(b.branch_ref, "accepted", "Checked cited P1 passage")
            for b in snapshot.branches
        ),
        settings=MapSettingsEdit(
            "Invoice arrives", None, "P1 leaves mixed-category invoices unresolved"
        ),
    )
    grant = setup._read_grant()
    sign_case(
        home,
        dict(
            schema_version=GRANT,
            grant_ref="p1-cases",
            tenant=actor.tenant,
            engagement_epoch=selected["engagement_epoch"],
            workflow_ref=draft["workflow_ref"],
            run_ref=draft["run_ref"],
            starter_grant_revision=grant.revision,
            source_revision=grant.source_revision,
            settings_revision=grant.setup_policy_revision,
            source_digest=confirmed.source_digest,
            map_confirmation_ref=confirmed.confirmed_ref,
            map_settings_ref=confirmed.settings_ref,
            owner_ref="workflow-owner",
            owner_actor=ACTOR,
            custodian_ref=grant.custodian_id,
            expires_at=int(time.time()) + 600,
            owner_purposes=["propose", "review", "adjudicate", "freeze"],
            worker_purposes={},
            frozen_plan=asdict(
                FrozenPlan(
                    "baseline-pending",
                    "simple-policy-v1",
                    "exact-action-v1",
                    "review-v1",
                    "v1",
                    "independent-evaluator",
                    "selected-model",
                )
            ),
            critical_branches=[
                r.branch_ref
                for r in confirmed.branches
                if r.action in {"REVIEW", "FREIGHT", "SUPPLIES", "SOFTWARE"}
            ],
        ),
    )
    _write_private(
        home / "starter-case-selection.json",
        dict(schema_version=SELECTION, grant_file="starter-case-grant.json"),
    )
    return StarterCaseSession(settings, actor)


def test_p1_deterministic_provider_blind_review_and_reopen(p1_session):
    from opentour_cell.pilot.starter_map_confirmation import StarterCaseSession

    session = p1_session
    ref = session.propose(SevenInvoiceFamilies())
    rows = session.review()["cases"]
    assert len(rows) == len({r["family_id"] for r in rows}) == 7
    assert all(r["owner_adjudicated_target"] is None for r in rows)
    for revision, row in enumerate(rows):
        name = row["case_id"]
        assert set(dict(row["decision_inputs"])) == {"category", "po"}
        if name == "freight":
            assert row["suggestion"] == "SUPPLIES"
        if name == "mixed":
            assert "silent" in row["rationale"] and row["case_origin"] == "generated_starter"
        session.adjudicate(
            case_id=name,
            target=P1_OWNER_LABELS[name],
            reason="Policy is silent" if name == "mixed" else "Owner checked cited passage",
            expected_revision=revision,
            operation_ref="adjudicate-" + name,
        )
    reviewed = {r["case_id"]: r for r in session.review()["cases"]}
    assert reviewed["freight"]["owner_adjudicated_target"] == "FREIGHT"
    assert reviewed["mixed"]["owner_adjudicated_target"] is None
    splits = {
        r["family_id"]: (
            "excluded"
            if n == "mixed"
            else "development"
            if n in {"utilities", "tax"}
            else "reserved"
        )
        for n, r in reviewed.items()
    }
    frozen = session.freeze(splits=splits, expected_revision=7)
    assert (
        frozen.proposed,
        frozen.resolved,
        frozen.unresolved,
        frozen.development,
        frozen.reserved,
    ) == (7, 6, 1, 2, 4)
    assert len(set(frozen.reserved_family_refs)) == 4 and not frozen.uncovered_critical
    assert frozen.state == "frozen_unscored"
    reopened = StarterCaseSession(session.settings, session.actor)
    assert reopened.propose(SevenInvoiceFamilies()) == ref
    assert reopened.freeze(splits=splits, expected_revision=7) == frozen
