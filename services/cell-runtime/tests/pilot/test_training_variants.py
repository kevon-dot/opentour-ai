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
import os
import shutil
import sqlite3
import time
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from opentour_cell.pilot import learning_mandate, open_weight
from opentour_cell.pilot.learning_mandate import LearningMandates, MandateError
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


# --------------------------------------- cross-session review of PR #225 (N1, G3, L1, survivors)

AGREE_FREIGHT = ValidatorDecision("generated-independent-validator", "FREIGHT")
AGREE_REVIEW = ValidatorDecision("generated-independent-validator", "REVIEW")


def _world(base: Path, name: str) -> FitWorld:
    directory = base / name
    directory.mkdir(mode=0o700)
    return FitWorld(directory)


class _NeverDiffers(str):
    """A look-alike value for which `!=` is always False."""

    __slots__ = ()

    def __eq__(self, other: object) -> bool:
        return True

    def __ne__(self, other: object) -> bool:
        return False

    def __hash__(self) -> int:
        return hash(str(self))


def test_a_stale_shared_experiment_cannot_admit_a_co_member_held_child(tmp_path: Path) -> None:
    """N1a: root-1 and root-2 share one fit; retracting root-1 stops root-2's held child."""
    world = FitWorld(tmp_path)
    rid = world.reserve(1)
    gate = FitGate(rid, DELEGATE)
    ledger = new_ledger(world=world)
    ledger.run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    shared = plan((PARAPHRASE,), root(2), fit_gate=gate, plan_ref="generated-plan-r2")
    held = ledger.submit(shared, now=NOW).child_ids[0]
    ledger.retract_root("generated-root-1", "generated-fam-1", "reopened")
    (child,) = ledger.children("generated-root-2")
    assert (child["disposition"], child["reason"], child["prior_disposition"]) == (
        REJECTED,
        "fit_experiment_stale",
        HELD,
    )
    assert ledger.validate(held, AGREE_FREIGHT) == REJECTED  # the late callback admits nothing
    assert manifest(ledger)["admitted_count"] == 0
    (exp,) = ledger.experiments()
    assert exp["state"] == "stale" and exp["reheld_child_ids"] == [held]
    # Only in-flight experiments count as a binding: the still-permitted root may move to a
    # fresh reservation, and its re-held child is re-evaluated in place.
    fresh = world.reserve(2)
    moved = plan(
        (PARAPHRASE,), root(2), fit_gate=FitGate(fresh, DELEGATE), plan_ref="generated-plan-r2b"
    )
    ledger.run(moved, VALIDATOR, now=NOW)
    (child,) = ledger.children("generated-root-2")
    assert (child["child_id"], child["disposition"]) == (held, ADMITTED)
    assert child["history"][-1]["reason"] == "fit_experiment_stale"
    states = [(e["reservation_id"], e["state"]) for e in ledger.experiments()]
    assert states == sorted([(rid, "stale"), (fresh, "in_flight")])
    assert manifest(ledger)["admitted_count"] == 1


def test_admission_rechecks_the_bound_experiment_state(tmp_path: Path) -> None:
    """N1: an experiment staled out of band is still refused at admission."""
    world = _world(tmp_path, "mandates")
    path = tmp_path / "variants.sqlite3"
    ledger = new_ledger(path, world)
    held = ledger.submit(plan((PARAPHRASE,), fit_gate=FitGate(world.reserve(1), DELEGATE)), now=NOW)
    db = sqlite3.connect(path)
    db.execute("UPDATE experiments SET state='stale', reason='generated-out-of-band'")
    db.commit()
    db.close()
    assert ledger.validate(held.child_ids[0], AGREE_FREIGHT) == REJECTED
    assert ledger.children()[0]["reason"] == "fit_experiment_stale"
    assert manifest(ledger)["admitted_count"] == 0


def test_a_withdrawn_mandate_cannot_admit_a_held_child(tmp_path: Path) -> None:
    """N1b: the reservation is re-read at admission, so withdrawal holds the child."""
    world = FitWorld(tmp_path)
    ledger = new_ledger(world=world)
    held = ledger.submit(plan((PARAPHRASE,), fit_gate=FitGate(world.reserve(1), DELEGATE)), now=NOW)
    world.mandates.withdraw(MANDATE, actor=OWNER)
    assert ledger.validate(held.child_ids[0], AGREE_FREIGHT) == REJECTED
    (child,) = ledger.children()
    assert child["reason"] == "fit_gate_closed"
    assert child["history"][-1] == {
        "disposition": HELD,
        "reason": "independent_validation_absent",
        "plan_ref": "generated-plan-1",
    }
    assert manifest(ledger)["admitted_count"] == 0


def test_co_member_admitted_children_of_a_stale_experiment_leave_fit_counts(
    tmp_path: Path,
) -> None:
    """N1: already-admitted co-member children are marked stale, history untouched."""
    world = FitWorld(tmp_path)
    rid = world.reserve(1)
    gate = FitGate(rid, DELEGATE)
    ledger = new_ledger(world=world)
    ledger.run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    ledger.run(
        plan((PARAPHRASE,), root(2), fit_gate=gate, plan_ref="generated-plan-r2"),
        VALIDATOR,
        now=NOW,
    )
    (before,) = ledger.children("generated-root-2")
    assert (before["disposition"], before["weight"]) == (ADMITTED, "1")
    assert manifest(ledger)["admitted_count"] == 2
    ledger.retract_root("generated-root-1", "generated-fam-1", "reopened")
    (after,) = ledger.children("generated-root-2")
    assert (after["disposition"], after["reason"]) == (ADMITTED, "validated")
    assert after.get("history") == before.get("history")
    assert after["fit_stale"] == {"reservation_id": rid, "reason": "root_reopened"}
    assert (after["weight"], after["prior_weight"]) == (None, "1")
    m = manifest(ledger)
    assert (m["admitted_count"], m["stale_admitted_count"]) == (0, 1)
    assert m["stale_admitted_child_ids"] == [after["child_id"]]
    assert m["final_weights"] == {}
    assert m["family_sibling_counts"] == {} and m["root_sibling_counts"] == {}
    assert m["observed_mistake_count"] == 1  # root-2 itself is still a live real root
    (exp,) = ledger.experiments()
    assert exp["stale_admitted_child_ids"] == [after["child_id"]]


def test_a_copied_mandate_store_cannot_stand_in_for_the_pinned_one(tmp_path: Path) -> None:
    """G3a/a': copy the store, withdraw the original; the copy is refused, also on restart."""
    world = _world(tmp_path, "mandates")
    rid = world.reserve(1)
    gate = FitGate(rid, DELEGATE)
    path = tmp_path / "variants.sqlite3"
    new_ledger(path, world).run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    pending = plan((NO_PO,), fit_gate=gate, plan_ref="generated-plan-held")
    held = new_ledger(path, world).submit(pending, now=NOW).child_ids[0]
    shutil.copytree(world.mandates.directory, tmp_path / "copy")
    copied = LearningMandates(tmp_path / "copy", "generated-org", world.mandates.engagement)
    world.mandates.withdraw(MANDATE, actor=OWNER)
    # The copy still answers as an active mandate, so only the pin stops it.
    assert LearningMandates.grant(copied, rid, actor=DELEGATE)["reservation_id"] == rid
    assert VariantLedger(path, fit_authority=copied).validate(held, AGREE_REVIEW) == REJECTED
    assert VariantLedger(path).children()[1]["reason"] == "fit_authority_mismatch"
    for n, layout in enumerate(("generated-copy-a", "generated-copy-b")):  # each a restart
        over_copy = VariantLedger(path, fit_authority=copied)
        attempt = ProposedChild("layout-v1", {"layout": layout}, "FREIGHT")
        over_copy.run(
            plan((attempt,), fit_gate=gate, plan_ref=f"generated-plan-copy-{n}"), VALIDATOR, now=NOW
        )
        assert over_copy.children()[-1]["reason"] == "fit_authority_mismatch"
    original = new_ledger(path, world)
    late = ProposedChild("layout-v1", {"layout": "generated-late"}, "FREIGHT")
    original.run(plan((late,), fit_gate=gate, plan_ref="generated-plan-late"), VALIDATOR, now=NOW)
    assert original.children()[-1]["fit_gate"]["status"] == "standing_mandate_withdrawn"
    # F3: the original is withdrawn, so its one honest admission is stale, not counted.
    m = manifest(original)
    assert (m["admitted_count"], m["stale_admitted_count"]) == (0, 1)


def test_a_store_replaced_at_the_same_path_is_refused(tmp_path: Path) -> None:
    """G3b: an older snapshot moved back, or rm -rf and recreate, at the same path."""
    base = tmp_path / "mandates"
    base.mkdir(mode=0o700)
    world = FitWorld(base)
    rid = world.reserve(1)
    gate = FitGate(rid, DELEGATE)
    path = tmp_path / "variants.sqlite3"
    ledger = new_ledger(path, world)
    ledger.run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    held = ledger.submit(plan((NO_PO,), fit_gate=gate, plan_ref="generated-plan-held"), now=NOW)
    shutil.copytree(base, tmp_path / "snapshot")
    world.mandates.withdraw(MANDATE, actor=OWNER)
    shutil.rmtree(base)
    shutil.move(tmp_path / "snapshot", base)  # same path, organization and engagement
    restored = LearningMandates(base, "generated-org", world.mandates.engagement)
    assert LearningMandates.grant(restored, rid, actor=DELEGATE)["reservation_id"] == rid
    again = VariantLedger(path, fit_authority=restored)
    assert again.validate(held.child_ids[0], AGREE_REVIEW) == REJECTED
    assert again.children()[1]["reason"] == "fit_authority_mismatch"
    shutil.rmtree(base)
    base.mkdir(mode=0o700)
    fresh = FitWorld(base)
    recreated = VariantLedger(path, fit_authority=fresh.mandates)
    recreated.run(
        plan(
            (LAYOUT,), fit_gate=FitGate(fresh.reserve(1), DELEGATE), plan_ref="generated-plan-new"
        ),
        VALIDATOR,
        now=NOW,
    )
    assert recreated.children()[-1]["reason"] == "fit_authority_mismatch"
    # F3: a substituted store cannot verify the honest admission, so it is not counted.
    m = manifest(recreated)
    assert (m["admitted_count"], m["unverified_admitted_count"]) == (0, 1)


def test_an_instance_level_grant_override_is_refused(tmp_path: Path) -> None:
    """G3c: an exact LearningMandates whose instance shadows `grant` is not the worker ledger."""
    world = FitWorld(tmp_path)
    ledger = new_ledger(world=world)
    held = ledger.submit(plan((PARAPHRASE,), fit_gate=FitGate(world.reserve(1), DELEGATE)), now=NOW)

    def forged(reservation_id: str, *, actor: str) -> dict[str, Any]:
        return {"reservation_id": reservation_id, "allowed_uses": ["train"], "delegate": actor}

    vars(world.mandates)["grant"] = forged
    with pytest.raises(TrainingError, match="fit_authority_must_be_worker_mandate_ledger"):
        VariantLedger(fit_authority=world.mandates)
    assert ledger.validate(held.child_ids[0], AGREE_FREIGHT) == REJECTED
    assert ledger.children()[0]["reason"] == "fit_authority_mismatch"
    invented = plan(
        (LAYOUT,), fit_gate=FitGate("fit-generated-forged", DELEGATE), plan_ref="generated-plan-f"
    )
    ledger.run(invented, VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "fit_authority_mismatch"
    assert manifest(ledger)["admitted_count"] == 0


def _sql(path: Path, statement: str, *args: object) -> None:
    db = sqlite3.connect(path)
    try:
        db.execute(statement, args)
        db.commit()
    finally:
        db.close()


def _pinned(path: Path) -> str | None:
    db = sqlite3.connect(path)
    try:
        row = db.execute("SELECT authority_identity FROM authority_pin").fetchone()
    finally:
        db.close()
    return None if row is None else str(row[0])


def test_a_rewritten_or_cleared_pin_cannot_hand_experiments_to_another_store(
    tmp_path: Path,
) -> None:
    """G3 fault injection: each experiment records its store; a lost pin is never re-pinned."""
    world = _world(tmp_path, "mandates")
    gate = FitGate(world.reserve(1), DELEGATE)
    path = tmp_path / "variants.sqlite3"
    ledger = new_ledger(path, world)
    ledger.run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    held = ledger.submit(plan((NO_PO,), fit_gate=gate, plan_ref="generated-plan-held"), now=NOW)
    shutil.copytree(world.mandates.directory, tmp_path / "copy")
    copied = LearningMandates(tmp_path / "copy", "generated-org", world.mandates.engagement)
    scratch = tmp_path / "scratch.sqlite3"
    VariantLedger(scratch, fit_authority=copied).submit(plan((LAYOUT,), fit_gate=gate), now=NOW)
    copy_identity = _pinned(scratch)
    assert copy_identity is not None and copy_identity != _pinned(path)
    # (1) The pin is rewritten to the copy: the experiment's own recorded store still refuses.
    _sql(path, "UPDATE authority_pin SET authority_identity=?", copy_identity)
    over_copy = VariantLedger(path, fit_authority=copied)
    assert over_copy.validate(held.child_ids[0], AGREE_REVIEW) == REJECTED
    assert over_copy.children()[1]["reason"] == "fit_authority_mismatch"
    moved = ProposedChild("layout-v1", {"layout": "generated-rewritten"}, "FREIGHT")
    over_copy.submit(plan((moved,), fit_gate=gate, plan_ref="generated-plan-rw"), now=NOW)
    last = over_copy.children()[-1]  # refused at bind, before any admission check
    assert (last["disposition"], last["reason"]) == (REJECTED, "fit_authority_mismatch")
    # (2) The pin is deleted: with experiments present that is a mismatch, never a fresh pin.
    _sql(path, "DELETE FROM authority_pin")
    cleared = ProposedChild("layout-v1", {"layout": "generated-cleared"}, "FREIGHT")
    over_copy.run(plan((cleared,), fit_gate=gate, plan_ref="generated-plan-cl"), VALIDATOR, now=NOW)
    assert over_copy.children()[-1]["reason"] == "fit_authority_mismatch"
    assert _pinned(path) is None
    # F3: with the pin gone nothing can be verified, so even the honest admission is excluded.
    m = manifest(over_copy)
    assert (m["admitted_count"], m["unverified_admitted_count"]) == (0, 1)
    assert _pinned(path) is None  # reading a manifest never re-pins either


def test_one_ledger_file_refuses_another_engagement_and_a_copied_ledger(tmp_path: Path) -> None:
    """G3d: a restart over another engagement's store, or a copied ledger file, is refused."""
    first, second = _world(tmp_path, "a"), _world(tmp_path, "b")
    ra, rb = first.reserve(1), second.reserve(1)
    path = tmp_path / "variants.sqlite3"
    new_ledger(path, first).run(
        plan((PARAPHRASE,), fit_gate=FitGate(ra, DELEGATE)), VALIDATOR, now=NOW
    )
    assert first.mandates.engagement != second.mandates.engagement
    assert LearningMandates.grant(second.mandates, rb, actor=DELEGATE)["reservation_id"] == rb
    restarted = new_ledger(path, second)
    restarted.run(
        plan((PARAPHRASE,), root(2), fit_gate=FitGate(rb, DELEGATE), plan_ref="generated-plan-b"),
        VALIDATOR,
        now=NOW,
    )
    assert restarted.children()[-1]["reason"] == "fit_authority_mismatch"
    copied = tmp_path / "variants-copy.sqlite3"
    shutil.copy2(path, copied)
    clone = new_ledger(copied, first)
    clone.run(
        plan((LAYOUT,), fit_gate=FitGate(ra, DELEGATE), plan_ref="generated-plan-copy"),
        VALIDATOR,
        now=NOW,
    )
    assert clone.children()[-1]["reason"] == "fit_authority_mismatch"
    m = manifest(new_ledger(path, first))
    assert m["admitted_count"] == 1
    assert [e["reservation_id"] for e in m["experiments"]] == [ra]


@pytest.mark.parametrize("tamper", ["drop_train_scope", "other_reservation"])
def test_a_permission_outside_training_scope_or_for_another_reservation_is_refused(
    monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    """R1/R2: the worker's permission must name this reservation and allow training."""
    real = LearningMandates.grant

    def narrowed(self: LearningMandates, reservation_id: str, *, actor: str) -> dict[str, Any]:
        doc = dict(real(self, reservation_id, actor=actor))
        if tamper == "drop_train_scope":
            doc["allowed_uses"] = ["inspect", "evaluate"]
        else:
            doc["reservation_id"] = "fit-generated-other"
        return doc

    ledger = new_ledger()
    held = ledger.submit(plan((PARAPHRASE,)), now=NOW).child_ids[0]  # bound while permitted
    monkeypatch.setattr(LearningMandates, "grant", narrowed)
    ledger.run(plan((LAYOUT,), plan_ref="generated-plan-narrowed"), VALIDATOR, now=NOW)
    last = ledger.children()[-1]
    assert (last["reason"], last["fit_gate"]["status"]) == (
        "fit_gate_closed",
        "fit_reservation_outside_training_scope",
    )
    assert ledger.validate(held, AGREE_FREIGHT) == REJECTED
    assert ledger.children()[0]["reason"] == "fit_gate_closed"
    assert manifest(ledger)["admitted_count"] == 0


@pytest.mark.parametrize("actor", [_Sneaky("generated-not-the-delegate"), 7], ids=["sub", "int"])
def test_a_look_alike_or_non_string_fit_actor_is_refused(actor: object) -> None:
    """R9: the gate's actor is an exact str, so a look-alike cannot pass the delegate check."""
    ledger = new_ledger()
    gate = FitGate(FIT["gate"].reservation_id, actor)  # type: ignore[arg-type]
    with pytest.raises(TrainingError, match="invalid_identity_type"):
        ledger.run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    assert ledger.children() == [] and ledger.experiments() == []


def test_a_blocked_or_retracted_seed_binds_no_experiment() -> None:
    """R10: only an eligible, live root can be bound to a fit."""
    ledger = new_ledger()
    blocked = (
        root(7, family_id="generated-fam-protected", split="protected"),
        root(2, verifier_independent=False),
    )
    for n, seed in enumerate(blocked):
        ledger.run(plan((PARAPHRASE,), seed, plan_ref=f"generated-plan-b{n}"), VALIDATOR, now=NOW)
    assert ledger.experiments() == []
    ledger.retract_root("generated-root-1", "generated-fam-1", "revoked")
    ledger.run(plan((PARAPHRASE,)), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "root_invalidated"
    assert ledger.experiments() == []


def test_manifest_experiments_are_scoped_to_their_roots(tmp_path: Path) -> None:
    """R14: a manifest lists only experiments whose roots are in its scope."""
    world = FitWorld(tmp_path)
    r1, r2 = world.reserve(1), world.reserve(2)
    ledger = new_ledger(world=world)
    other = dataclasses.replace(GRANT, grant_ref="generated-grant-2")
    ledger.run(plan((PARAPHRASE,), fit_gate=FitGate(r1, DELEGATE)), VALIDATOR, now=NOW)
    ledger.run(
        plan((PARAPHRASE,), root(2), grant=other, fit_gate=FitGate(r2, DELEGATE), plan_ref="g2"),
        VALIDATOR,
        now=NOW,
    )
    assert len(ledger.experiments()) == 2
    assert [e["reservation_id"] for e in manifest(ledger)["experiments"]] == [r1]
    g2 = ledger.manifest(
        seal=SEAL, policy_revision=POLICY.revision, grant_ref="generated-grant-2", mode="training"
    )
    assert [e["reservation_id"] for e in g2["experiments"]] == [r2]
    preview = ledger.manifest(
        seal=SEAL, policy_revision=POLICY.revision, grant_ref=GRANT.grant_ref, mode="preview"
    )
    assert preview["experiments"] == []


def test_a_never_differs_policy_revision_cannot_pass_the_revision_check() -> None:
    """L1: with a non-exact str, `seed.policy_revision != policy.revision` would be False."""
    sneaky = _NeverDiffers("generated-policy-r9")
    assert not (POLICY.revision != sneaky)  # the attack the exact-type check closes
    ledger = VariantLedger()
    forged = dataclasses.replace(POLICY, revision=sneaky)
    with pytest.raises(TrainingError, match="invalid_identity_type"):
        ledger.run(
            plan((PARAPHRASE,), mode="preview", fit_gate=None, policy=forged), VALIDATOR, now=NOW
        )
    assert ledger.children() == []


@pytest.mark.parametrize(
    "change",
    [
        {"root": root(fields={**root().fields, "layout": _NeverDiffers("table")})},
        {"proposals": (ProposedChild("paraphrase-v1", {"vendor_note": _NeverDiffers("x")}, "F"),)},
        {"proposals": (dataclasses.replace(PARAPHRASE, proposed_label=_NeverDiffers("FREIGHT")),)},
        {"proposals": (dataclasses.replace(PARAPHRASE, recipe_id=_NeverDiffers("paraphrase-v1")),)},
        {
            "policy": dataclasses.replace(
                POLICY, clauses={"purchase_order": _NeverDiffers("REVIEW")}
            )
        },
        {"policy": dataclasses.replace(POLICY, decision_fields=list(POLICY.decision_fields))},
        {"seal": dataclasses.replace(SEAL, digest=_NeverDiffers(SEAL.digest))},
        {
            "seal": SplitSeal(
                {_NeverDiffers("generated-fam-1"): ("training",)}, SEAL.cases, SEAL.digest
            )
        },
    ],
    ids=[
        "root_field",
        "edit_value",
        "proposed_label",
        "recipe_id",
        "clause",
        "fields_list",
        "seal_digest",
        "seal_family_key",
    ],
)
def test_policy_root_and_edit_values_must_be_exact_types(change: dict[str, object]) -> None:
    """L1: every compared value is an exact str (or tuple of them)."""
    ledger = VariantLedger()
    with pytest.raises(TrainingError):
        ledger.run(plan((PARAPHRASE,), mode="preview", fit_gate=None, **change), VALIDATOR, now=NOW)
    assert ledger.children() == []


# ------------------------------------------- re-review of PR #225 at 4f04a9b (F1, F3, survivors)


def test_a_ledger_whose_first_training_submit_was_closed_is_still_pinned(tmp_path: Path) -> None:
    """F1 repro: pinned at construction, so a store copied before withdrawal cannot take over."""
    world = _world(tmp_path, "mandates")
    gate = FitGate(world.reserve(1), DELEGATE)
    shutil.copytree(world.mandates.directory, tmp_path / "copy")
    copied = LearningMandates(tmp_path / "copy", "generated-org", world.mandates.engagement)
    world.mandates.withdraw(MANDATE, actor=OWNER)
    path = tmp_path / "variants.sqlite3"
    first = VariantLedger(path, fit_authority=world.mandates)
    assert _pinned(path) is not None  # pinned before any bind
    first.run(plan((PARAPHRASE,), mode="preview", fit_gate=None), VALIDATOR, now=NOW)
    first.run(plan((LAYOUT,), fit_gate=gate, plan_ref="generated-plan-t"), VALIDATOR, now=NOW)
    assert first.children()[-1]["reason"] == "fit_gate_closed"
    restarted = VariantLedger(path, fit_authority=copied)
    restarted.run(
        plan((PARAPHRASE,), root(2), fit_gate=gate, plan_ref="generated-plan-r2"),
        VALIDATOR,
        now=NOW,
    )
    last = restarted.children()[-1]
    assert (last["disposition"], last["reason"]) == (REJECTED, "fit_authority_mismatch")
    assert manifest(restarted)["admitted_count"] == 0


def test_a_ledger_with_rows_but_no_pin_is_never_pinned(tmp_path: Path) -> None:
    """F1, fail-closed choice: rows written without an authority mean no later pin."""
    world = _world(tmp_path, "mandates")
    path = tmp_path / "variants.sqlite3"
    VariantLedger(path).run(plan((PARAPHRASE,), mode="preview", fit_gate=None), VALIDATOR, now=NOW)
    later = VariantLedger(path, fit_authority=world.mandates)
    assert _pinned(path) is None
    later.run(plan((LAYOUT,), fit_gate=FitGate(world.reserve(1), DELEGATE)), VALIDATOR, now=NOW)
    assert later.children()[-1]["reason"] == "fit_authority_mismatch"
    assert later.experiments() == []


def test_withdrawal_after_admission_stales_the_experiment_in_the_manifest(tmp_path: Path) -> None:
    """F3: the manifest re-reads each bound reservation; a closed one is not counted."""
    world = FitWorld(tmp_path)
    rid = world.reserve(1)
    ledger = new_ledger(world=world)
    ledger.run(plan((PARAPHRASE, NO_PO), fit_gate=FitGate(rid, DELEGATE)), VALIDATOR, now=NOW)
    before = ledger.children()
    assert manifest(ledger)["admitted_count"] == 2
    world.mandates.withdraw(MANDATE, actor=OWNER)
    m = manifest(ledger)
    assert (m["admitted_count"], m["stale_admitted_count"]) == (0, 2)
    assert m["final_weights"] == {} and m["unverified_experiments"] == []
    assert m["experiments"] == [
        {
            "reservation_id": rid,
            "state": "stale",
            "reason": "fit_gate_closed",
            "root_refs": ["generated-root-1"],
        }
    ]
    (exp,) = ledger.experiments()
    assert exp["gate_status"] == "standing_mandate_withdrawn"
    assert exp["stale_admitted_child_ids"] == [c["child_id"] for c in before]
    for was, now in zip(before, ledger.children(), strict=True):
        assert (now["disposition"], now["reason"], now.get("history")) == (
            was["disposition"],
            was["reason"],
            was.get("history"),
        )
        assert now["fit_stale"] == {"reservation_id": rid, "reason": "fit_gate_closed"}
    assert manifest(ledger)["manifest_digest"] == m["manifest_digest"]  # recorded once


def test_another_engagement_over_the_same_store_files_is_a_mismatch(tmp_path: Path) -> None:
    """Identity covers engagement and scope, not just the files."""
    world = _world(tmp_path, "mandates")
    gate = FitGate(world.reserve(1), DELEGATE)
    path = tmp_path / "variants.sqlite3"
    ledger = new_ledger(path, world)
    ledger.run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    held = ledger.submit(plan((NO_PO,), fit_gate=gate, plan_ref="generated-plan-held"), now=NOW)
    other = LearningMandates(
        world.mandates.directory, "generated-org", "generated-other-engagement", owner=OWNER
    )
    assert (other.path, other.directory) == (world.mandates.path, world.mandates.directory)
    swapped = VariantLedger(path, fit_authority=other)
    swapped.run(plan((LAYOUT,), fit_gate=gate, plan_ref="generated-plan-o"), VALIDATOR, now=NOW)
    assert swapped.children()[-1]["reason"] == "fit_authority_mismatch"
    assert swapped.validate(held.child_ids[0], AGREE_REVIEW) == REJECTED
    assert swapped.children()[1]["reason"] == "fit_authority_mismatch"


def test_a_replaced_training_store_file_is_a_mismatch(tmp_path: Path) -> None:
    """Identity covers training.sqlite3: a same-content replacement is another store."""
    world = _world(tmp_path, "mandates")
    gate = FitGate(world.reserve(1), DELEGATE)
    path = tmp_path / "variants.sqlite3"
    new_ledger(path, world).run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    training = Path(world.mandates.directory) / "training.sqlite3"
    snapshot = tmp_path / "training-snapshot.sqlite3"
    shutil.copy2(training, snapshot)
    os.replace(snapshot, training)  # same content, engagement and path; a different file
    again = new_ledger(path, world)
    again.run(plan((LAYOUT,), fit_gate=gate, plan_ref="generated-plan-t"), VALIDATOR, now=NOW)
    assert again.children()[-1]["reason"] == "fit_authority_mismatch"


def test_an_authority_whose_mandate_file_is_elsewhere_is_never_pinned(tmp_path: Path) -> None:
    """A store whose mandate file is not inside its directory is no consistent identity."""
    world = _world(tmp_path, "mandates")
    rid = world.reserve(1)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    shutil.copy2(world.mandates.path, elsewhere / "learning-mandates.sqlite3")
    world.mandates.withdraw(MANDATE, actor=OWNER)  # the real store now refuses
    vars(world.mandates)["path"] = elsewhere / "learning-mandates.sqlite3"
    # The stray copy still answers, so only the consistency check stops it.
    assert LearningMandates.grant(world.mandates, rid, actor=DELEGATE)["reservation_id"] == rid
    path = tmp_path / "variants.sqlite3"
    ledger = VariantLedger(path, fit_authority=world.mandates)
    assert _pinned(path) is None
    ledger.run(plan((PARAPHRASE,), fit_gate=FitGate(rid, DELEGATE)), VALIDATOR, now=NOW)
    assert ledger.children()[-1]["reason"] == "fit_authority_mismatch"
    assert manifest(ledger)["admitted_count"] == 0


# --------------------------------------------- re-review of PR #225 at f737f5f (F4, F1e)


def _admitted_and_held(world: FitWorld) -> tuple[VariantLedger, FitGate, str, str]:
    rid = world.reserve(1)
    gate = FitGate(rid, DELEGATE)
    ledger = new_ledger(world=world)
    ledger.run(plan((PARAPHRASE,), fit_gate=gate), VALIDATOR, now=NOW)
    held = ledger.submit(plan((NO_PO,), fit_gate=gate, plan_ref="generated-plan-held"), now=NOW)
    return ledger, gate, rid, held.child_ids[0]


def test_a_locked_mandate_store_is_unverified_and_fully_recovers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4 repro: a manifest read during lock contention must not stale a valid experiment."""
    world = FitWorld(tmp_path)
    ledger, gate, rid, held = _admitted_and_held(world)
    before = ledger.children()
    original = sqlite3.connect

    def quick(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        if str(args[0]).endswith("learning-mandates.sqlite3"):
            kwargs["timeout"] = 0.05  # the store's own 10 s wait, shortened for the test
        return original(*args, **kwargs)

    blocker = original(world.mandates.path, isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with monkeypatch.context() as mp:
            mp.setattr(sqlite3, "connect", quick)
            m = manifest(ledger)
            counts = (
                m["admitted_count"],
                m["unverified_admitted_count"],
                m["stale_admitted_count"],
            )
            assert counts == (0, 1, 0)
            assert m["unverified_experiments"] == [rid]
            assert ledger.validate(held, AGREE_REVIEW) == HELD  # deferred, not refused
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert ledger.children() == before  # nothing durable was written during the outage
    (exp,) = ledger.experiments()
    assert (exp["state"], exp["reason"]) == ("in_flight", "")
    m = manifest(ledger)
    assert (m["admitted_count"], m["unverified_admitted_count"]) == (1, 0)
    assert ledger.validate(held, AGREE_REVIEW) == ADMITTED
    ledger.run(
        plan((PARAPHRASE,), root(2), fit_gate=gate, plan_ref="generated-plan-r2"),
        VALIDATOR,
        now=NOW,
    )
    assert ledger.children("generated-root-2")[0]["disposition"] == ADMITTED
    assert manifest(ledger)["admitted_count"] == 3


@pytest.mark.parametrize("failure", ["os_error", "sqlite_error", "unknown_code"])
def test_a_failing_mandate_store_is_unverified_never_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    """F4: OSError, sqlite3.Error or an unknown code from the store writes nothing."""
    world = FitWorld(tmp_path)
    ledger, _, _, held = _admitted_and_held(world)
    before = ledger.children()

    def broken_guard(self: LocalTrainingStore, *args: Any, **kwargs: Any) -> Any:
        raise OSError("generated-io-failure")

    def broken_db(self: LearningMandates, *, write: bool = True) -> Any:
        raise sqlite3.DatabaseError("generated-corrupt-store")

    def odd_grant(self: LearningMandates, reservation_id: str, *, actor: str) -> dict[str, Any]:
        raise MandateError("generated-unknown-code")

    with monkeypatch.context() as mp:
        if failure == "os_error":
            mp.setattr(LocalTrainingStore, "engagement_guard", broken_guard)
        elif failure == "sqlite_error":
            mp.setattr(LearningMandates, "_db", broken_db)
        else:
            mp.setattr(LearningMandates, "grant", odd_grant)
        m = manifest(ledger)
        assert (m["admitted_count"], m["unverified_admitted_count"]) == (0, 1)
        assert ledger.validate(held, AGREE_REVIEW) == HELD
    assert ledger.children() == before
    assert ledger.experiments()[0]["state"] == "in_flight"
    assert manifest(ledger)["admitted_count"] == 1
    assert ledger.validate(held, AGREE_REVIEW) == ADMITTED


def test_a_real_withdrawal_still_stales_while_an_outage_does_not(tmp_path: Path) -> None:
    """F4: a definitive closure from the pinned store is still persisted."""
    world = FitWorld(tmp_path)
    ledger, _, _, held = _admitted_and_held(world)
    world.mandates.withdraw(MANDATE, actor=OWNER)
    m = manifest(ledger)
    assert (m["admitted_count"], m["stale_admitted_count"], m["unverified_admitted_count"]) == (
        0,
        1,
        0,
    )
    (exp,) = ledger.experiments()
    assert (exp["state"], exp["gate_status"]) == ("stale", "standing_mandate_withdrawn")
    reheld = [c for c in ledger.children() if c["child_id"] == held]
    assert (reheld[0]["disposition"], reheld[0]["reason"]) == (REJECTED, "fit_experiment_stale")


def _assert_manifest_stales(ledger: VariantLedger, rid: str, held: str, status: str) -> None:
    """The manifest persists a definitive closure: stale experiment, nothing counted."""
    before = ledger.children()
    m = manifest(ledger)
    counts = (m["admitted_count"], m["stale_admitted_count"], m["unverified_admitted_count"])
    assert counts == (0, 1, 0)
    assert m["unverified_experiments"] == []
    assert m["experiments"] == [
        {
            "reservation_id": rid,
            "state": "stale",
            "reason": "fit_gate_closed",
            "root_refs": ["generated-root-1"],
        }
    ]
    (exp,) = ledger.experiments()
    assert (exp["state"], exp["reason"], exp["gate_status"]) == ("stale", "fit_gate_closed", status)
    admitted = [c["child_id"] for c in before if c["disposition"] == ADMITTED]
    assert exp["stale_admitted_child_ids"] == admitted
    reheld = [c for c in ledger.children() if c["child_id"] == held]
    assert (reheld[0]["disposition"], reheld[0]["reason"]) == (REJECTED, "fit_experiment_stale")
    assert manifest(ledger)["manifest_digest"] == m["manifest_digest"]  # recorded once


def test_an_expired_mandate_stales_the_experiment_in_the_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F4f: `standing_mandate_expired` from the pinned store is a definitive closure."""
    world = FitWorld(tmp_path)
    ledger, _, rid, held = _admitted_and_held(world)
    assert manifest(ledger)["admitted_count"] == 1
    later = int(time.time()) + 7200  # past the generated mandate's one-hour expiry
    monkeypatch.setattr(learning_mandate, "time", types.SimpleNamespace(time=lambda: later))
    with pytest.raises(MandateError, match="^standing_mandate_expired$"):
        LearningMandates.grant(world.mandates, rid, actor=DELEGATE)
    _assert_manifest_stales(ledger, rid, held, "standing_mandate_expired")


def test_a_delegate_mismatch_stales_the_experiment_in_the_manifest(tmp_path: Path) -> None:
    """F4g: `standing_delegate_mismatch` from the pinned store is a definitive closure.

    The generated mandate row is rewritten in place to name another delegate, so the
    store's own grant path refuses the experiment's recorded actor.
    """
    world = FitWorld(tmp_path)
    ledger, _, rid, held = _admitted_and_held(world)
    assert manifest(ledger)["admitted_count"] == 1
    db = sqlite3.connect(world.mandates.path, isolation_level=None)
    try:
        (spec_json,) = db.execute("SELECT spec FROM mandates WHERE id=?", (MANDATE,)).fetchone()
        spec = json.loads(spec_json)
        spec["delegate"] = "generated-other-delegate"
        db.execute("UPDATE mandates SET spec=? WHERE id=?", (json.dumps(spec), MANDATE))
    finally:
        db.close()
    with pytest.raises(MandateError, match="^standing_delegate_mismatch$"):
        LearningMandates.grant(world.mandates, rid, actor=DELEGATE)
    _assert_manifest_stales(ledger, rid, held, "standing_delegate_mismatch")


def test_a_ledger_holding_only_seed_rejected_children_is_never_pinned(tmp_path: Path) -> None:
    """F1e: any row, including a rejected child with no root, blocks a later pin."""
    world = _world(tmp_path, "mandates")
    path = tmp_path / "variants.sqlite3"
    protected = root(7, family_id="generated-fam-protected", split="protected")
    VariantLedger(path).run(plan((PARAPHRASE,), protected), VALIDATOR, now=NOW)
    db = sqlite3.connect(path)
    try:
        counts = [
            db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("roots", "children")
        ]
    finally:
        db.close()
    assert counts == [0, 1]  # only the seed-rejected child
    later = VariantLedger(path, fit_authority=world.mandates)
    assert _pinned(path) is None
    gate = FitGate(world.reserve(1), DELEGATE)
    later.run(plan((LAYOUT,), fit_gate=gate, plan_ref="generated-plan-later"), VALIDATOR, now=NOW)
    assert later.children()[-1]["reason"] == "fit_authority_mismatch"
