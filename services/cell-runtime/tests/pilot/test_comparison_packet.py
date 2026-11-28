"""OPE-421 verified comparison packet and exactly-one worker completion event.

Generated sentinel fixtures only. Comparisons are real ``comparison_plan`` runs over generated
rows. The audit receipt follows the OPE-526 receipt shape and the verdict follows the proposed
OPE-528 shape; both are generated here and prove nothing about a landed audit or an independent
verifier. The sink and authority are test doubles. These tests prove software behavior only and
establish no provider, Zapier, Slack or customer evidence.

Acceptance IDs (A*, R*, V*, E*, C*, L*) are this ticket's own labels derived from the live spec;
they are not bound in the approved specification manifest.
"""

import copy
import json
import re
import subprocess
import sys
import time

import pytest
from opentour_cell.pilot import comparison_packet as cp
from opentour_cell.pilot.comparison_plan import RESULT, development_hold, freeze_plan
from opentour_cell.pilot.comparison_plan import run_comparison as run_plan
from opentour_cell.pilot.operating_point import canonical, digest
from opentour_cell.pilot.simple_repair import assess_development, declared_rule
from opentour_cell.pilot.training import LocalTrainingStore

NOW = 1_000
STRICT = {"minimum_coverage_bps": 10000, "error_ceiling_bps": 0}


def auto(label):
    return {"status": "automatic", "label": label}


def rows(prefix, refs=("a", "a", "a", "a")):
    return [
        {
            "case_id": f"{prefix}-{i}",
            "group_id": f"family-{prefix}-{i}",
            "features": {"category": "x"},
            "slices": ["critical"],
            "reference": ref,
            "incumbent": auto("b"),
            "outcome_id": f"out-{prefix}-{i}",
            "adjudicator_id": "independent",
            "verified_at": 100 + i,
        }
        for i, ref in enumerate(refs)
    ]


def enroll(store, development=None, protected=None, allowed_slices=None):
    dev = rows("dev") if development is None else development
    test = rows("protected") if protected is None else protected
    data = {
        "schema_version": "opentour.pilot.source-intake.v1",
        "organization": store.organization,
        "engagement": store.engagement_id,
        "sources": {
            "trace": {"records_digest": digest("events")},
            "correction": {"records_digest": digest("corrections")},
        },
        "authority_revision": digest("owner"),
        "policy_version": "v3",
        "source_schema_version": "v5",
        "protected_membership_digest": digest(sorted(r["case_id"] for r in test)),
        "training_allowed": True,
        "training_expires_at": int(time.time()) + 7200,
        "expires_at": int(time.time()) + 7200,
        "allowed_features": ["category"],
        "allowed_slices": ["critical", "rare"] if allowed_slices is None else allowed_slices,
        "error_class": {"from_label": "b", "to_label": "a"},
        "split": {"method": "time", "after": 90},
        "excluded_counts": {"missing_outcome": 1},  # a real source_intake reason
        "development": dev,
        "protected": test,
    }
    return data | {"intake_revision": digest(data)}


def compare(
    root,
    *,
    protected=None,
    development=None,
    terms=None,
    mapping=None,
    model=None,
    measured=None,
    incumbent=None,
    allowed_slices=None,
):
    store = LocalTrainingStore(root / "training.sqlite3", "generated")
    data = enroll(
        store, development=development, protected=protected, allowed_slices=allowed_slices
    )
    terms = STRICT if terms is None else terms
    rule = declared_rule(
        feature="category", allowed_features=["category"], mapping=mapping or {"x": "a"}
    )
    dev = assess_development(
        data["development"],
        rule,
        terms=terms,
        model_authorized=model is not None,
        compute_authorized=model is not None,
    )
    plan = freeze_plan(
        organization=data["organization"],
        engagement=data["engagement"],
        audience="reviewer",
        intake_revision=data["intake_revision"],
        protected_ids=[r["case_id"] for r in data["protected"]],
        incumbent_revision=digest("incumbent") if incumbent is None else incumbent,
        rule=rule,
        development_decision=dev,
        terms=terms,
        model_revision=digest("frozen-model") if model is not None else None,
        model_predictions=model,
        model_budget_revision=digest("permitted-cap") if model is not None else None,
        measurements=measured,
    )
    return run_plan(
        store,
        plan,
        data,
        rule=rule,
        model_predictions=model,
        measurements=measured,
        authorize=lambda: None,
    ), plan


def seal(comparison):
    body = {k: v for k, v in comparison.items() if k != "evidence_revision"}
    return {**body, "evidence_revision": digest(body)}


def meter(count, cases):
    return {
        "basis": "observed",
        "permission_ref": "generated-permit",
        "provenance_ref": "generated-meter",
        "hardware": "cpu",
        "period_start": 1,
        "period_end": 2,
        "currency": "USD",
        "unit": "cpu_second",
        "rate_per_unit": "0.1",
        "cases": {r["case_id"]: {"usage": count} for r in cases},
    }


@pytest.fixture(scope="module")
def runs(tmp_path_factory):
    def at(name):
        path = tmp_path_factory.mktemp(name)
        return path

    out = {}
    out["winner"] = compare(at("winner"))[0]
    out["hand"] = compare(
        at("hand"),
        protected=rows("protected", ("a",) * 6 + ("b",)),
        terms={"minimum_coverage_bps": 10000, "error_ceiling_bps": 2000},
    )[0]
    dev = rows("dev")
    for r in dev:
        r["features"]["category"] = "uncorrected"
    hidden = rows("hidden")
    for r in hidden[:2]:
        r["features"]["category"] = "uncorrected"
    predicted = {r["case_id"]: auto("a") for r in hidden}
    metered = {
        arm: meter(n, hidden) for arm, n in (("incumbent", "5"), ("simple", "1"), ("model", "3"))
    }
    out["tie"] = compare(
        at("tie"),
        development=dev,
        protected=hidden,
        terms={"minimum_coverage_bps": 10000, "error_ceiling_bps": 5000},
        mapping={"x": "a", "uncorrected": "b"},
        model=predicted,
        measured=metered,
    )[0]
    review = {r["case_id"]: {"status": "review"} for r in hidden}
    out["coverage_loss_model"] = compare(
        at("cov"),
        development=dev,
        protected=hidden,
        terms={"minimum_coverage_bps": 10000, "error_ceiling_bps": 5000},
        mapping={"x": "a", "uncorrected": "b"},
        model=review,
    )[0]
    rare = rows("protected")
    rare[0]["slices"] = ["rare"]
    out["missing_slice"] = compare(
        at("slice"),
        protected=rare,
        terms={
            "minimum_coverage_bps": 10000,
            "error_ceiling_bps": 10000,
            "critical_slices": ["rare"],
            "minimum_slice_cases": 2,
        },
    )[0]
    out["insufficient"] = compare(at("insuff"), terms={**STRICT, "minimum_verified_cases": 50})[0]
    neutral = rows("protected")
    neutral_dev = rows("dev")
    for r in neutral + neutral_dev:
        r["incumbent"] = auto("a")
    out["no_change"] = compare(at("nochange"), development=neutral_dev, protected=neutral)[0]
    held, plan = compare(at("devhold"), mapping={"zzz": "a"})
    out["zero_eligible"] = held
    assert plan["development_decision"]["route"] == "hold"
    assert development_hold(plan) == held
    return out


def make_receipt(**over):
    frozen = {
        k: digest(["generated", k])
        for k in (
            "canonical_inputs",
            "source_revisions",
            "split_manifest",
            "scoring_policy",
            "label_manifest",
            "answer_key",
        )
    }
    body = {
        "schema_version": cp.RECEIPT,
        "verdict": "evaluation_allowed",
        "reasons": [],
        "evidence_origin": "generated",
        "bindings": {"frozen": frozen, "current": dict(frozen)},
        "claims": {"overall_accuracy_claim": "not_established"},
        "superseded_by": None,
        "successor_of": None,
    }
    body.update(over)
    body["receipt_digest"] = digest(body)
    body["receipt_id"] = "audit-" + body["receipt_digest"][:16]
    return body


def reseal_receipt(receipt):
    body = {k: v for k, v in receipt.items() if k not in ("receipt_digest", "receipt_id")}
    return make_receipt(**body)


def context(comparison, **over):
    arms = comparison["arms"]
    values = {
        "tenant": comparison["organization"],
        "engagement": comparison["engagement"],
        "recipient": "generated-recipient",
        "mandate_revision": "generated-mandate-r1",
        "run_ref": "generated-run-1",
        "proposal_version": "generated-proposal-v1",
        "benchmark_digest": digest("generated-benchmark-1"),
        "remedy_digest": arms["simple"]["revision"] if "simple" in arms else None,
        "fallback_digest": arms["incumbent"]["revision"] if arms else digest("generated-fallback"),
        "expires_at": NOW + 3600,
        "previous_deployed_version": "generated-deployed-v0",
        "worker_id": "generated-worker-1",
    }
    values.update(over)
    return cp.PacketContext(**values)


def make_verdict(comparison, receipt, ctx, *, slice_key=None, **over):
    arms = comparison["arms"]
    key = key_for(comparison) if slice_key is None else slice_key
    body = {
        "schema_version": cp.VERDICT,
        "verdict": "reproduced",
        "producer_id": ctx.worker_id,
        "verifier_id": "generated-independent-verifier",
        "audit_receipt_digest": receipt["receipt_digest"],
        "comparison_evidence_revision": comparison["evidence_revision"],
        "candidate_digest": arms["simple"]["revision"],
        "comparator_digest": arms["incumbent"]["revision"],
        "operating_point_digest_form": cp.OPERATING_POINT_DIGEST_FORM,
        "operating_point_digest": cp.operating_point_digest(
            cp.slice_substituted_terms(comparison["terms"], key, **scope(comparison))
        ),
        "benchmark_digest": ctx.benchmark_digest,
        "denominator": {
            "protected_rows": comparison["protected_rows_evaluated"],
            "membership_digest": comparison["protected_membership_digest"],
        },
    }
    body.update(over)
    return {**body, "verdict_digest": digest(body)}


def reseal_verdict(verdict):
    body = {k: v for k, v in verdict.items() if k != "verdict_digest"}
    return {**body, "verdict_digest": digest(body)}


class Authority:
    def __init__(self, ctx):
        self.ctx = ctx

    def current(self, run_ref):
        return self.ctx if self.ctx and self.ctx.run_ref == run_ref else None


class Keyring:
    """Engagement-local slice-key double: one generated 32-byte key per (tenant, engagement).

    TEST DOUBLE ONLY. It derives keys from public inputs, which a real ``SliceKeyView`` must
    never do (OPE-566: keys come from a CSPRNG or a hardware token)."""

    def __init__(self, missing=()):
        self.missing = set(missing)

    def slice_key(self, tenant, engagement):
        if (tenant, engagement) in self.missing:
            return None
        return bytes.fromhex(digest(["generated-slice-key", tenant, engagement]))


KEYS = Keyring()


def key_for(comparison):
    return KEYS.slice_key(comparison["organization"], comparison["engagement"])


def scope(comparison):
    return {"tenant": comparison["organization"], "engagement": comparison["engagement"]}


class Sink:
    """Consumer double that deduplicates on (resource, event id, event revision)."""

    def __init__(self):
        self.calls = []
        self.effects = {}
        self.fail = False

    def deliver(self, event):
        if self.fail:
            raise ConnectionError("simulated:consumer-unreachable")
        self.calls.append(event)
        key = (event["resource"], event["event_id"], event["event_revision"])
        self.effects.setdefault(key, event)


class Bundle:
    def __init__(self, comparison, tmp_path, **ctx_over):
        self.comparison = comparison
        self.receipt = make_receipt()
        self.ctx = context(comparison, **ctx_over)
        self.verdict = (
            make_verdict(comparison, self.receipt, self.ctx)
            if "simple" in comparison["arms"]
            else None
        )
        self.authority = Authority(self.ctx)
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.store = cp.PacketOutbox(
            tmp_path / "outbox.sqlite3", allow_generated_fixture=True, slice_keys=KEYS
        )
        self.sink = Sink()

    def stage(self, **over):
        args = {"comparison": self.comparison, "receipt": self.receipt, "verdict": self.verdict}
        args.update(over)
        return self.store.stage(
            args["comparison"],
            args["receipt"],
            args["verdict"],
            authority=self.authority,
            run_ref=self.ctx.run_ref,
            now=NOW,
            fault=args.get("fault"),
        )

    def dispatch(self, **kw):
        return self.store.dispatch(self.sink, self.authority, now=kw.pop("now", NOW), **kw)


def empty(store):
    return all(rows_ == [] for rows_ in store.snapshot().values())


# ---------------------------------------------------------------- A: positive outcomes


def test_verified_simple_winner_stages_one_packet_and_one_event(runs, tmp_path):
    """A1: a verified simple winner stages a measured-improvement packet and one event."""
    b = Bundle(runs["winner"], tmp_path)
    out = b.stage()
    assert out.status == "staged" and out.reason is None
    packet = out.packet
    assert packet["kind"] == "measured_improvement"
    assert packet["decision"]["winner"] == "simple"
    assert packet["binding"] == {**b.ctx.as_json()}
    assert packet["binding"]["previous_deployed_version"] == "generated-deployed-v0"
    assert packet["audit"]["receipt_digest"] == b.receipt["receipt_digest"]
    assert packet["audit"]["independent_verdict_digest"] == b.verdict["verdict_digest"]
    assert packet["authority"] == {"release": False, "delivery": False}
    assert packet["claim"]["scope"] == "implementation_only"
    assert packet["claim"]["provider_or_customer_evidence"] is False
    # Immature outcome data and missing comparable units are explicit, not silent.
    assert packet["immature_outcome_data"] is True
    assert {u["arm"] for u in packet["missing_comparable_units"]} == {"incumbent", "simple"}
    assert b.dispatch() == [(out.event_id, "delivered")]
    assert len(b.sink.effects) == 1
    event = next(iter(b.sink.effects.values()))
    assert event["packet_ref"]["packet_digest"] == packet["packet_digest"]
    assert event["staged_for_human_decision"] is True
    assert event["authority"] == {"release": False, "delivery": False}


def test_paired_denominators_match_hand_checked_fixture(runs, tmp_path):
    """A5: 7 protected cases, 6 fixes, 1 regression; hand arithmetic reproduced exactly."""
    # Hand check. Protected references: a,a,a,a,a,a,b. Incumbent always says b, so it is wrong on
    # the six "a" cases (6/7) and right on the "b" case. The frozen rule always says a, so it
    # fixes the six and regresses the one: it is wrong on 1/7. Paired: 6 fixes, 1 regression,
    # 7 comparable, 0 excluded. Both arms cover 7/7 (every case is automatic).
    packet = Bundle(runs["hand"], tmp_path).stage().packet
    assert packet["denominator"]["protected_rows"] == 7
    assert packet["arms"]["incumbent"]["wrong_automatic"] == [6, 7]
    assert packet["arms"]["simple"]["wrong_automatic"] == [1, 7]
    assert packet["arms"]["incumbent"]["coverage"] == [7, 7]
    assert packet["arms"]["simple"]["coverage"] == [7, 7]
    paired = packet["paired"]["incumbent_vs_simple"]
    assert (paired["fixes"], paired["regressions"], paired["comparable"], paired["excluded"]) == (
        6,
        1,
        7,
        0,
    )
    assert paired["observed_comparison"] == "improved"


def test_tie_favors_sufficient_simpler_arm_with_spend_and_runtime(runs, tmp_path):
    """A2: model ties the simple repair; the cheaper adequate simple arm wins, spend recorded."""
    comparison = runs["tie"]
    assert comparison["decision"]["reason"] == "adequate_simple_repair_at_lower_observed_cost"
    packet = Bundle(comparison, tmp_path).stage().packet
    assert packet["decision"]["winner"] == "simple"
    assert set(packet["arms"]) == {"incumbent", "simple", "model"}
    costs = {k: v["economics"]["cost_per_1000"] for k, v in packet["arms"].items()}
    assert all(c is not None for c in costs.values())
    assert packet["arms"]["simple"]["economics"]["basis"] == "observed"
    assert packet["missing_comparable_units"] == []


@pytest.mark.parametrize(
    "name,reason",
    [
        ("missing_slice", "critical_slice_failed_or_unverified"),
        ("insufficient", "insufficient_protected_support"),
        ("no_change", None),
        ("zero_eligible", "recurring_residual_requires_model_and_compute_rights"),
    ],
)
def test_hold_and_no_proposal_outcomes_stage_a_reasoned_hold_without_authority(
    runs, tmp_path, name, reason
):
    """A3/A4/A6: missing critical slice, insufficient evidence, no proposal, zero eligible."""
    comparison = runs[name]
    b = Bundle(comparison, tmp_path)
    b.verdict = b.receipt = None  # a hold needs no audit receipt and acquires no authority
    out = b.stage()
    assert out.status == "staged"
    packet = out.packet
    assert packet["kind"] == "hold"
    assert packet["decision"]["winner"] in (None, "incumbent")
    if reason:
        assert packet["decision"]["reason"] == reason
    assert packet["authority"] == {"release": False, "delivery": False}
    assert packet["next_action"] == "no_release_authority_held"
    b.dispatch()
    (event,) = b.sink.effects.values()
    assert event["staged_for_human_decision"] is False and event["disposition"] != "recommend"


def test_zero_eligible_comparison_is_explicit_hold_not_empty_packet(runs, tmp_path):
    """A3b: nothing evaluated yields a hold with a reason and zero denominator, never a blank."""
    packet = Bundle(runs["zero_eligible"], tmp_path).stage().packet
    assert packet["kind"] == "hold" and packet["denominator"]["protected_rows"] == 0
    assert packet["arms"] == {} and packet["decision"]["reason"]
    assert packet["decision"]["disposition"] == "hold"


def test_coverage_loss_is_reported_and_never_promoted(runs, tmp_path):
    """A4: a model arm that abstains everywhere shows 0/4 coverage and cannot win."""
    comparison = runs["coverage_loss_model"]
    packet = Bundle(comparison, tmp_path).stage().packet
    assert packet["arms"]["model"]["coverage"] == [0, 4]
    assert packet["decision"]["winner"] == "simple"
    # Hand-built reseal of a generated result: the winning arm itself loses coverage -> hold.
    lost = copy.deepcopy(runs["winner"])
    lost["arms"]["simple"]["metrics"]["coverage"] = {"numerator": 2, "denominator": 4, "value": 0.5}
    lost["arms"]["simple"]["hold_reason"] = "operating_point_missed_or_undetermined"
    lost["decision"] = {
        "disposition": "hold",
        "winner": None,
        "fallback": "incumbent",
        "reason": "operating_point_missed_or_undetermined",
    }
    held = Bundle(seal(lost), tmp_path / "lost").stage().packet
    assert held["kind"] == "hold" and held["arms"]["simple"]["coverage"] == [2, 4]


# ---------------------------------------------------------------- R/V: refusals, fail closed


def refused(b, expect, **over):
    out = b.stage(**over)
    assert out.status == "refused" and out.reason == expect, out
    assert out.packet is None and out.event_id is None
    assert out.scoped_hold == {
        "scope": "measured_improvement_proposal",
        "state": "hold",
        "reason": expect,
        "release_authority": False,
    }
    assert empty(b.store), "a refusal must leave no packet, no event and no authority"
    assert b.dispatch() == [] and b.sink.calls == []


def test_missing_receipt_refuses_packet(runs, tmp_path):
    """R1: no receipt, no packet, no event."""
    refused(Bundle(runs["winner"], tmp_path), "audit_receipt_missing", receipt=None)


def test_executable_scorer_or_acknowledged_benchmark_alone_cannot_stage(runs, tmp_path):
    """R1b: a bare comparison with neither receipt nor verdict is refused."""
    refused(Bundle(runs["winner"], tmp_path), "audit_receipt_missing", receipt=None, verdict=None)


def test_unverified_receipt_refuses_packet(runs, tmp_path):
    """R2: a receipt whose digest or shape does not verify is refused."""
    b = Bundle(runs["winner"], tmp_path)
    tampered = {**b.receipt, "verdict": "evaluation_allowed", "evidence_origin": "observed"}
    refused(b, "audit_receipt_unverified", receipt=tampered)
    refused(b, "audit_receipt_unverified", receipt={"schema_version": cp.RECEIPT})
    refused(b, "audit_receipt_unverified", receipt="not-a-receipt")


def test_stale_receipt_refuses_packet(runs, tmp_path):
    """R3: frozen != current bindings, or a stale verdict, is refused."""
    b = Bundle(runs["winner"], tmp_path)
    changed = make_receipt()
    changed["bindings"]["current"]["label_manifest"] = digest("relabelled")
    stale = reseal_receipt(changed)
    refused(
        b, "audit_receipt_stale", receipt=stale, verdict=make_verdict(b.comparison, stale, b.ctx)
    )
    marked = make_receipt(verdict="stale")
    refused(
        b, "audit_receipt_stale", receipt=marked, verdict=make_verdict(b.comparison, marked, b.ctx)
    )


def test_receipt_superseded_in_its_own_body_refuses_packet(runs, tmp_path):
    """R4: a receipt that names its successor is refused."""
    b = Bundle(runs["winner"], tmp_path)
    old = make_receipt(superseded_by="audit-successor-generated")
    refused(
        b, "audit_receipt_superseded", receipt=old, verdict=make_verdict(b.comparison, old, b.ctx)
    )


@pytest.mark.parametrize(
    "verdict,expect",
    [
        ("claim_limited_to_named_population", "audit_claim_limited_to_named_population"),
        ("starter_only", "audit_verdict_starter_only"),
        ("insufficient_evidence", "audit_verdict_insufficient_evidence"),
        ("invalid", "audit_verdict_invalid"),
    ],
)
def test_scope_limited_or_non_allowing_audit_never_yields_an_improvement_packet(
    runs, tmp_path, verdict, expect
):
    """R5: a claim limited to a named population cannot become an unqualified improvement."""
    b = Bundle(runs["winner"], tmp_path)
    receipt = make_receipt(verdict=verdict)
    refused(b, expect, receipt=receipt, verdict=make_verdict(b.comparison, receipt, b.ctx))


@pytest.mark.parametrize(
    "change,expect",
    [
        ({"verdict": "not_reproduced"}, "independent_verdict_not_reproduced"),
        ({"verdict": "inconclusive"}, "independent_verdict_not_reproduced"),
        ({"verifier_id": "generated-worker-1"}, "independent_verdict_not_independent"),
        ({"producer_id": "generated-other-worker"}, "independent_verdict_not_independent"),
        ({"candidate_digest": digest("other-candidate")}, "verdict_binding_mismatch"),
        ({"comparator_digest": digest("other-comparator")}, "verdict_binding_mismatch"),
        ({"operating_point_digest": digest("other-point")}, "verdict_binding_mismatch"),
        ({"benchmark_digest": digest("other-benchmark")}, "verdict_binding_mismatch"),
        ({"audit_receipt_digest": digest("other-receipt")}, "verdict_binding_mismatch"),
        ({"comparison_evidence_revision": digest("other-run")}, "verdict_binding_mismatch"),
        (
            {"denominator": {"protected_rows": 5, "membership_digest": digest("x")}},
            "denominator_changed",
        ),
    ],
)
def test_verdict_must_be_independent_reproduced_and_bound_to_exact_versions(
    runs, tmp_path, change, expect
):
    """V1: the independent verdict binds candidate, comparator, operating point, denominator."""
    b = Bundle(runs["winner"], tmp_path)
    verdict = make_verdict(b.comparison, b.receipt, b.ctx, **change)
    refused(b, expect, verdict=verdict)


def test_missing_or_tampered_verdict_refuses_packet(runs, tmp_path):
    """V2: absent or unverifiable independent verdict is a refusal, not a soft warning."""
    b = Bundle(runs["winner"], tmp_path)
    refused(b, "independent_verdict_missing", verdict=None)
    refused(
        b,
        "independent_verdict_unverified",
        verdict={**b.verdict, "verdict": "reproduced", "verifier_id": "forged"},
    )


def test_changed_denominator_refuses_packet(runs, tmp_path):
    """R6: an arm whose denominator no longer equals the frozen population is refused."""
    b = Bundle(runs["winner"], tmp_path)
    broken = copy.deepcopy(runs["winner"])
    broken["arms"]["simple"]["metrics"]["coverage"]["denominator"] = 5
    broken = seal(broken)
    verdict = make_verdict(broken, b.receipt, b.ctx)
    refused(b, "denominator_changed", comparison=broken, verdict=verdict)


def test_comparison_scope_and_integrity_failures_refuse(runs, tmp_path):
    """R7: tampered, foreign-tenant, wrong-schema, model-branch and remedy-mismatch refusals."""
    b = Bundle(runs["winner"], tmp_path)
    refused(
        b, "comparison_unverified", comparison={**runs["winner"], "protected_rows_evaluated": 9}
    )
    refused(b, "comparison_unverified", comparison={"schema_version": RESULT})
    refused(b, "comparison_unverified", comparison=None)
    foreign = Bundle(runs["winner"], tmp_path / "f", tenant="generated-other-tenant")
    refused(foreign, "comparison_scope_mismatch")
    mismatch = Bundle(runs["winner"], tmp_path / "m", remedy_digest=digest("other-remedy"))
    refused(mismatch, "remedy_binding_mismatch")
    model = copy.deepcopy(runs["winner"])
    model["decision"]["winner"] = "model"
    model = seal(model)
    refused(b, "model_winner_branch_not_in_this_packet", comparison=model)


def test_expiry_context_and_authority_failures_fail_closed(runs, tmp_path):
    """R8: expired binding, invalid context and unavailable authority create nothing."""
    refused(Bundle(runs["winner"], tmp_path, expires_at=NOW), "packet_expired")
    refused(Bundle(runs["winner"], tmp_path, run_ref="bad ref"), "authority_unavailable")
    refused(
        Bundle(runs["winner"], tmp_path, fallback_digest="not-a-digest"), "invalid_packet_context"
    )
    down = Bundle(runs["winner"], tmp_path)
    down.authority.ctx = None
    refused(down, "authority_unavailable")


# ---------------------------------------------------------------- E: one event, retry, crash


def test_repeat_staging_yields_one_event(runs, tmp_path):
    """E1: staging the same comparison again changes nothing and emits no second event."""
    b = Bundle(runs["winner"], tmp_path)
    first = b.stage()
    before = b.store.snapshot()
    again = b.stage()
    assert again.status == "already_staged" and again.packet == first.packet
    assert b.store.snapshot() == before
    b.dispatch()
    b.dispatch()
    assert len(b.sink.calls) == 1 and len(b.sink.effects) == 1
    assert len(before["outbox"]) == 1 and len(before["packets"]) == 1


def test_conflicting_restage_of_same_proposal_is_refused_unchanged(runs, tmp_path):
    """E1b: the same proposal version cannot be re-staged with different evidence."""
    b = Bundle(runs["winner"], tmp_path)
    b.stage()
    before = b.store.snapshot()
    changed = make_receipt(successor_of="audit-generated-predecessor")
    out = b.stage(receipt=changed, verdict=make_verdict(b.comparison, changed, b.ctx))
    assert out.reason == "restage_conflict" and b.store.snapshot() == before


def test_crash_before_commit_leaves_nothing_then_restart_stages_exactly_one(runs, tmp_path):
    """E2: a crash after packet assembly but before commit leaves zero, restart gives one."""
    b = Bundle(runs["winner"], tmp_path)

    def crash(point):
        raise SystemExit("simulated:crash-" + point)

    for point in ("before_outbox", "before_commit"):
        with pytest.raises(SystemExit):
            b.stage(fault=lambda p, point=point: crash(p) if p == point else None)
        assert empty(b.store)
    restarted = cp.PacketOutbox(b.store.path, allow_generated_fixture=True, slice_keys=KEYS)
    b.store = restarted
    assert b.stage().status == "staged"
    b.dispatch()
    assert len(b.sink.effects) == 1 and len(restarted.snapshot()["outbox"]) == 1


def test_hard_process_exit_after_commit_then_restart_emits_exactly_one(runs, tmp_path):
    """E3: kill the process after the commit and before any delivery; restart emits one."""
    b = Bundle(runs["winner"], tmp_path)
    payload = {
        "path": str(b.store.path),
        "comparison": b.comparison,
        "receipt": b.receipt,
        "verdict": b.verdict,
        "ctx": {**b.ctx.as_json(), "worker_id": b.ctx.worker_id},
        "key": key_for(b.comparison).hex(),
    }
    script = (
        "import json,os,sys\n"
        "from opentour_cell.pilot import comparison_packet as cp\n"
        "q=json.load(sys.stdin)\n"
        "class A:\n"
        "    def current(self, r): return cp.PacketContext(**q['ctx'])\n"
        "class K:\n"
        "    def slice_key(self, t, e): return bytes.fromhex(q['key'])\n"
        "s=cp.PacketOutbox(q['path'], slice_keys=K(), allow_generated_fixture=True)\n"
        "o=s.stage(q['comparison'],q['receipt'],q['verdict'],authority=A(),run_ref='generated-run-1'"
        ",now=1000)\n"
        "assert o.status=='staged'\n"
        "os._exit(9)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 9, proc.stderr
    restarted = Bundle(runs["winner"], tmp_path)  # reopens the same outbox file
    assert len(restarted.store.snapshot()["outbox"]) == 1
    assert restarted.stage().status == "already_staged"
    assert len(restarted.dispatch()) == 1 and len(restarted.sink.effects) == 1
    assert restarted.dispatch() == [] and len(restarted.sink.calls) == 1


def test_crash_after_delivery_before_mark_redelivers_same_key_one_effect(runs, tmp_path):
    """E4: delivery succeeded but the worker died before recording it: same key, one effect."""
    b = Bundle(runs["winner"], tmp_path)
    b.stage()

    def die(point):
        if point == "after_deliver":
            raise SystemExit("simulated:crash-after-deliver")

    with pytest.raises(SystemExit):
        b.dispatch(fault=die)
    assert len(b.sink.calls) == 1
    b.store = cp.PacketOutbox(b.store.path, allow_generated_fixture=True, slice_keys=KEYS)
    assert b.dispatch()[0][1] == "delivered"
    assert len(b.sink.calls) == 2 and len(b.sink.effects) == 1  # at-least-once, effect once
    assert b.dispatch() == []
    assert b.store.snapshot()["outbox"][0][5] == "delivered"


def test_unknown_delivery_result_is_never_inferred_as_success(runs, tmp_path):
    """E5: a failed or unknown delivery stays retryable and is never marked delivered."""
    b = Bundle(runs["winner"], tmp_path)
    b.stage()
    b.sink.fail = True
    assert b.dispatch()[0][1] == "uncertain"
    assert b.store.snapshot()["outbox"][0][5] == "sending"
    b.sink.fail = False
    assert b.dispatch()[0][1] == "delivered" and len(b.sink.effects) == 1


def test_duplicate_dispatch_and_late_callbacks_have_zero_additional_effects(runs, tmp_path):
    """E6: duplicate dispatch and late, duplicate or unknown callbacks change nothing."""
    b = Bundle(runs["winner"], tmp_path)
    out = b.stage()
    resource = "comparison/" + b.ctx.run_ref
    assert b.dispatch()[0][1] == "delivered"
    settled = b.store.snapshot()
    assert b.dispatch() == []
    assert b.store.acknowledge(resource, out.event_id, 1, "generated-receipt-1") == (
        "duplicate_ignored"
    )
    assert b.store.acknowledge(resource, out.event_id, 2, "generated-receipt-1") == (
        "unknown_ignored"
    )
    assert b.store.acknowledge(resource, "evt-unknown", 1, "generated-receipt-1") == (
        "unknown_ignored"
    )
    assert b.store.snapshot() == settled and len(b.sink.calls) == 1


def test_callback_before_dispatch_is_ignored_and_never_recorded_as_delivered(runs, tmp_path):
    """E6b: an unauthenticated early callback cannot mark an undispatched event delivered."""
    b = Bundle(runs["winner"], tmp_path)
    out = b.stage()
    resource = "comparison/" + b.ctx.run_ref
    before = b.store.snapshot()
    assert b.store.acknowledge(resource, out.event_id, 1, "generated-receipt-1") == (
        "not_dispatched_ignored"
    )
    assert b.store.snapshot() == before
    b.sink.fail = True
    assert b.dispatch()[0][1] == "uncertain"
    assert b.store.acknowledge(resource, out.event_id, 1, "bad receipt!") == (
        "invalid_receipt_ignored"
    )
    assert b.store.acknowledge(resource, out.event_id, 1, "generated-receipt-1") == "acknowledged"
    assert b.dispatch() == []


def test_forged_observed_receipt_and_verdict_stage_nothing(runs, tmp_path):
    b = Bundle(runs["winner"], tmp_path)
    forged = make_receipt(evidence_origin="observed", population_kind="real_sample")
    out = b.stage(receipt=forged, verdict=make_verdict(b.comparison, forged, b.ctx))
    assert out.reason == "audit_and_verdict_issuers_unverified"
    assert out.packet is None
    assert empty(b.store)


def test_live_store_refuses_generated_improvement_and_fences_existing_row(runs, tmp_path):
    b = Bundle(runs["winner"], tmp_path)
    live = cp.PacketOutbox(b.store.path, slice_keys=KEYS)
    out = live.stage(
        b.comparison, b.receipt, b.verdict, authority=b.authority, run_ref=b.ctx.run_ref, now=NOW
    )
    assert out.reason == "audit_and_verdict_issuers_unverified"
    assert empty(live)

    staged = b.stage()
    assert live.dispatch(b.sink, b.authority, now=NOW) == [(staged.event_id, "fenced")]
    assert b.sink.calls == []


def test_receipt_free_text_cannot_enter_a_packet_or_a_refusal(runs, tmp_path):
    """E7: origin and verdict are closed vocabularies; free text is refused as unverified."""
    b = Bundle(runs["winner"], tmp_path)
    for over in ({"evidence_origin": "case-123 label"}, {"verdict": "case-77 group-9"}):
        bad = make_receipt(**over)
        refused(
            b,
            "audit_receipt_unverified",
            receipt=bad,
            verdict=make_verdict(b.comparison, bad, b.ctx),
        )


def test_recommendation_inconsistent_with_its_own_evidence_is_refused(runs, tmp_path):
    """A7: a resealed 'recommend' whose winner lost coverage, is held, or is not improved."""
    b = Bundle(runs["winner"], tmp_path)
    lost = copy.deepcopy(runs["winner"])
    lost["arms"]["simple"]["metrics"]["coverage"] = {"numerator": 2, "denominator": 4, "value": 0.5}
    held = copy.deepcopy(runs["winner"])
    held["arms"]["simple"]["hold_reason"] = "operating_point_missed_or_undetermined"
    tied = copy.deepcopy(runs["winner"])
    tied["comparisons"]["incumbent_vs_simple"]["observed_comparison"] = "equivalent"
    for comparison in (lost, held, tied):
        sealed = seal(comparison)
        refused(
            b,
            "decision_inconsistent_with_evidence",
            comparison=sealed,
            verdict=make_verdict(sealed, b.receipt, b.ctx),
        )


def test_uncertain_delivery_then_receipt_supersession_reconciles_as_possibly_disclosed(
    runs, tmp_path
):
    """C3c: an uncertain send may have disclosed, so its reconciliation entry says so."""
    b = Bundle(runs["winner"], tmp_path)
    b.stage()
    b.sink.fail = True
    assert b.dispatch()[0][1] == "uncertain"
    b.store.record_receipt_supersession(b.receipt["receipt_id"])
    assert b.store.reconciliations()[0]["event_delivered"] == 1


# ---------------------------------------------------------------- C: supersession and fences


def test_changed_benchmark_supersedes_proposal_and_fences_prior_approval_inputs(runs, tmp_path):
    """C1: a new benchmark supersedes the old packet; its undelivered event never goes out."""
    b = Bundle(runs["winner"], tmp_path)
    old = b.stage()
    old_id = old.packet["packet_digest"][:32]
    assert b.store.packet_status("pkt-" + old_id) == "current"
    successor = digest("generated-benchmark-2")
    ctx2 = context(runs["winner"], benchmark_digest=successor, proposal_version="generated-v2")
    b.authority.ctx = ctx2
    verdict2 = make_verdict(runs["winner"], b.receipt, ctx2)
    new = b.store.stage(
        runs["winner"], b.receipt, verdict2, authority=b.authority, run_ref=ctx2.run_ref, now=NOW
    )
    assert new.status == "staged"
    assert b.store.packet_status("pkt-" + old_id) == "superseded"
    assert b.store.packet_status(f"pkt-{new.packet['packet_digest'][:32]}") == "current"
    assert old.event_id not in dict(b.dispatch())
    assert [e["packet_ref"]["packet_digest"] for e in b.sink.effects.values()] == [
        new.packet["packet_digest"]
    ]
    assert b.store.packet_status("pkt-unknown") == "unknown"


def test_benchmark_change_without_new_packet_fences_via_supersede_call(runs, tmp_path):
    """C1b: supersede_benchmark alone fences the packet and its pending event."""
    b = Bundle(runs["winner"], tmp_path)
    out = b.stage()
    assert b.store.supersede_benchmark(b.ctx.run_ref, b.ctx.benchmark_digest) == []
    fenced = b.store.supersede_benchmark(b.ctx.run_ref, digest("generated-benchmark-9"))
    assert fenced == ["pkt-" + out.packet["packet_digest"][:32]]
    assert b.dispatch() == [] and b.sink.calls == []
    assert b.store.supersede_benchmark(b.ctx.run_ref, digest("generated-benchmark-9")) == []


def test_binding_change_expiry_or_authority_loss_at_dispatch_fences_or_holds(runs, tmp_path):
    """C2: dispatch rechecks authority; stale binding is fenced, unavailable authority waits."""
    b = Bundle(runs["winner"], tmp_path)
    b.stage()
    b.authority.ctx = None
    assert b.dispatch()[0][1] == "authority_unavailable" and b.sink.calls == []
    b.authority.ctx = context(runs["winner"], mandate_revision="generated-mandate-r2")
    assert b.dispatch()[0][1] == "fenced" and b.sink.calls == []
    assert b.dispatch() == []
    late = Bundle(runs["hand"], tmp_path / "late")
    late.stage()
    assert late.dispatch(now=NOW + 3600)[0][1] == "fenced" and late.sink.calls == []


def test_superseded_receipt_blocks_new_packet_and_reconciles_earlier_disclosure(runs, tmp_path):
    """C3: superseding a receipt reconciles delivered disclosures once and blocks new packets."""
    b = Bundle(runs["winner"], tmp_path)
    out = b.stage()
    b.dispatch()
    pid = "pkt-" + out.packet["packet_digest"][:32]
    assert b.store.record_receipt_supersession(b.receipt["receipt_id"], "audit-next") == [pid]
    (entry,) = b.store.reconciliations()
    assert entry["packet_id"] == pid and entry["event_delivered"] == 1
    assert entry["state"] == "open"
    assert b.store.packet_status(pid) == "superseded"
    settled = b.store.snapshot()
    assert b.store.record_receipt_supersession(b.receipt["receipt_id"]) == [pid]
    assert b.store.snapshot() == settled  # reconciliation is recorded once
    ctx2 = context(runs["winner"], proposal_version="generated-v2")
    b.authority.ctx = ctx2
    again = b.store.stage(
        runs["winner"],
        b.receipt,
        make_verdict(runs["winner"], b.receipt, ctx2),
        authority=b.authority,
        run_ref=ctx2.run_ref,
        now=NOW,
    )
    assert again.reason == "audit_receipt_superseded" and again.packet is None
    assert len(b.store.snapshot()["packets"]) == 1


def test_superseded_receipt_before_delivery_sends_nothing(runs, tmp_path):
    """C3b: the undelivered event is fenced and the reconciliation records no disclosure."""
    b = Bundle(runs["winner"], tmp_path)
    b.stage()
    b.store.record_receipt_supersession(b.receipt["receipt_id"])
    assert b.dispatch() == [] and b.sink.calls == []
    assert b.store.reconciliations()[0]["event_delivered"] == 0


# ---------------------------------------------------------------- L: least disclosure, replay


def test_packet_and_event_disclose_only_allowlisted_aggregates_and_pointers(runs, tmp_path):
    """L1: no case ids, group ids, labels, references or feature values leave the worker."""
    b = Bundle(runs["winner"], tmp_path)
    out = b.stage()
    b.dispatch()
    text = canonical(out.packet) + canonical(next(iter(b.sink.effects.values())))
    for marker in (
        "protected-",
        "family-",
        "out-protected",
        "adjudicator",
        "category",
        "paired_cases",
        "case_id",
        "group_id",
        "reference",
        "features",
    ):
        assert marker not in text, marker
    pointer = {p["kind"]: p["digest"] for p in out.packet["evidence_pointers"]}
    assert pointer["protected_comparison"] == runs["winner"]["evidence_revision"]
    assert pointer["benchmark_audit_receipt"] == b.receipt["receipt_digest"]
    assert pointer["independent_verdict"] == b.verdict["verdict_digest"]
    assert pointer["paired_case_set"] == digest(runs["winner"]["paired_cases"])


def test_replay_reproduces_identical_packet_digest_for_two_independent_consumers(runs, tmp_path):
    """L2: in-process and fresh-interpreter replays, plus a pointer-only consumer, agree."""
    b = Bundle(runs["winner"], tmp_path)
    out = b.stage()
    key = key_for(b.comparison)
    again, reason = cp.assemble_packet(
        b.comparison, b.receipt, b.verdict, b.ctx, slice_key=key, allow_generated_fixture=True
    )
    assert reason is None and again == out.packet
    assert cp.verify_packet(
        out.packet,
        b.comparison,
        b.receipt,
        b.verdict,
        b.ctx,
        slice_key=key,
        allow_generated_fixture=True,
    )
    forged = {**out.packet, "authority": {"release": True, "delivery": True}}
    assert not cp.verify_packet(
        forged,
        b.comparison,
        b.receipt,
        b.verdict,
        b.ctx,
        slice_key=key,
        allow_generated_fixture=True,
    )
    # Consumer 1: fresh interpreter re-derives the packet from the source documents.
    payload = {
        "comparison": b.comparison,
        "receipt": b.receipt,
        "verdict": b.verdict,
        "ctx": b.ctx.as_json() | {"worker_id": b.ctx.worker_id},
        "key": key.hex(),
    }
    script = (
        "import json,sys\n"
        "from opentour_cell.pilot import comparison_packet as cp\n"
        "q=json.load(sys.stdin)\n"
        "p,r=cp.assemble_packet(q['comparison'],q['receipt'],q['verdict'],"
        "cp.PacketContext(**q['ctx']),slice_key=bytes.fromhex(q['key']),"
        "allow_generated_fixture=True)\n"
        "print(p['packet_digest'] if p else r)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert proc.stdout.strip() == out.packet["packet_digest"]
    # Consumer 2: sees only the packet and event; checks self-consistency and pointers.
    event = cp.event_for(out.packet)
    body = {k: v for k, v in out.packet.items() if k != "packet_digest"}
    assert digest(body) == out.packet["packet_digest"] == event["packet_ref"]["packet_digest"]
    assert event["event_id"] == "evt-" + out.packet["packet_digest"][:32]
    assert cp.event_for(out.packet) == event


def test_no_delivery_or_release_before_a_separately_authorized_human_decision(runs, tmp_path):
    """L3: the only effect is the completion event; nothing grants or delivers a release."""
    b = Bundle(runs["winner"], tmp_path)
    out = b.stage()
    b.dispatch()
    snapshot = b.store.snapshot()
    assert set(snapshot) == {"packets", "outbox", "superseded_receipts", "reconciliations"}
    assert not any(hasattr(b.store, name) for name in ("approve", "release", "deliver_release"))
    assert (
        all(e["authority"] == {"release": False, "delivery": False} for e in b.sink.calls)
        and len(b.sink.calls) == 1
    )
    assert out.packet["authority"]["release"] is False
    assert out.packet["next_action"] == "separate_human_decision_required"


# ------------------------------------------- E7b: comparison free text (OPE-421 follow-up)

# Generated sentinels only. Each stands in for a case, group, label or person reference that
# must never reach a published packet or a refusal reason.
SENTINELS = {
    "reason": "case-77 sentinel-address-A1",
    "fallback": "group-9",
    "metric": "case-5",
    "hold_reason": "case-123 label",
}
_PLAIN_HOLD = "insufficient"  # a hold: needs no receipt or verdict


def tampered(comparison, field, value, *, arm="incumbent"):
    """Reseal a copy of a comparison with one free-text field replaced."""
    changed = copy.deepcopy(comparison)
    if field in ("reason", "fallback"):
        changed["decision"][field] = value
    elif field == "metric":
        changed["metric"] = value
    else:
        changed["arms"][arm]["hold_reason"] = value
    return seal(changed)


def live_bundle(comparison, tmp_path):
    """A hold bundle on a LIVE store: the default flag off, as production constructs it."""
    b = Bundle(comparison, tmp_path)
    b.store = cp.PacketOutbox(tmp_path / "live.sqlite3", slice_keys=KEYS)
    assert b.store.allow_generated_fixture is False
    b.receipt = b.verdict = None
    return b


def assert_no_leak(out, store):
    assert out.status == "refused" and out.reason == "comparison_unverified", out
    assert out.packet is None and out.event_id is None
    assert empty(store), "a refusal must leave no packet, no event and no authority"
    shown = json.dumps([out.reason, out.scoped_hold], sort_keys=True)
    assert not any(s in shown for s in SENTINELS.values())


@pytest.mark.parametrize("field", sorted(SENTINELS))
def test_comparison_free_text_is_refused_on_the_live_hold_path(runs, tmp_path, field):
    """E7b: a hold in a live store with the flag off needs no receipt; free text must not stage."""
    base = runs[_PLAIN_HOLD]
    assert base["decision"]["disposition"] == "hold"
    b = live_bundle(tampered(base, field, SENTINELS[field]), tmp_path)
    out = b.stage()
    assert_no_leak(out, b.store)
    assert b.dispatch() == [] and b.sink.calls == []


@pytest.mark.parametrize("field", sorted(SENTINELS))
def test_comparison_free_text_is_refused_on_the_staging_path(runs, tmp_path, field):
    """E7b: with the fixture flag on, a resealed recommendation cannot carry free text either."""
    b = Bundle(runs["winner"], tmp_path)
    sealed = tampered(runs["winner"], field, SENTINELS[field])
    out = b.stage(comparison=sealed, verdict=make_verdict(sealed, b.receipt, b.ctx))
    assert_no_leak(out, b.store)


def test_all_four_comparison_free_text_fields_together_are_refused_everywhere(runs, tmp_path):
    """E7b: the reviewer's combined probe, on the live hold path and the fixture staging path."""
    hold = runs[_PLAIN_HOLD]
    for field in SENTINELS:
        hold = tampered(hold, field, SENTINELS[field])
    live = live_bundle(hold, tmp_path / "hold")
    assert_no_leak(live.stage(), live.store)
    rec = runs["winner"]
    for field in SENTINELS:
        rec = tampered(rec, field, SENTINELS[field])
    b = Bundle(rec, tmp_path / "rec")
    assert_no_leak(b.stage(verdict=make_verdict(rec, b.receipt, b.ctx)), b.store)


def test_vocabulary_check_rejects_non_string_and_unhashable_values_without_raising(runs, tmp_path):
    """E7b: the check must itself fail closed on any type, never raise."""
    for value in (["case-1"], {"k": "case-1"}, 7, True, ""):
        for field in SENTINELS:
            b = live_bundle(tampered(runs[_PLAIN_HOLD], field, value), tmp_path / field)
            out = b.stage()
            assert out.status == "refused" and out.reason == "comparison_unverified"
            assert empty(b.store)


@pytest.mark.parametrize("name", ["missing_slice", "insufficient", "no_change", "zero_eligible"])
def test_honest_holds_still_stage_in_a_live_store(runs, tmp_path, name):
    """E7b: planner-emitted hold and no-change values stay inside the vocabulary (live store)."""
    b = live_bundle(runs[name], tmp_path)
    out = b.stage()
    assert out.status == "staged", out
    assert out.packet["kind"] == "hold"
    assert b.dispatch() == [(out.event_id, "delivered")]
    assert len(b.sink.effects) == 1


@pytest.mark.parametrize("name", ["winner", "hand", "tie", "coverage_loss_model"])
def test_honest_recommendations_still_stage_with_the_fixture_flag(runs, tmp_path, name):
    """E7b: planner-emitted recommend values stay inside the vocabulary (fixture staging)."""
    out = Bundle(runs[name], tmp_path).stage()
    assert out.status == "staged" and out.packet["kind"] == "measured_improvement"


def test_honest_recommendation_in_a_live_store_is_still_held_for_unverified_issuers(runs, tmp_path):
    """E7b: unchanged behavior. Live improvement staging stays disabled; this change does not
    re-enable it."""
    b = Bundle(runs["winner"], tmp_path)
    b.store = cp.PacketOutbox(tmp_path / "live.sqlite3", slice_keys=KEYS)
    refused(b, "audit_and_verdict_issuers_unverified")


def test_every_value_the_planner_emitted_in_these_runs_is_inside_the_vocabularies(runs):
    """E7b drift guard: a planner value missing from the closed sets would refuse honest work."""
    for comparison in runs.values():
        decision = comparison["decision"]
        assert decision["reason"] in cp._DECISION_REASONS
        assert decision["fallback"] in cp._FALLBACKS
        assert comparison.get("metric") in cp._METRICS
        for arm in comparison["arms"].values():
            assert arm["hold_reason"] in cp._HOLD_REASONS


# ---------- E8: the fixture flag is refused while PYTEST_CURRENT_TEST is unset (accident guard)


def test_fixture_flag_works_in_fixture_context(runs, tmp_path):
    """E8: under the test runner the flag is honored (every staging test relies on it)."""
    assert cp._fixture_context() is True
    assert Bundle(runs["winner"], tmp_path).stage().status == "staged"


def test_live_store_refuses_the_fixture_flag_loudly(tmp_path, monkeypatch):
    """E8: with PYTEST_CURRENT_TEST unset, a PacketOutbox cannot be built with the flag."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    assert cp._fixture_context() is False
    path = tmp_path / "live.sqlite3"
    with pytest.raises(cp.PacketStoreError) as caught:
        cp.PacketOutbox(path, allow_generated_fixture=True, slice_keys=KEYS)
    assert str(caught.value) == "generated_fixture_not_permitted_in_live_context"
    assert not path.exists(), "a refused construction must not create a store"


def test_live_assemble_and_verify_refuse_the_fixture_flag(runs, tmp_path, monkeypatch):
    """E8: the function kwarg is gated too, with a closed code and without raising."""
    b = Bundle(runs["winner"], tmp_path)
    packet = b.stage().packet
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    args = (b.comparison, b.receipt, b.verdict, b.ctx)
    key = key_for(b.comparison)
    assert cp.assemble_packet(*args, slice_key=key, allow_generated_fixture=True) == (
        None,
        "generated_fixture_not_permitted_in_live_context",
    )
    assert cp.verify_packet(packet, *args, slice_key=key, allow_generated_fixture=True) is False


def test_live_context_without_the_flag_is_unchanged(runs, tmp_path, monkeypatch):
    """E8: live holds still stage; a live recommendation is still held for unverified issuers."""
    monkeypatch.delenv("PYTEST_CURRENT_TEST")
    store = cp.PacketOutbox(tmp_path / "live.sqlite3", slice_keys=KEYS)
    held = runs["insufficient"]
    hctx = context(held)
    out = store.stage(held, None, None, authority=Authority(hctx), run_ref=hctx.run_ref, now=NOW)
    assert out.status == "staged" and out.packet["kind"] == "hold"
    win = runs["winner"]
    wctx = context(win)
    receipt = make_receipt()
    out = store.stage(
        win,
        receipt,
        make_verdict(win, receipt, wctx),
        authority=Authority(wctx),
        run_ref=wctx.run_ref,
        now=NOW,
    )
    assert out.status == "refused" and out.reason == "audit_and_verdict_issuers_unverified"


# ------------- E9: receipt/verdict on holds, more closed fields, honest-output positives, drift

RECEIPT_SENTINEL = "case-91 sentinel-receipt"
VERDICT_SENTINEL = "group-31 sentinel-verdict"


def _leaks(out, *needles):
    shown = json.dumps([out.reason, out.scoped_hold, out.packet], sort_keys=True, default=str)
    return [n for n in needles if n in shown]


def _bad_receipt(field):
    receipt = make_receipt()
    if field == "verdict":
        return make_receipt(verdict=RECEIPT_SENTINEL)
    receipt[field] = RECEIPT_SENTINEL
    return receipt


@pytest.mark.parametrize("field", ["receipt_id", "receipt_digest", "verdict"])
def test_hold_refuses_a_receipt_with_free_text_in_a_live_store(runs, tmp_path, field):
    """E9: the hold path copies receipt fields into the packet, so it must validate them."""
    b = live_bundle(runs[_PLAIN_HOLD], tmp_path)
    out = b.stage(receipt=_bad_receipt(field), verdict=None)
    assert out.status == "refused" and out.reason == "audit_receipt_unverified", out
    assert out.packet is None and not _leaks(out, RECEIPT_SENTINEL)
    assert empty(b.store) and b.dispatch() == []


def test_hold_refuses_a_verdict_with_free_text_in_a_live_store(runs, tmp_path):
    """E9: same, for the independent verdict digest."""
    b = live_bundle(runs[_PLAIN_HOLD], tmp_path)
    receipt = make_receipt()
    verdict = make_verdict(b.comparison, receipt, b.ctx)
    verdict["verdict_digest"] = VERDICT_SENTINEL
    out = b.stage(receipt=receipt, verdict=verdict)
    assert out.status == "refused" and out.reason == "independent_verdict_unverified", out
    assert out.packet is None and not _leaks(out, VERDICT_SENTINEL)
    assert empty(b.store) and b.dispatch() == []


def test_hold_with_an_honest_receipt_and_verdict_still_stages_in_a_live_store(runs, tmp_path):
    """E9 positive: validated documents are carried as pointers on a hold."""
    base = runs[_PLAIN_HOLD]
    b = live_bundle(base, tmp_path)
    receipt = make_receipt()
    verdict = make_verdict(base, receipt, b.ctx)
    out = b.stage(receipt=receipt, verdict=verdict)
    assert out.status == "staged" and out.packet["kind"] == "hold", out
    assert out.packet["audit"]["receipt_digest"] == receipt["receipt_digest"]
    assert out.packet["audit"]["independent_verdict_digest"] == verdict["verdict_digest"]


SENT = "case-55 sentinel"


def _set(path, value):
    def apply(c):
        node = c
        for key in path[:-1]:
            node = node[key]
        node[path[-1]] = value

    return apply


def _slice_name(c):
    c["arms"]["incumbent"]["critical_slices"][SENT] = {"status": "met"}


def _exclusion_key(c):
    c["exclusions"] = {SENT: 1}


NEWLY_CLOSED = {
    "cost_cap": _set(("arms", "incumbent", "metrics", "economics", "cost_cap"), SENT),
    "latency_cap": _set(("arms", "incumbent", "metrics", "economics", "latency_cap"), SENT),
    "combined": _set(("arms", "incumbent", "metrics", "operating_point", "combined"), SENT),
    "cost_per_1000": _set(("arms", "incumbent", "metrics", "economics", "cost_per_1000"), SENT),
    "uncertainty_lower": _set(("arms", "incumbent", "metrics", "uncertainty", "lower"), SENT),
    "uncertainty_small_sample": _set(
        ("arms", "incumbent", "metrics", "uncertainty", "small_sample"), SENT
    ),
    "coverage_numerator": _set(("arms", "incumbent", "metrics", "coverage", "numerator"), SENT),
    "slice_name": _slice_name,
    "exclusion_key": _exclusion_key,
    "exclusion_count_string": _set(("exclusions",), {"missing_outcome": SENT}),
    "exclusion_count_negative": _set(("exclusions",), {"missing_outcome": -1}),
}


def mutated(comparison, name):
    changed = copy.deepcopy(comparison)
    NEWLY_CLOSED[name](changed)
    return seal(changed)


@pytest.mark.parametrize("name", sorted(NEWLY_CLOSED))
def test_newly_closed_fields_are_refused_on_the_live_hold_path(runs, tmp_path, name):
    """E9: every further copied field is closed, and none raises or echoes its value."""
    b = live_bundle(mutated(runs[_PLAIN_HOLD], name), tmp_path)
    out = b.stage()  # must not raise, in particular on a string exclusion count
    assert out.status == "refused" and out.reason == "comparison_unverified", out
    assert not _leaks(out, SENT) and empty(b.store)


@pytest.mark.parametrize("name", sorted(NEWLY_CLOSED))
def test_newly_closed_fields_are_refused_on_the_staging_path(runs, tmp_path, name):
    """E9: and on the fixture staging path."""
    b = Bundle(runs["winner"], tmp_path)
    sealed = mutated(runs["winner"], name)
    out = b.stage(comparison=sealed, verdict=make_verdict(sealed, b.receipt, b.ctx))
    assert out.status == "refused" and out.reason == "comparison_unverified", out
    assert not _leaks(out, SENT) and empty(b.store)


def test_string_exclusion_count_returns_a_code_instead_of_raising(runs, tmp_path):
    """E9: the sum behind immature_outcome_data used to raise TypeError on a string count."""
    sealed = mutated(runs[_PLAIN_HOLD], "exclusion_count_string")
    ctx = context(sealed)
    assert cp.assemble_packet(sealed, None, None, ctx, slice_key=key_for(sealed)) == (
        None,
        "comparison_unverified",
    )


def test_honest_abstain_heavy_run_still_stages(tmp_path):
    """E9 positive: a model that abstains on half the cases yields a different_automatic_cases
    pair (readout.py) and an honest recommend/simple must still stage."""
    dev = rows("dev")
    for r in dev:
        r["features"]["category"] = "uncorrected"
    hidden = rows("hidden")
    for r in hidden[:2]:
        r["features"]["category"] = "uncorrected"
    predicted = {r["case_id"]: auto("a") for r in hidden}
    for r in hidden[:2]:
        predicted[r["case_id"]] = {"status": "review"}
    comparison = compare(
        tmp_path,
        development=dev,
        protected=hidden,
        terms={"minimum_coverage_bps": 10000, "error_ceiling_bps": 5000},
        mapping={"x": "a", "uncorrected": "b"},
        model=predicted,
    )[0]
    observed = {p["observed_comparison"] for p in comparison["comparisons"].values()}
    assert "different_automatic_cases" in observed, observed
    assert comparison["decision"]["disposition"] == "recommend"
    out = Bundle(comparison, tmp_path / "stage").stage()
    assert out.status == "staged" and out.packet["kind"] == "measured_improvement", out


@pytest.mark.parametrize("name", ["insufficient", "no_change"])
def test_honest_hold_with_a_non_hex_incumbent_revision_still_stages(tmp_path, name):
    """E9 positive: freeze_plan only requires an identifier for incumbent_revision."""
    if name == "insufficient":
        comparison = compare(
            tmp_path,
            terms={**STRICT, "minimum_verified_cases": 50},
            incumbent="incumbent-v1",
        )[0]
    else:
        neutral, neutral_dev = rows("protected"), rows("dev")
        for r in neutral + neutral_dev:
            r["incumbent"] = auto("a")
        comparison = compare(
            tmp_path, development=neutral_dev, protected=neutral, incumbent="incumbent-v1"
        )[0]
    assert comparison["arms"]["incumbent"]["revision"] == "incumbent-v1"
    b = live_bundle(comparison, tmp_path / "live")
    b.ctx = context(comparison, fallback_digest=digest("generated-fallback"))
    b.authority = Authority(b.ctx)
    out = b.stage()
    assert out.status == "staged" and out.packet["kind"] == "hold", out


# ---- drift guard: derive what the planner can emit from its source, not from the fixtures

import ast  # noqa: E402
from pathlib import Path  # noqa: E402

_PILOT = Path(cp.__file__).parent


def _tree(name):
    return ast.parse((_PILOT / name).read_text())


def _function(tree, name):
    return next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)


def _strings(node):
    return {
        n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    }


def _branches(node):
    """String literals a value can evaluate to: a constant, or either arm of a conditional."""
    if isinstance(node, ast.Constant):
        return {node.value} if isinstance(node.value, str) else set()
    if isinstance(node, ast.IfExp):
        return _branches(node.body) | _branches(node.orelse)
    return set()


def _assigned(function, target):
    found = set()
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == target for t in node.targets
        ):
            found |= _branches(node.value)
    return found


def slice_names_validated(tree):
    """True only if ``_terms`` calls ``identifier()`` inside the loop over critical_slices and
    material_slices. A call elsewhere in ``_terms`` (for example on ``metric``) does not count."""
    for loop in ast.walk(_function(tree, "_terms")):
        if not isinstance(loop, ast.For) or not isinstance(loop.iter, (ast.Tuple, ast.List)):
            continue
        names = {e.value for e in loop.iter.elts if isinstance(e, ast.Constant)}
        if {"critical_slices", "material_slices"} <= names:
            return any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Name)
                and n.func.id == "identifier"
                for n in ast.walk(loop)
            )
    return False


def planner_emitted():
    """Every closed-vocabulary value the planner and readout can emit, read from source.

    Known blind spots, recorded rather than fixed: only string LITERALS (and either arm of a
    conditional) are seen, so values built from non-literal expressions are invisible. It does
    not derive slice statuses, economics basis, uncertainty method, winners, dispositions or the
    metric; those vocabularies are maintained by hand and covered only by the fixture runs.
    Slice-name shape is derived from the planner's identifier rule below.

    Do not delete ``test_drift_guard_fails_when_the_slice_shape_drifts_from_the_planner``: it is
    the real protection for the slice shape, because it asserts ``slice_shape == {pattern}``.
    If ``simple_repair.identifier`` stopped using ``fullmatch`` the derived shape set would be
    empty, and the first guard test could pass vacuously (an empty set has no gap); the second
    test fails by equality. The first test also asserts the set is non-empty, as a cheap
    backstop, but the second remains the real protection.
    """
    readout = _function(_tree("readout.py"), "evaluate_manifest")
    checks = _function(_tree("comparison_plan.py"), "_checks")
    pick = _function(_tree("comparison_plan.py"), "_pick")
    develop = _function(_tree("simple_repair.py"), "assess_development")
    arm = _function(_tree("operating_point.py"), "_arm")
    econ = _function(_tree("pilot_metrics.py"), "economics")
    intake = _tree("source_intake.py")
    reason = _function(intake, "_reason")
    partition = _function(intake, "_partition")

    holds = set()
    for n in ast.walk(checks):
        if isinstance(n, ast.Return) and n.value is not None:
            holds |= _branches(n.value)
    outcomes, appended = set(), set()
    for n in ast.walk(pick):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "outcome":
            outcomes |= _strings(n.args[2])
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "append"
            and n.args
            and isinstance(n.args[0], ast.Constant)
        ):
            appended.add(n.args[0].value)
    development_holds = set()
    for n in ast.walk(develop):
        if (
            isinstance(n, ast.Assign)
            and isinstance(n.targets[0], ast.Tuple)
            and isinstance(n.value, ast.Tuple)
            and len(n.value.elts) == 2
            and all(isinstance(e, ast.Constant) for e in n.value.elts)
            and n.value.elts[0].value == "hold"
        ):
            development_holds.add(n.value.elts[1].value)
    exclusions = set()
    for n in ast.walk(reason):
        if isinstance(n, ast.Return) and n.value is not None:
            exclusions |= _branches(n.value)
    for n in ast.walk(intake):
        if isinstance(n, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "reason" for t in n.targets
        ):
            exclusions |= _branches(n.value)
    for n in ast.walk(partition):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant):
            if isinstance(n.value.value, str):
                exclusions.add(n.value.value)
        if (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Attribute)
            and n.func.attr == "update"
        ):
            exclusions |= _strings(n) - {"case_id"}
    exclusions.discard("")
    identifier = _function(_tree("simple_repair.py"), "identifier")
    shapes = {
        n.args[0].value
        for n in ast.walk(identifier)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "fullmatch"
        and isinstance(n.args[0], ast.Constant)
    }
    validated = slice_names_validated(_tree("comparison_plan.py"))
    return {
        "slice_shape": shapes if validated else {"<planner no longer validates slice names>"},
        "observed": _assigned(readout, "comparison") | {"improved"},
        "hold_reasons": holds,
        "decision_reasons": outcomes | appended | holds | development_holds,
        "combined": _assigned(arm, "combined"),
        "caps": _assigned(econ, "cost_cap") | _assigned(econ, "latency_cap"),
        "exclusions": exclusions,
    }


def vocabulary_gaps(emitted):
    """Values the planner can emit that a closed vocabulary would falsely refuse."""
    observed_reasons = {o for o in emitted["observed"] if o not in ("improved", "equivalent")}
    return {
        "observed": set(emitted["observed"]) - set(cp._OBSERVED),
        "hold_reasons": set(emitted["hold_reasons"]) - set(cp._HOLD_REASONS),
        "decision_reasons": (set(emitted["decision_reasons"]) | observed_reasons)
        - set(cp._DECISION_REASONS),
        "combined": set(emitted["combined"]) - set(cp._COMBINED),
        "caps": set(emitted["caps"]) - set(cp._CAPS),
        "exclusions": set(emitted["exclusions"]) - set(cp._EXCLUSION_KEYS),
        "slice_shape": set(emitted["slice_shape"]) - {cp._SLICE_NAME.pattern},
    }


def test_drift_guard_reads_the_planner_source_not_the_fixtures():
    """E9: every planner-emitted value is inside the vocabularies, and the guard sees the
    values that escaped before (readout.py's different_automatic_cases)."""
    emitted = planner_emitted()
    assert emitted["slice_shape"], "an empty slice shape would make the gap check vacuous"
    assert "different_automatic_cases" in emitted["observed"]
    assert "operating_point_missed_or_undetermined" in emitted["hold_reasons"]
    assert "recurring_residual_requires_model_and_compute_rights" in emitted["decision_reasons"]
    assert "group_crosses_split" in emitted["exclusions"]
    assert vocabulary_gaps(emitted) == {k: set() for k in emitted}


def test_drift_guard_fails_when_a_planner_value_is_absent_from_a_vocabulary(monkeypatch):
    """E9: demonstrate the guard has teeth by removing values and watching it report them."""
    emitted = planner_emitted()
    short = tuple(v for v in cp._OBSERVED if v != "different_automatic_cases")
    monkeypatch.setattr(cp, "_OBSERVED", short)
    monkeypatch.setattr(
        cp,
        "_DECISION_REASONS",
        tuple(v for v in cp._DECISION_REASONS if v != "different_automatic_cases"),
    )
    monkeypatch.setattr(
        cp, "_EXCLUSION_KEYS", tuple(v for v in cp._EXCLUSION_KEYS if v != "reused_outcome")
    )
    gaps = vocabulary_gaps(emitted)
    assert gaps["observed"] == {"different_automatic_cases"}
    assert gaps["decision_reasons"] == {"different_automatic_cases"}
    assert gaps["exclusions"] == {"reused_outcome"}


def test_drift_guard_fails_when_the_slice_shape_drifts_from_the_planner(monkeypatch):
    """E10: the guard that was missing. The old lowercase-only filter is reported as drift."""
    emitted = planner_emitted()
    assert emitted["slice_shape"] == {cp._SLICE_NAME.pattern}
    monkeypatch.setattr(cp, "_SLICE_NAME", re.compile(r"[a-z]+(?:_[a-z]+)*"))
    assert vocabulary_gaps(emitted)["slice_shape"] == emitted["slice_shape"]


@pytest.mark.parametrize(
    "name", ["slice1", "vip-accounts", "Tier1", "bucket:x", "enterprise.eu", "john_smith_9"]
)
def test_real_run_with_a_planner_legal_slice_name_still_stages(tmp_path, name):
    """E10 positive: a real comparison_plan run whose critical slice is a legal identifier."""
    dev, protected = rows("dev"), rows("protected")
    for r in dev + protected:
        r["slices"] = [name]
    comparison = compare(
        tmp_path,
        development=dev,
        protected=protected,
        terms={**STRICT, "critical_slices": [name]},
        allowed_slices=[name],
    )[0]
    assert comparison["decision"]["disposition"] == "recommend", comparison["decision"]
    assert set(comparison["arms"]["simple"]["critical_slices"]) == {name}
    out = Bundle(comparison, tmp_path / "stage").stage()
    assert out.status == "staged" and out.packet["kind"] == "measured_improvement", out
    assert set(out.packet["arms"]["simple"]["critical_slices"]) == {
        cp.slice_digest(name, key_for(comparison), **scope(comparison))
    }  # OPE-571: the packet carries the keyed digest, never the name


def test_drift_guard_fails_when_only_the_slice_identifier_call_is_removed():
    """E10: removing just the critical_slices identifier() call must be reported, even though
    identifier() is still called on ``metric`` elsewhere in ``_terms``."""
    source = (_PILOT / "comparison_plan.py").read_text()
    assert slice_names_validated(ast.parse(source)) is True
    needle = "len({identifier(v) for v in items}) != len(items)"
    assert source.count(needle) == 1, "planner source changed; update this mutation"
    mutated_source = source.replace(needle, "len({v for v in items}) != len(items)")
    assert 'identifier(value["metric"])' in mutated_source  # the unrelated call survives
    assert slice_names_validated(ast.parse(mutated_source)) is False


def test_drift_guard_itself_reports_drift_when_only_the_slice_identifier_call_is_removed(
    monkeypatch,
):
    """E10: the guard end to end, not only its helper. ``planner_emitted`` and
    ``vocabulary_gaps`` run on a planner source with just the slice ``identifier()`` call
    removed, so a guard that stopped using ``slice_names_validated`` (for example by going
    back to "any ``identifier()`` anywhere in ``_terms``") is caught here."""
    assert vocabulary_gaps(planner_emitted())["slice_shape"] == set()  # unmutated: no drift
    source = (_PILOT / "comparison_plan.py").read_text()
    needle = "len({identifier(v) for v in items}) != len(items)"
    assert source.count(needle) == 1, "planner source changed; update this mutation"
    mutated = ast.parse(source.replace(needle, "len({v for v in items}) != len(items)"))
    real_tree = _tree
    monkeypatch.setitem(
        globals(),
        "_tree",
        lambda name: mutated if name == "comparison_plan.py" else real_tree(name),
    )
    emitted = planner_emitted()
    gaps = vocabulary_gaps(emitted)
    assert gaps["slice_shape"] == {"<planner no longer validates slice names>"}
    assert {k: v for k, v in gaps.items() if k != "slice_shape"} == {
        k: set() for k in gaps if k != "slice_shape"
    }  # only the slice check drifted; the planner's other vocabularies are unchanged


# ------------------------------------- S: critical-slice names as keyed digests (OPE-571, D2)

# Generated sentinel names shaped like the guessable names D2 warns about. Not real people.
SLICE_NAMES = ("jane_doe", "acme_corp")


def named_slice_run(root):
    dev, protected = rows("dev"), rows("protected")
    for r in dev + protected:
        r["slices"] = list(SLICE_NAMES)
    comparison = compare(
        root,
        development=dev,
        protected=protected,
        terms={**STRICT, "critical_slices": list(SLICE_NAMES)},
        allowed_slices=list(SLICE_NAMES),
    )[0]
    assert comparison["decision"]["disposition"] == "recommend", comparison["decision"]
    assert set(comparison["arms"]["simple"]["critical_slices"]) == set(SLICE_NAMES)
    return comparison


def in_another_engagement(comparison, engagement="generated-engagement-b"):
    assert comparison["engagement"] != engagement
    return seal({**copy.deepcopy(comparison), "engagement": engagement})


def every_text(value):
    """Every dict key and string leaf, recursively: a name hidden at any depth is found."""
    if isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from every_text(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from every_text(v)
    elif isinstance(value, (str, bytes)):
        yield value if isinstance(value, str) else value.hex()
    elif value is not None:
        yield str(value)


def mentions(value, needle):
    return [t for t in every_text(value) if needle.lower() in t.lower()]


def test_packet_carries_keyed_slice_digests_not_names(tmp_path):
    """S1: each published slice key is HMAC-SHA-256 under the engagement key, status preserved."""
    comparison = named_slice_run(tmp_path)
    out = Bundle(comparison, tmp_path / "stage").stage()
    assert out.status == "staged", out
    key = key_for(comparison)
    for arm, aggregate in out.packet["arms"].items():
        expected = {
            cp.slice_digest(name, key, **scope(comparison)): v["status"]
            for name, v in comparison["arms"][arm]["critical_slices"].items()
        }
        assert aggregate["critical_slices"] == expected
        assert all(re.fullmatch("[0-9a-f]{64}", d) for d in aggregate["critical_slices"])


def test_a_guessed_plain_sha256_of_a_slice_name_matches_nothing(tmp_path):
    """S2 adverse: a guesser hashing candidate names (D2's ``jane_doe``) finds no match."""
    import hashlib

    comparison = named_slice_run(tmp_path)
    out = Bundle(comparison, tmp_path / "stage").stage()
    published = set(every_text(out.packet))
    for name in SLICE_NAMES:
        guesses = {
            hashlib.sha256(name.encode()).hexdigest(),
            digest(name),  # the repository's canonical-JSON SHA-256
            hashlib.sha256((cp.SLICE_DIGEST + name).encode()).hexdigest(),
        }
        assert not guesses & published, name
        assert not any(g[:32] in t for g in guesses for t in published), name


def test_identical_names_under_two_engagements_yield_different_digests(tmp_path):
    """S3: the same names under another engagement's key give unlinkable digests."""
    first = named_slice_run(tmp_path)
    second = in_another_engagement(first)
    a = Bundle(first, tmp_path / "a").stage()
    staged_b = Bundle(second, tmp_path / "b").stage()
    assert a.status == staged_b.status == "staged", (a, staged_b)
    assert key_for(first) != key_for(second)
    for arm in a.packet["arms"]:
        da = set(a.packet["arms"][arm]["critical_slices"])
        db = set(staged_b.packet["arms"][arm]["critical_slices"])
        assert len(da) == len(db) == len(SLICE_NAMES) and not da & db


def test_no_slice_name_or_key_appears_anywhere_in_packet_event_or_outbox(tmp_path):
    """S4: recursive scan of the packet, its event, every outbox row and the raw store file."""
    comparison = named_slice_run(tmp_path)
    b = Bundle(comparison, tmp_path / "stage")
    out = b.stage()
    assert out.status == "staged", out
    b.dispatch()
    event = next(iter(b.sink.effects.values()))
    raw = b.store.path.read_bytes()
    key = key_for(comparison)
    for name in SLICE_NAMES:
        assert mentions(out.packet, name) == [], name
        assert mentions(event, name) == [], name
        assert mentions(b.store.snapshot(), name) == [], name
        assert name.encode() not in raw, name
    for secret in (key.hex(), key.hex()[:32]):
        assert mentions([out.packet, event, b.store.snapshot()], secret) == []
    assert key not in raw and key.hex().encode() not in raw


def test_packet_states_the_slice_digest_is_not_issuer_authenticated(tmp_path):
    """S5: until OPE-566 the key is engagement-local; the packet says so explicitly."""
    comparison = named_slice_run(tmp_path)
    out = Bundle(comparison, tmp_path / "stage").stage()
    assert out.packet["critical_slice_names"] == {
        "form": "keyed_digest",
        "algorithm": "hmac_sha256",
        "key_scope": "engagement",
        "keyed_engagement": comparison["engagement"],
        "key_custody": "customer_worker_local",
        "names_disclosed": False,
        # LOW-1: the arm field is keyed, but on an improvement packet the remedy binding makes
        # ``binding.fallback_digest`` the plain incumbent revision, and the packet says so.
        "incumbent_revision": {
            "arm_revision": "keyed_digest",
            "linkable_across_engagements_via_fallback_digest": True,
        },
        "issuer_authenticated": False,
    }
    assert out.packet["binding"]["fallback_digest"] == comparison["arms"]["incumbent"]["revision"]
    assert out.packet["audit"]["issuer_authenticated"] is False


def test_private_view_resolves_readable_names_with_the_engagement_key(tmp_path):
    """S6: the customer's own view maps every published digest back to its name and status."""
    comparison = named_slice_run(tmp_path)
    b = Bundle(comparison, tmp_path / "stage")
    out = b.stage()
    before = b.store.snapshot()
    view = cp.private_slice_view(out.packet, comparison, key_for(comparison))
    assert view == {
        arm: {name: v["status"] for name, v in sorted(a["critical_slices"].items())}
        for arm, a in sorted(comparison["arms"].items())
    }
    assert set(view["simple"]) == set(SLICE_NAMES)
    assert b.store.snapshot() == before, "the private view must not be stored"


def test_private_view_refuses_wrong_key_tampering_or_another_engagement(tmp_path):
    """S6 adverse: no partial or guessed resolution."""
    comparison = named_slice_run(tmp_path)
    out = Bundle(comparison, tmp_path / "stage").stage()
    other = in_another_engagement(comparison)
    assert cp.private_slice_view(out.packet, comparison, key_for(other)) is None
    assert cp.private_slice_view(out.packet, comparison, None) is None
    assert cp.private_slice_view(out.packet, comparison, b"short") is None
    tampered = copy.deepcopy(out.packet)
    tampered["arms"]["simple"]["critical_slices"] = {digest("jane_doe"): "met"}
    body = {k: v for k, v in tampered.items() if k != "packet_digest"}
    resealed = {**body, "packet_digest": digest(body)}
    assert cp.private_slice_view(resealed, comparison, key_for(comparison)) is None
    unsealed = {**out.packet, "kind": "hold"}
    assert cp.private_slice_view(unsealed, comparison, key_for(comparison)) is None


@pytest.mark.parametrize("name", ["winner", _PLAIN_HOLD])
def test_missing_engagement_key_holds_instead_of_falling_back_to_plain_hashing(
    runs, tmp_path, name
):
    """S7 adverse: no key, no packet, on the improvement path and on the live hold path."""
    comparison = runs[name]
    b = Bundle(comparison, tmp_path)
    missing = Keyring(missing={(comparison["organization"], comparison["engagement"])})
    b.store = cp.PacketOutbox(tmp_path / "nokey.sqlite3", slice_keys=missing)
    if name == _PLAIN_HOLD:
        b.receipt = b.verdict = None
    out = b.stage()
    assert out.status == "refused" and out.reason == "slice_digest_key_unavailable", out
    assert out.packet is None and out.event_id is None
    assert out.scoped_hold["release_authority"] is False
    assert empty(b.store) and b.dispatch() == [] and b.sink.calls == []


@pytest.mark.parametrize(
    "key",
    [
        None,
        b"",
        b"\x01\x02" * 8,
        b"\x01\x02" * 16 + b"\x03",
        "00" * 32,
        bytearray(range(32)),
        0,
        type("BytesSub", (bytes,), {})(range(32)),
        bytes(32),
        b"a" * 32,
    ],
    ids=[
        "none",
        "empty",
        "short",
        "long",
        "hex-string",
        "bytearray",
        "int",
        "bytes-subclass",
        "all-zero",
        "one-repeated-value",
    ],
)
def test_malformed_engagement_key_refuses_every_entry_point(runs, tmp_path, key):
    """S7 adverse: the store, assemble, verify and private view all refuse a malformed key."""
    comparison = runs["winner"]
    b = Bundle(comparison, tmp_path)

    class Bad:
        def slice_key(self, tenant, engagement):
            return key

    b.store = cp.PacketOutbox(tmp_path / "bad.sqlite3", slice_keys=Bad())
    out = b.stage()
    assert out.reason == "slice_digest_key_unavailable" and empty(b.store), out
    args = (comparison, b.receipt, b.verdict, b.ctx)
    assert cp.assemble_packet(*args, slice_key=key, allow_generated_fixture=True) == (
        None,
        "slice_digest_key_unavailable",
    )
    good = Bundle(comparison, tmp_path / "good").stage().packet
    assert cp.verify_packet(good, *args, slice_key=key, allow_generated_fixture=True) is False
    assert cp.private_slice_view(good, comparison, key) is None


def test_outbox_without_a_keyring_refuses_and_invalid_engagement_is_not_looked_up(runs, tmp_path):
    """S7 adverse: the default store has no keys; a non-identifier engagement is never used."""
    b = Bundle(runs[_PLAIN_HOLD], tmp_path)
    b.receipt = b.verdict = None
    b.store = cp.PacketOutbox(tmp_path / "default.sqlite3")
    assert b.stage().reason == "slice_digest_key_unavailable" and empty(b.store)
    seen = []

    class Spy:
        def slice_key(self, tenant, engagement):
            seen.append(engagement)
            return KEYS.slice_key(tenant, engagement)

    odd = seal({**copy.deepcopy(runs[_PLAIN_HOLD]), "engagement": "case-9 sentinel"})
    b.store = cp.PacketOutbox(tmp_path / "spy.sqlite3", slice_keys=Spy())
    out = b.stage(comparison=odd)
    # The engagement is bound into the key derivation, so a non-identifier one is unverified.
    assert out.reason == "comparison_unverified" and seen == [] and empty(b.store)
    assert not _leaks(out, "case-9 sentinel")


def test_comparison_refusals_still_precede_the_key_check(runs, tmp_path):
    """S7: an unverified comparison is reported as such, never masked as a key problem."""
    sealed = mutated(runs[_PLAIN_HOLD], "slice_name")
    assert cp.assemble_packet(sealed, None, None, context(sealed), slice_key=None) == (
        None,
        "comparison_unverified",
    )


def test_fixture_exclusion_keys_are_inside_the_derivable_set(tmp_path):
    """S8: the fixture's exclusion key is one source_intake can derive, and the closed
    vocabulary is exactly the derivable set (no extra, no missing key)."""
    derivable = planner_emitted()["exclusions"]
    assert set(cp._EXCLUSION_KEYS) == derivable
    (tmp_path / "enroll").mkdir(mode=0o700)
    store = LocalTrainingStore(tmp_path / "enroll" / "training.sqlite3", "generated")
    assert set(enroll(store)["excluded_counts"]) <= derivable
    comparison = compare(tmp_path)[0]
    assert set(comparison["exclusions"]) <= derivable
    assert Bundle(comparison, tmp_path / "stage").stage().status == "staged"


def test_the_previous_out_of_set_fixture_key_is_refused(runs, tmp_path):
    """S8 adverse: ``late_feature`` (the old fixture key) is not derivable and is refused."""
    assert "late_feature" not in planner_emitted()["exclusions"]
    sealed = seal({**copy.deepcopy(runs[_PLAIN_HOLD]), "exclusions": {"late_feature": 1}})
    b = live_bundle(sealed, tmp_path)
    out = b.stage()
    assert out.reason == "comparison_unverified" and not _leaks(out, "late_feature")
    assert empty(b.store)


# ---------------- P: operating-point digest over slice-substituted terms (OPE-571 follow-up)


def named_material_run(root):
    """A real run whose terms name both a critical and a material slice."""
    dev, protected = rows("dev"), rows("protected")
    for r in dev + protected:
        r["slices"] = list(SLICE_NAMES)
    comparison = compare(
        root,
        development=dev,
        protected=protected,
        terms={
            **STRICT,
            "critical_slices": [SLICE_NAMES[0]],
            "material_slices": [SLICE_NAMES[1]],
        },
        allowed_slices=list(SLICE_NAMES),
    )[0]
    assert comparison["decision"]["disposition"] == "recommend", comparison["decision"]
    return comparison


def test_operating_point_digest_cannot_confirm_a_guessed_slice_name(tmp_path):
    """P1 adverse: knowing every other term exactly, a guesser still cannot confirm a name.

    The guesser tries the TRUE names, plain and under a wrong key; nothing matches. Only the
    engagement key reproduces the published digest (positive control)."""
    comparison = named_material_run(tmp_path)
    out = Bundle(comparison, tmp_path / "stage").stage()
    assert out.status == "staged", out
    published = out.packet["comparison"]["operating_point_digest"]
    terms = comparison["terms"]
    wrong = bytes.fromhex(digest("guesser-key"))
    guesses = [
        digest(terms),  # the pre-change digest over raw names
        digest({**terms, "critical_slices": ["acme_corp"], "material_slices": ["jane_doe"]}),
        cp.operating_point_digest(cp.slice_substituted_terms(terms, wrong, **scope(comparison))),
        cp.operating_point_digest(
            {
                **terms,
                "critical_slices": [digest(n) for n in terms["critical_slices"]],
                "material_slices": [digest(n) for n in terms["material_slices"]],
            }
        ),
    ]
    assert published not in guesses
    key = key_for(comparison)
    assert published == cp.operating_point_digest(
        cp.slice_substituted_terms(terms, key, **scope(comparison))
    )


def test_operating_point_digest_is_verifiable_from_substituted_terms_without_the_key(tmp_path):
    """P2: a verifier holding only the substituted terms (no key, fresh interpreter, plain
    hashlib over the repository canonical form) reproduces the published binding."""
    comparison = named_material_run(tmp_path)
    b = Bundle(comparison, tmp_path / "stage")
    out = b.stage()
    substituted = cp.slice_substituted_terms(
        comparison["terms"], key_for(comparison), **scope(comparison)
    )
    for name in SLICE_NAMES:
        assert mentions(substituted, name) == [], name
    assert {k: v for k, v in substituted.items() if not k.endswith("_slices")} == {
        k: v for k, v in comparison["terms"].items() if not k.endswith("_slices")
    }
    script = (
        "import hashlib,json,sys\n"
        "from opentour_cell.pilot.operating_point import canonical\n"
        "t=json.load(sys.stdin)\n"
        "print(hashlib.sha256(canonical(t).encode()).hexdigest())\n"
    )
    proc = subprocess.run(
        [sys.executable, "-B", "-c", script],
        input=json.dumps(substituted),
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    published = out.packet["comparison"]["operating_point_digest"]
    assert proc.stdout.strip() == published == b.verdict["operating_point_digest"]


def test_verdict_without_the_digest_form_predates_slice_digests(tmp_path):
    """P3/F1: a verdict that does not declare the substituted form is refused by the field's
    absence alone, whatever digest it carries, and is never re-digested."""
    comparison = named_material_run(tmp_path)
    b = Bundle(comparison, tmp_path / "stage")
    for claimed in (digest(comparison["terms"]), b.verdict["operating_point_digest"]):
        body = {k: v for k, v in b.verdict.items() if k not in ("verdict_digest",)}
        body.pop("operating_point_digest_form")
        legacy = reseal_verdict({**body, "operating_point_digest": claimed})
        out = b.stage(verdict=legacy)
        assert out.reason == "operating_point_digest_predates_slice_digests", out
        assert out.packet is None and empty(b.store)
        assert legacy["operating_point_digest"] == claimed  # untouched
    assert b.stage().status == "staged"  # the declared, substituted binding still stages


def test_terms_without_slice_names_keep_their_digest(runs, tmp_path):
    """P3 positive: with no slice names there is nothing to substitute or to leak, so the
    digest is unchanged and an existing verdict still binds."""
    comparison = runs["winner"]
    terms = comparison["terms"]
    assert not terms.get("critical_slices") and not terms.get("material_slices")
    key = key_for(comparison)
    assert cp.operating_point_digest(
        cp.slice_substituted_terms(terms, key, **scope(comparison))
    ) == digest(terms)
    b = Bundle(comparison, tmp_path)
    plain = make_verdict(comparison, b.receipt, b.ctx, operating_point_digest=digest(terms))
    assert b.stage(verdict=plain).status == "staged"


@pytest.mark.parametrize(
    "terms_slices",
    [{"critical_slices": "jane_doe"}, {"material_slices": [7]}, {"critical_slices": ["a b"]}],
    ids=["not-a-list", "not-a-string", "bad-shape"],
)
def test_malformed_slice_terms_refuse_without_raising(runs, tmp_path, terms_slices):
    """P4 adverse: digested terms are still checked, so substitution can never raise."""
    changed = copy.deepcopy(runs[_PLAIN_HOLD])
    changed["terms"] = {**changed["terms"], **terms_slices}
    b = live_bundle(runs[_PLAIN_HOLD], tmp_path)  # the helper's verdict needs valid terms
    out = b.stage(comparison=seal(changed))
    assert out.reason == "comparison_unverified" and empty(b.store), out


# ------------------- R: independent-review findings on b63b4ea (OPE-571 follow-up)

KAT_KEY = bytes(range(32))
KAT_SCOPE = {"tenant": "generated-tenant", "engagement": "generated-engagement-a"}
KAT_DIGEST = "ff7b8bd6cbcdb4e48bcfd641fb2377a4f3d1615758117a7cf9e561cfa7b6b887"


def test_slice_digest_known_answer_pins_the_derivation():
    """R2: a literal known answer, also rebuilt step by step with the standard library:
    scoped = HMAC(key, canonical([purpose, tenant, engagement])); digest = HMAC(scoped, name)."""
    import hashlib
    import hmac

    assert cp.slice_digest("jane_doe", KAT_KEY, **KAT_SCOPE) == KAT_DIGEST
    label = json.dumps(
        [cp.SLICE_DIGEST, KAT_SCOPE["tenant"], KAT_SCOPE["engagement"]], separators=(",", ":")
    ).encode()
    scoped = hmac.new(KAT_KEY, label, hashlib.sha256).digest()
    assert hmac.new(scoped, b"jane_doe", hashlib.sha256).hexdigest() == KAT_DIGEST
    # The derivations a mutation could fall back to must all differ from the pinned answer.
    unscoped = hmac.new(KAT_KEY, b"jane_doe", hashlib.sha256).hexdigest()
    purpose_only = hmac.new(
        hmac.new(KAT_KEY, cp.SLICE_DIGEST.encode(), hashlib.sha256).digest(),
        b"jane_doe",
        hashlib.sha256,
    ).hexdigest()
    assert KAT_DIGEST not in (unscoped, purpose_only, hashlib.sha256(b"jane_doe").hexdigest())


@pytest.mark.parametrize(
    "other",
    [
        {"tenant": "generated-tenant-2", "engagement": "generated-engagement-a"},
        {"tenant": "generated-tenant", "engagement": "generated-engagement-b"},
    ],
    ids=["tenant", "engagement"],
)
def test_one_shared_master_key_still_gives_unlinkable_digests(other):
    """R2: tenant and engagement are bound into the derivation, not only the purpose label."""
    assert cp.slice_digest("jane_doe", KAT_KEY, **other) != KAT_DIGEST
    terms = {**STRICT, "critical_slices": ["jane_doe"], "material_slices": ["acme_corp"]}
    a = cp.slice_substituted_terms(terms, KAT_KEY, **KAT_SCOPE)
    b = cp.slice_substituted_terms(terms, KAT_KEY, **other)
    assert set(a["critical_slices"] + a["material_slices"]).isdisjoint(
        b["critical_slices"] + b["material_slices"]
    )


def test_shared_master_key_across_engagements_in_a_staged_packet(tmp_path):
    """R2: the same keyring key for two engagements still yields disjoint published digests."""

    class Shared:
        def slice_key(self, tenant, engagement):
            return KAT_KEY

    first = named_slice_run(tmp_path)
    second = in_another_engagement(first)
    out = []
    for i, comparison in enumerate((first, second)):
        b = Bundle(comparison, tmp_path / f"s{i}")
        b.store = cp.PacketOutbox(
            tmp_path / f"s{i}" / "shared.sqlite3", slice_keys=Shared(), allow_generated_fixture=True
        )
        b.verdict = make_verdict(
            comparison,
            b.receipt,
            b.ctx,
            operating_point_digest=cp.operating_point_digest(
                cp.slice_substituted_terms(comparison["terms"], KAT_KEY, **scope(comparison))
            ),
        )
        out.append(b.stage())
    assert [o.status for o in out] == ["staged", "staged"], out
    for arm in out[0].packet["arms"]:
        assert set(out[0].packet["arms"][arm]["critical_slices"]).isdisjoint(
            out[1].packet["arms"][arm]["critical_slices"]
        )
    ops = [o.packet["comparison"]["operating_point_digest"] for o in out]
    assert ops[0] != ops[1]


CANDIDATE_NAMES = ("jane_doe", "acme_corp", "john_smith", "critical", "rare", "vip", "Tier1")


def _candidate_terms():
    """Every one- and two-name guess, both orders, on top of the exactly known STRICT terms."""
    import itertools

    for size in (1, 2):
        for names in itertools.permutations(CANDIDATE_NAMES, size):
            yield {**STRICT, "critical_slices": list(names)}


def test_brute_force_over_candidate_names_cannot_reproduce_any_published_digest(tmp_path):
    """R1/R4: the reviewer's probe. With STRICT known and the true names among the candidates,
    no plain or unkeyed-substituted reconstruction reproduces the packet's operating-point
    digest, the verdict binding, or any other 64-hex value the packet publishes."""
    import hashlib

    comparison = named_slice_run(tmp_path)
    b = Bundle(comparison, tmp_path / "stage")
    out = b.stage()
    assert out.status == "staged", out
    assert comparison["terms"] == {**STRICT, "critical_slices": list(SLICE_NAMES)}
    published = {t for t in every_text(out.packet) if re.fullmatch("[0-9a-f]{64}", t)}
    assert out.packet["comparison"]["operating_point_digest"] in published
    assert b.verdict["operating_point_digest"] == out.packet["comparison"]["operating_point_digest"]
    tried = 0
    for terms in _candidate_terms():
        names = terms["critical_slices"]
        for guess in (
            terms,
            {**terms, "critical_slices": [hashlib.sha256(n.encode()).hexdigest() for n in names]},
            {**terms, "critical_slices": [digest(n) for n in names]},
        ):
            tried += 1
            assert digest(guess) not in published, names
    assert tried == 3 * (len(CANDIDATE_NAMES) + len(CANDIDATE_NAMES) * (len(CANDIDATE_NAMES) - 1))
    # Positive control: the guess that matches only the TRUE names under the real key.
    key = key_for(comparison)
    true = cp.slice_substituted_terms(comparison["terms"], key, **scope(comparison))
    assert digest(true) == out.packet["comparison"]["operating_point_digest"]


def test_names_disclosed_false_holds_for_every_published_value(tmp_path):
    """R4: the packet's ``names_disclosed: False`` is true. No name in clear, no guessable
    digest of one, and the documents whose plain digests do include the names (plan and
    comparison revisions) also bind an intake revision that is never published."""
    comparison = named_slice_run(tmp_path)
    b = Bundle(comparison, tmp_path / "stage")
    out = b.stage()
    b.dispatch()
    assert out.packet["critical_slice_names"]["names_disclosed"] is False
    surfaces = [out.packet, next(iter(b.sink.effects.values())), b.store.snapshot()]
    for name in SLICE_NAMES:
        assert mentions(surfaces, name) == [], name
    assert mentions(surfaces, comparison["intake_revision"]) == []
    assert mentions(surfaces, comparison["intake_revision"][:16]) == []


def test_a_raising_keyring_refuses_instead_of_raising(runs, tmp_path):
    """R3: an exception from the keyring is an unknown result: refused, never raised, nothing
    staged, and the exception text never reaches the refusal."""

    class Broken:
        def slice_key(self, tenant, engagement):
            raise RuntimeError("case-12 sentinel-keyring-detail")

    for name in ("winner", _PLAIN_HOLD):
        b = Bundle(runs[name], tmp_path / name)
        if name == _PLAIN_HOLD:
            b.receipt = b.verdict = None
        b.store = cp.PacketOutbox(
            tmp_path / name / "broken.sqlite3", slice_keys=Broken(), allow_generated_fixture=True
        )
        out = b.stage()
        assert out.status == "refused" and out.reason == "slice_digest_key_lookup_failed", out
        assert out.packet is None and empty(b.store) and b.dispatch() == []
        assert not _leaks(out, "sentinel-keyring-detail")


@pytest.mark.parametrize("problem", ["unverified", "scope", "context"])
def test_stage_refuses_comparison_problems_before_any_key_lookup(runs, tmp_path, problem):
    """R3: comparison and context refusals precede the key lookup in ``stage`` itself."""
    seen = []

    class Spy:
        def slice_key(self, tenant, engagement):
            seen.append((tenant, engagement))
            return KEYS.slice_key(tenant, engagement)

    b = live_bundle(runs[_PLAIN_HOLD], tmp_path)
    b.store = cp.PacketOutbox(tmp_path / "spy.sqlite3", slice_keys=Spy())
    expected = {
        "unverified": "comparison_unverified",
        "scope": "comparison_scope_mismatch",
        "context": "invalid_packet_context",
    }[problem]
    if problem == "unverified":
        out = b.stage(comparison=mutated(runs[_PLAIN_HOLD], "slice_name"))
    else:
        over = {"tenant": "generated-other-tenant"} if problem == "scope" else {"expires_at": "x"}
        b.ctx = context(runs[_PLAIN_HOLD], **over)
        b.authority = Authority(b.ctx)
        out = b.stage()
    assert out.reason == expected and seen == [] and empty(b.store), out
    b.ctx = context(runs[_PLAIN_HOLD])
    b.authority = Authority(b.ctx)
    assert b.stage().status == "staged" and len(seen) == 1  # positive control: one lookup


# ---------- F: lane-owner review of d7520b0 (key-free oracle, engagement, weak keys, holds)


def named_hold(root, incumbent=None):
    """A real named-slice run that the planner holds (the reviewer's probe-4 shape)."""
    dev, protected = rows("dev"), rows("protected")
    for r in dev + protected:
        r["slices"] = list(SLICE_NAMES)
    comparison = compare(
        root,
        development=dev,
        protected=protected,
        terms={**STRICT, "critical_slices": list(SLICE_NAMES), "minimum_verified_cases": 1000},
        allowed_slices=list(SLICE_NAMES),
        incumbent=incumbent,
    )[0]
    assert comparison["decision"]["disposition"] == "hold", comparison["decision"]
    return comparison


def raw_digest_with(comparison, names):
    """An attacker's or a legacy worker's digest: plain SHA-256 over raw terms with ``names``."""
    return digest({**comparison["terms"], "critical_slices": list(names)})


def verdict_with(b, comparison, *, claimed, declared=True):
    body = {k: v for k, v in make_verdict(comparison, b.receipt, b.ctx).items()}
    body.pop("verdict_digest")
    if not declared:
        body.pop("operating_point_digest_form")
    return reseal_verdict({**body, "operating_point_digest": claimed})


def path_bundle(path, tmp_path):
    """('improvement', fixture store), ('improvement-live', live store), ('hold', live store)."""
    if path == "hold":
        comparison = named_hold(tmp_path)
        b = live_bundle(comparison, tmp_path / "b")
        b.receipt = make_receipt()
        b.verdict = make_verdict(comparison, b.receipt, b.ctx)  # a hold publishes its digest
        return comparison, b
    if path == "hold-identifier-incumbent":
        comparison = named_hold(tmp_path, incumbent=INCUMBENT_ID)
        b = live_bundle(comparison, tmp_path / "b")
        b.ctx = context(comparison, fallback_digest=digest("generated-fallback"))
        b.authority = Authority(b.ctx)
        b.receipt = make_receipt()
        # OPE-528 emits content digests, never the identifier, as the comparator.
        b.verdict = make_verdict(
            comparison, b.receipt, b.ctx, comparator_digest=digest("generated-cmp")
        )
        return comparison, b
    comparison = named_slice_run(tmp_path)
    b = Bundle(comparison, tmp_path / "b")
    if path == "improvement-live":
        b.store = cp.PacketOutbox(tmp_path / "b" / "live.sqlite3", slice_keys=KEYS)
    return comparison, b


PATHS = ["improvement", "improvement-live", "hold"]


@pytest.mark.parametrize("path", PATHS)
def test_correct_and_wrong_raw_name_guesses_get_the_identical_reason(tmp_path, path):
    """F1 (blocker): no reason code separates a correct raw-name guess from a wrong one.

    Declared form: a raw-terms digest with the TRUE names, with wrong names, and a random
    digest all get ``verdict_binding_mismatch``. Undeclared form: all three get
    ``operating_point_digest_predates_slice_digests``. Nothing is staged in any case."""
    comparison, b = path_bundle(path, tmp_path)
    claims = {
        "true_raw_guess": raw_digest_with(comparison, SLICE_NAMES),
        "wrong_raw_guess": raw_digest_with(comparison, ("john_smith", "vip")),
        "random": digest("generated-random-point"),
    }
    for declared, expected in (
        (True, "verdict_binding_mismatch"),
        (False, "operating_point_digest_predates_slice_digests"),
    ):
        reasons = {
            label: b.stage(verdict=verdict_with(b, comparison, claimed=claimed, declared=declared))
            for label, claimed in claims.items()
        }
        assert {label: o.reason for label, o in reasons.items()} == dict.fromkeys(
            claims, expected
        ), (declared, reasons)
        assert all(o.packet is None for o in reasons.values()) and empty(b.store)
    honest = b.stage()
    assert honest.reason in (None, "audit_and_verdict_issuers_unverified"), honest


@pytest.mark.parametrize("path", PATHS)
def test_unknown_declared_form_is_a_binding_mismatch(tmp_path, path):
    """F1: a declared but different form is not the legacy case and compares nothing raw."""
    comparison, b = path_bundle(path, tmp_path)
    good = make_verdict(comparison, b.receipt, b.ctx)
    body = {k: v for k, v in good.items() if k != "verdict_digest"}
    out = b.stage(verdict=reseal_verdict({**body, "operating_point_digest_form": "raw_terms.v0"}))
    assert out.reason == "verdict_binding_mismatch" and empty(b.store), out


def test_no_code_path_hashes_unsubstituted_terms(tmp_path, monkeypatch):
    """F1: spy on ``digest``, ``canonical`` (both the packet module's and the one ``digest``
    uses) and ``operating_point_digest`` across staging, holds, refusals, replay, dispatch and
    the private view. No raw sentinel name may reach them, with exactly one recorded
    exception: verifying the comparison's own ``evidence_revision`` seal, which by definition
    hashes the whole comparison (see F4 in the module docstring). Raw ``terms`` never reach
    them on their own, and ``operating_point_digest`` never sees a raw name at all."""
    from opentour_cell.pilot import operating_point as op_mod

    for sub in ("w", "h"):
        (tmp_path / sub).mkdir(mode=0o700)
    win = named_slice_run(tmp_path / "w")
    hold = named_hold(tmp_path / "h")
    seals = [{k: v for k, v in c.items() if k != "evidence_revision"} for c in (win, hold)]
    wb = Bundle(win, tmp_path / "wb")
    hb = live_bundle(hold, tmp_path / "hb")
    hb.receipt = make_receipt()
    hb.verdict = make_verdict(hold, hb.receipt, hb.ctx)
    verdicts = {
        name: (b, c, verdict_with(b, c, claimed=raw_digest_with(c, SLICE_NAMES), declared=d))
        for name, b, c, d in (
            ("win-raw-declared", wb, win, True),
            ("win-raw-undeclared", wb, win, False),
            ("hold-raw-declared", hb, hold, True),
            ("hold-raw-undeclared", hb, hold, False),
        )
    }
    calls = []

    def spy(name, real):
        def wrapped(value, *args, **kwargs):
            if any(mentions(value, n) for n in SLICE_NAMES):
                calls.append((name, value))
            return real(value, *args, **kwargs)

        return wrapped

    monkeypatch.setattr(cp, "digest", spy("cp.digest", cp.digest))
    monkeypatch.setattr(cp, "canonical", spy("cp.canonical", cp.canonical))
    monkeypatch.setattr(op_mod, "canonical", spy("operating_point.canonical", op_mod.canonical))
    monkeypatch.setattr(
        cp, "operating_point_digest", spy("operating_point_digest", cp.operating_point_digest)
    )
    for _, (b, _c, verdict) in verdicts.items():
        assert b.stage(verdict=verdict).status == "refused"
    staged = [wb.stage(), hb.stage()]
    assert [o.status for o in staged] == ["staged", "staged"], staged
    wb.dispatch()
    hb.dispatch()
    key = key_for(win)
    assert cp.verify_packet(
        staged[0].packet,
        win,
        wb.receipt,
        wb.verdict,
        wb.ctx,
        slice_key=key,
        allow_generated_fixture=True,
    )
    assert cp.private_slice_view(staged[0].packet, win, key) is not None
    monkeypatch.undo()
    offenders = [(name, value) for name, value in calls if value not in seals]
    assert offenders == [], [name for name, _ in offenders]
    assert all(name != "operating_point_digest" for name, _ in calls)
    assert all(value != c["terms"] for _, value in calls for c in (win, hold))
    assert calls, "the spy must have seen the evidence seal, or it is not wired in"


def test_relabelled_and_resealed_comparison_is_refused_before_key_lookup(runs, tmp_path):
    """F2: the engagement comes from the worker's context, not from the comparison's author."""
    seen = []

    class Spy:
        def slice_key(self, tenant, engagement):
            seen.append(engagement)
            return KEYS.slice_key(tenant, engagement)

    base = runs[_PLAIN_HOLD]
    relabelled = seal({**copy.deepcopy(base), "engagement": "generated-engagement-b"})
    b = live_bundle(base, tmp_path)  # context resolved for the real engagement
    b.store = cp.PacketOutbox(tmp_path / "spy.sqlite3", slice_keys=Spy())
    out = b.stage(comparison=relabelled)
    assert out.status == "refused" and out.reason == "comparison_engagement_mismatch", out
    assert seen == [] and empty(b.store) and not _leaks(out, "generated-engagement-b")
    honest = b.stage()
    assert honest.status == "staged" and seen == [base["engagement"]]
    assert honest.packet["critical_slice_names"]["keyed_engagement"] == base["engagement"]
    assert honest.packet["binding"]["engagement"] == base["engagement"]


def test_context_engagement_must_be_an_identifier(runs, tmp_path):
    """F2: the context's engagement is validated with every other context identifier."""
    b = live_bundle(runs[_PLAIN_HOLD], tmp_path)
    b.ctx = context(runs[_PLAIN_HOLD], engagement="case-9 sentinel")
    b.authority = Authority(b.ctx)
    out = b.stage()
    assert out.reason == "invalid_packet_context" and empty(b.store), out


def test_probe4_hold_with_a_legacy_verdict_publishes_nothing(tmp_path):
    """F5 regression (reviewer probe 4): a named-slice hold given a verdict bound to the old
    raw-terms digest used to stage and publish ``independent_verdict_digest``, from which the
    names were brute-forced. Now it is refused on the hold path, declared form or not."""
    comparison = named_hold(tmp_path)
    b = live_bundle(comparison, tmp_path / "h")
    b.receipt = make_receipt()
    raw = digest(comparison["terms"])
    for declared, reason in (
        (False, "operating_point_digest_predates_slice_digests"),
        (True, "verdict_binding_mismatch"),
    ):
        out = b.stage(verdict=verdict_with(b, comparison, claimed=raw, declared=declared))
        assert out.status == "refused" and out.reason == reason, out
        assert out.packet is None and empty(b.store) and b.dispatch() == []
    assert b.stage(verdict=make_verdict(comparison, b.receipt, b.ctx)).status == "staged"


HEX_RUN = re.compile(r"[0-9a-f]{16,}")


def published_hex_runs(*surfaces):
    """Every hex run of 16+ characters anywhere in the surfaces, found generically: dict keys,
    string leaves, tuples from outbox rows, and JSON text stored in those rows (parsed and
    walked again). Truncated digests such as ``evt-``/``pkt-`` ids are included.

    The COLLECTION is generic, but the brute force below only compares these runs with the
    fixed reconstruction forms in ``candidate_reconstructions``. A digest field added later in
    any other form (domain-separated, salted with a constant, over joined names, ...) is
    collected here and still NOT caught by that test. The generic guard against such a field
    is the differential test (``key_free_leaves``, section D)."""
    found = set()

    def walk(value):
        for text in every_text(value):
            found.update(HEX_RUN.findall(text))
            if text[:1] in "{[":
                try:
                    walk(json.loads(text))
                except ValueError:
                    pass

    walk(list(surfaces))
    return found


# A caller-chosen, identifier-form incumbent revision (F6) and what a guesser might try for it.
INCUMBENT_ID = "incumbent-v1"
INCUMBENT_GUESSES = ("incumbent-v1", "incumbent-v2", "generated-incumbent", "incumbent")


def candidate_reconstructions(comparison, verdict):
    """A FIXED list of what a brute-forcer can build per guessed name pair from public
    knowledge: the raw operating-point digest, a verdict re-sealed around it (form declared or
    not), and plain hashes of the names themselves. Only these forms are tried, so this does
    not generalise to new digest fields; ``key_free_leaves`` (section D) does."""
    import hashlib
    import itertools

    body = {k: v for k, v in verdict.items() if k != "verdict_digest"} if verdict else None
    for size in (1, 2):
        for names in itertools.permutations(CANDIDATE_NAMES, size):
            raw = raw_digest_with(comparison, names)
            yield names, raw
            for name in names:
                yield names, hashlib.sha256(name.encode()).hexdigest()
                yield names, digest(name)
            if body is not None:
                for declared in (True, False):
                    forged = {**body, "operating_point_digest": raw}
                    if not declared:
                        forged.pop("operating_point_digest_form", None)
                    yield names, digest(forged)
    for guess in INCUMBENT_GUESSES:
        yield ("incumbent", guess), hashlib.sha256(guess.encode()).hexdigest()
        yield ("incumbent", guess), digest(guess)
        if body is not None:
            yield ("incumbent", guess), digest({**body, "comparator_digest": guess})


@pytest.mark.parametrize("path", ["improvement", "hold", "hold-identifier-incumbent"])
def test_brute_force_recovers_nothing_from_any_published_digest(tmp_path, path):
    """F5: on both paths, every hex run published in the packet, the event and the outbox rows
    is compared (as a prefix, so truncated ids count) against every candidate reconstruction.
    Nothing matches. Legacy verdicts a stale worker could send (raw-terms digest, form declared
    or not) are offered first, each to its own fresh store, and whatever they publish is
    scanned too. A positive control shows the comparison would catch a real hit."""
    comparison, b = path_bundle(path, tmp_path)
    runs_ = set()
    for i, declared in enumerate((True, False)):
        attempt = cp.PacketOutbox(
            tmp_path / f"legacy{i}.sqlite3",
            slice_keys=KEYS,
            allow_generated_fixture=b.store.allow_generated_fixture,
        )
        legacy = verdict_with(b, comparison, claimed=digest(comparison["terms"]), declared=declared)
        tried = attempt.stage(
            comparison, b.receipt, legacy, authority=b.authority, run_ref=b.ctx.run_ref, now=NOW
        )
        attempt.dispatch(b.sink, b.authority, now=NOW)
        runs_ |= published_hex_runs(tried.packet, attempt.snapshot())
    out = b.stage()
    assert out.status == "staged", out
    b.dispatch()
    runs_ |= published_hex_runs(out.packet, list(b.sink.effects.values()), b.store.snapshot())
    assert out.packet["audit"]["independent_verdict_digest"] in runs_
    assert out.packet["comparison"]["operating_point_digest"] in runs_
    assert out.packet["packet_digest"][:32] in runs_  # the truncated event id is collected
    hits = [
        (names, run)
        for names, guess in candidate_reconstructions(comparison, b.verdict)
        for run in runs_
        if guess.startswith(run)
    ]
    # The one disclosure by contract (F6, module docstring): ``binding.fallback_digest`` is the
    # worker's fallback content digest, which the remedy binding sets equal to a 64-hex incumbent
    # revision. It is the only allowed hit, it must sit in the binding, and nowhere else.
    contract = [h for h in hits if h[1] == b.ctx.fallback_digest]
    assert [h for h in hits if h not in contract] == []
    # The fixtures' 64-hex incumbent is digest("incumbent"), so the scan must see the contract
    # hit there (proving it reads the binding); the identifier-form incumbent has none.
    assert bool(contract) is (path != "hold-identifier-incumbent"), contract
    if contract:
        assert out.packet["binding"]["fallback_digest"] == b.ctx.fallback_digest
        assert out.packet["arms"]["incumbent"]["revision"] != b.ctx.fallback_digest
    # Plain-text scan for the incumbent revision guesses (``incumbent`` itself is an arm name).
    texts = set(every_text([out.packet, list(b.sink.effects.values()), b.store.snapshot()]))
    for text in [t for t in texts if t[:1] in "{["]:
        try:
            texts |= set(every_text(json.loads(text)))
        except ValueError:
            pass
    assert [g for g in INCUMBENT_GUESSES if g != "incumbent" and g in texts] == []
    # Positive control: plant one raw-terms digest into a copy and the scan finds it.
    planted = {**out.packet, "extra": {"nested": [raw_digest_with(comparison, SLICE_NAMES)]}}
    assert any(
        names == SLICE_NAMES
        for names, guess in candidate_reconstructions(comparison, None)
        if any(guess.startswith(run) for run in published_hex_runs(planted))
    )


def test_incumbent_revision_is_published_as_a_keyed_digest(tmp_path):
    """F6: the identifier-form incumbent revision is keyed exactly like the slice names, and the
    keyed digest is domain-separated from a slice name with the same text."""
    comparison = named_hold(tmp_path, incumbent=INCUMBENT_ID)
    b = live_bundle(comparison, tmp_path / "b")
    b.ctx = context(comparison, fallback_digest=digest("generated-fallback"))
    b.authority = Authority(b.ctx)
    out = b.stage()
    assert out.status == "staged", out
    key = key_for(comparison)
    expected = cp.incumbent_revision_digest(INCUMBENT_ID, key, **scope(comparison))
    assert out.packet["arms"]["incumbent"]["revision"] == expected
    assert expected != cp.slice_digest(INCUMBENT_ID, key, **scope(comparison))
    other = cp.incumbent_revision_digest(INCUMBENT_ID, KAT_KEY, **scope(comparison))
    assert other != expected  # keyed: another key gives another digest
    assert out.packet["arms"]["simple"]["revision"] == comparison["arms"]["simple"]["revision"]


def test_verdict_with_a_free_text_comparator_is_refused(tmp_path):
    """F6: a verdict whose comparator (or candidate) is not a content digest is refused, so an
    identifier-form incumbent cannot be confirmed through the published verdict digest."""
    comparison = named_hold(tmp_path, incumbent=INCUMBENT_ID)
    b = live_bundle(comparison, tmp_path / "b")
    b.ctx = context(comparison, fallback_digest=digest("generated-fallback"))
    b.authority = Authority(b.ctx)
    b.receipt = make_receipt()
    for field in ("comparator_digest", "candidate_digest"):
        verdict = make_verdict(comparison, b.receipt, b.ctx, **{field: INCUMBENT_ID})
        out = b.stage(verdict=verdict)
        assert out.reason == "independent_verdict_unverified" and empty(b.store), (field, out)
    good = make_verdict(comparison, b.receipt, b.ctx, comparator_digest=digest("generated-cmp"))
    assert b.stage(verdict=good).status == "staged"


# ------------- D: differential leak tests (cross-session reviews of 5f7d830 and 1ff8bc8)
#
# The brute force above only tries fixed reconstruction forms. These tests need no forms. They
# build the same packet under several engagement keys and two inputs (slice names, or the
# incumbent revision) and inspect every published leaf: packet, event, outbox rows, and JSON
# stored in those rows. Two criteria, each a key-free function of the varied input:
#
# * Whole leaf (D1, D2). Every keyed digest of a varied input is first replaced by a role token
#   (``normalised``), re-derived here independently of the module. A leaf then EQUAL under two
#   keys for one input yet CHANGED when only the input changes is flagged. The tokens expose
#   structure around keyed digests (an order driven by raw names, an unkeyed suffix on a keyed
#   digest). Values under digest-shaped dict keys are compared as multisets, so an order that
#   follows a keyed sort neither hides nor invents a leak.
# * Fragment (D5). A 6-character fragment of an alphanumeric word that is the same under three
#   keys for one input and absent under every key for the other input is flagged. It catches an
#   unkeyed piece glued onto a keyed value of any form, without the token step.
#
# What this does NOT prove. A hash over a keyed input AND an unkeyed one is caught only by a
# fixture that varies the unkeyed input alone (hence the material-only fixture). A leak spread
# over fragments shorter than six characters, or split by punctuation into such fragments, can
# escape D5. Only these fixtures are covered. A future field is flagged only if it falls under
# one of the two criteria, not whatever form it takes.

# F4 (module docstring): plain SHA-256 digests over the whole comparison. They do cover the raw
# names and the raw incumbent revision, so the differential flags them. They are not a
# guess-confirmation oracle, because they also cover inputs the packet never publishes: the
# intake revision (a digest over every intake record), the independent reference digest, the
# stage timestamps and the per-case reports. ``test_f4_leaves_also_change_with_unpublished_
# entropy_inputs`` shows that. EXACTLY these two leaf paths, nothing else, may be flagged.
F4_UNGUESSABLE = frozenset(
    {
        "packet/comparison/evidence_revision",
        "packet/evidence_pointers/0/digest",  # the same value, as the protected_comparison pointer
    }
)
# LOW-1 (module docstring), NOT part of the F4 allowlist: on an improvement packet the remedy
# binding requires ``binding.fallback_digest`` to equal the incumbent revision, so it is the
# plain revision. It is flagged by the incumbent variation on that path only, by contract.
LOW1_FALLBACK = frozenset({"packet/binding/fallback_digest"})


class SaltedKeyring:
    """TEST DOUBLE ONLY: different keys (salts) for the SAME tenant and engagement. Derived
    from public inputs, which a real ``SliceKeyView`` must never do (OPE-566)."""

    def __init__(self, salt):
        self.salt = salt

    def slice_key(self, tenant, engagement):
        return bytes.fromhex(digest(["generated-slice-key", self.salt, tenant, engagement]))


K1, K2, K3 = SaltedKeyring("k1"), SaltedKeyring("k2"), SaltedKeyring("k3")
OTHER_NAMES = ("john_smith", "vip")
# Unequal length to the base incumbent ("incumbent-v1"), so a length-only leak is visible.
OTHER_INCUMBENT_ID = "incumbent-version-2"


def renamed(value, mapping):
    """Replace every string equal to a key of ``mapping``, dict keys included, recursively."""
    if isinstance(value, dict):
        return {mapping.get(k, k): renamed(v, mapping) for k, v in value.items()}
    if isinstance(value, list):
        return [renamed(v, mapping) for v in value]
    if isinstance(value, str):
        return mapping.get(value, value)
    return value


def varied(comparison, mapping):
    """The same comparison with only the mapped strings changed, resealed."""
    changed = seal(renamed(comparison, mapping))
    for old, new in mapping.items():
        assert mentions(changed, old) == [] and mentions(changed, new), (old, new)
    return changed


def role_tokens(comparison, key):
    """Each keyed digest this module derives from a varied input, mapped to its role.

    Re-derived here with ``hmac`` (the derivation the KAT test pins), never with the module's
    own functions, so a mutated module cannot normalise its own leak away. A slice name's role
    is its position in the terms, which ``varied`` keeps, so a name and its replacement share a
    token. The incumbent revision has one role."""
    import hmac

    label = canonical([cp.SLICE_DIGEST, comparison["organization"], comparison["engagement"]])
    scoped = hmac.digest(key, label.encode(), "sha256")
    terms = comparison["terms"]
    tokens = {
        hmac.digest(scoped, name.encode(), "sha256").hex(): f"<{kind}:{i}>"
        for kind in ("critical_slices", "material_slices")
        for i, name in enumerate(terms.get(kind, []))
    }
    revision = canonical(["incumbent_revision", comparison["arms"]["incumbent"]["revision"]])
    tokens[hmac.digest(scoped, revision.encode(), "sha256").hex()] = "<incumbent>"
    return tokens


def normalised(value, tokens):
    """``value`` with every occurrence of a keyed role digest replaced by its token."""

    def text(s):
        for hexed, token in tokens.items():
            s = s.replace(hexed, token)
        return s

    if isinstance(value, dict):
        return {
            text(k) if isinstance(k, str) else k: normalised(v, tokens) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [normalised(v, tokens) for v in value]
    return text(value) if isinstance(value, str) else value


def published_leaves(*surfaces):
    """Every published leaf, by path. Dict keys are leaves too (``<path>@key``). A key that is a
    hex run is addressed as ``#``: every entry of such a dict shares one path, and its values
    are compared as a multiset (``same``), so a keyed sort order cannot hide a leaf or move it.
    JSON text stored in outbox rows is parsed and walked again. A packet or event document,
    wherever it appears, is rooted at ``packet`` or ``event``, so the copy stored in a row and
    the one returned share paths."""
    leaves = {}

    def walk(value, path):
        if isinstance(value, dict):
            schema = value.get("schema_version")
            path = {cp.PACKET: "packet", cp.EVENT: "event"}.get(schema, path)
            for k, v in value.items():
                step = "#" if HEX_RUN.fullmatch(str(k)) else str(k)
                leaves.setdefault(f"{path}/{step}@key", []).append(k)
                walk(v, f"{path}/{step}")
        elif isinstance(value, (list, tuple)):
            for i, v in enumerate(value):
                walk(v, f"{path}/{i}")
        else:
            leaves.setdefault(path, []).append(value)
            if isinstance(value, str) and value[:1] in "{[":
                try:
                    walk(json.loads(value), path + "/json")
                except ValueError:
                    pass

    walk({"returned": surfaces[0], "events": surfaces[1], "store": surfaces[2]}, "")
    return leaves


def same(x, y):
    """Leaf values equal as multisets (``None`` when the path is absent)."""
    return (x is None) == (y is None) and sorted(map(repr, x or ())) == sorted(map(repr, y or ()))


class Build:
    """One staged and dispatched packet under one keyring: its raw and normalised leaves."""

    def __init__(self, path, comparison, keyring, root):
        receipt = make_receipt()
        over, verdict_over = {}, {}
        if path == "hold-identifier-incumbent":
            over = {"fallback_digest": digest("generated-fallback")}
            verdict_over = {"comparator_digest": digest("generated-cmp")}
        ctx = context(comparison, **over)
        key = keyring.slice_key(comparison["organization"], comparison["engagement"])
        verdict = make_verdict(comparison, receipt, ctx, slice_key=key, **verdict_over)
        root.mkdir(parents=True)
        store = cp.PacketOutbox(
            root / "outbox.sqlite3",
            slice_keys=keyring,
            allow_generated_fixture=not path.startswith("hold"),
        )
        authority, sink = Authority(ctx), Sink()
        out = store.stage(
            comparison, receipt, verdict, authority=authority, run_ref=ctx.run_ref, now=NOW
        )
        assert out.status == "staged", out
        assert store.dispatch(sink, authority, now=NOW) == [(out.event_id, "delivered")]
        surfaces = (out.packet, list(sink.effects.values()), store.snapshot())
        self.raw = published_leaves(*surfaces)
        self.leaves = published_leaves(*normalised(surfaces, role_tokens(comparison, key)))


def differential_build(path, comparison, keyring, root):
    """Stage and dispatch one packet under ``keyring``; return every published leaf."""
    return Build(path, comparison, keyring, root).raw


def key_free_leaves(path, base, variant, root):
    """Normalised leaf paths equal under K1 and K2 for one input, yet different when only the
    input varies: (a) K1+base, (b) K2+base, (c) K1+variant, (d) K2+variant."""
    a = Build(path, base, K1, root / "a").leaves
    b = Build(path, base, K2, root / "b").leaves
    c = Build(path, variant, K1, root / "c").leaves
    d = Build(path, variant, K2, root / "d").leaves
    assert Build(path, base, K1, root / "a2").leaves == a  # deterministic: no noise
    assert a != b and c != d  # the key reaches the published surfaces
    return {
        p
        for p in set(a) | set(b) | set(c) | set(d)
        if (same(a.get(p), b.get(p)) and not same(a.get(p), c.get(p)))
        or (same(c.get(p), d.get(p)) and not same(c.get(p), a.get(p)))
    }


WORD = re.compile(r"[A-Za-z0-9]+")
FRAGMENT = 6


def fragments(values, allowed):
    """Every 6-character window of every alphanumeric word in the string leaves, except words
    in ``allowed``. Splitting at punctuation keeps a keyed digest's random characters from
    joining fixed text into chance fragments."""
    found = set()
    for value in values or ():
        if isinstance(value, str):
            for word in WORD.findall(value):
                if word not in allowed:
                    found.update(word[i : i + FRAGMENT] for i in range(len(word) - FRAGMENT + 1))
    return found


def key_free_fragments(path, base, variant, root, allowed=frozenset()):
    """Raw leaf paths with a fragment shared by K1, K2 and K3 for one input and absent under
    all three keys for the other input, with the fragments found. No token step: this works
    on what is published, whatever form a keyed value takes."""
    builds = {
        (side, k.salt): Build(path, comparison, k, root / f"{side}-{k.salt}").raw
        for side, comparison in (("base", base), ("variant", variant))
        for k in (K1, K2, K3)
    }
    sides = {s: [v for (side, _), v in builds.items() if side == s] for s in ("base", "variant")}
    flagged = {}
    for this, other in (("base", "variant"), ("variant", "base")):
        for p in set().union(*sides[this]):
            shared = set.intersection(
                *(fragments(leaves.get(p), allowed) for leaves in sides[this])
            )
            hits = shared - set().union(
                *(fragments(leaves.get(p), allowed) for leaves in sides[other])
            )
            if hits:
                flagged.setdefault(p, set()).update(hits)
    return flagged


def named_material_only_run(root):
    """A real run whose terms name material slices only: no slice name reaches any keyed
    published field except through the operating-point digest."""
    dev, protected = rows("dev"), rows("protected")
    for r in dev + protected:
        r["slices"] = list(SLICE_NAMES)
    comparison = compare(
        root,
        development=dev,
        protected=protected,
        terms={**STRICT, "material_slices": list(SLICE_NAMES)},
        allowed_slices=list(SLICE_NAMES),
    )[0]
    assert comparison["decision"]["disposition"] == "recommend", comparison["decision"]
    assert all(arm["critical_slices"] == {} for arm in comparison["arms"].values())
    return comparison


NAME_RUNS = {
    "improvement": named_slice_run,
    "hold": named_hold,
    "material": named_material_run,  # one critical and one material name
    "material-only": named_material_only_run,
}


def names_variation(path, root):
    comparison = NAME_RUNS[path](root)
    return comparison, varied(comparison, dict(zip(SLICE_NAMES, OTHER_NAMES, strict=True)))


def incumbent_variation(path, root):
    if path == "improvement":  # a 64-hex incumbent; the fallback digest follows it by contract
        comparison = named_slice_run(root)
        return comparison, varied(comparison, {digest("incumbent"): digest("incumbent-2")})
    comparison = named_hold(root, incumbent=INCUMBENT_ID)
    return comparison, varied(comparison, {INCUMBENT_ID: OTHER_INCUMBENT_ID})


def build_path(path):
    """The staging path a variation fixture runs on (material runs are improvements)."""
    return "improvement" if path.startswith("material") else path


@pytest.mark.parametrize("path", sorted(NAME_RUNS))
def test_no_published_leaf_is_a_key_free_function_of_the_slice_names(tmp_path, path):
    """D1: varying the slice names alone (critical, mixed, or material only) flags exactly the
    two F4 leaves, nothing else, after keyed digests are normalised to role tokens."""
    base, variant = names_variation(path, tmp_path)
    assert key_free_leaves(build_path(path), base, variant, tmp_path / "d") == F4_UNGUESSABLE


@pytest.mark.parametrize(
    "path, expected",
    [
        ("hold-identifier-incumbent", F4_UNGUESSABLE),
        ("improvement", F4_UNGUESSABLE | LOW1_FALLBACK),
    ],
)
def test_no_published_leaf_is_a_key_free_function_of_the_incumbent(tmp_path, path, expected):
    """D2: varying the incumbent revision alone flags the F4 leaves and, on an improvement
    packet only, the contract fallback digest (LOW-1). The keyed arm revision is not flagged.
    The identifier-form variants differ in length, so a published length would be flagged
    (the 64-hex form cannot differ in length)."""
    base, variant = incumbent_variation(path, tmp_path)
    old, new = (c["arms"]["incumbent"]["revision"] for c in (base, variant))
    assert (len(old) != len(new)) is (path != "improvement")
    assert key_free_leaves(path, base, variant, tmp_path / "d") == expected


def test_differential_flags_planted_key_free_digests(tmp_path, monkeypatch):
    """D3 positive control: an unkeyed slice digest and an unkeyed incumbent digest are both
    flagged, at the fields that carry them."""
    import hashlib

    for sub in ("n", "i"):
        (tmp_path / sub).mkdir(mode=0o700)
    names_base, names_variant = names_variation("improvement", tmp_path / "n")
    inc_base, inc_variant = incumbent_variation("hold-identifier-incumbent", tmp_path / "i")
    real_mac = cp._mac

    def unkeyed_slices(name, scoped):  # the incumbent (a canonical list) stays keyed
        if name.startswith("["):
            return real_mac(name, scoped)
        return hashlib.sha256(name.encode()).hexdigest()

    with monkeypatch.context() as patch:
        patch.setattr(cp, "_mac", unkeyed_slices)
        flagged = key_free_leaves("improvement", names_base, names_variant, tmp_path / "dn")
    assert {
        "packet/arms/simple/critical_slices/#@key",
        "packet/comparison/operating_point_digest",
    } <= flagged, flagged
    with monkeypatch.context() as patch:
        patch.setattr(cp, "_revision_mac", lambda revision, scoped: digest(revision))
        flagged = key_free_leaves(
            "hold-identifier-incumbent", inc_base, inc_variant, tmp_path / "di"
        )
    assert "packet/arms/incumbent/revision" in flagged, flagged


def _sha8(text):
    import hashlib

    return hashlib.sha256(text.encode()).hexdigest()[:8]


def test_differential_flags_structure_around_keyed_digests(tmp_path, monkeypatch):
    """D3 positive controls for the LOW-A gaps (cross-session review of 1ff8bc8): leaks that
    keep the key in the leaf, so no whole leaf is key-free, are still flagged after the token
    step. (1) keyed digests listed in the order of the raw names; (2) a keyed digest with an
    unkeyed name-hash suffix; (3) an unkeyed name hash inside the status under a keyed-sorted
    dict; (4) the incumbent revision's length."""
    real_aggregate, real_mac = cp._arm_aggregate, cp._mac

    def ordered(name, arm, slice_key):
        out = real_aggregate(name, arm, slice_key)
        return {**out, "x_order": [real_mac(n, slice_key) for n in sorted(arm["critical_slices"])]}

    def suffixed(name, scoped):
        return real_mac(name, scoped) + ("" if name.startswith("[") else _sha8(name))

    def status_hash(name, arm, slice_key):
        out = real_aggregate(name, arm, slice_key)
        slices = {
            real_mac(k, slice_key): v["status"] + _sha8(k)
            for k, v in arm["critical_slices"].items()
        }
        return {**out, "critical_slices": dict(sorted(slices.items()))}

    def with_length(name, arm, slice_key):
        out = real_aggregate(name, arm, slice_key)
        return {**out, "x_length": len(arm["revision"])} if name == "incumbent" else out

    slices = "packet/arms/simple/critical_slices/"
    cases = [
        ("names", "_arm_aggregate", ordered, lambda p: p == "packet/arms/simple/x_order/0"),
        # The token step leaves "<critical_slices:0>" + the unkeyed suffix as the dict key.
        ("names", "_mac", suffixed, lambda p: p.startswith(slices + "<critical_slices:0>")),
        ("names", "_arm_aggregate", status_hash, lambda p: p == slices + "<critical_slices:0>"),
        ("incumbent", "_arm_aggregate", with_length, lambda p: p.endswith("incumbent/x_length")),
    ]
    for i, (kind, attr, fake, expected) in enumerate(cases):
        root = tmp_path / str(i)
        root.mkdir(mode=0o700)
        if kind == "names":
            path, (base, variant) = "improvement", names_variation("improvement", root)
        else:
            path = "hold-identifier-incumbent"
            base, variant = incumbent_variation(path, root)
        with monkeypatch.context() as patch:
            patch.setattr(cp, attr, fake)
            flagged = key_free_leaves(path, base, variant, root / "d")
        assert any(expected(p) for p in flagged), (i, attr, sorted(flagged))


def test_f4_leaves_also_change_with_unpublished_entropy_inputs(tmp_path):
    """D4: each allowlisted F4 leaf changes when ONLY an unpublished high-entropy input changes,
    under the same key and the same names, and that input appears nowhere in what is published.
    So a guesser holding the right names still cannot rebuild these leaves."""
    base = named_slice_run(tmp_path)
    reference = differential_build("improvement", base, K1, tmp_path / "base")
    every = set(every_text(reference))
    for field in ("intake_revision", "independent_reference_digest"):
        # A 64-hex digest over content the packet does not carry, and itself never published.
        assert re.fullmatch("[0-9a-f]{64}", base[field]), field
        assert not any(base[field][:16] in t for t in every), field
        variant = seal({**base, field: digest(["generated-other", field])})
        leaves = differential_build("improvement", variant, K1, tmp_path / field)
        for leaf in F4_UNGUESSABLE:
            assert leaves[leaf] != reference[leaf], (field, leaf)


@pytest.mark.parametrize("path", ["improvement", "hold"])
def test_evidence_pointer_is_the_evidence_revision(tmp_path, path):
    """INFO-D: the two F4 leaves are one value. The ``protected_comparison`` pointer is
    ``comparison.evidence_revision`` itself, which is the comparison's own seal."""
    comparison, b = path_bundle(path, tmp_path)
    out = b.stage()
    assert out.status == "staged", out
    pointer = out.packet["evidence_pointers"][0]
    assert pointer == {"kind": "protected_comparison", "digest": comparison["evidence_revision"]}
    assert out.packet["comparison"]["evidence_revision"] == pointer["digest"]


def _f4_values(*comparisons):
    return frozenset(c["evidence_revision"] for c in comparisons)


@pytest.mark.parametrize(
    "variation, path",
    [
        ("names", "improvement"),
        ("names", "hold"),
        ("names", "material-only"),
        ("incumbent", "hold-identifier-incumbent"),
        ("incumbent", "improvement"),
    ],
)
def test_no_published_fragment_is_a_key_free_function_of_the_input(tmp_path, variation, path):
    """D5 (LOW-A): no 6-character fragment of a published word is shared by three keys yet
    missing under the other input. The only exceptions are the F4 value itself and, for the
    incumbent on an improvement packet, the contract fallback digest (LOW-1), both excluded as
    whole words. Without that exclusion the same scan flags the F4 leaves, which shows it reads
    them (wiring control), and every fragment it flags then is a window of an excluded value
    (here the F4 leaves, the stored packet row that carries them, and the fallback digest)."""
    if variation == "names":
        base, variant = names_variation(path, tmp_path)
    else:
        base, variant = incumbent_variation(path, tmp_path)
    allowed = _f4_values(base, variant)
    if variation == "incumbent" and path == "improvement":
        allowed |= {c["arms"]["incumbent"]["revision"] for c in (base, variant)}
    run = build_path(path)
    assert key_free_fragments(run, base, variant, tmp_path / "f", allowed) == {}
    unfiltered = key_free_fragments(run, base, variant, tmp_path / "u")
    assert F4_UNGUESSABLE <= set(unfiltered), sorted(unfiltered)
    windows = fragments(sorted(allowed), frozenset())
    assert set().union(*unfiltered.values()) <= windows, sorted(unfiltered)


def test_fragment_scan_flags_unkeyed_pieces_glued_to_keyed_digests(tmp_path, monkeypatch):
    """D5 positive control: an unkeyed name hash glued to a keyed slice digest, and an unkeyed
    incumbent hash glued to the keyed arm revision, are flagged at the fields that carry them,
    with the fragment that leaks."""
    real_mac, real_revision_mac = cp._mac, cp._revision_mac
    (tmp_path / "n").mkdir(mode=0o700)
    (tmp_path / "i").mkdir(mode=0o700)
    names_base, names_variant = names_variation("improvement", tmp_path / "n")
    inc_base, inc_variant = incumbent_variation("hold-identifier-incumbent", tmp_path / "i")
    with monkeypatch.context() as patch:
        patch.setattr(
            cp,
            "_mac",
            lambda n, s: real_mac(n, s) + ("" if n.startswith("[") else _sha8(n)),
        )
        flagged = key_free_fragments(
            "improvement", names_base, names_variant, tmp_path / "fn", _f4_values(names_base)
        )
    assert _sha8(SLICE_NAMES[0])[:FRAGMENT] in flagged["packet/arms/simple/critical_slices/#@key"]
    with monkeypatch.context() as patch:
        patch.setattr(cp, "_revision_mac", lambda r, s: real_revision_mac(r, s) + _sha8(r))
        flagged = key_free_fragments(
            "hold-identifier-incumbent", inc_base, inc_variant, tmp_path / "fi", _f4_values()
        )
    assert _sha8(INCUMBENT_ID)[:FRAGMENT] in flagged["packet/arms/incumbent/revision"]


# --------------------------- LOW-1: the incumbent is linkable through ``fallback_digest``


def test_improvement_packet_says_the_incumbent_is_linkable_via_the_fallback_digest(tmp_path):
    """LOW-1: two engagements with the same incumbent publish the same fallback digest (the
    plain revision) on an improvement packet, and each packet says so. The keyed arm revision
    still differs between them."""
    first = named_slice_run(tmp_path)
    second = in_another_engagement(first)
    outs = [Bundle(c, tmp_path / str(i)).stage() for i, c in enumerate((first, second))]
    assert [o.status for o in outs] == ["staged", "staged"], outs
    incumbent = first["arms"]["incumbent"]["revision"]
    for out in outs:
        assert out.packet["binding"]["fallback_digest"] == incumbent
        assert out.packet["critical_slice_names"]["incumbent_revision"] == {
            "arm_revision": "keyed_digest",
            "linkable_across_engagements_via_fallback_digest": True,
        }
    arm_revisions = {o.packet["arms"]["incumbent"]["revision"] for o in outs}
    assert len(arm_revisions) == 2 and incumbent not in arm_revisions


def test_hold_with_a_distinct_fallback_says_the_incumbent_is_not_linked_by_it(tmp_path):
    """LOW-1: a hold whose fallback digest is not the incumbent revision reports ``False``."""
    comparison, b = path_bundle("hold-identifier-incumbent", tmp_path)
    out = b.stage()
    assert out.status == "staged", out
    linked = out.packet["critical_slice_names"]["incumbent_revision"]
    assert linked["linkable_across_engagements_via_fallback_digest"] is False
    assert out.packet["binding"]["fallback_digest"] != comparison["arms"]["incumbent"]["revision"]


# ------- LOW-2 (narrowed, LOW-B): ``previous_deployed_version`` is the deployed revision. It may
# equal the incumbent revision, and is refused only when the fallback digest does not already
# publish that revision.

_PDV_REFUSAL = "previous_deployed_version_equals_incumbent_revision"


class _AlwaysUnequal(str):
    """A ``str`` subclass that would slip past an ``==`` check."""

    def __eq__(self, other):
        return False

    __hash__ = str.__hash__


_INCUMBENT = object()  # "use the comparison's own raw incumbent revision"
# Paths whose fallback digest is NOT the incumbent revision (where the refusal can fire), and
# paths whose fallback digest IS the incumbent revision (already published).
UNPUBLISHED = ["hold", "hold-identifier-incumbent"]
PUBLISHED = ["improvement", "hold-fallback-is-incumbent"]


def _pdv_bundle(path, tmp_path, previous, **over):
    comparison, b = path_bundle("hold" if path == "hold-fallback-is-incumbent" else path, tmp_path)
    incumbent = comparison["arms"]["incumbent"]["revision"]
    if previous is _INCUMBENT:
        previous = incumbent
    seen = []

    class Spy:
        def slice_key(self, tenant, engagement):
            seen.append(engagement)
            return KEYS.slice_key(tenant, engagement)

    if path in UNPUBLISHED:
        over.setdefault("fallback_digest", digest("generated-fallback"))
    b.ctx = context(comparison, previous_deployed_version=previous, **over)
    assert (b.ctx.fallback_digest == incumbent) is (path in PUBLISHED), path
    b.authority = Authority(b.ctx)
    b.store = cp.PacketOutbox(
        tmp_path / "spy.sqlite3",
        slice_keys=Spy(),
        allow_generated_fixture=b.store.allow_generated_fixture,
    )
    return comparison, b, seen


def _replay_kw(comparison, path):
    return {"slice_key": key_for(comparison), "allow_generated_fixture": path == "improvement"}


@pytest.mark.parametrize("path", UNPUBLISHED)
def test_previous_deployed_version_equal_to_an_unpublished_incumbent_is_refused(tmp_path, path):
    """LOW-2 adverse (the one remaining refusal): on a hold whose fallback digest is NOT the
    incumbent, a previous version equal to the raw incumbent revision, 64-hex ("hold") or
    identifier form ("incumbent-v1", the reviewer's probe value), refuses with its own code,
    before any key lookup, on stage, assemble and replay, with nothing stored and the value not
    echoed."""
    comparison, b, seen = _pdv_bundle(path, tmp_path, _INCUMBENT)
    incumbent = comparison["arms"]["incumbent"]["revision"]
    assert b.ctx.previous_deployed_version == incumbent != b.ctx.fallback_digest
    assert (incumbent == INCUMBENT_ID) is (path == "hold-identifier-incumbent")
    out = b.stage()
    assert out.status == "refused" and out.reason == _PDV_REFUSAL, out
    assert seen == [] and empty(b.store) and b.dispatch() == []
    assert not _leaks(out, incumbent)
    kw = _replay_kw(comparison, path)
    assert cp.assemble_packet(comparison, b.receipt, b.verdict, b.ctx, **kw) == (None, _PDV_REFUSAL)
    assert not cp.verify_packet({}, comparison, b.receipt, b.verdict, b.ctx, **kw)


@pytest.mark.parametrize("path", PUBLISHED)
def test_previous_deployed_version_equal_to_an_already_published_incumbent_stages(tmp_path, path):
    """LOW-2 positive (LOW-B): the deployed revision normally IS the incumbent. When the
    fallback digest already publishes it (always on an improvement packet, and on a hold using
    the incumbent as fallback), it is accepted on stage, assemble and replay, and published
    as-is."""
    comparison, b, seen = _pdv_bundle(path, tmp_path, _INCUMBENT)
    incumbent = comparison["arms"]["incumbent"]["revision"]
    out = b.stage()
    assert out.status == "staged", out
    binding = out.packet["binding"]
    assert binding["previous_deployed_version"] == binding["fallback_digest"] == incumbent
    assert len(seen) == 1 and b.dispatch() == [(out.event_id, "delivered")]
    kw = _replay_kw(comparison, path)
    assert cp.assemble_packet(comparison, b.receipt, b.verdict, b.ctx, **kw) == (out.packet, None)
    assert cp.verify_packet(out.packet, comparison, b.receipt, b.verdict, b.ctx, **kw)


@pytest.mark.parametrize("path", PUBLISHED)
def test_rollback_restoring_the_incumbent_is_accepted(tmp_path, path):
    """LOW-2 positive (LOW-B): successive proposals of one run, in one store, while the
    deployment moves from the incumbent to another revision and is rolled back to the
    incumbent. Every proposal stages and replays, and the rollback proposal publishes the
    incumbent as its previous version."""
    comparison, b, _seen = _pdv_bundle(path, tmp_path, _INCUMBENT)
    incumbent = comparison["arms"]["incumbent"]["revision"]
    deployed = [
        ("generated-proposal-v1", incumbent),
        ("generated-proposal-v2", digest("generated-other-deployment")),
        ("generated-proposal-v3", incumbent),  # rolled back to the incumbent
    ]
    staged = []
    for version, previous in deployed:
        b.ctx = context(comparison, proposal_version=version, previous_deployed_version=previous)
        b.authority = Authority(b.ctx)
        out = b.stage()
        assert out.status == "staged", (version, out)
        assert out.packet["binding"]["previous_deployed_version"] == previous
        kw = _replay_kw(comparison, path)
        assert cp.verify_packet(out.packet, comparison, b.receipt, b.verdict, b.ctx, **kw)
        staged.append(out)
    assert len({o.event_id for o in staged}) == 3
    assert all(
        b.store.packet_status("pkt-" + o.packet["packet_digest"][:32]) == "current" for o in staged
    )
    assert len(b.dispatch()) == 3


@pytest.mark.parametrize("path", PUBLISHED + UNPUBLISHED)
@pytest.mark.parametrize("previous", [None, "generated-deployed-v0"])
def test_previous_deployed_version_distinct_from_the_incumbent_still_stages(
    tmp_path, path, previous
):
    """LOW-2 positive: an absent or distinct previous version stages and is published as-is."""
    _, b, seen = _pdv_bundle(path, tmp_path, previous)
    out = b.stage()
    assert out.status == "staged", out
    assert out.packet["binding"]["previous_deployed_version"] == previous and len(seen) == 1


DERIVED = {
    "upper": str.upper,
    "prefix-8": lambda r: r[:8],
    "prefix": lambda r: r[:-1],
    "suffix": lambda r: r[1:],
}


@pytest.mark.parametrize("form", sorted(DERIVED))
@pytest.mark.parametrize("path", UNPUBLISHED)
def test_previous_deployed_version_derived_from_the_incumbent_is_accepted(tmp_path, path, form):
    """NIT-E: only exact equality is refused, as the module docstring says. Another case, a
    prefix or a suffix of the raw incumbent revision stages, even where the refusal is active,
    and is published as-is."""
    comparison, b, seen = _pdv_bundle(path, tmp_path, None)
    incumbent = comparison["arms"]["incumbent"]["revision"]
    previous = DERIVED[form](incumbent)
    assert previous != incumbent
    b.ctx = context(
        comparison, previous_deployed_version=previous, fallback_digest=b.ctx.fallback_digest
    )
    b.authority = Authority(b.ctx)
    out = b.stage()
    assert out.status == "staged", (form, out)
    assert out.packet["binding"]["previous_deployed_version"] == previous and len(seen) == 1


def test_previous_deployed_version_must_be_an_exact_str(tmp_path):
    """LOW-2 adverse: a ``str`` subclass that answers ``==`` with False, carrying the
    incumbent's text, and a non-string are invalid contexts (no raise, no key lookup)."""
    for previous in (_AlwaysUnequal(INCUMBENT_ID), 5, b"incumbent-v1"):
        root = tmp_path / type(previous).__name__
        root.mkdir(mode=0o700)
        _, b, seen = _pdv_bundle("hold-identifier-incumbent", root, previous)
        out = b.stage()
        assert out.reason == "invalid_packet_context" and seen == [] and empty(b.store), out


def test_incumbent_revision_must_be_an_exact_str(tmp_path):
    """LOW-2 adverse: an incumbent revision that is a ``str`` subclass (which could defeat the
    equality check) makes the comparison unverified, before any key lookup."""
    comparison, b, seen = _pdv_bundle("hold-identifier-incumbent", tmp_path, INCUMBENT_ID)
    sneaky = copy.deepcopy(comparison)
    sneaky["arms"]["incumbent"]["revision"] = _AlwaysUnequal(INCUMBENT_ID)
    sneaky = seal(sneaky)
    out = b.stage(comparison=sneaky)
    assert out.reason == "comparison_unverified" and seen == [] and empty(b.store), out
    assert b.stage().reason == _PDV_REFUSAL  # the honest comparison is caught by the check
