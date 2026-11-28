"""OPE-516 generated-sentinel conformance for validated training variants.

Fixture kind: generated. Generators, validators and grants are local doubles. The fit gate is a
real worker-held `LearningMandates` ledger in a private generated directory, with generated
sentinel mandate, source and reservation values. Nothing here is provider, customer-evidence or
fit-qualification evidence.
"""

from __future__ import annotations

import copy
import dataclasses
import inspect
import json
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import open_weight
from opentour_cell.pilot.learning_mandate import LearningMandates
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.text_classifier import TrainingError
from opentour_cell.pilot.training import LocalTrainingStore
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

OWNER = "generated-customer-owner"
DELEGATE = "generated-local-worker"
MANDATE = "generated-standing-mandate"
CLOSED_GATE = FitGate("fit-generated-unreserved", DELEGATE)  # names no worker reservation


class FitWorld:
    """A real worker-held learning-mandate ledger with generated sentinel values."""

    def __init__(self, directory: Path) -> None:
        directory.chmod(0o700)
        store = LocalTrainingStore(directory / "training.sqlite3", "generated-org")
        self.mandates = LearningMandates(
            directory, "generated-org", store.engagement_id, owner=OWNER
        )
        self.mandates.enroll(
            {
                "mandate_id": MANDATE,
                "owner_id": OWNER,
                "delegate": DELEGATE,
                "mode": "autonomous",
                "workflow": "generated-coding",
                "connectors": ["generated-mcp"],
                "trace_source": "generated-traces",
                "correction_source": "generated-corrections",
                "field": "route",
                "fields": ["question"],
                "labels": ["FREIGHT", "REVIEW", "SUPPLIES"],
                "profile": open_weight.PROFILE,
                "uses": ["inspect", "evaluate", "train"],
                "permitted_error_pairs": [["SUPPLIES", "FREIGHT"]],
                "agreement_revision": digest("generated-operating-limits"),
                "trainer": DELEGATE,
                "payer": "generated-zero-cost-cpu",
                "reviewer": "generated-independent-reviewer",
                "audience": "generated-review-channel",
                "destination": "generated-customer-local",
                "expires_at": int(time.time()) + 3600,
                "max_runs": 5,
                "max_fits": 3,
                "max_total_seconds": 60,
                "max_seconds_per_fit": 20,
                "max_rows_per_run": 40,
                "max_total_rows": 200,
                "max_cost_cents": 0,
            },
            actor=OWNER,
        )

    def reserve(self, idx: int) -> str:
        """Reserve one fit through the mandate's own path (never through the variant ledger)."""
        run = self.mandates.authorize_source(
            MANDATE,
            {
                "source_revision": digest(["generated-source", idx]),
                "trace_sha256": digest(["generated-traces", idx]),
                "correction_sha256": digest(["generated-corrections", idx]),
                "workflow": "generated-coding",
                "connector": "generated-mcp",
                "trace_source": "generated-traces",
                "correction_source": "generated-corrections",
                "field": "route",
                "fields": ["question"],
                "rows": 24,
            },
            actor=DELEGATE,
        )["run_id"]
        pair = ["SUPPLIES", "FREIGHT"]
        self.mandates.confirm_class(
            run,
            actor=DELEGATE,
            discovery_revision=digest(["generated-discovery", idx]),
            candidate={
                "from_label": pair[0],
                "to_label": pair[1],
                "class_id": "class-" + digest(pair)[:24],
                "count": 4,
                "groups": 3,
            },
        )
        reservation = {
            "run_id": run,
            "plan_revision": digest(["generated-plan", idx]),
            "intake_revision": digest(["generated-intake", idx]),
            "residual_revision": digest(["generated-residual", idx]),
            "max_seconds": 15,
        }
        return str(self.mandates.reserve(MANDATE, reservation, actor=DELEGATE)["reservation_id"])

    def reserved_fits(self) -> int:
        return int(self.mandates.inspect(MANDATE)["cumulative_reserved_fits"])


FIT: dict[str, Any] = {}


@pytest.fixture(scope="module", autouse=True)
def _worker_fit_ledger(tmp_path_factory: pytest.TempPathFactory) -> Iterator[None]:
    world = FitWorld(tmp_path_factory.mktemp("generated-mandates"))
    FIT["world"] = world
    FIT["gate"] = FitGate(world.reserve(1), DELEGATE)
    yield
    FIT.clear()


def new_ledger(path: Path | str = ":memory:", world: FitWorld | None = None) -> VariantLedger:
    return VariantLedger(path, fit_authority=(world or FIT["world"]).mandates)


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
        fit_gate=FIT["gate"],
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
    third = ProposedChild("layout-v1", {"layout": "stacked"}, "FREIGHT")
    ledger.run(plan((PARAPHRASE, NO_PO, third)), VALIDATOR, now=NOW)
    kids = ledger.children()
    assert kids[2]["disposition"] == REJECTED and kids[2]["reason"] == "root_cap_exceeded"


def test_wrong_generator_label_is_quarantined_with_zero_fit_admission() -> None:
    ledger = new_ledger()
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

    ledger = new_ledger()
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

    ledger = new_ledger()
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
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE,)), None, now=NOW)
    (child,) = ledger.children()
    assert (child["disposition"], child["reason"]) == (HELD, "independent_validation_absent")
    m = manifest(ledger)
    assert m["admitted_count"] == 0 and m["held_count"] == 1


def test_self_validation_by_generator_is_rejected() -> None:
    ledger = new_ledger()
    result = ledger.submit(plan((PARAPHRASE,)), now=NOW)
    with pytest.raises(TrainingError, match="self_validation_rejected"):
        ledger.validate(result.child_ids[0], ValidatorDecision(GEN.ref, "FREIGHT"))
    assert ledger.children()[0]["disposition"] == HELD


def test_protected_development_and_cross_split_seeds_are_rejected() -> None:
    ledger = new_ledger()
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
    ledger = new_ledger(path)
    seed = root(7, family_id="generated-fam-protected", split="protected")
    ledger.run(plan((PARAPHRASE,), seed), VALIDATOR, now=NOW)
    assert b"generated carrier haulage sentinel" not in path.read_bytes()
    assert b"generated-po-0001" not in path.read_bytes()


def test_unverified_seeds_yield_no_usable_variant() -> None:
    ledger = new_ledger()
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
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE,), grant=grant), VALIDATOR, now=NOW)
    assert ledger.children()[0]["reason"] == reason


def test_unapproved_external_teacher_is_rejected_and_approved_one_is_allowed() -> None:
    ledger = new_ledger()
    teacher = Generator("generated-external-teacher", "r1", external=True)
    ledger.run(plan((PARAPHRASE,), generator=teacher), VALIDATOR, now=NOW)
    assert ledger.children()[0]["reason"] == "unapproved_external_teacher"
    ok = dataclasses.replace(GRANT, approved_external_teachers=frozenset({teacher.ref}))
    ledger.run(plan((LAYOUT,), generator=teacher, grant=ok), VALIDATOR, now=NOW)
    assert ledger.children()[1]["disposition"] == ADMITTED


def test_replay_adds_no_rows_and_no_spend_and_survives_restart(tmp_path: Path) -> None:
    path = tmp_path / "variants.sqlite3"
    props = (dataclasses.replace(PARAPHRASE, cost=7), dataclasses.replace(NO_PO, cost=5))
    first = new_ledger(path).run(plan(props), VALIDATOR, now=NOW)
    assert new_ledger(path).spent(GRANT.grant_ref) == 12
    restarted = new_ledger(path)  # simulated process restart
    again = restarted.run(plan(props), VALIDATOR, now=NOW)
    assert again.created == 0 and again.replayed == 2
    assert again.child_ids == first.child_ids
    assert len(restarted.children()) == 2
    assert restarted.spent(GRANT.grant_ref) == 12
    assert [c["disposition"] for c in restarted.children()] == [ADMITTED, ADMITTED]


def test_retry_after_partial_failure_completes_without_duplicates() -> None:
    ledger = new_ledger()
    first = ledger.submit(plan((PARAPHRASE, NO_PO)), now=NOW)  # crash before validation
    assert [c["disposition"] for c in ledger.children()] == [HELD, HELD]
    ledger.run(plan((PARAPHRASE, NO_PO)), VALIDATOR, now=NOW)
    assert len(ledger.children()) == 2
    assert [c["disposition"] for c in ledger.children()] == [ADMITTED, ADMITTED]
    assert first.child_ids == tuple(c["child_id"] for c in ledger.children())


def test_exhausted_authorization_stops_without_overspend() -> None:
    ledger = new_ledger()
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
    ledger = new_ledger()
    a = ProposedChild("paraphrase-v1", {"vendor_note": "Generated  Freight, wording!"}, "FREIGHT")
    b = ProposedChild("paraphrase-v1", {"vendor_note": "generated freight wording"}, "FREIGHT")
    ledger.run(plan((a, b)), VALIDATOR, now=NOW)
    assert [c["disposition"] for c in ledger.children()] == [ADMITTED, REJECTED]
    assert ledger.children()[1]["reason"] == "near_duplicate"


def test_one_mistake_plus_100_children_is_still_one_mistake_one_family() -> None:
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE,), fit_gate=CLOSED_GATE), VALIDATOR, now=NOW)
    assert ledger.children()[0]["reason"] == "fit_gate_closed"
    assert ledger.children()[0]["disposition"] == REJECTED
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["disposition"] == ADMITTED


def test_unresolved_rule_requests_one_owner_decision_instead_of_generating() -> None:
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE, NO_PO)), VALIDATOR, now=NOW)
    assert manifest(ledger)["admitted_count"] == 2
    ledger.retract_root("generated-root-1", "generated-fam-1", "revoked")
    m = manifest(ledger)
    assert m["admitted_count"] == 0 and m["invalidated_count"] == 2
    assert m["final_weights"] == {} and m["exclusion_reasons"] == {"root_revoked": 2}
    assert all(c["prior_disposition"] == ADMITTED for c in ledger.children())


def test_manifest_is_sealed_with_counts_and_exclusion_reasons() -> None:
    ledger = new_ledger()
    bad = ProposedChild("paraphrase-v1", {"payment": "generated-pay-9"}, "FREIGHT")
    wrong = ProposedChild("layout-v1", {"layout": "stacked"}, "REVIEW")
    ledger.run(plan((PARAPHRASE, bad, wrong)), VALIDATOR, now=NOW)
    m = manifest(ledger)
    # Formerly asserted a constant `sealed: True`; the seal is reported for what it is.
    assert "sealed" not in m and m["split_digest"] == SEAL.digest
    assert m["seal"] == {
        "kind": "digest_self_consistent_only",
        "signed": False,
        "digest_field": "manifest_digest",
    }
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    moved = root(1, family_id="generated-fam-2")
    ledger.run(plan((LAYOUT,), moved), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "root_identity_conflict"


def test_preview_does_not_block_or_consume_the_training_equivalent() -> None:
    """F1: preview must precede training without poisoning it."""
    ledger = new_ledger()
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
    ledger = new_ledger()
    base = ProposedChild("paraphrase-v1", {"vendor_note": "generated freight wording"}, "FREIGHT")
    twin = ProposedChild("paraphrase-v1", {"vendor_note": variant}, "FREIGHT")
    roomy = plan((base, twin), max_children_per_root=5, max_children_per_family=5)
    ledger.run(roomy, VALIDATOR, now=NOW)
    assert [c["disposition"] for c in ledger.children()] == [ADMITTED, REJECTED]
    assert ledger.children()[1]["reason"] == "near_duplicate"


def test_forged_validation_cannot_admit() -> None:
    """F3: authority comes from the stored plan, not from whatever the caller passes."""
    ledger = new_ledger()
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
    ledger = new_ledger()
    wrong = ProposedChild("no-po-v1", {"purchase_order": None}, "SUPPLIES")
    result = ledger.submit(plan((wrong,)), now=NOW)
    decision = ValidatorDecision("generated-independent-validator", "SUPPLIES")
    assert ledger.validate(result.child_ids[0], decision) == QUARANTINED
    assert ledger.children()[0]["reason"] == "validator_policy_disagreement"


def test_manifest_is_scoped_to_its_mode_policy_grant_and_seal() -> None:
    """F6: a sealed manifest cannot mix modes, grants or policy revisions."""
    ledger = new_ledger()
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
    ledger = new_ledger()
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

    ledger = new_ledger()
    ledger.run(plan((NO_PO,), policy=policy), Hostile(), now=NOW)
    assert ledger.children()[0]["disposition"] == ADMITTED
    assert policy.clauses["purchase_order"] == "REVIEW"


def test_a_corrected_label_is_a_new_row_and_the_quarantined_one_stays() -> None:
    """F8: resubmitting with the right label is not swallowed by the earlier wrong one."""
    ledger = new_ledger()
    wrong = ProposedChild(
        "paraphrase-v1", {"vendor_note": "generated corrected wording"}, "SUPPLIES"
    )
    right = dataclasses.replace(wrong, proposed_label="FREIGHT")
    ledger.run(plan((wrong,)), VALIDATOR, now=NOW)
    ledger.run(plan((right,)), VALIDATOR, now=NOW)
    assert [c["disposition"] for c in ledger.children()] == [QUARANTINED, ADMITTED]


def test_retryable_rejection_keeps_its_history_when_it_flips() -> None:
    """F8: re-evaluating a state-dependent rejection never erases the earlier outcome."""
    ledger = new_ledger()
    closed = plan((PARAPHRASE,), fit_gate=CLOSED_GATE)
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
    ledger = new_ledger(path)
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
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
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    with pytest.raises(TrainingError, match="root_identity_conflict"):
        ledger.retract_root("generated-root-1", "generated-fam-2", "reopened")
    assert ledger.children()[0]["disposition"] == ADMITTED
    assert ledger.retract_root("generated-root-1", "generated-fam-1", "reopened")


def test_records_are_json_serializable_but_domain_objects_are_not_asdict_safe() -> None:
    """N5: consumers serialize the ledger's JSON records, never the frozen domain objects."""
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    json.dumps(ledger.children())
    with pytest.raises(TypeError):
        dataclasses.asdict(POLICY)


def test_rotating_validators_then_correcting_the_plan_is_not_poisoned() -> None:
    """R1: a rejected attempt under a wrong pin must not block the corrected attempt."""
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    other = plan(
        (LAYOUT,), approved_validators=frozenset({"generated-other-validator"}), plan_ref="p-wrong"
    )
    ledger.run(other, VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "root_pin_mismatch"
    ledger.run(plan((LAYOUT,), plan_ref="p-right"), VALIDATOR, now=NOW)
    last = ledger.children()[-1]
    assert (last["disposition"], last["plan_ref"]) == (ADMITTED, "p-right")
    assert [c["reason"] for c in ledger.children()].count("root_pin_mismatch") == 1


def test_a_label_ref_equal_to_an_existing_case_ref_is_a_duplicate() -> None:
    """R3: case and label refs are compared against both columns."""
    ledger = new_ledger()
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    crossed = root(
        root_ref="generated-root-x",
        case_ref="generated-case-2",
        label_ref="generated-case-1",
        family_id="generated-fam-2",
    )
    ledger.run(plan((LAYOUT,), crossed, plan_ref="generated-plan-x"), VALIDATOR, now=NOW)
    flipped = root(
        root_ref="generated-root-y",
        case_ref="generated-case-3",
        label_ref="generated-label-1",
        family_id="generated-fam-3",
    )
    ledger.run(plan((LAYOUT,), flipped, plan_ref="generated-plan-y"), VALIDATOR, now=NOW)
    assert [c["reason"] for c in ledger.children()[1:]] == ["duplicate_case"] * 2
    assert manifest(ledger)["observed_mistake_count"] == 1


# ------------------------------------------- cross-session audit, batch 2 (retract, gate, trust)


class _Sneaky(str):
    """A look-alike identity that claims equality with everything."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return True

    def __hash__(self) -> int:
        return hash(str(self))


def test_a_caller_cannot_open_the_fit_without_a_worker_reservation() -> None:
    """The fit gate's state is read from the worker's mandate ledger, never from the caller."""
    forged = FitGate("fit-generated-forged", DELEGATE)
    bare = VariantLedger()  # no worker ledger wired in
    bare.run(plan((PARAPHRASE,), fit_gate=forged), VALIDATOR, now=NOW)
    (child,) = bare.children()
    assert (child["disposition"], child["reason"]) == (REJECTED, "fit_gate_closed")
    assert child["fit_gate"]["status"] == "no_worker_fit_authority"
    assert manifest(bare)["admitted_count"] == 0
    assert [f.name for f in dataclasses.fields(FitGate)] == ["reservation_id", "actor"]

    ledger = new_ledger()
    attempts = (
        (PARAPHRASE, forged, "fit_reservation_unavailable"),
        (LAYOUT, FitGate(FIT["gate"].reservation_id, OWNER), "standing_delegate_mismatch"),
        (NO_PO, None, "fit_reservation_absent"),
    )
    for n, (proposal, gate, _) in enumerate(attempts):
        ledger.run(
            plan((proposal,), fit_gate=gate, plan_ref=f"generated-plan-forged-{n}"),
            VALIDATOR,
            now=NOW,
        )
    kids = ledger.children()
    assert [c["reason"] for c in kids] == ["fit_gate_closed"] * 3
    assert [c["fit_gate"]["status"] for c in kids] == [want for *_, want in attempts]
    assert manifest(ledger)["admitted_count"] == 0 and ledger.experiments() == []

    @dataclasses.dataclass(frozen=True)
    class OpenGate:
        reservation_id: str = "fit-generated-forged"
        actor: str = DELEGATE
        open: bool = True

    with pytest.raises(TrainingError, match="invalid_fit_gate"):
        ledger.submit(plan((PARAPHRASE,), fit_gate=OpenGate()), now=NOW)

    class Permissive(LearningMandates):
        def grant(self, reservation_id: str, *, actor: str) -> dict[str, Any]:
            return {"reservation_id": reservation_id, "allowed_uses": ["train"]}

    with pytest.raises(TrainingError, match="fit_authority_must_be_worker_mandate_ledger"):
        VariantLedger(fit_authority=Permissive.__new__(Permissive))
    # The worker's own reservation, and only that, opens the same proposal.
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    assert ledger.children()[0]["disposition"] == ADMITTED


def test_variants_never_reserve_unlock_or_double_a_fit(tmp_path: Path) -> None:
    """100 children reserve nothing; a root's augmentation binds to one in-flight fit."""
    world = FitWorld(tmp_path)
    first = world.reserve(1)
    assert world.reserved_fits() == 1
    ledger = new_ledger(world=world)
    roomy = dataclasses.replace(GRANT, budget=1000)
    props = tuple(
        ProposedChild("paraphrase-v1", {"vendor_note": f"generated wording variant {i}"}, "FREIGHT")
        for i in range(100)
    )
    caps = {"max_children_per_root": 100, "max_children_per_family": 100}
    many = plan(props, fit_gate=FitGate(first, DELEGATE), grant=roomy, **caps)
    ledger.run(many, VALIDATOR, now=NOW)
    replay = ledger.run(many, VALIDATOR, now=NOW)
    assert replay.created == 0 and replay.replayed == 100
    m = manifest(ledger)
    assert m["admitted_count"] == 100 and m["observed_mistake_count"] == 1
    assert world.reserved_fits() == 1  # children never reserve or unlock a fit
    experiments = [(e["reservation_id"], e["state"], e["root_refs"]) for e in ledger.experiments()]
    assert experiments == [(first, "in_flight", ["generated-root-1"])]

    second = world.reserve(2)  # a second, genuinely reserved fit
    doubled = plan(
        (NO_PO,),
        fit_gate=FitGate(second, DELEGATE),
        grant=roomy,
        plan_ref="generated-plan-double",
        **caps,
    )
    ledger.run(doubled, VALIDATOR, now=NOW)
    last = ledger.children()[-1]
    assert (last["disposition"], last["reason"]) == (REJECTED, "fit_already_bound")
    assert ledger.spent(roomy.grant_ref) == 100  # the refused child is not charged
    assert len(ledger.experiments()) == 1

    # Another real root may join the same single fit: one more family, not one more fit.
    joined = plan(
        (PARAPHRASE,),
        root(2),
        fit_gate=FitGate(first, DELEGATE),
        grant=roomy,
        plan_ref="generated-plan-r2",
    )
    ledger.run(joined, VALIDATOR, now=NOW)
    (exp,) = ledger.experiments()
    assert exp["root_refs"] == ["generated-root-1", "generated-root-2"]
    assert world.reserved_fits() == 2  # only the mandate's own reserve() calls count
    assert manifest(ledger)["experiments"] == [
        {
            "reservation_id": first,
            "state": "in_flight",
            "reason": "",
            "root_refs": ["generated-root-1", "generated-root-2"],
        }
    ]


def test_a_reopened_root_stales_its_in_flight_experiment_and_pending_proposals(
    tmp_path: Path,
) -> None:
    """Retraction reaches experiments and pending proposals; earlier records are kept."""
    world = FitWorld(tmp_path)
    rid = world.reserve(1)
    gate = FitGate(rid, DELEGATE)
    ledger = new_ledger(world=world)
    small = dataclasses.replace(GRANT, budget=3)
    caps = {"max_children_per_root": 5, "max_children_per_family": 5}
    wrong = ProposedChild("layout-v1", {"layout": "generated-stacked"}, "REVIEW")
    ledger.run(plan((PARAPHRASE, wrong), fit_gate=gate, grant=small, **caps), VALIDATOR, now=NOW)
    invented = ProposedChild("paraphrase-v1", {"payment": "generated-pay-9"}, "FREIGHT")
    pending = plan(
        (NO_PO, invented, dataclasses.replace(LAYOUT, cost=5)),
        fit_gate=gate,
        grant=small,
        plan_ref="generated-plan-pending",
        **caps,
    )
    ledger.submit(pending, now=NOW)  # NO_PO held; LAYOUT stopped on exhausted authorization
    before_kids = {c["child_id"]: c for c in ledger.children()}
    assert [(c["disposition"], c["reason"]) for c in before_kids.values()] == [
        (ADMITTED, "validated"),
        (QUARANTINED, "label_disagreement"),
        (HELD, "independent_validation_absent"),
        (REJECTED, "invented_field"),
        (REJECTED, "authorization_exhausted"),
    ]
    before = manifest(ledger)
    frozen = copy.deepcopy(before)
    assert before["experiments"] == [
        {
            "reservation_id": rid,
            "state": "in_flight",
            "reason": "",
            "root_refs": ["generated-root-1"],
        }
    ]

    prior = ledger.retract_root("generated-root-1", "generated-fam-1", "reopened")

    kids = ledger.children()
    assert [c["disposition"] for c in kids] == [
        INVALIDATED,
        QUARANTINED,
        INVALIDATED,
        REJECTED,
        INVALIDATED,
    ]
    changed = (kids[0], kids[2], kids[4])
    assert prior == tuple(c["child_id"] for c in changed)
    assert [(c["prior_disposition"], c["prior_reason"], c["reason"]) for c in changed] == [
        (ADMITTED, "validated", "root_reopened"),
        (HELD, "independent_validation_absent", "root_reopened"),
        (REJECTED, "authorization_exhausted", "root_reopened"),
    ]
    assert all(c["history"][-1]["disposition"] == c["prior_disposition"] for c in changed)
    # Final outcomes are not rewritten.
    for kept in (kids[1], kids[3]):
        assert kept == before_kids[kept["child_id"]]
    # The in-flight experiment derived from the root is recorded stale, with its reason.
    (exp,) = ledger.experiments()
    assert (exp["reservation_id"], exp["state"], exp["reason"], exp["stale_root_ref"]) == (
        rid,
        "stale",
        "root_reopened",
        "generated-root-1",
    )
    assert exp["history"] == [{"state": "in_flight", "reason": ""}]
    # A late validation callback for the pending proposal cannot re-admit it.
    late = ledger.validate(
        kids[2]["child_id"], ValidatorDecision("generated-independent-validator", "REVIEW")
    )
    assert late == INVALIDATED
    # The earlier report is unchanged and still self-consistent; a new report shows the change.
    assert before == frozen
    assert (
        digest({k: v for k, v in before.items() if k != "manifest_digest"})
        == (before["manifest_digest"])
    )
    after = manifest(ledger)
    assert after["manifest_digest"] != before["manifest_digest"]
    assert after["experiments"] == [
        {
            "reservation_id": rid,
            "state": "stale",
            "reason": "root_reopened",
            "root_refs": ["generated-root-1"],
        }
    ]
    assert after["admitted_count"] == 0 and after["observed_mistake_count"] == 0
    assert after["invalidated_child_ids"] == sorted(prior)
    # No root can add children to the stale experiment, and nothing more is spent or reserved.
    spent = ledger.spent(small.grant_ref)
    ledger.run(
        plan((PARAPHRASE,), root(2), fit_gate=gate, grant=small, plan_ref="generated-plan-r2"),
        VALIDATOR,
        now=NOW,
    )
    assert ledger.children()[-1]["reason"] == "fit_experiment_stale"
    assert ledger.spent(small.grant_ref) == spent
    assert world.reserved_fits() == 1


def test_retraction_keeps_final_outcomes_and_their_reasons() -> None:
    """Only live children and pending proposals are invalidated; final outcomes stay."""
    ledger = VariantLedger()
    wrong = ProposedChild("layout-v1", {"layout": "generated-stacked"}, "REVIEW")
    invented = ProposedChild("paraphrase-v1", {"payment": "generated-pay-9"}, "FREIGHT")
    ledger.run(plan((wrong,), mode="preview", fit_gate=None), VALIDATOR, now=NOW)
    ledger.submit(
        plan((PARAPHRASE, invented), mode="preview", fit_gate=None, plan_ref="generated-plan-2"),
        now=NOW,
    )
    before = {c["child_id"]: c for c in ledger.children()}
    prior = ledger.retract_root("generated-root-1", "generated-fam-1", "revoked")
    kids = ledger.children()
    assert [c["disposition"] for c in kids] == [QUARANTINED, INVALIDATED, REJECTED]
    assert prior == (kids[1]["child_id"],)
    assert kids[0] == before[kids[0]["child_id"]] and kids[2] == before[kids[2]["child_id"]]
    assert (kids[1]["prior_disposition"], kids[1]["prior_reason"]) == (
        HELD,
        "independent_validation_absent",
    )


def test_manifest_seal_is_digest_self_consistent_only_and_unsigned() -> None:
    """The manifest reports exactly what its digest is; nothing claims a signed seal."""
    ledger = VariantLedger()
    ledger.run(plan((PARAPHRASE,), mode="preview", fit_gate=None), VALIDATOR, now=NOW)
    m = ledger.manifest(
        seal=SEAL, policy_revision=POLICY.revision, grant_ref=GRANT.grant_ref, mode="preview"
    )
    assert "sealed" not in m
    assert m["seal"] == {
        "kind": "digest_self_consistent_only",
        "signed": False,
        "digest_field": "manifest_digest",
    }
    body = {k: v for k, v in m.items() if k != "manifest_digest"}
    assert digest(body) == m["manifest_digest"]
    # Anyone can recompute it: a tampered body with a recomputed digest is equally consistent.
    forged = {**body, "preview_valid_count": 99}
    assert digest(forged) != m["manifest_digest"]
    assert m["trust"]["target_verified"] == "caller_asserted"
    assert m["trust"]["grant"] == "caller_asserted"
    assert m["trust"]["approved_validators"].startswith("caller_asserted")
    assert m["trust"]["verifier_independent"].startswith("caller_asserted")
    assert m["trust"]["fit_gate"] == "worker_ledger:learning_mandate_reservation"


@pytest.mark.parametrize(
    "change",
    [
        {"root": root(target_verified=1)},
        {"root": root(verifier_independent="yes")},
        {"root": root(root_ref=_Sneaky("generated-root-1"))},
        {"root": root(family_id=_Sneaky("generated-fam-1"))},
        {"root": root(target_verifier_ref=_Sneaky("generated-accountant"))},
        {"grant": dataclasses.replace(GRANT, training_authorized=1)},
        {"grant": dataclasses.replace(GRANT, generation_authorized="true")},
        {"grant": dataclasses.replace(GRANT, execution_location_approved=1)},
        {"grant": dataclasses.replace(GRANT, budget=True)},
        {"grant": dataclasses.replace(GRANT, grant_ref=_Sneaky("generated-grant-1"))},
        {"approved_validators": frozenset({_Sneaky("generated-independent-validator")})},
        {"approved_validators": {"generated-independent-validator"}},
        {"plan_ref": _Sneaky("generated-plan-1")},
        {"proposals": (dataclasses.replace(PARAPHRASE, cost=True),)},
    ],
    ids=[
        "target_verified_int",
        "verifier_independent_str",
        "root_ref_subclass",
        "family_id_subclass",
        "verifier_ref_subclass",
        "training_authorized_int",
        "generation_authorized_str",
        "location_int",
        "budget_bool",
        "grant_ref_subclass",
        "validator_subclass",
        "validators_mutable_set",
        "plan_ref_subclass",
        "cost_bool",
    ],
)
def test_caller_identities_and_flags_must_be_exact_types(change: dict[str, object]) -> None:
    """A truthy non-boolean or a str subclass can never stand in for a caller assertion."""
    ledger = VariantLedger()
    with pytest.raises(TrainingError):
        ledger.run(plan((PARAPHRASE,), mode="preview", fit_gate=None, **change), VALIDATOR, now=NOW)
    assert ledger.children() == []


def test_a_look_alike_validator_identity_is_refused() -> None:
    ledger = VariantLedger()
    result = ledger.submit(plan((PARAPHRASE,), mode="preview", fit_gate=None), now=NOW)
    for decision in (
        ValidatorDecision(_Sneaky("generated-independent-validator"), "FREIGHT"),
        ValidatorDecision("generated-independent-validator", _Sneaky("FREIGHT")),
    ):
        with pytest.raises(TrainingError, match="invalid_identity_type"):
            ledger.validate(result.child_ids[0], decision)
    with pytest.raises(TrainingError, match="invalid_identity_type"):
        ledger.validate(_Sneaky(result.child_ids[0]), None)
    assert ledger.children()[0]["disposition"] == HELD


def test_caller_assertions_alone_never_make_a_child_fit_eligible() -> None:
    """Every caller-asserted input at its most permissive still needs worker-held evidence."""
    lavish = dataclasses.replace(
        GRANT, budget=10**9, approved_external_teachers=frozenset({GEN.ref})
    )
    # (1) Without the worker-held reservation nothing is admitted, whatever the flags say.
    bare = VariantLedger()
    bare.run(plan((PARAPHRASE,), grant=lavish), VALIDATOR, now=NOW)
    assert bare.children()[0]["reason"] == "fit_gate_closed"
    assert manifest(bare)["admitted_count"] == 0
    # (2) verifier_independent=True is refuted when the verifier is the generator or the fit's
    #     delegate/trainer, as recorded by the worker's mandate ledger.
    ledger = new_ledger()
    for n, who in enumerate((GEN.ref, DELEGATE, " " + DELEGATE.upper())):
        seed = root(n + 1, target_verifier_ref=who, verifier_independent=True)
        ledger.run(
            plan((PARAPHRASE,), seed, grant=lavish, plan_ref=f"generated-plan-v{n}"),
            VALIDATOR,
            now=NOW,
        )
    assert [c["reason"] for c in ledger.children()] == ["verifier_not_independent"] * 3

    # (3) An approved validator that rubber-stamps the generator's label cannot admit it: the
    #     ledger's own policy re-derivation disagrees.
    class RubberStamp:
        def validate(self, request: ValidationRequest) -> ValidatorDecision:
            return ValidatorDecision("generated-independent-validator", "REVIEW")

    stamped = ProposedChild("layout-v1", {"layout": "generated-stamped"}, "REVIEW")
    ledger.run(
        plan((stamped,), grant=lavish, plan_ref="generated-plan-stamp"), RubberStamp(), now=NOW
    )
    last = ledger.children()[-1]
    assert (last["disposition"], last["reason"]) == (QUARANTINED, "validator_policy_disagreement")
    assert manifest(ledger)["admitted_count"] == 0
