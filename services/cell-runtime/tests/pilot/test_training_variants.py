"""OPE-516 generated-sentinel conformance for validated training variants.

Fixture kind: generated. Generators, validators, grants and gates are local doubles; nothing here
is provider, customer-evidence or fit-qualification evidence.
"""

from __future__ import annotations

import dataclasses
import inspect
import json
import sqlite3
from pathlib import Path

import pytest
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training_variants import (
    ADMITTED,
    HELD,
    INVALIDATED,
    PREVIEW_VALID,
    QUARANTINED,
    REJECTED,
    FitGate,
    GenerationPlan,
    Generator,
    Grant,
    Policy,
    PolicyValidator,
    ProposedChild,
    Recipe,
    SeedRoot,
    SplitSeal,
    ValidationRequest,
    ValidatorDecision,
    VariantLedger,
    render_with_spans,
)

NOW = 100
POLICY = Policy(
    revision="generated-policy-r1",
    decision_fields=("vendor_note", "purchase_order", "amount", "layout"),
    required_inputs=("vendor_note",),
    clauses={"purchase_order": "REVIEW"},
)
RECIPES = {
    "paraphrase-v1": Recipe("paraphrase-v1", "r1", "paraphrase", frozenset({"vendor_note"})),
    "layout-v1": Recipe("layout-v1", "r1", "formatting", frozenset({"layout"})),
    "no-po-v1": Recipe("no-po-v1", "r1", "counterfactual", frozenset({"purchase_order"})),
    "amount-cf-v1": Recipe("amount-cf-v1", "r1", "counterfactual", frozenset({"amount"})),
}
GEN = Generator("generated-recipe-generator", "r1")
VALIDATOR = PolicyValidator("generated-independent-validator")
SEAL = SplitSeal.from_rows(
    [
        ("generated-case-1", "generated-fam-1", "training"),
        ("generated-case-2", "generated-fam-2", "training"),
        ("generated-case-3", "generated-fam-3", "training"),
        ("generated-case-p", "generated-fam-protected", "protected"),
        ("generated-case-d", "generated-fam-dev", "development"),
        ("generated-case-x1", "generated-fam-cross", "training"),
        ("generated-case-x2", "generated-fam-cross", "protected"),
        ("generated-case-6", "generated-fam-1", "training"),
    ]
)
GRANT = Grant("generated-grant-1", True, True, True, budget=100, expires_at=1000)
GATE = FitGate(True, "generated-gate-1")


def root(n: int = 1, **kw: object) -> SeedRoot:
    base = SeedRoot(
        root_ref=f"generated-root-{n}",
        case_ref=f"generated-case-{n}",
        label_ref=f"generated-label-{n}",
        family_id=f"generated-fam-{n}",
        split="training",
        policy_revision=POLICY.revision,
        source_revision="generated-src-r1",
        fields={
            "vendor_note": "generated carrier haulage sentinel",
            "purchase_order": "generated-po-0001",
            "amount": "generated-amt-100",
            "layout": "table",
        },
        prediction="SUPPLIES",
        target="FREIGHT",
        target_verified=True,
        target_verifier_ref="generated-accountant",
        verifier_independent=True,
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


def plan(
    props: tuple[ProposedChild, ...],
    seed: SeedRoot | None = None,
    **kw: object,
) -> GenerationPlan:
    base = GenerationPlan(
        plan_ref="generated-plan-1",
        mode="training",
        root=seed or root(),
        policy=POLICY,
        grant=GRANT,
        seal=SEAL,
        recipes=RECIPES,
        generator=GEN,
        proposals=props,
        fit_gate=GATE,
        approved_validators=frozenset({VALIDATOR.validator_ref}),
    )
    return dataclasses.replace(base, **kw)  # type: ignore[arg-type]


PARAPHRASE = ProposedChild("paraphrase-v1", {"vendor_note": "generated freight wording"}, "FREIGHT")
LAYOUT = ProposedChild("layout-v1", {"layout": "stacked"}, "FREIGHT")
NO_PO = ProposedChild("no-po-v1", {"purchase_order": None}, "REVIEW")


def by_reason(ledger: VariantLedger) -> dict[str, str]:
    return {c["child_id"]: c["reason"] for c in ledger.children()}


def manifest(ledger: VariantLedger, **kw: object) -> dict:
    return ledger.manifest(
        seal=SEAL,
        policy_revision=POLICY.revision,
        grant_ref=GRANT.grant_ref,
        mode="training",
        **kw,  # type: ignore[arg-type]
    )


def test_supplies_to_freight_wording_variant_stays_freight_and_missing_po_is_review() -> None:
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE, NO_PO)), VALIDATOR, now=NOW)
    kids = {c["recipe"]["recipe_id"]: c for c in ledger.children()}
    assert kids["paraphrase-v1"]["disposition"] == ADMITTED
    assert kids["paraphrase-v1"]["derived_label"] == "FREIGHT"
    assert kids["no-po-v1"]["disposition"] == ADMITTED
    assert kids["no-po-v1"]["derived_label"] == "REVIEW"
    for c in kids.values():
        assert c["origin"] == "synthetic"
        assert c["parent_case_ref"] == "generated-case-1"
        assert c["parent_label_ref"] == "generated-label-1"
        assert c["policy_revision"] == POLICY.revision
        assert c["validator_ref"] == "generated-independent-validator"
        assert c["changed_fields"] and c["invariant_fields"]


def test_evidence_spans_are_rebuilt_for_the_child_text() -> None:
    ledger = VariantLedger()
    ledger.run(plan((NO_PO,)), VALIDATOR, now=NOW)
    (child,) = ledger.children()
    text, spans = render_with_spans(POLICY, child["fields"])
    assert child["rendered_text"] == text
    by_field = {s["field"]: s for s in child["evidence_spans"]}
    assert by_field["purchase_order"]["present"] is False
    span = by_field["vendor_note"]
    assert text[span["start"] : span["end"]] == child["fields"]["vendor_note"]
    assert child["evidence_spans"] == spans


def test_rejects_invented_payment_multi_edit_unapproved_recipe_and_no_op() -> None:
    ledger = VariantLedger()
    invented = ProposedChild("paraphrase-v1", {"payment": "generated-pay-9"}, "FREIGHT")
    multi = ProposedChild(
        "no-po-v1", {"purchase_order": None, "amount": "generated-amt-7"}, "REVIEW"
    )
    unknown = ProposedChild("generated-unknown-recipe", {"layout": "x"}, "FREIGHT")
    outside = ProposedChild("paraphrase-v1", {"amount": "generated-amt-7"}, "FREIGHT")
    noop = ProposedChild("layout-v1", {"layout": "  TABLE "}, "FREIGHT")
    result = ledger.run(plan((invented, multi, unknown, outside, noop)), VALIDATOR, now=NOW)
    assert [c["reason"] for c in ledger.children()] == [
        "invented_field",
        "multi_edit_outside_recipe",
        "unapproved_recipe",
        "multi_edit_outside_recipe",
        "near_duplicate",
    ]
    assert all(c["disposition"] == REJECTED for c in ledger.children())
    assert ledger.spent(GRANT.grant_ref) == 0
    assert result.created == 5


def test_independent_falsification_freight_root_two_admitted_third_rejected() -> None:
    ledger = VariantLedger()
    both = ProposedChild(
        "no-po-v1", {"purchase_order": None, "amount": "generated-amt-7"}, "REVIEW"
    )
    props = (PARAPHRASE, NO_PO, both)
    first = ledger.run(plan(props), VALIDATOR, now=NOW)
    second = ledger.run(plan(props), VALIDATOR, now=NOW)  # each submitted twice
    assert second.created == 0 and second.replayed == 3
    assert first.child_ids == second.child_ids
    kids = ledger.children()
    assert [c["disposition"] for c in kids] == [ADMITTED, ADMITTED, REJECTED]
    assert kids[2]["reason"] == "multi_edit_outside_recipe"
    assert manifest(ledger)["admitted_count"] == 2


def test_cap_per_root_rejects_third_valid_child() -> None:
    ledger = VariantLedger()
    third = ProposedChild("layout-v1", {"layout": "stacked"}, "FREIGHT")
    ledger.run(plan((PARAPHRASE, NO_PO, third)), VALIDATOR, now=NOW)
    kids = ledger.children()
    assert kids[2]["disposition"] == REJECTED and kids[2]["reason"] == "root_cap_exceeded"


def test_wrong_generator_label_is_quarantined_with_zero_fit_admission() -> None:
    ledger = VariantLedger()
    wrong = ProposedChild("paraphrase-v1", {"vendor_note": "generated other wording"}, "SUPPLIES")
    ledger.run(plan((wrong,)), VALIDATOR, now=NOW)
    (child,) = ledger.children()
    assert (child["disposition"], child["reason"]) == (QUARANTINED, "label_disagreement")
    assert manifest(ledger)["admitted_count"] == 0
    assert manifest(ledger)["final_weights"] == {}


def test_ambiguous_paraphrase_is_quarantined() -> None:
    class Unsure:
        def validate(self, request: ValidationRequest) -> ValidatorDecision:
            return ValidatorDecision("generated-independent-validator", None)

    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,)), Unsure(), now=NOW)
    assert ledger.children()[0]["reason"] == "ambiguous_target"
    assert ledger.children()[0]["disposition"] == QUARANTINED


def test_validator_never_sees_the_proposed_label() -> None:
    marker = "GENERATED-PROPOSED-LABEL-MARKER"
    seen: list[ValidationRequest] = []

    class Spy:
        def validate(self, request: ValidationRequest) -> ValidatorDecision:
            seen.append(request)
            return PolicyValidator("generated-independent-validator").validate(request)

    ledger = VariantLedger()
    leaky = ProposedChild("paraphrase-v1", {"vendor_note": "generated spy wording"}, marker)
    ledger.run(plan((leaky,)), Spy(), now=NOW)
    names = {f.name for f in dataclasses.fields(ValidationRequest)}
    assert not any("label" in n or "generator" in n for n in names)
    assert len(seen) == 1
    assert marker not in repr(seen[0])
    assert not any(marker in str(v) for v in vars(seen[0]).values())
    # The marker disagrees with the policy-derived label, proving a label was available to leak.
    assert ledger.children()[0]["reason"] == "label_disagreement"


def test_missing_validation_holds_and_never_passes() -> None:
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,)), None, now=NOW)
    (child,) = ledger.children()
    assert (child["disposition"], child["reason"]) == (HELD, "independent_validation_absent")
    m = manifest(ledger)
    assert m["admitted_count"] == 0 and m["held_count"] == 1


def test_self_validation_by_generator_is_rejected() -> None:
    ledger = VariantLedger()
    result = ledger.submit(plan((PARAPHRASE,)), now=NOW)
    with pytest.raises(TrainingError, match="self_validation_rejected"):
        ledger.validate(result.child_ids[0], ValidatorDecision(GEN.ref, "FREIGHT"))
    assert ledger.children()[0]["disposition"] == HELD


def test_protected_development_and_cross_split_seeds_are_rejected() -> None:
    ledger = VariantLedger()
    cases = {
        "protected_seed": root(7, family_id="generated-fam-protected", split="protected"),
        "not_training_partition": root(8, family_id="generated-fam-dev", split="development"),
        "cross_split_family": root(9, family_id="generated-fam-cross"),
    }
    for reason, seed in cases.items():
        ledger.run(
            plan((PARAPHRASE,), seed, plan_ref=f"generated-plan-{reason}"), VALIDATOR, now=NOW
        )
    assert sorted(c["reason"] for c in ledger.children()) == sorted(cases)
    assert {c["disposition"] for c in ledger.children()} == {REJECTED}
    assert ledger.spent(GRANT.grant_ref) == 0
    # No field content from a refused root is stored.
    assert all("fields" not in c for c in ledger.children())


def test_protected_seed_content_never_reaches_the_ledger_file(tmp_path: Path) -> None:
    path = tmp_path / "variants.sqlite3"
    ledger = VariantLedger(path)
    seed = root(7, family_id="generated-fam-protected", split="protected")
    ledger.run(plan((PARAPHRASE,), seed), VALIDATOR, now=NOW)
    assert b"generated carrier haulage sentinel" not in path.read_bytes()
    assert b"generated-po-0001" not in path.read_bytes()


def test_unverified_seeds_yield_no_usable_variant() -> None:
    ledger = VariantLedger()
    for i, bad in enumerate(
        (
            {"target_verified": False},
            {"verifier_independent": False},
            {"target_verifier_ref": ""},
            {"policy_revision": "generated-policy-r0"},
        ),
        start=1,
    ):
        ledger.run(
            plan((PARAPHRASE,), root(root_ref=f"generated-root-bad-{i}", **bad)), VALIDATOR, now=NOW
        )
    reasons = [c["reason"] for c in ledger.children()]
    assert reasons[:3] == ["unverified_target"] * 3
    assert reasons[3] == "policy_revision_mismatch"
    assert manifest(ledger)["admitted_count"] == 0


@pytest.mark.parametrize(
    ("grant", "reason"),
    [
        (dataclasses.replace(GRANT, generation_authorized=False), "grant_missing"),
        (dataclasses.replace(GRANT, training_authorized=False), "grant_missing"),
        (dataclasses.replace(GRANT, expires_at=NOW), "grant_missing"),
        (
            dataclasses.replace(GRANT, execution_location_approved=False),
            "execution_location_unapproved",
        ),
    ],
)
def test_grant_and_location_gates_fail_closed(grant: Grant, reason: str) -> None:
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,), grant=grant), VALIDATOR, now=NOW)
    assert ledger.children()[0]["reason"] == reason


def test_unapproved_external_teacher_is_rejected_and_approved_one_is_allowed() -> None:
    ledger = VariantLedger()
    teacher = Generator("generated-external-teacher", "r1", external=True)
    ledger.run(plan((PARAPHRASE,), generator=teacher), VALIDATOR, now=NOW)
    assert ledger.children()[0]["reason"] == "unapproved_external_teacher"
    ok = dataclasses.replace(GRANT, approved_external_teachers=frozenset({teacher.ref}))
    ledger.run(plan((LAYOUT,), generator=teacher, grant=ok), VALIDATOR, now=NOW)
    assert ledger.children()[1]["disposition"] == ADMITTED


def test_replay_adds_no_rows_and_no_spend_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "variants.sqlite3"
    props = (dataclasses.replace(PARAPHRASE, cost=7), dataclasses.replace(NO_PO, cost=5))
    first = VariantLedger(path).run(plan(props), VALIDATOR, now=NOW)
    assert VariantLedger(path).spent(GRANT.grant_ref) == 12
    restarted = VariantLedger(path)  # simulated process restart
    again = restarted.run(plan(props), VALIDATOR, now=NOW)
    assert again.created == 0 and again.replayed == 2
    assert again.child_ids == first.child_ids
    assert len(restarted.children()) == 2
    assert restarted.spent(GRANT.grant_ref) == 12
    assert [c["disposition"] for c in restarted.children()] == [ADMITTED, ADMITTED]


def test_retry_after_partial_failure_completes_without_duplicates() -> None:
    ledger = VariantLedger()
    first = ledger.submit(plan((PARAPHRASE, NO_PO)), now=NOW)  # crash before validation
    assert [c["disposition"] for c in ledger.children()] == [HELD, HELD]
    ledger.run(plan((PARAPHRASE, NO_PO)), VALIDATOR, now=NOW)
    assert len(ledger.children()) == 2
    assert [c["disposition"] for c in ledger.children()] == [ADMITTED, ADMITTED]
    assert first.child_ids == tuple(c["child_id"] for c in ledger.children())


def test_exhausted_authorization_stops_without_overspend() -> None:
    ledger = VariantLedger()
    small = dataclasses.replace(GRANT, budget=6)
    props = (
        dataclasses.replace(PARAPHRASE, cost=4),
        dataclasses.replace(NO_PO, cost=4),
        dataclasses.replace(LAYOUT, cost=1),
    )
    result = ledger.run(plan(props, grant=small), VALIDATOR, now=NOW)
    assert result.stopped and result.not_attempted == 1
    assert ledger.spent(small.grant_ref) == 4
    kids = ledger.children()
    assert kids[1]["reason"] == "authorization_exhausted" and len(kids) == 2


def test_near_duplicate_siblings_are_not_counted_twice() -> None:
    ledger = VariantLedger()
    a = ProposedChild("paraphrase-v1", {"vendor_note": "Generated  Freight, wording!"}, "FREIGHT")
    b = ProposedChild("paraphrase-v1", {"vendor_note": "generated freight wording"}, "FREIGHT")
    ledger.run(plan((a, b)), VALIDATOR, now=NOW)
    assert [c["disposition"] for c in ledger.children()] == [ADMITTED, REJECTED]
    assert ledger.children()[1]["reason"] == "near_duplicate"


def test_one_mistake_plus_100_children_is_still_one_mistake_one_family() -> None:
    ledger = VariantLedger()
    props = tuple(
        ProposedChild("paraphrase-v1", {"vendor_note": f"generated wording variant {i}"}, "FREIGHT")
        for i in range(100)
    )
    ledger.run(
        plan(props, max_children_per_root=100, max_children_per_family=100), VALIDATOR, now=NOW
    )
    m = manifest(ledger, coverage_required_groups=3)
    assert m["admitted_count"] == 100
    assert m["observed_mistake_count"] == 1
    assert m["independent_family_count"] == 1
    assert m["children_in_independent_count"] == 0
    assert m["coverage"] == {
        "required_independent_groups": 3,
        "real_independent_groups": 1,
        "filled": False,
    }
    assert m["root_sibling_counts"] == {"generated-root-1": 100}
    assert set(m["final_weights"].values()) == {"1/100"}  # per-root influence stays capped


def test_three_independent_real_families_fill_the_coverage_gate() -> None:
    ledger = VariantLedger()
    roots = [root(n) for n in (1, 2, 3)]
    for seed in roots:
        ledger.run(
            plan((PARAPHRASE,), seed, plan_ref=f"generated-plan-{seed.root_ref}"),
            VALIDATOR,
            now=NOW,
        )
    m = manifest(ledger, coverage_required_groups=3)
    assert m["coverage"]["filled"] is True
    assert m["independent_family_count"] == 3 and m["observed_mistake_count"] == 3


def test_preview_validates_but_never_admits_for_fit() -> None:
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,), mode="preview", fit_gate=None), VALIDATOR, now=NOW)
    (child,) = ledger.children()
    assert child["disposition"] == PREVIEW_VALID
    assert (
        ledger.manifest(
            seal=SEAL,
            policy_revision=POLICY.revision,
            grant_ref=GRANT.grant_ref,
            mode="preview",
        )["admitted_count"]
        == 0
    )


def test_training_mode_holds_until_the_fit_gate_opens() -> None:
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,), fit_gate=FitGate(False, "generated-gate-0")), VALIDATOR, now=NOW)
    assert ledger.children()[0]["reason"] == "fit_gate_closed"
    assert ledger.children()[0]["disposition"] == REJECTED
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["disposition"] == ADMITTED


def test_unresolved_rule_requests_one_owner_decision_instead_of_generating() -> None:
    ledger = VariantLedger()
    unclaused = Policy(
        revision=POLICY.revision,
        decision_fields=POLICY.decision_fields,
        required_inputs=POLICY.required_inputs,
        clauses={},
    )
    seed = root()
    ledger.run(plan((NO_PO,), seed, policy=unclaused), VALIDATOR, now=NOW)
    (child,) = ledger.children()
    assert child["reason"] == "policy_clause_missing" and child["disposition"] == REJECTED
    assert manifest(ledger)["requests"] == [
        {"kind": "owner_decision", "field": "purchase_order", "accountable_role": "owner"}
    ]
    assert "fields" not in child


def test_missing_decision_input_goes_to_data_context_request() -> None:
    ledger = VariantLedger()
    seed = root(fields={"purchase_order": "generated-po-0001", "layout": "table"})
    ledger.run(plan((LAYOUT,), seed), VALIDATOR, now=NOW)
    assert ledger.children()[0]["reason"] == "decision_input_missing"
    assert manifest(ledger)["requests"] == [
        {
            "kind": "data_context_request",
            "field": "vendor_note",
            "accountable_role": "data_custodian",
        }
    ]


def test_retracted_root_invalidates_descendants_and_late_callback_cannot_readmit() -> None:
    ledger = VariantLedger()
    result = ledger.submit(plan((PARAPHRASE, NO_PO)), now=NOW)  # generated, validation pending
    prior = ledger.retract_root("generated-root-1", "generated-fam-1", "reopened")
    assert prior == result.child_ids
    late = ledger.validate(
        result.child_ids[0],
        PolicyValidator("generated-independent-validator").validate(
            ValidationRequest(POLICY, "FREIGHT", {}, ())
        ),
    )
    assert late == INVALIDATED
    assert {c["disposition"] for c in ledger.children()} == {INVALIDATED}
    # Resubmitting the retracted root re-admits nothing and spends nothing more.
    again = ledger.run(plan((LAYOUT,)), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "root_invalidated"
    assert again.created == 1
    m = manifest(ledger)
    assert m["admitted_count"] == 0
    assert m["observed_mistake_count"] == 0 and m["independent_family_count"] == 0
    assert m["invalidated_child_ids"] == sorted(prior)


def test_retraction_after_admission_invalidates_admitted_children_and_recomputes() -> None:
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE, NO_PO)), VALIDATOR, now=NOW)
    assert manifest(ledger)["admitted_count"] == 2
    ledger.retract_root("generated-root-1", "generated-fam-1", "revoked")
    m = manifest(ledger)
    assert m["admitted_count"] == 0 and m["invalidated_count"] == 2
    assert m["final_weights"] == {} and m["exclusion_reasons"] == {"root_revoked": 2}
    assert all(c["prior_disposition"] == ADMITTED for c in ledger.children())


def test_manifest_is_sealed_with_counts_and_exclusion_reasons() -> None:
    ledger = VariantLedger()
    bad = ProposedChild("paraphrase-v1", {"payment": "generated-pay-9"}, "FREIGHT")
    wrong = ProposedChild("layout-v1", {"layout": "stacked"}, "REVIEW")
    ledger.run(plan((PARAPHRASE, bad, wrong)), VALIDATOR, now=NOW)
    m = manifest(ledger)
    assert m["sealed"] is True and m["split_digest"] == SEAL.digest
    assert (
        m["generated_count"],
        m["admitted_count"],
        m["rejected_count"],
        m["quarantined_count"],
    ) == (3, 1, 1, 1)
    assert m["exclusion_reasons"] == {"invented_field": 1, "label_disagreement": 1}
    assert m["manifest_digest"] == manifest(ledger)["manifest_digest"]
    json.dumps(m)  # serializable


def test_split_seal_carries_no_labels_and_flags_cross_split_families() -> None:
    assert SEAL.assignments["generated-fam-cross"] == ("protected", "training")
    other = SplitSeal.from_rows([("generated-case-1", "generated-fam-1", "training")])
    assert other.digest != SEAL.digest
    with pytest.raises(TrainingError, match="invalid_split"):
        SplitSeal.from_rows([("generated-case-1", "generated-fam-1", "generated-bad-split")])


# ------------------------------------------------------------- independent-review regressions


def test_family_count_is_derived_from_the_ledger_and_seal_not_from_the_caller() -> None:
    """F4: only roots the ledger itself admitted, still sealed as training, are counted."""
    ledger = VariantLedger()
    seeds = (
        root(1),
        root(2, verifier_independent=False),
        root(4, family_id="generated-fam-cross"),
        root(5, family_id="generated-fam-not-in-seal"),
    )
    for seed in seeds:
        pref = f"generated-plan-{seed.root_ref}"
        ledger.run(plan((PARAPHRASE,), seed, plan_ref=pref), VALIDATOR, now=NOW)
    m = manifest(ledger)
    assert m["independent_families"] == ["generated-fam-1"]
    assert m["independent_family_count"] == 1 and m["observed_mistake_count"] == 1
    assert "real_roots" not in inspect.signature(VariantLedger.manifest).parameters
    # A different sealed split (or policy revision) can never reuse these counts.
    other = SplitSeal.from_rows([("generated-case-1", "generated-fam-1", "development")])
    swapped = ledger.manifest(
        seal=other, policy_revision=POLICY.revision, grant_ref=GRANT.grant_ref, mode="training"
    )
    assert swapped["independent_family_count"] == 0
    revised = ledger.manifest(
        seal=SEAL, policy_revision="generated-policy-r9", grant_ref=GRANT.grant_ref, mode="training"
    )
    assert revised["independent_family_count"] == 0


def test_a_synthetic_child_cannot_be_resubmitted_as_a_real_root() -> None:
    """F4: no grandchildren and no inflated counts from a child presented as a root."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    child = ledger.children()[0]
    attacks = (
        root(
            root_ref=child["child_id"], case_ref="generated-case-9", label_ref="generated-label-9"
        ),
        root(root_ref="generated-root-9", case_ref=child["child_id"]),
        root(root_ref="generated-root-8", fields=child["fields"], family_id="generated-fam-2"),
    )
    for n, attack in enumerate(attacks):
        pref = f"generated-plan-attack-{n}"
        ledger.run(plan((LAYOUT,), attack, plan_ref=pref), VALIDATOR, now=NOW)
    assert [c["reason"] for c in ledger.children()[1:]] == ["synthetic_lineage_seed"] * 3
    m = manifest(ledger)
    assert m["admitted_count"] == 1 and m["independent_family_count"] == 1
    assert m["children_in_independent_count"] == 0


def test_a_root_cannot_change_family_after_registration() -> None:
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    moved = root(1, family_id="generated-fam-2")
    ledger.run(plan((LAYOUT,), moved), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "root_identity_conflict"


def test_preview_does_not_block_or_consume_the_training_equivalent() -> None:
    """F1: preview must precede training without poisoning it."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE, LAYOUT), mode="preview", fit_gate=None), VALIDATOR, now=NOW)
    assert [c["disposition"] for c in ledger.children()] == [PREVIEW_VALID, PREVIEW_VALID]
    ledger.run(plan((PARAPHRASE, NO_PO)), VALIDATOR, now=NOW)
    training = [c for c in ledger.children() if c["mode"] == "training"]
    assert [c["disposition"] for c in training] == [ADMITTED, ADMITTED]
    assert manifest(ledger)["admitted_count"] == 2


@pytest.mark.parametrize(
    "variant",
    [
        "ｇｅｎｅｒａｔｅｄ ｆｒｅｉｇｈｔ wording",
        "wording freight generated",
        "generated fréight wording",
        "generated fre​ight wording",
        "GENERATED   freight--wording",
    ],
)
def test_near_duplicate_bypasses_are_caught(variant: str) -> None:
    """F2: width, order, accent, invisible-character and punctuation tricks are one example."""
    ledger = VariantLedger()
    base = ProposedChild("paraphrase-v1", {"vendor_note": "generated freight wording"}, "FREIGHT")
    twin = ProposedChild("paraphrase-v1", {"vendor_note": variant}, "FREIGHT")
    roomy = plan((base, twin), max_children_per_root=5, max_children_per_family=5)
    ledger.run(roomy, VALIDATOR, now=NOW)
    assert [c["disposition"] for c in ledger.children()] == [ADMITTED, REJECTED]
    assert ledger.children()[1]["reason"] == "near_duplicate"


def test_forged_validation_cannot_admit() -> None:
    """F3: authority comes from the stored plan, not from whatever the caller passes."""
    ledger = VariantLedger()
    result = ledger.submit(plan((PARAPHRASE, NO_PO)), now=NOW)
    first, second = result.child_ids
    assert "base_target" not in inspect.signature(VariantLedger.validate).parameters
    for ref in ("x-other", "generated-independent-validator-2"):
        with pytest.raises(TrainingError, match="validator_not_authorized"):
            ledger.validate(first, ValidatorDecision(ref, "FREIGHT"))
    for ref in (GEN.ref + " ", GEN.ref.upper(), GEN.ref + "​", ""):
        with pytest.raises(TrainingError, match="self_validation_rejected"):
            ledger.validate(first, ValidatorDecision(ref, "FREIGHT"))
    # An authorized validator that contradicts the stored policy cannot admit either.
    wrong_policy = ValidatorDecision("generated-independent-validator", "SUPPLIES")
    assert ledger.validate(first, wrong_policy) == QUARANTINED
    # The second child's proposal is REVIEW; a validator answering FREIGHT disagrees.
    forged = ValidatorDecision("generated-independent-validator", "FREIGHT")
    assert ledger.validate(second, forged) == QUARANTINED
    assert ledger.children()[1]["reason"] == "label_disagreement"
    assert manifest(ledger)["admitted_count"] == 0


def test_validator_agreeing_with_a_wrong_generator_but_not_the_policy_is_quarantined() -> None:
    ledger = VariantLedger()
    wrong = ProposedChild("no-po-v1", {"purchase_order": None}, "SUPPLIES")
    result = ledger.submit(plan((wrong,)), now=NOW)
    decision = ValidatorDecision("generated-independent-validator", "SUPPLIES")
    assert ledger.validate(result.child_ids[0], decision) == QUARANTINED
    assert ledger.children()[0]["reason"] == "validator_policy_disagreement"


def test_manifest_is_scoped_to_its_mode_policy_grant_and_seal() -> None:
    """F6: a sealed manifest cannot mix modes, grants or policy revisions."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,), mode="preview", fit_gate=None), VALIDATOR, now=NOW)
    other_grant = dataclasses.replace(GRANT, grant_ref="generated-grant-2")
    other_policy = dataclasses.replace(POLICY, revision="generated-policy-r2")
    ledger.run(plan((LAYOUT,), grant=other_grant, plan_ref="generated-plan-g2"), VALIDATOR, now=NOW)
    seed_p2 = root(2, policy_revision="generated-policy-r2")
    ledger.run(
        plan((PARAPHRASE,), seed_p2, policy=other_policy, plan_ref="generated-plan-p2"),
        VALIDATOR,
        now=NOW,
    )
    ledger.run(plan((NO_PO,), plan_ref="generated-plan-main"), VALIDATOR, now=NOW)
    m = manifest(ledger)
    assert (m["generated_count"], m["admitted_count"], m["preview_valid_count"]) == (1, 1, 0)
    assert m["independent_families"] == ["generated-fam-1"]
    assert m["spend"] == 3  # spend is grant-wide: preview, training and policy-r2 children
    preview = ledger.manifest(
        seal=SEAL, policy_revision=POLICY.revision, grant_ref=GRANT.grant_ref, mode="preview"
    )
    counts = (preview["generated_count"], preview["preview_valid_count"], preview["admitted_count"])
    assert counts == (1, 1, 0)
    g2 = ledger.manifest(
        seal=SEAL, policy_revision=POLICY.revision, grant_ref="generated-grant-2", mode="training"
    )
    assert g2["generated_count"] == 1 and g2["spend"] == 1


def test_a_later_plan_cannot_raise_the_per_root_or_per_family_cap() -> None:
    """F5: caps are fixed when the root is first registered and are family-wide."""
    ledger = VariantLedger()
    third = ProposedChild("layout-v1", {"layout": "stacked"}, "FREIGHT")
    ledger.run(plan((PARAPHRASE, NO_PO)), VALIDATOR, now=NOW)
    raised = plan(
        (third,),
        plan_ref="generated-plan-raise",
        max_children_per_root=50,
        max_children_per_family=50,
    )
    ledger.run(raised, VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "root_cap_exceeded"
    assert manifest(ledger)["admitted_count"] == 2
    # A second root in the same family cannot double the family's synthetic influence.
    sibling = root(6, family_id="generated-fam-1", case_ref="generated-case-6")
    ledger.run(plan((LAYOUT,), sibling, plan_ref="generated-plan-sib"), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "family_cap_exceeded"
    m = manifest(ledger)
    assert m["admitted_count"] == 2 and m["family_sibling_counts"] == {"generated-fam-1": 2}
    assert set(m["final_weights"].values()) == {"1/2"}


def test_policy_and_inputs_are_immutable_for_validators() -> None:
    """F7: a hostile validator cannot rewrite the rules used for later derivations."""
    clauses = {"purchase_order": "REVIEW"}
    policy = dataclasses.replace(POLICY, clauses=clauses)
    clauses["purchase_order"] = "FREIGHT"  # caller later mutates its own dict
    assert policy.clauses["purchase_order"] == "REVIEW"

    class Hostile:
        def validate(self, request: ValidationRequest) -> ValidatorDecision:
            with pytest.raises(TypeError):
                request.policy.clauses["purchase_order"] = "FREIGHT"  # type: ignore[index]
            with pytest.raises(TypeError):
                request.child_fields["layout"] = "x"  # type: ignore[index]
            return PolicyValidator("generated-independent-validator").validate(request)

    ledger = VariantLedger()
    ledger.run(plan((NO_PO,), policy=policy), Hostile(), now=NOW)
    assert ledger.children()[0]["disposition"] == ADMITTED
    assert policy.clauses["purchase_order"] == "REVIEW"


def test_a_corrected_label_is_a_new_row_and_the_quarantined_one_stays() -> None:
    """F8: resubmitting with the right label is not swallowed by the earlier wrong one."""
    ledger = VariantLedger()
    wrong = ProposedChild(
        "paraphrase-v1", {"vendor_note": "generated corrected wording"}, "SUPPLIES"
    )
    right = dataclasses.replace(wrong, proposed_label="FREIGHT")
    ledger.run(plan((wrong,)), VALIDATOR, now=NOW)
    ledger.run(plan((right,)), VALIDATOR, now=NOW)
    assert [c["disposition"] for c in ledger.children()] == [QUARANTINED, ADMITTED]


def test_retryable_rejection_keeps_its_history_when_it_flips() -> None:
    """F8: re-evaluating a state-dependent rejection never erases the earlier outcome."""
    ledger = VariantLedger()
    closed = plan((PARAPHRASE,), fit_gate=FitGate(False, "generated-gate-0"))
    ledger.run(closed, VALIDATOR, now=NOW)
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    (child,) = ledger.children()
    assert child["disposition"] == ADMITTED
    assert child["history"] == [
        {"disposition": REJECTED, "reason": "fit_gate_closed", "plan_ref": "generated-plan-1"}
    ]


def test_lock_contention_is_reported_as_such_not_as_a_rollback_error(tmp_path: Path) -> None:
    """F8: failing to start a transaction must surface the real error."""
    path = tmp_path / "variants.sqlite3"
    ledger = VariantLedger(path)
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    original = sqlite3.connect

    def quick(*args: object, **kwargs: object) -> sqlite3.Connection:
        kwargs["timeout"] = 0.05
        return original(*args, **kwargs)  # type: ignore[arg-type]

    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(sqlite3, "connect", quick)
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                ledger.spent("generated-grant-1")
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()


# ------------------------------------------------------- independent re-review regressions (N1-N6)


def test_root_content_and_policy_are_pinned_at_first_registration() -> None:
    """N1: a later plan cannot rewrite the target, fields, case, policy or validators."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)

    def layout(tag: str) -> ProposedChild:
        return ProposedChild("layout-v1", {"layout": f"stacked-{tag}"}, "FREIGHT")

    retarget = ProposedChild("layout-v1", {"layout": "stacked"}, "SUPPLIES")
    forged_policy = dataclasses.replace(POLICY, clauses={"purchase_order": "PAYMENT"})
    pay = ProposedChild("no-po-v1", {"purchase_order": None}, "PAYMENT")
    attacks = (
        plan((retarget,), root(target="SUPPLIES"), plan_ref="generated-plan-a1"),
        plan((pay,), policy=forged_policy, plan_ref="generated-plan-a2"),
        plan((layout("a3"),), root(case_ref="generated-case-2"), plan_ref="generated-plan-a3"),
        plan(
            (LAYOUT,),
            root(fields={**root().fields, "amount": "generated-amt-999"}),
            plan_ref="generated-plan-a4",
        ),
        plan(
            (layout("a5"),),
            approved_validators=frozenset({GEN.ref}),
            plan_ref="generated-plan-a5",
        ),
    )
    for attack in attacks:
        ledger.run(attack, PolicyValidator("generated-independent-validator"), now=NOW)
    assert [c["reason"] for c in ledger.children()[1:]] == ["root_pin_mismatch"] * 5
    m = manifest(ledger)
    assert m["admitted_count"] == 1 and m["observed_mistake_count"] == 1


def test_one_real_case_registered_under_many_roots_counts_once() -> None:
    """N2: a 3-group coverage gate cannot be filled from one mistake."""
    ledger = VariantLedger()
    for n in (1, 2, 3):
        # Same real case and label under a new root_ref; only the first can register.
        clone = root(
            root_ref=f"generated-root-clone-{n}",
            case_ref="generated-case-1",
            label_ref="generated-label-1",
            family_id="generated-fam-1",
        )
        ledger.run(plan((PARAPHRASE,), clone, plan_ref=f"generated-plan-c{n}"), VALIDATOR, now=NOW)
    reasons = [c["reason"] for c in ledger.children()]
    assert reasons[0] != "duplicate_case" and reasons[1:] == ["duplicate_case"] * 2
    m = manifest(ledger, coverage_required_groups=3)
    assert m["observed_mistake_count"] == 1 and m["independent_family_count"] == 1
    assert m["coverage"]["filled"] is False


def test_a_case_must_be_sealed_under_the_family_it_claims() -> None:
    """N2: the seal binds case to family, so a case cannot be re-homed into another family."""
    ledger = VariantLedger()
    for n, fam in enumerate(("generated-fam-2", "generated-fam-3")):
        moved = root(
            root_ref=f"generated-root-moved-{n}",
            family_id=fam,
            case_ref="generated-case-1",
            label_ref=f"generated-label-moved-{n}",
        )
        ledger.run(plan((PARAPHRASE,), moved, plan_ref=f"generated-plan-m{n}"), VALIDATOR, now=NOW)
    assert [c["reason"] for c in ledger.children()] == ["case_family_mismatch"] * 2
    assert manifest(ledger)["independent_family_count"] == 0
    assert SEAL.cases["generated-case-1"] == ("generated-fam-1",)


def test_family_cap_is_per_family_not_per_root_registration() -> None:
    """N3: a second root cannot reopen the family's cap by passing a larger one."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE, NO_PO)), VALIDATOR, now=NOW)
    sibling = root(6, family_id="generated-fam-1")
    roomy = plan(
        (LAYOUT,),
        sibling,
        plan_ref="generated-plan-roomy",
        max_children_per_root=50,
        max_children_per_family=50,
    )
    ledger.run(roomy, VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "family_cap_exceeded"
    assert manifest(ledger)["family_sibling_counts"] == {"generated-fam-1": 2}


def test_replay_under_a_different_grant_is_charged_to_that_grant() -> None:
    """N4: an in-place re-evaluation moves the row, its cost and its budget check together."""
    ledger = VariantLedger()
    blocked = dataclasses.replace(GRANT, execution_location_approved=False)
    ledger.run(plan((PARAPHRASE,), grant=blocked), VALIDATOR, now=NOW)
    assert ledger.children()[0]["reason"] == "execution_location_unapproved"
    grant_b = dataclasses.replace(GRANT, grant_ref="generated-grant-b", budget=1)
    ledger.run(plan((PARAPHRASE,), grant=grant_b), VALIDATOR, now=NOW)
    assert ledger.children()[0]["disposition"] == ADMITTED
    assert ledger.spent("generated-grant-b") == 1
    assert ledger.spent(GRANT.grant_ref) == 0
    # Grant B is now exhausted: a further child under it must stop, not overspend.
    ledger.run(plan((NO_PO,), grant=grant_b, plan_ref="generated-plan-b2"), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "authorization_exhausted"
    assert ledger.spent("generated-grant-b") == 1


def test_a_preview_only_root_is_not_counted_in_a_training_manifest() -> None:
    """N6: root counts follow the (mode, grant) scope the root was actually registered in."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,), mode="preview", fit_gate=None), VALIDATOR, now=NOW)
    assert manifest(ledger)["independent_family_count"] == 0
    preview = ledger.manifest(
        seal=SEAL, policy_revision=POLICY.revision, grant_ref=GRANT.grant_ref, mode="preview"
    )
    assert preview["independent_family_count"] == 1
    ledger.run(plan((LAYOUT,)), VALIDATOR, now=NOW)
    assert manifest(ledger)["independent_family_count"] == 1


def test_retract_cannot_reassign_a_registered_root_to_another_family() -> None:
    """N6: retraction uses the stored family and refuses a conflicting one."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    with pytest.raises(TrainingError, match="root_identity_conflict"):
        ledger.retract_root("generated-root-1", "generated-fam-2", "reopened")
    assert ledger.children()[0]["disposition"] == ADMITTED
    assert ledger.retract_root("generated-root-1", "generated-fam-1", "reopened")


def test_records_are_json_serializable_but_domain_objects_are_not_asdict_safe() -> None:
    """N5: consumers serialize the ledger's JSON records, never the frozen domain objects."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    json.dumps(ledger.children())
    with pytest.raises(TypeError):
        dataclasses.asdict(POLICY)
