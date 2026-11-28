"""OPE-528 x OPE-571: an ``independent_verdict`` verdict consumed by ``comparison_packet``.

The evaluator is handed only the OPE-571 slice-substituted operating point (every slice name
already a keyed digest) and never the slice key or a plain name. Its verdict must declare
``operating_point_digest_form: "slice_substituted.v1"`` over exactly the digest the packet
recomputes from the comparison under the engagement key, and its ``denominator`` must carry the
comparison's own ``protected_membership_digest`` form (``comparison_plan``). Then the packet
assembles from it on the hold and the improvement path, and every other hold still applies.

Generated sentinel fixtures only: comparisons are real ``comparison_plan`` runs over generated
rows, the evaluator scores the same generated protected cohort, and the slice names are the
generated ``SLICE_NAMES`` sentinels. These tests prove software behavior only and establish no
provider, customer or separated-custody evidence.
"""

from __future__ import annotations

import copy
import hashlib
from types import SimpleNamespace
from typing import NamedTuple

import pytest
from opentour_cell.pilot import comparison_packet as cp
from opentour_cell.pilot import comparison_plan
from opentour_cell.pilot import independent_verdict as iv
from opentour_cell.pilot.operating_point import digest
from opentour_cell.pilot.simple_repair import RepairError
from test_comparison_packet import (
    KEYS,
    SLICE_NAMES,
    STRICT,
    auto,
    compare,
    context,
    empty,
    in_another_engagement,
    key_for,
    mentions,
    reseal_verdict,
    rows,
    scope,
)
from test_independent_verdict import (
    BENCHMARK,
    WORKER,
    A,
    B,
    Replay,
    Scenario,
    evaluator,
    receipt_for,
)
from test_independent_verdict import claim as iv_claim
from test_independent_verdict import policy as iv_policy

COHORT = 200  # enough protected cases for the evaluator's family interval to clear
HOLD_TERMS = {**STRICT, "minimum_verified_cases": 50}
LABEL = {"a": A, "b": B}  # plan labels to the evaluator's generated label set


class Run(NamedTuple):
    comparison: dict
    cohort: list  # the generated protected rows the plan froze


def named_run(root, *, critical=SLICE_NAMES, hold=False, material=(), size=None):
    """A real comparison over generated rows whose terms name the sentinel slices.

    The improvement run has ``COHORT`` protected cases, every incumbent decision wrong and the
    frozen simple rule right, so the plan recommends ``simple``. The hold run has four cases
    under a verified-case minimum of 50.
    """
    root.mkdir(mode=0o700, parents=True)
    size = (4 if hold else COHORT) if size is None else size
    dev, protected = rows("dev"), rows("protected", ("a",) * size)
    for r in dev + protected:
        r["slices"] = list(SLICE_NAMES)
    terms = {**(HOLD_TERMS if hold else STRICT), "critical_slices": list(critical)}
    if material:
        terms["material_slices"] = list(material)
    comparison = compare(
        root,
        development=dev,
        protected=protected,
        terms=terms,
        allowed_slices=list(SLICE_NAMES),
    )[0]
    recommends = comparison["decision"]["disposition"] == "recommend"
    assert recommends is not hold, comparison["decision"]
    assert recommends is False or comparison["decision"]["winner"] == "simple"
    assert comparison["protected_rows_evaluated"] == size
    return Run(comparison, protected)


def substituted(comparison, key=None):
    """What the customer worker hands the evaluator: names replaced by their keyed digests."""
    return cp.slice_substituted_terms(
        comparison["terms"], key or key_for(comparison), **scope(comparison)
    )


def population_for(cohort, critical):
    """The evaluator's sealed population over exactly ``cohort``'s case ids and answers."""
    cases, labels = [], {}
    preds = {"candidate": {}, "incumbent": {}}
    for i, r in enumerate(cohort):
        case_id = r["case_id"]
        cases.append(
            iv.DecisionCase(
                case_id,
                r["group_id"],
                {"case_ref": case_id, "text": f"generated-input-{i}"},
                tuple(critical),
            )
        )
        labels[case_id] = LABEL[r["reference"]]
        incumbent = r["incumbent"]
        preds["incumbent"][case_id] = {**incumbent, "label": LABEL[incumbent["label"]]}
        preds["candidate"][case_id] = auto(A)  # the frozen simple rule maps x -> a
    population = iv.SealedPopulation(
        cases,
        labels,
        label_set=(A, B),
        benchmark_digest=BENCHMARK,
        reference_fields=("case_ref",),
    )
    return SimpleNamespace(population=population, preds=preds, comparators=("incumbent",))


class Issued:
    """One verdict issued by the real evaluator for a claim bound to ``run``'s comparison."""

    def __init__(self, run, root, *, terms=None, point=None, critical=None, cohort=None):
        comparison = run.comparison
        arms = comparison["arms"]
        self.terms = substituted(comparison) if terms is None else terms
        self.point = cp.operating_point_digest(self.terms) if point is None else point
        if critical is None:
            critical = tuple(self.terms.get("critical_slices", []))
        self.policy = iv_policy(
            operating_point_terms=self.terms,
            operating_point_digest=self.point,
            critical_slices=critical,
            required_comparators=("incumbent",),
        )
        s = population_for(run.cohort if cohort is None else cohort, critical)
        self.population = s.population
        self.receipt = receipt_for(s.population)
        self.ctx = context(comparison, benchmark_digest=s.population.benchmark_digest)
        assert self.ctx.worker_id == WORKER
        self.ev = evaluator(root)
        self.verdict = self.ev.evaluate(
            claim=iv_claim(
                s,
                pol=self.policy,
                result_revision=comparison["evidence_revision"],
                candidate_kind="simple_repair",
                candidate_digest=arms["simple"]["revision"],
                comparator_digests={"incumbent": arms["incumbent"]["revision"]},
                job_manifest_digest=None,
            ),
            population=s.population,
            audit_receipt=self.receipt,
            policy=self.policy,
            replays={
                "candidate": Replay(arms["simple"]["revision"], s.preds["candidate"]),
                "incumbent": Replay(arms["incumbent"]["revision"], s.preds["incumbent"]),
            },
            fit_manifest=None,
        )
        self.doc = self.verdict.document


def assemble(run, issued, verdict=None, *, fixture=True):
    """Improvement staging needs the generated-fixture flag to get past the issuer hold."""
    return cp.assemble_packet(
        run.comparison,
        issued.receipt,
        issued.doc if verdict is None else verdict,
        issued.ctx,
        slice_key=key_for(run.comparison),
        allow_generated_fixture=fixture,
    )


def stage(run, issued, root, verdict=None, *, fixture=False):
    """Stage through an outbox; LIVE (no fixture flag) unless asked, as production builds it."""
    root.mkdir(mode=0o700, parents=True)
    store = cp.PacketOutbox(
        root / "outbox.sqlite3", allow_generated_fixture=fixture, slice_keys=KEYS
    )

    class _Authority:
        def current(self, run_ref):
            return issued.ctx if run_ref == issued.ctx.run_ref else None

    out = store.stage(
        run.comparison,
        issued.receipt,
        issued.doc if verdict is None else verdict,
        authority=_Authority(),
        run_ref=issued.ctx.run_ref,
        now=1_000,
    )
    return out, store


# --------------------------------------------------------------------- the producer's contract


def test_528_verdict_binds_the_packets_operating_point_and_denominator_forms(tmp_path):
    run = named_run(tmp_path / "run")
    issued = Issued(run, tmp_path / "eval")
    doc, comparison = issued.doc, run.comparison
    assert doc["outcome"] == "verified_gain" and doc["verdict"] == "reproduced", doc["reasons"]
    assert doc["operating_point_digest_form"] == cp.OPERATING_POINT_DIGEST_FORM
    assert doc["operating_point_digest"] == cp.operating_point_digest(substituted(comparison))
    # Exactly the dict the packet compares, in the plan's own membership form.
    assert doc["denominator"] == {
        "protected_rows": comparison["protected_rows_evaluated"],
        "membership_digest": comparison["protected_membership_digest"],
    }
    ids = [r["case_id"] for r in run.cohort]
    assert doc["denominator"]["membership_digest"] == (
        comparison_plan.protected_membership_digest(ids)
    )
    # The domain-tagged form stays for the claim binding and is a different value.
    assert doc["sealed_membership_digest"] == issued.population.membership_digest
    assert doc["sealed_membership_digest"] != doc["denominator"]["membership_digest"]
    assert cp._verdict_ok(doc)


def test_plan_membership_digest_is_order_independent_and_plan_identifiers_only():
    ids = [f"protected-{i}" for i in range(12)]
    expected = comparison_plan.protected_membership_digest(ids)
    assert comparison_plan.protected_membership_digest(reversed(ids)) == expected
    assert comparison_plan.protected_membership_digest(ids[1:]) != expected
    with pytest.raises(RepairError):
        comparison_plan.protected_membership_digest(["generated@case-0001"])
    # An evaluator id the plan could never freeze has no plan-form membership at all.
    odd = Scenario([("WR", 4)], comparators=("incumbent",), prefix="generated@")
    assert odd.population.plan_membership_digest is None


# ---------------------------------------------------------------- the improvement path, positive


def test_packet_assembles_and_verifies_a_528_verdict_on_the_improvement_path(tmp_path):
    run = named_run(tmp_path / "run")
    issued = Issued(run, tmp_path / "eval")
    packet, reason = assemble(run, issued)
    assert reason is None, reason
    assert packet["kind"] == "measured_improvement"
    assert packet["decision"]["winner"] == "simple"
    assert packet["audit"]["independent_verdict_digest"] == issued.doc["verdict_digest"]
    assert packet["comparison"]["operating_point_digest"] == issued.doc["operating_point_digest"]
    assert cp.verify_packet(
        packet,
        run.comparison,
        issued.receipt,
        issued.doc,
        issued.ctx,
        slice_key=key_for(run.comparison),
        allow_generated_fixture=True,
    )
    out, store = stage(run, issued, tmp_path / "fixture", fixture=True)
    assert out.status == "staged" and out.packet == packet, out
    # What a staged improvement packet still withholds: no authority of any kind.
    assert packet["authority"] == {"release": False, "delivery": False}
    assert packet["next_action"] == "separate_human_decision_required"
    assert packet["audit"]["issuer_authenticated"] is False
    assert packet["audit"]["integrity"] == "digest_self_consistent_only"
    assert packet["critical_slice_names"]["issuer_authenticated"] is False
    assert packet["claim"] == {
        "evidence_tier": "generated",
        "scope": "implementation_only",
        "provider_or_customer_evidence": False,
        "accuracy_claim_eligible": False,
    }


def test_every_other_hold_still_applies_to_an_accepted_528_verdict(tmp_path):
    """Assembly grants nothing: the issuer hold, D11 and ``authorize`` still hold the gain."""
    assert iv.REGISTERED_POLICY_FLOOR is not None
    assert iv.REGISTERED_POLICY_FLOOR.floor_id == "d11.v1"
    run = named_run(tmp_path / "run")
    issued = Issued(run, tmp_path / "eval")
    # OPE-526/OPE-566: no issuer can be verified, so live staging always holds.
    assert assemble(run, issued, fixture=False) == (None, "audit_and_verdict_issuers_unverified")
    out, store = stage(run, issued, tmp_path / "live")
    assert out.status == "refused" and out.reason == "audit_and_verdict_issuers_unverified"
    assert out.scoped_hold["release_authority"] is False and empty(store)
    # D11: this fixture's preregistered policy is laxer than the registered d11.v1 floor, so no
    # consumer may act on the gain even though it is reproduced.
    doc = issued.doc
    assert doc["verdict"] == "reproduced"
    assert iv.REGISTERED_POLICY_FLOOR.shortfalls(doc["policy"]) == [
        "min_families",
        "max_coverage_loss_bps",
        "operating_point_terms.minimum_verified_cases",
        "operating_point_terms.minimum_slice_cases",
    ]
    assert doc["alert_gates"]["policy_floor_hold"] == "policy_floor_below_registered_floor"
    assert not any(doc["effects"].values())
    for effect in ("improvement_alert", "release_request", "handoff_request"):
        refused = issued.ev.authorize(doc, effect=effect)
        assert refused["authorized"] is False
        assert refused["reason"] == "policy_below_registered_floor"
    # A copy the evaluator did not store digests fine, but ``authorize`` refuses it.
    forged = reseal_verdict({**doc, "producer_id": "generated-other-worker"})
    assert cp._verdict_ok(forged)
    assert issued.ev.authorize(forged, effect="improvement_alert") == {
        "authorized": False,
        "effect": "improvement_alert",
        "reason": "verdict_document_mismatch",
        "state": "unverified",
    }


def test_packet_accepts_a_528_verdict_on_the_hold_path(tmp_path):
    run = named_run(tmp_path / "run", hold=True)
    issued = Issued(run, tmp_path / "eval")
    assert issued.doc["verdict"] == "not_reproduced"  # four cases under a minimum of 50
    packet, reason = assemble(run, issued, fixture=False)
    assert reason is None and packet["kind"] == "hold", reason
    assert packet["audit"]["independent_verdict_digest"] == issued.doc["verdict_digest"]
    assert cp.verify_packet(
        packet,
        run.comparison,
        issued.receipt,
        issued.doc,
        issued.ctx,
        slice_key=key_for(run.comparison),
    )
    out, _ = stage(run, issued, tmp_path / "live")
    assert out.status == "staged" and out.packet == packet, out


# ------------------------------------------------------------------- adverse: the denominator


def test_a_verdict_over_a_cohort_differing_by_one_case_is_refused(tmp_path):
    run = named_run(tmp_path / "run")
    dropped = Issued(run, tmp_path / "eval-drop", cohort=run.cohort[1:])
    assert dropped.doc["verdict"] == "reproduced"  # a gain, but on another cohort
    assert assemble(run, dropped) == (None, "denominator_changed")
    swapped_rows = copy.deepcopy(run.cohort)
    swapped_rows[0]["case_id"] = "protected-substitute"
    swapped = Issued(run, tmp_path / "eval-swap", cohort=swapped_rows)
    assert swapped.doc["denominator"]["protected_rows"] == COHORT  # same count, other ids
    assert assemble(run, swapped) == (None, "denominator_changed")
    out, store = stage(run, dropped, tmp_path / "stage", fixture=True)
    assert out.reason == "denominator_changed" and empty(store)


def test_an_edited_and_resealed_membership_digest_is_refused(tmp_path):
    run = named_run(tmp_path / "run")
    honest = Issued(run, tmp_path / "eval")
    for denominator in (
        {**honest.doc["denominator"], "membership_digest": honest.population.membership_digest},
        {**honest.doc["denominator"], "membership_digest": digest("generated-other-cohort")},
        {**honest.doc["denominator"], "protected_rows": COHORT - 1},
        {**honest.doc["denominator"], "membership_digest": None},
    ):
        edited = reseal_verdict({**honest.doc, "denominator": denominator})
        assert cp._verdict_ok(edited)
        assert assemble(run, honest, edited) == (None, "denominator_changed"), denominator
    assert assemble(run, honest)[1] is None


# ------------------------------------------------------------- adverse: the operating point


def test_old_form_528_verdict_is_still_refused(tmp_path):
    for name, hold in (("improve", False), ("hold", True)):
        run = named_run(tmp_path / name, hold=hold)
        issued = Issued(run, tmp_path / f"eval-{name}")
        legacy = reseal_verdict(
            {k: v for k, v in issued.doc.items() if k != "operating_point_digest_form"}
        )
        assert cp._verdict_ok(legacy)
        assert assemble(run, issued, legacy) == (
            None,
            "operating_point_digest_predates_slice_digests",
        )
        out, store = stage(run, issued, tmp_path / f"stage-{name}", legacy, fixture=True)
        assert out.reason == "operating_point_digest_predates_slice_digests" and empty(store)


def test_tampered_slice_digest_is_refused(tmp_path):
    run = named_run(tmp_path / "run")
    terms = substituted(run.comparison)
    terms["critical_slices"] = [digest("generated-tampered-slice"), *terms["critical_slices"][1:]]
    # The evaluator sees a self-consistent operating point (format and digest both check)...
    issued = Issued(run, tmp_path / "eval", terms=terms)
    assert iv.operating_point_binding(issued.terms, issued.point) is None
    assert issued.doc["verdict"] == "reproduced"
    # ...but the packet recomputes from the comparison under the key and refuses it.
    assert assemble(run, issued) == (None, "verdict_binding_mismatch")
    # A digest edited inside an otherwise honest, resealed document is refused the same way.
    honest = Issued(run, tmp_path / "eval-honest")
    edited = reseal_verdict({**honest.doc, "operating_point_digest": issued.point})
    assert assemble(run, honest, edited) == (None, "verdict_binding_mismatch")
    # And a digest over the RAW terms is the same refusal, never a distinguishing code.
    raw_point = digest(run.comparison["terms"])
    raw = reseal_verdict({**honest.doc, "operating_point_digest": raw_point})
    assert assemble(run, honest, raw) == (None, "verdict_binding_mismatch")
    hold = named_run(tmp_path / "hold", hold=True)
    held = Issued(
        hold,
        tmp_path / "eval-hold",
        terms=substituted(hold.comparison) | {"critical_slices": terms["critical_slices"]},
    )
    assert assemble(hold, held, fixture=False) == (None, "verdict_binding_mismatch")


def test_verdict_from_a_different_slice_set_is_refused(tmp_path):
    run = named_run(tmp_path / "run")
    other = named_run(tmp_path / "other", critical=SLICE_NAMES[:1], hold=True).comparison
    assert other["terms"]["critical_slices"] != run.comparison["terms"]["critical_slices"]
    issued = Issued(run, tmp_path / "eval", terms=substituted(other))
    assert issued.doc["operating_point_digest"] == cp.operating_point_digest(substituted(other))
    assert assemble(run, issued) == (None, "verdict_binding_mismatch")
    # Same names under another engagement's key are a different slice-digest set too.
    moved = substituted(in_another_engagement(run.comparison))
    assert moved["critical_slices"] != substituted(run.comparison)["critical_slices"]
    elsewhere = Issued(run, tmp_path / "eval-b", terms=moved)
    assert assemble(run, elsewhere) == (None, "verdict_binding_mismatch")
    # The slice veto must gate exactly the bound set: gating another is never reproduced, so
    # the improvement path refuses it.
    gated = Issued(run, tmp_path / "eval-g", critical=tuple(substituted(other)["critical_slices"]))
    assert gated.doc["verdict"] == "not_reproduced"
    assert "critical_slices_not_bound_to_operating_point" in gated.doc["reasons"]
    assert assemble(run, gated) == (None, "independent_verdict_not_reproduced")
    # Material slices are part of the substituted set as well.
    material = named_run(tmp_path / "mat", critical=SLICE_NAMES[:1], material=SLICE_NAMES[1:])
    mixed = Issued(material, tmp_path / "eval-m", terms=substituted(other))
    assert assemble(material, mixed) == (None, "verdict_binding_mismatch")
    assert assemble(material, Issued(material, tmp_path / "eval-m2"))[1] is None


# ---------------------------------------------------------------------------- leak properties


def test_evaluator_refuses_unsubstituted_slice_names_and_publishes_none(tmp_path):
    """The evaluator never issues a document carrying a name it can tell is not substituted."""
    run = named_run(tmp_path / "run", hold=True)
    raw = copy.deepcopy(run.comparison["terms"])
    with pytest.raises(iv.VerdictInputError, match="operating_point_slice_names_not_substituted"):
        Issued(run, tmp_path / "eval", terms=raw)
    verdicts = tmp_path / "eval" / "evaluator-custody" / "verdicts"
    assert list(verdicts.iterdir()) == []


def test_no_slice_name_or_key_appears_in_the_528_verdict_or_the_packet(tmp_path):
    run = named_run(tmp_path / "run", critical=SLICE_NAMES[:1], material=SLICE_NAMES[1:])
    issued = Issued(run, tmp_path / "eval")
    packet, reason = assemble(run, issued)
    assert reason is None and packet["kind"] == "measured_improvement", reason
    key = key_for(run.comparison)
    for name in SLICE_NAMES:
        assert mentions(issued.doc, name) == [], name
        assert mentions(packet, name) == [], name
        for guess in (hashlib.sha256(name.encode()).hexdigest(), digest(name)):
            assert mentions([issued.doc, packet], guess) == [], name
    assert mentions([issued.doc, packet], key.hex()) == []
    custody = tmp_path / "eval" / "evaluator-custody"
    for path in custody.rglob("*.json"):
        data = path.read_bytes()
        assert not any(n.encode() in data for n in SLICE_NAMES), path
        assert key.hex().encode() not in data, path
