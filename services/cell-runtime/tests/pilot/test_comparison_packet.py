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


def enroll(store, development=None, protected=None):
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
        "allowed_slices": ["critical", "rare"],
        "error_class": {"from_label": "b", "to_label": "a"},
        "split": {"method": "time", "after": 90},
        "excluded_counts": {"late_feature": 1},
        "development": dev,
        "protected": test,
    }
    return data | {"intake_revision": digest(data)}


def compare(
    root, *, protected=None, development=None, terms=None, mapping=None, model=None, measured=None
):
    store = LocalTrainingStore(root / "training.sqlite3", "generated")
    data = enroll(store, development=development, protected=protected)
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
        incumbent_revision=digest("incumbent"),
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


def make_verdict(comparison, receipt, ctx, **over):
    arms = comparison["arms"]
    body = {
        "schema_version": cp.VERDICT,
        "verdict": "reproduced",
        "producer_id": ctx.worker_id,
        "verifier_id": "generated-independent-verifier",
        "audit_receipt_digest": receipt["receipt_digest"],
        "comparison_evidence_revision": comparison["evidence_revision"],
        "candidate_digest": arms["simple"]["revision"],
        "comparator_digest": arms["incumbent"]["revision"],
        "operating_point_digest": digest(comparison["terms"]),
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
        self.store = cp.PacketOutbox(tmp_path / "outbox.sqlite3")
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
    observed = make_receipt(evidence_origin="observed")
    out = b.stage(receipt=observed, verdict=make_verdict(b.comparison, observed, b.ctx))
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
    restarted = cp.PacketOutbox(b.store.path)
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
    }
    script = (
        "import json,os,sys\n"
        "from opentour_cell.pilot import comparison_packet as cp\n"
        "q=json.load(sys.stdin)\n"
        "class A:\n"
        "    def current(self, r): return cp.PacketContext(**q['ctx'])\n"
        "s=cp.PacketOutbox(q['path'])\n"
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
    b.store = cp.PacketOutbox(b.store.path)
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


def test_forged_self_consistent_receipt_is_marked_unauthenticated(runs, tmp_path):
    """V3: a self-consistent forged receipt and verdict pass, but are never presented as proven."""
    b = Bundle(runs["winner"], tmp_path)
    forged = make_receipt(evidence_origin="observed", population_kind="real_sample")
    out = b.stage(receipt=forged, verdict=make_verdict(b.comparison, forged, b.ctx))
    assert out.status == "staged"
    assert out.packet["audit"]["issuer_authenticated"] is False
    assert out.packet["audit"]["integrity"] == "digest_self_consistent_only"
    assert out.packet["claim"]["accuracy_claim_eligible"] is False
    assert out.packet["authority"] == {"release": False, "delivery": False}


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
    again, reason = cp.assemble_packet(b.comparison, b.receipt, b.verdict, b.ctx)
    assert reason is None and again == out.packet
    assert cp.verify_packet(out.packet, b.comparison, b.receipt, b.verdict, b.ctx)
    forged = {**out.packet, "authority": {"release": True, "delivery": True}}
    assert not cp.verify_packet(forged, b.comparison, b.receipt, b.verdict, b.ctx)
    # Consumer 1: fresh interpreter re-derives the packet from the source documents.
    payload = {
        "comparison": b.comparison,
        "receipt": b.receipt,
        "verdict": b.verdict,
        "ctx": b.ctx.as_json() | {"worker_id": b.ctx.worker_id},
    }
    script = (
        "import json,sys\n"
        "from opentour_cell.pilot import comparison_packet as cp\n"
        "q=json.load(sys.stdin)\n"
        "p,r=cp.assemble_packet(q['comparison'],q['receipt'],q['verdict'],"
        "cp.PacketContext(**q['ctx']))\n"
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
